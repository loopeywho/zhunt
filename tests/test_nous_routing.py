import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
import os

import litellm
from fastapi.testclient import TestClient

from zhunt.providers import get_provider
from zhunt.server import create_proxy_app


class NousPortalRoutingTests(unittest.TestCase):
    def test_configured_portal_profile_rewrites_real_request(self) -> None:
        registry = """\
aliases:
  zhunt-auto: {tier: auto}
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
"""
        response = litellm.ModelResponse(
            id="portal-test",
            model="portal/m2-25",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        )
        provider_call = AsyncMock(return_value=response)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "models.yaml"
            registry_path.write_text(registry, encoding="utf-8")
            env_path = root / "env"
            env_path.write_text(
                'ZHUNT_PROVIDER="nous-portal"\nPORTAL_API_KEY="sk-portal-test"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                with patch("litellm.acompletion", provider_call):
                    app = create_proxy_app(
                        registry_path=registry_path,
                        env_path=env_path,
                    )
                    from litellm.proxy import proxy_server

                    with TestClient(app) as client:
                        result = client.post(
                            "/v1/chat/completions",
                            headers={
                                "Authorization": f"Bearer {proxy_server.master_key}",
                            },
                            json={
                                "model": "zhunt-auto",
                                "messages": [{"role": "user", "content": "Hi"}],
                            },
                        )

        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(provider_call.await_args.kwargs["model"], "openai/portal/m2-25")
        self.assertEqual(
            provider_call.await_args.kwargs["api_base"],
            get_provider("nous-portal").base_url,
        )
        self.assertEqual(provider_call.await_args.kwargs["api_key"], "sk-portal-test")


if __name__ == "__main__":
    unittest.main()
