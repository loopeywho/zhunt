import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from typer.testing import CliRunner

from zhunt.cli import app
from zhunt.installer import Installer


class ServeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_serve_defaults_to_localhost(self) -> None:
        with patch(
            "zhunt.cli.resolve_daemon_port",
            return_value=(4000, False),
        ), patch("zhunt.cli.run_proxy") as run_proxy:
            result = self.runner.invoke(app, ["serve"])

        self.assertEqual(result.exit_code, 0)
        run_proxy.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            allow_non_loopback=False,
            telemetry_path=Path.home() / ".zhunt" / "telemetry.jsonl",
            catalog_path=None,
        )

    def test_serve_accepts_explicit_telemetry_and_catalog_paths(self) -> None:
        with TemporaryDirectory() as directory:
            telemetry = Path(directory) / "evidence.jsonl"
            catalog = Path(directory) / "catalog.yaml"
            catalog.write_text("providers: {}\n")
            with patch(
                "zhunt.cli.resolve_daemon_port",
                return_value=(4000, False),
            ), patch("zhunt.cli.run_proxy") as run_proxy:
                result = self.runner.invoke(
                    app,
                    ["serve", "--telemetry", str(telemetry), "--catalog", str(catalog)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        run_proxy.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            allow_non_loopback=False,
            telemetry_path=telemetry,
            catalog_path=catalog,
        )

    def test_serve_reports_safe_local_fallback(self) -> None:
        with patch(
            "zhunt.cli.resolve_daemon_port",
            return_value=(4011, True),
        ), patch("zhunt.cli.run_proxy") as run_proxy:
            result = self.runner.invoke(app, ["serve"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("using localhost:4011 instead", result.output)
        run_proxy.assert_called_once()
        self.assertEqual(run_proxy.call_args.kwargs["port"], 4011)

    def test_run_proxy_forwards_options_to_the_server(self) -> None:
        from zhunt.cli import run_proxy

        with patch("zhunt.server.run_proxy") as start:
            run_proxy(host="127.0.0.1", port=4000, catalog_path=Path("c.yaml"))

        start.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            allow_non_loopback=False,
            telemetry_path=None,
            catalog_path=Path("c.yaml"),
        )


class CommandSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_v2_commands_are_present(self) -> None:
        listing = self.runner.invoke(app, ["--help"]).output

        for command in ("serve", "launch", "doctor", "open", "keys", "uninstall"):
            self.assertIn(command, listing)

    def test_v1_commands_are_gone(self) -> None:
        for command in ("setup", "dashboard", "tray", "install", "status", "benchmark", "sync"):
            result = self.runner.invoke(app, [command])
            self.assertEqual(result.exit_code, 2, command)
            self.assertIn("No such command", result.output)


class UninstallCommandTests(unittest.TestCase):
    """`uninstall` stays so configs written by an older Zhunt can be reverted."""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_uninstall_restores_a_managed_app(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".codex" / "config.toml"
            target.parent.mkdir(parents=True)
            original = b'# original\nmodel = "native"\n'
            target.write_bytes(original)
            installer = Installer(home=home, platform_name="darwin")
            installer.install("codex", mode="api", base_url="http://127.0.0.1:4000")

            with patch("zhunt.cli.create_installer", return_value=installer):
                restored = self.runner.invoke(app, ["uninstall", "codex"])

            self.assertEqual(restored.exit_code, 0, restored.output)
            self.assertEqual(target.read_bytes(), original)

    def test_uninstall_all_restores_managed_apps(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".codex" / "config.toml"
            original = b'model = "native"\n'
            target.parent.mkdir(parents=True)
            target.write_bytes(original)
            installer = Installer(home=home, platform_name="darwin")
            installer.install("codex", mode="api", base_url="http://127.0.0.1:4000")

            with patch("zhunt.cli.create_installer", return_value=installer):
                result = self.runner.invoke(app, ["uninstall", "--all"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(target.read_bytes(), original)

    def test_uninstall_all_with_nothing_installed_says_so(self) -> None:
        with TemporaryDirectory() as directory:
            installer = Installer(home=Path(directory), platform_name="darwin")
            with patch("zhunt.cli.create_installer", return_value=installer):
                result = self.runner.invoke(app, ["uninstall", "--all"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No Zhunt-managed app configurations found", result.output)

    def test_uninstall_needs_an_app_name_or_all(self) -> None:
        with TemporaryDirectory() as directory:
            installer = Installer(home=Path(directory), platform_name="darwin")
            with patch("zhunt.cli.create_installer", return_value=installer):
                result = self.runner.invoke(app, ["uninstall"])

        self.assertNotEqual(result.exit_code, 0)
