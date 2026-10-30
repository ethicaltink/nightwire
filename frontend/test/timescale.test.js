/* Run with `npm test` in frontend/, which is `node --test` - built into
 * Node 22, so this needs no package and works offline. */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DAY,
  HOUR,
  MINUTE,
  MIN_SPAN,
  SECOND,
  ZOOMS,
  barFor,
  clampWindow,
  dayBounds,
  formatClock,
  formatDuration,
  formatMoment,
  fractionOf,
  localDate,
  nearestZoomName,
  panBy,
  parseMoment,
  tickStep,
  ticks,
  timeAt,
  windowAround,
  zoomAt,
  zoomByName,
} from '../src/lib/timescale.js';

const DAY_START = new Date(2026, 9, 27, 0, 0, 0, 0).getTime(); // 2026-10-27 local
const BOUNDS = { start: DAY_START, end: DAY_START + DAY };

test('the three named zooms match the spec', () => {
  assert.deepEqual(
    ZOOMS.map((z) => z.name),
    ['6h', '1h', '10m'],
  );
  assert.equal(zoomByName('10m').span, 10 * MINUTE);
  assert.equal(zoomByName('nonsense').span, HOUR, 'unknown names fall back to 1h');
});

test('nearestZoomName labels the current span', () => {
  assert.equal(nearestZoomName(6 * HOUR), '6h');
  assert.equal(nearestZoomName(HOUR), '1h');
  assert.equal(nearestZoomName(9 * MINUTE), '10m');
});

test('clampWindow keeps a window inside the day', () => {
  const before = clampWindow({ start: BOUNDS.start - HOUR, end: BOUNDS.start + HOUR }, BOUNDS);
  assert.equal(before.start, BOUNDS.start);
  assert.equal(before.end - before.start, 2 * HOUR, 'span is preserved');

  const after = clampWindow({ start: BOUNDS.end - HOUR, end: BOUNDS.end + HOUR }, BOUNDS);
  assert.equal(after.end, BOUNDS.end);
});

test('clampWindow refuses to go below the minimum span', () => {
  const tiny = clampWindow({ start: DAY_START, end: DAY_START + 1 }, BOUNDS);
  assert.equal(tiny.end - tiny.start, MIN_SPAN);
});

test('clampWindow cannot exceed its bounds', () => {
  const huge = clampWindow({ start: DAY_START, end: DAY_START + 5 * DAY }, BOUNDS);
  assert.equal(huge.start, BOUNDS.start);
  assert.equal(huge.end, BOUNDS.end);
});

test('windowAround centres on a moment', () => {
  const noon = DAY_START + 12 * HOUR;
  const window = windowAround(noon, HOUR, BOUNDS);
  assert.equal(window.start, noon - 30 * MINUTE);
  assert.equal(window.end, noon + 30 * MINUTE);
});

test('windowAround near midnight stays inside the day', () => {
  const window = windowAround(DAY_START + MINUTE, HOUR, BOUNDS);
  assert.equal(window.start, BOUNDS.start);
  assert.equal(window.end - window.start, HOUR);
});

test('zoomAt keeps the anchored moment under the pointer', () => {
  const window = { start: DAY_START + 10 * HOUR, end: DAY_START + 11 * HOUR };
  const anchor = DAY_START + 10 * HOUR + 15 * MINUTE; // a quarter in
  const fractionBefore = fractionOf(anchor, window);
  const zoomed = zoomAt(window, anchor, 0.5, BOUNDS);
  assert.equal(zoomed.end - zoomed.start, 30 * MINUTE);
  assert.ok(Math.abs(fractionOf(anchor, zoomed) - fractionBefore) < 1e-9);
});

test('zoomAt out is clamped to the day, not to a sliding window', () => {
  const window = { start: DAY_START + 10 * HOUR, end: DAY_START + 11 * HOUR };
  const zoomed = zoomAt(window, DAY_START + 10 * HOUR, 100, BOUNDS);
  assert.equal(zoomed.start, BOUNDS.start);
  assert.equal(zoomed.end, BOUNDS.end);
});

test('zoomAt in stops at the minimum span', () => {
  const window = { start: DAY_START, end: DAY_START + MINUTE };
  const zoomed = zoomAt(window, DAY_START, 0.0001, BOUNDS);
  assert.equal(zoomed.end - zoomed.start, MIN_SPAN);
});

test('panBy moves and then clamps', () => {
  const window = { start: DAY_START + HOUR, end: DAY_START + 2 * HOUR };
  assert.equal(panBy(window, HOUR, BOUNDS).start, DAY_START + 2 * HOUR);
  // Panning past midnight pins to the edge without shrinking.
  const pinned = panBy(window, -5 * HOUR, BOUNDS);
  assert.equal(pinned.start, BOUNDS.start);
  assert.equal(pinned.end - pinned.start, HOUR);
});

test('fractionOf and timeAt round trip', () => {
  const window = { start: DAY_START, end: DAY_START + HOUR };
  const at = DAY_START + 20 * MINUTE;
  assert.ok(Math.abs(fractionOf(at, window) - 1 / 3) < 1e-9);
  assert.equal(timeAt(1 / 3, window), at);
});

test('fractionOf reports off-screen values rather than clamping', () => {
  const window = { start: DAY_START, end: DAY_START + HOUR };
  assert.ok(fractionOf(DAY_START - HOUR, window) < 0);
  assert.ok(fractionOf(DAY_START + 2 * HOUR, window) > 1);
});

test('barFor clips a recorded range to the window', () => {
  const window = { start: DAY_START, end: DAY_START + HOUR };
  const bar = barFor({ start_ts: DAY_START - HOUR, end_ts: DAY_START + 30 * MINUTE }, window);
  assert.equal(bar.left, 0);
  assert.ok(Math.abs(bar.width - 50) < 1e-9);
});

test('barFor returns null for a range outside the window', () => {
  const window = { start: DAY_START, end: DAY_START + HOUR };
  assert.equal(barFor({ start_ts: DAY_START + 2 * HOUR, end_ts: DAY_START + 3 * HOUR }, window), null);
});

test('barFor gives a sliver a visible minimum width', () => {
  const window = { start: DAY_START, end: DAY_START + DAY };
  const bar = barFor({ start_ts: DAY_START, end_ts: DAY_START + SECOND }, window);
  assert.ok(bar.width >= 0.1, 'a one-second range must still be visible');
});

test('tickStep picks round intervals', () => {
  // A whole day at 8 labels is 3-hourly: 3h gives exactly 8, 6h would waste
  // half the labels.
  assert.equal(tickStep(DAY, 8), 3 * HOUR);
  assert.equal(tickStep(DAY, 4), 6 * HOUR);
  assert.equal(tickStep(6 * HOUR, 8), HOUR);
  assert.equal(tickStep(HOUR, 8), 10 * MINUTE);
  assert.equal(tickStep(10 * MINUTE, 8), 2 * MINUTE);
});

test('ticks align to the interval, not to the window edge', () => {
  const window = { start: DAY_START + 37 * MINUTE, end: DAY_START + 97 * MINUTE };
  const found = ticks(window, 8);
  assert.ok(found.length > 0);
  for (const tick of found) {
    assert.equal(tick.at % (10 * MINUTE), 0, 'every tick is on a round boundary');
    assert.ok(tick.fraction >= 0 && tick.fraction <= 1);
  }
});

test('ticks on an empty window is empty, not an infinite loop', () => {
  assert.deepEqual(ticks({ start: DAY_START, end: DAY_START }), []);
});

test('formatClock and formatDuration', () => {
  const at = new Date(2026, 9, 27, 14, 32, 5).getTime();
  assert.equal(formatClock(at), '14:32:05');
  assert.equal(formatClock(at, { seconds: false }), '14:32');
  assert.equal(formatDuration(0), '0s');
  assert.equal(formatDuration(45 * SECOND), '45s');
  assert.equal(formatDuration(65 * SECOND), '1m 05s');
  assert.equal(formatDuration(2 * HOUR + 5 * MINUTE), '2h 05m');
});

test('localDate and dayBounds agree', () => {
  const bounds = dayBounds('2026-10-27');
  assert.equal(localDate(bounds.start), '2026-10-27');
  assert.equal(bounds.end - bounds.start, DAY, 'no DST change on this date');
  assert.equal(localDate(bounds.end - 1), '2026-10-27', 'the last millisecond is still that day');
});

test('deep-link moments round trip through local time', () => {
  const at = new Date(2026, 9, 27, 14, 32, 5).getTime();
  const text = formatMoment(at);
  assert.equal(text, '2026-10-27T14:32:05');
  assert.equal(parseMoment(text), at);
});

test('an explicit zone in a deep link is respected', () => {
  assert.equal(parseMoment('2026-10-27T14:32:05Z'), Date.parse('2026-10-27T14:32:05Z'));
});

test('parseMoment rejects junk rather than returning NaN', () => {
  for (const junk of ['', null, undefined, 'not a date']) {
    assert.equal(parseMoment(junk), null);
  }
});
