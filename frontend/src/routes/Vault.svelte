<!-- Vault: saved clips.
     Clips stay until deleted here; retention never removes them. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { api, ApiError } from '../lib/api.js';
  import { link } from '../lib/router.js';
  import { formatDuration, formatMoment } from '../lib/timescale.js';

  const POLL_MS = 3000;

  let exports = [];
  let status = null;
  let error = '';
  let loading = true;
  let busy = '';
  let timer;

  async function load() {
    try {
      const result = await api.listExports();
      exports = result.exports;
      status = result.status;
      error = '';
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function remove(item) {
    if (!window.confirm(`Delete ${item.name}?\n\nThis cannot be undone.`)) return;
    busy = item.id;
    try {
      await api.deleteExport(item.id);
      await load();
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      busy = '';
    }
  }

  function when(item) {
    return formatMoment(item.start_ts).replace('T', ' ');
  }

  function size(item) {
    if (!item.size_bytes) return '—';
    return `${(item.size_bytes / 1_000_000).toFixed(0)} MB`;
  }

  function rewindLink(item) {
    return `/playback/${item.camera_id}?t=${formatMoment(item.start_ts)}`;
  }

  onMount(() => {
    load();
    // Poll while anything is queued or running, so progress shows up.
    timer = setInterval(() => {
      if (!status || status.queued > 0 || status.running) load();
    }, POLL_MS);
  });
  onDestroy(() => clearInterval(timer));

  $: storeUsed = status ? (status.store_bytes / 1_000_000).toFixed(0) : null;
  $: nearlyFull = status && status.store_percent >= 90;
</script>

<div class="page">
  <header>
    <h1>Vault</h1>
    <p class="hint">
      Saved clips stay until you delete them; retention never removes them.
    </p>
    {#if storeUsed !== null}
      <span class="pill" class:warn={nearlyFull}>
        <span class="dot" class:warn={nearlyFull} class:ok={!nearlyFull}></span>
        {storeUsed} MB in saved clips
        {#if status.store_cap_bytes}
          &middot; {status.store_percent}% of the cap
        {/if}
      </span>
    {/if}
  </header>

  {#if error}
    <p class="note" role="alert">{error}</p>
  {/if}

  {#if nearlyFull}
    <p class="note warn">
      The export store is nearly full. New clips will be refused once it is, rather than
      footage being deleted to make room.
    </p>
  {/if}

  {#if !loading && exports.length === 0}
    <p class="hint empty">
      No saved clips yet. Mark in and out points on <a href="/playback" use:link>Rewind</a> with
      <code>[</code> and <code>]</code>, then press Export.
    </p>
  {:else}
    <div class="scroll panel">
      <table>
        <thead>
          <tr>
            <th>Camera</th>
            <th>Starts</th>
            <th>Length</th>
            <th>Size</th>
            <th>Cut</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each exports as item (item.id)}
            <tr>
              <td>{item.camera_id}</td>
              <td class="mono">{when(item)}</td>
              <td>{formatDuration(item.duration_ms ?? 0)}</td>
              <td>{size(item)}</td>
              <td class="hint">{item.precise ? 'Precise' : 'Keyframe'}</td>
              <td>
                {#if item.status === 'done'}
                  <span class="state ok">Ready</span>
                {:else if item.status === 'running'}
                  <span class="state run">Exporting...</span>
                {:else if item.status === 'queued'}
                  <span class="state run">Queued</span>
                {:else}
                  <span class="state bad" title={item.error ?? ''}>Failed</span>
                {/if}
                {#if item.has_gaps}
                  <span class="hint gap" title="The clip spans a gap in the recording">
                    has a gap
                  </span>
                {/if}
              </td>
              <td class="actions">
                {#if item.download_url}
                  <a class="chip" href={item.download_url} download>Download</a>
                {/if}
                <a class="chip" href={rewindLink(item)} use:link>Open in Rewind</a>
                <button
                  class="chip danger"
                  on:click={() => remove(item)}
                  disabled={busy === item.id || item.status === 'running'}
                  title={item.status === 'running' ? 'Wait for it to finish' : 'Delete this clip'}
                >
                  Delete
                </button>
              </td>
            </tr>
            {#if item.status === 'failed' && item.error}
              <tr class="why">
                <td colspan="7" class="hint">{item.error}</td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>

    <p class="hint">
      File names follow <code>camera_date-time_length.mp4</code>, for example
      <code>drive_20261026-180410_6m00s.mp4</code>.
    </p>
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-width: 72rem;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 16px;
  }
  h1 {
    font-size: 20px;
  }
  header .hint {
    margin-right: auto;
  }
  .pill.warn {
    border-color: var(--yellow);
  }
  .scroll {
    overflow-x: auto;
    padding: 4px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    padding: 8px;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text-faint);
    white-space: nowrap;
  }
  td {
    padding: 8px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  tr.why td {
    border-bottom: 1px solid var(--border);
    padding-top: 0;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .state {
    font-weight: 600;
  }
  .state.ok {
    color: var(--green);
  }
  .state.run {
    color: var(--yellow);
  }
  .state.bad {
    color: var(--magenta);
  }
  .gap {
    margin-left: 6px;
    color: var(--orange);
  }
  .chip {
    display: inline-flex;
    align-items: center;
    min-height: 30px;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--panel);
    color: var(--text-secondary);
    font: 500 12px var(--font-body);
    text-decoration: none;
    cursor: pointer;
  }
  .chip:hover {
    color: var(--text);
  }
  .chip.danger:hover {
    color: var(--magenta);
  }
  .chip:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  code {
    color: var(--cyan);
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
  .empty {
    padding: 16px;
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-lg);
  }
</style>
