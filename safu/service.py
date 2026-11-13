import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from safu.alerts import Alerter
from safu.compose import compose, delivery_text, render_pack
from safu.config import Settings
from safu.errors import DeliveryError, ExtractError, FetchError
from safu.extract import extract_any, fetch_url, html_to_text
from safu.logutil import log_event
from safu.models import Incoming, Order, Outbound, Session, TurnResult
from safu.parse import EMAIL_RE, looks_like_name, parse_cv, parse_job
from safu.payments import StubPaymentGateway, build_payments
from safu.samples import CV_TEXT, JOB_HTML, JOB_TEXT, OCR_TEXT, text_to_image, text_to_pdf
from safu.store import build_store
from safu.vault import FileVault
from safu.whatsapp import CloudWhatsApp

JOB_PHOTO_TEXT = (
    "Logistics Coordinator\n"
    "Freightline Kenya - Nairobi\n"
    "Dispatch, inventory, Excel, and cycle counts for a Nairobi hub.\n"
)

FOLLOWUP_TEXT = (
    "You started a Safu CV pack about 48 hours ago and it is not finished. "
    "Reply here to continue. I keep CV text only in short-lived working files, so I may ask you to send the CV again. "
    "Reply STOP to close this order."
)

SAMPLES = {
    "sample cv": "cv",
    "use sample cv": "cv",
    "sample job": "job",
    "use sample job": "job",
    "sample photo": "photo",
    "use sample photo": "photo",
    "sample url": "url",
    "sample link": "url",
    "use sample link": "url",
}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
CORRECTION_RE = re.compile(r"^(name|email|phone|location)\s*(?:is|:)\s*(.+)$", re.IGNORECASE)
GREETINGS = {"hi", "hello", "hey", "start", "habari", "safu", "hi safu", "hello safu", "hey safu"}
AUTO = object()


class Clock:
    def __init__(self) -> None:
        self.frozen: datetime | None = None

    def now(self) -> datetime:
        if self.frozen is not None:
            return self.frozen
        return datetime.now(timezone.utc)


class Channel:
    def __init__(self, cloud: CloudWhatsApp | None = None):
        self.cloud = cloud
        self.runtime = None
        self.fail_remaining = 0

    def bind(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    def deliver(self, wa_id: str, messages: list[Outbound]) -> None:
        if self.fail_remaining:
            self.fail_remaining -= 1
            raise DeliveryError("down")
        runtime = self.runtime
        if self.cloud is not None and runtime is not None:
            for message in messages:
                if message.kind == "text":
                    self.cloud.send_text(wa_id, message.text)
                elif message.token:
                    found = runtime.vault.read(message.token, runtime.clock.now())
                    if found is None:
                        raise DeliveryError("missing_file")
                    item, data = found
                    self.cloud.send_document(wa_id, item.filename, data, message.caption or item.filename)
        session = runtime.sessions.get(wa_id) if runtime is not None else None
        if session is None:
            return
        stamp = runtime.clock.now().isoformat()
        for message in messages:
            session.transcript.append(
                {
                    "id": "out-" + uuid.uuid4().hex[:10],
                    "direction": "out",
                    "kind": message.kind,
                    "text": message.text,
                    "filename": message.filename,
                    "token": message.token,
                    "at": stamp,
                }
            )


@dataclass
class Runtime:
    settings: Settings
    store: object
    store_name: str
    sessions: dict[str, Session]
    vault: FileVault
    alerter: Alerter
    channel: Channel
    cloud: CloudWhatsApp | None
    llm: object | None
    payments: StubPaymentGateway
    clock: Clock
    lock: threading.RLock
    work_dir: Path


def build_runtime(settings: Settings, llm=AUTO, channel: Channel | None = None, cloud: CloudWhatsApp | None = None) -> Runtime:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store, store_name = build_store(
        settings.order_store,
        settings.supabase_url,
        settings.supabase_service_key,
        settings.data_dir,
    )
    if llm is AUTO:
        from safu.llm import build_llm

        llm = build_llm(settings)
    if settings.app_mode == "live" and cloud is None and settings.whatsapp_token and settings.whatsapp_phone_number_id:
        cloud = CloudWhatsApp(settings)
    if channel is None:
        channel = Channel(cloud=cloud if settings.app_mode == "live" else None)
    runtime = Runtime(
        settings=settings,
        store=store,
        store_name=store_name,
        sessions={},
        vault=FileVault(settings.data_dir / "files", settings.file_ttl_seconds, settings.max_hold_seconds),
        alerter=Alerter(settings.alert_webhook_url, settings.log_path),
        channel=channel,
        cloud=cloud,
        llm=llm,
        payments=build_payments(settings.payment_provider),
        clock=Clock(),
        lock=threading.RLock(),
        work_dir=settings.data_dir / "work",
    )
    runtime.work_dir.mkdir(parents=True, exist_ok=True)
    channel.bind(runtime)
    log_event(
        "boot",
        settings.log_path,
        provider=settings.llm_provider,
        model=settings.active_model,
        status=settings.app_mode,
        reason=store_name,
    )
    return runtime


def say(text: str) -> Outbound:
    return Outbound(kind="text", text=text)


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _command(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _is_human(command: str) -> bool:
    return bool(re.match(r"^(human|agent|person|representative)\b", command))


def _is_stop(command: str) -> bool:
    return bool(re.match(r"^(stop|cancel|unsubscribe)\b", command))


def _is_resend(command: str) -> bool:
    return bool(re.match(r"^(resend|send again)\b", command))


def _is_yes(text: str) -> bool:
    return bool(re.match(r"^(yes|yep|yeah|confirm|confirmed)\b", text.strip(), re.IGNORECASE))


def _is_greeting(command: str) -> bool:
    return command in GREETINGS


def _valid_email(value: str | None) -> bool:
    return bool(value and EMAIL_RE.fullmatch(value.strip()))


def _valid_phone(value: str | None) -> bool:
    if not value:
        return False
    return len(re.sub(r"\D", "", value)) >= 9


def _clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _new_order(rt: Runtime, wa_id: str) -> Order:
    stamp = rt.clock.now().isoformat()
    order = Order(
        id="ord_" + uuid.uuid4().hex[:12],
        wa_id=wa_id,
        step="ask_name",
        status="active",
        created_at=stamp,
        updated_at=stamp,
    )
    rt.store.save(order)
    return order


def new_session(rt: Runtime, wa_id: str) -> Session:
    return Session(order=_new_order(rt, wa_id))


def _snapshot_path(rt: Runtime, order_id: str) -> Path:
    return rt.work_dir / f"{order_id}.json"


def write_snapshot(rt: Runtime, session: Session) -> None:
    if not (session.cv_text or session.job_text or session.name or session.email):
        return
    path = _snapshot_path(rt, session.order.id)
    payload = {
        "name": session.name,
        "email": session.email,
        "phone": session.phone,
        "location": session.location,
        "cv_text": session.cv_text,
        "job_text": session.job_text,
        "confirmed": session.confirmed,
        "paid": session.paid,
        "greeted": session.greeted,
        "unreadable": session.unreadable,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def delete_snapshot(rt: Runtime, order_id: str) -> None:
    path = _snapshot_path(rt, order_id)
    if path.exists():
        path.unlink()


def read_snapshot(rt: Runtime, order_id: str) -> dict | None:
    path = _snapshot_path(rt, order_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def session_from_snapshot(rt: Runtime, order: Order, snap: dict) -> Session:
    del rt
    session = Session(order=order, greeted=bool(snap.get("greeted", True)))
    session.name = snap.get("name")
    session.email = snap.get("email")
    session.phone = snap.get("phone")
    session.location = snap.get("location")
    session.cv_text = snap.get("cv_text")
    session.job_text = snap.get("job_text")
    session.confirmed = bool(snap.get("confirmed"))
    session.paid = bool(snap.get("paid"))
    session.unreadable = int(snap.get("unreadable") or 0)
    if session.cv_text:
        session.record = parse_cv(session.cv_text)
    if session.job_text:
        session.job = parse_job(session.job_text)
    return session


def load_session(rt: Runtime, wa_id: str) -> Session:
    current = rt.sessions.get(wa_id)
    if current is not None and current.order.status != "closed":
        return current
    order = rt.store.get_by_wa(wa_id)
    if order is not None and order.status != "closed":
        snap = read_snapshot(rt, order.id)
        session = session_from_snapshot(rt, order, snap) if snap else Session(order=order, greeted=True)
        rt.sessions[wa_id] = session
        return session
    session = new_session(rt, wa_id)
    rt.sessions[wa_id] = session
    return session


def ensure_session(rt: Runtime, order: Order) -> Session:
    current = rt.sessions.get(order.wa_id)
    if current is not None and current.order.id == order.id:
        return current
    snap = read_snapshot(rt, order.id)
    session = session_from_snapshot(rt, order, snap) if snap else Session(order=order, greeted=True)
    rt.sessions[order.wa_id] = session
    return session


def _remember_in(session: Session, text: str, filename: str | None, at: str) -> None:
    session.transcript.append(
        {
            "id": "in-" + uuid.uuid4().hex[:10],
            "direction": "in",
            "kind": "file" if filename else "text",
            "text": text,
            "filename": filename,
            "token": None,
            "at": at,
        }
    )


def _set_step(rt: Runtime, session: Session, step: str, awaiting: str | None = None) -> None:
    session.order.step = step
    session.order.awaiting_field = awaiting
    session.order.updated_at = rt.clock.now().isoformat()
    log_event(
        "step",
        rt.settings.log_path,
        order_id=session.order.id,
        step=step,
        status=session.order.status,
    )


def _maybe_whatsapp_phone(session: Session) -> None:
    if session.phone:
        return
    if session.order.wa_id.isdigit() and len(session.order.wa_id) >= 9:
        session.phone = "+" + session.order.wa_id


def greeting() -> Outbound:
    return say(
        "Hi, I'm Safu. I build an ATS-friendly CV, a cover letter, and an interview pack "
        "from your current CV and one job post.\n\n"
        "Nothing is written until you confirm the facts.\n\n"
        "What's your full name?"
    )


def summary_text(session: Session) -> str:
    record = session.record
    job = session.job
    lines = [
        "Here is what I will use. Nothing is generated until you reply YES.",
        "",
        f"Name: {session.name}",
        f"Email: {session.email}",
        f"Phone: {session.phone}",
        f"Location: {session.location}",
    ]
    if job and (job.title or job.company):
        target = job.title or "the role"
        if job.company:
            target += f" at {job.company}"
        lines.append(f"Target: {target}")
    lines.extend(["", "Experience:"])
    for role in record.roles:
        lines.append(f"- {role.title}, {role.employer} ({role.start} - {role.end})")
    if record.education:
        lines.extend(["", "Education:"])
        for edu in record.education:
            lines.append(f"- {edu.qualification}, {edu.institution} ({edu.start} - {edu.end})")
    lines.extend(
        [
            "",
            "Reply YES to continue to payment.",
            "I can update name, email, phone, or location. Example: email is you@example.com",
            "If an employer or date is wrong, say HUMAN. I will not silently change work history.",
        ]
    )
    return "\n".join(lines)


def _missing_prompt(field: str) -> str:
    prompts = {
        "email": "I still need your email address.",
        "phone": "I still need a phone number we can put on the CV.",
        "location": "Which city should appear on the CV?",
    }
    return prompts[field]


def advance(rt: Runtime, session: Session) -> TurnResult:
    _maybe_whatsapp_phone(session)
    missing = []
    if not _valid_email(session.email):
        missing.append("email")
    if not _valid_phone(session.phone):
        missing.append("phone")
    if not (session.location and session.location.strip()):
        missing.append("location")
    if missing:
        _set_step(rt, session, "ask_missing", missing[0])
        preview = summary_text(session).split("Reply YES")[0].strip()
        return TurnResult([say(_missing_prompt(missing[0]) + "\n\n" + preview)])
    _set_step(rt, session, "confirm", None)
    return TurnResult([say(summary_text(session))])


def _after_sources(rt: Runtime, session: Session) -> TurnResult:
    if not session.name:
        _set_step(rt, session, "ask_name", "name")
        return TurnResult([say("I saved that. What's your full name?")])
    if session.record is None:
        _set_step(rt, session, "ask_cv", None)
        return TurnResult([say("Send your CV as a PDF, Word file, photo, or link. Pasting the text works too.")])
    if session.job is None:
        _set_step(rt, session, "ask_job", None)
        return TurnResult([say("I have your CV. Send the job post next: paste the text, a link, a screenshot, or a PDF.")])
    return advance(rt, session)


def ingest_cv(rt: Runtime, session: Session, text: str) -> TurnResult:
    record = parse_cv(text)
    if record.confidence < 0.45 or not record.roles:
        session.unreadable += 1
        if session.unreadable >= 2:
            return do_handoff(rt, session, "unreadable_cv")
        return TurnResult(
            [
                say(
                    "I couldn't find a work history in that file. Send a clearer PDF, Word file, or paste the text. "
                    "Say HUMAN if you want a person to look."
                )
            ]
        )
    session.unreadable = 0
    session.cv_text = text
    session.record = record
    session.email = session.email or record.email
    session.phone = session.phone or record.phone
    session.location = session.location or record.location
    return _after_sources(rt, session)


def ingest_job(rt: Runtime, session: Session, text: str) -> TurnResult:
    job = parse_job(text)
    if len(text.strip()) < 30 or not (job.title or job.keywords):
        return TurnResult([say("That job post is too thin to match against. Paste more of the listing, or send a link.")])
    session.job_text = text
    session.job = job
    return _after_sources(rt, session)


def route_text(rt: Runtime, session: Session, text: str) -> TurnResult:
    if session.record is None or session.order.step == "ask_cv":
        return ingest_cv(rt, session, text)
    if session.job is None or session.order.step == "ask_job":
        return ingest_job(rt, session, text)
    return TurnResult(
        [say("I already have a CV and a job post. Reply YES if the summary is right, or say HUMAN.")]
    )


def ingest_bytes(rt: Runtime, session: Session, data: bytes, filename: str | None, content_type: str | None) -> TurnResult:
    if len(data) > 8_000_000:
        return TurnResult([say("That file is larger than 8 MB. Send a smaller PDF.")])
    try:
        text = extract_any(filename, content_type, data)
    except ExtractError as exc:
        session.unreadable += 1
        if session.unreadable >= 2:
            return do_handoff(rt, session, exc.code)
        if exc.code == "ocr_unavailable":
            detail = "Photo reading isn't available on this machine (tesseract is missing). Send a PDF or paste the text."
        elif exc.code == "unsupported_type":
            detail = "Send a PDF, Word file, photo, or plain text."
        else:
            detail = "I couldn't read that file. Send a clearer PDF or paste the text."
        return TurnResult([say(detail)])
    return route_text(rt, session, text)


def accept_name(rt: Runtime, session: Session, text: str) -> TurnResult:
    if len(text) > 80:
        return ingest_cv(rt, session, text)
    if not looks_like_name(text):
        return TurnResult([say("Send your full name, first and last. For example: Amina Wanjiku.")])
    session.name = _clean_name(text)
    return _after_sources(rt, session)


def accept_missing(rt: Runtime, session: Session, text: str) -> TurnResult:
    field = session.order.awaiting_field
    if field == "email":
        if not _valid_email(text):
            return TurnResult([say("Send a valid email address.")])
        session.email = text.strip()
    elif field == "phone":
        if not _valid_phone(text):
            return TurnResult([say("Send a phone number with at least 9 digits.")])
        session.phone = text.strip()
    elif field == "location":
        if len(text.strip()) < 2:
            return TurnResult([say("Send your city or town.")])
        session.location = text.strip()
    else:
        return advance(rt, session)
    return advance(rt, session)


def accept_confirm(rt: Runtime, session: Session, text: str) -> TurnResult:
    if _is_yes(text):
        session.confirmed = True
        _set_step(rt, session, "payment", None)
        intent = rt.payments.start(session.order.id, rt.settings.price_amount, rt.settings.price_currency)
        return TurnResult([say(intent.instructions)])
    match = CORRECTION_RE.match(text.strip())
    if match:
        field, value = match.group(1).lower(), match.group(2).strip()
        if field == "name" and looks_like_name(value):
            session.name = _clean_name(value)
        elif field == "email" and _valid_email(value):
            session.email = value
        elif field == "phone" and _valid_phone(value):
            session.phone = value
        elif field == "location" and len(value) >= 2:
            session.location = value
        else:
            return TurnResult([say(f"I couldn't use that {field}. Send it like: {field} is ...")])
        return advance(rt, session)
    return TurnResult(
        [
            say(
                "Reply YES to go to payment. I can update name, email, phone, or location, "
                "for example: email is you@example.com. Say HUMAN for a person."
            )
        ]
    )


def resend_replies(rt: Runtime, session: Session, lead: str) -> list[Outbound]:
    replies = [say(lead)]
    base = rt.settings.public_base_url
    for item in rt.vault.list_pack(session.order.pack_id):
        replies.append(
            Outbound(
                kind="document",
                text=f"{base}/files/{item.token}",
                token=item.token,
                filename=item.filename,
                caption=item.filename,
            )
        )
    return replies


def generate(rt: Runtime, session: Session) -> TurnResult:
    session.paid = True
    session.order.paid = True
    session.confirmed = True
    _set_step(rt, session, "generating", None)
    pack = compose(rt, session)
    files = render_pack(pack)
    session.order.pack_id = rt.vault.put_pack(session.order.id, files, rt.clock.now())
    rt.alerter.fire("info", "payment_stubbed", session.order.id, "stub", rt.clock.now().isoformat())
    replies = [say(delivery_text(pack))]
    base = rt.settings.public_base_url
    for item in rt.vault.list_pack(session.order.pack_id):
        replies.append(
            Outbound(
                kind="document",
                text=f"{base}/files/{item.token}",
                token=item.token,
                filename=item.filename,
                caption=item.filename,
            )
        )
    return TurnResult(replies, finalize=True)


def accept_payment(rt: Runtime, session: Session, text: str) -> TurnResult:
    if not rt.payments.confirm(session.order.id, text):
        return TurnResult([say("Reply PAY to record the stub payment. Nothing is charged.")])
    if session.record is None or session.job is None:
        return TurnResult([say("I still need your CV and the job post before I can write the pack.")])
    return generate(rt, session)


def do_handoff(rt: Runtime, session: Session, reason: str) -> TurnResult:
    session.order.handoff = True
    session.order.last_error_code = reason
    rt.alerter.fire("critical", "handoff", session.order.id, reason, rt.clock.now().isoformat())
    if session.order.pack_id and rt.vault.pack_ready(session.order.pack_id):
        replies = resend_replies(rt, session, "I've flagged this for a person. Your documents are attached below.")
        return TurnResult(replies, finalize=True)
    _set_step(rt, session, "handoff", None)
    session.order.status = "needs_human"
    return TurnResult(
        [
            say(
                "I've paused automatic writing and flagged this for a person. "
                "They'll pick up this chat. I have not invented a CV."
            )
        ]
    )


def do_stop(rt: Runtime, session: Session) -> TurnResult:
    session.order.status = "closed"
    session.order.step = "closed"
    session.order.followup_sent = True
    session.order.updated_at = rt.clock.now().isoformat()
    rt.vault.delete_pack(session.order.pack_id)
    session.order.pack_id = None
    delete_snapshot(rt, session.order.id)
    session.cv_text = None
    session.job_text = None
    session.record = None
    session.job = None
    return TurnResult([say("Closed. I deleted the working files for this order.")])


def do_resend(rt: Runtime, session: Session) -> TurnResult:
    if session.order.status != "delivered" or not session.order.pack_id:
        return TurnResult([say("There isn't a finished pack to resend yet.")])
    if not rt.vault.pack_ready(session.order.pack_id):
        return TurnResult(
            [
                say(
                    "Those files were deleted after 30 minutes. Reply NEW and send your CV again. "
                    "I did not keep a copy."
                )
            ]
        )
    when = rt.clock.now() + timedelta(seconds=rt.settings.file_ttl_seconds)
    rt.vault.arm_expiry(session.order.pack_id, when)
    return TurnResult(
        resend_replies(rt, session, "Sending your documents again. The new links expire in 30 minutes.")
    )


def reopen(rt: Runtime, session: Session) -> TurnResult:
    fresh = new_session(rt, session.order.wa_id)
    session.order = fresh.order
    session.greeted = True
    session.name = None
    session.email = None
    session.phone = None
    session.location = None
    session.cv_text = None
    session.job_text = None
    session.record = None
    session.job = None
    session.confirmed = False
    session.paid = False
    session.unreadable = 0
    session.transcript = []
    rt.sessions[session.order.wa_id] = session
    return TurnResult([greeting()])


def _sample_bytes(kind: str, session: Session) -> tuple[bytes, str, str]:
    if kind == "cv":
        return text_to_pdf(CV_TEXT), "sample-cv.pdf", "application/pdf"
    if kind == "job":
        return JOB_TEXT.encode("utf-8"), "sample-job.txt", "text/plain"
    if session.record is None:
        return text_to_image(OCR_TEXT), "sample-cv.png", "image/png"
    return text_to_image(JOB_PHOTO_TEXT), "sample-job.png", "image/png"


def dispatch_text(rt: Runtime, session: Session, text: str) -> TurnResult:
    step = session.order.step
    if step == "ask_name":
        return accept_name(rt, session, text)
    if step == "ask_cv":
        if len(text) >= 80:
            return ingest_cv(rt, session, text)
        return TurnResult([say("Send your CV as a PDF, Word file, photo, or link. Pasting the text works too.")])
    if step == "ask_job":
        if len(text) >= 20:
            return ingest_job(rt, session, text)
        return TurnResult([say("Send the job post as text, a link, a photo, or a PDF.")])
    if step == "ask_missing":
        return accept_missing(rt, session, text)
    if step == "confirm":
        return accept_confirm(rt, session, text)
    if step == "payment":
        return accept_payment(rt, session, text)
    if step == "generating":
        return TurnResult([say("I'm still preparing your documents.")])
    if step == "delivered":
        return TurnResult(
            [say("Your pack was already sent. Reply RESEND for fresh links, or NEW to start another application.")]
        )
    if step == "handoff":
        return TurnResult(
            [say("A person has been flagged on this order. I won't generate anything new until they step in.")]
        )
    if step == "closed":
        return reopen(rt, session)
    return TurnResult([say("What's your full name?")])


def turn(rt: Runtime, session: Session, inc: Incoming) -> TurnResult:
    stamp = rt.clock.now().isoformat()
    text = (inc.text or "").strip()
    command = _command(text)
    if inc.unsupported:
        _remember_in(session, text, None, stamp)
        return TurnResult([say("Send text, a PDF, a Word file, a photo, or a link.")])
    if _is_human(command):
        _remember_in(session, text, None, stamp)
        return do_handoff(rt, session, "user_requested")
    if _is_stop(command):
        _remember_in(session, text, None, stamp)
        return do_stop(rt, session)
    if _is_resend(command):
        _remember_in(session, text, None, stamp)
        return do_resend(rt, session)
    if command == "new":
        _remember_in(session, text, None, stamp)
        return reopen(rt, session)

    sample = SAMPLES.get(command)
    if sample and inc.data is None:
        if sample == "url":
            _remember_in(session, f"{rt.settings.public_base_url}/samples/job", None, stamp)
            return route_text(rt, session, html_to_text(JOB_HTML))
        data, filename, content_type = _sample_bytes(sample, session)
        _remember_in(session, filename, filename, stamp)
        return ingest_bytes(rt, session, data, filename, content_type)

    if inc.data:
        _remember_in(session, text or inc.filename or "upload", inc.filename, stamp)
        return ingest_bytes(rt, session, inc.data, inc.filename, inc.content_type)

    url_match = URL_RE.search(text)
    if url_match:
        _remember_in(session, text, None, stamp)
        url = url_match.group(0).rstrip(").,")
        if urlparse(url).path.rstrip("/") == "/samples/job":
            return route_text(rt, session, html_to_text(JOB_HTML))
        try:
            fetched = fetch_url(url, rt.settings.allow_local_urls)
        except FetchError:
            return TurnResult([say("I couldn't read that link. Paste the text, or send a PDF.")])
        return route_text(rt, session, fetched)

    _remember_in(session, text, None, stamp)
    if not session.greeted:
        session.greeted = True
        if not text or _is_greeting(command):
            _set_step(rt, session, "ask_name", "name")
            return TurnResult([greeting()])
        result = dispatch_text(rt, session, text)
        hello = say("Hi, I'm Safu. I'll use only the facts you confirm.")
        return TurnResult([hello, *result.replies], finalize=result.finalize)
    if not text:
        return TurnResult([say("Send a message, a file, or a link.")])
    return dispatch_text(rt, session, text)


def finalize(rt: Runtime, session: Session) -> None:
    session.order.step = "delivered"
    session.order.status = "delivered"
    session.order.last_error_code = None
    session.order.delivery_attempts = 0
    session.order.awaiting_field = None
    session.order.updated_at = rt.clock.now().isoformat()
    if session.order.pack_id:
        when = rt.clock.now() + timedelta(seconds=rt.settings.file_ttl_seconds)
        rt.vault.arm_expiry(session.order.pack_id, when)
    delete_snapshot(rt, session.order.id)
    session.cv_text = None
    session.job_text = None
    session.record = None
    session.job = None
    rt.store.save(session.order)
    rt.alerter.fire("info", "delivered", session.order.id, "delivered", rt.clock.now().isoformat())
    log_event(
        "delivered",
        rt.settings.log_path,
        order_id=session.order.id,
        step="delivered",
        status="delivered",
    )


def handle_incoming(rt: Runtime, inc: Incoming) -> None:
    with rt.lock:
        if rt.store.has_message(inc.message_id):
            log_event("duplicate_webhook", rt.settings.log_path, message_id=inc.message_id)
            return
        if inc.media_id and inc.data is None:
            session = load_session(rt, inc.wa_id)
            rt.store.remember(inc.message_id, session.order.id, rt.clock.now().isoformat())
            if rt.cloud is None:
                rt.channel.deliver(
                    inc.wa_id,
                    [say("I can't download WhatsApp media while mock mode is on. Upload the file in the demo chat.")],
                )
                rt.store.save(session.order)
                return
            try:
                data, mime = rt.cloud.download_media(inc.media_id)
            except DeliveryError:
                rt.alerter.fire(
                    "critical",
                    "delivery_failed",
                    session.order.id,
                    "media_download_failed",
                    rt.clock.now().isoformat(),
                )
                rt.channel.deliver(inc.wa_id, [say("I couldn't download that file from WhatsApp. Please send it again.")])
                rt.store.save(session.order)
                return
            inc.data = data
            inc.content_type = inc.content_type or mime
        session = load_session(rt, inc.wa_id)
        rt.store.remember(inc.message_id, session.order.id, rt.clock.now().isoformat())
        try:
            result = turn(rt, session, inc)
            if result.replies:
                rt.channel.deliver(session.order.wa_id, result.replies)
            if result.finalize:
                finalize(rt, session)
            else:
                session.order.updated_at = rt.clock.now().isoformat()
                rt.store.save(session.order)
                if session.order.status not in {"delivered", "closed"}:
                    write_snapshot(rt, session)
                elif session.order.status == "closed":
                    delete_snapshot(rt, session.order.id)
        except DeliveryError:
            session.order.last_error_code = "delivery_failed"
            session.order.delivery_attempts += 1
            session.order.step = "generating"
            session.order.status = "active"
            session.order.updated_at = rt.clock.now().isoformat()
            rt.store.save(session.order)
            write_snapshot(rt, session)
            rt.alerter.fire(
                "critical",
                "delivery_failed",
                session.order.id,
                "delivery_failed",
                rt.clock.now().isoformat(),
            )
        except Exception as exc:
            log_event(
                "generation_failed",
                rt.settings.log_path,
                order_id=session.order.id,
                error_code="generation_failed",
                reason=type(exc).__name__,
            )
            session.order.last_error_code = "generation_failed"
            session.order.delivery_attempts += 1
            session.order.status = "active"
            session.order.updated_at = rt.clock.now().isoformat()
            rt.store.save(session.order)
            write_snapshot(rt, session)
            rt.alerter.fire(
                "critical",
                "generation_failed",
                session.order.id,
                "generation_failed",
                rt.clock.now().isoformat(),
            )
            try:
                rt.channel.deliver(
                    session.order.wa_id,
                    [say("I hit an error while preparing your documents. I will retry. You will still get them.")],
                )
            except DeliveryError:
                pass


def run_recovery(rt: Runtime) -> list[str]:
    fixed: list[str] = []
    with rt.lock:
        for order in list(rt.store.all()):
            if order.status != "active":
                continue
            if order.last_error_code not in {"delivery_failed", "generation_failed"}:
                continue
            if order.delivery_attempts >= 5:
                order.status = "needs_human"
                order.handoff = True
                order.step = "handoff"
                order.last_error_code = "recovery_exhausted"
                order.updated_at = rt.clock.now().isoformat()
                rt.store.save(order)
                rt.alerter.fire(
                    "critical",
                    "recovery_exhausted",
                    order.id,
                    "recovery_exhausted",
                    rt.clock.now().isoformat(),
                )
                try:
                    rt.channel.deliver(
                        order.wa_id,
                        [
                            say(
                                "I couldn't finish automatically. A person has been alerted. "
                                "Send your CV again if a document has not arrived."
                            )
                        ],
                    )
                except DeliveryError:
                    pass
                continue
            session = ensure_session(rt, order)
            try:
                if order.last_error_code == "delivery_failed" and rt.vault.pack_ready(order.pack_id):
                    replies = resend_replies(
                        rt,
                        session,
                        "Sending your documents again after a delivery error. Links expire 30 minutes after this message.",
                    )
                    rt.channel.deliver(order.wa_id, replies)
                    finalize(rt, session)
                    fixed.append(order.id)
                    continue
                if session.record and session.job and session.confirmed and session.paid:
                    result = generate(rt, session)
                    if result.replies:
                        rt.channel.deliver(order.wa_id, result.replies)
                    if result.finalize:
                        finalize(rt, session)
                    fixed.append(order.id)
                    continue
                rt.channel.deliver(
                    order.wa_id,
                    [say("I lost the working copy of your CV. Please send it again. I do not keep a copy in the database.")],
                )
                order.step = "ask_cv"
                order.last_error_code = None
                order.updated_at = rt.clock.now().isoformat()
                rt.store.save(order)
                rt.alerter.fire(
                    "warning",
                    "recovery_needs_reupload",
                    order.id,
                    "missing_workfile",
                    rt.clock.now().isoformat(),
                )
            except DeliveryError:
                order.delivery_attempts += 1
                order.last_error_code = "delivery_failed"
                order.updated_at = rt.clock.now().isoformat()
                rt.store.save(order)
                rt.alerter.fire(
                    "critical",
                    "delivery_failed",
                    order.id,
                    "retry_failed",
                    rt.clock.now().isoformat(),
                )
            except Exception:
                order.delivery_attempts += 1
                order.last_error_code = "generation_failed"
                order.updated_at = rt.clock.now().isoformat()
                rt.store.save(order)
                log_event(
                    "generation_failed",
                    rt.settings.log_path,
                    order_id=order.id,
                    error_code="generation_failed",
                    reason="retry_failed",
                )
                rt.alerter.fire(
                    "critical",
                    "generation_failed",
                    order.id,
                    "retry_failed",
                    rt.clock.now().isoformat(),
                )
    return fixed


def run_followups(rt: Runtime, now: datetime | None = None) -> list[str]:
    now = now or rt.clock.now()
    cutoff = now - timedelta(hours=rt.settings.followup_after_hours)
    sent: list[str] = []
    with rt.lock:
        for order in list(rt.store.all()):
            if order.status != "active" or order.followup_sent:
                continue
            if _parse_dt(order.updated_at) > cutoff:
                continue
            try:
                rt.channel.deliver(order.wa_id, [say(FOLLOWUP_TEXT)])
            except DeliveryError:
                rt.alerter.fire("critical", "delivery_failed", order.id, "followup_failed", now.isoformat())
                continue
            order.followup_sent = True
            order.updated_at = now.isoformat()
            rt.store.save(order)
            rt.alerter.fire("info", "followup_sent", order.id, "abandoned_48h", now.isoformat())
            sent.append(order.id)
    return sent


def run_sweep(rt: Runtime, now: datetime | None = None) -> int:
    now = now or rt.clock.now()
    removed = rt.vault.sweep(now)
    if rt.work_dir.exists():
        cutoff = now.timestamp() - rt.settings.max_hold_seconds
        for path in rt.work_dir.glob("*.json"):
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                removed += 1
    if removed:
        log_event("sweep", rt.settings.log_path, count=removed, reason="ttl")
    return removed


def row_contains_cv(order: Order, session: Session | None) -> bool:
    blob = json.dumps(order.to_dict()).casefold()
    if session is None or not session.cv_text:
        return False
    for line in session.cv_text.splitlines():
        snippet = line.strip()
        if len(snippet) > 40 and snippet.casefold() in blob:
            return True
    return False


def inspector(rt: Runtime, session: Session) -> dict:
    alerts = [
        {"level": item.level, "code": item.code, "reason": item.reason, "at": item.at}
        for item in rt.alerter.for_order(session.order.id)
    ]
    return {
        "database_row": session.order.to_dict(),
        "database_contains_cv_text": row_contains_cv(session.order, session),
        "store": rt.store_name,
        "ephemeral": {
            "cv_in_memory": bool(session.cv_text),
            "job_in_memory": bool(session.job_text),
            "confirmed": session.confirmed,
            "paid": session.paid,
        },
        "llm": {
            "provider": rt.settings.llm_provider,
            "model": rt.settings.active_model,
            "live": rt.llm is not None,
            "fallback": None if rt.llm is not None else "safe_local",
        },
        "alerts": alerts,
    }


def public_messages(session: Session) -> list[dict]:
    return [
        {
            "id": item["id"],
            "direction": item["direction"],
            "kind": item["kind"],
            "text": item.get("text") or "",
            "filename": item.get("filename"),
            "token": item.get("token"),
            "at": item["at"],
        }
        for item in session.transcript
    ]


def open_demo_session(rt: Runtime) -> Session:
    session = new_session(rt, "demo-" + uuid.uuid4().hex[:10])
    session.greeted = True
    rt.sessions[session.order.wa_id] = session
    rt.channel.deliver(session.order.wa_id, [greeting()])
    rt.store.save(session.order)
    write_snapshot(rt, session)
    return session
