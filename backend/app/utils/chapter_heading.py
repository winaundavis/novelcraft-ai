"""章节标题行的统一格式化。

背景（2026-10-18）：大纲生成的章节标题从「第N章」升级为「第N章 章节名」。
标题变成**自带编号**之后，原先那些自己拼「第{order}章《{title}》」或
「第{order}章 {title}」的地方就会重复（变成「第3章《第3章 雨夜来客》」）。
这里集中提供两个函数，避免每处各写一份前缀判断。

- `chapter_name_only(title)`：只要章节名，去掉可能已有的「第N章」前缀。
- `chapter_heading(order, title)`：输出稳定的「第N章 章节名」标题行，且不会重复前缀。
"""
from __future__ import annotations

import re

# 匹配「第3章」「第 3 章」「第3章·」「第3章：」「第3章 」等编号前缀。
#
# 只认阿拉伯数字：老数据的「第一章」在既有实现里是**没有编号**的标题，
# `chapter_heading(1, "第一章")` 一直是「第1章 第一章」（全量导出、摘要块都依赖它）。
# 把中文数字也当成编号会静默改掉这些既有输出，所以这里保持原样。
_CHAPTER_NUMBER_PREFIX = re.compile(r"^第\s*[0-9]+\s*章\s*[·:：\-—]?\s*")


def chapter_name_only(title: str | None) -> str:
    """去掉「第N章」前缀，只返回章节名；没有名字时返回空串（调用方自行兜底）。"""
    return _CHAPTER_NUMBER_PREFIX.sub("", str(title or "").strip()).strip()


def has_chapter_number(title: str | None) -> bool:
    """标题是否已经自带「第N章」编号。"""
    return bool(_CHAPTER_NUMBER_PREFIX.match(str(title or "").strip()))


def chapter_heading(order: int | None, title: str | None) -> str:
    """组合「第N章 章节名」，且**不会**因为 title 已自带编号而重复。

    关键：标题自带编号时**以标题为准**，不再用 order 重组。
    因为 `chapters.chapter_order` 是**卷内序号**（每卷从 1 重新开始），
    而章节标题里的编号是**全书全局序号**。用卷内序号重组会把第二卷首章
    「第16章 雨夜来客」写成「第1章 雨夜来客」——多卷书直接串号。

    纯函数：相同输入永远相同输出，便于缓存与测试。
    """
    text = str(title or "").strip()
    if has_chapter_number(text):
        return text
    try:
        number = f"第{int(order)}章" if order else ""
    except (TypeError, ValueError):
        number = ""
    if number and text:
        return f"{number} {text}"
    return number or text


_MARKDOWN_HEADING_PREFIX = re.compile(r"^\s*#{1,6}\s*")


def _comparable_heading(line: str) -> str:
    """把一行标题归一化到可比较的形态：去掉 markdown 井号、书名号与所有空白。

    书名号要**全部**删掉（不是只 strip 首尾）：`第3章《雨夜来客》` 只 strip 尾部会留下
    一个不配对的「《」，和 `第3章 雨夜来客` 就比不相等了。
    """
    text = _MARKDOWN_HEADING_PREFIX.sub("", str(line or "").strip())
    return re.sub(r"[《》\s]+", "", text)


def ensure_chapter_heading(content: str, title: str | None) -> str:
    """确保正文字首有一行「第N章 章节名」（**幂等**）。

    只有**「标题自带编号」且「编号后面还有名字」**时才插入：
    - 老数据的标题就是纯「第N章」/「第一章」→ 原样返回，绝不凭空加行改变既有正文与字数；
    - 没有编号的裸标题（夹具/异常输入）→ 同样不动，避免把任意标签当成章节名。
    """
    text = (content or "").lstrip()
    if not text or not has_chapter_number(title) or not chapter_name_only(title):
        return content
    heading = chapter_heading(None, title)
    if not heading:
        return content
    if _comparable_heading(text.splitlines()[0]) == _comparable_heading(heading):
        return content
    return f"{heading}\n\n{text}"


def content_starts_with_heading(content: str, order: int | None, title: str | None) -> bool:
    """正文是否已以该章标题行开头（导出时用它避免重复一行标题）。"""
    text = (content or "").lstrip()
    heading = chapter_heading(order, title)
    if not text or not heading:
        return False
    return _comparable_heading(text.splitlines()[0]) == _comparable_heading(heading)
