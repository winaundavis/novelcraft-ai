/**
 * ReferenceAnalysis — 「参考分析」页：参考书档案（作者的个人素材资产）
 *
 * 产品定位（产品方 2026-10-06 确认，方案 C）：
 * - 把**别人的作品**拆成结构化档案（技法规则 + 本地实测指标 + 拆解报告），存下来当长期资产；
 * - 与「文风档案」严格分开：**参考档案不会改变你项目的文风画像**，也不参与文风过渡；
 * - **删除了旧的「风格量化对比」**——那一栏的数字是模型/兜底凭空生成的（没有任何真实来源），
 *   属于造假。现在页面上的指标全部来自后端**确定性统计**（同一段文本算出来的真值）。
 * - 只有「技法规则」用一次 AI 调用；AI 不可用时如实显示原因，并保留按统计生成的规则。
 * - P3b（2026-10-06）：可**同时启用多份**参考，每份给一个**强度**——强度的真实含义只是
 *   「提示词预算不够时先保留谁」，不是"像不像"的权重（预算不足先丢掉低强度的）。
 *     标签与搜索是纯前端筛选，不发额外请求（素材库大起来要靠它）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  BookMarked,
  FileText,
  Info,
  Library,
  PenLine,
  RefreshCw,
  Search,
  Sparkles,
  Tag,
  Trash2,
  Upload,
} from 'lucide-react';

import { Badge, Button, Input, Modal, Select, Textarea, useToast } from '../common';
import { referenceWorkApi } from '../../services/api';
import type { ReferenceActiveItem, ReferenceRule, ReferenceWork, ReferenceWorkSummary } from '../../types';

/** 参考强度：界面上三档，落到后端的 weight 数字（1-10）。 */
const WEIGHT_OPTIONS: Array<{ label: string; value: number }> = [
  { label: '低', value: 3 },
  { label: '中', value: 6 },
  { label: '高', value: 9 },
];
const DEFAULT_WEIGHT = 6;

function weightLabel(weight: number): string {
  const hit = WEIGHT_OPTIONS.find((option) => option.value === weight);
  return hit ? hit.label : String(weight);
}

/** 把「逗号/顿号分隔的字符串」拆成标签数组（与后端 sanitize_tags 同一套分隔符）。 */
function splitTagInput(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

const RULE_GROUPS: Array<{ key: keyof ReferenceWork; label: string }> = [
  { key: 'narrativeRules', label: '叙事技法' },
  { key: 'languageRules', label: '语言习惯' },
  { key: 'rhythmRules', label: '节奏组织' },
  { key: 'characterRules', label: '人物处理' },
  { key: 'antiAiRules', label: '应当避免' },
];

/** 本地实测指标的中文名（后端确定性统计，非模型生成）。 */
const METRIC_LABELS: Array<{ key: string; label: string; suffix?: string; percent?: boolean }> = [
  { key: 'avgSentenceLength', label: '平均句长', suffix: ' 字' },
  { key: 'dialogueRatio', label: '对话占比', percent: true },
  { key: 'descriptionDensity', label: '描写密度', percent: true },
  { key: 'paragraphShortRatio', label: '短段占比', percent: true },
  { key: 'totalParagraphs', label: '段落数' },
];

function metricText(metrics: Record<string, number>, item: { key: string; suffix?: string; percent?: boolean }): string {
  const value = Number(metrics?.[item.key]);
  if (!Number.isFinite(value)) return '—';
  if (item.percent) return `${Math.round(value * 100)}%`;
  return `${value}${item.suffix || ''}`;
}

function ruleLines(rules: unknown): ReferenceRule[] {
  if (!Array.isArray(rules)) return [];
  return rules
    .map((entry) => {
      if (typeof entry === 'string') return { rule: entry } as ReferenceRule;
      const rule = String((entry as ReferenceRule)?.rule || '').trim();
      if (!rule) return null;
      return { rule, evidence: String((entry as ReferenceRule)?.evidence || '').trim() || undefined };
    })
    .filter((entry): entry is ReferenceRule => entry !== null);
}

export default function ReferenceAnalysis({
  projectId,
  projectName = '',
}: {
  projectId: string;
  /** 当前项目名：用于只在「跳项目」的档案上标注来源，避免自己项目的档案也挂个「来自」 */
  projectName?: string;
}) {
  const toast = useToast();
  const [items, setItems] = useState<ReferenceWorkSummary[]>([]);
  const [activeItems, setActiveItems] = useState<ReferenceActiveItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // 素材库筛选（纯前端，不发请求）
  const [keyword, setKeyword] = useState('');
  const [tagFilter, setTagFilter] = useState<string[]>([]);

  const [name, setName] = useState('');
  const [sourceNote, setSourceNote] = useState('');
  const [tagsInput, setTagsInput] = useState('');
  const [text, setText] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const [fileNotice, setFileNotice] = useState('');
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [detail, setDetail] = useState<ReferenceWork | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ReferenceWorkSummary | null>(null);
  const [renameTarget, setRenameTarget] = useState<ReferenceWorkSummary | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [tagTarget, setTagTarget] = useState<ReferenceWorkSummary | null>(null);
  const [tagValue, setTagValue] = useState('');
  const [showRawText, setShowRawText] = useState(false);

  /** 强度最高的那份（兼容旧的「当前启用」单一语义，用于徽标与文案）。 */
  const activeId = activeItems[0]?.id || '';
  const activeWeightOf = useCallback(
    (referenceId: string) => activeItems.find((entry) => entry.id === referenceId)?.weight ?? null,
    [activeItems],
  );

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError('');
    try {
      const json = await referenceWorkApi.list(projectId);
      setItems(json.data?.items || []);
      setActiveItems(json.data?.activeReferences || []);
    } catch (err) {
      const raw = err instanceof Error ? err.message : '';
      const isNetwork = !raw || /failed to fetch|networkerror|load failed|err_failed/i.test(raw);
      setError(isNetwork ? '参考档案列表没读到：请求没有到达服务端。请点「重试」。' : raw);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handlePickFile = useCallback(async (file: File | undefined) => {
    if (!file) return;
    try {
      const content = await file.text();
      setText(content);
      setFileNotice(`已读取《${file.name}》共 ${content.length.toLocaleString()} 字（可直接编辑后再建档）`);
      setName((previous) => previous.trim() || file.name.replace(/\.(txt|md|markdown)$/i, '').slice(0, 60));
      setCreateError('');
    } catch {
      setFileNotice('');
      setCreateError('文件读取失败（可能是编码问题）。请改用粘贴，或先转成 UTF-8 的 .txt。');
    }
  }, []);

  const handleCreate = useCallback(async () => {
    if (!projectId) return;
    const payloadText = text.trim();
    if (!payloadText) {
      setCreateError('请先粘贴参考文本，或上传 .txt / .md 文件。');
      return;
    }
    setCreating(true);
    setCreateError('');
    try {
      const json = await referenceWorkApi.create(projectId, {
        name: name.trim(),
        text: payloadText,
        sourceNote: sourceNote.trim(),
        tags: splitTagInput(tagsInput),
      });
      const created = json.data;
      setItems((previous) => [created, ...previous]);
      setDetail(created);
      setShowRawText(false);
      setName('');
      setSourceNote('');
      setTagsInput('');
      setText('');
      setFileNotice('');
      toast.success(
        '已建立参考档案',
        created.status === 'degraded'
          ? '指标已按本地统计算好；AI 拆解没成功，档案里保留了统计规则。'
          : `《${created.name}》已拆解并入库。`,
      );
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : '建档失败，请重试。');
    } finally {
      setCreating(false);
    }
  }, [name, projectId, sourceNote, tagsInput, text, toast]);

  const openDetail = useCallback(async (item: ReferenceWorkSummary) => {
    setDetailLoading(true);
    setShowRawText(false);
    try {
      const json = await referenceWorkApi.get(projectId, item.id);
      setDetail(json.data);
    } catch (err) {
      toast.error('读取档案失败', err instanceof Error ? err.message : '请重试。');
    } finally {
      setDetailLoading(false);
    }
  }, [projectId, toast]);

  const handleReanalyze = useCallback(async (item: ReferenceWorkSummary) => {
    setBusyId(item.id);
    try {
      const json = await referenceWorkApi.reanalyze(projectId, item.id);
      setItems((previous) => previous.map((entry) => (entry.id === item.id ? { ...entry, ...json.data } : entry)));
      setDetail((previous) => (previous?.id === item.id ? json.data : previous));
      toast.success(
        '已重新拆解',
        json.data.status === 'degraded'
          ? '这次 AI 仍然不可用，规则仍是按本地统计生成的。'
          : '技法规则已更新。',
      );
    } catch (err) {
      toast.error('重新拆解失败', err instanceof Error ? err.message : '请重试。');
    } finally {
      setBusyId(null);
    }
  }, [projectId, toast]);

  /** 提交一份新的启用集合（后端是**整体替换**；任何一条不是自己的会被整体拒绝）。 */
  const applyActive = useCallback(async (nextItems: ReferenceActiveItem[], message: string, detailText: string) => {
    try {
      const json = await referenceWorkApi.setActive(
        projectId,
        nextItems.map((entry) => ({ referenceId: entry.id, weight: entry.weight })),
      );
      setActiveItems(json.data?.activeReferences || []);
      toast.success(message, detailText);
    } catch (err) {
      toast.error('设置失败', err instanceof Error ? err.message : '请重试。');
      void load(); // 失败时拉回真实状态，避免界面和后端不一致
    }
  }, [load, projectId, toast]);

  const handleToggleActive = useCallback(async (item: ReferenceWorkSummary) => {
    const existing = activeItems.find((entry) => entry.id === item.id);
    setBusyId(item.id);
    try {
      if (existing) {
        await applyActive(
          activeItems.filter((entry) => entry.id !== item.id),
          '已取消创作参考',
          `以后的创作不再带上《${item.name}》。`,
        );
      } else {
        await applyActive(
          [...activeItems, { id: item.id, weight: DEFAULT_WEIGHT, name: item.name }],
          '已设为创作参考',
          `以后写正文时，AI 会参考《${item.name}》的写法（只作表达参考，不覆盖你的文风与设定）。`,
        );
      }
    } finally {
      setBusyId(null);
    }
  }, [activeItems, applyActive]);

  const handleChangeWeight = useCallback(async (item: ReferenceWorkSummary, weight: number) => {
    setBusyId(item.id);
    try {
      await applyActive(
        activeItems.map((entry) => (entry.id === item.id ? { ...entry, weight } : entry)),
        '已调整参考强度',
        `《${item.name}》强度设为「${weightLabel(weight)}」（${weight}）。`,
      );
    } finally {
      setBusyId(null);
    }
  }, [activeItems, applyActive]);

  const handleRename = useCallback(async () => {
    if (!renameTarget) return;
    const target = renameTarget;
    setRenameTarget(null);
    setBusyId(target.id);
    try {
      const json = await referenceWorkApi.update(projectId, target.id, { name: renameValue });
      setItems((previous) => previous.map((entry) => (entry.id === target.id ? { ...entry, ...json.data } : entry)));
      setDetail((previous) => (previous?.id === target.id ? json.data : previous));
      toast.success('已改名');
    } catch (err) {
      toast.error('改名失败', err instanceof Error ? err.message : '请重试。');
    } finally {
      setBusyId(null);
    }
  }, [projectId, renameTarget, renameValue, toast]);

  const handleSaveTags = useCallback(async () => {
    if (!tagTarget) return;
    const target = tagTarget;
    setTagTarget(null);
    setBusyId(target.id);
    try {
      const json = await referenceWorkApi.update(projectId, target.id, {
        tags: splitTagInput(tagValue),
      });
      setItems((previous) => previous.map((entry) => (entry.id === target.id ? { ...entry, ...json.data } : entry)));
      setDetail((previous) => (previous?.id === target.id ? json.data : previous));
      toast.success('已保存标签', json.data?.tags?.length ? `标签：${json.data.tags.join('、')}` : '标签已清空。');
    } catch (err) {
      toast.error('保存标签失败', err instanceof Error ? err.message : '请重试。');
    } finally {
      setBusyId(null);
    }
  }, [projectId, tagTarget, tagValue, toast]);

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setDeleteTarget(null);
    setBusyId(target.id);
    try {
      await referenceWorkApi.remove(projectId, target.id);
      setItems((previous) => previous.filter((entry) => entry.id !== target.id));
      // 删的是当前启用项 → 后端会一并清掉指向它的绑定，这里同步本地集合
      setActiveItems((previous) => previous.filter((entry) => entry.id !== target.id));
      setDetail((previous) => (previous?.id === target.id ? null : previous));
      toast.success('已删除参考档案');
    } catch (err) {
      toast.error('删除失败', err instanceof Error ? err.message : '请重试。');
    } finally {
      setBusyId(null);
    }
  }, [deleteTarget, projectId, toast]);

  // ── 素材库筛选：搜索 + 标签（纯前端） ──
  const allTags = useMemo(() => {
    const pool = new Set<string>();
    items.forEach((item) => (item.tags || []).forEach((tag) => pool.add(tag)));
    return Array.from(pool).sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
  }, [items]);

  const visibleItems = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return items.filter((item) => {
      const tags = item.tags || [];
      const hitKeyword =
        !needle ||
        item.name.toLowerCase().includes(needle) ||
        (item.sourceNote || '').toLowerCase().includes(needle) ||
        tags.some((tag) => tag.toLowerCase().includes(needle));
      // 多选标签 = 「同时具备」的收敛筛选（写作者找素材时更符合直觉）
      const hitTags = tagFilter.length === 0 || tagFilter.every((tag) => tags.includes(tag));
      return hitKeyword && hitTags;
    });
  }, [items, keyword, tagFilter]);

  const filtering = keyword.trim().length > 0 || tagFilter.length > 0;

  const toggleTagFilter = useCallback((tag: string) => {
    setTagFilter((previous) =>
      previous.includes(tag) ? previous.filter((entry) => entry !== tag) : [...previous, tag],
    );
  }, []);

  const detailRuleGroups = useMemo(
    () =>
      RULE_GROUPS.map((group) => ({
        label: group.label,
        lines: ruleLines(detail ? detail[group.key] : []),
      })),
    [detail],
  );

  return (
    <div className="flex-1 overflow-auto" data-testid="reference-panel">
      <div className="mx-auto max-w-4xl p-8">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-xl font-bold text-surface-800 dark:text-surface-100">
            <Library size={22} className="text-primary-600" aria-hidden="true" />
            参考书档案
          </h2>
          <span className="text-xs text-surface-400">拆解别人的写法 · 沉淀成你自己的素材库</span>        </div>

        {/* 边界说明：这一句是产品底线，别删 */}
        <div
          className="mb-6 flex items-start gap-2 rounded-lg bg-surface-50 px-3 py-2.5 text-xs leading-relaxed text-surface-500 dark:bg-surface-800/40 dark:text-surface-400"
          data-testid="reference-scope-note"
        >
          <Info size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>
            这里是**别人的作品**的写法档案：指标由本地统计真算，技法规则由 AI 拆解。
            <strong className="font-semibold">它是你的个人素材库（所有书共享）、不会改变你项目的文风画像，
            也不会自动套用到你的正文</strong>
            —— 除非你**显式**把某份标为「创作参考」（默认不启用）。
          </span>
        </div>

        {/* 建档 */}
        <section className="card mb-6 space-y-3" data-testid="reference-create">
          <div className="flex items-center gap-2">
            <BookMarked size={18} className="text-primary-600" aria-hidden="true" />
            <h3 className="text-sm font-semibold text-surface-800 dark:text-surface-100">建立参考档案</h3>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <Input
              label="档案名（留空则用首行）"
              className="w-64"
              value={name}
              placeholder="如：夜雨（短篇范例）"
              onChange={(event) => setName(event.target.value)}
              data-testid="reference-name-input"
            />
            <Input
              label="来源备注"
              className="w-64"
              value={sourceNote}
              placeholder="如：某短篇集第 3 篇"
              onChange={(event) => setSourceNote(event.target.value)}
              data-testid="reference-source-input"
            />
            <Input
              label="标签（逗号分隔，便于日后检索）"
              className="w-64"
              value={tagsInput}
              placeholder="如：对话流、短句、悬疑"
              onChange={(event) => setTagsInput(event.target.value)}
              data-testid="reference-tags-input"
            />
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.md,.markdown,text/plain,text/markdown"
              className="hidden"
              data-testid="reference-file-input"
              onChange={(event) => void handlePickFile(event.target.files?.[0])}
            />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              data-testid="reference-upload-button"
            >
              <Upload size={14} aria-hidden="true" />
              上传 .txt / .md
            </Button>
          </div>
          {fileNotice && (
            <p className="text-[11px] text-surface-500 dark:text-surface-400" data-testid="reference-file-notice">
              {fileNotice}
            </p>
          )}
          <Textarea
            rows={6}
            value={text}
            placeholder="在这里粘贴参考小说文本（片段或整本都行；整本建议上传 .txt）"
            onChange={(event) => setText(event.target.value)}
            data-testid="reference-text-input"
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[11px] text-surface-400">
              已输入 {text.trim().length.toLocaleString()} 字 · 指标本地统计（不花额度），技法规则用 1 次 AI 调用
            </span>
            <Button
              variant="primary"
              size="sm"
              loading={creating}
              disabled={!text.trim()}
              onClick={() => void handleCreate()}
              data-testid="reference-create-button"
            >
              <Sparkles size={14} aria-hidden="true" />
              建立档案
            </Button>
          </div>
          {createError && (
            <p
              className="rounded-lg bg-error-soft px-3 py-2 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
              data-testid="reference-create-error"
            >
              {createError}
            </p>
          )}
        </section>

        {/* 档案列表 */}
        <section data-testid="reference-list">
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
            <Library size={16} className="text-primary-600" aria-hidden="true" />
            我的参考档案
            <Badge variant="neutral" data-testid="reference-count">
              {filtering ? `${visibleItems.length}/${items.length}` : items.length}
            </Badge>
          </h3>

          {/* 当前启用（可多份；按强度从高到低） */}
          <div
            className="mb-4 rounded-lg border border-surface-200 px-3 py-2.5 dark:border-surface-700"
            data-testid="reference-active-summary"
          >
            <p className="mb-1.5 text-[11px] font-medium text-surface-500 dark:text-surface-400">
              当前启用为创作参考（{activeItems.length} 份）
            </p>
            {activeItems.length === 0 ? (
              <p className="text-[11px] text-surface-400" data-testid="reference-active-empty">
                没有启用任何参考 —— 默认就是不用。需要时才把某份标为「创作参考」。
              </p>
            ) : (
              <>
                <ul className="space-y-1 text-xs text-surface-700 dark:text-surface-300">
                  {activeItems.map((entry, index) => (
                    <li key={entry.id} data-testid={`reference-active-entry-${entry.id}`}>
                      {index + 1}. 《{entry.name || entry.id}》· 强度「{weightLabel(entry.weight)}」
                    </li>
                  ))}
                </ul>
                <p className="mt-1.5 text-[11px] text-surface-400">
                  强度只表示优先级：提示词预算不够时，会先丢掉强度低的那些（高强度的先保留）。
                </p>
              </>
            )}
          </div>

          {/* 素材库检索：纯前端，不发请求 */}
          {(items.length > 0 || filtering) && (
            <div className="mb-4 space-y-2" data-testid="reference-filters">
              <div className="flex flex-wrap items-end gap-3">
                <Input
                  label="搜索素材库"
                  className="w-64"
                  value={keyword}
                  placeholder="按档案名 / 来源备注 / 标签搜"
                  onChange={(event) => setKeyword(event.target.value)}
                  data-testid="reference-search-input"
                />
                {filtering && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setKeyword('');
                      setTagFilter([]);
                    }}
                    data-testid="reference-clear-filters"
                  >
                    <Trash2 size={14} aria-hidden="true" />
                    清除筛选
                  </Button>
                )}
              </div>
              {allTags.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5" data-testid="reference-tag-filter">
                  <span className="mr-1 inline-flex items-center gap-1 text-[11px] text-surface-400">
                    <Tag size={12} aria-hidden="true" />
                    标签
                  </span>
                  {allTags.map((tag) => {
                    const on = tagFilter.includes(tag);
                    return (
                      <button
                        key={tag}
                        type="button"
                        aria-pressed={on}
                        onClick={() => toggleTagFilter(tag)}
                        data-testid={`reference-tag-chip-${tag}`}
                        className={
                          on
                            ? 'rounded-full border border-primary-500 bg-primary-50 px-2 py-0.5 text-[11px] text-primary-700 dark:bg-primary-900/40 dark:text-primary-200'
                            : 'rounded-full border border-surface-200 px-2 py-0.5 text-[11px] text-surface-500 hover:border-primary-400 dark:border-surface-700 dark:text-surface-400'
                        }
                      >
                        {tag}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {error && (
            <div
              className="mb-3 flex items-center justify-between gap-3 rounded-lg bg-error-soft px-3 py-2 dark:bg-error-soft-dark"
              data-testid="reference-error"
            >
              <span className="text-xs text-error dark:text-error-dark">{error}</span>
              <Button variant="ghost" size="sm" onClick={() => void load()} data-testid="reference-retry">
                重试
              </Button>
            </div>
          )}

          {!loading && items.length === 0 && !error && (
            <p className="py-8 text-center text-xs text-surface-400" data-testid="reference-empty">
              还没有参考档案。粘贴一段你欣赏的文字，建立第一份档案。
              <br />
              （素材库按作者存，所有书共享；在本页建档的档案，在别的书里也能看到与启用）
            </p>
          )}

          {!loading && items.length > 0 && visibleItems.length === 0 && (
            <p className="py-8 text-center text-xs text-surface-400" data-testid="reference-no-match">
              没有符合条件的档案。换个关键词，或点「清除筛选」。
            </p>
          )}

          <div className="space-y-3">
            {visibleItems.map((item) => (
              <div key={item.id} className="card" data-testid={`reference-item-${item.id}`}>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-surface-800 dark:text-surface-100">{item.name}</span>
                      {activeWeightOf(item.id) !== null && (
                        <Badge variant="info" data-testid={`reference-active-badge-${item.id}`}>
                          正在参考 · 强度「{weightLabel(activeWeightOf(item.id) as number)}」
                        </Badge>
                      )}
                      <Badge
                        variant={item.status === 'degraded' ? 'warning' : 'success'}
                        data-testid={`reference-status-${item.id}`}
                      >
                        {item.status === 'degraded' ? '统计降级' : item.llmUsed ? 'AI 已拆解' : '已建档'}
                      </Badge>
                      <Badge variant="neutral">{item.charCount.toLocaleString()} 字</Badge>
                      {item.sampleChars < item.charCount && (
                        <Badge variant="info">采样 {item.sampleChars.toLocaleString()} 字</Badge>
                      )}
                      {item.truncated && (
                        <Badge variant="warning">原文只存了前 {item.storedChars.toLocaleString()} 字</Badge>
                      )}
                    </div>
                    {item.sourceProjectName && item.sourceProjectName !== projectName && (
                      <p className="mt-1 text-[11px] text-surface-400" data-testid={`reference-source-${item.id}`}>
                        来自：《{item.sourceProjectName}》
                      </p>
                    )}
                    {item.sourceNote && (
                      <p className="mt-1 text-[11px] text-surface-400">来源备注：{item.sourceNote}</p>
                    )}
                    {item.tags.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5" data-testid={`reference-tags-${item.id}`}>
                        {item.tags.map((tag) => (
                          <span
                            key={tag}
                            className="rounded-full bg-surface-100 px-2 py-0.5 text-[11px] text-surface-500 dark:bg-surface-800 dark:text-surface-400"
                          >
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                    {item.status === 'degraded' && item.statusReason && (
                      <p
                        className="mt-1 text-[11px] text-warning dark:text-warning-dark"
                        data-testid={`reference-reason-${item.id}`}
                      >
                        {item.statusReason}
                      </p>
                    )}
                    {activeWeightOf(item.id) !== null && (
                      <p className="mt-1 text-[11px] text-primary-600" data-testid={`reference-inject-note-${item.id}`}>
                        注入到提示词的措辞：「参考写法（来自《{item.name}》，仅作表达参考，不覆盖本书既有风格与设定）」
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-2">
                    {activeWeightOf(item.id) !== null && (
                      <Select
                        className="w-20"
                        aria-label={`《${item.name}》的参考强度`}
                        value={String(activeWeightOf(item.id))}
                        disabled={busyId === item.id}
                        onChange={(event) => void handleChangeWeight(item, Number(event.target.value))}
                        data-testid={`reference-weight-${item.id}`}
                      >
                        {WEIGHT_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </Select>
                    )}
                    <Button
                      variant={activeWeightOf(item.id) !== null ? 'secondary' : 'ghost'}
                      size="sm"
                      loading={busyId === item.id}
                      onClick={() => void handleToggleActive(item)}
                      data-testid={`reference-activate-${item.id}`}
                    >
                      <BookMarked size={14} aria-hidden="true" />
                      {activeWeightOf(item.id) !== null ? '取消参考' : '设为创作参考'}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busyId === item.id}
                      onClick={() => {
                        setTagTarget(item);
                        setTagValue((item.tags || []).join('、'));
                      }}
                      data-testid={`reference-edit-tags-${item.id}`}
                    >
                      <Tag size={14} aria-hidden="true" />
                      标签
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void openDetail(item)}
                      data-testid={`reference-view-${item.id}`}
                    >
                      <FileText size={14} aria-hidden="true" />
                      查看报告
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busyId === item.id}
                      onClick={() => {
                        setRenameTarget(item);
                        setRenameValue(item.name);
                      }}
                      data-testid={`reference-rename-${item.id}`}
                    >
                      <PenLine size={14} aria-hidden="true" />
                      改名
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      loading={busyId === item.id}
                      onClick={() => void handleReanalyze(item)}
                      data-testid={`reference-reanalyze-${item.id}`}
                    >
                      <RefreshCw size={14} aria-hidden="true" />
                      重新拆解
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-error"
                      disabled={busyId === item.id}
                      onClick={() => setDeleteTarget(item)}
                      data-testid={`reference-delete-${item.id}`}
                    >
                      <Trash2 size={14} aria-hidden="true" />
                      删除
                    </Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* 报告 */}
        {detailLoading && <p className="mt-6 text-xs text-surface-400">正在读取档案…</p>}
        {detail && !detailLoading && (
          <section className="mt-6 space-y-4" data-testid="reference-report">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-surface-800 dark:text-surface-100">
                《{detail.name}》拆解报告
              </h3>
              <Button variant="ghost" size="sm" onClick={() => setDetail(null)} data-testid="reference-close-report">
                收起
              </Button>
            </div>

            {detail.status === 'degraded' && (
              <p className="rounded-lg bg-warning-soft px-3 py-2 text-xs text-warning dark:bg-warning-soft-dark dark:text-warning-dark">
                {detail.statusReason || 'AI 拆解未完成，以下规则来自本地统计。'}
              </p>
            )}

            {/* 本地实测指标（不再是模型编的对比数字） */}
            <div className="card" data-testid="reference-metrics">
              <h4 className="mb-3 text-sm font-semibold text-surface-800 dark:text-surface-100">本地实测指标</h4>
              <p className="mb-3 text-[11px] text-surface-400">
                由后端对参考文本做确定性统计得出（同一段文本结果固定，不经过模型）。
                {detail.sampleChars < detail.charCount
                  ? `本次分析取用了其中 ${detail.sampleChars.toLocaleString()} 字（全文 ${detail.charCount.toLocaleString()} 字）。`
                  : '本次分析了全文。'}
              </p>
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3">
                {METRIC_LABELS.map((metric) => (
                  <div key={metric.key} className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="text-surface-500 dark:text-surface-400">{metric.label}</span>
                    <span className="font-medium text-surface-800 dark:text-surface-100">
                      {metricText(detail.metrics, metric)}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {detail.report && (
              <div className="card">
                <h4 className="mb-2 text-sm font-semibold text-surface-800 dark:text-surface-100">拆解结论</h4>
                <p className="whitespace-pre-wrap text-xs leading-relaxed text-surface-600 dark:text-surface-400">
                  {detail.report}
                </p>
              </div>
            )}

            {detailRuleGroups.filter((group) => group.lines.length > 0).map((group) => (
              <div className="card" key={group.label} data-testid={`reference-rules-${group.label}`}>
                <h4 className="mb-2 text-sm font-semibold text-surface-800 dark:text-surface-100">{group.label}</h4>
                <ul className="space-y-1.5 text-xs leading-relaxed text-surface-600 dark:text-surface-400">
                  {group.lines.map((line, index) => (
                    <li key={`${group.label}-${index}`}>
                      · {line.rule}
                      {line.evidence && <span className="text-surface-400">（例：{line.evidence}）</span>}
                    </li>
                  ))}
                </ul>
              </div>
            ))}

            {(detail.strengths.length > 0 || detail.weaknesses.length > 0) && (
              <div className="card grid gap-3 sm:grid-cols-2">
                {detail.strengths.length > 0 && (
                  <div>
                    <h4 className="mb-2 text-sm font-semibold text-surface-800 dark:text-surface-100">可借鉴之处</h4>
                    <ul className="space-y-1 text-xs text-surface-600 dark:text-surface-400">
                      {detail.strengths.map((entry, index) => (
                        <li key={`s-${index}`}>· {entry}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {detail.weaknesses.length > 0 && (
                  <div>
                    <h4 className="mb-2 text-sm font-semibold text-surface-800 dark:text-surface-100">不必照搬之处</h4>
                    <ul className="space-y-1 text-xs text-surface-600 dark:text-surface-400">
                      {detail.weaknesses.map((entry, index) => (
                        <li key={`w-${index}`}>· {entry}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {detail.rawText && (
              <div className="card">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h4 className="text-sm font-semibold text-surface-800 dark:text-surface-100">原文</h4>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setShowRawText((value) => !value)}
                    data-testid="reference-toggle-raw"
                  >
                    {showRawText ? '收起原文' : '回看原文'}
                  </Button>
                </div>
                {showRawText && (
                  <pre
                    className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-surface-50 p-3 text-[11px] leading-relaxed text-surface-600 dark:bg-surface-800/40 dark:text-surface-400"
                    data-testid="reference-raw-text"
                  >
                    {detail.rawText}
                  </pre>
                )}
                {detail.truncated && (
                  <p className="mt-1 text-[11px] text-warning dark:text-warning-dark">
                    原文超过存储上限，只保留了前 {detail.storedChars.toLocaleString()} 字。
                  </p>
                )}
              </div>
            )}
          </section>
        )}
      </div>

      <Modal open={deleteTarget !== null} onClose={() => setDeleteTarget(null)} title="删除参考档案？">
        <p className="text-xs leading-relaxed text-surface-500 dark:text-surface-400">
          {deleteTarget ? `《${deleteTarget.name}》的拆解结果与保存的原文会一并删除，无法恢复。` : ''}
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(null)}>
            取消
          </Button>
          <Button variant="error" size="sm" onClick={() => void handleDelete()} data-testid="reference-delete-confirm">
            删除
          </Button>
        </div>
      </Modal>

      <Modal open={renameTarget !== null} onClose={() => setRenameTarget(null)} title="给档案改个名字">
        <p className="mb-3 text-xs leading-relaxed text-surface-500 dark:text-surface-400">
          只改名字，不动拆解结果与原文。
        </p>
        <Input
          label="档案名"
          value={renameValue}
          onChange={(event) => setRenameValue(event.target.value)}
          data-testid="reference-rename-input"
        />
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setRenameTarget(null)}>
            取消
          </Button>
          <Button variant="primary" size="sm" onClick={() => void handleRename()} data-testid="reference-rename-confirm">
            保存
          </Button>
        </div>
      </Modal>

      <Modal open={tagTarget !== null} onClose={() => setTagTarget(null)} title="给档案打标签">
        <p className="mb-3 text-xs leading-relaxed text-surface-500 dark:text-surface-400">
          {tagTarget ? `《${tagTarget.name}》的标签（逗号或顿号分隔；用于素材库搜索与筛选）。` : ''}
        </p>
        <Input
          label="标签"
          value={tagValue}
          placeholder="如：对话流、短句、悬疑"
          onChange={(event) => setTagValue(event.target.value)}
          data-testid="reference-tags-edit-input"
        />
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setTagTarget(null)}>
            取消
          </Button>
          <Button variant="primary" size="sm" onClick={() => void handleSaveTags()} data-testid="reference-tags-confirm">
            保存
          </Button>
        </div>
      </Modal>
    </div>
  );
}
