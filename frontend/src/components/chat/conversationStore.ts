import { create } from 'zustand';
import { aiApi } from '../../services/api';
import { getInviteToken } from '../../services/betaAccess';
import type { ChatConversation } from '../../types';

interface Conversations {
  records: ChatConversation[];
  selectedId: string | null;
  current: ChatConversation | null;
  initialized: boolean;
  loading: boolean;
  busy: boolean;
  error: string;
}

const EMPTY: Conversations = { records: [], selectedId: null, current: null, initialized: false, loading: false, busy: false, error: '' };
const useStore = create<{ projects: Record<string, Conversations> }>(() => ({ projects: {} }));
const inFlight = new Map<string, Promise<void>>();
// Cache is memory-only and identity-scoped. Only the selected ID goes to sessionStorage.
export const conversationKey = (projectId: string) => `${getInviteToken()}:${projectId}`;
export const useConversations = (key: string) => useStore((s) => s.projects[key] || EMPTY);
const get = (key: string) => useStore.getState().projects[key] || EMPTY;
export const changeConversations = (key: string, patch: Partial<Conversations>) => {
  useStore.setState((s) => ({ projects: { ...s.projects, [key]: { ...get(key), ...patch } } }));
};
const storageKey = (projectId: string) => `novelcraft-chat-selected:${projectId}`;
const remember = (projectId: string, id: string | null) => {
  try { if (id) sessionStorage.setItem(storageKey(projectId), id); else sessionStorage.removeItem(storageKey(projectId)); } catch { /* Selection is optional. */ }
};
function savedSelection(projectId: string) {
  try { return sessionStorage.getItem(storageKey(projectId)); } catch { return null; }
}
function singleFlight(key: string, work: () => Promise<void>) {
  const existing = inFlight.get(key);
  if (existing) return existing;
  const pending = work().finally(() => { inFlight.delete(key); });
  inFlight.set(key, pending);
  return pending;
}
export const reportChatError = (key: string, error: unknown) => changeConversations(key, {
  error: error instanceof Error ? error.message : '无法读取对话，请稍后重试。',
});

export function refreshConversation(projectId: string, key: string, id: string) {
  return singleFlight(`${key}:detail:${id}`, async () => {
    const response = await aiApi.conversations.get(projectId, id);
    if (!response.data || get(key).selectedId !== id) return;
    const current = get(key).current;
    if (current?.id === id && current.revision > response.data.revision) return;
    const record = response.data;
    changeConversations(key, { current: record, records: get(key).records.map((r) => r.id === id ? record : r) });
  });
}

export async function selectConversation(projectId: string, key: string, id: string | null) {
  changeConversations(key, { selectedId: id, current: null, loading: !!id, error: '' });
  remember(projectId, id);
  try { if (id) await refreshConversation(projectId, key, id); }
  catch (error) { reportChatError(key, error); }
  finally { if (get(key).selectedId === id) changeConversations(key, { loading: false }); }
}

export function loadConversations(projectId: string, key: string) {
  return singleFlight(`${key}:list`, async () => {
    changeConversations(key, { error: '' });
    if (!get(key).initialized) changeConversations(key, { loading: true });
    try {
      const [active, archived] = await Promise.all([aiApi.conversations.list(projectId), aiApi.conversations.list(projectId, true)]);
      const records = [...(active.data?.records || []), ...(archived.data?.records || [])];
      const preferred = get(key).selectedId || (!get(key).initialized ? savedSelection(projectId) : null);
      const id = records.find((r) => r.id === preferred)?.id || records.find((r) => !r.archived)?.id || null;
      changeConversations(key, { records, initialized: true });
      if (id !== get(key).selectedId || !get(key).current) await selectConversation(projectId, key, id);
      else await refreshConversation(projectId, key, id!);
    } catch (error) { reportChatError(key, error); }
    finally { changeConversations(key, { loading: false }); }
  });
}

export async function newConversation(projectId: string, key: string) {
  const result = await aiApi.conversations.create(projectId);
  if (!result.data) throw new Error('新建对话失败。');
  const current = result.data;
  remember(projectId, current.id);
  changeConversations(key, { records: [current, ...get(key).records], current, selectedId: current.id, error: '' });
  return current.id;
}

export async function manageConversation(projectId: string, key: string, id: string, action: 'archive' | 'restore' | 'delete') {
  if (action === 'delete') await aiApi.conversations.delete(projectId, id);
  else await aiApi.conversations.update(projectId, id, { archived: action === 'archive' });
  if (get(key).selectedId === id) {
    changeConversations(key, { selectedId: null, current: null });
    remember(projectId, null);
  }
  await loadConversations(projectId, key);
}

// Synchronous lock shared by full/compact panels, acquired before any network await.
export function acquireChat(key: string) {
  if (get(key).busy || get(key).loading) return false;
  changeConversations(key, { busy: true, error: '' });
  return true;
}
