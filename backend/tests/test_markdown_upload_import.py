"""Markdown 章节「上传式」导入的测试。

背景：路径式接口（`/import/markdown-folder*`）在 `DEBUG=false` 下一律 403，
而前端此前只有路径模式 —— 于是生产配置下 Markdown 导入完全不可用，报错还写着
「仅支持上传文件」。本文件锁住新增的上传通道：

- 不读服务端任意路径，因此不受 DEBUG 门禁影响；
- 章节顺序与目录结构沿用路径模式的规则（文件名里的章节号优先）；
- 落盘的临时目录必须清理干净。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from main import _request_windows, app


OWNER = {"X-NovelCraft-Invite": "owner-token"}
OTHER = {"X-NovelCraft-Invite": "other-token"}

SCAN_URL = "/api/import/markdown-files"
INGEST_URL = "/api/import/markdown-files/ingest"


def _configure(monkeypatch) -> None:
    # 关键：这条通道必须在生产配置（debug=false）下可用
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner-token:user-a,other-token:user-b")
    _request_windows.clear()


def _md(title: str, body: str) -> bytes:
    return f"# {title}\n\n{body}".encode("utf-8")


def _files(*items: tuple[str, bytes]):
    """items: (relativePath, payload)"""
    return [
        ("files", (Path(relative).name, payload, "text/markdown"))
        for relative, payload in items
    ]


def _paths(*relative_paths: str):
    return {"relativePaths": list(relative_paths)}


@pytest.fixture
def tracked_temp_dirs(monkeypatch):
    """记录 mkdtemp 产生的目录，用于断言清理彻底。"""
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _spy(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr("app.api.imports.tempfile.mkdtemp", _spy)
    return created


# ── 鉴权与输入校验 ─────────────────────────────────────────

def test_upload_scan_requires_authentication(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(SCAN_URL, files=_files(("第01章.md", _md("第一章", "林晚走了。"))))

    assert response.status_code == 401


def test_upload_scan_rejects_empty_file_list(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(SCAN_URL, headers=OWNER, files=[])

    assert response.status_code == 422  # FastAPI 对必填 File(...) 的校验


def test_upload_scan_rejects_non_markdown(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=[("files", ("notes.txt", b"hello", "text/plain"))],
        )

    assert response.status_code == 400
    assert "只支持" in response.json()["detail"]


def test_upload_scan_rejects_binary_payload(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=_files(("第01章.md", b"# \xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0\x00\x01")),
        )

    assert response.status_code == 400
    assert "二进制" in response.json()["detail"]


# ── 扫描：顺序与结构 ───────────────────────────────────────

def test_chapters_are_ordered_by_chapter_number_in_filename(monkeypatch):
    """章节目录顺序必须跟文件名里的章节号走，而不是浏览器的选择顺序。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=_files(
                ("第10章 归途.md", _md("第十章 归途", "她回到了港口。")),
                ("第02章 旧档.md", _md("第二章 旧档", "档案被人动过。")),
                ("第01章 雨夜.md", _md("第一章 雨夜", "雨下了一整夜。")),
            ),
            data=_paths("第10章 归途.md", "第02章 旧档.md", "第01章 雨夜.md"),
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] is True
    assert data["totalFiles"] == 3
    assert [chapter["fileName"] for chapter in data["chapters"]] == [
        "第01章 雨夜.md",
        "第02章 旧档.md",
        "第10章 归途.md",
    ]
    assert [chapter["order"] for chapter in data["chapters"]] == [1, 2, 3]


def test_relative_paths_preserve_nested_folders_and_avoid_collisions(monkeypatch):
    """选整个文件夹时会给相对路径；不同卷下的同名章节不能互相覆盖。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=_files(
                ("第01章.md", _md("第一卷第一章", "卷一开局。")),
                ("第01章.md", _md("第二卷第一章", "卷二开局。")),
            ),
            data=_paths("我的小说/第一卷/第01章.md", "我的小说/第二卷/第01章.md"),
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["totalFiles"] == 2
    relative = {chapter["relativePath"] for chapter in data["chapters"]}
    # 相对路径完整保留上传时的目录层级（含顶层文件夹）
    assert relative == {"我的小说/第一卷/第01章.md", "我的小说/第二卷/第01章.md"}
    titles = {chapter["title"] for chapter in data["chapters"]}
    assert titles == {"第一卷第一章", "第二卷第一章"}


def test_folder_name_is_inferred_from_top_level_directory(monkeypatch):
    """临时目录是随机 uuid，项目名要从上传的相对路径推出来。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章", "开头。"))),
            data=_paths("雨夜长卷/第01章.md"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["folderName"] == "雨夜长卷"


def test_path_traversal_in_relative_paths_is_neutralized(monkeypatch, tracked_temp_dirs):
    """`../../` 不能把文件写到临时根目录之外。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章", "开头。"))),
            data=_paths("../../../etc/第01章.md"),
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["totalFiles"] == 1
    assert ".." not in data["chapters"][0]["relativePath"]


def test_temp_directory_is_cleaned_up(monkeypatch, tracked_temp_dirs):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章", "开头。"))),
        )

    assert response.status_code == 200
    assert tracked_temp_dirs, "应当创建过临时目录"
    assert all(not path.exists() for path in tracked_temp_dirs), "临时目录必须清理干净"


def test_temp_directory_is_cleaned_up_even_on_failure(monkeypatch, tracked_temp_dirs):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            SCAN_URL,
            headers=OWNER,
            files=[("files", ("第01章.txt", b"x", "text/plain"))],
        )

    assert response.status_code == 400
    assert all(not path.exists() for path in tracked_temp_dirs), "失败路径也要清理"


# ── 入库 ──────────────────────────────────────────────────

def _create_project(client: TestClient, name: str = "上传导入目标") -> str:
    created = client.post(
        "/api/projects/",
        headers=OWNER,
        json={"name": name, "genre": "xuanhuan", "targetWordCount": 30000, "stylePreset": "epic"},
    )
    assert created.status_code == 200, created.text
    return created.json()["data"]["id"]


def _chapters_of(client: TestClient, project_id: str) -> list[dict]:
    response = client.get(f"/api/projects/{project_id}/chapters/", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_upload_ingest_writes_chapters_in_order(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(
                ("第02章 旧档.md", _md("第二章 旧档", "档案被人动过。")),
                ("第01章 雨夜.md", _md("第一章 雨夜", "雨下了一整夜。")),
            ),
            data={**_paths("第02章 旧档.md", "第01章 雨夜.md"), "projectId": project_id},
        )

        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        assert payload["importedChapters"] == 2

        chapters = _chapters_of(client, project_id)

    assert [chapter["title"] for chapter in chapters] == ["第一章 雨夜", "第二章 旧档"]
    assert [chapter["order"] for chapter in chapters] == [1, 2]


def test_import_outline_structure_uses_the_declared_enum_spelling(monkeypatch):
    """导入生成的大纲结构必须落在 `schemas.Outline` 声明的枚举写法里。

    这里曾经写成 `"three-act"`（连字符），而声明是 `Literal["three_act", ...]`（下划线）。
    前端按枚举查中文名会因此漏掉，把内部值 `three-act` 原样显示给作者
    （用户报告：大纲页「结构」卡片上写着英文）。

    断言取值来源是**声明本身**，而不是测试里另抄一份常量——否则两边一起漂移就测不出来。
    """
    from typing import get_args

    from app.models.schemas import GenerateOutlineRequest

    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章 雨夜.md", _md("第一章 雨夜", "雨下了一整夜。"))),
            data={**_paths("第01章 雨夜.md"), "projectId": project_id},
        )
        assert response.status_code == 200, response.text
        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]

    structure = project["outline"]["structure"]
    allowed = set(get_args(GenerateOutlineRequest.model_fields["structure"].annotation))
    assert structure in allowed, f"{structure!r} 不在声明的枚举 {sorted(allowed)} 中"
    assert "-" not in structure, "枚举写法用下划线，不要用连字符"


def test_import_outline_nodes_point_at_real_chapters(monkeypatch):
    """导入生成的大纲节点 id 必须是**真实章节 id**。

    这里曾用 `gen_id()` 另生成一个 id，而章节行是以扫描阶段的 `chapter["id"]` 写入的，
    两套 id 从一开始就对不上：章节导航能列出很多章，但点进去逐章空白
    （前端 `GET /chapters/{node.id}` 拿到 404），而章节行其实完好。
    实测线上某项目：114 章全有正文，节点 id 匹配数为 0。

    所以断言不能只看数量相等，必须**逐个节点真的能取到正文**。
    """
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(
                ("第02章 旧档.md", _md("第二章 旧档", "档案被人动过。")),
                ("第01章 雨夜.md", _md("第一章 雨夜", "雨下了一整夜。")),
            ),
            data={**_paths("第02章 旧档.md", "第01章 雨夜.md"), "projectId": project_id},
        )
        assert response.status_code == 200, response.text
        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]
        nodes = [node for node in project["outline"]["nodes"] if node["type"] == "chapter"]
        assert len(nodes) == 2

        for node in nodes:
            detail = client.get(f"/api/projects/{project_id}/chapters/{node['id']}", headers=OWNER)
            assert detail.status_code == 200, f"「{node['title']}」的节点 id 指向了不存在的章节"
            assert detail.json()["data"]["content"].strip(), f"「{node['title']}」正文为空"

        chapters = _chapters_of(client, project_id)

    assert {node["id"] for node in nodes} == {chapter["id"] for chapter in chapters}
    # 按章节顺序对应，标题不能错位
    assert [node["title"] for node in nodes] == ["第一章 雨夜", "第二章 旧档"]


def test_upload_ingest_creates_project_named_after_folder(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))),
            data=_paths("雨夜长卷/第01章.md"),
        )

        assert response.status_code == 200, response.text
        project = response.json()["data"]["project"]
        chapters = _chapters_of(client, project["id"])

    assert project["name"] == "雨夜长卷"
    assert len(chapters) == 1


def test_upload_ingest_honours_explicit_project_name(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章", "开头。"))),
            data={**_paths("随便/第01章.md"), "projectName": "我指定的名字"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["project"]["name"] == "我指定的名字"


def test_upload_ingest_rejects_non_owner(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        response = client.post(
            INGEST_URL,
            headers=OTHER,
            files=_files(("第01章.md", _md("第一章", "开头。"))),
            data={**_paths("第01章.md"), "projectId": project_id},
        )

    assert response.status_code == 403


def test_upload_ingest_requires_authentication(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        response = client.post(
            INGEST_URL,
            files=_files(("第01章.md", _md("第一章", "开头。"))),
            data={**_paths("第01章.md"), "projectId": project_id},
        )

    assert response.status_code == 401


def test_upload_ingest_replaces_previous_chapters(monkeypatch):
    """导入到已有项目会先回收旧章节（与路径模式一致），这属于既有行为。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章 旧", "旧内容。"))),
            data={**_paths("第01章.md"), "projectId": project_id},
        )
        client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(
                ("第01章.md", _md("第一章 新", "新内容。")),
                ("第02章.md", _md("第二章 新", "又一段。")),
            ),
            data={**_paths("第01章.md", "第02章.md"), "projectId": project_id},
        )

        chapters = _chapters_of(client, project_id)

    assert [chapter["title"] for chapter in chapters] == ["第一章 新", "第二章 新"]


def test_upload_ingest_builds_outline_from_imported_chapters(monkeypatch):
    """导入必须顺带生成大纲：章节导航渲染的是 outline.nodes，不是 chapters。

    回归背景：`projects.outline` 曾被硬编码写成 "null"，于是导入上百章之后
    「章节导航」依然一片空白（真实作品 114 章 / outline=null 即此形态），
    作者无法从导航进入刚导入的章节。
    """
    _configure(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(
                ("第02章 旧档.md", _md("第二章 旧档", "档案被人动过。")),
                ("第01章 雨夜.md", _md("第一章 雨夜", "雨下了一整夜。")),
            ),
            data=_paths("雨夜长卷/第02章 旧档.md", "雨夜长卷/第01章 雨夜.md"),
        )

        assert response.status_code == 200, response.text
        created = response.json()["data"]["project"]
        # 入库响应本身就要带上大纲，前端用它直接刷新章节导航
        assert created["outline"], "入库响应里的 outline 不能为空"
        refreshed = client.get(f"/api/projects/{created['id']}", headers=OWNER)
        assert refreshed.status_code == 200, refreshed.text
        outline = refreshed.json()["data"]["outline"]

    assert outline, "导入后 outline 不能为空，否则章节导航没有任何内容"
    nodes = outline["nodes"]
    # 每章一个节点，顺序与章节一致（seed 顺序被打乱也要按章节号排好）
    assert [node["title"] for node in nodes] == ["第一章 雨夜", "第二章 旧档"]
    assert [node["order"] for node in nodes] == [1, 2]
    assert [node["type"] for node in nodes] == ["chapter", "chapter"]
    # 不虚构卷/弧：顶层平铺，前端 ChapterTree 会归到「大纲章节」下
    assert {node["parentId"] for node in nodes} == {None}
    # 节点 id 必须唯一，否则导航会互相覆盖
    assert len({node["id"] for node in nodes}) == len(nodes)


def test_upload_ingest_outline_survives_reimport(monkeypatch):
    """重复导入（替换已有章节）后，大纲必须跟着更新，不能留着上一次的陈旧章节。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章 旧", "旧内容。"))),
            data={**_paths("第01章.md"), "projectId": project_id},
        )
        client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章 新", "新内容。"))),
            data={**_paths("第01章.md"), "projectId": project_id},
        )

        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]

    assert [node["title"] for node in project["outline"]["nodes"]] == ["第一章 新"]


# ── 作者舍弃误识别的条目 ────────────────────────────────


def _scan_result_with_analysis() -> dict:
    return {
        "chapters": [{"title": "第一章 雨夜"}, {"title": "第二章 旧档"}],
        "analysis": {
            "characters": [
                {"name": "林晚", "frequency": 5},
                {"name": "雨夜", "frequency": 3},
            ],
            "locations": [
                {"name": "教学楼", "frequency": 4},
                {"name": "三天", "frequency": 2},
            ],
            "timeline": [
                {"text": "三年前", "type": "past", "chapter": 1},
                {"text": "雨下了一整夜", "type": "scene", "chapter": 1},
            ],
            "foreshadowing": [
                {"text": "半份手稿", "category": "道具", "context": "档案被人动过。"},
                {"text": "雨", "category": "意象", "context": "雨下了一整夜。"},
            ],
            "summary": "已分析 2 章，识别出 2 个角色、2 个地点、2 个时间节点。",
        },
    }


def test_excluded_entries_are_removed_across_all_four_categories():
    """四类（人物/地点/时间线/伏笔）都只删不补，摘要计数同步重算。"""
    from app.api.imports import _apply_analysis_exclusions

    scan_result = _scan_result_with_analysis()
    _apply_analysis_exclusions(
        scan_result,
        json.dumps(
            {
                "characters": ["雨夜"],
                "locations": ["三天"],
                "timeline": ["雨下了一整夜"],
                "foreshadowing": ["雨"],
            }
        ),
    )

    analysis = scan_result["analysis"]
    assert [item["name"] for item in analysis["characters"]] == ["林晚"]
    assert [item["name"] for item in analysis["locations"]] == ["教学楼"]
    assert [item["text"] for item in analysis["timeline"]] == ["三年前"]
    assert [item["text"] for item in analysis["foreshadowing"]] == ["半份手稿"]
    assert scan_result["excludedAnalysis"] == {
        "characters": ["雨夜"],
        "locations": ["三天"],
        "timeline": ["雨下了一整夜"],
        "foreshadowing": ["雨"],
    }
    # 摘要必须跟着变，否则「识别出 2 个角色」和列表里的 1 条自相矛盾
    assert "1 个角色" in analysis["summary"]
    assert "1 个地点" in analysis["summary"]
    assert "1 个时间节点" in analysis["summary"]


def test_partial_exclusion_only_touches_the_named_category():
    """只给了人物名单时，地点/时间线/伏笔一条都不能被误伤。"""
    from app.api.imports import _apply_analysis_exclusions

    scan_result = _scan_result_with_analysis()
    _apply_analysis_exclusions(scan_result, '{"characters": ["雨夜"]}')

    analysis = scan_result["analysis"]
    assert [item["name"] for item in analysis["characters"]] == ["林晚"]
    assert len(analysis["locations"]) == 2
    assert len(analysis["timeline"]) == 2
    assert len(analysis["foreshadowing"]) == 2
    assert list(scan_result["excludedAnalysis"]) == ["characters"]


def test_exclusion_payload_is_never_allowed_to_break_the_import():
    """脏名单一律忽略：导入不能因为一个畸形参数就失败或改结果。"""
    from app.api.imports import _apply_analysis_exclusions

    dirty_payloads = (
        "",
        "   ",
        "not json",
        "null",
        "[]",  # 不是对象
        '{"characters": "雨夜"}',  # 值不是数组
        '{"characters": [123]}',  # 条目不是字符串
        '{"characters": [""]}',  # 空字符串
        "{}",  # 空对象
        '{"unknownCategory": ["雨夜"]}',  # 不认识的分类
    )
    for payload in dirty_payloads:
        scan_result = _scan_result_with_analysis()
        _apply_analysis_exclusions(scan_result, payload)

        analysis = scan_result["analysis"]
        assert len(analysis["characters"]) == 2, f"{payload!r} 不该改动结果"
        assert "2 个角色" in analysis["summary"], f"{payload!r} 不该改动摘要"
        assert scan_result.get("excludedAnalysis") is None, f"{payload!r} 不该留下痕迹"


def test_exclusions_tolerate_dirty_entries():
    """条目里混入非 dict / 空字段时不能抛错，也不能误删。"""
    from app.api.imports import _apply_analysis_exclusions

    scan_result = _scan_result_with_analysis()
    # 插到最前面：结果为 无名条目、None、林晚、雨夜
    scan_result["analysis"]["characters"].insert(0, None)
    scan_result["analysis"]["characters"].insert(0, {"name": ""})
    _apply_analysis_exclusions(scan_result, '{"characters": ["雨夜"]}')

    names = [item["name"] for item in scan_result["analysis"]["characters"] if isinstance(item, dict)]
    assert names == ["", "林晚"]


def test_ingest_applies_excluded_analysis(monkeypatch):
    """端到端：入库带的舍弃名单要落到 import_analysis 里。"""
    _configure(monkeypatch)
    from app.services import import_service

    # 固定识别结果，避免依赖 jieba 启发式在小样本上是否命中
    monkeypatch.setattr(
        import_service,
        "_extract_characters",
        lambda text: [{"name": "林晚", "frequency": 5}, {"name": "雨夜", "frequency": 3}],
    )

    with TestClient(app) as client:
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))),
            data={
                **_paths("第01章.md"),
                "excludedAnalysis": '{"characters": ["雨夜"]}',
            },
        )

        assert response.status_code == 200, response.text
        project = response.json()["data"]["project"]
        analysis = project["importAnalysis"]
        names = [item["name"] for item in analysis["characters"]]

    assert names == ["林晚"], "被舍弃的条目不该进入入库结果"
    assert "1 个角色" in analysis["summary"]


# ── 写入方式：完全替换 / 增量合并 ─────────────────────────
#
# 背景：导入到已有项目曾**只有一种**行为 —— 把当前项目全部章节移入回收站再写新内容。
# 作者若只改了几章后重导整个文件夹，导入文件里没有的章节同样会被清掉；而且这些记录
# 到了回收站也点不回来（章节已不在大纲里）。
# 现在由作者在导入面板上选：完全替换（历史行为）/ 增量合并（按**章节序号**对齐）。


def _ingest(client: TestClient, project_id: str, items, *, merge_mode: str | None = None, excluded: dict | None = None):
    """items: [(文件名, 正文)]。不传 merge_mode 时走接口默认值（完全替换）。"""
    data = {"projectId": project_id, **_paths(*[name for name, _ in items]), **(excluded or {})}
    if merge_mode is not None:
        data["mergeMode"] = merge_mode
    return client.post(INGEST_URL, headers=OWNER, files=_files(*items), data=data)


def _characters_of(client: TestClient, project_id: str) -> list[dict]:
    """「人物」页读的就是 project.characters。"""
    response = client.get(f"/api/projects/{project_id}", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()["data"]["characters"]


def _trash_of(client: TestClient, project_id: str) -> list[dict]:
    response = client.get(f"/api/projects/{project_id}/chapter-trash", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()["data"]["records"]


def _seed_three_chapters(client: TestClient, project_id: str) -> dict[str, dict]:
    response = _ingest(
        client,
        project_id,
        [
            ("第01章.md", _md("第一章 雨夜", "雨下了一整夜。")),
            ("第02章.md", _md("第二章 旧档", "档案被人动过。")),
            ("第03章.md", _md("第三章 借炉", "炉火还亮着。")),
        ],
    )
    assert response.status_code == 200, response.text
    return {chapter["title"]: chapter for chapter in _chapters_of(client, project_id)}


def test_merge_replaces_only_the_chapters_present_in_the_import(monkeypatch):
    """增量合并：命中的章节被替换，导入里没有的章节原样留在项目里。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        before = _seed_three_chapters(client, project_id)

        response = _ingest(
            client,
            project_id,
            [("第01章.md", _md("第一章 雨夜（改）", "雨停了，天亮得很慢。"))],
            merge_mode="merge",
        )
        assert response.status_code == 200, response.text
        payload = response.json()["data"]

        after = {chapter["title"]: chapter for chapter in _chapters_of(client, project_id)}
        records = _trash_of(client, project_id)

    # 章节数不变：被保留的两章还在，命中的那一章换了内容
    assert sorted(after) == ["第一章 雨夜（改）", "第三章 借炉", "第二章 旧档"]
    # 入库的正文是整份文件内容（标题行也在里面），标题另有字段
    assert after["第一章 雨夜（改）"]["content"] == "# 第一章 雨夜（改）\n\n雨停了，天亮得很慢。"
    # 没被导入提到的章节必须**逐字**保持原样（含 id —— 派生数据靠它挂靠）
    assert after["第二章 旧档"]["id"] == before["第二章 旧档"]["id"]
    assert after["第二章 旧档"]["content"] == before["第二章 旧档"]["content"]
    assert after["第三章 借炉"]["id"] == before["第三章 借炉"]["id"]

    # 后端如实报告各类数量，而不是笼统说「已导入 1 章」
    assert payload["mergeMode"] == "merge"
    assert payload["addedChapters"] == 0
    assert payload["updatedChapters"] == 1
    assert payload["keptChapters"] == 2
    assert payload["recycledChapters"] == 1

    # 被替换的旧正文进了回收站，原因能区分出是「增量合并」
    assert len(records) == 1
    assert records[0]["reason"] == "import_merged"
    assert records[0]["title"] == "第一章 雨夜"
    # 存档的字数与它在项目里时一致（正文没被截断）
    assert records[0]["wordCount"] == before["第一章 雨夜"]["wordCount"]


def test_merged_away_chapter_can_still_be_read_from_the_trash(monkeypatch):
    """被合并覆盖的旧正文必须能回查 —— 覆盖作者写过的正文不能是静默的。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _seed_three_chapters(client, project_id)
        _ingest(
            client,
            project_id,
            [("第01章.md", _md("第一章 雨夜（改）", "雨停了，天亮得很慢。"))],
            merge_mode="merge",
        )

        trash_id = _trash_of(client, project_id)[0]["id"]
        detail = client.get(f"/api/projects/{project_id}/chapter-trash/{trash_id}", headers=OWNER)

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["content"] == "# 第一章 雨夜\n\n雨下了一整夜。"


def test_merge_adds_orders_the_project_does_not_have_yet(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _seed_three_chapters(client, project_id)

        response = _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜（改）", "改过的第一章。")),
                ("第02章.md", _md("第二章 旧档（改）", "改过的第二章。")),
                ("第03章.md", _md("第三章 借炉（改）", "改过的第三章。")),
                ("第04章.md", _md("第四章 后窗", "新加的第四章。")),
                ("第05章.md", _md("第五章 笔录", "新加的第五章。")),
            ],
            merge_mode="merge",
        )
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)

    assert [chapter["order"] for chapter in chapters] == [1, 2, 3, 4, 5]
    assert [chapter["title"] for chapter in chapters] == [
        "第一章 雨夜（改）", "第二章 旧档（改）", "第三章 借炉（改）", "第四章 后窗", "第五章 笔录",
    ]
    assert payload["updatedChapters"] == 3
    assert payload["addedChapters"] == 2
    assert payload["keptChapters"] == 0


def test_merge_outline_points_at_every_surviving_chapter(monkeypatch):
    """合并后的大纲必须覆盖**项目里的全部章节**，否则被保留的章节会点不开。

    这正是「完全替换顺手重建大纲」在合并下会犯的错：导入里没有的章节会从导航里消失
    （正文还在，作者却进不去）。
    """
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        before = _seed_three_chapters(client, project_id)
        _ingest(
            client,
            project_id,
            [("第01章.md", _md("第一章 雨夜（改）", "雨停了。"))],
            merge_mode="merge",
        )

        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]
        outline = project["outline"]
        chapters = _chapters_of(client, project_id)
        titles = {}
        for node in outline["nodes"]:
            fetched = client.get(f"/api/projects/{project_id}/chapters/{node['id']}", headers=OWNER)
            titles[node["title"]] = fetched.status_code

    assert {node["id"] for node in outline["nodes"]} == {chapter["id"] for chapter in chapters}
    assert len({node["id"] for node in outline["nodes"]}) == len(outline["nodes"])
    # 被保留章节的节点必须仍指向**原来那一行**，不能被重建成新 id
    by_title = {node["title"]: node for node in outline["nodes"]}
    assert by_title["第二章 旧档"]["id"] == before["第二章 旧档"]["id"]
    assert by_title["第三章 借炉"]["id"] == before["第三章 借炉"]["id"]
    # 每个节点都要能真打开（这是作者报告的那个 404 空白）
    assert set(titles.values()) == {200}


def test_merge_recounts_words_and_target_chapters_for_the_whole_project(monkeypatch):
    """合并后的项目字数/目标章数必须按合并结果算，不能只看这次导入的内容。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _seed_three_chapters(client, project_id)
        _ingest(
            client,
            project_id,
            [("第01章.md", _md("第一章 雨夜（改）", "雨停了，天亮得很慢。"))],
            merge_mode="merge",
        )

        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]
        chapters = _chapters_of(client, project_id)

    assert project["currentWordCount"] == sum(chapter["wordCount"] for chapter in chapters)
    assert project["targetChapters"] == len(chapters) == 3


def test_replace_stays_the_default_and_still_trashes_every_previous_chapter(monkeypatch):
    """不传写入方式时行为与历史一致：旧章节全部进回收站，项目里只剩这次导入的内容。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _seed_three_chapters(client, project_id)

        response = _ingest(client, project_id, [("第01章.md", _md("第一章 全新", "全新内容。"))])
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)
        records = _trash_of(client, project_id)

    assert payload["mergeMode"] == "replace"
    assert payload["recycledChapters"] == 3
    assert payload["updatedChapters"] == 0
    assert payload["keptChapters"] == 0
    assert payload["addedChapters"] == 1
    assert [chapter["title"] for chapter in chapters] == ["第一章 全新"]
    assert {record["reason"] for record in records} == {"import_replaced"}


def test_an_unknown_merge_mode_is_rejected_before_anything_is_written(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        before = _seed_three_chapters(client, project_id)

        response = _ingest(
            client,
            project_id,
            [("第01章.md", _md("第一章 雨夜（改）", "改过的第一章。"))],
            merge_mode="purge",
        )

        chapters = _chapters_of(client, project_id)
        records = _trash_of(client, project_id)

    assert response.status_code == 400, response.text
    assert "完全替换" in response.json()["detail"]
    # 校验发生在任何写入之前：章节与回收站都不该有变化
    assert [chapter["title"] for chapter in chapters] == ["第一章 雨夜", "第二章 旧档", "第三章 借炉"]
    assert before["第一章 雨夜"]["id"] in {chapter["id"] for chapter in chapters}
    assert records == []


# ── 合并的大纲对齐（纯函数） ──────────────────────────────


def _row(chapter_id: str, title: str, order: int, word_count: int) -> dict:
    return {"id": chapter_id, "title": title, "chapter_order": order, "word_count": word_count}


def test_merge_outline_keeps_author_structure_and_only_refreshes_chapter_nodes():
    """卷/弧、手工摘要、关键点、POV 都要留住；只把命中序号的节点指向新章节。"""
    from app.api.imports import _merge_import_outline

    existing = json.dumps(
        {
            "mainSummary": "作者自己写的主线摘要。",
            "structure": "hero_journey",
            "outlineVersion": 7,
            "nodes": [
                {"id": "v1", "parentId": None, "type": "volume", "title": "第一卷", "order": 1},
                {
                    "id": "old-ch1",
                    "parentId": "v1",
                    "type": "chapter",
                    "title": "第一章 雨夜",
                    "order": 1,
                    "summary": "作者手写的章节摘要。",
                    "isKeyPoint": True,
                    "povCharacterId": "c-lin",
                    "targetWordCount": 10,
                },
                {
                    "id": "kept-ch2",
                    "parentId": "v1",
                    "type": "chapter",
                    "title": "第二章 旧档",
                    "order": 2,
                    "summary": "",
                    "isKeyPoint": False,
                    "povCharacterId": None,
                    "targetWordCount": 20,
                },
            ],
        },
        ensure_ascii=False,
    )

    merged = json.loads(
        _merge_import_outline(
            existing,
            # 第 1 章被替换成新内容；第 3 章是项目里的新章节
            [_row("new-ch1", "第一章 雨夜（改）", 1, 123), _row("kept-ch2", "第二章 旧档", 2, 20), _row("new-ch3", "第三章 借炉", 3, 45)],
        )
    )

    nodes = {node["id"]: node for node in merged["nodes"]}
    # 结构、版本、主线摘要都不动：作者选的是英雄之旅，不能被导入改成三幕式
    assert merged["structure"] == "hero_journey"
    assert merged["outlineVersion"] == 7
    assert merged["mainSummary"] == "作者自己写的主线摘要。"
    # 卷节点原样保留
    assert nodes["v1"]["type"] == "volume"
    # 命中序号的节点换了 id/title/字数，但归属卷与手工字段全部留住
    assert nodes["new-ch1"]["parentId"] == "v1"
    assert nodes["new-ch1"]["title"] == "第一章 雨夜（改）"
    assert nodes["new-ch1"]["targetWordCount"] == 123
    assert nodes["new-ch1"]["summary"] == "作者手写的章节摘要。"
    assert nodes["new-ch1"]["isKeyPoint"] is True
    assert nodes["new-ch1"]["povCharacterId"] == "c-lin"
    assert "old-ch1" not in nodes
    # 没被导入提到的章节节点保持不变
    assert nodes["kept-ch2"]["title"] == "第二章 旧档"
    # 项目里新出现的章节补一个平铺节点
    assert nodes["new-ch3"]["type"] == "chapter"
    assert nodes["new-ch3"]["parentId"] is None
    assert nodes["new-ch3"]["order"] == 3


def test_merge_outline_falls_back_to_a_flat_outline_without_a_usable_one():
    from app.api.imports import _merge_import_outline

    rows = [_row("c1", "第一章", 1, 5), _row("c2", "第二章", 2, 6)]

    for broken in (None, "", "{not json", "[]"):
        outline = json.loads(_merge_import_outline(broken, rows))
        assert [node["id"] for node in outline["nodes"]] == ["c1", "c2"]
        assert [node["order"] for node in outline["nodes"]] == [1, 2]
        # 补上导入一贯的结构写法，避免「结构」卡片空白
        assert outline["structure"] == "three_act"


# ── 合并对齐：文件名里的章节号 ───────────────────────────
#
# 作者指定：增量合并按**文件名里的章节号**对齐（第几章），不看标题、也不看导入顺序，
# 这样才能只导一部分文件。代价是「上一次按顺序编号」的库里会留下旧编号残影
# （文件夹是 1、2、4 时曾被编成 1、2、3）。下面既验证按号对齐，也验证残影能被认出来
# 并回收 —— 以及认不出来时宁可不动手。


def test_chapter_number_reads_the_number_from_the_file_name():
    from app.api.imports import _chapter_number

    cases = {
        "第10章 后窗.md": 10,
        "第001章 雨夜.md": 1,
        "第 3 节 归途.md": 3,
        "第2卷 第10章 旧档.md": 10,  # 不能被卷号骗走
        "010-雨夜.md": 10,
        "10.雨夜.md": 10,
        "雨夜-002.md": 2,
        "ch1-02.md": 2,
        "雨夜.md": None,  # 没有数字：调用方退回按导入顺序
        "第一章 雨夜.md": None,  # 中文数字不猜
    }
    for file_name, expected in cases.items():
        assert _chapter_number({"fileName": file_name}) == expected, file_name


def test_merge_aligns_on_the_number_in_the_file_name_not_the_import_position(monkeypatch):
    """只导「第03章」时必须替换第 3 章 —— 按导入顺序对齐会错改成第 1 章。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        before = _seed_three_chapters(client, project_id)

        response = _ingest(
            client,
            project_id,
            [("第03章 借炉.md", _md("第三章 借炉（改）", "改过的第三章。"))],
            merge_mode="merge",
        )
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)

    assert payload["mergeKey"] == "number"
    assert (payload["updatedChapters"], payload["keptChapters"], payload["addedChapters"]) == (1, 2, 0)
    by_title = {chapter["title"]: chapter for chapter in chapters}
    assert "改过的第三章。" in by_title["第三章 借炉（改）"]["content"]
    # 前两章一个字都没动（按顺序对齐就会把第 1 章错改成第三章的内容）
    assert by_title["第一章 雨夜"]["id"] == before["第一章 雨夜"]["id"]
    assert by_title["第二章 旧档"]["id"] == before["第二章 旧档"]["id"]
    assert [chapter["order"] for chapter in chapters] == [1, 2, 3]


def _force_sequential_orders(project_id: str) -> None:
    """把章节序号改写成「旧版按导入顺序编号」的样子（1..N 连续）。

    旧版把 1、2、4 号文件编成了 1、2、3 号 —— 这是升级前真实存在过的形态，
    用一条 UPDATE 造出来比绕接口更直接。
    """
    from app.db import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM chapters WHERE project_id = ? ORDER BY chapter_order ASC, created_at ASC",
            (project_id,),
        ).fetchall()
        for rank, row in enumerate(rows, start=1):
            conn.execute("UPDATE chapters SET chapter_order = ? WHERE id = ?", (rank, row["id"]))


def test_merge_cleans_up_leftovers_from_the_previous_sequential_numbering(monkeypatch):
    """文件夹缺号时旧编号会留下重复章节，合并要认出来并回收（而不是留个副本）。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜", "雨下了一整夜。")),
                ("第02章.md", _md("第二章 旧档", "档案被人动过。")),
                ("第04章.md", _md("第四章 后窗", "后窗开着。")),
            ],
        )
        _force_sequential_orders(project_id)
        assert [chapter["order"] for chapter in _chapters_of(client, project_id)] == [1, 2, 3]

        response = _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜", "雨下了一整夜，天亮得很慢。")),
                ("第02章.md", _md("第二章 旧档", "档案被人动过两次。")),
                ("第04章.md", _md("第四章 后窗", "后窗一直开着。")),
            ],
            merge_mode="merge",
        )
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)
        records = _trash_of(client, project_id)

    assert payload["mergeKey"] == "number"
    assert (
        payload["updatedChapters"],
        payload["addedChapters"],
        payload["cleanedChapters"],
        payload["keptChapters"],
    ) == (2, 1, 1, 0)
    # 清理了哪一章要能一眼看到（编号 + 标题），不能只说「清理了 1 章」
    assert payload["cleanedDetails"] == [{"order": 3, "title": "第四章 后窗"}]
    # 结果里没有重复章节：编号跟着文件名走（1、2、4），旧的 3 号不再存在
    assert [chapter["order"] for chapter in chapters] == [1, 2, 4]
    assert [chapter["title"] for chapter in chapters] == ["第一章 雨夜", "第二章 旧档", "第四章 后窗"]
    # 被清理的那一章进了回收站（可回查），不是静默删掉
    assert {record["title"] for record in records} == {"第一章 雨夜", "第二章 旧档", "第四章 后窗"}
    assert {record["reason"] for record in records} == {"import_merged"}


def test_a_legacy_leftover_whose_title_changed_is_kept_and_reported(monkeypatch):
    """标题也改过的残影不敢认定成同一章：只保留，并在计数里如实体现。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜", "雨下了一整夜。")),
                ("第02章.md", _md("第二章 旧档", "档案被人动过。")),
                ("第04章.md", _md("第四章 后窗", "后窗开着。")),
            ],
        )
        _force_sequential_orders(project_id)

        response = _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜", "雨下了一整夜，天亮得很慢。")),
                ("第02章.md", _md("第二章 旧档", "档案被人动过两次。")),
                ("第04章.md", _md("第四章 后窗（改）", "后窗一直开着。")),
            ],
            merge_mode="merge",
        )
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)

    assert payload["cleanedChapters"] == 0
    assert payload["keptChapters"] == 1
    # 旧的 3 号（第四章 后窗）留在项目里，新的第 4 章另起一号 —— 没有丢数据，
    # 作者看到「保留 1 章」可以自行判断要不要删掉那一章
    assert [chapter["order"] for chapter in chapters] == [1, 2, 3, 4]
    assert {chapter["title"] for chapter in chapters} == {"第一章 雨夜", "第二章 旧档", "第四章 后窗", "第四章 后窗（改）"}


def test_merge_falls_back_to_the_import_order_when_a_file_has_no_number(monkeypatch):
    """文件名里取不到章节号时整体退回导入顺序对齐，并把这个依据告诉前端。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _seed_three_chapters(client, project_id)

        response = _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜（改）", "改过的第一章。")),
                ("尾声.md", _md("尾声 后来", "后来他们都老了。")),
            ],
            merge_mode="merge",
        )
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)

    assert payload["mergeKey"] == "index"
    # 退回顺序对齐时「第 2 个文件」会顶掉项目里的第 2 章 —— 这正是有号时一定要按号对齐的原因
    assert payload["updatedChapters"] == 2
    assert payload["keptChapters"] == 1
    assert [chapter["order"] for chapter in chapters] == [1, 2, 3]
    assert {chapter["title"] for chapter in chapters} == {"第一章 雨夜（改）", "尾声 后来", "第三章 借炉"}


def test_duplicate_chapter_numbers_fall_back_to_the_import_order(monkeypatch):
    """同一个章节号出现两次是二义的，整体退回导入顺序而不是任选一个。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        _seed_three_chapters(client, project_id)

        response = _ingest(
            client,
            project_id,
            [
                ("第01章.md", _md("第一章 雨夜", "雨下了一整夜。")),
                ("第01章 备份.md", _md("第一章 雨夜（备份）", "另一份草稿。")),
            ],
            merge_mode="merge",
        )
        payload = response.json()["data"]
        chapters = _chapters_of(client, project_id)

    assert payload["mergeKey"] == "index"
    assert [chapter["order"] for chapter in chapters] == [1, 2, 3]


def test_residue_detection_refuses_every_ambiguous_case():
    """残影识别是「敢删作者章节」的地方，四条护栏逐条钉住。"""
    from app.api.imports import _residue_orders

    previous = [(1, "第一章"), (2, "第二章"), (3, "第四章 后窗")]

    # 正常残影：旧编号连续、章数相同、名次配对后标题一致
    assert _residue_orders(
        previous=previous,
        incoming=[(1, "第一章"), (2, "第二章"), (4, "第四章 后窗")],
    ) == [3]

    # 换了一批文件（章数相同但标题对不上）：不动
    assert _residue_orders(previous=previous, incoming=[(20, "甲"), (21, "乙"), (22, "丙")]) == []
    # 章数不同（文件夹变长或变短）：不动
    assert _residue_orders(
        previous=previous,
        incoming=[(1, "第一章"), (2, "第二章"), (4, "第四章 后窗"), (5, "第五章")],
    ) == []
    # 既有编号已经不连续（说明早就是按号编号）：没有残影可认
    assert _residue_orders(
        previous=[(1, "第一章"), (2, "第二章"), (4, "第四章 后窗")],
        incoming=[(1, "第一章"), (2, "第二章"), (4, "第四章 后窗")],
    ) == []
    # 标题改过：不算残影
    assert _residue_orders(
        previous=previous,
        incoming=[(1, "第一章"), (2, "第二章"), (4, "第四章 后窗（改）")],
    ) == []
    # 标题为空（未命名章节）：空对空不算证据
    assert _residue_orders(previous=[(1, ""), (2, ""), (3, "")], incoming=[(1, ""), (2, ""), (4, "")]) == []


# ── 导入把识别到的人物补进「人物」页 ─────────────────────
#
# 背景：人物面板读的是 `projects.characters`，而导入结果此前只写进 `import_analysis`，
# 于是导入完一整本书，人物页里仍然只有建项目时自动生成的主角（作者报告）。
# 识别是词频/词性启发式、必然有误判，所以规则是「只增不改 + 只搬事实」。


def _patch_characters(monkeypatch, *entries: tuple[str, int]) -> None:
    from app.services import import_service

    monkeypatch.setattr(
        import_service,
        "_extract_characters",
        lambda text: [{"name": name, "frequency": count, "aliases": []} for name, count in entries],
    )


def test_import_reports_candidates_without_writing_any_character(monkeypatch):
    """导入**不写** `projects.characters`，只报告识别到几位候选。

    背景（作者决定）：识别是词频/词性启发式，20 个候选里混进「毛巾」「安静」这种误判
    是常态。自动建档等于把垃圾数据变成作者必须清理的负担，所以候选只留在
    `import_analysis` 里，由「人物」页勾选后才落库。
    """
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12), ("雨夜", 3))

    with TestClient(app) as client:
        project_id = _create_project(client)
        response = _ingest(client, project_id, [("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))])
        assert response.status_code == 200, response.text
        payload = response.json()["data"]

        assert _characters_of(client, project_id) == [], "导入后人物页必须原样为空"
        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]

    assert payload["charactersDetected"] == 2
    assert "charactersAdded" not in payload, "自动写入这条路已经拆掉，字段不能留着骗人"
    # 候选本身要留住，否则「人物」页无从勾选
    assert [item["name"] for item in project["importAnalysis"]["characters"]] == ["林晚", "雨夜"]


def test_a_new_project_from_an_import_also_starts_with_no_characters(monkeypatch):
    """新建项目这条路同样不写：候选等着作者挑，而不是替他建档。"""
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12))

    with TestClient(app) as client:
        response = client.post(
            INGEST_URL,
            headers=OWNER,
            files=_files(("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))),
            data=_paths("雨夜长卷/第01章.md"),
        )
        assert response.status_code == 200, response.text
        created = response.json()["data"]["project"]

    assert created["characters"] == []
    assert [item["name"] for item in created["importAnalysis"]["characters"]] == ["林晚"]


def test_a_discarded_character_never_becomes_a_candidate(monkeypatch):
    """作者在导入预览里叉掉的误识别条目，不该再出现在人物页的候选里。"""
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12), ("雨夜", 3))

    with TestClient(app) as client:
        project_id = _create_project(client)
        response = _ingest(
            client,
            project_id,
            [("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))],
            excluded={"excludedAnalysis": '{"characters": ["雨夜"]}'},
        )
        project = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]

    assert response.json()["data"]["charactersDetected"] == 1
    assert [item["name"] for item in project["importAnalysis"]["characters"]] == ["林晚"]


# ── 勾选候选入库（「人物」页的那块面板） ─────────────────────────


def _accept_candidates(client: TestClient, project_id: str, names: list[str], headers: dict | None = None):
    return client.post(
        f"/api/import/projects/{project_id}/characters/from-analysis",
        headers=headers or OWNER,
        json={"names": names},
    )


def test_selected_candidates_are_added_with_facts_only(monkeypatch):
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12), ("雨夜", 3))

    with TestClient(app) as client:
        project_id = _create_project(client)
        _ingest(client, project_id, [("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))])

        response = _accept_candidates(client, project_id, ["林晚"])
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        characters = body["project"]["characters"]

    assert body["added"] == 1
    assert [item["name"] for item in characters] == ["林晚"], "没勾选的「雨夜」不能跟着进来"
    # 只搬事实：出现次数写进背景，并明说这是启发式识别、要人工确认
    assert "出现 12 次" in characters[0]["background"]
    assert "请人工确认" in characters[0]["background"]
    # 不编造：性别/外貌/性格/年龄一律留空（写 0 岁也是假事实）
    assert characters[0]["basicInfo"]["gender"] == ""
    assert characters[0]["basicInfo"]["appearance"] == ""
    assert "age" not in characters[0]["basicInfo"]
    assert characters[0]["personality"]["description"] == ""
    assert characters[0]["role"] == "supporting"


def test_the_picker_never_overwrites_a_hand_written_archive(monkeypatch):
    """同名角色保留作者写的那份；勾选只补项目里还没有的名字。"""
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12), ("文辉", 8))

    with TestClient(app) as client:
        project_id = _create_project(client)
        _ingest(client, project_id, [("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))])

        saved = [
            {
                "id": "char-hand",
                "name": "林晚",
                "role": "protagonist",
                "basicInfo": {"identity": "拾荒者", "gender": "女"},
                "personality": {"description": "沉默寡言，只在必要时开口。"},
                "background": "作者手写的背景。",
            },
            {"id": "char-old", "name": "老王", "role": "mentor", "basicInfo": {"identity": "守夜人"}},
        ]
        patched = client.patch(f"/api/projects/{project_id}", headers=OWNER, json={"characters": saved})
        assert patched.status_code == 200, patched.text

        response = _accept_candidates(client, project_id, ["林晚", "文辉"])
        assert response.status_code == 200, response.text
        characters = response.json()["data"]["project"]["characters"]

    by_name = {item["name"]: item for item in characters}
    assert sorted(by_name) == sorted(["林晚", "老王", "文辉"]), "既有两位 + 新来的文辉"
    assert by_name["林晚"]["basicInfo"]["identity"] == "拾荒者"
    assert by_name["林晚"]["basicInfo"]["gender"] == "女"
    assert by_name["林晚"]["personality"]["description"] == "沉默寡言，只在必要时开口。"
    assert by_name["林晚"]["background"] == "作者手写的背景。"
    assert by_name["老王"]["role"] == "mentor"
    assert "出现 8 次" in by_name["文辉"]["background"]


def test_the_picker_ignores_names_that_are_not_candidates(monkeypatch):
    """不在候选里的名字一律不认：混着提交时只收能对上的，一个都对不上就报错。"""
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12))

    with TestClient(app) as client:
        project_id = _create_project(client)
        _ingest(client, project_id, [("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))])

        mixed = _accept_candidates(client, project_id, ["林晚", "不存在的名字"])
        assert mixed.status_code == 200, mixed.text
        assert [item["name"] for item in mixed.json()["data"]["project"]["characters"]] == ["林晚"]

        none_match = _accept_candidates(client, project_id, ["也不存在"])
        assert none_match.status_code == 400
        assert "候选" in none_match.json()["detail"]

        blank = client.post(
            f"/api/import/projects/{project_id}/characters/from-analysis",
            headers=OWNER,
            json={"names": ["   "]},
        )
        assert blank.status_code == 400, "空名单不能算成功"


def test_the_picker_requires_a_project_with_an_import_analysis(monkeypatch):
    """没有导入分析的项目：候选为空，不该静默成功。"""
    _configure(monkeypatch)

    with TestClient(app) as client:
        project_id = _create_project(client)
        response = _accept_candidates(client, project_id, ["林晚"])

    assert response.status_code == 400
    assert "候选" in response.json()["detail"]


def test_the_picker_rejects_non_owner_and_anonymous(monkeypatch):
    _configure(monkeypatch)
    _patch_characters(monkeypatch, ("林晚", 12))

    with TestClient(app) as client:
        project_id = _create_project(client)
        _ingest(client, project_id, [("第01章.md", _md("第一章 雨夜", "雨下了一整夜。"))])

        other = _accept_candidates(client, project_id, ["林晚"], headers=OTHER)
        anonymous = client.post(
            f"/api/import/projects/{project_id}/characters/from-analysis",
            json={"names": ["林晚"]},
        )

    assert other.status_code == 403
    assert anonymous.status_code == 401


def test_character_merge_only_adds_and_tolerates_dirty_input():
    from app.api.imports import _merge_analysis_characters

    existing = json.dumps(
        [{"id": "c1", "name": "林晚", "role": "protagonist", "background": "作者手写。"}],
        ensure_ascii=False,
    )
    analysis = {
        "characters": [
            {"name": "林晚", "frequency": 99},  # 同名：不动
            {"name": "  文辉 ", "frequency": 5},  # 两侧空白要归一
            {"name": "", "frequency": 3},  # 空名字：不要
            "不是对象",  # 脏数据：跳过
            {"frequency": 3},  # 没有名字：跳过
        ]
    }

    merged, added = _merge_analysis_characters(existing, analysis)
    characters = json.loads(merged)

    assert added == 1
    assert [item["name"] for item in characters] == ["林晚", "文辉"]
    assert characters[0]["background"] == "作者手写。", "既有档案必须原样保留"
    assert "出现 5 次" in characters[1]["background"]

    # 坏 JSON 不能把整批人物清空：解析不了就当空表重新补（此时两位都是新名字）
    merged, added = _merge_analysis_characters("{not json", analysis)
    assert added == 2
    assert [item["name"] for item in json.loads(merged)] == ["林晚", "文辉"]


def test_character_merge_caps_how_many_names_one_import_can_add():
    from app.api.imports import _IMPORTED_CHARACTER_LIMIT, _merge_analysis_characters

    analysis = {"characters": [{"name": f"角色{i}", "frequency": i} for i in range(50)]}

    merged, added = _merge_analysis_characters(None, analysis)

    assert added == _IMPORTED_CHARACTER_LIMIT == len(json.loads(merged))


