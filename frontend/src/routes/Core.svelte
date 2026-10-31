<!-- Core: storage, retention projection, health. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { api, ApiError } from '../lib/api.js';
  import { formatDuration } from '../lib/timescale.js';

  const POLL_MS = 15000;

  let system = null;
  let error = '';
  let saving = false;
  let saved = '';
  let form = {};
  let timer;

  async function load() {
    try {
      system = await api.system();
      if (!Object.keys(form).length) {
        form = {
          retention_days: Number(system.settings.retention_days ?? 45),
          max_disk_percent: Number(system.settings.max_disk_percent ?? 90),
          target_disk_percent: Number(system.settings.target_disk_percent ?? 85),
          max_exports_gb: Number(system.settings.max_exports_gb ?? 200),
        };
      }
      error = '';
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    }
  }

  async function save() {
    saving = true;
    error = '';
    saved = '';
    try {
      await api.patchSettings(form);
      saved = 'Saved.';
      await load();
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      saving = false;
    }
  }

  function gb(bytes) {
    if (!bytes) return '0 GB';
    const tb = bytes / 1000 ** 4;
    return tb >= 1 ? `${tb.toFixed(2)} TB` : `${(bytes / 1000 ** 3).toFixed(1)} GB`;
  }

  onMount(() => {
    load();
    timer = setInterval(load, POLL_MS);
  });
  onDestroy(() => clearInterval(timer));

  $: disk = system?.disk;
  $: warnings = system?.projection?.warnings ?? [];
  $: clockWarnings = (system?.cameras ?? []).filter((c) => c.clock?.warn);
</script>

<div class="page">
  <header>
    <h1>Core</h1>
    {#if system}
      <span class="hint mono">
        Nightwire {system.version} &middot; {system.timezone}
      </span>
    {/if}
  </header>

  {#if error}
    <p class="note" role="alert">{error}</p>
  {/if}

  {#if !system}
    <p class="hint">Loading...</p>
  {:else}
    <!-- Storage -->
    <section class="panel">
      <h2>Storage</h2>
      <p class="hint">
        {gb(disk.used_bytes)} of {gb(disk.total_bytes)} used
        ({disk.used_percent}%) &middot; {gb(disk.free_bytes)} free
      </p>

      <div class="bar" title="Per-camera share of the recordings volume">
        {#each disk.per_camera as share, index}
          <span
            class="slice c{index % 5}"
            style="width: {share.percent}%"
            title="{share.camera_id}: {gb(share.bytes)} ({share.percent}%)"
          ></span>
        {/each}
        <span class="marker target" style="left: {disk.target_percent}%" title="Guard deletes down to {disk.target_percent}%"></span>
        <span class="marker max" style="left: {disk.max_percent}%" title="Guard engages at {disk.max_percent}%"></span>
      </div>
      <p class="hint small">
        Markers are the retention guard's levels: it starts deleting at
        {disk.max_percent}% and stops at {disk.target_percent}%.
      </p>

      {#if system.exports}
        <p class="hint">
          Saved clips: {gb(system.exports.store_bytes)} of
          {gb(system.exports.store_cap_bytes)} ({system.exports.store_percent}%).
          Retention never deletes these.
        </p>
      {/if}
    </section>

    <!-- Retention projection -->
    <section class="panel">
      <h2>Retention projection</h2>
      {#if system.projection.projected_days === null}
        <p class="hint">
          Not enough footage yet to measure a write rate. This fills in once the recorder has
          been running a while.
        </p>
      {:else}
        <p class="hint">
          At the current combined rate of
          {system.projection.combined_gb_per_day} GB/day, the volume holds
          <strong>{system.projection.projected_days} days</strong>
          (measured over the last {system.projection.window_days} days).
        </p>
      {/if}

      {#each warnings as warning}
        <p class="note warn">{warning.detail}</p>
      {/each}

      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>Camera</th>
              <th>GB/day</th>
              <th>Mbps</th>
              <th>Keeps</th>
              <th>Projected</th>
              <th>Segments</th>
            </tr>
          </thead>
          <tbody>
            {#each system.cameras as camera}
              <tr>
                <td>{camera.name}</td>
                <td>{camera.rate ? camera.rate.gb_per_day : '—'}</td>
                <td>{camera.rate ? camera.rate.mbps : '—'}</td>
                <td>{camera.retention_days} d</td>
                <td
                  class:short={system.projection.projected_days !== null &&
                    system.projection.projected_days < camera.retention_days}
                >
                  {system.projection.projected_days ?? '—'}
                  {system.projection.projected_days !== null ? 'd' : ''}
                </td>
                <td>{camera.rate ? camera.rate.segments : 0}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Health -->
    <section class="panel">
      <h2>Health</h2>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>Camera</th>
              <th>Recorder</th>
              <th>Last segment</th>
              <th>Events</th>
              <th>Clock</th>
            </tr>
          </thead>
          <tbody>
            {#each system.cameras as camera}
              <tr>
                <td>{camera.name}</td>
                <td>
                  <span class="dot" class:ok={camera.recorder.state === 'recording'} class:warn={camera.recorder.state === 'reconnecting' || camera.recorder.state === 'not-started'} class:bad={camera.recorder.state === 'error'}></span>
                  {camera.recorder.state}
                </td>
                <td class="mono small">{camera.recorder.last_segment_at ?? '—'}</td>
                <td>
                  <span class="dot" class:ok={camera.events.state === 'subscribed'} class:warn={camera.events.state !== 'subscribed'}></span>
                  {camera.events.state}
                </td>
                <td>
                  {#if camera.clock}
                    <span class="dot" class:ok={!camera.clock.warn} class:warn={camera.clock.warn}></span>
                    {(camera.clock.offset_ms / 1000).toFixed(1)}s
                    {#if !camera.clock.plausible}<span class="hint">(not applied)</span>{/if}
                  {:else}
                    <span class="hint">not measured</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      {#each clockWarnings as camera}
        <p class="note warn">{camera.clock.detail}</p>
      {/each}

      {#if system.go2rtc}
        <p class="hint">
          Live video: {system.go2rtc.state} &middot; {system.go2rtc.streams.length} stream(s).
          {#if system.go2rtc.state !== 'running'}Recording is unaffected.{/if}
        </p>
      {/if}

      {#if system.retention}
        <p class="hint">
          Last retention pass: removed {system.retention.deleted_segments} segment(s),
          freed {gb(system.retention.bytes_freed)}
          {#if system.retention.deleted_by_disk}
            &middot; <strong>{system.retention.deleted_by_disk} deleted early by the disk guard</strong>
          {/if}
        </p>
      {/if}
    </section>

    <!-- Settings -->
    <section class="panel">
      <h2>Retention settings</h2>
      <form class="grid" on:submit|preventDefault={save}>
        <label>
          Keep footage for (days)
          <input class="input" type="number" min="1" bind:value={form.retention_days} />
        </label>
        <label>
          Start deleting at (% full)
          <input class="input" type="number" min="50" max="99" bind:value={form.max_disk_percent} />
        </label>
        <label>
          Delete down to (% full)
          <input class="input" type="number" min="10" max="98" bind:value={form.target_disk_percent} />
        </label>
        <label>
          Saved clip cap (GB)
          <input class="input" type="number" min="1" bind:value={form.max_exports_gb} />
        </label>
      </form>
      <div class="row">
        <button class="button" on:click={save} disabled={saving}>
          {saving ? 'Saving...' : 'Save settings'}
        </button>
        {#if saved}<span class="hint">{saved}</span>{/if}
      </div>
      <p class="hint small">
        The per-camera retention on Nodes overrides the default above. "Delete down to" must be
        below "start deleting at", or the guard would delete continuously once it engages.
      </p>
    </section>

    <section class="panel">
      <h2>Monitoring</h2>
      <p class="hint">
        Prometheus metrics are at <code>/metrics</code>, and the alert rules are in
        <code>deploy/alerts.yml</code> in the repository.
        <strong>The rules are the part that matters:</strong> metrics with nothing watching
        them are how an NVR records nothing for days without anyone noticing.
      </p>
    </section>
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 72rem;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 12px;
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
  section {
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .bar {
    position: relative;
    height: 26px;
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    display: flex;
  }
  .slice {
    height: 100%;
  }
  .slice.c0 {
    background: var(--purple);
  }
  .slice.c1 {
    background: var(--purple-bright);
  }
  .slice.c2 {
    background: var(--lavender);
  }
  .slice.c3 {
    background: var(--orange);
  }
  .slice.c4 {
    background: var(--cyan);
  }
  .marker {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
  }
  .marker.target {
    background: var(--yellow);
  }
  .marker.max {
    background: var(--magenta);
  }

  .scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text-faint);
    white-space: nowrap;
  }
  td {
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td.short {
    color: var(--yellow);
    font-weight: 600;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px;
  }
  .grid label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
    color: var(--text-secondary);
  }
  .row {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .small {
    font-size: 12px;
  }
  code {
    color: var(--cyan);
  }
  .note {
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--magenta);
    border-radius: var(--radius);
    background: var(--tile);
    font-size: 13px;
  }
  .note.warn {
    border-color: var(--yellow);
  }
</style>
