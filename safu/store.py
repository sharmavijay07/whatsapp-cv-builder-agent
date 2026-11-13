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


def build_store(order_store: str, supabase_url: str, supabase_key: str):
    if order_store == "supabase" and supabase_url and supabase_key:
        return SupabaseOrderStore(supabase_url, supabase_key), "supabase"
    return MemoryOrderStore(), "memory"
