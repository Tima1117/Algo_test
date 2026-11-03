"""ООО «УК РЕНКАП» (rencapwealth.ru): ряд СЧА и пая БПИФ — JSON графика на странице фонда,
доходность — КИД (PDF) из «Обязательной информации».

Источники (только сайт УК, без MOEX ISS):
* https://www.rencapwealth.ru/products/ — «Продукты»: ссылки на страницы БПИФ /products/etf/<тикер>/;
  в подвале каждой страницы — «Сведения о регистрации правил …: БПИФ … «РенКап – Высокодоходные
  облигации» - № 7681 от 26.03.2026; …» — по нему номер правил реестра связывается с названием фонда;
* https://www.rencapwealth.ru/products/etf/<тикер>/ — страница фонда: h1 «БПИФ «<название>»» и конфиг
  графика <script type="application/json" data-chart-config> с рядами «Стоимость пая» (id price) и
  «График СЧА» (id navl): view = marketdata.fund_<код>_share_price / _nav, unit = «₽»;
* https://www.rencapwealth.ru/api/marketdata/chart-points?view=<view> — то, что рисует график:
  JSON [{x: epoch-ms, value, changePercent}] за всю историю фонда (с завершения формирования),
  по рабочим дням. Диапазон дат не принимает — качаем целиком (~10 КБ) и режем по ctx.since.
  x — полночь UTC; дату берём по Москве, чтобы и полночь МСК (21:00 UTC накануне) не сдвинула день.
  Значения сверены с ежедневными «Сообщениями о СЧА и расчетной стоимости пая» (PDF) и с КИД:
  цена пая в рублях, СЧА в рублях (не в тысячах);
* https://www.rencapwealth.ru/regulations/ — «Обязательная информация»: раздел каждого фонда
  /regulations/<id>/ -> подраздел «Ключевой информационный документ о паевом инвестиционном фонде»
  /regulations/<id>/<sub>/ -> список КИД /regulations/<id>/<sub>/doc/<doc>/<file>/ (сам PDF);
  раздел 5 КИД: 1 месяц / 3 месяца / 6 месяцев / 1 год («-*» — «недостаточно данных», фонд молодой).
  Текст из PDF достаём pypdf (его нет в зависимостях pyproject); без него доходность не собирается,
  а ряд — собирается. Других публикаций доходности за 1/3/6/12 мес. у УК нет.
Выплаты дохода: по КИД «Доход от управления Фондом не выплачивается, но капитализируется» — payouts пустые.
Сопоставление: номер правил -> название из подвала сайта; страница фонда — по тикеру реестра
(/products/etf/<тикер>/, как в меню сайта) или перебором страниц БПИФ, с обязательной сверкой названия h1.
Раздел раскрытия — по названию (norm_text), номер правил сверяется ещё и в КИД.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.rencapwealth.ru"
PRODUCTS_URL = f"{BASE}/products/"
REGULATIONS_URL = f"{BASE}/regulations/"
CHART_URL = f"{BASE}/api/marketdata/chart-points"
KID_TITLE = "Ключевой информационный документ"
# КИД по ссылке не меняется (у каждой редакции свой адрес) — повторно не перекачиваем
KID_FRESH_HOURS = 24 * 30

MSK = timezone(timedelta(hours=3))
_UNITS = {"₽": "RUB", "руб": "RUB", "rub": "RUB", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
          "¥": "CNY", "cny": "CNY"}
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_KID_PERIODS = (("1 месяц", "1m"), ("3 месяца", "3m"), ("6 месяцев", "6m"), ("1 год", "1y"))
_FOOTER_RE = re.compile(r"«([^«»]+)»\s*[-–—]\s*№\s*([\dА-Яа-яA-Za-z-]+)\s+от\s+\d{2}\.\d{2}\.\d{4}")
_AMOUNT = r"(\d[\d  ]*(?:[.,]\d+)?)\s*руб"


@dataclass
class ProductPage:
    name: str             # название из h1 (в «»)
    currency: str
    price_view: str       # view ряда «Стоимость пая»
    nav_view: str         # view ряда «График СЧА»


@dataclass
class Kid:
    doc_date: date                        # «по состоянию на»
    rules: str | None                     # раздел 7: «зарегистрированы Банком России … за № 7681»
    price: float | None                   # раздел 5, п. 1 («____ руб.» у ещё не сформированного фонда -> None)
    nav: float | None                     # раздел 5, п. 2
    returns: dict[str, float | None]      # 1m / 3m / 6m / 1y


# ---------- чистые разборщики ----------

def _text(soup: BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", soup.get_text(" "))


def parse_rules_footer(html: bytes | str) -> dict[str, str]:
    """Подвал любой страницы сайта -> {номер правил: название фонда}."""
    text = _text(BeautifulSoup(html, "lxml"))
    i = text.find("Сведения о регистрации правил")
    require(i >= 0, "подвал: нет «Сведения о регистрации правил доверительного управления …»")
    out = {rules: name.strip() for name, rules in _FOOTER_RE.findall(text[i:i + 3000])}
    require(bool(out), "подвал: не нашёл ни одного ««Название» - № N от ДД.ММ.ГГГГ»")
    return out


def parse_products(html: bytes | str) -> dict[str, str]:
    """Страница «Продукты» -> {код БПИФ из адреса (vdor): адрес страницы фонда}."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.fullmatch(r"/products/etf/([\w-]+)/", a["href"])
        if m:
            out[m.group(1).lower()] = urljoin(BASE, a["href"])
    require(bool(out), "продукты: нет ссылок /products/etf/<код>/")
    return out


def _unit(unit: str, where: str) -> str:
    cur = _UNITS.get(str(unit).strip().lower().rstrip("."))
    require(cur is not None, f"{where}: незнакомая единица {unit!r}")
    return cur


def parse_product_page(html: bytes | str) -> ProductPage:
    """Страница БПИФ -> название из h1, валюта и view рядов цены пая и СЧА из конфига графика."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    require(h1 is not None, "страница фонда: нет h1")
    m = re.search(r"«(.+)»", h1.get_text(" ", strip=True))
    require(m is not None, f"страница фонда: нет названия в «» в h1 {h1.get_text(strip=True)!r}")
    tag = soup.select_one("script[data-chart-config]")
    require(tag is not None, "страница фонда: нет конфига графика script[data-chart-config]")
    try:
        cfg = json.loads(tag.get_text())
    except ValueError as e:
        raise UnknownFormat(f"конфиг графика: не JSON ({e})") from e
    require_keys(cfg, ("series",), "конфиг графика")
    series = {s.get("id"): s for s in cfg["series"] if isinstance(s, dict)}
    for sid in ("price", "navl"):
        require(sid in series, f"конфиг графика: нет ряда id={sid!r}; есть {sorted(map(str, series))}")
        require_keys(series[sid], ("view", "unit"), f"конфиг графика: ряд {sid}")
    require(_unit(series["navl"]["unit"], "СЧА") == "RUB", "конфиг графика: СЧА не в рублях")
    return ProductPage(name=m.group(1).strip(), currency=_unit(series["price"]["unit"], "цена пая"),
                       price_view=series["price"]["view"], nav_view=series["navl"]["view"])


def parse_chart_points(data: bytes | str | list, what: str) -> dict[date, float | None]:
    """chart-points -> {дата: значение}. Пустой список — это «точек нет», а не ошибка."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise UnknownFormat(f"{what}: не JSON ({e})") from e
    require(isinstance(data, list), f"{what}: ожидался список точек, получено {type(data).__name__}")
    out: dict[date, float | None] = {}
    for row in data:
        require_keys(row, ("x", "value"), f"{what}: точка")
        x = row["x"]
        require(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 10**11,
                f"{what}: x не epoch-ms: {x!r}")
        d = datetime.fromtimestamp(x / 1000, MSK).date()
        require(d not in out, f"{what}: дата {d} повторяется")
        out[d] = num(row["value"]) or None      # 0 — «значения нет», а не ноль
    return out


def parse_regulations(html: bytes | str) -> dict[str, str]:
    """«Обязательная информация» -> {norm_text(название фонда в «»): адрес раздела}."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=re.compile(r"^/regulations/\d+/$")):
        m = re.search(r"«(.+)»", a.get_text(" ", strip=True))
        if m:
            out[norm_text(m.group(1))] = urljoin(BASE, a["href"])
    require(bool(out), "обязательная информация: нет разделов фондов /regulations/<id>/ с названием в «»")
    return out


def parse_section(html: bytes | str, section_url: str) -> str | None:
    """Раздел фонда -> адрес подраздела «Ключевой информационный документ …» (None — подраздела нет)."""
    soup = BeautifulSoup(html, "lxml")
    path = re.escape(section_url.removeprefix(BASE))
    for a in soup.find_all("a", href=re.compile(rf"^{path}\d+/$")):
        if a.get_text(" ", strip=True).startswith(KID_TITLE):
            return urljoin(BASE, a["href"])
    return None


def parse_kid_list(html: bytes | str) -> list[tuple[date, str]]:
    """Подраздел КИД -> [(дата КИД, адрес PDF)], свежие первыми. За одну дату бывает исходный
    и «уточненный» КИД — уточнённый (и более поздняя загрузка) идёт первым."""
    soup = BeautifulSoup(html, "lxml")
    found = []
    for a in soup.find_all("a", href=re.compile(r"^/regulations/\d+/\d+/doc/\d+/\d+/$")):
        label = a.get_text(" ", strip=True)
        if not label.startswith(KID_TITLE):
            continue
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", label)
        require(m is not None, f"список КИД: нет даты в названии {label!r}")
        file_id = int(a["href"].rstrip("/").rsplit("/", 1)[1])
        found.append((day(m.group(1)), "уточн" in label.lower(), file_id, urljoin(BASE, a["href"])))
    found.sort(reverse=True)
    return [(d, url) for d, _, _, url in found]


def kid_pdf_text(content: bytes) -> str:
    """Текст КИД из PDF. pypdf нет в зависимостях проекта: без него -> ImportError."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # noqa: BLE001 — битый PDF не должен ронять ряд фонда
        raise UnknownFormat(f"КИД: не читается как PDF ({type(e).__name__}: {e})") from e


def _kid_date(text: str) -> date:
    m = re.search(r"по\s+состоянию\s+на\s*(\d{1,2})\s+([а-я]+)\s+(\d{4})", text)
    if m and m.group(2) in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"по\s+состоянию\s+на\s*(\d{2}\.\d{2}\.\d{4})", text)
    require(m is not None, "КИД: нет даты «по состоянию на»")
    return day(m.group(1))


def _kid_value(token: str) -> float | None:
    """'0,65%' -> 0.65; '-1,2%' -> -1.2; '-*' — «недостаточно данных» -> None."""
    if re.fullmatch(r"[-–—]?\**", token):
        return None
    return pct(token)


def parse_kid_text(text: str) -> Kid:
    """Текст КИД (как его отдаёт pypdf) -> дата, правила, цена пая, СЧА и доходность за 1/3/6/12 мес."""
    sec = re.search(r"Раздел\s+5\.(.*?)Раздел\s+6\.", text, re.S)
    require(sec is not None, "КИД: нет раздела 5 «Основные результаты инвестирования»")
    body = sec.group(1)
    require("Доходность" in body, "КИД: в разделе 5 нет таблицы доходности")
    returns: dict[str, float | None] = {}
    for label, code in _KID_PERIODS:
        r = re.search(rf"^\s*{label}\s+(\S+)", body, re.M)
        require(r is not None, f"КИД: нет строки «{label}» в таблице доходности")
        returns[code] = _kid_value(r.group(1))
    require(re.search(r"Расчетная\s+стоимость\s+инвестиционного\s+пая", body) is not None,
            "КИД: в разделе 5 нет «Расчетная стоимость инвестиционного пая»")
    price = re.search(r"Расчетная\s+стоимость\s+инвестиционного\s+пая\s*" + _AMOUNT, body)
    nav = re.search(r"Стоимость\s+чистых\s+активов\s+паевого\s+инвестиционного\s+фонда\s*" + _AMOUNT, body)
    rules = re.search(r"зарегистрированы\s+Банком\s+России\s+[\d.]+\s+за\s+№\s*([\d-]+)", text)
    return Kid(doc_date=_kid_date(text), rules=rules.group(1) if rules else None,
               price=num(price.group(1)) if price else None, nav=num(nav.group(1)) if nav else None,
               returns=returns)


# ---------- адаптер ----------

class RenCapAdapter(Adapter):
    key = "rencap"
    title = "ООО «УК РЕНКАП»"
    uk_inns = ("9703186884",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # Общие страницы качаются внутри guarded: повторный get берётся из кэша,
        # а сбой превращается в статус фонда, а не роняет адаптер.
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _product(self, ctx: Context, fund: Fund, site_name: str) -> tuple[str, ProductPage] | None:
        """Страница БПИФ: сначала по тикеру реестра, иначе перебором; название h1 сверяется всегда."""
        lst = ctx.http.get(PRODUCTS_URL, fmt="html", funds=[fund.rules])
        pages = parse_products(lst.content())
        want = {norm_text(site_name), norm_text(fund.name)}
        code = (fund.ticker or "").lower()
        order = ([pages[code]] if code in pages else []) + [u for c, u in pages.items() if c != code]
        for url in order:
            page = parse_product_page(ctx.http.get(url, fmt="html", funds=[fund.rules]).content())
            if norm_text(page.name) in want:
                return url, page
        return None

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        home = ctx.http.get(PRODUCTS_URL, fmt="html", funds=[fund.rules])
        site_name = parse_rules_footer(home.content()).get(fund.rules)
        if site_name is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в подвале {PRODUCTS_URL} нет фонда с правилами № {fund.rules}")
        found = self._product(ctx, fund, site_name)
        if found is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"на {PRODUCTS_URL} нет страницы БПИФ «{site_name}»")
        page_url, page = found

        raw_p = ctx.http.get(CHART_URL, params={"view": page.price_view}, fmt="json", funds=[fund.rules])
        raw_n = ctx.http.get(CHART_URL, params={"view": page.nav_view}, fmt="json", funds=[fund.rules])
        prices = parse_chart_points(raw_p.content(), "стоимость пая")
        navs = parse_chart_points(raw_n.content(), "СЧА")
        fd = FundData(fund.rules, currency=page.currency, page_url=page_url, segments=[
            self.segment(raw_p, [SeriesPoint(d, None, v) for d, v in sorted(prices.items())],
                         page_url=page_url, since=ctx.since),
            self.segment(raw_n, [SeriesPoint(d, v, None) for d, v in sorted(navs.items())],
                         page_url=page_url, since=ctx.since),
        ])
        if norm_text(site_name) != norm_text(fund.name):
            fd.warnings.append(f"название на сайте «{site_name}» отличается от реестра «{fund.name}»")
        if not prices and not navs:
            if fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", "фонд формируется: УК ещё не публикует СЧА и пай"
            else:
                fd.status, fd.reason = "partial", "график СЧА и пая на сайте УК пуст"
            return fd
        if not prices or not navs:
            fd.status = "partial"
            fd.reason = "УК публикует только " + ("СЧА" if navs else "расчётную стоимость пая")
        elif set(prices) != set(navs):
            fd.warnings.append("даты рядов цены пая и СЧА на сайте УК не совпадают")
        fd.official_returns = self._returns(ctx, fund, site_name, prices, fd)
        return fd

    def _returns(self, ctx: Context, fund: Fund, site_name: str, prices: dict[date, float | None],
                 fd: FundData) -> list[OfficialReturn]:
        """Доходность из последнего КИД. Сбой КИД не отменяет ряд — только предупреждение."""
        try:
            regs = parse_regulations(ctx.http.get(REGULATIONS_URL, fmt="html", funds=[fund.rules]).content())
            section = regs.get(norm_text(site_name)) or regs.get(norm_text(fund.name))
            if section is None:
                fd.warnings.append(f"на {REGULATIONS_URL} нет раздела фонда — КИД не найден")
                return []
            kid_list = parse_section(ctx.http.get(section, fmt="html", funds=[fund.rules]).content(), section)
            if kid_list is None:
                fd.warnings.append(f"в разделе {section} нет подраздела КИД — официальной доходности нет")
                return []
            kids = parse_kid_list(ctx.http.get(kid_list, fmt="html", funds=[fund.rules]).content())
            if not kids:
                fd.warnings.append(f"на {kid_list} нет ни одного КИД — официальной доходности нет")
                return []
            url = kids[0][1]
            doc = ctx.http.get(url, fmt="pdf", funds=[fund.rules], fresh_hours=KID_FRESH_HOURS)
            kid = parse_kid_text(kid_pdf_text(doc.content()))
        except ImportError:
            fd.warnings.append("доходность есть только в КИД (PDF), а pypdf не установлен — не собрана")
            return []
        except (FetchError, UnknownFormat) as e:
            ctx.log.error("[%s] %s: КИД: %s", fund.uk_short, fund.rules, e)
            fd.warnings.append(f"КИД не разобран: {e}")
            return []
        if kid.rules != fund.rules:
            fd.warnings.append(f"в КИД {url} правила № {kid.rules}, а в реестре № {fund.rules} — доходность не взята")
            return []
        as_of = kid.doc_date
        if kid.price is not None and (prices.get(as_of) is None or abs(prices[as_of] - kid.price) >= 0.005):
            fd.warnings.append(f"цена пая из КИД ({kid.price} на {as_of:%d.%m.%Y}) не совпала с рядом УК")
        out = [OfficialReturn(period=code, value_pct=v, as_of=as_of, source_url=url)
               for code, v in kid.returns.items() if v is not None]
        if not out:
            fd.warnings.append(f"в КИД на {as_of:%d.%m.%Y} доходность за 1–12 мес. не приводится "
                               "(«недостаточно данных»: фонд молодой)")
        return out
