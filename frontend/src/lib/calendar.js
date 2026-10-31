/* Month-grid arithmetic for the Rewind calendar. Pure, so it is tested with
 * `node --test` and holds no DOM.
 *
 * Dates are `YYYY-MM-DD` strings in the display time zone throughout, never
 * Date objects in UTC: the server groups footage into local days, and a day
 * the user clicks has to be the same day the server means.
 */

/** `YYYY-MM-DD` for a Date, read in local time rather than UTC. */
export function isoDate(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** `YYYY-MM` for a `YYYY-MM-DD`. */
export function monthOf(date) {
  return date.slice(0, 7);
}

/** Step a `YYYY-MM` by whole months. */
export function shiftMonth(month, delta) {
  const [year, m] = month.split('-').map(Number);
  const moved = new Date(year, m - 1 + delta, 1);
  return monthOf(isoDate(moved));
}

/** Step a `YYYY-MM-DD` by whole days. */
export function shiftDay(date, delta) {
  const [y, m, d] = date.split('-').map(Number);
  return isoDate(new Date(y, m - 1, d + delta));
}

/**
 * The cells of a month grid, six rows of seven, Sunday first.
 *
 * Leading and trailing cells come from the neighbouring months and are
 * marked `outside`, so the grid never changes height as you page through
 * months. Each cell carries whatever the server said about that day.
 */
export function monthGrid(month, daysByDate = {}, { today = null, selected = null } = {}) {
  const [year, m] = month.split('-').map(Number);
  const first = new Date(year, m - 1, 1);
  const start = new Date(year, m - 1, 1 - first.getDay());

  const cells = [];
  for (let i = 0; i < 42; i += 1) {
    const at = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const date = isoDate(at);
    const info = daysByDate[date] ?? null;
    cells.push({
      date,
      day: at.getDate(),
      outside: monthOf(date) !== month,
      hasFootage: Boolean(info && info.segments > 0),
      seconds: info ? info.seconds : 0,
      segments: info ? info.segments : 0,
      bytes: info ? info.bytes : 0,
      isToday: date === today,
      isSelected: date === selected,
    });
  }
  return cells;
}

/** `[{date, ...}]` from the API into a lookup by date. */
export function indexDays(days = []) {
  const out = {};
  for (const day of days) out[day.date] = day;
  return out;
}

/**
 * How full a day is, 0 to 1, for shading a cell.
 *
 * Against 24 hours rather than against the fullest day, so a day with an
 * outage reads as emptier than a complete one instead of being rescaled to
 * look full. Clamped, because a day with a DST repeat can exceed 24 hours.
 */
export function dayFullness(seconds) {
  if (!seconds) return 0;
  return Math.max(0, Math.min(1, seconds / 86400));
}

/** The nearest day with footage at or before `date`, else the nearest after. */
export function nearestDayWithFootage(date, days = []) {
  const present = days.filter((d) => d.segments > 0).map((d) => d.date).sort();
  if (!present.length) return null;
  let best = null;
  for (const candidate of present) {
    if (candidate <= date) best = candidate;
  }
  return best ?? present[0];
}

/** Short human text for a cell's tooltip. */
export function describeDay(cell) {
  if (!cell.hasFootage) return `${cell.date} - no footage`;
  const hours = Math.floor(cell.seconds / 3600);
  const minutes = Math.round((cell.seconds % 3600) / 60);
  const length = hours ? `${hours}h ${minutes}m` : `${minutes}m`;
  const gb = cell.bytes / 1000 ** 3;
  const size = gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(cell.bytes / 1000 ** 2)} MB`;
  return `${cell.date} - ${length} recorded, ${size}`;
}
