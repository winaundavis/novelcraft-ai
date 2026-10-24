/**
 * TaskCenter — 任务中心（ui-task-center.md §2 / §3.7 / §3.8）
 *
 * 三视图：待处理 | 历史 | 写作冲突（Segmented 胶囊切换）。
 * 数据源：getByProject + getRecoverable + getHistory + conflicts.list 并行拉取；
 * 运行中任务经 SSE（subscribe）实时更新，终止自动断开并后台刷新。
 * 只复用 common 组件（Button/Modal/Badge/Toast），不引入任何新依赖。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  ClipboardList,
  Inbox,
  Loader2,
  Play,
  RefreshCw,
} from 'lucide-react';

import { Badge, Button, Modal, useToast } from '../common';
import ConflictList from './ConflictList';
import TaskCard from './TaskCard';
import { isTerminalStatus, parseResumeTarget } from './constants';
import { useReducedMotion } from './useReducedMotion';
import { aiApi } from '../../services/api';
import type { NovelJobMode, NovelJobPayload, Outline, OutlineNode, WritingConflict } from '../../types';

type TaskView = 'active' | 'history' | 'conflicts';

function collectChapterNodes(outlineData: Outline | null): OutlineNode[] {
  if (!outlineData) return [];
  return outlineData.nodes
    .filter((node) => node.type === 'chapter')
    .sort((a, b) => a.order - b.order);
}

export default function TaskCenter({
  projectId,
  outlineData = null,
  onGoToChapter,
  onGoToWrite,
}: {
  projectId: string;
  outlineData?: Outline | null;
  onGoToChapter?: (chapterId: string) => void;
  onGoToWrite?: () => void;
}) {
  const { toast } = useToast();
  const reducedMotion = useReducedMotion();

  const [view, setView] = useState<TaskView>('active');
  const [activeJobs, setActiveJobs] = useState<NovelJobPayload[]>([]);
  const [recoverableJobs, setRecoverableJobs] = useState<NovelJobPayload[]>([]);
  const [historyJobs, setHistoryJobs] = useState<NovelJobPayload[]>([]);
  const [conflicts, setConflicts] = useState<WritingConflict[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [resumingJobId, setResumingJobId] = useState<string | null>(null);
  const [stopTarget, setStopTarget] = useState<NovelJobPayload | null>(null);
  const [restartTarget, setRestartTarget] = useState<NovelJobPayload | null>(null);
  const [newJobOpen, setNewJobOpen] = useState(false);
  const [startingJob, setStartingJob] = useState(false);
  // 运行中任务每秒刷新已耗时（仅触发渲染，不存储值）
  const [, setNowTick] = useState(0);

  const chapterNodes = useMemo(() => collectChapterNodes(outlineData), [outlineData]);
  const hasRunningJob = activeJobs.some((job) => job.status === 'queued' || job.status === 'running');

  // 运行中任务计时器
  useEffect(() => {
    if (!hasRunningJob) return;
    const timer = window.setInterval(() => setNowTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunningJob]);

  /** 拉取全部任务数据；background=true 时不显示加载骨架（SSE 终止后的后台刷新） */
  const loadAll = useCallback(async (background = false) => {
    if (!projectId) return;
    if (!background) {
      setLoading(true);
      setError('');
    }
    try {
      const [activeJson, recoverableJson, historyJson, conflictJson] = await Promise.all([
        aiApi.novelJob.getByProject(projectId),
        aiApi.novelJob.getRecoverable(projectId),
        aiApi.novelJob.getHistory(projectId),
        aiApi.conflicts.list(projectId),
      ]);

      const active = activeJson.data
        && (activeJson.data.status === 'queued' || activeJson.data.status === 'running')
        ? [activeJson.data]
        : [];
      setActiveJobs(active);
      setRecoverableJobs((recoverableJson.data || []).filter((job) => job.recoverable));
      setHistoryJobs(historyJson.data || []);
      setConflicts(conflictJson.data || []);
    } catch (err) {
      if (!background) {
        setError(err instanceof Error ? err.message : '任务数据加载失败。');
      }
    } finally {
      if (!background) setLoading(false);
    }
  }, [projectId]);

  // 首次加载
  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  // SSE 实时订阅运行中任务（job id 集合变化才重建订阅）
  const activeJobIdsKey = useMemo(
    () => activeJobs
      .filter((job) => job.status === 'queued' || job.status === 'running')
      .map((job) => job.id)
      .join(','),
    [activeJobs],
  );

  useEffect(() => {
    if (!projectId || !activeJobIdsKey) return;
    const unsubscribers: Array<() => void> = [];

    for (const job of activeJobs) {
      if (job.status !== 'queued' && job.status !== 'running') continue;

      const unsubscribe = aiApi.novelJob.subscribe(job.id, {
        onMessage: (updated) => {
          setActiveJobs((previous) => {
            if (!previous.some((item) => item.id === updated.id)) return previous;
            if (isTerminalStatus(updated.status)) {
              return previous.filter((item) => item.id !== updated.id);
            }
            return previous.map((item) => (item.id === updated.id ? updated : item));
          });
          if (isTerminalStatus(updated.status)) {
            void loadAll(true);
          }
        },
        onError: () => {
          // SSE 断开：静默回退轮询 GET /{job_id}（§3.8）
          void aiApi.novelJob
            .get(job.id)
            .then((json) => {
              if (!json.data) return;
              setActiveJobs((previous) => {
                if (!previous.some((item) => item.id === json.data.id)) return previous;
                if (isTerminalStatus(json.data.status)) {
                  return previous.filter((item) => item.id !== json.data.id);
                }
                return previous.map((item) => (item.id === json.data.id ? json.data : item));
              });
            })
            .catch(() => { /* 保留最后一次已知状态，连续失败由 loadAll 重试兜底 */ });
        },
      });
      unsubscribers.push(unsubscribe);
    }

    return () => {
      unsubscribers.forEach((unsubscribe) => unsubscribe());
    };
  }, [projectId, activeJobIdsKey, activeJobs, loadAll]);

  // ── 操作 ──────────────────────────────

  const handleResume = useCallback(async (job: NovelJobPayload) => {
    setResumingJobId(job.id);
    try {
      await aiApi.novelJob.resume(job.id);
      toast('success', '已恢复任务', '将从断点继续生成。');
      setView('active');
      await loadAll(true);
    } catch (err) {
      toast('error', '恢复失败', err instanceof Error ? err.message : '恢复任务失败。');
      await loadAll(true);
    } finally {
      setResumingJobId(null);
    }
  }, [loadAll, toast]);

  const handlePause = useCallback(async (job: NovelJobPayload) => {
    try {
      await aiApi.novelJob.cancel(job.id);
      toast('info', '已请求暂停', '将在当前章节完成后结束。');
      await loadAll(true);
    } catch (err) {
      toast('error', '暂停失败', err instanceof Error ? err.message : '暂停任务失败。');
    }
  }, [loadAll, toast]);

  const handleStop = useCallback(async (job: NovelJobPayload) => {
    setStopTarget(null);
    try {
      await aiApi.novelJob.cancel(job.id);
      toast('info', '已请求停止', '将在当前章节完成后结束。');
      await loadAll(true);
    } catch (err) {
      toast('error', '停止失败', err instanceof Error ? err.message : '停止任务失败。');
    }
  }, [loadAll, toast]);

  const handleRestart = useCallback(async () => {
    const job = restartTarget;
    setRestartTarget(null);
    if (!job) return;
    try {
      await aiApi.novelJob.start({ projectId, mode: 'all', maxRetries: 2 });
      toast('success', '已重新开始整书创作');
      setView('active');
      await loadAll(true);
    } catch (err) {
      toast('error', '重新开始失败', err instanceof Error ? err.message : '启动任务失败。');
    }
  }, [loadAll, projectId, restartTarget, toast]);

  const handleStartNewJob = useCallback(async () => {
    setNewJobOpen(false);
    setStartingJob(true);
    try {
      await aiApi.novelJob.start({ projectId, mode: 'all', maxRetries: 2 });
      toast('success', '任务已启动', '整书创作任务已创建。');
      setView('active');
      await loadAll(true);
    } catch (err) {
      toast('error', '任务启动失败', err instanceof Error ? err.message : '启动整书创作失败。');
    } finally {
      setStartingJob(false);
    }
  }, [loadAll, projectId, toast]);

  // ── 冲突决策 ──────────────────────────

  const isSkipDisabled = useCallback((conflict: WritingConflict) => {
    if (!conflict.chapterId) return true;
    return !recoverableJobs.some(
      (job) => job.recoverable && parseResumeTarget(job)?.chapter_id === conflict.chapterId,
    );
  }, [recoverableJobs]);

  const handleConflictSkip = useCallback(async (conflict: WritingConflict) => {
    const match = recoverableJobs.find(
      (job) => job.recoverable && parseResumeTarget(job)?.chapter_id === conflict.chapterId,
    );
    if (!match) {
      toast('warning', '无法跳过', '需先恢复对应任务。');
      return false;
    }
    try {
      await aiApi.novelJob.resume(match.id);
      toast('success', '已跳过此章继续', '将从后续章节继续生成。');
      setView('active');
      await loadAll(true);
      return true;
    } catch (err) {
      toast('error', '操作失败', err instanceof Error ? err.message : '跳过失败。');
      await loadAll(true);
      return false;
    }
  }, [loadAll, recoverableJobs, toast]);

  const handleConflictRetry = useCallback(async (conflict: WritingConflict) => {
    if (hasRunningJob) {
      toast('warning', '无法重试', '当前项目已有进行中的任务，请先停止或等待完成。');
      return false;
    }
    if (!conflict.chapterId) {
      toast('warning', '无法重试', '该冲突缺少章节信息。');
      return false;
    }
    try {
      await aiApi.novelJob.start({
        projectId,
        mode: 'current-to-end',
        currentChapterId: conflict.chapterId,
        maxRetries: 2,
      });
      toast('success', '已开始重试此章', '已创建「当前至结尾」任务。');
      setView('active');
      await loadAll(true);
      return true;
    } catch (err) {
      toast('error', '重试失败', err instanceof Error ? err.message : '重试启动失败。');
      await loadAll(true);
      return false;
    }
  }, [hasRunningJob, loadAll, projectId, toast]);

  const handleConflictResolve = useCallback(async (conflict: WritingConflict) => {
    try {
      await aiApi.conflicts.resolve(projectId, conflict.id);
      setConflicts((previous) => previous.filter((item) => item.id !== conflict.id));
      toast('success', '已标记处理', '该冲突不再展示。');
      return true;
    } catch (err) {
      toast('error', '标记失败', err instanceof Error ? err.message : '标记已处理失败。');
      return false;
    }
  }, [projectId, toast]);

  // ── 渲染 ──────────────────────────────

  const activeCount = activeJobs.length + recoverableJobs.length;
  const conflictCount = conflicts.length;

  const elapsedFor = (job: NovelJobPayload): number => {
    if (!job.started_at) return 0;
    const start = new Date(job.started_at).getTime();
    if (Number.isNaN(start)) return 0;
    return Math.max(Math.floor((Date.now() - start) / 1000), 0);
  };

  const renderSkeleton = () => (
    <div className="space-y-4" role="status" aria-live="polite">
      {[0, 1, 2].map((index) => (
        <div
          key={index}
          className={reducedMotion ? 'h-28 rounded-xl bg-surface-100 dark:bg-surface-700' : 'h-28 animate-pulse rounded-xl bg-surface-100 dark:bg-surface-700'}
        />
      ))}
      <p className="text-sm text-surface-500">正在加载任务…</p>
    </div>
  );

  const renderError = () => (
    <div
      className="flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
      role="alert"
      data-testid="task-error-notice"
    >
      <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1">{error}</span>
      <Button variant="ghost" size="sm" className="shrink-0" onClick={() => void loadAll()}>
        重试
      </Button>
    </div>
  );

  return (
    <div className="flex-1 overflow-auto" data-testid="task-center">
      <div className="mx-auto max-w-4xl px-8 py-8">
        {/* ① 页面头 */}
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-xl font-bold text-surface-800 dark:text-surface-100">
            <ClipboardList size={22} className="text-primary-600" aria-hidden="true" />
            任务中心
          </h2>
          <div className="flex items-center gap-3">
            <span className="hidden text-xs text-surface-400 sm:inline">整书创作任务与写作冲突的统一管理视图</span>
            <Button variant="primary" size="sm" onClick={() => setNewJobOpen(true)} data-testid="task-new-job">
              <Play size={14} aria-hidden="true" />
              新建任务
            </Button>
          </div>
        </div>

        {/* ② Segmented Control（§3.7） */}
        <div
          className="mb-5 inline-flex rounded-full bg-surface-100 p-1 dark:bg-surface-700"
          role="tablist"
          aria-label="任务视图"
        >
          {[
            { id: 'active' as const, label: '待处理', count: activeCount, badgeVariant: 'neutral' as const },
            { id: 'history' as const, label: '历史', count: 0, badgeVariant: 'neutral' as const },
            { id: 'conflicts' as const, label: '写作冲突', count: conflictCount, badgeVariant: 'error' as const },
          ].map((tab) => {
            const active = view === tab.id;
            return (
              <button
                key={tab.id}
                role="tab"
                aria-selected={active}
                aria-controls={`task-panel-${tab.id}`}
                onClick={() => setView(tab.id)}
                className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                  active
                    ? 'bg-primary-600 text-white'
                    : 'bg-transparent text-surface-500 hover:bg-surface-200 dark:text-surface-300 dark:hover:bg-surface-600'
                }`}
                data-testid={`task-tab-${tab.id}`}
              >
                {tab.label}
                {tab.count > 0 && (
                  active
                    ? <span className="rounded-full bg-white/25 px-1.5 text-[11px] font-semibold leading-4">{tab.count}</span>
                    : <Badge variant={tab.badgeVariant}>{tab.count}</Badge>
                )}
              </button>
            );
          })}
        </div>

        {/* ③ 内容面板 */}
        <div id={`task-panel-${view}`} role="tabpanel">
          {view === 'active' && (
            loading
              ? renderSkeleton()
              : error
                ? renderError()
                : activeJobs.length === 0 && recoverableJobs.length === 0
                  ? (
                    <div className="flex flex-col items-center gap-1 py-12 text-center" data-testid="task-empty-active">
                      <ClipboardList size={40} className="mb-2 text-surface-300" aria-hidden="true" />
                      <span className="text-sm font-semibold text-surface-800 dark:text-surface-100">没有待处理的任务</span>
                      <span className="text-xs text-surface-400">启动整书创作后，任务进度与恢复入口会显示在这里。</span>
                      <Button variant="primary" size="sm" className="mt-4" onClick={() => setNewJobOpen(true)}>
                        <Play size={14} aria-hidden="true" />
                        按大纲自动创作
                      </Button>
                    </div>
                  )
                  : (
                    <div className="space-y-4">
                      {activeJobs.map((job) => (
                        <TaskCard
                          key={job.id}
                          job={job}
                          chapterNodes={chapterNodes}
                          elapsedSeconds={elapsedFor(job)}
                          resumingJobId={resumingJobId}
                          onPause={handlePause}
                          onStop={setStopTarget}
                          onGoToChapter={onGoToChapter}
                          onGoToWrite={onGoToWrite}
                        />
                      ))}
                      {recoverableJobs.map((job) => (
                        <TaskCard
                          key={job.id}
                          job={job}
                          chapterNodes={chapterNodes}
                          showRecoverable
                          resumingJobId={resumingJobId}
                          onResume={handleResume}
                          onGoToChapter={onGoToChapter}
                          onGoToWrite={onGoToWrite}
                        />
                      ))}
                    </div>
                  )
          )}

          {view === 'history' && (
            loading
              ? renderSkeleton()
              : error
                ? renderError()
                : historyJobs.length === 0
                  ? (
                    <div className="flex flex-col items-center gap-1 py-12 text-center" data-testid="task-empty-history">
                      <Inbox size={40} className="mb-2 text-surface-300" aria-hidden="true" />
                      <span className="text-sm font-semibold text-surface-800 dark:text-surface-100">还没有创作任务</span>
                      <span className="text-xs text-surface-400">完成一次整书创作后会在这里沉淀历史。</span>
                    </div>
                  )
                  : (
                    <div className="space-y-4">
                      {historyJobs.map((job) => (
                        <TaskCard
                          key={job.id}
                          job={job}
                          chapterNodes={chapterNodes}
                          statsCollapsed
                          resumingJobId={resumingJobId}
                          onRestart={setRestartTarget}
                          onGoToChapter={onGoToChapter}
                          onGoToWrite={onGoToWrite}
                        />
                      ))}
                    </div>
                  )
          )}

          {view === 'conflicts' && (
            <ConflictList
              conflicts={conflicts}
              chapterNodes={chapterNodes}
              loading={loading}
              error={error}
              onRetry={() => void loadAll()}
              isSkipDisabled={isSkipDisabled}
              onSkip={handleConflictSkip}
              onRetryChapter={handleConflictRetry}
              onResolve={handleConflictResolve}
              onGoToChapter={onGoToChapter}
            />
          )}
        </div>
      </div>

      {/* 停止任务二次确认（Modal，无 window.confirm） */}
      <Modal
        open={stopTarget !== null}
        onClose={() => setStopTarget(null)}
        title="停止任务"
        width="sm"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setStopTarget(null)}>
              取消
            </Button>
            <Button variant="error" size="sm" onClick={() => void handleStop(stopTarget as NovelJobPayload)}>
              确认停止
            </Button>
          </>
        )}
      >
        <p className="text-sm leading-relaxed text-surface-600 dark:text-surface-300">
          停止后任务将在当前章节完成后结束，已生成章节会保留。你可以稍后从「可恢复」入口继续。
        </p>
      </Modal>

      {/* 重新开始二次确认 */}
      <Modal
        open={restartTarget !== null}
        onClose={() => setRestartTarget(null)}
        title="重新开始整书创作"
        width="sm"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setRestartTarget(null)}>
              取消
            </Button>
            <Button variant="error" size="sm" onClick={() => void handleRestart()}>
              确认重新开始
            </Button>
          </>
        )}
      >
        <p className="text-sm leading-relaxed text-surface-600 dark:text-surface-300">
          重新开始将创建一篇全新的「全书」创作任务，从头生成整本书。已生成的章节会保留，但会被新任务重新覆盖。
        </p>
      </Modal>

      {/* 新建任务 */}
      <Modal
        open={newJobOpen}
        onClose={() => setNewJobOpen(false)}
        title="新建整书创作任务"
        width="sm"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setNewJobOpen(false)}>
              取消
            </Button>
            <Button variant="primary" size="sm" loading={startingJob} onClick={() => void handleStartNewJob()}>
              {startingJob ? '启动中' : '开始创作'}
            </Button>
          </>
        )}
      >
        <p className="text-sm leading-relaxed text-surface-600 dark:text-surface-300">
          将按大纲从第一章到结尾完整生成整本书（模式：<span className="font-medium">全书</span>）。
          如需从指定章节续写或重试失败章，可在「正文」中使用对应的创作按钮。
        </p>
      </Modal>
    </div>
  );
}
