"""
This module contains common functions used throughout the application.
"""

import json  # for testing purposes
import os  # for testing purposes
from datetime import UTC, datetime, timedelta

from google.cloud.firestore_v1.base_query import FieldFilter


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
