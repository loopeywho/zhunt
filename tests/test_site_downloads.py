from pathlib import Path


SITE = Path(__file__).parents[1] / "site" / "index.html"
RELEASE_BASE = "https://github.com/loopeywho/zhunt/releases/download/v0.1.0.dev1/"


def test_download_cards_point_to_preview_release_assets():
    html = SITE.read_text(encoding="utf-8")

    assert RELEASE_BASE + "Zhunt-Setup-macos-arm64.dmg" in html
    assert RELEASE_BASE + "Zhunt-Setup-macos-arm64.pkg" in html
    assert RELEASE_BASE + "Zhunt-Setup-win-x64.exe" in html
    assert RELEASE_BASE + "Zhunt-Setup-linux-x64.tar.gz" in html
    assert "macOS Universal" not in html
    assert "Installer connection in progress" not in html
    assert "Windows ARM64" not in html
    assert "Linux x86_64" in html
    assert "Coming soon" not in html


def test_public_checkout_uses_usd_payment_link():
    html = SITE.read_text(encoding="utf-8")

    assert html.count("https://buy.stripe.com/28EdR883o8tSgfE2RncMM01") == 4
    assert "$7/month" in html
    assert "$7/mo" in html
    assert "£" not in html
    assert "eVqfZg5Vg25ubZo77DcMM00" not in html
