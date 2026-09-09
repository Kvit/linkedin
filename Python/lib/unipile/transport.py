"""HTTP transport: authentication, retry policy, and error mapping.

The retry policy is the load-bearing part of this module.

``POST /users/invite``, ``POST /chats`` and ``POST /chats/{id}/messages`` are not
idempotent. Retrying one after a timeout sends a second real invitation or
message to a real person, so writes are never retried under any circumstance.
Reads retry only on transport failures and 5xx. A 429 is never retried on either
path -- it is the provider telling us to stop.
"""

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from .errors import (
    AccountRestricted,
    CircuitOpen,
    RateLimited,
    UnipileError,
    raise_for_response,
)

RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
# Unipile's gateway returns 502 in bursts lasting seconds. A notebook run that
# has already spent profile budget should wait one out rather than die, so these
# are tens of seconds -- reads are idempotent, and nothing here is interactive.
BACKOFF_SECONDS = (2.0, 10.0, 30.0)

FormData = list[tuple[str, str]]


def encode_form(data: dict[str, Any]) -> FormData:
    """Encode a payload the way ``unipile-node-sdk`` builds its ``FormData``.

    Verified against ``src/resources/messaging.resource.ts`` (``startNewChat``):
    arrays become repeated keys, nested objects use bracket notation, and
    booleans are the strings ``"true"``/``"false"``. ``None`` values are omitted.

    The ``/emails`` resource JSON-encodes array values instead; that convention
    is specific to email and is deliberately not used here.
    """
    fields: FormData = []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_value is not None:
                    fields.append((f"{key}[{sub_key}]", _scalar(sub_value)))
        elif isinstance(value, (list, tuple)):
            fields.extend((key, _scalar(item)) for item in value if item is not None)
        else:
            fields.append((key, _scalar(value)))
    return fields


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Transport:
    """Wraps an ``httpx.Client`` and owns the retry, circuit and error rules.

    The client is injected rather than constructed, so tests can mock it and so
    base URL and headers are configured in exactly one place.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._writes_blocked = False

    @property
    def writes_blocked(self) -> bool:
        """True once a restricted-account response has tripped the breaker."""
        return self._writes_blocked

    # --- reads: retried -------------------------------------------------------

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._retrying("GET", path, params=params)

    # --- writes: never retried ------------------------------------------------

    def post_json(
        self,
        path: str,
        *,
        json: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._guard_write()
        return self._once("POST", path, json=json, params=params)

    def post_form(self, path: str, *, data: dict[str, Any]) -> dict[str, Any]:
        """POST as multipart/form-data, which these endpoints require.

        httpx only emits multipart when ``files`` is present, and passing plain
        ``data`` would send urlencoded instead. A ``(None, value)`` file tuple
        is the standard way to express a plain multipart field, and a list of
        tuples is what allows a key to repeat.
        """
        self._guard_write()
        parts = [(name, (None, value)) for name, value in encode_form(data)]
        return self._once("POST", path, files=parts)

    def patch_json(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        self._guard_write()
        return self._once("PATCH", path, json=json)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Delete is a mutation, so it is never retried -- but it is exempt from
        the circuit breaker. Withdrawing a pending invitation reduces exposure,
        and it is the first thing an operator needs after a restriction."""
        return self._once("DELETE", path, params=params)

    # --- internals ------------------------------------------------------------

    def _guard_write(self) -> None:
        if self._writes_blocked:
            raise CircuitOpen(
                type="local/circuit_open",
                title="Writes are blocked because the account was restricted",
                detail="Re-instantiate the client once the restriction is cleared.",
            )

    def _retrying(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._once(method, path, **kwargs)
            except (RateLimited, CircuitOpen):
                raise
            except UnipileError as exc:
                if exc.status not in RETRYABLE_STATUS:
                    raise
                last = exc
            except httpx.TransportError as exc:
                last = exc
            if attempt < self._max_attempts - 1:
                self._sleep(_backoff(attempt))
        assert last is not None
        raise last

    def _once(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        if response.is_success:
            return _body(response)
        self._raise(response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _raise(self, response: httpx.Response) -> None:
        try:
            raise_for_response(response.status_code, _body(response))
        except AccountRestricted:
            self._writes_blocked = True
            raise
        except RateLimited as exc:
            exc.retry_after = _retry_after(response)
            raise


def _backoff(attempt: int) -> float:
    base = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
    return base + random.uniform(0.0, 0.25)


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except ValueError:
        return {"title": response.text[:200]}
    return parsed if isinstance(parsed, dict) else {"items": parsed}


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
