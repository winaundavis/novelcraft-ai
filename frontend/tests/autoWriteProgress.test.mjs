import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveAutoWriteProgress } from '../src/components/editor/autoWriteProgress.ts';

/** 构造一个最小会话片段（只包含进度计算关心的字段）。 */
function session({ totalChapters = 0, completedChapters = 0, completedChapterIds = [] } = {}) {
  return { totalChapters, completedChapters, completedChapterIds };
}

test('进度以作业自己报告的完成数/总数为准', () => {
  const result = resolveAutoWriteProgress(session({ totalChapters: 8, completedChapters: 3 }), 8);
  assert.deepEqual(result, { completed: 3, total: 8, percent: 37.5 });
});

test('从中间开始的任务也能到 100%（不再拿大纲全长当分母）', () => {
  // 大纲 50 章，作业从第 41 章开始、共 10 章；全部写完时进度必须是 100%。
  // 修复前的算法会拿大纲全长（50）当分母，于是永远停在 20%。
  const result = resolveAutoWriteProgress(session({ totalChapters: 10, completedChapters: 10 }), 50);
  assert.equal(result.total, 10, '分子/分母都应来自作业，而不是本地大纲');
  assert.equal(result.percent, 100);
});

test('旧会话（IndexedDB 里恢复的、没有这两个字段）回退到按大纲推算', () => {
  const legacy = session({ completedChapterIds: ['a', 'b', 'c'] });
  const result = resolveAutoWriteProgress(legacy, 12);
  assert.deepEqual(result, { completed: 3, total: 12, percent: 25 });
});

test('夹紧在 [0, total]：不出现负数、也不出现 >100% 的宽度', () => {
  assert.equal(resolveAutoWriteProgress(session({ totalChapters: 5, completedChapters: 9 }), 5).percent, 100);
  assert.equal(resolveAutoWriteProgress(session({ totalChapters: 5, completedChapters: -3 }), 5).percent, 0);
  // 没有总数时不得除零
  assert.deepEqual(resolveAutoWriteProgress(session(), 0), { completed: 0, total: 0, percent: 0 });
});
