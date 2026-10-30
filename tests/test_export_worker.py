"""The export worker: the queue, the single slot, and failure handling.

ffmpeg is replaced by an injected runner, so none of this needs it installed.
"""

import asyncio
from pathlib import Path

from nightwire import cameras, db
from nightwire.export import plan as plan_mod
from nightwire.export import worker as worker_mod
from nightwire.export.worker import ExportWorker
from nightwire.recording import paths

SECRET = "s" * 40
MIN = 60_000
T0 = 1_793_000_000_000


def build(tmp_path, segment_count=10, runner=None):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    cameras.create(
        conn, SECRET, camera_id="front", name="Front Door", host="192.168.1.101", main_uri="rtsp://x"
    )
    recordings = tmp_path / "recordings"
    for index in range(segment_count):
        start = T0 + index * MIN
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

    calls = []

    async def fake_runner(argv, output):
        calls.append({"argv": argv, "output": Path(output)})
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"MP4" * 100)
        return True, ""

    worker = ExportWorker(
        conn,
        recordings,
        tmp_dir=tmp_path / "tmp",
        runner=runner or fake_runner,
        poll_seconds=0.01,
    )
    return conn, worker, recordings, calls


# ------------------------------------------------------------------- submitting


def test_submitting_queues_a_job(tmp_path):
    conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + 2 * MIN)
    assert created["status"] == worker_mod.QUEUED
    assert created["name"].endswith(".mp4")
    assert created["duration_ms"] == 2 * MIN
    assert created["segment_count"] == 2
    assert created["download_url"] is None


def test_an_impossible_range_is_refused_at_submit(tmp_path):
    _conn, worker, _rec, _calls = build(tmp_path)
    for start, end in [(T0 + MIN, T0), (T0 + 100 * MIN, T0 + 101 * MIN)]:
        try:
            worker.submit("front", start, end)
        except plan_mod.ExportError:
            continue
        raise AssertionError(f"accepted {start}-{end}")


def test_a_full_export_store_blocks_a_new_job(tmp_path):
    """The cap refuses work; it never deletes a saved clip."""
    conn, worker, recordings, _calls = build(tmp_path)
    exports = paths.exports_dir(recordings)
    exports.mkdir(parents=True, exist_ok=True)
    evidence = exports / "earlier.mp4"
    evidence.write_bytes(b"x" * 4096)
    conn.execute("UPDATE settings SET value = '0.000001' WHERE key = 'max_exports_gb'")
    conn.commit()

    try:
        worker.submit("front", T0, T0 + 2 * MIN)
    except plan_mod.ExportError as exc:
        assert "export store is full" in str(exc)
    else:
        raise AssertionError("expected ExportError")
    assert evidence.is_file(), "the cap must not delete anything"


# ---------------------------------------------------------------------- running


def test_running_a_job_produces_a_file_and_marks_it_done(tmp_path):
    conn, worker, recordings, calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + 2 * MIN)
    finished = asyncio.run(worker.run_one(created["id"]))

    assert finished["status"] == worker_mod.DONE
    assert finished["size_bytes"] > 0
    assert finished["download_url"] == f"/api/exports/{created['id']}/download"
    assert finished["finished_at"]
    output = paths.absolute_path(recordings, finished["path"])
    assert output.is_file()
    assert output.parent == paths.exports_dir(recordings)


def test_the_command_carries_the_planned_offset_and_duration(tmp_path):
    _conn, worker, _rec, calls = build(tmp_path)
    created = worker.submit("front", T0 + 2 * MIN + 30_000, T0 + 4 * MIN)
    asyncio.run(worker.run_one(created["id"]))

    argv = calls[0]["argv"]
    assert argv[0] == "nice", "exports run at low priority"
    assert argv[argv.index("-ss") + 1] == "30.000"
    assert argv[argv.index("-t") + 1] == "90.000"


def test_the_concat_list_is_cleaned_up(tmp_path):
    _conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + 2 * MIN)
    asyncio.run(worker.run_one(created["id"]))
    leftovers = list((tmp_path / "tmp").glob("export-*.txt"))
    assert leftovers == []


def test_a_precise_job_asks_for_a_re_encode(tmp_path):
    _conn, worker, _rec, calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN, precise=True)
    asyncio.run(worker.run_one(created["id"]))
    assert "libx264" in calls[0]["argv"]


def test_a_failed_job_records_why_and_leaves_no_file(tmp_path):
    async def failing(argv, output):
        return False, "Invalid data found when processing input"

    conn, worker, recordings, _calls = build(tmp_path, runner=failing)
    created = worker.submit("front", T0, T0 + MIN)
    finished = asyncio.run(worker.run_one(created["id"]))

    assert finished["status"] == worker_mod.FAILED
    assert "Invalid data" in finished["error"]
    assert finished["download_url"] is None
    assert list(paths.exports_dir(recordings).glob("*.mp4")) == []


def test_footage_deleted_while_the_job_waited_fails_cleanly(tmp_path):
    """Retention can remove the segments while a job sits in the queue."""
    conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + 2 * MIN)
    conn.execute("DELETE FROM segments")
    conn.commit()

    finished = asyncio.run(worker.run_one(created["id"]))
    assert finished["status"] == worker_mod.FAILED
    assert "no recorded footage" in finished["error"]


def test_running_an_unknown_job_is_a_no_op(tmp_path):
    _conn, worker, _rec, _calls = build(tmp_path)
    assert asyncio.run(worker.run_one("nope")) is None


# ----------------------------------------------------------------- the one slot


def test_jobs_are_taken_oldest_first(tmp_path):
    conn, worker, _rec, calls = build(tmp_path)
    first = worker.submit("front", T0, T0 + MIN)
    conn.execute("UPDATE exports SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (first["id"],))
    conn.commit()
    second = worker.submit("front", T0 + MIN, T0 + 2 * MIN)

    queued = worker._next_queued()
    assert queued["id"] == first["id"]
    asyncio.run(worker.run_one(first["id"]))
    assert worker._next_queued()["id"] == second["id"]


def test_only_queued_jobs_are_picked_up(tmp_path):
    conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN)
    asyncio.run(worker.run_one(created["id"]))
    assert worker._next_queued() is None


def test_a_job_interrupted_by_a_restart_is_requeued(tmp_path):
    """A `running` row with no process behind it would otherwise be stuck."""
    conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN)
    conn.execute("UPDATE exports SET status = ? WHERE id = ?", (worker_mod.RUNNING, created["id"]))
    conn.commit()

    assert worker.requeue_interrupted() == 1
    assert worker_mod.get_export(conn, created["id"])["status"] == worker_mod.QUEUED


def test_requeueing_with_nothing_running_changes_nothing(tmp_path):
    _conn, worker, _rec, _calls = build(tmp_path)
    assert worker.requeue_interrupted() == 0


# ------------------------------------------------------------------- deleting


def test_deleting_removes_the_row_and_the_file(tmp_path):
    conn, worker, recordings, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN)
    finished = asyncio.run(worker.run_one(created["id"]))
    output = paths.absolute_path(recordings, finished["path"])

    result = worker.delete(created["id"])
    assert result["file_removed"] is True
    assert not output.exists()
    assert worker_mod.get_export(conn, created["id"]) is None


def test_deleting_an_unknown_export_raises(tmp_path):
    _conn, worker, _rec, _calls = build(tmp_path)
    try:
        worker.delete("nope")
    except LookupError:
        return
    raise AssertionError("expected LookupError")


def test_a_running_export_cannot_be_deleted(tmp_path):
    conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN)
    conn.execute("UPDATE exports SET status = ? WHERE id = ?", (worker_mod.RUNNING, created["id"]))
    conn.commit()
    try:
        worker.delete(created["id"])
    except plan_mod.ExportError as exc:
        assert "still being made" in str(exc)
    else:
        raise AssertionError("expected ExportError")


def test_deleting_never_reaches_outside_the_exports_folder(tmp_path):
    """Whatever the row says, only _exports is touched."""
    conn, worker, recordings, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN)
    victim = recordings / "front" / "2026" / "10" / "27" / "14" / "0.ts"
    assert victim.is_file()
    conn.execute(
        "UPDATE exports SET status = ?, path = ? WHERE id = ?",
        (worker_mod.DONE, "front/2026/10/27/14/0.ts", created["id"]),
    )
    conn.commit()

    result = worker.delete(created["id"])
    assert result["file_removed"] is False
    assert victim.is_file(), "recorded footage must never be deleted by an export delete"


# -------------------------------------------------------------------- listing


def test_listing_is_newest_first(tmp_path):
    conn, worker, _rec, _calls = build(tmp_path)
    old = worker.submit("front", T0, T0 + MIN)
    conn.execute("UPDATE exports SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old["id"],))
    conn.commit()
    new = worker.submit("front", T0 + MIN, T0 + 2 * MIN)
    assert [e["id"] for e in worker_mod.list_exports(conn)] == [new["id"], old["id"]]


def test_status_reports_the_queue_and_the_store(tmp_path):
    conn, worker, _rec, _calls = build(tmp_path)
    created = worker.submit("front", T0, T0 + MIN)
    assert worker.status()["queued"] == 1
    asyncio.run(worker.run_one(created["id"]))
    state = worker.status()
    assert state["done"] == 1
    assert state["queued"] == 0
    assert state["store_bytes"] > 0
    assert state["store_cap_bytes"] > 0


# ------------------------------------------------- the empty-output regression


def test_a_missing_output_is_rejected(tmp_path):
    reason = asyncio.run(worker_mod.verify_output(tmp_path / "nope.mp4"))
    assert reason and "no output file" in reason


def test_a_tiny_output_is_rejected_without_probing(tmp_path):
    """The bug this guards: a 257-byte MP4 with no tracks passed as success.

    ffmpeg exited 0 having written a valid but empty container - which
    happens when the recorded codec cannot be stream-copied into MP4 - and
    the job was marked done and offered for download.
    """
    tiny = tmp_path / "empty.mp4"
    tiny.write_bytes(b"\x00" * 257)
    reason = asyncio.run(worker_mod.verify_output(tiny))
    assert reason and "257 bytes" in reason


def test_a_job_whose_output_is_empty_is_marked_failed(tmp_path):
    async def runner(argv, output):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"\x00" * 257)
        reason = await worker_mod.verify_output(Path(output))
        return (reason is None), reason or ""

    conn, worker, recordings, _calls = build(tmp_path, runner=runner)
    created = worker.submit("front", T0, T0 + MIN)
    finished = asyncio.run(worker.run_one(created["id"]))

    assert finished["status"] == worker_mod.FAILED
    assert "257 bytes" in finished["error"]
    assert finished["download_url"] is None
    assert list(paths.exports_dir(recordings).glob("*.mp4")) == []
