import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // The container image copies dist/ to /app/static and FastAPI mounts
    // /assets and /fonts from there.
    assetsDir: 'assets',
  },
  server: {
    port: 5173,
    // `npm run dev` proxies the API to a locally running uvicorn.
    proxy: {
      '/api': 'http://127.0.0.1:8080',
    },
  },
});
