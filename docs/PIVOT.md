# Zhunt v2 — free-tier engine + tiling workspace

Status (2026-10-10): **milestones 1-5 built and tested** on branch
`pivot/free-tier-workspace` (404 tests); the pre-pivot state is tagged
`legacy-router-v1` (commit 6b12833). Not yet pushed.

| Milestone | State |
| --- | --- |
| 1 Engine core | Done |
| 2 Real providers | Wired and exercised with fake keys against the real APIs; **the live per-tier check with real keys is still pending** (`scripts/live_tier_check.py`) |
| 3 Workspace | Backend API and frontend done; driven in the browser against the real daemon. floating/tiled window styles, For you and Panels windows and the theme pack added 2026-10-11 (checked against the mock daemon) |
| 4 Connect | `launch`/`doctor`/`open` done for Codex and Hermes; Codex from docs only (not installed here), Hermes not run live. Claude Code support (launcher and install recipe) removed 2026-10-11 at the owner's request |
| 5 Remove v1 | Done; the public `site/` and native installers are still v1 and are not rebuilt |

The text below is the original plan, kept for the reasoning.

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
- **Quota + cooldown engine**: per-key sliding windows persisted in `~/.zhunt/state.db`
  (survive restarts); a benched `(model, key)` after a failure: 90 s for a per-minute
  429, the 2m/10m/1h/24h ladder only when the daily quota is spent (or 429s repeat with
  no known daily cap, capped at 10 min), until the daily reset when the provider says so
  (midnight Pacific for Google), fixed benches for 401/402/403/404, and a 10-minute
  model-wide bench after 3 failures in 15 min. Limits stated in 429 bodies or
  rate-limit headers are learned (only ever lowered). A hard "can this request go out"
  check stays separate from ranking.
- **Selection**: classification still picks the tier (chat / coding / long-context /
  reasoning). Within a tier, candidates are healthy free models ranked by reliability
  (Thompson sample of a decayed success/failure posterior), speed and capability,
  demoted in the last 20% of quota and by a short-lived penalty after 429s/errors.
  Paid models are used only if the user enables a budget. When every free candidate is
  exhausted, serve the next tier down rather than fail ("degraded mode").
- **Privacy gate**: models flagged `may_train_on_prompts` are excluded unless the user
  opts in; a per-request override exists in the workspace.
- **Credit**: the quota/cooldown/bandit ideas follow FreeLLMAPI (MIT). Implement fresh in
  Python, keep an attribution note.

## Workspace (replaces onboarding page, dashboard, tray)

Served by the daemon at `127.0.0.1`, talking to the daemon's own OpenAI-compatible
endpoint.

- **Window styles** (added 2026-10-11): *floating* (Hark-style glass windows over a
  wallpaper: drag, resize, overlap, minimise to a dock) or *tiled* (Omarchy/Hyprland split
  grid), switchable at any time over the same live windows. Hark itself is a closed cloud
  agent product; only its interface ideas (proactive cards, panels, calm full-screen look)
  are borrowed, as the *For you* and *Panels* windows. Omarchy's theme palettes ship as
  theme files with wallpapers.
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
4. ~~The Hark interface~~: researched from Hark Pro's launch coverage (no link was supplied); see Window styles above.
