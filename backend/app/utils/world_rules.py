"""世界规则的统一选取与渲染（2026-10-22）。

此前三处注入各有一套**静默截断**：正文生成压缩取 12 条、续写提示取 12 条、
项目上下文取 8 条。规则一多，后面的规则永远不会进 prompt，作者也收不到任何提示——
等于"设了规则但没生效"。

这里统一成**按字数预算**选取：装得下就全给；装不下才截断，并把被省略的条数
写进 prompt（让模型知道还有规则存在，不许自行新增/改写世界规则）。
"""

from __future__ import annotations

from typing import Any

# 正文生成侧的单条预算（scene guidance / 续写提示共用）
DEFAULT_MAX_CHARS = 1500
DEFAULT_MAX_COUNT = 24


def _field(rule: Any, key: str) -> str:
    """兼容两种形态：项目里存的是 dict，`ContinueWritingRequest.worldRules` 是 pydantic 对象。

    只按 dict 处理会静默返回空列表——续写提示里的世界规则会整块消失。
    """
    value = rule.get(key) if isinstance(rule, dict) else getattr(rule, key, None)
    return str(value or "").strip()


def select_world_rules(
    rules: list[Any] | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_count: int = DEFAULT_MAX_COUNT,
) -> tuple[list[dict[str, str]], int]:
    """按预算选出要注入的规则，返回 (保留的规则, 被省略的条数)。

    保持作者给定顺序（题目/核心设定通常写在前面）；空规则（无名且无描述）直接跳过，
    不计入省略数。
    """
    kept: list[dict[str, str]] = []
    used = 0
    omitted = 0
    for rule in rules or []:
        if not isinstance(rule, dict) and not hasattr(rule, "name"):
            continue
        name = _field(rule, "name")
        description = _field(rule, "description")
        if not name and not description:
            continue
        cost = len(name) + len(description)
        if len(kept) >= max_count or (kept and used + cost > max_chars):
            omitted += 1
            continue
        kept.append(
            {
                "name": name or "未命名规则",
                "description": description,
                "category": _field(rule, "category"),
            }
        )
        used += cost
    return kept, omitted


def world_rule_lines(
    rules: list[Any] | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_count: int = DEFAULT_MAX_COUNT,
    with_category: bool = False,
) -> list[str]:
    """渲染成 prompt 行；被预算截断时补一条显式说明，绝不静默丢规则。"""
    kept, omitted = select_world_rules(rules, max_chars=max_chars, max_count=max_count)
    lines: list[str] = []
    for rule in kept:
        category = f"（{rule['category']}）" if with_category and rule["category"] else ""
        description = f"：{rule['description']}" if rule["description"] else ""
        lines.append(f"- {rule['name']}{category}{description}")
    if omitted:
        lines.append(
            f"- 另有 {omitted} 条世界规则因篇幅未列出；未列出的规则同样有效，"
            "不得自行新增或改写任何世界规则。"
        )
    return lines
