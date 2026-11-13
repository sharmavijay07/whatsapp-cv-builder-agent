import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from safu.config import Settings, load_settings
from safu.logutil import configure_logging, log_event
from safu.models import Incoming
from safu.samples import JOB_HTML
from safu.service import (
    AUTO,
    build_runtime,
    handle_incoming,
    inspector,
    open_demo_session,
    public_messages,
    run_followups,
    run_recovery,
    run_sweep,
    say,
)
from safu.whatsapp import mime_for, parse_webhook, verify_meta_signature

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _repeat(stop: asyncio.Event, interval: int, fn) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(fn)
        except Exception as exc:
            log_event("background_error", reason=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


def create_app(settings: Settings | None = None, llm=AUTO) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings, llm=llm)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        tasks = []
        if app.state.runtime.settings.background_tasks:
            tasks = [
                asyncio.create_task(_repeat(stop, 30, lambda: run_sweep(app.state.runtime))),
                asyncio.create_task(_repeat(stop, 60, lambda: run_recovery(app.state.runtime))),
                asyncio.create_task(_repeat(stop, 300, lambda: run_followups(app.state.runtime))),
            ]
        yield
        stop.set()
        for task in tasks:
            task.cancel()

    app = FastAPI(title="Safu", lifespan=lifespan)
    app.state.runtime = runtime
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def rt():
        return app.state.runtime

    def require_mock() -> None:
        if rt().settings.app_mode != "mock":
            raise HTTPException(status_code=404, detail="mock_disabled")

    def require_ops(request: Request) -> None:
        current = rt()
        if current.settings.app_mode == "mock" and not current.settings.ops_token:
            return
        token = request.headers.get("x-ops-token")
        if not current.settings.ops_token or token != current.settings.ops_token:
            raise HTTPException(status_code=401, detail="ops_unauthorized")

    def payload(session):
        return {
            "session_id": session.order.wa_id,
            "messages": public_messages(session),
            "inspector": inspector(rt(), session),
        }

    @app.get("/", response_class=HTMLResponse)
    def landing():
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page():
        return HTMLResponse((STATIC_DIR / "demo.html").read_text(encoding="utf-8"))

    @app.get("/samples/job", response_class=HTMLResponse)
    def sample_job():
        return HTMLResponse(JOB_HTML)

    @app.get("/api/health")
    def health():
        current = rt()
        return {
            "ok": True,
            "mode": current.settings.app_mode,
            "llm_provider": current.settings.llm_provider,
            "llm_model": current.settings.active_model,
            "llm_live": current.llm is not None,
            "llm_fallback": None if current.llm is not None else "safe_local",
            "store": current.store_name,
            "payment": current.payments.name,
            "file_ttl_seconds": current.settings.file_ttl_seconds,
        }

    @app.post("/api/demo/sessions")
    def create_demo_session():
        require_mock()
        session = open_demo_session(rt())
        return payload(session)

    @app.get("/api/demo/sessions/{session_id}")
    def get_demo_session(session_id: str):
        require_mock()
        session = rt().sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown_session")
        return payload(session)

    @app.get("/api/demo/sessions/{session_id}/inspector")
    def get_inspector(session_id: str):
        require_mock()
        session = rt().sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown_session")
        return inspector(rt(), session)

    @app.post("/api/demo/sessions/{session_id}/messages")
    def post_demo_message(
        session_id: str,
        text: str = Form(""),
        message_id: str = Form(""),
        sample: str = Form(""),
        file: UploadFile | None = File(None),
    ):
        require_mock()
        current = rt()
        session = current.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown_session")
        body = text or ""
        if sample and not body:
            body = sample if sample.startswith("sample") else f"sample {sample}"
        data = None
        filename = None
        content_type = None
        if file is not None and file.filename:
            data = file.file.read()
            filename = file.filename
            content_type = file.content_type
        incoming = Incoming(
            message_id=message_id or ("msg_" + uuid.uuid4().hex),
            wa_id=session_id,
            text=body,
            filename=filename,
            content_type=content_type,
            data=data,
        )
        handle_incoming(current, incoming)
        return payload(session)

    @app.post("/api/demo/sessions/{session_id}/simulate-failure")
    def simulate_failure(session_id: str):
        require_mock()
        current = rt()
        session = current.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown_session")
        if not session.order.pack_id or not current.vault.pack_ready(session.order.pack_id):
            raise HTTPException(status_code=400, detail="Generate a pack first")
        session.order.status = "active"
        session.order.step = "generating"
        session.order.last_error_code = "delivery_failed"
        session.order.delivery_attempts += 1
        for item in current.vault.list_pack(session.order.pack_id):
            item.expires_at = None
            item.delivered = False
        current.vault._save()
        session.transcript = [item for item in session.transcript if item.get("kind") != "document"]
        current.alerter.fire(
            "critical",
            "delivery_failed",
            session.order.id,
            "simulated",
            current.clock.now().isoformat(),
        )
        current.channel.deliver(
            session.order.wa_id,
            [say("Delivery failed on my side. I still have the files and will retry.")],
        )
        current.store.save(session.order)
        return payload(session)

    @app.post("/api/demo/sessions/{session_id}/simulate-abandonment")
    def simulate_abandonment(session_id: str):
        require_mock()
        current = rt()
        session = current.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown_session")
        if session.order.status != "active":
            return {"ok": False, "detail": "Finished orders are not nudged.", "sent": []}
        when = current.clock.now() - timedelta(hours=current.settings.followup_after_hours + 1)
        session.order.updated_at = when.isoformat()
        current.store.save(session.order)
        sent = run_followups(current, current.clock.now())
        return {"ok": True, "sent": sent, **payload(session)}

    @app.get("/files/{token}")
    def download_file(token: str):
        found = rt().vault.read(token, rt().clock.now())
        if found is None:
            return JSONResponse({"error": "expired"}, status_code=410)
        item, data = found
        return Response(
            content=data,
            media_type=mime_for(item.filename),
            headers={
                "Content-Disposition": f'attachment; filename="{item.filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/webhook")
    def verify_webhook(request: Request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge") or ""
        if mode == "subscribe" and token == rt().settings.whatsapp_verify_token:
            return PlainTextResponse(challenge)
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook")
    async def webhook(request: Request):
        body = await request.body()
        current = rt()
        if not verify_meta_signature(body, request.headers.get("x-hub-signature-256"), current.settings.whatsapp_app_secret):
            return Response(status_code=403)
        try:
            payload_json = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return {"ok": True}
        for incoming in parse_webhook(payload_json):
            await asyncio.to_thread(handle_incoming, current, incoming)
        return {"ok": True}

    @app.post("/api/ops/recovery/run")
    def ops_recovery(request: Request):
        require_ops(request)
        return {"ok": True, "recovered": run_recovery(rt())}

    @app.post("/api/ops/followups/run")
    def ops_followups(request: Request):
        require_ops(request)
        return {"ok": True, "sent": run_followups(rt())}

    @app.post("/api/ops/sweep")
    def ops_sweep(request: Request):
        require_ops(request)
        return {"ok": True, "removed": run_sweep(rt())}

    @app.get("/api/ops/alerts")
    def ops_alerts(request: Request):
        require_ops(request)
        return {
            "alerts": [
                {
                    "level": item.level,
                    "code": item.code,
                    "order_id": item.order_id,
                    "reason": item.reason,
                    "at": item.at,
                }
                for item in rt().alerter.items
            ]
        }

    @app.get("/favicon.svg")
    def favicon():
        icon = STATIC_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=404)

    return app
