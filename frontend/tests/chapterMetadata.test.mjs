import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveChapterSaveMetadata } from '../src/components/editor/chapterMetadata.ts';

test('preserves saved chapter metadata when no outline node is active', () => {
  assert.deepEqual(
    resolveChapterSaveMetadata(null, { title: '第一章：测试入口', order: 1, volumeId: 'volume-1' }),
    { title: '第一章：测试入口', order: 1, volumeId: 'volume-1' },
  );
});

test('outline metadata takes precedence over persisted metadata', () => {
  assert.deepEqual(
    resolveChapterSaveMetadata({ title: '大纲标题', order: 2, parentId: 'volume-2' }, { title: '旧标题', order: 1, volumeId: 'volume-1' }),
    { title: '大纲标题', order: 2, volumeId: 'volume-2' },
  );
});

test('作者手填的章节名赢过大纲标题（否则改名会被下一次保存覆盖回 AI 原名）', () => {
  assert.deepEqual(
    resolveChapterSaveMetadata(
      { title: '第3章 雨夜来客', order: 3, parentId: 'volume-1' },
      { title: '第3章 雨夜来客', order: 3, volumeId: 'volume-1' },
      '第3章 门后的脚步声',
    ),
    { title: '第3章 门后的脚步声', order: 3, volumeId: 'volume-1' },
  );
});

test('空白的手填章节名视为"没改"，仍按大纲/已保存取值', () => {
  assert.deepEqual(
    resolveChapterSaveMetadata({ title: '第3章 雨夜来客', order: 3, parentId: 'v' }, null, '   '),
    { title: '第3章 雨夜来客', order: 3, volumeId: 'v' },
  );
});
