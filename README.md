# Zhunt

Zhunt (pronounced “shunt” with a z) routes each LLM request to the cheapest
capable model without requiring the calling app to change protocols.

The project is in early development. The first implementation slice is the shared
routing core: request classification, a YAML-backed model registry, and
session-level model stickiness. Anthropic Messages, OpenAI Responses, and OpenAI
Chat Completions adapters will be added after those contracts are stable.

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

The daemon and wire-dialect adapters are not implemented yet.
