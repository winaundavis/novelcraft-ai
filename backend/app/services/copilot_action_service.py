"""Small deterministic action contract for the project-aware Novel Copilot."""

from __future__ import annotations

import re


def prose_edit_mode(instruction: str, action: str | None = None) -> str:
    """Classify the author's instruction, never the attached source prose.

    Mode controls plot authority; selection scope independently controls how
    much text may be replaced. A ban on *adding* plot during a polish is not
    an instruction to discard the existing draft.
    """
    if action not in {None, "rewrite"}:
        return "preserve"
    text = instruction.split("\n\nOriginal text:\n", 1)[0]
    prose = action == "rewrite" or any(term in text for term in ("正文", "本章", "这一章", "小说内容")) or bool(
        re.search(r"第[\d一二三四五六七八九十百零两]+章", text)
    )
    if not prose:
        return "preserve"
    other_arc = r"(?:(?:其他|其它|后续|后面|别的)(?:剧情)?弧|第[\d一二三四五六七八九十百零两]+弧|越弧|串弧)"
    removal = r"(?:删除|移除|去掉|清除|剔除)"
    remove_plot = any(
        re.search(removal + r".{0,24}" + other_arc, clause)
        and not re.search(r"(?:不要|不能|不得|禁止|别|不).{0,6}" + removal, clause)
        for clause in re.split(r"[。！？，,；;\n]", text)
    )
    wording_only = bool(re.search(
        r"(?:只|仅)(?:做|调整|修改|改|改善)?(?:润色|措辞|表达|文风|标点)|"
        r"剧情(?:不要|不能|不得|不)变|保留(?:原有)?剧情|不改变剧情", text
    )) or ("润色" in text and any(t in text for t in ("不要新增", "不新增", "不要添加", "不添加")))
    if wording_only and not remove_plot:
        return "preserve"
    exclude_plot = bool(re.search(
        r"(?:不要|不能|不得|禁止|避免|不再|不应)(?:再)?(?:出现|引入|包含|涉及|暗示|掺入|混入|写到|写入|写|有)"
        r"[^。！？\n]{0,24}" + other_arc, text
    )) or bool(re.search(r"(?:不要|禁止|避免|不能)(?:再)?(?:越弧|串弧)", text))
    if remove_plot or exclude_plot or any(t in text for t in ("纠正越弧", "修正越弧", "只写当前弧")):
        return "rebuild"
    if any(term in text for term in ("重新生成", "重新创作", "重新写", "重写", "从头写")):
        return "rebuild"
    if any(term in text for term in ("大纲", "当前弧", "本弧", "剧情弧")) and any(
        term in text for term in ("调整", "修改", "纠正", "修正", "依据", "按照", "遵循", "符合")
    ):
        return "correct"
    return "preserve"


def requests_outline_regeneration(instruction: str, action: str | None = None) -> bool:
    return prose_edit_mode(instruction, action) == "rebuild"

COPILOT_ACTIONS = {
    "continue": {"action": "continue", "profile": "continuation", "label": "继续写", "instruction": "续写当前场景，承接当前章节合同并推进一个可感知的变化。"},
    "rewrite": {"action": "rewrite", "profile": "scene_writing", "label": "改写当前段", "instruction": "改写当前选区或当前正文，保留事实与剧情意图，只改善表达。"},
    "expand": {"action": "expand", "profile": "scene_writing", "label": "扩写细节", "instruction": "扩写当前场景的动作、感官和人物反应，不添加未经允许的设定。"},
    "condense": {"action": "condense", "profile": "scene_writing", "label": "压缩冗余", "instruction": "压缩当前文本，保留因果、信息和人物语气，删除重复解释。"},
    "style": {"action": "style", "profile": "scene_writing", "label": "调整文风", "instruction": "按当前项目文风调整文本，不改变事实、人物立场或剧情结果。"},
    "character": {"action": "character", "profile": "scene_writing", "label": "强化人物", "instruction": "强化人物动机、行为选择和独立声线，让变化落在行动与对白中。"},
    "emotion": {"action": "emotion", "profile": "scene_writing", "label": "增强情绪", "instruction": "增强情绪递进与余波，避免直接贴情绪标签或空泛抒情。"},
    "pacing": {"action": "pacing", "profile": "scene_writing", "label": "调整节奏", "instruction": "检查并调整场景节奏，让冲突、信息释放和停顿形成有效变化。"},
    "branch": {"action": "branch", "profile": "plot_planning", "label": "生成走向", "instruction": "提出三个差异明显的剧情走向，并分别说明收益、风险和后续影响。"},
    "analyze_scene": {"action": "analyze_scene", "profile": "scene_writing", "label": "分析本场", "instruction": "分析当前场景的目标、冲突、状态变化、节奏和可执行修改点。"},
    "analyze_next": {"action": "analyze_next", "profile": "plot_planning", "label": "分析下一章", "instruction": "基于当前 Story State 和章节合同，给出下一章目标、冲突、变化和结尾钩子。"},
    "continuity": {"action": "continuity", "profile": "consistency_check", "label": "检查连续性", "instruction": "检查人物、事实、时间线、世界规则和伏笔是否连续，并区分真矛盾与合理揭示。"},
    "foreshadow": {"action": "foreshadow", "profile": "consistency_check", "label": "检查伏笔", "instruction": "检查当前章节对活跃伏笔的承接、推进或回收，并指出遗漏的兑现风险。"},
}


def get_action(action: str | None) -> dict[str, str] | None:
    if not action:
        return None
    return COPILOT_ACTIONS.get(action)
