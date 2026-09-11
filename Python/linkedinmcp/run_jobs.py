"""Run one of the service's jobs from the command line, and the one function
that runs a job for the HTTP endpoint too.

    uv run python -m linkedinmcp.run_jobs {tick,sync,daily} [--dry-run]

from `Python/`, the directory `settings.load_environment()` resolves `.env`
against. `main` loads the environment, reads the settings, runs the job,
prints its summary as indented JSON and exits 0; on any exception it prints
only the exception's class name to stderr and exits 1 -- never its message,
which can carry project ids, URLs or a contact's data.

`tick` without `--dry-run` sends a real LinkedIn message when one is due.

`run` is shared with `POST /jobs/{job}` in `app.py`, so the command line and
the scheduler build the clients the same way:

- a Firestore client and a NEW LinkedIn client (`clients.unipile_client()`),
  closed in a `finally` whatever the job does;
- `state=RuntimeState(db, clock.utcnow)`, on the real clock. Left to
  themselves the jobs build a state whose clock is frozen at `now`, under
  which `lease_remaining` never decreases and the tick's 30-second lease
  floor would mean nothing;
- `sync` classifies with `jobs.default_classify` (Gemini), except under a dry
  run, which must never call Gemini.

Importing this module opens nothing: no settings read, no file loaded, no
client built. `clients`, `clock`, `jobs`, `settings` and `state` are reached
through their modules at call time, so a test can replace any of them.
"""

import argparse
import json
import sys

from linkedinmcp import clients, clock, jobs, settings as cfg, state

#: The jobs `run` knows, in the order the command line lists them.
JOBS = ("tick", "sync", "daily")


def run(job: str, settings, *, dry_run: bool = False) -> dict:
    """Build the clients, run `job` and return its summary.

    Raises `ValueError` for a job outside `JOBS` before any client is built.
    Whatever the job raises propagates, after the LinkedIn client is closed.
    """
    if job not in JOBS:
        raise ValueError(f"unknown job: {job!r}")
    db = clients.firestore_client()
    runtime = state.RuntimeState(db, clock.utcnow)
    client = clients.unipile_client()
    try:
        now = clock.utcnow()
        if job == "tick":
            return jobs.tick(db, client, settings, now, dry_run=dry_run, state=runtime)
        if job == "daily":
            return jobs.daily(db, client, settings, now, dry_run=dry_run, state=runtime)
        classify = None if dry_run else jobs.default_classify
        return jobs.sync(db, client, settings, now, dry_run=dry_run, state=runtime, classify=classify)
    finally:
        client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m linkedinmcp.run_jobs",
        description="Run one linkedin-outreach job and print its summary as JSON.",
    )
    parser.add_argument("job", choices=JOBS, help="the job to run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="write nothing, send nothing and never call Gemini; report what the job would do",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg.load_environment()
        settings = cfg.get_settings()
        summary = run(args.job, settings, dry_run=args.dry_run)
        text = json.dumps(summary, indent=2, default=str)
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
