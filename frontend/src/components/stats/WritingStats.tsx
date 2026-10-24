import { useEffect, useMemo, useState } from 'react';
import { BarChart3, BookOpen, Target, TrendingUp, Users } from 'lucide-react';

import { chapterApi, projectApi } from '../../services/api';
import type { Chapter, Project } from '../../types';

export default function WritingStats({ projectId }: { projectId: string; isEmpty?: boolean }) {
  const [project, setProject] = useState<Project | null>(null);
  const [chapters, setChapters] = useState<Chapter[]>([]);

  useEffect(() => {
    if (!projectId) return;
    let active = true;

    Promise.all([projectApi.get(projectId), chapterApi.list(projectId)])
      .then(([projectJson, chapterJson]) => {
        if (!active) return;
        setProject(projectJson.data);
        setChapters(chapterJson.data || []);
      })
      .catch(() => {
        if (!active) return;
        setProject(null);
        setChapters([]);
      });

    return () => {
      active = false;
    };
  }, [projectId]);

  const totalWords = project?.currentWordCount || chapters.reduce((sum, chapter) => sum + chapter.wordCount, 0);

  const characterAppearances = useMemo(() => {
    const counter: Record<string, number> = {};
    for (const character of project?.characters || []) {
      if (!character.name) continue;
      const pattern = new RegExp(character.name, 'g');
      let count = 0;

      for (const chapter of chapters) {
        count += chapter.content.match(pattern)?.length || 0;
      }

      if (count > 0) counter[character.name] = count;
    }

    return Object.entries(counter)
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 8);
  }, [project?.characters, chapters]);

  const chapterTrend = chapters.slice(0, 7).map((chapter) => ({
    day: `第${chapter.order}章`,
    words: chapter.wordCount,
  }));

  const maxWords = Math.max(...chapterTrend.map((item) => item.words), 1);
  const maxCharAppear = Math.max(...characterAppearances.map((item) => item.count), 1);

  if (!project && chapters.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="text-center" data-testid="stats-empty-state">
          <BarChart3 size={48} className="mx-auto mb-4 text-surface-300" />
          <h3 className="mb-2 text-lg font-semibold">暂无数据</h3>
          <p className="text-sm text-surface-500">开始创作后，写作数据会在这里逐步累积。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-auto">
      <div className="mx-auto max-w-5xl p-8">
        <h2 className="mb-6 flex items-center gap-2 text-xl font-bold">
          <BarChart3 size={22} className="text-primary-600" />
          写作数据看板
        </h2>

        <div className="mb-8 grid grid-cols-2 gap-4 md:grid-cols-4">
          {[
            { label: '总字数', value: totalWords.toLocaleString(), icon: BookOpen, color: 'text-info' },
            { label: '章节数', value: `${chapters.length}`, icon: TrendingUp, color: 'text-success' },
            { label: '人物数', value: `${project?.characters.length || 0}`, icon: Users, color: 'text-purple-500' },
            { label: '章均字数', value: `${chapters.length > 0 ? Math.round(totalWords / chapters.length) : 0}`, icon: Target, color: 'text-orange-500' },
          ].map((card) => (
            <div key={card.label} className="card text-center" data-testid={`stats-card-${card.label}`}>
              <card.icon size={20} className={`mx-auto mb-2 ${card.color}`} />
              <div className="text-2xl font-bold">{card.value}</div>
              <div className="mt-1 text-xs text-surface-400">{card.label}</div>
            </div>
          ))}
        </div>

        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <div className="card" data-testid="stats-chapter-trend">
            <h3 className="mb-4 flex items-center gap-2 text-sm font-semibold">
              <BarChart3 size={16} className="text-primary-600" />
              章节字数趋势
            </h3>
            <div className="flex h-40 items-end justify-between gap-2">
              {chapterTrend.map((item) => (
                <div key={item.day} className="flex flex-1 flex-col items-center gap-1">
                  <span className="text-xs font-medium text-surface-600 dark:text-surface-400">{item.words.toLocaleString()}</span>
                  <div
                    className="min-h-[4px] w-full rounded-t-md bg-primary-400 transition-all hover:bg-primary-500"
                    style={{ height: `${maxWords > 0 ? (item.words / maxWords) * 120 : 4}px` }}
                  />
                  <span className="text-[10px] text-surface-400">{item.day}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="card" data-testid="stats-chapter-distribution">
            <h3 className="mb-4 text-sm font-semibold">章节分布</h3>
            <div className="space-y-3">
              {chapters.map((chapter) => (
                <div key={chapter.id}>
                  <div className="mb-1 flex justify-between text-xs">
                    <span>{chapter.title}</span>
                    <span className="text-surface-400">{chapter.wordCount.toLocaleString()} 字</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700">
                    <div className="h-full rounded-full bg-primary-400" style={{ width: `${Math.min((chapter.wordCount / 6000) * 100, 100)}%` }} />
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="card" data-testid="stats-character-frequency">
            <h3 className="mb-4 text-sm font-semibold">人物出场频次</h3>
            <div className="space-y-3">
              {characterAppearances.length > 0 ? characterAppearances.map((character) => (
                <div key={character.name} className="flex items-center gap-3">
                  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-100 text-xs font-bold text-primary-700 dark:bg-primary-900/40">
                    {character.name[0]}
                  </div>
                  <span className="w-16 shrink-0 text-sm">{character.name}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700">
                    <div className="h-full rounded-full bg-primary-400" style={{ width: `${maxCharAppear > 0 ? (character.count / maxCharAppear) * 100 : 0}%` }} />
                  </div>
                  <span className="w-12 text-right text-xs text-surface-400">{character.count}</span>
                </div>
              )) : <p className="text-sm text-surface-400">当前正文内容不足，无法准确统计人物出场频次。</p>}
            </div>
          </div>

          <div className="card" data-testid="stats-writing-habits">
            <h3 className="mb-4 text-sm font-semibold">写作习惯</h3>
            <div className="space-y-4">
              <div className="flex items-center justify-between rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                <div className="flex items-center gap-2">
                  <Target size={16} className="text-surface-400" />
                  <span className="text-sm">章均字数</span>
                </div>
                <span className="font-semibold">{chapters.length > 0 ? Math.round(totalWords / chapters.length) : 0} 字</span>
              </div>

              <div className="flex items-center justify-between rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                <div className="flex items-center gap-2">
                  <TrendingUp size={16} className="text-success" />
                  <span className="text-sm">最长章节</span>
                </div>
                <span className="font-semibold">{chapters.length > 0 ? Math.max(...chapters.map((chapter) => chapter.wordCount)) : 0} 字</span>
              </div>

              <div className="flex items-center justify-between rounded-lg bg-surface-50 px-3 py-2 dark:bg-surface-800">
                <div className="flex items-center gap-2">
                  <BookOpen size={16} className="text-orange-500" />
                  <span className="text-sm">目标进度</span>
                </div>
                <span className="font-semibold">
                  {project?.targetWordCount ? `${Math.min(Math.round((totalWords / Math.max(project.targetWordCount, 1)) * 100), 100)}%` : '创作中'}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
