"""``python -m agentcap.ops`` end to end, without any model call.

The notification sender is a local program in ``tmp_path``: no endpoint, no
network and no coordinator settings are involved.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from agentcap.ops.cli import build_parser, main
from agentcap.ops.locks import FileLock
from agentcap.ops.store import OpsStore
from ops_support import PY, ops_env, write_config  # noqa: F401

pytestmark = pytest.mark.usefixtures("ops_env")


def _cli(*args, state):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--state-dir", str(state), *args], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _sender(tmp_path, body='#!/bin/sh\ncat >> "$1"\n'):
    path = tmp_path / "bin" / "existing-sender"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_cli_tracks_a_failed_model_and_delivers_its_notification(tmp_path):
    state = tmp_path / "state"
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _cli("task", "add", "--id", "T1", "--title", "fake task", "--worktree", str(wt), state=state)[0] == 0
    assert _cli("model", "set", "--worktree", str(wt), "--model", "fake-model", state=state)[0] == 0

    code, out, err = _cli(
        "run", "--task", "T1", "--model", "fake-model", "--json", "--",
        PY, "-c", "import sys; sys.exit(9)", state=state,
    )
    assert code == 9, err  # the CLI exits with the model's real exit code
    result = json.loads(out)
    assert (result["state"], result["exit_code"], result["task_state"]) == ("failure", 9, "failure")

    status = json.loads(_cli("status", "--json", state=state)[1])
    assert status["tasks"]["failure"] == 1
    assert status["notifications"]["pending"] == 1
    assert status["models"][0]["state"] == "failure"

    sender = _sender(tmp_path)
    sent = tmp_path / "sent.jsonl"
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    config = write_config(secrets, {"command": [str(sender), str(sent)]})
    code, out, err = _cli("deliver", "--config", str(config), state=state)
    assert code == 0, err
    [payload] = [json.loads(line) for line in sent.read_text().splitlines()]
    assert (payload["event"], payload["state"], payload["exit_code"]) == ("run_finished", "failure", 9)
    assert str(sender) not in out + err
    assert json.loads(_cli("status", "--json", state=state)[1])["notifications"]["delivered"] == 1


def test_cli_run_refuses_a_model_that_is_not_the_active_one(tmp_path):
    state = tmp_path / "state"
    wt = tmp_path / "wt"
    wt.mkdir()
    _cli("task", "add", "--id", "T1", "--title", "fake task", "--worktree", str(wt), state=state)
    _cli("model", "set", "--worktree", str(wt), "--model", "model-a", state=state)
    code, _, err = _cli("run", "--task", "T1", "--model", "model-b", "--", PY, "-c", "pass", state=state)
    assert code == 2
    assert "model-a" in err
    assert OpsStore(state).list_runs(task_id="T1") == []


def test_cli_probe_verifies_a_sender_without_touching_tasks(tmp_path):
    state = tmp_path / "state"
    code, out, _ = _cli("notify", "probe", "--json", state=state)
    assert code == 0
    probe = json.loads(out)
    assert probe["event"] == "probe"
    sender = _sender(tmp_path)
    sent = tmp_path / "sent.txt"
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    config = write_config(secrets, {"command": [str(sender), str(sent)], "format": "text"})
    code, _, err = _cli("deliver", "--config", str(config), state=state)
    assert code == 0, err
    text = sent.read_text()
    assert text.startswith("agentcap-ops: endpoint probe")
    assert text.endswith("\n")
    store = OpsStore(state)
    assert store.list_tasks() == [] and store.notification_counts()["delivered"] == 1


def test_cli_verifies_a_completed_run_with_independent_tests(tmp_path):
    state = tmp_path / "state"
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _cli(
        "task", "add", "--id", "T1", "--title", "verified task", "--worktree", str(wt),
        "--no-commit", state=state,
    )[0] == 0
    assert _cli("model", "set", "--worktree", str(wt), "--model", "fake-model", state=state)[0] == 0
    code, out, err = _cli(
        "run", "--task", "T1", "--model", "fake-model", "--json", "--", PY, "-c", "print('work')",
        state=state,
    )
    assert code == 0, err
    assert (json.loads(out)["state"], json.loads(out)["task_state"]) == ("completed", "unverified")

    code, out, err = _cli(
        "task", "verify", "--json", "T1", "--", PY, "-c", "print('1 passed in 0.01s')", state=state,
    )
    assert code == 0, err
    result = json.loads(out)
    assert (result["state"], result["task_state"]) == ("passed", "success")
    assert result["tests"]["counts"]["passed"] == 1
    verification = OpsStore(state).get_verification(result["verification_id"])
    assert verification["evidence_sha256"] == result["evidence_sha256"]
    assert (Path(result["evidence_dir"]) / "verification.json").is_file()
    assert OpsStore(state).get_task("T1")["state"] == "success"
    assert [n["event"] for n in OpsStore(state).list_notifications()] == ["run_finished", "task_verified"]


def test_cli_deliver_without_config_keeps_the_queue(tmp_path):
    state = tmp_path / "state"
    OpsStore(state).enqueue(dedup_key="k", event="probe", payload={"message": "m"})
    code, _, err = _cli("deliver", state=state)
    assert code == 2 and "config" in err
    [row] = OpsStore(state).list_notifications()
    assert (row["state"], row["attempts"]) == ("pending", 0)


def test_run_keeps_the_subcommand_and_the_whole_model_command():
    args = build_parser().parse_args(
        ["run", "--task", "T1", "--model", "m", "--", "fake-model", "-p", "--json", "--task", "X"]
    )
    # The model command used to be stored under dest "command", overwriting the
    # subcommand name, so every `run` crashed in main() (TypeError on a list).
    assert args.command == "run"
    assert args.model_command[-5:] == ["fake-model", "-p", "--json", "--task", "X"]
    assert (args.task, args.model, args.json) == ("T1", "m", False)


def _assert_no_dest_clash(parser, inherited=frozenset(), path=()):
    own = {action.dest for action in parser._actions if action.dest not in ("help", argparse.SUPPRESS)}
    clash = own & inherited
    assert not clash, f"`{' '.join(path) or 'top level'}` overwrites {sorted(clash)} of a parent parser"
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                _assert_no_dest_clash(child, inherited | own, (*path, name))


def test_no_subcommand_argument_overwrites_a_parent_destination():
    # A subparser's values are copied over the parent namespace: a shared dest
    # silently replaces the parent's value (the `run` / "command" crash).
    _assert_no_dest_clash(build_parser())


def test_recover_requeues_what_a_stopped_deliverer_left_sending(tmp_path):
    state = tmp_path / "state"
    store = OpsStore(state)
    note, _ = store.enqueue(dedup_key="k", event="probe", payload={"message": "m"})
    assert store.claim_next()["state"] == "sending"  # claimed by a deliverer ...
    live = FileLock(store.delivery_lock_path, label="live deliverer").acquire()
    try:
        assert _cli("recover", state=state)[0] == 0
        assert store.get_notification(note["id"])["state"] == "sending"  # ... still alive: left alone
    finally:
        live.release()  # ... which has now stopped without recording a result
    code, out, err = _cli("recover", "--json", state=state)
    assert code == 0, err
    assert json.loads(out) == [{"action": "notifications_requeued", "count": 1}]
    row = store.get_notification(note["id"])
    assert (row["state"], row["attempts"]) == ("pending", 1)
    assert [a["outcome"] for a in store.delivery_attempts(note["id"])] == ["interrupted"]


def test_cli_deliver_hands_notifications_to_an_existing_sender(tmp_path):
    state = tmp_path / "state"
    assert _cli("notify", "probe", state=state)[0] == 0
    sender = _sender(tmp_path)
    sent = tmp_path / "sent.txt"
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    config = write_config(secrets, {"command": [str(sender), str(sent)], "format": "text"})
    code, _, err = _cli("deliver", "--config", str(config), state=state)
    assert code == 0, err
    assert sent.read_text().startswith("agentcap-ops: endpoint probe")
    assert sent.read_text().endswith("\n")
    assert OpsStore(state).notification_counts()["delivered"] == 1
