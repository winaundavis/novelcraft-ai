"""正文页「这一章在服务端已不存在」之后仍然能保存 —— 保存会把该章节静默重建。

这条钉住的是一个真实的数据一致性缺陷（症状由作者在真实使用中发现）：

1. 浏览器里残留的大纲节点 id 已经不在服务端 `chapters` 里（导入整批替换、或已被移入回收站），
   所以 `GET /chapters/{id}` 返回 404 → 界面如实提示「这一章在服务端已不存在…」；
2. 可是 `PUT /chapters/{id}` 在记录不存在时**无条件插入**，于是用户继续写并保存时，
   这一章又被建了回来 —— 所以作者看到「提示说不存在，但我照样能创作」；
3. 而这个重建出来的行**不在 `projects.outline` 里**（大纲是项目的另一份 JSON），
   左侧章节树与章节进度都按大纲渲染 → 看不到它 → 「创作好后进度不变化」。

这些用例只操作临时库，不调用 LLM，也不碰真实 Beta 库。
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from main import app, _request_windows

OWNER = {"X-NovelCraft-Invite": "ghost-owner"}


def _nodes(outline: dict) -> list[dict]:
    return outline["nodes"]


def _chapter_node_ids(outline: dict) -> list[str]:
    return [node["id"] for node in _nodes(outline) if node.get("type") == "chapter"]


@pytest.fixture
def workspace(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "ghost-owner:ghost-user")
    _request_windows.clear()
    with TestClient(app) as client:
        pid = client.post("/api/projects/", headers=OWNER, json={
            "name": "幽灵章节", "genre": "xuanhuan", "targetWordCount": 10000, "stylePreset": "epic",
        }).json()["data"]["id"]
        ids = [f"{pid}-c{i}" for i in range(1, 4)]
        outline = {"mainSummary": "测试", "nodes": [
            {"id": "v1", "type": "volume", "title": "第一卷", "order": 1},
            {"id": "a1", "type": "arc", "title": "小故事", "parentId": "v1", "order": 1},
            *[{"id": cid, "type": "chapter", "title": f"第{i}章", "parentId": "v1", "arcId": "a1", "order": i}
              for i, cid in enumerate(ids, 1)],
        ]}
        assert client.patch(f"/api/projects/{pid}", headers=OWNER, json={"outline": outline}).status_code == 200
        for i, cid in enumerate(ids[:2], 1):
            assert client.put(f"/api/projects/{pid}/chapters/{cid}", headers=OWNER, json={
                "title": f"第{i}章", "content": f"保存正文{i}。\n人物站在窗前。", "volumeId": "v1", "order": i,
            }).status_code == 200
        yield client, pid, ids, outline


def _chapters(client, pid) -> list[dict]:
    response = client.get(f"/api/projects/{pid}/chapters/", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _outline(client, pid) -> dict:
    response = client.get(f"/api/projects/{pid}", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()["data"]["outline"]


def test_saving_a_chapter_the_server_lost_silently_recreates_it_outside_the_outline(workspace):
    """作者遇到的完整链路：读 404 → 仍能保存 → 章节被建回来，但大纲里没有它。"""
    client, pid, _ids, _outline_data = workspace
    ghost = f"{pid}-ghost"

    # 1) 读不到：这正是界面提示「这一章在服务端已不存在」的来源。
    assert client.get(f"/api/projects/{pid}/chapters/{ghost}", headers=OWNER).status_code == 404

    # 2) 仍然可以保存 —— 于是作者认为"提示是误报，我明明能写"。
    saved = client.put(f"/api/projects/{pid}/chapters/{ghost}", headers=OWNER, json={
        "title": "第99章", "content": "我还是把它写出来了。", "volumeId": "v1", "order": 9,
    })
    assert saved.status_code == 200, saved.text

    # 3) 章节确实被静默重建了。
    assert client.get(f"/api/projects/{pid}/chapters/{ghost}", headers=OWNER).status_code == 200

    # 4) 但它不在大纲里 —— 左侧章节树/章节进度按大纲渲染，所以看不到它、进度也不动。
    assert ghost not in _chapter_node_ids(_outline(client, pid))

    # 5) 而按「章节列表」取的视图又能看到它 → 同一份数据两套事实，界面必然自相矛盾。
    assert ghost in [row["id"] for row in _chapters(client, pid)]


def test_a_never_written_outline_node_404s_but_is_still_in_the_outline(workspace):
    """**新建项目/刚生成大纲**时的正常状态：节点在大纲里，但服务端还没有 chapters 行。

    前端必须把这种 404 当成"尚未创作"（空态），而不是"这一章已不存在" ——
    这正是作者"新建项目、还没创作，正文页就弹报错"的直接原因。

    同时钉住另一件事：**PUT 仍然必须能创建**（前端保存新章节是走 PUT 落库的，
    前端从不调用 `POST /chapters/`）。曾经试过用后端守卫禁止"往不存在的 id 保存"，
    结果打破了 `test_chapter_trash.py` 里"删了继续写、批量还原不覆盖新正文"的既有契约，已撤回。
    """
    client, pid, ids, _outline_data = workspace
    never_written = ids[2]  # 夹具只保存了前两章：这一章在大纲里但从未落库
    ghost = f"{pid}-ghost"  # 既不在大纲里、也没有 chapters 行

    assert client.get(f"/api/projects/{pid}/chapters/{never_written}", headers=OWNER).status_code == 404
    assert client.get(f"/api/projects/{pid}/chapters/{ghost}", headers=OWNER).status_code == 404

    # 两者的区别**只在客户端的大纲里**能看出来：一个还在大纲里，一个已经不在。
    node_ids = _chapter_node_ids(_outline(client, pid))
    assert never_written in node_ids
    assert ghost not in node_ids

    # 而"从未创作"的那一章首次保存必须照常成功。
    assert client.put(f"/api/projects/{pid}/chapters/{never_written}", headers=OWNER, json={
        "title": "第3章", "content": "第一次写这一章。", "volumeId": "v1", "order": 3,
    }).status_code == 200
