"""АО «УК МКБ Инвестиции» (www.mkb-am.ru) — 2 БПИФ: «МКБ – Российские Дивидендные Акции» (MKBD)
и «РЕГИОН – Индекс МосБиржи государственных облигаций (1-3 года)» (SUGB).

Где что лежит (разведка 04.11.2026). Из сети разработчика сайт не открывается; из GitHub Actions он
отвечает на первый запрос запуска, а следующие соединения минут 20–30 обрываются по ConnectTimeout
(сайт за Qrator, 178.248.237.12). Так получены главная и страница MKBD за 04.11.2026; страница SUGB
и полная месячная таблица — из копий веб-архива от 20–21.11.2025 (ответ того же сайта, вёрстка та же:
те же классы, имена фильтров и вкладки).
* robots.txt на обоих адресах — «User-Agent: * / Disallow: /»; исключение — в config.ROBOTS_EXCEPTIONS;
* главная https://www.mkb-am.ru/:
  - форма подписки на раскрытие: <input name="pif" id="<id>" value="БПИФ рыночных финансовых
    инструментов «…»"> — id страницы раскрытия фонда (31747 — MKBD, 29150 — SUGB);
  - подвал: «Биржевой паевой инвестиционный фонд … «…» (правила доверительного управления
    зарегистрированы Банком России 21.06.2021 за №4474).» — номер правил. Фонд реестра сопоставляем
    по номеру правил, а id находим по тому же названию в форме (тип фонда + текст в «»);
* страница раскрытия /information/products/<id>/ (Битрикс, шаблон region_2020_red):
  - заголовок div.info-section__content-title — то же название, что в форме;
  - вкладка «Стоимость пая и СЧА» (div#tab-1): фильтр select info-pai__period («За месяц» / «За год»),
    info-pai__year, info-pai__month и таблица div.info-table «Информация раскрыта | Дата расчета |
    Стоимость пая | СЧА | Изменение пая / СЧА к пред. дате» — по рабочим дням, по умолчанию за
    текущий месяц. Единиц в шапке нет: числа в рублях (MKBD на 02.11.2026: СЧА 202 757 358,36,
    пай 997,80), фонды рублёвые;
  - другие месяцы: скрипт шаблона (getInfomationTab_1) шлёт POST на адрес той же страницы с полями
    period=month, tabId=tab-1, year=ГГГГ, month=ММ и подставляет ответ вместо вкладки #tab-1. Тот же
    запрос делаем и мы; ответ проверяем строго (выбранные в ответе год и месяц, даты строк).
* официальной доходности за 1/3/6/12 мес. на страницах раскрытия нет (вкладки: Стоимость пая и СЧА,
  Отчетность — справки XLSX за месяц, Официальные сообщения, Документы фонда, Информация о фонде);
  ISIN и тикер сайт не показывает.

Ловушки:
* В подвале и форме одни и те же фонды названы по-разному: «Биржевой паевой инвестиционный фонд …» и
  «БПИФ …»; у «Заблокированных активов…» внутри названия вложенные «», закрывающая — одна.
* Строка таблицы — div-ы с подписью для мобильной вёрстки (.info-table__col-header) и значением
  (.info-table__value); колонки берём по подписи, а не по порядку.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..model import Fund, FundData, SeriesPoint
from ..parsing import day, norm_text, num, require
from .base import Adapter, Context

BASE = "https://www.mkb-am.ru"
HOME_URL = f"{BASE}/"
PAGE_URL = BASE + "/information/products/{id}/"
NAV_TAB = "tab-1"                                          # вкладка «Стоимость пая и СЧА»
AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}      # как у $.ajax на странице

DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
# «… «Название» (правила доверительного управления зарегистрированы Банком России 21.06.2021 за №4474).»
_FOOTER_RE = re.compile(r"(?P<name>.+?)\s*\(\s*правила доверительного управления зарегистрированы\s+"
                        r"Банком России\s+\d{2}\.\d{2}\.\d{4}\s+за\s*№\s*(?P<rules>\d+(?:\s*[-–—‑]\s*\d+)?)\s*\)",
                        re.I)
# Тип фонда по началу названия: в подвале полные слова, в форме — сокращения.
_KINDS = (("бпиф", "exchange"), ("биржевой", "exchange"), ("опиф", "open"), ("открытый", "open"),
          ("ипиф", "interval"), ("интервальный", "interval"), ("зпиф", "closed"), ("закрытый", "closed"))
# Колонки таблицы СЧА (подпись в нижнем регистре) -> поле.
COLUMNS = {"дата расчета": "date", "стоимость пая": "price", "сча": "nav"}
# Насколько дата расчёта может быть раньше начала запрошенного месяца. В таблице за ноябрь 2025 г. первая
# строка — расчёт за 01.11 (31.10, раскрытый 01.11, не попал), то есть месяц отбирается по дате расчёта;
# небольшой допуск — на случай отбора по дате раскрытия (в январе это расчёт за 31.12 после праздников).
LOOKBACK_DAYS = 15


@dataclass
class MkbSiteFund:
    rules: str
    kind: str                   # exchange / open / interval / closed — по названию на сайте
    name: str                   # название из подвала (текст в «»)
    page_id: str | None         # id страницы раскрытия из формы подписки; None — в форме фонда нет


@dataclass
class MkbFundPage:
    title: str                                  # заголовок страницы раскрытия
    month: tuple[int, int]                      # (год, месяц), за который показана таблица
    years: list[int] = field(default_factory=list)   # годы в фильтре таблицы
    points: list[SeriesPoint] = field(default_factory=list)
    currency: str = "RUB"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm_rules(s: str) -> str:
    """'№ 4474' / '0120–14241368' -> '4474' / '0120-14241368' (как в реестре ЦБ)."""
    return re.sub(r"[\s№]", "", str(s)).replace("–", "-").replace("—", "-").replace("‑", "-")


def fund_kind(title: str) -> str | None:
    s = _clean(title).lower()
    return next((kind for prefix, kind in _KINDS if s.startswith(prefix)), None)


def name_key(title: str) -> str:
    """«БПИФ рыночных финансовых инструментов «МКБ – Российские Дивидендные Акции»» и
    «МКБ – Российские Дивидендные Акции» -> один ключ: текст в «» (если они есть) через norm_text."""
    s = _clean(title)
    m = re.search(r"«(.+)»", s)        # жадно: у «Заблокированных активов…» вложенные кавычки
    return norm_text(m.group(1) if m else s)


# ---------- главная: номер правил -> id страницы раскрытия ----------

def parse_home(html: str) -> dict[str, MkbSiteFund]:
    """Подвал (название + номер правил) и форма подписки (название + id) -> {номер правил: фонд}."""
    soup = BeautifulSoup(html, "lxml")
    ids: dict[tuple[str | None, str], list[str]] = {}
    for inp in soup.select('input[name="pif"][id]'):
        title = _clean(inp.get("value", ""))
        require(bool(re.fullmatch(r"\d+", inp["id"])) and bool(title),
                f"в форме подписки фонд без числового id или названия: {inp}")
        ids.setdefault((fund_kind(title), name_key(title)), []).append(inp["id"])
    require(len(ids) >= 2, f"в форме подписки на главной найдено фондов: {len(ids)} — сменилась вёрстка?")

    out: dict[str, MkbSiteFund] = {}
    for p in soup.find_all("p"):
        m = _FOOTER_RE.fullmatch(_clean(p.get_text(" ")).rstrip(". "))
        if not m:
            continue
        rules, name = norm_rules(m.group("rules")), m.group("name")
        kind = fund_kind(name)
        require(kind is not None, f"не понял тип фонда в подвале: {name!r}")
        require(rules not in out, f"номер правил {rules} в подвале дважды")
        found = ids.get((kind, name_key(name)), [])
        require(len(found) <= 1, f"в форме подписки несколько фондов «{name}»: {found}")
        out[rules] = MkbSiteFund(rules, kind, name_key(name), found[0] if found else None)
    require(len(out) >= 2, f"в подвале главной найдено номеров правил: {len(out)} — сменилась вёрстка?")
    return out


# ---------- страница раскрытия: вкладка «Стоимость пая и СЧА» ----------

def _selected(root, name: str) -> str | None:
    sel = root.select_one(f'select[name="{name}"]')
    if sel is None:
        return None
    opt = sel.select_one("option[selected]")
    return opt.get("value") if opt is not None else None


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first - timedelta(days=LOOKBACK_DAYS), nxt - timedelta(days=1)


def _currency(heads: list[str]) -> str:
    """В шапке единиц нет — рубли; если появятся, валюта и единицы должны быть понятными."""
    joined = " ".join(heads)
    require(not re.search(r"тыс|млн|млрд", joined), f"значения не в целых единицах: {heads}")
    found = {code for marker, code in (("руб", "RUB"), ("₽", "RUB"), ("долл", "USD"), ("$", "USD"),
                                       ("юан", "CNY"), ("евро", "EUR"), ("€", "EUR")) if marker in joined}
    require(len(found) <= 1, f"в шапке таблицы несколько валют: {heads}")
    return found.pop() if found else "RUB"


def _nav_table(root) -> tuple[list[SeriesPoint], str]:
    """div.info-table -> (ряд по возрастанию дат, валюта)."""
    table = root.select_one("div.info-table")
    require(table is not None, "нет таблицы «Стоимость пая и СЧА» (div.info-table)")
    heads = [_clean(c.get_text(" ")).lower() for c in table.select(".info-table__header .info-table__col")]
    missing = [h for h in COLUMNS if h not in heads]
    require(not missing, f"в шапке таблицы СЧА нет колонок {missing}; есть {heads}")
    currency = _currency(heads)
    points: dict[date, SeriesPoint] = {}
    for row in table.select(".info-table__body > .info-table__row"):
        cells: dict[str, str] = {}
        for col in row.select(":scope > .info-table__col"):
            label, value = col.select_one(".info-table__col-header"), col.select_one(".info-table__value")
            require(label is not None and value is not None,
                    f"ячейка без подписи или значения: {_clean(col.get_text(' '))[:80]}")
            cells[_clean(label.get_text(" ")).lower()] = _clean(value.get_text(" "))
        missing = [h for h in COLUMNS if h not in cells]
        require(not missing, f"в строке таблицы нет колонок {missing}: {_clean(row.get_text(' '))[:120]}")
        raw_date = cells["дата расчета"]
        require(bool(DATE_RE.fullmatch(raw_date)), f"в колонке «Дата расчета» не дата: {raw_date!r}")
        d = day(raw_date)
        price, nav = num(cells["стоимость пая"]), num(cells["сча"])
        if price is None and nav is None:
            continue
        for what, v in (("стоимость пая", price), ("СЧА", nav)):
            require(v is None or v > 0, f"{d}: {what} {v} — не положительное число")
        if price is not None and nav is not None:
            require(nav >= price, f"{d}: СЧА {nav} меньше стоимости пая {price} — перепутаны колонки?")
        require(d not in points, f"дата расчёта {d} в таблице дважды")
        points[d] = SeriesPoint(d, nav, price)
    return [points[d] for d in sorted(points)], currency


def _check_month(points: list[SeriesPoint], year: int, month: int, where: str) -> None:
    lo, hi = _month_bounds(year, month)
    bad = [p.date.isoformat() for p in points if not lo <= p.date <= hi]
    require(not bad, f"{where}: в таблице за {month:02d}.{year} даты из других месяцев: {bad[:5]}")


def parse_fund_page(html: str) -> MkbFundPage:
    """Страница раскрытия: заголовок, за какой месяц показана таблица, годы фильтра, ряд за этот месяц."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("div.info-section__content-title")
    require(title is not None, "на странице раскрытия нет заголовка фонда (div.info-section__content-title)")
    tab = soup.find(id=NAV_TAB)
    require(tab is not None, f"на странице нет вкладки #{NAV_TAB} «Стоимость пая и СЧА»")
    nav_tab = soup.select_one(f'.info-section__tabs-nav-item[data-tab="{NAV_TAB.split("-")[1]}"]')
    require(nav_tab is not None and "стоимость пая и сча" in _clean(nav_tab.get_text(" ")).lower(),
            f"вкладка #{NAV_TAB} — не «Стоимость пая и СЧА»")
    period, year, month = (_selected(tab, f"info-pai__{x}") for x in ("period", "year", "month"))
    require(period == "month", f"таблица по умолчанию не «За месяц», а {period!r}")
    require(bool(year and re.fullmatch(r"\d{4}", year) and month and re.fullmatch(r"\d{2}", month)),
            f"в фильтре таблицы не выбраны год и месяц: {year!r}, {month!r}")
    years = sorted({int(o["value"]) for o in tab.select('select[name="info-pai__year"] option[value]')
                    if re.fullmatch(r"\d{4}", o["value"])})
    require(int(year) in years, f"выбранного года {year} нет в фильтре: {years}")
    points, currency = _nav_table(tab)
    _check_month(points, int(year), int(month), "страница фонда")
    return MkbFundPage(title=_clean(title.get_text(" ")), month=(int(year), int(month)), years=years,
                       points=points, currency=currency)


def parse_nav_month(html: str, year: int, month: int) -> list[SeriesPoint]:
    """Ответ на POST period=month&tabId=tab-1&year=&month= — html вкладки (или вся страница) за этот месяц."""
    soup = BeautifulSoup(html, "lxml")
    tab = soup.find(id=NAV_TAB) or soup
    got = tuple(_selected(tab, f"info-pai__{x}") for x in ("period", "year", "month"))
    if any(v is not None for v in got):
        # сайт сам пишет в фильтре, что показал; если не то, что просили, — параметры не сработали
        require(got == ("month", str(year), f"{month:02d}"),
                f"просили «За месяц» {month:02d}.{year}, а в ответе выбрано {got}")
    points, _ = _nav_table(tab)
    _check_month(points, year, month, "ответ за месяц")
    return points


def months_between(start: date, end: tuple[int, int]) -> list[tuple[int, int]]:
    """Месяцы от месяца даты start включительно до end=(год, месяц) не включительно."""
    y, m = start.year, start.month
    out = []
    while (y, m) < end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------- адаптер ----------

class MkbAdapter(Adapter):
    key = "mkb"
    title = "УК МКБ Инвестиции"
    uk_inns = ("7710186602",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        state: dict = {}

        def site() -> dict[str, MkbSiteFund]:
            # главную качаем один раз; её ошибка становится статусом каждого фонда через guarded()
            if "data" not in state:
                try:
                    doc = ctx.http.get(HOME_URL, fmt="html", funds=[f.rules for f in funds])
                    state["data"] = parse_home(doc.text())
                except Exception as e:  # noqa: BLE001 — запомнить и отдать каждому фонду
                    state["data"] = e
            if isinstance(state["data"], Exception):
                raise state["data"]
            return state["data"]

        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, site())) for f in funds]

    def _fund(self, ctx: Context, fund: Fund, site: dict[str, MkbSiteFund]) -> FundData:
        ref = site.get(norm_rules(fund.rules))
        if ref is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"в подвале {HOME_URL} нет фонда с правилами № {fund.rules}")
        if ref.page_id is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"фонд «{ref.name}» (правила № {fund.rules}) есть в подвале {HOME_URL}, "
                                   f"но не в списке фондов — нет адреса страницы раскрытия")
        if ref.kind != fund.type:
            return FundData(fund.rules, status="not_found",
                            reason=f"правила № {fund.rules} на сайте у фонда другого типа ({ref.kind}): «{ref.name}»")
        url = PAGE_URL.format(id=ref.page_id)
        doc = ctx.http.get(url, fmt="html", funds=[fund.rules])
        page = parse_fund_page(doc.text())
        if name_key(page.title) != ref.name:
            return FundData(fund.rules, status="not_found", page_url=url,
                            reason=f"на странице {url} другой фонд: «{page.title}»")

        fd = FundData(fund.rules, currency=page.currency, page_url=url)
        if name_key(fund.name) != ref.name:
            fd.warnings.append(f"название на сайте «{ref.name}» отличается от реестра «{fund.name}»; "
                               f"сопоставлено по номеру правил")
        seg = self.segment(doc, page.points, page_url=url, since=ctx.since)
        if seg.points:
            fd.segments.append(seg)
        # Прошлые месяцы — тем же POST, что шлёт скрипт страницы; с докачкой хвоста от ctx.start_for().
        # Сбой любого месяца — сбой фонда целиком: иначе последняя точка (текущий месяц) сдвинула бы
        # начало следующей докачки, и пропущенные месяцы больше никто не запросил бы.
        for year, month in months_between(ctx.start_for(fund.rules), page.month):
            if year not in page.years:
                continue                      # фонда в том году ещё не было
            mdoc = ctx.http.get(url, method="POST", fmt="html", funds=[fund.rules], headers=AJAX_HEADERS,
                                data={"period": "month", "tabId": NAV_TAB, "year": str(year),
                                      "month": f"{month:02d}"})
            mseg = self.segment(mdoc, parse_nav_month(mdoc.text(), year, month), page_url=url, since=ctx.since)
            if mseg.points:
                fd.segments.append(mseg)

        fd.warnings.append("официальную доходность за 1/3/6/12 мес. УК на странице фонда не публикует "
                           "(нет и на других вкладках раскрытия)")
        points = [p for s in fd.segments for p in s.points]
        if not points:
            empty = f"в таблице «Стоимость пая и СЧА» на {url} нет точек с {ctx.start_for(fund.rules)}"
            if fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", f"фонд формируется: {empty}"
            elif fund.rules in ctx.last_dates:
                fd.status, fd.reason = "unavailable", f"{empty}, хотя раньше были"
            else:
                fd.status, fd.reason = "no_history", empty
        elif all(p.nav is None for p in points):
            fd.status, fd.reason = "partial", "УК публикует только стоимость пая, без СЧА"
        elif all(p.price is None for p in points):
            fd.status, fd.reason = "partial", "УК публикует только СЧА, без стоимости пая"
        return fd
