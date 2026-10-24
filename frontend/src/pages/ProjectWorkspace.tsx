import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import {
  BarChart3,
  BookOpen,
  ClipboardList,
  FileText,
  Flag,
  FolderOpen,
  Globe,
  LayoutGrid,
  Lightbulb,
  Loader2,
  Menu,
  MessageCircle,
  Network,
  ShieldCheck,
  Users,
  X,
} from 'lucide-react';

import ReferenceAnalysis from '../components/analysis/ReferenceAnalysis';
import AIChatPanel from '../components/chat/AIChatPanel';
import CharacterPanel from '../components/characters/CharacterPanel';
import { ErrorBoundary, Modal, useToast } from '../components/common';
import MarkdownEditor from '../components/editor/MarkdownEditor';
import { chapterMutationBlockReason } from '../components/editor/chapterEvents';
import ForeshadowPanel from '../components/foreshadow/ForeshadowPanel';
import InspirationBoard from '../components/inspiration/InspirationBoard';
import MarkdownImport from '../components/import/MarkdownImport';
import { setImportHubMode, useImportHubMode } from '../components/import/importStore';
import WordImport from '../components/import/WordImport';
import ChapterTree from '../components/outline/ChapterTree';
import OutlineGenerator from '../components/outline/OutlineGenerator';
import ReviewPanel from '../components/review/ReviewPanel';
import WritingStats from '../components/stats/WritingStats';
import TaskCenter from '../components/tasks/TaskCenter';
import WorldBuilder from '../components/world/WorldBuilder';
import { projectApi } from '../services/api';
import { normalizeProject } from '../services/normalize';
import { useEditorStore, useProjectStore } from '../stores';
import type { Project } from '../types';

type TabId =
  | 'outline'
  | 'write'
  | 'characters'
  | 'chat'
  | 'inspiration'
  | 'review'
  | 'import'
  | 'world'
  | 'reference'
  | 'tasks'
  | 'foreshadow'
  | 'stats';

const PRIMARY_TABS = [
  { id: 'outline' as const, label: '大纲', icon: Network },
  { id: 'write' as const, label: '正文', icon: FileText },
  { id: 'characters' as const, label: '人物', icon: Users },
  { id: 'chat' as const, label: '助手', icon: MessageCircle },
  { id: 'inspiration' as const, label: '灵感', icon: Lightbulb },
];

const TOOL_TABS = [
  { id: 'review' as const, label: '审校', icon: ShieldCheck },
  { id: 'import' as const, label: '导入', icon: FolderOpen },
  { id: 'world' as const, label: '世界观', icon: Globe },
  { id: 'reference' as const, label: '参考分析', icon: BookOpen },
  { id: 'tasks' as const, label: '任务', icon: ClipboardList },
  { id: 'foreshadow' as const, label: '伏笔', icon: Flag },
  { id: 'stats' as const, label: '统计', icon: BarChart3 },
];

// 手机端菜单用：全部 12 个 tab（5 个主 tab + 7 个工具 tab）。
const ALL_TABS = [...PRIMARY_TABS, ...TOOL_TABS];

export default function ProjectWorkspace() {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams] = useSearchParams();
  const [showRightPanel, setShowRightPanel] = useState(() => (
    typeof window === 'undefined' ? true : window.innerWidth >= 1280
  ));
  const [showChapterPanel, setShowChapterPanel] = useState(false);
  const [activeTab, setActiveTab] = useState<TabId>('write');
  // 手机端把 12 个 tab 收进菜单（桌面端保持原来的横向 tab 条）。
  const [tabMenuOpen, setTabMenuOpen] = useState(false);
  const [remoteProject, setRemoteProject] = useState<Project | null>(null);
  const [isLoadingProject, setIsLoadingProject] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [isDesktopWorkspace, setIsDesktopWorkspace] = useState(() => (
    typeof window === 'undefined' ? true : window.innerWidth >= 1024
  ));
  const [isWideWorkspace, setIsWideWorkspace] = useState(() => (
    typeof window === 'undefined' ? true : window.innerWidth >= 1280
  ));
  const initialTabSetRef = useRef(false);

  const projects = useProjectStore((state) => state.projects);
  const addProject = useProjectStore((state) => state.addProject);
  const updateProject = useProjectStore((state) => state.updateProject);
  const editorActiveProjectId = useEditorStore((state) => state.activeProjectId);
  const outlineData = useEditorStore((state) => state.outlineData);
  const setOutlineData = useEditorStore((state) => state.setOutlineData);
  const { warning } = useToast();

  const requestTabChange = useCallback((nextTab: TabId) => {
    if (nextTab !== activeTab && activeTab === 'write' && projectId) {
      const reason = chapterMutationBlockReason(projectId, 'unmount');
      if (reason) {
        warning('暂不能切换视图', reason);
        return;
      }
    }
    setActiveTab(nextTab);
  }, [activeTab, projectId, warning]);

  const currentProject = useMemo(
    () => projects.find((project) => project.id === projectId) || remoteProject,
    [projects, projectId, remoteProject],
  );

  useEffect(() => {
    if (!projectId) return;
    useProjectStore.getState().setCurrentProject(projectId);
    useEditorStore.getState().setActiveProject(projectId);
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;

    let active = true;
    setIsLoadingProject(true);
    setLoadError('');

    projectApi.get(projectId)
      .then((json) => {
        if (!active || !json.data) return;
        const normalizedProject = normalizeProject(json.data);
        setRemoteProject(normalizedProject);
        const exists = useProjectStore.getState().projects.some((project) => project.id === normalizedProject.id);
        if (exists) updateProject(normalizedProject.id, normalizedProject);
        else addProject(normalizedProject);
        setOutlineData(normalizedProject.outline || null);
      })
      .catch((error) => {
        if (!active) return;
        setLoadError(error instanceof Error ? error.message : '项目加载失败。');
      })
      .finally(() => {
        if (active) setIsLoadingProject(false);
      });

    return () => {
      active = false;
    };
  }, [projectId, addProject, updateProject, setOutlineData]);

  useEffect(() => {
    if (!projectId) return;

    let active = true;
    const refreshProject = (event: Event) => {
      const detail = (event as CustomEvent<{ projectId?: string }>).detail;
      if (detail?.projectId && detail.projectId !== projectId) return;
      projectApi.get(projectId).then((json) => {
        if (!active || !json.data) return;
        const normalizedProject = normalizeProject(json.data);
        setRemoteProject(normalizedProject);
        const exists = useProjectStore.getState().projects.some((project) => project.id === normalizedProject.id);
        if (exists) updateProject(normalizedProject.id, normalizedProject);
        else addProject(normalizedProject);
        setOutlineData(normalizedProject.outline || null);
      }).catch((error) => {
        if (!active) return;
        setLoadError(error instanceof Error ? error.message : '项目刷新失败。');
      });
    };

    window.addEventListener('novelcraft:project-refresh', refreshProject);
    return () => {
      active = false;
      window.removeEventListener('novelcraft:project-refresh', refreshProject);
    };
  }, [projectId, addProject, updateProject, setOutlineData]);

  const isEmpty = !currentProject || (
    currentProject.currentWordCount === 0
    && !currentProject.outline
    && !outlineData
  );

  useEffect(() => {
    initialTabSetRef.current = false;
    setActiveTab('write');
  }, [projectId]);

  useEffect(() => {
    if (initialTabSetRef.current) return;
    if (isLoadingProject) return;
    // 【未落地】原计划在这里读回「上次所在的页签」（sessionStorage，按项目记），
    // 让刷新/被浏览器丢弃后回到离开时那一页。实测它会与一个**既有竞态**打架：
    // 加载期间作者手动点的页签会被这个「初始决定」覆盖回去
    // （最小复现：同一页面里连续 goto 项目、每次先点「正文」，带上记忆后
    // `copilot-accept.spec.ts` 稳定失败；关掉记忆即 11 passed）。
    // 要修得先把「初始页签不得覆盖加载期间的手动选择」处理掉，见 CHANGELOG「仍未修」。
    setActiveTab(isEmpty ? 'outline' : 'write');
    initialTabSetRef.current = true;
  }, [isEmpty, isLoadingProject]);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const syncLayout = () => {
      const desktop = window.innerWidth >= 1024;
      const wide = window.innerWidth >= 1280;
      setIsDesktopWorkspace(desktop);
      setIsWideWorkspace(wide);
      if (desktop) setShowChapterPanel(false);
      if (!wide) setShowRightPanel(false);
    };

    syncLayout();
    window.addEventListener('resize', syncLayout);
    return () => window.removeEventListener('resize', syncLayout);
  }, []);

  if (!projectId) {
    return <div className="flex flex-1 items-center justify-center text-sm text-surface-500">项目地址无效。</div>;
  }

  const editorProjectReady = editorActiveProjectId === projectId;
  const projectName = currentProject?.name || searchParams.get('name') || '未命名项目';
  const premise = currentProject?.premise || currentProject?.subtitle || searchParams.get('premise') || '';

  if (isLoadingProject && !currentProject) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 text-sm text-surface-500">
        <Loader2 size={16} className="animate-spin" />
        正在加载项目...
      </div>
    );
  }

  if (loadError && !currentProject) {
    return (
      <div className="flex flex-1 items-center justify-center px-6">
        <div className="max-w-md rounded-lg border border-error bg-error-soft px-4 py-3 text-sm text-error dark:border-error/40 dark:bg-error-soft-dark dark:text-error-dark">
          {loadError}
        </div>
      </div>
    );
  }

  const handleChapterSelect = () => {
    setActiveTab('write');
    if (!isDesktopWorkspace) setShowChapterPanel(false);
  };

  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-testid="workspace-root">
      <div className="relative z-20 flex shrink-0 items-center gap-2 border-b border-surface-200 bg-white px-2 dark:border-surface-700 dark:bg-surface-850">
        <button
          onClick={() => setShowChapterPanel(true)}
          className="btn-ghost shrink-0 px-2.5 lg:hidden"
          data-testid="toggle-chapter-panel"
          title="查看章节导航"
        >
          <BookOpen size={16} />
        </button>

        <div className="hidden min-w-0 flex-1 items-center overflow-x-auto scrollbar-none lg:flex">
          {PRIMARY_TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => requestTabChange(tab.id)}
              className={`shrink-0 flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm font-medium transition-all ${
                activeTab === tab.id
                  ? 'border-primary-600 text-primary-600'
                  : 'border-transparent text-surface-500 hover:text-surface-700 dark:hover:text-surface-300'
              }`}
              data-testid={`workspace-tab-${tab.id}`}
            >
              <tab.icon size={16} />
              {tab.label}
            </button>
          ))}

          <span className="mx-1 h-5 shrink-0 w-px bg-surface-300 dark:bg-surface-600" />

          {TOOL_TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => requestTabChange(tab.id)}
              className={`shrink-0 flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm font-medium transition-all ${
                activeTab === tab.id
                  ? 'border-primary-600 text-primary-600'
                  : 'border-transparent text-surface-500 hover:text-surface-700 dark:hover:text-surface-300'
              }`}
              data-testid={`workspace-tab-${tab.id}`}
            >
              <tab.icon size={16} />
              {tab.label}
            </button>
          ))}
        </div>

        {/* 手机端：12 个 tab 收进菜单，不再横滑。与上面那条 `hidden lg:flex` 的横滑条互斥。 */}
        <button
          type="button"
          onClick={() => setTabMenuOpen(true)}
          className="btn-ghost flex min-w-0 flex-1 items-center justify-center gap-1.5 lg:hidden"
          data-testid="open-tab-menu"
          aria-haspopup="dialog"
        >
          <LayoutGrid size={16} />
          <span className="max-w-[46vw] truncate">{ALL_TABS.find((tab) => tab.id === activeTab)?.label}</span>
        </button>

        <button
          onClick={() => setShowRightPanel((previous) => !previous)}
          className="btn-ghost shrink-0"
          data-testid="toggle-right-panel"
          title={showRightPanel ? '隐藏右侧助手' : '显示右侧助手'}
        >
          {showRightPanel ? <X size={16} /> : <MessageCircle size={16} />}
        </button>
      </div>

      <Modal
        open={tabMenuOpen}
        onClose={() => setTabMenuOpen(false)}
        title="切换视图"
        data-testid="tab-menu-dialog"
      >
        <div className="grid grid-cols-2 gap-2">
          {ALL_TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => {
                requestTabChange(tab.id);
                setTabMenuOpen(false);
              }}
              className={`flex min-h-[52px] items-center gap-2 rounded-lg border px-3 text-sm font-medium transition-colors ${
                activeTab === tab.id
                  ? 'border-primary-500 bg-primary-50 text-primary-700 dark:bg-primary-900/40 dark:text-primary-200'
                  : 'border-surface-200 text-surface-700 hover:border-primary-300 dark:border-surface-700 dark:text-surface-300'
              }`}
              data-testid={`tab-menu-item-${tab.id}`}
            >
              <tab.icon size={18} />
              <span className="truncate">{tab.label}</span>
            </button>
          ))}
        </div>
      </Modal>

      <div className="flex min-h-0 flex-1 overflow-hidden">
      <aside className="hidden w-60 shrink-0 border-r border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-850 lg:block">
        <div className="border-b border-surface-200 p-3 dark:border-surface-700">
          <h2 className="text-sm font-semibold text-surface-800 dark:text-surface-200">{projectName}</h2>
          <p className="mt-0.5 text-xs text-surface-400">章节与卷册导航</p>
        </div>
        <div className="h-[calc(100%-57px)] overflow-auto">
          <ChapterTree
            projectId={projectId}
            isEmpty={isEmpty}
            outlineData={outlineData || currentProject?.outline || null}
            isProjectReady={editorProjectReady}
            onChapterSelect={handleChapterSelect}
          />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex-1 overflow-auto">
          {/* 中栏兜底：面板渲染抛错时就地显示原因，而不是让中间一片空白 */}
          <ErrorBoundary variant="panel">
            {activeTab === 'outline' && (
              <OutlineGenerator projectId={projectId} isEmpty={isEmpty} onOutlineGenerated={setOutlineData} />
            )}
          {activeTab === 'write' && (
            <MarkdownEditor
              projectId={projectId}
              isEmpty={isEmpty}
              outlineData={outlineData || currentProject?.outline || null}
              premise={premise}
              isProjectReady={editorProjectReady}
                onNavigateToTasks={() => requestTabChange('tasks')}
            />
          )}
          {activeTab === 'characters' && (
            <CharacterPanel projectId={projectId} isEmpty={isEmpty} project={currentProject} />
          )}
          {activeTab === 'chat' && <AIChatPanel projectId={projectId} />}
          {activeTab === 'inspiration' && <InspirationBoard projectId={projectId} project={currentProject} />}
          {activeTab === 'review' && <ReviewPanel projectId={projectId} />}
          {activeTab === 'import' && <ImportHub projectId={projectId} isEmpty={isEmpty} />}
          {activeTab === 'world' && <WorldBuilder projectId={projectId} />}
          {activeTab === 'reference' && (
            <ReferenceAnalysis projectId={projectId} projectName={currentProject?.name || ''} />
          )}
          {activeTab === 'tasks' && (
            <TaskCenter
              projectId={projectId}
              outlineData={outlineData || currentProject?.outline || null}
              onGoToChapter={(chapterId) => {
                const currentChapterId = useEditorStore.getState().currentChapterId;
                if (chapterId !== currentChapterId) {
                  const reason = chapterMutationBlockReason(projectId);
                  if (reason) {
                    warning('暂不能切换章节', reason);
                    return;
                  }
                }
                useEditorStore.getState().setCurrentChapter(chapterId);
                requestTabChange('write');
              }}
              onGoToWrite={() => requestTabChange('write')}
            />
          )}
          {activeTab === 'foreshadow' && <ForeshadowPanel projectId={projectId} />}
          {activeTab === 'stats' && <WritingStats projectId={projectId} isEmpty={isEmpty} />}
          </ErrorBoundary>
        </div>
      </div>

      {showRightPanel && isWideWorkspace && (
        <aside className="hidden w-80 shrink-0 border-l border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-850 xl:block">
          <AIChatPanel projectId={projectId} compact />
        </aside>
      )}
      </div>

      {showChapterPanel && !isDesktopWorkspace && (
        <div className="absolute inset-x-0 bottom-0 top-[43px] z-30 flex lg:hidden" data-testid="chapter-panel-drawer">
          <aside className="relative z-10 h-full w-[min(22rem,90vw)] border-r border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-850">
            <div className="flex items-center justify-between border-b border-surface-200 p-3 dark:border-surface-700">
              <div>
                <h2 className="text-sm font-semibold text-surface-800 dark:text-surface-200">{projectName}</h2>
                <p className="mt-0.5 text-xs text-surface-400">章节与卷册导航</p>
              </div>
              <button className="btn-ghost px-2.5" onClick={() => setShowChapterPanel(false)} aria-label="关闭章节导航">
                <X size={16} />
              </button>
            </div>
            <div className="h-[calc(100%-61px)] overflow-auto">
              <ChapterTree
                projectId={projectId}
                isEmpty={isEmpty}
                outlineData={outlineData || currentProject?.outline || null}
                isProjectReady={editorProjectReady}
                onChapterSelect={handleChapterSelect}
              />
            </div>
          </aside>
          <button
            className="flex-1 bg-surface-900/45"
            aria-label="关闭章节导航"
            onClick={() => setShowChapterPanel(false)}
          />
        </div>
      )}

      {showRightPanel && !isWideWorkspace && (
        <div className="absolute inset-x-0 bottom-0 top-[43px] z-30 flex xl:hidden" data-testid="assistant-panel-drawer">
          <button
            className="flex-1 bg-surface-900/45"
            aria-label="关闭右侧助手"
            onClick={() => setShowRightPanel(false)}
          />
          <aside className="relative z-10 h-full w-[min(24rem,100vw)] border-l border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-850">
            <div className="flex items-center justify-between border-b border-surface-200 px-3 py-2 dark:border-surface-700">
              <div>
                <h2 className="text-sm font-semibold text-surface-800 dark:text-surface-200">写作助手</h2>
                <p className="mt-0.5 text-xs text-surface-400">续写、拆解、审读与即时追问</p>
              </div>
              <button className="btn-ghost px-2.5" onClick={() => setShowRightPanel(false)} aria-label="关闭右侧助手">
                <X size={16} />
              </button>
            </div>
            <div className="h-[calc(100%-57px)]">
              <AIChatPanel projectId={projectId} compact />
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function ImportHub({ projectId, isEmpty }: { projectId: string; isEmpty?: boolean }) {
  // 子页签的选择也存在 store 里：切走再回来还是原来那一页
  const mode = useImportHubMode(projectId);
  const base = 'btn-ghost text-xs';

  return (
    <div className="flex h-full flex-col" data-testid="import-hub">
      <div className="flex items-center gap-1 border-b border-surface-200 bg-white px-4 py-2 dark:border-surface-700 dark:bg-surface-850">
        <button
          onClick={() => setImportHubMode(projectId, 'markdown')}
          className={`${base} ${mode === 'markdown' ? 'bg-surface-200 dark:bg-surface-700' : ''}`}
          data-testid="import-mode-markdown"
        >
          Markdown 章节
        </button>
        <button
          onClick={() => setImportHubMode(projectId, 'word')}
          className={`${base} ${mode === 'word' ? 'bg-surface-200 dark:bg-surface-700' : ''}`}
          data-testid="import-mode-word"
        >
          Word 建议书
        </button>
      </div>
      {mode === 'markdown' ? <MarkdownImport projectId={projectId} isEmpty={isEmpty} /> : <WordImport projectId={projectId} />}
    </div>
  );
}
