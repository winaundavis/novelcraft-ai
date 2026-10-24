from unittest.mock import AsyncMock, patch

import pytest

from app.dao import chapter_rewrites
from app.services import copilot_candidate_service as service


class _IsolatedDb:
    """最小 sqlite 连接的薄包装：暴露 fts_calls 以便断言派生同步被触发。"""

    def __init__(self, conn):
        self._conn = conn
        self.fts_calls: list[tuple[str, str]] = []

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        return self._conn.commit()


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    import sqlite3
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT,
          current_word_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE chapters (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, chapter_order INTEGER,
          content TEXT, word_count INTEGER, status TEXT, summary TEXT DEFAULT '', notes TEXT DEFAULT '',
          created_at TEXT, updated_at TEXT);
        CREATE TABLE chapter_rewrites (id TEXT PRIMARY KEY, project_id TEXT, chapter_id TEXT,
          rewrite_type TEXT, original TEXT, rewritten TEXT, reason TEXT, score_before REAL,
          score_after REAL, status TEXT, created_at TEXT, updated_at TEXT, applied_at TEXT);
    """)
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at, current_word_count)"
        " VALUES ('p-d', 'D', '2026-01-01', '2026-01-01', 0)"
    )
    conn.execute("INSERT INTO chapters VALUES ('c-d', 'p-d', 'C', 1, '旧句子。', 4, 'draft', '', '', '2026-01-01', '2026-01-01')")
    conn.commit()
    db = _IsolatedDb(conn)
    monkeypatch.setattr(service, "get_conn", lambda: conn)
    monkeypatch.setattr(chapter_rewrites, "get_conn", lambda: conn)
    monkeypatch.setattr(service.projects_dao, "project_exists", lambda project_id: project_id == "p-d")
    monkeypatch.setattr(service.chapters_dao, "get_chapter", lambda chapter_id, project_id: {
        "id": "c-d", "project_id": "p-d", "content": "旧句子。"
    } if chapter_id == "c-d" and project_id == "p-d" else None)
    monkeypatch.setattr(
        service.memory_fts_dao,
        "sync_chapter_fts",
        lambda *args, **kwargs: db.fts_calls.append((args[0], args[1])) or True,
    )
    yield db
    conn.close()


@pytest.mark.asyncio
async def test_candidate_is_proposed_and_does_not_change_original(isolated_db):
    with patch.object(service.ai_service, "chat_reply", new=AsyncMock(return_value={"reply": "新句子。", "context": {"blocks": ["memory", "style"]}})):
        candidate = await service.create_candidate(project_id="p-d", chapter_id="c-d", action="rewrite", selection_text="旧句子。")
    assert candidate["status"] == "proposed"
    assert candidate["original"] == "旧句子。"
    assert candidate["candidate"] == "新句子。"
    assert candidate["diff"]
    assert isolated_db.execute("SELECT content FROM chapters WHERE id='c-d'").fetchone()[0] == "旧句子。"


@pytest.mark.asyncio
async def test_reject_and_regenerate_keep_history(isolated_db):
    with patch.object(service.ai_service, "chat_reply", new=AsyncMock(side_effect=[{"reply": "候选一"}, {"reply": "候选二"}])):
        first = await service.create_candidate(project_id="p-d", chapter_id="c-d", action="rewrite", selection_text="旧句子。")
        second = await service.regenerate_candidate(first["id"])
    assert second["id"] != first["id"]
    assert chapter_rewrites.get_rewrite(first["id"])["status"] == "proposed"
    service.reject_candidate(first["id"])
    assert chapter_rewrites.get_rewrite(first["id"])["status"] == "rejected"
    assert isolated_db.execute("SELECT content FROM chapters WHERE id='c-d'").fetchone()[0] == "旧句子。"


@pytest.mark.asyncio
async def test_accept_replaces_only_selected_text_and_is_one_shot(isolated_db):
    with patch.object(service.ai_service, "chat_reply", new=AsyncMock(return_value={"reply": "新句子"})):
        candidate = await service.create_candidate(project_id="p-d", chapter_id="c-d", action="rewrite", selection_text="旧句子。")
    accepted = service.accept_candidate(candidate["id"])
    assert accepted["status"] == "applied"
    assert isolated_db.execute("SELECT content FROM chapters WHERE id='c-d'").fetchone()[0] == "新句子"
    with pytest.raises(ValueError):
        service.accept_candidate(candidate["id"])


@pytest.mark.asyncio
async def test_accept_recomputes_project_word_count_and_syncs_fts(isolated_db):
    """T2：Accept 后必须与手工保存一致——重算项目总字数并同步 FTS 派生索引。"""
    isolated_db.execute(
        "INSERT INTO chapters VALUES ('c-d-2', 'p-d', 'C2', 2, '第二章正文。', 6, 'draft', '', '', '2026-01-01', '2026-01-01')"
    )
    isolated_db.execute("UPDATE projects SET current_word_count = 999999 WHERE id = 'p-d'")
    isolated_db.commit()

    with patch.object(service.ai_service, "chat_reply", new=AsyncMock(return_value={"reply": "独一无二的替换词"})):
        candidate = await service.create_candidate(project_id="p-d", chapter_id="c-d", action="rewrite", selection_text="旧句子。")
    # Accept 之前：正文与项目汇总都不变
    assert isolated_db.execute("SELECT content FROM chapters WHERE id='c-d'").fetchone()[0] == "旧句子。"
    assert isolated_db.execute("SELECT current_word_count FROM projects WHERE id='p-d'").fetchone()[0] == 999999

    accepted = service.accept_candidate(candidate["id"])

    chapter_total = isolated_db.execute(
        "SELECT COALESCE(SUM(word_count), 0) FROM chapters WHERE project_id = 'p-d'"
    ).fetchone()[0]
    project_total = isolated_db.execute(
        "SELECT current_word_count FROM projects WHERE id = 'p-d'"
    ).fetchone()[0]
    assert project_total == chapter_total
    assert project_total != 999999
    assert accepted["applied_content"] == "独一无二的替换词"
    assert accepted["status"] == "applied"
    # 派生 FTS 同步被触发（针对被接受的章节）
    assert ("p-d", "c-d") in isolated_db.fts_calls
    # 候选状态与正文一致：重复接受按现有契约拒绝
    assert isolated_db.execute("SELECT status FROM chapter_rewrites WHERE id=?", (candidate["id"],)).fetchone()[0] == "applied"
    with pytest.raises(ValueError):
        service.accept_candidate(candidate["id"])


@pytest.mark.asyncio
async def test_fts_sync_failure_does_not_lose_accepted_content(isolated_db, monkeypatch):
    """派生投影故障时：已接受正文与候选状态保持提交，不回滚、不丢失。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("fts backend down")

    monkeypatch.setattr(service.memory_fts_dao, "sync_chapter_fts", _boom)

    with patch.object(service.ai_service, "chat_reply", new=AsyncMock(return_value={"reply": "故障下也应保留"})):
        candidate = await service.create_candidate(project_id="p-d", chapter_id="c-d", action="rewrite", selection_text="旧句子。")
    accepted = service.accept_candidate(candidate["id"])

    assert accepted["status"] == "applied"
    assert accepted["applied_content"] == "故障下也应保留"
    assert isolated_db.execute("SELECT content FROM chapters WHERE id='c-d'").fetchone()[0] == "故障下也应保留"
    assert isolated_db.execute("SELECT status FROM chapter_rewrites WHERE id=?", (candidate["id"],)).fetchone()[0] == "applied"
