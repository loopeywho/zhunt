import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ruamel.yaml import YAML

from zhunt.pricing import sync_registry


class PricingSyncTests(unittest.TestCase):
    def test_shipped_prices_match_current_snapshot(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        models = {
            item["model"]: item
            for tier in document["tiers"].values()
            for item in tier
        }

        self.assertEqual(
            (models["openai/portal/m2-25"]["in"],
             models["openai/portal/m2-25"]["out"]),
            (0.12, 0.59),
        )
        self.assertEqual(
            (models["openai/portal/m2-25-fast"]["in"],
             models["openai/portal/m2-25-fast"]["out"]),
            (0.70, 3.50),
        )
        self.assertEqual(
            (models["openai/portal/m3-500k-fast"]["in"],
             models["openai/portal/m3-500k-fast"]["out"]),
            (2.80, 8.40),
        )
        self.assertEqual(
            (models["openai/portal/m3-55-xh-1m"]["in"],
             models["openai/portal/m3-55-xh-1m"]["out"]),
            (3.50, 21.00),
        )

    def test_default_tiers_only_contain_openai_portal_models(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())

        for tier_name, models_in_tier in document["tiers"].items():
            for entry in models_in_tier:
                self.assertTrue(
                    entry["model"].startswith("openai/portal/"),
                    f"tier {tier_name!r} has non-Portal model "
                    f"{entry['model']!r} in the default tiers block",
                )

    def test_sync_updates_matching_models_and_reports_unavailable(self) -> None:
        registry_text = (
            "aliases:\n"
            "  zhunt-auto: {tier: auto}\n"
            "tiers:\n"
            "  chat:\n"
            "    - model: openrouter/provider/chat\n"
            "      in: 0.5\n"
            "      out: 1.0\n"
            "    - model: custom/provider/missing\n"
            "      in: 0.2\n"
            "      out: 0.4\n"
        )
        remote = [
            {
                "id": "provider/chat",
                "pricing": {"input": "0.0000001", "output": "0.0000002"},
            }
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "models.yaml"
            path.write_text(registry_text, encoding="utf-8")
            result = sync_registry(path, fetcher=lambda: remote)
            yaml = YAML()
            document = yaml.load(path.read_text(encoding="utf-8"))

        self.assertEqual(result.updated, ("openrouter/provider/chat",))
        self.assertEqual(result.unavailable, ("custom/provider/missing",))
        self.assertEqual(result.cheaper_tiers, ("chat",))
        self.assertEqual(document["tiers"]["chat"][0]["in"], 0.1)
        self.assertEqual(document["tiers"]["chat"][0]["out"], 0.2)

    def test_sync_accepts_current_provider_prompt_completion_pricing(self) -> None:
        registry_text = (
            "aliases:\n"
            "  zhunt-auto: {tier: auto}\n"
            "tiers:\n"
            "  chat:\n"
            "    - model: openrouter/deepseek/deepseek-chat\n"
            "      in: 0.14\n"
            "      out: 0.28\n"
        )
        remote = [
            {
                "id": "deepseek/deepseek-chat",
                "pricing": {
                    "prompt": "0.0000002574",
                    "completion": "0.0000010287",
                },
            }
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "models.yaml"
            path.write_text(registry_text, encoding="utf-8")
            result = sync_registry(path, fetcher=lambda: remote)
            yaml = YAML()
            document = yaml.load(path.read_text(encoding="utf-8"))

        self.assertEqual(result.updated, ("openrouter/deepseek/deepseek-chat",))
        self.assertEqual(document["tiers"]["chat"][0]["in"], 0.2574)
        self.assertEqual(document["tiers"]["chat"][0]["out"], 1.0287)


if __name__ == "__main__":
    unittest.main()
