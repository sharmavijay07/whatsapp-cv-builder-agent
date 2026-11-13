import pytest
from fastapi.testclient import TestClient

from safu.api import create_app
from safu.config import load_settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in (
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_PROVIDER",
        "WHATSAPP_TOKEN",
        "WHATSAPP_APP_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    loaded = load_settings()
    loaded.data_dir = tmp_path / "data"
    loaded.log_path = loaded.data_dir / "safu.log"
    loaded.background_tasks = False
    loaded.app_mode = "mock"
    loaded.llm_provider = "groq"
    loaded.groq_api_key = ""
    loaded.groq_model = "openai/gpt-oss-120b"
    loaded.groq_base_url = "https://api.groq.com/openai/v1"
    loaded.file_ttl_seconds = 1800
    loaded.allow_local_urls = True
    loaded.whatsapp_app_secret = ""
    loaded.payment_provider = "stub"
    loaded.require_payment = True
    loaded.price_amount = 1500
    loaded.price_currency = "KES"
    loaded.admin_token = ""
    loaded.ops_token = ""
    loaded.cron_secret = ""
    loaded.gmail_user = ""
    loaded.gmail_app_password = ""
    loaded.owner_email = ""
    loaded.telegram_bot_token = ""
    loaded.telegram_chat_id = ""
    loaded.alert_webhook_url = ""
    loaded.paystack_secret_key = ""
    loaded.mpesa_consumer_key = ""
    loaded.mpesa_consumer_secret = ""
    loaded.mpesa_passkey = ""
    loaded.mpesa_shortcode = ""
    loaded.groq_api_key = ""
    return loaded


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def post_text(client, session_id, text, message_id=None):
    data = {"text": text}
    if message_id:
        data["message_id"] = message_id
    return client.post(f"/api/demo/sessions/{session_id}/messages", data=data)
