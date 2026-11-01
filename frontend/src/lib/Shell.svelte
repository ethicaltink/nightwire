<!-- Page chrome: header, nav, status pill, clock. -->
<script>
  import { onMount, onDestroy } from 'svelte';
  import Wordmark from './Wordmark.svelte';
  import { api } from './api.js';
  import { link, location } from './router.js';

  export let onSignOut = () => {};

  const nav = [
    { label: 'Feeds', href: '/live', route: 'feeds' },
    { label: 'Rewind', href: '/playback', route: 'rewind' },
    { label: 'Vault', href: '/exports', route: 'vault' },
    { label: 'Nodes', href: '/cameras', route: 'nodes' },
    { label: 'Core', href: '/system', route: 'core' },
  ];

  let clock = '';
  let health = null;
  let clockTimer;
  let healthTimer;

  function tick() {
    const now = new Date();
    const date = now.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    });
    clock = `${date} · ${now.toLocaleTimeString(undefined, { hour12: false })}`;
  }

  async function pollHealth() {
    try {
      health = await api.health();
    } catch {
      health = null;
    }
  }

  onMount(() => {
    tick();
    pollHealth();
    clockTimer = setInterval(tick, 1000);
    healthTimer = setInterval(pollHealth, 15000);
  });

  onDestroy(() => {
    clearInterval(clockTimer);
    clearInterval(healthTimer);
  });

  // Reads the health report rather than inventing a count. The
  // detail is the supervisor's own "N of M recording", and `degraded` means
  // at least one camera needs attention without the process being unhealthy.
  $: recorders = health?.components?.recorders;
  // Including when idle: the supervisor distinguishes "no cameras at all"
  // from "cameras configured, none recording", and the chip should too.
  $: statusText =
    health === null ? 'Server unreachable' : (recorders?.detail ?? 'Unknown');
  $: statusDot =
    health === null
      ? 'bad'
      : recorders?.state === 'idle'
        ? ''
        : recorders?.state === 'recording'
          ? 'ok'
          : recorders?.state === 'degraded'
            ? 'warn'
            : recorders?.ok
              ? 'ok'
              : 'bad';
</script>

<div class="shell">
  <header>
    <Wordmark />

    <nav aria-label="Main">
      {#each nav as item}
        <a
          href={item.href}
          use:link
          class:current={$location.name === item.route}
          aria-current={$location.name === item.route ? 'page' : undefined}
        >
          {item.label}
        </a>
      {/each}
    </nav>

    <div class="right">
      <span class="pill"><span class="dot {statusDot}"></span>{statusText}</span>
      <span class="mono clock">{clock}</span>
      <!-- A new tab, deliberately: the station is a kiosk view with no
           navigation, so closing the tab is the obvious way back. -->
      <a href="/station" class="button" target="_blank" rel="noopener">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <path d="M8 21h8M12 17v4" />
        </svg>
        Open station
      </a>
      <button class="button-quiet" on:click={onSignOut}>Sign out</button>
    </div>
  </header>

  <main>
    <slot />
  </main>
</div>

<style>
  .shell {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 32px;
    padding: 12px var(--gutter);
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }

  nav {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  nav a {
    display: inline-flex;
    align-items: center;
    height: var(--touch);
    padding: 0 14px;
    border-radius: var(--radius);
    color: var(--text-secondary);
    text-decoration: none;
    font-weight: 500;
  }

  nav a:hover {
    color: var(--text);
  }

  nav a.current {
    background: var(--border);
    color: var(--text);
    font-weight: 600;
    box-shadow: inset 0 -2px 0 var(--cyan);
  }

  .right {
    margin-left: auto;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }

  .clock {
    font-size: 13px;
    color: var(--text-secondary);
  }

  main {
    flex: 1;
    padding: 24px var(--gutter);
  }

  @media (max-width: 760px) {
    header {
      gap: 8px 16px;
      padding: 12px var(--gutter);
    }
    .right {
      margin-left: 0;
      width: 100%;
    }
    .clock {
      order: 3;
    }
  }
</style>
