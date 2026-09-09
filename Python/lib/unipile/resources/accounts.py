"""Connected provider accounts."""

from ..models import Account
from ..transport import Transport


class AccountsResource:
    """Everything under `/accounts`."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list(self) -> list[Account]:
        """Every account connected to this Unipile tenant."""
        body = self._transport.get("/api/v1/accounts")
        return [Account.model_validate(item) for item in body.get("items", [])]

    def get(self, account_id: str) -> Account:
        return Account.model_validate(self._transport.get(f"/api/v1/accounts/{account_id}"))
