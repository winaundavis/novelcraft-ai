/**
 * ForeshadowPanel — 伏笔账本管理面板（P2-1 前端入口）
 *
 * 三个能力：AI 建议候选（确认/忽略）、已确认伏笔列表（状态徽章/目标章/描述/状态变更/删除）、
 * 超期告警与大纲一致性提示。严格遵循 docs/ui-design-system.md Token，明暗成对。
 * 只复用 common 组件（Card/Badge/Button/Select/Modal/Toast），不引入任何新依赖。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Flag,
  Inbox,
  Link2,
  Sparkles,
  Trash2,
} from 'lucide-react';

import { Badge, Button, Modal, Select, useToast } from '../common';
import FulltextAskPanel from '../fulltext/FulltextAskPanel';
import ForeshadowManualEntry from './ForeshadowManualEntry';
import type { ChapterOption } from './ForeshadowManualEntry';
import { foreshadowApi } from '../../services/api';
import { useEditorStore } from '../../stores';
import type { BadgeVariant } from '../common';
import type {
  Foreshadow,
  ForeshadowListData,
  ForeshadowSuggestion,
  ForeshadowStatus,
} from '../../types';

const STATUS_META: Record<ForeshadowStatus, { label: string; variant: BadgeVariant }> = {
  planted: { label: '已埋设', variant: 'neutral' },
  progressing: { label: '推进中', variant: 'info' },
  resolved: { label: '已回收', variant: 'success' },
  abandoned: { label: '已废弃', variant: 'neutral' },
};

const STATUS_OPTIONS: Array<{ value: ForeshadowStatus; label: string }> = [
  { value: 'planted', label: '已埋设' },
  { value: 'progressing', label: '推进中' },
  { value: 'resolved', label: '已回收' },
  { value: 'abandoned', label: '已废弃' },
];

function ForeshadowRow({
  item,
  updating,
  onStatusChange,
  onDelete,
}: {
  item: Foreshadow;
  updating: boolean;
  onStatusChange: (fid: string, status: ForeshadowStatus) => void;
  onDelete: () => void;
}) {
  const [eventsOpen, setEventsOpen] = useState(false);
  const meta = STATUS_META[item.status] || STATUS_META.planted;
  const targetLabel = item.totalChapters > 0
    ? `第 ${item.targetChapter}/${item.totalChapters} 章`
    : `第 ${item.targetChapter} 章`;

  return (
    <div className="card" data-testid={`foreshadow-item-${item.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={meta.variant} data-testid={`foreshadow-status-${item.id}`}>{meta.label}</Badge>
            <span className="font-semibold text-surface-800 dark:text-surface-100">{item.name}</span>
            {item.overdue && (
              <Badge variant="warning" data-testid={`foreshadow-overdue-${item.id}`}>
                <AlertTriangle size={12} aria-hidden="true" />
                超期 {item.overdueBy} 章
              </Badge>
            )}
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-surface-500 dark:text-surface-400">
            {item.description || '暂无描述。'}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-surface-400">
            <span className="flex items-center gap-1">
              <Flag size={11} aria-hidden="true" />
              埋设于第 {item.plantChapter} 章
            </span>
            <span className="flex items-center gap-1">
              <Link2 size={11} aria-hidden="true" />
              目标 {targetLabel}
            </span>
            {item.resolution && (
              <span>回收方式：{item.resolution}</span>
            )}
          </div>
          {item.events.length > 0 && (
            <button
              onClick={() => setEventsOpen((previous) => !previous)}
              className="mt-2 flex items-center gap-1 text-[11px] text-surface-400 hover:text-surface-600 dark:hover:text-surface-300"
              aria-expanded={eventsOpen}
              aria-controls={`foreshadow-events-${item.id}`}
            >
              <ChevronDown size={12} className={`transition-transform ${eventsOpen ? '' : '-rotate-90'}`} aria-hidden="true" />
              事件记录（{item.events.length}）
            </button>
          )}
          {eventsOpen && (
            <ul
              id={`foreshadow-events-${item.id}`}
              className="mt-1.5 space-y-1 text-[11px] leading-relaxed text-surface-500 dark:text-surface-400"
              role="region"
              aria-label="伏笔事件记录"
            >
              {item.events.map((event, index) => (
                <li key={`${event.at || index}-${index}`} className="flex flex-wrap gap-x-2">
                  <span className="text-surface-400">
                    {event.at ? new Date(event.at).toLocaleString('zh-CN', { hour12: false }) : `#${index + 1}`}
                  </span>
                  <span>
                    {event.status === 'resolved' ? '已回收' : event.status === 'abandoned' ? '已废弃' : event.status === 'progressing' ? '推进中' : '已埋设'}
                    {event.note ? `：${event.note}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex shrink-0 items-end gap-2">
          <Select
            label="状态"
            className="w-28"
            value={item.status}
            disabled={updating}
            onChange={(event) => onStatusChange(item.id, event.target.value as ForeshadowStatus)}
            data-testid={`foreshadow-status-select-${item.id}`}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
          <Button
            variant="ghost"
            size="sm"
            className="text-error"
            disabled={updating}
            onClick={onDelete}
            data-testid={`foreshadow-delete-${item.id}`}
          >
            <Trash2 size={14} aria-hidden="true" />
            删除
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function ForeshadowPanel({ projectId }: { projectId: string }) {
  const toast = useToast();
  // 手写登记需要「大纲章节列表」与「当前打开的章」：两者都在编辑器 store 里（与工作台一致）
  const outlineData = useEditorStore((state) => state.outlineData);
  const currentChapterId = useEditorStore((state) => state.currentChapterId);

  const chapterOptions: ChapterOption[] = useMemo(
    () =>
      (outlineData?.nodes || [])
        .filter((node) => node.type === 'chapter')
        .map((node) => ({ id: node.id, order: Number(node.order) || 0, title: node.title || '未命名' }))
        .sort((a, b) => a.order - b.order),
    [outlineData],
  );

  const [listData, setListData] = useState<ForeshadowListData | null>(null);
  const [suggestions, setSuggestions] = useState<ForeshadowSuggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [suggesting, setSuggesting] = useState(false);
  const [suggestError, setSuggestError] = useState('');
  const [confirmingIndex, setConfirmingIndex] = useState<number | null>(null);
  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const [deleteModalId, setDeleteModalId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError('');
    try {
      const json = await foreshadowApi.list(projectId);
      setListData(json.data);
    } catch (err) {
      const raw = err instanceof Error ? err.message : '';
      // "Failed to fetch" 对作者毫无信息量（实测：接口路径少了尾斜杠 → 307 跳到
      // 直连后端的绝对地址 → 跨源 → 在浏览器里就长这样）。这种网络层失败统一给一句可执行的话。
      const isNetworkFailure = !raw || /failed to fetch|networkerror|load failed|err_failed/i.test(raw);
      setError(isNetworkFailure ? '伏笔列表没读到：请求没有到达服务端（网络或接口路径问题）。请点「重试」。' : raw);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSuggest = useCallback(async () => {
    if (!projectId) return;
    setSuggesting(true);
    setSuggestError('');
    try {
      const json = await foreshadowApi.suggest(projectId);
      setSuggestions(json.data || []);
      if ((json.data || []).length === 0) {
        toast.info('暂无建议', '当前大纲暂未产出可建议的伏笔候选。');
      }
    } catch (err) {
      setSuggestError(err instanceof Error ? err.message : '伏笔建议生成失败。');
    } finally {
      setSuggesting(false);
    }
  }, [projectId, toast]);

  const handleConfirmCandidate = useCallback(async (candidate: ForeshadowSuggestion, index: number) => {
    setConfirmingIndex(index);
    try {
      await foreshadowApi.create(projectId, [{
        name: candidate.name,
        description: candidate.description,
        plantChapterId: candidate.plant_chapter_id,
        targetChapterId: candidate.target_chapter_id,
      }]);
      setSuggestions((previous) => previous.filter((_, itemIndex) => itemIndex !== index));
      toast.success('已确认伏笔', `《${candidate.name}》已入库。`);
      await load();
    } catch (err) {
      toast.error('确认失败', err instanceof Error ? err.message : '伏笔入库失败。');
    } finally {
      setConfirmingIndex(null);
    }
  }, [load, projectId, toast]);

  const handleStatusChange = useCallback(async (fid: string, status: ForeshadowStatus) => {
    setUpdatingId(fid);
    try {
      await foreshadowApi.update(projectId, fid, { status });
      toast.success('状态已更新');
      await load();
    } catch (err) {
      toast.error('更新失败', err instanceof Error ? err.message : '状态更新失败。');
    } finally {
      setUpdatingId(null);
    }
  }, [load, projectId, toast]);

  const handleDelete = useCallback(async () => {
    if (!deleteModalId) return;
    const fid = deleteModalId;
    setDeleteModalId(null);
    setUpdatingId(fid);
    try {
      await foreshadowApi.remove(projectId, fid);
      toast.success('已删除伏笔');
      await load();
    } catch (err) {
      toast.error('删除失败', err instanceof Error ? err.message : '删除伏笔失败。');
    } finally {
      setUpdatingId(null);
    }
  }, [deleteModalId, load, projectId, toast]);

  const items = listData?.items || [];
  const alertCount = listData?.alertCount || 0;
  const issues = listData?.consistency.issues || [];

  return (
    <div className="flex-1 overflow-auto" data-testid="foreshadow-panel">
      <div className="mx-auto max-w-4xl px-8 py-8">
        {/* 页面头 */}
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-xl font-bold text-surface-800 dark:text-surface-100">
            <Flag size={22} className="text-primary-600" aria-hidden="true" />
            伏笔账本
          </h2>
          <div className="flex items-center gap-3">
            <span className="hidden text-xs text-surface-400 sm:inline">长程叙事张力：埋设 → 推进 → 回收</span>
            <Button variant="primary" size="sm" loading={suggesting} onClick={() => void handleSuggest()} data-testid="foreshadow-suggest">
              <Sparkles size={14} aria-hidden="true" />
              生成建议
            </Button>
          </div>
        </div>

        {/* 全文问答：直灌全书核对设定/伏笔（建议2：DeepSeek 前缀缓存低成本） */}
        <FulltextAskPanel projectId={projectId} />

        {/* 手动登记伏笔（作者自己写完正文后登记，Q1=A：放在伏笔页顶部） */}
        <ForeshadowManualEntry
          projectId={projectId}
          existing={items}
          chapters={chapterOptions}
          currentChapterId={currentChapterId || ''}
          onChanged={load}
        />

        {/* 超期告警 */}
        {alertCount > 0 && (
          <div
            className="mb-4 flex items-start gap-2 rounded-lg bg-warning-soft px-3 py-2.5 text-xs text-warning dark:bg-warning-soft-dark dark:text-warning-dark"
            data-testid="foreshadow-overdue-alert"
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{alertCount} 条伏笔已超期未回收，请尽快在对应章节完成回收。</span>
          </div>
        )}

        {/* 大纲一致性提示 */}
        {!loading && !error && issues.length > 0 && (
          <div
            className="mb-4 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2.5 dark:bg-warning-soft-dark"
            data-testid="foreshadow-consistency-alert"
          >
            <p className="flex items-center gap-1.5 text-xs font-semibold text-warning dark:text-warning-dark">
              <AlertTriangle size={13} aria-hidden="true" />
              伏笔与大纲存在 {issues.length} 处不一致
            </p>
            <ul className="mt-1.5 space-y-1 text-[11px] leading-relaxed text-warning dark:text-warning-dark">
              {issues.slice(0, 5).map((issue, index) => (
                <li key={`${issue.foreshadowId}-${index}`}>
                  《{issue.name}》：{issue.detail}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* 建议候选 */}
        {suggestions.length > 0 && (
          <section className="mb-6" data-testid="foreshadow-suggestions">
            <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
              <Sparkles size={16} className="text-primary-600" aria-hidden="true" />
              建议候选
              <Badge variant="info">{suggestions.length}</Badge>
            </h3>
            <div className="space-y-3">
              {suggestions.map((candidate, index) => (
                <div key={`${candidate.name}-${index}`} className="card">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold text-surface-800 dark:text-surface-100">{candidate.name}</span>
                        <Badge variant="info">
                          第 {candidate.plant_chapter} 章埋设 → 第 {candidate.target_chapter} 章回收
                        </Badge>
                      </div>
                      <p className="mt-1.5 text-xs leading-relaxed text-surface-500 dark:text-surface-400">
                        {candidate.description || '暂无描述。'}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        variant="primary"
                        size="sm"
                        loading={confirmingIndex === index}
                        onClick={() => void handleConfirmCandidate(candidate, index)}
                        data-testid={`foreshadow-confirm-${index}`}
                      >
                        <Check size={14} aria-hidden="true" />
                        确认
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setSuggestions((previous) => previous.filter((_, itemIndex) => itemIndex !== index))}
                      >
                        忽略
                      </Button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* suggest 错误提示 */}
        {suggestError && (
          <div
            className="mb-4 flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
            role="alert"
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1">{suggestError}</span>
            <Button variant="ghost" size="sm" className="shrink-0" onClick={() => void handleSuggest()}>
              重试
            </Button>
          </div>
        )}

        {/* 已确认伏笔列表 */}
        <section data-testid="foreshadow-list">
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
            <Flag size={16} className="text-primary-600" aria-hidden="true" />
            已确认伏笔
            <Badge variant="neutral">{error && !listData ? '—' : items.length}</Badge>
          </h3>

          {loading && (
            <div className="space-y-3" role="status" aria-live="polite">
              {[0, 1, 2].map((index) => (
                <div key={index} className="h-24 animate-pulse rounded-xl bg-surface-100 dark:bg-surface-700" />
              ))}
              <p className="text-sm text-surface-500">正在加载伏笔…</p>
            </div>
          )}

          {!loading && error && (
            <div
              className="flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
              role="alert"
              data-testid="foreshadow-error-notice"
            >
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0 flex-1">{error}</span>
              <Button variant="ghost" size="sm" className="shrink-0" onClick={() => void load()}>
                重试
              </Button>
            </div>
          )}

          {!loading && !error && items.length === 0 && (
            <div className="flex flex-col items-center gap-1 py-12 text-center" data-testid="foreshadow-empty">
              <Inbox size={40} className="mb-2 text-surface-300" aria-hidden="true" />
              <span className="text-sm font-semibold text-surface-800 dark:text-surface-100">还没有确认的伏笔</span>
              <span className="text-xs text-surface-400">点击「生成建议」让 AI 基于大纲推荐长程伏笔。</span>
            </div>
          )}

          {!loading && !error && items.length > 0 && (
            <div className="space-y-3">
              {items.map((item) => (
                <ForeshadowRow
                  key={item.id}
                  item={item}
                  updating={updatingId === item.id}
                  onStatusChange={handleStatusChange}
                  onDelete={() => setDeleteModalId(item.id)}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      {/* 删除二次确认（Modal，无 window.confirm） */}
      <Modal
        open={deleteModalId !== null}
        onClose={() => setDeleteModalId(null)}
        title="删除伏笔"
        width="sm"
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setDeleteModalId(null)}>
              取消
            </Button>
            <Button variant="error" size="sm" loading={updatingId === deleteModalId} onClick={() => void handleDelete()}>
              <Trash2 size={14} aria-hidden="true" />
              确认删除
            </Button>
          </>
        )}
      >
        <p className="text-sm leading-relaxed text-surface-600 dark:text-surface-300">
          删除后该伏笔将从账本移除，且不再参与后续章节的活跃注入与自动追踪。
        </p>
      </Modal>
    </div>
  );
}
