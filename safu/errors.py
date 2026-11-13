class DeliveryError(Exception):
    """Raised when an outbound message cannot be handed to the channel."""


class ExtractError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class FetchError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LLMError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PaymentAuthError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
