import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger("safu")

ALLOWED_LOG_FIELDS = {
    "order_id",
    "step",
    "status",
    "error_code",
    "message_id",
    "provider",
    "model",
    "http_status",
    "codes",
    "count",
    "reason",
}


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    LOGGER.setLevel(numeric)
    if not any(isinstance(handler, logging.StreamHandler) for handler in LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)


def log_event(event: str, log_path: Path | None = None, **fields: object) -> None:
    payload: dict[str, object] = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in fields.items():
        if key in ALLOWED_LOG_FIELDS and value is not None:
            payload[key] = value
    line = json.dumps(payload, separators=(",", ":"), default=str)
    LOGGER.info(line)
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return
