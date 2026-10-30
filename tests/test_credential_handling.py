"""Credentials reach the child processes and nothing else.

The recorder and go2rtc both need the camera password inside an RTSP URL.
These tests pin where that URL is allowed to appear: ffmpeg's argv, go2rtc's
config file at mode 0600, and nowhere in the database, the stderr buffers the
UI shows, or the log.
"""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from nightwire import cameras, db
from nightwire.live import process as live_process
from nightwire.recording import supervisor as supervisor_mod

SECRET = "s" * 40


def fresh(tmp_path, **kw):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    cameras.create(
        conn,
        SECRET,
        camera_id="front",
        name="Front Door",
        host="192.168.1.101",
        main_uri="rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
        username="nvr",
        password="p@ss word",
        **kw,
    )
    return conn


# ------------------------------------------------------------- the recorder


def test_the_recorder_builds_an_authenticated_uri_per_spawn(tmp_path):
    conn = fresh(tmp_path)
    supervisor = supervisor_mod.Supervisor(
        conn, tmp_path / "recordings", secret_key=SECRET, tmp_dir=tmp_path / "tmp"
    )
    out = supervisor._authenticated_uri("front", "rtsp://192.168.1.101/x")
    assert out.startswith("rtsp://nvr:p%40ss%20word@192.168.1.101/")


def test_without_a_secret_the_recorder_falls_back_to_the_stored_uri(tmp_path):
    conn = fresh(tmp_path)
    supervisor = supervisor_mod.Supervisor(
        conn, tmp_path / "recordings", tmp_dir=tmp_path / "tmp"
    )
    assert "@" not in supervisor._authenticated_uri("front", "rtsp://192.168.1.101/x")


def test_an_unknown_camera_falls_back_rather_than_raising(tmp_path):
    conn = fresh(tmp_path)
    supervisor = supervisor_mod.Supervisor(
        conn, tmp_path / "recordings", secret_key=SECRET, tmp_dir=tmp_path / "tmp"
    )
    assert supervisor._authenticated_uri("gone", "rtsp://fallback/x") == "rtsp://fallback/x"


def test_the_stderr_buffer_the_ui_shows_is_redacted():
    recorder = supervisor_mod.CameraRecorder("front", "Front", "rtsp://10.0.0.5/x", 60)
    recorder.note(
        "[rtsp @ 0x1] 401 Unauthorized for rtsp://nvr:hunter2@192.168.1.101/media.amp"
    )
    [line] = recorder.stderr
    assert "hunter2" not in line
    assert "401 Unauthorized" in line
    assert line in str(recorder.status()["stderr_tail"])


# ---------------------------------------------------------------- go2rtc


def test_go2rtc_config_is_written_private(tmp_path):
    """The file holds camera credentials and /config is a host bind mount."""
    conn = fresh(tmp_path)
    supervisor = live_process.Go2rtcSupervisor(
        conn, tmp_path / "config", secret_key=SECRET
    )
    path = supervisor.write_config()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "nvr:p%40ss%20word@" in path.read_text()


def test_rewriting_the_config_keeps_it_private(tmp_path):
    """write_text on an existing file would keep whatever mode it had."""
    conn = fresh(tmp_path)
    supervisor = live_process.Go2rtcSupervisor(
        conn, tmp_path / "config", secret_key=SECRET
    )
    path = supervisor.write_config()
    path.chmod(0o644)
    supervisor.write_config()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_without_a_secret_the_config_has_no_credentials(tmp_path):
    conn = fresh(tmp_path)
    supervisor = live_process.Go2rtcSupervisor(conn, tmp_path / "config")
    text = supervisor.write_config().read_text()
    assert "@" not in text
    assert "front_main" in text


def test_the_go2rtc_stderr_buffer_is_redacted(tmp_path):
    conn = fresh(tmp_path)
    supervisor = live_process.Go2rtcSupervisor(conn, tmp_path / "config")
    class FakeStream:
        def __init__(self, lines):
            self.lines = list(lines)

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class FakeProcess:
        stderr = FakeStream(
            [b"streams: cannot connect to rtsp://nvr:hunter2@192.168.1.101/media.amp\n"]
        )

    asyncio.run(supervisor._drain_stderr(FakeProcess()))
    [line] = supervisor.stderr
    assert "hunter2" not in line
    assert "cannot connect" in line


# ------------------------------------------------- the ONVIF WSDL cache ----


def test_an_unusable_cache_directory_is_detected(tmp_path):
    """zeep writes a WSDL cache under the cache directory, and in the
    container the process runs as a user that could not write root's. Every
    ONVIF call failed with "[Errno 13] Permission denied: '/root/.cache'"
    before any question of camera credentials.

    The unusable case here is a path blocked by a file rather than by
    permissions, because root ignores permissions and CI runs as root."""
    from nightwire.onvif import client as onvif_client

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    assert onvif_client.cache_is_writable(blocker / "zeep") is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_cache_directory_is_detected(tmp_path):
    from nightwire.onvif import client as onvif_client

    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        assert onvif_client.cache_is_writable(blocked / "zeep") is False
    finally:
        blocked.chmod(0o700)


def test_a_writable_cache_directory_is_detected(tmp_path):
    from nightwire.onvif import client as onvif_client

    assert onvif_client.cache_is_writable(tmp_path / "zeep") is True


def test_the_cache_directory_follows_xdg(tmp_path, monkeypatch):
    from nightwire.onvif import client as onvif_client

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert onvif_client.cache_dir() == tmp_path / "xdg" / "zeep"

    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert onvif_client.cache_dir() == tmp_path / "home" / ".cache" / "zeep"


def test_a_rejected_credential_says_so(tmp_path):
    """An Axis camera answers "Sender not authorized"; on its own that reads
    like a fault in Nightwire rather than in the password typed in."""
    from nightwire.onvif import client as onvif_client

    message = onvif_client._explain("10.0.0.5", 80, Exception("Sender not authorized"))
    assert "rejected those credentials" in message
    assert "Sender not authorized" in message

    other = onvif_client._explain("10.0.0.5", 80, Exception("connection refused"))
    assert "refused ONVIF" in other
