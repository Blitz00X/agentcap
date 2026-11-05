"""Notification delivery: failures are kept and retried, duplicates are sent once,
a crashed deliverer loses nothing, and the sender's secret never reaches the store.

Every sender here is a local program in ``tmp_path``; nothing talks to a model,
the internet or the coordinator's real notification settings.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from agentcap.ops.locks import FileLock
from agentcap.ops.notify import (
    ConfigError,
    build_sender_call,
    deliver,
    load_config_fd,
    load_config_file,
    parse_config,
)
from agentcap.ops.store import OpsStore
from ops_support import (  # noqa: F401
    PY,
    FakeClock,
    make_repo,
    ops_env,
    requires_git,
    write_config,
)

pytestmark = pytest.mark.usefixtures("ops_env")

SECRET = "123456:SECRET-TOKEN-abcdef"

SENDER = """
import json, os, sys
record = {"argv": sys.argv[1:], "stdin": sys.stdin.buffer.read().decode("utf-8"),
          "env": {k: v for k, v in os.environ.items() if k.startswith("AGENTCAP_OPS_")}}
with open(os.environ["OPS_TEST_SENDER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
print("sender stdout", os.environ.get("OPS_TEST_SENDER_ECHO", ""))
print("sender stderr", os.environ.get("OPS_TEST_SENDER_ECHO", ""), file=sys.stderr)
sys.exit(int(os.environ.get("OPS_TEST_SENDER_EXIT", "0")))
"""


@pytest.fixture
def queued(tmp_path):
    clock = FakeClock()
    store = OpsStore(tmp_path / "state", clock=clock)
    note, _ = store.enqueue(
        dedup_key="run:abc:final",
        event="run_finished",
        payload={"event": "run_finished", "state": "failure", "message": "agentcap-ops: T1 FAILURE"},
    )
    return store, clock, note


def _executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!" + PY + "\n" + body)
    path.chmod(0o755)
    return path


def _command_config(command, **extra):
    return parse_config(json.dumps({"command": [str(part) for part in command], **extra}).encode())


@pytest.fixture
def sender(tmp_path, monkeypatch):
    """Stand-in for the coordinator's existing deterministic sender (local, no network)."""

    log = tmp_path / "sender.jsonl"
    monkeypatch.setenv("OPS_TEST_SENDER_LOG", str(log))
    monkeypatch.setenv("OPS_TEST_SENDER_EXIT", "0")
    script = _executable(tmp_path / "bin" / "existing-sender", SENDER)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return script, calls


def test_existing_sender_failure_keeps_the_notification_for_retry(queued, sender, monkeypatch):
    store, clock, note = queued
    script, calls = sender
    monkeypatch.setenv("OPS_TEST_SENDER_EXIT", "3")
    config = _command_config([script])
    report = deliver(store, config)
    assert (report.delivered, report.retried, report.exit_code) == (0, 1, 1)
    row = store.get_notification(note["id"])
    assert (row["state"], row["attempts"], row["last_error"]) == ("pending", 1, "exit 3")
    assert row["next_attempt_at"] == clock() + 30
    assert row["payload_json"] == note["payload_json"]
    [attempt] = store.delivery_attempts(note["id"])
    assert (attempt["outcome"], attempt["endpoint"]) == ("sender_error", config.fingerprint)
    assert json.loads(calls()[0]["stdin"]) == json.loads(note["payload_json"])


def test_retained_notification_is_delivered_once_after_the_sender_recovers(queued, sender, monkeypatch):
    store, clock, note = queued
    script, calls = sender
    monkeypatch.setenv("OPS_TEST_SENDER_EXIT", "3")
    config = _command_config([script])
    deliver(store, config)
    calls_before = len(calls())
    assert deliver(store, config).delivered == 0  # backoff not over yet: nothing is sent
    assert len(calls()) == calls_before

    monkeypatch.setenv("OPS_TEST_SENDER_EXIT", "0")
    clock.advance(31)
    report = deliver(store, config)
    assert (report.delivered, report.exit_code) == (1, 0)
    deliver(store, config)
    clock.advance(10_000)
    deliver(store, config)
    assert len(calls()) == 2  # the failed attempt and exactly one delivery
    assert store.get_notification(note["id"])["state"] == "delivered"


def test_sender_errors_dead_letter_after_max_attempts_but_are_kept(queued, sender, monkeypatch):
    store, clock, note = queued
    script, calls = sender
    monkeypatch.setenv("OPS_TEST_SENDER_EXIT", "5")
    config = _command_config([script], max_attempts=3)
    for _ in range(5):
        deliver(store, config)
        clock.advance(3600)
    row = store.get_notification(note["id"])
    assert (row["state"], row["attempts"], row["last_error"]) == ("failed", 3, "exit 5")
    assert len(calls()) == 3
    assert [a["outcome"] for a in store.delivery_attempts(note["id"])] == ["sender_error"] * 3
    assert store.summary()["failed_notifications"][0]["id"] == note["id"]

    assert store.retry_failed() == 1
    retried = store.get_notification(note["id"])
    assert (retried["state"], retried["attempts"]) == ("pending", 0)


def test_duplicate_event_is_delivered_once(tmp_path, sender):
    store = OpsStore(tmp_path / "state")
    script, calls = sender
    for _ in range(3):
        store.enqueue(
            dedup_key="signal:same-prompt",
            event="permission_needed",
            payload={"event": "permission_needed", "message": "needs permission"},
        )
    config = _command_config([script])
    deliver(store, config)
    deliver(store, config)
    assert len(calls()) == 1
    [row] = store.list_notifications()
    assert (row["state"], row["duplicates"]) == ("delivered", 2)


def test_claims_of_a_crashed_deliverer_are_requeued(queued, sender):
    store, _, note = queued
    script, calls = sender
    claimed = store.claim_next()  # a deliverer took it ... and died before recording
    assert claimed["state"] == "sending"
    report = deliver(store, _command_config([script]))
    assert (report.recovered_sending, report.delivered) == (1, 1)
    assert len(calls()) == 1
    assert [a["outcome"] for a in store.delivery_attempts(note["id"])] == ["interrupted", "delivered"]


def test_only_one_deliverer_runs_at_a_time(queued, sender):
    store, _, note = queued
    script, calls = sender
    holder = FileLock(store.delivery_lock_path, label="deliverer A").acquire()
    try:
        report = deliver(store, _command_config([script]))
        assert (report.busy, report.exit_code) == (True, 75)
        assert calls() == []
    finally:
        holder.release()
    assert store.get_notification(note["id"])["state"] == "pending"


def test_sender_output_and_reference_names_never_reach_the_store(queued, sender, monkeypatch):
    store, _, note = queued
    script, _ = sender
    monkeypatch.setenv("OPS_TEST_SENDER_ECHO", SECRET)  # what a sender might print about its settings
    config = _command_config(
        [script, "--endpoint", "{endpoint}", "--bearer", "{bearer}"],
        endpoint="ops-alerts", bearer="ops-alerts-token",
    )
    report = deliver(store, config)
    assert (report.delivered, report.exit_code) == (1, 0)
    assert SECRET not in repr(config) and SECRET not in str(config)
    assert "ops-alerts-token" not in repr(config)
    assert store.get_notification(note["id"])["last_error"] is None
    conn = sqlite3.connect(store.db_path)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert SECRET not in dump
    assert "ops-alerts-token" not in dump
    assert store.delivery_attempts(note["id"])[0]["endpoint"] == config.fingerprint


def test_timeouts_are_stopped_and_use_the_same_backoff(queued, tmp_path):
    store, clock, note = queued
    script = _executable(tmp_path / "bin" / "hanging-sender", "import time\ntime.sleep(60)\n")
    config = _command_config([script], timeout_s=1)
    started = time.monotonic()
    report = deliver(store, config)
    assert time.monotonic() - started < 30
    assert (report.delivered, report.retried) == (0, 1)
    row = store.get_notification(note["id"])
    assert (row["state"], row["last_error"], row["next_attempt_at"]) == (
        "pending", "TimeoutExpired", clock() + 30,
    )
    assert [a["outcome"] for a in store.delivery_attempts(note["id"])] == ["timeout"]


def test_text_format_and_reference_placeholders(sender):
    script, _ = sender
    config = _command_config(
        [script, "--text", "{message}", "--event", "{event}", "--endpoint", "{endpoint}",
         "--bearer", "{bearer}"],
        endpoint="ops-alerts", bearer="ops-alerts-token", format="text",
    )
    argv, body, env = build_sender_call(
        config, {"event_id": "e1", "kind": "run_finished", "message": "hello"},
    )
    assert body == b"hello\n"
    assert argv == [
        str(script), "--text", "hello", "--event", "run_finished",
        "--endpoint", "ops-alerts", "--bearer", "ops-alerts-token",
    ]
    assert env["AGENTCAP_OPS_EVENT"] == "run_finished"
    assert env["AGENTCAP_OPS_EVENT_ID"] == "e1"
    assert env["AGENTCAP_OPS_ENDPOINT_REF"] == "ops-alerts"
    assert env["AGENTCAP_OPS_BEARER_REF"] == "ops-alerts-token"
    assert "AGENTCAP_OPS_CONFIG" not in env


def test_missing_reference_placeholder_is_refused(sender):
    script, _ = sender
    with pytest.raises(ConfigError, match="config.endpoint"):
        parse_config(json.dumps({"command": [str(script), "{endpoint}"]}).encode())


def test_config_file_must_be_owner_only_and_not_a_symlink(tmp_path, sender):
    script, _ = sender
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    path = write_config(secrets, {"command": [str(script)]}, mode=0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config_file(path)
    os.chmod(path, 0o600)
    assert load_config_file(path).command == (str(script),)
    link = secrets / "link.json"
    link.symlink_to(path)
    with pytest.raises(ConfigError, match="symlink"):
        load_config_file(link)


@requires_git
def test_config_inside_a_git_work_tree_is_refused(tmp_path, sender):
    script, _ = sender
    repo = make_repo(tmp_path / "repo")
    path = write_config(repo, {"command": [str(script)]})
    with pytest.raises(ConfigError, match="git work tree"):
        load_config_file(path)


@pytest.mark.parametrize(
    "data, needle",
    [
        ({"url": "https://hooks.example.invalid/x"}, "refused keys"),
        ({"headers": {"X-A": "a"}}, "refused keys"),
        ({"chat_id": "42"}, "refused keys"),
        ({"command": [], "format": "telegram"}, "telegram"),
        ({"command": [], "format": "yaml"}, "config.format"),
        ({"command": [], "colour": "red"}, "unknown"),
        ({"command": [], "timeout_s": 0}, "timeout_s"),
        ({"command": [], "max_attempts": 0}, "max_attempts"),
        ({"command": [], "endpoint": "https://hooks.example.invalid/x"}, "config.endpoint"),
        ({"command": [], "bearer": "Bearer abc"}, "config.bearer"),
    ],
)
def test_config_validation_rejects_unsafe_values(sender, data, needle):
    script, _ = sender
    value = dict(data)
    if value.get("command") == []:
        value["command"] = [str(script)]
    with pytest.raises(ConfigError, match=needle):
        parse_config(json.dumps(value).encode())


def test_command_validation_rejects_relative_paths_executables_and_secrets(tmp_path, sender):
    script, _ = sender
    plain = tmp_path / "bin" / "not-executable"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o644)
    cases = [
        ({"command": ["sender"]}, "absolute path"),
        ({"command": [str(plain)]}, "executable"),
        ({"command": [str(tmp_path / "bin")]}, "executable"),
        ({"command": []}, "list of"),
        ({"command": str(script)}, "list of"),
        ({"command": [str(script)], "url": "https://hooks.example.invalid/x"}, "refused keys"),
        ({"command": [str(script)], "headers": {"X-A": "a"}}, "refused keys"),
        ({"command": [str(script), "https://hooks.example.invalid/x"]}, "must not carry"),
        ({"command": [str(script), "--token=abc"]}, "must not carry"),
    ]
    for data, needle in cases:
        with pytest.raises(ConfigError, match=needle):
            parse_config(json.dumps(data).encode())


def test_config_can_be_passed_through_a_file_descriptor(sender):
    script, _ = sender
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps({"command": [str(script)]}).encode())
    os.close(write_fd)
    config = load_config_fd(read_fd)
    assert config.command == (str(script),)
    with pytest.raises(OSError):
        os.fstat(read_fd)  # closed after reading: nothing started later inherits it


def test_an_unencodable_message_does_not_wedge_the_queue(tmp_path, sender):
    store = OpsStore(tmp_path / "state", clock=FakeClock())
    script, calls = sender
    store.enqueue(dedup_key="bad", event="permission_needed", payload={"message": "needs \ud800 permission"})
    store.enqueue(dedup_key="good", event="probe", payload={"message": "after"})
    report = deliver(store, _command_config([script], format="text"))
    assert (report.delivered, report.exit_code) == (2, 0)
    assert [call["stdin"] for call in calls()] == ["needs ? permission\n", "after\n"]


def test_a_malformed_row_is_kept_for_retry_instead_of_stopping_delivery(queued, sender):
    store, _, note = queued
    script, calls = sender
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE notifications SET payload_json = '[1, 2]' WHERE id = ?", (note["id"],))
        conn.commit()
    finally:
        conn.close()
    report = deliver(store, _command_config([script]))
    assert (report.delivered, report.retried) == (0, 1)
    row = store.get_notification(note["id"])
    assert (row["state"], row["last_error"]) == ("pending", "AttributeError")
    assert [a["outcome"] for a in store.delivery_attempts(note["id"])] == ["internal_error"]
    assert calls() == []


def test_existing_sender_gets_the_message_and_a_stable_event_id(queued, sender, monkeypatch, tmp_path):
    store, _, note = queued
    script, calls = sender
    monkeypatch.setenv("AGENTCAP_OPS_CONFIG", str(tmp_path / "endpoint.json"))
    config = _command_config([script, "--text", "{message}", "--id", "{event_id}"], format="text")
    report = deliver(store, config)
    assert (report.delivered, report.exit_code) == (1, 0)
    event_id = json.loads(note["payload_json"])["event_id"]
    [call] = calls()
    assert call["stdin"] == "agentcap-ops: T1 FAILURE\n"
    assert call["argv"] == ["--text", "agentcap-ops: T1 FAILURE", "--id", event_id]
    # No AGENTCAP_OPS_CONFIG / state dir: only the event's own stable markers.
    assert call["env"] == {"AGENTCAP_OPS_EVENT": "run_finished", "AGENTCAP_OPS_EVENT_ID": event_id}
    [attempt] = store.delivery_attempts(note["id"])
    assert (attempt["outcome"], attempt["endpoint"]) == ("delivered", config.fingerprint)
    assert str(script) not in repr(config)
