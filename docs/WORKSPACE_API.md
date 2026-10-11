# Workspace API contract (v2)

The workspace (browser UI) and the daemon agree on this contract. The backend and
frontend are built in parallel against it; change it only by editing this file.

All endpoints live on the daemon (`127.0.0.1`, free-tier mode) and require
`Authorization: Bearer <ZHUNT_MASTER_KEY>`, except the static files. The UI gets the
key from the URL **fragment** (`/workspace/#key=...`, never sent to the server),
keeps it in `sessionStorage`, and sends it as the Bearer token. `zhunt open` builds
that URL.

## Static

`GET /workspace/` serves the package directory `zhunt/workspace/` (`index.html`,
`js/`, `css/`). No build step; plain ES modules. Unauthenticated (it contains no
secrets).

## Chat (existing endpoint, no change)

`POST /v1/chat/completions` with `model: "zhunt-auto"` (or `zhunt-chat`,
`zhunt-coding`, `zhunt-long-context`, `zhunt-reasoning`), `stream: true|false`,
header `x-session-id: <ui session id>`. After a reply the UI looks the decision up
in `/zhunt/api/decisions` by that session id.

## `GET /zhunt/api/status`

```json
{
  "version": "0.1.0.dev0",
  "policy": {"allow_paid": false, "allow_training": false, "premium_reason": "pro only"},
  "spend": {"month": "2026-10", "spent": 0.0, "cap": 5.0},
  "providers": [
    {
      "platform": "groq",
      "name": "Groq",
      "key_set": true,
      "key_env": "GROQ_API_KEY",
      "models": [
        {
          "id": "groq/openai/gpt-oss-120b",
          "tiers": ["chat", "coding", "reasoning"],
          "free": true,
          "may_train_on_prompts": false,
          "allowed": true,
          "blocked_reason": null,
          "headroom": 0.82,
          "cooldown_remaining": 0,
          "reliability": 0.91,
          "limits": {"rpm": null, "rpd": null, "tpm": null, "tpd": null}
        }
      ]
    }
  ]
}
```

`allowed` is false when policy or a missing key excludes the model;
`blocked_reason` is then one of `"no key"`, `"may train on prompts"`, or for a paid model
the `premium_reason`: `"pro only"` (no Pro licence), `"premium off"` (Pro, but
`zhunt premium on` was not run) or `"monthly cap reached"`. `premium_reason` is null
while premium models are allowed. `spend` is this UTC month's premium spend (catalog
prices x tokens) against `ZHUNT_MONTHLY_CAP` (USD, default 5).
`cooldown_remaining` is seconds. Never include key values, only `key_set`.

## `GET /zhunt/api/decisions?limit=50&session=<id>`

Newest first, from an in-memory ring buffer of the last 200 requests.

```json
{
  "decisions": [
    {
      "ts": 1760000000.0,
      "session": "ui-7f3a",
      "requested_alias": "zhunt-auto",
      "tier": "coding",
      "model": "groq/openai/gpt-oss-120b",
      "degraded": false,
      "attempt": 1,
      "success": true,
      "failure": null,
      "input_tokens": 812,
      "output_tokens": 240,
      "latency_ms": 1432
    }
  ]
}
```

A failed attempt that fails over appears as its own entry (`success: false`,
`failure` set) followed by the next attempt with `attempt` incremented.

## `POST /zhunt/api/compare`

Runs one prompt on several chosen models at once (bypasses routing, still honours
keys, quota and the privacy/paid policy).

Request: `{"messages": [{"role": "user", "content": "..."}], "models": ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b"]}` — 1 to 4 model ids from the catalog.

Response (always 200 when the request itself is valid):

```json
{
  "results": [
    {"model": "groq/openai/gpt-oss-120b", "ok": true, "text": "...", "error": null,
     "latency_ms": 900, "input_tokens": 12, "output_tokens": 80},
    {"model": "cerebras/gpt-oss-120b", "ok": false, "text": null,
     "error": "blocked: may train on prompts", "latency_ms": null,
     "input_tokens": null, "output_tokens": null}
  ]
}
```

Errors: 400 for an empty/invalid body or more than 4 models, 404 for an unknown
model id (listing the offending ids).

## Layouts: `GET /zhunt/api/layouts`, `GET|PUT|DELETE /zhunt/api/layouts/{name}`

Layouts are plain JSON files in `~/.zhunt/layouts/<name>.json` (hand-editable).
`GET /layouts` → `{"layouts": ["default", "coding"]}`. `PUT` body is any JSON object
(the UI owns its schema), max 64 KB. `name` must match `^[a-z0-9][a-z0-9_-]{0,39}$`
(else 400). Missing layout → 404. Files are written atomically with mode 0600.

## Themes: `GET /zhunt/api/themes`, `GET /zhunt/api/themes/{name}`

Themes are CSS files of custom properties. Built-ins ship in `zhunt/workspace/css/`
(`themes/dark.css`, `themes/light.css`); users add `~/.zhunt/themes/<name>.css`.
`GET /themes` → `{"themes": [{"name": "dark", "builtin": true}, ...]}` (user theme
with the same name overrides the built-in). `GET /themes/{name}` → `text/css`. Every
theme is free on every plan.

## Plan: `GET /zhunt/api/plan`

```json
{"plan": "free", "email": null, "expires_at": null, "in_grace": false,
 "problem": null, "features": [], "feature_labels": {"premium_fallback": "..."},
 "upgrade_url": "https://zhunt.kindredwildcat.com/#pricing"}
```

Read from `~/.zhunt/licence` on every request (verified offline, see `zhunt/licence.py`),
so `zhunt licence activate` takes effect without a restart. `problem` explains why a stored
key is not in effect (expired, altered). Pro has `features: ["premium_fallback"]`.
Same name rule as layouts.

## Auth failures

Missing/wrong key → 401 `{"detail": "invalid Zhunt master key"}` (same as the
inference endpoints).

## Implementation notes (where the contract was silent)

Decided while building the backend; the code and tests treat these as the contract.

- `PUT /layouts/{name}`: body over 64 KB -> 413; body that is not a JSON object -> 400.
  `PUT` and `DELETE` return `{"name": name}`.
- `GET /themes`: a user theme that overrides a built-in is listed with `builtin: false`.
- `POST /compare`: repeated model ids are de-duplicated. 400/404 are returned before
  any model runs. Compare calls are not added to the decision log (they bypass routing).
  Output reserves 512 tokens against token quotas; provider timeout is 60 s.
- Compare error texts: `blocked: no key`, `blocked: may train on prompts`,
  `blocked: paid`, `blocked: over quota`, `blocked: cooling down for Ns`; a provider
  error is `<status>: <message>` with the key redacted, capped at 300 characters.
- Decision `failure` is one of `rate-limit`, `server`, `auth`, `credit`,
  `client-error`, `truncation`, `refusal`.
- Decision `session` is the `x-session-id` header, or `fallback:<hash>` without one.
  For streamed replies `latency_ms` is the time to the end of the stream.
- `GET /decisions?limit=` outside 1-200 -> 422 (FastAPI validation).
