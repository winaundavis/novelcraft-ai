/**
 * ForeshadowManualEntry — 手写伏笔登记（作者自己写完正文后，把埋下的伏笔登记进账本）
 *
 * 设计约定（产品方 2026-10-06 确认）：
 * - 入口在「伏笔」页顶部（Q1=A）；
 * - 既能结构化填写，也能贴一段自由文字让 AI 整理成草稿、作者核对后再保存（Q2=C）；
 * - 埋设章默认「当前打开的章」，回收章默认「待定」（Q3）；
 * - 可选「同时更新一条已有伏笔」并记下备注与所在章节（Q4=1）——
 *   覆盖「手动改正文不会触发自动追踪」这个盲区。
 *
 * 纪律：AI 整理失败/没认出伏笔时**如实说明并让作者手写**，绝不静默伪造成果；
 * 只有作者点「保存到伏笔账本」才会落库。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Check, FileText, Plus, Sparkles, Trash2 } from 'lucide-react';

import { Button, Input, Select, Textarea, useToast } from '../common';
import { foreshadowApi } from '../../services/api';
import type { Foreshadow, ForeshadowStatus, ForeshadowSuggestion } from '../../types';

export interface ChapterOption {
  id: string;
  order: number;
  title: string;
}

interface DraftRow {
  key: string;
  name: string;
  description: string;
  plantChapterId: string;
  targetChapterId: string;
}

const STATUS_LABELS: Record<ForeshadowStatus, string> = {
  planted: '已埋设',
  progressing: '推进中',
  resolved: '已回收',
  abandoned: '已废弃',
};

/** 与后端 DAO 的 FORWARD_STATUSES 保持一致（非法回退会被后端拒绝）。 */
const FORWARD_STATUSES: Record<ForeshadowStatus, ForeshadowStatus[]> = {
  planted: ['progressing', 'resolved', 'abandoned'],
  progressing: ['resolved', 'abandoned'],
  resolved: [],
  abandoned: [],
};

let rowSeq = 0;
function newRow(plantChapterId: string): DraftRow {
  rowSeq += 1;
  return { key: `row-${rowSeq}`, name: '', description: '', plantChapterId, targetChapterId: '' };
}

function chapterLabel(chapters: ChapterOption[], id: string): string {
  const found = chapters.find((chapter) => chapter.id === id);
  return found ? `第 ${found.order} 章《${found.title}》` : '';
}

export default function ForeshadowManualEntry({
  projectId,
  existing,
  chapters,
  currentChapterId,
  onChanged,
}: {
  projectId: string;
  existing: Foreshadow[];
  chapters: ChapterOption[];
  currentChapterId: string;
  onChanged: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [rows, setRows] = useState<DraftRow[]>(() => [newRow(currentChapterId)]);
  const [draftText, setDraftText] = useState('');
  const [drafting, setDrafting] = useState(false);
  const [draftReason, setDraftReason] = useState('');
  const [draftDegraded, setDraftDegraded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [savedNotice, setSavedNotice] = useState('');
  const [existingPick, setExistingPick] = useState('');
  const [existingStatus, setExistingStatus] = useState<ForeshadowStatus | ''>('');
  const [note, setNote] = useState('');

  const updatable = useMemo(
    () => existing.filter((item) => FORWARD_STATUSES[item.status]?.length > 0),
    [existing],
  );
  const picked = updatable.find((item) => item.id === existingPick) || null;
  const statusOptions = picked ? FORWARD_STATUSES[picked.status] : [];

  // 「埋设章默认 = 当前打开的章」：当前章可能比本组件晚一步就绪（首帧、store 异步恢复、
  // 作者刚打开某章又切回伏笔页）。只补**还完全空白**的行，不动作者已动过的行。
  useEffect(() => {
    if (!currentChapterId) return;
    setRows((previous) =>
      previous.map((row) =>
        row.plantChapterId || row.name.trim() || row.description.trim()
          ? row
          : { ...row, plantChapterId: currentChapterId },
      ),
    );
  }, [currentChapterId]);

  const updateRow = useCallback((key: string, patch: Partial<DraftRow>) => {
    setRows((previous) => previous.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }, []);

  const handleDraft = useCallback(async () => {
    if (!projectId || !draftText.trim()) {
      setDraftReason('请先写下这段内容（本章埋了什么伏笔、怎么埋的都行）。');
      setDraftDegraded(false);
      return;
    }
    setDrafting(true);
    setDraftReason('');
    setDraftDegraded(false);
    try {
      const json = await foreshadowApi.draft(projectId, draftText.trim(), currentChapterId);
      const items: ForeshadowSuggestion[] = json.data?.items || [];
      if (json.data?.degraded) {
        setDraftDegraded(true);
        setDraftReason(json.data.reason || 'AI 暂时不可用，请手动填写下面的字段。');
      } else if (items.length === 0) {
        setDraftReason(json.data?.reason || 'AI 没从这段文字里认出长程伏笔，请手动填写。');
      } else {
        const drafted = items.map((item) => ({
          ...newRow(item.plant_chapter_id || currentChapterId),
          name: item.name || '',
          description: item.description || '',
          targetChapterId: item.target_chapter_id || '',
        }));
        // 丢掉完全空白的占位行，避免出现「AI 结果 + 空行」的困惑
        setRows((previous) => [
          ...previous.filter((row) => row.name.trim() || row.description.trim()),
          ...drafted,
        ]);
        setDraftText('');
        setDraftReason(`已整理出 ${items.length} 条草稿，请核对后再保存。`);
      }
    } catch (err) {
      setDraftDegraded(true);
      setDraftReason(err instanceof Error ? err.message : 'AI 整理失败，请手动填写。');
    } finally {
      setDrafting(false);
    }
  }, [currentChapterId, draftText, projectId]);

  const handleSave = useCallback(async () => {
    const filled = rows.filter((row) => row.name.trim());
    if (!projectId) return;
    if (filled.length === 0) {
      setError('至少填一条伏笔名称（描述与章节可留空）。');
      return;
    }
    setSaving(true);
    setError('');
    setSavedNotice('');
    try {
      await foreshadowApi.create(
        projectId,
        filled.map((row) => ({
          name: row.name.trim(),
          description: row.description.trim(),
          plantChapterId: row.plantChapterId,
          targetChapterId: row.targetChapterId,
        })),
      );
      let notice = `已登记 ${filled.length} 条伏笔`;
      if (picked && existingStatus) {
        const chapterText = chapterLabel(chapters, currentChapterId);
        await foreshadowApi.update(projectId, picked.id, {
          status: existingStatus,
          note: note.trim() || (chapterText ? `作者手动登记（${chapterText}）` : '作者手动登记'),
          chapterId: currentChapterId || '',
        });
        notice += `；《${picked.name}》已标记为「${STATUS_LABELS[existingStatus]}」`;
      }
      setSavedNotice(notice);
      toast.success('已登记伏笔', notice);
      setRows([newRow(currentChapterId)]);
      setNote('');
      setExistingPick('');
      setExistingStatus('');
      setDraftReason('');
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : '登记失败，请重试。');
    } finally {
      setSaving(false);
    }
  }, [chapters, currentChapterId, existingPick, existingStatus, note, onChanged, picked, projectId, rows, toast]);

  return (
    <section className="mb-6 card" data-testid="foreshadow-manual-entry">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
          <FileText size={16} className="text-primary-600" aria-hidden="true" />
          手动登记伏笔
        </h3>
        <span className="text-[11px] text-surface-400">
          自己写完正文后，把埋下的伏笔记在这里；它会进账本，参与后续创作的提醒
        </span>
      </div>

      {/* 草稿行 */}
      <div className="space-y-3">
        {rows.map((row, index) => (
          <div key={row.key} className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
            <div className="flex flex-wrap items-end gap-3">
              <Input
                label="伏笔名称"
                className="w-48"
                value={row.name}
                placeholder="如：剑鞘上的刻痕"
                onChange={(event) => updateRow(row.key, { name: event.target.value })}
                data-testid={`foreshadow-manual-name-${index}`}
              />
              <Select
                label="埋设章"
                className="w-56"
                value={row.plantChapterId}
                onChange={(event) => updateRow(row.key, { plantChapterId: event.target.value })}
                data-testid={`foreshadow-manual-plant-${index}`}
              >
                <option value="">待定</option>
                {chapters.map((chapter) => (
                  <option key={chapter.id} value={chapter.id}>
                    第 {chapter.order} 章《{chapter.title}》
                  </option>
                ))}
              </Select>
              <Select
                label="预计回收章"
                className="w-56"
                value={row.targetChapterId}
                onChange={(event) => updateRow(row.key, { targetChapterId: event.target.value })}
                data-testid={`foreshadow-manual-target-${index}`}
              >
                <option value="">待定</option>
                {chapters.map((chapter) => (
                  <option key={chapter.id} value={chapter.id}>
                    第 {chapter.order} 章《{chapter.title}》
                  </option>
                ))}
              </Select>
              <Button
                variant="ghost"
                size="sm"
                className="text-error"
                disabled={rows.length <= 1}
                onClick={() => setRows((previous) => previous.filter((item) => item.key !== row.key))}
                data-testid={`foreshadow-manual-remove-${index}`}
              >
                <Trash2 size={14} aria-hidden="true" />
                删掉这条
              </Button>
            </div>
            <Textarea
              label="伏笔说明"
              className="mt-2"
              rows={2}
              value={row.description}
              placeholder="埋设方式 / 暗示内容 / 预期读者反应（可留空）"
              onChange={(event) => updateRow(row.key, { description: event.target.value })}
              data-testid={`foreshadow-manual-description-${index}`}
            />
          </div>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setRows((previous) => [...previous, newRow(currentChapterId)])}
          data-testid="foreshadow-manual-add-row"
        >
          <Plus size={14} aria-hidden="true" />
          再加一条
        </Button>
        {currentChapterId && chapters.some((chapter) => chapter.id === currentChapterId) && (
          <span className="text-[11px] text-surface-400">
            新条目的埋设章默认 = 当前打开的{chapterLabel(chapters, currentChapterId)}
          </span>
        )}
      </div>

      {/* AI 整理 */}
      <div className="mt-4 rounded-lg bg-surface-50 p-3 dark:bg-surface-800/40">
        <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-surface-700 dark:text-surface-200">
          <Sparkles size={13} className="text-primary-600" aria-hidden="true" />
          懒得填字段？把这段内容贴进来，让 AI 整理成伏笔（会消耗 1 次 AI 调用）
        </p>
        <Textarea
          rows={3}
          value={draftText}
          placeholder="例如：本章主角注意到剑鞘内侧刻着半个名字，他没多想；这个名字会在后面牵出前任剑主。"
          onChange={(event) => setDraftText(event.target.value)}
          data-testid="foreshadow-manual-text"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            loading={drafting}
            disabled={!draftText.trim()}
            onClick={() => void handleDraft()}
            data-testid="foreshadow-manual-draft"
          >
            <Sparkles size={14} aria-hidden="true" />
            让 AI 整理
          </Button>
          <span className="text-[11px] text-surface-400">整理结果不会自动入库，你先核对再保存</span>
        </div>
        {draftReason && (
          <p
            className={`mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed ${
              draftDegraded
                ? 'text-warning dark:text-warning-dark'
                : 'text-surface-500 dark:text-surface-400'
            }`}
            data-testid="foreshadow-manual-draft-reason"
          >
            {draftDegraded && <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden="true" />}
            {draftReason}
          </p>
        )}
      </div>

      {/* 顺便更新一条已有伏笔（手改正文后自动追踪不会触发，这里补记） */}
      {updatable.length > 0 && (
        <div className="mt-4 rounded-lg border border-surface-200 p-3 dark:border-surface-700">
          <p className="mb-2 text-xs font-semibold text-surface-700 dark:text-surface-200">
            顺便更新一条已有伏笔（可选）——你手改正文时自动追踪不会跑，这里补记
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <Select
              label="已有伏笔"
              className="w-64"
              value={existingPick}
              onChange={(event) => {
                setExistingPick(event.target.value);
                setExistingStatus('');
              }}
              data-testid="foreshadow-manual-existing"
            >
              <option value="">不更新</option>
              {updatable.map((item) => (
                <option key={item.id} value={item.id}>
                  《{item.name}》（{STATUS_LABELS[item.status]}）
                </option>
              ))}
            </Select>
            <Select
              label="改成"
              className="w-32"
              value={existingStatus}
              disabled={!picked}
              onChange={(event) => setExistingStatus(event.target.value as ForeshadowStatus)}
              data-testid="foreshadow-manual-existing-status"
            >
              <option value="">请选择</option>
              {statusOptions.map((status) => (
                <option key={status} value={status}>
                  {STATUS_LABELS[status]}
                </option>
              ))}
            </Select>
            <Input
              label="备注"
              className="w-64"
              value={note}
              placeholder="这一章你把它怎么了（可留空）"
              onChange={(event) => setNote(event.target.value)}
              data-testid="foreshadow-manual-note"
            />
          </div>
        </div>
      )}

      {error && (
        <p
          className="mt-3 rounded-lg bg-error-soft px-3 py-2 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
          data-testid="foreshadow-manual-error"
        >
          {error}
        </p>
      )}
      {savedNotice && (
        <p
          className="mt-3 flex items-center gap-1.5 rounded-lg bg-success-soft px-3 py-2 text-xs text-success dark:bg-success-soft-dark dark:text-success-dark"
          data-testid="foreshadow-manual-success"
        >
          <Check size={13} aria-hidden="true" />
          {savedNotice}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          loading={saving}
          onClick={() => void handleSave()}
          data-testid="foreshadow-manual-save"
        >
          保存到伏笔账本
        </Button>
        <span className="text-[11px] text-surface-400">
          共 {rows.filter((row) => row.name.trim()).length} 条待登记
        </span>
      </div>
    </section>
  );
}
