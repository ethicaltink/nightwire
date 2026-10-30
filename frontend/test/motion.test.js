import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  CLIP_LEAD_MS,
  CLIP_TAIL_MS,
  applyLive,
  barForEvent,
  clipRange,
  currentEvent,
  density,
  drawEnd,
  isOpen,
  lanes,
  markerFor,
  motionByCamera,
  newestFirst,
  nextEvent,
  peak,
  prevEvent,
  seekTarget,
} from '../src/lib/motion.js';

const T0 = 1_793_000_000_000;
const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const WINDOW = { start: T0, end: T0 + HOUR };

function event(id, start, end, sourceId = 1, cameraId = 'front') {
  return { id, start_ts: start, end_ts: end, source_id: sourceId, camera_id: cameraId };
}

test('an event with no end is open', () => {
  assert.equal(isOpen(event(1, T0, null)), true);
  assert.equal(isOpen(event(1, T0, undefined)), true);
  assert.equal(isOpen(event(1, T0, T0 + MINUTE)), false);
});

test('an open event is drawn to the edge of the window', () => {
  assert.equal(drawEnd(event(1, T0, null), WINDOW), WINDOW.end);
  assert.equal(drawEnd(event(1, T0, T0 + MINUTE), WINDOW), T0 + MINUTE);
});

test('a span event becomes a bar clipped to the window', () => {
  const bar = barForEvent(event(1, T0 + 30 * MINUTE, T0 + 45 * MINUTE), WINDOW);
  assert.ok(Math.abs(bar.left - 50) < 1e-9);
  assert.ok(Math.abs(bar.width - 25) < 1e-9);
});

test('an event outside the window has no bar', () => {
  assert.equal(barForEvent(event(1, T0 + 2 * HOUR, T0 + 3 * HOUR), WINDOW), null);
});

test('a point event is a position, not a width', () => {
  const marker = markerFor(event(1, T0 + 15 * MINUTE, T0 + 15 * MINUTE), WINDOW);
  assert.ok(Math.abs(marker.left - 25) < 1e-9);
  assert.equal(markerFor(event(2, T0 - MINUTE, T0 - MINUTE), WINDOW), null);
});

test('one lane per source, in the sources own order', () => {
  const sources = [
    { id: 2, label: 'Street edge' },
    { id: 1, label: 'Porch' },
  ];
  const found = lanes([event(1, T0, T0 + MINUTE, 1), event(2, T0, T0 + MINUTE, 2)], sources);
  assert.deepEqual(
    found.map((lane) => lane.source.label),
    ['Street edge', 'Porch'],
  );
  assert.equal(found[0].events.length, 1);
});

test('a lane with no events still exists, so the row is visible', () => {
  const found = lanes([], [{ id: 1, label: 'Porch' }]);
  assert.equal(found.length, 1);
  assert.deepEqual(found[0].events, []);
});

test('an event whose source is gone is not drawn', () => {
  const found = lanes([event(1, T0, T0 + MINUTE, 99)], [{ id: 1 }]);
  assert.equal(found[0].events.length, 0);
});

test('density buckets count events across the window', () => {
  const counts = density([event(1, T0, T0 + 1), event(2, T0 + 1, T0 + 2)], WINDOW, 4);
  assert.deepEqual(counts, [2, 0, 0, 0]);
});

test('density ignores events outside the window', () => {
  const counts = density([event(1, T0 - HOUR, T0 - HOUR + 1)], WINDOW, 4);
  assert.deepEqual(counts, [0, 0, 0, 0]);
});

test('an event at the very end lands in the last bucket', () => {
  const counts = density([event(1, WINDOW.end - 1, WINDOW.end)], WINDOW, 4);
  assert.equal(counts[3], 1);
});

test('density on a zero-width window does not divide by zero', () => {
  assert.deepEqual(density([event(1, T0, T0)], { start: T0, end: T0 }, 3), [0, 0, 0]);
});

test('peak finds the busiest bucket', () => {
  assert.equal(peak([0, 3, 1]), 3);
  assert.equal(peak([]), 0);
});

const timeline = [
  event(1, T0 + 10 * MINUTE, T0 + 11 * MINUTE, 1),
  event(2, T0 + 20 * MINUTE, T0 + 21 * MINUTE, 2),
  event(3, T0 + 30 * MINUTE, T0 + 31 * MINUTE, 1),
];

test('next and previous walk the events', () => {
  assert.equal(nextEvent(timeline, T0).id, 1);
  assert.equal(nextEvent(timeline, T0 + 15 * MINUTE).id, 2);
  assert.equal(nextEvent(timeline, T0 + 40 * MINUTE), null);
  assert.equal(prevEvent(timeline, T0 + 25 * MINUTE).id, 2);
  assert.equal(prevEvent(timeline, T0), null);
});

test('a filtered-out source is skipped by next and previous', () => {
  const hidden = new Set([2]);
  assert.equal(nextEvent(timeline, T0 + 15 * MINUTE, hidden).id, 3);
  assert.equal(prevEvent(timeline, T0 + 25 * MINUTE, hidden).id, 1);
});

test('hiding every source leaves nothing to navigate to', () => {
  const hidden = new Set([1, 2]);
  assert.equal(nextEvent(timeline, T0, hidden), null);
});

test('the current event is the one the playhead sits inside', () => {
  assert.equal(currentEvent(timeline, T0 + 10.5 * MINUTE, WINDOW).id, 1);
  assert.equal(currentEvent(timeline, T0 + 15 * MINUTE, WINDOW), null);
});

test('the playhead sits inside an open event indefinitely', () => {
  const open = [event(9, T0 + 5 * MINUTE, null)];
  assert.equal(currentEvent(open, T0 + 50 * MINUTE, WINDOW).id, 9);
});

test('clicking an event seeks a little before it', () => {
  assert.equal(seekTarget(event(1, T0 + MINUTE, T0 + 2 * MINUTE)), T0 + MINUTE - 5 * SECOND);
});

test('clip this event brackets it', () => {
  const range = clipRange(event(1, T0 + MINUTE, T0 + 2 * MINUTE), WINDOW);
  assert.equal(range.in, T0 + MINUTE - CLIP_LEAD_MS);
  assert.equal(range.out, T0 + 2 * MINUTE + CLIP_TAIL_MS);
});

test('clipping an open event runs to the window edge', () => {
  const range = clipRange(event(1, T0 + MINUTE, null), WINDOW);
  assert.equal(range.out, WINDOW.end + CLIP_TAIL_MS);
});

test('the events panel lists newest first', () => {
  assert.deepEqual(
    newestFirst(timeline).map((e) => e.id),
    [3, 2, 1],
  );
});

test('a live message is merged in, in time order', () => {
  const next = applyLive([timeline[0]], { event: event(2, T0 + 5 * MINUTE, null) });
  assert.deepEqual(
    next.map((e) => e.id),
    [2, 1],
  );
});

test('a live message for an event already present replaces it', () => {
  const reopened = { ...timeline[0], end_ts: null };
  const next = applyLive(timeline, { event: reopened });
  assert.equal(next.length, timeline.length);
  assert.equal(next.find((e) => e.id === 1).end_ts, null);
});

test('a live message with no event changes nothing', () => {
  assert.deepEqual(applyLive(timeline, {}), timeline);
  assert.deepEqual(applyLive(timeline, null), timeline);
});

test('motion by camera keeps the newest open event per camera', () => {
  const open = [
    event(1, T0, null, 1, 'front'),
    event(2, T0 + MINUTE, null, 2, 'front'),
    event(3, T0, null, 1, 'yard'),
    event(4, T0, T0 + MINUTE, 1, 'drive'),
  ];
  const map = motionByCamera(open);
  assert.equal(map.get('front').id, 2);
  assert.equal(map.get('yard').id, 3);
  assert.equal(map.has('drive'), false, 'a closed event is not live motion');
});
