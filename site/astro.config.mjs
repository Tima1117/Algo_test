// Статическая сборка раздела /pif/. Базовый путь меняется переменной PIF_BASE.
import { defineConfig } from 'astro/config';

const base = (process.env.PIF_BASE || '/pif/').replace(/\/?$/, '/');

export default defineConfig({
  site: process.env.PIF_SITE || 'https://algo-test-pif.vercel.app',
  base,
  trailingSlash: 'always',
  outDir: './dist' + base,
  build: { format: 'directory', assets: '_assets' },
  compressHTML: true,
  devToolbar: { enabled: false },
});
