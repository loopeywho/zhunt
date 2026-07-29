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
so it can cost more than using those subscriptions directly. Passthrough mode is
planned as the default for subscription-backed apps.

## Development

Zhunt targets Python 3.12.

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

An initial localhost-only LiteLLM daemon and routing hook are implemented. Run
`zhunt serve --help` for its options. Failed requests are retried once on the
promoted route. Zhunt buffers the first streaming attempt so a truncated or
refused response can be discarded before retrying; real-provider streaming and
tool-call fidelity validation remains pending until provider keys are available.
Installer recipes remain.
