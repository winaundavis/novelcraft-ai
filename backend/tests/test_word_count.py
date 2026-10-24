"""字数口径一致性（2026-10-22）。

同一段正文此前会算出不同字数：
- 手动保存 / Copilot 接受 / 正文生成 / 重写用 `replace(" ","").replace("\\n","")`；
- 导入用 `re.sub(r"\\s+", "")`；
- 前端显示用 `content.replace(/\\s/g, '')`。

差异来源：`\\r`（Windows 换行，每段多算 1 字）、`\\t`、全角空格 U+3000
（前端不算、旧后端算）。这里锁住「所有入口同口径」。
"""
from __future__ import annotations

from app.services import import_service, novel_generation_service, rewrite_service
from app.utils.word_count import count_words

CRLF_TEXT = "第一段文字。\r\n第二段文字。\r\n\r\n第三段文字。\r\n"
FULLWIDTH_TEXT = "第一段\u3000文字。\n\n第二段\u3000\u3000文字。"
TABBED_TEXT = "第一段文字。\n\t第二段文字。\n"


def test_counts_ignore_all_whitespace():
    assert count_words(CRLF_TEXT) == 18  # 第一段文字。×3（“第一段文字。”每段 6 字）
    assert count_words(FULLWIDTH_TEXT) == 12  # 第一段文字。×2
    assert count_words(TABBED_TEXT) == 12  # 第一段文字。×2
    assert count_words("") == 0
    assert count_words(None) == 0


def test_crlf_and_fullwidth_no_longer_inflate_counts():
    """旧口径会给 \\r 与全角空格各算一个字。"""
    legacy = len(CRLF_TEXT.replace(" ", "").replace("\n", ""))
    assert legacy > count_words(CRLF_TEXT)  # 旧口径偏大

    legacy_fullwidth = len(FULLWIDTH_TEXT.replace(" ", "").replace("\n", ""))
    assert legacy_fullwidth > count_words(FULLWIDTH_TEXT)


def test_all_backend_entry_points_agree():
    """生成 / 重写 / 导入 / 手动保存走同一个函数。"""
    for text in (CRLF_TEXT, FULLWIDTH_TEXT, TABBED_TEXT):
        assert novel_generation_service._count_words(text) == count_words(text)
        assert rewrite_service._count_words(text) == count_words(text)
