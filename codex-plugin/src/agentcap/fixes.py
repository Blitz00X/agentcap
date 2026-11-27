"""`agentcap fixes` (Free): where you keep correcting your agent, and what to turn into a check.

Reads Claude Code session logs read-only and finds the moments you had to step in:

* corrections: your next message after an agent turn says it was wrong, to undo,
  to stop, or that it still does not work (English and Turkish phrases)
* corrections right after the agent said it was done
* interrupts (Esc) and tool calls you rejected
* edit churn: one file edited many times in one session
* "done" after editing code with no test / lint / build run since the last edit

It then suggests checks that run every time (a hook, a permission rule, a test) for
the patterns that repeat, instead of one more line in CLAUDE.md. No model call, no
network. Your messages are shown on this screen only (first 70 characters); ``--json``
holds counts and no text unless ``--include-text``.

Claude Code only for now; Codex logs are not read yet.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .history import _lines, _safe_file, _ts
from .waste import VERIFY_RE, command_text, edited_files

CHURN_MIN = 5          # edits to one file in one session
EXAMPLES = 5
SNIPPET = 70

# A correction is your message right after an agent turn. Strong phrases count anywhere
# in the first 200 characters; weak ones ("no", "wait", "hayır") only at the start.
_STRONG = re.compile(
    r"(?<!\w)("
    r"that'?s (not|wrong)|this is wrong|not what i|i (said|told you|asked)|you (didn'?t|did not|broke|forgot|missed)"
    r"|(doesn'?t|does not|didn'?t|did not|still (doesn'?t|does not)) work|not working|still (broken|failing|wrong)"
    r"|why did you|revert (it|that|this)|undo (it|that|this)|put it back|roll ?back"
    r"|yanlış|olmadı|olmamış|olmuyor|çalışmıyor|çalışmadı|demedim|dememiştim|söylemedim|istemedim"
    r"|bozdun|bozmuşsun|geri al|eski haline|hala (aynı|olmadı|çalışmıyor|yok)|hâlâ"
    r"|neden (böyle|bunu|öyle)|niye (böyle|bunu)|anlamadın|yanlış anladın|öyle değil|bu değil(?! mi)"
    r")(?!\w)", re.I)
_WEAK_START = re.compile(
    r"^\s*(no|nope|wrong|don'?t|do not|actually|instead|hayır|yok|yapma|değil|ulan|lan|yav|yahu)"
    r"(?!\w)", re.I)
# "stop" / "wait" / "dur" / "bekle" open plenty of plain updates ("dur, VPS açıldı"); only a short
# message that is little more than the word counts.
_STOP_ONLY = re.compile(r"^\s*(stop|wait|hold on|dur|bekle)\b[\s\W]*(\w+[\s\W]*){0,3}$", re.I)
_PASTED = re.compile(r"<pasted_content\b.*?(</pasted_content>|$)", re.S)
_DONE = re.compile(
    r"(?<!\w)(done|finished|complete[d]?|all set|fixed|ready|works now|all tests pass(ed)?|bitti|tamamlandı|hazır"
    r"|düzeltildi|çalışıyor|tamam)(?!\w)", re.I)
_SKIP_PREFIX = ("<command-", "<local-command", "<system-reminder", "<user-prompt-submit-hook", "Caveat:",
                "<task-notification", "<bash-input", "<bash-stdout", "<bash-stderr")
_REJECTED = ("doesn't want to proceed with this tool use", "tool use was rejected")
_INTERRUPT = "[Request interrupted by user"
# "done, no test" only counts edits to code; notes, docs and config need no test run
_CODE_EXT = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".c",
                       ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".vue", ".svelte"})
_NOT_A_PROGRAM = frozenset({"for", "if", "while", "until", "case", "{", "(", "[", "echo", "printf", "cat", "ls",
                            "true", "false", "test", "export", "source", "."})


@dataclass
class Fix:
    kind: str            # correction | interrupt | rejected
    session: str
    ts: float
    after: str           # what the agent did just before: tool names or "text"
    after_done: bool = False
    text: str = ""       # your words, first SNIPPET chars (never in --json unless asked)


@dataclass
class FixesReport:
    sessions: int = 0
    messages: int = 0                     # your own messages
    first_ts: float | None = None
    fixes: list[Fix] = field(default_factory=list)
    churn: list[tuple[str, str, int]] = field(default_factory=list)   # (session, file, edits)
    done_untested: int = 0                # "done" after edits with no verification since
    rejected_tools: Counter = field(default_factory=Counter)          # "Bash git", "Edit", ...

    def count(self, kind: str) -> int:
        return sum(1 for f in self.fixes if f.kind == kind)

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        fixes = [asdict(f) for f in self.fixes]
        if not include_text:
            for f in fixes:
                f.pop("text", None)
        return {"sessions": self.sessions, "messages": self.messages, "first_ts": self.first_ts,
                "corrections": self.count("correction"), "interrupts": self.count("interrupt"),
                "rejected": self.count("rejected"),
                "corrections_after_done": sum(1 for f in self.fixes if f.kind == "correction" and f.after_done),
                "done_untested": self.done_untested,
                "rejected_tools": dict(self.rejected_tools),
                "churn": [{"session": s, "file": Path(p).name, "edits": n} for s, p, n in self.churn],
                "fixes": fixes}


def is_correction(text: str) -> bool:
    """Your own words only: pasted text is left out."""

    head = _PASTED.sub(" ", text).strip()[:200]
    return bool(head and (_STRONG.search(head) or _WEAK_START.match(head) or _STOP_ONLY.match(head)))


def _human_text(obj: dict[str, Any]) -> tuple[str | None, bool]:
    """(your text, interrupted) for a user line written by a person; (None, False) otherwise."""

    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("toolUseResult"):
        return None, False
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None, False
    content = msg.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None, False
        parts = [str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None, False
    parts = [p.strip() for p in parts if p.strip() and not p.lstrip().startswith(_SKIP_PREFIX)]
    if not parts:
        return None, False
    text = "\n".join(parts)
    if text.startswith(_INTERRUPT):
        return text, True
    return text, False


def _rejections(obj: dict[str, Any]) -> Iterator[str]:
    msg = obj.get("message")
    if obj.get("type") != "user" or not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
        return
    for b in msg["content"]:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            body = b.get("content")
            text = body if isinstance(body, str) else json.dumps(body, default=str)
            if any(r in text[:400] for r in _REJECTED):
                yield str(b.get("tool_use_id"))


_CMD_PREFIX = re.compile(r"^\s*(?:(?:cd|pushd)\s+\S+\s*(?:&&|;)\s*|[A-Z_][A-Z0-9_]*=\S*\s+|(?:sudo|env|time|timeout\s+\S+)\s+)+")


def _tool_label(name: str, payload: Any) -> str:
    if name == "Bash":
        cmd = _CMD_PREFIX.sub("", command_text(payload) or "").strip().split()
        return f"Bash {cmd[0].rsplit('/', 1)[-1]}" if cmd else "Bash"
    if name.startswith("mcp__"):
        return name.rsplit("__", 1)[-1]
    return name


def _session(path: Path, since: float | None, rep: FixesReport, seen: set[str]) -> None:
    sess = path.stem
    tools: dict[str, str] = {}                 # tool_use id -> label
    turn: list[str] = []                       # tool labels since your last message
    turn_text = ""                             # the agent's last text in this turn
    agent_spoke = False
    edits: Counter = Counter()
    dirty = False                              # edited since the last verification
    counted = False
    for obj in _lines(path):
        if obj is None:
            continue
        uid = obj.get("uuid")
        if isinstance(uid, str):               # a resumed session copies earlier lines
            if uid in seen:
                continue
            seen.add(uid)
        ts = _ts(obj.get("timestamp"))
        if ts is None or (since is not None and ts < since):
            continue
        sess = str(obj.get("sessionId") or sess)
        if obj.get("isSidechain"):
            continue
        kind = obj.get("type")
        if kind == "assistant":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name = str(b.get("name") or "?")
                    label = _tool_label(name, b.get("input"))
                    tools[str(b.get("id"))] = label
                    turn.append(label)
                    agent_spoke = True
                    files = edited_files(name, b.get("input"))
                    for f in files:
                        edits[f] += 1
                    if any(Path(f).suffix.lower() in _CODE_EXT for f in files):
                        dirty = True
                    cmd = command_text(b.get("input")) if name == "Bash" else None
                    if cmd and VERIFY_RE.search(cmd):
                        dirty = False
                elif b.get("type") == "text" and str(b.get("text") or "").strip():
                    turn_text = str(b["text"])
                    agent_spoke = True
            continue
        for tid in _rejections(obj):
            label = tools.get(tid, "?")
            rep.rejected_tools[label] += 1
            rep.fixes.append(Fix("rejected", sess, ts, label))
        text, interrupted = _human_text(obj)
        if text is None:
            continue
        if not counted:
            rep.sessions += 1
            counted = True
        rep.first_ts = ts if rep.first_ts is None else min(rep.first_ts, ts)
        after = "/".join(sorted(set(turn))[:3]) if turn else "text"
        tail = turn_text.strip()[-300:]
        said_done = bool(agent_spoke and _DONE.search(tail))
        if interrupted:
            rep.fixes.append(Fix("interrupt", sess, ts, after))
            continue
        rep.messages += 1
        if said_done and dirty:
            rep.done_untested += 1
        if agent_spoke and is_correction(text):
            rep.fixes.append(Fix("correction", sess, ts, after, said_done,
                                 " ".join(text.split())[:SNIPPET]))
        turn, turn_text, agent_spoke = [], "", False
    for f, n in edits.items():
        if n >= CHURN_MIN:
            rep.churn.append((sess, f, n))


def scan(home: Path | None = None, since: float | None = None, claude_dir: Path | None = None) -> FixesReport:
    home = Path(home) if home is not None else Path.home()
    claude_dir = claude_dir or home / ".claude" / "projects"
    rep = FixesReport()
    seen: set[str] = set()
    if claude_dir.is_dir():
        paths = [p for p in claude_dir.rglob("*.jsonl") if p.parent.name != "subagents" and _safe_file(p)]

        def key(p: Path) -> tuple[float, str]:
            try:
                return (p.stat().st_mtime, str(p))
            except OSError:
                return (0.0, str(p))
        for path in sorted(paths, key=key):
            _session(path, since, rep, seen)
    rep.churn.sort(key=lambda x: -x[2])
    rep.fixes.sort(key=lambda f: f.ts)
    return rep


# --------------------------------------------------------------------- render
def _per100(n: int, d: int) -> str:
    return f"{n / d * 100:.1f} per 100 of your messages" if d else "-"


def suggestions(rep: FixesReport) -> list[str]:
    out: list[tuple[int, str]] = []
    if rep.done_untested:
        out.append((rep.done_untested,
                    f"{rep.done_untested}x \"done\" after editing with no test/lint/build since -> a Stop hook that"
                    " runs your test command before the agent may finish"))
    after_done = sum(1 for f in rep.fixes if f.kind == "correction" and f.after_done)
    if after_done:
        out.append((after_done,
                    f"{after_done}x you corrected the agent right after it said done -> the same Stop hook; a check"
                    " that fails is a better \"done\" than the agent's word"))
    for label, n in rep.rejected_tools.most_common(2):
        cmd = label.split(" ", 1)[1] if label.startswith("Bash ") else ""
        if n >= 2 and cmd and cmd not in _NOT_A_PROGRAM:
            out.append((n, f"you rejected `{cmd}` {n}x -> a permission rule in .claude/settings.json:"
                           f" \"deny\": [\"Bash({cmd}:*)\"] (or \"ask\"), so it never runs unasked"))
        elif n >= 2 and label in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            out.append((n, f"you rejected {label} {n}x -> an \"ask\" rule for {label} on the paths you protect"
                           " (permissions in .claude/settings.json)"))
    if rep.churn:
        s, f, n = rep.churn[0]
        out.append((n, f"{Path(f).name} edited {n}x in one session -> a check the agent runs after each edit"
                       " (test, lint, screenshot), so it sees the result instead of you"))
    out.sort(key=lambda x: -x[0])
    return [t for _, t in out[:4]]


def render(rep: FixesReport, period: str, top: int = 3, examples: int = EXAMPLES) -> str:
    import datetime as _dt

    start = (_dt.datetime.fromtimestamp(rep.first_ts).strftime("%d %b %Y") if rep.first_ts else "-")
    out = [f"agentcap fixes - {period} - {rep.sessions} Claude Code sessions - {rep.messages} messages from you"
           f" (history from {start})",
           "(local Claude Code logs, read-only; nothing leaves this machine; your words are shown on this screen only)",
           ""]
    if not rep.messages:
        out.append("No messages from you in this period.")
        return "\n".join(out)
    corr = [f for f in rep.fixes if f.kind == "correction"]
    done = [f for f in corr if f.after_done]
    out.append(f"CORRECTIONS        {len(corr):5}  {_per100(len(corr), rep.messages)}"
               " (wrong / undo / stop / still broken)")
    out.append(f"   right after done {len(done):4}  the agent said it was finished, your next message corrected it")
    out.append(f"INTERRUPTS         {rep.count('interrupt'):5}  you stopped a turn")
    rej = ", ".join(f"{k} x{n}" for k, n in rep.rejected_tools.most_common(top))
    out.append(f"REJECTED CALLS     {rep.count('rejected'):5}  you said no to a tool call" + (f"  ({rej})" if rej else ""))
    out.append(f"DONE, NO TEST      {rep.done_untested:5}  \"done\" after editing, no test/lint/build run since the edit")
    out.append(f"EDIT CHURN         {len(rep.churn):5}  files edited >= {CHURN_MIN}x in one session")
    for s, f, n in rep.churn[:top]:
        out.append(f"   {s[:8]}  {Path(f).name[:40]:<40} x{n}")
    if corr:
        before = Counter(f.after.split("/")[0] if f.after != "text" else "only text" for f in corr)
        out.append("")
        out.append("JUST BEFORE YOU CORRECTED IT, THE AGENT RAN")
        out.append("   " + "   ".join(f"{k} {n}" for k, n in before.most_common(5)))
        out.append("")
        out.append("LATEST CORRECTIONS (your words, first 70 characters)")
        for f in corr[-examples:]:
            day = _dt.datetime.fromtimestamp(f.ts).strftime("%d %b")
            out.append(f"   {day}  {f.session[:8]}  after {f.after[:24]:<24} \"{f.text}\"")
    sug = suggestions(rep)
    if sug:
        out.append("")
        out.append("TURN THESE INTO CHECKS (a check runs every time; a line in CLAUDE.md is only a request)")
        for i, s in enumerate(sug, 1):
            out.append(f" {i}. {s}")
    out.append("")
    out.append("Counts come from phrases in your messages (English and Turkish); about 1 in 6 is a false match in our own history.")
    return "\n".join(out)


def render_json(rep: FixesReport, include_text: bool = False) -> str:
    return json.dumps(rep.to_dict(include_text), indent=2)
