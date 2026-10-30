import shutil
from pathlib import Path

import pytest

from agentcap import cli, history

FIX = Path(__file__).parent / "fixtures" / "history"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    shutil.copytree(FIX / "claude", home / ".claude")
    shutil.copytree(FIX / "codex", home / ".codex")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "nolicense.json"))
    return tmp_path


def test_import_command_reports_counts(env, capsys):
    assert cli.main(["import"]) == 0
    out = capsys.readouterr().out
    assert "imported 4" in out
    assert (env / "data" / "agentcap" / "usage.db").exists()
    assert cli.main(["import"]) == 0
    assert "imported 0" in capsys.readouterr().out


@pytest.mark.parametrize("by,needle", [
    ("day", "2026-10-29"), ("model", "gpt-5.4"), ("session", "sess-a"), ("project", "/home/dev/api"),
])
def test_report_groups(env, capsys, by, needle):
    cli.main(["import"])
    capsys.readouterr()
    assert cli.main(["report", "--by", by, "--since", "all"]) == 0
    out = capsys.readouterr().out
    assert needle in out
    assert "cache read" in out.lower()
    assert "API-equivalent" in out


def test_report_imports_first_when_db_empty(env, capsys):
    assert cli.main(["report", "--by", "model", "--since", "all"]) == 0
    assert "claude-sonnet" in capsys.readouterr().out


def test_report_json(env, capsys):
    import json
    cli.main(["import"])
    capsys.readouterr()
    assert cli.main(["report", "--by", "model", "--since", "all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    models = {r["key"] for r in data["rows"]}
    assert "gpt-5.4" in models
    row = next(r for r in data["rows"] if r["key"] == "gpt-5.4")
    assert row["cache_read_tokens"] == 16000


def test_report_csv(env, capsys):
    cli.main(["import"])
    capsys.readouterr()
    assert cli.main(["report", "--by", "session", "--since", "all", "--csv"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("key,requests,input_tokens")
