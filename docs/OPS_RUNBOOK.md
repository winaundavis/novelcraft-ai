# 云主机运维手册

更新：2026-10-24。记录当前**实际运行**的单实例 Beta 环境。与 `PUBLIC_BETA_DEPLOYMENT.md`（Nginx + systemd 参考契约）并存：那份是通用契约，这份是本次真实部署的落地形态。

## 1. 拓扑

```text
浏览器
  │ HTTPS (Let's Encrypt, 自动续期)
  ▼
Caddy :80/:443
  ├── /api/*  ──► 127.0.0.1:8000  novelcraft.service   (线上后端, 1 worker)
  ├── /assets/* ─► 静态文件（不存在直接 404）
  └── 其他     ──► SPA 回落 index.html（no-cache）

staging 实例（同一台机器）
  └── staging.novelcraft.example.com ─► 127.0.0.1:8001 novelcraft-staging.service
```

| 项 | 值 |
|---|---|
| 服务器 | `198.51.100.10`（腾讯云轻量·香港，Ubuntu 24.04.4 LTS，2 核 / 2 GB / 40 GB） |
| Python | 3.12.3（系统自带）。依赖锁 `requirements-production.lock.txt` 在该版本下**已跑通全部 1509+ 测试** |
| 线上地址 | `https://novelcraft.example.com` |
| 预发布地址 | `https://staging.novelcraft.example.com` |
| 后端监听 | **仅 127.0.0.1**，公网无法直连 8000/8001 |
| 服务账号 | `novelcraft`（系统账号，无登录 shell） |

> 为什么用 `sslip.io` 免费域名：不需要备案即可拿到可信 HTTPS。`nip.io` 在同一 IP 下 TLS 握手会被拦截，**不要改回去**。正式上线建议换自有域名。

## 2. 目录

| 路径 | 用途 |
|---|---|
| `/srv/novelcraft/releases/<release-id>/novelcraft-ai` | 每版独立源码 + 自有 venv |
| `/srv/novelcraft/current` | 指向当前线上版本的软链接 |
| `/srv/novelcraft/releases/staging/novelcraft-ai` | 预发布实例（前端静态 + 代码） |
| `/srv/novelcraft-private/data` | 线上 SQLite |
| `/srv/novelcraft-private/uploads` | 上传文件 |
| `/srv/novelcraft-private/staging-data` | 预发布库（线上库副本） |
| `/srv/novelcraft-private/backup-status.json` | 备份脚本写给后台展示的状态 |
| `/var/backups/novelcraft` | 备份归档，保留 14 份 |
| `/etc/novelcraft/backend.env` | 线上配置（`root:novelcraft 0640`） |
| `/etc/novelcraft/staging.env` | 预发布配置 |
| `/etc/novelcraft/master.key` | Fernet 主密钥，用于解密库内模型 API Key |

## 3. 服务

| unit | 说明 |
|---|---|
| `novelcraft.service` | 线上后端，`Restart=on-failure`，开机自启 |
| `novelcraft-staging.service` | 预发布后端（8001） |
| `caddy.service` | 反向代理 + 自动 HTTPS |
| `novelcraft-backup.timer` | 每日 03:30（服务器本地时间 CST），`Persistent=true`（关机错过会补跑） |

```bash
systemctl status novelcraft novelcraft-staging caddy novelcraft-backup.timer
journalctl -u novelcraft -n 100 --no-pager
systemctl list-timers novelcraft-backup.timer
```

## 4. 常用操作

### 4.1 只看代码变更、不动发布（改配置）

```bash
# 改完 /etc/novelcraft/backend.env 后
systemctl restart novelcraft
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: novelcraft.example.com' http://127.0.0.1:8000/api/ready
```

### 4.2 发布新版本（含 staging 预演）

1. 本地构建前端：`VITE_API_BASE_URL=/api VITE_BETA_AUTH_REQUIRED=true npm run build`
2. 打包 `backend/app backend/main.py backend/scripts backend/tests backend/requirements-*.txt frontend/dist`
   （**不要漏 `requirements-*.txt`**，曾因此中断部署）
3. 解包到新的 `releases/<新ID>/novelcraft-ai`，`python3 -m venv .venv` 后 `pip install -r requirements-production.lock.txt`
4. 先在 staging 验证（见 4.3），确认后在低峰切换：

```bash
OLD=/srv/novelcraft/releases/<旧ID>/novelcraft-ai
NEW=/srv/novelcraft/releases/<新ID>/novelcraft-ai
/usr/local/bin/novelcraft-backup.sh                 # 先备份，失败就停
ln -sfn "$NEW" /srv/novelcraft/current
systemctl restart novelcraft
# 30 秒内 /api/ready 必须 200，否则回滚
ln -sfn "$OLD" /srv/novelcraft/current && systemctl restart novelcraft
```

**切换前自检清单**（本轮踩过的坑）：

- [ ] 前端包哈希与源码一致（不要混用不同批次的构建产物）
- [ ] 新 `dist/assets` 里的文件名与 `index.html` 引用一致
- [ ] 新版本能 `import main`，`/api/admin/*` 等新路由已注册
- [ ] 数据库先备份

### 4.3 staging 更新

```bash
S=/srv/novelcraft/releases/staging/novelcraft-ai
rm -rf "$S/frontend/dist" && tar -xzf /root/xxx.tar.gz -C "$S"
systemctl restart novelcraft-staging
```

### 4.4 备份与恢复

```bash
/usr/local/bin/novelcraft-backup.sh          # 立即备份一次
ls -lh /var/backups/novelcraft/              # 查看归档
journalctl -u novelcraft-backup.service -n 20 --no-pager
```

恢复（会覆盖线上库，务必先停服）：

```bash
systemctl stop novelcraft
gunzip -c /var/backups/novelcraft/<文件>.db.gz > /tmp/r.db
/srv/novelcraft/current/backend/.venv/bin/python \
  /srv/novelcraft/current/backend/scripts/backup_sqlite.py restore \
  /srv/novelcraft-private/data/novelcraft.db /tmp/r.db --replace
chown novelcraft:novelcraft /srv/novelcraft-private/data/novelcraft.db
systemctl start novelcraft
```

恢复演练已做过一次：`integrity_check = ok`，外键检查无异常，账号/项目/章节/字数与线上一致。

> **主密钥不随备份保存**（安全设计）。`master.key` 丢失只需重新填一次模型 API Key，作品数据不受影响，但建议单独离线留存一份。

## 5. 关键配置项

| 变量 | 当前值 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `beta` | 触发生产就绪校验 |
| `DEBUG` | **必须 `false`** | `true` 时匿名会被当成 `local-dev`（默认管理员），等于公网开放管理员 |
| `CORS_ALLOW_ORIGINS` | `https://<域名>` | 写请求的 Origin 必须精确匹配 |
| `ALLOWED_HOSTS` | `<域名>` | 只写主机名 |
| `AUTH_COOKIE_SECURE` | `true` | 必须有 HTTPS |
| `APP_ENCRYPTION_KEY_FILE` | `/etc/novelcraft/master.key` | 解密库内 API Key |
| `ADMIN_USER_IDS` | 管理员账号 ID | 逗号分隔；不是用户名 |
| `PROJECT_DAILY_LLM_CALL_LIMIT` | `3000` | **成本护栏**（项目+UTC 日） |
| `PROJECT_DAILY_TOKEN_LIMIT` | `30000000` | 同上，测试期放宽值 |
| `RATE_LIMIT` | `120` | 每 IP 限流 |
| `USER_DAILY_CHAPTER_LIMIT` | `10` | 非管理员每日自动创作章数 |
| `USER_DAILY_CHAPTER_LIMIT_ENABLED` | `false` | **产品层额度开关**：测试期关闭，正式上线改 `true` 即可 |

> 两项限额关系：`PROJECT_DAILY_*` 是"钱"的护栏（防刷爆账单）；`USER_DAILY_CHAPTER_LIMIT` 是"产品额度"。前者放宽不代表后者失效。

## 6. Caddy 配置要点

- `/api/*` 必须 `flush_interval -1`：否则 SSE 流式正文会被缓冲、最后一次性吐出
- `/assets/*` **单独处理，缺失返回 404**，不得回落 `index.html`
  —— 否则浏览器缓存了旧首页时，会把 HTML 当 JS 执行，整页白屏
- 其余路径 `try_files {path} /index.html`，并带 `Cache-Control: no-cache`

配置改动后先 `caddy validate --config /etc/caddy/Caddyfile`，再 `systemctl reload caddy`。

## 7. 故障速查

| 现象 | 先查什么 |
|---|---|
| 页面白屏 | 浏览器缓存旧首页？换无痕窗口验证；`curl -I /` 看 `Cache-Control: no-cache`；确认 `index.html` 引用的 JS/CSS 逐个 200 |
| `/api/ready` 503 | 维护者 `GET /api/health/internal` 看错误码（cors/host/cookie/encryption_key/admin/database/storage） |
| 登录后什么都点不动 | 邀请码是否 `pending`、是否过期；`/api/auth/permissions` 返回什么 |
| AI 提示"未配置" | `/etc/novelcraft/backend.env` 无 provider key 时需在网页「模型设置」里填（加密存库） |
| 生成中途断掉 | 任务会进入可恢复状态（`recover_jobs_after_restart`），已完成章节不丢，用户点继续 |
| 证书异常 | `journalctl -u caddy -n 50`；确认 80/443 在云控制台防火墙放行 |

## 8. 访问凭据

| 项 | 位置 |
|---|---|
| SSH 私钥 | 本机 `<REPO_ROOT>\NovelCraft-public\.deploy-key\novelcraft_vm`（**不入库、不外发**） |
| 服务器登录 | `ssh -i <私钥> root@198.51.100.10`（密钥登录，密码登录已不再使用） |
| 主密钥 | `/etc/novelcraft/master.key` |
| 云控制台 | 腾讯云 → 轻量应用服务器；防火墙需放行 22/80/443 |

## 9. 已废弃的做法（留档，别再回去）

| 做法 | 为什么弃用 |
|---|---|
| 用本机 + 免费隧道（tunnelmole）对外 | 免费隧道 **12 小时**强制断线且每次换地址；本机必须常开 |
| `nip.io` 免费域名 | 同 IP 下 TLS 握手被拦截 |
| 直接 `python main.py` | 会监听 `0.0.0.0`；生产统一用 `uvicorn --host 127.0.0.1` + Caddy |
| 多 worker / 多副本 | SQLite + 进程内限流不支持，必须单实例 |
