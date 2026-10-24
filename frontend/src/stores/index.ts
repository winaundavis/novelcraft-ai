import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import type { LLMConfig, Outline, Project, StylePlanToken } from '../types';
import { createIndexedDBStorage } from '../storage/indexeddb';
import { pruneReviewSnapshots, reviewContentSignature, reviewSnapshotKey } from './reviewSnapshots';

// 快照的键、指纹与裁剪规则都在这个纯函数模块里（可被 node --test 直接覆盖）
export { pruneReviewSnapshots, reviewContentSignature, reviewSnapshotKey };
export type { ReviewSnapshot } from './reviewSnapshots';

const indexedDB = createIndexedDBStorage();
const indexedDBStorage = createJSONStorage(() => indexedDB);
const localFallback = createJSONStorage(() => localStorage);

const storage = (() => {
  try {
    if (typeof indexedDB !== 'undefined') return indexedDBStorage;
  } catch {
    // Fall back to localStorage.
  }
  return localFallback;
})();

interface ProjectStore {
  projects: Project[];
  currentProjectId: string | null;
  isLoading: boolean;
  setProjects: (projects: Project[]) => void;
  addProject: (project: Project) => void;
  updateProject: (id: string, updates: Partial<Project>) => void;
  deleteProject: (id: string) => void;
  setCurrentProject: (id: string | null) => void;
  readonly currentProject: Project | undefined;
}

export const useProjectStore = create<ProjectStore>()(
  persist(
    (set, get) => ({
      projects: [],
      currentProjectId: null,
      isLoading: false,
      setProjects: (projects) => set({ projects }),
      addProject: (project) => set((state) => ({ projects: [...state.projects, project] })),
      updateProject: (id, updates) => set((state) => ({
        projects: state.projects.map((project) => (
          project.id === id ? { ...project, ...updates } : project
        )),
      })),
      deleteProject: (id) => set((state) => ({
        projects: state.projects.filter((project) => project.id !== id),
        currentProjectId: state.currentProjectId === id ? null : state.currentProjectId,
      })),
      setCurrentProject: (id) => set({ currentProjectId: id }),
      get currentProject() {
        return get().projects.find((project) => project.id === get().currentProjectId);
      },
    }),
    {
      name: 'nc-projects',
      storage,
    },
  ),
);

export type AutoWriteMode = 'all' | 'current-to-end' | 'first-unfinished' | 'single';
export type AutoWriteStatus = 'idle' | 'running' | 'paused' | 'stopped' | 'completed' | 'error';

export interface AutoWriteSession {
  mode: AutoWriteMode;
  status: AutoWriteStatus;
  currentChapterId: string | null;
  completedChapterIds: string[];
  failedChapterIds: string[];
  /**
   * 作业自己报告的进度（优先于用本地大纲推算出来的 `completedChapterIds`）。
   *
   * 为什么必须单独存：`completedChapterIds` 是拿「本地大纲的前 N 个节点」推出来的，
   * 而 `current-to-end` 这类从中途开始的任务，N 只覆盖大纲后半段 ——
   * 拿大纲全长当分母就永远到不了 100%，看起来就像进度条不动。
   */
  totalChapters: number;
  completedChapters: number;
  lastSummary: string;
  progressMessage: string;
  startedAt: string | null;
  updatedAt: string | null;
  errorMessage: string;
}

export interface DraftStylePlan {
  token: StylePlanToken;
  owner: { projectId: string; chapterId: string; id: string };
}

interface EditorProjectState {
  chapterContent: string;
  currentChapterId: string | null;
  outlineData: Outline | null;
  autoWriteSession: AutoWriteSession;
  draftStylePlan: DraftStylePlan | null;
}

interface EditorStore extends EditorProjectState {
  activeProjectId: string;
  editorDataMap: Record<string, EditorProjectState>;
  isDirty: boolean;
  wordCount: number;
  selectionText: string;
  contentRevision: number;
  setDraftStylePlan: (projectId: string, draft: DraftStylePlan | null) => void;
  setActiveProject: (projectId: string) => void;
  setCurrentChapter: (id: string | null) => void;
  setChapterContent: (content: string) => void;
  setSelectionText: (text: string) => void;
  markClean: () => void;
  setOutlineData: (outline: Outline | null) => void;
  updateAutoWriteSession: (patch: Partial<AutoWriteSession>) => void;
  resetAutoWriteSession: () => void;
}

const createEmptyAutoWriteSession = (): AutoWriteSession => ({
  mode: 'current-to-end',
  status: 'idle',
  currentChapterId: null,
  completedChapterIds: [],
  failedChapterIds: [],
  totalChapters: 0,
  completedChapters: 0,
  lastSummary: '',
  progressMessage: '',
  startedAt: null,
  updatedAt: null,
  errorMessage: '',
});

const createEmptyEditorProjectState = (): EditorProjectState => ({
  chapterContent: '',
  currentChapterId: null,
  outlineData: null,
  autoWriteSession: createEmptyAutoWriteSession(),
  draftStylePlan: null,
});

function ensureProjectState(projectState?: Partial<EditorProjectState> | null): EditorProjectState {
  const base = createEmptyEditorProjectState();
  return {
    chapterContent: projectState?.chapterContent ?? base.chapterContent,
    currentChapterId: projectState?.currentChapterId ?? base.currentChapterId,
    outlineData: projectState?.outlineData ?? base.outlineData,
    draftStylePlan: projectState?.draftStylePlan ?? null,
    autoWriteSession: {
      ...base.autoWriteSession,
      ...(projectState?.autoWriteSession || {}),
      completedChapterIds: projectState?.autoWriteSession?.completedChapterIds || [],
      failedChapterIds: projectState?.autoWriteSession?.failedChapterIds || [],
    },
  };
}

function syncProjectState(
  state: EditorStore,
  patch: Partial<EditorProjectState>,
  extra: Partial<Pick<EditorStore, 'isDirty' | 'wordCount' | 'selectionText' | 'contentRevision'>>,
): Partial<EditorStore> {
  const projectId = state.activeProjectId;
  const currentProjectState = ensureProjectState(
    projectId ? state.editorDataMap[projectId] : createEmptyEditorProjectState(),
  );
  const nextProjectState = ensureProjectState({
    ...currentProjectState,
    ...patch,
  });

  const nextEditorDataMap = projectId
    ? { ...state.editorDataMap, [projectId]: nextProjectState }
    : state.editorDataMap;

  return {
    ...nextProjectState,
    editorDataMap: nextEditorDataMap,
    ...extra,
  };
}

export const useEditorStore = create<EditorStore>()(
  persist(
    (set, get) => ({
      activeProjectId: '',
      editorDataMap: {},
      chapterContent: '',
      currentChapterId: null,
      outlineData: null,
      autoWriteSession: createEmptyAutoWriteSession(),
      isDirty: false,
      wordCount: 0,
      selectionText: '',
      contentRevision: 0,
      draftStylePlan: null,
      setActiveProject: (projectId) => set((state) => {
        const target = ensureProjectState(state.editorDataMap[projectId]);
        const opened = { ...target, chapterContent: '', draftStylePlan: null };
        return {
          activeProjectId: projectId,
          // Chapter正文 is authoritative in the backend. Do not hydrate a
          // stale local draft on project reopen; MarkdownEditor will fetch the
          // selected chapter by id and restore the persisted content.
          chapterContent: '',
          draftStylePlan: null,
          // Keep the cached entry consistent with the reopened server-backed view.
          // Otherwise a later setChapterContent would resurrect the old draft token.
          editorDataMap: { ...state.editorDataMap, [projectId]: opened },
          contentRevision: state.contentRevision + 1,
          currentChapterId: target.currentChapterId,
          outlineData: target.outlineData,
          autoWriteSession: target.autoWriteSession,
          isDirty: false,
          wordCount: 0,
          selectionText: '',
        };
      }),
      setDraftStylePlan: (projectId, draft) => set((state) => {
        if (state.activeProjectId !== projectId) return state;
        if (draft && (draft.owner.projectId !== projectId || state.currentChapterId !== draft.owner.chapterId)) return state;
        return syncProjectState(state, { draftStylePlan: draft }, {});
      }),
      setCurrentChapter: (id) => set((state) => syncProjectState(state, {
        currentChapterId: id,
        draftStylePlan: state.currentChapterId === id ? state.draftStylePlan : null,
      }, { contentRevision: state.contentRevision + Number(state.currentChapterId !== id) })),
      setChapterContent: (content) => set((state) => syncProjectState(
        state,
        { chapterContent: content },
        {
          isDirty: true,
          wordCount: content.replace(/\s/g, '').length,
          contentRevision: state.contentRevision + Number(state.chapterContent !== content),
        },
      )),
      setSelectionText: (text) => set({ selectionText: text }),
      markClean: () => set({ isDirty: false }),
      setOutlineData: (outline) => set((state) => syncProjectState(state, { outlineData: outline }, {})),
      updateAutoWriteSession: (patch) => set((state) => {
        const nextSession: AutoWriteSession = {
          ...state.autoWriteSession,
          ...patch,
          updatedAt: new Date().toISOString(),
          completedChapterIds: patch.completedChapterIds || state.autoWriteSession.completedChapterIds,
          failedChapterIds: patch.failedChapterIds || state.autoWriteSession.failedChapterIds,
        };

        if (nextSession.status === 'running' && !nextSession.startedAt) {
          nextSession.startedAt = new Date().toISOString();
        }

        return syncProjectState(state, { autoWriteSession: nextSession }, {});
      }),
      resetAutoWriteSession: () => set((state) => syncProjectState(
        state,
        { autoWriteSession: createEmptyAutoWriteSession() },
        {},
      )),
    }),
    {
      name: 'nc-editor',
      storage,
      partialize: (state) => ({
        activeProjectId: state.activeProjectId,
        editorDataMap: state.editorDataMap,
      }),
    },
  ),
);

/**
 * 审校结果快照：按「项目::章节」保留，切到正文/其他功能再回到审校页时不丢失。
 *
 * **2026-10-04 起落盘**（`nc-review`）。此前刻意不持久化，理由见 `reviewSnapshots.ts` 顶部：
 * 作者实测发现工作台闲置一段时间后整页重新加载，审校结果就没了；而「怕旧结果被误用」
 * 这件事已由 `contentSignature` + 面板「结果可能已过期」提示 + 禁用一键修改覆盖，
 * 不必再用丢数据来换。快照上限由 `REVIEW_SNAPSHOT_LIMIT` 兜住。
 */
export interface ReviewStore {
  snapshots: Record<string, ReviewSnapshotLike>;
  setSnapshot: (key: string, snapshot: ReviewSnapshotLike) => void;
  markIssueApplied: (key: string, issueKey: string) => void;
  setLastApplied: (key: string, lastApplied: ReviewSnapshotLike['lastApplied']) => void;
  clearSnapshot: (key: string) => void;
  clearAll: () => void;
}

type ReviewSnapshotLike = import('./reviewSnapshots').ReviewSnapshot;

export const useReviewStore = create<ReviewStore>()(
  persist(
    (set) => ({
      snapshots: {},
      // 写入时顺手裁剪：既约束内存里的快照数，也约束落盘的大小（同一个 state 走 persist）
      setSnapshot: (key, snapshot) => set((state) => ({
        snapshots: pruneReviewSnapshots({ ...state.snapshots, [key]: snapshot }),
      })),
      markIssueApplied: (key, issueKey) => set((state) => {
        const current = state.snapshots[key];
        if (!current || current.appliedIssueKeys.includes(issueKey)) return state;
        return {
          snapshots: {
            ...state.snapshots,
            [key]: { ...current, appliedIssueKeys: [...current.appliedIssueKeys, issueKey] },
          },
        };
      }),
      setLastApplied: (key, lastApplied) => set((state) => {
        const current = state.snapshots[key];
        if (!current) return state;
        return { snapshots: { ...state.snapshots, [key]: { ...current, lastApplied } } };
      }),
      clearSnapshot: (key) => set((state) => {
        const next = { ...state.snapshots };
        delete next[key];
        return { snapshots: next };
      }),
      clearAll: () => set({ snapshots: {} }),
    }),
    {
      name: 'nc-review',
      storage,
    },
  ),
);

interface ChatStore {
  messages: import('../types').ChatMessage[];
  isStreaming: boolean;
  streamingContent: string;
  addMessage: (msg: import('../types').ChatMessage) => void;
  setStreaming: (value: boolean) => void;
  appendStreamContent: (chunk: string) => void;
  clearChat: () => void;
}

export const useChatStore = create<ChatStore>()(
  persist(
    (set) => ({
      messages: [],
      isStreaming: false,
      streamingContent: '',
      addMessage: (msg) => set((state) => ({ messages: [...state.messages, msg] })),
      setStreaming: (value) => set({ isStreaming: value, streamingContent: value ? '' : '' }),
      appendStreamContent: (chunk) => set((state) => ({ streamingContent: state.streamingContent + chunk })),
      clearChat: () => set({ messages: [], streamingContent: '' }),
    }),
    {
      name: 'nc-chat',
      storage,
      partialize: (state) => ({ messages: state.messages.slice(-50) }),
    },
  ),
);

interface SettingsStore {
  llmConfigs: LLMConfig[];
  activeLLMConfigId: string | null;
  theme: 'light' | 'dark';
  setLLMConfigs: (configs: LLMConfig[]) => void;
  setActiveLLMConfig: (id: string | null) => void;
  toggleTheme: () => void;
}

export const useSettingsStore = create<SettingsStore>()(
  persist(
    (set) => ({
      llmConfigs: [],
      activeLLMConfigId: null,
      theme: (localStorage.getItem('nc-theme') as 'light' | 'dark') || 'light',
      setLLMConfigs: (configs) => set({ llmConfigs: configs }),
      setActiveLLMConfig: (id) => set({ activeLLMConfigId: id }),
      toggleTheme: () => set((state) => {
        const nextTheme = state.theme === 'light' ? 'dark' : 'light';
        localStorage.setItem('nc-theme', nextTheme);
        document.documentElement.classList.toggle('dark', nextTheme === 'dark');
        return { theme: nextTheme };
      }),
    }),
    {
      name: 'nc-settings',
      storage,
    },
  ),
);

// ── 权限（管理员判定）────────────────────────────────────────────────
// **刻意不持久化**：权限是跟账号走的。一旦写进 localStorage/IndexedDB，
// 上一个管理员账号的 `isAdmin: true` 会残留到下一个登录的账号，
// 等于把管理入口永久敲开。所以每次打开页面都重新问服务端一次。
interface PermissionStore {
  /**
   * 是否管理员。`null` 表示**还没问过服务端**（未确定）——
   * 它和「确定不是」必须分开：否则管理员打开页面时会先闪一下「仅管理员可见」。
   */
  isAdmin: boolean | null;
  setPermissions: (isAdmin: boolean) => void;
}

export const usePermissionStore = create<PermissionStore>()((set) => ({
  isAdmin: null,
  setPermissions: (isAdmin) => set({ isAdmin }),
}));
