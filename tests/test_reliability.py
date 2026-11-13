import hashlib
import hmac
import json
from datetime import timedelta

from safu.models import ORDER_FIELDS, Order
from safu.samples import CV_TEXT, JOB_TEXT, text_to_pdf
from safu.service import run_followups, run_recovery, run_sweep
from safu.store import SupabaseOrderStore
from tests.conftest import post_text


def _session(client):
    created = client.post("/api/demo/sessions")
    assert created.status_code == 200
    return created.json()["session_id"]


def _to_payment(client, session_id):
    assert post_text(client, session_id, "Amina Wanjiku").status_code == 200
    pdf = text_to_pdf(CV_TEXT)
    uploaded = client.post(
        f"/api/demo/sessions/{session_id}/messages",
        data={"text": ""},
        files={"file": ("cv.pdf", pdf, "application/pdf")},
    )
    assert uploaded.status_code == 200
    job = post_text(client, session_id, JOB_TEXT)
    assert "YES" in job.json()["messages"][-1]["text"]
    agreed = post_text(client, session_id, "YES")
    assert "PAY" in agreed.json()["messages"][-1]["text"]
    return agreed


def test_duplicate_demo_message_and_webhook(client):
    session_id = _session(client)
    first = post_text(client, session_id, "Amina Wanjiku", message_id="msg-name")
    second = post_text(client, session_id, "Amina Wanjiku", message_id="msg-name")
    assert len(first.json()["messages"]) == len(second.json()["messages"])

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "254700000111",
                                    "id": "wamid.1",
                                    "type": "text",
                                    "text": {"body": "Amina Wanjiku"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    assert client.post("/webhook", json=payload).status_code == 200
    again = client.post("/webhook", json=payload)
    assert again.status_code == 200
    runtime = client.app.state.runtime
    transcript = runtime.sessions["254700000111"].transcript
    inbound = [item for item in transcript if item["direction"] == "in"]
    assert len(inbound) == 1


def test_webhook_signature_and_verify(settings):
    from fastapi.testclient import TestClient

    from safu.api import create_app

    settings.whatsapp_app_secret = "top-secret"
    settings.whatsapp_verify_token = "safu-verify"
    with TestClient(create_app(settings)) as client:
        ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "safu-verify", "hub.challenge": "12345"})
        assert ok.text == "12345"
        bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"})
        assert bad.status_code == 403
        body = json.dumps({"entry": []}).encode()
        digest = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()
        rejected = client.post("/webhook", content=body, headers={"content-type": "application/json"})
        assert rejected.status_code == 403
        accepted = client.post(
            "/webhook",
            content=body,
            headers={"content-type": "application/json", "x-hub-signature-256": "sha256=" + digest},
        )
        assert accepted.status_code == 200


def test_recovery_after_delivery_failure(client):
    session_id = _session(client)
    _to_payment(client, session_id)
    runtime = client.app.state.runtime
    runtime.channel.fail_remaining = 1
    paid = post_text(client, session_id, "PAY", message_id="pay-1")
    assert paid.status_code == 200
    assert not any(item.get("filename") == "CV.docx" for item in paid.json()["messages"])
    order = runtime.sessions[session_id].order
    assert order.last_error_code == "delivery_failed"
    assert order.pack_id
    recovered = run_recovery(runtime)
    assert order.id in recovered
    assert order.status == "delivered"
    names = [item.get("filename") for item in runtime.sessions[session_id].transcript]
    assert "CV.docx" in names
    assert "CV.pdf" in names


def test_generation_failure_still_produces_a_document(client, monkeypatch):
    session_id = _session(client)
    _to_payment(client, session_id)
    calls = {"n": 0}
    from safu import service as service_module

    real = service_module.compose

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk")
        return real(*args, **kwargs)

    monkeypatch.setattr(service_module, "compose", flaky)
    paid = post_text(client, session_id, "PAY")
    assert "retry" in paid.json()["messages"][-1]["text"].lower()
    runtime = client.app.state.runtime
    assert runtime.sessions[session_id].order.last_error_code == "generation_failed"
    run_recovery(runtime)
    assert runtime.sessions[session_id].order.status == "delivered"
    assert runtime.vault.pack_ready(runtime.sessions[session_id].order.pack_id)


def test_followup_sends_once_after_48_hours(client):
    session_id = _session(client)
    runtime = client.app.state.runtime
    order = runtime.sessions[session_id].order
    order.updated_at = (runtime.clock.now() - timedelta(hours=49)).isoformat()
    sent = run_followups(runtime, runtime.clock.now())
    assert sent == [order.id]
    assert order.followup_sent is True
    text = " ".join(item["text"] for item in runtime.sessions[session_id].transcript)
    assert "48 hours" in text
    assert run_followups(runtime, runtime.clock.now()) == []
    before = len(runtime.sessions[session_id].transcript)
    assert run_followups(runtime, runtime.clock.now()) == []
    assert len(runtime.sessions[session_id].transcript) == before


def test_handoff_when_unsure(client):
    session_id = _session(client)
    body = post_text(client, session_id, "HUMAN")
    inspector = body.json()["inspector"]
    assert inspector["database_row"]["handoff"] is True
    assert inspector["database_row"]["status"] == "needs_human"
    assert any(alert["code"] == "handoff" for alert in inspector["alerts"])
    assert inspector["database_row"]["pack_id"] is None


def test_file_ttl_and_store_shape(client):
    session_id = _session(client)
    _to_payment(client, session_id)
    paid = post_text(client, session_id, "PAY")
    docs = [item for item in paid.json()["messages"] if item.get("filename") == "CV.pdf"]
    token = docs[0]["token"]
    downloaded = client.get(f"/files/{token}")
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"%PDF")
    runtime = client.app.state.runtime
    order = runtime.sessions[session_id].order
    assert not (runtime.work_dir / f"{order.id}.json").exists()
    blob = json.dumps(runtime.store.dump())
    assert "SwiftHaul" not in blob
    assert "Cut dispatch errors" not in blob
    assert set(order.to_dict()) == set(ORDER_FIELDS)
    expires = next(item.expires_at for item in runtime.vault.items.values() if item.token == token)
    from datetime import datetime

    runtime.clock.frozen = datetime.fromisoformat(expires) + timedelta(seconds=5)
    assert run_sweep(runtime) >= 1
    assert client.get(f"/files/{token}").status_code == 410
    log_text = runtime.settings.log_path.read_text(encoding="utf-8")
    assert "SwiftHaul" not in log_text
    assert "Cut dispatch errors" not in log_text


def test_supabase_payload_has_no_cv_fields():
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeClient:
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            captured["auth"] = headers["Authorization"]
            return FakeResponse()

        def get(self, url, headers=None, params=None):
            return FakeResponse()

    store = SupabaseOrderStore("https://example.supabase.co", "service-key", client=FakeClient())
    store.save(
        Order(
            id="ord_1",
            wa_id="254700000000",
            step="confirm",
            status="active",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    assert set(captured["json"]) == set(ORDER_FIELDS)
    assert "SwiftHaul" not in json.dumps(captured["json"])
    assert captured["auth"] == "Bearer service-key"
    store.remember("wamid.1", "ord_1", "2026-01-01T00:00:00+00:00")
    assert set(captured["json"]) == {"message_id", "order_id", "received_at"}
