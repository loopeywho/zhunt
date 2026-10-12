import pytest


@pytest.fixture(autouse=True)
def _isolated_state_db(tmp_path, monkeypatch):
    """Keep every test's quota state in a temp file, never ~/.zhunt/state.db."""

    monkeypatch.setenv("ZHUNT_STATE_DB", str(tmp_path / "state.db"))
    # ...and never read the tester's real Pro licence
    monkeypatch.setenv("ZHUNT_LICENCE_FILE", str(tmp_path / "licence"))
    monkeypatch.delenv("ZHUNT_LICENCE", raising=False)
    # ...nor the tester's cached catalog feed, catalog settings or feed URL
    monkeypatch.setenv("ZHUNT_CATALOG_CACHE", str(tmp_path / "catalog.json"))
    for name in ("ZHUNT_CATALOG_UPDATES", "ZHUNT_CATALOG_URL", "ZHUNT_CATALOG_PUBKEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _deterministic_selection(request, monkeypatch):
    """Daemon tests assert which model is called, so rank by the posterior mean
    instead of a Thompson sample. Mark a test ``thompson`` to keep sampling."""

    if request.node.get_closest_marker("thompson") is None:
        monkeypatch.setattr("zhunt.engine.selection_rng", lambda: None)
