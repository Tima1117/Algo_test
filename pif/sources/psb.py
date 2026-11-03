"""ООО «УК ПРОМСВЯЗЬ» (УК ПСБ) — www.upravlyaem.ru, ОПИФ и БПИФ.

Где что лежит (разведка 2026-11-03):
* каталог: /funds/mutual_funds/ — меню сайта со ссылками /funds/<slug>/ на все ОПИФ и БПИФ;
* страница фонда /funds/<slug>/:
  - таблица «Динамика фонда» (div.fund-dynamics): стоимость пая на дату и изменение стоимости
    пая за 1/3/6 мес., с начала года, 1/3/5 лет «в соответствии с Указанием Банка России 5609-У»;
  - ссылка «Загрузить» на выгрузку /upload/costs/costs_<n>_full.csv — вся история фонда без
    заголовка: «ДД.ММ.ГГГГ,стоимость пая,СЧА», СЧА в рублях, строки от новых к старым.
* графики на странице берут те же ряды из /ajax/data.php?fund=<id>&type=price|cost (умеет
  date_from), но цена и СЧА там в разных ответах, а даты — epoch-ms московской полуночи
  с летним временем до 2011 г. Берём CSV: один документ — и дата, и цена, и СЧА.
* выплаты дохода (сейчас у «Дивидендных акций» и «Финансового потока»): у графика стоимости пая
  атрибут data-json-payment="/ajax/datav.php?fund=<id>&…" -> [{"x": epoch-ms полуночи МСК,
  "y": стоимость пая на эту дату, "ap": выплата на пай, ₽}, …] — вся история, параметры дат
  сервер игнорирует, поэтому запрашиваем стабильный адрес ?fund=<id>&type=price.

Ловушки:
* Номер правил ДУ на страницах фонда не публикуется (только внутри PDF правил), поэтому фонд
  реестра сопоставляется с сайтом по названию; у БПИФ на сайте к названию добавлен тикер
  («ПСБ – Денежный рынок - PSMM»).
* Дата выплаты — день, когда доход уже исключён из стоимости пая (у «Дивидендных акций»
  30.09.2026: пай 767,49 ₽, выплата 71,54 ₽). Официальную доходность УК считает без
  реинвестирования: (пай на конец + выплаты с датой в (начало, конец]) / пай на начало − 1,
  где начало — последний рабочий день месяца N месяцев назад. Так воспроизводятся все 8 цифр
  обоих фондов с выплатами на 30.10.2026 до 0,01 п. п.
* Эпохи в /ajax/*.php — полночь по Москве с учётом исторического летнего времени (до 2011 г.
  это 20:00 UTC), поэтому дата берётся через zoneinfo Europe/Moscow, а не «UTC+3».
* «Курс на Восток» на графике переключается и в юани (data-json_new=…&currency=ch) — это пересчёт
  для наглядности; таблица и выгрузка — «Стоимость пая, руб.», валюта пая — рубли.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, Payout, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.upravlyaem.ru"
CATALOG_URL = f"{BASE}/funds/mutual_funds/"
PAYOUTS_URL = f"{BASE}/ajax/datav.php"

try:  # даты в epoch-ms — московская полночь с историческим летним временем
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — нет базы часовых поясов: с 2014 г. Москва всегда UTC+3
    MSK = timezone(timedelta(hours=3))

# Заголовки колонок таблицы «Динамика фонда» -> период OfficialReturn.
RETURN_COLUMNS = {"за 1 мес.": "1m", "за 3 мес.": "3m", "за 6 мес.": "6m", "за 1 год": "1y"}
# Служебные разделы каталога, а не фонды.
NOT_FUNDS = {"mutual_funds", "bpif"}


@dataclass
class PsbFundPage:
    title: str                       # название фонда из <h1>
    csv_url: str                     # выгрузка истории стоимости пая и СЧА
    currency: str
    as_of: date | None               # «Стоимость пая, руб. на 30.10.2026»
    price_as_of: float | None        # стоимость пая на эту дату (для сверки с CSV)
    returns: dict[str, float | None] = field(default_factory=dict)
    payout_fund: str | None = None   # id фонда для /ajax/datav.php, если УК показывает выплаты


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def currency_from_label(label: str) -> str:
    """'Стоимость пая, руб.' -> 'RUB'. Незнакомая валюта — UnknownFormat, а не тихий рубль."""
    s = label.lower()
    for marker, code in (("руб", "RUB"), ("₽", "RUB"), ("долл", "USD"), ("usd", "USD"), ("$", "USD"),
                         ("юан", "CNY"), ("cny", "CNY"), ("евро", "EUR"), ("eur", "EUR"), ("€", "EUR")):
        if marker in s:
            return code
    raise UnknownFormat(f"не понял валюту пая из подписи {label!r}")


def parse_catalog(html: str) -> dict[str, set[str]]:
    """Каталог фондов: адрес страницы фонда -> тексты всех ссылок на неё (название — одна из них)."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, set[str]] = {}
    for a in soup.find_all("a", href=True):
        m = re.fullmatch(r"(?:https?://www\.upravlyaem\.ru)?/funds/([^/?#]+)/", a["href"].strip())
        if not m or m.group(1) in NOT_FUNDS:
            continue
        text = _clean(a.get_text(" "))
        if text:
            out.setdefault(urljoin(BASE, f"/funds/{m.group(1)}/"), set()).add(text)
    require(len(out) >= 5, f"в каталоге найдено только {len(out)} ссылок на фонды — сменилась вёрстка?")
    return out


def parse_fund_page(html: str) -> PsbFundPage:
    """Страница фонда: название, ссылка на CSV, валюта и таблица «Динамика фонда»."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    require(h1 is not None, "на странице фонда нет <h1> с названием")
    links = sorted({a["href"] for a in soup.find_all("a", href=True)
                    if re.search(r"/upload/costs/costs_\d+_full\.csv$", a["href"])})
    require(len(links) == 1, f"ожидалась одна ссылка на выгрузку costs_*_full.csv, найдено {links}")

    table = soup.select_one("div.fund-dynamics table")
    require(table is not None, "нет таблицы «Динамика фонда» (div.fund-dynamics table)")
    heads = [_clean(th.get_text(" ")) for th in table.select("thead th")]
    rows = [tr for tr in table.select("tbody tr") if tr.find("td")]
    require(len(rows) == 1, f"в таблице «Динамика фонда» ожидалась одна строка, их {len(rows)}")
    cells = [_clean(td.get_text(" ")) for td in rows[0].find_all("td")]
    require(len(heads) == len(cells), f"в «Динамике фонда» {len(heads)} заголовков и {len(cells)} ячеек")
    require(len(heads) >= 2 and heads[1].lower().startswith("стоимость пая"),
            f"вторая колонка «Динамики фонда» — не стоимость пая: {heads[:3]}")
    as_of = re.search(r"на (\d{2}\.\d{2}\.\d{4})", heads[1])
    require(as_of is not None, f"нет даты в заголовке {heads[1]!r}")

    lower = [h.lower() for h in heads]
    missing = [h for h in RETURN_COLUMNS if h not in lower]
    require(not missing, f"в «Динамике фонда» нет колонок {missing}; есть {heads}")
    returns = {period: pct(cells[lower.index(h)]) for h, period in RETURN_COLUMNS.items()}

    payout_fund = None
    pay_attrs = {d["data-json-payment"] for d in soup.find_all(attrs={"data-json-payment": True})}
    if pay_attrs:
        ids = set().union(*(re.findall(r"[?&]fund=(\d+)", a) for a in pay_attrs))
        require(len(ids) == 1, f"не понял id фонда в data-json-payment: {sorted(pay_attrs)}")
        payout_fund = ids.pop()
    return PsbFundPage(
        title=_clean(h1.get_text(" ")),
        csv_url=urljoin(BASE, links[0]),
        currency=currency_from_label(heads[1]),
        as_of=day(as_of.group(1)),
        price_as_of=num(cells[1]),
        returns=returns,
        payout_fund=payout_fund,
    )


def parse_returns_psb(html: str, source_url: str) -> list[OfficialReturn]:
    """Официальная доходность (изменение стоимости пая по 5609-У) со страницы фонда."""
    page = parse_fund_page(html)
    return [OfficialReturn(period, value, page.as_of, source_url)
            for period, value in page.returns.items() if value is not None]


def parse_costs_csv(content: bytes | str) -> list[SeriesPoint]:
    """costs_<n>_full.csv: «ДД.ММ.ГГГГ,стоимость пая,СЧА» без заголовка -> ряд по возрастанию дат."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    points: dict[date, SeriesPoint] = {}
    for i, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or not any(c.strip() for c in row):
            continue
        require(len(row) == 3, f"строка {i}: ожидалось 3 колонки (дата, пай, СЧА), получено {row!r}")
        require(bool(re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", row[0].strip())),
                f"строка {i}: первая колонка не дата ДД.ММ.ГГГГ: {row[0]!r} (появился заголовок?)")
        d, price, nav = day(row[0]), num(row[1]), num(row[2])
        if price is None and nav is None:
            continue          # день начала формирования: значений ещё нет
        if price is not None and nav is not None:
            # Колонки не подписаны: СЧА всегда больше цены одного пая — иначе их переставили.
            require(nav >= price, f"строка {i}: СЧА {nav} меньше стоимости пая {price} — сменился порядок колонок?")
        require(d not in points, f"строка {i}: дата {d} повторяется")
        points[d] = SeriesPoint(d, nav, price)
    require(bool(points), "в выгрузке нет ни одной строки с данными")
    return [points[d] for d in sorted(points)]


def _msk_day(ts) -> date:
    require(isinstance(ts, (int, float)) and not isinstance(ts, bool), f"метка времени не число: {ts!r}")
    dt = datetime.fromtimestamp(ts / 1000, MSK)
    require((dt.hour, dt.minute, dt.second) == (0, 0, 0), f"метка {ts} — не полночь по Москве ({dt})")
    return dt.date()


def parse_payouts_psb(content: bytes | str | list, source_url: str) -> list[Payout]:
    """/ajax/datav.php: [{"x": epoch-ms, "y": пай, "ap": выплата на пай}] -> выплаты по возрастанию дат."""
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as e:
            raise UnknownFormat(f"выплаты: ответ не JSON: {e}") from e
    require(isinstance(content, list), f"выплаты: ожидался список, получено {type(content).__name__}")
    out: dict[date, Payout] = {}
    for item in content:
        require_keys(item, ["x", "ap"], "выплата")
        d, amount = _msk_day(item["x"]), num(item["ap"])
        require(amount is not None and amount > 0, f"выплата на {d}: сумма {item['ap']!r}")
        require(d not in out, f"выплата на {d} повторяется")
        out[d] = Payout(d, amount, source_url)
    return [out[d] for d in sorted(out)]


class PsbAdapter(Adapter):
    key = "psb"
    title = "УК ПСБ (ООО «УК ПРОМСВЯЗЬ»)"
    uk_inns = ("7718218817",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        catalog: dict = {}

        def get_catalog() -> dict[str, set[str]]:
            # Каталог качаем один раз; его ошибка превращается в статус каждого фонда через guarded().
            if "data" not in catalog:
                try:
                    doc = ctx.http.get(CATALOG_URL, fmt="html", funds=[f.rules for f in funds])
                    catalog["data"] = parse_catalog(doc.text())
                except Exception as e:  # noqa: BLE001 — запомнить и отдать каждому фонду
                    catalog["data"] = e
            if isinstance(catalog["data"], Exception):
                raise catalog["data"]
            return catalog["data"]

        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, get_catalog())) for f in funds]

    @staticmethod
    def _names(fund: Fund) -> set[str]:
        names = {norm_text(fund.name)}
        if fund.ticker:
            names.add(norm_text(f"{fund.name} {fund.ticker}"))   # «ПСБ – Денежный рынок - PSMM»
        return names

    def _fund(self, ctx: Context, fund: Fund, catalog: dict[str, set[str]]) -> FundData:
        names = self._names(fund)
        urls = sorted(url for url, texts in catalog.items() if names & {norm_text(t) for t in texts})
        if not urls:
            return FundData(fund.rules, status="not_found",
                            reason=f"в каталоге УК нет фонда с названием «{fund.name}»")
        if len(urls) > 1:
            return FundData(fund.rules, status="not_found",
                            reason=f"название «{fund.name}» подходит к нескольким страницам: {urls}")
        page_url = urls[0]
        page_doc = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
        html = page_doc.text()
        page = parse_fund_page(html)
        if norm_text(page.title) not in names:
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"на странице {page_url} другой фонд: «{page.title}»")

        csv_doc = ctx.http.get(page.csv_url, fmt="csv", funds=[fund.rules])
        points = parse_costs_csv(csv_doc.content())
        fd = FundData(fund.rules, currency=page.currency, page_url=page_url,
                      official_returns=parse_returns_psb(html, page_url))
        seg = self.segment(csv_doc, points, page_url=page_url, since=ctx.since)
        if seg.points:
            fd.segments.append(seg)

        if page.payout_fund:
            pay_doc = ctx.http.get(PAYOUTS_URL, params={"fund": page.payout_fund, "type": "price"},
                                   fmt="json", funds=[fund.rules])
            fd.payouts = [p for p in parse_payouts_psb(pay_doc.content(), pay_doc.url) if p.date >= ctx.since]

        # Сверка выгрузки с таблицей на странице: та же цена пая на ту же дату.
        by_date = {p.date: p for p in points}
        if page.as_of in by_date and page.price_as_of is not None:
            got = by_date[page.as_of].price
            if got is None or abs(got - page.price_as_of) > 0.006:
                fd.warnings.append(f"цена пая на {page.as_of}: в выгрузке {got}, на странице {page.price_as_of}")

        if not seg.points:
            fd.status, fd.reason = "partial", f"в выгрузке УК нет точек после {ctx.since}"
        elif all(p.nav is None for p in seg.points):
            fd.status, fd.reason = "partial", "УК публикует только стоимость пая, без СЧА"
        elif all(p.price is None for p in seg.points):
            fd.status, fd.reason = "partial", "УК публикует только СЧА, без стоимости пая"
        return fd
