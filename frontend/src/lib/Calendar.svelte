<!-- A month calendar that only offers days with footage.

     Days with no recording are not clickable: picking one could only ever
     land on an empty player. Days that do have footage are shaded by how
     much of the 24 hours they cover, so an outage reads as a lighter cell
     rather than being hidden. -->
<script>
  import { createEventDispatcher } from 'svelte';
  import {
    describeDay,
    dayFullness,
    indexDays,
    monthGrid,
    monthOf,
    shiftMonth,
  } from './calendar.js';

  /** `YYYY-MM-DD` currently selected. */
  export let selected = '';
  /** `[{date, segments, seconds, bytes}]` from /api/cameras/{id}/days. */
  export let days = [];
  /** `YYYY-MM-DD` for the server's today. */
  export let today = '';

  const dispatch = createEventDispatcher();
  const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];

  let month = monthOf(selected || today || '2026-01-01');
  // Follow the selection when it moves outside the month on screen, so a
  // deep link or a keyboard jump does not leave the grid behind.
  $: if (selected && monthOf(selected) !== month) month = monthOf(selected);

  $: byDate = indexDays(days);
  $: cells = monthGrid(month, byDate, { today, selected });
  $: label = new Date(`${month}-01T00:00:00`).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  });
  $: recorded = days.filter((d) => d.segments > 0).map((d) => d.date);
  $: earliest = recorded.length ? recorded[0] : null;
  $: latest = recorded.length ? recorded[recorded.length - 1] : null;
  $: canGoBack = earliest ? month > monthOf(earliest) : false;
  $: canGoForward = latest ? month < monthOf(latest) : false;

  function pick(cell) {
    if (!cell.hasFootage) return;
    dispatch('pick', cell.date);
  }
</script>

<div class="calendar panel">
  <div class="head">
    <button
      class="button-quiet nav"
      on:click={() => (month = shiftMonth(month, -1))}
      disabled={!canGoBack}
      title="Previous month"
    >‹</button>
    <span class="label">{label}</span>
    <button
      class="button-quiet nav"
      on:click={() => (month = shiftMonth(month, 1))}
      disabled={!canGoForward}
      title="Next month"
    >›</button>
  </div>

  <div class="grid weekdays" aria-hidden="true">
    {#each WEEKDAYS as name, index}
      <span class="weekday">{name}</span>
    {/each}
  </div>

  <div class="grid">
    {#each cells as cell (cell.date)}
      <button
        class="cell"
        class:outside={cell.outside}
        class:has={cell.hasFootage}
        class:today={cell.isToday}
        class:selected={cell.isSelected}
        style="--fill: {dayFullness(cell.seconds)}"
        disabled={!cell.hasFootage}
        title={describeDay(cell)}
        on:click={() => pick(cell)}
      >
        {cell.day}
      </button>
    {/each}
  </div>

  {#if recorded.length}
    <p class="hint footer">
      {recorded.length} day{recorded.length === 1 ? '' : 's'} recorded
      {#if latest}
        · <button class="link" on:click={() => dispatch('pick', latest)}>latest</button>
      {/if}
    </p>
  {:else}
    <p class="hint footer">No footage recorded yet.</p>
  {/if}
</div>

<style>
  .calendar {
    padding: 12px;
    width: 268px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .label {
    font-size: 13px;
    color: var(--text-secondary);
    letter-spacing: 0.02em;
  }

  .nav {
    padding: 2px 10px;
    line-height: 1.2;
  }

  .nav:disabled {
    opacity: 0.3;
    cursor: default;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 3px;
  }

  .weekday {
    text-align: center;
    font-size: 11px;
    color: var(--text-muted);
  }

  .cell {
    aspect-ratio: 1;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    font: inherit;
    font-size: 12px;
    cursor: default;
  }

  .cell.outside {
    opacity: 0.35;
  }

  /* Shaded by how much of the day was recorded, so a gap is visible rather
     than hidden behind a binary dot. */
  .cell.has {
    cursor: pointer;
    color: var(--text-primary);
    background: color-mix(in srgb, var(--cyan) calc(var(--fill) * 42%), transparent);
    border-color: color-mix(in srgb, var(--cyan) 30%, transparent);
  }

  .cell.has:hover {
    border-color: var(--cyan);
  }

  .cell.today {
    outline: 1px dashed var(--border-subtle);
    outline-offset: -3px;
  }

  .cell.selected {
    border-color: var(--cyan);
    box-shadow: 0 0 0 1px var(--cyan) inset;
    color: var(--text-primary);
  }

  .footer {
    margin: 0;
    text-align: center;
  }

  .link {
    background: none;
    border: 0;
    padding: 0;
    font: inherit;
    color: var(--cyan);
    cursor: pointer;
    text-decoration: underline;
  }
</style>
