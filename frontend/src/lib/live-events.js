/* The live motion feed, over server-sent events.
 *
 * One EventSource for the whole page, shared by every tile, because a browser
 * limits concurrent connections per origin and a 4x4 station wall would
 * otherwise open sixteen. Reconnection is built into EventSource, but it
 * gives up on some errors, so this re-creates it on failure.
 */

import { readable } from 'svelte/store';
import { applyLive, motionByCamera } from './motion.js';

const RETRY_MS = 5000;

/**
 * A store of `Map<camera_id, event>` for the cameras currently reporting
 * motion. The server sends whatever is already open on connect, so a page
 * reload lights up the right tiles without waiting for a transition.
 */
export const motion = readable(new Map(), (set) => {
  let source = null;
  let retry = null;
  let open = [];

  function publish() {
    set(motionByCamera(open));
  }

  function handle(message) {
    if (message.type === 'close') {
      open = open.filter((event) => event.id !== message.event?.id);
    } else {
      open = applyLive(open, message).filter(
        (event) => event.end_ts === null || event.end_ts === undefined,
      );
    }
    publish();
  }

  function connect() {
    try {
      source = new EventSource('/api/events/stream');
    } catch {
      retry = setTimeout(connect, RETRY_MS);
      return;
    }
    source.onmessage = (raw) => {
      try {
        handle(JSON.parse(raw.data));
      } catch {
        /* a malformed line is not worth tearing the stream down for */
      }
    };
    source.onerror = () => {
      source?.close();
      source = null;
      // Keep showing what we last knew rather than clearing every glow.
      retry = setTimeout(connect, RETRY_MS);
    };
  }

  connect();

  return () => {
    clearTimeout(retry);
    source?.close();
  };
});
