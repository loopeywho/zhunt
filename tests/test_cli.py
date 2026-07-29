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
        with patch("zhunt.cli.run_proxy") as run_proxy:
            result = self.runner.invoke(app, ["serve"])

        self.assertEqual(result.exit_code, 0)
        run_proxy.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            registry_path=None,
            allow_non_loopback=False,
        )

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


if __name__ == "__main__":
    unittest.main()
