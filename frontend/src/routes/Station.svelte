<!-- The viewing station.
     Full screen, no chrome, configured entirely by URL so it survives a
     reload: /station?layout=2x2&cams=front,drive,yard&cycle=0

     Built to be left open for days, which is what everything here is for: a
     tile watchdog in LiveTile, a server-unreachable banner with retry, a
     daily page reload for browser leaks, and no state that is not in the URL. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import LiveTile from '../lib/LiveTile.svelte';
  import { api } from '../lib/api.js';
  import { motion } from '../lib/live-events.js';
  import { link, navigate } from '../lib/router.js';
  import {
    layoutOf,
    page as pageOf,
    pageCount,
    parseStationOptions,
    selectCameras,
    showsStatusCell,
    msUntilHour,
  } from '../lib/layouts.js';

  const POLL_MS = 15000;
  const RETRY_MS = 5000;

  const options = parseStationOptions(window.location.search);

  let cameras = [];
  let health = null;
  let reachable = true;
  let pageIndex = 0;
  let single = null; // camera_id shown alone after a double-click
  let clock = '';
  let date = '';

  let pollTimer;
  let cycleTimer;
  let clockTimer;
  let reloadTimer;
  let retryTimer;

  async function load() {
    try {
      const [live, state] = await Promise.all([api.liveStreams(), api.health()]);
      cameras = selectCameras(live.cameras, options.cams);
      health = state;
      reachable = true;
      clearTimeout(retryTimer);
    } catch {
      reachable = false;
      // Retry faster than the normal poll while the server is away.
      clearTimeout(retryTimer);
      retryTimer = setTimeout(load, RETRY_MS);
    }
  }

  function tick() {
    const now = new Date();
    clock = now.toLocaleTimeString(undefined, { hour12: false });
    date = now.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    });
  }

  function onKey(event) {
    if (event.key !== 'Escape') return;
    // First Escape closes an expanded tile; a second one leaves the
    // station. With a full grid there is no clock cell, so without this
    // there is no way back at all short of editing the URL.
    if (single !== null) {
      single = null;
      return;
    }
    navigate('/live');
  }

  onMount(() => {
    load();
    tick();
    pollTimer = setInterval(load, POLL_MS);
    clockTimer = setInterval(tick, 1000);
    if (options.cycleSeconds > 0) {
      cycleTimer = setInterval(() => {
        if (!single) pageIndex += 1;
      }, options.cycleSeconds * 1000);
    }
    // Memory safety: a tab left open for days leaks in every browser, so
    // reload once a night at a configurable hour (default 04:00).
    reloadTimer = setTimeout(() => window.location.reload(), msUntilHour(options.reloadHour));
    window.addEventListener('keydown', onKey);
  });

  onDestroy(() => {
    clearInterval(pollTimer);
    clearInterval(cycleTimer);
    clearInterval(clockTimer);
    clearTimeout(reloadTimer);
    clearTimeout(retryTimer);
    window.removeEventListener('keydown', onKey);
  });

  $: layout = layoutOf(options.layout);
  $: withStatus = showsStatusCell(cameras.length, options.layout);
  $: visible = single
    ? cameras.filter((camera) => camera.camera_id === single)
    : pageOf(cameras, options.layout, pageIndex, { reserveStatusCell: withStatus });
  $: pages = pageCount(cameras.length, options.layout, { reserveStatusCell: withStatus });
  $: recorders = health?.components?.recorders;
  $: columns = single ? 1 : layout.columns;
  $: rows = single ? 1 : layout.rows;

  function unhealthy(cameraId) {
    const state = recorders?.state;
    if (!state || state === 'idle' || state === 'not-started') return false;
    // The health detail names the cameras needing attention.
    return (recorders.detail || '').includes(cameraId);
  }
</script>

<svelte:head><title>Nightwire station</title></svelte:head>

<div class="station" style="--columns: {columns}; --rows: {rows}">
  {#if !reachable}
    <div class="banner" role="alert">
      Nightwire is unreachable. Retrying every {RETRY_MS / 1000} seconds.
    </div>
  {/if}

  {#each visible as camera, index (camera.camera_id)}
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
    <div
      class="cell"
      class:feature={layout.feature && index === 0 && !single}
      on:dblclick={() => (single = single ? null : camera.camera_id)}
      title="Double-click for one camera, Esc to go back"
    >
      <LiveTile
        name={camera.name}
        stream={single ? camera.main_stream : camera.grid_stream}
        unhealthy={unhealthy(camera.camera_id)}
        motion={$motion.has(camera.camera_id)}
        motionZone={$motion.get(camera.camera_id)?.label ?? ''}
        compact={!single}
      />
    </div>
  {/each}

  <!-- Hidden until the pointer moves, so a wall screen shows only cameras. -->
  <button class="exit" on:click={() => navigate('/live')} title="Leave the station (Esc)">
    Exit
  </button>

  {#if withStatus && !single}
    <div class="cell status">
      <span class="mono time">{clock}</span>
      <span class="hint">{date}</span>
      <span class="pill">
        <span
          class="dot"
          class:ok={reachable && recorders?.state === 'recording'}
          class:warn={reachable &&
            (recorders?.state === 'degraded' || recorders?.state === 'not-started')}
          class:bad={!reachable}
        ></span>
        {reachable ? (recorders?.detail ?? 'No recorder state') : 'Server unreachable'}
      </span>
      {#if pages > 1}
        <span class="hint">Page {(pageIndex % pages) + 1} of {pages}</span>
      {/if}
      <a href="/live" use:link class="hint">Feeds</a>
    </div>
  {/if}

  {#if cameras.length === 0}
    <div class="cell status">
      <span class="hint">
        {#if !reachable}
          Waiting for the server.
        {:else}
          No cameras to show. Add them on Nodes, or check the ?cams= list.
        {/if}
      </span>
      <a href="/live" use:link class="hint">Feeds</a>
    </div>
  {/if}
</div>

<style>
  .exit {
    position: fixed;
    top: 10px;
    right: 10px;
    z-index: 30;
    padding: 6px 14px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: rgba(10, 6, 18, 0.72);
    color: var(--text-secondary);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  /* Appears on pointer movement or keyboard focus, never on its own: the
     point of the station is a screen with nothing but cameras on it. */
  :global(body:hover) .exit,
  .exit:focus-visible {
    opacity: 1;
  }

  .exit:hover {
    border-color: var(--cyan);
    color: var(--text-primary);
  }

  .station {
    position: relative;
    min-height: 100vh;
    background: var(--black);
    display: grid;
    grid-template-columns: repeat(var(--columns), 1fr);
    grid-template-rows: repeat(var(--rows), 1fr);
    gap: 2px;
    padding: 2px;
  }

  .cell {
    position: relative;
    min-height: 0;
    min-width: 0;
  }

  /* 1+5: the first tile takes a 2x2 block of the 3x3 grid. */
  .cell.feature {
    grid-column: span 2;
    grid-row: span 2;
  }

  .status {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 12px;
    background: var(--tile);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    text-align: center;
  }

  .time {
    font-size: clamp(28px, 6vw, 64px);
    color: var(--text);
    line-height: 1;
  }

  .banner {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 2;
    padding: 10px 16px;
    background: var(--magenta);
    color: var(--ground);
    font-weight: 600;
    text-align: center;
  }

  @media (max-width: 760px) {
    .station {
      grid-template-columns: 1fr;
      grid-template-rows: none;
      grid-auto-rows: minmax(0, 1fr);
    }
    .cell.feature {
      grid-column: span 1;
      grid-row: span 1;
    }
  }
</style>
