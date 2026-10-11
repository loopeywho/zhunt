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


def test_public_checkout_uses_usd_payment_link():
    html = (SITE / "index.html").read_text(encoding="utf-8")

    assert html.count(PAYMENT_LINK) == 3  # nav, checkout card, closing call to action
    assert "$7/month" in html
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
