"""scripts/brief_eval.py scores extraction against labelled mail (synthetic here)."""

import asyncio
import datetime as dt
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("brief_eval", Path(__file__).resolve().parents[1] / "scripts" / "brief_eval.py")
brief_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brief_eval)

LABELS = [
    {"from": "Sam <sam@example.com>", "subject": "Deck?", "snippet": "Can you send the deck by Friday?", "expected": {"action": "reply", "due": "2026-10-16"}},
    {"from": "Shop <news@shop.com>", "subject": "50% off", "snippet": "Sale ends soon", "expected": {"action": "none", "due": None}},
    {"from": "ACME <bills@acme.com>", "subject": "Invoice 7", "snippet": "£40 due 20 Oct", "expected": {"action": "pay", "due": "2026-10-20"}},
    {"from": "Bo <bo@x.com>", "subject": "Call?", "snippet": "Free next week?", "expected": {"action": "schedule", "due": None}},
]


def test_the_eval_scores_actions_and_due_dates(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in LABELS) + "\n")
    labels = brief_eval.load_labels(path)
    mails = [brief_eval.to_mail(r) for r in labels]

    async def model(messages, session):
        items = [{"id": "1", "summary": "Sam wants the deck", "due": "2026-10-16", "people": ["Sam"], "action": "reply"},
                 {"id": "2", "summary": "A sale", "due": None, "people": [], "action": "read"},
                 {"id": "3", "summary": "ACME invoice", "due": "2026-10-21", "people": [], "action": "pay"}]
        return {"message": {"content": json.dumps({"items": items})}, "model": "groq/test"}

    result = asyncio.run(brief_eval.briefs.extract(mails, model, "brief-eval", today=dt.date(2026, 10, 13)))
    report = brief_eval.score(labels, brief_eval.predictions(result["items"]))
    assert report["messages"] == 4 and report["action_accuracy"] == 0.5  # sale -> read, call -> none
    assert report["actionable_precision"] == 2 / 3 and report["actionable_recall"] == 2 / 3
    assert report["due_accuracy"] == 0.5  # the invoice date is a day off
    assert {m["line"] for m in report["misses"]} == {2, 3, 4}
    assert report["confusion"]["schedule->none"] == 1
