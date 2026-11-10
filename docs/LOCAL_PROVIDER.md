# Local model provider and private chats

Status: first integration (LOO-192), November 2026. Not released.

Zhunt can send chats to a model running on this computer: a Zhunt Plus /
InferSSD server, or any OpenAI-style server on a loopback address. A chat marked
private goes only there.

## How it works

- **Settings** (in `~/.zhunt/env`, written by `zhunt local setup`):
  - `ZHUNT_LOCAL_URL`, e.g. `http://127.0.0.1:8080/v1`, or `ZHUNT_LOCAL_PORT`. The address must be 127.0.0.1, ::1 or localhost. Any other address is refused, and the daemon's start-up summary says so.
  - `ZHUNT_LOCAL_CONTEXT`: the context window in tokens. Default 1024.
  - `ZHUNT_LOCAL_MODEL`: the model name sent to the server. Default `local-model`.
  - `ZHUNT_LOCAL_TOOLS=1`: set this only if the server handles tool calls. Default off.
- **Where it lives in the code.** The local model is added to whichever catalog is active, in `zhunt/local_provider.py` and `engine.with_local`. It is added again when the signed catalog feed replaces the catalog. It is not in `catalog.yaml`: its address and model differ per machine, and a catalog file cannot mark a provider as local.
- **Marking a chat private.** Send the header `x-zhunt-private: 1`, or use the model alias `zhunt-local` or `zhunt-private`. This works on all three wire formats: Chat Completions, Responses and Anthropic Messages.
- **Privacy rule.** A private chat is only ever routed to the local model. In each of these cases the daemon answers 503 with a plain reason ending "nothing was sent to any cloud provider", and makes no provider call:
  - no local model is set up;
  - the local server does not answer a 2-second `GET /models`;
  - the chat is longer than the local context;
  - the chat needs tools the local model lacks.

  A failed local call is not retried on the cloud. It is also not benched, so a restarted server works at once. Calls to a loopback address ignore `HTTP_PROXY`/`HTTPS_PROXY`, so a private chat cannot pass through a configured proxy.
- **Scope.** Only the three chat endpoints honour the private mark: `/v1/chat/completions`, `/v1/responses` and `/v1/messages`. The workspace's compare, transcription and image features are separate actions the user starts, and they ignore it.
- **Ranking.** Ordinary chats try cloud models first, tier by tier as before (free before paid within a tier). The local model is used only when no cloud model on any tier can take the chat. This order is structural, not a score, so sampled reliability cannot put the slow local model ahead. A truncated local answer is not rerun.

## `zhunt local` commands

| Command | What it does |
|---|---|
| `zhunt local check` | Shows Apple Silicon, RAM and free disk. Finds complete `.gguf` models already on disk (whole files, or complete shard sets) and times random SSD reads of the model found. The timing reads with F_NOCACHE, is read-only and never writes a test file. Changes nothing. |
| `zhunt local setup --zhunt-plus PATH` | Five steps: hardware check, SSD timing, engine, model, server. **Engine:** clones the local InferSSD checkout (a folder, never a URL), checks out the pinned revision `368df4f`, checks and then applies patches 0001-0003 from the Zhunt Plus folder, and builds with Metal. It asks first. **Model:** picks the largest complete model found, or `--model`. **Server:** asks first, starts the Zhunt Plus launcher in the background, waits until it answers, then registers it in `~/.zhunt/env`. |
| `zhunt local start` / `stop` / `status` | Start or stop the server set up earlier, or show the registered address and whether it answers. |

Zhunt itself downloads nothing, and the model must already be on disk. The engine's CMake files do contain fetch steps (KleidiAI, BoringSSL/LibreSSL), but they sit behind options the Metal build script does not turn on. Without running the build, it is unverified that it makes no network requests. The defaults are port 8080 (InferSSD's), context 1024 and an 11 GiB engine budget.

## What was tested

- 777 tests pass with `uv run pytest`, including 31 for the provider, the privacy rule and the proxy bypass, and 13 for `zhunt local`.
- Every daemon test uses a faked provider call. The "server down" case uses a real connection to an unused loopback port.
- In a real run on the dev Mac (Apple Silicon, 24 GB), using `prepare_engine`'s own code with only the build commands stubbed:
  - the local InferSSD checkout was cloned into a scratch folder;
  - the clone was pinned to `368df4f`;
  - patches 0001, 0002 and 0003 applied cleanly in order (25 files changed).
- The real hardware check ran: Apple Silicon yes, 24 GiB RAM.

## Not tested (needs the owner's go-ahead)

- **Engine build.** The cmake/Metal build and `engine-resources.py` were not run. The commands are recorded in tests only.
- **Server and model.** The server was never started, no model was loaded, and no real chat went to a local model. Whether the launcher's OpenAI endpoint accepts exactly what the engine sends (model name, streaming, `GET /models` as the health check) is unverified.
- **SSD timing.** The timing was not run on a real model file, so as not to disturb other benchmarks on this machine. It ran on a temporary file only.
- **Phase 5 goal.** "First chat without terminal knowledge" is **not** met. Setup is a terminal command, and nothing in the workspace UI lets a user mark a chat private yet.
- **Other machines.** Linux, Windows and Intel Macs are not covered: setup refuses anything but Apple Silicon.

## Open owner decisions

1. **Web search source** for chats that need fresh information: DuckDuckGo (no key) or a Brave API key. This is deliberately not built. LOO-192 also asks that a private chat's search query be written by the local model as a short generic query, and shown to the user before it leaves the machine.
2. **Model licences.** No model is shipped or pointed to by Zhunt. The licence of any converted model, and its suitability for a paid product, is **unverified**. Check it before recommending a model.
3. **Port.** LOO-192 says 127.0.0.1:8000. InferSSD's default is 8080, and the Zhunt Plus chat proxy uses 7777. The default here is 8080 and any port can be set. Pick one for the product.
4. **Local as a fallback.** Ordinary chats may fall back to the slow local model when every cloud model is busy. Keep this, or use local only when the user picks it?
5. **Explicit choice without privacy.** The `zhunt-local` alias currently implies private: no cloud fallback. Is a separate "prefer local, but cloud is fine" option wanted?

## Follow-ups

- **Workspace UI.** Add a private-chat switch and a local-model status line. Show the user anything about to leave the machine (search queries) before it is sent.
- **Agent tools.** Private mode covers model calls only. The agent's browser and email tools reach the internet whatever the chat's mode.
- **Start at login.** A launchd agent to start the local server at login, and a packaged installer step for it.
