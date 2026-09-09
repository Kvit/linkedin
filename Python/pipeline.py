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

from google import genai
from google.genai import types
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


PIPELINE_INSTRUCTIONS = """You classify LinkedIn conversations for a healthcare RCM technology company. Recovr by Pinnacle Services is AI-powered claim denial recovery software, sold to pathology practices, independent and physician-practice-owned medical laboratories, and revenue cycle management (RCM) and medical billing companies. Vitali, who runs the account, connects with people on LinkedIn and sends a short intro offering help with AI denial recovery and suggesting they stay in touch. Most people who answer are only being polite.

# Input
Two sections of plain text.

PROFILE: first, one line with the classification already on file -- industry, function and seniority, decided earlier from this same profile. It is given: use it to understand who is speaking, and never re-judge it. Then a mechanical dump of the contact's LinkedIn profile, which may contain field names, quotation marks, ids and repeated values; ignore those and read the professional content. Either part may be missing.

CONVERSATION: every message exchanged with the contact, oldest first, grouped into numbered conversations. Each line starts with the date it was sent. Lines marked "Me:" were written by Vitali. Lines marked "Them:" were written by the contact.

# Task
Decide where the contact stands in the sales pipeline from what THEY wrote. "Me:" lines and the profile are context for reading their words, never evidence on their own.

stage, exactly one of:
- "lead": they showed interest in denial recovery or in Recovr. They asked how it works, what it costs, or whether it fits their situation; described their own denial or billing problem; asked for a call, a demo, or material; or offered to introduce the person who handles this at their organization.
- "prospect": the default. The contact was chosen for outreach by their industry and seniority, and nothing they wrote changes that. A courtesy reply is "prospect": thanks for connecting, happy to stay in touch, will keep it in mind, sounds good, appreciate the offer. Use it whenever their words neither show interest, nor decline, nor reveal that they are not a buyer.
- "reject": they declined. Not interested, no need, already covered by another vendor or an in-house team, asked not to be contacted, or gave a clear no to a specific ask.
- "not_relevant": the conversation shows they are not a buyer at all. They are recruiting, job-seeking, selling their own product or service, asking for career advice, or their messages have nothing to do with their organization's billing. Judge this from what they wrote and the role the profile shows, never from the industry or size of their employer: a hospital executive who asks about the product is a "lead".
- "unknown": their text cannot be read at all -- a language you do not understand, garbled characters, an empty automated reply. Text you can read but that carries no signal is "prospect", not "unknown".

# Rules
- The latest signal wins. Someone who was interested and later said no is "reject"; someone who declined and later asked for a demo is "lead".
- Politeness is not interest. "Sounds good", "let's stay in touch" and "thanks for the offer" are "prospect".
- When in doubt, "prospect": it is the default, and the other four each need a reason you can quote.
- A question about Recovr, denials or pricing is interest even when hedged.
- An offer to forward the message or introduce a colleague is "lead".
- Never infer a stage from the profile or the classification on file. Every conversation you receive has at least one "Them:" line; classify from those.

# Output
Return JSON with exactly the keys stage and reason. reason is one sentence in English naming the words of theirs that decided it."""


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


def plan_pipeline(
    transcripts, stored, *, reprocess_all=False, force=frozenset()
) -> tuple[list[str], list[str], dict[str, int]]:
    """Decide who gets the silent rule, who goes to Gemini, and who is left alone.

    Pure on purpose, like `functions.plan_forward_writes`: the engine attaches
    `SERVER_TIMESTAMP` and does the writes, so every rule here is pinned by a
    test that needs no Firestore.

    Args:
        transcripts: `build_transcripts` output.
        stored: contact id to its current ``pipeline_stage`` and
            ``pipeline_message_id`` (either may be absent), for every contact
            that has an `analysis` document. A contact absent here has none and
            must never be minted.
        reprocess_all: queue every contact with an inbound message, whatever is
            stored -- for prompt or taxonomy changes.
        force: contacts to queue even when nothing changed.

    Returns:
        tuple: ``(silent, queue, tally)``. ``silent`` are contacts to mark
        ``prospect`` by rule. ``queue`` are contacts for Gemini, newest inbound
        first. ``tally`` counts ``silent``, ``queued``, ``unchanged``, ``stale``
        (Gemini-classified, but their inbound message is no longer stored),
        ``missing`` (no `analysis` document) and ``not_found`` (forced, but no
        messages at all).
    """
    silent: list[str] = []
    queue: list[str] = []
    tally = {
        "silent": 0, "queued": 0, "unchanged": 0, "stale": 0, "missing": 0,
        "not_found": len(set(force) - transcripts.keys()),
    }

    for contact, entry in transcripts.items():
        current = stored.get(contact)
        if current is None:
            tally["missing"] += 1
            continue

        if entry["inbound_total"] == 0:
            if current.get("pipeline_message_id"):
                # Gemini read a message that is no longer in the window. The
                # judgment stands; a reject turned prospect would be messaged.
                tally["stale"] += 1
            elif current.get("pipeline_stage") == SILENT_STAGE:
                tally["unchanged"] += 1
            else:
                silent.append(contact)
            continue

        if (
            reprocess_all
            or contact in force
            or current.get("pipeline_message_id") != entry["newest_inbound_id"]
        ):
            queue.append(contact)
        else:
            tally["unchanged"] += 1

    queue.sort(key=lambda contact: (transcripts[contact]["newest_inbound_date"], contact),
               reverse=True)
    tally["silent"] = len(silent)
    tally["queued"] = len(queue)
    return silent, queue, tally


# --- Gemini -------------------------------------------------------------------

#: gemini-3.8-flash defaults to medium, cannot turn thinking off, and the docs
#: recommend low for classification -- so low is the default, and the level is
#: a knob on every entry point so a run's rulings can be checked at another.
DEFAULT_THINKING_LEVEL = "low"
THINKING_LEVELS = ("low", "medium", "high")


def generation_config(thinking_level: str = DEFAULT_THINKING_LEVEL) -> types.GenerateContentConfig:
    """The call configuration. Holds no connection, so it is cheap to build per call.

    Temperature is left at the default 1.0 -- the Gemini 3 docs warn that
    lowering it degrades output. No tools, so no automatic-function-calling
    config. An unknown level is rejected here so a typo fails once, up front.
    """
    if thinking_level not in THINKING_LEVELS:
        raise ValueError(
            f"thinking_level must be one of {THINKING_LEVELS}, not {thinking_level!r}"
        )
    return types.GenerateContentConfig(
        system_instruction=PIPELINE_INSTRUCTIONS,
        response_mime_type="application/json",
        response_schema=PipelineAnalysis,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel(thinking_level.upper())
        ),
    )


def gemini_client() -> genai.Client:
    """A client with retries on, which the SDK leaves off unless asked.

    An all-default `HttpRetryOptions()` takes the SDK's own values: five
    attempts, exponential backoff from one second to sixty with jitter, on
    408, 429 and 5xx. Sixty seconds per request keeps one hung connection from
    stalling a run of six hundred. The async HTTP pool is built here and binds
    to the first event loop it runs on, so make one client per `asyncio.run`.
    """
    return genai.Client(
        http_options=types.HttpOptions(
            timeout=60_000,
            retry_options=types.HttpRetryOptions(),
        )
    )


def build_prompt(contact: dict, transcript: str) -> str:
    """The user turn: what is on file, then the profile, then the conversation.

    Industry, function and seniority are shown as given so the model reads the
    contact's words knowing who they are, and the prompt says they are not to
    be re-judged -- the output schema has no field for them. The summary is
    passed whole, as `analysis.ipynb` passes it; the largest is 35 KB, well
    inside the model's window.
    """
    on_file = "; ".join(
        f"{name} {contact[name]}" for name in ON_FILE_FIELDS if contact.get(name)
    )
    header = (
        f"On file, already classified and not to be re-judged: {on_file}"
        if on_file
        else NOT_CLASSIFIED
    )
    profile = " ".join((contact.get("summary") or "").split()) or NO_PROFILE
    return f"PROFILE\n{header}\n{profile}\n\nCONVERSATION\n{transcript}"


async def classify_conversation(
    client, contact: dict, transcript: str, *, thinking_level: str = DEFAULT_THINKING_LEVEL
) -> PipelineAnalysis:
    """One Gemini call. Raises on any failure, including a label outside the vocabulary.

    `response.parsed` is the SDK's validated instance of the schema. It is None
    when the model's JSON did not satisfy the schema -- the SDK swallows the
    validation error -- or when the response was cut short, so None is raised
    here with the finish reason, and the caller logs it against the contact. A
    failed contact is written nowhere and is queued again on the next run.

    No length cap on `reason` in the schema: a validation failure would leave
    the key unchanged, and a verbose model would then retry the same contact
    forever. The prompt asks for one sentence and the engine stores what comes.
    """
    response = await client.aio.models.generate_content(
        model=MODEL,
        contents=build_prompt(contact, transcript),
        config=generation_config(thinking_level),
    )
    if response.parsed is None:
        finish = response.candidates[0].finish_reason if response.candidates else None
        raise ValueError(
            f"no parseable stage (finish_reason={finish}): {(response.text or '')[:200]!r}"
        )
    return response.parsed
