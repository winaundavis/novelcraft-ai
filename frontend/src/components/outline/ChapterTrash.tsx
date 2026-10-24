import { useEffect, useRef, useState } from 'react';
import { Loader2, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';

import { Modal } from '../common';
import { chapterTrashApi } from '../../services/api';
import type { ChapterTrashBatchRestoreResult, ChapterTrashList, ChapterTrashRecord } from '../../types';
import { useEditorStore } from '../../stores';
import { chapterMutationBlockReason, notifyChapterChanged } from '../editor/chapterEvents';

const reasonLabels: Record<string, string> = {
  outline_removed: '大纲调整移除',
  outline_replaced: '大纲调整移除',
  manual_delete: '手动移入回收站',
  manual_recycle: '手动移入回收站',
  restore_replaced: '恢复章节时替换',
  import_replaced: '导入内容时替换',
  import_merged: '导入增量合并时替换',
};

function removalTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

/**
 * 单次批量请求携带的 id 上限。
 *
 * 后端把单请求的 id 数限制在 500（模型层约束），但回收站条目会随每次导入替换持续增长
 * （实测某个 Beta 项目已有 484 条）。若把「全选」的 id 一次性发出去，只要超过上限，
 * **整个请求**会被 422 拒绝——不是部分成功，而是全选彻底不可用。
 * 因此这里按片发送并在前端汇总结果；分片值取得比后端上限更保守。
 */
const BATCH_CHUNK_SIZE = 200;

function chunkIds(ids: string[]): string[][] {
  const groups: string[][] = [];
  for (let index = 0; index < ids.length; index += BATCH_CHUNK_SIZE) {
    groups.push(ids.slice(index, index + BATCH_CHUNK_SIZE));
  }
  return groups;
}

export default function ChapterTrash({ projectId, onClose, onRestored }: {
  projectId: string;
  onClose: () => void;
  onRestored: (chapterId: string) => void;
}) {
  const [data, setData] = useState<ChapterTrashList | null>(null);
  const [selectedId, setSelectedId] = useState('');
  /** 批量选择（与单条预览的 selectedId 相互独立：预览是「看哪条」，这里是「对哪些动手」）。 */
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [batchResult, setBatchResult] = useState<ChapterTrashBatchRestoreResult | null>(null);
  const [confirmedBatchDelete, setConfirmedBatchDelete] = useState(false);
  const [preview, setPreview] = useState<(ChapterTrashRecord & { content: string }) | null>(null);
  const [targetId, setTargetId] = useState('');
  const [confirmation, setConfirmation] = useState<'replace' | 'delete' | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const pendingRef = useRef(false);
  const mountedRef = useRef(false);
  const target = data?.targets.find((item) => item.chapterId === targetId);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    chapterTrashApi.list(projectId).then((json) => {
      if (!active) return;
      if (!json.data) throw new Error('回收站加载失败，请重试。');
      setData(json.data);
      setTargetId((current) => json.data!.targets.some((item) => item.chapterId === current) ? current : '');
      // 刷新后只保留仍然存在的选择，避免把已经消失的记录带到下一次批量操作里。
      const alive = new Set(json.data.records.map((item) => item.id));
      setCheckedIds((current) => current.filter((id) => alive.has(id)));
    }).catch((cause) => {
      if (active) setError(cause instanceof Error ? cause.message : '回收站加载失败。');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, revision]);

  useEffect(() => {
    setPreview(null);
    if (!selectedId) return;
    let active = true;
    setPreviewLoading(true);
    chapterTrashApi.get(projectId, selectedId).then((json) => {
      if (active) setPreview(json.data || null);
    }).catch((cause) => {
      if (active) setError(cause instanceof Error ? cause.message : '原稿加载失败。');
    }).finally(() => { if (active) setPreviewLoading(false); });
    return () => { active = false; };
  }, [projectId, selectedId]);

  const checkEditor = () => {
    const reason = chapterMutationBlockReason(projectId);
    if (reason) setError(reason);
    return !reason;
  };

  const restore = async (confirmReplace: boolean) => {
    if (!data || !preview || !target || pendingRef.current || !checkEditor()) return;
    pendingRef.current = true;
    setBusy(true);
    setError('');
    try {
      const json = await chapterTrashApi.restore(projectId, preview.id, {
        targetChapterId: target.chapterId,
        outlineHash: data.outlineHash,
        targetContentHash: target.contentHash,
        confirmReplace,
      });
      if (!json.data) throw new Error('恢复未完成，请刷新后重试。');
      // The immutable preview is exactly the body restored by the server.
      // Apply it before closing so a delayed GET cannot leave old prose editable.
      notifyChapterChanged({ projectId, chapterId: json.data.chapterId, action: 'restore', restoredContent: preview.content });
      if (!mountedRef.current || useEditorStore.getState().activeProjectId !== projectId) return;
      const blockedReason = chapterMutationBlockReason(projectId);
      if (blockedReason) {
        setError(`原稿已恢复到“${target.title}”。${blockedReason}处理后重新打开目标章节查看。`);
        setSelectedId('');
        setConfirmation(null);
        setRevision((value) => value + 1);
        return;
      }
      onRestored(json.data.chapterId);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '恢复失败。');
      setConfirmation(null);
    } finally {
      pendingRef.current = false;
      setBusy(false);
    }
  };

  const deleteRecord = async () => {
    if (!preview || pendingRef.current || !checkEditor()) return;
    pendingRef.current = true;
    setBusy(true);
    setError('');
    try {
      await chapterTrashApi.delete(projectId, preview.id);
      setData((current) => current ? { ...current, records: current.records.filter((item) => item.id !== preview.id) } : current);
      setSelectedId('');
      setPreview(null);
      setConfirmation(null);
      notifyChapterChanged({ projectId, action: 'delete-trash' });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '永久删除失败。');
    } finally {
      pendingRef.current = false;
      setBusy(false);
    }
  };

  const allChecked = Boolean(data && data.records.length > 0 && checkedIds.length === data.records.length);

  const toggleChecked = (id: string) => {
    setBatchResult(null);
    setConfirmedBatchDelete(false);
    setCheckedIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  };

  const toggleAll = () => {
    setBatchResult(null);
    setConfirmedBatchDelete(false);
    setCheckedIds(allChecked ? [] : (data?.records.map((item) => item.id) ?? []));
  };

  /**
   * 批量还原：各自回到原章节。冲突由服务端跳过，这里只如实展示「成功几条、跳过几条及原因」，
   * 不重试、不改写服务端给出的原因。选择数超过单请求上限时按片发送并汇总。
   */
  const batchRestore = async () => {
    if (!data || checkedIds.length === 0 || pendingRef.current || !checkEditor()) return;
    const requested = [...checkedIds];
    const outlineHash = data.outlineHash;
    pendingRef.current = true;
    setBusy(true);
    setError('');
    setBatchResult(null);
    try {
      const restored: ChapterTrashBatchRestoreResult['restored'] = [];
      const skipped: ChapterTrashBatchRestoreResult['skipped'] = [];
      for (const ids of chunkIds(requested)) {
        const json = await chapterTrashApi.batchRestore(projectId, ids, outlineHash);
        if (!json.data) throw new Error('批量还原未完成，请刷新后重试。');
        restored.push(...json.data.restored);
        skipped.push(...json.data.skipped);
      }
      setBatchResult({ restored, skipped, outlineHash });
      restored.forEach((item) => {
        notifyChapterChanged({ projectId, chapterId: item.chapterId, action: 'restore' });
      });
      // 只清掉真正还原成功的那些；被跳过的留在选中态，方便作者逐条处理。
      const restoredIds = new Set(restored.map((item) => item.trashId));
      setCheckedIds((current) => current.filter((id) => !restoredIds.has(id)));
      if (restoredIds.has(selectedId)) { setSelectedId(''); setPreview(null); }
      setRevision((value) => value + 1);
      if (restored.length > 0) {
        const first = restored[0];
        if (useEditorStore.getState().activeProjectId === projectId && !chapterMutationBlockReason(projectId)) {
          onRestored(first.chapterId);
        }
      }
    } catch (cause) {
      // 分片之间失败时，前面的片已经落库：如实告知，并保留未处理的选择供重试。
      setError(cause instanceof Error ? cause.message : '批量还原失败。');
    } finally {
      pendingRef.current = false;
      setBusy(false);
    }
  };

  const batchDelete = async () => {
    if (checkedIds.length === 0 || pendingRef.current || !checkEditor()) return;
    const requested = [...checkedIds];
    pendingRef.current = true;
    setBusy(true);
    setError('');
    setBatchResult(null);
    try {
      let removedAny = false;
      for (const ids of chunkIds(requested)) {
        const json = await chapterTrashApi.batchDelete(projectId, ids);
        if (!json.data) throw new Error('批量删除未完成，请刷新后重试。');
        const gone = new Set([...json.data.deleted, ...json.data.missing]);
        removedAny = removedAny || gone.size > 0;
        // 每片成功后立即反映到本地，失败时已删除的部分不会看起来仍在回收站里。
        setData((current) => current
          ? { ...current, records: current.records.filter((item) => !gone.has(item.id)) }
          : current);
        setCheckedIds((current) => current.filter((id) => !gone.has(id)));
        if (gone.has(selectedId)) { setSelectedId(''); setPreview(null); }
      }
      setConfirmedBatchDelete(false);
      if (removedAny) notifyChapterChanged({ projectId, action: 'delete-trash' });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '批量删除失败。');
    } finally {
      pendingRef.current = false;
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={() => { if (!pendingRef.current) onClose(); }} title="章节回收站" width="lg" data-testid="chapter-trash-dialog">
      <div className="mb-4 flex items-start justify-between gap-3">
        <p className="text-xs leading-5 text-surface-500">这里保留已移除的章节原稿。可恢复到当前大纲中的章节；不会自动清空。</p>
        <button className="btn-ghost shrink-0 p-2" aria-label="刷新回收站" disabled={busy || loading} onClick={() => {
          setError(''); setConfirmation(null); setBatchResult(null); setConfirmedBatchDelete(false); setRevision((value) => value + 1);
        }}><RefreshCw size={15} /></button>
      </div>
      {error ? <p role="alert" className="mb-3 rounded-lg bg-error-soft p-3 text-sm text-error dark:bg-error-soft-dark">{error}</p> : null}
      {loading ? <p className="py-8 text-center text-sm text-surface-500"><Loader2 size={16} className="mr-2 inline animate-spin" />正在加载回收站…</p> : null}
      {!loading && data?.records.length === 0 ? <p className="py-10 text-center text-sm text-surface-500">回收站为空</p> : null}
      {!loading && data && data.records.length > 0 ? (
        <div className="space-y-4">
          {/* 批量工具条：多选 → 一键还原 / 一键删除 */}
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 dark:border-surface-700 dark:bg-surface-800/60">
            <button className="btn-ghost text-xs" disabled={busy} data-testid="chapter-trash-select-all" onClick={toggleAll}>
              {allChecked ? '取消全选' : '全选'}
            </button>
            <span className="text-xs text-surface-500" data-testid="chapter-trash-selected-count">已选 {checkedIds.length} 条</span>
            <span className="ml-auto flex flex-wrap items-center gap-2">
              <button className="btn-primary text-xs" disabled={busy || checkedIds.length === 0} data-testid="chapter-trash-batch-restore" onClick={() => void batchRestore()}>
                <RotateCcw size={13} className="mr-1 inline" />一键还原所选
              </button>
              <button className="btn-ghost text-xs text-error" disabled={busy || checkedIds.length === 0} data-testid="chapter-trash-batch-delete" onClick={() => {
                if (checkEditor()) { setError(''); setBatchResult(null); setConfirmedBatchDelete(true); }
              }}><Trash2 size={13} className="mr-1 inline" />一键删除所选</button>
            </span>
          </div>
          <p className="text-xs leading-5 text-surface-500">
            批量还原会把每条记录送回<strong className="font-semibold">它自己的原章节</strong>；原章节已不在大纲、或已有正文的记录会被跳过，不会覆盖正文。
          </p>
          {confirmedBatchDelete ? (
            <div className="space-y-3 rounded-lg border border-error/40 bg-error/10 p-3" role="alertdialog" aria-label="确认批量永久删除">
              <p className="text-sm leading-6">将永久删除选中的 {checkedIds.length} 条原稿，删除后无法恢复。确认继续？</p>
              <div className="flex flex-wrap justify-end gap-2">
                <button className="btn-ghost text-sm" disabled={busy} data-testid="chapter-trash-batch-delete-cancel" onClick={() => setConfirmedBatchDelete(false)}>取消</button>
                <button className="btn-error text-sm" disabled={busy} data-testid="chapter-trash-batch-delete-confirm" onClick={() => void batchDelete()}>
                  {busy ? '处理中…' : `确认永久删除 ${checkedIds.length} 条`}
                </button>
              </div>
            </div>
          ) : null}
          {batchResult ? (
            <div className="space-y-1 rounded-lg border border-surface-200 p-3 text-xs leading-5 dark:border-surface-700" data-testid="chapter-trash-batch-result">
              <p className="font-medium text-success">已还原 {batchResult.restored.length} 条。</p>
              {batchResult.skipped.length > 0 ? (
                <>
                  <p className="font-medium text-warning">跳过 {batchResult.skipped.length} 条：</p>
                  <ul className="space-y-0.5">
                    {batchResult.skipped.map((item) => (
                      <li key={item.trashId} className="break-words text-surface-500">
                        {item.title || '未命名章节'} — {item.message}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </div>
          ) : null}
          <div className="max-h-44 space-y-1 overflow-y-auto rounded-lg border border-surface-200 p-1 dark:border-surface-700">
            {data.records.map((item) => (
              <div key={item.id} className={`flex items-start gap-2 rounded-md px-2 py-1 ${selectedId === item.id ? 'bg-primary-50 dark:bg-primary-900/20' : 'hover:bg-surface-100 dark:hover:bg-surface-700'}`}>
                <input
                  type="checkbox"
                  className="mt-2.5 shrink-0"
                  checked={checkedIds.includes(item.id)}
                  disabled={busy}
                  onChange={() => toggleChecked(item.id)}
                  aria-label={`选择 ${item.title || '未命名章节'}`}
                  data-testid={`chapter-trash-check-${item.id}`}
                />
                <button key={item.id} disabled={busy} data-testid={`chapter-trash-record-${item.id}`} onClick={() => {
                  setSelectedId(item.id); setConfirmation(null); setError('');
                }} className="min-w-0 flex-1 rounded-md px-1 py-1 text-left text-sm">
                  <span className="block break-words font-medium">{item.title || '未命名章节'}</span>
                  <span className="mt-1 block text-xs text-surface-500">{removalTime(item.removedAt)} · {item.wordCount} 字</span>
                </button>
              </div>
            ))}
          </div>
          {previewLoading ? <p className="text-sm text-surface-500">正在加载原稿…</p> : null}
          {!selectedId ? <p className="text-sm text-surface-500">选择一条记录查看原稿。</p> : null}
          {preview && !previewLoading ? (
            <div className="space-y-4">
              <dl className="space-y-1 break-words text-xs leading-5 text-surface-500">
                <div><dt className="inline">原标题：</dt><dd className="inline">{preview.title || '未命名章节'}</dd></div>
                <div><dt className="inline">原位置：</dt><dd className="inline">{preview.originalLocation || '未记录'}</dd></div>
                <div><dt className="inline">移除原因：</dt><dd className="inline">{reasonLabels[preview.reason] || preview.reason}</dd></div>
                <div><dt className="inline">移除时间：</dt><dd className="inline">{removalTime(preview.removedAt)}</dd></div>
              </dl>
              <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-surface-50 p-3 font-sans text-sm leading-7 dark:bg-surface-900" data-testid="chapter-trash-content">{preview.content || '（正文为空）'}</pre>
              <label className="block text-sm font-medium">
                恢复到当前大纲章节
                <select value={targetId} disabled={busy || Boolean(confirmation)} onChange={(event) => setTargetId(event.target.value)} data-testid="chapter-trash-target" className="mt-2 w-full min-w-0 rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm dark:border-surface-600 dark:bg-surface-800">
                  <option value="">请选择目标章节</option>
                  {data.targets.map((item) => <option key={item.chapterId} value={item.chapterId}>{item.location || item.title}{item.hasContent ? '（已有正文）' : '（空白）'}</option>)}
                </select>
              </label>
              {data.targets.length === 0 ? <p className="text-xs text-surface-500">当前大纲没有可恢复的章节，请先在大纲中添加章节。</p> : null}
              {confirmation ? (
                <div className="space-y-3 rounded-lg border border-warning/40 bg-warning/10 p-3" role="alertdialog" aria-label={confirmation === 'replace' ? '确认替换章节正文' : '确认永久删除原稿'}>
                  <p className="break-words text-sm leading-6">{confirmation === 'replace'
                    ? `“${target?.title}”已有正文。确认后将用原稿替换，目标章节的旧正文会移入回收站。`
                    : `永久删除“${preview.title}”后无法恢复。确认删除这份原稿？`}</p>
                  <div className="flex flex-wrap justify-end gap-2">
                    <button className="btn-ghost text-sm" disabled={busy} data-testid="chapter-trash-confirm-cancel" onClick={() => setConfirmation(null)}>取消</button>
                    <button className="btn-primary text-sm" disabled={busy} data-testid={confirmation === 'replace' ? 'chapter-trash-replace-confirm' : 'chapter-trash-delete-confirm'} onClick={() => { void (confirmation === 'replace' ? restore(true) : deleteRecord()); }}>
                      {busy ? '处理中…' : confirmation === 'replace' ? '确认替换并保留旧稿' : '确认永久删除'}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex flex-wrap justify-between gap-3">
                  <button className="btn-ghost text-sm text-error" disabled={busy} data-testid="chapter-trash-delete" onClick={() => {
                    if (checkEditor()) { setError(''); setConfirmation('delete'); }
                  }}><Trash2 size={14} className="mr-1 inline" />永久删除</button>
                  <button className="btn-primary text-sm" disabled={busy || !target} data-testid="chapter-trash-restore" onClick={() => {
                    if (!checkEditor()) return;
                    setError('');
                    if (target?.hasContent) setConfirmation('replace');
                    else void restore(false);
                  }}><RotateCcw size={14} className="mr-1 inline" />恢复到所选章节</button>
                </div>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </Modal>
  );
}
