"""The Jinja2 environment every screen renders through, and the context
every screen shares. Kept out of `app.py` so route modules (`contacts.py`)
can import it without importing the factory."""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from webapp import projection

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def local(value: datetime | None, tz: str) -> str:
    """A datetime shown in the service's time zone, or an empty string."""
    return value.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M") if value is not None else ""


templates.env.filters["local"] = local


def qs(params: dict, **changes) -> str:
    """A query string of `params` with `changes` applied, empty values left
    out and a list repeated as one parameter per value: how a sort heading
    or a pager link keeps the page's search and filters. The template
    escapes it."""
    merged = params | changes
    return urlencode({name: value for name, value in merged.items() if value not in (None, "", [])}, doseq=True)


templates.env.globals["qs"] = qs


def page_context(request: Request, **extra) -> dict:
    """What `base.html` needs on every screen, from `app.state`."""
    state = request.app.state
    frame = state.contacts.frame
    return {
        "user": request.scope.get("state", {}).get("user"),
        "built_at": local(state.contacts.built_at, state.outreach.tz),
        "tz": state.outreach.tz,
        "needs_answer": frame.filter(projection.needs_my_answer()).height if frame is not None else 0,
        "stars": frame.filter(projection.starred()).height if frame is not None else 0,
        **extra,
    }


def back_to(request: Request) -> str:
    """The screen a form was posted from, as a path on this site: the
    `Referer`'s path and query, never its origin, so a redirect can only
    land here. `/` when there is none."""
    referer = urlsplit(request.headers.get("referer") or "")
    if not referer.path.startswith("/"):
        return "/"
    return referer.path + (f"?{referer.query}" if referer.query else "")
