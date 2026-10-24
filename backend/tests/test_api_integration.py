"""NovelCraft API 集成测试

使用 FastAPI TestClient 对核心 API 端点进行 HTTP 层集成测试。

覆盖端点：
- GET  /api/projects            — 项目列表
- POST /api/projects            — 创建项目
- GET  /api/projects/{id}       — 获取单个项目
- PATCH /api/projects/{id}      — 更新项目（补充；统一为 PATCH，PUT 返回 405）
- DELETE /api/projects/{id}     — 删除项目（补充）
- POST /api/projects/{id}/chapters — 创建章节
- PUT  /api/projects/{id}/chapters/{chapter_id} — 保存章节内容（补充）
- DELETE /api/projects/{id}/chapters/{chapter_id} — 删除章节（补充）
- DELETE /api/projects/{id}/chapters — 批量删除章节（未实现，记录 405）

设计原则：
- 每个测试独立创建/清理数据，不依赖执行顺序
- 同时覆盖正常路径（200）和异常路径（404/422）
- 校验返回结构（ApiResponse 包装、必要字段存在性）
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from main import app


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _unique_name(prefix: str = "test") -> str:
    """生成唯一项目名，避免测试间冲突。"""
    return f"{prefix}-{int(time.time() * 1000)}"


def _create_project(client: TestClient, **overrides) -> dict:
    """创建项目并返回 data 字典。"""
    payload = {
        "name": _unique_name("api-test"),
        "genre": "scifi",
        **overrides,
    }
    resp = client.post("/api/projects/", json=payload)
    assert resp.status_code == 200, f"创建项目失败: {resp.text}"
    return resp.json()["data"]


def _delete_project(client: TestClient, project_id: str) -> None:
    """删除项目（忽略 404/409）。"""
    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code in (200, 404, 409), f"删除失败: {resp.status_code} {resp.text}"


# ═══════════════════════════════════════════════════════════
# GET /api/projects — 项目列表
# ═══════════════════════════════════════════════════════════

class TestListProjects:
    """GET /api/projects 端点测试"""

    def test_returns_200_and_data_is_list(self):
        """正常路径：返回 200 且 data 为列表。"""
        with TestClient(app) as client:
            resp = client.get("/api/projects/")
            assert resp.status_code == 200

            body = resp.json()
            assert body["success"] is True
            assert body["error"] is None
            assert isinstance(body["data"], list)

    def test_includes_newly_created_project(self):
        """正常路径：新建项目后，列表应包含该项目。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("list-test"))
            list_resp = client.get("/api/projects/")
            ids = [p["id"] for p in list_resp.json()["data"]]
            assert project["id"] in ids
            _delete_project(client, project["id"])

    def test_response_structure_has_required_fields(self):
        """结构校验：列表中每个项目应含必要字段。"""
        with TestClient(app) as client:
            project = _create_project(client)
            list_resp = client.get("/api/projects/")

            for p in list_resp.json()["data"]:
                for field in ("id", "name", "genre", "status", "createdAt", "updatedAt"):
                    assert field in p, f"缺少字段 {field}"
                break  # 只校验第一个元素即可
            _delete_project(client, project["id"])


# ═══════════════════════════════════════════════════════════
# POST /api/projects — 创建项目
# ═══════════════════════════════════════════════════════════

class TestCreateProject:
    """POST /api/projects 端点测试"""

    def test_minimal_fields_returns_200_with_defaults(self):
        """正常路径：仅必填字段，返回 200 且含默认值。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={
                "name": _unique_name("minimal"),
                "genre": "xuanhuan",
            })
            assert resp.status_code == 200

            body = resp.json()
            assert body["success"] is True
            data = body["data"]
            assert data["name"].startswith("minimal-")
            assert data["genre"] == "xuanhuan"
            assert data["status"] == "planning"
            assert "id" in data
            assert "createdAt" in data
            assert "updatedAt" in data
            assert data["targetWordCount"] == 300000  # 默认值
            assert data["isStarred"] is False          # 默认值

            _delete_project(client, data["id"])

    def test_all_fields_returns_matching_values(self):
        """正常路径：全字段创建，返回值与输入一致。"""
        with TestClient(app) as client:
            payload = {
                "name": _unique_name("full"),
                "subtitle": "测试副标题",
                "genre": "mystery",
                "targetWordCount": 150000,
                "stylePreset": "concise",
                "premise": "一位档案管理员发现她的生活日志正在被逐章改写。",
                "targetChapters": 24,
            }
            resp = client.post("/api/projects/", json=payload)
            assert resp.status_code == 200

            data = resp.json()["data"]
            assert data["name"] == payload["name"]
            assert data["subtitle"] == payload["subtitle"]
            assert data["genre"] == payload["genre"]
            assert data["targetWordCount"] == payload["targetWordCount"]
            assert data["premise"] == payload["premise"]
            assert data["targetChapters"] == payload["targetChapters"]

            _delete_project(client, data["id"])

    def test_optional_protagonist_creates_minimal_character_context(self):
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={
                "name": _unique_name("protagonist"),
                "premise": "一名信使要穿过封锁的河桥。",
                "stylePreset": "emotional_resonance",
                "protagonistName": "林乔",
            })
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["characters"] == [{
                "id": data["characters"][0]["id"], "name": "林乔", "role": "protagonist",
                "basicInfo": {"identity": "主角"},
            }]
            _delete_project(client, data["id"])

    def test_missing_name_returns_422(self):
        """异常路径：缺少必填字段 name，应返回 422。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={"genre": "scifi"})
            assert resp.status_code == 422

    def test_empty_body_returns_422(self):
        """异常路径：空请求体，应返回 422。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={})
            assert resp.status_code == 422

    def test_empty_name_falls_back_to_default(self):
        """边界路径：空字符串 name 应被兜底为"未命名项目"（ISSUE-001）。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={"name": "", "genre": "scifi"})
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["name"] == "未命名项目"
            _delete_project(client, data["id"])

    def test_whitespace_name_falls_back_to_default(self):
        """边界路径：纯空白 name 也应被兜底为"未命名项目"（ISSUE-001）。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={"name": "   \t ", "genre": "scifi"})
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["name"] == "未命名项目"
            _delete_project(client, data["id"])

    def test_large_target_word_count_accepts(self):
        """边界路径：极大的目标字数应正常接受。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/", json={
                "name": _unique_name("large"),
                "targetWordCount": 9999999,
            })
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["targetWordCount"] == 9999999
            _delete_project(client, data["id"])


# ═══════════════════════════════════════════════════════════
# GET /api/projects/{id} — 获取单个项目
# ═══════════════════════════════════════════════════════════

class TestGetProject:
    """GET /api/projects/{id} 端点测试"""

    def test_valid_id_returns_full_project(self):
        """正常路径：创建后立即查询，返回完整项目信息。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("query"))
            get_resp = client.get(f"/api/projects/{project['id']}")

            assert get_resp.status_code == 200
            data = get_resp.json()["data"]
            assert data["id"] == project["id"]
            assert data["name"] == project["name"]
            assert data["status"] == "planning"

            _delete_project(client, project["id"])

    def test_nonexistent_id_returns_404(self):
        """异常路径：不存在的项目 id，应返回 404。"""
        with TestClient(app) as client:
            resp = client.get("/api/projects/nonexistent-id-12345")
            assert resp.status_code == 404
            body = resp.json()
            assert "项目未找到" in body["detail"]

    def test_deleted_project_returns_404(self):
        """异常路径：创建→删除→再查询，应返回 404。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("delete-me"))
            project_id = project["id"]

            del_resp = client.delete(f"/api/projects/{project_id}")
            assert del_resp.status_code == 200

            get_resp = client.get(f"/api/projects/{project_id}")
            assert get_resp.status_code == 404

    def test_id_with_special_characters_safe(self):
        """边界路径：含特殊字符的 id，应安全返回 404（不崩溃）。"""
        with TestClient(app) as client:
            resp = client.get("/api/projects/../etc/passwd")
            # FastAPI 路由匹配可能返回 404 或 307；核心是不崩溃
            assert resp.status_code in (404, 307)


# ═══════════════════════════════════════════════════════════
# PATCH /api/projects/{id} — 更新项目
# ═══════════════════════════════════════════════════════════

class TestUpdateProject:
    """PATCH /api/projects/{id} 端点测试"""

    def test_update_name_returns_new_name(self):
        """正常路径：更新项目名称为非空值，返回新名称。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("rename"))
            pid = project["id"]

            resp = client.patch(f"/api/projects/{pid}", json={"name": "改名后的项目"})
            assert resp.status_code == 200
            assert resp.json()["data"]["name"] == "改名后的项目"

            _delete_project(client, pid)

    def test_update_name_to_empty_falls_back_to_default(self):
        """边界路径：更新名称为空字符串，应兜底为"未命名项目"（ISSUE-001）。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("clear-name"))
            pid = project["id"]

            resp = client.patch(f"/api/projects/{pid}", json={"name": ""})
            assert resp.status_code == 200
            assert resp.json()["data"]["name"] == "未命名项目"

            # 数据库中读取也应一致
            get_resp = client.get(f"/api/projects/{pid}")
            assert get_resp.json()["data"]["name"] == "未命名项目"

            _delete_project(client, pid)

    def test_update_name_to_whitespace_falls_back_to_default(self):
        """边界路径：更新名称为纯空白，应兜底为"未命名项目"（ISSUE-001）。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("blank-name"))
            pid = project["id"]

            resp = client.patch(f"/api/projects/{pid}", json={"name": "  \t "})
            assert resp.status_code == 200
            assert resp.json()["data"]["name"] == "未命名项目"

            _delete_project(client, pid)


# ═══════════════════════════════════════════════════════════
# POST /api/projects/{id}/chapters — 创建章节
# ═══════════════════════════════════════════════════════════

class TestCreateChapter:
    """POST /api/projects/{id}/chapters 端点测试"""

    @pytest.fixture
    def project(self):
        """测试级别的项目 fixture：创建项目→供测试使用→清理。"""
        with TestClient(app) as client:
            p = _create_project(client, name=_unique_name("chapter-proj"))
            yield client, p
            _delete_project(client, p["id"])

    def test_minimal_returns_200_with_required_fields(self, project):
        """正常路径：最少字段创建章节，返回 200 且含必要字段。"""
        client, proj = project
        resp = client.post(f"/api/projects/{proj['id']}/chapters/", json={
            "title": "第一章：启程",
            "volumeId": "",
            "order": 1,
        })
        assert resp.status_code == 200

        body = resp.json()
        assert body["success"] is True
        chapter = body["data"]
        assert chapter["title"] == "第一章：启程"
        assert chapter["projectId"] == proj["id"]
        assert chapter["order"] == 1
        assert chapter["status"] == "draft"
        assert chapter["wordCount"] == 0
        assert chapter["content"] == ""
        assert "id" in chapter
        assert "createdAt" in chapter
        assert "updatedAt" in chapter

    def test_multiple_chapters_in_same_project(self, project):
        """正常路径：同一项目创建多个章节，列表递增且排序正确。"""
        client, proj = project
        pid = proj["id"]

        # 创建第 1 章
        r1 = client.post(f"/api/projects/{pid}/chapters/", json={
            "title": "第一章",
            "volumeId": "",
            "order": 1,
        })
        assert r1.status_code == 200

        # 创建第 2 章
        r2 = client.post(f"/api/projects/{pid}/chapters/", json={
            "title": "第二章",
            "volumeId": "",
            "order": 2,
        })
        assert r2.status_code == 200

        # 列表应包含 2 个章节
        list_resp = client.get(f"/api/projects/{pid}/chapters/")
        assert list_resp.status_code == 200
        chapters = list_resp.json()["data"]
        assert len(chapters) == 2
        titles = [c["title"] for c in chapters]
        assert "第一章" in titles
        assert "第二章" in titles

        # 排序校验（按 order 升序）
        assert chapters[0]["order"] == 1
        assert chapters[1]["order"] == 2

    def test_missing_title_returns_422(self, project):
        """异常路径：缺少必填字段 title，应返回 422。"""
        client, proj = project
        resp = client.post(f"/api/projects/{proj['id']}/chapters/", json={
            "volumeId": "",
            "order": 1,
        })
        assert resp.status_code == 422

    def test_nonexistent_project_returns_404(self):
        """异常路径：在不存在的项目下创建章节，应返回 404（ISSUE-002）。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/fake-project-id-99999/chapters/", json={
                "title": "幽灵章节",
                "volumeId": "",
                "order": 1,
            })
            assert resp.status_code == 404
            body = resp.json()
            assert "项目未找到" in body["detail"]

    def test_whitespace_project_id_returns_404(self):
        """异常路径：空白 project_id 同样不插入幽灵章节，返回 404（ISSUE-002）。"""
        with TestClient(app) as client:
            resp = client.post("/api/projects/   /chapters/", json={
                "title": "幽灵章节",
                "volumeId": "",
                "order": 1,
            })
            assert resp.status_code == 404

    def test_large_order_value_accepted(self, project):
        """边界路径：极大的 order 值应正常接受。"""
        client, proj = project
        resp = client.post(f"/api/projects/{proj['id']}/chapters/", json={
            "title": "远章",
            "volumeId": "",
            "order": 99999,
        })
        assert resp.status_code == 200
        assert resp.json()["data"]["order"] == 99999


# ═══════════════════════════════════════════════════════════
# 跨端点一致性校验
# ═══════════════════════════════════════════════════════════

class TestCrossEndpointConsistency:
    """跨端点数据一致性校验"""

    def test_create_list_detail_consistent(self):
        """一致性校验：创建→列表包含→详情与创建数据一致。"""
        with TestClient(app) as client:
            payload = {
                "name": _unique_name("consistency"),
                "genre": "mystery",
                "premise": "验证数据一致性。",
            }
            create_resp = client.post("/api/projects/", json=payload)
            assert create_resp.status_code == 200
            created = create_resp.json()["data"]

            # 列表包含
            list_resp = client.get("/api/projects/")
            list_ids = [p["id"] for p in list_resp.json()["data"]]
            assert created["id"] in list_ids

            # 详情一致
            detail_resp = client.get(f"/api/projects/{created['id']}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()["data"]
            assert detail["name"] == payload["name"]
            assert detail["genre"] == payload["genre"]
            assert detail["premise"] == payload["premise"]

            _delete_project(client, created["id"])

    def test_chapter_appears_in_project_chapter_list(self):
        """一致性校验：章节创建后出现在项目章节列表中。"""
        with TestClient(app) as client:
            proj = _create_project(client)
            pid = proj["id"]

            ch_resp = client.post(f"/api/projects/{pid}/chapters/", json={
                "title": "一致性章节",
                "volumeId": "vol-1",
                "order": 1,
            })
            assert ch_resp.status_code == 200
            chapter_id = ch_resp.json()["data"]["id"]

            list_resp = client.get(f"/api/projects/{pid}/chapters/")
            chapter_ids = [c["id"] for c in list_resp.json()["data"]]
            assert chapter_id in chapter_ids

            _delete_project(client, pid)


class TestHealthEndpoint:
    """健康检查端点"""

    def test_health_returns_ok(self):
        """正常路径：健康检查返回 ok。"""
        with TestClient(app) as client:
            resp = client.get("/api/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["version"] == "1.0.0"
            assert "instance" not in body
            assert "debug" not in body


# ═══════════════════════════════════════════════════════════
# PATCH /api/projects/{id} — 更新项目（补充）
# 注：项目更新接口统一为 PATCH（部分字段更新语义），PUT 返回 405（契约测试见下）。
# ═══════════════════════════════════════════════════════════

class TestUpdateProjectFields:
    """PATCH /api/projects/{id} 端点补充测试"""

    def test_update_status_subtitle_starred_returns_updated(self):
        """正常路径：更新 status/subtitle/isStarred，返回更新后的值。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("patch-fields"))
            pid = project["id"]

            resp = client.patch(f"/api/projects/{pid}", json={
                "status": "writing",
                "subtitle": "补充测试副标题",
                "isStarred": True,
            })
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["status"] == "writing"
            assert data["subtitle"] == "补充测试副标题"
            assert data["isStarred"] is True

            # 数据库中读取也应一致
            get_resp = client.get(f"/api/projects/{pid}")
            assert get_resp.json()["data"]["status"] == "writing"
            assert get_resp.json()["data"]["isStarred"] is True

            _delete_project(client, pid)

    def test_update_nonexistent_project_returns_404(self):
        """异常路径：更新不存在的项目，应返回 404。"""
        with TestClient(app) as client:
            resp = client.patch("/api/projects/nonexistent-proj-0001", json={"name": "x"})
            assert resp.status_code == 404
            assert "项目未找到" in resp.json()["detail"]

    def test_update_empty_body_returns_project_unchanged(self):
        """边界路径：空请求体不产生任何更新，返回项目原值。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("empty-patch"))
            pid = project["id"]

            resp = client.patch(f"/api/projects/{pid}", json={})
            assert resp.status_code == 200
            assert resp.json()["data"]["id"] == pid
            assert resp.json()["data"]["name"] == project["name"]

            _delete_project(client, pid)

    def test_put_method_not_supported_returns_405(self):
        """契约校验：更新项目端点实际为 PATCH，PUT 应返回 405。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]

            resp = client.put(f"/api/projects/{pid}", json={"name": "x"})
            assert resp.status_code == 405

            _delete_project(client, pid)


# ═══════════════════════════════════════════════════════════
# DELETE /api/projects/{id} — 删除项目
# ═══════════════════════════════════════════════════════════

class TestDeleteProject:
    """DELETE /api/projects/{id} 端点测试"""

    def test_delete_existing_project_returns_200(self):
        """正常路径：删除存在的项目，返回 200 且 data 为 None。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]

            resp = client.delete(f"/api/projects/{pid}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["data"] is None

    def test_delete_nonexistent_project_returns_404(self):
        """异常路径：删除不存在的项目，应返回 404。"""
        with TestClient(app) as client:
            resp = client.delete("/api/projects/nonexistent-proj-0002")
            assert resp.status_code == 404
            assert "项目未找到" in resp.json()["detail"]

    def test_delete_project_removes_its_chapters(self):
        """一致性：删除项目后其章节应一并清除，项目查询返回 404。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("rm-proj"))
            pid = project["id"]

            ch_resp = client.post(f"/api/projects/{pid}/chapters/", json={
                "title": "待清理章节",
                "volumeId": "",
                "order": 1,
            })
            assert ch_resp.status_code == 200

            del_resp = client.delete(f"/api/projects/{pid}")
            assert del_resp.status_code == 200

            # 项目已删除
            get_resp = client.get(f"/api/projects/{pid}")
            assert get_resp.status_code == 404

            # 章节应已随项目清除（列表为空）
            list_resp = client.get(f"/api/projects/{pid}/chapters/")
            assert list_resp.status_code == 404

    def test_delete_project_cascades_m1_memory_tables(self):
        """一致性（P2-3）：删除项目时级联清理 M1 记忆表 novel_facts 与 volumes。"""
        from app.dao import novel_facts as novel_facts_dao
        from app.dao import volumes as volumes_dao

        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("rm-m1"))
            pid = project["id"]

            # 直接向 M1 记忆表写入数据（模拟长书创作后的事实账本与卷摘要）
            assert novel_facts_dao.add_fact(pid, "completed", "主角取得青云令", "ch-1") is True
            assert novel_facts_dao.add_fact(pid, "state_changed", "陈墨死亡", "ch-3") is True
            volumes_dao.upsert_volume(pid, "v1", title="第一卷", volume_order=1, summary="卷摘要")

            assert novel_facts_dao.count_facts(pid) == 2
            assert len(volumes_dao.list_volumes(pid)) == 1

            del_resp = client.delete(f"/api/projects/{pid}")
            assert del_resp.status_code == 200

            # 事实账本与卷记录应随项目一并清除
            assert novel_facts_dao.count_facts(pid) == 0
            assert volumes_dao.list_volumes(pid) == []


# ═══════════════════════════════════════════════════════════
# PUT /api/projects/{id}/chapters/{chapter_id} — 保存章节内容
# ═══════════════════════════════════════════════════════════

class TestSaveChapter:
    """PUT /api/projects/{id}/chapters/{chapter_id} 端点测试"""

    def test_save_new_chapter_auto_creates(self):
        """正常路径：保存到不存在的章节 id，应自动创建并返回。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("put-ch"))
            pid = project["id"]
            cid = f"ch-{int(time.time() * 1000)}"

            resp = client.put(f"/api/projects/{pid}/chapters/{cid}", json={
                "content": "自动创建的章节正文",
                "title": "自动章节",
                "order": 3,
            })
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["id"] == cid
            assert data["projectId"] == pid
            assert data["title"] == "自动章节"
            assert data["order"] == 3
            assert data["content"] == "自动创建的章节正文"
            assert data["wordCount"] == 9  # 中文按字符计，去空格/换行
            assert data["status"] == "writing"  # 首次自动创建时状态为 writing

            _delete_project(client, pid)

    def test_save_existing_chapter_updates_content_and_status(self):
        """正常路径：再次保存已有章节，wordCount 更新且状态转为 completed。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("upd-ch"))
            pid = project["id"]
            cid = f"ch-{int(time.time() * 1000)}"

            # 首次保存（自动创建）
            r1 = client.put(f"/api/projects/{pid}/chapters/{cid}", json={"content": "一二三"})
            assert r1.status_code == 200
            assert r1.json()["data"]["wordCount"] == 3
            assert r1.json()["data"]["status"] == "writing"

            # 二次保存（更新已有章节）
            r2 = client.put(f"/api/projects/{pid}/chapters/{cid}", json={
                "content": "一二三四五六",
                "title": "更新标题",
            })
            assert r2.status_code == 200
            data = r2.json()["data"]
            assert data["wordCount"] == 6
            assert data["status"] == "completed"
            assert data["title"] == "更新标题"

            _delete_project(client, pid)

    def test_save_missing_content_returns_422(self):
        """异常路径：缺少必填字段 content，应返回 422。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]
            cid = f"ch-{int(time.time() * 1000)}"

            resp = client.put(f"/api/projects/{pid}/chapters/{cid}", json={"title": "无正文"})
            assert resp.status_code == 422

            _delete_project(client, pid)

    def test_save_to_nonexistent_project_should_return_404(self):
        """异常路径：保存章节到不存在的项目应返回 404，且不产生幽灵章节（BUG-NC-001 已修复）。"""
        ghost_proj = "ghost-project-0001"
        ghost_ch = f"ghost-ch-{int(time.time() * 1000)}"
        with TestClient(app) as client:
            resp = client.put(
                f"/api/projects/{ghost_proj}/chapters/{ghost_ch}",
                json={"content": "幽灵正文"},
            )
            assert resp.status_code == 404
            assert "项目未找到" in resp.json()["detail"]

            # 确认未产生幽灵章节：再次保存仍返回 404
            resp2 = client.put(
                f"/api/projects/{ghost_proj}/chapters/{ghost_ch}",
                json={"content": "幽灵正文"},
            )
            assert resp2.status_code == 404


# ═══════════════════════════════════════════════════════════
# DELETE /api/projects/{id}/chapters/{chapter_id} — 删除章节
# ═══════════════════════════════════════════════════════════

class TestDeleteChapter:
    """DELETE /api/projects/{id}/chapters/{chapter_id} 端点测试"""

    def test_delete_existing_chapter_returns_200(self):
        """正常路径：删除存在的章节，返回 200 且 data 为 None。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("rm-ch"))
            pid = project["id"]

            create_resp = client.post(f"/api/projects/{pid}/chapters/", json={
                "title": "待删章节",
                "volumeId": "",
                "order": 1,
            })
            assert create_resp.status_code == 200
            cid = create_resp.json()["data"]["id"]

            resp = client.delete(f"/api/projects/{pid}/chapters/{cid}")
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            assert resp.json()["data"] is None

            _delete_project(client, pid)

    def test_delete_nonexistent_chapter_returns_404(self):
        """异常路径：删除不存在的章节，应返回 404。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]

            resp = client.delete(f"/api/projects/{pid}/chapters/nonexistent-ch-0001")
            assert resp.status_code == 404
            assert "章节未找到" in resp.json()["detail"]

            _delete_project(client, pid)

    def test_delete_chapter_removed_from_project_list(self):
        """一致性：删除章节后，项目章节列表不应再包含该章节。"""
        with TestClient(app) as client:
            project = _create_project(client, name=_unique_name("list-ch"))
            pid = project["id"]

            # 创建两章
            c1 = client.post(f"/api/projects/{pid}/chapters/", json={
                "title": "第一章", "volumeId": "", "order": 1,
            }).json()["data"]
            c2 = client.post(f"/api/projects/{pid}/chapters/", json={
                "title": "第二章", "volumeId": "", "order": 2,
            }).json()["data"]

            # 删除第一章
            resp = client.delete(f"/api/projects/{pid}/chapters/{c1['id']}")
            assert resp.status_code == 200

            # 列表只剩第二章
            list_resp = client.get(f"/api/projects/{pid}/chapters/")
            assert list_resp.status_code == 200
            ids = [c["id"] for c in list_resp.json()["data"]]
            assert c1["id"] not in ids
            assert c2["id"] in ids

            _delete_project(client, pid)


# ═══════════════════════════════════════════════════════════
# DELETE /api/projects/{id}/chapters — 批量删除章节（未实现）
# ═══════════════════════════════════════════════════════════

class TestBulkDeleteChapters:
    """DELETE /api/projects/{id}/chapters 批量删除章节端点"""

    def test_bulk_delete_endpoint_not_implemented_returns_405(self):
        """现状记录：批量删除章节端点后端未实现，请求返回 405（待产品/开发确认）。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]

            resp = client.delete(f"/api/projects/{pid}/chapters")
            assert resp.status_code == 405

            _delete_project(client, pid)


class TestNovelJobResume:
    """M2 断点续传：POST /api/ai/{job_id}/resume 端点"""

    def test_resume_nonexistent_job_returns_404(self):
        """不存在的任务 ID：返回 404。"""
        with TestClient(app) as client:
            resp = client.post(f"/api/ai/{_unique_name('resume')}/resume")
            assert resp.status_code == 404

    def test_resume_completed_job_returns_400(self):
        """已完成状态任务不可恢复：真实 DB 构造 completed 任务，resume 返回 400（业务拒绝）。"""
        from app.dao import novel_jobs as novel_jobs_dao
        from app.db import get_conn

        with TestClient(app) as client:
            project = _create_project(client)
            job_id = _unique_name("completed-job")
            novel_jobs_dao.save_job({
                "id": job_id,
                "project_id": project["id"],
                "mode": "all",
                "requested_chapter_id": None,
                "status": "completed",
                "message": "",
                "target_words": 10000,
                "total_chapters": 5,
                "completed_chapters": 5,
                "failed_chapters": 0,
                "fallback_chapters": 0,
                "generated_words": 0,
                "current_chapter_id": None,
                "current_chapter_title": "",
                "last_completed_chapter_id": None,
                "last_completed_chapter_title": "",
                "llm_provider": "",
                "llm_model": "",
                "last_generation_source": "",
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:00Z",
                "error": "",
                "cancel_requested": 0,
                "max_retries": 1,
                "retry_count": 0,
                "created_at": "2026-01-01T00:00:00Z",
                "checkpoint_type": "",
                "resume_target_json": "",
                "pending_manual_recovery": 0,
                "heartbeat_at": "",
            })
            try:
                resp = client.post(f"/api/ai/{job_id}/resume")
                assert resp.status_code == 400, f"期望 400 业务拒绝，实际 {resp.status_code}: {resp.text}"
                assert "不可恢复" in resp.json().get("detail", "")
            finally:
                # 清理：外键约束默认未开启，先手动删除任务行，再删项目
                with get_conn() as conn:
                    conn.execute("DELETE FROM novel_jobs WHERE id = ?", (job_id,))
                _delete_project(client, project["id"])

    def test_recoverable_list_empty_for_nonexistent_project(self):
        """不存在的项目：可恢复列表为空数组（200）。"""
        with TestClient(app) as client:
            resp = client.get(f"/api/ai/project/{_unique_name('proj')}/recoverable")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["data"] == []

# ═══════════════════════════════════════════════════════════
# P1：记忆投影状态 API（查询 + 单项重放）
# ═══════════════════════════════════════════════════════════

class TestProjectionStatus:
    """GET/POST 记忆投影端点（正文已保存但记忆未同步的可见状态 + 重试）。"""

    def _make_chapter(self, client: TestClient, pid: str) -> str:
        resp = client.post(f"/api/projects/{pid}/chapters/", json={
            "title": "第一章", "volumeId": "", "order": 1,
        })
        assert resp.status_code == 200
        return resp.json()["data"]["id"]

    def test_list_projections_empty_for_new_chapter(self):
        """新章节尚无投影记录 → 200 空数组。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]
            cid = self._make_chapter(client, pid)
            resp = client.get(f"/api/projects/{pid}/chapters/{cid}/projections")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert isinstance(body["data"], list)
            assert body["data"] == []
            _delete_project(client, pid)

    def test_replay_unknown_type_rejected(self):
        """未知投影类型 → 400。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]
            cid = self._make_chapter(client, pid)
            resp = client.post(f"/api/projects/{pid}/chapters/{cid}/projections/nope/replay")
            assert resp.status_code == 400
            assert "未知投影类型" in resp.json()["detail"]
            _delete_project(client, pid)

    def test_replay_skipped_when_chapter_has_no_content(self):
        """章节无正文时重放 → 200 且 status=skipped（不抛错，幂等安全）。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]
            cid = self._make_chapter(client, pid)
            resp = client.post(f"/api/projects/{pid}/chapters/{cid}/projections/facts/replay")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["data"]["status"] == "skipped"
            _delete_project(client, pid)

    def test_projection_endpoint_404_for_missing_chapter(self):
        """不存在的章节 → 404。"""
        with TestClient(app) as client:
            project = _create_project(client)
            pid = project["id"]
            resp = client.get(f"/api/projects/{pid}/chapters/nope-000/projections")
            assert resp.status_code == 404
            _delete_project(client, pid)
