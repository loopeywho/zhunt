"""``zhunt local``: hardware checks, model discovery, engine build and server steps.

The engine build and the model server are never run here: their commands are
recorded by fakes. Nothing is downloaded and no model is loaded.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from zhunt import local_cli, local_setup
from zhunt.cli import app
from zhunt.local_setup import Hardware, SetupError


@pytest.fixture
def state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(local_setup, "STATE_DIR", state_dir)
    monkeypatch.setattr(local_cli, "_env_path", lambda: tmp_path / "env")
    return state_dir


def write(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def zhunt_plus(root: Path) -> Path:
    for name in ("0001-a.patch", "0002-b.patch", "0003-c.patch"):
        write(root / "engine" / "inferssd-patches" / name)
    write(root / "engine" / "inferssd_engine.py")
    (root / "engines" / "inferssd" / ".git").mkdir(parents=True)
    return root


# -- hardware ------------------------------------------------------------------


def test_hardware_problems() -> None:
    good = Hardware(apple_silicon=True, ram_gib=24, free_disk_gb=500, disk_path="/")
    assert local_setup.hardware_problems(good) == ([], [])
    errors, warnings = local_setup.hardware_problems(
        Hardware(apple_silicon=False, ram_gib=8, free_disk_gb=2, disk_path="/")
    )
    assert len(errors) == 1 and "Apple Silicon" in errors[0]
    assert len(warnings) == 2


def test_ssd_benchmark_reads_an_existing_file_without_writing(tmp_path) -> None:
    model = write(tmp_path / "m.gguf", 3 << 20)
    before = sorted(tmp_path.iterdir())
    speed = local_setup.ssd_read_benchmark(model, seconds=0.2, chunk_mb=1, threads=2)
    assert speed > 0
    assert sorted(tmp_path.iterdir()) == before
    with pytest.raises(SetupError):
        local_setup.ssd_read_benchmark(write(tmp_path / "tiny.gguf", 10), chunk_mb=1)


# -- models --------------------------------------------------------------------


def test_complete_models_and_shards(tmp_path) -> None:
    single = write(tmp_path / "a" / "one.gguf", 100)
    first = write(tmp_path / "b" / "big-00001-of-00002.gguf", 300)
    write(tmp_path / "b" / "big-00002-of-00002.gguf", 300)
    broken = write(tmp_path / "c" / "half-00001-of-00002.gguf", 500)

    assert local_setup.complete_model(single) == single.resolve()
    with pytest.raises(SetupError, match="missing or empty shard"):
        local_setup.complete_model(broken)
    with pytest.raises(SetupError, match="first shard"):
        local_setup.complete_model(tmp_path / "b" / "big-00002-of-00002.gguf")
    with pytest.raises(SetupError):
        local_setup.complete_model(write(tmp_path / "x.bin"))

    found = local_setup.find_models([tmp_path, tmp_path / "nope"])
    assert found == [first.resolve(), single.resolve()]  # largest set first, broken skipped
    assert local_setup.model_bytes(found[0]) == 600


# -- engine --------------------------------------------------------------------


def test_patches_are_the_three_numbered_files(tmp_path) -> None:
    root = zhunt_plus(tmp_path / "zp")
    assert [p.name for p in local_setup.patch_files(root)] == [
        "0001-a.patch", "0002-b.patch", "0003-c.patch",
    ]
    (root / "engine" / "inferssd-patches" / "0003-c.patch").unlink()
    with pytest.raises(SetupError, match="0003-"):
        local_setup.patch_files(root)


def test_prepare_engine_clones_locally_pins_patches_builds_and_marks(tmp_path) -> None:
    root = zhunt_plus(tmp_path / "zp")
    patches = local_setup.patch_files(root)
    engine = tmp_path / "engine"
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            engine.mkdir()
        if "engine-resources.py" in " ".join(cmd):
            write(engine / "engine-resources.json")
        return subprocess.CompletedProcess(cmd, 0)

    local_setup.prepare_engine(
        source=root / "engines" / "inferssd", engine_dir=engine, patches=patches,
        python="python3", run=run, log=lambda _: None,
    )

    assert commands[0][:3] == ["git", "clone", "--quiet"]
    assert str(root / "engines" / "inferssd") in commands[0]  # a local folder, no URL
    assert commands[1][-1] == local_setup.PINNED_REVISION
    applied = [c for c in commands if "apply" in c]
    assert [c[-1] for c in applied] == [str(p) for p in patches for _ in range(2)]
    assert [c[4] for c in applied[::2]] == ["--check"] * 3  # each checked before applying
    assert commands[-2][1].endswith("build-macos-metal.sh")
    assert local_setup.engine_is_built(engine, patches)
    # a changed patch means the build is stale
    patches[0].write_bytes(b"changed")
    assert not local_setup.engine_is_built(engine, patches)


def test_prepare_engine_refuses_urls_and_existing_folders(tmp_path) -> None:
    root = zhunt_plus(tmp_path / "zp")
    patches = local_setup.patch_files(root)
    run = lambda *a, **k: pytest.fail("nothing should run")  # noqa: E731
    with pytest.raises(SetupError, match="nothing is downloaded"):
        local_setup.prepare_engine(
            source=Path("https://github.com/x/y"), engine_dir=tmp_path / "e",
            patches=patches, python="python3", run=run,
        )
    (tmp_path / "e").mkdir()
    with pytest.raises(SetupError, match="already exists"):
        local_setup.prepare_engine(
            source=root / "engines" / "inferssd", engine_dir=tmp_path / "e",
            patches=patches, python="python3", run=run,
        )


# -- server --------------------------------------------------------------------


def settings(**overrides) -> local_setup.ServerSettings:
    data = dict(launcher="/zp/launch.py", python="python3", engine_dir="/e", model="/m.gguf")
    data.update(overrides)
    return local_setup.ServerSettings(**data)


def test_server_command_and_pid_file(state) -> None:
    s = settings(port=7777)
    assert s.base_url == "http://127.0.0.1:7777/v1"
    assert s.command()[:2] == ["python3", "/zp/launch.py"]
    assert s.command()[-2:] == ["--port", "7777"]

    class Proc:
        pid = 4242

    popen = lambda cmd, **kw: Proc()  # noqa: E731
    assert local_setup.start_server(s, popen=popen) == 4242
    assert (state / "server.pid").read_text().strip() == "4242"
    local_setup.save_settings(s)
    assert local_setup.load_settings() == s


def test_stop_without_a_running_server_is_harmless(state) -> None:
    assert local_setup.stop_server() is False
    with pytest.raises(SetupError, match="zhunt local setup"):
        local_setup.load_settings()


# -- command line --------------------------------------------------------------


def _fake_hardware(path):
    return Hardware(apple_silicon=True, ram_gib=24, free_disk_gb=500, disk_path=str(path))


def test_setup_builds_starts_and_registers_with_every_step_faked(tmp_path, state) -> None:
    root = zhunt_plus(tmp_path / "zp")
    model = write(tmp_path / "models" / "m.gguf", 100)
    prepared, started = [], []
    with patch.object(local_setup, "hardware", _fake_hardware), patch.object(
        local_setup, "prepare_engine", lambda **kw: prepared.append(kw)
    ), patch.object(
        local_setup, "start_server", lambda s: started.append(s) or 99
    ), patch("zhunt.local_provider.is_answering", AsyncMock(return_value=True)):
        result = CliRunner().invoke(
            app,
            ["local", "setup", "--zhunt-plus", str(root), "--engine-dir", str(tmp_path / "e"),
             "--model-dir", str(tmp_path / "models"), "--no-bench", "--port", "7777", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert prepared and prepared[0]["source"] == root / "engines" / "inferssd"
    assert started[0].model == str(model.resolve()) and started[0].port == 7777
    env = (tmp_path / "env").read_text()
    assert 'ZHUNT_LOCAL_URL="http://127.0.0.1:7777/v1"' in env
    assert 'ZHUNT_LOCAL_CONTEXT="1024"' in env
    assert "licence" in result.output
    assert json.loads((state / "server.json").read_text())["port"] == 7777


def test_setup_asks_before_building_and_stops_on_no(tmp_path, state) -> None:
    root = zhunt_plus(tmp_path / "zp")
    write(tmp_path / "models" / "m.gguf", 100)
    with patch.object(local_setup, "hardware", _fake_hardware), patch.object(
        local_setup, "prepare_engine", lambda **kw: pytest.fail("built without asking")
    ):
        result = CliRunner().invoke(
            app,
            ["local", "setup", "--zhunt-plus", str(root), "--engine-dir", str(tmp_path / "e"),
             "--model-dir", str(tmp_path / "models"), "--no-bench"],
            input="n\n",
        )

    assert result.exit_code == 1
    assert "Build InferSSD 368df4f" in result.output
    assert not (tmp_path / "env").exists()


def test_setup_does_not_register_a_server_that_never_answers(tmp_path, state) -> None:
    root = zhunt_plus(tmp_path / "zp")
    write(tmp_path / "models" / "m.gguf", 100)
    with patch.object(local_setup, "hardware", _fake_hardware), patch.object(
        local_setup, "prepare_engine", lambda **kw: None
    ), patch.object(local_setup, "start_server", lambda s: 1), patch(
        "zhunt.local_provider.is_answering", AsyncMock(return_value=False)
    ):
        result = CliRunner().invoke(
            app,
            ["local", "setup", "--zhunt-plus", str(root), "--engine-dir", str(tmp_path / "e"),
             "--model-dir", str(tmp_path / "models"), "--no-bench", "--yes", "--wait", "0"],
        )

    assert result.exit_code == 1
    assert "Not registered" in result.output
    assert not (tmp_path / "env").exists()


def test_setup_without_a_zhunt_plus_folder_says_how(tmp_path, state, monkeypatch) -> None:
    monkeypatch.delenv(local_cli.ZHUNT_PLUS_ENV, raising=False)
    result = CliRunner().invoke(app, ["local", "setup"])
    assert result.exit_code == 1
    assert "ZHUNT_PLUS_HOME" in result.output


def test_status_reads_the_saved_settings(tmp_path, state) -> None:
    (tmp_path / "env").write_text('ZHUNT_LOCAL_URL="http://127.0.0.1:7777/v1"\n')
    with patch("zhunt.local_provider.is_answering", AsyncMock(return_value=False)):
        result = CliRunner().invoke(app, ["local", "status"])
    assert "http://127.0.0.1:7777/v1" in result.output and "not answering" in result.output
