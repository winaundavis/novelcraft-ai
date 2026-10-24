"""任务角色（Role）定义与默认档位 —— 模型档位路由层（建议4：自检分级 L0/L1/L2）。

背景（调研方向 D · 自检分级）：
- L0/L1 确定性自检（角色名/时间线/设定核查、钩子判定、摘要抽取）可用便宜模型；
- L2 跨章推理（冲突仲裁/根因分析）用中档模型；
- 发布级正文生成（文笔优先）用高质量模型 + 人工把关。

每个「任务角色」可配置独立的 model/temperature（provider 可选），未显式配置时
回退到激活配置（向后兼容：所有现有调用不改签名也能跑）。

角色覆盖优先级：显式 kwargs > DB `role_overrides` > 代码默认档位 > 激活配置。
"""

from __future__ import annotations

# ── 角色常量 ──────────────────────────────────────────
ROLE_WRITING = "writing"   # 正文生成/续写/扩写/润色 —— 文笔优先，默认高质量模型
ROLE_PLANNING = "planning" # 大纲、人物与章节规划 —— 保留推理能力
ROLE_COPILOT = "copilot"   # 局部编辑候选 —— 默认关闭推理，避免吞掉短输出预算
ROLE_SUMMARY = "summary"   # 章节摘要/卷摘要/事实抽取 —— 高频确定，可指向便宜模型
ROLE_CHECK = "check"       # 事实核查/钩子判定/AI 味 —— L0/L1 确定性自检，可指向便宜模型
ROLE_REASON = "reason"     # 冲突仲裁/根因分析 —— L2 需逻辑，中档模型
ROLE_STYLE = "style"       # 文风分析/画像抽取
ROLE_FULLTEXT = "fulltext" # 全文问答 —— 全书直灌核对设定/伏笔，前缀缓存命中低成本
# P2（Codex 复测）：调用方未显式指定 role 时的兜底标记——不把"不知道用途"
# 等同于"审校用途"（check），避免写作请求被静默错误路由到审校模型。
ROLE_UNSPECIFIED = "unspecified"

ALL_ROLES = (ROLE_WRITING, ROLE_PLANNING, ROLE_COPILOT, ROLE_SUMMARY, ROLE_CHECK, ROLE_REASON, ROLE_STYLE, ROLE_FULLTEXT, ROLE_UNSPECIFIED)

# 角色中文名（用于配置 UI 展示）
ROLE_LABELS = {
    ROLE_WRITING: "正文生成",
    ROLE_PLANNING: "规划设计",
    ROLE_COPILOT: "协作编辑",
    ROLE_SUMMARY: "摘要记忆",
    ROLE_CHECK: "自检核查",
    ROLE_REASON: "冲突仲裁",
    ROLE_STYLE: "文风分析",
    ROLE_FULLTEXT: "全文问答",
}

# 各角色一句话说明（配置 UI 提示）
ROLE_DESCRIPTIONS = {
    ROLE_WRITING: "正文生成、续写、扩写、润色，文笔优先，建议用高质量模型。",
    ROLE_PLANNING: "大纲、人物与章节规划，允许模型保留推理过程。",
    ROLE_COPILOT: "局部改写与增强候选，默认关闭推理以优先返回可编辑正文。",
    ROLE_SUMMARY: "章节摘要、卷摘要、事实抽取，高频确定性任务，可指向便宜模型。",
    ROLE_CHECK: "事实核查、钩子判定，L0/L1 确定性自检，可指向便宜模型。",
    ROLE_REASON: "冲突仲裁、根因分析，L2 需要逻辑推理，建议中档模型。",
    ROLE_STYLE: "文风分析、画像抽取，可指向便宜模型。",
    ROLE_FULLTEXT: "全书直灌问答，核对设定/伏笔/前文细节，前缀缓存命中成本极低，可指向便宜模型。",
}

# 默认档位（代码兜底）：model 为空表示回退激活配置；temperature 为 None 表示
# 不覆盖（沿用激活配置温度）。用户可通过 llm_configs.role_overrides 覆盖。
DEFAULT_ROLE_PROFILES: dict[str, dict] = {
    # 正文要有更大的输出下限：全局默认 4096 对 4000+ 字长章节不够，
    # 截断的正文会被当成成功结果静默落库（作者拿到半句话却以为写完了）。
    # 这里只设**下限**，不会压低作者在设置里调高的 max_tokens。
    ROLE_WRITING: {"model": "", "temperature": None, "thinking": "disabled", "max_tokens": 8192},
    ROLE_PLANNING: {"model": "", "temperature": 0.3, "thinking": "disabled"},
    ROLE_COPILOT: {"model": "", "temperature": None, "thinking": "disabled"},
    ROLE_SUMMARY: {"model": "", "temperature": 0.3, "thinking": "disabled"},
    ROLE_CHECK: {"model": "", "temperature": 0.1, "thinking": "disabled"},
    ROLE_REASON: {"model": "", "temperature": 0.2, "thinking": "disabled"},
    ROLE_STYLE: {"model": "", "temperature": 0.3, "thinking": "disabled"},
    ROLE_FULLTEXT: {"model": "", "temperature": 0.2, "thinking": "disabled"},
}

# 角色覆盖可配置的字段（与 role_overrides JSON 键对齐）
# 第 2 档：api_key / api_base —— 角色独立 provider + 独立 API key（真正跨厂商）
# 第 3 档：long_model —— 长度路由的长窗模型（输入超阈值时切换，默认不启用）
ROLE_PROFILE_KEYS = ("provider", "model", "temperature", "thinking", "max_tokens", "api_key", "api_base", "long_model")

# 第 3 档动态路由：置信度升级链阈值（0-1）
# check 档返回 confidence < 该阈值时，自动用 reason 档（强模型）复查一次，结果以强模型为准。
ESCALATION_THRESHOLD = 0.6
