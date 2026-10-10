from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml

from tests.catalog_fixtures import catalog_data
from zhunt.brain import Tier
from zhunt.catalog import Catalog, CatalogError


def test_loads_models_and_filters_by_tier() -> None:
    catalog = Catalog.from_data(catalog_data())

    chat = {m.id for m in catalog.models_for_tier(Tier.CHAT)}
    assert chat == {"alpha/chat-fast", "alpha/coder", "beta/trainer", "gamma/paid-chat"}
    assert {m.id for m in catalog.models_for_tier(Tier.REASONING)} == {"delta/thinker"}
    assert {m.id for m in catalog.models_for_tier(Tier.LONG_CONTEXT)} == set()


def test_free_means_zero_priced_and_limits_parse() -> None:
    catalog = Catalog.from_data(catalog_data())
    by_id = {m.id: m for m in catalog.models}

    assert by_id["alpha/coder"].free
    assert not by_id["gamma/paid-chat"].free
    assert by_id["alpha/coder"].limits.rpd == 3
    assert by_id["alpha/coder"].limits.rpm is None


def test_loads_from_a_yaml_file() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "catalog.yaml"
        path.write_text(yaml.safe_dump(catalog_data()))
        catalog = Catalog.from_path(path)

    assert catalog.providers["alpha"].verified_on.isoformat() == "2026-10-10"
    assert len(catalog.models) == 5


def test_training_policy_must_be_stated_for_every_model() -> None:
    data = catalog_data()
    del data["providers"]["alpha"]["models"][0]["may_train_on_prompts"]

    with pytest.raises(CatalogError, match="may_train_on_prompts"):
        Catalog.from_data(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["providers"]["alpha"]["models"][0].update(tiers=["nope"]), "unknown tier"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(tiers=[]), "requires tiers"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(limits={"rpm": 0}), "invalid rpm"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(limits={"rps": 1}), "unknown limits"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(intelligence=101), "intelligence"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(speed=True), "speed"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(**{"in": -1}), "negative"),
        (lambda d: d["providers"]["alpha"]["models"][0].update(context_window=0), "context_window"),
        (lambda d: d["providers"]["alpha"].update(verified_on="yesterday"), "ISO date"),
        (lambda d: d["providers"]["alpha"]["models"].append(
            deepcopy(d["providers"]["alpha"]["models"][0])), "duplicate"),
        (lambda d: d["providers"]["alpha"].update(models=[]), "requires models"),
    ],
)
def test_rejects_invalid_catalog_data(mutate, message) -> None:
    data = catalog_data()
    mutate(data)

    with pytest.raises(CatalogError, match=message):
        Catalog.from_data(data)


@pytest.mark.parametrize("data", [None, [], {}, {"providers": {}}])
def test_rejects_missing_providers(data) -> None:
    with pytest.raises(CatalogError):
        Catalog.from_data(data)
