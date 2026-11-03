"""ООО УК «Арикапитал» (ИНН 7705525627, https://www.aricapital.ru) — 6 открытых фондов.

Источники (всё — сайт УК, без ключей и логинов):
* Список фондов: https://www.aricapital.ru/funds/index.html — заголовки h2.content__title
  со ссылкой /funds/<id> (fund-cm, fund-gi, fund-rs, fund-ha, fund-mr, fund-ri, ...) и полным
  названием фонда.
* Страница фонда: https://www.aricapital.ru/funds/<id>. График и блок доходности рисуются из
  данных, встроенных в страницу как `var <имя> = <JSON>;`:
  - `var funds` — карточка фонда: id, fullName, registrationNumber, disclaimerName
    («… Правила доверительного управления зарегистрированы … за номером 2565»);
  - `var lineGraphData` — вся история с даты формирования, ежедневно по рабочим дням:
    элементы {currency, value: <id>, type: 'price' | 'nav', data: [[epoch_ms, значение], …]}.
    Берём currency='RUB' — это исходные значения: стоимость пая в рублях и СЧА в рублях
    (не в тысячах). Ряды USD/EUR/CNY сайт сам пересчитывает по курсу ЦБ для графика, их не берём.
    На страницах некоторых фондов в том же массиве лежат ряды фондов-«ориентиров» (у них
    другой value и нет type) — их пропускаем.
    Отметка времени — полночь по Москве (точка 02.11.2026 = 2026-11-01T21:00Z, а в 2013 г.,
    когда Москва жила в UTC+4, — 20:00Z), поэтому дату берём в зоне Europe/Moscow, не в UTC.
  - `var growths` — блок «Прирост стоимости пая за период, %» («Расчёты на <дата>»):
    {<id>: {dateStr: '30 октября 2026', RUB: {date: ISO-UTC, growth1m, growth3m, growth6m,
    growth1y, …}}}. Это официальная доходность УК; значения — строки вида '+11,89'.
    УК считает от конца месяца к концу месяца: as_of — последний рабочий день прошлого месяца,
    база — конец месяца N месяцев назад (1y на 30.10.2026 — от 31.10.2025).
* Та же история таблицей для человека: «Архив: СЧА и стоимость пая»
  https://www.aricapital.ru/disclosure/archive.html?type=navprice:<id> (2 МБ на фонд; сверено
  при разведке — все 2877 строк «Чистых денег» совпадают с lineGraphData). Её адрес ставим
  в page_url сегмента, саму не качаем.

Диапазон дат сайт не принимает — страница всегда отдаёт всю историю, окно режет segment(since).

Сопоставление: по нормализованному названию со страницы списка (у «Разумного инвестора» и
«Харизматичных идей» в названии нет слова «Арикапитал» — так и в реестре ЦБ), затем номер правил
проверяется на странице фонда: номер из дисклеймера или поле registrationNumber должны совпасть
с номером правил реестра. У «Разумного инвестора» поле registrationNumber = 5902 (это номер ЗПИФ
«Заблокированные активы … Глобальные инвестиции»), а дисклеймер и изменения в правила — 6022;
такое расхождение не мешает сопоставлению, но пишется в warnings.

Валюта пая всех фондов — рубль («Стоимость пая, руб.»), в том числе у «Мировых рынков»
(фонд внутренних валютных активов, но пай считается в рублях).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.aricapital.ru"
LIST_URL = f"{BASE}/funds/index.html"
FUND_URL = BASE + "/funds/{site_id}"
ARCHIVE_URL = BASE + "/disclosure/archive.html?type=navprice:{site_id}"

MSK = ZoneInfo("Europe/Moscow")
CURRENCY = "RUB"
# период OfficialReturn -> поле блока «Прирост стоимости пая за период»
RETURN_KEYS = {"1m": "growth1m", "3m": "growth3m", "6m": "growth6m", "1y": "growth1y"}

_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_SITE_ID = re.compile(r"^fund-[a-z0-9-]+$")
_RULES_IN_DISCLAIMER = re.compile(r"за\s+номером\s+(\d+(?:-[А-ЯA-Z]+)?)")


@dataclass
class FundCard:
    """Карточка фонда со страницы /funds/<id>."""
    site_id: str
    name: str
    rules_field: str | None        # поле registrationNumber
    rules_disclaimer: str | None   # номер из текста «… за номером NNNN»


# ---------- разбор ----------

def _text(content: bytes | str) -> str:
    return content.decode("utf-8") if isinstance(content, bytes) else content


def _script_json(html: str, name: str):
    """Значение `var <name> = <JSON>` из встроенного скрипта страницы."""
    found = list(re.finditer(rf"\bvar {name}\s*=\s*", html))
    require(len(found) == 1, f"на странице {len(found)} объявлений var {name}, ожидалось одно")
    try:
        value, _ = json.JSONDecoder().raw_decode(html, found[0].end())
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"var {name}: не JSON ({e.msg})") from e
    return value


def _msk_day_from_ms(ts) -> date:
    """epoch-ms (полночь по Москве) -> календарная дата по Москве."""
    require(isinstance(ts, int) and not isinstance(ts, bool) and ts > 10**11,
            f"ожидалась отметка времени в мс, получено {ts!r}")
    return datetime.fromtimestamp(ts / 1000, MSK).date()


def _msk_day_from_iso(value) -> date:
    """'2026-10-29T21:00:00.000Z' -> 2026-10-30 (дата по Москве)."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as e:
        raise UnknownFormat(f"не дата: {value!r}") from e
    require(dt.tzinfo is not None, f"дата без часового пояса: {value!r}")
    return dt.astimezone(MSK).date()


def _ru_day(value: str) -> date:
    """'30 октября 2026' -> date."""
    m = re.fullmatch(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", str(value).strip().lower())
    if not m or m.group(2) not in _MONTHS:
        raise UnknownFormat(f"не дата: {value!r}")
    return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))


def parse_fund_list(content: bytes | str) -> list[tuple[str, str]]:
    """Страница /funds/index.html -> [(id фонда на сайте, полное название)]."""
    soup = BeautifulSoup(_text(content), "lxml")
    out: list[tuple[str, str]] = []
    for h2 in soup.select("h2.content__title"):
        a = h2.find("a", href=True)
        if a is None or not a["href"].startswith("/funds/"):
            continue
        site_id = a["href"].rstrip("/").rsplit("/", 1)[-1]
        require(bool(_SITE_ID.match(site_id)), f"список фондов: странная ссылка {a['href']!r}")
        name = " ".join(h2.get_text(" ", strip=True).split())
        require(bool(name), f"список фондов: пустое название у {site_id}")
        out.append((site_id, name))
    require(bool(out), "список фондов: нет заголовков h2.content__title со ссылкой /funds/<id>")
    ids = [i for i, _ in out]
    require(len(ids) == len(set(ids)), f"список фондов: повторяются id {ids}")
    return out


def match_site_ids(fund: Fund, site_funds: list[tuple[str, str]]) -> list[str]:
    """id фондов сайта, чьё нормализованное название совпало с названием фонда в реестре."""
    keys = {norm_text(fund.name), norm_text(fund.full_name)} - {""}
    return [site_id for site_id, name in site_funds if norm_text(name) in keys]


def parse_fund_card(content: bytes | str, site_id: str) -> FundCard:
    """Карточка фонда из `var funds` на странице /funds/<id>."""
    cards = _script_json(_text(content), "funds")
    require(isinstance(cards, list), f"var funds: ожидался список, получено {type(cards).__name__}")
    mine = [c for c in cards if isinstance(c, dict) and c.get("id") == site_id]
    require(len(mine) == 1, f"var funds: карточек фонда {site_id}: {len(mine)}, ожидалась одна")
    card = mine[0]
    require_keys(card, ["id", "fullName", "registrationNumber", "disclaimerName"], f"var funds[{site_id}]")
    field = str(card["registrationNumber"]).strip() if card["registrationNumber"] else None
    m = _RULES_IN_DISCLAIMER.search(card["disclaimerName"] or "")
    disclaimer = m.group(1) if m else None
    require(bool(field or disclaimer), f"var funds[{site_id}]: нет номера правил ни в поле, ни в дисклеймере")
    return FundCard(site_id, " ".join(str(card["fullName"]).split()), field, disclaimer)


def _series(entries: list, site_id: str, kind: str) -> dict[date, float | None]:
    mine = [e for e in entries
            if isinstance(e, dict) and e.get("value") == site_id and e.get("currency") == CURRENCY
            and e.get("type") == kind]
    require(len(mine) == 1, f"lineGraphData: рядов {site_id}/{CURRENCY}/{kind}: {len(mine)}, ожидался один")
    require_keys(mine[0], ["data"], f"lineGraphData[{site_id}/{kind}]")
    data = mine[0]["data"]
    require(isinstance(data, list) and bool(data), f"lineGraphData[{site_id}/{kind}]: пустой ряд")
    out: dict[date, float | None] = {}
    label = "стоимость пая" if kind == "price" else "СЧА"
    for item in data:
        require(isinstance(item, list) and len(item) == 2,
                f"lineGraphData[{site_id}/{kind}]: ожидалась пара [время, значение], получено {item!r}")
        d = _msk_day_from_ms(item[0])
        require(d not in out, f"lineGraphData[{site_id}/{kind}]: дата {d} встречается дважды")
        v = num(item[1])
        require(v is None or v > 0, f"{site_id}: {label} на {d} не положительна: {v}")
        out[d] = v
    return out


def parse_series(content: bytes | str, site_id: str) -> list[SeriesPoint]:
    """История стоимости пая и СЧА в рублях из `var lineGraphData` на странице /funds/<id>."""
    entries = _script_json(_text(content), "lineGraphData")
    require(isinstance(entries, list), f"var lineGraphData: ожидался список, получено {type(entries).__name__}")
    prices = _series(entries, site_id, "price")
    navs = _series(entries, site_id, "nav")
    points = [SeriesPoint(d, navs.get(d), prices.get(d)) for d in sorted(set(prices) | set(navs))]
    points = [p for p in points if p.nav is not None or p.price is not None]
    require(bool(points), f"lineGraphData[{site_id}]: нет ни одного значения")
    return points


def parse_returns(content: bytes | str, site_id: str, source_url: str) -> list[OfficialReturn]:
    """Официальная доходность пая в рублях за 1/3/6/12 мес. из `var growths`.

    Пустая строка у периода — УК его не опубликовала: такого периода в ответе нет (не ноль).
    """
    growths = _script_json(_text(content), "growths")
    require_keys(growths, [site_id], "var growths")
    block = growths[site_id]
    require_keys(block, [CURRENCY, "dateStr"], f"var growths[{site_id}]")
    rub = block[CURRENCY]
    require_keys(rub, ["date", *RETURN_KEYS.values()], f"var growths[{site_id}].{CURRENCY}")
    require(rub.get("fundID", site_id) == site_id and rub.get("currencyID", CURRENCY) == CURRENCY,
            f"var growths[{site_id}].{CURRENCY}: чужой блок {rub.get('fundID')}/{rub.get('currencyID')}")
    as_of = _msk_day_from_iso(rub["date"])
    shown = _ru_day(block["dateStr"])
    require(as_of == shown, f"var growths[{site_id}]: дата расчёта {as_of} не совпадает с подписью «{block['dateStr']}»")
    out = []
    for period, key in RETURN_KEYS.items():
        value = pct(rub[key])
        if value is not None:
            out.append(OfficialReturn(period, value, as_of, source_url))
    return out


# ---------- сбор ----------

class AricapitalAdapter(Adapter):
    key = "aricapital"
    title = "УК Арикапитал"
    uk_inns = ("7705525627",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def site_funds() -> list[tuple[str, str]]:
            # Список фондов — один запрос на всю УК. Ошибку запоминаем и повторяем для каждого
            # фонда, чтобы guarded() поставил каждому свой статус с причиной.
            if "error" in state:
                raise state["error"]
            if "list" not in state:
                try:
                    raw = ctx.http.get(LIST_URL, fmt="html", funds=all_rules)
                    state["list"] = parse_fund_list(raw.content())
                except Exception as e:  # noqa: BLE001 — статус определит guarded()
                    state["error"] = e
                    raise
            return state["list"]

        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, site_funds())) for f in funds]

    def _one(self, ctx: Context, fund: Fund, site_funds: list[tuple[str, str]]) -> FundData:
        ids = match_site_ids(fund, site_funds)
        if not ids:
            if fund.status.lower().startswith("формир"):
                return FundData(fund.rules, status="not_formed",
                                reason="фонд формируется, на сайте УК его ещё нет")
            return FundData(fund.rules, status="not_found",
                            reason=f"в списке фондов {LIST_URL} нет фонда с названием «{fund.name}»")
        if len(ids) > 1:
            return FundData(fund.rules, status="not_found",
                            reason=f"название «{fund.name}» совпало с несколькими фондами сайта: {ids}")
        site_id = ids[0]
        url = FUND_URL.format(site_id=site_id)
        raw = ctx.http.get(url, fmt="html", funds=[fund.rules])
        html = raw.text("utf-8")

        card = parse_fund_card(html, site_id)
        shown = [n for n in (card.rules_disclaimer, card.rules_field) if n]
        if fund.rules not in shown:
            return FundData(fund.rules, page_url=url, status="not_found",
                            reason=f"по названию нашлась страница {url}, но номер правил на ней "
                                   f"{'/'.join(shown)}, а не {fund.rules}")
        warnings = []
        if card.rules_field and card.rules_disclaimer and card.rules_field != card.rules_disclaimer:
            warnings.append(f"на странице фонда {url} в карточке registrationNumber={card.rules_field}, "
                            f"а в дисклеймере «за номером {card.rules_disclaimer}»; сопоставлено по названию "
                            f"и номеру правил {fund.rules}")

        points = parse_series(html, site_id)
        seg = self.segment(raw, points, page_url=ARCHIVE_URL.format(site_id=site_id), since=ctx.since)
        returns = parse_returns(html, site_id, source_url=url)
        missing = [p for p in RETURN_KEYS if p not in {r.period for r in returns}]
        if missing:
            warnings.append(f"УК не опубликовала доходность за {', '.join(missing)} (пустое поле на {url})")

        status, reason = "ok", ""
        if not seg.points:
            status, reason = "partial", (f"на сайте нет точек начиная с {ctx.since}; последняя "
                                         f"опубликованная — {points[-1].date}")
        elif any(p.nav is None for p in seg.points) or any(p.price is None for p in seg.points):
            status, reason = "partial", "СЧА или стоимость пая опубликованы не на все даты"
        return FundData(fund.rules, currency=CURRENCY, segments=[seg], official_returns=returns,
                        page_url=url, status=status, reason=reason, warnings=warnings)
