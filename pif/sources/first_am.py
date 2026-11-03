"""АО УК «Первая» (Сбер, www.first-am.ru) — открытые, интервальный и биржевые фонды.

Источники (обычный HTML, без параметров в адресе — robots.txt сайта запрещает «/*?»):
* страница фонда — https://www.first-am.ru/individuals/fund/<slug> (ОПИФ/ИПИФ) или
  https://www.first-am.ru/individuals/etf/<slug> (БПИФ). Блок «Динамика стоимости» строится в
  браузере из атрибута chart-fund-data='{…}' той же страницы: JSON со ВСЕЙ историей фонда
  (каждый рабочий день с даты завершения формирования): дата, расчётная стоимость пая, СЧА,
  валюта пая (currencyCode). Отдельного API за рядом нет, диапазон дат не задаётся — страница
  всегда отдаёт ряд целиком (ctx.start_for не нужен). СЧА и пай — в валюте пая, без множителей
  (сверено с таблицей «Пай / СЧА» на той же странице и со справкой о СЧА за сентябрь 2026 —
  PDF во вкладке «Правила и документы фонда»: 11 924 828 726,36 ₽ и 15 577,43 ₽ на 30.09.2026).
* официальная доходность — раздел «Раскрытие информации → ПИФ», https://www.first-am.ru/disclosure/fund/,
  таблицы «Прирост расчетной стоимости инвестиционного пая (в валюте фонда)» за 1/3/6 мес, 1/3/5 лет
  на последний рабочий день месяца; строка ссылается на страницу фонда. Та же таблица стоит
  внизу страницы фонда (#pif-table) — она запасной источник.

Ловушки:
* У БПИФ после дробления паёв (SBMX, SBGB, SBRB — 04.06.2021, коэффициент 100) поле price
  пересчитано в «додробительный» масштаб для непрерывности графика (1765 вместо 17,65 ₽);
  фактическая расчётная стоимость — originalPrice. Берём originalPrice и проверяем, что
  price == originalPrice × crushingRatio, иначе UnknownFormat.
* Адреса страниц не совпадают с названиями фондов (fond-dinamichnyi-smart — «Моя цель 2045»,
  fond-progressivnyi-smart — «Моя цель 2035», fond-rublevye-sberezheniia-2 — «Инструменты в
  рублях»). Поэтому таблица PAGES — только подсказка, а принадлежность страницы проверяется:
  внизу каждой страницы есть юридическая справка «…«Первая – Фонд российских акций» – правила
  доверительного управления … за № 0011-46360962»; название, стоящее в справке при номере правил
  фонда, должно совпасть с названием фонда на его странице (norm_text). Для БПИФ достаточно
  совпадения биржевого тикера в заголовке страницы. Не совпало — 'not_found' с причиной.
* Фонды «с выплатой дохода» и FLOW платят доход пайщикам: pay_profit — сумма на пай в той же
  точке ряда. Дата точки — день, когда доход исключён из СЧА: расчётная стоимость пая в эту дату
  уже без выплаты (4978: 29.06.2026 — 49 838,98 ₽, 30.06.2026 — 47 992,05 ₽ при выплате
  1 918,53 ₽). Эти выплаты отдаём в FundData.payouts. Официальную доходность УК считает БЕЗ
  реинвестирования: (пай на конец + сумма выплат на пай с датой в (начало, конец]) / пай на
  начало − 1, где начало — последний рабочий день месяца N месяцев назад (а не тот же день
  месяца). Так воспроизводятся все 28 цифр семи фондов с выплатами на 30.10.2026 до 0,01 п. п.;
  у фондов без выплат та же база даёт точное совпадение.
* Часть фондов (ИПИФ «Валютные облигации», «Рублевые облигации», «Устойчивый портфель
  Зима/Лето/Осень») не входит в меню сайта: их страницы есть только в sitemap.xml, в упрощённом
  шаблоне (только график). Доходность за периоды в HTML есть лишь у «Валютных облигаций»
  (в таблице раскрытия, раздел «Интервальные»); у остальных четырёх — только в КИД (PDF на
  <страница>/documents; значения КИД на 30.09.2026 совпадают с пересчётом по ряду). PDF не
  разбираем — official_returns пустой, в warnings — где её искать.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, Payout, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.first-am.ru"
DISCLOSURE_URL = f"{BASE}/disclosure/fund/"

# Номер правил -> адрес страницы фонда. Найдено разведкой 2026-11-03 (меню сайта, sitemap.xml,
# таблица раскрытия). Это подсказка, а не истина: страница всё равно проверяется по справке
# «название ↔ № правил» на ней самой. Фонд, которого здесь нет (новый), ищется по ссылкам
# таблицы раскрытия — по тикеру или названию.
PAGES = {
    # ОПИФ
    "0007-45141428": "/individuals/fund/opif-obligatsiy-fond-ros-obligatsiy",   # Рублевые сбережения
    "0011-46360962": "/individuals/fund/opif-aktsiy-fond-ros-aktsiy",           # Фонд российских акций
    "0051-56540197": "/individuals/fund/opif-smeshannykh-investitsiy-fond-sbalans",  # Сбалансированный
    "0597-94120779": "/individuals/fund/opif-aktsiy-prirodnye-resursy",         # Природные ресурсы
    "2569": "/individuals/fund/opif-obligatsiy-valyutnye-sberegenia",           # Валютные сбережения
    "2940": "/individuals/fund/opif-rfi-rublevye-obligatsii",                   # Рублевые облигации
    "3428": "/individuals/fund/pif-nakopitelniy",                               # Накопительный
    "4860": "/individuals/fund/fond-lokalniy",                                  # Локальный
    "4978": "/individuals/fund/bond-fund-with-income-payment",
    "4979": "/individuals/fund/mixed-fund-with-income-payment",
    "4980": "/individuals/fund/stock-fund-with-income-payment",
    "5268": "/individuals/fund/fond-ustoychivyy-portfel",                       # Устойчивый портфель Зима
    "5324": "/individuals/fund/conservative-fund-with-income-payment",
    "5325": "/individuals/fund/weighted-fund-with-income-payment",
    "5471": "/individuals/fund/pif-sustainable-portfolio-summer",               # Устойчивый портфель Лето
    "5592": "/individuals/fund/fond-ustoichivyi-portfel-osen",                  # Устойчивый портфель Осень
    "6992": "/individuals/fund/fond-levitan",                                   # Левитан
    "7160": "/individuals/fund/fond-valiutnykh-obligatsii-s-vyplatoi-dokhoda",
    "7313": "/individuals/fund/fond-dragotsennykh-metallov",
    "7594": "/individuals/fund/fond-rublevye-sberezheniia-2",                   # Инструменты в рублях
    "7905": "/individuals/fund/opif-new-impressions",                           # Новые впечатления
    # ИПИФ (в реестре ЦБ записан как открытый; сайт УК называет его интервальным)
    "4471-СД": "/individuals/fund/ipif-fi-mezhdunarodnye-obligatsii",           # «Валютные облигации», USD
    # БПИФ
    "3555": "/individuals/etf/etf-moex",                     # SBMX
    "3629": "/individuals/etf/etf-ofz",                      # SBGB
    "3636": "/individuals/etf/etf-eurobonds",                # SBCB
    "3785": "/individuals/etf/corporate-bonds-rub",          # SBRB
    "4162": "/individuals/etf/etf-esg",                      # SBRI
    "4426": "/individuals/etf/fond-ostorozhnyi-smart",       # SBRS
    "4427": "/individuals/etf/fond-progressivnyi-smart",     # SBPS, Моя цель 2035
    "4428": "/individuals/etf/fond-dinamichnyi-smart",       # SBDS, Моя цель 2045
    "4429": "/individuals/etf/etf-stme",                     # STME
    "4431": "/individuals/etf/fond-konservativnyi-smart",    # SAFE
    "4607": "/individuals/etf/etf-sbmm",                     # SBMM
    "4678": "/individuals/etf/etf-sbhi",                     # SBHI
    "5061": "/individuals/etf/etf-sbgd",                     # SBGD
    "5230": "/individuals/etf/etf-sbcn",                     # SBCN, юани
    "5472": "/individuals/etf/etf-sbby",                     # SBBY, юани
    "5649": "/individuals/etf/etf-sbsc",                     # SBSC
    "5972": "/individuals/etf/etf-sbfr",                     # SBFR
    "6079": "/individuals/etf/etf-sblb",                     # SBLB
    "6312": "/individuals/etf/etf-sipo",                     # SIPO (по правилам — «Фонд АйПиО»)
    "6938": "/individuals/etf/etf-sbbc",                     # SBBC
    "7137": "/individuals/etf/fond-ezhemesiachnyi-dokhod",   # FLOW
    "7610": "/individuals/etf/fond-portfel-lezheboki",       # SPRN
    "7894": "/individuals/etf/etf-aiiq",                     # AIIQ
    "8124": "/individuals/etf/etf-secr",                     # SECR
}

CURRENCIES = {"RUB", "USD", "CNY", "EUR"}

# Заголовки колонок таблицы «Прирост расчетной стоимости» -> период OfficialReturn.
PERIOD_HEADERS = {"1 мес": "1m", "3 мес": "3m", "6 мес": "6m", "1 год": "1y"}

# У фондов в упрощённом шаблоне доходность за периоды есть только в ежемесячном КИД (PDF).
NO_RETURNS_WARNING = (
    "официальная доходность за 1/3/6/12 мес для этого фонда на HTML-страницах УК не публикуется "
    "(нет ни в таблице «Прирост расчетной стоимости» раздела раскрытия, ни на странице фонда) — "
    "только в ключевом информационном документе, PDF на {docs}; PDF не разбираем (нет "
    "pdf-библиотеки в зависимостях), доходность не заполнена")

# Насколько позже начала ряда (since / завершения формирования) может стоять первая точка,
# чтобы историю всё ещё считать полной: праздники, первый расчёт после формирования.
_START_SLACK = timedelta(days=7)

_CHART_RE = re.compile(r"chart-fund-data='([^']*)'")
# Огромные атрибуты (ряд и список дат для календаря) вырезаем перед разбором HTML.
_BIG_ATTRS_RE = re.compile(r"\s(?:chart-fund-data|data-enabled-date)='[^']*'")
# «…«Название» – правила доверительного управления … за № 0011-46360962». Название может
# содержать вложенные кавычки («Заблокированные активы … «Первая – Золото»»).
_LEGAL_RE = re.compile(
    r"«(?P<name>[^«»]*(?:«[^«»]*»[^«»]*)*)»\s*[-–—]?\s*правила доверительного управления"
    r"[^№]{0,200}?№\s*(?P<rules>\d{4}(?:-\d{8})?(?:-СД)?)")
_TICKER_RE = re.compile(r"[-–—]\s*([A-Z]{4})\b")


# ---------- разбор ----------
@dataclass
class Chart:
    title: str                 # подпись ряда на графике (_title)
    currency: str              # валюта пая
    points: list[SeriesPoint]
    payouts: dict[date, float] = field(default_factory=dict)   # дата -> выплата дохода на пай


@dataclass
class ReturnsRow:
    as_of: date | None
    values: dict[str, float] = field(default_factory=dict)   # период -> % (только опубликованные)
    name: str = ""
    path: str = ""


def _text(content: bytes | str) -> str:
    return content.decode("utf-8") if isinstance(content, bytes) else content


def _soup(content: bytes | str) -> BeautifulSoup:
    return BeautifulSoup(_BIG_ATTRS_RE.sub("", _text(content)), "lxml")


def _cell(tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def has_chart(content: bytes | str) -> bool:
    """Есть ли на странице блок «Динамика стоимости» с данными (у формирующегося фонда его нет)."""
    return "chart-fund-data=" in _text(content)


def parse_chart(content: bytes | str) -> Chart:
    """Ряд из атрибута chart-fund-data страницы фонда: дата -> (СЧА, расчётная стоимость пая)."""
    found = _CHART_RE.findall(_text(content))
    require(len(found) == 1, f"ожидался один атрибут chart-fund-data, найдено {len(found)}")
    try:
        data = json.loads(html_lib.unescape(found[0]))
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"chart-fund-data: не JSON ({e})") from e
    require_keys(data, ["chartData", "currencyCode", "_title", "crushingRatio"], "chart-fund-data")
    currency = data["currencyCode"]
    require(currency in CURRENCIES, f"chart-fund-data: незнакомая валюта пая {currency!r}")
    rows = data["chartData"]
    require(isinstance(rows, list), "chart-fund-data.chartData: ожидался список")
    ratio = num(data["crushingRatio"])           # коэффициент дробления паёв ('' — дробления не было)

    points: dict[date, SeriesPoint] = {}
    payouts: dict[date, float] = {}
    for i, row in enumerate(rows):
        where = f"chartData[{i}]"
        require_keys(row, ["type", "date", "dateFormat", "price", "originalPrice", "net_assets",
                           "isCrushing", "pay_profit"], where)
        require(row["type"] == "fund", f"{where}: незнакомый тип точки {row['type']!r}")
        d = day(row["dateFormat"])
        require(d == day(row["date"]), f"{where}: date {row['date']!r} ≠ dateFormat {row['dateFormat']!r}")
        price, shown = num(row["originalPrice"]), num(row["price"])
        # price на графике после дробления умножен на коэффициент; фактическая стоимость — originalPrice
        if row["isCrushing"]:
            require(ratio is not None and price is not None and shown is not None
                    and abs(shown - price * ratio) <= 0.011 * ratio,
                    f"{where}: price {shown} ≠ originalPrice {price} × crushingRatio {ratio}")
        else:
            require(shown == price, f"{where}: price {shown} ≠ originalPrice {price} без дробления")
        nav = num(row["net_assets"])
        require(price is None or price > 0, f"{where}: неположительная стоимость пая {price}")
        require(nav is None or nav > 0, f"{where}: неположительная СЧА {nav}")
        require(d not in points, f"{where}: дата {d} повторяется")
        points[d] = SeriesPoint(date=d, nav=nav, price=price)
        paid = num(row["pay_profit"])             # 0 — в этот день выплаты не было
        require(paid is None or paid >= 0, f"{where}: отрицательная выплата дохода {paid}")
        if paid:
            payouts[d] = paid
    return Chart(title=str(data["_title"]), currency=currency,
                 points=[points[d] for d in sorted(points)], payouts=payouts)


def parse_series(content: bytes | str) -> list[SeriesPoint]:
    return parse_chart(content).points


def parse_rules_names(content: bytes | str) -> dict[str, str]:
    """Юридическая справка внизу страницы: номер правил -> название фонда, как его пишет УК."""
    text = _cell(_soup(content))
    out: dict[str, str] = {}
    for m in _LEGAL_RE.finditer(text):
        out.setdefault(m.group("rules"), m.group("name").strip())
    require(len(out) >= 5, "нет юридической справки «… правила доверительного управления … за № …»")
    return out


def parse_page_names(content: bytes | str) -> list[str]:
    """Как фонд назван на своей странице: <title>, h1, полное название в h3, подпись графика."""
    soup = _soup(content)
    names: list[str] = []
    if soup.title:
        names.append(_cell(soup.title))
    names += [_cell(h) for h in soup.find_all("h1")]
    for h in soup.find_all("h3"):
        t = _cell(h)
        if re.match(r"(ОПИФ|БПИФ|ИПИФ|ЗПИФ)\b", t):
            m = re.search(r"[«\"]([^«»\"]+)[»\"]", t)       # первое название в кавычках
            if m:
                names.append(m.group(1))
    if has_chart(content):
        names.append(parse_chart(content).title)
    return [n for n in names if n]


def _returns_table(table, first_col: str, where: str) -> tuple[list[str], list]:
    head = table.find("tr")
    headers = [_cell(c).rstrip(".") for c in head.find_all(["th", "td"])] if head else []
    require(headers[:1] == [first_col] and all(h in headers for h in PERIOD_HEADERS),
            f"{where}: незнакомые колонки {headers}")
    return headers, [tr for tr in table.find_all("tr")[1:] if tr.find("td")]


def _returns_row(headers: list[str], cells: list[str], date_idx: int) -> ReturnsRow:
    require(len(cells) == len(headers), f"строка доходности: {len(cells)} ячеек при {len(headers)} колонках")
    # у фонда моложе месяца вместо даты и значений стоит «-»
    as_of = None if cells[date_idx] in ("-", "—", "") else day(cells[date_idx])
    values = {}
    for h, period in PERIOD_HEADERS.items():
        v = pct(cells[headers.index(h)])
        if v is not None:
            values[period] = v
    return ReturnsRow(as_of=as_of, values=values)


def parse_returns_disclosure(content: bytes | str) -> dict[str, ReturnsRow]:
    """Раздел раскрытия /disclosure/fund/: адрес страницы фонда -> доходность за периоды."""
    soup = _soup(content)
    block = soup.find(id="pif-table")
    require(block is not None and "Прирост расчетной стоимости" in _cell(block),
            "нет блока «Прирост расчетной стоимости инвестиционного пая» (#pif-table)")
    tables = block.find_all("table")
    require(bool(tables), "#pif-table: нет таблиц")
    out: dict[str, ReturnsRow] = {}
    for t in tables:
        headers, rows = _returns_table(t, "Фонд", "раскрытие #pif-table")
        require("Дата" in headers, f"раскрытие #pif-table: нет колонки «Дата» в {headers}")
        for tr in rows:
            tds = tr.find_all("td")
            link = tds[0].find("a", href=True)
            if link is None:            # строка без страницы фонда — сопоставить не с чем
                continue
            row = _returns_row(headers, [_cell(td) for td in tds], headers.index("Дата"))
            row.name = _cell(tds[0])
            row.path = re.sub(r"^https?://(www\.)?first-am\.ru", "", link["href"]).rstrip("/")
            out[row.path] = row
    require(bool(out), "#pif-table: нет строк со ссылками на фонды")
    return out


def parse_returns_page(content: bytes | str) -> ReturnsRow | None:
    """Таблица «Прирост расчетной стоимости» внизу страницы фонда; None — на странице её нет."""
    block = _soup(content).find(id="pif-table")
    if block is None:
        return None
    table = block.find("table")
    require(table is not None, "страница фонда, #pif-table: нет таблицы")
    headers, rows = _returns_table(table, "По данным на", "страница фонда, #pif-table")
    require(len(rows) == 1, f"страница фонда, #pif-table: ожидалась одна строка, найдено {len(rows)}")
    return _returns_row(headers, [_cell(td) for td in rows[0].find_all("td")], 0)


# ---------- сопоставление ----------
def name_key(name: str) -> str:
    """Название для сравнения: без «(ранее …)», тикера, слова «Первая» и типовых слов."""
    s = re.sub(r"\(ранее.*?(\)|$)", " ", str(name or ""))
    s = _TICKER_RE.sub(" ", s)
    s = norm_text(s)
    return re.sub(r"^первая\s+", "", s).strip()


def page_tickers(names: list[str]) -> set[str]:
    return {t for n in names for t in _TICKER_RE.findall(n)}


def match_page(fund: Fund, names: list[str], legal: dict[str, str]) -> str | None:
    """Чем подтверждено, что страница — этого фонда: 'правила' / 'тикер' / 'название'; None — не он."""
    keys = {name_key(n) for n in names} - {""}
    if fund.rules in legal and name_key(legal[fund.rules]) in keys:
        return "правила"
    if fund.ticker and fund.ticker in page_tickers(names):
        return "тикер"
    if name_key(fund.name) in keys:
        return "название"
    return None


def _returns(row: ReturnsRow | None, source_url: str) -> list[OfficialReturn]:
    if row is None:
        return []
    return [OfficialReturn(period=p, value_pct=v, as_of=row.as_of, source_url=source_url)
            for p, v in row.values.items()]


class FirstAmAdapter(Adapter):
    key = "first_am"
    title = "УК «Первая» (Сбер)"
    uk_inns = ("7710183778",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        disclosure, disclosure_legal, disclosure_error = None, {}, None
        try:
            raw = ctx.http.get(DISCLOSURE_URL, fmt="html", funds=[f.rules for f in funds])
            disclosure = parse_returns_disclosure(raw.content())
            disclosure_legal = parse_rules_names(raw.content())
        except (FetchError, UnknownFormat) as e:
            # Без раздела раскрытия ряды всё равно собираем; доходность — из таблицы на странице фонда.
            disclosure_error = e
            ctx.log.warning("[first_am] раздел раскрытия не разобран: %s", e)

        out = []
        for fund in funds:
            fd = self.guarded(ctx, fund, lambda f=fund: self._one(
                ctx, f, disclosure, disclosure_legal, disclosure_error))
            out.append(fd)
        return out

    @staticmethod
    def _find_path(fund: Fund, disclosure: dict[str, ReturnsRow] | None,
                   legal: dict[str, str]) -> str | None:
        if fund.rules in PAGES:
            return PAGES[fund.rules]
        # Новый фонд: ищем ссылку в таблице раскрытия по тикеру или по названию из справки/реестра.
        wanted = {name_key(fund.name)} | ({name_key(legal[fund.rules])} if fund.rules in legal else set())
        for path, row in (disclosure or {}).items():
            if (fund.ticker and fund.ticker in page_tickers([row.name])) or name_key(row.name) in wanted:
                return path
        return None

    def _one(self, ctx: Context, fund: Fund, disclosure: dict[str, ReturnsRow] | None,
             disclosure_legal: dict[str, str], disclosure_error: Exception | None) -> FundData:
        path = self._find_path(fund, disclosure, disclosure_legal)
        if path is None and fund.status == "Формируется":
            return FundData(fund.rules, status="not_formed",
                            reason=f"фонд формируется (правила от {fund.reg_date}), "
                                   "страницы фонда на сайте УК ещё нет")
        if path is None:
            return FundData(fund.rules, status="not_found",
                            reason="страницы фонда нет ни в известных адресах, "
                                   "ни в таблице раскрытия сайта УК")
        page_url = BASE + path
        raw = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
        content = raw.text()

        names = parse_page_names(content)
        how = match_page(fund, names, parse_rules_names(content))
        if how is None:
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"страница {page_url} описывает другой фонд ({'; '.join(names[:3])})")

        if not has_chart(content):
            if fund.status == "Формируется":
                return FundData(fund.rules, status="not_formed", page_url=page_url,
                                reason=f"фонд формируется (правила от {fund.reg_date}): на странице "
                                       "фонда нет динамики СЧА и пая")
            return FundData(fund.rules, status="no_history", page_url=page_url,
                            reason="на странице фонда нет блока «Динамика стоимости» с СЧА и паем")

        chart = parse_chart(content)
        fd = FundData(fund.rules, currency=chart.currency, page_url=page_url,
                      segments=[self.segment(raw, chart.points, page_url=page_url, since=ctx.since)])
        self._status(ctx, fund, fd, chart.points)
        # выплаты дохода — для доходности с реинвестированием (сумма на пай в валюте пая)
        fd.payouts = [Payout(date=d, amount=v, source_url=page_url)
                      for d, v in sorted(chart.payouts.items()) if d >= ctx.since]

        # Официальная доходность: раздел раскрытия, запасной вариант — таблица на странице фонда
        # (например, «Инструменты в рублях» есть только там).
        row, source = (disclosure or {}).get(path), DISCLOSURE_URL
        if row is None:
            source = page_url
            try:
                row = parse_returns_page(content)
            except UnknownFormat as e:
                fd.errors.append(f"разбор доходности на странице фонда: {e}")
        fd.official_returns = _returns(row, source)
        if isinstance(disclosure_error, UnknownFormat):
            fd.errors.append(f"разбор раздела раскрытия {DISCLOSURE_URL}: {disclosure_error}")
        elif disclosure_error is not None:
            fd.warnings.append(f"раздел раскрытия недоступен ({disclosure_error}); доходность — со страницы фонда")
        if not fd.official_returns and not fd.errors:
            fd.warnings.append(NO_RETURNS_WARNING.format(docs=f"{page_url}/documents") if row is None else
                               f"в таблице доходности УК ({source}) за 1/3/6/12 мес прочерки — фонд моложе месяца")
        return fd

    @staticmethod
    def _status(ctx: Context, fund: Fund, fd: FundData, points: list[SeriesPoint]) -> None:
        kept = fd.segments[0].points
        if not kept:
            last = points[-1].date.isoformat() if points else "нет точек"
            fd.status, fd.reason = "no_history", f"на сайте УК нет точек с {ctx.since} (последняя: {last})"
            return
        if any(p.nav is None for p in kept) or any(p.price is None for p in kept):
            fd.warnings.append("в ряду есть даты без СЧА или без стоимости пая (сайт УК не дал значения)")
        start = ctx.since
        if fund.formation_end:
            start = max(start, date.fromisoformat(fund.formation_end))
        elif fund.reg_date and date.fromisoformat(fund.reg_date) > ctx.since:
            return      # дата завершения формирования неизвестна — полноту начала ряда не оценить
        if points[0].date > start + _START_SLACK:
            fd.status = "partial"
            fd.reason = (f"УК публикует историю только с {points[0].date:%d.%m.%Y} "
                         f"(ожидалась с {start:%d.%m.%Y})")
