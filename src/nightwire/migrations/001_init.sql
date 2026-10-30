-- Nightwire schema, exactly as specified in .
-- All times are unix milliseconds, UTC. Local time is a display concern only.

CREATE TABLE cameras (
  id            TEXT PRIMARY KEY,          -- slug, e.g. 'front'
  name          TEXT NOT NULL,
  host          TEXT NOT NULL,
  onvif_port    INTEGER DEFAULT 80,
  username      TEXT,
  password_enc  TEXT,                      -- encrypted with NVR_SECRET_KEY (Fernet)
  main_uri      TEXT NOT NULL,             -- RTSP for recording + full-size live
  sub_uri       TEXT,                      -- RTSP for grid/station
  model         TEXT,
  firmware      TEXT,
  serial        TEXT,
  record        INTEGER DEFAULT 1,
  retention_days INTEGER DEFAULT 45,
  sort_order    INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE segments (
  id          INTEGER PRIMARY KEY,
  camera_id   TEXT NOT NULL REFERENCES cameras(id),
  start_ts    INTEGER NOT NULL,            -- unix ms, UTC
  end_ts      INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  path        TEXT NOT NULL UNIQUE,        -- relative to /recordings
  size_bytes  INTEGER NOT NULL,
  has_audio   INTEGER DEFAULT 0
);
CREATE INDEX idx_seg_cam_start ON segments(camera_id, start_ts);

CREATE TABLE exports (
  id TEXT PRIMARY KEY,
  camera_id TEXT,
  start_ts INTEGER,
  end_ts INTEGER,
  precise INTEGER,
  status TEXT,
  path TEXT,
  size_bytes INTEGER,
  error TEXT,
  created_at TEXT
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE event_sources (
  id            INTEGER PRIMARY KEY,
  camera_id     TEXT NOT NULL REFERENCES cameras(id),
  topic         TEXT NOT NULL,             -- full ONVIF topic as discovered
  source_filter TEXT,                      -- VMD profile / Fence Guard item when topics are shared
  label         TEXT NOT NULL,             -- e.g. 'Porch'
  color         TEXT NOT NULL,             -- e.g. '#FF2BD6'
  kind          TEXT NOT NULL,             -- 'span' | 'point'
  enabled       INTEGER DEFAULT 1,
  UNIQUE (camera_id, topic, source_filter)
);

CREATE TABLE events (
  id         INTEGER PRIMARY KEY,
  camera_id  TEXT NOT NULL REFERENCES cameras(id),
  source_id  INTEGER NOT NULL REFERENCES event_sources(id),
  start_ts   INTEGER NOT NULL,             -- unix ms, UTC, corrected onto the NVR clock
  camera_ts  INTEGER,                      -- raw camera UtcTime, kept for auditing
  end_ts     INTEGER,                      -- NULL while open; = start_ts for point events
  snapshot   TEXT                          -- relative to /recordings
);
CREATE INDEX idx_evt_cam_start ON events(camera_id, start_ts);

-- Defaults from and "Export and download".
INSERT INTO settings (key, value) VALUES
  ('retention_days', '45'),
  ('max_disk_percent', '90'),
  ('target_disk_percent', '85'),
  ('segment_seconds', '60'),
  ('max_exports_gb', '200'),
  ('station_reload_hour', '4');
