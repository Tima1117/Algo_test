"""Астра Управление активами: история СЧА и пая только в PDF — честный 'no_history', доходность из HTML.

Фикстуры:
- compliance_pif_live_16kb.html — живой ответ https://www.astra-am.ru/compliance/pif/ 03.11.2026:
  первые 16 КБ (дальше соединение из песочницы «замирает»), ссылка на фонд в них уже есть;
- astra-likvidnost_20261008.html — страница фонда из web.archive.org (снимок «id_» от 08.10.2026 —
  байты ответа самого сайта); вырезаны только <svg> и <style>, схлопнуты пробелы. Живой ответ
  совпадает в первых 16 КБ.
Сверено на странице: «Доходность ОПИФ» 1 мес. — 1,13 %, с даты формирования — 2,79 %, по состоянию
на 30.09.2026; правила № 7938; 60 PDF «Сообщение о стоимости чистых активов ПИФ и РСП».
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import astra as a
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "astra"
PAGE = f"{a.BASE}/compliance/astra-likvidnost/"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str = "7938", name: str = "Астра - Ликвидность") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ РФИ «{name}»", type="open", category="",
                status="Сформирован", reg_date="2026-07-06", formation_end="2026-07-16",
                uk_name="АО «Астра Управление активами»", uk_short="Астра УА", uk_ogrn="1097746445180",
                uk_inn="7722691889", uk_site="https://www.astra-am.ru", exchange_tradable=False)


def test_fund_list_from_truncated_live_page():
    assert a.parse_fund_list(raw("compliance_pif_live_16kb.html")) == {"Астра - Ликвидность": PAGE}
    with pytest.raises(UnknownFormat):
        a.parse_fund_list("<html><body><div class='content'>Раздел пуст</div></body></html>")


def test_fund_page_sections_rules_and_returns():
    page = raw("astra-likvidnost_20261008.html")
    sections = a.parse_fund_page(page, PAGE)
    msgs = sections[a.NAV_SECTION]
    assert len(msgs) == 60 and all(d.url.endswith(".pdf") for d in msgs)
    assert max(d.disclosed for d in msgs) == date(2026, 10, 8)
    assert a.parse_rules(page) == {"7938"}
    rets = a.parse_returns(page, PAGE)
    assert [(r.period, r.value_pct, r.as_of) for r in rets] == [("1m", 1.13, date(2026, 9, 30))]  # пустые — не 0
    reason = a.describe_pdf_only(sections)
    assert "только в PDF" in reason and "60 шт." in reason and "08.10.2026" in reason


def test_broken_fund_page_raises():
    page = raw("astra-likvidnost_20261008.html").decode()
    with pytest.raises(UnknownFormat):
        a.parse_fund_page(page.replace("Динамика стоимости пая и СЧА", "Динамика"), PAGE)
    with pytest.raises(UnknownFormat):
        a.parse_returns(page.replace("<strong>1 мес.</strong>", "<strong>Месяц</strong>"), PAGE)
    with pytest.raises(UnknownFormat):
        a.parse_returns(page.replace("Доходности представлены по состоянию на", "Доходности на"), PAGE)
    with pytest.raises(UnknownFormat):
        a.parse_rules(page.replace("7938", "XXXX"))


def _ctx(tmp_path, monkeypatch, routes):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url in routes:
            body = routes[url]
            if isinstance(body, Exception):
                raise body
            return httpx.Response(200, content=body, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.astra")), seen


def test_adapter_pdf_only_is_no_history_with_returns(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, {a.LIST_URL: raw("compliance_pif_live_16kb.html"),
                                             PAGE: raw("astra-likvidnost_20261008.html")})
    res = a.AstraAdapter().collect(ctx, [_fund(), _fund("1111", "Астра - Облигации")])
    fd = res[0]
    assert fd.status == "no_history" and fd.segments == [] and "только в PDF" in fd.reason
    assert fd.page_url == PAGE
    assert [r.period for r in fd.official_returns] == ["1m"]
    assert res[1].status == "not_found"
    assert seen == [a.LIST_URL, PAGE]


def test_adapter_stalled_site_is_unavailable(tmp_path, monkeypatch):
    # Так ведёт себя сайт из песочницы: первые 16 КБ, потом тишина -> ReadTimeout.
    monkeypatch.setattr("pif.http.time.sleep", lambda s: None)
    stall = httpx.ReadTimeout("stalled")
    ctx, _ = _ctx(tmp_path, monkeypatch, {a.LIST_URL: stall})
    fd = a.AstraAdapter().collect(ctx, [_fund()])[0]
    assert fd.status == "unavailable" and fd.segments == []
