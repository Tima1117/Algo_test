"""ИП «ВербаКапитал» (verbapartners.ru): график admin-ajax get_bond_values, страница фонда, «Обязательная информация».

Фикстуры — настоящие ответы сайта 03.11.2026: bonds.html и inf_pif.html целиком,
get_bond_values.json — ответ на date_start=2025-09-01, date_end=2026-11-03.
Сверено с таблицей на странице фонда: 02.11.2026 — СЧА 708 367 218,55 руб., пай 441,00 руб.;
30.10.2026 — 715 414 461,15 и 445,39. Доходность 1/3/6 мес (7,96 / 5,56 / 3,34 %) пересчитывается
из ряда от 30.09, 31.07, 30.04.2026.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund, SeriesPoint
from pif.parsing import UnknownFormat
from pif.sources import verba as v
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "verba"
RULES = "1638-94198209"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str = RULES) -> Fund:
    return Fund(rules=rules, name="ВербаКапитал", full_name="ОПИФ РФИ «ВербаКапитал»", type="open",
                category="", status="Сформирован", reg_date="2009-11-19", formation_end="2010-03-17",
                uk_name="ООО «ИП «ВербаКапитал»", uk_short="ИП ВербаКапитал", uk_ogrn="1065404008339",
                uk_inn="5404265403", uk_site="https://www.verbacapital.ru", exchange_tradable=False)


def test_rules_from_mandatory_info():
    assert v.parse_rules(raw("inf_pif.html")) == RULES          # а не «…-12» из изменений
    with pytest.raises(UnknownFormat):
        v.parse_rules(raw("inf_pif.html").decode().replace("Регистрация:", "Рег. номер:"))


def test_chart_values_and_units():
    ccy, pts, ops = v.parse_chart(raw("get_bond_values.json"))
    assert ccy == "RUB" and ops == []
    by = {p.date: p for p in pts}
    assert by[date(2026, 11, 2)] == SeriesPoint(date(2026, 11, 2), 708367218.55, 441.0)
    assert by[date(2026, 10, 30)] == SeriesPoint(date(2026, 10, 30), 715414461.15, 445.39)
    assert pts[0].date == date(2025, 9, 1) and len(pts) == 292
    assert date(2025, 11, 1) in by                               # рабочая суббота 2025 г.
    assert_series(pts, "verba_get_bond_values")


def test_broken_chart_raises():
    good = json.loads(raw("get_bond_values.json"))
    for key in ("date", "pif_price", "asset_price", "currency"):
        bad = dict(good)
        del bad[key]
        with pytest.raises(UnknownFormat):
            v.parse_chart(json.dumps(bad))
    bad = dict(good, pif_price=good["pif_price"][:-1])           # массивы разной длины
    with pytest.raises(UnknownFormat):
        v.parse_chart(json.dumps(bad))
    with pytest.raises(UnknownFormat):
        v.parse_chart(b"0")                                       # так WordPress отвечает на чужой action


def test_returns_block():
    rets = {r.period: r for r in v.parse_returns(raw("bonds.html"), v.FUND_URL)}
    assert {p: r.value_pct for p, r in rets.items()} == {"1m": 7.96, "3m": 5.56, "6m": 3.34, "1y": 8.64}
    assert all(r.as_of == date(2026, 10, 30) for r in rets.values())
    page = raw("bonds.html").decode()
    with pytest.raises(UnknownFormat):
        v.parse_returns(page.replace("<b>6 месяцев</b>", "<b>полгода</b>"), v.FUND_URL)
    with pytest.raises(UnknownFormat):
        v.parse_returns(page.replace("Относительное изменение стоимости пая на", "Доходность на"), v.FUND_URL)


def _ctx(tmp_path, monkeypatch):
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        form = {k: x[0] for k, x in parse_qs(request.content.decode()).items()}
        seen.append((url, form))
        if url == v.AJAX_URL and request.method == "POST" and form.get("action") == "get_bond_values":
            return httpx.Response(200, content=raw("get_bond_values.json"), headers={"content-type": "application/json"})
        pages = {v.FUND_URL: "bonds.html", v.INFO_URL: "inf_pif.html"}
        if url in pages:
            return httpx.Response(200, content=raw(pages[url]), headers={"content-type": "text/html"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.verba"),
                  last_dates={RULES: date(2026, 10, 1)})
    return ctx, seen


def test_adapter_end_to_end_requests_tail(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    fd = v.VerbaAdapter().collect(ctx, [_fund()])[0]
    assert fd.status == "ok" and fd.currency == "RUB" and fd.page_url == v.FUND_URL
    assert fd.segments[0].points[-1] == SeriesPoint(date(2026, 11, 2), 708367218.55, 441.0)
    assert {r.period for r in fd.official_returns} == {"1m", "3m", "6m", "1y"}
    ajax = [form for url, form in seen if url == v.AJAX_URL]
    # докачка хвоста: last - 10 дней; конец диапазона фиксированный, а не «сегодня» — тело запроса не меняется по дням
    assert ajax == [{"action": "get_bond_values", "date_start": "2026-09-21", "date_end": "2099-12-31"}]


def test_adapter_other_rules_not_found(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    fd = v.VerbaAdapter().collect(ctx, [_fund("0000-00000000")])[0]
    assert fd.status == "not_found" and RULES in fd.reason
