/**
 * 任务中心（Task Center）共享常量与工具函数。
 * 唯一指定 JOB_STATUS_META，供 TaskCard / 详情 / 徽章统一使用（ui-task-center.md §3.1）。
 */

import type { LucideIcon } from 'lucide-react';
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Clock,
  Loader2,
  RotateCcw,
  XCircle,
} from 'lucide-react';

import type { BadgeVariant } from '../common';
import type { NovelJobPayload, NovelJobStatus } from '../../types';

export interface JobStatusMeta {
  label: string;
  variant: BadgeVariant;
  icon: LucideIcon;
}

/** 任务状态 → Badge 变体 / 图标 / 文案（唯一指定，§3.1） */
export const JOB_STATUS_META: Record<NovelJobStatus, JobStatusMeta> = {
  queued: { label: '排队中', variant: 'info', icon: Clock },
  running: { label: '进行中', variant: 'info', icon: Loader2 },
  interrupted: { label: '已中断', variant: 'warning', icon: AlertTriangle },
  completed: { label: '已完成', variant: 'success', icon: CheckCircle2 },
  failed: { label: '失败', variant: 'error', icon: XCircle },
  cancelled: { label: '已取消', variant: 'neutral', icon: Ban },
};

/** 任务模式 → 中文文案 */
export const JOB_MODE_LABELS: Record<string, string> = {
  all: '全书',
  'current-to-end': '当前至结尾',
  'first-unfinished': '续写未完成',
};

export const TERMINAL_STATUSES: NovelJobStatus[] = ['completed', 'failed', 'cancelled'];

export function isTerminalStatus(status: NovelJobStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/** 相对时间：刚刚 / x 分钟前 / x 小时前 / 昨天 / M月D日 */
export function formatRelativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '—';

  const diffMinutes = Math.floor((now - time) / 60000);
  if (diffMinutes < 1) return '刚刚';
  if (diffMinutes < 60) return `${diffMinutes} 分钟前`;

  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours} 小时前`;

  const date = new Date(time);
  const today = new Date(now);
  const yesterday = new Date(now);
  yesterday.setDate(today.getDate() - 1);

  if (date.toDateString() === today.toDateString()) return '今天';
  if (date.toDateString() === yesterday.toDateString()) return '昨天';
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

/** 运行耗时：x时x分x秒 / x分x秒 / x秒 */
export function formatElapsed(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}时${m}分${s}秒`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}

/** 解析 resume_target_json（后端 json.dumps 的 `{ chapter_id, chapter_title }`） */
export function parseResumeTarget(job: NovelJobPayload): {
  chapter_id?: string;
  chapter_title?: string;
} | null {
  try {
    const parsed = JSON.parse(job.resume_target_json || '{}');
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

/** 标记「可恢复」的派生徽章（非独立 status） */
export const RECOVERABLE_BADGE = {
  label: '可恢复',
  icon: RotateCcw,
};
