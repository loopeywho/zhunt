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

## Keys: `POST /zhunt/api/keys`, `DELETE /zhunt/api/keys/{platform}`

The Connect window. `POST {"platform": "groq", "key": "..."}` saves the key to
`~/.zhunt/env` (mode 0600) and this process's environment, then reloads keys so the
provider is usable at once; the response is `{"platform", "key_set": true}` and never
contains the key. 400 for a value that is not one printable line of 8-512 characters,
404 for an unknown provider. `DELETE` removes it. `/status` providers carry `key_url`
(where to get a key) and `free_note` (one plain-language line) from the catalog.

## Bots: `GET /zhunt/api/bots`, `PUT|DELETE /zhunt/api/bots/{handle}`

Team chat bots, one JSON file each in `~/.zhunt/bots/<handle>.json` (mode 0600, editable
by hand): `{"handle", "name" (1-40 chars), "instructions" (<= 4000), "alias" (a zhunt-*
tier alias), "color" ("#rrggbb")}`. A file that no longer parses is skipped by `GET`.
Orchestration runs in the workspace: each bot turn is an ordinary routed
`/v1/chat/completions` call with the bot's instructions and the team roster as the system
message; an @mention of a teammate in a reply queues that teammate, up to 6 bot turns per
user message.

## AI pictures: `GET /zhunt/api/images/status`, `PUT|DELETE /zhunt/api/images/connect`, `POST /zhunt/api/images`

Cloudflare Workers AI (FLUX-1-schnell) on the user's own free account: `PUT /images/connect
{"account_id", "token"}` saves `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` to
`~/.zhunt/env`. `POST /images {"prompt"}` -> `{"image": "data:image/jpeg;base64,..."}`; 409
when not connected, 429 when the free daily allowance is spent. Bot pictures:
`GET|PUT|DELETE /zhunt/api/bots/{handle}/avatar` with `{"image": data URL}` (JPEG, PNG or
WebP, at most 2 MB; SVG refused), stored next to the bot as `<handle>.jpg|png|webp`.

## Agent: `/zhunt/api/agent...`

The agent works through a task with tools, each model step being an ordinary routed
`/v1/chat/completions` call made in-process (so failover, quotas and privacy rules apply,
and only tool-capable models are chosen). Tools: `web_search` (DuckDuckGo HTML, no key),
`open_page` (public http(s) only; every redirect hop is checked and private, loopback and
link-local addresses are refused), `list_files`, `read_file` (read risk), `write_file`,
`move_file` (write risk: the task waits for approval). Files are confined to the folders in
`~/.zhunt/agent.json`.

- `GET /agent` -> `{folders, tools: [{name, risk, description}], tasks: [summary...], loops}`
- `PUT /agent/folders {"folders": [...]}` (existing folders only; never `/` or the whole home)
- `POST /agent/tasks {"goal", "mode": "task"|"goal", "tools"?: [...]}` -> task detail
- `GET /agent/tasks/{id}?since=n` -> `{id, goal, mode, state, answer, events[n:]}`; states
  `running | waiting | done | failed | stopped`; events `goal, thought, tool, approval,
  decision, answer, error, stopped`
- `POST /agent/tasks/{id}/decide {"call_id", "approve"}`, `POST /agent/tasks/{id}/stop`
- `POST /agent/loops {"every": "30m"|"2h"|"1d", "goal", ...}` (minimum 5 minutes, only
  while the daemon runs), `DELETE /agent/loops/{id}`

Limits: 12 model steps per task (24 in goal mode), approvals time out after 30 minutes.

Email (when connected): `list_emails`, `read_email` (read risk; IMAP `BODY.PEEK`, mailbox
opened read-only, so nothing is marked read), `draft_email` (write: IMAP APPEND to the
`\Drafts` special-use folder), `send_email` (send: always approved, full message shown).
`GET /email/status` -> `{connected, address, presets}` (never the password);
`PUT /email/connect {"provider": "gmail"|"icloud"|"yahoo", "address", "password"}` or
`{"imap": "host:port", "smtp": "host:port", ...}` logs in over IMAP first and saves
`ZHUNT_EMAIL_*` to `~/.zhunt/env` only on success; `DELETE /email/connect` removes them.

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
