/* The Rewind range: which stretch of time the page is looking at, and the
 * clip selected inside it. Pure, no DOM, so the arithmetic is tested with
 * `node --test`.
 *
 * Rewind used to be a day at a time, with a whole-day bar above a zoomable
 * detail bar. The day bar was a poor control: it was 24 hours wide whatever
 * had been recorded, so a few hours of footage was a sliver, and it
 * duplicated what the calendar already decides. The calendar now chooses the
 * range - start date and time to end date and time - and the timeline shows
 * exactly that.
 *
 * Every value is unix milliseconds UTC. Local time appears only in
 * formatting.
 */

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

/** The shortest range worth showing, so a range cannot collapse to nothing. */
export const MIN_RANGE = 60_000;

const pad = (n) => String(n).padStart(2, '0');

/** `YYYY-MM-DD` for a moment, in local time. */
export function dateOf(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** `HH:MM` for a moment, in local time. */
export function timeOf(ms) {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `HH:MM:SS` for a moment, in local time. */
export function clockOf(ms) {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** A date and a time of day into unix ms, in local time. */
export function momentOf(date, time = '00:00') {
  if (!date) return null;
  const [y, m, d] = date.split('-').map(Number);
  const [hh = 0, mm = 0, ss = 0] = time.split(':').map(Number);
  const at = new Date(y, m - 1, d, hh, mm, ss, 0);
  return Number.isFinite(at.getTime()) ? at.getTime() : null;
}

/** Build a range from the four fields the picker shows. */
export function rangeFrom(startDate, startTime, endDate, endTime) {
  const start = momentOf(startDate, startTime);
  const end = momentOf(endDate, endTime);
  if (start === null || end === null) return null;
  return normalise({ start, end });
}

/** Ordered, and never shorter than MIN_RANGE. */
export function normalise(range) {
  let { start, end } = range;
  if (end < start) [start, end] = [end, start];
  if (end - start < MIN_RANGE) end = start + MIN_RANGE;
  return { start, end };
}

/** The whole of a local day. */
export function dayRange(date) {
  const start = momentOf(date, '00:00');
  if (start === null) return null;
  const [y, m, d] = date.split('-').map(Number);
  // Next local midnight rather than +24h: a DST day is 23 or 25 hours.
  return { start, end: new Date(y, m - 1, d + 1).getTime() };
}

/** A range covering the recorded runs, padded a little at each end. */
export function rangeAroundFootage(ranges, pad_ms = 5 * MINUTE) {
  const recorded = (ranges ?? []).filter((r) => r && Number.isFinite(r.start_ts));
  if (!recorded.length) return null;
  const start = Math.min(...recorded.map((r) => r.start_ts));
  const end = Math.max(...recorded.map((r) => r.end_ts));
  return normalise({ start: start - pad_ms, end: end + pad_ms });
}

/** How a range reads in the picker's summary line. */
export function describeRange(range) {
  if (!range) return '';
  const sameDay = dateOf(range.start) === dateOf(range.end);
  const span = formatSpan(range.end - range.start);
  return sameDay
    ? `${dateOf(range.start)} ${clockOf(range.start)} to ${clockOf(range.end)} (${span})`
    : `${dateOf(range.start)} ${clockOf(range.start)} to ${dateOf(range.end)} ${clockOf(
        range.end,
      )} (${span})`;
}

/** `2h 05m`, `12m 30s`, `45s`. */
export function formatSpan(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}h ${pad(minutes)}m`;
  if (minutes) return `${minutes}m ${pad(seconds)}s`;
  return `${seconds}s`;
}

// ------------------------------------------------------------- clip selection

/**
 * A clip selection being dragged on the timeline.
 *
 * Kept as `{from, to}` while dragging, because a drag can go leftwards, and
 * resolved to an ordered `{start, end}` when read. Dragging is the whole
 * interaction: press at the in point, release at the out point, and both
 * ends stay adjustable afterwards by dragging a handle.
 */
export function selectionFrom(from, to) {
  if (from === null || to === null) return null;
  const start = Math.min(from, to);
  const end = Math.max(from, to);
  return { start, end };
}

/** Whether a selection is long enough to export rather than a stray click. */
export function isUsable(selection, minimum = 1000) {
  return Boolean(selection && selection.end - selection.start >= minimum);
}

/** Which handle of a selection a moment is nearest, for drag-to-adjust. */
export function handleNear(selection, atMs, toleranceMs) {
  if (!selection) return null;
  const toStart = Math.abs(atMs - selection.start);
  const toEnd = Math.abs(atMs - selection.end);
  if (Math.min(toStart, toEnd) > toleranceMs) return null;
  return toStart <= toEnd ? 'start' : 'end';
}

/** Move one end of a selection, keeping it ordered. */
export function moveHandle(selection, handle, atMs) {
  if (!selection) return null;
  const next = handle === 'start' ? { start: atMs, end: selection.end } : { start: selection.start, end: atMs };
  return selectionFrom(next.start, next.end);
}
