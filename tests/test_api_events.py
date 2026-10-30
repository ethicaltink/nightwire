"""Motion event endpoints. Needs FastAPI."""

from nightwire.events import store
from nightwire.onvif import client as onvif
from tests import fake_pullpoint as fake

T0 = 1_793_000_000_000
MIN = 60_000


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


def source_payload(topic=fake.VMD_TOPIC, label="Porch", kind="span"):
    return {"topic": topic, "label": label, "kind": kind, "color": "#FF2BD6"}


def test_event_routes_need_a_session(client):
    for path in [
        "/api/cameras/front/event-topics",
        "/api/cameras/front/event-sources",
        "/api/cameras/front/events",
        "/api/events/recent",
        "/api/events/open",
    ]:
        assert client.get(path).status_code == 401, path


def test_rescanning_asks_the_camera(signed_in, monkeypatch):
    add(signed_in)

    async def properties(host, port, username, password):
        return fake.topic_set()

    monkeypatch.setattr(onvif, "event_properties", properties)
    body = signed_in.get("/api/cameras/front/event-topics").json()
    assert body["count"] == 3
    kinds = {t["topic"]: t["kind"] for t in body["topics"]}
    assert kinds[fake.VMD_TOPIC] == "span"
    assert kinds[fake.FENCE_TOPIC] == "point"
    # Labels are derived, not looked up.
    assert all(t["label"] for t in body["topics"])


def test_rescanning_a_camera_that_refuses_is_a_502(signed_in, monkeypatch):
    add(signed_in)

    async def refuse(*args, **kwargs):
        raise onvif.OnvifError("GetEventProperties failed")

    monkeypatch.setattr(onvif, "event_properties", refuse)
    assert signed_in.get("/api/cameras/front/event-topics").status_code == 502


def test_rescanning_without_onvif_installed_is_a_503(signed_in, monkeypatch):
    add(signed_in)

    async def missing(*args, **kwargs):
        raise onvif.OnvifUnavailable("needs onvif-zeep-async")

    monkeypatch.setattr(onvif, "event_properties", missing)
    response = signed_in.get("/api/cameras/front/event-topics")
    assert response.status_code == 503
    assert response.json()["dependency_missing"] is True


def test_sources_start_empty(signed_in):
    add(signed_in)
    body = signed_in.get("/api/cameras/front/event-sources").json()
    assert body["sources"] == []
    assert body["listener"]["state"] in ("not-started", "stopped", "no sources")


def test_saving_and_reading_sources(signed_in):
    add(signed_in)
    response = signed_in.put(
        "/api/cameras/front/event-sources", json={"sources": [source_payload()]}
    )
    assert response.status_code == 200
    [saved] = response.json()["sources"]
    assert saved["label"] == "Porch"
    assert saved["kind"] == "span"
    assert signed_in.get("/api/cameras/front/event-sources").json()["sources"][0]["id"] == saved["id"]


def test_a_source_without_a_label_is_rejected(signed_in):
    add(signed_in)
    response = signed_in.put(
        "/api/cameras/front/event-sources", json={"sources": [source_payload(label="  ")]}
    )
    assert response.status_code == 400


def test_a_source_with_a_bad_kind_is_rejected(signed_in):
    add(signed_in)
    response = signed_in.put(
        "/api/cameras/front/event-sources", json={"sources": [source_payload(kind="blob")]}
    )
    assert response.status_code == 400


def test_sources_for_an_unknown_camera_are_404(signed_in):
    assert signed_in.get("/api/cameras/nope/event-sources").status_code == 404
    assert signed_in.put("/api/cameras/nope/event-sources", json={"sources": []}).status_code == 404


def seed(client, camera_id="front", specs=((T0, T0 + MIN),)):
    conn = client.app.state.db
    # The first source, not the only one: some tests configure several.
    src = store.list_sources(conn, camera_id)[0]
    for start, end in specs:
        conn.execute(
            "INSERT INTO events (camera_id, source_id, start_ts, end_ts) VALUES (?,?,?,?)",
            (camera_id, src["id"], start, end),
        )
    conn.commit()
    return src


def test_a_days_events_come_back_with_their_lane_details(signed_in):
    add(signed_in)
    signed_in.put("/api/cameras/front/event-sources", json={"sources": [source_payload()]})
    seed(signed_in)
    body = signed_in.get(f"/api/cameras/front/events?start={T0 - MIN}&end={T0 + 10 * MIN}").json()
    [event] = body["events"]
    assert event["label"] == "Porch"
    assert event["color"] == "#FF2BD6"
    assert event["duration_ms"] == MIN
    assert body["sources"]


def test_events_can_be_filtered_to_one_source(signed_in):
    add(signed_in)
    signed_in.put(
        "/api/cameras/front/event-sources",
        json={"sources": [source_payload(), source_payload(fake.FENCE_TOPIC, "Line", "point")]},
    )
    src = seed(signed_in)
    body = signed_in.get(
        f"/api/cameras/front/events?start={T0 - MIN}&end={T0 + 10 * MIN}&sources={src['id']}"
    ).json()
    assert len(body["events"]) == 1


def test_a_bad_sources_filter_is_a_400(signed_in):
    add(signed_in)
    assert signed_in.get("/api/cameras/front/events?sources=abc").status_code == 400


def test_a_bad_date_is_a_400(signed_in):
    add(signed_in)
    assert signed_in.get("/api/cameras/front/events?date=27-10-2026").status_code == 400


def test_recent_events_across_cameras(signed_in):
    add(signed_in, "front")
    add(signed_in, "yard")
    for camera in ("front", "yard"):
        signed_in.put(f"/api/cameras/{camera}/event-sources", json={"sources": [source_payload()]})
    seed(signed_in, "front", [(T0, T0 + MIN)])
    seed(signed_in, "yard", [(T0 + 5 * MIN, T0 + 6 * MIN)])
    events = signed_in.get("/api/events/recent?limit=5").json()["events"]
    assert [e["camera_id"] for e in events] == ["yard", "front"]
    assert events[0]["camera_name"] == "Yard"


def test_open_events_are_listed_for_the_live_glow(signed_in):
    add(signed_in)
    signed_in.put("/api/cameras/front/event-sources", json={"sources": [source_payload()]})
    seed(signed_in, "front", [(T0, None), (T0 - MIN, T0)])
    [event] = signed_in.get("/api/events/open").json()["events"]
    assert event["start_ts"] == T0
    assert event["open"] is True


def test_an_event_snapshot_that_does_not_exist_is_404(signed_in):
    add(signed_in)
    signed_in.put("/api/cameras/front/event-sources", json={"sources": [source_payload()]})
    seed(signed_in)
    event_id = signed_in.get("/api/events/open").json()["events"]
    assert signed_in.get("/api/events/999/snapshot.jpg").status_code == 404


def test_an_event_snapshot_is_served_when_present(signed_in):
    add(signed_in)
    signed_in.put("/api/cameras/front/event-sources", json={"sources": [source_payload()]})
    seed(signed_in)
    conn = signed_in.app.state.db
    recordings = signed_in.app.state.settings.recordings_dir
    event_id = conn.execute("SELECT id FROM events").fetchone()["id"]
    snapshot = recordings / "_events" / "front" / f"{event_id}.jpg"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"\xff\xd8\xff")
    store.record_snapshot(conn, event_id, f"_events/front/{event_id}.jpg")

    response = signed_in.get(f"/api/events/{event_id}/snapshot.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_health_names_the_event_listener(signed_in):
    components = signed_in.get("/api/health").json()["components"]
    assert "events" in components
    assert components["events"]["state"] == "not-started"
    assert components["events"]["state"] == "not-started"
