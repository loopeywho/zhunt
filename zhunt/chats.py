"""Saved chats and Projects: local files in ~/.zhunt/chats, never sent anywhere.

One file per chat (0600), a small index.json so listing stays fast however many chats
there are, and projects.json for named groups with optional instructions. The index is
rebuilt from the chat files when it is missing or damaged. A chat's `rev` rises on every
save and must be echoed back, so a second window holding an older copy gets a 409 instead
of silently overwriting newer messages.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from zhunt.server import _authenticate_local_request
from zhunt.workspace_api import _json_body, _write_private

ID = re.compile(r"[a-f0-9]{16}")
MAX_BODY_BYTES = 1024 * 1024
MAX_MESSAGES = 400
MAX_CONTENT_CHARS = 100_000
MAX_CHATS = 1000
MAX_PROJECTS = 50
MAX_TITLE = 120
MAX_NAME = 60
MAX_INSTRUCTIONS = 4000
ROLES = ("user", "assistant")


class ChatError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _text(value: Any, limit: int, what: str, *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise ChatError(400, f"{what} is required")
        return ""
    if not isinstance(value, str):
        raise ChatError(400, f"{what} must be text")
    value = value.strip()
    if required and not value:
        raise ChatError(400, f"{what} is required")
    return value[:limit]


def _safe_read(path: Path) -> Any:
    """JSON from a regular file, or None. Symlinks and unreadable files count as absent."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _meta(chat: dict[str, Any]) -> dict[str, Any]:
    return {"id": chat["id"], "title": chat["title"], "project": chat.get("project"),
            "updated": chat["updated"], "count": len(chat["messages"]), "rev": chat["rev"]}


class ChatStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] | None = None

    # ---- paths and index -----------------------------------------------------------------

    def _chat_path(self, chat_id: str) -> Path:
        if not ID.fullmatch(chat_id):
            raise ChatError(400, "chat ids are 16 lowercase hex characters")
        return self.directory / f"{chat_id}.json"

    def _files(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return [p for p in self.directory.iterdir() if p.suffix == ".json" and ID.fullmatch(p.stem)
                and p.is_file() and not p.is_symlink()]

    def index(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            if self._index is None:
                stored = _safe_read(self.directory / "index.json")
                if not isinstance(stored, dict) or set(stored) != {p.stem for p in self._files()}:
                    stored = self._rebuild()
                self._index = stored
            return self._index

    def _rebuild(self) -> dict[str, dict[str, Any]]:
        rebuilt: dict[str, dict[str, Any]] = {}
        for path in self._files():
            chat = self._valid(_safe_read(path), path.stem)
            if chat:
                rebuilt[chat["id"]] = _meta(chat)
        self._write_index(rebuilt)
        return rebuilt

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        _write_private(self.directory / "index.json", json.dumps(index).encode())

    @staticmethod
    def _valid(chat: Any, chat_id: str) -> dict[str, Any] | None:
        """A stored chat that has the shape the app wrote, else None (it is skipped, not repaired)."""
        if not isinstance(chat, dict) or chat.get("id") != chat_id or not isinstance(chat.get("messages"), list):
            return None
        if not all(isinstance(chat.get(k), str) for k in ("title",)) or not isinstance(chat.get("rev"), int) \
                or not isinstance(chat.get("updated"), (int, float)):
            return None
        return chat

    # ---- chats ---------------------------------------------------------------------------

    def list(self, project: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            rows = sorted(self.index().values(), key=lambda m: m["updated"], reverse=True)
            if project == "none":
                rows = [m for m in rows if not m.get("project")]
            elif project:
                rows = [m for m in rows if m.get("project") == project]
            rows = [dict(m) for m in rows]
        needle = (q or "").strip().lower()
        if not needle:
            return rows
        found = []
        # Search reads the files, so it runs outside the lock: saves are never held up behind it. Files
        # are replaced atomically, so a read sees a whole old or new chat; a deleted one is just skipped.
        for meta in rows:
            chat = self._valid(_safe_read(self._chat_path(meta["id"])), meta["id"])
            if not chat:
                continue
            if needle in chat["title"].lower():
                found.append({**meta, "snippet": chat["title"]})
                continue
            for message in chat["messages"]:
                content = str(message.get("content", ""))
                at = content.lower().find(needle)
                if at >= 0:
                    start = max(0, at - 40)
                    found.append({**meta, "snippet": content[start:at + len(needle) + 80].replace("\n", " ")})
                    break
        return found

    def get(self, chat_id: str) -> dict[str, Any]:
        chat = self._valid(_safe_read(self._chat_path(chat_id)), chat_id)
        if not chat:
            raise ChatError(404, "no saved chat with that id")
        return chat

    def put(self, chat_id: str, body: Any) -> dict[str, Any]:
        path = self._chat_path(chat_id)
        if not isinstance(body, dict):
            raise ChatError(400, "send a JSON object")
        messages = self._messages(body.get("messages"))
        project = body.get("project")
        if project is not None and not (isinstance(project, str) and ID.fullmatch(project)):
            raise ChatError(400, "project must be a project id or null")
        alias = _text(body.get("alias"), 40, "alias")
        with self.lock:
            index = self.index()
            existing = self._valid(_safe_read(path), chat_id)
            if existing:
                if body.get("rev") != existing["rev"]:
                    raise ChatError(409, "this chat was saved from another window; reopen it from History")
                created, rev = existing.get("created", existing["updated"]), existing["rev"] + 1
            else:
                if body.get("rev") is not None:  # the client held a saved copy that is gone: do not bring it back
                    raise ChatError(404, "this chat was deleted from another window")
                if len(index) >= MAX_CHATS:
                    raise ChatError(409, f"there are already {MAX_CHATS} saved chats; delete some from History")
                created, rev = time.time(), 1
            title = _text(body.get("title"), MAX_TITLE, "title") or self._title(messages)
            chat = {"id": chat_id, "title": title, "project": project, "alias": alias, "created": created,
                    "updated": time.time(), "rev": rev, "messages": messages}
            _write_private(path, json.dumps(chat).encode())
            index[chat_id] = _meta(chat)
            self._write_index(index)
            return {"id": chat_id, "rev": rev, "updated": chat["updated"]}

    @staticmethod
    def _title(messages: list[dict[str, Any]]) -> str:
        first = next((m["content"] for m in messages if m["role"] == "user"), "New chat")
        line = " ".join(first.split())
        return (line[:60] + "…") if len(line) > 60 else (line or "New chat")

    @staticmethod
    def _messages(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ChatError(400, "there is nothing to save yet")
        if len(raw) > MAX_MESSAGES:
            raise ChatError(413, f"a saved chat holds at most {MAX_MESSAGES} messages; start a new chat")
        out = []
        for item in raw:
            if not isinstance(item, dict) or item.get("role") not in ROLES or not isinstance(item.get("content"), str):
                raise ChatError(400, "each message needs a role (user or assistant) and text content")
            message = {"role": item["role"], "content": item["content"][:MAX_CONTENT_CHARS]}
            if isinstance(item.get("model"), str) and item["model"]:
                message["model"] = item["model"][:100]
            out.append(message)
        return out

    def delete(self, chat_id: str) -> None:
        path = self._chat_path(chat_id)
        with self.lock:
            index = self.index()
            if chat_id not in index and not path.exists():
                raise ChatError(404, "no saved chat with that id")
            path.unlink(missing_ok=True)
            index.pop(chat_id, None)
            self._write_index(index)

    # ---- projects ------------------------------------------------------------------------

    def projects(self) -> list[dict[str, Any]]:
        with self.lock:
            stored = _safe_read(self.directory / "projects.json")
            rows = stored.get("projects") if isinstance(stored, dict) else None
            good = [p for p in rows or [] if isinstance(p, dict) and isinstance(p.get("id"), str) and ID.fullmatch(p["id"])
                    and isinstance(p.get("name"), str) and isinstance(p.get("instructions", ""), str)]
            return sorted(good, key=lambda p: p["name"].lower())

    def put_project(self, project_id: str, body: Any) -> dict[str, Any]:
        if not ID.fullmatch(project_id):
            raise ChatError(400, "project ids are 16 lowercase hex characters")
        if not isinstance(body, dict):
            raise ChatError(400, "send a JSON object")
        name = _text(body.get("name"), MAX_NAME, "name", required=True)
        instructions = _text(body.get("instructions"), MAX_INSTRUCTIONS, "instructions")
        with self.lock:
            rows = self.projects()
            if not any(p["id"] == project_id for p in rows) and len(rows) >= MAX_PROJECTS:
                raise ChatError(409, f"at most {MAX_PROJECTS} projects")
            if any(p["id"] != project_id and p["name"].lower() == name.lower() for p in rows):
                raise ChatError(409, "a project with that name already exists")
            project = {"id": project_id, "name": name, "instructions": instructions}
            rows = [p for p in rows if p["id"] != project_id] + [project]
            _write_private(self.directory / "projects.json", json.dumps({"projects": rows}).encode())
            return project

    def delete_project(self, project_id: str) -> None:
        if not ID.fullmatch(project_id):
            raise ChatError(400, "project ids are 16 lowercase hex characters")
        with self.lock:
            rows = self.projects()
            if not any(p["id"] == project_id for p in rows):
                raise ChatError(404, "no project with that id")
            # chats keep the id (cheaper than rewriting every file); a missing project shows as "no project"
            _write_private(self.directory / "projects.json",
                           json.dumps({"projects": [p for p in rows if p["id"] != project_id]}).encode())


def mount_chats(app: Any, directory: Path) -> ChatStore:
    store = ChatStore(directory)
    router = APIRouter(prefix="/zhunt/api", dependencies=[Depends(_authenticate_local_request)])

    def run(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except ChatError as error:
            raise HTTPException(error.status, error.detail) from None
        except OSError:
            raise HTTPException(500, "could not read or write the saved chats folder") from None

    @router.get("/chats")
    def list_chats(project: str | None = None, q: str | None = None) -> dict[str, Any]:
        return {"chats": run(store.list, project, q[:200] if q else None), "limit": MAX_CHATS}

    @router.get("/chats/{chat_id}")
    def get_chat(chat_id: str) -> Any:
        return run(store.get, chat_id)

    @router.put("/chats/{chat_id}")
    async def put_chat(chat_id: str, request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=MAX_BODY_BYTES)
        return run(store.put, chat_id, body)

    @router.delete("/chats/{chat_id}")
    def delete_chat(chat_id: str) -> dict[str, Any]:
        run(store.delete, chat_id)
        return {"id": chat_id}

    @router.get("/projects")
    def list_projects() -> dict[str, Any]:
        return {"projects": run(store.projects), "limit": MAX_PROJECTS}

    @router.put("/projects/{project_id}")
    async def put_project(project_id: str, request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=16 * 1024)
        return run(store.put_project, project_id, body)

    @router.delete("/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, Any]:
        run(store.delete_project, project_id)
        return {"id": project_id}

    app.include_router(router)
    app.state.chats = store
    return store
