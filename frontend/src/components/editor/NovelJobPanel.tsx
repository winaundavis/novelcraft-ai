/**
 * NovelJobPanel — 长篇创作任务进度面板
 *
 * 显示后端 novel-jobs 任务的实时进度，包含：
 *   - 当前章节名、完成章数/总章数、累计字数、失败章数、耗时
 *   - 暂停（当前章后停）/ 停止按钮
 *
 * 用法（在 MarkdownEditor 中）：
 *   <NovelJobPanel
 *     job={job}
 *     elapsedSeconds={elapsed}
 *     onPauseAfterCurrent={() => cancelNovelJob(jobId)}
 *     onStop={() => cancelNovelJob(jobId)}
 *   />
 */

import { Clock, FileText, Hash, AlertTriangle, Square, Pause } from 'lucide-react';

import { Badge } from '../common';
import { JOB_STATUS_META } from '../tasks/constants';
import { useReducedMotion } from '../tasks/useReducedMotion';
import type { NovelJobPayload } from '../../types';

interface NovelJobPanelProps {
  job: NovelJobPayload;
  /** 从任务启动到现在的秒数 */
  elapsedSeconds: number;
  /** 当前章后暂停 */
  onPauseAfterCurrent?: () => void;
  /** 立即停止 */
  onStop?: () => void;
}

function formatElapsed(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}时${m}分${s}秒`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}

export default function NovelJobPanel({
  job,
  elapsedSeconds,
  onPauseAfterCurrent,
  onStop,
}: NovelJobPanelProps) {
  const isRunning = job.status === 'running' || job.status === 'queued';
  const progressPct = job.total_chapters > 0
    ? Math.round((job.completed_chapters / job.total_chapters) * 100)
    : 0;
  const meta = JOB_STATUS_META[job.status] || JOB_STATUS_META.failed;
  const Icon = meta.icon;
  const reducedMotion = useReducedMotion();

  return (
    <div
      className="border-b border-primary-200 bg-primary-50 px-4 py-2 dark:border-primary-800 dark:bg-primary-900/30"
      data-testid="novel-job-panel"
    >
      {/* 状态栏（语义色 + lucide 图标 + 文字三重表达，§3.1） */}
      <div className="mb-1.5 flex items-center justify-between">
        <Badge variant={meta.variant} data-testid="job-status-badge">
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
              <Icon size={12} className={job.status === 'running' && !reducedMotion ? 'animate-spin' : ''} aria-hidden="true" />
              {meta.label}
            </>
          )}
        </Badge>
        <span className="text-[11px] text-primary-500">
          {formatElapsed(elapsedSeconds)}
        </span>
      </div>

      {/* 指标行 */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-primary-600 dark:text-primary-400">
        {job.current_chapter_title && (
          <span className="flex items-center gap-1" data-testid="job-current-chapter">
            <FileText size={12} />
            {job.current_chapter_title}
          </span>
        )}
        <span className="flex items-center gap-1" data-testid="job-chapter-progress">
          <Hash size={12} />
          {job.completed_chapters}/{job.total_chapters} 章
        </span>
        <span className="flex items-center gap-1" data-testid="job-word-count">
          <Clock size={12} />
          {job.generated_words.toLocaleString()} 字
        </span>
        {job.failed_chapters > 0 && (
          <span className="flex items-center gap-1 text-error" data-testid="job-failed-count">
            <AlertTriangle size={12} />
            {job.failed_chapters} 章失败
          </span>
        )}
      </div>

      {/* 进度条 */}
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-primary-200 dark:bg-primary-800">
        <div
          className="h-full rounded-full bg-primary-500 transition-all duration-700"
          style={{ width: `${isRunning ? progressPct : 100}%` }}
          data-testid="job-progress-bar"
        />
      </div>

      {/* 操作按钮 */}
      {isRunning && (
        <div className="mt-2 flex gap-2">
          {onPauseAfterCurrent && (
            <button
              onClick={onPauseAfterCurrent}
              className="btn-ghost flex items-center gap-1 px-2 py-1 text-xs"
              data-testid="job-pause-after-current"
            >
              <Pause size={12} />
              当前章后暂停
            </button>
          )}
          {onStop && (
            <button
              onClick={onStop}
              className="btn-ghost flex items-center gap-1 px-2 py-1 text-xs text-error"
              data-testid="job-stop-button"
            >
              <Square size={12} />
              停止任务
            </button>
          )}
        </div>
      )}

      {job.error && (
        <p className="mt-1 text-xs text-error" data-testid="job-error-message">
          {job.error}
        </p>
      )}
    </div>
  );
}
