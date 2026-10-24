import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BookOpen,
  MoreHorizontal,
  Pencil,
  Pin,
  Plus,
  Search,
  Star,
  StarOff,
  Trash2,
} from 'lucide-react';

import { projectApi } from '../services/api';
import { useProjectStore } from '../stores';
import { Badge, Button, Card, Input, Modal, Select, Textarea, useToast } from '../components/common';
import { GENRE_LABELS } from '../types';
import type { Genre, Project } from '../types';

const STYLE_OPTIONS = [
  { key: 'concise', label: '现实 / 克制' },
  { key: 'intense', label: '爽快 / 商业' },
  { key: 'poetic', label: '悬疑 / 冷峻' },
  { key: 'lightNovel', label: '轻快 / 轻小说' },
  { key: 'epic', label: '古典 / 文学' },
  { key: 'emotional_resonance', label: '细腻情感 / 共鸣型' },
];

const STATUS_LABELS: Record<string, string> = {
  planning: '筹备中',
  drafting: '创作中',
  revising: '修订中',
  completed: '已完结',
};

const STATUS_COLORS: Record<string, string> = {
  planning: 'bg-warning-soft text-warning dark:bg-warning-soft-dark dark:text-warning-dark',
  drafting: 'bg-info-soft text-info dark:bg-info-soft-dark dark:text-info-dark',
  revising: 'bg-purple-50 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  completed: 'bg-success-soft text-success dark:bg-success-soft-dark dark:text-success-dark',
};

function formatGenreLabel(genre: string): string {
  if (genre.includes(',')) {
    return genre
      .split(',')
      .map((item) => {
        const key = item.trim() as Genre;
        return GENRE_LABELS[key] || item.trim();
      })
      .join(' / ');
  }

  return GENRE_LABELS[genre as Genre] || genre;
}

function formatWordCount(value: number): string {
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万字`;
  return `${value} 字`;
}

export default function Dashboard() {
  const navigate = useNavigate();
  const projects = useProjectStore((state) => state.projects);
  const setProjects = useProjectStore((state) => state.setProjects);
  const updateProject = useProjectStore((state) => state.updateProject);
  const deleteProjectFromStore = useProjectStore((state) => state.deleteProject);

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;

    projectApi.list()
      .then((json) => {
        if (!active) return;
        setProjects(json.data || []);
      })
      .catch((loadError) => {
        if (!active) return;
        setError(loadError instanceof Error ? loadError.message : '项目加载失败。');
      });

    return () => {
      active = false;
    };
  }, [setProjects]);

  const filteredProjects = useMemo(
    () => projects.filter((project) => {
      const query = searchQuery.trim().toLowerCase();
      if (!query) return true;

      return (
        project.name.toLowerCase().includes(query)
        || project.subtitle.toLowerCase().includes(query)
        || project.tags.some((tag) => tag.toLowerCase().includes(query))
      );
    }),
    [projects, searchQuery],
  );

  const handleToggleStar = async (project: Project) => {
    try {
      const json = await projectApi.update(project.id, { isStarred: !project.isStarred } as Partial<Project>);
      updateProject(project.id, json.data);
    } catch (toggleError) {
      setError(toggleError instanceof Error ? toggleError.message : '项目更新失败。');
    } finally {
      setMenuOpenId(null);
    }
  };

  const handleDelete = async (project: Project) => {
    if (!window.confirm(`确认删除《${project.name}》吗？此操作无法撤销。`)) return;

    try {
      await projectApi.delete(project.id);
      deleteProjectFromStore(project.id);
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : '项目删除失败。');
    } finally {
      setMenuOpenId(null);
    }
  };

  return (
    <div className="flex-1 overflow-auto" data-testid="dashboard-page">
      <div className="mx-auto max-w-5xl px-8 py-8">
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-surface-900 dark:text-surface-100">项目总览</h1>
            <p className="mt-1 text-sm text-surface-500">当前共有 {projects.length} 个项目</p>
          </div>
          <Button onClick={() => setShowCreateModal(true)} data-testid="open-create-project-modal">
            <Plus size={18} />
            新建项目
          </Button>
        </div>

        <div className="relative mb-6">
          <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-surface-400" />
          <Input
            className="pl-10"
            placeholder="按书名、简介或标签搜索"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            data-testid="project-search-input"
          />
        </div>

        {error && (
          <Card className="mb-4 border-error bg-error-soft text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark">
            {error}
          </Card>
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredProjects.map((project) => (
            <Card
              key={project.id}
              clickable
              className="group"
              onClick={() => navigate(`/project/${project.id}`)}
              data-testid={`project-card-${project.id}`}
            >
              <div className="mb-3 flex items-start justify-between">
                <div className="flex items-center gap-2">
                  <BookOpen size={20} className="text-primary-600" />
                  <Badge>{formatGenreLabel(project.genre)}</Badge>
                </div>

                <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  {project.isStarred && <Star size={14} className="fill-warning text-warning" />}

                  <div className="relative">
                    <button
                      className="btn-ghost p-1"
                      onClick={(event) => {
                        event.stopPropagation();
                        setMenuOpenId(menuOpenId === project.id ? null : project.id);
                      }}
                    >
                      <MoreHorizontal size={14} />
                    </button>

                    {menuOpenId === project.id && (
                      <div
                        className="absolute right-0 top-7 z-50 w-40 rounded-lg border border-surface-200 bg-white py-1 shadow-xl dark:border-surface-700 dark:bg-surface-800"
                        onClick={(event) => event.stopPropagation()}
                      >
                        <button
                          className="w-full px-3 py-2 text-left text-sm text-surface-700 transition-colors hover:bg-surface-100 dark:text-surface-300 dark:hover:bg-surface-700"
                          onClick={() => void handleToggleStar(project)}
                        >
                          <span className="flex items-center gap-2">
                            {project.isStarred ? <StarOff size={14} /> : <Star size={14} />}
                            {project.isStarred ? '取消星标' : '设为星标'}
                          </span>
                        </button>

                        <button
                          className="w-full px-3 py-2 text-left text-sm text-surface-700 transition-colors hover:bg-surface-100 dark:text-surface-300 dark:hover:bg-surface-700"
                          onClick={() => {
                            navigate(`/project/${project.id}`);
                            setMenuOpenId(null);
                          }}
                        >
                          <span className="flex items-center gap-2">
                            <Pencil size={14} />
                            打开项目
                          </span>
                        </button>

                        <button
                          className="w-full px-3 py-2 text-left text-sm text-surface-700 transition-colors hover:bg-surface-100 dark:text-surface-300 dark:hover:bg-surface-700"
                          onClick={() => {
                            updateProject(project.id, { updatedAt: new Date().toISOString().slice(0, 10) });
                            setMenuOpenId(null);
                          }}
                        >
                          <span className="flex items-center gap-2">
                            <Pin size={14} />
                            置顶项目
                          </span>
                        </button>

                        <hr className="my-1 border-surface-200 dark:border-surface-700" />

                        <button
                          className="w-full px-3 py-2 text-left text-sm text-error transition-colors hover:bg-error-soft dark:hover:bg-error-soft-dark"
                          onClick={() => void handleDelete(project)}
                        >
                          <span className="flex items-center gap-2">
                            <Trash2 size={14} />
                            删除项目
                          </span>
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>

              <h3 className="mb-1 text-lg font-semibold">{project.name}</h3>
              <p className="mb-3 line-clamp-2 text-sm text-surface-500">{project.subtitle || '还没有项目简介。'}</p>

              <div className="mb-3">
                <div className="mb-1 flex justify-between text-xs text-surface-500">
                  <span>当前 {formatWordCount(project.currentWordCount)}</span>
                  <span>目标 {formatWordCount(project.targetWordCount)}</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-surface-200 dark:bg-surface-700">
                  <div
                    className="h-full rounded-full bg-primary-500 transition-all"
                    style={{ width: `${Math.min((project.currentWordCount / Math.max(project.targetWordCount, 1)) * 100, 100)}%` }}
                  />
                </div>
              </div>

              <div className="flex items-center justify-between">
                <Badge className={STATUS_COLORS[project.status] || STATUS_COLORS.planning}>
                  {STATUS_LABELS[project.status] || project.status}
                </Badge>
                <span className="text-xs text-surface-400">{project.updatedAt}</span>
              </div>
            </Card>
          ))}

          <button
            onClick={() => setShowCreateModal(true)}
            className="card flex min-h-[200px] flex-col items-center justify-center gap-3 border-2 border-dashed border-surface-300 text-surface-400 transition-all hover:border-primary-400 hover:text-primary-500 dark:border-surface-600 dark:hover:border-primary-500"
            data-testid="create-project-card"
          >
            <Plus size={32} />
            <span className="font-medium">创建新的创作项目</span>
          </button>
        </div>
      </div>

      {showCreateModal && <CreateProjectModal onClose={() => setShowCreateModal(false)} />}
    </div>
  );
}

function CreateProjectModal({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const addProject = useProjectStore((state) => state.addProject);
  const setCurrentProject = useProjectStore((state) => state.setCurrentProject);

  const [name, setName] = useState('');
  const [premise, setPremise] = useState('');
  const [protagonistName, setProtagonistName] = useState('');
  const [genres, setGenres] = useState<Genre[]>([]);
  const [customGenre, setCustomGenre] = useState('');
  const [targetWords, setTargetWords] = useState(300000);
  const [targetChapters, setTargetChapters] = useState(100);
  const [stylePresets, setStylePresets] = useState<string[]>([]);
  const [customStyle, setCustomStyle] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');
  const toast = useToast();

  const toggleGenre = (genre: Genre) => {
    setGenres((previous) => (
      previous.includes(genre)
        ? previous.filter((item) => item !== genre)
        : [...previous, genre]
    ));
  };

  const toggleStyle = (style: string) => {
    setStylePresets((previous) => {
      if (previous.includes(style)) return previous.filter((item) => item !== style);
      if (previous.length >= 3) return previous;
      return [...previous, style];
    });
  };

  const addCustomStyle = () => {
    const value = customStyle.trim();
    if (!value || stylePresets.length >= 3) return;

    const key = `custom:${value}`;
    if (!stylePresets.includes(key)) {
      setStylePresets((previous) => [...previous, key]);
    }

    setCustomStyle('');
  };

  const handleCreate = async () => {
    if (!name.trim()) return;

    setIsSubmitting(true);
    setError('');

    const genreValue = genres.length > 0 ? genres.join(',') : (customGenre.trim() || 'xuanhuan');
    const styleValue = stylePresets.length > 0
      ? stylePresets.map((item) => (item.startsWith('custom:') ? item.slice(7) : item)).join(',')
      : (customStyle.trim() || 'epic');

    try {
      const json = await projectApi.create({
        name: name.trim(),
        subtitle: premise.trim(),
        genre: genreValue,
        targetWordCount: targetWords,
        premise: premise.trim(),
        protagonistName: protagonistName.trim(),
        targetChapters,
        stylePreset: styleValue.split(',')[0],
      } as never);

      const project = json.data;
      addProject(project);
      setCurrentProject(project.id);
      toast.success('项目已创建', `《${project.name}》已就绪，开始创作吧。`);
      navigate(`/project/${project.id}?new=true&premise=${encodeURIComponent(premise)}`);
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : '项目创建失败。');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="新建项目"
      width="sm"
      data-testid="create-project-modal"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>取消</Button>
          <Button
            onClick={() => void handleCreate()}
            disabled={!name.trim() || isSubmitting}
            loading={isSubmitting}
            data-testid="submit-create-project"
          >
            创建并开始写作
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Input
          label="书名"
          placeholder="给这本小说起个名字"
          value={name}
          onChange={(event) => setName(event.target.value)}
          data-testid="create-project-name"
        />

        <Textarea
          label="核心设定"
          className="h-28 resize-none"
          placeholder="描述主要冲突、关键人物关系和你想要的整体气质。"
          value={premise}
          onChange={(event) => setPremise(event.target.value)}
          data-testid="create-project-premise"
        />

        <Input
          label="主角（可选）"
          placeholder="先写下主角名字，其他角色可以稍后补充"
          value={protagonistName}
          onChange={(event) => setProtagonistName(event.target.value)}
          data-testid="create-project-protagonist"
        />

        <div>
          <label className="mb-1.5 block text-sm font-medium">题材</label>
          <div className="grid grid-cols-4 gap-2">
            {(Object.entries(GENRE_LABELS) as [Genre, string][]).map(([key, label]) => (
              <button
                key={key}
                onClick={() => toggleGenre(key)}
                className={`rounded-lg px-2 py-2 text-xs font-medium transition-all ${
                  genres.includes(key)
                    ? 'bg-primary-600 text-white'
                    : 'bg-surface-100 text-surface-600 hover:bg-surface-200 dark:bg-surface-700 dark:text-surface-300'
                }`}
                data-testid={`genre-option-${key}`}
              >
                {label}
              </button>
            ))}
          </div>

          <Input
            className="mt-2 text-sm"
            placeholder="自定义题材"
            value={customGenre}
            onChange={(event) => setCustomGenre(event.target.value)}
            data-testid="custom-genre-input"
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <Select
            label="目标字数"
            value={targetWords}
            onChange={(event) => setTargetWords(Number(event.target.value))}
            data-testid="target-word-count"
          >
            <option value={100000}>10 万字</option>
            <option value={300000}>30 万字</option>
            <option value={500000}>50 万字</option>
            <option value={1000000}>100 万字</option>
            <option value={2000000}>200 万字</option>
          </Select>

          <Select
            label="目标章节数"
            value={targetChapters}
            onChange={(event) => setTargetChapters(Number(event.target.value))}
            data-testid="target-chapter-count"
          >
            <option value={50}>50 章</option>
            <option value={100}>100 章</option>
            <option value={200}>200 章</option>
            <option value={500}>500 章以上</option>
          </Select>
        </div>

        <div>
          <label className="mb-1.5 block text-sm font-medium">风格预设（最多 3 个）</label>
          <div className="grid grid-cols-4 gap-2">
            {STYLE_OPTIONS.map((style) => (
              <button
                key={style.key}
                onClick={() => toggleStyle(style.key)}
                className={`rounded-lg px-2 py-2 text-xs font-medium transition-all ${
                  stylePresets.includes(style.key)
                    ? 'bg-primary-600 text-white'
                    : 'bg-surface-100 text-surface-600 hover:bg-surface-200 dark:bg-surface-700 dark:text-surface-300'
                }`}
                data-testid={`style-option-${style.key}`}
              >
                {style.label}
              </button>
            ))}
          </div>

          <div className="mt-2 flex gap-2">
            <Input
              className="flex-1 text-sm"
              placeholder="补充自定义风格"
              value={customStyle}
              onChange={(event) => setCustomStyle(event.target.value)}
              data-testid="custom-style-input"
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  addCustomStyle();
                }
              }}
            />
            <Button
              onClick={addCustomStyle}
              disabled={!customStyle.trim() || stylePresets.length >= 3}
              className="shrink-0"
              data-testid="add-custom-style"
            >
              添加
            </Button>
          </div>
        </div>

        {error && <p className="text-sm text-error">{error}</p>}
      </div>
    </Modal>
  );
}
