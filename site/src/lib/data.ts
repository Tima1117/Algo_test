// Данные для страниц читаются во время сборки из ../data (их готовит python -m pif build).
import fs from 'node:fs';
import path from 'node:path';

const DATA_DIR = process.env.PIF_DATA_DIR || path.resolve(process.cwd(), '..', 'data');

function read<T>(rel: string): T {
  return JSON.parse(fs.readFileSync(path.join(DATA_DIR, rel), 'utf-8')) as T;
}

export interface Official { value: number; as_of: string | null; ours: number | null; status: string; url: string | null }

export interface FundSummary {
  rules: string; name: string; full_name: string; type: 'open' | 'interval' | 'exchange'; type_label: string;
  category: string; status: string; reg_date: string; formation_end: string | null;
  uk_name: string; uk_short: string; uk_ogrn: string; uk_inn: string; uk_site: string;
  exchange_tradable: boolean; isin: string | null; ticker: string | null; currency: string;
  page_url: string | null; last_date: string | null; nav: number | null; nav_rub: number | null; price: number | null;
  flow_m: number | null; flow_q: number | null; flow_y: number | null; ret_as_of: string | null;
  ret_1m: number | null; ret_3m: number | null; ret_6m: number | null; ret_1y: number | null;
  official: Record<string, Official>; discrepancy: boolean; n_points: number; first_date: string | null;
  frequency: string | null; corrections: number; anomalies: number; gaps: number;
  data_status: string; data_status_label: string; data_reason: string;
}

export interface Uk {
  inn: string; ogrn: string; name: string; short: string; site: string; funds: number; funds_with_data: number;
  nav_rub: number; flow_m: number; flow_q: number; flow_y: number;
  monthly: { month: string; flow_rub?: number; nav_rub?: number; funds_flow?: number; funds_nav?: number }[];
  statuses: Record<string, number>;
}

export interface Source { id: number; url: string; page: string | null; sha256: string; fetched_at: string; stale: boolean }
export interface MonthRow { month: string; date: string | null; nav: number | null; price: number | null; nav_rub: number | null; flow: number | null; flow_rub: number | null; note: string; src?: number }
export interface Series {
  rules: string; currency: string; frequency: string;
  points: { date: string; nav: number | null; price: number | null; nav_rub: number | null; src: number }[];
  sources: Source[]; monthly: MonthRow[]; corrections: any[]; anomalies: any[]; gaps: any[]; checks: any[];
  payouts?: { date: string; amount: number; source_url: string }[];
}

export const meta = read<any>('site/meta.json');
export const funds = read<FundSummary[]>('site/funds.json');
export const uks = read<Uk[]>('site/uk.json');
export const market = read<any>('site/market.json');
export const quality = read<any>('site/quality.json');
export const corrections = read<any[]>('site/corrections.json');
export const manualCheck = read<any[]>('site/manual_check.json');

export function series(rules: string): Series | null {
  const p = path.join(DATA_DIR, 'series', `${rules}.json`);
  return fs.existsSync(p) ? (JSON.parse(fs.readFileSync(p, 'utf-8')) as Series) : null;
}

export const ukByInn = new Map(uks.map((u) => [u.inn, u]));

// Адрес раздела «Сравнить УК» на algosphere.tech. ИНН передаётся параметром — раздел пока
// его не читает, ссылка ведёт на общий список (см. README).
export const COMPARE_UK_URL = 'https://algosphere.tech/sravnenie/';
