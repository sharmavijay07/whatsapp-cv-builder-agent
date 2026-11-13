import sqlite3
import threading
from pathlib import Path

import httpx

from safu.models import ORDER_FIELDS, Order, order_from_row


class MemoryOrderStore:
    """Order rows and message ids only. There is no field for CV or job text."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.by_wa: dict[str, str] = {}
        self.messages: dict[str, dict] = {}

    def save(self, order: Order) -> None:
        self.orders[order.id] = order
        self.by_wa[order.wa_id] = order.id

    def get(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def get_by_wa(self, wa_id: str) -> Order | None:
        order_id = self.by_wa.get(wa_id)
        if not order_id:
            return None
        return self.orders.get(order_id)

    def all(self) -> list[Order]:
        return list(self.orders.values())

    def has_message(self, message_id: str) -> bool:
        return message_id in self.messages

    def remember(self, message_id: str, order_id: str, received_at: str) -> None:
        self.messages[message_id] = {"order_id": order_id, "received_at": received_at}

    def dump(self) -> dict:
        return {
            "orders": [order.to_dict() for order in self.orders.values()],
            "messages": [
                {"message_id": message_id, "order_id": payload["order_id"], "received_at": payload["received_at"]}
                for message_id, payload in self.messages.items()
            ],
        }


class SupabaseOrderStore:
    """Supabase REST store. The payload is limited to ORDER_FIELDS."""

    def __init__(self, url: str, key: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/") + "/rest/v1"
        self.client = client or httpx.Client(timeout=15)
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def save(self, order: Order) -> None:
        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        response = self.client.post(
            f"{self.url}/orders?on_conflict=id",
            headers=headers,
            json=order.to_dict(),
        )
        response.raise_for_status()

    def get_by_wa(self, wa_id: str) -> Order | None:
        response = self.client.get(
            f"{self.url}/orders",
            headers=self.headers,
            params={"wa_id": f"eq.{wa_id}", "select": ",".join(ORDER_FIELDS), "limit": "1"},
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        return order_from_row(rows[0])

    def all(self) -> list[Order]:
        response = self.client.get(
            f"{self.url}/orders",
            headers=self.headers,
            params={"select": ",".join(ORDER_FIELDS)},
        )
        response.raise_for_status()
        return [order_from_row(row) for row in response.json()]

    def has_message(self, message_id: str) -> bool:
        response = self.client.get(
            f"{self.url}/processed_messages",
            headers=self.headers,
            params={"message_id": f"eq.{message_id}", "select": "message_id", "limit": "1"},
        )
        response.raise_for_status()
        return bool(response.json())

    def remember(self, message_id: str, order_id: str, received_at: str) -> None:
        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        response = self.client.post(
            f"{self.url}/processed_messages?on_conflict=message_id",
            headers=headers,
            json={"message_id": message_id, "order_id": order_id, "received_at": received_at},
        )
        response.raise_for_status()

    def dump(self) -> dict:
        return {
            "orders": [order.to_dict() for order in self.all()],
            "messages": [],
        }


class SQLiteOrderStore:
    """Local order rows. The schema is ORDER_FIELDS plus message ids. No CV text."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            columns = ", ".join(f"{name} TEXT" for name in ORDER_FIELDS if name != "id")
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, {columns})"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS wa_index (wa_id TEXT PRIMARY KEY, order_id TEXT NOT NULL)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, order: Order) -> None:
        data = order.to_dict()
        assignments = ", ".join(f"{name}=?" for name in ORDER_FIELDS if name != "id")
        values = [str(data[name]) if data[name] is not None else None for name in ORDER_FIELDS]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO orders ({", ".join(ORDER_FIELDS)})
                VALUES ({", ".join("?" for _ in ORDER_FIELDS)})
                ON CONFLICT(id) DO UPDATE SET {assignments}
                """,
                [*values, *values[1:]],
            )
            conn.execute(
                """
                INSERT INTO wa_index (wa_id, order_id) VALUES (?, ?)
                ON CONFLICT(wa_id) DO UPDATE SET order_id=excluded.order_id
                """,
                (order.wa_id, order.id),
            )

    def get(self, order_id: str) -> Order | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return order_from_row(dict(row)) if row else None

    def get_by_wa(self, wa_id: str) -> Order | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT orders.* FROM orders
                JOIN wa_index ON wa_index.order_id = orders.id
                WHERE wa_index.wa_id = ?
                """,
                (wa_id,),
            ).fetchone()
        return order_from_row(dict(row)) if row else None

    def all(self) -> list[Order]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM orders").fetchall()
        return [order_from_row(dict(row)) for row in rows]

    def has_message(self, message_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT message_id FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def remember(self, message_id: str, order_id: str, received_at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_messages (message_id, order_id, received_at)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (message_id, order_id, received_at),
            )

    def dump(self) -> dict:
        with self._lock, self._connect() as conn:
            orders = conn.execute("SELECT * FROM orders").fetchall()
            messages = conn.execute(
                "SELECT message_id, order_id, received_at FROM processed_messages"
            ).fetchall()
        return {
            "orders": [order_from_row(dict(row)).to_dict() for row in orders],
            "messages": [dict(row) for row in messages],
        }


def build_store(order_store: str, supabase_url: str, supabase_key: str, data_dir: Path | None = None):
    """Supabase when it is configured. Otherwise a local SQLite file, or memory when asked."""
    directory = data_dir or Path("var")
    if order_store == "supabase" and supabase_url and supabase_key:
        return SupabaseOrderStore(supabase_url, supabase_key), "supabase"
    if order_store == "memory":
        return MemoryOrderStore(), "memory"
    return SQLiteOrderStore(directory / "orders.sqlite"), "sqlite"
