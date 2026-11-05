"""Ops store: persistent tasks/runs/outbox, dedup, single writer, explicit model state."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import time

import pytest

from agentcap.ops.locks import FileLock, LockBusy
from agentcap.ops.store import Conflict, OpsError, OpsStore, StoreBusy
from ops_support import PY, ops_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("ops_env")


def _store(tmp_path, **kwargs):
    return OpsStore(tmp_path / "state", **kwargs)


def _start(store, worktree, *, task="T1", model="model-a", run_id="a" * 32, rerun=False):
    if store.get_task(task) is None:
        store.add_task(task, f"title {task}", worktree)
    return store.start_run(
        task_id=task, model=model, argv=["fake-model"], run_id=run_id,
        run_dir=str(store.runs_dir / run_id), rerun=rerun,
    )


def test_identical_task_dispatch_is_deduplicated(tmp_path):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    first, created = store.add_task("T1", "harden locks", wt)
    again, created_again = store.add_task("T1", "harden locks", wt)
    assert (created, created_again) == (True, False)
    assert again["id"] == first["id"] == "T1"
    assert [task["id"] for task in store.list_tasks()] == ["T1"]


def test_conflicting_task_redefinition_is_refused(tmp_path):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    store.add_task("T1", "harden locks", wt)
    with pytest.raises(Conflict):
        store.add_task("T1", "something else", wt)
    assert store.get_task("T1")["title"] == "harden locks"


def test_duplicate_event_is_queued_once_and_counted(tmp_path):
    store = _store(tmp_path)
    payload = {"event": "probe", "message": "hello"}
    first, created = store.enqueue(dedup_key="probe:1", event="probe", payload=payload)
    second, created_again = store.enqueue(dedup_key="probe:1", event="probe", payload=payload)
    assert (created, created_again) == (True, False)
    assert second["id"] == first["id"]
    [row] = store.list_notifications()
    assert (row["state"], row["duplicates"]) == ("pending", 1)


def test_store_writes_wait_for_the_single_writer_lock(tmp_path):
    store = _store(tmp_path, lock_timeout=0.3)
    other = FileLock(store.writer_lock_path, label="other writer").acquire()
    try:
        started = time.monotonic()
        with pytest.raises(StoreBusy) as busy:
            store.add_task("T1", "title", str(tmp_path / "wt"))
        assert time.monotonic() - started >= 0.25
        assert str(os.getpid()) in str(busy.value)
        assert store.get_task("T1") is None
    finally:
        other.release()
    store.add_task("T1", "title", str(tmp_path / "wt"))
    assert store.get_task("T1") is not None


LOCK_HOLDER = """
import sys, time
from agentcap.ops.locks import FileLock
lock = FileLock(sys.argv[1], label="doomed").acquire()
print("locked", flush=True)
time.sleep(60)
"""


def test_lock_of_a_killed_holder_is_released_by_the_kernel(tmp_path):
    path = tmp_path / "state" / "ops.lock"
    holder = subprocess.Popen([PY, "-c", LOCK_HOLDER, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy) as busy:
            FileLock(path).acquire(timeout=0.0)
        assert busy.value.holder.get("pid") == holder.pid
        holder.kill()  # crash: no release, no cleanup
        holder.wait(timeout=10)
        FileLock(path).acquire(timeout=5.0).release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        holder.stdout.close()


def test_active_model_changes_only_explicitly(tmp_path):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    store.set_model(wt, "model-a", note="coordinator picked A")
    store.add_task("T1", "title", wt)
    with pytest.raises(Conflict) as mismatch:
        store.start_run(task_id="T1", model="model-b", argv=["x"], run_id="a" * 32, run_dir=str(tmp_path / "a"))
    assert "model-a" in str(mismatch.value)
    assert store.get_task("T1")["state"] == "queued"

    store.set_model(wt, "model-b", note="switch after review")
    run = store.start_run(task_id="T1", model="model-b", argv=["x"], run_id="b" * 32, run_dir=str(tmp_path / "b"))
    assert run["model"] == "model-b"
    active = store.get_model(wt)
    assert (active["model"], active["state"], active["run_id"]) == ("model-b", "running", "b" * 32)
    history = [(entry["model"], entry["state"]) for entry in store.model_history(wt)]
    assert history == [("model-a", "ready"), ("model-b", "ready"), ("model-b", "running")]


def test_active_model_cannot_be_switched_mid_run(tmp_path):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    _start(store, wt)
    with pytest.raises(Conflict):
        store.set_model(wt, "model-b")
    assert store.get_model(wt)["model"] == "model-a"


@pytest.mark.parametrize(
    "state, model_state, task_state",
    [
        ("completed", "ready", "unverified"),
        ("failure", "failure", "failure"),
        ("quota", "quota", "quota"),
        ("permission", "permission", "permission"),
    ],
)
def test_run_outcome_sets_distinct_task_and_model_states(tmp_path, state, model_state, task_state):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    run = _start(store, wt)
    exit_code = 0 if state == "completed" else 9
    final, changed = store.finish_run(run["id"], state=state, reason=f"test {state}", exit_code=exit_code)
    assert changed is True
    assert (final["state"], final["exit_code"]) == (state, exit_code)
    assert store.get_task("T1")["state"] == task_state
    assert store.get_model(wt)["state"] == model_state
    [note] = store.list_notifications()
    payload = json.loads(note["payload_json"])
    assert (payload["event"], payload["state"], payload["exit_code"]) == ("run_finished", state, exit_code)
    assert payload["model"] == "model-a"


def test_a_run_is_finalised_once_and_notified_once(tmp_path):
    store = _store(tmp_path)
    run = _start(store, str(tmp_path / "wt"))
    store.finish_run(run["id"], state="failure", reason="exit code 7", exit_code=7)
    again, changed = store.finish_run(run["id"], state="completed", reason="late", exit_code=0)
    assert changed is False
    assert (again["state"], again["exit_code"]) == ("failure", 7)
    _, recovered = store.interrupt_run(run["id"], reason="late recovery")
    assert recovered is False
    assert len(store.list_notifications()) == 1


def test_task_cannot_run_twice_at_once(tmp_path):
    store = _store(tmp_path)
    _start(store, str(tmp_path / "wt"), run_id="a" * 32)
    with pytest.raises(Conflict):
        store.start_run(task_id="T1", model="model-a", argv=["x"], run_id="b" * 32, run_dir=str(tmp_path / "b"))
    assert [run["id"] for run in store.list_runs(task_id="T1")] == ["a" * 32]


def test_one_running_run_per_worktree(tmp_path):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    _start(store, wt, task="T1", run_id="a" * 32)
    store.add_task("T2", "other task, same worktree", wt)
    with pytest.raises(Conflict):
        store.start_run(task_id="T2", model="model-a", argv=["x"], run_id="b" * 32, run_dir=str(tmp_path / "b"))
    assert store.get_task("T2")["state"] == "queued"


def test_succeeded_task_is_not_dispatched_again_without_rerun(tmp_path):
    store = _store(tmp_path)
    wt = str(tmp_path / "wt")
    store.add_task("T1", "title T1", wt, requires_commit=False)
    run = _start(store, wt, run_id="a" * 32)
    store.finish_run(run["id"], state="completed", reason="exit code 0", exit_code=0)
    with pytest.raises(Conflict, match="verification"):
        store.start_run(
            task_id="T1", model="model-a", argv=["x"], run_id="b" * 32, run_dir=str(tmp_path / "b"),
        )
    verification, task = store.record_verification(
        task_id="T1", run_id=run["id"], passed=True, reason="1 passed",
        verified_by="coordinator", test_argv=["pytest"], test_exit_code=0,
        counts={"passed": 1}, evidence_dir=str(tmp_path / "verification"), evidence_sha256="f" * 64,
    )
    assert (verification["state"], task["state"]) == ("passed", "success")
    with pytest.raises(Conflict):
        store.start_run(
            task_id="T1", model="model-a", argv=["x"], run_id="b" * 32, run_dir=str(tmp_path / "b"),
        )
    rerun = store.start_run(
        task_id="T1", model="model-a", argv=["x"], run_id="c" * 32, run_dir=str(tmp_path / "c"),
        rerun=True,
    )
    assert rerun["state"] == "running"
    assert store.get_task("T1")["run_count"] == 2


def test_queue_survives_a_restart(tmp_path):
    store = _store(tmp_path)
    store.enqueue(dedup_key="k1", event="probe", payload={"message": "m"})
    run = _start(store, str(tmp_path / "wt"))
    del store
    reopened = _store(tmp_path)
    [row] = reopened.list_notifications(state="pending")
    assert row["dedup_key"] == "k1"
    assert reopened.get_run(run["id"])["state"] == "running"


def test_store_refuses_a_newer_schema(tmp_path):
    store = _store(tmp_path)
    conn = sqlite3.connect(store.db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(OpsError, match="schema"):
        OpsStore(store.state_dir)


def test_state_is_private(tmp_path):
    store = _store(tmp_path)
    assert stat.S_IMODE(os.stat(store.state_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.db_path).st_mode) == 0o600
