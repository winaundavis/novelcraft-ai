/**
 * ProjectionStatusPanel — 章节记忆投影状态面板（P1 Codex 复测闭环）
 *
 * 展示当前章节 5 路记忆投影（章节摘要 / 事实 / 实体 / 卷摘要 / FTS）的同步状态，
 * 明确「正文已保存但记忆尚未更新」的可见状态；failed 项提供单项重试按钮。
 *
 * 用法：
 *   <ProjectionStatusPanel projectId={projectId} chapterId={currentChapterId} />
 */

import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, RotateCw, Clock } from 'lucide-react';

import { chapterApi } from '../../services/api';
import type { MemoryProjection, ReplayProjectionResult } from '../../types';

const TYPE_LABELS: Record<string, string> = {
  chapter_summary: '章节摘要',
  facts: '事实抽取',
  entities: '实体抽取',
  volume: '卷摘要',
  fts: 'FTS 索引',
};

const STATUS_META: Record<string, { label: string; icon: typeof CheckCircle2; cls: string }> = {
  done: { label: '已同步', icon: CheckCircle2, cls: 'text-emerald-600 dark:text-emerald-400' },
  failed: { label: '同步失败', icon: AlertTriangle, cls: 'text-red-600 dark:text-red-400' },
  skipped: { label: '已跳过', icon: Clock, cls: 'text-amber-600 dark:text-amber-400' },
  pending: { label: '待同步', icon: Clock, cls: 'text-slate-400' },
};

interface Props {
  projectId: string;
  chapterId?: string | null;
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  leadingControl?: ReactNode;
  companionContent?: ReactNode;
}

export default function ProjectionStatusPanel({ projectId, chapterId, expanded: controlledExpanded, onExpandedChange, leadingControl, companionContent }: Props) {
  const [projections, setProjections] = useState<MemoryProjection[]>([]);
  const [loading, setLoading] = useState(false);
  const [replaying, setReplaying] = useState<string | null>(null);
  const [localExpanded, setLocalExpanded] = useState(false);
  const expanded = controlledExpanded ?? localExpanded;

  const load = useCallback(async () => {
    if (!chapterId) {
      setProjections([]);
      return;
    }
    setLoading(true);
    try {
      const json = await chapterApi.listProjections(projectId, chapterId);
      setProjections(json.data || []);
    } catch {
      setProjections([]);
    } finally {
      setLoading(false);
    }
  }, [projectId, chapterId]);

  useEffect(() => {
    load();
  }, [load]);

  if (!chapterId && !leadingControl) return null;

  const failedItems = projections.filter((p) => p.status === 'failed');
  const hasWork = projections.some((p) => p.status !== 'done');

  async function handleReplay(type: string) {
    setReplaying(type);
    try {
      const json = await chapterApi.replayProjection(projectId, chapterId!, type);
      const result = json.data as ReplayProjectionResult;
      await load();
      // 重放后仍失败 → 保留提示（detail 已更新）
      if (result?.status === 'failed') {
        // 状态仍显示 failed，detail 含原因，UI 不额外弹窗
      }
    } catch {
      // 网络/服务错误：保留原状态
    } finally {
      setReplaying(null);
    }
  }

  return (
    <div
      className={leadingControl ? 'grid grid-cols-2 bg-white text-xs dark:bg-surface-850' : 'rounded-lg border border-primary-200 bg-white text-sm dark:border-primary-800 dark:bg-primary-950'}
      data-testid="projection-panel"
    >
      {leadingControl}
      <button
        type="button"
        onClick={() => { setLocalExpanded(!expanded); onExpandedChange?.(!expanded); }}
        disabled={!chapterId}
        className={`flex w-full min-w-0 items-center justify-between gap-2 px-3 py-2 text-left transition-colors hover:bg-primary-50/70 dark:hover:bg-primary-900/20 ${leadingControl ? 'border-l border-surface-200 dark:border-surface-700' : 'rounded-lg'}`}
        aria-expanded={expanded}
        aria-controls="projection-panel-content"
        data-testid="projection-panel-toggle"
      >
        <span className="flex min-w-0 items-center gap-2 font-medium text-slate-700 dark:text-slate-200">
          {expanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
          <span className="truncate">章节记忆同步</span>
        </span>
        {failedItems.length > 0 ? (
          <span className="shrink-0 text-xs text-red-600 dark:text-red-400" data-testid="projection-failed-count">
            有 {failedItems.length} 项待处理
          </span>
        ) : !chapterId ? (
          <span className="shrink-0 text-xs text-slate-400">未选章节</span>
        ) : loading ? (
          <span className="shrink-0 text-xs text-slate-400">正在检查…</span>
        ) : (
          <span className="shrink-0 text-xs text-emerald-600 dark:text-emerald-400">全部已同步</span>
        )}
      </button>
      {companionContent && <div className="col-span-2 border-t border-surface-200 px-3 py-2 dark:border-surface-700">{companionContent}</div>}

      {expanded ? (
        <div id="projection-panel-content" className="col-span-2 border-t border-primary-100 px-3 py-2.5 dark:border-primary-900" data-testid="projection-panel-content">
          {projections.length === 0 && !loading && hasWork === false ? (
            <p className="text-xs text-slate-400">本章暂无记忆投影记录（尚未生成章节）。</p>
          ) : (
            <ul className="space-y-1" data-testid="projection-list">
              {projections.length === 0 && loading ? (
                <li className="text-xs text-slate-400">投影状态加载中…</li>
              ) : (
                projections.map((p) => {
                  const meta = STATUS_META[p.status] || STATUS_META.pending;
                  const Icon = meta.icon;
                  return (
                    <li key={p.projection_type} className="flex items-center justify-between gap-2">
                      <span className="flex items-center gap-1.5 text-slate-600 dark:text-slate-300">
                        <Icon className={`h-3.5 w-3.5 ${meta.cls}`} />
                        {TYPE_LABELS[p.projection_type] || p.projection_type}
                      </span>
                      <span className="flex items-center gap-2">
                        {p.status === 'failed' && p.detail ? (
                          <span className="max-w-[160px] truncate text-xs text-slate-400" title={p.detail}>
                            {p.detail}
                          </span>
                        ) : null}
                        {p.status === 'failed' ? (
                          <button
                            type="button"
                            onClick={() => handleReplay(p.projection_type)}
                            disabled={replaying === p.projection_type}
                            className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50 dark:text-red-400 dark:hover:bg-red-950/40"
                            data-testid={`projection-replay-${p.projection_type}`}
                          >
                            <RotateCw className={`h-3 w-3 ${replaying === p.projection_type ? 'animate-spin' : ''}`} />
                            重试
                          </button>
                        ) : (
                          <span className={`text-xs ${meta.cls}`}>{meta.label}</span>
                        )}
                      </span>
                    </li>
                  );
                })
              )}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
