"""Learned prompt-difficulty scorer, in the spirit of RouteLLM.

RouteLLM (lm-sys/RouteLLM, Apache-2.0) routes each prompt to a strong or a weak
model by predicting whether the weak model's answer will be good enough, and
exposes one threshold that sets the cost/quality trade-off. This module does the
same with a small hashed n-gram logistic regression so it runs in plain Python,
offline, in well under a millisecond per prompt, with no extra dependencies.

The weights in ``router_weights.json`` are trained by
``scripts/train_router.py`` on the public ``routellm/gpt4_dataset``
(Apache-2.0): about 109,000 real chat prompts, each with a GPT-4 judge score
(1-5) of a weak model's (Mixtral 8x7B) answer. A prompt is labelled "needs a
strong model" when that score is 3 or lower.

``score`` returns the predicted probability that a weak model is not good
enough. ``threshold_for_strong_share`` turns "send about this share of prompts
to the strong tier" into a score threshold, using quantiles of the scores on
the held-out validation split, the way RouteLLM calibrates its thresholds.
"""

from __future__ import annotations

import base64
import json
import math
import re
import zlib
from array import array
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

WEIGHTS_PATH = Path(__file__).with_name("router_weights.json")

# Only the start of a prompt is read: it is where the ask usually is, and it
# keeps scoring cheap on long pasted documents.
MAX_TOKENS = 384

_TOKEN = re.compile(r"[a-z0-9]+|[^\sa-z0-9]", re.IGNORECASE)
_MATH = re.compile(r"[=^∑∫√≤≥±×÷]|\\frac|\\sum|\d+\s*[-+*/]\s*\d+")


def _bucket(feature: str, size: int) -> int:
    return zlib.crc32(feature.encode("utf-8")) % size


def _length_bucket(n: int) -> int:
    return min(12, int(math.log2(n + 1)))


def features(text: str) -> list[str]:
    """Return the string features of a prompt (shared by training and scoring)."""

    tokens = [t.lower() for t in _TOKEN.findall(text)]
    n = len(tokens)
    tokens = tokens[:MAX_TOKENS]
    out = ["__bias__", f"__len{_length_bucket(n)}"]
    out.append(f"__lines{_length_bucket(text.count(chr(10)))}")
    if "```" in text:
        out.append("__fence")
    if _MATH.search(text):
        out.append("__math")
    if text.rstrip().endswith("?"):
        out.append("__endq")
    out.extend("u:" + t for t in tokens)
    out.extend("b:" + a + " " + b for a, b in zip(tokens, tokens[1:]))
    return out


@dataclass(frozen=True)
class DifficultyModel:
    weights: array
    quantiles: tuple[float, ...]
    meta: dict

    @property
    def size(self) -> int:
        return len(self.weights)

    def score(self, text: str) -> float:
        """Probability that a weak model's answer to ``text`` is not good enough."""

        size = self.size
        w = self.weights
        z = sum(w[_bucket(f, size)] for f in set(features(text)))
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)
        return e / (1.0 + e)

    def threshold_for_strong_share(self, share: float) -> float:
        """Score threshold that sends about ``share`` of prompts to the strong tier.

        Calibrated on the validation split; real traffic will differ, as
        RouteLLM's documentation also warns.
        """

        if not 0.0 <= share <= 1.0:
            raise ValueError("strong share must be between 0 and 1")
        if share <= 0.0:
            return math.inf
        if share >= 1.0:
            return -math.inf
        q = self.quantiles
        # quantiles[i] is the score below which i/(len-1) of prompts fall.
        position = (1.0 - share) * (len(q) - 1)
        low = int(position)
        high = min(low + 1, len(q) - 1)
        return q[low] + (q[high] - q[low]) * (position - low)

    def strong_share_for_threshold(self, threshold: float) -> float:
        q = self.quantiles
        return 1.0 - bisect_left(q, threshold) / (len(q) - 1)


def encode_weights(weights: list[float]) -> dict:
    """Quantise weights to int16 and pack them for the JSON file."""

    peak = max((abs(x) for x in weights), default=0.0) or 1.0
    scale = peak / 32767.0
    packed = array("h", (int(round(x / scale)) for x in weights))
    if packed.itemsize != 2:  # pragma: no cover - every CPython platform is 2
        raise RuntimeError("int16 array expected")
    raw = packed.tobytes() if _little_endian() else _swapped(packed).tobytes()
    return {
        "size": len(weights),
        "scale": scale,
        "int16_le_zlib_b64": base64.b64encode(zlib.compress(raw, 9)).decode("ascii"),
    }


def _little_endian() -> bool:
    import sys

    return sys.byteorder == "little"


def _swapped(a: array) -> array:
    b = array(a.typecode, a)
    b.byteswap()
    return b


def decode_weights(blob: dict) -> array:
    raw = zlib.decompress(base64.b64decode(blob["int16_le_zlib_b64"]))
    packed = array("h")
    packed.frombytes(raw)
    if not _little_endian():
        packed.byteswap()
    if len(packed) != blob["size"]:
        raise ValueError("router weights are truncated")
    scale = float(blob["scale"])
    return array("f", (x * scale for x in packed))


def load(path: Path | str = WEIGHTS_PATH) -> DifficultyModel:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    quantiles = tuple(float(x) for x in data["quantiles"])
    if len(quantiles) < 2 or list(quantiles) != sorted(quantiles):
        raise ValueError("router quantiles must be sorted")
    return DifficultyModel(
        weights=decode_weights(data["weights"]),
        quantiles=quantiles,
        meta=dict(data.get("meta", {})),
    )


@lru_cache(maxsize=1)
def default_model() -> DifficultyModel:
    return load(WEIGHTS_PATH)
