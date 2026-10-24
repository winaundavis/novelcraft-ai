"""建议2 全文问答（DeepSeek 前缀缓存）—— fulltext_service / API 单测。

覆盖：
- build_fulltext：按 (chapter_order, created_at) 稳定顺序拼接、跳过空章、截断保护、
  空书/缺项目抛 FulltextEmptyError、token 估算；
- ask_fulltext：稳定前缀组装（系统提示 → 全书正文 → 问题，正文恒在问题前）、
  LLM mock 返回回答、LLM 失败降级（degraded=True 且统计字段保留）、空问题抛错；
- API：POST /fulltext-ask（200 / 空书 400 / 缺项目 404 / LLM 失败降级 200）、
  GET /fulltext-stats（200 / 空书 400 / 缺项目 404）。

所有 LLM 调用均 mock（秒级完成）；数据库用独立内存 SQLite 隔离。
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.dao import chapters as chapters_dao
from app.dao import projects as projects_dao
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.llm.roles import ROLE_FULLTEXT
from app.services import fulltext_service
from app.services.fulltext_service import (
    FulltextEmptyError,
    ask_fulltext,
    build_fulltext,
    fulltext_stats,
)
from main import app


# ── 内存库 Schema（与 db.py 对齐） ─────────────────────

PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    genre TEXT NOT NULL DEFAULT 'xuanhuan',
    target_word_count INTEGER NOT NULL DEFAULT 300000,
    current_word_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planning',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    style_config TEXT NOT NULL DEFAULT '{}',
    outline TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    is_starred INTEGER NOT NULL DEFAULT 0,
    premise TEXT NOT NULL DEFAULT '',
    target_chapters INTEGER NOT NULL DEFAULT 30,
    characters TEXT NOT NULL DEFAULT '[]',
    locations TEXT NOT NULL DEFAULT '[]',
    world_rules TEXT NOT NULL DEFAULT '[]',
    import_analysis TEXT,
    inspiration_fragments TEXT NOT NULL DEFAULT '[]'
);
"""

CHAPTERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    volume_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    chapter_order INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    summary TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _build_memory_db() -> sqlite3.Connection:
    # check_same_thread=False：TestClient 在独立线程中处理请求，需允许跨线程访问
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PROJECTS_SCHEMA + CHAPTERS_SCHEMA)
    conn.commit()
    return conn


def _get_conn_factory(conn: sqlite3.Connection):
    def _get_conn() -> sqlite3.Connection:
        return conn
    return _get_conn


# ── 通用 Fixtures ─────────────────────────────────────

@pytest.fixture
def env():
    """服务级隔离库：projects + chapters 表，patch 两个 DAO 的 get_conn。"""
    conn = _build_memory_db()
    get_conn = _get_conn_factory(conn)
    with patch.object(projects_dao, "get_conn", get_conn), patch.object(
        chapters_dao, "get_conn", get_conn
    ):
        yield conn
    conn.close()


@pytest.fixture
def api_env():
    """API 级隔离库：patch 两个 DAO 的 get_conn（TestClient 处理请求时生效）。"""
    conn = _build_memory_db()
    get_conn = _get_conn_factory(conn)
    with patch.object(projects_dao, "get_conn", get_conn), patch.object(
        chapters_dao, "get_conn", get_conn
    ):
        yield conn
    conn.close()


# ── 测试数据工具 ──────────────────────────────────────

PROJECT_ID = "proj-fulltext-1"
OTHER_PROJECT_ID = "proj-fulltext-2"


def _insert_project(conn: sqlite3.Connection, project_id: str = PROJECT_ID) -> None:
    conn.execute(
        """
        INSERT INTO projects
            (id, name, genre, status, created_at, updated_at, style_config, target_chapters)
        VALUES (?, '测试项目', 'xuanhuan', 'drafting', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z', '{}', 5)
        """,
        (project_id,),
    )
    conn.commit()


def _insert_chapter(
    conn: sqlite3.Connection,
    project_id: str = PROJECT_ID,
    *,
    chapter_id: str = "ch1",
    order: int = 1,
    title: str = "第一章",
    content: str = "正文内容",
    created_at: str = "2026-08-10T00:00:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO chapters
            (id, project_id, volume_id, title, chapter_order, content, word_count,
             status, summary, notes, created_at, updated_at)
        VALUES (?, ?, '', ?, ?, ?, ?, 'completed', '', '', ?, ?)
        """,
        (chapter_id, project_id, title, order, content, len(content), created_at, created_at),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════
# ① build_fulltext：顺序稳定 / 跳空 / 截断 / 空书
# ═══════════════════════════════════════════════════════════

class TestBuildFulltext:
    def test_empty_book_raises(self, env):
        """无章节 → FulltextEmptyError。"""
        _insert_project(env)
        with pytest.raises(FulltextEmptyError):
            build_fulltext(PROJECT_ID)

    def test_project_missing_raises(self, env):
        """项目不存在 → FulltextEmptyError。"""
        with pytest.raises(FulltextEmptyError):
            build_fulltext(OTHER_PROJECT_ID)

    def test_all_empty_chapters_raises(self, env):
        """章节均无正文 → FulltextEmptyError。"""
        _insert_project(env)
        _insert_chapter(env, content="   ")
        with pytest.raises(FulltextEmptyError):
            build_fulltext(PROJECT_ID)

    def test_order_stable_regardless_of_insert_order(self, env):
        """章节乱序插入，按 chapter_order 稳定拼接（前缀缓存依赖稳定前缀）。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c2", order=2, title="第二章", content="乙")
        _insert_chapter(env, chapter_id="c3", order=3, title="第三章", content="丙")
        _insert_chapter(env, chapter_id="c1", order=1, title="第一章", content="甲")
        result = build_fulltext(PROJECT_ID)
        fulltext = result["fulltext"]
        assert fulltext.index("## 第1章 第一章") < fulltext.index("## 第2章 第二章")
        assert fulltext.index("## 第2章 第二章") < fulltext.index("## 第3章 第三章")
        assert fulltext.index("甲") < fulltext.index("乙") < fulltext.index("丙")
        assert result["chapters"] == 3
        assert result["truncated"] is False
        assert result["chars"] > 0
        assert result["estimated_tokens"] >= 1

    def test_same_order_sorted_by_created_at(self, env):
        """同 chapter_order 时按 created_at 升序（次要排序键稳定）。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="later", order=1, title="后", content="后", created_at="2026-08-10T02:00:00Z")
        _insert_chapter(env, chapter_id="earlier", order=1, title="前", content="前", created_at="2026-08-10T01:00:00Z")
        result = build_fulltext(PROJECT_ID)
        assert result["fulltext"].index("前") < result["fulltext"].index("后")

    def test_skips_empty_chapters(self, env):
        """空正文章节不参与拼接。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c1", order=1, title="有内容", content="正文甲")
        _insert_chapter(env, chapter_id="c2", order=2, title="空章", content="")
        result = build_fulltext(PROJECT_ID)
        assert "空章" not in result["fulltext"]
        assert result["chapters"] == 1
        assert "正文甲" in result["fulltext"]

    def test_truncation_protection(self, env):
        """超限时截断并标记 truncated（保留稳定前缀的前缀部分）。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c1", order=1, title="第一章", content="甲" * 100)
        _insert_chapter(env, chapter_id="c2", order=2, title="第二章", content="乙" * 100)
        result = build_fulltext(PROJECT_ID, max_chars=120)
        assert result["truncated"] is True
        assert "第二章" not in result["fulltext"]
        assert "第一章" in result["fulltext"]

    def test_max_chars_respects_settings_default(self, env, monkeypatch):
        """未传 max_chars 时使用 settings 默认（600K 内不截断）。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c1", order=1, title="第一章", content="正文")
        monkeypatch.setattr(fulltext_service.settings, "fulltext_max_chars", 600000)
        result = build_fulltext(PROJECT_ID)
        assert result["truncated"] is False


# ═══════════════════════════════════════════════════════════
# ② ask_fulltext：稳定前缀组装 / LLM mock / 降级
# ═══════════════════════════════════════════════════════════

class TestAskFulltext:
    @pytest.mark.asyncio
    async def test_asks_with_stable_prefix(self, env):
        """稳定前缀：系统提示 → 全书正文 → 问题；role=ROLE_FULLTEXT。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c1", order=1, title="第一章", content="主角在第一章捡到一枚铜铃。")

        captured: dict = {}

        async def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            captured["role"] = kwargs.get("role")
            captured["max_tokens"] = kwargs.get("max_tokens")
            return "铜铃出现在第一章，是主角在河滩捡到的。"

        with patch.object(fulltext_service.llm, "chat", new=fake_chat):
            result = await ask_fulltext(PROJECT_ID, "铜铃在哪里出现的？")

        assert captured["role"] == ROLE_FULLTEXT
        assert captured["max_tokens"] == fulltext_service.MAX_ANSWER_TOKENS
        messages = captured["messages"]
        assert messages[0]["role"] == "system"
        assert "设定档案管理员" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert "第一章" in messages[1]["content"]       # 正文消息
        assert messages[2]["role"] == "user"
        assert "铜铃在哪里出现的" in messages[2]["content"]  # 问题消息
        # 前缀稳定：正文必须完整出现在问题之前（正文内容顺序固定）
        assert messages[1]["content"].find("捡到一枚铜铃") < messages[2]["content"].find("铜铃在哪里出现的")
        assert messages[1]["content"].startswith("## 第1章 第一章")

        assert result["answer"] == "铜铃出现在第一章，是主角在河滩捡到的。"
        assert result["degraded"] is False
        assert result["estimatedTokens"] == result["chars"]
        assert result["chapters"] == 1
        assert result["truncated"] is False
        assert result["estimatedCostUsd"] >= 0

    @pytest.mark.asyncio
    async def test_llm_error_degrades_with_stats(self, env):
        """LLM 未配置/失败 → degraded=True + 降级提示，统计字段保留（不抛异常）。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c1", order=1, title="第一章", content="正文甲")

        with patch.object(
            fulltext_service.llm, "chat",
            new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
        ):
            result = await ask_fulltext(PROJECT_ID, "伏笔在哪？")
        assert result["degraded"] is True
        assert "暂不可用" in result["answer"]
        assert result["chars"] > 0
        assert result["estimatedTokens"] > 0
        assert result["chapters"] == 1

        with patch.object(
            fulltext_service.llm, "chat",
            new=AsyncMock(side_effect=LLMResponseError("上游失败")),
        ):
            result = await ask_fulltext(PROJECT_ID, "伏笔在哪？")
        assert result["degraded"] is True

    @pytest.mark.asyncio
    async def test_empty_question_raises(self, env):
        """空问题 → FulltextEmptyError（API 层转 400）。"""
        _insert_project(env)
        _insert_chapter(env, chapter_id="c1", order=1, title="第一章", content="正文甲")
        with pytest.raises(FulltextEmptyError):
            await ask_fulltext(PROJECT_ID, "   ")

    @pytest.mark.asyncio
    async def test_empty_book_raises(self, env):
        """空书 → FulltextEmptyError。"""
        _insert_project(env)
        with pytest.raises(FulltextEmptyError):
            await ask_fulltext(PROJECT_ID, "有什么伏笔？")


# ═══════════════════════════════════════════════════════════
# ③ API：fulltext-ask / fulltext-stats
# ═══════════════════════════════════════════════════════════

class TestFulltextApi:
    def test_ask_returns_answer_and_stats(self, api_env):
        _insert_project(api_env)
        _insert_chapter(api_env, chapter_id="c1", order=1, title="第一章", content="主角在第一章捡到一枚铜铃。")
        with patch.object(
            fulltext_service.llm, "chat",
            new=AsyncMock(return_value="铜铃出现在第一章。"),
        ), TestClient(app) as client:
            resp = client.post(
                f"/api/projects/{PROJECT_ID}/fulltext-ask",
                json={"question": "铜铃在哪？"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert data["answer"] == "铜铃出现在第一章。"
        assert data["degraded"] is False
        assert data["estimatedTokens"] > 0
        assert data["chars"] > 0
        assert data["chapters"] == 1
        assert data["estimatedCostUsd"] >= 0

    def test_ask_empty_question_returns_400(self, api_env):
        _insert_project(api_env)
        _insert_chapter(api_env, chapter_id="c1", order=1, title="第一章", content="正文甲")
        with TestClient(app) as client:
            resp = client.post(
                f"/api/projects/{PROJECT_ID}/fulltext-ask",
                json={"question": ""},
            )
        assert resp.status_code == 400

    def test_ask_empty_book_returns_400(self, api_env):
        _insert_project(api_env)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/projects/{PROJECT_ID}/fulltext-ask",
                json={"question": "有什么伏笔？"},
            )
        assert resp.status_code == 400

    def test_ask_project_missing_returns_404(self, api_env):
        with TestClient(app) as client:
            resp = client.post(
                f"/api/projects/{OTHER_PROJECT_ID}/fulltext-ask",
                json={"question": "有什么伏笔？"},
            )
        assert resp.status_code == 404

    def test_ask_llm_error_degrades_200(self, api_env):
        _insert_project(api_env)
        _insert_chapter(api_env, chapter_id="c1", order=1, title="第一章", content="正文甲")
        with patch.object(
            fulltext_service.llm, "chat",
            new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
        ), TestClient(app) as client:
            resp = client.post(
                f"/api/projects/{PROJECT_ID}/fulltext-ask",
                json={"question": "伏笔在哪？"},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["degraded"] is True
        assert data["estimatedTokens"] > 0

    def test_stats_returns_counts(self, api_env):
        _insert_project(api_env)
        # 每章约 3 万字，接近真实直灌量级，成本估算不再因精度归零
        _insert_chapter(api_env, chapter_id="c1", order=1, title="第一章", content="正文甲" * 10000)
        _insert_chapter(api_env, chapter_id="c2", order=2, title="第二章", content="正文乙" * 10000)
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{PROJECT_ID}/fulltext-stats")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["chars"] > 0
        assert data["estimatedTokens"] == data["chars"]
        assert data["chapters"] == 2
        assert data["truncated"] is False
        assert data["estimatedCostUsd"] > 0

    def test_stats_empty_book_returns_400(self, api_env):
        _insert_project(api_env)
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{PROJECT_ID}/fulltext-stats")
        assert resp.status_code == 400

    def test_stats_project_missing_returns_400(self, api_env):
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{OTHER_PROJECT_ID}/fulltext-stats")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# ③ T4：provider 解析 + 费用语义（估算 vs 实际）
# ═══════════════════════════════════════════════════════════

class _RecordingProvider:
    """测试替身：记录调用参数，返回固定回答，暴露 provider/model 供解析。"""

    def __init__(self, reply: str = "回答", provider: str = "deepseek", model: str = "deepseek-v4-flash"):
        self.reply = reply
        self.provider = provider
        self.model = model
        self.calls: list[dict] = []

    def request_manifest(self, role=None, **kwargs):
        return {"provider": self.provider, "model": self.model}

    async def chat(self, messages, role=None, **kwargs):
        self.calls.append({"messages": messages, "role": role, "kwargs": kwargs})
        return self.reply


class TestProviderRoutingAndCostSemantics:
    @pytest.fixture(autouse=True)
    def _seed(self, env):
        _insert_project(env)
        _insert_chapter(env, content="正文内容" * 20)

    @pytest.mark.asyncio
    async def test_runtime_provider_is_used_and_isolated_across_concurrent_runs(self, env):
        """并发 A/B 使用不同 provider 时，回答与费用元数据必须各归其主，不串线。"""
        import asyncio

        from app.llm.resolver import reset_runtime_provider, set_runtime_provider

        provider_a = _RecordingProvider(reply="A 的回答", provider="arts", model="deepseek-v4-flash")
        provider_b = _RecordingProvider(reply="B 的回答", provider="deepseek", model="deepseek-v4-flash")

        async def _run(target):
            token = set_runtime_provider(target)
            try:
                return await ask_fulltext(PROJECT_ID, "同一个问题")
            finally:
                reset_runtime_provider(token)

        result_a, result_b = await asyncio.gather(_run(provider_a), _run(provider_b))

        assert result_a["answer"] == "A 的回答"
        assert result_b["answer"] == "B 的回答"
        assert result_a["provider"] == "arts"
        assert result_b["provider"] == "deepseek"
        assert len(provider_a.calls) == 1 and len(provider_b.calls) == 1
        # Arts 价与 DeepSeek 官方价不得混用
        assert result_a["estimate"]["priceSource"] != result_b["estimate"]["priceSource"]
        assert result_a["estimate"]["costUsd"] != result_b["estimate"]["costUsd"]

    @pytest.mark.asyncio
    async def test_runtime_provider_takes_precedence_over_global_singleton(self, env):
        """必须走 resolver 解析出的 provider，而不是直接用全局单例。"""
        provider = _RecordingProvider(reply="来自 runtime provider")

        async def _boom(*args, **kwargs):
            raise AssertionError("全局单例 llm.chat 不应被调用")

        with patch.object(fulltext_service.llm, "chat", new=_boom):
            result = await ask_fulltext(PROJECT_ID, "问题", provider=provider)

        assert result["answer"] == "来自 runtime provider"
        assert len(provider.calls) == 1
        assert provider.calls[0]["role"] == ROLE_FULLTEXT

    @pytest.mark.asyncio
    async def test_unknown_price_source_yields_null_cost(self, env):
        """未列入已核实价格表的 provider/model → 费用为 null，不猜价格。"""
        provider = _RecordingProvider(reply="ok", provider="unknown-vendor", model="mystery-model")
        result = await ask_fulltext(PROJECT_ID, "问题", provider=provider)

        estimate = result["estimate"]
        assert estimate["status"] == "unknown"
        assert estimate["costUsd"] is None
        assert estimate["currency"] is None
        assert estimate["priceSource"] is None
        assert result["estimatedCostUsd"] is None

    @pytest.mark.asyncio
    async def test_estimate_separates_tokens_and_never_assumes_cache_hit(self, env):
        """估算必须分开输入/输出 token，且不得把所有输入计为缓存命中。"""
        provider = _RecordingProvider(reply="ok", provider="deepseek", model="deepseek-v4-flash")
        result = await ask_fulltext(PROJECT_ID, "问题", provider=provider)

        estimate = result["estimate"]
        assert estimate["status"] == "estimated"
        assert estimate["inputTokens"] == result["estimatedTokens"]
        assert estimate["outputTokensMax"] == fulltext_service.MAX_ANSWER_TOKENS
        # 估算阶段缓存命中未知
        assert estimate["cacheHitTokens"] is None
        # 按未命中价取上界，而不是缓存命中价
        assert estimate["basis"] == "cache_miss_input_upper_bound"
        expected = (
            estimate["inputTokens"] / 1_000_000 * 0.44
            + estimate["outputTokensMax"] / 1_000_000 * 1.32
        )
        assert estimate["costUsd"] == pytest.approx(round(expected, 6))
        assert 'DeepSeek official' in estimate["priceSource"]

    @pytest.mark.asyncio
    async def test_arts_price_is_not_deepseek_official_price(self, env):
        """Arts 报价必须独立，不能套用 DeepSeek 官方单价。"""
        provider = _RecordingProvider(reply="ok", provider="arts", model="deepseek-v4-flash")
        result = await ask_fulltext(PROJECT_ID, "问题", provider=provider)

        estimate = result["estimate"]
        assert estimate["status"] == "estimated"
        assert 'Arts' in estimate["priceSource"]
        expected = (
            estimate["inputTokens"] / 1_000_000 * 0.825
            + estimate["outputTokensMax"] / 1_000_000 * 2.475
        )
        assert estimate["costUsd"] == pytest.approx(round(expected, 6))
        deepseek_expected = (
            estimate["inputTokens"] / 1_000_000 * 0.44
            + estimate["outputTokensMax"] / 1_000_000 * 1.32
        )
        assert estimate["costUsd"] != pytest.approx(deepseek_expected)

    def test_actual_reports_provider_cache_hit_tokens(self, monkeypatch):
        """实际用量要透传 provider 回传的缓存命中量（未知时才为 null）。"""
        monkeypatch.setattr(fulltext_service, "get_llm_call_meta", lambda: {
            "provider": "deepseek", "model": "deepseek-v4-flash", "audit_id": "audit-1",
        })
        monkeypatch.setattr(fulltext_service, "get_llm_audit_record", lambda _id: {
            "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cache_hit_tokens": 100,
        })

        actual = fulltext_service.build_actual()

        assert actual["inputTokens"] == 120
        assert actual["outputTokens"] == 30
        assert actual["totalTokens"] == 150
        assert actual["cacheHitTokens"] == 100
        assert actual["actualCostUsd"] is None
        assert actual["status"] == "reported"

    def test_actual_cache_hit_is_null_when_provider_omits_it(self, monkeypatch):
        """provider 未回传缓存命中量时必须为 null，不得假设命中。"""
        monkeypatch.setattr(fulltext_service, "get_llm_call_meta", lambda: {
            "provider": "arts", "model": "deepseek-v4-flash", "audit_id": "audit-2",
        })
        monkeypatch.setattr(fulltext_service, "get_llm_audit_record", lambda _id: {
            "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cache_hit_tokens": None,
        })

        actual = fulltext_service.build_actual()

        assert actual["cacheHitTokens"] is None
        assert actual["actualCostUsd"] is None

    @pytest.mark.asyncio
    async def test_failure_never_promises_zero_cost(self, env):
        """失败提示不得声称"未产生费用"；实际费用恒为 null（以服务商记录为准）。"""
        failing = _RecordingProvider()

        async def _fail(*args, **kwargs):
            raise LLMResponseError("upstream failed")

        failing.chat = _fail  # type: ignore[assignment]
        result = await ask_fulltext(PROJECT_ID, "问题", provider=failing)

        assert result["degraded"] is True
        assert "未产生费用" not in result["answer"]
        assert "以服务商记录为准" in result["answer"]
        assert result["actual"]["actualCostUsd"] is None
        assert result["actual"]["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_budget_failure_blocks_before_any_provider_request(self, env):
        """项目预算不足时必须在发起真实请求之前失败（不产生 provider 调用）。"""
        from unittest.mock import AsyncMock

        from app.llm.provider import LLMBudgetExceededError
        from app.llm.provider import LLMProvider
        from app.services import project_budget_service

        provider = LLMProvider(provider="deepseek", model="deepseek-v4-flash", api_key="test-key-not-real")
        transport = AsyncMock()
        with patch.object(provider, "_call_deepseek", new=transport), patch.object(
            project_budget_service, "consume_project_budget",
            side_effect=LLMBudgetExceededError("项目今日 LLM 预算已用尽"),
        ):
            result = await ask_fulltext(PROJECT_ID, "问题", provider=provider)

        transport.assert_not_awaited()
        assert result["degraded"] is True
        assert "以服务商记录为准" in result["answer"]
