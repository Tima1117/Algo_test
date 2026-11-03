"""МАКС-Жизнь: блок «Фонды под управлением» и JSON-API calculations страницы фонда.

Фикстуры — настоящие ответы makclife.ru 03.11.2026:
- upravlyayushchaya-kompaniya.html — блок «Фонды под управлением» страницы УК (вся страница ~440 КБ);
- calc_2025-09-01_p1_of100.json — date_from=2025-09-01, page_size=100 (вся история, 27 строк);
- calc_2026-10-01_p{1,2,3}_of10.json — date_from=2026-10-01, page_size=10: три страницы (как листает сайт).
Сверено с «Таблицей стоимости пая и СЧА» на странице фонда: 02.11.2026 — РСИП 1 051,80 руб.,
СЧА 141 993,63 руб.; 30.10.2026 — 1 063,59 и 143 584,77.
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
from pif.sources import makslife as m
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "makslife"
NAME = "МАКС-Жизнь Первый для долевого страхования жизни"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(name: str = NAME, status: str = "Сформирован") -> Fund:
    return Fund(rules="7662", name=name, full_name=f"ОПИФ РФИ «{name}»", type="open", category="",
                status=status, reg_date="2026-03-12", formation_end="2026-09-25",
                uk_name="ООО «МАКС-Жизнь»", uk_short="МАКС-Жизнь", uk_ogrn="1047796266308",
                uk_inn="7724510200", uk_site="https://www.makclife.ru", exchange_tradable=False)


def test_fund_links_and_matching():
    links = m.parse_fund_links(raw("upravlyayushchaya-kompaniya.html"))
    assert links == {"1": NAME}
    assert m.match_fund(_fund(), links) == "1"
    assert m.match_fund(_fund("МАКС-Жизнь Второй"), links) is None
    with pytest.raises(UnknownFormat):
        m.parse_fund_links("<html><body><a href='/invest/'>Инвестировать</a></body></html>")


def test_api_values_and_units():
    pts, more = m.parse_page(raw("calc_2025-09-01_p1_of100.json"))
    assert more is False and len(pts) == 27
    by = {p.date: p for p in pts}
    assert by[date(2026, 11, 2)] == SeriesPoint(date(2026, 11, 2), 141993.63, 1051.8)
    assert by[date(2026, 10, 30)] == SeriesPoint(date(2026, 10, 30), 143584.77, 1063.59)
    assert pts[0] == SeriesPoint(date(2026, 9, 25), 134971.03, 999.79)       # дата формирования
    _, more = m.parse_page(raw("calc_2026-10-01_p1_of10.json"))
    assert more is True


def test_broken_api_raises():
    good = json.loads(raw("calc_2025-09-01_p1_of100.json"))
    for key in ("date", "rsip", "cha"):
        bad = json.loads(json.dumps(good))
        del bad["items"][0][key]
        with pytest.raises(UnknownFormat):
            m.parse_page(json.dumps(bad))
    with pytest.raises(UnknownFormat):      # настоящий ответ на page_size=500
        m.parse_page('{"state":"error","error":"Параметр \\"page_size\\" должен быть целым числом","errorCode":400}')
    bad = json.loads(json.dumps(good))
    del bad["pagination"]
    with pytest.raises(UnknownFormat):
        m.parse_page(json.dumps(bad))


def _ctx(tmp_path, monkeypatch, pages: dict[int, str]):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if str(url) == m.UK_URL:
            return httpx.Response(200, content=raw("upravlyayushchaya-kompaniya.html"), headers={"content-type": "text/html"})
        if str(url.copy_with(query=None)) == m.API_URL:
            q = dict(url.params)
            seen.append(q)
            return httpx.Response(200, content=raw(pages[int(q["page"])]), headers={"content-type": "application/json"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.makslife")), seen


def test_adapter_single_page(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, {1: "calc_2025-09-01_p1_of100.json"})
    fd = m.MakslifeAdapter().collect(ctx, [_fund()])[0]
    assert fd.status == "ok" and fd.currency == "RUB" and fd.page_url == m.FUND_URL.format(id=1)
    assert len(fd.segments) == 1 and fd.segments[0].points[-1].price == 1051.8
    assert seen[0]["opifId"] == "1" and seen[0]["date_from"] == "2025-09-01" and seen[0]["page_size"] == "100"
    assert fd.official_returns == [] and fd.warnings == [m.NO_RETURNS]


def test_adapter_follows_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PAGE_SIZE", 10)
    ctx, seen = _ctx(tmp_path, monkeypatch, {i: f"calc_2026-10-01_p{i}_of10.json" for i in (1, 2, 3)})
    fd = m.MakslifeAdapter().collect(ctx, [_fund()])[0]
    assert [q["page"] for q in seen] == ["1", "2", "3"]
    dates = sorted(p.date for s in fd.segments for p in s.points)
    assert len(dates) == len(set(dates)) == 23
    assert dates[0] == date(2026, 10, 1) and dates[-1] == date(2026, 11, 2)
