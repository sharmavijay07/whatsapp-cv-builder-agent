import hashlib
import hmac
import re
from typing import Any

import httpx

from safu.config import Settings
from safu.errors import DeliveryError
from safu.models import Incoming

MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def mime_for(filename: str) -> str:
    lower = filename.lower()
    for suffix, value in MIME.items():
        if lower.endswith(suffix):
            return value
    return "application/octet-stream"


def safe_filename(filename: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "file")
    cleaned = cleaned.strip("._")[:80]
    return cleaned or "file"


def wa_recipient(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits or value


def verify_meta_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not secret:
        return True
    if not header:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest
    return hmac.compare_digest(expected, header.strip())


def _incoming_from_message(message: dict) -> Incoming:
    incoming = Incoming(message_id=str(message.get("id") or ""), wa_id=str(message.get("from") or ""))
    kind = message.get("type")
    if kind == "text":
        incoming.text = (message.get("text") or {}).get("body") or ""
    elif kind == "document":
        document = message.get("document") or {}
        incoming.media_id = document.get("id")
        incoming.filename = document.get("filename") or "upload"
        incoming.content_type = document.get("mime_type")
        incoming.text = document.get("caption")
    elif kind == "image":
        image = message.get("image") or {}
        incoming.media_id = image.get("id")
        incoming.filename = "upload.jpg"
        incoming.content_type = image.get("mime_type") or "image/jpeg"
        incoming.text = image.get("caption")
    elif kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        incoming.text = reply.get("title") or reply.get("id") or ""
    elif kind == "button":
        button = message.get("button") or {}
        incoming.text = button.get("text") or button.get("payload") or ""
    else:
        incoming.unsupported = True
    return incoming


def parse_webhook(payload: dict) -> list[Incoming]:
    found: list[Incoming] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                if message.get("id") and message.get("from"):
                    found.append(_incoming_from_message(message))
    return found


class CloudWhatsApp:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.whatsapp_token}"}

    def _root(self) -> str:
        version = self.settings.whatsapp_graph_version
        phone_id = self.settings.whatsapp_phone_number_id
        return f"https://graph.facebook.com/{version}/{phone_id}"

    def send_text(self, to: str, body: str) -> None:
        self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_recipient(to),
                "type": "text",
                "text": {"preview_url": False, "body": body[:4096]},
            },
        )

    def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        action = []
        for button_id, title in buttons[:3]:
            label = (title or button_id or "OK")[:20]
            action.append({"type": "reply", "reply": {"id": (button_id or label)[:256], "title": label}})
        if not action or not body.strip():
            raise DeliveryError("buttons_invalid")
        self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_recipient(to),
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body[:1024]},
                    "action": {"buttons": action},
                },
            },
        )

    def send_cta_url(self, to: str, body: str, display_text: str, url: str) -> None:
        self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_recipient(to),
                "type": "interactive",
                "interactive": {
                    "type": "cta_url",
                    "body": {"text": body[:1024]},
                    "action": {
                        "name": "cta_url",
                        "parameters": {"display_text": (display_text or "Open")[:20], "url": url},
                    },
                },
            },
        )

    def send_template(
        self,
        to: str,
        name: str,
        language: str = "en",
        body_parameters: list[str] | None = None,
    ) -> None:
        template: dict[str, Any] = {"name": name, "language": {"code": language or "en"}}
        if body_parameters:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value[:1024]} for value in body_parameters],
                }
            ]
        self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_recipient(to),
                "type": "template",
                "template": template,
            },
        )

    def send_document(self, to: str, filename: str, data: bytes, caption: str) -> None:
        safe_name = safe_filename(filename)
        media_id = self._upload(safe_name, data)
        self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_recipient(to),
                "type": "document",
                "document": {"id": media_id, "filename": safe_name, "caption": (caption or safe_name)[:1024]},
            },
        )

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        version = self.settings.whatsapp_graph_version
        meta = self.client.get(
            f"https://graph.facebook.com/{version}/{media_id}",
            headers=self._auth(),
        )
        if meta.status_code >= 400:
            raise DeliveryError("media_meta_failed")
        info: dict[str, Any] = meta.json()
        blob = self.client.get(info["url"], headers=self._auth())
        if blob.status_code >= 400:
            raise DeliveryError("media_download_failed")
        return blob.content, info.get("mime_type") or "application/octet-stream"

    def _upload(self, filename: str, data: bytes) -> str:
        mime = mime_for(filename)
        safe_name = safe_filename(filename)
        response = self.client.post(
            self._root() + "/media",
            headers=self._auth(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (safe_name, data, mime)},
        )
        if response.status_code >= 400:
            raise DeliveryError("upload_failed")
        media_id = response.json().get("id")
        if not media_id:
            raise DeliveryError("upload_failed")
        return str(media_id)

    def _post(self, path: str, payload: dict) -> None:
        response = self.client.post(self._root() + "/" + path, headers={**self._auth(), "Content-Type": "application/json"}, json=payload)
        if response.status_code >= 400:
            raise DeliveryError("send_failed")
