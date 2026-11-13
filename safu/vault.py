import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class VaultItem:
    token: str
    pack_id: str
    order_id: str
    filename: str
    created_at: str
    expires_at: str | None
    delivered: bool


class FileVault:
    """Generated documents on disk. The index stores filenames and expiry, not CV text."""

    def __init__(self, root: Path, ttl_seconds: int, max_hold_seconds: int):
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.max_hold_seconds = max_hold_seconds
        self.blob_dir = root / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"
        self.items: dict[str, VaultItem] = {}
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        for row in raw:
            item = VaultItem(**row)
            self.items[item.token] = item

    def _save(self) -> None:
        payload = [asdict(item) for item in self.items.values()]
        self.index_path.write_text(json.dumps(payload), encoding="utf-8")

    def put_pack(self, order_id: str, files: list[tuple[str, bytes]], now: datetime) -> str:
        pack_id = "pack_" + secrets.token_hex(6)
        created = _now_iso(now)
        for filename, data in files:
            token = secrets.token_urlsafe(18)
            path = self.blob_dir / token
            path.write_bytes(data)
            self.items[token] = VaultItem(
                token=token,
                pack_id=pack_id,
                order_id=order_id,
                filename=filename,
                created_at=created,
                expires_at=None,
                delivered=False,
            )
        self._save()
        return pack_id

    def list_pack(self, pack_id: str | None) -> list[VaultItem]:
        if not pack_id:
            return []
        rows = [item for item in self.items.values() if item.pack_id == pack_id]
        rows.sort(key=lambda item: item.filename)
        return rows

    def pack_ready(self, pack_id: str | None) -> bool:
        return bool(self.list_pack(pack_id))

    def read(self, token: str, now: datetime) -> tuple[VaultItem, bytes] | None:
        item = self.items.get(token)
        if item is None:
            return None
        if item.expires_at and _parse(item.expires_at) <= now:
            self._delete(item)
            return None
        path = self.blob_dir / token
        if not path.exists():
            self.items.pop(token, None)
            self._save()
            return None
        return item, path.read_bytes()

    def arm_expiry(self, pack_id: str, when: datetime) -> None:
        stamp = _now_iso(when)
        for item in self.items.values():
            if item.pack_id == pack_id:
                item.expires_at = stamp
                item.delivered = True
        self._save()

    def delete_pack(self, pack_id: str | None) -> None:
        for item in list(self.items.values()):
            if item.pack_id == pack_id:
                self._delete(item, persist=False)
        self._save()

    def sweep(self, now: datetime) -> int:
        removed = 0
        hold_before = now - timedelta(seconds=self.max_hold_seconds)
        for item in list(self.items.values()):
            expired = item.expires_at is not None and _parse(item.expires_at) <= now
            abandoned = (not item.delivered) and _parse(item.created_at) <= hold_before
            if expired or abandoned:
                self._delete(item, persist=False)
                removed += 1
        if removed:
            self._save()
        return removed

    def _delete(self, item: VaultItem, persist: bool = True) -> None:
        path = self.blob_dir / item.token
        if path.exists():
            path.unlink()
        self.items.pop(item.token, None)
        if persist:
            self._save()
