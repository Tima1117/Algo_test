"""ПЕРАМО ИНВЕСТ: карточка ИПИФ «КОНСЕРВАТОРия» (цена пая на конец месяца + доходность).

Фикстуры — настоящие ответы www.peramo.ru 03.11.2026, целиком: pif.html (список ПИФов) и
conservatoriya.html (карточка фонда со встроенными let data / let income).
Сверено на странице: «Изменение стоимости пая по состоянию на 30.10.2026» — пай 2268,28 руб.,
за 1 мес. 0,64 %, 3 мес. 1,64 %, 6 мес. 4,11 %, 1 год 10,36 %; на графике 30.09.2026 — 2253,85
(2268,28 / 2253,85 − 1 = 0,64 %).
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund, SeriesPoint
from pif.parsing import UnknownFormat
from pif.sources import peramo as p
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "peramo"
RULES = "2006-94172747"
CARD = f"{p.BASE}/pif/conservatoriya/"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str = RULES) -> Fund:
    return Fund(rules=rules, name="КОНСЕРВАТОРия", full_name="ИПИФ РФИ «КОНСЕРВАТОРия»", type="interval",
                category="", status="Сформирован", reg_date="2010-12-09", formation_end="2011-03-24",
                uk_name="ООО «ПЕРАМО ИНВЕСТ»", uk_short="ПЕРАМО ИНВЕСТ", uk_ogrn="5087746367143",
                uk_inn="7716618947", uk_site="https://www.peramo.ru", exchange_tradable=True)


def test_list_skips_closed_funds():
    assert p.parse_fund_links(raw("pif.html")) == ["conservatoriya"]


def test_rules_prices_and_returns():
    card = raw("conservatoriya.html")
    assert p.parse_rules(card) == RULES
    pts = p.parse_prices(card)
    by = {x.date: x for x in pts}
    assert by[date(2026, 10, 30)] == SeriesPoint(date(2026, 10, 30), None, 2268.28)   # СЧА нет — None, не 0
    assert by[date(2026, 9, 30)].price == 2253.85
    assert by[date(2025, 8, 29)].price == 2038.86
    assert pts[0].date == date(2015, 1, 30) and len(pts) == 140
    rets = {r.period: r for r in p.parse_returns(card, CARD)}
    assert {k: r.value_pct for k, r in rets.items()} == {"1m": 0.64, "3m": 1.64, "6m": 4.11, "1y": 10.36}
    assert all(r.as_of == date(2026, 10, 30) for r in rets.values())


def test_sign_comes_from_income_json():
    card = raw("conservatoriya.html").decode()
    neg = card.replace('"ONE_MONTH_DATA_INCOME":{"PERCENT":0.64', '"ONE_MONTH_DATA_INCOME":{"PERCENT":-0.64')
    assert {r.period: r.value_pct for r in p.parse_returns(neg, CARD)}["1m"] == -0.64
    # таблица и скрипт разошлись — это не «тихо»
    other = card.replace('"ONE_MONTH_DATA_INCOME":{"PERCENT":0.64', '"ONE_MONTH_DATA_INCOME":{"PERCENT":0.99')
    with pytest.raises(UnknownFormat):
        p.parse_returns(other, CARD)


def test_broken_card_raises():
    card = raw("conservatoriya.html").decode()
    with pytest.raises(UnknownFormat):
        p.parse_prices(card.replace('"SHA":', '"PRICE":'))
    with pytest.raises(UnknownFormat):
        p.parse_prices(card.replace("let data =", "var points ="))
    with pytest.raises(UnknownFormat):
        p.parse_returns(card.replace("<th>За 6 мес.</th>", "<th>Полгода</th>"), CARD)
    with pytest.raises(UnknownFormat):
        p.parse_rules(card.replace("Номер и дата регистрации правил", "Регистрация"))


def _ctx(tmp_path, monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        pages = {p.LIST_URL: "pif.html", CARD: "conservatoriya.html"}
        if url in pages:
            return httpx.Response(200, content=raw(pages[url]), headers={"content-type": "text/html"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.peramo")), seen


def test_adapter_partial_price_only(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    res = p.PeramoAdapter().collect(ctx, [_fund(), _fund("0000-00000000")])
    fd = res[0]
    assert fd.status == "partial" and "PDF" in fd.reason and "/pif/conservatoriya/reports/" in fd.reason
    assert fd.currency == "RUB" and fd.page_url == CARD
    pts = fd.segments[0].points
    assert pts[0] == SeriesPoint(date(2025, 9, 30), None, 2036.16)      # помесячно, с since
    assert pts[-1].date == date(2026, 10, 30) and all(x.nav is None for x in pts)
    assert len(fd.official_returns) == 4
    assert res[1].status == "not_found"
    assert seen == [p.LIST_URL, CARD]
