"""Tests for the existing profile-flattening helpers.

`join_keys` builds the plain-text `summary` that Gemini classifies. Its output
must be deterministic: the notebook treats a changed summary as a signal to
re-classify, so unstable ordering re-bills Gemini for profiles that did not
actually change.

`count_created_since` is what the rolling-24h budget is recounted from, so the
query it builds is worth pinning: a wrong operator would silently report zero
contacts saved and hand back a full allowance.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from functions import (
    boundary_window,
    contact_message_stats,
    count_created_since,
    get_member_distance,
    index_external_ids,
    join_keys,
    member_id_from_urn,
    needs_backfill,
    plan_forward_writes,
    select_intro_candidates,
)
from lib.unipile.models import Message, Relation


def test_join_keys_preserves_input_order():
    """Deduplication must not reorder.

    `list(set(...))` returns an arbitrary order that varies per process, because
    Python randomises string hashing. With eight distinct values, matching input
    order by luck is effectively impossible.
    """
    values = ["v-one", "v-two", "v-three", "v-four", "v-five", "v-six", "v-seven", "v-eight"]
    data = {f"k{i}": value for i, value in enumerate(values)}

    result = join_keys(data, list(data))

    assert result.split("\n") == values


def test_join_keys_still_deduplicates():
    data = {"a": "same", "b": "same", "c": "different"}

    assert join_keys(data, list(data)).split("\n") == ["same", "different"]


def test_get_member_distance_reads_every_stored_shape():
    """`extracted` holds documents from two eras, so the field has several shapes.

    Phase E of new-contacts.ipynb reads distance straight off stored documents,
    so a shape it does not understand would silently write 0 into `analysis` and
    make a first-degree connection look out-of-network.
    """
    assert get_member_distance({"memberDistance": {"memberDistance": "DISTANCE_3"}}) == 3
    assert get_member_distance({"memberDistance": "DISTANCE_1"}) == 1
    assert get_member_distance({"memberDistance": 2}) == 2
    assert get_member_distance({"memberDistance": "OUT_OF_NETWORK"}) == 0


def test_get_member_distance_defaults_to_zero_when_absent_or_unknown():
    assert get_member_distance({}) == 0
    assert get_member_distance({"memberDistance": None}) == 0
    assert get_member_distance({"memberDistance": {}}) == 0
    assert get_member_distance({"memberDistance": "SOMETHING_NEW"}) == 0


# --- count_created_since ------------------------------------------------------


class _FakeAggregation:
    def __init__(self, value: int) -> None:
        self._value = value

    def get(self):
        """Firestore returns one row of results per aggregation requested."""
        return [[SimpleNamespace(value=self._value)]]


class _FakeQuery:
    def __init__(self, matches: int) -> None:
        self._matches = matches
        self.alias = None

    def count(self, alias=None):
        self.alias = alias
        return _FakeAggregation(self._matches)


class _FakeCollection:
    """Records the filter it was handed instead of talking to Firestore."""

    def __init__(self, matches: int) -> None:
        self._matches = matches
        self.filter = None
        self.query = None

    def where(self, filter=None):
        self.filter = filter
        self.query = _FakeQuery(self._matches)
        return self.query


def test_count_created_since_unwraps_the_aggregation_result():
    assert count_created_since(_FakeCollection(7), datetime(2026, 9, 8, tzinfo=UTC)) == 7


def test_count_created_since_filters_on_the_timestamp_inclusively():
    """`>=`, not `>`: the window is closed at the cutoff, and the field is the
    server timestamp the notebook writes when it saves a contact."""
    collection = _FakeCollection(0)
    cutoff = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)

    count_created_since(collection, cutoff)

    assert collection.filter.field_path == "created_at"
    assert collection.filter.op_string == ">="
    assert collection.filter.value == cutoff


def test_count_created_since_accepts_a_different_timestamp_field():
    collection = _FakeCollection(0)

    count_created_since(collection, datetime(2026, 9, 8, tzinfo=UTC), field="sent_at")

    assert collection.filter.field_path == "sent_at"


# --- message sync helpers -----------------------------------------------------


class _FakeDoc:
    """A streamed Firestore document: an id and a body, nothing else."""

    def __init__(self, doc_id: str, body: dict) -> None:
        self.id = doc_id
        self._body = body

    def to_dict(self):
        return self._body


def _external(doc_id, *entries):
    return _FakeDoc(doc_id, {"externalIds": list(entries)})


def test_member_id_from_urn_extracts_the_trailing_number():
    assert member_id_from_urn("urn:li:member:12345") == "12345"


def test_member_id_from_urn_accepts_a_bare_number_and_rejects_nothing_useful():
    assert member_id_from_urn("12345") == "12345"
    assert member_id_from_urn(None) is None
    assert member_id_from_urn("") is None


def test_index_external_ids_maps_both_key_types_to_the_document():
    index = index_external_ids([
        _external("some-slug",
                  {"type": "public-id", "externalId": "some-slug"},
                  {"type": "li-hash-id", "externalId": "ACoAA-hash"},
                  {"type": "member-id", "externalId": "12345"}),
    ])

    assert index[("li-hash-id", "ACoAA-hash")] == "some-slug"
    assert index[("member-id", "12345")] == "some-slug"


def test_index_external_ids_finds_documents_carrying_only_one_key_type():
    """Neither key alone is sufficient across the two eras of stored contacts:
    2,092 documents carry a member id and no hash, 260 the reverse."""
    index = index_external_ids([
        _external("legacy-only", {"type": "member-id", "externalId": "111"}),
        _external("modern-only", {"type": "li-hash-id", "externalId": "ACoAA-222"}),
    ])

    assert index[("member-id", "111")] == "legacy-only"
    assert index[("li-hash-id", "ACoAA-222")] == "modern-only"


def test_index_external_ids_ignores_the_extra_keys_legacy_entries_carry():
    """This is why the index is built in Python and not by a Firestore query.

    `array_contains` matches a map only in its entirety, and legacy entries
    carry bookkeeping fields alongside the two that matter. A query filtering on
    {"type": ..., "externalId": ...} therefore misses every legacy document --
    which is most of them.
    """
    index = index_external_ids([
        _external("legacy", {
            "type": "li-hash-id",
            "externalId": "ACoAA-hash",
            "personId": 1,
            "id": 564,
            "hash": "ACoAA-hash",
            "createdAt": "2020-11-17T22:28:42.704Z",
            "updatedAt": "2022-08-23T13:39:05.219Z",
            "sentAtToPAS": "2024-03-28T12:15:27.605Z",
            "actualAt": "2022-08-23T13:39:05.219Z",
        }),
    ])

    assert index[("li-hash-id", "ACoAA-hash")] == "legacy"


def test_index_external_ids_normalises_the_external_id_to_a_string():
    """The stored type is not uniform across eras; an int key would never match."""
    index = index_external_ids([
        _external("numeric", {"type": "member-id", "externalId": 12345}),
    ])

    assert index[("member-id", "12345")] == "numeric"


def test_index_external_ids_skips_entries_with_no_usable_id():
    index = index_external_ids([
        _FakeDoc("empty", {}),
        _FakeDoc("null-array", {"externalIds": None}),
        _external("no-id", {"type": "member-id", "externalId": None}),
        _external("not-a-map", "junk"),
    ])

    assert index == {}


# --- forward write ordering ---------------------------------------------------


def _msg(mid, ts):
    return Message.model_validate({"id": mid, "timestamp": ts})


def test_plan_forward_writes_orders_the_delta_oldest_first():
    """The API answers newest-first; a durable store must be filled the other way."""
    api_order = [_msg("E", "2026-09-05T00:00:00.000Z"),
                 _msg("D", "2026-09-04T00:00:00.000Z"),
                 _msg("C", "2026-09-03T00:00:00.000Z")]

    assert [m.id for m in plan_forward_writes(api_order)] == ["C", "D", "E"]


def test_a_forward_write_interrupted_midway_leaves_no_gap():
    """The regression test for the bug this ordering exists to prevent.

    Writing newest-first and then dying raises the stored high-water mark past
    messages that were never written: the next run's forward pass starts above
    them and its backward pass starts below them, so nothing ever fetches them
    again. Ascending order means every prefix of the plan is contiguous -- an
    interruption leaves a shorter range, not a hole.
    """
    planned = plan_forward_writes([
        _msg("E", "2026-09-05T00:00:00.000Z"),
        _msg("D", "2026-09-04T00:00:00.000Z"),
        _msg("C", "2026-09-03T00:00:00.000Z"),
    ])

    for cut in range(1, len(planned)):
        written, unwritten = planned[:cut], planned[cut:]
        highest_written = max(m.timestamp for m in written)
        lowest_unwritten = min(m.timestamp for m in unwritten)
        assert highest_written < lowest_unwritten, (
            f"writing the first {cut} would strand {[m.id for m in unwritten]}"
        )


def test_plan_forward_writes_puts_undated_messages_last():
    """An undated message cannot move a watermark, so it is written only once
    every dated message is safely down."""
    planned = plan_forward_writes([
        _msg("undated", None),
        _msg("dated", "2026-09-03T00:00:00.000Z"),
    ])

    assert [m.id for m in planned] == ["dated", "undated"]


# --- exclusive-bound nudging --------------------------------------------------


def test_boundary_window_widens_an_after_bound_backwards_by_one_millisecond():
    """`after` is exclusive, so asking for exactly the watermark would drop any
    other message sharing that millisecond."""
    watermark = datetime(2026, 9, 4, 12, 46, 3, 616000, tzinfo=UTC)

    assert boundary_window(watermark, "forward") == watermark - timedelta(milliseconds=1)


def test_boundary_window_widens_a_before_bound_forwards_by_one_millisecond():
    watermark = datetime(2024, 1, 1, 19, 40, 55, 935000, tzinfo=UTC)

    assert boundary_window(watermark, "backward") == watermark + timedelta(milliseconds=1)


def test_boundary_window_passes_through_a_missing_watermark():
    """An empty collection has no bound to widen -- the first run is unbounded."""
    assert boundary_window(None, "forward") is None
    assert boundary_window(None, "backward") is None


def test_backfill_is_needed_while_stored_history_stops_short_of_the_floor():
    floor = datetime(2024, 1, 1, tzinfo=UTC)

    assert needs_backfill(None, floor) is True, "an empty collection must backfill"
    assert needs_backfill(datetime(2026, 3, 10, tzinfo=UTC), floor) is True


def test_backfill_stops_once_stored_history_reaches_the_floor():
    """Raising the floor above what is stored must not invert the window.

    The backward pass asks for (after=floor, before=min_ts). If the floor is
    raised past min_ts those bounds cross, and the API rejects the request with
    `errors/invalid_parameters` rather than returning nothing.
    """
    stored_from = datetime(2024, 1, 1, 19, 40, tzinfo=UTC)

    assert needs_backfill(stored_from, datetime(2026, 9, 1, tzinfo=UTC)) is False
    assert needs_backfill(stored_from, stored_from) is False


# --- per-contact message stats ------------------------------------------------


def _stored(mid, chat, contact, sender, ts):
    """One document as `messages` holds it, projected to the four fields read."""
    return _FakeDoc(mid, {
        "chat_id": chat,
        "contact_doc_id": contact,
        "is_sender": sender,
        "timestamp": ts,
    })


def _day(number):
    return datetime(2026, 1, number, 12, 0, tzinfo=UTC)


def test_an_inbound_before_the_first_outbound_is_not_a_reply():
    """The single most consequential rule in this function.

    616 contacts have sent an inbound message; only 456 have answered one of
    ours. The other 160 opened the conversation themselves -- recruiters and
    vendors pitching us -- and counting them would overstate the reply rate by
    35%.
    """
    stats = contact_message_stats([
        _stored("pitch", "chat-1", "cold-caller", 0, _day(1)),
        _stored("ours", "chat-1", "cold-caller", 1, _day(2)),
    ])

    assert stats["cold-caller"]["replied_total"] == 0
    assert stats["cold-caller"]["sent_total"] == 1


def test_an_inbound_after_the_first_outbound_is_a_reply():
    stats = contact_message_stats([
        _stored("ours", "chat-1", "answered", 1, _day(1)),
        _stored("theirs", "chat-1", "answered", 0, _day(2)),
    ])

    assert stats["answered"]["replied_total"] == 1
    assert stats["answered"]["last_reply_date"] == _day(2)


def test_the_reply_rule_is_applied_per_chat_before_the_contact_rollup():
    """21 contacts hold more than one conversation.

    Judging a contact as a whole would let an outbound in one chat license an
    unsolicited inbound in another as a reply.
    """
    stats = contact_message_stats([
        _stored("pitch", "chat-cold", "two-chats", 0, _day(1)),
        _stored("ours", "chat-warm", "two-chats", 1, _day(2)),
        _stored("theirs", "chat-warm", "two-chats", 0, _day(3)),
    ])

    assert stats["two-chats"]["replied_total"] == 1
    assert stats["two-chats"]["sent_total"] == 1


def test_an_unattributed_outbound_still_opens_the_conversation():
    """Attribution is dropped in the roll-up, never before the per-chat pass.

    330 stored messages carry no `contact_doc_id`. Filtering them out first
    could remove the outbound that opens a chat, silently demoting a real reply
    to an unsolicited approach.
    """
    stats = contact_message_stats([
        _stored("ours", "chat-1", None, 1, _day(1)),
        _stored("theirs", "chat-1", "joined", 0, _day(2)),
    ])

    assert stats["joined"]["replied_total"] == 1


def test_a_null_is_sender_counts_as_neither_sent_nor_replied():
    """`is_sender` is 0, 1 or None. `if not is_sender` would invent a reply."""
    stats = contact_message_stats([
        _stored("ours", "chat-1", "unknown-direction", 1, _day(1)),
        _stored("mystery", "chat-1", "unknown-direction", None, _day(2)),
    ])

    assert stats["unknown-direction"]["sent_total"] == 1
    assert stats["unknown-direction"]["replied_total"] == 0


def test_undated_messages_are_left_out_entirely():
    """A message with no timestamp cannot be placed against the first outbound.

    The sync omits a null timestamp rather than storing one, so the key is
    absent rather than None.
    """
    stats = contact_message_stats([
        _stored("ours", "chat-1", "partly-dated", 1, _day(1)),
        _FakeDoc("undated", {"chat_id": "chat-1", "contact_doc_id": "partly-dated",
                             "is_sender": 0}),
    ])

    assert stats["partly-dated"]["replied_total"] == 0
    assert stats["partly-dated"]["sent_total"] == 1


def test_a_contact_who_never_replied_carries_no_last_reply_fields():
    """Absent beats null: a null date sorts first in every query written later."""
    stats = contact_message_stats([
        _stored("ours", "chat-1", "silent", 1, _day(1)),
    ])

    assert stats["silent"]["replied_total"] == 0
    assert "last_reply_date" not in stats["silent"]
    assert "last_reply_message_id" not in stats["silent"]


def test_last_reply_message_id_is_the_document_id_of_the_newest_reply():
    """The id is the join key back into `messages`, where it is the document id."""
    stats = contact_message_stats([
        _stored("ours", "chat-1", "chatty", 1, _day(1)),
        _stored("first-reply", "chat-1", "chatty", 0, _day(2)),
        _stored("latest-reply", "chat-1", "chatty", 0, _day(3)),
    ])

    assert stats["chatty"]["last_reply_message_id"] == "latest-reply"
    assert stats["chatty"]["replied_total"] == 2


def test_the_newest_reply_breaks_a_timestamp_tie_on_the_message_id():
    """Stability matters because the pass rewrites only what changed.

    An arbitrary winner among two replies sharing a millisecond would rewrite
    the document on every run. `boundary_window` exists because such ties are
    real.
    """
    tie = _day(2)
    stats = contact_message_stats([
        _stored("ours", "chat-1", "tied", 1, _day(1)),
        _stored("zzz", "chat-1", "tied", 0, tie),
        _stored("aaa", "chat-1", "tied", 0, tie),
    ])

    assert stats["tied"]["last_reply_message_id"] == "zzz"


def test_last_sent_date_is_the_newest_outbound_across_every_chat():
    stats = contact_message_stats([
        _stored("old", "chat-a", "two-chats", 1, _day(1)),
        _stored("new", "chat-b", "two-chats", 1, _day(5)),
    ])

    assert stats["two-chats"]["last_sent_date"] == _day(5)
    assert stats["two-chats"]["sent_total"] == 2


def test_a_message_with_no_chat_id_is_its_own_conversation():
    """Grouping every chatless message together would fabricate a conversation."""
    stats = contact_message_stats([
        _stored("ours", None, "no-chat", 1, _day(1)),
        _stored("theirs", None, "no-chat", 0, _day(2)),
    ])

    assert stats["no-chat"]["replied_total"] == 0


def test_messages_with_no_contact_reach_no_contact_document():
    """Two stored messages carry no identity at all, and an empty string would
    address a Firestore document just as readily as a real id."""
    stats = contact_message_stats([
        _stored("orphan", "chat-1", None, 1, _day(1)),
        _stored("blank", "chat-2", "", 0, _day(2)),
    ])

    assert stats == {}


# --- select_intro_candidates ---------------------------------------------------
#
# The intro campaign's whole safety story lives in this function, so each guard
# gets its own test. Getting one wrong does not raise -- it sends a stranger a
# second copy of "Thanks for connecting", which cannot be taken back.


def _relation(slug, provider_id, connected=None, first="Test", last="Person"):
    """A first-degree connection as `/users/relations` returns one.

    `member_id` is the `ACoAA...` provider hash rather than the numeric member
    id -- `Relation.provider_id` aliases it, and it is what the send endpoints
    take.
    """
    return Relation(
        public_identifier=slug,
        member_id=provider_id,
        first_name=first,
        last_name=last,
        headline="Director of Revenue Cycle",
        created_at=connected,
    )


TARGETS = {"RCM", "Pathology", "Medical Lab"}


def test_a_connection_with_no_classification_is_never_messaged():
    """~2.1k documents are keyed by numeric member id and carry neither a
    public-id nor an li-hash-id, so they cannot be joined to a relation at all.

    Silence is the safe direction: an unjoined contact is one whose industry we
    cannot confirm, and guessing would put the pitch in front of a hospital.
    """
    candidates, skipped = select_intro_candidates(
        [_relation("stranger", "ACoAA-stranger")], {}, {}, industries=TARGETS
    )

    assert candidates == []
    assert skipped["unclassified"] == 1


def test_only_the_target_industries_are_selected():
    relations = [
        _relation("rcm-person", "ACoAA-1"),
        _relation("hospital-person", "ACoAA-2"),
    ]
    contacts = {
        "rcm-person": {"industry": "RCM"},
        "hospital-person": {"industry": "Hospital"},
    }

    candidates, skipped = select_intro_candidates(
        relations, contacts, {}, industries=TARGETS
    )

    assert [c["doc_id"] for c in candidates] == ["rcm-person"]
    assert skipped["off_target"] == 1


def test_a_missing_sent_total_means_never_messaged_not_no_data():
    """4,906 of the 4,912 never-messaged targets have no `sent_total` field.

    `refresh_contact_stats` only writes the field for contacts who appear in the
    `messages` collection, so treating absence as unknown-and-skip would empty
    the campaign; treating it as "messaged" would empty it too.
    """
    candidates, _ = select_intro_candidates(
        [_relation("never-written-to", "ACoAA-1")],
        {"never-written-to": {"industry": "RCM"}},
        {},
        industries=TARGETS,
    )

    assert [c["doc_id"] for c in candidates] == ["never-written-to"]


def test_a_contact_we_have_already_written_to_is_skipped():
    candidates, skipped = select_intro_candidates(
        [_relation("in-conversation", "ACoAA-1")],
        {"in-conversation": {"industry": "RCM", "sent_total": 3}},
        {},
        industries=TARGETS,
    )

    assert candidates == []
    assert skipped["already_messaged"] == 1


def test_an_inbound_only_contact_still_counts_as_never_messaged():
    """`sent_total` of 0 is a measured zero: they wrote to us and we never
    answered. That is a target, not a contact to protect."""
    candidates, _ = select_intro_candidates(
        [_relation("wrote-to-us", "ACoAA-1")],
        {"wrote-to-us": {"industry": "Pathology", "sent_total": 0, "replied_total": 0}},
        {},
        industries=TARGETS,
    )

    assert [c["doc_id"] for c in candidates] == ["wrote-to-us"]


def test_an_open_conversation_is_skipped_even_when_firestore_says_nothing():
    """The chat map is read live from LinkedIn; `sent_total` is only as fresh as
    the last messages-sync run, and is absent entirely below its `--since` floor.

    So the live signal has to be the one that decides. This is also the case
    where a mistake reads worst: "Thanks for connecting" landing underneath a
    question of theirs we never answered.
    """
    candidates, skipped = select_intro_candidates(
        [_relation("has-a-chat", "ACoAA-1")],
        {"has-a-chat": {"industry": "RCM"}},
        {"ACoAA-1": "chat-42"},
        industries=TARGETS,
    )

    assert candidates == []
    assert skipped["existing_chat"] == 1


def test_an_open_conversation_carries_its_chat_id_when_it_is_not_skipped():
    """Sending into an existing chat needs the id: `send_to` would otherwise
    re-walk every conversation on the account to rediscover it."""
    candidates, _ = select_intro_candidates(
        [_relation("has-a-chat", "ACoAA-1")],
        {"has-a-chat": {"industry": "RCM"}},
        {"ACoAA-1": "chat-42"},
        industries=TARGETS,
        skip_existing_chats=False,
    )

    assert [c["chat_id"] for c in candidates] == ["chat-42"]


def test_a_contact_this_campaign_already_wrote_to_is_skipped():
    """`intro_sent_at` is written the moment a send succeeds, because
    `sent_total` will not catch up until the next messages-sync -- and a re-run
    an hour later must not send a second copy.

    It is a separate field on purpose: `refresh_contact_stats` deletes
    `sent_total` from any contact whose messages it cannot find.
    """
    candidates, skipped = select_intro_candidates(
        [_relation("intro-done", "ACoAA-1")],
        {"intro-done": {"industry": "RCM", "intro_sent_at": _day(1)}},
        {},
        industries=TARGETS,
    )

    assert candidates == []
    assert skipped["intro_already_sent"] == 1


def test_the_newest_connections_are_messaged_first():
    """The template opens with "Thanks for connecting", so it ages badly. With
    thousands of candidates and 50 sends a day, order decides who ever gets it.

    A relation with no `created_at` sorts last rather than first: unknown is not
    evidence of recency.
    """
    relations = [
        _relation("older", "ACoAA-1", connected=_day(1)),
        _relation("undated", "ACoAA-2", connected=None),
        _relation("newest", "ACoAA-3", connected=_day(9)),
    ]
    contacts = {slug: {"industry": "RCM"} for slug in ("older", "undated", "newest")}

    candidates, _ = select_intro_candidates(
        relations, contacts, {}, industries=TARGETS
    )

    assert [c["doc_id"] for c in candidates] == ["newest", "older", "undated"]


def test_seniority_narrows_only_when_it_is_asked_to():
    relations = [_relation("boss", "ACoAA-1"), _relation("staffer", "ACoAA-2")]
    contacts = {
        "boss": {"industry": "RCM", "seniority": "Director"},
        "staffer": {"industry": "RCM", "seniority": "Staff"},
    }

    everyone, _ = select_intro_candidates(relations, contacts, {}, industries=TARGETS)
    leaders, skipped = select_intro_candidates(
        relations, contacts, {}, industries=TARGETS, seniorities={"Director"}
    )

    assert len(everyone) == 2
    assert [c["doc_id"] for c in leaders] == ["boss"]
    assert skipped["off_target"] == 1


def test_an_unclassified_contact_is_not_reported_as_off_target():
    """14,636 of the 28,318 documents have no industry at all.

    Lumping them in with Hospital would hide a different remedy behind the same
    number: an off-target contact is a dead end, while an unclassified one
    becomes a candidate as soon as analysis.ipynb runs.
    """
    candidates, skipped = select_intro_candidates(
        [_relation("not-yet-classified", "ACoAA-1")],
        {"not-yet-classified": {"industry": ""}},
        {},
        industries=TARGETS,
    )

    assert candidates == []
    assert skipped["unclassified"] == 1
    assert skipped["off_target"] == 0


def test_a_contact_marked_exclude_is_never_messaged():
    """`handling` is set by hand, so it outranks every automatic signal."""
    candidates, skipped = select_intro_candidates(
        [_relation("hands-off", "ACoAA-1")],
        {"hands-off": {"industry": "RCM", "handling": "exclude"}},
        {},
        industries=TARGETS,
    )

    assert candidates == []
    assert skipped["handling"] == 1


def test_a_contact_marked_manual_is_never_messaged():
    """"manual" means someone intends to write to them personally. A templated
    "Thanks for connecting" arriving first is exactly what it is meant to stop."""
    candidates, skipped = select_intro_candidates(
        [_relation("write-by-hand", "ACoAA-1")],
        {"write-by-hand": {"industry": "RCM", "handling": "manual"}},
        {},
        industries=TARGETS,
    )

    assert candidates == []
    assert skipped["handling"] == 1


def test_other_handling_values_do_not_hold_a_contact_back():
    """The field is absent on 28,233 of 28,318 documents, so only the two values
    that mean "do not send" may filter -- anything else must pass through."""
    relations = [_relation("no-field", "ACoAA-1"), _relation("some-other", "ACoAA-2")]
    contacts = {
        "no-field": {"industry": "RCM"},
        "some-other": {"industry": "RCM", "handling": "auto"},
    }

    candidates, skipped = select_intro_candidates(
        relations, contacts, {}, industries=TARGETS
    )

    assert [c["doc_id"] for c in candidates] == ["no-field", "some-other"]
    assert skipped["handling"] == 0


def test_handling_is_matched_regardless_of_case_or_padding():
    """A hand-maintained field picks up stray capitals and whitespace, and a
    near-miss here fails open -- it sends the message anyway."""
    relations = [_relation("shouty", "ACoAA-1"), _relation("padded", "ACoAA-2")]
    contacts = {
        "shouty": {"industry": "RCM", "handling": "Exclude"},
        "padded": {"industry": "RCM", "handling": " manual "},
    }

    candidates, skipped = select_intro_candidates(
        relations, contacts, {}, industries=TARGETS
    )

    assert candidates == []
    assert skipped["handling"] == 2
