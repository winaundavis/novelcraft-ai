"""人物回填脚本的边界（`scripts/backfill_import_characters.py`）。

背景：导入结果此前只写 `import_analysis`，没写 `projects.characters`，而「人物」页读的是
后者 —— 导入完一整本书，人物页里仍只有建项目时自动生成的主角一位。代码已修（新导入
会补），历史项目靠这个脚本回填。

这个脚本是少数几个会改写作者已有数据的工具之一，所以这里把它的承诺钉死：
默认只预览不写库；`--apply` 也**只增不改**（同名的手写档案原样保留）；写之前必须先备份；
`--exclude` 能在写入前把误判的名字挑掉（相当于导入预览里的那个叉号）。
"""

from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import get_conn
from main import _request_windows, app
import scripts.backfill_import_characters as backfill


OWNER = {"X-NovelCraft-Invite": "owner-token"}

HAND_WRITTEN = {
    "id": "char-hand",
    "name": "文辉",
    "role": "protagonist",
    "basicInfo": {"age": 31, "gender": "男", "appearance": "作者亲手写的", "identity": "", "occupation": ""},
    "personality": {"description": "作者亲手写的"},
    "background": "作者亲手写的背景",
}

ANALYSIS = {
    "characters": [
        {"name": "文辉", "frequency": 1128, "aliases": []},
        {"name": "林远", "frequency": 556, "aliases": []},
        {"name": "毛巾", "frequency": 40, "aliases": []},
    ]
}


def _configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner-token:user-a")
    _request_windows.clear()


@pytest.fixture
def seeded(monkeypatch) -> str:
    """建一个项目并写入「导入分析」与一份手写人物档案，返回 project_id。"""
    _configure(monkeypatch)
    with TestClient(app) as client:
        created = client.post(
            "/api/projects/",
            headers=OWNER,
            json={"name": "回填测试", "genre": "xuanhuan", "targetWordCount": 30000, "stylePreset": "epic"},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["data"]["id"]

    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET characters = ?, import_analysis = ? WHERE id = ?",
            (json.dumps([HAND_WRITTEN], ensure_ascii=False), json.dumps(ANALYSIS, ensure_ascii=False), project_id),
        )
    return project_id


def _names_of(project_id: str) -> list[str]:
    with get_conn() as conn:
        raw = conn.execute("SELECT characters FROM projects WHERE id = ?", (project_id,)).fetchone()["characters"]
    return [item["name"] for item in json.loads(raw)]


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["backfill_import_characters.py", *argv])
    return backfill.main()


def test_preview_changes_nothing(seeded, monkeypatch, capsys):
    before = _names_of(seeded)

    assert _run(monkeypatch, "--project", seeded) == 0

    assert _names_of(seeded) == before
    assert "这是预览，未写入" in capsys.readouterr().out


def test_apply_adds_only_missing_names_and_keeps_the_hand_written_archive(seeded, monkeypatch, capsys):
    assert _run(monkeypatch, "--project", seeded, "--apply") == 0

    names = _names_of(seeded)
    assert names == ["文辉", "林远", "毛巾"]  # 文辉不重复，两个新名字排在其后

    with get_conn() as conn:
        raw = conn.execute("SELECT characters FROM projects WHERE id = ?", (seeded,)).fetchone()["characters"]
    kept = json.loads(raw)[0]
    assert kept == HAND_WRITTEN  # 手写档案一个字节都不许动
    assert "这是预览" not in capsys.readouterr().out


def test_apply_backs_up_the_old_value_next_to_the_database(seeded, monkeypatch, capsys):
    backup_dir = backfill._db_path().parent

    assert _run(monkeypatch, "--project", seeded, "--apply") == 0

    # 备份文件是按时间戳累加的（同一秒内连跑两次也不会互相覆盖），所以按项目 id 找自己那份
    entries = [
        entry
        for path in backup_dir.glob("characters-backup-*.json")
        for entry in json.loads(path.read_text(encoding="utf-8"))
        if entry["projectId"] == seeded
    ]
    assert len(entries) == 1, "写入前必须留下且只留下一份该项目的备份"
    assert json.loads(entries[0]["characters"]) == [HAND_WRITTEN]
    assert "旧值已备份" in capsys.readouterr().out


def test_exclude_drops_misjudged_names_before_they_land(seeded, monkeypatch):
    assert _run(monkeypatch, "--project", seeded, "--exclude", "毛巾,安静", "--apply") == 0

    assert _names_of(seeded) == ["文辉", "林远"]


def test_second_apply_is_a_no_op(seeded, monkeypatch, capsys):
    assert _run(monkeypatch, "--project", seeded, "--apply") == 0
    after_first = _names_of(seeded)

    assert _run(monkeypatch, "--project", seeded, "--apply") == 0

    assert _names_of(seeded) == after_first
    assert "没有需要补的项目" in capsys.readouterr().out


def test_projects_without_import_analysis_are_left_alone(monkeypatch, capsys):
    _configure(monkeypatch)
    with TestClient(app) as client:
        created = client.post(
            "/api/projects/",
            headers=OWNER,
            json={"name": "没有导入分析", "genre": "xuanhuan", "targetWordCount": 30000, "stylePreset": "epic"},
        )
        project_id = created.json()["data"]["id"]
        before = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["characters"]

    assert _run(monkeypatch, "--project", project_id, "--apply") == 0

    assert _names_of(project_id) == [item["name"] for item in before]
    assert "没有需要补的项目" in capsys.readouterr().out
