/**
 * 章节对账：**当前选中的章节 id 与当前大纲不一致时，该怎么办**。
 *
 * 为什么抽成纯函数：这条分支决定"会不会把作者正在写的正文切走"，其中
 * 「有未保存正文时绝不自动切换」是**会丢字**的那一类风险，必须在单元测试里钉死；
 * 而用 E2E 去钉它需要先往 IndexedDB 里塞一个"当前章 id 不在大纲里"的会话状态，
 * 既脆又慢（更糟的是唯一便宜的构造写法 `page.evaluate(import('/src/...'))` 对 HMR 敏感，
 * 会引入新的假失败源）。
 */

/** 只依赖 id —— 用结构类型而不是 OutlineNode，纯函数不必被大纲结构绑死。 */
export interface ChapterNodeRef {
  id: string;
}

export type ChapterReconcileReason =
  /** 不用动 */
  | 'none'
  /** 之前没有选中章节（首次进入项目 / 大纲刚生成）→ 选第一章 */
  | 'select-first'
  /** 选中的章节已不在大纲里 → 切到第一章 */
  | 'reconcile'
  /** 选中的章节已不在大纲里，但**有未保存正文** → 不切，交给作者自己处理 */
  | 'blocked-dirty';

export interface ChapterReconcileInput {
  /** 项目快照是否就绪：没就绪时大纲可能只是还没拉到，此时任何切换都是误判。 */
  isProjectReady: boolean;
  /** 当前大纲里的章节节点（调用方保证已按 order 升序）。 */
  chapterNodes: readonly ChapterNodeRef[];
  currentChapterId: string | null;
  /** 编辑器里是否有未保存的正文。 */
  hasUnsavedDraft: boolean;
}

export interface ChapterReconcileResult {
  /** null = 不切换。 */
  nextChapterId: string | null;
  reason: ChapterReconcileReason;
}

export function resolveChapterReconcile(input: ChapterReconcileInput): ChapterReconcileResult {
  const { isProjectReady, chapterNodes, currentChapterId, hasUnsavedDraft } = input;

  // 项目还没就绪、或大纲里一章都没有 → 没有可切换的目标。
  if (!isProjectReady || chapterNodes.length === 0) return { nextChapterId: null, reason: 'none' };

  // 还没选中任何章节（首次进入项目 / 大纲刚生成）→ 选第一章。
  if (!currentChapterId) {
    const first = chapterNodes[0]?.id;
    return first ? { nextChapterId: first, reason: 'select-first' } : { nextChapterId: null, reason: 'none' };
  }

  // 选中的章节仍在大纲里 → 什么都不用做。
  if (chapterNodes.some((node) => node.id === currentChapterId)) {
    return { nextChapterId: null, reason: 'none' };
  }

  // 选中的章节已不在大纲里（导入整批替换 / 移入回收站 / 大纲被重新生成）。
  // **有未保存正文时绝不自动切换** —— 切换会先清空编辑器缓冲区再去拉新章，
  // 等于直接丢掉作者刚写的字。这种情况只保留"已不在项目里"的提示，由作者自己决定。
  if (hasUnsavedDraft) return { nextChapterId: null, reason: 'blocked-dirty' };

  const fallback = chapterNodes[0]?.id;
  if (!fallback || fallback === currentChapterId) return { nextChapterId: null, reason: 'none' };
  return { nextChapterId: fallback, reason: 'reconcile' };
}
