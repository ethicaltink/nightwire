<!-- The "Motion event sources" panel on Nodes.
     The camera is asked what it emits; nothing here knows a topic string. -->
<script>
  import { api, ApiError } from './api.js';

  export let camera;

  let sources = [];
  let listener = null;
  let topics = null;
  let chosen = new Map(); // topic -> { label, color, kind }
  let busy = '';
  let message = '';
  let kind = 'info';

  const PALETTE = [
    { name: 'Motion magenta', value: '#FF2BD6' },
    { name: 'Orange', value: '#FF8A1E' },
    { name: 'Lavender', value: '#C9A6FF' },
    { name: 'Green', value: '#39FF88' },
    { name: 'Yellow', value: '#FCEE0A' },
    { name: 'Cyan', value: '#7DF9FF' },
  ];

  function say(text, level = 'info') {
    message = text;
    kind = level;
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

  const load = () =>
    run('load', async () => {
      const result = await api.eventSources(camera.id);
      sources = result.sources;
      listener = result.listener;
      chosen = new Map(
        sources
          .filter((source) => source.enabled)
          .map((source) => [
            source.topic,
            { label: source.label, color: source.color, kind: source.kind },
          ]),
      );
    });

  const rescan = () =>
    run('rescan', async () => {
      const result = await api.eventTopics(camera.id);
      topics = result.topics;
      if (result.count === 0) say('The camera reported no event topics.', 'warn');
    });

  function toggle(topic) {
    const next = new Map(chosen);
    if (next.has(topic.topic)) {
      next.delete(topic.topic);
    } else {
      next.set(topic.topic, {
        label: topic.label,
        color: PALETTE[next.size % PALETTE.length].value,
        kind: topic.kind,
      });
    }
    chosen = next;
  }

  function edit(topicPath, field, value) {
    const entry = chosen.get(topicPath);
    if (!entry) return;
    chosen = new Map(chosen).set(topicPath, { ...entry, [field]: value });
  }

  const save = () =>
    run('save', async () => {
      const payload = [...chosen.entries()].map(([topic, entry]) => ({
        topic,
        label: entry.label,
        color: entry.color,
        kind: entry.kind,
      }));
      const result = await api.saveEventSources(camera.id, payload);
      sources = result.sources;
      say(`Saved ${payload.length} source${payload.length === 1 ? '' : 's'}.`, 'ok');
      await load();
    });

  load();

  $: rows = topics ?? sources.map((source) => ({
      topic: source.topic,
      label: source.label,
      kind: source.kind,
      shows_as: source.kind === 'span' ? 'Block (on/off)' : 'Diamond (one-off)',
      already_chosen: Boolean(source.enabled),
    }));
</script>

<section class="panel">
  <header>
    <h3>Motion event sources</h3>
    {#if listener}
      <span class="pill">
        <span
          class="dot"
          class:ok={listener.state === 'subscribed'}
          class:warn={listener.state === 'reconnecting' || listener.state === 'no sources'}
          class:bad={listener.state === 'unavailable'}
        ></span>
        Listener {listener.state}
        {#if listener.last_event_at}
          &middot; last event {new Date(listener.last_event_ms).toLocaleTimeString()}
        {/if}
      </span>
    {/if}
    <button class="button-quiet" on:click={rescan} disabled={!!busy}>
      {busy === 'rescan' ? 'Asking the camera...' : 'Rescan events'}
    </button>
  </header>

  {#if message}
    <p class="note {kind}">{message}</p>
  {/if}

  {#if rows.length === 0}
    <p class="hint">
      No event sources yet. Press <strong>Rescan events</strong> to ask the camera which ONVIF
      events it emits.
    </p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Use</th>
            <th>Found on camera</th>
            <th>Timeline label</th>
            <th>Color</th>
            <th>Shows as</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.topic)}
            {@const entry = chosen.get(row.topic)}
            <tr class:on={!!entry}>
              <td>
                <input
                  type="checkbox"
                  checked={!!entry}
                  on:change={() => toggle(row)}
                  aria-label="Use {row.label}"
                />
              </td>
              <td class="mono topic" title={row.topic}>{row.label}</td>
              <td>
                {#if entry}
                  <input
                    class="input small"
                    value={entry.label}
                    on:input={(e) => edit(row.topic, 'label', e.currentTarget.value)}
                  />
                {:else}
                  <span class="hint">&mdash;</span>
                {/if}
              </td>
              <td>
                {#if entry}
                  <select
                    class="input small"
                    value={entry.color}
                    on:change={(e) => edit(row.topic, 'color', e.currentTarget.value)}
                  >
                    {#each PALETTE as swatch}
                      <option value={swatch.value}>{swatch.name}</option>
                    {/each}
                  </select>
                {:else}
                  <span class="hint">&mdash;</span>
                {/if}
              </td>
              <td class="hint">{row.shows_as}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <p class="hint">
      Found by asking the camera which ONVIF events it emits. Ticked sources become lanes on the
      Rewind timeline. Zones and lines are set up in the camera's own web page.
    </p>

    <div class="actions">
      <button class="button" on:click={save} disabled={!!busy}>
        {busy === 'save' ? 'Saving...' : 'Save sources'}
      </button>
      <span class="hint">
        Unticking a source that already has events disables it rather than deleting it, so the
        markers already on the timeline survive.
      </span>
    </div>
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
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
  }
  h3 {
    font-size: 13px;
    letter-spacing: 1px;
    color: var(--text-secondary);
    margin-right: auto;
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
    vertical-align: middle;
  }
  tr.on td {
    background: var(--tile);
  }
  .topic {
    font-size: 12px;
    max-width: 18rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .input.small {
    height: 32px;
    font-size: 13px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
  }
  .note {
    margin: 0;
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
  .note.warn {
    border: 1px solid var(--yellow);
  }
  .note.info {
    border: 1px solid var(--border);
  }
</style>
