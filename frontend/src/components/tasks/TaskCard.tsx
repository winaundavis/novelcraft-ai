/**
 * TaskCard — 任务中心任务卡（ui-task-center.md §3.2 / §3.4 / §3.6）
 *
 * 覆盖：任务卡（状态三重表达 + 进度 + 章数/字数/当前章 + 自检统计 4 指标）、
 * 可恢复/末章失败断点说明、详情展开（三段式断点进度 + 序号点列 + 大指标卡 + 错误 + 元信息）。
 * 只复用 common 组件（Card/Badge/Button），不引入任何新依赖。
 */
import { useMemo, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  ChevronDown,
  Eye,
  FileText,
  Info,
  Link2,
  Pause,
  PenLine,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  SkipForward,
  Sparkles,
  Square,
} from 'lucide-react';

import { Badge, Button, Card } from '../common';
import {
  JOB_MODE_LABELS,
  JOB_STATUS_META,
  formatElapsed,
  formatRelativeTime,
  parseResumeTarget,
} from './constants';
import { useReducedMotion } from './useReducedMotion';
import type { NovelJobPayload, OutlineNode } from '../../types';

export interface TaskCardProps {
  job: NovelJobPayload;
  /** 大纲章节节点（详情序号点列依赖；缺失时降级只显示分段进度条） */
  chapterNodes: OutlineNode[];
  /** 历史视图：自检统计默认收起；待处理默认展开 */
  statsCollapsed?: boolean;
  /** 运行中任务已耗时（秒） */
  elapsedSeconds?: number;
  /** 是否显示「可恢复」派生态（顶部 Badge + 左侧竖边条 + 断点说明） */
  showRecoverable?: boolean;
  /** 正在恢复的任务 id（loading 态） */
  resumingJobId?: string | null;
  onResume?: (job: NovelJobPayload) => void;
  onPause?: (job: NovelJobPayload) => void;
  onStop?: (job: NovelJobPayload) => void;
  onRestart?: (job: NovelJobPayload) => void;
  onGoToChapter?: (chapterId: string) => void;
  onGoToWrite?: () => void;
}

/** 与 MarkdownEditor 一致的已完成章节推导（§3.6：按 mode/requested_chapter_id 偏移） */
function deriveCompletedChapterIds(job: NovelJobPayload, chapterNodes: OutlineNode[]): string[] {
  if (job.completed_chapters <= 0) return [];

  let orderedNodes = [...chapterNodes];
  if (job.mode === 'current-to-end' && job.requested_chapter_id) {
    const startIndex = orderedNodes.findIndex((node) => node.id === job.requested_chapter_id);
    if (startIndex >= 0) orderedNodes = orderedNodes.slice(startIndex);
  }

  return orderedNodes.slice(0, Math.min(job.completed_chapters, orderedNodes.length)).map((node) => node.id);
}

/** 末章失败判定（failed + pending_manual_recovery 且失败章为最后一章）——复用 MarkdownEditor 逻辑 */
function isLastChapterFailure(job: NovelJobPayload, chapterNodes: OutlineNode[]): boolean {
  if (job.status !== 'failed' || !job.pending_manual_recovery || chapterNodes.length === 0) return false;
  const target = parseResumeTarget(job);
  const failedId = (target?.chapter_id || '').trim();
  if (!failedId) return false;
  return chapterNodes[chapterNodes.length - 1].id === failedId;
}

function formatLocalTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { hour12: false });
}

function jobDurationSeconds(job: NovelJobPayload): number {
  const start = new Date(job.started_at).getTime();
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return 0;
  return Math.max(Math.floor((end - start) / 1000), 0);
}

/** 状态徽章：语义色 + lucide 图标 + 文字三重表达（§3.1） */
function StatusBadge({ job }: { job: NovelJobPayload }) {
  const reducedMotion = useReducedMotion();
  const meta = JOB_STATUS_META[job.status] || JOB_STATUS_META.failed;
  const Icon = meta.icon;

  return (
    <Badge variant={meta.variant} data-testid={`task-status-${job.id}`}>
      {job.status === 'queued' ? (
        <>
          <span
            className={reducedMotion ? 'h-1.5 w-1.5 rounded-full bg-info' : 'h-1.5 w-1.5 rounded-full bg-info animate-pulse'}
            aria-hidden="true"
          />
          {meta.label}
        </>
      ) : (
        <>
          <Icon
            size={12}
            className={job.status === 'running' && !reducedMotion ? 'animate-spin' : ''}
            aria-hidden="true"
          />
          {meta.label}
        </>
      )}
    </Badge>
  );
}

/** 自检统计 4 指标（卡片内小尺寸，§3.2-④） */
function StatsInline({ job }: { job: NovelJobPayload }) {
  const items = [
    { key: 'fact_check_count', label: '事实核查', icon: ShieldCheck, color: 'text-primary-500', strong: false },
    { key: 'rewrite_count', label: '重写', icon: PenLine, color: 'text-info', strong: false },
    { key: 'hook_miss_count', label: '钩子未命中', icon: Link2, color: 'text-warning', strong: true },
    { key: 'quality_debt_count', label: 'AI 味质量债', icon: Sparkles, color: 'text-error', strong: true },
  ] as const;

  return (
    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map((item) => {
        const value = job[item.key] ?? 0;
        const emphasized = item.strong && value > 0;
        return (
          <div key={item.key} className="rounded-lg bg-surface-50 px-3 py-2.5 dark:bg-surface-800">
            <div className="flex items-center gap-1 text-[11px] text-surface-400">
              <item.icon size={13} className={item.color} aria-hidden="true" />
              {item.label}
            </div>
            <div className={`mt-1 text-sm font-semibold tabular-nums text-surface-700 dark:text-surface-200 ${emphasized ? item.color : ''}`}>
              {value} 次
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** 详情自检统计（大指标卡 + 可折叠说明，§3.6-②） */
function StatsDetail({ job }: { job: NovelJobPayload }) {
  const [showHints, setShowHints] = useState(false);
  const items = [
    { key: 'fact_check_count', label: '事实核查', icon: ShieldCheck, color: 'text-primary-500', hint: 'LLM 客观矛盾核查次数', strong: false },
    { key: 'rewrite_count', label: '重写', icon: PenLine, color: 'text-info', hint: '事实重写 + 结尾重写 + 开头重写次数', strong: false },
    { key: 'hook_miss_count', label: '钩子未命中', icon: Link2, color: 'text-warning', hint: '未检测到强钩、触发结尾重写的次数', strong: true },
    { key: 'quality_debt_count', label: 'AI 味质量债', icon: Sparkles, color: 'text-error', hint: 'AI 味高/中项数（需修订时重点排查）', strong: true },
  ] as const;

  return (
    <div className="mt-4" data-testid={`task-stats-${job.id}`}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {items.map((item) => {
          const value = job[item.key] ?? 0;
          const emphasized = item.strong && value > 0;
          return (
            <div
              key={item.key}
              className="rounded-lg border border-surface-200 bg-surface-50 px-3 py-3 dark:border-surface-700 dark:bg-surface-800"
            >
              <div className="flex items-center gap-1.5 text-[11px] text-surface-400">
                <item.icon size={14} className={item.color} aria-hidden="true" />
                {item.label}
              </div>
              <div className={`mt-1 text-xl font-bold tabular-nums ${emphasized ? item.color : 'text-surface-800 dark:text-surface-100'}`}>
                {value}
              </div>
            </div>
          );
        })}
      </div>
      <button
        onClick={(event) => {
          event.stopPropagation();
          setShowHints((previous) => !previous);
        }}
        className="mt-2 flex items-center gap-1 text-[11px] text-surface-400 hover:text-surface-600 dark:hover:text-surface-300"
        aria-expanded={showHints}
        aria-controls={`task-stats-hints-${job.id}`}
      >
        <Info size={12} aria-hidden="true" />
        指标说明
        <ChevronDown size={12} className={`transition-transform ${showHints ? '' : '-rotate-90'}`} aria-hidden="true" />
      </button>
      {showHints && (
        <ul id={`task-stats-hints-${job.id}`} className="mt-1 space-y-0.5 text-[11px] leading-relaxed text-surface-400" role="region" aria-label="指标说明">
          {items.map((item) => (
            <li key={item.key}>
              {item.label}：{item.hint}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 三段式断点进度条（已生成 success / 当前 primary / 剩余 surface，§3.6-①） */
function SegmentProgress({ job }: { job: NovelJobPayload }) {
  const total = Math.max(job.total_chapters, 1);
  const completed = Math.min(job.completed_chapters, total);
  const hasCurrent = job.status === 'queued' || job.status === 'running';
  const current = hasCurrent ? 1 : 0;
  const remaining = Math.max(total - completed - current, 0);
  const completedPct = (completed / total) * 100;
  const currentPct = (current / total) * 100;

  return (
    <div>
      <div className="flex h-1.5 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700">
        <div className="h-full rounded-l-full bg-success transition-all duration-700" style={{ width: `${completedPct}%` }} />
        {hasCurrent && (
          <div className="h-full bg-primary-500 transition-all duration-700" style={{ width: `${currentPct}%` }} />
        )}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-surface-400">
        <span className="text-success">■ 已生成 {completed}</span>
        {hasCurrent && <span className="text-primary-500">■ 当前</span>}
        <span>■ 剩余 {remaining}</span>
      </div>
    </div>
  );
}

/** 章节序号点列（outline 缺失时不渲染，§3.6-①） */
function ChapterDots({ job, chapterNodes }: { job: NovelJobPayload; chapterNodes: OutlineNode[] }) {
  const completedIds = useMemo(() => deriveCompletedChapterIds(job, chapterNodes), [job, chapterNodes]);
  const currentId = job.current_chapter_id;
  const isLive = job.status === 'queued' || job.status === 'running';
  const failedId = job.status === 'failed' ? parseResumeTarget(job)?.chapter_id : undefined;

  return (
    <div className="mt-3 flex flex-wrap gap-1.5" aria-hidden="true">
      {chapterNodes.map((node) => {
        const dotClass = completedIds.includes(node.id)
          ? 'bg-success'
          : isLive && node.id === currentId
            ? 'bg-primary-500 ring-2 ring-primary-300'
            : node.id === failedId
              ? 'bg-error'
              : 'bg-surface-300 dark:bg-surface-600';
        return (
          <span
            key={node.id}
            className={`h-2 w-2 rounded-full ${dotClass}`}
            title={node.title}
          />
        );
      })}
    </div>
  );
}

export default function TaskCard({
  job,
  chapterNodes,
  statsCollapsed = false,
  elapsedSeconds,
  showRecoverable = false,
  resumingJobId = null,
  onResume,
  onPause,
  onStop,
  onRestart,
  onGoToChapter,
  onGoToWrite,
}: TaskCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [statsOpen, setStatsOpen] = useState(!statsCollapsed);

  const total = Math.max(job.total_chapters, 1);
  const completed = Math.min(job.completed_chapters, total);
  const progressPct = Math.round((completed / total) * 100);
  const isLive = job.status === 'queued' || job.status === 'running';
  const resumeTarget = parseResumeTarget(job);
  const lastChapterFailure = isLastChapterFailure(job, chapterNodes);

  const stopPropagation = (event: { stopPropagation: () => void }) => event.stopPropagation();

  return (
    <Card
      clickable
      onClick={() => setExpanded((previous) => !previous)}
      className={showRecoverable ? 'w-full border-l-4 border-l-warning' : 'w-full'}
      data-testid={`task-card-${job.id}`}
    >
      {/* ① 头部行 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <StatusBadge job={job} />
          {showRecoverable && (
            <Badge variant="warning" data-testid={`task-recoverable-${job.id}`}>
              <RotateCcw size={12} aria-hidden="true" />
              可恢复
            </Badge>
          )}
        </div>
        <span className="text-[11px] text-surface-400" title={job.updated_at || job.started_at}>
          {isLive && elapsedSeconds !== undefined
            ? `已耗时 ${formatElapsed(elapsedSeconds)}`
            : formatRelativeTime(job.updated_at || job.started_at)}
        </span>
      </div>

      {/* ② 标题行 */}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold text-surface-800 dark:text-surface-100">
          {job.mode === 'all' ? '整书创作' : '续写创作'}
        </span>
        <Badge variant="neutral">{JOB_MODE_LABELS[job.mode] || job.mode}</Badge>
        {job.fallback_chapters > 0 && (
          <Badge variant="warning" data-testid={`task-fallback-${job.id}`}>
            <AlertTriangle size={12} aria-hidden="true" />
            {job.fallback_chapters} 章降级
          </Badge>
        )}
        {job.llm_provider && (
          <span className="text-[11px] text-surface-400">
            {job.llm_provider}
            {job.llm_model ? `/${job.llm_model}` : ''}
          </span>
        )}
      </div>

      {/* 降级提示条（运行中 + fallback，§3.3） */}
      {isLive && job.fallback_chapters > 0 && (
        <div
          className="mt-3 flex items-start gap-2 rounded-lg bg-info-soft px-3 py-2.5 text-xs text-info dark:bg-info-soft-dark dark:text-info-dark"
          data-testid={`task-degraded-notice-${job.id}`}
        >
          <Info size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>当前章节使用了降级生成（LLM 不可用），质量可能低于预期。</span>
        </div>
      )}

      {/* ③ 进度区 */}
      <div className="mt-3 space-y-1.5">
        <div
          className="h-1.5 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={completed}
          aria-label="章节进度"
          data-testid={`task-progress-${job.id}`}
        >
          <div
            className="h-full rounded-full bg-primary-500 transition-all duration-700"
            style={{ width: `${progressPct}%` }}
          />
        </div>

        <div className="flex items-center justify-between text-[11px]">
          <span className="text-surface-500">{completed}/{total} 章</span>
          <span className="tabular-nums text-surface-400">{(job.generated_words || 0).toLocaleString()} 字</span>
        </div>

        {isLive && job.current_chapter_title && (
          <div className="flex items-center gap-1 text-[11px] text-surface-500 dark:text-surface-300">
            <FileText size={12} className="shrink-0 text-primary-500" aria-hidden="true" />
            <span>正在创作：《{job.current_chapter_title}》({Math.min(completed + 1, total)}/{total})</span>
          </div>
        )}

        {!isLive && showRecoverable && (
          <div className="text-[11px] text-surface-500" data-testid={`task-resume-hint-${job.id}`}>
            {job.status === 'interrupted'
              ? <>已从断点中断。恢复后将<strong className="font-semibold">从第一个未完成章节继续</strong>，不重跑已生成章节。</>
              : job.pending_manual_recovery
                ? <>失败章待处理 · 已完成 {completed}/{total} 章 · 恢复后将<strong className="font-semibold">跳过失败章</strong>《{resumeTarget?.chapter_title || '—'}》继续。</>
                : `已完成 ${completed}/${total} 章 · 将从断点继续生成，不重跑已生成章节。`}
          </div>
        )}

        {job.failed_chapters > 0 && (
          <div className="flex items-center gap-1 text-[11px] text-error" data-testid={`task-failed-${job.id}`}>
            <AlertTriangle size={12} aria-hidden="true" />
            {job.failed_chapters} 章失败
          </div>
        )}
      </div>

      {/* ④ 自检统计 4 指标（可折叠；待处理默认展开、历史默认收起） */}
      <div className="mt-3">
        <button
          onClick={(event) => {
            stopPropagation(event);
            setStatsOpen((previous) => !previous);
          }}
          className="flex items-center gap-1 text-[11px] font-medium text-surface-400 transition-colors hover:text-surface-600 dark:hover:text-surface-300"
          aria-expanded={statsOpen}
          aria-controls={`task-stats-panel-${job.id}`}
        >
          <ChevronDown size={12} className={`transition-transform ${statsOpen ? '' : '-rotate-90'}`} aria-hidden="true" />
          自检统计
        </button>
        {statsOpen && (
          <div id={`task-stats-panel-${job.id}`} role="region" aria-label="自检统计">
            <StatsInline job={job} />
          </div>
        )}
      </div>

      {/* ⑤ 操作行（§3.1 状态 → 操作矩阵） */}
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-surface-200 pt-3 dark:border-surface-700">
        {job.status === 'queued' && onStop && (
          <Button
            variant="ghost"
            size="sm"
            className="text-error"
            onClick={(event) => {
              stopPropagation(event);
              onStop(job);
            }}
            data-testid={`task-stop-${job.id}`}
          >
            <Square size={14} aria-hidden="true" />
            停止任务
          </Button>
        )}

        {job.status === 'running' && (
          <>
            {onPause && (
              <Button
                variant="warning"
                size="sm"
                onClick={(event) => {
                  stopPropagation(event);
                  onPause(job);
                }}
                data-testid={`task-pause-${job.id}`}
              >
                <Pause size={14} aria-hidden="true" />
                当前章后暂停
              </Button>
            )}
            {onStop && (
              <Button
                variant="ghost"
                size="sm"
                className="text-error"
                onClick={(event) => {
                  stopPropagation(event);
                  onStop(job);
                }}
                data-testid={`task-stop-${job.id}`}
              >
                <Square size={14} aria-hidden="true" />
                停止任务
              </Button>
            )}
          </>
        )}

        {job.status === 'interrupted' && showRecoverable && onResume && (
          <Button
            variant="primary"
            size="sm"
            loading={resumingJobId === job.id}
            onClick={(event) => {
              stopPropagation(event);
              onResume(job);
            }}
            data-testid={`task-resume-${job.id}`}
          >
            <RotateCcw size={14} aria-hidden="true" />
            恢复任务
          </Button>
        )}

        {job.status === 'failed' && job.pending_manual_recovery && job.recoverable && (
          <>
            {onResume && (
              <Button
                variant="warning"
                size="sm"
                loading={resumingJobId === job.id}
                onClick={(event) => {
                  stopPropagation(event);
                  onResume(job);
                }}
                data-testid={`task-resume-${job.id}`}
              >
                <SkipForward size={14} aria-hidden="true" />
                恢复并跳过失败章
              </Button>
            )}
            {resumeTarget?.chapter_id && onGoToChapter && (
              <Button
                variant="ghost"
                size="sm"
                onClick={(event) => {
                  stopPropagation(event);
                  onGoToChapter(resumeTarget.chapter_id as string);
                }}
              >
                <Eye size={14} aria-hidden="true" />
                查看失败章
              </Button>
            )}
          </>
        )}

        {(job.status === 'completed' || job.status === 'cancelled') && (
          <>
            <Button
              variant="ghost"
              size="sm"
              onClick={(event) => {
                stopPropagation(event);
                setExpanded((previous) => !previous);
              }}
              data-testid={`task-expand-${job.id}`}
            >
              <ChevronDown size={14} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} aria-hidden="true" />
              查看详情
            </Button>
            {job.status === 'completed' && onRestart && (
              <Button
                variant="ghost"
                size="sm"
                onClick={(event) => {
                  stopPropagation(event);
                  onRestart(job);
                }}
                data-testid={`task-restart-${job.id}`}
              >
                <RefreshCw size={14} aria-hidden="true" />
                重新开始
              </Button>
            )}
          </>
        )}

        {/* 末章失败：无操作按钮，渲染 error 提示条（§3.4） */}
        {lastChapterFailure && (
          <div
            className="flex w-full items-start gap-2 rounded-lg border border-error/30 bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
            data-testid={`task-last-chapter-failure-${job.id}`}
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
              <span>失败章为最后一章，无法自动恢复。请先到「正文」人工处理该章节，再重新开始整书创作。</span>
              {onGoToWrite && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="shrink-0"
                  onClick={(event) => {
                    stopPropagation(event);
                    onGoToWrite();
                  }}
                >
                  前往正文
                </Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 详情展开（§3.6） */}
      {expanded && (
        <div className="mt-4 border-t border-surface-200 pt-4 dark:border-surface-700">
          <SegmentProgress job={job} />
          {chapterNodes.length > 0 && <ChapterDots job={job} chapterNodes={chapterNodes} />}

          <StatsDetail job={job} />

          {job.status === 'failed' && (job.error || job.message) && (
            <div
              className="mt-4 rounded-lg border border-error/30 bg-error-soft px-3 py-3 text-sm text-error dark:bg-error-soft-dark dark:text-error-dark"
              data-testid={`task-error-${job.id}`}
            >
              <AlertCircle size={14} className="mr-1.5 inline" aria-hidden="true" />
              <span className="whitespace-pre-wrap">{job.error || job.message}</span>
            </div>
          )}

          <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-1.5 rounded-lg bg-surface-50 px-3 py-3 text-[11px] text-surface-500 dark:bg-surface-800 dark:text-surface-400 sm:grid-cols-3">
            <div>任务 ID：<span className="font-mono">{job.id}</span></div>
            <div>启动时间：{formatLocalTime(job.started_at)}</div>
            <div>完成时间：{job.finished_at ? formatLocalTime(job.finished_at) : '—'}</div>
            <div>耗时：{job.finished_at ? formatElapsed(jobDurationSeconds(job)) : isLive ? formatElapsed(elapsedSeconds ?? jobDurationSeconds(job)) : '—'}</div>
            <div>模型：{job.llm_provider ? `${job.llm_provider}${job.llm_model ? ` / ${job.llm_model}` : ''}` : '—'}</div>
            <div>生成来源：{job.last_generation_source === 'fallback' ? '降级' : 'LLM'}</div>
            {job.retry_count > 0 && (
              <div>重试：{job.retry_count} / {job.max_retries}</div>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
