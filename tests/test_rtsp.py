"""Credential handling in stream URLs.

These are the tests for the security fix that keeps camera passwords out of
the database, out of go2rtc.yaml and out of every log line. The assertions
are deliberately literal about what must not appear in the output.
"""

from __future__ import annotations

import logging

import pytest

from nightwire import logs, rtsp

PASSWORD = "s3cr3t-p@ss/word:x"


def test_split_userinfo():
    assert rtsp.split_userinfo("rtsp://root:pw@10.0.0.5/axis-media/media.amp") == ("root", "pw")


def test_split_userinfo_without_credentials():
    assert rtsp.split_userinfo("rtsp://10.0.0.5/axis-media/media.amp") == (None, None)


def test_split_userinfo_on_nonsense():
    assert rtsp.split_userinfo("") == (None, None)
    assert rtsp.split_userinfo("not a url") == (None, None)


def test_has_credentials():
    assert rtsp.has_credentials("rtsp://root:pw@10.0.0.5/s")
    assert rtsp.has_credentials("rtsp://root@10.0.0.5/s")
    assert not rtsp.has_credentials("rtsp://10.0.0.5/s")
    assert not rtsp.has_credentials("")


def test_strip_credentials_keeps_everything_else():
    given = "rtsp://root:pw@10.0.0.5:554/axis-media/media.amp?videocodec=h264"
    assert rtsp.strip_credentials(given) == (
        "rtsp://10.0.0.5:554/axis-media/media.amp?videocodec=h264"
    )


def test_strip_credentials_leaves_a_clean_url_alone():
    given = "rtsp://10.0.0.5:554/axis-media/media.amp"
    assert rtsp.strip_credentials(given) is given


def test_strip_credentials_on_an_ipv6_literal():
    given = "rtsp://root:pw@[fd00::5]:554/s"
    assert rtsp.strip_credentials(given) == "rtsp://[fd00::5]:554/s"


def test_strip_credentials_on_a_path_containing_an_at_sign():
    given = "rtsp://10.0.0.5/profile@main"
    assert rtsp.strip_credentials(given) == given


def test_with_credentials_percent_encodes():
    """An Axis password may contain @, : and /, which would break the URL."""
    out = rtsp.with_credentials("rtsp://10.0.0.5:554/s", "root", PASSWORD)
    assert out == "rtsp://root:s3cr3t-p%40ss%2Fword%3Ax@10.0.0.5:554/s"
    assert rtsp.split_userinfo(out) == ("root", PASSWORD)


def test_with_credentials_replaces_any_already_there():
    out = rtsp.with_credentials("rtsp://old:old@10.0.0.5/s", "root", "new")
    assert out == "rtsp://root:new@10.0.0.5/s"


def test_with_credentials_without_a_username_strips_instead():
    assert rtsp.with_credentials("rtsp://old:old@10.0.0.5/s", None, None) == "rtsp://10.0.0.5/s"


def test_with_credentials_with_a_username_and_no_password():
    assert rtsp.with_credentials("rtsp://10.0.0.5/s", "root", None) == "rtsp://root@10.0.0.5/s"


def test_with_credentials_on_an_ipv6_literal():
    out = rtsp.with_credentials("rtsp://[fd00::5]:554/s", "root", "pw")
    assert out == "rtsp://root:pw@[fd00::5]:554/s"


def test_redact_an_ffmpeg_error_line():
    line = (
        "[rtsp @ 0x55] method DESCRIBE failed: 401 Unauthorized for "
        "rtsp://root:hunter2@10.0.0.5:554/axis-media/media.amp"
    )
    out = rtsp.redact(line)
    assert "hunter2" not in out
    assert "rtsp://***:***@10.0.0.5:554/axis-media/media.amp" in out
    assert "401 Unauthorized" in out


def test_redact_every_url_in_one_line():
    out = rtsp.redact("a rtsp://u:p@h/s and b rtsp://u2:p2@h2/s")
    assert "p@" not in out.replace("***:***@", "")
    assert out.count(rtsp.REDACTED) == 2


def test_redact_leaves_a_clean_url_alone():
    assert rtsp.redact("rtsp://10.0.0.5/s") == "rtsp://10.0.0.5/s"


def test_redact_on_nothing():
    assert rtsp.redact("") == ""


def test_redact_argv():
    argv = ["ffmpeg", "-i", "rtsp://root:pw@10.0.0.5/s", "-c", "copy"]
    assert rtsp.redact_argv(argv) == ["ffmpeg", "-i", "rtsp://***:***@10.0.0.5/s", "-c", "copy"]


# ---------------------------------------------------------- the logging filter


def record(message: str, *args) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, "f", 1, message, args, None)


def test_the_log_filter_redacts_the_message():
    entry = record("connecting to rtsp://root:pw@10.0.0.5/s")
    logs.RedactFilter().filter(entry)
    assert "pw" not in entry.getMessage()
    assert rtsp.REDACTED in entry.getMessage()


def test_the_log_filter_redacts_arguments():
    entry = record("camera %s: %s", "front", "failed on rtsp://root:pw@10.0.0.5/s")
    logs.RedactFilter().filter(entry)
    assert "root:pw" not in entry.getMessage()
    assert "camera front:" in entry.getMessage()


def test_the_log_filter_redacts_dict_arguments():
    entry = record("%(uri)s", {"uri": "rtsp://root:pw@10.0.0.5/s"})
    logs.RedactFilter().filter(entry)
    assert "root:pw" not in entry.getMessage()


def test_the_log_filter_leaves_other_lines_untouched():
    entry = record("indexed %d segments", 12)
    logs.RedactFilter().filter(entry)
    assert entry.getMessage() == "indexed 12 segments"
