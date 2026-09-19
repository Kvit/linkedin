"""Crawl recent LinkedIn activity of first-degree target contacts into `activity/{doc_id}`.

Each call checks contacts one at a time: never-checked first (most recently active
first), then the oldest `updated_at`. At most `UNIPILE_MAX_ACTIVITY_CHECKS_PER_DAY`
contacts per rolling 24 h. A check is four reads (posts, comments, reactions,
profile) plus one read per distinct post its newest comments and reactions were on,
each preceded by the client's human cadence (random 20-40 s gaps, a 2-5 min break
about every 10 calls).
A 429, a restriction, a 5xx or a profile throttle lockout ends the call.
"""

import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from google.cloud.firestore_v1.base_query import FieldFilter

from lib.contacts import TARGET_INDUSTRIES, activity_key, headline
from lib.unipile.errors import (
    BudgetExhausted,
    CircuitOpen,
    NotFound,
    PermissionDenied,
    RateLimited,
    ServerError,
    ThrottleLockout,
    UnprocessableError,
)

ACTIVITY_COLLECTION = "activity"
ANALYSIS_COLLECTION = "analysis"
EXTRACTED_COLLECTION = "extracted"

KINDS = ("posts", "comments", "reactions", "profile")
POSTS_PAGE = 20  # one request either way; the newest `activity_items_per_kind` in the window are kept
COMMENTS_PAGE = 20
TEXT_CHARS = 300
MAX_RECENCY_DAYS = 365

#: The `activity/{doc_id}` fields `get_activity_record` returns.
FIELDS = (
    "doc_id", "name", "industry", "pipeline_stage", "profile_url", "updated_at", "recency_days", "last_activity",
    "suggested_message", "suggested_message_updated_at", "suggested_message_sent_at", "errors", "posts", "comments",
    "reactions", "profile_changes", "unknown_before",
)
COUNTED = ("posts", "comments", "reactions", "profile_changes")

_SKIP = (NotFound, UnprocessableError)  # noted on the contact; the crawl goes on
_STOP = (RateLimited, PermissionDenied, CircuitOpen, ThrottleLockout, ServerError)  # ends the call

# Withheld profile sections, by compared field.
_SECTIONS = {"position": {"work_experience", "experience"}, "about": {"about", "summary"}}


def get_contact_activity(
    db, client, *, type: str | list[str] = "All", recency: str | int = 10, limit: int | None = None,
    industries: Iterable[str] | None = None, doc_ids: Iterable[str] | None = None, now: datetime | None = None,
) -> dict:
    """Check the next contacts in the crawl and return what they did in the last `recency` days.

    `type`: "All" or any of posts, comments, reactions, profile. `limit` narrows today's allowance.
    `doc_ids` re-checks exactly those audience contacts, in that order, instead of the crawl order.
    """
    kinds = _kinds(type)
    days = _days(recency)
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    industries = list(TARGET_INDUSTRIES if industries is None else industries)
    if not industries:
        raise ValueError("industries must not be empty")
    fixed_now = now is not None  # a caller's `now` stamps every contact (tests)
    now = now or _utcnow()
    started = time.monotonic()

    audience = _audience(db, client, industries)
    activity = db.collection(ACTIVITY_COLLECTION)
    stored = _stored(db, activity, audience)
    never = [c for c in audience if not stored[c["doc_id"]].get("updated_at")]
    never.sort(key=lambda c: activity_key(c["analysis"]), reverse=True)
    seen = sorted((c for c in audience if stored[c["doc_id"]].get("updated_at")),
                  key=lambda c: stored[c["doc_id"]]["updated_at"])
    order, missing = never + seen, []
    if doc_ids is not None:
        by_id = {c["doc_id"]: c for c in audience}
        wanted = list(dict.fromkeys(doc_ids))
        order = [by_id[d] for d in wanted if d in by_id]
        missing = [d for d in wanted if d not in by_id]

    since = now - timedelta(hours=24)
    used = int(activity.where(filter=FieldFilter("updated_at", ">=", since)).count().get()[0][0].value)  # 0.0 when none
    allowance = max(0, client.settings.max_activity_checks_per_day - used)
    take = allowance if limit is None else min(allowance, limit)

    cutoff = now - timedelta(days=days)
    if "profile" in kinds:
        client.budget.reconcile(profile=used)  # about one profile read per contact checked today
    profile_start = client.budget.used("profile")
    state = {"profile": "profile" in kinds, "profile_skipped": None}
    rows, checked, stopped = [], 0, None
    for contact in order[:take]:
        try:
            doc = _check(db, client, contact, kinds, cutoff, state)
        except _STOP as error:
            stopped = f"{error.__class__.__name__}: {error.title}"
            break
        doc.update(updated_at=now if fixed_now else _utcnow(), recency_days=days)
        previous = stored[contact["doc_id"]]
        doc["last_activity"] = _latest(doc.pop("_dates"), previous.get("last_activity"))
        if not previous or doc["last_activity"] != previous.get("last_activity"):
            doc["suggested_message"] = None  # created empty; a draft is stale once newer activity appears
        activity.document(contact["doc_id"]).set(doc, merge=True)  # kinds not checked keep their values
        checked += 1
        if doc.get("posts") or doc.get("comments") or doc.get("reactions") or doc.get("profile_changes"):
            rows.append({key: doc.get(key) for key in (
                "doc_id", "name", "industry", "pipeline_stage", "profile_url", "last_activity",
                "posts", "comments", "reactions", "profile_changes",
            )})

    rows.sort(key=lambda row: (row["last_activity"] is not None, row["last_activity"]), reverse=True)
    return {
        "audience": len(audience), "never_checked": len(never), "allowance": allowance, "checked": checked,
        "profile_reads": client.budget.used("profile") - profile_start, "profile_skipped": state["profile_skipped"],
        "stopped": stopped, "not_in_audience": missing, "seconds": round(time.monotonic() - started, 1),
        "contacts": rows,
    }


def activity_summary(
    db, *, freshness: int = 15, has_suggested_message: bool = False, limit: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Contacts with `last_activity` in the last `freshness` days, newest first, with a draft or needing one.

    Needing one: no draft, and none cleared or sent at or after `last_activity`."""
    if freshness < 1:
        raise ValueError("freshness must be at least 1 day")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    cutoff = (now or _utcnow()) - timedelta(days=freshness)
    # One range filter; the draft test runs here, as a second filter would need a composite index.
    query = db.collection(ACTIVITY_COLLECTION).where(filter=FieldFilter("last_activity", ">=", cutoff)).select(
        ["name", "last_activity", "updated_at", "suggested_message", "suggested_message_updated_at",
         "suggested_message_sent_at", *COUNTED]
    )
    rows = []
    for snapshot in query.stream():
        data = snapshot.to_dict() or {}
        draft = (data.get("suggested_message") or "").strip()
        if bool(draft) != has_suggested_message:
            continue
        changed = data.get("suggested_message_updated_at")
        if not draft and changed is not None and changed >= data["last_activity"]:
            continue  # cleared or sent after this activity
        row = {"doc_id": snapshot.id, "name": data.get("name"), "last_activity": data.get("last_activity"),
               "updated_at": data.get("updated_at"), **{kind: len(data.get(kind) or []) for kind in COUNTED},
               "suggested_message_updated_at": changed, "suggested_message_sent_at": data.get("suggested_message_sent_at")}
        if draft:
            row["suggested_message"] = draft
        rows.append(row)
    rows.sort(key=lambda row: row["last_activity"], reverse=True)
    return rows if limit is None else rows[:limit]


def get_activity_record(db, doc_id: str) -> dict | None:
    """The stored `activity/{doc_id}` fields (`FIELDS`), or `None` when the contact has none."""
    snapshot = db.collection(ACTIVITY_COLLECTION).document(doc_id).get()
    if not snapshot.exists:
        return None
    data = {**(snapshot.to_dict() or {}), "doc_id": doc_id}
    return {key: data[key] for key in FIELDS if key in data}


def set_suggested_message(db, doc_id: str, text: str, now: datetime | None = None) -> bool:
    """Store the draft (blank text clears it) and when it changed, on an existing record; `False` when there is none."""
    reference = db.collection(ACTIVITY_COLLECTION).document(doc_id)
    if not reference.get().exists:
        return False
    reference.update({"suggested_message": text.strip() or None, "suggested_message_updated_at": now or _utcnow()})
    return True


def mark_suggested_message_sent(db, doc_id: str, now: datetime | None = None) -> bool:
    """The draft went out: clear it and set `suggested_message_sent_at`; `False` when there is no record."""
    reference = db.collection(ACTIVITY_COLLECTION).document(doc_id)
    if not reference.get().exists:
        return False
    now = now or _utcnow()
    reference.update({"suggested_message": None, "suggested_message_updated_at": now,
                      "suggested_message_sent_at": now})
    return True


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _kinds(value: str | list[str]) -> list[str]:
    values = [value] if isinstance(value, str) else list(value)
    if len(values) == 1 and values[0].lower() == "all":
        return list(KINDS)
    if not values or any(v not in KINDS for v in values):
        raise ValueError(f"type must be 'All' or any of {', '.join(KINDS)}")
    return values


def _days(value: str | int) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*d?\s*", str(value))
    days = int(match.group(1)) if match else 0
    if not 1 <= days <= MAX_RECENCY_DAYS:
        raise ValueError(f"recency must be 1 to {MAX_RECENCY_DAYS} days, e.g. '10d'")
    return days


def _audience(db, client, industries: list[str]) -> list[dict]:
    """First-degree connections whose `analysis` industry is a target, minus `handling == exclude`."""
    relations = {r.public_identifier: r for r in client.users.iter_relations()}
    query = db.collection(ANALYSIS_COLLECTION).where(filter=FieldFilter("industry", "in", industries)).select(
        ["industry", "pipeline_stage", "handling", "last_reply_date", "last_sent_date"]
    )
    audience = []
    for snapshot in query.stream():
        relation = relations.get(snapshot.id)
        data = snapshot.to_dict() or {}
        if relation is None or str(data.get("handling") or "").strip().lower() == "exclude":
            continue
        name = f"{relation.first_name or ''} {relation.last_name or ''}".strip()
        audience.append({
            "doc_id": snapshot.id, "provider_id": relation.provider_id, "name": name,
            "profile_url": relation.public_profile_url or f"https://www.linkedin.com/in/{snapshot.id}",
            "industry": data.get("industry"), "pipeline_stage": data.get("pipeline_stage"), "analysis": data,
        })
    return audience


def _stored(db, activity, audience: list[dict]) -> dict[str, dict]:
    """`updated_at` and `last_activity` of each audience contact's `activity` document ({} if none)."""
    stored = {c["doc_id"]: {} for c in audience}
    if audience:
        refs = [activity.document(c["doc_id"]) for c in audience]
        for snapshot in db.get_all(refs, field_paths=["updated_at", "last_activity"]):
            if snapshot.exists:
                stored[snapshot.id] = snapshot.to_dict() or {}
    return stored


def _check(db, client, contact: dict, kinds: list[str], cutoff: datetime, state: dict) -> dict:
    """One contact's reads; the document fields to write, plus `_dates` of everything read."""
    pid = contact["provider_id"]
    items = client.settings.activity_items_per_kind
    doc = {key: contact[key] for key in ("doc_id", "name", "industry", "pipeline_stage", "profile_url")}
    doc.update(errors=[], _dates=[])
    context = []  # (row, post ref): comments and reactions that get the post they were on

    if "posts" in kinds:
        posts = _read(client, doc, "posts", lambda: client.users.iter_posts(pid, page_size=POSTS_PAGE, max_pages=1))
        if posts is not None:
            doc["_dates"] += [p.action_date for p in posts if p.action_date]
            recent = sorted((p for p in posts if p.action_date and p.action_date >= cutoff),
                            key=lambda p: p.action_date, reverse=True)[:items]
            doc["posts"] = [
                {"date": p.action_date, "text": _text(p.display_text), "share_url": _clean_url(p.share_url),
                 "author": _text(p.author.name if p.author else None), "reactions": p.reaction_counter,
                 "comments": p.comment_counter, "is_repost": p.is_repost}
                for p in recent
            ]
    if "comments" in kinds:
        comments = _read(client, doc, "comments",
                         lambda: client.users.iter_comments(pid, page_size=COMMENTS_PAGE, max_pages=1))
        if comments is not None:
            doc["_dates"] += [c.date for c in comments if c.date]
            recent = sorted((c for c in comments if c.date and c.date >= cutoff),
                            key=lambda c: c.date, reverse=True)[:items]
            doc["comments"] = [{"date": c.date, "text": _text(c.text), "post_id": c.post_id}
                               for c in recent]
            context += zip(doc["comments"], (c.post_ref for c in recent))
    if "reactions" in kinds:
        reactions = _read(client, doc, "reactions",
                          lambda: client.users.iter_reactions(pid, page_size=items, max_pages=1))
        if reactions is not None:
            doc["_dates"] += [r.date for r in reactions if r.date]
            recent = [r for r in reactions if r.date is None or r.date >= cutoff][:items]  # newest first
            doc["reactions"] = [{"date": r.date, "value": r.value, "post_id": r.post_id} for r in recent]
            context += zip(doc["reactions"], (r.post_id for r in recent))
    found = {}
    for row, ref in context:
        if ref not in found:
            found[ref] = _post(client, doc, ref)
        row["post"] = found[ref]
    if "profile" in kinds and state["profile"]:
        try:
            fresh = client.users.get_profile(pid)  # paced and charged by the client
        except BudgetExhausted:
            state.update(profile=False, profile_skipped="budget")
        except _SKIP as error:
            doc["errors"].append(f"profile: {error.__class__.__name__}: {error.title}")
        else:
            snapshot = db.collection(EXTRACTED_COLLECTION).document(contact["doc_id"]).get()
            doc["profile_changes"], doc["unknown_before"] = _compare(
                snapshot.to_dict() if snapshot.exists else {}, fresh
            )
    return doc


def _read(client, doc: dict, kind: str, fetch) -> list | None:
    """One paced list read; `None` (noted in `errors`) when it fails for this contact only."""
    client.budget.throttle()
    try:
        return list(fetch())
    except _SKIP as error:
        doc["errors"].append(f"{kind}: {error.__class__.__name__}: {error.title}")
        return None


def _post(client, doc: dict, ref: str | None) -> dict | None:
    """One paced post read, as `{author, text, url, date}`; `None` (noted in `errors`) if unreadable."""
    if not ref:
        return None
    client.budget.throttle()
    try:
        post = client.users.get_post(ref)
    except _SKIP as error:
        doc["errors"].append(f"post {ref}: {error.__class__.__name__}: {error.title}")
        return None
    return {"author": _text(post.author.name if post.author else None), "text": _text(post.display_text),
            "url": _clean_url(post.share_url), "date": post.parsed_datetime}


def _compare(stored: dict, fresh) -> tuple[list[dict], list[str]]:
    """Changes between the stored profile and a fresh one; fields the stored copy lacks go to `unknown`."""
    extra = stored.get("extra") or {}
    current = fresh.current_position
    pairs = {
        "headline": (headline(stored), fresh.headline),
        "position": (_position(stored.get("currentPosition")),
                     _position({"position": current.position, "company": current.company} if current else None)),
        "location": (extra.get("locationName"), fresh.location),
        "about": (extra.get("summary"), fresh.summary),
    }
    withheld = set(fresh.incomplete_sections)
    changes, unknown = [], []
    for field, (before, after) in pairs.items():
        if withheld & _SECTIONS.get(field, set()):
            continue
        before, after = _norm(before), _norm(after)
        if before is None:
            if after is not None:
                unknown.append(field)
        elif before != after:
            changes.append({"field": field, "before": before, "after": after})
    return changes, unknown


def _position(value) -> str | None:
    if not isinstance(value, dict):
        return None
    return " at ".join(part for part in (value.get("position"), value.get("company")) if part) or None


def _text(value: str | None) -> str:
    return (value or "")[:TEXT_CHARS]


def _clean_url(url: str | None) -> str | None:
    """Drop LinkedIn's share-tracking query (its `rcm` carries the viewer's own member id)."""
    return url.split("?")[0] if url else url


def _norm(value) -> str | None:
    """Whitespace-collapsed text, so line-ending differences are not changes."""
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def _latest(dates: list[datetime], previous: datetime | None) -> datetime | None:
    candidates = [d for d in [*dates, previous] if d is not None]
    return max(candidates) if candidates else None
