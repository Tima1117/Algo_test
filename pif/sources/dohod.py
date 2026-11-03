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
* <код>/unit-value — «Прирост расчетной стоимости пая согласно 5609-У». Для БПИФ это единственная
  доходность по расчётной стоимости: на странице БПИФ таблица «Прирост биржевой стоимости пая» — по цене биржи.
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

# Метки времени в JSON графика: у ОПИФ — полночь UTC, у БПИФ — полночь по Москве (21:00 UTC
# предыдущего дня). Дату берём по московскому времени: так оба варианта дают правильный день.
MSK = timezone(timedelta(hours=3))

_FUND_HREF = re.compile(r"^/?individuals/mutual-funds/(open-funds|bpif)/[a-z0-9-]+/?$")
_DISCLOSURE_HREF = re.compile(r"information-disclosure/mutual-fund-reports/([A-Za-z0-9_-]+)/"
                              r"(?:official-information|unit-value)")
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
# заголовок колонки таблицы доходности -> период OfficialReturn и его длина в месяцах
_PERIODS = {"1 мес.": ("1m", 1), "3 мес.": ("3m", 3), "6 мес.": ("6m", 6), "1 год": ("1y", 12)}
_CURRENCY_SIGNS = {"₽": "RUB", "$": "USD", "€": "EUR", "¥": "CNY"}

WARN_5609 = ("официальная доходность взята из раздела «Прирост расчетной стоимости пая согласно 5609-У»: "
             "сайт считает её от последнего рабочего дня месяца, отстоящего на n месяцев от текущего, "
             "до даты «информация актуальна на», а не за n полных месяцев на конец прошлого месяца "
             "(сверено с рядом пая; в начале месяца «1 мес.» почти равен изменению за последние дни)")


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


def parse_chart(content: bytes | str | dict, what: str = "ряд") -> list[tuple[date, float]]:
    """JSON графика {"items": [[epoch_ms, value], ...]} -> [(дата, значение)] по возрастанию даты.

    Ноль на графике — это «нет значения» (расчётная стоимость и СЧА фонда не бывают нулевыми):
    такую точку пропускаем, а не записываем 0. Отрицательное значение — незнакомые данные.
    """
    data = content if isinstance(content, dict) else json.loads(content)
    require_keys(data, ["items"], f"{what}: JSON графика")
    items = data["items"]
    require(isinstance(items, list), f"{what}: items — не список (а {type(items).__name__}); "
                                     "для БПИФ нужен action=costdynamics/opif, а не bpif")
    out: dict[date, float] = {}
    for it in items:
        require(isinstance(it, list) and len(it) == 2, f"{what}: элемент не [время, значение]: {it!r}")
        d, v = _msk_day(it[0]), num(it[1])
        if v is None or v == 0:
            continue
        require(v > 0, f"{what}: отрицательное значение {v} на {d}")
        require(out.get(d, v) == v, f"{what}: два разных значения на {d}")
        out[d] = v
    return sorted(out.items())


def parse_series(pai: bytes | str | dict, cha: bytes | str | dict) -> list[SeriesPoint]:
    """Два ряда графика (пай и СЧА) -> точки по датам; чего нет в одном из рядов — None."""
    prices = dict(parse_chart(pai, "стоимость пая"))
    navs = dict(parse_chart(cha, "СЧА"))
    return [SeriesPoint(d, navs.get(d), prices.get(d)) for d in sorted(set(prices) | set(navs))]


def _signed(cell) -> float | None:
    """Ячейка таблицы доходности: модуль числа + знак по классу (стрелка loss/profit или --loss)."""
    value = cell.select_one(".value")
    text = value.get_text(strip=True) if value is not None else ""
    if not text:
        text = cell.get_text(" ", strip=True).replace("↑", "").replace("↓", "").strip()
    v = num(text)
    if v is None:
        return None
    classes = set(cell.get("class", []))
    arrow = cell.select_one(".arrow")
    if arrow is not None:
        classes |= {f"arrow-{c}" for c in arrow.get("class", [])}
    if "arrow-loss" in classes or "charts__table-cell--loss" in classes:
        return -abs(v)
    return v


def _growth_table(table) -> dict[str, float | None]:
    heads = [c.get_text(" ", strip=True) for c in table.select(".charts__table-cell--head")]
    cells = [c for c in table.select(".charts__table-cell") if "charts__table-cell--head" not in c.get("class", [])]
    # в таблице 5609-У перед значениями есть подпись строки «Прирост стоимости пая, %»
    if heads and heads[0] == "Показатель":
        heads, cells = heads[1:], cells[1:]
    require(len(heads) == len(cells), f"в таблице доходности {len(heads)} заголовков и {len(cells)} значений")
    missing = [h for h in _PERIODS if h not in heads]
    require(not missing, f"в таблице доходности нет колонок {missing}; есть {heads}")
    return {h: _signed(c) for h, c in zip(heads, cells)}


def _to_returns(values: dict[str, float | None], as_of: date, url: str) -> list[OfficialReturn]:
    return [OfficialReturn(period, values[head], as_of, url)
            for head, (period, _) in _PERIODS.items() if values.get(head) is not None]


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


def parse_returns_5609(content: bytes | str, url: str = "") -> list[OfficialReturn]:
    """Раздел раскрытия unit-value: «Прирост расчетной стоимости инвестиционного пая согласно 5609-У»."""
    soup = _soup(content)
    when = soup.find(string=re.compile(r"информация актуальна на"))
    require(when is not None, "нет строки «информация актуальна на»")
    as_of = _ru_date(when.find_next(string=re.compile(r"\d{2}\.\d{2}\.\d{4}")) or "")
    head = soup.find(string=re.compile(r"Прирост расчетной стоимости инвестиционного пая"))
    require(head is not None, "нет заголовка «Прирост расчетной стоимости инвестиционного пая»")
    table = head.find_next(class_="charts__table-growth")
    require(table is not None, "нет таблицы прироста 5609-У")
    return _to_returns(_growth_table(table), as_of, url)


def drop_too_young(returns: list[OfficialReturn], first_day: date) -> tuple[list[OfficialReturn], list[str]]:
    """Убрать периоды длиннее истории фонда: таблица 5609-У ставит там 0, это не доходность."""
    months = {p: n for p, n in _PERIODS.values()}
    kept, dropped = [], []
    for r in returns:
        if r.as_of is not None and _months_back(r.as_of, months[r.period]) < first_day:
            dropped.append(r.period)
        else:
            kept.append(r)
    return kept, dropped


def _months_back(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


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
        prices = parse_chart(raw_pai.content(), "стоимость пая")
        navs = parse_chart(raw_cha.content(), "СЧА")
        first_day = min([d for d, _ in prices + navs], default=None)
        fd.segments = [
            self.segment(raw_pai, [SeriesPoint(d, None, v) for d, v in prices], page.url, ctx.since),
            self.segment(raw_cha, [SeriesPoint(d, v, None) for d, v in navs], page.url, ctx.since),
        ]
        n_price, n_nav = (len(s.points) for s in fd.segments)

        # официальная доходность: у ОПИФ — таблица на странице фонда, у БПИФ — раздел 5609-У
        returns = parse_returns_fund_page(raw_page.content(), page.url)
        if returns is None:
            uv_url = DISCLOSURE_URL.format(code=page.code, page="unit-value")
            returns = parse_returns_5609(ctx.http.get(uv_url, fmt="html", funds=rules).content(), uv_url)
            if returns:
                fd.warnings.append(WARN_5609)
        if first_day is not None:
            returns, dropped = drop_too_young(returns, first_day)
            if dropped:
                fd.warnings.append(f"доходность за {', '.join(dropped)} не взята: фонд моложе периода "
                                   f"(история с {first_day:%d.%m.%Y})")
        fd.official_returns = returns

        if not n_price and not n_nav:
            fd.status = "not_formed" if fund.status == "Формируется" else "no_history"
            fd.reason = (f"график на сайте УК не содержит точек с {ctx.since:%d.%m.%Y}"
                         + (f" (последняя: {max(d for d, _ in prices + navs):%d.%m.%Y})" if prices or navs else ""))
        elif not n_nav or not n_price:
            fd.status = "partial"
            fd.reason = "на сайте УК есть только " + ("стоимость пая, без СЧА" if not n_nav else "СЧА, без стоимости пая")
        return fd
