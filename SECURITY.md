# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue:

- **Preferred:** GitHub private vulnerability reporting — the **Report a vulnerability** button on
  this repository's [Security tab](https://github.com/Blitz00X/agentcap/security/advisories/new).
- **Or email:** kutaykarahan6@gmail.com with `AgentCap security` in the subject.

Include the AgentCap version (`agentcap --version`, or the plugin version in
`/plugin`), the agent and its version (Claude Code, Codex), your OS, and steps or a proof of
concept.

You will get an answer within 7 days. Confirmed issues are fixed in a release with a test that
failed before the fix, and the CHANGELOG credits the reporter unless you ask otherwise.

## Supported versions

Only the latest 0.x release (PyPI `agentcap-cli`, the Claude Code plugin and the Codex plugin
from this repository) receives security fixes.

## Scope

In scope: the `agentcap` CLI and proxy, the Claude Code plugin (`plugin/`), the Codex plugin
(`codex-plugin/`), and anything that lets an agent or a project bypass a limit, run commands,
read credentials, or send data off the machine.

Out of scope: the accuracy of quota percentages reported by Claude Code or Codex themselves, and
overshoot of up to one request or one 30-second check, which is documented behaviour.
