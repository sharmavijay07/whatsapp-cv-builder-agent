import json
from pathlib import Path

from safu.config import load_settings
from safu.models import Order
from safu.store import build_store


def test_render_blueprint_is_a_free_python_service():
    text = Path("render.yaml").read_text(encoding="utf-8")
    assert "type: web" in text
    assert "runtime: python" in text
    assert "plan: free" in text
    assert "pip install --no-cache-dir -r requirements.txt" in text
    assert "python -m safu" in text
    assert "GROQ_API_KEY" in text
    assert "sync: false" in text
    assert "openai/gpt-oss-120b" in text
    assert "https://api.groq.com/openai/v1" in text
    assert "ORDER_STORE" in text
    assert "sqlite" in text
    for key in (
        "ADMIN_TOKEN",
        "CRON_SECRET",
        "MPESA_CONSUMER_KEY",
        "MPESA_PASSKEY",
        "PAYSTACK_SECRET_KEY",
        "GMAIL_APP_PASSWORD",
        "OWNER_EMAIL",
        "GROQ_VISION_MODEL",
        "WHATSAPP_FOLLOWUP_TEMPLATE",
    ):
        assert key in text
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "tesseract-ocr" in dockerfile
    assert 'CMD ["python", "-m", "safu"]' in dockerfile
    template = Path("meta/safu_followup_48h.json").read_text(encoding="utf-8")
    assert "safu_followup_48h" in template


def test_port_and_render_url(monkeypatch):
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://whatsapp-cv-builder.onrender.com")
    settings = load_settings()
    assert settings.port == 10000
    assert settings.host == "0.0.0.0"
    assert settings.public_base_url == "https://whatsapp-cv-builder.onrender.com"


def test_missing_whatsapp_keys_keep_demo_mode(monkeypatch):
    monkeypatch.setenv("APP_MODE", "live")
    monkeypatch.setenv("WHATSAPP_TOKEN", "")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "")
    assert load_settings().app_mode == "mock"

    monkeypatch.setenv("WHATSAPP_TOKEN", "token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    assert load_settings().app_mode == "live"


def test_missing_supabase_keys_use_sqlite_without_cv_text(tmp_path):
    store, name = build_store("supabase", "", "", tmp_path)
    assert name == "sqlite"
    order = Order(
        id="ord_sql",
        wa_id="254700000099",
        step="confirm",
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        paid=False,
    )
    store.save(order)
    store.remember("wamid.sql", order.id, order.created_at)
    loaded = store.get_by_wa(order.wa_id)
    assert loaded is not None
    assert loaded.paid is False
    assert loaded.step == "confirm"
    assert store.has_message("wamid.sql") is True
    blob = json.dumps(store.dump())
    assert "SwiftHaul" not in blob
    assert set(store.dump()["orders"][0]) == {
        "id",
        "wa_id",
        "step",
        "status",
        "created_at",
        "updated_at",
        "paid",
        "handoff",
        "followup_sent",
        "delivery_attempts",
        "pack_id",
        "last_error_code",
        "awaiting_field",
    }

    memory, memory_name = build_store("memory", "", "", tmp_path)
    assert memory_name == "memory"
    ready, ready_name = build_store("supabase", "https://example.supabase.co", "key", tmp_path)
    assert ready_name == "supabase"
    assert ready.__class__.__name__ == "SupabaseOrderStore"
