"""АО УК «Доверительная» (бывшая «Система Капитал»), entrustment.ru: история СЧА/пая и официальная доходность.

Источники (сайт УК):
- https://entrustment.ru/catalog/ — каталог фондов: таблица «Динамика фондов» (короткое название →
  страница фонда) и подвал с юридическими дисклеймерами, где у каждого фонда указаны полное
  название, короткое название и «Регистрационный номер» (номер правил) — по нему и сопоставляем;
- https://entrustment.ru/catalog/<slug>/?AJAX=Y&period=<окно>&type=index_inv|scha&valute=rub&format=sistema
  (заголовок X-Requested-With) — HTML-фрагмент, которым график на странице фонда подгружает окно:
  в атрибуте data-array-price блока #chart-data лежит JSON [[epoch_ms, значение], ...];
  type=index_inv — расчётная стоимость пая, type=scha — СЧА. Частота — каждый рабочий день.
  Окна фиксированные (month/3month/6month/year/3year/all) и отсчитываются от последней даты ряда,
  произвольного диапазона дат нет;
- https://entrustment.ru/disclosure_old/<slug>/ — раскрытие информации по фонду: блок
  «Расчет прироста РСП % на ДД.ММ.ГГГГ» (за 1/3/6 мес., 1 год, 3 года, 5 лет) — официальная
  доходность; блок «Стоимость пая и СЧА на ДД.ММ.ГГГГ» — для сверки с рядом. Страницы раскрытия
  находим по меню раздела https://entrustment.ru/disclosure_old/otchetnost/ (полные названия фондов).

Подводные камни:
- epoch_ms в графике — полночь по Москве (UTC+3); через UTC дата съезжает на день назад,
  поэтому здесь свой перевод в дату (parsing.day() считает в UTC).
- «Валютные накопления» и «Резервный. Валютный»: страница фонда по умолчанию показывает
  долларовый эквивалент (СЧА в рублях / курс ЦБ). Официально пай и СЧА раскрываются в рублях
  («Стоимость пая (руб.)» на странице раскрытия, рублёвая выгрузка истории), официальная
  доходность — тоже по рублёвой цене. Поэтому все ряды берём с valute=rub, валюта пая — RUB.
- Полная история есть ещё в PDF «Скачать таблицу» (disclosure_old/print_table.php?element=<slug>),
  но HTML-фрагмент графика даёт те же значения без PDF-разбора.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://entrustment.ru"
CATALOG_URL = f"{BASE}/catalog/"
DISCLOSURE_INDEX_URL = f"{BASE}/disclosure_old/otchetnost/"
XHR = {"X-Requested-With": "XMLHttpRequest"}
MSK = timezone(timedelta(hours=3))

# Окна графика: значение параметра period -> глубина в днях от последней даты ряда (data-days в ответе).
WINDOWS = (("year", 366), ("3year", 1095), ("all", None))
# что запрашиваем -> (type в запросе, data-type в ответе)
KINDS = {"price": "index_inv", "nav": "scha"}
RETURN_PERIODS = {"за 1 мес.": "1m", "за 3 мес.": "3m", "за 6 мес.": "6m", "за 1 год": "1y"}


@dataclass
class SiteFund:
    rules: str          # регистрационный номер из дисклеймера
    full_name: str      # «Доверительная - фонд Резервный»
    short_name: str     # «Резервный» — так фонд назван в каталоге
    slug: str | None    # /catalog/<slug>/ или None, если в таблице каталога фонда нет


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("\xa0", " ")).strip()


def _norm_rules(s: str) -> str:
    return re.sub(r"[\s№]", "", str(s or ""))


def _msk_day(ms) -> date:
    """epoch_ms графика -> дата. Метка — полночь по Москве; иное время = незнакомый формат."""
    require(isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 10**11,
            f"ожидалась метка времени в мс, получено {ms!r}")
    dt = datetime.fromtimestamp(ms / 1000, MSK)
    require((dt.hour, dt.minute, dt.second) == (0, 0, 0),
            f"метка {ms} — не полночь по Москве ({dt.isoformat()}), дата неоднозначна")
    return dt.date()


# ---------------------------------------------------------------- разбор

_DISCLAIMER = re.compile(
    r'"(?P<full>[^"]+)"\s*\(в настоящем материале\s*[–—-]\s*«(?P<short>[^»]+)»\)\.?\s*'
    r"Регистрационный номер\s*[–—-]\s*(?P<rules>\d[\d\s-]*\d)")


def parse_catalog(content) -> dict[str, SiteFund]:
    """Каталог фондов: номер правил -> SiteFund. Номер и названия — из дисклеймеров подвала,
    страница фонда — из таблицы «Динамика фондов» (по короткому названию)."""
    soup = BeautifulSoup(content, "lxml")
    rows = soup.select("table.js-mutual-funds-table tbody tr")
    require(bool(rows), "каталог: нет таблицы фондов (table.js-mutual-funds-table)")
    slugs: dict[str, str] = {}
    for tr in rows:
        a = tr.select_one("td.text-left a[href]")
        require(a is not None, "каталог: в строке таблицы нет ссылки на фонд")
        m = re.fullmatch(r"/catalog/([\w-]+)/", a["href"])
        require(m is not None, f"каталог: незнакомая ссылка на фонд {a['href']!r}")
        name = _clean(a.get_text(" "))
        require(bool(name), "каталог: пустое название фонда в таблице")
        slugs[norm_text(name)] = m.group(1)
    out: dict[str, SiteFund] = {}
    for li in soup.find_all("li"):
        text = _clean(li.get_text(" "))
        if "Регистрационный номер" not in text:
            continue
        m = _DISCLAIMER.search(text)
        require(m is not None, f"каталог: незнакомый дисклеймер фонда: {text[:150]!r}")
        rules = _norm_rules(m.group("rules"))
        short = _clean(m.group("short"))
        out[rules] = SiteFund(rules, _clean(m.group("full")), short, slugs.get(norm_text(short)))
    require(bool(out), "каталог: нет дисклеймеров с регистрационными номерами фондов")
    return out


def parse_disclosure_index(content) -> dict[str, str]:
    """Меню раздела «Раскрытие информации»: norm_text(полное название фонда) -> URL страницы раскрытия."""
    soup = BeautifulSoup(content, "lxml")
    links = soup.select(".second-menu a[href^='/disclosure_old/']")
    require(bool(links), "раскрытие: нет меню раздела (.second-menu)")
    out = {}
    for a in links:
        m = re.search(r"«(.+)»", _clean(a.get_text(" ")))
        if m:   # пункты без «…» — не фонды (отчётность, СРО и т. п.)
            out[norm_text(m.group(1))] = BASE + a["href"]
    require(bool(out), "раскрытие: в меню нет ни одного фонда")
    return out


def parse_history(content, kind: str) -> list[SeriesPoint]:
    """Фрагмент графика страницы фонда -> точки. kind: 'price' (стоимость пая) или 'nav' (СЧА), в рублях."""
    require(kind in KINDS, f"неизвестный вид ряда {kind!r}")
    soup = BeautifulSoup(content, "lxml")
    box = soup.select_one("#chart-data")
    require(box is not None, "график: нет блока #chart-data")
    require(box.get("data-type") == KINDS[kind],
            f"график: data-type={box.get('data-type')!r}, ожидался {KINDS[kind]!r}")
    require(box.get("data-currency") == "rub",
            f"график: data-currency={box.get('data-currency')!r}, ожидались рубли")
    raw = box.get("data-array-price")
    require(raw is not None, "график: нет атрибута data-array-price")
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"график: data-array-price — не JSON ({e})") from e
    require(isinstance(arr, list) and len(arr) > 0, "график: пустой или не список data-array-price")
    points: dict[date, SeriesPoint] = {}
    for item in arr:
        require(isinstance(item, list) and len(item) == 2, f"график: незнакомая точка {item!r}")
        d, value = _msk_day(item[0]), num(item[1])
        require(d not in points, f"график: дата {d} повторяется")
        points[d] = SeriesPoint(d, value, None) if kind == "nav" else SeriesPoint(d, None, value)
    return [points[d] for d in sorted(points)]


def _block_table(soup: BeautifulSoup, title_re: str):
    """Блок страницы раскрытия «<заголовок> на ДД.ММ.ГГГГ» -> (дата, таблица) или None."""
    for b in soup.select(".center-text b"):
        m = re.fullmatch(title_re + r"\s+на\s+(\d{2}\.\d{2}\.\d{4})", _clean(b.get_text(" ")))
        if m:
            table = b.find_next("table")
            require(table is not None, f"раскрытие: у блока «{_clean(b.get_text(' '))}» нет таблицы")
            return day(m.group(1)), table
    return None


def parse_returns(content, source_url: str) -> list[OfficialReturn] | None:
    """Страница раскрытия фонда -> официальная доходность пая (блок «Расчет прироста РСП %»).

    None — блока сейчас нет, хотя страница узнаётся (так бывает, пока УК выкладывает данные дня).
    Страница без обоих известных блоков или незнакомая таблица — UnknownFormat."""
    soup = BeautifulSoup(content, "lxml")
    found = _block_table(soup, r"Расчет прироста РСП %")
    if found is None:
        require(_block_table(soup, r"Стоимость пая и СЧА") is not None,
                "раскрытие: нет ни блока «Расчет прироста РСП % на …», ни «Стоимость пая и СЧА на …»")
        return None
    as_of, table = found
    rows = table.select("tr")
    require(len(rows) >= 2, "раскрытие: в таблице прироста РСП нет строки значений")
    heads = [_clean(c.get_text(" ")) for c in rows[0].select("th,td")]
    values = [_clean(c.get_text(" ")) for c in rows[1].select("th,td")]
    require(len(heads) == len(values), f"раскрытие: заголовков {len(heads)}, значений {len(values)}")
    missing = [h for h in RETURN_PERIODS if h not in heads]
    require(not missing, f"раскрытие: в таблице прироста РСП нет колонок {missing}; есть {heads}")
    out = []
    for head, value in zip(heads, values):
        period = RETURN_PERIODS.get(head)
        v = pct(value)
        if period and v is not None:   # «—» — УК не публикует (фонд моложе периода)
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def parse_latest(content) -> tuple[date, float | None, float | None]:
    """Страница раскрытия -> последняя опубликованная (дата, цена пая, СЧА) в рублях — для сверки ряда."""
    soup = BeautifulSoup(content, "lxml")
    found = _block_table(soup, r"Стоимость пая и СЧА")
    require(found is not None, "раскрытие: нет блока «Стоимость пая и СЧА на …»")
    d, table = found
    rows = table.select("tr")
    require(len(rows) >= 2, "раскрытие: в таблице стоимости пая нет строки значений")
    heads = [_clean(c.get_text(" ")) for c in rows[0].select("th,td")]
    values = [_clean(c.get_text(" ")) for c in rows[1].select("th,td")]
    require(heads == ["Стоимость пая (руб.)", "Стоимость чистых активов (руб.)"] and len(values) == 2,
            f"раскрытие: незнакомая таблица стоимости пая {heads}")
    return d, num(values[0]), num(values[1])


def pick_window(since: date, today: date) -> str:
    """Наименьшее окно графика, которое покрывает since (с запасом на праздники).

    Зависит только от since, поэтому URL стабилен и повторный запуск берёт ответ из кэша."""
    for period, days in WINDOWS:
        if days is None or today - timedelta(days=days) <= since - timedelta(days=7):
            return period
    return "all"


# ---------------------------------------------------------------- адаптер

class EntrustmentAdapter(Adapter):
    key = "entrustment"
    title = "АО УК «Доверительная»"
    uk_inns = ("9725182361",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # общие документы (каталог, меню раскрытия) относим в манифесте ко всем фондам УК
        memo: dict[str, object] = {"rules": [f.rules for f in funds]}
        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, memo)) for f in funds]

    @staticmethod
    def _once(memo: dict, key: str, fn):
        """Общий документ качаем один раз; сбой запоминаем и отдаём каждому фонду как его статус."""
        if key not in memo:
            try:
                memo[key] = fn()
            except Exception as e:  # noqa: BLE001 — перебросим ниже, в guarded() конкретного фонда
                memo[key] = e
        if isinstance(memo[key], Exception):
            raise memo[key]
        return memo[key]

    def _catalog(self, ctx: Context, memo: dict) -> dict[str, SiteFund]:
        return self._once(memo, "catalog", lambda: parse_catalog(
            ctx.http.get(CATALOG_URL, fmt="html", funds=memo["rules"]).content()))

    def _disclosure(self, ctx: Context, memo: dict) -> dict[str, str]:
        return self._once(memo, "disclosure", lambda: parse_disclosure_index(
            ctx.http.get(DISCLOSURE_INDEX_URL, fmt="html", funds=memo["rules"]).content()))

    @staticmethod
    def match(fund: Fund, catalog: dict[str, SiteFund]) -> SiteFund | None:
        """По номеру правил из дисклеймера; если номера нет — по точному полному названию."""
        site = catalog.get(_norm_rules(fund.rules))
        if site is None:
            by_name = [s for s in catalog.values() if norm_text(s.full_name) == norm_text(fund.name)]
            site = by_name[0] if len(by_name) == 1 else None
        return site

    def _one(self, ctx: Context, fund: Fund, memo: dict) -> FundData:
        rules = [fund.rules]
        site = self.match(fund, self._catalog(ctx, memo))
        if site is None:
            return FundData(fund.rules, status="not_found",
                            reason="в каталоге entrustment.ru нет фонда с таким номером правил или названием")
        if site.slug is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"фонд «{site.short_name}» есть в дисклеймерах УК, но нет в каталоге фондов")
        page_url = f"{BASE}/catalog/{site.slug}/"
        window = pick_window(ctx.since, date.today())
        fd = FundData(fund.rules, currency="RUB", page_url=page_url)
        series = {}
        for kind, type_ in KINDS.items():
            params = {"AJAX": "Y", "period": window, "type": type_, "valute": "rub", "format": "sistema"}
            raw = ctx.http.get(page_url, params=params, fmt="html", funds=rules, headers=XHR)
            series[kind] = parse_history(raw.content(), kind)
            fd.segments.append(self.segment(raw, series[kind], page_url=page_url, since=ctx.since))

        self._official(ctx, memo, fund, site, fd, series)

        prices = [p for p in fd.segments[0].points if p.price is not None]
        navs = [p for p in fd.segments[1].points if p.nav is not None]
        if not prices and not navs:
            fd.status, fd.reason = "no_history", "в графике на странице фонда нет точек с начала истории"
        elif not prices or not navs:
            fd.status = "partial"
            fd.reason = f"на странице фонда есть только {'СЧА' if navs else 'стоимость пая'}"
        else:
            first = min(prices[0].date, navs[0].date)
            formed = date.fromisoformat(fund.formation_end) if fund.formation_end else None
            if (formed is None or formed < ctx.since) and first > ctx.since + timedelta(days=10):
                fd.status = "partial"
                fd.reason = f"сайт отдал историю только с {first.isoformat()}"
        return fd

    def _official(self, ctx: Context, memo: dict, fund: Fund, site: SiteFund, fd: FundData,
                  series: dict[str, list[SeriesPoint]]) -> None:
        """Официальная доходность и сверка последней точки со страницей раскрытия.

        Сбой здесь не отменяет собранный ряд: сеть — предупреждение, незнакомый формат — ошибка разбора."""
        try:
            index = self._disclosure(ctx, memo)
            url = index.get(norm_text(site.full_name)) or index.get(norm_text(fund.name))
            if url is None:
                fd.warnings.append("страница раскрытия информации фонда не найдена — без официальной доходности")
                return
            content = ctx.http.get(url, fmt="html", funds=[fund.rules]).content()
            returns = parse_returns(content, url)
            d, price, nav = parse_latest(content)
        except FetchError as e:
            fd.warnings.append(f"раскрытие информации недоступно ({e.reason}) — без официальной доходности")
            return
        except UnknownFormat as e:
            ctx.log.error("[%s] %s: раскрытие: %s", fund.uk_short, fund.rules, e)
            fd.errors.append(f"разбор раскрытия информации: {e}")
            return
        if returns is None:
            fd.warnings.append("на странице раскрытия сейчас нет блока «Расчет прироста РСП %» "
                               "— без официальной доходности")
        else:
            fd.official_returns = returns
        # сверка: последняя опубликованная в раскрытии точка должна совпасть с графиком
        chart = {kind: {p.date: (p.price if kind == "price" else p.nav) for p in pts} for kind, pts in series.items()}
        for kind, official in (("price", price), ("nav", nav)):
            ours = chart[kind].get(d)
            if ours is not None and official is not None and abs(ours - official) > 1e-6 * max(1.0, abs(official)):
                fd.warnings.append(f"на {d.isoformat()} {'цена пая' if kind == 'price' else 'СЧА'} в графике "
                                   f"{ours} ≠ {official} на странице раскрытия")
