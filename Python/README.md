# LinkedIn Profile Collector

A FastAPI-based service for collecting, processing, and storing LinkedIn profile data in Google Cloud Firestore.

## Overview

This package is an end-to-end **LinkedIn profile data pipeline**. It receives raw LinkedIn profile JSON (e.g. from a browser extension or scraper), enriches and stores each profile in Google Cloud Firestore, then — through manually-run Jupyter notebooks — classifies each profile with Google Gemini and generates personalised outreach messages. Results are exported to CSV for downstream use.

**Important:** The API server and the AI classification are separate, independent steps. Saving a profile via the API does **not** automatically trigger any AI processing. You must run the notebooks yourself when you want to classify new profiles.

The pipeline has four stages, each triggered manually:

1. **Ingest** (`main.py`) — A FastAPI server accepts raw LinkedIn profile JSON via a POST request.
2. **Enrich & Store** (`main.py`) — The API flattens key profile fields (current position, past positions, education, skills, occupation) into a single plain-text `summary` and saves the full document to the `extracted` Firestore collection. This is the only step that runs automatically on each POST.
3. **Classify** (`analysis.ipynb`, run manually) — The notebook streams all profiles from `extracted`, identifies which ones are new or have changed since the last run, sends only that subset to Gemini for classification (industry / job function / seniority), and writes results to the `analysis` Firestore collection. Already-classified records are skipped automatically.
4. **Export** (`collection-tocsv.py`, run manually) — Dumps the `analysis` collection to `analysis.csv` and `analysis.txt`.

## Workflow

```
[Browser extension / scraper]
        │
        │  raw LinkedIn profile JSON
        ▼
POST /add-profile/              ← automatic (runs on every POST)
        │
        │  join_keys() builds plain-text summary from
        │  miniProfile, currentPosition, positions,
        │  occupation, extra, skills, educations
        ▼
Firestore: extracted collection
        │
        │  ◀─── NO automatic trigger beyond this point ───▶
        │
        │  run analysis.ipynb manually
        ▼
Phase A  stream all docs from  extracted
Phase B  stream all docs from  analysis   (existing results)
Phase C  join → find new / changed / unclassified docs only
Phase D  send filtered subset to Gemini → classify each profile
        │
        ▼
Firestore: analysis collection
  (industry, function, seniority, summary, profileUrl per doc)
        │
        │  run collection-tocsv.py manually  (or last cell of notebook)
        ▼
analysis.csv  /  analysis.txt
```

## Project Structure

| File | Description |
|------|-------------|
| `main.py` | FastAPI server with `/add-profile/` and `/echo/` endpoints |
| `functions.py` | Helper functions for profile processing (ID extraction, text joining) |
| `collection-tocsv.py` | Export Firestore collection to CSV and TXT files |
| `analysis.ipynb` | Classify profiles with Gemini, including re-classifying changed summaries |
| `new-contacts.ipynb` | Pull new connections through Unipile, store them, then classify the unclassified backlog |
| `lib/unipile/` | Unipile API client for LinkedIn contacts and messaging |
| `tests/` | Test suite (`pytest`; `pytest -m live` hits the real API, read-only) |
| `Dockerfile.deploy` | Docker configuration for cloud deployment |

## API Endpoints

- **POST `/add-profile/`** - Add or update a LinkedIn profile in Firestore
- **GET `/echo/{text}`** - Simple echo endpoint for testing

## Setup

### Prerequisites
- Google Cloud SDK configured (`gcloud init`)
- Service account credentials file: `vk-linkedin-master-service-account.json` (place in project root)

### Environment Variables

Create a `.env` file in the project root and configure:

| Variable | Description | Required |
|----------|-------------|----------|
| `GOOGLE_API_KEY` | Google Gemini API key for analysis notebooks | For notebooks |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON | Optional* |
| `UNIPILE_API_KEY` | Unipile access token | For LinkedIn ops |
| `UNIPILE_DNS` | Unipile tenant host, e.g. `api62.unipile.com:19262` | For LinkedIn ops |

Every other setting has a working default; see [the reference below](#unipile-configuration-reference) and `.env.example`.

*If not set, the app looks for `vk-linkedin-master-service-account.json` in the project root.

### Unipile configuration reference

Every variable below is optional and shown with its default. Defaults live in
[`lib/unipile/config.py`](lib/unipile/config.py); the pacing mechanism is in
[`lib/unipile/pacing.py`](lib/unipile/pacing.py).

**Daily caps** — per UTC day, per account, persisted to disk so a kernel restart
does not hand back a fresh allowance. Hitting one raises `BudgetExhausted`.

| Variable | Default | What it does |
|----------|---------|--------------|
| `UNIPILE_MAX_INVITES_PER_DAY` | `25` | Connection invitations per day. |
| `UNIPILE_MAX_MESSAGES_PER_DAY` | `50` | Messages per day, new chats and replies alike. |
| `UNIPILE_MAX_PROFILE_FETCHES_PER_DAY` | `250` | Profile reads per day. The binding constraint in practice; retries of a throttled fetch count against it too. |

**Pacing** — every budgeted call waits first. The gap is drawn fresh each time
from a distribution skewed toward the short end, so the mean sits near a third
of the way up the range (~40s for 20-90), not at the midpoint.

| Variable | Default | What it does |
|----------|---------|--------------|
| `UNIPILE_MIN_DELAY_SECONDS` | `20` | Shortest gap between calls. Raising it is the most effective way to look less automated, and the most expensive in wall-clock time. |
| `UNIPILE_MAX_DELAY_SECONDS` | `90` | Longest ordinary gap. |
| `UNIPILE_LONG_PAUSE_EVERY` | `20` | Average number of calls between long breaks. Each call rolls independently, so breaks do not fall on a fixed stride. `0` disables them. |
| `UNIPILE_LONG_PAUSE_MIN_SECONDS` | `180` | Shortest long break. |
| `UNIPILE_LONG_PAUSE_MAX_SECONDS` | `600` | Longest long break. |

At these defaults a full 250-profile day takes roughly four hours before any
retries. That is the intended cost.

**Throttle recovery** — throttling arrives as HTTP 200 with the requested
sections empty, never as an error.

| Variable | Default | What it does |
|----------|---------|--------------|
| `UNIPILE_THROTTLE_RETRIES` | `2` | Extra attempts for a profile whose sections were withheld. `2` means up to three fetches, waiting ~2x then ~4x the normal gap. Each attempt is charged to the daily profile budget. `0` skips the profile immediately and leaves it for a later run. |
| `UNIPILE_MAX_CONSECUTIVE_THROTTLED` | `5` | Profiles in a row that may exhaust their retries before `get_profile` raises `ThrottleLockout` and the run stops. One complete profile resets the count. `0` disables the stop. |

Retries bound one slug; the lockout bounds the run. Without it a throttled
account keeps fetching at the 8x pace until the daily budget is gone, storing
nothing — roughly 83 slugs and many hours for zero profiles.

The backoff multiplier itself is not an environment variable: each withheld
response doubles every subsequent gap, capped by `HumanCadence.MAX_BACKOFF`
(`8.0`) in [`lib/unipile/pacing.py`](lib/unipile/pacing.py). A clean response
resets it.

**Provider quota** — LinkedIn reports its own invitation quota usage as a
percentage; these act on that reading, independently of the caps above.

| Variable | Default | What it does |
|----------|---------|--------------|
| `UNIPILE_USAGE_WARN_PCT` | `75` | Log a warning past this reported usage. |
| `UNIPILE_USAGE_HALT_PCT` | `90` | Stop sending invitations at this reading. |

**Other**

| Variable | Default | What it does |
|----------|---------|--------------|
| `UNIPILE_ACCOUNT_ID` | resolved from `GET /accounts` | Pin a specific connected account. |
| `UNIPILE_BUDGET_STATE_PATH` | `.unipile_budget.json` | Where daily counters live. Local run state, gitignored. |
| `UNIPILE_PROFILE_SECTIONS` | `about,experience,education,skills,certifications,languages,projects` | Sections to request. Without them the API returns no experience, education, skills or About text at all. Asking for more also gives LinkedIn more to withhold. |
| `UNIPILE_TIMEOUT_SECONDS` | `30` | HTTP timeout per request. Reads retry 4 times over 2/10/30s on 5xx and network errors; writes are never retried. |


### Installation

```bash
uv sync
```

### Running Locally

```bash
uv run uvicorn main:app --reload --port 8080
```

### Running with Docker (Windows)

Build the Docker images:

```cmd
build.cmd
```

Run the container locally with credentials mapped:

```cmd
start.cmd
```

### Exposing Locally (for webhooks/testing)

```bash
ngrok http http://localhost:8080
```

Note: The Dockerfile copies `.env` into the image (`/etc/environment`), so the file must exist before building.

## Deployment

### Configure Docker for GCP Artifact Registry

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
```

### Deploy to Cloud Run (Windows)

```cmd
deploy-gcs.cmd [tag]
```

This script builds the image, pushes to Artifact Registry, and deploys to Cloud Run. Tag defaults to `latest`.

### Manual Build and Deploy

```bash
# Build image
docker build -f Dockerfile.deploy -t linkedin:latest .

# Tag and push
docker tag linkedin:latest us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin:latest
docker push us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin:latest

# Deploy to Cloud Run
gcloud run deploy linkedin --image us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin:latest --platform managed --region us-central1 --project vk-linkedin
```

## Data Export

Export Firestore data to CSV:

```bash
python collection-tocsv.py
```

This generates `analysis.csv` and `analysis.txt` from the `analysis` Firestore collection.

## AI Classification (`analysis.ipynb`)

AI classification is **not automatic**. You run this notebook manually whenever you want to process new profiles.

### What it does

The notebook uses Google Gemini to classify each profile into three dimensions:

| Field | Values |
|-------|--------|
| `industry` | RCM, Pathology, Medical Lab, Physician Practice, Hospital, Health IT, Health Insurance Payer, Pharma, Other Healthcare, Non-Healthcare |
| `function` | Operations, Finance, IT, Clinical, Executive, Consulting, Owner, Sales & Marketing, Other |
| `seniority` | Executive, VP, Director, Manager, Staff, Owner, Unknown |

All three fields describe the person's **current** role. `industry` is the employer's business, and
laboratories are split by owner because that is what decides who buys: `Medical Lab` is a lab that is
independent or owned by a physician practice, `Pathology` is a pathology practice (including its lab),
`Physician Practice` is a practice with no lab in evidence, and hospital-owned labs and pathology
departments stay under `Hospital`. Functional executives take their domain (a CFO is `Finance`, not
`Executive`); founders take `seniority = Owner`. The vocabulary is enforced by `Literal` types on the
`ProfileAnalysis` schema, so Gemini cannot return a label outside these lists.

### How the notebook works

The notebook is incremental — it does not re-call Gemini for records that are already classified and unchanged:

1. **Phase A** — Stream all documents from `extracted` into memory.
2. **Phase B** — Stream all existing documents from `analysis` into memory.
3. **Phase C** — Join the two sets and flag only the docs that need Gemini:
   - New profiles not yet in `analysis`
   - Profiles whose `summary` has changed since last run
   - Profiles with missing `industry`, `function`, or `seniority` fields
   - Profiles with a summary shorter than 50 characters are skipped entirely
4. **Phase D** — Send only the flagged subset to Gemini. Results are written back to `analysis`.

To force reclassification of every record (e.g. after changing the prompt), set `REPROCESS_ALL = True` in the configuration cell before running.
The taxonomy changed on 2026-09-08 (`Medical Lab`, `Pathology` and `RCM` were redefined, `Physician Practice` added), so records classified before that date must be reprocessed once.

### Setup

Set your Google Gemini API key in `.env`:

```bash
GOOGLE_API_KEY=your-api-key-here
```

Note: The core API server (`main.py`) does not require Gemini — it only uses Firestore.

## LinkedIn operations (`lib/unipile`)

Replaces LinkedIn Helper for contact and messaging work, using the Unipile API.
It is a pure API client: HTTP, typed models, pagination and send budgets. It
does not touch Firestore and does not run campaigns -- orchestration stays in
your scripts and notebooks.

```python
from functions import join_keys
from lib.unipile import SUMMARY_KEYS, UnipileClient, to_lh_document

with UnipileClient.from_env() as li:
    for relation in li.users.iter_relations():
        if relation.public_identifier in already_in_firestore:
            continue

        profile = li.users.get_profile(relation.public_identifier)
        if not profile.is_complete:
            continue  # LinkedIn throttled sections; retry another day

        document = to_lh_document(profile)
        document["summary"] = join_keys(document, SUMMARY_KEYS)
        db.collection("extracted").document(document["id"]).set(document)
```

The resulting document matches the LinkedIn Helper shape, so the same Gemini prompt
and `analysis.ipynb` work on both sources unchanged.

### Pacing and throttling

LinkedIn watches the *rhythm* of calls, not just their number, so the client
waits a randomised, browsing-like interval before every profile fetch, message
and invitation: mostly 20-90s, skewed short, with a 3-10 minute break roughly
every 20 calls. All of it is tunable in `.env` (`UNIPILE_MIN_DELAY_SECONDS`,
`UNIPILE_LONG_PAUSE_EVERY`, ...). At that pace a full 250-profile day takes
several hours, which is the point.

Throttling does not arrive as an error. LinkedIn returns HTTP 200 with the
withheld sections empty, named in `throttled_sections`. When that happens the
client says so, doubles every subsequent gap, and retries the fetch
(`UNIPILE_THROTTLE_RETRIES`, default 2); a clean response restores the normal
pace. Each attempt costs a fetch from the daily budget, because LinkedIn
counted it either way.

```
LinkedIn withheld skills, experience for jane-doe (attempt 1 of 3) -- throttling
detected; pausing longer, then trying again.
LinkedIn is throttling -- waiting 2m 38s before the next call (pace slowed 4x).
```

A profile whose sections never arrive is **never stored**: `to_lh_document`
refuses to build a document from an incomplete profile, so a classification can
never be cached from data LinkedIn withheld. Those slugs are simply retried on a
later run.

When throttling does not clear, the retries alone would not stop anything — they
bound one slug, not the run. So after `UNIPILE_MAX_CONSECUTIVE_THROTTLED`
profiles in a row exhaust their retries (default 5), `get_profile` raises
`ThrottleLockout` and the caller stops for the day. A single complete profile
resets the count.


`new-contacts.ipynb` runs exactly this loop and then classifies. Its two halves
are deliberately independent: the fetch phase stores documents in `extracted`,
and the classify phase ignores what that phase just fetched, asking Firestore
instead which documents in `extracted` have no complete document in `analysis`.
So a run interrupted between the two, or a profile that arrived through
`POST /add-profile/`, is picked up by the next run rather than sitting stored and
unclassified forever.

### Things worth knowing

- **Reads retry, writes never.** Invitations and messages are not idempotent, so
  a retry after a timeout would send a second one to a real person.
- **Budgets are enforced, not advisory.** Every invitation, message and profile
  fetch is capped per day and delayed by a random interval. Counters live in a
  file so a notebook restart does not hand back a fresh allowance.
- **Incomplete profiles must not be stored.** LinkedIn returns throttled
  sections as empty with HTTP 200. Check `profile.is_complete` before writing,
  or the pipeline caches a classification built from partial data.
- **Restriction trips a circuit breaker.** After a `403 account_restricted` the
  client refuses further writes, though withdrawing invitations still works.

### Running the tests

```bash
uv run pytest                 # unit tests, fully mocked
uv run pytest -m live         # read-only smoke tests against the real account
```

