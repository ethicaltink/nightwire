-- Additions for motion events and camera clock offset.
--
-- Event times come from the camera's clock and segment times from the NVR's,
-- and the timeline draws one over the other. These two columns hold the
-- measured offset so event timestamps can be corrected onto the NVR clock,
-- with the raw camera time kept in `events.camera_ts` (added in 001).

ALTER TABLE cameras ADD COLUMN clock_offset_ms INTEGER;
ALTER TABLE cameras ADD COLUMN clock_checked_at INTEGER;

-- The Events panel and the prev/next buttons filter by source within a day.
CREATE INDEX idx_evt_source_start ON events(source_id, start_ts);

-- Finding events still open is a hot path: the live glow asks on every page
-- load, and the startup reconcile closes whatever a crash left behind.
CREATE INDEX idx_evt_open ON events(camera_id) WHERE end_ts IS NULL;
