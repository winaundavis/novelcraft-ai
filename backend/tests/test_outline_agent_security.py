from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import settings
from main import app


def test_unauthenticated_outline_edit_returns_401_before_candidate_write(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        response = client.post("/api/projects/missing/outline-candidates", json={"candidateOutline": {"nodes": []}})
    assert response.status_code == 401
