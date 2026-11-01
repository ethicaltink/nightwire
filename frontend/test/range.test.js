/* The Rewind range and the clip selection inside it. */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MIN_RANGE,
  clockOf,
  dateOf,
  dayRange,
  describeRange,
  formatSpan,
  handleNear,
  isUsable,
  momentOf,
  moveHandle,
  normalise,
  rangeAroundFootage,
  rangeFrom,
  selectionFrom,
  timeOf,
} from '../src/lib/range.js';

const at = (text) => new Date(text).getTime();

test('momentOf builds a local moment from a date and a time', () => {
  assert.equal(momentOf('2026-10-30', '14:02'), new Date(2026, 9, 30, 14, 2, 0).getTime());
  assert.equal(momentOf('2026-10-30'), new Date(2026, 9, 30, 0, 0, 0).getTime());
  assert.equal(momentOf('', '14:02'), null);
});

test('dateOf, timeOf and clockOf read local time', () => {
  const moment = new Date(2026, 9, 30, 23, 45, 7).getTime();
  assert.equal(dateOf(moment), '2026-10-30');
  assert.equal(timeOf(moment), '23:45');
  assert.equal(clockOf(moment), '23:45:07');
});

test('rangeFrom assembles the four picker fields', () => {
  const r = rangeFrom('2026-10-30', '14:00', '2026-10-30', '16:30');
  assert.equal(clockOf(r.start), '14:00:00');
  assert.equal(clockOf(r.end), '16:30:00');
});

test('a range spanning midnight into the next day is fine', () => {
  const r = rangeFrom('2026-10-30', '22:00', '2026-10-31', '02:00');
  assert.equal(dateOf(r.start), '2026-10-30');
  assert.equal(dateOf(r.end), '2026-10-31');
  assert.equal(r.end - r.start, 4 * 60 * 60 * 1000);
});

test('a backwards range is put in order rather than rejected', () => {
  const r = rangeFrom('2026-10-30', '16:00', '2026-10-30', '14:00');
  assert.equal(clockOf(r.start), '14:00:00');
  assert.equal(clockOf(r.end), '16:00:00');
});

test('a zero-length range is widened to the minimum', () => {
  const r = rangeFrom('2026-10-30', '14:00', '2026-10-30', '14:00');
  assert.equal(r.end - r.start, MIN_RANGE);
});

test('dayRange covers a whole local day', () => {
  const r = dayRange('2026-10-30');
  assert.equal(clockOf(r.start), '00:00:00');
  assert.equal(dateOf(r.end), '2026-10-31');
});

test('dayRange across the autumn clock change is 25 hours', () => {
  // US clocks go back on 2026-11-01; that local day has 25 hours in it.
  const r = dayRange('2026-11-01');
  const hours = (r.end - r.start) / 3_600_000;
  assert.ok(hours === 25 || hours === 24, `expected 24 or 25 hours, got ${hours}`);
});

test('rangeAroundFootage spans the recorded runs with padding', () => {
  const ranges = [
    { start_ts: at('2026-10-30T14:00:00Z'), end_ts: at('2026-10-30T14:30:00Z') },
    { start_ts: at('2026-10-30T16:00:00Z'), end_ts: at('2026-10-30T16:10:00Z') },
  ];
  const r = rangeAroundFootage(ranges, 60_000);
  assert.equal(r.start, at('2026-10-30T13:59:00Z'));
  assert.equal(r.end, at('2026-10-30T16:11:00Z'));
});

test('rangeAroundFootage with nothing recorded', () => {
  assert.equal(rangeAroundFootage([]), null);
  assert.equal(rangeAroundFootage(undefined), null);
});

test('formatSpan reads as a duration', () => {
  assert.equal(formatSpan(45_000), '45s');
  assert.equal(formatSpan(12 * 60_000 + 30_000), '12m 30s');
  assert.equal(formatSpan(2 * 3_600_000 + 5 * 60_000), '2h 05m');
});

test('describeRange collapses a same-day range', () => {
  const r = rangeFrom('2026-10-30', '14:00', '2026-10-30', '16:30');
  assert.match(describeRange(r), /^2026-10-30 14:00:00 to 16:30:00 \(2h 30m\)$/);
});

test('describeRange keeps both dates when it spans days', () => {
  const r = rangeFrom('2026-10-30', '22:00', '2026-10-31', '02:00');
  assert.match(describeRange(r), /2026-10-30 22:00:00 to 2026-10-31 02:00:00/);
});

// ------------------------------------------------------------ clip selection

test('a selection is ordered however the drag went', () => {
  assert.deepEqual(selectionFrom(200, 100), { start: 100, end: 200 });
  assert.deepEqual(selectionFrom(100, 200), { start: 100, end: 200 });
  assert.equal(selectionFrom(null, 200), null);
});

test('a stray click is not a usable clip', () => {
  assert.equal(isUsable(selectionFrom(1000, 1000)), false);
  assert.equal(isUsable(selectionFrom(1000, 1500)), false); // half a second
  assert.equal(isUsable(selectionFrom(1000, 4000)), true);
  assert.equal(isUsable(null), false);
});

test('handleNear finds the end being grabbed', () => {
  const s = { start: 10_000, end: 20_000 };
  assert.equal(handleNear(s, 10_200, 500), 'start');
  assert.equal(handleNear(s, 19_900, 500), 'end');
  assert.equal(handleNear(s, 15_000, 500), null);
  assert.equal(handleNear(null, 10_000, 500), null);
});

test('handleNear prefers the start on an exact tie', () => {
  assert.equal(handleNear({ start: 0, end: 1000 }, 500, 10_000), 'start');
});

test('moveHandle keeps the selection ordered when dragged past the far end', () => {
  const s = { start: 10_000, end: 20_000 };
  assert.deepEqual(moveHandle(s, 'start', 25_000), { start: 20_000, end: 25_000 });
  assert.deepEqual(moveHandle(s, 'end', 5_000), { start: 5_000, end: 10_000 });
});

test('normalise is idempotent', () => {
  const r = normalise({ start: 5_000, end: 500_000 });
  assert.deepEqual(normalise(r), r);
});
