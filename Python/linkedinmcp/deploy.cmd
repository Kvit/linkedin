@echo off
REM Deploy linkedin-outreach to Google Cloud Run.
REM
REM   Usage:    linkedinmcp\deploy.cmd <tag>        (from Python\)
REM   Example:  linkedinmcp\deploy.cmd v1.0.1
REM
REM The tag is REQUIRED and there is no default. It becomes the image's tag in
REM Artifact Registry, and Cloud Run deploys that exact image as a new revision.
REM
REM Use a NEW tag for every deploy. This script always rebuilds from the code on
REM disk before tagging, so running it with an old tag does not redeploy that
REM build -- it overwrites that tag with today's code, and the old image is gone.
REM There used to be a `latest` default, which made that the normal case: every
REM deploy replaced the one tag there was, so no previous build could be named.
REM
REM Paths are anchored to this script's own location with %~dp0, so it behaves
REM the same from any working directory. `docker build`'s -f and context
REM arguments otherwise resolve against the CALLER's directory, which is easy to
REM get backwards for a script that lives one level inside its own build context.
REM
REM Every `gcloud` and `docker` below is invoked with `call`. On Windows `gcloud` is
REM `gcloud.cmd`, a batch file, and running one batch file from another WITHOUT
REM `call` hands control over permanently: the rest of this script would never
REM run. Verified empirically -- without `call`, the service deployed and then
REM the script stopped silently, never writing platform\ids.env and never
REM printing the MCP URL the setup runbook's next step needs. `docker` is an
REM executable here, where `call` changes nothing, but it costs nothing either and
REM keeps the script correct on a machine where docker is a batch wrapper.

setlocal

SET PROJECT=vk-linkedin
SET REGION=us-central1
SET SERVICE=linkedin-outreach
SET REGISTRY=us-central1-docker.pkg.dev/%PROJECT%/linkedin/linkedin-outreach

SET "tag=%~1"
if not defined tag goto :usage

echo.
echo === Building Docker image ===
call docker build -f "%~dp0Dockerfile" -t linkedin-outreach:build "%~dp0.."
if errorlevel 1 (
    echo.
    echo ERROR: docker build failed. Nothing was tagged, pushed or deployed.
    exit /b 1
)

echo.
echo === Tagging image: %tag% ===
call docker tag linkedin-outreach:build %REGISTRY%:%tag%
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
echo === Deploying to Cloud Run ===
call gcloud run deploy %SERVICE% ^
    --image %REGISTRY%:%tag% ^
    --platform managed ^
    --region %REGION% ^
    --project %PROJECT% ^
    --allow-unauthenticated ^
    --timeout=600 ^
    --concurrency=4 ^
    --port 8080
if errorlevel 1 (
    echo.
    echo ERROR: gcloud run deploy failed. The image %REGISTRY%:%tag% is pushed,
    echo but Cloud Run was not updated and is still serving the previous revision.
    exit /b 1
)

echo.
echo === Deployment complete ===

REM deploy-gcs.cmd, the sibling script for the ingest service, echoes a guessed
REM URL of the form https://%SERVICE%-%PROJECT%.%REGION%.run.app. That form is
REM wrong: Cloud Run URLs are built from the project NUMBER or a hash, never the
REM project id, so a guess built from %PROJECT% is not a reachable address. Read
REM the actual URL back from Cloud Run instead. What comes back is the hashed
REM form, https://SERVICE-HASH-uc.a.run.app. The project-number form that
REM `gcloud run deploy` prints above reaches the same service, but ids.env, the
REM agent definitions and the vault must all use one string, and this is it.
REM `for /f` runs its command in a child cmd, so it needs no `call`.
SET "SERVICE_URL="
for /f "delims=" %%u in ('gcloud run services describe %SERVICE% --region %REGION% --project %PROJECT% --format "value(status.url)"') do set "SERVICE_URL=%%u"
if not defined SERVICE_URL (
    echo.
    echo WARNING: the deploy succeeded but the service URL could not be read back,
    echo so platform\ids.env was not written. Find the URL in the Cloud Run console.
    exit /b 1
)

if not exist "%~dp0platform" mkdir "%~dp0platform"
(echo OUTREACH_URL=%SERVICE_URL%/mcp/)>"%~dp0platform\ids.env"

echo.
echo === Service info ===
echo Version:      %tag%
echo Image:        %REGISTRY%:%tag%
echo Service URL:  %SERVICE_URL%
echo MCP endpoint: %SERVICE_URL%/mcp/
echo Saved to linkedinmcp\platform\ids.env as OUTREACH_URL=%SERVICE_URL%/mcp/
echo.
REM The service answers %SERVICE_URL%/mcp/ and, since v1.0.2, %SERVICE_URL%/mcp
REM too: app.py rewrites the slashless path instead of redirecting it, because
REM the Claude app's connector does not follow redirects. ids.env keeps the
REM slashed form, which the agent definitions and the vault credential copy.
echo Register it with Claude Code:
echo   claude mcp add --transport http outreach %SERVICE_URL%/mcp/ --header "x-api-key: ..."
echo.
echo To roll back, point Cloud Run at an older image instead of rerunning this:
echo   gcloud run deploy %SERVICE% --image %REGISTRY%:^<older-tag^> --region %REGION% --project %PROJECT%
echo.
echo NOTE -- only relevant if the deployed service answers get_status with a
echo Firestore error (PermissionDenied, DefaultCredentialsError, or similar):
echo the Cloud Run service account needs read access to the "linkedin" Firestore
echo database. The ingest service already works, so the default compute service
echo account most likely has it -- but this is a new service and may run as a
echo different account. If, and only if, get_status reports a Firestore error:
echo   gcloud projects add-iam-policy-binding %PROJECT% --member "serviceAccount:<RUNTIME_SERVICE_ACCOUNT_EMAIL>" --role roles/datastore.user
exit /b 0

:usage
echo.
echo ERROR: a version tag is required. There is no default.
echo.
echo   Usage:    linkedinmcp\deploy.cmd ^<tag^>
echo   Example:  linkedinmcp\deploy.cmd v1.0.1
echo.
echo Use a new tag for every deploy. This script always rebuilds from the code
echo on disk before tagging, so reusing an old tag overwrites that build with
echo today's code rather than redeploying it. To roll back, point Cloud Run at
echo the older image directly:
echo   gcloud run deploy %SERVICE% --image %REGISTRY%:^<older-tag^> --region %REGION% --project %PROJECT%
exit /b 1
