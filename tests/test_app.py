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


def test_chart_scale_is_published_to_the_page():
    with demo_client() as client:
        config = client.get("/api/dashboard").json()["config"]
        assert config["throughput_scale"] == "log"
        assert config["log_decades"] == 3.0


def test_debug_wan_endpoint_exposes_uplinks_without_client_detail():
    with demo_client() as client:
        payload = client.get("/api/debug/wan").json()
        assert set(payload["wan_interfaces"]) == {"wan1", "wan3"}
        assert [link["key"] for link in payload["parsed"]] == ["wan1", "wan3"]
        # Uplink detail only: no client names or MAC addresses ride along.
        assert "clients" not in payload
        blob = str(payload)
        assert "essid" not in blob and "hostname" not in blob


def test_assets_are_version_stamped_and_never_cached_independently():
    # A fresh index.html paired with a stale app.js means the script hunts for
    # elements that no longer exist and the page dies on a null dereference.
    with demo_client() as client:
        page = client.get("/")
        assert page.headers["cache-control"] == "no-store"
        assert "/static/app.js?v=" in page.text
        assert "/static/styles.css?v=" in page.text

        asset = client.get("/static/app.js")
        assert "no-cache" in asset.headers["cache-control"]


def test_public_ip_can_be_rechecked_on_demand():
    with demo_client() as client:
        client.get("/api/dashboard")          # prime a snapshot
        response = client.post("/api/public-ip/refresh")
        assert response.status_code == 200
        assert response.json()["address"] == "198.51.100.7"
