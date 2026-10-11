"""The website's licence function (JavaScript) must issue keys that zhunt (Python) accepts."""

import base64
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zhunt import licence as lic

ROOT = Path(__file__).parents[1]
NOW = 1_800_000_000


@unittest.skipUnless(shutil.which("node"), "node is needed to run the website function")
class LicenceFunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        private = Ed25519PrivateKey.generate()
        der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        cls.keys = {"test": base64.urlsafe_b64encode(raw).rstrip(b"=").decode()}
        out = subprocess.run(
            ["node", str(ROOT / "tests" / "support" / "licence_fn_driver.mjs")],
            env={**os.environ, "PRIV": base64.b64encode(der).decode()},
            capture_output=True, text=True, timeout=60, check=True,
        )
        cls.results = json.loads(out.stdout)

    def test_a_completed_checkout_yields_a_key_zhunt_accepts(self) -> None:
        r = self.results["purchase"]
        self.assertEqual(r["status"], 200)
        plan = lic.verify(r["body"]["licence"], now=NOW, public_keys=self.keys)
        self.assertEqual((plan.name, plan.email), ("pro", "buyer@example.com"))
        self.assertEqual(plan.expires_at, NOW + 30 * 86400)  # the paid-up period, not just the trial
        self.assertEqual(lic.subscription_of(r["body"]["licence"]), "sub_ABCDEFGH12")

    def test_renewal_needs_the_matching_email(self) -> None:
        self.assertEqual(self.results["renew"]["status"], 200)
        lic.verify(self.results["renew"]["body"]["licence"], now=NOW, public_keys=self.keys)
        self.assertEqual(self.results["renewWrongEmail"]["status"], 403)

    def test_no_key_without_a_live_paid_subscription(self) -> None:
        self.assertEqual(self.results["incomplete"]["status"], 409)
        self.assertEqual(self.results["canceled"]["status"], 402)
        self.assertEqual(self.results["unknown"]["status"], 404)
        self.assertEqual(self.results["malformed"]["status"], 400)
        self.assertEqual(self.results["unconfigured"]["status"], 503)
        for name in ("incomplete", "canceled", "unknown", "malformed", "renewWrongEmail", "unconfigured"):
            self.assertNotIn("licence", self.results[name]["body"], name)


if __name__ == "__main__":
    unittest.main()
