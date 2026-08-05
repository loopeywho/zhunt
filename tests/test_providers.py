import unittest
from urllib.request import Request

from zhunt.brain import Tier
from zhunt.providers import ProviderError, get_provider, validate_provider_key


class ProviderTests(unittest.TestCase):
    def test_nous_portal_is_openai_compatible_profile(self) -> None:
        provider = get_provider("nous-portal")
        self.assertEqual(provider.base_url, "https://api.portal.ai/v1")
        self.assertEqual(provider.key_env, "PORTAL_API_KEY")
        self.assertEqual(provider.model_for_tier(Tier.CHAT, "fallback"), "portal/m2-25")

    def test_validation_uses_bearer_key_and_counts_models(self) -> None:
        seen: list[Request] = []

        def fetch(request: Request) -> dict[str, object]:
            seen.append(request)
            return {"data": [{"id": "portal/m2-25"}, {"id": "portal/m3-55-xh-1m"}]}

        count = validate_provider_key("nous-portal", "sk-portal-test", fetcher=fetch)

        self.assertEqual(count, 2)
        self.assertEqual(seen[0].full_url, "https://api.portal.ai/v1/models")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer sk-portal-test")

    def test_validation_rejects_empty_key(self) -> None:
        with self.assertRaisesRegex(ProviderError, "API key is required"):
            validate_provider_key("nous-portal", " ")


if __name__ == "__main__":
    unittest.main()
