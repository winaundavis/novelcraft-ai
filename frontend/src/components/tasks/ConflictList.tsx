/**
 * ConflictList — 写作冲突处理区（ui-task-center.md §3.5）
 *
 * 冲突卡（error 语义）：章节名 + 时间 + 内容摘要 + 根因 + 三决策按钮
 * （跳过此章继续 / 重试此章 / 标记已处理，均带 loading 态；标记已处理走 Modal 二次确认）。
 * 只复用 common 组件，不引入任何新依赖。
 */
import { useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Eye,
  Info,
  RefreshCw,
  ShieldCheck,
  SkipForward,
} from 'lucide-react';

import { Badge, Button, Modal } from '../common';
import { formatRelativeTime } from './constants';
import type { OutlineNode, WritingConflict } from '../../types';

export interface ConflictListProps {
  conflicts: WritingConflict[];
  chapterNodes: OutlineNode[];
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  /** 「跳过此章继续」是否可用（无匹配可恢复任务时禁用） */
  isSkipDisabled?: (conflict: WritingConflict) => boolean;
  onSkip?: (conflict: WritingConflict) => Promise<boolean>;
  onRetryChapter?: (conflict: WritingConflict) => Promise<boolean>;
  onResolve?: (conflict: WritingConflict) => Promise<boolean>;
  onGoToChapter?: (chapterId: string) => void;
}

type PendingActionType = 'skip' | 'retry' | 'resolve';
type PendingAction = { id: string; action: PendingActionType } | null;

/** 拆分冲突文本：后端已拆 summary/rootCause，缺失时回退从 content 解析 */
function splitConflictText(conflict: WritingConflict): { summary: string; rootCause: string } {
  if (conflict.summary || conflict.rootCause) {
    return { summary: conflict.summary, rootCause: conflict.rootCause };
  }
  const marker = '（根因：';
  const text = (conflict.content || '').trim();
  if (text.includes(marker)) {
    const [summary, rest] = text.split(marker);
    return { summary: summary.trim(), rootCause: rest.replace(/）$/, '').trim() };
  }
  return { summary: text, rootCause: '' };
}

function chapterTitleOf(conflict: WritingConflict, chapterNodes: OutlineNode[]): string {
  if (!conflict.chapterId) return '未知章节';
  const node = chapterNodes.find((item) => item.id === conflict.chapterId);
  return node?.title || `章节 #${conflict.chapterId.slice(0, 8)}`;
}

export default function ConflictList({
  conflicts,
  chapterNodes,
  loading = false,
  error = '',
  onRetry,
  isSkipDisabled,
  onSkip,
  onRetryChapter,
  onResolve,
  onGoToChapter,
}: ConflictListProps) {
  const [pending, setPending] = useState<PendingAction>(null);
  const [confirmResolveId, setConfirmResolveId] = useState<string | null>(null);

  const runAction = async (
    conflict: WritingConflict,
    action: PendingActionType,
    handler?: (item: WritingConflict) => Promise<boolean>,
  ) => {
    if (!handler || pending) return;
    setPending({ id: conflict.id, action });
    try {
      await handler(conflict);
    } finally {
      setPending(null);
    }
  };

  const confirmResolve = async () => {
    if (!confirmResolveId) return;
    const conflict = conflicts.find((item) => item.id === confirmResolveId);
    setConfirmResolveId(null);
    if (!conflict) return;
    await runAction(conflict, 'resolve', onResolve);
  };

  return (
    <div data-testid="conflict-section">
      {/* 视图头 */}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <AlertTriangle size={14} className="text-error" aria-hidden="true" />
          <span className="text-sm font-semibold text-surface-800 dark:text-surface-100">写作冲突</span>
          <Badge variant="error">{conflicts.length} 条</Badge>
        </div>
        <span className="text-[11px] text-surface-400">事实核查多次失败且不可调和，需人工决策后继续。</span>
      </div>

      {/* 加载态 */}
      {loading && (
        <div className="space-y-3" role="status" aria-live="polite">
          {[0, 1].map((index) => (
            <div key={index} className="h-28 animate-pulse rounded-xl bg-surface-100 dark:bg-surface-700" />
          ))}
          <p className="text-sm text-surface-500">正在加载写作冲突…</p>
        </div>
      )}

      {/* 错误态 */}
      {!loading && error && (
        <div
          className="flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
          role="alert"
          data-testid="task-error-notice"
        >
          <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1">{error}</span>
          {onRetry && (
            <Button variant="ghost" size="sm" className="shrink-0" onClick={onRetry}>
              重试
            </Button>
          )}
        </div>
      )}

      {/* 冲突列表 */}
      {!loading && !error && conflicts.length > 0 && (
        <div className="space-y-3">
          {conflicts.map((conflict) => {
            const { summary, rootCause } = splitConflictText(conflict);
            const chapterTitle = chapterTitleOf(conflict, chapterNodes);
            const skipDisabled = isSkipDisabled?.(conflict) ?? false;
            const busy = pending?.id === conflict.id;

            return (
              <div
                key={conflict.id}
                className="rounded-lg border border-error/30 bg-error-soft p-3 dark:bg-error-soft-dark"
                data-testid={`conflict-card-${conflict.id}`}
              >
                {/* 头部行 */}
                <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 text-xs font-semibold text-error">
                    <AlertTriangle size={13} aria-hidden="true" />
                    {chapterTitle}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-surface-400">
                      {formatRelativeTime(conflict.createdAt)}
                    </span>
                    {conflict.chapterId && onGoToChapter && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="shrink-0"
                        onClick={() => onGoToChapter(conflict.chapterId)}
                      >
                        <Eye size={12} aria-hidden="true" />
                        查看章节
                      </Button>
                    )}
                  </div>
                </div>

                {/* 冲突内容 */}
                <p className="text-sm leading-relaxed text-surface-700 dark:text-surface-200">{summary}</p>

                {/* 根因 */}
                {rootCause && (
                  <div className="mt-1.5 flex items-start gap-1.5 text-[11px] leading-relaxed text-surface-500 dark:text-surface-400">
                    <Info size={12} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
                    <span>根因：{rootCause}</span>
                  </div>
                )}

                {/* 人工决策三按钮 */}
                <div
                  className="mt-3 flex flex-wrap gap-2 border-t border-error/20 pt-3"
                  role="group"
                  aria-label="写作冲突决策"
                >
                  <Button
                    variant="warning"
                    size="sm"
                    disabled={skipDisabled}
                    loading={busy && pending?.action === 'skip'}
                    title={skipDisabled ? '需先恢复对应任务' : undefined}
                    onClick={() => void runAction(conflict, 'skip', onSkip)}
                    data-testid={`conflict-skip-${conflict.id}`}
                  >
                    <SkipForward size={14} aria-hidden="true" />
                    跳过此章继续
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    loading={busy && pending?.action === 'retry'}
                    onClick={() => void runAction(conflict, 'retry', onRetryChapter)}
                    data-testid={`conflict-retry-${conflict.id}`}
                  >
                    <RefreshCw size={14} aria-hidden="true" />
                    重试此章
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    loading={busy && pending?.action === 'resolve'}
                    onClick={() => setConfirmResolveId(conflict.id)}
                    data-testid={`conflict-resolve-${conflict.id}`}
                  >
                    <Check size={14} aria-hidden="true" />
                    标记已处理
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 冲突空状态 */}
      {!loading && !error && conflicts.length === 0 && (
        <div className="flex flex-col items-center gap-1 py-10 text-center" data-testid="conflict-empty">
          <ShieldCheck size={40} className="mb-2 text-surface-300" aria-hidden="true" />
          <span className="text-sm font-semibold text-surface-800 dark:text-surface-100">没有写作冲突</span>
          <span className="text-xs text-surface-400">事实核查通过，无需人工处理。</span>
        </div>
      )}

      {/* 标记已处理二次确认 */}
      <Modal
        open={confirmResolveId !== null}
        onClose={() => setConfirmResolveId(null)}
        title="标记为已处理"
        width="sm"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setConfirmResolveId(null)}>
              取消
            </Button>
            <Button variant="error" size="sm" loading={pending?.action === 'resolve'} onClick={() => void confirmResolve()}>
              <Check size={14} aria-hidden="true" />
              确认标记
            </Button>
          </>
        )}
      >
        <p className="text-sm leading-relaxed text-surface-600 dark:text-surface-300">
          该冲突将不再展示，也不会重新写入账本。若问题仍未解决，后续章节可能再次触发冲突。
        </p>
      </Modal>
    </div>
  );
}
