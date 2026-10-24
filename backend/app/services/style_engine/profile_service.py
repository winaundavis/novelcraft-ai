"""M4 文风画像引擎 · 画像服务（编排层）。

- analyze_project_style(project_id)：采样 → LLM 抽取 → 落库 style_profiles + style_bindings；
  LLM 失败自动降级为纯规则统计（status='degraded'，metrics 仍可产出）。
- get_profile(project_id)：查询项目当前生效画像。
- build_style_contract(project_id) / compile_contract()：把「预设五维 + 画像规则
  + 过渡档位」编译成不覆盖故事事实的风格偏好契约。
- contract_to_prompt_block()：把结构化契约编译成注入生成的文风提示块
  （替代原来只有一行的 styleTone 字符串）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.dao import projects as projects_dao
from app.dao import style_dao
from app.db import get_conn
from app.services.style_engine import extraction
from app.services.style_engine import transition_service


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _load_chapters(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, content, chapter_order FROM chapters "
            "WHERE project_id = ? ORDER BY chapter_order ASC",
            (project_id,),
        ).fetchall()
    return [
        {"title": row["title"] or "", "content": row["content"] or "", "order": row["chapter_order"]}
        for row in rows
        if (row["content"] or "").strip()
    ]


# ══════════════════════════════════════════════════════
# 深度文风分析（编排）
# ══════════════════════════════════════════════════════

async def analyze_project_style(project_id: str) -> dict[str, Any]:
    """触发一次深度文风分析：采样 → 抽取 → 落库画像 + 绑定。返回画像。"""
    chapters = _load_chapters(project_id)
    if not chapters:
        raise ValueError("项目暂无章节正文，无法进行文风分析。")

    sample_text, total_chars, sample_chars, sample_count = extraction.sample_chapters(chapters)
    metrics = extraction.compute_metrics(sample_text)
    dimensions = extraction.compute_dimensions(metrics)

    llm_used = False
    try:
        extracted = await extraction.extract_rules(sample_text, metrics)
        llm_used = True
    except Exception:  # noqa: BLE001 —— LLM 分析失败一律降级为规则统计（PRD：零 LLM 成本路径）
        extracted = extraction.build_rules_from_metrics(metrics)

    status = "completed" if llm_used else "degraded"
    profile = style_dao.create_profile(
        {
            "project_id": project_id,
            "name": f"文风画像 {_utc_now()[:10]}",
            "source_type": "import",
            "style_kind": "reference",
            "identity": {
                "impression": str(dimensions.get("tone") or "保持参考文本的整体感觉"),
                "tone": [str(dimensions.get("tone") or "")],
            },
            "mechanics": {
                "sentence_variation": "；".join(item.get("rule", "") for item in extracted["language_rules"][:3]),
                "scene_progression": "通过具体行动、对话和后果推进场景",
            },
            "boundaries": {
                "avoid": [item.get("rule", "") for item in extracted["anti_ai_rules"][:8]],
                "must_preserve": ["事实准确", "人物信息边界"],
            },
            "status": status,
            "narrative_rules": extracted["narrative_rules"],
            "character_rules": extracted["character_rules"],
            "language_rules": extracted["language_rules"],
            "rhythm_rules": extracted["rhythm_rules"],
            "anti_ai_rules": extracted["anti_ai_rules"],
            "metrics": metrics,
            "dimensions": dimensions,
            "strengths": extracted["strengths"],
            "weaknesses": extracted["weaknesses"],
            "report": extracted.get("report", ""),
            "sample_count": sample_count,
            "sample_chars": sample_chars,
            "llm_used": int(llm_used),
            "today": _utc_now(),
        }
    )
    style_dao.upsert_binding(project_id, profile["id"], _utc_now())
    return profile


def create_genre_style_profile(project_id: str, genre: str, name: str = "") -> dict[str, Any]:
    """Persist and activate a deterministic genre style profile."""
    generated = build_genre_style_profile(genre)
    profile = style_dao.create_profile(
        {
            "project_id": project_id,
            "name": name.strip()[:120] or generated["name"],
            "source_type": "genre",
            "style_kind": "genre",
            "identity": generated["identity"],
        "mechanics": {**generated["mechanics"], "genreRules": generated["genreRules"]},
            "boundaries": generated["boundaries"],
            "status": "completed",
            "today": _utc_now(),
        }
    )
    style_dao.upsert_binding(project_id, profile["id"], _utc_now())
    return profile


def get_profile(project_id: str) -> dict[str, Any] | None:
    """查询项目当前生效画像（绑定的那份；无绑定时取最新一份）。"""
    binding = style_dao.get_binding(project_id)
    if binding and binding.get("profileId"):
        profile = style_dao.get_profile(binding["profileId"])
        if profile:
            return profile
    return style_dao.get_profile_by_project(project_id)


# ══════════════════════════════════════════════════════
# 风格契约编译（表达偏好 + 画像参考 + 过渡档位）
# ══════════════════════════════════════════════════════

_PRESET_DIM_KEYS = ("avgSentenceLength", "dialogueRatio", "descriptionDensity", "paragraphLength", "tone")
_PARAGRAPH_LABELS = {"short": "短段", "medium": "中段", "long": "长段"}

_GENRE_STYLE_PROFILES: dict[str, dict[str, Any]] = {
    "mystery": {
        "identity": {"impression": "克制、压迫、重视线索回收", "tone": ["冷静", "悬疑"]},
        "mechanics": {"information_release": "通过行动和细节逐步揭示", "scene_progression": "每场推进一个问题并留下可追查后果"},
        "boundaries": {"avoid": ["无证据的全知解释", "模板化反转"], "must_preserve": ["线索可追溯", "人物信息边界"]},
    },
    "xuanhuan": {
        "identity": {"impression": "宏阔、热烈、持续升级", "tone": ["热血", "奇诡"]},
        "mechanics": {"information_release": "用行动和代价展示规则", "scene_progression": "每场产生能力、关系或资源变化"},
        "boundaries": {"avoid": ["无代价升级", "重复解释规则"], "must_preserve": ["能力边界", "升级因果"]},
    },
    "urban": {
        "identity": {"impression": "具体、利落、贴近现实压力", "tone": ["克制", "生活化"]},
        "mechanics": {"information_release": "通过选择、关系和现实后果释放信息", "scene_progression": "每场改变一个实际处境"},
        "boundaries": {"avoid": ["空泛成功学", "人物只为推动情节服务"], "must_preserve": ["现实约束", "人物自主选择"]},
    },
}


_GENRE_ALIASES = {
    "fantasy": "xuanhuan",
    "xianxia": "xuanhuan",
    "historical": "history",
    "wuxia": "history",
    "apocalypse": "unlimited_flow",
}

_GENRE_PROFILE_RULES: dict[str, dict[str, Any]] = {
    "urban": {
        "readerExpectations": "recognizable social pressure and concrete consequences",
        "conflictTypes": ["career", "relationship", "resource", "institution"],
        "pacingTendency": "steady escalation with scene-level decisions",
        "informationRelease": "reveal through choices, relationships, and consequences",
        "chapterChange": "each chapter changes a practical situation or relationship",
        "smallArc": "a local problem reaches a visible decision",
        "bigArc": "the protagonist gains or loses agency in the social system",
        "commonRisks": ["empty success", "characters serving only plot movement"],
    },
    "xuanhuan": {
        "readerExpectations": "clear progression, discovery, and consequential power growth",
        "conflictTypes": ["power", "resource", "faction", "rule"],
        "pacingTendency": "frequent progression with alternating pressure and recovery",
        "informationRelease": "show rules through action and price rather than lectures",
        "chapterChange": "ability, resource, alliance, or threat changes with a cost",
        "smallArc": "a local obstacle is solved, transformed, or escalated",
        "bigArc": "the protagonist moves toward a larger power or world conflict",
        "commonRisks": ["cost-free upgrades", "repeated rule explanations"],
    },
    "mystery": {
        "readerExpectations": "fair clues, controlled uncertainty, and traceable reveals",
        "conflictTypes": ["information", "trust", "time", "hidden motive"],
        "pacingTendency": "investigation beats alternate with danger and partial answers",
        "informationRelease": "release actionable clues while preserving a solvable question",
        "chapterChange": "a clue, suspect relation, risk, or question materially changes",
        "smallArc": "one question gains evidence or reaches a provisional answer",
        "bigArc": "the central truth becomes more constrained and more costly to expose",
        "commonRisks": ["unearned reveal", "withholding information only to fake suspense"],
    },
    "romance": {
        "readerExpectations": "visible emotional movement and meaningful relational choices",
        "conflictTypes": ["desire", "trust", "commitment", "external pressure"],
        "pacingTendency": "attraction, friction, vulnerability, and consequence alternate",
        "informationRelease": "reveal inner motives through behavior and selective honesty",
        "chapterChange": "the relationship, self-understanding, or commitment changes",
        "smallArc": "a relational barrier is crossed, reinforced, or redefined",
        "bigArc": "the pair moves toward an earned choice about staying together",
        "commonRisks": ["misunderstanding loops", "romance without reciprocal agency"],
    },
    "history": {
        "readerExpectations": "credible period constraints, consequence, and grounded detail",
        "conflictTypes": ["faction", "status", "resource", "duty"],
        "pacingTendency": "measured escalation through decisions under institutional limits",
        "informationRelease": "embed context in action, custom, and material consequence",
        "chapterChange": "status, alliance, resource, duty, or danger changes within the period rules",
        "smallArc": "a local objective is achieved at a social or material price",
        "bigArc": "the protagonist navigates a larger historical force without erasing agency",
        "commonRisks": ["modern assumptions in period characters", "encyclopedic exposition"],
    },
    "scifi": {
        "readerExpectations": "speculative ideas with understandable constraints and human stakes",
        "conflictTypes": ["technology", "ethics", "resource", "institution"],
        "pacingTendency": "concept discovery alternates with operational and human consequences",
        "informationRelease": "introduce systems only when a choice depends on them",
        "chapterChange": "knowledge, system state, resource, or ethical position changes",
        "smallArc": "a concrete problem tests one speculative premise",
        "bigArc": "the human cost of the central technology or society becomes unavoidable",
        "commonRisks": ["technology as magic", "worldbuilding detached from character choice"],
    },
    "unlimited_flow": {
        "readerExpectations": "clear scenario rules, escalating trials, and earned survival or growth",
        "conflictTypes": ["scenario", "rule", "team", "resource"],
        "pacingTendency": "rapid objective cycles with a quieter consequence beat between trials",
        "informationRelease": "reveal scenario rules through testable evidence and cost",
        "chapterChange": "objective, resource, alliance, injury, or rule knowledge changes",
        "smallArc": "the current scenario objective advances or imposes a new price",
        "bigArc": "individual trials reveal the structure and purpose of the larger system",
        "commonRisks": ["identical trial loops", "rules changing only to rescue the protagonist"],
    },
    "light_novel": {
        "readerExpectations": "quick readability, distinct character reactions, and satisfying beats",
        "conflictTypes": ["social", "comic", "goal", "misunderstanding"],
        "pacingTendency": "short readable beats with frequent character or situation turns",
        "informationRelease": "prioritize immediately usable information and reaction",
        "chapterChange": "a character relation, goal, status, or situation turns visibly",
        "smallArc": "a compact problem produces a comic, emotional, or plot payoff",
        "bigArc": "character bonds and goals accumulate toward a larger emotional payoff",
        "commonRisks": ["same reaction repeated", "tone breaks that erase consequence"],
    },
}


def build_genre_style_profile(genre: str) -> dict[str, Any]:
    """Build a reusable genre mechanism profile without an author imitation target."""
    requested = str(genre or "mystery").strip().lower()
    canonical = _GENRE_ALIASES.get(requested, requested)
    rules = _GENRE_PROFILE_RULES.get(canonical) or _GENRE_PROFILE_RULES["mystery"]
    source = _GENRE_STYLE_PROFILES.get(canonical)
    if source is None:
        source = {
            "identity": {"impression": rules["readerExpectations"], "tone": [canonical]},
            "mechanics": {"information_release": rules["informationRelease"], "scene_progression": rules["chapterChange"]},
            "boundaries": {"avoid": rules["commonRisks"], "must_preserve": [rules["smallArc"], rules["bigArc"]]},
        }
    return {
        "id": f"genre-{genre or 'mystery'}",
        "name": f"{genre or 'mystery'} 类型风格",
        "sourceType": "genre",
        "styleKind": "genre",
        "version": 1,
        "identity": dict(source["identity"]),
        "mechanics": dict(source["mechanics"]),
        "boundaries": {key: list(value) for key, value in source["boundaries"].items()},
        "genre": canonical,
        "genreRules": {key: list(value) if isinstance(value, list) else value for key, value in rules.items()},
        "status": "completed",
    }


def validate_style_switch(
    story_state_before: dict[str, Any],
    story_state_after: dict[str, Any],
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
) -> dict[str, Any]:
    """Validate a style switch without allowing it to alter narrative state."""
    narrative_keys = (
        "current_volume_goal", "current_phase_goal", "protagonist_goal", "protagonist_obstacle",
        "main_conflict", "sub_conflicts", "relationship_changes", "current_mysteries",
        "active_foreshadows", "unresolved_questions", "promised_payoffs", "last_major_state_change",
        "next_stage_directions", "characters", "relationships", "knowledge", "resources", "conflicts",
        "foreshadows", "mysteries", "next_goals",
    )
    before = {key: story_state_before.get(key) for key in narrative_keys if key in story_state_before}
    after = {key: story_state_after.get(key) for key in narrative_keys if key in story_state_after}
    preserved = before == after
    source_style = {key: source_profile.get(key) for key in ("identity", "mechanics", "boundaries", "genreRules")}
    target_style = {key: target_profile.get(key) for key in ("identity", "mechanics", "boundaries", "genreRules")}
    style_changed = source_style != target_style
    issues = []
    if not preserved:
        issues.append("story_state_changed")
    if not style_changed:
        issues.append("target_style_is_not_different")
    return {
        "valid": preserved and style_changed,
        "story_preserved": preserved,
        "style_changed": style_changed,
        "preserved_keys": [key for key in narrative_keys if before.get(key) == after.get(key) and key in before],
        "issues": issues,
        "source_genre": source_profile.get("genre"),
        "target_genre": target_profile.get("genre"),
        "api_calls": 0,
    }


def _profile_v2(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "styleKind": profile.get("styleKind") or profile.get("style_kind") or "reference",
        "version": int(profile.get("version") or 1),
        "identity": profile.get("identity") or {"impression": "保持项目现有整体感觉"},
        "mechanics": profile.get("mechanics") or {"scene_progression": "每场产生可感知变化"},
        "boundaries": profile.get("boundaries") or {"avoid": [], "must_preserve": []},
    }


def _build_preset_range(preset: dict[str, Any]) -> dict[str, list[float]]:
    """把预设均值扩成允许区间（±20%，句长下限至少 6 字）。"""
    ranges: dict[str, list[float]] = {}
    avg = preset.get("avgSentenceLength")
    if avg:
        ranges["avgSentenceLength"] = [max(round(avg * 0.8), 6), round(avg * 1.2)]
    dialog = preset.get("dialogueRatio")
    if dialog is not None:
        ranges["dialogueRatio"] = [round(max(dialog - 0.2, 0.0), 2), round(min(dialog + 0.2, 1.0), 2)]
    desc = preset.get("descriptionDensity")
    if desc is not None:
        ranges["descriptionDensity"] = [round(max(desc - 0.2, 0.0), 2), round(min(desc + 0.2, 1.0), 2)]
    return ranges


def evaluate_preset_deviation(
    content: str, style_config: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """生成后统计校验（P1-1）：正文客观指标 vs 预设允许区间（±20%），返回超区间项列表。

    对应 PRD KPI「文风预设匹配度：生成文本句长/对话占比落在预设区间 ±20%」。
    空正文 / 无预设 / 无超区间时返回空列表（调用方计入质量债或忽略）。
    """
    if not content or not content.strip():
        return []
    preset = {
        key: style_config.get(key)
        for key in _PRESET_DIM_KEYS
        if style_config and style_config.get(key) is not None
    }
    ranges = _build_preset_range(preset)
    if not ranges:
        return []
    metrics = extraction.compute_metrics(content)

    deviations: list[dict[str, Any]] = []
    label_map = {
        "avgSentenceLength": "平均句长",
        "dialogueRatio": "对话占比",
        "descriptionDensity": "描写密度",
    }
    for key, rng in ranges.items():
        value = metrics.get(key)
        if value is None:
            continue
        if rng[0] <= value <= rng[1]:
            continue
        label = label_map.get(key, key)
        if key == "avgSentenceLength":
            detail = f"平均句长 {value} 字超出预设区间 {rng[0]}–{rng[1]} 字"
        else:
            detail = f"{label} {value:.2f} 超出预设区间 {rng[0]}–{rng[1]}"
        deviations.append(
            {
                "dimension": key,
                "label": label,
                "value": value,
                "range": rng,
                "detail": detail,
            }
        )
    return deviations


def compile_contract(
    style_config: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    transition: dict[str, Any] | None,
) -> dict[str, Any]:
    """把预设 + 画像 + 过渡编译成结构化契约。"""
    plan = (transition or {}).get("plan") or {}
    if plan.get("schemaVersion") == 2:
        # Frozen source and destination are authoritative for this plan, even
        # if the author later analyzes another sample or changes project style.
        style_config = plan["targetSnapshot"]["preset"]
        source = plan["sourceSnapshot"]
        rules = source["rules"]
        profile = {"id": source["profileId"], "status": source["status"],
                   "version": source["profileVersion"], "dimensions": source["dimensions"],
                   "identity": source.get("identity") or {}, "mechanics": source.get("mechanics") or {},
                   "boundaries": source.get("boundaries") or {},
                   **{dest: rules.get(key) or [] for key, dest in {
                       "narrative_rules": "narrativeRules", "character_rules": "characterRules",
                       "language_rules": "languageRules", "rhythm_rules": "rhythmRules", "anti_ai_rules": "antiAiRules"}.items()}}
    preset = {key: style_config.get(key) for key in _PRESET_DIM_KEYS if style_config and style_config.get(key) is not None}
    if style_config and style_config.get("name"):
        preset["name"] = style_config["name"]
    preset["range"] = _build_preset_range(preset)

    contract: dict[str, Any] = {"preset": preset}
    if profile:
        profile_v2 = _profile_v2(profile)
        contract["profile"] = {
            "id": profile["id"],
            "status": profile["status"],
            **profile_v2,
            "dimensions": profile.get("dimensions") or {},
            "rules": {
                "narrative_rules": profile.get("narrativeRules") or [],
                "character_rules": profile.get("characterRules") or [],
                "language_rules": profile.get("languageRules") or [],
                "rhythm_rules": profile.get("rhythmRules") or [],
                "anti_ai_rules": profile.get("antiAiRules") or [],
            },
        }
    if transition:
        block = transition_service.build_transition_block(transition, profile, style_config)
        if block:
            contract["transition"] = block
    return contract


def build_style_contract(project_id: str) -> dict[str, Any]:
    """为项目编译当前生效的风格契约（供生成注入）。"""
    return build_generation_contract(project_id)


def build_generation_contract(project_id: str, style_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成任务专用的契约编译入口（style_config 已由调用方加载时复用）。"""
    if style_config is None:
        project = projects_dao.get_project(project_id)
        style_config = (project or {}).get("styleConfig") or {}
    profile = get_profile(project_id)
    transition = transition_service.get_transition(project_id)
    return compile_contract(style_config, profile, transition)


def contract_to_prompt_block(contract: dict[str, Any] | None) -> str:
    """把结构化契约编译成注入生成的文风提示块（替代原一行 styleTone）。"""
    if not contract:
        return ""
    lines: list[str] = []
    transition = contract.get("transition") or {}
    if transition.get("schemaVersion") == 2:
        lines.append("【执行优先级】以下预设是最终目标，不是要求立即全量采用。请按渐进过渡权重融合原稿机制与目标机制。")
        lines.append(transition.get("instruction", ""))
    preset = contract.get("preset") or {}
    preset_name = preset.get("name") or "自定义风格"

    lines.append(f"【风格偏好】预设「{preset_name}」：")
    lines.append("仅影响叙事距离、句式节奏、对话风格、描写密度、情绪表达和信息释放；不得覆盖故事事实、人物身份、世界规则、时间线或已确认关系。")
    avg = preset.get("avgSentenceLength")
    if avg:
        rng = (preset.get("range") or {}).get("avgSentenceLength")
        rng_text = f"（允许区间 {rng[0]}–{rng[1]} 字）" if rng else ""
        lines.append(f"- 平均句长：约 {avg} 字{rng_text}")
    dialog = preset.get("dialogueRatio")
    if dialog is not None:
        rng = (preset.get("range") or {}).get("dialogueRatio")
        rng_text = f"（允许区间 {rng[0] * 100:.0f}%–{rng[1] * 100:.0f}%）" if rng else ""
        lines.append(f"- 对话占比：约 {dialog * 100:.0f}%{rng_text}")
    desc = preset.get("descriptionDensity")
    if desc is not None:
        lines.append(f"- 描写密度：约 {desc * 100:.0f}%")
    para = preset.get("paragraphLength")
    if para:
        lines.append(f"- 段落长度：{_PARAGRAPH_LABELS.get(para, para)}")
    tone = preset.get("tone")
    if tone:
        lines.append(f"- 语气基调：{tone}")

    profile = contract.get("profile")
    if profile:
        identity = profile.get("identity") or {}
        mechanics = profile.get("mechanics") or {}
        boundaries = profile.get("boundaries") or {}
        lines.append("【风格身份】" + str(identity.get("impression") or "保持项目整体感觉"))
        lines.append("【写法机制】" + "；".join(f"{key}：{value}" for key, value in mechanics.items()))
        lines.append("【风格边界】避免：" + "；".join(str(item) for item in (boundaries.get("avoid") or [])))
        if boundaries.get("must_preserve"):
            lines.append("必须保留：" + "；".join(str(item) for item in boundaries["must_preserve"]))
        dims = profile.get("dimensions") or {}
        lines.append("【现状文风基线】")
        if dims:
            lines.append(
                f"- 现状维度：平均句长约 {dims.get('avgSentenceLength', '-')} 字 / "
                f"对话占比约 {dims.get('dialogueRatio', '-')} / "
                f"描写密度约 {dims.get('descriptionDensity', '-')} / "
                f"段落以{_PARAGRAPH_LABELS.get(dims.get('paragraphLength', ''), dims.get('paragraphLength') or '-')}为主"
            )
        if profile.get("status") == "degraded":
            lines.append("- 注：画像为规则统计降级生成，仅供参考。")
        rules = profile.get("rules") or {}
        if rules.get("narrative_rules"):
            lines.append("- 叙述规则：" + "；".join(r["rule"] for r in rules["narrative_rules"][:4]))
        if rules.get("character_rules"):
            lines.append("- 角色说话：" + "；".join(r["rule"] for r in rules["character_rules"][:4]))
        if rules.get("language_rules"):
            lines.append("- 用词习惯：" + "；".join(r["rule"] for r in rules["language_rules"][:4]))
        if rules.get("rhythm_rules"):
            lines.append("- 节奏：" + "；".join(r["rule"] for r in rules["rhythm_rules"][:4]))
        if rules.get("anti_ai_rules"):
            lines.append("- 避免（反 AI）：" + "；".join(r["rule"] for r in rules["anti_ai_rules"][:4]))

    transition = contract.get("transition")
    if transition:
        lines.append("【渐进过渡】")
        lines.append(transition.get("instruction", ""))

    return "\n".join(lines)
