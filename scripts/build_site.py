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


def build(out: Path, download_base_url: str | None = None) -> None:
    # Never publish download buttons without their actual artifacts and checksums.
    import hashlib
    import re
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    downloads = sorted(set(re.findall(r'href="downloads/([^"/]+)"', html)))
    for name in downloads:
        artifact = ROOT / "dist" / name
        if not artifact.is_file():
            raise SystemExit(f"Missing desktop release artifact: {artifact}. Build/retrieve all packages before publishing.")
        if not name.endswith(".sha256"):
            expected = artifact.with_name(name + ".sha256").read_text(encoding="utf-8-sig").split()[0]
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                raise SystemExit(f"Checksum mismatch: {artifact}")
    if download_base_url:
        from urllib.request import Request, urlopen
        if not download_base_url.startswith("https://github.com/loopeywho/zhunt/releases/download/"):
            raise SystemExit("Use the exact published Zhunt GitHub release download base URL.")
        for name in downloads:
            url = download_base_url.rstrip("/") + "/" + name
            # Check the actual public bytes, not merely the existence of a release page.
            with urlopen(Request(url, headers={"User-Agent": "Zhunt-release-verifier"}), timeout=120) as response:
                remote_digest = hashlib.file_digest(response, "sha256").hexdigest()
            if remote_digest != hashlib.sha256((ROOT / "dist" / name).read_bytes()).hexdigest():
                raise SystemExit(f"Published artifact differs from reviewed artifact: {url}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "site", out)
    if download_base_url:
        html = html.replace('href="downloads/', 'href="' + download_base_url.rstrip("/") + "/")
        (out / "index.html").write_text(html, encoding="utf-8")
    else:
        (out / "downloads").mkdir(exist_ok=True)
        for name in downloads:
            shutil.copy2(ROOT / "dist" / name, out / "downloads" / name)
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
    parser.add_argument("--download-base-url", help="Published GitHub release base; every download is verified against local reviewed artifacts")
    args = parser.parse_args()
    build(args.out, args.download_base_url)
