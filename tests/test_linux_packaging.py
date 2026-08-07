from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_linux_package_targets_x86_64_and_bundles_registry():
    build = (ROOT / "packaging/linux/build.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/linux-x64.yml").read_text(encoding="utf-8")

    assert "Zhunt-Setup-linux-x64.tar.gz" in build
    assert '--add-data "$ROOT/models.yaml:zhunt"' in build
    assert "ubuntu-22.04" in workflow
    assert "packaging/linux/verify.sh" in workflow
    assert "zhunt-linux-x64" in workflow
