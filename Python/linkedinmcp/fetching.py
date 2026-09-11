"""One profile per tick that sends nothing (task 3b): when `tick` finds
nothing due to send, or is stopped only from SENDING -- a sends pause or the
message budget (ruling P3-6) -- fetch ONE queued new connection's LinkedIn
profile, store it in `extracted`, classify it and write the classification
to `analysis` -- what `new-contacts.ipynb` Phases C-E do by hand, for the
daily increment the daily job queued in `fetch_queue` (ledger ruling P3-1).

Profile fetches are the LinkedIn read throttled hardest, so most of this
module is budget, pauses and charging:

- **The budget is a rolling 24 h count** (`profiles_last_24h`): every
  profile stored in `extracted` in the window, whoever stored it -- the
  notebook too -- plus the ledger's `profile` rows for fetches LinkedIn
  charged that stored nothing (`short`, `incomplete`, `failed`). A `stored`
  row is not added: its profile is already one of the `extracted`
  documents. `fetch_one` reconciles the client's budget with this count
  before it fetches, and does not fetch once nothing remains.
- **A ledger row is written only when LinkedIn charged the fetch.** The
  client charges a profile fetch on every 200, complete or not
  (`UsersResource._fetch_profile`), and never for a request refused before
  or instead of one -- the budget refusing locally, a 4xx, a 5xx, a dropped
  connection. So "charged" is read off the budget itself: its `profile`
  count moving during `get_profile`, compared in a `finally`. This counts
  ONE fetch per call, which holds because the service forces zero throttle
  retries (`linkedinmcp.settings.SERVICE_PACING`, ruling P2-22) and
  `get_profile` then makes a single request. With retries turned back on,
  the extra charged attempts of one call would go uncounted.
- **A charged fetch is always counted.** When a step after a charged fetch
  raises -- mapping the profile, creating `extracted`, any write --
  `fetch_one` writes a `failed` row (unless the fetch is already counted:
  its own `short`/`incomplete`/`failed` row, or the `extracted` document it
  created) and counts one attempt on the slug before the error leaves (review
  finding I1), so the same failure can repeat at most `MAX_ATTEMPTS` times.
- **Fetches pause on their own**, independently of sends:
  `runtime_state/linkedin` holds `fetches_paused_until`, reported as
  `fetch_until` so a tick summary can carry a sends pause's `until` beside
  it. LinkedIn withholding sections backs off 30 min, 1 h, 2 h, ... capped
  at a day (`RuntimeState.note_fetch_throttled`), and one clean fetch
  resets that count; a 429 pauses for its Retry-After or an hour; a 401 for
  an hour; a 403 for a day; an unavailable API for 15 minutes.

Nothing is fetched while writes are blocked -- LinkedIn restricted the
account, whether the runtime state or the client's own breaker says so --
`{"fetch": "writes_blocked"}`, with no LinkedIn call and no write (ruling
P3-8). A restricted account is not read again until a human clears the
block.

Before any LinkedIn call, a queued slug whose profile is already in
`extracted` -- the notebook stored it after the daily job queued it -- is
marked `stored` with `classified` = whether `analysis` holds all three
categories (ruling P3-4, amended): `{"fetch": "already_stored"}`, no ledger
row, no `note_fetch_ok`. This comes after the budget check, so while the
profile budget is spent such a slug waits too.

The outcome of one fetch (task 3b's table, amended by rulings P3-3 and
P3-5; most specific class first -- `AccountRestricted` is a
`PermissionDenied`, `AccountDisconnected` an `AuthenticationError`):

- a profile whose summary is longer than `profiles.SUMMARY_MIN_LEN`:
  ledger `stored`; stored and classified (below); the slug marked `stored`
  with `classified`; `note_fetch_ok`.
- a profile whose summary is not: ledger `short`; the slug marked `short`;
  `note_fetch_ok`.
- `ProfileIncomplete`, `ThrottleLockout`: ledger `incomplete`; the slug
  moves to the back of the queue with one more `incomplete_count` and no
  attempt -- it is LinkedIn throttling, not a failed try -- and is given up
  as `failed` at `fetch_queue.MAX_INCOMPLETE` (ruling P3-3);
  `note_fetch_throttled`.
- `BudgetExhausted`, `CircuitOpen` (refused before any request): nothing.
- `RateLimited`: ledger `failed`; fetches pause for the Retry-After or an
  hour, from the later of the tick's `now` and the state's clock
  (`jobs._pause_start`).
- `AccountRestricted`: ledger `failed`; fetches pause a day. No alert and
  no block here: `jobs._watch_restriction` blocks writes and raises the
  `restricted` alert whenever the client's breaker has tripped, which a
  read trips as readily as a send.
- any other `PermissionDenied`: ledger `failed`; alert `fetch_forbidden`;
  fetches pause a day.
- `AuthenticationError`, `AccountDisconnected`: ledger `failed`; alert
  `fetch_disconnected`; fetches pause an hour.
- `NotFound`, `UnprocessableError`: ledger `failed`; the slug marked
  `failed` with the class name.
- `ServerError`, `httpx.TransportError` (the API unavailable): ledger
  `failed`; the slug stays queued with no attempt counted; fetches pause 15
  minutes (ruling P3-5) -- an outage must not use up every slug's attempts.
- anything else: ledger `failed`; one attempt on the slug
  (`fetch_queue.note_attempt`, `failed` at its `MAX_ATTEMPTS`).

Ledger rows are "when charged" throughout: a 4xx or 5xx is never charged,
so in practice only `stored`, `short`, `incomplete` and an error after a
200 (a body that failed to parse) leave one.

**Storing and classifying** mirror the notebook's Phases D-E: the
`to_lh_document` document with `summary = join_keys(document,
SUMMARY_KEYS)` and a server `created_at`, CREATED in `extracted` -- a
document already there (the notebook stored it while LinkedIn answered) is
left untouched -- then `profiles.classify_profile` and, when it returns a
classification, `analysis/{doc_id}` merged with `profiles.analysis_body`.
Ruling P3-2: this is the ONE writer allowed to create an `analysis`
document; every other writer still merges only onto one that exists. It
never merges over a classification already there (minor M4): an `analysis`
document holding all three categories is left as it is -- no Gemini call --
and the merge itself re-reads the document in its transaction, in case the
notebook classified the contact while Gemini ran. A tick with less than
`jobs.LEASE_FLOOR_SECONDS` of lease left stores the profile without
classifying it (minor M2). A classification of `None` leaves the contact for
Phase E, which classifies anything stored but unclassified. So does a Gemini
client that cannot even be built (a missing key): that is logged, raised as
a `gemini_unavailable` alert once per local day (minor M3), and treated as
`None` -- letting it raise would leave the slug queued, its profile fetched
from LinkedIn again on every tick only to fail the same way. A Firestore
error while storing propagates, after the fetch is counted (above): the
tick's run is recorded failed and the slug stays queued, to be fetched again
at most `MAX_ATTEMPTS` times -- or, when `extracted` was written, marked
`already_stored` by the next tick without a LinkedIn call.

The send path's own pieces -- `jobs.LEASE_FLOOR_SECONDS`, `jobs.DEFAULT_PAUSE`,
`jobs._retry_after` (a 429's Retry-After, cleaned and capped),
`jobs._pause_start` (when that pause starts) and
`jobs._local_day` (a once-per-local-day alert key) -- are read through the
`jobs` module rather than copied. `jobs` imports this module at its top, so
this module imports `jobs` inside the functions that use it. Project modules
(`functions`, `profiles`, `lib.unipile.compat`) are imported the same way and
reached through the module, so importing this one stays free and a test can
replace them.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from linkedinmcp import clients, decisions, fetch_queue, ledger

#: The rolling window the profile cap applies to.
PROFILE_WINDOW = timedelta(hours=24)

#: Ledger `profile` results for fetches LinkedIn charged that stored
#: nothing -- the ones the `extracted` count cannot see.
UNCOUNTED_BY_EXTRACTED = ("short", "incomplete", "failed")

#: How long fetches pause after a 403, a restriction or any other refusal.
FORBIDDEN_PAUSE = timedelta(hours=24)

#: Ruling P3-5: how long fetches pause when the LinkedIn API is unavailable
#: (a 5xx, a dropped or timed-out connection).
UNAVAILABLE_PAUSE = timedelta(minutes=15)

EXTRACTED_COLLECTION = "extracted"
ANALYSIS_COLLECTION = "analysis"

#: The `analysis` fields a classification fills -- `new-contacts.ipynb`
#: Phase E's `CATEGORY_FIELDS`. A document is classified only when all
#: three are non-empty.
CATEGORY_FIELDS = ("industry", "function", "seniority")

logger = logging.getLogger(__name__)


@dataclass
class _Charge:
    """One `get_profile` call's cost, carried through the outcome that
    follows it. `charged`: LinkedIn charged the fetch (the budget's
    `profile` count moved). `counted`: `profiles_last_24h` already sees it
    -- this call wrote a `short`, `incomplete` or `failed` row, or created
    the `extracted` document. A `stored` row does not count it: the count
    relies on `extracted` for that, and after a `Conflict` the document
    there is the notebook's, not this fetch's.
    """

    slug: str
    charged: bool
    counted: bool = False


def profiles_last_24h(db, now) -> int:
    """Profile fetches in the 24 h before `now`: `extracted` documents
    created since then -- one query, counted server-side -- plus `profile`
    ledger rows `short`, `incomplete` or `failed` since then.
    """
    import functions

    cutoff = now - PROFILE_WINDOW
    stored = functions.count_created_since(db.collection(EXTRACTED_COLLECTION), cutoff)
    return stored + ledger.count_since(db, "profile", cutoff, results=UNCOUNTED_BY_EXTRACTED)


def fetch_one(db, client, settings, now, *, state, owner, classify=True, slugs=None) -> dict:
    """Fetch, store and classify at most ONE queued profile, and return a
    small summary: `{"fetch": ...}` plus `fetch_until`, `classified` or
    `error` where the outcome has one (see the module docstring).

    `classify=False` stores without classifying -- the `get_contacts` step
    (`steps.py`), which keeps storing and classifying separate as
    `new-contacts.ipynb` does; `classify_contacts` classifies later. The
    tick always classifies as it goes. `slugs` restricts the choice to
    those queued slugs -- that step's own connections; the tick passes
    none and takes the whole queue.

    0. `{"fetch": "writes_blocked"}` while writes are blocked -- in the
       runtime state, or on the client's own breaker (`client.writes_blocked`:
       some code in this job caught an `AccountRestricted`) -- with no
       LinkedIn call and no write (ruling P3-8). A tick that stopped sending
       for a message-side reason reaches this path (ruling P3-6), and the
       restriction may have landed after its own state check: while the
       message budget was recounted, say.
    1. `{"fetch": "paused", "fetch_until": ...}` while fetches are paused.
    2. Reconcile the client's `profile` budget with `profiles_last_24h`;
       `{"fetch": "budget"}` when none remains.
    3. The next queued slug -- among `slugs` when given -- the newest
       connection first (`fetch_queue.next_queued`);
       `{"fetch": "idle"}` when there is none.
    4. Its profile already in `extracted`: mark it `stored`, and return
       `{"fetch": "already_stored", "classified": ...}` -- no LinkedIn call
       (ruling P3-4).
    5. `{"fetch": "lease_short"}` when the tick holds less than
       `jobs.LEASE_FLOOR_SECONDS` of its lease.
    6. `client.users.get_profile(slug, require_complete=True)`. Whether
       LinkedIn charged it is read off the budget in a `finally` -- the
       client charges any 200, complete or not -- and then the outcome is
       applied. An exception out of applying it, after a charged fetch, is
       counted first (`_count_charged_failure`) and then re-raised.
    """
    from linkedinmcp import jobs

    if state.writes_blocked() or client.writes_blocked:
        return {"fetch": "writes_blocked"}

    until = state.fetches_paused_until()
    if until is not None:
        return {"fetch": "paused", "fetch_until": until.isoformat()}

    client.budget.reconcile(profile=profiles_last_24h(db, now))
    if client.budget.remaining("profile") <= 0:
        return {"fetch": "budget"}

    item = fetch_queue.next_queued(db, now, slugs=slugs)
    if item is None:
        return {"fetch": "idle"}
    slug = item["id"]

    if _in_extracted(db, slug):
        classified = _classified(db, slug)
        fetch_queue.mark(db, slug, fetch_queue.STORED, now, classified=classified)
        return {"fetch": "already_stored", "classified": classified}

    if state.lease_remaining(owner) < jobs.LEASE_FLOOR_SECONDS:
        return {"fetch": "lease_short"}

    before = client.budget.used("profile")
    fetched = failure = None
    try:
        fetched = client.users.get_profile(slug, require_complete=True)
    except Exception as error:
        failure = error
    finally:
        charged = client.budget.used("profile") > before

    charge = _Charge(slug, charged)
    try:
        if failure is not None:
            return _after_failed_fetch(db, settings, now, state=state, charge=charge, error=failure)
        return _after_fetched(
            db, settings, now, state=state, owner=owner, charge=charge, fetched=fetched, classify=classify
        )
    except Exception as error:
        if charge.charged:
            _count_charged_failure(db, now, charge, error)
        raise


def preview(db, now, *, state) -> dict:
    """What `fetch_one` would do, for the dry tick: no LinkedIn call and no
    write. `{"fetch": "writes_blocked"}` while writes are blocked in the
    runtime state (ruling P3-8, as `fetch_one` does);
    `{"fetch": "paused", "fetch_until": ...}` while fetches are
    paused; otherwise `{"fetch": "idle"}`, `{"fetch": "already_stored",
    "slug": ..., "classified": ...}` (the slug `fetch_one` would mark
    without a LinkedIn call) or `{"fetch": "would_fetch", "slug": ...}`,
    each with `profiles_24h` -- the count `fetch_one` would reconcile the
    budget with, which needs only Firestore. The budget itself is not
    reconciled or checked: that needs the client's limit.
    """
    if state.writes_blocked():
        return {"fetch": "writes_blocked"}
    until = state.fetches_paused_until()
    if until is not None:
        return {"fetch": "paused", "fetch_until": until.isoformat()}
    profiles_24h = profiles_last_24h(db, now)
    item = fetch_queue.next_queued(db, now)
    if item is None:
        return {"fetch": "idle", "profiles_24h": profiles_24h}
    slug = item["id"]
    if _in_extracted(db, slug):
        classified = _classified(db, slug)
        return {"fetch": "already_stored", "slug": slug, "classified": classified, "profiles_24h": profiles_24h}
    return {"fetch": "would_fetch", "slug": slug, "profiles_24h": profiles_24h}


def _in_extracted(db, slug) -> bool:
    """Whether `extracted/{slug}` exists -- the notebook stored the profile
    after the daily job queued it (ruling P3-4)."""
    return db.collection(EXTRACTED_COLLECTION).document(slug).get().exists


def _classified(db, doc_id) -> bool:
    """Whether `analysis/{doc_id}` holds a classification (see
    `_has_classification`)."""
    return _has_classification(db.collection(ANALYSIS_COLLECTION).document(doc_id).get().to_dict())


def _has_classification(data: dict | None) -> bool:
    """Whether an `analysis` document's fields hold a classification:
    industry, function and seniority all non-empty -- `new-contacts.ipynb`
    Phase E's rule (ruling P3-4, amended). A document can exist with none of
    them: thousands hold only a contact's name, email and message tallies.
    """
    data = data or {}
    return all(data.get(field) for field in CATEGORY_FIELDS)


def _after_fetched(db, settings, now, *, state, owner, charge, fetched, classify=True) -> dict:
    """The two rows for a profile `get_profile` returned: `stored` when its
    summary is longer than `profiles.SUMMARY_MIN_LEN`, else `short` --
    `new-contacts.ipynb` Phase D's own test. The slug leaves `queued` only
    after the ledger row, the `extracted` document and the classification
    are written, so a failure before then leaves it queued (and
    `fetch_one` counts the fetch, see `_count_charged_failure`).
    """
    import functions
    import profiles
    from lib.unipile import compat

    document = compat.to_lh_document(fetched)
    document["summary"] = functions.join_keys(document, compat.SUMMARY_KEYS)

    if len(document["summary"]) <= profiles.SUMMARY_MIN_LEN:
        _record(db, "short", document["id"], now, charge=charge)
        fetch_queue.mark(db, charge.slug, fetch_queue.SHORT, now)
        state.note_fetch_ok()
        return {"fetch": "short"}

    classified = _store_and_classify(
        db, settings, document, now, state=state, owner=owner, charge=charge, classify=classify
    )
    fetch_queue.mark(db, charge.slug, fetch_queue.STORED, now, classified=classified)
    state.note_fetch_ok()
    return {"fetch": "stored", "classified": classified}


def _store_and_classify(db, settings, document, now, *, state, owner, charge, classify=True) -> bool:
    """Create `extracted/{document["id"]}` -- an existing document is left
    untouched -- record the `stored` row, then classify and merge the
    result into `analysis`. Returns whether the contact is classified:

    1. `analysis` already holds a classification (all three categories
       non-empty): `True`, with nothing merged and no Gemini call (minor
       M4). With `classify=False` the answer stops here: `False` otherwise,
       and no Gemini call.
    2. Less than `jobs.LEASE_FLOOR_SECONDS` of the tick's lease left:
       `False`, no Gemini call (minor M2) -- Phase E classifies it.
    3. Gemini's result, when there is one, merged into `analysis`
       (`_merge_classification`, which again writes nothing over a
       classification that appeared meanwhile): `True`. No result: `False`.

    The document id is the profile's public identifier, or its provider id
    when LinkedIn returned none; the fetch queue keeps the slug it queued.
    """
    import profiles
    from google.api_core import exceptions as api_exceptions
    from google.cloud import firestore

    from linkedinmcp import jobs

    doc_id = document["id"]
    document["created_at"] = firestore.SERVER_TIMESTAMP
    try:
        db.collection(EXTRACTED_COLLECTION).document(doc_id).create(document)
    except api_exceptions.Conflict:
        pass
    else:
        charge.counted = True
    _record(db, "stored", doc_id, now, charge=charge)

    if _classified(db, doc_id):
        return True
    if not classify or state.lease_remaining(owner) < jobs.LEASE_FLOOR_SECONDS:
        return False
    result = _classify(db, settings, now, document["summary"].replace("\n", " "), charge.slug)
    if result is None:
        return False
    _merge_classification(db, doc_id, profiles.analysis_body(document, result))
    return True


def _merge_classification(db, doc_id, body) -> bool:
    """Merge `body` -- `profiles.analysis_body`, the ONE way this service
    creates or changes an `analysis` document (ruling P3-2) -- into
    `analysis/{doc_id}`, in a transaction that first re-reads it and writes
    nothing when it already holds a classification (minor M4): the
    notebook's Phase E, or a human, may have classified the contact while
    Gemini ran. Returns whether it wrote. `merge=True`: the document also
    carries contact names, emails and message tallies that exist nowhere
    else.
    """
    from google.cloud import firestore

    ref = db.collection(ANALYSIS_COLLECTION).document(doc_id)

    @firestore.transactional
    def _merge(transaction):
        if _has_classification(ref.get(transaction=transaction).to_dict()):
            return False
        transaction.set(ref, body, merge=True)
        return True

    return _merge(db.transaction())


def _classify(db, settings, now, summary, slug):
    """`profiles.classify_profile(clients.gemini_client(), summary)`, which
    itself returns `None` on any failure of the Gemini call.

    A Gemini client that cannot even be built (a missing key) is `None`
    too, rather than an error -- see the module docstring for why that must
    not raise -- logged, and (minor M3) raised as a `gemini_unavailable`
    alert once per local day. The alert is written here, before the caller
    marks the slug stored, as the fetch alerts are written before the state
    they explain.
    """
    import profiles

    from linkedinmcp import jobs

    try:
        gemini = clients.gemini_client()
    except Exception as error:
        name = type(error).__name__
        logger.warning(
            "classifying %s could not start (%s: %s); it is stored unclassified for new-contacts.ipynb Phase E",
            slug,
            name,
            error,
        )
        decisions.raise_alert(
            db,
            "gemini_unavailable",
            jobs._local_day(now, settings),
            (
                f"The service could not set up Gemini to classify new profiles ({name}). Profiles are still stored "
                "in `extracted`, unclassified; new-contacts.ipynb Phase E classifies them."
            ),
            {"slug": slug, "error": name},
            now,
        )
        return None
    return profiles.classify_profile(gemini, summary)


def _after_failed_fetch(db, settings, now, *, state, charge, error) -> dict:
    """The rows for an exception out of `get_profile`, most specific class
    first (see the module docstring for the table).

    Each row writes its ledger row first -- for these rows the ledger row
    IS the budget's record of the fetch -- then acts on the fetch queue,
    then on the state. An alert is written BEFORE the fetch pause, for the
    reason `jobs._note_restriction` gives: the pause is what makes every
    later tick stop before meeting the error again, so an alert that failed
    after it would never be raised. If the alert write fails, nothing is
    paused and the next tick does both; if the pause fails after it, the
    next tick's alert is the same once-per-local-day key and writes nothing.
    """
    from lib.unipile import errors as unipile_errors
    from linkedinmcp import jobs

    name = type(error).__name__
    slug = charge.slug

    if isinstance(error, (unipile_errors.ProfileIncomplete, unipile_errors.ThrottleLockout)):
        _record(db, "incomplete", slug, now, charge=charge)
        fetch_queue.requeue_incomplete(db, slug, now, name)
        until = state.note_fetch_throttled()
        return {"fetch": "throttled", "fetch_until": until.isoformat()}

    if isinstance(error, unipile_errors.BudgetExhausted):
        return {"fetch": "budget"}
    if isinstance(error, unipile_errors.CircuitOpen):
        return {"fetch": "circuit_open"}

    # Every row from here on records `failed` when LinkedIn charged the fetch.
    _record(db, "failed", slug, now, charge=charge)

    if isinstance(error, unipile_errors.RateLimited):
        state.pause_fetches(jobs._pause_start(now, state) + jobs._retry_after(error), "rate limited")
        return {"fetch": "rate_limited"}

    if isinstance(error, unipile_errors.AccountRestricted):
        state.pause_fetches(now + FORBIDDEN_PAUSE, f"LinkedIn restricted the account ({name})")
        return {"fetch": "restricted"}

    if isinstance(error, unipile_errors.PermissionDenied):
        until = now + FORBIDDEN_PAUSE
        _raise_fetch_alert(
            db, settings, now, "fetch_forbidden", slug=slug, error=name, until=until,
            question=(
                f"LinkedIn refused to show the profile {slug} ({name}). Profile fetches are paused until "
                f"{until.isoformat()}; the profile stays queued and is fetched again after that."
            ),
        )
        state.pause_fetches(until, f"LinkedIn refused a profile fetch ({name})")
        return {"fetch": "forbidden"}

    if isinstance(error, unipile_errors.AuthenticationError):
        until = now + jobs.DEFAULT_PAUSE
        _raise_fetch_alert(
            db, settings, now, "fetch_disconnected", slug=slug, error=name, until=until,
            question=(
                f"Unipile could not fetch a profile for the LinkedIn account ({name}) -- it may need "
                f"reconnecting. Profile fetches are paused until {until.isoformat()}; the profile stays queued."
            ),
        )
        state.pause_fetches(until, f"LinkedIn account disconnected ({name})")
        return {"fetch": "disconnected"}

    if isinstance(error, (unipile_errors.NotFound, unipile_errors.UnprocessableError)):
        fetch_queue.mark(db, slug, fetch_queue.FAILED, now, error=name)
        return {"fetch": "failed"}

    import httpx

    if isinstance(error, (unipile_errors.ServerError, httpx.TransportError)):
        state.pause_fetches(now + UNAVAILABLE_PAUSE, "LinkedIn API unavailable")
        return {"fetch": "unavailable"}

    fetch_queue.note_attempt(db, slug, now, name)
    return {"fetch": "error", "error": name}


def _record(db, result, contact_doc_id, now, *, charge) -> None:
    """One `profile` ledger row, keyed to the fetch-queue slug -- written
    only when LinkedIn charged the fetch. A `short`, `incomplete` or
    `failed` row is the budget's record of it (`charge.counted`)."""
    if not charge.charged:
        return
    ledger.record(db, "profile", contact_doc_id, result, now, queue_id=charge.slug)
    if result in UNCOUNTED_BY_EXTRACTED:
        charge.counted = True


def _count_charged_failure(db, now, charge, error) -> None:
    """A step after a CHARGED fetch raised `error` (review finding I1):
    count the fetch before `error` leaves `fetch_one`, so no later tick can
    fetch the same profile again, charged and counted nowhere, without
    limit.

    - A `failed` row, unless `profiles_last_24h` already sees this fetch
      (`charge.counted`: its `short`/`incomplete`/`failed` row was written,
      or its `extracted` document created).
    - One attempt on the slug (`fetch_queue.note_attempt`, `failed` at
      `MAX_ATTEMPTS`) -- nothing when the slug has already left `queued`.

    Both are best-effort: the step that raised is often a Firestore write,
    and the same outage fails these as well. Each failure here is logged,
    and `error` -- the root cause -- is still what leaves `fetch_one`.
    """
    name = type(error).__name__
    if not charge.counted:
        try:
            _record(db, "failed", charge.slug, now, charge=charge)
        except Exception as ledger_error:
            logger.warning(
                "the charged fetch of %s failed after LinkedIn answered (%s), and its `failed` row could not be "
                "written (%s: %s)",
                charge.slug, name, type(ledger_error).__name__, ledger_error,
            )
    try:
        fetch_queue.note_attempt(db, charge.slug, now, name)
    except Exception as queue_error:
        logger.warning(
            "the charged fetch of %s failed after LinkedIn answered (%s), and its attempt could not be counted "
            "(%s: %s)",
            charge.slug, name, type(queue_error).__name__, queue_error,
        )


def _raise_fetch_alert(db, settings, now, kind, *, slug, error, until, question) -> None:
    """A `kind` alert keyed by the local day (`jobs._local_day`), so a
    refusal that repeats all day is one alert, not one per tick."""
    from linkedinmcp import jobs

    decisions.raise_alert(
        db,
        kind,
        jobs._local_day(now, settings),
        question,
        {"slug": slug, "error": error, "paused_until": until.isoformat()},
        now,
    )
