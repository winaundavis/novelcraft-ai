# 运营后台（管理端）

更新：2026-10-24。代码位置：后端 `backend/app/api/admin.py`，前端 `frontend/src/components/admin/AdminConsole.tsx`。

## 它是什么

给**管理员**用的运营视图，提供三块能力：系统监控、用量统计、邀请码发放。

**它不是什么**：不是内容管理后台。后台**不读取、不展示任何作品正文**，只看统计数字。

## 入口与权限

- 入口：左侧菜单「运营后台」（仅管理员可见），路由 `/admin`
- 权限判定与 `require_maintainer` 同一套：`is_admin_user()`，即 `users.is_admin` 或 `ADMIN_USER_IDS` 命中

防护分三层，缺一不可：

| 层 | 实现 | 效果 |
|---|---|---|
| 菜单 | `Sidebar.tsx` 的 `adminOnly` 过滤 | 普通用户看不到入口 |
| 路由 | `App.tsx` 用 `RequireAdmin` 包裹 | 手输 `/admin` 显示「仅管理员可见」，不渲染后台 |
| **接口** | 全部端点 `Depends(require_maintainer)` | **未登录 401、普通用户 403**（真正的防线） |

> 已用真实账号实测：普通用户访问 `/api/admin/overview|users|usage|invites` 全部 403，而 `/api/projects/` 仍 200。测试见 `backend/tests/test_admin_api.py`。

## 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/admin/overview` | 就绪状态与错误码、进程运行时长、服务器时间、最近备份、磁盘、数据规模 |
| GET | `/api/admin/users?days=N` | 用户列表：是否管理员、项目数、章节数、今日与近 N 日调用数与 token |
| GET | `/api/admin/usage?days=N` | 按日趋势、合计、项目 Top 10（次数与 token） |
| GET | `/api/admin/invites` | 邀请码列表（只有 hint，无明文） |
| POST | `/api/admin/invites` | 生成邀请码，支持 `count` 1–20 批量 |
| POST | `/api/admin/invites/{id}/disable` | 停用邀请码 |

`days` 会被夹在 1–90。

## 统计口径（重要）

- 调用次数与 token 来自 `project_llm_usage`（**项目 + UTC 日**维度），按用户聚合时需要 join `projects.owner_id`。
- 因此：**没有项目上下文的调用不计入这里**（表本身要求 `project_id`）。这是既有成本护栏的数据结构决定的，不是本页遗漏。
- 「今日」按 UTC 计算，对应北京时间 08:00 换日。

## 邀请码规则

- 明文**只在创建响应里出现一次**，库里只存 `sha256(码)` 与 `code_hint`（后 4 位）。丢了只能停用重发。这是原有设计，未改动。
- 一次最多生成 20 个；每个码的 `maxUses` 与有效期在创建时确定。
- 页面提供「复制全部邀请码」与「复制发送话术」——话术里的注册地址取**当前页面 origin**，所以：
  - 在 staging 后台复制出的话术指向 staging；
  - 在线上后台复制才指向线上。
- **邀请码 ≠ 管理员**：发出去的码只让对方注册普通账号。

## 隐私边界（明确不做）

| 不做 | 原因 |
|---|---|
| 查看/导出用户作品正文 | 隐私风险最高 |
| 停用账号、重置密码 | 误操作直接伤害真实用户 |
| 修改用户额度 | 未在需求内；额度本身有独立开关 |

因此后台只有两个写操作：创建邀请码、停用邀请码。

## 页面样式注意事项

主题色板（`tailwind.config.js`）里 `primary` 只有 50–900 色阶，**没有 `DEFAULT`**；状态色叫 `error`，**不是 `danger`**。

用 `bg-primary` / `text-danger` 这类不存在的类名时，Tailwind **静默忽略**，构建、类型检查、lint 全部通过，但元素会隐形（白底白字）。新增界面前请核对色板，或在构建后确认 CSS 里确有对应类。

## 已知风险

`DEBUG=true` 时 `current_user_id()` 会返回 `local-dev`，而它默认位于 `ADMIN_USER_IDS` —— 等于**对公网开放管理员权限**。生产必须保持 `DEBUG=false`。该行为已被 `test_debug_mode_anonymous_is_admin_by_design` 固定下来，避免有人误以为默认就是安全的。
