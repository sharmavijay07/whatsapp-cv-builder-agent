import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class Settings:
    app_mode: str = "mock"
    host: str = "0.0.0.0"
    port: int = 8765
    public_base_url: str = "http://127.0.0.1:8765"
    llm_provider: str = "groq"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "safu-verify"
    whatsapp_app_secret: str = ""
    whatsapp_graph_version: str = "v21.0"
    order_store: str = "memory"
    supabase_url: str = ""
    supabase_service_key: str = ""
    payment_provider: str = "stub"
    price_amount: int = 1500
    price_currency: str = "KES"
    file_ttl_seconds: int = 1800
    followup_after_hours: int = 48
    max_hold_seconds: int = 172800
    alert_webhook_url: str = ""
    ops_token: str = ""
    allow_local_urls: bool = True
    log_level: str = "INFO"
    background_tasks: bool = True
    data_dir: Path = Path("var")
    log_path: Path = Path("var/safu.log")

    @property
    def active_model(self) -> str:
        return {
            "groq": self.groq_model,
            "openai": self.openai_model,
            "gemini": self.gemini_model,
            "anthropic": self.anthropic_model,
            "local": "safe-local",
        }.get(self.llm_provider, self.groq_model)

    @property
    def active_base_url(self) -> str:
        return {
            "groq": self.groq_base_url,
            "openai": self.openai_base_url,
            "gemini": self.gemini_base_url,
            "anthropic": self.anthropic_base_url,
        }.get(self.llm_provider, self.groq_base_url)


def load_settings() -> Settings:
    app_mode = (os.getenv("APP_MODE") or "mock").strip().lower()
    if app_mode not in {"mock", "live"}:
        app_mode = "mock"
    provider = (os.getenv("LLM_PROVIDER") or "groq").strip().lower()
    data_dir = Path(os.getenv("DATA_DIR") or "var")
    return Settings(
        app_mode=app_mode,
        host=os.getenv("HOST") or "0.0.0.0",
        port=_int("PORT", 8765),
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8765").rstrip("/"),
        llm_provider=provider,
        groq_api_key=os.getenv("GROQ_API_KEY") or "",
        groq_model=os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b",
        groq_base_url=(os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or "",
        openai_model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        openai_base_url=(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or "",
        gemini_model=os.getenv("GEMINI_MODEL") or "gemini-2.0-flash",
        gemini_base_url=(
            os.getenv("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai"
        ).rstrip("/"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or "",
        anthropic_model=os.getenv("ANTHROPIC_MODEL") or "claude-haiku-4-5",
        anthropic_base_url=(os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/"),
        whatsapp_token=os.getenv("WHATSAPP_TOKEN") or "",
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "",
        whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN") or "safu-verify",
        whatsapp_app_secret=os.getenv("WHATSAPP_APP_SECRET") or "",
        whatsapp_graph_version=os.getenv("WHATSAPP_GRAPH_VERSION") or "v21.0",
        order_store=(os.getenv("ORDER_STORE") or "memory").strip().lower(),
        supabase_url=os.getenv("SUPABASE_URL") or "",
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY") or "",
        payment_provider=(os.getenv("PAYMENT_PROVIDER") or "stub").strip().lower(),
        price_amount=_int("PRICE_AMOUNT", 1500),
        price_currency=os.getenv("PRICE_CURRENCY") or "KES",
        file_ttl_seconds=_int("FILE_TTL_SECONDS", 1800),
        followup_after_hours=_int("FOLLOWUP_AFTER_HOURS", 48),
        max_hold_seconds=_int("MAX_HOLD_SECONDS", 172800),
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or "",
        ops_token=os.getenv("OPS_TOKEN") or "",
        allow_local_urls=_flag("ALLOW_LOCAL_URLS", app_mode == "mock"),
        log_level=os.getenv("LOG_LEVEL") or "INFO",
        background_tasks=_flag("BACKGROUND_TASKS", True),
        data_dir=data_dir,
        log_path=data_dir / "safu.log",
    )
