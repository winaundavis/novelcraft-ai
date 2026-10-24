"""章节标题行格式化：多卷书的全局编号不能被卷内序号覆盖。

背景：`chapters.chapter_order` 是**卷内序号**（每卷从 1 重新开始），
而章节标题里的「第N章」是**全书全局序号**。标题自带编号时必须以标题为准。
"""
from __future__ import annotations

from app.utils.chapter_heading import (
    chapter_heading,
    chapter_name_only,
    content_starts_with_heading,
    ensure_chapter_heading,
    has_chapter_number,
)


def test_keeps_global_number_from_title_instead_of_recomposing_with_volume_local_order():
    # 第二卷首章：chapter_order = 1（卷内序号），标题里是全书第 16 章。
    assert chapter_heading(1, "第16章 雨夜来客") == "第16章 雨夜来客"


def test_composes_number_for_legacy_title_without_number():
    assert chapter_heading(3, "雨夜来客") == "第3章 雨夜来客"


def test_title_that_is_only_a_number_is_not_duplicated():
    # 存量项目的章节标题就是「第N章」，不能被拼成「第3章 第3章」。
    assert chapter_heading(3, "第3章") == "第3章"


def test_missing_name_or_order_falls_back():
    assert chapter_heading(7, "") == "第7章"
    assert chapter_heading(None, "雨夜来客") == "雨夜来客"
    assert chapter_heading(0, "") == ""
    assert chapter_heading(None, None) == ""


def test_name_only_strips_prefix_variants():
    assert chapter_name_only("第 3 章·雨夜来客") == "雨夜来客"
    assert chapter_name_only("第3章：雨夜来客") == "雨夜来客"
    assert chapter_name_only("第3章-雨夜来客") == "雨夜来客"
    assert chapter_name_only("雨夜来客") == "雨夜来客"
    assert chapter_name_only("第3章") == ""
    assert chapter_name_only(None) == ""


def test_has_chapter_number():
    assert has_chapter_number("第3章 雨夜来客") is True
    assert has_chapter_number("第 12 章 灰烬") is True
    assert has_chapter_number("雨夜来客") is False
    assert has_chapter_number("") is False


# ── 正文标题行：幂等插入 ──────────────────────────────────────

def test_ensure_heading_prepends_once_and_is_idempotent():
    once = ensure_chapter_heading("雨下了一夜。", "第3章 雨夜来客")
    assert once == "第3章 雨夜来客\n\n雨下了一夜。"
    # 再跑一次不得变出第二行标题
    assert ensure_chapter_heading(once, "第3章 雨夜来客") == once


def test_ensure_heading_accepts_markdown_and_bracket_variants():
    # 模型自己写了 markdown 标题行 / 书名号，也算"已有标题"
    assert ensure_chapter_heading("## 第3章 雨夜来客\n\n正文", "第3章 雨夜来客") == "## 第3章 雨夜来客\n\n正文"
    assert ensure_chapter_heading("第3章《雨夜来客》\n\n正文", "第3章 雨夜来客") == "第3章《雨夜来客》\n\n正文"


def test_ensure_heading_skips_titles_without_a_real_name():
    # 存量项目标题就是「第N章」：绝不能凭空插一行标题改变既有正文
    assert ensure_chapter_heading("正文", "第3章") == "正文"
    assert ensure_chapter_heading("正文", "") == "正文"
    assert ensure_chapter_heading("", "第3章 雨夜来客") == ""
    # 中文数字的纯编号同样是"没有名字"（老数据常见写法）——
    # 只认阿拉伯数字会让「第一章」被当成有名字，从而给老项目插标题行、改变字数。
    assert ensure_chapter_heading("正文", "第一章") == "正文"
    assert ensure_chapter_heading("正文", "第十一章") == "正文"
    assert ensure_chapter_heading("正文", "第三章：") == "正文"
    # 没有编号的裸标题也不动：不能把任意标签当成"章节名"去插行
    assert ensure_chapter_heading("正文", "并发新章") == "正文"


def test_content_starts_with_heading_detects_duplicate_for_export():
    assert content_starts_with_heading("第3章 雨夜来客\n\n正文", 3, "第3章 雨夜来客") is True
    assert content_starts_with_heading("## 第3章 雨夜来客\n正文", 3, "第3章 雨夜来客") is True
    assert content_starts_with_heading("正文", 3, "第3章 雨夜来客") is False
    # 老数据（标题只有编号）导出时仍要照常补标题行
    assert content_starts_with_heading("正文", 3, "第3章") is False
