import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveChapterReconcile } from '../src/components/editor/chapterReconcile.ts';

/** 章节节点的最小形状（纯函数只读 id）。 */
function nodes(...ids) {
  return ids.map((id) => ({ id }));
}

const ready = { isProjectReady: true, hasUnsavedDraft: false };

test('还没选中章节时选中第一章（首次进入项目 / 大纲刚生成）', () => {
  const result = resolveChapterReconcile({ ...ready, chapterNodes: nodes('c1', 'c2'), currentChapterId: null });
  assert.deepEqual(result, { nextChapterId: 'c1', reason: 'select-first' });
});

test('选中的章节仍在大纲里时不动', () => {
  const result = resolveChapterReconcile({ ...ready, chapterNodes: nodes('c1', 'c2'), currentChapterId: 'c2' });
  assert.deepEqual(result, { nextChapterId: null, reason: 'none' });
});

test('选中的章节已不在大纲里 → 切到第一章（否则一直 404，且保存会写出大纲之外的新章节）', () => {
  const result = resolveChapterReconcile({ ...ready, chapterNodes: nodes('c9', 'c10'), currentChapterId: 'c1' });
  assert.deepEqual(result, { nextChapterId: 'c9', reason: 'reconcile' });
});

test('有未保存正文时**绝不**自动切换（防丢字，这是本函数存在的主要理由）', () => {
  const result = resolveChapterReconcile({
    isProjectReady: true,
    hasUnsavedDraft: true,
    chapterNodes: nodes('c9', 'c10'),
    currentChapterId: 'c1',
  });
  assert.deepEqual(result, { nextChapterId: null, reason: 'blocked-dirty' });
});

test('项目未就绪 / 大纲为空时一律不动（避免把"还没拉到"误判成"章节已不存在"）', () => {
  assert.deepEqual(
    resolveChapterReconcile({ isProjectReady: false, hasUnsavedDraft: false, chapterNodes: nodes('c1'), currentChapterId: 'cX' }),
    { nextChapterId: null, reason: 'none' },
  );
  assert.deepEqual(
    resolveChapterReconcile({ isProjectReady: true, hasUnsavedDraft: false, chapterNodes: [], currentChapterId: 'cX' }),
    { nextChapterId: null, reason: 'none' },
  );
  assert.deepEqual(
    resolveChapterReconcile({ isProjectReady: true, hasUnsavedDraft: false, chapterNodes: [], currentChapterId: null }),
    { nextChapterId: null, reason: 'none' },
  );
});

test('大纲只剩一个节点、且它不是当前章节 → 切到它', () => {
  const result = resolveChapterReconcile({ ...ready, chapterNodes: nodes('only'), currentChapterId: 'c1' });
  assert.deepEqual(result, { nextChapterId: 'only', reason: 'reconcile' });
});
