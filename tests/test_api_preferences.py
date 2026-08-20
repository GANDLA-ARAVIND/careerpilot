import json

import config


def _valid_payload(**overrides) -> dict:
    payload = {
        "title_allowlist": ["software engineer", "backend"],
        "seniority_keywords": ["senior", "staff"],
        "non_engineering_keywords": ["sales"],
        "india_location_keywords": ["bangalore", "india"],
    }
    payload.update(overrides)
    return payload


def test_get_preferences_returns_the_four_lists(client):
    body = client.get("/api/preferences").json()

    for field in (
        "title_allowlist",
        "seniority_keywords",
        "non_engineering_keywords",
        "india_location_keywords",
    ):
        assert isinstance(body[field], list) and body[field]


def test_preview_reports_impact_without_saving(client, seeded, temp_env, monkeypatch):
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])
    before = list(config.TITLE_ALLOWLIST)

    body = client.post("/api/preferences/preview", json=_valid_payload(title_allowlist=["backend"])).json()

    assert "current_survivors" in body and "new_survivors" in body
    assert body["delta"] == body["new_survivors"] - body["current_survivors"]
    assert config.TITLE_ALLOWLIST == before  # untouched
    assert not temp_env["preferences_path"].exists()  # nothing written


def test_preview_narrowing_the_allowlist_reduces_survivors(client, seeded):
    """Only "Backend Developer" matches - "Software Engineer I" no longer
    does, so survivors must drop."""
    body = client.post("/api/preferences/preview", json=_valid_payload(title_allowlist=["backend"])).json()

    assert body["new_survivors"] < body["current_survivors"]
    assert body["delta"] < 0


def test_put_preferences_saves_and_applies_immediately(client, temp_env, monkeypatch):
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])
    payload = _valid_payload(title_allowlist=["backend"])

    body = client.put("/api/preferences", json=payload).json()

    assert body["saved"] is True
    assert config.TITLE_ALLOWLIST == ["backend"]  # live in this process
    saved = json.loads(temp_env["preferences_path"].read_text(encoding="utf-8"))
    assert saved["title_allowlist"] == ["backend"]


def test_put_preferences_returns_new_survivor_count(client, seeded, temp_env, monkeypatch):
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])

    body = client.put("/api/preferences", json=_valid_payload(title_allowlist=["backend"])).json()

    assert body["survivors"] == 1  # only Globex | Backend Developer


def test_put_rejects_an_empty_title_allowlist(client, temp_env, monkeypatch):
    """An empty allowlist would let every fetched job through to the
    Analyst - roughly 8000 postings and a day's quota. Refused loudly at
    the write, not silently corrected on the next read."""
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])

    response = client.put("/api/preferences", json=_valid_payload(title_allowlist=[]))

    assert response.status_code == 422
    assert "quota" in response.json()["detail"].lower()
    assert not temp_env["preferences_path"].exists()


def test_put_rejects_any_empty_list(client, temp_env, monkeypatch):
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])

    for field in ("seniority_keywords", "non_engineering_keywords", "india_location_keywords"):
        response = client.put("/api/preferences", json=_valid_payload(**{field: []}))
        assert response.status_code == 422, field


def test_preview_rejects_an_empty_list_too(client):
    assert client.post("/api/preferences/preview", json=_valid_payload(title_allowlist=[])).status_code == 422


def test_reset_restores_defaults(client, temp_env, monkeypatch):
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])
    client.put("/api/preferences", json=_valid_payload(title_allowlist=["backend"]))
    assert config.TITLE_ALLOWLIST == ["backend"]

    body = client.post("/api/preferences/reset").json()

    assert body["saved"] is True
    assert body["title_allowlist"] == config.DEFAULT_PREFERENCES.title_allowlist
    assert config.TITLE_ALLOWLIST == config.DEFAULT_PREFERENCES.title_allowlist


def test_reset_is_reflected_in_get(client, temp_env, monkeypatch):
    monkeypatch.setattr(config, "PREFERENCES_PATH", temp_env["preferences_path"])
    client.put("/api/preferences", json=_valid_payload(title_allowlist=["backend"]))
    client.post("/api/preferences/reset")

    body = client.get("/api/preferences").json()

    assert body["is_default"] is True
