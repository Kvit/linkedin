"""
This module contains common functions used throughout the application.
"""

import json  # for testing purposes
import os  # for testing purposes
from datetime import UTC, datetime, timedelta

from google.cloud.firestore_v1.base_query import FieldFilter

SKIP_REASONS = (
    "unclassified",
    "off_target",
    "handling",
    "already_messaged",
    "intro_already_sent",
    "existing_chat",
)

#: Values of the hand-maintained ``handling`` field that hold a contact back.
#: "exclude" is a decision never to write to them; "manual" reserves them for a
#: personal message, which a templated intro arriving first would pre-empt.
HANDLING_HOLDS = frozenset({"exclude", "manual"})


def get_linkedin_id(profile) -> str:
    """
    Get the profile from the JSON file.

    Returns:
        str: LinkedIn profile id
    """

    # get "externalIds" from the profile
    external_ids = profile.get("externalIds")

    # extract exteral id for linkedin
    linkedin = next(
        (person_id for person_id in external_ids if person_id["type"] == "member-id"),
        None,
    )

    if linkedin is None:
        raise ValueError("LinkedIn ID not found in the profile")
    else:
        return linkedin["externalId"]


# function to join text from several keys
def join_keys(data, keys, separator="\n") -> str:
    """
    Joins the keys of a nested dictionary.

    Args:
        data (dict): The nested dictionary to join the keys from.
        keys (list): The keys to join.
        separator (str, optional): The separator to use between the keys. Defaults to "\n".

    Returns:
        str: The joined keys.

    """

    result = []

    # check if the data is a dictionary
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value:
                if isinstance(value, dict):
                    keys = value.keys()
                    result.append(join_keys(value, keys, separator))
                else:
                    # only appnend is value is not a number
                    if not isinstance(value, (int, float)):
                        result.append(str(value))

    # check if the data is a list
    elif isinstance(data, list):
        for item in data:
            keys = item.keys()
            result.append(join_keys(item, keys, separator))

    # join only unique results, preserving order
    # dict.fromkeys rather than set(): Python randomises string hashing per
    # process, so set() reorders the summary on every run. The analysis notebook
    # treats a changed summary as "reclassify with Gemini", so unstable ordering
    # silently re-bills for profiles that never changed.
    result = list(dict.fromkeys(result))

    # clean up text
    result = [
        s.replace("{", "").replace("}", "").replace("[", "").replace("]", "")
        for s in result
    ]

    return separator.join(result)


def get_member_distance(data) -> int:
    """
    Extract the LinkedIn network distance from a stored `extracted` document.

    The collection holds documents from two eras, so the field arrives in
    several shapes and the classifier reads whichever one it is given:
    - Nested dict: {"memberDistance": {"memberDistance": "DISTANCE_3", ...}}
    - Direct string: {"memberDistance": "DISTANCE_3"}
    - Direct integer: {"memberDistance": 3}   (what `to_lh_document` writes)
    - Out of network: {"memberDistance": "OUT_OF_NETWORK"}  -> 0

    Args:
        data (dict): The stored document.

    Returns:
        int: The distance, or 0 when it is absent or unrecognised.
    """

    try:
        member_distance = data.get("memberDistance")

        if member_distance is None:
            return 0

        # unwrap the nested form written by older LinkedIn Helper exports
        if isinstance(member_distance, dict):
            member_distance = member_distance.get("memberDistance")
            if member_distance is None:
                return 0

        if isinstance(member_distance, bool):
            return 0

        if isinstance(member_distance, int):
            return member_distance

        if isinstance(member_distance, str):
            if member_distance.startswith("DISTANCE_") and member_distance[-1:].isdigit():
                return int(member_distance[-1:])
            return 0

        return 0

    except Exception as e:
        print(f"Error getting member distance: {e}, data: {data.get('memberDistance')}")
        return 0


def count_created_since(collection_ref, cutoff, field="created_at") -> int:
    """Count documents in a collection whose `field` is at or after `cutoff`.

    This is the durable record of how many contacts were saved in a window, and
    it is what the notebook feeds to `SendBudget.reconcile` so the daily cap
    follows a rolling 24 hours rather than the UTC calendar day.

    It runs server-side as an aggregation, so it costs one document read per
    1000 matches instead of streaming the whole collection.

    Documents written before `created_at` existed carry no such field, and
    Firestore's inequality filter skips a document that is missing the field
    entirely -- which is the behaviour we want, since those predate any window
    we ask about.

    Args:
        collection_ref: The Firestore CollectionReference to count.
        cutoff (datetime): Timezone-aware lower bound, inclusive.
        field (str, optional): Timestamp field to compare. Defaults to "created_at".

    Returns:
        int: The number of matching documents.
    """

    query = collection_ref.where(filter=FieldFilter(field, ">=", cutoff))

    # `.get()` on an aggregation returns one result row per aggregation asked
    # for, wrapped in a list of result sets.
    result = query.count(alias="n").get()

    return int(result[0][0].value)


def member_id_from_urn(urn: str | None) -> str | None:
    """The numeric member id inside ``urn:li:member:<N>``.

    LinkedIn identifies a person two ways and the contact store is split across
    both: the newer documents carry the ``ACoAA...`` provider hash, while the
    ~28k imported from LinkedIn Helper carry this member id. A message-to-contact
    join that knows only one of them misses whole eras of the collection.

    Args:
        urn: A member URN, a bare numeric id, or None.

    Returns:
        str | None: The trailing id, or None when there is nothing to read.
    """
    if not urn:
        return None
    return str(urn).rsplit(":", 1)[-1] or None


def index_external_ids(documents) -> dict[tuple[str, str], str]:
    """Map ``(key_type, external_id) -> document id`` over streamed contacts.

    This is built in Python rather than queried, and that is a correctness
    requirement rather than an optimisation. Firestore's ``array_contains``
    matches an array element only in its entirety, and entries imported from
    LinkedIn Helper carry bookkeeping fields -- ``personId``, ``createdAt``,
    ``updatedAt``, ``memberId``, ``id``, ``sentAtToPAS``, ``actualAt`` --
    alongside the two that matter. Filtering on ``{"type": ..., "externalId":
    ...}`` therefore matches only the handful of documents written in the newer,
    thinner shape, and silently resolves about a tenth of the collection.

    The first document to claim a key keeps it, so a duplicate contact cannot
    displace the original mapping partway through a stream.

    Args:
        documents: Streamed Firestore documents exposing `.id` and `.to_dict()`.

    Returns:
        dict: Keys are `(type, external_id)` with the id normalised to `str`,
        because the stored type is not uniform across the two eras and an int
        key would never match a string lookup.
    """
    index: dict[tuple[str, str], str] = {}

    for document in documents:
        entries = (document.to_dict() or {}).get("externalIds")
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key_type = entry.get("type")
            external_id = entry.get("externalId")
            if not key_type or external_id is None or external_id == "":
                continue
            index.setdefault((key_type, str(external_id)), document.id)

    return index


def plan_forward_writes(messages: list) -> list:
    """Order a batch of newly-fetched messages oldest-first, ready to write.

    The API answers newest-first, and writing in that order is the one way an
    incremental sync can lose data permanently. A store that resumes from the
    newest timestamp it holds must never hold a message newer than one it is
    missing: write the newest of a batch first, die, and the high-water mark now
    sits above messages that were never written. The next run's forward pass
    starts above them and its backward pass starts below them, so nothing goes
    looking for them again.

    Ascending order makes every prefix of the plan contiguous, so an interruption
    leaves a shorter range rather than a hole.

    Undated messages sort last. They cannot move a watermark, so they are safe to
    write only once every dated message is already down.
    """
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(
        messages,
        key=lambda message: (message.timestamp is None, message.timestamp or floor),
    )


def needs_backfill(stored_from, floor) -> bool:
    """Whether any history older than what is stored still lies above the floor.

    Guards against an inverted window. The backward pass asks the API for
    ``after=floor, before=stored_from``; raising the floor past the oldest
    stored message crosses those bounds, and the API answers
    ``errors/invalid_parameters`` rather than an empty page.

    Raising the floor is not an error -- it narrows future interest without
    discarding anything already stored -- so this reports that there is simply
    no backfill left to do.

    Args:
        stored_from: The oldest timestamp held, or None for an empty collection.
        floor: The oldest timestamp the caller is interested in.

    Returns:
        bool: True while stored history stops short of the floor.
    """
    if stored_from is None:
        return True
    return stored_from > floor


def boundary_window(watermark, direction: str):
    """Widen an exclusive bound by a millisecond so a timestamp tie is not lost.

    ``before`` and ``after`` on the messages endpoint are exclusive, so asking
    for exactly the stored watermark silently drops any other message sharing
    that millisecond. Two such messages normally arrive in the same page and are
    written together -- but when the tie straddles a page boundary and the run
    dies in between, the survivor is excluded from every later walk.

    One millisecond of deliberate overlap closes that hole. It costs one
    idempotent rewrite per run, which is why the caller excludes the boundary
    ids by hand before deciding whether a delta is empty.

    Args:
        watermark: The stored bound, or None for an unbounded first run.
        direction: "forward" to widen an `after` bound (older by 1ms), or
            "backward" to widen a `before` bound (newer by 1ms).

    Returns:
        The widened bound, or None when there was no watermark to widen.
    """
    if watermark is None:
        return None
    if direction not in ("forward", "backward"):
        raise ValueError(f"direction must be 'forward' or 'backward', got {direction!r}")

    step = timedelta(milliseconds=1)
    return watermark - step if direction == "forward" else watermark + step


def contact_message_stats(documents) -> dict[str, dict]:
    """Per-contact reply and send tallies, keyed by contact document id.

    A reply is an inbound message in a conversation *we* opened. One arriving
    before our first message in that chat is someone approaching us -- a
    recruiter or a vendor -- and counting it overstates the reply rate by 35%:
    616 contacts have sent an inbound message, only 456 have answered one of
    ours.

    The test is applied per chat and the totals roll up per contact, because 21
    contacts hold more than one conversation, and an outbound in the warm one
    must not license an unsolicited inbound in the cold one.

    Attribution is dropped in the roll-up rather than in the first pass. 330
    stored messages carry no ``contact_doc_id``, and discarding them earlier
    could remove the very outbound that opens a chat, silently demoting a real
    reply to a cold approach.

    The ``--since`` floor causes that same demotion honestly: a conversation
    opened below the floor whose reply landed above it has no stored outbound.
    That is a property of the stored window rather than a rule to loosen --
    widen the floor and let the backward pass refill.

    Args:
        documents: Streamed `messages` documents, each exposing ``.id`` and
            ``.to_dict()``. Undated messages, and any whose ``is_sender`` is
            neither 0 nor 1, are ignored: neither can be placed against the
            conversation's opening message.

    Returns:
        dict[str, dict]: ``contact_doc_id`` to ``replied_total`` and
        ``sent_total``, plus ``last_reply_date``, ``last_reply_message_id`` and
        ``last_sent_date`` where there is one. Those three keys are absent
        rather than None, so "never replied" stays distinguishable from "no
        data" in any query written downstream.
    """
    rows = []
    opened: dict[str, datetime] = {}

    for document in documents:
        body = document.to_dict() or {}
        timestamp = body.get("timestamp")
        is_sender = body.get("is_sender")
        if timestamp is None or is_sender not in (0, 1):
            continue

        # A message with no chat is its own conversation. Pooling them under one
        # key would let an outbound to one contact open a chat for another.
        chat = body.get("chat_id") or document.id
        rows.append((chat, body.get("contact_doc_id"), is_sender, timestamp, document.id))
        if is_sender == 1 and (chat not in opened or timestamp < opened[chat]):
            opened[chat] = timestamp

    stats: dict[str, dict] = {}
    newest_reply: dict[str, tuple] = {}
    newest_sent: dict[str, datetime] = {}

    for chat, contact, is_sender, timestamp, message_id in rows:
        if not contact:
            continue

        entry = stats.setdefault(contact, {"replied_total": 0, "sent_total": 0})
        if is_sender == 1:
            entry["sent_total"] += 1
            if contact not in newest_sent or timestamp > newest_sent[contact]:
                newest_sent[contact] = timestamp
        elif chat in opened and timestamp > opened[chat]:
            # Strictly later: a message sharing an instant with our own opening
            # message was not written in response to it.
            entry["replied_total"] += 1
            # The id breaks a timestamp tie, so the winner is the same on every
            # recompute and an unchanged conversation is never rewritten.
            mark = (timestamp, message_id)
            if contact not in newest_reply or mark > newest_reply[contact]:
                newest_reply[contact] = mark

    for contact, entry in stats.items():
        if contact in newest_sent:
            entry["last_sent_date"] = newest_sent[contact]
        if contact in newest_reply:
            entry["last_reply_date"], entry["last_reply_message_id"] = newest_reply[contact]

    return stats


# test
if __name__ == "__main__" and os.path.exists("profile.json"):
    with open("profile.json", "r") as f:
        profile = json.load(f)

    # test get_linkedin_id function
    linkedin_id = get_linkedin_id(profile)
    print(linkedin_id)

    # summary function
    summary = join_keys(profile, ["currentPosition", "educations", "positions"])
    print(summary)


def select_intro_candidates(
    relations,
    contacts,
    chat_ids,
    *,
    industries,
    seniorities=None,
    holds=HANDLING_HOLDS,
    skip_existing_chats=True,
) -> tuple[list[dict], dict[str, int]]:
    """Who should receive the intro message, newest connection first.

    Driven from ``relations`` rather than from ``analysis`` because that is the
    only list that is both current and complete. ``memberDistance`` on a stored
    contact is whatever it was when the profile was scraped and is never
    refreshed: 32 of the first run's 173 candidates -- 18% -- were stored as
    second-degree despite being connections today. A relation also carries the
    ``ACoAA`` provider id the send endpoints take, which ``analysis`` does not
    hold at all.

    Three separate guards stand between a target and a duplicate message, and
    they are not redundant -- each covers a window the others cannot see:

    ``chat_ids``
        Read live from LinkedIn, so it cannot go stale. It is also the only
        guard that catches an inbound-only conversation, where the intro would
        land underneath a question of theirs we never answered.
    ``sent_total``
        Only as fresh as the last ``messages-sync.py`` run, and absent entirely
        for anyone messaged below its ``--since`` floor. It narrows the list; it
        cannot protect it.
    ``intro_sent_at``
        Written by this campaign the moment a send succeeds, because
        ``sent_total`` will not catch up until the next sync -- and a re-run an
        hour later must not send a second copy.

    Args:
        relations: First-degree connections, each exposing ``public_identifier``,
            ``provider_id``, the name fields and ``created_at``.
        contacts: ``doc_id`` to the stored ``analysis`` body, holding at least
            ``industry`` and whichever of ``seniority``, ``handling``,
            ``sent_total`` and ``intro_sent_at`` exist. A relation missing here, or present with an
            empty ``industry``, counts as unclassified rather than off-target:
            both are contacts whose industry we cannot confirm, and guessing
            puts the pitch in front of a hospital.
        chat_ids: ``provider_id`` to ``chat_id`` for every open conversation.
        industries: The target industry labels.
        holds: ``handling`` values that hold a contact back, matched after
            trimming and lowercasing. Defaults to :data:`HANDLING_HOLDS`.
        seniorities: Target seniority labels, or None to accept every level.
        skip_existing_chats: Drop anyone we already have a conversation with.

    Returns:
        tuple: The candidates, ordered newest connection first, and a tally of
        why the rest were skipped. Every reason key is present even at zero, so
        a printed summary has the same shape on every run.
    """
    skipped = dict.fromkeys(SKIP_REASONS, 0)
    candidates = []

    for relation in relations:
        slug = relation.public_identifier
        provider_id = relation.provider_id
        contact = contacts.get(slug)

        if contact is None:
            skipped["unclassified"] += 1
            continue

        industry = contact.get("industry") or ""
        seniority = contact.get("seniority") or ""

        # Half the collection has no industry yet. Counting that as off-target
        # would hide a different remedy behind the same number: an off-target
        # contact is a dead end, an unclassified one becomes a candidate the
        # moment analysis.ipynb runs.
        if not industry:
            skipped["unclassified"] += 1
            continue
        if industry not in industries:
            skipped["off_target"] += 1
            continue
        if seniorities is not None and seniority not in seniorities:
            skipped["off_target"] += 1
            continue

        # Set by hand on 85 of 28,318 documents, so it is the most deliberate
        # signal in the collection and outranks every automatic one. Normalised
        # because a hand-maintained field collects stray capitals and padding,
        # and a near-miss here fails open -- it sends the message anyway.
        if str(contact.get("handling") or "").strip().lower() in holds:
            skipped["handling"] += 1
            continue

        if contact.get("intro_sent_at"):
            skipped["intro_already_sent"] += 1
            continue

        # Truthiness, not `is not None`: the field is absent for the 4,906
        # contacts never written to, and a measured 0 means they wrote to us and
        # we never answered -- a target either way.
        if contact.get("sent_total"):
            skipped["already_messaged"] += 1
            continue

        chat_id = chat_ids.get(provider_id)
        if chat_id and skip_existing_chats:
            skipped["existing_chat"] += 1
            continue

        candidates.append(
            {
                "doc_id": slug,
                "provider_id": provider_id,
                "chat_id": chat_id,
                "name": f"{relation.first_name or ''} {relation.last_name or ''}".strip(),
                "headline": relation.headline or "",
                "industry": industry,
                "seniority": seniority,
                "connected_at": relation.created_at,
                "profile_url": relation.public_profile_url
                or f"https://www.linkedin.com/in/{slug}",
            }
        )

    return sorted(candidates, key=_newest_connection_first), skipped


def _newest_connection_first(candidate: dict) -> tuple[int, float]:
    """Sort newest first, undated last.

    The template opens with "Thanks for connecting", so it ages badly, and at 50
    sends a day against thousands of candidates the order decides who ever gets
    it. A relation with no ``created_at`` sorts last rather than first: unknown
    is not evidence of recency.
    """
    connected = candidate["connected_at"]
    if connected is None:
        return (1, 0.0)
    return (0, -connected.timestamp())
