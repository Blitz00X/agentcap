"""CLI routing adapters: Codex ``-c`` injection, Gemini settings, trust flag."""

from __future__ import annotations

import io
import json
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


def test_gemini_settings_are_merged_over_the_parent_file_and_cleaned_up(
    tmp_path, monkeypatch
):
    parent = tmp_path / "parent-settings.json"
    parent.write_text(
        json.dumps(
            {
                "theme": "dark",
                "security": {
                    "auth": {"selectedType": "gateway", "custom": "keep-me"},
                    "other": True,
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[object] = []
    captured: dict[str, object] = {}

    class RecordingChild:
        pid = 999_999
        returncode = 0

        def __init__(self, argv, env):
            self.argv = list(argv)
            self.env = dict(env)
            settings = Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
            captured["payload"] = json.loads(settings.read_text(encoding="utf-8"))
            captured["settings_path"] = settings
            calls.append(self)

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
        env={"PATH": "/bin", "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(parent)},
        popen_factory=lambda argv, env: RecordingChild(argv, env),
        features=set(),
        trust_workspace=True,
    )

    assert runner.run() == 0
    payload = captured["payload"]
    assert payload["theme"] == "dark"
    assert payload["security"]["other"] is True
    assert payload["security"]["auth"] == {
        "selectedType": "gemini-api-key",
        "useExternal": False,
        "custom": "keep-me",
    }
    child = calls[0]
    assert child.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] != str(parent)
    assert child.env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    # The temporary settings file is removed after the run.
    assert not Path(captured["settings_path"]).exists()


def test_gemini_without_trust_flag_does_not_set_it(tmp_path):
    prepared = prepare_child(
        ["gemini", "-p", "hi"],
        port=1234,
        env={"PATH": "/bin"},
        trust_workspace=False,
    )
    try:
        assert "GEMINI_CLI_TRUST_WORKSPACE" not in prepared.env
        settings = Path(prepared.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
        assert json.loads(settings.read_text(encoding="utf-8")) == {
            "security": {
                "auth": {"selectedType": "gemini-api-key", "useExternal": False}
            }
        }
    finally:
        assert prepared.cleanup is not None
        prepared.cleanup()
    assert not settings.exists()


def test_unknown_cli_is_passed_through_unchanged():
    argv = ["python", "agent.py"]
    env = {"PATH": "/bin"}
    prepared = prepare_child(argv, port=1234, env=env)
    assert prepared.argv == argv
    assert prepared.env == env
    assert prepared.cleanup is None
