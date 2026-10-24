# NovelCraft Beta 部署检查清单

更新：2026-09-13。此处是待执行检查项，不是部署PASS；最新待办见[发布清单](beta-release-checklist.md)和[当前状态](current-status.md)。

- [ ] 账户密码登录/失效/退出/换用户无残留身份；ADMIN_USER_IDS使用账号ID，注销作品政策明确。
- [ ] 用非管理员账号核验「模型设置」入口不出现、手输 `/settings` 打不开、`/api/settings/*` 返回 403。
- [ ] HTTPS下 AUTH_COOKIE_SECURE=true；优先同源 /api，SSE反向代理不缓冲。
- [ ] 确认 Settings UI 保存的DB可能含provider key，限制数据库/备份读取权限，响应脱敏不当作静态加密。

- [ ] CORS 仅允许正式前端 origin，不使用 `*`。
- [ ] Secret 由平台 Secret Manager 注入；仓库只保留 placeholder，且 `INVITE_TOKENS` 与 provider key 不进入前端构建环境。
- [ ] `DEBUG=false`，健康检查不返回 PID、密钥或 traceback。
- [ ] 上传大小、类型和存储目录已限制；关闭服务器本地路径导入。
- [ ] 生产 SQLite/数据目录位于持久卷，并完成恢复演练。
- [ ] 部署前后各有一次备份，明确保留周期和恢复负责人。
- [ ] 日志脱敏，不记录 API key、完整用户正文或 provider 凭据。
- [ ] 反向代理启用 HTTPS、认证和基础 rate limit。
- [ ] `/api/health` 仅返回status/version；`/api/health/internal` 限维护者；错误响应不泄露内部信息。
- [ ] 前端 API base 指向生产 API，未混入 localhost。
- [ ] 前端 `npm run build` 产物通过静态服务器提供。
- [ ] 后端测试、前端 typecheck、deterministic E2E 全部通过。
- [ ] 先零API验证，再在当次预算/次数授权下人工做小规模smoke；历史真实测试授权不能复用为无限调用。
- [ ] 使用 `python backend/scripts/backup_sqlite.py backup <db> <backup>` 演练手动备份；恢复默认拒绝覆盖，确需覆盖时使用 `restore <db> <backup> --replace`，脚本会生成 pre-restore 副本。
- [ ] 配置注册/旧身份兼容所需的 `INVITE_TOKENS`、项目日调用/Token上限和 `RATE_LIMIT`；daily预算跨run累计，run统计隔离；生产API base包含 /api。
- [ ] 备份前停写并确认WAL一致性，先恢复到独立路径验证；上传附件另备份。
- [ ] 源码包检查dotenv变体、SQLite/WAL、备份和测试产物，不能只依赖当前package_source过滤。
- [ ] 生产日志仅保留 request ID、用户/项目 ID、操作、状态与延迟；不得记录请求体、完整 Prompt、正文或凭据。
