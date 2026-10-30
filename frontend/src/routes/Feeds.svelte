<!-- Feeds: the live grid.
     1 + 2 by default (one large tile, two beside it), substreams in the
     tiles, the main stream at full size, a magenta glow while a camera
     reports motion, and the recent-events strip linking into Rewind. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import LiveTile from '../lib/LiveTile.svelte';
  import { api, ApiError } from '../lib/api.js';
  import { motion } from '../lib/live-events.js';
  import { navigate, link } from '../lib/router.js';
  import { formatClock, formatMoment } from '../lib/timescale.js';

  const REFRESH_MS = 15000;

  let cameras = [];
  let go2rtc = null;
  let error = '';
  let loading = true;
  let expanded = null; // camera_id shown full size
  let health = null;
  let timer;
  let recent = [];

  async function load() {
    try {
      const result = await api.liveStreams();
      cameras = result.cameras;
      go2rtc = result.go2rtc;
      error = '';
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function pollHealth() {
    try {
      health = await api.health();
    } catch {
      health = null;
    }
    try {
      const result = await api.recentEvents(6);
      recent = result.events;
    } catch {
      recent = [];
    }
  }

  /** The strip links into Rewind at the moment the event started. */
  function eventLink(event) {
    return `/playback/${event.camera_id}?t=${formatMoment(event.start_ts)}`;
  }

  onMount(() => {
    load();
    pollHealth();
    timer = setInterval(pollHealth, REFRESH_MS);
  });
  onDestroy(() => clearInterval(timer));

  function unhealthy(cameraId) {
    const state = health?.components?.recorders?.state;
    if (!state || state === 'idle' || state === 'not-started') return false;
    // The health detail names the cameras needing attention.
    return (health.components.recorders.detail || '').includes(cameraId);
  }

  /** Open the timeline 30 seconds back. */
  function jumpToPlayback(cameraId) {
    const at = new Date(Date.now() - 30000);
    const stamp = at.toISOString().slice(0, 19);
    navigate(`/playback/${cameraId}?t=${stamp}`);
  }

  $: expandedCamera = cameras.find((c) => c.camera_id === expanded) ?? null;
</script>

<div class="page">
  {#if error}
    <p class="note" role="alert">{error}</p>
  {/if}

  {#if go2rtc && go2rtc.state !== 'running'}
    <p class="note warn">
      Live video is {go2rtc.state}: {go2rtc.detail}
      {#if go2rtc.state === 'missing'}
        Recording is unaffected.
      {/if}
    </p>
  {/if}

  {#if expandedCamera}
    <div class="expanded">
      <div class="full">
        <LiveTile
          name={expandedCamera.name}
          stream={expandedCamera.main_stream}
          unhealthy={unhealthy(expandedCamera.camera_id)}
          motion={$motion.has(expandedCamera.camera_id)}
          motionZone={$motion.get(expandedCamera.camera_id)?.label ?? ''}
          showClock={true}
        />
      </div>
      <div class="actions">
        <button class="button-quiet" on:click={() => (expanded = null)}>Back to grid</button>
        <button class="button" on:click={() => jumpToPlayback(expandedCamera.camera_id)}>
          Jump to playback
        </button>
        <span class="hint">Showing the main stream.</span>
      </div>
    </div>
  {:else if cameras.length > 0}
    <div
      class="grid"
      class:one={cameras.length === 1}
      class:two={cameras.length === 2}
      style="--tiles: {cameras.length}"
    >
      {#each cameras as camera, index (camera.camera_id)}
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        <div
          class="cell"
          class:feature={index === 0 && cameras.length >= 3}
          on:click={() => (expanded = camera.camera_id)}
          role="button"
          tabindex="0"
          on:keydown={(event) => event.key === 'Enter' && (expanded = camera.camera_id)}
          title="Click for the full-size main stream"
        >
          <LiveTile
            name={camera.name}
            stream={camera.grid_stream}
            unhealthy={unhealthy(camera.camera_id)}
            motion={$motion.has(camera.camera_id)}
            motionZone={$motion.get(camera.camera_id)?.label ?? ''}
            compact={index !== 0}
          />
        </div>
      {/each}
    </div>

    {#if recent.length}
      <div class="strip">
        <span class="hint label">Recent events</span>
        {#each recent as event (event.id)}
          <a class="chip" href={eventLink(event)} use:link title="Open this moment in Rewind">
            <span class="swatch" style="--swatch: {event.color}"></span>
            <span class="mono">{formatClock(event.start_ts)}</span>
            {event.camera_name} &middot; {event.label}
          </a>
        {/each}
      </div>
    {/if}

    {#if !loading && cameras.length === 0}
      <p class="hint empty">
        No cameras yet. Add them on <a href="/cameras">Nodes</a>.
      </p>
    {/if}
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  /* 1 + 2: the first tile takes two columns and two rows, the rest stack
     beside it. Three columns only make sense from three cameras up: with
     one camera the tile was a third of the width and the video letterboxed
     into a tall slot with two thirds of the page black beside it. */
  .grid {
    display: grid;
    gap: 8px;
    grid-template-columns: repeat(3, 1fr);
    grid-auto-rows: minmax(0, 1fr);
    height: calc(100vh - 160px);
    min-height: 420px;
  }

  .grid.one {
    grid-template-columns: 1fr;
  }

  .grid.two {
    grid-template-columns: repeat(2, 1fr);
  }

  .cell {
    position: relative;
    min-height: 0;
    cursor: pointer;
  }

  .cell.feature {
    grid-column: span 2;
    grid-row: span 2;
  }

  .expanded {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .full {
    height: calc(100vh - 220px);
    min-height: 360px;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }

  .strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 8px;
  }

  .strip .label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-faint);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 30px;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--panel);
    color: var(--text-secondary);
    font-size: 12px;
    text-decoration: none;
  }
  .chip:hover {
    color: var(--text);
  }

  .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    background: var(--swatch, var(--motion-magenta));
  }

  .note {
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--magenta);
    border-radius: var(--radius);
    background: var(--panel);
    font-size: 13px;
  }
  .note.warn {
    border-color: var(--yellow);
  }

  /* With no cameras the grid is not rendered at all, so this is the whole
     page: centre it rather than leaving a full-height black void above a
     line of text pinned to the bottom. */
  .empty {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 40vh;
    padding: 16px;
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-lg);
  }

  @media (max-width: 760px) {
    .grid {
      grid-template-columns: 1fr;
      height: auto;
    }
    .cell {
      aspect-ratio: 16 / 9;
    }
    .cell.feature {
      grid-column: span 1;
      grid-row: span 1;
    }
    .full {
      height: auto;
      aspect-ratio: 16 / 9;
    }
  }
</style>
