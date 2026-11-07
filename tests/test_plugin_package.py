"""plugin/ (the marketplace's `source`) is in sync with the repo and ships only runtime files."""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
BUILD = ROOT / "scripts" / "build_plugin.py"

if not PLUGIN.is_dir() or not BUILD.is_file():
    pytest.skip("plugin package is not part of this source tree (sdist)", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("build_plugin", BUILD)
build_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_plugin)

FORBIDDEN_PARTS = {"tests", "landing", "__pycache__", ".git", "fixtures", "docs", ".pytest_cache"}
FORBIDDEN_NAMES = {".credentials.json", "license.json", "pyproject.toml", ".env"}
FORBIDDEN_SUFFIXES = {".vsix", ".pyc", ".pem", ".key", ".db", ".sqlite3", ".log", ".gif"}


def _plugin_files() -> list[Path]:
    return sorted(p.relative_to(PLUGIN) for p in PLUGIN.rglob("*") if not p.is_dir())


def test_plugin_dir_is_in_sync_with_sources():
    assert build_plugin.problems() == [], "run: python3 scripts/build_plugin.py"


def test_plugin_files_match_sources_by_hash():
    for rel, src in build_plugin.manifest().items():
        assert build_plugin._sha(PLUGIN / rel) == build_plugin._sha(src), rel


def test_plugin_has_no_forbidden_paths():
    for rel in _plugin_files():
        assert not (set(rel.parts) & FORBIDDEN_PARTS), rel
        assert rel.name not in FORBIDDEN_NAMES, rel
        assert not rel.name.startswith(".env"), rel
        assert rel.suffix not in FORBIDDEN_SUFFIXES, rel
        assert not (PLUGIN / rel).is_symlink(), rel


def test_plugin_ships_exactly_the_manifest_plus_icon():
    expected = set(build_plugin.manifest()) | build_plugin.KEEP
    assert set(_plugin_files()) == expected


def test_import_closure_is_minimal_and_complete():
    closure = build_plugin.import_closure()
    assert "quota" in closure
    # the heavy / networked parts of the package never reach the plugin
    for heavy in ("proxy", "cli", "runner", "license", "upstreams", "ops", "devtools"):
        assert heavy not in closure
    shipped = {p.name for p in (PLUGIN / "src" / "agentcap").glob("*.py")}
    assert shipped == {"__init__.py", *(f"{m}.py" for m in closure),
                       *build_plugin.path_loaded_files(closure)} - {"data/pricing.json"}


def test_marketplace_points_at_plugin_dir_and_manifests_agree():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = market["plugins"][0]
    assert entry["source"] == "./plugin"
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == entry["name"] == "agentcap"


def test_icon_is_square_png_in_range():
    icon = PLUGIN / build_plugin.ICON
    data = icon.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height and 512 <= width <= 2048
    assert len(data) < 2 * 1024 * 1024


def test_copied_plugin_runs_without_the_repo(tmp_path):
    """Simulated install: only plugin/ is copied; the script must import from its own src/."""
    import shutil

    root = tmp_path / "cache" / "agentcap"
    shutil.copytree(PLUGIN, root)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
           "CLAUDE_PLUGIN_ROOT": str(root), "AGENTCAP_QUOTA_DIR": str(tmp_path / "state"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    script = root / "scripts" / "agentcap_cc.py"
    probe = subprocess.run(
        [sys.executable, "-I", str(script), "status"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    hook = subprocess.run(
        [sys.executable, "-I", str(script), "hook", "start"],
        input=json.dumps({"session_id": "pkg-test", "hook_event_name": "SessionStart",
                          "cwd": str(tmp_path), "source": "startup"}),
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert hook.returncode == 0, hook.stderr
    loaded = subprocess.run(
        [sys.executable, "-I", "-c",
         f"import sys; sys.path.insert(0, {str(root / 'src')!r}); import agentcap.quota as q; print(q.__file__)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert loaded.stdout.strip().startswith(str(root)), loaded.stdout
