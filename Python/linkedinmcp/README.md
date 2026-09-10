# linkedinmcp — the LinkedIn outreach service

`linkedinmcp` is a small web service that lets a Claude agent work the LinkedIn
outreach pipeline safely. It exposes this project's LinkedIn tooling over the
Model Context Protocol (MCP), runs on Google Cloud Run as the service
`linkedin-outreach`, and keeps every action that touches LinkedIn inside code the
agent cannot bypass.

It is an internal package with no outside users.

- [Why it exists](#why-it-exists)
- [Status](#status)
- [How it fits together](#how-it-fits-together)
- [Package layout](#package-layout)
- [MCP tools](#mcp-tools)
- [HTTP endpoints](#http-endpoints)
- [Authentication](#authentication)
- [Connecting to the MCP server](#connecting-to-the-mcp-server)
- [Configuration](#configuration)
- [Shared code in `lib/`](#shared-code-in-lib)
- [Running locally](#running-locally)
- [Tests](#tests)
- [Deploying](#deploying)
- [Setting up the Claude platform](#setting-up-the-claude-platform)
- [Security](#security)
- [Rules for changing this code](#rules-for-changing-this-code)
- [Roadmap](#roadmap)

---

## Why it exists

The LinkedIn work splits in two.

| Who | Does what |
|---|---|
| **You, in the notebooks** | Volume work: loading the backlog of connections, classifying them, and the bulk intro campaign (`new-contacts.ipynb`, `send-intros.ipynb`, `analysis.ipynb`). |
| **A Claude agent, through this service** | The daily increment: the day's new connections, their classification, intros to the newly eligible, message sync, pipeline staging — and above all, acting on leads and prospects with individual follow-ups and drip sequences. |

The agent never gets the Unipile client itself. It asks this service to act, and
the service decides whether it may. That split is deliberate, for two reasons:

- **Sends must be constrained in code, not by instructions.** Daily caps,
  per-contact cooldowns, and a two-phase claim that prevents any message going
  out twice are enforced here, where a prompt cannot argue with them.
- **Inbound messages are written by strangers.** The agent reads them, so they
  are a prompt-injection surface. Anything a message could persuade the agent to
  do has to pass the same code-level checks as everything else.

## Status

**Phase 1 is complete: the walking skeleton.** Transport, authentication,
configuration, one MCP tool, the container, the deploy script and the Claude
platform definitions all exist, are tested, and have been verified end to end in
a real container against the real configuration. It is deployed to Cloud Run as
`v1.0.2` (2026-09-10) and verified against the live URL: both header forms,
`/mcp` with and without the trailing slash, the MCP handshake, and a
`get_status` call that reached Firestore and loaded the Unipile credentials.

**Nothing that sends a message exists yet.** The outbound queue, the send guards,
the scheduled jobs and the daily load are later phases — see
[Roadmap](#roadmap). Today the service can tell an agent whether it is healthy
and what limits apply. It cannot yet act on LinkedIn.

## How it fits together

```
 Claude Managed Agent ──┐
 Claude Code          ──┼── POST /mcp/ ──▶  linkedin-outreach  (Cloud Run)
 Claude connectors    ──┘   x-api-key or        │
                            Bearer              ├──▶ Firestore   vk-linkedin / linkedin
 Cloud Scheduler ───── POST /jobs/*  (phase 2)  ├──▶ Unipile     LinkedIn API
 Unipile webhook ───── POST /webhooks (phase 2) └──▶ Gemini      classification
```

One service, one API key, short requests. Each request does its work and
returns; nothing sleeps in-process waiting to look human. When scheduled jobs
arrive in phase 2, the scheduler's interval *is* the pacing.

## Package layout

```
linkedinmcp/
  __init__.py
  app.py            ASGI entry point: create_app() builds the app
  settings.py       OutreachSettings and get_settings()
  http_auth.py      ApiKeyMiddleware and the require_api_key dependency
  clients.py        factories for the Firestore, Unipile and Gemini clients
  mcp_server.py     the FastMCP server and its tools
  Dockerfile        the container image
  deploy.cmd        build, push and deploy to Cloud Run
  platform/         Claude Managed Agents definitions, applied with `ant apply`
    agents/scheduled.md       the agent that runs every weekday morning
    agents/interactive.md     the agent you drive from the Console
    environments/cloud.yaml   the sandbox both agents run in
    deployments/morning.md    the schedule that starts the morning run
    README.md                 the step-by-step setup runbook

tests/linkedinmcp/  the service's tests, including an in-memory Firestore
```

`linkedinmcp` is an ordinary importable package, run from the `Python/`
directory. Code that other parts of the project can use lives in
[`lib/`](#shared-code-in-lib), not here.

## MCP tools

One tool exists today.

### `get_status`

Reports whether the service is healthy and which limits it enforces. The agent
calls it at the start of a session, and again whenever another tool fails in a
way that might be this service rather than LinkedIn.

| Field | Meaning |
|---|---|
| `service` | Always `"linkedin-outreach"`. |
| `time` | Current time in the configured timezone, ISO 8601. |
| `timezone` | The IANA zone from `OUTREACH_TZ`. Every date the service reports is in it. |
| `caps.messages_per_day` | Daily message ceiling, read from `UNIPILE_MAX_MESSAGES_PER_DAY`. |
| `caps.profile_fetches_per_day` | Daily profile-fetch ceiling, from `UNIPILE_MAX_PROFILE_FETCHES_PER_DAY`. |
| `caps.intro_daily_cap` | Intros one planning run may queue. |
| `caps.max_touches` | Most outbound messages any one contact may ever receive. |
| `caps.min_days_between_touches` | Minimum days between messages to one contact. |
| `require_approval` | Whether every queued message waits for a human. |
| `firestore` | `"ok"`, or the class name of the error the database raised. |
| `unipile` | `"ok"`, or the class name of the error reading the LinkedIn configuration. |

Two properties of this tool matter more than its fields:

- **It never raises.** A database or credential failure comes back as a named
  field, such as `"firestore": "PermissionDenied"`. An agent that receives an
  exception learns nothing; one that receives a named failure can tell you what
  to fix. When `unipile` is not `"ok"`, the two rate limits are *absent* from
  `caps`, because they could not be read, which is not the same as unlimited.
- **It stays small.** The Claude platform moves any tool output over 100,000
  characters into a file the agent then has to open before acting.

The Firestore probe reads one empty projection of one document, so checking
health costs bytes rather than a 35 KB profile.

## HTTP endpoints

| Request | Response | Why |
|---|---|---|
| `GET /health` | `200 {"ok": true}` | Open, for a person or an uptime monitor without the key. Cloud Run's own startup probe is a TCP check and never calls it. |
| `POST /mcp/` with a valid key | `200` | The MCP endpoint. Stateless Streamable HTTP. |
| `POST /mcp/` with no key or a wrong one | `401` | |
| `POST /mcp`, without the trailing slash | same as `/mcp/` | See below. |
| `GET /mcp/` | `405` | Stateless mode serves POST only. |

**`/mcp` and `/mcp/` are the same endpoint.** Up to `v1.0.1`, `/mcp` answered
with a redirect to `/mcp/`. The Claude app's connector does not follow
redirects, so a connector set up without the slash could never connect. The
service now rewrites `/mcp` to `/mcp/` before routing, so both spellings pass the
same key check. `ids.env`, the agent definitions and the vault credential still
use `/mcp/`.

**No path may end in `z`.** Cloud Run's front end reserves some paths ending in
`z` and answers them itself, before the request reaches the container. `v1.0.0`
served its liveness check at `/healthz`, which passed every local test and
returned Google's own 404 page once deployed. `test_no_route_ends_in_z` fails on
any such route. See Cloud Run's
[known issues](https://docs.cloud.google.com/run/docs/known-issues).

## Authentication

There is exactly one credential, `OUTREACH_API_KEY`, and everything that calls
the service presents it: the Claude agent platform, Claude connectors, Claude
Code, and later Cloud Scheduler and the Unipile webhook.

- **Two header forms are accepted, and no others:** `x-api-key: <key>`, and
  `Authorization: Bearer <key>` with the scheme matched case-insensitively.
  Claude connectors may send either without Anthropic reviewing the header name
  first, and every client in [Connecting](#connecting-to-the-mcp-server) uses one
  of them, so a third form would be dead code that widens the attack surface.
- **The key must be at least 16 characters, checked at startup.** An empty key
  would authenticate every caller on a public URL, because comparing two empty
  strings succeeds. The service refuses to start instead. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **The comparison is constant-time and on bytes,** so a hostile non-ASCII header
  produces a 401 rather than a 500.
- **`/health` is open because of where the check is mounted, not because its
  path is special-cased.** The middleware knows nothing about paths and has no
  exemption list, so no future route can be exposed by a typo in one.

Which tools a given agent may call is decided by that agent's platform
definition, not by the key. The scheduled agent's allowlist excludes the tools
meant for a human.

## Connecting to the MCP server

Every client needs the same two values:

| Value | Where it is |
|---|---|
| **URL** | `OUTREACH_URL` in `linkedinmcp/platform/ids.env`, which every deploy rewrites. It looks like `https://linkedin-outreach-<hash>-uc.a.run.app/mcp/`. |
| **Key** | `OUTREACH_API_KEY` in `Python/.env`. |

The service accepts the key as `x-api-key: <key>` or as
`Authorization: Bearer <key>`.

### Claude app: claude.ai, Desktop and mobile

Add the service as a custom connector that sends the key as a request header.

1. **Remove any connector you already added for this service.** A connector's
   authentication settings cannot be edited after it is added.
2. **Open the Add dialog.** On a Team or Enterprise plan, an owner uses
   **Organization settings → Connectors → Add → Custom**, choosing **Web** if
   asked. On Free, Pro or Max, use **Customize → Connectors → Add custom
   connector**.
3. **Remote MCP server URL:** the URL.
4. **Authentication: None.** Claude probes the URL and may pre-fill *Always
   required*, because the service answers 401 to a request without the key. The
   service has no OAuth sign-in, so change it to None.
5. **Request headers:** pick `x-api-key`, paste the key as the value, and mark it
   **Required**. If you pick `authorization` instead, type `Bearer ` and a space
   before the key: Claude sends the value exactly as entered.
6. **Add**, then **Connect**. On Team and Enterprise, members connect from
   **Customize → Connectors**.

**Request headers are in beta, and only some organizations have them.** If the
dialog has no Request headers section, the Claude app cannot connect to this
service: without a header it would need an OAuth sign-in, which the service does
not provide. Use Claude Code instead. Anthropic's pages:
[adding a request header](https://claude.com/docs/connectors/custom/remote-mcp#authenticating-with-request-headers)
and [connector authentication](https://claude.com/docs/connectors/building/authentication).

**A connector added without the header never says the key is missing.** It
connects without one, gets a 401, looks for an OAuth sign-in, finds none, and
reports "Couldn't reach" followed by the connector's name.

### Claude Code

From the `Python/` directory, in PowerShell, replacing `<key>` with the key:

```powershell
$OutreachUrl = (Get-Content linkedinmcp\platform\ids.env | Where-Object { $_ -match '^OUTREACH_URL=' }) -replace '^OUTREACH_URL=', ''
claude mcp add --transport http outreach $OutreachUrl --header "x-api-key: <key>"
claude mcp list
```

`claude mcp list` should show `outreach` as connected. Inside a session, `/mcp`
lists its tools.

**Keep the default scope.** `claude mcp add` stores the header, key included, in
your own Claude Code settings. `--scope project` would write it into `.mcp.json`
in the repository instead, where the next commit would publish it.

### A Claude Managed Agent

The agent never sees the key. The key lives in a vault as a `static_bearer`
credential, and the platform sends it as `Authorization: Bearer <key>` whenever
the credential's `mcp_server_url` matches the URL of the agent's `outreach`
server. Steps 3 and 5 of [`platform/README.md`](platform/README.md) set this up.

A URL that matches no credential does not fail outright. The agent connects
without the key and reports `mcp_authentication_failed_error`, so compare the two
URLs before replacing the key.

### When a connection fails

The service's request log shows what the client actually sent:

```powershell
gcloud run services logs read linkedin-outreach --region us-central1 --project vk-linkedin --freshness 1h --limit 50
```

| The log shows | Meaning | Fix |
|---|---|---|
| `POST 200` on `/mcp/` or `/mcp` | Connected. | Nothing. |
| `POST 401` on `/mcp/`, then `GET 404` on three `/.well-known/oauth-…` paths | The client sent no key, or a wrong one, and then looked for an OAuth sign-in. | Claude app: re-add the connector with the request header. Claude Code: check `--header`. Managed Agent: compare the vault URL with the agent's. |
| `POST 404` on `/` | The URL is missing its `/mcp/` path. | Use the whole `OUTREACH_URL` value. |
| No request at all | The client never reached the service. | Check the host name against `ids.env`. |

`GET /health` answers `{"ok": true}` without a key, from a browser or curl. It
tells a service that is down apart from a client that is misconfigured.

## Configuration

### Custom caps go in the global `.env`

**Set any custom cap in `Python/.env`, the project's global environment file,
and nowhere else.** Every cap has its default written beside its field in code:
`lib/unipile/config.py` for the daily LinkedIn limits, and
`linkedinmcp/settings.py` for the campaign caps listed under
[Outreach settings](#outreach-settings). A variable of the same name in
`Python/.env` overrides the default. The rest of the project works the same way.

- **The notebooks read the same file.** A LinkedIn limit set there applies to a
  notebook run and to the service alike. The notebooks ignore the `OUTREACH_*`
  variables.
- **The file is copied into the container image when it is built.** A changed
  cap reaches Cloud Run only with the next deploy, under a new tag. `get_status`
  reports the caps the running service is actually using.
- **The file also holds every credential:** `OUTREACH_API_KEY`,
  `UNIPILE_API_KEY`, `UNIPILE_DNS` and `GOOGLE_API_KEY`.

The service also loads an optional `linkedinmcp/.env` after the global file,
with override. It is reserved for values that must differ from the notebooks,
which means pacing, and it never holds a cap. No such file exists at present.

### Rate limits live in the Unipile configuration, and nowhere else

Messages and profile fetches per day are `UNIPILE_MAX_MESSAGES_PER_DAY` and
`UNIPILE_MAX_PROFILE_FETCHES_PER_DAY`, the same variables the notebooks use. The
Unipile client enforces those exact numbers on every call. This service keeps no
copy of them under a name of its own, because a second copy could disagree with
the first, and the first sign of the disagreement would be a restricted LinkedIn
account. A test fails if anyone reintroduces one.

### Pacing

The notebooks sleep between LinkedIn calls so their traffic looks human. By
default each call waits 20 to 40 seconds, with a longer break every 10 calls;
the defaults are in `lib/unipile/config.py`. The service should not sleep at all.
It does one action per request and the scheduler's interval is its pacing, so a
sleep would only bill Cloud Run for waiting.

Nothing gives the service zero pacing today. With no `linkedinmcp/.env`, it
would pace itself like a notebook. That costs nothing in phase 1, which makes no
LinkedIn calls. Phase 2 has to settle it before the service sends anything, and
not by setting zero pacing in `Python/.env`, which would take the pacing away
from the notebooks too.

### Outreach settings

All of these are optional. The default applies unless you set the variable in
`Python/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `OUTREACH_API_KEY` | *required* | The one credential. At least 16 characters. |
| `OUTREACH_TZ` | `UTC` | IANA timezone for every reported date and the working-hours schedule. UTC is deliberately wrong for a person, so forgetting is visible. |
| `OUTREACH_INTRO_DAILY_CAP` | `10` | Intros one planning run may queue. |
| `OUTREACH_MIN_DAYS_BETWEEN_TOUCHES` | `5` | Minimum days between messages to one contact. |
| `OUTREACH_MAX_TOUCHES` | `3` | Most outbound messages one contact may ever receive. |
| `OUTREACH_MESSAGE_MAX_CHARS` | `1200` | Longest message the service will send. |
| `OUTREACH_ALLOWED_LINK_DOMAINS` | none | Comma-separated domains a message may link to. Empty means no links. |
| `OUTREACH_TARGET_INDUSTRIES` | `RCM,Pathology,Medical Lab,Physician Practice` | Industries eligible for an intro. |
| `OUTREACH_REQUIRE_APPROVAL` | `false` | Hold every queued message for a human. |
| `OUTREACH_ALLOW_HTTP_DRY_RUN` | `true` | Honour `?dry_run=1` on job endpoints. |
| `OUTREACH_BUDGET_SNAPSHOT_MAX_AGE_MINUTES` | `60` | How stale the cached account-wide send count may get. |
| `OUTREACH_ANTHROPIC_WEBHOOK_SIGNING_KEY` | unset | Enables the optional platform webhook. |

**Careful with `OUTREACH_TARGET_INDUSTRIES`.** Setting it to an empty value makes
no contact eligible for an intro. The service keeps running and looks healthy
while doing nothing. Leave it unset to get the default four.

**`OUTREACH_TZ` must match the scheduled deployment's timezone** in
`platform/deployments/morning.md`. One decides when the morning session starts,
the other what the service calls "today" once it has. Change both together.

A bad value in any setting stops the service at startup with a `ConfigError`
naming the field. The error never repeats the value, so a mistyped key never
reaches a log.

## Shared code in `lib/`

Anything more than one entry point can use belongs in `lib/`, not in this
package.

| Module | What it provides |
|---|---|
| `lib/config.py` | `BaseConfig`: loading settings from the environment and turning a validation failure into an error that names fields without printing values. Also `ConfigError`, `safe_detail` and `split_list`. `OutreachSettings` and the Unipile client's settings both inherit it. |
| `lib/firestore.py` | `client()`: the Firestore client on project `vk-linkedin`, database `linkedin`, with the local service-account guard. Every entry point in the project uses it. |
| `lib/unipile/` | The LinkedIn API client: send budget, pacing, throttling, retries, and the error hierarchy. |

The service also uses these top-level modules, which the notebooks share:

| Module | Used for |
|---|---|
| `profiles.py` | Classifying a LinkedIn profile by industry, function and seniority with Gemini. |
| `pipeline.py` | Classifying a conversation into a pipeline stage, and building transcripts. |
| `messages_sync.py` | Syncing LinkedIn messages into Firestore. |
| `functions.py` | Intro candidate selection and contact join keys. |

## Running locally

From the `Python/` directory:

```powershell
uv run uvicorn --factory linkedinmcp.app:create_app --port 8080
```

`--factory` is required. `app.py` deliberately has no module-level app object,
because building one at import would read settings and break test collection.

The service reads the same environment files locally as it does in the
container. Without Google credentials, `get_status` reports a Firestore error
rather than failing, which is the intended behaviour.

To check it is up and authenticating, with the key in `$KEY`:

```powershell
curl.exe -s -X POST http://localhost:8080/mcp/ `
  -H "accept: application/json, text/event-stream" `
  -H "content-type: application/json" `
  -H "x-api-key: $KEY" `
  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}'
```

A working service answers with a JSON-RPC result whose `serverInfo.name` is
`linkedin-outreach`. The `serverInfo.version` it reports is the FastMCP library's
version, not this package's.

## Tests

```powershell
uv run pytest tests/linkedinmcp
```

| File | Tests | Covers |
|---|---|---|
| `test_settings.py` | 17 | Defaults, validation, key redaction, the length floor, the timezone check, and that no rate limit is declared here. |
| `test_http_auth.py` | 11 | Both header forms, rejection paths, non-ASCII headers, the lifespan passthrough, and the empty-key guard. |
| `test_app.py` | 12 | Every row of the endpoint table, `/mcp` served without a redirect and still behind the key, mount-scoped auth, the Cloud Run host header, lifespan wiring, and that no route ends in `z`. |
| `test_mcp_server.py` | 7 | `get_status` output, timezone, and that Firestore and Unipile failures are reported rather than raised. |
| `test_fake_firestore.py` | 43 | The in-memory Firestore used by later phases' queue and lease tests. |

No test touches Firestore, Unipile, Gemini or the network, and none reads your
`.env`. Client factories are replaced in tests, and settings are built directly.

`tests/linkedinmcp/fake_firestore.py` imitates the parts of Firestore the service
relies on, including real transactions driven by the library's own
`@firestore.transactional` decorator, simulated contention, and the rule that a
document missing a queried field is left out of the results.

## Deploying

From the `Python/` directory, with a version tag:

```powershell
linkedinmcp\deploy.cmd v1.0.2
```

**The tag is required. There is no default.** It becomes the image's tag in
Artifact Registry, and Cloud Run deploys that exact image as a new revision.

**Use a new tag for every deploy.** The script always rebuilds from the code on
disk before tagging. Running it with an old tag does not redeploy that build; it
overwrites the tag with today's code, and the old image is gone.

The script builds, tags, pushes, deploys, then reads the service URL back from
Cloud Run and writes it to `platform/ids.env`. It stops at the first failed step
and says which step failed and what state Cloud Run was left in.

Cloud Run answers on two URLs for the same service:
`https://linkedin-outreach-<hash>-uc.a.run.app`, which the script reads back and
writes to `ids.env`, and `https://linkedin-outreach-<project-number>.us-central1.run.app`,
which `gcloud run deploy` prints along the way. Both reach the same revision. Use
the `ids.env` one everywhere: the vault matches its credential to the agent's MCP
URL by string, so mixing the two leaves the agent connecting without its key.

**To roll back,** point Cloud Run at an earlier image directly rather than
rerunning the script:

```powershell
gcloud run deploy linkedin-outreach --image us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin-outreach:v1.0.1 --region us-central1 --project vk-linkedin
```

**If `get_status` reports a Firestore error after a deploy,** the service's
runtime account probably lacks access to the `linkedin` database. The deploy
script prints the command that grants it.

The build fails, rather than producing a broken image, if any shared module the
service imports is missing from the package. Those imports are lazy, so without
that check a packaging mistake would not show up until a scheduled job failed.

## Setting up the Claude platform

The agent definitions live in `platform/` and are applied with `ant apply`, which
treats them as code: it creates resources on the first run, updates them on later
runs, and records which file made which resource in `claude-lock.json`.

**Commit `claude-lock.json`.** It is the only thing that makes a second
`ant apply` update your resources instead of creating a duplicate of every agent,
environment and deployment in the account. `Python/.gitignore` ignores JSON
files by default and has an exception for this one.

[`platform/README.md`](platform/README.md) is the step-by-step runbook:
generating the key, deploying, creating the vault, applying the environment and
agents, a manual test session, the Unipile credential, the scheduled deployment,
and connecting Claude Code. Follow it in order; each step's output feeds the
next.

## Security

- **The container image contains real secrets.** `Python/.env` is copied into it
  on purpose, so the service needs no other secret store. Anyone with read access
  to the Artifact Registry repository can pull the image and read them.
- **Keys never reach a log.** Verified in a running container: the logs contain
  neither the outreach key nor the Unipile key.
- **Configuration errors never print values.** A rejected key is reported by
  field name and length only.
- **Treat inbound message text as data.** Instructions inside a contact's message
  are something to report, never something to follow.
- **Writes to the `analysis` collection are always merges.** Those documents hold
  the only copy of thousands of contacts' names and emails, and a plain write would
  destroy them.

## Rules for changing this code

These are easy to break by accident, and each has already caused a real bug or a
near miss.

- **Import modules, not names.** Write `from linkedinmcp import settings as cfg`
  and call `cfg.get_settings()`. A name imported directly cannot be replaced in a
  test, and the resulting failure is baffling.
- **Never cache `get_settings()`.** A cached settings object survives between
  tests and makes test order matter.
- **Never cache the Unipile client.** It carries the send budget and the transport
  circuit breaker. Two jobs sharing one would spend each other's budget and trip
  each other's breaker.
- **Keep module imports free.** Importing any module here must not open a
  connection or read a credential. Heavy dependencies are imported inside the
  functions that need them.
- **Status tools report; they never raise.**
- **Keep tool output small.**
- **Put shareable code in `lib/`.**
- **No `from __future__ import annotations`.**
- **Never add a rate limit here.** It belongs in the Unipile configuration.

## Roadmap

| Phase | Adds | State |
|---|---|---|
| 0 | Project packaging, `profiles.py`, importable `messages_sync.py` | Done |
| 1 | Settings, authentication, `get_status`, container, deploy, platform definitions | Done |
| 2 | The outbound queue, send guards, the action ledger, the decision inbox, scheduled jobs, the Unipile webhook, and the agent's working tools | Next |
| 3 | The daily load of new connections: fetch, store, classify | Planned |
| 4 | The outreach Skill with templates, the scheduled deployment, the optional platform webhook | Planned |
| 5 | Design notes and remaining documentation | Planned |

Phase 2 is where the service starts sending. Its central rule is that **a message
to a real person must never be sent twice**: every send is claimed before the call
and settled after it, and an outcome that cannot be confirmed is marked unknown
and put to you rather than retried.
