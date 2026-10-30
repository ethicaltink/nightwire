<!-- Rewind: the player, the controls and the timeline.

     Motion events fill the `lanes` slot on the timeline and the Events
     panel on the right; the Clip panel cuts an export. Both are laid out here
     already so neither needs the page restructured. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import Timeline from '../lib/Timeline.svelte';
  import EventLanes from '../lib/EventLanes.svelte';
  import EventsPanel from '../lib/EventsPanel.svelte';
  import { clipRange, currentEvent, nextEvent, prevEvent, seekTarget } from '../lib/motion.js';
  import { api, ApiError } from '../lib/api.js';
  import { link, location, navigate } from '../lib/router.js';
  import {
    MINUTE,
    SECOND,
    dayBounds,
    formatClock,
    formatDuration,
    formatMoment,
    localDate,
    parseMoment,
    windowAround,
  } from '../lib/timescale.js';

  const SPEEDS = [0.5, 1, 2, 4, 8, 16];
  const TIMELINE_REFRESH_MS = 30000;

  let cameras = [];
  let cameraId = '';
  let date = localDate(Date.now());
  let timeline = null;
  let window = { start: 0, end: 0 };
  let playhead = null;
  let error = '';
  let loading = true;

  let video;
  let hls = null;
  let playing = false;
  let speed = 1;
  let goToText = '';
  let clipIn = null;
  let clipOut = null;
  let refreshTimer;

  let events = [];
  let sources = [];
  let hidden = new Set();

  let precise = false;
  let exporting = false;
  let exportError = '';
  let lastExport = null;
  let exportPoll;

  $: bounds = dayBounds(date);

  async function loadCameras() {
    try {
      const result = await api.listCameras();
      cameras = result.cameras;
      if (!cameraId && cameras.length) cameraId = cameras[0].id;
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    }
  }

  async function loadTimeline() {
    if (!cameraId) return;
    try {
      timeline = await api.timeline(cameraId, date);
      error = '';
    } catch (err) {
      timeline = null;
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      loading = false;
    }
    await loadEvents();
  }

  async function loadEvents() {
    if (!cameraId) return;
    try {
      const result = await api.events(cameraId, date);
      events = result.events;
      sources = result.sources.filter((source) => source.enabled || hasEvents(result.events, source));
    } catch {
      events = [];
      sources = [];
    }
  }

  function hasEvents(list, source) {
    return list.some((event) => event.source_id === source.id);
  }

  function toggleSource({ detail }) {
    const next = new Set(hidden);
    if (next.has(detail.sourceId)) {
      next.delete(detail.sourceId);
    } else {
      next.add(detail.sourceId);
    }
    hidden = next;
  }

  /** Clicking an event plays from 5 s before it starts. */
  function pickEvent({ detail }) {
    const at = seekTarget(detail.event);
    playhead = at;
    window = windowAround(at, window.end - window.start, bounds);
    seekToMoment(at);
    if (video?.paused) video.play();
  }

  function stepEvent(direction) {
    const from = playhead ?? bounds.start;
    const found =
      direction > 0 ? nextEvent(events, from, hidden) : prevEvent(events, from, hidden);
    if (found) pickEvent({ detail: { event: found } });
  }

  function clipEvent({ detail }) {
    const range = clipRange(detail.event, bounds);
    clipIn = range.in;
    clipOut = range.out;
    exportError = '';
  }

  async function startExport() {
    if (!canExport) return;
    exporting = true;
    exportError = '';
    try {
      const result = await api.createExport({
        camera_id: cameraId,
        start_ts: Math.round(Math.min(clipIn, clipOut)),
        end_ts: Math.round(Math.max(clipIn, clipOut)),
        precise,
      });
      lastExport = result.export;
      pollExport(lastExport.id);
    } catch (err) {
      exportError = err instanceof ApiError ? err.message : String(err);
    } finally {
      exporting = false;
    }
  }

  function pollExport(id) {
    clearInterval(exportPoll);
    exportPoll = setInterval(async () => {
      try {
        const result = await api.getExport(id);
        lastExport = result.export;
        if (lastExport.status === 'done' || lastExport.status === 'failed') {
          clearInterval(exportPoll);
        }
      } catch {
        clearInterval(exportPoll);
      }
    }, 2000);
  }

  /** Attach hls.js to the day's playlist and seek to a moment. */
  async function openPlaylist(seekTo = null) {
    if (!cameraId || !video) return;
    const url = api.vodUrl(cameraId, date);

    const { default: Hls } = await import('hls.js');
    if (hls) {
      hls.destroy();
      hls = null;
    }

    if (Hls.isSupported()) {
      hls = new Hls({ lowLatencyMode: false, backBufferLength: 90 });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (seekTo !== null) seekToMoment(seekTo);
      });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data?.fatal) return;
        // An empty playlist is the ordinary "nothing was recorded on this
        // day" case, and hls.js reports it as `levelEmptyError`. Showing
        // that string told nobody anything; it is the first thing on the
        // page on a fresh install.
        error =
          data.details === 'levelEmptyError' || data.details === 'manifestParsingError'
            ? 'No footage for this camera on this day.'
            : `Playback error: ${data.details ?? 'unknown'}`;
      });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari plays HLS natively.
      video.src = url;
      if (seekTo !== null) video.addEventListener('loadedmetadata', () => seekToMoment(seekTo), {
        once: true,
      });
    } else {
      error = 'This browser cannot play HLS.';
    }
  }

  /**
   * Wall clock to player position.
   *
   * hls.js exposes the playlist's EXT-X-PROGRAM-DATE-TIME mapping, which is
   * the only correct way to turn a moment into a seek position across gaps:
   * media time is not wall-clock time once a discontinuity exists.
   */
  function seekToMoment(atMs) {
    if (!video) return;
    if (hls?.playingDate !== undefined && typeof hls.setPlayingDate === 'function') {
      try {
        hls.setPlayingDate(new Date(atMs));
        return;
      } catch {
        /* fall through to the offset estimate below */
      }
    }
    // Fallback: offset from the start of the day's playlist.
    const seconds = (atMs - bounds.start) / 1000;
    if (Number.isFinite(seconds)) video.currentTime = Math.max(0, seconds);
  }

  /** Player position back to wall clock, for the playhead. */
  function currentMoment() {
    if (!video) return null;
    if (hls?.playingDate instanceof Date) return hls.playingDate.getTime();
    return bounds.start + video.currentTime * 1000;
  }

  function onTimeUpdate() {
    playhead = currentMoment();
  }

  function togglePlay() {
    if (!video) return;
    if (video.paused) {
      video.play();
    } else {
      video.pause();
    }
  }

  function setSpeed(next) {
    speed = next;
    if (video) video.playbackRate = next;
  }

  function skip(deltaMs) {
    if (!video) return;
    video.currentTime = Math.max(0, video.currentTime + deltaMs / 1000);
  }

  function onSeek(event) {
    const at = event.detail.at;
    playhead = at;
    seekToMoment(at);
  }

  function goToTime() {
    const at = parseMoment(goToText.trim()) ?? parseMoment(`${date}T${goToText.trim()}`);
    if (at === null) {
      error = `Could not read "${goToText}". Try 14:32:05 or 2026-10-27T14:32:05.`;
      return;
    }
    error = '';
    const nextDate = localDate(at);
    if (nextDate !== date) {
      date = nextDate;
      changeDay(at);
      return;
    }
    window = windowAround(at, window.end - window.start, bounds);
    onSeek({ detail: { at } });
  }

  function jumpToLive() {
    navigate(`/live`);
  }

  function snapshot() {
    if (!video) return;
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 1280;
    canvas.height = video.videoHeight || 720;
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const link = document.createElement('a');
    link.download = `${cameraId}_${formatMoment(playhead ?? Date.now()).replace(/[:T]/g, '')}.jpg`;
    link.href = canvas.toDataURL('image/jpeg', 0.92);
    link.click();
  }

  async function downloadSegment() {
    if (playhead === null) return;
    try {
      const info = await api.segmentAt(cameraId, playhead);
      const link = document.createElement('a');
      link.href = info.url;
      link.download = info.url.split('/').pop();
      link.click();
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    }
  }

  function sameMomentOn(otherId) {
    const at = playhead ?? bounds.start;
    navigate(`/playback/${otherId}?t=${formatMoment(at)}`);
  }

  async function changeDay(seekTo = null) {
    await loadTimeline();
    const target = seekTo ?? bounds.start;
    window = windowAround(target, window.end - window.start || 60 * MINUTE, bounds);
    await openPlaylist(target);
  }

  function onKey(event) {
    if (event.target instanceof HTMLInputElement) return;
    switch (event.key) {
      case ' ':
        event.preventDefault();
        togglePlay();
        break;
      case 'ArrowLeft':
        event.preventDefault();
        skip(event.shiftKey ? -MINUTE : -10 * SECOND);
        break;
      case 'ArrowRight':
        event.preventDefault();
        skip(event.shiftKey ? MINUTE : 10 * SECOND);
        break;
      case '[':
        clipIn = playhead;
        break;
      case ']':
        clipOut = playhead;
        break;
      case 'n':
      case 'N':
        event.preventDefault();
        stepEvent(event.shiftKey ? -1 : 1);
        break;
      default:
        break;
    }
  }

  /** The end of the last recorded run, or now, or midday: what to look at. */
  function newestMoment() {
    const recorded = timeline?.ranges ?? [];
    if (recorded.length) return recorded[recorded.length - 1].end_ts;
    const now = Date.now();
    if (now >= bounds.start && now <= bounds.end) return now;
    return (bounds.start + bounds.end) / 2;
  }

  onMount(async () => {
    await loadCameras();
    // Deep link: /playback/front?t=2026-10-27T14:32:05
    const requested = $location.param;
    if (requested && cameras.some((c) => c.id === requested)) cameraId = requested;
    const at = parseMoment(new URLSearchParams(globalThis.location.search).get('t'));
    if (at !== null) date = localDate(at);
    window = windowAround(at ?? bounds.start, 60 * MINUTE, bounds);
    await loadTimeline();
    // Without a deep link the detail view used to open on midnight, so a day
    // with footage showed an empty hour and you had to hunt for it. Open on
    // the newest footage instead, which is what anyone opening Rewind is
    // looking for.
    if (at === null) window = windowAround(newestMoment(), 60 * MINUTE, bounds);
    await openPlaylist(at);
    if (at !== null) playhead = at;

    refreshTimer = setInterval(loadTimeline, TIMELINE_REFRESH_MS);
    globalThis.addEventListener('keydown', onKey);
  });

  onDestroy(() => {
    clearInterval(refreshTimer);
    clearInterval(exportPoll);
    globalThis.removeEventListener('keydown', onKey);
    if (hls) hls.destroy();
  });

  $: others = cameras.filter((camera) => camera.id !== cameraId);
  $: current = playhead === null ? null : currentEvent(events, playhead, bounds);
  $: clipLength =
    clipIn !== null && clipOut !== null ? Math.abs(clipOut - clipIn) : null;
  $: canExport = Boolean(cameraId) && clipLength !== null && clipLength >= 1000;
</script>

<div class="page">
  <header>
    <h1>Rewind</h1>
    <label class="field">
      Camera
      <select
        class="input"
        bind:value={cameraId}
        on:change={() => changeDay(playhead)}
      >
        {#each cameras as camera}
          <option value={camera.id}>{camera.name}</option>
        {/each}
      </select>
    </label>
    <label class="field">
      Date
      <input class="input" type="date" bind:value={date} on:change={() => changeDay()} />
    </label>
    <span class="hint playhead mono">
      {playhead === null ? '--:--:--' : formatClock(playhead)}
    </span>
  </header>

  {#if error}
    <p class="note" role="alert">{error}</p>
  {/if}

  {#if !loading && cameras.length === 0}
    <p class="hint empty">No cameras yet. Add them on <a href="/cameras">Nodes</a>.</p>
  {:else}
    <div class="main">
      <div class="left">
        <div class="player">
          <!-- svelte-ignore a11y-media-has-caption -->
          <video
            bind:this={video}
            on:timeupdate={onTimeUpdate}
            on:play={() => (playing = true)}
            on:pause={() => (playing = false)}
            playsinline
          ></video>
        </div>

        <div class="controls">
          <button class="button" on:click={togglePlay}>{playing ? 'Pause' : 'Play'}</button>
          <button class="chip" on:click={() => skip(-MINUTE)} title="Back 1 minute">-1m</button>
          <button class="chip" on:click={() => skip(-10 * SECOND)} title="Back 10 seconds">-10s</button>
          <button class="chip" on:click={() => skip(10 * SECOND)} title="Forward 10 seconds">+10s</button>
          <button class="chip" on:click={() => skip(MINUTE)} title="Forward 1 minute">+1m</button>

          <span class="speeds">
            {#each SPEEDS as option}
              <button class="chip" class:on={speed === option} on:click={() => setSpeed(option)}>
                {option}x
              </button>
            {/each}
          </span>

          <form class="goto" on:submit|preventDefault={goToTime}>
            <input class="input" placeholder="14:32:05" bind:value={goToText} aria-label="Go to time" />
            <button class="chip" type="submit">Go</button>
          </form>

          <button class="chip" on:click={jumpToLive}>Jump to live</button>
          <button class="chip" on:click={snapshot}>Snapshot</button>
          <button class="chip" on:click={downloadSegment}>Download segment</button>
        </div>

        <Timeline
          dayStart={bounds.start}
          dayEnd={bounds.end}
          {timeline}
          {cameraId}
          bind:window
          {playhead}
          on:seek={onSeek}
        >
          <svelte:fragment slot="lanes">
            <EventLanes
              {window}
              {events}
              {sources}
              {hidden}
              currentId={current?.id ?? null}
              on:pick={pickEvent}
            />
          </svelte:fragment>
        </Timeline>

        <p class="hint keys">
          Space play/pause &middot; &larr;/&rarr; 10 s &middot; Shift+&larr;/&rarr; 1 min &middot;
          N next event, Shift+N previous &middot; [ and ] set clip in and out
        </p>
      </div>

      <aside class="right">
        <section class="panel">
          <h2>Same moment on</h2>
          {#if others.length === 0}
            <p class="hint">No other cameras.</p>
          {:else}
            <div class="row">
              {#each others as other}
                <button class="chip" on:click={() => sameMomentOn(other.id)}>{other.name}</button>
              {/each}
            </div>
          {/if}
        </section>

        <section class="panel">
          <h2>Clip</h2>
          <p class="hint mono">
            In {clipIn === null ? '--:--:--' : formatClock(clipIn)} &middot; Out
            {clipOut === null ? '--:--:--' : formatClock(clipOut)}
            {#if clipLength !== null}
              &middot; {formatDuration(clipLength)}
            {/if}
          </p>

          <div class="row">
            <button class="chip" on:click={() => (clipIn = playhead)}>Set in [</button>
            <button class="chip" on:click={() => (clipOut = playhead)}>Set out ]</button>
            <button
              class="chip"
              on:click={() => {
                clipIn = null;
                clipOut = null;
                exportError = '';
              }}
            >
              Clear
            </button>
          </div>

          <label class="precise">
            <input type="checkbox" bind:checked={precise} />
            Precise cut
          </label>
          <p class="hint small">
            A stream copy cuts on keyframes, so a clip can start up to a second early. A precise
            cut re-encodes to land exactly, which takes longer.
          </p>

          {#if exportError}
            <p class="note" role="alert">{exportError}</p>
          {/if}
          {#if lastExport}
            <p class="hint">
              {#if lastExport.status === 'done'}
                <a href={lastExport.download_url} download>Download {lastExport.name}</a>
              {:else if lastExport.status === 'failed'}
                Export failed: {lastExport.error}
              {:else}
                Exporting {lastExport.name}... ({lastExport.status})
              {/if}
              &middot; <a href="/exports" use:link>Vault</a>
            </p>
          {/if}

          <button class="button" on:click={startExport} disabled={!canExport || exporting}>
            {exporting ? 'Exporting...' : 'Export clip'}
          </button>
        </section>

        <EventsPanel
          {events}
          {sources}
          {hidden}
          currentId={current?.id ?? null}
          on:toggle={toggleSource}
          on:pick={pickEvent}
          on:prev={() => stepEvent(-1)}
          on:next={() => stepEvent(1)}
          on:clip={clipEvent}
        />
      </aside>
    </div>
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px 16px;
  }

  h1 {
    font-size: 20px;
    margin-right: auto;
  }

  h2 {
    font-size: 13px;
    letter-spacing: 1px;
    color: var(--text-secondary);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .playhead {
    font-size: 15px;
    color: var(--cyan);
  }

  .main {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 18rem;
    gap: 14px;
    align-items: start;
  }

  .left {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }

  .player {
    background: var(--black);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    aspect-ratio: 16 / 9;
  }

  video {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: var(--black);
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
  }

  .speeds,
  .goto,
  .row {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px;
    align-items: center;
  }

  .goto .input {
    width: 7.5rem;
    height: 32px;
    font-size: 13px;
  }

  .chip {
    min-height: 32px;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--panel);
    color: var(--text-secondary);
    font: 500 12px var(--font-body);
    cursor: pointer;
  }
  .chip:hover {
    color: var(--text);
  }
  .chip.on {
    background: var(--border);
    color: var(--text);
    box-shadow: inset 0 -2px 0 var(--cyan);
  }

  .right {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .panel {
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .precise {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .small {
    font-size: 12px;
  }

  .keys {
    font-size: 12px;
  }

  .note {
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--magenta);
    border-radius: var(--radius);
    background: var(--panel);
    font-size: 13px;
  }

  .empty {
    padding: 16px;
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-lg);
  }

  @media (max-width: 1000px) {
    .main {
      grid-template-columns: 1fr;
    }
  }
</style>
