from dataclasses import dataclass

import httpx

from safu.logutil import log_event


@dataclass
class Alert:
    level: str
    code: str
    order_id: str
    reason: str
    at: str


class Alerter:
    def __init__(self, webhook_url: str = "", log_path=None, client: httpx.Client | None = None):
        self.webhook_url = webhook_url
        self.log_path = log_path
        self.client = client
        self.items: list[Alert] = []

    def fire(self, level: str, code: str, order_id: str, reason: str, at: str) -> None:
        self.items.append(Alert(level=level, code=code, order_id=order_id, reason=reason, at=at))
        self.items = self.items[-200:]
        log_event(
            "alert",
            self.log_path,
            error_code=code,
            order_id=order_id,
            reason=reason,
            status=level,
        )
        if not self.webhook_url:
            return
        payload = {"level": level, "code": code, "order_id": order_id, "reason": reason}
        try:
            if self.client is not None:
                response = self.client.post(self.webhook_url, json=payload)
                response.raise_for_status()
            else:
                httpx.post(self.webhook_url, json=payload, timeout=5)
        except (httpx.HTTPError, OSError):
            log_event("alert_webhook_failed", self.log_path, order_id=order_id, error_code=code)

    def for_order(self, order_id: str) -> list[Alert]:
        return [item for item in self.items if item.order_id == order_id][-20:]
