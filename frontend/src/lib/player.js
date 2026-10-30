/* Registering go2rtc's player element.
 *
 * `video-rtc.js` is vendored from go2rtc at image build time and is an **ES
 * module** that exports the `VideoRTC` class and registers nothing. It was
 * loaded with a plain `<script src>` tag, which threw
 * "SyntaxError: Unexpected token 'export'" before any of it ran, so
 * `<video-stream>` never existed and **every live tile read "Player not
 * loaded"**. Found 2026-10-30, the first time the UI was opened in a browser.
 *
 * go2rtc's own `video-stream.js` subclasses it and does the registration, but
 * it also injects its own status overlay, which would sit under the tile's.
 * So the class is registered here instead: one dynamic import, one define,
 * and the tile keeps sole ownership of what it draws on top of the video.
 */

const SOURCE = '/video-rtc.js';

export const playerReady = (async () => {
  if (typeof customElements === 'undefined') return false;
  if (customElements.get('video-stream')) return true;
  try {
    // Dynamic, and hidden from the bundler: the file is served from the
    // static root at runtime and is deliberately not part of the bundle.
    const module = await import(/* @vite-ignore */ SOURCE);
    const Player = module.VideoRTC ?? module.default;
    if (typeof Player !== 'function') {
      console.warn(`${SOURCE} loaded but exported no player class`);
      return false;
    }
    if (!customElements.get('video-stream')) {
      customElements.define('video-stream', Player);
    }
    return true;
  } catch (error) {
    // Live video is the only casualty: recording, playback and the rest of
    // the UI do not depend on this file.
    console.warn(`live player unavailable (${SOURCE}):`, error);
    return false;
  }
})();
