<!-- One camera on the Nodes page. Matches the design mockups,
     including the "Motion event sources" panel behind its own button. -->
<script>
  import { onDestroy } from 'svelte';
  import { api, ApiError } from './api.js';
  import EventSources from './EventSources.svelte';

  export let camera;
  export let onChanged = () => {};

  const SNAPSHOT_REFRESH_MS = 15000;
  const STATUS_REFRESH_MS = 10000;

  let snapshotUrl = api.snapshotUrl(camera.id);
  let snapshotFailed = false;
  let busy = '';
  let message = '';
  let messageKind = 'info';
  let editing = false;
  let showLog = false;
  let showSources = false;
  let status = null;

  let form = {};

  const refresh = setInterval(() => {
    if (!snapshotFailed) snapshotUrl = api.snapshotUrl(camera.id);
  }, SNAPSHOT_REFRESH_MS);

  // Poll recorder state so the card shows what is actually being written,
  // not just what the last probe said.
  async function pollStatus() {
    try {
      status = await api.cameraStatus(camera.id);
    } catch {
      status = null;
    }
  }
  pollStatus();
  const statusPoll = setInterval(pollStatus, STATUS_REFRESH_MS);

  onDestroy(() => {
    clearInterval(refresh);
    clearInterval(statusPoll);
  });

  function say(text, kind = 'info') {
    message = text;
    messageKind = kind;
  }

  function startEdit() {
    form = {
      name: camera.name,
      host: camera.host,
      onvif_port: camera.onvif_port,
      username: camera.username ?? '',
      password: '',
      retention_days: camera.retention_days,
    };
    editing = true;
    message = '';
  }

  async function run(label, action) {
    busy = label;
    message = '';
    try {
      await action();
    } catch (err) {
      say(err instanceof ApiError ? err.message : String(err), 'bad');
    } finally {
      busy = '';
    }
  }

  const save = () =>
    run('save', async () => {
      const body = { ...form };
      if (!body.password) delete body.password;
      await api.patchCamera(camera.id, body);
      editing = false;
      say('Saved.', 'ok');
      onChanged();
    });

  const test = () =>
    run('test', async () => {
      const result = await api.testCamera(camera.id);
      const device = result.probe?.device;
      say(
        device?.model
          ? `Reachable. ${device.manufacturer} ${device.model}, firmware ${device.firmware || 'unknown'}.`
          : 'Reachable.',
        'ok',
      );
      snapshotFailed = false;
      snapshotUrl = api.snapshotUrl(camera.id);
      onChanged();
    });

  const toggleRecord = () =>
    run('record', async () => {
      await api.patchCamera(camera.id, { record: !camera.record });
      onChanged();
    });

  const remove = () =>
    run('delete', async () => {
      const keep = window.confirm(
        `Remove ${camera.name}?\n\nOK keeps the recorded footage on disk.\nCancel aborts.`,
      );
      if (!keep) return;
      const alsoFootage = window.confirm(
        `Also delete ${camera.name}'s recorded footage?\n\nOK deletes it permanently.\nCancel keeps it.`,
      );
      await api.deleteCamera(camera.id, alsoFootage);
      onChanged();
    });

  const loadStatus = () =>
    run('log', async () => {
      await pollStatus();
      showLog = true;
    });

  // The recorder is the authority on whether footage is being written; the
  // probe result only says whether ONVIF answered.
  const RECORDER_LABELS = {
    recording: 'Recording',
    reconnecting: 'Reconnecting',
    error: 'Recorder error',
    disabled: 'Not recording',
    stopped: 'Recorder stopped',
    'not-started': 'Recorder not built yet',
  };
  const RECORDER_DOTS = {
    recording: 'ok',
    reconnecting: 'warn',
    error: 'bad',
    disabled: '',
    stopped: 'warn',
    'not-started': 'warn',
  };

  $: recorderState = status?.recorder?.state ?? null;
  $: stateDot = recorderState
    ? RECORDER_DOTS[recorderState] ?? 'warn'
    : camera.record === false
      ? ''
      : camera.last_error
        ? 'bad'
        : camera.last_probe_at
          ? 'ok'
          : 'warn';
  $: stateLabel = recorderState
    ? (RECORDER_LABELS[recorderState] ?? recorderState)
    : camera.record === false
      ? 'Not recording'
      : camera.last_error
        ? 'Unreachable'
        : 'Added, recorder pending';
  $: identity = [camera.host, camera.model, camera.firmware].filter(Boolean).join(' · ');
  $: keeps = [
    `Keeps ${camera.retention_days} days`,
    camera.event_source_count != null
      ? `${camera.event_source_count} event source${camera.event_source_count === 1 ? '' : 's'}`
      : null,
  ]
    .filter(Boolean)
    .join(' · ');
</script>

<article class="panel">
  <div class="top">
    <div class="shot">
      {#if snapshotFailed}
        <span class="hint">No snapshot</span>
      {:else}
        <img
          src={snapshotUrl}
          alt="Live snapshot from {camera.name}"
          on:error={() => (snapshotFailed = true)}
        />
      {/if}
    </div>

    <div class="body">
      <header>
        <h2>{camera.name}</h2>
        <span class="pill"><span class="dot {stateDot}"></span>{stateLabel}</span>
        <code class="mono id">{camera.id}</code>
      </header>

      <p class="hint">{identity}</p>

      <p class="hint streams">
        {#if camera.main_stream}
          <strong>Main</strong> {camera.main_stream.summary}
        {:else}
          <strong>Main</strong> <span class="mono">{camera.main_uri}</span>
        {/if}
        {#if camera.sub_stream}
          &nbsp;|&nbsp; <strong>Sub</strong> {camera.sub_stream.summary}
        {:else if camera.sub_uri}
          &nbsp;|&nbsp; <strong>Sub</strong> <span class="mono">{camera.sub_uri}</span>
        {/if}
      </p>

      <p class="hint">{keeps}</p>

      {#if status?.recorder}
        <p class="hint">
          {status.recorder.detail}
          {#if status.recorder.last_segment_at}
            &middot; last segment <span class="mono">{status.recorder.last_segment_at}</span>
          {/if}
          {#if status.recorder.watchdog_kills}
            &middot; {status.recorder.watchdog_kills} watchdog restart{status.recorder
              .watchdog_kills === 1
              ? ''
              : 's'}
          {/if}
        </p>
      {/if}

      {#if camera.last_error}
        <p class="note bad">{camera.last_error}</p>
      {/if}
      {#if message}
        <p class="note {messageKind}">{message}</p>
      {/if}

      <div class="actions">
        <button class="button-quiet" on:click={startEdit} disabled={!!busy}>Edit</button>
        <button class="button-quiet" on:click={test} disabled={!!busy}>
          {busy === 'test' ? 'Testing...' : 'Test connection'}
        </button>
        <button class="button-quiet" on:click={() => (showSources = !showSources)} disabled={!!busy}>
          Event sources
        </button>
        <button class="button-quiet" on:click={loadStatus} disabled={!!busy}>Recorder log</button>
        <button class="button-quiet danger" on:click={remove} disabled={!!busy}>Remove</button>
        <label class="toggle">
          <input
            type="checkbox"
            checked={camera.record}
            on:change={toggleRecord}
            disabled={!!busy}
          />
          Record
        </label>
      </div>
    </div>
  </div>

  {#if showSources}
    <EventSources {camera} />
  {/if}

  {#if editing}
    <form class="edit" on:submit|preventDefault={save}>
      <div class="grid">
        <label>Name<input class="input" bind:value={form.name} /></label>
        <label>IP address<input class="input" bind:value={form.host} /></label>
        <label>ONVIF port<input class="input" type="number" bind:value={form.onvif_port} /></label>
        <label>ONVIF user<input class="input" bind:value={form.username} /></label>
        <label>
          Password
          <input
            class="input"
            type="password"
            placeholder={camera.has_password ? 'unchanged' : 'none set'}
            bind:value={form.password}
          />
        </label>
        <label>
          Keep footage for (days)
          <input class="input" type="number" min="1" bind:value={form.retention_days} />
        </label>
      </div>
      <div class="actions">
        <button class="button" type="submit" disabled={!!busy}>
          {busy === 'save' ? 'Saving...' : 'Save changes'}
        </button>
        <button class="button-quiet" type="button" on:click={() => (editing = false)}>Cancel</button>
      </div>
    </form>
  {/if}

  {#if showLog}
    <div class="log">
      <div class="log-head">
        <h3>Recorder log</h3>
        <button class="button-quiet" on:click={() => (showLog = false)}>Close</button>
      </div>
      <p class="hint">
        {status?.recorder?.detail ?? 'No recorder state.'}
        {#if status?.recorder?.restarts}
          &middot; {status.recorder.restarts} restart{status.recorder.restarts === 1 ? '' : 's'}
        {/if}
      </p>
      {#if status?.recorder?.stderr_tail?.length}
        <pre class="mono">{status.recorder.stderr_tail.join('\n')}</pre>
      {:else}
        <p class="hint">
          Nothing to show yet: the last 50 ffmpeg stderr lines appear here once the recorder has
          something to say.
        </p>
      {/if}
      {#if status?.last_probe_at}
        <p class="hint mono">Last probe {status.last_probe_at}</p>
      {/if}
    </div>
  {/if}
</article>

<style>
  article {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .top {
    display: flex;
    gap: 16px;
  }

  .shot {
    flex: 0 0 240px;
    aspect-ratio: 16 / 9;
    background: var(--tile);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .shot img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  .body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
  }
  h2 {
    font-size: 17px;
  }
  h3 {
    font-size: 14px;
  }
  .id {
    font-size: 12px;
    color: var(--text-faint);
  }

  .streams strong {
    color: var(--text-secondary);
    font-weight: 600;
  }
  .streams .mono {
    font-size: 12px;
    word-break: break-all;
  }

  .note {
    margin: 2px 0 0;
    padding: 8px 10px;
    border-radius: var(--radius);
    background: var(--tile);
    font-size: 13px;
  }
  .note.bad {
    border: 1px solid var(--magenta);
  }
  .note.ok {
    border: 1px solid var(--green);
  }
  .note.info {
    border: 1px solid var(--border);
  }

  .actions {
    margin-top: 4px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
  }
  .danger:hover {
    color: var(--magenta);
  }

  .toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: var(--touch);
    font-size: 13px;
    color: var(--text-secondary);
  }

  .edit {
    border-top: 1px solid var(--border);
    padding-top: 14px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
  }
  .grid label,
  .edit label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .log {
    border-top: 1px solid var(--border);
    padding-top: 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .log-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  pre {
    margin: 0;
    padding: 10px;
    background: var(--ground);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font-size: 12px;
    overflow-x: auto;
  }

  @media (max-width: 760px) {
    .top {
      flex-direction: column;
    }
    .shot {
      flex: none;
      width: 100%;
    }
  }
</style>
