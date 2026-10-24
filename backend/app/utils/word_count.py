"""统一的字数口径（2026-10-22）。

同一段正文此前会算出不同字数，因为四个入口各写各的：

| 入口 | 旧公式 | 问题 |
|---|---|---|
| 手动保存 / Copilot 接受 / 正文生成 / 重写 | `replace(" ", "").replace("\n", "")` | **不去** `\\r`、`\\t`、全角空格 → Windows 换行每段多算 1 字 |
| 导入 | `re.sub(r"\\s+", "", ...)` | 与上面不同 |
| 前端显示（`/\\s/g`） | 去掉所有空白 | 与第一类不同 → 编辑器显示与库里对不上 |

统一为「去掉所有空白字符（含 `\\r\\n`、制表符、全角空格）后的字符数」，
与前端 `content.replace(/\\s/g, '').length` 完全等价（JS 的 `\\s` 同样包含 U+3000）。
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def count_words(text: str | None) -> int:
    """统计正文字数：去掉所有空白字符后的字符数（中文标点计入）。"""
    return len(_WHITESPACE.sub("", text or ""))
