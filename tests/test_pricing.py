import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ruamel.yaml import YAML

from zhunt.pricing import sync_registry


class PricingSyncTests(unittest.TestCase):
    # -- base (ungated) tiers ------------------------------------------------

    def test_base_tiers_snapshot(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        tiers = document["tiers"]

        # chat → luna
        chat = {m["model"]: m for m in tiers["chat"]}
        self.assertEqual(
            (chat["openai/gpt-5.6-luna"]["in"],
             chat["openai/gpt-5.6-luna"]["out"]),
            (0.20, 1.20),
        )
        # coding → terra (standard rate)
        coding = {m["model"]: m for m in tiers["coding"]}
        self.assertEqual(
            (coding["openai/gpt-5.6-terra"]["in"],
             coding["openai/gpt-5.6-terra"]["out"]),
            (2.00, 12.00),
        )
        # long-context → terra (elevated rate)
        long_ctx = {m["model"]: m for m in tiers["long-context"]}
        self.assertEqual(
            (long_ctx["openai/gpt-5.6-terra"]["in"],
             long_ctx["openai/gpt-5.6-terra"]["out"]),
            (4.00, 18.00),
        )
        # reasoning → sol
        reasoning = {m["model"]: m for m in tiers["reasoning"]}
        self.assertEqual(
            (reasoning["openai/gpt-5.6-sol"]["in"],
             reasoning["openai/gpt-5.6-sol"]["out"]),
            (5.00, 30.00),
        )

    def test_base_tiers_only_contain_openai_models(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        for tier_name, models_in_tier in document["tiers"].items():
            for entry in models_in_tier:
                self.assertTrue(
                    entry["model"].startswith("openai/"),
                    f"base tier {tier_name!r} has non-OpenAI model {entry['model']!r}",
                )

    # -- gated provider profiles --------------------------------------------

    def test_provider_profile_snapshot_anthropic(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        p = document["providers"]["anthropic"]
        models = {item["model"]: item for tier in p["tiers"].values() for item in tier}

        self.assertEqual(
            (models["anthropic/claude-haiku-4-5"]["in"],
             models["anthropic/claude-haiku-4-5"]["out"]),
            (1.00, 5.00),
        )
        self.assertEqual(
            (models["anthropic/claude-sonnet-5"]["in"],
             models["anthropic/claude-sonnet-5"]["out"]),
            (2.00, 10.00),
        )
        self.assertEqual(
            (models["anthropic/claude-opus-5"]["in"],
             models["anthropic/claude-opus-5"]["out"]),
            (5.00, 25.00),
        )

    def test_provider_profile_mutation_guard_anthropic(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        p = document["providers"]["anthropic"]
        for tier_name, models_in_tier in p["tiers"].items():
            for entry in models_in_tier:
                self.assertTrue(
                    entry["model"].startswith("anthropic/"),
                    f"anthropic tier {tier_name!r} has non-Anthropic model {entry['model']!r}",
                )

    def test_provider_profile_snapshot_openai(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        tiers = document["providers"]["openai"]["tiers"]

        # chat → luna
        self.assertEqual(
            ({m["model"]: m for m in tiers["chat"]}["openai/gpt-5.6-luna"]["in"],
             {m["model"]: m for m in tiers["chat"]}["openai/gpt-5.6-luna"]["out"]),
            (0.20, 1.20),
        )
        # coding → terra (standard rate)
        coding = {m["model"]: m for m in tiers["coding"]}
        self.assertEqual(
            (coding["openai/gpt-5.6-terra"]["in"],
             coding["openai/gpt-5.6-terra"]["out"]),
            (2.00, 12.00),
        )
        # long-context → terra (elevated rate — must NOT match coding)
        long_ctx = {m["model"]: m for m in tiers["long-context"]}
        self.assertEqual(
            (long_ctx["openai/gpt-5.6-terra"]["in"],
             long_ctx["openai/gpt-5.6-terra"]["out"]),
            (4.00, 18.00),
        )
        # reasoning → sol
        self.assertEqual(
            ({m["model"]: m for m in tiers["reasoning"]}["openai/gpt-5.6-sol"]["in"],
             {m["model"]: m for m in tiers["reasoning"]}["openai/gpt-5.6-sol"]["out"]),
            (5.00, 30.00),
        )

    def test_provider_profile_mutation_guard_openai(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        p = document["providers"]["openai"]
        for tier_name, models_in_tier in p["tiers"].items():
            for entry in models_in_tier:
                self.assertTrue(
                    entry["model"].startswith("openai/"),
                    f"openai tier {tier_name!r} has non-OpenAI model {entry['model']!r}",
                )

    # -- Terra long-context mutation test -----------------------------------

    def test_terra_long_context_uses_elevated_not_standard_rate(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        openai_tiers = document["providers"]["openai"]["tiers"]
        coding = {m["model"]: m for m in openai_tiers["coding"]}
        long_ctx = {m["model"]: m for m in openai_tiers["long-context"]}

        self.assertEqual(coding["openai/gpt-5.6-terra"]["in"], 2.00)
        self.assertEqual(coding["openai/gpt-5.6-terra"]["out"], 12.00)
        self.assertEqual(long_ctx["openai/gpt-5.6-terra"]["in"], 4.00)
        self.assertEqual(long_ctx["openai/gpt-5.6-terra"]["out"], 18.00)
        self.assertNotEqual(
            long_ctx["openai/gpt-5.6-terra"]["in"],
            coding["openai/gpt-5.6-terra"]["in"],
            "long-context terra must use the elevated rate, not the standard coding rate",
        )

    def test_terra_long_context_would_detect_rate_regression(self) -> None:
        yaml = YAML()
        document = yaml.load((Path(__file__).parent.parent / "models.yaml").read_text())
        long_ctx = {m["model"]: m for m in document["providers"]["openai"]["tiers"]["long-context"]}
        terra = long_ctx["openai/gpt-5.6-terra"]
        self.assertGreater(terra["in"], 3.00, "long-context terra input price must be > $3/MTok")
        self.assertGreater(terra["out"], 15.00, "long-context terra output price must be > $15/MTok")

    # -- Sync tests (unchanged from prior rounds) ---------------------------

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
