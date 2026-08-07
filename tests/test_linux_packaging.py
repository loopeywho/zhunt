from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_linux_package_targets_x86_64_and_bundles_registry():
    build = (ROOT / "packaging/linux/build.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/linux-x64.yml").read_text(encoding="utf-8")

    assert "Zhunt-Setup-linux-x64.tar.gz" in build
    assert '--add-data "$ROOT/models.yaml:zhunt"' in build
    assert '(cd "$OUTPUT" && sha256sum "$(basename "$ARCHIVE")")' in build
    assert "ubuntu-22.04" in workflow
    assert "packaging/linux/verify.sh" in workflow
    assert "zhunt-linux-x64" in workflow


def test_linux_smoke_uses_an_authenticated_inference_route():
    verify = (ROOT / "packaging/linux/verify.sh").read_text(encoding="utf-8")

    assert "-X POST" in verify
    assert "/v1/chat/completions" in verify
    assert "/v1/models" not in verify
