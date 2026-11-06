"""ООО «РСХБ Управление Активами» (www.rshb-am.ru) — 11 ОПИФ и 3 БПИФ.

Где что лежит (разведка 04.11.2026; из части сетей сайт не отвечает, разведка шла из GitHub Actions):
* каталог /paevye-investicionnye-fondy-pif/ — меню со ссылками на страницы всех фондов:
  ОПИФ /paevye-investicionnye-fondy-pif/opif-<slug>/, БПИФ /birzhevye-paevye-investicionnye-fondy-bpif/bpif-<slug>/;
  текст ссылки — название («Фонд Облигаций», «РСХБ – Облигации, вектор И.Эс.Джи»);
* страница фонда:
  - <h1>: «ОПИФ «Фонд Облигаций»», «БПИФ «РСХБ – Корпоративные облигации смарт бета (RSHU)»»;
  - графики: div#graph-pif data-json="/ajax/data.php?fund=<id>&type=&period=begin&date_from=&date_to=<дата>" —
    стоимость пая, div#graph-sha data-json="/ajax/data-sha.php?…" — СЧА. Ответ — [[epoch-ms, значение], …]
    по рабочим дням за всю историю; date_from=ГГГГ-ММ-ДД сервер учитывает (проверено на обоих адресах).
    date_to на странице — дата последней точки; пустой date_to= (как date_from= на самой странице) сервер
    понимает как «до конца ряда»: ответы data.php, data-sha.php и datapotok.php с date_to=, без date_to и
    с date_to=сегодня совпали байт в байт (проверено 06.11.2026 из GitHub Actions);
  - шапка дневной таблицы: «Расчетная стоимость инвестиционного пая (РСИП), рубли», «Стоимость чистых
    активов (СЧА), рубли» — отсюда валюта пая и единицы СЧА (рубли, не тысячи);
  - таблица «Динамика стоимости пая» (table.graph__pro-table): День / Месяц / 3 месяца / 6 месяцев /
    1 год / …, в подсказке заголовка — границы периодов («Месяц - c 30.09.2026 по 31.10.2026»). Это и есть
    опубликованная УК доходность (от конца месяца до конца месяца); у «Денежного потока» — с учётом выплат;
  - у «Денежного потока» (выплата дохода раз в квартал) PRO-график грузит /ajax/datapotok.php?fund=<id>&…:
    {"result": …, "payouts": [{"x": epoch-ms, "amount": выплата на пай, ₽}], "periodYields": …}.

Ловушки:
* Номер правил на странице фонда не выделить: в подвале каждой страницы перечислены правила всех фондов
  УК, в документах — номера изменений и дополнений. Сопоставляем по названию: у ОПИФ на сайте оно без
  «РСХБ –» («Фонд Облигаций»), у БПИФ в <h1> бывает тикер в скобках — сверяем его с тикером реестра.
* Метки времени — полночь по Москве; там, где Москва жила в UTC+4 (летом до 2011 г. и в 2011–2014 гг.),
  сервер пишет полночь UTC+3, и в зоне Europe/Moscow это 01:00 того же дня. Дату берём по Москве
  (не по UTC — иначе всё съезжает на день назад), 00:00 и 01:00 допускаем, остальное — ошибка формата.
* "result" в datapotok.php — ряд для PRO-графика, на границах периодов значения подменены (на 12 датах
  не совпадает с data.php), поэтому цену берём только из data.php, а из datapotok — только выплаты.
* Дата выплаты в payouts — день, когда стоимость пая уже уменьшена на выплату (30.09.2026: 818,80 -> 776,02 ₽
  при выплате 42,27 ₽); с такими датами формула returns.py воспроизводит все четыре официальные цифры
  «Денежного потока» на 31.10.2026 (2,91 / 2,51 / 1,86 / 8,98 %).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, Payout, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

try:  # epoch-ms — московская полночь с историческим летним временем
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — нет базы часовых поясов: с 2014 г. Москва всегда UTC+3
    MSK = timezone(timedelta(hours=3))

BASE = "https://www.rshb-am.ru"
CATALOG_URL = f"{BASE}/paevye-investicionnye-fondy-pif/"
PRICE_PATH, NAV_PATH, PAYOUTS_PATH = "/ajax/data.php", "/ajax/data-sha.php", "/ajax/datapotok.php"
PRICE_API, NAV_API, PAYOUTS_API = (BASE + p for p in (PRICE_PATH, NAV_PATH, PAYOUTS_PATH))

_FUND_PATH = re.compile(r"(?:https?://www\.rshb-am\.ru)?"
                        r"(/(?:paevye-investicionnye-fondy-pif/opif|birzhevye-paevye-investicionnye-fondy-bpif/bpif)"
                        r"-[a-z0-9-]+/)")
# Колонки таблицы «Динамика стоимости пая» -> период OfficialReturn.
RETURN_COLUMNS = {"месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}
# Строка подсказки: «Месяц - c 30.09.2026 по 31.10.2026» (буква «с» то латинская, то русская).
_PERIOD_LINE = re.compile(r"([А-Яа-яЁё0-9 ]+?)\s*-\s*[сc]\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})")
# Тикер БПИФ в конце названия: «РСХБ – Корпоративные облигации смарт бета (RSHU)».
_TICKER = re.compile(r"\s*\(([A-Z]{3,6})\)(?=\s*»?\s*$)")


@dataclass
class RshbFundPage:
    title: str                        # название из <h1> без тикера
    ticker: str | None                # тикер из <h1>, если УК его показывает
    fund_id: str                      # внутренний id фонда в /ajax/*.php
    price_params: dict[str, str]      # параметры data-json графика стоимости пая — как на странице
    nav_params: dict[str, str]        # то же для графика СЧА
    currency: str
    returns: list[tuple[str, float | None, date]] = field(default_factory=list)   # (период, %, по какую дату)
    payouts_params: dict[str, str] | None = None   # есть только у фондов с выплатой дохода


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def name_key(name: str) -> str:
    """Ключ сопоставления: «РСХБ – Фонд Облигаций», «ОПИФ «Фонд Облигаций»», «Фонд Облигаций» -> «облигаций»."""
    return re.sub(r"^рсхб\s+", "", norm_text(name))


def currency_from_label(label: str) -> str:
    """'…(РСИП), рубли' -> 'RUB'. Незнакомая валюта — UnknownFormat, а не тихий рубль."""
    s = label.lower()
    for marker, code in (("руб", "RUB"), ("₽", "RUB"), ("долл", "USD"), ("usd", "USD"), ("$", "USD"),
                         ("юан", "CNY"), ("cny", "CNY"), ("евро", "EUR"), ("eur", "EUR"), ("€", "EUR")):
        if marker in s:
            return code
    raise UnknownFormat(f"не понял валюту из подписи {label!r}")


# ---------- каталог и страница фонда ----------

def parse_catalog(html: str) -> dict[str, set[str]]:
    """Каталог: адрес страницы фонда -> тексты всех ссылок на неё (название — одна из них)."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, set[str]] = {}
    for a in soup.find_all("a", href=True):
        m = _FUND_PATH.fullmatch(a["href"].strip())
        if not m:
            continue
        texts = out.setdefault(urljoin(BASE, m.group(1)), set())
        text = _clean(a.get_text(" "))
        if text:
            texts.add(text)
    require(len(out) >= 5, f"в каталоге найдено только {len(out)} ссылок на фонды — сменилась вёрстка?")
    return out


def _graph_params(soup: BeautifulSoup, element_id: str, path: str) -> dict[str, str]:
    el = soup.find(id=element_id)
    require(el is not None and bool(el.get("data-json")), f"на странице нет графика #{element_id} с data-json")
    src = el["data-json"]
    u = urlsplit(src)
    require(u.path == path, f"#{element_id}: ожидался {path}, а в data-json {src!r}")
    params = dict(parse_qsl(u.query, keep_blank_values=True))
    require(bool(re.fullmatch(r"\d+", params.get("fund", ""))), f"#{element_id}: нет числового fund в {src!r}")
    return params


def _currency(soup: BeautifulSoup) -> str:
    """Валюта пая и единицы СЧА — из шапки дневной таблицы «Дата / РСИП / … / СЧА»."""
    heads = [_clean(th.get_text(" ")) for th in soup.select("table.docs__scroll-table thead th")]
    price = [h for h in heads if h.lower().startswith("расчетная стоимость инвестиционного пая")]
    nav = [h for h in heads if h.lower().startswith("стоимость чистых активов")]
    require(len(price) >= 1 and len(nav) >= 1, f"в шапке таблицы нет колонок РСИП и СЧА: {heads[:8]}")
    require(not re.search(r"тыс|млн|млрд", nav[0].lower()), f"СЧА не в целых единицах: {nav[0]!r}")
    cur = currency_from_label(price[0].rsplit(",", 1)[-1])
    require(currency_from_label(nav[0].rsplit(",", 1)[-1]) == cur,
            f"валюта пая и СЧА различаются: {price[0]!r} / {nav[0]!r}")
    return cur


def _returns(soup: BeautifulSoup) -> list[tuple[str, float | None, date]]:
    """Таблица «Динамика стоимости пая» + границы периодов из подсказки -> [(период, %, дата конца)]."""
    head = next((h for h in soup.find_all(["h2", "h3", "h4"]) if "Динамика стоимости пая" in h.get_text(" ")), None)
    require(head is not None, "нет блока «Динамика стоимости пая» с доходностью")
    info = head.select_one(".info__text")
    require(info is not None, "у «Динамики стоимости пая» нет подсказки с периодами расчёта")
    spans = {_clean(label).lower(): (day(d1), day(d2)) for label, d1, d2 in _PERIOD_LINE.findall(info.get_text(" "))}
    table = head.find_next("table", class_="graph__pro-table")
    require(table is not None, "нет таблицы доходности (table.graph__pro-table)")
    rows = table.find_all("tr")
    require(len(rows) == 2, f"в таблице доходности ожидались 2 строки (периоды и значения), их {len(rows)}")
    heads = [_clean(c.get_text(" ")).lower() for c in rows[0].find_all(["th", "td"])]
    cells = [_clean(c.get_text(" ")) for c in rows[1].find_all(["th", "td"])]
    require(len(heads) == len(cells), f"в таблице доходности {len(heads)} заголовков и {len(cells)} значений")
    out = []
    for label, period in RETURN_COLUMNS.items():
        require(label in heads, f"в таблице доходности нет колонки «{label}»; есть {heads}")
        require(label in spans, f"в подсказке нет границ периода «{label}»; есть {sorted(spans)}")
        start, end = spans[label]
        require(start < end, f"период «{label}»: начало {start} не раньше конца {end}")
        out.append((period, pct(cells[heads.index(label)].replace("−", "-")), end))
    return out


def parse_fund_page(html: str) -> RshbFundPage:
    """Страница фонда: название, id графиков, валюта, официальная доходность, источник выплат."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    require(h1 is not None, "на странице фонда нет <h1> с названием")
    title = _clean(h1.get_text(" "))
    m = _TICKER.search(title)
    ticker = m.group(1) if m else None
    if m:
        title = _clean(title[:m.start()] + title[m.end():])

    price_params = _graph_params(soup, "graph-pif", PRICE_PATH)
    nav_params = _graph_params(soup, "graph-sha", NAV_PATH)
    require(price_params["fund"] == nav_params["fund"],
            f"графики пая и СЧА про разные фонды: {price_params['fund']} и {nav_params['fund']}")

    payouts_params = None
    pro = soup.find(id="graph-pif-pro")
    if pro is not None and urlsplit(pro.get("data-json", "")).path == PAYOUTS_PATH:
        payouts_params = _graph_params(soup, "graph-pif-pro", PAYOUTS_PATH)
        require(payouts_params["fund"] == price_params["fund"],
                f"выплаты про другой фонд: {payouts_params['fund']} вместо {price_params['fund']}")
    return RshbFundPage(title=title, ticker=ticker, fund_id=price_params["fund"], price_params=price_params,
                        nav_params=nav_params, currency=_currency(soup), returns=_returns(soup),
                        payouts_params=payouts_params)


def parse_returns_rshb(html: str, source_url: str) -> list[OfficialReturn]:
    """Официальная доходность за 1/3/6/12 мес. со страницы фонда (пустые «-» пропускаем)."""
    return [OfficialReturn(period, value, as_of, source_url)
            for period, value, as_of in parse_fund_page(html).returns if value is not None]


# ---------- ряды и выплаты (/ajax/*.php) ----------

def _json(content, what: str):
    if isinstance(content, (bytes, str)):
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise UnknownFormat(f"{what}: ответ не JSON: {e}") from e
    return content


def _msk_day(ts) -> date:
    require(isinstance(ts, int) and not isinstance(ts, bool) and ts > 10**11, f"метка времени не epoch-ms: {ts!r}")
    dt = datetime.fromtimestamp(ts / 1000, MSK)
    require(dt.hour in (0, 1) and dt.minute == dt.second == 0,
            f"метка {ts} — не московская полночь ({dt:%Y-%m-%d %H:%M})")
    return dt.date()


def _series(content, what: str) -> list[tuple[date, float]]:
    """[[epoch-ms, значение], …] -> [(дата, значение)] по возрастанию дат; 0 и null — нет значения."""
    data = _json(content, what)
    require(isinstance(data, list), f"{what}: ожидался список точек, получено {type(data).__name__}")
    out: dict[date, float] = {}
    for item in data:
        require(isinstance(item, list) and len(item) == 2, f"{what}: точка не пара [время, значение]: {item!r}")
        d, v = _msk_day(item[0]), num(item[1])
        if v is None or v == 0:
            continue
        require(v > 0, f"{what}: отрицательное значение {v} на {d}")
        require(out.get(d, v) == v, f"{what}: на {d} два разных значения: {out.get(d)} и {v}")
        out[d] = v
    return sorted(out.items())


def parse_prices(content) -> list[SeriesPoint]:
    """/ajax/data.php — расчётная стоимость пая."""
    return [SeriesPoint(d, None, v) for d, v in _series(content, "стоимость пая")]


def parse_navs(content) -> list[SeriesPoint]:
    """/ajax/data-sha.php — СЧА в рублях."""
    return [SeriesPoint(d, v, None) for d, v in _series(content, "СЧА")]


def parse_payouts(content, source_url: str) -> list[Payout]:
    """/ajax/datapotok.php -> выплаты дохода на пай по возрастанию дат."""
    data = _json(content, "выплаты")
    require_keys(data, ["payouts"], "datapotok.php")
    require(isinstance(data["payouts"], list), f"payouts: ожидался список, получено {type(data['payouts']).__name__}")
    out: dict[date, Payout] = {}
    for item in data["payouts"]:
        require_keys(item, ["x", "amount"], "выплата")
        d, amount = _msk_day(item["x"]), num(item["amount"])
        require(amount is not None and amount > 0, f"выплата на {d}: сумма {item['amount']!r}")
        require(d not in out, f"выплата на {d} повторяется")
        out[d] = Payout(d, amount, source_url)
    return [out[d] for d in sorted(out)]


# ---------- сопоставление реестра с сайтом ----------

def match_funds(funds: list[Fund], catalog: dict[str, set[str]]) -> dict[str, tuple[str | None, str]]:
    """Номер правил -> (адрес страницы фонда | None, причина, если не нашли). Только однозначные пары."""
    keys = {url: {name_key(t) for t in texts} - {""} for url, texts in catalog.items()}
    found = {f.rules: sorted(u for u, ks in keys.items() if name_key(f.name) in ks) for f in funds}
    claimed: dict[str, list[str]] = {}
    for rules, urls in found.items():
        for u in urls:
            claimed.setdefault(u, []).append(rules)
    out: dict[str, tuple[str | None, str]] = {}
    for f in funds:
        urls = found[f.rules]
        if not urls:
            out[f.rules] = (None, f"в каталоге {CATALOG_URL} нет фонда с названием «{f.name}»")
        elif len(urls) > 1:
            out[f.rules] = (None, f"название «{f.name}» подходит к нескольким страницам: {urls}")
        elif len(claimed[urls[0]]) > 1:
            out[f.rules] = (None, f"страница {urls[0]} подходит к нескольким фондам реестра: {claimed[urls[0]]}")
        else:
            out[f.rules] = (urls[0], "")
    return out


# ---------- адаптер ----------

class RshbAdapter(Adapter):
    key = "rshb"
    title = "РСХБ Управление Активами"
    uk_inns = ("7704814182",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        state: dict = {}

        def matches() -> dict[str, tuple[str | None, str]]:
            # каталог качаем один раз; его ошибка превращается в статус каждого фонда через guarded()
            if "data" not in state:
                try:
                    doc = ctx.http.get(CATALOG_URL, fmt="html", funds=[f.rules for f in funds])
                    state["data"] = match_funds(funds, parse_catalog(doc.text()))
                except Exception as e:  # noqa: BLE001 — запомнить и отдать каждому фонду
                    state["data"] = e
            if isinstance(state["data"], Exception):
                raise state["data"]
            return state["data"]

        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, *matches()[f.rules])) for f in funds]

    def _fund(self, ctx: Context, fund: Fund, page_url: str | None, why: str) -> FundData:
        if page_url is None:
            return FundData(fund.rules, status="not_found", reason=why)
        html = ctx.http.get(page_url, fmt="html", funds=[fund.rules]).text()
        page = parse_fund_page(html)
        if name_key(page.title) != name_key(fund.name):
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"на странице {page_url} другой фонд: «{page.title}»")
        if page.ticker and fund.ticker and page.ticker != fund.ticker:
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"на странице {page_url} тикер {page.ticker}, в реестре {fund.ticker}")

        # Как на странице, но с докачкой хвоста (date_from) и открытым концом (пустой date_to): с датой
        # «сегодня» адрес менялся бы каждый день и повторный сбор качал бы ряд заново без изменений на сайте.
        rng = {"date_from": ctx.start_for(fund.rules).isoformat(), "date_to": ""}
        price_doc = ctx.http.get(PRICE_API, params={**page.price_params, **rng}, fmt="json", funds=[fund.rules])
        nav_doc = ctx.http.get(NAV_API, params={**page.nav_params, **rng}, fmt="json", funds=[fund.rules])
        prices, navs = parse_prices(price_doc.content()), parse_navs(nav_doc.content())

        fd = FundData(fund.rules, currency=page.currency, page_url=page_url,
                      official_returns=[OfficialReturn(p, v, as_of, page_url) for p, v, as_of in page.returns
                                        if v is not None])
        for doc, points in ((nav_doc, navs), (price_doc, prices)):
            seg = self.segment(doc, points, page_url=page_url, since=ctx.since)
            if seg.points:
                fd.segments.append(seg)
        missing = [p for p, v, _ in page.returns if v is None]
        if missing:
            fd.warnings.append(f"доходность за {', '.join(missing)} на {page_url} не указана («-»)")

        if page.payouts_params is not None:
            # выплаты нужны целиком: список короткий, а date_from сервер мог бы применить и к нему
            pay_doc = ctx.http.get(PAYOUTS_API, params={**page.payouts_params, "date_to": rng["date_to"]},
                                   fmt="json", funds=[fund.rules])
            fd.payouts = [p for p in parse_payouts(pay_doc.content(), pay_doc.url) if p.date >= ctx.since]

        has_nav = any(p.nav is not None for s in fd.segments for p in s.points)
        has_price = any(p.price is not None for s in fd.segments for p in s.points)
        if not fd.segments:
            empty = f"графики на {page_url} не содержат точек с {rng['date_from']}"
            if fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", f"фонд формируется: {empty}"
            elif fund.rules in ctx.last_dates:
                # при докачке хвоста точки с нахлёстом были всегда: пустой ответ — сбой, а не конец истории,
                # прошлые точки сборщик сохранит с пометкой «не обновилось»
                fd.status, fd.reason = "unavailable", f"{empty}, хотя раньше были"
            else:
                fd.status, fd.reason = "no_history", empty
        elif not has_nav:
            fd.status, fd.reason = "partial", "УК публикует только стоимость пая, без СЧА"
        elif not has_price:
            fd.status, fd.reason = "partial", "УК публикует только СЧА, без стоимости пая"
        return fd
