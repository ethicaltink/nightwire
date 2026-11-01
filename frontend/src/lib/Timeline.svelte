<!-- The Rewind timeline: one zoomable bar over the range the picker chose.

     There used to be a whole-day bar above this one. It was removed because
     it answered a question the calendar already answers, and badly: it was
     24 hours wide whatever had been recorded, so a few hours of footage was
     an unreadable sliver.

     Dragging on the bar selects a clip, because that is the thing anyone is
     here to do. Panning moved to shift-drag and the arrow buttons.

     One lane per motion event source sits above the bar, in the `lanes`
     slot. -->
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
  import { formatSpan, handleNear, isUsable, moveHandle, selectionFrom } from './range.js';

  /** The range the picker chose: the outer bounds of everything here. */
  export let rangeStart;
  export let rangeEnd;
  /** { ranges, gaps, not_yet } from GET /api/cameras/{id}/timeline */
  export let timeline = null;
  /** The zoom window currently shown: { start, end } */
  export let window;
  /** Playhead position in unix ms, or null. */
  export let playhead = null;
  /** The clip selection: { start, end } or null. Bound. */
  export let selection = null;
  export let cameraId = '';

  const dispatch = createEventDispatcher();

  let bar;
  let hover = null;
  /** 'pan' | 'select' | 'handle', or null when idle. */
  let gesture = null;
  let gestureFrom = null;
  let grabbed = null;

  $: bounds = { start: rangeStart, end: rangeEnd };
  $: zoomName = nearestZoomName(window.end - window.start);
  $: barTicks = ticks(window, 8);
  $: span = window.end - window.start;
  // A handle is grabbable within a few pixels, expressed in time so it
  // scales with the zoom rather than getting harder to hit as you zoom out.
  $: grabTolerance = span * 0.012;
  $: selectionBar = selection ? barFor({ start_ts: selection.start, end_ts: selection.end }, window) : null;

  function positionIn(element, event) {
    const box = element.getBoundingClientRect();
    return Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
  }

  function momentAt(event) {
    return timeAt(positionIn(bar, event), window);
  }

  function setWindow(next) {
    window = next;
    dispatch('window', next);
  }

  function setSelection(next) {
    selection = next;
    dispatch('selection', next);
  }

  function onWheel(event) {
    event.preventDefault();
    setWindow(zoomAt(window, momentAt(event), event.deltaY > 0 ? 1.3 : 1 / 1.3, bounds));
  }

  function onDown(event) {
    const at = momentAt(event);
    const handle = handleNear(selection, at, grabTolerance);
    if (handle) {
      gesture = 'handle';
      grabbed = handle;
      return;
    }
    if (event.shiftKey || event.button === 1) {
      gesture = 'pan';
      gestureFrom = { x: event.clientX, window: { ...window } };
      return;
    }
    gesture = 'select';
    gestureFrom = { x: event.clientX, at };
    setSelection(null);
  }

  function onMove(event) {
    const at = momentAt(event);
    hover = { at, fraction: positionIn(bar, event) };

    if (gesture === 'pan' && gestureFrom) {
      const box = bar.getBoundingClientRect();
      const movedFraction = (event.clientX - gestureFrom.x) / box.width;
      const movedMs = -movedFraction * (gestureFrom.window.end - gestureFrom.window.start);
      setWindow(panBy(gestureFrom.window, movedMs, bounds));
      return;
    }
    if (gesture === 'select' && gestureFrom) {
      setSelection(selectionFrom(gestureFrom.at, at));
      return;
    }
    if (gesture === 'handle') {
      setSelection(moveHandle(selection, grabbed, at));
    }
  }

  function onUp(event) {
    const moved = gestureFrom && Math.abs(event.clientX - gestureFrom.x) > 4;
    const wasSelect = gesture === 'select';
    gesture = null;
    gestureFrom = null;
    grabbed = null;
    // A click that never moved is a seek, not a one-pixel clip.
    if (wasSelect && !moved) {
      setSelection(null);
      dispatch('seek', { at: momentAt(event) });
    }
  }

  function onLeave() {
    hover = null;
    gesture = null;
    gestureFrom = null;
    grabbed = null;
  }

  function pickZoom(name) {
    const centre = playhead ?? (window.start + window.end) / 2;
    setWindow(windowAround(centre, ZOOMS.find((z) => z.name === name).span, bounds));
  }

  function nudge(deltaMs) {
    setWindow(panBy(window, deltaMs, bounds));
  }

  function showAll() {
    setWindow({ start: bounds.start, end: bounds.end });
  }
</script>

<div class="timeline">
  <!-- Event lanes go here, above the bar. -->
  <slot name="lanes" />

  <div class="row">
    <span class="label mono">{zoomName}</span>
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
    <div
      class="bar"
      bind:this={bar}
      on:wheel={onWheel}
      on:mousedown={onDown}
      on:mousemove={onMove}
      on:mouseup={onUp}
      on:mouseleave={onLeave}
      class:panning={gesture === 'pan'}
      class:selecting={gesture === 'select' || gesture === 'handle'}
    >
      {#if timeline}
        {#each timeline.gaps as gap}
          {@const seg = barFor(gap, window)}
          {#if seg}<span class="seg gap" style="left: {seg.left}%; width: {seg.width}%"></span>{/if}
        {/each}
        {#if timeline.not_yet}
          {@const seg = barFor(timeline.not_yet, window)}
          {#if seg}<span class="seg future" style="left: {seg.left}%; width: {seg.width}%"></span>{/if}
        {/if}
        {#each timeline.ranges as range}
          {@const seg = barFor(range, window)}
          {#if seg}
            <span
              class="seg recorded"
              style="left: {seg.left}%; width: {seg.width}%"
              title="{formatClock(range.start_ts)} to {formatClock(range.end_ts)} ({formatDuration(
                range.duration_ms,
              )})"
            ></span>
          {/if}
        {/each}
      {/if}

      {#each barTicks as tick}
        <span class="tick" style="left: {tick.fraction * 100}%">
          <span class="tick-label mono">{formatTick(tick.at, span)}</span>
        </span>
      {/each}

      {#if selectionBar}
        <span class="selection" style="left: {selectionBar.left}%; width: {selectionBar.width}%"></span>
        <span class="handle start" style="left: {selectionBar.left}%" title="Drag to move the clip start"></span>
        <span
          class="handle end"
          style="left: {selectionBar.left + selectionBar.width}%"
          title="Drag to move the clip end"
        ></span>
        <span class="edge-label in mono" style="left: {selectionBar.left}%">
          {formatClock(selection.start)}
        </span>
        <span class="edge-label out mono" style="left: {selectionBar.left + selectionBar.width}%">
          {formatClock(selection.end)}
        </span>
      {/if}

      {#if playhead !== null && playhead >= window.start && playhead <= window.end}
        <span class="playhead" style="left: {fractionOf(playhead, window) * 100}%"></span>
      {/if}

      {#if hover && !gesture}
        <span class="hoverline" style="left: {hover.fraction * 100}%"></span>
        <div class="preview" style="left: {hover.fraction * 100}%" class:flip={hover.fraction > 0.75}>
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
      <button class="chip" on:click={showAll} title="Fit the whole selected range">All</button>
    </span>
    <span class="pans">
      <button class="chip" on:click={() => nudge(-span / 2)} title="Pan back">&larr;</button>
      <button class="chip" on:click={() => nudge(span / 2)} title="Pan forward">&rarr;</button>
    </span>
    {#if isUsable(selection)}
      <span class="clipnote mono">
        Clip {formatClock(selection.start)} &rarr; {formatClock(selection.end)}
        · {formatSpan(selection.end - selection.start)}
        <button class="chip" on:click={() => setSelection(null)}>Clear</button>
      </span>
    {:else}
      <span class="hint">Drag to select a clip · click seeks · shift-drag pans · wheel zooms</span>
    {/if}
    {#if timeline}
      <span class="hint">
        {formatDuration(timeline.covered_ms)} recorded ({timeline.coverage_percent}%).
      </span>
    {/if}
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
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  .bar {
    position: relative;
    flex: 1;
    height: 76px;
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    cursor: crosshair;
  }

  .bar.panning {
    cursor: grabbing;
  }

  .bar.selecting {
    cursor: ew-resize;
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

  /* The clip being selected. Deliberately translucent so the recorded and
     gap bands stay readable underneath it. */
  .selection {
    position: absolute;
    top: 0;
    bottom: 0;
    background: color-mix(in srgb, var(--cyan) 22%, transparent);
    border-left: 2px solid var(--cyan);
    border-right: 2px solid var(--cyan);
    pointer-events: none;
  }

  .handle {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 10px;
    margin-left: -5px;
    cursor: ew-resize;
  }

  /* The exact in and out times, which is the whole point of selecting by
     dragging rather than by typing. */
  .edge-label {
    position: absolute;
    top: 3px;
    padding: 1px 5px;
    font-size: 11px;
    color: var(--ground);
    background: var(--cyan);
    border-radius: 3px;
    white-space: nowrap;
    pointer-events: none;
  }

  .edge-label.in {
    transform: translateX(-100%);
    margin-left: -3px;
  }

  .edge-label.out {
    margin-left: 3px;
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
    top: 68%;
    width: 1px;
    background: var(--border-subtle);
    pointer-events: none;
  }

  /* White, with a shadow, because these sit over purple and yellow bands
     and the muted grey they used to be was unreadable on both. */
  .tick-label {
    position: absolute;
    bottom: 2px;
    left: 3px;
    font-size: 11px;
    color: #fff;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.9);
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

  .clipnote {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--cyan);
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
