@echo off
REM Deploy LinkedIn API to Google Cloud Run
REM Usage: deploy-gcs.cmd [tag]
REM Example: deploy-gcs.cmd v1.0.0

SET tag=%1
if not defined tag SET tag=latest

SET PROJECT=vk-linkedin
SET REGION=us-central1
SET SERVICE=linkedin
SET REGISTRY=us-central1-docker.pkg.dev/%PROJECT%/linkedin/linkedin

echo.
echo === Building Docker image ===
docker build -f Dockerfile.deploy -t linkedin:latest .

echo.
echo === Tagging image: %tag% ===
docker tag linkedin:latest %REGISTRY%:%tag%

echo.
echo === Pushing to Artifact Registry ===
docker push %REGISTRY%:%tag%

echo.
echo === Deploying to Cloud Run ===
gcloud run deploy %SERVICE% ^
    --image %REGISTRY%:%tag% ^
    --platform managed ^
    --region %REGION% ^
    --project %PROJECT% ^
    --allow-unauthenticated ^
    --port 8080

echo.
echo === Deployment complete ===
echo Service URL: https://%SERVICE%-%PROJECT%.%REGION%.run.app
echo Image: %REGISTRY%:%tag%