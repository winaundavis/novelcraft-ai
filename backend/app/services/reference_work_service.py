"""P2-3 参考书档案 · 服务层：把「一段/一整本参考文本」拆成结构化档案。

设计纪律（与文风档案严格区分，产品方 2026-10-06 确认）：
1. 指标**必须本地确定性算**（`extraction.compute_metrics/compute_dimensions`），绝不让模型编数字；
2. 只有「技法规则」用一次 LLM 调用（`extraction.extract_rules`）；失败自动降级为
   按指标生成的规则（`build_rules_from_metrics`）+ `status='degraded'` + 中文原因；
3. 采样后**如实告知**取了多少字（`sample_chars/sample_count`），不静默截断；
4. 参考档案**永不写 style_bindings**，不参与本书文风判定与过渡。
"""

from __future__ import annotations

import re
from typing import Any

from app.dao import projects as projects_dao
from app.dao import reference_works as reference_dao
from app.dao import style_dao
from app.services.style_engine import extraction

# 原文最多存这么多字（超出的部分不存，但**如实标注** truncated；不影响采样分析）
MAX_STORED_CHARS = 300_000
# 一次允许提交的原文上限（防止把 10MB 文本灌进来）
MAX_INPUT_CHARS = 2_000_000

_CHAPTER_HEADING = re.compile(
    r"(?m)^\s*(?:#{1,3}\s*)?第\s*[0-9一二三四五六七八九十百零两]+\s*[章回节卷].*$"
)


def split_pseudo_chapters(text: str) -> list[dict[str, Any]]:
    """把整本书文本按章标题切成"伪章节"，让既有的均匀采样能真正生效。

    切不出 3 段以上就当成一整块（例如只粘了一个片段）。
    注意：字段名要对齐 `extraction.sample_chapters` 的期望（order/title/content）。
    """
    headings = list(_CHAPTER_HEADING.finditer(text))
    if len(headings) < 3:
        return [{"order": 1, "title": "", "content": text}]
    chapters: list[dict[str, Any]] = []
    for index, match in enumerate(headings):
        start = match.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            chapters.append({"order": index + 1, "title": match.group(0).strip(), "content": chunk})
    return chapters or [{"order": 1, "title": "", "content": text}]


def _sample_text(text: str) -> tuple[str, int, int, int]:
    """采样：(样本文本, 全文长度, 样本长度, 抽样段数)。

    - 多段（≥ 3 个伪章节）→ 复用 `extraction.sample_chapters` 的均匀采样；
    - 单块 → 首/中/尾三截各取一段：**不往样本里塞任何原文没有的字**
      （不能走 sample_chapters，它会补一个"第N章"标题）。
    """
    chapters = split_pseudo_chapters(text)
    if len(chapters) >= 3:
        return extraction.sample_chapters(chapters)
    total = len(text)
    if total <= extraction.MAX_SAMPLE_CHARS:
        return text, total, total, 1
    budget = extraction.MAX_SAMPLE_CHARS
    sep = "\n\n"
    head_budget = int(budget * 0.4)
    middle_budget = int(budget * 0.25)
    tail_budget = budget - head_budget - middle_budget - len(sep) * 2  # 给连接符留位置，总长不超预算
    head = text[:head_budget]
    middle_start = max(0, total // 2 - middle_budget // 2)
    middle = text[middle_start : middle_start + middle_budget]
    tail = text[-tail_budget:]
    sample = sep.join([head, middle, tail])
    return sample, total, len(sample), 1


def _degraded_reason(exc: Exception) -> str:
    from app.llm.provider import LLMConfigurationError

    if isinstance(exc, LLMConfigurationError):
        return "AI 未配置或不可用（请到「模型设置」检查 provider 与密钥），已按本地统计生成规则。"
    return f"AI 拆解失败：{exc}，已按本地统计生成规则。"


async def analyze_reference_text(text: str) -> dict[str, Any]:
    """拆解一段/一本参考文本 → 档案字段（纯计算 + 至多一次 LLM 调用，不落库）。"""
    chapters = split_pseudo_chapters(text)
    sample_text, total_chars, sample_chars, sample_count = _sample_text(text)
    metrics = extraction.compute_metrics(sample_text)
    dimensions = extraction.compute_dimensions(metrics)

    llm_used = False
    status_reason = ""
    try:
        extracted = await extraction.extract_rules(sample_text, metrics)
        llm_used = True
    except Exception as exc:  # noqa: BLE001 —— 与文风分析同样的纪律：失败一律降级，不抛给用户
        extracted = extraction.build_rules_from_metrics(metrics)
        status_reason = _degraded_reason(exc)

    return {
        "char_count": total_chars,
        "sample_chars": sample_chars,
        "sample_count": sample_count,
        "metrics": metrics,
        "dimensions": dimensions,
        "narrative_rules": extracted.get("narrative_rules") or [],
        "character_rules": extracted.get("character_rules") or [],
        "language_rules": extracted.get("language_rules") or [],
        "rhythm_rules": extracted.get("rhythm_rules") or [],
        "anti_ai_rules": extracted.get("anti_ai_rules") or [],
        "strengths": extracted.get("strengths") or [],
        "weaknesses": extracted.get("weaknesses") or [],
        "report": extracted.get("report") or "",
        "status": "completed" if llm_used else "degraded",
        "status_reason": status_reason,
        "llm_used": llm_used,
    }


def _default_name(text: str, index: int) -> str:
    """没起名时用首行（截 30 字）当名字，再不行就"未命名参考 N"。"""
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if len(cleaned) >= 2:
            return cleaned[:30]
    return f"未命名参考 {index}"


async def create_reference_work(
    project_id: str,
    owner_id: str,
    text: str,
    name: str = "",
    source_note: str = "",
    tags: Any = None,
) -> dict[str, Any]:
    """建档：拆解 + 落库（不碰 style_bindings）。档案归 owner，**跳项目共享**。"""
    analysis = await analyze_reference_text(text)
    stored_text = text[:MAX_STORED_CHARS]
    existing = reference_dao.list_reference_works(owner_id)
    final_name = (name or "").strip() or _default_name(text, len(existing) + 1)
    return reference_dao.create_reference_work(
        project_id,
        owner_id,
        name=final_name,
        raw_text=stored_text,
        source_note=source_note,
        tags=tags,
        stored_chars=len(stored_text),
        truncated=len(stored_text) < len(text),
        **analysis,
    )


async def reanalyze_reference_work(owner_id: str, reference_id: str) -> dict[str, Any] | None:
    """用已存原文重新拆解（例如上次 AI 不可用、或想换个模型再试一次）。"""
    record = reference_dao.get_reference_work(owner_id, reference_id)
    if not record:
        return None
    text = record.get("raw_text") or ""
    if not text.strip():
        raise ValueError("这条档案没有保存原文，无法重新拆解。")
    analysis = await analyze_reference_text(text)
    return reference_dao.update_reference_work(owner_id, reference_id, **analysis)


# ── 验收护栏：参考档案与文风档案必须互不干扰 ──────────────

def assert_isolated_from_style_binding(project_id: str, owner_id: str) -> dict[str, Any]:
    """返回诊断信息（供测试/健康断言）：参考档案不应出现在文风绑定里。"""
    binding = style_dao.get_binding(project_id)
    bound_id = (binding or {}).get("profileId") or ""
    reference_ids = [item["id"] for item in reference_dao.list_reference_works(owner_id)]
    return {
        "bound_profile_id": bound_id,
        "reference_ids": reference_ids,
        "leaked": bound_id in reference_ids,
    }


# ── P2：把「正在参考的那一份」注入创作上下文 ──────────────

# 注入块预算（token 可控）与措辞：**必须**写明"只作表达参考，不覆盖本书风格与设定"，
# 否则模型可能把参考当成目标风格去写（这是产品方确认过的措辞）。
REFERENCE_BLOCK_MAX_CHARS = 420        # 单份上限
REFERENCE_BLOCK_TOTAL_MAX_CHARS = 900  # 多份合起来的上限（不够就先丢强度低的）
REFERENCE_BLOCK_MAX_RULES = 5
REFERENCE_BLOCK_HEADER = "【参考写法（来自《{name}》，仅作表达参考，不覆盖本书既有风格与设定）】"


def _owner_of(project_id: str) -> str:
    """项目的作者（档案归属人）。项目不存在/异常时回落到默认用户，绝不因此报错。"""
    try:
        return str(projects_dao.get_project_owner(project_id) or "local-dev")
    except Exception:  # noqa: BLE001  降级：注入链路不应因此中断
        return "local-dev"


def _render_one(record: dict[str, Any]) -> str:
    """把一份档案渲染成注入块（只用已存规则与本地实测指标，零模型调用）。"""
    name = str(record.get("name") or "参考档案")
    lines: list[str] = []
    for key, label in (
        ("language_rules", "语言"),
        ("rhythm_rules", "节奏"),
        ("narrative_rules", "叙事"),
        ("character_rules", "人物"),
    ):
        for item in (record.get(key) or [])[:2]:
            rule = str((item or {}).get("rule") or "").strip()
            if rule:
                lines.append(f"- {label}：{rule}")
            if len(lines) >= REFERENCE_BLOCK_MAX_RULES:
                break
        if len(lines) >= REFERENCE_BLOCK_MAX_RULES:
            break
    metrics = record.get("metrics") or {}
    avg = metrics.get("avgSentenceLength")
    dialogue = metrics.get("dialogueRatio")
    if isinstance(avg, (int, float)) and avg:
        lines.append(f"- 参考统计：平均句长 {avg} 字")
    if isinstance(dialogue, (int, float)) and dialogue:
        lines.append(f"- 参考统计：对话占比 {round(float(dialogue) * 100)}%")
    if not lines:
        return ""
    block = REFERENCE_BLOCK_HEADER.format(name=name) + "\n" + "\n".join(lines)
    return block[:REFERENCE_BLOCK_MAX_CHARS]


def get_active_references(project_id: str) -> list[dict[str, Any]]:
    """本项目启用的参考档案（含强度），强度降序；悬空/已删的自动跳过。"""
    owner_id = _owner_of(project_id)
    active: list[dict[str, Any]] = []
    for binding in reference_dao.list_bindings(project_id):
        record = reference_dao.get_reference_work(
            owner_id, str(binding.get("reference_id") or ""), include_text=False
        )
        if not record:
            continue
        active.append({**record, "weight": int(binding.get("weight") or reference_dao.DEFAULT_BINDING_WEIGHT)})
    return active


def get_active_reference(project_id: str) -> dict[str, Any] | None:
    """兼容单份调用：返回强度最高的那份（没有启用则 None）。"""
    active = get_active_references(project_id)
    return active[0] if active else None


def set_active_references(
    project_id: str,
    owner_id: str,
    items: list[tuple[str, int]],
) -> list[dict[str, Any]] | None:
    """整体替换「正在参考」集合；`items` 为 (reference_id, weight)。空列表=全部停用。

    返回设置后的列表；只要有一条档案不属于该 owner 就**整体拒绝**（返回 None，不半途写入）。
    严格遵守：只写我们自己的 `reference_bindings`，**绝不碰 style_bindings**。
    """
    records: dict[str, dict[str, Any]] = {}
    for reference_id, _weight in items:
        rid = str(reference_id or "").strip()
        if not rid or rid in records:
            continue
        record = reference_dao.get_reference_work(owner_id, rid, include_text=False)
        if not record:
            return None
        records[rid] = record
    reference_dao.replace_bindings(project_id, [(rid, weight) for rid, weight in items])
    return get_active_references(project_id)


def set_active_reference(
    project_id: str,
    owner_id: str,
    reference_id: str,
    weight: int = reference_dao.DEFAULT_BINDING_WEIGHT,
) -> list[dict[str, Any]] | None:
    """单份开关（兼容旧调用）：空 id 表示全部停用；否则**只启用这一份**。"""
    if not reference_id:
        return set_active_references(project_id, owner_id, [])
    return set_active_references(project_id, owner_id, [(reference_id, weight)])


def render_reference_blocks(project_id: str) -> tuple[str, list[dict[str, Any]]]:
    """按强度降序拼装多份参考；总额受限（预算不够时**先丢强度低的**）。

    返回 (文本, 实际参与注入的 [{id, name, weight}])；没有启用/全部失效时返回 ("", [])。
    """
    try:
        parts: list[str] = []
        used: list[dict[str, Any]] = []
        total = 0
        for record in get_active_references(project_id):
            block = _render_one(record)
            if not block:
                continue
            if total + len(block) + 2 > REFERENCE_BLOCK_TOTAL_MAX_CHARS:
                break  # 后面的强度更低，直接留给下一轮
            parts.append(block)
            total += len(block) + 2  # 段间空行
            used.append(
                {
                    "id": str(record.get("id") or ""),
                    "name": str(record.get("name") or ""),
                    "weight": int(record.get("weight") or reference_dao.DEFAULT_BINDING_WEIGHT),
                }
            )
        return "\n\n".join(parts), used
    except Exception:  # noqa: BLE001  降级：注入失败绝不能阻塞创作
        return "", []


def render_reference_block(project_id: str) -> tuple[str, str]:
    """兼容旧调用：返回 (合并文本, 第一份的 id)。"""
    text, used = render_reference_blocks(project_id)
    return text, (used[0]["id"] if used else "")
