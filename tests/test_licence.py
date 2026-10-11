import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zhunt import licence as lic

PRIVATE = Ed25519PrivateKey.generate()
PUBLIC = base64.urlsafe_b64encode(
    PRIVATE.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
).rstrip(b"=").decode()
KEYS = {"test": PUBLIC}
NOW = 1_800_000_000


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_key(private=PRIVATE, **over) -> str:
    payload = {"v": 1, "kid": "test", "plan": "pro", "email": "a@b.c", "sub": "sub_123", "iat": NOW, "exp": NOW + 30 * 86400, **over}
    body = f"ZHUNT1.{b64(json.dumps(payload).encode())}"
    return f"{body}.{b64(private.sign(body.encode()))}"


class VerifyTests(unittest.TestCase):
    def test_a_valid_key_grants_pro_features(self) -> None:
        plan = lic.verify(make_key(), now=NOW, public_keys=KEYS)
        self.assertEqual(plan.name, "pro")
        self.assertTrue(plan.has("premium_fallback") and plan.has("theme_pack"))
        self.assertFalse(plan.in_grace)

    def test_forged_tampered_or_foreign_keys_are_refused(self) -> None:
        good = make_key()
        cases = {
            "other signer": make_key(private=Ed25519PrivateKey.generate()),
            "edited payload": "ZHUNT1." + b64(json.dumps({"v": 1, "kid": "test", "plan": "pro", "exp": NOW * 2}).encode()) + "." + good.split(".")[2],
            "unknown kid": make_key(kid="nope"),
            "not a key": "hello",
            "unknown plan": make_key(plan="enterprise"),
            "free plan": make_key(plan="free"),
        }
        for name, key in cases.items():
            with self.subTest(name), self.assertRaises(lic.LicenceError):
                lic.verify(key, now=NOW, public_keys=KEYS)

    def test_expiry_has_a_seven_day_grace_period(self) -> None:
        key = make_key(exp=NOW)
        self.assertTrue(lic.verify(key, now=NOW + 86400, public_keys=KEYS).in_grace)
        with self.assertRaisesRegex(lic.LicenceError, "expired"):
            lic.verify(key, now=NOW + lic.GRACE_SECONDS + 1, public_keys=KEYS)

    def test_the_subscription_id_can_be_read_for_renewal(self) -> None:
        self.assertEqual(lic.subscription_of(make_key()), "sub_123")
        self.assertIsNone(lic.subscription_of("junk"))

    def test_the_shipped_public_key_is_well_formed(self) -> None:
        for kid, value in lic.PUBLIC_KEYS.items():
            with self.subTest(kid):
                self.assertEqual(len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))), 32)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.keys = patch.object(lic, "PUBLIC_KEYS", KEYS)
        self.keys.start()
        self.env = patch.dict(os.environ, {"ZHUNT_LICENCE": ""})
        self.env.start()

    def tearDown(self) -> None:
        self.keys.stop()
        self.env.stop()
        self.dir.cleanup()

    def test_no_key_means_free(self) -> None:
        plan = lic.current_plan(self.home)
        self.assertEqual((plan.name, plan.problem, plan.features), ("free", None, frozenset()))

    def test_a_saved_key_is_private_and_in_effect(self) -> None:
        with patch("zhunt.licence.time.time", return_value=NOW):
            lic.save_key(make_key(), self.home)
            plan = lic.current_plan(self.home, now=NOW)
        path = lic.licence_path(self.home)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(plan.name, "pro")

    def test_a_bad_key_is_never_stored(self) -> None:
        with self.assertRaises(lic.LicenceError):
            lic.save_key("ZHUNT1.x.y", self.home)
        self.assertFalse(lic.licence_path(self.home).exists())

    def test_a_stored_key_that_expired_falls_back_to_free_with_the_reason(self) -> None:
        lic.licence_path(self.home).parent.mkdir(parents=True)
        lic.licence_path(self.home).write_text(make_key(exp=NOW))
        plan = lic.current_plan(self.home, now=NOW + 30 * 86400)
        self.assertEqual(plan.name, "free")
        self.assertIn("expired", plan.problem)

    def test_remove(self) -> None:
        self.assertFalse(lic.remove_key(self.home))
        with patch("zhunt.licence.time.time", return_value=NOW):
            lic.save_key(make_key(), self.home)
        self.assertTrue(lic.remove_key(self.home))
        self.assertEqual(lic.current_plan(self.home).name, "free")


if __name__ == "__main__":
    unittest.main()
