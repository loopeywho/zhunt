"""Request classification and session routing state."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from threading import RLock


class Tier(str, Enum):
    """Ordered capability tiers used by the shared routing core."""

    CHAT = "chat"
    CODING = "coding"
    LONG_CONTEXT = "long-context"
    REASONING = "reasoning"

    @property
    def rank(self) -> int:
        return _TIER_ORDER.index(self)

    def is_higher_than(self, other: Tier) -> bool:
        return self.rank > other.rank

    def next_higher(self) -> Tier:
        return _TIER_ORDER[min(self.rank + 1, len(_TIER_ORDER) - 1)]

    @classmethod
    def highest(cls) -> Tier:
        return _TIER_ORDER[-1]


# v1 intentionally projects workload categories onto one monotonic safety ladder.
# Stickiness and escalation therefore have one deterministic promotion path.
_TIER_ORDER = (
    Tier.CHAT,
    Tier.CODING,
    Tier.LONG_CONTEXT,
    Tier.REASONING,
)

_USER_REASONING_MARKERS = re.compile(
    r"\b(?:analy[sz]e|architect|plan|prove|reason|think)\b",
    re.IGNORECASE,
)
_DIFF_MARKERS = re.compile(
    r"^(?:diff --git |@@ .* @@|\+\+\+ [ab]/|--- [ab]/)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ClassificationInput:
    """Protocol-neutral request features supplied by a wire adapter."""

    user_text: str
    system_prompt: str = ""
    estimated_tokens: int | None = None
    has_tool_calls: bool = False


@dataclass(frozen=True)
class Classification:
    tier: Tier
    reasons: tuple[str, ...]


def _estimate(request: ClassificationInput) -> int:
    if request.estimated_tokens is not None:
        return request.estimated_tokens
    combined_text = "\n".join(
        part for part in (request.system_prompt, request.user_text) if part
    )
    return max(1, len(combined_text) // 4)


class HeuristicClassifier:
    """Cheap, deterministic v1 classifier."""

    def __init__(self, *, long_context_tokens: int = 16_000) -> None:
        if long_context_tokens <= 0:
            raise ValueError("long_context_tokens must be positive")
        self.long_context_tokens = long_context_tokens

    def classify(self, request: ClassificationInput) -> Classification:
        estimated_tokens = _estimate(request)
        if _USER_REASONING_MARKERS.search(request.user_text):
            return Classification(Tier.REASONING, ("user reasoning marker",))
        return self._structural(request, estimated_tokens)

    def _structural(
        self, request: ClassificationInput, estimated_tokens: int
    ) -> Classification:
        """Long-context, coding or chat, from facts about the request."""

        reasons: list[str] = []
        if estimated_tokens >= self.long_context_tokens:
            reasons.append("long context")
            return Classification(Tier.LONG_CONTEXT, tuple(reasons))

        if request.has_tool_calls:
            reasons.append("tool call")
        if "```" in request.user_text:
            reasons.append("code fence")
        if _DIFF_MARKERS.search(request.user_text):
            reasons.append("diff marker")
        if reasons:
            return Classification(Tier.CODING, tuple(reasons))

        return Classification(Tier.CHAT, ("conversational/default",))


# Share of prompts the learned router sends to the reasoning tier by default.
# On RouteLLM's validation data, 0.2 sends the hardest fifth of prompts up and
# catches about 48% of the prompts a weak model answers poorly (the keyword
# rule caught 8% while sending 7%). See zhunt/difficulty.py.
DEFAULT_STRONG_SHARE = 0.2

# Messages of this many words or fewer are never sent up a tier by the score.
MIN_SCORED_WORDS = 3


class LearnedClassifier(HeuristicClassifier):
    """RouteLLM-style classifier: a learned difficulty score picks the strong tier.

    The reasoning decision comes from ``zhunt.difficulty``: a model trained on
    RouteLLM's public data predicts whether a weak model's answer to the user's
    text would be good enough. ``strong_share`` is the cost/quality dial: about
    that share of prompts is sent to the reasoning tier. Long-context and coding
    detection stay rule-based, because they are facts about the request rather
    than guesses about difficulty. Only the user's text is scored, so agent
    system prompts cannot push every turn up a tier.
    """

    def __init__(
        self,
        *,
        strong_share: float = DEFAULT_STRONG_SHARE,
        long_context_tokens: int = 16_000,
        model=None,
    ) -> None:
        super().__init__(long_context_tokens=long_context_tokens)
        if model is None:
            from zhunt.difficulty import default_model

            model = default_model()
        self.model = model
        self.strong_share = strong_share
        self.threshold = model.threshold_for_strong_share(strong_share)

    def classify(self, request: ClassificationInput) -> Classification:
        # Messages of a few words ("hi", "Thanks.", "Continue.") are not scored:
        # RouteLLM's data has under 350 of them in 109,000 prompts, mostly hex
        # strings and bare URLs, so the model has not learned that they are easy.
        words = len(request.user_text.split())
        difficulty = (
            self.model.score(request.user_text) if words > MIN_SCORED_WORDS else 0.0
        )
        if difficulty >= self.threshold:
            return Classification(
                Tier.REASONING,
                (f"difficulty {difficulty:.2f} >= {self.threshold:.2f}",),
            )
        return self._structural(request, _estimate(request))


@dataclass(frozen=True)
class SessionRoute:
    tier: Tier
    model: str


@dataclass(frozen=True)
class StickinessDecision:
    route: SessionRoute
    changed: bool


class SessionStickiness:
    """Keeps a session on one model unless its required tier rises."""

    def __init__(self) -> None:
        self._routes: dict[str, SessionRoute] = {}
        self._lock = RLock()

    def choose(
        self,
        *,
        session_key: str,
        classified_tier: Tier,
        select_model: Callable[[Tier], str],
    ) -> StickinessDecision:
        if not session_key:
            raise ValueError("session_key cannot be empty")

        with self._lock:
            current = self._routes.get(session_key)
            if current is not None and not classified_tier.is_higher_than(current.tier):
                return StickinessDecision(route=current, changed=False)

            route = SessionRoute(
                tier=classified_tier,
                model=select_model(classified_tier),
            )
            self._routes[session_key] = route
            return StickinessDecision(route=route, changed=True)

    def get(self, session_key: str) -> SessionRoute | None:
        with self._lock:
            return self._routes.get(session_key)

    def clear(self, session_key: str) -> None:
        with self._lock:
            self._routes.pop(session_key, None)


def session_key(
    *,
    session_id: str | None,
    system_prompt: str,
    first_user_message: str,
) -> str:
    """Use an app session id, falling back to stable request-derived identity."""

    if session_id:
        return f"id:{session_id}"
    digest = sha256(
        f"{system_prompt}\0{first_user_message}".encode("utf-8")
    ).hexdigest()
    return f"fallback:{digest}"
