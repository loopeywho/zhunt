"""Local daemon authentication key management."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def ensure_master_key(env_path: Path | None = None) -> str:
    """Load or create the local Zhunt/LiteLLM master key."""

    path = (env_path or Path.home() / ".zhunt" / "env").expanduser()
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
