"""Independent verification and meaningful commit notifications.

A run only records what the model process did.  The coordinator's own test
command and git decide whether the task succeeded; every meaningful commit is
queued once, by hash.  No model and no network are involved.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentcap.ops.runner import RunRefused, run_task
from agentcap.ops.store import OpsStore
from agentcap.ops.verify import parse_test_counts, verify_task
from ops_support import PY, git, make_repo, ops_env, requires_git  # noqa: F401

pytestmark = [pytest.mark.usefixtures("ops_env"), requires_git]


def _script(tmp_path, body):
    path = tmp_path / "bin" / "model-step"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!" + PY + "\n" + body)
    path.chmod(0o755)
    return path


def _repo(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "work.txt").write_text("base\n")
    git(repo, "add", "work.txt")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def _events(store, kind):
    return [json.loads(note["payload_json"]) for note in store.list_notifications() if note["event"] == kind]


def test_parse_test_counts_reads_the_last_pytest_summary():
    text = "noise\n1 failed, 2 passed, 1 skipped in 0.10s\n3 passed, 1 warning in 0.20s\n"
    assert parse_test_counts(text) == {"passed": 3, "failed": 0, "skipped": 0, "errors": 0}
    assert parse_test_counts("no summary here") is None


def test_completed_run_needs_verification_before_the_task_succeeds(tmp_path):
    store = OpsStore(tmp_path / "state")
    repo = _repo(tmp_path)
    store.add_task("T1", "no commit required", str(repo), requires_commit=False)

    outcome = run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "print('work')"])
    assert (outcome.state, outcome.task_state) == ("completed", "unverified")

    verification = verify_task(
        store, task_id="T1", argv=[PY, "-c", "print('1 passed in 0.01s')"],
    )
    assert (verification.state, verification.task_state) == ("passed", "success")
    assert verification.tests["counts"] == {"passed": 1, "failed": 0, "skipped": 0, "errors": 0}

    row = store.get_verification(verification.verification_id)
    evidence = Path(verification.evidence_dir) / "verification.json"
    assert evidence.is_file()
    assert row["evidence_sha256"] == verification.evidence_sha256
    assert json.loads(evidence.read_text())["result"] == "passed"
    assert row["run_id"] == outcome.run_id
    assert store.get_task("T1")["state"] == "success"

    [finished] = _events(store, "run_finished")
    [verified] = _events(store, "task_verified")
    assert finished["state"] == "completed"
    assert (verified["state"], verified["verified"]) == ("success", True)
    assert verified["tests"]["passed"] == 1
    assert "verification.json" in " ".join(verified["evidence_paths"])


def test_failed_independent_tests_keep_the_task_failed(tmp_path):
    store = OpsStore(tmp_path / "state")
    repo = _repo(tmp_path)
    store.add_task("T1", "no commit required", str(repo), requires_commit=False)
    run_task(store, task_id="T1", model="fake-model", argv=[PY, "-c", "print('work')"])

    verification = verify_task(
        store, task_id="T1",
        argv=[PY, "-c", "print('1 failed in 0.01s'); raise SystemExit(1)"],
    )
    assert (verification.state, verification.task_state) == ("failed", "failure")
    [verified] = _events(store, "task_verified")
    assert (verified["state"], verified["verified"]) == ("failure", False)
    assert "tests ended with exit code 1" in verified["reason"]

    with pytest.raises(RunRefused, match="failed verification"):
        verify_task(store, task_id="T1", argv=[PY, "-c", "print('1 passed in 0.01s')"])


def test_meaningful_commit_is_recorded_and_notified_once(tmp_path):
    store = OpsStore(tmp_path / "state")
    repo = _repo(tmp_path)
    store.add_task("T1", "commit required", str(repo))
    commit = _script(
        tmp_path,
        """
from pathlib import Path
import subprocess
Path("work.txt").write_text("changed\\n")
subprocess.run(["git", "-c", "user.name=ops-test", "-c", "user.email=ops-test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-q", "-am", "work"], check=True)
""",
    )
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[commit])
    assert (outcome.state, outcome.task_state) == ("completed", "unverified")
    assert len(outcome.commits) == 1
    sha = outcome.commits[0]
    [commit_note] = _events(store, "commit")
    assert commit_note["commit_sha"] == sha
    assert commit_note["source"] == "agentcap-ops"
    assert "COMMIT" in commit_note["message"]
    assert (commit_note["task"], commit_note["run"]) == ("T1", outcome.run_id)
    assert store.get_commit(sha)["source"] == "run"

    verification = verify_task(
        store, task_id="T1", argv=[PY, "-c", "print('2 passed in 0.01s')"],
    )
    assert (verification.state, verification.task_state) == ("passed", "success")
    assert verification.commits == (sha,)

    # The same commit is not queued twice: verification only bumps duplicates.
    notes = [note for note in store.list_notifications() if note["event"] == "commit"]
    assert len(notes) == 1 and notes[0]["duplicates"] == 1
    stored = store.get_commit(sha)
    assert stored["verification_id"] == verification.verification_id
    assert stored["verified_at"] is not None
    [verified] = _events(store, "task_verified")
    assert verified["commits"] == [sha]


def test_empty_commit_is_not_a_meaningful_notification(tmp_path):
    store = OpsStore(tmp_path / "state")
    repo = _repo(tmp_path)
    store.add_task("T1", "commit required", str(repo))
    commit = _script(
        tmp_path,
        """
import subprocess
subprocess.run(["git", "-c", "user.name=ops-test", "-c", "user.email=ops-test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "empty"], check=True)
""",
    )
    outcome = run_task(store, task_id="T1", model="fake-model", argv=[commit])
    assert outcome.state == "completed"
    assert outcome.commits == ()
    assert _events(store, "commit") == []


def test_verification_is_refused_inside_a_tracked_model_run(tmp_path, monkeypatch):
    store = OpsStore(tmp_path / "state")
    repo = _repo(tmp_path)
    store.add_task("T1", "no commit required", str(repo), requires_commit=False)
    monkeypatch.setenv("AGENTCAP_OPS_RUN_ID", "f" * 32)
    with pytest.raises(RunRefused, match="model may not verify"):
        verify_task(store, task_id="T1", argv=[PY, "-c", "pass"])


def test_a_task_cannot_be_forced_to_success_without_a_verification(tmp_path):
    store = OpsStore(tmp_path / "state")
    repo = _repo(tmp_path)
    store.add_task("T1", "queued", str(repo))
    conn = sqlite3.connect(store.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET state = 'success' WHERE id = 'T1'")
    finally:
        conn.close()
    assert store.get_task("T1")["state"] == "queued"
