import { useEditorStore } from '../../stores';
import type { StyleTransition } from '../../types';

export const CHAPTER_MUTATION_CHECK_EVENT = 'novelcraft:chapter-mutation-check';
export const CHAPTER_CHANGED_EVENT = 'novelcraft:chapter-changed';
/**
 * 渐进风格计划或进度变化（保存正文 / 接受候选 / 重设计划后）。
 * 面板监听此事件刷新「完成数 / 计划进度」，避免各处各自轮询。
 */
export const STYLE_TRANSITION_CHANGED_EVENT = 'novelcraft:style-transition-changed';

export interface StyleTransitionChangedDetail {
  projectId: string;
  /** 后端在同一响应里回传的最新进度；没有时面板自行重新 GET。 */
  transition?: StyleTransition;
}

export function notifyStyleTransitionChanged(detail: StyleTransitionChangedDetail): void {
  window.dispatchEvent(new CustomEvent(STYLE_TRANSITION_CHANGED_EVENT, { detail }));
}

export interface ChapterMutationCheck {
  projectId: string;
  intent?: 'mutation' | 'unmount';
  checked?: boolean;
  blockedReason?: string;
}

export interface ChapterChangedDetail {
  projectId: string;
  chapterId?: string;
  action: 'restore' | 'recycle' | 'delete-trash' | 'save' | 'copilot-accept';
  restoredContent?: string;
  appliedContent?: string;
}

// Prefer the mounted editor's synchronous state. On the full Assistant tab
// the editor is unmounted, so its in-session draft lives only in the store.
export function chapterMutationBlockReason(projectId: string, intent: 'mutation' | 'unmount' = 'mutation'): string | undefined {
  const detail: ChapterMutationCheck = { projectId, intent };
  window.dispatchEvent(new CustomEvent(CHAPTER_MUTATION_CHECK_EVENT, { detail }));
  const editor = useEditorStore.getState();
  if (intent === 'mutation' && !detail.checked && editor.activeProjectId === projectId && editor.isDirty) {
    return '请先保存当前章节的未保存修改。';
  }
  return detail.blockedReason;
}

export function notifyChapterChanged(detail: ChapterChangedDetail): void {
  window.dispatchEvent(new CustomEvent(CHAPTER_CHANGED_EVENT, { detail }));
  notifyProjectRefresh(detail.projectId);
}

/**
 * 请工作台重新拉一次项目（大纲、字数、章节列表都靠它刷新）。
 *
 * **整批改动必须调它**：导入（完全替换 / 增量合并）会把现有章节全部或部分回收重建，
 * 章节 id 与大纲都会变；不通知的话左侧章节导航仍用导入前的大纲，
 * 点进去就是一个已经不存在的章节（服务端 404 → 编辑器看起来像「这一章是空的」）。
 */
export function notifyProjectRefresh(projectId: string): void {
  window.dispatchEvent(new CustomEvent('novelcraft:project-refresh', { detail: { projectId } }));
}
