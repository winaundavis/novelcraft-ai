"""P2-3 参考书档案 —— DAO / 服务 / API 单测。

覆盖：
- 建表：真实 schema（patch DB_PATH 后 init_db）下 reference_works 存在；
- 建档：伪章节切分 + 采样 + **本地确定性指标**（不来自模型）+ LLM 抽规则一次调用；
- 诚实降级：LLM 抛错 → status='degraded' + 中文原因，规则仍可用；
- 不静默截断：超长文本按上限存储并如实标注 truncated/char_count/stored_chars；
- 严格隔离：参考档案**永不写 style_bindings**（与文风档案分表）；
- API：400（空文本/超长）、列表**不含原文**、单条含原文、改名、删除、重新拆解、404。

所有 LLM 调用均打桩（AsyncMock）；数据库用临时文件 + 真实 init_db schema。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.dao import projects as projects_dao
from app.dao import reference_works as reference_dao
from app.dao import style_dao
from app.llm.provider import LLMConfigurationError
from app.services import reference_work_service
from app.services.style_engine import extraction
from main import app


PID = "proj-ref-1"
OTHER_PID = "proj-ref-2"
# 档案归「作者」（owner），跨项目共享；projects.owner_id 的库默认值是 local-dev
OWNER = "local-dev"
NOW = "2026-10-06T00:00:00"

SAMPLE_TEXT = """第一章 夜雨
雨敲在瓦上。阿七把刀放在膝上，听楼下脚步。他没有说话，等那人上楼。
“把东西给我。”来人开口。
阿七抬头，看清了对方袖口的银线。那是宗门的人。

第二章 断桥
桥断了。阿七退到桥头，才发觉自己手上还攥着那半张符纸。
“你早该扔掉它。”老人说。
阿七摇头。他把符纸收进怀里，像收起一块烧红的炭。

第三章 银线
银线在灯下闪了一下。阿七终于明白，宗门早已知道他来过。
他没有回头，只是把刀横在身前，等着对方先动。
"""


def _fake_rules(**overrides):
    """LLM 抽规则的替身（字段与 extraction.extract_rules 一致）。"""
    base = {
        "narrative_rules": [{"rule": "用具体动作推进场景", "evidence": "把刀放在膝上"}],
        "character_rules": [{"rule": "用克制反应塑造人物", "evidence": "他没有说话"}],
        "language_rules": [{"rule": "短句为主，长句用于停顿", "evidence": "桥断了。"}],
        "rhythm_rules": [{"rule": "对话后立刻接动作", "evidence": "抬头，看清"}],
        "anti_ai_rules": [{"rule": "不要解释情绪", "evidence": ""}],
        "strengths": ["动作干净"],
        "weaknesses": ["信息密度偏低"],
        "report": "整体是克制的动作流叙事。",
    }
    base.update(overrides)
    return base


@pytest.fixture
def ref_db(tmp_path, monkeypatch):
    """真实 schema（含新增的 reference_works 表）+ 一个项目。"""
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "reference.db")
    app_db.init_db()
    projects_dao.create_project({
        "id": PID, "name": "参考档案测试项目", "premise": "测试。",
        "target_word_count": 100000, "current_word_count": 0, "status": "writing",
        "created_at": NOW, "updated_at": NOW,
        "outline": json.dumps({"mainSummary": "测试。", "nodes": []}),
        "style_config": "{}", "world_rules": "[]",
    })
    yield


def test_reference_works_table_exists_in_real_schema(ref_db):
    with app_db.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reference_works'"
        ).fetchone()
    assert row is not None


# ── 建档：指标本地算，AI 只抽规则 ──────────────────────

@pytest.mark.asyncio
async def test_create_uses_local_metrics_and_llm_rules_once(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ) as mock_extract:
        record = await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT, name="夜雨三章")

    mock_extract.assert_awaited_once()
    assert record["name"] == "夜雨三章"
    assert record["status"] == "completed"
    assert record["status_reason"] == ""
    assert record["llm_used"] is True
    # 指标必须是**本地确定性算出来的**，与同一段文本直接算的结果一致
    expected_metrics = extraction.compute_metrics(
        reference_work_service._sample_text(SAMPLE_TEXT)[0]
    )
    assert record["metrics"]["avgSentenceLength"] == expected_metrics["avgSentenceLength"]
    assert record["metrics"]["avgSentenceLength"] > 0
    # 规则来自（被打桩的）AI
    assert record["language_rules"][0]["rule"] == "短句为主，长句用于停顿"
    assert record["report"] == "整体是克制的动作流叙事。"


@pytest.mark.asyncio
async def test_create_defaults_name_from_first_line(ref_db):
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        record = await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT)
    assert record["name"] == "第一章 夜雨"  # 首行去掉 # 后截 30 字


@pytest.mark.asyncio
async def test_create_reports_sampling_honestly(ref_db):
    long_text = "第一段。" * 12000  # 约 6 万字，触发采样
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        record = await reference_work_service.create_reference_work(PID, OWNER, long_text)
    assert record["char_count"] == len(long_text)          # 全文长度如实上报
    assert record["sample_chars"] <= extraction.MAX_SAMPLE_CHARS
    assert record["sample_chars"] < record["char_count"]    # 确实采样了，不是全量塞进 prompt
    assert record["truncated"] is False                     # 6 万字未超过存储上限


@pytest.mark.asyncio
async def test_create_marks_truncation_when_storage_capped(ref_db, monkeypatch):
    monkeypatch.setattr(reference_work_service, "MAX_STORED_CHARS", 200)
    long_text = "雨。" * 400
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        record = await reference_work_service.create_reference_work(PID, OWNER, long_text)
    assert record["truncated"] is True
    assert record["stored_chars"] == 200
    assert record["char_count"] == len(long_text)
    assert len(record["raw_text"]) == 200


# ── 诚实降级 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_failure_degrades_with_chinese_reason(ref_db):
    with patch.object(
        extraction, "extract_rules",
        new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
    ):
        record = await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT)
    assert record["status"] == "degraded"
    assert record["llm_used"] is False
    assert "模型设置" in record["status_reason"]
    # 降级后仍然有可用的规则（按本地统计生成），不是空壳
    assert record["metrics"]["totalChars"] > 0
    assert record["narrative_rules"] or record["language_rules"]


@pytest.mark.asyncio
async def test_reanalyze_reuses_stored_text_and_keeps_name(ref_db):
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        created = await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT, name="原名")
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules(report="重跑后的报告"))
    ):
        updated = await reference_work_service.reanalyze_reference_work(OWNER, created["id"])
    assert updated["name"] == "原名"  # 改名与备注不受重新拆解影响
    assert updated["report"] == "重跑后的报告"
    assert updated["status"] == "completed"


# ── 严格隔离：参考档案绝不进文风绑定 ──────────────────

@pytest.mark.asyncio
async def test_reference_work_never_touches_style_binding(ref_db):
    assert style_dao.get_binding(PID) is None
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        created = await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT)
    assert style_dao.get_binding(PID) is None  # 建档不动本项目当前生效文风
    assert style_dao.list_profiles(PID) == []  # 也不写进文风档案表
    diagnosis = reference_work_service.assert_isolated_from_style_binding(PID, OWNER)
    assert diagnosis["leaked"] is False
    assert diagnosis["bound_profile_id"] == ""
    assert created["id"] in diagnosis["reference_ids"]


# ── 切分成伪章节 ──────────────────────────────────────

def test_split_pseudo_chapters_by_heading():
    chapters = reference_work_service.split_pseudo_chapters(SAMPLE_TEXT)
    assert len(chapters) == 3
    assert all(chunk["content"].strip() for chunk in chapters)


def test_split_pseudo_chapters_falls_back_to_single_chunk():
    chapters = reference_work_service.split_pseudo_chapters("只有一段没有章标题的文字。")
    assert len(chapters) == 1


# ── API ───────────────────────────────────────────────

def test_api_create_list_and_hide_raw_text(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        resp = client.post(
            f"/api/projects/{PID}/reference-works",
            json={"name": "夜雨", "text": SAMPLE_TEXT, "sourceNote": "来自某短篇"},
        )
        assert resp.status_code == 200
        created = resp.json()["data"]
        assert created["name"] == "夜雨"
        assert created["source_note"] == "来自某短篇"
        assert created["raw_text"] == SAMPLE_TEXT.strip()  # 建完返回可回看原文（首尾空白已去掉）

        listing = client.get(f"/api/projects/{PID}/reference-works")
        assert listing.status_code == 200
        items = listing.json()["data"]["items"]
        assert len(items) == 1
        assert "raw_text" not in items[0]  # 列表刻意不带原文（可能几十万字）
        assert items[0]["name"] == "夜雨"

        one = client.get(f"/api/projects/{PID}/reference-works/{created['id']}")
        assert one.status_code == 200
        assert one.json()["data"]["raw_text"] == SAMPLE_TEXT.strip()


def test_api_rejects_empty_and_oversized_text(ref_db):
    with TestClient(app) as client:
        empty = client.post(f"/api/projects/{PID}/reference-works", json={"text": "   "})
        assert empty.status_code == 400
        oversized = client.post(
            f"/api/projects/{PID}/reference-works",
            json={"text": "雨。" * (reference_work_service.MAX_INPUT_CHARS // 2 + 10)},
        )
        assert oversized.status_code == 400


def test_api_rename_delete_and_404(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        created = client.post(
            f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT}
        ).json()["data"]
        rid = created["id"]

        renamed = client.patch(
            f"/api/projects/{PID}/reference-works/{rid}",
            json={"name": "改过的名字", "sourceNote": "换备注"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["data"]["name"] == "改过的名字"
        assert renamed.json()["data"]["source_note"] == "换备注"

        assert client.delete(f"/api/projects/{PID}/reference-works/{rid}").status_code == 200
        assert client.get(f"/api/projects/{PID}/reference-works/{rid}").status_code == 404
        assert client.patch(
            f"/api/projects/{PID}/reference-works/{rid}", json={"name": "x"}
        ).status_code == 404


def test_api_reanalyze_endpoint(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        created = client.post(
            f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT}
        ).json()["data"]
        with patch.object(
            extraction, "extract_rules",
            new=AsyncMock(return_value=_fake_rules(report="第二次拆解")),
        ):
            again = client.post(
                f"/api/projects/{PID}/reference-works/{created['id']}/reanalyze"
            )
        assert again.status_code == 200
        assert again.json()["data"]["report"] == "第二次拆解"
        assert client.post(
            f"/api/projects/{PID}/reference-works/not-exist/reanalyze"
        ).status_code == 404


def test_api_other_project_isolation(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        created = client.post(
            f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT}
        ).json()["data"]
        # 另一个项目看不到（require_project_owner 对未知项目返回 404）
        assert client.get(f"/api/projects/{OTHER_PID}/reference-works").status_code == 404
        assert client.get(
            f"/api/projects/{OTHER_PID}/reference-works/{created['id']}"
        ).status_code == 404


@pytest.mark.asyncio
async def test_dao_delete_by_project_cleans_up(ref_db):
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT)
    assert len(reference_dao.list_reference_works(OWNER)) == 1
    reference_dao.delete_reference_works_by_project(PID)
    assert reference_dao.list_reference_works(OWNER) == []


# ═══════════════════════════════════════════════════════════
# P2：把「正在参考的那一份」注入创作上下文
# ═══════════════════════════════════════════════════════════

async def _create_archive(name: str = "夜雨") -> dict:
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        return await reference_work_service.create_reference_work(PID, OWNER, SAMPLE_TEXT, name=name)


@pytest.mark.asyncio
async def test_render_block_wording_and_content(ref_db):
    created = await _create_archive()
    assert reference_work_service.set_active_reference(PID, OWNER, created["id"]) is not None

    block, source_id = reference_work_service.render_reference_block(PID)
    assert source_id == created["id"]
    # 措辞是产品底线：必须写明"只作表达参考、不覆盖本书风格与设定"
    assert "参考写法" in block
    assert "夜雨" in block
    assert "仅作表达参考，不覆盖本书既有风格与设定" in block
    assert "语言：" in block
    assert "参考统计" in block  # 本地实测指标
    assert len(block) <= reference_work_service.REFERENCE_BLOCK_MAX_CHARS

    # 找得到当前启用项，且**没有**碰文风绑定
    assert (reference_work_service.get_active_reference(PID) or {})["id"] == created["id"]
    assert style_dao.get_binding(PID) is None
    assert reference_work_service.assert_isolated_from_style_binding(PID, OWNER)["leaked"] is False


@pytest.mark.asyncio
async def test_disabled_reference_injects_nothing(ref_db):
    created = await _create_archive()
    reference_work_service.set_active_reference(PID, OWNER, created["id"])
    assert reference_work_service.render_reference_block(PID)[0] != ""

    reference_work_service.set_active_reference(PID, OWNER, "")
    assert reference_work_service.render_reference_block(PID) == ("", "")
    assert reference_work_service.get_active_reference(PID) is None


@pytest.mark.asyncio
async def test_project_context_carries_reference_block(ref_db):
    """真实行为验证：助手/续写用的项目上下文里确实出现了参考写法块。"""
    from app.services import project_context_service

    created = await _create_archive()
    reference_work_service.set_active_reference(PID, OWNER, created["id"])

    enabled = project_context_service.assemble_project_context(project_id=PID, query="继续写这一章")
    assert "参考写法" in enabled["text"]
    assert "仅作表达参考" in enabled["text"]
    assert "reference_style" in [block["key"] for block in enabled["blocks"]]

    reference_work_service.set_active_reference(PID, OWNER, "")
    disabled = project_context_service.assemble_project_context(project_id=PID, query="继续写这一章")
    assert "参考写法" not in disabled["text"]
    assert "reference_style" not in [block["key"] for block in disabled["blocks"]]


@pytest.mark.asyncio
async def test_batch_generation_context_carries_reference_block(ref_db):
    """整书自动创作走的是另一条上下文构建路径，同样要带上参考写法。"""
    from app.services import novel_generation_service as ngs

    created = await _create_archive()
    reference_work_service.set_active_reference(PID, OWNER, created["id"])
    nodes = [{"id": "c1", "order": 1, "type": "chapter", "title": "第一章", "summary": "开篇"}]

    enabled = ngs._build_memory_context(project_id=PID, chapter_nodes=nodes, current_chapter_id="c1")
    assert "参考写法" in enabled["reference_style"]

    reference_work_service.set_active_reference(PID, OWNER, "")
    disabled = ngs._build_memory_context(project_id=PID, chapter_nodes=nodes, current_chapter_id="c1")
    assert disabled["reference_style"] == ""


# ── API：启用 / 停用 / 删除当前启用项 ─────────────────

def test_api_activate_and_deactivate(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        created = client.post(f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT}).json()["data"]
        assert client.get(f"/api/projects/{PID}/reference-works").json()["data"]["activeReferenceId"] == ""

        activated = client.put(
            f"/api/projects/{PID}/reference-works/active", json={"referenceId": created["id"]}
        )
        assert activated.status_code == 200
        assert activated.json()["data"]["activeReferenceId"] == created["id"]
        listing = client.get(f"/api/projects/{PID}/reference-works").json()["data"]
        assert listing["activeReferenceId"] == created["id"]

        cleared = client.put(f"/api/projects/{PID}/reference-works/active", json={"referenceId": ""})
        assert cleared.status_code == 200
        assert client.get(f"/api/projects/{PID}/reference-works").json()["data"]["activeReferenceId"] == ""

        missing = client.put(
            f"/api/projects/{PID}/reference-works/active", json={"referenceId": "not-exist"}
        )
        assert missing.status_code == 404


def test_api_deleting_active_archive_clears_binding(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        created = client.post(f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT}).json()["data"]
        client.put(f"/api/projects/{PID}/reference-works/active", json={"referenceId": created["id"]})
        assert client.delete(f"/api/projects/{PID}/reference-works/{created['id']}").status_code == 200
        listing = client.get(f"/api/projects/{PID}/reference-works").json()["data"]
        assert listing["activeReferenceId"] == ""
        assert reference_work_service.render_reference_block(PID) == ("", "")


# ═══════════════════════════════════════════════════════════
# P2-3b：个人素材库（跨项目共享，但仍严格隔离于文风档案）
# ═══════════════════════════════════════════════════════════

def _create_second_project(project_id: str = OTHER_PID, owner_id: str = OWNER) -> None:
    projects_dao.create_project({
        "id": project_id, "name": "第二本书", "premise": "测试。",
        "target_word_count": 100000, "current_word_count": 0, "status": "writing",
        "created_at": NOW, "updated_at": NOW, "owner_id": owner_id,
        "outline": json.dumps({"mainSummary": "测试。", "nodes": []}),
        "style_config": "{}", "world_rules": "[]",
    })


@pytest.mark.asyncio
async def test_library_is_shared_across_projects(ref_db):
    """在 A 项目收集的档案，B 项目也看得到（个人素材库），并标明来源项目。"""
    _create_second_project()
    created = await _create_archive(name="夜雨")

    from_other_project = reference_dao.list_reference_works(OWNER)
    assert [item["id"] for item in from_other_project] == [created["id"]]

    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())), TestClient(app) as client:
        listing = client.get(f"/api/projects/{OTHER_PID}/reference-works").json()["data"]["items"]
    assert len(listing) == 1
    assert listing[0]["name"] == "夜雨"
    assert listing[0]["source_project_name"] == "参考档案测试项目"  # 来源标注（A 项目）
    assert "raw_text" not in listing[0]


@pytest.mark.asyncio
async def test_cross_project_reference_can_be_used_and_injected(ref_db):
    """B 项目可以启用 A 项目收集的档案，并且注入到 B 的创作上下文。"""
    from app.services import project_context_service

    _create_second_project()
    created = await _create_archive(name="夜雨")

    # B 项目启用它（跨项目）
    assert reference_work_service.set_active_reference(OTHER_PID, OWNER, created["id"]) is not None
    block, source_id = reference_work_service.render_reference_block(OTHER_PID)
    assert source_id == created["id"]
    assert "夜雨" in block
    assert "参考写法" in project_context_service.assemble_project_context(
        project_id=OTHER_PID, query="继续写这一章"
    )["text"]
    # 仍不碰文风绑定
    assert style_dao.get_binding(OTHER_PID) is None


@pytest.mark.asyncio
async def test_other_owner_has_empty_library(ref_db):
    """别人的素材库看不到我的档案（owner 隔离）。"""
    _create_second_project("proj-other-owner", owner_id="someone-else")
    created = await _create_archive()
    assert reference_dao.list_reference_works("someone-else") == []
    assert reference_dao.get_reference_work("someone-else", created["id"]) is None
    assert reference_work_service.set_active_reference("proj-other-owner", "someone-else", created["id"]) is None


@pytest.mark.asyncio
async def test_deleting_archive_clears_bindings_in_other_projects(ref_db):
    _create_second_project()
    created = await _create_archive()
    reference_work_service.set_active_reference(OTHER_PID, OWNER, created["id"])
    assert reference_dao.get_binding(OTHER_PID) is not None

    assert reference_dao.delete_reference_work(OWNER, created["id"]) is True
    assert reference_dao.get_binding(OTHER_PID) is None  # 绑定被一并清理，不留悬空
    assert reference_work_service.render_reference_block(OTHER_PID) == ("", "")


@pytest.mark.asyncio
async def test_deleting_project_keeps_personal_library(ref_db):
    """钉住「删项目不删素材」：参考档案是个人资产，不该随项目消失。

    project_id 保留来源标识，归属由 owner_id 决定；来源项目删除不删除素材。
    """
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())):
        await _create_archive(name="要留下来的档案")

    assert projects_dao.delete_project(PID) == 1
    remaining = reference_dao.list_reference_works(OWNER)
    assert [item["name"] for item in remaining] == ["要留下来的档案"]
    assert remaining[0]["project_id"] == PID           # 来源标注保留
    assert remaining[0]["source_project_name"] == ""   # 项目没了，来源名退化为空（界面需容忍）


def test_legacy_rows_get_owner_id_backfilled(ref_db):
    """老库兼容：owner_id 为空的旧行在启动迁移时按项目 owner 回填。"""
    import sqlite3

    with app_db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reference_works(id, project_id, owner_id, name, raw_text, created_at, updated_at)
            VALUES ('legacy-1', ?, '', '老档案', '正文', ?, ?)
            """,
            (PID, NOW, NOW),
        )
    app_db.init_db()  # 幂等迁移：应把 owner_id 回填为项目 owner
    with app_db.get_conn() as conn:
        row = conn.execute(
            "SELECT owner_id FROM reference_works WHERE id = 'legacy-1'"
        ).fetchone()
    assert row is not None and row["owner_id"] == OWNER
    assert "legacy-1" in [item["id"] for item in reference_dao.list_reference_works(OWNER)]


# ═══════════════════════════════════════════════════════════
# P3b：多份参考 + 强度权重 + 标签
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_multiple_references_ordered_by_weight(ref_db):
    low = await _create_archive(name="低强度那本")
    high = await _create_archive(name="高强度那本")
    assert reference_work_service.set_active_references(
        PID, OWNER, [(low["id"], 3), (high["id"], 9)]
    ) is not None

    active = reference_work_service.get_active_references(PID)
    assert [item["name"] for item in active] == ["高强度那本", "低强度那本"]  # 强度降序
    assert [item["weight"] for item in active] == [9, 3]

    block, used = reference_work_service.render_reference_blocks(PID)
    assert "高强度那本" in block and "低强度那本" in block
    assert block.index("高强度那本") < block.index("低强度那本")  # 注入顺序也按强度
    assert [item["name"] for item in used] == ["高强度那本", "低强度那本"]


@pytest.mark.asyncio
async def test_budget_drops_lowest_weight_first(ref_db, monkeypatch):
    """总预算不够时先丢**强度低**的（这正是"权重"的实际作用）。"""
    low = await _create_archive(name="会被丢掉的那本")
    high = await _create_archive(name="会保留的那本")
    reference_work_service.set_active_references(PID, OWNER, [(low["id"], 2), (high["id"], 9)])

    single, _ = reference_work_service.render_reference_blocks(PID)
    monkeypatch.setattr(
        reference_work_service, "REFERENCE_BLOCK_TOTAL_MAX_CHARS", len(single.split("\n\n")[0]) + 5
    )
    block, used = reference_work_service.render_reference_blocks(PID)
    assert [item["name"] for item in used] == ["会保留的那本"]
    assert "会保留的那本" in block
    assert "会被丢掉的那本" not in block


@pytest.mark.asyncio
async def test_replace_semantics_and_clear(ref_db):
    first = await _create_archive(name="第一份")
    second = await _create_archive(name="第二份")

    reference_work_service.set_active_references(PID, OWNER, [(first["id"], 6), (second["id"], 6)])
    reference_work_service.set_active_references(PID, OWNER, [(second["id"], 6)])
    block, _ = reference_work_service.render_reference_blocks(PID)
    assert "第二份" in block and "第一份" not in block

    reference_work_service.set_active_references(PID, OWNER, [])
    assert reference_work_service.render_reference_blocks(PID) == ("", [])
    assert reference_dao.list_bindings(PID) == []


@pytest.mark.asyncio
async def test_rejects_whole_set_when_one_is_not_mine(ref_db):
    """只要有一条不是自己的档案，就**整体拒绝**（不做半途写入）。"""
    mine = await _create_archive(name="我的")
    reference_work_service.set_active_references(PID, OWNER, [(mine["id"], 6)])

    assert reference_work_service.set_active_references(
        PID, OWNER, [(mine["id"], 9), ("not-exist", 6)]
    ) is None
    # 原有绑定保持不变（没有被改成 9，也没有把不存在的写进去）
    active = reference_work_service.get_active_references(PID)
    assert [(item["id"], item["weight"]) for item in active] == [(mine["id"], 6)]


@pytest.mark.asyncio
async def test_weight_is_clamped(ref_db):
    created = await _create_archive()
    reference_work_service.set_active_references(PID, OWNER, [(created["id"], 99)])
    assert reference_work_service.get_active_references(PID)[0]["weight"] == 10
    reference_work_service.set_active_references(PID, OWNER, [(created["id"], 0)])
    assert reference_work_service.get_active_references(PID)[0]["weight"] == 1


@pytest.mark.asyncio
async def test_api_replace_active_set_with_weights(ref_db):
    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())), TestClient(app) as client:
        first = client.post(f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT, "name": "A"}).json()["data"]
        second = client.post(f"/api/projects/{PID}/reference-works", json={"text": SAMPLE_TEXT, "name": "B"}).json()["data"]

        resp = client.put(
            f"/api/projects/{PID}/reference-works/active",
            json={"items": [{"referenceId": first["id"], "weight": 3}, {"referenceId": second["id"], "weight": 9}]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [item["id"] for item in data["activeReferences"]] == [second["id"], first["id"]]
        assert data["activeReferenceId"] == second["id"]  # 兼容字段 = 强度最高那份

        listed = client.get(f"/api/projects/{PID}/reference-works").json()["data"]
        assert [item["weight"] for item in listed["activeReferences"]] == [9, 3]

        # 某一条不存在 → 404，且原有集合不变
        bad = client.put(
            f"/api/projects/{PID}/reference-works/active",
            json={"items": [{"referenceId": "not-exist", "weight": 5}]},
        )
        assert bad.status_code == 404
        still = client.get(f"/api/projects/{PID}/reference-works").json()["data"]["activeReferences"]
        assert [item["id"] for item in still] == [second["id"], first["id"]]

        # 整体清空
        cleared = client.put(f"/api/projects/{PID}/reference-works/active", json={"items": []})
        assert cleared.json()["data"]["activeReferences"] == []


def test_tags_are_normalized_and_updatable(ref_db):
    with patch.object(
        extraction, "extract_rules", new=AsyncMock(return_value=_fake_rules())
    ), TestClient(app) as client:
        created = client.post(
            f"/api/projects/{PID}/reference-works",
            json={"text": SAMPLE_TEXT, "name": "带标签", "tags": "对话, 节奏、对话  "},
        ).json()["data"]
        assert created["tags"] == ["对话", "节奏"]  # 分隔符混用 + 去重

        patched = client.patch(
            f"/api/projects/{PID}/reference-works/{created['id']}",
            json={"tags": ["悬疑", "长句"]},
        ).json()["data"]
        assert patched["tags"] == ["悬疑", "长句"]

        listed = client.get(f"/api/projects/{PID}/reference-works").json()["data"]["items"]
        assert listed[0]["tags"] == ["悬疑", "长句"]


def test_tag_sanitizer_limits_and_ignores_junk():
    from app.dao.reference_works import sanitize_tags

    assert sanitize_tags(None) == []
    assert sanitize_tags("") == []
    assert sanitize_tags(123) == []
    assert sanitize_tags(["", "  ", "ok"]) == ["ok"]
    assert sanitize_tags(["a", "a", "b"]) == ["a", "b"]
    assert sanitize_tags("x" * 50)[0] == "x" * 20  # 单标签截断
    assert len(sanitize_tags([f"t{i}" for i in range(30)])) == 12  # 数量上限


def test_legacy_single_binding_table_is_upgraded(ref_db):
    """老形状（UNIQUE(project_id)，无 weight）在启动时被重建为「可多份 + 强度」。"""
    with app_db.get_conn() as conn:
        conn.execute("DROP TABLE reference_bindings")
        conn.execute(
            """
            CREATE TABLE reference_bindings (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                reference_id TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'project',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO reference_bindings(id, project_id, reference_id, scope, created_at, updated_at)"
            " VALUES ('old-b1', ?, 'rw-old', 'project', ?, ?)",
            (PID, NOW, NOW),
        )

    app_db.init_db()  # 迁移：重建表 + 搬老行

    with app_db.get_conn() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(reference_bindings)").fetchall()]
        rows = conn.execute("SELECT reference_id, weight FROM reference_bindings").fetchall()
    assert "weight" in columns
    assert [(row["reference_id"], row["weight"]) for row in rows] == [("rw-old", 6)]

    # 新形状允许同一项目多份
    reference_dao.replace_bindings(PID, [("rw-a", 3), ("rw-b", 9)])
    assert [item["reference_id"] for item in reference_dao.list_bindings(PID)] == ["rw-b", "rw-a"]
