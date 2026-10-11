"""Owner-only tool for zhunt licence keys. Never ship or commit the private key.

  keygen   create an Ed25519 signing key (PEM, mode 0600) and print the public half
           to paste into zhunt/licence.py PUBLIC_KEYS, plus the secret for the website
  issue    sign a licence by hand (comps, refunds, testing)

  uv run python scripts/licence_tool.py keygen --out ~/.zhunt-signing/licence-2026-10.pem --kid 2026-10
  uv run python scripts/licence_tool.py issue --key ~/.zhunt-signing/licence-2026-10.pem --kid 2026-10 \\
      --email someone@example.com --days 30
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PREFIX = "ZHUNT1"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(private: Ed25519PrivateKey, payload: dict) -> str:
    body = f"{PREFIX}.{b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    return f"{body}.{b64url(private.sign(body.encode()))}"


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
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(f"private key written to {out} (mode 0600). Back it up somewhere safe; it cannot be recovered.")
    print(f"\nzhunt/licence.py PUBLIC_KEYS entry:\n    {kid!r}: {b64url(public)!r},")
    print("\nWebsite secrets (Cloudflare Pages > Settings > Environment variables, encrypted):")
    print(f"  LICENCE_KID         = {kid}")
    print("  LICENCE_PRIVATE_KEY = <run: scripts/licence_tool.py worker-secret --key ...>")


def load_private(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.expanduser().read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit("not an Ed25519 key")
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", type=Path, required=True)
    k.add_argument("--kid", required=True)
    i = sub.add_parser("issue")
    i.add_argument("--key", type=Path, required=True)
    i.add_argument("--kid", required=True)
    i.add_argument("--email", required=True)
    i.add_argument("--days", type=int, default=30)
    i.add_argument("--plan", default="pro")
    i.add_argument("--sub", default=None, help="Stripe subscription id, if any (lets `zhunt licence renew` work)")
    w = sub.add_parser("worker-secret", help="print the private key as base64 PKCS8 DER for the website secret")
    w.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "keygen":
        keygen(args.out, args.kid)
    elif args.cmd == "issue":
        now = int(time.time())
        payload = {"v": 1, "kid": args.kid, "plan": args.plan, "email": args.email, "iat": now, "exp": now + args.days * 86400}
        if args.sub:
            payload["sub"] = args.sub
        print(sign(load_private(args.key), payload))
    else:
        der = load_private(args.key).private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        print(base64.b64encode(der).decode())


if __name__ == "__main__":
    main()
