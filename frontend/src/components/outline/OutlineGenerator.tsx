import { useEffect, useMemo, useState } from 'react';
import { Check, ChevronDown, ChevronRight, Loader2, Network, Sparkles, Target } from 'lucide-react';

import { aiApi, projectApi } from '../../services/api';
import { useProjectStore } from '../../stores';
import { OUTLINE_STRUCTURE_LABELS, outlineStructureLabel } from '../../types';
import type { Outline, OutlineStructure } from '../../types';

/**
 * 选项的中文名统一取自 `OUTLINE_STRUCTURE_LABELS`，不在这里再写一遍——
 * 两处各存一份名字，迟早会漂移，而展示与选择器必须是同一套说法。
 */
const STRUCTURE_OPTIONS: Array<{ id: OutlineStructure; name: string; desc: string }> = (
  [
    { id: 'three_act', desc: '建立 -> 对抗 -> 解决' },
    { id: 'kishotenketsu', desc: '铺陈 -> 展开 -> 转折 -> 收束' },
    { id: 'hero_journey', desc: '适合成长、冒险与史诗故事' },
    { id: 'free', desc: '不预设固定骨架，灵活推进' },
  ] as Array<{ id: OutlineStructure; desc: string }>
).map((item) => ({ ...item, name: OUTLINE_STRUCTURE_LABELS[item.id] }));

export default function OutlineGenerator({
  projectId,
  isEmpty = false,
  onOutlineGenerated,
}: {
  projectId: string;
  isEmpty?: boolean;
  onOutlineGenerated?: (outline: Outline) => void;
}) {
  const project = useProjectStore((state) => state.projects.find((item) => item.id === projectId));
  const updateProject = useProjectStore((state) => state.updateProject);

  const [structure, setStructure] = useState<Outline['structure']>('three_act');
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState('');
  const [premise, setPremise] = useState('');
  const [outline, setOutline] = useState<Outline | null>(project?.outline || null);
  const [expandedArcs, setExpandedArcs] = useState<Set<string>>(new Set());

  useEffect(() => {
    setPremise(project?.premise || project?.subtitle || '');
    setOutline(project?.outline || null);
  }, [project]);

  const targetWords = project?.targetWordCount || 300000;
  const targetChapters = project?.targetChapters || 100;

  const volumeNodes = useMemo(
    () => outline?.nodes.filter((node) => node.type === 'volume') || [],
    [outline],
  );

  const chapterCount = outline?.nodes.filter((node) => node.type === 'chapter').length || 0;

  const handleGenerate = async () => {
    if (!premise.trim()) return;

    setIsGenerating(true);
    setError('');

    try {
      const json = await aiApi.generateOutline({
        projectId,
        premise: premise.trim(),
        genre: project?.genre || 'xuanhuan',
        targetWordCount: targetWords,
        targetChapters,
        structure,
      });

      if (json.meta?.mode !== 'llm') {
        const reason = json.meta?.error_code || 'unclassified_response';
        setError(`模型未完成本次大纲，已保留当前大纲（${reason}）。诊断编号：${json.meta?.trace_id || '无'}`);
        setOutline(project?.outline || null);
        return;
      }

      const nextOutline = json.data;
      setOutline(nextOutline);
      onOutlineGenerated?.(nextOutline);

      if (projectId) {
        const projectJson = await projectApi.update(projectId, {
          subtitle: premise.trim(),
          premise: premise.trim(),
          outline: nextOutline,
          targetChapters,
        });
        updateProject(projectId, projectJson.data);
      }
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : '大纲生成失败。');
    } finally {
      setIsGenerating(false);
    }
  };

  const getChapters = (volumeId: string) =>
    outline?.nodes.filter((node) => node.parentId === volumeId && node.type === 'chapter') || [];

  const getArcs = (volumeId: string) =>
    (outline?.nodes.filter((node) => node.parentId === volumeId && node.type === 'arc') || [])
      .sort((a, b) => a.order - b.order);

  const getArcChapters = (volumeId: string, arcId: string) =>
    getChapters(volumeId)
      .filter((chapter) => chapter.arcId === arcId)
      .sort((a, b) => a.order - b.order);

  const toggleArc = (arcId: string) => {
    setExpandedArcs((previous) => {
      const next = new Set(previous);
      if (next.has(arcId)) next.delete(arcId);
      else next.add(arcId);
      return next;
    });
  };

  return (
    <div className="flex-1 overflow-auto">
      <div className="mx-auto max-w-4xl p-8">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-bold">
              <Network size={22} className="text-primary-600" />
              大纲规划
            </h2>
            <p className="mt-1 text-sm text-surface-500">
              {outline
                ? '当前项目已关联大纲。'
                : '输入核心构想，生成完整的卷章结构。'}
            </p>
          </div>
          {outline && (
            <button onClick={() => setOutline(null)} className="btn-secondary text-sm">
              重新生成
            </button>
          )}
        </div>

        {!outline && (
          <div className="card space-y-6">
            <div>
              <label className="mb-2 block text-sm font-medium">核心构想</label>
              <textarea
                className="input-field h-32 resize-none"
                placeholder="描述主线冲突、人物处境和你想要的情绪氛围。"
                value={premise}
                onChange={(event) => setPremise(event.target.value)}
              />
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium">故事结构</label>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                {STRUCTURE_OPTIONS.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => setStructure(item.id)}
                    className={`rounded-xl border-2 p-3 text-left transition-all ${
                      structure === item.id
                        ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/20'
                        : 'border-surface-200 hover:border-surface-300 dark:border-surface-700'
                    }`}
                  >
                    <div className="text-sm font-medium">{item.name}</div>
                    <div className="mt-0.5 text-xs text-surface-500">{item.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4 text-sm text-surface-500">
              <div className="card bg-surface-50 dark:bg-surface-800">
                目标长度：{targetWords.toLocaleString()} 字
              </div>
              <div className="card bg-surface-50 dark:bg-surface-800">
                计划章节：{targetChapters} 章
              </div>
            </div>

            {error && <p className="text-sm text-error">{error}</p>}

            <div className="flex justify-end">
              <button
                onClick={() => void handleGenerate()}
                disabled={!premise.trim() || isGenerating}
                className="btn-primary flex items-center gap-2"
              >
                {isGenerating ? (
                  <>
                    <Loader2 size={18} className="animate-spin" />
                    生成中...
                  </>
                ) : (
                  <>
                    <Sparkles size={18} />
                    生成大纲
                  </>
                )}
              </button>
            </div>
          </div>
        )}

        {outline && (
          <div className="space-y-6">
            <div className="card border-primary-200 bg-primary-50/50 dark:border-primary-800 dark:bg-primary-900/10">
              <div className="flex items-start gap-3">
                <Target size={20} className="mt-0.5 shrink-0 text-primary-600" />
                <div>
                  <h3 className="text-sm font-semibold text-primary-800 dark:text-primary-300">主线摘要</h3>
                  <p className="mt-1 whitespace-pre-wrap text-sm text-primary-700 dark:text-primary-400">{outline.mainSummary}</p>
                </div>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-4">
              <div className="card text-center">
                <div className="text-xl font-bold text-primary-600">{volumeNodes.length}</div>
                <div className="mt-1 text-xs text-surface-400">卷数</div>
              </div>
              <div className="card text-center">
                <div className="text-xl font-bold text-primary-600">{chapterCount}</div>
                <div className="mt-1 text-xs text-surface-400">章节数</div>
              </div>
              <div className="card text-center">
                <div className="text-xl font-bold text-primary-600" data-testid="outline-structure-label">{outlineStructureLabel(outline.structure)}</div>
                <div className="mt-1 text-xs text-surface-400">结构</div>
              </div>
            </div>

            <div className="space-y-6">
              {volumeNodes.map((volume) => (
                <div key={volume.id} className="card">
                  <div className="mb-4">
                    <h3 className="text-lg font-bold">{volume.title}</h3>
                    <p className="mt-1 text-sm text-surface-500">{volume.summary}</p>
                    {(volume.conflict || volume.revelation || volume.emotion) && (
                      <div className="mt-2 flex flex-wrap gap-3 text-xs text-surface-400">
                        {volume.conflict && <span>冲突：{volume.conflict}</span>}
                        {volume.revelation && <span>揭示：{volume.revelation}</span>}
                        {volume.emotion && <span>情绪：{volume.emotion}</span>}
                      </div>
                    )}
                  </div>

                  <div className="space-y-2 border-t border-surface-200 pt-3 dark:border-surface-700">
                    {getArcs(volume.id).map((arc) => {
                      const arcChapters = getArcChapters(volume.id, arc.id);
                      const isExpanded = expandedArcs.has(arc.id);
                      return (
                        <div key={arc.id} className="border-b border-surface-200 pb-2 last:border-b-0 dark:border-surface-700">
                          <button
                            type="button"
                            onClick={() => toggleArc(arc.id)}
                            className="flex w-full items-start gap-2 py-2 text-left"
                            aria-expanded={isExpanded}
                          >
                            {isExpanded
                              ? <ChevronDown size={16} className="mt-0.5 shrink-0 text-surface-400" />
                              : <ChevronRight size={16} className="mt-0.5 shrink-0 text-surface-400" />}
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center justify-between gap-3">
                                <span className="text-sm font-semibold text-surface-700 dark:text-surface-200">{arc.title}</span>
                                <span className="shrink-0 text-xs text-surface-400">{arcChapters.length} 章</span>
                              </div>
                              <p className="mt-1 text-sm leading-6 text-surface-500">{arc.summary}</p>
                            </div>
                          </button>

                          {isExpanded && (
                            <div className="ml-6 border-l border-surface-200 pb-1 pl-4 dark:border-surface-700">
                              {arcChapters.map((chapter) => (
                                <div key={chapter.id} className="py-1.5 text-sm text-surface-600 dark:text-surface-400">
                                  {chapter.title}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>

            {!isEmpty && (
              <div className="card flex items-center gap-2 border-success bg-success-soft text-success dark:border-success/40 dark:bg-success-soft-dark dark:text-success-dark">
                <Check size={16} />
                大纲已经保存到当前项目，可以切回正文面板继续写作。
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
