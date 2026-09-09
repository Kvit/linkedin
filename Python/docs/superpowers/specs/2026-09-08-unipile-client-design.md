# Unipile Client Library — Design

**Date:** 2026-09-08
**Status:** Implemented 2026-09-08; nine review findings fixed (135 unit tests, 7 live read-only tests)
**Location:** `Python/lib/unipile/`

## 1. Purpose

Replace LinkedIn Helper as the mechanism for all contact and message operations
against LinkedIn, using the Unipile API. The library is a **pure API client**: it
performs HTTP, validates responses, paginates, and enforces send budgets. It does
not touch Firestore, does not run campaigns, and does not classify profiles.

Orchestration — which contacts to fetch, what to classify, what to write to
Firestore — stays in the existing notebooks and scripts.

## 2. Scope

**In scope** (the four LinkedIn Helper surfaces being replaced):

1. **Invitations** — send, list sent, list received, accept/decline, withdraw.
2. **Messaging** — chats, messages, attendees, send to an existing or new chat.
3. **Profiles & relations** — retrieve profile, list relations, followers, following.
4. **Search** — LinkedIn classic search and search-parameter resolution.

**Out of scope:** Firestore reads/writes, campaign sequencing and cadence state,
jobs, posts, email, calendar, webhooks, async I/O.

## 3. Architecture

Layered, sync-only. Each layer is independently testable and depends only on the
layer beneath it.

```
Python/lib/
  __init__.py              # makes `from lib.unipile import ...` resolve
  unipile/
    __init__.py            # public exports
    config.py              # UnipileSettings — env-driven configuration
    errors.py              # exception hierarchy keyed on the API `type` string
    transport.py           # httpx.Client, auth, retry policy, error mapping,
                           #   multipart encoding, circuit breaker
    pagination.py          # cursor-style and page_count-style iterators
    budget.py              # SendBudget — file-backed daily counters + reconcile
    identity.py            # slug_from_profile_url(), is_provider_id()
    models.py              # Pydantic models (extra="allow")
    compat.py              # to_lh_document() — LinkedIn Helper shape adapter
    resources/
      __init__.py
      accounts.py
      users.py             # profiles, relations, invitations
      messaging.py         # chats, messages, attendees
      search.py
    client.py              # UnipileClient facade
```

Sync only. Notebooks are the primary consumer and the domain is rate-limited;
concurrency would be a liability, not a feature.

## 4. Configuration

All configuration comes from the environment (loaded from `.env` via
`pydantic-settings`, prefix `UNIPILE_`). **`api_key` and `dns` have no defaults** —
the OpenAPI spec's default host (`api1.unipile.com:13111`) is wrong for this
tenant and returns `503 no_client_session`.

| Variable | Default | Meaning |
|---|---|---|
| `UNIPILE_API_KEY` | *(required)* | Sent as the `X-API-KEY` header |
| `UNIPILE_DNS` | *(required)* | e.g. `api62.unipile.com:19262` |
| `UNIPILE_ACCOUNT_ID` | *(resolved)* | Falls back to `GET /accounts` on first use |
| `UNIPILE_MAX_INVITES_PER_DAY` | `25` | Daily invite cap |
| `UNIPILE_MAX_MESSAGES_PER_DAY` | `50` | Daily message cap |
| `UNIPILE_MAX_PROFILE_FETCHES_PER_DAY` | `250` | Profile fetches are the throttled read |
| `UNIPILE_MIN_DELAY_SECONDS` | `4` | Lower bound of randomized inter-call delay |
| `UNIPILE_MAX_DELAY_SECONDS` | `12` | Upper bound |
| `UNIPILE_USAGE_WARN_PCT` | `75` | Log a warning at this provider usage level |
| `UNIPILE_USAGE_HALT_PCT` | `90` | Refuse further writes at this level |
| `UNIPILE_BUDGET_STATE_PATH` | `.unipile_budget.json` | Counter file |
| `UNIPILE_PROFILE_SECTIONS` | `about,experience,education,skills,certifications,languages,projects` | Default `linkedin_sections` selector |
| `UNIPILE_TIMEOUT_SECONDS` | `30` | HTTP timeout |

## 5. Transport

### 5.1 The core rule: reads retry, writes never

`POST /users/invite`, `POST /chats`, and `POST /chats/{id}/messages` are **not
idempotent**. Retrying after a timeout sends a real second invitation or message
to a real person.

- **Reads** — retry up to 3 attempts with exponential backoff and jitter on
  network errors and `500`, `502`, `503`, `504`.
- **Writes** — never retried. Any failure raises on the first attempt.
- **`429` anywhere** — raises `RateLimited` on reads and writes alike, and is
  never slept-and-retried. A 429 is the provider saying stop, and profile
  fetches are the read it throttles hardest. Reads retry on 5xx and network
  errors only.

### 5.2 Circuit breaker

A `403 errors/account_restricted` sets a process-level flag; every subsequent
write raises `CircuitOpen` until the client is re-instantiated. This stops a
notebook loop from continuing to hammer a flagged LinkedIn account.

### 5.3 Request encoding

The API is inconsistent and the transport absorbs it:

- `POST /users/invite` — `application/json`.
- `POST /chats` and `POST /chats/{id}/messages` — `multipart/form-data`.
- Nested and repeated fields use bracket notation: `linkedin[inmail]=true`,
  repeated `attendees_ids`.

**Resolved 2026-09-08** against the `unipile-node-sdk` reference implementation
(`src/resources/messaging.resource.ts`, `startNewChat`):

- Arrays are **repeated keys** — `attendees_ids` is appended once per id.
- Nested objects use **bracket notation** — `linkedin[api]`, `linkedin[inmail]`.
- Booleans are the strings `"true"` / `"false"`.
- `sendMessage` sends `text`, `thread_id`, `attachments` only.

Note the email resource JSON-stringifies array values instead; that convention is
specific to `/emails` and does not apply here.

Implementation detail: httpx only emits multipart when `files` is present, and
plain `data` would go out urlencoded. Fields are therefore sent as `(None,
value)` file tuples in a list, which both forces multipart and allows a key to
repeat.

Additionally, `mark_read` is **`PATCH /chats/{chat_id}` with a JSON body**
`{action, value}` — not multipart. `GET /chats` accepts `unread` as the string
`"true"`/`"false"`.

## 6. Error model

Exceptions are keyed on the API's `type` string, **not** the HTTP status.
`already_connected` and `already_invited_recently` are both `422` and callers
must be able to branch between them.

```
UnipileError(type, status, title, detail, instance)
├── ConfigError                     # local: missing/invalid settings
├── BudgetExhausted                 # local: daily cap reached
├── CircuitOpen                     # local: account restricted this process
├── ProfileIncomplete               # local: throttled_sections non-empty
├── AuthenticationError             # 401
│   └── AccountDisconnected         # errors/disconnected_account
├── PermissionDenied                # 403
│   ├── AccountRestricted           # errors/account_restricted
│   └── FeatureNotSubscribed        # errors/feature_not_subscribed
├── NotFound                        # 404
├── RateLimited                     # 429 errors/too_many_requests
├── UnprocessableError              # 422 base
│   ├── AlreadyConnected
│   ├── AlreadyInvited              # errors/already_invited_recently
│   ├── NoConnectionWithRecipient
│   ├── InsufficientCredits
│   ├── ConnectionLimitReached
│   ├── UserUnreachable
│   └── CannotResendYet
└── ServerError                     # 5xx
```

Unrecognized `type` values fall back to the class for their status. Every
exception retains the raw `type`, so new provider error codes remain inspectable.

## 7. Pagination

Two shapes exist in the API and both are hidden behind generators:

- **Cursor** (relations, chats, messages) — opaque `cursor` in the response,
  echoed back as a query parameter.
- **Page count** (`/linkedin/search/parameters`) — `paging.page_count`.

Public methods expose `Iterator[Model]` only, never raw pages. `limit` controls
page size, not total results. The paginator breaks if a cursor repeats, to avoid
an infinite loop on a misbehaving endpoint.

## 8. Send budget

`.env` sets the caps; a JSON file keyed by `date + account_id` holds the counters
so they survive notebook kernel restarts and script re-runs.

Every write follows: `check()` then `throttle()` then send then `record()`.
Profile fetches follow the same path under their own counter, because they are
the read LinkedIn actually punishes.

- `check(kind)` raises `BudgetExhausted` when the daily cap is reached.
- `throttle()` sleeps a uniform random interval between the min/max delay.
- `record(kind)` increments and atomically rewrites the counter file
  (temp file plus `os.replace`).
- `reconcile(client)` recounts today's real sends from `/users/invite/sent` and
  `/messages`, for when the local file has drifted.
- A profile fetch is `record()`ed on **any** 200, complete or throttled —
  LinkedIn counted the fetch either way.
- `note_usage(pct)` consumes the `usage` percentage LinkedIn returns on invite
  responses: logs a warning at `USAGE_WARN_PCT`, raises `BudgetExhausted` at
  `USAGE_HALT_PCT`.

Old dates are pruned on write. No file locking — single user, atomic replace.

## 9. Public surface

```python
from lib.unipile import UnipileClient

with UnipileClient.from_env() as li:
    # profiles & relations
    li.users.get_profile("danareyesrn")
    li.users.iter_relations()
    li.users.iter_followers()
    li.users.iter_following()

    # invitations
    li.users.send_invitation(provider_id, message="...")
    li.users.iter_invitations_sent()
    li.users.iter_invitations_received()
    li.users.cancel_invitation(invitation_id)
    li.users.handle_invitation(invitation_id, "accept")

    # messaging
    li.messaging.iter_chats(unread=True)
    li.messaging.get_chat(chat_id)
    li.messaging.iter_messages(chat_id)
    li.messaging.iter_attendees(chat_id)
    li.messaging.find_chat_with(provider_id)
    li.messaging.send_to(provider_id, "text")
    li.messaging.send_message(chat_id, "text")
    li.messaging.start_chat([provider_id], "text", inmail=False)
    li.messaging.mark_read(chat_id)

    # search
    li.search.search({...}, api="classic")
    li.search.iter_search_parameters("LOCATION", "United States")
```

`account_id` is resolved once (env, else `GET /accounts`) and cached on the
client. `send_to()` is the direct LinkedIn Helper replacement: `GET /chats`
returns `attendee_provider_id` on every chat, so it matches the target
`provider_id` against that field and starts a new chat only if none exists —
no attendee-id resolution step is required.

Acceptance of a sent invitation is detected without scanning relations:
`iter_invitations_sent()` returns `invited_user_public_id` (the Firestore key),
so a slug that disappears from the pending set was accepted, withdrawn, or
ignored — disambiguated by `get_profile(slug).network_distance == "FIRST_DEGREE"`.
Note `invitation_text` comes back `null`, so the note text is never recoverable
from the API.

Search defaults to `api="classic"`. The connected account reports
`premiumFeatures: ["premium"]` with a null `premiumId`, which indicates no Sales
Navigator seat; `api="sales_navigator"` is opt-in and will raise
`FeatureNotSubscribed` if unavailable.

## 10. Profile completeness and section depth

Verified against the live API on 2026-09-08:

- With **no** `linkedin_sections`, the response contains no experience, no
  education, no skills, and no About text. It is unusable for classification.
- With `linkedin_sections=*_preview` on a profile having 7 jobs and 23 skills:
  5 of 7 jobs, **2 of 23 skills**, 2/2 education, 2/2 languages, 2/2 projects,
  and the full About text. Preview truncates skills severely.
- LinkedIn throttles heavy section use. Throttled sections return **empty with
  HTTP 200** and are listed in `throttled_sections` — silent degradation.
- The documented `current` boolean on work experience is **not returned**.
  Current position is derived from `end is null`.
- Dates are `M/D/YYYY` strings (e.g. `"5/1/2016"`), not ISO.

- With an explicit full list (`experience`, `skills`, `certifications`) on the
  same profile: **7 of 7 jobs** and **23 of 23 skills**, each job additionally
  carrying its own `skills[]` array (14 entries on the current role). No
  `throttled_sections` on that call.
- **`about` is its own section.** Requesting an explicit list that omits it drops
  the About text with no error and no `throttled_sections` entry — the single
  richest block of prose for classification, gone silently.

**Decision: default to the explicit list**
`about, experience, education, skills, certifications, languages, projects`.

This returns everything at full depth — matching or beating LinkedIn Helper's
richness — while deliberately excluding `recommendations` and
`recruiting_activity`. Recommendations run to thousands of characters of
third-party testimonial per profile, inflating both throttle exposure and Gemini
input cost, while carrying weak signal for industry / function / seniority.
`recruiting_activity` is Recruiter-seat only. `sections="*"` remains available
for callers who want literally everything.

Certifications are retained despite being empty on the sample profile: they are
high-signal for this domain (CRCR, CPC, RHIA, CCS appear throughout the existing
contact set).

The cost of that choice is a higher throttling probability, which is mitigated
rather than ignored:

- `Profile` exposes `throttled_sections`, `incomplete_sections` and
  `is_complete`. A section counts as withheld when it is named in
  `throttled_sections`, **or** when it comes back empty despite a non-zero
  `*_total_count` -- LinkedIn does not always name what it withheld.

  A merely *short* section does not count. Corrected 2026-09-08 after a live
  run: a real profile returned 9 of 10 work experiences alongside 93/93 skills,
  5/5 education, 2/2 certifications and a 2,492-character About section, because
  LinkedIn collapses grouped roles at the same company. The original
  strict-count rule rejected it, discarding excellent classification input for
  nothing.
- `get_profile(..., require_complete=True)` raises `ProfileIncomplete`.
- Throttled sections are **never** auto-retried; retrying compounds throttling.
  The caller decides whether to skip the contact or schedule it for later.
- Profile fetches are budgeted and jittered like writes (section 8).
- `sections=` is a per-call parameter, so a caller that hits sustained throttling
  can narrow to `["experience", "education", "skills"]` without code changes.

**Pipeline rule:** a profile with a non-empty `throttled_sections` must not be
written to Firestore. Writing it caches a classification derived from partial
data, silently and permanently.

## 11. LinkedIn Helper compatibility (`compat.py`)

`to_lh_document(profile) -> dict` produces a dict in the shape the existing
Firestore pipeline expects. It is a pure transform; the caller writes it.

| LH key | Unipile source |
|---|---|
| `id` (document key) | `public_identifier` |
| `profileUrl` | `public_profile_url` |
| `externalIds` | `[{type: "public-id", ...}, {type: "li-hash-id", externalId: provider_id}]` |
| `miniProfile.{firstName,lastName,headline}` | `first_name`, `last_name`, `headline` |
| `currentPosition` | `work_experience[]` entry where `end is null` |
| `positions[]` | `work_experience[]` — `title` from `position`, `companyName` from `company`, `locationName` from `location`, `description`, `dateRange` parsed from `M/D/YYYY` |
| `educations[]` | `education[]` — `schoolName` from `school`, `degreeName` from `degree`, `fieldOfStudy` from `field_of_study` |
| `skills[]` | `skills[]` — `name`, `endorsementsCount` from `endorsement_count` |
| `extra.summary` | `summary` (the About text) |
| `extra.locationName` | `location` |
| `extra.industry` | **typically absent.** The schema documents `work_experience[].industry[]`, but no live response has included it (same omission pattern as `current`). Populated when present, empty otherwise; the classifier derives industry from title and description text regardless |
| `positions[].skills` | `work_experience[].skills[]` — per-role skill tags, present only at full section depth; additional signal LinkedIn Helper never captured |
| `occupation` | `headline` |
| `memberDistance` | `network_distance` mapped `FIRST_DEGREE`→1, `SECOND_DEGREE`→2, `THIRD_DEGREE`→3, `OUT_OF_NETWORK`→0 |
| `email` | `contact_info.emails[0]` |

### 11.1 Enriching the classifier input

`join_keys()` traverses `extra` recursively over all of its keys, so additional
Unipile sections placed under `extra` reach the Gemini summary with **no change
to the existing pipeline**. `to_lh_document()` therefore also populates:

- `extra.certifications` — high signal for healthcare RCM roles
- `extra.languages`
- `extra.projects`
- `extra.volunteering`

`recommendations` is deliberately **excluded**: it runs to thousands of
characters per profile, inflating Gemini input cost, while carrying weak signal
for industry / function / seniority. Available on the model if wanted later.

LinkedIn Helper bookkeeping (`lhId`, `personId`, `campaign_*`,
`fullMessagingHistory`, `lastSendAndReceivedMessages`) is deliberately omitted —
message history is now served properly by `/chats/{id}/messages`.

The existing `get_member_distance()` in `functions.py` already accepts an int, so
no change is needed there.

## 12. Data flow

```
UnipileClient.users.get_profile(slug)          # sections="*"
        │
        ├── throttled_sections non-empty ──▶ skip, retry another day
        ▼
compat.to_lh_document(profile)
        │
        ▼
functions.join_keys(doc, [...])   ← existing, unchanged
        │
        ▼
Firestore `extracted`   (document id = public_identifier)
        │
        ▼
analysis.ipynb → Gemini → `analysis`   ← existing, unchanged
```

The Gemini prompt, the classification schema, the notebook, and the 28,058
existing rows are untouched.

**Migration policy (decided):** new contacts only. Unipile ingests profiles not
already present in `extracted`. Existing rows keep their LinkedIn-Helper-sourced
summaries and classifications, so there is no re-classification wave and no
additional Gemini spend.

## 13. Targeted fix to existing code

`functions.join_keys()` ends with `list(set(result))`. Python randomizes string
hashing per process, so the same profile produces a different `summary` string on
every run — verified: three runs, three different hashes, identical length.

Inside the notebook this is invisible, because summaries are read from Firestore
rather than recomputed. It bites on **re-ingest**: a re-POSTed profile gets a
reordered summary, the notebook sees "summary changed", and Gemini is re-billed
for no reason.

Fix: `list(dict.fromkeys(result))` — order-preserving dedup, deterministic, one
line. This is forward-looking only; it does not alter already-stored summaries.

`main.py` is deployed to Cloud Run, so this fix reaches the `/add-profile/` path
only after a redeploy. Local scripts importing `join_keys` directly pick it up
immediately.

Determinism does **not** make a Unipile-sourced summary match an LH-sourced one —
the field content differs regardless of ordering. That is why the migration
policy is new-contacts-only.

## 14. Testing

All tests run inside the dev container (`thirsty_fermat`, Python 3.13.9,
venv at `/home/vscode/.venv`).

- Unit tests mock HTTP with `respx`, using fixtures captured from real, redacted
  responses.
- `test_errors.py` — every `type` in the taxonomy maps to the intended class;
  unknown types fall back by status.
- `test_transport.py` — reads retry on 5xx; **writes issue exactly one request on
  failure**; 429 raises rather than sleeping.
- `test_budget.py` — cap enforcement, date rollover, atomic write, `usage`
  warn/halt thresholds, `reconcile()`.
- `test_compat.py` — `to_lh_document()` on the captured profile fixture yields all
  seven keys `join_keys()` consumes; `join_keys()` on the result is non-empty and
  identical across two calls; enrichment keys appear under `extra`.
- `test_profile_completeness.py` — a response with `throttled_sections` yields
  `is_complete is False` and raises under `require_complete=True`.
- `pytest -m live` — read-only smoke tests against the real account (accounts,
  relations first page, chats first page). **No test ever calls a write endpoint.**

## 15. Dependencies and housekeeping

- `pyproject.toml`: add `httpx`, `pydantic`, `pydantic-settings`; dev group gains
  `respx`. Register the `live` pytest marker.
- `.env.example`: add every `UNIPILE_*` variable from section 4.
- `.gitignore`: add the budget state file.
- `README.md`: add `lib/unipile` to the project structure table and a short usage
  section.
- `lib/__init__.py` must exist so `from lib.unipile import ...` resolves from
  `Python/`.

## 15b. Pacing and throttle recovery (added after the first live run)

The first live run of `new-contacts.ipynb` fetched 20 profiles and could store
4. LinkedIn had withheld sections on the other 16, returning HTTP 200 with the
sections empty -- so nothing raised, nothing retried, and the budget was spent
on unusable data.

Three changes follow from that:

- **`pacing.HumanCadence`** replaces the flat `random.uniform` delay. Gaps are
  drawn from Beta(2, 5) across `[min, max]` (mostly short, long tail reachable)
  and interrupted by a long break roughly every `long_pause_every` calls.
  Defaults moved from 4-12s to 20-90s with 3-10 minute breaks: a person reading
  a profile does not move on in six seconds.
- **Throttle recovery.** An incomplete profile calls `back_off`, doubling every
  subsequent gap (capped at 8x), and the fetch is retried up to
  `throttle_retries` times. A complete response calls `recovered`. Every wait
  longer than the norm is logged at WARNING naming the profile and the withheld
  sections, so a multi-minute pause in a notebook does not read as a hang.
- **`to_lh_document` refuses an incomplete profile.** It is the only path from a
  Unipile profile to a Firestore document, so the "never store partial data"
  rule belongs there rather than in each caller's `if`.

Transport backoff widened to 4 attempts over `(2, 10, 30)` seconds after a real
502 from Unipile's gateway killed a run that had already spent budget.

Open question: retries did not rescue any of three persistently-withheld slugs
across two runs hours apart, so a per-slug give-up counter may be worth more
than the third attempt.

## 16. Known blockers and open items

1. ~~**The `UNIPILE_API_KEY` in `.env` is not accepted.**~~ **RESOLVED
   2026-09-08.** The original 54-character key returned
   `401 errors/missing_credentials` from both the host and the dev container,
   byte-identical to a bogus key. A freshly issued 53-character key from the
   Unipile dashboard authenticates: `GET /accounts` returns `200` and account
   `ZIGT4FVWS4CCJze_MuVHCg`. `.env` now holds the working key, verified through
   the same parse path the library will use. Live smoke tests are unblocked.
   The key-bearing `lib/unipile/test.curl` is now gitignored.

2. **Multipart encoding is unverified.** Confirm array and nested-object
   serialization against `unipile-node-sdk` before implementing writes.
3. ~~**`find_chat_with()` lookup is unverified.**~~ **RESOLVED 2026-09-08.**
   `GET /chats` returns `attendee_provider_id` (a LinkedIn `provider_id`) on
   each chat, so chat lookup is a filter over `iter_chats()`. No
   `/chat_attendees/{id}/chats` call and no resolution step.
4. **`handle_invitation` requires a `shared_secret`.** Accepting or declining
   needs `{provider, shared_secret, account_id, action}`, where `shared_secret`
   is issued by LinkedIn alongside the invitation. The method therefore takes
   the `ReceivedInvitation` model rather than an id.
5. **`POST /linkedin/search` takes `account_id` as a query parameter**, not in
   the JSON body -- the body carries only `api` and the search config. Sending
   it in the body returns `400 invalid_parameters`. Found by live testing after
   the mocked tests passed.
6. **`member_urn` is inconsistent** — a bare numeric string on the self profile,
   a full `urn:li:fsd_profile:...` on relations. Models must tolerate both.

## 17. Review findings fixed (2026-09-08)

An external review raised nine issues. All nine reproduced, and all nine are
fixed. Two severity calls were revised and one proposed remedy was rejected.

| # | Issue | Severity | Resolution |
|---|---|---|---|
| 1 | Budget checked against a `"pending"` placeholder before the account resolved, so the first operation of every client instance bypassed the cap | P1 | `SendBudget` takes `account_id` as a callable, so resolution happens before the first check |
| 2 | Test fixtures matched the `*.json` ignore rule and would never be committed | P1 | `!tests/**/fixtures/*.json` |
| 3 | Pydantic renders the raw input dict before `SecretStr` applies, printing the API key in `ConfigError.detail` and the chained traceback | P2 | Detail rebuilt with `include_input=False`; chain broken with `from None` |
| 4 | `shared_secret` arrives as `specifics.shared_secret`, so `handle_invitation()` could never succeed | **P1** (raised from P2 -- broken by construction, not degraded) | `AliasChoices(AliasPath("specifics", "shared_secret"), "shared_secret")` |
| 5 | `cancel_invitation` omitted the required `account_id` query parameter | P2 | Passed through; the test now asserts the parameter, not just the call |
| 6 | The provider usage halt raised once and was then forgotten | P2 | **Proposed fix rejected.** Persisting a bare halt has no reset semantics, and LinkedIn publishes none for this percentage, so a latch would strand the account. Instead the reading is stored in today's counter bucket: it rolls over at UTC midnight with everything else, blocks invitations only (the quota it describes), and a later lower reading lifts it |
| 7 | A null `public_identifier` produced `id=None`, and `document(None)` generates a random id -- a fresh duplicate per ingestion | P2 | Falls back to `provider_id`, which is always present and equally stable |
| 8 | JSON-form `UNIPILE_PROFILE_SECTIONS` failed validation | **P3** (lowered from P2 -- a docstring claim, not a user-facing break) | The validator decodes the JSON branch |
| 9 | `502` was retried by the transport but missing from the status map, so a bare `UnipileError` escaped `except ServerError` | P2 | Any status >= 500 falls back to `ServerError` |

Two further issues the review did not raise:

- `send_to()` walks every chat page when no conversation exists yet, and
  `GET /chats` has no attendee filter. Correct but wasteful, and it grows with
  the chat list. Documented on `find_chat_with`; the real fix is a
  `provider_id -> chat_id` cache in the outreach layer, not here.
- `iter_search_parameters` returned raw dicts while every other iterator
  returned models. Now returns `SearchParameter`.

The duplicated per-resource `_iter` implementations are now one
`iter_account_scoped` helper in `pagination.py`, which also removes the
asymmetry where messaging did not drop `None`-valued filters.
