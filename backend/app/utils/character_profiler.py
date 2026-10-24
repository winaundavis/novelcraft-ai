"""从角色相关文本推断性别与外貌（规则引擎）。

统一用于 AI 生成角色解析、正文人物分析等场景，
避免角色资料出现"性别=未知 / 外貌空白"的占位空壳。

性格倾向不再由规则引擎打分推断（产品要求：只用一段纯文字描述，
不允许 MBTI 与五维打分），改由 AI 通读正文与大纲生成，见
`app/services/character_profile_service.py`。
"""

from __future__ import annotations

import re
from typing import Any

# ---------- 性别推断 ----------
_MALE_HINTS = [
    "他", "先生", "父亲", "爸爸", "爹爹", "哥哥", "弟弟", "儿子", "男子", "男人",
    "男孩", "少年", "叔", "伯", "爷爷", "外公", "舅舅", "男主",
]
_FEMALE_HINTS = [
    "她", "女士", "母亲", "妈妈", "娘", "姐姐", "妹妹", "女儿", "女子", "女人",
    "女孩", "少女", "阿姨", "奶奶", "外婆", "姑姑", "女主",
]
# 常见名字特征字（粗略启发式，取名字中最具性别倾向的用字）
_FEMALE_NAME_CHARS = set(
    "晚曦涵梦柔宁心月灵露冰雅欣怡蕾萱璐琪瑶珊妍婧姝娜媛婷静丽娟霞萍红梅兰芳颖"
    "雪娇婉慧敏琳燕洁茜芸薇汐玥荷莲蓉蕊茗莎菁菱晴霜露娥若诗黛婉"
)
_MALE_NAME_CHARS = set(
    "伟强磊军勇杰涛明辉建平毅超鹏亮龙辰昊宇轩皓天翔峰博睿霖阳泽锋康旭东国志文武"
    "凯斌俊诚刚德鑫荣华贵福禄海松柏山岳岩浩振瑞坤川冠邦达超恒"
)


def infer_gender(text: str, name: str = "") -> str:
    """从文本与名字推断性别，返回 男 / 女 / 未知。"""
    if name:
        name_gender = _name_gender_hint(name)
        if name_gender:
            return name_gender

    male_score = sum(text.count(word) for word in _MALE_HINTS)
    female_score = sum(text.count(word) for word in _FEMALE_HINTS)

    if male_score > female_score:
        return "男"
    if female_score > male_score:
        return "女"
    return "未知"


def _name_gender_hint(name: str) -> str | None:
    female = sum(1 for ch in name if ch in _FEMALE_NAME_CHARS)
    male = sum(1 for ch in name if ch in _MALE_NAME_CHARS)
    if female > male:
        return "女"
    if male > female:
        return "男"
    return None


# ---------- 外貌推断 ----------
_APPEARANCE_PART_PATTERN = re.compile(
    r"(身材|个子|身高|脸庞|脸|面容|皮肤|眼睛|眼眸|目光|头发|眉毛|鼻子|嘴唇|下巴|"
    r"胡子|气质|穿着|打扮|右眼|左脸|嘴角|眼角|脖子|手臂|手掌|声音|肩|腿|疤痕|纹身)"
)


def infer_appearance(text: str) -> str:
    """从文本提取最明显的一条外貌特征（精炼短语）；找不到则返回空串。"""
    if not text:
        return ""

    # 1) 优先匹配“外貌：xxx”标签后的内容
    label_match = re.search(r"(?:外貌|外观|长相)[：:\s]*([^。；\n！？]{4,40})", text)
    if label_match:
        return label_match.group(1).strip()

    # 2) 按句末标点切成短句，返回第一条含外貌部位词的短句
    for segment in re.split(r"[。；\n！？]", text):
        segment = segment.strip().strip("-*# \t•")
        if not segment:
            continue
        if _APPEARANCE_PART_PATTERN.search(segment) and 5 <= len(segment) <= 50:
            return segment

    # 3) 退化为按行匹配
    for line in text.splitlines():
        stripped = line.strip().strip("-*# \t•")
        if not stripped:
            continue
        if _APPEARANCE_PART_PATTERN.search(stripped) and 6 <= len(stripped) <= 70:
            return stripped

    return ""


# ---------- 综合推断 ----------

def profile_character(text: str, name: str = "") -> dict[str, Any]:
    """综合推断：返回 gender / appearance。

    性格倾向不在此处推断（已改为 AI 分析正文与大纲后由作者确认）。
    """
    return {
        "gender": infer_gender(text, name),
        "appearance": infer_appearance(text),
    }
