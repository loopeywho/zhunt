#!/usr/bin/env python3
"""Send one real request through each tier of a running Zhunt daemon.

This is the Milestone 2 exit check: it proves, with your real provider keys, that
every tier answers and shows which model served it. Stdlib only; never prints keys.

    zhunt serve &                      # in another terminal, with keys set
    python scripts/live_tier_check.py  # exits 0 only if every tier answered
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIERS = ("zhunt-chat", "zhunt-coding", "zhunt-long-context", "zhunt-reasoning")


def master_key() -> str | None:
    if os.environ.get("ZHUNT_MASTER_KEY"):
        return os.environ["ZHUNT_MASTER_KEY"]
    env_file = Path.home() / ".zhunt" / "env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name == "ZHUNT_MASTER_KEY":
                return value.strip().strip('"')
    return None


def call(url: str, key: str, body: dict | None = None, headers: dict | None = None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(headers or {})},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {}
    except urllib.error.URLError as error:
        return 0, {"error": str(error.reason)}


def main() -> int:
    port = os.environ.get("ZHUNT_PORT", "4000")
    base = f"http://127.0.0.1:{port}"
    key = master_key()
    if not key:
        print("No ZHUNT_MASTER_KEY: run `zhunt serve` once to create it.")
        return 2
    status, _ = call(f"{base}/zhunt/api/status", key)
    if status != 200:
        print(f"Daemon not reachable or key rejected on {base} (HTTP {status}). Start `zhunt serve`.")
        return 2

    failures = 0
    print(f"{'tier':<20} {'result':<8} {'served by':<38} attempts  latency")
    for alias in TIERS:
        session = f"live-check-{alias}"
        http, _ = call(
            f"{base}/v1/chat/completions",
            key,
            {"model": alias, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with the single word: ok"}]},
            {"x-session-id": session},
        )
        _, log = call(f"{base}/zhunt/api/decisions?session={session}&limit=10", key)
        decisions = list(reversed(log.get("decisions", [])))
        final = decisions[-1] if decisions else {}
        ok = http == 200
        failures += not ok
        served = final.get("model") if ok else "-"
        latency = f"{final['latency_ms']} ms" if final.get("latency_ms") is not None else "-"
        print(f"{alias:<20} {'OK' if ok else 'FAIL':<8} {served or '-':<38} {len(decisions):<9} {latency}")
        for attempt in decisions:
            if not attempt.get("success"):
                print(f"    attempt {attempt.get('attempt')}: {attempt.get('model')} -> {attempt.get('failure')}")
    print("All tiers answered." if not failures else f"{failures} tier(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
