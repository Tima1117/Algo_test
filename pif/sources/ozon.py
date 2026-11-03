"""ООО УК «ОЗОН Управление активами» (ozon-am.ru) — 5 биржевых фондов (БПИФ РФИ), сформированы в июне–июле 2026.

Сайт — одностраничное приложение (SvelteKit). Страницы сами ничего не содержат, данные берут
из JSON-API того же домена (найдено по запросам браузера и по таблице маршрутов в бандле сайта):

* {BASE}/api/mutual-funds — список фондов на сайте: mutualFundID, mutualFundName
  («БПИФ РФИ «Озон Денежный рынок»»), ticker (OLIQA, RUFOA, …). Страница фонда для человека —
  {BASE}/general/mutual-funds/<ticker>.
* {BASE}/api/documents?ticker=<ticker> — документы фонда (группы «Документы» и «Отчетность»):
  ссылки на PDF правил ДУ, КИД, справок о СЧА. Из названия «Правила доверительного управления
  № 7797» берём номер правил — по нему фонд сайта сопоставляется с реестром (название —
  только подсказка, какой фонд проверить первым).
* {BASE}/api/statistics — таблица «Статистика → СЧА» ({BASE}/statistics): по каждому фонду СЧА и
  расчётная стоимость пая за последний и предыдущий рабочие дни и валюта. СЧА — в рублях
  (не в тысячах: 52 414 080,98 = 500 000 паёв × 104,8282), пай — в валюте фонда (у всех RUB,
  включая «Корпоративные валютные облигации РФ»: так в API, на странице «₽» и в КИД «руб.»).
* {BASE}/api/deviation — «Статистика → Отклонение» ({BASE}/statistics/deviation): расчётная
  стоимость пая на последний день («Цена пая T», совпадает с таблицей СЧА) и 20 рабочих дней
  назад («Цена пая T-20»), без СЧА. Берём обе точки как ряд только цены.

Глубина истории. Ряда за период (графика, выгрузки, архива по датам) на сайте нет: API
игнорирует параметры дат (?date=, ?from=&to= отдают ту же таблицу), других маршрутов в
бандле нет. Ежедневно публикуются только два последних рабочих дня (+ цена пая T-20).
Более ранняя история — помесячные PDF «Справка о стоимости чистых активов» (СЧА и пай на
конец месяца, с 30.06.2026) и КИД (пай и СЧА на конец месяца). PDF адаптер не разбирает
(в зависимостях проекта нет PDF-библиотеки), поэтому статус фондов — 'partial'. Каждый
запуск заменяет сегмент целиком (тот же адрес источника), поэтому ежедневные точки между
запусками не накапливаются.

Доходность. Официальная доходность за 1/3/6/12 мес публикуется только в КИД (PDF, раздел 5
«Основные результаты инвестирования») — official_returns пустые, в warnings ссылка на
последний КИД.

robots.txt сайта: «User-agent: * / Disallow: /». Запросов немного (список, документы пяти
фондов, две таблицы — 8 запросов за прогон, не чаще 1 в секунду).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable
from urllib.parse import quote

from ..http import FetchError
from ..model import Fund, FundData, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require, require_keys
from .base import Adapter, Context

BASE = "https://ozon-am.ru"
FUNDS_API = f"{BASE}/api/mutual-funds"
DOCS_API = f"{BASE}/api/documents"
STATS_API = f"{BASE}/api/statistics"
DEVIATION_API = f"{BASE}/api/deviation"
FUND_PAGE = BASE + "/general/mutual-funds/{ticker}"
STATS_PAGE = f"{BASE}/statistics"
DEVIATION_PAGE = f"{BASE}/statistics/deviation"

CURRENCIES = {"RUB", "USD", "EUR", "CNY"}

PARTIAL_REASON = (
    "сайт УК публикует СЧА и расчётную стоимость пая только за два последних рабочих дня "
    "(«Статистика → СЧА») и цену пая 20 рабочих дней назад («Отклонение»)")

_RULES_RE = re.compile(r"правила\s+доверительного\s+управления[^№]{0,80}№\s*(\d{3,6})", re.I)
_KID_RE = re.compile(r"ключевой\s+информационный\s+документ.*?по\s+состоянию\s+на\s+(\d{2}\.\d{2}\.\d{4})", re.I)
_NAV_REPORT_RE = re.compile(r"справка\s+о\s+стоимости\s+чистых\s+активов.*?\bна\s+(\d{2}\.\d{2}\.\d{4})", re.I)
# Даты в API — строка Go-шного time.String(): «2026-11-02 00:00:00 +0000 UTC»
_API_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}:\d{2}.*)?$")


@dataclass
class SiteFund:
    fund_id: str
    name: str          # как на сайте: «БПИФ РФИ «Озон Денежный рынок»»
    ticker: str        # код на сайте (OLIQA) — часть адреса страницы фонда


@dataclass
class FundDocs:
    rules: set[str] = field(default_factory=set)                      # номера правил ДУ из названий
    kid: list[tuple[date, str]] = field(default_factory=list)          # КИД: (по состоянию на, ссылка)
    nav_reports: list[tuple[date, str]] = field(default_factory=list)  # справки о СЧА: (дата, ссылка)


@dataclass
class FundStat:
    name: str
    currency: str
    points: list[SeriesPoint]


# ---------------- разбор ----------------

def _load(content, where: str):
    if isinstance(content, (dict, list)):
        return content
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    try:
        return json.loads(content)
    except (TypeError, ValueError) as e:
        raise UnknownFormat(f"{where}: не JSON ({str(content)[:60]!r})") from e


def _items(data, key: str, where: str) -> list:
    require_keys(data, [key], where)
    items = data[key]
    require(isinstance(items, list), f"{where}: поле {key!r} — не список")
    return items


def _api_date(value, where: str) -> date | None:
    """«2026-11-02 00:00:00 +0000 UTC» -> date; пусто -> None (так «—» показывает и сам сайт)."""
    if value in (None, ""):
        return None
    m = _API_DATE_RE.match(str(value).strip())
    if not m:
        raise UnknownFormat(f"{where}: не дата: {value!r}")
    return day(m.group(1))


def _positive(value, where: str) -> float | None:
    v = num(value)
    if v is not None and v <= 0:
        raise UnknownFormat(f"{where}: неположительное значение {value!r}")
    return v


def _name_key(name: str) -> str:
    """«БПИФ РФИ «Озон Денежный рынок»» и «Озон Денежный рынок» из реестра -> «озон денежный рынок»."""
    return " ".join(w for w in norm_text(name).split() if w != "рфи")


def parse_fund_list(content) -> list[SiteFund]:
    """/api/mutual-funds -> фонды на сайте."""
    items = _items(_load(content, "список фондов"), "data", "список фондов")
    require(len(items) > 0, "список фондов: пустой")
    out: list[SiteFund] = []
    for i, it in enumerate(items):
        require_keys(it, ["mutualFundID", "mutualFundName", "ticker"], f"список фондов[{i}]")
        name, ticker = str(it["mutualFundName"] or "").strip(), str(it["ticker"] or "").strip()
        require(bool(name) and bool(ticker), f"список фондов[{i}]: пустое название или тикер")
        out.append(SiteFund(str(it["mutualFundID"]), name, ticker))
    require(len({f.ticker for f in out}) == len(out), "список фондов: повторяется тикер")
    return out


def parse_documents(content) -> FundDocs:
    """/api/documents?ticker=… -> номера правил ДУ, КИД и справки о СЧА (с датами и ссылками).

    Пустой список документов допустим (на сайте тогда «документы отсутствуют»).
    """
    groups = _items(_load(content, "документы"), "data", "документы")
    docs = FundDocs()
    for gi, g in enumerate(groups):
        items = _items(g, "items", f"документы[{gi}]")
        for ii, it in enumerate(items):
            where = f"документы[{gi}].items[{ii}]"
            require_keys(it, ["documentName", "link"], where)
            name, link = str(it["documentName"] or ""), str(it["link"] or "")
            m = _RULES_RE.search(name)
            if m:
                docs.rules.add(m.group(1))
            m = _KID_RE.search(name)
            if m:
                docs.kid.append((day(m.group(1)), link))
            m = _NAV_REPORT_RE.search(name)
            if m:
                docs.nav_reports.append((day(m.group(1)), link))
    docs.kid.sort()
    docs.nav_reports.sort()
    return docs


def parse_statistics(content) -> dict[str, FundStat]:
    """/api/statistics -> {название фонда на сайте: СЧА и пай за предыдущий и последний день}."""
    rows = _items(_load(content, "таблица СЧА"), "fundStats", "таблица СЧА")
    require(len(rows) > 0, "таблица СЧА: нет ни одного фонда")
    out: dict[str, FundStat] = {}
    for i, r in enumerate(rows):
        where = f"таблица СЧА[{i}]"
        require_keys(r, ["fundName", "currency", "prevStatDate", "prevNetAssetValue", "prevSharePrice",
                         "lastStatDate", "lastNetAssetValue", "lastSharePrice"], where)
        name = str(r["fundName"] or "").strip()
        require(bool(name), f"{where}: пустое название фонда")
        require(name not in out, f"таблица СЧА: фонд {name!r} встречается дважды")
        currency = str(r["currency"] or "").strip().upper()
        require(currency in CURRENCIES, f"{where}: незнакомая валюта {r['currency']!r}")
        points: list[SeriesPoint] = []
        for pref in ("prev", "last"):
            d = _api_date(r[f"{pref}StatDate"], f"{where}.{pref}StatDate")
            nav = _positive(r[f"{pref}NetAssetValue"], f"{where}.{pref}NetAssetValue")
            price = _positive(r[f"{pref}SharePrice"], f"{where}.{pref}SharePrice")
            if d is None:
                require(nav is None and price is None, f"{where}: значения {pref} без даты")
                continue
            require(nav is not None or price is not None, f"{where}: на {d} нет ни СЧА, ни пая")
            points.append(SeriesPoint(d, nav, price))
        require(len(points) > 0, f"{where}: нет ни одной даты")
        if len(points) == 2:
            require(points[0].date < points[1].date,
                    f"{where}: предыдущий день {points[0].date} не раньше последнего {points[1].date}")
        out[name] = FundStat(name, currency, points)
    return out


def parse_deviation(content) -> dict[str, list[SeriesPoint]]:
    """/api/deviation -> {название фонда: расчётная стоимость пая на T-20 и T (без СЧА)}."""
    rows = _items(_load(content, "таблица отклонения"), "funds", "таблица отклонения")
    require(len(rows) > 0, "таблица отклонения: нет ни одного фонда")
    out: dict[str, list[SeriesPoint]] = {}
    for i, r in enumerate(rows):
        where = f"таблица отклонения[{i}]"
        require_keys(r, ["fundName", "date20", "sharePrice20", "lastDate", "lastSharePrice"], where)
        name = str(r["fundName"] or "").strip()
        require(bool(name), f"{where}: пустое название фонда")
        require(name not in out, f"таблица отклонения: фонд {name!r} встречается дважды")
        points: list[SeriesPoint] = []
        for dkey, pkey in (("date20", "sharePrice20"), ("lastDate", "lastSharePrice")):
            d = _api_date(r[dkey], f"{where}.{dkey}")
            price = _positive(r[pkey], f"{where}.{pkey}")
            if d is None or price is None:
                require(d is None and price is None, f"{where}: {dkey}/{pkey} заполнены не вместе")
                continue
            points.append(SeriesPoint(d, None, price))
        if len(points) == 2:
            require(points[0].date < points[1].date, f"{where}: дата T-20 не раньше даты T")
        out[name] = points
    return out


def _by_site_name(table: dict, sf: SiteFund):
    """Строка таблицы для фонда сайта: по точному названию, иначе по нормализованному."""
    if sf.name in table:
        return table[sf.name]
    hits = [v for k, v in table.items() if _name_key(k) == _name_key(sf.name)]
    require(len(hits) <= 1, f"в таблице несколько строк для {sf.name!r}")
    return hits[0] if hits else None


# ---------------- сбор ----------------

class _Site:
    """Запросы, общие для всех фондов УК: каждый — один раз за прогон.

    Ошибка запоминается и поднимается для каждого фонда заново, чтобы guarded() поставил
    каждому свой статус (blocked / unavailable / parse_error), а не уронил весь адаптер.
    """

    def __init__(self, ctx: Context, rules: list[str]):
        self.ctx = ctx
        self.rules = rules
        self._memo: dict[str, tuple[object, BaseException | None]] = {}

    def _once(self, key: str, fn: Callable[[], object]):
        if key not in self._memo:
            try:
                self._memo[key] = (fn(), None)
            except Exception as e:  # noqa: BLE001 — статус определит guarded()
                self._memo[key] = (None, e)
        value, err = self._memo[key]
        if err is not None:
            raise err
        return value

    def funds(self) -> list[SiteFund]:
        return self._once("funds", lambda: parse_fund_list(
            self.ctx.http.get(FUNDS_API, fmt="json", funds=self.rules).content()))

    def docs(self, ticker: str, rules: str) -> FundDocs:
        """Документы фонда сайта; в манифест пишется фонд реестра, ради которого их запросили."""
        return self._once(f"docs:{ticker}", lambda: parse_documents(self.ctx.http.get(
            DOCS_API, params={"ticker": ticker}, fmt="json", funds=[rules]).content()))

    def stats(self):
        def load():
            raw = self.ctx.http.get(STATS_API, fmt="json", funds=self.rules)
            return raw, parse_statistics(raw.content())
        return self._once("stats", load)

    def deviation(self):
        def load():
            raw = self.ctx.http.get(DEVIATION_API, fmt="json", funds=self.rules)
            return raw, parse_deviation(raw.content())
        return self._once("deviation", load)


def match_fund(fund: Fund, site_funds: list[SiteFund],
               docs_for: Callable[[SiteFund], FundDocs]) -> tuple[SiteFund | None, FundDocs | None, str]:
    """Фонд реестра -> фонд сайта по номеру правил из документов фонда на сайте.

    Сначала проверяется фонд с тем же названием (один запрос документов), потом остальные.
    Если у одноимённого фонда номер правил на сайте не указан вовсе — принимаем по названию
    (с пометкой). Чужой номер правил — не наш фонд, даже при совпадении названия.
    """
    key = _name_key(fund.name)
    named = [sf for sf in site_funds if _name_key(sf.name) == key]
    others = [sf for sf in site_funds if sf not in named]
    for sf in named + others:
        docs = docs_for(sf)
        if fund.rules in docs.rules:
            return sf, docs, "по номеру правил"
        if sf in named and not docs.rules:
            return sf, docs, "по названию (номер правил на сайте не указан)"
    return None, None, ""


class OzonAdapter(Adapter):
    key = "ozon"
    title = "ОЗОН Управление Активами"
    uk_inns = ("9703217363",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        site = _Site(ctx, [f.rules for f in funds])
        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, site, f)) for f in funds]

    def _one(self, ctx: Context, site: _Site, fund: Fund) -> FundData:
        site_funds = site.funds()
        sf, docs, how = match_fund(fund, site_funds, lambda s: site.docs(s.ticker, fund.rules))
        if sf is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"на сайте УК нет фонда с правилами № {fund.rules} "
                                   f"(проверены документы {len(site_funds)} фондов из {FUNDS_API})")
        page = FUND_PAGE.format(ticker=sf.ticker)
        fd = FundData(fund.rules, page_url=page)
        if how != "по номеру правил":
            fd.warnings.append(f"фонд сопоставлен {how}: {sf.name}")

        stats_raw, stats = site.stats()
        stat = _by_site_name(stats, sf)
        if stat is None:
            fd.status = "no_history"
            fd.reason = f"фонд есть на сайте ({page}), но в таблице СЧА {STATS_PAGE} его нет"
            return fd
        fd.currency = stat.currency
        fd.segments.append(self.segment(stats_raw, stat.points, page_url=STATS_PAGE, since=ctx.since))

        # Цена пая T-20 — дополнительная точка; сбой этой таблицы не отменяет основные данные.
        try:
            dev_raw, dev = site.deviation()
        except UnknownFormat as e:
            ctx.log.error("[%s] %s: таблица отклонения: %s", fund.uk_short, fund.rules, e)
            fd.warnings.append(f"таблица отклонения {DEVIATION_API} не разобрана: {e}")
        except FetchError as e:   # в т.ч. Blocked: основная таблица уже есть — только предупреждение
            fd.warnings.append(f"таблица отклонения {DEVIATION_API} не скачалась: {e.reason}")
        else:
            dev_points = _by_site_name(dev, sf)
            if dev_points:
                fd.segments.append(self.segment(dev_raw, dev_points, page_url=DEVIATION_PAGE, since=ctx.since))

        fd.status, fd.reason = "partial", PARTIAL_REASON
        if docs.nav_reports:
            first, last = docs.nav_reports[0][0], docs.nav_reports[-1][0]
            fd.reason += (f"; более ранняя история — только помесячные PDF-справки о СЧА "
                          f"({len(docs.nav_reports)} шт. с {first:%d.%m.%Y} по {last:%d.%m.%Y}, "
                          f"список: {DOCS_API}?ticker={sf.ticker}), их адаптер не разбирает")
        else:
            fd.reason += "; архива значений на сайте нет"
        if docs.kid:
            as_of, link = docs.kid[-1]
            fd.warnings.append(f"официальная доходность за 1/3/6/12 мес публикуется только в КИД (PDF), "
                               f"последний — по состоянию на {as_of:%d.%m.%Y}: {quote(link, safe=':/')}; "
                               "PDF не разбираем")
        else:
            fd.warnings.append("официальная доходность на сайте УК не найдена (нет КИД в документах фонда)")
        return fd
