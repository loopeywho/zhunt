from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zhunt.tray import daemon_reachable, tray_state


def test_tray_marks_daemon_active_when_port_accepts_connections() -> None:
    with TemporaryDirectory() as tmpdir, patch(
        "zhunt.tray.socket.create_connection",
    ) as create:
        create.return_value.__enter__.return_value = object()

        empty_telemetry = Path(tmpdir) / "telemetry.jsonl"
        empty_telemetry.write_text("")
        assert daemon_reachable() is True
        assert tray_state(telemetry_path=empty_telemetry) == (
            "Zhunt Active — no requests yet",
            "#16804b",
        )


def test_tray_marks_daemon_offline_when_port_is_unreachable() -> None:
    with patch(
        "zhunt.tray.socket.create_connection",
        side_effect=OSError("offline"),
    ):
        assert daemon_reachable() is False
        assert tray_state(telemetry_path=Path("nonexistent.jsonl")) == (
            "Zhunt Offline",
            "#b42318",
        )
