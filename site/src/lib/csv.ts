// CSV для выгрузки: разделитель «;» и BOM, чтобы русский Excel открыл без мастера импорта.
export function toCsv(header: string[], rows: (string | number | null | undefined)[][]): string {
  const esc = (v: string | number | null | undefined) => {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[";\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return '﻿' + [header, ...rows].map((r) => r.map(esc).join(';')).join('\r\n') + '\r\n';
}

export const csvResponse = (body: string) =>
  new Response(body, { headers: { 'Content-Type': 'text/csv; charset=utf-8' } });
