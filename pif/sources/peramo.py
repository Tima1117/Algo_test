"""ООО «ПЕРАМО ИНВЕСТ» (www.peramo.ru) — один розничный фонд: ИПИФ РФИ «КОНСЕРВАТОРия»
(правила 2006-94172747). Остальные ПИФы на сайте — ЗПИФ для квалифицированных инвесторов.

Источники (1С-Битрикс, данные встроены в HTML карточки фонда):
* https://www.peramo.ru/pif/ — список ПИФов: ссылки /pif/<slug>/ (ЗПИФ пропускаем);
* https://www.peramo.ru/pif/<slug>/ — «Карточка фонда»:
  - «Номер и дата регистрации правил …: № 2006-94172747 …» — по нему сопоставляем с реестром;
  - график «Динамика доходности → Стоимость ПАЯ»: в скрипте let data = '[{"TIMESTAMP": …,
    "SHA": 2268.28}, …]' — расчётная стоимость пая на последний рабочий день каждого месяца с
    01.2015 (у интервального фонда СЧА считается раз в месяц и в даты интервалов). TIMESTAMP —
    полночь по Москве. «Загрузить в xls» — выгрузка этих же точек на стороне браузера;
  - «Изменение стоимости пая. По состоянию на 30.10.2026» — таблица: С начала года, За 1/3/6 мес.,
    За 1 год, 3, 5 лет; рядом в скрипте let income = '{"ONE_MONTH_DATA_INCOME": {"PERCENT": 0.64,
    …}}' — те же числа со знаком (в таблице знак показывает ещё и цвет стрелки). Берём значения
    из таблицы и сверяем их с income (расхождение — UnknownFormat), знак — из income.
* СЧА на сайте есть только в PDF: «Справка о стоимости чистых активов» (вкладка «Отчетность»,
  ежемесячно) и «Сообщение о СЧА и расчётной стоимости пая» (вкладка «Информационные
  сообщения»). PDF адаптер не разбирает, поэтому статус 'partial': ряд — только цена пая.
Цена — в рублях («Расчет стоимости пая, руб.»). Сообщений о выплате дохода на сайте нет — payouts пуст.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.peramo.ru"
LIST_URL = f"{BASE}/pif/"
REPORTS_URL = BASE + "/pif/{slug}/reports/"

PARTIAL_REASON = ("на сайте УК в машиночитаемом виде только расчётная стоимость пая на конец месяца "
                  "(график карточки фонда); СЧА публикуется только в PDF-справках и сообщениях о СЧА "
                  "(вкладки «Отчетность» и «Информационные сообщения»), PDF не разбираем")

_MSK = timezone(timedelta(hours=3))
_SLUG = re.compile(r"^/pif/([a-z0-9_-]+)/$")
_RULES = re.compile(r"№\s*(\d{4})\s*[-–—]\s*(\d{8})(?![-\d])")
# Подпись колонки таблицы -> (период, ключ в let income).
_PERIODS = {
    "За 1 мес.": ("1m", "ONE_MONTH_DATA_INCOME"),
    "За 3 мес.": ("3m", "THREE_MONTH_DATA_INCOME"),
    "За 6 мес.": ("6m", "SIX_MONTH_DATA_INCOME"),
    "За 1 год": ("1y", "TWELVE_MONTH_DATA_INCOME"),
}


def _soup(content) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def _script_json(html: str, var: str):
    m = re.search(rf"\blet\s+{var}\s*=\s*'(.*?)'\s*(?:;|\n|let\b)", html, re.S)
    require(m is not None, f"в карточке фонда нет скрипта let {var} = '…'")
    try:
        return json.loads(m.group(1))
    except ValueError as e:
        raise UnknownFormat(f"let {var}: не JSON: {e}") from e


# ---------- разбор ----------

def parse_fund_links(content) -> list[str]:
    """/pif/ -> slug'и открытых/интервальных фондов (ЗПИФ — только для квалов, пропускаем)."""
    slugs: list[str] = []
    for a in _soup(content).find_all("a", href=True):
        m = _SLUG.match(a["href"])
        t = _text(a)
        if m and not t.startswith("ЗПИФ") and re.match(r"^(ОПИФ|ИПИФ|БПИФ)\b", t) and m.group(1) not in slugs:
            slugs.append(m.group(1))
    require(bool(slugs), f"на {LIST_URL} нет ссылок на ОПИФ/ИПИФ/БПИФ")
    return slugs


def parse_rules(content) -> str:
    """Карточка фонда -> номер правил из строки «Номер и дата регистрации правил …»."""
    s = _soup(content)
    for td in s.find_all(["td", "th", "dt"]):
        if _text(td).startswith("Номер и дата регистрации правил"):
            val = td.find_next_sibling(["td", "dd"])
            m = _RULES.search(_text(val)) if val is not None else None
            require(m is not None, f"нет номера правил рядом с «Номер и дата регистрации правил»: {val}")
            return f"{m.group(1)}-{m.group(2)}"
    raise UnknownFormat("в карточке фонда нет строки «Номер и дата регистрации правил …»")


def parse_prices(content) -> list[SeriesPoint]:
    """let data = '[{TIMESTAMP, SHA}, …]' -> точки (только цена пая) по возрастанию даты."""
    html = content.decode("utf-8") if isinstance(content, bytes) else content
    data = _script_json(html, "data")
    require(isinstance(data, list) and data, "let data: ожидался непустой список точек графика")
    by_date: dict[date, SeriesPoint] = {}
    for item in data:
        require_keys(item, ("TIMESTAMP", "SHA"), "точка графика")
        ts = item["TIMESTAMP"]
        require(isinstance(ts, (int, float)) and not isinstance(ts, bool), f"TIMESTAMP не число: {ts!r}")
        d = datetime.fromtimestamp(ts, _MSK).date()
        price = num(item["SHA"])
        if not price or price <= 0:
            continue
        p = SeriesPoint(d, None, price)
        require(by_date.get(d, p) == p, f"две разные цены пая на {d}")
        by_date[d] = p
    return [by_date[d] for d in sorted(by_date)]


def parse_returns(content, source_url: str) -> list[OfficialReturn]:
    """«Изменение стоимости пая. По состоянию на …» + let income -> доходность 1/3/6/12 мес."""
    html = content.decode("utf-8") if isinstance(content, bytes) else content
    s = _soup(html)
    h = s.find(lambda t: t.name in ("h2", "h3", "h4") and _text(t) == "Изменение стоимости пая")
    require(h is not None, "в карточке фонда нет блока «Изменение стоимости пая»")
    block = h.find_parent("div")
    m = re.search(r"По состоянию на\s*(\d{2}\.\d{2}\.\d{4})", _text(block))
    require(m is not None, "в блоке доходности нет «По состоянию на ДД.ММ.ГГГГ»")
    as_of = day(m.group(1))
    table = block.find("table")
    require(table is not None, "в блоке доходности нет таблицы")
    trs = table.find_all("tr")
    require(len(trs) >= 2, "в таблице доходности нет строки значений")
    labels = [_text(c) for c in trs[0].find_all(["th", "td"])]
    values = [_text(c) for c in trs[1].find_all(["th", "td"])]
    require(len(labels) == len(values), f"в таблице доходности {len(labels)} подписей и {len(values)} значений")
    income = _script_json(html, "income")
    out = []
    for label, (period, key) in _PERIODS.items():
        require(label in labels, f"в таблице доходности нет колонки {label!r}: {labels}")
        shown = pct(values[labels.index(label)])
        require_keys(income.get(key), ("PERCENT",), f"let income.{key}")
        signed = pct(income[key]["PERCENT"])
        if shown is None and signed is None:
            continue
        require(shown is not None and signed is not None and abs(abs(shown) - abs(signed)) < 0.005,
                f"{period}: в таблице {shown}, в let income {signed}")
        out.append(OfficialReturn(period, signed, as_of, source_url))
    return out


# ---------- адаптер ----------

class PeramoAdapter(Adapter):
    key = "peramo"
    title = "ПЕРАМО ИНВЕСТ"
    uk_inns = ("7716618947",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def cards() -> tuple[dict[str, tuple[str, object]], list[Exception]]:
            """rules -> (slug, сырая карточка). Сбой одной карточки не мешает остальным."""
            if "cards" not in state:
                slugs = parse_fund_links(ctx.http.get(LIST_URL, fmt="html", funds=all_rules).content())
                found, errors = {}, []
                for slug in slugs:
                    try:
                        raw = ctx.http.get(f"{BASE}/pif/{slug}/", fmt="html", funds=all_rules)
                        found.setdefault(parse_rules(raw.content()), (slug, raw))
                    except Exception as e:  # noqa: BLE001
                        ctx.log.warning("[peramo] карточка %s: %s", slug, e)
                        errors.append(e)
                state["cards"] = (found, errors)
            return state["cards"]

        def one(fund: Fund) -> FundData:
            found, errors = cards()
            if fund.rules not in found:
                if errors:
                    raise errors[0]          # не все карточки проверены — это не «не найден»
                return FundData(fund.rules, status="not_found", page_url=LIST_URL,
                                reason=f"на {LIST_URL} нет фонда с правилами № {fund.rules}")
            slug, raw = found[fund.rules]
            page_url = f"{BASE}/pif/{slug}/"
            points = parse_prices(raw.content())
            fd = FundData(fund.rules, currency="RUB", page_url=page_url,
                          segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)],
                          official_returns=parse_returns(raw.content(), page_url))
            if fd.segments[0].points:
                fd.status, fd.reason = "partial", f"{PARTIAL_REASON}: {REPORTS_URL.format(slug=slug)}"
            else:
                fd.status, fd.reason = "no_history", f"на графике карточки нет точек с {ctx.since:%d.%m.%Y}"
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]
