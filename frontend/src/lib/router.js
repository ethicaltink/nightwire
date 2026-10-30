/* A minimal history-API router. Seven pages do not need a dependency.
 *
 * FastAPI serves the SPA shell for every non-API path, so a deep link such
 * as /playback/front?t=... lands here with the path intact.
 */
import { readable } from 'svelte/store';

export const routes = [
  { name: 'login', pattern: /^\/login\/?$/, chrome: false },
  { name: 'station', pattern: /^\/station\/?$/, chrome: false },
  { name: 'feeds', pattern: /^\/live\/?$/, chrome: true },
  { name: 'rewind', pattern: /^\/playback(?:\/([^/]+))?\/?$/, chrome: true },
  { name: 'vault', pattern: /^\/exports\/?$/, chrome: true },
  { name: 'nodes', pattern: /^\/cameras\/?$/, chrome: true },
  { name: 'core', pattern: /^\/system\/?$/, chrome: true },
];

export const HOME = '/live';

export function match(pathname) {
  if (pathname === '/' || pathname === '') {
    return { ...routes.find((r) => r.name === 'feeds'), param: null, path: HOME };
  }
  for (const route of routes) {
    const found = pathname.match(route.pattern);
    if (found) return { ...route, param: found[1] ?? null, path: pathname };
  }
  return { name: 'notfound', chrome: true, param: null, path: pathname };
}

export const location = readable(match(window.location.pathname), (set) => {
  const update = () => set(match(window.location.pathname));
  window.addEventListener('popstate', update);
  window.addEventListener('nightwire:navigate', update);
  return () => {
    window.removeEventListener('popstate', update);
    window.removeEventListener('nightwire:navigate', update);
  };
});

export function navigate(path, { replace = false } = {}) {
  if (path === window.location.pathname) return;
  window.history[replace ? 'replaceState' : 'pushState']({}, '', path);
  window.dispatchEvent(new Event('nightwire:navigate'));
}

/** Use on <a> so internal links route without a full page load. */
export function link(node) {
  const onClick = (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    const href = node.getAttribute('href');
    if (!href || !href.startsWith('/')) return;
    event.preventDefault();
    navigate(href);
  };
  node.addEventListener('click', onClick);
  return { destroy: () => node.removeEventListener('click', onClick) };
}
