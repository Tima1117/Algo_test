"""Капитал Лайф: JSON графиков web-api.kaplife.ru/dszh/get/, страницы фондов, КИД.

Фикстуры — настоящие ответы сайта 03.11.2026, урезанные:
- dszh_get.json — ответ API по обоим фондам, оставлено 7 дат из ~318;
- uk_disclosure.html, opif1.html, opif2.html — только ссылки / списки документов страниц;
- kid_*_09_2026.txt — текст КИД на 30.09.2026, извлечённый pypdf (PDF по ~150 КБ в фикстуры не
  кладём, pypdf нет в зависимостях проекта).
Сверено: на 30.09.2026 у фонда акций пай 823,73 и СЧА 135 143 488,97 руб. (КИД и API совпадают);
доходность КИД 1,71 / -0,64 / -13,62 / -10,02 % пересчитывается из ряда API на концы месяцев.
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
from pif.sources import kaplife as k
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "kaplife"
EQ = "Капитал Лайф Фонд акций для долевого страхования жизни"
MIX = "Капитал Лайф Фонд смешанных инвестиций для долевого страхования жизни"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ РФИ «{name}»", type="open", category="",
                status="Сформирован", reg_date="2025-06-16", formation_end="2025-07-25",
                uk_name="ООО «Капитал Лайф Страхование Жизни»", uk_short="Капитал Лайф",
                uk_ogrn="1047796614700", uk_inn="7706548313", uk_site="https://www.kaplife.ru",
                exchange_tradable=False)


def test_series_values_units_and_matching():
    series = k.parse_series(raw("dszh_get.json"))
    assert set(series) == {EQ, MIX}
    assert_series(series[EQ], "kaplife_akciy")
    assert_series(series[MIX], "kaplife_smesh")
    eq = {p.date: p for p in series[EQ]}
    # сверено с КИД и справкой о СЧА на 30.09.2026: рубли, не тысячи
    assert eq[date(2026, 9, 30)] == SeriesPoint(date(2026, 9, 30), 135143488.97, 823.73)
    assert eq[date(2026, 11, 2)] == SeriesPoint(date(2026, 11, 2), 154638699.83, 891.13)
    assert series[EQ][0].date == date(2025, 7, 25)            # по возрастанию, хотя API — новые сверху
    assert k.match_series(_fund("7101", EQ), series) == EQ
    assert k.match_series(_fund("7100", MIX), series) == MIX
    assert k.match_series(_fund("1", "Капитал Лайф Фонд облигаций"), series) is None


def test_broken_series_raise():
    good = json.loads(raw("dszh_get.json"))
    for field in ("date", "scha", "price"):
        bad = json.loads(json.dumps(good))
        del bad[next(iter(bad))][0][field]
        with pytest.raises(UnknownFormat):
            k.parse_series(json.dumps(bad))
    with pytest.raises(UnknownFormat):
        k.parse_series(b"")                     # так API отвечает без заголовка Origin
    with pytest.raises(UnknownFormat):
        k.parse_series(json.dumps({"без кавычек": []}))


def test_fund_pages():
    assert k.parse_fund_links(raw("uk_disclosure.html")) == [f"{k.BASE}/uk/opif1/", f"{k.BASE}/uk/opif2/"]
    p1 = k.parse_fund_page(raw("opif1.html"), f"{k.BASE}/uk/opif1/")
    assert p1.rules == {"7101"}
    assert p1.kid_url == f"{k.BASE}/upload/dszh/kid_opif_rfi_akciy_09_2026.pdf"
    assert p1.kid_published == date(2026, 10, 6)
    p2 = k.parse_fund_page(raw("opif2.html"), f"{k.BASE}/uk/opif2/")
    assert p2.rules == {"7100"} and p2.kid_url.endswith("kid_opif_rfi_s_i_09_2026.pdf")
    with pytest.raises(UnknownFormat):
        k.parse_fund_page(b"<html><body><h1>503</h1></body></html>", f"{k.BASE}/uk/opif1/")


def test_kid_returns():
    url = f"{k.BASE}/upload/dszh/kid_opif_rfi_akciy_09_2026.pdf"
    rets = {r.period: r for r in k.parse_kid_text(raw("kid_akciy_09_2026.txt").decode(), url)}
    assert {p: r.value_pct for p, r in rets.items()} == {"1m": 1.71, "3m": -0.64, "6m": -13.62, "1y": -10.02}
    assert all(r.as_of == date(2026, 9, 30) and r.source_url == url for r in rets.values())
    mix = {r.period: r.value_pct for r in k.parse_kid_text(raw("kid_s_i_09_2026.txt").decode(), url)}
    assert mix == {"1m": 0.25, "3m": 1.27, "6m": -0.21, "1y": 7.22}


def test_broken_kid_raises():
    text = raw("kid_akciy_09_2026.txt").decode()
    with pytest.raises(UnknownFormat):
        k.parse_kid_text(text.replace("6 месяцев", "полгода"), "u")
    with pytest.raises(UnknownFormat):
        k.parse_kid_text(text.replace("Основные результаты инвестирования", "Результаты"), "u")
    with pytest.raises(UnknownFormat):
        k.parse_kid_text(text.replace("по состоянию на", "на"), "u")


def _ctx(tmp_path, monkeypatch, routes):
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((request.method, url, request.headers.get("origin")))
        if url == k.API_URL:
            assert request.method == "POST"
            body = raw("dszh_get.json") if request.headers.get("origin") == k.BASE else b""
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        if url in routes:
            return httpx.Response(200, content=routes[url], headers={"content-type": "text/html"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.kaplife")), seen


def test_adapter_end_to_end_without_pypdf(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "has_pypdf", lambda: False)
    ctx, seen = _ctx(tmp_path, monkeypatch, {
        k.LIST_URL: raw("uk_disclosure.html"),
        f"{k.BASE}/uk/opif1/": raw("opif1.html"),
        f"{k.BASE}/uk/opif2/": raw("opif2.html"),
    })
    res = {fd.rules: fd for fd in k.KaplifeAdapter().collect(ctx, [_fund("7101", EQ), _fund("7100", MIX)])}
    fd = res["7101"]
    assert fd.status == "ok" and fd.currency == "RUB"
    assert fd.page_url == f"{k.BASE}/uk/opif1/"
    pts = fd.segments[0].points
    assert pts[0].date == date(2025, 9, 1) and pts[-1].date == date(2026, 11, 2)
    assert fd.official_returns == [] and any("pypdf" in w and "kid_opif_rfi_akciy" in w for w in fd.warnings)
    assert res["7100"].page_url == f"{k.BASE}/uk/opif2/"
    api_calls = [s for s in seen if s[1] == k.API_URL]
    assert api_calls == [("POST", k.API_URL, k.BASE)]           # один запрос на оба фонда, с Origin


def test_adapter_reads_kid_when_pypdf_present(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "has_pypdf", lambda: True)
    monkeypatch.setattr(k, "kid_pdf_text", lambda pdf: raw("kid_akciy_09_2026.txt").decode())
    kid = f"{k.BASE}/upload/dszh/kid_opif_rfi_akciy_09_2026.pdf"
    ctx, _ = _ctx(tmp_path, monkeypatch, {
        k.LIST_URL: raw("uk_disclosure.html"),
        f"{k.BASE}/uk/opif1/": raw("opif1.html"),
        f"{k.BASE}/uk/opif2/": raw("opif2.html"),
        kid: b"%PDF-1.4 fake",
    })
    fd = k.KaplifeAdapter().collect(ctx, [_fund("7101", EQ)])[0]
    assert {r.period: r.value_pct for r in fd.official_returns}["1y"] == -10.02
    assert fd.official_returns[0].source_url == kid
