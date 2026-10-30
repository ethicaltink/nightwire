/* Motion event helpers for the timeline and the Events panel. Pure.
 *
 * The lane layout, the density row, prev/next navigation and the clip range
 * are all arithmetic, so they live here and are covered by `npm test`.
 */

import { barFor, fractionOf } from './timescale.js';

export const CLIP_LEAD_MS = 5000; // "Clip this event" starts 5 s before
export const CLIP_TAIL_MS = 10000; // ...and ends 10 s after
export const SEEK_LEAD_MS = 5000; // clicking an event plays from 5 s before
export const DENSITY_BUCKETS = 96; // 15-minute buckets across a day

/** An event that has not closed yet. */
export function isOpen(event) {
  return event.end_ts === null || event.end_ts === undefined;
}

/** The end to draw with: an open event runs to the edge of the window. */
export function drawEnd(event, window) {
  return isOpen(event) ? window.end : event.end_ts;
}

/** One event as a percentage bar, or null when it is outside the window. */
export function barForEvent(event, window) {
  return barFor({ start_ts: event.start_ts, end_ts: drawEnd(event, window) }, window);
}

/** A point event's position, as a percentage. Points have no width. */
export function markerFor(event, window) {
  const fraction = fractionOf(event.start_ts, window);
  if (fraction < 0 || fraction > 1) return null;
  return { left: fraction * 100 };
}

/** Events grouped into one lane per source, in the sources' own order. */
export function lanes(events, sources) {
  const bySource = new Map();
  for (const source of sources) bySource.set(source.id, { source, events: [] });
  for (const event of events) {
    const lane = bySource.get(event.source_id);
    if (lane) lane.events.push(event);
  }
  return [...bySource.values()];
}

/** Counts per bucket across a window, for the density row on the day bar. */
export function density(events, window, buckets = DENSITY_BUCKETS) {
  const counts = new Array(buckets).fill(0);
  const span = window.end - window.start;
  if (span <= 0) return counts;
  for (const event of events) {
    const fraction = (event.start_ts - window.start) / span;
    if (fraction < 0 || fraction >= 1) continue;
    counts[Math.min(buckets - 1, Math.floor(fraction * buckets))] += 1;
  }
  return counts;
}

/** The busiest bucket, so the row can be scaled against something. */
export function peak(counts) {
  return counts.reduce((most, count) => Math.max(most, count), 0);
}

function visible(events, hiddenSourceIds) {
  if (!hiddenSourceIds || hiddenSourceIds.size === 0) return events;
  return events.filter((event) => !hiddenSourceIds.has(event.source_id));
}

/**
 * The next event starting after a moment.
 *
 * Hidden sources are skipped, which the spec requires: a filtered-out lane
 * must not be reachable with the next button either.
 */
export function nextEvent(events, afterMs, hiddenSourceIds) {
  const candidates = visible(events, hiddenSourceIds)
    .filter((event) => event.start_ts > afterMs)
    .sort((a, b) => a.start_ts - b.start_ts);
  return candidates[0] ?? null;
}

export function prevEvent(events, beforeMs, hiddenSourceIds) {
  const candidates = visible(events, hiddenSourceIds)
    .filter((event) => event.start_ts < beforeMs)
    .sort((a, b) => b.start_ts - a.start_ts);
  return candidates[0] ?? null;
}

/** Which event, if any, the playhead currently sits inside. */
export function currentEvent(events, atMs, window) {
  for (const event of events) {
    if (event.start_ts <= atMs && atMs <= drawEnd(event, window)) return event;
  }
  return null;
}

/** Where clicking an event should seek: a little before it starts. */
export function seekTarget(event) {
  return event.start_ts - SEEK_LEAD_MS;
}

/** "Clip this event": in 5 s before, out 10 s after. */
export function clipRange(event, window) {
  return {
    in: event.start_ts - CLIP_LEAD_MS,
    out: drawEnd(event, window) + CLIP_TAIL_MS,
  };
}

/** Newest first, which is the order the Events panel lists them. */
export function newestFirst(events) {
  return [...events].sort((a, b) => b.start_ts - a.start_ts);
}

/**
 * Apply a live SSE message to a list of events.
 *
 * Returns a new array. An `open` for an event already present replaces it,
 * which is what a reopen (a merge) looks like from the browser's side.
 */
export function applyLive(events, message) {
  const incoming = message?.event;
  if (!incoming) return events;
  const without = events.filter((event) => event.id !== incoming.id);
  return [...without, incoming].sort((a, b) => a.start_ts - b.start_ts);
}

/** Which cameras currently have motion, from a list of open events. */
export function motionByCamera(openEvents) {
  const map = new Map();
  for (const event of openEvents) {
    if (!isOpen(event)) continue;
    const existing = map.get(event.camera_id);
    if (!existing || event.start_ts > existing.start_ts) map.set(event.camera_id, event);
  }
  return map;
}
