import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from zhunt.dashboard import create_dashboard_app


def test_dashboard_requires_its_local_token(tmp_path: Path) -> None:
    app = create_dashboard_app(home=tmp_path, dashboard_token="dashboard-secret")
    client = TestClient(app)

    assert client.get("/api/status").status_code == 403
    assert client.get(
        "/api/status",
        headers={"X-Zhunt-Dashboard-Token": "dashboard-secret"},
    ).status_code == 200


def test_dashboard_reports_models_and_local_savings_without_content(
    tmp_path: Path,
) -> None:
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "request",
                "wire_dialect": "openai-chat-completions",
                "actual_cost": 0.12,
                "counterfactual_top_model_cost": 1.20,
                "secret_prompt": "must not be exposed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".zhunt").mkdir()
    (tmp_path / ".zhunt" / "env").write_text(
        'ZHUNT_PROVIDER="nous-portal"\n',
        encoding="utf-8",
    )
    app = create_dashboard_app(
        home=tmp_path,
        dashboard_token="dashboard-secret",
        telemetry_path=telemetry,
        provider_id="nous-portal",
        daemon_port=1,
    )
    client = TestClient(app)

    response = client.get(
        "/api/status",
        headers={"X-Zhunt-Dashboard-Token": "dashboard-secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "nous-portal"
    assert payload["daemon"]["reachable"] is False
    assert payload["summary"]["requests"] == 1
    assert payload["summary"]["savings"] == 1.08
    assert any(model["model"] == "openai/portal/m2-25" for model in payload["models"])
    assert "secret_prompt" not in response.text


def test_dashboard_html_does_not_embed_dashboard_token(tmp_path: Path) -> None:
    app = create_dashboard_app(home=tmp_path, dashboard_token="dashboard-secret")
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "dashboard-secret" not in response.text
    assert "No prompts, responses, or API keys" in response.text
