<!-- One live video tile.
     Wraps go2rtc's <video-stream> web component, which does WebRTC with an
     automatic MSE fallback. Everything around it is the self-healing the
     station needs: a tile with no frames for 10 s tears down and reconnects.

     The `motion` prop turns the glow below on while a camera reports
     motion; keeping the tile's own state separate from that
     asked for, so the tile does not need rework later. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { playerReady } from './player.js';

  export let name = '';
  export let stream = '';
  export let mode = 'webrtc,mse';
  /** True while this camera reports motion. */
  export let motion = false;
  /** The zone label shown beside the MOTION badge. */
  export let motionZone = '';
  /** Red dot when the recorder for this camera is not healthy. */
  export let unhealthy = false;
  export let showClock = false;
  export let compact = false;

  const NO_FRAMES_MS = 10000;
  const CHECK_MS = 2000;

  let host;
  let player;
  let attachToken = 0;
  let lastFrameAt = Date.now();
  let reconnects = 0;
  let state = 'connecting';
  let clock = '';
  let watchdog;
  let clockTimer;

  function source() {
    // Relative so it follows the page's host and scheme. The server proxies
    // this to go2rtc on localhost.
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/live/ws?src=${encodeURIComponent(stream)}`;
  }

  async function attach() {
    // Both onMount and the reactive statement below call this, and the
    // watchdog calls it again on a stall. It awaits, so without a token the
    // second call appends a **second** player while the first is still in
    // flight: two WebSocket connections and two decodes per tile, which is
    // exactly what happened the first time this ran in a browser.
    const token = ++attachToken;
    detach();
    if (!host || !stream) return;

    // The player element is registered by an async import, so a tile that
    // mounts first has to wait rather than conclude it is missing.
    const ready = await playerReady;
    if (token !== attachToken || !host || !stream) return;
    if (!ready || !window.customElements?.get('video-stream')) {
      state = 'no-player';
      return;
    }
    detach(); // anything that arrived while awaiting
    player = document.createElement('video-stream');
    player.mode = mode;
    player.background = true; // keep running while the tab is hidden
    player.style.width = '100%';
    player.style.height = '100%';
    host.appendChild(player);
    player.src = source();
    state = 'connecting';
    lastFrameAt = Date.now();

    // video-stream contains a <video>; its timeupdate is the only reliable
    // "frames are arriving" signal across both WebRTC and MSE.
    const video = player.querySelector('video') ?? player.video;
    if (video) {
      // go2rtc turns native controls on. A wall of live tiles is not a
      // player: the tile owns its own overlay, and a row of play/mute
      // buttons over every camera is not what the design shows.
      video.controls = false;
      video.addEventListener('timeupdate', onFrame);
      video.addEventListener('playing', onFrame);
    }
  }

  function onFrame() {
    lastFrameAt = Date.now();
    if (state !== 'live') state = 'live';
  }

  function detach() {
    // Clears whatever is in the host, not only the player this instance
    // knows about, so a stray element from an overlapping attach cannot
    // survive as a second live connection.
    if (host) {
      for (const stale of host.querySelectorAll('video-stream')) {
        if (stale !== player) stale.remove();
      }
    }
    if (player) {
      try {
        player.src = '';
      } catch {
        /* the component may already be torn down */
      }
      player.remove();
      player = null;
    }
  }

  function check() {
    if (!stream || state === 'no-player') return;
    if (Date.now() - lastFrameAt > NO_FRAMES_MS) {
      reconnects += 1;
      state = 'reconnecting';
      attach();
    }
  }

  function tickClock() {
    clock = new Date().toLocaleTimeString(undefined, { hour12: false });
  }

  onMount(() => {
    attach();
    watchdog = setInterval(check, CHECK_MS);
    if (showClock) {
      tickClock();
      clockTimer = setInterval(tickClock, 1000);
    }
  });

  onDestroy(() => {
    clearInterval(watchdog);
    clearInterval(clockTimer);
    detach();
  });

  // Re-attach when the tile is pointed at a different stream.
  $: if (host && stream) attach();
</script>

<div class="tile" class:motion class:compact>
  <div class="video" bind:this={host}></div>

  {#if state !== 'live'}
    <div class="overlay">
      {#if state === 'no-player'}
        <span class="hint">Player not loaded</span>
      {:else if state === 'reconnecting'}
        <span class="hint">Reconnecting...</span>
      {:else}
        <span class="hint">Connecting...</span>
      {/if}
    </div>
  {/if}

  <div class="bar">
    <span class="label">
      {#if unhealthy}<span class="rec" title="Recording unhealthy"></span>{/if}
      {name}
    </span>
    {#if motion}
      <span class="badge">MOTION{motionZone ? ` · ${motionZone}` : ''}</span>
    {/if}
    {#if showClock}
      <span class="mono clock">{clock}</span>
    {/if}
  </div>
</div>

<style>
  /* Backstop for the `controls` flag the player sets on its own <video>
     after we clear it: a live wall shows no native player chrome. */
  .video :global(video-stream) {
    display: block;
    width: 100%;
    height: 100%;
  }
  .video :global(video-stream video) {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .video :global(video::-webkit-media-controls) {
    display: none !important;
  }

  .tile {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
    background: var(--tile);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }

  /* On while the camera reports motion. */
  .tile.motion {
    border-color: var(--motion-magenta);
    box-shadow: var(--glow-motion);
  }

  .video {
    position: absolute;
    inset: 0;
    display: flex;
  }

  .video :global(video) {
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: var(--black);
  }

  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }

  .bar {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    background: linear-gradient(transparent, var(--ground));
    font-size: 13px;
  }

  .compact .bar {
    padding: 4px 8px;
    font-size: 12px;
  }

  .label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
  }

  .rec {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--magenta);
  }

  .badge {
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--motion-magenta);
    color: var(--ground);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }

  .clock {
    margin-left: auto;
    color: var(--text-secondary);
  }
</style>
