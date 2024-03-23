REM deploy specific version of pathlight app to google cloud run lh-pathlight prod
echo off
SET tag=%1
if not defined tag SET tag=latest
echo - Deploying version %tag% to google cloud run (project vk-linkedin)
docker tag linkedin:latest us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin:%tag%
docker push us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin:%tag%
echo full tag: us-central1-docker.pkg.dev/vk-linkedin/linkedin/linkedin:%tag%