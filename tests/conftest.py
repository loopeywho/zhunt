import pytest


@pytest.fixture(autouse=True)
def _isolated_state_db(tmp_path, monkeypatch):
    """Keep every test's quota state in a temp file, never ~/.zhunt/state.db."""

    monkeypatch.setenv("ZHUNT_STATE_DB", str(tmp_path / "state.db"))


@pytest.fixture(autouse=True)
def _deterministic_selection(request, monkeypatch):
    """Daemon tests assert which model is called, so rank by the posterior mean
    instead of a Thompson sample. Mark a test ``thompson`` to keep sampling."""

    if request.node.get_closest_marker("thompson") is None:
        monkeypatch.setattr("zhunt.engine.selection_rng", lambda: None)
