"""Cursor pagination, exposed as generators.

Every list endpoint on the Unipile API returns an opaque ``cursor`` alongside its
``items``. Callers should never handle that cursor: they iterate, and the
generator fetches more pages as needed.
"""

from collections.abc import Callable, Iterator
from typing import Any

Page = dict[str, Any]


def iter_cursor[T](
    fetch: Callable[[str | None], Page],
    *,
    parse: Callable[[Any], T] = lambda item: item,
) -> Iterator[T]:
    """Walk a cursor-paginated endpoint, yielding parsed items.

    ``fetch`` receives the cursor for the page to load (``None`` for the first)
    and returns the raw response body. Iteration stops when the response carries
    no cursor, returns an empty page, or repeats a cursor it has already served
    -- the last of which would otherwise loop forever against a misbehaving
    endpoint.
    """
    cursor: str | None = None
    seen: set[str] = set()

    while True:
        page = fetch(cursor)
        items = page.get("items") or []
        if not items:
            return

        for item in items:
            yield parse(item)

        cursor = page.get("cursor")
        if not cursor or cursor in seen:
            return
        seen.add(cursor)


def iter_account_scoped[T](
    get: Callable[..., Page],
    account_id: Callable[[], str],
    path: str,
    model: type[T],
    *,
    page_size: int = 100,
    **extra: Any,
) -> Iterator[T]:
    """Walk an account-scoped list endpoint, yielding parsed models.

    Every such endpoint takes ``account_id`` and ``limit`` and returns a cursor,
    so users and messaging shared an identical private implementation. Optional
    filters passed as ``None`` are omitted rather than sent empty.
    """

    def fetch(cursor: str | None) -> Page:
        params: dict[str, Any] = {"account_id": account_id(), "limit": page_size}
        params.update({key: value for key, value in extra.items() if value is not None})
        if cursor:
            params["cursor"] = cursor
        return get(path, params=params)

    return iter_cursor(fetch, parse=model.model_validate)
