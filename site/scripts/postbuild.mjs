// Сайт живёт в подпапке: корень домена только перенаправляет в раздел.
import { writeFileSync } from 'node:fs';

const base = (process.env.PIF_BASE || '/pif/').replace(/\/?$/, '/');
writeFileSync(new URL('../dist/index.html', import.meta.url),
  `<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=${base}">` +
  `<link rel="canonical" href="${base}"><title>Реестр ПИФов</title><a href="${base}">Реестр ПИФов</a>\n`);
