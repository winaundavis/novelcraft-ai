"""Settings API 集成测试（建议1 写作档位前端配套：roleOverrides 独立读写）。

覆盖：
- PUT /api/settings/llm-configs/{id}/role-overrides — 写入 writing 角色覆盖后读回
- 非法角色 / 空字段被 _normalize_role_overrides 清洗
- 更新不存在的配置返回 404
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from main import app


def _unique_name(prefix: str = "cfg") -> str:
    """生成唯一配置名，避免测试间冲突。"""
    return f"{prefix}-{int(time.time() * 1000)}"


def _create_config(client: TestClient, **overrides) -> dict:
    """创建 LLM 配置并返回 data 字典（apiKey 会被脱敏，不影响使用）。"""
    payload = {
        "provider": "deepseek",
        "apiKey": "sk-test",
        "apiBase": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": 0.8,
        "maxTokens": 4096,
        "isActive": True,
        **overrides,
    }
    resp = client.post("/api/settings/llm-configs/", json=payload)
    assert resp.status_code == 200, f"创建配置失败: {resp.text}"
    return resp.json()["data"]


def _delete_config(client: TestClient, config_id: str) -> None:
    """删除配置（忽略 404）。"""
    resp = client.delete(f"/api/settings/llm-configs/{config_id}")
    assert resp.status_code in (200, 404), f"删除失败: {resp.status_code} {resp.text}"


class TestUpdateRoleOverrides:
    """PUT /api/settings/llm-configs/{id}/role-overrides 端点测试"""

    def test_updates_writing_role_override(self):
        """正常路径：写入 writing 角色覆盖后，响应与独立读回均包含该覆盖。"""
        with TestClient(app) as client:
            config = _create_config(client)
            try:
                resp = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={
                        "roleOverrides": {
                            "writing": {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "temperature": 0.3,
                            }
                        }
                    },
                )
                assert resp.status_code == 200, f"更新失败: {resp.text}"

                body = resp.json()
                assert body["success"] is True
                writing = body["data"]["roleOverrides"]["writing"]
                assert writing["provider"] == "deepseek"
                assert writing["model"] == "deepseek-chat"
                assert writing["temperature"] == 0.3

                # 独立读回验证已持久化
                list_resp = client.get("/api/settings/llm-configs/")
                assert list_resp.status_code == 200
                saved = next(
                    item for item in list_resp.json()["data"] if item["id"] == config["id"]
                )
                assert saved["roleOverrides"]["writing"]["model"] == "deepseek-chat"
                assert saved["roleOverrides"]["writing"]["temperature"] == 0.3
            finally:
                _delete_config(client, config["id"])

    def test_clears_overrides_when_empty(self):
        """正常路径：传空 roleOverrides 时清除既有覆盖。"""
        with TestClient(app) as client:
            config = _create_config(client)
            try:
                first = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={"roleOverrides": {"writing": {"model": "deepseek-chat"}}},
                )
                assert first.status_code == 200
                assert "writing" in first.json()["data"]["roleOverrides"]

                second = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={"roleOverrides": {}},
                )
                assert second.status_code == 200
                assert second.json()["data"]["roleOverrides"] == {}
            finally:
                _delete_config(client, config["id"])

    def test_unknown_role_and_invalid_fields_are_dropped(self):
        """异常输入：未知角色与空字段被清洗，不写入 roleOverrides。"""
        with TestClient(app) as client:
            config = _create_config(client)
            try:
                resp = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={
                        "roleOverrides": {
                            "writing": {"model": "deepseek-chat", "temperature": 0.8},
                            "unknown_role": {"model": "x"},
                            "summary": {"model": ""},
                        }
                    },
                )
                assert resp.status_code == 200
                overrides = resp.json()["data"]["roleOverrides"]
                assert "unknown_role" not in overrides
                assert "summary" not in overrides
                assert overrides["writing"]["model"] == "deepseek-chat"
            finally:
                _delete_config(client, config["id"])

    def test_missing_config_returns_404(self):
        """异常路径：更新不存在的配置返回 404。"""
        with TestClient(app) as client:
            resp = client.put(
                "/api/settings/llm-configs/not-exist/role-overrides",
                json={"roleOverrides": {"writing": {"model": "deepseek-chat"}}},
            )
            assert resp.status_code == 404


class TestRoleApiKeyMasking:
    """第 2 档：角色独立 api_key 的掩码存取（掩码回显 / 掩码保留原值 / 空串清除）"""

    def test_role_api_key_masked_and_kept_on_remask_and_cleared(self):
        """写入 api_key → 读回掩码；提交掩码值保留原值；空串清除该角色 key。"""
        with TestClient(app) as client:
            config = _create_config(client)
            try:
                # 写入带 api_key 的角色覆盖
                resp = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={
                        "roleOverrides": {
                            "check": {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                                "api_key": "sk-role-secret-key-123456",
                            }
                        }
                    },
                )
                assert resp.status_code == 200, f"更新失败: {resp.text}"
                returned_key = resp.json()["data"]["roleOverrides"]["check"]["api_key"]
                assert "*" in returned_key
                assert "secret" not in returned_key  # 不泄露原文

                # 回显掩码值原样提交（前端回显场景）→ 保留原值，不丢 key
                masked = resp.json()["data"]["roleOverrides"]["check"]
                again = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={"roleOverrides": {"check": {**masked, "temperature": 0.1}}},
                )
                assert again.status_code == 200
                stored = again.json()["data"]["roleOverrides"]["check"]
                assert "*" in stored["api_key"]  # 仍掩码展示
                assert stored["temperature"] == 0.1

                # 空串提交 → 清除该角色 key
                cleared = client.put(
                    f"/api/settings/llm-configs/{config['id']}/role-overrides",
                    json={
                        "roleOverrides": {
                            "check": {"model": "deepseek-chat", "api_key": ""}
                        }
                    },
                )
                assert cleared.status_code == 200
                assert "api_key" not in cleared.json()["data"]["roleOverrides"]["check"]
            finally:
                _delete_config(client, config["id"])
