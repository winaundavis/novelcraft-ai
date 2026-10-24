"""阶段C：复杂上下文 20 次测试（含金丝雀事实）

核心命题（任务单原文）：
「重点不是把提示词无限增大，而是验证大量历史数据经过召回后能形成
小而准确的上下文包。」

每次测试构造一个记忆丰富的项目（10 卷 / 60 章 / 人物与规则实体 /
已完成+失效事实 / 未回收+已回收伏笔 / FTS 索引 / 干扰项目），在
`_build_memory_context` + `_build_memory_context_before` 全链路中验证：

1. 每次生成的实际 contextBefore 必须包含非空的 9 类信息：
   人物当前状态 / 世界规则 / 事实账本 / 最近章节摘要 / 卷摘要 /
   未回收伏笔 / 时间线约束 / 本章大纲（文风约束在 body styleTone，
   由真实 LLM 冒烟覆盖）
2. 每次放入唯一「金丝雀事实」：实际提示词包含正确事实
3. 不包含已失效（superseded）事实（过期事实过滤）
4. 不包含他项目事实（跨项目隔离）
5. 无剧透：最近章节摘要只含当前章之前
6. 总长 ≤ CONTEXT_BUDGET_TOTAL（小而准，非无限增大）
7. 记录各模块字符数
"""

from __future__ import annotations

import json

import pytest

from app import db as app_db
from app.dao import chapters as chapters_dao
from app.dao import foreshadows as foreshadows_dao
from app.dao import memory_entities as memory_entities_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import novel_facts as novel_facts_dao
from app.dao import projects as projects_dao
from app.dao import volumes as volumes_dao
from app.services import novel_generation_service as ngs

# 20 个唯一金丝雀事实：每个主角 + 每个唯一事件
CANARY_CASES = [
    ("云破霄", "金丝雀F01：在第三十六卷的雪崩事件中，云破霄失去左臂，改用玄铁义肢驱动阵法"),
    ("风清辞", "金丝雀F02：风清辞在第十卷获得镜海龙珠，自此能短暂预读他人的念头"),
    ("沈青梧", "金丝雀F03：沈青梧在第十八卷服下断情丹，永久失去对双亲的记忆"),
    ("顾砚舟", "金丝雀F04：顾砚舟在第二十四卷得知自己是被封印的北境太子"),
    ("苏曼华", "金丝雀F05：苏曼华在第三十卷与剑灵契约，此后每夜须以心头血供养"),
    ("萧长歌", "金丝雀F06：萧长歌在第一卷末尾坠入寒潭，被古琴残魂救起并寄宿识海"),
    ("江晚晴", "金丝雀F07：江晚晴在第十二卷中了情蛊，逢月圆之夜会失去自制"),
    ("陆昭宁", "金丝雀F08：陆昭宁在第二十卷铸成断岳刀，代价是折损十年寿元"),
    ("裴无咎", "金丝雀F09：裴无咎在第三十三卷亲手葬下兄长，自此封剑不复拔"),
    ("叶星澜", "金丝雀F10：叶星澜在第八卷成为星官，掌一域气运却不可干预凡尘"),
    ("白芷柔", "金丝雀F11：白芷柔在第十五卷失去味觉，此后借医书辨毒为生"),
    ("霍惊鸿", "金丝雀F12：霍惊鸿在第二十七卷被逐出师门，带走了镇派《惊鸿剑录》"),
    ("秦无涯", "金丝雀F13：秦无涯在第五卷吞服九转还魂丹，从此惧光畏寒"),
    ("林初雪", "金丝雀F14：林初雪在第二十一卷替挚友挡下毒箭，左肩留下蛇形疤痕"),
    ("燕孤鸿", "金丝雀F15：燕孤鸿在第三卷破解天机盘，代价是忘记自己的真名"),
    ("柳含烟", "金丝雀F16：柳含烟在第十九卷嫁给傀儡城主，实为卧底夺其机关图"),
    ("韩霜降", "金丝雀F17：韩霜降在第二十九卷炼成寒冰诀，容貌自此凝驻如雪"),
    ("慕青黛", "金丝雀F18：慕青黛在第十四卷与妖王结契，能号令群妖却折寿半百"),
    ("齐景明", "金丝雀F19：齐景明在第三十二卷揭穿国师阴谋，反被诬陷通敌入狱"),
    ("温若仙", "金丝雀F20：温若仙在第六卷继承药王谷，誓以医道济世不染刀兵"),
]

_CURRENT_INDEX = 39  # 第 40 章（index 39，共 60 章）


@pytest.fixture
def ctx_db(tmp_path, monkeypatch):
    """临时 DB：patch app.db.DB_PATH 后 init_db（各 DAO 绑定 get_conn 读该全局）。"""
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "ctx.db")
    app_db.init_db()
    yield


def _seed_memory_project(pid: str, protagonist: str, canary: str) -> list[dict]:
    """构造记忆丰富的项目（10 卷 / 60 章 / 实体 / 事实 / 伏笔 / FTS）。

    返回全局有序 chapter_nodes（含 9 类信息所需字段）。
    """
    # 项目 A（记忆丰富）
    projects_dao.create_project({
        "id": pid,
        "name": f"《{protagonist}》长卷",
        "premise": f"{protagonist} 踏遍九州，在恩怨与天命间求索大道。",
        "target_word_count": 300000,
        "current_word_count": 0,
        "status": "writing",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "outline": "{}",
        "style_config": "{}",
        "world_rules": json.dumps([
            {"category": "力量体系", "name": "灵气守恒", "description": "灵气不灭，只能转化"},
            {"category": "禁忌", "name": "禁入北境", "description": "北境三百年无人踏足"},
        ], ensure_ascii=False),
    })
    # 10 卷（含卷摘要）
    for v in range(1, 11):
        volumes_dao.upsert_volume(
            pid, f"{pid}-vol{v}",
            title=f"第{v}卷·{protagonist}的前路",
            volume_order=v,
            start_chapter_id=f"{pid}-c{(v - 1) * 6 + 1}",
            end_chapter_id=f"{pid}-c{v * 6}",
            summary=(f"第{v}卷卷摘要：{protagonist} 在卷首的困局中崛起，"
                     f"历经三次关键转折，于卷末奠定新的格局与代价。"
                     f"（本卷共 6 章，冲突聚焦第{v}卷主线。）"),
        )
    # 60 章（前 40 章已完成含正文+摘要，后 20 章为草稿）
    chapters: list[dict] = []
    for i in range(1, 61):
        cid = f"{pid}-c{i}"
        completed = i <= 40
        content = (
            f"第{i}章正文：{protagonist} 在{['东境', '西岭', '南疆', '北荒', '中州'][i % 5]}"
            f"的行旅中推进剧情，遭遇新的对手并留下转折。"
        ) if completed else ""
        chapters_dao.insert_chapter({
            "id": cid, "project_id": pid, "volume_id": f"{pid}-vol{(i - 1) // 6 + 1}",
            "title": f"第{i}章", "chapter_order": i, "content": content,
            "word_count": len(content), "status": "completed" if completed else "draft",
            "summary": f"第{i}章摘要：{protagonist} 推进剧情（事件{i}），埋下后续线索。",
            "notes": "", "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        })
        chapters.append({
            "id": cid, "type": "chapter", "order": i, "parentId": f"{pid}-vol{(i - 1) // 6 + 1}",
            "title": f"第{i}章",
            "summary": f"第{i}章摘要：{protagonist} 推进剧情（事件{i}），埋下后续线索。",
            "conflict": f"第{i}章的正面冲突：阻力升级。",
            "revelation": f"第{i}章揭示：新的线索浮出水面。",
            "emotion": "紧张",
        })
    # ── 事实账本 ──
    # 金丝雀（completed，必须注入）
    novel_facts_dao.add_fact(pid, "completed", canary, chapter_id=f"{pid}-c36")
    # 金丝雀（state_changed，进入时间线块）
    novel_facts_dao.add_fact(
        pid, "state_changed", f"{protagonist} 左臂已替换为玄铁义肢",
        chapter_id=f"{pid}-c36", status="active",
    )
    # 普通已完成事实
    novel_facts_dao.add_fact(pid, "completed", f"{protagonist} 在第9章击败山魈王", chapter_id=f"{pid}-c9")
    # 失效事实（superseded，必须被过滤）
    novel_facts_dao.add_fact(
        pid, "completed", f"（旧设定）{protagonist} 左臂完好（已随卷36雪崩事件失效）",
        chapter_id=f"{pid}-c35", status="superseded",
    )
    # ── 实体（人物当前状态 + 世界规则）──
    memory_entities_dao.upsert_entity(
        pid, entity_type="person", name=protagonist,
        description=f"主角；当前状态：左臂为玄铁义肢（{canary[:8]}）",
        source_chapter_id=f"{pid}-c36",
    )
    memory_entities_dao.upsert_entity(
        pid, entity_type="person", name=f"{protagonist}的宿敌",
        description="当前状态：在暗处虎视眈眈，尚未正面交锋", source_chapter_id=f"{pid}-c20",
    )
    memory_entities_dao.upsert_entity(
        pid, entity_type="world_rule", name="灵气守恒定律",
        description="世界规则：灵气不灭，只能转化，逆转需付代价", source_chapter_id=f"{pid}-c1",
    )
    # ── 伏笔（未回收 + 已回收）──
    foreshadows_dao.create_foreshadow(
        pid, name=f"金丝雀伏笔：{protagonist}的义肢来历",
        description="义肢内封存一段未解记忆，与北境旧约相关", status="planted",
        plant_chapter_id=f"{pid}-c36", target_chapter_id=f"{pid}-c55",
    )
    foreshadows_dao.create_foreshadow(
        pid, name="已回收伏笔：旧日信物", description="已于第25章回收", status="resolved",
    )
    # ── FTS 索引（前 40 章）──
    for i in range(1, 41):
        memory_fts_dao.sync_chapter_fts(pid, f"{pid}-c{i}")
    # ── 干扰项目 B（跨项目隔离验证）──
    pid_b = f"{pid}-B"
    projects_dao.create_project({
        "id": pid_b, "name": "《林疏影》无关项目",
        "premise": "林疏影 的独立故事。", "target_word_count": 100000,
        "current_word_count": 0, "status": "writing",
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "outline": "{}", "style_config": "{}", "world_rules": "[]",
    })
    novel_facts_dao.add_fact(pid_b, "completed", "【他项目】林疏影在第七卷继承神位", chapter_id=f"{pid_b}-c42")
    memory_entities_dao.upsert_entity(
        pid_b, entity_type="person", name="林疏影", description="【他项目】主角", source_chapter_id=f"{pid_b}-c1",
    )
    foreshadows_dao.create_foreshadow(pid_b, name="【他项目】雾海旧约", description="他项目伏笔")
    return chapters


def _build_prompt(pid: str, chapters: list[dict]) -> tuple[dict, str]:
    """全链路：_build_memory_context → _build_memory_context_before。"""
    mc = ngs._build_memory_context(
        project_id=pid,
        chapter_nodes=chapters,
        current_chapter_id=f"{pid}-c{_CURRENT_INDEX + 1}",
    )
    prompt = ngs._build_memory_context_before(
        project_id=pid,
        chapter_id=f"{pid}-c{_CURRENT_INDEX + 1}",
        premise="主角 踏遍九州，在恩怨与天命间求索大道。",
        chapter=chapters[_CURRENT_INDEX],
        chapter_index=_CURRENT_INDEX,
        total_chapters=len(chapters),
        outline_summary="主角 从微末崛起，历经卷卷磨难，最终直面天命。",
        memory_context=mc,
    )
    return mc, prompt


@pytest.mark.parametrize("protagonist,canary", CANARY_CASES, ids=[c[0] for c in CANARY_CASES])
def test_complex_context_canary(ctx_db, protagonist, canary, capsys):
    """阶段C 核心：20 次金丝雀验证（每次唯一事实 + 9 类信息非空 + 隔离/过滤/预算）。"""
    pid = f"proj-{protagonist[:2]}"
    chapters = _seed_memory_project(pid, protagonist, canary)
    mc, prompt = _build_prompt(pid, chapters)

    # ── ① 9 类信息必须非空 ──
    assert mc["volume_summaries"], "卷摘要为空"
    assert mc["recent_chapter_summaries"], "最近章节摘要为空"
    assert mc["last_chapter_ending"], "上一章结尾为空"
    assert mc["completed_milestones"], "事实账本为空"
    assert mc["entity_state"], "实体状态（人物/世界规则）为空"
    assert mc["active_foreshadows"], "未回收伏笔为空"
    assert mc["timeline"], "时间线约束为空"
    assert not ngs._memory_context_is_empty(mc)

    # 模块标题在最终提示词中（9 类信息可见）
    for marker in (
        "【实体状态】",       # 人物当前状态 / 世界规则
        "【时间线约束】",     # 时间线约束
        "【未回收伏笔】",     # 未回收伏笔
        "【已完成事实】",     # 事实账本
        "已完结卷摘要",       # 卷摘要
        "最近章节摘要",       # 最近章节摘要
        "上一章结尾",         # 上一章结尾
        "章节摘要：",         # 本章大纲（header）
    ):
        assert marker in prompt, f"缺少模块标记：{marker}"

    # ── ② 金丝雀事实正确注入 ──
    assert canary in prompt, "金丝雀事实未进入实际提示词"
    assert protagonist in prompt, "主角名未进入提示词"
    assert "玄铁义肢" in prompt, "实体当前状态（金丝雀线索）未进入提示词"

    # ── ③ 不含已失效事实（superseded 过滤）──
    assert "旧设定" not in prompt, "已失效事实泄漏进提示词"
    assert "左臂完好" not in prompt, "被取代的旧状态泄漏进提示词"

    # ── ④ 跨项目隔离（不含他项目事实/实体/伏笔）──
    assert "林疏影" not in prompt, "他项目实体泄漏"
    assert "【他项目】" not in prompt, "他项目事实/伏笔泄漏"

    # ── ⑤ 无剧透（最近章节摘要只含当前章之前 c35-c39）──
    for i in range(_CURRENT_INDEX + 2, 61):  # c41..c60
        assert f"第{i}章摘要" not in prompt, f"剧透：第{i}章摘要进入提示词"

    # ── ⑥ 小而准：总预算约束 ──
    assert len(prompt) <= ngs.CONTEXT_BUDGET_TOTAL, (
        f"contextBefore 超预算：{len(prompt)} > {ngs.CONTEXT_BUDGET_TOTAL}"
    )

    # ── ⑦ 记录各模块字符数（阶段C 要求）──
    with capsys.disabled():
        print(
            f"[{protagonist}] 卷摘要={len(mc['volume_summaries'])} "
            f"近章摘要={len(mc['recent_chapter_summaries'])} "
            f"上章结尾={len(mc['last_chapter_ending'])} "
            f"事实账本={len(mc['completed_milestones'])} "
            f"实体状态={len(mc['entity_state'])} "
            f"伏笔={len(mc['active_foreshadows'])} "
            f"时间线={len(mc['timeline'])} "
            f"FTS={len(mc['fts_history'])} "
            f"总长={len(prompt)}/{ngs.CONTEXT_BUDGET_TOTAL}"
        )


def test_complex_context_budget_stress(ctx_db, capsys):
    """预算压力：卷摘要/近章摘要堆满后，低优先级块正确让位、高优先级块保留。"""
    pid = "proj-stress"
    chapters = _seed_memory_project(pid, "云破霄", CANARY_CASES[0][1])
    # 把卷摘要与近章摘要撑满预算，验证 drop 机制不破坏金丝雀与实体
    for v in range(6, 11):
        volumes_dao.upsert_volume(
            pid, f"{pid}-vol{v}", title=f"第{v}卷", volume_order=v,
            summary="长卷摘要：" + "尘世浮沉，恩怨交织。" * 60,
        )
    mc, prompt = _build_prompt(pid, chapters)
    assert len(prompt) <= ngs.CONTEXT_BUDGET_TOTAL
    assert CANARY_CASES[0][1] in prompt, "预算压力下金丝雀事实被丢弃（不应发生）"
    assert "玄铁义肢" in prompt, "预算压力下实体状态被丢弃（不应发生）"
    assert "林疏影" not in prompt
    with capsys.disabled():
        print(f"[budget-stress] 总长={len(prompt)}/{ngs.CONTEXT_BUDGET_TOTAL}")
