"""Safe localhost port selection and persistence for Zhunt."""

from __future__ import annotations

import socket
from pathlib import Path

from zhunt.auth import save_env_value


DEFAULT_DAEMON_PORT = 4000
_LOOPBACK_HOST = "127.0.0.1"


def configured_port(home: Path | None = None) -> int | None:
    """Return the saved daemon port, if it is a valid TCP port."""

    path = (home or Path.home()).expanduser() / ".zhunt" / "env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name == "ZHUNT_PORT":
            try:
                port = int(value.strip().strip('"').strip("'"))
            except ValueError:
                return None
            if 1 <= port <= 65_535:
                return port
    return None


def port_available(port: int, *, host: str = _LOOPBACK_HOST) -> bool:
    """Check whether a port can be bound on the requested host."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except PermissionError:
        # Some sandboxed test runners deny bind probes even for high ports;
        # let the actual server bind remain the final authority.
        return True
    except OSError:
        return False
    return True


def choose_loopback_port(
    preferred: int = DEFAULT_DAEMON_PORT,
    *,
    attempts: int = 100,
) -> int:
    """Choose an available loopback port, preferring the standard port."""

    if not 1 <= preferred <= 65_535:
        raise ValueError("preferred port must be between 1 and 65535")
    for port in range(preferred, min(preferred + attempts, 65_536)):
        if port_available(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


def resolve_daemon_port(
    *,
    home: Path | None = None,
    requested: int | None = None,
    persist: bool = False,
) -> tuple[int, bool]:
    """Resolve a safe local daemon port and report whether fallback occurred.

    A saved port is preferred when no explicit port is supplied. If that port
    is occupied, a nearby loopback port is selected. The caller can persist the
    result after it has configured app endpoints with the same value.
    """

    home_path = (home or Path.home()).expanduser()
    preferred = requested or configured_port(home_path) or DEFAULT_DAEMON_PORT
    if port_available(preferred):
        selected = preferred
        fell_back = False
    else:
        selected = choose_loopback_port(preferred + 1 if preferred < 65_535 else 1)
        fell_back = True
    if persist:
        save_env_value("ZHUNT_PORT", str(selected), home_path / ".zhunt" / "env")
    return selected, fell_back
