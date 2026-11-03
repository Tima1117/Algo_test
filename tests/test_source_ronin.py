"""РОНИН Траст: страница фонда /opif и история trustopif.xls (Excel 97–2003, свой читатель BIFF8).

Фикстуры — настоящие ответы ronintrust.ru 03.11.2026: opif.html целиком; trustopif.xls — файл
целиком (3908 строк), сжат gzip, чтобы фикстура была меньше 100 КБ (сам файл не меняли).
Сверено: страница фонда — 02.11.2026 пай 2 970,72 руб., СЧА 35 648 600,99 руб.; 30.10.2026 —
2 979,57 и 35 754 827,86. Читатель .xls сверен с xlrd на этом же файле: все 3908 строк совпали.
"""
from __future__ import annotations

import gzip
import logging
import struct
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund, SeriesPoint
from pif.parsing import UnknownFormat
from pif.sources import ronin as r
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "ronin"
RULES = "2003-94172428"


def xls() -> bytes:
    return gzip.decompress((FIX / "trustopif.xls.gz").read_bytes())


def page() -> bytes:
    return (FIX / "opif.html").read_bytes()


def _fund(rules: str = RULES) -> Fund:
    return Fund(rules=rules, name="РОНИН фонд сбалансированный",
                full_name="ОПИФ РФИ «РОНИН фонд сбалансированный»", type="open", category="",
                status="Сформирован", reg_date="2010-12-06", formation_end="2010-12-30",
                uk_name="ОАО «РОНИН Траст»", uk_short="РОНИН Траст", uk_ogrn="1027739348999",
                uk_inn="7709379423", uk_site="http://ronintrust.ru/", exchange_tradable=False)


def test_xls_reader_rows():
    rows = r.xls_rows(xls())
    assert len(rows) == 3908
    assert rows[0][0] == 'ОПИФ рыночных финансовых инструментов "РОНИН фонд сбалансированный"'
    assert rows[1] == ["Дата", "Расчетная стоимость пая, руб.", "Стоимость чистых активов, руб."]
    assert rows[2] == ["02.11.2026", 2970.72, 35648600.99]
    assert rows[-1] == ["16.05.2010", 952.48, 11429779.74]


def test_history_values_and_units():
    title, pts = r.parse_history(xls())
    assert "РОНИН фонд сбалансированный" in title
    by = {p.date: p for p in pts}
    assert by[date(2026, 11, 2)] == SeriesPoint(date(2026, 11, 2), 35648600.99, 2970.72)
    assert by[date(2026, 9, 30)] == SeriesPoint(date(2026, 9, 30), 34773734.83, 2897.81)
    assert by[date(2025, 9, 1)] == SeriesPoint(date(2025, 9, 1), 32447193.35, 2703.93)
    assert [p.date for p in pts] == sorted(by) and len(pts) == 3905


def test_page_rules_and_link():
    assert r.parse_page(page()) == (RULES, r.XLS_URL)
    with pytest.raises(UnknownFormat):
        r.parse_page(page().decode().replace("История стоимости пая", "Архив"))
    with pytest.raises(UnknownFormat):
        r.parse_page(page().decode().replace("2003-94172428", "2003"))


def test_broken_xls_raises():
    with pytest.raises(UnknownFormat):
        r.parse_history(b"<html>404</html>")                 # не OLE2
    data = bytearray(xls())
    # «Стоимость чистых активов» в шапке -> другая подпись: колонка не та, разбор должен упасть.
    wb_hint = "Стоимость чистых активов, руб.".encode("utf-16-le")
    i = bytes(data).find(wb_hint)
    if i < 0:                                                 # строка в SST может быть в 1-байтовой кодировке
        pytest.skip("подпись в SST не в UTF-16")
    data[i:i + len(wb_hint)] = "Стоимость имущества фонда, р".ljust(30).encode("utf-16-le")[:len(wb_hint)]
    with pytest.raises(UnknownFormat):
        r.parse_history(bytes(data))
    # повреждённая сигнатура OLE2
    bad = bytearray(xls())
    bad[0:8] = struct.pack("<Q", 0)
    with pytest.raises(UnknownFormat):
        r.parse_history(bytes(bad))


def _ctx(tmp_path, monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url == r.PAGE_URL:
            return httpx.Response(200, content=page(), headers={"content-type": "text/html"})
        if url == r.XLS_URL:
            return httpx.Response(200, content=xls(), headers={"content-type": "application/vnd.ms-excel"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.ronin")), seen


def test_adapter_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    res = r.RoninAdapter().collect(ctx, [_fund(), _fund("0000-00000000")])
    fd = res[0]
    assert fd.status == "ok" and fd.currency == "RUB" and fd.page_url == r.PAGE_URL
    pts = fd.segments[0].points
    assert pts[0].date == date(2025, 9, 1) and pts[-1].date == date(2026, 11, 2)
    assert fd.official_returns == [] and any("КИД" in w for w in fd.warnings)
    assert res[1].status == "not_found"
    assert seen == [r.PAGE_URL, r.XLS_URL]
