"""Codex D3 / P2-3：诊断端点注册回归测试。

验证（debug=false 当前模式）：
- /api/ai/_diag-continue 不出现在 OpenAPI schema（D3：include_in_schema 用布尔值）
- 该路由**完全未注册**（P2-3：debug=false 时条件注册，路由不存在），
  无效请求直接 404，不会先触发 422 请求体校验。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_diag_endpoint_not_in_openapi_current_debug_state():
    """非 debug：_diag-continue 不在 OpenAPI（Codex D3）。"""
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    paths = schema.get("paths", {})
    assert "/api/ai/_diag-continue" not in paths, "非 debug 时诊断端点不应出现在 OpenAPI"


def test_diag_endpoint_route_not_registered_when_not_debug():
    """非 debug：_diag-continue 的 POST 不命中诊断处理器。

    可能返回 404（无匹配路由）或 405（被 novel_jobs 的 GET /{job_id} 参数化
    路由路径匹配但方法不符）——但绝不会 200 / 422，即诊断逻辑不可达。
    """
    client = TestClient(app)
    resp = client.post(
        "/api/ai/_diag-continue",
        json={
            "projectId": "p1",
            "chapterId": "c1",
            "cursorPosition": 0,
            "contextBefore": "正文",
            "direction": "继续",
            "wordCount": 100,
            "chapterTitle": "第一章",
            "premise": "设定",
            "outlineSummary": "大纲",
        },
    )
    assert resp.status_code in (404, 405), (
        f"非 debug 时诊断端点应不可达（404/405），实际 {resp.status_code}"
    )
    assert resp.status_code != 200


def test_diag_endpoint_404_even_with_invalid_body_when_not_debug():
    """非 debug：非法请求体也不触发 422 请求体校验（诊断端点不注册，校验不可达）。"""
    client = TestClient(app)
    resp = client.post("/api/ai/_diag-continue", json={"foo": "bar"})
    assert resp.status_code in (404, 405), (
        f"非 debug 时诊断端点不应触发 422 校验，实际 {resp.status_code}"
    )
    assert resp.status_code != 422
