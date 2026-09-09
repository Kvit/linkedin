"""Tests for the pure layer of pipeline classification.

`build_transcripts` decides what Gemini reads and which inbound message keys
the classification; `plan_pipeline` decides who is billed again; `build_prompt`
decides what the model is told is given. All three are pure, so the rules are
pinned here without Firestore or a model.
"""

from datetime import UTC, datetime

from pipeline import (
    NO_PROFILE,
    NOT_CLASSIFIED,
    SILENT_STAGE,
    build_prompt,
    build_transcripts,
    plan_pipeline,
)


class _FakeDoc:
    def __init__(self, doc_id, body):
        self.id = doc_id
        self._body = body

    def to_dict(self):
        return self._body


def _msg(mid, chat, contact, sender, ts, text="hello", **flags):
    """One document as `messages` holds it, projected to the seven fields read."""
    return _FakeDoc(mid, {
        "chat_id": chat,
        "contact_doc_id": contact,
        "is_sender": sender,
        "timestamp": ts,
        "text": text,
        **flags,
    })


def _day(number):
    return datetime(2026, 1, number, 12, 0, tzinfo=UTC)


# --- build_transcripts --------------------------------------------------------


def test_lines_are_labelled_by_sender_and_ordered_oldest_first():
    out = build_transcripts([
        _msg("m2", "c1", "ann", 0, _day(2), "Thanks, will do"),
        _msg("m1", "c1", "ann", 1, _day(1), "Thanks for connecting"),
    ])

    assert out["ann"]["transcript"] == (
        "--- conversation 1 ---\n"
        "2026-01-01 Me: Thanks for connecting\n"
        "2026-01-02 Them: Thanks, will do"
    )


def test_each_chat_is_its_own_section_in_the_order_they_started():
    """21 contacts hold more than one conversation; interleaving them would
    put a reply in the warm thread next to an intro in the cold one."""
    out = build_transcripts([
        _msg("m3", "later", "ann", 1, _day(5), "Second thread"),
        _msg("m1", "first", "ann", 1, _day(1), "First thread"),
        _msg("m2", "first", "ann", 0, _day(2), "Reply in first"),
    ])

    assert out["ann"]["transcript"].split("\n") == [
        "--- conversation 1 ---",
        "2026-01-01 Me: First thread",
        "2026-01-02 Them: Reply in first",
        "--- conversation 2 ---",
        "2026-01-05 Me: Second thread",
    ]


def test_events_deletions_blank_text_and_unplaceable_messages_are_left_out():
    """The same exclusions `contact_message_stats` makes, plus the three that
    only matter when the text is read: events, deletions, attachment-only."""
    out = build_transcripts([
        _msg("keep", "c1", "ann", 1, _day(1), "Hi"),
        _msg("event", "c1", "ann", 0, _day(2), "accepted your invitation", is_event=1),
        _msg("gone", "c1", "ann", 0, _day(3), "deleted later", deleted=1),
        _msg("blank", "c1", "ann", 0, _day(4), "   "),
        _msg("undated", "c1", "ann", 0, None, "no timestamp"),
        _msg("nobody", "c1", "ann", None, _day(5), "null is_sender"),
        _msg("orphan", "c1", None, 0, _day(6), "no contact"),
    ])

    assert out["ann"]["transcript"] == "--- conversation 1 ---\n2026-01-01 Me: Hi"
    assert out["ann"]["inbound_total"] == 0


def test_a_contact_with_nothing_readable_is_absent():
    out = build_transcripts([_msg("e", "c1", "ann", 0, _day(1), "joined", is_event=1)])

    assert "ann" not in out


def test_newest_inbound_ignores_our_own_messages_and_breaks_ties_on_id():
    """The key must not move when *we* write, or every follow-up campaign
    would re-bill Gemini for contacts who said nothing new."""
    out = build_transcripts([
        _msg("m1", "c1", "ann", 1, _day(1), "intro"),
        _msg("m2", "c1", "ann", 0, _day(2), "reply"),
        _msg("m2b", "c1", "ann", 0, _day(2), "same instant"),
        _msg("m9", "c1", "ann", 1, _day(9), "our follow-up"),
    ])

    assert out["ann"]["inbound_total"] == 2
    assert out["ann"]["newest_inbound_id"] == "m2b"
    assert out["ann"]["newest_inbound_date"] == _day(2)


def test_a_contact_who_never_wrote_has_no_newest_inbound():
    out = build_transcripts([_msg("m1", "c1", "ann", 1, _day(1), "intro")])

    assert out["ann"]["inbound_total"] == 0
    assert out["ann"]["newest_inbound_id"] is None
    assert out["ann"]["newest_inbound_date"] is None


def test_whitespace_inside_a_message_is_collapsed_to_one_line():
    """One line per message is what keeps `Me:` / `Them:` unambiguous."""
    out = build_transcripts([_msg("m1", "c1", "ann", 0, _day(1), "line one\n\nline  two")])

    assert out["ann"]["transcript"].endswith("Them: line one line two")


def test_a_message_with_no_chat_id_is_its_own_conversation():
    out = build_transcripts([
        _msg("m1", None, "ann", 1, _day(1), "first"),
        _msg("m2", None, "ann", 1, _day(2), "second"),
    ])

    assert out["ann"]["transcript"].count("--- conversation") == 2
