import type { StylePlanToken } from '../types';
import { styleApi } from './api';

/**
 * 读取当前生效的渐进过渡计划版本。
 *
 * 契约字段的铁律（已核对后端 `validate_plan_on_conn`）：
 * - `stylePlanId` 与 `stylePlanRevision` **必须成对出现**。只传其中一个会被判为冲突（409），
 *   因为服务端把「只给一个」视为调用方拿的是过期的一半信息。
 * - 未启用 v2 计划时后端返回 `stylePlanId: ''` 与 `stylePlanRevision: 0`，这仍是一个
 *   明确 token。生成草稿保存需要原样提交它；手工保存由调用方不传 token 表示。
 *
 * 读取失败（无画像、网络抖动）返回 `null`：不阻断用户操作，最终校验交给服务端。
 * 但调用方**不得**用"主动丢掉 token"的方式绕过冲突检查。
 */
export async function fetchCurrentStylePlanToken(projectId: string): Promise<StylePlanToken | null> {
  try {
    const json = await styleApi.getContract(projectId);
    const planId = json.data?.stylePlanId;
    const revision = json.data?.stylePlanRevision;
    if (typeof planId !== 'string' || typeof revision !== 'number') return null;
    return { stylePlanId: planId, stylePlanRevision: revision };
  } catch {
    return null;
  }
}

/**
 * 把 token 展开成请求体字段。
 * `null` 表示读取失败/没有可信 token；显式未启用计划仍会展开成成对的空值与 0。
 */
export function stylePlanTokenFields(token: StylePlanToken | null | undefined): Record<string, string | number> {
  if (!token) return {};
  return { stylePlanId: token.stylePlanId, stylePlanRevision: token.stylePlanRevision };
}
