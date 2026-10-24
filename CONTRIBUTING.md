# 贡献指南

感谢你有兴趣改进 NovelCraft AI。这个仓库是**部分开源（open-core）**版本，
请先读 [README 的「开源范围」](README.md#开源范围) 了解哪些实现不在此仓库中。

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | **3.11** | 后端依赖锁定在 `backend/requirements-production.lock.txt` |
| Node.js | **≥ 22** | `npm run test:unit` 使用 node 自带 glob，Node 20 及以下不展开通配符 |
| npm | 随 Node | 使用 `npm ci`，不要改锁文件里的无关条目 |

## 本地启动

```bash
# 后端
cd backend
python -m venv .venv && . .venv/Scripts/activate   # Windows
# 或：python -m venv .venv && source .venv/bin/activate   # Linux / macOS
pip install -r requirements-dev.txt
cp .env.example .env      # 仅在 .env 不存在时；不要提交 .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000

# 前端（另开终端）
cd frontend
npm ci
npm run dev               # http://127.0.0.1:5173，已配置 /api 代理到 8000
```

本地开发请保持 `ENVIRONMENT=development`、`DEBUG=true`。**`DEBUG=true` 会让匿名请求
被视为本地管理员身份，禁止用于任何可公网访问的环境**，详见 [SECURITY.md](SECURITY.md)。

## 提交前必须跑的命令

CI 会跑同样的内容，本地先过一遍能省一轮往返：

```bash
# 后端
cd backend
python -m flake8 app main.py tests
python -m pytest tests -q

# 前端
cd frontend
npm run typecheck
npm run lint
npm run test:unit
npm run build
```

当前基线：后端 **1517 passed / 11 skipped**，前端 **107 passed**，`eslint` **0 error**。

> 11 个 skipped 用例断言的是提示词/策略实现，该实现未随本仓库开源，
> 清单见 `backend/tests/conftest.py`。它们保留下来是作为**契约**——
> 你若自行实现提示词策略，可以去掉 skip 并让它们通过。

## 代码结构

```text
backend/app/api/         FastAPI 路由（鉴权、限流、参数校验）
backend/app/services/    业务服务（含生成编排结构；策略实现为占位）
backend/app/dao/         SQLite 访问层
backend/app/models/      Pydantic 模型与表结构
backend/app/llm/         provider 抽象、重试、调用审计、角色路由
backend/tests/           离线测试（默认零真实模型调用）
frontend/src/            React + TypeScript 工作台
frontend/tests/          纯函数单测与 Playwright E2E
```

## 提交约定

- **一个提交做一件事**，信息用「类型: 说明」，类型取 `feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `ci`。中文可用。
- **新增行为必须带测试**。修 bug 时优先写一个能复现的失败用例，再修。
- **不要提交生成物**：`node_modules/`、`dist/`、`.venv/`、`*.db`、日志、测试产物都已在 `.gitignore` 中，不要用 `git add -f` 绕过。
- **不要提交真实密钥、部署地址、账号或用户作品内容**。示例数据请自行合成。

## 提 PR

1. 从 `main` 切分支，命名如 `fix/outline-truncation`。
2. 确认上面的命令全绿。
3. PR 描述里写清：**问题是什么 / 怎么改的 / 怎么验证的**。涉及生成行为的改动，请附上改动前后的实际输出对比。
4. 涉及鉴权、额度、数据删除的改动，请额外说明影响面。

## 关于提示词与质量策略

本仓库刻意不含提示词正文与去 AI 味/审校的具体算法（原因见 README「开源范围」）。
因此：

- **欢迎**提交与策略无关的改进：接口、并发、错误处理、前端交互、测试、文档。
- 若你想自己实现策略，`backend/tests/` 中被 skip 的用例就是验收标准，可以直接参考。
- 不要在 PR 中提交你从线上版本逆向得到的提示词。

## 报告问题

安全问题请勿走公开 Issue，见 [SECURITY.md](SECURITY.md)。
功能问题请提供：复现步骤、期望行为、实际行为、以及相关日志（**先脱敏**）。
