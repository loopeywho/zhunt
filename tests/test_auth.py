import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zhunt.auth import ensure_master_key, save_env_value


class AuthEnvironmentTests(unittest.TestCase):
    def test_env_values_round_trip_into_daemon_process(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            with patch.dict(os.environ, {}, clear=False):
                save_env_value("PORTAL_API_KEY", "sk-portal-test", path)
                key = ensure_master_key(path)

            text = path.read_text(encoding="utf-8")

        self.assertTrue(key.startswith("sk-zhunt-"))
        self.assertIn('PORTAL_API_KEY="sk-portal-test"', text)
        self.assertEqual(path.parent.exists(), False)


if __name__ == "__main__":
    unittest.main()
