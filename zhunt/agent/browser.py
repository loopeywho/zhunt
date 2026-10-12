"""The agent's own browser: the user's installed Chrome/Edge/Brave/Chromium, started with a
separate, clean zhunt profile (no access to the user's passwords, cookies or tabs) and driven
through the Chrome DevTools Protocol. The window is visible so the user can watch, and log in
themselves when a task needs it.

Risk: reading, opening public pages and following links run straight away; typing fills a
field without submitting; clicking buttons and pressing Enter are "act" and need approval.
The agent may never type into password fields.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from zhunt.agent.tools import MAX_RESULT_CHARS, Tool, ToolError, check_url

PROFILE = Path.home() / ".zhunt" / "browser-profile"
_CANDIDATES = {
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
}
_LINUX = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"]

# Collects the visible page as text plus numbered interactive elements, tagging each with
# data-zhunt-id so later clicks/typing find exactly the element the model saw.
_SNAPSHOT_JS = r"""
(() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = (el) => (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || el.name || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const items = [];
  let n = 0;
  for (const el of document.querySelectorAll('a[href], button, input, textarea, select, [role=button], [role=link]')) {
    if (!visible(el) || el.disabled) continue;
    n += 1;
    el.setAttribute('data-zhunt-id', String(n));
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.type || 'text') : tag === 'a' || el.getAttribute('role') === 'link' ? 'link' : tag;
    items.push({ id: n, type, text: label(el), href: tag === 'a' ? el.href : undefined, form: !!el.closest('form') });
    if (n >= 150) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 9000);
  return JSON.stringify({ url: location.href, title: document.title, text, items });
})()
"""


def find_browser() -> str | None:
    override = os.environ.get("ZHUNT_BROWSER")
    if override and Path(override).exists():
        return override
    for path in _CANDIDATES.get(sys.platform, []):
        if Path(path).exists():
            return path
    for name in _LINUX:
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AgentBrowser:
    """One visible browser window per daemon, started on first use."""

    def __init__(self, profile: Path = PROFILE) -> None:
        self.profile = profile
        self.process: subprocess.Popen | None = None
        self.port = 0
        self._ws: Any = None
        self._next = 0
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._ws is not None and self.process and self.process.poll() is None:
            return
        exe = find_browser()
        if exe is None:
            raise ToolError("no Chrome, Edge, Brave or Chromium was found on this computer (set ZHUNT_BROWSER to its path)")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.port = _free_port()
        self.process = subprocess.Popen(
            [exe, f"--user-data-dir={self.profile}", f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1",
             "--no-first-run", "--no-default-browser-check", "--window-size=1200,900",
             *(["--headless=new"] if os.environ.get("ZHUNT_BROWSER_HEADLESS") else []), "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ws_url = None
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(60):
                try:
                    pages = (await client.get(f"http://127.0.0.1:{self.port}/json/list")).json()
                    page = next((p for p in pages if p.get("type") == "page"), None)
                    if page:
                        ws_url = page["webSocketDebuggerUrl"]
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.25)
        if not ws_url:
            raise ToolError("the agent browser did not start")
        import websockets

        self._ws = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        await self._send("Page.enable")
        await self._send("Runtime.enable")

    async def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        msg_id = self._next
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(await asyncio.wait_for(self._ws.recv(), 30))
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise ToolError(f"browser: {reply['error'].get('message', 'error')}")
                return reply.get("result", {})

    async def _eval(self, expression: str) -> Any:
        result = await self._send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise ToolError("the page script failed")
        return result.get("result", {}).get("value")

    async def _settle(self) -> None:
        for _ in range(40):
            if await self._eval("document.readyState") == "complete":
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.4)

    async def snapshot(self) -> str:
        data = json.loads(await self._eval(_SNAPSHOT_JS))
        rows = []
        for item in data["items"]:
            extra = f" -> {item['href']}" if item.get("href") else ""
            rows.append(f"[{item['id']}] {item['type']}: {item['text'] or '(no label)'}{extra}")
        return (f"Page: {data['title']}\nAddress: {data['url']}\n\nInteractive elements (use their numbers):\n"
                + "\n".join(rows) + f"\n\nPage text:\n{data['text']}")[:MAX_RESULT_CHARS + 4000]

    async def _element(self, index: int) -> dict[str, Any]:
        info = await self._eval(
            f"(() => {{ const el = document.querySelector('[data-zhunt-id=\"{int(index)}\"]'); if (!el) return null;"
            " return JSON.stringify({tag: el.tagName.toLowerCase(), type: el.type || '', href: el.href || '', text: (el.innerText || el.value || el.placeholder || '').slice(0, 80)}); })()"
        )
        if not info:
            raise ToolError(f"there is no element [{index}] on the current page; read the page again")
        return json.loads(info)

    # ---- tools ----

    async def open(self, url: str) -> str:
        async with self._lock:
            await self._ensure()
            await self._send("Page.navigate", {"url": check_url(url)})
            await self._settle()
            return await self.snapshot()

    async def read(self) -> str:
        async with self._lock:
            await self._ensure()
            return await self.snapshot()

    async def follow(self, index: int) -> str:
        async with self._lock:
            await self._ensure()
            el = await self._element(index)
            if not el["href"]:
                raise ToolError(f"[{index}] is not a link; use browser_click for buttons")
            await self._send("Page.navigate", {"url": check_url(el["href"])})
            await self._settle()
            return await self.snapshot()

    async def type_text(self, index: int, text: str) -> str:
        async with self._lock:
            await self._ensure()
            el = await self._element(index)
            if el["type"] == "password":
                raise ToolError("the agent never types passwords; ask the user to log in in the agent browser window")
            if el["tag"] not in ("input", "textarea"):
                raise ToolError(f"[{index}] is not a text field")
            await self._eval(f"document.querySelector('[data-zhunt-id=\"{int(index)}\"]').focus()")
            await self._eval(f"(() => {{ const el = document.querySelector('[data-zhunt-id=\"{int(index)}\"]'); el.value = ''; el.dispatchEvent(new Event('input', {{bubbles: true}})); }})()")
            await self._send("Input.insertText", {"text": str(text)[:2000]})
            return f"Typed into [{index}] ({el['text'] or el['type']}). Nothing was submitted."

    async def click(self, index: int) -> str:
        async with self._lock:
            await self._ensure()
            el = await self._element(index)
            await self._eval(f"document.querySelector('[data-zhunt-id=\"{int(index)}\"]').click()")
            await self._settle()
            return f"Clicked [{index}] ({el['text'] or el['tag']}).\n\n" + await self.snapshot()

    async def press_enter(self, index: int) -> str:
        async with self._lock:
            await self._ensure()
            await self._element(index)
            await self._eval(f"document.querySelector('[data-zhunt-id=\"{int(index)}\"]').focus()")
            for kind in ("keyDown", "keyUp"):
                await self._send("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r" if kind == "keyDown" else ""})
            await self._settle()
            return f"Pressed Enter in [{index}].\n\n" + await self.snapshot()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self.process and self.process.poll() is None:
            self.process.terminate()


def browser_tools(browser: AgentBrowser) -> list[Tool]:
    idx = {"type": "integer", "description": "The element number from the latest page reading."}

    async def browser_open(url: str) -> str:
        return await browser.open(url)

    async def browser_read() -> str:
        return await browser.read()

    async def browser_follow_link(index: int) -> str:
        return await browser.follow(index)

    async def browser_type(index: int, text: str) -> str:
        return await browser.type_text(index, text)

    async def browser_click(index: int) -> str:
        return await browser.click(index)

    async def browser_press_enter(index: int) -> str:
        return await browser.press_enter(index)

    return [
        Tool("browser_open", "Open a public website in the agent's browser window and read it (text plus numbered links, buttons and fields).", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, "read", browser_open),
        Tool("browser_read", "Read the current page in the agent's browser again (after the user logged in, or after changes).", {"type": "object", "properties": {}}, "read", browser_read),
        Tool("browser_follow_link", "Follow a numbered link on the current page.", {"type": "object", "properties": {"index": idx}, "required": ["index"]}, "read", browser_follow_link),
        Tool("browser_type", "Type text into a numbered field (never passwords). Does not submit.", {"type": "object", "properties": {"index": idx, "text": {"type": "string"}}, "required": ["index", "text"]}, "read", browser_type),
        Tool("browser_click", "Click a numbered button or element (may submit or buy something). The user must approve.", {"type": "object", "properties": {"index": idx}, "required": ["index"]}, "act", browser_click),
        Tool("browser_press_enter", "Press Enter in a numbered field (usually submits a form or search). The user must approve.", {"type": "object", "properties": {"index": idx}, "required": ["index"]}, "act", browser_press_enter),
    ]
