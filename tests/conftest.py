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
