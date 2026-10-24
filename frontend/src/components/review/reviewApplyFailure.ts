/**
 * 智能审校「一键修改」的失败分类（2026-10-21）。
 *
 * 现场反馈：勾选几条建议一键修改，改到一半就整批失败，只能重新审校重来。
 *
 * 关键区分——服务端是否**已经写了正文**：
 * - 可跳过：模型未产生实际修改 / 返回空 / 调用失败 / 创建候选超时。
 *   这几种都发生在 accept 之前或模型空转，正文一定没变，跳过继续下一条即可。
 * - 必须停：正文已被更新、风格过渡计划冲突（409）、本地有未保存修改。
 *   继续改会基于错位文本，必须停下让用户重新审校。
 *
 * 刻意**不**导入 `ApiError`：保持本模块零依赖，便于用 node --test 直接覆盖判定逻辑；
 * 这些 code 都由本项目服务端产生，按字符串判定足够安全。
 */
export const SKIPPABLE_APPLY_CODES = new Set([
  'copilot_no_change',
  'copilot_empty_output',
  'copilot_provider_failed',
  // 创建候选阶段超时：还没 accept，正文一定没改（服务端最多留一条未处理的候选）。
  'copilot_create_timeout',
]);

export function isSkippableApplyFailure(error: unknown): boolean {
  const code = (error as { code?: unknown } | null | undefined)?.code;
  return typeof code === 'string' && SKIPPABLE_APPLY_CODES.has(code);
}
