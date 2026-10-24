/** Identity and local revision captured before an asynchronous prose operation. */
export interface EditorResultSnapshot {
  activeProjectId: string;
  currentChapterId: string | null;
  chapterContent: string;
  contentRevision: number;
}

export function matchesEditorSnapshot(expected: EditorResultSnapshot, current: EditorResultSnapshot): boolean {
  return expected.activeProjectId === current.activeProjectId
    && expected.currentChapterId === current.currentChapterId
    && expected.chapterContent === current.chapterContent
    && expected.contentRevision === current.contentRevision;
}
