# Ollama API

Status: first version (LOO-204), November 2026. Not released. Covered by tests with faked provider calls only; no real Ollama client or real model has been run against it yet.

Zhunt answers the Ollama API on the daemon's own port (default 4000), next to the Anthropic Messages, OpenAI Responses and OpenAI Chat Completions formats. An Ollama client needs one change: point it at Zhunt.

| Was | Now |
|---|---|
| `http://localhost:11434` | `http://localhost:4000` |

For example `ollama.Client(host="http://localhost:4000")`, or `OLLAMA_HOST=http://localhost:4000`. Start the daemon first (`zhunt serve`).

Each request is translated to Chat Completions and sent down the same path as every other chat, so it is routed (to the local model unless you opt in to the cloud), can fail over between providers when it does use the cloud, is recorded in telemetry and obeys the privacy rule (see `LOCAL_PROVIDER.md`).

## Supported

| Endpoint | Notes |
|---|---|
| `GET /api/tags` | The local aliases first, then the cloud opt-in aliases (below), as `name:latest`. `size` and `digest` are empty. |
| `GET /api/version` | A fixed Zhunt version string. |
| `POST /api/chat` | `model`, `messages` (`role`, `content`, `images`), `stream`, `format:"json"`, `options`. |
| `POST /api/generate` | `model`, `prompt`, `system`, `images`, `stream`, `format:"json"`, `options`. |

Like Ollama, both POST routes stream newline-delimited JSON unless the request says `"stream": false`. An empty `prompt` or empty `messages` returns a `done` object with `done_reason: "load"` and calls no provider. Errors are `{"error": "..."}` with the provider's status code (400 for a bad request, 503 when the local model cannot take the chat).

Options honoured: `temperature`, `top_p`, `num_predict` (to `max_tokens`; -1 and -2 mean no limit and are ignored), `stop` (a string or a list), and `format:"json"` (to a JSON-object response format). `think` is accepted and has no effect. Everything else (`num_ctx`, `seed`, `top_k`, `keep_alive`, `template`, `raw`, `context`, a schema object in `format`) is ignored. A request with `tools` is a 400: use `/v1/chat/completions` for tool calls. If the provider returns reasoning text it comes back as `thinking`.

Timings in the answer are wall clock only: `total_duration` and `eval_duration` are the same number and `load_duration` and `prompt_eval_duration` are 0. Token counts are the provider's own when it reports them, otherwise a rough count. Do not use these for speed measurements.

## Private by default; cloud is an opt-in

Ollama requests are **local only by default**. Every request on `/api/chat` and `/api/generate` is routed only to the local model (see `LOCAL_PROVIDER.md`), under the same privacy rule as `x-zhunt-private: 1`: when the local model is not set up, not answering, or the chat does not fit its context, the answer is a 503 ending "nothing was sent to any cloud provider", and no cloud provider is called. There is no cloud fallback and no daemon-wide switch that makes cloud the default.

Clients send names such as `llama3.2:3b` or `qwen3:8b`. Zhunt does not run those models: **the name is not honoured**. An unknown name means `zhunt-local`, i.e. whatever model is set up locally, and the answer echoes the name you sent. The `x-zhunt-model` response header and `/zhunt/api/decisions` say which model actually answered.

To use the cloud, opt in on that request, either way:

- **Name a Zhunt routing alias** as the model: `zhunt-auto` (best available model) or a tier alias (`zhunt-chat`, `zhunt-coding`, `zhunt-long-context`, `zhunt-reasoning`), with or without `:latest`. Normal routing, failover and telemetry then apply.
- **Or send the header `x-zhunt-private: 0`**: any model name is then treated as `zhunt-auto`. This suits a client whose model name you cannot change but whose headers you can.

`zhunt-local` and `zhunt-private` always mean local only, and `x-zhunt-private: 1` always wins over a cloud alias.

`/api/tags` lists the local aliases first when a local model is set up (`zhunt-local`, `zhunt-private`), then the cloud opt-in aliases, each as `name:latest`.

## Named local models (Modelfile style)

Ollama lets you build a named model from a base model plus a system prompt and settings, and scripts ask for it by name (for example `translator`). Zhunt does the same with a profile file, `~/.zhunt/local_models.json` (or the file named by `ZHUNT_LOCAL_MODELS`), kept outside the repo because system prompts are personal:

```json
{"translator": {"url": "http://127.0.0.1:8082/v1", "model": "qwen3-8b",
              "system": "You are ...",
              "options": {"temperature": 0.7, "top_k": 20, "top_p": 0.95, "stop": ["<|im_end|>"]}}}
```

- A request on `/api/chat` or `/api/generate` that names a profile (`translator` or `translator:latest`) goes straight to that server, with the profile's system prompt and options. A system message or option in the request wins, as with Ollama. `x-zhunt-model` is `local/<name>`.
- A profile is a model on this computer: the request is never sent to the cloud, and if its server is not answering the answer is a 503 ending "nothing was sent to any cloud provider". The `url` must be on this computer.
- Names without a profile keep the rules above.
- These requests do not go through Zhunt's router, so they are not yet in telemetry.

Checked 11 November 2026: A custom Ollama model (Qwen3 8B Q4_K_M with a system prompt) rebuilt as a profile on a local llama.cpp server scored 0.907 on a Chinese-to-English subtitle translation benchmark (27 segments) against Ollama's 0.915, at about the same speed; 16 of 27 translations were identical and the rest differed in wording.

## Authentication

Ollama clients send no key, so these routes accept a request without one when it plainly comes from this computer and not from a web page. All of these must hold:

- the connection comes from a loopback address;
- the `Host` header is `localhost`, `127.0.0.1` or `[::1]` (blocks DNS rebinding);
- there is no `Origin` header (browsers add it to cross-site requests);
- a POST has `Content-Type: application/json` (a web page cannot send that to another origin without a CORS preflight, which the daemon never grants). Plain `curl -d` needs `-H 'Content-Type: application/json'`.

Anything else needs the master key as `Authorization: Bearer <key>`. Set `ZHUNT_OLLAMA_REQUIRE_KEY=1` to require the key on the Ollama routes always. The `/v1` routes, the workspace API and everything else are unchanged and always need the key.

Risk: any program running as any user on this computer can use these routes without a key, and so spend free-tier quota and reach the local model. That is the same trust the loopback Ollama port gives. If other users share the computer, set `ZHUNT_OLLAMA_REQUIRE_KEY=1`. With `--allow-non-loopback`, other machines still need the key.

## Not supported

- `501` with `{"error": "Zhunt does not support <path> yet"}`: `/api/pull`, `/api/push`, `/api/show`, `/api/create`, `/api/copy`, `DELETE /api/delete`, `GET /api/ps`. Embeddings: see `EMBEDDINGS.md`.
- Other `/api/*` paths: 404.
- Images are forwarded to the routed model as image parts, not dropped. Zhunt's catalog has no vision flag, so routing does not yet prefer a vision-capable model: an image sent to a model without vision may be rejected or ignored by that provider. Which catalog models accept images is unverified. Scripts that classify images should pin a tier or model known to take them, or stay on Ollama until the catalog marks vision.
- Telemetry records these requests as OpenAI Chat Completions requests (the translated form), not as a separate Ollama dialect.
