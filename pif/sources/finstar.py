"""ООО УК «Финстар Капитал» (finstar-capital.ru, сайт на 1С-Битрикс): СЧА и пай — данные графика
на странице фонда, доходность — ежемесячный КИД (PDF).

Источники (сайт УК; http://finstar-capital.ru из реестра отвечает тем же сайтом по https):
* список фондов: https://finstar-capital.ru/funds/ — карточки div.products div.item
  (класс not-qual / qual), название и ссылка /funds/<slug>/ в .title a;
* страница фонда: https://finstar-capital.ru/funds/<slug>/ — в одном HTML всё нужное:
  - «Сведения о фонде» → «Дата регистрации и Регистрационный номер фонда»: «08.09.2025 № 7276»
    или «№ 7976 от 20.07.2026 г.» — по нему сопоставляем с реестром;
  - графики Highcharts «Стоимость 1 пая (руб)» и «Стоимость чистых активов (руб)»: в скрипте
    страницы JSON-массивы `let chart1Data = [{"x": <epoch ms UTC>, "y": <пай>}, …]` и
    `let chart2Data = [{"x": …, "y": <СЧА>}, …]` — вся история фонда с завершения формирования,
    каждый рабочий день (у «Рантье» ещё chart1ProfitData — пай с учётом выплаченного дохода, его
    не берём: расчётная стоимость пая — chart1Data). Диапазон дат не задаётся, ctx.start_for не нужен;
  - таблица «Стоимость пая и СЧА» (две последние даты, заголовки «Стоимость пая, RUR», «СЧА, RUR»):
    из неё валюта пая (RUR -> RUB) и сверка последних точек графика;
  - вкладка «Ключевой информационный документ»: ссылки «Ключевой информационный документ по
    состоянию на 30.09.2026» (бывают «(скорректированный)») на PDF в /upload/iblock/….
СЧА — в рублях, пай — в рублях, числа как есть (без множителей). БПИФ «ФИНСТАР - Денежный рынок ПЛЮС»
берём так же, со страницы фонда на сайте УК (не с биржи).

Официальная доходность за 1/3/6/12 мес на HTML-страницах не публикуется — только в КИД, раздел 5
«Основные результаты инвестирования» (строки «1 месяц / 3 месяца / 6 месяцев / Год»: доходность,
отклонение от инфляции, от индикатора). Берём самый свежий КИД. Текст из PDF достаём библиотекой
pypdf, если она установлена (в зависимостях проекта её нет: без неё доходность не заполняется,
а в warnings фонда пишется причина и адрес КИД). Сломанный КИД не роняет ряд фонда — тоже warning.

Сопоставление: кандидаты из списка — сначала совпавшие по названию (norm_text), затем остальные фонды
того же типа (ОПИФ/ИПИФ/БПИФ); номер правил на странице фонда обязан совпасть с rules.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

try:  # необязательная зависимость: нужна только для текста КИД (PDF)
    from pypdf import PdfReader
except ImportError:  # pragma: no cover — зависит от окружения
    PdfReader = None

BASE = "https://finstar-capital.ru"
LIST_URL = f"{BASE}/funds/"

TYPE_PREFIX = {"open": "ОПИФ", "interval": "ИПИФ", "exchange": "БПИФ"}
CURRENCIES = {"RUR": "RUB", "RUB": "RUB", "USD": "USD", "EUR": "EUR", "CNY": "CNY"}
KID_PERIODS = {"1 месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "Год": "1y"}


@dataclass
class ListedFund:
    name: str
    url: str


@dataclass
class KidLink:
    as_of: date
    corrected: bool          # «(скорректированный)» — заменяет КИД на ту же дату
    published: date | None
    url: str


def _soup(content: bytes | str) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True).replace("\xa0", " ").replace("﻿", "")).strip()


# ---------- чистые разборщики (на них тесты с настоящими фикстурами) ----------

def parse_fund_list(content: bytes | str) -> list[ListedFund]:
    """Каталог /funds/ -> карточки фондов в порядке сайта."""
    links = _soup(content).select("div.products div.item .title a[href]")
    require(bool(links), "список фондов: нет карточек div.products div.item .title a")
    out = []
    for a in links:
        name = _text(a)
        require(bool(name) and a["href"].startswith("/funds/"),
                f"список фондов: неожиданная карточка {name!r} -> {a['href']!r}")
        out.append(ListedFund(name=name, url=urljoin(BASE, a["href"])))
    return out


def _detail(soup: BeautifulSoup, label: str) -> str | None:
    """Значение строки «Сведения о фонде» по началу подписи."""
    for item in soup.select("div.details div.item div.cols"):
        cols = item.find_all("div", class_="col", recursive=False)
        if len(cols) >= 2 and _text(cols[0]).startswith(label):
            return _text(cols[1])
    return None


def parse_rules(content: bytes | str) -> str:
    """Страница фонда -> номер правил из «Дата регистрации и Регистрационный номер фонда»."""
    value = _detail(_soup(content), "Дата регистрации и")
    require(value is not None, "сведения о фонде: нет строки «Дата регистрации и Регистрационный номер фонда»")
    m = re.search(r"№\s*(\d+(?:-[0-9А-ЯЁA-Z]+)*)", value)
    require(m is not None, f"сведения о фонде: не нашёл номер правил в {value!r}")
    return m.group(1)


def _js_array(html: str, name: str) -> list:
    """JSON-массив из `let <name> = [...]` в скрипте страницы."""
    m = re.search(rf"\blet\s+{name}\s*=\s*", html)
    require(m is not None, f"график: в странице нет массива {name}")
    try:
        value, _ = json.JSONDecoder().raw_decode(html, m.end())
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"график: {name} — не JSON ({e.msg})") from e
    require(isinstance(value, list), f"график: {name} — не массив")
    return value


def _chart_values(html: str, name: str) -> dict[date, float | None]:
    out = {}
    for p in _js_array(html, name):
        require(isinstance(p, dict) and "x" in p and "y" in p, f"график: точка {name} без x/y: {p!r}"[:200])
        x = p["x"]
        require(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 10**11,
                f"график: в {name} x не время в мс: {x!r}")
        d = datetime.fromtimestamp(x / 1000, timezone.utc).date()     # Highcharts useUTC: полночь UTC
        require(d not in out, f"график: в {name} дата {d} повторяется")
        out[d] = num(p["y"])
    return out


def parse_series(content: bytes | str) -> list[SeriesPoint]:
    """Графики страницы фонда -> точки: chart1Data — расчётная стоимость пая, chart2Data — СЧА."""
    html = content.decode("utf-8") if isinstance(content, bytes) else content
    price = _chart_values(html, "chart1Data")
    nav = _chart_values(html, "chart2Data")
    return [SeriesPoint(date=d, nav=nav.get(d), price=price.get(d)) for d in sorted(set(price) | set(nav))]


def parse_table(content: bytes | str) -> tuple[str, list[SeriesPoint]]:
    """Таблица «Стоимость пая и СЧА» -> (валюта пая, последние точки). Для валюты и сверки с графиком."""
    soup = _soup(content)
    box = soup.select_one("div.product-char")
    require(box is not None, "таблица стоимости: нет div.product-char")
    heads = [_text(c) for c in box.select(".product-char__head .cols:not(.char__head_top) > .col")]
    require(len(heads) >= 3 and heads[0] == "Дата", f"таблица стоимости: неожиданный заголовок {heads}")
    m_price = re.fullmatch(r"Стоимость пая, ([A-Z]{3})", heads[1])
    m_nav = re.fullmatch(r"СЧА, ([A-Z]{3})", heads[2])
    require(m_price is not None and m_nav is not None, f"таблица стоимости: неожиданные колонки {heads[:3]}")
    cur = CURRENCIES.get(m_price.group(1))
    require(cur is not None, f"таблица стоимости: незнакомая валюта пая {m_price.group(1)!r}")
    points = []
    for row in box.select(".product-char__items .product-char__item > .cols"):
        cols = row.find_all("div", class_="col", recursive=False)
        require(len(cols) >= 3, "таблица стоимости: в строке меньше трёх колонок")
        vals = [c.select_one("span.nows") for c in cols[:3]]
        require(all(v is not None for v in vals), "таблица стоимости: нет значений span.nows")
        points.append(SeriesPoint(date=day(_text(vals[0])), price=num(_text(vals[1])), nav=num(_text(vals[2]))))
    return cur, sorted(points, key=lambda p: p.date)


def parse_kid_links(content: bytes | str) -> list[KidLink]:
    """Вкладка КИД страницы фонда -> ссылки на PDF, свежие первыми."""
    out = []
    for a in _soup(content).select("div.docs__item .title a[href]"):
        title = _text(a)
        m = re.match(r"Ключевой информационный документ по состоянию на (\d{2}\.\d{2}\.\d{4})", title)
        if not m:
            continue
        item = a.find_parent("div", class_="docs__item")
        pub = re.search(r"опубликовано (\d{2}\.\d{2}\.\d{4})", _text(item))
        out.append(KidLink(as_of=day(m.group(1)), corrected="скорректирован" in title.lower(),
                           published=day(pub.group(1)) if pub else None, url=urljoin(BASE, a["href"])))
    out.sort(key=lambda k: (k.as_of, k.corrected, k.published or date.min), reverse=True)
    return out


def kid_text(pdf: bytes) -> str:
    """Текст PDF КИД (нужен pypdf)."""
    require(PdfReader is not None, "pypdf не установлен")
    try:
        reader = PdfReader(io.BytesIO(pdf))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # noqa: BLE001 — битый PDF для нас просто незнакомый формат
        raise UnknownFormat(f"КИД: не читается как PDF ({type(e).__name__}: {e})") from e


def parse_kid_returns(text: str, source_url: str) -> list[OfficialReturn]:
    """Текст КИД -> доходность за 1/3/6/12 мес из раздела 5 (пустые строки таблицы пропускаются)."""
    m_asof = re.search(r"Ключевой информационный документ по состоянию на\s*(\d{2}\.\d{2}\.\d{4})", text)
    require(m_asof is not None, "КИД: нет «Ключевой информационный документ по состоянию на <дата>»")
    m5 = re.search(r"Раздел 5\.?\s*Основные результаты инвестирования(.*?)(Раздел 6|$)", text, re.S)
    require(m5 is not None, "КИД: нет раздела 5 «Основные результаты инвестирования»")
    section = m5.group(1).replace("−", "-")
    require(re.search(r"Период\s+Доходность", section) is not None,
            "КИД: в разделе 5 нет таблицы «Период / Доходность»")
    as_of = day(m_asof.group(1))
    out = []
    for label, period in KID_PERIODS.items():
        row = re.search(rf"^[ \t]*{label}(?=[ \t]|$)[ \t]*(-?\d+(?:[.,]\d+)?)?", section, re.M)
        require(row is not None, f"КИД: в таблице доходности нет строки «{label}»")
        if row.group(1) is not None:
            out.append(OfficialReturn(period=period, value_pct=pct(row.group(1)), as_of=as_of,
                                      source_url=source_url))
    return out


# ---------- адаптер ----------

class FinstarAdapter(Adapter):
    key = "finstar"
    title = "УК «Финстар Капитал»"
    uk_inns = ("9703154610",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # Список и страницы фондов качаются внутри guarded: повторный get того же адреса
        # берётся из кэша, а сбой списка становится статусом фонда, а не роняет адаптер.
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _find(self, ctx: Context, fund: Fund, listed: list[ListedFund]):
        """Страница фонда с тем же номером правил -> (адрес, RawDoc, что проверили)."""
        want = norm_text(fund.name)
        prefix = TYPE_PREFIX.get(fund.type, "")
        by_name = [f for f in listed if norm_text(f.name) == want]
        same_type = [f for f in listed if f not in by_name and re.search(rf"\b{prefix}\b", f.name)]
        checked = []
        for cand in by_name + same_type:
            page = ctx.http.get(cand.url, fmt="html")
            rules = parse_rules(page.content())
            checked.append(f"«{cand.name}» — № {rules}")
            if rules == fund.rules:
                if norm_text(cand.name) != want:
                    ctx.log.info("[finstar] %s: на сайте «%s», совпал номер правил", fund.rules, cand.name)
                # повторный запрос берётся из кэша и лишь помечает документ этим фондом в манифесте
                return cand.url, ctx.http.get(cand.url, fmt="html", funds=[fund.rules]), checked
        return None, None, checked

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        lst = ctx.http.get(LIST_URL, fmt="html", funds=[fund.rules])
        page_url, page, checked = self._find(ctx, fund, parse_fund_list(lst.content()))
        if page is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"на {LIST_URL} нет фонда с правилами № {fund.rules} "
                                   f"(проверены: {'; '.join(checked) or 'кандидатов нет'})")

        html = page.content()
        currency, table = parse_table(html)
        points = parse_series(html)
        fd = FundData(fund.rules, currency=currency, page_url=page_url)
        seg = self.segment(page, points, page_url=page_url, since=ctx.since)
        if seg.points:
            fd.segments.append(seg)

        # Сверка: последние строки таблицы на странице должны совпасть с точками графика.
        by_date = {p.date: p for p in points}
        for row in table:
            p = by_date.get(row.date)
            if p is None or (p.price, p.nav) != (row.price, row.nav):
                fd.warnings.append(f"таблица на странице на {row.date:%d.%m.%Y} ({row.price}; {row.nav}) "
                                   f"не совпала с графиком ({p and p.price}; {p and p.nav})")

        self._returns(ctx, fund, fd, html)

        if not fd.segments:
            text = _text(_soup(html))
            if fund.status == "Формируется" or "стадии формирования" in text:
                fd.status, fd.reason = "not_formed", "фонд формируется, график стоимости пая и СЧА пуст"
            elif points:
                fd.status, fd.reason = "partial", f"на графике УК нет точек начиная с {ctx.since:%d.%m.%Y}"
            else:
                fd.status, fd.reason = "no_history", "на странице фонда график стоимости пая и СЧА пуст"
        return fd

    def _returns(self, ctx: Context, fund: Fund, fd: FundData, html: bytes) -> None:
        """Доходность из самого свежего КИД; любые проблемы с КИД — warning, ряд не трогаем."""
        kids = parse_kid_links(html)
        if not kids:
            fd.warnings.append("на странице фонда нет КИД — официальной доходности за периоды нет")
            return
        kid = kids[0]
        if PdfReader is None:
            fd.warnings.append(f"доходность за 1/3/6/12 мес публикуется только в КИД (PDF) {kid.url}; "
                               "pypdf не установлен — не разобрана")
            return
        raw = ctx.http.get(kid.url, fmt="pdf", funds=[fund.rules])
        try:
            fd.official_returns = parse_kid_returns(kid_text(raw.content()), kid.url)
        except UnknownFormat as e:
            ctx.log.error("[finstar] %s: КИД %s не разобран: %s", fund.rules, kid.url, e)
            fd.warnings.append(f"доходность из КИД {kid.url} не разобрана: {e}")
            return
        if not fd.official_returns:
            fd.warnings.append(f"в КИД на {kid.as_of:%d.%m.%Y} доходность за периоды не заполнена")
