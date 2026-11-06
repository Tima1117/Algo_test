"""АО УК «Ингосстрах - Инвестиции» (ingosinvest-am.ru): история СЧА/пая и официальная доходность.

Сайт — Next.js поверх собственного API и CMS Strapi. Всё, что нужно, отдаёт API самого сайта УК:

- /api/content/open-mutual-funds — каталог фондов из раздела «Раскрытие информации»:
  номер правил ДУ («№ 4742 от 02.12.2021») и внутренний fundId. Сопоставление — по номеру правил.
- /api/Info/FundQuotesDynamic/{fundId}?startDate=YYYY-MM-DD&useCache=false —
  JSON-ряд с даты startDate: date, unitValue (расчётная стоимость пая), assetsValue (СЧА).
  endDate API не обязателен: без него отдаёт ряд до последней опубликованной точки (проверено 06.11.2026:
  ответ байт в байт тот же, что с endDate=сегодня). Не передаём его, чтобы адрес не менялся каждый день.
  Частота — каждый рабочий день. Последние две точки те же, что в таблице «Раскрытие информации →
  О ПИФах → Расчётная стоимость пая и СЧА» (/open-info/required/about-pifs).
- POST /api/Info/FundProfitabilityRegular, тело [fundId] — официальная доходность за 1/3/6/12 мес.
  (и 3/5 лет) на последний месяц, по п. 20.1–20.3 Указания Банка России № 5609-У. Эти же числа (с
  округлением до сотых) сайт печатает в блоке «Правовая информация» внизу каждой страницы.
- /pif и /bpif — витрина фондов: карточка {id: fundId, detailLink: "/pif/pif5"} даёт страницу
  фонда для человека (у части фондов её нет — тогда ссылка на раздел раскрытия).

Валюта: УК публикует пай и СЧА всех фондов в рублях («РСП, руб.», «СЧА, руб.» в разделе раскрытия),
в том числе «Ингосстрах-валютные облигации» и БПИФ «Облигации в юанях» (пай на бирже тоже в рублях).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.ingosinvest-am.ru"
CATALOG_URL = f"{BASE}/api/content/open-mutual-funds"
QUOTES_URL = BASE + "/api/Info/FundQuotesDynamic/{fund_id}"
RETURNS_URL = f"{BASE}/api/Info/FundProfitabilityRegular"
LISTING_PAGES = (f"{BASE}/pif", f"{BASE}/bpif")
DISCLOSURE_PAGE = f"{BASE}/open-info/required/about-pifs"

CURRENCY = "RUB"

# periodId в ответе FundProfitabilityRegular -> (наш период, длина в месяцах). 5 и 6 — 3 и 5 лет, не берём.
RETURN_PERIODS = {1: ("1m", 1), 2: ("3m", 3), 3: ("6m", 6), 4: ("1y", 12)}

# Последняя точка старше этого — предупреждение (УК публикует СЧА ежедневно).
STALE_AFTER = timedelta(days=14)

_RULES_RE = re.compile(r"^\s*№?\s*(\d+(?:-\d+)?)\s*(?:от\b|$)")
_FLIGHT_RE = re.compile(r"<script>self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_DETAIL_LINK_RE = re.compile(r"/b?pif/[\w-]+")


@dataclass
class SiteFund:
    fund_id: int
    rules: str
    title: str
    type: str


def norm_rules(value: str) -> str:
    """'№ 0050-54904637 от 15.11.2000 г.' / '1148-94139715 от ...' / '4742' -> '0050-54904637'."""
    m = _RULES_RE.match(str(value or ""))
    if not m:
        raise UnknownFormat(f"не номер правил: {value!r}")
    return m.group(1)


def _json(content):
    if isinstance(content, (bytes, str)):
        try:
            return json.loads(content)
        except ValueError as e:
            raise UnknownFormat(f"ответ не JSON: {e}") from e
    return content


def parse_catalog(content) -> dict[str, SiteFund]:
    """Каталог Strapi /api/content/open-mutual-funds -> {номер правил: SiteFund}."""
    data = _json(content)
    require_keys(data, ("data", "meta"), "каталог фондов")
    items = data["data"]
    require(isinstance(items, list) and items, "каталог фондов: пустой список data")
    total = (data["meta"].get("pagination") or {}).get("total")
    require(total is None or total == len(items),
            f"каталог фондов отдан не целиком: {len(items)} из {total}")
    out: dict[str, SiteFund] = {}
    for item in items:
        require_keys(item, ("attributes",), "каталог фондов: элемент")
        a = item["attributes"]
        require_keys(a, ("numberAndDateOfRuleRegistration", "fundId", "title", "type"), "каталог фондов: фонд")
        rules = norm_rules(a["numberAndDateOfRuleRegistration"])
        try:
            fund_id = int(str(a["fundId"]).strip())
        except ValueError as e:
            raise UnknownFormat(f"каталог фондов: fundId не число: {a['fundId']!r}") from e
        require(rules not in out, f"каталог фондов: правила № {rules} встречаются дважды")
        out[rules] = SiteFund(fund_id, rules, str(a["title"]).strip(), str(a["type"]))
    return out


def parse_history(content, fund_id: int | None = None) -> list[SeriesPoint]:
    """FundQuotesDynamic -> точки (дата, СЧА в руб., расчётная стоимость пая в руб.)."""
    data = _json(content)
    require(isinstance(data, list), f"ряд: ожидался список, получено {type(data).__name__}")
    points: dict[date, SeriesPoint] = {}
    for row in data:
        require_keys(row, ("fundId", "date", "unitValue", "assetsValue"), "ряд: точка")
        if fund_id is not None:
            require(str(row["fundId"]) == str(fund_id),
                    f"ряд: точка чужого фонда {row['fundId']} (ждали {fund_id})")
        d = day(row["date"])
        require(d not in points, f"ряд: дата {d} повторяется")
        # 0 у живого фонда не бывает — это «нет значения», а не ноль
        nav = num(row["assetsValue"]) or None
        price = num(row["unitValue"]) or None
        points[d] = SeriesPoint(d, nav, price)
    return [points[d] for d in sorted(points)]


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def parse_returns(content, fund_id: int, source_url: str) -> list[OfficialReturn]:
    """FundProfitabilityRegular -> официальная доходность 1m/3m/6m/1y.

    Период определяется по periodId и сверяется с датами начала/конца (1/3/6/12 мес.),
    чтобы перестановка periodId не перепутала периоды. Нет значения (null) — периода нет.
    """
    data = _json(content)
    require(isinstance(data, dict), f"доходность: ожидался объект, получено {type(data).__name__}")
    require(str(fund_id) in data, f"доходность: в ответе нет фонда {fund_id}; есть {sorted(data)[:10]}")
    rows = data[str(fund_id)]
    require(isinstance(rows, list), "доходность: ожидался список периодов")
    out: list[OfficialReturn] = []
    for row in rows:
        require_keys(row, ("periodId", "startDate", "endDate", "profitability"), "доходность: период")
        known = RETURN_PERIODS.get(row["periodId"])
        if known is None:
            continue
        period, months = known
        start, end = day(row["startDate"]), day(row["endDate"])
        require(_months_between(start, end) == months,
                f"доходность: periodId {row['periodId']} ({period}) охватывает {start}…{end}")
        value = pct(row["profitability"])
        if value is None:
            continue
        out.append(OfficialReturn(period, value, end, source_url))
    return out


def _flight_text(html: str) -> str:
    """Данные Next.js (self.__next_f.push([1, "..."])) одной строкой."""
    parts = []
    for m in _FLIGHT_RE.finditer(html):
        try:
            arr = json.loads(m.group(1))
        except ValueError as e:
            raise UnknownFormat(f"страница: непонятный блок self.__next_f: {e}") from e
        if len(arr) == 2 and isinstance(arr[1], str):
            parts.append(arr[1])
    require(parts, "страница: нет данных Next.js (self.__next_f.push)")
    return "".join(parts)


def parse_cards(html: str) -> dict[int, str]:
    """Витрина /pif, /bpif -> {fundId: путь страницы фонда}."""
    text = _flight_text(html)
    dec = json.JSONDecoder()
    cards: dict[int, str] = {}
    for m in re.finditer(r'\{"id":\d+,', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        link = obj.get("detailLink")
        if isinstance(link, str) and _DETAIL_LINK_RE.fullmatch(link):
            cards[int(obj["id"])] = link
    require(cards, "витрина фондов: нет карточек с id и detailLink")
    return cards


class _Lazy:
    """Общий документ на все фонды: качается один раз; ошибка тоже запоминается."""

    def __init__(self, fn: Callable):
        self.fn = fn
        self.done = False
        self.value = None
        self.error: Exception | None = None

    def get(self):
        if not self.done:
            try:
                self.value = self.fn()
            except Exception as e:  # noqa: BLE001 — превратится в статус фонда в guarded()
                self.error = e
            self.done = True
        if self.error is not None:
            raise self.error
        return self.value


class IngosstrakhAdapter(Adapter):
    key = "ingosstrakh"
    title = "АО УК «Ингосстрах - Инвестиции»"
    uk_inns = ("7705136973",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        catalog = _Lazy(lambda: parse_catalog(
            ctx.http.get(CATALOG_URL, params={"pagination[pageSize]": 100}, fmt="json",
                         funds=all_rules).content()))
        cards = _Lazy(lambda: self._cards(ctx, all_rules))
        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, catalog, cards)) for f in funds]

    @staticmethod
    def _cards(ctx: Context, all_rules: list[str]) -> tuple[dict[int, str], list[str]]:
        """Страницы фондов из витрины. Не критично: при сбое — ссылка на раздел раскрытия."""
        found: dict[int, str] = {}
        problems: list[str] = []
        for url in LISTING_PAGES:
            try:
                found.update(parse_cards(ctx.http.get(url, fmt="html", funds=all_rules).text()))
            except (FetchError, UnknownFormat) as e:
                ctx.log.warning("[ingosstrakh] витрина %s: %s", url, e)
                problems.append(f"витрина фондов {url} не разобрана: {e}")
        return found, problems

    def _one(self, ctx: Context, fund: Fund, catalog: _Lazy, cards: _Lazy) -> FundData:
        site = catalog.get().get(norm_rules(fund.rules))
        if site is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в каталоге фондов сайта УК нет правил № {fund.rules}")
        fd = FundData(fund.rules, currency=CURRENCY)
        if norm_text(site.title) != norm_text(fund.name):
            fd.warnings.append(f"название на сайте УК «{site.title}» отличается от реестра «{fund.name}»"
                               f" (сопоставлено по номеру правил)")

        pages, problems = cards.get()
        fd.warnings.extend(problems)
        path = pages.get(site.fund_id)
        fd.page_url = BASE + path if path else DISCLOSURE_PAGE
        if not path:
            fd.warnings.append("у фонда нет страницы на сайте УК; текущие СЧА и пай видны в разделе "
                               "«Раскрытие информации → О ПИФах», ряд и доходность — из API сайта УК")

        # --- ряд СЧА и пая ---
        # Без endDate (открытый конец): адрес зависит только от startDate и не меняется, пока на сайте
        # нет новых точек, — повторный сбор берёт его из кэша/условным запросом, а не качает заново.
        start = ctx.start_for(fund.rules)
        raw = ctx.http.get(QUOTES_URL.format(fund_id=site.fund_id), fmt="json", funds=[fund.rules],
                           params={"startDate": start.isoformat(), "useCache": "false"})
        points = parse_history(raw.content(), site.fund_id)
        if not points:
            if fund.rules in ctx.last_dates:
                # докачка вернула пусто — пусть сборщик оставит прошлые точки с пометкой
                raise FetchError(raw.url, f"сайт УК не вернул точек с {start}")
            fd.status, fd.reason = "no_history", f"сайт УК не вернул ни одной точки с {start}"
            return fd
        # страница фонда (график строится из того же API); у фонда без страницы — раздел раскрытия
        fd.segments.append(self.segment(raw, points, page_url=fd.page_url, since=ctx.since))
        if date.today() - points[-1].date > STALE_AFTER:
            fd.warnings.append(f"последняя опубликованная точка {points[-1].date}")

        # --- официальная доходность ---
        try:
            ret_raw = ctx.http.get(RETURNS_URL, method="POST", json_body=[site.fund_id], fmt="json",
                                   funds=[fund.rules])
            fd.official_returns = parse_returns(ret_raw.content(), site.fund_id, RETURNS_URL)
        except FetchError as e:
            fd.warnings.append(f"официальная доходность не получена: {e.reason}")
        except UnknownFormat as e:
            # ряд оставляем, но ошибка разбора видна в отчёте качества
            ctx.log.error("[ingosstrakh] %s: доходность: %s", fund.rules, e)
            fd.errors.append(f"разбор доходности: {e}")
        if not fd.official_returns and not fd.errors:
            fd.warnings.append("УК не опубликовала доходность за 1/3/6/12 мес.")
        return fd
