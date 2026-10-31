/* Wall clock to player position across gaps.
 *
 * The case that matters: a day whose recording starts at 14:02. Media time 0
 * is 14:02, not midnight, so seeking by offset-from-midnight played from the
 * beginning of the footage wherever you clicked. */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { fragmentsOf, hasFrameAt, mediaTimeFor, wallClockFor } from '../src/lib/playclock.js';

const AT = (text) => new Date(`2026-10-30T${text}Z`).getTime();

/* Two runs with an hour-long gap between them:
 *   14:00:00 - 14:02:00  media   0 -  120
 *   15:02:00 - 15:04:00  media 120 -  240   */
const FRAGMENTS = [
  { programDateTime: AT('14:00:00'), start: 0, duration: 60 },
  { programDateTime: AT('14:01:00'), start: 60, duration: 60 },
  { programDateTime: AT('15:02:00'), start: 120, duration: 60 },
  { programDateTime: AT('15:03:00'), start: 180, duration: 60 },
];

test('a moment inside the first run maps to its offset', () => {
  assert.equal(mediaTimeFor(FRAGMENTS, AT('14:00:30')), 30);
  assert.equal(mediaTimeFor(FRAGMENTS, AT('14:01:30')), 90);
});

test('a moment after a gap skips the gap, not the media time', () => {
  // Naive arithmetic would give 3780 s; the playlist only holds 120 s before it.
  assert.equal(mediaTimeFor(FRAGMENTS, AT('15:02:30')), 150);
  assert.equal(mediaTimeFor(FRAGMENTS, AT('15:03:30')), 210);
});

test('a moment before any footage goes to the first frame, not to zero offset', () => {
  // The bug this module exists for: 00:00 on a day starting at 14:00.
  assert.equal(mediaTimeFor(FRAGMENTS, AT('00:00:00')), 0);
  assert.equal(mediaTimeFor(FRAGMENTS, AT('13:59:59')), 0);
});

test('a moment inside a gap jumps forward to where recording resumes', () => {
  assert.equal(mediaTimeFor(FRAGMENTS, AT('14:30:00')), 120);
});

test('a moment after the last frame clamps to the end', () => {
  assert.equal(mediaTimeFor(FRAGMENTS, AT('23:00:00')), 240);
});

test('an empty playlist seeks to zero rather than NaN', () => {
  assert.equal(mediaTimeFor([], AT('14:00:30')), 0);
  assert.equal(mediaTimeFor(undefined, AT('14:00:30')), 0);
  assert.equal(mediaTimeFor(FRAGMENTS, NaN), 0);
});

test('wallClockFor is the inverse, including across the gap', () => {
  assert.equal(wallClockFor(FRAGMENTS, 30), AT('14:00:30'));
  assert.equal(wallClockFor(FRAGMENTS, 150), AT('15:02:30'));
  assert.equal(wallClockFor(FRAGMENTS, 0), AT('14:00:00'));
});

test('round trip through both directions is stable', () => {
  for (const moment of ['14:00:10', '14:01:45', '15:02:05', '15:03:59']) {
    const seconds = mediaTimeFor(FRAGMENTS, AT(moment));
    assert.equal(wallClockFor(FRAGMENTS, seconds), AT(moment));
  }
});

test('wallClockFor with nothing loaded returns null, not midnight', () => {
  assert.equal(wallClockFor([], 10), null);
});

test('hasFrameAt distinguishes footage from a gap', () => {
  assert.equal(hasFrameAt(FRAGMENTS, AT('14:00:30')), true);
  assert.equal(hasFrameAt(FRAGMENTS, AT('14:30:00')), false);
  assert.equal(hasFrameAt(FRAGMENTS, AT('23:00:00')), false);
});

test('fragments with no programDateTime are ignored rather than trusted', () => {
  const mixed = [{ start: 0, duration: 60 }, ...FRAGMENTS];
  assert.equal(mediaTimeFor(mixed, AT('14:00:30')), 30);
});

test('fragmentsOf reads the loaded level, falling back to the first', () => {
  const details = { fragments: FRAGMENTS };
  assert.equal(fragmentsOf({ levels: [{ details }], currentLevel: -1 }).length, 4);
  assert.equal(fragmentsOf({ levels: [{ details: { fragments: [] } }, { details }], currentLevel: 1 }).length, 4);
  assert.deepEqual(fragmentsOf(null), []);
  assert.deepEqual(fragmentsOf({ levels: [] }), []);
});
