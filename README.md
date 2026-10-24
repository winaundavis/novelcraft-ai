# NovelCraft AI

[![CI](https://github.com/winaundavis/novelcraft-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/winaundavis/novelcraft-ai/actions/workflows/ci.yml)

中文小说创作工作台：用项目组织故事、大纲、人物、世界规则与正文，通过项目助手提出修改，再由作者确认候选。

当前定位为**单实例邀请制 Beta**，应用版本 `1.0.0`。本仓库是**部分开源版本**：保留完整的应用结构、数据模型、接口与测试，但**生成策略类实现以占位形式提供**（提示词正文、去 AI 味与审校的具体算法）。范围与限制见文末「开源范围」。操作见[使用指南](docs/user-guide.md)，部署/恢复/回滚见[部署手册](docs/PUBLIC_BETA_DEPLOYMENT.md)，放行依据见[上线门禁](docs/PUBLIC_BETA_GO_LIVE_CHECKLIST.md)。

## 功能与边界

| 模块 | 当前行为 |
|---|---|
| 账户与项目 | 账号密码登录、邀请注册、退出、导出自身备份。普通账号按 owner 隔离；模型设置仅管理员可见且接口受保护。名下仍有受保护数据时拒绝注销。 |
| 大纲与正文 | 总纲/卷/剧情弧/章节导航，Markdown 编辑/预览/保存/续写/按大纲创作；大纲和 Copilot 修改先出候选再确认，过期候选不强制覆盖。 |
| 助手与回收站 | SQLite 持久化多会话，新建/归档/恢复/删除/手动重生成；章节单条/批量还原，冲突跳过不覆盖。 |
| 故事资料 | 人物、关系网络、世界规则、伏笔、Story State、章节合同、记忆与检索。 |
| 文风与审校 | 预设/画像/渐进过渡、审校、参考分析、全文问答；AI 操作可能消耗额度。过渡进度是章节计数，质量由作者判断。 |
| 导入与运维 | Markdown 导入、Word 建议书、任务进度/恢复、健康检查、模型审计、SQLite 备份/恢复、发布包审查。 |

不提供多实例高可用、复杂团队权限、支付/订阅或自动计费。合成测试不能证明长篇连续创作质量，功能存在不等于逐项通过真实模型验收。

## 本地启动

需要 Python 3.11、兼容前端锁文件的 Node.js/npm。后端生产依赖使用 `backend/requirements-production.lock.txt`，测试依赖使用 `backend/requirements-dev.txt`（包含生产锁）；前端使用 `frontend/package-lock.json`。各终端从项目根目录出发，固定使用 `127.0.0.1`，避免与 localhost 混用 Cookie/浏览器存储。

后端终端（PowerShell）：

~~~powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-production.lock.txt
python -m pip check
~~~

仅在 `backend/.env` 不存在时复制 `backend/.env.example` 并编辑，不覆盖私有配置。隔离本地开发使用 `ENVIRONMENT=development`、`DEBUG=true`，该模式允许本地身份，不能用于公网。Beta 用根目录 `.env.production.example`，具体配置/管理员准备见[部署手册](docs/PUBLIC_BETA_DEPLOYMENT.md)。

~~~powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
~~~

后端改代码须重启，开发时可给上述命令显式添加 `--reload`；生产用部署手册的监管服务。仓库没有 `python -m app` 入口。

前端终端：

~~~powershell
cd frontend
npm ci
$env:NOVELCRAFT_PROXY_TARGET = 'http://127.0.0.1:8000'
npm run dev -- --host 127.0.0.1
~~~

打开 `http://127.0.0.1:5173/`。开发 `/api` 经 Vite 代理；生产用静态构建与 HTTPS 代理。外部 `VITE_API_BASE_URL` 含 /api 前缀。前端只用 npm，避免混用包管理器；Vite 环境改变后重启开发服务或重建静态产物。

~~~powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
~~~

health 不调用模型，ready 另查配置/数据库/存储。基础编辑和存储不依赖 provider 凭据，真实 AI 操作需要有效配置。相对数据路径按进程工作目录解析，部署用绝对私有路径。用所属终端 Ctrl+C 停止服务并等待任务结束，不按端口批量终止不明进程。

## 账户、凭据与费用

Beta 注册始终需服务端认可的邀请码，`INVITE_TOKENS` 为空不会开放注册。数据库码有失效时间/使用配额；共享多次码只供注册，各自账号密码登录。管理员 ID/数据库标记决定模型设置权限，`GET /api/auth/permissions` 可核对。

密码为加盐哈希；会话 Cookie 为 HttpOnly、有效期 14 天，Beta 必须配置 Secure。SQLite 保存作品、对话、候选等，浏览器状态不是服务器备份。名下有受保护数据时拒绝注销，先导出并处理内容。

代码默认 `deepseek / deepseek-v4-flash`，最终路由由环境默认、数据库激活配置、角色覆盖、显式参数共同决定，不能只看环境。选项列出的模型不保证所有网关支持。Planning 默认保留 thinking，Writing/Copilot 默认关闭；max_tokens=4096 不等于 4096 汉字。

模型设置为 server-global。provider key 通过私有环境/加密数据库保存；独立主密钥经 `APP_ENCRYPTION_KEY_FILE` 私有文本文件或 `APP_ENCRYPTION_KEY` 私有环境注入，并单独备份；同时配置时必须相同。历史明文迁移见[部署手册](docs/PUBLIC_BETA_DEPLOYMENT.md)。本次继续原 provider key；本地加密不能使历史副本失效。公开 ZIP 不含真实环境、数据库、作品或密钥。

token/call 预算不是货币硬上限；Arts 和官方 DeepSeek 价格不能混用，无账单只标“估算”，失败请求也可能扣费。真实测试需限定账号、次数、数据、预算，一次授权不是持续生成/重试授权。

## 测试

默认测试不调用真实模型。后端 pytest 使用临时 SQLite 并拦截真实模型 HTTP：

~~~powershell
cd backend
python -m pip install -r requirements-dev.txt
$env:RUN_LIVE_LLM = '0'
python -m pytest tests -q
~~~

前端从另一终端执行：

~~~powershell
cd frontend
npm ci
npm run typecheck
npm run test:unit
npm run build
~~~

test:unit 已含 normalize 用例，不重复相加。浏览器测试必须用隔离后端/临时数据或完整 API mock。先启动前端，可执行完全 mock API 的子集：

~~~powershell
cd frontend
$env:RUN_LIVE_LLM = '0'
npm run test:e2e -- tests/e2e/copilot-accept.spec.ts tests/e2e/chat-conversations.spec.ts tests/e2e/chapter-trash.spec.ts --reporter=line --output=../test-results-offline --workers=1
~~~

`RUN_LIVE_LLM=0` 不替代数据隔离/网络拦截，旧巡检或其他 E2E 可能写运行库。独立安装副本可避免 Vite/esbuild 锁住 Windows 依赖。本轮已执行结果只引用验收报告，不沿用历史数字。

## 发布与目录

从项目根运行 `backend/scripts/package_source.py --source . --output <新ZIP>`，再用 `backend/scripts/release_audit.py <ZIP> --json-out <报告>`；完整命令/哈希/证据见[上线门禁](docs/PUBLIC_BETA_GO_LIVE_CHECKLIST.md)。包内 `release-build.json` 记录实际 commit、dirty 与逐文件哈希；源码包不是数据库备份。

~~~text
backend/app/          FastAPI、DAO、LLM 路由、创作/记忆服务
backend/tests/        离线单元/集成测试
backend/scripts/      打包、审查、邀请码、备份/迁移
frontend/src/         React + TypeScript 工作台
frontend/tests/       规范化和浏览器回归
test-fixtures/       导入样例
docs/                公开使用、部署、门禁和验收文档
~~~

前端为 React、TypeScript、Vite、Tailwind CSS、Zustand，正文当前为 Markdown textarea；后端为 FastAPI、Pydantic、httpx、SQLite/FTS5、python-docx。精确依赖见版本文件。内部历史报告、项目记忆和智能体工作流留在维护者仓库，不随公开候选交付。

## 开源范围

本仓库是**部分开源（open-core）版本**：应用结构、数据模型、路由、鉴权、DAO、
前端界面与测试是完整的；**决定产出质量的策略实现不在其中**。

| 内容 | 状态 |
|---|---|
| 前端界面与交互 | ✅ 完整 |
| 后端 API、DAO、数据模型、鉴权 | ✅ 完整 |
| 部署与运维脚本、备份恢复、发布审计 | ✅ 完整 |
| 测试体系（离线、零真实调用） | ✅ 完整 |
| 生成编排的结构（角色路由、预算、重试、截断判定） | ✅ 保留结构 |
| **提示词正文**（`*_PROMPT` 常量） | ⚠️ 以占位常量提供 |
| **去 AI 味、审校、世界规则的具体算法** | ⚠️ 以简化实现提供 |
| 真实部署地址、账号、管理员标识、API Key | ❌ 已移除 |
| 阶段性验收报告与内部审计 | ❌ 未包含 |

因此：**本仓库可以运行、可以部署、可以改，但开箱产出的文本质量不代表线上版本。**
若你希望复现线上质量，需要自行编写提示词与质量策略——这也是本项目的核心工作量所在。

## 许可

MIT，详见 [LICENSE](LICENSE)。安全相关请先读 [SECURITY.md](SECURITY.md)，
特别是 `DEBUG=true` 仅限本机开发这一条。
