from pathlib import Path

from typer.testing import CliRunner

from zhunt.cli import app

ROOT = Path(__file__).parents[1]


def test_desktop_command_exposes_headless_smoke_option():
    result = CliRunner().invoke(app, ["desktop", "--help"])
    assert result.exit_code == 0
    assert "no-browser" in result.output
    assert "port" in result.output


def test_packaged_launchers_start_server_and_workspace_together():
    for path in ["packaging/macos/Zhunt.command", "packaging/linux/Start-Zhunt.sh", "packaging/windows/Start-Zhunt.cmd"]:
        assert "desktop" in (ROOT / path).read_text()
    assert 'Parameters: "desktop"' in (ROOT / "packaging/windows/zhunt.iss").read_text()


def test_landing_downloads_are_architecture_specific_and_mobile_is_not_linked():
    import re
    html = (ROOT / "site/index.html").read_text()
    links = re.findall(r'href="downloads/([^"]+)"', html)
    assert len(links) == 6
    assert "Zhunt-Portable-win-x64.zip" in links
    assert "Zhunt-Portable-macos-arm64.tar.gz" in links
    assert "Zhunt-Setup-linux-x64.tar.gz" in links
    assert "Android — <strong>Under development</strong>" in html
    assert "iOS — <strong>Under development</strong>" in html
    assert not any("android" in x.lower() or "ios" in x.lower() for x in links)


def test_site_build_refuses_missing_downloads(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts/build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "site").mkdir()
    (tmp_path / "site/index.html").write_text('<a href="downloads/missing.zip">download</a>')
    monkeypatch.setattr(module, "ROOT", tmp_path)
    import pytest
    with pytest.raises(SystemExit, match="Missing desktop release artifact"):
        module.build(tmp_path / "out")
    assert not (tmp_path / "out").exists()
