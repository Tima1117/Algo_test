"""Адаптер АО УК «Апрель Капитал»: разбор настоящих ответов сайта (урезаны по числу точек).

fund_details_list.json — ответ POST /api/v01_fund_details_list с 2025-09-01 (сбор 2026-11-03), в каждом
фонде оставлены точки 01–05.09.2025 и 26.10–02.11.2026. funds_footer.html — подвал страницы /funds/
с перечнем фондов и номерами правил. Значения ниже сверены со страницами фондов (часовой пояс Москвы):
/funds/balance/ — «на 02.11.2026: 650,16 руб., СЧА 91 713 105,11; на 30.10.2026: 658,19 / 92 845 973,86»,
доходность «на 30.10.2026»: +6.34 / +5.66 / −2.19 / +6.19 %; /funds/stock/ — 1 182,52 / 444 805 026,72.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.april import (API_URL, FUNDS_PAGE, AprilAdapter, match_funds, parse_fund_list,
                               parse_returns, parse_rules_map, parse_series)
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "april"
API_FIX = FIX / "fund_details_list.json"
FOOTER_FIX = FIX / "funds_footer.html"

# Названия — как в реестре ЦБ (с разными тире и двойными пробелами).
REGISTRY = [
    ("0060-56716383", "Апрель Капитал - Сбалансированный",
     "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Апрель Капитал – Сбалансированный»"),
    ("0118-14241730", "Апрель Капитал – Акции",
     "Открытый паевой инвестиционный фонд рыночных финансовых инструментов  «Апрель Капитал – Акции»"),
    ("0846-94127344", "Апрель Капитал – Акции сырьевых компаний",
     "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Апрель Капитал – Акции сырьевых компаний»"),
    ("0424-79363131", "Апрель Капитал – Облигации плюс",
     "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Апрель Капитал – Облигации плюс»"),
    ("6778", "Апрель Капитал –Долгосрочные облигации",
     "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Апрель Капитал – Долгосрочные облигации»"),
    ("0847-94127333", "Апрель Капитал– Акции несырьевых компаний",
     "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Апрель Капитал  – Акции несырьевых компаний»"),
]
# rules -> id фонда в API сайта
SITE_IDS = {"0060-56716383": 3, "0118-14241730": 2, "0846-94127344": 6, "0424-79363131": 5,
            "6778": 16, "0847-94127333": 7}


def make_fund(rules: str, name: str, full_name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=full_name, type="open",
                category="Фонд рыночных финансовых инструментов", status="Сформирован",
                reg_date="2001-08-01", formation_end=None, uk_name="АО УК «Апрель Капитал»",
                uk_short="АО УК \"Апрель Капитал\"", uk_ogrn="1027700266813", uk_inn="7704239292",
                uk_site="https://www.april-capital.ru", exchange_tradable=False)


FUNDS = [make_fund(*row) for row in REGISTRY]


def api() -> dict:
    return json.loads(API_FIX.read_text("utf-8"))


def item(data: dict, fund_id: int) -> dict:
    return next(it for it in data["success"] if it["id"] == fund_id)


# ---------------- список фондов ----------------

def test_fund_list():
    items = parse_fund_list(API_FIX.read_bytes())
    assert len(items) == 8
    active = {it["id"] for it in items if it["active"]}
    assert active == {2, 3, 5, 6, 7, 16}
    closed = [it for it in items if not it["active"]]
    assert all(it["priceList"] == [] for it in closed)        # закрытые фонды — без ряда


@pytest.mark.parametrize("broken", [
    lambda d: d["success"][1].pop("priceList"),
    lambda d: d["success"][1].pop("shortName"),
    lambda d: d.update(success=[]),
    lambda d: d.update(error={"message": "boom"}),
    lambda d: d.pop("success"),
])
def test_fund_list_broken(broken):
    d = api()
    broken(d)
    with pytest.raises(UnknownFormat):
        parse_fund_list(d)


# ---------------- ряд ----------------

def test_series_balance():
    pts = parse_series(item(api(), 3))
    assert_series(pts, "april_series_balance")
    assert len(pts) == 11
    assert [p.date for p in pts][:5] == [date(2025, 9, d) for d in (1, 2, 3, 4, 5)]
    assert pts[-1].date == date(2026, 11, 2)
    by = {p.date: p for p in pts}
    # копейки -> рубли; даты — полночь по Москве
    assert (by[date(2026, 11, 2)].price, by[date(2026, 11, 2)].nav) == (650.16, 91713105.11)
    assert (by[date(2026, 10, 30)].price, by[date(2026, 10, 30)].nav) == (658.19, 92845973.86)
    assert (by[date(2025, 9, 1)].price, by[date(2025, 9, 1)].nav) == (657.91, 96267203.30)


def test_series_stock():
    by = {p.date: p for p in parse_series(item(api(), 2))}
    assert_series(parse_series(item(api(), 2)), "april_series_stock")
    assert (by[date(2026, 11, 2)].price, by[date(2026, 11, 2)].nav) == (1182.52, 444805026.72)
    assert (by[date(2026, 10, 30)].price, by[date(2026, 10, 30)].nav) == (1208.90, 454728097.77)


@pytest.mark.parametrize("broken", [
    lambda it: it["priceList"][0].pop("sharePrice"),
    lambda it: it["priceList"][0].pop("assetValue"),
    lambda it: it["priceList"][0].update(sharePrice=657.91),            # рубли вместо копеек
    lambda it: it["priceList"][0].update(sharePrice=0),
    lambda it: it["priceList"][0].update(assetValue=-1),
    lambda it: it["priceList"][0].update(date=it["priceList"][0]["date"] + 3 * 3600),   # не полночь МСК
    lambda it: it["priceList"][0].update(date="2025-09-01"),
    lambda it: it["priceList"].append(dict(it["priceList"][0])),        # дубль даты
    lambda it: it.update(priceList=[]),                                 # действующий фонд без ряда
])
def test_series_broken(broken):
    it = copy.deepcopy(item(api(), 3))
    broken(it)
    with pytest.raises(UnknownFormat):
        parse_series(it)


# ---------------- доходность ----------------

def test_returns_balance():
    rets = parse_returns(item(api(), 3), "https://april-capital.ru/funds/balance/")
    assert {r.period: r.value_pct for r in rets} == {"1m": 6.34, "3m": 5.66, "6m": -2.19, "1y": 6.19}
    assert {r.as_of for r in rets} == {date(2026, 10, 30)}
    assert {r.source_url for r in rets} == {"https://april-capital.ru/funds/balance/"}


def test_returns_young_fund_and_zero():
    it = copy.deepcopy(item(api(), 16))     # Долгосрочные облигации: за 3 и 5 лет на сайте «-» (0)
    assert it["dynamics"]["year3"] == 0
    rets = parse_returns(it, "u")
    assert {r.period: r.value_pct for r in rets} == {"1m": 2.54, "3m": -1.17, "6m": -3.38, "1y": 5.17}
    it["dynamics"]["month1"] = 0            # 0 сайт показывает как «-» — не опубликовано
    assert {r.period for r in parse_returns(it, "u")} == {"3m", "6m", "1y"}


def test_returns_closed_fund_and_broken():
    assert parse_returns(item(api(), 1), "u") == []          # закрытый фонд: dynamics = null
    it = copy.deepcopy(item(api(), 3))
    it["dynamics"].pop("year1")
    with pytest.raises(UnknownFormat):
        parse_returns(it, "u")
    it = copy.deepcopy(item(api(), 3))
    it["dynamics"]["month1"] = "6,34"
    with pytest.raises(UnknownFormat):
        parse_returns(it, "u")


# ---------------- номера правил и сопоставление ----------------

def test_rules_map():
    m = parse_rules_map(FOOTER_FIX.read_bytes())
    assert len(m) == 6
    assert m["0118-14241730"] == "Апрель Капитал — Акции"
    assert m["6778"] == "Апрель Капитал — Долгосрочные облигации"
    assert m["0847-94127333"] == "Апрель Капитал — Акции несырьевых компаний"


def test_rules_map_broken():
    html = FOOTER_FIX.read_text("utf-8").replace("Правила доверительного управления", "Правила")
    with pytest.raises(UnknownFormat):
        parse_rules_map(html)


def test_match_by_rules_and_by_name():
    items = parse_fund_list(api())
    by_rules = match_funds(FUNDS, items, parse_rules_map(FOOTER_FIX.read_bytes()))
    assert {r: it["id"] for r, it in by_rules.items()} == SITE_IDS
    by_name = match_funds(FUNDS, items, {})                  # подвал не прочитан — по названию реестра
    assert {r: it["id"] for r, it in by_name.items()} == SITE_IDS
    # закрытый на сайте фонд «Облигации» (active=false) не сопоставляется, хоть название и совпало
    stray = make_fund("0000-0", "Апрель Капитал - Облигации",
                      "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Апрель Капитал - Облигации»")
    assert any(not it["active"] and it["shortName"].endswith("Облигации»") for it in items)
    assert match_funds([stray], items, {}) == {}


# ---------------- collect целиком (без сети) ----------------

class FakeHttp:
    def __init__(self, fail: set[str] = frozenset()):
        self.fail = fail
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if url in self.fail:
            raise FetchError(url, "сайт не отвечает")
        path = API_FIX if url == API_URL else FOOTER_FIX
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(),
                      "2026-11-03T12:00:00+00:00", kw.get("fmt", "html"))


def run(http: FakeHttp):
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))
    return {fd.rules: fd for fd in AprilAdapter().collect(ctx, FUNDS)}


def test_collect():
    http = FakeHttp()
    res = run(http)
    assert set(res) == set(SITE_IDS)
    for fd in res.values():
        assert fd.status == "ok", (fd.rules, fd.reason)
        assert fd.currency == "RUB" and not fd.warnings
        assert len(fd.segments) == 1 and len(fd.segments[0].points) == 11
        assert_series(fd.segments[0].points, f"april_collect_{fd.rules}")
        assert fd.segments[0].raw_url == API_URL
        assert {r.period for r in fd.official_returns} == {"1m", "3m", "6m", "1y"}
    bal = res["0060-56716383"]
    assert bal.page_url == "https://april-capital.ru/funds/balance/"
    assert bal.segments[0].points[-1].price == 650.16
    # запрос стабилен: всегда с начала истории, без «сегодня» в теле
    url, kw = http.calls[0]
    assert url == API_URL and kw["method"] == "POST"
    assert kw["json_body"] == {"pointsCount": 100000, "dateStart": 1756674000, "dateEnd": 4102444800}
    assert kw["headers"] == {"Prefer": "params=single-object"}
    assert sorted(kw["funds"]) == sorted(SITE_IDS)


def test_collect_footer_unavailable():
    res = run(FakeHttp(fail={FUNDS_PAGE}))
    for fd in res.values():
        assert fd.status == "ok" and fd.segments
        assert any("сопоставлен по названию" in w for w in fd.warnings)


def test_collect_api_unavailable():
    res = run(FakeHttp(fail={API_URL}))
    assert {fd.status for fd in res.values()} == {"unavailable"}


def test_collect_not_found():
    stray = make_fund("9999-1", "Апрель Капитал – Деньги", "ОПИФ «Апрель Капитал – Деньги»")
    ctx = Context(http=FakeHttp(), since=date(2025, 9, 1), log=logging.getLogger("test"))
    [fd] = AprilAdapter().collect(ctx, [stray])
    assert fd.status == "not_found" and fd.reason
