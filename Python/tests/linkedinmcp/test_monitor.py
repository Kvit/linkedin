"""Tests for `linkedinmcp.monitor`: a process step started as a job runs
once, reports its result, frees its step's lock however it ends, and a job
that goes quiet never blocks its step.

The step functions here are stand-ins registered on `steps.STEPS`; the real
steps have their own tests (`test_steps.py`). The executor is `inline`, so
`launch` runs the job before it returns.
"""

from datetime import UTC, datetime, timedelta

import pytest

from linkedinmcp import clients, clock, decisions, jobs, monitor, steps
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import make_settings

NOW = datetime(2026, 9, 11, 14, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    db = FakeFirestore(clock=lambda: NOW)
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    return db


@pytest.fixture
def step(monkeypatch):
    """A stand-in step `demo`: it reports progress once and returns a count
    and a row -- or raises what `step.error` holds."""

    class Demo:
        error = None
        calls: list = []

        def __call__(self, job):
            self.calls.append(job.params)
            job.report(done=1, total=1, note="one")
            if self.error is not None:
                raise self.error
            return {"count": 2, "rows": [{"doc_id": "pat"}]}

    demo = Demo()
    monkeypatch.setitem(steps.STEPS, "demo", demo)
    return demo


def lock_of(db, step_name):
    return (db.collection("runtime_state").document("locks").get().to_dict() or {}).get(step_name)


def test_a_launched_job_runs_once_and_reports_its_result(db, step, tmp_path):
    settings = make_settings(tmp_path)

    started = monitor.launch(db, "demo", {"days": 14}, settings, NOW)

    assert started == {"ok": True, "job_id": "demo:20260911T140000000000Z", "status": "succeeded"}
    job = monitor.get(db, started["job_id"])
    assert job["result"] == {"count": 2, "rows": [{"doc_id": "pat"}]}
    assert job["summary"] == {"count": 2}
    assert job["progress"] == {"done": 1, "total": 1, "note": "one"}
    assert job["params"] == {"days": 14}
    assert step.calls == [{"days": 14}]
    assert lock_of(db, "demo") is None
    # Delivered again, the job is not run again.
    assert monitor.run(started["job_id"], settings) == {
        "job_id": started["job_id"], "ran": False, "status": "succeeded",
    }
    assert len(step.calls) == 1


def test_a_live_step_refuses_a_second_start_until_its_job_goes_quiet(db):
    first = monitor.start(db, "demo", {}, NOW)
    assert first["ok"] is True

    assert monitor.start(db, "demo", {}, NOW + timedelta(minutes=5)) == {
        "ok": False, "reason": "already_running", "job_id": first["job_id"], "status": "queued",
    }

    later = NOW + monitor.LOST_AFTER + timedelta(seconds=1)
    second = monitor.start(db, "demo", {}, later)
    assert second["ok"] is True
    lost = monitor.get(db, first["job_id"])
    assert (lost["status"], lost["error"]) == ("failed", "lost")
    assert lock_of(db, "demo")["job_id"] == second["job_id"]


def test_a_running_job_hands_its_step_to_its_successor_and_its_own_finish_leaves_that_lock(db):
    """`send_messages` chains jobs: near its time limit a running job starts
    the next job of its own step. That successor takes the lock the running
    job holds, the running job's finish leaves it alone, and anyone else is
    still refused."""
    first = monitor.start(db, "demo", {}, NOW)
    monitor.claim(db, first["job_id"], NOW)
    later = NOW + timedelta(minutes=27)

    second = monitor.start(db, "demo", {"limit": 3}, later, created_by=first["job_id"], successor_of=first["job_id"])

    assert second["ok"] is True
    assert monitor.get(db, second["job_id"])["created_by"] == first["job_id"]
    assert monitor.finish(db, first["job_id"], later, result={"sent": 28}) is True
    assert lock_of(db, "demo")["job_id"] == second["job_id"]
    assert monitor.start(db, "demo", {}, later)["reason"] == "already_running"


def test_a_job_taken_over_as_lost_stops_at_its_next_report_and_keeps_its_lost_record(db, tmp_path, monkeypatch):
    """Silent past `LOST_AFTER`, the job's step is taken by a newer start.
    Its next report stops it, and its late end writes nothing over the
    record: still failed/lost, the lock the newer job's."""
    newer = {}

    def silent_then_reports(job):
        newer.update(monitor.start(db, "demo", {}, NOW + monitor.LOST_AFTER + timedelta(minutes=1)))
        job.report(done=1, total=2, note="still here")
        return {"count": 1}

    monkeypatch.setitem(steps.STEPS, "demo", silent_then_reports)

    started = monitor.launch(db, "demo", {}, make_settings(tmp_path), NOW)

    old = monitor.get(db, started["job_id"])
    assert (old["status"], old["error"], old["progress"]) == ("failed", "lost", None)
    assert lock_of(db, "demo")["job_id"] == newer["job_id"]


def test_a_step_that_raises_is_recorded_failed_raises_the_alert_and_frees_its_lock(db, step, tmp_path):
    step.error = ValueError("boom")
    settings = make_settings(tmp_path)

    started = monitor.launch(db, "demo", {}, settings, NOW)

    job = monitor.get(db, started["job_id"])
    assert (job["status"], job["ok"], job["error"]) == ("failed", False, "ValueError")
    assert [item["id"] for item in decisions.list_decisions(db)] == ["alert:job_failed:demo:20260911"]
    assert lock_of(db, "demo") is None


def test_get_reads_a_scheduled_run_as_a_finished_job(db):
    run_id = jobs.record_run(db, "daily", NOW, NOW, ok=True, summary={"enqueued": 3})

    job = monitor.wait(db, run_id, 45)

    assert (job["status"], job["result"], job["lost"]) == ("succeeded", {"enqueued": 3}, False)
    assert monitor.get(db, "daily:20990101T000000000000Z") is None


def test_the_daily_job_is_refused_the_intro_lock_while_a_send_intro_job_is_live(db):
    live = monitor.start(db, jobs.INTRO_STEP, {}, NOW)

    assert monitor.acquire_lock(db, jobs.INTRO_STEP, "daily:x", NOW) == {"job_id": live["job_id"], "status": "queued"}


def test_a_task_name_keeps_only_what_a_cloud_task_id_may_hold():
    assert monitor.task_name("send_intro:20260911T140000000000Z") == "send_intro-20260911T140000000000Z"
