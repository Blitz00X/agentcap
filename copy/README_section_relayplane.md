## AgentCap vs RelayPlane

RelayPlane is a good tool. If it fits you, use it. The facts below come from RelayPlane's own
website as of 2026-10-30. We have not tested RelayPlane hands-on.

| | RelayPlane (per its site) | AgentCap |
|---|---|---|
| License | MIT | MIT (Free tier) |
| Per-run / session / hour / day caps | Free | Free (run, hour, day) |
| Kill switch | Yes | Yes. Stops the agent's process group (exit 3) |
| Over-budget response | HTTP 429 `budget_exceeded` | HTTP 429, and the request is never sent |
| Output reservation | Capped at 4096 output tokens | Full `max_tokens` worst case, plus clamping to fit |
| Loop breaker | Not listed on their site | Pro |
| Telegram / webhook alerts, spend receipt | Not listed on their site | Pro |

The difference that matters for runaway bills: AgentCap reserves the worst case each request
can cost before sending it, so a single long response cannot overshoot the cap.
