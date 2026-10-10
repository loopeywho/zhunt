import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from typer.testing import CliRunner

from zhunt.cli import app

SECRET = "gsk_test_secret_value_123"
runner = CliRunner()


def invoke(home: Path, *args: str, input: str | None = None):
    clean = {"GROQ_API_KEY": "", "GEMINI_API_KEY": "", "CEREBRAS_API_KEY": ""}
    with patch("pathlib.Path.home", return_value=home), patch.dict(os.environ, clean):
        return runner.invoke(app, ["keys", *args], input=input)


def env_file(home: Path) -> Path:
    return home / ".zhunt" / "env"


def test_add_saves_the_key_privately_and_never_echoes_it() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        result = invoke(home, "add", "groq", input=f"{SECRET}\n")

        assert result.exit_code == 0, result.output
        assert SECRET not in result.output
        assert "GROQ_API_KEY" in result.output
        assert f'GROQ_API_KEY="{SECRET}"' in env_file(home).read_text()
        assert stat.S_IMODE(env_file(home).stat().st_mode) == 0o600


def test_add_replaces_an_existing_key_without_duplicating_it() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        invoke(home, "add", "groq", input="first-key\n")
        invoke(home, "add", "groq", input="second-key\n")

        text = env_file(home).read_text()
        assert text.count("GROQ_API_KEY=") == 1
        assert "second-key" in text and "first-key" not in text


def test_add_keeps_other_entries_in_the_file() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        env_file(home).parent.mkdir()
        env_file(home).write_text('ZHUNT_MASTER_KEY="master"\n')

        invoke(home, "add", "cerebras", input="csk-1\n")

        text = env_file(home).read_text()
        assert 'ZHUNT_MASTER_KEY="master"' in text and "CEREBRAS_API_KEY" in text


def test_add_rejects_an_unknown_provider_and_an_empty_key() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        unknown = invoke(home, "add", "nope", input="k\n")
        empty = invoke(home, "add", "groq", input="   \n")

        assert unknown.exit_code != 0
        assert "groq" in unknown.output and "cerebras" in unknown.output
        assert empty.exit_code != 0
        assert not env_file(home).exists()


def test_list_shows_set_and_not_set_but_never_values() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        invoke(home, "add", "groq", input=f"{SECRET}\n")

        result = invoke(home, "list")

        assert result.exit_code == 0
        assert SECRET not in result.output
        lines = {line.split()[0]: line for line in result.output.splitlines()}
        assert lines["groq"].endswith("set") and not lines["groq"].endswith("not set")
        assert lines["google"].endswith("not set")
        assert lines["cerebras"].endswith("not set")


def test_remove_deletes_only_that_provider() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        invoke(home, "add", "groq", input="g\n")
        invoke(home, "add", "cerebras", input="c\n")

        result = invoke(home, "remove", "groq")

        text = env_file(home).read_text()
        assert result.exit_code == 0
        assert "GROQ_API_KEY" not in text and "CEREBRAS_API_KEY" in text
        assert stat.S_IMODE(env_file(home).stat().st_mode) == 0o600


def test_remove_tightens_a_world_readable_file() -> None:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        invoke(home, "add", "groq", input="g\n")
        invoke(home, "add", "cerebras", input="c\n")
        env_file(home).chmod(0o644)

        invoke(home, "remove", "groq")

        assert stat.S_IMODE(env_file(home).stat().st_mode) == 0o600


def test_remove_of_a_missing_key_is_a_harmless_message() -> None:
    with TemporaryDirectory() as tmp:
        result = invoke(Path(tmp), "remove", "groq")

        assert result.exit_code == 0
        assert "was not set" in result.output
