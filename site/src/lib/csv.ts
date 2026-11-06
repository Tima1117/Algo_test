// CSV для выгрузки: разделитель «;», дробная часть через запятую и BOM — так русский Excel
// открывает файл без мастера импорта и читает числа как числа, а не как текст или даты.
export function toCsv(header: string[], rows: (string | number | null | undefined)[][]): string {
  const esc = (v: string | number | null | undefined) => {
    if (v === null || v === undefined) return '';
    const s = typeof v === 'number' ? String(v).replace('.', ',') : String(v);
    return /[";\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return '﻿' + [header, ...rows].map((r) => r.map(esc).join(';')).join('\r\n') + '\r\n';
}

export const csvResponse = (body: string) =>
  new Response(body, { headers: { 'Content-Type': 'text/csv; charset=utf-8' } });
