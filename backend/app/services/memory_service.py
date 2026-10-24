"""M1 记忆底座 —— 事实账本抽取 / 章节摘要 / 卷级摘要 的 LLM 编排。

设计原则：
- 所有 LLM 调用失败均降级（跳过抽取 / 截断拼接），绝不阻塞整书生成任务。
- 只做结构化注入，不引入向量库 / 图数据库（PRD 明确约束）。
- prompt 为自研文案，仅借鉴参照项目（show-me-the-story / AI-Novel-Writing-Assistant）
  的架构与注入思想，不复制其代码/长文本。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.dao import chapters as chapters_dao
from app.dao import memory_entities as memory_entities_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import memory_recall as memory_recall_dao
from app.dao import novel_facts as novel_facts_dao
from app.dao import projects as projects_dao
from app.dao import volumes as volumes_dao
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.roles import ROLE_SUMMARY
from app.utils.text_repair import repair_text

# ── 常量 ──────────────────────────────────────────────
MAX_FACT_ITEMS = 10          # 每章最多抽取事实条数（成本控制）
MAX_FACT_LENGTH = 200        # 单条事实最大字数
CHAPTER_SUMMARY_MAX = 220    # 单章摘要最大字数
VOLUME_SUMMARY_MIN = 300     # 卷摘要目标下限
VOLUME_SUMMARY_MAX = 600     # 卷摘要目标上限
VOLUME_CHAPTERS = 10         # 无大纲卷节点时的兜底切分：每 10 章一卷
RECENT_CHAPTER_SUMMARIES = 5  # 生成时注入的最近章节摘要数
RECENT_VOLUME_SUMMARIES = 5   # 生成时注入的最近卷摘要数（P1-2：避免早期卷摘要挤占保留窗口）
MAX_MILESTONES_LENGTH = 3000  # 【已完成事实】注入块总长上限（Codex P2 复测 P1：1500→3000，确保早期关键事实不被挤出，降低叙事一致性冲突）
LAST_ENDING_LENGTH = 800      # 注入的上一章结尾字数

# ── 记忆升级 v1：实体抽取 / FTS 检索 / 摘要对账 常量 ──
MAX_ENTITY_ITEMS = 6         # 每章最多抽取实体数（成本控制）
MAX_RELATION_ITEMS = 6       # 每章最多抽取关系数
MAX_ENTITY_NAME = 20         # 实体名最大长度
MAX_ENTITY_DESC = 80         # 实体描述最大长度
FTS_RESULTS_LIMIT = 3        # 注入的相关历史摘要 top-K
FTS_RESULTS_MAX = 500        # 相关历史摘要注入块总长上限（显式预算内）
ENTITY_STATE_MAX = 400       # 实体状态注入块总长上限（与 DAO 默认一致）

# BUG-A：fallback 占位模板特征词（ai_service._continue_fallback），
# 用于识别历史数据中被污染的摘要（新章节在落库时直接跳过，见 novel_generation_service）
FALLBACK_PLACEHOLDER_MARKERS = ("没有立刻开口", "重新咀嚼那句最危险的话")


def is_fallback_text(text: str | None) -> bool:
    """判断文本是否包含 fallback 占位模板特征（BUG-A 存量污染检测）。

    仅用于注入时排除被污染的摘要/结尾，不做通用检测。
    """
    if not text:
        return False
    return any(marker in text for marker in FALLBACK_PLACEHOLDER_MARKERS)


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.8
    if confidence != confidence:  # NaN
        return 0.8
    return min(1.0, max(0.0, confidence))


# ── Prompt（自研文案） ────────────────────────────────

FACT_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

SUMMARY_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

VOLUME_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

ENTITY_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _build_fact_prompt(content: str) -> str:
    return (
        "请从下面的章节正文中提取\"不可逆事实\"（一旦发生就不会撤销的事件或信息变化）。\n"
        "只提取以下三类：\n"
        "1. completed —— 过程性目标/任务已经完成（如：取得信物、签订契约、通过考核）；\n"
        "2. revealed —— 重要信息/身份/秘密已被揭示（如：幕后主使身份揭晓、真实身世曝光）；\n"
        "3. state_changed —— 不可逆的状态变化（如：角色死亡、关系彻底破裂、某地毁灭）。\n"
        "规则：\n"
        "- 不提取人物情绪、心理活动、日常对话等可逆或主观内容；\n"
        "- 每条事实用一两句话概括（不超过 200 字），写清主体与结果；\n"
        f"- 最多提取 {MAX_FACT_ITEMS} 条；没有符合的返回空数组；\n"
        "- 必须返回 JSON 数组，元素格式为 "
        "{\"fact_type\": \"completed|revealed|state_changed\", \"content\": \"事实描述\"}。\n\n"
        f"章节正文：\n{content[:6000]}"
    )


def _build_chapter_summary_prompt(content: str, chapter_title: str) -> str:
    return (
        f"请概括章节《{chapter_title or '未命名'}》的剧情，150 字以内：\n"
        "要点：发生了什么事、谁做了什么、结果与影响、章末留下了什么钩子。\n"
        f"章节正文：\n{content[:4000]}"
    )


def _build_volume_summary_prompt(chapter_summaries: list[str]) -> str:
    lines = "\n".join(
        f"第{index}章：{summary}" for index, summary in enumerate(chapter_summaries, 1) if summary
    )
    return (
        f"请将以下一卷的逐章摘要压缩为 {VOLUME_SUMMARY_MIN}-{VOLUME_SUMMARY_MAX} 字的卷级摘要：\n"
        "要求：保留一次性事件、主角能力/认知变化、未回收钩子；配角支线压缩成一句；\n"
        f"不要引入原文没有的新信息；只输出摘要正文。\n"
        f"再次强调：输出长度请务必控制在 {VOLUME_SUMMARY_MIN}-{VOLUME_SUMMARY_MAX} 字，"
        "过短会丢失前情，过长会挤占上下文。\n\n"
        f"逐章摘要：\n{lines}"
    )


# ── 章节摘要 ──────────────────────────────────────────

def _truncate_text(text: str, max_length: int) -> str:
    """截断降级摘要（与原 _summarize 行为一致）。"""
    compact = " ".join((text or "").split()).strip()
    if not compact:
        return ""
    return compact[:max_length] + ("..." if len(compact) > max_length else "")


async def summarize_chapter(content: str, chapter_title: str = "") -> str:
    """生成章节摘要（LLM），失败降级为截断，绝不抛异常。"""
    if not content or not content.strip():
        return ""
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": _build_chapter_summary_prompt(content, chapter_title)},
            ],
            role=ROLE_SUMMARY,
            max_tokens=400,
        )
        summary = repair_text(reply).strip()
        if summary:
            return summary[:CHAPTER_SUMMARY_MAX]
    except (LLMConfigurationError, LLMResponseError):
        pass
    return _truncate_text(content, CHAPTER_SUMMARY_MAX)


# ── 事实抽取 ──────────────────────────────────────────

def _parse_facts(reply: str) -> list[dict[str, str]]:
    """鲁棒解析 LLM 返回的 JSON 事实数组（容忍代码块围栏 / 前后杂文）。"""
    if not reply:
        return []
    json_text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", json_text, re.DOTALL)
    if fence:
        json_text = fence.group(1).strip()
    start = json_text.find("[")
    end = json_text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(json_text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    facts: list[dict[str, str]] = []
    for item in data[:MAX_FACT_ITEMS]:
        if not isinstance(item, dict):
            continue
        fact_type = str(item.get("fact_type") or novel_facts_dao.FACT_TYPE_COMPLETED)
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if fact_type not in novel_facts_dao.VALID_FACT_TYPES:
            fact_type = novel_facts_dao.FACT_TYPE_COMPLETED
        facts.append({"fact_type": fact_type, "content": content[:MAX_FACT_LENGTH]})
    return facts


async def extract_facts(project_id: str, chapter_id: str, content: str) -> list[dict[str, str]]:
    """从章节正文抽取不可逆事实并幂等入库；LLM 失败时跳过抽取，不阻塞任务。

    返回真正新插入的事实列表。
    """
    if not content or not content.strip():
        return []
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": FACT_SYSTEM_PROMPT},
                {"role": "user", "content": _build_fact_prompt(content)},
            ],
            role=ROLE_SUMMARY,
            max_tokens=800,
        )
        facts = _parse_facts(reply)
    except (LLMConfigurationError, LLMResponseError):
        return []

    inserted: list[dict[str, str]] = []
    for fact in facts:
        is_new = novel_facts_dao.add_fact(
            project_id=project_id,
            fact_type=fact["fact_type"],
            content=fact["content"],
            chapter_id=chapter_id,
        )
        if is_new:
            inserted.append(fact)
    return inserted


def _build_entity_prompt(content: str) -> str:
    return (
        "请从下面的章节正文中提取持续生效的实体及其相互关系，供长篇连载保持一致设定。\n"
        "实体类型 entity_type 只能是：person(人物) / location(地点) / item(物品) / "
        "faction(阵营/势力) / world_rule(世界规则)。\n"
        "规则：\n"
        "- 只提取在本章有实质表现或明确新信息的实体（出场、行动、状态变化、身份揭示）；\n"
        "- name 使用**规范全名**（canonical name）：首次出现全名则用全名；若本章只用"
        "称呼/简称/别名，请推断其规范全名并填到 name，把出现的称呼填入 aliases；\n"
        "- aliases 为该实体的别名/简称/称谓数组（如 name=\"沈怀舟\"，aliases=[\"沈建国\", "
        "\"怀舟\", \"父亲\"]），帮助跨章节识别同一人物，防止姓名漂移；\n"
        "- description 用一句话概括该实体当前的关键状态（不超过 80 字）；\n"
        "- confidence 为 0~1 的置信度（文中明确 0.9，仅推测 0.6；aliases 推断的 "
        "confidence 请给 0.6 以下，便于人工确认）；\n"
        f"- 最多 {MAX_ENTITY_ITEMS} 个实体、{MAX_RELATION_ITEMS} 条关系；没有符合的返回空数组；\n"
        "- 必须返回 JSON 对象，格式："
        "{\"entities\": [{\"entity_type\": \"person\", \"name\": \"规范全名\", "
        "\"aliases\": [\"别名1\", \"别名2\"], "
        "\"description\": \"当前状态\", \"confidence\": 0.9}], "
        "\"relations\": [{\"from\": \"角色A\", \"relation\": \"knows\", "
        "\"to\": \"角色B\", \"confidence\": 0.8}]}；\n"
        "- relation 取值参考：kinship(亲缘)/ally(盟友)/enemy(敌对)/master(师承)/"
        "possesses(拥有)/located_at(位于)/member_of(隶属)/knows(相识)/loves(爱慕)。\n\n"
        f"章节正文：\n{content[:6000]}"
    )


def _parse_entities(reply: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """鲁棒解析实体/关系 JSON（容忍代码块围栏 / 前后杂文），返回 (entities, relations)。"""
    if not reply:
        return [], []
    json_text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", json_text, re.DOTALL)
    if fence:
        json_text = fence.group(1).strip()
    start = json_text.find("{")
    end = json_text.rfind("}")
    if start < 0 or end <= start:
        return [], []
    try:
        data = json.loads(json_text[start : end + 1])
    except json.JSONDecodeError:
        return [], []
    if not isinstance(data, dict):
        return [], []

    entities: list[dict[str, Any]] = []
    for item in (data.get("entities") or [])[:MAX_ENTITY_ITEMS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("canonical_name") or "").strip()
        if not name:
            continue
        # 别名：显式 aliases 数组 + 历史兼容字段 alias
        raw_aliases: list[str] = []
        if isinstance(item.get("aliases"), list):
            raw_aliases = [str(a).strip() for a in item["aliases"] if str(a).strip()]
        elif isinstance(item.get("alias"), str) and item["alias"].strip():
            raw_aliases = [item["alias"].strip()]
        entities.append(
            {
                "entity_type": str(item.get("entity_type") or "person").strip()[:20],
                "name": name[:MAX_ENTITY_NAME],
                "aliases": [a[:MAX_ENTITY_NAME] for a in raw_aliases],
                "description": str(item.get("description") or "").strip()[:MAX_ENTITY_DESC],
                "confidence": _clamp_confidence(item.get("confidence")),
            }
        )
    relations: list[dict[str, Any]] = []
    for item in (data.get("relations") or [])[:MAX_RELATION_ITEMS]:
        if not isinstance(item, dict):
            continue
        frm = str(item.get("from") or "").strip()
        to = str(item.get("to") or "").strip()
        if not frm or not to:
            continue
        relations.append(
            {
                "from": frm[:MAX_ENTITY_NAME],
                "relation": str(item.get("relation") or "related").strip()[:30],
                "to": to[:MAX_ENTITY_NAME],
                "confidence": _clamp_confidence(item.get("confidence")),
            }
        )
    return entities, relations


async def extract_entities(project_id: str, chapter_id: str, content: str) -> int:
    """从章节正文抽取实体/关系并幂等写入 memory_entities / memory_relations。

    - 实体幂等：project_id + entity_type + name 唯一，重复 upsert 更新描述/置信度；
    - 关系幂等：project_id + entity_id + relation_type + target 唯一；
    - LLM 失败 / JSON 解析失败一律降级跳过，绝不阻塞生成任务。
    返回成功写入的实体数。
    """
    if not content or not content.strip():
        return 0
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": ENTITY_SYSTEM_PROMPT},
                {"role": "user", "content": _build_entity_prompt(content)},
            ],
            role=ROLE_SUMMARY,
            max_tokens=800,
        )
        entities, relations = _parse_entities(reply)
    except (LLMConfigurationError, LLMResponseError):
        return 0

    if not entities:
        return 0
    try:
        return _write_entities_with_normalization(
            project_id, chapter_id, entities, relations
        )
    except Exception:  # noqa: BLE001  实体写入异常降级，不阻塞任务
        return 0


def _write_entities_with_normalization(
    project_id: str, chapter_id: str,
    entities: list[dict[str, Any]], relations: list[dict[str, Any]],
) -> int:
    """实体/关系写入 + 别名归一化（验收 P1：实体归一化）。

    - 规范化匹配：新实体 name/aliases 先做规范化（全角/空格/称谓），
      再与存量实体（name + aliases_json）比对；
    - 高置信度别名命中（confidence ≥ 0.8）→ 自动合并进已有实体
      （merge_entities），防"沈建国/沈怀舟"式姓名漂移；
    - 低置信度 → 各自独立写入（留待人工确认，不自动合并）；
    - 关系幂等：project_id + entity_id + relation_type + target 唯一。
    返回成功写入（或合并）的实体数。
    """
    # 跨章关系解析：先载入存量实体名→id 映射（含别名，长书 500 实体以内足够）
    name_to_id: dict[str, str] = {}
    alias_to_id: dict[str, str] = {}
    for existing in memory_entities_dao.list_entities(
        project_id, limit=1000, include_superseded=True
    ):
        name_to_id.setdefault(existing["name"], existing["id"])
        norm = memory_entities_dao.normalize_name(existing["name"])
        if norm:
            alias_to_id.setdefault(norm, existing["id"])
        for alias in existing.get("aliases") or []:
            norm_alias = memory_entities_dao.normalize_name(str(alias))
            if norm_alias:
                alias_to_id.setdefault(norm_alias, existing["id"])

    written = 0
    for ent in entities:
        entity_type = (
            ent["entity_type"]
            if ent["entity_type"] in memory_entities_dao.ENTITY_TYPES
            else "person"
        )
        ent_name = (ent.get("name") or "").strip()
        if not ent_name:
            continue
        ent_aliases = ent.get("aliases") or []
        norm_name = memory_entities_dao.normalize_name(ent_name)
        # 新实体全部规范化名称（name + aliases）——任一名命中即视为同一实体
        new_norms: set[str] = {norm_name} if norm_name else set()
        for a in ent_aliases:
            na = memory_entities_dao.normalize_name(str(a))
            if na:
                new_norms.add(na)

        # ① 高置信度别名命中 → 合并进已有实体（防姓名漂移）
        existing_id: str | None = None
        for candidate in new_norms:
            existing_id = alias_to_id.get(candidate) or name_to_id.get(candidate)
            if existing_id:
                break
        if existing_id is None:
            # 尝试按规范化名精确查找（DAO 层）
            existing_id = memory_entities_dao.find_entity_id_by_alias(
                project_id, ent_name, entity_type=entity_type
            )

        if existing_id and float(ent.get("confidence") or 0.8) >= 0.8:
            # 更新已有实体的描述/别名（不新建，避免漂移为两个实体）
            cur = memory_entities_dao.get_entity(project_id, existing_id)
            merged_aliases = list((cur or {}).get("aliases") or [])
            for a in ent_aliases:
                if a and a not in merged_aliases and a != (cur or {}).get("name"):
                    merged_aliases.append(a)
            if ent_name != (cur or {}).get("name"):
                merged_aliases.append(ent_name)
            memory_entities_dao.upsert_entity(
                project_id=project_id,
                entity_type=entity_type,
                name=(cur or {}).get("name") or ent_name,
                description=ent.get("description") or (cur or {}).get("description") or "",
                aliases=merged_aliases,
                confidence=max(float(ent.get("confidence") or 0.8), float((cur or {}).get("confidence") or 0.8)),
                source_chapter_id=chapter_id,
            )
            entity_id = existing_id
            name_to_id[ent_name] = entity_id
            if norm_name:
                alias_to_id[norm_name] = entity_id
            for a in ent_aliases:
                norm_a = memory_entities_dao.normalize_name(a)
                if norm_a:
                    alias_to_id[norm_a] = entity_id
            written += 1
            continue

        # ② 无别名命中 → 正常 upsert（含低置信度新实体，独立写入留人工确认）
        entity_id = memory_entities_dao.upsert_entity(
            project_id=project_id,
            entity_type=entity_type,
            name=ent_name,
            description=ent.get("description") or "",
            aliases=ent_aliases,
            confidence=float(ent.get("confidence") or 0.8),
            source_chapter_id=chapter_id,
        )
        name_to_id[ent_name] = entity_id
        if norm_name:
            alias_to_id[norm_name] = entity_id
        for a in ent_aliases:
            norm_a = memory_entities_dao.normalize_name(a)
            if norm_a:
                alias_to_id[norm_a] = entity_id
        written += 1

    # 关系写入：用规范名 + 别名解析实体 id
    for rel in relations:
        from_id = name_to_id.get(rel["from"]) or alias_to_id.get(
            memory_entities_dao.normalize_name(rel["from"])
        )
        to_id = name_to_id.get(rel["to"]) or alias_to_id.get(
            memory_entities_dao.normalize_name(rel["to"])
        )
        if not from_id or not to_id or from_id == to_id:
            continue
        memory_entities_dao.upsert_relation(
            project_id=project_id,
            entity_id=from_id,
            relation_type=rel["relation"],
            target_entity_id=to_id,
            confidence=rel["confidence"],
            source_chapter_id=chapter_id,
        )
    return written


# ── 重写版本替换（验收 P1-1：重写后旧版本必须失效，抽取失败保留旧版） ──

# ── 跨章事实矛盾消解（Codex P2 复测 P1：事实账本矛盾消解） ──

CONFLICT_RESOLVE_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _build_conflict_resolve_prompt(
    new_facts: list[dict[str, str]], existing_facts: list[dict[str, Any]]
) -> str:
    new_lines = "\n".join(
        f"- [{f.get('fact_type', '')}] {f.get('content', '')}" for f in new_facts
    ) or "- 无"
    existing_lines = "\n".join(
        f"- [{f.get('fact_type', '')}] {f.get('content', '')}" for f in existing_facts
    ) or "- 无"
    return (
        "下面是新章节揭示的事实（新事实，代表故事当前状态）与既有事实账本"
        "（旧事实，来自之前章节）。\n"
        "请判断：哪些**旧事实**与新事实构成**直接矛盾**（同一主体/道具/事件出现"
        "两种互相排斥的说法，且新事实是明确的剧情推进或揭示），应被新事实取代？\n"
        "规则：\n"
        "- 只标记**直接矛盾**的旧事实（如：旧说'守碑人是陈九'，新说'守碑人是程老七'）；\n"
        "- 不标记可共存的补充信息（如旧说'沈墨有剑'，新说'剑鞘刻字'）；\n"
        "- 不标记剧情反转的铺垫（新事实未明确推翻旧事实时，旧事实保留）；\n"
        "- 新事实本身不参与标记（只标记旧事实）。\n"
        "返回 JSON：{\"supersede_ids\": [旧事实 id 数组]}。\n"
        "没有矛盾时 supersede_ids 返回空数组。\n\n"
        f"新事实：\n{new_lines}\n\n"
        f"既有事实账本：\n{existing_lines}"
    )


def _parse_conflict_resolve_result(reply: str) -> list[str]:
    """解析矛盾消解结果，返回应失效的旧事实 id 列表。"""
    if not reply or not reply.strip():
        return []
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end])
            if isinstance(data, dict):
                ids = data.get("supersede_ids") or []
                if isinstance(ids, list):
                    return [str(i).strip() for i in ids if str(i).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


async def resolve_cross_chapter_fact_conflicts(
    project_id: str,
    new_facts: list[dict[str, str]],
) -> int:
    """跨章事实矛盾消解：检测新事实与既有 active 事实的矛盾，失效旧事实。

    背景：supersede_facts_by_chapter 只失效"同一章来源"的旧事实，无法处理
    跨章矛盾（如新章揭示"守碑人是程老七" vs 早期"守碑人是陈九"并存）。
    本函数用一次 LLM 调用检测矛盾，将旧事实标记 superseded，保证账本单一事实源。

    成本：每章 +1 次 LLM 调用（ROLE_SUMMARY，便宜模型）。
    全程降级：LLM 失败返回 0，不阻塞生成。
    返回被失效的旧事实数。
    """
    if not new_facts:
        return 0
    # 取既有 active 事实（completed/revealed + 近期 state_changed）
    existing = novel_facts_dao.list_completed_revealed_facts(project_id)
    recent_state = novel_facts_dao.list_recent_state_changed_facts(project_id)
    existing = existing + list(reversed(recent_state))
    if not existing:
        return 0
    # 控制输入规模：最多取 40 条既有事实（成本控制）
    existing = existing[:40]
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": CONFLICT_RESOLVE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_conflict_resolve_prompt(new_facts, existing),
                },
            ],
            role=ROLE_SUMMARY,
            max_tokens=400,
        )
        supersede_ids = _parse_conflict_resolve_result(reply)
        if not supersede_ids:
            return 0
        return novel_facts_dao.supersede_facts_by_ids(project_id, supersede_ids)
    except (LLMConfigurationError, LLMResponseError):
        return 0


async def replace_chapter_memory(
    project_id: str, chapter_id: str, content: str
) -> dict[str, Any]:
    """重写后版本替换：旧设定失效、新设定生效（验收 P1-1 修复）。

    语义（与验收建议一致）：
    1. 先用新版本正文抽取事实/实体/关系（LLM）；
    2. 抽取成功 → 写新版本，再把该章旧版本中"新版本不再包含"的
       事实/实体/关系标记 superseded（新版本仍存在的保持 active）；
    3. 抽取失败 → 保留旧版本（不清空记忆）；
    4. 事实/实体 upsert 遇 superseded 同名记录自动"复活"（改回原设定不丢）。

    返回统计 dict：
    {"ok", "facts_written", "facts_superseded", "entities_written",
     "entities_superseded", "relations_written", "relations_superseded"}
    """
    result: dict[str, Any] = {
        "ok": False,
        "facts_written": 0,
        "facts_superseded": 0,
        "entities_written": 0,
        "entities_superseded": 0,
        "relations_written": 0,
        "relations_superseded": 0,
    }
    if not content or not content.strip():
        return result

    # ── 1. 抽取新版本（失败 → 保留旧版本，直接返回） ──
    try:
        fact_reply = await llm.chat(
            [
                {"role": "system", "content": FACT_SYSTEM_PROMPT},
                {"role": "user", "content": _build_fact_prompt(content)},
            ],
            role=ROLE_SUMMARY,
            max_tokens=800,
        )
        new_facts = _parse_facts(fact_reply)
    except (LLMConfigurationError, LLMResponseError):
        return result  # 抽取失败 → 旧版本全部保留

    # ── 2. 写新版本事实（含 superseded 同名"复活"） ──
    new_fact_contents: set[str] = set()
    for fact in new_facts:
        fact_content = fact["content"]
        new_fact_contents.add(fact_content)
        # 先尝试复活已 superseded 的同 content 记录（改回原设定场景）
        revived = novel_facts_dao.reactivate_fact_if_superseded(
            project_id, fact_content, chapter_id=chapter_id
        )
        if not revived:
            novel_facts_dao.add_fact(
                project_id=project_id,
                fact_type=fact["fact_type"],
                content=fact_content,
                chapter_id=chapter_id,
            )
            result["facts_written"] += 1
        else:
            result["facts_written"] += 1

    # ── 3. 失效该章旧版本中"新版本不再包含"的事实 ──
    result["facts_superseded"] = novel_facts_dao.supersede_facts_by_chapter(
        project_id, chapter_id, keep_contents=new_fact_contents
    )

    # ── 3.5 跨章事实矛盾消解（Codex P2 复测 P1） ──
    # 新事实可能推翻早期章节的设定（如"守碑人是陈九"→"守碑人是程老七"），
    # supersede_facts_by_chapter 只失效同章旧事实���无法处理跨章矛盾。
    # 用一次 LLM 调用检测矛盾并失效旧事实，保证账本单一事实源。
    try:
        result["facts_superseded"] += await resolve_cross_chapter_fact_conflicts(
            project_id, new_facts
        )
    except Exception:  # noqa: BLE001  全程降级，绝不阻塞生成
        pass

    # ── 4. 实体/关系版本替换（实体抽取失败不阻塞事实替换，实体旧版保留） ──
    try:
        entity_reply = await llm.chat(
            [
                {"role": "system", "content": ENTITY_SYSTEM_PROMPT},
                {"role": "user", "content": _build_entity_prompt(content)},
            ],
            role=ROLE_SUMMARY,
            max_tokens=800,
        )
        new_entities, new_relations = _parse_entities(entity_reply)
    except (LLMConfigurationError, LLMResponseError):
        new_entities, new_relations = [], []

    if new_entities:
        try:
            # 版本替换 + 别名归一化：写入新实体（复用归一化写入器）
            written = _write_entities_with_normalization(
                project_id, chapter_id, new_entities, new_relations
            )
            result["entities_written"] += written
            result["relations_written"] += len(new_relations)
            # 载入写入后的实体名→id（含别名，用于旧版本比对）
            new_entity_names: set[str] = set()
            new_rel_keys: set[tuple[str, str, str]] = set()
            name_to_id: dict[str, str] = {}
            for existing in memory_entities_dao.list_entities(
                project_id, limit=1000, include_superseded=True
            ):
                name_to_id.setdefault(existing["name"], existing["id"])
                for alias in existing.get("aliases") or []:
                    name_to_id.setdefault(alias, existing["id"])
            for ent in new_entities:
                new_entity_names.add(ent["name"])
                for alias in ent.get("aliases") or []:
                    new_entity_names.add(alias)
            # 新关系（以新版本实体名 + 别名解析）
            for rel in new_relations:
                from_id = name_to_id.get(rel["from"])
                to_id = name_to_id.get(rel["to"])
                if not from_id or not to_id or from_id == to_id:
                    continue
                new_rel_keys.add((from_id, rel["relation"], to_id))
            # 失效该章旧实体/关系中"新版本不再包含"的
            result["entities_superseded"] = (
                memory_entities_dao.supersede_entities_by_chapter(
                    project_id, chapter_id, keep_names=new_entity_names
                )
            )
            result["relations_superseded"] = (
                memory_entities_dao.supersede_relations_by_chapter(
                    project_id, chapter_id, keep_keys=new_rel_keys
                )
            )
        except Exception:  # noqa: BLE001  实体替换降级：不阻塞事实替换
            pass

    result["ok"] = True
    return result


# ── 已完成事实注入块 ──────────────────────────────────

def _milestone_items(project_id: str) -> list[dict[str, Any]]:
    """已完成事实候选：completed/revealed 全量 + state_changed 最近 15 条，时间升序。"""
    facts = novel_facts_dao.list_completed_revealed_facts(project_id)
    recent_state = novel_facts_dao.list_recent_state_changed_facts(project_id)
    combined = facts + list(reversed(recent_state))
    if not combined:
        return []
    return sorted(combined, key=lambda f: (f.get("created_at") or "", f.get("id") or ""))


def build_completed_milestones(project_id: str) -> str:
    """组装\"已完成事实\"注入块：completed/revealed 全量 + state_changed 最近 15 条。

    对应 PRD：代写时注入 completed_milestones（\"已发生事实——不要再写一遍\"）。

    P1-2 长度上限：总长不超过 MAX_MILESTONES_LENGTH（1500 字）。超限时优先保留
    最近发生的事实（时间靠后），并保持时间正序输出，防止长书场景单块过大
    被下游 `contextBefore[-4000:]` 整体截断丢弃。
    """
    items = _milestone_items(project_id)
    if not items:
        return ""

    header = "【已完成事实】以下事件已经发生且不可撤销，请不要再重复描写或再次触发："
    lines: list[str] = []
    total = len(header) + 1
    for item in reversed(items):  # 从最近往前贪心保留，优先最近事实
        line = f"- {item['content']}"
        if total + len(line) > MAX_MILESTONES_LENGTH:
            continue
        lines.append(line)
        total += len(line) + 1
    lines.reverse()  # 恢复时间正序
    if not lines:  # 极端兜底：至少保留最近一条
        lines = [f"- {items[-1]['content']}"]
    return "\n".join([header, *lines])


def milestone_source_ids(project_id: str) -> list[str]:
    """返回与 `build_completed_milestones` 同条件的已注入事实 id（记忆召回日志 source_ids）。

    与注入块使用完全相同的贪心保留逻辑，保证日志与真实注入一致。
    """
    items = _milestone_items(project_id)
    if not items:
        return []
    header_len = len("【已完成事实】以下事件已经发生且不可撤销，请不要再重复描写或再次触发：") + 1
    total = header_len
    included: list[str] = []
    for item in reversed(items):
        line_len = len(f"- {item['content']}") + 1
        if total + line_len > MAX_MILESTONES_LENGTH:
            continue
        included.append(item["id"])
        total += line_len
    if not included:
        included = [items[-1]["id"]]
    return included


# ── 相关历史摘要（FTS5 关键词检索注入块，验收 M7） ──────

def search_related_history(
    project_id: str,
    keywords: list[str] | None,
    limit: int = FTS_RESULTS_LIMIT,
    exclude_chapter_ids: set[str] | list[str] | None = None,
) -> str:
    """按关键词检索相关历史章节并格式化为注入块；无结果返回空串。

    - 用 memory_fts.search_relevant（FTS5 优先，LIKE 兜底，零外部依赖）；
    - exclude_chapter_ids：排除当前章及之后章节，防止剧透（与 _build_memory_context 一致）；
    - 总长限制 FTS_RESULTS_MAX，超限截断，绝不抛异常。
    """
    if not keywords:
        return ""
    try:
        results = memory_fts_dao.search_relevant(project_id, keywords, limit=limit)
    except Exception:  # noqa: BLE001  检索异常降级返回空块
        return ""
    if exclude_chapter_ids:
        excluded = set(exclude_chapter_ids)
        results = [r for r in results if r["chapter_id"] not in excluded]
    if not results:
        return ""
    lines = ["【相关历史摘要】以下为与本章主题相关的过往章节，供设定与文风参考："]
    for result in results[:limit]:
        chapter_order = result.get("chapter_order") or ""
        title = result.get("title") or ""
        snippet = result.get("snippet") or ""
        lines.append(f"- 第{chapter_order}章《{title}》：{snippet}")
    return "\n".join(lines)[:FTS_RESULTS_MAX]


# ── 摘要对账（G3：从章摘要重建卷摘要并对比现有） ────────

def _signature_hash(parts: list[str]) -> str:
    """卷摘要的\"输入签名\"：卷内非空章摘要拼接的 MD5（确定性，用于漂移检测）。"""
    joined = "；".join(p for p in parts if p)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


# ── 卷级摘要 ──────────────────────────────────────────

def build_volume_specs(outline: dict[str, Any]) -> list[dict[str, Any]]:
    """从大纲卷节点派生卷规格；无卷节点时按每 10 章切分（PRD 允许的兜底）。

    返回：[{volume_id, title, order, start_chapter_id, end_chapter_id, chapter_ids}]
    """
    nodes = outline.get("nodes", []) if isinstance(outline, dict) else []
    volume_nodes = sorted(
        (n for n in nodes if n.get("type") == "volume"),
        key=lambda n: (n.get("order", 0), n.get("title", "")),
    )
    chapter_nodes = sorted(
        (n for n in nodes if n.get("type") == "chapter"),
        key=lambda n: (n.get("order", 0), n.get("title", "")),
    )

    specs: list[dict[str, Any]] = []
    if volume_nodes:
        for volume_node in volume_nodes:
            chapter_ids = [
                c["id"] for c in chapter_nodes if c.get("parentId") == volume_node["id"]
            ]
            if not chapter_ids:
                continue
            specs.append(
                {
                    "volume_id": volume_node["id"],
                    "title": volume_node.get("title", ""),
                    "order": volume_node.get("order", len(specs) + 1),
                    "start_chapter_id": chapter_ids[0],
                    "end_chapter_id": chapter_ids[-1],
                    "chapter_ids": chapter_ids,
                }
            )
        return specs

    for index in range(0, len(chapter_nodes), VOLUME_CHAPTERS):
        chunk = chapter_nodes[index : index + VOLUME_CHAPTERS]
        if not chunk:
            continue
        volume_no = index // VOLUME_CHAPTERS + 1
        specs.append(
            {
                "volume_id": f"vol-{volume_no}",
                "title": f"第{volume_no}卷",
                "order": volume_no,
                "start_chapter_id": chunk[0]["id"],
                "end_chapter_id": chunk[-1]["id"],
                "chapter_ids": [c["id"] for c in chunk],
            }
        )
    return specs


def ensure_volumes(project_id: str, outline: dict[str, Any]) -> list[dict[str, Any]]:
    """确保卷记录存在（幂等 UPSERT），返回卷规格列表。"""
    specs = build_volume_specs(outline)
    for spec in specs:
        volumes_dao.upsert_volume(
            project_id=project_id,
            volume_id=spec["volume_id"],
            title=spec["title"],
            volume_order=spec["order"],
            start_chapter_id=spec["start_chapter_id"],
            end_chapter_id=spec["end_chapter_id"],
        )
    return specs


async def compress_volume_summary(
    project_id: str, volume_id: str, chapter_ids: list[str]
) -> str:
    """把一卷章节摘要压缩为 300-600 字卷摘要；LLM 失败降级为拼接章摘要。

    返回空串表示该卷暂无可用章节摘要（无需压缩）。
    """
    chapter_summaries = chapters_dao.list_summaries_by_ids(project_id, chapter_ids)
    non_empty = [item["summary"] for item in chapter_summaries if item["summary"]]
    if not non_empty:
        return ""
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": VOLUME_SYSTEM_PROMPT},
                {"role": "user", "content": _build_volume_summary_prompt(non_empty)},
            ],
            role=ROLE_SUMMARY,
            max_tokens=800,
        )
        summary = repair_text(reply).strip()
        if len(summary) >= 100:
            return _clamp_volume_summary(summary)
    except (LLMConfigurationError, LLMResponseError):
        pass
    return _fallback_volume_summary(non_empty)


async def record_chapter_completed(
    project_id: str, outline: dict[str, Any], chapter_id: str
) -> None:
    """章节落库后调用：卷完结时懒压缩卷摘要 + 自动对账（LLM 失败降级，不抛异常）。

    记忆升级 v1：写入卷摘要时一并保存\"输入签名\"（summary_hash），
    供 reconcile_volume_summaries 检测章摘要变更导致的漂移。

    验收 P1（摘要对账自动化）：
    - 卷完结（end_chapter_id 落库）时，若卷无摘要 → 压缩生成；
    - 若卷已有摘要但存在漂移 / 无基线（章摘要后来被重写改变）→ apply=True
      自动重建并更新签名，保证后续生成注入的是最新卷摘要。
    """
    specs = build_volume_specs(outline)
    for spec in specs:
        if chapter_id != spec["end_chapter_id"]:
            continue
        volume = volumes_dao.get_volume(project_id, spec["volume_id"])
        if volume and not volume.get("summary"):
            summary = await compress_volume_summary(
                project_id, spec["volume_id"], spec["chapter_ids"]
            )
            if summary:
                chapter_summaries = chapters_dao.list_summaries_by_ids(
                    project_id, spec["chapter_ids"]
                )
                signature = _signature_hash(
                    [item["summary"] for item in chapter_summaries]
                )
                volumes_dao.update_volume_summary(
                    project_id, spec["volume_id"], summary, summary_hash=signature
                )
        # 验收 P1：卷完结时对已存在摘要的卷做漂移对账（自动重建修复）
        if volume and volume.get("summary"):
            try:
                await reconcile_volume_summaries(project_id, apply=True)
            except Exception:  # noqa: BLE001  对账失败降级，不阻塞生成任务
                pass
        break


async def reconcile_volume_summaries(
    project_id: str, apply: bool = False
) -> list[dict[str, Any]]:
    """从章摘要重建卷摘要并与现有对比，输出差异报告（G3 / 摘要对账）。

    判定：
    - ok            ：摘要存在且输入签名一致（未漂移）；
    - drift         ：章摘要已变化（签名 ≠ 已存 summary_hash），卷摘要可能过期；
    - missing       ：卷无摘要（从未压缩）；
    - no_baseline   ：有摘要但无 summary_hash（老数据），无法比对；
    - no_chapter_summaries / missing_volume：无章摘要 / 卷记录缺失。

    apply=True 时对 drift / missing / no_baseline 重新压缩并更新摘要与签名。
    返回每卷一份报告（含现有摘要、重建结果、判定、是否应用），绝不影响其它数据。
    """
    project = projects_dao.get_project(project_id)
    if not project:
        return []
    outline = project.get("outline") or {}
    specs = build_volume_specs(outline)
    report: list[dict[str, Any]] = []
    for spec in specs:
        volume = volumes_dao.get_volume(project_id, spec["volume_id"])
        chapter_summaries = chapters_dao.list_summaries_by_ids(
            project_id, spec["chapter_ids"]
        )
        non_empty = [item["summary"] for item in chapter_summaries if item["summary"]]
        signature = _signature_hash(non_empty) if non_empty else ""
        existing_summary = (volume or {}).get("summary") or ""
        current_hash = (volume or {}).get("summary_hash") or ""

        if volume is None:
            action = "missing_volume"
        elif not non_empty:
            action = "no_chapter_summaries"
        elif not existing_summary:
            action = "missing"
        elif current_hash and current_hash != signature:
            action = "drift"
        elif not current_hash:
            action = "no_baseline"
        else:
            action = "ok"

        item: dict[str, Any] = {
            "volume_id": spec["volume_id"],
            "title": spec["title"],
            "action": action,
            "existing_summary": existing_summary,
            "existing_chars": len(existing_summary),
            "chapter_summary_count": len(non_empty),
            "signature": signature,
            "applied": False,
        }
        if apply and action in ("missing", "drift", "no_baseline"):
            rebuilt = await compress_volume_summary(
                project_id, spec["volume_id"], spec["chapter_ids"]
            )
            if rebuilt:
                volumes_dao.update_volume_summary(
                    project_id, spec["volume_id"], rebuilt, summary_hash=signature
                )
                item["rebuilt_summary"] = rebuilt
                item["rebuilt_chars"] = len(rebuilt)
                item["applied"] = True
        report.append(item)
    return report


def _fallback_volume_summary(chapter_summaries: list[str]) -> str:
    """降级：拼接章摘要并裁剪到 600 字内。"""
    joined = "；".join(s for s in chapter_summaries if s)
    return _clamp_volume_summary(joined)


def _clamp_volume_summary(text: str) -> str:
    compact = " ".join((text or "").split()).strip()
    if not compact:
        return ""
    if len(compact) <= VOLUME_SUMMARY_MAX:
        return compact
    return compact[:VOLUME_SUMMARY_MAX] + "..."
