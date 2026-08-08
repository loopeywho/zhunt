# Zhunt

Zhunt (pronounced “shunt” with a z) shunts every LLM request to the cheapest
capable model without requiring the calling app to change protocols.

The shared routing core includes request classification, a YAML-backed model
registry, session-level model stickiness, health-aware failover, measured
latency-aware selection, and failure escalation. Thin inbound adapters normalize
Anthropic Messages, OpenAI Responses, and OpenAI Chat Completions requests into
that shared contract.

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
`zhunt serve --help` for its options. The daemon exposes only the four inference
POST routes and creates a local master key in `~/.zhunt/env` on first run; the
same key is used by the app installer recipes. Non-loopback binding requires
`--allow-non-loopback`. Failed requests are retried once on the promoted route.
Zhunt buffers the first streaming attempt so a truncated or refused response can
be discarded before retrying; routine client-requested output limits do not
escalate a session, while an uncapped provider truncation can promote one tier
for that turn and never double-promotes its replay. Escalated routes decay after
clean turns. Real-provider streaming and tool-call fidelity validation remains
pending until provider keys are available.

The daemon keeps request telemetry locally in `~/.zhunt/telemetry.jsonl`. Use
`zhunt status` to view today's actual spend versus the counterfactual top-model
spend, broken down by wire dialect. Zhunt does not claim to identify the
originating desktop app unless a future integration supplies that identity;
this avoids merging Cursor/VS Code traffic into another app's bucket. Use
`zhunt serve --telemetry /path/to/evidence.jsonl` when an auditable run needs a
retained telemetry file, and use `zhunt sync` to refresh matching registry
prices from OpenRouter's models API.
No prompt or response content is written to telemetry, and Zhunt does not
phone home.

## Local dashboard and tray indicator

Open the local status dashboard with:

```console
zhunt dashboard
```

It shows daemon reachability, the configured provider, registry model prices,
request counts, and estimated savings. The dashboard is loopback-only and its
API requires a one-time local token; it never displays provider keys or prompt
content. Savings are estimates based on the local registry, so run `zhunt sync`
before using them in public material.

The native installers include an optional menu-bar/system-tray indicator. Run
`zhunt tray` to show a green **Zhunt Active** or red **Zhunt Offline** badge and
open the dashboard from its menu. A Python installation can add the same
support with `python -m pip install 'zhunt[desktop]'`. The indicator reports
daemon health and wire protocol activity; it does not guess which desktop app
originated a request.

Run the offline benchmark with:

```console
zhunt benchmark --output ~/.zhunt/benchmark.json
```

This uses representative, sanitized requests and the registry's projected
prices. It makes no provider calls and does not measure response quality or
real latency, so treat it as a routing/cost baseline rather than proof of
production savings.

## Windows x64 preview

The native Windows x64 installer is built on a Windows CI runner; macOS cannot
validate a native executable. Until the installer artifact has passed the Quill
fresh-machine checklist, Windows users can install the Python wheel with Python
3.12 x64. The packaging instructions and CI build are in
`packaging/windows/README.md`.

The release gate is a fresh Windows 11 x64 install from the produced artifact:
`zhunt --help`, a daemon that stays running and returns 401 without its local
key, loopback setup, provider validation, recipe install/uninstall with backup
restore, and preservation of `%USERPROFILE%\\.zhunt` across uninstall. Do not
describe the Windows installer as release-ready until that test passes on Quill.

## macOS arm64 preview

The macOS packaging path currently produces Apple Silicon (`arm64`) `.dmg` and
`.pkg` installers. It is not a Universal 2 build: an independently tested
x86_64 build is still required before describing the download as macOS
Universal. The packages are also unsigned; public distribution requires
Developer ID signing and notarization, and Gatekeeper may reject the unsigned
artifacts.

The macOS release gate is a fresh Apple Silicon install from the produced
artifact: verify the package checksum, run `zhunt --help`, keep `zhunt serve`
running with an unauthenticated request returning 401, open the loopback setup
UI, and confirm `~/.zhunt/env` is created with user-only access. Do not publish
the macOS installers as release-ready until signing/notarization and this
fresh-machine check are complete. Build instructions are in
`packaging/macos/README.md`.

## Linux x86_64 preview

The Linux packaging path produces a portable x86_64 `.tar.gz` bundle. It is
built on Linux in CI and does not yet provide distro-specific `.deb`/`.rpm`
packages or an ARM64 build. The bundle includes its Python runtime, so no
system Python installation is required. Build and smoke-test instructions are
in `packaging/linux/README.md`.

Do not publish the Linux bundle as release-ready until the Linux CI artifact has
passed its real daemon startup and unauthenticated-401 smoke test.

## Local setup UI

For browser-based setup, run:

```console
zhunt setup
```

This opens a loopback-only setup page where you choose OpenRouter or Nous Portal,
enter an API key, validate access to the provider's model list, and select app
recipes to configure. The key is written with restrictive permissions to
`~/.zhunt/env`; the setup page uses a one-time local token and is not exposed by
the inference daemon. Nous Portal uses its OpenAI-compatible endpoint and
`PORTAL_API_KEY`; live provider responses still require a real account key.

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

The Claude command is intentionally marked `--mode api`: Claude Code has no
supported passthrough mode, so it moves all traffic from a flat-rate Claude Max
subscription to pay-per-token API billing. Real Claude Code turns include tools,
so Zhunt keeps those sessions in the coding tier; expect roughly 1.9×–2.3× savings
versus Sonnet 4.5, not the much larger chat-tier multiple.

Hermes, Claude Code, Codex, and VS Code use automatic config merges. Cursor keeps
its API key in UI-managed secure storage, so its recipe prints the supported
manual steps instead of modifying undocumented internal state. Claude Code's
native model IDs are classified by request content after they reach Zhunt, so
the API recipe routes them through the registry rather than forwarding them
unchanged.

### Tier 2 coverage

Cursor and VS Code are supported for their chat/agent panels through their
OpenAI-compatible custom endpoints. Cursor tab completion and inline edit stay
on Cursor's backend. VS Code code completions stay on Copilot; only the BYOK
chat/agent traffic configured for the custom endpoint reaches Zhunt. Cursor's
setup is intentionally manual in the vendor UI, while VS Code's custom
endpoint is written by the recipe. Both paths remain subject to the same
provider billing caveat and local wire-dialect telemetry labeling.

Provider-backed one-token verification and real-provider streaming/tool-call
fidelity tests remain pending until provider keys are available.
