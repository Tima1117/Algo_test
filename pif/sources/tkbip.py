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
  рабочий день, новые даты сверху. Совпадает с данными графика на той же странице.

Валюта: УК официально считает СЧА и пай в рублях («Пай (руб.)», «СЧА (руб.)»). Для «Фонда
валютных облигаций» и «Золота» сайт дополнительно показывает пересчёт в USD по курсу ЦБ с
пометкой «носят информационный характер и не являются данными официальной отчётности», а CSV
отдаёт рубли. Поэтому ряд и доходность берём рублёвые, currency='RUB'.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://www.tkbip.ru"
CATALOG_URL = f"{BASE}/funds/"

_SLUG_HREF = re.compile(r"^/funds/([a-z0-9-]+)/$")
_RULES = re.compile(r"(\d{4})\s*[-–—]\s*(\d{8})")
_AS_OF = re.compile(r"прирост стоимости пая на (\d{2}\.\d{2}\.\d{4})", re.I)
_FUND_LINK_TEXT = re.compile(r"^[ОИБ]ПИФ\b.*«")      # «ОПИФ рыночных финансовых инструментов «…»»
# Подписи колонок доходности в таблицах /funds/ (после norm_text) -> период OfficialReturn.
_PERIODS = {"1 м": "1m", "3 м": "3m", "6 м": "6m", "1 год": "1y"}
_OFFICIAL_CCY = "RUB"


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

        def rules_map() -> tuple[dict[str, str], list[Exception]]:
            """rules -> slug по страницам «Информация о фонде»; сбой одной страницы не мешает другим."""
            slugs = parse_catalog(once("catalog", catalog))
            found: dict[str, str] = {}
            errors: list[Exception] = []
            for slug in slugs:
                try:
                    raw = ctx.http.get(f"{BASE}/funds/{slug}/information/", fmt="html", funds=all_rules)
                    found.setdefault(parse_rules(raw.content()), slug)
                except Exception as e:  # noqa: BLE001 — решим при сопоставлении конкретного фонда
                    ctx.log.warning("[tkbip] %s: страница «Информация о фонде»: %s", slug, e)
                    errors.append(e)
            return found, errors

        def one(fund: Fund) -> FundData:
            found, errors = once("rules", rules_map)
            slug = found.get(fund.rules)
            if slug is None:
                if errors:
                    raise errors[0]        # не смогли проверить все страницы — это не «не найден»
                return FundData(fund.rules, status="not_found", page_url=CATALOG_URL,
                                reason=f"на {CATALOG_URL} нет фонда с правилами № {fund.rules}")
            page_url = f"{BASE}/funds/{slug}/results/"
            start = ctx.start_for(fund.rules)
            raw = ctx.http.get(f"{BASE}/funds/{slug}/export/", fmt="csv", funds=[fund.rules], params={
                "dateFrom": start.strftime("%d.%m.%Y"),
                "dateTo": date.today().strftime("%d.%m.%Y"),
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
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]
