# 单实例 Beta 启动与恢复
更新：2026-09-13。适用于当前账户密码登录＋旧邀请码兼容版本。本文提供部署步骤，本轮未部署。先阅读[当前状态](current-status.md)的已知问题。

## 1. 依赖与工作目录
使用 Python 3.11、Node.js 20 LTS／npm；固定依赖见 requirements 与 package-lock。下面后端命令在 `backend/` 执行，备份命令单独注明从项目根执行。不要依赖含本机路径的 `start.bat`。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

当前完整测试证据来自已有环境，尚未重新验证全新机器上的所有 Python 锁定依赖；部署者需要记录安装结果。

## 2. 私有环境配置
根目录 [.env.production.example](../.env.production.example) 是**后端变量样例**。只在目标 `backend/.env` 不存在时复制并编辑，或通过进程环境注入。Settings 从进程工作目录读取 `.env`；不自动加载根目录 `.env.production.example`。不要把 Vite 变量放进后端 dotenv，当前 Settings 对多余字段可能报错。

| 变量 | 配置意义 |
|---|---|
| DEBUG | Beta 必须 false；不要使用旧 APP_DEBUG |
| DATA_DIR / NOVELCRAFT_DB_PATH | 私有可写持久目录／SQLite 完整路径；为空 DB override 时使用 DATA_DIR/novelcraft.db |
| UPLOAD_DIR / UPLOAD_MAX_BYTES | 私有上传目录，默认上限 10 MiB |
| CORS_ALLOW_ORIGINS | 允许的浏览器 origin，逗号分隔，不带 /api 路径；Beta 必须是明确的 HTTPS 来源 |
| AUTH_COOKIE_SECURE | HTTPS 必须 true；本地 HTTP 验证为 false，否则浏览器不发 Cookie |
| ENVIRONMENT | `development` 或 `beta`；公网 Beta 必须设为 `beta` |
| ALLOWED_HOSTS | 反向代理传入的 Host 白名单；Beta 必须明确配置，不能使用 `*` |
| INVITE_TOKENS | 旧 header 映射仅供兼容；数据库邀请码为空或缺失时注册会拒绝，不会开放注册 |
| APP_ENCRYPTION_KEY | 由 Secret Manager/进程环境注入的 Fernet 密钥，用于 SQLite 中 provider key 的应用层加密；Beta 缺失或占位时 readiness 失败 |
| ADMIN_USER_IDS | 稳定用户 ID 列表，不是账户名；生产不得使用 local-dev。**该名单（或数据库里的管理员标记）决定谁能管理服务器模型配置，也决定前端是否显示「模型设置」入口** |
| DEFAULT_LLM_PROVIDER / DEFAULT_LLM_MODEL | 环境默认；激活数据库配置和角色覆盖可能优先 |
| DEFAULT_MAX_TOKENS / DEFAULT_TEMPERATURE | 正文起点 4096／0.8；不能据此推断任务最终参数 |
| ARTS_API_KEY / ARTS_API_BASE | 使用 Arts 时从私有环境注入；不要混用官方 DeepSeek 账单 |
| DEEPSEEK_API_KEY | 直连官方 DeepSeek 时使用；当前无 DEEPSEEK_API_BASE Settings 字段 |
| PROJECT_DAILY_LLM_CALL_LIMIT / PROJECT_DAILY_TOKEN_LIMIT | 项目／日期额度；所有 run 共同消耗 |
| RATE_LIMIT | 当前内存限流阈值；多实例不会共享 |
| LOG_LEVEL / HEALTH_PATH | 日志级别及公开健康路径，默认 INFO／/api/health |
| FULLTEXT_MAX_CHARS | 全文问答输入字符保护，不是花费上限 |

数据、上传、数据库及备份不能位于 `frontend/dist` 或任何公开静态／下载目录；限制 OS 文件权限。生产配置要求 provider key 在 SQLite 中使用 `APP_ENCRYPTION_KEY` 应用层加密；历史明文配置必须先离线迁移或轮换，不能把“响应脱敏”当作数据库加密。

## 3. 首个账户与管理员
当前 UI 提供登录，尚无完整自助注册／管理员初始化向导。新安装先仅监听本机，使用 `POST /api/auth/register` 创建账号，收到 `data.id` 后将该 ID 配置为 `ADMIN_USER_IDS`，重启后端，再登录。不要复用其他用户身份或直接修改历史 owner。

配好并重启后，用该账号登录时侧边栏会出现「模型设置」；普通账号看不到这个入口（手输 `/settings` 也打不开，接口侧返回 403）。判定结果可用 `GET /api/auth/permissions` 核对 —— 它认账户 session，也认旧 invite header。

可在 PowerShell 交互输入（不在命令文本或仓库写真实密码；不启用 transcript）：

```powershell
$ncUsername = Read-Host '新账户名'
$ncPassword = Read-Host '密码（至少 8 字符）' -AsSecureString
$ncInvite = Read-Host '注册邀请码（受控 Beta 必填；服务端不配置邀请码时注册会拒绝）'
$ncCredential = [System.Net.NetworkCredential]::new('', $ncPassword)
$ncBody = @{ username = $ncUsername; password = $ncCredential.Password; invite = $ncInvite } | ConvertTo-Json -Compress
try {
    $ncRegistration = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/auth/register' -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($ncBody))
    $ncRegistration.data.id
} finally {
    Remove-Variable ncBody,ncCredential,ncPassword,ncInvite -ErrorAction SilentlyContinue
}
```

只输出账号 ID。设置邀请码时格式为 `私有令牌:兼容用户ID`；通过注册创建的账号 ID 不自动等于邀请映射 ID。已有管理员和旧项目使用原数据，不运行初始化脚本。跨身份项目迁移是独立的数据操作，须先备份并核对授权。

目前 `/auth/me` 只承认账号会话，旧项目 API 仍支持 invite header；`/auth/permissions` 两种都认（用于前端判断管理入口是否显示）。注销／退出边界待办见工作清单。

## 4. Backend startup
```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```
保持一个实例／worker，不使用 reload。数据库、后台任务与内存 rate limiter 不支持本文档之外的水平扩容。使用对应终端 Ctrl+C 停止，等待活动任务退出。不要按端口批量杀进程。

## 5. Frontend build
在独立终端，从项目根执行：
```powershell
cd frontend
npm ci
$env:VITE_API_BASE_URL = '/api'
$env:VITE_BETA_AUTH_REQUIRED = 'true'
npm run typecheck
npm run build
```
Vite 环境变量在构建时固定；改变后重新 build。不要向任何 VITE_* 变量放 provider key。本地开发可运行 `npm run dev -- --host 127.0.0.1`，设置 `NOVELCRAFT_PROXY_TARGET=http://127.0.0.1:8000` 指向后端。生产不依赖 Vite dev server。

## 6. 反向代理与 CORS
生产由代理提供 HTTPS、静态 `frontend/dist` 和同源 `/api`，保留 API 前缀、Cookie 和长连接。以下为 nginx server 内片段，TLS 证书和路径由部署者配置：

```nginx
root /srv/novelcraft/frontend/dist;
location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 600s;
    client_max_body_size 12m;
}
location / {
    try_files $uri $uri/ /index.html;
}
```

禁用流式缓冲用于及时显示正文；代理总请求上限留 multipart 开销，应用仍按上传文件本身 10 MiB 限制。不要配置能下载 data、uploads 或备份目录的 location。跨源 API base 必须包含 `/api`，CORS 精确配置 frontend origin 并保留 credentials；当前 SameSite=Lax 会限制真正跨站 Cookie，优先同源，不能仅设置 CORS 就保证跨站登录成功。

## 7. 健康、日志与预算
```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```
公开响应只能含 `status` 和 `version`。登录维护者才可访问 `/api/health/internal` 查看 provider_configured、database_ready、storage_ready。健康成功不代表 provider 可付费调用，检查本身不请求模型。

日志保留请求 ID、operation、status、latency 等安全元数据；禁止添加完整 Prompt、小说、密码、用户隐私和 provider key。run/audit 默认范围与全文问答费用存在待办，不能把现有统计当精确账单。限流和 daily budget 是两层控制，不等价于美元／人民币花费硬上限。

## 8. 备份与恢复
从项目根运行：
```powershell
python backend/scripts/backup_sqlite.py backup <database-path> <new-backup-path>
python backend/scripts/backup_sqlite.py restore <new-database-path> <backup-path>
```
将尖括号替换为已核实的绝对路径；含空格时加引号。备份命令使用 SQLite 在线 `Connection.backup()`，会在事务一致的快照中包含已提交的 WAL 页面，并在完成后自动执行 `PRAGMA integrity_check` 与外键检查。CLI 严格拒绝非 SQLite 文件；备份前仍应正常停止后端及其他写者，避免业务继续写入造成恢复时序不确定。

备份目的已存在时拒绝；备份完成后会再次校验快照。脚本只处理数据库文件，不包含目录外的上传附件；附件应另行私有备份。不要把备份放到 `frontend/dist` 或任何可下载目录。

恢复目标存在时默认拒绝。优先恢复到全新私有路径，设置新 DB_PATH，在另一个本地端口检查账户／项目／章节完整后再决定切换。测试恢复只用测试数据库。恢复会在替换前为当前数据库创建带微秒时间戳的 `*.pre-restore-<timestamp>.bak` 保留副本，并对恢复结果执行同样的完整性检查。

明确确认替换当前库后才使用：
```powershell
python backend/scripts/backup_sqlite.py restore <database-path> <backup-path> --replace
```
它会先保留 `*.pre-restore-<timestamp>.bak`；不要在相同秒重复覆盖，也不要留下不匹配的旧 WAL/SHM。若后端仍在写入，恢复会被文件锁阻止，应先停止服务后重试。恢复命令同样严格拒绝非 SQLite 归档。

## 9. 发布与回滚
每次部署重新填写[Beta 发布清单](beta-release-checklist.md)，记录代码版本、环境（脱敏）、当前数据路径、备份位置和测试结果。保留旧代码及独立备份；遇到不兼容迁移先离线恢复到新路径验证，不把历史 release-candidate.json 的哈希当当前结果。
