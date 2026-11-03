"""АО УК «Апрель Капитал» (ИНН 7704239292), сайт https://april-capital.ru/.

Источники
* POST https://april-capital.ru/api/v01_fund_details_list — JSON-API, которым сайт рисует графики
  и таблицы на страницах фондов. Тело: {"pointsCount", "dateStart", "dateEnd"} (epoch-секунды),
  обязателен заголовок «Prefer: params=single-object» (без него шлюз отвечает 400). Один ответ —
  все фонды УК, включая закрытые (active=false, пустой priceList).
  * priceList: [{date, sharePrice, assetValue}] — ежедневно по рабочим дням (праздники пропущены,
    рабочие субботы есть). date — полночь по Москве (UTC+3) в epoch-секундах; так же сайт показывает
    даты пользователю из Москвы, а соседний метод v01_fund_price_change отдаёт к ним rawDate.
  * sharePrice и assetValue — целые копейки: сайт делит на 100 (1 182,52 руб. = 118252). Здесь тоже
    делим на 100: цена пая и СЧА в рублях.
  * pointsCount прореживает ряд (сайт просит 100 точек); 100000 больше числа дней в любом диапазоне —
    приходит каждая опубликованная дата.
  * dynamics {month1, month3, month6, year1, ...} + dynamicsDate — официальная доходность, которую
    сайт показывает в таблице «Динамика стоимости инвестиционного пая на <дата>» на /funds/ и на
    странице фонда (УК считает её от последнего рабочего дня месяца до последнего рабочего дня
    месяца; dynamicsDate — конец последнего полного месяца). 0 сайт показывает как «-» — значит,
    не опубликовано (например, 3 и 5 лет у молодого фонда).
* GET https://april-capital.ru/funds/ — подвал страницы перечисляет фонды с номерами правил ДУ
  («… «Апрель Капитал — Акции» (Правила … № 0118–14241730)»). В API номеров правил нет, поэтому
  сопоставление: номер правил реестра → название в подвале → shortName в API (через norm_text).
  Если подвал не прочитался, сопоставляем по названию из реестра (с предупреждением).

История: запрашиваем всегда целиком с ctx.since, а не хвост с ctx.start_for(): у POST-запроса
адрес источника (raw_url) не включает тело, поэтому хвостовой сегмент заменил бы при слиянии
(cli.merge_with_previous) полный ряд прошлого сбора. Один запрос на все фонды ~120 КБ — это дёшево.
dateEnd — постоянная дальняя дата (API требует верхнюю границу), чтобы тело запроса и ключ кэша
не менялись каждый день.

Страницы фондов для человека: https://april-capital.ru/funds/<slug>/ (stock, raw, notraw, balance,
plus, longbonds) — там же таблица «Расчетная стоимость инвестиционного пая» (цена и СЧА) и доходность.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, norm_text, require, require_keys
from .base import Adapter, Context

SITE = "https://april-capital.ru"
API_URL = f"{SITE}/api/v01_fund_details_list"
FUNDS_PAGE = f"{SITE}/funds/"
FUND_PAGE = SITE + "/funds/{slug}/"
API_HEADERS = {"Prefer": "params=single-object"}

MSK = timezone(timedelta(hours=3))
POINTS_ALL = 100_000              # больше, чем дней в любом диапазоне: ряд не прореживается
DATE_END = 4102444800             # 2100-01-01: «без верхней границы», тело запроса стабильно

# Страница фонда на сайте по номеру правил (адреса из меню сайта); новый фонд — общая страница.
PAGE_SLUGS = {
    "0118-14241730": "stock",      # Акции
    "0846-94127344": "raw",        # Акции сырьевых компаний
    "0847-94127333": "notraw",     # Акции несырьевых компаний
    "0060-56716383": "balance",    # Сбалансированный
    "0424-79363131": "plus",       # Облигации плюс
    "6778": "longbonds",           # Долгосрочные облигации
}

# Поле dynamics -> период OfficialReturn
RETURN_FIELDS = {"month1": "1m", "month3": "3m", "month6": "6m", "year1": "1y"}

_FUND_KEYS = ("id", "active", "shortName", "priceList", "dynamics", "dynamicsDate")
_POINT_KEYS = ("date", "sharePrice", "assetValue")
_RULES_RE = re.compile(
    r"«([^«»]+)»\s*\d*\s*\(\s*Правила доверительного управления[^()]*?№\s*(\d[\d\s–—-]*\d)\s*\)")


# ---------------- разбор ----------------

def msk_day(ts) -> date:
    """Epoch-секунды полуночи по Москве -> дата. Другое время суток — значит, формат сменился."""
    require(isinstance(ts, int) and not isinstance(ts, bool), f"дата не epoch-секунды: {ts!r}")
    dt = datetime.fromtimestamp(ts, MSK)
    require(dt.time() == time(0, 0), f"дата {ts} — не полночь по Москве ({dt.isoformat()})")
    return dt.date()


def _rubles(value, what: str) -> float | None:
    """Целые копейки -> рубли. Дробное число значило бы смену единиц — не угадываем."""
    if value is None:
        return None
    require(isinstance(value, int) and not isinstance(value, bool),
            f"{what}: ожидались целые копейки, получено {value!r}")
    require(value > 0, f"{what}: неположительное значение {value!r}")
    return round(value / 100, 2)


def parse_fund_list(content: bytes | str | dict) -> list[dict]:
    """Ответ v01_fund_details_list -> список фондов сайта (проверены ключевые поля)."""
    data = json.loads(content) if isinstance(content, (bytes, str)) else content
    require_keys(data, ("error", "success"), "ответ fund_details_list")
    require(data["error"] is None, f"API вернул ошибку: {data['error']!r}")
    items = data["success"]
    require(isinstance(items, list) and items, "ответ fund_details_list: success — не непустой список")
    for it in items:
        require_keys(it, _FUND_KEYS, "фонд в fund_details_list")
        require(isinstance(it["shortName"], str) and it["shortName"].strip(),
                f"фонд id={it['id']}: пустое название")
        require(isinstance(it["priceList"], list), f"фонд id={it['id']}: priceList — не список")
    return items


def parse_series(item: dict) -> list[SeriesPoint]:
    """priceList одного действующего фонда -> точки (дата, СЧА руб., цена пая руб.)."""
    where = f"priceList фонда {item.get('shortName')!r}"
    rows = item.get("priceList")
    require(isinstance(rows, list) and rows, f"{where}: пуст — у действующего фонда ряд обязан быть")
    points: dict[date, SeriesPoint] = {}
    for row in rows:
        require_keys(row, _POINT_KEYS, where)
        d = msk_day(row["date"])
        require(d not in points, f"{where}: дата {d} встречается дважды")
        points[d] = SeriesPoint(d, nav=_rubles(row["assetValue"], f"{where}, {d}, СЧА"),
                                price=_rubles(row["sharePrice"], f"{where}, {d}, цена пая"))
    return [points[d] for d in sorted(points)]


def parse_returns(item: dict, source_url: str) -> list[OfficialReturn]:
    """dynamics одного фонда -> официальная доходность за 1/3/6/12 мес. 0 и null — не опубликовано."""
    dyn = item.get("dynamics")
    if dyn is None:
        return []
    require_keys(dyn, RETURN_FIELDS, f"dynamics фонда {item.get('shortName')!r}")
    as_of = msk_day(item["dynamicsDate"]) if item.get("dynamicsDate") is not None else None
    out = []
    for fld, period in RETURN_FIELDS.items():
        v = dyn[fld]
        if v is None or v == 0:          # сайт показывает 0 как «-»
            continue
        require(isinstance(v, (int, float)) and not isinstance(v, bool),
                f"dynamics.{fld}: не число {v!r}")
        out.append(OfficialReturn(period, float(v), as_of, source_url))
    return out


def parse_rules_map(content: bytes | str) -> dict[str, str]:
    """Подвал страницы сайта -> {номер правил: название фонда}."""
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    text = BeautifulSoup(content, "lxml").get_text(" ")
    text = re.sub(r"\s+", " ", text)
    out: dict[str, str] = {}
    for name, rules in _RULES_RE.findall(text):
        rules = re.sub(r"\s", "", rules).replace("–", "-").replace("—", "-")
        require(out.get(rules, name) == name, f"номер правил {rules} у двух фондов в подвале")
        out[rules] = name
    require(bool(out), "на странице нет перечня фондов с номерами правил (подвал сменился?)")
    return out


def match_funds(funds: list[Fund], items: list[dict], rules_map: dict[str, str]) -> dict[str, dict]:
    """Фонд реестра -> фонд из API. Номер правил -> название в подвале -> shortName; иначе по названию."""
    by_name: dict[str, dict] = {}
    for it in items:
        if it["active"] is False:        # закрытые фонды сайт не показывает
            continue
        key = norm_text(it["shortName"])
        require(key not in by_name, f"два действующих фонда с одинаковым названием: {it['shortName']!r}")
        by_name[key] = it
    out = {}
    for f in funds:
        names = [rules_map[f.rules]] if f.rules in rules_map else [f.full_name, f.name]
        for name in names:
            it = by_name.get(norm_text(name))
            if it is not None:
                out[f.rules] = it
                break
    return out


# ---------------- адаптер ----------------

class AprilAdapter(Adapter):
    key = "april"
    title = "Апрель Капитал"
    uk_inns = ("7704239292",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        try:
            raw, matched, warn = self._load(ctx, funds, all_rules)
            err = None
        except Exception as e:  # noqa: BLE001 — превратится в статус каждого фонда через guarded
            raw, matched, warn, err = None, {}, None, e
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, raw, matched, warn, err))
                for f in funds]

    def _load(self, ctx: Context, funds: list[Fund], all_rules: list[str]):
        start = int(datetime.combine(ctx.since, time(0, 0), MSK).timestamp())
        raw = ctx.http.get(API_URL, method="POST", fmt="json", funds=all_rules, headers=API_HEADERS,
                           json_body={"pointsCount": POINTS_ALL, "dateStart": start, "dateEnd": DATE_END})
        items = parse_fund_list(raw.content())
        warn = None
        try:
            page = ctx.http.get(FUNDS_PAGE, fmt="html", funds=all_rules)
            rules_map = parse_rules_map(page.content())
        except (FetchError, UnknownFormat) as e:
            rules_map = {}
            warn = f"номера правил на сайте не прочитаны ({e}); фонд сопоставлен по названию"
        return raw, match_funds(funds, items, rules_map), warn

    def _fund(self, ctx: Context, fund: Fund, raw, matched: dict, warn: str | None,
              err: Exception | None) -> FundData:
        if err is not None:
            raise err
        slug = PAGE_SLUGS.get(fund.rules)
        page_url = FUND_PAGE.format(slug=slug) if slug else FUNDS_PAGE
        item = matched.get(fund.rules)
        if item is None:
            return FundData(fund.rules, page_url=FUNDS_PAGE, status="not_found",
                            reason="фонда нет среди действующих фондов на сайте УК "
                                   "(ни по номеру правил, ни по названию)")
        points = parse_series(item)
        fd = FundData(fund.rules, currency="RUB", page_url=page_url,
                      segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)],
                      official_returns=parse_returns(item, page_url))
        if warn:
            fd.warnings.append(warn)
        missing = sorted(set(RETURN_FIELDS.values()) - {r.period for r in fd.official_returns})
        if missing:
            fd.warnings.append(f"УК не публикует доходность за {', '.join(missing)}")
        if not any(p.nav is not None for p in points):
            fd.status, fd.reason = "partial", "на сайте только цена пая, СЧА нет"
        return fd
