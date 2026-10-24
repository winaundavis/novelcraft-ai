"""Zero-API chapter QA and LLM review budget classification."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

MIN_HARD_WORDS = 200
HIGH_RISK_TERMS = (
    "死亡",
    "牺牲",
    "真相揭示",
    "世界规则",
    "时间跳跃",
    "决战",
    "大战",
    "身份反转",
)
_DIALOGUE_RE = re.compile(r"[“\"]([^”\"]+)[”\"]")
_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{3,}")
_STOP_WORDS = {
    "于是",
    "但是",
    "他们",
    "自己",
    "这个",
    "那个",
    "已经",
    "没有",
    "一个",
    "什么",
    "然后",
}


def _issue(code: str, message: str, *, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?\n]+", text) if len(item.strip()) >= 4]


def _paragraph_shape(text: str) -> tuple[int, ...]:
    values = [len(item.strip()) for item in re.split(r"\n\s*\n", text) if item.strip()]
    return tuple(min(value // 80, 8) for value in values[:12])


def _chapter_order(chapter: dict[str, Any]) -> int:
    return int(chapter.get("order") or chapter.get("chapter_order") or 0)


def _mode(chapter: dict[str, Any]) -> str:
    return str(chapter.get("chapter_mode") or chapter.get("mode") or "").strip()


def _check_context_rules(
    text: str,
    rules: list[dict[str, Any]] | None,
    *,
    code: str,
    label: str,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for rule in rules or []:
        fact = str(rule.get("fact") or rule.get("event") or "").strip()
        contradictions = rule.get("contradictions") or rule.get("forbidden_terms") or []
        for contradiction in contradictions if isinstance(contradictions, list) else []:
            token = str(contradiction).strip()
            if token and token in text:
                issues.append(_issue(code, f"{label}：{fact or token} ↔ {token}", severity="blocking"))
    return issues


def classify_risk(chapter: dict[str, Any], contract: dict[str, Any], content: str) -> dict[str, Any]:
    mode = str(contract.get("chapter_mode") or contract.get("pacing") or "")
    evidence = " ".join([str(chapter.get("title") or ""), str(contract.get("state_change") or ""), *[str(item) for item in contract.get("key_events") or []], content[:1200]])
    high_reasons = [term for term in HIGH_RISK_TERMS if term in evidence]
    if mode == "Climax" or high_reasons:
        return {"level": "high", "llm_qa_budget": 2, "reasons": high_reasons or ["climax_mode"]}
    if mode in {"Investigation", "Conflict", "Reveal", "Action", "Character", "Emotion"} or contract.get("must_payoff"):
        return {"level": "medium", "llm_qa_budget": 1, "reasons": [f"mode:{mode or 'unspecified'}"]}
    return {"level": "low", "llm_qa_budget": 0, "reasons": [f"mode:{mode or 'transition'}"]}


def evaluate_chapter(
    *,
    content: str,
    chapter: dict[str, Any],
    contract: dict[str, Any] | None,
    previous_chapters: list[dict[str, Any]],
    foreshadows: list[dict[str, Any]] | None = None,
    active_foreshadow_names: list[str] | None = None,
    character_name_rules: list[dict[str, Any]] | None = None,
    known_fact_rules: list[dict[str, Any]] | None = None,
    timeline_rules: list[dict[str, Any]] | None = None,
    expected_state_version: int | None = None,
    actual_state_version: int | None = None,
    allow_order_gap: bool = False,
) -> dict[str, Any]:
    blocking: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    text = str(content or "").strip()
    if not text:
        blocking.append(_issue("empty_chapter", "章节正文为空。", severity="blocking"))
    elif len(text) < MIN_HARD_WORDS:
        blocking.append(_issue("chapter_too_short", "章节正文低于最低安全长度。", severity="blocking"))
    if not contract:
        blocking.append(_issue("missing_contract", "缺少 Chapter Contract。", severity="blocking"))
        contract = {}
    orders = sorted({_chapter_order(item) for item in previous_chapters if _chapter_order(item)})
    current_order = _chapter_order(chapter)
    if not allow_order_gap and current_order > 1 and (not orders or max(orders) != current_order - 1):
        blocking.append(_issue("chapter_order_gap", "章节顺序不连续。", severity="blocking"))
    if any(str(item.get("title") or "").strip() == str(chapter.get("title") or "").strip() for item in previous_chapters):
        warnings.append(_issue("duplicate_title", "章节标题与前文重复。"))
    if (
        expected_state_version is not None
        and actual_state_version is not None
        and int(actual_state_version) != int(expected_state_version)
    ):
        blocking.append(_issue("state_version_gap", "Story State 版本不连续。", severity="blocking"))
    blocking.extend(
        _check_context_rules(
            text, known_fact_rules, code="known_fact_conflict", label="正文与已知事实冲突"
        )
    )
    blocking.extend(
        _check_context_rules(
            text, timeline_rules, code="timeline_conflict", label="正文与时间线冲突"
        )
    )
    for rule in character_name_rules or []:
        canonical = str(rule.get("canonical") or "").strip()
        invalid_variants = rule.get("invalid_variants") or []
        found = [
            str(item).strip()
            for item in invalid_variants if str(item).strip() and str(item).strip() in text
        ]
        if found:
            blocking.append(
                _issue(
                    "character_name_inconsistency",
                    f"人物名不一致：{canonical or '未命名人物'} ↔ {'、'.join(found[:5])}",
                    severity="blocking",
                )
            )
    sentences = _sentences(text)
    counts = Counter(sentences)
    if counts and max(counts.values()) >= 3:
        warnings.append(_issue("sentence_repetition", "检测到句子高频重复，仅标记为可能问题。"))
    if any(len(sentence) >= 20 and sentence in str(item.get("content") or "") for sentence in sentences for item in previous_chapters[-5:]):
        warnings.append(_issue("repeated_text", "检测到与近期章节重复的文本片段。"))
    current_shape = _paragraph_shape(text)
    if current_shape and any(current_shape == _paragraph_shape(str(item.get("content") or "")) for item in previous_chapters[-3:]):
        warnings.append(_issue("recent_structure_similarity", "段落结构与近期章节高度相似。"))
    paragraphs = [len(item.strip()) for item in re.split(r"\n\s*\n", text) if item.strip()]
    if paragraphs and (max(paragraphs) > 1200 or sum(value < 15 for value in paragraphs) > len(paragraphs) * 0.7):
        warnings.append(_issue("paragraph_length_anomaly", "段落长度分布异常。"))
    dialogue_chars = sum(len(match) for match in _DIALOGUE_RE.findall(text))
    dialogue_ratio = dialogue_chars / max(len(text), 1)
    if len(text) >= MIN_HARD_WORDS and (dialogue_ratio > 0.75 or dialogue_ratio < 0.005):
        warnings.append(_issue("dialogue_ratio_anomaly", "对话比例异常，仅标记为可能问题。"))
    tokens = [token for token in _WORD_RE.findall(text) if token not in _STOP_WORDS]
    token_counts = Counter(tokens)
    if tokens and any(count >= 12 and count / len(tokens) >= 0.08 for count in token_counts.values()):
        warnings.append(_issue("high_frequency_term", "检测到高频词异常，仅标记为可能问题。"))
    current_mode = str(contract.get("chapter_mode") or contract.get("pacing") or "")
    recent_modes = [_mode(item) for item in previous_chapters[-5:]]
    if current_mode and len(recent_modes) >= 3 and all(mode == current_mode for mode in recent_modes[-3:]):
        warnings.append(_issue("chapter_mode_repetition", "近期章节模式连续重复，仅标记为可能问题。"))
    required_characters = [str(item).strip() for item in contract.get("characters") or [] if str(item).strip()]
    missing_characters = [name for name in required_characters if name not in text]
    if missing_characters:
        warnings.append(
            _issue("character_evidence_missing", f"合同人物未在正文出现：{'、'.join(missing_characters[:5])}")
        )
    for payoff in contract.get("must_payoff") or []:
        token = str(payoff).strip()
        if token and token not in text:
            warnings.append(_issue("payoff_evidence_missing", f"必须兑现项缺少直接证据：{token}"))
    resolved = {str(item.get("name") or "") for item in (foreshadows or []) if item.get("status") in {"resolved", "completed", "closed"}}
    if resolved & set(active_foreshadow_names or []):
        warnings.append(_issue("resolved_foreshadow_active", "已关闭伏笔仍被标记为 active。"))
    risk = classify_risk(chapter, contract, text)
    return {"decision": "pause" if blocking else "continue", "blocking": blocking, "warnings": warnings, "layer1": {"status": "failed" if blocking else "passed"}, "layer2": {"status": "possible_issue" if warnings else "passed", "is_literary_judgment": False}, "risk": risk}
