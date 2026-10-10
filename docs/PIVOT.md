# Zhunt v2 — free-tier engine + tiling workspace

Status: **draft for review, nothing built yet.** Branch `pivot/free-tier-workspace`.
The pre-pivot state is tagged `legacy-router-v1` (commit 6b12833).

## Why change

1. **Integrations break.** v1 only works if another app is pointed at the daemon by
   editing that app's config. Hermes, Codex and Claude Desktop rewrite or ignore those
   files on update, and the Codex/Claude desktop apps have no provider hook at all.
2. **The product is invisible.** Routing happens behind other apps, so users never see
   what Zhunt does. Abacus owns its interface; Zhunt should too.
3. **Single paid path.** v1 routes between paid models. Most people have access to many
   free tiers and trials that nothing manages for them.

## Product in one sentence

A local, open-source workspace that sends every prompt to the best *available* model,
free tiers first, switching automatically when one runs dry — usable on its own, or as
an endpoint for other apps.

## Engine (replaces the OpenRouter-centred registry)

Keep from v1: protocol adapters (Anthropic Messages, OpenAI Responses, OpenAI Chat),
request classification (`brain.py`), the routing coordinator, session stickiness, health,
latency, telemetry, auth, port handling. Their tests come with them.

New:

- **Provider catalog** (`catalog/*.yaml`): per platform — base URL, key env var, models,
  capabilities (tools, vision, context), free-tier limits (rpm / rpd / tpm / tpd),
  `may_train_on_prompts`, and a `verified_on` date. OpenRouter becomes one optional entry
  with no special handling. Limits are filled in from each provider's own docs when the
  catalog is built; none are assumed in this draft.
- **Quota + cooldown engine**: per-key sliding windows; a benched `(model, key)` after
  429/5xx/auth failure with escalating cooldown; a hard "can this request go out" check
  separate from ranking.
- **Selection**: classification still picks the tier (chat / coding / long-context /
  reasoning). Within a tier, candidates are healthy free models ranked by reliability
  (decayed success/failure posterior), speed and capability, scaled by remaining quota.
  Paid models are used only if the user enables a budget. When every free candidate is
  exhausted, serve the next tier down rather than fail ("degraded mode").
- **Privacy gate**: models flagged `may_train_on_prompts` are excluded unless the user
  opts in; a per-request override exists in the workspace.
- **Credit**: the quota/cooldown/bandit ideas follow FreeLLMAPI (MIT). Implement fresh in
  Python, keep an attribution note.

## Workspace (replaces onboarding page, dashboard, tray)

Served by the daemon at `127.0.0.1`, talking to the daemon's own OpenAI-compatible
endpoint.

- **Tiles**: chat · compare (same prompt, several models side by side) · routing
  decision ("why this model") · quota & keys · logs. Split, close, swap, resize.
- **Keyboard-first**: a leader key and a command palette; tile focus and layout actions
  from the keyboard.
- **Malleable**: layouts saved as plain JSON files; themes as CSS variables; both
  editable by hand.
- Packaged later as a desktop app; browser first.

## Connecting other apps (optional, demoted)

- `zhunt launch <app>`: run an app with the endpoint injected into that process only.
  No persistent config edits, so app updates cannot break it.
- `zhunt doctor`: detect drift after an app update and say exactly what changed.
- Where a file edit is unavoidable: backup, merge, `--dry-run`, and keep v1's uninstall
  flows.

## Milestones (each with a check I can run)

1. **Stabilise**: catalog schema + loader; quota/cooldown engine with unit tests;
   selection over a fake catalog. *Check: tests; simulated 429 storm still answers.*
2. **Real providers**: 3–4 free providers wired live with the user's own keys.
   *Check: live requests per tier through the real daemon, one per tier.*
3. **Workspace MVP**: chat + compare + routing-decision tiles, tiling layout, saved
   layouts. *Check: drive it in the browser pane.*
4. **Connect**: `launch` for Codex CLI, Claude Code, Hermes; `doctor`.
   *Check: real session per app.*
5. **Remove v1 leftovers**: OpenRouter price sync, old onboarding/dashboard/tray, stale
   registry data, old installer paths. Done last, so nothing is removed before its
   replacement works.

## Risks

- Pooling free tiers across several accounts may breach provider terms; FreeLLMAPI
  labels itself personal-use. Default to one key per provider; say so in the docs.
- Free tiers change often; the catalog needs dates and a refresh path.
- Scope: this is two products (engine + interface). Milestone order keeps the engine
  shippable on its own.

## Open decisions

1. Frontend stack: no-build static ES modules (keeps install to a Python package) vs
   React/Vite (richer ecosystem, adds a Node toolchain).
2. Privacy default: exclude prompt-training providers entirely, or only for the coding
   tier?
3. Paid fallback: off by default (free only) or on with a monthly cap?
4. The Hark interface: needs a link or screenshot before the tile design is fixed.
