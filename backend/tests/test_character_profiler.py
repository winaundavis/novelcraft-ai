"""character_profiler 单元测试

覆盖：
- infer_gender：称谓/人称/名字特征推断
- infer_appearance：标签提取 / 短句提取 / 空兜底
- profile_character：综合

注意：性格倾向已改为「作者手写 / AI 通读正文与大纲生成」的纯文字描述，
本模块不再推断性格五维与 MBTI，相关用例已随代码一并移除。
"""

from app.utils.character_profiler import (
    infer_appearance,
    infer_gender,
    profile_character,
)


# ── infer_gender ──────────────────────────────────────────

def test_gender_from_pronoun():
    assert infer_gender("他是个冷静的杀手。") == "男"
    assert infer_gender("她是一位温柔的医生。") == "女"


def test_gender_from_title():
    assert infer_gender("先生走了进来。") == "男"
    assert infer_gender("这位女士很神秘。") == "女"


def test_gender_from_name_characters():
    assert infer_gender("", name="林晚") == "女"
    assert infer_gender("", name="张伟") == "男"


def test_gender_neutral_name_without_text_stays_unknown():
    # 中性名且无文本线索时不强行猜测
    assert infer_gender("", name="陈默") == "未知"


def test_gender_unknown_when_no_clue():
    assert infer_gender("一个人站在门口，没有说话。") == "未知"


# ── infer_appearance ──────────────────────────────────────

def test_appearance_from_label():
    assert infer_appearance("外貌：左脸有一道疤，目光锐利。") == "左脸有一道疤，目光锐利"


def test_appearance_from_short_segment():
    assert infer_appearance(
        "林晚是女主角，性格内向。她身材高挑，一头黑色长发，眼神清冷。"
    ) == "她身材高挑，一头黑色长发，眼神清冷"


def test_appearance_empty_when_no_clue():
    assert infer_appearance("这里没有任何外貌描述。") == ""


# ── profile_character 综合 ─────────────────────────────────

def test_profile_character_combines_fields():
    profile = profile_character(
        "陈默是个冷酷的男性反派，行事严谨克制，心机深沉，多疑而偏执。他左脸有一道疤。",
        name="陈默",
    )
    assert profile["gender"] == "男"
    assert "疤" in profile["appearance"]


def test_profile_character_no_longer_returns_personality_or_mbti():
    """性格不再由规则引擎猜测，接口只保留性别与外貌。"""
    profile = profile_character("陈默很冷酷。", name="陈默")
    assert set(profile) == {"gender", "appearance"}
