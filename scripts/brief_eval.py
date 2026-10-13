#!/usr/bin/env python3
"""Score brief extraction against mail you labelled yourself, through a running zhunt daemon.

Keep the labels OUTSIDE the repo (they are your real mail). One JSON object per line:

    {"from": "Sam <sam@example.com>", "to": "me@example.com", "subject": "Deck?",
     "date": "Tue, 13 Oct 2026 07:00:00 +0000", "snippet": "Can you send the deck by Friday?",
     "expected": {"action": "reply", "due": "2026-10-16"}}

action is reply | pay | schedule | read | none; due is YYYY-MM-DD or null.

    zhunt serve &
    python scripts/brief_eval.py ~/.zhunt/brief_labels.jsonl --today 2026-10-13

Prints action accuracy, actionable precision/recall and due-date accuracy, plus the misses
(by line number and subject). Never prints keys; snippets stay on this computer.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhunt.agent import briefs  # noqa: E402
from zhunt.ports import DEFAULT_DAEMON_PORT, configured_port  # noqa: E402

DEFAULT_LABELS = Path.home() / ".zhunt" / "brief_labels.jsonl"


def load_labels(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        expected = row.get("expected") or {}
        if expected.get("action") not in briefs.ACTIONS:
            raise SystemExit(f"line {number}: expected.action must be one of {', '.join(briefs.ACTIONS)}")
        rows.append({**row, "line": number})
    return rows


def to_mail(row: dict[str, Any]) -> briefs.Mail:
    from email.utils import parseaddr

    name, address = parseaddr(str(row.get("from", "")))
    return briefs.Mail(id=str(row["line"]), sender=address.lower(), sender_name=name or address, to=str(row.get("to", "")),
                       subject=str(row.get("subject", "")), date=str(row.get("date", "")),
                       snippet=briefs.snippet(str(row.get("snippet", ""))))


def predictions(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per message id: the action and due date of the item that covers it (merged threads share one)."""

    out = {}
    for item in items:
        for mail_id in item.get("ids") or [item["id"]]:
            out[mail_id] = {"action": item["action"], "due": item["due"]}
    return out


def score(labels: list[dict[str, Any]], predicted: dict[str, dict[str, Any]]) -> dict[str, Any]:
    confusion: Counter[tuple[str, str]] = Counter()
    misses = []
    tp = fp = fn = 0
    due_total = due_ok = 0
    for row in labels:
        want = row["expected"]
        got = predicted.get(str(row["line"]), {"action": "none", "due": None})
        confusion[(want["action"], got["action"])] += 1
        want_act, got_act = want["action"] != "none", got["action"] != "none"
        tp += want_act and got_act
        fp += got_act and not want_act
        fn += want_act and not got_act
        if want_act and got_act:
            due_total += 1
            due_ok += (want.get("due") or None) == got["due"]
        if want["action"] != got["action"] or (want_act and got_act and (want.get("due") or None) != got["due"]):
            misses.append({"line": row["line"], "subject": str(row.get("subject", ""))[:80], "expected": want, "got": got})
    n = len(labels)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "messages": n,
        "action_accuracy": sum(v for (w, g), v in confusion.items() if w == g) / n if n else 0.0,
        "actionable_precision": precision,
        "actionable_recall": recall,
        "actionable_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "due_accuracy": due_ok / due_total if due_total else None,
        "confusion": {f"{w}->{g}": v for (w, g), v in sorted(confusion.items())},
        "misses": misses,
    }


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


def daemon_caller(base: str, key: str):
    def post(messages: list[dict[str, Any]], session: str) -> dict[str, Any]:
        body = {"model": "zhunt-auto", "messages": messages, "stream": False, "max_tokens": briefs.MAX_OUTPUT_TOKENS}
        request = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "x-session-id": session})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise RuntimeError(f"daemon call failed: {error}") from None
        return {"message": (data.get("choices") or [{}])[0].get("message") or {}, "model": data.get("model")}

    async def call(messages: list[dict[str, Any]], session: str) -> dict[str, Any]:
        return await asyncio.to_thread(post, messages, session)

    return call


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("labels", nargs="?", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today(), help="the date the mail was read on (YYYY-MM-DD)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    if Path(__file__).resolve().parents[1] in args.labels.resolve().parents:
        print("keep your labelled mail outside the repository", file=sys.stderr)
        return 2
    key = master_key()
    if not key:
        print("no ZHUNT_MASTER_KEY (set it or run zhunt once)", file=sys.stderr)
        return 2
    labels = load_labels(args.labels)
    base = f"http://127.0.0.1:{args.port or configured_port() or DEFAULT_DAEMON_PORT}"
    result = asyncio.run(briefs.extract([to_mail(r) for r in labels], daemon_caller(base, key), "brief-eval", today=args.today))
    report = {**score(labels, predictions(result["items"])), "models": result["models"], "error": result["error"]}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{report['messages']} messages on {', '.join(report['models']) or 'no model'}")
        print(f"action accuracy      {report['action_accuracy']:.0%}")
        print(f"actionable P / R / F1 {report['actionable_precision']:.0%} / {report['actionable_recall']:.0%} / {report['actionable_f1']:.0%}")
        if report["due_accuracy"] is not None:
            print(f"due-date accuracy    {report['due_accuracy']:.0%}")
        for miss in report["misses"]:
            print(f"  line {miss['line']}: {miss['subject']!r} expected {miss['expected']} got {miss['got']}")
        if report["error"]:
            print(f"stopped early: {report['error']}")
    return 1 if report["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
