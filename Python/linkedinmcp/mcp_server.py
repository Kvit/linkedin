"""The MCP server the Claude agent connects to.

One tool, `get_status`, and that is deliberate: this is the walking skeleton,
and its whole job is to prove the transport, the authentication and the
database reach of the deployed service before any LinkedIn logic exists. Tools
that queue, send or list belong to later tasks.

`settings` and `clients` are imported as *modules* and called at request time
(`cfg.get_settings()`, `clients.firestore_client()`), never bound as names at
import. Two reasons, and both bite:

- binding `get_settings` or `firestore_client` locally puts them out of reach
  of `monkeypatch.setattr(clients, ...)`, and the test then silently talks to
  the real database;
- binding a settings object or a client *instance* at import time would open a
  connection while the module loads, which `app.py` does at startup and every
  test does at collection.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from lib.unipile.config import UnipileSettings

from linkedinmcp import clients, settings as cfg

mcp = FastMCP("linkedin-outreach")


def _firestore_health() -> str:
    """`"ok"`, or the class name of whatever went wrong.

    The class name, never the message: `google.api_core` errors quote the
    project, the database and the caller's service account in theirs, and this
    string is handed to an agent that may repeat it to the user.

    The read is the cheapest one Firestore offers. `.select([])` returns
    documents with no fields at all; without it, `.limit(1).get()` would pull
    a whole `analysis` document, and the largest of those is 35 KB -- absurd
    for a liveness probe that runs on every status call.

    Read-only, and it must stay that way: `analysis` holds the only copy of
    thousands of contact names and email addresses.

    `except Exception` is broad on purpose, and covers the *construction* as
    well as the query -- on Cloud Run, absent Application Default Credentials
    raise at `firestore.Client(...)`, before any query exists. Letting either
    escape would turn a credentials problem into an agent that looks broken.
    """
    try:
        db = clients.firestore_client()
        db.collection("analysis").select([]).limit(1).get()
    except Exception as exc:
        return type(exc).__name__
    return "ok"


def _unipile_limits() -> tuple[dict[str, int], str]:
    """Unipile's own per-day ceilings, and `"ok"` or why they could not be read.

    Rate limits live in the Unipile configuration rather than this service's:
    `SendBudget` enforces those exact numbers on the call itself, so a
    separately-named copy here would let the agent be told one ceiling while a
    different one was applied, and the first sign of the disagreement would be a
    restricted LinkedIn account.

    Missing Unipile configuration is reported, never raised -- the same contract
    as the Firestore probe, and for the same reason. A service that can answer
    "my LinkedIn credentials are missing" is far more use to the agent than one
    whose status call explodes.
    """
    try:
        unipile = UnipileSettings.from_env()
    except Exception as error:  # noqa: BLE001 - report anything, never raise
        return {}, type(error).__name__
    return {
        "messages_per_day": unipile.max_messages_per_day,
        "profile_fetches_per_day": unipile.max_profile_fetches_per_day,
    }, "ok"


@mcp.tool
def get_status() -> dict[str, Any]:
    """Report whether this service is healthy and what limits it will enforce.

    Call it at the start of a session, and again whenever another tool fails
    in a way that might be this service rather than LinkedIn.

    Returns the service name; the current time and IANA timezone it works in
    (every date it reports is in that zone); `caps`, the per-day ceilings on
    messages, profile fetches and intros it will not exceed, and the limits on
    how often one contact may be touched; `require_approval`, whether a human
    must approve each queued message before it is sent; and
    `firestore`, which is `"ok"` when the database answered and otherwise the
    class name of the error it raised -- `PermissionDenied` or
    `DefaultCredentialsError`, say, meaning the service is running but cannot
    reach its data, and nothing that reads or writes contacts will work until
    that is fixed. `unipile` reports the same for the LinkedIn credentials; when
    it is not `"ok"` the two rate-limit entries are absent from `caps`, because
    the numbers could not be read rather than being unlimited.
    """
    # Sync `def` on purpose: FastMCP runs sync tools in a worker thread, which
    # is where the blocking Firestore call belongs.
    settings = cfg.get_settings()
    limits, unipile_health = _unipile_limits()
    caps: dict[str, int] = {
        "intro_daily_cap": settings.intro_daily_cap,
        "max_touches": settings.max_touches,
        "min_days_between_touches": settings.min_days_between_touches,
    }
    caps.update(limits)
    return {
        "service": "linkedin-outreach",
        "time": datetime.now(ZoneInfo(settings.tz)).isoformat(),
        "timezone": settings.tz,
        # Small on purpose. The Claude platform offloads tool output over
        # 100k characters into a sandbox file the agent then has to open
        # before it can act on it.
        "caps": caps,
        "require_approval": settings.require_approval,
        "firestore": _firestore_health(),
        "unipile": unipile_health,
    }
