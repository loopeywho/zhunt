"""The agent browser against a real local Chrome (skipped when none is installed)."""

import asyncio
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from zhunt.agent import browser as B
from zhunt.agent.tools import ToolError

PAGE = """<!doctype html><title>Test shop</title>
<h1>Welcome</h1><p>Prices from 3 pounds.</p>
<a href="/about.html">About us</a>
<form onsubmit="event.preventDefault(); document.querySelector('h1').textContent='Searched: ' + document.querySelector('#q').value">
  <input id="q" name="q" placeholder="Search products">
  <input type="password" name="pw" placeholder="Password">
  <button type="submit">Search</button>
</form>
<button onclick="document.querySelector('h1').textContent='Added to basket'">Add to basket</button>"""
ABOUT = "<!doctype html><title>About</title><p>We sell chilli sauce.</p>"

pytestmark = pytest.mark.skipif(B.find_browser() is None, reason="no Chrome/Edge/Brave/Chromium installed")


@pytest.fixture()
def site(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    (tmp_path / "about.html").write_text(ABOUT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_read_type_click_and_follow_in_a_separate_profile(site, tmp_path, monkeypatch):
    monkeypatch.setenv("ZHUNT_BROWSER_HEADLESS", "1")

    async def run():
        browser = B.AgentBrowser(profile=tmp_path / "profile")
        # the local test site is on 127.0.0.1, which the agent normally refuses
        with patch.object(B, "check_url", lambda url: url):
            try:
                page = await browser.open(site + "/index.html")
                lines = {line.split("]")[0].strip("["): line for line in page.splitlines() if line.startswith("[")}
                search = next(k for k, v in lines.items() if "Search products" in v)
                password = next(k for k, v in lines.items() if "password" in v)
                add = next(k for k, v in lines.items() if "Add to basket" in v)
                link = next(k for k, v in lines.items() if "About us" in v)
                typed = await browser.type_text(int(search), "chilli")
                with pytest.raises(ToolError, match="never types passwords"):
                    await browser.type_text(int(password), "hunter2")
                entered = await browser.press_enter(int(search))
                clicked = await browser.click(int(add))
                followed = await browser.follow(int(link))
                return page, typed, entered, clicked, followed
            finally:
                await browser.close()

    page, typed, entered, clicked, followed = asyncio.run(run())
    assert "Page: Test shop" in page and "Prices from 3 pounds." in page
    assert "Nothing was submitted" in typed
    assert "Searched: chilli" in entered
    assert "Added to basket" in clicked
    assert "We sell chilli sauce." in followed


def test_the_agent_browser_still_refuses_private_addresses(tmp_path):
    async def run():
        browser = B.AgentBrowser(profile=tmp_path / "profile")
        try:
            await browser.open("http://127.0.0.1:4000/zhunt/api/status")
        finally:
            await browser.close()

    with pytest.raises(ToolError, match="private network"):
        asyncio.run(run())


def test_clicks_and_enter_need_approval_reading_does_not():
    risks = {t.name: t.risk for t in B.browser_tools(B.AgentBrowser())}
    assert risks["browser_click"] == "act" and risks["browser_press_enter"] == "act"
    assert {risks[k] for k in ("browser_open", "browser_read", "browser_follow_link", "browser_type")} == {"read"}
