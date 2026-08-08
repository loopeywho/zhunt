"""Optional cross-platform tray/menu-bar indicator for Zhunt."""

from __future__ import annotations

import secrets
import socket
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from zhunt.dashboard import create_dashboard_app


def daemon_reachable(host: str = "127.0.0.1", port: int = 4000) -> bool:
    """Return whether the local daemon accepts a TCP connection."""

    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def tray_state(host: str = "127.0.0.1", port: int = 4000) -> tuple[str, str]:
    """Return a display label and color for the tray indicator."""

    if daemon_reachable(host, port):
        return "Zhunt Active", "#16804b"
    return "Zhunt Offline", "#b42318"


def run_tray(
    *,
    daemon_host: str = "127.0.0.1",
    daemon_port: int = 4000,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8402,
    telemetry_path: Path | None = None,
    registry_path: Path | None = None,
    provider_id: str | None = None,
) -> None:
    """Run the optional local tray indicator and its dashboard server."""

    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError(
            "tray support is optional; install it with `pip install 'zhunt[desktop]'`"
        ) from error

    import uvicorn

    if dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("tray dashboard must remain loopback-only")
    token = secrets.token_urlsafe(24)
    dashboard_url = (
        f"http://{dashboard_host}:{dashboard_port}/?token={token}"
    )
    dashboard = create_dashboard_app(
        dashboard_token=token,
        telemetry_path=telemetry_path,
        registry_path=registry_path,
        provider_id=provider_id,
        daemon_host=daemon_host,
        daemon_port=daemon_port,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            dashboard,
            host=dashboard_host,
            port=dashboard_port,
            log_level="warning",
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    def icon_image(color: str) -> Any:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=color)
        return image

    def open_dashboard(_icon: Any, _item: Any) -> None:
        webbrowser.open(dashboard_url)

    def refresh(icon: Any) -> None:
        while True:
            label, color = tray_state(daemon_host, daemon_port)
            icon.icon = icon_image(color)
            icon.title = label
            time.sleep(5)

    label, color = tray_state(daemon_host, daemon_port)
    icon = pystray.Icon(
        "zhunt",
        icon_image(color),
        label,
        menu=pystray.Menu(
            pystray.MenuItem("Open dashboard", open_dashboard),
            pystray.MenuItem(
                "Quit Zhunt indicator",
                lambda _icon, _item: icon.stop(),
            ),
        ),
    )
    threading.Thread(target=refresh, args=(icon,), daemon=True).start()
    try:
        icon.run()
    finally:
        server.should_exit = True
        server_thread.join(timeout=2)
