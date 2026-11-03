"""УК «ДОХОДЪ» (www.dohod.ru): история СЧА и расчётной стоимости пая, официальная доходность.

Где что лежит на сайте (разведка 03.11.2026):
* списки фондов — /individuals/mutual-funds/open-funds и /individuals/mutual-funds/bpif/
  (карточки a.products__slide / a.js-stocks-slide со ссылкой на страницу фонда);
* страница фонда — график «Динамика значений» (div.js-stock-chart с data-res-id), ссылки на раздел
  раскрытия information-disclosure/mutual-fund-reports/<код>/…, блок «СЧА … ₽»; у ОПИФ ещё таблица
  «Прирост расчетной стоимости пая на <дата>» (1 день, 1/3/6 мес., 1 год…);
* <код>/official-information — «Номер правил доверительного управления»: по нему сопоставляем с реестром;
* JSON графика connectorweb.php?type=&tab=pai|cha&action=costdynamics/opif&resId=<id> —
  {"items": [[epoch_ms, значение], …]}: вся история с запуска фонда, по рабочим дням;
  pai — расчётная стоимость пая, cha — СЧА, оба в рублях. На странице БПИФ график рисует только цены
  (action=costdynamics/bpif), но тот же costdynamics/opif отдаёт и для БПИФ ряды СЧА и пая;
  значения совпадают с таблицей 5609-У на сайте (GROD: 30.10.2026 — 747,06 / 193 552 721,89).
  Диапазон дат источник не принимает — всегда отдаёт всю историю (60–90 КБ на ряд).

Официальная доходность. У ОПИФ — таблица «Прирост расчетной стоимости пая на <дата>» со страницы фонда:
окна календарные (1 мес. назад от даты таблицы и т. д.), значения совпадают с рядом пая до 0,01 п. п.
(«Дивидендные акции. Россия» на 02.11.2026: 7,93 / 7,08 / −1,34 / 5,29). Знак — только в классе
стрелки (arrow profit/loss), само число без минуса.
У БПИФ сопоставимой цифры нет: на странице фонда «Прирост биржевой стоимости пая» (по цене биржи),
а раздел <код>/unit-value «Прирост расчетной стоимости пая согласно 5609-У» считает доходность от
последнего рабочего дня месяца, отстоящего на n месяцев от текущего, до текущей даты (GROD на 02.11.2026:
«1 мес.» = −1,46 % — это изменение за один день с 30.10, «3 мес.» — с 31.08 и т. д.; проверено по ряду).
Такие числа не являются доходностью за 1/3/6/12 мес., поэтому для БПИФ official_returns пустые,
а в warnings — объяснение и ссылка.

Выплат дохода пайщикам у фондов УК нет (БПИФ реинвестируют дивиденды) — payouts пустые.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, num, require, require_keys
from .base import Adapter, Context

BASE = "https://www.dohod.ru/"
LIST_PAGES = (
    "https://www.dohod.ru/individuals/mutual-funds/open-funds",
    "https://www.dohod.ru/individuals/mutual-funds/bpif/",
)
CHART_URL = "https://www.dohod.ru/assets/components/dohod/connectorweb.php"
DISCLOSURE_URL = "https://www.dohod.ru/information-disclosure/mutual-fund-reports/{code}/{page}"

# Метки времени в JSON графика: в costdynamics/opif — полночь UTC, в costdynamics/bpif (график цен БПИФ) —
# полночь по Москве, то есть 21:00 UTC предыдущего дня. Дату берём по московскому времени: так оба
# варианта дают правильный день (utcfromtimestamp сдвинул бы ряды БПИФ на день назад).
MSK = timezone(timedelta(hours=3))

_FUND_HREF = re.compile(r"^/?individuals/mutual-funds/(open-funds|bpif)/[a-z0-9-]+/?$")
_DISCLOSURE_HREF = re.compile(r"information-disclosure/mutual-fund-reports/([A-Za-z0-9_-]+)/"
                              r"(?:official-information|unit-value)")
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
# заголовок колонки таблицы доходности -> период OfficialReturn
_PERIODS = {"1 мес.": "1m", "3 мес.": "3m", "6 мес.": "6m", "1 год": "1y"}
_CURRENCY_SIGNS = {"₽": "RUB", "$": "USD", "€": "EUR", "¥": "CNY"}

WARN_BPIF = ("официальная доходность по расчётной стоимости пая не взята: на странице БПИФ УК публикует "
             "прирост биржевой цены, а в разделе «Прирост расчетной стоимости пая согласно 5609-У» ({url}) "
             "значения посчитаны от конца месяца, отстоящего на n месяцев, до текущей даты, а не за полные "
             "1/3/6/12 мес. (сверено с рядом пая); доходность за период на конец месяца есть в КИД (PDF, "
             "вкладка «Документация по фонду»), PDF не разбираем")


# ---------------------------------------------------------------- разбор

def _soup(content: bytes | str) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return BeautifulSoup(content, "lxml")


def _msk_day(ms) -> date:
    require(isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 10**11,
            f"ожидалась метка времени в мс, получено {ms!r}")
    return datetime.fromtimestamp(ms / 1000, MSK).date()


def _ru_date(text: str) -> date:
    """«на 02 ноября 2026, в %» / «02.11.2026» -> date."""
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text)
    if m and m.group(2) in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise UnknownFormat(f"не нашёл дату в {text!r}")


def parse_fund_links(content: bytes | str) -> list[str]:
    """Страница-список фондов -> абсолютные адреса страниц фондов (карточки в каталоге)."""
    soup = _soup(content)
    links = []
    for a in soup.select("a.products__slide[href], a.js-stocks-slide[href]"):
        href = a["href"].strip()
        if _FUND_HREF.match(href):
            url = urljoin(BASE, href)
            if url not in links:
                links.append(url)
    require(bool(links), "на странице-списке нет карточек фондов (a.products__slide / a.js-stocks-slide)")
    return links


@dataclass
class FundPage:
    url: str
    title: str
    res_id: str
    code: str               # код фонда в разделе раскрытия информации
    currency: str
    rules: str | None = None


def parse_fund_page(content: bytes | str, url: str = "") -> FundPage:
    """Страница фонда -> resId графика, код раздела раскрытия, валюта (по знаку у СЧА)."""
    soup = _soup(content)
    charts = soup.select("div.js-stock-chart[data-res-id]")
    require(bool(charts), "на странице фонда нет графика с data-res-id")
    res_ids = {c["data-res-id"].strip() for c in charts}
    require(len(res_ids) == 1 and all(r.isdigit() for r in res_ids),
            f"неоднозначный data-res-id графика: {sorted(res_ids)}")
    codes = set(_DISCLOSURE_HREF.findall(str(soup)))
    require(len(codes) == 1, f"ожидалась одна ссылка на раздел раскрытия фонда, нашёл {sorted(codes)}")
    h1 = soup.find("h1")
    require(h1 is not None, "на странице фонда нет заголовка h1")
    currency = None
    for head in soup.select(".about-header"):
        if head.get_text(strip=True) == "СЧА":
            info = head.find_next_sibling(class_="about-info")
            text = info.get_text(" ", strip=True) if info else ""
            currency = next((cur for sign, cur in _CURRENCY_SIGNS.items() if sign in text), None)
            break
    require(currency is not None, "не нашёл блок «СЧА» со знаком валюты на странице фонда")
    return FundPage(url=url, title=h1.get_text(" ", strip=True), res_id=res_ids.pop(),
                    code=codes.pop(), currency=currency)


def parse_rules_number(content: bytes | str) -> str:
    """Раздел раскрытия official-information -> номер правил ДУ («4444», «0238-58232991»)."""
    soup = _soup(content)
    label = soup.find(string=re.compile(r"^\s*Номер правил доверительного управления\s*$"))
    require(label is not None, "на странице раскрытия нет поля «Номер правил доверительного управления»")
    value = label.find_next(string=lambda s: bool(s and s.strip()))
    require(value is not None, "пустое поле «Номер правил доверительного управления»")
    m = re.fullmatch(r"(\d{4}(?:-\d{8})?)", value.strip())
    require(m is not None, f"номер правил в незнакомом виде: {value.strip()!r}")
    return m.group(1)


def parse_chart(content: bytes | str | dict, what: str = "ряд") -> tuple[list[tuple[date, float]], list[date]]:
    """JSON графика {"items": [[epoch_ms, value], ...]} -> ([(дата, значение)] по возрастанию, спорные даты).

    В выгрузке бывают повторы одной даты (одинаковые строки — склеиваем). Если за дату пришли
    разные значения (у «Первого эшелона» 04.09.2023: 3179,12 и 3191,83 — видимо, пересчёт), какое из них
    верное, из выгрузки не понять: дату исключаем и возвращаем во втором списке.
    Ноль — это «нет значения» (расчётная стоимость и СЧА не бывают нулевыми): точку пропускаем,
    а не записываем 0. Отрицательное значение — незнакомые данные.
    """
    data = content if isinstance(content, dict) else json.loads(content)
    require_keys(data, ["items"], f"{what}: JSON графика")
    items = data["items"]
    require(isinstance(items, list), f"{what}: items — не список (а {type(items).__name__}); "
                                     "для БПИФ нужен action=costdynamics/opif, а не bpif")
    values: dict[date, set[float]] = {}
    for it in items:
        require(isinstance(it, list) and len(it) == 2, f"{what}: элемент не [время, значение]: {it!r}")
        d, v = _msk_day(it[0]), num(it[1])
        if v is None or v == 0:
            continue
        require(v > 0, f"{what}: отрицательное значение {v} на {d}")
        values.setdefault(d, set()).add(v)
    points = [(d, next(iter(vs))) for d, vs in sorted(values.items()) if len(vs) == 1]
    return points, sorted(d for d, vs in values.items() if len(vs) > 1)


def parse_series(pai: bytes | str | dict, cha: bytes | str | dict) -> list[SeriesPoint]:
    """Два ряда графика (пай и СЧА) -> точки по датам; чего нет в одном из рядов — None."""
    prices = dict(parse_chart(pai, "стоимость пая")[0])
    navs = dict(parse_chart(cha, "СЧА")[0])
    return [SeriesPoint(d, navs.get(d), prices.get(d)) for d in sorted(set(prices) | set(navs))]


def _signed(cell) -> float | None:
    """Ячейка таблицы доходности: число без знака + стрелка arrow profit/loss -> число со знаком."""
    value = cell.select_one(".value")
    text = value.get_text(strip=True) if value is not None else ""
    if not text:
        text = cell.get_text(" ", strip=True).replace("↑", "").replace("↓", "").strip()
    v = num(text)
    if v is None:
        return None
    arrow = cell.select_one(".arrow")
    require(arrow is not None, f"у значения доходности {text!r} нет стрелки со знаком")
    kind = set(arrow.get("class", [])) & {"profit", "loss"}
    require(len(kind) == 1, f"стрелка доходности без класса profit/loss: {arrow.get('class')}")
    return -abs(v) if kind == {"loss"} else abs(v)


def _growth_table(table) -> dict[str, float | None]:
    heads = [c.get_text(" ", strip=True) for c in table.select(".charts__table-cell--head")]
    cells = [c for c in table.select(".charts__table-cell") if "charts__table-cell--head" not in c.get("class", [])]
    require(len(heads) == len(cells), f"в таблице доходности {len(heads)} заголовков и {len(cells)} значений")
    missing = [h for h in _PERIODS if h not in heads]
    require(not missing, f"в таблице доходности нет колонок {missing}; есть {heads}")
    return {h: _signed(c) for h, c in zip(heads, cells)}


def _to_returns(values: dict[str, float | None], as_of: date, url: str) -> list[OfficialReturn]:
    # «-» в ячейке (фонд моложе периода) -> None -> периода нет
    return [OfficialReturn(period, values[head], as_of, url)
            for head, period in _PERIODS.items() if values.get(head) is not None]


def parse_returns_fund_page(content: bytes | str, url: str = "") -> list[OfficialReturn] | None:
    """Таблица «Прирост расчетной стоимости пая на <дата>» со страницы ОПИФ.

    None — если на странице только «Прирост биржевой стоимости пая» (БПИФ: доходность по цене биржи,
    к расчётной стоимости отношения не имеет).
    """
    soup = _soup(content)
    titles = [t for t in soup.select("h3.title_text") if t.get_text(" ", strip=True).startswith("Прирост")]
    require(bool(titles), "на странице фонда нет блока «Прирост … стоимости пая»")
    title = next((t for t in titles if "расчетной стоимости" in t.get_text(" ", strip=True)), None)
    if title is None:
        return None
    block = title.find_parent(class_="accordion__item")
    require(block is not None, "блок доходности не внутри accordion__item")
    when = block.find(string=re.compile(r"на\s+\d{1,2}\s+[а-яё]+\s+\d{4}"))
    require(when is not None, "у таблицы доходности нет даты «на DD месяца YYYY»")
    table = block.select_one('[class^="charts__table-growth"]')
    require(table is not None, "в блоке доходности нет таблицы")
    return _to_returns(_growth_table(table), _ru_date(when), url)


# ---------------------------------------------------------------- сбор

@dataclass
class _Catalog:
    by_rules: dict[str, FundPage] = field(default_factory=dict)
    problems: list[Exception] = field(default_factory=list)   # страницы фондов, которые не удалось разобрать


class DohodAdapter(Adapter):
    key = "dohod"
    title = "УК «ДОХОДЪ»"
    uk_inns = ("7826685368",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        failure: Exception | None = None
        catalog = _Catalog()
        try:
            catalog = self._catalog(ctx, [f.rules for f in funds])
        except Exception as e:  # noqa: BLE001 — превратится в статус каждого фонда через guarded()
            failure = e
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, catalog, failure)) for f in funds]

    def _catalog(self, ctx: Context, all_rules: list[str]) -> _Catalog:
        """Страницы фондов с сайта и номер правил каждого (из раздела раскрытия)."""
        catalog = _Catalog()
        pages: list[str] = []
        for url in LIST_PAGES:
            for link in parse_fund_links(ctx.http.get(url, fmt="html", funds=all_rules).content()):
                if link not in pages:
                    pages.append(link)
        for url in pages:
            try:
                page = parse_fund_page(ctx.http.get(url, fmt="html").content(), url)
                info = ctx.http.get(DISCLOSURE_URL.format(code=page.code, page="official-information"), fmt="html")
                page.rules = parse_rules_number(info.content())
            except Exception as e:  # noqa: BLE001 — одна битая страница не мешает остальным
                ctx.log.warning("[dohod] %s: %s", url, e)
                catalog.problems.append(e)
                continue
            if page.rules in catalog.by_rules:
                raise UnknownFormat(f"номер правил {page.rules} у двух страниц: "
                                    f"{catalog.by_rules[page.rules].url} и {url}")
            catalog.by_rules[page.rules] = page
        return catalog

    def _fund(self, ctx: Context, fund: Fund, catalog: _Catalog, failure: Exception | None) -> FundData:
        if failure is not None:
            raise failure
        page = catalog.by_rules.get(fund.rules)
        if page is None:
            if catalog.problems:
                # фонд мог быть на странице, которая не загрузилась — честнее показать причину
                raise catalog.problems[0]
            status = "not_formed" if fund.status == "Формируется" else "not_found"
            return FundData(fund.rules, status=status,
                            reason=f"на {BASE} нет страницы фонда с правилами № {fund.rules}")
        rules = [fund.rules]
        # страницы уже скачаны при сборе каталога: повторный get берёт их из кэша
        # и заодно помечает в манифесте, к какому фонду они относятся
        raw_page = ctx.http.get(page.url, fmt="html", funds=rules)
        ctx.http.get(DISCLOSURE_URL.format(code=page.code, page="official-information"), fmt="html", funds=rules)

        fd = FundData(fund.rules, currency=page.currency, page_url=page.url)
        params = {"type": "", "action": "costdynamics/opif", "resId": page.res_id}
        raw_pai = ctx.http.get(CHART_URL, params={**params, "tab": "pai"}, fmt="json", funds=rules)
        raw_cha = ctx.http.get(CHART_URL, params={**params, "tab": "cha"}, fmt="json", funds=rules)
        prices, bad_p = parse_chart(raw_pai.content(), "стоимость пая")
        navs, bad_n = parse_chart(raw_cha.content(), "СЧА")
        for what, bad in (("стоимости пая", bad_p), ("СЧА", bad_n)):
            bad = [d for d in bad if d >= ctx.since]
            if bad:
                fd.warnings.append(f"на графике УК за {', '.join(f'{d:%d.%m.%Y}' for d in bad)} два разных значения "
                                   f"{what} — эти даты исключены")
        fd.segments = [
            self.segment(raw_pai, [SeriesPoint(d, None, v) for d, v in prices], page.url, ctx.since),
            self.segment(raw_cha, [SeriesPoint(d, v, None) for d, v in navs], page.url, ctx.since),
        ]
        n_price, n_nav = (len(s.points) for s in fd.segments)

        # официальная доходность: у ОПИФ — таблица на странице фонда; у БПИФ сопоставимой нет
        returns = parse_returns_fund_page(raw_page.content(), page.url)
        if returns is None:
            fd.warnings.append(WARN_BPIF.format(url=DISCLOSURE_URL.format(code=page.code, page="unit-value")))
        fd.official_returns = returns or []

        if not n_price and not n_nav:
            last = max((d for d, _ in prices + navs), default=None)
            fd.status = "not_formed" if fund.status == "Формируется" else "no_history"
            fd.reason = (f"график на сайте УК не содержит точек с {ctx.since:%d.%m.%Y}"
                         + (f" (последняя: {last:%d.%m.%Y})" if last else ""))
        elif not n_nav or not n_price:
            fd.status = "partial"
            fd.reason = "на сайте УК есть только " + ("стоимость пая, без СЧА" if not n_nav else "СЧА, без стоимости пая")
        return fd
