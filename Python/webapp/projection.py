"""One in-memory frame of every contact, built from Firestore on demand.

Built from `analysis` (the contact universe; one row per document) joined
with `extracted` (names and headlines), with `.select()` on exactly the
fields below: never `summary` (up to 35 KB each), never `email*` or
`phone*`. A headline is `occupation` on a profile fetched through Unipile
(`lib.unipile.compat.to_lh_document`), else `miniProfile.headline` on a
LinkedIn Helper document -- 28,336 of 28,559 `extracted` documents have
one of the two, only 1,156 the first (2026-09-16). The whole `miniProfile`
map is selected, not just its headline: 0.8 s more per build, and it works
with `tests/linkedinmcp/fake_firestore.py`, which selects top-level fields
only. About 57,000 document reads and 10 to 15 seconds a build (measured
2026-09-16), so it is built at startup and rebuilt only when asked (the
Refresh button, and later after a job the webapp started).

The build is synchronous, inside the startup or the request that asks
for it: Cloud Run allocates CPU only while a request or the startup is in
progress, so a background thread would crawl. Requests arriving during a
rebuild read the previous frame.

Every column is typed in `SCHEMA`, so a frame built from a handful of test
documents has the same dtypes as one built from 28,675. `analysis` is also
written by notebooks, so a value of the wrong type -- a string date, a NaN
name -- is dropped to null and counted in a warning rather than failing
the build. Firestore returns `DatetimeWithNanoseconds`, a `datetime`
subclass polars stores to the microsecond.
"""

import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

import polars as pl

logger = logging.getLogger(__name__)

ANALYSIS_COLLECTION = "analysis"
EXTRACTED_COLLECTION = "extracted"

ANALYSIS_FIELDS = [
    "firstName", "lastName", "industry", "function", "seniority",
    "pipeline_stage", "pipeline_reason", "pipeline_classified_at",
    "sent_total", "replied_total", "last_sent_date", "last_reply_date",
    "intro_sent_at", "handling", "hand_set", "profileUrl",
]
EXTRACTED_SELECT = ["fullName", "occupation", "miniProfile"]
EXTRACTED_FIELDS = ["fullName", "headline"]

_DATETIME = pl.Datetime("us", "UTC")
_TEXT_LIST = pl.List(pl.String)
SCHEMA: dict[str, pl.DataType] = {
    "doc_id": pl.String,
    "firstName": pl.String, "lastName": pl.String, "fullName": pl.String, "headline": pl.String,
    "industry": pl.String, "function": pl.String, "seniority": pl.String,
    "pipeline_stage": pl.String, "pipeline_reason": pl.String, "pipeline_classified_at": _DATETIME,
    "sent_total": pl.Int64, "replied_total": pl.Int64,
    "last_sent_date": _DATETIME, "last_reply_date": _DATETIME, "intro_sent_at": _DATETIME,
    "handling": pl.String, "hand_set": _TEXT_LIST, "profileUrl": pl.String,
}

#: Columns a page may sort on. Anything else falls back to `activity_at`.
SORTABLE = (
    "name", "headline", "industry", "function", "seniority", "pipeline_stage", "handling",
    "sent_total", "replied_total", "last_sent_date", "last_reply_date", "activity_at",
)

#: What an empty or missing value is called in counts and filters, as
#: `linkedinmcp.contacts.NONE` names it for `contact_report`.
NONE = "none"


def _typed(field: str, value):
    """`value` when it fits `SCHEMA[field]`, else `None`. A float holding a
    whole number counts as an integer (pandas writes `2.0`)."""
    kind = SCHEMA[field]
    if value is None:
        return None
    if kind == pl.String:
        return value if isinstance(value, str) else None
    if kind == pl.Int64:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return int(value) if isinstance(value, float) and value.is_integer() else None
    if kind == _DATETIME:
        return value if isinstance(value, datetime) and value.tzinfo is not None else None
    if kind == _TEXT_LIST:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else None
    return None


def _fields(data: dict, fields: list[str], dropped: Counter) -> dict:
    row = {}
    for field in fields:
        value = data.get(field)
        row[field] = _typed(field, value)
        if value is not None and row[field] is None:
            dropped[field] += 1
    return row


def load_frame(db) -> pl.DataFrame:
    """Stream the two collections and build the frame."""
    dropped: Counter = Counter()
    rows: dict[str, dict] = {}
    for snapshot in db.collection(ANALYSIS_COLLECTION).select(ANALYSIS_FIELDS).stream():
        rows[snapshot.id] = {"doc_id": snapshot.id, **_fields(snapshot.to_dict() or {}, ANALYSIS_FIELDS, dropped)}
    empty = dict.fromkeys(EXTRACTED_FIELDS)
    for row in rows.values():
        row.update(empty)
    for snapshot in db.collection(EXTRACTED_COLLECTION).select(EXTRACTED_SELECT).stream():
        row = rows.get(snapshot.id)
        if row is not None:
            data = snapshot.to_dict() or {}
            mini = data.get("miniProfile")
            headline = data.get("occupation") or (mini.get("headline") if isinstance(mini, dict) else None)
            row.update(_fields({"fullName": data.get("fullName"), "headline": headline}, EXTRACTED_FIELDS, dropped))
    if dropped:
        logger.warning("dropped values of the wrong type, by field: %s", dict(dropped))
    return _derive(pl.from_dicts(list(rows.values()), schema=SCHEMA))


def _derive(frame: pl.DataFrame) -> pl.DataFrame:
    """`name` (`firstName` + `lastName`, else `fullName`, the rule of
    `linkedinmcp.contacts._name`) and `activity_at` (the later of the two
    dates, the sort `contacts._activity_key` uses). `concat_str` yields null
    on any null input, so both parts are filled."""
    joined = pl.concat_str(
        [pl.col("firstName").fill_null("").str.strip_chars(), pl.col("lastName").fill_null("").str.strip_chars()],
        separator=" ",
    ).str.strip_chars()
    return frame.with_columns(
        name=pl.when(joined != "").then(joined).otherwise(pl.col("fullName")),
        activity_at=pl.max_horizontal("last_reply_date", "last_sent_date"),
    )


def contact_row(frame: pl.DataFrame | None, doc_id: str) -> dict | None:
    """One contact's frame row as a dict, or `None` when it is not there."""
    if frame is None:
        return None
    found = frame.filter(pl.col("doc_id") == doc_id)
    return found.row(0, named=True) if found.height else None


def query(
    frame: pl.DataFrame, *, q: str = "", sort: str = "activity_at", descending: bool = True,
    page: int = 1, per_page: int = 100,
) -> tuple[pl.DataFrame, int]:
    """One page of `frame` after the text search, and the total matched.
    `literal=True`: the needle is text, never a regex."""
    found = frame
    needle = q.strip().lower()
    if needle:
        found = found.filter(
            pl.col("name").fill_null("").str.to_lowercase().str.contains(needle, literal=True)
            | pl.col("headline").fill_null("").str.to_lowercase().str.contains(needle, literal=True)
        )
    column = sort if sort in SORTABLE else "activity_at"
    found = found.sort([column, "doc_id"], descending=[descending, False], nulls_last=True)
    start = max(page - 1, 0) * per_page
    return found.slice(start, per_page), found.height


def normalized(column: str) -> pl.Expr:
    """`column` with an empty value read as `NONE`. `handling` is a
    hand-maintained field that collects stray capitals and padding, so it
    is trimmed and lowercased first, as `contact_report` does."""
    if column == "handling":
        return pl.col(column).fill_null("").str.strip_chars().str.to_lowercase().replace("", NONE)
    return pl.col(column).fill_null(NONE)


def counts(frame: pl.DataFrame, column: str) -> list[tuple[str, int]]:
    """Each value of `column` and how many contacts hold it, most frequent
    first, ties in ASCII order."""
    tally = (
        frame.select(normalized(column).alias("value"))
        .group_by("value")
        .len()
        .sort(["len", "value"], descending=[True, False])
    )
    return list(zip(tally["value"].to_list(), tally["len"].to_list(), strict=True))


class Contacts:
    """Holds the frame and its build time. `db_factory` is called on each
    build (`clients.firestore_client` in the app, a `FakeFirestore` in tests)."""

    def __init__(self, db_factory: Callable[[], object]) -> None:
        self._db_factory = db_factory
        self.frame: pl.DataFrame | None = None
        self.built_at: datetime | None = None

    def rebuild(self) -> None:
        """Build a new frame and swap it in; until then readers see the old one."""
        frame = load_frame(self._db_factory())
        self.frame, self.built_at = frame, datetime.now(UTC)
