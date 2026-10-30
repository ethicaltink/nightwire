<!-- The Rewind timeline: a whole-day bar with a detail bracket above a
     zoomable detail view.

     One lane per motion event source sits above the recorded bar, in the
     `lanes` slot below. -->
<script>
  import { createEventDispatcher } from 'svelte';
  import {
    ZOOMS,
    barFor,
    formatClock,
    formatDuration,
    formatTick,
    fractionOf,
    nearestZoomName,
    panBy,
    ticks,
    timeAt,
    windowAround,
    zoomAt,
  } from './timescale.js';

  export let dayStart;
  export let dayEnd;
  /** { ranges, gaps, not_yet } from GET /api/cameras/{id}/timeline */
  export let timeline = null;
  /** The detail window currently shown: { start, end } */
  export let window;
  /** Playhead position in unix ms, or null. */
  export let playhead = null;
  export let cameraId = '';

  const dispatch = createEventDispatcher();

  let detail;
  let hover = null;
  let dragging = false;
  let dragFrom = null;

  $: bounds = { start: dayStart, end: dayEnd };
  $: dayWindow = bounds;
  $: zoomName = nearestZoomName(window.end - window.start);
  $: detailTicks = ticks(window, 8);
  $: span = window.end - window.start;

  function positionIn(element, event) {
    const box = element.getBoundingClientRect();
    const fraction = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
    return fraction;
  }

  function seekFrom(element, event, target) {
    dispatch('seek', { at: timeAt(positionIn(element, event), target) });
  }

  function setWindow(next) {
    window = next;
    dispatch('window', next);
  }

  function onDayClick(event) {
    // Clicking the day bar moves the detail view there and seeks.
    const at = timeAt(positionIn(event.currentTarget, event), dayWindow);
    setWindow(windowAround(at, span, bounds));
    dispatch('seek', { at });
  }

  function onDetailWheel(event) {
    event.preventDefault();
    const at = timeAt(positionIn(event.currentTarget, event), window);
    setWindow(zoomAt(window, at, event.deltaY > 0 ? 1.3 : 1 / 1.3, bounds));
  }

  function onDetailDown(event) {
    dragging = true;
    dragFrom = { x: event.clientX, window: { ...window } };
  }

  function onDetailMove(event) {
    const fraction = positionIn(event.currentTarget, event);
    const at = timeAt(fraction, window);
    hover = { at, fraction };

    if (!dragging || !dragFrom) return;
    const box = event.currentTarget.getBoundingClientRect();
    const movedFraction = (event.clientX - dragFrom.x) / box.width;
    const movedMs = -movedFraction * (dragFrom.window.end - dragFrom.window.start);
    setWindow(panBy(dragFrom.window, movedMs, bounds));
  }

  function onDetailUp(event) {
    const wasDrag = dragFrom && Math.abs(event.clientX - dragFrom.x) > 4;
    dragging = false;
    dragFrom = null;
    if (!wasDrag) seekFrom(event.currentTarget, event, window);
  }

  function pickZoom(name) {
    const centre = playhead ?? (window.start + window.end) / 2;
    const zoom = ZOOMS.find((z) => z.name === name);
    setWindow(windowAround(centre, zoom.span, bounds));
  }

  function nudge(deltaMs) {
    setWindow(panBy(window, deltaMs, bounds));
  }
</script>

<div class="timeline">
  <!-- Whole-day overview -->
  <div class="row">
    <span class="label mono">Day</span>
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
    <div class="bar day" on:click={onDayClick} title="Click to jump there">
      {#if timeline}
        {#each timeline.gaps as gap}
          {@const bar = barFor(gap, dayWindow)}
          {#if bar}
            <span class="seg gap" style="left: {bar.left}%; width: {bar.width}%"></span>
          {/if}
        {/each}
        {#if timeline.not_yet}
          {@const bar = barFor(timeline.not_yet, dayWindow)}
          {#if bar}
            <span class="seg future" style="left: {bar.left}%; width: {bar.width}%"></span>
          {/if}
        {/if}
        {#each timeline.ranges as range}
          {@const bar = barFor(range, dayWindow)}
          {#if bar}
            <span
              class="seg recorded"
              style="left: {bar.left}%; width: {bar.width}%"
              title="{formatClock(range.start_ts)} to {formatClock(range.end_ts)} ({formatDuration(
                range.duration_ms,
              )})"
            ></span>
          {/if}
        {/each}
      {/if}

      <!-- The bracket showing where the detail view is looking -->
      {#if barFor({ start_ts: window.start, end_ts: window.end }, dayWindow)}
        {@const bracket = barFor({ start_ts: window.start, end_ts: window.end }, dayWindow)}
        <span class="bracket" style="left: {bracket.left}%; width: {bracket.width}%"></span>
      {/if}

      {#if playhead !== null}
        <span class="playhead" style="left: {fractionOf(playhead, dayWindow) * 100}%"></span>
      {/if}
    </div>
  </div>

  <!-- Event lanes go here, above the detail bar. -->
  <slot name="lanes" />

  <!-- Zoomable detail -->
  <div class="row">
    <span class="label mono">{zoomName}</span>
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
    <div
      class="bar detail"
      bind:this={detail}
      on:wheel={onDetailWheel}
      on:mousedown={onDetailDown}
      on:mousemove={onDetailMove}
      on:mouseup={onDetailUp}
      on:mouseleave={() => {
        hover = null;
        dragging = false;
        dragFrom = null;
      }}
      class:dragging
    >
      {#if timeline}
        {#each timeline.gaps as gap}
          {@const bar = barFor(gap, window)}
          {#if bar}
            <span class="seg gap" style="left: {bar.left}%; width: {bar.width}%"></span>
          {/if}
        {/each}
        {#if timeline.not_yet}
          {@const bar = barFor(timeline.not_yet, window)}
          {#if bar}
            <span class="seg future" style="left: {bar.left}%; width: {bar.width}%"></span>
          {/if}
        {/if}
        {#each timeline.ranges as range}
          {@const bar = barFor(range, window)}
          {#if bar}
            <span class="seg recorded" style="left: {bar.left}%; width: {bar.width}%"></span>
          {/if}
        {/each}
      {/if}

      {#each detailTicks as tick}
        <span class="tick" style="left: {tick.fraction * 100}%">
          <span class="tick-label mono">{formatTick(tick.at, span)}</span>
        </span>
      {/each}

      {#if playhead !== null && playhead >= window.start && playhead <= window.end}
        <span class="playhead" style="left: {fractionOf(playhead, window) * 100}%"></span>
      {/if}

      {#if hover}
        <span class="hoverline" style="left: {hover.fraction * 100}%"></span>
        <div
          class="preview"
          style="left: {hover.fraction * 100}%"
          class:flip={hover.fraction > 0.75}
        >
          <img src={`/api/cameras/${cameraId}/thumb?t=${hover.at}`} alt="" loading="lazy" />
          <span class="mono">{formatClock(hover.at)}</span>
        </div>
      {/if}
    </div>
  </div>

  <div class="controls">
    <span class="zooms">
      {#each ZOOMS as zoom}
        <button class="chip" class:on={zoomName === zoom.name} on:click={() => pickZoom(zoom.name)}>
          {zoom.name}
        </button>
      {/each}
    </span>
    <span class="pans">
      <button class="chip" on:click={() => nudge(-span / 2)} title="Pan back">&larr;</button>
      <button class="chip" on:click={() => nudge(span / 2)} title="Pan forward">&rarr;</button>
    </span>
    <span class="hint">
      Wheel zooms, drag pans, click seeks.
      {#if timeline}
        {formatDuration(timeline.covered_ms)} recorded ({timeline.coverage_percent}%).
      {/if}
    </span>
  </div>
</div>

<style>
  .timeline {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .row {
    display: flex;
    align-items: stretch;
    gap: 8px;
  }

  .label {
    flex: 0 0 3.2rem;
    display: flex;
    align-items: center;
    font-size: 11px;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  .bar {
    position: relative;
    flex: 1;
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    cursor: pointer;
  }

  .bar.day {
    height: 26px;
  }

  .bar.detail {
    height: 64px;
    cursor: crosshair;
  }
  .bar.detail.dragging {
    cursor: grabbing;
  }

  .seg {
    position: absolute;
    top: 0;
    bottom: 0;
  }

  .seg.recorded {
    background: var(--purple);
  }

  .seg.gap {
    background: var(--yellow);
    opacity: 0.45;
  }

  .seg.future {
    background: var(--border);
    opacity: 0.5;
  }

  .bracket {
    position: absolute;
    top: 0;
    bottom: 0;
    border: 1px solid var(--cyan);
    background: transparent;
    box-shadow: var(--glow-cyan-soft);
    pointer-events: none;
  }

  .playhead {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
    background: var(--cyan);
    box-shadow: var(--glow-cyan-strong);
    pointer-events: none;
  }

  .hoverline {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 1px;
    background: var(--text-muted);
    pointer-events: none;
  }

  .tick {
    position: absolute;
    bottom: 0;
    top: 70%;
    width: 1px;
    background: var(--border-subtle);
    pointer-events: none;
  }

  .tick-label {
    position: absolute;
    bottom: 2px;
    left: 3px;
    font-size: 10px;
    color: var(--text-faint);
    white-space: nowrap;
  }

  .preview {
    position: absolute;
    bottom: calc(100% + 6px);
    transform: translateX(-50%);
    z-index: 3;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 4px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    pointer-events: none;
  }
  .preview.flip {
    transform: translateX(-85%);
  }
  .preview img {
    display: block;
    width: 160px;
    height: 90px;
    object-fit: cover;
    background: var(--tile);
    border-radius: 3px;
  }
  .preview span {
    font-size: 11px;
    text-align: center;
    color: var(--text-secondary);
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 14px;
    padding-left: 3.2rem;
  }

  .zooms,
  .pans {
    display: inline-flex;
    gap: 4px;
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

  @media (max-width: 760px) {
    .label {
      display: none;
    }
    .controls {
      padding-left: 0;
    }
  }
</style>
