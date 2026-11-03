"""An agent must never be able to raise its own quota: approve refuses inside an
agent session, the hook denies a self-approve Bash call, and harness children
carry the guard marker."""
from __future__ import annotations

import io
import json

import pytest

from agentcap import cli
from agentcap import harness as h
from agentcap import quota
from tests.test_quota import _job


def _clear_markers(monkeypatch):
    for name in quota.AGENT_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)


def _hook(job_dir, command, tool="Bash"):
    out = io.StringIO()
    quota.hook_main(
        job_dir,
        io.StringIO(json.dumps({"tool_name": tool, "tool_input": {"command": command}})),
        out,
    )
    return out.getvalue()


@pytest.mark.parametrize("marker", quota.AGENT_ENV_MARKERS)
def test_cli_approve_refused_inside_agent_session(tmp_path, monkeypatch, capsys, marker):
    _clear_markers(monkeypatch)
    root, job_dir = _job(tmp_path)
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(root))
    monkeypatch.setenv(marker, str(job_dir) if marker == "AGENTCAP_QUOTA_JOB" else "1")

    assert cli.main(["approve"]) == 2

    err = capsys.readouterr().err
    assert "approve refused" in err
    assert marker in err

    job = json.loads((job_dir / "job.json").read_text())
    assert not job.get("approvals")


def test_cli_approve_from_human_terminal_still_works(tmp_path, monkeypatch, capsys):
    _clear_markers(monkeypatch)
    root, job_dir = _job(tmp_path)
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(root))

    assert cli.main(["approve"]) == 0

    capsys.readouterr()
    job = json.loads((job_dir / "job.json").read_text())
    assert job["approvals"] == 1
    assert job["quota"] > 4.0


def test_hook_denies_bash_self_approve(tmp_path):
    _root, job_dir = _job(tmp_path)
    before = (job_dir / "job.json").read_text()

    out = _hook(job_dir, "cd /x && agentcap approve")

    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "continue" not in data
    assert (job_dir / "job.json").read_text() == before


def test_hook_allows_unrelated_bash(tmp_path):
    _root, job_dir = _job(tmp_path)
    # not at the limit: the quota branch must not deny this unrelated command
    (job_dir / "status.json").write_text(
        json.dumps({"limit_hit": False, "share": 0.0, "quota": 4.0, "window": "week"})
    )

    assert _hook(job_dir, "echo approve agentcap-cli") == ""


def test_harness_child_env_has_guard_marker(monkeypatch):
    recorded = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            recorded["argv"] = argv
            recorded["env"] = kw.get("env")
            raise RuntimeError("stop before the real child starts")

    monkeypatch.setattr(h.subprocess, "Popen", FakePopen)
    runner = h.HarnessRunner(
        ["opencode", "run", "hi"],
        tokens=10,
        stderr=io.StringIO(),
        stdout=io.StringIO(),
    )

    with pytest.raises(RuntimeError):
        runner.run()

    assert recorded["env"]["AGENTCAP_GUARDED"] == "1"
