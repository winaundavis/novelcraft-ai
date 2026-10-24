"""AI-powered character & relationship analysis for an existing novel's chapters."""

from __future__ import annotations

import json
import re
from typing import Any

from app.db import get_conn
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.utils.character_profiler import profile_character
from app.utils.profile_text import clean_profile_text

# 一键分析里每个角色的性格描述上限（比单角色分析更短，避免列表臃肿）
CHARACTER_PERSONALITY_MAX_CHARS = 120

# ---------- 采样参数 ----------
# 全文低于此字符数时直接使用全文（避免过度抽样）
MAX_FULL_TEXT_CHARS = 40000
# 章节较多时每章抽取的头/尾字符数
CHAPTER_HEAD_CHARS = 1200
CHAPTER_TAIL_CHARS = 400
# 章节较多时最多抽样的章数（超过则取前段 + 尾段）
MAX_SAMPLED_CHAPTERS = 20
HEAD_SAMPLE_COUNT = 12
TAIL_SAMPLE_COUNT = 6

ROLES = {
    "protagonist", "antagonist", "supporting", "mentor",
    "love_interest", "sidekick", "cameo",
}
ROLE_ALIASES = {
    "主角": "protagonist", "男主": "protagonist", "女主": "protagonist",
    "反派": "antagonist", "配角": "supporting", "导师": "mentor",
    "感情线": "love_interest", "恋爱对象": "love_interest", "搭档": "sidekick",
    "客串": "cameo", "重要配角": "supporting",
}

RELATION_TYPES = {
    "family", "friend", "love", "enemy",
    "interest", "secret", "rival", "master_student",
}
RELATION_ALIASES = {
    "家人": "family", "亲人": "family", "父子": "family", "母子": "family",
    "兄弟": "family", "姐妹": "family", "父女": "family", "母女": "family",
    "朋友": "friend", "好友": "friend", "挚友": "friend", "青梅竹马": "friend",
    "爱慕": "love", "恋人": "love", "情侣": "love", "夫妻": "love", "暗恋": "love",
    "敌对": "enemy", "敌人": "enemy", "仇人": "enemy", "死对头": "enemy", "宿敌": "enemy",
    "共同利益": "interest", "同盟": "interest", "合作": "interest", "盟友": "interest",
    "秘密": "secret", "秘密关联": "secret", "竞争者": "rival", "竞争": "rival",
    "师徒": "master_student", "师生": "master_student", "师徒关系": "master_student",
    "上下级": "interest", "主仆": "interest",
}

CHARACTER_ANALYSIS_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _load_chapters(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, content, chapter_order FROM chapters "
            "WHERE project_id = ? ORDER BY chapter_order ASC",
            (project_id,),
        ).fetchall()
    return [
        {"title": row["title"], "content": row["content"], "order": row["chapter_order"]}
        for row in rows
    ]


def _build_full_text(chapters: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"第{item['order']}章 {item['title']}\n{item['content']}" for item in chapters
    )


def _sample_text(chapters: list[dict[str, Any]]) -> tuple[str, int]:
    """返回 (分析文本, 全文总字符数)。全文不长时直接返回全文，否则抽样。"""
    full_text = _build_full_text(chapters)
    total = len(full_text)
    if total <= MAX_FULL_TEXT_CHARS or len(chapters) <= HEAD_SAMPLE_COUNT:
        return full_text, total

    if len(chapters) <= MAX_SAMPLED_CHAPTERS:
        selected = list(chapters)
    else:
        head = chapters[:HEAD_SAMPLE_COUNT]
        tail = chapters[-TAIL_SAMPLE_COUNT:]
        tail_orders = {item["order"] for item in head}
        selected = head + [item for item in tail if item["order"] not in tail_orders]
        selected.sort(key=lambda item: item["order"])

    parts: list[str] = []
    for item in selected:
        content = item["content"]
        head = content[:CHAPTER_HEAD_CHARS]
        tail = content[-CHAPTER_TAIL_CHARS:] if len(content) > CHAPTER_HEAD_CHARS + CHAPTER_TAIL_CHARS else ""
        body = head + (("..." + tail) if tail else "")
        parts.append(f"第{item['order']}章 {item['title']}\n{body}")

    return "\n\n".join(parts), total


# 公开别名：供其它服务（如角色性格分析）复用同一套章节读取/抽样实现，
# 全局只有一个实现，避免两份采样逻辑逐渐跑偏。
load_chapters = _load_chapters
sample_text = _sample_text


def _extract_json(reply: str) -> dict[str, Any]:
    cleaned = reply.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMResponseError("返回内容中未找到 JSON 对象。")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"JSON 解析失败：{exc}") from exc


def _normalize_role(value: Any) -> str:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in ROLES:
            return key
        if key in ROLE_ALIASES:
            return ROLE_ALIASES[key]
        for alias, mapped in ROLE_ALIASES.items():
            if alias in key:
                return mapped
    return "supporting"


def _normalize_relation_type(value: Any) -> str:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in RELATION_TYPES:
            return key
        if key in RELATION_ALIASES:
            return RELATION_ALIASES[key]
        for alias, mapped in RELATION_ALIASES.items():
            if alias in key:
                return mapped
    return "interest"


def _parse_reply(reply: str, total_chars: int, chapter_count: int) -> dict[str, Any]:
    data = _extract_json(reply)

    characters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data.get("characters") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name or name in seen or len(name) > 8:
            continue
        seen.add(name)
        summary = str(raw.get("summary", "")).strip()[:120]
        # 性别与外貌仍由规则引擎从简介与名字推断；性格倾向只取模型给的文字描述
        profile = profile_character(f"{summary} {name}", name)
        characters.append(
            {
                "name": name,
                "role": _normalize_role(raw.get("role")),
                "summary": summary,
                "gender": profile["gender"],
                "appearance": profile["appearance"],
                "personality": {
                    "description": clean_profile_text(
                        str(raw.get("personality") or ""),
                        max_length=CHARACTER_PERSONALITY_MAX_CHARS,
                    )
                },
            }
        )
        if len(characters) >= 15:
            break

    relations: list[dict[str, Any]] = []
    relation_pairs: set[tuple[str, str, str]] = set()
    for raw in data.get("relations") or []:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source", "")).strip()
        target = str(raw.get("target", "")).strip()
        if not source or not target or source == target:
            continue
        rel_type = _normalize_relation_type(raw.get("type"))
        key = (source, target, rel_type)
        if key in relation_pairs:
            continue
        relation_pairs.add(key)
        try:
            since_chapter = int(raw.get("sinceChapter") or 1)
        except (TypeError, ValueError):
            since_chapter = 1
        relations.append(
            {
                "source": source,
                "target": target,
                "type": rel_type,
                "description": str(raw.get("description", "")).strip()[:160],
                "sinceChapter": max(since_chapter, 1),
            }
        )
        if len(relations) >= 20:
            break

    return {
        "characters": characters,
        "relations": relations,
        "summary": (
            f"已分析 {chapter_count} 章 / 约 {total_chars} 字，"
            f"识别出 {len(characters)} 位主要人物、{len(relations)} 组人物关系。"
        ),
    }


def _fallback_characters(text: str, total_chars: int, chapter_count: int) -> dict[str, Any]:
    """LLM 不可用时的本地词频兜底：仅能识别高频人名，不包含关系。"""
    try:
        import jieba.posseg as pseg

        counter: dict[str, int] = {}
        stopwords = {
            "我们", "他们", "你们", "这里", "那里", "如果", "但是", "因为", "所以", "然后",
            "已经", "这个", "那个", "没有", "不是", "自己", "有人", "可以", "需要", "当前",
            "本章", "章节", "项目", "内容", "故事", "时候", "之后", "之前", "什么", "怎么",
            "一个", "一下", "现在", "知道", "感觉", "时候", "就是", "还是",
        }
        for word, flag in pseg.cut(text):
            if flag in ("nr", "nrt") and 2 <= len(word) <= 4 and word not in stopwords:
                counter[word] = counter.get(word, 0) + 1
    except ImportError:  # pragma: no cover
        counter = {}
        for word in re.findall(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])", text):
            counter[word] = counter.get(word, 0) + 1

    characters: list[dict[str, Any]] = []
    for name, count in sorted(counter.items(), key=lambda item: item[1], reverse=True)[:15]:
        if count < 3:
            continue
        profile = profile_character(name, name)
        characters.append(
            {
                "name": name,
                "role": "supporting",
                "summary": f"正文中出现 {count} 次。",
                "gender": profile["gender"],
                "appearance": profile["appearance"],
                # 本地词频兜底不编造性格：留空由作者自己写或稍后让 AI 分析
                "personality": {"description": ""},
            }
        )
    return {
        "characters": characters,
        "relations": [],
        "summary": (
            f"已分析 {chapter_count} 章 / 约 {total_chars} 字（本地词频模式），"
            f"识别出 {len(characters)} 位高频人名；人物关系需要 AI 服务可用后重新分析。"
        ),
    }


async def analyze_project_characters(project_id: str) -> dict[str, Any]:
    """分析一个项目所有章节正文，返回 { characters, relations, summary }。"""
    chapters = _load_chapters(project_id)
    if not chapters:
        return {
            "characters": [],
            "relations": [],
            "summary": "项目还没有可分析的章节正文。请先创作或导入章节内容。",
        }

    sampled, total_chars = _sample_text(chapters)
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": CHARACTER_ANALYSIS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"以下是小说《{project_id}》的正文（{len(chapters)} 章，"
                        f"全文约 {total_chars} 字；内容可能为抽样节选）。请分析人物与人物关系：\n\n{sampled}"
                    ),
                },
            ]
        )
        return _parse_reply(reply, total_chars, len(chapters))
    except (LLMConfigurationError, LLMResponseError):
        return _fallback_characters(sampled, total_chars, len(chapters))
