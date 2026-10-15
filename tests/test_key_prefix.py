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


def test_keys_on_a_page_are_found_once_each_and_hidden_from_readings() -> None:
    from zhunt.key_prefix import find_keys, hide_keys

    groq = "gsk_" + "A1b2C3d4E5f6G7h8J9k0Lm"
    google = "AIza" + "SyD-abcdefghijklmnopqrstuvwxyz012"
    page = f"Your new key: {groq}\nCopy {groq}\nGemini {google}\nnot a key: sk-learn and gsk_short"
    assert find_keys(page) == [("groq", groq), ("google", google)]
    hidden = hide_keys(page)
    assert groq not in hidden and google not in hidden
    assert "sk-learn" in hidden and "gsk_short" in hidden
