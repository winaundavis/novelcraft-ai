# 脚本目录与运行边界
更新：2026-10-12。保留脚本不等于建议立即运行；“没有代码引用”也不等于运维工具无用。本次未执行以下真实生成、清理DB或回填工具。

## 首选入口
| 位置 | 用途 | 风险与前提 |
|---|---|---|
| backend/scripts/package_source.py | 源码ZIP与关键路径检查 | 写归档/解压临时目录；POSIX /；过滤不是完整隐私审查，待补dotenv变体/备份排除 |
| backend/scripts/backup_sqlite.py | 显式源/目标备份恢复 | 文件复制，须停止写者并确认WAL；--replace会替换目标并留原库备份 |
| backend/scripts/novelcraft_doctor.py | inspect_project诊断报告 | 读取所配置的项目/DB，输出可能含内部状态，仅维护者本地使用；不能当公开健康接口 |
| backend/scripts/prepare_beta_smoke.py | 生成Chapter1 dry-run manifest | 读取指定项目/章并写报告；不是真实生成，仍要确认数据/输出隔离 |
| backend/scripts/backfill_import_outline.py | 为「有章节但无大纲」的历史项目补生成大纲 | **会写 `projects.outline`**；默认只预览，必须 `--apply` 才写；不覆盖已有大纲；目标库由 `NOVELCRAFT_DB_PATH` 决定，跑前先核对 |
| backend/scripts/repair_import_outline_ids.py | 把导入产生的大纲节点 id 重连回真实章节行 | **会写 `projects.outline`（只改 node id）**；默认只预览，必须 `--apply`；写入前把旧值备份到**数据库旁边**的 `outline-ids-backup-<时间戳>.json`；order+标题不一致、有二义、或目标已被占用时**只报告不写入**；不碰标题/顺序/正文，不增删节点 |
| backend/scripts/backfill_import_characters.py | 离线批量把「导入时识别到的人物」补进 `projects.characters` | **会写 `projects.characters`**；默认只预览，必须 `--apply`；**只增不改**（同名一律保留作者手写档案）；写前把旧值备份到**数据库旁边**的 `characters-backup-<时间戳>.json`（同一秒连跑也不会互相覆盖）；`--exclude 名字1,名字2` 可先挑掉启发式误判；不碰章节，因此不能替代重新导入。**日常已不需要它**：导入候选现在由「人物」页勾选入库（`POST /import/projects/{id}/characters/from-analysis`），本脚本只在需要离线/批量处理历史数据时使用 |
| backend/scripts/invite_tool.py | 邀请码发放／停用：`new --count N --label 备注`、`list`、`disable --hint ab12` | **明文只在 `new` 的输出里出现一次**（库内只存 sha256 与后 4 位）；`list` 不回显明文，忘了只能停用重发 |
| backend/scripts/migrate_legacy_owner.py | 把历史归属收敛到指定账号（归属统一） | **会写 `projects.owner_id` / `outline_candidates.user_id`**；默认 dry-run，必须 `--apply`；`--apply` 前强制备份且**不覆盖已有备份**；幂等；不碰 `user_sessions` |
| backend/scripts/check_orphan_owners.py | 只读体检：列出没有对应用户行的 `owner_id`、悬空邀请码 | 只报告不修改，可随时运行 |
| frontend/scripts/browser-bridge.mjs | 本地浏览器自动化桥接 | 可以操纵页面；不能暴露到公网；是否写真实后端取决动作 |
| backend/tests/ 与 frontend/tests/ | 回归测试 | 见[测试隔离](test-suite-runtime.md)，不是所有旧E2E都自动离线 |

首选备份完整命令和恢复限制见[启动文档](production-startup.md)，避免同时存在两个备份工具时用错数据库。

## 历史工具：保留，逐项审批运行参数
| 位置 | 用途/风险 |
|---|---|
| scripts/backup_db.py | 旧备份恢复入口；硬编码 backend/data/novelcraft.db，不代表当前 NOVELCRAFT_DB_PATH，默认输出 test_artifacts/db_backups |
| scripts/api_smoke_test.py | API巡检，可能创建/修改数据和触发AI |
| scripts/api_smoke_novel_jobs.py | 创作任务smoke，可能写项目/调用LLM |
| scripts/audit_longrun_memory.py | 长跑记忆审计，不是默认suite |
| scripts/backfill_memory.py | 回填派生记忆，会写数据 |
| scripts/cleanup_test_data.py | 清理测试数据，属于破坏性DB操作；不能自动跑 |
| scripts/cross_model_review.py | 跨模型评审，可能真实付费 |
| scripts/long_run_report.py | 长跑结果报告，读运行数据并写产物 |
| scripts/report_chapter_targets.py | 章节目标报告，需核对所连DB |
| scripts/verify_100k_generation.py | 历史长篇验证，本阶段禁止真实运行 |
| backend/_qa_synthetic_memory_test.py | 历史合成容量/记忆诊断，可能创建数据/备份；保留但不当真实文学验收 |
| backend/scripts/prepare_experiment_001.py、prepare_phase7d_experiment.py | 实验准备和输出；不在用户Beta库运行 |
| backend/scripts/run_experiment_001.py | 实验执行入口，需单独核定模型/次数/预算 |
| backend/scripts/style_regression.py | --mock/--real模式；--real收费，旧价格不可作为Arts账单 |
| backend/scripts/e2e_report.py | 汇总测试/DB审计和live evidence；报告不代替实际测试 |

运行前阅读脚本而不是盲运行 --help（有些入口在解析参数前就导入应用/配置）。RUN_LIVE_LLM=0不是所有历史脚本都实现的安全开关，要核对provider mock和网络拦截。

## 本轮移除与保留
移除3个一次性残留和可再生Python/pytest缓存，路径/哈希/恢复见清理记录。

保留 test-fixtures、experiment_fixtures、历史报告、所有数据/上传/备份、node_modules、dist、tsconfig.tsbuildinfo、.edge-*浏览器资料、.npm-cache、test_artifacts、tools中的既有工具与父目录历史压缩包。未证实无用的文件不删。浏览器资料、密钥、真实作品和测试产物不得随源码发布。

## 2026-10-12 复核
**移除**：12 个运行日志（`backend/longrun-*.log`、`backend/server*.log`、`frontend/vite*.log`）、3 个 Playwright 产物目录（`test-results-offline/`、`frontend/test-results/`、`frontend/playwright-report/`）、13 个 `__pycache__/` 与 `backend/.pytest_cache/` —— 合计 **约 8.1 MB**，全部是可再生缓存或运行日志，删除前逐个确认「除本清单外无其他引用」。详见清理记录。

**保留（复核后仍不删）**：`data/`（真实库与备份）、`test_artifacts/`（测试与审计证据）、`.edge-profile/`（654 MB）与 `.edge-cdp/`（23 MB）浏览器资料、`.npm-cache/`（46 MB）、`tools/cloudflared-*`（51.7 MB 隧道工具）、`scripts/` 下的历史工具、`backend/_qa_synthetic_memory_test.py`、`test-fixtures/`、`memory/`、`skills/`、`frontend/tsconfig.tsbuildinfo`。
理由：它们要么不可再生或属于他人数据，要么仍被源码／测试／脚本／文档引用，要么是刻意保留的运维工具 —— **未证实无用的文件不删**（沿用 2026-09-13 的规则）。
