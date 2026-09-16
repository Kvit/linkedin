"""One in-memory frame of every contact, built from Firestore on demand.

Built from `analysis` (the contact universe; one row per document) joined
with `extracted` (names, headlines, LinkedIn Helper's connection dates),
`fetch_queue` (the service's connection dates) and `messages` (what each
contact wrote to us), with `.select()` on exactly the fields below: never
`summary` (up to 35 KB each), never `email*` or `phone*`.

A headline is `occupation` on a profile fetched through Unipile
(`lib.unipile.compat.to_lh_document`), else `miniProfile.headline` on a
LinkedIn Helper document -- 28,336 of 28,559 `extracted` documents have
one of the two, only 1,156 the first (2026-09-16). The whole `miniProfile`
and `connect` maps are selected, not the fields inside them: that works
with `tests/linkedinmcp/fake_firestore.py`, which selects top-level fields
only.

`connected_at` is `fetch_queue.connected_at` -- what the service's
`contact_report` calls `date_connected`, set for the connections
`get_contacts` queued -- else LinkedIn Helper's `connect.connectedAt`,
milliseconds since the epoch. On 2026-09-16 they covered 648 and 2,158
contacts, none in both: 2,806 of 28,675, so most contacts have none.

`inbound_total` counts the readable messages a contact sent us, by
`pipeline.build_transcripts`' rule, the transcript the Contact screen
shows. It is not `replied_total`, which counts only answers in a
conversation we opened: 159 contacts had a readable message from them and
a `replied_total` of 0 on 2026-09-16, most of them people who wrote first.
`sent_total` needs no such care: the contacts with one above 0 are exactly
the 1,985 with a stored outbound message.

`needs_answer` is true when the contact wrote last: their newest readable
message (`last_received_at`, `build_transcripts`' `newest_inbound_date`)
is newer than our newest readable one, or we never wrote. Both sides are
read from the same `messages` stream, not from `last_sent_date`, which
also counts a system event or a deleted message.

About 65,000 document reads and 14 to 17 seconds a build (measured
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
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

import polars as pl

logger = logging.getLogger(__name__)

ANALYSIS_COLLECTION = "analysis"
EXTRACTED_COLLECTION = "extracted"
FETCH_COLLECTION = "fetch_queue"
MESSAGES_COLLECTION = "messages"

ANALYSIS_FIELDS = [
    "firstName", "lastName", "industry", "function", "seniority",
    "pipeline_stage", "pipeline_reason", "pipeline_classified_at",
    "sent_total", "replied_total", "last_sent_date", "last_reply_date",
    "intro_sent_at", "handling", "hand_set", "profileUrl",
]
EXTRACTED_SELECT = ["fullName", "occupation", "miniProfile", "connect"]
#: The columns filled from outside `analysis`, null until a source has them.
JOINED_FIELDS = ["fullName", "headline", "connected_at"]

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
    "connected_at": _DATETIME, "inbound_total": pl.Int64, "last_received_at": _DATETIME, "needs_answer": pl.Boolean,
}

#: Columns a page may sort on. Anything else falls back to `activity_at`.
SORTABLE = (
    "name", "headline", "industry", "function", "seniority", "pipeline_stage", "handling",
    "sent_total", "replied_total", "connected_at", "last_sent_date", "last_reply_date", "activity_at",
    "last_received_at",
)

#: The named views, by their `view` query value: the label the header's
#: button and the list show.
VIEWS = {"needs_answer": "Need my answer"}

#: The Contacts screen's value filters: query parameter, and the column it
#: matches through `normalized`.
VALUE_FILTERS = {
    "industry": "industry", "function": "function", "seniority": "seniority",
    "stage": "pipeline_stage", "handling": "handling",
}

#: What an empty or missing value is called in counts and filters, as
#: `linkedinmcp.contacts.NONE` names it for `contact_report`.
NONE = "none"

#: The pipeline stages Need my answer keeps, `NONE` for no stage: a contact
#: staged `soft_no`, `reject`, `not_relevant` or `unknown` is left out
#: however recently they wrote.
ANSWER_STAGES = ("prospect", "lead", NONE)

#: The Message Sent and Message Received filters' query values; a missing
#: or other value is Any.
YES_NO = {"yes": True, "no": False}


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


def _epoch_ms(value):
    """LinkedIn Helper's `connect.connectedAt`, milliseconds since the epoch,
    as a datetime. Any other value is returned as it is, for `_typed` to
    drop and count."""
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            return value
    return value


def _newest_outbound(documents) -> dict[str, datetime]:
    """Each contact's newest readable message from us. Readable is
    `pipeline.build_transcripts`' rule, repeated because that function
    returns only the newest message from them: a contact, a timestamp, not
    an event, not deleted, some text."""
    newest: dict[str, datetime] = {}
    for document in documents:
        body = document.to_dict() or {}
        contact, timestamp = body.get("contact_doc_id"), body.get("timestamp")
        if (
            body.get("is_sender") != 1
            or not contact
            or timestamp is None
            or body.get("is_event") == 1
            or body.get("deleted") == 1
            or not (body.get("text") or "").strip()
        ):
            continue
        if contact not in newest or timestamp > newest[contact]:
            newest[contact] = timestamp
    return newest


def load_frame(db) -> pl.DataFrame:
    """Stream the four collections and build the frame."""
    import pipeline  # about 0.8 s, once per process, as `linkedinmcp.contacts.get_conversation` imports it

    dropped: Counter = Counter()
    rows: dict[str, dict] = {}
    for snapshot in db.collection(ANALYSIS_COLLECTION).select(ANALYSIS_FIELDS).stream():
        rows[snapshot.id] = {
            "doc_id": snapshot.id,
            **_fields(snapshot.to_dict() or {}, ANALYSIS_FIELDS, dropped),
            **dict.fromkeys(JOINED_FIELDS),
            "inbound_total": 0,
            "last_received_at": None,
            "needs_answer": False,
        }
    for snapshot in db.collection(EXTRACTED_COLLECTION).select(EXTRACTED_SELECT).stream():
        row = rows.get(snapshot.id)
        if row is None:
            continue
        data = snapshot.to_dict() or {}
        mini, connect = data.get("miniProfile"), data.get("connect")
        joined = {
            "fullName": data.get("fullName"),
            "headline": data.get("occupation") or (mini.get("headline") if isinstance(mini, dict) else None),
            "connected_at": _epoch_ms(connect.get("connectedAt")) if isinstance(connect, dict) else None,
        }
        row.update(_fields(joined, JOINED_FIELDS, dropped))
    for snapshot in db.collection(FETCH_COLLECTION).select(["connected_at"]).stream():
        row = rows.get(snapshot.id)
        if row is None:
            continue
        queued = _fields(snapshot.to_dict() or {}, ["connected_at"], dropped)
        if queued["connected_at"] is not None:
            row.update(queued)
    documents = pipeline.load_messages(db.collection(MESSAGES_COLLECTION))
    transcripts = pipeline.build_transcripts(documents)
    newest_sent = _newest_outbound(documents)
    for doc_id, entry in transcripts.items():
        row = rows.get(doc_id)
        if row is None:
            continue
        received, sent = entry["newest_inbound_date"], newest_sent.get(doc_id)
        row.update(
            inbound_total=entry["inbound_total"],
            last_received_at=received,
            needs_answer=received is not None and (sent is None or received > sent),
        )
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


def normalized(column: str) -> pl.Expr:
    """`column` with an empty value read as `NONE`. `handling` is a
    hand-maintained field that collects stray capitals and padding, so it
    is trimmed and lowercased first, as `contact_report` does."""
    if column == "handling":
        return pl.col(column).fill_null("").str.strip_chars().str.to_lowercase().replace("", NONE)
    return pl.col(column).fill_null(NONE)


def needs_my_answer() -> pl.Expr:
    """The Need my answer view, for the list and the header's count alike:
    the contact wrote last, and their stage is one of `ANSWER_STAGES`."""
    return pl.col("needs_answer") & normalized("pipeline_stage").is_in(list(ANSWER_STAGES))


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


def choices(frame: pl.DataFrame) -> dict[str, list[str]]:
    """Each value filter's values as `frame` holds them, most frequent
    first. Read from the data, not from `profiles`' `Literal` types, so a
    stray stored value can be found too."""
    return {name: [value for value, _count in counts(frame, column)] for name, column in VALUE_FILTERS.items()}


def _every(params: Mapping[str, str], name: str) -> list[str]:
    """Every value of `name`: all of a repeated query parameter, or the one
    value a plain mapping holds."""
    if hasattr(params, "getlist"):
        return params.getlist(name)
    value = params.get(name)
    return [] if value is None else [value]


@dataclass(frozen=True)
class Filters:
    """What the Contacts list is narrowed to. A value filter holds the values
    a contact may have, as `normalized` reads them (`none` for unset), or
    nothing for all. `sent`
    is `True` for the contacts with a message from us (`sent_total` above
    0), `False` for those with none, `None` for any; `received` the same
    over `inbound_total`. `view` is a key of `VIEWS` or `None`: the
    `needs_answer` view is `needs_my_answer`."""

    industry: tuple[str, ...] = ()
    function: tuple[str, ...] = ()
    seniority: tuple[str, ...] = ()
    stage: tuple[str, ...] = ()
    handling: tuple[str, ...] = ()
    sent: bool | None = None
    received: bool | None = None
    view: str | None = None

    @classmethod
    def from_params(cls, params: Mapping[str, str], known: dict[str, list[str]]) -> Self:
        """Filters from a query string, where a value filter repeats
        (`industry=RCM&industry=Pathology`). A value `known` (from
        `choices`) does not hold is ignored, and so is a `sent` or
        `received` other than `yes` or `no`, so the page never shows one
        filter and applies another."""
        return cls(
            **{
                name: tuple(dict.fromkeys(value for value in _every(params, name) if value in known[name]))
                for name in VALUE_FILTERS
            },
            sent=YES_NO.get(params.get("sent")),
            received=YES_NO.get(params.get("received")),
            view=params.get("view") if params.get("view") in VIEWS else None,
        )

    def apply(self, frame: pl.DataFrame) -> pl.DataFrame:
        """`frame` narrowed to the contacts matching every filter that is set."""
        conditions = [
            normalized(column).is_in(list(values))
            for name, column in VALUE_FILTERS.items()
            if (values := getattr(self, name))
        ]
        for wanted, has in ((self.sent, pl.col("sent_total").fill_null(0) > 0), (self.received, pl.col("inbound_total") > 0)):
            if wanted is not None:
                conditions.append(has if wanted else ~has)
        if self.view == "needs_answer":
            conditions.append(needs_my_answer())
        return frame.filter(*conditions) if conditions else frame

    def params(self) -> dict[str, str]:
        """The filters that are set, as query parameters for a link; a value
        filter as the list of its values."""
        chosen = {name: list(values) for name in VALUE_FILTERS if (values := getattr(self, name))}
        flags = {name: "yes" if wanted else "no" for name in ("sent", "received") if (wanted := getattr(self, name)) is not None}
        return chosen | flags | ({"view": self.view} if self.view else {})


def query(
    frame: pl.DataFrame, *, q: str = "", filters: Filters = Filters(), sort: str = "activity_at",
    descending: bool = True, page: int = 1, per_page: int = 100,
) -> tuple[pl.DataFrame, int]:
    """One page of `frame` after the filters and the text search, and the
    total matched. `literal=True`: the needle is text, never a regex."""
    found = filters.apply(frame)
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


class Contacts:
    """Holds the frame and its build time. `db_factory` is called on each
    build (`clients.firestore_client` in the app, a `FakeFirestore` in tests).
    The lock makes a swap and a patch take turns: without it, a patch
    computed from the old frame could replace a rebuild that finished
    meanwhile."""

    def __init__(self, db_factory: Callable[[], object]) -> None:
        self._db_factory = db_factory
        self._lock = threading.Lock()
        self.frame: pl.DataFrame | None = None
        self.built_at: datetime | None = None

    def rebuild(self) -> None:
        """Build a new frame and swap it in; until then readers see the old one."""
        frame = load_frame(self._db_factory())
        with self._lock:
            self.frame, self.built_at = frame, datetime.now(UTC)

    def patch(self, doc_id: str, **fields) -> None:
        """Give one contact's row the values the webapp just wrote, so lists
        and counts show an edit without a rebuild. The row is replaced whole
        (a list value such as `hand_set` cannot be set through `pl.lit`); row
        order does not matter, every query sorts. A contact the frame does
        not hold yet waits for the next Refresh."""
        with self._lock:
            row = contact_row(self.frame, doc_id)
            if row is None:
                return
            others = self.frame.filter(pl.col("doc_id") != doc_id)
            self.frame = pl.concat([others, pl.from_dicts([row | fields], schema=self.frame.schema)])
