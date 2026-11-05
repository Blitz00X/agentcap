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
