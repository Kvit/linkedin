"""The client facade: wires settings, transport, budget and resources together."""

from types import TracebackType
from typing import Self

import httpx

from .budget import SendBudget
from .config import UnipileSettings
from .errors import ConfigError
from .pacing import HumanCadence
from .resources.accounts import AccountsResource
from .resources.messaging import MessagingResource
from .resources.search import SearchResource
from .resources.users import UsersResource
from .transport import Transport


class UnipileClient:
    """Entry point for every LinkedIn contact and messaging operation.

        with UnipileClient.from_env() as li:
            for relation in li.users.iter_relations():
                ...

    The client is the only place an `httpx.Client` is constructed, so base URL
    and authentication are configured once. It is sync by design: notebooks are
    the primary consumer and the domain is rate limited, so concurrency would be
    a liability rather than a feature.
    """

    def __init__(self, settings: UnipileSettings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.Client(
            base_url=settings.base_url,
            headers={
                "X-API-KEY": settings.api_key.get_secret_value(),
                "accept": "application/json",
            },
            timeout=settings.timeout_seconds,
        )
        self._transport = Transport(self.http)
        self._account_id: str | None = settings.account_id

        budget = SendBudget(
            path=settings.budget_state_path,
            # A callable, so the account resolves before the very first budget
            # check rather than after it. A placeholder here meant the opening
            # operation of every client instance was checked against an empty
            # counter.
            account_id=lambda: self.account_id,
            limits={
                "invite": settings.max_invites_per_day,
                "message": settings.max_messages_per_day,
                "profile": settings.max_profile_fetches_per_day,
            },
            cadence=HumanCadence(
                settings.min_delay_seconds,
                settings.max_delay_seconds,
                long_pause_every=settings.long_pause_every,
                long_pause_min=settings.long_pause_min_seconds,
                long_pause_max=settings.long_pause_max_seconds,
            ),
            usage_warn_pct=settings.usage_warn_pct,
            usage_halt_pct=settings.usage_halt_pct,
        )
        self.budget = budget

        self.accounts = AccountsResource(self._transport)
        self.users = UsersResource(
            self._transport,
            account_id=lambda: self.account_id,
            budget=budget,
            default_sections=settings.profile_sections,
            throttle_retries=settings.throttle_retries,
        )
        self.messaging = MessagingResource(
            self._transport, account_id=lambda: self.account_id, budget=budget
        )
        self.search = SearchResource(self._transport, account_id=lambda: self.account_id)

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> Self:
        """Build a client from `UNIPILE_*` environment variables."""
        return cls(UnipileSettings.from_env(env_file=env_file))

    @property
    def account_id(self) -> str:
        """The connected account, from settings or resolved once on first use."""
        if self._account_id is None:
            accounts = self.accounts.list()
            if not accounts:
                raise ConfigError(
                    type="config/no_account",
                    title="No account is connected to this Unipile tenant",
                    detail="Connect a LinkedIn account, or set UNIPILE_ACCOUNT_ID.",
                )
            self._account_id = accounts[0].id
        return self._account_id

    @property
    def writes_blocked(self) -> bool:
        """True once LinkedIn restricted the account during this session."""
        return self._transport.writes_blocked

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
