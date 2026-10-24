import type { AutoWriteSession } from '../../stores';

/**
 * 自动创作的进度：完成章数 / 总章数 / 百分比。
 *
 * 为什么**不**用「本地大纲的前 N 个节点」推算：
 * - 那是按**位置**推的。`current-to-end` 这类从中途开始的任务，分子只覆盖大纲后半段，
 *   而分母若取大纲全长，进度就永远到不了 100% —— 作者看到的就是"创作完成后进度条不动"。
 * - 本地大纲还可能是旧的（导入整批替换后 ids 全变了），推出来的 id 未必是真章节。
 *
 * 所以优先用**作业自己报告的** `completed_chapters` / `total_chapters`；
 * 只有在旧会话（IndexedDB 里恢复、没有这两个字段）时才回退到按大纲推算。
 */
export function resolveAutoWriteProgress(
  session: Pick<AutoWriteSession, 'totalChapters' | 'completedChapters' | 'completedChapterIds'>,
  outlineChapterCount: number,
): { completed: number; total: number; percent: number } {
  const total = Math.max(0, session.totalChapters > 0 ? session.totalChapters : outlineChapterCount);
  const reported = session.completedChapters > 0
    ? session.completedChapters
    : session.completedChapterIds.length;
  // 夹在 [0, total]：既不出现负数，也不出现 >100% 的宽度。
  const completed = total > 0 ? Math.min(Math.max(0, reported), total) : 0;
  const percent = total > 0 ? (completed / total) * 100 : 0;
  return { completed, total, percent };
}
