"""Playback routes. Needs FastAPI."""

from datetime import datetime, timezone

from nightwire.recording import paths

MIN = 60_000
HOUR = 60 * MIN


def add(client, camera_id="front"):
    return client.post(
        "/api/cameras",
        json={
            "id": camera_id,
            "name": camera_id,
            "host": "192.168.1.101",
            "main_uri": "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
        },
    )


def write_segments(client, camera_id, count, start_ms, duration=MIN, step=None, with_files=True):
    """Insert segment rows, and optionally the files they point at."""
    conn = client.app.state.db
    recordings = client.app.state.settings.recordings_dir
    step = duration if step is None else step
    created = []
    for index in range(count):
        start = start_ms + index * step
        stamp = datetime.fromtimestamp(start / 1000, timezone.utc).strftime("%Y%m%dT%H%M%S")
        relative = f"{camera_id}/2026/10/27/14/{stamp}.ts"
        if with_files:
            absolute = recordings / relative
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(b"x" * 2048)
        conn.execute(
            "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
            " VALUES (?,?,?,?,?,?)",
            (camera_id, start, start + duration, duration, relative, 2048),
        )
        created.append(relative)
    conn.commit()
    return created


def day_start(client, date="2026-10-27"):
    from nightwire.playback import timeline

    start, _ = timeline.day_bounds(date, client.app.state.settings.tzinfo())
    return start


# -------------------------------------------------------------------- gating


def test_playback_routes_need_a_session(client):
    for path in [
        "/api/cameras/front/timeline",
        "/api/cameras/front/vod.m3u8",
        "/api/cameras/front/thumb",
        "/api/cameras/front/segment?t=1",
    ]:
        assert client.get(path).status_code == 401, path


def test_media_needs_a_session(client):
    """Recorded footage must never be reachable without signing in."""
    response = client.get("/media/front/2026/10/27/14/x.ts")
    assert response.status_code == 401


# ------------------------------------------------------------------ timeline


def test_timeline_of_an_unknown_camera_is_404(signed_in):
    assert signed_in.get("/api/cameras/nope/timeline").status_code == 404


def test_timeline_of_an_empty_day(signed_in):
    add(signed_in)
    body = signed_in.get("/api/cameras/front/timeline?date=2026-10-27").json()
    assert body["camera_id"] == "front"
    assert body["ranges"] == []
    assert body["coverage_percent"] == 0.0


def test_timeline_merges_contiguous_footage(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    write_segments(signed_in, "front", 10, start)
    body = signed_in.get("/api/cameras/front/timeline?date=2026-10-27").json()
    assert len(body["ranges"]) == 1
    assert body["ranges"][0]["segments"] == 10


def test_timeline_reports_a_gap(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    write_segments(signed_in, "front", 3, start)
    write_segments(signed_in, "front", 3, start + HOUR)
    body = signed_in.get("/api/cameras/front/timeline?date=2026-10-27").json()
    assert len(body["ranges"]) == 2


def test_timeline_rejects_a_bad_date(signed_in):
    add(signed_in)
    response = signed_in.get("/api/cameras/front/timeline?date=27-10-2026")
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["error"]


def test_timeline_accepts_an_explicit_window(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    write_segments(signed_in, "front", 5, start)
    body = signed_in.get(f"/api/cameras/front/timeline?start={start}&end={start + 2 * MIN}").json()
    assert body["start_ts"] == start
    assert body["segment_count"] == 2


def test_timeline_rejects_a_backwards_window(signed_in):
    add(signed_in)
    assert signed_in.get("/api/cameras/front/timeline?start=2000&end=1000").status_code == 400


# ------------------------------------------------------------------ playlist


def test_vod_playlist_is_served_with_the_hls_media_type(signed_in):
    add(signed_in)
    write_segments(signed_in, "front", 3, day_start(signed_in) + 10 * HOUR)
    response = signed_in.get("/api/cameras/front/vod.m3u8?date=2026-10-27")
    assert response.status_code == 200
    assert "mpegurl" in response.headers["content-type"]
    assert response.text.startswith("#EXTM3U")
    assert "#EXT-X-ENDLIST" in response.text


def test_vod_playlist_points_at_the_media_route(signed_in):
    add(signed_in)
    [relative] = write_segments(signed_in, "front", 1, day_start(signed_in) + 10 * HOUR)
    assert f"/media/{relative}" in signed_in.get("/api/cameras/front/vod.m3u8?date=2026-10-27").text


def test_vod_playlist_marks_a_gap(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    write_segments(signed_in, "front", 2, start)
    write_segments(signed_in, "front", 2, start + HOUR)
    text = signed_in.get("/api/cameras/front/vod.m3u8?date=2026-10-27").text
    assert text.count("#EXT-X-DISCONTINUITY") == 1


def test_vod_playlist_of_an_empty_day_is_still_valid(signed_in):
    add(signed_in)
    text = signed_in.get("/api/cameras/front/vod.m3u8?date=2026-10-27").text
    assert text.startswith("#EXTM3U")
    assert "#EXT-X-ENDLIST" in text
    assert "#EXTINF" not in text


def test_vod_of_an_unknown_camera_is_404(signed_in):
    assert signed_in.get("/api/cameras/nope/vod.m3u8").status_code == 404


# -------------------------------------------------------------------- media


def test_media_serves_a_segment_to_a_signed_in_browser(signed_in):
    add(signed_in)
    [relative] = write_segments(signed_in, "front", 1, day_start(signed_in) + 10 * HOUR)
    response = signed_in.get(f"/media/{relative}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp2t"
    assert response.headers["accept-ranges"] == "bytes"
    assert len(response.content) == 2048


def test_media_honours_a_range_request(signed_in):
    add(signed_in)
    [relative] = write_segments(signed_in, "front", 1, day_start(signed_in) + 10 * HOUR)
    response = signed_in.get(f"/media/{relative}", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 0-99/2048"
    assert len(response.content) == 100


def test_media_honours_a_suffix_range(signed_in):
    add(signed_in)
    [relative] = write_segments(signed_in, "front", 1, day_start(signed_in) + 10 * HOUR)
    response = signed_in.get(f"/media/{relative}", headers={"Range": "bytes=-50"})
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 1998-2047/2048"


def test_media_rejects_an_unsatisfiable_range(signed_in):
    add(signed_in)
    [relative] = write_segments(signed_in, "front", 1, day_start(signed_in) + 10 * HOUR)
    response = signed_in.get(f"/media/{relative}", headers={"Range": "bytes=9000-9100"})
    assert response.status_code == 416


def test_media_refuses_a_path_outside_the_volume(signed_in):
    """The one route that turns user input into a filesystem path.

    A literal `../` in a URL is normalised away by any HTTP client before it
    reaches the server, so the guard is exercised directly as well as through
    an encoded path, which does arrive intact.
    """
    from nightwire.api.playback import _safe_media_path

    recordings = signed_in.app.state.settings.recordings_dir
    recordings.mkdir(parents=True, exist_ok=True)
    for attempt in [
        "../../../etc/passwd",
        "front/../../../etc/passwd",
        "/etc/passwd",
        "front/../../etc/shadow",
    ]:
        assert _safe_media_path(recordings, attempt) is None, attempt

    # Percent-encoded separators survive normalisation and reach the route.
    for encoded in ["..%2F..%2F..%2Fetc%2Fpasswd", "front%2F..%2F..%2Fetc%2Fpasswd"]:
        assert signed_in.get(f"/media/{encoded}").status_code == 404, encoded


def test_safe_media_path_accepts_a_real_segment(signed_in):
    """The guard must not be so strict that it rejects legitimate footage."""
    from nightwire.api.playback import _safe_media_path

    add(signed_in)
    [relative] = write_segments(signed_in, "front", 1, day_start(signed_in) + 10 * HOUR)
    recordings = signed_in.app.state.settings.recordings_dir
    assert _safe_media_path(recordings, relative) is not None


def test_media_refuses_a_file_type_it_does_not_serve(signed_in):
    recordings = signed_in.app.state.settings.recordings_dir
    secret = recordings / "nightwire.db"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"not for you")
    assert signed_in.get("/media/nightwire.db").status_code == 404


def test_media_on_a_missing_file_is_404(signed_in):
    assert signed_in.get("/media/front/2026/10/27/14/nope.ts").status_code == 404


# ------------------------------------------------------------ thumb, segment


def test_thumb_serves_the_nearest_segments_thumbnail(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    [relative] = write_segments(signed_in, "front", 1, start)
    recordings = signed_in.app.state.settings.recordings_dir
    thumb = paths.thumb_path(recordings, "front", recordings / relative)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"\xff\xd8\xff")

    response = signed_in.get(f"/api/cameras/front/thumb?t={start + 30_000}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-segment-start"] == str(start)


def test_thumb_without_footage_is_404(signed_in):
    add(signed_in)
    assert signed_in.get("/api/cameras/front/thumb?t=1793000000000").status_code == 404


def test_thumb_without_a_thumbnail_file_is_404(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    write_segments(signed_in, "front", 1, start)
    assert signed_in.get(f"/api/cameras/front/thumb?t={start}").status_code == 404


def test_segment_info_describes_the_covering_segment(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    [relative] = write_segments(signed_in, "front", 1, start)
    body = signed_in.get(f"/api/cameras/front/segment?t={start + 30_000}").json()
    assert body["contains"] is True
    assert body["url"] == f"/media/{relative}"
    assert body["duration_ms"] == MIN


def test_segment_info_reports_the_nearest_when_nothing_covers_the_moment(signed_in):
    add(signed_in)
    start = day_start(signed_in) + 10 * HOUR
    write_segments(signed_in, "front", 1, start)
    body = signed_in.get(f"/api/cameras/front/segment?t={start + 10 * MIN}").json()
    assert body["contains"] is False
    assert body["start_ts"] == start


def test_segment_info_without_footage_is_404(signed_in):
    add(signed_in)
    assert signed_in.get("/api/cameras/front/segment?t=1793000000000").status_code == 404


# ------------------------------------- which days have footage, for the UI


def local_midnight_ms(client, date_text: str) -> int:
    """Local midnight for a date, in the app's display time zone."""
    from nightwire.playback import timeline as timeline_mod

    tz = client.app.state.settings.tzinfo()
    return timeline_mod.day_bounds(date_text, tz)[0]


def test_days_lists_only_days_with_footage(signed_in, recordings):
    """The Rewind calendar offers these, so a date cannot land on an empty page."""
    add(signed_in)
    write_segments(signed_in, "front", 2, local_midnight_ms(signed_in, "2026-10-28") + 9 * HOUR)
    write_segments(signed_in, "front", 1, local_midnight_ms(signed_in, "2026-10-30") + 14 * HOUR)

    body = signed_in.get("/api/cameras/front/days").json()
    assert [d["date"] for d in body["days"]] == ["2026-10-28", "2026-10-30"]
    assert body["days"][0]["segments"] == 2
    assert body["days"][0]["seconds"] == 120
    assert body["first"] == "2026-10-28"
    assert body["last"] == "2026-10-30"
    assert body["today"]


def test_days_counts_a_segment_on_the_day_it_starts(signed_in, recordings):
    """One straddling local midnight must not appear on both days."""
    add(signed_in)
    just_before = local_midnight_ms(signed_in, "2026-10-31") - 30_000
    write_segments(signed_in, "front", 1, just_before)

    body = signed_in.get("/api/cameras/front/days").json()
    assert [d["date"] for d in body["days"]] == ["2026-10-30"]


def test_days_is_empty_before_anything_is_recorded(signed_in, recordings):
    add(signed_in)
    body = signed_in.get("/api/cameras/front/days").json()
    assert body["days"] == []
    assert body["first"] is None and body["last"] is None


def test_days_404s_for_an_unknown_camera(signed_in, recordings):
    assert signed_in.get("/api/cameras/nope/days").status_code == 404


def test_days_needs_a_session(client, recordings):
    assert client.get("/api/cameras/front/days").status_code == 401
