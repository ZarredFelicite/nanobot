import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.NANOBOT_BASE_URL || 'http://127.0.0.1:4096';

  return {
    plugins: [sveltekit()],
    server: {
      host: '0.0.0.0',
      port: 4173,
      allowedHosts: ['web.manticore-lenok.ts.net'],
      proxy: {
        '^/(config|provider|agent|event|global|session|command|skill|lsp|mcp|experimental|formatter|vcs|path|find|file|permission|question|log|instance)(/.*)?$': {
          target,
          changeOrigin: true
        }
      }
    }
  };
});
