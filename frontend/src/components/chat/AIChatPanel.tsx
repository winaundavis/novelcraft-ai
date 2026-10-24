import { useCallback, useEffect, useMemo, useRef, useState } from 'react';import { Bot, Lightbulb, MessageCircle, RefreshCw, Send, ThumbsDown, ThumbsUp, User } from 'lucide-react';

import { aiApi, chapterApi, isStylePlanConflict } from '../../services/api';
import { fetchCurrentStylePlanToken, stylePlanTokenFields } from '../../services/stylePlanToken';
import { useEditorStore } from '../../stores';
import type { ChatContextProfile, ChatMessage, CopilotAction, CopilotCandidate, OutlineCandidate } from '../../types';
import OutlineCandidateCard from './OutlineCandidateCard';
import { isProseEditRequest } from './proseEditIntent';
import { chatFailureMessage } from './outlineFailureMessage';
import { canRewriteSelection } from './copilotGuards';
import { chapterMutationBlockReason, notifyChapterChanged, notifyStyleTransitionChanged } from '../editor/chapterEvents';
import ConversationHistory from './ConversationHistory';
import { acquireChat, changeConversations, conversationKey, loadConversations, newConversation, refreshConversation, reportChatError, useConversations } from './conversationStore';

const WELCOME_MESSAGE: ChatMessage = {
  id: 'welcome',
  role: 'assistant',
  content: '你好，我可以帮你续写场景、打磨细节、做剧情头脑风暴，也能一起拆解角色动机和章节卡点。',
  timestamp: new Date().toISOString(),
};

const QUICK_ACTIONS = [
  {
    label: '续写当前场景',
    prompt: '请在保持当前语气、人物状态和剧情连续性的前提下，续写当前章节。',
    testId: 'continue-scene',
    profile: 'continuation' as ChatContextProfile,
    action: 'continue' as CopilotAction,
  },
  {
    label: '增强细节描写',
    prompt: '请加强当前场景里的环境细节、情绪表现和氛围感，同时避免空泛抒情。',
    testId: 'enhance-detail',
    profile: 'scene_writing' as ChatContextProfile,
    action: 'expand' as CopilotAction,
  },
  {
    label: '头脑风暴灵感',
    prompt: '请围绕当前故事做一轮头脑风暴，给我 6 条可直接落地的剧情或场景灵感。',
    testId: 'brainstorm',
    profile: 'plot_planning' as ChatContextProfile,
    action: 'branch' as CopilotAction,
  },
  {
    label: '下一章走向',
    prompt: '请给我 3 个可信、差异明显的下一章发展方向，并说明各自的戏剧效果。',
    testId: 'next-chapter',
    profile: 'plot_planning' as ChatContextProfile,
    action: 'analyze_next' as CopilotAction,
  },
  {
    label: '设计新角色',
    prompt: '请设计一个适合当前故事世界的新角色，说明他与主线的冲突点和出场价值。',
    testId: 'new-character',
    profile: 'plot_planning' as ChatContextProfile,
    action: 'character' as CopilotAction,
  },
  {
    label: '分析本场',
    prompt: '请分析当前场景的目标、冲突、状态变化、节奏和可执行修改点。',
    testId: 'analyze-scene',
    profile: 'scene_writing' as ChatContextProfile,
    action: 'analyze_scene' as CopilotAction,
  },
  {
    label: '检查连续性',
    prompt: '请检查当前章节的人物、事实、时间线、世界规则和伏笔连续性。',
    testId: 'continuity',
    profile: 'consistency_check' as ChatContextProfile,
    action: 'continuity' as CopilotAction,
  },
  {
    label: '检查伏笔',
    prompt: '请检查当前章节对活跃伏笔的承接、推进或回收。',
    testId: 'foreshadow',
    profile: 'consistency_check' as ChatContextProfile,
    action: 'foreshadow' as CopilotAction,
  },
  {
    label: '改写当前段',
    prompt: '请改写当前选区或当前正文，保留事实与剧情意图，只改善表达。',
    testId: 'rewrite',
    profile: 'scene_writing' as ChatContextProfile,
    action: 'rewrite' as CopilotAction,
  },
];

/**
 * 候选所属的计划版本，三种状态必须区分开：
 * - `no-plan`：后端明确告知当时**未启用**计划（显式 `stylePlanId: ''` + `revision: 0`）。
 * - `versioned`：有可信版本号。
 * - `unrecorded`：历史候选没有持久化计划元数据（`null`/缺失）。
 *   这**不等于**「未启用计划」——不能把未知状态冒充成已知状态，
 *   否则作者会在不知情的情况下接受一个可能与当前计划冲突的候选。
 */
type CandidatePlanState = 'no-plan' | 'versioned' | 'unrecorded';

function candidatePlanState(candidate: CopilotCandidate): CandidatePlanState {
  if (candidate.stylePlanId === '' && candidate.stylePlanRevision === 0) return 'no-plan';
  if (typeof candidate.stylePlanId === 'string' && candidate.stylePlanId && typeof candidate.stylePlanRevision === 'number') {
    return 'versioned';
  }
  return 'unrecorded';
}

export default function AIChatPanel({ projectId, compact = false }: { projectId: string; compact?: boolean }) {
  const key = conversationKey(projectId);
  const chat = useConversations(key);
  const [input, setInput] = useState('');
  const isLoading = chat.busy || !!chat.current?.messages?.some((m) => m.status === 'pending');
  const unavailable = isLoading || chat.loading || !chat.initialized || !!chat.current?.archived;
  const messages = useMemo(() => chat.current?.messages?.length ? chat.current.messages : [WELCOME_MESSAGE], [chat.current]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  /**
   * 计划已变更、导致接受被 409 拒绝的候选 id 集合。
   * 这些候选**仍然保留**（状态仍是 proposed，正文与 diff 卡不变），
   * 只是需要用户主动点「基于最新计划重新生成」——不做任何自动重试。
   */
  const [staleCandidateIds, setStaleCandidateIds] = useState<Record<string, string>>({});
  const editorProjectId = useEditorStore((state) => state.activeProjectId);  const currentChapterId = useEditorStore((state) => state.currentChapterId);
  const chapterContent = useEditorStore((state) => state.chapterContent);
  const selectionText = useEditorStore((state) => state.selectionText);
  const storeSetChapterContent = useEditorStore((state) => state.setChapterContent);
  // 刷新后恢复：对话消息里可能不再带着候选（例如重新载入会话/消息被裁剪），
  // 从后端拉取仍处于 proposed 的大纲候选，确保用户仍能应用/拒绝。
  const [restoredOutlineCandidates, setRestoredOutlineCandidates] = useState<OutlineCandidate[]>([]);

  const refreshOutlineCandidates = useCallback(async () => {
    try {
      const json = await aiApi.outlineCandidates.list(projectId, 'proposed');
      setRestoredOutlineCandidates((json.data || []).filter((item) => item?.status === 'proposed'));
    } catch {
      // 拉取失败（离线/未授权/端点不可用）不影响对话使用。
      setRestoredOutlineCandidates([]);
    }
  }, [projectId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isLoading]);

  useEffect(() => { void loadConversations(projectId, key); }, [projectId, key]);
  useEffect(() => {
    if (!isLoading || !chat.selectedId) return;
    const id = chat.selectedId;
    const timer = window.setInterval(() => {
      void refreshConversation(projectId, key, id).catch((error) => reportChatError(key, error));
    }, 2500);
    return () => window.clearInterval(timer);
  }, [isLoading, chat.selectedId, projectId, key]);
  useEffect(() => {
    const refresh = () => { void loadConversations(projectId, key); };
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [projectId, key]);
  useEffect(() => { setInput(''); }, [key, chat.selectedId]);
  useEffect(() => { void refreshOutlineCandidates(); }, [refreshOutlineCandidates, chat.selectedId, isLoading]);

  // 已被对话消息渲染过的候选不再重复展示。
  const renderedOutlineCandidateIds = useMemo(() => {
    const ids = new Set<string>();
    messages.forEach((message) => {
      const c = message.metadata?.intent === 'outline_edit' ? message.metadata.candidate as OutlineCandidate | undefined : undefined;
      if (c?.id) ids.add(c.id);
    });
    return ids;
  }, [messages]);

  const pendingOutlineCandidates = useMemo(
    () => restoredOutlineCandidates.filter(
      (item) => item?.status === 'proposed' && !renderedOutlineCandidateIds.has(item.id),
    ),
    [restoredOutlineCandidates, renderedOutlineCandidateIds],
  );

  const trimmedInput = useMemo(() => input.trim(), [input]);

  const handleSend = async (
    text?: string,
    profile?: ChatContextProfile,
    action?: CopilotAction,
    regenerateMessageId?: string,
    forceProseEdit = false,
    candidateSourceMessageId?: string,
  ) => {
    const content = text || trimmedInput;
    if (!content || unavailable) return;
    // 口语化/多样表述交给统一判定，避免"调整正文"的指令落到普通对话里。
    // forceProseEdit：用户在回复上点“按此指令修改正文”时强制走正文候选（兼底）。
    const proseEditRequested = !action && editorProjectId === projectId && !!currentChapterId
      && (forceProseEdit || isProseEditRequest(content));
    const effectiveAction = proseEditRequested ? 'rewrite' as CopilotAction : action;
    if (effectiveAction === 'rewrite' && !proseEditRequested && !regenerateMessageId && !canRewriteSelection(selectionText)) {
      changeConversations(key, { error: '请先在正文中选择要改写的文字，再使用改写功能。' });
      return;
    }
    if (!acquireChat(key)) return;
    let conversationId = chat.selectedId;
    try {
      if (!conversationId) conversationId = await newConversation(projectId, key);
      const requestId = crypto.randomUUID();
      const turn = {
        conversationId,
        requestId,
        ...(candidateSourceMessageId ? {} : { regenerateMessageId }),
      };
      const editableActions = new Set<CopilotAction>(['continue', 'rewrite', 'expand', 'condense', 'style', 'character', 'emotion', 'pacing']);
      const sourceMessageId = candidateSourceMessageId || regenerateMessageId;
      const previous = sourceMessageId ? messages.find((m) => m.id === sourceMessageId) : null;
      const originalCandidate = previous?.metadata?.kind === 'copilot' ? previous.metadata.candidate as CopilotCandidate : null;
      const originalOperation = previous?.metadata?.kind === 'copilot' ? previous.metadata.operation : null;
      if (originalOperation || originalCandidate || effectiveAction && editableActions.has(effectiveAction) && editorProjectId === projectId && currentChapterId) {
        const blockedReason = chapterMutationBlockReason(projectId);
        if (blockedReason) throw new Error(blockedReason);
        const targetChapterId = originalOperation?.chapterId || originalCandidate?.chapter_id || currentChapterId!;
        const editor = useEditorStore.getState();
        const isCurrent = editor.activeProjectId === projectId && editor.currentChapterId === targetChapterId;
        const currentText = isCurrent ? editor.chapterContent : (await chapterApi.get(projectId, targetChapterId)).data?.content || '';
        // Replay the instruction/action, not the old draft snapshot. A prior
        // full-chapter selection remains a full-chapter edit after Accept.
        const oldSelection = originalOperation?.selectionText ?? originalCandidate?.original ?? '';
        const oldFullText = originalOperation?.currentText ?? originalCandidate?.original ?? '';
        const nextSelection = previous
          ? (oldSelection && oldSelection !== oldFullText ? oldSelection : '')
          : isCurrent ? editor.selectionText : '';
        if (nextSelection && !currentText.includes(nextSelection)) throw new Error('原选区已更新，请在当前正文中重新选择要修改的文字。');
        // 候选必须带**创建时**的最新计划版本：服务端用它校验，陈旧会被 409 拒绘。
        // 不信任缓存的 token（另一个标签页/自动创作可能已推进计划）。
        const planToken = await fetchCurrentStylePlanToken(projectId);
        if (!planToken) throw new Error('读取当前风格过渡计划失败，候选尚未生成。');
        await aiApi.copilot.createCandidate({
          ...turn,
          projectId,
          chapterId: targetChapterId,
          action: originalOperation?.action || originalCandidate?.rewrite_type || effectiveAction!,
          currentText,
          selectionText: nextSelection,
          instruction: originalOperation?.instruction || content,
          ...stylePlanTokenFields(planToken),
        });
      } else await aiApi.chat(
        projectId,
        [{ role: 'user', content }],
        editorProjectId === projectId
          ? {
              ...turn,
              chapterId: currentChapterId || undefined,
              currentText: chapterContent.slice(-4000),
              selectionText: selectionText.slice(0, 2000),
              contextProfile: profile,
              action,
            }
          : { ...turn, contextProfile: profile, action },
      );
      if (!regenerateMessageId) setInput('');
    } catch (error) {
      if (!regenerateMessageId) setInput(content);
      reportChatError(key, error);
    } finally {
      try { if (conversationId) await refreshConversation(projectId, key, conversationId); }
      catch (error) { reportChatError(key, error); }
      changeConversations(key, { busy: false });
    }
  };

  const handleRegenerate = async (assistantMessageId: string) => {
    const index = messages.findIndex((m) => m.id === assistantMessageId);
    const user = messages.slice(0, index).reverse().find((m) => m.role === 'user');
    const previous = messages[index];
    if (user && previous?.metadata?.kind === 'copilot' && previous.metadata.candidate) {
      await handleSend(user.content, undefined, undefined, undefined, false, assistantMessageId);
    } else if (user) {
      await handleSend(user.content, undefined, undefined, assistantMessageId);
    }
  };
  const candidateAction = async (work: () => Promise<unknown>) => {
    if (unavailable || !acquireChat(key)) return;
    try {
      await work();
    } catch (error) { reportChatError(key, error); }
    finally {
      try { if (chat.selectedId) await refreshConversation(projectId, key, chat.selectedId); }
      catch (error) { reportChatError(key, error); }
      changeConversations(key, { busy: false });
      void refreshOutlineCandidates();
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid={compact ? 'assistant-panel-compact' : 'assistant-panel'}>
      {!compact && (
        <div className="flex items-center gap-2 border-b border-surface-200 px-4 py-3 dark:border-surface-700">
          <MessageCircle size={18} className="text-primary-600" />
          <span className="text-sm font-semibold">写作助手</span>
          <span className="ml-auto rounded bg-success-soft px-1.5 py-0.5 text-[11px] text-success dark:bg-success-soft-dark dark:text-success-dark">
            可用
          </span>
        </div>
      )}

      <ConversationHistory projectId={projectId} chatKey={key} disabled={isLoading || chat.loading} />
      {chat.error && <div role="alert" className="mx-3 my-2 rounded bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-900/20 dark:text-amber-200">{chat.error}<button className="ml-2 underline" onClick={() => void loadConversations(projectId, key)}>重新加载记录</button></div>}
      {chat.loading && <div className="p-3 text-xs text-surface-500">正在读取对话…</div>}
      {chat.current?.archived && <div className="px-3 py-2 text-xs text-surface-500">此对话已归档，恢复后可继续聊天。</div>}

      <div className="min-h-0 flex-1 space-y-4 overflow-auto px-4 py-3">
        {messages.map((message, index) => {
          const candidate = message.metadata?.kind === 'copilot' ? message.metadata.candidate as CopilotCandidate | undefined : undefined;            const planState = candidate ? candidatePlanState(candidate) : 'unrecorded';          const outlineCandidate = message.metadata?.intent === 'outline_edit' ? message.metadata.candidate as OutlineCandidate | undefined : undefined;
          // 兼底入口：普通回复（非候选）且有前一条用户指令时，允许一键改为正文修改，
          // 避免口语化表述漏判后用户“以为正文被改、实际没改”。
          const precedingInstruction = message.role === 'assistant'
            ? (messages.slice(0, index).reverse().find((item) => item.role === 'user')?.content || '')
            : '';
          const canOfferProseFallback = message.role === 'assistant'
            && !candidate && !outlineCandidate
            && editorProjectId === projectId && !!currentChapterId
            && !!precedingInstruction;
          return (
          <div key={message.id}>
          <div
            key={message.id}
            className={`flex gap-2.5 ${message.role === 'user' ? 'justify-end' : ''}`}
            data-testid={message.role === 'assistant' ? 'assistant-message' : undefined}
          >
            {message.role === 'assistant' && (
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-100 dark:bg-primary-900/40">
                <Bot size={14} className="text-primary-600" />
              </div>
            )}

            <div
              className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed ${
                message.role === 'user'
                  ? 'rounded-br-md bg-primary-600 text-white'
                  : 'rounded-bl-md bg-surface-100 text-surface-800 dark:bg-surface-750 dark:text-surface-200'
              }`}
            >
              {message.metadata?.mode === 'failed' ? chatFailureMessage(message.metadata) : `${message.metadata?.mode === 'fallback' ? '当前使用本地建议（不是模型生成）。\n' : ''}${message.content}`}
              {message.status === 'pending' && <span className="text-surface-500">{message.content ? '\n正在重新生成…' : '正在思考…刷新后可继续查看。'}</span>}
              {message.role === 'assistant' && message.id !== 'welcome' && (
                <div className="mt-2 flex items-center gap-1 border-t border-surface-200 pt-2 dark:border-surface-600">
                  <button className="btn-ghost p-1" title="这条建议有帮助">
                    <ThumbsUp size={12} />
                  </button>
                  <button className="btn-ghost p-1" title="这条建议还可以更好">
                    <ThumbsDown size={12} />
                  </button>
                  <button
                    className="btn-ghost ml-auto p-1 disabled:opacity-50"
                    title="重新生成回复"
                    aria-label="重新生成回复"
                    disabled={unavailable}
                    onClick={() => void handleRegenerate(message.id)}
                  >
                    <RefreshCw size={12} />
                  </button>
                  {canOfferProseFallback && (
                    <button
                      className="btn-ghost px-1.5 py-1 text-[11px] disabled:opacity-50"
                      title="把上一条指令按正文修改处理，生成可接受的候选"
                      disabled={unavailable}
                      onClick={() => void handleSend(precedingInstruction, undefined, undefined, undefined, true)}
                      data-testid="apply-instruction-to-prose"
                    >
                      改为修改正文
                    </button>
                  )}
                </div>
              )}
            </div>

            {message.role === 'user' && (
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface-300 dark:bg-surface-600">
                <User size={14} className="text-surface-600 dark:text-surface-300" />
              </div>
            )}
          </div>
        {candidate && candidate.status === 'proposed' && (
          <div className="rounded-lg border border-primary-200 bg-primary-50 p-3 text-sm dark:border-primary-800 dark:bg-primary-900/20" data-testid="copilot-candidate">
            <div className="mb-2 font-semibold">候选文本</div>
            <div className="mb-2 text-xs text-surface-500" data-testid="copilot-context-summary">
              此候选属于生成时的章节；接受后写回对应正文。
            </div>
            <div className="mb-2 flex items-center gap-2 text-xs text-surface-500">
              <span data-testid="copilot-style-plan-version">
                {planState === 'no-plan'
                  ? '未启用渐进计划'
                  : planState === 'versioned'
                    ? `风格计划版本 ${candidate.stylePlanRevision}`
                    : '未记录风格计划版本'}
              </span>
              {staleCandidateIds[candidate.id] && (
                <span className="text-danger" data-testid="copilot-candidate-stale">计划已更新</span>
              )}
            </div>
            {planState === 'unrecorded' && (
              // 历史候选没有计划版本：无法判断它是否仍与当前计划一致，
              // 因此始终给出显式的「重新生成」入口，而不是等 Accept 被 409 之后才提示。
              <div className="mb-2 rounded border border-warning bg-warning-soft p-2 text-xs text-warning" data-testid="copilot-unrecorded-plan-notice">
                <div className="mb-1 font-semibold">该候选未记录风格计划版本</div>
                <div className="mb-1">
                  它生成时没有持久化计划版本，无法判断是否仍与当前计划一致。
                  接受可能被服务端拒绝；请基于最新计划重新生成。
                </div>
                <button
                  className="btn-ghost mt-2 px-2 py-1 text-xs"
                  disabled={unavailable}
                  onClick={() => void handleRegenerate(message.id)}
                  data-testid="copilot-regenerate-unrecorded"
                >
                  基于最新计划重新生成
                </button>
              </div>
            )}
            {staleCandidateIds[candidate.id] && (
              <div className="mb-2 rounded border border-warning bg-warning-soft p-2 text-xs text-warning" data-testid="copilot-stale-notice">
                <div className="mb-1 font-semibold">风格过渡计划已更新</div>
                <div className="mb-1">{staleCandidateIds[candidate.id]}</div>
                <div>候选与正文均已保留，不会自动重试。请基于最新计划重新生成后再接受。</div>
                <button
                  className="btn-ghost mt-2 px-2 py-1 text-xs"
                  disabled={unavailable}
                  onClick={() => void handleRegenerate(message.id)}
                  data-testid="copilot-regenerate-after-conflict"
                >
                  基于最新计划重新生成
                </button>
              </div>
            )}
            <div className="mb-2 whitespace-pre-wrap text-surface-700 dark:text-surface-200">{candidate.candidate}</div>
            <div className="mb-2 text-xs text-surface-500">变更 {candidate.diff.length} 处，原文尚未修改</div>
            <div className="flex gap-2">
              <button disabled={unavailable} className="btn-primary px-2 py-1 text-xs" onClick={() => void candidateAction(async () => {
                const blockedReason = chapterMutationBlockReason(projectId);
                if (blockedReason) throw new Error(blockedReason);
                const before = useEditorStore.getState().chapterContent;
                let accepted;
                try {
                  // 服务端用候选持久化时记录的 token 校验，而不是面板当前看到的计划。
                  accepted = await aiApi.copilot.accept(candidate.id);
                } catch (acceptError) {
                  if (isStylePlanConflict(acceptError)) {
                    // 候选保持 proposed，正文与 diff 卡都不动；只标记并给出用户主动入口。
                    setStaleCandidateIds((prev) => ({
                      ...prev,
                      [candidate.id]:
                        acceptError instanceof Error && acceptError.message
                          ? acceptError.message
                          : '风格过渡计划或进度已更新，候选未写入正文。',
                    }));
                  }
                  throw acceptError;
                }
                const savedContent = accepted.data?.applied_content
                  ?? (await chapterApi.get(projectId, candidate.chapter_id)).data?.content;
                if (savedContent === undefined) throw new Error('修改已保存，但正文读取失败，请重新打开该章节。');
                const editor = useEditorStore.getState();
                if (editor.activeProjectId === projectId && editor.currentChapterId === candidate.chapter_id) {
                  if (chapterMutationBlockReason(projectId) || editor.chapterContent !== before) {
                    throw new Error('候选已保存；编辑器保留了你刚输入的新修改，请先核对后再保存。');
                  }
                  storeSetChapterContent(savedContent);
                  editor.setSelectionText('');
                  editor.markClean();
                }
                // 接受会推进完成数：用响应里的最新进度刷新导航/字数/进度，不自行推算。
                const transition = accepted.data?.styleTransition;
                if (transition) notifyStyleTransitionChanged({ projectId, transition });
                setStaleCandidateIds((prev) => {
                  if (!(candidate.id in prev)) return prev;
                  const next = { ...prev };
                  delete next[candidate.id];
                  return next;
                });
                notifyChapterChanged({ projectId, chapterId: candidate.chapter_id,
                  action: 'copilot-accept', appliedContent: savedContent });
              })} data-testid="copilot-accept">接受</button>
              <button disabled={unavailable} className="btn-ghost px-2 py-1 text-xs" onClick={() => void candidateAction(async () => {
                await aiApi.copilot.reject(candidate.id);
              })} data-testid="copilot-reject">拒绝</button>
              <button disabled={unavailable} className="btn-ghost px-2 py-1 text-xs" onClick={() => void handleRegenerate(message.id)} data-testid="copilot-regenerate">重新生成</button>
            </div>
          </div>
        )}
        {outlineCandidate && outlineCandidate.status === 'proposed' && (
          <OutlineCandidateCard
            candidate={outlineCandidate}
            onApply={(confirmed = false) => candidateAction(async () => {
              await aiApi.outlineCandidates.apply(outlineCandidate.id, confirmed);
              window.dispatchEvent(new Event('novelcraft:project-refresh'));
            })}
            onReject={() => candidateAction(() => aiApi.outlineCandidates.reject(outlineCandidate.id))}
          />
        )}
          </div>
        ); })}

        {pendingOutlineCandidates.map((item) => (
          <div key={item.id} data-testid="restored-outline-candidate">
            <div className="mb-1 text-[11px] text-surface-500">刷新后恢复的待处理大纲候选</div>
            <OutlineCandidateCard
              candidate={item}
              onApply={(confirmed = false) => candidateAction(async () => {
                await aiApi.outlineCandidates.apply(item.id, confirmed);
                window.dispatchEvent(new Event('novelcraft:project-refresh'));
              })}
              onReject={() => candidateAction(() => aiApi.outlineCandidates.reject(item.id))}
            />
          </div>
        ))}

        {isLoading && !messages.some((m) => m.status === 'pending') && (
          <div className="flex gap-2.5">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-100 dark:bg-primary-900/40">
              <Bot size={14} className="text-primary-600" />
            </div>
            <div className="rounded-2xl rounded-bl-md bg-surface-100 px-4 py-3 text-sm text-surface-500 dark:bg-surface-750">
              正在思考...
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {messages.length <= 1 && !chat.current?.archived && !chat.loading && (
        <div className="px-4 pb-2">
          <p className="mb-2 text-xs text-surface-400">快速提示</p>
          <div className="flex flex-wrap gap-1.5">
            {QUICK_ACTIONS.map((action) => (
              <button
                key={action.label}
                disabled={unavailable}
                onClick={() => void handleSend(action.prompt, action.profile, action.action)}
                className="inline-flex items-center gap-1 rounded-full bg-surface-100 px-2.5 py-1.5 text-xs text-surface-600 transition-colors hover:bg-primary-50 hover:text-primary-600 dark:bg-surface-700 dark:text-surface-300 dark:hover:bg-primary-900/30 dark:hover:text-primary-400"
                data-testid={compact ? `assistant-quick-${action.testId}-compact` : `assistant-quick-${action.testId}`}
              >
                {action.testId === 'brainstorm' && <Lightbulb size={12} />}
                {action.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="border-t border-surface-200 p-3 dark:border-surface-700">
        <div className="flex items-center gap-2">
          <input
            data-testid="chat-input"
            aria-label="聊天消息"
            disabled={unavailable}
            className="min-w-0 flex-1 rounded-lg bg-surface-100 px-3.5 py-2 text-sm outline-none placeholder:text-surface-400 focus:ring-1 focus:ring-primary-500 dark:bg-surface-700"
            placeholder="输入你的问题，比如情节推进、章节润色、脑暴方向或角色冲突..."
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void handleSend();
              }
            }}
          />
          <button
            onClick={() => void handleSend()}
            disabled={!trimmedInput || unavailable}
            className="btn-primary shrink-0 p-2 disabled:bg-surface-300 disabled:opacity-100 dark:disabled:bg-surface-600"
            title="发送消息"
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
