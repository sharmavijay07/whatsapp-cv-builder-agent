import json
from datetime import timedelta

import httpx

from safu.models import Order, Session
from safu.service import build_runtime, run_followups
from safu.whatsapp import CloudWhatsApp, parse_webhook


def _cloud(settings, handler):
    settings.app_mode = "live"
    settings.whatsapp_token = "meta-token"
    settings.whatsapp_phone_number_id = "123456"
    settings.whatsapp_graph_version = "v21.0"
    settings.background_tasks = False
    settings.groq_api_key = ""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CloudWhatsApp(settings, client=client), client


def test_documents_and_interactive_buttons_match_the_cloud_api(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            body = request.content
            assert b"messaging_product" in body
            assert b"CV.pdf" in body
            assert b"application/pdf" in body
            return httpx.Response(200, json={"id": "media-1"})
        payload = json.loads(request.content)
        if payload.get("type") == "document":
            return httpx.Response(200, json={"messages": [{"id": "wamid.doc"}]})
        return httpx.Response(200, json={"messages": [{"id": "wamid.btn"}]})

    cloud, _client = _cloud(settings, handler)
    cloud.send_buttons("254712345678", "Reply YES to continue to payment.", [("yes", "YES"), ("human", "HUMAN")])
    cloud.send_document("+254712345678", "CV.pdf", b"%PDF-1.4 test", "CV.pdf")
    cloud.send_cta_url("254712345678", "Pay with Paystack.", "Pay now", "https://checkout.paystack.com/abc")

    button = json.loads(calls[0].content)
    assert calls[0].url.path == "/v21.0/123456/messages"
    assert calls[0].headers["authorization"] == "Bearer meta-token"
    assert button["messaging_product"] == "whatsapp"
    assert button["recipient_type"] == "individual"
    assert button["to"] == "254712345678"
    assert button["type"] == "interactive"
    assert button["interactive"]["type"] == "button"
    assert button["interactive"]["action"]["buttons"][0]["reply"] == {"id": "yes", "title": "YES"}
    assert button["interactive"]["action"]["buttons"][1]["reply"]["title"] == "HUMAN"

    document = json.loads(calls[2].content)
    assert document["type"] == "document"
    assert document["to"] == "254712345678"
    assert document["document"] == {"id": "media-1", "filename": "CV.pdf", "caption": "CV.pdf"}

    cta = json.loads(calls[3].content)
    assert cta["interactive"]["type"] == "cta_url"
    assert cta["interactive"]["action"]["parameters"]["url"] == "https://checkout.paystack.com/abc"
    assert cta["interactive"]["action"]["parameters"]["display_text"] == "Pay now"


def test_followup_after_48_hours_uses_the_template(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.tpl"}]})

    cloud, _client = _cloud(settings, handler)
    runtime = build_runtime(settings, llm=None, cloud=cloud)
    order = Order(
        id="ord_follow",
        wa_id="254700000111",
        step="ask_cv",
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at=(runtime.clock.now() - timedelta(hours=49)).isoformat(),
    )
    runtime.store.save(order)
    runtime.sessions[order.wa_id] = Session(order=order, greeted=True)
    sent = run_followups(runtime, runtime.clock.now())
    assert sent == [order.id]
    assert len(calls) == 1
    assert calls[0]["type"] == "template"
    assert calls[0]["template"]["name"] == "safu_followup_48h"
    assert calls[0]["template"]["language"]["code"] == "en"
    assert "text" not in calls[0]
    transcript = runtime.sessions[order.wa_id].transcript
    assert "48 hours" in transcript[-1]["text"]
    assert run_followups(runtime, runtime.clock.now()) == []


def test_button_replies_become_text():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "254700000111",
                                    "id": "wamid.btn",
                                    "type": "interactive",
                                    "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "YES"}},
                                },
                                {
                                    "from": "254700000111",
                                    "id": "wamid.stop",
                                    "type": "button",
                                    "button": {"text": "Stop", "payload": "Stop"},
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }
    found = parse_webhook(payload)
    assert [item.text for item in found] == ["YES", "Stop"]
    assert found[0].unsupported is False
