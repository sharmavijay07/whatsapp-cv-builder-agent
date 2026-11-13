import json

import httpx

from safu.compose import build_safe_pack, compose
from safu.config import load_settings
from safu.errors import LLMError
from safu.llm import AnthropicLLM, OpenAICompatibleLLM, build_llm
from safu.models import Order, Session
from safu.parse import parse_cv, parse_job
from safu.samples import CV_TEXT, JOB_TEXT
from safu.service import Clock
from safu.alerts import Alerter


def test_defaults_point_at_groq(monkeypatch):
    for key in ("LLM_PROVIDER", "GROQ_MODEL", "GROQ_BASE_URL", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert settings.llm_provider == "groq"
    assert settings.groq_model == "openai/gpt-oss-120b"
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"
    assert build_llm(settings) is None


def test_groq_request_uses_the_openai_compatible_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": '{"summary":"Kept facts."}'}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenAICompatibleLLM(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        model="openai/gpt-oss-120b",
        provider_name="groq",
        client=client,
    )
    payload = llm.complete_json("system", "user")
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "openai/gpt-oss-120b"
    assert payload["summary"] == "Kept facts."


def test_optional_providers_are_configurable():
    settings = load_settings()
    settings.llm_provider = "gemini"
    settings.gemini_api_key = "gem-key"
    gemini = build_llm(settings)
    assert isinstance(gemini, OpenAICompatibleLLM)
    assert gemini.provider_name == "gemini"
    assert "generativelanguage.googleapis.com" in gemini.base_url

    settings.llm_provider = "openai"
    settings.openai_api_key = "sk-test"
    openai = build_llm(settings)
    assert openai.model == settings.openai_model
    assert openai.base_url == "https://api.openai.com/v1"

    settings.llm_provider = "anthropic"
    settings.anthropic_api_key = "ant-key"
    claude = build_llm(settings)
    assert isinstance(claude, AnthropicLLM)
    assert claude.model == settings.anthropic_model


def _ready_session():
    record = parse_cv(CV_TEXT)
    job = parse_job(JOB_TEXT)
    order = Order("ord_llm", "demo-llm", "payment", "active", "t", "t")
    return Session(
        order=order,
        name="Amina Wanjiku",
        email=record.email,
        phone=record.phone,
        location=record.location,
        cv_text=CV_TEXT,
        job_text=JOB_TEXT,
        record=record,
        job=job,
        confirmed=True,
        paid=True,
    )


class _Runtime:
    def __init__(self, llm):
        self.llm = llm
        self.clock = Clock()
        self.alerter = Alerter()
        self.settings = load_settings()
        self.settings.llm_provider = "groq"
        self.settings.groq_model = "openai/gpt-oss-120b"
        self.settings.log_path = None


class EvilLLM:
    def complete_json(self, system, user):
        del system, user
        return {
            "summary": "Raised revenue by 300% at Unilever.",
            "skills": ["Excel"],
            "experience": [
                {
                    "employer": "Unilever",
                    "title": "Director",
                    "start": "January 2010",
                    "end": "Present",
                    "bullets": ["Managed a budget of 9000000"],
                }
            ],
            "education": [
                {"institution": "Harvard University", "qualification": "MBA", "start": "2008", "end": "2010"}
            ],
            "cover_letter": "I led Unilever to 300% growth.",
            "interview": {
                "intro": "I managed 9000000 at Unilever.",
                "questions": ["How big was the 300%?"],
                "ask_them": ["Why Unilever?"],
            },
        }


class RateLimitLLM:
    def complete_json(self, system, user):
        del system, user
        raise LLMError("rate_limited")


def test_fabricated_groq_draft_falls_back_to_source_facts():
    session = _ready_session()
    runtime = _Runtime(EvilLLM())
    pack = compose(runtime, session)
    safe = build_safe_pack(session)
    assert pack.cv.roles[0].employer == safe.cv.roles[0].employer == "SwiftHaul Logistics"
    assert "Unilever" not in pack.cover_letter
    assert "300%" not in pack.cv.summary
    assert any(alert.code == "validation_blocked" for alert in runtime.alerter.items)


def test_groq_rate_limit_still_returns_a_pack():
    session = _ready_session()
    runtime = _Runtime(RateLimitLLM())
    pack = compose(runtime, session)
    assert pack.cv.roles[0].employer == "SwiftHaul Logistics"
    assert any(alert.code == "llm_rate_limited" for alert in runtime.alerter.items)
