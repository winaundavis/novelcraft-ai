import test from 'node:test';
import assert from 'node:assert/strict';

import { isSameGeneration } from '../src/components/editor/generationGuard.ts';

/**
 * 在途生成/保存回调的守门判定。
 *
 * 背景：编辑器在**切换项目时不重挂载**（`ProjectWorkspace` 没有按 projectId 加 key），
 * 同一个组件实例会先服务项目 A、再服务项目 B。而续写/保存都是长异步操作，
 * 回调返回时必须能回答「我还是当前那一次请求吗」。
 *
 * 这里用真值表把判定钉死；「组件是否在每个回调都调用它」由 E2E 覆盖。
 */

/** @type {{ id: string; projectId: string; chapterId: string }} */
const EXPECTED = { id: 'req-1', projectId: 'A', chapterId: 'a1' };

/** @param {Record<string, unknown>} overrides */
const snapshot = (overrides = {}) => ({
  componentProjectId: 'A',
  storeProjectId: 'A',
  storeChapterId: 'a1',
  ...overrides,
});

test('同一请求且上下文未变时才放行', () => {
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot()), true);
});

test('没有在途请求记录时一律丢弃回调', () => {
  assert.equal(isSameGeneration(null, EXPECTED, snapshot()), false);
  assert.equal(isSameGeneration(undefined, EXPECTED, snapshot()), false);
});

test('已有更新的请求开始时，旧请求的回调必须丢弃', () => {
  const newer = { ...EXPECTED, id: 'req-2' };
  assert.equal(isSameGeneration(newer, EXPECTED, snapshot()), false);
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot()) , true);
});

test('切换到其它项目后，旧项目的回调必须丢弃', () => {
  // 组件级 ref 与 store 分别单独变化时都要失效，避免只靠其中一处判断
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ componentProjectId: 'B' })), false);
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ storeProjectId: 'B' })), false);
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ componentProjectId: 'B', storeProjectId: 'B' })), false);
});

test('切换到其它章节后，旧章节的回调必须丢弃', () => {
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ storeChapterId: 'a2' })), false);
});

test('store 尚未选中章节时只按项目判定，不能一律丢弃', () => {
  // 新建项目、章节列表尚未加载时 currentChapterId 为空：
  // 若把它当成「章节不匹配」，合法的回调会被静默丢弃，作者看不到生成结果。
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ storeChapterId: '' })), true);
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ storeChapterId: null })), true);
  // 但项目仍必须匹配
  assert.equal(isSameGeneration(EXPECTED, EXPECTED, snapshot({ storeChapterId: '', storeProjectId: 'B' })), false);
});

test('记录本身若被换成了别的项目/章节，也要丢弃', () => {
  assert.equal(isSameGeneration({ ...EXPECTED, projectId: 'B' }, EXPECTED, snapshot()), false);
  assert.equal(isSameGeneration({ ...EXPECTED, chapterId: 'a2' }, EXPECTED, snapshot()), false);
});
