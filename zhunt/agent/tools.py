"""The zhunt agent's tools: web research and files in folders the user chose.

Every tool declares a risk: ``read`` tools run straight away, ``write`` tools wait for the
user's approval in the Agent window. Tool results are data for the model, never instructions.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

MAX_RESULT_CHARS = 12_000
MAX_FILE_BYTES = 2 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; zhunt-agent/0.2; +https://zhunt.kindredwildcat.com)"
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html", ".htm", ".xml", ".log", ".py", ".js", ".ts", ".css", ".ini", ".toml", ".tsv"}


class ToolError(RuntimeError):
    """A tool could not do what was asked; the message goes back to the model."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str  # "read" | "write"
    run: Callable[..., Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class AgentSettings:
    """Folders the agent may use. Stored in ~/.zhunt/agent.json (the user edits them in the app)."""

    folders: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> AgentSettings:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        folders = [Path(f).expanduser() for f in raw.get("folders", []) if isinstance(f, str)]
        return cls(folders=[f for f in folders if f.is_dir()])

    def to_json(self) -> dict[str, Any]:
        return {"folders": [str(f) for f in self.folders]}


# ---- web ------------------------------------------------------------------------------------

def _public_host(host: str) -> bool:
    """True only when every address the host resolves to is public (no localhost, LAN,
    link-local or metadata addresses), so a web page cannot steer the agent into this
    computer or network, including zhunt's own API."""

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("only http and https web addresses can be opened")
    if not _public_host(parsed.hostname):
        raise ToolError("that address is on this computer or a private network; the agent may only open public websites")
    return parsed.geturl()


class _TextExtractor(HTMLParser):
    """Readable text and the page title from HTML, skipping scripts, styles and navigation."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", joined)).strip()


async def _get(url: str, *, data: dict[str, str] | None = None) -> httpx.Response:
    # redirects are followed by hand so every hop passes the public-address check
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=False) as client:
        for _ in range(5):
            check_url(url)
            response = await (client.post(url, data=data) if data else client.get(url))
            if response.is_redirect and "location" in response.headers:
                url = urllib.parse.urljoin(url, response.headers["location"])
                data = None
                continue
            return response
    raise ToolError("too many redirects")


async def web_search(query: str, limit: int = 6) -> str:
    """Search results (title, address, snippet) from DuckDuckGo's plain-HTML page. No key needed;
    it has no official API, so the agent searches sparingly."""

    query = str(query).strip()[:300]
    if not query:
        raise ToolError("the search needs some words")
    try:
        response = await _get("https://html.duckduckgo.com/html/", data={"q": query})
    except httpx.HTTPError as error:
        raise ToolError(f"search failed ({type(error).__name__})") from None
    page = response.text
    results = []
    for href, title, snippet in re.findall(
        r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', page, re.S
    )[: max(1, min(int(limit or 6), 10))]:
        target = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [html.unescape(href)])[0]
        clean = lambda s: re.sub(r"\s+", " ", re.sub("<[^>]+>", "", html.unescape(s))).strip()  # noqa: E731
        results.append(f"- {clean(title)}\n  {target}\n  {clean(snippet)}")
    if not results:
        return f"No results found for {query!r}."
    return f"Search results for {query!r}:\n" + "\n".join(results)


async def open_page(url: str) -> str:
    """The readable text of a public web page (first ~12,000 characters)."""

    try:
        response = await _get(str(url))
    except httpx.HTTPError as error:
        raise ToolError(f"could not open the page ({type(error).__name__})") from None
    if response.status_code >= 400:
        raise ToolError(f"the page answered HTTP {response.status_code}")
    kind = response.headers.get("content-type", "")
    if "html" not in kind and "text" not in kind and "json" not in kind:
        raise ToolError(f"that address is not a web page ({kind or 'unknown type'})")
    if "html" in kind:
        parser = _TextExtractor()
        parser.feed(response.text[:600_000])
        body = parser.text()
        title = parser.title.strip()
    else:
        body, title = response.text, ""
    return f"Page: {title or url}\nAddress: {response.url}\n\n{body[:MAX_RESULT_CHARS]}"


# ---- files ----------------------------------------------------------------------------------

def _inside(settings: AgentSettings, raw: str, *, must_exist: bool = True) -> Path:
    """Resolve a path the model gave against the allowed folders; refuse anything outside."""

    if not settings.folders:
        raise ToolError("no folders are shared with the agent yet; the user can add one in the Agent window")
    raw = str(raw or "").strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = settings.folders[0] / candidate
    resolved = candidate.resolve(strict=False)
    for root in settings.folders:
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            if must_exist and not resolved.exists():
                raise ToolError(f"{raw!r} does not exist")
            return resolved
    raise ToolError(f"{raw!r} is outside the folders shared with the agent: {', '.join(str(f) for f in settings.folders)}")


def file_tools(settings: AgentSettings) -> list[Tool]:
    async def list_files(path: str = "") -> str:
        folder = _inside(settings, path or str(settings.folders[0]) if settings.folders else path)
        if not folder.is_dir():
            raise ToolError(f"{path!r} is not a folder")
        rows = []
        for child in sorted(folder.iterdir())[:200]:
            if child.name.startswith("."):
                continue
            kind = "folder" if child.is_dir() else f"{child.stat().st_size:,} bytes"
            rows.append(f"- {child.name} ({kind})")
        return f"{folder}:\n" + ("\n".join(rows) or "(empty)")

    async def read_file(path: str) -> str:
        target = _inside(settings, path)
        if target.is_dir():
            raise ToolError(f"{path!r} is a folder; use list_files")
        if target.suffix.lower() not in TEXT_SUFFIXES:
            raise ToolError(f"{target.name} is not a text file the agent can read yet")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"{target.name} is larger than 2 MB")
        text = target.read_text(encoding="utf-8", errors="replace")
        return f"{target} ({len(text):,} characters):\n\n{text[:MAX_RESULT_CHARS]}"

    async def write_file(path: str, content: str) -> str:
        target = _inside(settings, path, must_exist=False)
        if target.exists() and target.is_dir():
            raise ToolError(f"{path!r} is a folder")
        if len(str(content).encode()) > MAX_FILE_BYTES:
            raise ToolError("that is more than 2 MB of text")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return f"Saved {target} ({len(str(content)):,} characters)."

    async def move_file(source: str, destination: str) -> str:
        src = _inside(settings, source)
        dst = _inside(settings, destination, must_exist=False)
        if dst.exists():
            raise ToolError(f"{destination!r} already exists; the agent never overwrites by moving")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return f"Moved {src} to {dst}."

    path_param = {"type": "string", "description": "A path inside a shared folder (absolute, or relative to the first shared folder)."}
    return [
        Tool("list_files", "List the files and folders in a shared folder.", {"type": "object", "properties": {"path": path_param}}, "read", list_files),
        Tool("read_file", "Read a text file (txt, md, csv, json, html, code...) from a shared folder.", {"type": "object", "properties": {"path": path_param}, "required": ["path"]}, "read", read_file),
        Tool("write_file", "Create or overwrite a text file in a shared folder. The user must approve.", {"type": "object", "properties": {"path": path_param, "content": {"type": "string"}}, "required": ["path", "content"]}, "write", write_file),
        Tool("move_file", "Move or rename a file within the shared folders. The user must approve.", {"type": "object", "properties": {"source": path_param, "destination": path_param}, "required": ["source", "destination"]}, "write", move_file),
    ]


def web_tools() -> list[Tool]:
    return [
        Tool("web_search", "Search the web. Returns titles, addresses and snippets.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}, "read", web_search),
        Tool("open_page", "Open a public web page and read its text.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, "read", open_page),
    ]


def all_tools(settings: AgentSettings) -> dict[str, Tool]:
    return {t.name: t for t in [*web_tools(), *file_tools(settings)]}
