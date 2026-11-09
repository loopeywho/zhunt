from pathlib import Path


SITE = Path(__file__).parents[1] / "site"
PAYMENT_LINK = "https://buy.stripe.com/28EdR883o8tSgfE2RncMM01"


def test_install_section_points_at_the_v2_package_not_v1_installers():
    html = (SITE / "index.html").read_text(encoding="utf-8")

    assert "uv tool install git+https://github.com/loopeywho/zhunt" in html
    assert "zhunt keys add groq" in html
    # the v1 installers install the old OpenRouter product
    assert "releases/download/v0.1.0.dev1" not in html
    assert "OpenRouter" not in html and "Nous Portal" not in html


def test_pre_release_takes_no_payments_until_pro_can_be_activated():
    # Pre-release: the licence service is not configured, so a checkout would charge people
    # who then get no key. Put the payment link back (Pro card and disclosure) for launch.
    html = (SITE / "index.html").read_text(encoding="utf-8")

    assert PAYMENT_LINK not in html and "buy.stripe.com" not in html
    assert html.count("%5Bzhunt%20Pro%5D%20waitlist") == 2  # Pro card and the note under the plans
    assert "$7/mo" in html
    assert "£" not in html
    assert "eVqfZg5Vg25ubZo77DcMM00" not in html


def test_activation_page_exchanges_the_checkout_session_for_a_key():
    page = (SITE / "activate.html").read_text(encoding="utf-8")
    script = (SITE / "activate.js").read_text(encoding="utf-8")

    assert '<script src="activate.js"></script>' in page  # the site CSP allows only same-origin scripts
    assert "/api/licence?session_id=" in script
    assert "history.replaceState" in script  # session id leaves the address bar
    assert "zhunt licence activate" in page


def test_homepage_shows_the_demo_video_without_preloading_it():
    html = (SITE / "index.html").read_text(encoding="utf-8")

    assert 'src="media/zhunt-demo.mp4"' in html and 'preload="none"' in html
    assert (SITE / "media" / "zhunt-demo.mp4").stat().st_size < 10 * 1024 * 1024
    assert (SITE / "media" / "zhunt-demo-poster.jpg").is_file()


def test_setup_guide_is_linked_and_only_names_real_commands():
    import re

    from typer.testing import CliRunner

    from zhunt.cli import app

    page = (SITE / "setup.html").read_text(encoding="utf-8")
    assert 'href="setup.html"' in (SITE / "index.html").read_text(encoding="utf-8")
    assert "<script" not in page  # the site CSP allows no inline scripts; the guide needs none
    blocks = re.findall(r"<code>(.*?)</code>", page, re.S)
    code = "\n".join(line.split("#")[0] for block in blocks for line in block.splitlines())
    commands = set(re.findall(r"\bzhunt ((?:[a-z]+)(?: [a-z]+)?)", code))
    assert {"serve", "open", "doctor", "keys add", "launch codex"} <= commands
    assert not {"licence activate", "premium on"} & commands
    for command in sorted(commands):
        words = command.split()
        args = words if words[0] in {"keys", "licence", "premium"} else words[:1]
        assert CliRunner().invoke(app, [*args, "--help"]).exit_code == 0, command


def test_setup_preserves_portable_preview_and_not_on_sale_disclosures():
    page = (SITE / "setup.html").read_text(encoding="utf-8")
    for text in (
        "zhunt/Zhunt.command", "./zhunt/Start-Zhunt.sh",
        "glibc 2.35 or newer", "unsigned and not notarized",
        "integrated Windows terminals remain unsupported",
        "uv tool install --python 3.12 git+https://github.com/loopeywho/zhunt",
        "Pro is not on sale in this pre-release. Paid activation instructions will be published when it opens.",
    ):
        assert text in page
    assert page.index("zhunt/Zhunt.command") < page.index("uv tool install")
    for name in ("Zhunt-Portable-macos-arm64.tar.gz", "Zhunt-Setup-linux-x64.tar.gz"):
        url = "https://github.com/loopeywho/zhunt/releases/download/v0.2.0.dev0/" + name
        assert f'href="{url}"' in page
        assert f'href="{url}.sha256"' in page
    assert "After checkout" not in page and "Pro subscribers:" not in page


def test_pages_give_link_previews_and_crawlers_what_they_need():
    # Shared links on LinkedIn and X show a card only with these; the image must be absolute.
    for page in ("index.html", "setup.html", "privacy.html", "terms.html"):
        html = (SITE / page).read_text(encoding="utf-8")
        for tag in ('rel="canonical"', 'property="og:title"', 'property="og:description"',
                    'property="og:url"', 'name="twitter:card" content="summary_large_image"'):
            assert tag in html, (page, tag)
        assert 'property="og:image" content="https://zhunt.kindredwildcat.com/media/zhunt-demo-poster.jpg"' in html
    assert (SITE / "media" / "zhunt-demo-poster.jpg").is_file()
    # Without these files Cloudflare Pages answers with the home page instead.
    robots = (SITE / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://zhunt.kindredwildcat.com/sitemap.xml" in robots
    sitemap = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.startswith("<?xml") and "<loc>https://zhunt.kindredwildcat.com/</loc>" in sitemap
