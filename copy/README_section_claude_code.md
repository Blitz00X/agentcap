## Claude Code (new in 0.1.5)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
agentcap run --cap 3 -- claude -p "fix the failing tests"
# or: agentcap run --cap 3 --hour-cap 10 --day-cap 25 -- npx @anthropic-ai/claude-code -p "..."
```

AgentCap points Claude Code at its local proxy (`ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/anthropic`)
and meters every `POST /v1/messages`, streamed or not:

- Prices come from Anthropic's published table (docs.claude.com pricing, checked 2026-10-30):
  input, cache reads, 5-minute cache writes (1.25x input), 1-hour cache writes (2x input) and output.
  Thinking tokens are billed as output and are covered by `max_tokens`.
- Before a request goes out, AgentCap reserves its worst case (`max_tokens` of output, all prompt
  tokens as 1-hour cache writes). If that does not fit the remaining budget it lowers `max_tokens`
  (and a manual thinking budget) or refuses the request. It never raises a limit.
- Unknown models, unknown `inference_geo` values and fast mode (`speed: "fast"`, not priced yet) are
  blocked (fail-closed). `inference_geo: "us"` is charged at 1.1x.
- `count_tokens` and `GET /v1/models` pass through for free. Other Anthropic endpoints are blocked
  unless you pass `--allow-unmetered`.
- AgentCap refuses to start Claude Code when `CLAUDE_CODE_USE_BEDROCK`, `_VERTEX` or `_FOUNDRY` is set,
  or when a Claude settings file sets its own `ANTHROPIC_BASE_URL` in `env`. Those would send
  traffic around the cap.

Limits:

- With a Claude subscription login instead of an API key, the cap counts API-list-price
  equivalents, not your plan's billing.
- Costs are estimates from provider-reported usage. Your Anthropic invoice is authoritative.
- A large first request needs a cap that fits it. Claude Code's first request often reserves
  about $0.50–$1. AgentCap prints a suggested `--cap` when it refuses one.
