import { defineConfig } from 'vite';
import { resolve } from 'node:path';

export default defineConfig({
  root: resolve('.'),
  publicDir: false,
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: resolve('index.html'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8100',
      '/ws': { target: 'ws://127.0.0.1:8100', ws: true },
      '/health': 'http://127.0.0.1:8100',
    },
  },
});
