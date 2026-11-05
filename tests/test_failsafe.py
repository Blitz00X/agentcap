"""Fail-closed guards: JobLock, SchemaWatch and the harness guard paths."""
from __future__ import annotations

import stat
import sys
import time

import pytest

from agentcap import failsafe, harness as h, quota
from tests.test_harness import FAKE_OC, fakebin, run  # noqa: E402,F401


# ------------------------------------------------------------------ JobLock
def test_joblock_refuses_second_holder(tmp_path):
    from agentcap.failsafe import GuardFailure, JobLock
    import os

    path = tmp_path / "quota.lock"
    first, second = JobLock(path), JobLock(path)
    first.acquire()
    try:
        with pytest.raises(GuardFailure) as excinfo:
            second.acquire()
        message = str(excinfo.value)
        assert "already running" in message
        assert str(os.getpid()) in message
    finally:
        first.release()
    second.acquire()
    second.release()


def test_joblock_file_and_dir_are_private(tmp_path):
    from agentcap.failsafe import JobLock

    path = tmp_path / "state" / "quota.lock"
    lock = JobLock(path)
    lock.acquire()
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        lock.release()


def test_joblock_allow_parallel_env(tmp_path, monkeypatch):
    from agentcap.failsafe import JobLock

    monkeypatch.setenv("AGENTCAP_ALLOW_PARALLEL", "1")
    a, b = JobLock(tmp_path / "quota.lock"), JobLock(tmp_path / "quota.lock")
    a.acquire()
    b.acquire()
    a.release()
    b.release()


# -------------------------------------------------------------- SchemaWatch
def test_schemawatch_trips_once_only_if_nothing_recognised():
    from agentcap.failsafe import SchemaWatch

    watch = SchemaWatch("x", limit=3)
    assert watch.feed("a", False) is None
    assert watch.feed("b", False) is None
    message = watch.feed("c", False)
    assert message is not None and "fail-closed" in message
    assert watch.feed("d", False) is None

    seen_one = SchemaWatch("x", limit=3)
    assert seen_one.feed("recognised", True) is None
    for _ in range(10):
        assert seen_one.feed("junk", False) is None


# ------------------------------------------------------- harness guard paths
def _install_opencode(bin_dir, body: str) -> None:
    exe = bin_dir / "opencode"
    exe.write_text(body.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)


UNKNOWN_SCHEMA_FOREVER = '''#!{py}
import json, time
for i in range(100):
    print(json.dumps({{"kind": "new_format", "i": i}}), flush=True)
    time.sleep(0.02)
time.sleep(30)
'''

UNKNOWN_SCHEMA_THIRTY = '''#!{py}
import json
for i in range(30):
    print(json.dumps({{"kind": "new_format", "i": i}}), flush=True)
'''


def test_harness_unknown_schema_stops_agent(fakebin, tmp_path):
    _install_opencode(fakebin, UNKNOWN_SCHEMA_FOREVER)
    started = time.monotonic()
    r, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, tokens=10**9,
                           int_timeout=0.5, term_timeout=0.5)
    assert time.monotonic() - started < 15
    assert code == failsafe.EXIT_GUARD_FAILED, err
    assert "known log schema" in err
    assert r.summary.reason == "guard_failed"


def test_harness_unknown_schema_free_only_warns(fakebin, tmp_path):
    _install_opencode(fakebin, UNKNOWN_SCHEMA_THIRTY)
    _, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, tokens=10**9, enforce=False)
    assert code == 0, err
    assert "Pro would stop it" in err


def test_harness_internal_error_fails_closed(fakebin, tmp_path, monkeypatch):
    def boom(self, obj):
        raise RuntimeError("boom")

    monkeypatch.setattr(h.OpenCodeAdapter, "parse", boom)
    monkeypatch.setenv("FAKE_N", "50")
    monkeypatch.setenv("FAKE_SLEEP", "0.1")
    _, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, tokens=10**9)
    assert code == failsafe.EXIT_GUARD_FAILED, err
    assert "internal error" in err
    assert "fail-closed" in err


def test_harness_quota_lock_refuses_second_job(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv(quota.STATE_ENV, str(tmp_path / "state"))
    held = failsafe.JobLock(quota.state_root() / "quota.lock")
    held.acquire()
    try:
        _, code, err, *_ = run(["agy", "-p", "hi"], tmp_path, quota=50.0)
    finally:
        held.release()
    assert code == 2, err
    assert "already running" in err


def test_codex_tailer_counts_unreadable_log(tmp_path):
    """T2: an unreadable Codex rollout is counted, so the runner can fail closed."""
    from agentcap.quota import CodexTailer
    t = CodexTailer(tmp_path, 0.0)
    t.own = tmp_path / "gone.jsonl"  # the job's rollout vanished / is unreadable
    for _ in range(3):
        t.poll("week")
    assert t.read_errors == 3
    t.own.write_text("")
    t.poll("week")
    assert t.read_errors == 0
