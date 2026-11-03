"""One-step local workspace launcher; server lifetime follows this process."""
from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import typer


def desktop(
    port: int | None = typer.Option(None, min=1, max=65535),
    browser: bool = typer.Option(True, "--browser/--no-browser"),
) -> None:
    """Start the local workspace and open it. Close this terminal to stop Zhunt."""
    import uvicorn
    from zhunt.auth import read_master_key, save_env_value
    from zhunt.ports import resolve_daemon_port
    from zhunt.server import create_proxy_app

    selected, _ = resolve_daemon_port(requested=port)
    app = create_proxy_app(telemetry_path=Path.home() / ".zhunt" / "telemetry.jsonl")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=selected))
    finished = threading.Event()

    def ready() -> None:
        while not finished.wait(0.05):
            if server.started:
                save_env_value("ZHUNT_PORT", str(selected), Path.home() / ".zhunt" / "env")
                key = read_master_key()
                url = f"http://127.0.0.1:{selected}/workspace/#key={quote(key, safe='')}"
                typer.echo(f"Zhunt is ready at http://127.0.0.1:{selected}/workspace/")
                typer.echo("Keep this window open. Press Ctrl+C to stop. Add free provider keys in Connect.")
                if browser and not webbrowser.open(url):
                    typer.echo("Browser could not open. Run `zhunt open --print` for your private workspace URL.", err=True)
                return

    worker = threading.Thread(target=ready, daemon=True)
    worker.start()
    try:
        server.run()
    finally:
        finished.set()
        worker.join(timeout=2)
