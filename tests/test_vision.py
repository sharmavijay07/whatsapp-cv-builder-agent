import json

import httpx

from safu.extract import extract_image
from safu.samples import OCR_TEXT, text_to_image
from safu.vision import GroqVision, build_vision


def test_groq_vision_is_primary_and_sends_an_image_block(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": OCR_TEXT}}]})

    settings.groq_api_key = "gsk_test"
    settings.ocr_provider = "auto"
    settings.groq_vision_model = "qwen/qwen3.8-27b"
    vision = build_vision(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert isinstance(vision, GroqVision)
    text = extract_image(text_to_image(OCR_TEXT), vision=vision, content_type="image/png")
    assert "SwiftHaul Logistics" in text
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["body"]["model"] == "qwen/qwen3.8-27b"
    content = seen["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/")


def test_tesseract_runs_when_vision_fails():
    class Down:
        def read_image(self, data, content_type=None):
            raise RuntimeError("vision down")

    text = extract_image(text_to_image(OCR_TEXT), vision=Down(), content_type="image/png")
    assert "SwiftHaul Logistics" in text


def test_tesseract_only_mode_skips_groq(settings):
    settings.groq_api_key = "gsk_test"
    settings.ocr_provider = "tesseract"
    assert build_vision(settings) is None
    settings.groq_api_key = ""
    settings.ocr_provider = "auto"
    assert build_vision(settings) is None
