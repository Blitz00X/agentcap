## Free vs Pro (0.1.5)

**Free (MIT, forever):** every spend cap.
- Per-run `--cap`, cross-run `--hour-cap` and `--day-cap`
- Output clamping, fail-closed metering, terminal alerts
- Codex, Gemini CLI, Claude Code, and any SDK script

If a Free run repeats the same request 5 times within 120 s, AgentCap does not stop it.
It prints one line: `Pro's loop breaker would have stopped this at $X.XX`.

**Pro, $12 one-time: the unattended pack.** For runs nobody is watching, such as overnight
jobs, CI and background agents:
- **Loop breaker:** stops the agent (exit 4) on the 5th identical request within 120 s,
  before that request is sent.
- **Phone and webhook alerts:** Telegram (`AGENTCAP_TELEGRAM_BOT_TOKEN`, `AGENTCAP_TELEGRAM_CHAT_ID`)
  and webhook (`AGENTCAP_WEBHOOK_URL`) alerts for cap hits, loop kills and the end-of-run receipt.
  Tokens are read from the environment and are never printed or logged.
- **Spend receipt:** a JSON file per run (`~/.local/state/agentcap/receipts/<run>.json`, or
  `AGENTCAP_RECEIPT_DIR`) with requests, spend, caps, the stop reason and the exit code. It contains
  numbers only, never prompts. The same one-line receipt is sent to Telegram and the webhook.
