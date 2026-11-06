"""ТКБ Инвестмент Партнерс (АО), www.tkbip.ru — 7 открытых фондов.

Источники (сайт УК, без JS):
- https://www.tkbip.ru/funds/ — «Результаты работы паевых фондов»: таблицы со ссылками на
  страницы фондов и официальным приростом стоимости пая за 1/3/6 мес, с начала года, 1/3/5 лет
  («Прирост стоимости пая на ДД.ММ.ГГГГ, %»). Отсюда — список фондов и доходность;
- https://www.tkbip.ru/funds/<slug>/information/ — «Информация о фонде»: блок «Регистрация
  правил фонда …: № XXXX-XXXXXXXX». По нему сопоставляем фонд реестра со страницей сайта
  (slug'и исторические и обманчивые: «…-globalnye-investitsii» — это «Фонд сбалансированный с
  выплатой дохода», «…-sbalansirovannyy-konservativnyy» — «Фонд сбалансированный»,
  «opif-aktsiy-agana-ekstrim» — «Перспектива»), поэтому название не используем;
- https://www.tkbip.ru/funds/<slug>/export/?dateFrom=ДД.ММ.ГГГГ&dateTo=ДД.ММ.ГГГГ&format=csv —
  выгрузка с вкладки «Результаты» (форма «Выберите диапазон дат для экспорта данных»):
  CSV «Дата;Стоимость чистых активов (СЧА);Расчетная стоимость инвестиционного пая», каждый
  рабочий день, новые даты сверху. Совпадает с данными графика на той же странице. Без dateTo
  выгрузка пуста (одна строка «"";"";""»), а дату в будущем сервер принимает: с dateTo=31.12.2099
  CSV байт в байт тот же, что с сегодняшней датой (проверено 06.11.2026), — граница фиксированная.

- https://www.tkbip.ru/disclosure-of-information/reporting-of-mutual-funds/<id>/ — страница фонда
  в «Раскрытии информации» (ссылка с «Информации о фонде»): PDF «Сообщение о выплате дохода …» и
  «Сообщение о дате составления списка лиц / о дате, на которую выплачивается доход» -> выплаты
  дохода на пай (сейчас только у «Фонда сбалансированного с выплатой дохода», см. блок ниже).

Валюта: УК официально считает СЧА и пай в рублях («Пай (руб.)», «СЧА (руб.)»). Для «Фонда
валютных облигаций» и «Золота» сайт дополнительно показывает пересчёт в USD по курсу ЦБ с
пометкой «носят информационный характер и не являются данными официальной отчётности», а CSV
отдаёт рубли. Поэтому ряд и доходность берём рублёвые, currency='RUB'.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, Payout, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://www.tkbip.ru"
CATALOG_URL = f"{BASE}/funds/"

_SLUG_HREF = re.compile(r"^/funds/([a-z0-9-]+)/$")
_RULES = re.compile(r"(\d{4})\s*[-–—]\s*(\d{8})")
_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_AS_OF = re.compile(r"прирост стоимости пая на (\d{2}\.\d{2}\.\d{4})", re.I)
_FUND_LINK_TEXT = re.compile(r"^[ОИБ]ПИФ\b.*«")      # «ОПИФ рыночных финансовых инструментов «…»»
# Подписи колонок доходности в таблицах /funds/ (после norm_text) -> период OfficialReturn.
_PERIODS = {"1 м": "1m", "3 м": "3m", "6 м": "6m", "1 год": "1y"}
_OFFICIAL_CCY = "RUB"
# Фиксированный конец диапазона выгрузки вместо «сегодня»: без dateTo CSV пуст, а с сегодняшней датой адрес
# менялся бы каждый день — повторный сбор качал бы выгрузку заново, хотя на сайте ничего не изменилось.
EXPORT_DATE_TO = "31.12.2099"


def _soup(content) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def norm_rules(s: str) -> str | None:
    """'№ 0478 — 75408434' / '№0991-94131990' -> '0478-75408434'."""
    m = _RULES.search(s or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


# ---------- разбор ----------

def parse_catalog(content) -> list[str]:
    """Slug'и страниц фондов со страницы /funds/: строки таблиц результатов + список «ПИФы».

    Служебные ссылки раздела («Сообщение о СЧА … по ОПИФ», квалификация инвесторов) не берём:
    из таблиц — только ссылки в строках фондов, из прочих мест — только вида «ОПИФ … «Название»».
    """
    s = _soup(content)
    tables = s.select("table.table--stats")
    require(bool(tables), "на /funds/ нет таблиц результатов (table.table--stats)")
    slugs: list[str] = []

    def add(a) -> None:
        m = _SLUG_HREF.match(a.get("href", ""))
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))

    for t in tables:
        for a in t.find_all("a", href=True):
            add(a)
    for a in s.find_all("a", href=True):
        if _FUND_LINK_TEXT.match(_text(a)):
            add(a)
    require(bool(slugs), "на /funds/ не найдено ни одной ссылки на страницу фонда")
    return slugs


def parse_rules(content) -> str:
    """Номер правил ДУ со страницы «Информация о фонде».

    Берём именно блок «Регистрация правил фонда …» — в подвале каждой страницы перечислены
    правила всех фондов УК, поиск по всему тексту дал бы чужой номер.
    """
    s = _soup(content)
    for h in s.find_all(["h4", "h3", "h5", "dt"]):
        if _text(h).lower().startswith("регистрация правил"):
            val = h.find_next_sibling()
            require(val is not None, "после «Регистрация правил фонда» нет значения")
            rules = norm_rules(_text(val))
            require(rules is not None, f"не номер правил: {_text(val)!r}")
            return rules
    raise UnknownFormat("на странице «Информация о фонде» нет блока «Регистрация правил фонда»")


def parse_history(content) -> list[SeriesPoint]:
    """CSV выгрузки /funds/<slug>/export/?format=csv -> точки по возрастанию даты."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    content = content.lstrip("\ufeff")
    rows = [r for r in csv.reader(io.StringIO(content), delimiter=";") if any(c.strip() for c in r)]
    require(bool(rows), "пустая выгрузка (нет даже заголовка)")
    head = [c.strip().lower() for c in rows[0]]

    def col(*words: str) -> int:
        for i, h in enumerate(head):
            if all(w in h for w in words):
                return i
        raise UnknownFormat(f"в заголовке CSV нет колонки {' '.join(words)!r}; есть {rows[0]}")

    i_date = col("дата")
    i_nav = col("чистых активов")
    i_price = col("стоимость", "пая")
    need = max(i_date, i_nav, i_price) + 1
    by_date: dict[date, SeriesPoint] = {}
    for r in rows[1:]:
        require(len(r) >= need, f"строка CSV короче заголовка: {r}")
        d = day(r[i_date])
        nav, price = num(r[i_nav]), num(r[i_price])
        # Нулевое/отрицательное значение — это отсутствие данных, а не ноль.
        nav = nav if nav and nav > 0 else None
        price = price if price and price > 0 else None
        if nav is None and price is None:
            continue
        p = SeriesPoint(d, nav, price)
        if d in by_date and by_date[d] != p:
            raise UnknownFormat(f"две разные строки на одну дату {d}: {by_date[d]} и {p}")
        by_date[d] = p
    return [by_date[d] for d in sorted(by_date)]


def parse_returns(content, source_url: str) -> dict[str, list[OfficialReturn]]:
    """Официальный прирост пая из таблиц /funds/: slug -> [OfficialReturn] (рублёвая строка).

    Таблица: «Паевой фонд» | [валюта USD*/RUB у валютных фондов] | «Стоимость пая» |
    «% к пред. значению» | 1 м | 3 м | 6 м | С начала года | 1 год | 3 года | 5 лет.
    Пустая ячейка (нет данных за период) — периода нет, а не 0.
    """
    s = _soup(content)
    tables = s.select("table.table--stats")
    require(bool(tables), "на /funds/ нет таблиц результатов (table.table--stats)")
    out: dict[str, list[OfficialReturn]] = {}
    for t in tables:
        head_rows = t.find("thead").find_all("tr") if t.find("thead") else t.find_all("tr")[:2]
        require(len(head_rows) >= 2, "у таблицы результатов нет двухстрочной шапки")
        top = [_text(c) for c in head_rows[0].find_all("th")]
        growth = [h for h in top if h.lower().startswith("прирост")]
        require(len(growth) == 1, f"нет колонки «Прирост стоимости пая на …»: {top}")
        # В той же ячейке ниже — «Опубликовано ДД.ММ.ГГГГ ЧЧ:ММ»: берём дату именно после «на».
        m = _AS_OF.match(growth[0])
        require(m is not None, f"нет даты «Прирост стоимости пая на ДД.ММ.ГГГГ»: {growth[0]!r}")
        as_of = day(m.group(1))
        cols = [norm_text(_text(c)) for c in head_rows[1].find_all("th")]
        idx = {}
        for label, period in _PERIODS.items():
            require(label in cols, f"в шапке нет колонки {label!r}: {cols}")
            idx[period] = cols.index(label)

        slug = None
        for tr in t.find_all("tr"):
            if tr.find("th"):
                continue                           # шапка
            tds = tr.find_all("td")
            if len(tds) == 1 and tds[0].get("colspan"):
                continue                           # строка-группа «Низкий риск» и т. п.
            a = tds[0].find("a", href=True) if tds else None
            if a is not None:
                m = _SLUG_HREF.match(a["href"])
                require(m is not None, f"ссылка фонда не на /funds/<slug>/: {a['href']!r}")
                slug, cells = m.group(1), tds[1:]
            else:
                require(slug is not None, f"строка без фонда: {_text(tr)!r}")
                cells = tds                        # вторая валютная строка (rowspan у названия)
            values = [_text(c) for c in cells]
            ccy = None
            if len(values) == len(cols) + 1:
                ccy = values[0].rstrip("*").strip().upper()
                values = values[1:]
            require(len(values) == len(cols),
                    f"в строке фонда {slug} {len(values)} значений, в шапке {len(cols)}: {values}")
            if ccy not in (None, _OFFICIAL_CCY):
                continue                           # справочный пересчёт в USD — не официальные данные
            require(slug not in out, f"у фонда {slug} две рублёвые строки доходности")
            rets = []
            for period, i in idx.items():
                v = pct(values[i])
                if v is not None:
                    rets.append(OfficialReturn(period, v, as_of, source_url))
            out[slug] = rets
    require(bool(out), "в таблицах /funds/ не найдено ни одной строки фонда")
    return out


# ---------- выплаты дохода (PDF-сообщения в «Раскрытии информации» фонда) ----------
#
# Доход «Фонда сбалансированного с выплатой дохода» начисляется за полумесячные отчётные периоды
# (1–15 и 16–конец месяца) и вычитается из СЧА на «дату, на которую выплачивается доход» (она же
# дата составления списка владельцев, 5-й рабочий день после конца периода; в ряду пая в этот день
# видно падение на сумму дохода). Деньги перечисляются раз в квартал. Размер дохода на пай УК
# публикует только в PDF «Сообщение о выплате дохода …» (раз в квартал, сразу за 6 периодов).
# Три вида этого сообщения:
#   A (до 01.2026): «Сумма дохода по одному инвестиционному паю …: За отчетный период с 01 по 15
#     июля 2025 г.: 2.539803665 руб.; …» — без дат определения дохода; даты берём из отдельных
#     «Сообщение о дате составления списка лиц …» («по состоянию на 22 октября 2025 г. …»);
#   B (05.2026): то же + блок «Даты, на которые … выплачивается доход: За отчетный период …: 22
#     января 2026 г.; …»;
#   C (с 08.2026): таблица «01.04.2026 – 15.04.2026 | 1 332 958.53 | 1.0889134505 | 22.04.2026».
# Текст из PDF достаём pypdf; он рвёт слова случайными пробелами («пр аво»), поэтому ключевые
# слова и пункты «За отчетный период …» ищем в тексте без пробелов.

DISCLOSURE_HREF = re.compile(r"/disclosure-of-information/reporting-of-mutual-funds/(\d+)/?$")
_MONTHS = {m: i for i, m in enumerate(
    ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
     "октября", "ноября", "декабря"), start=1)}
_PERIOD_ITEM = r"заотчетныйпериодс(\d{1,2})по(\d{1,2})([а-я]+)(\d{4})г\.?\)?:"
_WORD_DATE = r"(\d{1,2})([а-я]+)(\d{4})г"
_HDR_PER_SHARE = re.compile(r"(?:поодномуинвестиционномупаю|наодининвестиционныйпай)[^:]{0,40}:", re.I)
_HDR_TOTAL = re.compile(r"распределениюсредивладельцевинвестиционныхпаев:", re.I)
_HDR_RECORD = re.compile(r"даты,накоторые[^:]{0,200}выплачиваетсядоход[^:]{0,100}:", re.I)
_TABLE_ROW = re.compile(r"(\d{2}\.\d{2}\.\d{4}) ?[–—-] ?(\d{2}\.\d{2}\.\d{4}) (\d{1,3}(?: \d{3})*\.\d{2}) "
                        r"(\d+\.\d+) (\d{2}\.\d{2}\.\d{4})")
_TABLE_PERIOD = re.compile(r"\d{2}\.\d{2}\.\d{4}[–—-]\d{2}\.\d{2}\.\d{4}")
# заголовки документов раздела «Сообщения о деятельности управляющей компании»
_PAYOUT_TITLE = re.compile(r"^Сообщение о выплате дохода", re.I)
_NOTICE_TITLE = re.compile(r"^Сообщение о дате(?: составления списка лиц|, на которую)", re.I)


@dataclass(frozen=True)
class DisclosedDoc:
    published: date
    title: str
    url: str


@dataclass(frozen=True)
class IncomePeriod:
    start: date
    end: date
    amount: float               # доход на один пай, руб.
    record_date: date | None    # дата, на которую выплачивается (определяется) доход


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _word_date(d: str, month: str, year: str) -> date:
    m = _MONTHS.get(month.lower())
    require(m is not None, f"не месяц: {month!r}")
    return date(int(year), m, int(d))


def _period(d1: str, d2: str, month: str, year: str) -> tuple[date, date]:
    start, end = _word_date(d1, month, year), _word_date(d2, month, year)
    require(start <= end, f"период наоборот: {start}–{end}")
    return start, end


def _items(sq: str, header: re.Pattern, value: str) -> list[tuple[tuple[date, date], tuple]] | None:
    """Пункты «За отчетный период с … по … г.: <value>» сразу после заголовка; None — нет заголовка."""
    h = header.search(sq)
    if h is None:
        return None
    item = re.compile(_PERIOD_ITEM + value + r"[.;]*", re.I)       # «… руб.;» / «… 2026 г.;»
    out, pos = [], h.end()
    while (m := item.match(sq, pos)) is not None:
        out.append((_period(*m.groups()[:4]), m.groups()[4:]))
        pos = m.end()
    return out


def pdf_text(content: bytes) -> str:
    """Текст PDF. pypdf нет в зависимостях проекта: без него — ImportError (ловит адаптер)."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


def _has_pypdf() -> bool:
    return importlib.util.find_spec("pypdf") is not None


def _abs_url(href: str) -> str:
    """Ссылки на вложения — с кириллицей и пробелами: кодируем один раз и одинаково (ключ кэша)."""
    u = urlsplit(urljoin(BASE + "/", href))
    return f"{u.scheme}://{u.netloc}{quote(unquote(u.path), safe='/')}"


def parse_disclosure_link(content) -> str | None:
    """Ссылка «Информации о фонде» на страницу фонда в «Раскрытии информации» (там сообщения о доходе)."""
    for a in _soup(content).find_all("a", href=True):
        m = DISCLOSURE_HREF.search(urlsplit(a["href"]).path)
        if m:
            return f"{BASE}/disclosure-of-information/reporting-of-mutual-funds/{m.group(1)}/"
    return None


def parse_disclosure_docs(content, rules: str) -> list[DisclosedDoc]:
    """Документы страницы фонда в «Раскрытии информации»: дата публикации, заголовок, адрес.

    Сначала проверяем, что страница — нужного фонда (строка «Регистрация ФСФР России …: № …»).
    """
    s = _soup(content)
    reg = [td for td in s.find_all("td") if _text(td).lower().startswith("регистрация")]
    require(bool(reg), "на странице раскрытия нет строки «Регистрация … №»")
    val = reg[0].find_next_sibling("td")
    got = norm_rules(_text(val)) if val is not None else None
    require(got == rules, f"страница раскрытия другого фонда: правила {got}, ждали {rules}")
    tables = s.select("table.table--files")
    require(bool(tables), "на странице раскрытия нет таблиц документов (table.table--files)")
    docs = []
    for t in tables:
        for tr in t.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            a = tds[1].find("a", href=True)
            if a is None:
                continue
            m = _DATE.search(_text(tds[0]))
            if m is None and "выплат" not in _text(tr).lower():
                # опечатка УК в дате постороннего документа («24.06.024 … Отчёт об оценке») —
                # на выплаты не влияет, пропускаем строку
                continue
            require(m is not None, f"у сообщения о выплате нет даты публикации: {_text(tr)[:120]!r}")
            docs.append(DisclosedDoc(day(m.group(0)), _text(a), _abs_url(a["href"])))
    return docs


def parse_payouts(text: str, rules: str) -> list[IncomePeriod]:
    """«Сообщение о выплате дохода …» (текст PDF) -> доход на пай по отчётным периодам."""
    sq = _squash(text)
    require("СООБЩЕНИЕОВЫПЛАТЕДОХОДА" in sq.upper(), "это не «Сообщение о выплате дохода»")
    require(f"№{rules}" in sq, f"в сообщении нет номера правил {rules}")
    out: list[IncomePeriod] = []
    rows = _TABLE_ROW.findall(re.sub(r"\s+", " ", text))
    if rows:                                                   # вид C: таблица
        require(len(rows) == len(_TABLE_PERIOD.findall(sq)),
                "в таблице сообщения не все строки периодов разобраны")
        for d1, d2, _total, amount, rec in rows:
            out.append(IncomePeriod(day(d1), day(d2), float(amount), day(rec)))
    else:                                                      # виды A и B: пункты списком
        per = _items(sq, _HDR_PER_SHARE, r"(\d+(?:\.\d+)?)руб")
        require(bool(per), "нет блока «доход на один инвестиционный пай» с периодами")
        total = _items(sq, _HDR_TOTAL, r"(\d+(?:\.\d+)?)руб")
        require(total is not None and [p for p, _ in total] == [p for p, _ in per],
                "периоды в блоках «сумма дохода» и «на один пай» не совпадают")
        rec = _items(sq, _HDR_RECORD, _WORD_DATE)
        rec_by = {p: _word_date(*v) for p, v in rec or []}
        require(rec is None or set(rec_by) == {p for p, _ in per},
                "периоды в блоке «даты, на которые выплачивается доход» не совпадают")
        for (start, end), (amount,) in per:
            out.append(IncomePeriod(start, end, float(amount), rec_by.get((start, end))))
    require(len({(p.start, p.end) for p in out}) == len(out), "в сообщении повторяется период")
    require(all(p.amount > 0 for p in out), "нулевой или отрицательный доход на пай")
    return out


def parse_record_notice(text: str, rules: str) -> tuple[date, tuple[date, date] | None] | None:
    """Сообщение о дате определения дохода -> (дата, период или None); None — список не для дохода."""
    sq = _squash(text)
    up = sq.upper()
    require(f"№{rules}" in sq, f"в сообщении нет номера правил {rules}")
    if "СООБЩЕНИЕОДАТЕ,НАКОТОРУЮ" in up and "ВЫПЛАЧИВАЕТСЯДОХОД" in up:      # с 08.2026
        m = re.search(_PERIOD_ITEM + _WORD_DATE, sq, re.I)
        require(m is not None, "в сообщении нет «за отчетный период …: <дата>»")
        return _word_date(*m.groups()[4:]), _period(*m.groups()[:4])
    if "СООБЩЕНИЕОДАТЕСОСТАВЛЕНИЯСПИСКАЛИЦ" in up:
        m = re.search(r"посостояниюна" + _WORD_DATE + r"\.?будетсоставленсписок", sq, re.I)
        require(m is not None, "в сообщении нет «по состоянию на <дата> будет составлен список»")
        if "правонаполучениедохода" not in sq.lower():
            return None                      # список для другой цели (например, общего собрания)
        return _word_date(*m.groups()), None
    raise UnknownFormat("это не сообщение о дате определения дохода")


def assign_record_dates(periods: list[IncomePeriod],
                        notices: list[tuple[date, tuple[date, date] | None]]) -> list[IncomePeriod]:
    """Дата определения дохода для периодов из сообщений вида A (в самом сообщении её нет).

    Сообщение с периодом — по периоду; старое «по состоянию на …» — ближайшая неиспользованная
    дата через 5–31 день после конца периода (по правилам это 5-й рабочий день после него).
    """
    explicit = {p: d for d, p in notices if p is not None}
    loose = sorted({d for d, p in notices if p is None})
    used: set[date] = set()
    out = []
    for p in sorted(periods, key=lambda x: x.start):
        if p.record_date is None:
            d = explicit.get((p.start, p.end))
            if d is None:
                cands = [x for x in loose if x not in used and 5 <= (x - p.end).days <= 31]
                require(bool(cands), f"нет сообщения о дате определения дохода за {p.start}–{p.end}")
                d = cands[0]
                used.add(d)
            p = IncomePeriod(p.start, p.end, p.amount, d)
        out.append(p)
    dates = [p.record_date for p in out]
    require(dates == sorted(set(dates)), f"даты определения дохода не идут по порядку периодов: {dates}")
    return out


def fetch_payouts(ctx: Context, rules: str, disclosure_url: str) -> tuple[list[Payout], list[str]]:
    """Выплаты дохода на пай с ctx.since -> (выплаты, предупреждения). Нет сообщений — ([], [])."""
    raw = ctx.http.get(disclosure_url, fmt="html", funds=[rules])
    docs = parse_disclosure_docs(raw.content(), rules)
    messages = sorted((d for d in docs if _PAYOUT_TITLE.match(d.title) and d.published >= ctx.since),
                      key=lambda d: d.published)
    notice_docs = sorted((d for d in docs if _NOTICE_TITLE.match(d.title)), key=lambda d: d.published)
    if not messages and not notice_docs:
        return [], []                                  # фонд доход не выплачивает
    if not _has_pypdf():
        return [], ["размер выплат дохода УК публикует только в PDF-сообщениях, а pypdf не установлен "
                    "— в этот раз выплаты не собраны (собранные раньше сохраняются при слиянии)"]

    def text(doc: DisclosedDoc) -> str:
        return pdf_text(ctx.http.get(doc.url, fmt="pdf", funds=[rules]).content())

    found: list[tuple[IncomePeriod, str]] = []
    notices: dict[str, tuple[date, tuple[date, date] | None] | None] = {}
    for msg in messages:
        periods = parse_payouts(text(msg), rules)
        if any(p.record_date is None for p in periods):
            # даты — из сообщений «о дате составления списка лиц» между началом периодов и сообщением
            lo = min(p.start for p in periods)
            for n in notice_docs:
                if lo <= n.published <= msg.published and n.url not in notices:
                    notices[n.url] = parse_record_notice(text(n), rules)
            periods = assign_record_dates(periods, [v for v in notices.values() if v is not None])
        found += [(p, msg.url) for p in periods]

    payouts: dict[date, Payout] = {}
    for p, url in found:
        require(p.record_date not in payouts or payouts[p.record_date].amount == p.amount,
                f"две разные выплаты на {p.record_date}")
        if p.record_date >= ctx.since:
            payouts[p.record_date] = Payout(p.record_date, p.amount, url)
    notes = []
    last = max((p.record_date for p, _ in found), default=None)
    pending = [n for n in notice_docs if last is None or n.published > last]
    if pending:
        notes.append(f"размер дохода на пай за отчетные периоды после {last:%d.%m.%Y} УК ещё не "
                     f"опубликовала (сообщений о датах определения дохода с тех пор: {len(pending)}, "
                     f"последнее от {pending[-1].published:%d.%m.%Y}) — доходность по ряду с учётом "
                     "выплат за периоды, захватывающие эти даты, будет ниже официальной"
                     if last else "есть сообщения о датах определения дохода, но нет сообщений "
                     "о его размере — выплаты не собраны")
    return [payouts[d] for d in sorted(payouts)], notes


# ---------- адаптер ----------

class TkbipAdapter(Adapter):
    key = "tkbip"
    title = "ТКБ Инвестмент Партнерс"
    uk_inns = ("7825489723",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        memo: dict[str, object] = {}

        def once(key: str, fn):
            # Общие для УК документы качаем один раз; ошибку тоже запоминаем, чтобы guarded()
            # поставил её каждому фонду, а не ронял весь адаптер.
            if key not in memo:
                try:
                    memo[key] = ("ok", fn())
                except Exception as e:  # noqa: BLE001
                    memo[key] = ("err", e)
            kind, val = memo[key]
            if kind == "err":
                raise val
            return val

        def catalog() -> bytes:
            return ctx.http.get(CATALOG_URL, fmt="html", funds=all_rules).content()

        def rules_map() -> tuple[dict[str, tuple[str, str | None]], list[Exception]]:
            """rules -> (slug, страница раскрытия) по «Информации о фонде»; сбой одной страницы не мешает другим."""
            slugs = parse_catalog(once("catalog", catalog))
            found: dict[str, tuple[str, str | None]] = {}
            errors: list[Exception] = []
            for slug in slugs:
                try:
                    raw = ctx.http.get(f"{BASE}/funds/{slug}/information/", fmt="html", funds=all_rules)
                    found.setdefault(parse_rules(raw.content()), (slug, parse_disclosure_link(raw.content())))
                except Exception as e:  # noqa: BLE001 — решим при сопоставлении конкретного фонда
                    ctx.log.warning("[tkbip] %s: страница «Информация о фонде»: %s", slug, e)
                    errors.append(e)
            return found, errors

        def one(fund: Fund) -> FundData:
            found, errors = once("rules", rules_map)
            slug, disclosure_url = found.get(fund.rules, (None, None))
            if slug is None:
                if errors:
                    raise errors[0]        # не смогли проверить все страницы — это не «не найден»
                return FundData(fund.rules, status="not_found", page_url=CATALOG_URL,
                                reason=f"на {CATALOG_URL} нет фонда с правилами № {fund.rules}")
            page_url = f"{BASE}/funds/{slug}/results/"
            start = ctx.start_for(fund.rules)
            raw = ctx.http.get(f"{BASE}/funds/{slug}/export/", fmt="csv", funds=[fund.rules], params={
                "dateFrom": start.strftime("%d.%m.%Y"),
                "dateTo": EXPORT_DATE_TO,
                "format": "csv",
            })
            points = parse_history(raw.content())
            fd = FundData(fund.rules, currency=_OFFICIAL_CCY, page_url=page_url,
                          segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status = "partial"
                fd.reason = f"выгрузка УК с {start:%d.%m.%Y} не содержит значений СЧА и пая"
            # Доходность — отдельно: поломка таблицы доходности не должна стоить фонду ряда,
            # но и молча пропасть не должна (errors видны в отчёте качества).
            try:
                returns = once("returns", lambda: parse_returns(once("catalog", catalog), CATALOG_URL))
            except UnknownFormat as e:
                fd.errors.append(f"доходность с {CATALOG_URL}: незнакомый формат: {e}")
            else:
                if slug in returns:
                    fd.official_returns = returns[slug]
                else:
                    fd.warnings.append(f"фонда нет в таблице доходности {CATALOG_URL}")
            # Выплаты дохода — тоже отдельно: без них ряд всё равно нужен, но поломка видна.
            if disclosure_url is None:
                fd.warnings.append("в «Информации о фонде» нет ссылки на раскрытие — выплаты дохода не проверены")
            else:
                try:
                    fd.payouts, notes = fetch_payouts(ctx, fund.rules, disclosure_url)
                    fd.warnings += notes
                except UnknownFormat as e:
                    fd.errors.append(f"выплаты дохода ({disclosure_url}): незнакомый формат: {e}")
                except FetchError as e:
                    fd.warnings.append(f"выплаты дохода не собраны: {e.reason} ({e.url})")
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]
