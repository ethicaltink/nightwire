"""A fresh install must report healthy."""

from nightwire import db, health


def test_health_is_200_with_no_cameras(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["components"]["recorders"]["state"] == "idle"
    assert body["components"]["database"]["ok"] is True


def test_health_needs_no_session(client):
    client.cookies.clear()
    assert client.get("/api/health").status_code == 200


def test_health_names_components_that_have_not_started(client):
    components = client.get("/api/health").json()["components"]
    assert components["go2rtc"]["state"] == "not-started"
    assert components["go2rtc"]["state"] == "not-started"


def test_configured_camera_without_a_recorder_is_not_claimed_healthy(client):
    conn = client.app.state.db
    conn.execute(
        "INSERT INTO cameras (id, name, host, main_uri, created_at)"
        " VALUES ('front', 'Front Door', '192.168.1.101', 'rtsp://x', '2026-10-27')"
    )
    conn.commit()
    body = client.get("/api/health").json()
    recorders = body["components"]["recorders"]
    assert recorders["state"] == "not-started"
    assert recorders["state"] == "not-started"
    assert db.enabled_cameras(conn)


def test_a_registered_failing_probe_makes_it_unhealthy(client):
    health.register("go2rtc", lambda: (False, "error", "process exited"))
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
