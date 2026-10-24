import { useCallback, useEffect, useMemo, useState } from 'react';
import { CheckCheck, Lightbulb, Plus, Sparkles, Trash2 } from 'lucide-react';

import { aiApi, projectApi } from '../../services/api';
import { useProjectStore } from '../../stores';
import type { InspirationFragment, Project } from '../../types';

const TYPE_OPTIONS: Array<{ value: InspirationFragment['type']; label: string }> = [
  { value: 'idea', label: '点子' },
  { value: 'plot', label: '剧情' },
  { value: 'scene', label: '场景' },
  { value: 'dialogue', label: '对白' },
  { value: 'character', label: '角色' },
];

const TYPE_HINTS: Array<{
  type: InspirationFragment['type'];
  keywords: string[];
}> = [
  { type: 'dialogue', keywords: ['对白', '台词', '对话', '说', '问', '答'] },
  { type: 'character', keywords: ['角色', '人物', '身份', '动机', '设定'] },
  { type: 'scene', keywords: ['场景', '地点', '空间', '环境', '镜头'] },
  { type: 'plot', keywords: ['剧情', '反转', '冲突', '伏笔', '推进'] },
];

const BRAINSTORM_PRESETS = [
  '给我 6 个剧情转折点',
  '围绕当前主角设计 5 个强冲突人物',
  '生成 5 个适合当前世界观的场景火花',
  '给我 5 段带戏剧张力的对白灵感',
];

function nowIso(): string {
  return new Date().toISOString();
}

function sanitizeListTags(input: string): string[] {
  return input
    .split(/[\s,，。！？；：、]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 8);
}

function cleanBulletPrefix(line: string): string {
  return line.replace(/^\s*(?:[-*+]|\d+[.)]|[一二三四五六七八九十]+[、.])\s*/, '').trim();
}

function buildFragmentTitle(content: string, fallback: string): string {
  const firstLine = cleanBulletPrefix(content.split('\n')[0] || '');
  if (!firstLine) return fallback;
  return firstLine.length > 24 ? `${firstLine.slice(0, 24)}...` : firstLine;
}

function inferFragmentType(content: string): InspirationFragment['type'] {
  if (/["“”''「」『』]/.test(content)) return 'dialogue';

  for (const rule of TYPE_HINTS) {
    if (rule.keywords.some((keyword) => content.includes(keyword))) {
      return rule.type;
    }
  }

  return 'idea';
}

function extractBrainstormFragments(projectId: string, reply: string, prompt: string): InspirationFragment[] {
  const rows = reply
    .replace(/\r\n?/g, '\n')
    .split(/\n{2,}/)
    .flatMap((block) => block.split('\n'))
    .map((line) => cleanBulletPrefix(line))
    .filter(Boolean);

  const uniqueRows = Array.from(new Set(rows)).slice(0, 8);
  const promptTags = sanitizeListTags(prompt).slice(0, 3);

  return uniqueRows.map((content, index) => ({
    id: `brainstorm-${Date.now()}-${index}`,
    projectId,
    title: buildFragmentTitle(content, `灵感 ${index + 1}`),
    content,
    type: inferFragmentType(content),
    tags: promptTags,
    createdAt: nowIso(),
    isConverted: false,
    source: 'brainstorm',
  }));
}

const EMPTY_BRAINSTORM_MESSAGE = '这次头脑风暴没有产出可用碎片，请换个提示词再试一次。';
const BRAINSTORM_FAILED_MESSAGE = '头脑风暴生成失败。';

export default function InspirationBoard({
  projectId,
  project: projectProp = null,
}: {
  projectId: string;
  project?: Project | null;
}) {
  const storeProject = useProjectStore((state) => state.projects.find((item) => item.id === projectId));
  const addProject = useProjectStore((state) => state.addProject);
  const updateProject = useProjectStore((state) => state.updateProject);
  const project = storeProject || projectProp;

  const [manualTitle, setManualTitle] = useState('');
  const [manualContent, setManualContent] = useState('');
  const [manualType, setManualType] = useState<InspirationFragment['type']>('idea');
  const [manualTags, setManualTags] = useState('');
  const [brainstormPrompt, setBrainstormPrompt] = useState('');
  const [brainstormDrafts, setBrainstormDrafts] = useState<InspirationFragment[]>([]);
  const [fragmentState, setFragmentState] = useState<InspirationFragment[]>([]);
  const [isSaving, setIsSaving] = useState(false);
  const [isBrainstorming, setIsBrainstorming] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setFragmentState([]);
  }, [projectId]);

  const projectFragments = project?.inspirationFragments || [];

  const fragments = useMemo(
    () => {
      const sourceFragments = fragmentState.length > 0 ? fragmentState : projectFragments;
      return sourceFragments.slice().sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    },
    [fragmentState, projectFragments],
  );

  const persistFragments = useCallback(async (nextFragments: InspirationFragment[]) => {
    setIsSaving(true);
    setError('');
    setFragmentState(nextFragments);

    try {
      const json = await projectApi.update(projectId, { inspirationFragments: nextFragments });
      const hasProjectInStore = useProjectStore.getState().projects.some((item) => item.id === projectId);

      if (json.data) {
        setFragmentState(json.data.inspirationFragments || nextFragments);
        if (hasProjectInStore) {
          updateProject(projectId, json.data);
        } else {
          addProject(json.data);
        }
      } else {
        if (hasProjectInStore) {
          updateProject(projectId, { inspirationFragments: nextFragments });
        } else if (project) {
          addProject({ ...project, inspirationFragments: nextFragments });
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存灵感碎片失败。');
      throw err;
    } finally {
      setIsSaving(false);
    }
  }, [addProject, project, projectId, updateProject]);

  const handleManualAdd = useCallback(async () => {
    const content = manualContent.trim();
    if (!content) return;

    const fragment: InspirationFragment = {
      id: `manual-${Date.now()}`,
      projectId,
      title: manualTitle.trim() || buildFragmentTitle(content, '手动灵感'),
      content,
      type: manualType,
      tags: sanitizeListTags(manualTags),
      createdAt: nowIso(),
      isConverted: false,
      source: 'manual',
    };

    await persistFragments([fragment, ...fragmentState]);
    setManualTitle('');
    setManualContent('');
    setManualTags('');
    setManualType('idea');
  }, [fragmentState, manualContent, manualTags, manualTitle, manualType, persistFragments, projectId]);

  const handleToggleConverted = useCallback(async (id: string) => {
    const next = fragmentState.map((fragment) => (
      fragment.id === id ? { ...fragment, isConverted: !fragment.isConverted } : fragment
    ));
    await persistFragments(next);
  }, [fragmentState, persistFragments]);

  const handleDelete = useCallback(async (id: string) => {
    const next = fragmentState.filter((fragment) => fragment.id !== id);
    await persistFragments(next);
  }, [fragmentState, persistFragments]);

  const handleSaveDraft = useCallback(async (fragment: InspirationFragment) => {
    const next = [fragment, ...fragmentState];
    await persistFragments(next);
    setBrainstormDrafts((previous) => previous.filter((item) => item.id !== fragment.id));
  }, [fragmentState, persistFragments]);

  const handleBrainstorm = useCallback(async (preset?: string) => {
    const prompt = (preset || brainstormPrompt).trim();
    if (!prompt || isBrainstorming) return;

    setIsBrainstorming(true);
    setError('');

    try {
      const messages = [
        {
          role: 'system' as const,
          content: '你是小说策划助手。请把头脑风暴结果拆成简洁、具体、可直接落地的灵感碎片，每条尽量独立成点。',
        },
        {
          role: 'user' as const,
          content: `请围绕这个需求做头脑风暴：${prompt}\n\n输出 5 到 8 条独立灵感，每条控制在 1 到 3 句。`,
        },
      ];

      const json = await aiApi.chat(projectId, messages);
      const reply = json.data?.reply || '';
      const nextDrafts = extractBrainstormFragments(projectId, reply, prompt);

      if (nextDrafts.length === 0) {
        setBrainstormDrafts([]);
        setError(EMPTY_BRAINSTORM_MESSAGE);
        return;
      }

      setBrainstormDrafts(nextDrafts);

      if (!preset) {
        setBrainstormPrompt('');
      }
    } catch (err) {
      setBrainstormDrafts([]);
      setError(err instanceof Error ? err.message : BRAINSTORM_FAILED_MESSAGE);
    } finally {
      setIsBrainstorming(false);
    }
  }, [brainstormPrompt, isBrainstorming, projectId]);

  return (
    <div className="flex h-full flex-col" data-testid="inspiration-board">
      <div className="border-b border-surface-200 bg-white px-4 py-3 dark:border-surface-700 dark:bg-surface-850">
        <div className="flex items-center gap-2">
          <Lightbulb size={18} className="text-warning" />
          <div>
            <h2 className="text-sm font-semibold text-surface-900 dark:text-surface-100">灵感碎片</h2>
            <p className="text-xs text-surface-500">
              把突发点子、对白火花和剧情脑暴沉淀成可复用素材。
            </p>
          </div>
        </div>
      </div>

      <div className="grid flex-1 gap-4 overflow-auto p-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
        <section className="min-w-0 space-y-4">
          <div className="rounded-lg border border-surface-200 bg-white p-4 dark:border-surface-700 dark:bg-surface-850">
            <div className="mb-3 flex items-center gap-2">
              <Sparkles size={16} className="text-primary-500" />
              <h3 className="text-sm font-semibold">头脑风暴</h3>
            </div>

            <div className="mb-3 flex flex-wrap gap-2">
              {BRAINSTORM_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className="rounded-full bg-surface-100 px-3 py-1.5 text-xs text-surface-600 transition-colors hover:bg-primary-50 hover:text-primary-600 dark:bg-surface-700 dark:text-surface-300"
                  onClick={() => void handleBrainstorm(preset)}
                  data-testid={`inspiration-preset-${preset}`}
                >
                  {preset}
                </button>
              ))}
            </div>

            <div className="flex min-w-0 gap-2">
              <input
                value={brainstormPrompt}
                onChange={(event) => setBrainstormPrompt(event.target.value)}
                placeholder="例如：给反派设计一次极具压迫感的登场"
                className="min-w-0 flex-1 rounded-lg bg-surface-100 px-3 py-2 text-sm outline-none ring-1 ring-transparent transition focus:ring-primary-500 dark:bg-surface-700"
                data-testid="inspiration-brainstorm-input"
              />
              <button
                type="button"
                onClick={() => void handleBrainstorm()}
                disabled={!brainstormPrompt.trim() || isBrainstorming}
                className="btn-primary shrink-0 px-3 py-2 text-sm disabled:bg-surface-300 disabled:opacity-100 dark:disabled:bg-surface-600"
                data-testid="inspiration-brainstorm-generate"
              >
                {isBrainstorming ? '生成中...' : '开始脑暴'}
              </button>
            </div>
          </div>

          <div
            className="rounded-lg border border-surface-200 bg-white p-4 dark:border-surface-700 dark:bg-surface-850"
            data-testid="inspiration-drafts"
          >
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold">待采纳灵感</h3>
              <span className="text-xs text-surface-500">{brainstormDrafts.length} 条</span>
            </div>

            {brainstormDrafts.length === 0 ? (
              <p className="text-sm text-surface-400">暂无灵感，请在上方输入提示词后点击「开始脑暴」生成。</p>
            ) : (
              <div className="space-y-3">
                {brainstormDrafts.map((fragment) => (
                  <article key={fragment.id} className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-surface-800 dark:text-surface-200">{fragment.title}</span>
                      <span className="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] text-primary-600 dark:bg-primary-900/30 dark:text-primary-300">
                        {TYPE_OPTIONS.find((option) => option.value === fragment.type)?.label}
                      </span>
                    </div>

                    <p className="whitespace-pre-wrap text-sm leading-6 text-surface-600 dark:text-surface-300">
                      {fragment.content}
                    </p>

                    <div className="mt-3 flex items-center justify-between">
                      <div className="flex flex-wrap gap-1">
                        {fragment.tags.map((tag) => (
                          <span
                            key={tag}
                            className="rounded-full bg-surface-100 px-2 py-0.5 text-[11px] text-surface-500 dark:bg-surface-700 dark:text-surface-300"
                          >
                            #{tag}
                          </span>
                        ))}
                      </div>

                      <button
                        type="button"
                        onClick={() => void handleSaveDraft(fragment)}
                        className="btn-warning px-2.5 py-1 text-xs"
                        data-testid={`inspiration-save-draft-${fragment.id}`}
                      >
                        保存为碎片
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="min-w-0 space-y-4">
          <div className="rounded-lg border border-surface-200 bg-white p-4 dark:border-surface-700 dark:bg-surface-850">
            <div className="mb-3 flex items-center gap-2">
              <Plus size={16} className="text-success" />
              <h3 className="text-sm font-semibold">手动记录</h3>
            </div>

            <div className="space-y-3">
              <input
                value={manualTitle}
                onChange={(event) => setManualTitle(event.target.value)}
                placeholder="标题（可选）"
                className="w-full rounded-lg bg-surface-100 px-3 py-2 text-sm outline-none ring-1 ring-transparent transition focus:ring-primary-500 dark:bg-surface-700"
                data-testid="inspiration-manual-title"
              />

              <textarea
                value={manualContent}
                onChange={(event) => setManualContent(event.target.value)}
                placeholder="把突然冒出来的设定、对白、场景火花先记下来……"
                className="min-h-28 w-full rounded-lg bg-surface-100 px-3 py-2 text-sm outline-none ring-1 ring-transparent transition focus:ring-primary-500 dark:bg-surface-700"
                data-testid="inspiration-manual-content"
              />

              <div className="grid gap-3 sm:grid-cols-2">
                <select
                  value={manualType}
                  onChange={(event) => setManualType(event.target.value as InspirationFragment['type'])}
                  className="rounded-lg bg-surface-100 px-3 py-2 text-sm outline-none ring-1 ring-transparent transition focus:ring-primary-500 dark:bg-surface-700"
                  data-testid="inspiration-manual-type"
                >
                  {TYPE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>

                <input
                  value={manualTags}
                  onChange={(event) => setManualTags(event.target.value)}
                  placeholder="标签，用空格或逗号分隔"
                  className="rounded-lg bg-surface-100 px-3 py-2 text-sm outline-none ring-1 ring-transparent transition focus:ring-primary-500 dark:bg-surface-700"
                  data-testid="inspiration-manual-tags"
                />
              </div>

              <button
                type="button"
                onClick={() => void handleManualAdd()}
                disabled={!manualContent.trim() || isSaving}
                className="btn-success px-3 py-2 text-sm disabled:bg-surface-300 disabled:opacity-100 dark:disabled:bg-surface-600"
                data-testid="inspiration-manual-save"
              >
                {isSaving ? '保存中...' : '保存碎片'}
              </button>
            </div>
          </div>

          <div
            className="rounded-lg border border-surface-200 bg-white p-4 dark:border-surface-700 dark:bg-surface-850"
            data-testid="inspiration-fragment-list"
          >
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold">已保存碎片</h3>
              <span className="text-xs text-surface-500">{fragments.length} 条</span>
            </div>

            {fragments.length === 0 ? (
              <div className="rounded-lg border border-dashed border-surface-300 px-4 py-6 text-sm text-surface-500 dark:border-surface-600">
                这里会收纳你的剧情火花、对白灵感和设定碎片，后续写作时随时回来取用。
              </div>
            ) : (
              <div className="space-y-3">
                {fragments.map((fragment) => (
                  <article key={fragment.id} className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                    <div className="mb-2 flex items-start justify-between gap-3">
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-surface-800 dark:text-surface-200">{fragment.title}</span>
                          <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[11px] text-surface-500 dark:bg-surface-700 dark:text-surface-300">
                            {TYPE_OPTIONS.find((option) => option.value === fragment.type)?.label}
                          </span>
                        </div>

                        <p className="mt-1 text-xs text-surface-400">
                          {fragment.source === 'brainstorm' ? 'AI 头脑风暴' : '手动记录'}
                          {' · '}
                          {new Date(fragment.createdAt).toLocaleString('zh-CN', { hour12: false })}
                        </p>
                      </div>

                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => void handleToggleConverted(fragment.id)}
                          className={`rounded-lg px-2 py-1 text-xs transition ${
                            fragment.isConverted
                              ? 'bg-success-soft text-success dark:bg-success-soft-dark dark:text-success-dark'
                              : 'bg-surface-100 text-surface-500 dark:bg-surface-700 dark:text-surface-300'
                          }`}
                          data-testid={`inspiration-toggle-converted-${fragment.id}`}
                        >
                          <CheckCheck size={12} className="mr-1 inline" />
                          {fragment.isConverted ? '已转化' : '标记转化'}
                        </button>

                        <button
                          type="button"
                          onClick={() => void handleDelete(fragment.id)}
                          className="rounded-lg bg-error-soft px-2 py-1 text-xs text-error transition hover:bg-error/10 dark:bg-error-soft-dark dark:text-error-dark"
                          data-testid={`inspiration-delete-${fragment.id}`}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </div>

                    <p className="whitespace-pre-wrap text-sm leading-6 text-surface-600 dark:text-surface-300">
                      {fragment.content}
                    </p>

                    {fragment.tags.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-1">
                        {fragment.tags.map((tag) => (
                          <span
                            key={tag}
                            className="rounded-full bg-warning-soft px-2 py-0.5 text-[11px] text-warning dark:bg-warning-soft-dark dark:text-warning-dark"
                          >
                            #{tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </article>
                ))}
              </div>
            )}
          </div>

          {error && (
            <div className="rounded-lg border border-error bg-error-soft px-3 py-2 text-sm text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark">
              {error}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}



