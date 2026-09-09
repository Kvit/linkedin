"""Sales-pipeline classification of LinkedIn conversations.

Three layers, one module, so a batch over every contact and a one-contact call
from a webhook share every rule:

- pure: `build_transcripts`, `plan_pipeline`, `build_prompt` -- unit-tested
  with plain dicts in tests/test_pipeline.py;
- Gemini: `gemini_client`, `classify_conversation`, the prompt and the
  Literal-typed schema -- live-tested in test_gemini_pipeline.py, which
  imports them rather than copying them;
- Firestore and the engine: `run_pipeline` for everyone or a list of contacts,
  `classify_contact` for one. pipeline-classify.py is an argparse wrapper
  around the first; a script reacting to a new message awaits the second.

Nothing here opens a connection at import: Firestore documents arrive as an
iterable, and the Firestore and Gemini clients arrive as parameters.
`gemini_client` is a function for that reason.

`build_transcripts` mirrors the exclusions `functions.contact_message_stats`
makes -- undated messages, and any whose `is_sender` is neither 0 nor 1 -- and
adds the three that only matter when the text is read rather than counted:
events, deletions and attachment-only messages. Of 7,126 stored messages those
are 4, 6 and 15.
"""

from typing import Literal

from pydantic import BaseModel, Field

MODEL = "gemini-3.8-flash"

#: One label per contact. The vocabulary lives in the schema Gemini must
#: satisfy, so an out-of-vocabulary label fails validation instead of landing
#: in Firestore.
Stage = Literal["prospect", "lead", "reject", "not_relevant", "unknown"]

#: What the silent rule writes. A contact we have written to who has never
#: answered is a prospect by definition; a model call would have nothing to read.
SILENT_STAGE = "prospect"
SILENT_REASON = "messaged, no reply yet"

#: Stands in for `summary` on the 26 of 617 inbound contacts who have none.
NO_PROFILE = "(no profile on file)"
#: Stands in for the on-file classification when a contact has none yet.
NOT_CLASSIFIED = "On file: not yet classified"

#: The classifications `analysis.ipynb` already made. Shown to the model as
#: given facts; never asked for back.
ON_FILE_FIELDS = ("industry", "function", "seniority")


class PipelineAnalysis(BaseModel):
    stage: Stage = Field(
        description="Where the contact stands, judged from what they wrote. "
        "prospect is the default and needs no reason beyond a courtesy reply."
    )
    reason: str = Field(
        description="One sentence in English naming the words of theirs that decided it."
    )


# --- pure ---------------------------------------------------------------------


def build_transcripts(documents) -> dict[str, dict]:
    """Per-contact conversation text and newest inbound message, keyed by contact id.

    The newest *inbound* message is the incremental key, not the newest message:
    our own follow-ups must not re-bill Gemini for a contact who said nothing
    new. It is computed here rather than read from `last_reply_message_id`,
    which counts only inbound-after-our-opening and would never key the 161
    contacts who wrote first.

    Args:
        documents: Streamed `messages` documents exposing ``.id`` and
            ``.to_dict()``, projected to chat_id, contact_doc_id, is_sender,
            timestamp, text, is_event and deleted. A message is left out when it
            has no contact, no timestamp, an ``is_sender`` other than 0 or 1,
            ``is_event == 1``, ``deleted == 1`` or no text.

    Returns:
        dict[str, dict]: ``contact_doc_id`` to ``transcript``, ``inbound_total``,
        ``newest_inbound_id`` and ``newest_inbound_date``. The last two are None
        for a contact who has never written to us. A contact with no readable
        message at all is absent.
    """
    chats: dict[str, dict[str, list]] = {}
    for document in documents:
        body = document.to_dict() or {}
        contact = body.get("contact_doc_id")
        timestamp = body.get("timestamp")
        is_sender = body.get("is_sender")
        text = " ".join((body.get("text") or "").split())
        if (
            not contact
            or timestamp is None
            or is_sender not in (0, 1)
            or body.get("is_event") == 1
            or body.get("deleted") == 1
            or not text
        ):
            continue
        # A message with no chat is its own conversation -- the same rule as
        # `contact_message_stats`, for the same reason.
        chat = body.get("chat_id") or document.id
        chats.setdefault(contact, {}).setdefault(chat, []).append(
            (timestamp, document.id, is_sender, text)
        )

    result: dict[str, dict] = {}
    for contact, by_chat in chats.items():
        # Conversations in the order they started, messages oldest first, the
        # id breaking a timestamp tie so the text is identical on every run.
        ordered = sorted(by_chat.values(), key=lambda lines: min(line[:2] for line in lines))
        sections: list[str] = []
        inbound: list[tuple] = []
        for number, lines in enumerate(ordered, 1):
            sections.append(f"--- conversation {number} ---")
            for timestamp, message_id, is_sender, text in sorted(lines, key=lambda line: line[:2]):
                who = "Me" if is_sender == 1 else "Them"
                sections.append(f"{timestamp:%Y-%m-%d} {who}: {text}")
                if is_sender == 0:
                    inbound.append((timestamp, message_id))
        newest = max(inbound) if inbound else (None, None)
        result[contact] = {
            "transcript": "\n".join(sections),
            "inbound_total": len(inbound),
            "newest_inbound_id": newest[1],
            "newest_inbound_date": newest[0],
        }
    return result


def plan_pipeline(transcripts, stored, *, reprocess_all=False, force=frozenset()):
    raise NotImplementedError  # Task 2


def build_prompt(contact, transcript) -> str:
    raise NotImplementedError  # Task 3
