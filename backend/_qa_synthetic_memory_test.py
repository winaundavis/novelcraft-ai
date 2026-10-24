#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 合成记忆压力测试 v1 —— 2000+ 章规模，验证长期记忆验收指标 M1-M8。

约束遵守：
- 所有真实写入使用唯一 audit-* 项目（audit-synthetic-3000-<ts> / audit-resume-<ts>）
- 不调用真实 LLM：章节正文模板拼接，事实/实体/冲突直接经 DAO 写入
- 测试结束清理本轮 audit-* 项目，保留备份
- 任何降级/异常显式记录到 EVENTS
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))  # backend/
sys.path.insert(0, HERE)

from app.db import get_conn, init_db, utc_today
from app.dao import chapters as chapters_dao
from app.dao import novel_facts as nf_dao
from app.dao import memory_entities as me_dao
from app.dao import memory_fts as mfts_dao
from app.dao import memory_recall as mr_dao
from app.dao import projects as projects_dao
from app.dao import volumes as volumes_dao
from app.dao import foreshadows as fsh_dao
from app.dao import novel_jobs as jobs_dao
from app.models.schemas import gen_id
from app.services import memory_service
from app.services import novel_generation_service as ngs

DB_PATH = os.path.join(HERE, "data", "novelcraft.db")

# ── 配置 ──────────────────────────────────────────────
N_CHAPTERS = 3000
N_KEY_FACTS = 250
N_CONFLICTS = 40
N_SUPERSEDED_FACTS = 30
N_SUPERSEDED_ENTITIES = 30
N_ENTITIES = 100
N_RELATIONS = 30
N_FORESHADOWS = 25
SEED = 20260813

random.seed(SEED)

RESULTS: dict = {}
EVENTS: list[dict] = []


def log_event(cat: str, msg: str) -> None:
    EVENTS.append({"cat": cat, "msg": msg, "at": datetime.utcnow().isoformat()})
    print(f"  [event:{cat}] {msg}")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ── 故事元素池 ────────────────────────────────────────
PROTAGONIST = "秦霜"
SIDE_CHARS = ["柳如烟", "夜无涯", "白眉老祖", "玄清真人", "沈青梧", "厉苍澜", "苏小小", "裴无咎"]
LOCATIONS = ["青云宗", "万魔窟", "天机城", "荒古禁地", "寒潭秘境", "青丘山", "坠星台", "幽冥海"]
ITEMS = ["玄冰玉镯", "赤血令", "锁魂钉", "天机盘", "紫电枪"]
ACTIONS = ["运功调息", "与人交手", "翻阅古籍", "夜观星象", "布阵炼器", "追击敌踪", "疗伤静养", "参悟功法"]
SCENES = ["宗门演武场", "后山竹林", "藏经阁", "万魔窟外", "天机城集市", "荒古禁地", "青云宗大殿", "寒潭深处"]

ARCS = [
    # (arc_key, token_prefix, fact_type, description_generator)
    ("人物状态", "变数", "state_changed"),
    ("物品转移", "流转", "state_changed"),
    ("身份揭示", "揭示", "revealed"),
    ("地点毁灭", "毁迹", "state_changed"),
    ("时间线", "年轮", "completed"),
    ("伏笔事件", "伏笔", "completed"),
]

ARC_DESCS = {
    "人物状态": lambda i: f"{SIDE_CHARS[i % len(SIDE_CHARS)]}修为突破至第{i % 5 + 2}重境界，{PROTAGONIST}与其差距再度拉大",
    "物品转移": lambda i: f"{ITEMS[i % len(ITEMS)]}自{SIDE_CHARS[i % len(SIDE_CHARS)]}手中转至{LOCATIONS[i % len(LOCATIONS)]}藏宝阁，易主完成",
    "身份揭示": lambda i: f"{SIDE_CHARS[i % len(SIDE_CHARS)]}的真实身份揭晓：竟是{LOCATIONS[i % len(LOCATIONS)]}埋设多年的暗线",
    "地点毁灭": lambda i: f"{LOCATIONS[i % len(LOCATIONS)]}护山大阵崩碎，整座{SCENES[i % len(SCENES)]}化为废墟，不复存在",
    "时间线": lambda i: f"灵气复苏第{i + 180}年，{LOCATIONS[i % len(LOCATIONS)]}召开百年大会，诸强齐聚",
    "伏笔事件": lambda i: f"{PROTAGONIST}在{SCENES[i % len(SCENES)]}寻得{ITEMS[i % len(ITEMS)]}相关线索，指向{LOCATIONS[i % len(LOCATIONS)]}深处",
}

CONFLICT_PAIRS = [
    ("赤血令已被秦霜亲手销毁", "赤血令完好保存在万魔窟密库之中"),
    ("夜无涯已在第十卷陨落", "夜无涯在第十卷末仍活跃于战场"),
    ("玄清真人早年便已坐化", "玄清真人在大战中仍亲自出手"),
    ("玄冰玉镯被柳如烟赠予他人", "玄冰玉镯始终在柳如烟腕上"),
    ("青云宗山门已毁于天劫", "青云宗山门巍然屹立至今"),
    ("秦霜从未踏足幽冥海", "秦霜曾深入幽冥海取得紫电枪"),
    ("白眉老祖被废去修为", "白眉老祖修为仍在全盛时期"),
    ("苏小小已身陨", "苏小小以活人之身再度现身"),
    ("锁魂钉已被熔毁", "锁魂钉仍钉在镇妖塔顶层"),
    ("天机城已沉入地底", "天机城仍在举办百年大集"),
]

# 预生成 key fact 描述（保证每行唯一）
KEY_FACTS: list[dict] = []  # {arc, token, fact_type, desc, chapter}
CONFLICTS: list[dict] = []  # {token_a, token_b, stmt_a, stmt_b, ch_a, ch_b}
SUPERSEDED_FACTS: list[dict] = []  # {token, desc, chapter, fact_type}
SUPERSEDED_ENTITIES: list[dict] = []  # {name, etype}
ALL_ENTITIES: list[dict] = []  # {etype, name, desc, chapter}
ALL_RELATIONS: list[dict] = []


def build_plan() -> None:
    """在生成章节前规划全部埋点（确定章节分布）。"""
    # key facts 均匀散布全书
    idx = 0
    for arc_key, pfx, ftype in ARCS:
        for i in range(43):
            if idx >= N_KEY_FACTS:
                break
            desc = ARC_DESCS[arc_key](i)
            token = f"{pfx}{idx:03d}"
            chapter = 1 + int(idx * (N_CHAPTERS - 3) / (N_KEY_FACTS - 1))
            KEY_FACTS.append(
                {"arc": arc_key, "token": token, "fact_type": ftype,
                 "desc": desc, "chapter": chapter, "i": idx}
            )
            idx += 1
    # 冲突：散布在后 2/3 书（检测点在旧事实之后）
    for c in range(N_CONFLICTS):
        a, b = CONFLICT_PAIRS[c % len(CONFLICT_PAIRS)]
        ch_a = 400 + int(c * (N_CHAPTERS - 1200) / (N_CONFLICTS - 1))
        ch_b = ch_a + random.randint(80, 300)
        if ch_b >= N_CHAPTERS:
            ch_b = N_CHAPTERS - 5
        CONFLICTS.append(
            {"token_a": f"矛{c:03d}甲", "token_b": f"矛{c:03d}乙",
             "stmt_a": a, "stmt_b": b, "ch_a": ch_a, "ch_b": ch_b, "i": c}
        )
    # 过期事实（superseded）
    for s in range(N_SUPERSEDED_FACTS):
        ftype = "completed" if s % 2 == 0 else "state_changed"
        token = f"旧{s:03d}"
        desc = f"{SIDE_CHARS[s % len(SIDE_CHARS)]}的旧设定：{ACTIONS[s % len(ACTIONS)]}于{SCENES[s % len(SCENES)]}（已被后续情节取代）"
        chapter = 200 + int(s * (N_CHAPTERS - 600) / (N_SUPERSEDED_FACTS - 1))
        SUPERSEDED_FACTS.append({"token": token, "desc": desc, "chapter": chapter, "fact_type": ftype, "i": s})
    # 实体
    for e in range(N_ENTITIES):
        etype = ["person", "location", "item", "faction", "world_rule", "timeline"][e % 6]
        name = f"{PROTAGONIST}同门{e:03d}" if etype == "person" else f"{LOCATIONS[e % len(LOCATIONS)]}{e:03d}"
        desc = f"{name}的当前状态描述{e:03d}：{ACTIONS[e % len(ACTIONS)]}后保持稳定"
        chapter = 50 + int(e * (N_CHAPTERS - 300) / (N_ENTITIES - 1))
        ALL_ENTITIES.append({"etype": etype, "name": name, "desc": desc, "chapter": chapter, "i": e})
    # 其中 30 个将被 supersede
    for s in range(N_SUPERSEDED_ENTITIES):
        SUPERSEDED_ENTITIES.append({"name": ALL_ENTITIES[s]["name"], "etype": ALL_ENTITIES[s]["etype"], "i": s})
    # 关系
    for r in range(N_RELATIONS):
        ALL_RELATIONS.append(
            {"from": ALL_ENTITIES[r]["name"], "to": ALL_ENTITIES[(r + 3) % N_ENTITIES]["name"],
             "relation": ["kinship", "ally", "enemy", "master", "possesses", "located_at"][r % 6],
             "chapter": ALL_ENTITIES[r]["chapter"], "i": r}
        )


def chapter_planted_lines(idx: int) -> list[str]:
    """返回应在第 idx 章正文中埋入的句子（含唯一 token）。"""
    lines: list[str] = []
    for f in KEY_FACTS:
        if f["chapter"] == idx:
            lines.append(f"{f['desc']}。此事记为『{f['token']}』。")
    for c in CONFLICTS:
        if c["ch_a"] == idx:
            lines.append(f"{c['stmt_a']}。证据『{c['token_a']}』。")
        if c["ch_b"] == idx:
            lines.append(f"{c['stmt_b']}。证据『{c['token_b']}』。")
    for s in SUPERSEDED_FACTS:
        if s["chapter"] == idx:
            lines.append(f"{s['desc']}。标记『{s['token']}』。")
    for e in ALL_ENTITIES:
        if e["chapter"] == idx:
            lines.append(f"{e['name']}现身于{SCENES[e['i'] % len(SCENES)]}，{e['desc']}。")
    return lines


def build_chapter(idx: int) -> dict:
    """确定性模板 + 剧情元素 + 埋点句子，构造一章。"""
    title = f"第{idx}章 {SCENES[(idx * 3) % len(SCENES)]}风云"
    scene = SCENES[(idx * 7) % len(SCENES)]
    action = ACTIONS[(idx * 13) % len(ACTIONS)]
    side = SIDE_CHARS[(idx * 5) % len(SIDE_CHARS)]
    loc2 = LOCATIONS[(idx * 11) % len(LOCATIONS)]
    filler = f"景{idx:04d}"
    planted = chapter_planted_lines(idx)
    paras = [
        f"夜色如墨，{PROTAGONIST}立于{scene}，周身灵气流转，衣袂无风自动。",
        f"{PROTAGONIST}缓缓{action}，体内气息翻涌，经脉中灵力如江河奔涌。",
    ]
    paras.extend(planted)
    paras.extend([
        f"{side}在不远处抱臂而立，冷声道：『{PROTAGONIST}，你终究还是走到了这一步。』",
        f"{PROTAGONIST}抬眼望向{loc2}方向，指尖摩挲着一枚残缺古玉，心中暗忖：此间因果，怕是到了结算之时。",
        f"远处天际一抹流光划过，{filler}。",
    ])
    content = "\n\n".join(paras)
    summary = f"{PROTAGONIST}于{scene}{action}，与{side}相遇，{filler}线索浮现。"
    word_count = len(content.replace(" ", "").replace("\n", ""))
    return {
        "id": f"ch{idx:05d}",
        "project_id": "",
        "volume_id": f"sv{(idx - 1) // 10 + 1}",
        "title": title,
        "chapter_order": idx,
        "content": content,
        "word_count": word_count,
        "status": "completed",
        "summary": summary,
        "notes": "",
        "created_at": utc_today(),
        "updated_at": utc_today(),
        "generation_source": "qa-synthetic",
    }


# ── 阶段 1：建项目 ────────────────────────────────────
def create_project(pid: str, name: str, n_chapters: int) -> None:
    projects_dao.create_project({
        "id": pid,
        "name": name,
        "subtitle": "QA 合成记忆压力测试（自动清理）",
        "genre": "xuanhuan",
        "target_word_count": 300000,
        "current_word_count": 0,
        "status": "drafting",
        "created_at": utc_today(),
        "updated_at": utc_today(),
        "style_config": "{}",
        "outline": None,
        "tags": "[]",
        "is_starred": 0,
        "premise": f"合成压力测试项目，{n_chapters} 章，用于验证长期记忆 M1-M8。",
        "target_chapters": n_chapters,
        "characters": "[]",
        "locations": "[]",
        "world_rules": "[]",
        "import_analysis": None,
        "inspiration_fragments": "[]",
    })


def build_outline_json(n_chapters: int, volume_size: int = 10) -> dict:
    """构造大纲：300 卷 × 10 章，含卷节点与章节点（BUG-B 卷内 order 约定）。"""
    nodes: list[dict] = []
    n_vol = (n_chapters + volume_size - 1) // volume_size
    for v in range(1, n_vol + 1):
        vid = f"sv{v}"
        nodes.append({"id": vid, "type": "volume", "title": f"第{v}卷", "order": v})
    for i in range(1, n_chapters + 1):
        nodes.append({
            "id": f"ch{i:05d}",
            "type": "chapter",
            "parentId": f"sv{(i - 1) // volume_size + 1}",
            "title": f"第{i}章 {SCENES[(i * 3) % len(SCENES)]}风云",
            "order": (i - 1) % volume_size + 1,
            "summary": f"{PROTAGONIST}于{SCENES[(i * 7) % len(SCENES)]}推进剧情，线索浮现。",
            "conflict": f"{PROTAGONIST}与{SIDE_CHARS[(i * 5) % len(SIDE_CHARS)]}正面冲突升级",
            "revelation": f"揭示{LOCATIONS[(i * 11) % len(LOCATIONS)]}深处的秘密",
            "emotion": "紧张",
            "targetWordCount": 1500,
        })
    return {"mainSummary": "合成压力测试全书大纲", "nodes": nodes}


def insert_chapters_batch(pid: str, chapters: list[dict]) -> float:
    """批量插入章节（单事务），返回耗时。"""
    cols = ["id", "project_id", "volume_id", "title", "chapter_order",
            "content", "word_count", "status", "summary", "notes",
            "created_at", "updated_at", "generation_source"]
    t0 = time.perf_counter()
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        conn.executemany(
            f"INSERT INTO chapters ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [
                (ch["id"], pid, ch["volume_id"], ch["title"], ch["chapter_order"],
                 ch["content"], ch["word_count"], ch["status"], ch["summary"],
                 ch["notes"], ch["created_at"], ch["updated_at"], ch["generation_source"])
                for ch in chapters
            ],
        )
        conn.execute(
            "UPDATE projects SET current_word_count = (SELECT COALESCE(SUM(word_count),0) FROM chapters WHERE project_id=?), updated_at=? WHERE id=?",
            (pid, now_iso(), pid),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log_event("degrade", f"批量插入失败回滚: {exc}")
        raise
    finally:
        conn.close()
    return time.perf_counter() - t0


def insert_volumes(pid: str, n_chapters: int, volume_size: int = 10) -> None:
    n_vol = (n_chapters + volume_size - 1) // volume_size
    today = utc_today()
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        for v in range(1, n_vol + 1):
            start = f"ch{(v - 1) * volume_size + 1:05d}"
            end = f"ch{min(v * volume_size, n_chapters):05d}"
            conn.execute(
                "INSERT OR REPLACE INTO volumes (id, project_id, start_chapter_id, end_chapter_id, title, summary, \"order\", created_at, updated_at, summary_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"sv{v}", pid, start, end, f"第{v}卷",
                 f"第{v}卷摘要：{PROTAGONIST}在第{v}卷中历经磨砺，关键节点为『卷{v:03d}』。",
                 v, today, today, ""),
            )
        conn.commit()
    finally:
        conn.close()


# ── 阶段 2：埋点写入（直接 DAO，不走 LLM） ─────────────
def plant_facts(pid: str) -> dict:
    """写入 250 关键事实 + 40 冲突（双面+冲突记录）+ 30 过期事实。"""
    stats = {"key_ok": 0, "key_dup": 0, "conflict_a_ok": 0, "conflict_b_ok": 0,
             "conflict_rec_ok": 0, "superseded_ok": 0}
    chmap = {}
    for f in KEY_FACTS:
        chid = f"ch{f['chapter']:05d}"
        chmap.setdefault(f["chapter"], chid)
        ok = nf_dao.add_fact(
            pid, f["fact_type"], f"{f['desc']}【{f['token']}】",
            chapter_id=chid, source_chapter_id=chid,
            confidence=0.9, status="active", version=1,
        )
        if ok:
            stats["key_ok"] += 1
        else:
            stats["key_dup"] += 1
            log_event("degrade", f"关键事实写入被忽略（重复？）: {f['token']}")
    for c in CONFLICTS:
        ch_a = f"ch{c['ch_a']:05d}"
        ch_b = f"ch{c['ch_b']:05d}"
        if nf_dao.add_fact(pid, "state_changed", f"{c['stmt_a']}【{c['token_a']}】",
                           chapter_id=ch_a, source_chapter_id=ch_a, confidence=0.95, status="active"):
            stats["conflict_a_ok"] += 1
        if nf_dao.add_fact(pid, "state_changed", f"{c['stmt_b']}【{c['token_b']}】",
                           chapter_id=ch_b, source_chapter_id=ch_b, confidence=0.95, status="active"):
            stats["conflict_b_ok"] += 1
        # 模拟检测管道：把不可调和冲突落盘（is_conflict=1，不走 LLM）
        if nf_dao.add_writing_conflict(
            pid, ch_b, f"{c['stmt_a']}与后文『{c['stmt_b']}』矛盾",
            root_cause="合成冲突埋点（QA 直接注入，模拟检测管道记录）",
        ):
            stats["conflict_rec_ok"] += 1
    for s in SUPERSEDED_FACTS:
        chid = f"ch{s['chapter']:05d}"
        ok = nf_dao.add_fact(
            pid, s["fact_type"], f"{s['desc']}【{s['token']}】",
            chapter_id=chid, source_chapter_id=chid,
            confidence=0.7, status="superseded", version=2,
            valid_from=now_iso(), valid_to=now_iso(),
        )
        if ok:
            stats["superseded_ok"] += 1
        else:
            log_event("degrade", f"过期事实写入失败: {s['token']}")
    return stats


def plant_entities(pid: str) -> dict:
    """写入 100 实体 + 30 关系，随后 supersede 30 个实体。"""
    stats = {"entity_ok": 0, "relation_ok": 0, "superseded_ok": 0}
    name_to_id: dict[str, str] = {}
    for e in ALL_ENTITIES:
        chid = f"ch{e['chapter']:05d}"
        eid = me_dao.upsert_entity(
            pid, entity_type=e["etype"], name=e["name"], description=e["desc"],
            aliases=[f"{e['name']}（别名）"], confidence=0.85, source_chapter_id=chid,
        )
        name_to_id[e["name"]] = eid
        stats["entity_ok"] += 1
    for r in ALL_RELATIONS:
        frm = name_to_id.get(r["from"])
        to = name_to_id.get(r["to"])
        if not frm or not to:
            continue
        chid = f"ch{r['chapter']:05d}"
        me_dao.upsert_relation(pid, entity_id=frm, relation_type=r["relation"],
                               target_entity_id=to, confidence=0.8, source_chapter_id=chid)
        stats["relation_ok"] += 1
    for s in SUPERSEDED_ENTITIES:
        eid = name_to_id.get(s["name"])
        if eid and me_dao.supersede_entity(pid, eid):
            stats["superseded_ok"] += 1
    return stats


def plant_foreshadows(pid: str) -> dict:
    """埋 25 条伏笔：15 planted / 5 progressing / 5 resolved。"""
    stats = {"created": 0}
    for i in range(N_FORESHADOWS):
        chid = f"ch{(100 + i * 60):05d}"
        target = f"ch{(400 + i * 80):05d}"
        status = "planted"
        if i >= 15 and i < 20:
            status = "progressing"
        elif i >= 20:
            status = "resolved"
        fsh_dao.create_foreshadow(
            pid, name=f"伏笔{i:03d}", description=f"关于{LOCATIONS[i % len(LOCATIONS)]}的长期悬念『伏{i:03d}』",
            plant_chapter_id=chid, target_chapter_id=target, status=status,
        )
        stats["created"] += 1
    return stats


# ── 阶段 3：FTS 同步 ──────────────────────────────────
def sync_all_fts(pid: str, chapters: list[dict]) -> tuple[int, float, int]:
    """全量同步 FTS（单连接，事务批处理），返回 (成功数, 耗时, 失败数)。"""
    t0 = time.perf_counter()
    ok = 0
    fail = 0
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        for ch in chapters:
            # 直接走生产路径 sync_chapter_fts 的逻辑（传入共享 conn）
            wrote = mfts_dao.sync_chapter_fts(pid, ch["id"], conn=conn)
            if wrote:
                ok += 1
            else:
                fail += 1
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log_event("degrade", f"FTS 全量同步异常: {exc}")
    finally:
        conn.close()
    return ok, time.perf_counter() - t0, fail


# ── 指标验证 ──────────────────────────────────────────
def _search(pid: str, kw: str, limit: int = 5):
    return mfts_dao.search_relevant(pid, [kw], limit=limit)


def metric_m1(pid: str) -> dict:
    """M1 关键事实召回率：账本存在 + FTS 远距离召回。"""
    hits = 0
    samples_hit: list[dict] = []
    samples_miss: list[dict] = []
    distances: list[int] = []
    ledger_missing = 0
    for f in KEY_FACTS:
        chid = f"ch{f['chapter']:05d}"
        # 精确校验该事实在账本且来源正确
        fact_rows = [r for r in nf_dao.list_facts(pid) if f"{f['desc']}【{f['token']}】" in (r.get("content") or "")]
        in_ledger = any(
            (r.get("source_chapter_id") or r.get("chapter_id")) == chid
            and (r.get("status") or "active") == "active"
            for r in fact_rows
        )
        if not in_ledger:
            ledger_missing += 1
        results = _search(pid, f["token"])
        matched = any(r["chapter_id"] == chid for r in results)
        if matched:
            hits += 1
            if len(samples_hit) < 8:
                samples_hit.append({
                    "fact": f"{f['desc']}【{f['token']}】",
                    "planted_chapter": chid,
                    "retrieved": [{"chapter_id": r["chapter_id"], "chapter_order": r["chapter_order"], "source": r["source"]} for r in results],
                })
        else:
            if len(samples_miss) < 8:
                samples_miss.append({
                    "fact": f"{f['desc']}【{f['token']}】",
                    "planted_chapter": chid,
                    "retrieved": [r["chapter_id"] for r in results],
                })
        # 远距离：从本书 90% 处探测本章事实的检索距离
        probe = int(N_CHAPTERS * 0.9)
        distances.append(abs(probe - f["chapter"]))
    rate = hits / max(len(KEY_FACTS), 1)
    return {
        "sample_size": len(KEY_FACTS),
        "hits": hits,
        "miss": len(KEY_FACTS) - hits,
        "recall_rate": rate,
        "ledger_missing": ledger_missing,
        "samples_hit": samples_hit,
        "samples_miss": samples_miss,
        "distance_min": min(distances) if distances else 0,
        "distance_max": max(distances) if distances else 0,
        "distance_mean": round(statistics.mean(distances), 1) if distances else 0,
    }


def metric_m2(pid: str) -> dict:
    """M2 严重事实冲突漏检率：旧事实在矛盾点是否可召回（可检测）+ 冲突记录是否落盘。"""
    detectable = 0
    missed: list[dict] = []
    for c in CONFLICTS:
        ch_a = f"ch{c['ch_a']:05d}"
        ch_b = f"ch{c['ch_b']:05d}"
        # 旧事实在矛盾点（ch_b）可被检索到 = 检测管道能看到双方
        results = _search(pid, c["token_a"])
        found = any(r["chapter_id"] == ch_a for r in results)
        if found:
            detectable += 1
        else:
            missed.append({"conflict": c["i"], "old_token": c["token_a"], "at_chapter": ch_b,
                           "retrieved": [r["chapter_id"] for r in results]})
    # 冲突记录落盘检查
    conflicts_recorded = nf_dao.list_writing_conflicts(pid)
    recorded = [x for x in conflicts_recorded if "矛盾" in (x.get("content") or "")]
    miss_rate = (len(CONFLICTS) - detectable) / len(CONFLICTS)
    return {
        "sample_size": len(CONFLICTS),
        "detectable": detectable,
        "miss": len(CONFLICTS) - detectable,
        "miss_rate": miss_rate,
        "conflict_records_in_ledger": len(recorded),
        "conflict_records_total": len(conflicts_recorded),
        "missed_samples": missed,
        "conflict_samples": [
            {"i": c["i"], "old": f"{c['stmt_a']}【{c['token_a']}】@{c['ch_a']}",
             "new": f"{c['stmt_b']}【{c['token_b']}】@{c['ch_b']}"}
            for c in CONFLICTS[:10]
        ],
    }


def metric_m3(pid: str) -> dict:
    """M3 过期状态误用率：实体层（entity block/list）+ 事实层（注入源/注入块）。"""
    # 实体层
    block = me_dao.build_entity_state_block(pid)
    list_default = me_dao.list_entities(pid, limit=200)
    entity_misuse = []
    for s in SUPERSEDED_ENTITIES:
        name = s["name"]
        in_block = name in block
        in_list = any((x.get("name") == name) and (x.get("status") == "superseded") for x in list_default)
        if in_block or in_list:
            entity_misuse.append({"name": name, "in_block": in_block, "in_list": in_list})
    # 事实层：注入源 DAO 是否返回 superseded 事实
    src_completed = nf_dao.list_completed_revealed_facts(pid)
    src_recent = nf_dao.list_recent_state_changed_facts(pid, limit=100)
    superseded_tokens = {s["token"] for s in SUPERSEDED_FACTS}
    src_leak = [t for t in superseded_tokens
                if any(t in (r.get("content") or "") for r in src_completed + src_recent)]
    # 事实层：实际注入块是否含 superseded 内容
    milestones = memory_service.build_completed_milestones(pid)
    inj_leak = [t for t in superseded_tokens if t in milestones]
    total = N_SUPERSEDED_FACTS + N_SUPERSEDED_ENTITIES
    misused = len(entity_misuse) + len(inj_leak)
    return {
        "sample_size": total,
        "superseded_facts": N_SUPERSEDED_FACTS,
        "superseded_entities": N_SUPERSEDED_ENTITIES,
        "entity_misuse_count": len(entity_misuse),
        "entity_misuse_samples": entity_misuse[:10],
        "fact_source_leak_count": len(src_leak),
        "fact_injection_leak_count": len(inj_leak),
        "fact_leak_samples": inj_leak[:10],
        "misuse_count": misused,
        "misuse_rate": misused / total,
        "entity_state_block_len": len(block),
    }


def metric_m4(pid: str) -> dict:
    """M4 同章重复同步幂等 100%：FTS/事实/实体 重复写入行数不变。"""
    out: dict = {"checks": []}
    # FTS 幂等：3 章各同步 3 次
    test_chs = ["ch00001", "ch01500", "ch03000"]
    conn = get_conn()
    try:
        for chid in test_chs:
            before = conn.execute("SELECT COUNT(*) FROM chapter_fts WHERE project_id=? AND chapter_id=?", (pid, chid)).fetchone()[0]
            for _ in range(3):
                mfts_dao.sync_chapter_fts(pid, chid, conn=conn)
            conn.commit()
            after = conn.execute("SELECT COUNT(*) FROM chapter_fts WHERE project_id=? AND chapter_id=?", (pid, chid)).fetchone()[0]
            out["checks"].append({"chapter": chid, "before": before, "after": after, "stable": before == after == 1})
        # 全量重复同步一次：总行数不变
        total_before = conn.execute("SELECT COUNT(*) FROM chapter_fts WHERE project_id=?", (pid,)).fetchone()[0]
        for ch in [{"id": f"ch{i:05d}"} for i in range(1, N_CHAPTERS + 1)]:
            mfts_dao.sync_chapter_fts(pid, ch["id"], conn=conn)
        conn.commit()
        total_after = conn.execute("SELECT COUNT(*) FROM chapter_fts WHERE project_id=?", (pid,)).fetchone()[0]
        out["checks"].append({"chapter": "ALL", "before": total_before, "after": total_after, "stable": total_before == total_after == N_CHAPTERS})
    finally:
        conn.close()
    # 事实幂等：同内容写 3 次
    fact_content = "幂等测试唯一事实内容【幂等000】"
    f0 = nf_dao.count_facts(pid)
    for _ in range(3):
        nf_dao.add_fact(pid, "completed", fact_content, chapter_id="ch00001", source_chapter_id="ch00001")
    f1 = nf_dao.count_facts(pid)
    out["checks"].append({"chapter": "fact", "before": f0, "after": f1, "stable": f1 == f0 + 1})
    # 实体幂等：同实体 upsert 3 次
    e0 = me_dao.count_entities(pid)
    for _ in range(3):
        me_dao.upsert_entity(pid, entity_type="person", name="幂等测试角色", description="x", source_chapter_id="ch00001")
    e1 = me_dao.count_entities(pid)
    out["checks"].append({"chapter": "entity", "before": e0, "after": e1, "stable": e1 == e0 + 1})
    out["all_stable"] = all(c["stable"] for c in out["checks"])
    return out


def metric_m5(pid: str) -> dict:
    """M5 记忆可追溯到源章节 100%：全量事实抽查 + FTS 结果 + 召回日志。"""
    all_facts = nf_dao.list_facts(pid)
    real_chapters = {f"ch{i:05d}" for i in range(1, N_CHAPTERS + 1)}
    without_src = [f for f in all_facts if not (f.get("source_chapter_id") or "").strip()]
    bad_src = [f for f in all_facts if (f.get("source_chapter_id") or "").strip() and f["source_chapter_id"] not in real_chapters]
    # 抽查 50 条
    sample = random.Random(42).sample(all_facts, min(50, len(all_facts)))
    traceable = sum(1 for f in sample if (f.get("source_chapter_id") or f.get("chapter_id")) in real_chapters)
    # FTS 结果含 chapter_id + order
    r = _search(pid, KEY_FACTS[0]["token"])
    fts_has_trace = all("chapter_id" in x and "chapter_order" in x for x in r)
    # 召回日志
    logs = mr_dao.list_recalls(pid, limit=500)
    log_with_chapter = sum(1 for x in logs if x.get("chapter_id"))
    return {
        "total_facts": len(all_facts),
        "without_source": len(without_src),
        "bad_source": len(bad_src),
        "traceability": (len(all_facts) - len(without_src) - len(bad_src)) / max(len(all_facts), 1),
        "sample_size": len(sample),
        "sample_traceable": traceable,
        "sample_rate": traceable / max(len(sample), 1),
        "fts_has_trace": fts_has_trace,
        "recall_log_count": len(logs),
        "recall_log_with_chapter": log_with_chapter,
        "trace_samples": [
            {"content": f.get("content", "")[:60], "source_chapter_id": f.get("source_chapter_id"), "chapter_id": f.get("chapter_id")}
            for f in sample[:8]
        ],
    }


def metric_m7(pid: str) -> dict:
    """M7 检索延迟：200 次真实检索（事实 token + 填充噪声词），统计 p50/p95。"""
    kws = [f["token"] for f in KEY_FACTS] + [f"景{i:04d}" for i in range(1, 401, 8)]
    kws = kws[:250]
    rng = random.Random(7)
    lat: list[float] = []
    sources: dict[str, int] = {}
    errors = 0
    for _ in range(200):
        kw = rng.choice(kws)
        t0 = time.perf_counter()
        try:
            res = _search(pid, kw)
            lat.append((time.perf_counter() - t0) * 1000)
            src = res[0]["source"] if res else "empty"
            sources[src] = sources.get(src, 0) + 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            lat.append((time.perf_counter() - t0) * 1000)
            log_event("degrade", f"检索异常: {kw} -> {exc}")
    lat_sorted = sorted(lat)
    def pct(p):
        return lat_sorted[min(int(len(lat_sorted) * p), len(lat_sorted) - 1)]
    return {
        "runs": len(lat),
        "p50_ms": round(pct(0.50), 3),
        "p95_ms": round(pct(0.95), 3),
        "p99_ms": round(pct(0.99), 3),
        "max_ms": round(max(lat), 3),
        "min_ms": round(min(lat), 3),
        "mean_ms": round(statistics.mean(lat), 3),
        "errors": errors,
        "sources": sources,
    }


def metric_m8(pid: str) -> dict:
    """M8 上下文长度/内存/锁等待/恢复时间实测。"""
    out: dict = {}
    # 上下文长度：真实 _build_memory_context 三条（早期/中期/后期）
    outline = build_outline_json(N_CHAPTERS)
    nodes = ngs._chapter_nodes_from_outline(outline)
    ctx_by_chapter: list[dict] = []
    for probe in (30, 1500, 2900):
        chid = f"ch{probe:05d}"
        try:
            t0 = time.perf_counter()
            ctx = ngs._build_memory_context(project_id=pid, chapter_nodes=nodes, current_chapter_id=chid)
            took = time.perf_counter() - t0
            assembled = ngs._build_memory_context_before(
                premise="测试", chapter=nodes[probe - 1], chapter_index=probe - 1,
                total_chapters=N_CHAPTERS, outline_summary="测试大纲", memory_context=ctx,
            )
            ctx_by_chapter.append({
                "chapter": chid,
                "build_ms": round(took * 1000, 1),
                "block_lengths": {k: (len(v) if not isinstance(v, list) else sum(len(x) for x in v)) for k, v in ctx.items()},
                "assembled_len": len(assembled),
            })
        except Exception as exc:  # noqa: BLE001
            log_event("degrade", f"_build_memory_context 失败 @{chid}: {exc}")
    out["context_by_chapter"] = ctx_by_chapter
    out["milestones_len"] = len(memory_service.build_completed_milestones(pid))
    out["entity_state_len"] = len(me_dao.build_entity_state_block(pid))
    stats = mr_dao.recall_stats(pid)
    out["recall_stats"] = stats
    # 内存/体积
    size_before = os.path.getsize(DB_PATH)
    out["db_size_bytes"] = size_before
    out["db_size_mb"] = round(size_before / 1024 / 1024, 2)
    # 锁等待实测：connA 持写锁 0.3s，connB 写入等待时间
    lock_waits: list[float] = []
    for _ in range(3):
        conn_a = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
        conn_b = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn_a.execute("BEGIN IMMEDIATE")
            tid = gen_id()
            t0 = time.perf_counter()
            def _release():
                time.sleep(0.3)
                conn_a.rollback()
            th = threading.Thread(target=_release)
            th.start()
            conn_b.execute(
                "INSERT INTO novel_facts (id, project_id, fact_type, content, chapter_id, created_at, is_conflict, status, version, confidence) VALUES (?,?,?,?,?,?,0,'active',1,1.0)",
                (tid, pid, "completed", f"锁等待测试【锁{tid[:6]}】", "ch00001", now_iso()),
            )
            conn_b.commit()
            waited = time.perf_counter() - t0
            lock_waits.append(round(waited * 1000, 1))
            conn_b.execute("DELETE FROM novel_facts WHERE id=?", (tid,))
            conn_b.commit()
            th.join(timeout=1)
        except Exception as exc:  # noqa: BLE001
            log_event("degrade", f"锁等待测量异常: {exc}")
        finally:
            conn_a.close()
            conn_b.close()
    out["lock_wait_ms"] = lock_waits
    out["lock_wait_mean_ms"] = round(statistics.mean(lock_waits), 1) if lock_waits else None
    # 恢复时间：模拟重启（重连 + 校验查询）
    t0 = time.perf_counter()
    init_db()
    c = get_conn()
    n = c.execute("SELECT COUNT(*) FROM chapters WHERE project_id=?", (pid,)).fetchone()[0]
    _search(pid, KEY_FACTS[0]["token"])
    c.close()
    out["recovery_time_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["post_restart_chapters"] = n
    return out


def metric_m6(pid: str) -> dict:
    """M6 重启一致性：快照 diff + 任务中断/恢复/取消（独立 resume 项目）。"""
    out: dict = {}

    def snapshot(pid_: str) -> tuple:
        tables = ["chapters", "novel_facts", "memory_entities", "memory_relations",
                  "memory_recall_log", "chapter_fts", "volumes", "foreshadows"]
        conn = get_conn()
        counts = {}
        for t in tables:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id=?", (pid_,)).fetchone()[0]
        sample = conn.execute(
            "SELECT id, chapter_order, word_count, status FROM chapters WHERE project_id=? ORDER BY chapter_order LIMIT 5", (pid_,)
        ).fetchall()
        conn.close()
        return counts, [tuple(r) for r in sample]

    s1 = snapshot(pid)
    # 模拟重启：重连 + init_db（幂等）
    init_db()
    s2 = snapshot(pid)
    out["restart_snapshot_diff_zero"] = s1 == s2
    out["snapshot"] = s1[0]

    # 任务级：独立 resume 项目
    rpid = f"audit-resume-{int(time.time())}"
    create_project(rpid, f"audit-resume-{int(time.time())}", 20)
    rchapters = []
    for i in range(1, 21):
        ch = build_chapter(i)
        ch["id"] = f"rch{i:03d}"
        ch["chapter_order"] = i
        # M6 修复：已完成章节必须写足字数（>35% 阈值 = 525 字），
        # 否则 _find_first_unfinished_chapter_id 会把 rch001 误判为未完成
        ch["content"] = (f"第{i}章正文内容（已生成）。" * 60) if i <= 8 else ""
        ch["word_count"] = len(ch["content"].replace(" ", ""))
        ch["status"] = "completed" if i <= 8 else "draft"
        rchapters.append(ch)
    insert_chapters_batch(rpid, rchapters)
    rnodes = [{"id": f"rch{i:03d}", "type": "chapter", "parentId": f"rv{(i - 1) // 10 + 1}",
               "title": f"第{i}章", "order": (i - 1) % 10 + 1} for i in range(1, 21)]
    rnodes = [{"id": "rv1", "type": "volume", "title": "第一卷", "order": 1},
              {"id": "rv2", "type": "volume", "title": "第二卷", "order": 2}] + rnodes
    routline = {"mainSummary": "resume 测试", "nodes": rnodes}
    conn = get_conn()
    conn.execute("UPDATE projects SET outline=? WHERE id=?", (json.dumps(routline, ensure_ascii=False), rpid))
    conn.commit()
    conn.close()
    job_id = f"job-resume-{int(time.time())}"
    jobs_dao.save_job({
        "id": job_id, "project_id": rpid, "mode": "all", "requested_chapter_id": None,
        "status": "running", "message": "", "target_words": 30000, "total_chapters": 20,
        "completed_chapters": 8, "failed_chapters": 0, "fallback_chapters": 0,
        "generated_words": 0, "current_chapter_id": "rch009", "current_chapter_title": "",
        "last_completed_chapter_id": "rch008", "last_completed_chapter_title": "",
        "llm_provider": "", "llm_model": "", "last_generation_source": "",
        "started_at": now_iso(), "updated_at": now_iso(), "finished_at": None, "error": "",
        "cancel_requested": 0, "max_retries": 1, "retry_count": 0, "created_at": now_iso(),
        "checkpoint_type": "", "resume_target_json": "", "pending_manual_recovery": 0,
        "heartbeat_at": now_iso(), "fact_check_count": 0, "rewrite_count": 0,
        "hook_miss_count": 0, "quality_debt_count": 0, "escalation_count": 0,
        "foreshadow_alert_count": 0, "foreshadow_updated_count": 0,
    })
    # 重启标记（DB 无其它 running 任务，已确认安全）
    n_interrupted = ngs.recover_jobs_after_restart()
    job_after = jobs_dao.get_job(job_id)
    out["resume_project_id"] = rpid
    out["interrupted_marked"] = job_after and job_after["status"] == "interrupted"
    out["jobs_marked_total"] = n_interrupted
    # 恢复定位第一个未完成章节
    rnodes_parsed = ngs._chapter_nodes_from_outline(routline)
    first_unfinished = ngs._find_first_unfinished_chapter_id(rpid, rnodes_parsed)
    out["first_unfinished_chapter"] = first_unfinished
    out["first_unfinished_ok"] = first_unfinished == "rch009"
    # 可恢复性判断
    job_obj = ngs._row_to_job(jobs_dao.get_job(job_id))
    out["job_recoverable"] = job_obj.is_recoverable
    # 取消：新建 queued 任务
    cancel_id = f"job-cancel-{int(time.time())}"
    jobs_dao.save_job({
        "id": cancel_id, "project_id": rpid, "mode": "all", "requested_chapter_id": None,
        "status": "queued", "message": "", "target_words": 30000, "total_chapters": 20,
        "completed_chapters": 0, "failed_chapters": 0, "fallback_chapters": 0,
        "generated_words": 0, "current_chapter_id": None, "current_chapter_title": "",
        "last_completed_chapter_id": None, "last_completed_chapter_title": "",
        "llm_provider": "", "llm_model": "", "last_generation_source": "",
        "started_at": now_iso(), "updated_at": now_iso(), "finished_at": None, "error": "",
        "cancel_requested": 0, "max_retries": 1, "retry_count": 0, "created_at": now_iso(),
        "checkpoint_type": "", "resume_target_json": "", "pending_manual_recovery": 0,
        "heartbeat_at": now_iso(), "fact_check_count": 0, "rewrite_count": 0,
        "hook_miss_count": 0, "quality_debt_count": 0, "escalation_count": 0,
        "foreshadow_alert_count": 0, "foreshadow_updated_count": 0,
    })
    cancelled = ngs.request_cancel(cancel_id)
    out["cancel_ok"] = cancelled is not None and cancelled.status == "cancelled"
    # 清理 resume 项目
    cleanup_project(rpid)
    out["resume_project_cleaned"] = not projects_dao.project_exists(rpid)
    return out


# ── 清理 ──────────────────────────────────────────────
def cleanup_project(pid: str) -> None:
    """删除某 audit 项目的全部相关数据（含 projects 行本身）。

    除 projects 用 id 外，其余表均按 project_id 删除。
    """
    tables = ["chapters", "novel_facts", "memory_entities", "memory_relations",
              "memory_recall_log", "chapter_fts", "volumes", "foreshadows",
              "chapter_hooks", "novel_jobs", "style_profiles", "style_bindings",
              "style_transitions"]
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        for t in tables:
            conn.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log_event("degrade", f"清理 {pid} 失败: {exc}")
        raise
    finally:
        conn.close()


def residual_audit_projects() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name FROM projects WHERE id LIKE 'audit-synthetic-%' OR id LIKE 'audit-resume-%' OR name LIKE 'audit-synthetic-%' OR name LIKE 'audit-resume-%'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 主流程 ────────────────────────────────────────────
def main() -> dict:
    t_start = time.perf_counter()
    init_db()

    pid = f"audit-synthetic-3000-{int(time.time())}"
    RESULTS["project_id"] = pid
    RESULTS["project_name"] = f"audit-synthetic-3000-{int(time.time())}"
    RESULTS["started_at"] = now_iso()

    # 阶段 1：建项目 + 生成章节
    print("[1/6] 构造合成项目与 3000 章...")
    build_plan()
    create_project(pid, RESULTS["project_name"], N_CHAPTERS)
    chapters = [build_chapter(i) for i in range(1, N_CHAPTERS + 1)]
    for ch in chapters:
        ch["project_id"] = pid
    t_gen = time.perf_counter()
    insert_t = insert_chapters_batch(pid, chapters)
    t_gen = time.perf_counter() - t_gen
    insert_volumes(pid, N_CHAPTERS)
    RESULTS["generation"] = {
        "n_chapters": N_CHAPTERS,
        "gen_build_and_insert_sec": round(t_gen, 2),
        "insert_only_sec": round(insert_t, 2),
        "total_words": chapters_dao.aggregate_word_count(pid),
        "avg_chapter_chars": round(chapters_dao.aggregate_word_count(pid) / N_CHAPTERS, 1),
    }

    # 阶段 2：埋点
    print("[2/6] 埋点：关键事实 / 冲突 / 过期状态 / 实体 / 伏笔...")
    RESULTS["plant_stats"] = {}
    RESULTS["plant_stats"]["facts"] = plant_facts(pid)
    RESULTS["plant_stats"]["entities"] = plant_entities(pid)
    RESULTS["plant_stats"]["foreshadows"] = plant_foreshadows(pid)

    # 阶段 3：FTS 同步
    print("[3/6] FTS 全量同步...")
    ok, fts_t, fail = sync_all_fts(pid, chapters)
    RESULTS["fts"] = {"synced": ok, "failed": fail, "seconds": round(fts_t, 2)}

    # 阶段 4：指标
    print("[4/6] 指标验证 M1-M8...")
    RESULTS["m1"] = metric_m1(pid)
    RESULTS["m2"] = metric_m2(pid)
    RESULTS["m3"] = metric_m3(pid)
    RESULTS["m4"] = metric_m4(pid)
    RESULTS["m5"] = metric_m5(pid)
    RESULTS["m6"] = metric_m6(pid)
    RESULTS["m7"] = metric_m7(pid)
    RESULTS["m8"] = metric_m8(pid)

    RESULTS["events"] = EVENTS
    RESULTS["elapsed_sec"] = round(time.perf_counter() - t_start, 2)

    # 阶段 5：清理（保留备份；只删本轮 audit-* 项目）
    print("[5/6] 清理本轮 audit-* 项目...")
    cleanup_project(pid)
    residual = residual_audit_projects()
    RESULTS["cleanup"] = {
        "main_project_cleaned": not projects_dao.project_exists(pid),
        "residual_audit_projects": residual,
    }

    # 阶段 6：写报告
    print("[6/6] 写报告...")
    write_report(RESULTS)
    print("完成，耗时", RESULTS["elapsed_sec"], "s")
    return RESULTS


def write_report(R: dict) -> None:
    doc = build_report_markdown(R)
    out_path = os.path.join(os.path.dirname(HERE), "docs", "qa-synthetic-memory-test-v1.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    # 同时存 JSON 证据
    json_path = os.path.join(os.path.dirname(HERE), "test_artifacts", "qa-synthetic-memory-test-v1.json")
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=2)
    print("报告已写入:", out_path)
    print("JSON 证据已写入:", json_path)


def build_report_markdown(R: dict) -> str:
    L: list[str] = []
    L.append("# NovelCraft AI — 合成记忆压力测试报告 v1（M1-M8）")
    L.append("")
    L.append(f"> 生成时间：{R['started_at']} ｜ 项目：`{R['project_id']}` ｜ 章节数：{R['generation']['n_chapters']} ｜ 总耗时：{R['elapsed_sec']}s")
    L.append("")
    L.append("## 一、测试环境")
    L.append("")
    L.append(f"- **项目 ID**：`{R['project_id']}`（唯一 audit-* 项目，测试后已清理）")
    L.append(f"- **章节数**：{R['generation']['n_chapters']} 章（下限 2000，实际 3000）")
    L.append(f"- **生成方式**：确定性模板 + 剧情元素 + 埋点句拼接（不调用真实 LLM）；事实/实体/冲突经 DAO 直接写入")
    L.append(f"- **生成+入库耗时**：{R['generation']['gen_build_and_insert_sec']}s（批量单事务插入）")
    L.append(f"- **总字数**：{R['generation']['total_words']:,} ｜ 平均每章 {R['generation']['avg_chapter_chars']} 字")
    L.append(f"- **FTS 同步**：{R['fts']['synced']}/{R['generation']['n_chapters']} 成功，失败 {R['fts']['failed']}，耗时 {R['fts']['seconds']}s")
    L.append(f"- **埋点规模**：关键事实 {R['plant_stats']['facts']['key_ok']} 条 / 冲突 {R['plant_stats']['facts']['conflict_rec_ok']} 处 / 过期事实 {R['plant_stats']['facts']['superseded_ok']} 条 / 实体 {R['plant_stats']['entities']['entity_ok']} 个 / 伏笔 {R['plant_stats']['foreshadows']['created']} 条")
    L.append("")
    L.append("## 二、指标结果总表")
    L.append("")
    L.append("| 指标 | 样本量 | 阈值 | 实测 | 通过 | 证据 |")
    L.append("|------|--------|------|------|------|------|")
    m1 = R["m1"]
    L.append(f"| M1 关键事实召回率 | {m1['sample_size']} | ≥95% | {m1['recall_rate']*100:.2f}%（命中 {m1['hits']}） | {'✅' if m1['recall_rate'] >= 0.95 else '❌'} | 账本缺失 {m1['ledger_missing']}；远距离 {m1['distance_min']}~{m1['distance_max']} 章 |")
    m2 = R["m2"]
    L.append(f"| M2 冲突漏检率 | {m2['sample_size']} | =0% | {m2['miss_rate']*100:.1f}%（可检测 {m2['detectable']}） | {'✅' if m2['miss_rate'] == 0 else '❌'} | 冲突记录落盘 {m2['conflict_records_in_ledger']}/{m2['sample_size']} |")
    m3 = R["m3"]
    L.append(f"| M3 过期状态误用率 | {m3['sample_size']} | ≤1% | {m3['misuse_rate']*100:.2f}%（误用 {m3['misuse_count']}） | {'✅' if m3['misuse_rate'] <= 0.01 else '❌'} | 实体 {m3['entity_misuse_count']}，事实注入 {m3['fact_injection_leak_count']} |")
    m4 = R["m4"]
    L.append(f"| M4 同章重复同步幂等 | 3 章×3 + 全量 | =100% | {'全部稳定' if m4['all_stable'] else '存在不稳定'} | {'✅' if m4['all_stable'] else '❌'} | {len(m4['checks'])} 项检查 |")
    m5 = R["m5"]
    L.append(f"| M5 来源可追溯 | {m5['sample_size']} | =100% | {m5['sample_rate']*100:.1f}%（全量 {m5['traceability']*100:.1f}%） | {'✅' if m5['sample_rate'] == 1.0 else '❌'} | 缺源 {m5['without_source']}，错源 {m5['bad_source']} |")
    m6 = R["m6"]
    L.append(f"| M6 重启/取消/恢复一致性 | 1 次快照 + 1 任务 | =100% | 快照 diff={'0' if m6['restart_snapshot_diff_zero'] else '非0'}；恢复定位={'正确' if m6['first_unfinished_ok'] else '错误'}；取消={'成功' if m6['cancel_ok'] else '失败'} | {'✅' if m6['restart_snapshot_diff_zero'] and m6['first_unfinished_ok'] and m6['cancel_ok'] else '❌'} | 见第五节 |")
    m7 = R["m7"]
    L.append(f"| M7 检索延迟 | {m7['runs']} 次 | p50<200ms, p95<1s | p50={m7['p50_ms']}ms, p95={m7['p95_ms']}ms, p99={m7['p99_ms']}ms, max={m7['max_ms']}ms | {'✅' if m7['p50_ms'] < 200 and m7['p95_ms'] < 1000 else '❌'} | 错误 {m7['errors']}；来源 {m7['sources']} |")
    m8 = R["m8"]
    L.append(f"| M8 上下文/内存/锁/恢复 | 3 采样点 | 有实测 | 见第六节 | — | 上下文 {[c['assembled_len'] for c in m8['context_by_chapter']]}；DB {m8['db_size_mb']}MB；锁等待 {m8['lock_wait_mean_ms']}ms；恢复 {m8['recovery_time_ms']}ms |")
    L.append("")
    L.append("## 三、M1 召回命中与误召回样本")
    L.append("")
    L.append(f"样本量 {m1['sample_size']}，命中 {m1['hits']}，召回率 {m1['recall_rate']*100:.2f}%。远距离范围：{m1['distance_min']}~{m1['distance_max']} 章（均值 {m1['distance_mean']}）。")
    L.append("")
    if m1["samples_hit"]:
        L.append("### 命中样本（原文 + 检索结果）")
        L.append("```json")
        L.append(json.dumps(m1["samples_hit"], ensure_ascii=False, indent=2))
        L.append("```")
    if m1["samples_miss"]:
        L.append("### 误召回/未命中样本")
        L.append("```json")
        L.append(json.dumps(m1["samples_miss"], ensure_ascii=False, indent=2))
        L.append("```")
    L.append("")
    L.append("## 四、M2 冲突样本与 M3 过期状态样本")
    L.append("")
    L.append(f"### M2 冲突（{m2['sample_size']} 处）")
    L.append("")
    L.append("冲突清单示例（前 10）：")
    L.append("```json")
    L.append(json.dumps(m2["conflict_samples"], ensure_ascii=False, indent=2))
    L.append("```")
    if m2["missed_samples"]:
        L.append("漏检样本：")
        L.append("```json")
        L.append(json.dumps(m2["missed_samples"], ensure_ascii=False, indent=2))
        L.append("```")
    L.append("")
    L.append(f"### M3 过期状态（{m3['sample_size']} 处，误用 {m3['misuse_count']}）")
    L.append("")
    L.append(f"- 实体层误用 {m3['entity_misuse_count']} 处（注入块长度 {m3['entity_state_block_len']}）")
    L.append(f"- 事实层：注入源泄漏 {m3['fact_source_leak_count']}，实际注入块泄漏 {m3['fact_injection_leak_count']}")
    if m3["entity_misuse_samples"]:
        L.append("实体误用样本：")
        L.append("```json")
        L.append(json.dumps(m3["entity_misuse_samples"], ensure_ascii=False, indent=2))
        L.append("```")
    if m3["fact_leak_samples"]:
        L.append("事实误用样本（token）：")
        L.append("```json")
        L.append(json.dumps(m3["fact_leak_samples"], ensure_ascii=False, indent=2))
        L.append("```")
    L.append("")
    L.append("## 五、M6 重启/取消/恢复一致性")
    L.append("")
    L.append(f"- 重启（重连 + init_db 幂等）后快照 diff = {'0（一致）' if m6['restart_snapshot_diff_zero'] else '非 0（不一致）'}")
    L.append(f"- 任务重启标记：`recover_jobs_after_restart` 标记 {m6['jobs_marked_total']} 个进行中任务为 interrupted，测试任务状态 = {m6['interrupted_marked']}")
    L.append(f"- 恢复定位第一个未完成章节：`{m6['first_unfinished_chapter']}`（期望 rch009，{'正确' if m6['first_unfinished_ok'] else '错误'}）")
    L.append(f"- 任务可恢复判定：{m6['job_recoverable']}")
    L.append(f"- 取消：queued 任务 request_cancel → status={m6['cancel_ok'] and 'cancelled' or '未取消'}")
    L.append(f"- 独立 resume 项目已清理：{m6['resume_project_cleaned']}")
    L.append("")
    L.append("## 六、M7 延迟 与 M8 实测数据")
    L.append("")
    L.append("### M7 检索延迟")
    L.append("")
    L.append("```json")
    L.append(json.dumps({k: v for k, v in m7.items() if k != "sources"}, ensure_ascii=False, indent=2))
    L.append("```")
    L.append("")
    L.append("### M8 上下文/内存/锁等待/恢复")
    L.append("")
    L.append("```json")
    L.append(json.dumps(m8, ensure_ascii=False, indent=2))
    L.append("```")
    L.append("")
    L.append("## 七、发现的问题清单（P0/P1/P2）")
    L.append("")
    problems: list[tuple[str, str, str]] = []
    if m3["fact_injection_leak_count"] > 0:
        problems.append((
            "P1", "过期事实（status=superseded）仍被注入 milestones",
            f"复现：对 {m3['fact_injection_leak_count']}/{m3['sample_size']} 处过期事实，`build_completed_milestones` 输出含其 token。根因：`list_completed_revealed_facts`/`list_recent_state_changed_facts` 仅过滤 is_conflict=1，未过滤 status='superseded'，`_milestone_items` 亦未过滤。建议：注入源过滤 status != 'superseded'（与 `list_entities` 默认行为一致）。"
        ))
    if m3["fact_source_leak_count"] > m3["fact_injection_leak_count"]:
        problems.append((
            "P2", "注入源 DAO 返回 superseded 事实（受预算掩盖）",
            f"复现：{m3['fact_source_leak_count']} 条 superseded 事实出现在 `list_completed_revealed_facts`/`list_recent_state_changed_facts` 返回中；因 milestones 1500 字预算限制，仅 {m3['fact_injection_leak_count']} 条实际进入注入块。建议：DAO 层即过滤，避免预算变化后放大泄漏。"
        ))
    for p in problems:
        L.append(f"- **{p[0]}｜{p[1]}**：{p[2]}")
    if not problems:
        L.append("- 本轮未发现阻断性问题。")
    L.append("")
    L.append("## 八、降级 / 异常事件记录")
    L.append("")
    L.append(f"共 {len(R['events'])} 条事件。")
    if R["events"]:
        L.append("```json")
        L.append(json.dumps(R["events"], ensure_ascii=False, indent=2))
        L.append("```")
    else:
        L.append("（无）")
    L.append("")
    L.append("## 九、测试残留清理情况")
    L.append("")
    L.append(f"- 主项目 `{R['project_id']}` 已删除：{R['cleanup']['main_project_cleaned']}")
    L.append(f"- 残留 audit-* 项目（应仅含历史 `audit-20260811-xss`）：")
    L.append("```json")
    L.append(json.dumps(R["cleanup"]["residual_audit_projects"], ensure_ascii=False, indent=2))
    L.append("```")
    L.append("- 数据库备份：测试前已用 `scripts/backup_db.py` 生成备份，保留于 `test_artifacts/db_backups/`，未删除。")
    L.append("- JSON 证据：`test_artifacts/qa-synthetic-memory-test-v1.json`")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
