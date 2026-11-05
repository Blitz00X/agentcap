"""F6: AgentCap must never chmod a directory it did not create.

Only directories AgentCap creates itself, and its own default state/data/config
directories, are made owner-only (0700).  An existing user, project or shared
directory that a path option points into keeps exactly its mode.  Every test
here works on ``tmp_path`` only.
"""

from __future__ import annotations

import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from agentcap import budget, private, quota, runner
from agentcap.failsafe import JobLock
from agentcap.history import HistoryStore
from agentcap.ledger import Ledger, default_ledger_path
from agentcap.ops import private as ops_private
from agentcap.ops.locks import FileLock
from agentcap.ops.store import OpsStore, default_state_dir

_ENV = ("AGENTCAP_DB", "AGENTCAP_LEDGER_PATH", "AGENTCAP_RECEIPT_DIR", "AGENTCAP_QUOTA_DIR",
        "AGENTCAP_OPS_DIR", "XDG_DATA_HOME", "AGENTCAP_ALLOW_PARALLEL")


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake $HOME (0755, like a real one) with every AgentCap path env cleared."""

    fake = tmp_path / "home"
    fake.mkdir()
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake / ".config"))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return fake


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _shared(tmp_path: Path, perms: int) -> Path:
    shared = tmp_path / f"shared-{perms:o}"
    shared.mkdir()
    shared.chmod(perms)
    return shared


def _write_everything_into(directory: Path, monkeypatch) -> None:
    """Drive every writer that takes a user-chosen directory."""

    Ledger(directory / "ledger.db")
    HistoryStore(directory / "usage.db")
    budget.save(budget.Budget(task_usd=Decimal("1")), directory / "budget.json")
    monkeypatch.setenv("AGENTCAP_RECEIPT_DIR", str(directory))
    private.write_private_text(runner.default_receipt_dir() / "run-1.json", "{}\n")
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(directory))
    quota._atomic_write(quota.state_root() / "q1.json", {"quota": 1})
    quota._append_sample({"used": 1})
    lock = JobLock(quota.state_root() / "quota.lock")
    lock.acquire()
    lock.release()
    ops_lock = FileLock(directory / "ops.lock")
    ops_lock.acquire()
    ops_lock.release()
    OpsStore(directory)
    ops_private.write_private_text(directory / "ops-note.json", "{}\n")


@pytest.mark.parametrize("perms", [0o755, 0o770, 0o700])
def test_existing_directory_mode_is_left_alone(tmp_path, home, monkeypatch, umask_022, perms):
    shared = _shared(tmp_path, perms)
    _write_everything_into(shared, monkeypatch)
    assert mode(shared) == perms
    for name in ("ledger.db", "usage.db", "budget.json", "run-1.json", "q1.json",
                 "claude_samples.jsonl", "quota.lock", "ops.lock", "ops.sqlite3", "ops-note.json"):
        assert mode(shared / name) == 0o600, name


def test_agentcap_db_in_home_does_not_chmod_home(home, monkeypatch, umask_022):
    monkeypatch.setenv("AGENTCAP_DB", "~/agentcap.db")
    ledger = Ledger(default_ledger_path())
    assert ledger.path == home / "agentcap.db"
    assert mode(home) == 0o755
    assert mode(ledger.path) == 0o600


def test_relative_db_path_does_not_chmod_project_dir(tmp_path, home, monkeypatch, umask_022):
    project = _shared(tmp_path, 0o775)
    monkeypatch.chdir(project)
    monkeypatch.setenv("AGENTCAP_DB", "./ledger.db")
    Ledger(default_ledger_path())
    assert mode(project) == 0o775
    assert mode(project / "ledger.db") == 0o600


@pytest.mark.parametrize("umask", [0o000, 0o002, 0o022, 0o077])
def test_directories_agentcap_creates_are_0700(tmp_path, umask):
    shared = _shared(tmp_path, 0o755)
    old = os.umask(umask)
    try:
        Ledger(shared / "new" / "deeper" / "ledger.db")
        ops_private.ensure_private_dir(shared / "ops-new" / "deeper")
    finally:
        os.umask(old)
    assert mode(shared) == 0o755
    for created in ("new", "new/deeper", "ops-new", "ops-new/deeper"):
        assert mode(shared / created) == 0o700, created
    assert mode(shared / "new" / "deeper" / "ledger.db") == 0o600


def test_default_state_dirs_are_tightened_to_0700(home, umask_022):
    # A 0.2.2 install left the default dirs at the umask default (0755).
    state = home / ".local" / "state" / "agentcap"
    (state / "quota").mkdir(parents=True)
    (state / "receipts").mkdir()
    (state / "ops").mkdir()
    for d in (home / ".local", home / ".local" / "state", state, state / "quota",
              state / "receipts", state / "ops"):
        d.chmod(0o755)
    data = home / ".local" / "share" / "agentcap"
    data.mkdir(parents=True, mode=0o755)
    config = home / ".config" / "agentcap"
    config.mkdir(parents=True, mode=0o755)

    Ledger(default_ledger_path())
    JobLock(quota.state_root() / "quota.lock").acquire()
    private.write_private_text(runner.default_receipt_dir() / "run-1.json", "{}\n")
    OpsStore()
    HistoryStore()
    budget.save(budget.Budget(task_usd=Decimal("1")))

    assert default_state_dir() == state / "ops"
    for d in (state, state / "quota", state / "receipts", state / "ops", data, config):
        assert mode(d) == 0o700, d
    # ... but nothing above AgentCap's own directories is touched.
    for d in (home, home / ".local", home / ".local" / "state", home / ".local" / "share",
              home / ".config"):
        assert mode(d) == 0o755, d


def test_fresh_default_state_dir_is_0700(home, umask_022):
    Ledger(default_ledger_path())
    assert mode(home / ".local" / "state" / "agentcap") == 0o700
    assert mode(home) == 0o755


def test_symlinked_default_state_dir_does_not_chmod_its_target(tmp_path, home, umask_022):
    target = _shared(tmp_path, 0o755)
    (home / ".local" / "state").mkdir(parents=True)
    (home / ".local" / "state" / "agentcap").symlink_to(target, target_is_directory=True)
    Ledger(default_ledger_path())
    OpsStore()
    assert mode(target) == 0o755
    assert mode(target / "ledger.sqlite3") == 0o600
    # AgentCap's own subdirectory inside it is still created owner-only.
    assert mode(target / "ops") == 0o700


def test_symlink_inside_state_dir_cannot_redirect_a_chmod(tmp_path, home, umask_022):
    user_dir = _shared(tmp_path, 0o755)
    (user_dir / "sub").mkdir()
    (user_dir / "sub").chmod(0o755)
    state = home / ".local" / "state" / "agentcap"
    state.mkdir(parents=True)
    (state / "link").symlink_to(user_dir, target_is_directory=True)
    for helper in (private.ensure_private_dir, ops_private.ensure_private_dir):
        helper(state / "link")
        helper(state / "link" / "sub")
    assert mode(user_dir) == 0o755
    assert mode(user_dir / "sub") == 0o755


@pytest.mark.parametrize("helpers", [private, ops_private])
def test_symlinked_state_files_are_still_refused(tmp_path, helpers):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "state.json"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        os.close(helpers.open_private(link))
    assert victim.read_text() == "keep"


def test_symlinked_lock_file_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "quota.lock").symlink_to(victim)
    with pytest.raises(OSError):
        JobLock(tmp_path / "quota.lock").acquire()
    assert victim.read_text() == "keep"


@pytest.mark.parametrize("helpers", [private, ops_private])
def test_chmod_failure_is_best_effort(tmp_path, monkeypatch, helpers):
    def refuse(*args, **kwargs):
        raise PermissionError("no POSIX modes here")

    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(os, "fchmod", refuse)
    target = tmp_path / "made" / "here"
    assert helpers.ensure_private_dir(target) == target
    assert target.is_dir()


@pytest.mark.parametrize("helpers", [private, ops_private])
def test_existing_file_in_the_way_still_raises(tmp_path, helpers):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(FileExistsError):
        helpers.ensure_private_dir(blocker)
