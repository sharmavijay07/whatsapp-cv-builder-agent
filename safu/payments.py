"""Payment gateways.

`PAYMENT_PROVIDER` selects one of:

- `stub` — demo only. Reply PAY. Nothing is charged.
- `mpesa` — Safaricom Daraja STK Push. Sandbox by default. Documents are
  released only after the callback reports ResultCode 0 for a checkout we
  started, and only when the amount matches.
- `paystack` — test-mode checkout link. Documents are released only after a
  webhook whose `x-paystack-signature` matches the raw body, for a reference
  we created, with a successful charge of at least the expected amount.

Card charges against a live Paystack key are refused unless
`PAYSTACK_ALLOW_LIVE=true`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from safu.config import Settings
from safu.errors import PaymentAuthError
from safu.logutil import log_event

SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE = "https://api.safaricom.co.ke"


@dataclass
class PaymentIntent:
    order_id: str
    amount: int
    currency: str
    instructions: str
    pending: bool = False
    reference: str = ""
    checkout_url: str = ""


@dataclass
class PaymentOutcome:
    order_id: str = ""
    status: str = "ignored"
    reason: str = ""


@dataclass
class PaymentRecord:
    reference: str
    order_id: str
    provider: str
    amount: int
    currency: str
    status: str
    provider_id: str = ""
    checkout_url: str = ""
    phone: str = ""
    updated_at: str = ""


def secrets_equal(presented: str | None, expected: str | None) -> bool:
    if not presented or not expected:
        return False
    try:
        return hmac.compare_digest(presented, expected)
    except (TypeError, ValueError):
        return False


def normalize_msisdn(value: str | None) -> str | None:
    """Kenyan numbers as 2547XXXXXXXX, the form Daraja expects."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if len(digits) == 9 and digits[0] in {"1", "7"}:
        return "254" + digits
    return None


def _money(amount: int, currency: str) -> str:
    return f"{currency} {amount:,}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nairobi_timestamp() -> str:
    """Daraja timestamps are East Africa Time (UTC+3), format YYYYMMDDHHMMSS."""
    try:
        from zoneinfo import ZoneInfo

        current = datetime.now(ZoneInfo("Africa/Nairobi"))
    except Exception:
        current = datetime.now(timezone.utc) + timedelta(hours=3)
    return current.strftime("%Y%m%d%H%M%S")


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _new_reference() -> str:
    # Daraja AccountReference allows 12 characters. Paystack accepts the same id.
    return "cv" + uuid.uuid4().hex[:10]


class PaymentLedger:
    """Payment references only. No CV text, names, or document bodies.

    The local SQLite file is the working copy. When Supabase is configured the
    same row is mirrored there (without the phone number) so a callback can
    still be matched after the free Render disk is wiped.
    """

    def __init__(
        self,
        path: Path,
        supabase_url: str = "",
        supabase_key: str = "",
        remote_client: httpx.Client | None = None,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._remote_url = supabase_url.rstrip("/") + "/rest/v1" if supabase_url else ""
        self._remote_key = supabase_key
        self._remote = remote_client
        self._owns_remote = False
        if self._remote_url and self._remote_key and self._remote is None:
            self._remote = httpx.Client(timeout=10)
            self._owns_remote = True
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    reference TEXT PRIMARY KEY,
                    provider_id TEXT,
                    order_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checkout_url TEXT,
                    phone TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS payments_order_idx ON payments (order_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS payments_provider_idx ON payments (provider_id)")

    def close(self) -> None:
        if self._owns_remote and self._remote is not None:
            self._remote.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self, row: sqlite3.Row | None) -> PaymentRecord | None:
        if row is None:
            return None
        return PaymentRecord(
            reference=row["reference"],
            order_id=row["order_id"],
            provider=row["provider"],
            amount=int(row["amount"]),
            currency=row["currency"],
            status=row["status"],
            provider_id=row["provider_id"] or "",
            checkout_url=row["checkout_url"] or "",
            phone=row["phone"] or "",
            updated_at=row["updated_at"],
        )

    def save(self, record: PaymentRecord) -> None:
        record.updated_at = record.updated_at or _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO payments (
                    reference, provider_id, order_id, provider, amount, currency,
                    status, checkout_url, phone, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reference) DO UPDATE SET
                    provider_id=excluded.provider_id,
                    order_id=excluded.order_id,
                    provider=excluded.provider,
                    amount=excluded.amount,
                    currency=excluded.currency,
                    status=excluded.status,
                    checkout_url=excluded.checkout_url,
                    phone=excluded.phone,
                    updated_at=excluded.updated_at
                """,
                (
                    record.reference,
                    record.provider_id,
                    record.order_id,
                    record.provider,
                    record.amount,
                    record.currency,
                    record.status,
                    record.checkout_url,
                    record.phone,
                    record.updated_at,
                ),
            )
        self._mirror(record)

    def _mirror(self, record: PaymentRecord) -> None:
        if not self._remote_url or self._remote is None:
            return
        payload = {
            "reference": record.reference,
            "provider_id": record.provider_id,
            "order_id": record.order_id,
            "provider": record.provider,
            "amount": record.amount,
            "currency": record.currency,
            "status": record.status,
            "checkout_url": record.checkout_url,
            "updated_at": record.updated_at,
        }
        headers = {
            "apikey": self._remote_key,
            "Authorization": f"Bearer {self._remote_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        try:
            response = self._remote.post(
                f"{self._remote_url}/payments?on_conflict=reference",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError):
            log_event("payment_mirror_failed", provider=record.provider, order_id=record.order_id, error_code="supabase")

    def _remote_one(self, column: str, value: str) -> PaymentRecord | None:
        if not value or not self._remote_url or self._remote is None:
            return None
        headers = {"apikey": self._remote_key, "Authorization": f"Bearer {self._remote_key}"}
        try:
            response = self._remote.get(
                f"{self._remote_url}/payments",
                headers=headers,
                params={column: f"eq.{value}", "select": "*", "limit": "1"},
            )
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return None
        if not rows:
            return None
        row = rows[0]
        record = PaymentRecord(
            reference=str(row.get("reference") or ""),
            order_id=str(row.get("order_id") or ""),
            provider=str(row.get("provider") or ""),
            amount=int(row.get("amount") or 0),
            currency=str(row.get("currency") or ""),
            status=str(row.get("status") or ""),
            provider_id=str(row.get("provider_id") or ""),
            checkout_url=str(row.get("checkout_url") or ""),
            phone="",
            updated_at=str(row.get("updated_at") or ""),
        )
        if record.reference:
            self.save(record)
        return record

    def get_by_reference(self, reference: str) -> PaymentRecord | None:
        if not reference:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE reference = ?", (reference,)).fetchone()
        found = self._row(row)
        return found or self._remote_one("reference", reference)

    def get_by_provider_id(self, provider_id: str) -> PaymentRecord | None:
        if not provider_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE provider_id = ?", (provider_id,)).fetchone()
        found = self._row(row)
        return found or self._remote_one("provider_id", provider_id)

    def for_order(self, order_id: str) -> list[PaymentRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payments WHERE order_id = ? ORDER BY updated_at DESC",
                (order_id,),
            ).fetchall()
        return [item for item in (self._row(row) for row in rows) if item is not None]

    def latest(self, order_id: str) -> PaymentRecord | None:
        rows = self.for_order(order_id)
        return rows[0] if rows else None

    def is_paid(self, order_id: str) -> bool:
        return any(item.status == "paid" for item in self.for_order(order_id))

    def mark(self, reference: str, status: str, provider_id: str | None = None) -> PaymentRecord | None:
        current = self.get_by_reference(reference)
        if current is None:
            return None
        current.status = status
        if provider_id:
            current.provider_id = provider_id
        current.updated_at = _now()
        self.save(current)
        return current


class PaymentGateway:
    name = "stub"
    ready = True

    def start(
        self,
        order_id: str,
        amount: int,
        currency: str,
        *,
        phone: str | None = None,
        email: str | None = None,
    ) -> PaymentIntent:
        raise NotImplementedError

    def confirm(self, order_id: str, proof: str) -> bool:
        del order_id, proof
        return False

    def status_line(self, order_id: str) -> str:
        del order_id
        return "Reply PAY to continue."

    def handle_mpesa(self, body: bytes, token: str | None) -> PaymentOutcome | None:
        del body, token
        return None

    def handle_paystack(self, body: bytes, signature: str | None) -> PaymentOutcome | None:
        del body, signature
        return None


class StubPaymentGateway(PaymentGateway):
    """Demo gateway. Reply PAY. No money moves."""

    name = "stub"
    ready = True

    def start(
        self,
        order_id: str,
        amount: int,
        currency: str,
        *,
        phone: str | None = None,
        email: str | None = None,
    ) -> PaymentIntent:
        del phone, email
        instructions = (
            f"The pack is {_money(amount, currency)}. "
            "This step uses a stub payment gateway, so nothing is charged.\n\n"
            "Reply PAY to continue."
        )
        return PaymentIntent(order_id=order_id, amount=amount, currency=currency, instructions=instructions)

    def confirm(self, order_id: str, proof: str) -> bool:
        del order_id
        return bool(re.match(r"^\s*(pay|paid)\b", proof or "", re.IGNORECASE))

    def status_line(self, order_id: str) -> str:
        del order_id
        return "Reply PAY to record the stub payment. Nothing is charged."


class MpesaGateway(PaymentGateway):
    """Safaricom Daraja Lipa Na M-Pesa Online (STK Push).

    Default host is the sandbox. Set MPESA_ENV=production only with production
    credentials. The callback is trusted only when it names a CheckoutRequestID
    this process created (or mirrored), the result code is 0, and the amount
    is at least the amount we prompted. An optional MPESA_CALLBACK_SECRET is
    required as `?token=` on the callback URL when it is set.
    """

    name = "mpesa"

    def __init__(self, settings: Settings, client: httpx.Client | None = None, ledger: PaymentLedger | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)
        self._owns_client = client is None
        self.ledger = ledger or PaymentLedger(
            settings.data_dir / "payments.sqlite",
            settings.supabase_url,
            settings.supabase_service_key,
        )
        self._token = ""
        self._token_until = datetime.now(timezone.utc)
        env = (settings.mpesa_env or "sandbox").strip().lower()
        if settings.mpesa_base_url:
            self.base_url = settings.mpesa_base_url.rstrip("/")
        elif env == "production":
            self.base_url = PRODUCTION_BASE
        else:
            self.base_url = SANDBOX_BASE
        self.ready = bool(
            settings.mpesa_consumer_key
            and settings.mpesa_consumer_secret
            and settings.mpesa_shortcode
            and settings.mpesa_passkey
        )

    def close(self) -> None:
        self.ledger.close()
        if self._owns_client:
            self.client.close()

    def _callback_url(self) -> str:
        base = (self.settings.mpesa_callback_url or f"{self.settings.public_base_url}/webhooks/mpesa").rstrip("/")
        # The path itself has no query. A configured full URL may already include one.
        secret = self.settings.mpesa_callback_secret
        if not secret:
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}token={secret}"

    def _access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now < self._token_until:
            return self._token
        response = self.client.get(
            f"{self.base_url}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            auth=(self.settings.mpesa_consumer_key, self.settings.mpesa_consumer_secret),
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("mpesa_oauth")
        expires = _as_int(payload.get("expires_in")) or 3000
        self._token = token
        self._token_until = now + timedelta(seconds=max(60, expires - 60))
        return token

    def start(
        self,
        order_id: str,
        amount: int,
        currency: str,
        *,
        phone: str | None = None,
        email: str | None = None,
    ) -> PaymentIntent:
        del email
        price = _money(amount, currency)
        if not self.ready:
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions=(
                    f"The pack is {price}. M-Pesa isn't available on this server yet. "
                    "Say HUMAN and a person will help. Nothing has been charged."
                ),
            )
        msisdn = normalize_msisdn(phone)
        if not msisdn:
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                pending=True,
                instructions=(
                    f"The pack is {price}. Reply with the M-Pesa number that should receive the prompt, "
                    "for example 0712 345 678. I'll send the documents only after Safaricom confirms."
                ),
            )
        reference = _new_reference()
        timestamp = _nairobi_timestamp()
        raw_password = f"{self.settings.mpesa_shortcode}{self.settings.mpesa_passkey}{timestamp}"
        password = base64.b64encode(raw_password.encode("utf-8")).decode("ascii")
        payload = {
            "BusinessShortCode": self.settings.mpesa_shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": str(int(amount)),
            "PartyA": msisdn,
            "PartyB": self.settings.mpesa_shortcode,
            "PhoneNumber": msisdn,
            "CallBackURL": self._callback_url(),
            "AccountReference": reference,
            "TransactionDesc": "Safu CV pack",
        }
        record = PaymentRecord(
            reference=reference,
            order_id=order_id,
            provider="mpesa",
            amount=int(amount),
            currency=currency,
            status="pending",
        )
        try:
            token = self._access_token()
            response = self.client.post(
                f"{self.base_url}/mpesa/stkpush/v1/processrequest",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError, RuntimeError):
            log_event("payment_start_failed", provider="mpesa", order_id=order_id, error_code="stk_push")
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions=(
                    "I couldn't reach M-Pesa. Reply with the phone number to try again, or say HUMAN. "
                    "Nothing has been charged."
                ),
            )
        response_code = str(body.get("ResponseCode") or "")
        checkout_id = str(body.get("CheckoutRequestID") or "")
        if response_code != "0" or not checkout_id:
            log_event("payment_start_failed", provider="mpesa", order_id=order_id, error_code="stk_rejected")
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions=(
                    "Safaricom did not accept that prompt. Check the number and reply with it again, "
                    "or say HUMAN. Nothing has been charged."
                ),
            )
        record.provider_id = checkout_id
        self.ledger.save(record)
        log_event("payment_pending", provider="mpesa", order_id=order_id, status="pending")
        return PaymentIntent(
            order_id=order_id,
            amount=amount,
            currency=currency,
            pending=True,
            reference=reference,
            instructions=(
                f"The pack is {price}. I sent an M-Pesa prompt to {msisdn}. Enter your PIN. "
                "I'll send the documents when Safaricom confirms the payment. Nothing is sent before that."
            ),
        )

    def confirm(self, order_id: str, proof: str) -> bool:
        del proof
        return self.ledger.is_paid(order_id)

    def status_line(self, order_id: str) -> str:
        if not self.ready:
            return "M-Pesa isn't configured on this server yet. Say HUMAN and a person will help."
        latest = self.ledger.latest(order_id)
        if latest and latest.status == "pending":
            return (
                "I'm waiting for M-Pesa to confirm the payment. Enter the PIN on the phone prompt, "
                "or reply PAY to send it again."
            )
        if latest and latest.status == "failed":
            return "That payment did not complete. Reply PAY to send a new M-Pesa prompt, or say HUMAN."
        return "Reply with the M-Pesa phone number to pay, for example 0712 345 678."

    def handle_mpesa(self, body: bytes, token: str | None) -> PaymentOutcome | None:
        secret = self.settings.mpesa_callback_secret
        if secret and not secrets_equal(token, secret):
            raise PaymentAuthError("mpesa_callback")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return PaymentOutcome(status="ignored", reason="bad_json")
        callback = _stk_callback(payload)
        if not callback:
            return PaymentOutcome(status="ignored", reason="empty")
        checkout_id = str(callback.get("CheckoutRequestID") or "")
        account = str(callback.get("AccountReference") or "")
        record = self.ledger.get_by_provider_id(checkout_id) or self.ledger.get_by_reference(account)
        if record is None or record.provider != "mpesa":
            log_event("payment_unmatched", provider="mpesa", error_code="unknown_checkout")
            return PaymentOutcome(status="ignored", reason="unmatched")
        result_code = _as_int(callback.get("ResultCode"))
        if result_code != 0:
            self.ledger.mark(record.reference, "failed", checkout_id or record.provider_id)
            log_event("payment_failed", provider="mpesa", order_id=record.order_id, error_code=str(result_code))
            return PaymentOutcome(order_id=record.order_id, status="failed", reason=f"result_{result_code}")
        amount = _callback_amount(callback)
        if amount is None or amount < record.amount:
            self.ledger.mark(record.reference, "failed", checkout_id or record.provider_id)
            log_event("payment_failed", provider="mpesa", order_id=record.order_id, error_code="amount")
            return PaymentOutcome(order_id=record.order_id, status="failed", reason="amount_mismatch")
        self.ledger.mark(record.reference, "paid", checkout_id or record.provider_id)
        log_event("payment_confirmed", provider="mpesa", order_id=record.order_id, status="paid")
        return PaymentOutcome(order_id=record.order_id, status="paid", reason="stk_confirmed")


class PaystackGateway(PaymentGateway):
    """Paystack test checkout. Live secret keys are refused unless explicitly allowed."""

    name = "paystack"

    def __init__(self, settings: Settings, client: httpx.Client | None = None, ledger: PaymentLedger | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)
        self._owns_client = client is None
        self.ledger = ledger or PaymentLedger(
            settings.data_dir / "payments.sqlite",
            settings.supabase_url,
            settings.supabase_service_key,
        )
        secret = settings.paystack_secret_key
        live = secret.startswith("sk_live_")
        self.ready = bool(secret) and (not live or settings.paystack_allow_live)
        self._blocked_live = bool(secret) and live and not settings.paystack_allow_live

    def close(self) -> None:
        self.ledger.close()
        if self._owns_client:
            self.client.close()

    def start(
        self,
        order_id: str,
        amount: int,
        currency: str,
        *,
        phone: str | None = None,
        email: str | None = None,
    ) -> PaymentIntent:
        del phone
        price = _money(amount, currency)
        if self._blocked_live:
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions=(
                    f"The pack is {price}. This server is holding a live Paystack key and will not charge it. "
                    "Say HUMAN. Nothing has been charged."
                ),
            )
        if not self.ready:
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions=(
                    f"The pack is {price}. Card payment isn't available on this server yet. "
                    "Say HUMAN and a person will help. Nothing has been charged."
                ),
            )
        if not email or "@" not in email:
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                pending=True,
                instructions="I need an email address on the order before I can open a Paystack checkout.",
            )
        reference = _new_reference()
        payload = {
            "email": email,
            "amount": int(amount) * 100,
            "currency": currency,
            "reference": reference,
            "callback_url": f"{self.settings.public_base_url}/payment/return",
            "metadata": {"order_id": order_id},
        }
        try:
            response = self.client.post(
                "https://api.paystack.co/transaction/initialize",
                headers={
                    "Authorization": f"Bearer {self.settings.paystack_secret_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            log_event("payment_start_failed", provider="paystack", order_id=order_id, error_code="initialize")
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions="I couldn't open the Paystack page. Reply PAY to try again, or say HUMAN. Nothing has been charged.",
            )
        data = body.get("data") or {}
        url = str(data.get("authorization_url") or "")
        if not body.get("status") or not url:
            log_event("payment_start_failed", provider="paystack", order_id=order_id, error_code="initialize")
            return PaymentIntent(
                order_id=order_id,
                amount=amount,
                currency=currency,
                instructions="Paystack did not open a checkout. Reply PAY to try again, or say HUMAN. Nothing has been charged.",
            )
        self.ledger.save(
            PaymentRecord(
                reference=reference,
                order_id=order_id,
                provider="paystack",
                amount=int(amount),
                currency=currency,
                status="pending",
                provider_id=str(data.get("access_code") or ""),
                checkout_url=url,
            )
        )
        log_event("payment_pending", provider="paystack", order_id=order_id, status="pending")
        return PaymentIntent(
            order_id=order_id,
            amount=amount,
            currency=currency,
            pending=True,
            reference=reference,
            checkout_url=url,
            instructions=(
                f"The pack is {price}. Pay here:\n{url}\n\n"
                "I'll send the documents when Paystack confirms the payment. Nothing is sent before that."
            ),
        )

    def confirm(self, order_id: str, proof: str) -> bool:
        del proof
        return self.ledger.is_paid(order_id)

    def status_line(self, order_id: str) -> str:
        if self._blocked_live:
            return "Payments are paused because a live Paystack key is set. Say HUMAN. Nothing has been charged."
        if not self.ready:
            return "Card payment isn't configured on this server yet. Say HUMAN and a person will help."
        latest = self.ledger.latest(order_id)
        if latest and latest.checkout_url and latest.status != "paid":
            return (
                "I'm waiting for Paystack to confirm the payment. "
                f"The checkout link is still:\n{latest.checkout_url}\n\nReply PAY if you need a new link."
            )
        return "Reply PAY and I'll send a Paystack checkout link. Documents follow the confirmation."

    def handle_paystack(self, body: bytes, signature: str | None) -> PaymentOutcome | None:
        secret = self.settings.paystack_secret_key
        if not secret or not signature:
            raise PaymentAuthError("paystack_signature")
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
        if not secrets_equal(signature.strip(), expected):
            raise PaymentAuthError("paystack_signature")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return PaymentOutcome(status="ignored", reason="bad_json")
        event = payload.get("event")
        data = payload.get("data") or {}
        if event != "charge.success":
            return PaymentOutcome(status="ignored", reason=str(event or "ignored"))
        reference = str(data.get("reference") or "")
        record = self.ledger.get_by_reference(reference)
        if record is None or record.provider != "paystack":
            log_event("payment_unmatched", provider="paystack", error_code="unknown_reference")
            return PaymentOutcome(status="ignored", reason="unmatched")
        metadata = data.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        meta_order = str(metadata.get("order_id") or record.order_id)
        if meta_order != record.order_id:
            self.ledger.mark(record.reference, "failed")
            return PaymentOutcome(order_id=record.order_id, status="failed", reason="order_mismatch")
        if str(data.get("status") or "").lower() != "success":
            return PaymentOutcome(order_id=record.order_id, status="ignored", reason="not_success")
        charged = _as_int(data.get("amount"))
        currency = str(data.get("currency") or record.currency).upper()
        if charged is None or charged < record.amount * 100 or currency != record.currency.upper():
            self.ledger.mark(record.reference, "failed")
            log_event("payment_failed", provider="paystack", order_id=record.order_id, error_code="amount")
            return PaymentOutcome(order_id=record.order_id, status="failed", reason="amount_mismatch")
        provider_id = str(data.get("id") or record.provider_id)
        self.ledger.mark(record.reference, "paid", provider_id)
        log_event("payment_confirmed", provider="paystack", order_id=record.order_id, status="paid")
        return PaymentOutcome(order_id=record.order_id, status="paid", reason="charge.success")


def _stk_callback(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    body = payload.get("Body") or payload.get("body") or {}
    if isinstance(body, dict) and isinstance(body.get("stkCallback"), dict):
        return body["stkCallback"]
    callback = payload.get("stkCallback")
    if isinstance(callback, dict):
        return callback
    return {}


def _callback_amount(callback: dict) -> int | None:
    metadata = callback.get("CallbackMetadata") or {}
    items = metadata.get("Item") if isinstance(metadata, dict) else None
    if not isinstance(items, list):
        return _as_int(callback.get("Amount"))
    for item in items:
        if isinstance(item, dict) and str(item.get("Name") or "").lower() == "amount":
            return _as_int(item.get("Value"))
    return None


def build_payments(settings: Settings, client: httpx.Client | None = None) -> PaymentGateway:
    provider = (settings.payment_provider or "stub").strip().lower()
    if provider in {"mpesa", "m-pesa", "daraja"}:
        return MpesaGateway(settings, client=client)
    if provider in {"paystack", "card"}:
        return PaystackGateway(settings, client=client)
    return StubPaymentGateway()
