import test from 'node:test';
import assert from 'node:assert/strict';
import { matchesEditorSnapshot } from '../src/components/editor/editorResultGuard.ts';

const before = { activeProjectId: 'A', currentChapterId: 'a1', chapterContent: 'draft', contentRevision: 4 };

test('an asynchronous result can apply only to the unchanged project, chapter and draft version', () => {
  assert.equal(matchesEditorSnapshot(before, { ...before }), true);
  for (const changed of [
    { activeProjectId: 'B' },
    { currentChapterId: 'a2' },
    { chapterContent: 'new draft' },
    // Editing and undoing still invalidates a result based on the older version.
    { contentRevision: 6 },
  ]) assert.equal(matchesEditorSnapshot(before, { ...before, ...changed }), false);
});
