<!-- Rewind: the player, the controls and the timeline.

     Motion events fill the `lanes` slot on the timeline and the Events
     panel on the right; the Clip panel cuts an export. Both are laid out here
     already so neither needs the page restructured. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import Timeline from '../lib/Timeline.svelte';
  import Calendar from '../lib/Calendar.svelte';
  import EventLanes from '../lib/EventLanes.svelte';
  import EventsPanel from '../lib/EventsPanel.svelte';
  import { clipRange, currentEvent, nextEvent, prevEvent, seekTarget } from '../lib/motion.js';
  import { api, ApiError } from '../lib/api.js';
  import RangePicker from '../lib/RangePicker.svelte';
  import { dateOf, dayRange, describeRange, isUsable, rangeAroundFootage } from '../lib/range.js';
  import { fragmentsOf, mediaTimeFor, wallClockFor } from '../lib/playclock.js';
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
  // The range the picker chose. Rewind used to be one day at a time; the
  // calendar now decides start and end, including spanning midnight.
  let range = dayRange(localDate(Date.now()));
  let pickerOpen = false;
  let timeline = null;
  // Which days have footage, for the calendar. Loaded per camera and
  // refreshed when the day changes, so a day that starts recording while
  // Rewind is open becomes selectable without a reload.
  let recordedDays = [];
  let serverToday = '';
  let calendarOpen = false;
  // A seek asked for before the playlist loaded. Applied on the first level
  // load, because only then is the program-date-time mapping available.
  let pendingSeek = null;
  let window = { start: 0, end: 0 };
  let playhead = null;
  let error = '';
  let loading = true;

  let video;
  let hls = null;
  let playing = false;
  let speed = 1;
  let goToText = '';
  let selection = null;
  let refreshTimer;

  let events = [];
  let sources = [];
  let hidden = new Set();

  let precise = false;
  let exporting = false;
  let exportError = '';
  let lastExport = null;
  let exportPoll;

  $: bounds = range;
  // The clip selection lives on the timeline now; these mirror it so the
  // export call and the keyboard shortcuts keep working unchanged.
  $: clipIn = selection ? selection.start : null;
  $: clipOut = selection ? selection.end : null;

  async function loadCameras() {
    try {
      const result = await api.listCameras();
      cameras = result.cameras;
      if (!cameraId && cameras.length) cameraId = cameras[0].id;
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    }
  }

  /** Which days this camera has footage on. Never fatal: without it the
      calendar simply offers nothing, and the date can still be deep-linked. */
  async function loadDays() {
    if (!cameraId) return;
    try {
      const result = await api.days(cameraId);
      recordedDays = result.days ?? [];
      serverToday = result.today ?? '';
    } catch {
      recordedDays = [];
    }
  }

  async function loadTimeline() {
    if (!cameraId) return;
    try {
      timeline = await api.timeline(cameraId, range);
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
      const result = await api.events(cameraId, range);
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
    const around = clipRange(detail.event, bounds);
    selection = { start: around.in, end: around.out };
    exportError = '';
  }

  /** Move one end of the selection to the playhead, creating one if needed. */
  function setClipEdge(which) {
    if (playhead === null) return;
    const other = selection
      ? which === 'start'
        ? selection.end
        : selection.start
      : playhead;
    selection = {
      start: Math.min(playhead, other),
      end: Math.max(playhead, other),
    };
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
    const url = api.vodUrl(cameraId, range);

    const { default: Hls } = await import('hls.js');
    if (hls) {
      hls.destroy();
      hls = null;
    }

    if (Hls.isSupported()) {
      hls = new Hls({ lowLatencyMode: false, backBufferLength: 90 });
      hls.loadSource(url);
      hls.attachMedia(video);
      if (seekTo !== null) pendingSeek = seekTo;
      // LEVEL_LOADED, not MANIFEST_PARSED: the fragments and their
      // EXT-X-PROGRAM-DATE-TIME only exist once the level itself has loaded,
      // and seeking before that has nothing to map against.
      hls.on(Hls.Events.LEVEL_LOADED, () => {
        if (pendingSeek === null) return;
        const target = pendingSeek;
        pendingSeek = null;
        seekToMoment(target);
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
    const fragments = fragmentsOf(hls);
    if (fragments.length) {
      video.currentTime = mediaTimeFor(fragments, atMs);
      return;
    }
    // The playlist has not loaded yet. Remember where we were asked for and
    // seek once it does, rather than guessing an offset: media time 0 is the
    // day's first recorded frame, not midnight, so any guess is wrong by the
    // length of every gap before the target.
    pendingSeek = atMs;
  }

  /** Player position back to wall clock, for the playhead. */
  function currentMoment() {
    if (!video) return null;
    // Our own mapping first, hls.js's `playingDate` only as a fallback. The
    // two disagree on a day with gaps: `playingDate` drifted 24 minutes from
    // the requested moment across 15 discontinuities, while walking the
    // fragments' own EXT-X-PROGRAM-DATE-TIME is exact and is the inverse of
    // the mapping used to seek, so the playhead cannot disagree with where
    // the seek landed.
    const fragments = fragmentsOf(hls);
    if (fragments.length) {
      const at = wallClockFor(fragments, video.currentTime);
      if (at !== null) return at;
    }
    if (hls?.playingDate instanceof Date) return hls.playingDate.getTime();
    return null;
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
    // Outside the chosen range, widen to the day the moment falls on rather
    // than refusing: typing a time is a request to go there.
    if (at < range.start || at > range.end) {
      range = dayRange(dateOf(at));
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
    await Promise.all([loadTimeline(), loadDays()]);
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
        setClipEdge('start');
        break;
      case ']':
        setClipEdge('end');
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
    if (at !== null) range = dayRange(dateOf(at));
    window = windowAround(at ?? bounds.start, 60 * MINUTE, bounds);
    await loadDays();
    // With no deep link, open on the most recent day that has footage
    // rather than on today, which may be empty on a fresh install or after
    // an outage.
    if (at === null && recordedDays.length) {
      range = dayRange(recordedDays[recordedDays.length - 1].date);
    }
    await loadTimeline();
    // Then tighten the range to what was actually recorded that day, so the
    // timeline opens full of footage instead of mostly empty hours.
    if (at === null && timeline?.ranges?.length) {
      const tight = rangeAroundFootage(timeline.ranges);
      if (tight) {
        range = tight;
        await loadTimeline();
      }
    }
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
    <div class="field datefield">
      <span>Range</span>
      <button
        class="input datebutton"
        on:click={() => (pickerOpen = !pickerOpen)}
        title="Choose the start and end of what the timeline shows"
      >
        {describeRange(range)}
        <span class="hint"
          >{recordedDays.length
            ? `${recordedDays.length} day${recordedDays.length === 1 ? '' : 's'} recorded`
            : 'no footage'}</span
        >
      </button>
      {#if pickerOpen}
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        <div class="scrim" on:click={() => (pickerOpen = false)}></div>
        <div class="popover">
          <RangePicker
            {range}
            days={recordedDays}
            today={serverToday}
            on:apply={(event) => {
              pickerOpen = false;
              range = event.detail;
              changeDay();
            }}
          />
        </div>
      {/if}
    </div>
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
          rangeStart={bounds.start}
          rangeEnd={bounds.end}
          {timeline}
          {cameraId}
          bind:window
          bind:selection
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
            <button class="chip" on:click={() => setClipEdge('start')}>Set in [</button>
            <button class="chip" on:click={() => setClipEdge('end')}>Set out ]</button>
            <button
              class="chip"
              on:click={() => {
                selection = null;
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

  /* The date control is a button that opens the calendar, not a native date
     input: the point is to offer only the days that have footage, which a
     native picker cannot express. */
  .datefield {
    position: relative;
  }

  .datebutton {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    cursor: pointer;
    text-align: left;
    font: inherit;
    color: var(--text-primary);
  }

  .datebutton:hover {
    border-color: var(--cyan);
  }

  /* Closes the popover on any click outside it, without a document-level
     listener that would also fire on the button that opened it. */
  .scrim {
    position: fixed;
    inset: 0;
    z-index: 20;
  }

  .popover {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    z-index: 21;
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
