"""Shared helpers for the launch/doctor/open tests. Everything is hermetic:
temp HOME trees, a throwaway local HTTP server, and stub executables."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

KEY = "sk-zhunt-TESTKEY-must-never-leak"

STUB_SOURCE = """#!{python}
import json, os, signal, sys, time
out = os.environ.get("STUB_OUT")
if out:
    with open(out, "w") as handle:
        json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, handle)
mode = os.environ.get("STUB_MODE", "")
if mode == "wait":
    def done(code, name):
        def handler(signum, frame):
            open(os.environ["STUB_SIGNAL_FILE"], "w").write(name)
            sys.exit(code)
        return handler
    signal.signal(signal.SIGTERM, done(42, "TERM"))
    signal.signal(signal.SIGINT, done(43, "INT"))
    open(os.environ["STUB_READY_FILE"], "w").write("ready")
    time.sleep(30)
    sys.exit(99)
if mode == "selfkill":
    os.kill(os.getpid(), signal.SIGKILL)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


def write_stub(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text(STUB_SOURCE.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def fake_daemon():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def make_home(root: Path, *, port: int | None, key: str | None = KEY, extra: str = "") -> Path:
    home = root / "home"
    (home / ".zhunt").mkdir(parents=True)
    lines = []
    if key is not None:
        lines.append(f'ZHUNT_MASTER_KEY="{key}"')
    if port is not None:
        lines.append(f'ZHUNT_PORT="{port}"')
    (home / ".zhunt" / "env").write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
    return home


def snapshot(*roots: Path) -> dict[str, str]:
    """Every file under the roots -> content hash and mtime, to detect any write."""

    state: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state[str(path)] = f"{digest}:{stat.st_mtime_ns}"
            else:
                state[str(path)] = "dir"
    return state


def clean_environ(home: Path, stub_dir: Path | None = None, **extra: str) -> dict[str, str]:
    """A minimal environment: no inherited provider keys, flags or app routing."""

    path = os.environ.get("PATH", "/usr/bin:/bin")
    if stub_dir is not None:
        path = f"{stub_dir}{os.pathsep}{path}"
    base = {"HOME": str(home), "PATH": path, "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    base.update(extra)
    return base


def read_stub_output(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
