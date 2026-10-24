import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, FileText, Plus, Trash2 } from 'lucide-react';

import { chapterApi, chapterTrashApi } from '../../services/api';
import { useEditorStore } from '../../stores';
import type { Chapter, Outline, OutlineNode } from '../../types';
import { Modal } from '../common';
import ChapterTrash from './ChapterTrash';
import { CHAPTER_CHANGED_EVENT, chapterMutationBlockReason, notifyChapterChanged, type ChapterChangedDetail } from '../editor/chapterEvents';

interface TreeNode {
  id: string;
  title: string;
  type: 'volume' | 'arc' | 'chapter';
  children?: TreeNode[];
  wordCount?: number;
  hasContent?: boolean;
}

function outlineToTree(nodes: OutlineNode[]): TreeNode[] {
  const volumes = nodes.filter((node) => node.type === 'volume').sort((a, b) => a.order - b.order);

  if (volumes.length === 0) {
    return [{
      id: 'outline-chapters',
      title: '大纲章节',
      type: 'volume',
      children: nodes
        .filter((node) => node.type === 'chapter')
        .sort((a, b) => a.order - b.order)
        .map((chapter) => ({
          id: chapter.id,
          title: chapter.title,
          type: 'chapter' as const,
          wordCount: chapter.targetWordCount,
          hasContent: false,
        })),
    }];
  }

  return volumes.map((volume) => {
    const chapters = nodes
      .filter((node) => node.parentId === volume.id && node.type === 'chapter')
      .sort((a, b) => a.order - b.order);
    const arcs = nodes
      .filter((node) => node.parentId === volume.id && node.type === 'arc')
      .sort((a, b) => a.order - b.order);
    const chapterNode = (chapter: OutlineNode): TreeNode => ({
      id: chapter.id,
      title: chapter.title,
      type: 'chapter',
      wordCount: chapter.targetWordCount,
      hasContent: false,
    });
    const assignedChapterIds = new Set<string>();
    const arcNodes = arcs.map((arc): TreeNode => {
      const arcChapters = chapters.filter((chapter) => chapter.arcId === arc.id);
      arcChapters.forEach((chapter) => assignedChapterIds.add(chapter.id));
      return {
        id: arc.id,
        title: arc.title,
        type: 'arc',
        children: arcChapters.map(chapterNode),
      };
    });
    const unassigned = chapters.filter((chapter) => !assignedChapterIds.has(chapter.id)).map(chapterNode);
    return {
      id: volume.id,
      title: volume.title,
      type: 'volume' as const,
      wordCount: volume.targetWordCount,
      children: arcs.length > 0 ? [...arcNodes, ...unassigned] : unassigned,
    };
  });
}

export default function ChapterTree({
  projectId,
  isEmpty = false,
  outlineData = null,
  isProjectReady = true,
  onChapterSelect,
}: {
  projectId: string;
  isEmpty?: boolean;
  outlineData?: Outline | null;
  isProjectReady?: boolean;
  onChapterSelect?: (chapterId: string) => void;
}) {
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [trashCount, setTrashCount] = useState<number | null>(null);
  const [showTrash, setShowTrash] = useState(false);
  const [recycleTarget, setRecycleTarget] = useState<TreeNode | null>(null);
  const [recycling, setRecycling] = useState(false);
  const recyclingRef = useRef(false);
  const currentChapterId = useEditorStore((state) => state.currentChapterId);
  const setCurrentChapter = useEditorStore((state) => state.setCurrentChapter);

  useEffect(() => {
    if (!projectId || !isProjectReady) return;
    let active = true;
    setError('');

    chapterApi.list(projectId)
      .then((json) => {
        if (!active) return;
        const loadedChapters = json.data || [];
        setChapters(loadedChapters);
        if (!currentChapterId && loadedChapters.length > 0) {
          setCurrentChapter(loadedChapters[0].id);
        }
      })
      .catch((loadError) => {
        if (!active) return;
        setError(loadError instanceof Error ? loadError.message : '章节列表加载失败。');
      });

    return () => {
      active = false;
    };
  }, [currentChapterId, isProjectReady, projectId, revision, setCurrentChapter]);

  useEffect(() => {
    if (!projectId || !isProjectReady) return;
    let active = true;
    chapterTrashApi.list(projectId).then((json) => {
      if (active) setTrashCount(json.data?.records.length ?? 0);
    }).catch(() => { if (active) setTrashCount(null); });
    return () => { active = false; };
  }, [isProjectReady, projectId, revision]);

  useEffect(() => {
    const handleChange = (event: Event) => {
      if ((event as CustomEvent<ChapterChangedDetail>).detail.projectId === projectId) {
        setRevision((value) => value + 1);
      }
    };
    window.addEventListener(CHAPTER_CHANGED_EVENT, handleChange);
    return () => window.removeEventListener(CHAPTER_CHANGED_EVENT, handleChange);
  }, [projectId]);

  const treeData = useMemo(() => {
    const tree = outlineData ? outlineToTree(outlineData.nodes) : [];
    if (chapters.length === 0) return tree;

    const savedMap = new Map<string, Chapter>();
    for (const chapter of chapters) {
      savedMap.set(chapter.id, chapter);
    }

    const markTree = (nodes: TreeNode[]): TreeNode[] => nodes.map((node) => {
      if (node.type === 'chapter' && savedMap.has(node.id)) {
        const saved = savedMap.get(node.id)!;
        return { ...node, wordCount: saved.wordCount || node.wordCount, hasContent: Boolean(saved.content?.trim()) };
      }
      if (node.children) return { ...node, children: markTree(node.children) };
      return node;
    });

    return markTree(tree);
  }, [chapters, outlineData]);

  useEffect(() => {
    setExpandedNodes((previous) => new Set([...previous, ...treeData.map((node) => node.id)]));
  }, [treeData]);

  const toggleNode = (id: string) => {
    setExpandedNodes((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleChapterSelect = (chapterId: string) => {
    if (!isProjectReady) return;
    const reason = chapterMutationBlockReason(projectId);
    if (reason && chapterId !== currentChapterId) { setError(reason); return; }
    setCurrentChapter(chapterId);
    onChapterSelect?.(chapterId);
  };

  const handleRecycle = async () => {
    if (!recycleTarget || recyclingRef.current) return;
    const reason = chapterMutationBlockReason(projectId);
    if (reason) { setError(reason); setRecycleTarget(null); return; }
    recyclingRef.current = true;
    setRecycling(true);
    setError('');
    try {
      await chapterApi.delete(projectId, recycleTarget.id);
      notifyChapterChanged({ projectId, chapterId: recycleTarget.id, action: 'recycle' });
      setRecycleTarget(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '移入回收站失败。');
      setRecycleTarget(null);
    } finally {
      recyclingRef.current = false;
      setRecycling(false);
    }
  };

  const renderTreeNode = (node: TreeNode, depth = 0) => {
    if (node.type === 'chapter') {
      return (
        <div key={node.id} className="flex min-w-0 items-center">
        <button
          onClick={() => handleChapterSelect(node.id)}
          className={`flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pr-2 text-left text-sm transition-colors ${
            currentChapterId === node.id
              ? 'bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-400'
              : 'text-surface-600 hover:bg-surface-100 dark:text-surface-400 dark:hover:bg-surface-750'
          }`}
          style={{ paddingLeft: `${12 + depth * 16}px` }}
          data-testid={`chapter-tree-item-${node.id}`}
        >
          <FileText size={13} className="shrink-0" />
          <span className="flex-1 truncate">{node.title}</span>
          {node.wordCount !== undefined && node.wordCount > 0 ? (
            <span className="shrink-0 text-[10px] text-surface-400">{node.wordCount} 字</span>
          ) : null}
          {!node.hasContent ? <span className="shrink-0 text-[10px] text-surface-400">未创作</span> : null}
        </button>
        {node.hasContent ? (
          <button className="mr-1 shrink-0 rounded p-1.5 text-surface-400 hover:bg-surface-100 hover:text-error dark:hover:bg-surface-700" aria-label={`将${node.title}移入回收站`} title="移入回收站" data-testid={`chapter-recycle-${node.id}`} onClick={() => {
            const reason = chapterMutationBlockReason(projectId);
            if (reason) { setError(reason); return; }
            setError(''); setRecycleTarget(node);
          }}><Trash2 size={13} /></button>
        ) : null}
        </div>
      );
    }

    const isExpanded = expandedNodes.has(node.id);
    return (
      <div key={node.id}>
        <button
          onClick={() => toggleNode(node.id)}
          className="flex w-full items-center gap-1 py-1.5 pr-3 text-left transition-colors hover:bg-surface-100 dark:hover:bg-surface-750"
          style={{ paddingLeft: `${12 + depth * 16}px` }}
          data-testid={`chapter-tree-${node.type}-${node.id}`}
          aria-expanded={isExpanded}
        >
          {isExpanded
            ? <ChevronDown size={14} className="shrink-0 text-surface-400" />
            : <ChevronRight size={14} className="shrink-0 text-surface-400" />}
          <span className={`${node.type === 'volume' ? 'font-semibold' : 'font-medium'} truncate text-sm text-surface-700 dark:text-surface-300`}>
            {node.title}
          </span>
        </button>
        {isExpanded ? node.children?.map((child) => renderTreeNode(child, depth + 1)) : null}
      </div>
    );
  };

  return (
    <div className="py-2">
      <button className="mb-2 flex w-full items-center gap-2 border-b border-surface-200 px-3 pb-3 pt-1 text-left text-xs text-surface-500 hover:text-primary-600 dark:border-surface-700" disabled={!isProjectReady} data-testid="chapter-trash-open" onClick={() => setShowTrash(true)}>
        <Trash2 size={14} />章节回收站{trashCount === null ? '' : `（${trashCount}）`}
      </button>
      {error && <p role="alert" className="px-3 pb-2 text-xs text-error">{error}</p>}
      {isEmpty && treeData.length === 0 ? (
      <div className="py-8 text-center">
        <FileText size={28} className="mx-auto mb-2 text-surface-300" />
        <p className="text-xs text-surface-400">暂无章节</p>
        <p className="mt-1 px-4 text-[10px] leading-5 text-surface-400">
          生成大纲或导入 Markdown 章节后，这里会自动整理成可点击的章节导航。
        </p>
        <div className="mx-auto mt-3 flex items-center justify-center gap-1 text-xs text-primary-500">
          <Plus size={12} />
          等待内容生成
        </div>
      </div>
      ) : treeData.map((node) => renderTreeNode(node))}
      {showTrash ? <ChapterTrash projectId={projectId} onClose={() => setShowTrash(false)} onRestored={(chapterId) => {
        setCurrentChapter(chapterId); onChapterSelect?.(chapterId);
      }} /> : null}
      <Modal open={Boolean(recycleTarget)} onClose={() => { if (!recyclingRef.current) setRecycleTarget(null); }} title="移入章节回收站" footer={(
        <>
          <button className="btn-ghost" disabled={recycling} onClick={() => setRecycleTarget(null)}>取消</button>
          <button className="btn-primary" disabled={recycling} data-testid="chapter-recycle-confirm" onClick={() => void handleRecycle()}>{recycling ? '处理中…' : '确认移入回收站'}</button>
        </>
      )}>
        <p className="text-sm leading-6">将“{recycleTarget?.title}”的已保存正文移入回收站？大纲章节会保留为空白，原稿可从回收站恢复。</p>
      </Modal>
    </div>
  );
}
