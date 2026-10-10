# Zhunt

Zhunt (pronounced “shunt” with a z) is a local, open-source router that sends each
prompt to the best *available* free-tier model and switches automatically when one
runs out of quota or fails. Use it from its own tiling workspace, or as an endpoint
for coding tools such as Codex and Hermes.

> **Status: v2 pre-release.** The engine, daemon, workspace API and app launchers
> are built and tested against simulated providers. Verification against the real
> Groq, Google AI Studio and Cerebras APIs with live keys is still pending, so
> treat provider behaviour as unproven until that is done.

## How it works

1. Each request is classified into a tier: chat, coding, long-context or reasoning.
2. Zhunt ranks the models in that tier that are allowed and have quota left:
   free models first, ordered by observed reliability, speed, capability and
   remaining headroom. Paid models are used only if you turn them on.
3. If a call fails, the model is benched and the request is retried on the next
   candidate (up to three failovers). Cooldowns escalate: 2 minutes, 10 minutes,
   1 hour, then 24 hours. A rejected key (401) or empty account (402) benches every
   model from that provider; a provider's own `retry-after` is honoured.
4. If every model in a tier is out, the request drops to the next tier down
   instead of failing.

Three wire protocols are accepted, so existing tools need no changes: Anthropic
Messages, OpenAI Responses and OpenAI Chat Completions.

## Quick start

Zhunt targets Python 3.12.

```console
python -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'

zhunt keys add groq        # hidden prompt; stored in ~/.zhunt/env (mode 0600)
zhunt serve                # starts the daemon on localhost:4000
zhunt open                 # opens the workspace in your browser, already signed in
```

Run a tool through Zhunt for that one process only, with no config file edited:

```console
zhunt launch codex         # Codex CLI
zhunt launch hermes        # Hermes
zhunt launch codex --dry-run    # show exactly what would be set (keys shown as ***)
zhunt doctor               # check the daemon, keys, policy flags and catalog age
```

Cursor and VS Code have no environment or flag route for a custom endpoint, so
`zhunt launch cursor` and `zhunt launch vscode` print the manual steps instead.

## Providers

The catalog is `zhunt/catalog.yaml`: Groq, Google AI Studio and Cerebras, each
recorded from the provider's own documentation with a verification date. Free tiers
change often, so re-check a row before relying on it. Limits a provider does not
publish are left unset and learned from 429 responses.

| Provider | Key variable | Prompt-training policy used by Zhunt |
| --- | --- | --- |
| Groq | `GROQ_API_KEY` | Not retained by default per Groq's docs |
| Google AI Studio | `GEMINI_API_KEY` | Free tier may use content to improve Google products |
| Cerebras | `CEREBRAS_API_KEY` | No statement found for API prompts; treated as possible |

## Privacy and cost

- **Providers that may train on your prompts are off by default.** Set
  `ZHUNT_ALLOW_TRAINING=1` to allow them. Until you do, only providers marked as
  not training are used.
- **Paid models are off by default.** Set `ZHUNT_ALLOW_PAID=1` to allow them.
- Keys are only ever read from `~/.zhunt/env` and the environment. The workspace and
  its API show whether a key is set, never its value.
- The master key that protects the local API is created on first run. `zhunt open`
  passes it in the URL fragment, which the browser never sends to a server.
- Request telemetry stays in `~/.zhunt/telemetry.jsonl` (no prompt or response
  content) and Zhunt does not phone home.
- Routing through Zhunt uses your provider keys, not a flat-rate subscription such as
  ChatGPT Pro, and the answering model is not the one the tool names.

## Cursor and VS Code coverage

Cursor and VS Code reach Zhunt only through their OpenAI-compatible custom
endpoints, for their chat and agent panels. Cursor tab completion and inline edit stay
on Cursor's backend, and VS Code code completions stay on Copilot. Cursor's
setup is intentionally manual in the vendor UI (`zhunt launch cursor` prints the
steps); Zhunt will not edit undocumented internal state. Both remain subject to the
same provider-key billing and privacy settings described above.

## Moving from the earlier Zhunt

The earlier version edited each app's config file, which broke whenever an app
updated. If you used `zhunt install`, restore those files with:

```console
zhunt uninstall --all
```

The OpenRouter and Nous Portal support, the setup page, the old dashboard, the tray
indicator and `zhunt sync` were removed. The previous code is kept at the git tag
`legacy-router-v1`.

## Development

```console
pytest
```

`docs/PIVOT.md` describes the design and milestones; `docs/WORKSPACE_API.md` is the
contract between the daemon and the workspace.

## Native installers (earlier version)

The macOS, Windows and Linux installers under `packaging/` come from the earlier
version and have not been rebuilt for v2. Treat them as previews, not release-ready
builds. Their notes are kept here so the claims stay honest.

## macOS arm64 preview

The macOS packaging path currently produces Apple Silicon (`arm64`) `.dmg` and
`.pkg` installers. It is not a Universal 2 build: an independently tested
x86_64 build is still required before describing the download as macOS
Universal. The packages are also unsigned; public distribution requires
Developer ID signing and notarization, and Gatekeeper may reject the unsigned
artifacts. Do not publish the macOS installers as release-ready until signing,
notarization and a fresh-machine check are complete. Build instructions are in
`packaging/macos/README.md`.

## Windows x64 preview

The native Windows x64 installer is built on a Windows CI runner; macOS cannot
validate a native executable. Do not describe it as release-ready until it has passed
the fresh Windows 11 x64 checklist in `packaging/windows/README.md`.

## Linux x86_64 preview

The Linux packaging path produces a portable x86_64 `.tar.gz` bundle built in CI,
with no distro packages or ARM64 build. Do not publish it as release-ready until
the Linux CI artifact has passed its real daemon startup and unauthenticated-401
smoke test. Details are in `packaging/linux/README.md`.

## Credits

The quota, cooldown and failover ideas follow
[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (MIT); Zhunt's
implementation is written from scratch in Python.
