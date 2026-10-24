# 测试、隔离和证据范围
更新：2026-10-12。默认 0 次真实 LLM，不依赖真实小说或用户账号。

## 后端默认入口
在 backend/ 执行：
```powershell
$env:RUN_LIVE_LLM = '0'
$env:PYTHONUTF8 = '1'
python -m pytest tests -q
```
Windows 也可使用 `py -3 -m pytest`。backend/tests/conftest.py 在 pytest_configure 创建临时 DB 并设置 NOVELCRAFT_DB_PATH；autouse guard 在 RUN_LIVE_LLM!=1 时阻断 provider 的 httpx.AsyncClient。多数旧测试默认 DEBUG=true，权限／production 测试显式切回 false。因此“全量通过”不自动等于每个测试都在生产配置下运行。

该 guard 不是系统级断网：测试需明确 mock 新增网络客户端，不能绕到 requests、其他 HTTP 客户端或独立脚本真实调用。导入 app 之前必须设置测试路径；若 app/settings/db 已加载，仅之后设置环境变量未必会重建已缓存的路径。

### 隔离加固（T0，2026-09-13）
`backend/tests/isolation.py` 提供唯一隔离入口：`activate_temp_environment()` 建立临时根目录并设置 `DATA_DIR` / `NOVELCRAFT_DB_PATH` / `UPLOAD_DIR`（`RUN_LIVE_LLM` 默认 0），`assert_app_isolated()` 在导入 app 后校验 `app.db.DB_PATH`、`app.llm.audit.DB_PATH`、`settings.data_dir`、`settings.upload_dir` 全部落在临时根目录内。

- `conftest.py::pytest_configure` 现在会在 `db.init_db()` 之后立即调用 `assert_app_isolated()`：若 `backend/.env` 的真实 `NOVELCRAFT_DB_PATH=../data/beta-user-001.db` 生效，测试会**立刻失败**而不是静默写真实库。
- 回归用例 `backend/tests/test_test_isolation.py` 覆盖：环境变量声明路径、app 解析后路径、`.env` 真实路径不泄漏、guard 拒收临时目录外路径。
- 默认套件通过 `live_llm_call_spy` fixture 暴露被拦截的真实 provider HTTP 调用计数（默认 0），`_BlockedAsyncClient` 同时计数并抛 `NetworkError`。
- 裸 `TestClient` 脚本必须在导入 app 前调用 `isolation.activate_temp_environment()`，导入后调用 `isolation.assert_app_isolated()`；不得复制真实库作 fixture。

安全提醒：不要打印 `.env`、API key、password_hash、session token 或完整用户作品；审计输出只保留安全 ID / 计数 / 状态 / 哈希。

## 审计临时脚本
本轮发生过裸 TestClient 读取 backend/.env、在真实 Beta DB 写入空项目的事故。后续必须：
1. 在新进程导入 app 前设置临时 DATA_DIR、NOVELCRAFT_DB_PATH、UPLOAD_DIR 和 RUN_LIVE_LLM=0。
2. 核对解析后的 DB 路径位于该临时目录；不满足立即终止。
3. Mock provider／拦截所有非本地 HTTP；无匹配请求使测试失败。
4. 使用虚构账户／小说，关闭 lifespan 和连接后再处理临时输出。不要清理生产行来掩盖测试事故。

已有 .env 中的真实凭据不要打印、复制进报告或 test fixture。

## 前端
在 frontend/ 执行：
```powershell
npm ci
npm run typecheck
npm run build
npm run test:unit        # Node 内置 runner，跑 tests/*.test.mjs（当前 90 条）
npm run test:normalize   # 只跑 normalize.test.mjs（test:unit 已包含它）
```
npm ci 重新安装依赖；运行中 Vite/esbuild 可能占用文件，全新安装验证用独立检出，不强删锁文件。构建的 >500 kB chunk warning 单独记录，不算编译失败。

拦截 API 的现有子集（先启动前端）：
```powershell
$env:RUN_LIVE_LLM = '0'
npm run test:e2e -- tests/e2e/auth-session.spec.ts tests/e2e/copilot-accept.spec.ts tests/e2e/chat-conversations.spec.ts tests/e2e/chapter-trash.spec.ts tests/e2e/review-panel.spec.ts tests/e2e/character-profile.spec.ts tests/e2e/markdown-upload-import.spec.ts --reporter=line --output=../test-results-offline --workers=1
```
这些 mock 流程用于交互和同步验证；其他 E2E／scripts 默认行为须逐项查看。不要假设 npm run test:e2e 全部不写真实后端，也不要把 RUN_LIVE_LLM 环境变量当所有浏览器代码自动遵守的授权开关。

真实后端子集（会写数据，默认跳过）：

`tests/e2e/novelcraft-smoke.spec.ts` 与 `tests/e2e/projection-panel.spec.ts` 直接调用真实后端（后者还用 `node:sqlite` 直接写 `memory_projections`），需要一个有效邀请码身份。设置后 `playwright.config.ts` 会用 `extraHTTPHeaders` 为所有请求（含 `request` fixture）附带 `X-NovelCraft-Invite`：

```powershell
$env:NOVELCRAFT_E2E_INVITE = '<后端 INVITE_TOKENS 中的邀请码>'
$env:NOVELCRAFT_API_BASE = 'http://127.0.0.1:8000'   # 可选，默认即此
npx playwright test tests/e2e/novelcraft-smoke.spec.ts tests/e2e/projection-panel.spec.ts
```

未设置 `NOVELCRAFT_E2E_INVITE` 时这两个文件整体 skip（不是失败）。它们会创建并删除真实项目、直接改真实库，**建议指向一次性后端与独立数据目录**，不要对着 Beta 数据跑。注意 `/api/auth/me` 只认账户 session，不认邀请码，不能用它探测邀请码身份。

## 全量套件当前基线（2026-10-12）

这是**当前代码**上的最近一次全量结果，用来与新改动对比；它是"现状"，不是下方历史表里逐轮数字的累加。

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `python -m pytest tests -q --basetemp=<非 C 盘临时目录>` | **1265 passed / 0 failed**（约 300s） |
| 前端单测 | `npm run test:unit` | **90 passed / 0 failed** |
| 前端 E2E（全量） | `node node_modules/@playwright/test/cli.js test --workers=1` | **145 passed / 34 skipped / 0 failed** |

三个必须记住的前提：

- **`--basetemp` 指向非 C 盘目录**：C 盘空间紧张时 SQLite 会报 `database or disk is full`。
- **跑 E2E 前先重启开发服务器**：改过 `src/`（尤其 `stores/`、`App.tsx`）后 HMR 会让
  `page.evaluate(import('/src/...'))` 拿到另一个模块实例，产生与代码无关的假失败。
  已实测一次（`style-transition.spec.ts` 两条"迟到响应"用例，重启后该文件 28/28 通过）。
- 全量 E2E 的 34 条 skipped 来自 `novelcraft-smoke.spec.ts` 与 `projection-panel.spec.ts`
  整体被 `NOVELCRAFT_E2E_INVITE` 跳过。**不要把守卫放进这类文件**——它永远不会跑，
  而 `EXIT=0` 可能只是 `skipped`。

## 历史结果与本次验证
| 范围 | 已记录结果 | 限制 |
|---|---|---|
| 近期正文同步修复的后端全量 | 933 passed，1 warning，154.87s | 当时已有环境，不是本次重跑 |
| 同次前端子集 | 22 mocked E2E passed；typecheck/build PASS | 不证明真实模型遵循剧情弧 |
| 随后账户／运行定向审计 | 23 passed | 指定 4 个测试文件 |
| 随后内容流浏览器审计 | 4 条 mocked 流程 PASS；unexpected requests=0；page errors=0 | 与上述 22 项不合并成全功能结论 |
| 随后 Arts 验证 | 未执行，calls=0 | harness 未通过调用前审查 |
| 角色档案改造（2026-09-16） | 后端 1029 passed；离线 E2E 52 passed / 34 skipped / 0 failed；`test:normalize` 29/29；`profile-falsify` 12/12；typecheck/build PASS | 真实后端 E2E 默认跳过；未跑真实模型 |
| 角色档案反向验证 | 故意让分析路径写草稿 → 源码守卫失败 2 项、浏览器行为测试失败 1 项，已还原 | 只证明守卫有效，不证明真实模型质量 |
| Markdown 导入通道（2026-09-17） | 后端导入专项 16 passed；新增离线 spec `markdown-upload-import` 6 passed | 离线 fixture，不覆盖真实目录差异 |
| 导入通道 + 角色档案合并后（2026-09-17） | 后端全量 **1045 passed**（1 warning，168.08s）；离线 E2E **58 passed / 34 skipped / 0 failed** | 此轮之后再未重跑全量 |
| 白屏兜底与导入超时（2026-09-18 / 09-21） | `test:normalize` 31/31；导入 spec 7 passed；`tsc --noEmit` PASS | 白屏根因未复现，只加了兜底 |
| 导入舍弃误识别条目（2026-09-21） | 后端导入专项 **23 passed**；导入 spec 7 passed | 四类按值过滤、只删不补 |
| 参考书档案 + 素材库（2026-10-06） | 后端 **1202 passed**；E2E **124 passed / 34 skipped / 0 failed** | 各自范围，不与上方相加 |
| 账号注册 · 邀请码绑定（2026-10-07） | 后端全量 **1257 passed**；E2E **135 passed / 34 skipped / 0 failed**；单测 86 | 真实库迁移已执行并留备份 |
| 无障碍 / 回车提交 / 焦点修复（2026-10-09 ~ 10-10） | E2E **137 → 139 passed**；单测 86 | 「每打一个字丢焦点」是回归，已用 `page.keyboard.type()` 逐字输入钉住 |
| hook 依赖守卫 + 手机端 tab 菜单（2026-10-10） | E2E **141 passed / 34 skipped / 0 failed**；单测 **90** | 静态守卫，属形态检查而非行为测试 |
| 模型设置仅管理员可见（2026-10-10） | 后端 **1265 passed**；E2E **145 passed / 34 skipped / 0 failed** | 与上方当前基线同一轮；反向验证 6/6、真实后端 6/6 |

定向后端命令：
```powershell
python -m pytest tests/test_auth_accounts.py tests/test_phase10c_ownership.py tests/test_phase10d_production.py tests/test_phase11a_operational.py -q
```
表内各行来自不同时间的不同范围，不能相加成"全功能通过"；实际检查另见清理记录与执行报告。

## Live 门
真实调用必须具备明确当次授权、独立数据目录、最终 provider/model/config source、输入输出上限、预算、审计 run_id 和失败停止策略。默认 retry=0；失败也可能计费；无账单 actual_cost=null。现有 pytest guard 只有设置 RUN_LIVE_LLM=1 才会放开，但设置该变量本身不是用户授权。

合成 Memory/FTS 测试反映特定容量／召回样例，不能证明真实长篇质量。浏览器 mock 证明显示／状态链，不证明 provider 返回格式、真实模型遵循大纲或真实成本。
