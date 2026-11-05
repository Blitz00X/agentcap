"""Ops runner: real exit codes, unique evidence, distinct states, one model per
worktree and recovery after the runner dies.

No model is called: every "model" is a short Python command.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from agentcap.ops.locks import FileLock
from agentcap.ops.runner import (
    EXIT_BUSY,
    EXIT_REFUSED,
    Classifier,
    RunRefused,
    live_group_members,
    recover_stale_runs,
    run_task,
)
from agentcap.ops.store import OpsStore
from ops_support import PY, git, make_repo, ops_env, requires_git  # noqa: F401

pytestmark = pytest.mark.usefixtures("ops_env")


@pytest.fixture
def tracked(tmp_path):
    store = OpsStore(tmp_path / "state")
    wt = tmp_path / "wt"
    wt.mkdir()
    wt = wt.resolve()
    store.add_task("T1", "fake task", str(wt))
    return store, wt


def _payloads(store, event=None):
    payloads = [json.loads(note["payload_json"]) for note in store.list_notifications()]
    return [p for p in payloads if event is None or p["event"] == event]


def _evidence(store, run_id):
    return json.loads((Path(store.get_run(run_id)["run_dir"]) / "evidence.json").read_text())


def _wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_failed_model_records_its_actual_exit_code(tracked):
    store, wt = tracked
    outcome = run_task(
        store, task_id="T1", model="fake-model",
        argv=[PY, "-c", "import sys; print('working'); sys.exit(7)"],
    )
    assert (outcome.state, outcome.exit_code, outcome.cli_exit_code) == ("failure", 7, 7)
    run = store.get_run(outcome.run_id)
    assert (run["state"], run["exit_code"], run["reason"]) == ("failure", 7, "exit code 7")
    assert store.get_task("T1")["state"] == "failure"
    assert store.get_model(str(wt))["state"] == "failure"

    [payload] = _payloads(store, "run_finished")
    assert (payload["state"], payload["exit_code"], payload["run_id"]) == ("failure", 7, outcome.run_id)
    raw = (Path(run["run_dir"]) / "evidence.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == run["evidence_sha256"] == payload["evidence_sha256"]
    evidence = json.loads(raw)
    assert evidence["exit_code"] == 7
    assert evidence["stdout"]["sha256"] == hashlib.sha256(b"working\n").hexdigest()


def test_every_run_gets_unique_evidence(tracked):
    store, _ = tracked
    first = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "print('one')"])
    second = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "print('two')"], rerun=True)
    assert first.state == second.state == "completed"
    assert first.run_id != second.run_id
    assert first.run_dir != second.run_dir
    assert first.evidence_sha256 != second.evidence_sha256
    assert len({p["event_id"] for p in _payloads(store, "run_finished")}) == 2


def test_quota_exit_code_is_a_distinct_state(tracked):
    store, wt = tracked
    outcome = run_task(
        store, task_id="T1", model="fake-model",
        argv=[PY, "-c", "import sys; sys.exit(3)"],
        classifier=Classifier.build(quota_exit_codes=[3]),
    )
    assert (outcome.state, outcome.exit_code) == ("quota", 3)
    assert store.get_model(str(wt))["state"] == "quota"
    assert _payloads(store, "run_finished")[0]["state"] == "quota"


def test_agentcap_wrapped_cap_stop_defaults_to_quota(tracked, tmp_path):
    store, _ = tracked
    fake = tmp_path / "bin" / "agentcap"
    fake.parent.mkdir()
    fake.write_text(f"#!{PY}\nimport sys\nsys.exit(3)\n")
    fake.chmod(0o755)
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[str(fake), "run", "--", "codex"])
    assert (outcome.state, outcome.exit_code) == ("quota", 3)


PERMISSION_THEN_EXIT = """
import json, os, subprocess, sys
payload = {"session_id": "s1", "hook_event_name": "Notification",
           "notification_type": "permission_prompt",
           "message": "Claude needs your permission to use Bash", "cwd": os.getcwd()}
subprocess.run([sys.executable, "-m", "agentcap.ops", "hook", "claude-notification",
                "--worktree", os.getcwd()], input=json.dumps(payload), text=True, check=True)
sys.exit(int(sys.argv[1]))
"""


def test_permission_prompt_during_a_failed_run_is_the_permission_state(tracked):
    store, _ = tracked
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", PERMISSION_THEN_EXIT, "1"])
    assert (outcome.state, outcome.exit_code) == ("permission", 1)
    assert [p["event"] for p in _payloads(store)] == ["permission_needed", "run_finished"]
    [needed] = _payloads(store, "permission_needed")
    assert (needed["run_id"], needed["model"], needed["task_id"]) == (outcome.run_id, "fake-model", "T1")


def test_answered_permission_prompt_does_not_turn_success_into_failure(tracked):
    store, _ = tracked
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", PERMISSION_THEN_EXIT, "0"])
    assert (outcome.state, outcome.exit_code) == ("completed", 0)
    [finished] = _payloads(store, "run_finished")
    assert finished["signals"] == {"permission": 1, "quota": 0}


def test_missing_model_binary_is_a_failure_without_a_fake_exit_code(tracked, tmp_path):
    store, _ = tracked
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[str(tmp_path / "no-such-model")])
    assert outcome.state == "failure"
    assert outcome.exit_code is None
    assert outcome.cli_exit_code == 127
    assert "could not start" in store.get_run(outcome.run_id)["reason"]


def test_timeout_stops_the_model_and_records_the_signal(tracked):
    store, _ = tracked
    started = time.monotonic()
    outcome = run_task(
        store, task_id="T1", model="fake-model",
        argv=[PY, "-c", "import time; time.sleep(30)"], timeout_s=0.5, term_grace_s=5.0,
    )
    assert time.monotonic() - started < 20
    assert outcome.state == "failure"
    assert outcome.exit_signal == "SIGTERM"
    assert outcome.cli_exit_code == 128 + signal.SIGTERM
    assert "timed out" in store.get_run(outcome.run_id)["reason"]


def test_second_model_in_the_same_worktree_is_refused(tracked):
    store, wt = tracked
    holder = FileLock(store.worktree_lock_path(str(wt)), label="other runner").acquire()
    try:
        with pytest.raises(RunRefused) as refused:
            run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "pass"])
        assert refused.value.exit_code == EXIT_BUSY
    finally:
        holder.release()
    assert store.list_runs(task_id="T1") == []


def test_dispatching_a_running_task_again_is_a_refused_duplicate(tracked):
    store, wt = tracked
    store.add_task("T2", "another task, same worktree", str(wt))
    run_id = "e" * 32
    store.start_run(task_id="T1", model="fake-model", argv=["x"], run_id=run_id, run_dir=str(store.runs_dir / run_id))
    runner = FileLock(store.worktree_lock_path(str(wt)), label="ops run T1").acquire()  # T1's live runner
    try:
        with pytest.raises(RunRefused) as duplicate:
            run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "pass"])
        assert duplicate.value.exit_code == EXIT_REFUSED  # refused, not "busy, retry later"
        assert "already running" in str(duplicate.value)
        with pytest.raises(RunRefused) as busy:
            run_task(store, task_id="T2", model="fake-model", argv=[PY, "-c", "pass"])
        assert busy.value.exit_code == EXIT_BUSY
    finally:
        runner.release()
    assert [run["id"] for run in store.list_runs()] == [run_id]


def test_model_never_sees_the_notification_config(tracked, monkeypatch, tmp_path):
    store, _ = tracked
    monkeypatch.setenv("AGENTCAP_OPS_CONFIG", str(tmp_path / "secret-endpoint.json"))
    probe = (
        "import json, os; print(json.dumps({k: v for k, v in os.environ.items() "
        "if k.startswith('AGENTCAP_OPS_')}))"
    )
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", probe])
    seen = json.loads((Path(store.get_run(outcome.run_id)["run_dir"]) / "stdout.log").read_text())
    assert "AGENTCAP_OPS_CONFIG" not in seen
    assert seen["AGENTCAP_OPS_RUN_ID"] == outcome.run_id
    assert seen["AGENTCAP_OPS_DIR"] == str(store.state_dir)


def test_a_model_inside_a_tracked_run_cannot_start_another_job(tracked, monkeypatch):
    store, _ = tracked
    monkeypatch.setenv("AGENTCAP_OPS_RUN_ID", "f" * 32)
    with pytest.raises(RunRefused, match="second job"):
        run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "pass"])
    assert store.list_runs(task_id="T1") == []


@requires_git
def test_commit_evidence_comes_from_git_not_from_model_output(tmp_path):
    store = OpsStore(tmp_path / "state")
    repo = make_repo(tmp_path / "repo")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    store.add_task("T1", "claims", str(repo))

    liar = run_task(
        store, task_id="T1", model="fake-model",
        argv=[PY, "-c", "print('committed 0123456789abcdef0123456789abcdef01234567')"],
    )
    liar_run = store.get_run(liar.run_id)
    assert liar_run["head_before"] == liar_run["head_after"]
    assert _evidence(store, liar.run_id)["git"]["new_commits"] == []

    commit = (
        "import subprocess; subprocess.run(['git', '-c', 'user.name=t', '-c', "
        "'user.email=t@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-q', "
        "'--allow-empty', '-m', 'real'], check=True)"
    )
    real = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", commit], rerun=True)
    real_run = store.get_run(real.run_id)
    head = git(repo, "rev-parse", "HEAD")
    assert real_run["head_after"] == head != real_run["head_before"]
    assert real_run["new_commits"] == 1
    assert _evidence(store, real.run_id)["git"]["new_commits"] == [head]


def test_recovery_keeps_an_exit_code_recorded_before_the_crash(tracked):
    store, _ = tracked
    run_id = "d" * 32
    store.start_run(task_id="T1", model="fake-model", argv=["x"], run_id=run_id, run_dir=str(store.runs_dir / run_id))
    store.record_exit(run_id, 4, None)  # kernel reported the exit, then the runner died
    [recovered] = recover_stale_runs(store)
    run = store.get_run(run_id)
    assert (run["state"], run["exit_code"]) == ("interrupted", 4)
    assert "code 4" in run["reason"]
    assert (recovered["run_id"], recovered["child_alive"]) == (run_id, False)


def test_restart_recovers_a_run_whose_runner_was_killed(tracked):
    store, _ = tracked
    runner = subprocess.Popen(
        [PY, "-m", "agentcap.ops", "--state-dir", str(store.state_dir), "run", "--task", "T1",
         "--model", "fake-model", "--", PY, "-c", "import time; time.sleep(60)"],
    )
    pgid = None
    try:
        run = _wait_for(lambda: next((r for r in store.list_runs(task_id="T1") if r["child_pgid"]), None))
        pgid = run["child_pgid"]
        runner.kill()  # the runner crashes; nothing gets recorded
        runner.wait(timeout=10)
        assert store.get_run(run["id"])["state"] == "running"

        [recovered] = recover_stale_runs(store)
        assert (recovered["run_id"], recovered["child_alive"]) == (run["id"], True)
        after = store.get_run(run["id"])
        assert after["state"] == "interrupted"
        assert after["exit_code"] is None  # never invented
        assert store.get_task("T1")["state"] == "interrupted"

        # The orphaned model still owns the worktree: no second model may start.
        with pytest.raises(RunRefused) as refused:
            run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "pass"])
        assert refused.value.exit_code == EXIT_BUSY

        recover_stale_runs(store, kill_orphans=True)
        _wait_for(lambda: not live_group_members(store.get_run(run["id"])))
        assert recover_stale_runs(store) == []
        finals = [p for p in _payloads(store, "run_finished") if p["run_id"] == run["id"]]
        assert len(finals) == 1 and finals[0]["state"] == "interrupted"

        retry = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "pass"])
        assert retry.state == "completed"
        assert retry.task_state == "unverified"
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=10)
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
