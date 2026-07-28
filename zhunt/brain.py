"""Request classification and session routing state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


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


_TIER_ORDER = (
    Tier.CHAT,
    Tier.CODING,
    Tier.LONG_CONTEXT,
    Tier.REASONING,
)

_REASONING_MARKERS = re.compile(
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


class HeuristicClassifier:
    """Cheap, deterministic v1 classifier."""

    def __init__(self, *, long_context_tokens: int = 16_000) -> None:
        if long_context_tokens <= 0:
            raise ValueError("long_context_tokens must be positive")
        self.long_context_tokens = long_context_tokens

    def classify(self, request: ClassificationInput) -> Classification:
        combined_text = "\n".join(
            part for part in (request.system_prompt, request.user_text) if part
        )
        estimated_tokens = request.estimated_tokens
        if estimated_tokens is None:
            estimated_tokens = max(1, len(combined_text) // 4)

        reasons: list[str] = []
        if _REASONING_MARKERS.search(combined_text):
            reasons.append("reasoning marker")
            return Classification(Tier.REASONING, tuple(reasons))

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
