# NovelCraft 单实例 Beta 部署与回滚手册

本手册是可复用操作模板，不是已完成公网部署的证明。真实平台、域名、证书、服务账号、持久化路径、备份保留期和负责人由部署者填写；完成[上线门禁](PUBLIC_BETA_GO_LIVE_CHECKLIST.md)前不开放招募。已执行证据以验收报告为准。

## 1. 部署契约与目录

运行一个 Uvicorn 进程、一个 worker；前端构建为静态文件，由 HTTPS 代理同源转发 `/api`。进程内限流和 SQLite 未验证多副本写入，不能直接增加 worker 或横向扩容。Vite dev/preview 不作为公网静态服务器。

仓库 Vite 开发默认监听 `127.0.0.1`，Host 白名单为 `localhost`/`127.0.0.1`；这仅约束开发服务，不替代生产 Nginx。不得为部署改回 `0.0.0.0`/`allowedHosts:true` 后直接开放开发服务器。

下面给出 Linux + systemd + Nginx 原生参考配置，不要求特定云平台。其他平台须实现同样的进程监管、同源 TLS、流式转发和持久化契约。宿主机先具备 Python 3.11、venv、兼容前端锁文件的 Node.js/npm、systemd 和 Nginx；本手册不安装或操作这些基础设施。

| 路径（可替换，但须统一） | 用途 |
|---|---|
| `/srv/novelcraft/releases/<release-id>/novelcraft-ai` | 每版独立源码、后端 venv、前端 dist；不原地覆盖旧版 |
| `/srv/novelcraft/current` | 当前版本链接；后端与前端一起切换 |
| `/etc/novelcraft/backend.env` | 私有后端配置，不能放进静态目录或源码 ZIP |
| `/srv/novelcraft-private/data` | SQLite 和应用数据，跨版本保留 |
| `/srv/novelcraft-private/uploads` | 上传文件，跨版本保留 |
| `/var/backups/novelcraft` | 应挂载到与应用卷分离的备份存储 |

创建专用无登录账号 `novelcraft`。发布目录由部署管理员拥有，服务账号只读；数据/上传目录由服务账号拥有，模式 `0700`；私有配置例如 `root:novelcraft 0640`。Nginx 账号只需读取前端 dist，不能读取私有目录。为 journal、代理日志配置保留上限/轮转；持久化 journal 由宿主机配置。

新 Linux 主机可由管理员一次性准备如下；已有同名账号/目录时先核对，不重复创建账号。备份存储应先按实际平台挂载，避免误写宿主机本地卷。

~~~bash
sudo useradd --system --home-dir /srv/novelcraft-private --shell /usr/sbin/nologin novelcraft
sudo install -d -o root -g root -m 0755 /srv/novelcraft /srv/novelcraft/releases
sudo install -d -o root -g novelcraft -m 0750 /etc/novelcraft /srv/novelcraft-private
sudo install -d -o novelcraft -g novelcraft -m 0700 /srv/novelcraft-private/data /srv/novelcraft-private/uploads /var/backups/novelcraft
~~~

## 2. 候选包与安装

先在开发机或 CI 从项目根目录打包、审查，记录源提交/工作区状态；命令和证据字段见[上线门禁](PUBLIC_BETA_GO_LIVE_CHECKLIST.md)。将候选 ZIP、旁置审查报告、SHA-256 传到主机，主机重新核对哈希；不沿用历史 ZIP 或 `release-candidate.json`。

以下在主机 Bash 中执行，替换发布 ID 与已审查 ZIP 路径，同名目录应不存在。ZIP 内层目录为 `novelcraft-ai/`：

~~~bash
set -euo pipefail
release_id='REPLACE_WITH_RELEASE_ID'
release_parent="/srv/novelcraft/releases/$release_id"
test ! -e "$release_parent"
sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0755 "$release_parent"
unzip /path/to/reviewed-candidate.zip -d "$release_parent"
release_dir="$release_parent/novelcraft-ai"
cd "$release_dir/backend"
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-production.lock.txt
.venv/bin/python -m pip check
cd "$release_dir/frontend"
npm ci
env VITE_API_BASE_URL=/api VITE_BETA_AUTH_REQUIRED=true npm run build
test -f dist/index.html
chmod -R a+rX "$release_parent"
~~~

记录 Python、Node、npm 版本及安装日志。后端生产使用 `requirements-production.lock.txt` 固定的依赖闭包（CPython 3.11、Windows/Linux），测试另用 `requirements-dev.txt`；后者包含生产锁和测试工具。留存该版锁文件、`pip check`/`pip freeze` 输出、已验证的 venv/依赖下载缓存供回滚。前端 `npm ci` 读取 `package-lock.json`。`VITE_*` 构建时写入公开 JavaScript，不能放 provider key；部署后改变这些变量须重新构建。

依赖审查需分别保存生产范围与包含 dev 的完整结果，不能以 `npm audit --omit=dev` 代替构建链审查。本轮仅对 nanoid/source-map-js 做兼容补丁，Vite/Tailwind 开发链主版本升级延后，最终残余数字以验收报告为准。生产只服务静态 dist；构建环境仅处理可信仓库源码、CSS 和 source map，不运行不可信 PR 输入。若构建来源改变，需重新评估并处理开发链残余问题。

## 3. 配置、初始化与首个管理员

以根目录 `.env.production.example` 为字段模板，建立 `/etc/novelcraft/backend.env`：

~~~bash
if ! sudo test -e /etc/novelcraft/backend.env; then
    sudo install -o root -g novelcraft -m 0640 "$release_dir/.env.production.example" /etc/novelcraft/backend.env
fi
sudoedit /etc/novelcraft/backend.env
~~~

已有配置仅编辑必要字段，不重复复制模板。上述示例以具有 sudo 权限的部署账号构建，应用服务账号没有源码写权限。

- `ENVIRONMENT=beta`、`DEBUG=false`、`AUTH_COOKIE_SECURE=true`。
- `CORS_ALLOW_ORIGINS=https://真实域名`；`ALLOWED_HOSTS` 只写主机名，多个以逗号分隔，不含协议或路径。
- `DATA_DIR`、`NOVELCRAFT_DB_PATH`、`UPLOAD_DIR` 指向上表绝对私有路径。
- 通过 `APP_ENCRYPTION_KEY_FILE=/etc/novelcraft/master.key` 读取受限文本文件，或私有环境注入 `APP_ENCRYPTION_KEY`；两者同时设置时必须相同，否则失败。主密钥独立备份，丢失将使加密配置不可读。provider 凭据与此主密钥不同。
- `INVITE_TOKENS=`，新邀请码通过数据库工具创建。

首次生成 Fernet 主密钥可在受保护终端运行 `"$release_dir/backend/.venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`，立即存入私有文件/Secret Manager，不写报告、日志或 shell 命令参数。文件模式例如 `root:novelcraft 0640`，只保存一行主密钥，不加 `KEY=`。选择文件来源时删去/清空内联 APP_ENCRYPTION_KEY 占位值。升级复用原主密钥，不能每次重新生成或覆盖现有主密钥文件。

以下临时 systemd 单元将私有配置注入运维命令，保持与应用一致的身份和工作目录；`release_dir` 沿用上一节。普通 Python 只自动读工作目录的 `.env`，不会自动读 `/etc/novelcraft/backend.env`。

~~~bash
sudo systemd-run --wait --pipe --collect \
  -p User=novelcraft -p Group=novelcraft \
  -p "WorkingDirectory=$release_dir/backend" \
  -p EnvironmentFile=/etc/novelcraft/backend.env \
  "$release_dir/backend/.venv/bin/python" -c 'from app.db import init_db; init_db()'
~~~

首次 Beta 使用全新空库，不直接复制本机开发库。历史数据迁移须另行对隔离副本做完整性/外键检查；若有历史孤儿记录，先确定保留数据的一致性修复方案，检查失败不得跳过或通过删除作品凑数。

仅对新库或已备份的升级副本执行初始化；应用启动同样会执行 `init_db()`，可能改变表结构/数据，不能当作只读检查。仓库没有自动逆向迁移或数据库降级工具。

首次没有账号 ID，可暂置 `ADMIN_USER_IDS=`，仅允许运维者访问临时 HTTPS 入口（代理 IP 白名单），不开放招募。先生成短期单次邀请码：

~~~bash
sudo systemd-run --wait --pipe --collect \
  -p User=novelcraft -p Group=novelcraft \
  -p "WorkingDirectory=$release_dir/backend" \
  -p EnvironmentFile=/etc/novelcraft/backend.env \
  "$release_dir/backend/.venv/bin/python" -m scripts.invite_tool new \
  --count 1 --label bootstrap --max-uses 1 --expires-at REPLACE_WITH_FUTURE_ISO8601_TIME
~~~

明文只显示一次，该输出不附公开证据。按下节启动服务，在受限 HTTPS 页面注册；从已登录 `/api/auth/me` 的 `data.id` 取得账号 ID，写入 `ADMIN_USER_IDS` 后重启。初始化阶段 `/api/ready` 因管理员未配置返回 503 是预期阻断，注册路由仍可使用。随后核对管理员 `/api/auth/permissions` 为 `isAdmin=true`，普通账号为 `false`。

## 4. 进程监管

保存以下为主机 `/etc/systemd/system/novelcraft.service`，内容不含密钥：

~~~ini
[Unit]
Description=NovelCraft single-instance backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=novelcraft
Group=novelcraft
WorkingDirectory=/srv/novelcraft/current/backend
EnvironmentFile=/etc/novelcraft/backend.env
ExecStart=/srv/novelcraft/current/backend/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
Restart=on-failure
RestartSec=5
TimeoutStopSec=90
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/srv/novelcraft-private/data /srv/novelcraft-private/uploads
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
~~~

首次创建当前链接后启动：

~~~bash
test ! -e /srv/novelcraft/current
sudo ln -s "$release_dir" /srv/novelcraft/current
sudo systemctl daemon-reload
sudo systemctl enable --now novelcraft
sudo systemctl status novelcraft --no-pager
sudo journalctl -u novelcraft -n 80 --no-pager
~~~

只监听回环，代理/后端同机。其他网络拓扑需将可信代理设为明确受控地址并限制后端端口，不能使用 `--forwarded-allow-ips '*'`。不开 `--reload`。停服前暂停入口、等待生成任务结束，不把强制中断任务误当作成功。

## 5. HTTPS、SPA 与 SSE 代理

以下置于 Nginx `http` 上下文。替换域名/证书路径；证书申请、续期与防火墙由实际平台负责。初始化/验收期间在 HTTPS server 增加 `allow 运维出口IP; deny all;`，完成门禁后解除。

~~~nginx
# Omit query strings; never log Cookie/Authorization/request bodies.
log_format novelcraft '$remote_addr $request_method $uri $status $request_time';
server {
    listen 80;
    server_name app.example.invalid;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    server_name app.example.invalid;
    ssl_certificate /etc/ssl/novelcraft/fullchain.pem;
    ssl_certificate_key /etc/ssl/novelcraft/privkey.pem;
    root /srv/novelcraft/current/frontend/dist;
    index index.html;
    client_max_body_size 12m;
    access_log /var/log/nginx/novelcraft-access.log novelcraft;

    location ^~ /api/ {
        # No trailing slash: retain backend /api/ prefix.
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_connect_timeout 5s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
    location ~ /\. { deny all; }
    location /assets/ { try_files $uri =404; }
    location / {
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache";
    }
}
~~~

`12m` 留出 multipart 开销；应用默认单文件限制 10 MiB，改变 `UPLOAD_MAX_BYTES` 时同步代理上限。SSE 须关闭缓冲/压缩、延长空闲超时，还须确认上游负载均衡/CDN 不缓冲或提前断开。禁止为源码根、数据、上传、备份配置静态 alias。

~~~bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail --show-error https://app.example.invalid/api/health
curl --fail --show-error https://app.example.invalid/api/ready
~~~

`health` 仅表明进程存活；`ready` 在配置/数据库/存储检查通过时返回 200，不证明 TLS 全链、账单或生成质量合格。配置错误码只在维护者保护的 `/api/health/internal` 中显示。浏览器另验注册/登录 Cookie 的 Secure/HttpOnly、深层 SPA 刷新；以隔离 Fake Provider 流式任务确认事件逐段到达。真实模型验收单独按用户授权执行。

显式 `production`/`prod`/`beta` 的 Cookie 客户端执行 POST/PUT/PATCH/DELETE 等 API 写请求时，必须携带与 `CORS_ALLOW_ORIGINS` 精确匹配的 `Origin`，否则返回 403；浏览器通常自动携带，原生客户端若使用会话 Cookie 也需明确发送可信 Origin。`null`、重复或伪造代理头不能代替该来源；不带会话 Cookie 的原生 Header 鉴权客户端保留无 Origin 的兼容方式。不能仅凭 SameSite=Lax 判断 CSRF 已验收。

`GET /api/settings/llm-health` 仅供管理员读取配置快照，`checked=false` 表示尚未探测，不调用 provider。设置页进入时只读配置；“检测连接”按钮才发出受 Origin guard 约束的 POST，实际触发单次模型请求，输出 token 参数上限 32、请求 timeout 参数 15 秒、关闭 thinking，仍可能计费。该管理操作受通用 IP 限流，但不绑定项目，**不计入项目每日额度，也没有全局每日金额上限**；32 只限制输出，timeout 不是绝对总耗时上限。进程监控、部署 smoke 和本轮零真实调用验收使用 `/api/health`、`/api/ready` 及 Fake Provider，不自动调用该 POST。

## 6. 备份、明文迁移与隔离恢复

备份/恢复脚本不导入应用配置。下面从项目根目录执行 PowerShell，路径由操作者赋值，备份/恢复目标须是新路径：

~~~powershell
$dbPath = 'REPLACE_WITH_ABSOLUTE_DATABASE_PATH'
$backupPath = 'REPLACE_WITH_NEW_BACKUP_PATH'
$restorePath = 'REPLACE_WITH_NEW_ISOLATED_DATABASE_PATH'
py -3 backend/scripts/backup_sqlite.py backup $dbPath $backupPath
py -3 backend/scripts/backup_sqlite.py restore $restorePath $backupPath
~~~

CLI 顺序始终为 `动作 数据库路径 备份路径`，恢复是 **`restore <目标数据库> <源备份>`**。两种操作均校验 SQLite integrity/foreign keys，online backup 包含已提交 WAL 内容。恢复到已有文件须加 `--replace`，会保留 `.pre-restore-<timestamp>.bak`；禁止恢复运行中数据库，不要只替换主文件并遗留活动 WAL/SHM。

数据库备份不是完整灾备：暂停写入期间同时备份上传目录、版本、私有配置、独立保管的主密钥，记录快照对应关系及保留期。历史明文备份仍含凭据，本地加密不会撤销这些副本。

本次继续使用原 provider key，不强制轮换。先备份历史 SQLite，在停服副本试迁移，再处理选定目标；**应用、监控、数据库查看器等全部访问者须停止**。工具仅支持 DELETE/WAL journal 模式，使用 `secure_delete`、离线 VACUUM 及 WAL TRUNCATE 清理本地 SQLite 残片，遇 busy 或清理未完成须失败退出。从 **`backend/`** 执行，使用装好 `requirements-production.lock.txt` 的 Python；下面明确导出主密钥文件路径，CLI 不会自动从 `.env` 加载这两个来源变量：

~~~powershell
$env:APP_ENCRYPTION_KEY = ''
$env:APP_ENCRYPTION_KEY_FILE = 'REPLACE_WITH_ABSOLUTE_PRIVATE_KEY_FILE'
py -3 -m scripts.migrate_llm_keys $restorePath --dry-run
py -3 -m scripts.migrate_llm_keys $restorePath
py -3 -m scripts.migrate_llm_keys $restorePath --dry-run
~~~

dry-run 只读核对业务数据库和 WAL，不改其中业务数据/文件字节；SQLite 读连接仍可能改变 SHM 读锁/索引元数据，不承诺 SHM 字节完全不变。第一次报告待改数量；迁移后应为 `changed=0`，并验证密文可由同一主密钥解密及数据库/WAL 的文件清理结果。若依赖只装在 venv，将示例 `py -3` 替换为 `.\.venv\Scripts\python.exe`。Linux 使用第 3 节 systemd-run 注入配置，将命令改为 `.venv/bin/python -m scripts.migrate_llm_keys /绝对/目标.db`。不要直接执行 `python scripts/migrate_llm_keys.py`，其应用导入需要后端模块工作目录。不要打印凭据/数据库配置内容。

清理失败时，逻辑加密事务可能已经提交；保持所有访问者停止，保留原文件与备份，用**同一 master key** 重跑，不能换 key 后直接覆盖。此流程只处理选定 SQLite 文件及其本地 journal，不抹除外部旧备份、Git 历史或 SSD 的底层历史。

隔离恢复后，用独立 `DATA_DIR`、`NOVELCRAFT_DB_PATH`、`UPLOAD_DIR` 和端口启动同版应用，注入原主密钥，实际验 ready、测试账号登录、项目/章节/上传读取和权限；检查本身不调用模型。记录快照时间、恢复耗时、结果、可接受的数据丢失窗口。只有完整性检查不能算完成应用恢复演练。

## 7. 升级、回滚与失败分支

每次记录发布 ID、候选哈希、旧版绝对路径、数据库/上传快照路径和时间、配置版本、主密钥保管位置（仅标识）。保留上一版源码、静态构建和 venv。

1. 在隔离恢复副本安装候选并初始化/迁移，验证读取、权限、Fake Provider 流。如果准备只回退代码，额外验证**旧版能读升级库和加密配置**；没有证据默认不兼容。
2. 代理切维护入口/IP 白名单，等待任务结束，`sudo systemctl stop novelcraft`。完成停服后数据库/上传快照并验证可恢复；验收通过前保持写入关闭。
3. 候选依赖/静态构建完成后切换当前链接并启动。`release_dir` 为新版本绝对路径，`current.next` 须不存在：

   ~~~bash
   test ! -e /srv/novelcraft/current.next
   sudo ln -s "$release_dir" /srv/novelcraft/current.next
   sudo mv -Tf /srv/novelcraft/current.next /srv/novelcraft/current
   sudo systemctl start novelcraft
   ~~~

4. 核对 HTTPS ready、版本/产物标识、登录、SPA、读写、权限、SSE；完成门禁再恢复流量。浏览器重新加载 HTML，旧版产物留在原目录。

| 失败阶段 | 处理 |
|---|---|
| 新后端未启动，数据库未变化 | 停服，将上面链接目标换为旧版绝对路径，再启动并验收 |
| 已初始化，但已验证旧版兼容且无需撤销数据变化 | 停服，留失败快照，切旧版代码保留当前库，重新验证读写/权限 |
| 库结构/凭据格式不兼容或未知 | 停服，保留失败库/上传/日志；发布前备份恢复到**新路径**，配对应上传快照、旧版代码、配置与主密钥，再验收 |
| 已重新开放写入后失败 | 暂停写入、留最新快照；明确恢复旧快照将丢失的新增/修改时间段，由运营者决定是否恢复，不自动覆盖新数据 |

只切旧 Git 提交不构成数据库回滚。恢复分支修改私有路径后，若目录不在 systemd `ReadWritePaths` 中须同步白名单并 daemon-reload；恢复副本可放既有私有 data 目录内用新文件名。核对实际数据库/上传路径和版本对应，再开放入口。

## 8. 日常处置

- 先查服务状态、受限日志、health/ready；上游失败不能当成功正文。
- 模型不可用、预算保护失效或异常调用时暂停生成，保留请求 ID、脱敏错误、额度/账单证据后重新验收。token/call 限额不是货币硬上限。
- 继续使用原 provider key 是本次选择，不表示旧副本已失效；出现泄露证据另行处理凭据。
- 单次邀请码兼容绑定账号登录；多次共享码仅供注册，各自账号密码登录。定期停用旧批次并演练恢复。
