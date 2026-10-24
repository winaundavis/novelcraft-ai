# NovelCraft 公网邀请制 Beta 上线门禁

每次发布重新填写；空白、跳过或缺少证据不算通过。源码交付完成不等于公网运行就绪。本轮结果和测试范围见验收报告，实际操作见[部署与回滚手册](PUBLIC_BETA_DEPLOYMENT.md)；本清单不固定历史 commit、文件数或测试数字。

## 发布前自动检查

以下 PowerShell 从**项目根目录 novelcraft-ai/**执行。产物/报告使用仓库外的新绝对路径：

~~~powershell
$candidatePath = 'REPLACE_WITH_ABSOLUTE_CANDIDATE_ZIP_PATH'
$auditPath = 'REPLACE_WITH_ABSOLUTE_AUDIT_JSON_PATH'
git rev-parse HEAD
git status --short
py -3 backend/scripts/package_source.py --source . --output $candidatePath
if ($LASTEXITCODE -ne 0) { throw 'Package failed' }
py -3 backend/scripts/release_audit.py $candidatePath --json-out $auditPath
if ($LASTEXITCODE -ne 0) { throw 'Release audit failed' }
Get-FileHash -Algorithm SHA256 $candidatePath
~~~

要求审查 `passed=true`、`violations=[]`，ZIP 解压验证完整。记录实际源 commit、dirty、工具时间、ZIP SHA-256、文件清单与包内逐文件哈希（`release-build.json`）。dirty 源树的 commit 不能单独标识候选字节；审查后的任何包修改须重审/重算哈希。不得包含数据库/WAL/SHM、真实环境、上传/作品、私钥、凭据、运行日志、会话和内部历史文档；环境样例只能含占位值。

在干净解压副本中，生产运行安装 `backend/requirements-production.lock.txt` 并执行 `pip check`；后端测试安装 `backend/requirements-dev.txt`（包含生产锁）。执行当前后端测试、前端类型检查/单元测试/构建、明确隔离的浏览器用例；命令见[README](../README.md#测试)。记录准确命令、运行时版本、退出码、通过/失败/跳过数及原因。`RUN_LIVE_LLM=0` 不是数据隔离或网络拦截，仍须临时库、Fake Provider/mock，不能据此直接运行写真实库的旧巡检脚本。

使用隔离测试库演练，项目根目录 CLI 为：

~~~powershell
py -3 backend/scripts/backup_sqlite.py backup <隔离测试数据库> <新备份路径>
py -3 backend/scripts/backup_sqlite.py restore <新恢复数据库路径> <新备份路径>
~~~

恢复顺序是**目标数据库在前、源备份在后**。恢复后用恢复库启动隔离同版应用，读取测试项目/正文/上传，记录完整性与耗时。禁止以真实用户库作为自动演练目标。

## 运营人员必须完成并提交证据

| 门禁 | 核验内容 | 必留证据（脱敏） |
|---|---|---|
| 产物一致性 | 部署 ZIP 与审查 ZIP 相同，后端/静态前端同版 | release ID、ZIP SHA-256、commit/dirty、逐文件清单、审查结果 |
| 配置与身份 | Beta/HTTPS/host/Secure Cookie、绝对私有数据路径、真实管理员 ID | 字段合格状态、admin/普通账号 permissions、ready 状态；不附 secret |
| 凭据保护 | 主密钥独立保存/备份；新写入和历史配置加密且可解密 | 隔离 dry-run/迁移/再 dry-run 结果，末次 changed=0；继续原 provider key，不强制轮换 |
| 邀请制 | 无效/缺失/过期/停用/超配额拒绝，并发不超配额；共享码不可登录或作为 Header 身份 | 独立测试账号、状态码/断言；不含邀请码/Cookie 明文 |
| 用户隔离 | 双普通账号交叉测章节、会话、任务、配置、导入导出、回收站、审计；管理 API 为 403 | 双账号矩阵、预期/实际/失败项；区分管理员的有意跨项目权限 |
| HTTPS 与 SPA | 有效证书、HTTP 重定向、同源 API、深层刷新、Secure/HttpOnly；后端端口不公网直连 | 真实域名/TLS/浏览器断言、代理配置校验 |
| SSE 与 LLM 保护 | 不缓冲、超时合理；Fake Provider 流逐段到达/取消/超时/限流/预算/重试/未知价格 | 流事件时间或断言、Fake 日志、上游 CDN/代理超时配置 |
| 持久化与监管 | 非特权单 worker，重启后数据存在，受限日志及保留上限 | 服务配置、重启前后读取、路径标识（不含内容） |
| 备份与回滚 | 数据库/上传/配置/主密钥对应备份；隔离恢复可用；明确旧版兼容性与回滚分支 | 快照标识/时间、完整性、耗时、切版/恢复 smoke、数据丢失窗口 RPO/恢复目标 RTO |
| 真实模型（独立授权） | 限定账号、请求数和预算，验证目标 provider/模型/角色/生成流程 | 授权范围、请求数/token、已知账单或“未知”、结果；单次最小请求不代替完整链路 |
| 放行 | 全部门禁对应本次发布，缺证据不可标通过 | 运维负责人、时间、READY/NOT_READY 及具体未完成项 |

加密不会撤销旧源码包、明文库或备份里的原 key。继续原 provider key 是本次已接受的选择，轮换不是本次强制先决条件。token/call 限额不等于货币硬上限，费用证据须注明是否已核对账单。

## 每次发布的证据模板

复制到仓库外的私有发布记录，填实际证据路径/ID；不含密码、邀请码、Cookie、provider key、主密钥或作品原文：

~~~text
Release ID / timestamp / operator:
Source commit / dirty / per-file manifest:
Candidate path / SHA-256 / release-audit result:
Runtime versions / dependency evidence:
Backend command / exit / passed-failed-skipped / log:
Frontend command / exit / passed-failed-skipped / log:
Browser isolation / Fake Provider assertion / real-call count:
Deployed version / domain / HTTPS-cookie-SPA-SSE evidence:
Config-readiness / user-isolation / invite evidence:
Encryption-migration / master-key backup reference:
Database-upload backup IDs / restore result / RPO-RTO:
Previous release / schema-credential compatibility / rollback branch / drill:
Live-model authorization / requests / tokens / bill evidence:
Open items / READY or NOT_READY / sign-off:
~~~

当前没有真实平台、域名/证书、HTTPS/SSE 验收或生产恢复演练完成的证据，公网状态保持 `NOT_READY`。自动测试和包审查只能关闭各自门禁，不能自动关闭运营项。
