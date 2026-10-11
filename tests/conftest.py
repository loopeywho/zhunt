import pytest


@pytest.fixture(autouse=True)
def _isolated_state_db(tmp_path, monkeypatch):
    """Keep every test's quota state in a temp file, never ~/.zhunt/state.db."""

    monkeypatch.setenv("ZHUNT_STATE_DB", str(tmp_path / "state.db"))
