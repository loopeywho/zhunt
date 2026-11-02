import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from zhunt.server import create_proxy_app, run_proxy


class DaemonSecurityTests(unittest.TestCase):
    def test_surface_requires_key_and_excludes_admin_routes_and_cors(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_proxy_app(env_path=Path(directory) / "env")
            paths = {
                route.path
                for route in app.app.routes
                if hasattr(route, "path")
            }
            inference = {
                "/v1/chat/completions",
                "/v1/responses",
                "/v1/messages",
                "/v1/messages/count_tokens",
            }
            self.assertTrue(inference <= paths)
            # Everything else is the workspace: its static files or its
            # authenticated API. No admin UI, docs or health routes are exposed.
            extra = paths - inference
            self.assertTrue(
                all(
                    path == "/workspace" or path.startswith("/zhunt/api/")
                    for path in extra
                ),
                sorted(extra),
            )
            with TestClient(app) as client:
                route_payloads = (
                    (
                        "/v1/chat/completions",
                        {"model": "unknown", "messages": []},
                    ),
                    (
                        "/v1/responses",
                        {"model": "unknown", "input": "hello"},
                    ),
                    (
                        "/v1/messages",
                        {
                            "model": "unknown",
                            "max_tokens": 1,
                            "messages": [],
                        },
                    ),
                    (
                        "/v1/messages/count_tokens",
                        {"model": "unknown", "messages": []},
                    ),
                )
                unauthorized = [
                    client.post(path, headers=headers, json=payload)
                    for headers in (
                        {},
                        {"Authorization": "Bearer wrong-key"},
                        {"x-api-key": "wrong-key"},
                    )
                    for path, payload in route_payloads
                ]
                preflight = client.options(
                    "/v1/chat/completions",
                    headers={
                        "Origin": "https://evil.example",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                admin = client.get("/ui/")
                count_tokens = client.post(
                    "/v1/messages/count_tokens",
                    headers={
                        "Authorization": f"Bearer {app.app.state.zhunt_master_key}",
                    },
                    json={
                        "model": "claude-sonnet-4-5-20250929",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        self.assertEqual(
            [response.status_code for response in unauthorized],
            [401] * 12,
        )
        self.assertNotIn("access-control-allow-origin", preflight.headers)
        self.assertEqual(admin.status_code, 404)
        self.assertEqual(count_tokens.status_code, 200)
        self.assertIn("input_tokens", count_tokens.json())

    def test_non_loopback_binding_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(ValueError, "refusing non-loopback"):
            run_proxy(host="0.0.0.0", port=4000)

        with patch("uvicorn.run") as uvicorn_run, patch(
            "zhunt.server.create_proxy_app", return_value=MagicMock(hook=MagicMock(summary=()))
        ) as create_app:
            run_proxy(host="0.0.0.0", port=4000, allow_non_loopback=True)
        uvicorn_run.assert_called_once()
        create_app.assert_called_once_with(
            telemetry_path=Path.home() / ".zhunt" / "telemetry.jsonl",
            catalog_path=None,
            engineering=False,
        )

    def test_run_proxy_forwards_explicit_telemetry_path(self) -> None:
        with TemporaryDirectory() as directory:
            telemetry_path = Path(directory) / "evidence.jsonl"
            with patch("uvicorn.run") as uvicorn_run, patch(
                "zhunt.server.create_proxy_app",
                return_value=MagicMock(hook=MagicMock(summary=())),
            ) as create_app:
                run_proxy(
                    host="127.0.0.1",
                    port=4000,
                    telemetry_path=telemetry_path,
                )

        uvicorn_run.assert_called_once()
        create_app.assert_called_once_with(
            telemetry_path=telemetry_path,
            catalog_path=None,
            engineering=True,
        )


if __name__ == "__main__":
    unittest.main()
