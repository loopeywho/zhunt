# Zhunt

Zhunt (pronounced “shunt” with a z) routes each LLM request to the cheapest
capable model without requiring the calling app to change protocols.

The project is in early development. The shared routing core currently includes
request classification, a YAML-backed model registry, session-level model
stickiness, and failure escalation. Thin inbound adapters normalize Anthropic
Messages, OpenAI Responses, and OpenAI Chat Completions requests into that shared
contract.

## Billing warning

Routing through Zhunt uses your provider API keys and is billed per token. It does
not consume flat-rate Claude Max, ChatGPT Pro, Copilot, or Cursor Pro allowances,
so it can cost more than using those subscriptions directly. For Claude Code,
there is no supported passthrough mode: `zhunt install claude --mode api` routes
all Claude traffic through pay-per-token API keys. Claude Max subscribers should
not install that recipe unless they explicitly intend to create that bill.

## Development

Zhunt targets Python 3.12.

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

An initial localhost-only LiteLLM daemon and routing hook are implemented. Run
`zhunt serve --help` for its options. The daemon exposes only the three inference
POST routes and creates a local master key in `~/.zhunt/env` on first run; the
same key is used by the app installer recipes. Non-loopback binding requires
`--allow-non-loopback`. Failed requests are retried once on the promoted route.
Zhunt buffers the first streaming attempt so a truncated or refused response can
be discarded before retrying; routine client-requested output limits do not
escalate a session, while an uncapped provider truncation can promote one tier
for that turn and never double-promotes its replay. Escalated routes decay after
clean turns. Real-provider streaming and tool-call fidelity validation remains
pending until provider keys are available.

## App wiring

The installer backs up an existing configuration before merging a packaged YAML
recipe. Where supported, `passthrough` registers Zhunt without replacing an
app's native default; it is an installer behavior, not a daemon-side bypass.

```console
zhunt install hermes --mode api
zhunt install claude --mode api
zhunt install codex --mode passthrough
zhunt install cursor
zhunt install vscode
zhunt uninstall codex
```

Hermes, Claude Code, Codex, and VS Code use automatic config merges. Cursor keeps
its API key in UI-managed secure storage, so its recipe prints the supported
manual steps instead of modifying undocumented internal state. Claude Code's
native model IDs are classified by request content after they reach Zhunt, so
the API recipe routes them through the registry rather than forwarding them
unchanged.

Provider-backed one-token verification and real-provider streaming/tool-call
fidelity tests remain pending until provider keys are available.
