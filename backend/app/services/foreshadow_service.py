"""P2 伏笔账本（Foreshadow Engine）—— 长程跨卷张力管理。

对应参照项目 show-me-the-story 的伏笔管理拆解（reference-projects-deepdive.md 第 ① 节）：

生命周期闭环：
1. **建议**：`suggest_foreshadows` 基于全书大纲调 LLM 建议伏笔
   （name/description/plant_chapter/target_chapter），返回候选列表供用户确认；
   LLM 失败降级为空列表，不阻塞。
2. **确认入库**：用户在 API 层勾选确认 → `foreshadows_dao.create_foreshadows_batch`
   落库（status=planted）。
3. **活跃注入**：`build_foreshadow_instruction` 每章生成前只注入当前章的**活跃伏笔**
   （planted/progressing）块（省 token），接入 `_generate_chapter_with_expansion`；
   超期（当前章 > target_chapter + 3）输出"伏笔超期告警"。
4. **自动追踪**：`update_foreshadows_after_chapter` 每章生成后，把本章正文 + 现有伏笔
   列表喂 LLM 更新状态（planted→progressing→resolved/abandoned），记录 events；
   LLM 失败降级跳过不阻塞。
5. **超期告警**：当前章 > target_chapter + 3 时计入告警（任务 payload 暴露计数）。
6. **一致性检查**：`check_foreshadow_outline_consistency` 独立检查"伏笔 × 大纲"冲突
   （missing_payoff / weak_payoff / missing_plant / structural / out_of_range 五类，
   P2 基础版为确定性规则，零 LLM 成本）。

设计原则（与 M3 钩子账本 / M1 记忆底座一致）：
- 所有 LLM 调用失败均降级（跳过建议 / 跳过追踪），绝不抛出异常阻塞整书任务；
- 成本控制：注入只取活跃项 + 上限（token 可控），追踪 prompt 只喂本章正文窗口 + 伏笔列表；
- prompt 为自研文案，仅借鉴参照项目的架构思想，不复制其代码/长文本。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.dao import foreshadows as foreshadows_dao
from app.dao import projects as projects_dao
from app.llm.audit import reset_llm_audit_context, set_llm_audit_context
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.roles import ROLE_PLANNING
from app.utils.text_repair import repair_text

# ── 常量 ──────────────────────────────────────────────
MAX_SUGGESTIONS = 8          # 建议伏笔上限（成本控制）
SUGGEST_CHAPTER_CAP = 60     # 建议 prompt 注入的章节数上限（长书裁剪）
MAX_ACTIVE_INJECT = 8        # 生成前注入的活跃伏笔上限（token 可控）
OVERDUE_GRACE = 3            # 超期告警宽限：当前章 > target_chapter + 3 判定超期
TRACK_CONTENT_CAP = 2500     # 自动追踪 prompt 的本章正文窗口
MAX_ACTIVE_TRACK = 12        # 自动追踪一次处理的活跃伏笔上限
WEAK_PAYOFF_MIN_LENGTH = 8   # 回收方式说明少于该字数视为 weak_payoff

SUGGEST_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

TRACK_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

STATUS_LABELS = {
    foreshadows_dao.FORESHADOW_STATUS_PLANTED: "已埋设",
    foreshadows_dao.FORESHADOW_STATUS_PROGRESSING: "推进中",
    foreshadows_dao.FORESHADOW_STATUS_RESOLVED: "已回收",
    foreshadows_dao.FORESHADOW_STATUS_ABANDONED: "已放弃",
}


# ── 大纲章节工具 ──────────────────────────────────────

def _load_outline(project_id: str) -> dict[str, Any] | None:
    project = projects_dao.get_project(project_id)
    if not project:
        return None
    outline = project.get("outline")
    return outline if isinstance(outline, dict) else None


def chapter_nodes_from_outline(outline: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not outline:
        return []
    nodes = outline.get("nodes")
    if not isinstance(nodes, list):
        return []
    return sorted(
        [node for node in nodes if isinstance(node, dict) and node.get("type") == "chapter"],
        key=lambda node: (node.get("order", 0), node.get("title", "")),
    )


def _order_map(chapter_nodes: list[dict[str, Any]]) -> dict[str, int]:
    """章节 id → 章序号（1 起）。"""
    return {
        node["id"]: int(node.get("order") or index + 1)
        for index, node in enumerate(chapter_nodes)
    }


def _id_by_order(chapter_nodes: list[dict[str, Any]], order: int) -> str:
    """章序号 → 章节 id；越界或不存在返回空串。"""
    for node in chapter_nodes:
        if int(node.get("order") or 0) == int(order):
            return node["id"]
    return ""


def _total_chapters(chapter_nodes: list[dict[str, Any]]) -> int:
    return len(chapter_nodes)


# ── ① 建议伏笔（SuggestForeshadows） ─────────────────

def _build_suggest_prompt(outline: dict[str, Any]) -> str:
    chapter_nodes = chapter_nodes_from_outline(outline)
    chapters = chapter_nodes[:SUGGEST_CHAPTER_CAP]
    lines = [f"全书大纲摘要：{outline.get('mainSummary') or '（无）'}"]
    if chapters:
        lines.append("章节序列（第 N 章《标题》：摘要）：")
        lines.extend(
            f"- 第{int(node.get('order') or index + 1)}章《{node.get('title') or '未命名'}》："
            f"{node.get('summary') or node.get('conflict') or '（无摘要）'}"
            for index, node in enumerate(chapters)
        )
    lines.append(
        "请设计 3-6 条适合本书的长程伏笔，每条给出："
        "name（伏笔简称）、description（埋设方式/暗示内容/预期顿悟感，30-80 字）、"
        "plant_chapter（埋设章节序号，正整数）、target_chapter（预计回收章节序号，"
        "须大于 plant_chapter）。"
        "注意：跨卷/跨长程承诺，不要与单章钩子混淆；埋设章应早于回收章且留出推进空间。"
    )
    lines.append(
        '返回 JSON 数组：[{"name": "...", "description": "...", '
        '"plant_chapter": 5, "target_chapter": 40}]'
    )
    return "\n".join(lines)


def _parse_suggestions(reply: str) -> list[dict[str, Any]]:
    """鲁棒解析建议伏笔 JSON 数组（容忍代码块围栏 / 前后杂文）。"""
    if not reply or not reply.strip():
        return []
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    suggestions: list[dict[str, Any]] = []
    for item in data[:MAX_SUGGESTIONS]:
        if not isinstance(item, dict):
            continue
        name = repair_text(str(item.get("name") or "")).strip()
        if not name:
            continue
        plant = int(item.get("plant_chapter") or 0)
        target = int(item.get("target_chapter") or 0)
        suggestions.append(
            {
                "name": name[:foreshadows_dao.MAX_NAME_LENGTH],
                "description": repair_text(str(item.get("description") or "")).strip()[
                    :foreshadows_dao.MAX_DESCRIPTION_LENGTH
                ],
                "plant_chapter": max(plant, 0),
                "target_chapter": max(target, 0),
            }
        )
    return suggestions


async def suggest_foreshadows(project_id: str) -> list[dict[str, Any]]:
    """基于全书大纲调 LLM 建议伏笔候选列表（供用户确认）。

    返回候选（含 plant_chapter / target_chapter 序号与解析出的章节 id）；
    LLM 未配置或失败降级返回空列表（不阻塞）。
    """
    outline = _load_outline(project_id)
    if not outline:
        return []
    chapter_nodes = chapter_nodes_from_outline(outline)
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": SUGGEST_SYSTEM_PROMPT},
                {"role": "user", "content": _build_suggest_prompt(outline)},
            ],
            max_tokens=1200,
        )
        suggestions = _parse_suggestions(reply)
    except (LLMConfigurationError, LLMResponseError):
        return []

    # 序号 → 章节 id 解析；越界章节裁剪到有效范围；非法结构（target <= plant）剔除
    total = _total_chapters(chapter_nodes)
    resolved: list[dict[str, Any]] = []
    for suggestion in suggestions:
        plant_order = suggestion["plant_chapter"]
        target_order = suggestion["target_chapter"]
        if plant_order <= 0:
            plant_order = 1
        if target_order <= plant_order:
            target_order = min(plant_order + 10, total) if total else plant_order + 10
        if plant_order > total and total:
            plant_order = total
        if target_order > total and total:
            target_order = total
        plant_id = _id_by_order(chapter_nodes, plant_order) if total else ""
        target_id = _id_by_order(chapter_nodes, target_order) if total else ""
        resolved.append(
            {
                "name": suggestion["name"],
                "description": suggestion["description"],
                "plant_chapter": plant_order,
                "target_chapter": target_order,
                "plant_chapter_id": plant_id,
                "target_chapter_id": target_id,
            }
        )
    return resolved


# ── ①.5 手写伏笔（作者自己写完后登记，AI 只做整理） ────

DRAFT_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"
DRAFT_TEXT_CAP = 3000    # 送进 prompt 的作者文字上限（成本可控）
DRAFT_MAX_ITEMS = 5      # 一次最多整理出的条数（宁可少给，避免作者要删垃圾）


def _build_draft_prompt(
    text: str,
    outline: dict[str, Any] | None,
    chapter_nodes: list[dict[str, Any]],
    default_plant_order: int,
) -> str:
    lines = [
        "以下是作者自己写的伏笔记录（可能还包含情节描述、人物对话或零散要点）：",
        '"""',
        text[:DRAFT_TEXT_CAP],
        '"""',
    ]
    if chapter_nodes:
        lines.append("本书章节序列（第 N 章《标题》：摘要）：")
        lines.extend(
            f"- 第{int(node.get('order') or index + 1)}章《{node.get('title') or '未命名'}》："
            f"{node.get('summary') or '（无摘要）'}"
            for index, node in enumerate(chapter_nodes[:SUGGEST_CHAPTER_CAP])
        )
    if outline and (outline.get("mainSummary") or "").strip():
        lines.append(f"全书大纲摘要：{outline.get('mainSummary')}")
    plant_hint = f"，作者没写明就填 {default_plant_order}" if default_plant_order else "，作者没写明就填 0"
    lines.append(
        f"请整理出其中的长程伏笔（最多 {DRAFT_MAX_ITEMS} 条）。规则：\n"
        "1. 只算跨章的长程承诺/悬念；单章反转、一次性笑点、纯情节说明不算；\n"
        "2. name（伏笔简称，≤20 字）、description（30-80 字：埋设方式 / 暗示内容 / 预期阅读效果）；\n"
        f"3. plant_chapter（埋设章节序号）：以作者写明或所在章节为准{plant_hint}；\n"
        "4. target_chapter（预计回收章节序号）：作者没写明就填 0（表示待定），不要替作者猜；\n"
        "5. 不要编造作者没写的情节；宁可少给条目。\n"
        '返回 JSON 数组：[{"name": "...", "description": "...", "plant_chapter": 12, "target_chapter": 0}]'
    )
    return "\n".join(lines)


def _draft_degraded(reason: str) -> dict[str, Any]:
    return {"items": [], "degraded": True, "reason": reason}


def _resolve_draft_chapters(
    chapter_nodes: list[dict[str, Any]],
    item: dict[str, Any],
    default_plant_order: int,
) -> dict[str, Any]:
    """草稿的章序号 → 章节 id；不合法/未写明的目标章保留为「待定」（不替作者编造）。"""
    total = _total_chapters(chapter_nodes)
    plant_order = int(item.get("plant_chapter") or 0) or int(default_plant_order or 0)
    target_order = int(item.get("target_chapter") or 0)
    if plant_order and total and plant_order > total:
        plant_order = total
    if target_order and total and target_order > total:
        target_order = total
    if target_order and plant_order and target_order <= plant_order:
        target_order = 0  # 顺序不合法 → 退回待定，让作者自己定
    return {
        "name": item["name"],
        "description": item["description"],
        "plant_chapter": plant_order,
        "target_chapter": target_order,
        "plant_chapter_id": _id_by_order(chapter_nodes, plant_order) if plant_order else "",
        "target_chapter_id": _id_by_order(chapter_nodes, target_order) if target_order else "",
    }


async def draft_foreshadows_from_text(
    project_id: str,
    text: str,
    plant_chapter_id: str = "",
) -> dict[str, Any]:
    """把作者手写的文字整理成伏笔草稿（**只返回草稿，不落库**）。

    返回 {"items": [...], "degraded": bool, "reason": str}；
    LLM 不可用/失败时 degraded=True + 中文原因（不抛错，由前端如实告知）。
    """
    outline = _load_outline(project_id)
    chapter_nodes = chapter_nodes_from_outline(outline)
    order_map = _order_map(chapter_nodes)
    default_plant_order = order_map.get(plant_chapter_id, 0)

    token = set_llm_audit_context(project_id=project_id, role=ROLE_PLANNING)
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_draft_prompt(
                        text, outline, chapter_nodes, default_plant_order
                    ),
                },
            ],
            role=ROLE_PLANNING,
            max_tokens=900,
        )
    except LLMConfigurationError:
        return _draft_degraded("AI 未配置或不可用（请到「模型设置」检查 provider 与密钥）。")
    except LLMResponseError as exc:
        return _draft_degraded(f"AI 调用失败：{exc}。可重试，或直接手动填写。")
    finally:
        reset_llm_audit_context(token)

    parsed = _parse_suggestions(reply)[:DRAFT_MAX_ITEMS]
    items = [_resolve_draft_chapters(chapter_nodes, item, default_plant_order) for item in parsed]
    if not items:
        return {
            "items": [],
            "degraded": False,
            "reason": "AI 没从这段文字里认出长程伏笔，请手动填写。",
        }
    return {"items": items, "degraded": False, "reason": ""}


# ── 活跃伏笔解析（注入 / 超期告警共用） ──────────────

def _resolve_active(
    project_id: str,
    outline: dict[str, Any] | None,
    current_chapter_id: str = "",
    *,
    limit: int = MAX_ACTIVE_INJECT,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """解析当前章可注入的活跃伏笔（planted/progressing 且已到埋设章）。

    返回 (enriched_items, order_map)：
    - enriched_items：含 plant_order / target_order / overdue / overdue_by 的活跃伏笔；
      仅保留 plant_order <= 当前章序号 的项（未到埋设章的不注入）；
    - order_map：章节 id → 序号。
    """
    chapter_nodes = chapter_nodes_from_outline(outline)
    order_map = _order_map(chapter_nodes)
    total = _total_chapters(chapter_nodes)
    current_order = order_map.get(current_chapter_id, 0)

    items = foreshadows_dao.list_active_foreshadows(project_id, limit=max(limit * 2, 20))
    enriched: list[dict[str, Any]] = []
    for item in items:
        plant_order = order_map.get(item["plant_chapter_id"], 0)
        target_order = order_map.get(item["target_chapter_id"], 0)
        if current_order and plant_order and plant_order > current_order:
            continue  # 未到埋设章，不注入
        overdue = bool(current_order and target_order and current_order > target_order + OVERDUE_GRACE)
        enriched.append(
            {
                **item,
                "plant_order": plant_order,
                "target_order": target_order,
                "total_chapters": total,
                "overdue": overdue,
                "overdue_by": max(current_order - target_order, 0) if target_order else 0,
            }
        )
    return enriched[:limit], order_map


# ── ③ 活跃注入（生成前） ─────────────────────────────

def build_foreshadow_instruction(
    project_id: str,
    outline: dict[str, Any] | None,
    current_chapter_id: str = "",
) -> str:
    """生成前注入的「【伏笔】块」：只注入当前章的活跃伏笔 + 超期告警。

    返回空串表示无活跃伏笔（调用方不注入，零 token 开销）。
    """
    active, _ = _resolve_active(project_id, outline, current_chapter_id)
    if not active:
        return ""

    lines = ["【伏笔】以下为本书尚未回收的长程伏笔（跨卷承诺），本章需视剧情自然推进或回收："]
    for item in active:
        label = STATUS_LABELS.get(item["status"], item["status"])
        target = f"第{item['target_order']}章" if item["target_order"] else "待定"
        lines.append(
            f"- {item['name']}（状态：{label}，预计回收：{target}）"
            f"：{item['description'] or '（无描述）'}"
        )
    for item in active:
        if item.get("overdue"):
            lines.append(
                f"⚠️ 伏笔超期告警：《{item['name']}》已超过预计回收章节 {item['overdue_by']} 章，"
                "本章应优先安排推进或回收，避免读者遗忘。"
            )
    return "\n".join(lines)


def count_overdue_foreshadows(
    project_id: str,
    outline: dict[str, Any] | None,
    current_chapter_id: str,
) -> int:
    """当前章的超期伏笔告警数（当前章 > target_chapter + 3 的活跃伏笔条数）。"""
    active, _ = _resolve_active(project_id, outline, current_chapter_id)
    return sum(1 for item in active if item.get("overdue"))


# ── ③.5 超期明细（整书自动创作注入用） ────────────────

def overdue_active_foreshadows(
    project_id: str,
    current_order: int,
    *,
    limit: int = 5,
    grace: int = OVERDUE_GRACE,
) -> list[dict[str, Any]]:
    """按「章序号」列出已超期的活跃伏笔，供自动创作链路注入超期提醒。

    与 count_overdue_foreshadows 判定一致（current_order > target_order + grace），
    区别是入参为章序号而非章节 id，且返回可读明细，便于提示词直接引用。

    返回 [{name, target_order, overdue_by}]，按超期章数降序；任何异常返回空列表
    （降级：超期提醒失败绝不阻塞生成）。
    """
    try:
        if not current_order or current_order <= 0:
            return []
        outline = _load_outline(project_id)
        order_map = _order_map(chapter_nodes_from_outline(outline))
        items = foreshadows_dao.list_active_foreshadows(project_id, limit=MAX_ACTIVE_TRACK)
        overdue: list[dict[str, Any]] = []
        for item in items:
            plant_order = order_map.get(item["plant_chapter_id"], 0)
            target_order = order_map.get(item["target_chapter_id"], 0)
            if plant_order and plant_order > current_order:
                continue  # 本章尚未埋设，谈不上超期
            if not target_order or current_order <= target_order + grace:
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            overdue.append(
                {
                    "name": name,
                    "target_order": target_order,
                    "overdue_by": current_order - target_order,
                }
            )
        overdue.sort(key=lambda entry: (-entry["overdue_by"], entry["name"]))
        return overdue[:limit]
    except Exception:  # noqa: BLE001  降级：不阻塞生成
        return []


# ── ④ 自动追踪（生成后） ─────────────────────────────

def _build_track_prompt(content: str, active: list[dict[str, Any]]) -> str:
    lines = [
        "以下是本书当前的活跃伏笔（已埋设或推进中）。请判断本章正文对它们的推进情况。",
        "伏笔列表：",
    ]
    for item in active:
        target = f"第{item.get('target_order') or '?'}章" if item.get("target_order") else "待定"
        lines.append(
            f"- id={item['id']} 《{item['name']}》状态={item['status']} 预计回收={target}"
            f"：{item['description'] or '（无描述）'}"
        )
    lines.append(
        "请对每条被本章推进/回收/放弃的伏笔给出更新。规则：\n"
        "1. planted → 本章有暗示/线索推进 → progressing；本章直接揭晓 → resolved；\n"
        "2. progressing → 本章完成回收 → resolved；本章确认放弃该线 → abandoned；\n"
        "3. 无推进的伏笔不要出现在 updates 里；\n"
        "4. 不允许回退到 planted。\n"
        '返回 JSON：{"updates": [{"id": "伏笔id", "status": "progressing|resolved|abandoned", '
        '"event": "本章如何推进/回收/放弃（一句话）", "resolution": "回收方式（仅 resolved 时填写）"}]}'
    )
    lines.append(f"\n本章正文（前 {TRACK_CONTENT_CAP} 字）：\n{content[:TRACK_CONTENT_CAP]}")
    return "\n".join(lines)


def _parse_track_updates(reply: str) -> list[dict[str, Any]]:
    """鲁棒解析追踪结果 JSON（容忍代码块围栏 / 前后杂文）。"""
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
            updates = data.get("updates") if isinstance(data, dict) else None
            if isinstance(updates, list):
                return [
                    u
                    for u in updates
                    if isinstance(u, dict)
                    and (u.get("id") or "").strip()
                    and u.get("status") in foreshadows_dao.VALID_FORESHADOW_STATUSES
                ]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


async def update_foreshadows_after_chapter(
    project_id: str,
    chapter_id: str,
    chapter_order: int,
    content: str,
    outline: dict[str, Any] | None = None,
) -> int:
    """每章生成后自动追踪：LLM 更新活跃伏笔状态并记录事件。

    返回成功更新的伏笔数；LLM 失败降级返回 0（不阻塞整书任务）。
    非法状态迁移由 DAO 层 FORWARD_STATUSES 兜底拒绝（如回退 planted）。
    """
    if not content or not content.strip():
        return 0
    outline = outline or _load_outline(project_id)
    active = foreshadows_dao.list_active_foreshadows(project_id, limit=MAX_ACTIVE_TRACK)
    if not active:
        return 0
    order_map = _order_map(chapter_nodes_from_outline(outline))
    active_enriched = [
        {**item, "target_order": order_map.get(item["target_chapter_id"], 0)}
        for item in active
    ]

    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": TRACK_SYSTEM_PROMPT},
                {"role": "user", "content": _build_track_prompt(content, active_enriched)},
            ],
            max_tokens=1000,
        )
        updates = _parse_track_updates(reply)
    except Exception:  # noqa: BLE001  追踪是降级能力：任何异常都不得阻塞整书任务
        return 0

    updated = 0
    known_ids = {item["id"] for item in active}
    for update in updates:
        fid = str(update["id"] or "").strip()
        if fid not in known_ids:
            continue
        status = str(update.get("status") or "").strip()
        note = repair_text(str(update.get("event") or "")).strip()
        resolution = repair_text(str(update.get("resolution") or "")).strip()
        ok = foreshadows_dao.update_foreshadow(
            project_id,
            fid,
            status=status,
            resolution=resolution,
            event_note=note,
            chapter_id=chapter_id,
            chapter_order=int(chapter_order or 0),
        )
        if ok:
            updated += 1
    return updated


# ── ⑤ 一致性检查（基础版，确定性规则，零 LLM 成本） ──

def check_foreshadow_outline_consistency(
    project_id: str,
    outline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """独立检查"伏笔 × 大纲"冲突，产出报告供前端展示。

    P2 基础版：确定性规则（零 LLM 成本），五类问题：
    - structural：结构异常（埋设章序号 >= 回收章序号，或埋设/回收序号越界/不可解析）
    - out_of_range：埋设/回收章超出大纲章节总数（序号越界）
    - missing_plant：未放弃的伏笔缺少有效埋设章（无法落地）
    - missing_payoff：已回收伏笔没有填写回收方式
    - weak_payoff：已回收伏笔的回收方式过短（< WEAK_PAYOFF_MIN_LENGTH 字），疑似敷衍
    """
    outline = outline or _load_outline(project_id)
    chapter_nodes = chapter_nodes_from_outline(outline)
    order_map = _order_map(chapter_nodes)
    total = _total_chapters(chapter_nodes)
    items = foreshadows_dao.list_foreshadows(project_id)

    issues: list[dict[str, Any]] = []
    for item in items:
        fid = item["id"]
        name = item["name"] or "未命名伏笔"
        status = item["status"]
        plant_order = order_map.get(item["plant_chapter_id"], 0)
        target_order = order_map.get(item["target_chapter_id"], 0)
        resolution = (item["resolution"] or "").strip()

        # out_of_range：序号越界（不可解析视为 0，不重复报 out_of_range）
        if total and plant_order > total:
            issues.append(
                {
                    "type": "out_of_range",
                    "foreshadow_id": fid,
                    "name": name,
                    "detail": f"埋设章（第{plant_order}章）超出大纲章节总数（{total}）。",
                }
            )
        if total and target_order > total:
            issues.append(
                {
                    "type": "out_of_range",
                    "foreshadow_id": fid,
                    "name": name,
                    "detail": f"预计回收章（第{target_order}章）超出大纲章节总数（{total}）。",
                }
            )
        # structural：埋设 >= 回收（都有有效序号时才判定）
        if plant_order and target_order and plant_order >= target_order:
            issues.append(
                {
                    "type": "structural",
                    "foreshadow_id": fid,
                    "name": name,
                    "detail": f"埋设章（第{plant_order}章）不早于回收章（第{target_order}章），"
                    "伏笔埋设与回收顺序倒置。",
                }
            )
        # missing_plant：未放弃的伏笔缺少有效埋设章
        if status != foreshadows_dao.FORESHADOW_STATUS_ABANDONED and not plant_order:
            issues.append(
                {
                    "type": "missing_plant",
                    "foreshadow_id": fid,
                    "name": name,
                    "detail": "该伏笔没有解析到有效埋设章（可能埋设章不在大纲或为空）。",
                }
            )
        # missing_payoff / weak_payoff：已回收但回收方式缺失或过短
        if status == foreshadows_dao.FORESHADOW_STATUS_RESOLVED:
            if not resolution:
                issues.append(
                    {
                        "type": "missing_payoff",
                        "foreshadow_id": fid,
                        "name": name,
                        "detail": "伏笔已标记为回收，但未填写回收方式。",
                    }
                )
            elif len(resolution) < WEAK_PAYOFF_MIN_LENGTH:
                issues.append(
                    {
                        "type": "weak_payoff",
                        "foreshadow_id": fid,
                        "name": name,
                        "detail": f"回收方式过短（{len(resolution)} 字），疑似未真正完成回收。",
                    }
                )

    by_type: dict[str, int] = {}
    for issue in issues:
        by_type[issue["type"]] = by_type.get(issue["type"], 0) + 1

    return {
        "passed": not issues,
        "total": len(issues),
        "by_type": by_type,
        "issues": issues[:50],
    }


# ── 展示富化（前端列表用） ────────────────────────────

def enrich_foreshadows(
    project_id: str,
    outline: dict[str, Any] | None = None,
    current_chapter_id: str = "",
) -> list[dict[str, Any]]:
    """为列表展示富化：章序号 / 总章数 / 超期标记 / 事件倒序。"""
    outline = outline or _load_outline(project_id)
    chapter_nodes = chapter_nodes_from_outline(outline)
    order_map = _order_map(chapter_nodes)
    total = _total_chapters(chapter_nodes)
    current_order = order_map.get(current_chapter_id, 0)

    result: list[dict[str, Any]] = []
    for item in foreshadows_dao.list_foreshadows(project_id):
        plant_order = order_map.get(item["plant_chapter_id"], 0)
        target_order = order_map.get(item["target_chapter_id"], 0)
        overdue = bool(
            current_order
            and target_order
            and item["status"] in (
                foreshadows_dao.FORESHADOW_STATUS_PLANTED,
                foreshadows_dao.FORESHADOW_STATUS_PROGRESSING,
            )
            and current_order > target_order + OVERDUE_GRACE
        )
        result.append(
            {
                **item,
                "plant_chapter": plant_order,
                "target_chapter": target_order,
                "total_chapters": total,
                "overdue": overdue,
                "overdue_by": max(current_order - target_order, 0) if target_order else 0,
                "events": list(reversed(item.get("events") or [])),
            }
        )
    return result
