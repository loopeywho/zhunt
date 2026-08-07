from pathlib import Path


SITE = Path(__file__).parents[1] / "site" / "index.html"
RELEASE_BASE = "https://github.com/loopeywho/zhunt/releases/download/v0.1.0.dev1/"


def test_download_cards_point_to_preview_release_assets():
    html = SITE.read_text(encoding="utf-8")

    assert RELEASE_BASE + "Zhunt-Setup-macos-arm64.dmg" in html
    assert RELEASE_BASE + "Zhunt-Setup-macos-arm64.pkg" in html
    assert RELEASE_BASE + "Zhunt-Setup-win-x64.exe" in html
    assert "macOS Universal" not in html
    assert "Installer connection in progress" not in html
    assert "Windows ARM64" not in html
    assert "Linux x86_64" in html
    assert "Coming soon" in html
