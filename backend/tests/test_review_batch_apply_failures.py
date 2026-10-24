"""智能审校「一键修改」批量执行的失败语义（2026-10-21）。

现场反馈：一键修改勾选几条，改到一半就整批失败，只能重新审校重来。

两个可跳过的成因（服务端**正文未动**）：
1. 模型未产生实际修改 / 返回空 / 调用失败（`copilot_no_change` 等）；
2. 创建候选超时（前端 60s 超时而后端写作角色 600s，整章改写经常超过 60s）。

这里锁住后端一侧：这些失败必须带机器可读码并且**不写正文**，
接口层再把它变成结构化 detail `{code, message}`，前端才能"跳过继续"。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import copilot as copilot_api
from app.services import copilot_candidate_service as service


class _IsolatedDb:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)


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
    conn.execute(
        "INSERT INTO chapters VALUES ('c-d', 'p-d', 'C', 1, '旧句子。', 4, 'draft', '', '', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    monkeypatch.setattr(service, "get_conn", lambda: conn)
    monkeypatch.setattr(service.projects_dao, "project_exists", lambda project_id: project_id == "p-d")
    monkeypatch.setattr(service.chapters_dao, "get_chapter", lambda chapter_id, project_id: {
        "id": "c-d", "project_id": "p-d", "content": "旧句子。"
    } if chapter_id == "c-d" and project_id == "p-d" else None)
    yield conn
    conn.close()


async def _create_with(reply: dict) -> ValueError:
    with patch.object(service.ai_service, "chat_reply", new=AsyncMock(return_value=reply)):
        with pytest.raises(ValueError) as excinfo:
            await service.create_candidate(
                project_id="p-d", chapter_id="c-d", action="rewrite", selection_text="旧句子。"
            )
    return excinfo.value


@pytest.mark.asyncio
async def test_no_change_carries_skippable_code_and_writes_nothing(isolated_db):
    """模型原样返回 → `copilot_no_change`，正文与候选都不变。"""
    error = await _create_with({"reply": "旧句子。"})
    assert getattr(error, "copilot_code", "") == "copilot_no_change"
    assert "未产生实际修改" in str(error)
    assert isolated_db.execute("SELECT content FROM chapters WHERE id='c-d'").fetchone()[0] == "旧句子。"
    assert isolated_db.execute("SELECT COUNT(*) FROM chapter_rewrites").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_empty_output_carries_skippable_code(isolated_db):
    error = await _create_with({"reply": "   "})
    assert getattr(error, "copilot_code", "") == "copilot_empty_output"


@pytest.mark.asyncio
async def test_provider_failure_carries_skippable_code(isolated_db):
    error = await _create_with({"reply": "", "mode": "failed"})
    assert getattr(error, "copilot_code", "") == "copilot_provider_failed"


@pytest.mark.asyncio
async def test_stale_text_failure_has_no_skippable_code(isolated_db):
    """正文已更新属于**必须停下**的失败，不能带可跳过码。"""
    with pytest.raises(ValueError) as excinfo:
        await service.create_candidate(
            project_id="p-d", chapter_id="c-d", action="rewrite",
            selection_text="旧句子。", current_text="别的正文",
        )
    assert getattr(excinfo.value, "copilot_code", "") == ""


def test_api_returns_structured_detail_for_skippable_code():
    """接口层把带码的失败变成 `{code, message}`，前端 ApiError.code 才能拿到。"""
    error = ValueError("模型未产生实际修改，未创建候选，原文保持不变。")
    error.copilot_code = "copilot_no_change"

    async def _boom(**_kwargs):
        raise error

    with patch.object(copilot_api.candidates, "create_candidate", new=_boom), \
         patch.object(copilot_api, "require_project_owner", lambda *_a, **_k: None):
        import asyncio

        request = type("R", (), {"headers": {}})()
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(copilot_api.create_candidate(
                copilot_api.CandidateRequest(
                    projectId="p-d", chapterId="c-d", action="rewrite",
                    selectionText="旧句子。", currentText="旧句子。",
                ),
                request,
            ))
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "copilot_no_change"
    assert excinfo.value.status_code == 400
