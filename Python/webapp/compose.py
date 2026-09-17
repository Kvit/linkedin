"""The message box above the conversation: **Expand with AI** and **Send**.

Design: the spec's correction 8 (`docs/superpowers/specs/2026-09-16-contacts-webapp-design.md`).

**Expand with AI** asks Gemini (`WebappSettings.expand_model`) to turn the
box's text into the message, with the conversation and the profile summary
as context: `pipeline.build_prompt` -- the text the stage classifier reads --
followed by the note. Nothing is stored. The client is built on the first
press (`gemini`): `genai.Client()` needs `GOOGLE_API_KEY` when it is built,
which tests do not have, and one uvicorn process runs one event loop.

**Send** sends the box's text at once, through `lib.unipile`, and records it
as the outreach service records its own sends: an `outreach_queue` item,
created straight in `sending` (`queue.start_manual`) before the LinkedIn
call and settled after it (`queue.settle`, which writes the `action_log`
row the 24-hour count reads). The item carries `tags: ["manual"]`, and
`messages_sync` copies an item's tags onto the stored message by
`message_id`: that is how the tag reaches `messages`. The webapp never
writes `messages` itself -- the sync rewrites each document whole, and a
document it did not write would move the timestamp its next pass starts from.

The item's id holds a token the page was rendered with, and the item is
created create-only, so a form submitted twice sends one message.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

import pipeline
from lib.unipile import errors as unipile_errors
from linkedinmcp import clients, clock, contacts as reads, decisions, guards, jobs, queue, state as runtime_state
from linkedinmcp.mcp_server import _newest_chat_id
from webapp import contacts as contact_screen, routine

logger = logging.getLogger(__name__)

router = APIRouter()

ANALYSIS_COLLECTION = "analysis"
FETCH_COLLECTION = "fetch_queue"
EXTRACTED_COLLECTION = "extracted"

#: Who claims a manual item, and the tag it carries.
OWNER = "webapp"
TAG = "manual"

#: One Expand call: two attempts of at most 120 s each. A Pro model thinks
#: before it answers, and the prompt holds up to 35 KB of profile and 20 KB
#: of conversation.
EXPAND_TIMEOUT_MS = 120_000
EXPAND_ATTEMPTS = 2

#: Who we are, as the stage classifier is told it: the product and the
#: account sentences of its instructions' first paragraph, without the
#: sentences about classifying and about polite answers.
ABOUT = ". ".join(pipeline.PIPELINE_INSTRUCTIONS.split("\n\n", 1)[0].split(". ")[1:3]) + "."

NO_CONVERSATION = "No messages yet: this is the first message to them."


def expand_instructions(max_chars: int) -> str:
    """The system instruction for **Expand with AI**."""
    return f"""You write LinkedIn messages for Vitali. {ABOUT}

# Input
PROFILE: the classification on file, then a mechanical dump of the contact's LinkedIn profile; read the professional content.
CONVERSATION: every message exchanged with the contact, oldest first. Lines marked "Me:" were written by Vitali, lines marked "Them:" by the contact.
NOTE: what Vitali wants this message to say, often in shorthand.

# Task
Write the one message Vitali sends next, in Vitali's voice, in the first person.
- The NOTE is the whole content. Say what it says and keep every fact, name and number in it. Ask only what it asks: add no call, demo, meeting length, date, next step, price, promise or claim that the NOTE does not contain.
- The CONVERSATION is what both people already know. Use it to make the message specific: name the open item in the fewest words that identify it, and never retell the conversation or list its details. They remember what they wrote.
- A short NOTE makes a short message. "check status" is one or two sentences asking where the open item from the conversation stands.
- When the contact wrote last, answer their newest message.
- Match the language of the conversation.
- Plain text in one paragraph: no markdown, no bullet symbols, no blank lines, no subject line, no placeholders such as [Name] or {{first_name}}, and no link unless the NOTE contains one.
- Never write an email address or a phone number, not even one from the CONVERSATION, unless the NOTE contains it.
- At most {max_chars} characters; shorter is better.
- Answer with the message text only.

# Style: one executive writing to another
- A business note between peers: direct, brief and courteous, with no eagerness, no deference and no pitch.
- Start with the point. Never open by saying you are following up, checking in, reaching out, circling back or checking on the status.
- End when the point is made. No closing offer of more information or help, and no "looking forward to hearing from you".
- Greet with "Hi" and the first name at most. No sign-off and no name at the end: LinkedIn shows who wrote it.
- No sales or marketing jargon and no stock phrases. Never: "jump on a call", "hop on a call", "a quick call", "grab some time", "touch base", "pick your brain", "just following up", "just checking in", "I hope this finds you well", "I wanted to reach out", "at your earliest convenience", "no pressure", "let me know if you have any questions", "don't hesitate to reach out", "feel free to", "value proposition", "add value", "pain points", "solution", "offering", "partner with you", "move the needle", "best-in-class", "industry-leading", "game-changer", "cutting-edge", "seamless", "leverage", "streamline", "synergy", "robust", "empower", "unlock", "revolutionize".
- Name the specific thing -- their lab, their payer, the problem or the person they mentioned -- instead of general words about software."""


def expand_prompt(contact: dict, conversation: dict | None, note: str) -> str:
    """The user turn: the profile and conversation as the stage classifier
    reads them, then the note."""
    transcript = conversation["transcript"] if conversation else NO_CONVERSATION
    return f"{pipeline.build_prompt(contact, transcript)}\n\nNOTE\n{note.strip()}"


_gemini: genai.Client | None = None


def gemini() -> genai.Client:
    """The Gemini client, built on the first press and kept."""
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(
            http_options=types.HttpOptions(
                timeout=EXPAND_TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=EXPAND_ATTEMPTS)
            )
        )
    return _gemini


class ExpandRequest(BaseModel):
    text: str


@router.post("/contacts/{doc_id}/expand")
async def expand(request: Request, doc_id: str, body: ExpandRequest) -> dict:
    """The box's text turned into the message, for the page's script to put
    in the box: `{ok, text, model, seconds, problem}`, `problem` being the
    text check's sentence when the answer would be refused on Send; or
    `{ok: false, detail}`."""
    note = body.text.strip()
    if not note:
        return {"ok": False, "detail": "Write a note first: Expand with AI turns it into the message."}
    settings, outreach = request.app.state.settings, request.app.state.outreach
    db = clients.firestore_client()
    contact = await asyncio.to_thread(reads.get_contact, db, outreach, doc_id, full=True)
    if contact is None:
        raise HTTPException(status_code=404, detail="No such contact.")
    conversation = await asyncio.to_thread(reads.get_conversation, db, doc_id)
    started = time.monotonic()
    try:
        response = await gemini().aio.models.generate_content(
            model=settings.expand_model,
            contents=expand_prompt(contact, conversation, note),
            config=types.GenerateContentConfig(
                system_instruction=expand_instructions(outreach.message_max_chars),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except Exception as error:
        logger.exception("Expand with AI failed for %s", doc_id)
        return {"ok": False, "detail": f"Gemini did not answer: {type(error).__name__}: {error}"}
    seconds = round(time.monotonic() - started, 1)
    text = (response.text or "").strip()
    if not text:
        finish = response.candidates[0].finish_reason if response.candidates else None
        return {"ok": False, "detail": f"Gemini answered without text (finish reason {finish}). Try again or rewrite the note."}
    usage = response.usage_metadata
    logger.info(
        "Expand with AI for %s: %s in %.1f s, %s prompt tokens, %s thinking, %s output",
        doc_id, response.model_version, seconds, getattr(usage, "prompt_token_count", None),
        getattr(usage, "thoughts_token_count", None), getattr(usage, "candidates_token_count", None),
    )
    verdict = guards.validate_text(text, outreach)
    return {
        "ok": True, "text": text, "model": response.model_version or settings.expand_model, "seconds": seconds,
        "problem": None if verdict.ok else verdict.detail,
    }


@dataclass
class Outcome:
    """What one press of Send did: sent at `sent_at`, or not sent because of
    `refusal` (reason, sentence), `warnings` to confirm, or `open_item`."""

    sent_at: datetime | None = None
    refusal: tuple[str, str] | None = None
    warnings: list[str] = field(default_factory=list)
    open_item: dict | None = None


def _refused(reason: str, detail: str, **extra) -> Outcome:
    return Outcome(refusal=(reason, detail), **extra)


def _local(value: datetime, tz: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(ZoneInfo(tz)).strftime(fmt)


def _provider_id(db, doc_id: str, messages: list[dict]) -> str | None:
    """The contact's LinkedIn id for opening a chat: the one their stored
    messages agree on (`jobs._contact_provider_id`), else the fetch queue's,
    else the `li-hash-id` LinkedIn Helper or the fetch stored in `extracted`."""
    found = jobs._contact_provider_id({}, messages)
    if found:
        return found
    fetched = db.collection(FETCH_COLLECTION).document(doc_id).get()
    value = (fetched.to_dict() or {}).get("provider_id") if fetched.exists else None
    if isinstance(value, str) and value:
        return value
    for snapshot in db.get_all([db.collection(EXTRACTED_COLLECTION).document(doc_id)], field_paths=["externalIds"]):
        for entry in (snapshot.to_dict() or {}).get("externalIds") or []:
            if isinstance(entry, dict) and entry.get("type") == "li-hash-id" and entry.get("externalId"):
                return entry["externalId"]
    return None


def _warnings(contact: dict, messages: list[dict], items: list[dict], tz: str, now: datetime) -> list[str]:
    """What a second press confirms: a stage that says no, and a second
    message today to someone who has not written since the first."""
    found = []
    stage = contact.get("pipeline_stage")
    if stage in guards.BLOCKED_STAGES:
        found.append(f"Their stage is {stage}.")
    usable = guards._usable_messages(messages)
    outbound = [message for message in usable if message["is_sender"] == 1]
    if guards._messaged_today(outbound, {}, items, clock.local_date(now, tz), tz):
        touched = [message["timestamp"] for message in outbound] + [
            item[name] for item in items if item.get("status") in (queue.SENT, queue.SENDING, queue.UNKNOWN)
            for name in ("sent_at", "sending_at") if item.get(name) is not None
        ]
        newest_inbound = max((message["timestamp"] for message in usable if message["is_sender"] == 0), default=None)
        if newest_inbound is None or newest_inbound < max(touched):
            found.append("You already wrote to them today, and they have not written since.")
    return found


def _after_failure(db, outreach, runtime, now: datetime, item: dict, error: Exception) -> tuple[str, str]:
    """Settle a send LinkedIn did not take, and say why. The service's own
    ladder (`jobs._after_failed_send`), except that nothing goes back to
    `approved`: `send_messages` would then send it later. An error that
    proves the message did not go settles `failed`, with the service's
    reactions to the account's state; anything else raises the
    `unknown_send` alert and settles `unknown`, which the sync resolves."""
    name = type(error).__name__
    queue_id, doc_id = item["id"], item["contact_doc_id"]
    context = {"queue_id": queue_id, "contact_doc_id": doc_id, "kind": queue.MANUAL, "error": name}
    not_sent = f"Not sent: LinkedIn answered {name}."

    if isinstance(error, unipile_errors.RateLimited):
        until = jobs._pause_start(now, runtime) + jobs._retry_after(error)
        queue.settle(db, queue_id, queue.FAILED, now=now, error=name)
        runtime.pause_sends(until, f"LinkedIn rate limit ({name})")
        return "send:failed", f"{not_sent} Sends are paused until {_local(until, outreach.tz)}."
    if isinstance(error, unipile_errors.AccountRestricted):
        queue.settle(db, queue_id, queue.FAILED, now=now, error=name)
        jobs._note_restriction(
            db, runtime, now, reason=f"LinkedIn restricted the account ({name})",
            question=(
                f"LinkedIn restricted the account while the contacts webapp sent {queue_id}. Every send is "
                "blocked until a human clears the block; the message was not sent."
            ),
            context=context,
        )
        return "send:failed", f"{not_sent} LinkedIn restricted the account, so every send is blocked."
    if isinstance(error, unipile_errors.AuthenticationError):
        until = now + jobs.DEFAULT_PAUSE
        queue.settle(db, queue_id, queue.FAILED, now=now, error=name)
        runtime.pause_sends(until, f"LinkedIn account disconnected ({name})")
        decisions.raise_alert(
            db, "disconnected", jobs._local_day(now, outreach),
            f"Unipile could not act for the LinkedIn account ({name}) -- it may need reconnecting. Sends are "
            f"paused until {until.isoformat()}; the contacts webapp's message was not sent.",
            {**context, "paused_until": until.isoformat()}, now,
        )
        return "send:failed", f"{not_sent} The account may need reconnecting in Unipile; sends are paused for an hour."
    if isinstance(error, (unipile_errors.BudgetExhausted, unipile_errors.CircuitOpen, unipile_errors.UnprocessableError,
                          unipile_errors.NotFound, unipile_errors.PermissionDenied)):
        queue.settle(db, queue_id, queue.FAILED, now=now, error=name)
        return "send:failed", f"{not_sent} {error}".strip()
    decisions.raise_alert(
        db, "unknown_send", queue_id,
        f"Sending {queue_id} to {doc_id} from the contacts webapp failed with {name}, so LinkedIn may or may not "
        "have delivered it. It will not be retried. Sync marks it sent if the message turns up in the history, "
        "and failed if none does within 48 hours.",
        context, now,
    )
    queue.settle(db, queue_id, queue.UNKNOWN, now=now, error=name)
    return "send:unknown", (
        f"LinkedIn did not answer clearly ({name}), so the message may have gone. It is not retried: "
        "Sync Messages marks it sent if it turns up, and failed after 48 hours."
    )


def send_now(app, doc_id: str, text: str, token: str, *, confirmed: bool, cancel_item: str = "") -> Outcome:
    """Every check, then the LinkedIn call and its records. Blocking: the
    route runs it in a thread."""
    outreach = app.state.outreach
    verdict = guards.validate_text(text, outreach)
    if not verdict.ok:
        return _refused(verdict.reason, verdict.detail)
    db = clients.firestore_client()
    snapshot = db.collection(ANALYSIS_COLLECTION).document(doc_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="No such contact.")
    contact = snapshot.to_dict() or {}
    if (contact.get("handling") or "").strip().lower() == "exclude":
        return _refused("contact:excluded", "Their handling is exclude: change it to send them a message.")
    runtime = runtime_state.RuntimeState(db, clock.utcnow)
    if runtime.writes_blocked():
        return _refused("state:writes_blocked", "LinkedIn writes are blocked for this account until a person clears the block.")
    paused = runtime.sends_paused_until()
    if paused is not None:
        return _refused("state:sends_paused", f"Sends are paused until {_local(paused, outreach.tz)}.")

    queue_id = f"manual:{doc_id}:{token}"
    now = clock.utcnow()
    items = queue.items_for_contact(db, doc_id)
    mine = next((item for item in items if item["id"] == queue_id), None)
    if mine is not None:
        return _refused("form:already_sent", f"This form was already sent: that message is {mine.get('status')}.")
    # "Cancel it and send mine" names a pending or approved item: it is
    # passed over here and cancelled just before the send, once every check
    # has passed, so a refused send leaves it queued.
    if cancel_item and not any(
        item["id"] == cancel_item and item.get("status") in (queue.PENDING, queue.APPROVED) for item in items
    ):
        cancel_item = ""
    open_item = next((item for item in items if item.get("status") in queue.OPEN and item["id"] != cancel_item), None)
    if open_item is not None:
        if open_item.get("status") in (queue.PENDING, queue.APPROVED):
            detail = f"A {open_item.get('kind')} message is queued for them ({open_item.get('status')})."
        else:
            detail = f"A message to them is {open_item.get('status')}: wait for it to settle, or for the next sync."
        return _refused("contact:open_item", detail, open_item=open_item)

    messages = jobs._stored_messages(db, doc_id)
    chat_id = _newest_chat_id(messages)
    provider_id = None if chat_id else _provider_id(db, doc_id, messages)
    if chat_id is None and provider_id is None:
        return _refused("contact:no_provider_id", "Their LinkedIn id is not known, so a conversation cannot be opened from here.")
    if not confirmed:
        warnings = _warnings(contact, messages, items, outreach.tz, now)
        if warnings:
            return Outcome(warnings=warnings)

    client = clients.unipile_client()
    try:
        try:
            sent_24h = jobs.messages_last_24h(db, client, runtime, outreach, now)
            client.budget.reconcile(message=sent_24h)
            if client.budget.remaining("message") <= 0:
                return _refused("budget", f"{sent_24h} messages went out in the last 24 hours: the daily limit is reached.")
            if chat_id is not None:
                verdict = jobs._chat_verdict(client, {"kind": queue.MANUAL, "contact_doc_id": doc_id, "chat_id": chat_id}, messages)
                if not verdict.ok:
                    return _refused(verdict.reason, verdict.detail)
        except Exception as error:
            logger.exception("checking the send to %s with LinkedIn failed", doc_id)
            name = type(error).__name__
            if isinstance(error, unipile_errors.AccountRestricted):
                jobs._note_restriction(
                    db, runtime, now, reason=f"LinkedIn restricted the account ({name})",
                    question=(
                        f"LinkedIn restricted the account while the contacts webapp checked a send to {doc_id}. "
                        "Every send is blocked until a human clears the block; nothing was sent."
                    ),
                    context={"contact_doc_id": doc_id, "error": name},
                )
                return _refused("state:writes_blocked", f"Not sent: LinkedIn restricted the account ({name}), so every send is blocked.")
            return _refused("linkedin:unavailable", f"Not sent: checking with LinkedIn failed ({name}).")

        now = clock.utcnow()
        if cancel_item and not queue.cancel(db, cancel_item, "cancelled in the contacts webapp to send a message by hand", now):
            return _refused("contact:open_item", "The queued message could not be cancelled: it changed meanwhile. Reload the page.")
        item, created = queue.start_manual(
            db, queue_id,
            {"contact_doc_id": doc_id, "text": text, "chat_id": chat_id, "provider_id": provider_id,
             "tags": [TAG], "created_by": OWNER},
            OWNER, now,
        )
        if not created:
            return _refused("form:already_sent", f"This form was already sent: that message is {item.get('status')}.")
        try:
            if chat_id is not None:
                message_id, opened = client.messaging.send_message(chat_id, text).message_id, None
            else:
                started = client.messaging.start_chat([provider_id], text)
                message_id, opened = started.message_id, started.chat_id
        except Exception as error:
            logger.warning("the send to %s failed: %r", doc_id, error)
            return _refused(*_after_failure(db, outreach, runtime, now, item, error))
        queue.settle(db, queue_id, queue.SENT, now=now, message_id=message_id, chat_id=opened)
    finally:
        client.close()

    stored_total = contact.get("sent_total")
    sent_total = (stored_total if isinstance(stored_total, int) else 0) + 1
    db.collection(ANALYSIS_COLLECTION).document(doc_id).set({"last_sent_date": now, "sent_total": sent_total}, merge=True)
    app.state.contacts.patch(doc_id, last_sent_date=now, sent_total=sent_total, activity_at=now, needs_answer=False)
    logger.info("sent %s to %s as %s", queue_id, doc_id, message_id)
    return Outcome(sent_at=now)


@router.post("/contacts/{doc_id}/send")
async def send(
    request: Request,
    doc_id: str,
    token: Annotated[str, Form(pattern=r"^[A-Za-z0-9_-]{16,64}$")],
    text: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    cancel_item: Annotated[str, Form()] = "",
):
    """Send the box's text. Sent: back to the Contact screen with a notice,
    and Sync Messages started as Home starts it. Not sent: the screen again
    with the text in the box and the reason under it."""
    outcome = await asyncio.to_thread(
        send_now, request.app, doc_id, text, token, confirmed=confirm == "1", cancel_item=cancel_item
    )
    if outcome.sent_at is None:
        return contact_screen.render_contact(
            request, doc_id,
            compose={"text": text, "refusal": outcome.refusal, "warnings": outcome.warnings, "open_item": outcome.open_item},
        )
    run = await routine.start(request.app, "sync-messages")
    sync = "busy" if run is None else "failed" if run.error else "started"
    notice = {"sent": _local(outcome.sent_at, request.app.state.outreach.tz, "%H:%M"), "sync": sync}
    return RedirectResponse(f"/contacts/{quote(doc_id, safe='')}?{urlencode(notice)}", status_code=303)
