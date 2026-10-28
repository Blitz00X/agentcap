# AgentCap

**The API-key guard for unattended Codex CLI and Gemini CLI runs.** AgentCap runs
the CLI behind a local proxy on `127.0.0.1`, caps spend for the run, and when the
budget is gone it kills the local process and tells you why. No dashboard, no
account, no code changes for tools that honour a base-URL environment variable.

It meters API-key traffic: `POST /v1/responses` and `/v1/chat/completions`
(Codex CLI uses the Responses API) plus Gemini `generateContent` /
`streamGenerateContent`, by setting `OPENAI_BASE_URL` and
`GOOGLE_GEMINI_BASE_URL` for the child. ChatGPT or Google sign-in
(OAuth/subscription) traffic is not metered.

## What it is

AgentCap is a small, local, serverless spend-cap proxy. You give it a command
and a cap:

```bash
agentcap run --cap 2 -- codex exec "fix the failing tests"
```

Everything the child sends to OpenAI or Gemini is routed through a proxy on
`127.0.0.1`. AgentCap reserves the worst-case cost of each request before it is
forwarded, settles the real usage afterwards, and terminates the agent's process
group if the cap would be exceeded.

## Install

The package is published on PyPI as `agentcap-cli` (the name `agentcap` is
taken), but the command it installs is still `agentcap`.

```bash
# install the command globally
pipx install agentcap-cli

# or into the current environment
pip install agentcap-cli
```

From a checkout:

```bash
uv venv .venv
uv pip install -e .

# or install this checkout as the global command
pipx install .
```

## Quickstart

```bash
# Codex CLI under a $2 cap (API-key mode: uses OPENAI_API_KEY)
agentcap run --cap 2 -- codex exec "fix the failing tests"

# Gemini CLI under a $2 cap (API-key mode: uses GEMINI_API_KEY)
agentcap run --cap 2 -- gemini -p "refactor utils.py"

# Run your own Python agent (it must honour OPENAI_BASE_URL)
agentcap run --cap 1 -- python my_agent.py

# Keep a proxy open for several terminals
agentcap serve --port 8787 --cap 5

# Inspect spend
agentcap status --port 8787
agentcap status --db

# Inspect the bundled price table
agentcap pricing
agentcap pricing google/gemini-4-argon
```

Uses your API key (`OPENAI_API_KEY` / `GEMINI_API_KEY`). ChatGPT or Google
sign-in (OAuth/subscription) traffic is not metered.

`agentcap run` accepts `--cap` (default `$5`, or `$AGENTCAP_CAP`),
`--hour-cap` / `--day-cap` (Pro), `--allow-unmetered`, `--no-clamp`, `--port`
and `--quiet`.

## Try it without an API key

A local fake upstream is bundled for testing metering and clamping offline.

```bash
# Terminal 1: a fake Gemini/OpenAI endpoint on loopback only
python -m agentcap.devtools.fake_upstream --port 9000

# Terminal 2: run a child against it (no API key needed)
AGENTCAP_GEMINI_UPSTREAM=http://127.0.0.1:9000 \
  agentcap run --cap 0.05 -- python - <<'PY'
import os, httpx

base = os.environ["GOOGLE_GEMINI_BASE_URL"]
url = base + "/v1beta/models/gemini-4-argon:generateContent"
r = httpx.post(url, json={"contents": [{"parts": [{"text": "hi"}]}]})
print(r.status_code, "output limit:", r.headers.get("x-fake-output-limit"))
PY
```

The fake returns realistic prompt tokens (`ceil(text characters / 4)`) and
`min(output limit, 2000)` output tokens, so the run has a real cost. The
`x-fake-output-limit` response header echoes the limit the fake actually
received (`none` if the request had none): with a 5-cent cap the number should
be below the model's 65536-token default, proving AgentCap clamped it. Use
`AGENTCAP_OPENAI_UPSTREAM` for OpenAI paths.

Overrides must be `https://`, or `http://` on `127.0.0.1`, `localhost` or
`::1`; anything else is refused with exit code 2. A yellow one-line notice
shows the active override with userinfo and query strings stripped.

## How it works

1. `agentcap run` starts the proxy on a random free port, bound **only** to
   `127.0.0.1`, and waits for `/_agentcap/status`.
2. The child process is started in its own process group with:

   ```text
   OPENAI_BASE_URL=http://127.0.0.1:PORT/openai/v1
   GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:PORT/gemini
   GEMINI_API_BASE_URL=http://127.0.0.1:PORT/gemini
   AGENTCAP_RUN_ID=...
   AGENTCAP_PROXY=http://127.0.0.1:PORT
   ```

3. Before forwarding a request, the proxy estimates the prompt size and the
   maximum output size, then atomically reserves that worst-case USD amount in
   a local SQLite ledger. The input estimate is padded by a conservative
   `input_safety_factor` (1.25 by default) to cover system prompts, tool
   schemas, images and tokenizer differences that the local estimate cannot
   see. Concurrent requests cannot jointly overshoot the cap.
4. After the upstream responds, the real usage is parsed and the reservation is
   settled to the actual cost (rounded up to the micro-dollar). Settlement uses
   the provider's reported tokens, never the padded estimate. If the provider
   bills more input tokens than estimated, that one settlement can exceed its
   reservation; AgentCap emits an `overshoot` alert and the run's spend may end
   slightly above the cap.
5. If the worst case does not fit the remaining budget but the prompt itself
   does, the proxy **clamps the output limit** to exactly what fits (at least
   `256` tokens by default) and reserves only that amount. The agent keeps
   running instead of dying on the first request; real usage still settles the
   reservation. Clamping is on by default and can be disabled with
   `--no-clamp`.
6. If even a minimal clamped request cannot fit, the request is blocked and the
   whole child process group is terminated (`SIGTERM`, then `SIGKILL` after
   5s). `agentcap run` exits with code `3`. A detected loop exits with `4`.
   The kill and its red alert fire at most once per run; later rejected
   requests just receive `429` responses.
7. Unknown models and unparseable bodies are rejected (fail-closed); the request
   is never forwarded unmetered.

The proxy makes only two kinds of request-body change: it adds
`stream_options.include_usage` to OpenAI Chat Completions streams so final
usage is available, and (unless `--no-clamp` is given) it lowers the request's
output limit when the worst-case reservation would cross the remaining budget.
For OpenAI that is `max_completion_tokens` (or the legacy `max_tokens` if the
body already used it) or `max_output_tokens` for the Responses API; for Gemini
it is `generationConfig.maxOutputTokens`. An existing smaller limit is never
raised. Everything else is forwarded byte-for-byte, including credentials.

Reasoning/thinking tokens are billed as output and count against the same
output limit, so a clamp also caps them.

## Supported

| Provider | Endpoints |
| --- | --- |
| OpenAI | `POST /v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings` |
| Gemini | `generateContent`, `streamGenerateContent` (other Gemini endpoints pass through) |

Token counting (`countTokens`) and `GET` requests are free passthroughs.
Request bodies are billed at price-book rates; embeddings are input-only.

## Limitations

- Only tools that honour `OPENAI_BASE_URL` / `GOOGLE_GEMINI_BASE_URL` (and send
  API keys) can be metered. OAuth or subscription logins are **not** metered.
- Prices are a local table, not a live feed. Check them with
  `agentcap pricing`; override any entry in
  `~/.config/agentcap/pricing.json` (or `$AGENTCAP_PRICING_FILE`).
- The proxy rewrites a request only to add `stream_options.include_usage` to
  OpenAI chat streams and, unless `--no-clamp` is set, to lower its output
  limit to what the remaining budget can afford. Prompts are never changed.
- Estimates are deliberately conservative, so a reservation can be larger than
  the eventual cost. The cap is enforced before each request with a worst-case
  reservation, and concurrent requests cannot jointly overshoot it. This is not
  a mathematical guarantee: if a provider bills more input tokens than
  estimated (for example server-side system prompts, tool schemas, images or
  tokenizer differences), the final spend can exceed the cap by at most that
  one request's estimation error. AgentCap emits an `overshoot` alert when a
  settlement exceeds its reservation.
- The ledger lives at `~/.local/share/agentcap/ledger.sqlite3`
  (override with `$AGENTCAP_DB`).

## Privacy

AgentCap stores **no prompts, response contents, API keys or Authorization
headers** — not in the ledger, not in logs, not in alerts. Only token counts,
costs, model names, timestamps and request hashes are recorded. Nothing leaves
your machine except your own upstream API calls — and, for Pro users only, the
license check described below.

**Pro license checks:** activating a Pro key (and the periodic re-check) sends
only the license key, the Polar organization id and an anonymous machine
fingerprint hash (SHA-256; it contains no hostname and no username) to Polar's
public license API at `api.polar.sh`. Free users make no license requests at
all.

## Free vs Pro

| Capability | Free | Pro |
| --- | --- | --- |
| Per-run USD cap with worst-case reservation | ✅ | ✅ |
| Output clamping | ✅ | ✅ |
| Kills the local process at the cap | ✅ | ✅ |
| Terminal alert with the reason | ✅ | ✅ |
| Loop breaker (exit code 4) | — | ✅ |
| Webhook alerts (`AGENTCAP_WEBHOOK_URL`) | — | ✅ |
| Telegram alerts (`AGENTCAP_TELEGRAM_BOT_TOKEN`, `AGENTCAP_TELEGRAM_CHAT_ID`) | — | ✅ |
| Hourly / daily caps | — | ✅ |

Free is **$0 forever**: the basic hard cap — a per-run USD cap with a
worst-case reservation, output clamping, the run killed at the cap, and
terminal alerts — with no account required.

Pro is **$12 one-time** — not a subscription — and is centred on the **loop
breaker**: loop detection that stops a stuck agent and exits with code `4`.
It also adds **alerts** over a webhook and Telegram, plus hourly / daily caps as
an extra, and updates for the 0.x line.

[Buy AgentCap Pro — $12](https://buy.polar.sh/polar_cl_qagsvUdwQ0dRvaW7X5ynEJg5md27pEctIRAr40MBeLi)

After purchase you receive a license key by email (via Polar). Activate it with
`agentcap license activate <KEY>`; `agentcap license deactivate` removes it from
this machine.

## How it compares

| Tool | What it does and how it enforces a cap |
| --- | --- |
| RelayPlane | [relayplane.com](https://relayplane.com): free, MIT-licensed local proxy with daily/hourly/per-request/per-run caps, routing and a kill switch. In its source (as of early Oct 2026) we found no inbound OpenAI Responses API (`/v1/responses`) route and no Gemini-native `generateContent` route, and on budget breach it blocks or warns at the proxy rather than stopping your agent's process. A good choice for Anthropic/OpenAI chat traffic and broader routing. |
| LoopGuard | [tryloopguard.com](https://tryloopguard.com): free MIT CLI that hooks into Claude Code, Codex CLI and Cursor (no proxy, works with subscriptions) with per-session/daily caps and a no-progress loop killer; paid tier ($9/month) adds phone/Slack alerts and remote pause. A good fit if you use subscriptions or Claude Code. |
| LiteLLM budgets | Budgets per key, user or team in the LiteLLM proxy; budgets need its database. A good fit if you already run a LiteLLM gateway; it is a server you deploy and configure. |
| OpenRouter credit limits / provider spend caps | Account- or key-level and typically monthly (OpenRouter credit limits, OpenAI and Google spend caps). Provider caps are evaluated with a delay — Google has described about 10 minutes for the Gemini API cap — so a fast loop can overshoot. |
| AgentCap | Proxy for API-key traffic from Codex CLI, Gemini CLI or your own script: per-run cap with worst-case reservation and output clamping, kills the process group at the cap and prints why. Pro: loop breaker + Telegram/webhook alerts, $12 one-time. |

Pick what matches your setup — these tools can also be combined.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Kutay Karahan.

The Pro license key is separate from the source license. How it is checked:

- On activation the key is validated against Polar's public license API
  (`api.polar.sh`). After that it is re-checked at most about once every 7 days.
- The result is cached in `~/.config/agentcap/license.json` (overridable with
  `$AGENTCAP_LICENSE_FILE`).
- If you are offline, a previously validated license keeps working for up to
  14 days since the last successful check. After that AgentCap falls back to the
  free tier until the key can be validated again.
- `AGENTCAP_POLAR_ORG_ID` selects the Polar organization the key belongs to.

## Development

```bash
uv venv .venv
uv pip install -e '.[dev]'
.venv/bin/pytest -q
```

Tests use mocked upstreams (`httpx.MockTransport`) and never make real network
calls; no real API keys are required.
