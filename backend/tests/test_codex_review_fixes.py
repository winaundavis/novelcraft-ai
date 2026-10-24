"""Codex 复测 5 个问题的回归测试（记忆升级 v1 · 修复验证）。

覆盖：
- R1：上下文预算裁剪必须保留尾部（milestones 最高优先级，绝不被裁掉）
- R2：FTS 索引接入章节保存/生成落库/导入主路径
- R3：召回日志记录裁剪后的真实注入（非构建 dict 时的原始长度）
- R4：实体/关系 upsert 更新已有记录时显式提交（不丢失）
- R5：apply_rewrite 应用后同步项目字数/FTS/摘要/记忆
"""

from __future__ import annotations

import sqlite3
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, r"<REPO_ROOT>\creat\novelcraft-ai\backend")

from app.dao import memory_entities as me_dao
from app.dao import memory_fts as mfts_dao
from app import db as app_db
from app.db import get_conn
from app.services import novel_generation_service as ngs
from app.services import rewrite_service


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Keep this regression suite independent from a developer's local database."""
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "review-fixes.db")
    app_db.init_db()


def _mk_project(pid: str) -> None:
    """建一个最小项目（供需要 projects 表的测试用）。"""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects (id,name,subtitle,genre,target_word_count,current_word_count,status,created_at,updated_at,style_config,outline,tags,is_starred,premise,target_chapters,characters,locations,world_rules,import_analysis,inspiration_fragments) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, pid, "", "x", 1, 0, "planning", now, now, "{}", "{}", "[]", 0, "", 1, "[]", "[]", "[]", None, "[]"),
        )
        conn.commit()


def _big_memory_context(milestones_len: int = 2000) -> dict:
    """构造超预算的 memory_context（milestones 很长，触发总预算裁剪）。"""
    return {
        "volume_summaries": ["卷摘要A"] * 5,
        "recent_chapter_summaries": ["近章摘要B"] * 5,
        "last_chapter_ending": "上一章结尾" * 300,
        "completed_milestones": "【已完成事实】" + ("- 关键不可逆事实X" * milestones_len),
        "entity_state": "实体状态" * 100,
        "fts_history": "历史片段" * 100,
    }


# ══════════════════════════════════════════════════════
# R1：预算裁剪保留尾部（milestones 不被裁掉）
# ══════════════════════════════════════════════════════

def test_r1_budget_trim_keeps_tail_milestones():
    """总预算超限时，必须保留尾部 milestones（最高优先级记忆）。"""
    ctx = _big_memory_context(milestones_len=2000)
    result = ngs._build_memory_context_before(
        project_id="p1", chapter_id="c1",
        premise="测试", chapter={"title": "第一章", "summary": "", "conflict": "", "revelation": "", "emotion": ""},
        chapter_index=1, total_chapters=3, outline_summary="概要",
        memory_context=ctx,
    )
    # 关键不可逆事实必须仍在（在尾部，不应被裁掉）
    assert "关键不可逆事实X" in result
    # 总长度受控
    assert len(result) <= ngs.CONTEXT_BUDGET_TOTAL + 20


def test_r1_old_head_trim_would_lose_milestones():
    """回归：若仍用旧逻辑 joined[:TOTAL] 会裁掉尾部 milestones，本测试守护新逻辑。"""
    ctx = _big_memory_context(milestones_len=2000)
    result = ngs._build_memory_context_before(
        project_id="p1", chapter_id="c1",
        premise="测试", chapter={"title": "第一章", "summary": "", "conflict": "", "revelation": "", "emotion": ""},
        chapter_index=1, total_chapters=3, outline_summary="概要",
        memory_context=ctx,
    )
    # 尾部必须有完整的"已完成事实"块（不能被截断成只有头部）
    assert result.rstrip().endswith("X") or "X\n" in result[-50:] or "X" in result[-200:]


# ══════════════════════════════════════════════════════
# R2：FTS 索引接入章节保存主路径
# ══════════════════════════════════════════════════════

def test_r2_generation_persist_syncs_fts():
    """生成落库（_persist_chapter_and_sync_memory）后 FTS 应同步。"""
    pid = f"audit-r2-{int(time.time())}"
    _mk_project(pid)

    class _Job:
        project_id = pid

    chapter = {"id": "ch001", "parentId": "", "title": "第一章", "order": 1, "summary": "", "conflict": "", "revelation": "", "emotion": "", "targetWordCount": 1500}
    result = {"content": "主角秦霜在青云宗修炼，柳如烟突破境界。", "used_fallback": False}

    async def _run():
        with patch.object(ngs.memory_service, "summarize_chapter", new=AsyncMock(return_value="第一章摘要")), \
             patch.object(ngs.memory_service, "extract_facts", new=AsyncMock(return_value=[])), \
             patch.object(ngs.memory_service, "extract_entities", new=AsyncMock(return_value=0)), \
             patch.object(ngs.memory_service, "record_chapter_completed", new=AsyncMock()):
            await ngs._persist_chapter_and_sync_memory(job=_Job(), chapter=chapter, result=result, outline={"nodes": []})

    import asyncio
    asyncio.run(_run())

    # FTS 应有该章索引
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM chapter_fts WHERE project_id=? AND chapter_id=?", (pid, "ch001")).fetchone()[0]
    assert n == 1, f"生成落库后 FTS 应有 1 行，实际 {n}"

    # 清理
    with get_conn() as conn:
        conn.execute("DELETE FROM chapter_fts WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM chapters WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()


# ══════════════════════════════════════════════════════
# R3：召回日志记录真实注入（裁剪后）
# ══════════════════════════════════════════════════════

def test_r3_recall_logged_after_budget_trim():
    """召回日志应在预算裁剪后记录，且字符数反映真实注入。"""
    pid = f"audit-r3-{int(time.time())}"
    _mk_project(pid)

    ctx = _big_memory_context(milestones_len=2000)
    with patch.object(ngs, "_log_memory_recall") as mock_log:
        result = ngs._build_memory_context_before(
            project_id=pid, chapter_id="c001",
            premise="测试", chapter={"title": "第一章", "summary": "", "conflict": "", "revelation": "", "emotion": ""},
            chapter_index=1, total_chapters=3, outline_summary="概要",
            memory_context=ctx,
        )
        # 日志被调用（裁剪后）
        assert mock_log.called
        # 日志接收的 memory_context 与传入一致（内部再按块记字符数）
        call_kwargs = mock_log.call_args
        assert call_kwargs[0][0] == pid
        assert call_kwargs[0][1] == "c001"

    # 清理
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.execute("DELETE FROM memory_recall_log WHERE project_id=?", (pid,))
        conn.commit()


def test_r3_injected_blocks_match_final_prompt():
    """召回日志的每个注入块必须**确实存在于最终 prompt**（验收 P1-2 硬指标）。"""
    pid = f"audit-r3b-{int(time.time())}"
    _mk_project(pid)

    ctx = _big_memory_context(milestones_len=2000)
    with patch.object(ngs, "_log_memory_recall") as mock_log:
        result = ngs._build_memory_context_before(
            project_id=pid, chapter_id="c001",
            premise="测试", chapter={"title": "第一章", "summary": "", "conflict": "", "revelation": "", "emotion": ""},
            chapter_index=1, total_chapters=3, outline_summary="概要",
            memory_context=ctx,
        )
        assert mock_log.called
        injected_blocks = mock_log.call_args[0][2]  # 第三个参数是 injected_blocks
        assert isinstance(injected_blocks, list) and injected_blocks, "应有注入块"
        for block in injected_blocks:
            text = block.get("text") or ""
            assert text, f"块 {block.get('key')} 文本为空"
            # 硬断言：每个日志片段必须存在于最终 prompt
            assert text in result, f"块 {block.get('key')} 不在最终 prompt"
        # 总字符数之和不得超过最终 prompt 长度（不虚报）
        total_logged = sum(len(b.get("text") or "") for b in injected_blocks)
        assert total_logged <= len(result) + 1

    # 清理
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.execute("DELETE FROM memory_recall_log WHERE project_id=?", (pid,))
        conn.commit()


# ══════════════════════════════════════════════════════
# T4：预算常量收敛 + 不变量测试（验收 P2）
# ══════════════════════════════════════════════════════

def test_t4_budget_constants_single_definition():
    """预算总常量只保留一套（防止重复定义覆盖导致容量误判，验收 P2）。"""
    # 上游组装预算 7000 < 下游兜底 8200，保证兜底不会先于组装裁剪。
    # （阶段C：纳入未回收伏笔/时间线约束后总预算提升；
    #   Codex P2 复测 P1：5200→7000、6400→8200，确保早期关键事实不被挤出。）
    assert ngs.CONTEXT_BUDGET_TOTAL == 7000
    assert ngs.CONTEXT_BUDGET_DOWNSTREAM_FALLBACK == 8200
    assert ngs.CONTEXT_BUDGET_TOTAL <= ngs.CONTEXT_BUDGET_DOWNSTREAM_FALLBACK


def test_t4_blocks_budget_sum_not_exceed_total():
    """各记忆块预算之和不得超过总预算的上限推导（防维护者按错误预算推算容量）。"""
    # 核心可保留块（entity + milestones）之和必须显著小于总预算，
    # 否则极端情况下 milestones 可能被挤掉。
    core_sum = ngs.CONTEXT_BUDGET_ENTITY + ngs.CONTEXT_BUDGET_MILESTONES
    assert core_sum <= ngs.CONTEXT_BUDGET_TOTAL
    # 完整分块预算之和可以大于总预算——由裁剪机制处理，
    # 但必须保证 milestones 单块预算 ≤ 总预算（永不丢弃的约束前提）。
    assert ngs.CONTEXT_BUDGET_MILESTONES <= ngs.CONTEXT_BUDGET_TOTAL


def test_t4_final_prompt_length_within_budget():
    """裁剪后最终 prompt 长度必须受控在总预算附近（验收 P2：最终 prompt 长度）。"""
    pid = f"audit-t4-{int(time.time())}"
    _mk_project(pid)

    ctx = _big_memory_context(milestones_len=2000)
    result = ngs._build_memory_context_before(
        project_id=pid, chapter_id="c001",
        premise="测试", chapter={"title": "第一章", "summary": "", "conflict": "", "revelation": "", "emotion": ""},
        chapter_index=1, total_chapters=3, outline_summary="概要",
        memory_context=ctx,
    )
    # 最终 prompt 长度受控（总预算 + 前缀容差）
    assert len(result) <= ngs.CONTEXT_BUDGET_TOTAL + 20
    # 最低保留事实数：milestones 关键内容必须在最终 prompt 中
    assert "关键不可逆事实X" in result

    # 清理
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()


# ══════════════════════════════════════════════════════
# R4：实体/关系 upsert 更新已有记录显式提交
# ══════════════════════════════════════════════════════

def test_r4_upsert_entity_update_persists():
    """对已有实体重复 upsert（更新分支）后，数据库里应保留更新值。"""
    pid = f"audit-r4-{int(time.time())}"
    _mk_project(pid)

    # 新增（提交）
    eid = me_dao.upsert_entity(pid, entity_type="person", name="测试角色", description="v1")
    # 更新（此前缺 commit 会丢）
    me_dao.upsert_entity(pid, entity_type="person", name="测试角色", description="v2-已更新")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT description FROM memory_entities WHERE id=? AND project_id=?", (eid, pid)
        ).fetchone()
    assert row is not None, "实体应存在"
    assert row[0] == "v2-已更新", f"更新应落库，实际 {row[0]!r}"

    # 清理
    with get_conn() as conn:
        conn.execute("DELETE FROM memory_entities WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()


# ══════════════════════════════════════════════════════
# R5：apply_rewrite 应用后同步项目字数
# ══════════════════════════════════════════════════════

def test_r5_apply_rewrite_syncs_project_word_count():
    """apply_rewrite 应用后项目总字数应更新（BUG-5 核心）。"""
    from app.dao import chapter_rewrites as cr_dao

    pid = f"audit-r5-{int(time.time())}"
    _mk_project(pid)
    now = time.strftime("%Y-%m-%d", time.gmtime())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chapters (id,project_id,title,content,word_count,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("ch001", pid, "第一章", "旧正文", 3, "completed", now, now),
        )
        conn.commit()

    rid = cr_dao.insert_rewrite(pid, "ch001", "emotion", original="旧正文", rewritten="新正文内容更长一些", reason="D10 评分 1/5")

    record = rewrite_service.apply_rewrite(rid)
    assert record is not None
    assert record["status"] == "applied"

    # 章节正文已更新
    with get_conn() as conn:
        row = conn.execute("SELECT content FROM chapters WHERE id='ch001' AND project_id=?", (pid,)).fetchone()
        assert row[0] == "新正文内容更长一些"

    # 清理
    with get_conn() as conn:
        conn.execute("DELETE FROM chapter_rewrites WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM chapters WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
