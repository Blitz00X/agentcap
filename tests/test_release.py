"""Release consistency: one version for the package, plugin manifest and changelog."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import agentcap

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_consistent_everywhere():
    plugin_path = ROOT / ".claude-plugin" / "plugin.json"
    changelog_path = ROOT / "CHANGELOG.md"
    if not plugin_path.exists() or not changelog_path.exists():
        pytest.skip("release metadata is not part of this source tree (sdist)")

    version = agentcap.__version__
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    changelog = changelog_path.read_text(encoding="utf-8")

    assert project["project"]["version"] == version
    assert plugin["version"] == version
    assert f"## {version}\n" in changelog
    assert "Unreleased" not in changelog


def test_internal_ops_tracker_is_not_shipped():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = project["tool"]["hatch"]["build"]["targets"]
    assert "src/agentcap/ops" in targets["wheel"]["exclude"]
    sdist_exclude = targets["sdist"]["exclude"]
    assert "src/agentcap/ops" in sdist_exclude
    # the sdist must not ship tests for a package it leaves out
    assert "tests/test_ops_*.py" in sdist_exclude and "tests/ops_support.py" in sdist_exclude
