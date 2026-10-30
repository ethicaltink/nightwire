<!-- The Events side panel on Rewind: filter chips and the day's list. -->
<script>
  import { createEventDispatcher } from 'svelte';
  import { newestFirst } from './motion.js';
  import { formatClock, formatDuration } from './timescale.js';

  export let events = [];
  export let sources = [];
  export let hidden = new Set();
  export let currentId = null;

  const dispatch = createEventDispatcher();

  $: listed = newestFirst(events.filter((event) => !hidden.has(event.source_id)));
  $: counts = sources.map((source) => ({
    source,
    count: events.filter((event) => event.source_id === source.id).length,
  }));
</script>

<section class="panel">
  <header>
    <h2>Events</h2>
    <span class="hint">{listed.length}</span>
  </header>

  {#if sources.length === 0}
    <p class="hint">
      No event sources chosen. Pick them on <a href="/cameras">Nodes</a>.
    </p>
  {:else}
    <div class="chips">
      {#each counts as entry (entry.source.id)}
        <button
          class="chip"
          class:off={hidden.has(entry.source.id)}
          style="--swatch: {entry.source.color}"
          on:click={() => dispatch('toggle', { sourceId: entry.source.id })}
          title={hidden.has(entry.source.id) ? 'Show this lane' : 'Hide this lane'}
        >
          <span class="swatch"></span>
          {entry.source.label}
          <span class="count">{entry.count}</span>
        </button>
      {/each}
    </div>

    <div class="nav">
      <button class="chip" on:click={() => dispatch('prev')} title="Previous event (Shift+N)">
        &larr; Prev
      </button>
      <button class="chip" on:click={() => dispatch('next')} title="Next event (N)">
        Next &rarr;
      </button>
    </div>

    {#if listed.length === 0}
      <p class="hint">No events on this day for the lanes shown.</p>
    {:else}
      <ul>
        {#each listed as event (event.id)}
          <li class:current={event.id === currentId}>
            <button class="row" on:click={() => dispatch('pick', { event })}>
              <span class="swatch" style="--swatch: {event.color}"></span>
              <span class="when mono">{formatClock(event.start_ts)}</span>
              <span class="what">{event.label}</span>
              <span class="how hint">
                {event.end_ts === null ? 'open' : formatDuration(event.end_ts - event.start_ts)}
              </span>
            </button>
            {#if event.id === currentId}
              <button class="chip clip" on:click={() => dispatch('clip', { event })}>
                Clip this event
              </button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  section {
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  h2 {
    font-size: 13px;
    letter-spacing: 1px;
    color: var(--text-secondary);
    margin-right: auto;
  }
  .chips,
  .nav {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 30px;
    padding: 0 9px;
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
  .chip.off {
    opacity: 0.45;
    text-decoration: line-through;
  }
  .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    background: var(--swatch, var(--motion-magenta));
  }
  .count {
    color: var(--text-faint);
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 22rem;
    overflow-y: auto;
  }
  li.current {
    background: var(--tile);
    border-radius: var(--radius);
  }
  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 6px 6px;
    border: 0;
    background: none;
    color: var(--text);
    font: 400 13px var(--font-body);
    text-align: left;
    cursor: pointer;
  }
  .row:hover {
    background: var(--tile);
    border-radius: var(--radius);
  }
  .when {
    font-size: 12px;
    color: var(--text-secondary);
  }
  .what {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .clip {
    margin: 0 6px 6px;
  }
</style>
