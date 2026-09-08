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

*If not set, the app looks for `vk-linkedin-master-service-account.json` in the project root.

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
| `industry` | RCM, Pathology, Medical Lab, Hospital, Health IT, Health Insurance Payer, Pharma, Other Healthcare, Non-Healthcare |
| `function` | Operations, Finance, IT, Clinical, Executive, Consulting, Owner, Sales & Marketing, Other |
| `seniority` | Executive, VP, Director, Manager, Staff, Owner, Unknown |

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

### Setup

Set your Google Gemini API key in `.env`:

```bash
GOOGLE_API_KEY=your-api-key-here
```

Note: The core API server (`main.py`) does not require Gemini — it only uses Firestore.
