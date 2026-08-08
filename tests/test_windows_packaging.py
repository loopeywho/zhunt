from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zhunt.installer import Installer


ROOT = Path(__file__).parent.parent


class WindowsPackagingTests(unittest.TestCase):
    def test_packaging_inputs_exist(self) -> None:
        self.assertTrue((ROOT / "packaging/windows/build.ps1").is_file())
        self.assertTrue((ROOT / "packaging/windows/verify.ps1").is_file())
        self.assertTrue((ROOT / "packaging/windows/zhunt.iss").is_file())
        self.assertTrue((ROOT / "packaging/windows/entrypoint.py").is_file())

    def test_windows_smoke_check_asserts_authentication(self) -> None:
        smoke = (ROOT / "packaging/windows/verify.ps1").read_text(encoding="utf-8")
        self.assertIn("Expected HTTP 401", smoke)
        self.assertIn("icacls", smoke)

    def test_windows_ci_runs_installer_smoke_verifier(self) -> None:
        workflow = (ROOT / ".github/workflows/windows-x64.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Install Windows package for smoke test", workflow)
        self.assertIn("/VERYSILENT", workflow)
        self.assertIn("Run Windows smoke verification", workflow)
        self.assertIn("packaging\\windows\\verify.ps1", workflow)

    def test_windows_build_includes_dynamic_tiktoken_encoding(self) -> None:
        build = (ROOT / "packaging/windows/build.ps1").read_text(encoding="utf-8")
        self.assertIn("--collect-all tiktoken", build)
        self.assertIn("--hidden-import tiktoken_ext.openai_public", build)
        self.assertIn("--add-data", build)
        self.assertIn('"models.yaml"', build)
        self.assertIn('pip install ".[desktop]"', build)
        self.assertIn("--collect-all pystray", build)
        self.assertIn("--collect-all PIL", build)

    def test_windows_recipe_targets_are_under_user_home(self) -> None:
        cases = {
            "claude": Path(".claude/settings.json"),
            "codex": Path(".codex/config.toml"),
            "hermes": Path(".hermes/config.yaml"),
            "vscode": Path("AppData/Roaming/Code/User/chatLanguageModels.json"),
        }
        for app_name, relative_target in cases.items():
            with self.subTest(app=app_name):
                with TemporaryDirectory() as directory:
                    home = Path(directory).resolve()
                    installer = Installer(home=home, platform_name="windows")
                    result = installer.install(
                        app_name,
                        mode="api",
                        base_url="http://127.0.0.1:4000",
                    )
                    self.assertEqual(result.target, home / relative_target)
                    self.assertTrue(result.target.is_file())
                    installer.uninstall(app_name)
                    self.assertFalse(result.target.exists())
