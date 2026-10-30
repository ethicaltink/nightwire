<!-- Nodes: the camera list and the add-camera panel. -->
<script>
  import { onMount } from 'svelte';
  import CameraCard from '../lib/CameraCard.svelte';
  import AddCamera from '../lib/AddCamera.svelte';
  import { api, ApiError } from '../lib/api.js';

  let cameras = [];
  let loading = true;
  let error = '';

  async function load() {
    try {
      const result = await api.listCameras();
      cameras = result.cameras;
      error = '';
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  onMount(load);
</script>

<div class="page">
  <header>
    <h1>Nodes</h1>
    <p class="hint">
      {#if loading}
        Loading cameras...
      {:else}
        {cameras.length} camera{cameras.length === 1 ? '' : 's'}. Recording starts as soon as a
        camera is added with recording switched on.
      {/if}
    </p>
  </header>

  {#if error}
    <p class="note" role="alert">{error}</p>
  {/if}

  {#each cameras as camera (camera.id)}
    <CameraCard {camera} onChanged={load} />
  {/each}

  {#if !loading && cameras.length === 0}
    <p class="hint empty">
      No cameras yet. Discover them on the network, or add one by address below.
    </p>
  {/if}

  <AddCamera onAdded={load} />
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 72rem;
  }

  header {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  h1 {
    font-size: 20px;
  }

  .note {
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--magenta);
    border-radius: var(--radius);
    background: var(--tile);
    font-size: 13px;
  }

  .empty {
    padding: 16px;
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-lg);
  }
</style>
