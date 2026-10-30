/* The station layout arithmetic, under test now that there is
 * a runner. `node --test` is built into Node 22. */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_LAYOUT,
  LAYOUTS,
  capacity,
  layoutOf,
  msUntilHour,
  page,
  pageCount,
  parseStationOptions,
  selectCameras,
  showsStatusCell,
} from '../src/lib/layouts.js';

const cameras = ['front', 'drive', 'yard', 'gate', 'shed'].map((id) => ({
  camera_id: id,
  name: id,
}));

test('the five layouts the spec names all exist', () => {
  assert.deepEqual(Object.keys(LAYOUTS), ['1', '2x2', '3x3', '4x4', '1+5']);
  assert.equal(LAYOUTS['1+5'].cells, 6);
  assert.equal(LAYOUTS['1+5'].feature, true);
});

test('an unknown layout falls back to the default', () => {
  assert.equal(layoutOf('6x6'), LAYOUTS[DEFAULT_LAYOUT]);
});

test('capacity leaves room for the status cell when asked', () => {
  assert.equal(capacity('2x2'), 4);
  assert.equal(capacity('2x2', { reserveStatusCell: true }), 3);
  assert.equal(capacity('1', { reserveStatusCell: true }), 1, 'never drops below one tile');
});

test('three cameras in a 2x2 get the clock cell', () => {
  assert.equal(showsStatusCell(3, '2x2'), true);
});

test('four cameras in a 2x2 do not, so no camera is pushed off', () => {
  assert.equal(showsStatusCell(4, '2x2'), false);
});

test('no cameras means no status cell', () => {
  assert.equal(showsStatusCell(0, '2x2'), false);
});

test('one page when everything fits', () => {
  assert.equal(pageCount(3, '2x2', { reserveStatusCell: true }), 1);
  assert.deepEqual(
    page(cameras.slice(0, 3), '2x2', 0, { reserveStatusCell: true }).map((c) => c.camera_id),
    ['front', 'drive', 'yard'],
  );
});

test('cameras split into pages when they do not fit', () => {
  assert.equal(pageCount(5, '2x2'), 2);
  assert.deepEqual(
    page(cameras, '2x2', 0).map((c) => c.camera_id),
    ['front', 'drive', 'yard', 'gate'],
  );
  assert.deepEqual(
    page(cameras, '2x2', 1).map((c) => c.camera_id),
    ['shed'],
  );
});

test('the page index wraps, so cycling never runs off the end', () => {
  assert.deepEqual(page(cameras, '2x2', 2), page(cameras, '2x2', 0));
  assert.deepEqual(page(cameras, '2x2', -1), page(cameras, '2x2', 1));
  assert.deepEqual(page(cameras, '2x2', 97), page(cameras, '2x2', 1));
});

test('pageCount on an empty wall is one, not zero', () => {
  assert.equal(pageCount(0, '2x2'), 1);
});

test('?cams filters and orders the wall', () => {
  assert.deepEqual(
    selectCameras(cameras, 'yard,front').map((c) => c.camera_id),
    ['yard', 'front'],
  );
});

test('?cams ignores unknown ids rather than rendering a hole', () => {
  assert.deepEqual(
    selectCameras(cameras, 'front,nope').map((c) => c.camera_id),
    ['front'],
  );
});

test('an empty ?cams shows everything', () => {
  assert.equal(selectCameras(cameras, '').length, cameras.length);
  assert.equal(selectCameras(cameras, ' , ').length, cameras.length);
});

test('station options parse, with fallbacks for nonsense', () => {
  const good = parseStationOptions('?layout=1%2B5&cams=front,yard&cycle=30&reload=2');
  assert.equal(good.layout, '1+5');
  assert.equal(good.cams, 'front,yard');
  assert.equal(good.cycleSeconds, 30);
  assert.equal(good.reloadHour, 2);

  const bad = parseStationOptions('?layout=6x6&cycle=abc&reload=99');
  assert.equal(bad.layout, DEFAULT_LAYOUT);
  assert.equal(bad.cycleSeconds, 0, 'a bad cycle means no cycling');
  assert.equal(bad.reloadHour, 4, 'the default reload hour');
});

test('a tiny cycle interval is floored, not honoured', () => {
  assert.equal(parseStationOptions('?cycle=1').cycleSeconds, 5);
});

test('msUntilHour always points at the future', () => {
  const now = new Date(2026, 9, 27, 10, 0, 0, 0);
  const untilFour = msUntilHour(4, now);
  assert.ok(untilFour > 0);
  // 10:00 to 04:00 the next day is 18 hours.
  assert.equal(untilFour, 18 * 60 * 60 * 1000);
  assert.equal(msUntilHour(23, now), 13 * 60 * 60 * 1000);
});
