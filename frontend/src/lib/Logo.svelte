<!-- The winking panda, an inline SVG with
     literal hex swapped for tokens. Geometry is unchanged. -->
<script>
  export let size = 32;
  export let large = false;
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 32 32"
  aria-hidden="true"
  style="filter: {large ? 'var(--drop-glow-cyan-lg)' : 'var(--drop-glow-cyan)'}"
>
  <circle cx="8" cy="8.5" r="3.6" fill="var(--cyan)" />
  <circle cx="24" cy="8.5" r="3.6" fill="var(--cyan)" />
  <circle cx="16" cy="18" r="11" fill="var(--panel)" stroke="var(--cyan)" stroke-width="1.8" />
  <ellipse cx="11.4" cy="16.8" rx="3" ry="3.9" transform="rotate(25 11.4 16.8)" fill="var(--cyan)" />
  <ellipse cx="20.6" cy="16.8" rx="3" ry="3.9" transform="rotate(-25 20.6 16.8)" fill="var(--cyan)" />
  <circle cx="11.8" cy="16.6" r="1.25" fill="var(--panel)" />
  <path d="M19 17.4Q20.6 15.4 22.2 17.4" fill="none" stroke="var(--panel)" stroke-width="1.4" stroke-linecap="round" />
  <ellipse cx="16" cy="21.6" rx="1.6" ry="1.1" fill="var(--cyan)" />
  <path d="M13.8 23.8Q16 25.6 18.6 23.4" fill="none" stroke="var(--cyan)" stroke-width="1.4" stroke-linecap="round" />
  <circle cx="24" cy="22" r="1.4" fill="var(--motion-magenta)" />
</svg>
