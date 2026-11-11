"""Chats, messages and attendees."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from ..budget import SendBudget
from ..models import Attendee, Chat, ChatStarted, Message, MessageSent
from ..pagination import iter_account_scoped
from ..transport import Transport


def _api_timestamp(value: datetime) -> str:
    """Encode a datetime for the ``before``/``after`` filters on ``/messages``.

    The API validates against a regex demanding exactly three fractional digits
    and a literal ``Z``. ``datetime.isoformat()`` produces six digits and
    ``+00:00``, and is rejected -- so the string is built by hand rather than
    delegated, and the public methods take a ``datetime`` so no caller ever has
    the chance to pass the wrong shape.

    Sub-millisecond precision is truncated, never rounded: rounding an ``after``
    bound upward would step past a message and drop it from every future walk.
    """
    value = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class MessagingResource:
    """Everything under `/chats`, `/messages` and `/chat_attendees`."""

    def __init__(
        self,
        transport: Transport,
        account_id: Callable[[], str],
        budget: SendBudget,
    ) -> None:
        self._transport = transport
        self._account_id = account_id
        self._budget = budget

    # --- reading --------------------------------------------------------------

    def iter_chats(self, *, unread: bool | None = None, page_size: int = 100) -> Iterator[Chat]:
        """Conversations, newest first.

        `unread=True` is the reply-detection signal an outreach sequence needs:
        a contact who answered must drop out of the cadence.
        """
        extra: dict[str, Any] = {}
        if unread is not None:
            extra["unread"] = "true" if unread else "false"
        return self._list("/api/v1/chats", Chat, page_size, **extra)

    def get_chat(self, chat_id: str) -> Chat:
        return Chat.model_validate(self._transport.get(f"/api/v1/chats/{chat_id}"))

    def iter_messages(self, chat_id: str, *, page_size: int = 100) -> Iterator[Message]:
        return self._list(f"/api/v1/chats/{chat_id}/messages", Message, page_size)

    def iter_attendees(self, chat_id: str, *, page_size: int = 100) -> Iterator[Attendee]:
        return self._list(f"/api/v1/chats/{chat_id}/attendees", Attendee, page_size)

    def iter_all_messages(
        self,
        *,
        before: datetime | None = None,
        after: datetime | None = None,
        sender_id: str | None = None,
        page_size: int = 100,
    ) -> Iterator[Message]:
        """Every message on the account, newest first, across all conversations.

        `iter_messages` walks one chat; this walks the mailbox. Finding a handful
        of new messages the other way costs one request per conversation, which
        on this account is three thousand of them.

        `before` and `after` are **exclusive** bounds and may be combined to ask
        for a window -- which is what lets a backfill terminate by running out of
        window rather than by paging to exhaustion. Both are taken as `datetime`
        because the API's accepted format is narrow enough that hand-built
        strings are the likeliest way to break this call.

        Ordering is strictly timestamp-descending. A caller writing these to a
        durable store must reverse the order first: writing newest-first and
        being interrupted raises the stored high-water mark past messages that
        were never written, and nothing afterwards goes looking for them.
        """
        return self._list(
            "/api/v1/messages",
            Message,
            page_size,
            before=_api_timestamp(before) if before is not None else None,
            after=_api_timestamp(after) if after is not None else None,
            sender_id=sender_id,
        )

    def iter_all_attendees(self, *, page_size: int = 100) -> Iterator[Attendee]:
        """Every attendee on the account, across all conversations.

        The per-chat route answers "who is in this conversation"; this answers
        "who have I ever spoken to", which is the table a bulk join needs. Three
        thousand attendees arrive in thirteen pages instead of three thousand
        requests.
        """
        return self._list("/api/v1/chat_attendees", Attendee, page_size)

    def count_messages_sent_since(self, cutoff: datetime) -> int:
        """How many messages this account sent at or after ``cutoff``.

        Feeds ``SendBudget.reconcile`` so the cap follows a rolling window of
        real sends instead of a local counter that empties at UTC midnight.

        `iter_chats` returns conversations newest first, so the walk stops at the
        first chat whose last activity predates the cutoff rather than paging the
        whole inbox. A quiet account costs a single request; only conversations
        touched inside the window are opened.
        """
        cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
        total = 0
        for chat in self.iter_chats():
            if chat.timestamp is not None and chat.timestamp < cutoff:
                break
            total += sum(
                1
                for message in self.iter_messages(chat.id)
                if message.is_sender
                and message.timestamp is not None
                and message.timestamp >= cutoff
            )
        return total

    def find_chat_with(self, provider_id: str) -> Chat | None:
        """The existing one-to-one chat with a contact, if there is one.

        Every chat carries `attendee_provider_id`, so this is a filter over the
        chat list rather than an attendee-id resolution round trip.

        Cost: `GET /chats` has no attendee filter, so a miss walks every page.
        On an account with hundreds of conversations that is several extra round
        trips before each first message. A caller sending in volume should cache
        `provider_id -> chat_id` and call `send_message` directly.
        """
        for chat in self.iter_chats():
            if chat.attendee_provider_id == provider_id:
                return chat
        return None

    # --- writing --------------------------------------------------------------

    def send_message(self, chat_id: str, text: str, *, quote_id: str | None = None) -> MessageSent:
        """Send into an existing conversation."""
        self._budget.check("message")
        self._budget.throttle()
        body = self._transport.post_form(
            f"/api/v1/chats/{chat_id}/messages",
            data={"text": text, "account_id": self._account_id(), "quote_id": quote_id},
        )
        self._budget.record("message")
        return MessageSent.model_validate(body)

    def start_chat(
        self,
        attendee_ids: list[str],
        text: str,
        *,
        inmail: bool = False,
        subject: str | None = None,
    ) -> ChatStarted:
        """Open a new conversation.

        Array and nested-object encoding follows the `unipile-node-sdk`
        reference implementation: `attendees_ids` is repeated once per id and
        LinkedIn options use bracket notation.
        """
        self._budget.check("message")
        self._budget.throttle()
        payload: dict[str, Any] = {
            "account_id": self._account_id(),
            "attendees_ids": attendee_ids,
            "text": text,
            "subject": subject,
        }
        if inmail:
            payload["linkedin"] = {"api": "classic", "inmail": True}
        body = self._transport.post_form("/api/v1/chats", data=payload)
        self._budget.record("message")
        return ChatStarted.model_validate(body)

    def send_to(self, provider_id: str, text: str, *, inmail: bool = False) -> MessageSent | ChatStarted:
        """Message a contact, reusing their existing chat if one exists.

        This is the direct LinkedIn Helper replacement. Note it only works for
        contacts you can already message: for someone who is not a first-degree
        connection the API raises `NoConnectionWithRecipient`, and the intro has
        to go out as an invitation note instead.
        """
        existing = self.find_chat_with(provider_id)
        if existing is not None:
            return self.send_message(existing.id, text)
        return self.start_chat([provider_id], text, inmail=inmail)

    def mark_read(self, chat_id: str) -> None:
        """Mark a conversation read. Chat actions are JSON, not multipart."""
        self._transport.patch_json(
            f"/api/v1/chats/{chat_id}", json={"action": "setReadStatus", "value": True}
        )

    # --- internals ------------------------------------------------------------

    def _list[T](self, path: str, model: type[T], page_size: int, **extra: Any) -> Iterator[T]:
        return iter_account_scoped(
            self._transport.get, self._account_id, path, model,
            page_size=page_size, **extra,
        )
