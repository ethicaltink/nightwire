-- Additions to `cameras`: which ONVIF profile each stream URI came from.
--
-- The base schema stores the two RTSP URIs but not which ONVIF profile they
-- came from or what that profile looks like, and the design mockups shows both
-- on every camera card ("Main nvr-main - 1920x1080 - 15 fps - H.264"). These
-- columns carry that, plus the result of the last probe so a camera that has
-- gone unreachable says so without waiting for a round trip.

ALTER TABLE cameras ADD COLUMN main_profile TEXT;      -- ONVIF profile token
ALTER TABLE cameras ADD COLUMN sub_profile TEXT;
ALTER TABLE cameras ADD COLUMN main_meta TEXT;         -- JSON: codec/w/h/fps
ALTER TABLE cameras ADD COLUMN sub_meta TEXT;
ALTER TABLE cameras ADD COLUMN event_source_count INTEGER;
ALTER TABLE cameras ADD COLUMN last_probe_at TEXT;
ALTER TABLE cameras ADD COLUMN last_error TEXT;
