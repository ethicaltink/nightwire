/* Station layout arithmetic, kept pure so it is obvious and reviewable.
 *
 * layouts 1, 2x2, 3x3, 4x4 and 1+5
 * (one large, five small), optional cycling through pages of cameras, and
 * with three cameras the 2x2 default puts a clock and recorder-status cell in
 * the fourth slot.
 */

export const LAYOUTS = {
  '1': { cells: 1, columns: 1, rows: 1, feature: false },
  '2x2': { cells: 4, columns: 2, rows: 2, feature: false },
  '3x3': { cells: 9, columns: 3, rows: 3, feature: false },
  '4x4': { cells: 16, columns: 4, rows: 4, feature: false },
  '1+5': { cells: 6, columns: 3, rows: 3, feature: true },
};

export const DEFAULT_LAYOUT = '2x2';

export function layoutOf(name) {
  return LAYOUTS[name] ?? LAYOUTS[DEFAULT_LAYOUT];
}

export function layoutNames() {
  return Object.keys(LAYOUTS);
}

/** Cameras per page for this layout, leaving room for the status cell. */
export function capacity(name, { reserveStatusCell = false } = {}) {
  const layout = layoutOf(name);
  if (!reserveStatusCell) return layout.cells;
  return Math.max(1, layout.cells - 1);
}

export function pageCount(cameraCount, name, options) {
  const perPage = capacity(name, options);
  if (cameraCount <= 0) return 1;
  return Math.max(1, Math.ceil(cameraCount / perPage));
}

/** The cameras shown on one page, wrapping round if the index overruns. */
export function page(cameras, name, pageIndex = 0, options) {
  const perPage = capacity(name, options);
  const total = pageCount(cameras.length, name, options);
  const index = ((pageIndex % total) + total) % total;
  return cameras.slice(index * perPage, index * perPage + perPage);
}

/**
 * Whether the status cell gets the spare slot.
 *
 * Only when the cameras leave exactly the right amount of room: with three
 * cameras in a 2x2 it fills the fourth cell, which is what the mockup shows.
 * With four cameras in a 2x2 there is no spare cell and the clock would push
 * a camera off the wall, so it is left out.
 */
export function showsStatusCell(cameraCount, name) {
  const layout = layoutOf(name);
  return cameraCount > 0 && cameraCount < layout.cells;
}

/** `?cams=front,drive,yard` filters and orders the wall. */
export function selectCameras(cameras, spec) {
  if (!spec) return cameras;
  const wanted = spec
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean);
  if (wanted.length === 0) return cameras;
  const byId = new Map(cameras.map((camera) => [camera.camera_id, camera]));
  return wanted.map((id) => byId.get(id)).filter(Boolean);
}

/** Parse the station's URL options. Unknown values fall back, never throw. */
export function parseStationOptions(search) {
  const params = new URLSearchParams(search || '');
  const layout = params.get('layout');
  const cycleRaw = Number(params.get('cycle'));
  const reloadRaw = Number(params.get('reload'));
  return {
    layout: layout && LAYOUTS[layout] ? layout : DEFAULT_LAYOUT,
    cams: params.get('cams') || '',
    cycleSeconds: Number.isFinite(cycleRaw) && cycleRaw > 0 ? Math.max(5, cycleRaw) : 0,
    reloadHour:
      Number.isFinite(reloadRaw) && reloadRaw >= 0 && reloadRaw <= 23 ? Math.floor(reloadRaw) : 4,
  };
}

/** Milliseconds until the next daily reload, for the memory-safety reload. */
export function msUntilHour(hour, now = new Date()) {
  const next = new Date(now);
  next.setHours(hour, 0, 0, 0);
  if (next <= now) next.setDate(next.getDate() + 1);
  return next.getTime() - now.getTime();
}
