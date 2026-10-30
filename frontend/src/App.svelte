<script>
  import { onMount } from 'svelte';
  import Shell from './lib/Shell.svelte';
  import Login from './routes/Login.svelte';
  import Placeholder from './routes/Placeholder.svelte';
  import Station from './routes/Station.svelte';
  import Nodes from './routes/Nodes.svelte';
  import Feeds from './routes/Feeds.svelte';
  import Rewind from './routes/Rewind.svelte';
  import Vault from './routes/Vault.svelte';
  import Core from './routes/Core.svelte';
  import { api } from './lib/api.js';
  import { location, navigate, HOME } from './lib/router.js';

  // null while the session check is in flight, so nothing flashes.
  let authenticated = null;
  let sessionTimer;

  async function checkSession() {
    try {
      const result = await api.session();
      authenticated = result.authenticated;
    } catch {
      authenticated = false;
    }
  }

  onMount(() => {
    checkSession();
    // Touching the session endpoint hourly is what renews the 30-day cookie
    // on a station tab left open for weeks.
    sessionTimer = setInterval(checkSession, 3600 * 1000);
    return () => clearInterval(sessionTimer);
  });

  function onSignedIn() {
    authenticated = true;
    if ($location.name === 'login') navigate(HOME, { replace: true });
  }

  async function onSignOut() {
    try {
      await api.logout();
    } finally {
      authenticated = false;
      navigate('/login');
    }
  }

  // Every route below has a page. This map stays as the slot an added
  // page would use, and Placeholder is what an unknown route renders.
  const pending = {};
</script>

{#if authenticated === null}
  <div class="boot" aria-busy="true"></div>
{:else if !authenticated}
  <Login on:signedin={onSignedIn} />
{:else if $location.name === 'station'}
  <Station />
{:else if $location.name === 'notfound'}
  <Shell {onSignOut}>
    <Placeholder title="Not found" blurb={`No page at ${$location.path}.`} />
  </Shell>
{:else}
  <Shell {onSignOut}>
    {#if $location.name === 'feeds'}
      <Feeds />
    {:else if $location.name === 'rewind'}
      <!-- Keyed so switching camera through a deep link remounts cleanly. -->
      {#key $location.param}
        <Rewind />
      {/key}
    {:else if $location.name === 'vault'}
      <Vault />
    {:else if $location.name === 'core'}
      <Core />
    {:else if $location.name === 'nodes'}
      <Nodes />
    {:else if pending[$location.name]}
      <Placeholder {...pending[$location.name]} />
    {/if}
  </Shell>
{/if}

<style>
  .boot {
    min-height: 100vh;
    background: var(--ground);
  }
</style>
