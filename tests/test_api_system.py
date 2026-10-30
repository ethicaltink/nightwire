"""The Core page payload, the settings endpoint, and /metrics."""


def add(client, camera_id="front"):
    return client.post(
        "/api/cameras",
        json={
            "id": camera_id,
            "name": camera_id.title(),
            "host": "192.168.1.101",
            "main_uri": "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
        },
    )


# ------------------------------------------------------------------- gating


def test_system_and_settings_need_a_session(client):
    assert client.get("/api/system").status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert client.patch("/api/settings", json={}).status_code == 401


def test_metrics_is_reachable_without_a_session(client):
    """A Prometheus scraper cannot hold a login cookie."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "nightwire_up 1" in response.text
    assert "text/plain" in response.headers["content-type"]


def test_metrics_can_be_closed_with_a_token(client, monkeypatch):
    monkeypatch.setenv("NVR_METRICS_TOKEN", "sekrit")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200
    assert "nightwire_up" in ok.text


def test_metrics_rejects_a_wrong_token(client, monkeypatch):
    monkeypatch.setenv("NVR_METRICS_TOKEN", "sekrit")
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401


# ------------------------------------------------------------------ /system


def test_system_on_a_fresh_install(signed_in):
    body = signed_in.get("/api/system").json()
    assert body["version"]
    assert body["cameras"] == []
    assert body["disk"]["total_bytes"] > 0
    assert body["disk"]["max_percent"] == 90
    assert body["disk"]["target_percent"] == 85
    assert body["projection"]["projected_days"] is None


def test_system_reports_each_camera(signed_in):
    add(signed_in, "front")
    add(signed_in, "yard")
    body = signed_in.get("/api/system").json()
    assert [c["camera_id"] for c in body["cameras"]] == ["front", "yard"]
    for camera in body["cameras"]:
        assert camera["recorder"]["state"] == "not-started"
        assert camera["events"]["state"] in ("stopped", "not-started")
        assert camera["clock"] is None


def test_system_reports_the_per_camera_disk_share(signed_in):
    add(signed_in)
    conn = signed_in.app.state.db
    conn.execute(
        "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
        " VALUES ('front', 1, 2, 1000, 'front/x.ts', 5000)"
    )
    conn.commit()
    [share] = signed_in.get("/api/system").json()["disk"]["per_camera"]
    assert share["camera_id"] == "front"
    assert share["bytes"] == 5000
    assert share["segments"] == 1


def test_system_surfaces_the_clock_offset(signed_in):
    from nightwire.events import clock, store

    add(signed_in)
    store.save_offset(
        signed_in.app.state.db, clock.measure("front", 1_793_000_003_000, 1_793_000_000_000, 1_793_000_000_000)
    )
    [camera] = signed_in.get("/api/system").json()["cameras"]
    assert camera["clock"]["offset_ms"] == 3000
    assert camera["clock"]["warn"] is True
    assert camera["clock_warn_threshold_ms"] == 2000


# ----------------------------------------------------------------- settings


def test_reading_the_settings(signed_in):
    settings = signed_in.get("/api/settings").json()["settings"]
    assert settings["retention_days"] == "45"
    assert settings["max_exports_gb"] == "200"


def test_changing_a_setting(signed_in):
    response = signed_in.patch("/api/settings", json={"retention_days": 30})
    assert response.status_code == 200
    assert response.json()["settings"]["retention_days"] == "30"
    assert response.json()["changed"] == ["retention_days"]


def test_an_empty_patch_changes_nothing(signed_in):
    response = signed_in.patch("/api/settings", json={})
    assert response.status_code == 200
    assert response.json()["settings"]["retention_days"] == "45"


def test_a_setting_out_of_bounds_is_refused(signed_in):
    for body in [
        {"retention_days": 0},
        {"max_disk_percent": 10},
        {"max_disk_percent": 120},
        {"max_exports_gb": 0},
        {"station_reload_hour": 25},
    ]:
        response = signed_in.patch("/api/settings", json=body)
        assert response.status_code == 400, body


def test_a_target_above_the_ceiling_is_refused(signed_in):
    """The guard would otherwise delete continuously once it engaged."""
    response = signed_in.patch(
        "/api/settings", json={"max_disk_percent": 80, "target_disk_percent": 85}
    )
    assert response.status_code == 400
    assert "below max_disk_percent" in response.json()["error"]


def test_a_valid_threshold_pair_is_accepted(signed_in):
    response = signed_in.patch(
        "/api/settings", json={"max_disk_percent": 80, "target_disk_percent": 70}
    )
    assert response.status_code == 200
    assert response.json()["settings"]["target_disk_percent"] == "70"


def test_changed_settings_show_up_in_metrics(signed_in):
    # Above the default target of 85, or the validation correctly refuses it.
    assert signed_in.patch("/api/settings", json={"max_disk_percent": 95}).status_code == 200
    assert "nightwire_disk_max_percent 95" in signed_in.get("/metrics").text


def test_the_recordings_volume_is_reported_as_present(signed_in):
    assert "nightwire_recordings_volume_present 1" in signed_in.get("/metrics").text


def test_metrics_still_scrape_when_the_volume_is_gone(signed_in):
    """The scrape must survive the thing the alert is about.

    If /metrics returned 500 with the volume missing, Prometheus could not
    scrape and every alert would go blind exactly when the disk is the
    problem.
    """
    import shutil

    shutil.rmtree(signed_in.app.state.settings.recordings_dir)
    response = signed_in.get("/metrics")
    assert response.status_code == 200
    assert "nightwire_recordings_volume_present 0" in response.text
    assert "nightwire_up 1" in response.text


def test_the_core_page_still_loads_when_the_volume_is_gone(signed_in):
    import shutil

    shutil.rmtree(signed_in.app.state.settings.recordings_dir)
    response = signed_in.get("/api/system")
    assert response.status_code == 200
    assert response.json()["disk"]["total_bytes"] == 0


def test_metrics_includes_a_camera_once_added(signed_in):
    add(signed_in)
    text = signed_in.get("/metrics").text
    assert 'nightwire_recorder_recording{camera="front"} 0' in text
    assert "nightwire_cameras_total 1" in text
