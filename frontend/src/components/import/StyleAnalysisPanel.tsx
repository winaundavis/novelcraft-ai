import { useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  Circle,
  Clock,
  Crosshair,
  FileText,
  Gauge,
  Info,
  Loader2,
  MessageSquareQuote,
  Palette,
  PenLine,
  Sparkles,
  TrendingUp,
  Wand2,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { styleApi } from '../../services/api';
import { useProjectStore } from '../../stores';
import { STYLE_PRESETS } from '../../types';
import type { StyleConfig, StyleDimensions, StyleProfile, StyleTransition, TransitionPace } from '../../types';
import { Badge, Button, Card, cn } from '../common';
import { STYLE_TRANSITION_CHANGED_EVENT, type StyleTransitionChangedDetail } from '../editor/chapterEvents';

/**
 * 快捷档位的**默认周期**，与后端 `transition_service.PACE_CONFIG` 完全一致
 * （fast 9 章 / medium 60 章 / slow 200 章）。
 * 这里只用于「告诉作者默认值是多少」的展示：提交时只带 pace，由后端决定默认章节数，
 * 避免前后端各存一份默认值而悄悄漂移。
 */
const PACE_OPTIONS: Array<{ value: TransitionPace; label: string; desc: string; defaultChapters: number }> = [
  { value: 'fast', label: '快速', desc: '每 3 章推进 · 共 3 步', defaultChapters: 9 },
  { value: 'medium', label: '中速', desc: '每 6 章推进 · 共 10 步', defaultChapters: 60 },
  { value: 'slow', label: '慢速', desc: '每 10 章推进 · 共 20 步', defaultChapters: 200 },
];

/** 后端 `VALID_PRESETS`（9 个），key 必须原样发回，不得自造。 */
const TARGET_PRESET_KEYS = [
  'concise',
  'poetic',
  'humorous',
  'intense',
  'tender',
  'epic',
  'lightNovel',
  'western',
  'emotional_resonance',
] as const;

/**
 * 预设中文名复用 `types` 里既有的 `STYLE_PRESETS`（后端预设的真实参数表）。
 * 只有 `emotional_resonance` 不在其中，沿用 `ChapterContextBar` 已有的中文名。
 */
const EXTRA_PRESET_LABELS: Record<string, string> = {
  emotional_resonance: '细腻情感 / 共鸣型',
};

function presetLabel(key: string): string {
  if (EXTRA_PRESET_LABELS[key]) return EXTRA_PRESET_LABELS[key];
  return STYLE_PRESETS[key]?.name || key;
}

/** 补充说明的字符上限，与后端 `StyleTransitionTarget.customPrompt` 约束一致。 */
const TARGET_PROMPT_MAX = 2000;
/** 周期章节数的有效区间，与后端 `targetChapters`（1–200，严格整数）一致。 */
const TARGET_CHAPTERS_MIN = 1;
const TARGET_CHAPTERS_MAX = 200;

type RuleGroupKey =
  | 'narrativeRules'
  | 'characterRules'
  | 'languageRules'
  | 'rhythmRules'
  | 'antiAiRules';

const RULE_GROUPS: Array<{ key: RuleGroupKey; label: string; icon: LucideIcon }> = [
  { key: 'narrativeRules', label: '叙述规则', icon: PenLine },
  { key: 'characterRules', label: '角色规则', icon: MessageSquareQuote },
  { key: 'languageRules', label: '语言规则', icon: Palette },
  { key: 'rhythmRules', label: '节奏规则', icon: Gauge },
  { key: 'antiAiRules', label: '反 AI 规则', icon: Sparkles },
];

const PARAGRAPH_LABELS: Record<string, string> = { short: '短段', medium: '中段', long: '长段' };

function pct(value: number | undefined): string {
  return `${Math.round((value || 0) * 100)}%`;
}

function StatItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
      <p className="text-[11px] text-surface-400">{label}</p>
      <p className="mt-0.5 truncate text-sm font-semibold text-surface-700 dark:text-surface-200" title={value}>
        {value}
      </p>
    </div>
  );
}

interface ComparisonRow {
  label: string;
  current: string;
  target: string;
  badge: { variant: 'neutral' | 'success' | 'warning'; text: string };
}

/**
 * 构建「当前画像 vs 目标预设」对比行。
 * - 数值型：|当前 − 目标| ≤ 20% × 目标 → 「接近目标」；否则 → 「偏差较大 +差值」
 * - 类别型：一致 → 「一致」；不一致 → 「需调整」
 */
function buildComparisonRows(
  dims: StyleDimensions | undefined,
  styleConfig: StyleConfig | undefined,
): ComparisonRow[] | null {
  if (!dims || !styleConfig) return null;
  const rows: ComparisonRow[] = [];

  const pushNumeric = (
    label: string,
    current: number | undefined,
    target: number,
    currentText: string,
    targetText: string,
    diffText: string,
  ) => {
    if (current == null) return;
    const diff = Math.abs(current - target);
    rows.push({
      label,
      current: currentText,
      target: targetText,
      badge: diff <= target * 0.2
        ? { variant: 'neutral', text: '接近目标' }
        : { variant: 'warning', text: `偏差较大 +${diffText}` },
    });
  };

  pushNumeric(
    '平均句长',
    dims.avgSentenceLength,
    styleConfig.avgSentenceLength,
    dims.avgSentenceLength != null ? `${dims.avgSentenceLength} 字` : '—',
    `${styleConfig.avgSentenceLength} 字`,
    `${Math.round(Math.abs((dims.avgSentenceLength ?? 0) - styleConfig.avgSentenceLength))} 字`,
  );
  pushNumeric(
    '对话占比',
    dims.dialogueRatio,
    styleConfig.dialogueRatio,
    dims.dialogueRatio != null ? pct(dims.dialogueRatio) : '—',
    pct(styleConfig.dialogueRatio),
    `${Math.round(Math.abs((dims.dialogueRatio || 0) - styleConfig.dialogueRatio) * 100)}%`,
  );
  pushNumeric(
    '描写密度',
    dims.descriptionDensity,
    styleConfig.descriptionDensity,
    dims.descriptionDensity != null ? pct(dims.descriptionDensity) : '—',
    pct(styleConfig.descriptionDensity),
    `${Math.round(Math.abs((dims.descriptionDensity || 0) - styleConfig.descriptionDensity) * 100)}%`,
  );

  const pushCategorical = (
    label: string,
    current: string | undefined,
    target: string,
    currentText: string,
    targetText: string,
  ) => {
    if (!current) return;
    rows.push({
      label,
      current: currentText,
      target: targetText,
      badge: current === target
        ? { variant: 'success', text: '一致' }
        : { variant: 'warning', text: '需调整' },
    });
  };

  pushCategorical(
    '段落长度',
    dims.paragraphLength,
    styleConfig.paragraphLength,
    PARAGRAPH_LABELS[dims.paragraphLength || ''] || dims.paragraphLength || '',
    PARAGRAPH_LABELS[styleConfig.paragraphLength] || styleConfig.paragraphLength,
  );
  pushCategorical('语气基调', dims.tone, styleConfig.tone, dims.tone || '', styleConfig.tone);

  return rows;
}

/**
 * M4 深度文风分析面板：触发分析 → 展示画像（状态/优缺点/维度统计/四组规则）
 * + 风格渐进过渡（快/中/慢三档 + 进度）。所有请求失败均降级提示，不阻塞其余 UI。
 */
export default function StyleAnalysisPanel({
  projectId,
  enabled = true,
}: {
  projectId: string;
  /** 章节入库后才可分析（导入成功后置 true） */
  enabled?: boolean;
}) {
  // 目标风格预设：来自当前项目 styleConfig（缺失时隐藏「与目标风格对比」块）
  const styleConfig = useProjectStore((state) =>
    state.projects.find((project) => project.id === projectId)?.styleConfig,
  );

  const [profile, setProfile] = useState<StyleProfile | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState('');

  const [transition, setTransition] = useState<StyleTransition | null>(null);
  /**
   * 表单状态与计划状态**刻意分开**：
   * `transition` 只由服务端响应/事件写入；下面这些只由用户输入写入。
   * 否则轮询、章节事件或迟到的响应会把作者正在填的档位/章节数冲掉。
   */
  const [selectedPace, setSelectedPace] = useState<TransitionPace>('medium');
  const [targetPresetKey, setTargetPresetKey] = useState('');
  const [targetPrompt, setTargetPrompt] = useState('');
  const [customChapters, setCustomChapters] = useState('');
  const [isSettingTransition, setIsSettingTransition] = useState(false);
  const [transitionError, setTransitionError] = useState('');
  /** 已有计划时是否展开「重新设置」表单（只影响表单显隐，不动计划本身）。 */
  const [isEditingPlan, setIsEditingPlan] = useState(false);
  /**
   * 请求序号 + 项目快照：切换项目或在途响应乱序返回时，
   * 旧响应不得写进新项目的面板状态。
   */
  const transitionSeqRef = useRef(0);
  const panelProjectRef = useRef(projectId);

  const [expandedGroups, setExpandedGroups] = useState<Set<RuleGroupKey>>(new Set(['narrativeRules']));

  // 静默拉取既有画像与过渡计划（无则忽略，避免 404 报错）。
  // 注意：**取数据**不看 `enabled`——`enabled` 只决定「能否新建计划」（需先有章节）。
  // 混用会导致刷新页面后已有计划完全不显示。
  // 但保留 `enabled` 作为依赖：入库后（false → true）再取一次，让画像/进度立刻刷新。
  useEffect(() => {
    if (!projectId) return;
    let active = true;
    const seq = ++transitionSeqRef.current;
    panelProjectRef.current = projectId;
    // 切项目即清空旧项目的计划与表单，避免跨项目串状态。
    setTransition(null);
    setTransitionError('');
    setIsEditingPlan(false);

    // 「尚未分析」后端返回 404，这里静默忽略。但如果上游给出 200 + data:null，
    // normalize* 会把它变成 {} —— 一个「看起来是画像、实际没有任何字段」的假对象。
    // 直接塞进 state，读取 strengths / plan 这类字段时就会抛错并掀掉整棵 React 树（白屏）。
    // normalize 保证真实数据一定带 status / pace，用它作为「确实有数据」的判据。
    styleApi.getProfile(projectId)
      .then((json) => { if (active && json.data?.status) setProfile(json.data); })
      .catch(() => { /* 尚未分析，忽略 */ });

    styleApi.getTransition(projectId)
      .then((json) => {
        // 迟到的响应（用户已切项目/已有更新的请求）一律丢弃。
        if (!active || seq !== transitionSeqRef.current) return;
        if (json.data?.pace) setTransition(json.data);
      })
      .catch(() => { /* 尚未设置过渡，忽略 */ });

    return () => { active = false; };
  }, [enabled, projectId]);

  // 保存正文 / 接受候选后进度会变：用事件带回来的最新计划刷新，绝不自行推算完成数。
  useEffect(() => {
    const onTransitionChanged = (event: Event) => {
      const detail = (event as CustomEvent<StyleTransitionChangedDetail>).detail;
      if (!detail || detail.projectId !== panelProjectRef.current) return;
      if (detail.transition) setTransition(detail.transition);
      else void styleApi.getTransition(panelProjectRef.current)
        .then((json) => { if (json.data?.pace) setTransition(json.data); })
        .catch(() => { /* 忽略 */ });
    };
    window.addEventListener(STYLE_TRANSITION_CHANGED_EVENT, onTransitionChanged);
    return () => window.removeEventListener(STYLE_TRANSITION_CHANGED_EVENT, onTransitionChanged);
  }, []);

  const handleAnalyze = async () => {
    setAnalysisError('');
    setIsAnalyzing(true);

    try {
      const json = await styleApi.analyze(projectId);
      setProfile(json.data);
      // 分析完成（可能覆盖旧画像）后重新校准过渡进度
      const seq = transitionSeqRef.current;
      styleApi.getTransition(projectId)
        .then((transitionJson) => {
          if (seq !== transitionSeqRef.current) return;
          if (transitionJson.data) setTransition(transitionJson.data);
        })
        .catch(() => { /* 未设置过渡，忽略 */ });
    } catch (error) {
      setAnalysisError(error instanceof Error ? error.message : '深度文风分析失败。');
    } finally {
      setIsAnalyzing(false);
    }
  };

  /**
   * 提交/重设计划。
   *
   * 提交契约（已逐字核对后端）：
   * - 快捷档位只发 `pace`，默认周期 9/60/200 章由后端决定。
   * - `custom` 必须显式给 `targetChapters`，且必须是 1–200 的**整数**
   *   （字符串/小数/布尔都会被 422 拒绝，所以这里先在前端挡一次并提供可读原因）。
   * - `targetStyle` 只在作者选了目标预设或写了补充说明时才发。
   * - 每次 PUT 都会生成**新的** planId 与 revision=1，并重置起点——所以这是用户主动动作。
   */
  const handleStartTransition = async () => {
    setTransitionError('');

    const isCustom = selectedPace === 'custom';
    let chapters: number | undefined;
    if (isCustom) {
      const raw = customChapters.trim();
      if (!/^\d+$/.test(raw)) {
        setTransitionError('自定义周期请填写整数章节数（1–200）。');
        return;
      }
      chapters = Number(raw);
      if (chapters < TARGET_CHAPTERS_MIN || chapters > TARGET_CHAPTERS_MAX) {
        setTransitionError(`自定义周期必须在 ${TARGET_CHAPTERS_MIN}–${TARGET_CHAPTERS_MAX} 章之间。`);
        return;
      }
    }
    if (targetPrompt.length > TARGET_PROMPT_MAX) {
      setTransitionError(`补充说明最多 ${TARGET_PROMPT_MAX} 字，当前 ${targetPrompt.length} 字。`);
      return;
    }

    const targetStyle = (targetPresetKey || targetPrompt.trim())
      ? { presetKey: targetPresetKey, customPrompt: targetPrompt }
      : undefined;

    const seq = ++transitionSeqRef.current;
    setIsSettingTransition(true);
    try {
      const json = await styleApi.setTransition(projectId, {
        pace: selectedPace,
        ...(isCustom && chapters !== undefined ? { targetChapters: chapters } : {}),
        ...(targetStyle ? { targetStyle } : {}),
      });
      // 切项目后迟到的成功响应不得写进新面板。
      if (seq !== transitionSeqRef.current || panelProjectRef.current !== projectId) return;
      setTransition(json.data);
      setIsEditingPlan(false);
      setTargetPrompt('');
    } catch (error) {
      if (seq !== transitionSeqRef.current || panelProjectRef.current !== projectId) return;
      setTransitionError(error instanceof Error ? error.message : '设置过渡档位失败。');
    } finally {
      if (seq === transitionSeqRef.current) setIsSettingTransition(false);
    }
  };

  const toggleGroup = (key: RuleGroupKey) => {
    setExpandedGroups((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const dims = profile?.dimensions;
  const metrics = profile?.metrics;
  // 画像字段缺失时退化为空数组，保证渲染路径不会因为 undefined 直接抛错
  const strengths = profile?.strengths ?? [];
  const weaknesses = profile?.weaknesses ?? [];
  const progressPercent = transition ? Math.min(Math.max((transition.progress || 0) * 100, 0), 100) : 0;
  const currentPaceLabel = PACE_OPTIONS.find((option) => option.value === transition?.pace)?.label
    || (transition?.pace === 'custom' ? '自定义' : undefined);
  /**
   * v1 旧计划只读：后端明确标记 `legacyReadOnly`，并且不会补 planId/revision。
   * 旧进度照旧展示，但绝不折算成「完成几章」，也不显示不存在的版本号。
   */
  const isLegacyPlan = Boolean(transition && transition.legacyReadOnly);
  const completedCount = transition?.completedCount;
  const targetChapters = transition?.targetChapters;
  const transitionComplete = Boolean(transition && (transition.progress || 0) >= 1);
  const comparisonRows = buildComparisonRows(dims, styleConfig);
  // 过渡三态：未开始（neutral）/ 进行中（info）/ 已完成（success）——语义色 + 图标 + 文字三重表达
  const transitionStatus = !transition
    ? { variant: 'neutral' as const, text: '未开始', Icon: Circle }
    : isLegacyPlan
      ? { variant: 'neutral' as const, text: '旧版计划', Icon: Info }
      : transitionComplete
        ? { variant: 'success' as const, text: '计划已完成', Icon: CheckCircle2 }
        : { variant: 'info' as const, text: '进行中', Icon: Clock };

  /** 已有计划时的展示信息：原稿 / 目标 / 周期 / 完成数，全部来自后端快照。 */
  const planSummary = transition
    ? {
        sourceName: transition.sourceSnapshot?.name || '',
        targetLabel: transition.targetSnapshot?.presetKey
          ? presetLabel(transition.targetSnapshot.presetKey)
          : '',
        targetPrompt: transition.targetSnapshot?.customPrompt || '',
        targetChapters,
        completedCount,
      }
    : null;

  // 未开始、或作者主动点「重新设置」时展示表单；计划本身仍保留在 state 里。
  const showPlanForm = !transition || isEditingPlan;

  const openPlanForm = () => {
    setTransitionError('');
    setIsEditingPlan(true);
    // 已选档位沿用当前计划，避免作者重设时又要重新猜自己上次选了什么。
    if (transition && !isLegacyPlan) setSelectedPace(transition.pace);
    if (transition?.targetSnapshot?.presetKey) setTargetPresetKey(transition.targetSnapshot.presetKey);
  };

  const cancelPlanForm = () => {
    setTransitionError('');
    setIsEditingPlan(false);
  };

  return (
    <Card className="space-y-4" data-testid="style-analysis-panel">
      {/* ① 触发区 Trigger Area */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Wand2 size={18} className="text-primary-600" aria-hidden />
          <h3 className="text-sm font-semibold">深度文风分析</h3>
          {profile && (
            <Badge
              variant={profile.status === 'degraded' ? 'warning' : 'success'}
              className="gap-1"
              data-testid="style-profile-status"
            >
              {profile.status === 'degraded' ? (
                <>
                  <AlertTriangle size={12} aria-hidden />
                  降级 · 规则统计
                </>
              ) : (
                <>
                  <CheckCircle2 size={12} aria-hidden />
                  已完成 · LLM 深度分析
                </>
              )}
            </Badge>
          )}
        </div>
        <Button
          onClick={() => void handleAnalyze()}
          loading={isAnalyzing}
          disabled={!enabled}
          size="sm"
          data-testid="style-analyze-button"
        >
          <Sparkles size={14} />
          {profile ? '重新分析' : '深度文风分析'}
        </Button>
      </div>

      {!enabled && (
        <div
          className="flex items-start gap-2 rounded-lg bg-info-soft px-3 py-2.5 text-xs text-info dark:bg-info-soft-dark dark:text-info-dark"
          data-testid="style-disabled-notice"
        >
          <Info size={14} className="mt-0.5 shrink-0" aria-hidden />
          请先「导入到当前项目」，章节入库后才能进行深度文风分析。
        </div>
      )}

      {analysisError && (
        <div
          className="flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
          data-testid="style-analysis-error"
        >
          <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden />
          <span>{analysisError}</span>
        </div>
      )}

      {isAnalyzing && (
        <div className="space-y-4" role="status" aria-live="polite">
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <Loader2 size={22} className="animate-spin text-primary-500" aria-hidden />
            <p className="text-sm text-surface-500">正在采样正文并提炼文风特征…</p>
            <p className="text-xs text-surface-400">预计需要 1–2 分钟，请勿关闭页面</p>
          </div>
          {/* 加载骨架卡：保持 6 宫格布局稳定，减少跳动 */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6" aria-hidden>
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="h-16 animate-pulse rounded-lg bg-surface-100 dark:bg-surface-700" />
            ))}
          </div>
        </div>
      )}

      {/* 空状态引导：无画像时给出下一步动作（CTA 复用 handleAnalyze） */}
      {enabled && !profile && !isAnalyzing && !analysisError && (
        <div className="animate-fade-in flex flex-col items-center gap-1 py-10 text-center">
          <Wand2 size={40} className="mb-2 text-surface-300" aria-hidden />
          <h4 className="text-sm font-semibold">还没有文风画像</h4>
          <p className="mx-auto max-w-sm text-xs leading-relaxed text-surface-400">
            章节入库后，点击「深度文风分析」即可提炼叙述、角色、语言与节奏特征，并生成可执行的风格渐进过渡计划。
          </p>
          <Button
            size="sm"
            className="mt-4"
            onClick={() => void handleAnalyze()}
            loading={isAnalyzing}
            data-testid="style-analyze-empty-cta"
          >
            <Sparkles size={14} />
            深度文风分析
          </Button>
        </div>
      )}

      {/* ② 画像报告 Style Report */}
      {profile && !isAnalyzing && (
        <div className="space-y-6">
          {profile.status === 'degraded' && (
            <div
              className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2.5 text-xs leading-relaxed text-warning dark:bg-warning-soft-dark dark:text-warning-dark"
              data-testid="style-profile-degraded-notice"
            >
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden />
              <span>
                LLM 未配置或调用失败，已自动降级为规则统计模式：画像仅含客观文本指标与通用建议，叙事/角色/用词特征待配置 AI 服务后重新分析补充。
              </span>
            </div>
          )}

          {profile.report && (
            <div className="rounded-lg border border-surface-200 bg-surface-50 px-3 py-3 dark:border-surface-700 dark:bg-surface-800">
              <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-surface-600 dark:text-surface-300">
                <FileText size={13} className="text-primary-600" aria-hidden />
                分析摘要
              </h4>
              <p
                className="whitespace-pre-wrap text-sm leading-relaxed text-surface-600 dark:text-surface-300"
                data-testid="style-profile-report"
              >
                {profile.report}
              </p>
            </div>
          )}

          <div>
            <h4 className="mb-3 flex items-center gap-2 text-xs font-semibold">
              <Gauge size={14} className="text-primary-600" aria-hidden />
              客观指标
            </h4>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <StatItem label="平均句长" value={dims?.avgSentenceLength != null ? `${dims.avgSentenceLength} 字` : '—'} />
              <StatItem label="对话占比" value={dims?.dialogueRatio != null ? pct(dims.dialogueRatio) : '—'} />
              <StatItem label="描写密度" value={dims?.descriptionDensity != null ? pct(dims.descriptionDensity) : '—'} />
              <StatItem label="段落长度" value={PARAGRAPH_LABELS[dims?.paragraphLength || ''] || '—'} />
              <StatItem label="语气基调" value={dims?.tone || '—'} />
              <StatItem
                label="采样规模"
                value={profile ? `${(profile.sampleChars || 0).toLocaleString()} 字 · ${profile.sampleCount || 0} 章` : '—'}
              />
            </div>
            {metrics && (
              <p className="mt-2 text-[11px] text-surface-400">
                样本明细：短句占比 {pct(metrics.sentenceShortRatio)} / 长句占比 {pct(metrics.sentenceLongRatio)} ·
                段落分布 短 {pct(metrics.paragraphShortRatio)} / 中 {pct(metrics.paragraphMediumRatio)} / 长 {pct(metrics.paragraphLongRatio)}
              </p>
            )}
          </div>

          {/* 与目标风格对比（styleConfig 缺失时整体隐藏） */}
          {comparisonRows && comparisonRows.length > 0 && (
            <div className="overflow-hidden rounded-lg border border-surface-200 dark:border-surface-700">
              <div className="flex items-center justify-between border-b border-surface-200 px-3 py-2 dark:border-surface-700">
                <span className="flex items-center gap-1.5 text-xs font-semibold">
                  <Crosshair size={13} className="text-primary-600" aria-hidden />
                  与目标风格对比
                </span>
                <Badge variant="info">目标：{styleConfig?.name}</Badge>
              </div>
              <div className="divide-y divide-surface-200 px-3 dark:divide-surface-700">
                {comparisonRows.map((row) => (
                  <div key={row.label} className="flex items-center justify-between gap-3 py-2 text-xs">
                    <span className="w-20 shrink-0 text-surface-400">{row.label}</span>
                    <span className="min-w-0 flex-1 truncate text-right font-medium text-surface-700 dark:text-surface-200">
                      {row.current}
                    </span>
                    <ArrowRight size={12} className="shrink-0 text-surface-300" aria-hidden />
                    <span className="min-w-0 flex-1 truncate text-right font-medium text-surface-500">{row.target}</span>
                    <Badge variant={row.badge.variant} className="shrink-0">
                      {row.badge.text}
                    </Badge>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded-lg border border-success/30 bg-success-soft p-3 dark:bg-success-soft-dark">
              <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-success dark:text-success-dark">
                <TrendingUp size={14} aria-hidden />
                优点
              </h4>
              {strengths.length ? (
                <ul className="space-y-1.5">
                  {strengths.map((item, index) => (
                    <li key={index} className="flex gap-2 text-sm leading-relaxed text-surface-600 dark:text-surface-300">
                      <span className="text-success" aria-hidden>•</span>
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-surface-400">暂无优点描述。</p>
              )}
            </div>

            <div className="rounded-lg border border-warning/30 bg-warning-soft p-3 dark:bg-warning-soft-dark">
              <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-warning dark:text-warning-dark">
                <TrendingUp size={14} className="rotate-180" aria-hidden />
                不足与改进
              </h4>
              {weaknesses.length ? (
                <ul className="space-y-1.5">
                  {weaknesses.map((item, index) => (
                    <li key={index} className="flex gap-2 text-sm leading-relaxed text-surface-600 dark:text-surface-300">
                      <span className="text-warning" aria-hidden>•</span>
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-surface-400">暂无不足描述。</p>
              )}
            </div>
          </div>

          <div className="space-y-2">
            {RULE_GROUPS.map((group) => {
              const rules = profile?.[group.key] ?? [];
              const isOpen = expandedGroups.has(group.key);
              return (
                <div
                  key={group.key}
                  className="overflow-hidden rounded-lg border border-surface-200 dark:border-surface-700"
                >
                  <button
                    type="button"
                    onClick={() => toggleGroup(group.key)}
                    aria-expanded={isOpen}
                    aria-controls={`rule-panel-${group.key}`}
                    className="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left text-sm font-medium transition-colors hover:bg-surface-50 dark:hover:bg-surface-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                    data-testid={`style-rule-group-${group.key}`}
                  >
                    <span className="flex items-center gap-2">
                      <group.icon size={15} className="text-primary-600" aria-hidden />
                      {group.label}
                      <Badge variant="neutral">{rules.length} 条</Badge>
                    </span>
                    <ChevronDown
                      size={14}
                      className={`text-surface-400 transition-transform duration-150 ${isOpen ? 'rotate-180' : ''}`}
                      aria-hidden
                    />
                  </button>
                  {isOpen && (
                    <div
                      id={`rule-panel-${group.key}`}
                      role="region"
                      aria-label={group.label}
                      className="border-t border-surface-200 px-3 py-3 dark:border-surface-700"
                    >
                      {rules.length ? (
                        <ol className="space-y-2">
                          {rules.map((rule, index) => (
                            <li
                              key={index}
                              className="flex gap-2.5 text-sm leading-relaxed text-surface-600 dark:text-surface-300"
                            >
                              <span className="w-4 shrink-0 text-right font-semibold text-primary-500" aria-hidden>
                                {String(index + 1).padStart(2, '0')}
                              </span>
                              <span>{rule.rule}</span>
                            </li>
                          ))}
                        </ol>
                      ) : (
                        <p className="text-xs text-surface-400">该组暂无规则。</p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ③ 渐进过渡 Transition（始终展示，border-t 分隔） */}
      <div className="border-t border-surface-200 pt-4 dark:border-surface-700">
        <div className="mb-3 flex items-center gap-2 text-xs font-semibold">
          <TrendingUp size={14} className="text-primary-600" aria-hidden />
          风格渐进过渡
          <Badge variant={transitionStatus.variant} className="ml-auto gap-1">
            <transitionStatus.Icon size={12} aria-hidden />
            {transitionStatus.text}
          </Badge>
        </div>

        {showPlanForm ? (
          <div className="space-y-3">
            <p className="text-xs text-surface-500">
              渐进过渡把「原稿文风 → 目标文风」拆成若干章逐步推进。
              <strong className="font-semibold text-surface-600 dark:text-surface-300">进度只是章节计数</strong>
              ，不代表文学质量已经达标；每一次保存都会记录进度，重新设置会开始一份新计划。
            </p>

            <div role="radiogroup" aria-label="过渡档位" className="grid gap-2 sm:grid-cols-3">
              {PACE_OPTIONS.map((option) => {
                const selected = selectedPace === option.value;
                return (
                  <label
                    key={option.value}
                    className={cn(
                      'relative flex cursor-pointer flex-col gap-0.5 rounded-lg border px-3 py-2.5 transition-colors duration-150',
                      'border-surface-200 hover:border-primary-300 dark:border-surface-700',
                      'focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-primary-500',
                      'has-[:checked]:border-primary-500 has-[:checked]:bg-primary-50 dark:has-[:checked]:bg-primary-900/30',
                    )}
                  >
                    {/* 将原生控件的焦点区域限制在可见卡片内，避免跨滚动容器定位。 */}
                    <input
                      type="radio"
                      name="transition-pace"
                      value={option.value}
                      checked={selected}
                      onChange={() => setSelectedPace(option.value)}
                      className="absolute inset-0 m-0 h-full w-full cursor-pointer opacity-0"
                      data-testid={`style-pace-${option.value}`}
                    />
                    <span
                      className={cn(
                        'text-sm font-semibold',
                        selected ? 'text-primary-700 dark:text-primary-300' : 'text-surface-700 dark:text-surface-200',
                      )}
                    >
                      {option.label}
                    </span>
                    <span className="text-[11px] text-surface-400">{option.desc}</span>
                    <span className="text-[11px] text-surface-400" data-testid={`style-pace-default-${option.value}`}>
                      默认周期 {option.defaultChapters} 章
                    </span>
                  </label>
                );
              })}
            </div>

            {/* 自定义周期：只有 custom 档位才需要，且必须是 1–200 的整数 */}
            <div className="space-y-2">
              <label className="flex cursor-pointer items-center gap-2 text-xs text-surface-600 dark:text-surface-300">
                <input
                  type="radio"
                  name="transition-pace"
                  value="custom"
                  checked={selectedPace === 'custom'}
                  onChange={() => setSelectedPace('custom')}
                  data-testid="style-pace-custom"
                />
                自定义周期
              </label>
              {selectedPace === 'custom' && (
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min={TARGET_CHAPTERS_MIN}
                    max={TARGET_CHAPTERS_MAX}
                    step={1}
                    value={customChapters}
                    onChange={(event) => setCustomChapters(event.target.value)}
                    placeholder={`${TARGET_CHAPTERS_MIN}–${TARGET_CHAPTERS_MAX}`}
                    className="w-28 rounded border border-surface-300 px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-800"
                    data-testid="style-custom-chapters"
                  />
                  <span className="text-[11px] text-surface-400">章</span>
                </div>
              )}
            </div>

            {/* 目标文风：单一预设 + 可选文字补充（补充说明优先用于约束生成） */}
            <div className="space-y-2">
              <label className="block text-xs text-surface-600 dark:text-surface-300" htmlFor="style-target-preset">
                目标文风预设（可选）
              </label>
              <select
                id="style-target-preset"
                value={targetPresetKey}
                onChange={(event) => setTargetPresetKey(event.target.value)}
                className="w-full rounded border border-surface-300 px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-800"
                data-testid="style-target-preset"
              >
                <option value="">沿用项目当前文风</option>
                {TARGET_PRESET_KEYS.map((key) => (
                  <option key={key} value={key}>{presetLabel(key)}</option>
                ))}
              </select>
              <textarea
                value={targetPrompt}
                onChange={(event) => setTargetPrompt(event.target.value.slice(0, TARGET_PROMPT_MAX))}
                rows={2}
                placeholder="补充说明（可选）：例如「对白更口语化，减少四字成语」"
                className="w-full rounded border border-surface-300 px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-800"
                data-testid="style-target-prompt"
              />
              <div className="text-right text-[11px] text-surface-400" data-testid="style-target-prompt-count">
                {targetPrompt.length} / {TARGET_PROMPT_MAX}
              </div>
            </div>

            {transitionError && (
              <div className="flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark" data-testid="style-transition-error">
                <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden />
                <span>{transitionError}</span>
              </div>
            )}

            <div className="flex items-center gap-2">
              <Button
                size="sm"
                loading={isSettingTransition}
                onClick={() => void handleStartTransition()}
                disabled={!enabled}
                data-testid="style-transition-start-button"
              >
                {transition ? '重新设置计划' : '开始过渡'}
              </Button>
              {transition && (
                <Button size="sm" variant="ghost" onClick={cancelPlanForm} data-testid="style-transition-cancel-button">
                  取消
                </Button>
              )}
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-2 text-surface-500">
                当前档位：
                <Badge variant="info">{currentPaceLabel || transition.pace}</Badge>
              </span>
              {transition.schemaVersion === 2 && transition.revision !== undefined && (
                <span className="text-surface-400" data-testid="style-transition-revision">
                  计划版本 {transition.revision}
                </span>
              )}
            </div>

            {isLegacyPlan ? (
              // v1 只读：明确告知不折算完成数，也不假装有版本号；重新设置后才启用新记录。
              <p className="text-xs text-warning" data-testid="style-transition-legacy-notice">
                旧版计划，重新设置后启用新进度记录。以下进度为历史数据，仅作参考，未折算成章节完成数。
              </p>
            ) : (
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs sm:grid-cols-4" data-testid="style-transition-summary">
                <div>
                  <dt className="text-surface-400">原稿</dt>
                  <dd className="text-surface-700 dark:text-surface-200">{planSummary?.sourceName || '当前分析画像'}</dd>
                </div>
                <div>
                  <dt className="text-surface-400">目标</dt>
                  <dd className="text-surface-700 dark:text-surface-200">
                    {planSummary?.targetLabel || '沿用项目文风'}
                    {planSummary?.targetPrompt ? `（${planSummary.targetPrompt}）` : ''}
                  </dd>
                </div>
                <div>
                  <dt className="text-surface-400">周期</dt>
                  <dd className="text-surface-700 dark:text-surface-200" data-testid="style-transition-target-chapters">
                    {planSummary?.targetChapters !== undefined ? `${planSummary.targetChapters} 章` : '—'}
                  </dd>
                </div>
                <div>
                  <dt className="text-surface-400">完成数</dt>
                  <dd className="text-surface-700 dark:text-surface-200" data-testid="style-transition-completed-count">
                    {planSummary?.completedCount !== undefined ? `${planSummary.completedCount} 章` : '—'}
                  </dd>
                </div>
              </dl>
            )}

            <div className="flex items-center justify-between text-xs">
              <span className="text-surface-400">计划进度</span>
              <span className="text-surface-400" data-testid="style-transition-progress-label">
                {Math.round(progressPercent)}%
              </span>
            </div>
            <div
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progressPercent}
              aria-label="风格过渡计划进度"
              className="h-1.5 overflow-hidden rounded-full bg-surface-100 dark:bg-surface-700"
            >
              <div
                className="h-full rounded-full bg-primary-500 transition-all duration-700"
                style={{ width: `${progressPercent}%` }}
                data-testid="style-transition-progress"
              />
            </div>

            {!isLegacyPlan && transitionComplete ? (
              <div className="flex flex-wrap items-center justify-between gap-3">
                {/* 只说「计划已完成」（章节计数到位），不说文风已经达标。 */}
                <p className="text-xs text-success" data-testid="style-transition-complete-text">
                  计划已完成：过渡区间内的章节计数已走完。是否达到目标文风仍以人工判断为准。
                </p>
                <Button size="sm" variant="ghost" onClick={openPlanForm} data-testid="style-transition-reset-button">
                  重新设置计划
                </Button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-surface-400" data-testid="style-transition-hint">
                  {isLegacyPlan
                    ? '旧版计划不记录完成章节；重新设置后会按新计划逐章记录进度。'
                    : `每 ${transition.intervalChapters} 章推进一次；保存正文或接受候选时自动记录进度。进度是章节计数，不等于文风质量评估。`}
                </p>
                <Button size="sm" variant="ghost" onClick={openPlanForm} data-testid="style-transition-reset-button">
                  重新设置计划
                </Button>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
