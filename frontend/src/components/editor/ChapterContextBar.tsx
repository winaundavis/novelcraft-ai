import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Users, Flag, Sparkles } from 'lucide-react';

import { foreshadowApi, storyStateApi } from '../../services/api';
import type { Project, StoryState } from '../../types';

const STYLE_INFO: Record<string, { label: string; description: string; scenes: string }> = {
  concise: { label: '现实 / 克制', description: '真实、收敛，情绪藏在行动里。', scenes: '现实题材、关系冲突、日常压力' },
  intense: { label: '爽快 / 商业', description: '节奏明快，冲突和回报清晰。', scenes: '升级、对抗、强推进情节' },
  poetic: { label: '悬疑 / 冷峻', description: '控制信息释放，让线索逐步逼近。', scenes: '悬疑、推理、压迫感场景' },
  lightNovel: { label: '轻快 / 轻小说', description: '对话活跃，反应鲜明，阅读轻盈。', scenes: '人物互动、校园、轻喜剧' },
  epic: { label: '古典 / 文学', description: '重视语言质感、意象与节奏变化。', scenes: '历史、史诗、文学表达' },
  emotional_resonance: { label: '细腻情感 / 共鸣型', description: '让关系、潜台词和细微变化自然累积情绪。', scenes: '言情、家庭、现实与关系驱动故事' },
};

function styleInfo(project: Project | undefined) {
  const config = project?.styleConfig;
  const key = config?.id || '';
  const text = `${config?.name || ''} ${config?.tone || ''}`.toLowerCase();
  const inferred = text.includes('light') || text.includes('轻') ? 'lightNovel'
    : text.includes('intense') || text.includes('urgent') || text.includes('爽') ? 'intense'
      : text.includes('poetic') || text.includes('lyric') || text.includes('悬') ? 'poetic'
        : text.includes('epic') || text.includes('grand') || text.includes('古典') ? 'epic'
          : text.includes('emotion') || text.includes('intimate') || text.includes('情感') ? 'emotional_resonance'
            : 'concise';
  return STYLE_INFO[key] || STYLE_INFO[inferred] || { label: config?.name || '项目默认文风', description: '沿用项目当前文风。', scenes: '所有题材' };
}

export default function ChapterContextBar({ project, chapter, onStateChange }: {
  project?: Project;
  chapter?: { title?: string; summary?: string; conflict?: string; parentId?: string | null } | null;
  onStateChange?: (state: StoryState | null) => void;
}) {
  const [state, setState] = useState<StoryState | null>(null);
  const [foreshadowCount, setForeshadowCount] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const info = styleInfo(project);
  const characters = useMemo(() => (project?.characters || []).slice(0, 3), [project?.characters]);
  // `onStateChange` 是可选的，目前没有调用点传它；但它一旦被传成内联箭头函数，
  // 下面的 effect 就会在**每次渲染时重跑**（重新拉取 story state 与伏笔列表），
  // 而它又会回调父组件 setState —— 那就构成“渲染 → 拉取 → setState → 渲染”的无限请求循环。
  // 因此不把它放进依赖，改用 ref 读取最新回调。
  const onStateChangeRef = useRef(onStateChange);
  useEffect(() => {
    onStateChangeRef.current = onStateChange;
  });

  useEffect(() => {
    if (!project?.id) return;
    let active = true;
    void storyStateApi.get(project.id).then((json) => {
      if (active) { setState(json.data || null); onStateChangeRef.current?.(json.data || null); }
    }).catch(() => { if (active) setState(null); });
    void foreshadowApi.list(project.id).then((json) => {
      if (active) setForeshadowCount(json.data?.items?.filter((item) => item.status === 'planted' || item.status === 'progressing').length ?? 0);
    }).catch(() => { if (active) setForeshadowCount(null); });
    return () => { active = false; };
    // 只在项目变化时重取；onStateChange 走 ref（见上）
     
  }, [project?.id]);

  const goal = chapter?.summary || state?.currentPhaseGoal || state?.currentVolumeGoal || '尚未设定本章目标';
  const conflict = chapter?.conflict || state?.mainConflict || '尚未记录主要冲突';
  const stateItems = [
    ['当前目标', state?.currentPhaseGoal || state?.currentVolumeGoal || '暂无'],
    ['人物状态', state?.relationshipChanges?.join('；') || '暂无记录'],
    ['未解决问题', state?.unresolvedQuestions?.join('；') || state?.currentMysteries?.join('；') || '暂无'],
    ['活跃伏笔', state?.activeForeshadows?.join('；') || (foreshadowCount === 0 ? '暂无' : `${foreshadowCount ?? '—'} 个`)],
  ];

  return <section className="border-b border-surface-200 bg-surface-50/80 px-4 py-2 text-xs dark:border-surface-700 dark:bg-surface-850/80" data-testid="chapter-context-bar">
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
      <span className="font-semibold text-surface-800 dark:text-surface-100">{chapter?.title || '当前章节'}</span>
      <span className="text-surface-500" title={info.description}>文风：{info.label}</span>
      <button type="button" className="ml-auto inline-flex shrink-0 items-center gap-1 text-primary-600 hover:text-primary-700" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded} data-testid="chapter-context-toggle">
        故事状态 <ChevronDown size={14} className={expanded ? 'rotate-180' : ''} />
      </button>
    </div>
    {expanded && <div className="mt-2 grid gap-2 border-t border-surface-200 pt-2 sm:grid-cols-2 dark:border-surface-700" data-testid="story-state-readonly">
      <span className="inline-flex items-start gap-1 text-surface-500"><Flag size={12} className="shrink-0" />本章目标：{goal}</span>
      <span className="inline-flex items-start gap-1 text-surface-500"><Users size={12} className="shrink-0" />人物：{characters.length ? characters.map((item) => item.name).join('、') : '暂无'}</span>
      <span className="text-surface-500">本章冲突：{conflict}</span>
      {foreshadowCount !== null && <span className="inline-flex items-center gap-1 text-surface-500"><Sparkles size={12} />活跃伏笔：{foreshadowCount}</span>}
      {stateItems.map(([label, value]) => <div key={label}><span className="text-surface-400">{label}</span><p className="mt-0.5 text-surface-700 dark:text-surface-200">{value}</p></div>)}
      <p className="text-surface-400 sm:col-span-2">文风适合：{info.scenes}。{info.label === '细腻情感 / 共鸣型' ? ' 留白、动作和潜台词同样重要，不要求每段都写心理活动。' : ''}</p>
    </div>}
  </section>;
}
