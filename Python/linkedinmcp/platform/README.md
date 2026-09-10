# LinkedIn Outreach — Claude Managed Agents setup

This is a runbook, not code: every command below is meant to be read and run
by a person, in order, checking the noted output before moving to the next
one. Nothing in `platform/` applies itself.

**Shell: PowerShell.** All commands below are written for PowerShell 5.1
(the default on this machine), not bash — no `<<'YAML'` heredocs, no bare
`<` redirects. If you'd rather run the `ant` examples exactly as shown in
Anthropic's docs (bash heredocs), run this whole runbook from Git Bash
instead and adapt accordingly; don't mix the two styles mid-run.

**Working directory: `Python\`.** Every command below — `ant apply`,
`deploy.cmd`, `claude mcp add`, all of it — is written to run from the
repository's `Python\` directory, the same directory this project already
treats as its root (it's where `deploy.cmd` is invoked from, and where
`ids.env` and `claude-lock.json` are read by full path below). Don't run
any command here from inside `linkedinmcp\` or `linkedinmcp\platform\`
instead — every relative path in this file assumes `Python\`, and the
previous version of this runbook mixed the two without saying so, which is
exactly the kind of thing that makes a command silently resolve the wrong
file. Where an `ant apply` command needs its lockfile somewhere other than
the current directory, it says so explicitly with `--lock-file` — see
"How `ant apply` works in this project" below.

**Files here, and who owns them:**

- `agents/scheduled.md`, `agents/interactive.md`, `environments/cloud.yaml`,
  `deployments/morning.md`, this `README.md` — written by this task.
- `claude-lock.json` — **not created by this task.** The first `ant apply`
  in step 4 below writes it. `Python\.gitignore` already carves out an
  exception for `linkedinmcp/platform/claude-lock.json` specifically (the
  blanket `*.json` rule just above that exception would otherwise swallow
  it silently) — verified in the file itself, nothing to change there. It
  must be committed anyway; see "How `ant apply` works in this project"
  and Warning 4.
- `ids.env` — **not created by this task.** `linkedinmcp\deploy.cmd`
  writes it when you run step 2, and it's gitignored because it holds
  live resource IDs, not secrets meant to be checked in.
- `.dockerignore` (repository root), `linkedinmcp\Dockerfile`,
  `linkedinmcp\deploy.cmd`, `linkedinmcp\.env.example` — written by a
  sibling task, not this one. This runbook calls `deploy.cmd` in step 2
  but doesn't otherwise touch them.

---

## How `ant apply` works in this project

Skip this section if you already know `ant apply`; every step below
assumes it.

`ant apply` is declarative, not a one-shot create: you describe a resource
in a file, run `ant apply <file>`, and it creates the resource the first
time and **updates that same resource** on every later run — as long as it
can tell the file describes something that already exists. That's the
whole reason the rest of this section matters.
(`https://platform.claude.com/docs/en/cli-sdks-libraries/cli/apply`)

**Kind is inferred from the directory.** `agents/scheduled.md` is an agent
because it lives under `agents/`; `environments/cloud.yaml` is an
environment because it lives under `environments/`; `deployments/morning.md`
is a deployment for the same reason. None of these files need an explicit
`type` field — the directory says what they are. This `README.md`, sitting
directly under `platform/` rather than in a kind-named directory, matches
none of that and is silently skipped by a directory-wide apply, the same
way the CLI's own docs describe it skipping READMEs and CI config
(cli/apply, "How ant apply infers a file's kind").

**Resources reference each other by relative path, not a pasted ID.**
`deployments/morning.md` names its agent as `../agents/scheduled.md` and
its environment as `../environments/cloud.yaml` — literally, permanently,
in the file — instead of a `REPLACE_WITH_..._ID` you look up and paste by
hand. `ant apply` resolves those paths in dependency order and fills in
the real IDs itself, pinned to whichever version of the referenced file it
just applied. The one field this does **not** apply to is `vault_ids`:
vaults are not an `ant apply`-managed kind — there's no `vaults/`
directory, and nothing on the CLI's apply page lists them among the
applyable kinds (agents, environments, skills, memory stores,
deployments) — so a vault ID stays a real placeholder you paste by hand,
same as before. See steps 3 and 8.

**What a successful apply prints**, quoted verbatim from the CLI's own
docs — this closes a gap the previous version of this runbook left open,
which said to check the Console instead because no fetched page showed
this:

```text
Apply  ./claude-lock.json

± Name                    Status
+ ./agents/summarizer.md  created    agent_011CYm1BLqPXpQRk5khsSXrs

Resources  + 1 created

State written to ./claude-lock.json
```

The `Status` column carries the created (or updated) resource's real ID
inline, right there in the terminal. If you need it again later, the same
ID is sitting in `claude-lock.json` under that file's entry.

**Commit `claude-lock.json`.** It maps each file in this project to the ID
it created, and it is the *only* thing that makes the next `ant apply` an
update instead of a duplicate — of any of these files, not only the
deployment (see Warning 4). Whoever runs `ant apply` next — you, in six
months, or a teammate from a fresh clone — needs this file present, or
every resource in this project gets recreated a second time.

**Working directory and the lockfile's location.** The CLI's docs say the
lockfile is written "in the directory you run it from, so run it from the
repository root." Every command below instead runs from `Python\` (this
project's stated root) and passes `--lock-file linkedinmcp\platform\claude-lock.json`
explicitly, so the lockfile lands at the exact path `.gitignore` already
expects no matter which directory the command itself is run from. Two
things about this combination are **not** confirmed by the page fetched
for this task, and are marked `TODO(verify)` below:

- that `--lock-file <path>` causes a *first* run to create the file at
  that path — the documented behavior is to use an existing lockfile
  there "instead of searching upward from the current directory," which
  doesn't say what happens before that file exists;
- how paths are written as keys inside `claude-lock.json` when the
  lockfile's own directory differs from the current directory — relative
  to `Python\` (e.g. `./linkedinmcp/platform/agents/scheduled.md`) or
  relative to the lockfile itself (e.g. `./agents/scheduled.md`). The
  CLI's own example never has to distinguish the two, because it always
  runs from the project root.

Run the very first `ant apply` below with `--dry-run` first and read its
`Name` column before removing that flag — it shows which path form this
CLI build actually uses, before anything is created. Whatever it shows,
keep using the *same* working directory and the *same* `--lock-file` value
for every command in this runbook from then on. Mixing them — running one
apply from `Python\` and a later one from `linkedinmcp\platform\`, say —
is exactly how a resource stops being recognized as already-applied and
gets duplicated, per Warning 4.

**If any of these resources already exist** — an earlier, by-hand version
of this runbook was run against this account already, or something with a
matching name was created directly in the Console — `ant apply` cannot
adopt it into a fresh `claude-lock.json`. Applying a file that describes
an already-existing agent creates a second one (cli/apply, "Edit and
reapply"). Check the Console for anything already named "LinkedIn
Outreach ..." before your first `ant apply` here, and archive it first if
you find one.

---

## 1. Generate the API key

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

This prints a 43-character token. The service enforces a 16-character
minimum at startup (`linkedinmcp/settings.py`:
`api_key: SecretStr = Field(min_length=16)`) — 43 clears that with room to
spare, which is the point of generating it this way rather than typing
something short by hand.

Copy the printed value into `Python\.env` — **not** `linkedinmcp\.env` — as
`OUTREACH_API_KEY=<value>` (the env var name comes from `settings.py`'s
`env_prefix="OUTREACH_"` plus the `api_key` field) **before** step 2 — see
Warning 2 below for why the order matters. `Python\.env` is the project's
one shared environment file, the same one the notebooks read, and it's
where the real, checked-out file keeps this key today alongside the
Unipile and Gemini credentials; `linkedinmcp\.env` is a second, optional
file loaded on top of it (`linkedinmcp/app.py`), and its own
`linkedinmcp/.env.example` says plainly it holds no credentials — only
pacing overrides that differ for this service.

Keep the value somewhere you can paste from for steps 3 and 9. Don't put it
in any file under `platform\`.

## 2. Deploy the service

The version tag is required; the script refuses to run without one. Use a new
tag for every deploy — the script always rebuilds from the code on disk, so an
old tag would be overwritten with today's code rather than redeployed.

```powershell
linkedinmcp\deploy.cmd v1.0.0
```

Check: it ends with `=== Service info ===`, printing the version, the image and
the MCP endpoint. If it stops earlier with an `ERROR:` line, that line says which
step failed and what state Cloud Run was left in; nothing after a failed step
runs.

Then read the URL it wrote:

```powershell
Get-Content linkedinmcp\platform\ids.env
```

This is one line, already in its final form — scheme, host, and the
trailing `/mcp/` suffix `deploy.cmd` appends itself:

```text
OUTREACH_URL=https://linkedin-outreach-a1b2c3d4e5-uc.a.run.app/mcp/
```

Pull just the value and keep it in a variable for the rest of this
runbook. **Use it exactly as read below — do not append `/mcp/` again
anywhere in this runbook; it is already there**, and appending it a second
time is exactly the doubled-path bug (`/mcp//mcp/`) this version of the
runbook fixes:

```powershell
$OutreachUrl = (Get-Content linkedinmcp\platform\ids.env | Where-Object { $_ -match '^OUTREACH_URL=' }) -replace '^OUTREACH_URL=', ''
$OutreachUrl
```

Check: this prints exactly the same string as the `ids.env` line above,
minus the `OUTREACH_URL=` prefix — in this worked example,
`https://linkedin-outreach-a1b2c3d4e5-uc.a.run.app/mcp/`.

## 3. Create the vault and the outreach credential

```powershell
$VAULT_ID = @'
display_name: LinkedIn Outreach
'@ | ant beta:vaults create --transform id --raw-output
$VAULT_ID
```

Check: prints an ID like `vlt_01...`. This is the one vault this design
needs — it will hold both credentials below.

```powershell
$OutreachApiKey = "<paste the value from step 1>"
@"
display_name: linkedin-outreach service key
auth:
  type: static_bearer
  mcp_server_url: $OutreachUrl
  token: $OutreachApiKey
"@ | ant beta:vaults:credentials create --vault-id $VAULT_ID
```

Check: the command succeeds and echoes the credential back (without the
token — write-only fields are never returned, per the vaults doc). The
`mcp_server_url` here must be the same URL, byte-for-byte, as the
`outreach` entry's `url` you'll paste into `agents/scheduled.md` /
`agents/interactive.md` in step 5 — matched after normalization (scheme
and host lowercased, default port and trailing slash stripped). That
normalization rule is documented on the mcp-connector page's "Provide
authentication at session creation" section
(`https://platform.claude.com/docs/en/managed-agents/mcp-connector`), not
the vaults page — see Warning 1 below for what happens when the URLs
don't match.

## 4. Apply the environment

```powershell
ant apply --lock-file linkedinmcp\platform\claude-lock.json --dry-run linkedinmcp\platform\environments\cloud.yaml
```

Check: the plan shows one resource to create, and its `Name` column shows
you the path form this CLI build uses for lockfile keys (see the
`TODO(verify)` above). Once that looks reasonable, apply for real:

```powershell
ant apply --lock-file linkedinmcp\platform\claude-lock.json linkedinmcp\platform\environments\cloud.yaml
```

Approve the plan (`y`). Note the `id` it prints (a string like
`env_01...`) from the `Status` column, or read it from
`linkedinmcp\platform\claude-lock.json` afterward if you miss it.

```powershell
$EnvironmentId = "<paste the id>"
```

If instead it **rejects `allowed_hosts: []`**, delete that one line from
`environments\cloud.yaml` and run the command again. The schema documents
the field as an optional array or null with no stated minimum, and every
worked example happens to show it populated, so an empty list is within
the documented type but was never tested against the live API. Omitting
the line means the same thing: no hosts beyond the MCP servers
`allow_mcp_servers` covers.

## 5. Fill in and apply both agent definitions

Edit `linkedinmcp\platform\agents\scheduled.md` **and**
`linkedinmcp\platform\agents\interactive.md`, replacing
`REPLACE-WITH-SERVICE-URL` with the value of `$OutreachUrl` from step 2 —
the whole string, trailing slash included, with nothing appended (see the
comment at the top of either file for why). Then:

```powershell
ant apply --lock-file linkedinmcp\platform\claude-lock.json linkedinmcp\platform\agents\scheduled.md
ant apply --lock-file linkedinmcp\platform\claude-lock.json linkedinmcp\platform\agents\interactive.md
```

Check: approve each plan, and note each printed `id` (`agent_01...`) from
the `Status` column — or find "LinkedIn Outreach (scheduled)" /
"LinkedIn Outreach (interactive)" under Agents in the Console if you'd
rather look it up there.

```powershell
$ScheduledAgentId = "<paste the scheduled agent's id>"
```

## 6. Create one manual session and check it

This tests the **scheduled** agent specifically, since that's the one
about to run unattended in step 8 — it has a much more restricted toolset
than the interactive agent, and this is where you find out whether that
restricted configuration actually works before trusting it to a cron.

```powershell
$SessionId = ant beta:sessions create --agent $ScheduledAgentId --environment-id $EnvironmentId --vault-id $VAULT_ID --title "Manual status check" --transform id --raw-output
$SessionId
```

(Written as one line deliberately — PowerShell's backtick line-continuation
needs the backtick to be the very last character before the newline, which
copy-pasting from a rendered document tends to break silently.)

Open this session in the Claude Console and send it a message: `Report
your status.`

**What a working run looks like:** the agent calls `get_status` once and
reports back the service name (`linkedin-outreach`), the current time and
timezone, the `caps` object (daily message/profile-fetch ceilings, plus
per-contact touch limits), whether `require_approval` is on,
`firestore: "ok"`, and `unipile: "ok"`. (This description comes from
reading the tool's own docstring in `linkedinmcp/mcp_server.py`, not from
a platform.claude.com page — called out here as a code citation, not a
doc citation.)

- If `firestore` reads anything other than `"ok"` — `PermissionDenied`,
  `DefaultCredentialsError` — the service is up and the key was accepted,
  but it can't reach its own database; fix that before trusting any real
  run.
- If `unipile` reads anything other than `"ok"`, the service's *own*
  Unipile configuration — `UNIPILE_MAX_MESSAGES_PER_DAY` and
  `UNIPILE_MAX_PROFILE_FETCHES_PER_DAY` in `Python/.env`, read via
  `UnipileSettings.from_env()`, and unrelated to the vault credential you
  add in step 7 (that one authenticates the *interactive* agent's
  separate, direct MCP connection to Unipile) — couldn't be loaded, and
  `caps` will be missing `messages_per_day` and `profile_fetches_per_day`
  as a result — the numbers couldn't be read, not that they're unlimited.

**If it fails instead, the error name tells you where to look**
(`platform-schema-verified.md`, "Authentication"):

- **`mcp_authentication_failed_error`** — the outreach server answered
  401. This fires both when the key is genuinely wrong *and* when the
  vault has no credential matching the declared URL at all (the platform
  then connects unauthenticated, and our own server is what returns the
  401). Seeing this error does not by itself prove the key is bad — check
  the URL first. See Warning 1.
- **`mcp_connection_failed_error`** — the platform couldn't reach the URL
  at all: DNS, TLS, a timeout, or the Cloud Run service not actually up.
  This is a reachability problem, not a credential problem.

Don't proceed to step 8 until this session comes back clean.

## 7. Add and test the Unipile credential (needed for the interactive agent)

Add a second credential to the same vault:

```powershell
$UnipileApiKey = "<your Unipile API key>"
@"
display_name: unipile bridge key
auth:
  type: static_bearer
  mcp_server_url: https://developer.unipile.com/mcp?branch=v1.0
  token: $UnipileApiKey
"@ | ant beta:vaults:credentials create --vault-id $VAULT_ID
```

**Enter `mcp_server_url` exactly as shown, query string included.** See
the `SCHEMA GAP` comment at the top of `agents/interactive.md`: the
mcp-connector doc's "Provide authentication at session creation" section
(`https://platform.claude.com/docs/en/managed-agents/mcp-connector`)
documents URL normalization for scheme, host, port and trailing slash
only, and says nothing about query strings, so whether `?branch=v1.0`
needs to match exactly or is stripped before matching has never been
confirmed.

**This credential is unverified in a second way, and you have to test it
by hand.** The Unipile bridge expects an `X-API-KEY` header; a vault
`static_bearer` credential sends `Authorization: Bearer <token>` instead,
and whether the bridge accepts that has never been tested. To check:

1. Apply `agents/interactive.md` if you haven't (step 5), and start a
   manual session with it the same way as step 6, swapping in the
   interactive agent's ID.
2. In that session, ask it to call `get-endpoint` (or `execute-request`)
   for `GET /api/v1/accounts`.
3. **Read the returned body, not just whether the call "succeeded."** The
   bridge forwards whatever the upstream Unipile API returns, so a
   rejected key comes back as a 401 *inside* the tool result's content —
   not as a connection error, and not as `mcp_authentication_failed_error`
   the way a rejected outreach-service key would.

## 8. Fill in and apply the deployment (only after step 6 passes)

Fill in the remaining placeholders in
`linkedinmcp\platform\deployments\morning.md`: `vault_ids` and
`budget.max_list_cost.amount` (see that file's comments — the budget amount
is cents, as a string, and should be roughly 3x the `usage.list_cost` you saw
on the session(s) from step 6). `schedule.timezone` is already set to
`America/New_York` to match `OUTREACH_TZ` in `Python\.env`; if you ever change
one, change the other. The
`agent` and `environment_id` fields are already filled in as relative
paths (`../agents/scheduled.md`, `../environments/cloud.yaml`) — there is
nothing to paste there; `ant apply` resolves them itself.

Apply the whole project together, rather than naming just this one file:

```powershell
ant apply --lock-file linkedinmcp\platform\claude-lock.json linkedinmcp\platform
```

This applies every file under `linkedinmcp\platform\` in one pass. It
costs nothing extra here — the plan shows the environment and both agents
as unchanged, since nothing about them changed — and it sidesteps an
unconfirmed question: whether applying `deployments/morning.md` by itself
can resolve its `../agents/scheduled.md` reference from
`claude-lock.json` alone, without that file also being part of the same
apply. Applying the directory always includes it, so the question doesn't
need an answer.

Check: the plan shows the deployment as a create (and the environment and
agents as unchanged). Approve it and note the deployment's `id` from the
`Status` column, or from `claude-lock.json`. To confirm the actual
computed fire times (`schedule.upcoming_runs_at` on the deployment
object, documented on the deployments-create reference and quoted in
task-1d-report.md), look the deployment up in the Console —
no page fetched for this task shows whether `ant apply`'s own stdout
prints a resource's fields beyond the `Name` / `Status` / ID table shown
above, so treat that as `TODO(verify)` rather than assumed.

**Deployments are applied exactly like every other resource here, not
created with a separate one-shot command.** Re-running `ant apply`
against an edited `deployments/morning.md` — to change the schedule, the
budget, or repoint it at a newly-applied agent version — updates this
same deployment via `claude-lock.json`; it does not create a second,
independent deployment on the same cron. That guarantee depends entirely
on `claude-lock.json` being present and committed — see "How `ant apply`
works in this project" above and Warning 4.

To change the deployment some other way, or to pause it, the Console
works too, as does the deployment-update endpoint
(`PATCH /v1/deployments/{id}`,
`platform.claude.com/docs/en/managed-agents/scheduled-deployments`).

## 9. Connect from Claude Code

```powershell
claude mcp add --transport http outreach $OutreachUrl --header "x-api-key: $OutreachApiKey"
```

For the Claude app (claude.ai, Desktop, mobile), and for reading the request log
when a client cannot connect, see "Connecting to the MCP server" in
[`linkedinmcp/README.md`](../README.md#connecting-to-the-mcp-server).

---

## Warnings

### 1. A wrong URL does not error — it authenticates as nobody

If no vault credential's `mcp_server_url` matches the `outreach` entry's
`url` (after normalization), the platform does not refuse to start the
session. It connects to the server **unauthenticated**, our own
`ApiKeyMiddleware` (`linkedinmcp/http_auth.py`) answers 401, and the
session reports `mcp_authentication_failed_error` — which reads exactly
like "your key is wrong" when the actual problem is "your URL doesn't
match what's in the vault." Quoted from the mcp-connector doc's "Provide
authentication at session creation" section
(`https://platform.claude.com/docs/en/managed-agents/mcp-connector`) —
not the vaults page; `platform-schema-verified.md`'s "Authentication"
section draws on that same mcp-connector page, not a separate vaults
citation. Whenever you see this error, check the URL — both the one in
the agent file and the one in the credential — before assuming the key
itself needs regenerating.

### 2. The container image contains this service's secrets

`Python/.env` — the project's shared environment file, holding
`OUTREACH_API_KEY` alongside the Unipile and Gemini credentials — is
unconditionally copied into the image that `linkedinmcp\deploy.cmd`
builds and pushes; `linkedinmcp/Dockerfile`'s build fails outright if this
file is missing. `linkedinmcp/.env`, the second, optional file holding
this service's own pacing overrides, is copied in as well whenever it
exists locally — `COPY linkedinmcp/ linkedinmcp/` in the Dockerfile
carries its dotfiles along with everything else in that directory.
Anyone with read access to the Artifact Registry repository holding that
image can pull it and read every one of those values straight out of a
filesystem layer. This is a deliberate choice for a small, single-tenant
internal service, not an oversight — but it means access to that
repository is exactly as sensitive as the API key itself, and this note
exists so that stays a decision someone made on purpose rather than one
nobody noticed. If it stops being acceptable, the fix (not baking either
`.env` file into the image; reading secrets from Secret Manager or
mounting them at runtime instead) is a change to `linkedinmcp/Dockerfile`
and `linkedinmcp\deploy.cmd`, not to anything under `platform\`.

### 3. `tools`, `mcp_servers`, and `skills` are replaced wholesale, never merged

Per `platform-schema-verified.md` (established from an earlier reading of
the agent-setup docs, not re-fetched for this task): array fields on an
agent are **fully replaced** by whatever's in the file you apply, every
time. If you hand-edit `agents/scheduled.md` or `agents/interactive.md`
and the edited file is missing an `mcp_servers` entry or a `tools` entry
that the currently-applied version has, `ant apply` doesn't leave the old
one in place — it deletes it. There is no partial update for these three
fields; whatever the file contains becomes the entire array.

### 4. Losing `claude-lock.json` duplicates every resource in this project, not just the deployment

Under the old one-shot `ant beta:deployments create`, this warning only
applied to the deployment, because that was the only resource with no
update path at all. Now that the environment, both agents, and the
deployment are all applied the same way, they all share the same risk:
`ant apply` knows a resource already exists *only* because
`claude-lock.json` says so. Delete it, lose it, or simply forget to
commit it, and the next `ant apply` anyone runs — from a fresh clone,
from a different machine, or just after an accidental `rm` — cannot tell
these files already have live resources behind them. It creates a second
environment, two more agents, and a second deployment right alongside the
first ones, silently, because from `ant apply`'s point of view that looks
exactly like a brand new project.
`linkedinmcp/platform/claude-lock.json` is already carved out of the
blanket `*.json` rule in `Python\.gitignore` for exactly this reason.
Commit it along with every change to a file in this directory — it is not
optional housekeeping.
