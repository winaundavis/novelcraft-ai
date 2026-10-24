import test from 'node:test';
import assert from 'node:assert/strict';

import {
  EMPTY_MARKDOWN_FLOW,
  EMPTY_WORD_FLOW,
  beginMarkdownRun,
  changeMarkdownFlow,
  changeWordFlow,
  hasMarkdownProgress,
  hasWordProgress,
  isCurrentMarkdownRun,
  markdownFlowSnapshot,
  resetMarkdownFlow,
  resetWordFlow,
  wordFlowSnapshot,
} from '../src/components/import/importStore.ts';

/**
 * 导入页流程状态（`components/import/importStore.ts`）。
 *
 * 组件层的表现由 `tests/e2e/import-page-state.spec.ts` 覆盖；这里只钉住几条
 * 光看界面不容易发现的规则：
 *   - 状态按 projectId 分开，换作品不会串台；
 *   - 清空要连在途请求一起作废，否则几十秒后的旧响应会把面板「复活」；
 *   - 清空只动被清的那一个作品。
 *
 * 每个用例都用独立 projectId，避免共享模块级 store 造成顺序依赖。
 */

const PROJECT_A = 'unit-project-a';
const PROJECT_B = 'unit-project-b';

test('flow state is kept per project and never leaks across projects', () => {
  changeMarkdownFlow(PROJECT_A, { pickedFiles: [{ name: '第01章.md' }], step: 2 });

  assert.equal(markdownFlowSnapshot(PROJECT_A).step, 2);
  assert.equal(markdownFlowSnapshot(PROJECT_A).pickedFiles.length, 1);
  // 另一个作品仍是初始状态
  assert.equal(markdownFlowSnapshot(PROJECT_B), EMPTY_MARKDOWN_FLOW);
  assert.equal(markdownFlowSnapshot(PROJECT_B).step, 1);
});

test('resetting one project leaves the others untouched', () => {
  changeMarkdownFlow(PROJECT_A, { successMessage: '已导入 114 章。' });
  changeMarkdownFlow(PROJECT_B, { successMessage: '已导入 8 章。' });

  resetMarkdownFlow(PROJECT_A);

  assert.equal(markdownFlowSnapshot(PROJECT_A).successMessage, '');
  assert.equal(markdownFlowSnapshot(PROJECT_A).step, 1);
  assert.equal(markdownFlowSnapshot(PROJECT_B).successMessage, '已导入 8 章。');

  resetMarkdownFlow(PROJECT_B);
});

test('a function patch sees the previous state', () => {
  resetMarkdownFlow(PROJECT_A);
  changeMarkdownFlow(PROJECT_A, { skippedCount: 3 });
  changeMarkdownFlow(PROJECT_A, (previous) => ({ skippedCount: previous.skippedCount + 1 }));

  assert.equal(markdownFlowSnapshot(PROJECT_A).skippedCount, 4);
  resetMarkdownFlow(PROJECT_A);
});

test('clearing invalidates the in-flight scan so an old response cannot resurrect the panel', () => {
  resetMarkdownFlow(PROJECT_A);

  const first = beginMarkdownRun(PROJECT_A);
  assert.equal(isCurrentMarkdownRun(PROJECT_A, first), true);

  // 作者点了「清空」
  resetMarkdownFlow(PROJECT_A);
  assert.equal(isCurrentMarkdownRun(PROJECT_A, first), false);

  // 早先那次扫描返回时已经不作数了
  changeMarkdownFlow(PROJECT_A, { result: { success: true }, step: 3 });
  assert.equal(markdownFlowSnapshot(PROJECT_A).step, 3);

  // 而新的一次扫描仍然是当前的那次
  const second = beginMarkdownRun(PROJECT_A);
  assert.equal(isCurrentMarkdownRun(PROJECT_A, second), true);
  assert.equal(isCurrentMarkdownRun(PROJECT_A, first), false);
});

test('starting a new scan invalidates the previous one', () => {
  const first = beginMarkdownRun(PROJECT_A);
  const second = beginMarkdownRun(PROJECT_A);

  assert.notEqual(first, second);
  assert.equal(isCurrentMarkdownRun(PROJECT_A, first), false);
  assert.equal(isCurrentMarkdownRun(PROJECT_A, second), true);
});

test('word flow is tracked separately from the markdown flow', () => {
  resetWordFlow(PROJECT_A);
  resetMarkdownFlow(PROJECT_A);

  changeWordFlow(PROJECT_A, { filePath: 'F:\\docs\\notes.docx', step: 2 });

  assert.equal(wordFlowSnapshot(PROJECT_A).filePath, 'F:\\docs\\notes.docx');
  assert.equal(markdownFlowSnapshot(PROJECT_A).folderPath, '');
  assert.equal(wordFlowSnapshot(PROJECT_B), EMPTY_WORD_FLOW);

  resetWordFlow(PROJECT_A);
});

test('the clear button only shows up once there is something to clear', () => {
  assert.equal(hasMarkdownProgress(EMPTY_MARKDOWN_FLOW), false);
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, pickedFiles: [{ name: 'a.md' }] }), true);
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, step: 2 }), true);
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, folderPath: 'F:\\Novel' }), true);
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, error: '扫描失败。' }), true);
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, successMessage: '已导入 2 章。' }), true);
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, result: { success: true } }), true);
  // 只有空白字符不算「有进度」
  assert.equal(hasMarkdownProgress({ ...EMPTY_MARKDOWN_FLOW, folderPath: '   ' }), false);

  assert.equal(hasWordProgress(EMPTY_WORD_FLOW), false);
  assert.equal(hasWordProgress({ ...EMPTY_WORD_FLOW, selectedFile: { name: 'a.docx' } }), true);
  assert.equal(hasWordProgress({ ...EMPTY_WORD_FLOW, suggestions: [{ id: 's1' }] }), true);
});
