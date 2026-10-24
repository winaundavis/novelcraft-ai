import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  BookOpen,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  FileSearch,
  Info,
  Loader2,
  ShieldCheck,
  Sparkles,
  Undo2,
  Wand2,
} from 'lucide-react';

import { ApiError, chapterApi, reviewApi, aiApi, isStylePlanConflict } from '../../services/api';
import { fetchCurrentStylePlanToken, stylePlanTokenFields } from '../../services/stylePlanToken';
import { buildCharacterContexts } from '../../services/characterContext';
import {
  reviewContentSignature,
  reviewSnapshotKey,
  useEditorStore,
  useProjectStore,
  useReviewStore,
} from '../../stores';
import { chapterMutationBlockReason, notifyChapterChanged, notifyStyleTransitionChanged } from '../editor/chapterEvents';
import { isSkippableApplyFailure } from './reviewApplyFailure';

interface ReviewIssue {
  severity: string;
  type?: string;
  location?: string;
  original?: string;
  suggestion?: string;
  reason?: string;
  category?: string;
  issue?: string;
  description?: string;
}

/**
 * 服务端明确「正文未动」的失败码：模型未产生实际修改 / 返回空 / 调用失败 / 创建候选超时。
 *
 * 这类失败**没有写正文**，所以批量修改应当跳过该条继续下一条，
 * 而不是整批停下——否则用户改到一半就得重新审校重来（实际使用反馈）。
 * 其余失败（正文已变、计划冲突、未保存）必须停，避免基于错位文本继续改。
 * 判定逻辑见 `./reviewApplyFailure`（零依赖，单测直接覆盖）。
 */


interface ReviewResult {
  totalIssues: number;
  fatalCount: number;
  importantCount: number;
  suggestionCount: number;
  issues: ReviewIssue[];
  summary: string;
  languageScore: number;
  logicScore: number;
  rhythmScore: number;
}

function normalizeSeverity(severity?: string) {
  const value = (severity || '').toLowerCase();
  if (value.includes('fatal') || value.includes('致命') || value.includes('严重')) return 'fatal';
  if (value.includes('important') || value.includes('high') || value.includes('重要')) return 'important';
  return 'suggestion';
}

function severityLabel(severity?: string) {
  const normalized = normalizeSeverity(severity);
  if (normalized === 'fatal') return '致命';
  if (normalized === 'important') return '重要';
  return '建议';
}

export default function ReviewPanel({ projectId }: { projectId: string }) {
  const project = useProjectStore((state) => state.projects.find((item) => item.id === projectId));
  const { chapterContent, currentChapterId } = useEditorStore();
  const [isReviewing, setIsReviewing] = useState(false);
  const [degradedReason, setDegradedReason] = useState('');
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [error, setError] = useState('');
  const [activeFilter, setActiveFilter] = useState<'all' | 'fatal' | 'important' | 'suggestion'>('all');
  const [expandedIssues, setExpandedIssues] = useState<Set<number>>(new Set());
  const [applyingKey, setApplyingKey] = useState('');
  // 勾选「修改」的建议（按 issueKey，与严重度筛选无关，切筛选不会丢选择）
  const [selectedIssueKeys, setSelectedIssueKeys] = useState<Set<string>>(new Set());
  const [batchTotal, setBatchTotal] = useState(0);
  const [batchDone, setBatchDone] = useState(0);
  const [applyMessage, setApplyMessage] = useState('');
  const [isUndoing, setIsUndoing] = useState(false);

  const setChapterContent = useEditorStore((state) => state.setChapterContent);
  const snapshots = useReviewStore((state) => state.snapshots);
  const setSnapshot = useReviewStore((state) => state.setSnapshot);
  const markIssueApplied = useReviewStore((state) => state.markIssueApplied);
  const setLastApplied = useReviewStore((state) => state.setLastApplied);

  const snapshotKey = reviewSnapshotKey(projectId, currentChapterId || '');
  const snapshot = snapshots[snapshotKey];
  const appliedIssueKeys = snapshot?.appliedIssueKeys || [];
  // 撤销信息存在快照里：切换页签返回后仍可撤销上一次一键修改
  const undoState = snapshot?.lastApplied || null;
  const currentSignature = reviewContentSignature(chapterContent);
  // 审校后才改过正文：快照仍在，但提示结果可能已过期。
  // 注意：**不因此锁死一键修改**。批量修改本来就是“改一条 → 正文变了”的过程，
  // 若一条成功就把整张表锁住，用户改到一半就只能重新审校（实际使用反馈）。
  // 正确性由逐条保护兑现：原文不唯一时退回整章指令改写，服务端接受时再校验一次原文。
  const stale = !!snapshot && !!result && snapshot.contentSignature !== currentSignature;

  // 切回审校页、或整页刷新后恢复上次结果。
  //
  // 依赖里带上 `reviewedAt` / `contentSignature`（而不是只依赖 `snapshotKey`）是为了兼容
  // 异步存储（IndexedDB）晚一步才恢复：那时这两个字段才会变，于是恢复一次。
  // 「已应用标记 / 撤销信息」的变化不改这两个字段，不会把作者正在看的展开状态重置掉。
  useEffect(() => {
    if (!snapshot) {
      setResult(null);
      setDegradedReason('');
      setApplyMessage('');
      setError('');
      setExpandedIssues(new Set());
      setSelectedIssueKeys(new Set());
      return;
    }
    setResult(snapshot.result as unknown as ReviewResult);
    setDegradedReason(snapshot.degradedReason || '');
    setApplyMessage(snapshot.lastApplied ? '已按审校建议修改正文。' : '');
    setError('');
    setExpandedIssues(new Set());
    setSelectedIssueKeys(new Set());
  }, [snapshotKey, snapshot?.reviewedAt, snapshot?.contentSignature]);

  // 内容派生的稳定 key：切换严重度筛选后仍能正确判断"该条是否已应用"
  const issueKeyOf = (issue: ReviewIssue) => [
    issue.type || issue.category || '',
    (issue.original || issue.issue || '').slice(0, 40),
    (issue.suggestion || issue.description || '').slice(0, 40),
  ].join('|');

  const handleReview = async () => {
    if (!chapterContent.trim()) return;

    setIsReviewing(true);
    setError('');
    setDegradedReason('');
    setApplyMessage('');

    try {
      const json = await reviewApi.comprehensive({
        projectId: project?.id || '',
        text: chapterContent,
        chapterId: currentChapterId || '',
        characters: buildCharacterContexts(project?.characters),
        worldRules: (project?.worldRules || []).map((rule) => ({
          category: rule.category,
          name: rule.name,
          description: rule.description,
        })),
      });
      const nextResult = json.data as unknown as ReviewResult;
      const degraded = (json.data as { degradedReason?: string } | undefined)?.degradedReason;
      setResult(nextResult);
      if (degraded) setDegradedReason(degraded);
      // 保存快照：切换页签/查看正文后返回仍能看到本次审校
      setSnapshot(snapshotKey, {
        result: json.data as unknown as Record<string, unknown>,
        degradedReason: degraded || '',
        contentSignature: currentSignature,
        reviewedAt: Date.now(),
        appliedIssueKeys: [],
        lastApplied: null,
      });
    } catch (reviewError) {
      setResult(null);
      setError(reviewError instanceof Error ? reviewError.message : '审校失败。');
    } finally {
      setIsReviewing(false);
    }
  };

  /**
   * 按**一条**审校建议改写正文并落盘，返回新的正文；失败则抛错。
   *
   * 为什么不能直接做字符串替换：审校的 `suggestion` 是**修改指令**
   * （如"用动作描写替换重复的对话标签"），不是替换文本；直接替换会把指令写进正文。
   * 因此走既有的候选通道（带 owner / 版本 / 原文一致保护）生成改写，再立即接受。
   * 能定位到原文时只改该片段，否则按整章指令改写。
   *
   * 批量执行时逐条调用本函数：每条都重新从服务端读一次**当前**正文，因此上一条改完
   * 之后的文字变动会被自然纳入（原文不再唯一时自动退回整章指令改写）。
   */
  const applyIssueCore = async (issue: ReviewIssue): Promise<{ content: string; before: string }> => {
    if (!projectId || !currentChapterId) throw new Error('请先选择要修改的章节。');

    const blockedReason = chapterMutationBlockReason(projectId);
    if (blockedReason) throw new Error(blockedReason);

    const saved = await chapterApi.get(projectId, currentChapterId);
    const savedContent = saved.data?.content || '';
    if (!savedContent.trim()) throw new Error('当前章节还没有已保存的正文。');

    const editor = useEditorStore.getState();
    const editingThisChapter = editor.activeProjectId === projectId && editor.currentChapterId === currentChapterId;
    if (editingThisChapter && editor.isDirty && editor.chapterContent !== savedContent) {
      throw new Error('正文有未保存的修改，请先保存后再应用审校建议。');
    }

    const snippet = (issue.original || issue.issue || '').trim();
    // 只有原文唯一出现时才做定向改写，避免改错位置
    const selectionText = snippet.length >= 4 && savedContent.split(snippet).length === 2 ? snippet : '';
    const instruction = [
      '按审校建议修改正文，只做必要改动，保留事实、人物与剧情走向不变。',
      issue.type || issue.category ? `问题类型：${issue.type || issue.category}` : '',
      issue.location ? `位置：${issue.location}` : '',
      `审校建议：${issue.suggestion || issue.description || ''}`,
      issue.reason ? `原因：${issue.reason}` : '',
    ].filter(Boolean).join('\n');

    // 创建候选必须带当前最新计划版本，否则启用 v2 计划后会被 409 拦下。
    const planToken = await fetchCurrentStylePlanToken(projectId);
    let created: Awaited<ReturnType<typeof aiApi.copilot.createCandidate>>;
    try {
      created = await aiApi.copilot.createCandidate({
        projectId,
        chapterId: currentChapterId,
        action: 'rewrite',
        currentText: savedContent,
        selectionText,
        instruction,
        ...stylePlanTokenFields(planToken),
      });
    } catch (createError) {
      // 超时发生在 create 阶段 = 还未 accept = 正文未改动，可以跳过继续下一条。
      if (createError instanceof ApiError && createError.isTimeout) {
        const timeoutError = new Error('本条生成超时，正文未改动，已跳过。') as Error & { code?: string };
        timeoutError.code = 'copilot_create_timeout';
        throw timeoutError;
      }
      throw createError;
    }
    const candidateId = created.data?.id;
    if (!candidateId) throw new Error('未能创建修改候选，正文未改动。');

    const before = editor.chapterContent;
    const accepted = await aiApi.copilot.accept(candidateId);
    const next = accepted.data?.applied_content
      ?? (await chapterApi.get(projectId, currentChapterId)).data?.content;
    if (next === undefined) throw new Error('修改已保存，但正文读取失败，请重新打开该章节。');

    const latest = useEditorStore.getState();
    if (latest.activeProjectId === projectId && latest.currentChapterId === currentChapterId) {
      if (chapterMutationBlockReason(projectId) || latest.chapterContent !== before) {
        throw new Error('建议已写入正文；编辑器保留了你刚输入的新修改，请核对后再保存。');
      }
      setChapterContent(next);
      latest.setSelectionText('');
      latest.markClean();
    }
    notifyChapterChanged({ projectId, chapterId: currentChapterId, action: 'copilot-accept', appliedContent: next });
    // 接受会推进完成数：直接用响应里的最新进度刷新，不自行推算。
    if (accepted.data?.styleTransition) {
      notifyStyleTransitionChanged({ projectId, transition: accepted.data.styleTransition });
    }

    return { content: next, before: savedContent };
  };

  /**
   * 一键修改：把**勾选**的建议按列表顺序逐条应用。
   *
   * 语义（刻意选保守的一条）：
   * - 逐条顺序执行，每条都基于上一条改完后的正文重新定位原文；
   * - 中途一条失败就**停下来**，已成功的保留并如实报告，不静默跳过后面的；
   * - 撤销以「整批」为单位：记的是本批开始前的正文，所以一次撤销能回退这一整批。
   */
  const handleApplySelected = async () => {
    if (!projectId || !currentChapterId) return;
    const targets = filtered.filter((issue) => {
      const key = issueKeyOf(issue);
      return selectedIssueKeys.has(key) && !appliedIssueKeys.includes(key);
    });
    if (!targets.length) {
      setError('请先点每条建议下面的「修改」勾选要改的条目。');
      return;
    }

    setError('');
    setApplyMessage('');
    setBatchTotal(targets.length);
    setBatchDone(0);

    const appliedKeys: string[] = [];
    const skippedKeys: string[] = [];
    let consecutiveSkipped = 0;
    let batchStartContent = '';
    let failure = '';

    for (const issue of targets) {
      const key = issueKeyOf(issue);
      setApplyingKey(key);
      try {
        const { content, before } = await applyIssueCore(issue);
        if (!appliedKeys.length) batchStartContent = before;
        appliedKeys.push(key);
        consecutiveSkipped = 0;
        markIssueApplied(snapshotKey, key);
        setBatchDone(appliedKeys.length);
        void content;
      } catch (applyError) {
        // 这一类失败服务端明确「正文未动」：模型没产生实际修改 / 返回空 / 调用失败。
        // 跳过它继续下一条，否则一次批量改到一半就整批停下，用户只能重新审校重来。
        if (isSkippableApplyFailure(applyError)) {
          skippedKeys.push(key);
          consecutiveSkipped += 1;
          if (consecutiveSkipped >= 2) {
            failure = '模型连续两次没能给出有效修改，已停止后续条目（可稍后重试）。';
            break;
          }
          continue;
        }
        failure = applyError instanceof Error ? applyError.message : '修改失败。';
        if (isStylePlanConflict(applyError)) {
          failure = `${failure}\n风格过渡计划或进度已更新，本条被拒绝；正文未改动，也未插入任何占位文本。请重新审校后再试。`;
        }
        break;
      }
    }
    setApplyingKey('');

    if (appliedKeys.length) {
      // 撤销覆盖整批：记本批开始前的正文
      setLastApplied(snapshotKey, { issueKey: appliedKeys[0], previousContent: batchStartContent });
      setSelectedIssueKeys((current) => {
        const next = new Set(current);
        appliedKeys.forEach((key) => next.delete(key));
        return next;
      });
    }
    setApplyMessage(
      appliedKeys.length
        ? (appliedKeys.length === 1
          ? '已按审校建议修改正文。'
          : `已按 ${appliedKeys.length} 条审校建议依次修改正文。`)
          + (skippedKeys.length ? ` 跳过 ${skippedKeys.length} 条（模型未产生实际修改，正文未动）。` : '')
        : (skippedKeys.length ? `已跳过 ${skippedKeys.length} 条：模型未产生实际修改，正文未动。` : ''),
    );
    if (failure) {
      setError(
        appliedKeys.length
          ? `已成功修改 ${appliedKeys.length} 条，第 ${appliedKeys.length + 1} 条失败：${failure}`
          : failure,
      );
    }
    setBatchTotal(0);
    setBatchDone(0);
  };

  /** 撤销上一次一键修改（直接写回修改前的正文）。 */
  const handleUndoApply = async () => {
    if (!undoState || !projectId || !currentChapterId || isUndoing) return;
    setIsUndoing(true);
    setError('');
    try {
      const blockedReason = chapterMutationBlockReason(projectId);
      if (blockedReason) throw new Error(blockedReason);
      await chapterApi.save(projectId, currentChapterId, { content: undoState.previousContent });
      const latest = useEditorStore.getState();
      if (latest.activeProjectId === projectId && latest.currentChapterId === currentChapterId) {
        setChapterContent(undoState.previousContent);
        latest.markClean();
      }
      notifyChapterChanged({
        projectId, chapterId: currentChapterId, action: 'copilot-accept', appliedContent: undoState.previousContent,
      });
      setLastApplied(snapshotKey, null);
      setApplyMessage('已撤销上一次一键修改。');
    } catch (undoError) {
      setError(undoError instanceof Error ? undoError.message : '撤销失败。');
    } finally {
      setIsUndoing(false);
    }
  };

  const filtered =
    result?.issues.filter((issue) => activeFilter === 'all' || normalizeSeverity(issue.severity) === activeFilter) || [];

  const severityCounts = useMemo(
    () => (result?.issues || []).reduce(
      (counts, issue) => {
        const normalized = normalizeSeverity(issue.severity);
        counts.total += 1;
        counts[normalized] += 1;
        return counts;
      },
      { total: 0, fatal: 0, important: 0, suggestion: 0 },
    ),
    [result],
  );

  // 还没被应用的建议才能勾选；已应用的不再出现在「可改」里
  const selectableIssues = filtered.filter((issue) => !appliedIssueKeys.includes(issueKeyOf(issue)));
  const selectedCount = selectableIssues.filter((issue) => selectedIssueKeys.has(issueKeyOf(issue))).length;
  const selectionLocked = isReviewing || isUndoing || !!applyingKey || !currentChapterId || !projectId;

  const toggleIssueSelected = (key: string) => {
    setSelectedIssueKeys((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleSelectAll = () => {
    setSelectedIssueKeys((previous) => {
      const allSelected = selectableIssues.length > 0
        && selectableIssues.every((issue) => previous.has(issueKeyOf(issue)));
      return allSelected ? new Set() : new Set(selectableIssues.map((issue) => issueKeyOf(issue)));
    });
  };

  const toggleIssue = (index: number) => {    setExpandedIssues((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const severityIcon = (severity: string) => {
    const normalized = normalizeSeverity(severity);
    if (normalized === 'fatal') return <AlertTriangle size={14} className="text-error" />;
    if (normalized === 'important') return <AlertTriangle size={14} className="text-warning" />;
    return <Info size={14} className="text-info" />;
  };

  const severityColor = (severity: string) => {
    const normalized = normalizeSeverity(severity);
    if (normalized === 'fatal') return 'bg-error-soft border-error dark:bg-error-soft-dark dark:border-error/40';
    if (normalized === 'important') return 'bg-warning-soft border-warning dark:bg-warning-soft-dark dark:border-warning/40';
    return 'bg-info-soft border-info dark:bg-info-soft-dark dark:border-info/40';
  };

  return (
    <div className="flex-1 overflow-auto">
      <div className="mx-auto max-w-4xl p-8">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-bold">
              <ShieldCheck size={22} className="text-primary-600" />
              智能审校
            </h2>
            <p className="mt-1 text-sm text-surface-500">
              {result ? result.summary : '检查当前章节的语言质量、逻辑连贯性和节奏表现。'}
            </p>
          </div>
          <button
            onClick={() => void handleReview()}
            disabled={isReviewing || !chapterContent.trim() || !projectId}
            className="btn-primary flex items-center gap-2"
            data-testid="review-start-button"
          >
            {isReviewing ? <Loader2 size={18} className="animate-spin" /> : <FileSearch size={18} />}
            {isReviewing ? '审校中...' : result ? '重新审校' : '开始审校'}
          </button>
        </div>

        {!chapterContent.trim() && (
          <div className="card mb-6 border-dashed border-surface-300 py-10 text-center text-surface-500 dark:border-surface-700">
            当前章节还没有正文。先回到写作面板输入或生成内容，再来做审校。
          </div>
        )}

        {stale && (
          <div
            className="card mb-6 border-warning bg-warning-soft text-xs text-warning dark:border-warning/40 dark:bg-warning-soft-dark dark:text-warning-dark"
            data-testid="review-stale-notice"
          >
            正文在上次审校之后有改动，下面的结果可能已过期；仍可继续应用剩余条目，但改完后建议点「重新审校」再验收。
          </div>
        )}

        {applyMessage && (
          <div
            className="card mb-6 flex flex-wrap items-center gap-3 border-success bg-success-soft text-xs text-success dark:border-success/40 dark:bg-success-soft-dark dark:text-success-dark"
            data-testid="review-apply-message"
          >
            <span>{applyMessage}</span>
            {undoState && (
              <button
                className="btn-ghost px-2 py-0.5 text-[11px] disabled:opacity-50"
                disabled={isUndoing}
                onClick={() => void handleUndoApply()}
                data-testid="review-undo-apply"
              >
                <Undo2 size={12} className="mr-1 inline" />
                {isUndoing ? '撤销中…' : '撤销本次修改'}
              </button>
            )}
          </div>
        )}

        {error && <div className="card mb-6 border-error bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark" data-testid="review-error">{error}</div>}

        {isReviewing && (
          <div
            className="card mb-6 border-dashed border-surface-300 text-xs text-surface-500 dark:border-surface-700"
            data-testid="review-progress-hint"
          >
            综合审校需要两次串行模型调用（语言审校 + 逻辑审校），真实模型下通常 30–90 秒，请勿关闭页面。
          </div>
        )}

        {degradedReason && (
          <div
            role="alert"
            className="card mb-6 border-warning bg-warning-soft text-warning dark:border-warning/40 dark:bg-warning-soft-dark dark:text-warning-dark"
            data-testid="review-degraded-reason"
          >
            <p className="font-semibold">本次未使用 AI 审校，结果已回退为本地基础检查</p>
            <p className="mt-1 text-xs leading-relaxed">{degradedReason}</p>
            <p className="mt-1 text-xs opacity-80">下面的分数与问题不含模型分析，请按上述原因处理后重试。</p>
          </div>
        )}

        {result && (
          <>
            <div className="mb-6 grid grid-cols-3 gap-4">
              {[
                { label: '语言', score: result.languageScore, icon: BookOpen },
                { label: '逻辑', score: result.logicScore, icon: ShieldCheck },
                { label: '节奏', score: result.rhythmScore, icon: Sparkles },
              ].map((card) => (
                <div key={card.label} className="card text-center">
                  <card.icon size={20} className="mx-auto mb-2 text-primary-500" />
                  <div className={`text-2xl font-bold ${card.score >= 90 ? 'text-success' : card.score >= 80 ? 'text-warning' : 'text-error'}`}>
                    {card.score}
                  </div>
                  <div className="mt-1 text-xs text-surface-400">{card.label}</div>
                </div>
              ))}
            </div>

            <div className="mb-4 flex gap-2">
              {[
                { key: 'all', label: `全部 (${severityCounts.total})` },
                { key: 'fatal', label: `致命 (${severityCounts.fatal})`, color: 'text-error' },
                { key: 'important', label: `重要 (${severityCounts.important})`, color: 'text-warning' },
                { key: 'suggestion', label: `建议 (${severityCounts.suggestion})`, color: 'text-info' },
              ].map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setActiveFilter(tab.key as typeof activeFilter)}
                  className={`rounded-full px-3 py-1.5 text-xs transition-colors ${
                    activeFilter === tab.key
                      ? 'bg-primary-600 text-white'
                      : 'bg-surface-100 text-surface-500 hover:bg-surface-200 dark:bg-surface-700'
                  } ${tab.color && activeFilter === tab.key ? '' : tab.color || ''}`}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {filtered.length > 0 ? (
              <>
                {/* 批量修改：先勾选每条建议下面的「修改」，再在这里一次性应用 */}
                <div
                  className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 dark:border-surface-700 dark:bg-surface-800"
                  data-testid="review-batch-toolbar"
                >
                  <button
                    type="button"
                    className="btn-secondary px-2 py-1 text-xs"
                    disabled={selectionLocked || selectableIssues.length === 0}
                    onClick={toggleSelectAll}
                    data-testid="review-select-all"
                  >
                    {selectedCount > 0 && selectedCount === selectableIssues.length ? '取消全选' : '全选'}
                  </button>
                  <button
                    type="button"
                    className="btn-primary flex items-center gap-1 px-3 py-1 text-xs disabled:opacity-50"
                    disabled={selectionLocked || selectedCount === 0}
                    title={stale ? '正文已改动，结果可能已过期；仍可继续应用剩余条目（建议重新审校后再验收）' : '按勾选的建议逐条改写并保存正文'}
                    onClick={() => void handleApplySelected()}
                    data-testid="review-apply-selected"
                  >
                    {applyingKey ? <Loader2 size={12} className="animate-spin" /> : <Wand2 size={12} />}
                    {applyingKey
                      ? `修改中…（${Math.min(batchDone + 1, batchTotal)}/${batchTotal}）`
                      : `一键修改（${selectedCount}）`}
                  </button>
                  <span className="text-xs text-surface-400" data-testid="review-selection-hint">
                    已选 {selectedCount} / 可改 {selectableIssues.length}
                    {selectedCount > 1 ? '（按列表顺序逐条执行）' : ''}
                  </span>
                </div>
                <div className="space-y-2">
                {filtered.map((issue, index) => {
                  const issueKey = issueKeyOf(issue);
                  const applied = appliedIssueKeys.includes(issueKey);
                  const selected = selectedIssueKeys.has(issueKey);
                  const selectDisabled = selectionLocked || applied;
                  return (
                  <div key={`${issue.type || issue.category || 'issue'}-${index}`} className={`card border ${severityColor(issue.severity)}`} data-testid={`review-issue-${index}`}>
                    <button onClick={() => toggleIssue(index)} className="flex w-full items-start gap-3 text-left">
                      {severityIcon(issue.severity)}
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="rounded bg-white/50 px-1.5 py-0.5 text-xs font-medium dark:bg-black/20">
                            {severityLabel(issue.severity)}
                          </span>
                          <span className="text-xs text-surface-400">{issue.type || issue.category || '问题'}</span>
                          {issue.location && <span className="text-xs text-surface-400">/ {issue.location}</span>}
                        </div>

                        {expandedIssues.has(index) && (
                          <div className="mt-2 space-y-2 text-sm">
                            {(issue.original || issue.issue) && (
                              <div>
                                <span className="text-xs text-surface-400">发现：</span>
                                <span className="text-surface-600 dark:text-surface-400">{issue.original || issue.issue}</span>
                              </div>
                            )}

                            {(issue.suggestion || issue.description) && (
                              <div>
                                <span className="text-xs text-surface-400">建议：</span>
                                <span className="text-success dark:text-success-dark">{issue.suggestion || issue.description}</span>
                              </div>
                            )}

                            {issue.reason && <div className="text-xs text-surface-400">{issue.reason}</div>}
                          </div>
                        )}
                      </div>
                      {expandedIssues.has(index)
                        ? <ChevronDown size={16} className="shrink-0 text-surface-400" />
                        : <ChevronRight size={16} className="shrink-0 text-surface-400" />}
                    </button>
                    <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-surface-200 pt-2 dark:border-surface-600">
                      <button
                        type="button"
                        className={
                          selected
                            ? 'btn-primary flex items-center gap-1 px-2 py-1 text-xs disabled:opacity-50'
                            : 'btn-secondary flex items-center gap-1 px-2 py-1 text-xs disabled:opacity-50'
                        }
                        disabled={selectDisabled}
                        aria-pressed={selected}
                        title={
                          applied
                            ? '这条已经改过了'
                            : selected
                              ? '取消选择这条建议'
                              : '选中这条建议，稍后点上方「一键修改」一起改'
                        }
                        onClick={() => toggleIssueSelected(issueKey)}
                        data-testid={`review-issue-select-${index}`}
                      >
                        {applyingKey === issueKey
                          ? <Loader2 size={12} className="animate-spin" />
                          : selected
                            ? <CheckCircle size={12} />
                            : <Wand2 size={12} />}
                        {applyingKey === issueKey ? '修改中…' : selected ? '已选' : '修改'}
                      </button>
                      {applied && (
                        <span className="flex items-center gap-1 text-xs text-success dark:text-success-dark" data-testid="review-issue-applied">
                          <CheckCircle size={12} /> 已应用
                        </span>
                      )}
                      {!currentChapterId && <span className="text-xs text-surface-400">请先选择章节</span>}
                    </div>
                  </div>
                  );
                })}
                </div>
              </>
            ) : (
              <div className="card py-12 text-center">
                <CheckCircle size={40} className="mx-auto mb-3 text-success" />
                <p className="font-medium text-success dark:text-success-dark">本轮未发现明显问题</p>
                <p className="mt-1 text-sm text-surface-400">当前章节在这次审校中表现稳定。</p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
