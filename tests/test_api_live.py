"""Live endpoints, with go2rtc faked. Needs FastAPI."""

import pytest

from nightwire.live import client as live_client


class FakeGo2rtc:
    def __init__(self, state="running"):
        self.state = state
        self.detail = "2 stream(s) registered"
        self.streams = ["front_main", "front_sub"]
        self.syncs = 0

    def status(self):
        return {
            "state": self.state,
            "detail": self.detail,
            "streams": list(self.streams),
            "restarts": 0,
            "stderr_tail": [],
        }

    async def sync(self):
        self.syncs += 1
        return {"streams": list(self.streams), "added": [], "removed": []}

    async def sync_quietly(self):
        self.syncs += 1

    def health(self):
        if self.state == "running":
            return True, "running", self.detail
        return True, self.state, "live video is not running"


@pytest.fixture
def go2rtc(client):
    supervisor = FakeGo2rtc()
    client.app.state.go2rtc = supervisor
    yield supervisor
    client.app.state.go2rtc = None


def add(client, camera_id="front", sub=True):
    return client.post(
        "/api/cameras",
        json={
            "id": camera_id,
            "name": camera_id,
            "host": "192.168.1.101",
            "main_uri": "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
            "sub_uri": "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-sub" if sub else None,
        },
    )


def test_live_streams_needs_a_session(client):
    assert client.get("/api/live/streams").status_code == 401


def test_live_streams_is_empty_on_a_fresh_install(signed_in):
    body = signed_in.get("/api/live/streams").json()
    assert body["cameras"] == []
    assert body["go2rtc"]["state"] in ("not-started", "running", "stopped", "missing")


def test_live_streams_describes_each_tile(signed_in, go2rtc):
    add(signed_in, "front")
    add(signed_in, "yard", sub=False)
    body = signed_in.get("/api/live/streams").json()
    tiles = {tile["camera_id"]: tile for tile in body["cameras"]}
    assert tiles["front"]["grid_stream"] == "front_sub"
    assert tiles["front"]["main_stream"] == "front_main"
    assert tiles["front"]["has_sub"] is True
    # No substream: the grid falls back to the main stream.
    assert tiles["yard"]["grid_stream"] == "yard_main"
    assert tiles["yard"]["has_sub"] is False


def test_live_streams_reports_go2rtc_state(signed_in, go2rtc):
    body = signed_in.get("/api/live/streams").json()
    assert body["go2rtc"]["state"] == "running"
    assert body["go2rtc"]["streams"] == ["front_main", "front_sub"]


def test_adding_a_camera_resyncs_go2rtc(signed_in, go2rtc):
    before = go2rtc.syncs
    add(signed_in, "front")
    assert go2rtc.syncs > before


def test_editing_a_camera_resyncs_go2rtc(signed_in, go2rtc):
    add(signed_in, "front")
    before = go2rtc.syncs
    signed_in.patch("/api/cameras/front", json={"name": "Porch"})
    assert go2rtc.syncs > before


def test_removing_a_camera_resyncs_go2rtc(signed_in, go2rtc):
    add(signed_in, "front")
    before = go2rtc.syncs
    signed_in.delete("/api/cameras/front")
    assert go2rtc.syncs > before


def test_a_go2rtc_failure_does_not_fail_the_camera_edit(signed_in, go2rtc, monkeypatch):
    """A camera must be editable while live video is broken.

    The failure is injected at the HTTP client, which is where a real go2rtc
    outage appears. Patching `sync_quietly` itself would replace the very
    method whose job is to swallow this.
    """

    async def boom(*args, **kwargs):
        raise live_client.LiveError("go2rtc unreachable")

    add(signed_in, "front")
    monkeypatch.setattr(live_client, "sync_streams", boom)
    monkeypatch.setattr(live_client, "put_stream", boom)
    assert signed_in.patch("/api/cameras/front", json={"name": "Porch"}).status_code == 200


def test_sync_endpoint(signed_in, go2rtc):
    response = signed_in.post("/api/live/sync")
    assert response.status_code == 200
    assert response.json()["streams"] == ["front_main", "front_sub"]


def test_sync_without_go2rtc_is_a_503(signed_in):
    signed_in.app.state.go2rtc = None
    assert signed_in.post("/api/live/sync").status_code == 503


def test_snapshot_prefers_go2rtc_over_onvif(signed_in, go2rtc, monkeypatch):
    add(signed_in, "front")
    calls = []

    async def frame(stream):
        calls.append(stream)
        return b"\xff\xd8\xff-from-go2rtc"

    monkeypatch.setattr(live_client, "frame", frame)
    response = signed_in.get("/api/cameras/front/snapshot.jpg")
    assert response.status_code == 200
    assert response.content.startswith(b"\xff\xd8\xff")
    # The substream, which is what the grid shows.
    assert calls == ["front_sub"]


def test_snapshot_falls_back_to_onvif_when_go2rtc_cannot_supply_a_frame(
    signed_in, go2rtc, monkeypatch
):
    from nightwire.onvif import client as onvif
    from nightwire.onvif import snapshot

    add(signed_in, "front")

    async def no_frame(stream):
        raise live_client.LiveError("no frames yet")

    async def snapshot_uri(*args, **kwargs):
        return "http://192.168.1.101/onvif-media/jpg/image.cgi"

    async def fetch(uri, username, password):
        return b"\xff\xd8\xff-from-onvif"

    # The ONVIF fallback resolves a snapshot URI from a profile token, so the
    # row needs one. PATCH sets it without the create route's ONVIF probe.
    assert signed_in.patch(
        "/api/cameras/front", json={"sub_profile": "profile_2"}
    ).status_code == 200

    monkeypatch.setattr(live_client, "frame", no_frame)
    monkeypatch.setattr(onvif, "snapshot_uri", snapshot_uri)
    monkeypatch.setattr(snapshot, "fetch", fetch)

    response = signed_in.get("/api/cameras/front/snapshot.jpg")
    assert response.status_code == 200
    assert b"onvif" in response.content


def test_the_signalling_socket_refuses_an_unauthenticated_browser(client):
    import pytest as _pytest

    with _pytest.raises(Exception):
        with client.websocket_connect("/live/ws?src=front_sub"):
            pass


def test_the_signalling_socket_refuses_an_unknown_stream_name(signed_in):
    import pytest as _pytest

    with _pytest.raises(Exception):
        with signed_in.websocket_connect("/live/ws?src=not-ours"):
            pass


def test_go2rtc_being_down_does_not_make_the_process_unhealthy(signed_in, go2rtc):
    """Recording pulls RTSP straight from the cameras, so live video is not
    load-bearing for the container's health."""
    from nightwire import health as health_mod

    go2rtc.state = "reconnecting"
    health_mod.register("go2rtc", go2rtc.health)
    try:
        response = signed_in.get("/api/health")
        assert response.status_code == 200
        assert response.json()["components"]["go2rtc"]["state"] == "reconnecting"
    finally:
        health_mod.reset()
