from pathlib import Path

from fastapi.testclient import TestClient

from safu.api import create_app
from safu.whatsapp import safe_filename


def test_ops_routes_require_admin_bearer_and_cron_is_closed(client):
    assert client.get("/api/ops/alerts").status_code == 200
    assert client.post("/api/cron/tick").status_code == 401
    runtime = client.app.state.runtime
    runtime.settings.admin_token = "admin-secret"
    runtime.settings.cron_secret = "cron-secret"
    assert client.get("/api/ops/alerts").status_code == 401
    assert client.post("/api/ops/recovery/run").status_code == 401
    assert client.get("/api/ops/alerts", headers={"X-Ops-Token": "wrong"}).status_code == 401
    assert client.get("/api/ops/alerts", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/ops/alerts", headers={"X-Ops-Token": "admin-secret"}).status_code == 200
    allowed = client.get("/api/ops/alerts", headers={"Authorization": "Bearer admin-secret"})
    assert allowed.status_code == 200
    assert client.post("/api/cron/tick", headers={"Authorization": "Bearer cron-secret"}).status_code == 200
    assert client.post("/api/cron/tick", headers={"Authorization": "Bearer admin-secret"}).status_code == 200
    assert client.post("/api/demo/recovery/run").status_code == 200


def test_live_webhook_requires_the_app_secret(settings):
    settings.app_mode = "live"
    settings.whatsapp_token = "token"
    settings.whatsapp_phone_number_id = "123"
    settings.whatsapp_app_secret = ""
    with TestClient(create_app(settings)) as client:
        assert client.post("/webhook", json={"entry": []}).status_code == 403
        assert client.get("/").headers["x-content-type-options"] == "nosniff"
        assert client.get("/").headers["x-frame-options"] == "DENY"


def test_download_names_are_header_safe():
    cleaned = safe_filename('cv".pdf\r\nX-Evil: 1')
    assert cleaned == "cv_.pdf__X-Evil__1"
    assert "\r" not in cleaned and "\n" not in cleaned and '"' not in cleaned
    assert safe_filename("") == "file"


def test_followup_template_definition_is_ready_to_submit():
    payload = json_template()
    assert payload["name"] == "safu_followup_48h"
    assert payload["category"] == "UTILITY"
    assert payload["language"] == "en"
    body = payload["components"][0]["text"]
    assert "48 hours" in body
    assert "{{" not in body
    buttons = payload["components"][1]["buttons"]
    assert [item["text"] for item in buttons] == ["Continue", "Stop"]


def json_template():
    import json

    return json.loads(Path("meta/safu_followup_48h.json").read_text(encoding="utf-8"))
