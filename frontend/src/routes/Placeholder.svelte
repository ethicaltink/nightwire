<!-- An empty page that says why it is empty. Used for unknown routes. -->
<script>
  export let title = '';
  export let blurb = '';
</script>

<section class="panel">
  <h1>{title}</h1>
  {#if blurb}<p class="hint">{blurb}</p>{/if}
</section>

<style>
  section {
    max-width: 44rem;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  strong {
    color: var(--text-secondary);
  }
</style>
