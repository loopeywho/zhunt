"""Plans and licence keys. Verified offline: zhunt never phones home to check a licence.

A licence key is ``ZHUNT1.<payload>.<signature>`` where payload is base64url JSON
``{"v": 1, "kid": ..., "plan": "pro", "email": ..., "sub": ..., "iat": ..., "exp": ...}``
and signature is an Ed25519 signature (base64url) over the bytes ``ZHUNT1.<payload>``.
Keys are issued by the licence function on the website after a Stripe checkout
(site/functions/api/licence.js) or by hand with scripts/licence_tool.py.

zhunt is open source, so anyone can patch these checks out; the licence exists so that
people who want the Pro features can pay for them, not as copy protection.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from zhunt.auth import write_private_text

PREFIX = "ZHUNT1"
UPGRADE_URL = "https://zhunt.kindredwildcat.com/#pricing"
# A lapsed subscription keeps Pro for this long after the key's expiry, so a renewal
# that is a few days late (or a laptop that was offline) does not switch features off.
GRACE_SECONDS = 7 * 24 * 3600

# Public halves of the signing keys, by key id. The private key never enters the repo.
PUBLIC_KEYS: dict[str, str] = {
    "2026-10": "S7avIilATZM8VsRJfrxOgZLflO9-9MPdxQusE1GTyrM",
}

FEATURES = {
    "free": frozenset(),
    "pro": frozenset({"premium_fallback"}),
}
FEATURE_LABELS = {
    "premium_fallback": "Premium models (GPT, Claude, Gemini Pro) when free models run out, under your monthly cap",
}


class LicenceError(ValueError):
    """A licence key is malformed, forged or expired; the message is safe to show."""


@dataclass(frozen=True)
class Plan:
    name: str = "free"
    email: str | None = None
    expires_at: float | None = None
    in_grace: bool = False
    problem: str | None = None  # why a stored key is not in effect, if one exists
    features: frozenset[str] = field(default_factory=frozenset)

    def has(self, feature: str) -> bool:
        return feature in self.features

    def to_api(self) -> dict[str, object]:
        return {
            "plan": self.name,
            "email": self.email,
            "expires_at": self.expires_at,
            "in_grace": self.in_grace,
            "problem": self.problem,
            "features": sorted(self.features),
            "feature_labels": FEATURE_LABELS,
            "upgrade_url": UPGRADE_URL,
        }


FREE = Plan()


def _b64decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise LicenceError("the licence key is damaged (bad encoding)") from error


def verify(key: str, *, now: float | None = None, public_keys: dict[str, str] | None = None) -> Plan:
    """Return the plan a key grants, or raise LicenceError."""

    now = time.time() if now is None else now
    keys = PUBLIC_KEYS if public_keys is None else public_keys
    parts = key.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenceError("this is not a zhunt licence key (it should start with ZHUNT1.)")
    payload_raw = _b64decode(parts[1])
    try:
        payload = json.loads(payload_raw)
    except ValueError as error:
        raise LicenceError("the licence key is damaged (bad payload)") from error
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise LicenceError("this licence key is from a newer zhunt; update zhunt")
    public = keys.get(str(payload.get("kid")))
    if public is None:
        raise LicenceError("this licence key was signed by an unknown key; update zhunt")
    try:
        Ed25519PublicKey.from_public_bytes(_b64decode(public)).verify(
            _b64decode(parts[2]), f"{parts[0]}.{parts[1]}".encode()
        )
    except (InvalidSignature, ValueError) as error:
        raise LicenceError("the licence key's signature does not match (it was altered or mistyped)") from error
    plan = str(payload.get("plan"))
    if plan not in FEATURES or plan == "free":
        raise LicenceError(f"unknown plan {plan!r} in the licence key; update zhunt")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise LicenceError("the licence key has no expiry date")
    if now > exp + GRACE_SECONDS:
        raise LicenceError("this licence key has expired; renew it with `zhunt licence renew`")
    email = payload.get("email")
    return Plan(
        name=plan,
        email=email if isinstance(email, str) else None,
        expires_at=float(exp),
        in_grace=now > exp,
        features=FEATURES[plan],
    )


def subscription_of(key: str) -> str | None:
    """The Stripe subscription id inside a key (unverified), used to renew it."""

    try:
        payload = json.loads(_b64decode(key.strip().split(".")[1]))
    except (LicenceError, IndexError, ValueError):
        return None
    sub = payload.get("sub") if isinstance(payload, dict) else None
    return sub if isinstance(sub, str) else None


def licence_path(home: Path | None = None) -> Path:
    override = os.environ.get("ZHUNT_LICENCE_FILE") if home is None else None
    return Path(override) if override else (home or Path.home()) / ".zhunt" / "licence"


def stored_key(home: Path | None = None) -> str | None:
    env = os.environ.get("ZHUNT_LICENCE", "").strip()
    if env:
        return env
    try:
        return licence_path(home).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def current_plan(home: Path | None = None, *, now: float | None = None) -> Plan:
    """The plan in effect: Pro with a valid stored key, otherwise Free (with the reason)."""

    key = stored_key(home)
    if not key:
        return FREE
    try:
        return verify(key, now=now)
    except LicenceError as error:
        return Plan(problem=str(error))


def save_key(key: str, home: Path | None = None) -> Plan:
    """Verify then store a key (mode 0600). Raises LicenceError without storing a bad key."""

    plan = verify(key)
    write_private_text(licence_path(home), key.strip() + "\n")
    return plan


def remove_key(home: Path | None = None) -> bool:
    try:
        licence_path(home).unlink()
        return True
    except FileNotFoundError:
        return False
