"""ООО «Инвестиционное партнерство «ВербаКапитал» — один ОПИФ РФИ «ВербаКапитал» (правила 1638-94198209).

Сайт УК. В реестре ЦБ у УК указан www.verbacapital.ru, но это теперь сайт девелоперской группы
«ВербаКапитал» (проекты домов, без ПИФов). Сайт самой УК — https://verbapartners.ru: на нём
«Реквизиты и лицензии» с ИНН 5404265403 и «Сообщение об изменении адреса сайта в сети Интернет»
(старые адреса verbacapital.ru/ru/services/… переехали сюда же).

Источники (WordPress, обычный HTML + AJAX графика):
* https://verbapartners.ru/ru/services/s_m/bonds — страница фонда: две последние даты СЧА и пая и
  блок «Относительное изменение стоимости пая на ДД.ММ.ГГГГ / Опубликовано …» — таблица
  1 месяц | 3 месяца | 6 месяцев | 1 год | 3 года | 5 лет | С начала работы фонда («+ 7,96%»);
  1/3/6 мес на 30.10.2026 точно пересчитываются из ряда от концов месяцев;
* …/bonds/inf_pif — «Обязательная информация»: «Регистрация: ФСФР России № 1638-94198209 …» —
  по номеру правил сверяем, что это фонд из реестра;
* https://verbapartners.ru/wp-admin/admin-ajax.php — POST action=get_bond_values, date_start,
  date_end (ГГГГ-ММ-ДД): данные графика на странице фонда (скрипт темы js/common.js). Ответ:
  date (unix-время полуночи, новые сверху), pif_price (пай), asset_price (СЧА), currency, ops
  (сплиты/консолидации паёв — график по ним пересчитывает цену, подсказка показывает исходную).
  Берём исходные pif_price и asset_price; непустой ops — предупреждение. Каждый рабочий день,
  история с 2010 г.; запрашиваем с ctx.start_for() (докачка хвоста). СЧА в рублях
  (708 367 218,55 на 02.11.2026 — как в таблице на странице), пай в рублях (currency = RUB).
Сообщений о выплате дохода пайщикам на сайте нет — payouts не заполняем.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://verbapartners.ru"
FUND_URL = f"{BASE}/ru/services/s_m/bonds"
INFO_URL = f"{BASE}/ru/services/s_m/bonds/inf_pif"
AJAX_URL = f"{BASE}/wp-admin/admin-ajax.php"

_MSK = timezone(timedelta(hours=3))
_RULES = re.compile(r"№\s*(\d{4}-\d{8})(?!-)")
_PERIODS = {"1 месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}


def _soup(content) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()


# ---------- разбор ----------

def parse_rules(content) -> str:
    """«Обязательная информация» -> номер правил из строки «Регистрация: … № XXXX-XXXXXXXX от …»."""
    s = _soup(content)
    label = s.find(string=re.compile(r"^\s*Регистрация:\s*$"))
    require(label is not None, "на странице «Обязательная информация» нет поля «Регистрация:»")
    block = label.find_parent(["p", "div", "td", "li", "dt", "tr"]) or label.parent
    text = _text(block)
    nxt = block.find_next_sibling()
    if nxt is not None and not _RULES.search(text):
        text += " " + _text(nxt)
    m = _RULES.search(text)
    require(m is not None, f"в поле «Регистрация» нет номера правил: {text[:200]!r}")
    return m.group(1)


def parse_chart(content) -> tuple[str, list[SeriesPoint], list]:
    """Ответ get_bond_values -> (валюта, точки по возрастанию даты, ops)."""
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except ValueError as e:
            raise UnknownFormat(f"ответ admin-ajax не JSON: {str(content)[:80]!r}") from e
    require_keys(content, ("date", "pif_price", "asset_price", "currency", "ops"), "get_bond_values")
    dates, prices, navs = content["date"], content["pif_price"], content["asset_price"]
    require(isinstance(dates, list) and len(dates) == len(prices) == len(navs),
            f"длины массивов не совпадают: date {len(dates)}, pif_price {len(prices)}, asset_price {len(navs)}")
    ccy = str(content["currency"]).upper()
    require(ccy in ("RUB", "USD", "EUR", "CNY"), f"незнакомая валюта графика {ccy!r}")
    by_date: dict[date, SeriesPoint] = {}
    for ts, price, nav in zip(dates, prices, navs):
        require(isinstance(ts, (int, float)) and not isinstance(ts, bool), f"дата не unix-время: {ts!r}")
        # Полночь по UTC или по Москве — в московском времени это один и тот же день.
        d = datetime.fromtimestamp(ts, _MSK).date()
        price, nav = num(price), num(nav)
        price = price if price and price > 0 else None
        nav = nav if nav and nav > 0 else None
        if price is None and nav is None:
            continue
        p = SeriesPoint(d, nav, price)
        require(by_date.get(d, p) == p, f"две разные точки графика на {d}")
        by_date[d] = p
    return ccy, [by_date[d] for d in sorted(by_date)], list(content["ops"] or [])


def parse_returns(content, source_url: str) -> list[OfficialReturn]:
    """Страница фонда -> «Относительное изменение стоимости пая на ДД.ММ.ГГГГ»: 1/3/6/12 мес."""
    s = _soup(content)
    h = s.find(lambda t: t.name in ("h2", "h3", "h4", "p") and
               _text(t).startswith("Относительное изменение стоимости пая на"))
    require(h is not None, "на странице фонда нет блока «Относительное изменение стоимости пая на …»")
    m = re.search(r"\d{2}\.\d{2}\.\d{4}", _text(h))
    require(m is not None, f"в заголовке доходности нет даты: {_text(h)!r}")
    as_of = day(m.group(0))
    table = h.find_next("table")
    require(table is not None, "после заголовка доходности нет таблицы")
    rows = [[_text(td) for td in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
    require(len(rows) >= 2 and len(rows[0]) == len(rows[1]),
            f"таблица доходности не «подписи + значения»: {rows[:2]}")
    labels, values = rows[0], rows[1]
    out = []
    for label, period in _PERIODS.items():
        require(label in labels, f"в таблице доходности нет колонки {label!r}: {labels}")
        v = pct(values[labels.index(label)].replace("+", ""))
        if v is not None:
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


# ---------- адаптер ----------

class VerbaAdapter(Adapter):
    key = "verba"
    title = "ИП «ВербаКапитал»"
    uk_inns = ("5404265403",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def site_rules() -> str:
            if "rules" not in state:
                state["rules"] = parse_rules(ctx.http.get(INFO_URL, fmt="html", funds=all_rules).content())
            return state["rules"]

        def one(fund: Fund) -> FundData:
            rules = site_rules()
            if rules != fund.rules:
                return FundData(fund.rules, status="not_found", page_url=FUND_URL,
                                reason=f"на {INFO_URL} фонд с правилами № {rules}, а не {fund.rules}")
            start = ctx.start_for(fund.rules)
            raw = ctx.http.get(AJAX_URL, method="POST", fmt="json", funds=[fund.rules], data={
                "action": "get_bond_values",
                "date_start": start.isoformat(),
                "date_end": date.today().isoformat(),
            })
            ccy, points, ops = parse_chart(raw.content())
            fd = FundData(fund.rules, currency=ccy, page_url=FUND_URL,
                          segments=[self.segment(raw, points, page_url=FUND_URL, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status, fd.reason = "no_history", f"график УК не вернул точек с {start:%d.%m.%Y}"
            if ops:
                fd.warnings.append(f"в периоде есть операции с паями (сплит/консолидация): {ops}")
            try:
                page = ctx.http.get(FUND_URL, fmt="html", funds=[fund.rules])
                fd.official_returns = parse_returns(page.content(), FUND_URL)
            except UnknownFormat as e:
                ctx.log.error("[verba] %s: доходность: %s", fund.rules, e)
                fd.errors.append(f"разбор доходности {FUND_URL}: {e}")
            except FetchError as e:   # ряд уже есть: недоступная страница — только предупреждение
                fd.warnings.append(f"страница фонда {FUND_URL} не скачалась ({e.reason}), доходность не взята")
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]
