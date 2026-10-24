# NovelCraft 受控 Beta 发布清单

> 历史本地 Final Gate 的 PASS 不自动适用于当前代码。2026-09-13 更新说明：当前账户登录、数据政策、运行审计等待办见[当前状态](current-status.md)。对外部署前重新逐项验收；release-candidate.json 是 2026-09-03 历史哈希。

## 下一次发布需补验（尚未完成）

- [ ] 账户密码登录、失效会话、退出和同浏览器换用户无遗留身份；注销作品政策明确。
- [ ] Copilot 接受后的正文、项目总字数、检索与投影一致；未保存草稿不被覆盖。
- [ ] 所有 LLM 服务使用 resolver；无 run context 查询不隐式混入历史统计；project daily budget 跨 run 累计。
- [ ] Arts/官方 DeepSeek 计费源分别核实；无账单实际费用为 null；失败不承诺免费。
- [ ] 源码包检查 dotenv 变体、SQLite/备份、审计/浏览器产物；不包含真实 key、会话或小说。
- [ ] 离线数据库与附件备份、WAL 一致性、恢复到独立路径已验证。

每项在实际部署窗口由负责人标记为 `PASS`、`FAIL` 或 `N/A`。本阶段仅完成代码与本地 deterministic 验证，不自动部署。

## Final Gate 本地状态（2026-09-03）

以下是本地验收证据；带有部署者前置条件的项目保留为 `N/A`，不冒充已部署。

| 类别 | 状态 |
|---|---|
| Security / Auth / Ownership | PASS |
| Backend tests | PASS（787 passed, 1 warning） |
| Frontend typecheck / build | PASS |
| Upload / CORS / Health | PASS |
| AI budget / rate limit | PASS |
| Provider / run / audit isolation | PASS |
| Backup / restore / restart recovery | PASS（临时数据） |
| Packaging | PASS |
| 实际部署、HTTPS、告警联系人 | N/A（本阶段不部署） |
| 本阶段真实 LLM | 0 次，$0 |

## Security
- [ ] `PASS / FAIL / N/A` `DEBUG=false`，生产不注册 debug 路由。
- [ ] `PASS / FAIL / N/A` 401/403/404 语义一致，项目资源执行 owner 校验。
- [ ] `PASS / FAIL / N/A` 非管理员账号看不到「模型设置」入口、手输 `/settings` 打不开，且 `/api/settings/*` 返回 403（**隐藏 UI 不等于访问控制**）。
- [ ] `PASS / FAIL / N/A` 上传仅允许白名单扩展名、大小、MIME/签名，并使用随机文件名。
- [ ] `PASS / FAIL / N/A` 服务器路径导入在生产环境拒绝。

## Backend
- [ ] `PASS / FAIL / N/A` 使用 `python -m uvicorn main:app` 启动。
- [ ] `PASS / FAIL / N/A` 生产错误响应不返回 traceback、SQL、路径或凭据。
- [ ] `PASS / FAIL / N/A` `/api/health` 仅返回 `status` 与 `version`。

## Frontend
- [ ] `PASS / FAIL / N/A` `npm ci`、`npm run typecheck`、`npm run build`、`npm run test:unit` 通过。
- [ ] `PASS / FAIL / N/A` API 使用同源 `/api` 或显式 `VITE_API_BASE_URL`，无 localhost 硬编码。
- [ ] `PASS / FAIL / N/A` 静态服务器提供 production build 产物。

## Database
- [ ] `PASS / FAIL / N/A` SQLite 位于私有、持久化且可写的 `DATA_DIR`。
- [ ] `PASS / FAIL / N/A` 数据目录不在前端静态目录或公开下载目录。

## Backup
- [ ] `PASS / FAIL / N/A` 已执行手动备份并验证备份文件可读。
- [ ] `PASS / FAIL / N/A` 已在临时副本完成恢复演练，生产库未被覆盖。

## Secrets
- [ ] `PASS / FAIL / N/A` provider key、invite token 由 Secret Manager 注入。
- [ ] `PASS / FAIL / N/A` `.env` 未入 Git，前端构建未包含 provider key。

## AI Budget
- [ ] `PASS / FAIL / N/A` chapter、project daily call/token、retry、rewrite、QA 和 rate limit 均有硬上限。
- [ ] `PASS / FAIL / N/A` 额度耗尽后不会再次调用 provider。

## Monitoring
- [ ] `PASS / FAIL / N/A` 日志包含 request/user/project ID、操作、状态和延迟。
- [ ] `PASS / FAIL / N/A` 日志不包含完整 Prompt、正文、API key 或敏感用户信息。
- [ ] `PASS / FAIL / N/A` 已配置健康检查与错误告警联系人。

## Recovery
- [ ] `PASS / FAIL / N/A` 已验证重启后项目、章节、Story State、Style、Memory 可恢复。
- [ ] `PASS / FAIL / N/A` 已准备浏览器真人 smoke，但尚未在本阶段调用真实模型。

## Rollback
- [ ] `PASS / FAIL / N/A` 已保存本次 `release-candidate.json` 与构建产物校验值。
- [ ] `PASS / FAIL / N/A` 已确认可恢复上一版本应用与数据库备份。
