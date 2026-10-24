import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, ClipboardList, Code, Eye, Loader2, Play, RotateCcw, Save, SkipForward, Sparkles, Square, Wand2 } from 'lucide-react';

import { Button, useToast } from '../common';
import ProjectionStatusPanel from '../memory/ProjectionStatusPanel';
import ChapterContextBar from './ChapterContextBar';
import { STYLE_PLAN_CONFLICT_CODE, ApiError, aiApi, chapterApi, isStylePlanConflict, projectApi, styleApi } from '../../services/api';
import { buildCharacterContexts } from '../../services/characterContext';
import { type AutoWriteMode, useEditorStore, useProjectStore } from '../../stores';
import type { ContinueRequest, NovelJobPayload, Outline, OutlineNode, StylePlanToken } from '../../types';
import { resolveChapterSaveMetadata } from './chapterMetadata';
import { isSameGeneration } from './generationGuard';
import { resolveAutoWriteProgress } from './autoWriteProgress';
import { resolveChapterReconcile } from './chapterReconcile';
import { CHAPTER_CHANGED_EVENT, CHAPTER_MUTATION_CHECK_EVENT, notifyChapterChanged, notifyStyleTransitionChanged, type ChapterChangedDetail, type ChapterMutationCheck } from './chapterEvents';
import { fetchCurrentStylePlanToken, stylePlanTokenFields } from '../../services/stylePlanToken';

const serifFontFamily = '"STSong", "Songti SC", "Noto Serif CJK SC", Georgia, serif';

type SaveStatus = 'idle' | 'saved' | 'error';
type StopMode = 'pause' | 'stop' | null;

function friendlyGenerationStatus(message: string, status: string): string {
  const value = `${message} ${status}`.toLowerCase();
  if (status === 'completed') return '完成';
  if (value.includes('projection') || value.includes('state') || value.includes('状态')) return '正在更新故事状态';
  if (value.includes('commit') || value.includes('保存') || value.includes('persist')) return '正在保存章节';
  if (value.includes('context') || value.includes('上下文')) return '正在整理本章上下文';
  if (status === 'queued') return '正在整理本章上下文';
  if (status === 'running') return '正在生成正文';
  return message || '正在处理本章';
}

function collectChapterNodes(outlineData: Outline | null): OutlineNode[] {
  if (!outlineData) return [];
  return outlineData.nodes.filter((node) => node.type === 'chapter').sort((a, b) => a.order - b.order);
}

function extractProtagonist(premise: string): string {
  const match = premise.match(/([\u4e00-\u9fff]{2,4})/);
  return match?.[1] || '主角';
}

function buildFallbackDraft(chapter: OutlineNode, protagonist: string): string {
  return `# ${chapter.title}\n\n> ${chapter.summary || '本章围绕新的冲突展开，并推动主线继续向前。'}\n\n${protagonist}站在情节转折前的临界点，已经意识到局势与最初的设想不再相同。本章的核心冲突是：${chapter.conflict || '人物目标与外部阻力发生正面冲突。'} 随着场景推进，一个新的信息被揭开：${chapter.revelation || '事情的真相比表面更加复杂。'} 情绪基调保持在“${chapter.emotion || '紧张'}”，并在结尾留下下一章继续爆发的空间。`;
}

function renderMarkdown(markdown: string): string {
  let html = markdown
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  html = html
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^---$/gm, '<hr>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>');

  return `<p>${html}</p>`;
}

function deriveCompletedChapterIds(job: NovelJobPayload, chapterNodes: OutlineNode[]): string[] {
  if (job.completed_chapters <= 0) return [];

  let orderedNodes = [...chapterNodes];
  if (job.mode === 'current-to-end' && job.requested_chapter_id) {
    const startIndex = orderedNodes.findIndex((node) => node.id === job.requested_chapter_id);
    if (startIndex >= 0) orderedNodes = orderedNodes.slice(startIndex);
  }

  return orderedNodes.slice(0, Math.min(job.completed_chapters, orderedNodes.length)).map((node) => node.id);
}

// QA P1：末章失败（failed + pending_manual_recovery 且失败章为最后一章）——
// 后端已将该任务从可恢复列表剔除（is_recoverable=False），此处供前端展示
// 「请先人工处理该章节」提示，避免用户误点恢复。
function isLastChapterFailure(job: NovelJobPayload, chapterNodes: OutlineNode[]): boolean {
  if (job.status !== 'failed' || !job.pending_manual_recovery || chapterNodes.length === 0) return false;
  let failedId = '';
  try {
    const target = JSON.parse(job.resume_target_json || '{}') as { chapter_id?: string };
    failedId = (target.chapter_id || '').trim();
  } catch {
    return false;
  }
  if (!failedId) return false;
  return chapterNodes[chapterNodes.length - 1].id === failedId;
}

export default function MarkdownEditor({
  projectId,
  isEmpty = false,
  outlineData = null,
  premise = '',
  isProjectReady = true,
  onNavigateToTasks,
}: {
  projectId: string;
  isEmpty?: boolean;
  outlineData?: Outline | null;
  premise?: string;
  isProjectReady?: boolean;
  /** 「前往任务中心」回调（ui-task-center.md §2.1：恢复横幅保留为快捷入口） */
  onNavigateToTasks?: () => void;
}) {
  const project = useProjectStore((state) => state.projects.find((item) => item.id === projectId));
  const updateProject = useProjectStore((state) => state.updateProject);
  const toast = useToast();
  const {
    currentChapterId,
    autoWriteSession,
    setCurrentChapter,
    updateAutoWriteSession,
    resetAutoWriteSession,
    setChapterContent: storeSetChapterContent,
    setSelectionText,
    setOutlineData,
  } = useEditorStore();

  const [viewMode, setViewMode] = useState<'edit' | 'preview' | 'split'>('edit');
  const [detailPanel, setDetailPanel] = useState<'generation' | 'memory' | null>(null);
  const [isAiWriting, setIsAiWriting] = useState(false);
  const [isDeai, setIsDeai] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveMessage, setSaveMessage] = useState('');
  const [draftAtMount] = useState(() => {
    const editor = useEditorStore.getState();
    return editor.activeProjectId === projectId && editor.isDirty
      ? { chapterId: editor.currentChapterId, content: editor.chapterContent, stylePlan: editor.draftStylePlan }
      : null;
  });
  const [chapterContent, setChapterContent] = useState(draftAtMount?.content ?? '');
  const [persistedChapterMeta, setPersistedChapterMeta] = useState<{ title?: string; order?: number; volumeId?: string } | null>(null);
  /**
   * 章节名的编辑草稿（AI 在大纲阶段起名，作者可在此直接改）。
   * 初值取**已保存的章节名**（服务端那一份），没有才退回大纲节点标题 ——
   * 顺序不能反：大纲标题是 AI 原名，反了会把作者改过的名字盖回去。
   */
  const [chapterTitleDraft, setChapterTitleDraft] = useState('');
  // Codex P2 复测 P2：保存逻辑读取旧 state 的竞态修复——用 ref 同步最新内容，
  // 避免 React 异步渲染未提交时 handleSave 闭包捕获旧 chapterContent（保存空内容）。
  // onChange 同步更新 ref，handleSave 从 ref 读取最新值，不依赖固定延时。
  const chapterContentRef = useRef(draftAtMount?.content ?? '');
  const loadedChapterIdRef = useRef<string | null>(draftAtMount?.chapterId ?? null);
  const chapterReadVersionRef = useRef(0);
  const updateChapterContent = useCallback((value: string) => {
    chapterContentRef.current = value;
    setChapterContent(value);
  }, []);
  const [isDirty, setIsDirty] = useState(Boolean(draftAtMount));
  const isDirtyRef = useRef(Boolean(draftAtMount));
  const [chapterRevision, setChapterRevision] = useState(0);
  const [isAutoWriting, setIsAutoWriting] = useState(false);
  const [autoWriteMode, setAutoWriteMode] = useState<AutoWriteMode>(autoWriteSession.mode || 'current-to-end');
  const [autoProgress, setAutoProgress] = useState(autoWriteSession.progressMessage || '');
  const [autoStatusMessage, setAutoStatusMessage] = useState('');
  const [autoSourceMessage, setAutoSourceMessage] = useState('');
  const [aiDirection, setAiDirection] = useState('');
  const [aiWords, setAiWords] = useState(500);
  const wordCount = useMemo(() => chapterContent.replace(/\s/g, '').length, [chapterContent]);

  const continueAbortRef = useRef<AbortController | null>(null);
  const continueRequestRef = useRef<{ id: string; projectId: string; chapterId: string } | null>(null);
  const saveRequestRef = useRef<{ id: string; projectId: string; chapterId: string } | null>(null);
  const novelJobUnsubscribeRef = useRef<(() => void) | null>(null);
  const elapsedTimerRef = useRef<number | null>(null);
  const autoWriteStartedAtRef = useRef<number | null>(null);
  const activeNovelJobIdRef = useRef<string | null>(null);
  const stopModeRef = useRef<StopMode>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  // M2：可恢复任务入口（后端重启中断 / 失败章待处理）
  const [recoverableJob, setRecoverableJob] = useState<NovelJobPayload | null>(null);
  const [isResuming, setIsResuming] = useState(false);
  // QA P1：末章失败提示（无恢复入口）
  const [lastChapterFailure, setLastChapterFailure] = useState(false);
  /**
   * 因风格计划变化而停下的后台任务（checkpoint_type = style_plan_conflict）。
   * 它**不在**可恢复列表里，也不会自动恢复：只展示被保留的候选正文。
   */
  const [stylePlanConflictJob, setStylePlanConflictJob] = useState<NovelJobPayload | null>(null);

  /**
   * 章节正文读失败的原因（404 = 这一章已不在项目里）。
   * 有值时界面必须说清楚：否则作者看到的是一张空白正文 +「本章尚未创作」，
   * 会以为自己的正文丢了，而真实原因是这一章可能刚被导入替换或移入回收站。
   */
  const [chapterLoadError, setChapterLoadError] = useState('');

  useEffect(() => {
    const checkMutation = (event: Event) => {
      const detail = (event as CustomEvent<ChapterMutationCheck>).detail;
      if (detail.projectId !== projectId) return;
      detail.checked = true;
      if (isSaving) detail.blockedReason = '章节正在保存，请保存完成后切换视图。';
      else if (detail.intent === 'unmount') return;
      else if (isDirtyRef.current || isDirty) detail.blockedReason = '请先保存当前章节的未保存修改。';
      else if (isAiWriting || isDeai || isAutoWriting || isResuming) {
        detail.blockedReason = '章节正在保存或生成，请等待完成后再操作。';
      }
    };
    const refreshChapter = (event: Event) => {
      const detail = (event as CustomEvent<ChapterChangedDetail>).detail;
      if (detail.projectId !== projectId || detail.chapterId !== currentChapterId || isDirtyRef.current || isDirty) return;
      if (detail.action === 'recycle') {
        updateChapterContent('');
        storeSetChapterContent('');
        setPersistedChapterMeta(null);
      }
      if (detail.action === 'restore' && detail.restoredContent !== undefined) {
        updateChapterContent(detail.restoredContent);
        storeSetChapterContent(detail.restoredContent);
        setSaveStatus('idle');
        setSaveMessage('');
      }
      if (detail.action === 'copilot-accept' && detail.appliedContent !== undefined) {
        // Shared chat state alone does not update this editor's text/ref.
        // Invalidate an older GET before publishing the accepted snapshot.
        chapterReadVersionRef.current += 1;
        updateChapterContent(detail.appliedContent);
        storeSetChapterContent(detail.appliedContent);
        setSelectionText('');
        useEditorStore.getState().markClean();
        setIsDirty(false);
        isDirtyRef.current = false;
        setSaveStatus('saved');
        setSaveMessage('已接受并保存');
      }
      if (detail.action === 'restore' || detail.action === 'recycle') setChapterRevision((value) => value + 1);
    };
    window.addEventListener(CHAPTER_MUTATION_CHECK_EVENT, checkMutation);
    window.addEventListener(CHAPTER_CHANGED_EVENT, refreshChapter);
    return () => {
      window.removeEventListener(CHAPTER_MUTATION_CHECK_EVENT, checkMutation);
      window.removeEventListener(CHAPTER_CHANGED_EVENT, refreshChapter);
    };
  }, [currentChapterId, isAiWriting, isAutoWriting, isDeai, isDirty, isResuming, isSaving, projectId, setSelectionText, storeSetChapterContent, updateChapterContent]);

  const protagonist = useMemo(() => extractProtagonist(premise), [premise]);
  const chapterNodes = useMemo(() => collectChapterNodes(outlineData), [outlineData]);
  /**
   * 当前大纲的实时快照，只给**章节加载 effect 的 catch** 用（判断"这个节点还在不在大纲里"）。
   *
   * 为什么不直接把 chapterNodes 放进那个 effect 的依赖：项目快照每次刷新都会产生
   * **新的 outline 对象**（保存章节 → notifyChapterChanged → notifyProjectRefresh →
   * setOutlineData），放进依赖会让加载 effect 跟着重跑 —— 于是**每次保存都白拉一次整章正文**。
   * 用 ref 读最新值，既保留 404 的判据，又不改变"什么时候该重取"。
   */
  const chapterNodesRef = useRef(chapterNodes);
  useEffect(() => {
    chapterNodesRef.current = chapterNodes;
  }, [chapterNodes]);
  const activeOutlineChapter = useMemo(
    () => chapterNodes.find((node) => node.id === currentChapterId) || null,
    [chapterNodes, currentChapterId],
  );
  const currentChapterIndex = useMemo(
    () => chapterNodes.findIndex((node) => node.id === currentChapterId),
    [chapterNodes, currentChapterId],
  );

  // 进度以**作业自己报告的**完成数/总数为准（抽成纯函数以便单测，见 autoWriteProgress.ts）。
  const autoProgressInfo = resolveAutoWriteProgress(autoWriteSession, chapterNodes.length);

  const outlineSummary = project?.outline?.mainSummary || outlineData?.mainSummary || '';
  // M4（P1-3）：风格契约（预设五维 + 画像规则 + 过渡档位）从后端拉取，替代原来一行 styleTone
  const [styleTone, setStyleTone] = useState<string>(project?.styleConfig?.tone || '');
  /**
   * 当前生效的渐进计划版本。
   * 只能来自 style-contract 顶层字段：`stylePlanId === '' && stylePlanRevision === 0` 表示未启用。
   */
  const [stylePlanToken, setStylePlanToken] = useState<StylePlanToken | null>(null);
  /**
   * 本次生成草稿所属的计划版本（来自 SSE 的 started 事件）。
   * 保存时必须回传**这个**版本，而不是保存时的最新版本，否则服务端无法判断
   * “这段正文是按哪一版计划生成的”，会误拒或误放。
   */
  const draftStylePlanRef = useRef<StylePlanToken | null>(draftAtMount?.stylePlan?.token ?? null);
  const draftStylePlanOwnerRef = useRef<{ projectId: string; chapterId: string; id: string } | null>(draftAtMount?.stylePlan?.owner ?? null);
  const bindDraftStylePlan = useCallback((
    token: StylePlanToken | null,
    owner: { projectId: string; chapterId: string; id: string } | null,
  ) => {
    draftStylePlanRef.current = token;
    draftStylePlanOwnerRef.current = owner;
    useEditorStore.getState().setDraftStylePlan(projectId, token && owner ? { token, owner } : null);
  }, [projectId]);
  /**
   * 当前项目 id 的实时快照。
   * 续写/保存都是长时间异步操作：用户中途切项目后，迟到的响应绝不能把旧项目的
   * 正文或计划写进新项目的编辑器状态。
   */
  const activeProjectRef = useRef(projectId);
  useEffect(() => {
    const projectChanged = activeProjectRef.current !== projectId;
    activeProjectRef.current = projectId;
    if (projectChanged) {
      // 切项目即失效：上一项目的草稿 token 与保存响应不得跨项目使用。
      draftStylePlanRef.current = null;
      draftStylePlanOwnerRef.current = null;
      saveRequestRef.current = null;
      setIsSaving(false);
      setSaveStatus('idle');
      setSaveMessage('');
    }
  }, [projectId]);

  // M4（P1-3）：续写前拉取结构化风格契约；无画像/分析失败时回退基础 tone，不阻塞续写
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void styleApi
      .getContract(projectId)
      .then((json) => {
        if (cancelled) return;
        const tone = json.data?.styleTone;
        if (tone) setStyleTone(tone);
        const planId = json.data?.stylePlanId;
        const revision = json.data?.stylePlanRevision;
        // 注意：不能用 `|| undefined` 判空——空串与 0 就是「未启用」的明确取值。
        setStylePlanToken(
          typeof planId === 'string' && typeof revision === 'number'
            ? { stylePlanId: planId, stylePlanRevision: revision }
            : null,
        );
      })
      .catch(() => {
        if (!cancelled) {
          setStyleTone(project?.styleConfig?.tone || '');
          setStylePlanToken(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, project?.styleConfig?.tone]);

  const characterContext = useMemo(
    () => buildCharacterContexts(project?.characters),
    [project],
  );

  const worldRuleContext = useMemo(() => (
    (project?.worldRules || []).map((rule) => ({
      category: rule.category,
      name: rule.name,
      description: rule.description,
    }))
  ), [project]);

  const stopNovelJobSync = useCallback(() => {
    novelJobUnsubscribeRef.current?.();
    novelJobUnsubscribeRef.current = null;
  }, []);

  const refreshProjectSnapshot = useCallback(async () => {
    try {
      const json = await projectApi.get(projectId);
      if (json.data) updateProject(projectId, json.data);
    } catch {
      // keep local state
    }
  }, [projectId, updateProject]);

  const persistChapter = useCallback(
    async (
      chapterId: string,
      content: string,
      chapterMeta?: OutlineNode | null,
      /** 生成该正文时使用的计划版本；普通手工保存不传（不推进完成数）。 */
      token?: StylePlanToken | null,
    ) => {
      const metadata = resolveChapterSaveMetadata(chapterMeta, persistedChapterMeta, chapterTitleDraft);
      return chapterApi.save(projectId, chapterId, {
        content,
        ...metadata,
        ...stylePlanTokenFields(token),
      });
    },
    [chapterTitleDraft, persistedChapterMeta, projectId],
  );

  const handleSave = useCallback(async () => {
    if (!projectId || !isProjectReady) return;

    const chapterId = currentChapterId || chapterNodes[0]?.id || `${projectId}-draft`;
    const requestIdentity = { id: crypto.randomUUID(), projectId, chapterId };
    saveRequestRef.current = requestIdentity;
    const isCurrentSave = () => isSameGeneration(saveRequestRef.current, requestIdentity, {
      componentProjectId: activeProjectRef.current,
      storeProjectId: useEditorStore.getState().activeProjectId,
      storeChapterId: useEditorStore.getState().currentChapterId,
    });
    setIsSaving(true);
    setSaveStatus('idle');
    setSaveMessage('');

    try {
      // Codex P2 复测 P2：从 ref 读取最新内容（onChange 已同步），
      // 避免 React 异步渲染未提交时闭包捕获旧 chapterContent 导致保存空内容。
      const latestContent = chapterContentRef.current;
      if (!currentChapterId) setCurrentChapter(chapterId);
      // 带本次生成草稿的原始 token：计划在生成与保存之间变化时服务端会 409 拒绘，
      // 绝不静默覆盖正文，也绝不为了绕开冲突而丢掉 token 再保存。
      const draftOwner = draftStylePlanOwnerRef.current;
      const draftToken = draftOwner?.projectId === projectId && draftOwner.chapterId === chapterId
        ? draftStylePlanRef.current
        : null;
      const submittedDraftOwner = draftOwner;
      const saved = await persistChapter(chapterId, latestContent, activeOutlineChapter, draftToken);
      if (!isCurrentSave()) return;
      // 章节名：作者改过就同步到「章节行 + 大纲节点」。
      // 大纲节点是左侧导航与后续生成提示词的来源；只改章节行不改大纲，
      // 就会出现「正文里改名了、导航还是 AI 原名」。
      const committedTitle = chapterTitleDraft.trim();
      if (committedTitle) {
        setPersistedChapterMeta((prev) => ({ ...(prev || {}), title: committedTitle }));
        const outline = useEditorStore.getState().outlineData;
        if (outline && activeOutlineChapter && committedTitle !== activeOutlineChapter.title) {
          const nextOutline: Outline = {
            ...outline,
            nodes: outline.nodes.map((node) => (
              node.id === activeOutlineChapter.id ? { ...node, title: committedTitle } : node
            )),
          };
          setOutlineData(nextOutline);
          // 失败不阻塞保存：正文已经落库，重命名下次保存会再试一次。
          void projectApi.update(projectId, { outline: nextOutline }).catch(() => undefined);
        }
      }
      // 保存成功才清空草稿 token；失败（含 409）时保留，供用户基于最新计划重新处理。
      if (
        draftToken
        && submittedDraftOwner
        && draftStylePlanOwnerRef.current?.id === submittedDraftOwner.id
        && draftStylePlanRef.current === draftToken
      ) {
        bindDraftStylePlan(null, null);
      }
      const transition = saved.data?.styleTransition;
      if (transition) {
        setStylePlanToken(
          transition.planId
            ? { stylePlanId: transition.planId, stylePlanRevision: transition.revision ?? 0 }
            : null,
        );
        notifyStyleTransitionChanged({ projectId, transition });
      }
      // A save acknowledges its submitted snapshot only; typing during the
      // request must remain dirty and must not be overwritten in the shared store.
      if (useEditorStore.getState().currentChapterId === chapterId) {
        const hasNewEdits = chapterContentRef.current !== latestContent;
        storeSetChapterContent(chapterContentRef.current);
        setIsDirty(hasNewEdits);
        isDirtyRef.current = hasNewEdits;
        if (!hasNewEdits) useEditorStore.getState().markClean();
        setSaveStatus('saved');
        setSaveMessage(hasNewEdits ? '已保存，仍有新修改未保存' : '已保存');
      }
      notifyChapterChanged({ projectId, chapterId, action: 'save' });
    } catch (error) {
      if (!isCurrentSave()) return;
      // 计划冲突是可恢复的业务状态：正文未被覆盖，只需基于最新计划重新生成。
      if (isStylePlanConflict(error)) {
        setSaveStatus('error');
        setSaveMessage(
          error instanceof Error && error.message
            ? error.message
            : '风格过渡计划或进度已更新，正文未保存；请基于最新计划重新生成。',
        );
        return;
      }
      const message = error instanceof Error ? error.message : '保存失败，请稍后重试。';
      setSaveStatus('error');
      setSaveMessage(message);
    } finally {
      if (saveRequestRef.current?.id === requestIdentity.id) {
        saveRequestRef.current = null;
        setIsSaving(false);
      }
    }
  }, [activeOutlineChapter, bindDraftStylePlan, chapterTitleDraft, currentChapterId, isProjectReady, persistChapter, projectId, setCurrentChapter, setOutlineData, storeSetChapterContent]);

  const applyNovelJobState = useCallback((job: NovelJobPayload, phase: 'bootstrap' | 'stream' = 'stream') => {
    activeNovelJobIdRef.current = job.id;

    if (job.checkpoint_type === 'style_plan_conflict') setStylePlanConflictJob(job);

    // 已发送停止/暂停请求时，不再用 SSE 推送的消息覆盖 pending 文本（E2E 稳定性）
    const hasStopPending = stopModeRef.current !== null;

    // QA P1：末章失败场景标记「请人工处理」提示（不展示恢复按钮）
    setLastChapterFailure(isLastChapterFailure(job, chapterNodes));

    // M2：任务进入运行态后隐藏"可恢复"横幅
    if (job.status === 'queued' || job.status === 'running') {
      setRecoverableJob(null);
    }

    // Historical cancelled/failed jobs retain their last chapter id, but must
    // never override an explicit user chapter selection.
    if (job.current_chapter_id && isProjectReady && (job.status === 'queued' || job.status === 'running')) {
      setCurrentChapter(job.current_chapter_id);
    }

    // 自动创作期间显示后端 SSE 推送的实时正文预览；预览不代表已提交，
    // 任务完成后再从正式章节重新读取，失败/取消时不会写入正文。
    if (
      (job.status === 'queued' || job.status === 'running')
      && job.preview_chapter_id
      && job.preview_chapter_id === (job.current_chapter_id || currentChapterId)
      && typeof job.preview_content === 'string'
    ) {
      updateChapterContent(job.preview_content);
      storeSetChapterContent(job.preview_content);
      setIsDirty(false);
      isDirtyRef.current = false;
    }

    const completedChapterIds = deriveCompletedChapterIds(job, chapterNodes);
    const failedChapterIds = job.failed_chapters > 0 && job.current_chapter_id ? [job.current_chapter_id] : [];
    const contextSummary = job.message?.includes('本次生成已参考') ? job.message : '';
    const sourceMessage = [
      job.llm_provider ? `模型：${job.llm_provider}${job.llm_model ? ` / ${job.llm_model}` : ''} · fallback 章节 ${job.fallback_chapters || 0}` : '',
      contextSummary,
    ].filter(Boolean).join(' · ');

    if (!hasStopPending) {
      setIsAutoWriting(job.status === 'queued' || job.status === 'running');
      const friendlyStatus = friendlyGenerationStatus(job.message || '', job.status);
      setAutoProgress(friendlyStatus);
      setAutoStatusMessage(friendlyStatus);
    }
    setAutoSourceMessage(sourceMessage);

    // 耗时计时器
    if (job.status === 'queued' || job.status === 'running') {
      if (!autoWriteStartedAtRef.current) {
        autoWriteStartedAtRef.current = Date.now();
      }
      if (elapsedTimerRef.current === null) {
        elapsedTimerRef.current = window.setInterval(() => {
          if (autoWriteStartedAtRef.current) {
            setElapsedSeconds(Math.floor((Date.now() - autoWriteStartedAtRef.current) / 1000));
          }
        }, 1000);
      }
    }

    updateAutoWriteSession({
      mode: job.mode,
      // 进度条与"已完成 N 章"都读作业自己报告的值，而不是拿本地大纲去推算（见 autoWriteProgress.ts）。
      totalChapters: job.total_chapters,
      completedChapters: job.completed_chapters,
      status:
        job.status === 'completed'
          ? 'completed'
          : job.status === 'failed'
            ? 'error'
            : job.status === 'cancelled'
              ? (stopModeRef.current === 'pause' ? 'paused' : 'stopped')
              : job.status === 'interrupted'
                ? 'paused'
                : 'running',
      currentChapterId: job.current_chapter_id || currentChapterId,
      completedChapterIds,
      failedChapterIds,
      progressMessage: job.message || '',
      errorMessage: job.status === 'failed' ? (job.error || job.message) : '',
    });

    if (job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled') {
      // 暂停模式下保持自动创作视图，让用户看到"已暂停"状态（E2E 需要可定位的文本）
      const isPaused = job.status === 'cancelled' && stopModeRef.current === 'pause';
      if (!isPaused) {
        stopNovelJobSync();
        activeNovelJobIdRef.current = null;
        setIsAutoWriting(false);
      } else {
        setAutoProgress('已暂停——当前章节完成后结束。');
        setAutoStatusMessage('已暂停');
      }

      // 停止耗时计时器
      if (elapsedTimerRef.current !== null) {
        window.clearInterval(elapsedTimerRef.current);
        elapsedTimerRef.current = null;
      }

      const completedChapterId = job.current_chapter_id || currentChapterId;
      if (phase === 'stream' && job.status === 'completed' && completedChapterId && isProjectReady) {
        void chapterApi.get(projectId, completedChapterId).then((json) => {
          if (json.data?.content) {
            updateChapterContent(json.data.content);
            setIsDirty(false);
          }
        }).catch(() => {});
      }

      void refreshProjectSnapshot();
      stopModeRef.current = null;
    }
  }, [chapterNodes, currentChapterId, isProjectReady, projectId, refreshProjectSnapshot, setCurrentChapter, stopNovelJobSync, storeSetChapterContent, updateAutoWriteSession, updateChapterContent]);

  const handleNovelJobStreamError = useCallback(async (jobId: string) => {
    try {
      const json = await aiApi.novelJob.get(jobId);
      if (json.data) {
        applyNovelJobState(json.data);
        return;
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '长篇创作任务状态实时同步失败。';
      setIsAutoWriting(false);
      setAutoProgress(message);
      setAutoStatusMessage(message);
      updateAutoWriteSession({ mode: autoWriteMode, status: 'error', progressMessage: message, errorMessage: message });
      stopNovelJobSync();
      activeNovelJobIdRef.current = null;
    }
  }, [applyNovelJobState, autoWriteMode, stopNovelJobSync, updateAutoWriteSession]);

  const startNovelJobStream = useCallback((jobId: string) => {
    stopNovelJobSync();
    activeNovelJobIdRef.current = jobId;
    novelJobUnsubscribeRef.current = aiApi.novelJob.subscribe(jobId, {
      onMessage: (job) => applyNovelJobState(job),
      onError: () => { void handleNovelJobStreamError(jobId); },
    });
  }, [applyNovelJobState, handleNovelJobStreamError, stopNovelJobSync]);

  const handleAutoWrite = useCallback(async () => {
    if (!projectId || chapterNodes.length === 0 || !isProjectReady) return;

    const currentStartChapterId = autoWriteMode === 'all'
      ? undefined
      : (currentChapterId || chapterNodes[0]?.id);

    setAutoProgress('正在启动自动创作任务...');
    setAutoStatusMessage('正在启动自动创作任务...');
    setIsAutoWriting(true);

    try {
      const stylePlanToken = await fetchCurrentStylePlanToken(projectId);
      if (!stylePlanToken) throw new Error('读取当前风格过渡计划失败，自动创作尚未启动。');
      const json = await aiApi.novelJob.start({
        projectId,
        mode: autoWriteMode,
        currentChapterId: currentStartChapterId,
        maxRetries: autoWriteMode === 'single' ? 0 : 2,
        ...stylePlanTokenFields(stylePlanToken),
      });
      if (!json.data) throw new Error('未拿到任务信息。');
      applyNovelJobState(json.data, 'bootstrap');
      startNovelJobStream(json.data.id);
      updateAutoWriteSession({
        mode: autoWriteMode,
        status: 'running',
        currentChapterId: currentStartChapterId || currentChapterId,
        progressMessage: json.data.message || '自动创作进行中。',
        errorMessage: '',
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : '自动创作启动失败。';
      setIsAutoWriting(false);
      setAutoProgress(message);
      setAutoStatusMessage(message);
      updateAutoWriteSession({ mode: autoWriteMode, status: 'error', progressMessage: message, errorMessage: message });
    }
  }, [applyNovelJobState, autoWriteMode, chapterNodes, currentChapterId, isProjectReady, projectId, startNovelJobStream, updateAutoWriteSession]);

  // M2：从断点恢复长篇创作任务（普通中断续写 / 跳过失败章继续）
  const handleResumeNovelJob = useCallback(async () => {
    if (!recoverableJob) return;
    setIsResuming(true);
    try {
      const json = await aiApi.novelJob.resume(recoverableJob.id);
      if (!json.data) throw new Error('未拿到恢复后的任务信息。');
      setRecoverableJob(null);
      applyNovelJobState(json.data, 'bootstrap');
      startNovelJobStream(json.data.id);
      updateAutoWriteSession({
        mode: json.data.mode,
        status: 'running',
        currentChapterId: json.data.current_chapter_id || currentChapterId,
        progressMessage: json.data.message || '自动创作已恢复。',
        errorMessage: '',
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : '恢复任务失败。';
      setAutoProgress(message);
      setAutoStatusMessage(message);
    } finally {
      setIsResuming(false);
    }
  }, [applyNovelJobState, currentChapterId, recoverableJob, startNovelJobStream, updateAutoWriteSession]);

  const requestStopNovelJob = useCallback(async (mode: StopMode) => {
    if (!activeNovelJobIdRef.current) return;
    stopModeRef.current = mode;
    const pendingMessage = mode === 'pause'
      ? '已请求在当前章节完成后结束。'
      : '已收到停止请求，将在当前章节完成后结束。';
    setAutoProgress(pendingMessage);
    setAutoStatusMessage(pendingMessage);
    try {
      const json = await aiApi.novelJob.cancel(activeNovelJobIdRef.current);
      if (json.data) applyNovelJobState(json.data);
    } catch (error) {
      const message = error instanceof Error ? error.message : '停止任务失败。';
      setAutoProgress(message);
      setAutoStatusMessage(message);
    }
  }, [applyNovelJobState]);

  const handleAiContinue = useCallback(async () => {
    if (!projectId || !isProjectReady) return;
    const chapterId = currentChapterId || chapterNodes[0]?.id || `${projectId}-draft`;
    const targetProjectId = projectId;
    const requestId = crypto.randomUUID();
    const requestIdentity = { id: requestId, projectId: targetProjectId, chapterId };
    continueRequestRef.current = requestIdentity;
    continueAbortRef.current?.abort();
    setIsAiWriting(true);
    const isCurrentRequest = () => isSameGeneration(continueRequestRef.current, requestIdentity, {
      componentProjectId: activeProjectRef.current,
      storeProjectId: useEditorStore.getState().activeProjectId,
      storeChapterId: useEditorStore.getState().currentChapterId,
    });

    // 契约必须在**发请求前**重新拉取：缓存的 styleTone / 计划版本可能已被
    // 另一个标签页或自动创作任务推进，用陈旧版本会被 409 拒绝。
    // 这里显式 await，确保写入 payload 的 token 与随后捕获的 started token 同源。
    let latestTone = styleTone;
    let latestToken: StylePlanToken | null = stylePlanToken;
    try {
      const contract = await styleApi.getContract(targetProjectId);
      if (!isCurrentRequest()) {
        if (continueRequestRef.current?.id === requestId) {
          continueRequestRef.current = null;
          setIsAiWriting(false);
        }
        return;
      }
      if (contract.data?.styleTone) latestTone = contract.data.styleTone;
      latestToken =
        typeof contract.data?.stylePlanId === 'string' && typeof contract.data?.stylePlanRevision === 'number'
          ? { stylePlanId: contract.data.stylePlanId, stylePlanRevision: contract.data.stylePlanRevision }
          : null;
      setStylePlanToken(latestToken);
    } catch {
      // 契约读取失败时沿用最近已知版本；保存仍绑定 SSE started 返回的 token。
    }
    if (!isCurrentRequest()) {
      if (continueRequestRef.current?.id === requestId) {
        continueRequestRef.current = null;
        setIsAiWriting(false);
      }
      return;
    }

    const contentBeforeGeneration = chapterContentRef.current;
    const dirtyBeforeGeneration = isDirtyRef.current;
    const draftTokenBeforeGeneration = draftStylePlanRef.current;
    const draftOwnerBeforeGeneration = draftStylePlanOwnerRef.current;
    let startedTokenReceived = false;

    const requestPayload: ContinueRequest = {
      projectId,
      chapterId,
      cursorPosition: chapterContent.length,
      contextBefore: chapterContent,
      direction: aiDirection,
      wordCount: aiWords,
      chapterTitle: activeOutlineChapter?.title,
      premise,
      outlineSummary,
      chapterSummary: activeOutlineChapter?.summary,
      styleTone: latestTone,
      characters: characterContext,
      worldRules: worldRuleContext,
      // 合同明确返回空 id/0 时仍发送成对 token；null 才表示合同读取失败。
      ...stylePlanTokenFields(latestToken),
    };

    // 新一次生成开始前先清空上一轮的草稿 token，避免串章节串计划。
    bindDraftStylePlan(null, null);
    continueAbortRef.current = aiApi.continueWriting(
      requestPayload,
      (chunk) => {
        if (!isCurrentRequest()) return;
        const next = chapterContentRef.current + chunk;
        chapterContentRef.current = next;
        setChapterContent(next);
        storeSetChapterContent(next);
        setIsDirty(true);
        isDirtyRef.current = true;
      },
      (result) => {
        const sameRequest = continueRequestRef.current?.id === requestId;
        const stillActive = isCurrentRequest();
        if (stillActive && result?.generationIncomplete) {
          // 生成被截断：正文已回滚到生成前；恢复请求前的草稿 token，
          // 避免第二次失败尝试让已有生成草稿变成无保护的手工保存。
          bindDraftStylePlan(draftTokenBeforeGeneration, draftOwnerBeforeGeneration);
          setChapterContent(contentBeforeGeneration);
          storeSetChapterContent(contentBeforeGeneration);
          if (!dirtyBeforeGeneration) useEditorStore.getState().markClean();
          chapterContentRef.current = contentBeforeGeneration;
          setIsDirty(dirtyBeforeGeneration);
          isDirtyRef.current = dirtyBeforeGeneration;
          toast.warning('AI 生成未完成', '本次内容达到输出上限，已保留生成前的正文。');
        }
        if (stillActive && !result?.error && !result?.generationIncomplete && !startedTokenReceived) {
          bindDraftStylePlan(draftTokenBeforeGeneration, draftOwnerBeforeGeneration);
          chapterContentRef.current = contentBeforeGeneration;
          setChapterContent(contentBeforeGeneration);
          storeSetChapterContent(contentBeforeGeneration);
          if (!dirtyBeforeGeneration) useEditorStore.getState().markClean();
          setIsDirty(dirtyBeforeGeneration);
          isDirtyRef.current = dirtyBeforeGeneration;
          setSaveStatus('error');
          setSaveMessage('未收到有效的风格计划版本，生成内容已撤回，未保存。');
        }
        if (stillActive && result?.error) {
          // 409 计划冲突：服务端未生成任何内容，也没有本地兜底文本。
          // 保留原文与已有草稿，只提示下一步动作，绝不自动重试。
          bindDraftStylePlan(draftTokenBeforeGeneration, draftOwnerBeforeGeneration);
          if (result.error.code === STYLE_PLAN_CONFLICT_CODE) {
            setSaveStatus('error');
            setSaveMessage(result.error.message);
            toast.error(
              '风格过渡计划已更新',
              `${result.error.message}（正文未改动，已保留你正在编辑的草稿）`,
            );
          } else {
            toast.warning('续写失败', result.error.message);
          }
        }
        if (sameRequest) {
          continueRequestRef.current = null;
          setIsAiWriting(false);
          continueAbortRef.current = null;
        }
      },
      (token) => {
        // started 事件下发的是本次生成的计划版本：把它绑定到这份草稿，
        // 之后保存必须回传同一个 token。
        if (!isCurrentRequest()) return;
        startedTokenReceived = true;
        bindDraftStylePlan(token, requestIdentity);
        setStylePlanToken(token);
      },
    );
  }, [activeOutlineChapter, aiDirection, aiWords, bindDraftStylePlan, chapterNodes, characterContext, currentChapterId, isProjectReady, outlineSummary, premise, projectId, storeSetChapterContent, stylePlanToken, styleTone, worldRuleContext]);

  // 建议1 去 AI 味后处理：调后端 /ai/deai，成功后用重写文本替换正文；
  // 后端不可用（API 层已降级 degraded=true）或失败时保留原文，不破坏正文。
  const handleDeai = useCallback(async () => {
    if (!projectId || !isProjectReady) return;
    const text = chapterContent;
    if (!text.trim()) {
      toast.warning('当前章节为空', '没有可处理的正文内容。');
      return;
    }

    setIsDeai(true);
    try {
      // 传 chapterId：后端才能拉到上一章正文，比对「开头重复 / 场景关键词组」。
      const json = await aiApi.deai(projectId, text, currentChapterId || '');
      const data = json.data;
      if (json.success === false || !data) {
        throw new Error('去 AI 味处理失败。');
      }
      // 后端不可用 / 重写被截断或过度缩写时 degraded=true，此时保留原文
      if (data.degraded) {
        toast.warning(
          '本次未改写正文',
          (data.keptWindows ?? 0) > 0
            ? '模型改写偏离原文（被截断、大幅缩写/扩写或改动了对话），为避免丢失正文已保留原文，可重试一次。'
            : 'AI 服务暂不可用，已保留原文，未做任何修改。',
        );
        return;
      }
      if (data.result && data.result !== text) {
        updateChapterContent(data.result);
        storeSetChapterContent(data.result);
        setIsDirty(true);
        const changed = data.changes?.length ?? 0;
        toast.success(
          '去 AI 味完成',
          changed > 0 ? `已调整 ${changed} 处 AI 味表达，请注意核对。` : '未发现明显 AI 味，正文保持不变。',
        );
      } else {
        toast.success('去 AI 味完成', '未发现明显 AI 味，正文保持不变。');
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '去 AI 味处理失败。';
      toast.error('去 AI 味失败', message);
    } finally {
      setIsDeai(false);
    }
  }, [chapterContent, currentChapterId, isProjectReady, projectId, storeSetChapterContent, toast]);

  const handlePauseAfterCurrent = useCallback(() => {
    void requestStopNovelJob('pause');
  }, [requestStopNovelJob]);

  const handleStopAutoWrite = useCallback(() => {
    void requestStopNovelJob('stop');
  }, [requestStopNovelJob]);

  const handleClearAutoSession = useCallback(() => {
    setAutoProgress('');
    setAutoStatusMessage('');
    setAutoSourceMessage('');
    setElapsedSeconds(0);
    autoWriteStartedAtRef.current = null;
    resetAutoWriteSession();
  }, [resetAutoWriteSession]);

  useEffect(() => {
    if (!projectId || !currentChapterId || !isProjectReady) return;

    let active = true;
    const readVersion = ++chapterReadVersionRef.current;
    setChapterLoadError('');
    // 切换章节时先清空旧章节，避免网络请求返回前继续显示上一章正文。
    // 随后只接受与 currentChapterId 相同的响应。
    if (loadedChapterIdRef.current !== currentChapterId) {
      updateChapterContent('');
      storeSetChapterContent('');
      setPersistedChapterMeta(null);
      setIsDirty(false);
      isDirtyRef.current = false;
      loadedChapterIdRef.current = currentChapterId;
    }
    const applyContent = (content: string) => {
      updateChapterContent(content);
      storeSetChapterContent(content);
    };

    chapterApi.get(projectId, currentChapterId)
      .then((json) => {
        if (!active || readVersion !== chapterReadVersionRef.current || useEditorStore.getState().currentChapterId !== currentChapterId) return;
        if (activeNovelJobIdRef.current) return;
        const chapter = json.data;
        setPersistedChapterMeta(chapter ? {
          title: chapter.title,
          order: chapter.order,
          volumeId: chapter.volumeId,
        } : null);
        const remoteContent = chapter?.content || '';
        if (isDirtyRef.current) return;
        if (remoteContent) {
          applyContent(remoteContent);
        } else if (activeOutlineChapter) {
          // A persisted empty draft is intentionally empty; do not synthesize
          // an outline here because it masks the real chapter state after a
          // restart and can overwrite a later content refresh.
          applyContent('');
        } else {
          applyContent('');
        }
        setIsDirty(false);
        isDirtyRef.current = false;
        useEditorStore.getState().markClean();
      })
      .catch((error: unknown) => {
        if (!active || readVersion !== chapterReadVersionRef.current) return;
        if (useEditorStore.getState().currentChapterId !== currentChapterId) return;
        setPersistedChapterMeta(null);
        // 404 有两种完全不同的含义，必须分开 —— 否则**新建项目一进正文**就会看到
        // 「这一章在服务端已不存在」，而其实它只是**还没被创作过**：
        //   ① 大纲里**有**这个节点、服务端还没建 chapters 行 → 正常的"尚未创作"空态；
        //   ② 大纲里**没有**这个节点 → 才是真的被整批替换 / 移入回收站。
        const status = error instanceof ApiError ? error.status : null;
        if (status === 404 && chapterNodesRef.current.some((node) => node.id === currentChapterId)) {
          setChapterLoadError('');
          // 有未保存正文时不清空（那会丢字），只是不再把它当成错误。
          if (!isDirtyRef.current) {
            applyContent('');
            setIsDirty(false);
            isDirtyRef.current = false;
            useEditorStore.getState().markClean();
          }
          return;
        }
        setChapterLoadError(
          status === 404
            ? '这一章已不在项目里（可能被导入整批替换，或已移入回收站），但左侧大纲里还留着它的入口。'
              + '请重新选择章节；在这里保存会把正文写成一个不在大纲中的新章节。'
            : `章节正文加载失败${error instanceof Error && error.message ? `：${error.message}` : ''}；下面显示的不是该章内容。`,
        );
      });

    return () => {
      active = false;
    };
    // 这里**故意不放 chapterNodes**：404 判定要读「这个节点还在不在大纲里」，但项目快照每次刷新
    // 都会产生新的 outline 对象，放进依赖会让本 effect 跟着重跑 —— 每次保存都白拉一次整章正文。
    // 改用 chapterNodesRef 读最新值，"什么时候重取"与改动前完全一致。
  }, [chapterRevision, currentChapterId, isProjectReady, projectId, storeSetChapterContent, updateChapterContent]);

  // 章节对账（决策抽成纯函数以便单测，见 chapterReconcile.ts）：
  //   ① 还没选中章节 → 选第一章；
  //   ② 选中的章节已不在大纲里（导入整批替换 / 回收站 / 大纲被重新生成）→ 切到第一章，
  //      否则会一直 404，而且保存会把正文写成一个大纲之外的新章节；
  //   ③ 但**有未保存正文时绝不自动切换**（切换会清空缓冲区，等于丢字），
  //      只保留上面的提示，由作者自己决定。
  useEffect(() => {
    const { nextChapterId } = resolveChapterReconcile({
      isProjectReady,
      chapterNodes,
      currentChapterId,
      hasUnsavedDraft: isDirtyRef.current,
    });
    if (nextChapterId) setCurrentChapter(nextChapterId);
  }, [chapterNodes, currentChapterId, isProjectReady, setCurrentChapter]);

  // 章节名草稿跟随「当前章节」重置：优先已保存的名字，其次才是大纲（AI 起的）标题。
  useEffect(() => {
    setChapterTitleDraft(persistedChapterMeta?.title || activeOutlineChapter?.title || '');
  }, [activeOutlineChapter?.title, currentChapterId, persistedChapterMeta?.title]);

  useEffect(() => () => {
    continueAbortRef.current?.abort();
    stopNovelJobSync();
    if (elapsedTimerRef.current !== null) {
      window.clearInterval(elapsedTimerRef.current);
    }
  }, [stopNovelJobSync]);

  /**
   * 切换项目时**立即**中止在途续写。
   *
   * 编辑器在切项目时不会重挂载（`ProjectWorkspace` 没有按 projectId 加 key），
   * 所以上面的卸载清理不会执行。而 `requestIdentity` 守卫只能防止迟到的分片写错地方，
   * 防不住旧项目的模型流继续跑到结束——那是在真实 provider 上继续计费。
   * 这里同时把请求记录置空，让迟到的回调一律判为「非当前请求」。
   */
  useEffect(() => () => {
    continueAbortRef.current?.abort();
    continueAbortRef.current = null;
    continueRequestRef.current = null;
    saveRequestRef.current = null;
    // 中止后 onDone 会因为请求记录已清空而跳过收尾，这里必须自己复位按钮状态。
    setIsAiWriting(false);
  }, [projectId]);

  useEffect(() => {
    if (saveStatus === 'idle') return undefined;
    const timeout = window.setTimeout(() => {
      setSaveStatus('idle');
      setSaveMessage('');
    }, 2500);
    return () => window.clearTimeout(timeout);
  }, [saveStatus]);

  useEffect(() => {
    if (!projectId || chapterNodes.length === 0) return;
    let active = true;
    aiApi.novelJob.getByProject(projectId)
      .then((json) => {
        if (!active) return;
        setStylePlanConflictJob(json.data?.checkpoint_type === 'style_plan_conflict' ? json.data : null);
        if (json.data) {
          applyNovelJobState(json.data, 'bootstrap');
          if (json.data.status === 'queued' || json.data.status === 'running') {
            startNovelJobStream(json.data.id);
          }
        }
      })
      .catch(() => {});
    // M2：拉取可恢复任务，展示"恢复"入口（后端重启中断 / 失败章待处理）
    aiApi.novelJob.getRecoverable(projectId)
      .then((json) => {
        if (!active) return;
        const candidates = (json.data || []).filter((item) => item.recoverable);
        setRecoverableJob(candidates[0] || null);
      })
      .catch(() => {});
    return () => { active = false; };
  }, [applyNovelJobState, chapterNodes.length, projectId, startNovelJobStream]);

  const demoContent = isEmpty ? '' : '# 第一章\n\n在这里开始你的章节创作。';
  const displayContent = currentChapterId ? chapterContent : (chapterContent || demoContent);
  const sessionTimestamp = autoWriteSession.updatedAt
    ? new Date(autoWriteSession.updatedAt).toLocaleString('zh-CN', { hour12: false })
    : '';

  return (
    <div className="flex h-full flex-col" data-testid="markdown-editor">
      <ChapterContextBar project={project} chapter={activeOutlineChapter} />
      {/* 章节名：大纲生成时由 AI 起名，作者可在这里直接改；保存时同步到章节行与大纲节点。 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-surface-200 bg-white px-4 py-1.5 dark:border-surface-700 dark:bg-surface-850">
        <label htmlFor="editor-chapter-title" className="shrink-0 text-[11px] text-surface-400">章节名</label>
        <input
          id="editor-chapter-title"
          className="min-w-0 flex-1 bg-transparent text-sm font-medium outline-none placeholder:text-surface-400 disabled:opacity-60"
          value={chapterTitleDraft}
          onChange={(event) => {
            setChapterTitleDraft(event.target.value);
            setIsDirty(true);
            isDirtyRef.current = true;
          }}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            void handleSave();
          }}
          placeholder="为这一章起个名字（保存后同步到左侧导航）"
          maxLength={60}
          disabled={!isProjectReady}
          data-testid="editor-chapter-title"
        />
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-surface-200 bg-white px-4 py-1.5 dark:border-surface-700 dark:bg-surface-850">
        <div className="flex shrink-0 items-center gap-1 whitespace-nowrap">
          <button onClick={() => setViewMode('edit')} className={`btn-ghost text-xs ${viewMode === 'edit' ? 'bg-surface-200 dark:bg-surface-700' : ''}`} data-testid="editor-view-edit">
            <Code size={14} className="mr-1 inline" />编辑
          </button>
          <button onClick={() => setViewMode('preview')} className={`btn-ghost text-xs ${viewMode === 'preview' ? 'bg-surface-200 dark:bg-surface-700' : ''}`} data-testid="editor-view-preview">
            <Eye size={14} className="mr-1 inline" />预览
          </button>
          <button onClick={() => setViewMode('split')} className={`btn-ghost text-xs ${viewMode === 'split' ? 'bg-surface-200 dark:bg-surface-700' : ''}`} data-testid="editor-view-split">
            分栏
          </button>
          <span className="mx-2 h-5 w-px bg-surface-300 dark:bg-surface-600" />
          <span className="text-xs text-surface-400">{wordCount} 字</span>
          {isDirty && <span className="ml-2 text-xs text-warning">未保存</span>}
          {saveStatus !== 'idle' && <span className={`ml-2 text-xs ${saveStatus === 'saved' ? 'text-success' : 'text-error'}`} data-testid="editor-save-message">{saveMessage}</span>}
        </div>

        <div className="contents whitespace-nowrap">
          {chapterNodes.length > 0 && (
            <div className="flex shrink-0 items-center gap-1 whitespace-nowrap rounded-lg bg-surface-100 px-2 py-1 dark:bg-surface-700">
              {!isAutoWriting ? (
                <>
                  <select className="rounded-lg bg-transparent px-1 text-xs outline-none" value={autoWriteMode} onChange={(event) => setAutoWriteMode(event.target.value as AutoWriteMode)} data-testid="editor-auto-write-mode">
            <option value="all">从头生成全书</option>
            <option value="current-to-end">从当前章写到结尾</option>
            <option value="first-unfinished">续写未完成章节</option>
            <option value="single">仅生成当前章</option>
                  </select>
                  <button onClick={() => void handleAutoWrite()} className="btn-success flex items-center gap-1 px-2.5 py-1 text-xs" data-testid="editor-auto-write">
                    <Play size={12} />按大纲自动创作
                  </button>
                </>
              ) : (
                <>
                  <span className="mr-1 max-w-56 truncate text-[11px] text-surface-500" data-testid="editor-auto-progress">{autoProgress}</span>
                  <button onClick={handlePauseAfterCurrent} className="btn-warning flex items-center gap-1 px-2 py-1 text-xs" data-testid="editor-auto-pause-after-current">
                    <SkipForward size={12} />当前章后暂停
                  </button>
                  <button onClick={handleStopAutoWrite} className="btn-error flex items-center gap-1 px-2 py-1 text-xs" data-testid="editor-auto-stop">
                    <Square size={12} />停止任务
                  </button>
                </>
              )}
            </div>
          )}

          {!isAutoWriting && (
            <div className="flex max-w-full flex-wrap items-center gap-1.5 whitespace-nowrap rounded-lg bg-surface-100 px-2 py-1 dark:bg-surface-700">
              <input className="w-32 bg-transparent text-xs outline-none placeholder:text-surface-400" data-testid="editor-ai-direction" placeholder="续写方向（可选）" value={aiDirection} onChange={(event) => setAiDirection(event.target.value)} />
              <span className="text-[11px] text-surface-400">字数</span>
              <select className="bg-transparent text-xs outline-none" data-testid="editor-ai-word-count" value={aiWords} onChange={(event) => setAiWords(Number(event.target.value))}>
                <option value={200}>200</option>
                <option value={500}>500</option>
                <option value={1000}>1000</option>
                <option value={2000}>2000</option>
              </select>
              <button onClick={() => void handleAiContinue()} disabled={isAiWriting} className="btn-primary flex items-center gap-1 px-2.5 py-1 text-xs" data-testid="editor-ai-continue">
                <Sparkles size={12} className={isAiWriting ? 'animate-spin' : ''} />{isAiWriting ? '续写中' : 'AI 续写'}
              </button>
              <button onClick={() => void handleDeai()} disabled={isDeai || isAiWriting} className="btn-ghost flex items-center gap-1 px-2.5 py-1 text-xs" data-testid="editor-deai" title="检测并去除 AI 味表达">
                <Wand2 size={12} className={isDeai ? 'animate-pulse' : ''} />{isDeai ? '处理中' : '去 AI 味'}
              </button>
            </div>
          )}

          <button onClick={() => void handleSave()} disabled={isSaving || isAiWriting} className="btn-ghost text-xs" data-testid="editor-save">
            {isSaving ? <Loader2 size={14} className="mr-1 inline animate-spin" /> : <Save size={14} className="mr-1 inline" />}
            {isSaving ? '保存中' : '保存'}
          </button>
        </div>
      </div>

      {/* M2：可恢复任务入口（最小可用） */}
      {recoverableJob && !isAutoWriting && (
        <div className="flex items-center justify-between gap-3 border-b border-warning/40 bg-warning/10 px-4 py-2" data-testid="editor-resume-banner">
          <div className="flex min-w-0 flex-col">
            <span className="text-xs font-medium text-warning">检测到可恢复的长篇创作任务</span>
            <span className="truncate text-[11px] text-surface-500 dark:text-surface-400">
              {recoverableJob.pending_manual_recovery
                ? `失败章待处理 · 已完成 ${recoverableJob.completed_chapters}/${recoverableJob.total_chapters} 章 · 恢复后将重试失败章，成功后继续后续章节（不跳过）。`
                : `已完成 ${recoverableJob.completed_chapters}/${recoverableJob.total_chapters} 章 · 将从断点继续生成，不重跑已生成章节。`}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {onNavigateToTasks && (
              <Button variant="ghost" size="sm" onClick={onNavigateToTasks} data-testid="editor-goto-tasks">
                <ClipboardList size={14} />
                前往任务中心
              </Button>
            )}
            <Button variant="warning" size="sm" loading={isResuming} onClick={() => void handleResumeNovelJob()} data-testid="editor-resume-button">
              <RotateCcw size={14} />
              恢复任务
            </Button>
          </div>
        </div>
      )}

      {/* 风格计划冲突通过 latest-job GET / SSE 呈现，不进入可恢复列表。 */}
      {stylePlanConflictJob && !isAutoWriting && (
        <div
          className="flex items-start justify-between gap-3 border-b border-warning/40 bg-warning/10 px-4 py-2"
          data-testid="editor-style-conflict-banner"
        >
          <div className="flex min-w-0 flex-col gap-1">
            <span className="text-xs font-medium text-warning">计划变化，未保存候选已保留</span>
            <span className="text-[11px] text-surface-500 dark:text-surface-400">
              风格过渡计划在自动创作期间被更新，候选正文已保留（未覆盖正文）。
              该任务不会自动恢复；请在正文中人工处理候选后，再发起新的创作任务。
            </span>
            {stylePlanConflictJob.preview_content && (
              <div className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap rounded bg-surface-100 p-2 text-[11px] text-surface-600 dark:bg-surface-800 dark:text-surface-300" data-testid="editor-style-conflict-preview">
                {stylePlanConflictJob.preview_content.slice(0, 400)}
              </div>
            )}
          </div>
          {onNavigateToTasks && (
            <Button variant="ghost" size="sm" onClick={onNavigateToTasks} data-testid="editor-goto-tasks">
              <ClipboardList size={14} />
              前往任务中心
            </Button>
          )}
        </div>
      )}

      {/* P0/P1：末章失败——默认可重试失败章本身（不再仅提示人工处理） */}
      {lastChapterFailure && !isAutoWriting && (
        <div className="flex items-center justify-between gap-3 border-b border-error/40 bg-error/10 px-4 py-2" data-testid="editor-last-chapter-failure-notice">
          <div className="flex min-w-0 items-center gap-2">
            <AlertTriangle size={14} className="shrink-0 text-error" />
            <span className="text-xs text-error">失败章为最后一章，可重试该失败章（默认不跳过）。</span>
          </div>
          <Button variant="warning" size="sm" loading={isResuming} onClick={() => void handleResumeNovelJob()} data-testid="editor-retry-last-chapter">
            <RotateCcw size={14} />
            重试失败章
          </Button>
        </div>
      )}

      <div className="border-b border-surface-200 bg-surface-50 px-4 py-1.5 text-[11px] text-surface-500 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-400" data-testid="editor-status-bar">
        {isAutoWriting
          ? (
            <div className="flex flex-col gap-1">
              <div className="flex items-center justify-between">
                <span className="truncate">{autoProgress || '正在整理本章上下文'}</span>
                <span className="ml-2 shrink-0 tabular-nums" data-testid="editor-elapsed-time">{Math.floor(elapsedSeconds / 3600) > 0
                        ? `${Math.floor(elapsedSeconds / 3600)}时${Math.floor((elapsedSeconds % 3600) / 60)}分`
                        : `${Math.floor(elapsedSeconds / 60)}分${elapsedSeconds % 60}秒`}
                </span>
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px]" data-testid="generation-context-steps">
                <span>✓ 当前章节目标</span><span>✓ 人物状态</span><span>✓ 最近剧情</span><span>✓ 活跃伏笔</span><span>✓ 当前文风</span>
              </div>
              <div className="flex items-center gap-2">
                    <span className="shrink-0 tabular-nums" data-testid="editor-chapter-count">
                      {autoProgressInfo.completed}/{autoProgressInfo.total} 章
                    </span>
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700">
                      <div
                        className="h-full rounded-full bg-success transition-all duration-700"
                        style={{ width: `${autoProgressInfo.percent}%` }}
                        data-testid="editor-progress-bar"
                      />
                    </div>
              </div>
            </div>
          )
          : autoStatusMessage
            ? autoStatusMessage
            : activeOutlineChapter
              ? `当前章节：${activeOutlineChapter.title}${currentChapterIndex >= 0 ? ` · 第${currentChapterIndex + 1}章` : ''}`
              : '请选择章节后开始创作。'}
      </div>

      {!isAutoWriting && (
        <div className="shrink-0 border-b border-surface-200 text-[11px] text-surface-500 dark:border-surface-700 dark:text-surface-400">
          <ProjectionStatusPanel
            projectId={projectId}
            chapterId={currentChapterId}
            expanded={detailPanel === 'memory'}
            onExpandedChange={(open) => setDetailPanel(open ? 'memory' : null)}
            leadingControl={(
              <button type="button" className="flex min-w-0 items-center gap-2 px-3 py-2 text-left text-xs hover:bg-surface-50 dark:hover:bg-surface-800" aria-expanded={detailPanel === 'generation'} aria-controls="generation-record-content" data-testid="editor-generation-record-toggle" onClick={() => setDetailPanel((current) => current === 'generation' ? null : 'generation')}>
                <span aria-hidden="true">{detailPanel === 'generation' ? '▾' : '▸'}</span>
                <span>生成记录</span>
                <span className="text-[11px] text-surface-400">{autoWriteSession.status === 'idle' ? '暂无记录' : autoWriteSession.status === 'completed' ? '已完成' : autoWriteSession.status === 'error' ? '失败' : autoWriteSession.status === 'stopped' ? '已停止' : autoWriteSession.status === 'running' ? '进行中' : '已暂停'}</span>
              </button>
            )}
            companionContent={detailPanel === 'generation' ? (
              <div id="generation-record-content" data-testid="editor-generation-record-content">
              {autoWriteSession.status === 'idle' ? <p>暂无生成记录。</p> : <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-col">
            <span data-testid="editor-session-status">生成会话：{autoWriteSession.status}{sessionTimestamp ? ` · ${sessionTimestamp}` : ''}</span>
            {autoSourceMessage && <span className="truncate" data-testid="editor-session-source">{autoSourceMessage}</span>}
            {autoWriteSession.currentChapterId && <span className="truncate">最近章节：{autoWriteSession.currentChapterId} · 已完成 {autoProgressInfo.completed} 章</span>}
          </div>
          <button onClick={handleClearAutoSession} className="btn-ghost text-xs" data-testid="editor-clear-auto-session">
            清除记录
          </button>
              </div>}
              </div>
            ) : null}
          />
        </div>
      )}

      {chapterLoadError && (
        <div
          className="mx-5 mt-2 flex items-start gap-2 rounded-lg border border-error/40 bg-error-soft px-3 py-2 text-xs text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark sm:mx-8"
          role="alert"
          data-testid="chapter-load-error"
        >
          {chapterLoadError}
        </div>
      )}

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {(viewMode === 'edit' || viewMode === 'split') && (
          <div className={`${viewMode === 'split' ? 'w-1/2' : 'w-full'} overflow-auto`}>
            <textarea
              className="h-full w-full resize-none bg-transparent px-5 pb-8 pt-3 font-serif text-base leading-loose text-surface-800 outline-none dark:text-surface-200 sm:px-8"
              value={displayContent}
              onChange={(event) => {
                updateChapterContent(event.target.value);
                storeSetChapterContent(event.target.value);
                setIsDirty(true);
                isDirtyRef.current = true;
              }}
              onSelect={(event) => {
                const { selectionStart, selectionEnd, value } = event.currentTarget;
                setSelectionText(value.slice(selectionStart, selectionEnd));
              }}
              data-testid="editor-textarea"
              placeholder={
                chapterLoadError
                  ? '这一章暂时读不到正文（见上方提示）。'
                  : activeOutlineChapter && !displayContent.trim()
                    ? '本章尚未创作，可点击“AI 续写”或“按大纲自动创作”开始。'
                    : '在此开始创作...'
              }
              style={{ fontFamily: serifFontFamily, lineHeight: 2 }}
            />
          </div>
        )}

        {(viewMode === 'preview' || viewMode === 'split') && (
          <div className={`${viewMode === 'split' ? 'w-1/2 border-l border-surface-200 dark:border-surface-700' : 'w-full'} overflow-auto px-5 pb-8 pt-3 sm:px-8`}>
            <div className="prose max-w-none font-serif text-base leading-loose dark:prose-invert" style={{ fontFamily: serifFontFamily, lineHeight: 2 }} dangerouslySetInnerHTML={{ __html: renderMarkdown(displayContent) }} />
          </div>
        )}
      </div>
    </div>
  );
}
