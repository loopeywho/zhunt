import unittest
import json
from datetime import datetime, timezone
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
        with patch("zhunt.cli.run_proxy") as run_proxy:
            result = self.runner.invoke(app, ["serve"])

        self.assertEqual(result.exit_code, 0)
        run_proxy.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            registry_path=None,
            allow_non_loopback=False,
            telemetry_path=Path.home() / ".zhunt" / "telemetry.jsonl",
        )

    def test_serve_accepts_explicit_telemetry_path(self) -> None:
        with TemporaryDirectory() as directory:
            telemetry = Path(directory) / "evidence.jsonl"
            with patch("zhunt.cli.run_proxy") as run_proxy:
                result = self.runner.invoke(
                    app,
                    ["serve", "--telemetry", str(telemetry)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        run_proxy.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            registry_path=None,
            allow_non_loopback=False,
            telemetry_path=telemetry,
        )

    def test_setup_refuses_non_loopback_host(self) -> None:
        result = self.runner.invoke(
            app,
            ["setup", "--host", "0.0.0.0", "--no-browser"],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("setup is local-only", result.output)

    def test_dashboard_refuses_non_loopback_host(self) -> None:
        result = self.runner.invoke(
            app,
            ["dashboard", "--host", "0.0.0.0", "--no-browser"],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("dashboard is local-only", result.output)

    def test_dashboard_runs_with_local_only_defaults(self) -> None:
        with patch("uvicorn.run") as run_server:
            result = self.runner.invoke(app, ["dashboard", "--no-browser"])

        self.assertEqual(result.exit_code, 0, result.output)
        run_server.assert_called_once()
        self.assertEqual(run_server.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run_server.call_args.kwargs["port"], 8401)

    def test_tray_explains_optional_dependency(self) -> None:
        with patch(
            "zhunt.tray.run_tray",
            side_effect=RuntimeError(
                "tray support is optional; install it with `pip install 'zhunt[desktop]'`"
            ),
        ):
            result = self.runner.invoke(app, ["tray"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("zhunt[desktop]", result.output)

    def test_install_and_uninstall_codex_through_cli(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".codex" / "config.toml"
            target.parent.mkdir(parents=True)
            original = b'# original\nmodel = "native"\n'
            target.write_bytes(original)
            installer = Installer(home=home, platform_name="darwin")

            with patch(
                "zhunt.cli.create_installer",
                return_value=installer,
            ):
                installed = self.runner.invoke(
                    app,
                    ["install", "codex", "--mode", "api"],
                )
                restored = self.runner.invoke(
                    app,
                    ["uninstall", "codex"],
                )

            self.assertEqual(installed.exit_code, 0, installed.output)
            self.assertIn("Configured codex", installed.output)
            self.assertEqual(restored.exit_code, 0, restored.output)
            self.assertEqual(target.read_bytes(), original)

    def test_cursor_install_prints_vendor_supported_manual_steps(self) -> None:
        with TemporaryDirectory() as directory:
            installer = Installer(
                home=Path(directory),
                platform_name="darwin",
            )
            with patch(
                "zhunt.cli.create_installer",
                return_value=installer,
            ):
                result = self.runner.invoke(
                    app,
                    ["install", "cursor", "--mode", "api"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Manual setup required for cursor", result.output)
        self.assertIn("http://127.0.0.1:4000/v1", result.output)

    def test_cli_installs_every_automatic_app_recipe(self) -> None:
        cases = {
            "claude": Path(".claude/settings.json"),
            "codex": Path(".codex/config.toml"),
            "hermes": Path(".hermes/config.yaml"),
            "vscode": Path(
                "Library/Application Support/Code/User/"
                "chatLanguageModels.json"
            ),
        }
        for app_name, relative_target in cases.items():
            with self.subTest(app=app_name):
                with TemporaryDirectory() as directory:
                    home = Path(directory)
                    installer = Installer(
                        home=home,
                        platform_name="darwin",
                    )
                    with patch(
                        "zhunt.cli.create_installer",
                        return_value=installer,
                    ):
                        result = self.runner.invoke(
                            app,
                            ["install", app_name, "--mode", "api"],
                        )

                    self.assertEqual(
                        result.exit_code,
                        0,
                        result.output,
                    )
                    self.assertTrue((home / relative_target).is_file())

    def test_claude_safe_default_rejects_passthrough(self) -> None:
        with TemporaryDirectory() as directory:
            installer = Installer(
                home=Path(directory),
                platform_name="darwin",
            )
            with patch(
                "zhunt.cli.create_installer",
                return_value=installer,
            ):
                result = self.runner.invoke(app, ["install", "claude"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot register Zhunt", result.output)

    def test_status_shows_local_spend_summary(self) -> None:
        with TemporaryDirectory() as directory:
            telemetry = Path(directory) / "telemetry.jsonl"
            telemetry.write_text(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": "request",
                        "wire_dialect": "openai-chat-completions",
                        "actual_cost": 0.25,
                        "counterfactual_top_model_cost": 1.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = self.runner.invoke(
                app,
                ["status", "--telemetry", str(telemetry)],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Requests: 1", result.output)
        self.assertIn("Actual spend: $0.250000", result.output)
        self.assertIn("Savings: $0.750000", result.output)
        self.assertIn("openai-chat-completions: 1 requests", result.output)

    def test_sync_reports_pricing_changes(self) -> None:
        sync_result = type(
            "SyncResult",
            (),
            {
                "updated": ("provider/chat",),
                "unavailable": ("provider/missing",),
                "cheaper_tiers": ("chat",),
            },
        )()
        with TemporaryDirectory() as directory:
            registry = Path(directory) / "models.yaml"
            registry.write_text("tiers: {}\n", encoding="utf-8")
            with patch("zhunt.cli.sync_registry", return_value=sync_result):
                result = self.runner.invoke(
                    app,
                    ["sync", "--registry", str(registry)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Updated: 1 models", result.output)
        self.assertIn("Unavailable: 1 models", result.output)
        self.assertIn("Cheaper tier candidates: chat", result.output)


if __name__ == "__main__":
    unittest.main()
