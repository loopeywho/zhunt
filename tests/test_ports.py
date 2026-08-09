import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zhunt.ports import configured_port, resolve_daemon_port


class PortSelectionTests(unittest.TestCase):
    def test_prefers_standard_port_when_available(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                "zhunt.ports.port_available",
                return_value=True,
            ):
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
                side_effect=lambda port, host: port != 4000,
            ):
                selected, fell_back = resolve_daemon_port(
                    home=Path(directory),
                    persist=True,
                )

            self.assertEqual(selected, 4001)
            self.assertTrue(fell_back)
            self.assertEqual(configured_port(Path(directory)), 4001)

    def test_fallback_probes_requested_loopback_host(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                "zhunt.ports.port_available",
                side_effect=lambda port, host: port == 4001,
            ) as available:
                selected, _ = resolve_daemon_port(
                    home=Path(directory),
                    host="::1",
                    persist=False,
                )

            self.assertEqual(selected, 4001)
            available.assert_any_call(4000, host="::1")
            available.assert_any_call(4001, host="::1")

    def test_port_selection_rejects_non_loopback_host(self) -> None:
        with self.assertRaises(ValueError):
            resolve_daemon_port(host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
