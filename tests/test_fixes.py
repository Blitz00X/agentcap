"""`agentcap fixes` and the `waste` work split, on small synthetic Claude Code transcripts."""

import json
import re

import pytest

import agentcap.cli as cli
from agentcap import fixes, waste

T = "2026-11-20T10:{:02d}:00Z"


def _user(k, text, **extra):
    return {"type": "user", "uuid": f"u{k}", "timestamp": T.format(k), "sessionId": "s1",
            "message": {"role": "user", "content": text}, **extra}


def _tool(k, tid, name, inp, text=None, mid=None):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": inp}]
    if text:
        content.insert(0, {"type": "text", "text": text})
    return {"type": "assistant", "uuid": f"a{k}", "timestamp": T.format(k), "sessionId": "s1", "requestId": f"r{k}",
            "message": {"id": mid or f"m{k}", "model": "claude-sonnet-4-5", "content": content,
                        "usage": {"input_tokens": 1000, "output_tokens": 100}}}


def _say(k, text):
    return {"type": "assistant", "uuid": f"a{k}", "timestamp": T.format(k), "sessionId": "s1", "requestId": f"r{k}",
            "message": {"id": f"m{k}", "model": "claude-sonnet-4-5", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 1000, "output_tokens": 100}}}


def _result(k, tid, body="ok", error=False):
    return {"type": "user", "uuid": f"r{k}", "timestamp": T.format(k), "sessionId": "s1", "toolUseResult": {},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                      "content": body, "is_error": error}]}}


def _write(tmp_path, lines, name="s1.jsonl"):
    d = tmp_path / ".claude" / "projects" / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return tmp_path


@pytest.fixture
def session(tmp_path):
    lines = [
        _user(0, "fix the login bug"),
        _tool(1, "t1", "Edit", {"file_path": "/r/app.py", "old_string": "a", "new_string": "b"}),
        _result(2, "t1"),
        _say(3, "Done, the login bug is fixed."),
        _user(4, "no, it still doesn't work on mobile"),                       # correction after done
        _tool(5, "t2", "Edit", {"file_path": "/r/app.py", "old_string": "b", "new_string": "c"}),
        _result(6, "t2"),
        _tool(7, "t3", "Bash", {"command": "cd /r && .venv/bin/python -m pytest -q"}),
        _result(8, "t3", "1 failed", error=True),
        _tool(9, "t4", "Edit", {"file_path": "/r/app.py", "old_string": "c", "new_string": "d"}),  # rework
        _result(10, "t4"),
        _tool(11, "t5", "Bash", {"command": "git push --force origin main"}),
        _result(12, "t5", "The user doesn't want to proceed with this tool use. The tool use was rejected"),
        _user(13, "[Request interrupted by user for tool use]", message={"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user for tool use]"}]}),
        _tool(14, "t6", "Bash", {"command": "git push --force origin main"}),
        _result(15, "t6", "The user doesn't want to proceed with this tool use. The tool use was rejected"),
        _user(16, "<command-name>/agentcap:status</command-name>"),            # a slash command, not you
        _user(17, "Stop hook feedback: x", isMeta=True),                       # meta, not you
        _say(18, "All set."),
        _user(19, "thanks, looks good"),                                       # not a correction
    ]
    for k in range(5):  # churn: 5 more edits of one file
        lines.append(_tool(30 + k, f"c{k}", "Edit", {"file_path": "/r/style.py", "old_string": str(k), "new_string": "x"}))
    lines.append(_say(40, "Finished."))
    lines.append(_user(41, "ok"))
    return _write(tmp_path, lines)


def test_counts(session):
    rep = fixes.scan(home=session)
    assert rep.sessions == 1
    assert rep.messages == 4                       # your 4 typed messages; command/meta/interrupt excluded
    assert rep.count("correction") == 1
    corr = [f for f in rep.fixes if f.kind == "correction"][0]
    assert corr.after_done and corr.text.startswith("no, it still")
    assert rep.count("interrupt") == 1
    assert rep.count("rejected") == 2 and rep.rejected_tools["Bash git"] == 2
    assert [(f, n) for _, f, n in rep.churn] == [("/r/style.py", 5)]
    # "Done" after editing app.py, "All set." after the edit that followed the failed pytest,
    # "Finished." after style.py
    assert rep.done_untested == 3


def test_render_and_suggestions(session):
    text = fixes.render(fixes.scan(home=session), "all")
    for needle in ("CORRECTIONS", "INTERRUPTS", "REJECTED CALLS", "DONE, NO TEST", "EDIT CHURN",
                   "TURN THESE INTO CHECKS", "Stop hook", 'Bash(git:*)', "style.py"):
        assert needle in text
    assert "still doesn't work" in text


def test_json_has_no_text_unless_asked(session, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(session))
    assert cli.main(["fixes", "--since", "all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["corrections"] == 1 and data["rejected_tools"] == {"Bash git": 2}
    assert "mobile" not in json.dumps(data) and all("text" not in f for f in data["fixes"])
    assert cli.main(["fixes", "--since", "all", "--json", "--include-text"]) == 0
    assert "mobile" in capsys.readouterr().out


def test_resumed_copy_is_counted_once(session):
    src = session / ".claude" / "projects" / "p" / "s1.jsonl"
    (src.parent / "s2.jsonl").write_text(src.read_text())
    rep = fixes.scan(home=session)
    assert rep.count("correction") == 1 and rep.messages == 4


@pytest.mark.parametrize("text, hit", [
    ("no, that's wrong", True),
    ("hayır bunu demedim", True),
    ("olmadı", True),
    ("yanlış anladın, sadece README", True),
    ("still broken after your change", True),
    ("dur", True),
    ("dur, VPS açıldı ama 14.50 istiyor, ne yapayım", False),
    ("A ve B yi yapalım en uygun bu değil mi zaten ?", False),
    ("great, ship it", False),
    ("now add a dark mode", False),
    ('bak: <pasted_content id="x">this is wrong and does not work</pasted_content>', False),
])
def test_correction_phrases(text, hit):
    assert fixes.is_correction(text) is hit


def test_no_test_only_counts_code_edits(tmp_path):
    lines = [_user(0, "update the notes"),
             _tool(1, "t1", "Edit", {"file_path": "/r/NOTES.md", "old_string": "a", "new_string": "b"}),
             _say(2, "Done."), _user(3, "ok")]
    assert fixes.scan(home=_write(tmp_path, lines)).done_untested == 0


def test_waste_work_split(session):
    w = waste.scan(home=session, codex_dir=session / "none")
    split = w.split["claude"]
    assert split["verify"] > 0
    assert split["rework"] > 0        # app.py edited after the correction and after the failed pytest
    assert split["new"] > 0
    assert sum(split.values()) == pytest.approx(w.by_source["claude"]["total_usd"])
    text = waste.render(w, "all")
    assert "WHERE IT WENT" in text and "rework after fail/correction" in text


def test_edited_files_and_verify():
    assert waste.edited_files("Edit", {"file_path": "/a/b.py"}) == ["/a/b.py"]
    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** Add File: y.md\n+hi\n*** End Patch"
    assert waste.edited_files("apply_patch", patch) == ["src/x.py", "y.md"]
    assert waste.is_verify_call("Bash", {"command": "cd r && npm run test"})
    assert waste.is_verify_call("shell", {"command": ["bash", "-lc", "cargo test -q"]})
    assert not waste.is_verify_call("Bash", {"command": "git status"})


def test_fixes_help(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["fixes", "--help"])
    assert e.value.code == 0 and re.search(r"--include-text", capsys.readouterr().out)
