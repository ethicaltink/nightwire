<!-- One lane per event source, above the recorded bar.
     Span events are blocks, point events diamonds, in the source's colour. -->
<script>
  import { createEventDispatcher } from 'svelte';
  import { barForEvent, markerFor, lanes as lanesOf } from './motion.js';
  import { formatClock, formatDuration } from './timescale.js';

  export let window;
  export let events = [];
  export let sources = [];
  export let hidden = new Set();
  export let currentId = null;

  const dispatch = createEventDispatcher();

  let hover = null;

  $: shown = lanesOf(
    events.filter((event) => !hidden.has(event.source_id)),
    sources.filter((source) => !hidden.has(source.id)),
  );

  function describe(event, source) {
    const end = event.end_ts ?? null;
    const duration = end === null ? 'open' : formatDuration(end - event.start_ts);
    return `${source.label} ${formatClock(event.start_ts)} (${duration})`;
  }
</script>

{#each shown as lane (lane.source.id)}
  <div class="row">
    <span class="label" title={lane.source.label}>{lane.source.label}</span>
    <div class="lane">
      {#each lane.events as event (event.id)}
        {#if lane.source.kind === 'point'}
          {@const marker = markerFor(event, window)}
          {#if marker}
            <!-- svelte-ignore a11y-click-events-have-key-events -->
            <span
              class="diamond"
              class:current={event.id === currentId}
              style="left: {marker.left}%; background: {lane.source.color}"
              title={describe(event, lane.source)}
              on:click|stopPropagation={() => dispatch('pick', { event })}
              on:mouseenter={() => (hover = event)}
              on:mouseleave={() => (hover = null)}
              role="button"
              tabindex="0"
            ></span>
          {/if}
        {:else}
          {@const bar = barForEvent(event, window)}
          {#if bar}
            <!-- svelte-ignore a11y-click-events-have-key-events -->
            <span
              class="block"
              class:current={event.id === currentId}
              class:open={event.end_ts === null}
              style="left: {bar.left}%; width: {bar.width}%; background: {lane.source.color}"
              title={describe(event, lane.source)}
              on:click|stopPropagation={() => dispatch('pick', { event })}
              on:mouseenter={() => (hover = event)}
              on:mouseleave={() => (hover = null)}
              role="button"
              tabindex="0"
            ></span>
          {/if}
        {/if}
      {/each}

      {#if hover}
        {@const bar = barForEvent(hover, window)}
        {#if bar}
          <div class="preview" style="left: {bar.left}%" class:flip={bar.left > 70}>
            {#if hover.snapshot}
              <img src={`/api/events/${hover.id}/snapshot.jpg`} alt="" loading="lazy" />
            {/if}
            <span class="mono">
              {formatClock(hover.start_ts)}
              {#if hover.end_ts !== null}
                &middot; {formatDuration(hover.end_ts - hover.start_ts)}
              {:else}
                &middot; open
              {/if}
            </span>
            <span class="who">{hover.label}</span>
          </div>
        {/if}
      {/if}
    </div>
  </div>
{/each}

<style>
  .row {
    display: flex;
    align-items: stretch;
    gap: 8px;
  }
  .label {
    flex: 0 0 3.2rem;
    display: flex;
    align-items: center;
    font-size: 10px;
    color: var(--text-faint);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .lane {
    position: relative;
    flex: 1;
    height: 16px;
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: 3px;
  }
  .block {
    position: absolute;
    top: 2px;
    bottom: 2px;
    border-radius: 2px;
    cursor: pointer;
    min-width: 2px;
  }
  .block.open {
    /* An event still running has no right edge yet. */
    border-right: 2px dashed var(--text);
  }
  .block.current,
  .diamond.current {
    outline: 1px solid var(--text);
    outline-offset: 1px;
  }
  .diamond {
    position: absolute;
    top: 50%;
    width: 9px;
    height: 9px;
    margin-left: -4.5px;
    transform: translateY(-50%) rotate(45deg);
    cursor: pointer;
  }
  .preview {
    position: absolute;
    bottom: calc(100% + 4px);
    transform: translateX(-50%);
    z-index: 4;
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
    color: var(--text-secondary);
    text-align: center;
  }
  .preview .who {
    color: var(--text);
    font-weight: 600;
  }
  @media (max-width: 760px) {
    .label {
      display: none;
    }
  }
</style>
