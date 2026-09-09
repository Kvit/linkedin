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

import asyncio
import itertools
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from google import genai
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.genai import types
from pydantic import BaseModel, Field

MODEL = "gemini-3.8-flash"

#: One label per contact. The vocabulary lives in the schema Gemini must
#: satisfy, so an out-of-vocabulary label fails validation instead of landing
#: in Firestore.
Stage = Literal["prospect", "lead", "soft_no", "reject", "not_relevant", "unknown"]

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
- "soft_no": a circumstance stops them, not a lack of interest. They never say they do not want it; something outside their opinion of the product is in the way, and it can change: they are between roles or their position was eliminated; it is not their decision, or they are "not in a position to suggest tools like yours"; their workload, availability or budget will not allow it now; they have stepped away from this work "at present". These contacts still receive product updates.
- "reject": they say they do not want it. Not interested, no need, no thank you, already covered by another vendor or an in-house team and not looking to change, asked not to be contacted, or a flat refusal such as "not looking". A softening phrase attached to that -- "at this time", "right now", "currently", "for now" -- is politeness, not a reason, and does not make it a "soft_no".
- "not_relevant": the conversation shows they are not a buyer at all, in any timeframe. They are recruiting, selling their own product or service, asking for career advice, or their work has nothing to do with healthcare billing and never did. Judge this from what they wrote and the role the profile shows, never from the industry or size of their employer: a hospital executive who asks about the product is a "lead".
- "unknown": their text cannot be read at all -- a language you do not understand, garbled characters, an empty automated reply. Text you can read but that carries no signal is "prospect", not "unknown".

# Rules
- The latest signal wins. Someone who was interested and later said no is "reject"; someone who declined and later asked for a demo is "lead".
- Politeness is not interest. "Sounds good", "let's stay in touch" and "thanks for the offer" are "prospect".
- Separate the two kinds of no by what the words are about. A no about their *interest* -- "not interested", "no need", "we're all set" -- is "reject", however politely it is hedged. A no about their *circumstances* -- they have left the role, it is not their decision, their workload or budget will not allow it -- is "soft_no", because the obstacle can expire and they never said the product was unwanted.
- Someone who has left this work but may return -- between jobs, "not in healthcare at present" -- is "soft_no", never "not_relevant". Reserve "not_relevant" for people whose working life has nothing to do with what we sell.
- When in doubt, "prospect": it is the default, and the other five each need a reason you can quote.
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
    lowering it degrades output. No tools are passed, but automatic function
    calling is disabled all the same: left at its default, the SDK logs a
    once-per-process advisory about using it on the async client. An unknown
    level is rejected here so a typo fails once, up front.
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
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
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


# --- Firestore ----------------------------------------------------------------

MESSAGES_COLLECTION = "messages"
ANALYSIS_COLLECTION = "analysis"

#: Firestore commits at most 500 operations per batch; one chunk is one round
#: trip for `get_all` and one commit for the silent rule.
PAGE_SIZE = 250

#: `select()` leaves absent fields out of `to_dict()`, so every read is `.get()`.
MESSAGE_FIELDS = ["chat_id", "contact_doc_id", "is_sender", "timestamp", "text", "is_event", "deleted"]
ANALYSIS_FIELDS = ["summary", "profileUrl", "pipeline_stage", "pipeline_message_id", *ON_FILE_FIELDS]

logger = logging.getLogger("pipeline")


def load_messages(messages_ref, contacts=None) -> list:
    """Message documents, projected to what `build_transcripts` reads.

    The whole collection when `contacts` is None -- 7k documents, two seconds.
    One equality query per contact otherwise, which is what lets a caller
    reacting to a single new message avoid the full stream. `contact_doc_id`
    is a single field, so Firestore's automatic index serves it.
    """
    query = messages_ref.select(MESSAGE_FIELDS)
    if contacts is None:
        return list(query.stream())
    documents: list = []
    for contact in contacts:
        documents.extend(
            query.where(filter=FieldFilter("contact_doc_id", "==", contact)).stream()
        )
    return documents


def load_contacts(db, analysis_ref, contacts) -> dict[str, dict]:
    """The `analysis` documents for these contacts, by id; absent ones are absent.

    `get_all` on the contacts that have messages, not a stream of the
    collection: `analysis` holds 28k documents whose summaries run to 35 KB.
    `snapshot.exists` doubles as the never-mint check the planner relies on.
    """
    found: dict[str, dict] = {}
    for chunk in itertools.batched(sorted(contacts), PAGE_SIZE):
        references = [analysis_ref.document(contact) for contact in chunk]
        for snapshot in db.get_all(references, field_paths=ANALYSIS_FIELDS):
            if snapshot.exists:
                found[snapshot.id] = snapshot.to_dict() or {}
    return found


def mark_silent(db, analysis_ref, contacts, *, dry_run) -> None:
    """Write the rule for contacts who were messaged and never answered.

    Merged, never set: `analysis` is the only copy of some contacts' names
    and emails. No `pipeline_message_id` -- there is no inbound message to key
    on, and its absence is what tells `plan_pipeline` the rule wrote this.
    """
    for chunk in itertools.batched(contacts, PAGE_SIZE):
        batch = db.batch()
        for contact in chunk:
            batch.set(
                analysis_ref.document(contact),
                {
                    "pipeline_stage": SILENT_STAGE,
                    "pipeline_reason": SILENT_REASON,
                    "pipeline_classified_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        if not dry_run:
            batch.commit()


def write_stage(analysis_ref, contact, result: PipelineAnalysis, message_id, *, dry_run) -> None:
    """Merge one Gemini result onto the contact, keyed to the message it read."""
    if dry_run:
        return
    analysis_ref.document(contact).set(
        {
            "pipeline_stage": result.stage,
            "pipeline_reason": result.reason,
            "pipeline_classified_at": firestore.SERVER_TIMESTAMP,
            "pipeline_message_id": message_id,
        },
        merge=True,
    )


# --- the engine ---------------------------------------------------------------


@dataclass
class PipelineRun:
    """What one run did.

    `rows` holds one review row per Gemini-classified contact -- what
    pipeline-classify.py writes to the CSV, and what a one-contact caller reads
    the stage from. `tally` counts everything else, including what was left
    alone and why.
    """

    tally: dict[str, int]
    rows: list[dict] = field(default_factory=list)
    failed: int = 0

    @property
    def stages(self) -> Counter:
        return Counter(row["stage"] for row in self.rows)


async def run_pipeline(
    db, client, *, contacts=None, force=False, reprocess_all=False, limit=None,
    dry_run=False, concurrency=4, thinking_level=DEFAULT_THINKING_LEVEL,
) -> PipelineRun:
    """Classify every contact, or only `contacts`.

    The one engine behind pipeline-classify.py and `classify_contact`, so the
    batch and the one-contact call cannot drift apart. With `contacts` given,
    only their messages are read. `force` queues them whatever is stored;
    without it a contact whose newest inbound message was already classified
    costs no call and no write.

    Gemini calls run `concurrency` at a time through `client.aio` -- the SDK's
    pattern for hundreds of independent requests. The three bulk reads and the
    silent-rule batch happen before the gather and stay synchronous; each
    result is written as it lands, from a thread so the synchronous Firestore
    client does not block the loop, because 600 calls take minutes and an
    interrupted run should keep what it paid for. A failed call -- after the
    SDK's own retries -- writes nothing, so the contact's stored key is
    unchanged and the next run queues it again.

    Args:
        db: The Firestore client.
        client: The Gemini client, from `gemini_client()`; one per event loop.
        contacts: Restrict to these contact document ids. None means everyone.
        force: With `contacts`, classify them whatever is stored.
        reprocess_all: Classify every contact with an inbound message again.
        limit: At most this many Gemini calls, newest conversations first.
        dry_run: Read and call Gemini, but write nothing to Firestore.
        concurrency: Gemini calls in flight at once. Many 429 failures mean
            the account's tier is lower than this; use 1.
        thinking_level: "low", "medium" or "high". Recorded on every review
            row, so two runs at different levels can be compared.

    Returns:
        PipelineRun: see the class.
    """
    generation_config(thinking_level)  # a bad level fails here, before any read
    messages_ref = db.collection(MESSAGES_COLLECTION)
    analysis_ref = db.collection(ANALYSIS_COLLECTION)

    documents = load_messages(messages_ref, contacts)
    transcripts = build_transcripts(documents)
    stored = load_contacts(db, analysis_ref, transcripts)
    forced = frozenset(contacts) if (contacts is not None and force) else frozenset()
    silent, queue, tally = plan_pipeline(
        transcripts, stored, reprocess_all=reprocess_all, force=forced
    )
    if contacts is not None:
        tally["not_found"] = len(set(contacts) - transcripts.keys())
    if limit is not None:
        queue = queue[:limit]
    tally["messages"] = len(documents)
    tally["contacts"] = len(transcripts)
    tally["with_inbound"] = sum(1 for entry in transcripts.values() if entry["inbound_total"])
    tally["called"] = len(queue)

    mark_silent(db, analysis_ref, silent, dry_run=dry_run)

    run = PipelineRun(tally=tally)
    semaphore = asyncio.Semaphore(concurrency)
    finished = 0
    started = time.monotonic()

    async def one(contact):
        nonlocal finished
        entry, current = transcripts[contact], stored[contact]
        row = None
        async with semaphore:
            try:
                result = await classify_conversation(
                    client, current, entry["transcript"], thinking_level=thinking_level
                )
                await asyncio.to_thread(
                    write_stage, analysis_ref, contact, result, entry["newest_inbound_id"],
                    dry_run=dry_run,
                )
                row = {
                    "doc_id": contact,
                    "profile_url": current.get("profileUrl") or "",
                    "previous_stage": current.get("pipeline_stage") or "",
                    "stage": result.stage,
                    "reason": result.reason,
                    "thinking_level": thinking_level,
                    "inbound_total": entry["inbound_total"],
                    "last_inbound": f"{entry['newest_inbound_date']:%Y-%m-%d}",
                    "transcript": entry["transcript"],
                }
            except Exception as error:
                run.failed += 1
                logger.warning("  [!] %s: %s: %s", contact, type(error).__name__, error)
        finished += 1
        if finished % 50 == 0 or finished == len(queue):
            logger.info("  %s | classified %d/%d  (%.0fs)", time.strftime("%H:%M:%S"),
                        finished, len(queue), time.monotonic() - started)
        return row

    outcomes = await asyncio.gather(*(one(contact) for contact in queue))
    run.rows = [row for row in outcomes if row is not None]
    return run


async def classify_contact(
    db, client, contact: str, *, force=False, dry_run=False,
    thinking_level=DEFAULT_THINKING_LEVEL,
) -> PipelineRun:
    """One contact, for a caller reacting to a new message.

    Idempotent unless forced: a contact whose newest inbound message was
    already classified costs no Gemini call and no write, so this is safe to
    await on every event -- including our own outbound messages, which the
    provider's webhooks also deliver. After a call, `run.rows[0]["stage"]` is
    the new stage; `run.tally["silent"] == 1` means the rule applied (they
    have never written to us); all zeros means nothing changed.

        from google.cloud import firestore
        from pipeline import classify_contact, gemini_client

        db = firestore.Client(project="vk-linkedin", database="linkedin")
        run = await classify_contact(db, gemini_client(), "some-linkedin-slug")

    From synchronous code, `asyncio.run(classify_contact(...))` -- once per
    process and per client. Several contacts from synchronous code is one
    `run_pipeline(db, client, contacts=[...])`, not a loop of these.
    """
    return await run_pipeline(
        db, client, contacts=[contact], force=force, dry_run=dry_run, concurrency=1,
        thinking_level=thinking_level,
    )
