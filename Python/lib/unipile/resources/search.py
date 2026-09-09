"""LinkedIn search and search-parameter resolution."""

from collections.abc import Callable, Iterator
from typing import Any, Literal

from ..models import SearchParameter, SearchResult
from ..pagination import iter_cursor
from ..transport import Transport

SearchApi = Literal["classic", "sales_navigator", "recruiter"]


class SearchResource:
    """Everything under `/linkedin/search`."""

    def __init__(self, transport: Transport, account_id: Callable[[], str]) -> None:
        self._transport = transport
        self._account_id = account_id

    def search(
        self,
        config: dict[str, Any],
        *,
        api: SearchApi = "classic",
        page_size: int = 100,
    ) -> Iterator[SearchResult]:
        """Run a search and page through every result.

        `api` defaults to classic. Sales Navigator and Recruiter require their
        own subscriptions and raise `FeatureNotSubscribed` without one.
        """

        def fetch(cursor: str | None) -> dict[str, Any]:
            # account_id, limit and cursor belong in the query string. Putting
            # account_id in the body is rejected with 400 invalid_parameters,
            # "path": "/account_id", "Required property" -- verified live.
            params: dict[str, Any] = {"account_id": self._account_id(), "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            return self._transport.post_json(
                "/api/v1/linkedin/search", json={"api": api, **config}, params=params
            )

        return iter_cursor(fetch, parse=SearchResult.model_validate)

    def iter_search_parameters(
        self, parameter_type: str, keywords: str
    ) -> Iterator[SearchParameter]:
        """Resolve human names to the ids search expects (locations, industries).

        This endpoint paginates with a ``paging.page_count`` rather than a
        cursor, and returns everything it has in one response.
        """
        body = self._transport.get(
            "/api/v1/linkedin/search/parameters",
            params={
                "account_id": self._account_id(),
                "type": parameter_type,
                "keywords": keywords,
            },
        )
        return (SearchParameter.model_validate(item) for item in body.get("items", []))
