"""Local daemon authentication key management."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path


def write_private_text(path: Path, text: str) -> None:
    """Write ``text`` so the file is never readable by others, even briefly.

    The directory is created 0700 and the content goes to a 0600 temp file that
    replaces the target atomically, instead of writing with the default umask and
    tightening the mode afterwards.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def ensure_master_key(env_path: Path | None = None) -> str:
    """Load or create the local Zhunt/LiteLLM master key."""

    path = (env_path or Path.home() / ".zhunt" / "env").expanduser()
    _load_env_file(path)
    key: str | None = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name in {"ZHUNT_MASTER_KEY", "LITELLM_MASTER_KEY"}:
                value = value.strip().strip('"').strip("'")
                if value:
                    key = value
                    break
    if key is None:
        key = f"sk-zhunt-{secrets.token_urlsafe(32)}"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        write_private_text(path, f"{existing}ZHUNT_MASTER_KEY={key}\n")
    os.environ["ZHUNT_MASTER_KEY"] = key
    os.environ["LITELLM_MASTER_KEY"] = key
    return key


def read_env_values(env_path: Path | None = None) -> dict[str, str]:
    """Read KEY=value entries from the env file without touching os.environ."""

    path = (env_path or Path.home() / ".zhunt" / "env").expanduser()
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if not separator or not name or name.startswith("#"):
            continue
        values[name] = value.strip().strip('"').strip("'")
    return values


def read_master_key(env_path: Path | None = None) -> str | None:
    """Return the saved master key, or None. Never creates or modifies files."""

    values = read_env_values(env_path)
    for name in ("ZHUNT_MASTER_KEY", "LITELLM_MASTER_KEY"):
        if values.get(name):
            return values[name]
    return None


def save_env_value(name: str, value: str, env_path: Path | None = None) -> None:
    """Persist one local configuration value without exposing it in logs."""

    if not name or "=" in name or any(character.isspace() for character in name):
        raise ValueError("environment variable name is invalid")
    if "\n" in value or "\r" in value:
        raise ValueError("environment variable value must be one line")
    path = (env_path or Path.home() / ".zhunt" / "env").expanduser()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    replacement = f'{name}="{value}"'
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if separator and key == name:
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    write_private_text(path, "\n".join(lines).rstrip("\n") + "\n")
    os.environ[name] = value


def _load_env_file(path: Path) -> None:
    """Load simple KEY=value entries, preserving already-exported values."""

    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if not separator or not name or name.startswith("#"):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(name, value)
