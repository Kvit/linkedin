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

## What it does now (stages 1 to 5 and 8, and the Need my answer view)

| Screen | Path | Shows |
|---|---|---|
| Home | `/` | Total contacts; counts by industry, stage and handling (`none` = unset), each count a link to the Contacts list with that filter applied; the **Get New Contacts** and **Sync Messages** buttons with a panel of their progress and results |
| Contacts | `/contacts` | Every contact, 100 a page: filter, click a column heading to sort, search by name or headline |
| Contact | `/contacts/{doc_id}` | One contact: handling, industry, function, seniority and stage as dropdowns that save at once, the dates beside them; the conversation as a thread, your messages on the right and theirs on the left, a divider for each day; queued messages; the profile summary |

Every screen's header says when the data was loaded ("data as of", Chicago
time), has a **Refresh** button that reloads every contact from Firestore,
and a **Need my answer** button with the number of contacts waiting on you.
The Contact screen's dropdowns and the Home screen's two buttons write; the
rest only reads.

**Need my answer** opens the Contacts list narrowed to the prospects, leads and
contacts with no stage who wrote last: their newest readable message is newer
than your newest readable one, or you never wrote to them. A contact staged
`soft_no`, `reject`, `not_relevant` or `unknown` is left out. A system event,
a deleted message or a blank one does not count as an answer. The list is sorted by when they wrote, newest
first; the other filters and the sort still work inside the view, and
**Show all contacts** leaves it. The number is counted when the data is
loaded, so press **Refresh** after a sync or after answering someone.

**The conversation** shows the transcript the classifiers read: each message
is one paragraph, dated to the day, with its line breaks joined. When a
contact has more than one LinkedIn conversation, each gets a heading. Very
long histories show the newest 20,000 characters and say so.

**The look.** The colours are the two stains a pathology slide is read in:
hematoxylin blue-violet for your messages and every action, eosin pink-red for
the contact's messages and the Need my answer count. Interface text is
Atkinson Hyperlegible Next and message text is Literata, both loaded from
Google Fonts in the browser, with system fonts as the fallback.

**Buttons on the Home screen.** Each runs process steps on the outreach
service, for real, never as a dry run:

- **Get New Contacts** runs `get_contacts` for up to 10 new connections, which
  views their LinkedIn profiles, takes about five minutes and counts against
  the day's profile limit, then `classify_contacts` on exactly the profiles it
  stored. The panel lists each classified contact with its industry, function,
  seniority and whether it is in a target industry. When nothing was stored,
  nothing is classified.
- **Sync Messages** runs `sync_messages`: it stores the LinkedIn messages newer
  than the newest stored one, cancels queued messages to anyone who replied,
  refreshes the contact stats and stages up to 50 conversations.

One run at a time: both buttons are disabled while a run is going, and the Home
screen reloads itself every 3 seconds, reading the running job each time. The
run advances only while the Home screen is open, so the second step of Get New
Contacts starts when you next open it. The latest run is held in memory, so a
restart or a deploy forgets it; the outreach service still finishes its jobs,
and `get_run_report` there lists them. After a run, press **Refresh** to see
the changes in the lists and counts.

**Filters on the Contacts screen.** Tick one or more values for industry,
function, seniority, stage or handling, choose Any, Yes or No for **Message
Sent** and **Message Received**, and press **Apply**; **Clear** removes them
all. Values ticked in one filter widen it, so industry RCM and Pathology keeps
both; different filters narrow each other, and combine with the search and
the sort, and the sort headings and page links keep them. Each list shows the
values the contacts actually hold, most frequent first; `none` is an unset
value, so industry `none` lists the unclassified contacts.

- **Message Sent**: Yes keeps the contacts you have written to (`sent_total`
  above 0), No those you have not; Any, the default, does not filter.
- **Message Received**: Yes keeps the contacts with at least one readable
  message from them, as the Contact screen's conversation shows it, No those
  with none; Any is the default. This is not the **Replied** column, which
  counts only answers in a conversation you opened: on 2026-09-16, 159
  contacts had written to you and show no replies.
- **Connected**: the date from the fetch queue, set for the connections the
  outreach service found, else the date LinkedIn Helper stored. On 2026-09-16,
  2,806 of 28,675 contacts had one; the column is empty for the rest.

**Editing a contact.** On the Contact screen, Handling, Industry, Function,
Seniority and Stage are dropdowns, Handling first. Choosing a value saves it to
the contact's `analysis` document at once, updates that contact's row in the
in-memory table, and reloads the page with a line saying what was saved. A
write that another website's page sends is refused with 403: the browser's
`Sec-Fetch-Site` header must say the request came from this site.

- **Handling** offers `none`, `exclude` and `manual`. `exclude` or `manual`
  also cancels the contact's pending and approved queued messages, and the
  page says how many; `none` clears the field.
- **Industry, Function, Seniority and Stage** offer the values the classifiers
  use. A value chosen here is marked **set by hand**: the field's name goes
  into the document's `hand_set` list and the time into `hand_set_at`; a stage
  also gets the reason "set by hand". The classification notebooks,
  `classify_contacts` and `classify_stages` then leave that field alone, but a
  new message from the contact, or `classify_stages` with `force`, still
  re-stages them. The outreach service does this from `v2.4.0`, deployed
  2026-09-16.
- **Release** takes the field out of `hand_set`, so the classifiers may change
  it again; the value stays until they do.
- A field without a value shows `none` greyed out. It cannot be chosen, so a
  classification or a stage can be changed here but not cleared.

**How the data is loaded.** At startup the app reads every `analysis` document
(selected fields only, never `summary` or email addresses), every `extracted`
name, headline and connection date, the fetch queue's connection dates and
every message into one table in memory; that took 14 to 17 seconds on
2026-09-16 for 28,675 contacts. Lists, filters, sorting, search and counts work
on that table, so they answer in milliseconds. It is not reloaded on its own:
press Refresh (about 15 seconds; the page waits) after a notebook or the agent
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

**Running now:** `v0.7.0`, revision `linkedin-contacts-00006-vxn`, deployed
2026-09-16 at `https://linkedin-contacts-5czydyxqoa-uc.a.run.app`. Its
application startup took 32 seconds.

**The deploy needed no console step.** IAP's built-in sign-in admits accounts
of the organization that owns the project, and `vk-linkedin` belongs to the
`pinnacleservice.co` organization. In a project without an organization,
`gcloud run deploy --iap` warns that setup is required: open **Security,
Identity-Aware Proxy** in the Cloud console, configure the OAuth consent screen
when asked, then deploy again.

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
