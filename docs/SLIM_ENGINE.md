# Slim engine: zhunt without the LiteLLM proxy

zhunt used `litellm[proxy]` as its HTTP server and provider client. That one
dependency made the install ~450 MB (polars, numpy, botocore, the proxy UI...)
and the daemon ~300 MB of RAM, for a job that needs a few hundred lines: every
provider zhunt routes to speaks OpenAI Chat Completions (Gemini through its
OpenAI-compatible endpoint), except Anthropic, which has its own Messages API.

This note lists every LiteLLM touchpoint and what replaced it. Behaviour is
meant to be identical; the deliberate differences are listed at the end.

## Touchpoints and replacements

| LiteLLM touchpoint | Used for | Replacement |
| --- | --- | --- |
| `litellm.proxy.proxy_server` app, `chat_completion`, `responses_api`, `anthropic_response`, `count_tokens` routes | the authenticated `/v1/*` surface | `zhunt/server.py`: four FastAPI routes of zhunt's own |
| `proxy_server.user_api_key_auth` override, `UserAPIKeyAuth` | master-key auth | `_authenticate_local_request` returns the token; same headers (`Authorization: Bearer`, `x-api-key`, `x-litellm-api-key`), same 401 |
| `proxy_server.master_key` / `save_worker_config(drop_params=True)` | key and parameter dropping | `app.state.zhunt_master_key`; the client forwards only an allow-list of standard parameters |
| proxy exception handlers | error bodies | `{"error": {"message", "type", "param", "code"}}`, same status codes |
| `CustomLogger` hook lifecycle (`async_pre_call_hook`, `async_post_call_success_hook`, `async_post_call_failure_hook`, `async_post_call_streaming_iterator_hook`) | routing, failover, telemetry, decisions | `EngineProxyHook` keeps the same methods; `server.py` calls them in the same order LiteLLM did |
| `proxy_server_request`, `litellm_call_id` in the request data | original body/headers, pending-call key | the server builds the same two keys (names kept so the hook is unchanged) |
| streaming: first chunk fetched before the 200, `data: ...` SSE, `data: [DONE]`, `{"error": ...}` mid-stream, `model` rewritten to the requested alias | client-visible stream | reproduced in `server.py` |
| `litellm.acompletion` (JSON and stream) | chat calls, compare | `zhunt.llm.acompletion`: httpx, OpenAI Chat Completions; Anthropic Messages for the `anthropic/` route |
| `litellm.aresponses` | `/v1/responses` | `zhunt.llm.aresponses`: Responses -> Chat Completions -> Responses (JSON and SSE events) in `zhunt/translate.py` |
| `litellm.anthropic_messages` | `/v1/messages` | `zhunt.llm.anthropic_messages`: native pass-through to Anthropic; Messages <-> Chat Completions for every other provider |
| `litellm.atranscription` | voice input (Groq Whisper) | `zhunt.llm.atranscription`: one multipart POST |
| `RateLimitError`, `BadRequestError`, ... (`status_code`, `message`, `litellm_response_headers`) | failure classification, cooldowns, learned limits | `zhunt.llm.APIError` and the same subclass names; `status_code`, `message`, `response_headers` (real provider headers); `str(error)` is the provider's raw error body, as before |
| `response._hidden_params["additional_headers"]` (`llm_provider-` prefixed) | learning rate limits from success headers | `ModelResponse.headers`: the raw provider headers on every response and stream chunk |
| `ModelResponse`, `Usage`, cost tracking | usage tokens | plain dicts (`usage.prompt_tokens`/`completion_tokens`); zhunt already priced calls from its own catalog |
| `auth_utils._BANNED_REQUEST_BODY_PARAMS` | scrubbing hostile request fields | the endpoint/credential/observability names copied into `engine._LITELLM_BANNED_FIELDS`; the client allow-list is a second fence |
| provider routing by prefix (`gemini/`, `groq/`, `cerebras/`, `openai/` + `api_base`) | where to send a call | `zhunt.llm._ROUTES`: the same prefixes mapped to base URLs; the catalog's `litellm_provider` field keeps its name and meaning |
| `LITELLM_LOCAL_MODEL_COST_MAP` | stop LiteLLM phoning home on import | gone with LiteLLM: nothing is fetched on import |
| PyInstaller `--collect-all litellm` / `tiktoken` | desktop bundles | removed |

## The client (`zhunt/llm.py`)

* One shared `httpx.AsyncClient` per event loop, 600 s timeout (LiteLLM's default).
* Routes: `openai` (api.openai.com or the catalog's `api_base`), `groq`,
  `cerebras`, `gemini` (`generativelanguage.googleapis.com/v1beta/openai`) speak
  Chat Completions; `anthropic` speaks Messages (`x-api-key`,
  `anthropic-version: 2023-06-01`).
* Parameters: an allow-list of standard Chat Completions fields. zhunt's own
  `metadata`, `proxy_server_request` and `litellm_call_id` never leave the machine.
  `max_completion_tokens` becomes `max_tokens` for providers other than OpenAI
  (and the reverse for OpenAI), as LiteLLM did.
* Errors: a non-2xx becomes the `APIError` subclass for its status; connection
  failures and timeouts become `APIConnectionError` (500) and `Timeout` (408), which
  the engine treats as server failures, as before.

## The server (`zhunt/server.py`)

For each of `/v1/chat/completions`, `/v1/responses`, `/v1/messages`:

1. authenticate; parse the JSON body (400 if it is not an object);
2. build `data` = body + `proxy_server_request` + `litellm_call_id`;
3. `hook.async_pre_call_hook` (an `HTTPException` becomes the error body);
4. call `llm.acompletion` / `llm.aresponses` / `llm.anthropic_messages`;
5. JSON: `async_post_call_success_hook`, then the body. Stream: wrap in
   `async_post_call_streaming_iterator_hook`, pull the first chunk before
   answering 200 so an immediate provider error keeps its status;
6. on an exception, `async_post_call_failure_hook` and the error body.

`RetryMiddleware` (replay on failover) is unchanged. `/v1/messages/count_tokens`
returns zhunt's usual estimate (characters / 4).

## Deliberate differences

* Response headers no longer include `x-litellm-*`.
* `/v1/messages` errors no longer prefix the message with the status code.
* Unsupported request fields are dropped by an allow-list instead of
  LiteLLM's per-provider tables. Non-function tools (web search, computer use)
  sent to `/v1/responses` or `/v1/messages` are dropped for non-Anthropic
  providers, as no free provider runs them.

## Measurements

`uv tool install` of the branch into an empty tool dir, Python 3.14, macOS
(Apple silicon); start-up is spawn to first HTTP answer, warm (second and third
runs); RSS after 5 s idle.

| | install | start-up | idle RSS |
| --- | --- | --- | --- |
| before (`litellm[proxy]==1.94.0`) | 444 MB | 3.3 s (13 s first run) | 325 MB |
| after | 35 MB | 0.65 s (2.8 s first run) | 84 MB |

The first run compiles bytecode. Of the 84 MB, a bare Python is about 15 MB and
importing FastAPI (with pydantic) about 36 MB more; dropping FastAPI too would be
the next step down, and was out of scope. The 35 MB install is FastAPI, pydantic,
uvicorn, httpx, cryptography (licence checks), typer and the YAML/TOML parsers.
