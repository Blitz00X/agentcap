# Quota mode — real-account check (run on your own machine, your own login)

Nothing here shares or reads your tokens; you only compare AgentCap's numbers with `/usage` / `/status`.

```bash
# 0. install the branch build
pipx install --force "git+https://github.com/Blitz00X/agentcap@release-0.1.5"   # or pip install -e .
sha256sum ~/.claude/settings.json 2>/dev/null > /tmp/claude_settings.before

# 1. Claude Code (Max/Pro login). Note the weekly % shown by /usage, then /exit.
claude            # type: /usage   -> write down "week: A%"
agentcap quota    # may say "no data yet" the first time (that's expected)

# 2. Quota job: small limit so it triggers. Give it a multi-step task
#    (e.g. "add type hints to every function in this repo and run tests").
cd some-scratch-repo
agentcap run --quota 1% -- claude
#    expect: bottom line "agentcap: job x.x% / 1% (week)" under your own statusline,
#    a warning at 0.8%, then tool calls denied with "agentcap quota mode: ...", exit code 3:
echo "exit=$?"
#    receipt line: "agentcap: this job used X% of the weekly quota"

# 3. Compare: claude -> /usage -> "week: B%".  Target: |(B - A) - X| <= 1 point.
sha256sum -c /tmp/claude_settings.before   # must print OK (settings untouched)

# 4. Headless limit (expected warning, no stop): 
agentcap run --quota 1% -- claude -p "say hi"   # expect "no quota data" / "no subscription rate_limits"

# 5. Codex (ChatGPT login): codex -> /status -> note weekly %; then
agentcap run --quota 1% -- codex      # give a multi-step task; expect SIGINT + exit 3
#    codex -> /status again and compare like step 3.
agentcap quota                        # shows last claude + codex readings
```

Report back: A, B, X (Claude), same for Codex, exit codes, and whether your own statusline still showed.
