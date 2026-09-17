"""A stub LinkedIn client for the job tests, and the few seed helpers every
job test file shares.

`FakeUnipile` stands in for `lib.unipile.UnipileClient` exactly as far as
`linkedinmcp/jobs.py` and `linkedinmcp/fetching.py` touch it:
`messaging.send_message`, `messaging.start_chat`, `messaging.iter_chats`,
`messaging.get_chat`, `messaging.count_messages_sent_since`,
`users.iter_relations`,
`users.get_profile`, `budget.reconcile` / `budget.remaining` /
`budget.used`, and `writes_blocked` -- plus `messaging.iter_all_messages`,
the one read the REAL `messages_sync.forward_pass` makes, for the
end-to-end sync test. It opens no socket and constructs no `httpx.Client`,
so no job test can reach the real Unipile API. It returns the REAL response
models (`MessageSent`, `ChatStarted`, `Chat`, `Relation`, `Profile`) rather
than look-alikes, so a job that reads a field those models do not have
fails here.

Every call is recorded. The two write methods record the call in `attempts`
BEFORE raising any configured `send_error`, so a test can assert both that a
send was attempted and that it was attempted exactly once -- the property
the send-outcome tests exist to pin: whatever the error, a tick never
retries a message within itself. A configured error models the whole range
the real client can raise at that point, from `BudgetExhausted` (raised by
the real client before any HTTP request) to `ServerError` (raised after one).

The circuit breaker behaves like the real transport's
(`lib/unipile/transport.py`): raising an `AccountRestricted` -- from a read
(a profile fetch included) or a send -- sets `writes_blocked`, exactly as
`Transport._raise` does while raising it, and a send made while
`writes_blocked` is set raises `CircuitOpen`, as `Transport._guard_write`
does before any request. So `writes_blocked` never becomes true after a
send that succeeded.

## Shared seed helpers

`make_settings`, `write_template`, `seed_contact`, `seed_message`,
`seed_item`, `seed_fetch` and `store_snapshot` live here, under their own
heading below, because `test_jobs.py`, `test_jobs_sync.py`,
`test_jobs_daily.py`, `test_jobs_tick.py` and `test_fetching.py` all need
them and no test module in this suite imports another test module -- only
support modules like this one and `fake_firestore.py`.
"""

import copy
from datetime import datetime
from pathlib import Path

from lib.unipile import errors as unipile_errors
from lib.unipile.models import Chat, ChatStarted, MessageSent, Profile, Relation
from linkedinmcp import fetch_queue, queue
from linkedinmcp.settings import OutreachSettings


def _raise_as_transport(client: "FakeUnipile", error: BaseException) -> None:
    """Raise `error` as the real transport does: an `AccountRestricted`
    trips the client's breaker (`writes_blocked`) as it is raised, whether a
    read or a send met it -- `Transport._raise`.
    """
    if isinstance(error, unipile_errors.AccountRestricted):
        client.writes_blocked = True
    raise error


class FakeBudget:
    """Mirrors `SendBudget.reconcile`, `remaining`, `used` and `record`:
    counts passed to `reconcile(**observed)` replace the local ones,
    `record(kind)` charges one more (the real client charges a profile
    fetch on every 200 LinkedIn answers, complete or not), `used(kind)` is
    the current count, and `remaining(kind)` is the configured limit minus
    that count, never below zero. `reconcile` and `remaining` calls are
    recorded.
    """

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self.limits = dict(limits) if limits is not None else {"message": 50}
        self.counts: dict[str, int] = {}
        self.reconcile_calls: list[dict] = []
        self.remaining_calls: list[str] = []

    def reconcile(self, **observed: int) -> None:
        self.reconcile_calls.append(dict(observed))
        self.counts.update(observed)

    def used(self, kind: str) -> int:
        return self.counts.get(kind, 0)

    def record(self, kind: str, count: int = 1) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + count

    def remaining(self, kind: str) -> int:
        self.remaining_calls.append(kind)
        return max(0, self.limits.get(kind, 0) - self.used(kind))


class FakeMessaging:
    """`client.messaging`. `attempts` holds one `(route, target, text)`
    tuple per `send_message` / `start_chat` call, appended before any error
    is raised. `read_errors` maps a read method's name to the error it
    raises. Raising an `AccountRestricted` sets `client.writes_blocked`, and
    a send while that is set raises `CircuitOpen` before `send_error` --
    the real transport's breaker (see the module docstring).

    `chats` is LinkedIn's list of conversations: what `iter_chats` yields
    and what `get_chat` looks a chat up in, by id -- an id with no chat
    there raises `NotFound`, as the real endpoint answers an unknown chat.
    `get_chat_calls` records every id asked for.
    """

    def __init__(self, client: "FakeUnipile", *, chats=(), sent_24h: int = 0) -> None:
        self._client = client
        self.chats = list(chats)
        self.mailbox: list = []
        self.sent_24h = sent_24h
        self.attempts: list[tuple] = []
        self.count_calls: list[datetime] = []
        self.get_chat_calls: list[str] = []
        self.iter_chats_calls = 0
        self.send_error: BaseException | None = None
        self.read_errors: dict[str, BaseException] = {}
        self._sequence = 0

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def _raise(self, error: BaseException) -> None:
        _raise_as_transport(self._client, error)

    def _read(self, name: str) -> None:
        error = self.read_errors.get(name)
        if error is not None:
            self._raise(error)

    def _write(self) -> None:
        if self._client.writes_blocked:
            raise unipile_errors.CircuitOpen(
                type="local/circuit_open", title="Writes are blocked because the account was restricted"
            )
        if self.send_error is not None:
            self._raise(self.send_error)

    def send_message(self, chat_id, text) -> MessageSent:
        self.attempts.append(("send_message", chat_id, text))
        self._write()
        return MessageSent(message_id=f"sent-{self._next()}")

    def start_chat(self, attendee_ids, text) -> ChatStarted:
        self.attempts.append(("start_chat", tuple(attendee_ids), text))
        self._write()
        number = self._next()
        return ChatStarted(chat_id=f"chat-new-{number}", message_id=f"sent-{number}")

    def iter_chats(self, *, unread=None, page_size=100):
        self.iter_chats_calls += 1
        self._read("iter_chats")
        return iter(list(self.chats))

    def get_chat(self, chat_id) -> Chat:
        self.get_chat_calls.append(chat_id)
        self._read("get_chat")
        for known in self.chats:
            if known.id == chat_id:
                return known
        raise unipile_errors.NotFound(status=404, title=f"no chat configured for {chat_id}")

    def count_messages_sent_since(self, cutoff: datetime) -> int:
        self.count_calls.append(cutoff)
        self._read("count_messages_sent_since")
        return self.sent_24h

    def iter_all_messages(self, *, before=None, after=None, sender_id=None, page_size=100):
        """`mailbox` (a list of real `Message` models) newest first, with the
        real endpoint's EXCLUSIVE `before`/`after` bounds -- what the real
        `messages_sync.forward_pass` reads.
        """
        self._read("iter_all_messages")
        chosen = [
            message
            for message in self.mailbox
            if (after is None or message.timestamp > after) and (before is None or message.timestamp < before)
        ]
        return iter(sorted(chosen, key=lambda message: message.timestamp, reverse=True))


class FakeUsers:
    """`client.users`: `iter_relations()` over a fixed list, counted, and
    `get_profile(identifier, *, require_complete=False)`, the one profile
    read task 3b's tick makes.

    `get_profile` records `(identifier, require_complete)` in
    `profile_calls`, then raises `profile_error` when one is configured,
    else returns `profiles[identifier]`. An identifier with no configured
    profile raises `NotFound`, as the real endpoint answers an unknown slug.

    Charging mirrors the real client, which records a profile fetch against
    the budget on every 200 (`UsersResource._fetch_profile`): a returned
    profile is charged, and a configured error is charged only when
    `charge_error` is true. Set it for an error that comes AFTER LinkedIn
    answered -- `ProfileIncomplete`, `ThrottleLockout`, a body that failed
    to parse -- and leave it false for one raised before any request or in
    place of a 200 (`BudgetExhausted`, a 4xx, a 5xx, a dropped connection).
    Raising an `AccountRestricted` sets `client.writes_blocked`, as the real
    transport's breaker does on a read too.
    """

    def __init__(self, client: "FakeUnipile", relations=()) -> None:
        self._client = client
        self.relations = list(relations)
        self.iter_relations_calls = 0
        self.profiles: dict[str, Profile] = {}
        self.profile_error: BaseException | None = None
        self.charge_error = False
        self.profile_calls: list[tuple[str, bool]] = []

    def iter_relations(self):
        self.iter_relations_calls += 1
        return iter(list(self.relations))

    def get_profile(self, identifier, *, require_complete=False) -> Profile:
        self.profile_calls.append((identifier, require_complete))
        if self.profile_error is not None:
            if self.charge_error:
                self._client.budget.record("profile")
            _raise_as_transport(self._client, self.profile_error)
        found = self.profiles.get(identifier)
        if found is None:
            raise unipile_errors.NotFound(status=404, title=f"no profile configured for {identifier}")
        self._client.budget.record("profile")
        return found


class FakeUnipile:
    """The client object a job receives. `writes_blocked` is a plain
    attribute: `FakeMessaging` and `FakeUsers` set it when they raise an
    `AccountRestricted`, and a test may set it to stand for a restriction
    some code caught.

    `profile_limit` defaults to 250, `UNIPILE_MAX_PROFILE_FETCHES_PER_DAY`'s
    own default, as `message_limit` defaults to the messages one.
    """

    def __init__(
        self, *, chats=(), relations=(), sent_24h: int = 0, message_limit: int = 50, profile_limit: int = 250
    ) -> None:
        self.writes_blocked = False
        self.messaging = FakeMessaging(self, chats=chats, sent_24h=sent_24h)
        self.users = FakeUsers(self, relations)
        self.budget = FakeBudget({"message": message_limit, "profile": profile_limit})
        self.closed = False

    def close(self) -> None:
        self.closed = True


#: Unipile's chat `type` values (the node SDK's `ChatTypeSchema`: SINGLE 0,
#: GROUP 1, CHANNEL 2). The `Chat` model declares no `type` field; it is
#: kept as one of the model's extra fields, as a real response's is.
ONE_TO_ONE, GROUP, CHANNEL = 0, 1, 2

#: Leave `type` out of the chat altogether (see `chat`).
NO_TYPE = object()


def chat(chat_id: str, attendee_provider_id: str | None, *, type=ONE_TO_ONE, pinned: int = 0) -> Chat:
    """A real `Chat` model, as `iter_chats()` and `get_chat()` return it --
    a one-to-one chat unless `type` says otherwise; `type=NO_TYPE` builds
    one whose response carried no `type` at all. `pinned=1` is a chat
    starred in LinkedIn."""
    if type is NO_TYPE:
        return Chat(id=chat_id, attendee_provider_id=attendee_provider_id, pinned=pinned)
    return Chat(id=chat_id, attendee_provider_id=attendee_provider_id, type=type, pinned=pinned)


def provider_id_of(doc_id: str) -> str:
    """The LinkedIn provider id the seed helpers give contact `doc_id`: the
    `contact_provider_id` `seed_message` stores on their messages, and the
    `attendee_provider_id` of a one-to-one chat with them."""
    return f"ACoAA-{doc_id}"


def relation(slug: str, provider_id: str, connected_at: datetime | None, *, first="Pat", last="Doe") -> Relation:
    """A real `Relation` model, as `iter_relations()` yields it. Its
    `provider_id` is the `member_id` field, exactly as on the real model.
    """
    return Relation(
        public_identifier=slug,
        member_id=provider_id,
        first_name=first,
        last_name=last,
        created_at=connected_at,
    )


def profile(slug: str | None, provider_id: str, **fields) -> Profile:
    """A real `Profile` model, as `get_profile(require_complete=True)`
    returns it; `fields` use the API's own names (`first_name`, `headline`,
    `work_experience`, ...). No `*_total_count` field is set, so the
    profile is complete and `to_lh_document` maps it.
    """
    return Profile.model_validate({"provider_id": provider_id, "public_identifier": slug, **fields})


# =============================================================================
# Shared seed helpers
# =============================================================================


def make_settings(templates_dir: Path, **overrides) -> OutreachSettings:
    """An `OutreachSettings` with every field the jobs and guards read
    pinned, and `_env_file=None` -- so nothing reads `Python/.env` or depends
    on a developer's environment for these values (init arguments outrank
    environment variables in pydantic-settings).
    """
    fields = {
        "api_key": "x" * 16,
        "tz": "UTC",
        "intro_daily_cap": 10,
        "min_days_between_touches": 5,
        "max_touches": 3,
        "message_max_chars": 1200,
        "allowed_link_domains": [],
        "target_industries": ["RCM", "Pathology"],
        "templates_dir": templates_dir,
        "require_approval": False,
        "budget_snapshot_max_age_minutes": 60,
        "allow_http_dry_run": True,
        # 0: every eligible connection, however old -- the selection these
        # tests were written against. The 14-day window has its own test.
        "intro_connection_days": 0,
    }
    fields.update(overrides)
    return OutreachSettings(_env_file=None, **fields)


def write_template(templates_dir: Path, text: str) -> Path:
    """Write `intro.md` into `templates_dir` as UTF-8 and return its path."""
    path = templates_dir / "intro.md"
    path.write_text(text, encoding="utf-8")
    return path


def seed_contact(db, doc_id: str, **fields) -> None:
    """Store an `analysis/{doc_id}` document holding exactly `fields`."""
    db.collection("analysis").document(doc_id).set(dict(fields))


#: `seed_message`'s default `contact_provider_id`: derived from the contact.
_DERIVED = object()


def seed_message(
    db, message_id: str, contact_doc_id: str | None, *, is_sender: int, timestamp: datetime,
    chat_id: str = "chat-1", text: str = "a stored message", contact_provider_id=_DERIVED, **extra,
) -> None:
    """Store one `messages/{message_id}` document shaped like the ones
    `messages_sync` writes: the fields `pipeline.load_messages` projects,
    `contact_provider_id` -- the contact's LinkedIn id, which
    `messages_sync` stores on every message it attributes to them;
    `provider_id_of(contact_doc_id)` unless given, `None` for a message
    with no contact -- plus anything in `extra`.
    """
    if contact_provider_id is _DERIVED:
        contact_provider_id = provider_id_of(contact_doc_id) if contact_doc_id else None
    body = {
        "chat_id": chat_id,
        "contact_doc_id": contact_doc_id,
        "contact_provider_id": contact_provider_id,
        "is_sender": is_sender,
        "timestamp": timestamp,
        "text": text,
        "is_event": 0,
        "deleted": 0,
    }
    body.update(extra)
    db.collection("messages").document(message_id).set(body)


def seed_item(
    db, queue_id: str, contact_doc_id: str, *, now: datetime, kind: str = "follow_up",
    text: str = "Checking back in on denial recovery.", chat_id: str | None = "chat-1",
    provider_id: str | None = None, require_approval: bool = False, due_at: datetime | None = None,
    created_by: str = "agent", tags: list[str] | None = None,
) -> dict:
    """Enqueue one item through the real `queue.enqueue`, so it carries
    every field a real one does. `approved` unless `require_approval` (or
    `kind == "reply"`); move it on with `queue.claim` / `queue.settle` for
    the other statuses.
    """
    item = {
        "contact_doc_id": contact_doc_id,
        "kind": kind,
        "text": text,
        "chat_id": chat_id,
        "provider_id": provider_id,
        "created_by": created_by,
    }
    if due_at is not None:
        item["due_at"] = due_at
    if tags is not None:
        item["tags"] = tags
    stored, _created = queue.enqueue(db, queue_id, item, require_approval=require_approval, now=now)
    return stored


def seed_fetch(db, slug: str, *, now: datetime, provider_id: str = "ACoAAQueued", name: str = "Pat Doe") -> dict:
    """Queue one profile fetch through the real `fetch_queue.enqueue`, as
    the daily job does, and return it as `fetch_queue.get` reads it back.
    """
    fetch_queue.enqueue(db, slug, provider_id=provider_id, name=name, connected_at=now, now=now)
    return fetch_queue.get(db, slug)


def store_snapshot(db) -> dict:
    """A deep copy of everything `db` stores, with empty collections
    dropped.

    `FakeFirestore._collection_store` does `setdefault(name, {})` on every
    access -- a READ of a collection that never held a document leaves an
    empty entry behind -- so a job that wrote nothing at all would still
    compare unequal to the store it started from. An empty collection holds
    nothing, so dropping it loses nothing a "wrote nothing" assertion needs.
    """
    return {name: copy.deepcopy(docs) for name, docs in db._data.items() if docs}
