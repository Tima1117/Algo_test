"""ООО «СК «Ренессанс Жизнь» (www.renlife.ru) — страховщик с лицензией УК; ОПИФ для долевого
страхования жизни.

Источники (обычный HTML, без API и без параметров в адресе — robots.txt сайта запрещает «*?»):
* список фондов — https://www.renlife.ru/company/info_disclosure/raskrytie-informatsii-ob-upravlyayushchey-kompanii/
  (блок .opif-list: ссылки /funds/<slug>/ на все пять фондов, включая формирующиеся;
  каталог /funds/ показывает только сформированные, поэтому не годится);
* страница фонда — https://www.renlife.ru/funds/<slug>/ (grow_your_capital, balanced, cumulative,
  technologies, finance). Вкладка «Динамика стоимости» → «Таблица стоимости пая и СЧА»:
  HTML-таблица .fund-dynamic__table-cost со ВСЕЙ историей с даты завершения формирования
  (графики на странице строятся из неё же, отдельного запроса за данными нет). Колонки:
  «Дата», «Расчетная стоимость инвестиционного пая (РСИП), руб.», «Изменение РСИП за день, %»,
  «Стоимость чистых активов (СЧА), руб.», «Изменение СЧА за день, %». Частота — каждый рабочий
  день; СЧА и пай — в рублях (как есть, без множителей), числа с точкой. Строки — от новых к старым.
  Диапазон дат не задаётся: страница всегда отдаёт ряд целиком (ctx.start_for не нужен).
  У формирующегося фонда вместо таблицы во вкладке стоит «Данные обновляются».

Сопоставление: название из списка ↔ краткое название из реестра (norm_text), затем номер правил
из строки «Правила Фонда» на странице фонда обязан совпасть с rules — иначе 'not_found'.

Официальная доходность: на HTML-страницах фондов её нет (только «ожидаемая доходность» по методике).
Доходность за периоды есть только в ежемесячном КИД (PDF во вкладке «Документы и отчетность»);
PDF не разбираем (нет pdf-библиотеки в зависимостях, а robots.txt сайта закрывает «/*.pdf$»),
поэтому official_returns пустой + предупреждение.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require
from .base import Adapter, Context

BASE = "https://www.renlife.ru"
LIST_URL = f"{BASE}/company/info_disclosure/raskrytie-informatsii-ob-upravlyayushchey-kompanii/"

NO_RETURNS_WARNING = (
    "официальная доходность за 1/3/6/12 мес на странице фонда не публикуется — только в ежемесячном "
    "КИД (PDF); PDF не разбираем, доходность не заполнена")

# Насколько позже начала ряда (since / завершения формирования) может стоять первая точка,
# чтобы историю всё ещё считать полной: праздники, первая дата расчёта после формирования.
_START_SLACK = timedelta(days=7)


def _soup(content: bytes | str) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _cell(tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True).replace("\xa0", " ")).strip()


# ---------- разбор ----------

def parse_fund_list(content: bytes | str) -> dict[str, tuple[str, str]]:
    """Страница раскрытия информации УК -> {norm_text(название): (название, абсолютный URL фонда)}."""
    soup = _soup(content)
    links = [a for block in soup.select("div.opif-list") for a in block.select("a[href]")
             if "/funds/" in a["href"]]
    require(bool(links), "на странице раскрытия информации нет списка фондов (.opif-list a[href*=/funds/])")
    out: dict[str, tuple[str, str]] = {}
    for a in links:
        title = _cell(a)
        key = norm_text(title)
        require(bool(key), f"пустое название фонда у ссылки {a['href']!r}")
        url = urljoin(BASE, a["href"])
        if key in out and out[key][1] != url:
            raise UnknownFormat(f"два фонда с одним названием «{title}»: {out[key][1]} и {url}")
        out[key] = (title, url)
    return out


def parse_fund_rules(content: bytes | str) -> str:
    """Номер правил ДУ из строки «Правила Фонда» на странице фонда."""
    soup = _soup(content)
    for td in soup.select("table td"):
        if _cell(td) != "Правила Фонда":
            continue
        value = td.find_next_sibling("td")
        require(value is not None, "у строки «Правила Фонда» нет значения")
        m = re.search(r"№\s*(\d+)", _cell(value))
        require(m is not None, f"в строке «Правила Фонда» нет номера правил: {_cell(value)!r}")
        return m.group(1)
    raise UnknownFormat("на странице фонда нет строки «Правила Фонда»")


def _dynamics_tab(soup: BeautifulSoup):
    tab = soup.select_one('div.tab-content[data-tab="price-dynamics"]')
    require(tab is not None, "на странице фонда нет вкладки «Динамика стоимости» (data-tab=price-dynamics)")
    return tab


def parse_dynamics_placeholder(content: bytes | str) -> str | None:
    """Текст-заглушка вкладки «Динамика стоимости», если таблицы нет (у формирующихся фондов —
    «Данные обновляются»); None, если таблица есть. Пустая вкладка без таблицы — UnknownFormat."""
    tab = _dynamics_tab(_soup(content))
    if tab.select_one(".fund-dynamic__table-cost table") is not None:
        return None
    text = _cell(tab)
    require(bool(text), "во вкладке «Динамика стоимости» нет ни таблицы, ни пояснения")
    return text


def _column(headers: list[str], must: str, label: str) -> int:
    """Индекс колонки значения (не «Изменение … %»), в заголовке которой есть must."""
    idx = [i for i, h in enumerate(headers) if must in h and "%" not in h and "Изменение" not in h]
    require(len(idx) == 1, f"в таблице не найдена однозначно колонка «{label}»; заголовки: {headers}")
    h = headers[idx[0]]
    # Единицы: ждём рубли без множителя; «тыс. руб.» / «млн руб.» / другая валюта — новый формат.
    require(h.endswith("руб.") and not re.search(r"(тыс|млн|млрд)", h),
            f"колонка «{label}»: ожидались рубли, заголовок {h!r}")
    return idx[0]


def parse_series(content: bytes | str) -> list[SeriesPoint]:
    """Таблица «Стоимость пая и чистых активов» на странице фонда -> точки по возрастанию даты."""
    tab = _dynamics_tab(_soup(content))
    tables = tab.select(".fund-dynamic__table-cost table")
    require(len(tables) == 1, f"ожидалась одна таблица стоимости пая и СЧА, найдено {len(tables)}")
    table = tables[0]
    headers = [_cell(th) for th in table.select("thead th")]
    require(bool(headers) and headers[0] == "Дата", f"первая колонка таблицы не «Дата»: {headers}")
    i_price = _column(headers, "РСИП", "Расчетная стоимость пая")
    i_nav = _column(headers, "СЧА", "Стоимость чистых активов")
    rows = table.select("tbody tr")
    require(bool(rows), "таблица стоимости пая и СЧА пуста")
    points: dict[date, SeriesPoint] = {}
    for tr in rows:
        cells = [_cell(td) for td in tr.find_all("td")]
        require(len(cells) == len(headers), f"в строке {len(cells)} ячеек при {len(headers)} колонках: {cells}")
        d = day(cells[0])
        price, nav = num(cells[i_price]), num(cells[i_nav])
        require(price is not None or nav is not None, f"{d}: нет ни цены пая, ни СЧА")
        for name, v in (("цена пая", price), ("СЧА", nav)):
            require(v is None or v > 0, f"{d}: {name} не положительная: {v}")
        # Дубль даты на этом сайте не встречался — если появится, разбираться руками.
        require(d not in points, f"дата {d} встречается в таблице дважды")
        points[d] = SeriesPoint(date=d, nav=nav, price=price)
    return [points[d] for d in sorted(points)]


# ---------- адаптер ----------

class RenlifeAdapter(Adapter):
    key = "renlife"
    title = "СК «Ренессанс Жизнь»"
    uk_inns = ("7725520440",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        state: dict = {}

        def catalog() -> dict[str, tuple[str, str]]:
            # Один запрос на всю УК; ошибку запоминаем и повторяем для каждого фонда,
            # чтобы guarded() поставил каждому свой статус (blocked / unavailable / parse_error).
            if "error" in state:
                raise state["error"]
            if "list" not in state:
                try:
                    raw = ctx.http.get(LIST_URL, fmt="html", funds=[f.rules for f in funds])
                    state["list"] = parse_fund_list(raw.content())
                except Exception as e:  # noqa: BLE001 — статус определит guarded()
                    state["error"] = e
                    raise
            return state["list"]

        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, catalog())) for f in funds]

    def _one(self, ctx: Context, fund: Fund, listing: dict[str, tuple[str, str]]) -> FundData:
        found = listing.get(norm_text(fund.name))
        if found is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"фонда «{fund.name}» нет в списке ПИФ на странице раскрытия информации УК "
                                   f"({LIST_URL})")
        _, url = found
        raw = ctx.http.get(url, fmt="html", funds=[fund.rules])
        html = raw.content()
        rules = parse_fund_rules(html)
        if rules != fund.rules:
            return FundData(fund.rules, page_url=url, status="not_found",
                            reason=f"по названию нашлась страница {url}, но на ней правила № {rules}, "
                                   f"а не № {fund.rules}")

        placeholder = parse_dynamics_placeholder(html)
        if placeholder is not None:
            if fund.status == "Формируется":
                return FundData(fund.rules, page_url=url, status="not_formed",
                                reason=f"фонд формируется: на странице фонда нет СЧА и стоимости пая "
                                       f"(во вкладке «Динамика стоимости» — «{placeholder}»)")
            return FundData(fund.rules, page_url=url, status="no_history",
                            reason=f"на странице фонда нет таблицы СЧА и стоимости пая "
                                   f"(во вкладке «Динамика стоимости» — «{placeholder}»)")

        points = parse_series(html)
        fd = FundData(fund.rules, currency="RUB", page_url=url,
                      segments=[self.segment(raw, points, page_url=url, since=ctx.since)],
                      warnings=[NO_RETURNS_WARNING])
        # История сформированного фонда обязана начинаться с начала окна (или с завершения
        # формирования для молодых фондов). У формирующегося фонда ряд с первой же даты расчёта.
        if fund.status == "Формируется":
            return fd
        expected = ctx.since
        if fund.formation_end:
            expected = max(expected, date.fromisoformat(fund.formation_end))
        first = points[0].date
        if first > expected + _START_SLACK:
            fd.status = "partial"
            fd.reason = (f"таблица на странице фонда начинается с {first:%d.%m.%Y}, "
                         f"а ожидалась история с {expected:%d.%m.%Y}")
        return fd
