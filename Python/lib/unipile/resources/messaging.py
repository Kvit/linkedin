"""Chats, messages and attendees."""

from collections.abc import Callable, Iterator
from typing import Any

from ..budget import SendBudget
from ..models import Attendee, Chat, ChatStarted, Message, MessageSent
from ..pagination import iter_account_scoped
from ..transport import Transport


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
