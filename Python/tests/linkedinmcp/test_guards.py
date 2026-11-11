"""Tests for `linkedinmcp.guards`: the pure checks that stand between a
message and an action this service takes on LinkedIn.

`validate_text` looks only at the string -- length, an unfilled template
slot, a link to a domain nobody approved. `check_send` looks at everything
else -- the contact's own state, the service's pause/block switches, and the
conversation history -- to decide whether THIS contact may receive THIS
message right NOW. Both run twice per message (once at enqueue time, once
again at send time), which is why `check_send` takes `now` and every other
fact as a plain argument rather than reading anything live.

Fixtures build the smallest dict each rule needs, in the style of
`tests/test_functions.py`: `contact(**overrides)` for an `analysis` document,
`outbound`/`inbound(days_ago)` for a stored message, `queue_item(kind, ...)`
for the item being checked, and `other_item(**overrides)` for a second queue
item read alongside it. `days_ago` also accepts a `timedelta` directly, which
is how the `too_soon` boundary test reaches one second short of a day.
"""

from datetime import UTC, datetime, timedelta

from linkedinmcp import guards
from linkedinmcp.settings import OutreachSettings

#: Fixed instant every test measures "days ago" from, so fixtures never race
#: the real clock. Noon UTC keeps every "N days ago" fixture (N >= 1) safely
#: off today's UTC calendar date, so it never accidentally trips
#: `contact:messaged_today` in a test aimed at some other rule.
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _settings(**overrides):
    """An `OutreachSettings` built with every field `guards` reads pinned to
    a known value, and `_env_file=None` besides -- so nothing here can read a
    developer's real environment or `Python/.env`, the two `_env` in
    `test_settings.py` guards against for the same reason.
    """
    fields = {
        "api_key": "x" * 16,
        "tz": "UTC",
        "message_max_chars": 500,
        "allowed_link_domains": [],
        "min_days_between_touches": 5,
        "max_touches": 3,
    }
    fields.update(overrides)
    return OutreachSettings(_env_file=None, **fields)


def contact(**overrides):
    """A minimal, fully-eligible `analysis` document."""
    base = {"handling": None, "pipeline_stage": None, "intro_sent_at": None, "sent_total": 0}
    base.update(overrides)
    return base


def _touch(is_sender, days_ago, **overrides):
    delta = days_ago if isinstance(days_ago, timedelta) else timedelta(days=days_ago)
    base = {"is_sender": is_sender, "timestamp": NOW - delta, "chat_id": "chat-1"}
    base.update(overrides)
    return base


def outbound(days_ago, **overrides):
    """A usable outbound (`is_sender=1`) message, `days_ago` before `NOW`."""
    return _touch(1, days_ago, **overrides)


def inbound(days_ago, **overrides):
    """A usable inbound (`is_sender=0`) message, `days_ago` before `NOW`."""
    return _touch(0, days_ago, **overrides)


def queue_item(kind="intro", **overrides):
    """The item `check_send` is deciding whether to let through."""
    base = {"kind": kind, "contact_doc_id": "doc-1", "chat_id": None, "approved_by": None}
    base.update(overrides)
    return base


def other_item(**overrides):
    """A second queue item, alongside `item`, in the `queue_items` list."""
    base = {"id": "other-1", "status": "approved"}
    base.update(overrides)
    return base


# =============================================================================
# validate_text
# =============================================================================


def test_validate_text_rejects_a_non_string():
    assert guards.validate_text(None, _settings()).reason == "text:empty"


def test_validate_text_rejects_blank_text():
    assert guards.validate_text("   ", _settings()).reason == "text:empty"


def test_validate_text_rejects_text_over_the_length_cap():
    settings = _settings(message_max_chars=10)

    verdict = guards.validate_text("x" * 11, settings)

    assert verdict.reason == "text:too_long"


def test_validate_text_accepts_text_exactly_at_the_length_cap():
    """The rule is `len(text) > cap`, not `>=` -- the cap itself is fine."""
    settings = _settings(message_max_chars=10)

    assert guards.validate_text("x" * 10, settings) == guards.Verdict(True)


def test_validate_text_rejects_an_unfilled_template_slot():
    verdict = guards.validate_text("Hi {first_name}, thanks for connecting.", _settings())

    assert verdict.reason == "text:unfilled_slot"


def test_validate_text_allows_a_link_to_an_allowed_domain():
    settings = _settings(allowed_link_domains=["example.com"])

    verdict = guards.validate_text("See example.com for details.", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_allows_a_link_to_an_allowed_subdomain():
    settings = _settings(allowed_link_domains=["example.com"])

    verdict = guards.validate_text("See sub.example.com for details.", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_rejects_a_disallowed_https_url():
    settings = _settings(allowed_link_domains=["example.com"])

    verdict = guards.validate_text("Click https://evil.com/phish for a prize.", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_a_bare_disallowed_domain():
    settings = _settings(allowed_link_domains=["example.com"])

    verdict = guards.validate_text("Visit not-allowed.org today.", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_allows_a_www_prefixed_allowed_domain():
    settings = _settings(allowed_link_domains=["example.com"])

    verdict = guards.validate_text("Visit www.example.com today.", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_rejects_every_link_when_the_allowlist_is_empty():
    """An empty `allowed_link_domains` -- the default -- means no domain has
    been reviewed yet, so nothing may be linked, not even a plausible one."""
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("See example.com for details.", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_a_cyrillic_homoglyph_bare_host_under_an_empty_allowlist():
    """A bare host built with a Cyrillic look-alike character (`chr(0x0430)`,
    which renders identically to Latin "a") must still be recognised as a
    link and refused, the same as any other unapproved bare host, rather
    than silently passing because the label character class only matched
    ASCII letters and digits.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("Visit " + chr(0x0430) + "pple.com for details", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_a_cyrillic_homoglyph_bare_host_even_against_the_ascii_lookalike():
    """The Cyrillic-`а` host must not be satisfied by an allowlist entry for
    the ASCII-`a` domain it merely looks like -- `_host_allowed` compares
    strings exactly, and a homoglyph is a different string from the one on
    the allowlist.
    """
    settings = _settings(allowed_link_domains=["apple.com"])

    verdict = guards.validate_text("Visit " + chr(0x0430) + "pple.com for details", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_allows_a_non_ascii_bare_host_that_is_itself_allowlisted():
    """Widening the label character class to Unicode must not merely make
    every non-ASCII host invisible to `_host_allowed` too. The same text is
    refused under an empty allowlist -- proving the widened class captured
    `bücher.com` as a link candidate at all, rather than the pass below
    being vacuous -- and passes once `bücher.com` itself is allowlisted,
    exactly.
    """
    text = "See bücher.com for our catalogue."

    refused = guards.validate_text(text, _settings(allowed_link_domains=[]))
    allowed = guards.validate_text(text, _settings(allowed_link_domains=["bücher.com"]))

    assert refused.reason == "text:link_not_allowed"
    assert allowed == guards.Verdict(True)


def test_validate_text_rejects_a_decomposed_unicode_host_under_an_empty_allowlist():
    """The token "café.com" is "cafe" followed by a combining acute accent
    (U+0301) -- an NFD spelling of "café.com" that renders identically to
    the precomposed form. A combining mark is not `\\w`, so the label class
    -- even widened to Unicode letters/digits in fix round 1 -- ends at the
    mark: `_BARE_HOST_RE` finds no match anywhere in this text, so the host
    must still be refused as a link rather than passing unnoticed.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("Visit cafe\u0301.com today", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_a_fullwidth_full_stop_bare_host_under_an_empty_allowlist():
    """U+FF0E FULLWIDTH FULL STOP renders like an ordinary ".", so
    "evil．com" reads to a person as "evil.com" -- but `_BARE_HOST_RE`
    matches a literal ASCII "." between labels, and U+FF0E is punctuation,
    not a label character, so the un-normalized text has no ASCII "." at
    all for the regex to find.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("Click evil\uff0ecom for a prize.", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_an_ideographic_full_stop_bare_host_under_an_empty_allowlist():
    """U+3002 IDEOGRAPHIC FULL STOP is the CJK sentence-ending dot; IDNA
    (UTS-46) resolves it exactly like "." inside a hostname. Unlike U+FF0E,
    U+3002 has no Unicode decomposition at all -- NFKC normalization alone
    would leave it untouched -- so "evil。com" needs an explicit fold to
    "." to be recognised as the same host as "evil.com".
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("Click evil\u3002com for a prize.", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_fullwidth_letters_spelling_a_bare_host_under_an_empty_allowlist():
    """The token "ｅｖｉｌ.com" spells "evil.com" in fullwidth Latin
    letters (U+FF45, U+FF56, U+FF49, U+FF4C) with a plain ASCII ".". It must
    be refused as a link the same as the ASCII spelling.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("Click \uff45\uff56\uff49\uff4c.com for a prize.", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_allows_an_nfd_spelled_host_that_matches_an_nfc_allowlist_entry():
    """The allowlist is written in ordinary precomposed form ("café.com",
    a single U+00E9 for "é"); the message spells the same host in NFD form
    ("cafe" + a combining U+0301 acute accent) -- the same text as the
    previous test, just now with that exact domain allowlisted. Normalizing
    the link check must not make a legitimately allowlisted domain start
    failing just because the two spellings of "é" are different codepoint
    sequences that render identically.
    """
    settings = _settings(allowed_link_domains=["caf\u00e9.com"])

    verdict = guards.validate_text("Visit cafe\u0301.com today", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_allows_an_nfd_allowlist_entry_to_match_an_nfc_spelled_host():
    """The mirror image of the previous test: here the MESSAGE spells the
    host in ordinary precomposed form (`"caf\\u00e9.com"`, a single U+00E9),
    and it is the ALLOWLIST entry that is written in NFD form
    (`"cafe\\u0301.com"`, "cafe" + a combining U+0301 acute accent) -- as it
    might arrive from a config source that does not canonicalize Unicode.
    `_host_allowed` normalizes each allowlist entry the same way `_nfkc_dots`
    normalizes the text, so this still matches.
    """
    settings = _settings(allowed_link_domains=["cafe\u0301.com"])

    verdict = guards.validate_text("Visit caf\u00e9.com today", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_does_not_treat_abbreviations_as_links():
    """None of `U.S.`, `e.g.` or `Dr. Smith.` has a dot-separated fragment
    that is, by itself, one of the recognised TLDs -- so even the strictest
    possible allowlist (empty) still passes this text.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text(
        "In the U.S., e.g. talk to Dr. Smith. for pricing.", settings
    )

    assert verdict == guards.Verdict(True)


def test_validate_text_still_does_not_treat_extended_tld_lookalikes_or_accented_prose_as_links():
    """Two more non-link cases named in fix round 2, not previously asserted
    directly by any test: `example.comment` and `example.aiweapons` are not
    links -- the TLD alternation only matches `com` / `ai` as a *complete*
    trailing label (`_BARE_HOST_RE`'s `(?![A-Za-z])` lookahead rejects a
    match immediately followed by more letters), which the Unicode
    normalization this round adds to the link check does not change. A
    plain sentence built from accented words, "Café. Merci beaucoup.", is
    not a link either -- normalizing the text does not turn ordinary prose
    into a host.
    """
    settings = _settings(allowed_link_domains=[])

    tld_lookalikes = guards.validate_text(
        "Read the example.comment before you file the example.aiweapons form.", settings
    )
    accented_prose = guards.validate_text("Café. Merci beaucoup.", settings)

    assert tld_lookalikes == guards.Verdict(True)
    assert accented_prose == guards.Verdict(True)


def test_validate_text_rejects_a_zero_width_space_hiding_a_disallowed_prefix():
    """A ZERO WIDTH SPACE (U+200B, Unicode category `Cf`) between "evil" and
    an allowlisted domain must not let the guard read only the fragment
    after it as the host. Real hostname resolution strips format
    characters (`"ev\u200bil.com".encode("idna") == b"evil.com"`), so
    "evil\u200bpinnacleservice.co" resolves to "evilpinnacleservice.co"
    -- a different, unregistered-by-us domain -- and must be refused even
    though "pinnacleservice.co" itself is allowed.
    """
    settings = _settings(allowed_link_domains=["pinnacleservice.co"])

    verdict = guards.validate_text("see evil\u200bpinnacleservice.co today", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_a_soft_hyphen_hiding_a_disallowed_prefix():
    """The same attack as the ZERO WIDTH SPACE case above, with SOFT HYPHEN
    (U+00AD, also category `Cf`) standing in for it -- a different
    invisible format character must not reopen the same gap.
    """
    settings = _settings(allowed_link_domains=["pinnacleservice.co"])

    verdict = guards.validate_text("see evil\u00adpinnacleservice.co today", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_rejects_a_zero_width_space_split_host_that_reassembles_to_a_different_domain():
    """"ev\u200bil.com", with the format character removed, reassembles to
    the single host "evil.com" -- not "il.com", the trailing fragment a
    separator-blind reader would see. "evil.com" is neither equal to nor a
    subdomain of the allowlisted "il.com", so this must still be refused.
    """
    settings = _settings(allowed_link_domains=["il.com"])

    verdict = guards.validate_text("Click ev\u200bil.com now", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_allows_a_zero_width_space_split_host_that_reassembles_to_an_allowed_domain():
    """The positive mirror of the previous tests: a ZERO WIDTH SPACE splits
    the ALLOWED domain itself (a plausible paste artifact, not an attack).
    Removing the format character reassembles "pinnacle\u200bservice.co"
    to exactly "pinnacleservice.co", the allowlisted domain, so this must
    pass rather than being refused for a host that -- once resolved --
    never really existed.
    """
    settings = _settings(allowed_link_domains=["pinnacleservice.co"])

    verdict = guards.validate_text("see pinnacle\u200bservice.co", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_rejects_a_zero_width_space_that_blocks_accent_composition():
    """A ZERO WIDTH SPACE sitting between a base letter and its combining
    accent mark (`"cafe\u200b\u0301.com"`, a corrupted paste of
    "cafe.com" with an accented final e) blocks NFKC's canonical
    composition of the two into one accented letter -- Unicode's
    composition algorithm treats any intervening character as a "starter"
    that blocks composition, regardless of what that character is or
    whether it is itself visible. `_nfkc_dots` must remove `Cf` characters
    BEFORE running NFKC, not after: stripping first leaves the base letter
    directly followed by the combining mark, which NFKC then composes
    normally into a single accented "e"; stripping after NFKC has already
    run leaves the composition permanently blocked, and the host goes
    undetected the same way fix round 2's original decomposed-accent case
    did.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text("Visit cafe\u200b\u0301.com today", settings)

    assert verdict.reason == "text:link_not_allowed"


def test_validate_text_allows_prose_with_an_emoji_zwj_sequence_and_no_link():
    """A ZERO WIDTH JOINER (U+200D) joining two emoji in ordinary prose --
    hiding no link -- is not by itself grounds for refusal. Format
    characters are removed only to reassemble what a link's host would
    resolve to; their mere presence in a message that contains no link is
    not a reason to reject it.
    """
    settings = _settings(allowed_link_domains=[])

    verdict = guards.validate_text(
        "Great chat \U0001f469\u200d\U0001f4bb thanks!", settings
    )

    assert verdict == guards.Verdict(True)


def test_validate_text_does_not_treat_a_url_path_segment_as_a_second_link():
    """A path segment that happens to look like a domain is not read as a
    second, independent link -- `https://example.io/go.com` links only to
    `example.io`, not also to `go.com`."""
    settings = _settings(allowed_link_domains=["example.io"])

    verdict = guards.validate_text("Open https://example.io/go.com now.", settings)

    assert verdict == guards.Verdict(True)


def test_validate_text_passes_a_fully_valid_message():
    settings = _settings(allowed_link_domains=["example.com"])

    verdict = guards.validate_text(
        "Hi Jordan, thanks for connecting -- see example.com for our overview.",
        settings,
    )

    assert verdict == guards.Verdict(True)


# =============================================================================
# check_send -- contact-level rules
# =============================================================================


def test_check_send_refuses_when_contact_is_not_found():
    verdict = guards.check_send(queue_item("intro"), None, [], [], {}, _settings(), NOW)

    assert verdict.reason == "contact:not_found"


def test_check_send_refuses_a_held_contact():
    verdict = guards.check_send(
        queue_item("intro"), contact(handling="exclude"), [], [], {}, _settings(), NOW
    )

    assert verdict.reason == "contact:held"


def test_check_send_refuses_a_stage_blocked_contact():
    verdict = guards.check_send(
        queue_item("intro"), contact(pipeline_stage="reject"), [], [], {}, _settings(), NOW
    )

    assert verdict.reason == "contact:stage_blocked"


def test_check_send_a_held_and_stage_blocked_contact_reports_held_first():
    """Rule order: `contact:held` is checked before `contact:stage_blocked`,
    so a contact that trips both reports the first, not the second."""
    verdict = guards.check_send(
        queue_item("intro"),
        contact(handling="manual", pipeline_stage="reject"),
        [], [], {}, _settings(), NOW,
    )

    assert verdict.reason == "contact:held"


def test_check_send_refuses_when_writes_are_blocked():
    state = {"writes_blocked_at": NOW - timedelta(days=1)}

    verdict = guards.check_send(queue_item("intro"), contact(), [], [], state, _settings(), NOW)

    assert verdict.reason == "state:writes_blocked"


def test_check_send_refuses_when_sends_are_paused():
    state = {"sends_paused_until": NOW + timedelta(hours=1)}

    verdict = guards.check_send(queue_item("intro"), contact(), [], [], state, _settings(), NOW)

    assert verdict.reason == "state:sends_paused"


# =============================================================================
# check_send -- contact:messaged_today
# =============================================================================


def test_check_send_refuses_a_contact_already_messaged_today_by_us():
    verdict = guards.check_send(
        queue_item("follow_up", chat_id="chat-1"),
        contact(),
        [outbound(0)],
        [], {}, _settings(), NOW,
    )

    assert verdict.reason == "contact:messaged_today"


def test_check_send_refuses_when_another_queue_item_sent_today():
    other = other_item(status="sent", sent_at=NOW)

    verdict = guards.check_send(
        queue_item("follow_up", chat_id="chat-1", id="this-item"),
        contact(),
        [], [other], {}, _settings(), NOW,
    )

    assert verdict.reason == "contact:messaged_today"


def test_check_send_the_item_being_checked_does_not_count_against_itself():
    """`queue_items` may already include the item being checked, matched by
    `id`. Its own `sending_at` must not trigger `messaged_today` against
    itself -- proven here by reaching `Verdict(True)`: everything else about
    this call is a fully eligible follow-up.
    """
    self_item = queue_item("follow_up", chat_id="chat-1", id="self-1")
    self_in_queue = other_item(id="self-1", status="sending", sending_at=NOW)

    verdict = guards.check_send(
        self_item, contact(), [outbound(10)], [self_in_queue], {}, _settings(), NOW
    )

    assert verdict == guards.Verdict(True)


def test_check_send_a_missing_item_id_never_matches_another_missing_id():
    """`item` has no `id` yet at enqueue time (`item.get("id")` is `None`).
    A stored queue item should never lack an `id` (`queue.get` always
    attaches one), but if one somehow did, it must not be treated as `item`
    itself just because both read as `None` -- that would silently swallow a
    real same-day conflict, the exact duplicate send this rule exists to
    catch.
    """
    item = queue_item("follow_up", chat_id="chat-1")  # no "id" set
    other_with_no_id = other_item(id=None, status="sent", sent_at=NOW)

    verdict = guards.check_send(
        item, contact(), [], [other_with_no_id], {}, _settings(), NOW
    )

    assert verdict.reason == "contact:messaged_today"


def test_check_send_messaged_today_ignores_events_deleted_and_bad_is_sender():
    """Three messages timestamped today, none of which may count: a system
    event, a deleted message, and a corrupt `is_sender`. Proven by reaching
    `follow_up:no_prior_message` -- if any of them counted as a real outbound
    message, this would pass instead.
    """
    messages = [
        outbound(0, is_event=1),
        outbound(0, deleted=1),
        {"is_sender": 2, "timestamp": NOW, "chat_id": "chat-1"},
    ]

    verdict = guards.check_send(
        queue_item("follow_up", chat_id="chat-1"), contact(), messages, [], {}, _settings(), NOW
    )

    assert verdict.reason == "follow_up:no_prior_message"


def test_check_send_messaged_today_uses_the_local_date_not_utc():
    """03:00 UTC on 2026-01-15 is still 2026-01-14 in New York (UTC-5), so an
    outbound message at that instant does not count as sent "today" when
    `now` is noon UTC the same day -- it is one calendar day too soon to
    trigger `follow_up:too_soon` instead. The UTC call is the control: same
    message, same `now`, only the zone changes, and it DOES trip
    `contact:messaged_today` -- proving the New York result above actually
    depends on `settings.tz` rather than some other reason the guard passed.
    """
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    early_utc_message = {
        "is_sender": 1,
        "timestamp": datetime(2026, 1, 15, 3, 0, tzinfo=UTC),
        "chat_id": "chat-1",
    }
    item = queue_item("follow_up", chat_id="chat-1")

    ny_verdict = guards.check_send(
        item, contact(), [early_utc_message], [], {}, _settings(tz="America/New_York"), now
    )
    utc_verdict = guards.check_send(
        item, contact(), [early_utc_message], [], {}, _settings(tz="UTC"), now
    )

    assert ny_verdict.reason == "follow_up:too_soon"
    assert utc_verdict.reason == "contact:messaged_today"


# =============================================================================
# check_send -- intro kind
# =============================================================================


def test_check_send_intro_refuses_when_a_conversation_already_exists():
    """Any usable message counts, in either direction -- here an inbound one,
    so this is not incidentally also a `contact:messaged_today` test, which
    looks only at outbound messages."""
    verdict = guards.check_send(
        queue_item("intro"), contact(), [inbound(2)], [], {}, _settings(), NOW
    )

    assert verdict.reason == "intro:conversation_exists"


def test_check_send_intro_refuses_when_already_sent():
    verdict = guards.check_send(
        queue_item("intro"),
        contact(intro_sent_at=NOW - timedelta(days=3)),
        [], [], {}, _settings(), NOW,
    )

    assert verdict.reason == "intro:already_sent"


def test_check_send_intro_refuses_when_already_messaged():
    verdict = guards.check_send(
        queue_item("intro"), contact(sent_total=2), [], [], {}, _settings(), NOW
    )

    assert verdict.reason == "intro:already_messaged"


def test_check_send_a_fully_eligible_intro_passes():
    verdict = guards.check_send(queue_item("intro"), contact(), [], [], {}, _settings(), NOW)

    assert verdict == guards.Verdict(True)


# =============================================================================
# check_send -- follow_up / drip_step kind
# =============================================================================


def test_check_send_follow_up_refuses_with_no_conversation():
    verdict = guards.check_send(
        queue_item("follow_up", chat_id=None), contact(), [], [], {}, _settings(), NOW
    )

    assert verdict.reason == "follow_up:no_conversation"


def test_check_send_follow_up_refuses_with_no_prior_message():
    """An inbound-only conversation IS a conversation -- `no_conversation`
    does not fire -- but a follow-up follows something WE sent, and we never
    sent anything here."""
    verdict = guards.check_send(
        queue_item("follow_up", chat_id=None), contact(), [inbound(2)], [], {}, _settings(), NOW
    )

    assert verdict.reason == "follow_up:no_prior_message"


def test_check_send_follow_up_refuses_when_a_reply_is_pending():
    messages = [outbound(10), inbound(2)]

    verdict = guards.check_send(
        queue_item("follow_up", chat_id="chat-1"), contact(), messages, [], {}, _settings(), NOW
    )

    assert verdict.reason == "follow_up:reply_pending"


def test_check_send_follow_up_too_soon_boundary():
    """Exactly `min_days_between_touches` days since the last touch passes;
    one second less refuses."""
    settings = _settings(min_days_between_touches=5)
    item = queue_item("follow_up", chat_id="chat-1")

    at_the_boundary = guards.check_send(
        item, contact(), [outbound(timedelta(days=5))], [], {}, settings, NOW
    )
    one_second_short = guards.check_send(
        item,
        contact(),
        [outbound(timedelta(days=5) - timedelta(seconds=1))],
        [], {}, settings, NOW,
    )

    assert at_the_boundary == guards.Verdict(True)
    assert one_second_short.reason == "follow_up:too_soon"


def test_check_send_follow_up_refuses_at_the_touch_cap():
    """`sent_total` alone can trip the cap even with fewer outbound messages
    visible locally -- the rule is `max(sent_total, len(outbound))`."""
    settings = _settings(max_touches=3, min_days_between_touches=5)

    verdict = guards.check_send(
        queue_item("follow_up", chat_id="chat-1"),
        contact(sent_total=3),
        [outbound(10)],
        [], {}, settings, NOW,
    )

    assert verdict.reason == "follow_up:max_touches"


def test_check_send_a_fully_eligible_follow_up_passes():
    settings = _settings(min_days_between_touches=5, max_touches=3)

    verdict = guards.check_send(
        queue_item("follow_up", chat_id="chat-1"),
        contact(sent_total=1),
        [outbound(10)],
        [], {}, settings, NOW,
    )

    assert verdict == guards.Verdict(True)


def test_check_send_drip_step_shares_the_follow_up_reason_codes():
    """`drip_step` is governed by the same rules as `follow_up`, reported
    under the same `follow_up:*` codes -- there is no separate `drip_step:*`
    namespace."""
    settings = _settings(min_days_between_touches=5)

    verdict = guards.check_send(
        queue_item("drip_step", chat_id="chat-1"),
        contact(),
        [outbound(1)],
        [], {}, settings, NOW,
    )

    assert verdict.reason == "follow_up:too_soon"


# =============================================================================
# check_send -- reply kind
# =============================================================================


def test_check_send_reply_refuses_with_no_conversation():
    verdict = guards.check_send(
        queue_item("reply", chat_id=None), contact(), [], [], {}, _settings(), NOW
    )

    assert verdict.reason == "reply:no_conversation"


def test_check_send_reply_refuses_when_there_is_nothing_to_answer():
    """No inbound message at all counts as nothing to answer -- and so does
    an inbound message that is older than our own last reply, since we
    already had the last word."""
    item = queue_item("reply", chat_id="chat-1")

    no_inbound_at_all = guards.check_send(
        item, contact(), [outbound(2)], [], {}, _settings(), NOW
    )
    already_answered = guards.check_send(
        item, contact(), [outbound(2), inbound(5)], [], {}, _settings(), NOW
    )

    assert no_inbound_at_all.reason == "reply:nothing_to_answer"
    assert already_answered.reason == "reply:nothing_to_answer"


def test_check_send_reply_needs_approval_unless_enqueueing():
    messages = [outbound(5), inbound(1)]
    item = queue_item("reply", chat_id="chat-1", approved_by=None)

    at_enqueue_time = guards.check_send(
        item, contact(), messages, [], {}, _settings(), NOW, enqueueing=True
    )
    at_send_time = guards.check_send(
        item, contact(), messages, [], {}, _settings(), NOW, enqueueing=False
    )

    assert at_enqueue_time == guards.Verdict(True)
    assert at_send_time.reason == "reply:needs_approval"


def test_check_send_a_fully_eligible_reply_passes():
    messages = [outbound(5), inbound(1)]

    verdict = guards.check_send(
        queue_item("reply", chat_id="chat-1", approved_by="human"),
        contact(), messages, [], {}, _settings(), NOW,
    )

    assert verdict == guards.Verdict(True)


# =============================================================================
# check_send -- unknown kind
# =============================================================================


def test_check_send_refuses_an_unknown_kind():
    verdict = guards.check_send(queue_item("bogus"), contact(), [], [], {}, _settings(), NOW)

    assert verdict.reason == "item:unknown_kind"
