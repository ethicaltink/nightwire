"""Route-level tests for the camera API.

These need FastAPI. The ONVIF layer is monkeypatched, so no camera and no
network is required: what is under test is the wiring, the error mapping and
the gate, not the SOAP client.
"""

import pytest

from nightwire import cameras as repo
from nightwire.onvif import client as onvif
from nightwire.onvif import parse, snapshot

PROBE = parse.ProbeResult(
    host="192.168.1.101",
    port=80,
    device=parse.DeviceInfo(manufacturer="AXIS", model="M1234-LVE", firmware="10.12.338"),
    profiles=[
        parse.StreamProfile("profile_1", "nvr-main", "H264", 1920, 1080, 15, 4000),
        parse.StreamProfile("profile_2", "nvr-sub", "H264", 640, 360, 10, 500),
    ],
    suggested_main="profile_1",
    suggested_sub="profile_2",
    event_source_count=5,
)


@pytest.fixture
def fake_onvif(monkeypatch):
    async def probe(host, port, username, password):
        return PROBE

    async def stream_uri(host, port, username, password, token):
        return f"rtsp://{host}/onvif-media/media.amp?profile={token}"

    async def discover(timeout=5.0):
        return [{"host": "192.168.1.101", "port": 80, "xaddrs": "", "scopes": [], "name": "AXIS", "hardware": ""}]

    monkeypatch.setattr(onvif, "probe", probe)
    monkeypatch.setattr(onvif, "stream_uri", stream_uri)
    monkeypatch.setattr(onvif, "discover", discover)
    return onvif


def create(client, **overrides):
    body = {
        "name": "Front Door",
        "host": "192.168.1.101",
        "username": "nvr",
        "password": "secret",
        "main_profile": "profile_1",
        "sub_profile": "profile_2",
    }
    body.update(overrides)
    return client.post("/api/cameras", json=body)


def test_camera_routes_need_a_session(client):
    for path in ["/api/cameras", "/api/cameras/front/snapshot.jpg"]:
        assert client.get(path).status_code == 401, path
    for path in ["/api/discover", "/api/cameras/probe"]:
        assert client.post(path, json={}).status_code == 401, path


def test_list_is_empty_on_a_fresh_install(signed_in):
    assert signed_in.get("/api/cameras").json() == {"cameras": []}


def test_probe_returns_profiles_and_suggestions(signed_in, fake_onvif):
    response = signed_in.post("/api/cameras/probe", json={"host": "192.168.1.101"})
    assert response.status_code == 200
    body = response.json()
    assert body["device"]["model"] == "M1234-LVE"
    assert body["suggested_main"] == "profile_1"
    assert body["suggested_sub"] == "profile_2"
    assert body["profiles"][0]["summary"].startswith("nvr-main")
    assert body["suggested_id"]


def test_add_a_camera_resolves_rtsp_from_the_chosen_profiles(signed_in, fake_onvif):
    response = create(signed_in)
    assert response.status_code == 201
    camera = response.json()["camera"]
    assert camera["main_uri"].endswith("profile=profile_1")
    assert camera["sub_uri"].endswith("profile=profile_2")
    assert camera["main_stream"]["summary"].startswith("nvr-main")
    assert "password" not in camera and "password_enc" not in camera


def test_add_a_camera_by_rtsp_url_without_onvif(signed_in):
    response = create(
        signed_in,
        main_profile=None,
        sub_profile=None,
        main_uri="rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
    )
    assert response.status_code == 201


def test_add_without_a_profile_or_uri_is_a_400(signed_in):
    response = create(signed_in, main_profile=None, sub_profile=None)
    assert response.status_code == 400
    assert "main stream profile" in response.json()["error"]


def test_duplicate_id_is_a_400(signed_in, fake_onvif):
    create(signed_in, id="front")
    assert create(signed_in, id="front").status_code == 400


def test_discover_marks_cameras_already_added(signed_in, fake_onvif):
    create(signed_in, id="front")
    body = signed_in.post("/api/discover", json={}).json()
    assert body["devices"][0]["already_added"] is True
    assert body["already_added"] == 1


def test_patch_and_toggle_record(signed_in, fake_onvif):
    create(signed_in, id="front")
    response = signed_in.patch("/api/cameras/front", json={"name": "Porch", "record": False})
    assert response.status_code == 200
    assert response.json()["camera"]["name"] == "Porch"
    assert response.json()["camera"]["record"] is False


def test_patch_an_unknown_camera_is_a_404(signed_in):
    assert signed_in.patch("/api/cameras/nope", json={"name": "x"}).status_code == 404


def test_delete_keeps_footage_unless_asked(signed_in, fake_onvif):
    create(signed_in, id="front")
    response = signed_in.delete("/api/cameras/front")
    assert response.status_code == 200
    assert response.json()["recordings_deleted"] == 0
    assert signed_in.get("/api/cameras").json() == {"cameras": []}


def test_status_says_the_recorder_is_not_built_yet(signed_in, fake_onvif):
    create(signed_in, id="front")
    body = signed_in.get("/api/cameras/front/status").json()
    assert body["recorder"]["state"] == "not-started"
    assert "not running" in body["recorder"]["detail"]


def test_status_of_a_disabled_camera(signed_in, fake_onvif):
    create(signed_in, id="front")
    signed_in.patch("/api/cameras/front", json={"record": False})
    body = signed_in.get("/api/cameras/front/status").json()
    assert body["recorder"]["state"] == "disabled"


def test_test_connection_stores_the_result(signed_in, fake_onvif):
    create(signed_in, id="front")
    response = signed_in.post("/api/cameras/front/test")
    assert response.status_code == 200
    camera = response.json()["camera"]
    assert camera["model"] == "M1234-LVE"
    assert camera["event_source_count"] == 5
    assert camera["last_error"] is None


def test_test_connection_records_a_failure(signed_in, fake_onvif, monkeypatch):
    create(signed_in, id="front")

    async def refuse(*args, **kwargs):
        raise onvif.OnvifError("192.168.1.101:80 refused ONVIF: 401 Unauthorized")

    monkeypatch.setattr(onvif, "probe", refuse)
    assert signed_in.post("/api/cameras/front/test").status_code == 502
    assert "refused" in signed_in.get("/api/cameras/front/status").json()["last_error"]


def test_a_missing_dependency_is_a_503_not_a_500(signed_in, monkeypatch):
    async def missing(*args, **kwargs):
        raise onvif.OnvifUnavailable("ONVIF support needs `onvif-zeep-async`.")

    monkeypatch.setattr(onvif, "discover", missing)
    response = signed_in.post("/api/discover", json={})
    assert response.status_code == 503
    assert response.json()["dependency_missing"] is True


def test_snapshot_returns_jpeg_bytes(signed_in, fake_onvif, monkeypatch):
    create(signed_in, id="front")

    async def snapshot_uri(*args, **kwargs):
        return "http://192.168.1.101/onvif-media/jpg/image.cgi"

    async def fetch(uri, username, password):
        return b"\xff\xd8\xff-not-a-real-jpeg"

    monkeypatch.setattr(onvif, "snapshot_uri", snapshot_uri)
    monkeypatch.setattr(snapshot, "fetch", fetch)

    response = signed_in.get("/api/cameras/front/snapshot.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8\xff")


def test_snapshot_failure_is_a_502_with_a_reason(signed_in, fake_onvif, monkeypatch):
    create(signed_in, id="front")

    async def snapshot_uri(*args, **kwargs):
        return "http://192.168.1.101/jpg"

    async def fetch(uri, username, password):
        raise snapshot.SnapshotError("Snapshot failed: HTTP 401")

    monkeypatch.setattr(onvif, "snapshot_uri", snapshot_uri)
    monkeypatch.setattr(snapshot, "fetch", fetch)

    response = signed_in.get("/api/cameras/front/snapshot.jpg")
    assert response.status_code == 502
    assert "401" in response.json()["error"]


def test_health_still_reports_healthy_with_a_camera_added(signed_in, fake_onvif):
    create(signed_in, id="front")
    body = signed_in.get("/api/health").json()
    assert body["status"] == "healthy"
    assert body["components"]["recorders"]["state"] == "not-started"


# ------------------------------------------------------- recorder status


class FakeSupervisor:
    """Stands in for the real supervisor so routes can be tested without ffmpeg."""

    def __init__(self, states):
        self.states = states
        self.started = True

    def status(self, camera_id):
        state = self.states.get(camera_id, "disabled")
        return {
            "state": state,
            "detail": f"recording to 60s segments" if state == "recording" else state,
            "last_segment_at": "2026-10-27T14:32:00+00:00",
            "last_segment_ms": 1793543520000,
            "seconds_since_segment": 12.0,
            "restarts": 0,
            "watchdog_kills": 1,
            "stderr_tail": ["14:31:00 no new segment for 120s, restarting ffmpeg"],
        }

    def health(self):
        total = len(self.states)
        recording = sum(1 for s in self.states.values() if s == "recording")
        if recording == total:
            return True, "recording", f"{recording} of {total} recording"
        return True, "degraded", f"{recording} of {total} recording; attention: see Nodes"


@pytest.fixture
def fake_recorder(monkeypatch):
    from nightwire import health as health_mod
    from nightwire import recorders

    def install(states):
        supervisor = FakeSupervisor(states)
        recorders.register(supervisor)
        health_mod.register("recorders", supervisor.health)
        return supervisor

    yield install
    recorders.register(None)
    health_mod.reset()


def test_status_reports_real_recorder_state(signed_in, fake_onvif, fake_recorder):
    create(signed_in, id="front")
    fake_recorder({"front": "recording"})
    body = signed_in.get("/api/cameras/front/status").json()
    assert body["recorder"]["state"] == "recording"
    assert body["recorder"]["last_segment_at"].startswith("2026-10-27")
    assert body["recorder"]["stderr_tail"]
    assert body["recorder"]["watchdog_kills"] == 1


def test_a_reconnecting_camera_does_not_make_the_process_unhealthy(
    signed_in, fake_onvif, fake_recorder
):
    """The Docker healthcheck restarts the container on unhealthy.

    One unreachable camera must not trigger that, or recording stops for the
    cameras that are fine. The fault is reported, not escalated.
    """
    create(signed_in, id="front")
    create(signed_in, id="yard", name="Backyard", host="192.168.1.103")
    fake_recorder({"front": "recording", "yard": "reconnecting"})

    response = signed_in.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["components"]["recorders"]["state"] == "degraded"
    assert "1 of 2 recording" in body["components"]["recorders"]["detail"]


def test_all_cameras_recording_is_plainly_healthy(signed_in, fake_onvif, fake_recorder):
    create(signed_in, id="front")
    fake_recorder({"front": "recording"})
    body = signed_in.get("/api/health").json()
    assert body["components"]["recorders"]["state"] == "recording"
    assert body["components"]["recorders"]["detail"] == "1 of 1 recording"


def test_a_stopped_supervisor_is_unhealthy(signed_in, fake_onvif):
    """A process-wide fault is exactly what health should fail on."""
    from nightwire import health as health_mod

    create(signed_in, id="front")
    health_mod.register("recorders", lambda: (False, "stopped", "the recorder supervisor is not running"))
    response = signed_in.get("/api/health")
    assert response.status_code == 503
    assert response.json()["components"]["recorders"]["state"] == "stopped"
