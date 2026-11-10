"""Inline images count as IMAGE_TOKENS each, not as their base64 text."""

from __future__ import annotations

from zhunt.adapters.base import IMAGE_TOKENS, estimate_tokens

BIG = "A" * 92_000  # about a 69 KB JPEG frame in base64


def text_only() -> int:
    return estimate_tokens({"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})


def test_a_chat_completions_data_url_counts_as_one_image() -> None:
    payload = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + BIG}},
    ]}]}
    assert IMAGE_TOKENS <= estimate_tokens(payload) < text_only() + IMAGE_TOKENS + 50


def test_an_anthropic_base64_source_counts_as_one_image() -> None:
    payload = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": BIG}},
    ]}]}
    assert IMAGE_TOKENS <= estimate_tokens(payload) < IMAGE_TOKENS + 100


def test_each_image_counts_and_text_still_counts() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + BIG}}
    two = {"model": "m", "messages": [{"role": "user", "content": [image, image]}]}
    assert estimate_tokens(two) >= 2 * IMAGE_TOKENS
    long_text = {"model": "m", "messages": [{"role": "user", "content": "x" * 40_000}]}
    assert estimate_tokens(long_text) >= 10_000


def test_an_image_url_on_the_web_is_just_text() -> None:
    payload = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}
    assert estimate_tokens(payload) < 100
