"""Local daemon authentication key management."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


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
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(
            f"{existing}ZHUNT_MASTER_KEY={key}\n",
            encoding="utf-8",
        )
    try:
        path.chmod(0o600)
        path.parent.chmod(0o700)
    except OSError:
        pass
    os.environ["ZHUNT_MASTER_KEY"] = key
    os.environ["LITELLM_MASTER_KEY"] = key
    return key


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
        path.parent.chmod(0o700)
    except OSError:
        pass
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
