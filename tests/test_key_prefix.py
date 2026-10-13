import pytest

from zhunt.key_prefix import detect_platform


@pytest.mark.parametrize(
    "key, platform",
    [
        ("gsk_abc123", "groq"),
        ("AIzaSyAbc123", "google"),
        ("csk-abc123", "cerebras"),
        ("sk-ant-api03-abc123", "anthropic"),
        ("sk-proj-abc123", "openai"),
        ("sk-abc123", "openai"),
        ("  gsk_abc123\n", "groq"),
    ],
)
def test_known_prefixes_name_their_provider(key: str, platform: str) -> None:
    assert detect_platform(key) == platform


@pytest.mark.parametrize("key", ["", "abc123", "GSK_abc", "aiza123", "ant-sk-123", "nvapi-123"])
def test_unknown_prefixes_name_no_provider(key: str) -> None:
    assert detect_platform(key) is None
