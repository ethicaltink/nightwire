/* Time-to-pixel arithmetic for the Rewind timeline. Pure, no imports.
 *
 * All the logic that decides what the user is looking at lives here rather
 * than inside the component, so it can be tested with `npm test`
 * (`node --test`, built into Node 22 - no package needed).
 *
 * Every time value is unix milliseconds UTC. Formatting is the only place
 * local time appears.
 */

export const SECOND = 1000;
export const MINUTE = 60 * SECOND;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

/** The three detail zooms the spec names, widest first. */
export const ZOOMS = [
  { name: '6h', span: 6 * HOUR },
  { name: '1h', span: HOUR },
  { name: '10m', span: 10 * MINUTE },
];

export const MIN_SPAN = 30 * SECOND;
export const MAX_SPAN = DAY;

export function zoomByName(name) {
  return ZOOMS.find((zoom) => zoom.name === name) ?? ZOOMS[1];
}

/** Snap a span to the nearest named zoom, for labelling the current view. */
export function nearestZoomName(span) {
  let best = ZOOMS[0];
  let bestDistance = Infinity;
  for (const zoom of ZOOMS) {
    const distance = Math.abs(Math.log(zoom.span / span));
    if (distance < bestDistance) {
      best = zoom;
      bestDistance = distance;
    }
  }
  return best.name;
}

/** Keep a window inside its bounds, preserving its span where possible. */
export function clampWindow(window, bounds) {
  const boundSpan = bounds.end - bounds.start;
  let span = Math.min(Math.max(window.end - window.start, MIN_SPAN), Math.min(MAX_SPAN, boundSpan));
  let start = window.start;
  if (start < bounds.start) start = bounds.start;
  if (start + span > bounds.end) start = bounds.end - span;
  return { start, end: start + span };
}

/** A window of `span` centred on a moment, clamped to bounds. */
export function windowAround(centreMs, span, bounds) {
  return clampWindow({ start: centreMs - span / 2, end: centreMs + span / 2 }, bounds);
}

/**
 * Zoom about an anchor, the way a mouse wheel should feel: the time under the
 * pointer stays under the pointer.
 */
export function zoomAt(window, anchorMs, factor, bounds) {
    const span = window.end - window.start;
  const nextSpan = Math.min(Math.max(span * factor, MIN_SPAN), Math.min(MAX_SPAN, bounds.end - bounds.start));
  const fraction = span === 0 ? 0.5 : (anchorMs - window.start) / span;
  const start = anchorMs - fraction * nextSpan;
  return clampWindow({ start, end: start + nextSpan }, bounds);
}

export function panBy(window, deltaMs, bounds) {
  return clampWindow({ start: window.start + deltaMs, end: window.end + deltaMs }, bounds);
}

/** 0..1 position of a moment within a window. Not clamped: callers may want
 *  to know a value is off-screen. */
export function fractionOf(ms, window) {
  const span = window.end - window.start;
  if (span <= 0) return 0;
  return (ms - window.start) / span;
}

export function timeAt(fraction, window) {
  return Math.round(window.start + fraction * (window.end - window.start));
}

/** A recorded range as a percentage bar, clipped to the window.
 *  Returns null when the range is entirely outside. */
export function barFor(range, window) {
  const start = Math.max(range.start_ts, window.start);
  const end = Math.min(range.end_ts, window.end);
  if (end <= start) return null;
  const left = fractionOf(start, window) * 100;
  const width = ((end - start) / (window.end - window.start)) * 100;
  return { left, width: Math.max(width, 0.1) };
}

const TICK_STEPS = [
  SECOND,
  5 * SECOND,
  15 * SECOND,
  30 * SECOND,
  MINUTE,
  2 * MINUTE,
  5 * MINUTE,
  10 * MINUTE,
  15 * MINUTE,
  30 * MINUTE,
  HOUR,
  2 * HOUR,
  3 * HOUR,
  6 * HOUR,
  12 * HOUR,
  DAY,
];

/** Pick a round interval giving at most `maxTicks` labels for this span. */
export function tickStep(span, maxTicks = 8) {
  for (const step of TICK_STEPS) {
    if (span / step <= maxTicks) return step;
  }
  return DAY;
}

/** Tick positions across a window, aligned to the interval, not to the edge. */
export function ticks(window, maxTicks = 8) {
  const span = window.end - window.start;
  if (span <= 0) return [];
  const step = tickStep(span, maxTicks);
  const first = Math.ceil(window.start / step) * step;
  const out = [];
  for (let at = first; at <= window.end; at += step) {
    out.push({ at, fraction: fractionOf(at, window) });
  }
  return out;
}

function pad(value, width = 2) {
  return String(value).padStart(width, '0');
}

/** HH:MM:SS in the viewer's local zone. */
export function formatClock(ms, { seconds = true } = {}) {
  const date = new Date(ms);
  const base = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return seconds ? `${base}:${pad(date.getSeconds())}` : base;
}

/** A tick label: coarse spans do not need seconds. */
export function formatTick(ms, span) {
  return formatClock(ms, { seconds: span < 30 * MINUTE });
}

/** 1 h 05 m 03 s, for durations on the bar. */
export function formatDuration(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}h ${pad(minutes)}m`;
  if (minutes) return `${minutes}m ${pad(seconds)}s`;
  return `${seconds}s`;
}

/** `YYYY-MM-DD` for a moment in the viewer's local zone. */
export function localDate(ms) {
  const date = new Date(ms);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Local midnight-to-midnight bounds for a `YYYY-MM-DD` date. */
export function dayBounds(dateText) {
  const [year, month, day] = dateText.split('-').map(Number);
  const start = new Date(year, month - 1, day, 0, 0, 0, 0);
  const end = new Date(year, month - 1, day + 1, 0, 0, 0, 0);
  return { start: start.getTime(), end: end.getTime() };
}

/**
 * The deep-link parameter: `/playback/front?t=2026-10-27T14:32:05`.
 *
 * Parsed as local time when it has no zone, because that is what the user
 * reads off the timeline. An explicit Z or offset is respected.
 */
export function parseMoment(text) {
  if (!text) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text);
  const value = Date.parse(hasZone ? text : `${text}${localOffsetSuffix(text)}`);
  return Number.isFinite(value) ? value : null;
}

function localOffsetSuffix(text) {
  const probe = new Date(text.replace(' ', 'T'));
  if (!Number.isFinite(probe.getTime())) return 'Z';
  const minutes = -probe.getTimezoneOffset();
  const sign = minutes >= 0 ? '+' : '-';
  const absolute = Math.abs(minutes);
  return `${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
}

/** The `?t=` form: local wall-clock, seconds precision, no zone. */
export function formatMoment(ms) {
  const date = new Date(ms);
  return (
    `${localDate(ms)}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}
