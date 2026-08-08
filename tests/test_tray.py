from unittest.mock import patch

from zhunt.tray import daemon_reachable, tray_state


def test_tray_marks_daemon_active_when_port_accepts_connections() -> None:
    with patch("zhunt.tray.socket.create_connection") as create:
        create.return_value.__enter__.return_value = object()

        assert daemon_reachable() is True
        assert tray_state() == ("Zhunt Active", "#16804b")


def test_tray_marks_daemon_offline_when_port_is_unreachable() -> None:
    with patch(
        "zhunt.tray.socket.create_connection",
        side_effect=OSError("offline"),
    ):
        assert daemon_reachable() is False
        assert tray_state() == ("Zhunt Offline", "#b42318")
