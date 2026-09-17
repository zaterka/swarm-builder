import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Swarm Builder web app (PLAN.md Group 6). In dev, /api is proxied to the
// FastAPI server so the browser never needs a cross-origin request (and
// therefore never needs a CORS preflight for the non-safelisted
// `Last-Event-ID` header the SSE client sends on reconnect). In
// production, `main.py` serves `web/dist` same-origin and this proxy is
// irrelevant.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8420',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./test/setup.ts'],
    globals: true,
  },
});
