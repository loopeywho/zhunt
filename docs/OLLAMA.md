# Ollama API

Status: first version (LOO-204), November 2026. Not released. Covered by tests with faked provider calls only; no real Ollama client or real model has been run against it yet.

Zhunt answers the Ollama API on the daemon's own port (default 4000), next to the Anthropic Messages, OpenAI Responses and OpenAI Chat Completions formats. An Ollama client needs one change: point it at Zhunt.

| Was | Now |
|---|---|
| `http://localhost:11434` | `http://localhost:4000` |

For example `ollama.Client(host="http://localhost:4000")`, or `OLLAMA_HOST=http://localhost:4000`. Start the daemon first (`zhunt serve`).

Each request is translated to Chat Completions and sent down the same path as every other chat, so it is routed to the best available model, fails over between providers, is recorded in telemetry and obeys the privacy rule (see `LOCAL_PROVIDER.md`).

## Supported

| Endpoint | Notes |
|---|---|
| `GET /api/tags` | The names Zhunt can route to (below), as `name:latest`. `size` and `digest` are empty. |
| `GET /api/version` | A fixed Zhunt version string. |
| `POST /api/chat` | `model`, `messages` (`role`, `content`, `images`), `stream`, `format:"json"`, `options`. |
| `POST /api/generate` | `model`, `prompt`, `system`, `images`, `stream`, `format:"json"`, `options`. |

Like Ollama, both POST routes stream newline-delimited JSON unless the request says `"stream": false`. An empty `prompt` or empty `messages` returns a `done` object with `done_reason: "load"` and calls no provider. Errors are `{"error": "..."}` with the provider's status code (400 for a bad request, 503 when no model can take the chat).

Options honoured: `temperature`, `top_p`, `num_predict` (to `max_tokens`; -1 and -2 mean no limit and are ignored), `stop` (a string or a list), and `format:"json"` (to a JSON-object response format). `think` is accepted and has no effect. Everything else (`num_ctx`, `seed`, `top_k`, `keep_alive`, `template`, `raw`, `context`, a schema object in `format`) is ignored. A request with `tools` is a 400: use `/v1/chat/completions` for tool calls. If the provider returns reasoning text it comes back as `thinking`.

Timings in the answer are wall clock only: `total_duration` and `eval_duration` are the same number and `load_duration` and `prompt_eval_duration` are 0. Token counts are the provider's own when it reports them, otherwise a rough count. Do not use these for speed measurements.

## Model names are not honoured

Clients send names such as `llama3.2:3b` or `qwen3:8b`. Zhunt does not run those models. Any name it does not know is treated as `zhunt-auto`: normal routing picks the model, and the answer echoes the name you sent. The `x-zhunt-model` response header and `/zhunt/api/decisions` say which model actually answered.

Names Zhunt does know keep their meaning, with or without a `:latest` suffix: `zhunt-auto`, the tier aliases (`zhunt-chat`, `zhunt-coding`, `zhunt-long-context`, `zhunt-reasoning`), and the private aliases `zhunt-local` and `zhunt-private`, which mean local only, no cloud, exactly as on the other routes (503 "...nothing was sent to any cloud provider" when the local model cannot take the chat). The `x-zhunt-private: 1` header works too. `/api/tags` lists the routable names; the private ones appear only when a local model is set up.

## Authentication

Ollama clients send no key, so these routes accept a request without one when it plainly comes from this computer and not from a web page. All of these must hold:

- the connection comes from a loopback address;
- the `Host` header is `localhost`, `127.0.0.1` or `[::1]` (blocks DNS rebinding);
- there is no `Origin` header (browsers add it to cross-site requests);
- a POST has `Content-Type: application/json` (a web page cannot send that to another origin without a CORS preflight, which the daemon never grants). Plain `curl -d` needs `-H 'Content-Type: application/json'`.

Anything else needs the master key as `Authorization: Bearer <key>`. Set `ZHUNT_OLLAMA_REQUIRE_KEY=1` to require the key on the Ollama routes always. The `/v1` routes, the workspace API and everything else are unchanged and always need the key.

Risk: any program running as any user on this computer can use these routes without a key, and so spend free-tier quota and reach the local model. That is the same trust the loopback Ollama port gives. If other users share the computer, set `ZHUNT_OLLAMA_REQUIRE_KEY=1`. With `--allow-non-loopback`, other machines still need the key.

## Not supported

- `501` with `{"error": "Zhunt does not support <path> yet"}`: `/api/embeddings`, `/api/embed`, `/api/pull`, `/api/push`, `/api/show`, `/api/create`, `/api/copy`, `DELETE /api/delete`, `GET /api/ps`. Embeddings stay on Ollama for now.
- Other `/api/*` paths: 404.
- Images are forwarded to the routed model as image parts, not dropped. Zhunt's catalog has no vision flag, so routing does not yet prefer a vision-capable model: an image sent to a model without vision may be rejected or ignored by that provider. Which catalog models accept images is unverified. Scripts that classify images should pin a tier or model known to take them, or stay on Ollama until the catalog marks vision.
- Telemetry records these requests as OpenAI Chat Completions requests (the translated form), not as a separate Ollama dialect.
