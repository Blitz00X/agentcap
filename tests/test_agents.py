"""Subagent lineage + `agentcap waste` on realistic (redacted, synthetic) fixtures.

Formats follow what Claude Code / Codex write today (undocumented):
Claude ``<proj>/<session>/subagents/agent-<id>.jsonl`` with ``isSidechain`` and
``agentId``; Codex ``session_meta`` ``source.subagent.thread_spawn.parent_thread_id``
and ``forked_from_id``.
"""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

import agentcap.cli as cli
from agentcap import history, report, waste

FIX = Path(__file__).parent / "fixtures" / "agents"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    shutil.copytree(FIX / "claude", h / ".claude")
    shutil.copytree(FIX / "codex", h / ".codex")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "nolicense.json"))
    return h


@pytest.fixture
def store(tmp_path, home):
    s = history.HistoryStore(tmp_path / "u.db")
    history.import_claude(s, home / ".claude" / "projects")
    history.import_codex(s, home / ".codex" / "sessions")
    return s


def test_claude_subagent_rows_are_tagged(store):
    sub = [e for e in store.events() if e["agent_id"] == "a1b2c3"]
    assert len(sub) == 2
    assert all(e["parent_session"] == "sess-p" and e["is_sidechain"] == 1 for e in sub)
    assert all(e["model"] == "claude-opus-4-7" for e in sub)
    main = [e for e in store.events() if e["source"] == "claude" and not e["agent_id"]]
    assert main and all(e["is_sidechain"] == 0 for e in main)


def test_codex_spawn_and_fork_lineage(store):
    rows = {e["session_id"]: e for e in store.events() if e["source"] == "codex"}
    assert rows["thr-child"]["parent_session"] == "thr-parent"
    assert rows["thr-child"]["agent_id"] == "reviewer"
    assert rows["thr-fork"]["parent_session"] == "thr-parent"
    assert rows["thr-fork"]["agent_id"] == "fork"
    assert rows["thr-parent"]["parent_session"] is None


def test_reimport_backfills_lineage_on_old_rows(tmp_path, home):
    s = history.HistoryStore(tmp_path / "old.db")
    history.import_claude(s, home / ".claude" / "projects")
    with s._conn() as c:
        c.execute("UPDATE events SET agent_id=NULL, parent_session=NULL")
    history.import_claude(s, home / ".claude" / "projects")
    assert any(e["agent_id"] == "a1b2c3" for e in s.events())


def test_agent_tree_report(store):
    tree = report.agent_tree(store.events())
    root = next(t for t in tree if t["session"] == "sess-p")
    kids = {k["agent"]: k for k in root["children"]}
    assert set(kids) == {"a1b2c3", "d4e5f6"}
    assert root["total_usd"] > root["own_usd"] > 0
    assert 0 < root["opus_share"] < 1
    text = report.render_agent_tree(tree)
    assert "sess-p" in text and "└─" in text and "Opus" in text


def test_report_by_agent_cli(home, capsys):
    assert cli.main(["report", "--by", "agent", "--since", "all"]) == 0
    out = capsys.readouterr().out
    assert "a1b2c3" in out and "reviewer" in out and "subagent" in out.lower()


# ------------------------------------------------------------------ waste
def test_waste_cache_misses_respect_ttl(home):
    w = waste.scan(home=home)
    misses = [m for m in w.cache_misses if m.session == "sess-p"]
    assert len(misses) == 1  # 10 min gap on 5m TTL; 20 min gap on 1h TTL is fine
    assert misses[0].gap_s == pytest.approx(600, abs=1)
    assert misses[0].tokens == 20500
    assert misses[0].usd > 0


def test_waste_repeated_tool_and_error_streaks(home):
    w = waste.scan(home=home)
    rep = [s for s in w.repeat_streaks if s.session == "sess-p"]
    assert rep and rep[0].count == 5 and rep[0].tool == "Bash"
    err = [s for s in w.error_streaks if s.session == "sess-p"]
    assert err and err[0].count == 5
    cx = [s for s in w.repeat_streaks if s.session == "thr-parent"]
    assert cx and cx[0].count == 4 and cx[0].tool == "shell"
    cxe = [s for s in w.error_streaks if s.session == "thr-parent"]
    assert cxe and cxe[0].count == 4
    assert rep[0].usd > 0 and rep[0].stop_at_usd is not None


def test_waste_never_prints_tool_inputs(home, capsys):
    assert cli.main(["waste", "--since", "all"]) == 0
    out = capsys.readouterr().out
    assert "REDACTED" not in out and "npm test" not in out and "pytest" not in out


def test_waste_output_sections_and_pro_nudge(home, capsys):
    assert cli.main(["waste", "--since", "all"]) == 0
    out = capsys.readouterr().out
    for needle in ("SUBAGENTS", "OPUS", "CACHE MISSES", "REPEATED TOOL CALLS", "TOOL ERROR STREAKS"):
        assert needle in out
    last = out.strip().splitlines()[-1]
    assert last.startswith("Pro") and "repeat" in last


def test_waste_json(home, capsys):
    assert cli.main(["waste", "--since", "all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["subagent_share"] > 0 and "cache_misses" in data


# --- shapes seen in real Codex logs (Oct 2026), redacted -------------------
def test_codex_list_output_errors():
    item = lambda t: [{"type": "input_text", "text": t}]
    assert waste._codex_error(item("Script failed\nOutput:\nREDACTED"))
    assert not waste._codex_error(item("Script completed\nOutput:\nREDACTED"))
    assert waste._codex_error(item('Output:\n{"exit_code": 2, "stdout": "REDACTED"}'))
    assert not waste._codex_error(item('Output:\n{"exit_code": 0}'))
    assert waste._codex_error("Process exited with code 1\nREDACTED")
    assert not waste._codex_error([{"type": "input_image", "image_url": "data:"}])


def test_polling_tools_are_not_loops(tmp_path):
    d = tmp_path / "codex" / "sessions"
    d.mkdir(parents=True)
    lines = [{"timestamp": "2026-10-29T09:00:00Z", "type": "session_meta", "payload": {"id": "p"}}]
    for k in range(6):
        for name in ("wait", "sleep"):
            lines.append({"timestamp": "2026-10-29T09:00:01Z", "type": "response_item",
                          "payload": {"type": "custom_tool_call", "name": name, "input": "{}", "call_id": f"{name}{k}"}})
    (d / "rollout-x.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    lines = lines[:1] + [x for x in lines[1:] if x["payload"]["name"] == "wait"]
    (d / "rollout-y.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    w = waste.scan(home=tmp_path, codex_dir=d, claude_dir=tmp_path / "none")
    assert w.repeat_streaks == []


def test_top_level_and_subcommand_help_render(capsys):
    import pytest as _p
    from agentcap.cli import main
    for argv in (["--help"], ["budget", "--help"], ["waste", "--help"], ["report", "--help"], ["run", "--help"]):
        with _p.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 0


def test_waste_cache_misses_come_first_with_suggestion(home, capsys):
    assert cli.main(["waste", "--since", "all"]) == 0
    lines = capsys.readouterr().out.splitlines()
    heads = [i for i, l in enumerate(lines) if l.split("  ")[0].strip() in
             ("CACHE MISSES", "SUBAGENTS", "OPUS", "REPEATED TOOL CALLS", "TOOL ERROR STREAKS")]
    assert lines[heads[0]].startswith("CACHE MISSES")
    tip = [l for l in lines if l.strip().startswith("Tip:")]
    assert tip and "new session" in tip[0] and "/clear" in tip[0]
    assert tip[0] == lines[heads[1] - 1]  # tip closes the cache section


# ---------------------------------------------- 0.2.1: miss split, 1h tip, pricing
def _msg(ts, sess, mid, cw, cr=0, h1=0, agent=None, model="claude-sonnet-4-5"):
    o = {"type": "assistant", "timestamp": ts, "sessionId": sess, "uuid": "u" + mid, "requestId": "r" + mid,
         "isSidechain": agent is not None,
         "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": "x"}],
                     "usage": {"input_tokens": 10, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw, "output_tokens": 10,
                               "cache_creation": {"ephemeral_5m_input_tokens": cw - h1,
                                                  "ephemeral_1h_input_tokens": h1}}}}
    if agent:
        o["agentId"] = agent
    return json.dumps(o)


@pytest.fixture
def split_home(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-w"
    sub = proj / "s1" / "subagents"
    sub.mkdir(parents=True)
    (proj / "s1.jsonl").write_text("\n".join([
        _msg("2026-10-29T09:00:00Z", "s1", "m1", 40000),
        _msg("2026-10-29T09:10:00Z", "s1", "m2", 40000),          # 10 min idle: main miss (5m)
    ]) + "\n")
    (sub / "agent-z9.jsonl").write_text("\n".join([
        _msg("2026-10-29T09:00:30Z", "s1", "a1", 12000, agent="z9"),  # first request: 12k context
        _msg("2026-10-29T09:20:30Z", "s1", "a2", 100000, agent="z9"),  # 20 min idle: subagent miss
    ]) + "\n")
    return tmp_path


def test_waste_misses_split_main_vs_subagent(split_home):
    w = waste.scan(home=split_home)
    main = [m for m in w.cache_misses if m.agent is None]
    sub = [m for m in w.cache_misses if m.agent == "z9"]
    assert len(main) == 1 and len(sub) == 1
    text = waste.render(w, "all")
    assert "main session" in text and "subagents" in text
    assert 'subagentPromptCacheTtl: "1h"' in text


def test_waste_miss_cost_uses_5m_write_rate(split_home):
    # sonnet-4-5: input $3/M, cache read $0.30/M; a 5-minute write is 1.25x input = $3.75/M
    w = waste.scan(home=split_home)
    main = next(m for m in w.cache_misses if m.agent is None)
    assert main.usd == pytest.approx(40000 * (3.75 - 0.30) / 1e6, rel=1e-6)


def test_waste_miss_cost_uses_1h_write_rate(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-w"
    proj.mkdir(parents=True)
    (proj / "s2.jsonl").write_text("\n".join([
        _msg("2026-10-29T09:00:00Z", "s2", "m1", 40000, h1=40000),
        _msg("2026-10-29T11:00:00Z", "s2", "m2", 40000, h1=40000),  # 2 h idle on 1h TTL
    ]) + "\n")
    w = waste.scan(home=tmp_path)
    assert len(w.cache_misses) == 1
    assert w.cache_misses[0].usd == pytest.approx(40000 * (6.0 - 0.30) / 1e6, rel=1e-6)  # 2x input


def test_waste_subagent_starting_context(split_home):
    w = waste.scan(home=split_home)
    assert w.subagent_first == [("claude", "s1", "z9", 12010)]
    text = waste.render(w, "all")
    assert "STARTING CONTEXT" in text and "12.0k" in text


def test_pricer_bills_1h_writes_at_2x():
    p = history._Pricer()
    five = Decimal(p.usd("claude-sonnet-4-5", 0, 0, 1_000_000, 0))
    hour = Decimal(p.usd("claude-sonnet-4-5", 0, 0, 1_000_000, 0, cwrite_1h=1_000_000))
    assert five == Decimal("3.75") and hour == Decimal("6")
