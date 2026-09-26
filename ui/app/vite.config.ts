/// <reference types="vitest/config" />
import preact from '@preact/preset-vite';
import { defineConfig, type Plugin } from 'vite';

// fontsource 的 @font-face 同時列 woff2 與 woff；現代瀏覽器都吃 woff2，
// 拿掉 woff 來源讓建置產物（與映像）少一份字型檔。
function woff2Only(): Plugin {
  return {
    name: 'lore-vault-woff2-only',
    enforce: 'pre',
    transform(code, id) {
      if (!id.includes('@fontsource') || !id.split('?')[0]!.endsWith('.css')) return null;
      return code.replace(/,\s*url\([^)]*\.woff\)\s*format\('woff'\)/g, '');
    },
  };
}

// 本機開發：Vite dev server 把 API 轉到本機跑的 Lore Vault 服務（預設 127.0.0.1:8000）。
// 同源 proxy 讓 session cookie 與正式部署（服務自己提供 /ui）行為一致。
const apiTarget = process.env.LORE_VAULT_DEV_API ?? 'http://127.0.0.1:8000';

export default defineConfig({
  base: '/ui/',
  plugins: [woff2Only(), preact()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 不產生 inline script（CSP script-src 'self'）
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
  },
  server: {
    proxy: {
      '/v1': { target: apiTarget },
      '/ui/api': { target: apiTarget },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
