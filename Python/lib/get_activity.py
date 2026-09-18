"""Crawl recent LinkedIn activity of first-degree target contacts into `activity/{doc_id}`.

Each call checks contacts one at a time: never-checked first (most recently active
first), then the oldest `updated_at`. At most `UNIPILE_MAX_ACTIVITY_CHECKS_PER_DAY`
contacts per rolling 24 h; each check is up to four reads, each preceded by the
client's human cadence (random 20-40 s gaps, a 2-5 min break about every 10 calls).
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
POSTS_PAGE = 20
COMMENTS_PAGE = 20
REACTIONS_SHOWN = 5  # reactions carry no date, so only the newest few are kept
TEXT_CHARS = 300
MAX_RECENCY_DAYS = 365

_SKIP = (NotFound, UnprocessableError)  # noted on the contact; the crawl goes on
_STOP = (RateLimited, PermissionDenied, CircuitOpen, ThrottleLockout, ServerError)  # ends the call

# Withheld profile sections, by compared field.
_SECTIONS = {"position": {"work_experience", "experience"}, "about": {"about", "summary"}}


def get_contact_activity(
    db, client, *, type: str | list[str] = "All", recency: str | int = 10, limit: int | None = None,
    industries: Iterable[str] | None = None, now: datetime | None = None,
) -> dict:
    """Check the next contacts in the crawl and return what they did in the last `recency` days.

    `type`: "All" or any of posts, comments, reactions, profile. `limit` narrows today's allowance.
    """
    kinds = _kinds(type)
    days = _days(recency)
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    industries = list(TARGET_INDUSTRIES if industries is None else industries)
    if not industries:
        raise ValueError("industries must not be empty")
    now = now or datetime.now(UTC)
    started = time.monotonic()

    audience = _audience(db, client, industries)
    activity = db.collection(ACTIVITY_COLLECTION)
    stored = _stored(db, activity, audience)
    never = [c for c in audience if not stored[c["doc_id"]].get("updated_at")]
    never.sort(key=lambda c: activity_key(c["analysis"]), reverse=True)
    seen = sorted((c for c in audience if stored[c["doc_id"]].get("updated_at")),
                  key=lambda c: stored[c["doc_id"]]["updated_at"])

    since = now - timedelta(hours=24)
    used = activity.where(filter=FieldFilter("updated_at", ">=", since)).count().get()[0][0].value
    allowance = max(0, client.settings.max_activity_checks_per_day - used)
    take = allowance if limit is None else min(allowance, limit)

    cutoff = now - timedelta(days=days)
    profile_start = client.budget.used("profile")
    state = {"profile": "profile" in kinds, "profile_skipped": None}
    rows, checked, stopped = [], 0, None
    for contact in (never + seen)[:take]:
        try:
            doc = _check(db, client, contact, kinds, cutoff, state)
        except _STOP as error:
            stopped = f"{error.__class__.__name__}: {error.title}"
            break
        doc.update(updated_at=now, recency_days=days)
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
        "stopped": stopped, "seconds": round(time.monotonic() - started, 1), "contacts": rows,
    }


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
    doc = {key: contact[key] for key in ("doc_id", "name", "industry", "pipeline_stage", "profile_url")}
    doc.update(errors=[], _dates=[])

    if "posts" in kinds:
        posts = _read(client, doc, "posts", lambda: client.users.iter_posts(pid, page_size=POSTS_PAGE, max_pages=1))
        if posts is not None:
            doc["_dates"] += [p.parsed_datetime for p in posts if p.parsed_datetime]
            doc["posts"] = [
                {"date": p.parsed_datetime, "text": (p.text or "")[:TEXT_CHARS], "share_url": p.share_url,
                 "reactions": p.reaction_counter, "comments": p.comment_counter, "is_repost": p.is_repost}
                for p in posts if p.parsed_datetime and p.parsed_datetime >= cutoff
            ]
    if "comments" in kinds:
        comments = _read(client, doc, "comments",
                         lambda: client.users.iter_comments(pid, page_size=COMMENTS_PAGE, max_pages=1))
        if comments is not None:
            doc["_dates"] += [c.date for c in comments if c.date]
            doc["comments"] = [
                {"date": c.date, "text": (c.text or "")[:TEXT_CHARS], "post_id": c.post_id}
                for c in comments if c.date and c.date >= cutoff
            ]
    if "reactions" in kinds:
        reactions = _read(client, doc, "reactions",
                          lambda: client.users.iter_reactions(pid, page_size=REACTIONS_SHOWN, max_pages=1))
        if reactions is not None:
            doc["reactions"] = [{"value": r.value, "post_id": r.post_id} for r in reactions[:REACTIONS_SHOWN]]
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


def _norm(value) -> str | None:
    """Whitespace-collapsed text, so line-ending differences are not changes."""
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def _latest(dates: list[datetime], previous: datetime | None) -> datetime | None:
    candidates = [d for d in [*dates, previous] if d is not None]
    return max(candidates) if candidates else None
