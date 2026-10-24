/**
 * 在途生成/保存请求的「是否仍属当前上下文」判定。
 *
 * 为什么要抽成纯函数：编辑器在切换项目时**不会**重挂载（`ProjectWorkspace` 没有按
 * `projectId` 加 key），所以同一个组件实例会先服务项目 A、再服务项目 B。
 * 续写与保存都是长异步操作，回调返回时必须确认「我还是当前那一次请求」，
 * 否则项目 A 的迟到响应会写进项目 B 的正文与草稿 token。
 *
 * 判定条件必须同时成立（任一不成立即丢弃回调）：
 * 1. 记录在案的请求 id 与本次请求一致（新请求已开始 → 旧请求作废）；
 * 2. 记录在案的项目/章节与本次请求一致（自身记录未被后续请求覆盖）；
 * 3. 组件级项目 ref 仍指向该项目（切项目后立即失效）；
 * 4. store 里的活动项目仍是该项目（跨组件一致）；
 * 5. store 里的当前章节若不是空，必须与本次请求的章节一致（切章后失效）。
 *
 * 注意第 5 条：store 的 `currentChapterId` 允许为空（例如新建项目尚未选中章节），
 * 此时只按项目判定，不能因为「章节为空」就把回调全部丢弃。
 */

export interface GenerationIdentity {
  /** 每次请求唯一，用于识别「已有更新请求开始」。 */
  id: string;
  projectId: string;
  chapterId: string;
}

export interface GenerationGuardSnapshot {
  /** 组件内 `activeProjectRef.current`。 */
  componentProjectId?: string | null;
  /** store 的 `activeProjectId`。 */
  storeProjectId?: string | null;
  /** store 的 `currentChapterId`；允许为空。 */
  storeChapterId?: string | null;
}

export function isSameGeneration(
  active: GenerationIdentity | null | undefined,
  expected: GenerationIdentity,
  snapshot: GenerationGuardSnapshot,
): boolean {
  if (!active) return false;
  if (active.id !== expected.id) return false;
  if (active.projectId !== expected.projectId) return false;
  if (active.chapterId !== expected.chapterId) return false;
  if (snapshot.componentProjectId !== expected.projectId) return false;
  if (snapshot.storeProjectId !== expected.projectId) return false;
  if (snapshot.storeChapterId && snapshot.storeChapterId !== expected.chapterId) return false;
  return true;
}
