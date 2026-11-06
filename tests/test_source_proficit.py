"""УК «Профицит»: CSV lk_fund_values (ряд), страница фонда (pif_id, КИД), текст КИД.

Фикстуры — настоящие ответы myproficit.ru 03.11.2026:
- lk_fund_values.csv — целиком (фонды 6492 и 7127);
- proficit_fund.html — фрагмент страницы фонда: скрипты графика и панель «Ключевой информационный
  документ» (страница целиком ~360 КБ — в основном вёрстка и стили);
- kid_30.09.26.txt — текст КИД на 30.09.2026, извлечённый pypdf (PDF ~360 КБ в фикстуры не кладём).
Сверено: на 30.09.2026 пай 1 015,70 руб., СЧА 19 662 055,77 руб. — в CSV и в КИД; доходность КИД
-0,87 / -0,57 / -10,53 / -7,52 % пересчитывается из CSV от 31.08, 30.06, 31.03.2026, 30.09.2025.
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
from pif.sources import proficit as p
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "proficit"
KID = "https://myproficit.ru/wp-content/uploads/2026/10/KID30.09.26.pdf"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str = "6492") -> Fund:
    return Fund(rules=rules, name="Профицит", full_name="ОПИФ РФИ «Профицит»", type="open", category="",
                status="Сформирован", reg_date="2024-09-16", formation_end="2024-10-03",
                uk_name="ООО УК «Профицит»", uk_short="УК Профицит", uk_ogrn="1235500011735",
                uk_inn="5501283302", uk_site="https://www.myproficit.ru", exchange_tradable=False)


def test_csv_values_and_units():
    series = p.parse_csv(raw("lk_fund_values.csv"))
    assert set(series) == {"6492", "7127"}
    assert_series(series["6492"], "proficit_csv_6492")
    assert_series(series["7127"], "proficit_csv_7127")
    by = {x.date: x for x in series["6492"]}
    assert by[date(2026, 9, 30)] == SeriesPoint(date(2026, 9, 30), 19662055.77, 1015.7)   # как в КИД
    assert by[date(2026, 11, 2)] == SeriesPoint(date(2026, 11, 2), 20938273.71, 1110.18)
    assert series["6492"][0] == SeriesPoint(date(2024, 10, 2), 10000000.0, 1000.0)


def test_broken_csv_raises():
    text = raw("lk_fund_values.csv").decode()
    with pytest.raises(UnknownFormat):
        p.parse_csv(text.replace("scha_value", "nav"))
    with pytest.raises(UnknownFormat):
        p.parse_csv(text.replace("6492;2026-11-02;1110.18;20938273.71", "6492;2026-11-02;1110.18"))
    with pytest.raises(UnknownFormat):
        p.parse_csv("<html>404</html>")


def test_fund_page_and_kid():
    ids, kids = p.parse_fund_page(raw("proficit_fund.html"))
    assert ids == {"6492"}
    assert kids[0] == (date(2026, 9, 30), KID)
    rets = {r.period: r for r in p.parse_kid_text(raw("kid_30.09.26.txt").decode(), KID)}
    assert {k: r.value_pct for k, r in rets.items()} == {"1m": -0.87, "3m": -0.57, "6m": -10.53, "1y": -7.52}
    assert all(r.as_of == date(2026, 9, 30) for r in rets.values())
    text = raw("kid_30.09.26.txt").decode()
    with pytest.raises(UnknownFormat):
        p.parse_kid_text(text.replace("6 месяц", "полгода"), KID)
    with pytest.raises(UnknownFormat):
        p.parse_kid_text(text.replace("по состоянию на", "на"), KID)
    with pytest.raises(UnknownFormat):
        p.parse_fund_page("<html><body>Фонд</body></html>")


def _ctx(tmp_path, monkeypatch):
    seen: list[str] = []
    final_csv = "https://myproficit.ru/wp-content/uploads/2026/11/lk_fund_values.csv"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url == p.CSV_URL:                                 # как на сайте: редирект на файл в uploads
            return httpx.Response(302, headers={"location": final_csv})
        if url == final_csv:
            return httpx.Response(200, content=raw("lk_fund_values.csv"), headers={"content-type": "text/csv"})
        if url == p.FUND_URL:
            return httpx.Response(200, content=raw("proficit_fund.html"), headers={"content-type": "text/html"})
        if url == KID:
            return httpx.Response(200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.proficit")), seen


def test_adapter_without_pypdf(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "has_pypdf", lambda: False)
    ctx, seen = _ctx(tmp_path, monkeypatch)
    res = p.ProficitAdapter().collect(ctx, [_fund(), _fund("1")])
    fd = res[0]
    assert fd.status == "ok" and fd.currency == "RUB" and fd.page_url == p.FUND_URL
    pts = fd.segments[0].points
    assert pts[0].date == date(2025, 9, 1) and pts[-1].date == date(2026, 11, 2)
    assert fd.official_returns == [] and any("pypdf" in w and KID in w for w in fd.warnings)
    assert res[1].status == "not_found"
    assert KID not in seen


def test_adapter_with_pypdf(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "has_pypdf", lambda: True)
    monkeypatch.setattr(p, "kid_pdf_text", lambda pdf: raw("kid_30.09.26.txt").decode())
    ctx, _ = _ctx(tmp_path, monkeypatch)
    fd = p.ProficitAdapter().collect(ctx, [_fund()])[0]
    assert {r.period: r.value_pct for r in fd.official_returns}["1m"] == -0.87
    assert fd.official_returns[0].source_url == KID
