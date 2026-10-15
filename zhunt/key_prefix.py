"""Tell which provider a pasted API key belongs to from its prefix alone.

Only the first few characters are compared; the key is never logged, stored or
sent anywhere by this module.
"""

from __future__ import annotations

import re

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

# Providers whose keys can be told apart, so the agent can save them from a page.
KEY_PLATFORMS = frozenset(platform for _, platform in _PREFIXES)


def detect_platform(key: str) -> str | None:
    """The provider whose keys start like ``key``, or ``None`` when it is not clear."""

    key = key.strip()
    for prefix, platform in _PREFIXES:
        if key.startswith(prefix):
            return platform
    return None


# A key-shaped run on a web page: a known prefix, then at least 16 key characters.
KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:" + "|".join(re.escape(prefix) for prefix, _ in _PREFIXES) + r")[A-Za-z0-9_-]{16,}"
)


def find_keys(text: str) -> list[tuple[str, str]]:
    """Every (platform, key) on a page, in order, each key once."""

    found: list[tuple[str, str]] = []
    for match in KEY_PATTERN.finditer(text):
        key = match.group(0)
        platform = detect_platform(key)
        if platform and all(key != k for _, k in found):
            found.append((platform, key))
    return found


def hide_keys(text: str) -> str:
    """The text with every key-shaped run replaced, so a page reading never carries a key."""

    return KEY_PATTERN.sub("[API key hidden: call browser_save_key to save it]", text)
