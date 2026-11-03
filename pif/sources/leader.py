"""ЗАО «Лидер» (leader-invest.ru): месячный ряд СЧА и пая и доходность — из КИД (docx),
последняя ежедневная точка — со страницы фонда.

Что УК публикует в открытом доступе (проверено 03.11.2026):
* ежедневное «Сообщение о стоимости чистых активов и расчетной стоимости инвестиционных паев»
  (PDF, все ОПИФ в одном файле) висит на сайте 2 дня («Доступно до»), архива нет;
  страница фонда показывает только последнюю дату (блок .share: «Пай: … руб.», «СЧА: … руб.»);
* ежемесячно — справка о СЧА (форма 0420502) в «Отчетности фонда» и КИД. Справки у «Лидер –
  облигации» и «Лидер – смешанные инвестиции» — старый двоичный .xls (BIFF), openpyxl его не
  читает; поэтому месячный ряд берём из КИД (docx, у всех трёх фондов одинаковый): в разделе 5
  есть «Расчетная стоимость инвестиционного пая … руб.» и «Стоимость чистых активов … руб.» на
  дату «по состоянию на» (последний рабочий день месяца; сверено со справкой СЧА «Лидер – акции»
  за 30.09.2026), там же таблица «Доходность за период»: 1 месяц / 3 месяца / 6 месяцев / 1 год;
* «Сведения о приросте расчетной стоимости инвестиционного пая» (HTML) — только 3 мес / 6 мес /
  1 год / 3 года без 1 месяца; для доходности берём последний КИД (все четыре периода, дата —
  конец месяца, по ряду проверяемо).

Источники:
* https://leader-invest.ru/raskrytie-informacii/paevye-investicionnye-fondy-raskritie — меню раздела
  ПИФ: ссылки на страницы фондов и их подразделы «Ключевой информационный документ»;
* <страница фонда> — последняя ежедневная точка (она же page_url);
* <страница фонда>/klucevoj-informacionnyj-dokument — список КИД (a.file, «Опубликовано: …»);
* /uploads/YYYY/MM/file_<id>.docx?<дата> — сам КИД. Дата в названии ссылки бывает с опечаткой
  («по состоянию на 30.10. 2026», опубликован 10.02.2026), поэтому дату берём из самого КИД,
  а отбираем документы по дате публикации (не раньше ctx.since).
Сопоставление: по названию в «» (norm_text) в меню раздела, затем номер правил из КИД
(«Правила … зарегистрированы … № 0297-74549782») сверяется с реестром.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from lxml import etree

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://leader-invest.ru"
PIF_PATH = "/raskrytie-informacii/paevye-investicionnye-fondy-raskritie"
PIF_URL = BASE + PIF_PATH
KID_TITLE = "Ключевой информационный документ"
# у каждого КИД свой адрес /uploads/…/file_<id>.docx, содержимое не меняется — не перекачиваем
KID_FRESH_HOURS = 24 * 30

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_KID_PERIODS = {"1 месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}
_AMOUNT = r"(\d[\d\s]*[.,]\d{1,2})\s*руб"


@dataclass
class SiteFund:
    name: str          # название в «» на сайте
    page_url: str      # страница фонда в разделе раскрытия
    kid_url: str       # подраздел «Ключевой информационный документ»


@dataclass
class KidLink:
    url: str
    published: date


@dataclass
class Kid:
    rules: str | None
    as_of: date
    price: float | None
    nav: float | None
    returns: dict[str, float | None]


# ---------- чистые разборщики ----------

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def parse_fund_nav(html: bytes | str) -> dict[str, SiteFund]:
    """Меню раздела ПИФ -> {norm_text(название): SiteFund}. Фонд — ссылка PIF_PATH/<slug> с
    названием в «», КИД — ссылка PIF_PATH/<slug>/<…> с текстом «Ключевой информационный документ»."""
    soup = BeautifulSoup(html, "lxml")
    names: dict[str, str] = {}
    kids: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        path = urlsplit(urljoin(BASE, a["href"])).path.rstrip("/")
        if not path.startswith(PIF_PATH + "/"):
            continue
        parts = path[len(PIF_PATH) + 1:].split("/")
        text = _clean(a.get_text(" "))
        m = re.search(r"«([^«»]+)»", text)
        if len(parts) == 1 and m and "паевой инвестиционный фонд" in text.lower():
            names.setdefault(parts[0], m.group(1))
        elif len(parts) == 2 and text == KID_TITLE:
            kids.setdefault(parts[0], BASE + path)
    out = {norm_text(name): SiteFund(name, f"{PIF_URL}/{slug}", kids[slug])
           for slug, name in names.items() if slug in kids}
    require(bool(out), "меню раздела ПИФ: не нашёл ни одного фонда со ссылкой на КИД")
    return out


def parse_latest(html: bytes | str) -> SeriesPoint:
    """Блок .share на странице фонда: дата, «Пай: … руб.», «СЧА: … руб.»."""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select(".shares .share")
    require(len(blocks) == 1, f"страница фонда: ожидался один блок .share, найдено {len(blocks)}")
    share = blocks[0]
    d = share.select_one(".share-date")
    require(d is not None, "страница фонда: нет .share-date")
    values: dict[str, str] = {}
    for item in share.select(".share-item"):
        prop = item.select_one(".share-item__prop")
        rows = item.select(".share-item__row")
        if prop and rows:
            values[_clean(prop.get_text()).rstrip(":")] = _clean(rows[-1].get_text())
    require("Пай" in values and "СЧА" in values, f"страница фонда: нет «Пай»/«СЧА», есть {sorted(values)}")
    for k in ("Пай", "СЧА"):
        require(values[k].endswith("руб."), f"страница фонда: {k} не в рублях: {values[k]!r}")
    return SeriesPoint(date=day(_clean(d.get_text())), nav=num(values["СЧА"]), price=num(values["Пай"]))


def parse_kid_list(html: bytes | str) -> list[KidLink]:
    """Подраздел «Ключевой информационный документ» -> ссылки на КИД, свежие первыми."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main")
    require(main is not None, "список КИД: нет <main>")
    out = []
    for a in main.select("a.file[href]"):
        text = _clean(a.get_text(" "))
        if KID_TITLE not in text:
            continue
        m = re.search(r"Опубликовано:\s*(\d{2}\.\d{2}\.\d{4})", text)
        require(m is not None, f"список КИД: нет даты «Опубликовано»: {text[-80:]!r}")
        out.append(KidLink(url=urljoin(BASE, a["href"]), published=day(m.group(1))))
    require(bool(out), "список КИД: нет ни одной ссылки a.file на КИД")
    return sorted(out, key=lambda k: k.published, reverse=True)


def docx_text(content: bytes) -> tuple[list[str], list[list[str]]]:
    """docx -> (абзацы документа по порядку, строки таблиц как списки текстов ячеек)."""
    try:
        xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml")
        root = etree.fromstring(xml)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError) as e:
        raise UnknownFormat(f"КИД: не docx ({type(e).__name__}: {e})") from e

    def text(el) -> str:
        return _clean("".join(t.text or "" for t in el.iter(_W + "t")))

    paras = [text(p) for p in root.iter(_W + "p")]
    rows = [[text(tc) for tc in tr.findall(_W + "tc")] for tr in root.iter(_W + "tr")]
    return [p for p in paras if p], rows


def parse_kid(content: bytes) -> Kid:
    """КИД (docx) -> номер правил, дата, цена пая и СЧА (раздел 5) и доходность за 1/3/6/12 мес."""
    paras, rows = docx_text(content)
    full = "\n".join(paras)
    m = re.search(r"по\s+состоянию\s+на\s*(\d{2}\.\d{2}\.\d{4})", full)
    require(m is not None, "КИД: нет даты «по состоянию на дд.мм.гггг»")
    as_of = day(m.group(1))
    require(any("Доходность инвестиций" in c for r in rows for c in r),
            "КИД: нет таблицы «Доходность за период» (столбец «Доходность инвестиций»)")
    returns: dict[str, float | None] = {}
    for row in rows:
        cells = [c for c in row if c]
        if len(cells) >= 2 and cells[0] in _KID_PERIODS:
            returns[_KID_PERIODS[cells[0]]] = pct(cells[1])
    missing = [label for label, code in _KID_PERIODS.items() if code not in returns]
    require(not missing, f"КИД: в таблице доходности нет строк {missing}")
    price = re.search(r"Расчетная\s+стоимость\s+инвестиционного\s+пая\s*" + _AMOUNT, full)
    nav = re.search(r"Стоимость\s+чистых\s+активов\s+паевого\s+инвестиционного\s+фонда\s*" + _AMOUNT, full)
    require(price is not None, "КИД: нет «Расчетная стоимость инвестиционного пая … руб.»")
    require(nav is not None, "КИД: нет «Стоимость чистых активов паевого инвестиционного фонда … руб.»")
    rules = re.search(r"Правила\s+доверительного\s+управления[^№]{0,200}№\s*(\d{4}-\d{8})", full)
    return Kid(rules=rules.group(1) if rules else None, as_of=as_of, price=num(price.group(1)),
               nav=num(nav.group(1)), returns=returns)


# ---------- адаптер ----------

class LeaderAdapter(Adapter):
    key = "leader"
    title = "ЗАО «Лидер»"
    uk_inns = ("5018026672",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # Меню раздела ПИФ качается внутри guarded: повторный get берётся из кэша,
        # а сбой превращается в статус фонда, а не роняет адаптер.
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        nav_page = ctx.http.get(PIF_URL, fmt="html", funds=[fund.rules])
        site = parse_fund_nav(nav_page.content()).get(norm_text(fund.name))
        if site is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в разделе {PIF_URL} нет фонда «{fund.name}»")
        fd = FundData(fund.rules, currency="RUB", page_url=site.page_url)

        # месячный ряд и доходность — из КИД, опубликованных с начала истории
        lst = ctx.http.get(site.kid_url, fmt="html", funds=[fund.rules])
        kids: list[tuple[Kid, str]] = []
        for link in parse_kid_list(lst.content()):
            if link.published < ctx.since:
                continue
            raw = ctx.http.get(link.url, fmt="docx", funds=[fund.rules], fresh_hours=KID_FRESH_HOURS)
            kid = parse_kid(raw.content())
            if kid.rules and kid.rules != fund.rules:
                fd.warnings.append(f"КИД {link.url} в разделе фонда на деле относится к правилам "
                                   f"№ {kid.rules} (ошибка на сайте УК) — пропущен")
                continue
            kids.append((kid, raw.url))
            seg = self.segment(raw, [SeriesPoint(kid.as_of, kid.nav, kid.price)],
                               page_url=site.kid_url, since=ctx.since)
            if seg.points:
                fd.segments.append(seg)

        # последняя ежедневная точка — со страницы фонда (архива ежедневных значений нет)
        try:
            page = ctx.http.get(site.page_url, fmt="html", funds=[fund.rules])
            fd.segments.append(self.segment(page, [parse_latest(page.content())],
                                            page_url=site.page_url, since=ctx.since))
        except (FetchError, UnknownFormat) as e:
            ctx.log.error("[%s] %s: страница фонда: %s", fund.uk_short, fund.rules, e)
            fd.warnings.append(f"последняя ежедневная точка не получена: {e}")

        if kids:
            last, url = max(kids, key=lambda k: k[0].as_of)
            fd.official_returns = [OfficialReturn(period=code, value_pct=v, as_of=last.as_of, source_url=url)
                                   for code, v in last.returns.items() if v is not None]
        if any(s.page_url == site.kid_url for s in fd.segments):
            fd.warnings.append("ряд месячный (КИД на последний рабочий день месяца) + последняя "
                               "ежедневная точка: ежедневные сообщения о СЧА УК держит на сайте 2 дня")
        elif fd.segments:
            fd.status, fd.reason = "partial", f"нет КИД с {ctx.since} — есть только последняя ежедневная точка"
        else:
            fd.status, fd.reason = "unavailable", "не получены ни КИД, ни значение со страницы фонда"
        return fd
