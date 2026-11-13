import re
from dataclasses import dataclass


@dataclass
class PaymentIntent:
    order_id: str
    amount: int
    currency: str
    instructions: str


class StubPaymentGateway:
    """Stand-in for M-Pesa, a card acquirer, or any other gateway.

    `confirm` is the only method the conversation calls. A live gateway
    would create a checkout in `start` and accept a signed callback in
    `confirm`. This stub records a payment when the person replies PAY.
    """

    name = "stub"

    def start(self, order_id: str, amount: int, currency: str) -> PaymentIntent:
        formatted = f"{currency} {amount:,}"
        instructions = (
            f"The pack is {formatted}. "
            "This step uses a stub payment gateway, so nothing is charged.\n\n"
            "Reply PAY to continue."
        )
        return PaymentIntent(order_id=order_id, amount=amount, currency=currency, instructions=instructions)

    def confirm(self, order_id: str, proof: str) -> bool:
        del order_id
        return bool(re.match(r"^\s*(pay|paid)\b", proof or "", re.IGNORECASE))


def build_payments(provider: str) -> StubPaymentGateway:
    # Only the stub ships. A second class can be selected here later.
    del provider
    return StubPaymentGateway()
