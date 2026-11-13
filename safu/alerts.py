import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

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
    """Owner notifications.

    Gmail (smtp.gmail.com:465) and an optional Telegram bot are the live
    channels. An existing webhook is still honoured. When none of those are
    configured the alerter stays in demo mode: the alert is kept in memory
    and in the log, and nothing is sent.
    """

    def __init__(
        self,
        webhook_url: str = "",
        log_path=None,
        client: httpx.Client | None = None,
        *,
        gmail_user: str = "",
        gmail_app_password: str = "",
        owner_email: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        mailer=None,
    ):
        self.webhook_url = webhook_url
        self.log_path = log_path
        self.client = client
        self.gmail_user = gmail_user
        self.gmail_app_password = gmail_app_password
        self.owner_email = owner_email
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.mailer = mailer
        self.items: list[Alert] = []

    @property
    def mode(self) -> str:
        channels: list[str] = []
        if self.gmail_user and self.gmail_app_password and self.owner_email:
            channels.append("gmail")
        if self.telegram_bot_token and self.telegram_chat_id:
            channels.append("telegram")
        if self.webhook_url:
            channels.append("webhook")
        return "+".join(channels) if channels else "demo"

    def fire(self, level: str, code: str, order_id: str, reason: str, at: str) -> None:
        alert = Alert(level=level, code=code, order_id=order_id, reason=reason, at=at)
        self.items.append(alert)
        self.items = self.items[-200:]
        log_event(
            "alert",
            self.log_path,
            error_code=code,
            order_id=order_id,
            reason=reason,
            status=level,
        )
        self._send_webhook(alert)
        self._send_gmail(alert)
        self._send_telegram(alert)

    def _send_webhook(self, alert: Alert) -> None:
        if not self.webhook_url:
            return
        payload = {
            "level": alert.level,
            "code": alert.code,
            "order_id": alert.order_id,
            "reason": alert.reason,
        }
        try:
            if self.client is not None:
                response = self.client.post(self.webhook_url, json=payload)
                response.raise_for_status()
            else:
                httpx.post(self.webhook_url, json=payload, timeout=5)
        except (httpx.HTTPError, OSError):
            log_event("alert_webhook_failed", self.log_path, order_id=alert.order_id, error_code=alert.code)

    def _send_gmail(self, alert: Alert) -> None:
        if not (self.gmail_user and self.gmail_app_password and self.owner_email):
            return
        message = EmailMessage()
        message["Subject"] = f"[Safu] {alert.level}: {alert.code}"
        message["From"] = self.gmail_user
        message["To"] = self.owner_email
        message.set_content(
            "\n".join(
                [
                    f"Level: {alert.level}",
                    f"Code: {alert.code}",
                    f"Order: {alert.order_id}",
                    f"Reason: {alert.reason}",
                    f"At: {alert.at}",
                    "",
                    "This alert does not include CV text.",
                ]
            )
        )
        try:
            if self.mailer is not None:
                self.mailer(message)
                return
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
                smtp.login(self.gmail_user, self.gmail_app_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException):
            log_event("alert_email_failed", self.log_path, order_id=alert.order_id, error_code=alert.code)

    def _send_telegram(self, alert: Alert) -> None:
        if not (self.telegram_bot_token and self.telegram_chat_id):
            return
        text = f"Safu {alert.level}: {alert.code}\nOrder {alert.order_id}\n{alert.reason}\n{alert.at}"
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": text[:4000]}
        try:
            if self.client is not None:
                response = self.client.post(url, json=payload)
                response.raise_for_status()
            else:
                httpx.post(url, json=payload, timeout=5)
        except (httpx.HTTPError, OSError):
            log_event("alert_telegram_failed", self.log_path, order_id=alert.order_id, error_code=alert.code)

    def for_order(self, order_id: str) -> list[Alert]:
        return [item for item in self.items if item.order_id == order_id][-20:]
