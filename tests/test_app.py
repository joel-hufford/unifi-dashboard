from fastapi.testclient import TestClient

from unifi_dashboard.app import create_app
from unifi_dashboard.config import Config


def demo_client():
    cfg = Config(demo=True, poll_interval=1.0)
    cfg.history.retention_minutes = 30
    return TestClient(create_app(cfg))


def test_dashboard_endpoint_serves_a_window():
    with demo_client() as client:
        response = client.get("/api/dashboard?minutes=15")
        assert response.status_code == 200
        payload = response.json()
        assert payload["window"]["window_minutes"] == 15
        assert payload["config"]["demo"] is True
        assert len(payload["series"]["ts"]) > 0


def test_window_is_capped_at_the_retention_setting():
    with demo_client() as client:
        payload = client.get("/api/dashboard?minutes=600").json()
        assert payload["window"]["window_minutes"] == 30


def test_window_must_be_positive():
    with demo_client() as client:
        assert client.get("/api/dashboard?minutes=0").status_code == 422


def test_index_and_assets_are_served():
    with demo_client() as client:
        assert "<title>Network status</title>" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200


def test_healthz():
    with demo_client() as client:
        assert client.get("/api/healthz").json()["ok"] is True
