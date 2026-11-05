"""Worktree-scoped Claude Code hook: installed next to existing hooks, removed
without a trace, never written outside the target worktree.

The HOME settings used here are fixtures in a temp HOME, not real user config.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentcap.ops.hooks import (
    HookError,
    handle_claude_notification,
    hook_command,
    hooks_status,
    install_hooks,
    uninstall_hooks,
)
from agentcap.ops.store import OpsStore
from ops_support import PY, FakeClock, git, make_repo, ops_env, requires_git  # noqa: F401

pytestmark = [pytest.mark.usefixtures("ops_env"), requires_git]

EXISTING = {
    "permissions": {"allow": ["Bash(git status:*)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh", "timeout": 5}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "notify-send claude"}]}],
    },
}

PROMPT = {
    "session_id": "s1",
    "hook_event_name": "Notification",
    "notification_type": "permission_prompt",
    "message": "Claude needs your permission to use Bash",
}


def _write_settings(root, data=EXISTING, raw=None):
    settings = root / ".claude" / "settings.local.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_bytes(raw if raw is not None else (json.dumps(data, indent=4) + "\n").encode())
    return settings


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def test_install_keeps_existing_hooks_and_is_idempotent(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    settings = _write_settings(repo)
    assert install_hooks(repo, state_dir=state).changed is True
    data = json.loads(settings.read_text())
    assert data["permissions"] == EXISTING["permissions"]
    assert data["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    notification = data["hooks"]["Notification"]
    assert notification[0] == EXISTING["hooks"]["Notification"][0]
    assert notification[1] == {
        "hooks": [{"type": "command", "command": hook_command(state, str(repo)), "timeout": 10}]
    }
    before = settings.read_bytes()
    assert install_hooks(repo, state_dir=state).changed is False
    assert settings.read_bytes() == before


def test_uninstall_restores_the_original_bytes(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    raw = b'{\n  "hooks": {"Notification": [{"hooks": [{"type": "command", "command": "x"}]}]},\n  "z": 1\n}\n'
    settings = _write_settings(repo, raw=raw)
    install_hooks(repo, state_dir=state)
    assert settings.read_bytes() != raw
    assert uninstall_hooks(repo, state_dir=state).changed is True
    assert settings.read_bytes() == raw


def test_uninstall_removes_what_install_created(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    install_hooks(repo, state_dir=state)
    assert (repo / ".claude" / "settings.local.json").is_file()
    uninstall_hooks(repo, state_dir=state)
    assert not (repo / ".claude").exists()


def test_uninstall_keeps_hooks_added_after_install(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    settings = _write_settings(repo)
    install_hooks(repo, state_dir=state)
    data = json.loads(settings.read_text())
    data["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": "later.sh"}]})
    settings.write_text(json.dumps(data))
    uninstall_hooks(repo, state_dir=state)
    final = json.loads(settings.read_text())
    assert final["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "later.sh"}]}]
    assert final["hooks"]["Notification"] == EXISTING["hooks"]["Notification"]
    assert final["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    assert "agentcap.ops" not in settings.read_text()


def test_install_touches_only_the_target_worktree(tmp_path, state, ops_env):
    main = make_repo(tmp_path / "main")
    git(main, "commit", "-q", "--allow-empty", "-m", "base")
    git(main, "worktree", "add", "-b", "ops-test", str(tmp_path / "linked"))
    linked = (tmp_path / "linked").resolve()
    main_settings = _write_settings(main)
    home_settings = ops_env / ".claude" / "settings.json"
    home_settings.parent.mkdir()
    home_settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "private"}]}]}}))
    git_hooks = main / ".git" / "hooks"  # shared by every worktree of the repo
    git_hooks.mkdir(exist_ok=True)
    (git_hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    watched = [main_settings, home_settings, *sorted(p for p in git_hooks.iterdir() if p.is_file())]
    before = {path: _sha(path) for path in watched}

    install_hooks(linked, state_dir=state)
    uninstall_hooks(linked, state_dir=state)
    install_hooks(linked, state_dir=state)

    assert {path: _sha(path) for path in watched} == before
    assert "agentcap.ops" in (linked / ".claude" / "settings.local.json").read_text()
    assert "agentcap.ops" not in main_settings.read_text()


def test_refuses_to_write_through_a_symlinked_settings_file(tmp_path, state, ops_env):
    repo = make_repo(tmp_path / "repo")
    target = ops_env / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text("{}\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").symlink_to(target)
    with pytest.raises(HookError, match="symlink"):
        install_hooks(repo, state_dir=state)
    assert target.read_text() == "{}\n"


def test_refuses_a_symlinked_claude_directory(tmp_path, state, ops_env):
    repo = make_repo(tmp_path / "repo")
    (ops_env / ".claude").mkdir()
    (repo / ".claude").symlink_to(ops_env / ".claude")
    with pytest.raises(HookError, match="symlink"):
        install_hooks(repo, state_dir=state)
    assert list((ops_env / ".claude").iterdir()) == []


def test_refuses_anything_but_a_worktree_root(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    with pytest.raises(HookError, match="top level"):
        install_hooks(repo / "sub", state_dir=state)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(HookError, match="git work tree"):
        install_hooks(plain, state_dir=state)
    assert not (repo / "sub" / ".claude").exists() and not (plain / ".claude").exists()


def test_refuses_to_rewrite_invalid_json(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    settings = _write_settings(repo, raw=b"{ not json")
    with pytest.raises(HookError, match="valid JSON"):
        install_hooks(repo, state_dir=state)
    assert settings.read_bytes() == b"{ not json"


def test_refuses_a_settings_file_tracked_by_git(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    settings = _write_settings(repo)
    git(repo, "add", "-f", ".claude/settings.local.json")
    before = settings.read_bytes()
    with pytest.raises(HookError, match="tracked"):
        install_hooks(repo, state_dir=state)
    assert settings.read_bytes() == before


def test_status_reports_installation(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    assert hooks_status(repo, state_dir=state)["installed"] is False
    install_hooks(repo, state_dir=state)
    status = hooks_status(repo, state_dir=state)
    assert (status["installed"], status["entries"], status["tracked"]) == (True, 1, False)
    assert status["git_ignored"] is False


def test_duplicate_permission_prompt_is_recorded_and_queued_once(tmp_path):
    store = OpsStore(tmp_path / "state", clock=FakeClock())
    raw = json.dumps(PROMPT).encode()
    first = handle_claude_notification(store, raw, worktree=str(tmp_path), environ={})
    second = handle_claude_notification(store, raw, worktree=str(tmp_path), environ={})
    assert first["id"] == second["id"]
    assert second["seen_count"] == 2
    [note] = store.list_notifications()
    assert note["event"] == "permission_needed"


def test_idle_prompt_is_not_a_permission_event(tmp_path):
    store = OpsStore(tmp_path / "state")
    idle = {**PROMPT, "notification_type": "idle_prompt", "message": "Claude is waiting for your input"}
    assert handle_claude_notification(store, json.dumps(idle).encode(), worktree=str(tmp_path), environ={}) is None
    assert store.list_notifications() == []


def test_hook_entry_point_never_breaks_the_claude_session(tmp_path):
    state = tmp_path / "state"
    for stdin in (b"{not json", b"", json.dumps(PROMPT).encode()):
        result = subprocess.run(
            [PY, "-m", "agentcap.ops", "--state-dir", str(state), "hook", "claude-notification",
             "--worktree", str(tmp_path)],
            input=stdin, capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == b""
    assert len(OpsStore(state).list_notifications()) == 1


def test_installed_hook_command_records_a_permission_signal(tmp_path, state):
    repo = make_repo(tmp_path / "repo")
    install_hooks(repo, state_dir=state)
    settings = json.loads((repo / ".claude" / "settings.local.json").read_text())
    command = settings["hooks"]["Notification"][-1]["hooks"][0]["command"]
    payload = json.dumps({**PROMPT, "cwd": str(repo)}).encode()
    result = subprocess.run(["/bin/sh", "-c", command], input=payload, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    [note] = OpsStore(state).list_notifications()
    assert json.loads(note["payload_json"])["event"] == "permission_needed"
