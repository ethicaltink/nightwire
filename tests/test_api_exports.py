"""Export endpoints. Needs FastAPI."""

import asyncio
from pathlib import Path

import pytest

from nightwire.export.worker import ExportWorker
from nightwire.recording import paths

MIN = 60_000
T0 = 1_793_000_000_000


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


def write_segments(client, count=10, camera_id="front"):
    conn = client.app.state.db
    recordings = client.app.state.settings.recordings_dir
    for index in range(count):
        start = T0 + index * MIN
        relative = f"{camera_id}/2026/10/27/14/{index}.ts"
        absolute = recordings / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(b"x" * 1024)
        conn.execute(
            "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
            " VALUES (?,?,?,?,?,1024)",
            (camera_id, start, start + MIN, MIN, relative),
        )
    conn.commit()


@pytest.fixture
def worker(client):
    """A worker with ffmpeg faked, attached the way the lifespan would."""
    calls = []

    async def fake_runner(argv, output):
        calls.append(argv)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"MP4" * 100)
        return True, ""

    instance = ExportWorker(
        client.app.state.db,
        client.app.state.settings.recordings_dir,
        tmp_dir=client.app.state.settings.recordings_dir.parent / "tmp",
        runner=fake_runner,
    )
    instance.calls = calls
    client.app.state.exports = instance
    yield instance
    client.app.state.exports = None


def test_export_routes_need_a_session(client):
    assert client.get("/api/exports").status_code == 401
    assert client.post("/api/exports", json={}).status_code == 401
    assert client.get("/api/exports/abc").status_code == 401
    assert client.delete("/api/exports/abc").status_code == 401
    assert client.get("/api/exports/abc/download").status_code == 401


def test_the_vault_starts_empty(signed_in, worker):
    body = signed_in.get("/api/exports").json()
    assert body["exports"] == []
    assert body["status"]["queued"] == 0


def test_creating_an_export_queues_it(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    response = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + 2 * MIN}
    )
    assert response.status_code == 201
    export = response.json()["export"]
    assert export["status"] == "queued"
    assert export["name"].endswith(".mp4")
    assert export["duration_ms"] == 2 * MIN


def test_an_export_for_an_unknown_camera_is_404(signed_in, worker):
    response = signed_in.post(
        "/api/exports", json={"camera_id": "nope", "start_ts": T0, "end_ts": T0 + MIN}
    )
    assert response.status_code == 404


def test_a_range_with_no_footage_is_a_400(signed_in, worker):
    add(signed_in)
    response = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN}
    )
    assert response.status_code == 400
    assert "no recorded footage" in response.json()["error"]


def test_a_clip_over_two_hours_is_a_400(signed_in, worker):
    add(signed_in)
    write_segments(signed_in, count=200)
    response = signed_in.post(
        "/api/exports",
        json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + 3 * 60 * MIN},
    )
    assert response.status_code == 400
    assert "limited to 2 hours" in response.json()["error"]


def test_polling_an_export_through_to_done(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    created = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + 2 * MIN}
    ).json()["export"]

    assert signed_in.get(f"/api/exports/{created['id']}").json()["export"]["status"] == "queued"
    asyncio.run(worker.run_one(created["id"]))

    polled = signed_in.get(f"/api/exports/{created['id']}").json()["export"]
    assert polled["status"] == "done"
    assert polled["download_url"] == f"/api/exports/{created['id']}/download"


def test_downloading_a_finished_export(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    created = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN}
    ).json()["export"]
    asyncio.run(worker.run_one(created["id"]))

    response = signed_in.get(f"/api/exports/{created['id']}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert created["name"] in response.headers["content-disposition"]
    assert response.content.startswith(b"MP4")


def test_downloading_a_queued_export_is_a_409(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    created = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN}
    ).json()["export"]
    response = signed_in.get(f"/api/exports/{created['id']}/download")
    assert response.status_code == 409
    assert "queued" in response.json()["error"]


def test_downloading_an_export_whose_file_vanished_is_a_404(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    created = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN}
    ).json()["export"]
    finished = asyncio.run(worker.run_one(created["id"]))
    paths.absolute_path(signed_in.app.state.settings.recordings_dir, finished["path"]).unlink()
    assert signed_in.get(f"/api/exports/{created['id']}/download").status_code == 404


def test_downloading_an_unknown_export_is_a_404(signed_in, worker):
    assert signed_in.get("/api/exports/nope/download").status_code == 404


def test_deleting_an_export(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    created = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN}
    ).json()["export"]
    asyncio.run(worker.run_one(created["id"]))

    response = signed_in.delete(f"/api/exports/{created['id']}")
    assert response.status_code == 200
    assert response.json()["file_removed"] is True
    assert signed_in.get("/api/exports").json()["exports"] == []


def test_deleting_an_unknown_export_is_a_404(signed_in, worker):
    assert signed_in.delete("/api/exports/nope").status_code == 404


def test_the_vault_lists_what_was_made(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    for index in range(2):
        signed_in.post(
            "/api/exports",
            json={
                "camera_id": "front",
                "start_ts": T0 + index * MIN,
                "end_ts": T0 + (index + 1) * MIN,
            },
        )
    body = signed_in.get("/api/exports").json()
    assert len(body["exports"]) == 2
    assert body["status"]["queued"] == 2


def test_a_precise_export_is_marked_as_such(signed_in, worker):
    add(signed_in)
    write_segments(signed_in)
    created = signed_in.post(
        "/api/exports",
        json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN, "precise": True},
    ).json()["export"]
    assert created["precise"] is True
    asyncio.run(worker.run_one(created["id"]))
    assert "libx264" in " ".join(worker.calls[0])


def test_a_clip_spanning_a_gap_is_allowed_and_flagged(signed_in, worker):
    add(signed_in)
    conn = signed_in.app.state.db
    recordings = signed_in.app.state.settings.recordings_dir
    for index, start in enumerate([T0, T0 + 10 * MIN]):
        relative = f"front/2026/10/27/14/{index}.ts"
        absolute = recordings / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(b"x" * 1024)
        conn.execute(
            "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
            " VALUES ('front', ?, ?, ?, ?, 1024)",
            (start, start + MIN, MIN, relative),
        )
    conn.commit()

    created = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + 11 * MIN}
    ).json()["export"]
    assert created["has_gaps"] is True


def test_without_a_worker_creating_is_a_503(signed_in):
    signed_in.app.state.exports = None
    add(signed_in)
    response = signed_in.post(
        "/api/exports", json={"camera_id": "front", "start_ts": T0, "end_ts": T0 + MIN}
    )
    assert response.status_code == 503
