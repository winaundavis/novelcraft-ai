/**
 * 审校快照的存储策略（纯函数，可被 `node --test` 直接覆盖，不碰浏览器 API）。
 *
 * 背景（作者报告）：工作台闲置一段时间后回到页面，审校结果就没了 —— 因为这份快照此前
 * 只活在内存里，而「浏览器丢弃闲置标签页后恢复」「开发服务器 HMR 重连」都会让页面整页
 * 重新加载一次。改动前 `useReviewStore` 是全项目唯一没有持久化的业务 store。
 *
 * 当初不持久化的顾虑是「审校结果与当时的正文强相关，跨刷新保留容易让人误以为仍然适用」。
 * 这个顾虑其实已经有更准的机制在兜：每条快照都带 `contentSignature`，正文改过时审校面板
 * 会明确提示「结果可能已过期」并禁用一键修改。于是这里改为持久化，并给快照数量加上限。
 */

export interface ReviewSnapshot {
  result: Record<string, unknown>;
  degradedReason: string;
  contentSignature: string;
  reviewedAt: number;
  appliedIssueKeys: string[];
  /** 最近一次一键修改前的正文，用于撤销；与快照同生命周期。 */
  lastApplied: { issueKey: string; previousContent: string } | null;
}

/**
 * 一个浏览器里最多保留多少个快照（超出后淘汰最旧的）。
 *
 * 快照体积取决于审校返回的条目数（每章几 KB ~ 几十 KB），30 个够覆盖近期在读的几章，
 * 又不会让存储无限增长。淘汰只按时间：谁最近审校过就留谁，最容易向作者解释。
 */
export const REVIEW_SNAPSHOT_LIMIT = 30;

export function reviewSnapshotKey(projectId: string, chapterId: string): string {
  return `${projectId}::${chapterId || 'no-chapter'}`;
}

/** 正文指纹（FNV-1a + 长度）：只用于判断"正文是否变过"，不是加密哈希。 */
export function reviewContentSignature(text: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${text.length}:${hash.toString(16)}`;
}

/**
 * 只保留最近 `limit` 个快照（按 `reviewedAt`，新的在前）。
 *
 * 时间缺失/非法时按「最旧」处理；时间相同时保持原有顺序（先写入的先淘汰），
 * 因此结果是确定的：同样的输入永远得到同样的输出。
 */
export function pruneReviewSnapshots(
  snapshots: Record<string, ReviewSnapshot>,
  limit: number = REVIEW_SNAPSHOT_LIMIT,
): Record<string, ReviewSnapshot> {
  const entries = Object.entries(snapshots);
  const bound = Math.max(limit, 0);
  if (entries.length <= bound) return snapshots;

  const keep = new Set(
    entries
      .map(([key, snapshot], index) => ({
        key,
        index,
        reviewedAt: Number(snapshot?.reviewedAt) || 0,
      }))
      .sort((left, right) => (right.reviewedAt - left.reviewedAt) || (left.index - right.index))
      .slice(0, bound)
      .map((item) => item.key),
  );

  return Object.fromEntries(entries.filter(([key]) => keep.has(key)));
}
