import { funds, meta } from '../../lib/data';
import { csvResponse, toCsv } from '../../lib/csv';

export function GET() {
  const header = ['номер_правил', 'фонд', 'тип', 'класс_активов', 'категория_цб', 'ук', 'инн_ук', 'isin', 'тикер', 'валюта',
    'дата', 'сча_руб', 'сча_в_валюте', 'пай', `приток_${meta.last_month}_руб`, 'приток_квартал_руб', 'месяцев_в_квартале', 'приток_12м_руб', 'месяцев_в_12м',
    'доходность_1м', 'доходность_3м', 'доходность_6м', 'доходность_1г', 'выплаты', 'расхождение_с_официальной',
    'статус_данных', 'причина', 'страница_ук'];
  const rows = [...funds].sort((a, b) => (b.nav_rub ?? -1) - (a.nav_rub ?? -1)).map((f) => [
    f.rules, f.name, f.type_label, f.asset_class, f.category, f.uk_short, f.uk_inn, f.isin, f.ticker, f.currency,
    f.last_date, f.nav_rub, f.nav, f.price, f.flow_m, f.flow_q, f.flow_q_n, f.flow_y, f.flow_y_n,
    f.ret_1m, f.ret_3m, f.ret_6m, f.ret_1y, f.payouts_note, f.discrepancy ? 'да' : '', f.data_status_label, f.data_reason, f.page_url]);
  return csvResponse(toCsv(header, rows));
}
