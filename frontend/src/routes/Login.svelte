<!-- Sign in. -->
<script>
  import { createEventDispatcher } from 'svelte';
  import Wordmark from '../lib/Wordmark.svelte';
  import { api, ApiError } from '../lib/api.js';

  const dispatch = createEventDispatcher();

  let password = '';
  let error = '';
  let busy = false;

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    error = '';
    try {
      await api.login(password);
      password = '';
      dispatch('signedin');
    } catch (err) {
      error =
        err instanceof ApiError
          ? err.message
          : 'Could not reach the server. Check that Nightwire is running.';
    } finally {
      busy = false;
    }
  }
</script>

<div class="page">
  <form class="panel" on:submit={submit}>
    <Wordmark size={24} logoSize={44} large={true} />

    <div class="heading">
      <h1>Sign in</h1>
      <p class="hint">Enter the Nightwire password to view cameras.</p>
    </div>

    <div class="field">
      <label for="pw">Password</label>
      <!-- svelte-ignore a11y-autofocus -->
      <input
        id="pw"
        class="input"
        type="password"
        autocomplete="current-password"
        autofocus
        bind:value={password}
        disabled={busy}
      />
    </div>

    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}

    <button class="button" type="submit" disabled={busy}>
      {busy ? 'Signing in...' : 'Sign in'}
    </button>

    <p class="hint small">
      You stay signed in for 30 days on this browser, renewed while a page is open.
    </p>
  </form>
</div>

<style>
  .page {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px var(--gutter);
  }

  form {
    width: 100%;
    max-width: 416px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding: 32px;
  }

  .heading {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  h1 {
    font-size: 20px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  label {
    font-size: 13px;
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--magenta);
    border-radius: var(--radius);
    background: var(--tile);
    color: var(--text);
    font-size: 13px;
  }

  .small {
    font-size: 12px;
  }

  @media (max-width: 480px) {
    form {
      padding: 24px var(--gutter);
    }
  }
</style>
