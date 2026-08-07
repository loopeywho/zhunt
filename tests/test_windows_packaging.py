from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zhunt.installer import Installer


ROOT = Path(__file__).parent.parent


class WindowsPackagingTests(unittest.TestCase):
    def test_packaging_inputs_exist(self) -> None:
        self.assertTrue((ROOT / "packaging/windows/build.ps1").is_file())
        self.assertTrue((ROOT / "packaging/windows/zhunt.iss").is_file())
        self.assertTrue((ROOT / "packaging/windows/entrypoint.py").is_file())

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
