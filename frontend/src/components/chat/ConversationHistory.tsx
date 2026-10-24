import { useState } from 'react';
import { Archive, ArchiveRestore, ChevronDown, History, Plus, Trash2, X } from 'lucide-react';
import type { ChatConversation } from '../../types';
import { acquireChat, changeConversations, manageConversation, newConversation, reportChatError, selectConversation, useConversations } from './conversationStore';

export default function ConversationHistory({ projectId, chatKey, disabled }: { projectId: string; chatKey: string; disabled: boolean }) {
  const chat = useConversations(chatKey);
  const [open, setOpen] = useState(false);
  const [archived, setArchived] = useState(false);
  const [deleting, setDeleting] = useState<ChatConversation | null>(null);
  const run = async (work: () => Promise<unknown>) => {
    if (disabled || !acquireChat(chatKey)) return;
    try { await work(); } catch (error) { reportChatError(chatKey, error); }
    finally { changeConversations(chatKey, { busy: false }); }
  };
  return <div className="relative shrink-0 border-b border-surface-200 dark:border-surface-700">
    <div className="flex items-center gap-1 px-3 py-2">
      <button data-testid="chat-history-toggle" aria-expanded={open} onClick={() => setOpen(!open)} className="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-surface-100 dark:hover:bg-surface-700" title="对话记录">
        <History size={15} className="shrink-0 text-surface-500" /><span className="truncate">{chat.current?.title || '对话记录'}</span><ChevronDown size={14} className={`ml-auto shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      <button data-testid="chat-new" title="新建对话" disabled={disabled || !chat.initialized} onClick={() => void run(async () => { await newConversation(projectId, chatKey); setOpen(false); })} className="btn-ghost flex shrink-0 items-center gap-1 px-2 text-xs disabled:opacity-40"><Plus size={15} />新对话</button>
    </div>
    {open && <div className="absolute inset-x-2 top-full z-40 rounded-xl border border-surface-200 bg-white p-2 shadow-xl dark:border-surface-600 dark:bg-surface-800">
      <div className="mb-2 flex items-center gap-1 border-b border-surface-200 pb-2 dark:border-surface-700">
        <button data-testid="chat-history-active" onClick={() => setArchived(false)} className={`rounded px-3 py-1 text-xs ${!archived ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/30' : 'text-surface-500'}`}>最近对话</button>
        <button data-testid="chat-history-archived" onClick={() => setArchived(true)} className={`rounded px-3 py-1 text-xs ${archived ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/30' : 'text-surface-500'}`}>已归档</button>
        <button title="关闭对话记录" className="btn-ghost ml-auto p-1" onClick={() => setOpen(false)}><X size={14} /></button>
      </div>
      <div className="max-h-64 space-y-1 overflow-y-auto">
        {chat.records.filter((r) => r.archived === archived).map((record) => <div key={record.id} className={`flex items-center gap-1 rounded-lg px-1 ${chat.selectedId === record.id ? 'bg-primary-50 dark:bg-primary-900/20' : 'hover:bg-surface-100 dark:hover:bg-surface-700'}`}>
          <button disabled={disabled} data-testid={`chat-conversation-${record.id}`} className="min-w-0 flex-1 px-2 py-2 text-left disabled:opacity-40" onClick={() => { void selectConversation(projectId, chatKey, record.id); setOpen(false); }}>
            <span className="block truncate text-sm">{record.title}</span><span className="block text-[10px] text-surface-400">{new Date(record.updated_at).toLocaleDateString()}</span>
          </button>
          <button disabled={disabled} data-testid={`chat-${record.archived ? 'restore' : 'archive'}-${record.id}`} title={record.archived ? '恢复对话' : '归档对话'} className="btn-ghost p-1.5 disabled:opacity-40" onClick={() => void run(() => manageConversation(projectId, chatKey, record.id, record.archived ? 'restore' : 'archive'))}>{record.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}</button>
          <button disabled={disabled} data-testid={`chat-delete-${record.id}`} title="删除对话" className="btn-ghost p-1.5 text-surface-400 hover:text-red-600 disabled:opacity-40" onClick={() => setDeleting(record)}><Trash2 size={14} /></button>
        </div>)}
        {!chat.records.some((r) => r.archived === archived) && <p className="p-4 text-center text-xs text-surface-400">{archived ? '暂无归档对话' : '暂无对话，点击“新对话”开始'}</p>}
      </div>
    </div>}
    {deleting && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="删除对话确认" onKeyDown={(event) => { if (event.key === 'Escape') setDeleting(null); }}>
      <div className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl dark:bg-surface-800">
        <h3 className="font-semibold">删除这次对话？</h3>
        <p className="mt-2 break-words text-sm text-surface-500">“{deleting.title}”的聊天记录将被删除，无法恢复。已应用的大纲和正文修改不会撤销。</p>
        <div className="mt-5 flex justify-end gap-2"><button autoFocus className="btn-ghost" onClick={() => setDeleting(null)}>取消</button><button data-testid="chat-delete-confirm" className="rounded-lg bg-red-600 px-3 py-2 text-sm text-white" onClick={() => void run(async () => { await manageConversation(projectId, chatKey, deleting.id, 'delete'); setDeleting(null); })}>删除对话</button></div>
      </div>
    </div>}
  </div>;
}
