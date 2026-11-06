"""ПАО «УК «Арсагера»: СЧА и стоимость пая — публичный JSON-API УК «Метрики фонда».

Источник (документация УК: https://arsagera.ru/files/arsagera_metriki_fonda.pdf, «API является
публичным и не требует аутентификации»):
* https://arsagera.ru/api/v1/funds/<код>/fund-metrics/?from=YYYY-MM-DD&to=YYYY-MM-DD
  -> {"data": [{"date", "nav_per_share", "total_net_assets"}]}: цена пая и СЧА в рублях, по рабочим дням
  (в старой истории — и по выходным). Коды фондов — из документации: fa, f4si, f64, fo.
Сопоставление: по названию фонда из документации API и названию в кавычках из полного названия
в реестре ЦБ (номер правил API не показывает).

Особенности сайта (проверено 03.11.2026):
* ответы длиннее ~30 КБ (и все HTML-страницы сайта) с нашей сети обрываются на ~16 КБ и висят
  до тайм-аута — так ведут себя и curl, и httpx, и сторонний загрузчик. Поэтому ряд запрашиваем
  кусками по календарному кварталу (~6 КБ): адреса стабильны, прошлые кварталы берутся из кэша,
  докачка — только кварталы, начиная с того, где лежит ctx.start_for().
* официальная доходность (таблица «изменение стоимости пая … на <дата>» за 1/3/6 мес и 1 год)
  есть только на HTML-странице фонда, а она не отдаётся целиком — official_returns пустой,
  причина в warnings фонда.
* robots.txt сайта: «User-agent: * / Disallow: /» (разрешены только Яндекс и Google). Берём только
  документированный публичный API, без обхода страниц: 4 фонда × 1–6 кварталов за прогон.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..model import Fund, FundData, SeriesPoint
from ..parsing import day, norm_text, num, require, require_keys
from .base import Adapter, Context

BASE = "https://arsagera.ru"
API_URL = BASE + "/api/v1/funds/{code}/fund-metrics/"
DOC_URL = BASE + "/files/arsagera_metriki_fonda.pdf"

# Код API -> (название из документации API, страница фонда для человека).
API_FUNDS = {
    "fa": ("Арсагера — фонд акций", "/products/opifa_arsagera_-_fond_akcij/"),
    "f4si": ("Арсагера — фонд смешанных инвестиций", "/products/opifsi_arsagera_-_fond_smeshannyh_investicij/"),
    "f64": ("Арсагера — акции 6.4", "/products/ipifa_arsagera_-_akcii_64/"),
    "fo": ("Арсагера – фонд облигаций КР 1.55", "/products/opifo_arsagera_fond_obligacij_kr_155/"),
}


# ---------- чистые функции ----------

def parse_metrics(content: bytes | str | dict) -> list[SeriesPoint]:
    """Ответ fund-metrics -> точки (СЧА и цена пая в рублях). Пустой data — в окне нет дат, это не ошибка."""
    data = json.loads(content) if isinstance(content, (bytes, str)) else content
    require_keys(data, ("data",), "fund-metrics")
    rows = data["data"]
    require(isinstance(rows, list), f"fund-metrics: data — не список, а {type(rows).__name__}")
    points = []
    for row in rows:
        require_keys(row, ("date", "nav_per_share", "total_net_assets"), "fund-metrics: строка")
        points.append(SeriesPoint(day(row["date"]), num(row["total_net_assets"]), num(row["nav_per_share"])))
    return sorted(points, key=lambda p: p.date)


def quarter_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Календарные кварталы, покрывающие [start, end]: [(1-е число квартала, последний день квартала)]."""
    out = []
    first = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    while first <= end:
        nxt = date(first.year + (first.month == 10), (first.month + 2) % 12 + 1, 1)
        out.append((first, nxt - timedelta(days=1)))
        first = nxt
    return out


def _today() -> date:
    """Сегодня. В адрес запроса не попадает: только определяет последний квартал, а границы окна — всегда
    1-е и последнее число квартала, поэтому адреса не меняются по дням (с to=31.12.2026 ответ байт в байт
    тот же, что с to=сегодня, — проверено 06.11.2026)."""
    return date.today()


def quoted_name(full_name: str) -> str:
    """Название фонда в кавычках из полного названия реестра: '… «Арсагера - фонд акций»' -> 'Арсагера - фонд акций'."""
    for left, right in (("«", "»"), ('"', '"')):
        if left in full_name:
            i = full_name.index(left) + 1
            j = full_name.find(right, i)
            if j > i:
                return full_name[i:j]
    return full_name


def match_code(fund: Fund) -> str | None:
    want = {norm_text(fund.name), norm_text(quoted_name(fund.full_name))}
    hits = [code for code, (name, _) in API_FUNDS.items() if norm_text(name) in want]
    return hits[0] if len(hits) == 1 else None


# ---------- адаптер ----------

class ArsageraAdapter(Adapter):
    key = "arsagera"
    title = "ПАО «УК «Арсагера»"
    uk_inns = ("7840303927",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        code = match_code(fund)
        if code is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в документации API УК ({DOC_URL}) нет фонда «{quoted_name(fund.full_name)}»")
        page_url = BASE + API_FUNDS[code][1]
        fd = FundData(fund.rules, currency="RUB", page_url=page_url)
        for first, last in quarter_windows(ctx.start_for(fund.rules), _today()):
            raw = ctx.http.get(API_URL.format(code=code), fmt="json", funds=[fund.rules],
                               params={"from": first.isoformat(), "to": last.isoformat()})
            fd.segments.append(self.segment(raw, parse_metrics(raw.content()), page_url=page_url,
                                            since=ctx.since))
        if not any(s.points for s in fd.segments):
            fd.status, fd.reason = "partial", f"API УК не вернул точек после {ctx.start_for(fund.rules)}"
        fd.warnings.append("официальная доходность не собрана: УК публикует её только на HTML-странице фонда "
                           f"({page_url}), а страницы сайта с нашей сети обрываются на ~16 КБ и не загружаются")
        return fd
