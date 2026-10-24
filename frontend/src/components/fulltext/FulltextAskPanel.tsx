/**
 * FulltextAskPanel — 全文问答（建议2：DeepSeek 前缀缓存低成本直灌核对）
 *
 * 直灌全书正文 + 问题 → LLM 回答，用于核对伏笔 / 角色设定 / 前文细节。
 * 前缀缓存设计：正文恒在问题之前且章节顺序固定，同一项目后续提问命中缓存，成本可忽略。
 * 只复用 common 组件（Textarea/Button/Badge/useToast），零新增依赖；
 * 严格遵循 docs/ui-design-system.md Token，明暗主题成对书写。
 */
import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, BookOpenCheck, Send, Sparkles } from 'lucide-react';

import { Badge, Button, Textarea, useToast } from '../common';
import { fulltextApi } from '../../services/api';
import type { FulltextAskResult, FulltextStats } from '../../types';

/** 成本展示：不足 1 分钱显示 4 位小数，否则 3 位；未知价格为 null。 */
function formatCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return '未知';
  if (usd <= 0) return '$0.00';
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(3)}`;
}

export default function FulltextAskPanel({ projectId }: { projectId: string }) {
  const toast = useToast();

  const [stats, setStats] = useState<FulltextStats | null>(null);
  const [statsError, setStatsError] = useState('');
  const [question, setQuestion] = useState('');
  const [result, setResult] = useState<FulltextAskResult | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState('');

  const loadStats = useCallback(async () => {
    if (!projectId) return;
    try {
      const json = await fulltextApi.stats(projectId);
      setStats(json.data);
      setStatsError('');
    } catch (err) {
      setStats(null);
      setStatsError(err instanceof Error ? err.message : '');
    }
  }, [projectId]);

  useEffect(() => {
    void loadStats();
  }, [loadStats]);

  const handleAsk = useCallback(async () => {
    const trimmed = question.trim();
    if (!trimmed || !projectId) return;
    setAsking(true);
    setError('');
    try {
      const json = await fulltextApi.ask(projectId, trimmed);
      setResult(json.data);
      if (json.data.degraded) {
        toast.info('全文问答暂不可用', 'AI 服务未配置或上游请求失败。请求失败时是否计费，以服务商记录为准。');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '全文问答失败。');
    } finally {
      setAsking(false);
    }
  }, [question, projectId, toast]);

  // 空书：stats 端点返回 400「全书暂无正文」
  const emptyBook = statsError.includes('暂无正文') || statsError.includes('项目未找到');

  return (
    <section className="mb-6" data-testid="fulltext-ask-panel">
      {/* 头部：标题 + 成本提示 */}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
          <BookOpenCheck size={16} className="text-primary-600" aria-hidden="true" />
          全文问答
        </h3>
        {stats && !emptyBook && (
          <div className="flex flex-wrap items-center gap-2" data-testid="fulltext-stats">
            <Badge variant="neutral">全书 {stats.chars.toLocaleString()} 字</Badge>
            <Badge variant="neutral">约 {stats.estimatedTokens.toLocaleString()} tokens</Badge>
            <Badge variant="info">
              直灌成本{stats.estimatedCostUsd === null || stats.estimatedCostUsd === undefined ? '未知（以账单为准）' : `约 ${formatCost(stats.estimatedCostUsd)}`}
            </Badge>
          </div>
        )}
      </div>

      {stats?.truncated && (
        <p className="mb-2 text-[11px] text-warning dark:text-warning-dark">
          全书超过截断保护上限，直灌时仅包含前 {stats.chapters} 章正文。
        </p>
      )}

      {emptyBook && (
        <div
          className="mb-3 rounded-lg bg-surface-100 px-3 py-2.5 text-xs text-surface-500 dark:bg-surface-700 dark:text-surface-400"
          data-testid="fulltext-empty-hint"
        >
          全书暂无正文，先撰写章节内容后即可使用全文问答核对设定与伏笔。
        </div>
      )}

      <div className="card p-3">
        <div className="flex items-start gap-2">
          <Textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="例如：主角的佩剑是什么来头？第几章埋下过这个伏笔？"
            rows={2}
            className="min-h-0 resize-none"
            disabled={asking || emptyBook}
            data-testid="fulltext-question-input"
          />
          <Button
            variant="primary"
            size="sm"
            className="shrink-0"
            loading={asking}
            disabled={!question.trim() || emptyBook}
            onClick={() => void handleAsk()}
            data-testid="fulltext-ask-button"
          >
            <Send size={14} aria-hidden="true" />
            提问
          </Button>
        </div>
        <p className="mt-2 text-[11px] text-surface-400">
          将直灌全书正文 + 问题，利用服务商前缀缓存降低重复提问成本；正文恒在问题前且顺序固定。
          大书（数十万字）首次直灌可能需要 1-3 分钟，请耐心等待。展示的是按已核实价格表计算的
          <span className="font-medium">最坏情况估算</span>（按缓存未命中价、输出上限），无已核实价格来源时显示未知；
          实际费用以服务商账单为准。
        </p>

        {error && (
          <div
            className="mt-2 flex items-start gap-2 rounded-lg bg-error-soft px-3 py-2.5 text-xs text-error dark:bg-error-soft-dark dark:text-error-dark"
            role="alert"
            data-testid="fulltext-error"
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1">{error}</span>
          </div>
        )}

        {result && !error && (
          <div
            className={`mt-3 rounded-lg border px-3 py-2.5 text-sm leading-relaxed ${
              result.degraded
                ? 'border-warning/30 bg-warning-soft text-warning dark:bg-warning-soft-dark dark:text-warning-dark'
                : 'border-surface-200 bg-surface-50 text-surface-700 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-200'
            }`}
            data-testid="fulltext-answer"
          >
            <p className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold text-surface-400">
              <Sparkles size={12} aria-hidden="true" />
              {result.degraded
                ? '降级提示（是否计费以服务商记录为准）'
                : `本次输入约 ${result.estimatedTokens.toLocaleString()} tokens · 估算 ${formatCost(result.estimatedCostUsd)}`}
            </p>
            <p className="whitespace-pre-wrap">{result.answer}</p>
          </div>
        )}
      </div>
    </section>
  );
}
