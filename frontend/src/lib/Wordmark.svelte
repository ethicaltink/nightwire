<script>
  import Logo from './Logo.svelte';

  export let size = 20;
  export let logoSize = 32;
  export let large = false;
</script>

<div class="wordmark">
  <Logo size={logoSize} {large} />
  <span style="font-size: {size}px">Nightwire</span>
</div>

<style>
  .wordmark {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  span {
    font-family: var(--font-head);
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--cyan);
    text-shadow: var(--text-glow-cyan);
  }
</style>
