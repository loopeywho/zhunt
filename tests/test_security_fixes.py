"""Regressions for security findings from the independent review."""

import os
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import pytest
from ruamel.yaml.error import YAMLError as RuamelYAMLError

from tests.test_engine import ok
from tests.test_workspace_api import ENV, headers, workspace
from zhunt.auth import ensure_master_key, save_env_value, write_private_text


# -- key files are private from the moment they exist ----------------------------------


def test_key_files_are_never_world_readable_even_during_the_write() -> None:
    seen: list[int] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))  # the temp file, pre-rename
        return real_replace(src, dst)

    old = os.umask(0o022)  # a permissive umask would make a default write 0644
    try:
        with TemporaryDirectory() as tmp, patch("zhunt.auth.os.replace", spy):
            path = Path(tmp) / ".zhunt" / "env"
            ensure_master_key(path)
            save_env_value("GROQ_API_KEY", "k", path)
            write_private_text(path, "X=1\n")
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        os.umask(old)

    assert seen and set(seen) == {0o600}


def test_an_existing_loose_env_file_is_replaced_not_rewritten_in_place() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "env"
        path.write_text("OLD=1\n")
        path.chmod(0o644)

        save_env_value("NEW", "2", path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- hostile requests -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_headers",
    [
        {"Authorization": b"Bearer caf\xe9"},
        {"x-api-key": b"\xff\xfe"},
        {"x-litellm-api-key": b"\xe9"},
    ],
)
@pytest.mark.parametrize("path", ["/zhunt/api/status", "/v1/chat/completions"])
def test_non_ascii_credentials_are_a_401_not_a_500(bad_headers, path) -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env=ENV) as (client, _):
        if path == "/zhunt/api/status":
            response = client.get(path, headers=bad_headers)
        else:
            response = client.post(path, headers=bad_headers, json={"model": "zhunt-auto", "messages": []})

    assert response.status_code == 401


def test_compare_bodies_are_capped_and_absurd_nesting_is_a_400() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env=ENV) as (client, _):
        big = client.post(
            "/zhunt/api/compare",
            headers=headers(),
            content=b'{"messages":[{"role":"user","content":"' + b"x" * (1024 * 1024 + 10) + b'"}],"models":[]}',
        )
        deep = client.post("/zhunt/api/compare", headers=headers(), content=b"[" * 200_000)
        layout = client.put("/zhunt/api/layouts/deep", headers=headers(), content=b"[" * 30_000)

    assert big.status_code == 413
    assert deep.status_code == 400
    assert layout.status_code in (400, 413)  # never a 500


def test_a_symlinked_theme_cannot_expose_files_outside_the_themes_dir() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env=ENV) as (client, _):
        secret = Path(client.dirs.themes).parent / "secret.txt"
        client.dirs.themes.mkdir(parents=True, exist_ok=True)
        secret.write_text("TOP-SECRET-MASTER-KEY")
        (client.dirs.themes / "evil.css").symlink_to(secret)

        served = client.get("/zhunt/api/themes/evil", headers=headers())
        listed = client.get("/zhunt/api/themes", headers=headers())

    assert served.status_code == 404
    assert "TOP-SECRET" not in served.text
    assert "evil" not in {t["name"] for t in listed.json()["themes"]}


def test_a_symlinked_layout_is_not_followed() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env=ENV) as (client, _):
        client.dirs.layouts.mkdir(parents=True, exist_ok=True)
        target = client.dirs.layouts.parent / "outside.json"
        target.write_text('{"leak": true}')
        (client.dirs.layouts / "link.json").symlink_to(target)

        response = client.get("/zhunt/api/layouts/link", headers=headers())

    assert response.status_code == 404


def test_the_workspace_page_ships_a_strict_csp_and_the_api_does_not_need_one() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env=ENV) as (client, _):
        page = client.get("/workspace/")
        api = client.get("/zhunt/api/status", headers=headers())

    csp = page.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "img-src 'self' data:", "frame-ancestors 'none'"):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in api.headers


# -- doctor never quotes a config file --------------------------------------------------


def test_doctor_does_not_echo_config_content_when_yaml_is_malformed() -> None:
    from zhunt import doctor

    secret = "sk-SECRET-IN-CONFIG-LINE"
    with patch.object(doctor, "_installed_apps", return_value=("hermes",)), patch.object(
        doctor, "app_drift", side_effect=RuamelYAMLError(f"while parsing near: api_key: {secret}")
    ):
        checks = doctor._app_checks(Path("."), 4000, "k", Path("."), {})

    text = " ".join(f"{c.name} {c.message} {c.fix}" for c in checks)
    assert secret not in text
    assert "cannot parse" in text


# -- no phone-home on import -------------------------------------------------------------


def run_python(code: str, **env: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, **env}, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_importing_the_daemon_opens_no_connection_and_loads_no_litellm() -> None:
    # LiteLLM fetched its price table from GitHub on import; zhunt no longer uses it.
    probe = (
        "import socket, sys\n"
        "calls = []\n"
        "socket.socket.connect = lambda self, *a: calls.append(a)\n"
        "import zhunt.cli, zhunt.server, zhunt.engine, zhunt.workspace_api, zhunt.llm\n"
        "print('litellm' in sys.modules, calls)"
    )

    assert run_python(probe) == "False []"
