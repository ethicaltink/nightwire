/* Mapping between wall-clock time and player position.
 *
 * **Media time is not wall-clock time.** A day's playlist contains only the
 * segments that exist, so media time 0 is the first recorded frame, not
 * local midnight, and every gap in the day shifts the two further apart.
 * Seeking by `(atMs - midnight) / 1000` is therefore wrong by the length of
 * every gap before the target - on a day whose recording starts at 14:02
 * it is wrong by fourteen hours, which clamps to 0 and plays from the
 * beginning of the footage no matter where you clicked.
 *
 * The playlist's EXT-X-PROGRAM-DATE-TIME is the authority. hls.js exposes it
 * per fragment, and these functions do the arithmetic over that list. Pure,
 * so the gap cases are tested without a player.
 *
 * A `fragment` here is anything with `{programDateTime, start, duration}`,
 * which is the shape hls.js uses: `programDateTime` in epoch ms, `start` the
 * media-time offset in seconds, `duration` in seconds.
 */

const usable = (fragments) =>
  (fragments ?? []).filter(
    (f) => f && Number.isFinite(f.programDateTime) && Number.isFinite(f.start),
  );

/**
 * Player position, in seconds, for a wall-clock moment.
 *
 * A moment inside a gap has no frame of its own, so it resolves to the start
 * of the next fragment: clicking an empty stretch of timeline jumps forward
 * to where recording resumes, rather than silently doing nothing. Before the
 * first fragment gives 0, after the last gives the end.
 */
export function mediaTimeFor(fragments, atMs) {
  const frags = usable(fragments);
  if (!frags.length || !Number.isFinite(atMs)) return 0;

  for (const fragment of frags) {
    const begins = fragment.programDateTime;
    const ends = begins + (fragment.duration ?? 0) * 1000;
    if (atMs < begins) return fragment.start;
    if (atMs < ends) return fragment.start + (atMs - begins) / 1000;
  }
  const last = frags[frags.length - 1];
  return last.start + (last.duration ?? 0);
}

/** Wall-clock milliseconds for a player position, the inverse of the above. */
export function wallClockFor(fragments, seconds) {
  const frags = usable(fragments);
  if (!frags.length || !Number.isFinite(seconds)) return null;

  for (const fragment of frags) {
    const begins = fragment.start;
    const ends = begins + (fragment.duration ?? 0);
    if (seconds < begins) return fragment.programDateTime;
    if (seconds < ends) return fragment.programDateTime + (seconds - begins) * 1000;
  }
  const last = frags[frags.length - 1];
  return last.programDateTime + (last.duration ?? 0) * 1000;
}

/** Whether a moment has a frame, as opposed to falling in a gap. */
export function hasFrameAt(fragments, atMs) {
  return usable(fragments).some(
    (f) => atMs >= f.programDateTime && atMs < f.programDateTime + (f.duration ?? 0) * 1000,
  );
}

/** The fragments hls.js currently holds, whichever level is loaded. */
export function fragmentsOf(hls) {
  if (!hls) return [];
  const levels = hls.levels ?? [];
  const index = hls.currentLevel >= 0 && levels[hls.currentLevel] ? hls.currentLevel : 0;
  return levels[index]?.details?.fragments ?? [];
}
