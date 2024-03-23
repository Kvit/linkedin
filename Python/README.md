# Instructions and notes

Init gcloud SDK  to use google APIs : `gcloud init`

Pip install requirements: `pip install -r requirements.txt`

Runing fastAPI server on specific local port: `uvicorn main:app --reload --port 8080`

Exposing local server on the web: `ngrok http http://localhost:8080`

Configure docker to use gcloud SDK: `gcloud auth configure-docker us-central1-docker.pkg.dev`
