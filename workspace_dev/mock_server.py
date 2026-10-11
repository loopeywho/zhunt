#!/usr/bin/env python3
"""Dev-only mock of the Zhunt v2 daemon, for building/testing the workspace UI without the backend.

Implements docs/WORKSPACE_API.md with realistic fake data (stdlib only):

  GET  /workspace/...                   static files from zhunt/workspace/
  POST /v1/chat/completions             streaming SSE or JSON, scripted by keywords in the prompt:
                                          "failover"  attempt 1 fails (429) then attempt 2 succeeds
                                          "degraded"  served by a lower tier
                                          "nomodel"   503 no model available
                                          "xss"       reply containing HTML/script payloads (must render inert)
                                          "midstream" stream dies with an error after a few tokens
                                          "long"      long markdown reply with a code block
  GET  /zhunt/api/status | decisions    fake quota state (one provider with no key, one model in cooldown,
                                        one model blocked for training, one paid)
  POST /zhunt/api/compare               parallel fake results incl. a blocked and a rate-limited model
  *    /zhunt/api/layouts[/name]        backed by a temp dir
  GET  /zhunt/api/themes[/name]         built-ins from the workspace dir + a user theme from a temp dir
  POST /mock/control                    {"layouts_broken": bool, "status_down": bool, "plan": "free"|"pro"} (no auth), test hook

Usage: python3 workspace_dev/mock_server.py [--port 8765] [--key mockkey]
Then open http://127.0.0.1:8765/workspace/#key=mockkey
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = ROOT / "zhunt" / "workspace"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

STATE = {
    "key": "mockkey",
    "layouts_broken": False,
    "status_down": False,
    "layouts_dir": None,
    "themes_dir": None,
    "plan": "free",  # free | pro  (--plan, or POST /mock/control {"plan": "pro"})
}
PLAN_FEATURES = {"free": [], "pro": ["premium_fallback"]}
FEATURE_LABELS = {
    "premium_fallback": "Premium models (GPT, Claude, Gemini Pro) when free models run out, under your monthly cap",
}
LOCK = threading.Lock()
DECISIONS = []  # newest last; served newest first
COOLDOWNS = {"cerebras/gpt-oss-120b": time.time() + 47}

CATALOG = [
    {"platform": "groq", "name": "Groq", "key_set": True, "key_env": "GROQ_API_KEY", "key_url": "https://console.groq.com/keys", "free_note": "Free tier, no card: fast open models.", "models": [
        {"id": "groq/openai/gpt-oss-120b", "tiers": ["chat", "coding", "reasoning"], "headroom": 0.82, "reliability": 0.91},
        {"id": "groq/llama-3.3-70b-versatile", "tiers": ["chat", "coding"], "headroom": 0.45, "reliability": 0.88},
        {"id": "groq/qwen/qwen3-32b", "tiers": ["reasoning"], "headroom": 0.15, "reliability": 0.8},
    ]},
    {"platform": "cerebras", "name": "Cerebras", "key_set": False, "key_env": "CEREBRAS_API_KEY", "key_url": "https://cloud.cerebras.ai", "free_note": "Free tier, no card: very fast models.", "models": [
        {"id": "cerebras/gpt-oss-120b", "tiers": ["chat", "coding"], "headroom": 0.05, "reliability": 0.79},
        {"id": "cerebras/llama3.1-8b", "tiers": ["chat"], "headroom": 0.9, "reliability": 0.95},
    ]},
    {"platform": "google", "name": "Google AI Studio", "key_set": True, "key_env": "GEMINI_API_KEY", "models": [
        {"id": "google/gemini-2.5-flash", "tiers": ["chat", "long-context", "reasoning"], "headroom": 0.66, "reliability": 0.93},
        {"id": "google/gemini-2.5-pro", "tiers": ["long-context", "reasoning"], "headroom": 0.3, "reliability": 0.9,
         "may_train_on_prompts": True},
    ]},
    {"platform": "anthropic", "name": "Anthropic (premium)", "key_set": False, "key_env": "ANTHROPIC_API_KEY", "models": [
        {"id": "anthropic/claude-haiku-5-5", "tiers": ["chat", "coding"], "headroom": None, "reliability": None, "free": False},
        {"id": "anthropic/claude-sonnet-5-5", "tiers": ["chat", "coding", "long-context", "reasoning"], "headroom": None, "reliability": None, "free": False},
    ]},
    {"platform": "openai", "name": "OpenAI (premium)", "key_set": False, "key_env": "OPENAI_API_KEY", "models": [
        {"id": "openai/gpt-5.6-sol", "tiers": ["chat", "coding", "long-context", "reasoning"], "headroom": None, "reliability": None, "free": False},
    ]},
]


def model_view(provider, m):
    free = m.get("free", True)
    train = m.get("may_train_on_prompts", False)
    reason = None
    if not provider["key_set"]:
        reason = "no key"
    elif train:
        reason = "may train on prompts"
    elif not free:
        reason = "premium off" if STATE["plan"] == "pro" else "pro only"
    cd = max(0, int(COOLDOWNS.get(m["id"], 0) - time.time()))
    return {
        "id": m["id"], "tiers": m["tiers"], "free": free, "may_train_on_prompts": train,
        "allowed": reason is None, "blocked_reason": reason,
        "headroom": m["headroom"], "cooldown_remaining": cd, "reliability": m["reliability"],
        "limits": {"rpm": 30, "rpd": 1000, "tpm": None, "tpd": None} if provider["platform"] == "groq" else
                  {"rpm": None, "rpd": None, "tpm": None, "tpd": None},
    }


def status_payload():
    return {
        "version": "0.1.0.dev0-mock",
        "policy": {"allow_paid": False, "allow_training": False},
        "providers": [
            {"platform": p["platform"], "name": p["name"], "key_set": p["key_set"], "key_env": p["key_env"],
             "key_url": p.get("key_url", ""), "free_note": p.get("free_note", ""),
             "models": [model_view(p, m) for m in p["models"]]}
            for p in CATALOG
        ],
    }


def all_models():
    return {m["id"]: model_view(p, m) for p in CATALOG for m in p["models"]}


def usable(tier):
    return [m for m in all_models().values() if m["allowed"] and m["cooldown_remaining"] == 0 and tier in m["tiers"]]


def classify(alias, prompt):
    if alias != "zhunt-auto":
        return alias.replace("zhunt-", "")
    p = prompt.lower()
    if any(w in p for w in ("code", "function", "python", "bug", "regex")):
        return "coding"
    if any(w in p for w in ("reason", "prove", "why ", "step by step")):
        return "reasoning"
    if len(prompt) > 600 or "long" in p:
        return "long-context"
    return "chat"


def record(**kw):
    d = {"ts": time.time(), "session": None, "requested_alias": "zhunt-auto", "tier": "chat", "model": None, "degraded": False,
         "attempt": 1, "success": True, "failure": None, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
    d.update(kw)
    with LOCK:
        DECISIONS.append(d)
        del DECISIONS[:-200]


def words_for(prompt, model, tier):
    p = prompt.lower()
    if "xss" in p:
        return ('Here is a **test** payload: <img src=x onerror="document.title=\'pwned\'"> and <script>document.title=\'pwned\'</script> '
                'plus [a link](https://example.com) and `<b>code</b>`.\n\n```html\n<img src=x onerror=alert(1)>\n```\n').split(" ")
    if "long" in p:
        txt = ("## A long answer\n\nSome **bold** text, some *italic* text and `inline code`.\n\n- first point\n- second point\n- third point\n\n"
               "```python\ndef fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n```\n\n" +
               ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 12) + "\n")
        return txt.split(" ")
    snippet = prompt.strip().replace("\n", " ")[:80]
    return (f"This is a mock reply from {model} (tier {tier}). You said: \"{snippet}\". "
            "Every tile in the workspace talks to this fake daemon, so nothing here is real model output.").split(" ")


class Handler(BaseHTTPRequestHandler):
    server_version = "ZhuntMock/0"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- plumbing ----
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode() or "null")

    def _authed(self):
        if self.headers.get("Authorization") == "Bearer " + STATE["key"]:
            return True
        self._send(401, {"detail": "invalid Zhunt master key"})
        return False

    def _static(self, path):
        rel = unquote(path[len("/workspace/"):]) or "index.html"
        target = (WORKSPACE_DIR / rel).resolve()
        if WORKSPACE_DIR not in target.parents and target != WORKSPACE_DIR / "index.html":
            return self._send(404, {"detail": "not found"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._send(404, {"detail": "not found"})
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".json": "application/json", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/workspace"):
            self.send_response(302)
            self.send_header("Location", "/workspace/")
            self.end_headers()
            return
        if u.path.startswith("/workspace/"):
            return self._static(u.path)
        if u.path.startswith("/zhunt/api/") and not self._authed():
            return
        if u.path == "/zhunt/api/status":
            if STATE["status_down"]:
                return self._send(503, {"detail": "mock: status down"})
            return self._send(200, status_payload())
        if u.path == "/zhunt/api/decisions":
            q = parse_qs(u.query)
            limit = int(q.get("limit", ["50"])[0])
            sess = q.get("session", [None])[0]
            with LOCK:
                rows = [d for d in reversed(DECISIONS) if not sess or d["session"] == sess][:limit]
            return self._send(200, {"decisions": rows})
        if u.path == "/zhunt/api/layouts":
            return self._layouts("list", None)
        if u.path.startswith("/zhunt/api/layouts/"):
            return self._layouts("get", u.path.rsplit("/", 1)[1])
        if u.path == "/zhunt/api/plan":
            pro = STATE["plan"] == "pro"
            return self._send(200, {
                "plan": STATE["plan"], "email": "you@example.com" if pro else None,
                "expires_at": time.time() + 20 * 86400 if pro else None, "in_grace": False, "problem": None,
                "features": PLAN_FEATURES[STATE["plan"]], "feature_labels": FEATURE_LABELS,
                "upgrade_url": "https://zhunt.kindredwildcat.com/#pricing",
            })
        if u.path == "/zhunt/api/themes":
            return self._themes_list()
        if u.path.startswith("/zhunt/api/themes/"):
            return self._theme_css(u.path.rsplit("/", 1)[1])
        return self._send(404, {"detail": "not found"})

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path.startswith("/zhunt/api/layouts/") and self._authed():
            return self._layouts("put", u.path.rsplit("/", 1)[1])
        self._send(404, {"detail": "not found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path.startswith("/zhunt/api/layouts/") and self._authed():
            return self._layouts("delete", u.path.rsplit("/", 1)[1])
        if u.path.startswith("/zhunt/api/keys/") and self._authed():
            prov = next((p for p in CATALOG if p["platform"] == u.path.rsplit("/", 1)[1]), None)
            if prov is None:
                return self._send(404, {"detail": "unknown provider"})
            prov["key_set"] = False
            return self._send(200, {"platform": prov["platform"], "key_set": False})
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/mock/control":
            STATE.update({k: v for k, v in (self._json_body() or {}).items() if k in ("layouts_broken", "status_down", "plan")})
            return self._send(200, {"ok": True, **{k: STATE[k] for k in ("layouts_broken", "status_down", "plan")}})
        if not self._authed():
            return
        if u.path == "/v1/chat/completions":
            return self._chat()
        if u.path == "/zhunt/api/compare":
            return self._compare()
        if u.path == "/zhunt/api/transcribe":
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            return self._send(200, {"text": "Write a regex for UK postcodes"})
        if u.path == "/zhunt/api/keys":
            body = self._json_body() or {}
            prov = next((p for p in CATALOG if p["platform"] == body.get("platform")), None)
            if prov is None:
                return self._send(404, {"detail": "unknown provider"})
            if not isinstance(body.get("key"), str) or len(body["key"].strip()) < 8 or " " in body["key"].strip():
                return self._send(400, {"detail": "that does not look like an API key (one line, no spaces)"})
            prov["key_set"] = True  # the mock never stores the value
            return self._send(200, {"platform": prov["platform"], "key_set": True})
        self._send(404, {"detail": "not found"})

    # ---- layouts / themes ----
    def _layouts(self, op, name):
        if STATE["layouts_broken"]:
            return self._send(500, {"detail": "mock: layouts storage unavailable"})
        d = Path(STATE["layouts_dir"])
        if op == "list":
            return self._send(200, {"layouts": sorted(p.stem for p in d.glob("*.json"))})
        if not NAME_RE.match(name or ""):
            return self._send(400, {"detail": "invalid layout name"})
        f = d / (name + ".json")
        if op == "get":
            return self._send(200, f.read_bytes()) if f.is_file() else self._send(404, {"detail": "layout not found"})
        if op == "delete":
            if not f.is_file():
                return self._send(404, {"detail": "layout not found"})
            f.unlink()
            return self._send(200, {"deleted": name})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return self._send(400, {"detail": "layout too large (max 64 KB)"})
        try:
            body = json.loads(self.rfile.read(n).decode())
        except ValueError:
            return self._send(400, {"detail": "body must be JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"detail": "body must be a JSON object"})
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2))
        tmp.replace(f)
        return self._send(200, {"saved": name})

    def _themes_list(self):
        names = {p.stem: True for p in (WORKSPACE_DIR / "css" / "themes").glob("*.css")}
        for p in Path(STATE["themes_dir"]).glob("*.css"):
            names[p.stem] = False  # a user theme replaces the built-in of the same name
        self._send(200, {"themes": [{"name": n, "builtin": b} for n, b in sorted(names.items())]})

    def _theme_css(self, name):
        if not NAME_RE.match(name):
            return self._send(400, {"detail": "invalid theme name"})
        for base in (Path(STATE["themes_dir"]), WORKSPACE_DIR / "css" / "themes"):
            f = base / (name + ".css")
            if f.is_file():
                return self._send(200, f.read_bytes(), "text/css; charset=utf-8")
        self._send(404, {"detail": "theme not found"})

    # ---- compare ----
    def _compare(self):
        try:
            body = self._json_body()
        except ValueError:
            return self._send(400, {"detail": "invalid JSON"})
        msgs = (body or {}).get("messages")
        models = (body or {}).get("models")
        if not isinstance(msgs, list) or not msgs or not isinstance(models, list) or not 1 <= len(models) <= 4:
            return self._send(400, {"detail": "need messages and 1-4 models"})
        known = all_models()
        unknown = [m for m in models if m not in known]
        if unknown:
            return self._send(404, {"detail": "unknown model id(s): " + ", ".join(unknown)})
        prompt = str(msgs[-1].get("content", ""))

        def one(mid):
            v = known[mid]
            if not v["allowed"]:
                return {"model": mid, "ok": False, "text": None, "error": "blocked: " + v["blocked_reason"],
                        "latency_ms": None, "input_tokens": None, "output_tokens": None}
            if v["cooldown_remaining"] > 0:
                return {"model": mid, "ok": False, "text": None, "error": f"429 rate limited (cooldown {v['cooldown_remaining']}s)",
                        "latency_ms": 180, "input_tokens": None, "output_tokens": None}
            delay = 0.4 + (hash(mid) % 900) / 1000.0
            time.sleep(delay)
            text = " ".join(words_for(prompt, mid, "compare"))
            return {"model": mid, "ok": True, "text": text, "error": None, "latency_ms": int(delay * 1000),
                    "input_tokens": max(1, len(prompt) // 4), "output_tokens": len(text) // 4}

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(one, models))
        self._send(200, {"results": results})

    # ---- chat ----
    def _chat(self):
        try:
            body = self._json_body() or {}
        except ValueError:
            return self._send(400, {"detail": "invalid JSON"})
        alias = body.get("model", "zhunt-auto")
        msgs = body.get("messages") or []
        prompt = str(msgs[-1].get("content", "")) if msgs else ""
        session = self.headers.get("x-session-id")
        p = prompt.lower()
        tier = classify(alias, prompt)
        in_tok = max(1, sum(len(str(m.get("content", ""))) for m in msgs) // 4)

        if "nomodel" in p:
            record(session=session, requested_alias=alias, tier=tier, model=None, success=False,
                   failure="503 no model available", input_tokens=in_tok)
            return self._send(503, {"detail": "no model available: every free-tier candidate is exhausted, cooling down or blocked"})

        t0 = time.time()
        attempt = 1
        if "failover" in p:
            record(session=session, requested_alias=alias, tier=tier, model="cerebras/gpt-oss-120b", success=False,
                   failure="429 rate limited", input_tokens=in_tok, latency_ms=210, attempt=1)
            COOLDOWNS["cerebras/gpt-oss-120b"] = time.time() + 60
            attempt = 2
        degraded = "degraded" in p
        pool = usable("chat" if degraded else tier) or usable("chat")
        if not pool:
            return self._send(503, {"detail": "no model available"})
        model = pool[0]["id"]

        words = words_for(prompt, model, tier)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            slow = 0.25 if "slow" in p else 0.03
            sent = 0
            try:
                for i, w in enumerate(words):
                    if "midstream" in p and i == 6:
                        err = {"error": {"message": "upstream connection reset"}}
                        self.wfile.write(("data: " + json.dumps(err) + "\n\n").encode())
                        self.wfile.flush()
                        record(session=session, requested_alias=alias, tier=tier, model=model, success=False,
                               failure="stream reset", input_tokens=in_tok, attempt=attempt, latency_ms=int((time.time() - t0) * 1000))
                        return
                    chunk = {"id": "mock", "object": "chat.completion.chunk", "model": model,
                             "choices": [{"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")}}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                    sent += 1
                    time.sleep(slow)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            record(session=session, requested_alias=alias, tier=tier, model=model, degraded=degraded, attempt=attempt,
                   input_tokens=in_tok, output_tokens=sent * 2, latency_ms=int((time.time() - t0) * 1000))
            return
        text = " ".join(words)
        record(session=session, requested_alias=alias, tier=tier, model=model, degraded=degraded, attempt=attempt,
               input_tokens=in_tok, output_tokens=len(text) // 4, latency_ms=int((time.time() - t0) * 1000))
        self._send(200, {"id": "mock", "object": "chat.completion", "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})


def seed_history():
    now = time.time()
    rows = [
        ("ui-seed1", "zhunt-auto", "coding", "groq/openai/gpt-oss-120b", 1, True, None, False, 812, 240, 1432, -300),
        ("ui-seed1", "zhunt-auto", "chat", "cerebras/gpt-oss-120b", 1, False, "429 rate limited", False, 90, 0, 210, -200),
        ("ui-seed1", "zhunt-auto", "chat", "groq/llama-3.3-70b-versatile", 2, True, None, False, 90, 64, 880, -199),
        ("ui-seed2", "zhunt-reasoning", "reasoning", "google/gemini-2.5-flash", 1, True, None, True, 400, 700, 4310, -60),
    ]
    for sess, alias, tier, model, att, ok, fail, deg, i, o, ms, off in rows:
        record(ts=now + off, session=sess, requested_alias=alias, tier=tier, model=model, attempt=att, success=ok,
               failure=fail, degraded=deg, input_tokens=i, output_tokens=o, latency_ms=ms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--key", default="mockkey")
    ap.add_argument("--plan", choices=["free", "pro"], default="free")
    args = ap.parse_args()
    STATE["key"] = args.key
    STATE["plan"] = args.plan
    tmp = Path(tempfile.mkdtemp(prefix="zhunt-mock-"))
    (tmp / "layouts").mkdir()
    (tmp / "themes").mkdir()
    STATE["layouts_dir"] = str(tmp / "layouts")
    STATE["themes_dir"] = str(tmp / "themes")
    (tmp / "themes" / "solarized-night.css").write_text(
        ':root[data-theme="solarized-night"] {\n  color-scheme: dark;\n  --bg: #00212b; --surface: #002b36; --surface-2: #073642; --surface-3: #0d4452;\n'
        '  --border: #0f4a59; --border-strong: #2b6372; --text: #eee8d5; --text-dim: #a4b3b3; --text-faint: #93a1a1;\n'
        '  --accent: #b58900; --accent-fg: #00212b; --accent-soft: rgba(181,137,0,.18); --focus: #cb4b16;\n'
        '  --ok: #859900; --warn: #b58900; --err: #dc322f; --info: #2aa198;\n'
        '  --tier-chat: #2aa198; --tier-coding: #859900; --tier-long-context: #6c71c4; --tier-reasoning: #d33682;\n'
        '  --selection: rgba(181,137,0,.3); --scrim: rgba(0,10,14,.65); --shadow: 0 18px 50px rgba(0,0,0,.6);\n}\n')
    seed_history()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock zhunt daemon on http://127.0.0.1:{args.port}/workspace/#key={args.key}   (data dir {tmp})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
