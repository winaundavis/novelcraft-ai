# 文档索引

本目录只保留**当前有效**的说明与清单。阶段性验收报告、一次性审计与内部工作清单
不随本仓库发布；如需追溯，请参考 `CHANGELOG.md`。

## 一、当前说明

这些文档写错就是缺陷。改代码时如果碰到它们描述的行为，必须同步更新。

| 文档 | 内容 |
|---|---|
| [当前状态](current-status.md) | 定位、验证基线、开关、能力边界与已知风险的**汇总入口** |
| [使用指南](user-guide.md) | 账户、项目、章节、人物、导入、注册与邀请码、模型设置（仅管理员） |
| [API 权限矩阵](api-permission-matrix.md) / [Beta 权限矩阵](beta-permission-matrix.md) | 路由级鉴权口径 |
| [生产启动](production-startup.md) | 环境变量、启动、首个管理员、反向代理与备份 |
| [运维手册](OPS_RUNBOOK.md) | 拓扑、目录、发布与回滚、备份恢复、故障速查 |
| [运营后台](ADMIN_CONSOLE.md) | 管理端能力、三层权限、统计口径、隐私边界 |
| [公网部署手册](PUBLIC_BETA_DEPLOYMENT.md) / [上线门禁清单](PUBLIC_BETA_GO_LIVE_CHECKLIST.md) | 部署、验收与人工待办 |
| [Beta 环境](beta-environment.md) | 单实例限制与运行条件 |
| [测试与隔离](test-suite-runtime.md) | 零 API 默认测试、live 边界、全量套件基线 |
| [脚本目录](script-catalog.md) | 脚本用途、风险、是否可默认运行 |
| [项目对话记录](project-chat-history.md) | 会话持久化、归档与删除 |
| [章节回收站](chapter-recycle-bin.md) | 正文恢复与永久删除、批量还原 |
| [大纲助手](outline-agent-usage.md) | 候选、确认、失败诊断与安全边界 |
| [Canonical State Model](canonical-state-model.md) | 事实链与状态边界的底层约定 |

## 二、规格与验收口径

| 文档 | 内容 |
|---|---|
| [注册与邀请码 PRD](prd-registration-and-invites.md) | 注册／邀请码绑定的决策与验收口径 |

## 三、清单（每次发布重新核验）

- [Beta 发布清单](beta-release-checklist.md)
- [Beta 部署清单](beta-deployment-checklist.md)
- [Beta 浏览器 Smoke 清单](beta-browser-smoke-checklist.md)

## 四、复盘

- [项目复盘](RETROSPECTIVE.md)：实现难点、走过的弯路、克服方式与残余风险。
  **一次性冻结**，不随代码更新。

## 五、关于本仓库的删减

本仓库为部分开源版本，以下内容**未包含**：

- 生成策略实现：提示词正文（以占位常量代替）、去 AI 味与审校的具体算法
- 阶段性验收报告、内部审计、工作清单与历史状态存档
- 任何真实部署地址、账号、管理员标识与 API Key
