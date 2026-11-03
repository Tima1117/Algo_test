// Форматирование чисел и ссылок. Используется и при сборке, и в браузере.
const nf = (d: number) => new Intl.NumberFormat('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });

export function money(v: number | null | undefined, cur = '₽'): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e9) return `${nf(1).format(v / 1e9)} млрд ${cur}`;
  if (a >= 1e6) return `${nf(1).format(v / 1e6)} млн ${cur}`;
  if (a >= 1e3) return `${nf(0).format(v / 1e3)} тыс ${cur}`;
  return `${nf(0).format(v)} ${cur}`;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—';
  return nf(digits).format(v);
}

export function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return `${v > 0 ? '+' : ''}${nf(2).format(v)}%`;
}

export function sign(v: number | null | undefined): string {
  if (v === null || v === undefined) return '';
  return v > 0 ? 'pos' : v < 0 ? 'neg' : '';
}

export function date(d: string | null | undefined): string {
  if (!d) return '—';
  const [y, m, day] = d.slice(0, 10).split('-');
  return `${day}.${m}.${y}`;
}

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
export function month(key: string): string {
  const [y, m] = key.split('-');
  return `${MONTHS[Number(m) - 1]} ${y}`;
}

export function host(url: string | null | undefined): string {
  if (!url) return '';
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}
