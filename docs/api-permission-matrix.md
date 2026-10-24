# API Permission Matrix (Phase 10-C)

历史审计范围：`backend/app/api/*.py`。Phase 10-C记录开发86条、生产85条，仅表示当时路由；后来新增会话/回收站/账户，不能继续复用这个数量。本轮文档更新不导入真实环境重新枚举路由或宣称完整权限验收。2026-09-13补充账户入口如下，既有路由表保留历史范围。

当前约定：通用 `auth` 支持有效账户session及旧invite identity；账户/me和注销只认session。`owner` 为 `require_project_owner`（保持管理员例外）；`maintainer` 为 `require_maintainer`。DEBUG开发回退不适用于Beta。`R/W/LLM` 分别表示读取项目数据、写入数据、可能调用LLM。新增的 `GET /api/auth/permissions` 供前端判断「模型设置」入口是否显示，它与 `require_maintainer` 共用 `current_user_id` + `is_admin_user`，所以「菜单显示什么」与「接口放行什么」同源。退出/注销数据政策与run隔离已知问题见工作清单。

## 账户与健康增量（按当前源码核对）

| Method | Path | 谁可调用 | 数据/副作用 | LLM |
|---|---|---|---|---|
| POST | `/api/auth/register` | 公开；INVITE_TOKENS非空时要求有效invite | 创建自身账户；非管理员 | no |
| POST | `/api/auth/login` | 公开；校验账户密码 | 创建会话与HttpOnly Cookie | no |
| POST | `/api/auth/logout` | 可无身份调用，删除请求自身Cookie对应session | 撤销自身会话，不是按任意user ID操作 | no |
| GET | `/api/auth/me` | 有效账户session，缺失401 | 只返回当前账户安全字段 | no |
| GET | `/api/auth/permissions` | 有效账户session**或**旧invite header，都缺失才401 | 只返回`userId`与`isAdmin`；判定与`require_maintainer`同源，供前端决定是否渲染管理入口 | no |
| GET | `/api/auth/invites/mine` | 有效账户session，缺失401 | 只返回当前账号已绑定邀请码的hint／备注／绑定时间，不回显明文 | no |
| GET | `/api/auth/username-available` | 公开 | 只返回用户名可用性与原因，不创建任何数据 | no |
| DELETE | `/api/auth/account` | 有效账户session，缺失401 | 删除自身账号/session；当前作品留存政策待修复 | no |
| GET | `/api/health/internal` | maintainer | 内部就绪元数据 | no |

公开健康 `/api/health`、注册、登录和自身登出是明确的公共入口；不代表项目资源开放。

## 历史项目资源矩阵

| Method | Path | Auth | Ownership / Admin | R | W | LLM | Risk |
|---|---|---|---|---|---|---|---|
| GET | `/api/health` | no | public health only | no | no | no | low |
| GET | `/api/projects/{project_id}/chapter-trash` | auth | owner | yes | no | no | high |
| GET | `/api/projects/{project_id}/chapter-trash/{trash_id}` | auth | owner + trash/project lookup | yes | no | no | high |
| POST | `/api/projects/{project_id}/chapter-trash/{trash_id}/restore` | auth | owner + trash/project + outline target lookup | yes | yes | no | critical |
| DELETE | `/api/projects/{project_id}/chapter-trash/{trash_id}` | auth | owner + trash/project lookup | yes | yes | no | critical |
| GET | `/api/projects/{project_id}/chat-conversations` | auth | owner | yes | no | no | medium |
| POST | `/api/projects/{project_id}/chat-conversations` | auth | owner | no | yes | no | medium |
| GET | `/api/projects/{project_id}/chat-conversations/{conversation_id}` | auth | owner + conversation/project lookup | yes | no | no | high |
| PATCH | `/api/projects/{project_id}/chat-conversations/{conversation_id}` | auth | owner + conversation/project lookup | yes | yes | no | high |
| DELETE | `/api/projects/{project_id}/chat-conversations/{conversation_id}` | auth | owner + conversation/project lookup | yes | yes | no | high |
| GET | `/api/projects/` | auth | user sees own list | yes | no | no | medium |
| POST | `/api/projects/` | auth | created project binds caller | no | yes | no | high |
| GET/PATCH/DELETE | `/api/projects/{project_id}` | auth | owner | yes | yes for PATCH/DELETE | no | critical |
| POST | `/api/projects/{project_id}/analyze-characters` | auth | owner | yes | no | yes | critical |
| POST | `/api/projects/{project_id}/analyze-character-profile` | auth | owner + character-name lookup | yes | no | yes | critical |
| GET/PATCH | `/api/projects/{project_id}/conflicts[/{fact_id}]` | auth | owner | yes | PATCH yes | no | high |
| GET/POST | `/api/projects/{project_id}/chapters/` | auth | owner | yes | POST yes | no | critical |
| GET/PUT/DELETE | `/api/projects/{project_id}/chapters/{chapter_id}` | auth | owner + chapter/project DAO lookup | yes | PUT/DELETE yes | no | critical |
| GET | `/api/projects/{project_id}/chapters/{chapter_id}/projections` | auth | owner + chapter/project lookup | yes | no | no | high |
| GET | `/api/projects/{project_id}/chapters/{chapter_id}/canonical-commit` | auth | owner + chapter/project lookup | yes | no | no | high |
| GET | `/api/projects/{project_id}/chapters/{chapter_id}/digest` | auth | owner + chapter/project lookup | yes | no | no | high |
| POST | `/api/projects/{project_id}/chapters/replay` | auth | owner | yes | yes (projection) | no | critical |
| POST | `/api/projects/{project_id}/chapters/{chapter_id}/projections/{projection_type}/replay` | auth | owner + chapter/project lookup | yes | yes (projection) | no | critical |
| GET/POST | `/api/projects/{project_id}/chapters/{chapter_id}/contract[/compile]` | auth | owner + service chapter/project lookup | yes | POST yes | no | high |
| GET/PATCH | `/api/projects/{project_id}/story-state` | auth | owner | yes | PATCH yes | no | critical |
| GET | `/api/projects/{project_id}/story-state/transitions` | auth | owner | yes | no | no | high |
| POST | `/api/projects/{project_id}/story-state/transitions/{chapter_id}/resolve` | auth | owner + transition project lookup | yes | yes | no | critical |
| GET | `/api/projects/{project_id}/readiness` | auth | owner | yes | no | no | medium |
| GET | `/api/projects/{project_id}/readiness/experiment` | auth | owner | yes | no | no | medium |
| GET/PATCH | `/api/projects/{project_id}/character-voices[/{character_id}]` | auth | owner + profile project lookup | yes | PATCH yes | no | high |
| GET/POST | `/api/projects/{project_id}/writing-experiences` | auth | owner | yes | POST yes | no | high |
| POST | `/api/projects/{project_id}/writing-experiences/{experience_id}/accept` | auth | owner + DAO project filter | yes | yes | no | high |
| POST | `/api/projects/{project_id}/writing-experiences/{experience_id}/reject` | auth | owner + DAO project filter | yes | yes | no | high |
| GET/POST | `/api/projects/{project_id}/foreshadows/` | auth | owner | yes | POST yes | no | high |
| POST | `/api/projects/{project_id}/foreshadows/suggest` | auth | owner | yes | no | yes | critical |
| PATCH/DELETE | `/api/projects/{project_id}/foreshadows/{fid}` | auth | owner + DAO project filter | yes | yes | no | high |
| POST | `/api/projects/{project_id}/style-analysis` | auth | owner | yes | yes | yes | critical |
| GET | `/api/projects/{project_id}/style-profile` | auth | owner | yes | no | no | high |
| GET | `/api/projects/{project_id}/style-profiles` | auth | owner | yes | no | no | high |
| POST | `/api/projects/{project_id}/genre-style-profile` | auth | owner | yes | yes | no | high |
| POST | `/api/projects/{project_id}/style-profiles/{profile_id}/duplicate` | auth | owner + profile/project check | yes | yes | no | high |
| PUT | `/api/projects/{project_id}/style-profiles/{profile_id}/activate` | auth | owner + profile/project check | yes | yes | no | high |
| GET/PUT | `/api/projects/{project_id}/style-transition` | auth | owner | yes | PUT yes | no | high |
| GET | `/api/projects/{project_id}/style-contract` | auth | owner | yes | no | no | high |
| POST | `/api/projects/{project_id}/fulltext-ask` | auth | owner before service | yes | no | yes | critical |
| GET | `/api/projects/{project_id}/fulltext-stats` | auth | owner before service | yes | no | no | high |
| POST | `/api/ai/generate-outline` | auth | owner of body `projectId` | yes | no | yes | critical |
| POST | `/api/ai/generate-character` | auth | owner of body `projectId` | yes | no | yes | critical |
| POST | `/api/ai/continue-writing` | auth | owner of body `projectId` | yes | no | yes | critical |
| POST | `/api/ai/enhance` | auth | owner of body `projectId` | yes | no | yes | critical |
| POST | `/api/ai/deai` | auth | owner of body `projectId` | yes | no | yes | critical |
| POST | `/api/ai/chat` | auth | owner of body `projectId` | yes | no | yes | critical |
| GET | `/api/ai/llm-audit` | auth | maintainer | audit only | no | no | high |
| POST | `/api/ai/start` | auth | owner of body `projectId` | yes | yes | yes | critical |
| GET | `/api/ai/project/{project_id}` | auth | owner | yes | no | no | high |
| GET | `/api/ai/project/{project_id}/recoverable` | auth | owner | yes | no | no | high |
| GET | `/api/ai/project/{project_id}/history` | auth | owner | yes | no | no | high |
| GET | `/api/ai/{job_id}` | auth | owner resolved from job | yes | no | no | high |
| GET | `/api/ai/{job_id}/events` | auth | owner resolved from job | yes | no | no | high |
| POST | `/api/ai/{job_id}/resume` | auth | owner resolved from job | yes | yes | yes | critical |
| POST | `/api/ai/{job_id}/cancel` | auth | owner resolved from job | yes | yes | no | high |
| POST | `/api/review/language` | auth | owner of `projectId` + chapter/project check | yes | no | yes | critical |
| POST | `/api/review/logic` | auth | owner of `projectId` + chapter/project check | yes | no | yes | critical |
| POST | `/api/review/comprehensive` | auth | owner of `projectId` + chapter/project check | yes | no | yes | critical |
| POST | `/api/import/markdown-folder` | auth | debug-only, no project | filesystem | no | no | high |
| POST | `/api/import/markdown-folder/ingest` | auth | owner when `projectId` supplied; otherwise caller owns created project | yes | yes | no | critical |
| POST | `/api/import/markdown-files` | auth | caller uploads; reads no server path | yes (upload) | no | no | high |
| POST | `/api/import/markdown-files/ingest` | auth | owner when `projectId` supplied; otherwise caller owns created project. 可选表单字段 `excludedAnalysis`（JSON）按值过滤误识别条目，只删不补 | yes (upload) | yes | no | critical |
| POST | `/api/import/suggestion-docx` | auth | uploaded file only in production | upload | yes (temporary upload) | no | medium |
| POST | `/api/import/apply-suggestion` | auth | owner of body `projectId` | yes | yes | yes | critical |
| GET | `/api/settings/llm-configs` | auth | maintainer | global config metadata | no | no | critical |
| GET | `/api/settings/role-profiles` | auth | maintainer | global config metadata | no | no | critical |
| POST | `/api/settings/llm-configs` | auth | maintainer | no | yes | no | critical |
| PUT | `/api/settings/llm-configs/{config_id}/role-overrides` | auth | maintainer | yes | yes | no | critical |
| POST | `/api/settings/llm-configs/{config_id}/activate` | auth | maintainer | yes | yes | no | critical |
| DELETE | `/api/settings/llm-configs/{config_id}` | auth | maintainer | yes | yes | no | critical |
| GET | `/api/settings/llm-health` | auth | maintainer | global status | no | provider health only | critical |
| GET | `/api/rewrites?projectId=...` | auth | owner of query `projectId` + DAO project filter | yes | no | no | high |
| POST | `/api/rewrites/run` | auth | owner of body `projectId` | yes | yes (candidate audit) | yes | critical |
| POST | `/api/rewrites/{rewrite_id}/apply` | auth | owner resolved from rewrite record | yes | yes | no | critical |
| POST | `/api/copilot/candidates` | auth | owner of body `projectId` | yes | yes (candidate) | yes | critical |
| GET | `/api/copilot/candidates?projectId=...` | auth | owner of query `projectId` | yes | no | no | high |
| POST | `/api/copilot/candidates/{candidate_id}/accept` | auth | owner resolved from candidate record | yes | yes | no | critical |
| POST | `/api/copilot/candidates/{candidate_id}/reject` | auth | owner resolved from candidate record | yes | yes | no | high |
| POST | `/api/copilot/candidates/{candidate_id}/regenerate` | auth | owner resolved from candidate record | yes | yes (candidate) | yes | critical |
| POST | `/api/ai/_diag-continue` | debug only | unavailable in production | yes | no | yes | development only |

## Enforcement Notes

- `401`：没有有效身份；`403`：项目存在但当前用户不是 owner；`404`：目标项目或资源不存在。
- 有 `chapter_id`、`profile_id`、`experience_id`、`fid`、`candidate_id`、`rewrite_id`、`job_id` 的端点通过 project-scoped DAO 查询或由资源记录反查项目，不能用其他项目的资源 ID 绕过 owner 校验。
- LLM 入口的 ownership/auth guard 位于对应 service 调用之前；章节生成、review、rewrite、copilot 还沿用既有 budget、rate-limit 与 LLM audit 控制。
