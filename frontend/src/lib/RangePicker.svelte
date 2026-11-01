<!-- Choosing what stretch of time Rewind is looking at.

     Two dates picked on one calendar, plus a time of day for each end. The
     calendar offers only days that have footage, so a range cannot be built
     over nothing; the times are free, because a range may legitimately start
     before recording did. -->
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
  import { dateOf, dayRange, rangeFrom, timeOf } from './range.js';

  /** `{start, end}` in unix ms. */
  export let range;
  /** `[{date, segments, seconds, bytes}]` from /api/cameras/{id}/days. */
  export let days = [];
  /** `YYYY-MM-DD` for the server's today. */
  export let today = '';

  const dispatch = createEventDispatcher();
  const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];

  let startDate = dateOf(range.start);
  let startTime = timeOf(range.start);
  let endDate = dateOf(range.end);
  let endTime = timeOf(range.end);
  // Which end the next calendar click sets. Clicking a day sets the start
  // and arms the end, so picking a span is two clicks and no mode switch.
  let picking = 'start';
  let month = monthOf(startDate);

  $: byDate = indexDays(days);
  $: cells = monthGrid(month, byDate, { today, selected: startDate });
  $: label = new Date(`${month}-01T00:00:00`).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  });
  $: recorded = days.filter((d) => d.segments > 0).map((d) => d.date);
  $: earliest = recorded.length ? recorded[0] : null;
  $: latest = recorded.length ? recorded[recorded.length - 1] : null;
  $: canGoBack = earliest ? month > monthOf(earliest) : false;
  $: canGoForward = latest ? month < monthOf(latest) : false;
  $: inRange = (date) => date >= startDate && date <= endDate;

  function pickDay(cell) {
    if (!cell.hasFootage) return;
    if (picking === 'start') {
      startDate = cell.date;
      if (endDate < startDate) endDate = cell.date;
      picking = 'end';
    } else {
      if (cell.date < startDate) {
        startDate = cell.date;
      } else {
        endDate = cell.date;
      }
      picking = 'start';
    }
  }

  function apply() {
    const next = rangeFrom(startDate, startTime, endDate, endTime);
    if (next) dispatch('apply', next);
  }

  /** The whole of one day, the common case, in one click. */
  function wholeDay(date) {
    const whole = dayRange(date);
    if (!whole) return;
    startDate = dateOf(whole.start);
    endDate = dateOf(whole.start);
    startTime = '00:00';
    endTime = '23:59';
    dispatch('apply', rangeFrom(startDate, '00:00', endDate, '23:59'));
  }

  function lastHours(hours) {
    const end = Date.now();
    const start = end - hours * 3_600_000;
    startDate = dateOf(start);
    startTime = timeOf(start);
    endDate = dateOf(end);
    endTime = timeOf(end);
    dispatch('apply', { start, end });
  }
</script>

<div class="picker panel">
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
    {#each WEEKDAYS as name}<span class="weekday">{name}</span>{/each}
  </div>

  <div class="grid">
    {#each cells as cell (cell.date)}
      <button
        class="cell"
        class:outside={cell.outside}
        class:has={cell.hasFootage}
        class:today={cell.isToday}
        class:edge={cell.date === startDate || cell.date === endDate}
        class:within={cell.hasFootage && inRange(cell.date)}
        style="--fill: {dayFullness(cell.seconds)}"
        disabled={!cell.hasFootage}
        title={describeDay(cell)}
        on:click={() => pickDay(cell)}
        on:dblclick={() => wholeDay(cell.date)}
      >
        {cell.day}
      </button>
    {/each}
  </div>

  <p class="hint picking">
    {picking === 'start' ? 'Click a day to set the start' : 'Click a day to set the end'}
    · double-click for a whole day
  </p>

  <div class="fields">
    <label class="field">
      <span>From</span>
      <input class="input" type="date" bind:value={startDate} />
    </label>
    <label class="field time">
      <span>at</span>
      <input class="input" type="time" step="60" bind:value={startTime} />
    </label>
    <label class="field">
      <span>To</span>
      <input class="input" type="date" bind:value={endDate} />
    </label>
    <label class="field time">
      <span>at</span>
      <input class="input" type="time" step="60" bind:value={endTime} />
    </label>
  </div>

  <div class="actions">
    <button class="button-quiet" on:click={() => lastHours(1)}>Last hour</button>
    <button class="button-quiet" on:click={() => lastHours(24)}>Last 24h</button>
    {#if latest}
      <button class="button-quiet" on:click={() => wholeDay(latest)}>Latest day</button>
    {/if}
    <button class="button" on:click={apply}>Apply</button>
  </div>
</div>

<style>
  .picker {
    padding: 12px;
    width: 300px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .label {
    font-size: 13px;
    color: var(--text-primary);
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
    color: var(--text-secondary);
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

  .cell.has {
    cursor: pointer;
    color: var(--text-primary);
    background: color-mix(in srgb, var(--cyan) calc(var(--fill) * 42%), transparent);
    border-color: color-mix(in srgb, var(--cyan) 30%, transparent);
  }

  /* Every day between the two ends, so the span reads at a glance. */
  .cell.within {
    background: color-mix(in srgb, var(--magenta) 26%, transparent);
  }

  .cell.edge {
    border-color: var(--cyan);
    box-shadow: 0 0 0 1px var(--cyan) inset;
    color: var(--text-primary);
  }

  .cell.today {
    outline: 1px dashed var(--border-subtle);
    outline-offset: -3px;
  }

  .picking {
    margin: 0;
    text-align: center;
  }

  .fields {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 6px 8px;
    align-items: end;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11px;
    color: var(--text-secondary);
  }

  .field.time {
    width: 104px;
  }

  .actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  .actions .button {
    margin-left: auto;
  }
</style>
