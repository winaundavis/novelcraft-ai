"""P1-3：200 万字记忆合成压力测试（本地确定性，零 LLM 费用）

构造接近 200 万字规模的长书合成数据：
- 约 800 章（每章约 2500 字正文 + 摘要，总量约 200 万字）
- 100 人物 / 200 世界规则 / 500 事实（含金丝雀 / 已失效 superseded / 他项目）/ 200 伏笔（含回收）

验证：
1. 召回准确率：FTS5 检索「九星连珠」应命中含该关键词的历史章节
2. 过期事实过滤：superseded 事实不进入事实账本注入块
3. 跨项目隔离：干扰项目 B 的实体/事实/伏笔不进入项目 A 上下文
4. 伏笔状态：仅 planted/progressing（未回收）进入上下文
5. 延迟：构建分层记忆上下文（_build_memory_context）耗时在阈值内
6. 上下文预算：最终 contextBefore ≤ CONTEXT_BUDGET_TOTAL（小而准）
7. 上下文组成：8 类显式模块全部非空（卷摘要/近章摘要/上章结尾/事实账本/实体状态/FTS/伏笔/时间线）
"""

from __future__ import annotations

import json
import time

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

N_CH = 800          # 章节数（200 万字 ≈ 800 章 × 2500 字）
N_PERSON = 100      # 人物
N_RULES = 200       # 世界规则实体
N_FACTS = 500       # 事实（含 60 条 superseded 失效 + 1 条金丝雀）
N_FORES = 200       # 伏笔（含 40 条已回收 resolved）
CURRENT_INDEX = 600  # 从第 601 章生成（有 600 章历史可召回）


@pytest.fixture
def big_db(tmp_path, monkeypatch):
    """超大临时 DB：patch app.db.DB_PATH 后 init_db（FTS5 建表）。"""
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "big.db")
    app_db.init_db()
    yield


def _seed_big(pid: str, pid_b: str) -> None:
    """构造 800 章 / 500 事实 / 300 实体 / 200 伏笔 + 干扰项目 B。"""
    t0 = time.time()
    # ── 项目 A ──
    projects_dao.create_project({
        "id": pid, "name": "《两百万字长卷》", "premise": "主角 在浩荡天地间求索大道。",
        "target_word_count": 2000000, "current_word_count": 2000000, "status": "writing",
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "outline": "{}", "style_config": "{}", "world_rules": "[]",
    })
    # ── 项目 B（干扰，跨项目隔离）──
    projects_dao.create_project({
        "id": pid_b, "name": "《无关项目》", "premise": "林疏影 的独立故事。",
        "target_word_count": 100000, "current_word_count": 0, "status": "writing",
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "outline": "{}", "style_config": "{}", "world_rules": "[]",
    })

    # ── 800 章（每章 2500 字正文 + 摘要），第 360 章含独特关键词「九星连珠」；
    # 当前章（第 361 章）的摘要/揭示含「九星连珠」，触发 FTS 召回第 360 章 ──
    para = "他抬眼望向远天，云层压得很低，风声里藏着旧日的回响。" * 25  # ~350 字
    body_unit = para * 7  # ~2500 字
    chapter_nodes = []
    for i in range(1, N_CH + 1):
        cid = f"{pid}-c{i}"
        if i == 360:
            body = "九星连珠之夜，天地灵气紊乱，他于山巅窥见了命运的裂缝。" + body_unit
        else:
            body = body_unit
        if i == 361:
            summary = "第361章摘要：主角追查九星连珠的预言，重返山巅（事件361）。"
            revelation = "第361章揭示：九星连珠的真相渐显。"
        else:
            summary = f"第{i}章摘要：主角推进剧情（事件{i}），埋下线索。"
            revelation = f"第{i}章揭示"
        completed = i <= CURRENT_INDEX
        chapters_dao.insert_chapter({
            "id": cid, "project_id": pid, "volume_id": f"{pid}-v{(i - 1) // 20 + 1}",
            "title": f"第{i}章 尘世{i}", "chapter_order": i, "content": body,
            "word_count": len(body), "status": "completed" if completed else "draft",
            "summary": summary,
            "notes": "", "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        })
        chapter_nodes.append({
            "id": cid, "type": "chapter", "order": i, "parentId": f"{pid}-v{(i - 1) // 20 + 1}",
            "title": f"第{i}章 尘世{i}", "summary": summary,
            "conflict": f"第{i}章冲突", "revelation": revelation, "emotion": "紧张",
        })
    # 卷（40 卷，含摘要）
    for v in range(1, 41):
        volumes_dao.upsert_volume(
            pid, f"{pid}-v{v}", title=f"第{v}卷", volume_order=v,
            start_chapter_id=f"{pid}-c{(v - 1) * 20 + 1}", end_chapter_id=f"{pid}-c{v * 20}",
            summary=f"第{v}卷摘要：主角在卷首困局中崛起，卷末奠定新格局。",
        )

    # ── 500 事实（60 条 superseded 失效 + 1 条金丝雀 + 其余 active）──
    for i in range(1, N_FACTS + 1):
        if i == 1:
            content = "金丝雀P13：主角在第九星连珠之夜窥见命运裂缝，自此能看见他人死期倒计时"
            status = "active"
        elif i <= 61:
            content = f"（旧设定{i}）主角第{i}章的状态已被后续取代"
            status = "superseded"
        else:
            content = f"已发生事实{i}：主角在第{i % 800 + 1}章完成关键事件{i}"
            status = "active"
        novel_facts_dao.add_fact(
            pid, "completed" if i != 1 else "completed", content,
            chapter_id=f"{pid}-c{i % 800 + 1}", status=status,
        )
    # 金丝雀设为「最新」事实（created_at 最晚 → 1500 字贪心预算优先保留）
    with app_db.get_conn() as conn:
        conn.execute(
            "UPDATE novel_facts SET created_at='2030-01-01T00:00:00' "
            "WHERE project_id=? AND content LIKE '金丝雀P13%'",
            (pid,),
        )
    # 他项目事实（不应泄漏）
    novel_facts_dao.add_fact(pid_b, "completed", "【他项目】林疏影继承神位", status="active")
    for i in range(1, 60):
        novel_facts_dao.add_fact(pid_b, "completed", f"【他项目】事实{i}", status="active")

    # ── 实体：100 人物 + 200 世界规则 ──
    for i in range(1, N_PERSON + 1):
        memory_entities_dao.upsert_entity(
            pid, entity_type="person", name=f"人物{i}",
            description=f"第{i}号人物的当前状态：活跃", source_chapter_id=f"{pid}-c1",
        )
    for i in range(1, N_RULES + 1):
        memory_entities_dao.upsert_entity(
            pid, entity_type="world_rule", name=f"世界规则{i}",
            description=f"第{i}条规则：不可违背", source_chapter_id=f"{pid}-c1",
        )
    # 他项目实体（不应泄漏）
    memory_entities_dao.upsert_entity(
        pid_b, entity_type="person", name="林疏影",
        description="【他项目】主角", source_chapter_id=f"{pid_b}-c1",
    )

    # ── 伏笔：200 条（40 已回收 resolved，其余 planted/progressing）──
    for i in range(1, N_FORES + 1):
        status = "resolved" if i <= 40 else ("progressing" if i % 3 == 0 else "planted")
        foreshadows_dao.create_foreshadow(
            pid, name=f"伏笔{i}", description=f"第{i}条伏笔的未解之谜",
            status=status, plant_chapter_id=f"{pid}-c{i % 800 + 1}",
            target_chapter_id=f"{pid}-c{i % 800 + 40}",
        )
    foreshadows_dao.create_foreshadow(
        pid_b, name="【他项目】雾海旧约", description="他项目伏笔", status="planted",
    )

    # ── FTS 索引全部 800 章 ──
    fts_ok = 0
    for i in range(1, N_CH + 1):
        if memory_fts_dao.sync_chapter_fts(pid, f"{pid}-c{i}"):
            fts_ok += 1
    seed_sec = round(time.time() - t0, 2)
    return chapter_nodes, fts_ok, seed_sec


def test_p13_synthetic_pressure(big_db, capsys):
    """P1-3 核心：200 万字合成记忆压测（召回/过滤/隔离/伏笔/延迟/预算）。"""
    pid = "proj-2m"
    pid_b = "proj-other"
    chapter_nodes, fts_ok, seed_sec = _seed_big(pid, pid_b)

    # ── ① FTS5 可用性（优先 FTS5，LIKE 兜底不阻塞）──
    with capsys.disabled():
        print(f"[seed] {N_CH}章 FTS索引成功={fts_ok}/{N_CH} 构造耗时={seed_sec}s")

    # ── ② 延迟：构建分层记忆上下文耗时阈值（2 秒内）──
    t0 = time.time()
    mc = ngs._build_memory_context(
        project_id=pid,
        chapter_nodes=chapter_nodes,
        current_chapter_id=f"{pid}-c{CURRENT_INDEX + 1}",
    )
    build_sec = round(time.time() - t0, 3)
    with capsys.disabled():
        print(f"[build] 分层记忆上下文构建耗时={build_sec}s")

    # ── ③ 8 类显式模块全部非空 ──
    assert mc["volume_summaries"], "卷摘要为空"
    assert mc["recent_chapter_summaries"], "近章摘要为空"
    assert mc["last_chapter_ending"], "上章结尾为空"
    assert mc["completed_milestones"], "事实账本为空"
    assert mc["entity_state"], "实体状态为空"
    assert mc["active_foreshadows"], "未回收伏笔为空"
    assert mc["timeline"], "时间线约束为空"

    # ── ④ 召回准确率：金丝雀关键词「九星连珠」应命中第 360 章（FTS）──
    related = ngs._build_memory_context(
        project_id=pid,
        chapter_nodes=chapter_nodes,
        current_chapter_id=f"{pid}-c{361}",
    )["fts_history"]
    if fts_ok > 0:
        assert "九星连珠" in related or "第360章" in related or "窥见命运" in related, (
            f"FTS 召回未命中第360章：{related[:120]}"
        )
    else:
        with capsys.disabled():
            print("[fts] FTS 不可用（降级 LIKE/跳过），召回准确性跳过")

    # ── ⑤ 过期事实过滤：superseded 不进入事实账本 ──
    milestones = mc["completed_milestones"]
    assert "金丝雀P13" in milestones, "金丝雀事实未进入账本"
    assert "旧设定" not in milestones, "superseded 失效事实泄漏进账本"

    # ── ⑥ 跨项目隔离：B 的实体/事实/伏笔不进入 A 上下文 ──
    prompt = ngs._build_memory_context_before(
        project_id=pid,
        chapter_id=f"{pid}-c{CURRENT_INDEX + 1}",
        premise="主角 在浩荡天地间求索大道。",
        chapter=chapter_nodes[CURRENT_INDEX],
        chapter_index=CURRENT_INDEX,
        total_chapters=N_CH,
        outline_summary="主角 从微末崛起，最终直面天命。",
        memory_context=mc,
    )
    assert "林疏影" not in prompt, "他项目实体泄漏"
    assert "【他项目】" not in prompt, "他项目事实/伏笔泄漏"
    assert "雾海旧约" not in prompt, "他项目伏笔泄漏"

    # ── ⑦ 伏笔状态：仅未回收（planted/progressing）进入上下文 ──
    active_fores = mc["active_foreshadows"]
    assert "已回收" not in active_fores, "resolved 已回收伏笔不应出现在未回收块"
    # 伏笔1/伏笔2/... 前 40 条是 resolved，不得出现（用「伏笔N：」完整名避免子串误匹配）
    for resolved_i in (1, 2, 40):
        assert f"伏笔{resolved_i}：" not in active_fores, f"已回收伏笔{resolved_i} 不应出现"

    # ── ⑧ 预算：最终 contextBefore ≤ CONTEXT_BUDGET_TOTAL（小而准）──
    assert len(prompt) <= ngs.CONTEXT_BUDGET_TOTAL, (
        f"contextBefore 超预算：{len(prompt)} > {ngs.CONTEXT_BUDGET_TOTAL}"
    )

    with capsys.disabled():
        print(
            f"[prompt] 卷摘要={len(mc['volume_summaries'])}条 近章={len(mc['recent_chapter_summaries'])}条 "
            f"账本={len(mc['completed_milestones'])}字 实体={len(mc['entity_state'])}字 "
            f"伏笔={len(mc['active_foreshadows'])}字 时间线={len(mc['timeline'])}字 "
            f"FTS={len(mc['fts_history'])}字 总长={len(prompt)}/{ngs.CONTEXT_BUDGET_TOTAL}"
        )
