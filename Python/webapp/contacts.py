"""The Contact screen: one contact whole, and the five dropdowns that change
it.

Everything shown comes from `linkedinmcp.contacts`, whose readers build
explicit whitelists -- `email*` and `phone*` never reach a page -- and
give every contact date as an ISO string in the service's time zone.
`get_conversation` imports `pipeline` (about 0.8 s, once per process) for
the transcript. `clients.firestore_client` is called through its module,
so a test can replace it.

The headline comes from the frame, not from `get_contact`: that reader
reads only `extracted.occupation`, which LinkedIn Helper documents lack
(see `projection`'s docstring), so the Contact screen shows what the list
shows. The connection date is read from `fetch_queue` each time, as the
service's `contact_report` reads it, else taken from the frame, which also
holds LinkedIn Helper's date.

The dropdowns write `analysis/{doc_id}` at once, always with `merge=True`
and only when the document exists: it holds the only copy of some
contacts' names and emails, and a contact is never created here. Their
values are the classifiers' own vocabulary: `profiles.Industry`,
`Function` and `Seniority`, `pipeline.Stage`, and for handling `none` plus
`functions.HANDLING_HOLDS`.

- Handling is written as the service's `set_handling` and `clear_handling`
  write it: `exclude` or `manual` also cancels the contact's `pending` and
  `approved` queue items, and the page says how many; `none` clears it.
- Industry, function, seniority and stage add their field name to
  `hand_set` and set `hand_set_at`, so the classifiers leave the value
  alone (`profiles.without_hand_set`, `pipeline.plan_pipeline`). A stage
  also gets `pipeline_reason` "set by hand", `pipeline_classified_at` and,
  when the contact has written to us, `pipeline_message_id` of their newest
  readable message, so it holds until a newer reply, as a classified stage
  does.
- Release takes a field name out of `hand_set` without changing the value,
  so the classifiers may change it again.

After a write the frame row is patched, so the list and the Home counts
show it at once, and the page reloads with a notice.

The message box above the conversation posts to `compose.py`, which
renders this screen again through `render_contact` when a send is refused,
so the text stays in the box.
"""

import re
import secrets
from datetime import date
from typing import Annotated, get_args
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from google.cloud import firestore

import functions
import pipeline
import profiles
from linkedinmcp import clients, clock, contacts as reads, queue
from webapp import projection, render

router = APIRouter()

ANALYSIS_COLLECTION = "analysis"
FETCH_COLLECTION = "fetch_queue"
HAND_REASON = "set by hand"

#: The dropdowns, in page order: the `analysis` field, its label, and the
#: values it offers.
CHOICES: dict[str, tuple[str, tuple[str, ...]]] = {
    "handling": ("Handling", (projection.NONE, *sorted(functions.HANDLING_HOLDS))),
    "industry": ("Industry", get_args(profiles.Industry)),
    "function": ("Function", get_args(profiles.Function)),
    "seniority": ("Seniority", get_args(profiles.Seniority)),
    "pipeline_stage": ("Stage", get_args(pipeline.Stage)),
}


def _hand_set(stored: dict) -> list[str]:
    """The field names a person set by hand, as stored."""
    value = stored.get("hand_set")
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def _existing(db, doc_id: str):
    """The contact's `analysis` reference and its stored fields; 404 when
    there is no such document, so nothing is ever created."""
    reference = db.collection(ANALYSIS_COLLECTION).document(doc_id)
    snapshot = reference.get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="No such contact.")
    return reference, snapshot.to_dict() or {}


def _back(doc_id: str, **notice) -> RedirectResponse:
    """Back to the Contact screen, carrying what the write did."""
    return RedirectResponse(f"/contacts/{quote(doc_id, safe='')}?{urlencode(notice)}", status_code=303)


#: What the Contact screen says about the sync a send started (`compose.py`).
SYNC_NOTES = {
    "started": "Sync Messages started: the message is stored when it finishes, as Home shows.",
    "busy": "A run is already going on Home: press Sync Messages when it ends, to store the message.",
    "failed": "Sync Messages could not be started, as Home shows: press it there to store the message.",
}


def _notice(params) -> str:
    """The line a write leaves on the reloaded page, read from its redirect."""
    if re.fullmatch(r"\d{2}:\d{2}", params.get("sent", "")):
        return f"Sent at {params['sent']}. {SYNC_NOTES.get(params.get('sync'), '')}".strip()
    if (name := params.get("saved")) in CHOICES:
        cancelled = params.get("cancelled", "")
        return f"{CHOICES[name][0]} saved." + (f" Queued messages cancelled: {cancelled}." if cancelled.isdigit() else "")
    if (name := params.get("released")) in CHOICES:
        return f"{CHOICES[name][0]} released: the classifiers may change it again."
    return ""


#: The two kinds of line in `pipeline.build_transcripts`' text: a
#: conversation's header, and one message with its day and side.
_PART = re.compile(r"--- conversation (\d+) ---")
_MESSAGE = re.compile(r"(\d{4}-\d{2}-\d{2}) (Me|Them): (.*)")


def thread(conversation: dict | None) -> list[dict]:
    """`get_conversation`'s transcript as conversations of messages, for the
    screen to lay out: `[{"number", "messages": [{"day", "side", "text"}]}]`,
    `side` `me` or `them`. The text is parsed rather than the messages read
    again, so the screen shows exactly what the classifiers read, one
    whitespace-collapsed paragraph per message.

    A transcript cut to its newest characters (`truncated`) can start
    mid-line: that first line is dropped. Any other line that is not a
    message is kept as `side` `None`, never lost."""
    if conversation is None:
        return []
    parts: list[dict] = []
    for index, line in enumerate(conversation["transcript"].split("\n")):
        if header := _PART.fullmatch(line):
            parts.append({"number": int(header[1]), "messages": []})
            continue
        if not parts:
            parts.append({"number": None, "messages": []})
        if found := _MESSAGE.fullmatch(line):
            day, side, text = found.groups()
            parts[-1]["messages"].append({"day": date.fromisoformat(day), "side": side.lower(), "text": text})
        elif not (index == 0 and conversation.get("truncated")):
            parts[-1]["messages"].append({"day": None, "side": None, "text": line})
    return [part for part in parts if part["messages"]]


def unsynced_sends(db, doc_id: str) -> list[dict]:
    """The messages sent from the message box that the sync has not stored
    yet, oldest first: the contact's `manual` queue items that are `sent`
    with a `message_id` not in `messages`, or still `sending` or `unknown`."""
    items = [
        item for item in queue.items_for_contact(db, doc_id)
        if item.get("kind") == queue.MANUAL and item.get("status") in (queue.SENT, queue.SENDING, queue.UNKNOWN)
    ]
    if not items:
        return []
    stored = {document.id for document in pipeline.load_messages(db.collection(pipeline.MESSAGES_COLLECTION), [doc_id])}
    return [item for item in reversed(items) if item.get("status") != queue.SENT or item.get("message_id") not in stored]


@router.get("/contacts/{doc_id}")
def contact_screen(request: Request, doc_id: str):
    return render_contact(request, doc_id, notice=_notice(request.query_params))


def render_contact(request: Request, doc_id: str, *, notice: str = "", compose: dict | None = None):
    """The Contact screen. `compose` is the message box's state after a
    refused send: its `text`, and `refusal` (reason and sentence),
    `warnings` or the `open_item` in the way. Every render carries a new
    `token`, so each form sends at most one message."""
    db = clients.firestore_client()
    contact = reads.get_contact(db, request.app.state.outreach, doc_id, full=True)
    if contact is None:
        raise HTTPException(status_code=404, detail="No such contact.")
    listed = projection.contact_row(request.app.state.contacts.frame, doc_id)
    if listed is not None and listed["headline"]:
        contact["headline"] = listed["headline"]
    conversation = reads.get_conversation(db, doc_id)
    fetch = db.collection(FETCH_COLLECTION).document(doc_id).get()
    connected_at = (fetch.to_dict() or {}).get("connected_at") if fetch.exists else None
    if connected_at is None and listed is not None:
        connected_at = listed["connected_at"]
    current = {
        "handling": (contact.get("handling") or "").strip().lower() or projection.NONE,
        "industry": contact.get("industry") or projection.NONE,
        "function": contact.get("function") or projection.NONE,
        "seniority": contact.get("seniority") or projection.NONE,
        "pipeline_stage": contact.get("stage") or projection.NONE,
    }
    by_hand = set(listed["hand_set"] or ()) if listed is not None else set()
    fields = [
        {"name": name, "label": label, "options": options, "current": current[name], "by_hand": name in by_hand}
        for name, (label, options) in CHOICES.items()
    ]
    parts = thread(conversation)
    return render.templates.TemplateResponse(
        request, "contact.html",
        render.page_context(
            request, contact=contact, conversation=conversation, parts=parts,
            sides=[message["side"] for part in parts for message in part["messages"]],
            connected_at=connected_at, fields=fields, notice=notice,
            compose={"text": "", "refusal": None, "warnings": [], "open_item": None, **(compose or {}),
                     "token": secrets.token_urlsafe(16)},
            max_chars=request.app.state.outreach.message_max_chars, unsynced=unsynced_sends(db, doc_id),
        ),
    )


@router.post("/contacts/{doc_id}/field")
def set_field(request: Request, doc_id: str, name: Annotated[str, Form()], value: Annotated[str, Form()]):
    """One dropdown's write. A field or value outside `CHOICES` is 400 and
    writes nothing."""
    if name not in CHOICES or value not in CHOICES[name][1]:
        raise HTTPException(status_code=400, detail="That field does not take that value.")
    db = clients.firestore_client()
    reference, stored = _existing(db, doc_id)
    contacts = request.app.state.contacts
    if name == "handling":
        hold = None if value == projection.NONE else value
        reference.set({"handling": hold}, merge=True)
        contacts.patch(doc_id, handling=hold)
        if hold is None:
            return _back(doc_id, saved=name)
        cancelled = queue.cancel_for_contact(db, doc_id, f"handling set to {hold}", clock.utcnow())
        return _back(doc_id, saved=name, cancelled=cancelled)

    hand_set = _hand_set(stored)
    if name not in hand_set:
        hand_set.append(name)
    update = {name: value, "hand_set": hand_set, "hand_set_at": firestore.SERVER_TIMESTAMP}
    if name == "pipeline_stage":
        update |= {"pipeline_reason": HAND_REASON, "pipeline_classified_at": firestore.SERVER_TIMESTAMP}
        documents = pipeline.load_messages(db.collection(pipeline.MESSAGES_COLLECTION), [doc_id])
        newest = (pipeline.build_transcripts(documents).get(doc_id) or {}).get("newest_inbound_id")
        if newest:
            update["pipeline_message_id"] = newest
    reference.set(update, merge=True)
    contacts.patch(
        doc_id,
        **{key: item for key, item in update.items() if key in projection.SCHEMA and item is not firestore.SERVER_TIMESTAMP},
    )
    return _back(doc_id, saved=name)


@router.post("/contacts/{doc_id}/release")
def release_field(request: Request, doc_id: str, name: Annotated[str, Form()]):
    """Take `name` out of `hand_set`, leaving its value as it is."""
    if name not in CHOICES or name == "handling":
        raise HTTPException(status_code=400, detail="Only a classification or a stage is set by hand.")
    db = clients.firestore_client()
    reference, stored = _existing(db, doc_id)
    hand_set = [field for field in _hand_set(stored) if field != name]
    reference.set({"hand_set": hand_set, "hand_set_at": firestore.SERVER_TIMESTAMP}, merge=True)
    request.app.state.contacts.patch(doc_id, hand_set=hand_set)
    return _back(doc_id, released=name)
