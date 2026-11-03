"""СК «Сбербанк страхование жизни»: JSON страницы раскрытия (get-portfolios, get-params).

Фикстуры — настоящие ответы сайта 03.11.2026, урезанные по числу записей:
- get-portfolios.json — целиком;
- get-params.json — портфели 1, 2 и служебная копия 3, по несколько дат из каждого года;
- front-api-disclosure.json — раздел ПИФ с несколькими КИД и справками.
Значения сверены с КИД «Первого долевого» на 30.09.2026: пай 1 275,03 руб., СЧА 8 940 705 379,88 руб.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund, SeriesPoint
from pif.parsing import UnknownFormat
from pif.sources import sberlife as s
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "sberlife"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ РФИ «{name}»", type="open", category="",
                status="Сформирован", reg_date="2025-03-26", formation_end=None,
                uk_name="ООО СК «Сбербанк страхование жизни»", uk_short="СК Сбербанк страхование жизни",
                uk_ogrn="1037700051146", uk_inn="7744002123", uk_site="https://sberbank-insurance.ru/",
                exchange_tradable=False)


def test_portfolios_and_matching_by_full_name():
    ports = s.parse_portfolios(raw("get-portfolios.json"))
    assert ports[1] == "ОПИФ рыночных финансовых инструментов «Первый долевой»"
    assert len(ports) == 5
    # служебная копия «…»-2» не должна совпасть с витринным фондом
    assert s.match_portfolio(_fund("6930", "Первый долевой"), ports) == 1
    assert s.match_portfolio(_fund("7272", "Пульс рынка"), ports) == 2
    assert s.match_portfolio(_fund("9999", "Какой-то другой"), ports) is None


def test_params_series_values_and_units():
    series = s.parse_params(raw("get-params.json"))
    ccy, pts = series[1]
    assert ccy == "RUB"
    by = {p.date: p for p in pts}
    # сверено с КИД на 30.09.2026: СЧА в рублях, не в тысячах
    assert by[date(2026, 9, 30)] == SeriesPoint(date(2026, 9, 30), 8940705379.88, 1275.03)
    assert by[date(2026, 11, 2)].price == 1290.94
    assert by[date(2026, 11, 2)].nav == pytest.approx(8700742995.34)
    # значение за 30.12.2025 лежит под ключом "2026" (год публикации) — точка всё равно на своей дате
    assert by[date(2025, 12, 30)].price == 1140.53
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    assert pts[0].date == date(2025, 4, 10)
    _, pulse = series[2]
    assert pulse[0] == SeriesPoint(date(2025, 9, 23), 24947524.16, 999.92)


def test_broken_params_raise():
    good = json.loads(raw("get-params.json"))
    for field in ("quote", "quotesDate", "nav"):
        bad = json.loads(json.dumps(good))
        del bad["data"][0]["2026"][0][field]
        with pytest.raises(UnknownFormat):
            s.parse_params(json.dumps(bad))
    bad = json.loads(json.dumps(good))
    bad["data"][0]["2026"][0]["quote"] = "1,2,3abc"
    with pytest.raises(UnknownFormat):
        s.parse_params(json.dumps(bad))
    # одна дата с разными числами — не молчим
    bad = json.loads(json.dumps(good))
    dup = dict(bad["data"][0]["2026"][0], quote="1.00")
    bad["data"][0]["2025"].append(dup)
    with pytest.raises(UnknownFormat):
        s.parse_params(json.dumps(bad))
    with pytest.raises(UnknownFormat):
        s.parse_params(b'{"success": false, "data": []}')
    with pytest.raises(UnknownFormat):
        s.parse_portfolios(b"<html>maintenance</html>")


def test_kid_links_latest_first():
    links = s.parse_kid_links(raw("front-api-disclosure.json"))
    first = [v for k, v in links.items() if "Первый долевой" in k][0]
    assert first and all("КИД" in t for _, t, _ in first)
    assert [u for u, _, _ in first] == sorted((u for u, _, _ in first), reverse=True)


def _ctx(tmp_path, monkeypatch, routes: dict[str, bytes]):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url in routes:
            return httpx.Response(200, content=routes[url], headers={"content-type": "application/json"})
        return httpx.Response(404, content=b"not found")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.sberlife")), seen


def test_adapter_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, {
        s.PORTFOLIOS_URL: raw("get-portfolios.json"),
        s.PARAMS_URL: raw("get-params.json"),
        s.DOCS_URL: raw("front-api-disclosure.json"),
    })
    funds = [_fund("6930", "Первый долевой"), _fund("7272", "Пульс рынка"), _fund("1", "Нет такого")]
    res = {fd.rules: fd for fd in s.SberlifeAdapter().collect(ctx, funds)}
    fd = res["6930"]
    assert fd.status == "ok" and fd.currency == "RUB" and fd.page_url == s.PAGE_URL
    pts = fd.segments[0].points
    assert pts[0].date == date(2025, 9, 1)            # точки до since отрезаны
    assert pts[-1] == SeriesPoint(date(2026, 11, 2), pytest.approx(8700742995.34), 1290.94)
    assert fd.official_returns == []
    assert any("КИД" in w and "/upload/disclosure/" in w for w in fd.warnings)
    assert res["7272"].status == "ok"
    assert res["1"].status == "not_found"
    assert len(seen) == 3                              # общие JSON скачаны по одному разу
