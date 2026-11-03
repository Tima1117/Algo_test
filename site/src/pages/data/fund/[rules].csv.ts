import { funds, series } from '../../../lib/data';
import { csvResponse, toCsv } from '../../../lib/csv';

export function getStaticPaths() {
  return funds.filter((f) => f.n_points > 0).map((f) => ({ params: { rules: f.rules } }));
}

export function GET({ params }: { params: { rules: string } }) {
  const s = series(params.rules)!;
  const header = ['дата', `сча_${s.currency}`, `пай_${s.currency}`, 'сча_руб', 'источник', 'страница_ук', 'получено', 'sha256'];
  const rows = s.points.map((p) => {
    const src = s.sources[p.src];
    return [p.date, p.nav, p.price, p.nav_rub, src.url, src.page, src.fetched_at, src.sha256];
  });
  return csvResponse(toCsv(header, rows));
}
