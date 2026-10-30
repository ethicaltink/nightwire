<!-- "Add a camera": discovery, add by address, then
     probe and choose which profile records and which feeds the grid. -->
<script>
  import { api, ApiError } from './api.js';

  export let onAdded = () => {};

  let busy = '';
  let error = '';
  let discovery = null;
  let probe = null;

  let address = { host: '', port: 80, username: '', password: '' };
  let chosen = { name: '', id: '', main_profile: '', sub_profile: '', retention_days: 45 };

  async function run(label, action) {
    busy = label;
    error = '';
    try {
      await action();
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      busy = '';
    }
  }

  const discover = () =>
    run('discover', async () => {
      discovery = await api.discover();
    });

  function use(device) {
    address = { ...address, host: device.host, port: device.port || 80 };
    probe = null;
    error = '';
  }

  const runProbe = () =>
    run('probe', async () => {
      probe = await api.probe({
        host: address.host.trim(),
        port: Number(address.port) || 80,
        username: address.username || null,
        password: address.password || null,
      });
      chosen = {
        name: probe.device?.model || address.host,
        id: probe.suggested_id || '',
        main_profile: probe.suggested_main || '',
        sub_profile: probe.suggested_sub || '',
        retention_days: 45,
      };
    });

  const save = () =>
    run('save', async () => {
      await api.addCamera({
        id: chosen.id || null,
        name: chosen.name,
        host: address.host.trim(),
        onvif_port: Number(address.port) || 80,
        username: address.username || null,
        password: address.password || null,
        main_profile: chosen.main_profile || null,
        sub_profile: chosen.sub_profile || null,
        retention_days: Number(chosen.retention_days) || 45,
        model: probe?.device?.model || null,
        firmware: probe?.device?.firmware || null,
        serial: probe?.device?.serial || null,
        event_source_count: probe?.event_source_count ?? null,
      });
      probe = null;
      discovery = null;
      address = { host: '', port: 80, username: '', password: '' };
      onAdded();
    });
</script>

<section class="panel">
  <h2>Add a camera</h2>

  <div class="block">
    <button class="button" on:click={discover} disabled={!!busy}>
      {busy === 'discover' ? 'Searching...' : 'Discover on network (ONVIF)'}
    </button>

    {#if discovery}
      {#if discovery.devices.length === 0}
        <p class="hint">
          Nothing answered. {discovery.note} Add by address below instead.
        </p>
      {:else}
        <ul class="found">
          {#each discovery.devices as device}
            <li>
              <div>
                <strong>{device.name || device.hardware || 'ONVIF device'}</strong>
                <span class="hint mono">
                  {device.host}{device.port && device.port !== 80 ? `:${device.port}` : ''}
                  {device.already_added ? ' · already added' : ' · not added'}
                </span>
              </div>
              <button class="button-quiet" on:click={() => use(device)} disabled={!!busy}>
                Use
              </button>
            </li>
          {/each}
        </ul>
        <p class="hint">{discovery.note}</p>
      {/if}
    {/if}
  </div>

  <div class="block">
    <h3>Or add by address</h3>
    <form class="grid" on:submit|preventDefault={runProbe}>
      <label>IP address<input class="input" bind:value={address.host} placeholder="192.168.1.100" /></label>
      <label>ONVIF port<input class="input" type="number" bind:value={address.port} /></label>
      <label>ONVIF user<input class="input" bind:value={address.username} autocomplete="off" /></label>
      <label>
        Password
        <input class="input" type="password" bind:value={address.password} autocomplete="off" />
      </label>
      <div class="submit">
        <button class="button" type="submit" disabled={!!busy || !address.host.trim()}>
          {busy === 'probe' ? 'Probing...' : 'Probe camera'}
        </button>
      </div>
    </form>
    <p class="hint">
      Discovery only sees cameras on the same network segment, so adding by address always works.
    </p>
  </div>

  {#if error}
    <p class="note bad" role="alert">{error}</p>
  {/if}

  {#if probe}
    <div class="block">
      <p class="found-line">
        Found <strong>{probe.device.manufacturer} {probe.device.model || 'camera'}</strong>
        {#if probe.device.firmware}
          <span class="hint">firmware {probe.device.firmware}</span>
        {/if}
        · {probe.profiles.length} ONVIF profile{probe.profiles.length === 1 ? '' : 's'}
      </p>

      <form class="grid" on:submit|preventDefault={save}>
        <label>Name<input class="input" bind:value={chosen.name} /></label>
        <label>
          Id (used in file paths and URLs)
          <input class="input" bind:value={chosen.id} placeholder="front" />
        </label>
        <label>
          Main stream (recorded)
          <select class="input" bind:value={chosen.main_profile}>
            {#each probe.profiles as profile}
              <option value={profile.token}>{profile.summary}</option>
            {/each}
          </select>
        </label>
        <label>
          Substream (live grid)
          <select class="input" bind:value={chosen.sub_profile}>
            <option value="">None</option>
            {#each probe.profiles as profile}
              <option value={profile.token}>{profile.summary}</option>
            {/each}
          </select>
        </label>
        <label>
          Keep footage for (days)
          <input class="input" type="number" min="1" bind:value={chosen.retention_days} />
        </label>
        <div class="submit">
          <button class="button" type="submit" disabled={!!busy || !chosen.main_profile}>
            {busy === 'save' ? 'Saving...' : 'Save camera'}
          </button>
        </div>
      </form>

      <p class="hint">
        {#if probe.event_source_count}
          {probe.event_source_count} motion event sources found; choose which to use after saving.
        {:else}
          No motion event sources reported. You can still record; set the camera's motion
          detection up in its own web interface first.
        {/if}
        Saving starts recording unless you switch it off below.
      </p>
    </div>
  {/if}
</section>

<style>
  section {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  h2 {
    font-size: 17px;
  }
  h3 {
    font-size: 13px;
    letter-spacing: 1px;
    color: var(--text-secondary);
  }

  .block {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px;
    align-items: end;
  }
  .grid label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
    color: var(--text-secondary);
  }
  .submit {
    display: flex;
    align-items: flex-end;
  }

  .found {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .found li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 12px;
    background: var(--tile);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  .found strong {
    display: block;
    font-size: 14px;
  }
  .found .mono {
    font-size: 12px;
  }

  .found-line {
    margin: 0;
    font-size: 14px;
  }
  .found-line .hint {
    display: inline;
  }

  .note {
    margin: 0;
    padding: 10px 12px;
    border-radius: var(--radius);
    background: var(--tile);
    font-size: 13px;
  }
  .note.bad {
    border: 1px solid var(--magenta);
  }
</style>
