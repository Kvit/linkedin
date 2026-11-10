"""Tracked links to the files of one bucket folder: the listing, the link
records (`document_links`) and the requests on them (`document_requests`).

Shared by the outreach service (the public `/d/{token}` route, the MCP
tools) and the contacts webapp (the Documents tab). `db`, `storage`,
`settings` and `now` are always passed in, and every query filters on one
field. The bucket is the only source of the list: nothing is cached.

A link dict is the stored record plus `id`, `name` (the file name, without
the folder) and `url`. `google.cloud` is imported inside the functions, as
in `queue.py`.
"""

import base64
import re
import secrets
import string
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from string import Template

LINKS = "document_links"
REQUESTS = "document_requests"

PERMANENT = "permanent"
CUSTOM = "custom"

#: What a request is: a scanner or a link preview, or a person.
BOT = "bot"
PERSON = "person"

_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,200}")

#: The longest main link id, of the 200 a link id may have: the rest is room
#: for a `-9` and for a sublink's token.
_MAIN_ID_MAX = 180

#: A sublink's own part of its id: this many lowercase letters and digits,
#: put before the extension of the main link's id. A format, not a limit.
TOKEN_LENGTH = 5
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits

#: The page a recipient sees for a link that does not work: its title, the
#: headline's plain and orange parts, and the sentence under it.
_NOT_FOUND = {
    "invalid": (
        "This link is not valid", "This link is", "not valid",
        "Check that the address is complete, or ask the person who sent you the link for a new one.",
    ),
    "gone": (
        "This document is no longer available", "This document is", "no longer available",
        "It was replaced or removed. Ask the person who sent you the link for the current version.",
    ),
}


def list_documents(storage, settings) -> list[dict]:
    """The files directly in the folder, newest first."""
    prefix = settings.documents_prefix
    rows = []
    for blob in storage.list_blobs(settings.documents_bucket, prefix=prefix, delimiter="/"):
        name = blob.name[len(prefix):]
        if name:  # the folder itself is an empty object
            rows.append({
                "name": name, "object": blob.name, "type": PurePosixPath(name).suffix[1:].upper(),
                "size": blob.size, "updated_at": blob.updated,
            })
    return sorted(rows, key=lambda row: row["updated_at"], reverse=True)


def read_document(storage, settings, object_name: str, *, body: bool = True) -> tuple[bytes, str, int] | None:
    """The object's content, content type and size, or `None` when it is gone.
    Without `body` the content is empty and the size is still the object's."""
    from google.api_core.exceptions import NotFound

    bucket = storage.bucket(settings.documents_bucket)
    if not body:
        blob = bucket.get_blob(object_name)
        if blob is None:
            return None
        content, size = b"", blob.size
    else:
        blob = bucket.blob(object_name)
        try:
            content = blob.download_as_bytes()  # one call: it sets content_type too (measured: 145 ms less)
        except NotFound:
            return None
        size = len(content)
    return content, blob.content_type or "application/octet-stream", size


def slug(name: str) -> str:
    """A file name as a permanent link id: `Recovr_One_Pager.pdf` -> `recovr-one-pager.pdf`."""
    return re.sub(r"[^a-z0-9.-]", "-", name.lower())


def usable_token(token: str) -> bool:
    """Whether `token` can be a link id (and so a Firestore document id)."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or token in (".", ".."):
        return False
    return not (token.startswith("__") and token.endswith("__"))


def link_url(settings, link_id: str) -> str:
    return settings.document_link_base.rstrip("/") + "/" + link_id


def _object(settings, name: str) -> str:
    return settings.documents_prefix + name


def _link(settings, snapshot) -> dict:
    data = snapshot.to_dict() or {}
    name = str(data.get("document") or "").removeprefix(settings.documents_prefix)
    return {"id": snapshot.id, "name": name, "url": link_url(settings, snapshot.id), **data}


def _record(document: str, kind: str, *, doc_id: str | None, label: str | None, created_by: str, now: datetime) -> dict:
    return {
        "document": document, "kind": kind, "doc_id": doc_id, "label": label,
        "created_by": created_by, "created_at": now,
        "opens": 0, "bot_requests": 0, "first_open_at": None, "last_open_at": None,
    }


def _slugs(name: str) -> Iterator[str]:
    """The ids to try for a file's permanent link: its slug, cut to fit, then
    `-2` to `-9` before the extension."""
    stem, dot, extension = slug(name).rpartition(".")
    if not dot or len(extension) > 20:  # nothing to keep as an extension
        stem, dot, extension = slug(name), "", ""
    stem = stem[: _MAIN_ID_MAX - len(dot + extension)]
    yield stem + dot + extension
    for number in range(2, 10):
        yield f"{stem}-{number}{dot}{extension}"


def ensure_permanent_links(db, settings, names: list[str], now: datetime) -> dict[str, str]:
    """`{file name: its permanent link's id}`, creating the links that are missing."""
    from google.api_core import exceptions as api_exceptions
    from google.cloud.firestore_v1.base_query import FieldFilter

    collection = db.collection(LINKS)
    taken = {
        snapshot.id: (snapshot.to_dict() or {}).get("document")
        for snapshot in collection.where(filter=FieldFilter("kind", "==", PERMANENT)).stream()
    }
    by_document = {document: link_id for link_id, document in taken.items()}
    found = {}
    for name in sorted(names):
        document = _object(settings, name)
        if document in by_document:
            found[name] = by_document[document]
            continue
        for candidate in _slugs(name):
            if candidate in taken or not usable_token(candidate):
                continue
            reference = collection.document(candidate)
            try:
                reference.create(_record(document, PERMANENT, doc_id=None, label=None, created_by="auto", now=now))
            except api_exceptions.Conflict:  # created since the read above
                if (reference.get().to_dict() or {}).get("document") != document:
                    continue
            taken[candidate] = document
            found[name] = candidate
            break
    return found


def _token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def create_link(
    db, settings, name: str, *, doc_id: str | None = None, label: str | None = None, created_by: str, now: datetime,
) -> dict:
    """Store a new custom link (a sublink) to the file `name`, for a contact or a label.

    Its id is the file's main link id (made here if missing) with a token
    before the extension: `recovr-one-pager-k3x9q.pdf`. A token already
    taken is drawn again.
    """
    from google.api_core import exceptions as api_exceptions

    record = _record(_object(settings, name), CUSTOM, doc_id=doc_id, label=label, created_by=created_by, now=now)
    main = ensure_permanent_links(db, settings, [name], now).get(name) or next(_slugs(name))
    stem, dot, extension = main.rpartition(".")
    for _attempt in range(10):
        token = _token()
        link_id = f"{stem}-{token}.{extension}" if dot else f"{extension}-{token}"
        if not usable_token(link_id):  # a file name too long for an id
            link_id = token
        try:
            db.collection(LINKS).document(link_id).create(record)
        except api_exceptions.Conflict:
            continue
        return {"id": link_id, "name": name, "url": link_url(settings, link_id), **record}
    raise RuntimeError("no free link id in 10 draws")


def get_link(db, settings, link_id: str) -> dict | None:
    snapshot = db.collection(LINKS).document(link_id).get()
    return _link(settings, snapshot) if snapshot.exists else None


def links(db, settings, *, name: str | None = None, doc_id: str | None = None) -> list[dict]:
    """The links, newest first: all of them, a file's, or a contact's."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(LINKS)
    if doc_id is not None:
        query = query.where(filter=FieldFilter("doc_id", "==", doc_id))
    elif name is not None:
        query = query.where(filter=FieldFilter("document", "==", _object(settings, name)))
    found = [_link(settings, snapshot) for snapshot in query.stream()]
    if doc_id is not None and name is not None:
        found = [link for link in found if link["name"] == name]
    return sorted(found, key=lambda link: link["created_at"], reverse=True)


def totals(found: list[dict]) -> dict[str, dict]:
    """Per file name: the counted opens of its links, how many custom links, the newest open."""
    result: dict[str, dict] = {}
    for link in found:
        entry = result.setdefault(link["name"], {"opens": 0, "links": 0, "last_open_at": None})
        entry["opens"] += link.get("opens") or 0
        entry["links"] += link.get("kind") == CUSTOM
        last = link.get("last_open_at")
        if last is not None and (entry["last_open_at"] is None or last > entry["last_open_at"]):
            entry["last_open_at"] = last
    return result


def classify_request(method: str, user_agent: str, accept_language: str | None, bot_agents: list[str]) -> str:
    """`bot` for HEAD, a listed User-Agent or no Accept-Language; else `person`."""
    if method.upper() == "HEAD" or not (accept_language or "").strip():
        return BOT
    agent = (user_agent or "").lower()
    return BOT if any(entry.lower() in agent for entry in bot_agents if entry) else PERSON


def record_request(
    db, settings, link: dict, *, method: str, user_agent: str, ip: str, accept_language: str | None, now: datetime,
) -> dict:
    """Store one request on `link` and update the link's counters; returns the stored row.

    A person's request is counted unless it is a reload: the same address
    as the link's last counted open, `document_reopen_seconds` old or newer.
    The counters come from `link` as the caller read it: no transaction.
    """
    kind = classify_request(method, user_agent, accept_language, settings.document_bot_agents)
    ip = (ip or "")[:64]  # the headers come from anyone: cut what is stored
    last = link.get("last_open_at")
    # shortcut: only the last reader is remembered, so A, B, A inside the window counts A twice;
    # keep the window's readers on the link if that shows up.
    reload = (
        last is not None and ip == link.get("last_open_ip")
        and (now - last).total_seconds() <= settings.document_reopen_seconds
    )
    counted = kind == PERSON and not reload
    row = {
        "link": link["id"], "document": link["document"], "at": now, "method": method, "kind": kind,
        "counted": counted, "user_agent": (user_agent or "")[:500], "ip": ip,
        "accept_language": accept_language[:100] if accept_language else None,
    }
    db.collection(REQUESTS).document().set(row)
    if counted:
        update = {
            "opens": (link.get("opens") or 0) + 1, "last_open_at": now, "last_open_ip": ip,
            "first_open_at": link.get("first_open_at") or now,
        }
    elif kind == BOT:
        update = {"bot_requests": (link.get("bot_requests") or 0) + 1}
    else:
        return row
    db.collection(LINKS).document(link["id"]).update(update)
    return row


def requests_for(db, settings, name: str, per_link: int = 20) -> dict[str, list[dict]]:
    """`{link id: its newest requests, newest first}` for the file `name`."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    # shortcut: reads every request of the file; past ~5,000 rows add an index on (document, at) and a limit.
    query = db.collection(REQUESTS).where(filter=FieldFilter("document", "==", _object(settings, name)))
    rows = sorted((snapshot.to_dict() or {} for snapshot in query.stream()), key=lambda row: row["at"], reverse=True)
    found: dict[str, list[dict]] = {}
    for row in rows:
        kept = found.setdefault(row["link"], [])
        if len(kept) < per_link:
            kept.append(row)
    return found


def not_found_page(kind: str, year: int) -> str:
    """The 404 page, `invalid` or `gone`: `link_page.html` with the logo embedded, so it loads nothing from the bucket."""
    folder = Path(__file__).parent
    title, headline, accent, message = _NOT_FOUND[kind]
    logo = "data:image/jpeg;base64," + base64.b64encode((folder / "link_logo.jpg").read_bytes()).decode("ascii")
    return Template((folder / "link_page.html").read_text(encoding="utf-8")).substitute(
        title=title, headline=headline, accent=accent, message=message, year=year, logo=logo,
    )
