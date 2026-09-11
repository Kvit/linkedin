"""`linkedinmcp.run_jobs`: the command line (`uv run python -m
linkedinmcp.run_jobs tick|sync|daily [--dry-run]`) and `run`, the one function
that builds the clients and runs a job -- for the command line and for the
`POST /jobs/{job}` endpoint alike.

No test here reads `Python/.env` or reaches a real client. Every test chdirs
to an empty `tmp_path`, and `settings.load_environment`,
`settings.get_settings`, `clients.firestore_client` and
`clients.unipile_client` are replaced. The jobs themselves (`jobs.tick`,
`jobs.sync`, `jobs.daily`) are replaced by recorders: this file tests what
`run_jobs` hands a job and what it does with the answer, not what a job does.
"""

import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from linkedinmcp import clients, clock, jobs, run_jobs, settings as cfg, state
from linkedinmcp.settings import ConfigError, OutreachSettings
from tests.linkedinmcp.fake_firestore import FakeFirestore

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class FakeLinkedIn:
    """Stands in for `lib.unipile.UnipileClient` as far as `run_jobs` touches
    it: `close()`, counted."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class JobRecorder:
    """A replacement for `jobs.tick` / `jobs.sync` / `jobs.daily`: records
    every call's arguments, then returns `summary` or raises `error`."""

    def __init__(self, summary: dict) -> None:
        self.summary = summary
        self.error: Exception | None = None
        self.calls: list[dict] = []

    def __call__(self, db, client, settings, now, **kwargs):
        self.calls.append({"db": db, "client": client, "settings": settings, "now": now, **kwargs})
        if self.error is not None:
            raise self.error
        return self.summary


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Everything `run_jobs` reaches, replaced; `calls` records the order in
    which the environment and the settings were read."""
    monkeypatch.chdir(tmp_path)
    settings = OutreachSettings(api_key="k" * 16, _env_file=None)
    calls: list[str] = []

    def get_settings():
        calls.append("get_settings")
        return settings

    monkeypatch.setattr(cfg, "load_environment", lambda: calls.append("load_environment"))
    monkeypatch.setattr(cfg, "get_settings", get_settings)
    db = FakeFirestore()
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    linkedin = FakeLinkedIn()
    monkeypatch.setattr(clients, "unipile_client", lambda: linkedin)
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    recorders = {name: JobRecorder({"job": name}) for name in ("tick", "sync", "daily")}
    for name, recorder in recorders.items():
        monkeypatch.setattr(jobs, name, recorder)
    return SimpleNamespace(settings=settings, calls=calls, db=db, linkedin=linkedin, jobs=recorders)


# --- parse_args ------------------------------------------------------------------


def test_parse_args_reads_the_job_and_the_dry_run_flag():
    args = run_jobs.parse_args(["tick", "--dry-run"])

    assert args.job == "tick"
    assert args.dry_run is True
    assert run_jobs.parse_args(["daily"]).dry_run is False


@pytest.mark.parametrize("argv", [["send"], []])
def test_parse_args_refuses_an_unknown_or_missing_job(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_jobs.parse_args(argv)

    assert exit_info.value.code == 2


# --- main ---------------------------------------------------------------------


def test_main_runs_a_dry_run_tick_and_prints_the_summary_as_indented_json(cli, capsys):
    exit_code = run_jobs.main(["tick", "--dry-run"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert [call["dry_run"] for call in cli.jobs["tick"].calls] == [True]
    assert cli.jobs["sync"].calls == [] and cli.jobs["daily"].calls == []
    assert json.loads(out) == {"job": "tick"}
    assert out == json.dumps({"job": "tick"}, indent=2) + "\n"


def test_main_loads_the_environment_before_it_reads_the_settings(cli, capsys):
    run_jobs.main(["daily"])

    assert cli.calls == ["load_environment", "get_settings"]
    assert cli.jobs["daily"].calls[0]["settings"] is cli.settings


def test_main_prints_only_the_exception_class_name_and_exits_1(cli, capsys):
    cli.jobs["tick"].error = RuntimeError("detail that must not be printed")

    exit_code = run_jobs.main(["tick"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == "RuntimeError\n"
    assert captured.out == ""
    assert "must not be printed" not in captured.out + captured.err


def test_main_reports_a_settings_failure_by_class_name(cli, monkeypatch, capsys):
    def broken_settings():
        raise ConfigError(type="config/invalid_settings", title="Invalid outreach settings: api_key")

    monkeypatch.setattr(cfg, "get_settings", broken_settings)

    exit_code = run_jobs.main(["sync"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == "ConfigError\n"
    assert cli.jobs["sync"].calls == []


def test_main_prints_a_summary_holding_a_datetime(cli, capsys):
    """A summary value JSON has no type for is printed as its string form
    rather than failing the run after the job's work was done."""
    cli.jobs["tick"].summary = {"until": NOW}

    exit_code = run_jobs.main(["tick"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"until": str(NOW)}


# --- run ----------------------------------------------------------------------


def test_run_hands_the_job_the_clients_settings_and_now(cli):
    summary = run_jobs.run("daily", cli.settings)

    call = cli.jobs["daily"].calls[0]
    assert summary == {"job": "daily"}
    assert call["db"] is cli.db
    assert call["client"] is cli.linkedin
    assert call["settings"] is cli.settings
    assert call["now"] == NOW
    assert call["dry_run"] is False
    assert isinstance(call["state"], state.RuntimeState)


def test_sync_gets_the_default_classifier_unless_it_is_a_dry_run(cli):
    run_jobs.run("sync", cli.settings)
    run_jobs.run("sync", cli.settings, dry_run=True)

    first, second = cli.jobs["sync"].calls
    assert first["classify"] is jobs.default_classify
    assert (second["classify"], second["dry_run"]) == (None, True)


@pytest.mark.parametrize("job", ["tick", "daily"])
def test_tick_and_daily_get_no_classifier(cli, job):
    run_jobs.run(job, cli.settings)

    assert "classify" not in cli.jobs[job].calls[0]


def test_the_linkedin_client_is_closed_after_the_job(cli):
    run_jobs.run("tick", cli.settings)

    assert cli.linkedin.closed == 1


def test_the_linkedin_client_is_closed_even_when_the_job_raises(cli):
    cli.jobs["tick"].error = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_jobs.run("tick", cli.settings)

    assert cli.linkedin.closed == 1


def test_the_state_handed_to_the_job_reads_the_clock_on_every_call(cli, monkeypatch):
    """The `RuntimeState` a job receives runs on the clock, not on a copy of
    `now`: a 240 s lease taken inside the job has 140 s left once the clock
    moves on by 100 s. (Building the state as `RuntimeState(db, clock=lambda:
    now)` instead was tried while writing this test: it reported 240.)"""
    moments = [NOW]
    monkeypatch.setattr(clock, "utcnow", lambda: moments[0])
    seen = {}

    def tick(db, client, settings, now, *, dry_run, state):
        owner = state.acquire_tick_lease(240)
        moments[0] = NOW + timedelta(seconds=100)
        seen["remaining"] = state.lease_remaining(owner)
        return {}

    monkeypatch.setattr(jobs, "tick", tick)

    run_jobs.run("tick", cli.settings)

    assert seen["remaining"] == 140.0


def test_run_refuses_an_unknown_job_before_building_any_client(cli, monkeypatch):
    def tripwire():
        raise AssertionError("a client was built for an unknown job")

    monkeypatch.setattr(clients, "firestore_client", tripwire)
    monkeypatch.setattr(clients, "unipile_client", tripwire)

    with pytest.raises(ValueError):
        run_jobs.run("send", cli.settings)


# --- import ---------------------------------------------------------------------


def test_importing_the_module_runs_nothing(monkeypatch):
    """Re-executing the module with every environment reader and client
    factory rigged to fail completes: import reads no settings, loads no file,
    builds no client, and does not call `main`."""

    def tripwire(*args, **kwargs):
        raise AssertionError("called while importing linkedinmcp.run_jobs")

    for module, name in (
        (cfg, "load_environment"),
        (cfg, "get_settings"),
        (clients, "firestore_client"),
        (clients, "unipile_client"),
    ):
        monkeypatch.setattr(module, name, tripwire)

    importlib.reload(run_jobs)
