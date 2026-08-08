import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import tomllib
import yaml

from zhunt.installer import Installer, InstallerError, supported_apps


BASE_URL = "http://127.0.0.1:4000"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.installer = Installer(home=self.home, platform_name="darwin")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_all_target_apps_have_recipes(self) -> None:
        self.assertEqual(
            supported_apps(),
            ("claude", "codex", "cursor", "hermes", "vscode"),
        )

    def test_claude_install_and_uninstall_restore_original_bytes(self) -> None:
        target = self.home / ".claude" / "settings.json"
        original = b'{\n  "permissions": {"allow": ["Read"]}\n}\n'
        self._write(target, original)

        result = self.installer.install(
            "claude",
            mode="api",
            base_url=BASE_URL,
        )

        installed = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(installed["env"]["ANTHROPIC_BASE_URL"], BASE_URL)
        self.assertEqual(
            installed["env"]["ANTHROPIC_AUTH_TOKEN"],
            self._master_key(),
        )
        self.assertEqual(installed["permissions"], {"allow": ["Read"]})
        self.assertIsNotNone(result.backup)
        self.assertEqual(result.backup.read_bytes(), original)

        self.installer.uninstall("claude")
        self.assertEqual(target.read_bytes(), original)

    def test_claude_passthrough_is_rejected_without_changes(self) -> None:
        with self.assertRaisesRegex(
            InstallerError,
            "cannot register Zhunt",
        ):
            self.installer.install(
                "claude",
                mode="passthrough",
                base_url=BASE_URL,
            )
        self.assertFalse((self.home / ".claude").exists())

    def test_codex_api_mode_writes_user_provider_and_restores(self) -> None:
        target = self.home / ".codex" / "config.toml"
        original = b'# keep this comment\nmodel = "native-model"\n'
        self._write(target, original)

        self.installer.install(
            "codex",
            mode="api",
            base_url=BASE_URL,
        )

        installed = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(installed["model"], "zhunt-auto")
        self.assertEqual(installed["model_provider"], "zhunt")
        self.assertEqual(
            installed["model_providers"]["zhunt"],
            {
                "name": "Zhunt",
                "base_url": f"{BASE_URL}/v1",
                "wire_api": "responses",
                "experimental_bearer_token": self._master_key(),
            },
        )
        self.assertIn(
            "# keep this comment",
            target.read_text(encoding="utf-8"),
        )

        self.installer.uninstall("codex")

        self.assertEqual(target.read_bytes(), original)

    def test_codex_passthrough_registers_without_changing_default(self) -> None:
        target = self.home / ".codex" / "config.toml"
        self._write(target, b'model = "native-model"\n')

        self.installer.install(
            "codex",
            mode="passthrough",
            base_url=BASE_URL,
        )

        installed = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(installed["model"], "native-model")
        self.assertNotIn("model_provider", installed)
        self.assertIn("zhunt", installed["model_providers"])

    def test_hermes_api_mode_upserts_provider_and_selects_it(self) -> None:
        target = self.home / ".hermes" / "config.yaml"
        original = (
            b"# user config\n"
            b"model:\n"
            b"  default: native-model\n"
            b"  provider: openrouter\n"
        )
        self._write(target, original)

        self.installer.install(
            "hermes",
            mode="api",
            base_url=BASE_URL,
        )

        installed = yaml.safe_load(target.read_text(encoding="utf-8"))
        self.assertEqual(
            installed["model"],
            {"default": "zhunt-auto", "provider": "custom:zhunt"},
        )
        self.assertEqual(
            installed["custom_providers"],
            [
                {
                    "name": "zhunt",
                    "base_url": f"{BASE_URL}/v1",
                    "api_mode": "chat_completions",
                    "api_key": self._master_key(),
                }
            ],
        )
        self.assertIn("# user config", target.read_text(encoding="utf-8"))

        self.installer.uninstall("hermes")

        self.assertEqual(target.read_bytes(), original)

    def test_hermes_reinstall_is_idempotent_and_keeps_first_backup(self) -> None:
        target = self.home / ".hermes" / "config.yaml"
        original = b"model:\n  default: native-model\n"
        self._write(target, original)

        first = self.installer.install(
            "hermes",
            mode="passthrough",
            base_url=BASE_URL,
        )
        second = self.installer.install(
            "hermes",
            mode="passthrough",
            base_url=BASE_URL,
        )

        installed = yaml.safe_load(target.read_text(encoding="utf-8"))
        self.assertEqual(len(installed["custom_providers"]), 1)
        self.assertEqual(second.backup, first.backup)

        self.installer.uninstall("hermes")

        self.assertEqual(target.read_bytes(), original)

    def test_vscode_upserts_custom_endpoint_and_restores(self) -> None:
        target = (
            self.home
            / "Library"
            / "Application Support"
            / "Code"
            / "User"
            / "chatLanguageModels.json"
        )
        original = (
            b'[\n'
            b'  {"name": "Existing", "vendor": "openai", "models": []}\n'
            b']\n'
        )
        self._write(target, original)

        self.installer.install(
            "vscode",
            mode="api",
            base_url=BASE_URL,
        )

        installed = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(installed), 2)
        zhunt = installed[1]
        self.assertEqual(zhunt["vendor"], "customendpoint")
        self.assertEqual(zhunt["apiType"], "chat-completions")
        self.assertEqual(
            zhunt["models"][0]["url"],
            f"{BASE_URL}/v1/chat/completions",
        )
        self.assertTrue(zhunt["models"][0]["toolCalling"])

        self.installer.uninstall("vscode")

        self.assertEqual(target.read_bytes(), original)

    def test_cursor_recipe_returns_supported_manual_steps(self) -> None:
        result = self.installer.install(
            "cursor",
            mode="api",
            base_url=BASE_URL,
        )

        self.assertTrue(result.manual)
        self.assertIsNone(result.target)
        self.assertTrue(
            any(f"{BASE_URL}/v1" in step for step in result.manual_instructions)
        )

    def test_tier2_coverage_caveats_are_documented(self) -> None:
        readme = (
            Path(__file__).parent.parent / "README.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Cursor tab completion and inline edit stay", readme)
        self.assertIn("VS Code code completions stay on Copilot", readme)
        self.assertIn("setup is intentionally manual in the vendor UI", readme)

    def test_install_new_file_then_uninstall_removes_it(self) -> None:
        result = self.installer.install(
            "codex",
            mode="passthrough",
            base_url=BASE_URL,
        )

        self.assertTrue(result.target.exists())
        self.installer.uninstall("codex")
        self.assertFalse(result.target.exists())

    def test_installed_apps_lists_managed_recipes(self) -> None:
        self.installer.install(
            "codex",
            mode="api",
            base_url=BASE_URL,
        )
        self.assertEqual(self.installer.installed_apps(), ("codex",))

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _master_key(self) -> str:
        line = next(
            line
            for line in (self.home / ".zhunt" / "env").read_text().splitlines()
            if line.startswith("ZHUNT_MASTER_KEY=")
        )
        return line.split("=", 1)[1]


if __name__ == "__main__":
    unittest.main()
