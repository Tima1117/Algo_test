"""ООО «УК Райффайзен» — www.raiffeisen-capital.ru, ОПИФ.

Где что лежит (разведка 2026-11-03). Сайт на Next.js: данные страниц лежат JSON-ом
в <script id="__NEXT_DATA__">, графики ходят в публичный API на поддомене УК my.raiffeisen-capital.ru.
* /pifs-table-last-month/ — «Результаты работы ПИФов», «Данные на <последний рабочий день месяца>».
  В __NEXT_DATA__ в одном документе есть всё, что нужно для сопоставления и доходности:
  - props.pageProps.apiFunds[] — id (uuid фонда в API), title, currentCostDate (дата расчёта)
    и profitabilities: изменение стоимости пая за 1/3/6 мес., 1/3/5 лет по Указанию 5609-У
    (начало периода — последний рабочий день месяца N месяцев назад);
  - apiFundsFiltered[] — те же фонды с датой последней опубликованной стоимости пая;
  - menuItems — ссылки на страницы фондов для человека;
  - disclaimer.disclaimer — «Номера и даты регистрации правил»: название фонда -> номер правил.
* https://my.raiffeisen-capital.ru/api/v1/external/funds/<uuid>/chart?fromDate=&toDate= — ряд графика
  на странице фонда: scha.items[] (СЧА, руб.) и shares.items[] (стоимость пая, руб.), по рабочим дням,
  createAt — дата расчёта в виде полуночи UTC. toDate обязателен.
Внимание: в apiFundsFiltered[].profitabilityExpanded лежит «динамическая» доходность «от той же
календарной даты» — она отличается от официальной таблицы (1 год по «Акциям» 0,68 % против 2,01 %).
Берём таблицу «Результаты работы ПИФов».
Валюта: все ОПИФ УК номинированы в рублях — суммы на сайте в ₽, в данных графика валюты нет.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require, require_keys
from .base import Adapter, Context

BASE = "https://www.raiffeisen-capital.ru"
TABLE_URL = f"{BASE}/pifs-table-last-month/"
CHART_URL = "https://my.raiffeisen-capital.ru/api/v1/external/funds/{nid}/chart"
CURRENCY = "RUB"

RETURN_KEYS = {"oneMonth": "1m", "threeMonths": "3m", "sixMonths": "6m", "oneYear": "1y"}
PERIOD_MONTHS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12}


@dataclass
class RaifFund:
    nid: str                  # uuid фонда в API
    title: str                # «Облигации», «Золото» — без «Райффайзен —»
    last_date: date | None    # последняя опубликованная стоимость пая (apiFundsFiltered)
    page_url: str | None      # страница фонда для человека


def next_data(html: str) -> dict:
    """JSON из <script id="__NEXT_DATA__"> -> props.pageProps."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    require(m is not None, "на странице нет __NEXT_DATA__ (сайт сменил движок?)")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"__NEXT_DATA__ не JSON: {e}") from e
    require_keys(data, ["props"], "__NEXT_DATA__")
    require_keys(data["props"], ["pageProps"], "__NEXT_DATA__.props")
    return data["props"]["pageProps"]


def short_name(name: str) -> str:
    """«Райффайзен — Облигации» / «Облигации» -> 'облигации' (ключ сопоставления)."""
    return re.sub(r"^райффайзен\s+", "", norm_text(name))


def norm_rules(s: str) -> str:
    """'0241–74 050 443' / '№ 2241' -> '0241-74050443' / '2241' (как в реестре ЦБ)."""
    return re.sub(r"[\s№]", "", str(s)).replace("–", "-").replace("—", "-").replace("‑", "-")


def parse_rules_raif(page_props: dict) -> dict[str, str]:
    """Номер правил -> название фонда из дисклеймера «Номера и даты регистрации правил»."""
    require_keys(page_props, ["disclaimer"], "pageProps")
    require_keys(page_props["disclaimer"], ["disclaimer"], "pageProps.disclaimer")
    text = BeautifulSoup(page_props["disclaimer"]["disclaimer"], "lxml").get_text(" ")
    text = text.replace("\xa0", " ")
    start = text.find("Номера и даты регистрации правил")
    require(start >= 0, "в дисклеймере нет блока «Номера и даты регистрации правил»")
    out: dict[str, str] = {}
    for part in text[start:].split(";"):
        m = re.search(r"«(?P<name>.+)»\s*[—–-]\s*(?:№\s*)?(?P<num>\d[\d\s–—-]*\d)\s+от\s+\d{2}\.\d{2}\.\d{4}", part)
        if m:
            out[norm_rules(m.group("num"))] = m.group("name").strip()
    require(len(out) >= 3, f"из дисклеймера разобрано только {len(out)} номеров правил")
    return out


def parse_funds_raif(page_props: dict) -> list[RaifFund]:
    """Действующие фонды: uuid, название, последняя дата, ссылка на страницу."""
    require_keys(page_props, ["apiFundsFiltered", "menuItems"], "pageProps")
    links: dict[str, str] = {}
    for item in page_props["menuItems"] or []:
        for chapter in item.get("chapters") or []:
            for link in chapter.get("links") or []:
                if link.get("name") and str(link.get("url", "")).startswith(("/pifs/", "/landings/")):
                    links[short_name(link["name"])] = BASE + link["url"]
    funds = []
    for f in page_props["apiFundsFiltered"]:
        require_keys(f, ["id", "title", "currentCostDate"], "apiFundsFiltered[]")
        last = day(f["currentCostDate"]) if f["currentCostDate"] else None
        funds.append(RaifFund(f["id"], f["title"], last, links.get(short_name(f["title"]))))
    require(bool(funds), "в apiFundsFiltered нет ни одного фонда")
    return funds


def parse_returns_raif(page_props: dict, source_url: str) -> dict[str, list[OfficialReturn]]:
    """«Результаты работы ПИФов»: uuid фонда -> доходность за 1/3/6/12 мес. на дату расчёта.

    Ноль у молодого фонда означает «нет данных за период» — такие значения фильтрует адаптер,
    сравнивая начало периода с датой формирования фонда.
    """
    require_keys(page_props, ["apiFunds"], "pageProps")
    out: dict[str, list[OfficialReturn]] = {}
    for f in page_props["apiFunds"]:
        require_keys(f, ["id", "currentCostDate", "profitabilities"], "apiFunds[]")
        prof = f["profitabilities"]
        require_keys(prof, list(RETURN_KEYS), f"apiFunds[{f.get('title')}].profitabilities")
        as_of = day(f["currentCostDate"])
        rets = []
        for key, period in RETURN_KEYS.items():
            value = num(prof[key])
            if value is not None:
                rets.append(OfficialReturn(period, value, as_of, source_url))
        out[f["id"]] = rets
    return out


def _chart_items(block, where: str) -> dict[date, float | None]:
    require_keys(block, ["items"], where)
    items = block["items"]
    require(isinstance(items, list), f"{where}.items: ожидался список")
    out: dict[date, float | None] = {}
    for it in items:
        require_keys(it, ["value", "createAt"], f"{where}.items[]")
        ts = str(it["createAt"])
        # Дата расчёта отдаётся полуночью UTC; другое время — значит, сменили часовой пояс и даты съедут.
        require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(T00:00:00(\.0+)?Z)?", ts)),
                f"{where}: неожиданная метка даты {ts!r}")
        d = day(ts)
        require(d not in out, f"{where}: дата {d} повторяется")
        out[d] = num(it["value"])
    return out


def parse_chart_raif(content: bytes | str | dict) -> list[SeriesPoint]:
    """Ответ /external/funds/<uuid>/chart -> ряд (дата, СЧА, стоимость пая) по возрастанию дат."""
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as e:
            raise UnknownFormat(f"ответ графика не JSON: {e}") from e
    require_keys(content, ["scha", "shares"], "chart")
    navs = _chart_items(content["scha"], "scha")
    prices = _chart_items(content["shares"], "shares")
    points = []
    for d in sorted(set(navs) | set(prices)):
        nav, price = navs.get(d), prices.get(d)
        if nav is None and price is None:
            continue
        if nav is not None and price is not None:
            require(nav >= price, f"{d}: СЧА {nav} меньше стоимости пая {price} — перепутаны ряды?")
        points.append(SeriesPoint(d, nav, price))
    return points


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, 1)


class RaiffeisenAdapter(Adapter):
    key = "raiffeisen"
    title = "УК Райффайзен"
    uk_inns = ("7702358512",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        state: dict = {}

        def site():
            # Одна страница на всю УК; её ошибка становится статусом каждого фонда через guarded().
            if "data" not in state:
                try:
                    doc = ctx.http.get(TABLE_URL, fmt="html", funds=[f.rules for f in funds])
                    props = next_data(doc.text())
                    state["data"] = (parse_rules_raif(props), parse_funds_raif(props),
                                     parse_returns_raif(props, TABLE_URL))
                except Exception as e:  # noqa: BLE001
                    state["data"] = e
            if isinstance(state["data"], Exception):
                raise state["data"]
            return state["data"]

        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, *site())) for f in funds]

    def _fund(self, ctx: Context, fund: Fund, rules_map: dict[str, str], site_funds: list[RaifFund],
              returns: dict[str, list[OfficialReturn]]) -> FundData:
        # Сопоставление по номеру правил из дисклеймера сайта; если номера там нет — по названию.
        site_name = rules_map.get(norm_rules(fund.rules))
        how = "по номеру правил" if site_name else "по названию"
        key = short_name(site_name or fund.name)
        found = [f for f in site_funds if short_name(f.title) == key]
        if len(found) != 1:
            return FundData(fund.rules, status="not_found",
                            reason=f"на сайте УК нет действующего фонда «{site_name or fund.name}» ({how})")
        sf = found[0]
        if sf.last_date is None:
            return FundData(fund.rules, status="not_formed", page_url=sf.page_url,
                            reason="УК не публикует стоимость пая — фонд формируется")

        start = ctx.start_for(fund.rules)
        doc = ctx.http.get(CHART_URL.format(nid=sf.nid), fmt="json", funds=[fund.rules],
                           params={"fromDate": start.isoformat(), "toDate": sf.last_date.isoformat()})
        points = parse_chart_raif(doc.content())
        fd = FundData(fund.rules, currency=CURRENCY, page_url=sf.page_url)
        seg = self.segment(doc, points, page_url=sf.page_url, since=ctx.since)
        if seg.points:
            fd.segments.append(seg)

        # Официальная доходность; ноль за период длиннее жизни фонда — это «нет данных», а не 0 %.
        formed = date.fromisoformat(fund.formation_end) if fund.formation_end else None
        for r in returns.get(sf.nid, []):
            if r.value_pct == 0 and formed and r.as_of and _months_back(r.as_of, PERIOD_MONTHS[r.period]) < formed:
                continue
            fd.official_returns.append(r)
        if not fd.official_returns:
            fd.warnings.append("в таблице «Результаты работы ПИФов» нет доходности фонда")

        if not seg.points:
            fd.status, fd.reason = "partial", f"график УК не отдал точек с {start} по {sf.last_date}"
        elif all(p.nav is None for p in seg.points):
            fd.status, fd.reason = "partial", "УК публикует только стоимость пая, без СЧА"
        elif all(p.price is None for p in seg.points):
            fd.status, fd.reason = "partial", "УК публикует только СЧА, без стоимости пая"
        return fd
