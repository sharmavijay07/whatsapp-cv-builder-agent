"""Groq vision transcription for CV and job photos.

The free vision model is the primary reader. Tesseract remains the fallback
when this call fails or no Groq key is configured. Official vision docs:
https://console.groq.com/docs/vision
"""

from __future__ import annotations

import base64
import io

import httpx

from safu.config import Settings
from safu.errors import LLMError
from safu.llm import _message_text

VISION_PROMPT = (
    "Transcribe this photo of a CV or a job post. "
    "Return only the text you can see, keeping line breaks. "
    "Do not invent employers, dates, qualifications, or numbers."
)


def _image_payload(data: bytes, content_type: str | None) -> tuple[bytes, str]:
    mime = content_type if content_type and content_type.startswith("image/") else ""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
        if max(image.size) > 1600 or len(data) > 4_000_000 or image.format not in {"PNG", "JPEG"}:
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            elif image.mode == "L" and image.format == "JPEG":
                pass
            longest = max(image.size)
            if longest > 1600:
                scale = 1600 / longest
                resized = (
                    max(1, int(image.size[0] * scale)),
                    max(1, int(image.size[1] * scale)),
                )
                image = image.resize(resized)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue(), "image/png"
        if image.format == "PNG":
            return data, "image/png"
        return data, "image/jpeg"
    except Exception:
        return data, mime or "image/png"


class GroqVision:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        client: httpx.Client | None = None,
        timeout: float = 45,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout = timeout

    def read_image(self, data: bytes, content_type: str | None = None) -> str:
        encoded_bytes, mime = _image_payload(data, content_type)
        encoded = base64.b64encode(encoded_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1800,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    ],
                }
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
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError("bad_response") from exc
        return _message_text(message).strip()


def build_vision(settings: Settings, client: httpx.Client | None = None) -> GroqVision | None:
    if not settings.groq_api_key:
        return None
    if (settings.ocr_provider or "auto").strip().lower() == "tesseract":
        return None
    return GroqVision(
        api_key=settings.groq_api_key,
        model=settings.groq_vision_model,
        base_url=settings.groq_base_url,
        client=client,
    )
