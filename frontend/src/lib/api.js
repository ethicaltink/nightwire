/* Fetch helpers. Every call carries the session cookie; a 401 means the
 * session is gone, which the shell turns into a trip back to sign-in. */

export class ApiError extends Error {
  constructor(message, status, retryAfter) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new ApiError(
      body?.error ?? `Request failed (${response.status})`,
      response.status,
      Number(response.headers.get('Retry-After')) || 0,
    );
  }
  return body;
}

/**
 * The query for a time window.
 *
 * Takes either a `YYYY-MM-DD` day or a `{start, end}` in unix ms. The
 * routes accept both; Rewind moved from days to ranges, and deep links and
 * other callers still pass a day.
 */
function windowQuery(when) {
  if (when && typeof when === 'object' && Number.isFinite(when.start)) {
    return `start=${Math.round(when.start)}&end=${Math.round(when.end)}`;
  }
  return `date=${encodeURIComponent(when)}`;
}

const post = (path, body) =>
  request(path, { method: 'POST', body: JSON.stringify(body ?? {}) });

export const api = {
  health: () => request('/api/health'),
  session: () => request('/api/auth/session'),
  login: (password) => post('/api/auth/login', { password }),
  logout: () => post('/api/auth/logout'),

  // cameras
  listCameras: () => request('/api/cameras'),
  discover: (timeout) => post('/api/discover', timeout ? { timeout } : {}),
  probe: (body) => post('/api/cameras/probe', body),
  addCamera: (body) => post('/api/cameras', body),
  patchCamera: (id, body) =>
    request(`/api/cameras/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteCamera: (id, deleteRecordings = false) =>
    request(`/api/cameras/${id}?delete_recordings=${deleteRecordings}`, { method: 'DELETE' }),
  testCamera: (id) => post(`/api/cameras/${id}/test`),
  cameraStatus: (id) => request(`/api/cameras/${id}/status`),

  // system
  system: () => request('/api/system'),
  settings: () => request('/api/settings'),
  patchSettings: (body) =>
    request('/api/settings', { method: 'PATCH', body: JSON.stringify(body) }),

  // exports
  listExports: () => request('/api/exports'),
  createExport: (body) => post('/api/exports', body),
  getExport: (id) => request(`/api/exports/${id}`),
  deleteExport: (id) => request(`/api/exports/${id}`, { method: 'DELETE' }),

  // motion events
  eventTopics: (id) => request(`/api/cameras/${id}/event-topics`),
  eventSources: (id) => request(`/api/cameras/${id}/event-sources`),
  saveEventSources: (id, sources) =>
    request(`/api/cameras/${id}/event-sources`, {
      method: 'PUT',
      body: JSON.stringify({ sources }),
    }),
  events: (id, when, sourceIds) => {
    const params = new URLSearchParams(windowQuery(when));
    if (sourceIds?.length) params.set('sources', sourceIds.join(','));
    return request(`/api/cameras/${id}/events?${params}`);
  },
  recentEvents: (limit = 10) => request(`/api/events/recent?limit=${limit}`),
  openEvents: () => request('/api/events/open'),
  eventSnapshotUrl: (eventId) => `/api/events/${eventId}/snapshot.jpg`,

  // playback
  timeline: (id, when) => request(`/api/cameras/${id}/timeline?${windowQuery(when)}`),
  days: (id) => request(`/api/cameras/${id}/days`),
  segmentAt: (id, at) => request(`/api/cameras/${id}/segment?t=${at}`),
  vodUrl: (id, when) => `/api/cameras/${id}/vod.m3u8?${windowQuery(when)}`,
  thumbUrl: (id, at) => `/api/cameras/${id}/thumb?t=${at}`,

  // live video
  liveStreams: () => request('/api/live/streams'),
  syncStreams: () => post('/api/live/sync'),

  /** Snapshots are an <img src>, not a fetch. The query string busts the cache. */
  snapshotUrl: (id) => `/api/cameras/${id}/snapshot.jpg?t=${Date.now()}`,
};
