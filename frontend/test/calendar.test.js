/* Month-grid arithmetic for the Rewind calendar. The cases worth pinning are
 * the ones that break date code everywhere: month edges, year boundaries,
 * and reading a day in local time rather than UTC. */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  dayFullness,
  describeDay,
  indexDays,
  isoDate,
  monthGrid,
  monthOf,
  nearestDayWithFootage,
  shiftDay,
  shiftMonth,
} from '../src/lib/calendar.js';

const DAYS = [
  { date: '2026-10-28', segments: 1440, seconds: 86400, bytes: 11_000_000_000 },
  { date: '2026-10-29', segments: 720, seconds: 43200, bytes: 5_500_000_000 },
  { date: '2026-10-30', segments: 406, seconds: 24360, bytes: 3_200_000_000 },
];

test('isoDate reads local time, not UTC', () => {
  // 23:30 local on the 30th is the 31st in UTC; the calendar must say the 30th.
  const d = new Date(2026, 9, 30, 23, 30, 0);
  assert.equal(isoDate(d), '2026-10-30');
});

test('monthOf and shiftMonth', () => {
  assert.equal(monthOf('2026-10-30'), '2026-10');
  assert.equal(shiftMonth('2026-10', 1), '2026-11');
  assert.equal(shiftMonth('2026-01', -1), '2025-12');
  assert.equal(shiftMonth('2026-12', 1), '2027-01');
});

test('shiftDay crosses months and years', () => {
  assert.equal(shiftDay('2026-10-30', 1), '2026-10-31');
  assert.equal(shiftDay('2026-10-31', 1), '2026-11-01');
  assert.equal(shiftDay('2026-01-01', -1), '2025-12-31');
});

test('the grid is always six rows of seven', () => {
  for (const month of ['2026-02', '2026-10', '2027-01', '2024-02']) {
    assert.equal(monthGrid(month, {}).length, 42);
  }
});

test('the grid starts on a Sunday and marks neighbouring days', () => {
  const cells = monthGrid('2026-10', {});
  assert.equal(new Date(cells[0].date + 'T00:00:00').getDay(), 0);
  assert.equal(cells[0].outside, true);
  const first = cells.find((c) => c.date === '2026-10-01');
  assert.equal(first.outside, false);
});

test('days with footage are marked, days without are not', () => {
  const cells = monthGrid('2026-10', indexDays(DAYS), { selected: '2026-10-30' });
  const withFootage = cells.filter((c) => c.hasFootage).map((c) => c.date);
  assert.deepEqual(withFootage, ['2026-10-28', '2026-10-29', '2026-10-30']);

  const empty = cells.find((c) => c.date === '2026-10-27');
  assert.equal(empty.hasFootage, false);
  assert.equal(empty.segments, 0);
});

test('today and the selected day are flagged', () => {
  const cells = monthGrid('2026-10', indexDays(DAYS), {
    today: '2026-10-31',
    selected: '2026-10-30',
  });
  assert.equal(cells.find((c) => c.date === '2026-10-31').isToday, true);
  assert.equal(cells.find((c) => c.date === '2026-10-30').isSelected, true);
  assert.equal(cells.find((c) => c.date === '2026-10-29').isSelected, false);
});

test('fullness is measured against the whole day, not the fullest day', () => {
  assert.equal(dayFullness(86400), 1);
  assert.equal(dayFullness(43200), 0.5);
  assert.equal(dayFullness(0), 0);
  // A DST repeat makes a 25-hour day; it must not exceed 1.
  assert.equal(dayFullness(90000), 1);
});

test('nearestDayWithFootage prefers the latest day at or before', () => {
  assert.equal(nearestDayWithFootage('2026-10-31', DAYS), '2026-10-30');
  assert.equal(nearestDayWithFootage('2026-10-29', DAYS), '2026-10-29');
});

test('nearestDayWithFootage falls forward when asked before any footage', () => {
  assert.equal(nearestDayWithFootage('2026-10-01', DAYS), '2026-10-28');
});

test('nearestDayWithFootage with nothing recorded', () => {
  assert.equal(nearestDayWithFootage('2026-10-30', []), null);
});

test('describeDay reads as a tooltip', () => {
  const cells = monthGrid('2026-10', indexDays(DAYS));
  const full = cells.find((c) => c.date === '2026-10-28');
  assert.match(describeDay(full), /24h 0m recorded, 11\.0 GB/);

  const none = cells.find((c) => c.date === '2026-10-27');
  assert.match(describeDay(none), /no footage/);
});
