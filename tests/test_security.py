import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from fastapi.testclient import TestClient

from zhunt.server import create_proxy_app, run_proxy


class DaemonSecurityTests(unittest.TestCase):
    def test_surface_requires_key_and_excludes_admin_routes_and_cors(self) -> None:
        with TemporaryDirectory() as directory:
            registry = Path(directory) / "models.yaml"
            registry.write_text(
                "aliases: {}\ntiers:\n  chat:\n    - model: provider/test\n      in: 0.1\n      out: 0.2\n",
                encoding="utf-8",
            )
            app = create_proxy_app(
                registry_path=registry,
                env_path=Path(directory) / "env",
            )
            paths = {
                route.path
                for route in app.app.routes
                if hasattr(route, "path")
            }
            self.assertEqual(
                paths,
                {"/v1/chat/completions", "/v1/responses", "/v1/messages"},
            )
            with TestClient(app) as client:
                unauthorized = client.post(
                    "/v1/chat/completions",
                    json={"model": "unknown", "messages": []},
                )
                preflight = client.options(
                    "/v1/chat/completions",
                    headers={
                        "Origin": "https://evil.example",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                admin = client.get("/ui/")

        self.assertEqual(unauthorized.status_code, 401)
        self.assertNotIn("access-control-allow-origin", preflight.headers)
        self.assertEqual(admin.status_code, 404)

    def test_non_loopback_binding_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(ValueError, "refusing non-loopback"):
            run_proxy(host="0.0.0.0", port=4000)

        with patch("uvicorn.run") as uvicorn_run, patch(
            "zhunt.server.create_proxy_app", return_value=object()
        ):
            run_proxy(host="0.0.0.0", port=4000, allow_non_loopback=True)
        uvicorn_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
