import json

import httpx

from safu.alerts import Alerter


def test_alerts_stay_in_demo_mode_when_unset():
    alerter = Alerter()
    assert alerter.mode == "demo"
    alerter.fire("critical", "handoff", "ord_1", "user_requested", "2026-01-01T00:00:00+00:00")
    assert alerter.items[0].code == "handoff"


def test_gmail_uses_ssl_on_port_465_and_omits_cv_text(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, message):
            sent.append(("message", message["From"], message["To"], message["Subject"], message.get_content()))

    monkeypatch.setattr("safu.alerts.smtplib.SMTP_SSL", FakeSMTP)
    alerter = Alerter(
        gmail_user="owner@gmail.com",
        gmail_app_password="app-password",
        owner_email="owner@gmail.com",
    )
    assert alerter.mode == "gmail"
    alerter.fire("critical", "handoff", "ord_9", "user_requested", "2026-01-01T00:00:00+00:00")
    assert sent[0] == ("connect", "smtp.gmail.com", 465, 10)
    assert sent[1] == ("login", "owner@gmail.com", "app-password")
    body = sent[2][4]
    assert "ord_9" in body
    assert "handoff" in body
    assert "SwiftHaul" not in body
    assert "does not include CV text" in body


def test_telegram_posts_to_the_bot_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    alerter = Alerter(
        telegram_bot_token="123:abc",
        telegram_chat_id="555",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert "telegram" in alerter.mode
    alerter.fire("warning", "payment_failed", "ord_2", "result_1032", "2026-01-01T00:00:00+00:00")
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert seen["body"]["chat_id"] == "555"
    assert "ord_2" in seen["body"]["text"]
    assert "SwiftHaul" not in seen["body"]["text"]
