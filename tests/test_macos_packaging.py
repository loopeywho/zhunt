from pathlib import Path
import unittest


ROOT = Path(__file__).parent.parent


class MacOSPackagingTests(unittest.TestCase):
    def test_build_script_targets_current_architecture_and_packages_both_formats(self) -> None:
        script = (ROOT / "packaging/macos/build.sh").read_text(encoding="utf-8")
        self.assertIn("uname -m", script)
        self.assertIn("pkgbuild", script)
        self.assertIn("hdiutil create", script)
        self.assertIn("Zhunt-Setup-macos-$ARCH.pkg", script)
        self.assertIn("Zhunt-Setup-macos-$ARCH.dmg", script)

    def test_build_script_bundles_registry_and_tiktoken(self) -> None:
        script = (ROOT / "packaging/macos/build.sh").read_text(encoding="utf-8")
        self.assertIn("--collect-all tiktoken", script)
        self.assertIn("--hidden-import tiktoken_ext.openai_public", script)
        self.assertIn("--add-data \"$ROOT/models.yaml:zhunt\"", script)
        self.assertIn("--specpath \"$WORK\"", script)
        self.assertIn('pip install "$ROOT[desktop]"', script)
        self.assertIn("--collect-all pystray", script)
        self.assertIn("--collect-all PIL", script)

    def test_checksums_are_portable_for_downloaders(self) -> None:
        script = (ROOT / "packaging/macos/build.sh").read_text(encoding="utf-8")
        self.assertIn('(cd "$OUTPUT" && shasum -a 256 "$(basename "$PKG")")', script)
        self.assertIn('(cd "$OUTPUT" && shasum -a 256 "$(basename "$DMG")")', script)

    def test_macos_readme_does_not_claim_universal_binary(self) -> None:
        readme = (ROOT / "packaging/macos/README.md").read_text(encoding="utf-8")
        self.assertIn("not yet a Universal 2 build", readme)
        self.assertIn("Developer ID signing and", readme)

    def test_top_level_readme_gates_macos_release(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## macOS arm64 preview", readme)
        self.assertIn("not a Universal 2 build", readme)
        self.assertIn("Do not publish", readme)


if __name__ == "__main__":
    unittest.main()
