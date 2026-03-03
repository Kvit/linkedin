# LinkedIn Profile Collector

A FastAPI-based service for collecting, processing, and storing LinkedIn profile data in Google Cloud Firestore.

## Overview

This package provides:
- **REST API** for receiving LinkedIn profile data via POST requests
- **Firestore integration** for persistent storage of profiles
- **Data export utilities** to convert stored profiles to CSV/TXT formats
- **Profile summarization** by joining key fields (position, education, occupation)

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
| `OPENAI_API_KEY` | OpenAI API key for analysis notebooks | For notebooks |
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

## AI Integration

This project uses **OpenAI** (not Google AI) for profile analysis and outreach automation. The AI features are implemented in Jupyter notebooks using the OpenAI **Responses API** (`client.responses.create`).

### AI-Powered Features

| Notebook | Feature | Model | Description |
|----------|---------|-------|-------------|
| `analysis.ipynb` | Profile Analysis | `gpt-5-mini` | Analyzes profile summaries to extract structured data: **industry**, **job function**, and **seniority level** |
| `intro.ipynb` | Introduction Generator | `gpt-5.2` | Creates personalized LinkedIn connection request messages based on profile content |

### How It Works

1. **Profile Analysis** (`analysis.ipynb`): Reads profiles from the `extracted` Firestore collection and uses OpenAI to classify each person by:
   - Industry (e.g., Technology, Finance, Healthcare)
   - Function (e.g., Engineering, Sales, Marketing)
   - Seniority (e.g., Entry, Mid, Senior, Executive, C-Level)

   Results are written to the `analysis` collection. The notebook also includes statistics on classification coverage and a `get_member_distance()` helper for filtering by LinkedIn network distance.

2. **Introduction Generator** (`intro.ipynb`): Reads profile summaries from the `extracted` collection and generates brief, personalized connection request introductions (2-3 sentences). Results are stored in the `analysis` collection.

### Setup

To enable AI features, set your OpenAI API key in the `.env` file:

```bash
OPENAI_API_KEY=your-api-key-here
```

Note: The core API server (`main.py`) does not require OpenAI—it only uses Google Cloud Firestore for data storage.
