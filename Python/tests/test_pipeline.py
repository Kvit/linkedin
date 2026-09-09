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


# --- plan_pipeline ------------------------------------------------------------


def _entry(inbound_total, newest_id=None, newest_date=None):
    return {
        "transcript": "--- conversation 1 ---\n2026-01-01 Me: intro",
        "inbound_total": inbound_total,
        "newest_inbound_id": newest_id,
        "newest_inbound_date": newest_date,
    }


def test_a_contact_with_an_inbound_message_and_no_classification_is_queued():
    silent, queue, tally = plan_pipeline({"ann": _entry(1, "m2", _day(2))}, {"ann": {}})

    assert queue == ["ann"]
    assert silent == []
    assert tally["queued"] == 1


def test_a_single_inbound_message_is_enough_to_be_queued():
    """One 'no, thank you' must reach Gemini: it disqualifies the contact."""
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(1, "m2", _day(2))}, {"ann": {"pipeline_stage": "prospect"}}
    )

    assert queue == ["ann"]


def test_an_unchanged_newest_inbound_is_not_queued():
    """What makes `classify_contact` safe to call on every event."""
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(1, "m2", _day(2))},
        {"ann": {"pipeline_stage": "lead", "pipeline_message_id": "m2"}},
    )

    assert queue == []
    assert tally["unchanged"] == 1


def test_a_new_inbound_message_requeues_a_classified_contact():
    """The re-run path: a newer inbound was stored, so the whole transcript
    is read again and the latest signal wins."""
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(2, "m5", _day(5))},
        {"ann": {"pipeline_stage": "lead", "pipeline_message_id": "m2"}},
    )

    assert queue == ["ann"]


def test_reprocess_all_requeues_everyone_with_an_inbound_message():
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(1, "m2", _day(2)), "bob": _entry(0)},
        {
            "ann": {"pipeline_stage": "lead", "pipeline_message_id": "m2"},
            "bob": {"pipeline_stage": SILENT_STAGE},
        },
        reprocess_all=True,
    )

    assert queue == ["ann"]  # bob has nothing to read
    assert silent == []
    assert tally["unchanged"] == 1


def test_force_requeues_a_named_contact_whose_key_is_unchanged():
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(1, "m2", _day(2))},
        {"ann": {"pipeline_stage": "lead", "pipeline_message_id": "m2"}},
        force={"ann"},
    )

    assert queue == ["ann"]


def test_a_forced_contact_with_no_messages_is_reported_not_ignored():
    silent, queue, tally = plan_pipeline({}, {}, force={"ghost"})

    assert tally["not_found"] == 1


def test_a_silent_contact_never_classified_gets_the_rule():
    silent, queue, tally = plan_pipeline({"ann": _entry(0)}, {"ann": {}})

    assert silent == ["ann"]
    assert queue == []
    assert tally["silent"] == 1


def test_a_silent_contact_already_prospect_is_left_unchanged():
    """A steady-state run must write nothing."""
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(0)}, {"ann": {"pipeline_stage": SILENT_STAGE}}
    )

    assert silent == []
    assert tally["unchanged"] == 1


def test_the_rule_never_overwrites_a_stage_gemini_assigned():
    """Their reply left the window -- deleted on rescan, or the floor moved.
    Absence of evidence does not refute a judgment made on evidence; a reject
    silently turned prospect would be messaged again."""
    silent, queue, tally = plan_pipeline(
        {"ann": _entry(0)},
        {"ann": {"pipeline_stage": "reject", "pipeline_message_id": "m2"}},
    )

    assert silent == []
    assert queue == []
    assert tally["stale"] == 1


def test_a_contact_with_no_analysis_document_is_skipped_and_counted():
    """merge=True mints an absent document; one holding a stage and nothing
    else would flow into every downstream count and the CSV export."""
    silent, queue, tally = plan_pipeline({"ann": _entry(1, "m2", _day(2))}, {})

    assert silent == []
    assert queue == []
    assert tally["missing"] == 1


def test_the_queue_is_newest_inbound_first():
    """So `--limit 30` on a review pass reads the conversations that matter."""
    silent, queue, tally = plan_pipeline(
        {"old": _entry(1, "m1", _day(1)), "new": _entry(1, "m9", _day(9))},
        {"old": {}, "new": {}},
    )

    assert queue == ["new", "old"]


# --- build_prompt -------------------------------------------------------------


def test_prompt_states_what_is_on_file_then_the_profile_then_the_conversation():
    """The model should know who is speaking, and what is already decided
    about them, before it reads them."""
    prompt = build_prompt(
        {
            "summary": "Lab Director at Acme Path",
            "industry": "Pathology",
            "function": "Operations",
            "seniority": "Director",
        },
        "--- conversation 1 ---\n2026-01-01 Them: hi",
    )

    assert prompt == (
        "PROFILE\n"
        "On file, already classified and not to be re-judged: "
        "industry Pathology; function Operations; seniority Director\n"
        "Lab Director at Acme Path\n\n"
        "CONVERSATION\n--- conversation 1 ---\n2026-01-01 Them: hi"
    )


def test_a_missing_profile_or_classification_is_said_rather_than_left_blank():
    """26 of the 617 inbound contacts have no usable summary; they are
    classified from the conversation alone, and the model is told so."""
    assert build_prompt({}, "x").startswith(f"PROFILE\n{NOT_CLASSIFIED}\n{NO_PROFILE}\n")
    assert build_prompt({"summary": "   "}, "x").startswith(
        f"PROFILE\n{NOT_CLASSIFIED}\n{NO_PROFILE}\n"
    )
    assert build_prompt({"industry": "RCM"}, "x").startswith(
        "PROFILE\nOn file, already classified and not to be re-judged: industry RCM\n"
    )


# --- generation_config --------------------------------------------------------


def test_an_unknown_thinking_level_is_rejected_before_any_call():
    """A typo in --thinking-level must fail once, up front -- not 617 times
    inside the gather, each counted as a Gemini failure."""
    import pytest
    from pipeline import generation_config

    with pytest.raises(ValueError):
        generation_config("max")
