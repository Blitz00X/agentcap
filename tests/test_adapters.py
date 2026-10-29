"""CLI routing adapters: Codex ``-c`` injection, Gemini home overlay, trust flag."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from agentcap.adapters import (
    AdapterConflict,
    codex_conflicts,
    detect_agent,
    prepare_child,
)
from agentcap.ledger import Ledger
from agentcap.runner import Runner


def test_detect_codex_and_gemini_executables():
    assert detect_agent(["codex", "exec", "hi"]) == ("codex", 1)
    assert detect_agent(["/usr/local/bin/codex.exe", "exec"]) == ("codex", 1)
    assert detect_agent(["gemini", "-p", "hi"]) == ("gemini", 1)
    assert detect_agent(["python", "agent.py"]) is None


def test_detect_npx_forms_and_insertion_point():
    assert detect_agent(["npx", "@openai/codex", "exec"]) == ("codex", 2)
    assert detect_agent(["npx", "-y", "@openai/codex", "exec"]) == ("codex", 3)
    assert detect_agent(["npx", "-y", "@openai/codex@0.2.0", "exec"]) == ("codex", 3)
    assert detect_agent(["npx", "-y", "@google/gemini-cli", "-p", "hi"]) == (
        "gemini",
        3,
    )
    assert detect_agent(["npx", "-y", "--", "@openai/codex"]) == ("codex", 4)


def test_codex_config_arguments_are_injected_after_the_executable():
    prepared = prepare_child(
        ["codex", "exec", "hi"], port=12345, env={"PATH": "/bin"}
    )
    assert prepared.argv[:5] == [
        "codex",
        "-c",
        'model_provider="agentcap"',
        "-c",
        (
            'model_providers.agentcap={name="AgentCap",'
            'base_url="http://127.0.0.1:12345/openai/v1",'
            'wire_api="responses",env_key="OPENAI_API_KEY",'
            "requires_openai_auth=false,supports_websockets=false}"
        ),
    ]
    assert prepared.argv[5:] == ["exec", "hi"]
    assert prepared.cleanup is None
    # The caller's env is copied, not mutated.
    assert prepared.env["PATH"] == "/bin"


def test_codex_config_arguments_after_npx_package():
    prepared = prepare_child(
        ["npx", "-y", "@openai/codex", "exec", "hi"], port=4321, env={}
    )
    assert prepared.argv[:4] == ["npx", "-y", "@openai/codex", "-c"]
    assert prepared.argv[4] == 'model_provider="agentcap"'
    assert "http://127.0.0.1:4321/openai/v1" in prepared.argv[6]
    assert prepared.argv[7:] == ["exec", "hi"]


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "-c", 'model_provider="openai"', "exec", "hi"],
        ["codex", "--config", "model_providers.foo={}", "exec", "hi"],
        ["codex", "--config=openai_base_url=\"https://example.test\"", "exec"],
        ["npx", "-y", "@openai/codex", "-c", "model_provider=x", "exec"],
    ],
)
def test_codex_routing_conflicts_are_detected(argv):
    assert codex_conflicts(argv)
    with pytest.raises(AdapterConflict):
        prepare_child(argv, port=1, env={})


def test_codex_unrelated_config_is_allowed():
    argv = ["codex", "-c", "model_reasoning_effort=high", "exec"]
    assert codex_conflicts(argv) == []
    prepared = prepare_child(argv, port=1234, env={})
    assert "-c" in prepared.argv


def _fake_child(popen_calls):
    class FakeChild:
        pid = 999_999
        returncode = 0

        def __init__(self, argv, env):
            self.argv = list(argv)
            self.env = dict(env)
            popen_calls.append(self)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def send_signal(self, _sig):
            pass

    def factory(argv, env):
        return FakeChild(argv, env)

    return factory


def test_runner_injects_codex_config_with_the_real_proxy_port(tmp_path):
    calls: list[object] = []
    err = io.StringIO()
    runner = Runner(
        ["codex", "exec", "hi"],
        cap="1",
        stderr=err,
        ledger=Ledger(tmp_path / "codex.db"),
        popen_factory=_fake_child(calls),
        features=set(),
    )

    assert runner.run() == 0
    assert calls and runner.port is not None
    argv = calls[0].argv
    assert argv[0] == "codex"
    assert argv[1:3] == ["-c", 'model_provider="agentcap"']
    assert f"http://127.0.0.1:{runner.port}/openai/v1" in argv[4]
    assert "supports_websockets=false" in argv[4]


def test_runner_refuses_conflicting_codex_config_without_spawning():
    calls: list[object] = []
    err = io.StringIO()
    runner = Runner(
        ["codex", "-c", 'model_provider="openai"', "exec"],
        cap="1",
        stderr=err,
        popen_factory=_fake_child(calls),
        features=set(),
    )

    assert runner.run() == 2
    assert calls == []
    assert runner.server is None
    assert "refusing to run Codex" in err.getvalue()
    assert "model_provider" in err.getvalue()


def _tempdir_factory(root: Path):
    def factory(*, prefix: str = "") -> str:
        return tempfile.mkdtemp(prefix=prefix, dir=root)

    return factory


def _make_home(root: Path, settings: object | None = None) -> tuple[Path, Path]:
    """Create ``<root>/home/.gemini`` with a few user entries."""

    home = root / "home"
    gemini = home / ".gemini"
    gemini.mkdir(parents=True)
    if settings is not None:
        (gemini / "settings.json").write_text(
            settings if isinstance(settings, str) else json.dumps(settings),
            encoding="utf-8",
        )
    (gemini / "trustedFolders.json").write_text('["/work"]\n', encoding="utf-8")
    (gemini / "history").mkdir()
    (gemini / "history" / "session.json").write_text("{}\n", encoding="utf-8")
    return home, gemini


def test_gemini_overlay_sets_cli_home_and_not_system_settings_path(tmp_path):
    home, _ = _make_home(tmp_path)
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"PATH": "/bin", "HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"])
        assert overlay.is_dir()
        assert overlay.parent == tmp_path
        assert (overlay / ".gemini" / "settings.json").is_file()
        assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in prepared.env
    finally:
        assert prepared.cleanup is not None
        prepared.cleanup()


def test_gemini_overlay_forces_api_key_auth_and_keeps_user_keys(tmp_path):
    home, _ = _make_home(
        tmp_path,
        {
            "theme": "dark",
            "mcpServers": {"local": {"command": "mcp-server"}},
            "model": {"name": "gemini-3-pro"},
            "security": {
                "auth": {"selectedType": "oauth-personal", "custom": "keep-me"},
                "other": True,
            },
        },
    )
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"])
        settings_path = overlay / ".gemini" / "settings.json"
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        assert payload["theme"] == "dark"
        assert payload["mcpServers"] == {"local": {"command": "mcp-server"}}
        assert payload["model"] == {"name": "gemini-3-pro"}
        assert payload["security"]["other"] is True
        assert payload["security"]["auth"] == {
            "selectedType": "gemini-api-key",
            "useExternal": False,
            "custom": "keep-me",
        }
        assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(overlay.stat().st_mode) == 0o700
        assert stat.S_IMODE((overlay / ".gemini").stat().st_mode) == 0o700
    finally:
        prepared.cleanup()


def test_gemini_jsonc_user_settings_are_parsed(tmp_path):
    home, _ = _make_home(
        tmp_path,
        """
        {
          // line comment
          "theme": "light", /* block comment */
          "model": {"name": "gemini-3-pro",},
          "mcpServers": {"local": {"command": "mcp-server"}},
        }
        """,
    )
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"])
        payload = json.loads(
            (overlay / ".gemini" / "settings.json").read_text(encoding="utf-8")
        )
        assert payload["theme"] == "light"
        assert payload["model"] == {"name": "gemini-3-pro"}
        assert payload["mcpServers"] == {"local": {"command": "mcp-server"}}
        assert payload["security"]["auth"]["selectedType"] == "gemini-api-key"
    finally:
        prepared.cleanup()


def test_gemini_garbage_user_settings_fall_back_to_defaults(tmp_path, capsys):
    home, _ = _make_home(tmp_path, "{not json at all,}")
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"])
        payload = json.loads(
            (overlay / ".gemini" / "settings.json").read_text(encoding="utf-8")
        )
        assert payload == {
            "security": {
                "auth": {"selectedType": "gemini-api-key", "useExternal": False}
            }
        }
        err = capsys.readouterr().err
        assert err.count("could not parse ~/.gemini/settings.json") == 1
    finally:
        prepared.cleanup()


def test_gemini_overlay_links_user_entries_but_writes_real_settings(tmp_path):
    home, gemini = _make_home(tmp_path, {"theme": "dark"})
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"]) / ".gemini"
        for name in ("trustedFolders.json", "history"):
            link = overlay / name
            assert link.is_symlink()
            assert Path(os.readlink(link)) == gemini / name
            assert Path(os.readlink(link)).is_absolute()
        settings_path = overlay / "settings.json"
        assert settings_path.is_file()
        assert not settings_path.is_symlink()
        # Writes through the links land in the real home.
        (overlay / "history" / "new.json").write_text("{}\n", encoding="utf-8")
        assert (gemini / "history" / "new.json").is_file()
        # The real settings file is untouched.
        assert json.loads((gemini / "settings.json").read_text(encoding="utf-8")) == {
            "theme": "dark"
        }
    finally:
        prepared.cleanup()


def test_gemini_existing_cli_home_is_used_as_source(tmp_path):
    cli_home, gemini = _make_home(tmp_path, {"theme": "cli-home"})
    _, other_gemini = _make_home(tmp_path / "other", {"theme": "from-HOME"})
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(tmp_path / "other" / "home"), "GEMINI_CLI_HOME": str(cli_home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"])
        assert overlay != cli_home
        payload = json.loads(
            (overlay / ".gemini" / "settings.json").read_text(encoding="utf-8")
        )
        assert payload["theme"] == "cli-home"
        assert (overlay / ".gemini" / "trustedFolders.json").is_symlink()
        assert not (overlay / ".gemini" / "other-marker").exists()
        assert (other_gemini / "settings.json").read_text(encoding="utf-8") == (
            json.dumps({"theme": "from-HOME"})
        )
    finally:
        prepared.cleanup()


def test_gemini_missing_real_gemini_dir_still_creates_overlay(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        overlay = Path(prepared.env["GEMINI_CLI_HOME"]) / ".gemini"
        payload = json.loads((overlay / "settings.json").read_text(encoding="utf-8"))
        assert payload["security"]["auth"]["selectedType"] == "gemini-api-key"
        assert list(overlay.iterdir()) == [overlay / "settings.json"]
        assert not (home / ".gemini").exists()
    finally:
        prepared.cleanup()


def test_gemini_cleanup_removes_overlay_and_leaves_real_home_intact(tmp_path):
    home, gemini = _make_home(tmp_path, {"theme": "dark", "model": {"name": "x"}})
    trusted = gemini / "trustedFolders.json"
    history_file = gemini / "history" / "session.json"
    before = {
        "settings": (gemini / "settings.json").read_bytes(),
        "trusted": trusted.read_bytes(),
        "history": history_file.read_bytes(),
    }
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    overlay = Path(prepared.env["GEMINI_CLI_HOME"])
    assert overlay.is_dir()
    prepared.cleanup()

    assert not overlay.exists()
    assert (gemini / "settings.json").read_bytes() == before["settings"]
    assert trusted.read_bytes() == before["trusted"]
    assert history_file.read_bytes() == before["history"]
    assert sorted(entry.name for entry in gemini.iterdir()) == [
        "history",
        "settings.json",
        "trustedFolders.json",
    ]


def test_gemini_leaves_user_system_settings_path_untouched(tmp_path):
    home, _ = _make_home(tmp_path)
    user_path = str(tmp_path / "user-settings.json")
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home), "GEMINI_CLI_SYSTEM_SETTINGS_PATH": user_path},
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        assert prepared.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == user_path
        assert prepared.env["GEMINI_CLI_HOME"] != str(home)
    finally:
        prepared.cleanup()


def test_gemini_trust_workspace_sets_the_env_flag(tmp_path):
    home, _ = _make_home(tmp_path)
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        trust_workspace=True,
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        assert prepared.env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    finally:
        prepared.cleanup()


def test_gemini_without_trust_flag_does_not_set_it(tmp_path):
    home, _ = _make_home(tmp_path)
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"HOME": str(home)},
        trust_workspace=False,
        tempdir_factory=_tempdir_factory(tmp_path),
    )
    try:
        assert "GEMINI_CLI_TRUST_WORKSPACE" not in prepared.env
        overlay = Path(prepared.env["GEMINI_CLI_HOME"])
        settings = overlay / ".gemini" / "settings.json"
        assert json.loads(settings.read_text(encoding="utf-8")) == {
            "security": {
                "auth": {"selectedType": "gemini-api-key", "useExternal": False}
            }
        }
    finally:
        assert prepared.cleanup is not None
        prepared.cleanup()
    assert not overlay.exists()


def test_runner_uses_gemini_cli_home_overlay_and_cleans_up(tmp_path):
    home, gemini = _make_home(tmp_path, {"theme": "dark"})
    captured: dict[str, object] = {}

    class RecordingChild:
        pid = 999_999
        returncode = 0

        def __init__(self, argv, env):
            self.argv = list(argv)
            self.env = dict(env)
            overlay = Path(env["GEMINI_CLI_HOME"])
            captured["overlay"] = overlay
            captured["settings"] = json.loads(
                (overlay / ".gemini" / "settings.json").read_text(encoding="utf-8")
            )
            captured["system_settings"] = env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def send_signal(self, _sig):
            pass

    runner = Runner(
        ["gemini", "-p", "hi"],
        cap="1",
        stderr=io.StringIO(),
        ledger=Ledger(tmp_path / "gemini.db"),
        env={"PATH": "/bin", "HOME": str(home)},
        popen_factory=lambda argv, env: RecordingChild(argv, env),
        features=set(),
        trust_workspace=True,
    )

    assert runner.run() == 0
    payload = captured["settings"]
    assert payload["theme"] == "dark"
    assert payload["security"]["auth"]["selectedType"] == "gemini-api-key"
    assert captured["system_settings"] is None
    assert not Path(captured["overlay"]).exists()
    assert (gemini / "settings.json").is_file()


def test_unknown_cli_is_passed_through_unchanged():
    argv = ["python", "agent.py"]
    env = {"PATH": "/bin"}
    prepared = prepare_child(argv, port=1234, env=env)
    assert prepared.argv == argv
    assert prepared.env == env
    assert prepared.cleanup is None
