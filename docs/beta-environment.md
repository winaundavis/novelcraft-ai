# Beta 环境与运行限制
更新：2026-09-13。以下是运行要求，不是已完成部署证明。

```text
environment = beta
debug = false
single_instance = true
database = sqlite
auth = account-password + legacy-invite
llm_budget = configured
```

账户密码＋HttpOnly session 是当前登录方式；旧 invite header 仍可兼容身份。普通用户按 owner 隔离，管理员的既有访问与 server-global 模型设置权限保留；不引入复杂 RBAC。新注册是否需邀请码取决于 INVITE_TOKENS 是否为空。

这是受控 Beta，不是生产 SaaS。只支持单后端实例／worker，内存 rate limiter 不跨实例共享。不支持横向扩容、HA 集群、团队工作区、复杂多租户权限、在线支付、订阅或自动计费。

Beta、Experiment、Test 应使用不同 SQLite、上传与报告目录，并在运行 manifest 标明 environment 和 run_id。普通全量 pytest 会设置临时 DB；裸脚本/TestClient 不会自动获得该隔离。项目 daily budget 仍按项目／日期跨 run 累计，不能通过换 run 绕过。

provider／model／价格以最终路由为准，code default 和激活 DB config 不必相同。新模型操作默认不开 live，单次人工验证先核预算；不得套用 DeepSeek 官方价格给 Arts 账单。

当前仍有运行待办，尤其 logout、账号数据政策、全文问答 resolver/费用、无 context 的 run 查询。旧阶段 PASS 不覆盖这些后来发现的问题。配置与备份见[启动文档](production-startup.md)。

## 2026-10-24 增补：额度与部署相关配置

| 变量 | 默认 | 语义 |
|---|---|---|
| `USER_DAILY_CHAPTER_LIMIT` | `10` | 非管理员**每用户每日**「按大纲自动创作」可用章数。只统计成功落库的章；改写/审校/续写/大纲/人物/对话**不占用**；管理员不受限 |
| `USER_DAILY_CHAPTER_LIMIT_ENABLED` | `true` | 灰度开关。`false` = 整体放开（小范围测试期）；`true` = 恢复限制。**上线只改这一行，额度数字保持不动** |
| `NOVELCRAFT_STATIC_DIR` | 空 | 可选。指向已构建的 `frontend/dist` 时，后端在同一端口同时提供静态站点与 API（单端口部署用）；不设置则仅提供 API，由反向代理托管静态文件 |

与既有护栏的关系：`PROJECT_DAILY_LLM_CALL_LIMIT` / `PROJECT_DAILY_TOKEN_LIMIT` 是**成本护栏**（项目＋UTC 日，计调用与 token），用于防刷爆账单；`USER_DAILY_CHAPTER_LIMIT` 是**产品额度**（用户＋UTC 日，计章数）。两者维度不同，不能互相替代——一次自动创作会消耗几十次 LLM 调用，而审校只消耗调用次数却不该占用章数额度。

生产就绪校验：开关为 `true` 时要求 `USER_DAILY_CHAPTER_LIMIT > 0`（防止误配静默关闭额度）；开关为 `false` 时允许 0。

> `DEBUG` 必须为 `false`。为 `true` 时匿名请求会被解析成 `local-dev`，而它默认在 `ADMIN_USER_IDS` 中，等于对公网开放管理员权限。

实际运行环境、目录、发布与回滚流程见[云主机运维手册](OPS_RUNBOOK.md)；管理端能力与权限模型见[运营后台](ADMIN_CONSOLE.md)。
