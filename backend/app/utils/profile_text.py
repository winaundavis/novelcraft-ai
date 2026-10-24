"""角色档案文本的清洗工具（AI 生成 / AI 分析链路共用）。

产品要求：角色档案里的描述类字段只用**纯文字**，不出现 MBTI 与任何打分。
模型偶尔仍会输出 `INTJ`、"外向度：8"、"7/10"、"68%" 这类旧格式残留，
统一在这里清洗掉，避免它们落进角色资料或展示给作者。

只做"去噪"，不做"编造"：清洗后过短、或只剩标点符号的文本由调用方按无效处理
（降级/拒绝），绝不用规则引擎拼一段文字出来。
"""

from __future__ import annotations

import re

# 独立出现的 MBTI 四字母（前后不能紧邻其它英文字母，避免误伤普通英文单词）。
# 大小写不敏感：模型会输出 infj / Intj / INtJ 这类变体，同样不该漏给作者；
# `[JP]{1,2}` 兼容模型把末位重复的笔误（如 ESTJJ）。
_MBTI_TOKEN = re.compile(r"(?<![A-Za-z])[EI][NS][TF][JP]{1,2}(?![A-Za-z])", re.IGNORECASE)

# 维度名 + 分数（如「外向度：8」「自律度 7 分」）
_DIMENSION_SCORE = re.compile(
    r"(?:开放度|自律度|外向度|亲和度|敏感度|尽责性|宜人性|神经质)\s*[:：]?\s*\d+\s*分?"
)

# 残留的维度名（不带数字也算评分口吻，正文里不该出现）
_BARE_DIMENSION = re.compile(r"(?:开放度|自律度|外向度|亲和度|尽责性|宜人性)")

# 其它打分样式：7/10、68%、8 分。
# 注意「20 分钟」里的「分」是时间单位，不是分数，必须排除，否则会清掉语义。
_SCORE_STYLES = re.compile(r"\d+\s*/\s*10|\d+(?:\.\d+)?\s*%|\d+\s*分(?!钟|秒|期)")

# 「MBTI」「大五人格」「五维」等术语及其紧邻的说明片段
_JARGON = re.compile(
    r"(?:MBTI|mbti|大五人格|五维(?:人格|模型|性格|打分)?)"
    r"\s*(?:类型|人格|性格|模型|得分|评分|维度)?\s*(?:是|为|：|:)?\s*"
)

# 清洗后需要剥掉的残留标点与空白
_EDGE_TRIM = " \t\r\n，,。；;：:、-—·•()（）[]【】\"'“”‘’/|"

# 描述必须至少含一个汉字：否则「！！！！……」或 "_" * 14 这类纯符号噪声
# 会被当成有效内容展示给作者。
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def clean_profile_text(text: str, max_length: int = 200, min_length: int = 2) -> str:
    """清洗角色档案文本：去掉 MBTI / 维度 / 数字评分残留，并做长度约束。

    - 只接受字符串；其余类型一律返回空串（`str()` 出来的 repr 不是给作者看的内容）；
    - 清洗后只剩标点或空白 → 返回空串；
    - 长度不足 `min_length` → 返回空串（调用方按"无效"处理）。
    """
    if not isinstance(text, str):
        return ""

    cleaned = text.strip()
    if not cleaned:
        return ""

    cleaned = _MBTI_TOKEN.sub("", cleaned)
    cleaned = _DIMENSION_SCORE.sub("", cleaned)
    cleaned = _BARE_DIMENSION.sub("", cleaned)
    cleaned = _SCORE_STYLES.sub("", cleaned)
    cleaned = _JARGON.sub("", cleaned)

    # 归并空白与重复标点，再去掉首尾悬空的标点/空白
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"([，,。；;：:、])\1+", r"\1", cleaned)
    cleaned = cleaned.strip(_EDGE_TRIM)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(_EDGE_TRIM)

    if not _HAS_CJK.search(cleaned):
        return ""

    cleaned = cleaned[:max_length].strip(_EDGE_TRIM)
    return cleaned if len(cleaned) >= min_length else ""
