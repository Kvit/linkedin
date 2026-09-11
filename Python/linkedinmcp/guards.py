"""Whether a message's TEXT is safe to send, and whether SENDING it now is
safe -- the two checks that stand between a stranger's message and an action
this service takes on LinkedIn. `validate_text` looks only at the string
itself: length, an unfilled `{template_slot}`, a link to a domain nobody
approved. `check_send` looks at everything else -- the contact's own state,
the service's paused/blocked switches, and the conversation history -- to
decide whether THIS contact may receive THIS message right now.

Both run twice for every message that goes out: once when the agent queues it
(`enqueueing=True`), and again immediately before the send, potentially hours
later, when a reply may have arrived, a day may have turned over, or a human
may have changed the contact's `handling`. `check_send` is deliberately
identical at both points except for one rule that cannot possibly hold at
enqueue time -- a `reply` needs a human's approval, and approval necessarily
comes after the item exists to approve.

This module is PURE: no Firestore, no network, no wall-clock read. `now` is
always the caller's. The one import this module needs that is not free --
`functions.HANDLING_HOLDS`, which drags in `google.cloud.firestore_v1`
through `functions.py`'s own module-level import -- is deferred inside
`check_send`, so importing `guards` itself stays cheap and side-effect free.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from linkedinmcp import clock

#: `pipeline_stage` values that hold a contact back. Distinct from
#: `functions.HANDLING_HOLDS` -- a separate, hand-maintained field -- so a
#: contact can be blocked by either, or both, independently.
BLOCKED_STAGES = frozenset({"reject", "not_relevant", "soft_no"})

#: `outreach_queue` statuses that, alongside a matching timestamp field,
#: count as "already went out today" for a queue item other than the one
#: being checked. See `_messaged_today`.
_MESSAGED_TODAY_STATUSES = frozenset({"sent", "unknown", "sending"})
_MESSAGED_TODAY_FIELDS = ("sent_at", "settled_at", "sending_at")

#: `{first_name}` left unfilled -- a leading letter or underscore keeps this
#: from matching `{}` or `{3}`, neither of which is a template slot.
_SLOT_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

#: TLDs a bare (schemeless) host is recognised by. Matching this exact set,
#: rather than a prefix, is what keeps "example.comment" from being read as
#: a link to ".com" -- see `_BARE_HOST_RE`'s trailing lookahead.
_BARE_TLDS = (
    "com", "net", "org", "io", "ai", "co", "us",
    "health", "info", "biz", "app", "me", "ly", "gl",
)
#: The label class is Unicode-aware (`[^\W_]` is any Unicode letter or
#: digit, `re` being Unicode-aware by default) so a non-ASCII look-alike
#: host -- e.g. Cyrillic "а" standing in for Latin "a" -- is still captured
#: as a link candidate and sent through `_host_allowed`, rather than
#: silently skipped because it fell outside an ASCII-only class.
_BARE_HOST_RE = re.compile(
    r"\b(?:[^\W_](?:(?:[^\W_]|-)*[^\W_])?\.)+"
    r"(?:" + "|".join(_BARE_TLDS) + r")(?![A-Za-z])",
    re.IGNORECASE,
)

#: Alternative "dot" characters that IDNA (UTS-46) resolves to U+002E FULL
#: STOP when reading a hostname, so a browser treats "evil．com" or
#: "evil。com" exactly like "evil.com". Checked empirically
#: (`unicodedata.normalize("NFKC", ...)`), not assumed: U+FF0E (FULLWIDTH
#: FULL STOP) decomposes under NFKC straight to U+002E; U+FF61 (HALFWIDTH
#: IDEOGRAPHIC FULL STOP) decomposes under NFKC to U+3002, not U+002E; and
#: U+3002 (IDEOGRAPHIC FULL STOP) has no decomposition at all -- it is an
#: independent character, not a compatibility variant of anything -- so
#: NFKC alone leaves both U+3002 itself and (via U+FF61) one step short of
#: "." and each is folded the rest of the way here explicitly.
_DOT_LOOKALIKES = ("\u3002", "\uff0e", "\uff61")


def _nfkc_dots(text: str) -> str:
    """`text` with every Unicode format character (category `Cf` -- ZERO
    WIDTH SPACE U+200B, SOFT HYPHEN U+00AD, ZERO WIDTH NON-JOINER U+200C,
    ZERO WIDTH JOINER U+200D, WORD JOINER U+2060, the U+FEFF byte-order
    mark, and the rest of that category) removed, then NFKC-normalized,
    then with `_DOT_LOOKALIKES` folded to an ASCII ".". NFKC alone already
    defeats a link spelled with combining marks ("cafe" + a combining
    acute accent composes to "cafe" with a single accented "e") or
    compatibility/fullwidth letters (fullwidth "evil" folds to plain
    "evil"); the explicit dot fold on top of it defeats the one alternate
    IDNA dot character (U+3002, and U+FF61 once NFKC has reduced it to
    U+3002) that NFKC does not itself reduce all the way to ".".

    The `Cf` strip runs FIRST, before NFKC, not after. A `Cf` character
    survives NFKC unchanged (verified empirically: NFKC does not delete or
    map it away), so stripping it afterward would still work for a lone
    invisible character sitting between two ordinary letters. But a `Cf`
    character sitting between a base letter and its combining accent mark
    -- e.g. "e" + ZERO WIDTH SPACE + COMBINING ACUTE ACCENT, a corrupted
    paste of an accented "e" -- blocks NFKC's canonical composition of the
    two into one letter: Unicode's composition algorithm treats ANY
    intervening character as a "starter" that blocks composition,
    regardless of what that character is or whether it is itself visible
    (confirmed empirically: `unicodedata.normalize("NFKC", "e" +
    "\\u200b" + "\\u0301")` leaves all three codepoints separate, while
    stripping the ZERO WIDTH SPACE first and running NFKC on what remains
    composes it to a single accented "e"). Stripping `Cf` after NFKC would
    leave that composition permanently blocked, and the host would go
    undetected the same way fix round 2's original decomposed-accent
    finding did.

    Real hostname resolution strips `Cf` characters the same way this
    function does (`"ev\\u200bil.com".encode("idna") == b"evil.com"`), so
    without this step a bare host written with one embedded -- e.g.
    "evil\\u200bpinnacleservice.co" -- reads to a human, and resolves, as
    a single host, but the bare-host regex sees the format character as a
    separator (it is not `\\w`) and matches only the fragment after it,
    checking a different, shorter host against the allowlist than the one
    that would actually be visited. This normalization never rejects text
    for merely containing a `Cf` character -- it only changes what
    `_links_in` searches for a link inside; text with no link still passes
    regardless of what invisible characters it contains.
    """
    stripped = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    normalized = unicodedata.normalize("NFKC", stripped)
    for lookalike in _DOT_LOOKALIKES:
        normalized = normalized.replace(lookalike, ".")
    return normalized


@dataclass(frozen=True)
class Verdict:
    """The result of a guard check. A pass is always plain `Verdict(True)`
    -- `reason` stays `"ok"`, `detail` stays empty. A refusal carries one of
    the reason codes documented on `validate_text` / `check_send`, verbatim,
    plus one sentence a human can act on.
    """

    ok: bool
    reason: str = "ok"
    detail: str = ""


def validate_text(text: str, settings) -> Verdict:
    """Whether `text` is fit to send. Checked in this order; the first
    failure wins:

    - `text:empty` -- not a `str`, or blank once stripped.
    - `text:too_long` -- longer than `settings.message_max_chars`. Measured
      on `text` exactly as given -- this runs before the Unicode
      normalization below, so it can never be fooled by normalization
      changing the character count.
    - `text:unfilled_slot` -- still contains a `{template_slot}`.
    - `text:link_not_allowed` -- links to a host outside
      `settings.allowed_link_domains`.

    A "link" is any `http(s)://` URL, or a bare host such as
    `www.example.com` / `example.com/path` ending in a recognised TLD (see
    `_BARE_TLDS`) -- so `U.S.`, `e.g.` and `Dr. Smith.` are not links: none of
    their dot-separated fragments is, by itself, one of those TLDs. Before
    this check only, `text` is passed through `_nfkc_dots` -- removing every
    Unicode format character (category `Cf`, e.g. a ZERO WIDTH SPACE or
    SOFT HYPHEN), NFKC normalization, and folding the alternate IDNA "dot"
    characters to "." -- so a host spelled with combining marks,
    compatibility/fullwidth letters, one of those alternate dots, or split
    by an invisible format character is read exactly like its plain ASCII
    form, rather than slipping past undetected or being reassembled from
    only the fragment on one side of the invisible character. A host is
    allowed when it -- lowercased, a leading `www.` dropped -- equals an
    `allowed_link_domains` entry or ends with `"." + entry`. An empty
    allowlist allows nothing, so every link is refused. This normalization
    never rejects `text` merely for containing a `Cf` character; it only
    changes what this check searches for a link inside, so prose with no
    link -- an emoji ZERO WIDTH JOINER sequence included -- still passes.
    """
    if not isinstance(text, str) or not text.strip():
        return Verdict(False, "text:empty", "The message text is empty.")

    if len(text) > settings.message_max_chars:
        return Verdict(
            False,
            "text:too_long",
            f"The message is {len(text)} characters; the limit is {settings.message_max_chars}.",
        )

    if _SLOT_RE.search(text):
        return Verdict(
            False, "text:unfilled_slot", "The message still contains an unfilled template slot."
        )

    for host in _links_in(_nfkc_dots(text)):
        if not _host_allowed(host, settings.allowed_link_domains):
            label = host or "a link"
            return Verdict(
                False,
                "text:link_not_allowed",
                f"The message links to {label!r}, which is not an allowed domain.",
            )

    return Verdict(True)


def _links_in(text: str):
    """Every link's host in `text`, lowercased with a leading `www.` dropped:
    first the host of every `http(s)://` URL, then every bare host found in
    what is left once those URLs are blanked out.

    `text` is expected to already be Unicode-normalized -- `validate_text`,
    this function's one caller, passes it through `_nfkc_dots` first. This
    function does no normalization of its own, so calling it directly on
    raw text would miss a host spelled with combining marks or an alternate
    dot character, or would read an invisible Unicode format character
    (e.g. a ZERO WIDTH SPACE) as a host separator and match only the
    fragment that follows it.

    Blanking matters. Without it, a path segment that merely looks like a
    domain -- `https://example.io/go.com` -- would be read as a second,
    independent link, on top of the real one to `example.io`.

    A URL whose host cannot be parsed out at all yields `""`, which never
    matches an allowlist entry -- fail closed, since rule (a) counts every
    `http(s)://` URL as a link regardless of whether this module can make
    sense of it.
    """
    masked = list(text)
    for match in _URL_RE.finditer(text):
        start, end = match.span()
        for i in range(start, end):
            masked[i] = " "
        url = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        yield _normalize_host(host) if host else ""

    for match in _BARE_HOST_RE.finditer("".join(masked)):
        yield _normalize_host(match.group(0))


def _normalize_host(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _host_allowed(host: str, allowed_domains) -> bool:
    """`host` -- already `_nfkc_dots`-normalized and `_normalize_host`-cased
    by the time it reaches here -- is allowed when it equals or is a
    subdomain of some `allowed_link_domains` entry. Each entry is put
    through the same two steps, in the same order (`_nfkc_dots` first, then
    lowercase), before the comparison: an entry that itself arrived in a
    non-canonical Unicode form -- e.g. NFD, from a config source that does
    not canonicalize -- must still match a link spelled in the ordinary
    precomposed form, and vice versa.
    """
    if not host:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domain in (_nfkc_dots(entry).lower() for entry in allowed_domains)
    )


def check_send(
    item: dict,
    contact: dict | None,
    messages: list[dict],
    queue_items: list[dict],
    state: dict,
    settings,
    now: datetime,
    *,
    enqueueing: bool = False,
) -> Verdict:
    """Whether `item` may be sent to `contact` right now. Rules run in this
    order; the first failure wins:

    1. `contact:not_found` -- `contact is None`.
    2. `contact:held` -- `contact["handling"]` is in `functions.HANDLING_HOLDS`.
    3. `contact:stage_blocked` -- `contact["pipeline_stage"]` is in `BLOCKED_STAGES`.
    4. `state:writes_blocked` -- `state["writes_blocked_at"]` is set.
    5. `state:sends_paused` -- `state["sends_paused_until"]` is later than `now`.
    6. `contact:messaged_today` -- see `_messaged_today`.
    7. Kind-specific rules -- see `_check_intro`, `_check_follow_up`, `_check_reply`.

    `messages` is filtered to what is usable before any of this runs: an
    entry needs `is_sender` exactly `0` or `1`, a `timestamp`, and neither
    `is_event == 1` nor `deleted == 1`. `queue_items` is `contact`'s other
    queue items; one whose `id` equals `item.get("id")` is `item` itself and
    never counts against it.

    `enqueueing=True` is the pre-check `send_follow_up` and `send_reply` run before `item`
    exists in storage: it skips only `reply:needs_approval`, which reads
    `item["approved_by"]` -- set only once a human has actually approved a
    queued item, necessarily after enqueueing. Every other rule runs exactly
    the same way at enqueue time and at send time.
    """
    from functions import HANDLING_HOLDS  # deferred: pulls in google-cloud-firestore

    if contact is None:
        return Verdict(False, "contact:not_found", "No analysis document exists for this contact.")

    handling = str(contact.get("handling") or "").strip().lower()
    if handling in HANDLING_HOLDS:
        return Verdict(False, "contact:held", f"The contact's handling ({handling!r}) holds outreach back.")

    stage = str(contact.get("pipeline_stage") or "").strip().lower()
    if stage in BLOCKED_STAGES:
        return Verdict(
            False, "contact:stage_blocked", f"The contact's pipeline stage ({stage!r}) blocks outreach."
        )

    if state.get("writes_blocked_at"):
        return Verdict(False, "state:writes_blocked", "LinkedIn writes are blocked for this account.")

    paused_until = state.get("sends_paused_until")
    if paused_until is not None and paused_until > now:
        return Verdict(False, "state:sends_paused", f"Sends are paused until {paused_until.isoformat()}.")

    today = clock.local_date(now, settings.tz)
    usable = _usable_messages(messages)
    outbound = [m for m in usable if m["is_sender"] == 1]
    inbound = [m for m in usable if m["is_sender"] == 0]

    if _messaged_today(outbound, item, queue_items, today, settings.tz):
        return Verdict(False, "contact:messaged_today", "This contact was already messaged today.")

    newest_outbound = max((m["timestamp"] for m in outbound), default=None)
    newest_inbound = max((m["timestamp"] for m in inbound), default=None)
    kind = item.get("kind")

    if kind == "intro":
        return _check_intro(contact, usable)
    if kind in ("follow_up", "drip_step"):
        return _check_follow_up(item, contact, usable, outbound, newest_outbound, newest_inbound, settings, now)
    if kind == "reply":
        return _check_reply(item, usable, newest_outbound, newest_inbound, enqueueing)
    return Verdict(False, "item:unknown_kind", f"Unknown queue item kind: {kind!r}.")


def _usable_messages(messages: list[dict]) -> list[dict]:
    """Messages fit to reason about: `is_sender` exactly `0` or `1`, a
    `timestamp`, not an event, not deleted. Everything else -- a system
    event, a deleted message, a corrupt `is_sender` -- is dropped rather than
    guessed at.
    """
    return [
        m
        for m in messages
        if m.get("is_sender") in (0, 1)
        and m.get("timestamp") is not None
        and m.get("is_event") != 1
        and m.get("deleted") != 1
    ]


def _messaged_today(outbound: list[dict], item: dict, queue_items: list[dict], today, tz: str) -> bool:
    """True when this contact was already touched today: one of `outbound`'s
    timestamps falls on `today` (in `tz`), or another queue item -- anything
    in `queue_items` whose `id` is not `item.get("id")` -- is `sent`,
    `unknown` or `sending` with a `sent_at`, `settled_at` or `sending_at` on
    `today`.
    """
    if any(clock.local_date(m["timestamp"], tz) == today for m in outbound):
        return True

    self_id = item.get("id")
    for other in queue_items:
        if self_id is not None and other.get("id") == self_id:
            continue
        if other.get("status") not in _MESSAGED_TODAY_STATUSES:
            continue
        for field in _MESSAGED_TODAY_FIELDS:
            value = other.get(field)
            if value is not None and clock.local_date(value, tz) == today:
                return True
    return False


def _check_intro(contact: dict, usable: list[dict]) -> Verdict:
    if usable:
        return Verdict(False, "intro:conversation_exists", "A conversation with this contact already exists.")
    if contact.get("intro_sent_at"):
        return Verdict(False, "intro:already_sent", "The intro has already been sent to this contact.")
    if contact.get("sent_total"):
        return Verdict(False, "intro:already_messaged", "This contact has already been messaged.")
    return Verdict(True)


def _check_follow_up(
    item: dict,
    contact: dict,
    usable: list[dict],
    outbound: list[dict],
    newest_outbound,
    newest_inbound,
    settings,
    now: datetime,
) -> Verdict:
    if not item.get("chat_id") and not usable:
        return Verdict(False, "follow_up:no_conversation", "There is no conversation to follow up on.")
    if newest_outbound is None:
        return Verdict(
            False, "follow_up:no_prior_message", "There is no prior outbound message to follow up on."
        )
    if newest_inbound is not None and newest_inbound > newest_outbound:
        return Verdict(
            False,
            "follow_up:reply_pending",
            "They replied; this needs a human-approved reply, not a follow-up.",
        )
    if now - newest_outbound < timedelta(days=settings.min_days_between_touches):
        return Verdict(
            False,
            "follow_up:too_soon",
            f"Fewer than {settings.min_days_between_touches} days have passed since the last touch.",
        )
    touches = max(contact.get("sent_total") or 0, len(outbound))
    if touches >= settings.max_touches:
        return Verdict(
            False, "follow_up:max_touches", f"This contact has reached the {settings.max_touches}-touch cap."
        )
    return Verdict(True)


def _check_reply(
    item: dict, usable: list[dict], newest_outbound, newest_inbound, enqueueing: bool
) -> Verdict:
    if not item.get("chat_id") and not usable:
        return Verdict(False, "reply:no_conversation", "There is no conversation to reply to.")
    if newest_inbound is None or (newest_outbound is not None and newest_inbound <= newest_outbound):
        return Verdict(
            False,
            "reply:nothing_to_answer",
            "There is no inbound message newer than our last one to answer.",
        )
    if not enqueueing and item.get("approved_by") != "human":
        return Verdict(
            False, "reply:needs_approval", "A reply must be approved by a human before it can send."
        )
    return Verdict(True)
