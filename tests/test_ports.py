import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zhunt.ports import configured_port, resolve_daemon_port


class PortSelectionTests(unittest.TestCase):
    def test_prefers_standard_port_when_available(self) -> None:
        with TemporaryDirectory() as directory:
            selected, fell_back = resolve_daemon_port(
                home=Path(directory),
                persist=True,
            )

            self.assertEqual(selected, 4000)
            self.assertFalse(fell_back)
            self.assertEqual(configured_port(Path(directory)), 4000)

    def test_selects_next_local_port_when_preferred_is_occupied(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                "zhunt.ports.port_available",
                side_effect=lambda port: port != 4000,
            ):
                selected, fell_back = resolve_daemon_port(
                    home=Path(directory),
                    persist=True,
                )

            self.assertEqual(selected, 4001)
            self.assertTrue(fell_back)
            self.assertEqual(configured_port(Path(directory)), 4001)

    def test_fallback_never_probes_non_loopback(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                "zhunt.ports.port_available",
                side_effect=lambda port: port == 4001,
            ) as available:
                selected, _ = resolve_daemon_port(
                    home=Path(directory),
                    persist=False,
                )

            self.assertEqual(selected, 4001)
            available.assert_any_call(4000)
            available.assert_any_call(4001)


if __name__ == "__main__":
    unittest.main()
