# 当前状态

> **本页只写"现在是什么"。** 历史过程、阶段性测试数字、已作废的结论全部移入
> 当前状态存档（2026-10-18 及以前），不再维护。
>
> **最后核实时间：2026-10-24**。核实方式：登录生产主机实测 + 本机跑测试，不引用旧文档。
> 修改代码若影响本页描述，必须同步更新本页。

## 一、定位

单实例、邀请制受控 Beta。版本 `1.0.0`。中文小说创作工作台：
FastAPI 后端 + React/Vite 前端 + SQLite。

**不是**生产级多租户系统：单机单进程、单库，不支持水平扩展。

## 二、生产环境（实测）

| 项 | 实际值 |
|---|---|
| 生产地址 | `https://novelcraft.example.com` |
| 预发地址 | `https://staging.novelcraft.example.com` |
| 主机 | 腾讯云轻量（香港）`198.51.100.10`，2 核 / 1.9 GB / 40 GB（已用 6.6 GB，18%） |
| 系统 | Ubuntu 24.04.4 LTS，Python 3.12.3 |
| 进程 | `novelcraft.service`（8000，仅监听 127.0.0.1）、`novelcraft-staging.service`（8001）、`caddy.service` —— **三者均 active** |
| 当前发布 | `/srv/novelcraft/releases/20261024-2/novelcraft-ai`（`/srv/novelcraft/current` 软链） |
| 就绪探针 | `GET /api/ready` → `{"status":"ready","version":"1.0.0"}`；`/api/health` 生产与预发均 **200** |
| TLS | Let's Encrypt，`CN=novelcraft.example.com`，有效期至 **2027-01-22**，自动续期 |
| 备份 | 每日 03:30 CST 自动，下次 2026-10-25 03:34；最近一次 2026-10-24T10:43:34Z，`novelcraft-20261024-104334.db.gz`（85 KB），保留 4 / 上限 14 |
| 数据规模 | 生产库 `users=2`、`projects=1`、`chapters=3`、`invites=2` |

目录与发布回滚细节见 [云主机运维手册](OPS_RUNBOOK.md)。

## 三、关键开关（`/etc/novelcraft/backend.env` 实测）

| 变量 | 值 | 含义 |
|---|---|---|
| `DEBUG` | `false` | **生产必须为 false**；为 true 时匿名请求会被当作管理员 |
| `RATE_LIMIT` | `120` | 限流阈值 |
| `PROJECT_DAILY_LLM_CALL_LIMIT` | `3000` | 单项目每日 LLM 调用上限（成本护栏） |
| `PROJECT_DAILY_TOKEN_LIMIT` | `30000000` | 单项目每日 token 上限 |
| `USER_DAILY_CHAPTER_LIMIT` | `10` | 单个用户每日自动创作章数 |
| `USER_DAILY_CHAPTER_LIMIT_ENABLED` | **`false`** | 章数额度**当前关闭**（测试期）；改 `true` 即生效，无需改代码 |
| `ADMIN_USER_IDS` | `<ADMIN_USER_ID>` | 管理员白名单 |

两套额度互相独立：项目级管**成本**，用户级管**公平性**。

## 四、权限模型（已验证）

| 角色 | 能看到的入口 | `/api/admin/*` |
|---|---|---|
| 未登录 | 登录 / 注册 | 401 |
| 普通用户 | 正常前端页面（**不渲染**运营后台入口） | 403 |
| 管理员（`ADMIN_USER_IDS`） | 全部 + 侧边栏「运营后台」 | 200 |

`DEBUG=true`（仅本地）下匿名请求身份为 `local-dev`，属管理员——**这是既有设计**，
已由测试 `test_debug_mode_anonymous_is_admin_by_design` 钉死。生产 `DEBUG=false`。

路由级口径见 [API 权限矩阵](api-permission-matrix.md)。运营后台能力见
[运营后台](ADMIN_CONSOLE.md)。

## 五、验证基线（2026-10-24 本机实测）

| 项 | 结果 |
|---|---|
| 后端全量 | **1517 passed / 11 skipped / 0 failed**（217.59s）。跳过的 11 项断言提示词/策略实现，该实现未随本仓库开源，清单见 `backend/tests/conftest.py` |
| 前端 `typecheck` | 0 错误 |
| 前端 `eslint` | 0 error / 9 warning（`exhaustive-deps` 一类） |
| 前端单测 | 107 passed / 0 failed |
| 真实模型调用 | 本轮 **0 次**（所有验证均离线） |
| Playwright E2E | 本轮**未重跑**，最近一次记录见存档 |

**环境说明（已知漂移）**：共用 venv `<REPO_ROOT>\.venv` 原先缺 `cryptography`
（本次已补装 `cryptography==50.0.1`），但 `fastapi` 实装 `0.111.0`，
而 `requirements.txt` 声明 `0.141.1`——**本地 venv 与声明依赖不一致**，重建 venv 后需复验。

## 六、能力边界

**已具备**：邀请码注册与绑定、项目与章节管理、自动创作（含章数额度）、大纲（分卷/卷级信号/
章节名一致性）、审校与批量修改、去 AI 味、Word/Markdown 导入、人物与世界规则、章节回收站、
运营后台（概览/用户/用量 Top10/邀请码批量生成与停用）。

**明确没有**：多实例与水平扩展、数据库迁移到 PostgreSQL、支付与订阅、自助找回密码流程的
完整验证、任何 SLA 承诺。

## 七、已知风险

| 优先级 | 风险 | 现状 |
|---|---|---|
| **P0** | 仓库**无远程**，只有本地 Git，无异地备份 | 待接入私有 remote |
| **P0** | 单实例 + SQLite 的容量天花板 | 用户量上升前需评估迁移 |
| **P1** | 本地 venv 与 `requirements.txt` 漂移 | 会削弱"本地验证=可发布"的可信度 |
| **P1** | 质量依赖事后审校，缺事前约束 | 建议建质量基线集 |
| **P2** | 预发环境常驻占用资源 | 明确保留或关停 |
| **P2** | 本页之外仍有少量过期文档 | `docs/README.md` 已按类分区，历史文档不作为当前依据 |

## 八、入口索引

- **运维 / 发布 / 回滚 / 备份恢复** → [OPS_RUNBOOK.md](OPS_RUNBOOK.md)
- **运营后台怎么用、统计口径、隐私边界** → [ADMIN_CONSOLE.md](ADMIN_CONSOLE.md)
- **普通用户怎么用** → [user-guide.md](user-guide.md)
- **权限口径** → [api-permission-matrix.md](api-permission-matrix.md)
- **测试隔离与 live 边界** → [test-suite-runtime.md](test-suite-runtime.md)
- **复盘（难点与弯路）** → [RETROSPECTIVE.md](RETROSPECTIVE.md)
- **历史状态（冻结）** → current-status-archive.md
