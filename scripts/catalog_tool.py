"""Owner-only tool for the signed free-model catalog feed. Never ship or commit the private key.

The catalog key is separate from the licence key: a leaked catalog key can push models,
not mint licences, and the other way round.

  keygen    create the catalog signing key (PEM, mode 0600) and print the public half
            to paste into zhunt/catalog_feed.py PUBLIC_KEYS
  publish   validate zhunt/catalog.yaml, stamp its version, sign it and write
            site/catalog/latest.json (+ this month's snapshot, the first time each month)

  uv run python scripts/catalog_tool.py keygen --out ~/.zhunt-signing/catalog-2026-10.pem --kid cat-2026-10
  uv run python scripts/catalog_tool.py publish --key ~/.zhunt-signing/catalog-2026-10.pem --kid cat-2026-10
  uv run python scripts/build_site.py && cd dist/site && npx wrangler pages deploy . --project-name zhunt --branch main
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zhunt.catalog_feed import FORMAT, SIGNING_PREFIX, FeedError, verify_feed  # noqa: E402

CATALOG = ROOT / "zhunt" / "catalog.yaml"
OUT = ROOT / "site" / "catalog"
DAY = 86400
# A cached feed stops being used after this, so a replayed old file cannot pin a user
# to stale models forever. Latest is re-published far more often than this.
LATEST_DAYS = 45
MONTHLY_DAYS = 75
_VERSION_LINE = re.compile(r"^version: *\d+ *$", re.MULTILINE)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keygen(out: Path, kid: str) -> None:
    out = out.expanduser()
    if out.exists():
        sys.exit(f"{out} already exists; refusing to overwrite a signing key")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    print(f"private key written to {out} (mode 0600). Back it up somewhere safe; it cannot be recovered.")
    print(f"\nzhunt/catalog_feed.py PUBLIC_KEYS entry:\n    {kid!r}: {b64url(public_raw(private))!r},")


def public_raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_private(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.expanduser().read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit("not an Ed25519 key")
    return key


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def sign_feed(private: Ed25519PrivateKey, kid: str, channel: str, version: int, catalog: dict, *, now: int, days: int) -> bytes:
    payload = {
        "v": 1,
        "kid": kid,
        "channel": channel,
        "version": version,
        "issued_at": now,
        "expires_at": now + days * DAY,
        "catalog": catalog,
    }
    body = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = b64url(private.sign(SIGNING_PREFIX + body.encode()))
    envelope = {"format": FORMAT, "kid": kid, "payload": body, "sig": sig}
    return (json.dumps(envelope, indent=1) + "\n").encode()


def publish(key: Path, kid: str, *, catalog_path: Path, out: Path, now: int, new_snapshot: bool) -> None:
    private = load_private(key)
    text = catalog_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    match = _VERSION_LINE.search(text)
    if not isinstance(data, dict) or match is None:
        sys.exit(f"{catalog_path} needs a top-level `version: <number>` line")
    stamp = int(time.strftime("%Y%m%d%H%M", time.gmtime(now)))
    version = max(stamp, int(data["version"]) + 1)
    catalog = _plain({k: v for k, v in data.items() if k != "version"})

    files: dict[Path, bytes] = {out / "latest.json": sign_feed(private, kid, "latest", version, catalog, now=now, days=LATEST_DAYS)}
    month = time.strftime("%Y-%m", time.gmtime(now))
    snapshot = out / "monthly" / f"{month}.json"
    if new_snapshot or not snapshot.exists():
        monthly = sign_feed(private, kid, "monthly", version, catalog, now=now, days=MONTHLY_DAYS)
        files[snapshot] = monthly
        files[out / "monthly.json"] = monthly

    # Self-check with the public half: the same validation every zhunt runs.
    public = {kid: b64url(public_raw(private))}
    for path, raw in files.items():
        try:
            verify_feed(raw, now=now, public_keys=public)
        except FeedError as error:
            sys.exit(f"refusing to publish {path.name}: {error}")

    for path, raw in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    catalog_path.write_text(_VERSION_LINE.sub(f"version: {version}", text, count=1), encoding="utf-8")
    print(f"stamped {catalog_path.name} with version {version} (the floor the next release ships with)")
    if snapshot not in files:
        print(f"kept this month's snapshot {snapshot.name} (pass --new-snapshot to replace it)")
    print("\nNext: commit zhunt/catalog.yaml and site/catalog/, then deploy:")
    print("  uv run python scripts/build_site.py && cd dist/site && npx wrangler pages deploy . --project-name zhunt --branch main")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", type=Path, required=True)
    k.add_argument("--kid", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("--kid", required=True)
    p.add_argument("--catalog", type=Path, default=CATALOG)
    p.add_argument("--out", type=Path, default=OUT, help="where to write the feed files (default site/catalog)")
    p.add_argument("--new-snapshot", action="store_true", help="replace this month's snapshot with this catalog")
    args = parser.parse_args()
    if args.cmd == "keygen":
        keygen(args.out, args.kid)
    else:
        publish(args.key, args.kid, catalog_path=args.catalog, out=args.out, now=int(time.time()), new_snapshot=args.new_snapshot)


if __name__ == "__main__":
    main()
