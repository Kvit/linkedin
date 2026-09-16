# The contacts webapp

A private web application over the LinkedIn contacts in Firestore, for one
person. It runs on Google Cloud Run as `linkedin-contacts`, behind Google
sign-in (Identity-Aware Proxy, IAP), and is built in stages: each stage adds one
function after the previous one is approved.

It complements the other two ways of working the contacts:

| Surface | Does |
|---|---|
| The notebooks | volume: load the backlog, classify thousands, the bulk intro campaign |
| The Claude agent, through `linkedinmcp` | the daily increment behind code guards |
| **This webapp** | see everything about a contact in one place, filter the whole collection, correct and write by hand |

Design: `docs/superpowers/specs/2026-09-16-contacts-webapp-design.md`.
Plan and stages: `docs/superpowers/plans/2026-09-16-contacts-webapp.md`.

## What it does now (stage 1)

| Screen | Path | Shows |
|---|---|---|
| Home | `/` | Total contacts; counts by industry, stage and handling (`none` = unset) |
| Contacts | `/contacts` | Every contact, 100 a page: click a column heading to sort, search by name or headline |
| Contact | `/contacts/{doc_id}` | One contact: classification, stage and its reason, handling, dates, the whole conversation, queued messages, the profile summary |

Every screen's header says when the data was loaded ("data as of", Chicago
time) and has a **Refresh** button that reloads every contact from Firestore.
Nothing on these screens writes anything.

**How the data is loaded.** At startup the app reads every `analysis` document
(selected fields only, never `summary` or email addresses) and every
`extracted` name and headline into one table in memory; that took 14 seconds on
2026-09-16 for 28,675 contacts. Lists, sorting, search and counts work on that
table, so they answer in milliseconds. It is not reloaded on its own: press
Refresh (about 10 seconds; the page waits) after a notebook or the agent
changed contacts. The Contact screen reads Firestore directly each time it
opens, so it is always current.

The load happens inside the startup and inside the Refresh request, never in
the background, because Cloud Run gives a container CPU only while it starts or
answers a request.

## Running it locally, in the dev container

From `Python/`, inside the dev container (see the project's dev container
notes), with the outreach URL read from `linkedinmcp/platform/ids.env`:

```bash
WEBAPP_DEV_USER=vk@pinnacleservice.co WEBAPP_ALLOWED_EMAIL=vk@pinnacleservice.co WEBAPP_OUTREACH_URL=$(sed -n 's/^OUTREACH_URL=//p' linkedinmcp/platform/ids.env) uv run --no-sync uvicorn --factory webapp.app:create_app --host 0.0.0.0 --port 8080
```

It reads the real Firestore database through the local service-account key.
`WEBAPP_DEV_USER` skips Google sign-in and treats every request as that user;
the app prints a warning when it is set, and the deploy script never sets it.
Port 8080 inside the container reaches Windows through VS Code only once the
container has been rebuilt: `forwardPorts` gained 8080 on 2026-09-16, after the
running container was created.

To run the built image on Windows instead, from `Python/` in PowerShell: it
answers at `http://127.0.0.1:8090`, from this machine only, about 15 seconds
after it starts (the contact table loads first). The key is mounted read-only
and is not in the image. The last line stops and removes it.

```powershell
docker build -f webapp/Dockerfile -t linkedin-contacts:build .
$outreach = (Select-String -Path linkedinmcp\platform\ids.env -Pattern '^OUTREACH_URL=(.+)$').Matches[0].Groups[1].Value
docker run -d --name contacts-local -p 127.0.0.1:8090:8080 -e WEBAPP_DEV_USER=vk@pinnacleservice.co -e WEBAPP_ALLOWED_EMAIL=vk@pinnacleservice.co -e WEBAPP_OUTREACH_URL=$outreach -v "$PWD\vk-linkedin-master-service-account.json:/app/vk-linkedin-master-service-account.json:ro" linkedin-contacts:build
docker rm -f contacts-local
```

Tests, also in the container:

```bash
uv run --no-sync pytest tests/webapp -q
```

## Deploying

From `Python/` on Windows (Docker and gcloud run on the host, not in the
container), logged in to gcloud as the project owner:

```bash
webapp\deploy.cmd v0.1.0
```

Use a new tag for every deploy. The script builds and pushes the image, turns on
IAP and its service agent, deploys with `--iap --no-allow-unauthenticated` and
exactly one instance, and grants access. Every step changes nothing when it is
already in place, so the same command is the first deploy and every later one.
It ends by printing the service URL.

**Access.** Only `vk@pinnacleservice.co` gets through IAP
(`roles/iap.httpsResourceAccessor` on the service), and the app checks the IAP
assertion itself as well: its signature, the audience
`/projects/<project number>/locations/us-central1/services/linkedin-contacts`,
the issuer, and the email. Anyone else gets a Google "access denied" page or a
403.

**The first deploy may need a one-time console step.** IAP's built-in sign-in
admits accounts of the organization that owns the project. If `vk-linkedin`
belongs to no organization, `gcloud run deploy --iap` warns that setup is
required: open **Security, Identity-Aware Proxy** in the Cloud console for
`vk-linkedin`, configure the OAuth consent screen when asked (audience
**External**, add `vk@pinnacleservice.co` as a test user), then deploy again.

**If sign-in succeeds but the app answers 403**, the container log names the
reason: `IAP assertion rejected: ... (aud=..., expected ...)` means the audience
differs from the one the deploy script computed.

```bash
gcloud run services logs read linkedin-contacts --region us-central1 --project vk-linkedin --limit 50
```

**Settings** (Cloud Run environment variables; everything else comes from
`Python/.env`, as for the outreach service):

| Variable | Meaning |
|---|---|
| `WEBAPP_ALLOWED_EMAIL` | The one Google account allowed in |
| `WEBAPP_IAP_AUDIENCE` | The IAP JWT audience, computed by `deploy.cmd` |
| `WEBAPP_OUTREACH_URL` | The outreach service's MCP URL, for the routine buttons of a later stage |
| `WEBAPP_DEV_USER` | Local runs only |

**Cost.** One instance stays up (`--min-instances=1`) so the contact table is
always loaded; with Cloud Run's default request-based billing an idle instance
is charged at the reduced idle rate.
