"""ООО УК «ГЕРОИ» (ih-capital.ru): ежедневный ряд СЧА и пая — JSON графика, доходность — КИД (PDF).

Источники (сайт УК):
* https://ih-capital.ru/ — главная: карточки фондов (a.name -> /feo, /flagship, …) и подвал
  «Открытый паевой … «Название» – Правила доверительного управления зарегистрированы Банком
  России … за номером 6100». По подвалу фонд реестра сопоставляется по номеру правил;
* https://ih-capital.ru/disclosure.json — данные страницы «Раскрытие информации»: по каждому
  фонду адрес ряда (dataUrl = /funds?id=N, тот же, что data-src графика на странице фонда)
  и список документов, в т. ч. КИД (PDF);
* https://ih-capital.ru/funds?id=N — JSON [{date, share, scha, …}] за всю историю фонда.
  share — расчётная стоимость пая в рублях, scha — СЧА в рублях (сверено с КИД и справкой СЧА).
  Диапазон дат не принимает — качаем целиком (~100 КБ) и режем по ctx.since.
  Выходные и праздники УК заполняет копией предыдущего рабочего дня; такие копии выкидываем —
  так же делает таблица на странице фонда (схлопывает подряд идущие одинаковые строки).
  У «Валютных возможностей» есть ещё rate (курс USD ЦБ): долларовая цена на сайте — пересчёт
  рублёвой по курсу ЦБ, сам пай УК публикует в рублях («РСИП, рубли»; «1 107.32 руб.» в КИД);
* КИД https://ih-capital.ru/documents/<slug>/key_info_DD_MM_YYYY.pdf, раздел 5 «Доходность
  за период»: 1 месяц / 3 месяца / 6 месяцев / 1 год. Других публикаций доходности за эти периоды
  у УК нет (проценты на странице фонда браузер считает сам по тому же ряду).
  Внеочередной КИД (после изменения правил) датирован днём выпуска, а цифры в нём — на последний
  отчётный день (КИД «по состоянию на 22.10.2026» = цена и СЧА на 30.09.2026). Поэтому дату
  доходности берём из ряда УК: последний день не позже даты КИД с теми же ценой пая и СЧА.
  Текст из PDF достаём pypdf (его нет в зависимостях pyproject); без него доходность не
  собирается, а ряд — собирается.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://ih-capital.ru"
HOME_URL = f"{BASE}/"
DISCLOSURE_URL = f"{BASE}/disclosure"
DISCLOSURE_JSON = f"{BASE}/disclosure.json"
KID_TYPE = "Ключевой информационный документ"
# КИД по ссылке не меняется (у каждого свой адрес с датой) — повторно не перекачиваем
KID_FRESH_HOURS = 24 * 30

_RULES_RE = re.compile(
    r"«([^«»]+)»\s*[–—-]\s*Правила\s+доверительного\s+управления\s+зарегистрированы[^№]*?"
    r"за\s+номером\s*(\d+)")
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_KID_PERIODS = (("1 месяц", "1m"), ("3 месяца", "3m"), ("6 месяцев", "6m"), ("1 год", "1y"))
_AMOUNT = r"(\d[\d\s]*[.,]\d{1,2})\s*руб"


@dataclass
class SiteFund:
    name: str            # название на сайте (из подвала)
    page_url: str | None  # страница фонда для человека


@dataclass
class Section:
    name: str
    data_url: str
    kids: list[dict] = field(default_factory=list)   # КИД (PDF), свежие первыми


@dataclass
class Kid:
    doc_date: date                        # «по состоянию на» из шапки КИД
    price: float | None                   # п. 1 раздела 5 — расчётная стоимость пая
    nav: float | None                     # п. 3 раздела 5 — СЧА
    returns: dict[str, float | None]      # 1m / 3m / 6m / 1y


# ---------- чистые разборщики ----------

def _soup_text(html: bytes | str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" "))


def parse_home(html: bytes | str) -> dict[str, SiteFund]:
    """Главная -> {номер правил: SiteFund}. Номера правил — из подвала, страницы — из карточек."""
    soup = BeautifulSoup(html, "lxml")
    pages: dict[str, str] = {}
    for a in soup.select("a.name[href]"):
        pages[norm_text(a.get_text(" "))] = urljoin(BASE, a["href"])
    out: dict[str, SiteFund] = {}
    for name, rules in _RULES_RE.findall(_soup_text(html)):
        name = name.strip()
        out[rules] = SiteFund(name=name, page_url=pages.get(norm_text(name)))
    require(bool(out), "главная: не нашёл в подвале «… Правила доверительного управления … за номером N»")
    return out


def parse_disclosure(data: bytes | str | list) -> dict[str, Section]:
    """disclosure.json -> {norm_text(название фонда): Section}. Разделы без dataUrl (документы
    самой УК) пропускаются."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise UnknownFormat(f"disclosure.json: не JSON ({e})") from e
    require(isinstance(data, list) and data, "disclosure.json: ожидался непустой список разделов")
    out: dict[str, Section] = {}
    for sec in data:
        require_keys(sec, ("section", "files"), "disclosure.json: раздел")
        if not sec.get("dataUrl"):
            continue
        m = re.search(r"«([^«»]+)»", sec["section"])
        require(m is not None, f"disclosure.json: нет названия фонда в «»: {sec['section']!r}")
        kids = []
        for f in sec["files"]:
            require_keys(f, ("type", "name", "url", "date"), "disclosure.json: документ")
            if (f["type"] == KID_TYPE and f["name"].startswith(KID_TYPE)
                    and f["url"].lower().endswith(".pdf")):
                kids.append({**f, "url": urljoin(BASE, f["url"])})
        kids.sort(key=lambda f: f["date"], reverse=True)
        out[norm_text(m.group(1))] = Section(m.group(1), urljoin(BASE, sec["dataUrl"]), kids)
    require(bool(out), "disclosure.json: нет ни одного фонда с dataUrl")
    return out


def parse_history(data: bytes | str | list) -> list[SeriesPoint]:
    """JSON графика -> точки по рабочим дням. share — пай, scha — СЧА, оба в рублях.

    Копии предыдущего дня (выходные/праздники: и пай, и СЧА совпадают до копейки) не берём."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise UnknownFormat(f"ряд: не JSON ({e})") from e
    require(isinstance(data, list) and data, "ряд: ожидался непустой список")
    rows = []
    for row in data:
        require_keys(row, ("date", "share", "scha"), "ряд: строка")
        rows.append((day(row["date"]), num(row["scha"]), num(row["share"])))
    rows.sort(key=lambda r: r[0])
    points: list[SeriesPoint] = []
    for d, nav, price in rows:
        if points and points[-1].date == d:
            raise UnknownFormat(f"ряд: дата {d} повторяется")
        if points and points[-1].nav == nav and points[-1].price == price:
            continue
        points.append(SeriesPoint(date=d, nav=nav, price=price))
    return points


def kid_pdf_text(content: bytes) -> str:
    """Текст КИД из PDF. pypdf нет в зависимостях проекта: без него -> ImportError."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _kid_date(text: str) -> date:
    m = re.search(r"по\s+состоянию\s+на\s*«?\s*(\d{1,2})\s*»?\s*([а-я]+)\s*(\d{4})", text)
    if m and m.group(2) in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"по\s+состоянию\s+на\s*(\d{2}\.\d{2}\.\d{4})", text)
    require(m is not None, "КИД: нет даты «по состоянию на»")
    return day(m.group(1))


def parse_kid_text(text: str) -> Kid:
    """Текст КИД (как его отдаёт pypdf) -> дата, цена пая, СЧА и доходность за 1/3/6/12 мес."""
    require("Доходность за период" in text, "КИД: нет таблицы «Доходность за период»")
    returns: dict[str, float | None] = {}
    for label, code in _KID_PERIODS:
        m = re.search(rf"^\s*{label}\s+(\S+)", text, re.M)
        require(m is not None, f"КИД: нет строки «{label}» в таблице доходности")
        returns[code] = pct(m.group(1))
    price = re.search(r"Расчетная\s+стоимость\s+инвестиционного\s+пая\s*" + _AMOUNT, text)
    nav = re.search(r"Стоимость\s+чистых\s+активов\s+паевого\s+инвестиционного\s+фонда\s*" + _AMOUNT, text)
    require(price is not None, "КИД: нет «Расчетная стоимость инвестиционного пая … руб.»")
    return Kid(doc_date=_kid_date(text), price=num(price.group(1)),
               nav=num(nav.group(1)) if nav else None, returns=returns)


def kid_data_date(kid: Kid, points: list[SeriesPoint]) -> date | None:
    """На какую дату посчитаны цифры КИД: последний день ряда не позже даты КИД с той же ценой
    пая (и СЧА, если она есть в КИД). None — совпадения нет."""
    for p in reversed(points):
        if p.date > kid.doc_date or p.price is None:
            continue
        same_nav = kid.nav is None or (p.nav is not None and abs(p.nav - kid.nav) < 0.01)
        if abs(p.price - kid.price) < 0.005 and same_nav:
            return p.date
    return None


# ---------- адаптер ----------

class GeroiAdapter(Adapter):
    key = "geroi"
    title = "ООО УК «ГЕРОИ»"
    uk_inns = ("9725134382",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # Общие документы (главная, disclosure.json) качаются внутри guarded: повторный get
        # берётся из кэша, а сбой превращается в статус фонда, а не роняет адаптер.
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        home = ctx.http.get(HOME_URL, fmt="html", funds=[fund.rules])
        site = parse_home(home.content()).get(fund.rules)
        if site is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в подвале {HOME_URL} нет фонда с правилами № {fund.rules}")
        disc = ctx.http.get(DISCLOSURE_JSON, fmt="json", funds=[fund.rules])
        sec = parse_disclosure(disc.content()).get(norm_text(site.name))
        if sec is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в {DISCLOSURE_JSON} нет раздела фонда «{site.name}»")
        page_url = site.page_url or DISCLOSURE_URL

        raw = ctx.http.get(sec.data_url, fmt="json", funds=[fund.rules])
        points = parse_history(raw.content())
        seg = self.segment(raw, points, page_url=page_url, since=ctx.since)
        fd = FundData(fund.rules, currency="RUB", page_url=page_url, segments=[seg])
        if norm_text(site.name) != norm_text(fund.name):
            fd.warnings.append(f"название на сайте «{site.name}» отличается от реестра «{fund.name}»")
        if not seg.points:
            fd.status, fd.reason = "partial", f"УК не отдала точек с {ctx.since}"
        fd.official_returns = self._returns(ctx, fund, sec, points, fd)
        return fd

    def _returns(self, ctx: Context, fund: Fund, sec: Section, points: list[SeriesPoint],
                 fd: FundData) -> list[OfficialReturn]:
        """Доходность из последнего КИД. Сбой КИД не отменяет ряд — только предупреждение."""
        if not sec.kids:
            fd.warnings.append("в разделе фонда нет КИД (PDF) — официальной доходности нет")
            return []
        url = sec.kids[0]["url"]
        try:
            doc = ctx.http.get(url, fmt="pdf", funds=[fund.rules], fresh_hours=KID_FRESH_HOURS)
            kid = parse_kid_text(kid_pdf_text(doc.content()))
        except ImportError:
            fd.warnings.append("доходность есть только в КИД (PDF), а pypdf не установлен — не собрана")
            return []
        except (FetchError, UnknownFormat) as e:
            ctx.log.error("[%s] %s: КИД %s: %s", fund.uk_short, fund.rules, url, e)
            fd.warnings.append(f"КИД {url} не разобран: {e}")
            return []
        as_of = kid_data_date(kid, points)
        if as_of is None:
            as_of = kid.doc_date
            fd.warnings.append(f"цена пая из КИД ({kid.price}) не нашлась в ряде УК — "
                               f"доходность отнесена к дате КИД {kid.doc_date}")
        elif as_of != kid.doc_date:
            fd.warnings.append(f"КИД «по состоянию на {kid.doc_date:%d.%m.%Y}» содержит цифры на "
                               f"{as_of:%d.%m.%Y} (цена пая и СЧА совпали с рядом) — доходность на эту дату")
        return [OfficialReturn(period=code, value_pct=v, as_of=as_of, source_url=url)
                for code, v in kid.returns.items() if v is not None]
