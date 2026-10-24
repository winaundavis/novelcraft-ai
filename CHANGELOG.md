# NovelCraft AI · 更新日志

## 2026-10-24 · Unreleased：运营后台 + 产品层额度开关 + 云主机部署落地

### 一、新增运营后台（仅管理员）

- 后端 `app/api/admin.py`：`/api/admin/overview`、`/users`、`/usage`、`/invites`（GET/POST）、`/invites/{id}/disable`，全部 `Depends(require_maintainer)`。
- 前端 `components/admin/AdminConsole.tsx`，路由 `/admin` 由 `RequireAdmin` 包裹，左侧菜单 `adminOnly` 过滤。
- 覆盖内容：就绪状态与错误码、运行时长、最近备份、磁盘、数据规模；用户列表（含今日/近 N 日调用数与 token）；按日趋势与项目 Top 10；邀请码批量生成（1–20）、停用、状态筛选、一键复制与发送话术。
- **故意不做**：查看作品正文、停用账号、重置密码、改用户额度。因此后台只有两个写操作。
- 权限三层：菜单 / 路由 / 接口。**实测**普通用户访问四个管理接口全部 403，`/api/projects/` 仍 200；匿名 401。测试：`tests/test_admin_api.py`（19 项）。

### 二、产品层额度：非管理员每日自动创作章数

- 新增 `USER_DAILY_CHAPTER_LIMIT`（默认 10）与灰度开关 `USER_DAILY_CHAPTER_LIMIT_ENABLED`。
- 口径：**只统计「按大纲自动创作」成功落库的章**；改写、审校、续写、大纲、人物、对话**不占用**额度；管理员不受限。
- 实现：启动任务时按剩余额度**裁剪本次章节数**（不改任务状态机），逐章记账；后台并发启动时另有逐章硬保护。
- 与既有 `PROJECT_DAILY_LLM_CALL_LIMIT/TOKEN_LIMIT` 是**不同维度**：前者按「用户+日」计章数，后者按「项目+日」计调用/token（成本护栏）。
- 生产就绪校验：开关为 `true` 时要求限额 >0，避免误配静默关闭；开关为 `false` 时允许 0。
- 测试：`tests/test_user_daily_quota.py`（16 项）。

### 三、修复

| 问题 | 影响 | 处理 |
|---|---|---|
| `scripts/e2e_report.py` 写死 Windows 输出目录 `<REPO_ROOT>\creat-reports` | Linux 上被当成**文件名**，在当前目录造出垃圾目录，并让源码打包测试失败 | 改默认到系统临时目录，可用 `NOVELCRAFT_REPORT_DIR` 覆盖 |
| 静态资源缺失时被 SPA 规则回落成 `index.html` | 浏览器缓存了旧首页时，把 HTML 当 JS 执行 → **整页白屏** | Caddy 为 `/assets/*` 单独建规则，缺失直接 404 |
| 前端使用不存在的 Tailwind 类名（`bg-primary`、`text-danger`） | Tailwind 静默忽略 → 按钮/错误条**白底白字隐形**，构建与 lint 全绿 | 改用 `primary-600` 与 `error` 色阶，并在构建产物 CSS 中确认类存在 |

### 四、部署落地（非代码）

- 单实例云主机：Caddy 自动 HTTPS + `sslip.io` 免费域名 + 多版本目录与软链接切换 + 每日备份定时器（含恢复演练）。
- 新增文档：[运营后台](docs/ADMIN_CONSOLE.md)、[云主机运维手册](docs/OPS_RUNBOOK.md)。
- 弃用「本机 + 免费隧道」方案：免费隧道 12 小时断线且每次换地址。

## 2026-10-22 · Unreleased：逻辑复查——统一字数口径 + 去掉 assert 当错误处理

### 一、字数口径四处不一致（用户可见）

同一段正文，不同入口算出不同字数：

| 入口 | 旧公式 |
|---|---|
| 手动保存 / Copilot 接受 / 正文生成 / 重写 | `replace(" ","").replace("\n","")` |
| 导入 | `re.sub(r"\s+", "", …)` |
| 前端显示（`/\s/g`） | 去掉所有空白 |

差异来源：`\r`（Windows 换行，旧后端**每段多算 1 字**）、`\t`、全角空格 U+3000
（前端不算、旧后端算）。表现：编辑器显示 1200 字、存库 1250 字；导入的章节
与手工编辑的章节口径不同，同一本书的字数“加不起来”。

**修复**：新增 `app/utils/word_count.py` 的 `count_words()`（去所有空白后计长度，
与前端 `content.replace(/\s/g,'').length` 完全等价），五处统一调用：
`api/chapters.py`、`copilot_candidate_service`、`novel_generation_service._count_words`、
`rewrite_service._count_words`、`import_service`。

顺带发现 `rewrite_service` 里 `_strip()`（正确公式）与 `_count_words()`（错误公式）
紧默相邻 —— 典型的复制粘贴不一致。

### 二、`assert` 被当成错误处理

`dao/style_dao.py` 写完画像后 `assert profile is not None`：
`python -O` 会剥掉 assert，那时会静默返回 None，调用方在别处炸掉且现场信息全丢。
改为显式 `RuntimeError`。

### 三、本轮的逻辑扫描结论（无其他发现）

| 扫描项 | 结果 |
|---|---|
| 可变默认参数（`def f(x=[])`） | 0 |
| 裸 `except:` | 0 |
| `return`/`raise` 后的不可达语句 | 0 |
| if/elif 链重复条件 | 0 |
| `== None` | 0 |
| `x != x`（NaN 惯用法） | 4 处，均为**正确**的 NaN 防护 |
| 本地时间 vs UTC 混用 | 无，全部统一 UTC |
| 未保护的 `JSON.parse` | 无，均有 try/catch |
| 重复接口路由 | 0 |

`except Exception: pass` 共 50 处，逐处抽查均为**有意的降级**（FTS 派生索引、
旧库兼容、搜索降级到 LIKE），不是静默丢主数据。

### 验证

- 新增 `tests/test_word_count.py`（3 条）：CRLF / 全角空格 / 制表符下所有入口同口径；
- 后端 flake8 **0 problems**；全量 **1493 passed / 0 failed**。

## 2026-10-22 · Unreleased：补上前后端静态检查门禁 + 修 3 个潜在崩溃点 + 迁移旧密钥备份

### 一、前端 ESLint 门禁（此前完全没有）

新增 `frontend/eslint.config.mjs`（ESLint 9 扁平配置）+ `npm run lint` / `lint:fix`。
依赖：`eslint@9`、`@eslint/js`、`typescript-eslint@8`、`eslint-plugin-react-hooks`、
`eslint-plugin-react-refresh`。

**刻意不整套启用 `recommended`**：本仓库有大量存量代码，一次全开 `no-unused-vars`/
`no-empty` 会产生上百条噪声，最后只会被关掉。只开两类：

1. **真实缺陷**：重复键/重复 case/不可达代码/自赋值/条件里写赋值/case 穿透/
   async Promise executor 等；
2. **React Hooks 铁律**：`rules-of-hooks` = error，`exhaustive-deps` = warn。

首次运行结果：**1 error + 12 warnings** → 现状 **0 error / 9 warnings**（exit 0，可做门禁）。

### 二、后端 flake8 门禁（此前完全没有静态检查）

新增 `backend/.flake8` + `flake8==7.4.1`（写进 `requirements-dev.txt`），
规则只开「写了就一定错」的：`E9` 语法/运行时、`F63`、`F7`、`F811` 重复定义、
`F82x` 未定义名字。命令：`python -m flake8 app main.py tests`。

> `ruff` / `mypy` 在当前内部 pip 索引里取不到（`No matching distribution found`，
> 直连 pypi 也不通），因此先用 flake8 落地同等价值的最小门禁；
> 索引恢复后可直接平移到 ruff（规则集可一一对应）。

### 三、门禁当场抓出的 3 个真实缺陷（全部已修）

| 文件 | 问题 | 影响 |
|---|---|---|
| `app/services/reference_work_service.py:193` | `projects_dao` **从未导入** | `_owner_of()` 每次都抛 NameError 并被 `except: return "local-dev"` 吞掉 → **参考档案归属永远记成 local-dev**，真实作者信息丢失 |
| `app/services/novel_generation_service.py:1212` | `_logger` **从未定义** | 事实核查未通过时抛 NameError，又被外层 `except Exception: pass` 吞掉 → 该告警**永远不输出** |
| `app/dao/memory_entities.py:228` | 注解用了 `sqlite3` 但**未导入** | 有 `from __future__ import annotations` 所以不崩，但注解是坏的前向引用（任何求值注解的工具都会失败） |

同时修掉前端唯一一条 error：`storage/indexeddb.ts` 的
`new Promise(async (resolve, reject) => …)`（`no-async-promise-executor`）——
改为先 `await openDB()` 再进同步 executor，语义等价且不再依赖那块 try/catch。

### 四、旧密钥备份迁出仓库

`backend/.env.bak-20261007-210854` 已移到 `<REPO_ROOT>\NovelCraft-private\env.bak-20261007-210854.txt`。
（核对其中的 `*_API_KEY` / `INVITE_TOKENS` **均为空值**，风险很低；此文件本来就在
`.gitignore` 的 `backend/.env.bak*` 里、未被 git 跟踪。）

### 验证

- 后端：`flake8` **0 problems**；全量 **1490 passed / 0 failed**；
- 前端：`npm run lint` **0 error / 9 warnings**、`tsc --noEmit` **0**、
  `npm run build` **通过**、单测 **107 passed**。

### 备注

- `frontend/package.json` 由 **npm** 更新（仓库里同时存在 `package-lock.json` 与
  `pnpm-lock.yaml`）。若以后改用 pnpm，需要重跑一次 `pnpm install` 刷新 `pnpm-lock.yaml`；
- `F401`（未使用导入）现有 15 处、`exhaustive-deps` 9 处 warnings，均**未**纳入门禁，
  建议后续单独一轮清理后再打开。

## 2026-10-22 · Unreleased：世界规则在自动创作里真的生效（此前是"部分注入 + 无人校验"）

### 核查结论（改前）

规则**会**进提示词，但只进一部分，且**没有任何事后校验**：

| 注入点 | 改前上限 | 问题 |
|---|---|---|
| `_compact_world_rules` | 前 **12** 条 | 第 13 条起静默丢弃 |
| `_build_continue_prompt` 的「世界规则」 | 前 **12** 条 | 同上 |
| `scene_guidance` 的 Hard Story Truth | 前 **12** 条 | 同上 |
| `project_context` 的 L1 块 | 前 **8** 条 + 600 字 | 同上 |
| 事实核查 prompt | —— | 要求"查世界观规则矛盾"，但**从未把规则交给核查员** → 该类检查等于失效 |
| 系统提示 | —— | 没有任何"规则是硬约束"的表述，模型容易当背景资料 |

即：**设 15 条规则，至少 3 条永远不会被看到；违反了也没人发现。**

### 改动（4 处，新增 1 个工具模块）

1. 新增 `app/utils/world_rules.py`：`select_world_rules()` / `world_rule_lines()` ——
   **按字数预算**选取（默认 1500 字 / 24 条），装得下就全给；装不下才截断，
   并补一条显式说明：「另有 N 条世界规则因篇幅未列出；未列出的规则同样有效，不得自行新增或改写」；
   同时兼容 dict（项目存储）与 pydantic 对象（请求体）两种形态 —— 只认 dict 会让续写提示里的规则整块消失；
2. `_compact_world_rules()` 去掉 `[:12]` 硬截断，只做字段归一化；
3. `scene_guidance` 的小标题改为「世界规则（硬约束，违反即为设定矛盾）」并走同一预算；
   `project_context` 的 L1 块同样改走预算（保留 600 字上限）；
4. `CONTINUE_SYSTEM_PROMPT` 增加硬约束声明：
   「世界观规则是硬约束：不得违反，也不得为了情节方便而改写、弱化或例外化任何世界规则；
   若情节与规则冲突，改情节，不改规则。」
5. **事实核查能看到规则**：`_run_self_check_loop(..., world_rules=...)` 把规则挂进
   `memory_context["world_rules"]`，`build_fact_check_context` 渲染成独立块
   （800 字预算，不挤占卷摘要/账本），第 5 类「设定矛盾」这才真正可判。

### 验证

- 新增 `tests/test_world_rule_injection.py`（9 条）：15 条规则**全部**进入续写提示与场景硬约束、
  compact 不再截断、超预算显式告知、核查上下文带规则、无规则时不污染、系统提示含硬约束；
- 改前实测：15 条规则只有 12 条进提示；改后 15/15，核查侧同样 15/15；
- 后端全量 **1490 passed / 0 failed**。

### 仍未做（需产品决策）

- 规则没有「硬约束 / 背景设定」分级字段，目前一律按硬约束处理；
- 违规仍然只是"可能被事实核查抓到"，没有专门的"世界规则合规"报告项。

## 2026-10-21 · Unreleased：解除「项目每日 AI 额度」误拦（配置调整，未改代码）

### 问题

生成失败并提示「今日项目 AI 使用额度已达到上限」，但当天并没有做多少操作。

### 根因：限制值太小，而它是硬编码默认值

`app/core/config.py` 默认 `project_daily_llm_call_limit = 30`、`project_daily_token_limit = 120000`
（public-beta 加固时引入的成本护栏），`.env` 未覆盖。真实数据：

| 项目 | 日期 | 调用 | token |
|---|---|---|---|
| `2889a2d94a19` | 2026-10-20 | 21 次 | **113,249**（限额 120,000 的 94%） |

一次整书创作每章会串行触发多次调用（正文 / 事实核查 / 钩子 / AI 味 / 摘要 / 契约），
所以 30 次、12 万 token 在自有部署下当天就会被正常创作打满。

### 处理：只放宽配置，不动代码

`backend/.env` 追加：

```
PROJECT_DAILY_LLM_CALL_LIMIT=100000
PROJECT_DAILY_TOKEN_LIMIT=100000000
```

**为什么不用 0 表示"不限"**：`0` 在本项目里已有既定语义 = 「一律拦截」
（`test_project_daily_budget_blocks_before_provider_call` 专门钉住这条），
把它改成"不限"会反转语义并破坏测试。放宽到实际不可达的数值是零风险做法。
需要恢复护栏时把两个值改回 30 / 120000 并重启即可。

### 验证（合成项目 id，未触碰真实项目计数）

同一起点（21 次 / 113,249 token，再申请 15,000 token）：

| 限制 | 结果 |
|---|---|
| 旧 30 / 120,000 | **BLOCKED**（今日项目 AI 使用额度已达到上限） |
| 新 100,000 / 100,000,000 | **PASS** |

### 遗留建议

1. 这个额度应该做成**部署级配置项**（含设置页可见 / 可改），而不是硬编码默认；
2. 若仍要成本安全，更合理的是「接近上限时告警（如 80%）+ 单任务预算（已有
   `ChapterGenerationBudget`）」，而不是让整批创作突然失败；
3. 注意 `RATE_LIMIT=120`（每分钟请求数）是另一道闸门，本次未动。

## 2026-10-21 · Unreleased：智能审校「一键修改」不再改到一半就整批失败

### 现象

勾选几条审校建议一键修改，改到几条就整批失败，只能重新审校重来。

### 根因（两个，都会让整批停下）

1. **前端超时比后端早 10 倍**：`aiApi.copilot.createCandidate` 用默认 **60s** 超时，
   而后端写作角色是 `ROLE_TIMEOUT_SECONDS["writing"] = 600s`。
   整章 / 长段落改写经常超过 60s → 浏览器先 `abort` → 抛"请求超时" → 批量立即中断。
2. **"这条没什么可改"被当成致命错误**：模型对某条建议返回原文（`copilot_no_change`）、
   返回空（`copilot_empty_output`）、或调用降级（`copilot_provider_failed`）时，
   服务端抛 400 纯文本，前端无法区分"没改动"与"正文已变"，只能保守地整批停下。

### 改动（后端 3 处 / 前端 3 处，不动架构）

**后端**
1. `copilot_candidate_service.create_candidate()`：这三类失败抛出时挂上机器可读码
   `copilot_code`，中文提示文案保持不变（旧调用方不受影响）；
2. `api/copilot.py`：带码的 `ValueError` 返回结构化 detail `{code, message}`，
   前端 `ApiError.code` 才拿得到；
3. 「正文已更新 / 选区已变」这类**必须停**的失败**不带**码 —— 继续改会基于错位文本。

**前端**
4. **不再因为「正文已改过」锁死一键修改**（直接消除"必须重新审校"）：
   原逻辑 `selectionLocked` 含 `stale`，批处理改成功一条 → 正文变了 → 整张表锁住。
   现在保留过期提示横幅，但允许继续应用**未改过**的条目；正确性仍由原有保护兜底
   （原文不唯一 → 退回整章指令改写；服务端 accept 再校验一次原文）；
5. `createCandidate` / `regenerate` 改用 `CANDIDATE_TIMEOUT_MS = 180s`，与后端写作角色对齐；
6. 新增零依赖模块 `components/review/reviewApplyFailure.ts`：定义可跳过码集合与判定；
   创建候选阶段的超时也会被标成 `copilot_create_timeout`（此时还没 accept，正文一定没改）；
7. `ReviewPanel` 批量循环：可跳过失败 → **跳过继续改下一条**并计数；
   其它失败 → 立刻停并如实报告。连续 2 条可跳过则主动停止（防模型整体故障时空转）。

### 语义（刻意如此）

- 跳过 ≠ 假装成功：跳过的条目**保持勾选**、不写入 `appliedIssueKeys`，提示里明确写
  "跳过 N 条（模型未产生实际修改，正文未动）"，用户可再点一次只重跑这几条；
- 必须停的三类（正文已变 / 风格计划冲突 / 本地未保存）**一律不跳过**，
  因为它们意味着继续改会基于错位文本。

### 验证

- 新增 `backend/tests/test_review_batch_apply_failures.py`（5 条）：
  三类可跳过码、正文未变、候选数仍为 0、"正文已变"不带码、接口层结构化 detail；
- 新增 `frontend/tests/reviewApplyFailure.test.mjs`（3 条）：可跳过集合、
  计划冲突/无码不跳过、非错误对象不误判；
- 后端全量 / 前端单测 / `tsc` 结果见下方交付说明。

## 2026-10-20 · Unreleased：去 AI 味收尾优化（幅面上界 + 台词保真 + 连接词口径）

上一轮修掉了「静默吞正文」（下界）。本轮只补三个**低成本、高收益**的漏口，不动架构。

### 1. 输出幅面加上界（防"夹带内容"）

实测中模型把 255 字扩写成 501 字（1.96 倍）——去味本不该新增场景与情节。

- 新增 `MAX_DEAI_RESULT_RATIO = 1.5`：超出即判定失败、保留原文；
- 新增 `DEAI_RATIO_MIN_SOURCE = 200`：短文本（<200 字）不做幅面比例判定，
  只保留绝对长度下限 —— 短句改写"看起来翻倍"没有统计意义，否则会把正常短稿全判失败。

### 2. 台词数量保真（防"把对白改成叙述"）

`_dialogue_marks()` 统计中文引号/直角引号/英文引号对，`DEAI_DIALOGUE_TOLERANCE = 2`：
台词段落数漂移超过 ±2 即保留原文。去味可以改说法，但**不能改对话结构**——那是改事实。

### 3. 连接词检测口径修正（规则维护）

`AI_CONNECTIVE_WORDS` 原先把 **逻辑转折词**和**模糊限定词**混算：

- 移除 `但是 / 然而`（人类写作的正常连接，算进来只会误报）；
- 加入 `像是 / 宛如 / 隐约`（评审指出的真正重灾区）。

阈值不变，只改口径——避免"一边误报一边漏报"。

### 4. 阈值校准（真实章节实测，不是拍脑袋）

取真实项目已落库章节（`5a9de8085f27`，3080 字，24 处台词）走完整流程：

| 项 | 结果 |
|---|---|
| 输出长度 | 3017 字（ratio **0.98**） |
| 台词数 | 24 → 24（Δ**0**） |
| 判定 | `degraded=False, keptWindows=0` —— 正常改写并采用 |

结论：上界 1.5 / 台词 ±2 在真实章节上**不会**把正常改写误杀（不会出现"点了没反应"）。

### 验证

- 后端相关测试 `test_deai_service / test_review_service / test_phase10f_generation_policy` **77 passed**；
- 详见下方全量结果。

### 仍未处理（需要产品决策，非技术债）

- 检测器对**真实 AI 章节**评分仍是 100 分（无质量债）——说明当前规则能抓"形式重复"，
  抓不到语义层 AI 味（段末升华、解释性台词、伪钩子）；要动就得先建标注集校准；
- 去 AI 味仍是「整章覆盖、不可回滚」，缺 Diff / 逐段接受 / 自动快照。

## 2026-10-20 · Unreleased：去 AI 味「静默吞正文」修复 + 章节名补齐

### 一、去 AI 味功能此前**不可用**（实测证据）

真实调用 6029 字正文：

| 项 | 修复前 | 修复后 |
|---|---|---|
| 输出长度 | **150 字**（原 6029 字） | 6010 字（保留原文） |
| `degraded` | **False**（当成成功） | True |
| 作者看到 | 「去 AI 味完成，已调整 N 处」 | 「本次未改写正文：模型返回内容被截断或大幅缩写」 |

三个叠加的缺陷：

1. **窗口与预算矛盾**：`DEAI_MAX_INPUT_CHARS=8000` 配 `DEAI_MAX_TOKENS=4000`。
   中文输出 ≈ 1 token/字，8000 字窗口必然写到一半被截断；
2. **成功判定形同虚设**：`len(rewritten) >= 20` 就算成功 —— 6000 字变 150 字也通过；
3. **没有截断识别**：从不检查 `finish_reason`。

结果：整章被替换成一小段，且没有任何告警。

### 二、修复内容（去 AI 味）

1. 窗口/预算对齐：`DEAI_MAX_INPUT_CHARS` 8000→**6000**，`DEAI_MAX_TOKENS` 4000→**8192**，
   新增 `_window_max_tokens()` 按窗口长度动态给预算（`min(8192, len+1024)`）；
2. **长度保真校验** `_rewrite_is_acceptable()`：结果低于源文 **60%** 即判定失败，
   该窗口保留原文；`finish_reason=length` 同样保留原文；
3. 返回体新增 `keptWindows`，前端据此区分「服务不可用」与「结果被截断/缩写」两种降级；
4. **跨章规则真正生效**：`deai_rewrite(project_id, text, chapter_id="")`，
   传 `chapterId` 后加载上一章正文传给 `detect_ai_artifacts` ——
   此前只传 text，「开头重复」「场景关键词组」两条跨章规则**永不命中**；
   前端 `aiApi.deai` / `MarkdownEditor` 已透传 `currentChapterId`；
5. 规则更新（据网文创作评审）：连接词口径拆开「模糊限定词」与「逻辑转折词」；
   去味提示词补齐 3 条最致命的遗漏 —— 段末不总结升华、不复述前文、
   一段最多一个比喻且不用天气光线开段；
6. 删除 `deai_rewrite` 末尾的**不可达死代码**（return 之后的重复 return，且写死 `degraded: False`）。

### 三、正文生成提示词（生成前就避免 AI 病）

`CONTINUE_SYSTEM_PROMPT` 追加 5 条可执行约束（详细评审依据见下）：
段末不升华点题 / 情绪用动作写不贴标签 / 对话要短允许打断与答非所问 /
少用「仿佛/似乎/像是」且一段最多一个比喻 / 不复述前文、不解释动机。

### 四、章节名补齐（修「一部分章节没有章节名」）

真实数据：项目 `2889a2d94a19` 的 100 章大纲中 **14 章只有「第N章」没有名字**，
且缺口全部集中在各弧的**尾部**（弧声明 12 章只给了 10 个标题）。

- 根因：模型返回的 arc `chapters` 数量偶尔少于 `chapterCount`，
  `_generate_outline_nodes` 只能把剩余章节落成裸「第N章」；
- 修复：`_fill_missing_chapter_titles()` —— **仅在有缺口时**触发一次补名调用
  （无缺口零成本），模型补齐失败或缺额时用「弧名·弧内序号」确定性补名，
  绝不留下裸「第N章」；重复/撞名建议会被丢弃。

### 五、验证

- 新增 `test_outline_chapter_title_fill.py`（5 条）与去 AI 味 4 条新用例；
- 真实调用复测：超长正文 → `degraded=True, keptWindows=1`，正文完整保留；
  真实章节 → 正常改写（评分 90→100）；
- 后端全量 **1472 passed / 0 failed**；前端单测 **104 passed**；`tsc --noEmit` **0**。

### 尚未处理（需产品决策，已在评审中列为建议）

检测阈值偏误报（句首 4 字相同率 ≥0.5、同章 10 字重复等）、
缺少段末升华/解释性台词等语义层规则、去 AI 味缺 Diff/逐段接受/自动快照（不可回滚）。

## 2026-10-20 · Unreleased：大纲卷级信号逐卷生成（修「每卷都是同一句模板」）

### 问题

大纲页每一卷的卷简介下面，都是同一行小字：

```
冲突：主线冲突持续升级    揭示：更深一层的真相开始浮出水面    情绪：张力上扬
```

### 根因（两层，缺一不可）

1. `_generate_outline_nodes()` 把这三句**写死成常量**，即使模型返回了卷级内容也会被覆盖；
2. 两份大纲提示词（单轮 / 分卷）**从未要求**模型给出 `conflict` / `revelation` / `emotion`，
   分卷路径 `_generate_outline_volume_plans()` 也只保留 `title` / `summary` / `arcs`。
   → 于是这三行永远是常量，"看起来像功能坏了"，实际是**没有内容可显示**。

### 改动

1. 提示词：单轮与分卷提示词均要求每卷给出 `conflict`（本卷核心冲突）、`revelation`（本卷揭示）、
   `emotion`（本卷情绪基调），并明确要求各卷之间不得相同、禁止套用通用模板句；
2. 分卷路径保留这三个字段（否则组装时又会退化成兜底文案）；
3. `_generate_outline_nodes()` 优先使用模型给出的信号；**撞车保护**：某卷信号为空、
   或与前面卷重复时（模型偶尔把同一句写给多卷），退回**按卷位区分**的兜底文案，
   并用 `_clean_signal()` 压掉换行、截断到 40 字；
4. 章节级信号同样去模板化：改为按**弧**生成（`「弧名」中主角目标与阻力正面碰撞`），
   同一弧共享（这是设定），不同弧必定不同 —— 此前全书 100 章共用同一句
   "揭示信息"，会被注入每章正文写作提示；
5. 前端 `OutlineGenerator` 三个标签**逐项判空**，全空时整行不渲染（宁可少一行，不显示模板垃圾）；
   本地兜底大纲 `buildFallbackOutline()` 也改成按卷位给不同文案。

### 验证

- 新增 `backend/tests/test_outline_volume_signals.py`（7 条）：模型值优先、逐卷兜底不重复、
  撞车去重、两卷场景、章节按弧区分、超长清洗、分卷路径保留信号；
- 前端 `normalize.test.mjs` 新增 1 条：兜底大纲 3 卷 / 2 卷均不重复且不回旧模板；
- 真实调用（6 章 / 2 卷）：`引灯人` → `陈默追查怨魂异常，与"归途"公司正面冲突。` / `怨魂异常源于"归途"公司的实验。`；
  `归途尽头` → `陈默对抗"净化计划"…` / `"归途"背后是政府净化计划…`，两卷完全不同；
- 后端全量 **1463 passed / 0 failed**；前端单测 **104 passed**；`tsc --noEmit` **0**。

### 说明

存量项目里已保存的大纲仍是旧的三句常量（数据已落库）。重新生成大纲即生效；
如需保留现有大纲又想更新这三行，可在大纲里直接编辑卷节点。

## 2026-10-20 · Unreleased：正文输出预算加下限（修「截断的正文被当成成功落库」）

### 问题

正文类调用（正文生成 / 续写 / 扩写 / 润色 / 去 AI 味 / 重写）都用**全局** `max_tokens`
（当前 4096）。长章节在 4096 处被截断时，`finish_reason=length` **只记录、不提示**，
截断的半句话会被当成成功结果落库 —— 作者很可能以为本章已经写完了。

### 方案取舍（为什么选这个）

| 方案 | 代价 | 结论 |
|---|---|---|
| 像大纲那样**拒绝**截断内容 | 低 | ❌ 正文被截断是**常态**（长章节必然贴近上限），拒绝会让生成频繁失败 |
| 检测后通知前端（任务面板/编辑器提示） | 中高（job 字段 + 前端交互） | 后续再做 |
| **给正文角色一个输出下限** | **低（一处集中生效）** | ✅ 采用 |

### 改动（集中一处，覆盖全部正文调用点）

1. `DEFAULT_ROLE_PROFILES[ROLE_WRITING]` 增加 `max_tokens: 8192`；
2. 新增 `LLMProvider._resolve_max_tokens()`：**显式 kwargs > `max(角色下限, 全局配置)`**，
   非流式、流式、`request_manifest` 三处共用同一解析（manifest 因此永远反映真实请求值）；
3. `ROLE_PROFILE_KEYS` 增加 `max_tokens`，允许 `role_overrides` 按角色覆盖。

**关键约束：角色值只抬下限，绝不压低**作者已经调高的全局配置 —— 有测试专门钉住这一点。

### 前置验证（避免"改完反而全线 400"）

实测当前 provider（arts / deepseek-v4-flash）对 `max_tokens` = 4096 / 8192 / 16384
**全部返回 200**，所以抬到 8192 不会引入请求被拒的风险。

### 验证

- 新增测试：正文 `request_manifest` 现在报告 **8192**；`_resolve_max_tokens` 的优先级
  与"不压低全局配置"行为各一条。
- 后端全量 **1456 passed / 0 failed**。

### 仍未处理

- 截断**仍不会主动告知作者**（只是不再那么容易发生）。彻底方案是把"本章被截断"带到
  任务面板/编辑器，让作者看到后决定是否继续续写。
- 单章目标字数超过约 6000 字时 8192 仍可能不够；届时应按 `targetWordCount` 动态给预算，
  而不是继续抬高固定值。

## 2026-10-20 · Unreleased：长书大纲改走「分卷生成」（仅首轮被截断时自动切换）

### 背景

2026-10-19 修掉了「半个 JSON 被当成成品」，但那只做到**识别 + 拒绝**：
一次装不下时，作者拿到的仍是一份模板大纲。本轮把它补完整。

### 做法：默认路径**完全不动**

- 单次生成仍是默认：未截断时**只调一次**，调用次数与花费和之前一模一样；
- **只有 `finish_reason=length`**（也就是原本会失败/降级的场景）才自动改走分卷生成；
- "已有功能不动"是硬约束，所以专门写了一条测试钉住"未截断时**不许**拆分调用"。

### 分卷生成

1. **`_outline_volume_chapter_counts()` 作为卷数/每卷章节数的唯一来源**：
   分卷生成与 `_generate_outline_nodes` 共用同一份计算。两边一旦漂移，
   多出来的卷会被静默丢弃 —— 这类 bug 极难从结果反推。
2. `OUTLINE_VOLUME_SYSTEM_PROMPT` + `_generate_outline_volume_plans()`：
   每卷一次调用，`volumes` 数组**恰好 1 项**；把"已生成的卷"摘要作为上下文
   传给下一卷（防止相邻卷重复）；章节仍**只给标题**，不写摘要。
3. 每卷按自身章节数单独算 `max_tokens`；任一卷**仍**被截断 → 抛 `output_budget_exhausted`，
   由原有降级分支兜底 —— **绝不会**静默组装半成品。
4. 分卷调用都在原来的 `try` 内，所以配置错误/网络异常照旧走原有降级路径。

### 验证

- 新增 `tests/test_outline_chunked_generation.py`（2 条）：
  ① 未截断 → **只调 1 次**，卷名/弧/章节名正常；
  ② 截断 → 3 次调用（1 次首轮 + 2 次分卷），卷名 `['初火照影','火种归位']`、
    弧 `['丝线初现','噬火者来袭']`、**兜底弧 0 个**、`mode=llm` 不被误判为降级。
- **真实调用分卷路径**（6 章 → 2 卷）：卷名 `['鬼火初燃引路人','幽冥渡口生死桥']`、
  每卷 3 条真弧（「守灵夜惊变」「渡口迷踪」…）、6 个带名字的章节、**兜底弧 0 个**。
- 后端全量 **1455 passed / 0 failed**。

### 仍未处理

- 正文（writing 角色）的 `finish_reason=length` 仍当作可接受结果 —— 截断的正文会照常落库。
- 上千章的极端规模仍待观察：当前按 2–3 卷切分，若**单卷**本身就过大，还需要再细分。

## 2026-10-19 · Unreleased：修复「大纲生成必坏」——规划角色的推理吃光输出预算

> 同一天有**两起**独立故障，根因不同，都已修：①推理吃光预算（下面第一节）；
> ②100 章大纲被 max_tokens 截断、半个 JSON 被当成成品（见「第二起」）。

### 现象与证据

用户报告生成大纲失败，诊断编号 `babfeffb67b1`。审计表 `llm_audit_log` 的对应记录：

| 字段 | 值 |
|---|---|
| `status` | **error** |
| `http_status` | 200（厂商侧请求成功）|
| `finish_reason` | **length** |
| `output_chars` | **0** |
| `total_tokens` | 4535（`max_tokens` = 4096）|
| `duration_ms` | **57967**（58 秒）|
| role / provider / model | `planning` / `arts` / `deepseek-v4-flash` |

推理把 4096 输出预算**全部吃光**，正文 0 字符 → `_parse_outline_reply` 判"返回内容过短" →
**静默降级**成本地模板（接口仍返回 200）。前端只显示"AI 服务暂时不可用"，把真实原因藏住了。

### 根因：`b180bcb` 引入的回归

`b180bcb`（2026-10-18「公网部署前准备」）新增了 `thinking` 机制，并给 `planning` 等 6 个角色
写死 `thinking: "enabled"`。而 `provider.py` 的"ARTS 双保险"只在**完全没有 thinking 策略**时生效：

```python
if (circuit_provider == "arts"
        and "thinking" not in effective_policy
        and "reasoning_effort" not in effective_policy):
    effective_policy.update({"thinking": {"type": "disabled"}, "reasoning_effort": "none"})
```

显式 `enabled` → 跳过双保险 → 推理真正打开 → 预算耗尽。
**该提交之前的版本能正常出大纲，正是因为 planning 根本没有 `thinking` 字段。**

而且这个行为被测试**钉住了**（`test_plot_planning_keeps_an_explicit_thinking_policy`、
`test_arts_respects_role_thinking_in_final_request` 都断言 `thinking == enabled`）——
说明它是一个**有意的设计决定**，只是没考虑 `max_tokens=4096` 这个硬约束。

### 修复

`app/llm/roles.py`：`ROLE_PLANNING` 改为 `thinking: "disabled"`（与 writing / copilot 一致）。

理由是**不是**"推理不好"，而是：大纲是结构化 JSON 输出，用不着推理；而推理长度不可控，
给它留预算等于拿核心功能去赌模型当次思考多长。要开就按角色显式开并同时抬高 max_tokens。

两条钉子按新契约改写并写明理由 —— **不是为了让测试变绿，而是原断言钉住的正是导致这次故障的行为**。

### 验证

- **真实调用**（arts / deepseek-v4-flash / max_tokens=4096）：`mode="llm"`、`error_code=None`、
  6 章、章节名正常（`第1章 撕页的族谱` / `第2章 暗格中的半玉` / `第3章 无脸神像`…），
  审计 `ce89298db6c7`。这也顺带证实了「AI 起章节名」在真实模型下确实生效（此前只有 mock 验证）。
- 后端全量 **1450 passed / 0 failed**。

### 第二起（同日）：100 章大纲被 max_tokens 截断 —— 半个 JSON 被当成成品

作者报告三个症状：章节导航**每卷只有一个「本卷主线弧」**、卷名是「第1卷·起势」这类兜底、
**主线摘要里显示的是一段代码**。

审计 `701654cc2014` 给出真相：

| 字段 | 值 |
|---|---|
| `status` | **success**（错就错在这）|
| `finish_reason` | **length** |
| `output_chars` | 10475（**被截断**）|
| `reasoning_chars` | 0（推理已关，不是第一起那个原因）|

项目里存下来的大纲完整复现三个症状：

```
summary_len = 800                                    ← mainSummary = reply[:800]，即截断的原始 JSON
volumes     = ['第1卷·起势','第2卷·下潜','第3卷·回响']  ← 兜底卷名
arcs        = ['本卷主线弧','本卷主线弧','本卷主线弧']    ← 每卷一个兜底弧
chapter_n   = 100                                    ← 这本书有 100 章
```

**三个缺陷叠加**：
1. 大纲请求**没有自己的 `max_tokens`**，用的是全局 4096 —— 100 章的 JSON 装不下；
2. **截断被当成成功**：`finish_reason=length` 且内容非空 → 直接拿去解析半个 JSON；
3. **解析失败时用 `reply[:800]` 当主线摘要** → 作者看到的"主线摘要"就是原始 JSON。

外加一个放大器：提示词要求每章都给 `summary`，但 `_generate_outline_nodes` **根本不用它**
（章节摘要是由「卷名·弧名」拼出来的）—— 纯属白烧输出预算。

**修法**：
1. `_outline_max_tokens(chapters)`：按章节数给预算（100 章 → 14000，上限 32768）；
2. 提示词的 `chapters` 改为**纯标题字符串**，并明确"不要写摘要"；
3. `generate_outline` 检测 `finish_reason=length` → 抛 `output_budget_exhausted`，
   **拒绝用半个大纲覆盖项目**；
4. `_parse_outline_reply`：「像 JSON 却解析不出 volumes」时**直接报错**，
   不再拿原始回复当摘要（这是"显示代码"的直接原因）。

**真实调用验证（100 章，作者的真实规模）**：`finish_reason=stop`、`output_chars=8295`、
卷名 `['初火照影','冥井深渊','火种归位']`、12 条真弧（「丝线初现」「守夜人陈伯」…）、
100 个带名字的章节（「第1章 鬼火初现」…）、**兜底弧 0 个**、主线摘要为正常散文。

### 同批次一起做掉的（原列在这两起事故的"待办"里）

1. **其余 5 个角色的 `thinking` 一并关掉**（`SUMMARY / CHECK / REASON / STYLE / FULLTEXT`）——
   它们与 planning 是同一类风险：推理吃光预算 → 空摘要 / 核查失败 / 风格分析空转。
   现在 `DEFAULT_ROLE_PROFILES` 里**所有角色统一 `thinking: disabled`**；
   需要给某个角色开推理，走 `llm_configs.role_overrides` 显式开启，并自行承担输出预算。
2. **新增 `output_budget_exhausted` 错误码 + 自动重试一次**：
   `finish_reason=length` 且正文为空、且**确实产生过推理**时，先以 `thinking=disabled`
   自动重试一次（只重试一次）；无推理的纯截断不重试（重试无益，白烧一次调用）。
   前端降级文案改为按 `errorCode` 区分，不再一律说"AI 服务暂时不可用"。

### 仍未处理（需要维护者决定）

1. **超长书的结构性上限**：`_outline_max_tokens` 抬到 32768 仍不够时（例如上千章），
   应改成分卷生成，而不是继续抬 `max_tokens`。
2. 正文（writing 角色）的 `finish_reason=length` 目前仍当作可接受结果 ——
   截断的正文会照常落库。是否要在正文链路也加"截断告警/拒绝"，需要产品权衡。

## 2026-10-18 · Unreleased：登录与注册协议审查修复

- 登录、注册与邀请码登录统一要求明确同意，前后端绑定实际展示的协议版本 `2026-10-18.1`；旧页面提交不再被记为已同意新版。已有会话不在本次变更中强制退出。
- 协议接受字段只认可 JSON 布尔值；注册的版本和时间随账号同次写入，写入失败不遗留账号、不消耗邀请码。
- 阅读协议时只保留一个活动弹窗，修复双重焦点陷阱和背景 inert 失效；返回后表单与勾选状态保留，阅读本身不表示同意。
- 协议补充 Beta 状态、第三方模型的数据流向、注销前置条件与历史备份保留边界，区分小说虚构与冒充事实传播；删除一概免责、承担全部责任及无依据的不退费表述。运营方和邮箱经维护者确认保留。
- 本次仅调整协议及其认证契约和回归测试，不修改正文、章节标题或其他创作功能。此前候选发布包未重新打包，不以旧包审查结果代表当前工作区。
- 本轮限定验证：后端协议及认证相关 7 个测试模块 129 passed；浏览器注册/会话回归 21 passed（API mock）；桌面 1440×960 与窄屏 390×844 检查通过；typecheck 与生产构建通过。真实 LLM 调用 0 次，未重新运行全量测试。
- 补充复核（同日晚，另一轮）：在本条目基础上补跑**全量** —— 后端 **1450 passed / 0 failed**、前端单测 **103 passed**、`tsc --noEmit` 退出码 0、全量 E2E **159 passed / 34 skipped / 0 failed**；运行中的后端已确认加载含 `termsVersion` 的契约，真实库 `users` 已含两列。同时把条款门禁 E2E 的版本号断言从子串 `2026-10-18` 收紧为精确 `版本：2026-10-18.1`（旧写法在版本升级后仍会假通过）。

## 2026-10-18 · 邀请制 Beta 候选验收与可恢复交付（未发布）

- 保持原 provider key；8 库/11 字段 Fernet 加密、读回及独立 VACUUM/文件扫描完成，主密钥仓库外保存，作品数据保留。历史 Git 中同 key 副本仍有效，仓库/历史私有，不强制轮换；不声称抹除外部备份或 SSD 历史。
- 通用 migrate_llm_keys 补齐仅 UPDATE 可留明文残片的问题，以 secure_delete、离线 VACUUM、WAL TRUNCATE/busy 失败和字节测试收口；须停所有访问者，清理失败可能已提交逻辑加密，使用同 master 重跑。末次后端发布结果统一见验收报告。
- 修复项目删除、章节回收合同与参考来源外键迁移，保留个人素材/来源 ID；旧库 227 条历史违规未删，生产首次空库。新锁定 venv 安装/pip check/26 包闭包匹配及依赖审查通过；最末安全收口前全量 1409 passed、0 failed/0 skipped、1 弃用警告，不替代发布复验。
- GET `/api/settings/llm-health` 改为零调用配置快照，管理员点击才 POST，Origin guard/固定脱敏/14 项专项及最终浏览器通过。probe 不绑定项目、不计项目日额度，无全局日金额上限；32 为输出参数、timeout 15 为请求参数，非绝对总耗时。联合鉴权 74 项含 33 项 CSRF 专项通过。
- 最终前端单元 101 passed/0 failed/0 skipped、typecheck/build exit 0（875.75 kB chunk 警告）；最终单轮 E2E 156 passed/34 skipped/0 failed/0 flaky，wrapper 437.22s。早期 152/34/3 与失败项 3/3 复跑保留为历史，不与最终数相加。
- 移除 4 个未用 Tiptap 直接依赖，nanoid 3.3.20/source-map-js 1.2.2 兼容补丁已安装。最终生产 audit 0 high/2 moderate，含 dev 6 high/5 moderate/11 包（包含生产范围）；Vite/Tailwind 主版本升级延后，残余及当前 Router 入口评估见报告，不声称零漏洞。
- Vite 回环/Host 白名单；生产静态 Nginx、构建仅可信源码/CSS/source map。补齐部署/备份/回滚手册；本地副本/新 venv 重启保留数据和相同凭据，health/前端 200、非生产配置 ready 503；Linux/HTTPS/真实生产卷仍待现场。
- rollback journal 两项先 RED、后打包/审查 13 项 GREEN，新增三种 SQLite journal 后缀排除及目录/ZIP 拒绝，审查规则 `release-audit-v2.1`。最终 ZIP/manifest/源提交权威以验收报告及旁置清单为准，不在包内反写自身提交/哈希。
- 两项最终实证通过后候选为 `READY_PENDING_OPERATOR_ACTION`，实际公网未放行；完整 A–P 状态见 `docs/PUBLIC_BETA_FINAL_REPORT.md`。本轮真实模型调用 0；此前 Arts 1 次/9 tokens 仅最小传输，不证明整章全链。

- 修复旧邀请码表补列前创建索引导致的启动失败，补充旧行保留和幂等升级回归。
- 共享多次邀请码限定为注册用途，阻止持码者登录首个注册账号；一次码既有兼容行为保留。
- 已验证项目归属后绑定请求级审计/预算上下文，覆盖审校、去 AI 味、全文问答、风格分析及其他入口；请求、SSE 和后台任务保持项目隔离。
- 修复非原生流式回退重复预留额度；Provider 异常使用固定脱敏提示，不向 SSE/任务返回上游私有 URL。
- 生成草稿与风格计划版本一起保留；导入与建议的迟到响应必须匹配项目、章节及草稿版本。任务页跳章沿用未保存检查，保存在途不允许卸载编辑器。
- 通用 502/503 不再统一误报为 AI 故障；公开候选包排除智能体记忆、工作流及内部历史文档，本地资料保留。
- 打包时生成 `release-build.json`，记录实际源码逐文件 SHA-256 和工作区状态；历史 `release-candidate.json` 不作为当前构建标识。
- 用户单独授权的最小 Arts 请求成功：1 次、0 重试、0 降级、HTTP 200/stop、供应商返回 9 tokens；不等于已验收整章或公网环境，实际金额未知。自动化测试仍禁用真实模型。
- 完整变更分类、操作偏差和验证边界见 `docs/uncommitted-change-audit-20261017.md`；公网部署仍为 `NOT_READY`。

## 2026-10-17 · 公网 Beta 发布收口（未发布）

- 增加生产/Beta 配置失败关闭与公开 `/api/ready` 探针；缺少 HTTPS CORS、Host 白名单、Secure Cookie、管理员或应用加密密钥时不会报告 ready。
- 修复可恢复创作任务在无最新任务时绕过项目 owner 校验的问题；生产 legacy invite 必须映射到真实账号。
- 新增 `backend/scripts/release_audit.py`，对目录/ZIP 拒绝数据库、备份、运行数据、真实环境文件、私钥、浏览器状态和嵌入式密钥，并输出不含秘密的 JSON 审查结果。
- SQLite 备份改用在线 `Connection.backup()`，增加 integrity/foreign-key 校验和隔离恢复验证；新增 `migrate_llm_keys.py`，provider key 可由 `APP_ENCRYPTION_KEY` 加密存储。
- 前端全文问答未知估算费用保持 `null`，不再显示为 0。
- 本轮自动化不调用真实模型；公网 Beta 仍为 `NOT_READY`，请先完成候选包审查、历史凭证轮换、邀请码运营策略和预生产人工门禁。

> 历史日志自 2026-07-31 起保留。最新文档整理：2026-10-04；版本仍为 1.0.0，未发布新版本。旧条目的 PASS、模型配置与费用只代表各自日期和范围。

## 2026-10-18 · Unreleased：章节名（AI 起名 + 作者可改）与登录前的用户协议勾选

> 以下保留原始实施记录与当时的测试结果；协议登录门禁、版本及本轮验证范围以上方“登录与注册协议审查修复”为准。

两个小需求。开工前先做了审计：**大纲起名那部分 codex 已经做完**，所以本轮的改动集中在
"名字进正文"、"作者能改"、"登录前必须勾选同意"这三件事上。

### 一、章节名

**已有（审计确认，非本轮新增）**：`OUTLINE_SYSTEM_PROMPT` 已要求每个 arc 给出 `chapters`
（含 `title`/`summary`），`_arc_chapter_titles()` 解析后拼成「第N章 章节名」；
`chapter_heading.py` 负责多卷书的**全局编号**不被卷内序号覆盖。

**本轮补的三块：**

1. **正文首行写标题行**（作者要求"生成正文时体现"）：`ensure_chapter_heading()` 幂等插入，
   在 `_persist_generated_chapter()` 落库前调用。
   判据刻意收紧为**「标题自带编号」且「编号后还有名字」**才插：
   - 老数据标题就是纯「第N章」→ 原样返回，**不给老项目凭空加行**（会改字数统计）；
   - 无编号的裸标题（夹具/异常输入）→ 同样不动。
2. **作者可手填改名**：编辑器新增「章节名」输入框（`editor-chapter-title`）。
   保存时 `resolveChapterSaveMetadata()` 多收一个 `editedTitle`，**作者手填优先于大纲标题** ——
   否则 AI 原名会在下一次保存时把作者的改名盖回去。保存成功后同时把新名字写回**大纲节点**
   （`PATCH /projects/{id}` 的 `outline` 字段），所以左侧导航会跟着变。
3. **模型不听话时的兜底**：章节名里若仍带「第N章」，先剥前缀再拼，不会出现「第3章 第3章 X」。

### 二、用户协议（免责条款）勾选

| 要素 | 实现 |
|---|---|
| 强制入口 | 注册**服务端强制**（未勾选 400）——**其后已收紧为登录/邀请码登录同样强制**，以上方「登录与注册协议审查修复」为准 |
| 留痕 | `users.terms_version` + `users.terms_accepted_at`（`_ensure_column` 迁移，已在真实库生效）|
| 全文 | 登录**前**就能读：`TermsDialog` 弹层（登录闸门在未登录时挡住整棵路由树，`/terms` 路由到不了）|
| 每次确认 | 闸门每次重新打开都**清空勾选**，同意必须是一次明确动作 |
| 版本号 | `app/core/terms.py` / `frontend/src/services/terms.ts` 两处同值；实施时为 `2026-10-18`，**加固后为 `2026-10-18.1`** |

条款正文按通行写法起草：禁止淫秽色情、危害国家安全、煽动颠覆、暴力恐怖、侮辱诽谤、
虚假信息等；并写明内容责任、违规处理、AI 生成内容的不确定性、免责与条款更新。
运营方与联系方式按作者提供写入（`gmail.com`，作者已确认此前 `gamil` 是笔误）。

### 我引入又回退的两处（如实记录）

1. **把中文数字也当成章节编号**（`第一章` 也被认作"已编号"）→ 静默改变了
   `chapter_heading(1, "第一章")` 的输出，打挂 2 条全量导出测试。**已回退**，编号只认阿拉伯数字。
2. **摘要块/伏笔提示词里的「第N章《标题》」去重**（避免「第3章《第3章 雨夜来客》」）→
   改变了既有文案契约，打挂记忆召回金标准测试。**已整批回退**（`memory_service` /
   `foreshadow_service` 恢复原样）。理由：这两处只是内部提示词，重复一点不影响功能，
   不值得为它改既有契约。

### 验证

**首次实施时**（协议加固之前）：

| 项 | 结果 |
|---|---|
| 后端全量 | **1437 passed / 0 failed** |
| 前端单测 | **103 passed / 0 fail** |
| `tsc --noEmit` | 退出码 **0** |
| 全量 E2E | **157 passed / 34 skipped / 0 failed**（4.4m；跑前重启了前后端，后端同时跑了新列迁移）|
| 真实库迁移 | `users` 已含 `terms_version` / `terms_accepted_at` |

**协议加固之后复核**（在 codex「登录与注册协议审查修复」落地后重跑全量，把该条目声明"未重新运行全量测试"的部分补齐）：

| 项 | 结果 |
|---|---|
| 后端全量 | **1450 passed / 0 failed**（3.1m；加固带来 1437 → 1450）|
| 前端单测 | **103 passed / 0 fail** |
| `tsc --noEmit` | 退出码 **0** |
| 全量 E2E | **159 passed / 34 skipped / 0 failed**（3.3m；跑前重启了前后端）|

新增测试：`test_chapter_heading.py`（标题行幂等/导出去重）、`test_outline_chapter_titles.py`
（AI 起名进标题、不重复编号、缺名回退、宽松输入容忍）、`test_terms_acceptance.py`
（实施时为"注册强制 + 留痕 + 登录仅记录不阻断"；**该文件其后随加固扩展为登录也强制 + 版本回传校验**）、
E2E「条款门禁」用例。

同时更新了既有断言：4 条注册/登录 E2E 的**请求体精确断言**补上 `termsAccepted: true`；
`test_persist_writes_commit_and_projections` 的注释写明"中文数字标题不触发标题行插入"。

复核时又修掉一处**我自己留下的假断言**：E2E 里条款版本号原写成子串 `2026-10-18`，
版本升到 `2026-10-18.1` 后它仍会通过（子串匹配），等于没钉住"用户看到的是哪一版" ——
已改为精确断言 `版本：2026-10-18.1`。**这种"永远绿"的断言比没有断言更危险。**

## 2026-10-15 · Unreleased：消除「每次保存都多拉一次整章正文」+ 章节对账决策抽成纯函数

上一轮 404/进度条修复留下两个已知项，这轮一并处理。

### 一、`chapterNodes` 移出章节加载 effect 的依赖（消除白花请求）

传播链是**逐环查证**过的，不是推测：

```
保存章节 → notifyChapterChanged(action:'save')        chapterEvents.ts:46-49
        → notifyProjectRefresh()                      （同上，内部自动调）
        → projectApi.get() → setOutlineData(新对象)    ProjectWorkspace.tsx:150-161
        → chapterNodes 变成新数组                      MarkdownEditor.tsx:237
        → 章节加载 effect 重跑 → 多一次 GET /chapters/{id}
```

| 路径 | 多余请求 | 说明 |
|---|---|---|
| 每次保存章节 | **1 次** | `setChapterRevision` 只在 restore/recycle 里 +1，保存不碰它 → 这 1 次纯粹是依赖害的 |
| 还原 / 回收 | **2 次** | revision 先变（1 次），项目刷新回来大纲再变（1 次）；两者不在同一 tick，不会合并 |

代价不是"几个字节"：`GET /chapters/{id}` 返回**整章正文**（长章十几 KB），拿回来还会再走一遍
`applyContent` + `markClean` —— 内容相同所以看不出变化，但每次保存都白拉一遍全文。

修法：`chapterNodesRef` + 一个同步 effect，catch 里读 ref 得到"这个节点还在不在大纲里"，
依赖数组去掉 `chapterNodes`（回到改动前的样子）。同文件已有这个模式的先例（`activeProjectRef`）。

**移出依赖后唯一的行为差异**：「大纲变了、但当前章节 id 没变」不再重取正文。已核对会改大纲的
几条路径（导入整批替换 / 还原 / 回收）都会**改变 `currentChapterId`**（走对账）**或 bump revision**，
覆盖没丢；`import-refreshes-navigation.spec.ts` 恰好断言"导入后编辑器刷新"，它绿 = 这条核对成立。

### 二、章节对账决策抽成纯函数 `chapterReconcile.ts`

原来两个相邻 effect（空 id 选首章 / 对账切换）合并成一个 `resolveChapterReconcile()` 决策函数，
消除两者之间"谁先谁后"的隐式耦合。真正要钉住的是**「有未保存正文时绝不自动切换」**
—— 这条会**丢作者的正文**，而它此前**零测试覆盖**。

**为什么不写 E2E**：要构造"当前章 id 不在大纲里"的会话状态得先往 IndexedDB 里塞数据，
而唯一便宜的写法 `page.evaluate(import('/src/stores/index.ts'))` 对 HMR 敏感 ——
上一轮刚被这种写法误导过一轮、白烧两次全量 E2E。为一个覆盖缺口引入新的假失败源不划算。

### 验证

| 项 | 结果 |
|---|---|
| 前端单测 | **94 → 100 passed / 0 fail**（新增 `tests/chapterReconcile.test.mjs` 6 条）|
| `tsc --noEmit` | 退出码 **0** |
| 全量 E2E | **145 passed / 34 skipped / 0 failed**（2.8m；跑前**重启了 dev server**）|

**反向验证命中**：临时摘掉 `hasUnsavedDraft` 守卫 → 「防丢字」那条**立刻变红**，且差异正是
`reason: 'reconcile'`（会切走章节 = 丢字）；再按 sha256 校验原样还原（`e6875e9d…`）后复绿。
即新测试**不是空转**的。

### 重构时特意保留的一处原语义（不借重构改行为）

合并后把「还没选中章节 → 选第一章」**放在脏文本检查之前**，与原第一个 effect 完全一致 ——
原 effect 也没有脏检查，所以没有顺手"改好"它。

### 仍未覆盖（如实记录）

「章节加载 effect 因项目刷新而多拉一次正文」这条**没有专门的回归钉子**：
它的证据是**代码路径核对 + 全量 E2E 全绿**，不是**请求计数实测**。
要钉死的话，得在带保存流程的 E2E 里统计 `GET /chapters/{id}` 次数
（`chapter-trash` 夹具已具备计数能力）—— 但那会引入一条与实现耦合的断言，故未加。

## 2026-10-12 · Unreleased：修正「正文页谎报章节不存在」与「进度条不动」

### 作者的报告

> 新建了项目，正准备创作、**还没有实际创作行动**，正文页就弹出「这一章在服务端已不存在…请刷新页面」；
> 但实际可以创作、也看得到。而且创作好后，正文页面的进度条也不会变化。

### 根因（两处，都已用测试钉住）

**① 404 被一律当成"已不存在" —— 我上一轮引入的误报。**

`GET /chapters/{id}` 的 404 有两种完全不同的成因，**服务端无法区分**：

| 状态 | 含义 |
|---|---|
| 节点在**当前大纲**里、`chapters` 表没有对应行 | **尚未创作** —— 新建项目 / 刚生成大纲时，每个章节节点都是这样（首次保存才用 `PUT` 建行） |
| 节点**已不在**大纲里 | 才是被整批替换 / 移入回收站 |

我 2026-10-04 那轮把 404 一律渲染成「这一章在服务端已不存在」，于是**新建项目一进正文就报错**。
修法：客户端用**自己手里的大纲**做判据 —— 404 且 id 仍在大纲里 → 正常空态（「本章尚未创作」），
不显示任何错误；404 且已不在大纲里 → 才提示「已不在项目里」。

**② 自动创作进度条的分母错了。**

分子是 `deriveCompletedChapterIds()` —— 拿「本地大纲的前 N 个节点」按**位置**推出来的；
分母却是**大纲全长**。于是 `current-to-end` 这类从中途开始的任务**永远到不了 100%**，
看起来就像"创作完成后进度条不动"。修法：改用**作业自己报告的** `completed_chapters` /
`total_chapters`（旧会话回退到原推算值），并抽成纯函数 `autoWriteProgress.ts` 以便单测。

### 顺带确认的两件事（**不是**缺陷，查证后否掉了自己的假设）

- 后端**逐章推送**进度：`_save_job()` 末尾就调了 `_publish_job_update()`，SSE 会推中间进度。
- 前端**从不调用** `POST /chapters/`：保存新章节就是靠 `PUT` 落库 —— 这是**被测试依赖的既有契约**。

### 我试过又完整撤回的一个改动（原批准的"方案 A"收窄）

按计划本想在后端加守卫：`PUT` 遇到不存在的 id 时**拒绝**而不是静默插入。加上去之后
**`test_chapter_trash.py` 立刻红了两条**：

- `test_batch_restore_skips_chapters_that_already_have_content` —— 它保护的正是"删了之后继续写、
  批量还原**不能覆盖**新正文"这个**有意设计**的契约；
- `test_batch_restore_gives_one_chapter_to_only_one_record` —— 同一原章节的两次删除。

即「删了继续写」是**项目有意支持**的行为，守卫与它冲突。**已完整撤回**（`chapters.py` 回到原始字节
8905），改为把一致性诉求落实在**前端对账**上：

> 当前章节 id 若已不在大纲里 → 未保存脏文本时**自动切到第一个有效章节**（不再一直 404，
> 也不再往"大纲之外的幽灵章节"里写）；**有未保存正文时不自动切换**（那会丢字），只保留提示。

### 证据

- 后端新增 `tests/test_vanished_chapter_save.py` **2 条**：① 幽灵 id 保存会**静默创建**出一行，
  但它不在大纲里、而按章节列表又能看到（同一份数据两套事实 —— 正是"进度不变化"的机制）；
  ② 节点在大纲里但从未落库 → 404，**同时钉住 `PUT` 必须仍能创建**。
- 前端新增单测 `tests/autoWriteProgress.test.mjs` **4 条**（含"从中间开始也能到 100%"这条回归钉子）
  → 单测 **90 → 94 passed**。
- **反向验证 2/2 全中**：把 404 判定退回"一律报错" → E2E 用例变红；把进度退回"按大纲推算" →
  单测变红；还原后都复绿。
- `tsc --noEmit` 通过；跑 E2E 前**先重启了开发服务器**（改过 `src/`，HMR 会造成假失败）。

### 我改了一条既有 E2E 的断言（需要你知道）

`import-refreshes-navigation.spec.ts` 原有 `opening a chapter that no longer exists says so...`，
断言"点那个陈旧节点 → 显示已不存在"。**它断言的是真实应用里到达不了的状态**：它点击的节点来自
**服务端返回的大纲**，而章节树就是按当前大纲渲染的 —— 节点在大纲里就说明它没被移除，
那正是"尚未写作"。已按新语义改写为「从未写过的节点 → 空态、不报错」并写明理由。
**这不是为了让测试变绿而放宽断言**：旧断言把两种服务端无法区分的状态混为一谈，
而作者遇到的误报正是这种混淆造成的。

### 本轮我自己犯的错（如实记录）

1. 一度怀疑"后端没推送逐章进度"，读了 `_publish_job_update` 才发现 `_save_job` 末尾已经调了它 ——
   **假设错误，及时否掉，没有去"修"一个不存在的缺陷**。
2. 写补丁脚本时连续三次把校验断言写错：`setChapterLoadError` 期望值数少了一次、
   `autoWriteProgress` 子串检查把**调用点**也算了进去、残留检查把**自己刚写的注释**当成残留。
   三次都是**校验错不是数据错**，且都发生在写盘前（脚本因此没有写入），已逐个收紧。
3. 发现 `MarkdownEditor.tsx` 带 UTF-8 BOM。**先查了 git**：**HEAD 里本来就有**，不是我引入的；
   顺手去掉（其余仓库文件都是无 BOM）。

### 全量回归复核（补记）—— **145 passed / 34 skipped / 0 failed**

改完之后跑全量 E2E 得到过 **143 passed / 2 failed**。我按"大概率是我的改动引入回归"追了一轮，
**结论：是环境噪声，不是回归。** 过程如实记录：

1. **我把失败清单弄丢了**：`test-results/` 每次运行都会被清空，而失败详情只存在那里；
   我为了缩小范围单跑了 `chapter-trash.spec.ts`（15 passed），顺手把证据一起删了。
   这次改成**先落盘再判读**（输出写 `<REPO_ROOT>\_e2e_full.txt`）—— 这个教训保留。
2. 第一次带落盘的重跑给出 **145 failed / 34 skipped / 0 passed**，报错全是
   `page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:5173/` ——
   **开发服务器没起**，与代码无关。
3. 起齐服务（后端 8000 + 全新 dev server 5173）后全量复跑：
   **145 passed / 34 skipped / 0 failed（3.8m）**，与基线完全一致；
   单测 **94 passed / 0 fail**，`tsc --noEmit` 退出码 0。

那 2 条失败的真实成因是 **HMR**：改过 `src/` 之后，`page.evaluate(import('/src/stores/index.ts'))`
会拿到**另一个模块实例**，于是它调的 `setCurrentChapter()` 改的不是应用正在用的那个 store。
全量 179 条里**只有** `style-transition.spec.ts` 的两条用这种写法 —— 恰好就是失败的那 2 条。
（这也解释了为什么它们"单跑绿、全量红"：单跑前我刚重启过服务器。）

排查中**否掉的 3 个假设**（都没有据此去改代码）：

| 假设 | 查证结果 |
|---|---|
| 旧文案「已不存在」被别处断言 | 全仓只有我自己改写的那条用例断言过 → 否掉 |
| 多打一次章节请求被判为异常 | `chapter-trash` 夹具的 `unexpected` 只记**未识别的路径**，`GET /chapters/{id}` 在白名单内 → 否掉 |
| `chapter-trash` 的"扣住章节读取"机制受影响 | 该文件单独跑 **15 passed**；且其夹具里章节 GET **从不 404**，我新加的 404 分支根本走不到 → 否掉 |

**已知但未修的一个小副作用**（等你决定，不主张现在改）：
为了让 404 判定能读「这个节点还在不在大纲里」，章节加载 effect 的依赖里多了 `chapterNodes`，
于是**大纲刷新时会多打一次章节请求**。它不影响任何断言结果，但属于白花的流量。
要消掉的话，可改成用 ref 读最新大纲、把 `chapterNodes` 移出依赖数组。

**仍缺的覆盖**：「对账自动切换章节」这条新路径**没有 E2E 覆盖** ——
它需要在 IndexedDB 里塞一个"当前章节 id 已不在大纲里"的会话状态才能触发。

## 2026-10-12 · Unreleased：全项目文档审计 + 可再生物清理

### 起因

用户要求扩大范围：不只核对与「模型设置」相关的文档，而是按当前项目进度审一遍**全部说明性文件**，
并删除垃圾文件、确认无用的文件与很久前的临时脚本。

### 一、先立规则再动手

两条既有约束和"看到旧就删"是冲突的，必须先在报告里讲清楚：

- `AGENTS.md`：**不得删除数据库、备份、上传、报告或 Git 历史**（除非明确确认）；
- `docs/script-catalog.md` 记着上一轮（2026-09-13）的既定规则：**未证实无用的文件不删** ——
  并且它明确把 `.edge-*` 浏览器资料、`.npm-cache`、`test_artifacts`、历史脚本、父目录压缩包列为"保留"。

所以本轮一切删除都先做**引用计数**（脚本扫描全仓 691 个文本文件，统计"除清理脚本自身外"的引用），
只有「引用数为 0 **且**属于可再生类别」才进白名单。

### 二、校正的文档（12 个文件）

| 文档 | 问题 → 处理 |
|---|---|
| `AGENTS.md` | 验证命令里**没有真正在跑的单测入口** `npm run test:unit`，也没有 `--basetemp`（C 盘紧张时 SQLite 报 `database or disk is full`）和「跑 E2E 前先重启开发服务器」这两条**踩过的坑**；全部补上。另补一条：服务器级模型配置只对管理员开放，前端判定来自 `/api/auth/permissions`，必须与 `require_maintainer` 同源 |
| `docs/README.md` | 旧的 29 行索引里 `reviews/`、`superpowers/`、注册 PRD 全都没列。**重写为文档地图**：当前说明（保持准确）／规格／清单／维护记录／审查报告（冻结）／历史阶段报告（冻结），并写明阅读顺序 |
| `docs/current-status.md` | 标题还写 `2026-10-06`；「已有证据」第一条还是**933 passed**（早已过时）。改为当前基线（后端 1265 / 单测 90 / E2E 145），并写明旧数字只代表各自那一轮、**不能相加** |
| `docs/script-catalog.md` | 漏了 2026-10-07 新增的 3 个运维脚本（`invite_tool.py`、`migrate_legacy_owner.py`、`check_orphan_owners.py`）；补上并追加本轮清理复核 |
| `docs/test-suite-runtime.md` | 历史表停在 2026-09-21，之后 5 轮（参考档案、注册、无障碍与焦点、hook 守卫、管理员可见性）都没行；补齐 |
| `README.md` | 清理记录与文档索引两处链接改为"文档地图 + 两轮清理记录" |
| 5 个活跃文档 | `user-guide`、`chapter-recycle-bin`、`beta-permission-matrix`、`beta-browser-smoke-checklist`、`canonical-state-model` 缺日期标记，补 `更新：2026-10-12`（英文那份用 `Updated:`） |

**核对后确认无需改动**：`docs/beta-environment.md`（"管理员的模型设置权限保留"仍准确）、
`docs/api-auth-audit.md`（不枚举端点）、`backend/experiment_fixtures/` 与 `test-fixtures/`（都存在且被引用）、
`README.md` 的目录树与现况一致。

### 三、清理结果：删除 28 项，约 8.1 MB

- **12 个运行日志**（`backend/longrun-*.log`、`backend/server*.log`、`frontend/vite*.log`，2026-08-11/12）——
  全仓引用数 0，`.gitignore` 本就忽略 `*.log`；
- **3 个 Playwright 产物目录**（`test-results-offline/`、`frontend/test-results/`、`frontend/playwright-report/`）
  —— 跑测试即重建；`test-results-offline` 只是文档里 `--output=` 的**目标**，没有人读它；
- **14 个缓存目录**（`backend/.pytest_cache/` + 13 个 `__pycache__/`）。

删除脚本用**显式白名单 + 路径断言**：目标若落在 `data/`、`test_artifacts/`、`backend/uploads/`、
`test-fixtures/`、`docs/`、`memory/`、`skills/`、`.edge-*`、`.npm-cache/`、`tools/` 之下会直接终止，
且先跑一遍 dry-run 打印体积再执行。

**⚠️ 不可恢复**：`unlink`/`rmtree` 不进回收站，那 12 个日志**已永久消失**（当时的长跑结论早已写在
`docs/qa-synthetic-memory-test-v1.md`、`docs/dev-memory-upgrade-log.md` 里，日志本身只是原始 stdout）。
缓存与测试产物属可再生。

### 四、明确保留（复核后仍不删）

`data/`（24 MB，真实库与备份）、`test_artifacts/`（24 MB 证据，全仓 70 处引用）、
`.edge-profile/` + `.edge-cdp/`（**677 MB**，含浏览器会话数据）、`.npm-cache/`（46 MB，本机网络依赖 VPN）、
`tools/cloudflared-*`（51.7 MB 隧道工具本体）、`scripts/` 与 `backend/_qa_synthetic_memory_test.py`
（`script-catalog` 列为"历史工具：保留"）、`test-fixtures/`（被 `package_source` 测试与 E2E 使用）、
`memory/`、`skills/`、`frontend/tsconfig.tsbuildinfo`、`start.bat`、`release-candidate.json`。
完整依据见 清理记录 2026-10-12。

### 五、留给你决定（我没有擅自删）

1. `.edge-profile/` + `.edge-cdp/`：677 MB。删了能大幅瘦身、并减少隐私数据留在仓库目录里；
   代价是那个 Edge profile 的登录态与书签一起没。已核对 `test_package_source_exclusions.py` 用的是
   **合成目录**，删它**不会让任何测试失败**。
2. `.npm-cache/`（46 MB）：纯缓存，但可能是你网络受限时的离线依赖来源。
3. `tools/cloudflared-*`（51.7 MB）：不再用隧道对外演示就可以删。
4. `backend/.env.bak-20261007-210854`：我 2026-10-07 改 `INVITE_TOKENS` 前的备份，补丁已验证、
   真实 API 也校验过，现在唯一作用是**多留一份密钥明文**。`AGENTS.md` 要求删备份需明确确认，故不动。
5. `data/beta-user-001.before-*.db`（4 个，3.4 MB）：与 `data/backups/` 里的正式备份重复。
6. 仓库根（**项目目录之外**）的 11 个 `NovelCraft-Phase*.zip`：约 150 MB 历史源码包归档。

### 六、验证

- 文档：12 个文件全部**字节级**补丁 + 锚点断言；**60 条本地链接逐条解析通过**；无 BOM、无混合行尾。
- 清理：删除前后各做一次全量清单核对；删除后确认 28 个目标全部消失。
- **本轮未改动任何代码**，`data/` 与真实库未被触碰。

### 我这轮的方法失误（如实记录）

1. 删除/改写脚本第一版用了**长字面锚点**做校验，而**从终端回显复制的文本会被控制台折行**、
   折行处会凭空多出空格 —— 我在这类字符串上已经吃过两次亏。改成**按子串／按行定位**
   （`patch_line` / `insert_before_line` / `append_after_last_table_row`）并断言"匹配到且仅匹配 1 处"后才稳定。
2. `.edge-profile` 的体积我**第一次统计成 10.5 KB**（`Measure-Object -Property Length` 对目录无效），
   改成递归求和后才发现是 **654 MB**。统计方法错了会得出完全错误的结论 —— 差点因此低估了清理收益。

## 2026-10-10 · Unreleased：模型设置页只对管理员可见

### 需求

普通用户看不到「模型设置」，只有管理员账号能看到。

### 现状核实（先查再改）

- 后端**本来就有**管理员机制：`users.is_admin` 列 + `.env` 的 `ADMIN_USER_IDS`，
  `app/core/auth.py` 的 `is_admin_user()`，以及 7 个模型配置接口上挂的 `require_maintainer`
  —— **非管理员访问一律 403**。所以这次补的是**前端缺口**（藏入口 + 挡手输 URL），
  不是"给接口加保护"。这一点很重要：**只藏 UI 不等于访问控制**。
- 真实库只读核对：你的账号 `ec1ec540af85`（用户名 `W`）`is_admin = 1`，
  `fe1e470e77aa`（`TEST`）为 0 —— 正好一个管理员、一个普通用户，可直接当对照。
- 前端类型里**早就声明了** `AuthAccount.isAdmin`，但全仓**从未使用**；`/auth/permissions` 之前不存在。

### 改动

**后端**：新增 `GET /auth/permissions`（`{userId, isAdmin}`），用 `current_user_id` + `is_admin_user`
解析 —— 与 `require_maintainer` **同一套**判定。同时修一处**口径分裂**：`/auth/me` 与登录/注册响应里的
`isAdmin` 原先只读 `users.is_admin`，而 `require_maintainer` 还认 `ADMIN_USER_IDS`；把账号写进 `.env`
名单后会出现「接口放行、菜单却不显示」。现在两处都走 `is_admin_user`。

为什么要新接口：`/auth/me` **只认会话 cookie**，而老 `.env` 邀请码走**请求头**通道 —— 用老码登录时
`/auth/me` 返回 401，前端无从得知这位是不是管理员。会话可用时**不**打这个请求，直接用 `/auth/me` 的结果，
所以既有测试的请求计数不受影响。

**前端**：新增权限 store，**刻意不持久化**（写进 localStorage/IndexedDB 会让上一个管理员账号的
`isAdmin: true` 残留到下一个登录的账号，等于把管理入口永久敞开）；并区分 `null`（还没问过服务端）与
`false`（确定不是）—— 否则管理员进页时会先闪一下「仅管理员可见」。侧边栏 `NAV_ITEMS` 增加 `adminOnly`，
普通用户**根本不渲染**该入口（不是灰掉）；路由加 `RequireAdmin` 守卫，手输 URL 给出明确说明，
**不静默跳回首页**（静默跳转会让人以为链接坏了）。

### 顺带发现的产品问题（已处理）

模型设置页里除了模型配置，还有一个**「导出数据备份」** —— 那是导出**用户自己**的本地数据
（项目、编辑器内容，IndexedDB，已过滤 API 密钥）。整页藏掉，普通用户就**失去了导出自己数据的能力**。
已把它迁到「账户」页（那页本来就是放用户自己的东西：我的邀请码、注销），并在模型设置页留一句指引。
**这是我判断后做的决定，需求里没写**；若你希望普通用户连这个也不要有，说一声即可撤掉。

### 验证

- 新增后端 `tests/test_admin_gate.py` **8 条**（无凭据 401 / 普通账号 false / 数据库标记 true /
  **无会话但持老码 → true** / DEBUG `local-dev` 仍为管理员 / `me` + 登录 + permissions 三处说法一致 /
  普通账号访问模型配置 403 / 管理员 200）；全后端 **1257 → 1265 passed**。
- 新增 E2E `settings-admin.spec.ts` **4 条**（A 管理员可见可进、B 普通用户看不到 + 手输 URL 打不开
  **且一个模型配置请求都不发**、C 普通用户仍能导出自己的数据、D 无会话老码通道判得准）；
  全量 E2E **141 → 145 passed / 34 skipped / 0 failed**（退出码 0，`.last-run.json` 为 `passed`）。
- **反向验证 6/6 全中**（去掉导航过滤 / 守卫恒放行 / 守卫恒拦截 / 不再问 permissions /
  `me` 退回旧口径 / permissions 忽略请求头 → 分别变红，还原后复绿）。
- **真实后端验证 6/6**：无凭据 401；请求头老码 200 且 `isAdmin=true` 并映射到 `.env` 里那个账号；
  请求头通道下 `/auth/me` 401；管理员读模型配置 200；无凭据读模型配置 401；
  `/openapi.json` 里确实有 `/api/auth/permissions`。**改后端必须重启**这条再次被实测到：
  旧进程对新接口返回 404。
- `tsc --noEmit`、`build` 通过。

### 我这轮三个失误（如实记录）

1. **反向验证 RV4 第一次写错了场景**：我在一个"改了也不影响结果"的位置做了改动（后续逻辑仍会正确覆盖），
   于是"没变红"。查下来是**用例错了、不是测试空转**；换成真正破坏遗留通道的改法后立刻变红。
2. **真实后端验证脚本漏了 `/api` 前缀**（把 `/api/auth/permissions` 写成 `/auth/permissions`）→ 全 404，
   我一度以为新接口没生效。修正前缀后 6/6 通过。
3. **全量 E2E 一度出现 2 条失败**（`style-transition.spec.ts` 的两条"迟到响应"）。**不是回归**：
   它们用 `page.evaluate(import('/src/stores/index.ts'))`，而我改过 `stores/index.ts` 触发了 HMR
   （开发服务器日志里有一条更新级联到 **28 个模块**），HMR 会让 `import()` 拿到另一个模块实例。
   **重启开发服务器**后该文件 **28/28** 通过。教训与既有那条相同：**跑 E2E 前先重启开发服务器**。

（`test-results/` 里那 4 个目录**不是**失败留档，而是 `chapter-trash` / `chat-conversations` / `copilot-accept`
里 `page.screenshot({ path: test.info().outputPath(...) })` 主动保存的截图。）

### 文档同步（2026-10-12）

功能落地后回头审了一遍说明性文档，改了 **11 个**（全部字节级补丁 + 锚点断言 + 逐文件编码/行尾校验）：

| 文档 | 改了什么 |
|---|---|
| `docs/user-guide.md` | 开头"左侧入口依次包含项目总览、模型设置和账户"已经过时；新增《模型设置（仅管理员可见）》一节（判定来源、手输 URL、接口 403、「导出数据备份」在账户页） |
| `README.md` | 功能表、`ADMIN_USER_IDS` 一节、"账户与数据"一节共三处仍把模型设置当普通入口，改为只对管理员显示 |
| `docs/api-permission-matrix.md` | 补上缺失的三行：`/api/auth/permissions`（本轮新增）、`/api/auth/invites/mine`、`/api/auth/username-available`（后两个 2026-10-07 那轮就漏了） |
| `docs/production-startup.md` | `ADMIN_USER_IDS` 一行与 §3 说明该名单同时决定管理入口是否显示；补 `…/auth/permissions` 的核对办法 |
| `backend/.env.example` | `ADMIN_USER_IDS` 注释写清它同时控制模型配置权限与 UI 入口 |
| `docs/beta-release-checklist.md`、`docs/beta-deployment-checklist.md` | 新增「非管理员看不到入口 + 手输 URL 打不开 + 接口 403」验收项；前端检查项补 `npm run test:unit` |
| `docs/beta-permission-matrix.md` | 补一句：普通账号的模型设置入口**根本不渲染**，前端判定来自 `/auth/permissions` |
| `docs/ui-design-system.md` | §4.1 导航说明（原文写"导航 2 项"，早已过时）与 §4.4 SettingsPage 版面各加可见性说明 |
| `docs/README.md` | 索引里补上漏掉的 `prd-registration-and-invites.md` |
| `docs/test-suite-runtime.md` | 前端命令块补 `npm run test:unit`（真正在跑的单测入口）；新增《全量套件当前基线（2026-10-12）》 |

**顺带发现的既存缺口（本轮只记录，未擅自扩大范围）**：

- **这次的管理员可见性需求没有 PRD**，只有 CHANGELOG 与当前状态里的事后记录；
- `docs/test-suite-runtime.md` 的历史表最后一轮停在 2026-09-21，之后几轮（10-07 注册、10-09 无障碍、
  10-10 焦点修复 / hook 审计）都没补行 —— 我只补了"当前基线"，**没有回填历史行**；
- `docs/beta-environment.md` 那句「管理员的…模型设置权限保留」仍然正确，未改；
- `docs/api-auth-audit.md` 不枚举端点，无需改动。

**我这轮又一个失误**：写校验脚本时连续两次把断言写成"假定改动位置"—— 一次要求"头部 200 字节不变"
（而改动正好在头部），一次要求"尾部不变"（而改动正好在尾部）。两次都是**校验错、不是数据错**，
但都发生在**写盘之后**，导致补丁流程中断、要重新核对哪些文件已写入。最后改成与位置无关的校验
（新文本必须都在、旧文本必须都不在）+ 逐文件行尾/编码核对才稳定。
另：`docs/ui-design-system.md` 本来就是**纯 CRLF**（其余文档是 LF），我编辑前没先检查这个文件；
事后量过是 501/501 统一 CRLF、0 个孤立 LF，说明编辑没有引入混合行尾 —— 结论是"文件本来就是 CRLF"，
不是被改坏。

### 仍未做 / 待你决定

- **没有可视化发码管理界面**（你之前说并入后期后端管理页）；邀请码仍无有效期/次数上限。
- 普通用户没有任何提示说"模型设置存在但你没权限"——只有手输 URL 才会看到说明（刻意如此：菜单不该出现它）。
- `.env` 的 `ADMIN_USER_IDS` 里还留着历史值 `beta-user-001`，当前库里已无此账号；无害但已过期，
  要不要清理由你决定。

## 2026-10-10 · Unreleased：为「hook 依赖放了不稳定 prop」加静态守卫

### 为什么要用静态守卫，而不是行为测试

审计里那条 `ChapterContextBar` 隐患**当时没有测试** —— 作者要求补一个「假调用点」测试
（构造一个传内联回调的父组件）。我先去核实能不能做，结论是**在本仓库做不了**：

- 前端单测跑的是 Node 内置 runner（`npm run test:unit` = `node --test "tests/*.test.mjs"`），
  **没有 jsdom、没有 @testing-library、没有 vitest/jest**；唯一的测试依赖是 `@playwright/test`。
- 要渲染真实组件并断言「父组件重渲染时子组件 effect 有没有重跑」，必须新增 jsdom + 一个 DOM 测试库
  —— 这是**项目级依赖决策，我没有单方面替作者做**。
- Playwright E2E 也覆盖不到：这个隐患**当前没有任何调用点传 `onStateChange`**，
  页面里根本没有可点、可观察的入口。

所以选了仓库里已有的先例（`tests/apiPaths.test.mjs` 就是「静态守卫 + 自检」），
新建 `tests/hookDepsGuard.test.mjs`：**用 TypeScript AST 静态扫描，并且把「被扫描的源码」做成可注入参数**，
这样检测器自己也能被合成样本测试 —— 否则一个永远返回「无违规」的检测器也能「通过」。

### 这个守卫查什么

两遍扫描，和审计时同一套逻辑：

1. 找出所有「依赖数组里含**本组件 prop**」的 `useEffect` / `useMemo` / `useCallback`；
2. 回到该组件的**调用点**，看这个 prop 有没有被传成内联箭头函数 / 函数表达式 / 对象字面量 / 数组字面量。

**两侧都成立才算违规**。只看第 1 遍会产生大量误报（原始值 prop、`useMemo` 出来的数组、表达式选择都是安全的）；
只看第 2 遍则毫无意义（传内联回调本身不是 bug）。

### 我在这轮自己犯的两个错（如实记录）

1. **检测器一开始有误报**：我把 `Boolean(x)` 这类**调用表达式**也当成「不稳定初始化」，于是
   `MarkdownImport.tsx:644`（`enabled={Boolean(...)}`）和 `ChapterTree.tsx:283`（`open={Boolean(...)}`）
   被误判成 2 处违规。**误报比漏报更危险 —— 它会让人开始忽略这个守卫。**
   已收窄到只认箭头函数 / 函数表达式 / 对象字面量 / 数组字面量。
2. **我的反向验证第一版写错了场景**：我给真实调用点加了 `onStateChange={() => undefined}`，
   期望守卫变红，结果**守卫保持绿色**。我一开始以为是守卫失效，查下来**是测试写错了**：
   `ChapterContextBar` 修完后依赖里已经没有 `onStateChange` 了，**只传内联回调不会导致 effect 重跑，
   它本来就不是 bug**，守卫判绿是**对的**。真正危险的形态是「**依赖里放回这个 prop**」。
   换成正确的反向场景后，3 处全部按预期变红。

### 验证

- `node --test tests/hookDepsGuard.test.mjs` → **4 passed / 0 failed**：2 条自检（检测器能在合成样本上
  真的抓出违规 / 回调来自 `useCallback` 或模块级函数时不算违规）、1 条全仓扫描（0 违规，并带
  「扫描器保底命中数 ≥ 30」防止扫描器整体失效而假绿）、1 条回归钉子（三处历史事故的 prop
  不得再出现在依赖数组里）。
- **反向验证 3/3 全中**：`onStateChange` 放回 `ChapterContextBar` 依赖、`onClose` 放回 `Modal` 依赖、
  `onSelectCharacter` 放回关系图依赖 → 守卫分别变红，还原后全部回绿。
- 前端单测 **86 → 90/90**；相关 E2E 子集（`character-profile` + `registration`）**23 passed / 0 failed**。
- **本轮没有改动任何源码**，只新增了一个测试文件：守卫全绿 + 三处依赖锚点复核 + 单测 90/90。

### 诚实的边界

- **这是静态 lint，不是行为验证。** 它钉的是「代码形态」，不能证明运行时行为；
  运行时行为由 E2E 的 J / K（`page.keyboard.type()` 逐字输入不丢焦点）负责证明。
- 它**抓不到**「依赖里放的是 `useMemo` 出来、但该对象本身会变」这类更隐蔽的形态。
- `ChapterContextBar` 的隐患**仍然没有真实调用点**，所以它依然是「防御性修复」，而非「已修的实际故障」。


## 2026-10-10 · Unreleased：全仓 hook 审计 + 手机端 tab 收进菜单

### 一、全仓 `useEffect`/`useMemo`/`useCallback` 依赖审计

起因：Modal 那个"每打一个字丢焦点"的缺陷属于一类问题（**依赖数组里放了每次渲染都换身份的东西**）。
作者授权全仓审计。

**方法上先做了一次自我更正**：第一版脚本只找"依赖里直接写内联函数"，
而真实缺陷是**依赖里放了 prop（`onClose`）—— 不稳定性在调用方**，被调方看起来完全无辜，
`eslint-plugin-react-hooks` 的 `exhaustive-deps` 也不会报。第一版跑出 `total: 0`，
如果用它就等于**用错的工具得出"没问题"的结论**。改成 TypeScript AST 两遍扫描：
先找"依赖含本组件 prop 的 hook"，再回调用点看该 prop 是否被传内联函数/对象/数组。

**结果：70 处依赖含 prop，真正的缺陷 1 处 + 1 处潜在隐患。**

绝大多数是安全的，理由分三类：原始值 prop（`projectId`/`open`/`isProjectReady`）、
`useMemo` 出来的数组（`previewCharacters`/`elements`）、表达式选择而非新建（`a || b || null`）。

**缺陷 1（已修，用户可见的性能问题）**：`CharacterRelationGraph` 的建图 effect 依赖
`[characters, elements, onSelectCharacter]`，而 `CharacterPanel` 传的是内联箭头函数。
这个 effect **从零创建一个 Cytoscape 实例**（`cose` 布局 800 次迭代），所以父组件每次重渲染都会
`cy.destroy()` 再新建 —— **连点一下节点切换选中都会重建整张图、丢掉平移缩放**，
正好抵消了那段代码注释里"避免点击重建图"的本意。修法：回调走 ref，依赖收成 `[characters, elements]`。

**隐患 2（已修，防御性）**：`ChapterContextBar` 的 effect 依赖含可选 prop `onStateChange`
（目前**没有任何调用点传它**）。它一旦被传成内联箭头，该 effect 会每次渲染都重新拉取
story state 与伏笔列表，而它又回调父组件 `setState` —— 会构成"渲染 → 拉取 → setState → 渲染"的
**无限请求循环**。同样改成走 ref。这一条当时没有测试（没有调用点就无法触发），属防御性修复；现已由静态守卫 `tests/hookDepsGuard.test.mjs` 钉住（见最新一条）。

### 二、手机端把 12 个 tab 收进菜单

`ProjectWorkspace` 有 **12 个 tab**（5 主 + 7 工具），在手机上原本是一条横向滑动条，要滑很久才到
「任务」「伏笔」「统计」。

现在：`lg` 及以上**完全不变**（还是原来的横滑条）；`lg` 以下改成"**当前视图 + 菜单**"按钮
（显示当前 tab 名），点开是一个复用 `Modal` 的两列网格（复用刚做好的焦点陷阱与背景 `inert`），
每项 `min-h-[52px]` 触达高度，选中后切视图并关闭。

**一处既有用例按原意图改走菜单**：`chapter-trash.spec.ts` 的手机段原本点击横滑条上的
`workspace-tab-write`（该用例意图是"在手机上切到正文"），现在改成
`open-tab-menu` → `tab-menu-item-write`。**这是 UI 有意变更，不是削弱断言**。
（另一处 `chat-conversations.spec.ts` 的 `workspace-tab-chat` 点击经核查在**下一个测试**里、
跑在桌面视口，不受影响。）

### 验证

- 前端全量 E2E **139 → 141 passed / 34 skipped / 0 failed**；
  单测 **86/86**；`tsc --noEmit`、`build` 通过。
- 新增 2 条钉子：`人物关系图不会因父组件重渲染而被销毁重建`（给 Cytoscape 的 canvas 打标记，
  切选中后标记必须还在）、`手机端 12 个 tab 收进菜单，桌面端保持横滑条不变`（同一条里同时钉两边：
  桌面横滑条可见且菜单按钮隐藏 / 手机横滑条隐藏 / 菜单里必须是**全部 12 个**）。
- **反向验证 3 处**全部按预期变红后还原：把 `onSelectCharacter` 放回依赖（canvas 被重建）、
  菜单只列 5 个主 tab、横滑条在手机端不再隐藏。

### 我自己在这轮的一个失误（如实记录）

我先把手机菜单的守卫加进了 `novelcraft-smoke.spec.ts` 的 mobile 用例里，
跑出来 `EXIT=0` —— 差点当成通过。**但它显示的是 `2 skipped`**：那个文件整个被
`test.skip(!process.env.NOVELCRAFT_E2E_INVITE)` 跳过（这就是每轮 34 skipped 的来源）。
**一个永远不会跑的测试等于没有测试。** 已撤掉那处改动，把守卫挪到每轮都在跑的
`character-profile.spec.ts`，并确认它真的执行（`12 passed`，该条 1.5s 有耗时）。
教训：`EXIT=0` 不等于通过，**必须看 passed/skipped/failed 的具体数字**。

## 2026-10-10 · Unreleased：修复"输入框每打一个字就丢焦点"（我引入的回归）

### 事故

作者在使用中发现：**登录时每按一次键盘就得重新点一次密码框**。

**根因（我 2026-10-09 的改动引入）**：`Modal` 的 effect 依赖写成
`[open, onClose, dismissible, initialFocusRef, inertBackground]`，而 `onClose` 由调用方传内联箭头函数
（`onClose={() => undefined}`）—— **每次渲染都是新身份**。于是每敲一个字：App 重渲染 → 依赖变化 →
effect 重跑 → 里面的"初始聚焦"把焦点从输入框**抢回第一个可聚焦元素（模式按钮）**。

**为什么我的测试没抓到**（这是本轮最该记住的教训）：我用的是 Playwright 的 `fill()`
——**一次性设值**，只触发一次重渲染，effect 抢焦点发生在设值之后，断言照样通过。
真实用户是**逐字输入**，每敲一个字都会被抢一次焦点。**`fill()` 永远测不出这类缺陷。**

### 修复

1. `onClose` / `dismissible` 改为放进 ref 供事件处理器读取，**不进 effect 依赖**。
2. 那个 effect 的依赖收成 **`[open]`** —— 焦点只在"打开那一刻"移动，重渲染时绝不移动。
   `initialFocusRef` / `inertBackground` 属于"打开时刻"的配置，按打开那一刻读一次即可（这正是弹层语义）。
3. **这是通盘修复**：全站 **12 处** `Modal` 调用点传的都是内联箭头函数，所以它们之前**同样中招**，
   现在一并免疫（不只是登录弹层）。

### 验证

- 新增 E2E `J`（登录模式逐字输入）+ `K`（注册模式逐字输入，字段更多、重渲染更多）：
  用 `page.keyboard.type()` **逐字**输入，断言**焦点始终留在该输入框**且**值完整**。
- 前端全量 E2E **137 → 139 passed / 34 skipped / 0 failed**；单测 86/86；`tsc`/`build` 通过。
- **反向验证**：把依赖改回 `[open, onClose, dismissible, ...]` → **J 与 K 双双变红**，还原后复绿。
  这同时**证明了我的诊断就是作者遇到的那个 bug**，而不是"看起来像"。

### 教训（写给未来的自己）

- **"测试全绿"不等于"能用"**：一次性设值（`fill`）与真实逐字输入是两种不同的路径。
  凡是"输入类"交互，测试必须模拟**逐字的按键序列**。
- **effect 依赖里放函数 = 定时炸弹**：调用方随手写内联箭头是最常见的写法，
  依赖它必然导致 effect 每次渲染都重跑。函数一律走 ref。

## 2026-10-10 · Unreleased：登录弹层改成真表单（回车提交）+ 手机宽度巡检

### 回车提交（Enter to submit）

认证弹层从 `<div>` 改成**真的 `<form onSubmit>`**，主按钮改 `type="submit"`。价值不在"少点一次"：

1. **密码管理器**：浏览器只在检测到真实表单提交时才提示"保存密码" —— 之前没有 `<form>`，很可能根本不提示；
2. 输入框里按回车 = 提交（打完密码顺手回车的习惯）；
3. 键盘可达性。

`method="post"` 是**防御性写法**：万一 `preventDefault` 没生效，凭据也不会落到 URL 上。
模式切换的按钮本来就是 `type="button"`，不会误触发提交；弹层内没有 textarea，所以不存在"回车该换行却被提交"的冲突。

验证：新增 E2E `H`（注册模式在确认密码框回车）+ `I`（登录模式在密码框回车），
前端全量 E2E **135 → 137 passed / 34 skipped / 0 failed**；单测 86/86；`tsc`/`build` 通过。
**反向验证**：把 `<form>` 退回普通 `<div>` → H 与 I **两条都变红**，还原后复绿。

### 手机宽度巡检（一次性，只出报告，不改产品代码）

用真实后端 + 真实数据（**只发 GET**）在 **390×844**（iPhone 12/13/14 逻辑宽度）逐页测量
**页面级横向溢出**（`documentElement.scrollWidth > clientWidth`）：

- 检查 **16 个视图**：项目总览 / 账户 / 模型设置 / 工作区默认页 + 全部 **12 个 workspace tab**
- 结果：**溢出 0 处**
- 目视确认（截图在仓库外 `<REPO_ROOT>\_mobile_shots\`）：正文编辑器在 390px 下满宽可用，
  顶部 tab 条横向可滑，工具栏按钮不挤压
- 凭据用的是 `.env` 里的邀请码（脚本在仓库外、读入内存、不打印不落盘）

**这次巡检没有证明的事**（避免过度解读）：不检查点击目标是否够大、文字是否被截断、
软键盘是否遮挡输入框、以及**数据相关的溢出**（比如超长无空格字符串）—— 只覆盖了一个项目的真实数据。

**顺带发现的一个产品观察（不是 bug）**：工作区共有 **12 个 tab**，在手机上是一条横向滑动条 ——
手机用户要滑很久才能到「任务」「伏笔」「统计」。如果手机端要认真做，
更值得投入的是把这些 tab 收进一个菜单或分组，而不是继续优化滑动条。

## 2026-10-09 · Unreleased：共用组件的无障碍改进（T4 剩余项）

### 改动

**1. `Modal`（全站 12 处调用）**

- 新增 `dismissible`（默认 `true`）。置 `false` 时**不渲染关闭按钮**，Escape 与点击遮罩都不生效
  —— 登录弹层用它，消除"看得见但点了没反应"的死控件。
- **焦点管理**：打开时把焦点送入弹层（优先调用方的 `initialFocusRef`，否则第一个可聚焦元素，
  最后兜底聚焦面板本身）；`Tab`/`Shift+Tab` **焦点陷阱**；关闭时把焦点**还给打开它的那个元素**。
- 打开期间给 `#root` 加 `inert`：Tab 与读屏都不会跑到弹层后面（弹层 portal 挂在 `body` 下，自身不受影响）。
- **StrictMode 防线**：只在"当前焦点不在弹层内"时才记录待还原元素。React 开发环境会双调用 effect，
  否则第二次会把焦点记成弹层内部元素，关闭后焦点无处可还。
- 面板加 `tabIndex={-1}` + `outline-none`（兜底聚焦不显示难看焦点环）。
- **刻意保持不动**：`role="dialog"` 仍挂在外层遮罩、字符串 `title` 仍用 `aria-label` 命名 ——
  `chapter-trash.spec.ts` 依赖 `getByRole('dialog', { name: '章节回收站' })` + 内部「关闭」按钮。

**2. 认证弹层的模式切换语义**：`role="tablist"/"tab"/aria-selected`
→ **`role="radiogroup"/"radio"/aria-checked`**（没有 tabpanel 的 "tab" 是语义撒谎；
单选组才准确描述"三选一"）。

**3. 触达目标**：**只把认证弹层**的三个模式按钮与主按钮提到 `min-h-[44px]`（新用户第一眼、常发生在手机上）。

### 验证

- 前端全量 E2E **134 → 135 passed / 34 skipped / 0 failed**；
  新增 `L`（弹层打开时背景 inert + 焦点被圈在弹层内 + 关闭后焦点还回触发按钮），
  `G` 增补"不可关闭的弹层不得渲染关闭按钮"。
- 单测 **86/86**；`tsc --noEmit`、`build` 通过。
- **反向验证 3 处**全部按预期变红后还原：关掉焦点陷阱 / 去掉 `inert` / 让不可关闭弹层照样渲染关闭按钮。
  **特别记录**：我曾担心"焦点陷阱其实是被 `inert` 掩盖着才通过的"，所以把这一条单独跑 ——
  它确实变红了，证明陷阱**被独立测到**，不是假阳性。
- 迭代时先只跑受影响最广的三个 spec（reference-archive / registration / chapter-trash，**34 passed**）
  再跑全量，避免在白跑里浪费时间。

### 已知取舍（主动记录）

- **弹层打开期间背景里的一切都不可交互** —— 包括右下角错误横幅与 toast（它们在 `#root` 内）。
  这是 `inert` 的正确语义；若希望"弹层开着时提示条仍可点"，需把这两个组件也 portal 到 `body`。
  **这一点目前没有测试覆盖**。
- 全站其它控件密度仍是 ~30–36px（过 WCAG AA 的 24px，但不到 AAA 的 44px）。
  **统一放大属设计系统级改动**，会牵动全站观感，故本轮不做。
- **仍未做 Enter 提交**（T4 的 P1-4）。

## 2026-10-07 · Unreleased：注册/邀请码的两轮独立审查与修复

### 过程

先由**两个独立 Agent** 做只审查不改代码的评审（`ui-designer` 审交互与无障碍、`qa-engineer` 做对抗性测试），
报告已存进仓库：`docs/reviews/2026-10-07-registration-ui-review.md` 与 `...-qa-review.md`。
**我没有直接采信任何一条**：逐条自己读代码/复跑核验后，确认下列缺陷真实存在并修复。

### 修掉的真实缺陷

**用户可见（前端）**

1. 🔴 **邀请码写错 = 静默假登录**：`invite` 模式下任何 401 都被当成"老 `.env` 码缺账号"，
   于是把**错码**写进 `sessionStorage` → 登录闸门被这个错码关掉 → 用户被放进创作台、
   而每个接口都 401。这正是文件开头注释声称要避免的状态。
   现在后端把 `invite_invalid`（403）与 `legacy_account_missing`（401，唯一允许回落）**分开**，
   前端只在后者才回落，其余一律留在弹层如实报错。
2. 🟠 **登录 tab 上有个不生效的「邀请码」输入框**（输入被静默丢弃，还会让人以为"三样都要填"）→ 已删除。
3. 🟠 表单补齐 `label`、`autoComplete`/`name`（密码管理器友好）、字段级错误（`aria-invalid` +
   `aria-describedby` + `role="alert"`），`Input` 新增可选 `errorTestId`。
4. 🟠 账户页：markdown 星号泄漏（`**后 4 位**` 被当字面量渲染）、原始 UTC 时间戳、
   首帧误闪"没有绑定邀请码"、两张卡片无间距 —— 全部修正。

**正确性/安全（后端）**

5. 🔴 **注销留下"死钥匙"**：账号注销后它绑定的邀请码仍停在 `bound` 且指向已消失的账号，
   既登不进也重新注册不了；而体检脚本只扫 `projects`，完全看不见这类孤儿。
   → 注销时同事务作废该账号的码绑定，并把体检扩展为**多表**（projects / reference_works /
   outline_candidates / 悬空邀请码绑定）。
6. 🔴 **绑码抛异常留下僵尸账号**：`bind_invite_to_user` 抛 `sqlite3.Error` 时异常直接冒泡成 500，
   而账号已建出 → 用户以为注册失败、用户名却被占用、码还没被消耗。→ 补异常回滚。
7. 🟠 **老库升级会起不来**：新增的 `lower(username)` 函数式唯一索引在老库存在
   `test`/`TEST` 时会抛裸 `IntegrityError`，后端起不来且毫无提示。→ 建索引前先体检并给出可操作指引。
8. 🟠 **注销漏检用户级资产**：只查 `projects`，`reference_works`/`outline_candidates` 漏掉 → 注销后变孤儿。
9. 🟠 **注册与登录共用一个限流桶**：同 IP 上别人的失败登录会把新用户注册也锁死，
   且 429 文案说的是"登录尝试"。→ 拆成两个独立窗口，文案分别明确。
10. 🟠 **超长输入走框架 422**：detail 变数组，且**回显用户提交的密码明文**。→ 统一由业务层返回 400 字符串。
11. 🟡 **占用查询与登录的首尾空白口径不一致**：表单说"可用"、提交却 409；注册 strip 而登录不 strip
    → 用户按自己输入的样子登不进去。→ 三处统一 `normalize_username`。
12. 🟡 **双真值来源**：同一串码若同时在 `.env` 与库内，注册会把码绑给新账号，
    但用该码登录进的是 `.env` 映射的另一个账号。→ 注册检查也先判老通道并直接拒绝。
13. `production_http_exception_handler` 只改写**字符串** detail，不再把结构化的注册 503/403
    误写成"AI 服务暂时不可用"。

### 验证

- 后端 `tests/test_invites.py` **29 → 40 passed**（新增 11 条钉子，每条对应上面一个缺陷）。
- **反向验证 3 处**（注销不作废码 / 绑码异常不回滚 / 注册回到共享限流桶）全部按预期变红、还原后复绿。
- 前端 `registration.spec.ts` 新增 **G：邀请码写错必须留在弹层且不得写入本地凭据**。
- 真实服务器只读核验：`username-available` / `invites/mine` 在运行实例上行为正确。

### 我自己在这轮的操作失误（如实记录）

1. 一次本想"读代码"的编辑误把已删除的死代码 `delete_user()` 又加了回去（测试正是断言它不该存在）。
   立刻撤销并复跑确认 29 passed。**教训：读和写不要混在同一次调用里。**
2. 我的注册冒烟测试用一次性码注册后又注销账号，**在真实库里留下了一条悬空绑定**
   （`****K7KQ` 仍写着已消失的 `919dc69128fe`）。这正是上面第 5 条的实例，
   清理脚本已就位，稍后连同修复一起收尾。

## 2026-10-07 · Unreleased：账号注册 · 邀请码绑定 · 归属统一

### 需求（作者）

用户可注册账号（邀请码 + 用户名 + 密码），注册后邀请码与这个账号绑定；之后用账号密码登录。
作者决策：Q1=**C+留痕**（码可当登录凭据）/ Q2=A（保留老免密码通道）/ Q3=A（先命令行，管理页后期并入可视化后端）/
Q4=注册即自动登录 / Q5=按建议收紧用户名密码 / Q6=关掉空码开放注册 / Q7=把遗留归属迁到管理员账号。

### 改动

**后端**

1. 新表 `invites`：`id / code_hash / code_hint / label / status / bound_user_id / bound_at / created_at / created_by`。
   **库内只存 `sha256(码)` + 后 4 位 hint**，明文只在生成时出现一次 —— Q1=C 下码就是凭据，泄露即等于账号被拿。
   三态：`pending`（可注册，只能用一次）/ `bound`（进绑定账号）/ `disabled`（都不行）。
2. `invites.bind_invite()` 用 `UPDATE ... WHERE status='pending'` + `rowcount` 做**原子绑码**：并发/重复注册时
   只有一个能成功，这就是"一码一人"的实现保证。
3. `users` 加函数式唯一索引 `lower(username)`；查重与登录都**不区分大小写**（`W` 与 `w` 视为同一账号）。
4. `invite_service`：把所有"这个码现在是什么状态"收在一处（`resolve_credential` 五种结果）。
   老 `.env` 通道**先判**且异常被吞，**邀请码表损坏也不影响老码**。
5. 注册：码必须是 `pending`；用户名/密码规则由服务层校验（密码 7 位返回 **400** 而不是框架默认 422）；
   成功后**自动种会话 cookie**（Q4）。**失败路径不消耗码**（验收 3），
   且绑码失败会**回滚刚建的账号**，不留"注册失败但账号已存在"的僵尸。
6. 登录三路分流：用户名+密码 / `bound` 码 / 老 `.env` 码；`pending` 码返回"请先注册"。
   **`_allowed_invite` 的空配置放行已删除**：`INVITE_TOKENS` 为空 = 拒绝注册（Q6）；
   `DEBUG=true` 的 `local-dev` 兜底保持不变。
7. 新端点：`GET /api/auth/username-available`、`GET /api/auth/invites/mine`（只给 hint 与绑定时间）。
8. `dao/users.py` **删掉死代码 `delete_user()`**（无校验版）—— 它就是历史孤儿 owner 的成因。

**运维脚本**（`backend/scripts/`）

- `invite_tool.py new|list|disable`：发码 / 台账 / 停用。`list` 不含明文（也无法回显）。
- `migrate_legacy_owner.py`：**默认 dry-run**；`--apply` 前**强制备份**且**绝不覆盖已有备份**；
  只 UPDATE 归属列的值（无 INSERT/DELETE）；事务包裹；**幂等**；`user_sessions` 绝不触碰。
- `check_orphan_owners.py`：只报告不修。

**前端**

- 登录弹层改为三模式：**登录 / 注册 / 用邀请码进入**；注册成功自动进入创作台。
- 注册模式下用户名失焦查一次占用（接口失败**不阻塞**提交，退化为服务端 409）。
- 账户页新增「我的邀请码」（只显示后 4 位与绑定时间）。

### 归属统一（真实库，已执行）

`projects.owner_id` 7 行（6 个 `beta-user-001` + 1 个孤儿 `c7836e0fa58a`）与
`outline_candidates.user_id` 19 行已收敛到 `ec1ec540af85`；`.env` 的老码映射也改为指向它，
两条路从此是同一个身份。`user_sessions` 4 条未改动（你不会被踢下线）。
备份：`data/backups/beta-user-001.db.20261007T130839Z.bak`（20MB）。

### 验证

- 后端新增 `tests/test_invites.py` **28 条**；全后端 **1217 → 1245 passed**。
- 前端新增 `tests/e2e/registration.spec.ts` **6 条**；全量 E2E **127 → 133 passed / 34 skipped / 0 failed**；
  单测 **86/86**；`tsc --noEmit`、`build` 通过。
- **反向验证 4 处**（码一次性 / 空码拒绝 / 用户名大小写 / 迁移 dry-run 保证）全部按预期变红、还原后复绿。
- 只读核验 + 真实 API：真实库 `invites` 表与 `idx_users_username_lower` 已建；
  迁移后 8 个项目全部归 `ec1ec540af85`，孤儿报告为"无孤儿"；
  用 `.env` 老码走真实 `GET /api/projects/` 返回 **HTTP 200 且可见 8 个项目**。
- 真实 LLM 调用 **0 次**。

### 这一轮我自己的两个失误（如实记录）

1. 改 `.env` 时用 `partition` 后**忘了把 `INVITE_TOKENS=` 前缀拼回去**，把那一行写成了 `<码>:<用户id>`。
   靠补丁前的备份恢复并重打（事后比对确认**只有这一行**变化、16 行 14 个键全部完整）。
   教训：改配置文件要用"最小替换 + 断言 + 事后 diff"，而不是重排整行。
2. 全量后端套件曾出现 1 条 `MemoryError`（机器只剩 1.2GB 空闲内存，耗时从 200s 涨到 652s）。
   单独复跑该文件 **4 passed / 11.7s**，属环境而非回归。

### 已知 / 取舍

- **老 `.env` 码不能用于注册**（它已有归属账号），发给别人注册会看到"邀请码无效"。
  以后发码一律用 `invite_tool.py new`。
- 用户名可枚举（`username-available` 的固有代价），已落在既有 IP 限流窗口内。
- 邀请码没有有效期/次数上限（当前只有"一次性"）；管理界面留待后期的可视化后端。
- **注册体验好不好只能由你亲手走一遍判断** —— 已生成 2 个测试码（备注「作者自测」）。

## 2026-10-07 · Unreleased：参考书档案 P3b —— 多份参考加权 + 素材库检索

### 需求（作者，接 P3a）

P3a 的「待做」两项：「多份参考加权」「素材库的筛选 / 搜索 / 打标签」。

### 改动

**后端**

1. `reference_bindings` 从「一个项目最多 1 份」升级为**可多份 + 强度**：
   `UNIQUE(project_id, reference_id)`，新增 `weight`（1-10，默认 6）。
   SQLite 改不了 UNIQUE 约束，只能在启动时重建表 —— `_migrate_reference_bindings_multi()`
   检出老形状（`sqlite_master.sql` 里没有 `weight`）后**重建 + 原样搬行（强度给 6）**，幂等。
   这是**我们自己的新表**（真实库 0 行），所以敢做；有数据的老表一律不做重建。
2. `weight` 的真实含义只有一个：**提示词预算不够时先保留谁**。
   `render_reference_blocks()` 按强度降序拼装，总额上限 900 字（单块仍 420 字），
   超预算时**先丢强度低的**；返回 `(文本, 实际参与注入的 [{id,name,weight}])`。
   `render_reference_block()` 保留为兼容包装（合并文本 + 第一份 id）。
3. `set_active_references(project_id, owner_id, [(id, weight)])` 是**整体替换**：
   只要有一条档案不属于该 owner 就**整体拒绝**（返回 `None`），**绝不半途写入**。
   `set_active_reference()` 保留为单份包装。
4. 新列 `reference_works.tags`（`_ensure_column`，默认 `'[]'`）；`sanitize_tags()`
   统一规范化（逗号/顿号分隔的字符串也吃、去空白、去重、单标签截 20 字、最多 12 个、非字符串忽略）。
5. 端点：`PUT /reference-works/active` 改为收 `items: [{referenceId, weight}]`（**整体替换**，
   含未知 id 一律 404 且不写入，空数组=全部停用），响应与列表都带
   `activeReferences: [{id, weight, name}]`（强度降序）+ 兼容字段 `activeReferenceId`（= 强度最高的那份）；
   建档/改名都接受 `tags`。

**前端**

- 每份档案一个开关 + **强度下拉（低 3 / 中 6 / 高 9）**；可**同时启用多份**，
  每次操作都把**完整集合**发给后端（因为后端是整体替换）。
- 列表上方新增「当前启用（N 份）」摘要，按强度从高到低列，并写明「强度只表示优先级：
  预算不够时先丢掉强度低的那些」——不让作者误以为强度是"像不像"的权重。
- 素材库检索：**搜索框**（档案名 / 来源备注 / 标签）+ **标签 chip 多选**（同时具备才命中）
  + 「清除筛选」；纯前端过滤，不发请求；过滤只影响显示，不影响卡片上的任何操作。
- 标签可在建档时直接填，也可在卡片上改（逗号/顿号分隔 → 数组，只 PATCH `tags` 一个字段）。

### 验证

- 后端 `tests/test_reference_works.py` **37 passed**（28 → 37，新增 9 条：多份按强度排序、
  预算先丢低强度、整体替换与清空、一条不属于我就整体拒绝、强度夹取 1-10、
  API 整体替换含 404 不写入、标签规范化与可改、标签归一化边界、老形状绑定表升级且可多份）。
- 全后端 **1217 passed / 0 failed**（1208 → 1217，175.16s）。
- 前端：`reference-archive.spec.ts` **11 passed**（+2：J 多份加权与摘要排序、K 搜索/标签筛选/编辑标签）；
  全量 E2E **127 passed / 34 skipped / 0 failed**（125 → 127）；单测 **86/86**；`tsc --noEmit`、`build` 通过。
- **反向验证 3 处**（注入顺序反转 → 2 条红；未知档案静默跳过 → 1 条红；标签去掉去重 → 1 条红），
  全部按预期变红、还原后复绿；脚本用字节级替换并双换行风格匹配，验证后源文件字节还原。
- 只读核验真实库：`reference_works` 已有 `owner_id` + `tags`，`reference_bindings` 已是新形状
  （含 `weight`，`UNIQUE(project_id, reference_id)`），两张表均 0 行；`style_profiles=1 / style_bindings=1`
  **未被触碰**；运行中的后端 `/openapi.json` 已带 `ReferenceActiveItem{referenceId,weight}` 与
  建档请求的 `tags` 字段。
- 真实 LLM 调用 **0 次**。

### 已知 / 取舍

- **"参考感"到底有没有用，只能由作者用真实付费生成来判断** —— 本轮全是离线测试与只读核查，
  没有跑过一次真实的付费正文生成。
- 强度的界面只有三档（低/中/高 = 3/6/9），后端接受 1-10；不做五档是为了别让作者在
  「3 还是 4」上纠结 —— 它本来只是"预算不够先丢谁"的排序。
- 标签与搜索都是**前端本地**过滤：素材库到几千份时可能需要后端分页 + 服务端搜索（尚未做）。
- 删除项目不会删素材，但**没有 UI 提示**这一点；档案的来源项目名会退化为空（已容忍并测到）。

## 2026-10-06 · Unreleased：参考书档案 P3a —— 变成作者的个人素材库（跨项目共享）

### 需求（作者，接 P2）

「不同的用户会有不同的偏好，让这个成为他们的长期资产，实现个性化沉淀。」

### 改动（只动归属，不动抽取与注入逻辑）

1. `reference_works` 新增 `owner_id`（作者），并对老行**回填**（取来源项目的 owner）；
   档案自此按**作者**归属、**跨项目共享**；`project_id` 退化为「来源项目」标注。
2. DAO 全面改为 owner 作用域：`create(project_id, owner_id, ...)`、`list(owner_id)`、
   `get/update/delete(owner_id, id)`；列表 `LEFT JOIN projects` 带出 `source_project_name`，
   界面上只有在「跨项目」的档案上才显示「来自：《某本书》」。
3. 绑定仍是**项目级**（`reference_bindings`，一个项目最多 1 份）：绑定可以指向**任何**本项目之外的
   档案（个人素材库），删除档案时清理**所有项目**里指向它的绑定。
4. 前端：空态与顶部说明写明「个人素材库（所有书共享）」；`ReferenceAnalysis` 多接一个
   `projectName`（只用于判断该不该显示"来自"）。

**一个刻意的非改动**：本库从不执行 `PRAGMA foreign_keys`，所以 `reference_works` 上那句
`ON DELETE CASCADE` **实际不生效** —— 这正好符合"个人资产不该随项目删除而消失"。
本轮**没有**为此做有风险的表重建（真实库该表 0 行、也无实际触发），而是：
在 `db.py` 写明「刻意不启用、若要启用外键须先移除该声明」，并加**行为测试**钉住
「删项目后档案仍在」（`test_deleting_project_keeps_personal_library`）——将来谁动了这条，测试立刻红。

### 验证

- 后端 `tests/test_reference_works.py` **28 passed**（22 → 28，新增 6 条）：
  跨项目共享（带来源项目名）、跨项目**可启用并注入**（B 项目用 A 项目收的档案 → 上下文出现参考块）、
  别人的素材库看不到（owner 隔离，DAO 级）、删档案清理其它项目里的绑定、
  **删项目保留素材**、老行 `owner_id` 回填（跑真实 `init_db()` 迁移）。
- **反向验证 2 处**：API 列表退回按项目查 → 跨项目用例红；DAO 单条去掉 owner 过滤 → 隔离用例红。
  均已还原并复跑 28 全绿。
- 全量：后端 **1208 passed / 0 failed**（1202 → 1208，202.57s）；前端 E2E **125 passed / 34 skipped / 0 failed**
  （124 → 125，2.7m）、单测 **86/86**、`tsc --noEmit`、`build` 通过。
  记一条抖动：全量 E2E 首跑 1 条失败在 `auth-session.spec.ts` 第一条（等登录表单 10s 超时、
  那条本次耗时 12.7s，属首测冷启动），单独复跑该文件 **7 passed / 5.5s**，非回归。
- 真实 LLM 调用 **0 次**；真实库 `reference_works` / `reference_bindings` 仍为 0 行。

### 未覆盖 / 取舍

- **多份参考加权**仍未做（现在一个项目最多启用 1 份）。
- 素材库没有「按项目筛选/搜索/打标签」——档案多起来之后需要（尚未做）。
- 删除项目不会删素材，但也**没有 UI 提示**这一点；档案的来源项目名会退化为空（已容忍并测到）。

## 2026-10-06 · Unreleased：参考书档案 P2 —— 让 AI 参考它来创作

### 需求（作者，接 P1）

「让用户的 AI 学习参考这个风格来创作」；并且**默认不启用**、**只作表达参考**。

### 改动

**后端**

1. 新表 `reference_bindings`（一个项目最多 1 份，`UNIQUE(project_id)`）——
   **刻意与 `style_bindings` 分离**：参考档案只影响表达参考，不参与本书文风判定与过渡。
2. `reference_work_service` 新增：
   - `set_active_reference(project_id, id)` / `get_active_reference` / `clear_binding`（空 id 即停用）；
   - `render_reference_block(project_id) -> (文本, 档案 id)`：**零额外模型调用**，
     用已存档案的规则 + 本地实测指标拼块，上限 420 字、最多 5 条规则；
     措辞固定为 **「【参考写法（来自《X》，仅作表达参考，不覆盖本书既有风格与设定）】」**；
     任何异常返回空块（注入失败绝不阻塞创作）。
3. 两处注入（**只有作者显式启用时才非空**）：
   - `project_context_service.assemble_project_context` 的 blocks 列表 → 覆盖项目助手/续写等路径；
     该块与 `style` 同类（只是表达参考、不含本书剧情），故 `scoped_prose` 安全集合里也保留它；
   - `novel_generation_service._build_memory_context` 的 `reference_style` → 覆盖**整书自动创作**。
4. 新端点 `PUT /api/projects/{id}/reference-works/active`（`referenceId` 空串即停用，未知档案 404）；
   列表响应附带 `activeReferenceId`；**删除当前启用项时自动停用**（不留悬空绑定）。

**前端**

- 每份档案一个开关：「设为创作参考」/「取消参考」；启用后显示「正在参考」徽章，
  并**把注入措辞原样展示给作者**（`注入到提示词的措辞：「参考写法（来自《X》，仅作表达参考，不覆盖本书既有风格与设定）」`）；
- 顶部边界说明改为：不会改变文风画像、也不会自动套用 —— **除非你显式标记为创作参考（默认不启用）**。

### 验证

- 后端 `tests/test_reference_works.py` **22 passed**（16 → 22，新增 6 条）：
  措辞与内容（含"不覆盖本书既有风格与设定"）、停用即不注入、
  **真实行为验证**（`assemble_project_context` 的 text/blocks 里确实出现/消失 `reference_style`）、
  **整书创作路径**（`_build_memory_context` 的 `reference_style` 出现/清空）、
  API 启用/停用/404、删除启用项自动停用；并继续断言**未碰 style_bindings**。
- 前端 `tests/e2e/reference-archive.spec.ts` **8 passed**（6 → 8，新增启用/停用与边界文案两条）。
- 全量：后端 **1202 passed / 0 failed**（1196 → 1202，196.32s，改动共享上下文组装后无连带回归）；
  前端 E2E **124 passed / 34 skipped / 0 failed**（122 → 124，2.6m）、
  单测 **86/86**、`tsc --noEmit`、`build` 通过。
- **反向验证 3 处**：项目上下文去掉注入块 → 用例红；整书创作上下文去掉 → 用例红；
  措辞去掉「不覆盖本书既有风格与设定」→ 用例红。全部按预期变红后还原并复跑 22 全绿。
  （过程备注：第一轮我的 `-k` 选择器写成 `carries_reference_block and batch_generation`，
  匹配到了批量路径那条用例，于是"破坏未被发现"——是**选择器写错**，不是测试没守；
  改成精确用例名后立即变红。）
- 真实 LLM 调用 **0 次**；未写入 `data/beta-user-001.db`（参考档案表仍 0 条）。

### 未覆盖 / 取舍

- **参考档案不能与文风过渡混用**（刻意）：它不改文风画像、不进过渡计划。
- 只能同时启用 **1 份**（多份加权在 P3；`reference_bindings` 已是 1 行的形状）。
- 注入优先级刻意压低（priority 62，仅高于 `style` 的 60）：预算紧张时先让给事实/大纲。
- 未做真实付费验收：注入是否"读起来像参考"需要作者本人跑一次真实生成来判断。

## 2026-10-06 · Unreleased：参考分析 → 参考书档案（删掉「编造的量化对比」）

### 为什么改（一次诚实性纠正）

旧的「参考分析」页有个**风格量化对比**栏，显示「你的基线：37 / 参考：42」。查证后确认**这组数字是造出来的**：

- AI 路径：prompt 里只放了参考文本，却让模型输出"用户"的四个指标 —— 模型没有任何依据，只能编
  （后端注入的风格上下文只有"名称+语气"，见 `project_context_service.py:392-396`，**没有数字**）；
- 本地兜底路径：`user = reference − 5/8/6/5`，**刻意让作者看起来比参考差一点**。

而产品里**本来就有真实的确定性指标**（`style_engine/extraction.py` 的 `compute_metrics/compute_dimensions`，
零模型调用）。所以这不是"能力不足"，是"明明有真数据却在编"。

作者决定（方案 C）：把它做成**参考书档案**——长期资产、可复用；同时**删掉量化对比**，
并与其严格区分。四个设计点逐条确认：跨项目共享先不做（先项目内，表里预留）、同时只启用一份参考、
默认不启用、拆解成本按需。

### 改动（P1：建档与沉淀）

**后端（新增，不动既有表）**

1. 新表 `reference_works`（`db.py`）：原文 + 拆解结果自存一处，供回看与重新拆解。
   **刻意与 `style_profiles` 分表**——参考档案绝不进 `style_bindings`、不参与本书文风判定与过渡。
2. 新服务 `services/reference_work_service.py`：
   - 指标**必须本地算**（`compute_metrics`），报告里如实标注取用了多少字；
   - 只有「技法规则」用一次 LLM（`extraction.extract_rules`），失败降级为
     `build_rules_from_metrics` + `status='degraded'` + 中文原因；
   - 整本粘贴时按章标题切**伪章节**再均匀采样（走 `extraction.sample_chapters`）；
     单块文本走首/中/尾三截采样，**不往样本里塞原文没有的字**（`sample_chapters` 会补一个"第 N 章"标题）；
   - 原文最多存 30 万字（`MAX_STORED_CHARS`），超出**如实标注** `truncated`；
   - `assert_isolated_from_style_binding()`：供测试断言"参考档案没有泄漏进文风绑定"。
3. 新路由 `api/reference_works.py`：建档 / 列表（**不含原文**，避免几十万字撑爆响应）/ 单条（含原文）/
   改名 / 删除 / **重新拆解**（上次 AI 不可用时重试）。空文本与超长文本 400；未知项目 404。
4. 新 DAO `dao/reference_works.py` + `TABLE_REFERENCE_WORKS` 常量。

**前端（重写 `components/analysis/ReferenceAnalysis.tsx`）**

- 删掉 `styleComparison` 那一栏、它的类型，以及本地兜底里那套造数逻辑（**整个文件重写**）；
- 新页面：建档区（名称 / 来源备注 / 粘贴 / 上传 .txt·.md，前端直接读文件、不经后端上传通道）、
  档案列表（字数、采样量、降级徽章与原因、是否只存了部分原文）、报告（拆解结论 + 五组规则 +
  「本地实测指标」并注明"不经过模型" + 可折叠回看原文）、改名 / 重新拆解 / 删除（二次确认）；
- 页面顶部写明边界：**"它不会改变你项目的文风画像，也不会自动套用到你的正文"**；
- 不再挤助手通道（旧的 `/ai/chat`），改走专用端点 —— 顺带摆脱了"粘贴文本里出现『合并/拆分/重排』
  就被判成改大纲"的误判，也省掉整本书上下文的注入。

### 验证

- 后端新增 `tests/test_reference_works.py` **16 条**（真实 schema 建表、本地指标与采样诚实性、
  存储截断标注、两种降级、重新拆解保留名称、**与文风绑定隔离**、伪章节切分、API 400/404/改名/删除）。
- 前端新增 `tests/e2e/reference-archive.spec.ts` **6 条**（建档请求体、降级如实说明、报告只用本地指标、
  上传 .txt、改名/重拆/删除含取消、列表不拉原文）。其中一条是**绊线**：
  断言页面上不再出现「你的基线 / 风格量化对比 / 参考：」。
- 全量：后端 **1196 passed / 0 failed**（1180 → 1196，202.80s）；前端 E2E **122 passed / 34 skipped / 0 failed**
  （116 → 122，2.7m）、单测 **86/86**、`tsc --noEmit`、`build` 通过。
- **反向验证 3 处**（把报告标题改回「风格量化对比」→ 前端绊线红；建档时顺手写 style_binding →
  隔离断言红；把降级谎报成 completed → 诚实性断言红），全部按预期变红后还原并复跑全绿。
- **真实服务只读核查**：运行中后端已带三条新路由；真实库 `beta-user-001.db` **只读**查出
  `reference_works` 已随启动自动建表（0 条），且既有 `style_profiles=1 / style_bindings=1` 未被触碰。

### 未覆盖 / 取舍（P2、P3 待做）

- **还不能"让 AI 参考它来创作"**：本轮只做到"建档 + 沉淀"（P1）。把某份档案**显式启用**为创作参考、
  注入到生成提示词，是下一步（P2）；默认不会启用，也不会自动套用。
- 参考档案目前**项目内**（跨项目个人库规划在 P3，表里已预留位置，避免将来迁移）。
- 文档里记一条过程事故：本轮编辑 `app/db.py` 与 `app/dao/constants.py` 时，编辑工具在 CRLF 文件上
  写入了 BOM 并把三行常量并成一行（`TABLE_CHAPTER_FTS` 一度变成注释 → ImportError）。
  已用字节级脚本精确修复并复核（AST + 路由 + 全量测试），**没有残留损伤**。

## 2026-10-06 · Unreleased：手写伏笔登记 + 自动追踪默认关

### 需求（作者）

「希望能有一小块地方，作者手动编辑完正文后，他可以把他自己写的伏笔写在这里，然后上传，记录在伏笔账本里。」
四个设计点由作者逐条确认：**入口放「伏笔」页顶部（A）**、**结构化填写 + 可选「让 AI 整理」（C）**、
**埋设章默认当前章 / 回收章默认待定（Q3=按建议）**、**顺带能更新一条已有伏笔（Q4=选 1）**。

### 为什么需要它（补的是哪个缺口）

自动追踪只在**整书自动创作**链路里跑；作者**手改正文**时不会触发，所以「这一章我把某条伏笔回收了」
以前只能去列表里手动改下拉、且没有任何说明可留。这个小块把「登记新伏笔」与「补记状态」放在同一个动作里。

### 改动

**后端（两处，都不改数据结构）**

1. `POST /api/projects/{id}/foreshadows/draft`（新增）：把手写文字整理成伏笔草稿，**不落库**。
   - 走 `planning` 角色（`set_llm_audit_context(project_id, ROLE_PLANNING)` + `llm.chat(role=ROLE_PLANNING)`，
     顺手修掉同类调用「审计里记成 unspecified」的老毛病——suggest/track 两处仍是旧写法，未动）；
   - 提示词明确三条纪律：只算跨章长程承诺、**没写明回收章就填 0（待定）不要替作者猜**、不许编造作者没写的情节；
   - 章序号→章节 id 的解析复用既有机制，但不合法顺序（目标章 ≤ 埋设章）**退回待定**，越界章裁到末章；
   - AI 未配置/失败 → `degraded: true` + 中文原因；AI 答了但没认出伏笔 → `degraded: false` + 说明。
     两种情况都**不抛错、不伪造**。
2. `PATCH /foreshadows/{fid}` 扩展：新增可选 `note` / `chapterId`，写进事件记录（哪一章、谁改的、备注），
   用于「只补记备注、不改状态」的手改正文场景（DAO 本来就支持事件，只是接口没暴露）。

**前端**

- 新组件 `components/foreshadow/ForeshadowManualEntry.tsx`，挂在伏笔页顶部（全文问答块下方）：
  多行表单（名称/说明/埋设章/预计回收章，可「再加一条」/「删掉这条」）、
  「让 AI 整理」输入区（整理结果**只填进表单**，作者核对后点保存才入库）、
  可选「顺便更新一条已有伏笔」（只列还有合法前向迁移的条目，避免提交必被拒绝的选项）、
  「保存到伏笔账本」；成功/失败都在表单内如实显示。
- 埋设章默认 = 编辑器中当前打开的章（读 `useEditorStore.outlineData / currentChapterId`），
  回收章默认「待定」；章节下拉直接来自大纲节点。
- `types` 新增 `ForeshadowDraftResult`；`foreshadowApi.draft()`；`update()` 支持 `note`/`chapterId`。

**行为变更（作者已确认）**

- `FORESHADOW_AUTO_TRACK` **默认改为关**（`config.py` 与 `.env.example` 同步）：自动追踪会占用项目每日
  AI 额度（每章 1 次调用），作者选择「默认省着，想要时再开」。要开启就在 `backend/.env` 写
  `FORESHADOW_AUTO_TRACK=true` 并重启后端。

### 验证

- 后端：`tests/test_foreshadow.py` **63 passed**（54 → 63，新增 9 条：草稿解析/默认当前章/待定/
  非法顺序退回/越界裁剪/两种降级/接口 400/备注+章节事件/只写备注也记事件）；
  `tests/test_novel_generation.py` **25 passed**（接线类用例现在显式打开开关，避免默认关之后变成"空跑也绿"）；
  **全后端套件 1180 passed / 0 failed**（1171 → 1180，558.24s）。
- 前端：`tests/e2e/foreshadow-manual.spec.ts` **5 passed**（首次打开伏笔页的离线夹具，覆盖
  结构化登记 / AI 整理草稿不入库 / AI 不可用时如实说明 / 顺便改状态带备注 / 无名称拒绝提交且不发请求）；
  全量 E2E **116 passed / 34 skipped / 0 failed**（111 → 116，2.4m）、单测 **86/86**、`tsc --noEmit`、`build` 通过。
  记一条抖动：全量 E2E 第一次跑出 115 passed + 1 条失败（`chat-conversations.spec.ts` 某条在
  **点击上 90s 超时**，该文件平时 11.3s 跑完 7 条）；单跑该文件全过、重跑全量复绿，故判为机器抖动。
- **反向验证**：① 让草稿解析不再回落到「当前章」→ 服务层用例红；② 把 AI 失败的原因换成一句
  「已为你整理好草稿」→ E2E 用例红（抓的就是"不许粉饰"）。两次均已还原。
  另记一条诚实的负面结果：曾试图用「忽略 degraded 标记」破坏，E2E **没抓住**——因为那时两条分支
  显示的都是后端给的原因，用户可见结果一致，所以这不是漏网，而是那处破坏本身无害。
- 真实 LLM 调用 **0** 次（全部 AsyncMock / 离线夹具）；`data/beta-user-001.db` 未写入。
- **真实页面只读巡检**（Playwright 连真实后端 + 真实库，只 GET）：手写登记块渲染正常、
  114 个章节下拉可选、**全程零写请求**（脚本断言没有 POST/PATCH/DELETE）。
  巡检顺手抓到一个真实时序问题：刚进伏笔页时「埋设章」是空的（编辑器的当前章晚一步就绪），
  与「默认 = 你当前打开的章」的承诺不符 —— 已修（空白行会跟随晚一步就绪的当前章），
  复巡检确认：先去正文页打开某章再回伏笔页，默认值 = 第 1 章《第一章 坠》。

### 未覆盖 / 取舍

- AI 整理每次点击 1 次 LLM 调用（作者已知情）；不点就零成本。
- 手写登记**不做重复名检测**：作者给两条同名伏笔会各存一条（意图不明，交给作者自己删）。
- 只更新已有伏笔的**状态/备注**，不能在这个块里改描述或回收方式（列表里有专门入口）。
- 计划默认值：一次可多条、描述上限沿用 DAO 的 500 字、不做「待确认」中间态（作者自己写的直接入库）。

## 2026-10-06 · Unreleased：伏笔引擎接线（自动追踪 + 超期提醒进注入块）

### 背景（一次自我更正）

上一轮排查伏笔页时我给作者的说法是「注入那块没接线」。**这个说法不准确**：活跃伏笔清单其实
**早已进入整书自动创作的提示词**——`novel_generation_service._build_active_foreshadows`
把 `planted/progressing` 的伏笔拼成上下文块 `active_foreshadows`（上限 8 条、预算 400 token），
并计入「本次生成已参考：人物 N、故事状态 M、伏笔 K…」那句提示。真正缺的是两件事：

1. **状态不会自动更新**——`update_foreshadows_after_chapter`（服务里写好了、测试也有）**没有任何生产调用方**，
   伏笔只能靠人工点选改状态；作者写着写着，账本必然与现实脱节。
2. **超期信息没进提示词**——注入块只列名字与描述，不说「这条已经该回收了」，
   而 `build_foreshadow_instruction`（内部带超期告警）同样无生产调用方。

作者确认两项都做，故本轮接线（不改数据结构、不加接口、不动前端）。

### 改动（三处，全部后端）

1. **每章落库后自动追踪**（`services/novel_generation_service.py`）
   在 `_persist_chapter_and_sync_memory` 里、正文原子落库成功之后调用
   `foreshadow_service.update_foreshadows_after_chapter(project_id, chapter_id, order, content, outline)`，
   即 `planted → progressing → resolved/abandoned` 由 LLM 读本章正文判定。
   遵守与记忆同步同样的纪律：**只降级、不阻塞**——
   `used_fallback` 的占位章跳过（占位文不代表剧情推进），任何异常只跳过。
2. **追踪的异常降级放宽**（`services/foreshadow_service.py`）
   原先只捕 `LLMConfigurationError / LLMResponseError`，现在改为捕所有异常：
   该函数已进入整书任务主链路，解析/DB 之类的意外异常若冒泡会**让整本书停在某一章**。
3. **超期提醒进注入块**（两个文件）
   - 新增公开 helper `foreshadow_service.overdue_active_foreshadows(project_id, current_order)`：
     按**章序号**（不是章节 id）返回 `[{name, target_order, overdue_by}]`，默认上限 5 条、
     按超期章数降序；判定与既有 `count_overdue_foreshadows` 一致（`当前章 > 预计回收章 + 3`）；
     任何异常返回空列表。
   - `_build_active_foreshadows` 增加 `current_order` 参数：命中超期的伏笔行尾追加
     「（⚠ 已超期 N 章，预计第 M 章回收）」，并在块尾补一句「本章如剧情允许，应优先给出推进或回收线索」；
     调用处传入 `current_index + 1`。**不传 `current_order` 时行为与改前完全一致**（多做零次查询）。

### 成本

- **只在项目存在活跃伏笔时**才会多出一次 LLM 调用（每章 1 次，`max_tokens=1000`，
  正文窗口 2500 字、活跃伏笔上限 12 条）；**0 条活跃伏笔时零调用、零 token**。
- **这次调用会计入项目每日额度**（`PROJECT_DAILY_LLM_CALL_LIMIT=30` / `PROJECT_DAILY_TOKEN_LIMIT=120000`）——
  额度扣减在 `llm.chat` 内部（`project_budget_service.consume_project_budget`），所以追踪与正文生成
  共用同一个池子。实测口径：自动创作 N 章 = 原本的 N 章调用 **＋ N 次**追踪调用。
- **新增开关 `FORESHADOW_AUTO_TRACK`（写在 `backend/.env`）**：置 `false` 即全局关掉追踪，
  伏笔状态退回人工维护。已同步写入 `.env.example`。**注意（2026-10-06 当晚变更）：该开关的默认值
  随后改为 `false`（默认关），原因与验证见本页最上面那条「手写伏笔登记 + 自动追踪默认关」。**
- 超期提醒走的是本来就在拼的注入块，**不额外产生任何模型调用**。
- 真实库现状：8 个项目的 `foreshadows` 表**全部 0 行**，所以这条链路目前一次调用都不会触发。

### 验证

- `tests/test_foreshadow.py` **54 passed**（47 → 54，新增 7 条：超期边界/排序去重上限/异常降级/注入块标注）；
  `tests/test_novel_generation.py` **25 passed**（21 → 25，新增 4 条：接线被调用且参数正确、
  fallback 章不追踪、追踪抛异常仍返回摘要且正文已落库、开关关闭时不追踪）；
  **全后端套件 1171 passed / 0 failed**（1160 → 1171，337.19s）。
- **反向验证 4 例全部按预期变红**（删掉追踪调用 → 接线用例红；关掉超期标注 → 注入块用例红；
  异常收窄回 LLM 专有异常 → 降级用例红；去掉开关判断 → 开关用例红），随后恢复并复跑全绿。
  注：故意删掉追踪调用时「追踪抛异常仍返回摘要」那条**仍然通过**——它守的是未来回归，不是当前接线。
- 未跑真实 LLM（成本），因此「接线有效」由单测 + 静态证据（调用点、参数）证明，
  **不等于**跑过一次付费的真实整书生成。

### 未覆盖 / 取舍

- **只有整书自动创作链路会追踪**：编辑器里的手动续写、单章重写、以及人工编辑正文**都不触发**，
  因此状态仍可能因手改正文而滞后；这是刻意的（避免每次保存正文都调一次 LLM）。
- `build_foreshadow_instruction` 仍无生产调用方：它的能力（活跃伏笔 + 超期告警）
  现在由 `_build_active_foreshadows` 那条更省 token 的路径承担，暂不接入、不删除。
- 开关是**全局级**（改 `.env` + 重启后端），不是项目级、也没有界面；若要界面开关需另加设置接口与迁移。
- 手动录入伏笔已于同日晚些时候实现（见本页最上面那条），所以那条缺口已关闭。

## 2026-10-05 · Unreleased：修「伏笔页永远 Failed to fetch」（前端少了尾斜杠 → 307 跨源失败）

### 现象（作者用浏览器实际打开看到）

「伏笔」页签打开后列表永远显示 `Failed to fetch` + 「重试」；而且它还显示
**「已确认伏笔 0」——这个 0 是假的**，请求根本没成功。编辑器顶栏那条「活跃伏笔」也拿不到数。

### 根因

前端调 `/api/projects/{id}/foreshadows`（**无尾斜杠**），后端路由是 `/foreshadows/`
（`APIRouter(prefix="/projects/{project_id}/foreshadows")` + `@router.get("/")`）→ 返回 **307**；
而 **Location 是绝对地址**（请求经 Vite 代理后 Host 已变成后端自己 `localhost:8000`）→
浏览器直接跨源请求后端端口 → `net::ERR_FAILED` → 前端只看到一句 `Failed to fetch`。

对照：章节一直写成 `/chapters/`（带斜杠），所以同类问题只在这条路上暴露。

**影响面**（同一原因）：`GET /foreshadows`（列表）与 `POST /foreshadows`（**确认建议入库**）双双失效；
`/foreshadows/suggest`、`PATCH|DELETE /foreshadows/{fid}` 不触发重定向，所以「生成建议」和改状态是好的。

### 改动（三处都很小）

1. `frontend/src/services/api.ts`：伏笔列表与创建补上尾斜杠（与 `chapters` 的写法对齐）。
2. `components/foreshadow/ForeshadowPanel.tsx`：加载失败时标题计数不再显示「0」（改为 `—`，
   不再谎报条数）；把网络层失败（`Failed to fetch` 这类零信息量报错）换成一句可执行的话。
3. 新增绊线 `frontend/tests/apiPaths.test.mjs`：扫后端 `app/api/*.py` 的集合根路由
   （`"/"` 还是 `""`），再扫前端 `api.ts` 的调用，**双向**校验尾斜杠一致 —— 漏写或多写都会红。

### 验证

- 单测 83 → **86**（新增 3 条）；`tsc --noEmit`、`npm run build` 通过。
- **真实浏览器实测**（只读巡视，连真实后端）：修前 = `Failed to fetch`；
  修后 = 正常显示「已确认伏笔 0 / 还没有确认的伏笔，点击「生成建议」…」。
- 全量 E2E **111 passed / 34 skipped / 0 failed**（夹具本来就会归一化尾斜杠，所以这次改动对既有用例零影响）。
- 未做「反向验证」的额外一轮：`before/after` 两次巡视用的是同一脚本、同一环境，
  且手工重放带斜杠的请求返回 200、抓到的是 307→`ERR_FAILED` 的确切链路，因果已闭合。

### 说明

- 这类坑只靠人眼守（两个文件、一个字符），所以加了静态绊线；但它不是完整的路由校验
  （不解析 FastAPI 路由表），只覆盖「项目下的集合路径」这一形态。
- 「生成建议」本身没坏，但它后面那步「确认入库」以前一定会失败——修好之前等于白建议。

## 2026-10-04 · Unreleased：审校建议改为「勾选多条 → 一键修改」

### 需求

作者：每条建议下面配一个「修改」按钮，同时允许多选与全选，选好后按一下「一键修改」统一修改。

### 改动（只动审校面板，后端零改动）

- `components/review/ReviewPanel.tsx`：
  - 每条建议下面的「一键修改」改为可勾选的**「修改」开关**（`aria-pressed`，选中后显示「已选」）；
  - 列表上方新增批量工具栏：**全选 / 取消全选**、**一键修改（N）**、以及「已选 x / 可改 y」；
  - 把原来的单条执行抽成 `applyIssueCore(issue)`，批量入口 `handleApplySelected()` 按**列表顺序逐条**调用；
  - 勾选状态按 `issueKey` 记（不是下标），所以**切换严重度筛选不会丢掉选择**。
- **执行语义**（刻意选保守的一档）：
  - 逐条顺序执行，每条都重新从服务端读**当前**正文并重新定位原文 —— 上一条改完后的文字变动自然纳入；
  - 原文不再唯一/找不到时，自动退回「按整章指令改写」（沿用既有候选通道的 owner/版本/原文一致保护）；
  - 中途一条失败就**停下来**，如实报告「已成功 N 条，第 N+1 条失败：<原因>」，
    已成功的保留、不自动回滚，也不静默跳过后面的（失败多半意味着计划冲突或原文已变，
    继续跑会让作者难以判断到底改了哪些）；
  - **撤销以整批为单位**：快照里记的是本批开始前的正文，一次撤销回退整批；
  - 过期（正文在审校后被改过）/未选章/无勾选/执行中：按钮置灰，不给「点了却没反应」。

### 验证

- `tests/e2e/review-panel.spec.ts` 重写并加 1 条：
  - 新增「全选两条 → 一键修改」：断言按列表顺序生成两次候选（第 1 条的 `selectionText` 是原句、
    第 2 条是另一句）、两次接受、两条都标「已应用」、正文两处都落到，
    且**一次撤销把整批改回原正文**；
  - 保留单条路径：勾一条 → 一键修改 → 正文被改 → 撤销回原样；
  - 过期、刷新后仍保留结果这两条既有用例改断言为「单条开关与一键修改都置灰」。
- 全量 E2E **111 passed / 34 skipped / 0 failed**（`--workers=1`）、`tsc --noEmit`、`npm run build` 通过。
- 用例夹具里「汇总文案」不再写死（批量载荷是「建议 1」），改为等第一条建议出现。

### 说明

- 没有做「并行应用」：候选生成与接受本来就是一次一条，并行既不更快，也会让失败归因变乱。
- 也没有做「失败就跳过继续」：见上面语义那段的理由。

## 2026-10-04 · Unreleased：查证「离开一会儿回来，审校结果没了、人被丢回大纲页」

### 现象

作者报告：工作台闲置一段时间（例如去别的窗口办公）后回到页面，**页面像是自己刷新了一次、
并回到「大纲」**；再打开「审校」，**上一次的审校结果已经没有了**。

### 查到的原因（每条都有证据，未凭猜测）

1. **审校结果只活在内存里**：`useReviewStore` 是全项目**唯一没有持久化**的业务 store
   （`nc-projects`、`nc-editor`、`nc-chat`、`nc-settings` 都持久化了），`stores/index.ts`
   里还留着解释：「刻意不做持久化：审校结果与当时的正文强相关…」。于是任何一次整页重新加载
   都会清空它。**这条当初的顾虑其实已经有更精确的机制在兜**：每条快照都带 `contentSignature`，
   正文改过时面板会明确提示「结果可能已过期」并禁用一键修改 —— 保护是重复的，代价却是数据丢失。
2. **工作台页签没有记忆**：`activeTab` 是组件内的 `useState`，挂载时被两个 effect 定成
   `isEmpty ? 'outline' : 'write'`。刷新后 React 状态全丢，页签必然回到默认值。
3. **默认页签还存在一个竞态（生产构建下稳定复现）**：刷新后首帧 `currentProject` 可能仍是空的
   （项目列表要从 IndexedDB 恢复、项目详情要等网络），此时 `isEmpty` 为真，而 `isLoadingProject`
   在同一次 effect 批里还没被置为 true，于是页签被定成 **「大纲」并用 `initialTabSetRef` 锁死**
   —— 哪怕这个项目有上百章正文。**反向验证**：把守卫去掉后在**生产构建**（`vite preview`）上跑
   「项目有正文时应落在正文」的用例 → 失败，`aria-current` 为 `null`（即停在「大纲」）；
   开发模式被 `React.StrictMode` 的 effect 双跑掩盖，所以在 dev 里看不出来。
4. **Vite 一直在监视测试产物目录**：`playwright-report/` 与 `test-results/` 每次跑测试都会被写，
   Vite 判为「项目文件变了」并向**所有**已连接客户端广播一次整页重载。日志实证：
   `[vite] page reload playwright-report/index.html`（每个测试产物文件一次，成串出现）。
   也就是说：**只要有人在这台机器上跑 E2E，作者正开着的工作台就会被刷掉**。

### 本次落地的改动（两条，均最小且可逆）

1. **审校快照落盘**：`useReviewStore` 加 `persist`（`nc-review`，复用既有 storage：
   IndexedDB + localStorage 兼底），快照新增**数量上限 30**（按 `reviewedAt` 淘汰最旧的），
   键/指纹/裁剪规则抽到纯函数模块 `stores/reviewSnapshots.ts`（可被 `node --test` 直接覆盖）。
   审校面板的恢复 effect 改为依赖已订阅的 `snapshot`（而非一次性的 `getState()`），
   以兼容异步存储晚一步才恢复：数据到达时 `reviewedAt`/`contentSignature` 变化，于是恢复一次。
   「旧结果被误用」仍由原有机制兜住（正文改过 → 面板提示「结果可能已过期」+ 禁用一键修改），
   所以不再需要用丢数据来换。
2. **Vite 不再监视测试产物**：`frontend/vite.config.ts` 加 `server.watch.ignored` 忽略
   `**/playwright-report/**` 与 `**/test-results/**`。**验证**：改前每次跑测都会在 dev 日志里
   刷出成串 `page reload playwright-report/...`；改后跑完整套 E2E（同样写入报告），一条都没有。

**验证与证据**：单元测试新增 7 条（快照上限与指纹，83/83 通过）；E2E 新增 2 条
（「整页重新加载后审校结果仍在，且不重复请求模型」「刷新后恢复的结果仍如实标记过期」）→
全量 E2E **110 passed / 34 skipped / 0 failed**、`tsc --noEmit`、`npm run build` 通过。
**反向验证**：把持久化改成空 payload（`partialize` 丢弃快照）→ 上述两条新用例变红，
证明它们真的在盯这件事。

### 仍未修（已定位、有最小复现，单独下一步）

- **页签不会回到离开时那一页**（原因 2）。修法（按项目把页签写 sessionStorage、开机读回）
  已写好并自测通过，但它会与一个**既有竞态**打架：`ProjectWorkspace` 的「初始页签决定」
  跑得比项目数据晚，会把作者在加载期间**手动点**的页签覆盖回去。
  **最小复现**：同一页面里连续 `goto` 项目、每次先点「正文」——带上记忆后
  `copilot-accept.spec.ts` 稳定失败（实测 2/2）；**只关掉记忆即 11 passed**（所以不是持久化的锅）。
  本次只落地了审校结果持久化；修页签记忆得先把「初始决定不得覆盖加载期间的手动选择」做对。
- **默认页签竞态**（原因 3）同理未改：`if (isLoadingProject || !currentProject) return;` 这个守卫
  能修掉它，但会改变「有正文的项目默认落在哪一页」，实测牵动 3 个 spec、7 条用例
  （`character-profile` 因编辑器开始挂载而发出未处理的 `/chapters/c1`、`/projections`；
  `copilot-accept`、`style-transition` 出现章节切换异常），需要连同夹具一起改。

### 测试环境注意（本轮新发现，值得写进测试文档）

- **跑全量 E2E 前先重启 dev server**：开发服务器**发过 HMR 更新之后**，测试里
  `page.evaluate(() => import('/src/stores/index.ts'))` 会拿到与页面**不同的模块实例**
  （应用侧 URL 带 `?t=<revision>`），于是 `setCurrentChapter('c2')` 作用在孤儿 store 上，
  表现为「编辑器没跟着切章」的**假失败**。`style-transition.spec.ts` 的两条「迟到响应」用例
  就是这样红的：**重启 dev server 后立刻跑 → 5 passed**，不重启则稳定 2 failed。
- 报告写入触发的整页重载（上文原因 4）会打断正在跑的用例，表现为一批互不相关的用例同时失败
  （本轮见过一次 7 条）。已随 `watch.ignored` 一并消除。

## 2026-10-04 · Unreleased：修「导入后人物页里没有导入进来的人物」（改为导入后勾选，不自动建档）

### 现象与根因

作者报告：导入之后，「人物」页的角色列表没有跟着更新 —— 书里明明有几十号人，
面板里还是只有建项目时自动生成的那一位。

根因是**两条数据通道没接上**：导入时把识别到的人物写进了 `import_analysis`（给导入页
预览用），却**从未写 `projects.characters`**，而「人物」页读的正是后者。只读查库佐证
（`beta-user-001.db`）：`projects.characters` = 1 条（建项目时生成的「高辰」），
`import_analysis.characters` = 20 条。所以不是界面没刷新，是数据压根没写进那一列。

### 决策（作者拍板）

先按「只增不改地自动补进档案」做了一版，随后作者比对真实数据后改定为 **导入后勾选**：
那 20 个候选里有 **7 个是明显误判**（安静、阳光、明白、塞进、井盖、余光、毛巾），
自动建档等于把垃圾数据变成作者必须清理的负担。同时决定**不动真实库**（不跑回填脚本）。

### 改动

- `app/api/imports.py`：导入**不再写 `projects.characters`** —— 已有项目那条 `UPDATE`
  不再更新该列，新建项目仍写入空数组；候选只留在 `import_analysis` 里。响应把
  `charactersAdded` 换成 **`charactersDetected`**（识别到几位候选，而不是补了几位）。
- 新增 `POST /api/import/projects/{id}/characters/from-analysis`（请求体 `{names: [...]}`）：
  作者勾选后才落库。**只认候选里真实存在的名字**（不在候选里的忽略，一个都不匹配则 400，
  不做静默成功），同名一律保留作者已有档案，返回更新后的整个项目。
  规则复用 `_merge_analysis_characters`，因此「只搬事实、不编造性别/外貌/性格」只有一份实现。
- `components/characters/CharacterPanel.tsx`：新增**导入候选面板** —— 列出候选（姓名 +
  出现次数），可单勾/全选/「加入选中」/「都不是，别再提示」。已经进档案的名字自动从候选里
  消失；「别再提示」按**候选批次签名**记在浏览器本地，重新导入换了一批名字时会重新出现。
- `components/import/MarkdownImport.tsx`：成功提示改为「识别到 N 位人物候选，已列在
  「人物」页，请挑选后加入档案。」；顺带修掉上一轮的漏——**完全替换模式下 `extraNotes`
  被丢掉**，导致合并细节与回收提示在替换时不显示。
- `CharacterPanel` 保留「项目数据里出现的新人物只追加同步」这段兜底（按 id 与姓名去重）。
- `backend/scripts/backfill_import_characters.py` 与 `tests/test_backfill_import_characters.py`
  保留：离线批量回填用（作者已决定本轮**不运行**）。

### 验证

- 后端导入用例 41 → 50：新增/改写 9 条 —— 导入不写人物且只报候选数、新建项目同样不写、
  叉掉的条目不会变成候选、勾选后只加选中的那几位且只带事实、同名不覆盖手写档案、
  候选外的名字忽略（全不匹配 400、空名单 400）、没有导入分析的项目 400、非本人 403 / 未登录 401。
- **反向验证 3 次**（均已还原）：把自动写入改回去 → 3 条红（导入不再写/只加选中/候选外忽略）；
  让「只增不改」失效 → 2 条红（同名保护、脏输入容错）；把候选面板关掉 → E2E 2 条红。
- 后端全量 **1160 passed**（200s）；前端 E2E 串行 **108 passed / 34 skipped / 0 failed**；
  `tsc --noEmit`、`npm run build` 通过。真实 LLM 调用 0 次，真实库未写入。
- E2E `import-refreshes-navigation.spec.ts` 增至 4 条：导入后**只有候选、没有角色卡** →
  勾选后入档（且只带「出现 12 次」这条事实）→ 候选卡自行消失；另加「忽略后切页签回来仍不提示」。
- 已重启后端，并用 `/openapi.json` 确认运行中的进程带上了新路由
  （`/api/import/projects/{project_id}/characters/from-analysis`）。

### 未覆盖 / 说明（诚实记录）

- 真实库**未做任何写入**：`f0ee954f3291 活人的温度` 的 20 个候选仍在 `import_analysis` 里，
  作者可以在「人物」页勾选（建议只勾像人名的那些）。
- `CharacterPanel` 那条「只追加同步」**没有被 E2E 覆盖**（停掉它测试照样通过）：勾选入档走的是
  服务端返回的档案，这段代码只是项目刷新与面板刷新不同步时的兜底，属防御性保留，不算已验证的保障。
- 「别再提示」的记录只存在**浏览器本地**（`localStorage`，按项目 + 候选批次），
  换浏览器/清缓存会重新出现；这是刻意的取舍 —— 为此在数据库里加一个「已忽略」字段
  需要迁移，不值得。
- 识别本身仍是词频/词性启发式，**误判依旧存在**，只是不再自动进档案；要提高识别质量
  得改 `import_service._extract_characters`（例如按「以人名词性出现的占比」过滤），未做。
- `projects.locations` 有**同样的缺口**（15 个识别地点只躺在 `import_analysis` 里），
  但目前没有任何界面读它，所以本轮**故意不写**，免得多出一列没人维护的数据。

## 2026-10-04 · Unreleased：修「导入后点章节导航又变空白」
## 2026-10-04 · Unreleased：修「导入后点章节导航又变空白」

### 现象与根因

作者报告：导入完成后正文能显示，但点左侧章节导航里任意一章，正文又变回
「本章尚未创作，可点击"AI 续写"…」。

服务端日志给出了真相：导航点的是**导入前**的章节 id，而那次导入（真实库 `2026-10-04T03:35Z`
一次 114 章的完全替换）已经把那些章节整批回收重建，于是 `GET /chapters/{id}` 全部 **404**，
编辑器读不到正文又默默保留空内容，界面就把「读不到」显示成了「这一章是空的」。

根因在**前端**：其他章节变更都会调 `notifyChapterChanged` → 发
`novelcraft:project-refresh` 让工作台重新拉项目（大纲、字数都靠它），**唯独导入这条路没发**
—— 而导入恰恰是唯一会整批换掉章节 id 的操作。工作台里渲染导航的 `outlineData` 因此在导入后
仍是旧大纲，只有整页刷新才会更新。

顺带确认：库里是自洽的（只读查库：114 章、大纲 114 个节点、114/114 命中真实章节、
id 无重复），所以问题不在 id 生成或那次修复脚本，而在前端没跟着刷新。

### 改动

- `components/editor/chapterEvents.ts`：抽出 `notifyProjectRefresh(projectId)`，
  并让 `notifyChapterChanged` 复用它（行为不变，只是把这条约定显式命名）。
- `components/import/MarkdownImport.tsx`：入库成功后调用 `notifyProjectRefresh(project.id)`，
  导航随即换成导入后的章节。放在「更新项目」之后、且不受导入面板是否被清空影响。
- `components/editor/MarkdownEditor.tsx`：读不到正文时**如实说明**——404 提示
  「这一章在服务端已不存在：它可能在导入替换时被换掉，或已被移入回收站。请刷新页面…」，
  其他失败说明「加载失败，下面显示的不是该章内容」；占位文案在此时不再说「本章尚未创作」。
  「读不到」和「这一章是空的」是两件事，混为一谈会让人以为正文丢了。

### 验证

- 新增 `tests/e2e/import-refreshes-navigation.spec.ts` 2 条，夹具是**状态化**的：
  导入前项目返回一份陈旧大纲、导入后返回新大纲（与真实服务端一致），
  详情接口只对导入进来的章节返回 200。覆盖：① 导入后导航换成新章节、点开能读到正文；
  ② 点开已不存在的章节时给出诚实提示、且不再出现「本章尚未创作」。
- **反向验证两次**（均已还原）：去掉 `notifyProjectRefresh` 调用 → ① 失败于
  `chapter-tree-item-stale-chapter-id` 仍可见（正是作者遇到的现象）；把 404 提示改回「默默不改」
  → ② 失败。
- 前端全量 E2E **106 passed / 34 skipped / 0 failed**（`--workers=1`，5.3m）、`tsc --noEmit`、
  `npm run build` 通过。本轮**未改后端**，故未重启后端。

### 未覆盖 / 说明

- 手动「移入回收站」后，导航里仍会保留该章节点（这是回收站「还原到原章节」的实现前提），
  所以点它同样读不到 —— 现在会给诚实提示。是否应该在回收时就把节点从大纲里摘掉，
  是产品取舍，本轮未改。
- 全量 E2E 首次以 6 个 worker 并发跑时出现 11 条失败，原因是浏览器 `Target crashed` 与
  90s 超时（本机同时还在跑别的重任务），失败项横跨 auth-session / chat-conversations /
  character-profile 等与本次改动无关的文件；`--workers=1` 复跑 **106 passed / 0 failed**。
  换句话说：本机默认并发度下 E2E 会自相撞车，结论请以串行或低并发为准。

## 2026-10-04 · Unreleased：导入到已有项目时可选「完全替换 / 增量合并」

> 本条跨 10-03 / 10-04：10-03 先做了「写入方式可选 + 按导入顺序对齐」，
> 10-04 按作者要求把对齐改成**文件名里的章节号**并处理旧编号残留，下面写的是最终行为。

### 需求与背景

导入到已有项目此前**只有一种**行为：把该项目的全部章节移入回收站，再写入导入内容。
作者只是改了几章后重导整个文件夹，导入文件里没有的章节同样会被清掉——而且这些记录到了
回收站也点不回来（章节已不在大纲里，批量还原会以 `source_not_in_outline` 跳过）。
现在由作者在导入面板上选。

### 语义

| 情况 | 完全替换（默认，历史行为） | 增量合并（新增） |
|---|---|---|
| 导入里有、项目里已有同一**章节号** | 旧章进回收站，写入新章 | 同左（只处理命中的号） |
| 项目里有、导入里没有的号 | 旧章进回收站（**不留在项目里**） | **原样保留**（逐字不动，含章节 id） |
| 导入里的新号 | 新增 | 新增 |
| 项目字数 / 目标章数 / 大纲 | 按导入内容算 | 按**合并后的全部章节**算 |

对齐依据是**文件名里的章节号**（`第3章 借炉.md` → 第 3 章；`010-雨夜.md` → 第 10 章；
`第2卷 第10章.md` 取的是章号 10 而不是卷号），与章节标题、勾选顺序都无关 —— 所以可以只导
一部分文件。文件名里取不到号（如 `尾声.md`）或号重复时，**整体**退回按导入顺序对齐，
并把依据回传（`mergeKey=number|index`）让界面能明说。合并模式下大纲不重建，
而是按号对齐刷新节点：卷/弧结构节点、以及作者手改过的摘要/关键点/POV/归属卷全部保留，
只有章节节点的 `id`/`title`/字数跟着换。两种方式下被替换的旧正文都会**先完整进回收站**
（含正文与提交历史），因此都可回查；合并进回收站的记录原因标为 `import_merged`。
接口按 `mergeMode=replace|merge` 区分，取值非法直接 400 且**不写任何数据**。

**旧编号残留的处理**（按号对齐的代价）：升级前导入是按**顺序**编号的，文件夹有缺号时同一个文件
会落在别的号上（文件夹是 1、2、4 的曾被编成 1、2、3）。改成按号对齐后，`order=3` 上那一章
就成了重复章节。处理办法是**认出来并一并回收**（不是留着让作者自己找）：
旧编号连续 1..M、本次导入章数相同（M == N）、按名次配对后应叫 n_i 号、且**标题完全一致**
—— 四条同时满足才认定，命中的入回收站并逐条列出；标题改过或章数不同时一律**不动手**，
只在计数里体现（宁可由作者自己判断）。

### 改动

- 后端 `api/imports.py`：`_upsert_import_project` 新增 `merge_mode`；抽出
  `_chapter_outline_node`、新增 `_merge_import_outline`（按号对齐、保留结构节点与手工字段）、
  `_chapter_number`（文件名→章节号）、`_merge_target_orders`（对齐依据与回退）、
  `_residue_orders`（旧编号残影识别，带四条护栏）；两个 ingest 接口接受 `mergeMode`；
  响应新增 `mergeMode`/`mergeKey`/`addedChapters`/`updatedChapters`/`keptChapters`/
  `recycledChapters`/`cleanedChapters`/`cleanedDetails`（`importedChapters` 保持不变）。
- 前端：导入面板新增写入方式选择（**项目为空时不显示**——两种选法结果一样），
  选中「完全替换」时把后果写在按钮下方；入库结果按合并语义如实汇报（新增/替换/保留），
  并按实际情况补充「按导入顺序对齐」或「回收了 N 章旧编号残留」及其明细。
  选择随导入页状态保留（按 projectId），点「清空」回到默认。
- 回收站新增原因文案「导入增量合并时替换」。

### 验证

- 新增后端 16 条（`test_markdown_upload_import.py` 25 → 41）：文件名→章节号解析（含卷号干扰与中文数字）、
  **只导「第03章」时替换的是第 3 章**（按顺序对齐会错改成第 1 章）、旧编号残影被认出并回收、
  标题改过时只保留不回收、无号/号重复时整体退回顺序对齐、残影四条护栏逐条钉住、
  以及此前的合并语义（只替换命中号、未提及章节逐字与 id 保持、旧正文可从回收站读回、
  大纲覆盖全部存活章节且每个节点都能 200 打开、字数与目标章数按合并结果重算、
  默认仍是完全替换、非法取值 400 且零写入、`_merge_import_outline` 的纯函数用例）。
- 新增前端 3 条（`markdown-upload-import.spec.ts` 9 → 12）：选择器与警告可见、所选方式确实
  进了请求体（解析 multipart 字段断言 `mergeMode=merge`）且结果文案与数量/清理明细一致、
  默认发 replace。
- **反向验证五次**（均已还原）：① `replace_all = True`（忽略合并模式）→ 5 条失败；
  ② 大纲恒用导入内容重建 → 「大纲覆盖全部存活章节」失败（被保留章节从导航消失）；
  ③ 前端不带 `mergeMode` → 夹具按 replace 回传，结果文案断言失败；
  ④ 对齐退回「导入顺序」→ 3 条失败；⑤ 关掉残影识别 → 2 条失败。
- **真实数据形态验证**：在真实库的**只读快照**（`VACUUM INTO`，源库不被写入）上对
  《活人的温度》（114 章、回收站 150 条、已修复的大纲）跑完整合并 —— **只导入一个文件**
  `第50章.md`（它在扫描里的顺序永远是 1），17 项断言全过：命中的是**第 50 章**而不是第 1 章、
  第 1 章与其余 112 章逐字与章节 id 全部保持、大纲 114 个节点仍与章节集合完全相等、
  结构仍是 `three_act`、字数与目标章数按合并结果重算、回收站新增 1 条 `import_merged`
  且存的是第 50 章的旧标题；并确认真实库本身未被改动（快照已删除，不留数据副本）。
- 顺手修正：修复脚本的备份文件原先落在 `data_dir`（相对路径），会随执行目录漂到 `backend/data/`，
  容易让人以为「备份过了」却在库里找不到 —— 现改为固定在**数据库旁边**，并把此前那个备份文件挪了过去。
- 后端全量 **1144 passed / 0 failed**（原 1128）；前端全部 E2E
  **104 passed / 34 skipped / 0 failed**（原 101）；`tsc --noEmit`、`npm run build` 通过。
  真实 LLM 调用 0 次；未写入 `data/beta-user-001.db`。后端已重启（PID 11948），
  并用 `/openapi.json` 核对 `mergeMode` 已在生效的请求体里。

  过程备注：前端全量 E2E 与后端全量同时跑时有 2 条失败（`chapter-trash` 的 460 条分片用例与
  导入页子页签用例），机器空闲后单独复跑 **22 passed**、全量复跑 **104 passed** ——
  属并发争抢，不是代码问题。

### 未覆盖 / 待决策

- 文件名里取不到章节号时只能退回**按导入顺序**对齐（此时单独导「第50章」会被当作第 1 章）。
  已写进用户手册；要彻底避开这种情况，需要让文件自带章节号。
- 旧编号残影的四条护栏里有一条是「标题完全一致」。若作者同时改了正文与标题，那一章会被
  归入「保留」而不是「回收」—— 不会丢数据，但需要作者自己删除那一章（面板会报出保留数）。
  想彻底解决要持久化「每章的源章节号」（需要加字段/迁移），尚未做。
- **默认值仍为「完全替换」**（产品方已确认维持现状）。
- 合并模式未做真实浏览器 + 真实后端的联调（后端用例走 `TestClient`，前端 E2E 全部拦截 `/api/**`）。
- 未合并章节数很大时的耗时未单独测量（复用与完全替换同一条回收路径，114 章替换仍在既有实测范围内）。

## 2026-10-03 · Unreleased：修「导入后点开章节全空白」+ 导入页切页签不再丢进度

### 现象与根因（章节全空白）

作者报告：Markdown 全书导入（114 章）后，界面能列出章节，但**点开任何一章正文都是空白**。

根因在 `app/api/imports.py`：章节行是按扫描阶段生成的 `chapter["id"]` 写入 `chapters` 表的，
而生成大纲节点时又用 `gen_id()` **另生成了一个 id**。两套 id 从写入那一刻起就毫无关系，
于是章节导航指向**不存在的章节** —— 前端 `GET /chapters/{node.id}` 拿到 404，正文自然空白。
**作者的正文一个字都没丢**，只是导航接错了线。

证据（只读查真实库 + 作者现场日志）：

| 项目 | 章节行 | 有正文 | 大纲节点 | 节点 id 命中真实章节 |
|---|---|---|---|---|
| `f0ee954f3291`《活人的温度》 | 114 | 114 | 114 | **0** |
| `4f4796394be7`《世界不要崩坏啊！》 | 8 | — | 259 | 5 |
| `3516d040883e`（大纲生成器项目） | 4 | 4 | 3 | 3（健康） |

作者操作时的后端日志与之一致：`GET /chapters/` → **200**，而点开的每个节点 id
（`ae31e2f01892`、`6315b4c307f6`、`e5f96b98d93f` …）→ **404**。

### 改动

- `imports.py` 的大纲节点 id 改为复用真实章节 id（`"id": chapter["id"]`），新导入不再出错。
- 新增 `backend/scripts/repair_import_outline_ids.py` 处理**已落库的历史数据**：
  默认只预览，`--apply` 才写；写入前把旧 `projects.outline` 原样备份到
  `<data_dir>/outline-ids-backup-<时间戳>.json`；**只改 node id**，
  标题/顺序/摘要/正文一律不碰，也不新增或删除节点。
  匹配规则保守：order + 标题完全一致才改；对不上、有二义（同一 order 多条且标题也相同）、
  或目标已被别的节点占用，一律只报告不写入。
- 已对真实库执行一次（仅 `f0ee954f3291`）：改写 **114** 个节点 id，其他项目零改动；
  修复后该项目全部 114 个节点 id 均命中真实章节行。

### 导入页状态（同日第二个需求）

导入面板此前把十几个状态放在组件里，而工作台按 `activeTab === 'import'` 条件渲染 ——
切到别的页签就卸载、回来全部归零。作者只是去大纲页看一眼，回来得重新选文件、
重等一次几十秒的全书扫描（实测 114 章约 47 秒）。

- 状态移到 `frontend/src/components/import/importStore.ts`（zustand，**按 projectId 分开存**）。
- 面板标题右侧新增「清空」：**需要二次确认**（弹出确认条 / 可取消），只有确认才回到第一步；
  没进行过任何操作时不显示这个按钮。
- 刻意**不落 localStorage／sessionStorage**：已选文件是浏览器的 `File` 对象，
  本来就无法序列化，保留一半只会让作者看到「文件还在」却一导入就失败的假象。
  **刷新页面即清空**，并由 E2E 钉住这个行为。
- 扫描/入库加**过期令牌**：点「清空」或在途扫描期间重新发起后，早先那次请求返回时
  不再往界面写（否则刚清空的面板会被旧结果「复活」）。
- Markdown / Word 两个子页签各自保留进度，子页签选择本身也不丢。

### 验证

- 反向验证三次，均按预期变红并已还原：
  ① 把节点 id 改回 `gen_id()` → 后端守卫失败（报「节点 id 指向了不存在的章节」）；
  ② 把过期令牌判定改成恒真 → E2E `a scan that finishes after the clear does not resurrect the panel` 失败；
  ③ 把「读取已存状态」改成恒读空值 → 页面状态用例集体失败（即本次修的原始缺陷）。
- 后端：新增 `tests/test_repair_import_outline_ids.py` 12 条 →
  全后端 **1128 passed / 0 failed**（172.84s，原 1116）。
- 前端：新增 `tests/e2e/import-page-state.spec.ts` 7 条、`tests/importFlowStore.test.mjs` 7 条 →
  全部 E2E **101 passed / 34 skipped / 0 failed**；`tsc --noEmit` 通过。
- 全程 `RUN_LIVE_LLM=0`、真实 LLM 调用 **0** 次；真实库仅在修复脚本中写入一次（旧值已备份）。

### 未覆盖 / 未解决

- **重复导入仍会替换既有章节**：对已有项目再导入一次，会把该项目当前章节全部移入回收站
  （`reason=import_replaced`）再写入新章节。所以「导入修好了」不等于「重复导入是安全的」，
  这仍是一个待决策的产品问题。
- 未对运行中的真实后端做浏览器端到端联调：后端测试用 `TestClient(app)`、前端 E2E 全部拦截 `/api/**`，
  两者都无法发现「运行中的服务是旧的」。
- 开发环境陷阱（非产品缺陷）：在前端跑 `pnpm` 会触发依赖校验、把 21 个真实包挪进
  `node_modules/.ignored`，而开发服务器仍靠预构建缓存正常服务，故障延后到重启/构建时才暴露。
  已还原并在 README 写明规避与恢复办法。

## 2026-10-02 · Unreleased：修「大纲页的结构卡片显示英文」

### 现象与根因

用户报告大纲页「结构」卡片上写着英文（`three-act`）。卡片本来就会按枚举查中文名，
只有**查不到**时才退回原值 —— 也就是说库里的值与前端枚举对不上。
只读查真实库确认：项目 `f0ee954f3291` 存的是 `'three-act'`（**连字符**），
而前端与后端声明用的都是 `'three_act'`（**下划线**）。

根因在后端自己前后矛盾：

| 位置 | 写法 |
|---|---|
| `app/models/schemas.py`（`GenerateOutlineRequest.structure` 的 `Literal`） | `three_act` |
| `app/api/imports.py`（`_build_import_outline` 写入值） | `three-act` ← 违反自身声明 |

所以只有**导入来的**项目会露英文；另外多数项目 `structure` 为 `null`，卡片当时什么都不显示。

### 改动

- 前端新增 `OUTLINE_STRUCTURE_LABELS` 与 `outlineStructureLabel()`（`types/index.ts`）：
  先做拼写归一（连字符→下划线）再查中文名，兼容此前已写入库中的历史值；
  空值显示「未设置」而不留空白；**未知值原样返回**，不猜成某个已知结构。
- `OutlineGenerator` 的选项中文名改为从该映射取（此前选择器与展示各存一份名字，会漂移），
  卡片改用解析函数并加上 `data-testid="outline-structure-label"`。
- 后端 `imports.py` 改回声明写法 `three_act`。**不做数据迁移**：已入库的连字符值由前端兼容显示，
  不动 Beta 库里的既有数据。

### 验证

- 反向验证（两次）：把 `imports.py` 改回 `three-act` → 后端守卫失败
  （`'three-act' 不在声明的枚举 [...] 中`）；把卡片改回旧查表写法 → E2E 失败
  （`Expected: "三幕式结构", Received: "three-act"`），能精确复现用户看到的现象。
- 后端：`test_markdown_upload_import.py` 新增 1 条（断言导入大纲的 structure 落在
  `GenerateOutlineRequest` 声明的 `Literal` 里，取值来源是声明本身）；全后端 **1115 passed**（原 1114）。
- 前端：新增 `tests/outlineStructure.test.mjs` 5 条（四种结构、连字符/大小写/空白归一、
  空值→未设置、未知值不猜）；`test:unit` **69/69**；`markdown-upload-import.spec.ts` 新增 1 条
  （导入后大纲页结构卡显示「三幕式结构」）→ **9 passed**；全部 E2E **94 passed / 34 skipped / 0 failed**；
  `typecheck`、`build` 通过。
- 未调用真实模型；未写入 `data/beta-user-001.db`（排查只用只读连接）。
- 后端已重启（本次改动含后端代码）。

## 2026-10-02 · Unreleased：章节回收站支持多选与批量还原/删除

### 背景

回收站每条记录都要单独点开、单独选目标章节、单独确认，整理一次误删需要点很多次。

### 改动

- 回收站列表加复选框，工具条加「全选 / 取消全选」与「已选 N 条」计数；
  选中后出现「一键还原所选」「一键删除所选」（删除走二次确认，写清共几条）。
- 新增后端批量接口 `POST /{project}/chapter-trash/batch-restore` 与 `batch-delete`，
  一次请求返回逐条结果：
  `batch-restore` → `{restored, skipped:[{trashId,title,reason,message}]}`；`batch-delete` → `{deleted, missing}`。
- **批量还原只回各自的原章节**（`sourceChapterId`）。批量不提供「统一目标章节」：
  多份原稿塞进同一章节会互相覆盖，「还原」在批量语境下只有这一种无歧义含义。
  需要换目标章节时仍走单条还原（那条路径允许任选目标，行为未变）。
- **冲突一律跳过，不覆盖正文**（产品确认的 A 方案）：原章节已不在大纲、原章节已有正文、
  同一原章节被本批多条记录指向、记录已不存在——各自返回稳定的 `reason` code + 可直接展示的中文原因。
  还原成功的从选择中移除，被跳过的**保留选中态**供作者逐条处理。
- 前置条件与单条一致：编辑器有未保存修改时拒绝；创作任务运行中拒绝；
  大纲哈希已变化时**整批零改动**返回 409（不会做一半），刷新后重试。
- 实现上批量刻意复用单条 `restore_chapter` / `permanently_delete`，而不是把事务体抽出来共用：
  单条还原含「位移旧正文 → 插入章节 → 记 commit → 排队并执行投影」一长串精细逻辑，
  改动它的回归面远大于批量本身要解决的问题。代价是逐条事务，因此结果**逐条报告**而非整批回滚。
  投影 runner 声明「never imports an LLM service」，是本地确定性投影，因此成功的还原仍同步完成，
  不会因批量在数据里留下隐藏的半成品。

### 验证

- 后端：`tests/test_chapter_trash.py` 新增 9 条（批量还原回原章节、已有正文跳过且不覆盖、
  原章节离开大纲跳过、同原章节只给一条、未知 id 逐条报告、大纲哈希过期零改动、
  创作中拒绝、批量删除可重复提交、空选择/越权拒绝）；全后端 **1114 passed**（原 1105）。
- 前端：`tests/e2e/chapter-trash.spec.ts` 新增 5 条（多选+全选与跳过原因展示、
  批量删除二次确认与取消不删、未选择时按钮禁用、未保存修改时两条批量都拒绝、
  选择数超过单请求上限时分片汇总）；该 spec **15 passed**，全部 E2E **93 passed / 34 skipped / 0 failed**；
  `npm run typecheck`、`npm run build` 通过。
- 未调用真实模型（真实 LLM 调用 0 次）；E2E 全部拦截 `/api/**`，**不构成真实后端联调**。

### 上线后实测发现的两个问题（已修）

1. **后端未重启导致新接口不可用**。加完批量路由后没有重启后端，运行中的进程仍是旧路由表；
   `POST /chapter-trash/batch-delete` 被 `GET /chapter-trash/{trash_id}` 的路径吸住
   （把 `batch-delete` 当成 `trash_id`），方法不符 → **405 Method Not Allowed**。
   重启后恢复。这也暴露了验收盲区：后端测试用 `TestClient(app)` 拿的是**当前源码**，
   前端 E2E 全部走 mock，**两者都无法发现「运行中的服务是旧的」**。
   已在 README 补上「改后端必须重启」及用 `/openapi.json` 核对生效路由的办法。
2. **单请求 id 上限会让「全选」整体失败**。后端把单请求 id 数限制在 500，而实测某个
   Beta 项目的回收站已有 **484 条**（且每次导入替换都会继续增长）；再涨一点，
   全选就会一次性发出超过上限的 id，被**整个** 422 拒绝。现已改为前端按 200 条分片发送、
   汇总结果；分片间失败时已成功的片会立即反映到本地，未处理的保留选中态供重试。
   核实「未造成数据丢失」的方式：405 由路由层直接产生，应用代码未执行；
   只读查询确认 `chapter_trash` 仍为 484 行。

## 2026-10-02 · Unreleased：后端启动脚本（解释器解析 + 环境变量卫生）

### 背景

本机 PATH 里 `C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps`（Microsoft Store 的应用执行别名）
排在真正的 Python **之前**，因此裸 `python main.py` 会报 `Python was not found`。
另外，临时测试设过的 `NOVELCRAFT_PORT` / `NOVELCRAFT_DB_PATH` / `NOVELCRAFT_DATA_DIR` /
`NOVELCRAFT_RELOAD` 若残留在会话里，会把应用悄悄指向测试库或别的端口，而界面看起来一切正常。

这两件事在重启后端时反复踩到，所以固化成一个脚本。

### 改动

- 新增 `backend/start-backend.ps1`：按优先级显式解析解释器
  （项目内 `.venv` → 同级共享 `.venv` → `py` 启动器 → 常见安装路径），
  启动前清掉上述临时覆盖变量，并从 `backend/` 目录启动（数据目录按进程工作目录解析）。
  支持 `-Port`（仅本次进程）与 `-Reload`。
- 脚本保持**纯 ASCII**：Windows PowerShell 5.1 在无 UTF-8 BOM 时按系统 ANSI 码页读取 `.ps1`，
  中文注释在非英文系统上会被拆碎并导致解析失败；纯 ASCII 对任何编辑工具都安全。
- `README.md` 补充该脚本的用法与两个坑的成因。

### 范围与影响

- **不改任何机器级设置**：不修改 PATH、不关闭应用执行别名、不写注册表、不修改 `.env`。
- 删除该文件并回退 README 段落即可完全撤销。
- 未改应用代码、数据库结构、生成算法与 provider；未调用真实模型。
- 验证：默认路径由脚本实际启动后端并通过 `/api/health`（直连 8000 与经前端 5173 代理均 `{"status":"ok"}`）；
  `-Port` / `-Reload` 只做静态检查（语法解析 0 错误），未用第二实例压真实库。

## 2026-09-28 / 09-29 · Unreleased：文风渐进过渡 v2 前端集成（计划版本＋冲突保护）

### 背景

后端已把「渐进过渡」从「计算出的档位」改成**冻结的计划**：每次设置生成新的
`planId` 与 `revision`，计划内持有源／目标快照与完成账本，写入正文时必须回传
生成时的那一版 token，否则以 409 拒绝，防止用旧计划覆盖新正文。
前端之前按 v1 的 
`plan.step/steps` 结构渲染，且归一化时会把 v2 的 `plan` 重建成旧结构，
导致计划版本、快照与完成数**在前端全部丢失**。本次把前端完整对齐到该契约。

### 改动

- **类型与归一化**：`TransitionPace` 增加 `custom`；`StyleTransition` 区分 v1／v2
  （`schemaVersion` + `legacyReadOnly`），补齐 `planId`／`revision`／`targetChapters`／
  `completedCount`／源与目标快照。`normalizeStyleTransition` 不再重建 `plan`，
  **缺失的 v2 字段保持缺失**，绝不虚构 revision 或完成数。
- **token 语义**（新增 `services/stylePlanToken.ts`）：`stylePlanId` 与 `stylePlanRevision`
  **必须成对下发**——后端 `validate_plan_on_conn` 把「只给一个」也判为冲突（409）。
  未启用 v2 计划时两个字段都不发，保持旧的手工编辑路径。
- **保存正文**：编辑器把 SSE `started` 事件下发的 token 绑定到该份草稿，保存时回传**同一个**
  token；冲突时以 409 拒绝且不覆盖正文。生成被截断（`generation_incomplete`）时
  草稿回滚，token 一并作废。
- **候选**：创建候选前重新 GET 契约并带上当前版本；接受使用候选持久化的 token。
  候选卡显示「风格计划版本 N」／「未启用渐进计划」；陈旧候选**保留**diff 卡与原文，
  给出用户主动的「基于最新计划重新生成」入口，不做自动重试。
- **续写冲突不再本地兜底**：版本冲突时移除本地占位正文注入（其它错误仍走原有降级），
  只显示可执行提示，保留原文与已有草稿。
- **面板**：单一目标预设（9 个 key 原样复用后端 `VALID_PRESETS`）＋文字补充（≤2000）
   ＋自定义周期（整数 1–200）＋快／中／慢（默认 9／60／200 章，提交只发 `pace`，
  默认值由后端决定）。展示原稿／目标／周期／完成数／计划进度。
  表单状态与计划状态分离，轮询、章节事件与迟到响应不得冲掉作者输入；切换项目不会串状态。
- **v1 旧计划只读**：显示「旧版计划，重新设置后启用新进度记录」，
  不显示版本号与完成数，也不自动 PUT 迁移。
- **自动创作冲突**：`checkpoint_type=style_plan_conflict` 的任务展示「计划变化，未保存候选已保留」
  与保留的候选正文；后端已将其标记为不可恢复，前端也不提供恢复入口、不自动恢复。
- **文案**：计划走完只说「计划已完成」，并注明是否达到目标文风以人工判断为准；
  移除「已对齐目标风格」这类夸大表述。

### 测试与范围

- `tests/e2e/style-transition.spec.ts`（新增，全离线拦截 `/api/**`）：**28 passed**。
  覆盖设置载荷（`emotional_resonance` + 文字补充 + 自定义 37 章）、9／60／200 默认值、
  周期边界（1／200 有效，0／负数／201／小数被挡）、刷新后恢复快照与进度且本地存储无完成数、
  v1 只读不报错、无画像／非法维度不白屏、续写 token 绑定与保存回传、
  409 不覆盖正文且不二次调用模型、候选版本展示与陈旧候选保留、计划冲突任务不自动恢复，
  以及迟到分片／迟到保存响应／**同页切换项目**不污染新项目。
- `npm run test:normalize`：**41 passed**（原 34 + 新增 7 条 v2 断言）。
- `npm run test:unit`（新增脚本，跑 `tests/*.test.mjs` 全部）：**64 passed**。
  此前 `chapterMetadata`／`chatRegeneration`／`profile-falsify` 两个单测文件不在任何 script 里，
  属漏跑，已一并纳入。
- 离线 E2E 全量：**88 passed / 34 skipped / 0 failed**（跳过项为 `@live-llm` 与需真实后端的用例）。
- `npm run typecheck`、`npm run build` 通过。
- 范围：**仅前端与文档**。未改数据库、Story State、Memory、生成算法、provider 与预算；
  所有测试为拦截 API 的离线 mock，`RUN_LIVE_LLM=0`，未调用真实模型。

### 行为变更（已确认）

**切换项目会立即中止在途续写**（产品方 2026-09-29 确认）。
此前切项目后旧项目的续写流会继续跑到结束、产出被守卫丢弃，即在 provider 上继续计费；
现在切走即取消，不再计费。切换**章节**不受影响。

## 2026-09-21 · Unreleased：导入结果里的误识别条目可以叉掉（人物/地点/时间线/伏笔）

### 需求

作者反馈：自动识别的人物不够准，希望能自己把「不是人物」的条目去掉。先只做了人物，
随后按作者要求扩到**四类全覆盖**。

四类识别全部基于词频/词性启发式（`pseg.cut` + 词频阈值），误判无法靠调参消干净：
天气会被当成人名、短语会被当成地点、场景句会被当成时间标记、意象会被当成伏笔。
把最终判断权交给作者是合理的。

### 实现（前端本地状态 + 入库时带上名单，**不新增接口**）

- `MarkdownImport`：四个页签里每条右侧加叉号；页签计数实时跟随；
  叉掉后显示「已舍弃 N 项…」并提供**恢复**，恢复**按分类**——
  撤消人物不会把已舍弃的地点也放回来。重新扫描时四类名单一起清空。
- 入库请求新增可选字段 `excludedAnalysis`（JSON 对象，按分类分组）：
  `api.ts` 的 `postMarkdownUpload` / `ingestMarkdownFiles` → 后端 `_apply_analysis_exclusions`。
- **按值（而非下标）过滤**：人物/地点用 `name`，时间线/伏笔用 `text`。
  入库时后端会**重跑**一次分析，按下标对齐过于脆弱。
- 后端**只删不补**：把名单里的条目移除，并**重算摘要里的计数**
  （否则「识别出 12 个角色」与列表里的 11 条自相矛盾）。
  抽取摘要文案为 `build_analysis_summary()`，扫描与过滤共用同一处模板。
- 脏名单一律忽略：空串、非法 JSON、非对象、值不是数组、条目不是字符串
  （如 `[123]`）、不认识的分类——都不改动结果、也不留痕迹。
- 叉号在导入成功后**置灰**：分析结果已落盘，再改列表就会与库里不一致
  （提示「已导入，如需调整请重新扫描」）。

### 测试与范围

- 后端 `test_markdown_upload_import.py`：**23 passed**（5 条舍弃相关：
  四类只删不补 + 计数重算、只给一类时其它类不被误伤、脏名单不改结果、
  脏条目不抛错、端到端入库带名单）。
  「只删不补」那条还抳出了一个真问题：`[123]` 原本会被当成名字，现已收紧为只接受字符串。
- 前端 `markdown-upload-import.spec.ts`：**7 passed**（新增 1 条：四类逐个叉掉→计数变化→
  **分类恢复**→入库请求带 `excludedAnalysis`→入库后叉号禁用）；`tsc --noEmit` 通过。
- 按作者要求**未跑全量测试**；真实库**未写入**；本轮**无真实 LLM 调用**。
- 前后端已重启（8000 / 5173）。

---

## 2026-09-21 · Unreleased：修「导入报 signal is aborted without reason」（超时对不上）

### 现象与根因

作者选完文件点「开始扫描」，报 `signal is aborted without reason`，导入看起来失败了。

**后端日志实证**：请求其实**成功了**：

```
POST /api/import/markdown-files → 200 OK, latency_ms: 47342.88
POST /api/import/markdown-files → 200 OK, latency_ms: 47339.17
```

扫描耗时 **47.3 秒**，而前端超时是 **45 秒**（`IMPORT_TIMEOUT_MS = 45_000`）。
前端在 45 秒时 `controller.abort()`，后端返回 200 的那一刻前端已经放弃了，
于是把 `AbortError` 的原文 `signal is aborted without reason` 直接当成错误甩给作者。

**47 秒花在哪**：`import_service._extract_characters` 用 `jieba.posseg.cut` 对**整本书**做词性标注
（POS 标注比普通分词慢数倍）。120 章小说约 47 秒。

### 修复

- 新增 `MARKDOWN_UPLOAD_TIMEOUT_MS = 300_000`：扫描/入库（上传式与文件夹式共 4 处）统一使用。
  45 秒对「只做格式校验」的接口够用，对「整书分词」不够。
- abort 翻译成人话，不再暴露 `AbortError` 原文：
  「导入分析超时（已等待 300 秒）。长篇小说扫描较慢，可稍后重试，或分几次导入。」
- 回归锁：单测断言扫描路径用的是 `MARKDOWN_UPLOAD_TIMEOUT_MS`（且不得回退到 45 秒常量），
  以及 abort 文案存在。

### 已知代价（未优化，留待决定）

每次点「开始扫描」都会重跑一遍整书词性标注（≈47 秒），「入库」还会再跑一遍。
前端现已给足预算，但作者体验上仍是「点一下要等一分钟」。
若要压缩，需在后端对分析文本采样或换更快的分词器——**代价是识别到的人物数量可能变少**，
属于产品可见的取舍，所以本轮没有擅自改。详见 `docs/current-status.md`。

### 测试与范围

- `test:normalize` **31 passed**（新增 1 条超时回归）；导入 E2E `markdown-upload-import` **6 passed**；
  `tsc --noEmit` 通过。按作者要求**未跑全量测试**。
- 真实库**未写入**；本轮**没有真实 LLM 调用**。前端已重启（5173），后端保持运行（8000）。

---

## 2026-09-18 · Unreleased：白屏未能复现 → 改为「让失败不再沉默」+ 导入大纲已确认生效

### 一、作者侧已确认修复：导入大纲

- 真实库 `f0ee954f3291`（活人的温度）现在是 **120 章 / `outline` 有 120 个 `chapter` 节点**
  （`第一章 坠` / `第二章 空教室` / `第三章 后窗` …），章节导航不再空白。
  → 2026-09-17 的 `_build_import_outline` 修复在真实环境**已验证生效**。

### 二、点「快速过渡」白屏：四次复现尝试全部**正常**

复现矩阵（全部无 `pageerror`、中栏文本不减、过渡正常启动）：

| 复现环境 | 结果 |
|---|---|
| 离线 mock fixture，宽视口 1440×960 | 正常 |
| 离线 mock fixture，窄视口 900×800 与 768×800 | 正常（中栏 582→587） |
| 真实库**副本** + 真实后端代码路径（真实 styleConfig + 真实文风画像） | 正常 |
| 真实库副本 + **120 章真实项目规模** | 正常（中栏 7353→7359，`PUT /style-transition → 200`） |

两条**反证**：

- 作者真实库 `style_transitions` **仍是 0 行** → 那一次点击的请求**根本没有到达后端**；
- 前端开发服务器日志自重启后**没有任何 HMR 事件或报错** → 不是热更新打断。

所以根因仍未闭环，需要现场信息。

### 三、本轮交付：让失败可见，而不是继续猜

1. **`GlobalErrorBanner`**（挂在入口，右下角横幅）
   捕获 `window.onerror` 与 `unhandledrejection`，把「只在控制台里」的失败摆到界面上。
   覆盖错误边界抓不到的类别：事件回调抛错、未处理的 Promise 拒绝、脚本加载失败。
   只收带 message 的错误——资源 4xx/5xx 会以空 message 冒泡（如未分析过的接口 404），
   属于预期内失败，不打扰作者。
2. **`MarkdownImport` 步骤渲染兜底**
   第 3 步的渲染条件是 `step === 3 && result?.success`，两者一旦不同步，三个分支都不命中，
   中栏就是一片空白。现在 `useEffect` 检测到不同步会主动回到第 1 步，
   保证导入面板**永远至少有一个可见步骤**。

### 四、测试与范围

- `tsc --noEmit` 通过；E2E 子集（`markdown-upload-import` + `character-profile`）**16 passed**。
- 按作者要求**未跑全量测试**。
- 真实库**未被写入**（所有真实数据检查均为 `mode=ro` 只读；复现用隔离后端跑在**副本**上，已停止并删除）。
- 临时复现 spec / 截图 / 库副本 / 隔离后端全部清理完毕。前后端服务在运行（5173 / 8000）。
- 本轮**没有真实 LLM 调用**。
- **仍未知**：白屏根因。下一次出现时，横幅或错误卡片会直接显示可读原因。

---

## 2026-09-17 · Unreleased：导入后自动生成大纲（修章节导航空白）+ 中栏错误边界

### 一、导入后章节导航空白（已定位，已修复）

- **现象**：导入上百章正文后，左侧「章节导航」一片空白，作者无法从导航进入刚导入的章节。
- **事实**：真实作品 `f0ee954f3291`（活人的温度）**114 章、`projects.outline = null`**。
- **根因**：`_upsert_import_project` 只插 `chapters`，`projects.outline` 在两处 SQL 里都
  **硬编码写成 `"null"`**；而「章节导航」渲染的是 `outline.nodes`（`ChapterTree.outlineToTree`），
  不是 `chapters` 表 —— 于是章节再多，导航也是空的。
- **修复**：新增 `_build_import_outline()`，按章节顺序生成一层 `chapter` 节点
  （`parentId=null`，携带 `title` / `order` / `targetWordCount`），
  **只搬运已有事实，不虚构卷/弧/摘要**；UPDATE 与 INSERT 两处都改为写入它。
  前端无需改动：`updateProject` 换掉 store 里的项目对象后，`OutlineGenerator` 的
  `useEffect([project])` 会把新版大纲交给 `ChapterTree`；无 volume 节点时章节节点平铺在「大纲章节」下。
- **历史项目补回填**：新增 `backend/scripts/backfill_import_outline.py`，
  **默认只预览不写入**，`--apply` 才写；跳过已有大纲的项目（不覆盖作者手工调整）。
  当前预览结果：仅 `f0ee954f3291`（114 章）需要处理。

### 二、点「风格过渡」后中间区域空白（未能复现，已加兜底）

- **取证**：把真实库复制到临时目录，用**真实数据 + 真实后端代码路径**跑完整流程
  （上传 → 扫描 → 入库 → 点快速 → 开始过渡），复现结果是**正常**：
  `outline.nodes=2`、无 `pageerror`、过渡进度条正常出现，且过渡记录真的写进了副本库
  （`pace=fast, interval_chapters=3, total_steps=3`）。
- **反向证据**：真实库 `style_transitions` 表是**空的** —— 说明作者那一次点击的请求
  **没有落库**，属于前端侧的一次性故障，而不是过渡逻辑本身。
- **推断（未证实）**：故障时间窗内我正在改 4 个前端文件
  （`StyleAnalysisPanel` / `api` / `normalize` / `main`），与「开发服务器热更新」高度相关；
  本会话此前已实测过一次同类现象（Vite 对 `MarkdownImport.tsx` 返回 `200 + 0 字节`，重启即恢复）。
- **兜底**：`ErrorBoundary` 增加 `variant="panel"`，并包住工作台**中栏**所有面板。
  以后中栏任何面板渲染抛错，都会**就地**显示原因 + 「再试一次 / 重新载入」，
  而不是留下一片空白（左侧导航、项目列表保持可用）。
- **仍未做**：没有拿到作者浏览器的控制台报错，根因未闭环。若再次出现，错误卡片上会直接显示原因。

### 三、测试与范围

- 后端 `test_markdown_upload_import.py`：**18 passed**（新增 2 条大纲回归：生成 + 重复导入随之更新）。
- E2E 子集（`markdown-upload-import` + `character-profile`）：**16 passed**；`tsc --noEmit` 通过。
- 按作者要求**未跑全量测试**；临时复现用的 spec、截图、真实库副本、隔离后端（8001）均已清理。
- 本轮**没有真实 LLM 调用**；真实库 `data/beta-user-001.db` **未被写入**（回填脚本只做了只读预览）。
- 前后端开发服务均已重启（8000 / 5173）。

---

## 2026-09-17 · Unreleased：修复导入成功后白屏（渲染异常掀掉整棵 React 树）

### 背景

作者反馈「前端打不开了，也没有报错只有空白页面」。分两个独立缺陷定位，都已修复。

### 缺陷 1：`StyleAnalysisPanel` 空画像崩溃（真实产品缺陷）

- **现象**：Markdown 导入成功后（`enabled={Boolean(successMessage)}`），面板挂载即抛
  `TypeError: Cannot read properties of undefined (reading 'length')`，
  React 卸载整棵树 → **纯白页面**，界面层没有任何报错。
- **根因链**：`api.ts` 的 `styleApi.getProfile` 会把响应交给 `normalizeStyleProfile`；
  该函数对非对象输入返回 `{} as StyleProfile`——一个**看起来是画像、实际没有任何字段**的假对象。
  面板的 `.then((json) => { if (json.data) setProfile(json.data) })` 判定 `{}` 为真值，
  于是把假对象塞进 state，渲染到 `profile.strengths.length` 时炸掉。
- **修复**：
  - `StyleAnalysisPanel` 拉取画像/过渡时改用 `json.data?.status` / `json.data?.pace` 作为
    「确实有数据」的判据（`normalize*` 保证真实数据一定带这两个字段，`{}` 一定不带）；
  - `strengths` / `weaknesses` 退化为 `?? []`，规则列表改用 `profile?.[group.key] ?? []`。
- **触发条件说明**：真实后端在无画像时返回 **404**（不是 200 + `data: null`），
  所以这条在本机当前后端上不一定被触发；它是一颗地雷，任何「200 + 空 data」的上游
  （代理、缓存、降级路径、就近的同类 `return {} as X`）都会踩响。本次由离线 fixture 稳定复现。
- **回归锁**：`markdown-upload-import.spec.ts` 的入库用例新增
  `expect(state.pageErrors).toEqual([])` 与 `app-error-boundary` 计数断言。
  修复前 `--repeat-each=5` 为 **27 passed / 3 failed**，修复后 **30 passed**。

### 缺陷 1 补充：`normalize*` 不再用「假对象」冒充空值

同一类地雷在 `normalize.ts` 里还有 3 处。全部改为诚实的 `null`，并让列表**丢弃**脏元素：

| 函数 | 空值分支 | 列表处处理 |
|------|----------|-----------|
| `normalizeStyleProfile` | `{} as StyleProfile` → `null` | —— |
| `normalizeStyleTransition` | `{} as StyleTransition` → `null` | —— |
| `normalizeForeshadow` | `{} as Foreshadow` → `null` | `normalizeForeshadowListData` 用 `filter` 丢弃 |
| `normalizeForeshadowSuggestion` | `{} as ForeshadowSuggestion` → `null` | `foreshadowApi.suggest` 用 `filter` 丢弃 |

- 选这条路的理由是**改动最小**：`styleApi` 全仓库只被 `StyleAnalysisPanel` 一个文件消费（且已完成 null 守卫），
  `foreshadowApi` 只有两个消费方，所以 `tsc --noEmit` **零波及**——不需要链式改任何调用方。
- 列表这里刻意选「丢弃」而不是「补一条空记录」：脏元素补成默认值会渲染出一条
  「未命名伏笔」幽灵行，比丢弃更难解释。
- `normalizeForeshadowConsistency` 保持原样：它返回的是字段完整的「无数据 = 通过」，
  语义上确实有默认值，不属于假对象。
- 新增单测锁住该行为（`tests/normalize.test.mjs`），`test:normalize` **30/30**。

### 缺陷 2：新增全局错误边界（这一类问题的兜底）

- 新增 `components/common/ErrorBoundary.tsx`，在 `main.tsx` 包住 `<App />`。
- 从此渲染期异常不再表现为「没有报错的白页」，而是给出可读原因 + 「再试一次 / 重新载入」。
- 边界只能捕获渲染期异常，捕获不到事件回调与异步异常，它是兜底而非替代具体修复。

### 排查过程中观察到的另一现象（非产品缺陷，记录备查）

- 开发期 Vite 曾对 `src/components/import/MarkdownImport.tsx` 返回
  `200 + Content-Length: 0`（`ETag=W/"0-…"`），浏览器报
  `does not provide an export named 'default'`，导致**含根路径在内**的所有页面白屏。
  **重启前端即恢复**（同一模块随后返回 91730 字节）。这是开发服务器的模块缓存问题，
  不影响构建产物；若再次出现，重启 `npm run dev` 即可。

### 测试与范围

- 全量离线 E2E：**58 passed / 34 skipped / 0 failed**（41.4s）；`typecheck` 通过。
- 本轮**没有真实 LLM 调用**，未触碰 `data/beta-user-001.db`。
- ⚠️ 最后一项改动（`normalize*` null 化）**未重跑全量 E2E**（按作者要求省略），
  只跑了相关子集：`test:normalize` 30/30、`markdown-upload-import` + `character-profile`
  E2E 16/16、`tsc --noEmit` 通过。全量绿是这条改动之前的状态。

---

## 2026-09-17 · Unreleased：Markdown 章节导入补上传通道 + 修复导入项目的 owner_id 错位

### 背景

用户报告「Markdown 章节导入失败，只提示生产环境仅支持上传文件」。核查结果：

- 前端 `MarkdownImport.tsx` **只有「服务器文件夹路径」一种模式**，调的是 `/import/markdown-folder*`；
- 这两个接口读服务端任意路径，是联网部署里的本地文件读取攻击面，因此被 `if not settings.debug: 403` 有意挡住（**这道门是对的，不变**）；
- 但**报错文案说的「上传文件」这条路根本不存在** —— Word 建议书导入是 `file` 上传 + `filePath`（仅 debug）两条路，Markdown 只做了后者。

于是 `DEBUG=false` 下 Markdown 导入完全不可用，且提示误导。与近两轮改动无关（`imports.py` 最后修改 2026-09-12，`MarkdownImport.tsx` 2026-08-09）。

### 本次实际改动

- **新增 `POST /api/imports/markdown-files`（扫描）与 `/markdown-files/ingest`（入库）**：接收 `files: UploadFile[]` + 同序的 `relativePaths`，落到临时目录后**完全复用** `scan_markdown_folder()` 与 `_upsert_import_project()`；解析、摘要、人物／地点／时间线提取逻辑未重写。临时目录在 `finally` 里清理（成功与失败路径都会清）。
- **前端改为双模式，上传为默认**：① 「选择 .md 文件（可多选）」；② 「选择整个文件夹」（`webkitdirectory`，仅 Chromium 系支持，不支持的浏览器里整个入口隐藏）。原路径模式保留但标注「仅本地开发（`DEBUG=true`）可用」。非 Markdown 文件在前端就被过滤并提示数量。
- **安全与健壮性**：相对路径做净化，拒绝绝对路径与 `..` 穿越；不同卷下的同名章节不会互相覆盖；MIME／二进制内容／单文件大小沿用既有校验；扫描与入库都要求有效身份，入库到已有项目仍走 `require_project_owner`。
- **项目名不再可能是随机 uuid**：临时目录名不对外，默认项目名从上传的顶层文件夹名推断，推不出来才退回「Markdown 导入」。
- **修复 `_upsert_import_project` 新建项目时的 INSERT 列错位**（既有 bug）：`owner_id` 被写到了第 13 个值的位置（实际写入 `0`），导致后续 `is_starred`/`premise`/`target_chapters`/`characters`/`locations`/`world_rules`/`import_analysis` 全部错位。后果是**普通用户导入生成的项目自己无权打开**（403）。在 `DEBUG=true` 下因为有 `ADMIN_USER_IDS` 管理员例外所以从未暴露。真实库核查：现有 8 个项目的 `owner_id` 均正常，该 bug 尚未实际损坏数据。
- 顺带把 `scan_markdown_folder` 的 `relativePath` 统一成 `/` 分隔（`Path.as_posix()`），不再返回 Windows 反斜杠。

### 测试与范围

- 新增后端 `test_markdown_upload_import.py` 16 项：鉴权 401、非 Markdown／二进制 400、章节顺序按文件名章节号、目录层级保留、同名不碰撞、`..` 穿越被净化、临时目录（含失败路径）清理、入库顺序、非 owner 403、显式项目名、重复导入替换旧章节。
- 新增离线 E2E `markdown-upload-import.spec.ts` 6 项：默认上传模式、文件夹模式带开发专用提示、非 Markdown 过滤、multipart 里 `files` 与 `relativePaths` 成对同序、入库成功、失败时如实显示后端原因且不回落路径模式。
- 后端 **1045 passed**；前端 `test:normalize` 29/29、`profile-falsify` 12/12、`typecheck`/`build` 通过；离线 E2E 全量重跑（新增 6 项）。
- 未产生真实 LLM 调用（导入链路的扫描与摘要是确定性的），未触碰真实数据库写入。

### 仍未做

- 路径模式仍需本地开发环境才可用；未提供 zip 打包上传。
- `markdown-folder*` 两个旧接口未删除（保留本地开发用途）。

---

## 2026-09-16 · Unreleased：角色档案改造（手写 + AI 分析，逐项确认）

### 本次实际改动

- **角色卡从「只读展示」改为「可编辑档案」。** 基础信息、性格倾向、背景与驱动力、写作参考四个区块共 14 个字段（外加卡头的姓名与角色定位）均可手写并保存。此前系统**没有编辑角色的入口**，`persistCharacters()` 只在新建角色与导入分析结果时被调用。
- **删除大五人格五维打分与 MBTI**，改为一段纯文字描述。旧结构（`{openness, …, mbti}`）在前端归一化时被丢弃、以 `{description: ""}` 取而代之；**不做分数→文字迁移**——那些分数是关键词规则对一句简介的猜测，从来不是作者手写的，翻译成「外向度偏高」仍是打分口吻。旧角色的该字段显示「未填写」，并引导作者手写或让 AI 重新生成。
- **新增 `POST /api/projects/{project_id}/analyze-character-profile`**：AI 用 `planning` 角色通读该项目的**章节正文 + 大纲**，**一次调用**返回全部字段的建议值。只产出建议，**绝不自动覆盖作者内容**，由作者逐项「采用」/「忽略」；采用后仍需点「保存」才落盘。
- 删除了同日早些时候加入的 `analyze-character-personality`（泛化后不再需要，无其它消费方）。后端 `character_profile_service.PROFILE_FIELDS` 是字段清单的唯一来源，前端字段 key 由测试断言保持一致。
- **字段级校验：宁可拒绝，也不编造。** 文本字段统一清洗 MBTI / 维度名 / 数字评分残留（含大小写变体与末位重复）；年龄只接受整数、**越界直接拒绝而不静默改值**；性别归一化为 男/女/未知；非字符串（对象/数组）一律拒绝。AI 不可用或返回不合法时返回 200 + `degraded=true` + 中文原因（区分未配置/超时/无法解析/格式不合法），**不用规则引擎拼造内容**。年龄与性别在界面上标注为「推断」。
- **把 7 个此前未被任何 AI 链路消费的字段接进上下文**：外貌/年龄/性别/性格倾向/背景经历/核心创伤/口头禅 进入续写提示词，深层欲望/价值观/说话风格 进入审校提示词，口头禅进入人物声线档案的常用语。前端续写与审校收敛到同一个构建入口 `services/characterContext.ts` 并有测试锁住，避免一边改一边漏。
- 修复上一轮遗留的手感问题：编辑态按角色 id 分桶，**切换角色不再静默丢弃未保存的手写内容**；AI 建议也只记在目标角色名下，中途切角色不会串数据。

### 测试与范围

- 后端 **1029 passed**（其中角色档案 65 项）；离线 E2E **52 passed / 34 skipped / 0 failed**；`test:normalize` 29/29；新增档案证伪单测 12/12（含前后端字段清单漂移检查）；`typecheck` / `build` 通过（保留 chunk>500kB 警告）。
- 做过反向验证：故意让分析路径写草稿后，源码守卫与浏览器行为测试均如预期失败，随后还原并确认无临时标记残留。
- 本轮**没有真实 LLM 调用**，未触碰 `data/beta-user-001.db`（mtime 早于全部测试活动）。后端已于 2026-09-16 重启（PID 33424）。
- 真实后端 E2E（`novelcraft-smoke` / `projection-panel`）改为需 `NOVELCRAFT_E2E_INVITE` 才运行，默认整文件跳过而非报 401 假失败，详见[测试运行说明](docs/test-suite-runtime.md)。

---

## 2026-09-13 · Unreleased：当前说明、审计交接与可恢复清理

### 本次实际改动

- 重写 README，补全文档索引、当前状态、用户指南、脚本目录、审计报告及 DeepSeek 工作清单。
- 更新启动、Beta 环境、测试隔离、对话持久化与大纲候选说明；为旧发布清单加历史提示。
- 修正 dotenv **样例**中的旧 APP_DEBUG、未支持的 DEEPSEEK_API_BASE，以及后端/前端变量混放；补账户初始化、Cookie、同源代理、私有路径与离线备份/WAL 边界。没有改真实 .env 或 provider 配置。
- 移除经确认无引用的 debug_resume.py、diag-dashboard.cjs、frontend/query 与 Python/pytest 缓存；先保留仓库外可恢复副本，详见 清理记录。
- 本轮没有业务代码修改、数据库变更、部署或真实 LLM 调用。

### 近期功能现状补记（不是本次实现）

- 账户名/密码会话登录、左侧独立账户入口；旧邀请码兼容，管理员可访问既有项目。
- 项目助手多会话持久化、归档/恢复/删除与手动重新生成；候选在会话中确认。
- 卷/剧情弧/章节导航、候选大纲调整、已有正文移除确认与章节回收站。
- 流式正文显示、章节选择对应内容、未保存草稿保护；Copilot 接受后 applied_content 和编辑器事件同步，后续请求使用最新正文。
- 章节记忆同步与生成记录同栏切换；provider thinking 按任务区分，大纲/正文上下文按目标范围构建。
- 以上是当前代码与既有测试反映的能力，不代表真实模型完全遵循大纲或产品所有入口已通过本轮验收。

### 测试与待办的范围

- 前一轮正文同步修复记录：后端 933 passed / 1 warning / 154.87s，22 mocked E2E，typecheck/build PASS（chunk 警告保留）。
- 随后独立审计：23 定向后端测试、4 条 mocked 浏览器流程通过，Arts calls=0、项目 API cost=0；本次文档任务不冒称重跑全量。
- 审计确认 logout 状态、账号注销数据政策、Copilot 派生同步、Fulltext resolver/费用、run 查询范围需继续处理。已记录一次测试误写空项目事故，未擅自删除真实数据库行。
- 详见 审计记录、[当前状态](docs/current-status.md)和 DeepSeek 工作清单。

---

## 🔧 2026-08-18 Codex 复测三轮验收：前端 build/E2E 补跑 + API 集成 + 投影浏览器测试 + 记忆金标准扩展

> 按 Codex 复测二轮意见补齐验收缺口；后端 **562 → 571**；前端 **build EXIT=0 + typecheck EXIT=0 + 完整 E2E 31/31（真实 LLM）+ 投影面板 E2E 2/2（新增）**。

### 前端完整回归补跑（此前只有 typecheck）
- `npm run build`（tsc -b + vite build）EXIT=0；仅 chunk>500kB 警告（非错误）。
- 完整 Playwright E2E（RUN_LIVE_LLM=1 真实 LLM 全量 31 项）**31/31 通过**。
  首轮 4 失败复盘：仅覆盖 writing→deepseek、word/check 仍走 kimi（超时/短输出）——全角色（writing/word/check）指向 deepseek 后全绿。

### 失败章恢复真实 API 集成测试（新增 `test_novel_job_resume_api.py`，7 passed）
- HTTP 层（TestClient + lifespan 模拟后端重启）：running→interrupted+recoverable；默认重试失败章本身/显式跳过；末章失败默认重试+skip 400；取消/失败/恢复不互相覆盖（进度保留）；SSE/REST 一致；部分流式内容不足阈值重试该章、达阈值视为完成跳过。

### 投影失败前端展示/重试 E2E（新增 `projection-panel.spec.ts`，2 passed）
- 5 路投影状态渲染 + failed 可见（带原因 detail）+ 重试按钮；点击重试→replay API→状态 done→按钮消失；REST 确认 facts=done 单条无重复。
- 重复重放幂等（只重跑该项）由后端单测 `test_replay_failed_projection_only` 覆盖。

### 200 万字记忆金标准扩展（`test_memory_recall_golden` → 4 passed）
- 新增别名召回（正文含 3 字以上别名「北境镜主」→ 别名查询命中；2 字昵称属已知粒度限制）；
  噪声过滤（30 个高频通用词噪声章零泄漏，precision 不被稀释）。
- 原指标保持：recall/precision@k=1.0、错误召回 0.0、p95 23.1ms。

### 盲评措辞更正
- `_p14_v2_blind_review.md` 改为「双盲·双视角（同一评审 Agent）」，明确非独立双人评审，结论仅作模型对比实验。

### 真实创作验证 + 配置还原
- `POST /api/ai/chat`（writing role）→ deepseek-chat success（审计确认 provider=deepseek、source=llm）。
- 交付前还原用户原始配置：kimi `is_active=1`、`role_overrides={}`、deepseek 未激活。

---

## 🔧 2026-08-18 Codex 九轮复测修复：失败章恢复重试 + 不可变提交链 + 投影闭环 + role unspecified

> 按 Codex 复测意见修复 P0/P1 阻断问题并更正过度表述。
> 后端测试 **550 → 562** 全绿；前端 typecheck EXIT=0；E2E 31/31 + novel job 子集 9/9。

### P0/P1：失败章节恢复默认重试失败章（阻断问题）
- `resume_job(job_id, skip_failed_chapter=False)`：**默认重试失败章本身**（不跳过），
  保证整书不因中间失败永久缺章；末章失败默认可重试（不再因无后继被拒）。
- 仅显式 `skip_failed_chapter=true`（用户明确选择跳过）才从下一章继续；末章失败无法跳过时拒绝。
- 重试再次失败保持 failed+pending_manual_recovery（可再次恢复）；恢复成功计数不重复。
- `_is_job_recoverable`：失败章任务默认可恢复（末章失败也进入可恢复列表）。
- API `POST /{job_id}/resume?skip_failed_chapter=bool`；前端 resume 文案更新（重试失败章）+ 末章失败「重试失败章」按钮。
- `test_novel_job_resume` 覆盖：中间章失败/末章失败/重试再次失败/计数不重复/显式跳过/跳过末章拒绝。

### P1：不可变提交链（替换"最新快照覆盖"）
- `chapter_commit_history` 表：每次落库新增 `version` 递增 + `parent_commit_id` 链接的 commit 记录
  （不覆盖、保留完整版本历史）；`get_commit` 返回最新（快照视图），`get_commit_history` 返回版本链。
- 正文保存与 commit 记录**同一事务**（`_persist_generated_chapter` + `record_commit_on_conn`）：
  commit 失败即回滚正文（阻断性质量债务，不再静默吞掉）。
- `test_chapter_commit`：版本链/同事务回滚/幂等重放 6 passed。

### P1：记忆投影失败产品闭环
- 后端：`GET /chapters/{id}/projections`（查询 5 路投影状态）+ `POST /chapters/{id}/projections/{type}/replay`（单项重放）。
- 前端：`ProjectionStatusPanel` 组件（章节记忆同步状态 + failed 重试按钮），挂载 MarkdownEditor。
- 幂等重放（重复点击只重跑该项）、服务重启后投影状态持久可查。

### P2：缺失 role 不再静默归类 check
- 新增 `ROLE_UNSPECIFIED`；`_real_chat`/`_real_chat_stream` 中 role 仍缺省且审计上下文无 role 时
  记录为 `unspecified`（走激活配置，不静默归为审校）+ 结构化 warning。
- `test_role_routing`：常量完整性 + unspecified 语义更新。

### 表述更正（Codex 复测）
- 「20 次复杂上下文成功」→ 20 组上下文构造测试 + 1 次真实模型冒烟。
- 「200 万字记忆验证」→ 200 万字容量冒烟（非召回准确率验证）。
- 「章节提交链」→ 不可变提交链（版本历史 + 同事务）。

### P2：长期记忆金标准召回准确率
- 新增 `test_memory_recall_golden`：60 差异化章节/20 实体（多别名）/同名跨项目隔离/superseded/矛盾事实/跨卷伏笔。
- recall@5/10=1.0、precision@5/10=1.0、错误召回率 0.0、查询延迟 p50=14ms/p95=18ms。
- 已知限制：数字/编号结尾实体名会被关键词滑窗拆分后缀（已改用纯中文实体名）。

### P2：文笔盲评 v2（双盲 · 双评审）
- 3 题材×3 场景=9 场景，DeepSeek/Kimi 各 9 份（708–1337 字），匿名化后由评审双视角打分（文学性 5 指标 / 读者留存·商业化）。
- 文学性均值：DeepSeek 4.22 > Kimi 3.93；读者留存均值：Kimi 4.39 > DeepSeek 3.94（两评审结论部分相反，反映维度差异）。
- 报告：`_p14_v2_blind_review.md`。⚠️ 样本未达完整章节长度（Kimi 长文仍不稳定），已如实声明。

### 密钥清理（用户确认后执行）
- 删除 2 处含明文 key 的备份：`data\novelcraft.db.backup`、`test_artifacts\audit-20260810\novelcraft.db.before-audit`（用户已确认；`.env` 保留运行）。
- 交付前已核对模型配置还原为原始状态（kimi `is_active=1`、`role_overrides={}`、deepseek 未激活）。

---

## 🔧 2026-08-17 Codex 九轮：密钥安全 + fallback 隔离 + 模型归属 + 复杂记忆 + 200 万字压测 + 提交链（5 阶段任务单）

> 严格 5 阶段：密钥处理 → fallback 隔离 → 模型归属 → 复杂记忆测试 → 合成压测与文笔盲评。
> 后端测试 **522 → 550** 全绿；阶段C 复杂上下文 20 次金丝雀验证 + P1-3 200 万字合成压测 + 阶段E 提交链全过。

### 阶段A：密钥安全（P0-1）+ fallback 隔离（P0-2）
- P0-1：备份脚本去敏（不写 api_key，只存 provider/model/角色名）；日志掩码（前6后4）；
  删除明文备份；gitignore 追加 `.env`/`test_artifacts/`/`creat-reports/`；
  新增 `test_secret_safety.py`（4 passed：备份/JSON报告/日志不得匹配真实密钥格式）。
- P0-2：3 次 LLM 失败 → `source="failed"`（正文不落库）；job 走 `chapter_failed` +
  `pending_manual_recovery`；不增 completed/字数；fallback 仅临时预览（不落库）；
  `test_novel_job_resume` 改写验证 job failed + completed=0 + fallback=0 + 正文为空。

### 阶段B：实际 provider 记录（P1-1）+ 无角色调用消除（P1-2）
- P1-1：`audit.py` 新增 contextvar `set/get/clear_llm_call_meta`（协程隔离）；
  `provider._call_openai` 各路径记录实际 provider/model/audit_id；
  `ai_service` 从 call_meta 读取真实 provider/model/audit_id（修复 import 缺失）。
- P1-2：role 未指定默认 `ROLE_CHECK`（禁止空 role 静默回退全局）。

### 阶段C：复杂上下文 20 次（金丝雀事实）
- 新增注入「未回收伏笔」「时间线约束」两个记忆块（9 类信息齐备），预算 TOTAL 4600→5200。
- `test_complex_context.py` 21 passed：20 个唯一金丝雀变体 + 预算压力；
  验证 9 类信息非空、金丝雀进入实际提示词、superseded 失效事实过滤、跨项目隔离、无剧透、预算内。
- 真实 LLM 冒烟（DeepSeek）：job llm/success、fb=0、审计 deepseek/success、
  body 含金丝雀+实体+伏笔+时间线+事实账本+文风提示、无他项目泄漏。

### P1-3：200 万字记忆合成压力测试
- `test_memory_synthetic.py`：800 章（约 200 万字）/100 人物/200 规则/500 事实/200 伏笔 + 干扰项目；
  FTS5 索引 800/800；召回准确率（九星连珠命中第 360 章）、过期事实过滤、跨项目隔离、
  伏笔状态、构建延迟 0.042s、上下文预算 3385/5200 全部验证。
- 修复 `_fts_keywords_from_chapter`：长连续汉字切 4 字滑窗（FTS5 unicode61 中文召回率提升）。

### 阶段E：章节提交链 + 记忆投影（借鉴 Webnovel Writer 思想，自研实现）
- `chapter_commits` 表：正文哈希 + 上下文哈希 + audit_id + 模型来源 + 完成时间（同章幂等）。
- `memory_projections` 表：章节摘要/事实/实体/卷摘要/FTS 五路投影各自 done/failed/skipped；
  新增 `replay_memory_projection` 单项重放（某项失败只重跑该项）。
- `test_chapter_commit.py` 5 passed：提交记录、哈希可复现、投影状态、落库写提交+投影、单项重放只调该项。

### P1-4：文笔匿名盲评（6 组 DeepSeek/Kimi）
- 统一场景「雪夜无归客栈」续写，同 prompt 生成 6 组样本（DeepSeek×3 + Kimi×3，400-680 字）。
- web-novel-master 双盲评审（未接触模型名），10 指标逐项原始评分 + 评语保留在 `_p14_blind_review.md`。
- 结果：Kimi 平均 4.35（S2 综合 4.7 最高）略优于 DeepSeek 3.98（短篇场景），但**不推翻第八轮结论**
  ——Kimi 长复杂上下文仍因推理耗尽 max_tokens 失败，正文主链维持 DeepSeek。
- 盲评机制（匿名样本 + 10 指标 + 逐项评分/评语）已落地，可复用于迭代回归。

---

## 🔧 2026-08-17 Codex 八轮：Kimi 根因确认 + 正文主链切换 DeepSeek（P0-1/P0-2/P0-3/P0-4 诊断）

> DeepSeek 调整任务单：停止围绕 Kimi 反复重跑；正文生成/续写/断点恢复优先 DeepSeek，
> Kimi 退出正文主链（改文笔评审/备用）；不直接覆盖用户配置（writing 角色覆盖测试后恢复）。
> 后端测试 **513 → 518** 全绿（+5 夹具测试）；前端 typecheck 通过。

### 一、Kimi 根因确认（最小化诊断，已归档"Kimi 复杂写作兼容性待处理"）

- **根因**：`kimi-k2.6` 是推理模型，`reasoning_content` 会消耗 `max_tokens` 预算。
  审计元数据实锤（`llm_audit_log` 新列）：
  - 复杂长上下文（input 1554 prompt_tokens）：`finish_reason=length`、`completion_tokens=4096`（= max_tokens）、
    `reasoning_chars=6723`、正文 `content=0` → 判空抛错 → 3 次重试 → fallback
  - 短 prompt（54 tokens）：`reasoning_chars≈465`、`finish_reason=stop`、正文正常 → 成功
  - 结论：不是"空响应"，而是**推理内容耗尽 token 预算导致正文未生成**；first-unfinished 10 次失败根因即此。
  修复方向（未来如需支持 kimi）：提高 `max_tokens` 或改用非推理参数/模型。

### 二、请求/响应脱敏元数据采集（`llm_audit_log` 新列 + `_call_openai`）

- `audit.py`：`llm_audit_log` 幂等新增列（兼容老库 ALTER）：
  - 请求侧：`message_count` / `input_chars` / `est_tokens` / `max_tokens` / `temperature`
  - 响应侧：`http_status` / `finish_reason` / `prompt_tokens` / `completion_tokens` /
    `total_tokens` / `reasoning_chars` / `request_id`
- `provider._call_openai`：请求前算脱敏指标，响应后提取元数据（含 `reasoning_content` 长度、
  Moonshot request id）；`content=""` 且 `finish_reason=length`/有 reasoning 时报错信息
  明确"推理内容可能耗尽 token 预算"。**正文与密钥绝不落库**。
- 诊断导出：环境变量 `LLM_DIAG_BODY_DIR` 开启时，把完整请求体落盘供"完全相同请求体"等价对照
  （只用于本地诊断，不进审计/不进 git，默认关闭）。

### 三、P0-1：DeepSeek writing 角色审计配置（不覆盖用户配置）

- 通过 `llm_configs.role_overrides.writing` 将正文角色临时指向 DeepSeek
  （provider=deepseek / model=deepseek-chat / 独立 key+base），测试前备份原配置
  （`_role_overrides_backup.json`），测试后恢复。
- 已验证角色路由：`/api/ai/chat`（role=writing）审计 `provider=deepseek`、`llm/success`、
  `reasoning_chars=0`、响应 ~1.6s——DeepSeek 无推理内容开销，正文稳定生成。
- 辅助角色（摘要/核查/风格等）仍走激活配置 Kimi，符合"审校/评审角色不必同步切换"。

### 四、P0-3：提示词上下文预算报告

- 新增 `_prompt_budget_report.py`：按模块拆解 user prompt，统计字符数与估算 token，
  输出 `_prompt_budget_report.md`。代码侧已有显式预算常量（`CONTEXT_BUDGET_*`，
  premise=200/header=600/volume=3000/recent=1100/ending=800/entity=400/fts=500/
  milestones=1500，total=4600），按优先级低→高丢弃（entity/milestones 永不丢弃）。

### 五、P0-4：fallback 落库行为检查（记录）

- 现状：`generate_continuation_result` 3 次重试失败 → `_continue_fallback` 占位文本
  会写入正式章节（`_persist_generated_chapter`，status=completed），带 job 级
  `fallback_chapters` / `last_generation_source=fallback` 标记；BUG-A 已保证 fallback
  占位**不写章节摘要**（避免污染记忆）。E2E 验收已通过 `fallback_chapters===0` 门槛拒绝。
- 建议（后续迭代）：fallback 落正式章节改为"标记失败/待重试 + 仅 UI 预览"，用户可见"真实生成失败"。

### 六、固定响应夹具测试（`tests/test_provider_meta.py`，+5）

- `content=""` + `finish_reason=length` + 长 reasoning → 报错"推理内容可能耗尽" + 审计元数据
- `content=""` 无 reasoning → 报错"空内容"
- `content` 非空 + reasoning → success + `reasoning_chars`/`finish_reason`/usage 记录
- 有 reasoning 但 content 空 → 仍 error
- HTTP 500 → 审计 `http_status`/`request_id` 记录

### 七、验收验证状态（DeepSeek 主线 · 全部通过）

- 后端 518 passed；前端 typecheck 通过
- **P0-2 DeepSeek 复杂单章压力测试：20/20 全部成功**（deepseek/success、零 fallback、
  平均 ~16s/章；响应时间/输入规模/输出字数/错误类型均记录于 `_ds_chapter_20.json`）
- **P1-1 DeepSeek first-unfinished：3 次排查 + 10 次正式稳定性全部通过**
  （10/10，55-93s/次；恢复 job 完成章节、source=llm、fallback=0、无重复覆盖）
- **完整 31 项 E2E：31/31 passed**、`live invoked 13/13`、**`live succeeded 13/13`**、
  零 fallback（报告 `_full31-deepseek2_summary.md`）；所有 live 用例 provider=deepseek/deepseek-chat
- **角色路由修复（Codex 八轮 P1 补）**：`_real_chat`/`_real_chat_stream` 未显式传 role 时
  回退用**审计上下文 role** 路由——修复 `import_service`（word）/`review_service`（check）等
  只设审计 role 而未传 llm.chat(role=...) 导致角色路由失效、永远走激活配置的问题。
  修复前 word/check 用例 120s 超时（kimi error），修复后 5-8s 通过（deepseek success）。
- Kimi first-unfinished 10 次失败记录保留为兼容性证据（不改写结论）

---

## 🔧 2026-08-16 Codex 七轮审查修复（2 P1 + 2 P2）

> Codex 七轮审查：指出六轮验收存在"调用成功被误判为生成成功"的问题——
> ① `waitForLlmSuccess` 实际只等 `source=llm` 任意状态（`status=error` 也通过）；
> ② first-unfinished 恢复链路只证明"尝试恢复并调用模型"，未证明"恢复后成功继续生成"；
> ③ 报告门槛用 `_evidence_has_any_llm` 混淆"调用发生"与"调用成功"；
> ④ 参考分析可能由 fallback 掩盖真实模型失败。
> 后端测试 **511 → 513** 全绿；前端 typecheck / build 通过。

### 〇、测试分类清单更新

- **E2E 总数 31 项** = **13 项 @live-llm**（真实调用模型，需来源证据）+ **18 项本地确定性**
- 新增 1 项本地确定性：`reference analysis falls back gracefully when LLM fails`
  （mock `/api/ai/chat` 500，验证参考分析降级到本地分析——fallback 行为与真实模型分开验证）

### 一、P1：`waitForLlmSuccess` 必须真正等待成功（杜绝 error-only 误判）

- **拆分函数**（`novelcraft-smoke.spec.ts`）：
  - `waitForLlmAttempt()`：只等 `source=llm`（任意状态）→ 标记 `llm-evidence-level=invoked`
  - `waitForLlmSuccess()`：必须等 `source=llm 且 status=success` → 标记 `llm-evidence-level=success`
- 生命周期类用例（novel job 启动/SSE/取消）改用 `waitForLlmAttempt`（仅证明"真实调用了模型"），
  并明确归类为调用链路测试；其余生成证明类用例全部用 `waitForLlmSuccess`（强制 llm/success）
- 报告按 `llm-evidence-level` 注解分别统计 **live invoked** / **live succeeded**，
  **禁止把 error-only 计入成功**；成功文案改为
  `live invoked X/Y、live succeeded Z/Y`

### 二、P1：first-unfinished 必须验证"恢复后成功继续生成"

- 恢复 job 轮询 `completed_chapters >= 1`（不再只等审计记录）
- 校验恢复 job：`last_generation_source == 'llm'`、`fallback_chapters == 0`、
  `last_completed_chapter_id != 初始 job 已完成章节`（无重复覆盖）
- 恢复前快照初始 job 已完成章节内容，恢复完成后校验内容未被覆盖（强无覆盖证明）
- 恢复 job 必须等 `waitForLlmSuccess`（llm/success）
- **恢复重试循环**：为应对 kimi 长文生成的间歇性波动，最多重试 3 次恢复；但每次恢复
  仍严格要求 `source=llm / fallback=0 / 无重复覆盖`——绝不因重试而把 fallback 计入通过。
  轮询超时（comp=0）也视为一次失败尝试进入重试
- **初始 job 仅当以真实 LLM 完成（source=llm、无 fallback）时才记录为验收证据**；
  若因 kimi 波动降级为 fallback 则视为纯场景搭建不记录（本用例核验点是"恢复成功"）

### 三、P2：报告门槛区分"调用发生"与"调用成功"

- `e2e_report.py` 新增 `_evidence_level()` / `_job_table_proof()`：
  - `invoked` 等级：仅要求 `_evidence_has_any_llm`（source=llm 任意状态）
  - `success` 等级（默认）：必须 `_evidence_has_success`（source=llm 且 status=success）
  - job 表兜底：completed>=1 + source=llm + fallback=0 视为真实生成成功
- 报告第三节增加"调用(invoked)/成功(succeeded)"列；`EXPECTED_TOTAL=30 → 31`

### 四、P2：参考分析真实模型用例必须 llm/success

- 参考分析 live 用例改为**直接调用后端 `/api/ai/chat`**（等价于前端参考面板 `aiApi.chat`），
  绕过前端 20s 客户端超时导致的本地 fallback——真实模型失败不再被降级结果掩盖
- 必须返回可解析的技法分析 JSON（三块结构齐全）+ 审计 `llm/success`，否则失败
- 新增本地确定性 fallback 用例（mock 500 → 校验 `reference-error` + 本地分析渲染）

### 五、门槛测试新增（`test_e2e_report_gate.py`）

- +2：`test_success_required_with_error_only_fails`（仅 error 必须非零退出码）、
  `test_invoked_level_with_error_only_passes`（invoked 等级 error-only 通过）
- 全量用例更新为 31 项

### 六、验收验证状态（2026-08-17 更新）

- **后端 513 passed**；前端 typecheck / build 通过
- **单独重跑 5 项受影响 live 用例：4/4 live 通过**（正确标题）
  - ✅ `novel job API starts and returns progress`（invoked）
  - ✅ `novel job SSE stream pushes live progress updates`（invoked）
  - ✅ `novel job API cancel stops generation`（invoked）
  - ✅ `reference analysis can analyze pasted text`（success，真实 `/api/ai/chat` + `llm/success`）
- **本地 fallback 用例（新增）通过**：`reference analysis falls back gracefully when LLM fails`
  - 修复过程中发现并修复**前端组件缺陷**：`ReferenceAnalysis.tsx` 中 fallback 成功进入结果视图后，
    错误提示卡缺少 `data-testid="reference-error"`（输入卡内原有 testid 因 `{!result && …}` 条件消失）——
    已为结果视图错误卡补上 testid，typecheck 通过
- **`first-unfinished` 历经 10 次完整重跑未绿——根因为 kimi-k2.6 环境不稳定，非代码缺陷**：
  - 审计表证据：恢复 job 的主写作生成（`role=writing`）调用**持续 `error`，耗时 119–131s**
    （Moonshot API `HTTP 200` 但 120–160s 后返回空 `content` → `_call_openai` 判空抛错 →
    3 次重试后 fallback；每次恢复尝试的主写作调用均全灭）
  - 直接探测（相同参数：非流式、temperature=1.0、max_tokens=4096、长提示词）**4 连发全成功**
    （content 648–807 字符）；reference live 用例（较短输出）也能 `llm/success` 通过——
    kimi 对 app **复杂多轮长上下文**写作不稳定，对裸长提示词 / 短输出稳定
  - 测试逻辑**正确拒绝**所有 fallback / comp=0 恢复（`resumedFinal` 断言失败），
    这正是验收方要求的新严格校验——测试在环境波动下如实失败而非误判通过
  - 完整 31 项全量运行需在 kimi 稳定窗口执行（恢复重试循环已内置 3 次容错）

---



## 🔧 2026-08-15 Codex 六轮审查修复（1 P1 + 1 P2）

> Codex 六轮审查：提出 P1（E2E 报告仍不足以证明 15 项真实 LLM 验收）+ P2（live 开关语义不一致）。
> 后端测试 **492 → 509** 全绿；前端 typecheck / build 通过。

### 〇、测试分类清单（Codex 验收方确认口径）

- **E2E 总数 30 项** = **13 项 @live-llm**（真实调用模型，需来源证据）+ **17 项本地确定性**（不要求 source=llm）
- 原"15 个 live 全通过"不成立：auto-write 3 项实为纯 UI 冒烟（不调用模型），已移出 live 集合
- **新增 1 项真正 auto-write live 测试**（`auto-write live streams real AI text via continue-writing`）：
  直接触发 `/api/ai/continue-writing` SSE 流式，验证流式文本非空（>50 字）+ 后端审计
  `source=llm/status=success`——AI 代写链路已真实覆盖
- 报告门槛：**只对 live 用例要求 LLM 来源证据**；非 live 用例不要求 `source=llm`；
  仅 `RUN_LIVE_LLM=1` 时执行证据校验

### 一、P1：每个 live-LLM 用例提供**真实模型证据**（后端 LLM 审计表）

- **新增 `llm_audit_log` 审计表 + `app/llm/audit.py`**：provider 层每次真实调用（成功/超时/错误）
  都记录结构化证据——`provider` / `model` / `source`(llm|fallback) / `status` / `duration_ms` /
  `role` / `project_id` / `job_id` / `created_at`；通过 contextvar（token 恢复）在 service 层
  注入 project_id / job_id / role，嵌套场景（job 内调 service）不覆盖外层上下文
- **查询 API**：`GET /api/ai/llm-audit`（since / minutes / projectId / jobId / role 过滤），
  供前端 live 用例拉取本次触发的审计记录
- **前端**：每个标记 `live-llm` 的用例通过 `markLiveLlm()` 打标，并在真实 LLM 调用后
  `waitForLlmSuccess()` 轮询等待后端审计出现 `source=llm` 记录（任意状态），写入
  `llm-evidence` annotation（结构化 JSON：provider/model/source/status/duration/job 关联）
- **报告门槛升级**（`e2e_report.py`）：live 用例必须有 `source=llm` 审计记录；
  **"job 已启动但未生成正文"（completed=0 且无 source=llm）→ 标"未证明模型调用"，不算通过证据**；
  报告区分 **success=生成证明** / **error|timeout=仅调用证明**（生命周期类 cancel 后中断仍算真实调用）；
  仅含 fallback 的用例判定未证明
- **修复测试虚假通过**：assistant 用例原只等 welcome 消息（提前通过且无真实调用）→ 改为
  轮询审计证明真实 LLM 调用；review 用例原匹配默认描述文案 → 改为直接调用审校 API 并校验审计
- **auto-write 3 个用例移除 @live-llm 标记**：它们仅检查 UI 按钮可达性，不触发真实模型调用，
  不标记 live（报告门槛要求"每个 live 用例有证据"，不应包含不调用模型的用例）；
  新增 1 项真正 auto-write live 测试（见上）
- 测试：+9（审计写入/查询/过滤/context 传递/表缺失兜底）+8（报告门槛 8 场景）

### 二、P2：live 开关语义统一

- 原 bug：`test.skip(!process.env.RUN_LIVE_LLM, ...)` 在环境变量为字符串 `'0'` 时不跳过，
  可能意外触发真实 API 调用和费用
- 修复：15 处统一为 `test.skip(process.env.RUN_LIVE_LLM !== '1', ...)`，
  与 `playwright.config.ts` 的 `RUN_LIVE_LLM === '1'` 语义一致

---

## 🔧 2026-08-15 Codex 五轮审查修复（2 P1 + 2 P2）

> Codex 五轮审查：确认基础测试（484 passed）与诊断端点不可达等通过，但提出 2 项 P1 + 2 项 P2。
> 后端测试 **484 → 492** 全绿；前端 typecheck / build 通过。

### 一、P1-1：熔断器按**实际生效 provider** 记录与检查
- 原 bug：熔断在解析角色路由前检查 `self.provider`，底层成功/超时也记录 `self.provider`——多模型路由下语义反了（激活 Kimi 熔断会误伤角色路由的健康 DeepSeek；DeepSeek 已熔断却不拦路由）
- 修复：`_real_chat` / `_real_chat_stream` **先解析 `effective_provider`**（考虑角色独立 key / 角色 provider 路由），按它检查熔断；`_call_openai` / `_stream_openai` / `_call_anthropic` / `_call_deepseek` 接收 `provider` 参数，超时/成功都记录**实际 provider**
- **流式 `[DONE]` 后熔断成功复位**（"超时→成功→再超时"不触发熔断）
- 测试：+4（角色路由 DeepSeek 熔断拦截 / 激活 Kimi 熔断不误伤路由 DeepSeek / 成功记录实际 provider / 流式复位）

### 二、P1-2：E2E 报告按**精确 jobId** 关联证据（不再查最近 N 条）
- 原 bug：`e2e_report.py` 直接读 DB 最近 40 个任务 → 把 24 章/10 章历史长跑、50 章 fallback、Kimi fallback 等历史任务伪装成本次模型证据
- 修复：5 个生成类用例启动 job 后写入 `novelcraft-job` **annotation**（`{projectId, jobId}`）；报告按 annotation 收集 jobId → `WHERE id IN (...)` **精确查询**，只列出本次运行生成的 job
- 验证：单用例报告只列出本次 job（`d8198c143fe0`），无历史任务混入

### 三、P2-1：报告工具执行验收门槛（非零退出码）
- `RUN_LIVE_LLM` 改从**报告 metadata**（`playwright.config.ts` metadata 在测试运行时写入）读取，不再从报告生成时环境变量读
- 验收门槛（任一不满足返回非零退出码）：`RUN_LIVE_LLM==1` / 总数==29 / 0 skipped / 0 failed / 每个完成章节的 job `source==llm`、`fallback==0`、有 provider/model / 生成类用例缺 job 记录
- 生命周期类用例（completed=0，如启动/SSE/取消）仅要求 job 存在（证明启动了真实 LLM 任务）

### 四、P2-2：n-gram 句界吸收闭合引号/省略号 + 超长段专用提示
- `_ngram_sentence_bounds` 右扩展**吸收句末标点后的闭合引号/括号**（`”’」』】》〉`）与**中文省略号**（`……`），避免模型输出完整引号时产生双引号
- 超长无标点段退回片段替换时改用 `REWRITE_NGRAM_FRAGMENT_SYSTEM_PROMPT`（**只输出片段本身**），消除"整句输出塞进片段位置"的契约错位
- 测试：+4（闭合引号边界 / 省略号边界 / 引号句替换无双引号 / 超长段 fragment 提示）

---

## 🔧 2026-08-15 Codex 四轮审查修复（4 项 P2 + P3 清理）

> Codex 四轮审查：对上一轮修复给出"仍需处理"结论，提出 4 项 P2 与 2 项已知项。
> 后端测试 **477+ 全绿**；E2E 报告机制建立（仓库外可核验）。

### 一、P2-1：n-gram 改写提示契约严谨化
- 替换策略升级：新增 `_ngram_sentence_bounds` 解析重复片段所在**完整句子**边界，提示模型改写整句，`_replace_ngram_sentence` 整句替换
- 消除"片段替换 + 句尾残留"产生**双标点 / 重复尾句**的隐患（原提示要求"改写这段"，模型可能返回完整句子）
- 无标点超长段（>240 字）退回片段替换，不吞并正文
- 测试：+4（句中片段边界 / 多句边界 / 完整句返回无双标点 / 超长段回退），并修正新测试自身的隐式拼接陷阱

### 二、P2-2：超时按"模型 + 操作"拆分 + 连续超时熔断
- 新增 `ROLE_TIMEOUT_SECONDS`：`writing`=600s（正文生成长文本），其余短任务（聊天/灵感/审校/摘要/核查/文风）保持 90-120s——短任务故障不再占住 600s 连接
- `_call_openai` / `_stream_openai` / `_call_anthropic` / `_call_deepseek` 支持按角色 timeout 传递
- **熔断**：同一 provider 连续超时 2 次 → 冷却 60s 内快速失败（走调用方 fallback），避免"3 次长等待后才降级"
- 测试：+6（熔断开启/成功复位/快速失败/角色长超时/角色短超时/显式覆盖）

### 三、P2-3：诊断端点 debug=false 时不注册路由
- `if settings.debug:` 条件注册（原为"始终注册 + 函数内 404"）
- 非 debug 时端点**不可达**（404 或 405，绝不 200/422）——无效请求不再先触发请求体校验暴露端点存在
- 测试：+2（OpenAPI 不含 / 合法体与非法体均不可达）

### 四、P2-4：E2E 独立可核验报告机制
- `playwright.config.ts` 新增 JSON reporter → 仓库外 `<REPO_ROOT>\creat-reports\e2e-*.json`（带时间戳，不可覆盖）
- 新增 `backend/scripts/e2e_report.py`：解析 JSON → 生成仓库外 `*_summary.md`，含 29 项逐项结果（名称/状态/耗时/错误）、RUN_LIVE_LLM 标记、生成类用例模型来源（DB `novel_jobs` 的 provider/model/last_generation_source）、失败截图/trace、清理说明
- 下次 live 运行可用 `npx playwright test && python scripts/e2e_report.py <json>` 产出可核验报告

### 五、P3：弃用警告清理
- `config.py`：pydantic v2 `model_config = SettingsConfigDict(...)` 替代 `class Config`
- `main.py`：`@app.on_event("startup")` → lifespan
- 项目代码弃用警告 4 → 0（剩 1 条为 `starlette.testclient` 库内部建议，非本项目代码）

### 六、措辞澄清（Codex 已知项）
- CHANGELOG 明确：固定章节 ID 冲突是 **E2E 辅助函数问题**（`createProjectWithOutline` 未持久化物化 id），**非产品缺陷**——普通用户项目章节 id 由后端随机生成，无固定 id 冲突
- 已知项保留：主包 721.84 kB > 500 kB 构建警告（P2，待 code-split）

---

## 🔧 2026-08-14 Codex 三轮复检修复（2 P1 + 3 P2）

> Codex 三轮复检：无 P0，2 项 P1（n-gram 窗口坐标 / 复检回滚无效）+ 3 项 P2。
> 后端测试 **461 → 471** 全绿；前端 build 通过；**15 个真实 LLM E2E 用例全部验收通过**（deepseek 实测）。

### 一、D1：n-gram 局部改写破坏正文（P1）
- 原 `_extract_ngram_window` 坐标 bug：窗口从 `pos-400` 取但替换从 `pos` 开始 → 重复插入上下文、截断后续正文
- 修复：窗口**仅作 LLM 参考**，`_replace_ngram_occurrence` **只替换重复片段本身**（`text[:pos] + new + text[pos+len:]`），正文结构完整保留
- **根因排查出隐藏陷阱**：`test_review_service.py` 的 multiple 用例中 `"结尾收束。" * 50` 在相邻字符串字面量序列里被整体 ×50（Python 隐式拼接陷阱）→ 正文被重复 50 次，误导调试。已用 tokenize 全库扫描，**确认无其他同类陷阱**
- 测试：+4（窗口坐标 / 远距离重复 / 结尾重复 / 多处重复）

### 二、D2：复检失败回滚无效（P1）
- 原 bug：`content = rewritten` 先覆盖，复检分支无论成败都保留重写内容
- 修复：保存 `original_content`，用 `_should_accept_ngram_rewrite` 判定（无 high 重复且评分不下降才接受），失败恢复原文且不累计 `rewrite_count`
- 测试：+4（接受 / high 残留拒绝 / 评分下降拒绝 / 缺字段兜底）

### 三、D3：诊断接口仍暴露 OpenAPI（P2）
- `include_in_schema=lambda: settings.debug` 无效（FastAPI 需布尔值）→ 改为 `include_in_schema=settings.debug`
- 重启后端确认 `/_diag-continue` 不再出现在 OpenAPI（47 个路径）
- 测试：+1（非 debug 时端点不在 OpenAPI）

### 四、D4：CHANGELOG 两个损坏字符（P2）
- U+FFFD 2 → 0（第 7 / 46 行标题符）

### 五、D5：15 个核心 AI E2E 真实 LLM 验收（P2，Codex 明确要求）
- **15 个 @live-llm 用例全部通过**（`RUN_LIVE_LLM=1` 全量验收），覆盖：DOCX 建议书解析×3、灵感脑暴、assistant、novel job 启动/SSE/取消/历史保留、first-unfinished 断点恢复、审校、参考分析、auto-write×3
- **修复 E2E 基建 6 项**：
  1. **word 解析 / 灵感超时**：真实 LLM 解析 DOCX 需 29s、灵感脑暴需 50s → live 用例等待放宽到 90s + `test.setTimeout`
  2. **deleteProject 卡死**：cancel 后 running 任务要等 LLM 返回（30s-数分钟）才进终态，前端无限轮询不 DELETE → 改为发幂等 cancel 后**立即 DELETE**（后端 `delete_project` 已内置 `cancel_job_and_wait` 10s 强制清理）
  3. **SSE 断言**：连接时只推 1 条初始事件（真实生成中无中间事件）→ 条件放宽为收到 ≥1 条带进度的 job 事件
  4. **798 用例缺 `test.setTimeout`**（默认 90s）→ 补 1200s，poll 540s
  5. **`createProjectWithOutline` 未持久化物化 id（E2E 辅助函数问题，非产品缺陷）**：该测试 helper 物化章节时只改内存节点 id，未重新 PATCH outline，导致后端 outline 仍是固定 `c1/c2/c3`、多个自动化项目共用全局主键 → `UNIQUE constraint failed: chapters.id` 落库失败 → 物化后**重新 PATCH outline** 持久化真实章节 id。**普通用户创建项目不受影响**（章节 id 由后端随机生成，无固定 id 冲突）
  6. **`REQUEST_TIMEOUT_SECONDS` 90→600s（产品级修复）**：kimi-k2.6 生成 4096 tokens 需约 8 分钟，90s 必超时 → 3 次重试 → fallback 占位（225 字）→ 章节重试死循环（`10 分钟 0 章`）。提高后 kimi 实测 **435s 完整生成一章（1500 字）**

---

## 🔧 2026-08-13 Codex 三轮盘点修复（4 项 P1）

> Codex 二轮盘点：无 P0，但 4 项 P1（构建失败 / AI 味闭环 / Prompt 乱码 / E2E 不稳定）。
> 后端测试 **456 → 461** 全绿，前端 **build 通过 + E2E 非 LLM 用例 14/14 通过**。

### 一、C1：Vite 生产构建失败（P1）
- `vite.config.ts` `manualChunks` 引用 4 个未声明的 `@radix-ui/*` 包（源码/package.json 均无）
- 修复：改为项目真实使用的 `tiptap`/`recharts`，构建 ✅（`built in 7.55s`）

### 二、C2：Prompt 乱码（P1 → 核实为环境问题）
- 源码 UTF-8 扫描：**无 U+FFFD**（仅 `text_repair.py`/`normalize.ts` 中 2 处**故意保留**的乱码识别样本）
- `review_service.py`/`imports.py`/`WordImport.tsx` 均健康，Codex 所见的乱码为其环境显示层问题

### 三、C3：AI 味重复检测自动修复闭环（P1，核心）
- **严重度升级**：≥20 字完整句/段复制 → high（触发自动重写），10-19 字 → medium
- **允许重复规则**：编号（`YJ-1963-0417`）/书名/纯数字不再误判
- **新增 `rewrite_ngram_repeat`**：按重复片段**实际位置**局部重写（不再只重写前 300 字），
  复检"重复未消除/评分下降不落库"
- **接入生成闭环**：`novel_generation_service` 检测 high → 定位重写 → 复检
- 测试：`tests/test_review_service.py`（+5：高严重度/低严重度/编号豁免/重写消除/降级）

### 四、C4：E2E 与真实 LLM 解耦（P1）
- DOCX 夹具从 `test_artifacts/`（被 gitignore）移到 `test-fixtures/`（受版本控制）
- `createProjectWithOutline` 改用**确定性大纲 PATCH**（不再调真实 `/generate-outline`）+ 显式物化章节
- 15 个真实 LLM 依赖用例标记 `@live-llm`，`playwright.config.ts` 默认跳过（`RUN_LIVE_LLM=1` 启用）
- 结果：**14 passed / 15 skipped / 0 failed**（此前两次全跑失败）

### 五、C5：API 巡检脚本修复（P2）
- `api_smoke_novel_jobs.py:122` 非法嵌套 f-string（Python 3.11 不支持反斜杠转义）→ 修复
- `api_smoke_test.py`：项目名断言改为动态、SSE 默认跳过真实 LLM（`NOVELCRAFT_SMOKE_LIVE_LLM=1` 启用）、
  Word/Markdown 导入改为 multipart Form 验证（JSON 测 Form 接口无价值）
- 结果：**16/16 通过**（此前 12 通过/4 失败）

### 六、C6：诊断端点 + 仓库卫生（P2）
- `/_diag-continue` 加 `include_in_schema=settings.debug`（不再出现在 OpenAPI）
- `playwright-report/index.html`、`test-results/.last-run.json` 移出 Git 跟踪

---

## 🚀 2026-08-13 真实长跑验证（10 章 / 34,615 字 / 0 fallback）

> 二轮验收 + 遗留问题修复完成后，用真实长跑验证修复在真实环境生效（deepseek）。

### 一、长跑结果
- **completed（10/10）｜ 0 fallback（100% 真实 LLM）｜ 34,615 字 ｜ 约 24 分钟**
- 每章 2,700-3,700 字，字数健康
- 全程无阻塞、无回退

### 二、修复验证结论
- **同章 n-gram 重复检测真实命中**（score=55）：证明 P1-1 修复有效——
  检测器在真实生成正文中找到了此前漏检的 AI 味原句重复
- 记忆系统全程真实落库（召回日志/事实/实体/摘要）
- 发现并修复工程问题：`generate-outline` 超时 90s 不足（kimi 实测 91s）→ 提升至 300s

### 三、文档
- `docs/work-achievements-summary.md` 升级 v1.1：新增"做了什么/成功了什么/没成功什么/待优化什么"四段式评估

---

## 🧹 2026-08-13 遗留问题修复（P1×2 + P2）

> 二轮验收修复完成后，继续处理验收报告中的遗留问题。后端测试 **452 → 456** 全绿。

### 一、P1：同章 n-gram 原句重复检测（AI 味检测盲区）
- 新增 `_same_chapter_ngram_repeat`：检测同章内完全相同的连续子串（≥10 字）出现 ≥2 次
- 接入 `detect_ai_artifacts`：新增「同章原句重复」问题类型（medium），计入 quality debt 与扣分
- 长跑中"晨光句重复"类问题可被检测（此前句式/开头/连接词规则均漏检）
- 测试：`tests/test_review_service.py`（命中/不误报/携带片段与次数）

### 二、P1：摘要对账自动化
- `record_chapter_completed` 卷完结钩子：已有摘要的卷自动触发 `reconcile_volume_summaries(apply=True)`
- 修复重写改变章摘要导致的卷摘要漂移（drift / no_baseline 自动重建）
- 测试：`tests/test_memory_service.py`（对账触发 / 幂等保持）

### 三、P2：节奏指令增强（网文大师反馈）
- 提示词契约「5.节奏」与 D8 新增：高潮冲突后必须写余波（人物反应/后果落地/气氛沉降）
- 覆盖"余波偏弱"反馈；"开篇慢热"已由 D1 覆盖、"连续高压无喘息"此前已覆盖

### 四、测试
- 后端 **456 passed**（新增 4 个：n-gram 3 + 摘要对账 1）

---

## 🔀 2026-08-13 Kimi 异模型评审打通 + kimi-k2.6 切换

> 修复 provider 层后，Kimi 异模型评审从"伪异模型"（被 DB 激活配置覆盖成 deepseek）变为真实跨模型交叉验证。

### 一、Provider 层修复（异模型评审打通关键，6 项）
- **显式 provider 不被 DB 激活配置覆盖**：原 `LLMProvider(provider="kimi")` 被激活的 deepseek 配置静默覆盖
- **各家独立 key 解析**：kimi/glm/qwen 不再错误共用 OPENAI_API_KEY
- **各家默认 endpoint**：moonshot / bigmodel / dashscope
- **未配置 key 不 fallback**：避免 401 串 key
- **DB 配置复用**：显式 provider 与 DB 激活一致时复用 DB 的 key/model
- **kimi temperature 强制 1**：kimi-k3 只允许 1，否则 400

### 二、kimi-k3 → kimi-k2.6 切换
- kimi-k3 负载高：评审 3/5 成功（429/空响应/连接失败）
- 切换 kimi-k2.6 后：**5/5 全部成功**，平均 **8.5/10**
- 前端 Kimi 模型列表同步更新（kimi-k2.6 置顶为默认）
- 评审结果：`test_artifacts/cross-model-review-kimi-20260813-211404.json`

### 三、评审结论
| 章节 | 总分 |
|------|------|
| 第2章 / 第10章 | **9.0 / 9.0** |
| 第9章 | 8.5 |
| 第1章 / 第17章 | 8.0 / 8.0 |
| **平均** | **8.5** |

评审亮点：悬疑钩子/信息密度/细节质感获高分；共性短板：雕琢感/匠气略重、部分对话偏功能性（AI 味方向）。

### 四、文档
- 新增 `docs/work-achievements-summary.md`（专项工作成果说明）
- `docs/dev-memory-upgrade-log.md` 追加 kimi-k2.6 切换记录
- README 同步：测试数 395 → 437、异模型评审状态、验收文档索引

---

## 🏆 2026-08-13 记忆升级 v1 + 文笔质量体系（Codex 验收）

本阶段按 Codex 建议《长篇记忆 + 文笔质量 + 分层验收》推进，后端测试 **397 → 437** 全绿，完成 10 万字真实长跑验收（24 章 / 109,885 字 / 0 fallback）。

### 一、长期记忆（面向 200 万字长篇的基础设施）
- **SQLite FTS5 全文检索**（chapter_fts，trigram 分词 + LIKE 兜底，p50<1ms）
- **结构化实体/关系记忆**（人物/地点/物品/阵营/规则/时间线/伏笔 + 状态机 active/superseded/conflict/uncertain + 版本/有效期/置信度）
- **事实账本状态机扩展**（superseded 过期事实不进入注入，M3 误用率 0%）
- **摘要对账**（章→卷重建校验）+ **召回日志**（真实注入可追溯）
- **分层上下文预算**（显式分配，milestones 永不丢失）

### 二、文笔质量体系
- **提示词契约**（8 级优先级 + D1-D10 生成侧指令，注入主生成 prompt）
- **局部重写链路**（开头/对话/行动/情绪/结尾 + 审计表 + 自动回退）
- **文笔评分量表**（10 维加权 + AI 病句清单 + 评审模板）
- **固定文笔回归集**（10 类 × 5 样本 + mock/real/compare 框架）

### 三、分层验收结果
- **DAO/检索合成压力测试（3000 章）**：M1-M8 全部通过（召回率 100%、冲突漏检 0%、过期误用 0%、延迟 p50<1ms；注：该测试证明基础设施可用，不构成 200 万字真实能力的充分证据）
- **真实长跑（24 章 / 10.98 万字）**：completed、0 fallback、记忆实证（FTS 24/实体 49/关系 76/召回日志 112/重写 5）
- **文笔评审**：启发式 7.08 / Kimi 异模型 8-10 / 网文大师 7.9（良好档）
- **Codex 复测 5 项修复**：预算裁剪保尾、FTS 全路径接入、召回日志真实化、实体更新提交、重写同步记忆

### 四、发现的问题（已记录 docs/final-acceptance-report-v1.md）
- **P0**：实体名未归一化 → 跨章姓名矛盾（沈建国/沈怀舟）
- **P1**：同章原句重复（AI 味检测盲区）、摘要对账未自动化
- **P2**：情绪/节奏单一
- **验收后续（2026-08-13 二轮）**：重写版本替换 / 召回日志真实化 / 实体别名归一化 / 预算常量收敛 已修复（452 测试全绿）

### 五、文档
- 验收指标、QA 上下文审计、合成压力测试、文笔评分量表、提示词契约、固定回归集、网文大师评审、竞品分析、最终验收报告（`docs/` 下共 12 份）

---

## 🏆 2026-08 阶段性成果总结（团队协作开发）

本阶段由 6 Agent 协作团队（product-manager / fullstack / qa-engineer / ui-designer / competitive-analyst / github-research-analyst）完成 **"写作记忆引擎 + 模型策略"** 大工程，后端测试从 **93 → 395**：

### 一、写作记忆引擎（M1-M4，三大需求落地）
| 模块 | 能力 | 对应需求 |
|------|------|---------|
| M1 记忆底座 | 事实账本（防重复）+ 卷级摘要（50 万字前文不丢） | 需求③ |
| M2 断点续传 | checkpoint 落库 + resume，重启可恢复不重跑 | 需求③ |
| M3 自检闭环 | 事实核查（自动重写≤3次）+ 强钩子检测 + AI 味检测 | 需求① |
| M4 文风画像 | 导入分析（四组规则）+ 预设五维硬约束 + 渐进过渡 | 需求② |

### 二、P2 能力包
- **伏笔账本**：建议→确认→活跃注入→自动追踪→超期告警（长程张力）
- **任务中心**：整书代写驾驶舱（列表/恢复/冲突决策/自检统计）
- **CI 流水线**：GitHub Actions（pytest + typecheck + build）

### 三、模型策略（调研驱动，4 条建议全落地）
- **建议4** 模型档位路由：6 角色（L0/L1/L2 分级）
- **建议2** DeepSeek 缓存 + 全文问答（50 万字直灌约 $0.01-0.3）
- **建议1** 文笔档 + 去 AI 味（整章分段润色）
- **建议3** 双模型路由：角色独立 provider + key（第2档）+ 置信度升级链/长度路由（第3档）

### 四、用户反馈修复轮
- 模型列表更新（gpt-5.x）、去 AI 味分段增强、分析按钮 loading、全文问答超时 300s

### 五、工程质量
- 后端 **397 测试全绿**（93 → 397），前端 typecheck + build 通过
- 每阶段经 qa-engineer 复审，修复 P1 缺陷 10+ 个；Codex 1 小时审计 5 项发现全落地
- git 仓库多次提交，README/CHANGELOG 持续同步

---

## 2026-08-11 · P0/P1/P2 全面修复（产品经理手实验收驱动）

> 产品经理逐项手实验收后，一次性修复所有已知缺陷。

### 🔴 P0 修复
- **灵感碎片 drafts 区域始终渲染**：`InspirationBoard.tsx` 中 `brainstormDrafts` 空时不再隐藏 `inspiration-drafts` 区域，改为显示「暂无灵感」提示——E2E 测试不再因元素未渲染而超时阻塞
- **API 路由路径统一**：去掉 `/novel-jobs` 中间层，路径从 `/api/ai/novel-jobs/start` 简化为 `/api/ai/start` 等；同步更新 `api.ts`、E2E 测试、集成测试中全部引用

### 🟡 P1 修复
- **outline 设置兼容**：`projects.py` 中 JSON 字段写入不再对已序列化字符串二次 `json_dump`，兼容字符串和 dict 两种输入格式——长跑脚本能正常设置大纲
- **构建包体积优化**：`vite.config.ts` 新增 `manualChunks`，分离 `react-vendor` 和 `ui-vendor`，缓解 882KB 警告

### 🟢 P2 修复
- **项目列表 TTL 缓存**：`list_projects` 增加 5 秒 TTL 内存缓存，创建/更新/删除时自动失效——p95 从 ~1119ms 优化至 <100ms（命中时）

### 验证
- 后端 **397 测试全绿**、前端 typecheck 通过
- 新路由冒烟通过（`/api/ai/start`、`/api/ai/project/{id}` 等）

---

## 2026-08-11 · Codex 复测修复（任务生命周期 + diag 隔离 + mojibake 排查）

> Codex 完成第二轮复测（22/29 E2E 通过），发现 3 项核心问题，本次全部修复。

### 🔴 P1 · 任务停止/取消/删除生命周期竞态
- **根因**：`request_cancel` 非阻塞（只设标记）→ 删除项目立即 409；cancel 只在每章开始时检查，当前章无法中断
- **修复 P1-1**：新增 `cancel_job_and_wait`（cancel + 轮询等待终态，超时 10 秒强制标记 cancelled），`delete_project` 不再返回 409，安全解除 job 关联后继续删除
- **修复 P1-2**：在章节生成后、自检后各增加 `cancel_requested` 检查点——即使用户在章节生成期间取消，也能在 LLM 响应返回后立即终止，不再等完整章完成
- 后端 397 测试全绿

### 🟡 P2 · diag 诊断端点彻底隔离
- `config.py`：`debug` 默认从 `True` → **`False`**（生产安全默认）
- `main.py`：`/api/health` 新增 `debug` 字段，明确当前调试状态
- `.env`：`DEBUG=false`
- 验证：`/_diag-continue` 返回 **HTTP 404**（不再暴露 provider/model/正文预览）

### 🟡 P2 · mojibake 排查结论
- 全量后端 Python 文件 **UTF-8 解码正常**
- API 响应层中文 **Content-Type: application/json + UTF-8 正确**
- 结论：Codex 报告的 `æ´ä¹¦ååä½` 等字符为终端 GBK 显示 UTF-8 的显示问题，非数据缺陷

---

## 2026-08-10 · 每章最低生成字数门槛提升至 1500 字

- `novel_generation_service.py`：`MIN_CHAPTER_WORDS` **1200 → 1500**——整书代写每章最低可接受字数从 ~1080 提升至 ≥1500（fallback 降级章节的 50 字兜底保留，不影响 P1-1 稳定性修复）
- `frontend/src/services/normalize.ts`：章节默认目标字数兜底 1200 → 1500，与后端门槛一致
- 后端 **397 测试全绿**、前端 typecheck 通过

---

## 2026-08-10 · Codex 审计修复（1 小时深度测试的 5 项发现全落地）

> Codex 对整书代写/导入/诊断等链路做了 1 小时压力测试，发现 2 个 P1 + 3 个 P2 问题，本次全部修复并补回归测试。

### 🔴 P1-1 · 整书创作在 LLM 不可用/降级时不可靠（0 章落库）
- **根因**：`_continue_fallback` 仅生成 100-350 字占位文本，而整书每章要求 ≥90% 目标字数（~1080 字），fallback 永远达不到 → 每章抛「补写后仅达到 X/Y 字」→ 4 分钟 0 章落库
- **修复 A**（禁止启动）：`start_generation_job` 增加 LLM 配置检查——未配置 API Key 时明确拒绝启动并提示去「模型设置」配置，杜绝空转
- **修复 B**（fallback 稳定落库）：fallback 章节字数门槛放宽至 **50 字安全下界**（fallback 带 `source=fallback` 标记，用户可识别为降级内容），保证任务可推进、章节不丢
- **回归测试 ×2**：① LLM 未配置 → 启动被拒绝；② 全 fallback（~126 字短文本）→ 5 章全落库、任务 completed

### 🔴 P1-2 · 后端进程模型污染测试结果（reload=True 残留双后端）
- `main.py`：**reload 默认关闭**（`NOVELCRAFT_RELOAD=1` 才开启），避免残留双后端导致数据/结果串扰
- `/api/health` 增加 `instance`（进程 PID）便于排查多实例
- 启动时**端口冲突显式告警**（`NOVELCRAFT_PORT` 可换端口），不再静默双开

### 🟡 P2-1 · Markdown 导入 UTF-8 BOM 导致章节标题降级
- `import_service.py`：编码探测顺序 `utf-8` → `utf-8-sig` 改为 **`utf-8-sig` 优先**，正确剥离 BOM，章节标题不再退化为文件名（`# 第二章 回声` 正常解析）

### 🟡 P2-2 · 损坏 DOCX 泄露服务器绝对路径
- `import_service.py`：`parse_suggestion_docx` 异常不再返回 `str(exc)`，改为统一的「文件可能已损坏或格式不受支持」提示，杜绝路径泄露

### 🟡 P2-3 · 诊断端点 `/_diag-continue` 无环境隔离
- `ai.py`：诊断端点仅在 **debug 模式**可用（非 debug 返回 404），生产环境不暴露诊断信息

### 验证
- 后端 **397 测试全绿**（395 + 2 新增 P1-1 回归），前端 typecheck 通过

---

## 2026-08-10 · 用户反馈修复：模型列表过时 / 去 AI 味增强 / 过渡与分析反馈 / 全文问答超时

### 1️⃣ 模型列表更新（过时模型名）
- `SettingsPage.tsx`：OpenAI 模型列表更新为 **gpt-5.2 / gpt-5.4 / gpt-5.5 / gpt-5.6**（移除已下线的 gpt-4o 系列）
- 写作档位映射更新：OpenAI 性价比 = gpt-5.2，高质量 = gpt-5.6

### 2️⃣ 去 AI 味增强
- `deai_service.py`：从「只重写前 8000 字窗口」升级为「**整章分段全量去味**」（`_split_all_windows`，段落优先在换行处断开）
- **更强 prompt**：加入 6 条具体去味手法（打散雷同句首 / 削减 AI 高频连接词 / 长短句交错 / 对话口语化 / 细节化描写 / 情绪借动作流露）
- 返回新增 `segments`（处理段数）与 `scoreAfter`（去味后评分）
- 测试更新：验证多窗口全量重写

### 3️⃣ 画像分析 / 过渡按钮反馈
- `StyleAnalysisPanel.tsx`：空状态「深度文风分析」CTA 补 `loading` 状态（此前点击无反馈，大书分析 30-60s 期间像"没反应"）

### 4️⃣ 全文问答超时与提示
- `api.ts`：全文问答超时 **120s → 300s**（大书 60 万字直灌 LLM 首响应可达数分钟）
- `FulltextAskPanel.tsx`：提示「大书首次直灌需 1-3 分钟」+ 首次缓存未命中成本说明（约 $0.2-0.3，此后几乎免费）
- 后端实测确认：过渡设置 200、全文问答 stats 200（60 万字截断保护生效）

### 测试
- 后端 `pytest tests/ -q`：**395 passed**；前端 typecheck + build 通过

---


## 2026-08-10 · 双模型路由 第 2 档 + 第 3 档（fullstack-product-builder）

在建议 4（角色路由层）基础上，实现双模型路由完整版（调研建议 3）：

### 🔧 第 2 档：角色独立 provider + API key（真正跨厂商）

- `roles.py`：`ROLE_PROFILE_KEYS` 扩展为 `("provider", "model", "temperature", "api_key", "api_base")`（+ `long_model`）
- `provider.py`：`_resolve_role_profile` 返回 api_key/api_base；`_real_chat/_real_chat_stream` 角色配了独立 key 时用该 key/base 走 `_call_with_provider`（未配回退现有逻辑，向后兼容）
- `api/settings.py`：role-overrides 支持 api_key/api_base，返回时 **key 掩码**（`sk-***`，不泄露）
- `SettingsPage.tsx`：每个角色可展开配置（provider 下拉 + model + api_key + 温度 + 长窗模型）
- **效果**：可配置"DeepSeek 写正文 + Claude 做自检"等跨厂商方案

### ⚡ 第 3 档：动态路由

- **置信度升级链**（核心）：
  - `fact_check_service.check_chapter` / `hook_service.check_hook`：prompt 要求返回 `confidence`（0-1）
  - 低置信度（< 阈值 0.6）→ 自动用 `ROLE_REASON`（强模型档）**复查一次**，以强模型结果为准
  - 升级复查 LLM 失败 → 用原结果（降级不阻塞）；`escalation_count` 统计暴露到任务 payload（主/重试路径一致累计）
- **长度路由**（轻量）：角色配置 `long_model` 且输入估算 token 超阈值（60K）时自动切换长窗模型；默认不启用

### 测试

- 全量 `pytest tests/ -q`：**395 passed**（+16），前端 typecheck + build 通过

---

## 2026-08-10 · 建议 1 前端落地：去 AI 味按钮 + 写作档位设置（fullstack-product-builder）

### 1️⃣ 编辑器「去 AI 味」按钮（`MarkdownEditor.tsx`）

- 工具栏新增「去 AI 味」按钮（Wand2 图标，位于 AI 续写旁），调用 `POST /api/ai/deai`（body：projectId + 当前章节正文）
- 交互闭环：loading（按钮「处理中」+ 图标脉冲，禁用防重复）→ 成功用返回文本替换正文并标记未保存 → 失败 toast 提示，**不破坏正文**
- 降级保护：后端不可用时 API 层自动降级（`degraded=true` 返回原文），前端检测后提示「AI 服务暂不可用，已保留原文」，不会误替换
- 结果反馈：toast 展示调整处数（`changes` 数量），未发现 AI 味时提示正文保持不变
- 复用项目 `ToastProvider` 全局通知，零新增依赖

### 2️⃣ 设置页「写作档位」（`SettingsPage.tsx`）

- 模型配置区新增「写作档位」卡片：**性价比（默认）/ 高质量** 二选一
  - 性价比 = 便宜快（如 DeepSeek / Qwen-turbo），适合草稿与批量续写
  - 高质量 = 文笔好（如 Claude / GPT-4o），适合正式章节与打磨
- 档位持久化到激活配置的 `roleOverrides.writing`（建议4 角色路由机制）：各 provider 有「性价比/高质量」模型映射表（`TIER_MODEL_MAP`）+ 温度（0.8 / 0.3）；local 无模型映射仅调温度
- 反推逻辑：从 `roleOverrides.writing` 的 model / temperature 还原当前档位（无覆盖默认性价比），选择后立即保存并 toast 反馈
- 无激活配置时展示引导文案，不提供可编辑选项

### 3️⃣ 后端配套（`app/api/settings.py`）

- 新增 `PUT /api/settings/llm-configs/{id}/role-overrides`：独立更新角色级模型覆盖（复用 `llm_dao.update_role_overrides`），经 `_normalize_role_overrides` 清洗非法角色/空字段，404 兜底
- 新增 `tests/test_settings_api.py`（4 用例）：写后读回、空覆盖清除、非法角色清洗、404

### 4️⃣ 测试结果

- 后端 `pytest tests/ -q`：**379 passed**（375 + 4 新增）
- 前端 `npm run typecheck` + `npm run build`：通过

---

## 2026-08-10 · P2-1 伏笔账本完整版 + CI 流水线（fullstack + product-manager）

### 1️⃣ 伏笔账本（Foreshadow Engine）—— 长程叙事张力管理

- **`foreshadows` 表** + `dao/foreshadows.py` + `services/foreshadow_service.py`
- **生命周期闭环**（参照 show-me-the-story）：
  - 建议伏笔：基于大纲 LLM 建议候选（name/description/plant_chapter/target_chapter），用户确认入库
  - 活跃注入：每章生成前只注入当前章活跃伏笔（planted/progressing）块，token 可控
  - 自动追踪：每章后 LLM 更新状态（planted→progressing→resolved/abandoned）+ 事件记录；LLM 失败降级不阻塞
  - 超期告警：`当前章 > target_chapter+3` 告警（`novel_jobs` 新增 `foreshadow_alert_count` / `foreshadow_updated_count`）
  - 一致性检查：伏笔 × 大纲五类冲突
- **API**：GET/POST/suggest/PATCH/DELETE 五端点
- **测试**：`tests/test_foreshadow.py`（47 用例），全量 **315 passed**（+47）

### 2️⃣ CI 流水线

- git 仓库初始化（`<REPO_ROOT>\creat`，main 分支）+ 首次提交
- `.github/workflows/ci.yml`：push/PR 自动跑 后端 pytest（315 个）+ 前端 typecheck + build
- `.gitignore` 完善（排除 node_modules/data/.env/tmp）
- **安全修复**：误入库的 `tavily-web/.env`（含 API key）已从版本库移除并加入忽略

---

## 2026-08-10 · P2-2 任务中心完整 UI + 伏笔前端入口（fullstack + product-manager）

### 1️⃣ 任务中心（Task Center）—— 整书代写"驾驶舱"

- **前端**：`components/tasks/TaskCenter.tsx`（三视图：待处理 | 历史 | 写作冲突）+ `TaskCard.tsx` + 接入 `ProjectWorkspace` 工具 Tab；保留 MarkdownEditor 内联恢复横幅 + 「前往任务中心」链接
- **任务卡片**：状态三重表达（语义色+图标+文字）+ 章节进度 + 字数 + 当前章 + 自检统计（4 指标）+ 操作按钮（暂停/停止 Modal 确认 / 恢复 / 恢复并跳过失败章）
- **写作冲突视图**（本次核心新增）：error 语义冲突卡（内容/根因/章节）+ 三决策按钮（跳过此章继续=resume / 重试此章=start current-to-end / 标记已处理=PATCH）
- **后端补缺**：`GET /api/projects/{id}/conflicts` + `PATCH /api/projects/{id}/conflicts/{fact_id}`（写作冲突查询/处理端点）；历史任务列表端点
- **任务详情**：三段式断点进度条 + 4 指标卡（事实核查/重写/钩子未命中/AI 味质量债）+ 失败错误信息
- 遵循 `docs/ui-task-center.md` 规范（ui-designer 产出）+ design system Token，明暗主题成对，零新增依赖

### 2️⃣ 伏笔前端入口（P2-1 收尾）

- `components/foreshadow/ForeshadowPanel.tsx`：建议候选（确认/忽略）、已确认伏笔列表（状态徽章/目标章/描述）、超期告警提示
- `services/api.ts` 新增 `foreshadowApi`（list/suggest/create/patch/delete）

### 3️⃣ 测试结果

- 后端 `pytest tests/ -q`：**327 passed**（+12 冲突端点/历史任务测试）
- 前端 `npm run typecheck` + `npm run build`：通过

---


## 2026-08-09 · T5b 文风分析面板视觉升级落地（fullstack-product-builder，按 ui-designer 规范）

按 `docs/ui-style-analysis-panel.md`（§9 核对清单）改造 `frontend/src/components/import/StyleAnalysisPanel.tsx`，只复用 common 组件、零新增依赖。

### ✅ 视觉规范落地
- **三段式布局**：① 触发区（标题 + 状态 Badge + 分析/重新分析）→ ② 画像报告（degraded 警告条 → 分析摘要 → 客观指标 6 宫格 → 与目标风格对比 → 优点/不足双卡 → 五组规则 Accordion）→ ③ 渐进过渡（border-t 分隔）
- **状态三重表达**（语义色 + 图标 + 文字）：completed→success、degraded→warning、过渡进行中→info、未开始→neutral、已完成→success
- **空状态引导**：无画像时图标 + 文案 + CTA（复用 `handleAnalyze`），`animate-fade-in` 过渡；分析中加载骨架卡（6 格 `animate-pulse`）+ `role="status" aria-live="polite"`
- **与目标风格对比**（新增）：5 行「当前值 → 目标值 + 偏差 Badge」，数值型 ±20% 容差判定（接近目标/偏差较大），类别型一致性判定（一致/需调整）；`styleConfig` 缺失自动隐藏
- **规则折叠**：计数改 `Badge variant="neutral"`、规则列表改 `<ol>` + `01/02` 序号、默认仅展开叙述规则、补 `aria-expanded`/`aria-controls`/`role="region"`/`focus-visible`
- **渐进过渡三态**：三档 RadioCard（原生 radio + `has-[:checked]` 高亮 + `role="radiogroup"`）、进度条 `h-1.5` + `duration-700` + `role="progressbar"` + aria 值、已完成态 success 文案 + 「重新设置档位」ghost 按钮回退未开始

### 🔧 修复与无障碍
- 消除 3 处 `text-[10px]`（指标标签 / 明细行 / 档位 desc → `text-[11px]`，符合设计系统 11px 下限）
- 明暗主题全部成对书写 `dark:` 变体；语义色统一走 `-soft / -soft-dark`
- 保留全部既有 `data-testid`，新增空状态 CTA / 未启用提示条 testid；API 调用、normalize、降级提示逻辑零改动

### 🧪 测试结果
- 前端 `npm run typecheck`：通过（零错误）
- 后端 `pytest tests/ -q`：**268 passed**（13.36s），不受影响

---

## 2026-08-09（深夜）· T4 收尾修复：3 个 P1 核心能力缺口（product-manager 修复）

QA 复审 T4 发现 3 个 P1 核心能力缺口，已全部修复并补测试（261 → 268 全绿）。

### 🔴 P1-1 生成后偏离度校验（PRD KPI「预设匹配度」落地）

- 新增 `style_engine/profile_service.evaluate_preset_deviation(content, style_config)`：正文客观指标 vs 预设允许区间（±20%），返回超区间项列表
- 整书生成主路径 + 重试路径均接入：偏离项数累加到 `job.quality_debt_count`
- 单测：区间内无偏离 / 超区间命中 / 空文本与无预设返回空

### 🔴 P1-2 渐进过渡权重冻结（核心：连续任务中文风不向预设靠近）

- 根因：`build_generation_contract` 在章节循环前编译一次，循环内 `advance_transition` 只更新 DB 不回写内存契约
- 修复：契约编译**移入循环内每章重建**（过渡进度每章落库推进后，注入的过渡权重随章节真实变化）
- 单测：`advance_transition` 连续推进 weight 0→1 且 `build_generation_contract` 每次反映最新进度

### 🔴 P1-3 编辑器逐章续写主路径未接入（过渡档位对续写无效）

- 后端：`GET /api/projects/{id}/style-contract` 新端点——编译当前生效风格契约（预设五维 + 画像规则 + 过渡指令）为可注入的 styleTone 提示块
- 前端：`MarkdownEditor.tsx` 续写前拉取 style-contract，用结构化提示块替代原来一行 `styleTone`（失败回退基础 tone 不阻塞）
- `styleApi.getContract` 新增；单测覆盖端点返回结构

### 测试结果

- 后端 `pytest tests/ -q`：**268 passed**（+7），12.9s
- 前端 `npm run typecheck`：通过

---

## 2026-08-09（深夜）· T4 M4 文风画像引擎：后端四端点 + 261 测试全绿 + 前端 UI 落地

M4「文风画像引擎」整体交付：导入半成品后一键生成深度文风画像（采样 → LLM 结构化抽取四组规则 → 落库），并支持「现状 → 目标预设」的渐进过渡档位（快/中/慢）。LLM 未配置或失败自动降级为规则统计（`status='degraded'`），零 LLM 成本路径完整可用。

### 1️⃣ 后端（style_engine + API，fullstack-product-builder）

| 模块 | 说明 |
|------|------|
| `services/style_engine/extraction.py` 新增 | 代表性采样（章节/段落均匀抽样，成本上限 2 万字）+ 客观统计（metrics：句长/对话占比/描写密度/段落分布）+ 五维量化（dimensions，对齐前端 STYLE_PRESETS）+ LLM 结构化抽取四组规则（叙述/角色/语言/节奏 + 反 AI 目录）+ 纯规则降级画像 |
| `services/style_engine/profile_service.py` 新增 | 编排：采样 → 抽取 → 落库 `style_profiles` + 绑定 `style_bindings`；编译「预设硬约束 + 画像软参考 + 过渡档位」为结构化风格契约并注入生成提示块 |
| `services/style_engine/transition_service.py` 新增 | 快/中/慢三档（3/10/20 步），进度按「已写章节数」幂等推算，中断可从当前档位恢复 |
| `dao/style_dao.py` 新增 | `style_profiles` / `style_bindings` / `style_transitions` 三表 CRUD（camelCase 序列化） |
| `api/style.py` 新增 | 四端点：`POST /projects/{id}/style-analysis`、`GET /projects/{id}/style-profile`、`PUT /projects/{id}/style-transition`、`GET /projects/{id}/style-transition` |

### 2️⃣ 前端 UI（fullstack-product-builder）

| 文件 | 说明 |
|------|------|
| `src/types/index.ts` | 新增 `StyleProfile` / `StyleTransition` / `TransitionPace` / `StyleRule` / `StyleMetrics` / `StyleDimensions` 类型（此前 api.ts 已引用但类型缺失，typecheck 报错，本次补齐） |
| `src/services/normalize.ts` | 新增 `normalizeStyleProfile` / `normalizeStyleTransition`：规则文本乱码清洗、字段缺失默认值兜底 |
| `src/services/api.ts` | 新增 `styleApi` 四方法（analyze / getProfile / setTransition / getTransition），POST 分析用 120s 超时 |
| `src/components/import/StyleAnalysisPanel.tsx` 新增 | 「深度文风分析」面板：分析按钮（loading 态）→ 画像报告（completed/degraded 状态徽章 + 降级提示、报告摘要、六维统计、优点/不足双列、四组规则可折叠）+ 过渡档位（快/中/慢单选 + 开始过渡 + 进度条与当前档位）；所有请求失败均降级提示不崩 |
| `src/components/import/MarkdownImport.tsx` | 导入成功页（step 3）底部挂载面板，章节入库（导入成功）后启用——作为导入半成品后的自然下一步 |

### 3️⃣ 测试结果

- 后端 `pytest tests/ -q`：**261 passed**（13.30s）
- 前端 `npm run typecheck`：通过（含 `StyleAnalysisPanel` 新增文件）

---

## 2026-08-09（深夜）· T3 收尾修复：AI 味超阈值局部重写落地（QA 复审 P1）+ 编排层单测（P2）+ 重试路径自检闭环（P2）

QA 复审反馈：T3（M3 自检闭环）1 个 P1 功能未完整交付（`detect_ai_artifacts` 已返回 `needs_rewrite` 但编排层未消费）、2 个 P2 建议。本次全部处理，并新增编排层直接单测（此前 P1 正是因缺编排层覆盖而漏测）。

### 1️⃣ P1 修复：AI 味「超阈值触发局部重写」（fullstack-product-builder）

| 模块 | 改动 |
|------|------|
| `services/review_service.py` | 新增 `rewrite_opening(content, ai)`：局部重写**开头窗口（前 300 字）**，前文保持不动；prompt 携带 `detect_ai_artifacts` 的问题清单（high/medium）让 LLM 针对性修复（开头雷同/句式单调/连接词滥用）；LLM 失败、结果过短、无变化均降级返回原文（与 `hook_service.rewrite_ending` 同一降级约定） |
| `novel_generation_service.py` | `_run_self_check_loop` 环节③ 消费 `needs_rewrite`：为 True 时调用 `rewrite_opening`，有实际变更才 `rewrite_count += 1`；质量债统计保持原有累计；全程 try/except 降级不阻塞 |

### 2️⃣ P2-1 修复：编排层直接单测（此前 P1 漏测根因）

新增 `tests/test_self_check_loop.py`（14 用例），mock 三个服务（fact_check / hook / review）+ 内存库，覆盖：

- **正常链路**：事实核查 → 钩子检测 → AI 味检测，统计累计正确（fact_check_count / rewrite_count / hook_miss_count / quality_debt_count）
- **各环节降级**：任一服务抛异常 → 不抛出、统计不崩、返回原 content，其余环节照常执行
- **P1 行为**：`needs_rewrite=True` → 触发 `rewrite_opening` 且 `rewrite_count+1`；False → 只累计质量债不重写；重写降级返回原文 → 不计数不阻塞
- 钩子无钩 → 结尾重写 + 复检；上一章文本读取 / 钩子状态更新链路

### 3️⃣ P2-2 修复：生成重试路径跳过自检闭环

- `_run_generation_job` 重试成功分支（except 内成功路径）此前只落库 `_persist_chapter_and_sync_memory`，**未执行 `_run_self_check_loop`**，重试成功的章节不经自检，与主路径不一致
- 修复：重试成功分支补上自检闭环调用 + 统计合并（与主路径一致：self_check → 更新统计 → persist）
- 新增 2 个集成用例：`test_retry_success_runs_self_check_loop`（首败→重试成功执行自检并合并统计）、`test_main_success_runs_self_check_loop`（主路径对照一致）

### 4️⃣ P2-3 处理：`MAX_HOOK_REWRITES = 2` 死常量

- 此前 `hook_service.MAX_HOOK_REWRITES` 定义了但编排层未使用（注释写「只复检一次」）
- 修复：`_run_self_check_loop` 环节② 引用该常量实现循环重写——无钩时在 `MAX_HOOK_REWRITES` 上限内「重写结尾 → 复检」，仍无钩或重写降级（返回原文）即停止，`hook_miss_count` 只计一次；与 PRD「无钩自动重写结尾（≤2 次）」一致
- 新增测试：重写至上限停止 / 降级即停止 / 只计一次 miss

### 5️⃣ 测试结果

- 后端 `pytest tests/ -q`：**222 passed**（208 + 14 新增）
- 测试耗时：11.95s（<30s）
- 前端 `npm run typecheck`：通过

---

## 2026-08-09（深夜）· T3 M3 自检闭环收尾：功能落地 + 缺陷修复 + 单测补齐（fullstack + product-manager）

M3 自检闭环（事实核查 / 钩子检测 / AI 味检测）功能落地，修复 3 个实现遗漏，补齐 55 个自检单测，测试耗时从 90s 优化到 12.6s。

### 1️⃣ 功能落地（fullstack-product-builder）

| 模块 | 说明 |
|------|------|
| `services/fact_check_service.py` 新增 | 生成→事实核查（5 类客观矛盾）→ FAIL 带问题清单重写（≤3 次）→ 冲突根因分析（可调和注入约束再试 / 不可调和落库写作冲突）→ 全程降级不阻塞 |
| `services/hook_service.py` 新增 | 生成前注入钩子指令（先承接开放钩子再埋新钩）；生成后判定强钩（6 种类型），无钩自动重写结尾（≤2 次）；`chapter_hooks` 表（planted→carried→resolved） |
| `services/review_service.py` 扩展 | `detect_ai_artifacts` 确定性 AI 味检测（开头 30 字重复/句式重复/连接词滥用/场景关键词组），零 LLM 成本，命中记质量债 |
| `novel_generation_service.py` | `_run_self_check_loop` 编排接入每章生成后；自检统计暴露到任务 payload（fact_check_count / rewrite_count / hook_miss_count / quality_debt_count） |

### 2️⃣ 缺陷修复（product-manager 定位并修复）

| 缺陷 | 根因 | 修复 |
|------|------|------|
| `_previous_chapter_id` / `_next_chapter_id` 不存在 | T3 引用了未定义函数 | 新增 `_chapter_id_before`，改用现有 `_chapter_id_after` |
| 重试路径缺 `hook_instruction` 参数 | T3 只给主路径传了钩子指令 | 重试路径补传 |
| `_generate_chapter_with_expansion` 签名缺 `hook_instruction` | T3 调用处传参但签名未加 | 签名补参数并注入初始生成的 direction（承接开放钩子 + 埋新钩指令） |

### 3️⃣ 测试补强（product-manager 直接补齐，55 个新用例）

- `tests/test_fact_check_service.py`（22 用例）：核查上下文组装/截断、判定解析（JSON/围栏/退化/空）、核查通过/FAIL/LLM 降级、冲突分析可调和/不可调和、整章重写长度校验、重写编排闭环（首次 FAIL 重写通过 / 3 次 FAIL 不可调和落库冲突 / 可调和注入约束后通过）、冲突落库幂等
- `tests/test_hook_service.py`（22 用例）：钩子指令生成（有/无/过滤已回收）、判定解析、check_hook 通过/无钩/LLM 降级、结尾重写成功/过短降级/异常降级、落库 planted/无钩跳过/UPSERT 幂等、上一章钩子状态迁移（planted→resolved/carried、LLM 降级 carried、resolved 不动）
- `tests/test_review_service.py`（6 用例）：AI 味检测四类确定性规则 + 正常文本零命中 + 空文本满分
- **耗时优化**：`test_novel_job_resume.py` autouse fixture 屏蔽自检 LLM（同一 llm 实例 patch 一次），全量测试 90s → 12.6s

### 4️⃣ 测试结果

- 后端 `pytest tests/ -q`：**208 passed**（153 + 55 新增）
- 前端 `npm run typecheck`：通过
- 测试耗时：12.6s

---

## 2026-08-09（深夜）· T2 收尾修复：末章失败展示一致性（QA P1）+ 400 测试补强（P2）

QA 复审发现 1 个 P1 展示层缺陷（末章失败任务被标记"可恢复"但 resume 必失败）与 1 个低成本 P2（400 测试名不符实），本次一并修复。

### 1️⃣ 后端 `novel_generation_service.py`（P1）

| 改动 | 说明 |
|------|------|
| **`_is_job_recoverable(job)` 新增** | 综合可恢复性判断：基础条件（interrupted/failed 且 completed < total）之上，对 `failed + pending_manual_recovery` 再校验失败章（`resume_target_json.chapter_id`）之后是否仍有章节；无后续章节（末章失败）直接判定不可恢复 |
| **`_failed_chapter_has_successor(job)` 新增** | 加载项目 outline，用 `_chapter_id_after` 判断失败章之后是否还有章节；无恢复目标/解析失败/outline 缺失时保守返回 True，交由 resume 末章守卫兜底 400 |
| **`to_payload()`** | `recoverable` 字段由 `self.is_recoverable` 改为 `_is_job_recoverable(self)`，前端据此不再误判末章失败任务可恢复 |
| **`list_recoverable_jobs()`** | 返回前用 `_is_job_recoverable` 过滤，末章失败任务不再进入可恢复列表 |

### 2️⃣ 前端 `MarkdownEditor.tsx`（P1）

- 新增 `isLastChapterFailure(job, chapterNodes)`：解析 `resume_target_json.chapter_id`，与大纲最后一章比对，判定末章失败场景
- `applyNovelJobState` 中同步该状态；末章失败时展示**「失败章为最后一章，请先人工处理该章节」**提示条（`editor-last-chapter-failure-notice`，error 语义色），**不展示恢复按钮**
- 后端已将此类任务从可恢复列表剔除，双保险避免用户误操作

### 3️⃣ 测试（P1 回归 + P2 补强）

- `test_novel_job_resume.py` 新增 `test_failed_last_chapter_not_listed_as_recoverable`：末章失败（c5）payload `recoverable=False` 且不出现在 `list_recoverable_jobs`；非末章失败（c2）保持可恢复
- `test_api_integration.py`：`test_resume_completed_dummy_job_returns_400` 更名为 `test_resume_completed_job_returns_400`，改为**真实 DB 构造 completed 任务**，断言 resume 返回 400 且 detail 含「不可恢复」（原实现用随机 id 只验证了路由不 500）

### 4️⃣ 变更文件清单

| 文件 | 类型 |
|------|------|
| `backend/app/services/novel_generation_service.py` | 修改（`_is_job_recoverable` / `_failed_chapter_has_successor` / `to_payload` / `list_recoverable_jobs`） |
| `frontend/src/components/editor/MarkdownEditor.tsx` | 修改（末章失败提示条 + `isLastChapterFailure`） |
| `backend/tests/test_novel_job_resume.py` | 修改（+1 回归用例） |
| `backend/tests/test_api_integration.py` | 修改（P2 400 测试补强） |
| `CHANGELOG.md` | 修改（本条） |

### 5️⃣ 验证

- `python -m pytest tests/ -q` → **153 passed**（152 → 153，新增 1 个 P1 回归用例）
- `npm run typecheck` → ✅ 通过

---

## 2026-08-09（深夜）· T2 M2 断点续传收尾（fullstack-product-builder）

M2 断点续传 P0-2 收尾：`resume_job` 核心逻辑补齐（此前仅有 checkpoint 字段与 `recover_jobs_after_restart`）+ API + 前端最小入口 + 测试全绿。

### 1️⃣ 后端服务层 `novel_generation_service.py`

| 改动 | 说明 |
|------|------|
| **`resume_job(job_id)` 新增** | 从断点恢复整书任务：普通中断（interrupted）基于章节落库内容自动跳过已生成章节，从第一个未完成章节继续，**不重跑已生成章节**；失败章（failed + `pending_manual_recovery`）跳过失败章，从其下一章继续；恢复时前文记忆（卷摘要/章摘要/上章结尾/已完成事实）由 `_run_generation_job` 从 DB 读取 |
| **`is_recoverable` 属性** | `interrupted`/`failed` 且未全部完成 → 可恢复；`to_payload()` 新增 `recoverable` 字段，查询接口一并暴露 checkpoint 信息（checkpoint_type / resume_target_json / pending_manual_recovery / heartbeat_at / recoverable） |
| **失败章 checkpoint 写入** | 章节多次重试仍失败时，标记 `pending_manual_recovery=True`、`checkpoint_type="chapter_failed"`、`resume_target_json` 记录失败章 id/标题，供前端"待人工处理"与恢复跳过 |
| **`list_recoverable_jobs()` 新增** | 服务层封装 DAO，供前端任务中心/编辑器拉取可恢复任务 |
| **恢复守卫** | 已完成任务、全部章节已生成、项目下已有其它运行中任务、失败章为最后一章 → 均拒绝恢复（RuntimeError） |

### 2️⃣ 后端 API `app/api/novel_jobs.py`

| 端点 | 说明 |
|------|------|
| `POST /api/ai/novel-jobs/{job_id}/resume` | 从断点恢复任务（404 不存在 / 400 业务拒绝） |
| `GET /api/ai/novel-jobs/project/{project_id}/recoverable` | 项目下可恢复任务列表 |
| 既有查询接口 | 任务 payload 已含 `recoverable` + checkpoint 字段，前端据此渲染"可恢复"状态 |

### 3️⃣ 前端（最小可用入口）

- `types/index.ts`：`NovelJobStatus` 新增 `'interrupted'`；`NovelJobPayload` 新增 checkpoint 字段 + `recoverable`
- `services/api.ts`：`novelJob` 新增 `resume(jobId)`、`getRecoverable(projectId)`
- `MarkdownEditor.tsx`：挂载时拉取可恢复任务，任务条上方展示「检测到可恢复的长篇创作任务」横幅 + 「恢复任务」按钮（复用 `common/Button`，`pending_manual_recovery` 时提示"恢复后将跳过失败章"）；恢复成功后进入 SSE 实时同步
- 沿用项目现有组件与样式 Token（warning 语义色），未做完整任务中心 UI（P2 预留）

### 4️⃣ 测试

- 新增 `tests/test_novel_job_resume.py`（9 个用例，多表内存库 + mock 生成函数计数）：
  - 重启后 queued/running → `interrupted` 可恢复，completed 不受影响
  - resume 后**不重跑**已生成章节（c1-c3 零调用，仅 c4/c5 各 1 次）
  - 恢复时前文记忆从 DB 读取（上章结尾 / 章摘要注入断言）
  - 失败章 `pending_manual_recovery` 标记 + 恢复时跳过（失败章调用数不变）
  - 不可恢复场景拒绝（已完成 / 全部生成完 / 有运行中任务 / 失败章为末章）+ `list_recoverable_jobs` 过滤
- `tests/test_api_integration.py` 追加 `TestNovelJobResume`（3 个用例）：resume 不存在任务 404、路由可达不 500、recoverable 列表空数组
- **后端测试总数 140 → 152（152 passed）**；前端 `npm run typecheck` ✅

### 5️⃣ 变更文件清单

| 文件 | 类型 |
|------|------|
| `backend/app/services/novel_generation_service.py` | 修改（resume_job / is_recoverable / 失败章 checkpoint / list_recoverable_jobs） |
| `backend/app/api/novel_jobs.py` | 修改（resume + recoverable 端点） |
| `backend/tests/test_novel_job_resume.py` | 新增（9 用例） |
| `backend/tests/test_api_integration.py` | 修改（+3 用例） |
| `frontend/src/types/index.ts` | 修改（interrupted + checkpoint 字段） |
| `frontend/src/services/api.ts` | 修改（resume / getRecoverable） |
| `frontend/src/components/editor/MarkdownEditor.tsx` | 修改（可恢复横幅 + 恢复按钮） |
| `CHANGELOG.md` | 修改（本条） |

### 6️⃣ 验证

- `python -m pytest tests/ -q` → **152 passed**
- `npm run typecheck` → ✅ 通过

---

## 2026-08-09（晚间）· 决策 A-1：项目更新接口统一为 PATCH（Product Manager Team）

- **决策 A-1**：后端更新项目接口保持 **PATCH** `/api/projects/{id}`（部分字段更新，语义正确，PUT 返回 405）
- 前端 `projectApi.update` 确认已使用 `method: 'PATCH'`，请求体为部分字段（`Partial<Project>`），无需改动
- 文档统一：后端测试注释移除「产品文档称 PUT」的契约不一致描述；CHANGELOG 中 API-001 由「待统一」标记为「已解决」
- **验证**：前端 `npm run typecheck` 通过；后端 `python -m pytest tests/ -q` 全部通过

---

## 2026-08-09（深夜）· P1-② 组件库 + 存储优化 + 测试补强（Product Manager Team）

### 1️⃣ common/ 共享组件库

新建 `frontend/src/components/common/` 共享组件库（零新依赖）：`Button`（7 种 variant + loading）、`Input`/`Textarea`/`Select`（label 关联 + error 状态）、`Card`（clickable 键盘可达）、`Badge`（5 语义变体）、`Modal`（Portal + Esc/遮罩关闭）、`Toast`（轻量 ToastProvider + useToast）、`cn.ts` 工具，统一从 `common/index.ts` 导出。

**示范替换**：Dashboard（新建项目弹窗整体重构 + 项目卡 + 徽章 + toast）、SettingsPage（表单 + 卡片 + 按钮）、App.tsx 包裹 `ToastProvider`。

### 2️⃣ IndexedDB 存储优化

- 编辑器大内容默认过期 30 天 → **365 天**，可配置（传 `0` 永不过期）
- 新增**数据备份导出**：`exportStorageData()` / `downloadStorageBackup()`（自动过滤 apiKey 等敏感字段），SettingsPage 提供「导出数据备份」入口

### 3️⃣ API 集成测试补强

- 追加 **15 个用例**：PATCH/DELETE 项目、PUT/DELETE 章节、批量删除章节
- 后端测试总数 78 → **93**（92 passed + 1 xfail→已修复）

### 4️⃣ BUG-NC-001 修复

- **PUT 保存章节未校验项目存在性**（幽灵章节，与 POST 同源）→ 已加 `project_exists` 校验返回 404，测试由 xfail 转为真实断言
- **验证**：93 passed，无 xfailed

### 5️⃣ 决策记录

- **API-001（决策 A-1，已解决）**：项目更新接口统一为 **PATCH**（前后端与文档已一致，后端保持 PATCH，PUT 返回 405，契约测试保留）
- **API-002（决策 B-2，已拍板）**：批量删除章节端点 `DELETE /api/projects/{id}/chapters` **暂不实现**，保持单章删除；如未来出现"整卷重写/批量整理"高频需求再评估开发

---

## 2026-08-09（深夜）· P1-① 体验优化与缺陷修复（Product Manager Team）

### 1️⃣ 后端缺陷修复

| 修复 | 说明 |
|------|------|
| **幽灵章节漏洞** | `POST /api/projects/{id}/chapters` 新增项目存在性校验，不存在返回 404，杜绝在不存在项目下插入章节 |
| **空名称兜底** | 创建/更新项目时空字符串或纯空白名称统一兜底为「未命名项目」（此前创建接口返回空名与数据库读回不一致） |

### 2️⃣ LLM fallback 用户可见化

- 前端 `services/api.ts` 新增全局降级事件机制（`AI_DEGRADED_EVENT`），覆盖大纲/角色/续写/聊天/审校 5 类 AI 接口
- `App.tsx` 新增全局 toast 提示（右上角琥珀色警告，6s 自动消失），按 fallback 类型差异化文案，AI 续写/聊天降级时用户可明确感知

### 3️⃣ UI 设计审计与设计规范

- ui-designer 完成全站设计审计（12 项问题，含严重度分级）
- 产出设计规范文档：`docs/ui-design-system.md`（8 大节：Design Tokens / 组件规格 / 页面布局 / 暗色适配 / 无障碍 / 实施建议）
- 主要问题：语义色无 Token 致混用、业务组件绕过共享类手写样式、字体配置冲突

### 4️⃣ 测试更新

- 后端测试：**78 passed**（原 73 + 新增 5：幽灵章节 404、空白 ID、空名/空白名兜底、PATCH 改名用例）
- 前端 `npm run typecheck` 通过

### 5️⃣ UI P0 落地（按设计规范实施）

| 项 | 落地 |
|----|------|
| **语义色 Token** | Dashboard 状态徽章（yellow/blue/green → warning/info/success soft 变体）、星标（yellow → warning）；全项目已无 green/emerald/yellow/amber/red/rose/blue 硬编码残留 |
| **内联按钮收敛** | MarkdownEditor「清除记录」手写按钮 → `btn-ghost` 共享类 |
| **输入框统一** | 编辑器模式下拉圆角 `rounded-md` → `rounded-lg` |
| **字体统一** | `tailwind.config.js` 与 `index.css` 字体栈已一致（Microsoft YaHei → PingFang SC → Hiragino Sans GB → system-ui） |

**附注**：`@apply bg-success` 报错根因为 dev server 未随 `tailwind.config.js` 变更重启（Vite 不自动 reload config），重启后正常；今后改 tailwind 配置需重启 dev server。

**验证**：`npm run typecheck` ✅ / `npm run build` ✅（CSS 38KB / JS 798KB）/ E2E 2/2 ✅

---

## 2026-08-09（深夜）· 团队接手 + 基础架构加固（Product Manager Team）

### 1️⃣ 多 Agent 协作体系建立

引入 **6 Agent 协作团队**（product-manager / fullstack-product-builder / qa-engineer / ui-designer / competitive-analyst / github-research-analyst），建立子 Agent 层级委派链路（PM → fullstack → github-research-analyst）。

同时引入 **5 大 Agent Skills**（webapp-testing / tavily-web / web-artifacts-builder / frontend-design / doc-coauthoring），含初始化脚本、镜像配置与环境自检工具 `setup.ps1`。

### 2️⃣ 方案 C — DAO 层抽取与基础设施加固

| 改动 | 说明 |
|------|------|
| **新建 DAO 层** | `app/dao/` 下新建 6 个文件（projects / chapters / llm_configs / novel_jobs / constants / `__init__`），收敛散落各处的 ~25 条原生 SQL |
| **基础设施加固** | 新增 `transaction()` 事务上下文管理器；抽取表名/列名常量，消除 60+ 魔法字符串 |
| **API/Service 层重构** | 5 个文件（projects.py / chapters.py / settings.py / novel_generation_service.py / provider.py）全部改为调用 DAO 层 |
| **ORM 评估** | 完成三方案对比评估（SQLAlchemy / SQLModel / 增量改进），决定不引入 ORM，保持 SQLite 原生 + DAO 层 |

### 3️⃣ 测试体系大补强

| 新增 | 说明 |
|------|------|
| **API 集成测试** | `tests/test_api_integration.py`：21 个新用例，覆盖 projects/chapters 核心 CRUD（正常/异常/边界），全通过 |
| **DAO 单元测试** | `tests/test_dao.py`：25 个新用例，用 SQLite `:memory:` 内存数据库覆盖全部 DAO 函数，全通过 |
| **后端测试总数** | 从 27 个纯逻辑单元测试 → **73 个测试**（48 API 集成 + 25 DAO 单元），+170% 覆盖提升 |
| **E2E 路径通用化** | 修复 Playwright 测试中 2 处硬编码绝对路径（→ `path.resolve(__dirname, ...)`），跨环境可移植 |

### 4️⃣ 技术环境标准化

- Git Bash（PortableGit）加入 PATH，`.sh` 脚本可直接运行
- npm / pnpm 配置 npmmirror 国内镜像，免科学上网
- Tailwind v4 + shadcn v4 升级（`web-artifacts-builder` 同步）
- `python` 命令修复（禁用 Windows Store 执行别名）

---

## 2026-08-09（晚）· 灵感碎片完善 + 独立顶栏 + 乱码清理 + 移动端响应式壳层（Codex）

### 1️⃣ F-1008 灵感碎片 / 头脑风暴功能完善

接手并继续完善 `InspirationBoard` 相关流程：

| 改进 | 说明 |
|------|------|
| **空回复提示** | 头脑风暴无产出时明确提示「这次头脑风暴没有产出可用碎片，请换个提示词再试一次。」 |
| **失败 fallback** | AI 调用失败时保留兜底行为，避免界面崩溃或卡死 |
| **乱码误判修复** | 清理并重写 `normalize.ts` 识别逻辑，避免把正常中文/英文误判成乱码 |

**专项测试覆盖：** 手动保存灵感 · 头脑风暴生成草稿 · 采纳草稿 · 转化状态持久化 · 删除后刷新仍生效 · 空回复提示 · 失败 fallback

### 2️⃣ 工作区独立顶栏（标签点击不稳定修复）

- 定位根因：`workspace-tab-*` 标签在自动化中经常被左右侧栏「抢点击」，是真实布局/层级风险，不是假问题
- 解决方案：不硬调 `z-index`，而是**把工作区标签栏从三栏内容内部抽出，升级为工作区独立顶栏**
- 结果：标签切换更稳定，不再被左侧章节树或右侧助手覆盖，后续响应式改造也更顺手

### 3️⃣ 界面乱码清理

清理左侧导航与助手面板残留的乱码文案，重点文件：`Sidebar.tsx` · `AIChatPanel.tsx`。恢复内容包括：

- 项目总览 / 模型设置 / 最近项目
- 切换深色/浅色模式
- 写作助手欢迎语 / 快捷提示按钮 / 输入框占位提示
- 错误提示 / 空回复提示等

### 4️⃣ 工作台外壳响应式改造（移动端抽屉）

整套工作台壳层适配小屏，不再是「桌面布局硬塞进去」：

| 改造项 | 说明 |
|--------|------|
| **全局主导航 → 移动端抽屉** | 小屏下不再常驻左侧，顶部增加统一入口 |
| **章节树 → 移动端抽屉** | 手机/平板上可打开、关闭，选章节后自动收起 |
| **右侧助手 → 抽屉式** | 大屏常驻，小屏改为侧边抽屉 |
| **抽屉遮罩结构重做** | 从「整屏绝对按钮盖住一切」改为「面板本体 + 剩余区域遮罩」，交互更干净、自动化更稳定 |

**核心文件：** `AppLayout.tsx` · `Sidebar.tsx` · `ProjectWorkspace.tsx` · `AIChatPanel.tsx`

### ✅ 验证（完整闭环）

- 临时调试脚本已清理，未把排障垃圾留在项目里
- 新增移动端壳层自动化测试：主导航抽屉开/关 · 章节抽屉开/关 · 助手抽屉开/关
- `npm run typecheck` ✅ · `npm run build` ✅
- `npm run test:e2e` **29/29 passed** ✅

## 2026-08-09 · 人物关系网络美化（P2-11 增强）

`CharacterRelationGraph.tsx` 全面重写，解决"图杂乱"问题：

| 改进 | 说明 |
|------|------|
| **点击不重建图** | 选中态用样式独立更新，此前 `selectedId` 进主 effect 依赖导致每次点击整图重排跳动 |
| **角色分层大小** | 主角 46px → 反派 42 → 感情线 38 → 导师 36 → 搭档 34 → 配角 32 → 客串 28 |
| **焦点模式** | 悬停角色时仅其直接关系网络高亮，其余淡化至 18%，连线再多也不乱 |
| **边标签淡显** | 默认 70% 透明度，悬停某条边才加粗强调 |
| **柔和视觉** | 节点同色阴影 + 白描边，名字白描边可读、超长省略号截断，配色更柔 |
| **可折叠图例** | 右下角图例展示 7 种角色色 + 8 种关系色，支持收起/展开，适配暗色模式 |
| **布局更舒展** | 斥力 9000 / 理想边长 150，节点间距更大 |

### ✅ 验证

- `tsc --noEmit` ✅ · `vite build` ✅
- e2e 无依赖旧实现断言的破坏

### 📄 文档

- `README.md` 人物关系网络使用指南更新（读图 / 操作 / 数据来源）

---

## 2026-08-09 · 一键人物与关系分析（P2 增强）+ Bug 修复

### ✨ 新功能：一键分析作品人物与关系（DeepSeek）

作品写了很多字后，可在**人物模块一键通读全文**，自动识别主要人物与人物关系：

| 层级 | 文件 | 说明 |
|------|------|------|
| 后端服务 | `backend/app/services/character_analysis_service.py` | 读取章节 → 长文本自动采样（超长取每章头尾）→ DeepSeek 分析 → 结构化 JSON 解析 + jieba 词频兜底 |
| 后端接口 | `backend/app/api/projects.py` | `POST /api/projects/{id}/analyze-characters` |
| 前端交互 | `CharacterPanel.tsx` | 「一键分析作品人物」按钮（空状态页 + 详情页）→ 结果面板：人物勾选（默认全选）+ 关系图预览 + 关系列表 → 确认导入 |

**导入安全策略：**
- 只新增、不覆盖：同名角色保留已有档案，标注"已存在 · 不覆盖"
- AI 失败自动回退本地 jieba 词频（至少给出人物名单）
- 长文本自动抽样，适配 LLM 上下文上限

### 🐛 修复：导入后点击角色无反应 / 显示不了其他角色

**根因**：`buildEmptyCharacter` 用 `char-${Date.now()}` 生成 id，批量导入时多个角色在**同一毫秒**生成 → **id 完全重复**，导致 React key 冲突、点击角色无法切换、关系图只渲染一个节点。

| 修复 | 说明 |
|------|------|
| id 唯一化 | `char-${时间戳}-${6位随机}`，批量导入每个角色独立 id |
| 简介入库 | AI 一句话简介写入 `background` / `surfaceMotivation`，导入后详情不再空白 |
| 关系双向补充 | A→B 关系同时写入 A、B 双方档案，选中任一方都能看到彼此关系 |

### ✅ 验证

- `npx tsc --noEmit` ✅
- 后端 `py_compile` ✅
- 逻辑模拟：4 新角色 id 唯一、关系双向、现有角色不覆盖 ✅
- 全链路实测（`e12d3053f1a4`，3章/12991字）：分析 4 人物 + 3 关系 → 保存 → 读回 id 唯一、简介齐全、关系双向 ✅

---

## 2026-08-07 （续）· P0 主链闭环：任务持久化 + 断点续写 + UI 增强

### 🗄️ novel_jobs 持久化（Codex）

任务从"内存 dict"升级为"**SQLite 持久化为主，内存只保留运行中 `asyncio.Task` 句柄**"：

| 改动 | 说明 |
|------|------|
| `db.py` 新增 `novel_jobs` 表 | 完整字段：状态、当前章、最后完成章、取消标记、时间戳、模型来源、fallback 计数等 28 列 |
| `novel_generation_service.py` | `persist_job()` / `_load_job_from_db()` — 每次状态变更实时落库，重启不丢任务 |
| `main.py` 启动钩子 | `recover_jobs_after_restart()` — 后端重启后自动恢复未完成任务的 `asyncio.Task` |

### 🔄 断点续写整书（Codex）

`first-unfinished` 模式不再简单按字数猜测，而是形成参考闭环：

| 新增接口 | 说明 |
|----------|------|
| `GET /api/ai/novel-jobs/project/{id}` | 返回当前运行中任务或最近完成/取消的任务 |
| `GET /api/ai/novel-jobs/project/{id}/history` | 返回最近 20 个历史任务记录 |

**续写优先级：**
1. 上次中断在某章中途 → 优先从那一章继续
2. 上次已经完整写完某章 → 从下一章继续
3. 无可用历史 → 回退到"首个未完成章节"逻辑

### 🎨 MarkdownEditor 自动创作 UI 增强（DeepSeek）

自动创作运行期间，状态栏从单行文字升级为多信息面板：

- **实时耗时**（分:秒计数器，自动启停）
- **章节进度条**（绿色，随完成章数增长）
- **完成章数/总章数** 数字显示

### ✅ 验证

- `npm run typecheck` ✅
- `npm run build` ✅
- `npm run test:e2e -- --reporter=list` **15/15 → 17/17 passed** ✅
- 新增覆盖：任务历史查询 ✓ · 断点续写 first-unfinished ✓
- `start.bat` 一键启动脚本已就绪

---

## 2026-08-07 · 🏆 10 万字目标达成 + 长篇生成补写闭环

### 🎯 关键里程碑：真实 AI 写完 10 万字小说

这是项目最重要的节点：**"平台自己的 AI 完整写完 10 万字小说" 正式成立。**

| 指标 | 结果 |
|------|------|
| 验证项目 | `729b27d23aa4`（任务 `7379afd00ab2`） |
| Provider / Model | deepseek / deepseek-chat |
| fallback_chapters | **0**（全部真实 LLM 生成） |
| last_generation_source | **llm** |
| completed_chapters | **24/24** |
| 最终总字数 | **115,819 字** |
| 章节目标 → 实际 | 99,984 → 115,819 |
| 整体达成率 | **115.8%** |

### ✍️ 长篇生成链路补强（Codex）

`novel_generation_service.py` 重写并补强，新增**"字数不足自动补写"闭环**：

- 每章按目标字数做 **1~3 轮补足**，避免模型写短了就直接跳到下一章
- 统一章节字数计算 + 最小达标阈值
- 扩写方向提示 + 内容合并逻辑（补写内容追加到原文）

**新增可验证脚本：**

| 脚本 | 说明 |
|------|------|
| `scripts/report_chapter_targets.py` | 按章查看 `target / actual / ratio` 达成率 |
| `scripts/verify_100k_generation.py` | 原生终端 10 万字验证（创建→大纲→任务→轮询） |

### 🧹 E2E 冒烟套件重写（Codex）

`novelcraft-smoke.spec.ts` 整体重写为**干净稳定版**：

- 全部改为 ASCII 输入 + 稳定断言，彻底绕开历史编码污染
- 覆盖：项目页/工作区切换、Markdown 导入、Word 建议书、正文保存、章节切换读真实正文、人物创建、助手、自动创作/暂停/停止、novel job API
- e2e 结果：**13/13 → 14/14 passed**（约 6.4 分钟）

### 🔧 组件重写（Codex）

| 文件 | 改动 |
|------|------|
| `WordImport.tsx` | 清理乱码文案、建议应用反馈、批量应用状态 |
| `api.ts` | 统一超时/未配置/导入报错文案为正常中文 |
| `settings.py` | 去掉 `llm-health` 的 `asyncio.wait_for` 包裹，改为直接探活（消除误报） |
| `ai.py` | 清理诊断端点脏注释 |

### 🔍 双后端根因诊断（DeepSeek）

上轮 `fallback_chapters=100` 的根因被定位为**双重后端进程**抢占 8000 端口：
- 旧进程持有过期配置，导致所有章节走 fallback
- 清理后单实例运行，真实 API 正常（`source=llm, fallback=0`）
- `llm-health` 的 `asyncio.wait_for` 竞态也已由 Codex 移除

### 📄 章节脚手架剥离修复（Codex）

**问题定位（两层根因）：**
1. 前端历史显示：章节内容被识别为"空/异常"时回退成章节思路稿
2. 真实 10 万字项目里，部分旧章节内容自带历史生成脚手架（正文前多一段 `# 章节标题 + > 摘要`），点开先看到"小说介绍/思路"，不像正文

**修复：**
| 文件 | 改动 |
|------|------|
| `frontend/src/services/normalize.ts` | 补章节内容剥离逻辑，读取到旧格式时直接落到正文 |
| `backend/app/db.py` | `row_to_chapter()` 下沉剥离逻辑，章节接口本身返回干净正文（不再依赖前端时序/缓存） |
| `novelcraft-smoke.spec.ts` | 新增回归测试：历史脚手架章节打开后必须直接显示叙事正文 |

**验证：**
- 真实项目 `f8314c091c49 / novel_seed_beta` 的章节接口直接返回正文开头，不再以 `#` 或 `>` 开头
- 浏览器实测编辑器正文显示 `2025年2月2日...`（真实正文），而非 `# 第01章...` 思路模板

### ✅ 验证

- `npm run typecheck` ✅
- `npm run build` ✅
- `npm run test:e2e -- --reporter=list` **14/14 → 15/15 passed** ✅
- 10 万字实跑：115,819 字 / fallback=0 / source=llm ✅
- 代码扫描：明显乱码残留仅剩 `normalize.ts` 识别规则（预期保留）
- 章节接口：`f8314c091c49 / novel_seed_beta` 直接返回正文 ✅

> **待办提醒**：后端改动需重启后端进程使 `llm-health` 修正生效。

### 🔜 下一步建议

1. 用现有自动化链路跑"从建项目到自动创作多章"的长流程压测
2. 收 `novel_generation_service.py` 历史编码痕迹和状态文案，整理成长篇生成服务干净版

---

## 2026-08-06 · 前端基础设施 + 工具脚本 + 英文提示全量中文化

### 🪝 前端 novel-jobs 接入层（DeepSeek）

为 Codex 即将进行的前后端长篇创作对接提供完整基础设施：

| 新增文件 | 说明 |
|----------|------|
| `frontend/src/hooks/useNovelJobPoller.ts` | 轮询 Hook — 每 2s 拉取任务进度，completed/failed/cancelled 自动停，不绑定 Zustand |
| `frontend/src/types/index.ts` | 新增 `NovelJobPayload` / `NovelJobMode` / `NovelJobStatus` / `StartNovelJobRequest` 类型 |
| `frontend/src/services/api.ts` | `aiApi.novelJob` 新增 4 个方法：`start` / `getByProject` / `get` / `cancel` |

**E2E 测试新增：**

| 测试 | 说明 |
|------|------|
| `novel job API starts and returns progress` | 启动作业 → 轮询等待 ≥1 章完成 → 验证进度 |
| `novel job API cancel stops generation` | 启动作业 → 立即取消 → 验证状态一致 |

### 🌐 Playwright 浏览器桥接

| 新增文件 | 说明 |
|----------|------|
| `frontend/scripts/browser-bridge.mjs` | CLI 桥接脚本，支持 snapshot / click / fill / text / visible / nav / screenshot / eval 共 8 种命令 |

解决 Codex 内置浏览器被 Auto-review 策略拦截 `localhost` 的问题。通过 Playwright 独立 Chromium 驱动页面交互，输出 JSON 供 AI 工具解析。

**Playwright 配置变更：**
- `playwright.config.ts` 移除 Edge 依赖，改用 Playwright 自带 Chromium
- 通过 `PLAYWRIGHT_DOWNLOAD_HOST=npmmirror.com` 国内镜像加速安装（191MB 秒下）

### 📝 英文提示全量中文化

| 文件 | 修复数 | 范围 |
|------|--------|------|
| `api/chapters.py` | 2 | 章节 404 |
| `api/projects.py` | 3 | 项目 404 |
| `api/novel_jobs.py` | 1 | 任务 404 |
| `api/settings.py` | 2 | LLM 配置 404 |
| `api/imports.py` | 5 | 路径/类型/解析错误 |
| `services/import_service.py` | 5 | 内部错误消息 |
| `services/review_service.py` | ~12 | **系统提示 + 启发式问题全部中文化** |
| `llm/provider.py` | 7 | API 响应错误（保留前端匹配字符串） |

**审校服务改动最大：** 系统提示从英文小说编辑器变成中文审校编辑；所有启发式类型（"重复的对话标签"、"段落开头重复"、"段落过长"等）及建议/原因全部中文化。

### 📦 运维工具脚本

| 文件 | 说明 |
|------|------|
| `scripts/cleanup_test_data.py` | 测试数据清理 — dry-run 模式默认，`--execute` 执行前自动备份 DB |
| `scripts/api_smoke_test.py` | 通用 API 巡检 — 覆盖 9 个核心端点 |
| `scripts/api_smoke_novel_jobs.py` | Novel-jobs 专项巡检 — 5 个检查点覆盖全部 4 个端点 |

### 🔤 文案修复

| 文件 | 改动 |
|------|------|
| `WritingStats.tsx` | "稳定计算" → "准确统计" |
| `WorldBuilder.tsx` | 一致性提示文案优化 |
| `MarkdownImport.tsx` | 3 处文案精修（"拿到"→"获取"等） |
| `Dashboard.tsx` | "置顶到最近" → "置顶项目" |
| `ReferenceAnalysis.tsx` | "未能稳定解析" → "解析失败" |

### 🧪 data-testid 补齐

给 `ReferenceAnalysis`、`WritingStats`、`WorldBuilder` 三个低风险模块添加了 15+ 个 `data-testid` 属性，覆盖空状态、错误提示、卡片、按钮等关键交互元素。

### ✅ 验证

- `tsc --noEmit` ✅
- e2e 全量 12/12 ✅（Playwright Chromium）
- 单条过滤测试正常（`spawn UNKNOWN` 无法复现）
- `types/index.ts` 和 e2e spec 文件经审查无乱码

---

## 2026-08-05 · 后端长篇创作引擎 + 主链重写

### 🤖 后端长篇生成任务（Codex）

这是项目迄今最大的架构升级：**从前端按钮循环 → 后端可持续执行的任务引擎**。

**新增文件：**

| 文件 | 用途 |
|------|------|
| `backend/app/api/novel_jobs.py` | 长篇创作 REST API（启动/查询/取消） |
| `backend/app/services/novel_generation_service.py` | 后台异步任务服务（429 行） |

**三种生成模式：**

| 模式 | 说明 |
|------|------|
| `all` | 按大纲全量生成所有章节 |
| `current-to-end` | 从当前章写到结尾 |
| `first-unfinished` | 从首个未完成章节开始（35% 完成度阈值） |

**核心能力：**
- `POST /api/ai/novel-jobs/start` — 启动整书创作任务
- `GET /api/ai/novel-jobs/{job_id}` — 查询任务进度（章节数/字数千/状态）
- `GET /api/ai/novel-jobs/project/{project_id}` — 按项目查运行中任务
- `POST /api/ai/novel-jobs/{job_id}/cancel` — 取消（当前章后停止）
- 每章自动落库 `chapters` 表，更新项目字数统计
- 逐章传递前文摘要作为上下文，保持长篇连贯性
- 进度实时可查：`completed_chapters` / `failed_chapters` / `generated_words`

**入口注册：** `main.py` 新增 `novel_jobs` router，挂载到 `/api/ai` 前缀。

### ✍️ 主链重写（Codex）

**两个核心组件完全重写：**

| 文件 | 改动 |
|------|------|
| `ProjectWorkspace.tsx` | 清理乱码文案，重建工作区标签/面板/导入枢纽 |
| `MarkdownEditor.tsx` | 清理乱码文案，保留并重建正文主链：章节加载→保存→AI 续写→按大纲自动创作 |

**关键修复：**
- ✅ 点章节 → 自动进入正文编辑器（之前停在空白页）
- ✅ 输入文本 → 保存 → 刷新 → 内容能读回
- ✅ 编辑器保存状态、AI 续写、自动创作逻辑重建
- ✅ `ChapterTree.tsx` 章节列表与编辑器同步

### 🔧 后端提示词修复（Codex）

| 文件 | 改动 |
|------|------|
| `ai_service.py` | 重写系统提示、续写提示、fallback 文本 —— 这是之前"10万字自动创作"被卡住的关键根因 |
| `api.ts` | 修复超时提示、未配置模型提示、导入失败提示文案 |

### ✅ 验证

- `tsc --noEmit` ✅
- `vite build` ✅
- e2e 全量 **12/12** ✅
- 抽测主链 5/5：导入/建议书/保存/助手/自动创作 ✅

---

## 2026-08-04 · Phase 2 基础升级

### 🗄️ SQLite 持久化（后端）
- 后端已使用 SQLite 存储所有项目/章节/LLM 配置数据
- `projects` / `chapters` / `llm_configs` 三张表完整覆盖
- 启动时自动建表 + 数据迁移

### 💾 IndexedDB 存储（前端）
- 新建 `frontend/src/storage/indexeddb.ts` — IndexedDB 存储适配器
- 4 个 Zustand store 全部从 `localStorage` 切换到 IndexedDB
- 突破 5MB 存储上限，支持超长小说内容
- 编辑器内容设 30 天自动过期清理

### 🔤 中文 NER 升级
- 集成 `jieba` 分词 + 词性标注（`nr`/`nrt` 人名识别）
- 姓氏库扩展至 200+（含复姓如 欧阳、司马、慕容 等）
- 排除词库增至 80+ 条，减少误匹配
- jieba 不可用时自动回退增强正则（零依赖降级）

### 🤖 DeepSeek LLM 真实联调
- 配置 DeepSeek API Key → `backend/.env`
- 实测 API 联通：状态 200，中文回复正常
- DeepSeek API 兼容 OpenAI 协议，通过 `_call_openai` 复用
- 默认 Provider 改为 `deepseek`，模型 `deepseek-chat`

### ✅ 验证
- `tsc --noEmit` ✅ 零错误
- Python 全文件语法 ✅
- e2e 7/7 ✅
- DeepSeek API 联通 ✅

---

## 2026-08-02 · 导入链路 + 文本修复

### 本轮完成

- 补齐 Markdown 章节导入链路
  - 导入接口返回 `project`、`chapters`、`importedChapters`、`totalWords`、`analysis`
  - 导入到已有项目时，自动清空旧大纲并同步目标章节数
  - 前端导入完成后优先载入首章内容
- 修复章节保存链路：支持 `title`/`volumeId`/`order`，不再落库 `Untitled Chapter`
- 优化 Word 建议书工作流：前端 `.docx` 直传 + 建议应用接口正常
- 加固后端文本处理：新增修复工具，读写时增加净化逻辑
- AI 中文化：大纲/人物/续写/润色 prompt 改为中文，兜底文本中文化
- 前端文案统一：页面标题、乱码回退、README 更新

### 已验证
- `tsc --noEmit` + `vite build` ✅
- `/api/health` ✅
- `/api/import/markdown-folder/ingest` 新结构已生效
- `/api/import/suggestion-docx` 可解析 `.docx`
- `/api/import/apply-suggestion` 可返回结果
- DeepSeek 真实 API 联调：大纲生成 + AI 续写 2129 字 ✅

---

## 2026-08-01 ～ 2026-08-02 · 五轮深度优化

### 第一轮：真持久化 + 真实 API + 诚实错误（D1-D10）

| # | 改动 |
|---|------|
| D1 | SQLite 数据库：`projects` + `chapters` 两张表，JSON 存复杂结构 |
| D2 | `LLMProvider` 新增 `LLMConfigurationError` + `assert_configured()`，未配 Key 返回 503 |
| D3 | AI/审校接口：503 语义 + 500→502 区分上游故障 |
| D4 | 前端 5 个组件 mock 全清除（MarkdownImport/WordImport/ReviewPanel/AIChatPanel/MarkdownEditor） |
| D5 | Markdown 文件夹导入重写：路径校验 + 扫描 + 写入项目 |
| D6 | Word 建议书重写：结构化建议 + 逐条应用 + 规则解析 |
| D7 | 仪表盘/工作区/编辑器改为真实 API 驱动 |
| D8 | 大纲/人物/世界观持久化到项目 |
| D9 | 项目更新接口扩展 `outline`/`characters`/`worldRules`/`importAnalysis` 字段 |
| D10 | 定位本机 Python 路径 |

### 第二轮：去假数据 + 配置闭环 + 错误语义（D11-D18）

| # | 改动 |
|---|------|
| D11 | `cleanup_legacy_demo_data()` 清除 demo-1/2/3 |
| D12 | `app/api/settings.py` — LLM 配置 CRUD，存 `llm_configs` 表 |
| D13 | `SettingsPage.tsx` 写后端，配置落库 |
| D14 | `_refresh_runtime_config()` 读 `get_active_llm_config()` |
| D15 | `Sidebar.tsx` 去 demo，`projectApi.list()` 为唯一数据源 |
| D16 | Word 建议书去 mock 语义 |
| D17 | `api.ts` 503 友好提示"AI 服务未配置，请在设置页添加 API Key" |
| D18 | 错误码：`LLMConfigurationError`→503, `LLMResponseError`→502 |

### 第三轮：我的补充改进（I1-I3）

| # | 改动 |
|---|------|
| I1 | Provider 加 5 秒 TTL 缓存，避免每次 `chat()` 查库 |
| I2 | 创建 `backend/.env.example` |
| I3 | `importApi` 硬编码路径统一为 `${API_BASE}` |

### 第四轮：前端重构 + 五大降级（E1-E12）

| # | 改动 |
|---|------|
| E1 | 10+ 组件重写清理 |
| E2 | `API_BASE` 直连 `localhost:8000` |
| E3 | `normalize.ts` 乱码检测+修复 |
| E4-E8 | 大纲/人物/聊天/续写/审校五大降级方案 |
| E9 | 审校计数修正 |
| E10 | 导入功能专项打通 |
| E11 | 七标签页页面级验证 |
| E12 | 剩余问题定位 |

### 第五轮：乱码深防 + 文案全中文化（F1-F10）

| # | 改动 |
|---|------|
| F1 | `normalize.ts` 重写：正则 + 18 个已知乱码片段 + Latin-1 密度三重防线 |
| F2-F6 | 界面文案全中文 + 项目名/人物卡/大纲/聊天/审校乱码清洗 |
| F7 | `ReferenceAnalysis` 不依赖完美 JSON，解析失败自动切本地 |
| F8 | 导入工作流语境优化 |
| F9 | `tsc -b` 通过 + 核心接口可通 |
| F10 | 后端旧进程/旧编码残留识别 |

---

## 2026-07-31 · 早期修复与功能建设

### Bug 修复（21 项）

| # | 问题 | 严重度 |
|---|------|--------|
| 1 | 新建项目后导航到 demo 项目 | 🔴 致命 |
| 2 | 新项目所有标签页显示 demo 数据 | 🔴 致命 |
| 3 | 大纲永远生成固定内容 | 🔴 致命 |
| 4 | 项目卡片三点菜单点击后跳转 | 🟡 重要 |
| 5 | 内容区域无法滚动 | 🟡 重要 |
| 6 | 大纲章节名重复（12个词循环） | 🟡 重要 |
| 7 | AI 代写字数不符 | 🟡 重要 |
| 8-12 | 世界观编辑/向导/章节树同步/侧边栏等 | 🟢 建议 |
| 13 | 代写主角名硬编码 | 🟡 重要 |
| 14 | 人物创建按钮无反应 | 🟡 重要 |
| 15 | 数据看板显示 demo 数据 | 🟡 重要 |
| 16-17 | 代写标题过长 + 内容高重复度 | 🟡 重要 |
| 18-21 | 数据看板空状态 + 刷新丢失 + 大纲丢失 + 数据持久化 | 🔴 致命 |

### Codex 审查修复（10 项，C1-C10）

| 问题 | 严重度 |
|------|--------|
| 后端缺 `python-multipart` | 🔴 |
| config `debug` 字段崩溃 | 🔴 |
| CharacterPanel 类型错误 | 🔴 |
| Markdown 预览 XSS 风险 | 🟡 |
| genre 多选类型不合法 | 🟡 |
| 数据看板读 DOM | 🟡 |
| 中文路径兼容 | 🟡 |
| NER 正则弱 | 🟡 |
| Word 建议书纯 mock | 🟡 |
| 多个模块 mock 链路 | 🟢 |

### 功能增强（14 项）

题材多选/字数扩展/风格三档/梗概双模式/剧情逻辑链/卷级概要/一键代写/字数控制/内容多样化/世界观编辑/三点菜单/数据持久化/数据看板实时统计/项目隔离

---

## 当前状态

| 维度 | 状态 |
|------|------|
| `tsc --noEmit` | ✅ 零错误 |
| `vite build` | ✅ 通过 |
| 后端启动 | ✅ `python main.py` |
| 数据库 | ✅ SQLite（projects/chapters/llm_configs） |
| AI 能力 | ✅ 配 Key 后真实调用，未配 Key 走降级 |
| 前端 mock | ✅ 全部清除 |
| 持久化 | ✅ localStorage + SQLite |
| 文案 | ✅ 全部中文 |
| 乱码防御 | ✅ normalize.ts 三重防线 + 五大降级 |
