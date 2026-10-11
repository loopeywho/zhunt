"""Build the public website: site/ plus a live demo of the workspace at /demo.

The demo is the real workspace UI (zhunt/workspace) with workspace_dev/demo_shim.js
standing in for the local daemon, so it runs on a static host with no keys.

  uv run python scripts/build_site.py            # writes dist/site
  cd dist/site && npx wrangler pages deploy . --project-name zhunt --branch main
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOT = '<script src="js/boot-theme.js"></script>'


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "site", out)
    demo = out / "demo"
    shutil.copytree(ROOT / "zhunt" / "workspace", demo, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "workspace_dev" / "demo_shim.js", demo / "demo-shim.js")
    shutil.copy(ROOT / "workspace_dev" / "demo.css", demo / "demo.css")
    index = demo / "index.html"
    html = index.read_text(encoding="utf-8")
    if BOOT not in html:
        raise SystemExit("workspace index.html changed: cannot find the boot-theme script to inject before")
    html = html.replace(BOOT, '<script src="demo-shim.js"></script>\n  <link rel="stylesheet" href="demo.css">\n  ' + BOOT)
    html = html.replace("<title>zhunt workspace</title>", "<title>zhunt demo</title>")
    index.write_text(html, encoding="utf-8")
    print(f"built {out} ({sum(1 for _ in out.rglob('*') if _.is_file())} files)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "site")
    build(parser.parse_args().out)
