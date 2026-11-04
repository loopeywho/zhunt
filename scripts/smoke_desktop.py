"""Exercise an extracted desktop launcher against a new, empty user home.

Usage: python scripts/smoke_desktop.py /path/to/zhunt [--ui]
--ui requires Playwright and installed Google Chrome. No provider calls are made.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--terminal", action="store_true", help="Exercise a real POSIX Engineering PTY")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    home = args.output.resolve() / "clean-home"
    home.mkdir(exist_ok=False)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    for name in list(env):
        if name.endswith("_API_KEY") or name.startswith("ZHUNT_"):
            env.pop(name)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    exe = str(args.executable.resolve())
    command = [exe, "desktop", "--no-browser", "--port", str(port)]
    if args.executable.suffix in (".command", ".sh", ".cmd"):
        command.remove("desktop")
    if os.name == "nt" and args.executable.suffix == ".cmd":
        command = ["cmd.exe", "/d", "/c", *command]
    with (args.output / "startup.log").open("w") as log:
        child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(240):
                if child.poll() is not None:
                    raise RuntimeError(f"launcher exited {child.returncode}; inspect startup.log")
                try:
                    with urllib.request.urlopen(base + "/workspace/", timeout=1) as response:
                        html = response.read().decode()
                    if (home / ".zhunt/env").exists():
                        break
                except OSError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("workspace did not become ready")
            assert "js/main.js" in html
            values = dict(line.split("=", 1) for line in (home / ".zhunt/env").read_text().splitlines() if "=" in line)
            key = values["ZHUNT_MASTER_KEY"].strip("\"'")
            req = urllib.request.Request(base + "/zhunt/api/status", headers={"Authorization": "Bearer " + key})
            with urllib.request.urlopen(req) as response:
                status = json.load(response)
            assert status
            evidence = {"platform": __import__("platform").platform(), "executable": exe, "workspace_http": 200, "authenticated_status_http": 200, "fresh_home": str(home), "ui_tested": False}
            if args.terminal:
                from smoke_terminal import run_terminal
                evidence["terminal"] = run_terminal(base, key, home)
            if args.ui:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel="chrome")
                    page = browser.new_page(viewport={"width": 1280, "height": 800})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base + "/workspace/#key=" + key)
                    page.wait_for_timeout(2500)
                    page.get_by_text("Your agent", exact=True).wait_for()
                    assert "online" in page.locator("body").inner_text()
                    assert not errors, errors
                    evidence["body_text"] = page.locator("body").inner_text()
                    evidence["ui_tested"] = True
                    evidence["page_errors"] = errors
                    page.screenshot(path=str(args.output / "workspace.png"))
                    browser.close()
            (args.output / "result.json").write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence, indent=2))
        finally:
            if os.name == "nt" and args.executable.suffix == ".cmd":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL)
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
