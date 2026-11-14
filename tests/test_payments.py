import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient

from safu.api import create_app
from safu.payments import MpesaGateway, PaystackGateway, build_payments
from safu.samples import CV_TEXT, JOB_TEXT, text_to_pdf
from tests.conftest import post_text


def _to_yes(client):
    created = client.post("/api/demo/sessions")
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    assert post_text(client, session_id, "Amina Wanjiku").status_code == 200
    uploaded = client.post(
        f"/api/demo/sessions/{session_id}/messages",
        data={"text": ""},
        files={"file": ("cv.pdf", text_to_pdf(CV_TEXT), "application/pdf")},
    )
    assert uploaded.status_code == 200
    assert post_text(client, session_id, JOB_TEXT).status_code == 200
    agreed = post_text(client, session_id, "YES")
    assert agreed.status_code == 200
    return session_id, agreed


def _filenames(payload):
    return [item.get("filename") for item in payload["messages"]]


def _mpesa_handler(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/v1/generate"):
            assert request.headers["authorization"].startswith("Basic ")
            assert request.url.host == "sandbox.safaricom.co.ke"
            return httpx.Response(200, json={"access_token": "sandbox-token", "expires_in": "3599"})
        if request.url.path.endswith("/mpesa/stkpush/v1/processrequest"):
            body = json.loads(request.content)
            seen["stk"] = body
            assert request.headers["authorization"] == "Bearer sandbox-token"
            return httpx.Response(
                200,
                json={
                    "MerchantRequestID": "m-1",
                    "CheckoutRequestID": "ws_CO_123",
                    "ResponseCode": "0",
                    "ResponseDescription": "Success. Request accepted for processing",
                    "CustomerMessage": "Success. Request accepted for processing",
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    return handler


def _configure_mpesa(settings):
    settings.payment_provider = "mpesa"
    settings.mpesa_env = "sandbox"
    settings.mpesa_consumer_key = "consumer-key"
    settings.mpesa_consumer_secret = "consumer-secret"
    settings.mpesa_shortcode = "174379"
    settings.mpesa_passkey = "sandbox-passkey"
    settings.mpesa_callback_secret = ""
    settings.public_base_url = "https://whatsapp-cv-builder.onrender.com"


def _stk_callback(amount, result_code=0, checkout="ws_CO_123"):
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "m-1",
                "CheckoutRequestID": checkout,
                "ResultCode": result_code,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": amount},
                        {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
                        {"Name": "PhoneNumber", "Value": 254712345678},
                    ]
                },
            }
        }
    }


def test_mpesa_stk_does_not_deliver_on_a_short_payment(settings):
    seen = {}
    _configure_mpesa(settings)
    gateway = build_payments(settings, client=httpx.Client(transport=httpx.MockTransport(_mpesa_handler(seen))))
    assert isinstance(gateway, MpesaGateway)
    assert gateway.ready is True
    with TestClient(create_app(settings, payments=gateway)) as client:
        session_id, agreed = _to_yes(client)
        assert "CV.docx" not in _filenames(agreed.json())
        assert "254712345678" in agreed.json()["messages"][-1]["text"]
        assert agreed.json()["inspector"]["database_row"]["paid"] is False
        stk = seen["stk"]
        assert stk["BusinessShortCode"] == "174379"
        assert stk["PhoneNumber"] == "254712345678"
        assert stk["PartyA"] == "254712345678"
        assert stk["PartyB"] == "174379"
        assert stk["Amount"] == "1500"
        assert stk["TransactionType"] == "CustomerPayBillOnline"
        assert stk["TransactionDesc"] == "Safu CV pack"
        assert len(stk["AccountReference"]) == 12
        assert stk["CallBackURL"] == "https://whatsapp-cv-builder.onrender.com/webhooks/mpesa"
        assert len(stk["Timestamp"]) == 14
        assert stk["Password"]
        order_id = agreed.json()["inspector"]["database_row"]["id"]
        record = gateway.ledger.latest(order_id)
        assert record is not None
        assert record.status == "pending"
        assert record.provider_id == "ws_CO_123"
        blob = settings.data_dir.joinpath("payments.sqlite").read_bytes()
        assert b"SwiftHaul" not in blob
        assert b"254712345678" not in blob

        short = client.post("/webhooks/mpesa", json=_stk_callback(10))
        assert short.status_code == 200
        waiting = client.get(f"/api/demo/sessions/{session_id}").json()
        assert "CV.docx" not in _filenames(waiting)
        assert waiting["inspector"]["database_row"]["paid"] is False
        assert "did not complete" in waiting["messages"][-1]["text"].lower()
        assert gateway.ledger.is_paid(order_id) is False


def test_mpesa_success_sends_the_pack_and_ignores_unknown_checkouts(settings):
    seen = {}
    _configure_mpesa(settings)
    gateway = build_payments(settings, client=httpx.Client(transport=httpx.MockTransport(_mpesa_handler(seen))))
    with TestClient(create_app(settings, payments=gateway)) as client:
        session_id, agreed = _to_yes(client)
        order_id = agreed.json()["inspector"]["database_row"]["id"]
        unknown = client.post("/webhooks/mpesa", json=_stk_callback(1500, checkout="someone-else"))
        assert unknown.status_code == 200
        assert gateway.ledger.is_paid(order_id) is False

        confirmed = client.post("/webhooks/mpesa", json=_stk_callback(1500))
        assert confirmed.status_code == 200
        assert confirmed.json()["ResultCode"] == 0
        body = client.get(f"/api/demo/sessions/{session_id}").json()
        for name in ("CV.docx", "CV.pdf", "Cover-Letter.docx", "Cover-Letter.pdf", "Interview-Prep.docx", "Interview-Prep.pdf"):
            assert name in _filenames(body)
        assert body["inspector"]["database_row"]["paid"] is True
        assert body["inspector"]["database_row"]["status"] == "delivered"
        assert gateway.ledger.is_paid(order_id) is True

        again = client.post("/webhooks/mpesa", json=_stk_callback(1500))
        assert again.status_code == 200
        assert _filenames(client.get(f"/api/demo/sessions/{session_id}").json()).count("CV.docx") == 1


def test_mpesa_callback_secret_and_unconfigured_gateway(settings):
    settings.payment_provider = "mpesa"
    settings.mpesa_callback_secret = "callback-secret"
    gateway = MpesaGateway(settings, client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    assert gateway.ready is False
    intent = gateway.start("ord_x", 1500, "KES", phone="0712345678")
    assert "Nothing has been charged" in intent.instructions
    with TestClient(create_app(settings, payments=gateway)) as client:
        rejected = client.post("/webhooks/mpesa", json={"Body": {}})
        assert rejected.status_code == 403
        accepted = client.post("/webhooks/mpesa?token=callback-secret", json={"Body": {}})
        assert accepted.status_code == 200


def _paystack_handler(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.paystack.co/transaction/initialize"
        assert request.headers["authorization"] == "Bearer sk_test_example"
        body = json.loads(request.content)
        seen["init"] = body
        assert body["email"] == "amina.wanjiku@email.com"
        assert body["amount"] == 150000
        assert body["currency"] == "KES"
        assert body["metadata"]["order_id"]
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Authorization URL created",
                "data": {
                    "authorization_url": "https://checkout.paystack.com/abc123",
                    "access_code": "abc123",
                    "reference": body["reference"],
                },
            },
        )

    return handler


def test_paystack_link_then_signed_webhook(settings):
    seen = {}
    settings.payment_provider = "paystack"
    settings.paystack_secret_key = "sk_test_example"
    settings.public_base_url = "https://whatsapp-cv-builder.onrender.com"
    gateway = build_payments(settings, client=httpx.Client(transport=httpx.MockTransport(_paystack_handler(seen))))
    assert isinstance(gateway, PaystackGateway)
    with TestClient(create_app(settings, payments=gateway)) as client:
        session_id, agreed = _to_yes(client)
        text = agreed.json()["messages"][-1]["text"]
        assert "https://checkout.paystack.com/abc123" in text
        assert "CV.docx" not in _filenames(agreed.json())
        assert agreed.json()["inspector"]["database_row"]["paid"] is False
        order_id = agreed.json()["inspector"]["database_row"]["id"]
        reference = gateway.ledger.latest(order_id).reference
        assert seen["init"]["reference"] == reference
        assert seen["init"]["metadata"] == {"order_id": order_id}
        assert seen["init"]["callback_url"].endswith("/payment/return")

        event = {
            "event": "charge.success",
            "data": {
                "id": 99,
                "reference": reference,
                "amount": 150000,
                "currency": "KES",
                "status": "success",
                "metadata": {"order_id": order_id},
            },
        }
        raw = json.dumps(event).encode()
        bad = client.post(
            "/webhooks/paystack",
            content=raw,
            headers={"content-type": "application/json", "x-paystack-signature": "nope"},
        )
        assert bad.status_code == 401
        assert gateway.ledger.is_paid(order_id) is False

        short = dict(event)
        short["data"] = dict(event["data"], amount=100)
        short_raw = json.dumps(short).encode()
        short_sig = hmac.new(b"sk_test_example", short_raw, hashlib.sha512).hexdigest()
        rejected = client.post(
            "/webhooks/paystack",
            content=short_raw,
            headers={"content-type": "application/json", "x-paystack-signature": short_sig},
        )
        assert rejected.status_code == 200
        assert gateway.ledger.is_paid(order_id) is False
        assert "CV.docx" not in _filenames(client.get(f"/api/demo/sessions/{session_id}").json())

        signature = hmac.new(b"sk_test_example", raw, hashlib.sha512).hexdigest()
        ok = client.post(
            "/webhooks/paystack",
            content=raw,
            headers={"content-type": "application/json", "x-paystack-signature": signature},
        )
        assert ok.status_code == 200
        body = client.get(f"/api/demo/sessions/{session_id}").json()
        assert "CV.pdf" in _filenames(body)
        assert body["inspector"]["database_row"]["status"] == "delivered"
        assert "SwiftHaul" not in json.dumps(gateway.ledger.latest(order_id).__dict__)


def test_live_paystack_key_is_refused(settings):
    settings.paystack_secret_key = "sk_live_real"
    settings.paystack_allow_live = False
    gateway = PaystackGateway(settings)
    assert gateway.ready is False
    intent = gateway.start("ord_live", 1500, "KES", email="a@b.co")
    assert "live Paystack" in intent.instructions
    assert gateway.confirm("ord_live", "PAY") is False


def test_require_payment_false_delivers_on_yes(client):
    client.app.state.runtime.settings.require_payment = False
    session_id, agreed = _to_yes(client)
    assert "CV.docx" in _filenames(agreed.json())
    assert agreed.json()["inspector"]["database_row"]["status"] == "delivered"


def test_provider_selection_keeps_the_stub(settings):
    settings.payment_provider = "stub"
    assert build_payments(settings).name == "stub"
    settings.payment_provider = "daraja"
    settings.mpesa_consumer_key = "k"
    settings.mpesa_consumer_secret = "s"
    settings.mpesa_shortcode = "174379"
    settings.mpesa_passkey = "p"
    assert build_payments(settings).name == "mpesa"
    settings.payment_provider = "card"
    settings.paystack_secret_key = "sk_test_x"
    assert build_payments(settings).name == "paystack"
