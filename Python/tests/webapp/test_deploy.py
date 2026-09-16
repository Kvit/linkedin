"""`webapp/deploy.cmd` and `webapp/Dockerfile`, read as text: the flags
that keep the service private, and the lines that make the image start."""

from pathlib import Path

WEBAPP = Path(__file__).resolve().parents[2] / "webapp"


def _commands() -> str:
    """`deploy.cmd` without its `REM` comment lines, which name flags to avoid."""
    lines = (WEBAPP / "deploy.cmd").read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().upper().startswith("REM"))


def test_deploy_script_keeps_the_service_behind_iap_for_one_instance():
    script = _commands()
    for flag in ("--iap", "--no-allow-unauthenticated", "--min-instances=1", "--max-instances=1", "--update-env-vars"):
        assert flag in script, flag
    assert "roles/iap.httpsResourceAccessor" in script
    assert "--set-env-vars" not in script  # would wipe every other variable on the service
    assert "--allow-unauthenticated" not in script.replace("--no-allow-unauthenticated", "")


def test_deploy_script_never_sets_the_local_dev_user():
    assert "WEBAPP_DEV_USER" not in (WEBAPP / "deploy.cmd").read_text(encoding="utf-8")


def test_dockerfile_ships_and_starts_the_webapp():
    dockerfile = (WEBAPP / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY webapp/ webapp/" in dockerfile
    check = next(line for line in dockerfile.splitlines() if line.startswith("RUN uv run --no-sync python -c"))
    assert "webapp.app" in check and "linkedinmcp.app" in check
    assert '"--factory", "webapp.app:create_app"' in dockerfile
