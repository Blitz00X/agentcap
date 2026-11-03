"""T8 live check (opt-in): run the installed harness once and hold its current log
output against agentcap.logschema.CONTRACTS. Skipped unless AGENTCAP_LIVE_CONTRACT=1,
because it makes one real (small) model call. Run it after a harness update."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from agentcap.logschema import CONTRACTS, missing_fields

LIVE = os.environ.get("AGENTCAP_LIVE_CONTRACT") == "1"
PROMPT = "Run the shell command `echo agentcap-contract` once, then reply done."


def _lines(argv: list[str]) -> list[dict]:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL).stdout
    objs = []
    for line in out.splitlines():
        try:
            objs.append(json.loads(line))
        except ValueError:
            pass
    return objs


@pytest.mark.skipif(not LIVE or not shutil.which("opencode"), reason="opt-in live check")
def test_live_opencode_matches_contract():
    model = os.environ.get("AGENTCAP_LIVE_OPENCODE_MODEL", "opencode-go/deepseek-v4.1-flash")
    objs = _lines(["opencode", "run", "--format", "json", "-m", model, PROMPT])
    seen = set()
    for o in objs:
        ev = o.get("type")
        if ev in CONTRACTS["opencode"]["fields"]:
            seen.add(ev)
            assert missing_fields("opencode", ev, o) == [], (ev, o)
    assert seen == set(CONTRACTS["opencode"]["fields"]), f"events not seen: {seen}"


@pytest.mark.skipif(not LIVE or not shutil.which("agy"), reason="opt-in live check")
def test_live_agy_matches_contract():
    objs = _lines(["agy", "-p", PROMPT, "--output-format", "stream-json"])
    seen = set()
    for o in objs:
        su = o.get("step_update") or {}
        ev = "result" if o.get("event") == "result" else (
            f"step_update/{su.get('step_type')}" if su.get("state") == "DONE" else None)
        if ev in CONTRACTS["agy"]["fields"]:
            seen.add(ev)
            assert missing_fields("agy", ev, o) == [], (ev, o)
    assert seen == set(CONTRACTS["agy"]["fields"]), f"events not seen: {seen}"
