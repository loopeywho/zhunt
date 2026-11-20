"""Train Zhunt's learned router weights (``zhunt/router_weights.json``).

Data: the public ``routellm/gpt4_dataset`` from Hugging Face (Apache-2.0),
published by the RouteLLM authors. Download ``train.jsonl`` and ``valid.jsonl``
from https://huggingface.co/datasets/routellm/gpt4_dataset into a folder and
run:

    python scripts/train_router.py DATA_DIR

Each row has a prompt and ``mixtral_score``, a GPT-4 judge's 1-5 rating of a
weak model's answer. Label 1 ("needs a strong model") when the score is 3 or
lower. The model is a hashed unigram+bigram logistic regression trained with
AdaGrad; features come from ``zhunt.difficulty.features`` so training and
scoring cannot drift apart.

The script prints validation metrics, including a comparison with the old
keyword rule, and writes the weights plus score quantiles for calibrating the
strong-share threshold. Plain Python, no extra dependencies; takes a few
minutes.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zhunt.difficulty import (  # noqa: E402
    WEIGHTS_PATH,
    _bucket,
    decode_weights,
    encode_weights,
    features,
)

# The keyword rule the learned router replaces (zhunt/brain.py before this change).
_OLD_REASONING = re.compile(
    r"\b(?:analy[sz]e|architect|plan|prove|reason|think)\b", re.IGNORECASE
)


def read(path: Path) -> list[tuple[str, int]]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            score = row.get("mixtral_score")
            prompt = row.get("prompt")
            if not isinstance(prompt, str) or score in (None, ""):
                continue
            rows.append((prompt, 1 if int(score) <= 3 else 0))
    return rows


def encode(rows, size):
    return [
        (array("i", sorted({_bucket(f, size) for f in features(text)})), label)
        for text, label in rows
    ]


def train(examples, size, *, epochs, lr, l2, seed):
    w = [0.0] * size
    g2 = [1e-8] * size
    order = list(range(len(examples)))
    rng = random.Random(seed)
    for epoch in range(epochs):
        rng.shuffle(order)
        loss = 0.0
        for i in order:
            idx, y = examples[i]
            z = 0.0
            for j in idx:
                z += w[j]
            z = max(-30.0, min(30.0, z))
            p = 1.0 / (1.0 + math.exp(-z))
            loss -= math.log(p if y else 1.0 - p)
            g = p - y
            for j in idx:
                grad = g + l2 * w[j]
                g2[j] += grad * grad
                w[j] -= lr * grad / math.sqrt(g2[j])
        print(f"  epoch {epoch + 1}: train log-loss {loss / len(order):.4f}", flush=True)
    return w


def scores(w, examples):
    out = []
    for idx, _ in examples:
        z = max(-30.0, min(30.0, sum(w[j] for j in idx)))
        out.append(1.0 / (1.0 + math.exp(-z)))
    return out


def auc(s, y):
    pairs = sorted(zip(s, y))
    pos = sum(y)
    neg = len(y) - pos
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        rank_sum += avg_rank * sum(lbl for _, lbl in pairs[i:j])
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def caught_at_share(s, y, share):
    """Share of 'weak model not good enough' prompts sent to the strong tier."""

    k = int(round(share * len(s)))
    top = sorted(range(len(s)), key=lambda i: s[i], reverse=True)[:k]
    return sum(y[i] for i in top) / max(1, sum(y))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--size", type=int, default=1 << 17)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--l2", type=float, default=1e-6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=WEIGHTS_PATH)
    ap.add_argument(
        "--tune",
        action="store_true",
        help="hold out 10,000 training rows and report on them instead of valid.jsonl "
        "(use this to choose settings, so the validation figure stays unseen)",
    )
    args = ap.parse_args()

    started = time.time()
    train_rows = read(args.data_dir / "train.jsonl")
    valid_rows = read(args.data_dir / "valid.jsonl")
    if args.tune:
        random.Random(args.seed).shuffle(train_rows)
        train_rows, valid_rows = train_rows[10_000:], train_rows[:10_000]
        print("tuning mode: reporting on 10,000 held-out training rows")
    print(f"train {len(train_rows)} rows, {sum(l for _, l in train_rows)} need strong")
    print(f"valid {len(valid_rows)} rows, {sum(l for _, l in valid_rows)} need strong")

    train_x = encode(train_rows, args.size)
    valid_x = encode(valid_rows, args.size)
    w = train(train_x, args.size, epochs=args.epochs, lr=args.lr, l2=args.l2, seed=args.seed)

    blob = encode_weights(w)
    # Score validation with the quantised weights that will actually ship.
    shipped = decode_weights(blob)
    s = scores(shipped, valid_x)
    y = [label for _, label in valid_x]
    base_rate = sum(y) / len(y)

    old = [1 if _OLD_REASONING.search(text) else 0 for text, _ in valid_rows]
    old_share = sum(old) / len(old)
    old_caught = sum(o and l for o, l in zip(old, y)) / sum(y)

    metrics = {
        "validation_rows": len(y),
        "validation_need_strong_rate": round(base_rate, 4),
        "auc": round(auc(s, y), 4),
        "caught_at_strong_share": {
            str(share): round(caught_at_share(s, y, share), 4)
            for share in (0.1, 0.2, 0.3, 0.5)
        },
        "keyword_rule": {
            "strong_share": round(old_share, 4),
            "caught": round(old_caught, 4),
            "learned_caught_at_same_share": round(caught_at_share(s, y, old_share), 4),
        },
    }
    print(json.dumps(metrics, indent=2))

    ordered = sorted(s)
    quantiles = [ordered[min(len(ordered) - 1, round(i * (len(ordered) - 1) / 100))] for i in range(101)]
    payload = {
        "meta": {
            "format": 1,
            "source": "routellm/gpt4_dataset (Apache-2.0), label mixtral_score <= 3",
            "features": "hashed unigrams+bigrams of the first 384 tokens, crc32",
            "epochs": args.epochs,
            "lr": args.lr,
            "l2": args.l2,
            "seed": args.seed,
            "validation": metrics,
        },
        "quantiles": [round(q, 6) for q in quantiles],
        "weights": blob,
    }
    args.out.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes) in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
