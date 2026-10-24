export interface PersistedChapterMetadata {
  title?: string;
  order?: number;
  volumeId?: string;
}

export function resolveChapterSaveMetadata(
  outlineChapter: { title?: string; order?: number; parentId?: string | null } | null | undefined,
  persistedChapter: PersistedChapterMetadata | null | undefined,
  /**
   * 作者在编辑器里手填的章节名。一旦提供就**以它为准** —— 否则"AI 起名写进大纲"
   * 会在下一次保存时把作者改过的名字覆盖回 AI 原名。
   */
  editedTitle?: string | null,
): { title: string; order: number; volumeId: string } {
  const manual = (editedTitle || '').trim();
  return {
    title: manual || outlineChapter?.title || persistedChapter?.title || '未命名章节',
    order: outlineChapter?.order || persistedChapter?.order || 1,
    volumeId: outlineChapter?.parentId || persistedChapter?.volumeId || '',
  };
}
