@echo off
REM Deploy linkedin-contacts, the contacts webapp, to Google Cloud Run behind IAP.
REM
REM   Usage:    webapp\deploy.cmd <tag>        (from Python\)
REM   Example:  webapp\deploy.cmd v0.1.0
REM
REM A sibling of linkedinmcp\deploy.cmd, and its comments hold for this file:
REM the tag is required and must be new for every deploy (the script rebuilds
REM from the code on disk before tagging), paths are anchored with %%~dp0, and
REM every gcloud and docker call uses `call` so control comes back to this
REM script. What differs is below.
REM
REM IAP setup is part of the script and changes nothing when it is already in
REM place: the IAP API, its service agent, the agent's right to invoke the
REM service, and access for exactly one Google account (ALLOWED_EMAIL). The
REM first deploy in a project may ask for a one-time OAuth consent screen in the
REM Cloud console; webapp\README.md says where.

setlocal

SET PROJECT=vk-linkedin
SET REGION=us-central1
SET SERVICE=linkedin-contacts
SET REGISTRY=us-central1-docker.pkg.dev/%PROJECT%/linkedin/linkedin-contacts
SET ALLOWED_EMAIL=vk@pinnacleservice.co

SET "tag=%~1"
if not defined tag goto :usage

REM The IAP audience names the project NUMBER. Read it first: it also proves
REM gcloud is logged in before anything is built or pushed.
SET "PROJECT_NUMBER="
for /f "delims=" %%n in ('gcloud projects describe %PROJECT% --format "value(projectNumber)"') do set "PROJECT_NUMBER=%%n"
if not defined PROJECT_NUMBER (
    echo.
    echo ERROR: could not read the project number. Is gcloud logged in? Run: gcloud auth login
    exit /b 1
)

REM The outreach service's MCP URL, from the file its own deploy writes. The
REM routine buttons of a later stage call it; the settings require it now.
SET "OUTREACH_URL="
for /f "tokens=1,* delims==" %%a in ('findstr /b OUTREACH_URL= "%~dp0..\linkedinmcp\platform\ids.env"') do set "OUTREACH_URL=%%b"
if not defined OUTREACH_URL (
    echo.
    echo ERROR: OUTREACH_URL was not found in linkedinmcp\platform\ids.env.
    exit /b 1
)

SET "AUDIENCE=/projects/%PROJECT_NUMBER%/locations/%REGION%/services/%SERVICE%"

echo.
echo === Building Docker image ===
call docker build -f "%~dp0Dockerfile" -t linkedin-contacts:build "%~dp0.."
if errorlevel 1 (
    echo.
    echo ERROR: docker build failed. Nothing was tagged, pushed or deployed.
    exit /b 1
)

echo.
echo === Tagging image: %tag% ===
call docker tag linkedin-contacts:build %REGISTRY%:%tag%
if errorlevel 1 (
    echo.
    echo ERROR: docker tag failed. Nothing was pushed or deployed.
    exit /b 1
)

echo.
echo === Pushing to Artifact Registry ===
call docker push %REGISTRY%:%tag%
if errorlevel 1 (
    echo.
    echo ERROR: docker push failed. Cloud Run is unchanged.
    exit /b 1
)

echo.
echo === Enabling IAP and its service agent ===
call gcloud services enable iap.googleapis.com --project %PROJECT%
if errorlevel 1 (
    echo.
    echo ERROR: enabling iap.googleapis.com failed. Cloud Run is unchanged.
    exit /b 1
)
call gcloud beta services identity create --service=iap.googleapis.com --project=%PROJECT%
if errorlevel 1 (
    echo.
    echo ERROR: creating the IAP service agent failed. Cloud Run is unchanged.
    exit /b 1
)

REM --min-instances=1 and --max-instances=1: one instance, always up, so the
REM contact frame it builds at startup is warm and there is exactly one copy.
REM --timeout=300 covers the Refresh button, which rebuilds inside its request.
REM --update-env-vars, never --set-env-vars, which would wipe every other
REM variable set on the service.
echo.
echo === Deploying to Cloud Run ===
call gcloud run deploy %SERVICE% ^
    --image %REGISTRY%:%tag% ^
    --platform managed ^
    --region %REGION% ^
    --project %PROJECT% ^
    --no-allow-unauthenticated ^
    --iap ^
    --min-instances=1 ^
    --max-instances=1 ^
    --concurrency=4 ^
    --memory=1Gi ^
    --timeout=300 ^
    --update-env-vars=WEBAPP_ALLOWED_EMAIL=%ALLOWED_EMAIL%,WEBAPP_IAP_AUDIENCE=%AUDIENCE%,WEBAPP_OUTREACH_URL=%OUTREACH_URL% ^
    --port 8080
if errorlevel 1 (
    echo.
    echo ERROR: gcloud run deploy failed. The image %REGISTRY%:%tag% is pushed,
    echo but Cloud Run was not updated and is still serving the previous revision.
    exit /b 1
)

echo.
echo === Granting access ===
REM IAP calls the service as its own service agent, which needs the invoker role.
call gcloud run services add-iam-policy-binding %SERVICE% --region %REGION% --project %PROJECT% ^
    --member "serviceAccount:service-%PROJECT_NUMBER%@gcp-sa-iap.iam.gserviceaccount.com" ^
    --role roles/run.invoker --format none
if errorlevel 1 (
    echo.
    echo ERROR: granting the IAP service agent the invoker role failed.
    exit /b 1
)
REM Exactly one person gets through IAP.
call gcloud iap web add-iam-policy-binding --project %PROJECT% --resource-type=cloud-run ^
    --service=%SERVICE% --region=%REGION% ^
    --member "user:%ALLOWED_EMAIL%" --role roles/iap.httpsResourceAccessor --format none
if errorlevel 1 (
    echo.
    echo ERROR: granting %ALLOWED_EMAIL% access through IAP failed.
    exit /b 1
)

SET "SERVICE_URL="
for /f "delims=" %%u in ('gcloud run services describe %SERVICE% --region %REGION% --project %PROJECT% --format "value(status.url)"') do set "SERVICE_URL=%%u"

echo.
echo === Deployment complete ===
echo Version:     %tag%
echo Image:       %REGISTRY%:%tag%
echo Open:        %SERVICE_URL%
echo Signs in:    %ALLOWED_EMAIL% only
echo.
echo To roll back, point Cloud Run at an older image instead of rerunning this:
echo   gcloud run deploy %SERVICE% --image %REGISTRY%:^<older-tag^> --region %REGION% --project %PROJECT%
exit /b 0

:usage
echo.
echo ERROR: a version tag is required. There is no default.
echo.
echo   Usage:    webapp\deploy.cmd ^<tag^>
echo   Example:  webapp\deploy.cmd v0.1.0
echo.
echo Use a new tag for every deploy. This script always rebuilds from the code
echo on disk before tagging, so reusing an old tag overwrites that build.
exit /b 1
