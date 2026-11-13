import hashlib
import hmac
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
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body[:4096]},
            },
        )

    def send_document(self, to: str, filename: str, data: bytes, caption: str) -> None:
        media_id = self._upload(filename, data)
        self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": (caption or filename)[:1024]},
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
        response = self.client.post(
            self._root() + "/media",
            headers=self._auth(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)},
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
