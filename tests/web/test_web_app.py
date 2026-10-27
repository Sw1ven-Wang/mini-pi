from __future__ import annotations

import json

from fastapi.testclient import TestClient

from coding_agent.web.app import create_app
from coding_agent.web.config import ProviderRegistry, parse_allowed_root


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_WEB_KEY", "sk-hidden-secret")
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({
        "providers": [{
            "id": "fake",
            "label": "Fake Provider",
            "base_url": "https://provider.example.test/v1",
            "api_key_env": "TEST_WEB_KEY",
            "models": ["fake-model"],
            "default_model": "fake-model",
        }]
    }), encoding="utf-8")
    workspace = tmp_path / "projects"
    (workspace / "demo").mkdir(parents=True)
    root = parse_allowed_root(f"Projects={workspace}")
    return create_app(
        roots=[root],
        providers=ProviderRegistry.from_file(config),
        verify_command="pytest -q",
        csrf_token="fixed-csrf-token",
    ), root


def test_bootstrap_initializes_cookie_without_leaking_provider_secret(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        assert response.cookies.get("mini_pi_csrf") == "fixed-csrf-token"
        text = response.text
        assert "sk-hidden-secret" not in text
        assert "TEST_WEB_KEY" not in text
        assert "provider.example.test" not in text


def test_api_requires_bootstrap_cookie_and_csrf_header(tmp_path, monkeypatch):
    app, root = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost") as client:
        without_cookie = client.get(
            "/api/directories", params={"root_id": root.id, "path": ""},
        )
        assert without_cookie.status_code == 403
        client.get("/api/bootstrap")
        listing = client.get(
            "/api/directories", params={"root_id": root.id, "path": ""},
        )
        assert listing.status_code == 200
        assert listing.json()["directories"][0]["relative_path"] == "demo"
        mutation = client.post("/api/tasks", json={})
        assert mutation.status_code == 403
        validated = client.post(
            "/api/tasks",
            headers={"X-Mini-Pi-CSRF": "fixed-csrf-token"},
            json={},
        )
        assert validated.status_code == 422


def test_non_local_host_and_origin_are_rejected(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get(
            "/api/bootstrap", headers={"Host": "attacker.example"},
        ).status_code == 403
        assert client.get(
            "/api/bootstrap", headers={"Origin": "https://attacker.example"},
        ).status_code == 403
