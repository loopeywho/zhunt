"""Tell which provider a pasted API key belongs to from its prefix alone.

Only the first few characters are compared; the key is never logged, stored or
sent anywhere by this module.
"""

from __future__ import annotations

# Checked in order, so the longer "sk-ant-" (Anthropic) wins over OpenAI's "sk-".
# "AIza" is a Google API key; it maps to the free AI Studio provider. A paid
# (billing-enabled) Gemini key looks the same, so that one is chosen by name.
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gsk_", "groq"),
    ("AIza", "google"),
    ("csk-", "cerebras"),
    ("sk-ant-", "anthropic"),
    ("sk-proj-", "openai"),
    ("sk-", "openai"),
)


def detect_platform(key: str) -> str | None:
    """The provider whose keys start like ``key``, or ``None`` when it is not clear."""

    key = key.strip()
    for prefix, platform in _PREFIXES:
        if key.startswith(prefix):
            return platform
    return None
