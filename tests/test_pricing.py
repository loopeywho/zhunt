import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ruamel.yaml import YAML

from zhunt.pricing import sync_registry


class PricingSyncTests(unittest.TestCase):
    def test_shipped_openrouter_prices_match_current_snapshot(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        models = {
            item["model"]: item
            for tier in document["tiers"].values()
            for item in tier
        }

        self.assertEqual(
            (models["openrouter/deepseek/deepseek-chat"]["in"],
             models["openrouter/deepseek/deepseek-chat"]["out"]),
            (0.2574, 1.0287),
        )
        self.assertEqual(
            (models["openrouter/anthropic/claude-sonnet-5"]["in"],
             models["openrouter/anthropic/claude-sonnet-5"]["out"]),
            (2.0, 10.0),
        )

    def test_sync_updates_matching_models_and_reports_unavailable(self) -> None:
        registry_text = """\\
aliases:
  zhunt-auto: {tier: auto}
tiers:
  chat:
    - model: openrouter/provider/chat
      in: 0.5
      out: 1.0
    - model: custom/provider/missing
      in: 0.2
      out: 0.4
""".lstrip("\\")
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

    def test_sync_accepts_current_openrouter_prompt_completion_pricing(self) -> None:
        registry_text = """
aliases:
  zhunt-auto: {tier: auto}
tiers:
  chat:
    - model: openrouter/deepseek/deepseek-chat
      in: 0.14
      out: 0.28
""".lstrip()
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
