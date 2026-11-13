import json
import re

import httpx

from safu.config import Settings
from safu.errors import LLMError

SYSTEM_PROMPT = (
    "You rewrite a CV and write a cover letter and interview notes as one JSON object. "
    "Use only facts in the user payload. "
    "Never add an employer, date, qualification, skill, or number that is not in the source. "
    "Keep every source role and every source qualification. Copy date strings exactly. "
    "Copy skills from the source skill list. "
    "The cover letter and interview notes may mention the target role and company. "
    "Do not turn requirements from the job post into claims about the candidate. "
    "Return JSON only with keys summary, skills, experience, education, cover_letter, interview. "
    "experience items use employer, title, start, end, bullets. "
    "education items use institution, qualification, start, end. "
    "interview uses intro, questions, ask_them. questions and ask_them are lists of strings."
)


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("not_json")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("not_json") from exc
    if not isinstance(payload, dict):
        raise LLMError("not_json")
    return payload


def _message_text(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        content = "".join(parts)
    if not str(content).strip():
        content = message.get("reasoning") or ""
    return str(content)


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        provider_name: str,
        client: httpx.Client | None = None,
        timeout: float = 45,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider_name = provider_name
        self.client = client
        self.timeout = timeout

    def complete_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 2200,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = self.base_url + "/chat/completions"
        own = self.client is None
        http = self.client or httpx.Client(timeout=self.timeout)
        try:
            response = http.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError("transport") from exc
        finally:
            if own:
                http.close()
        if response.status_code == 429:
            raise LLMError("rate_limited")
        if response.status_code >= 400:
            raise LLMError(f"http_{response.status_code}")
        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("bad_response") from exc
        return parse_json_object(_message_text(message))


class AnthropicLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        timeout: float = 45,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider_name = "anthropic"
        self.client = client
        self.timeout = timeout

    def complete_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": 2200,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = self.base_url + "/v1/messages"
        own = self.client is None
        http = self.client or httpx.Client(timeout=self.timeout)
        try:
            response = http.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError("transport") from exc
        finally:
            if own:
                http.close()
        if response.status_code == 429:
            raise LLMError("rate_limited")
        if response.status_code >= 400:
            raise LLMError(f"http_{response.status_code}")
        try:
            blocks = response.json()["content"]
            text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError("bad_response") from exc
        return parse_json_object(text)


def build_llm(settings: Settings, client: httpx.Client | None = None):
    provider = settings.llm_provider
    if provider == "local":
        return None
    if provider == "groq":
        if not settings.groq_api_key:
            return None
        return OpenAICompatibleLLM(
            base_url=settings.groq_base_url,
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            provider_name="groq",
            client=client,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return None
        return OpenAICompatibleLLM(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            provider_name="openai",
            client=client,
        )
    if provider == "gemini":
        if not settings.gemini_api_key:
            return None
        return OpenAICompatibleLLM(
            base_url=settings.gemini_base_url,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            provider_name="gemini",
            client=client,
        )
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            return None
        return AnthropicLLM(
            base_url=settings.anthropic_base_url,
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            client=client,
        )
    return None
