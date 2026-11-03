"""ООО «УК РЕНКАП» (rencapwealth.ru): разбор настоящих ответов сайта и сбор без сети.

Фикстуры (tests/fixtures/rencap/) — настоящие ответы сайта 03.11.2026 (в HTML убраны отступы и svg):
* products.html, etf_vdor.html, etf_robl.html — «Продукты» и страницы БПИФ VDOR / ROBL;
* cp_fund_*.json — /api/marketdata/chart-points?view=marketdata.fund_<код>_share_price / _nav;
  у VDOR урезаны до окон 16–21.04, 28.09–02.10 и с 28.10.2026, у ROBL — целиком (13 точек);
* regulations.html, reg_<id>.html, reg_<id>_kid.html — «Обязательная информация», разделы фондов
  и подразделы КИД;
* kid_*_20260930.txt — текст КИД на 30.09.2026, как его отдаёт pypdf.
Значения сверены с ежедневными «Сообщениями о СЧА и расчетной стоимости пая» на 02.11.2026 (PDF)
и с КИД: VDOR 02.11.2026 — 1 069,77 / 987 351 576,56 руб., 30.09.2026 — 1 063,7 / 1 030 941 273,87;
ROBL 02.11.2026 — 1 006,57 / 25 164 266,92 руб.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import rencap
from pif.sources.base import Context
from pif.sources.rencap import (BASE, RenCapAdapter, parse_chart_points, parse_kid_list, parse_kid_text,
                                parse_product_page, parse_products, parse_regulations, parse_rules_footer,
                                parse_section)

FIX = Path(__file__).parent / "fixtures" / "rencap"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- продукты и страница фонда ----------

def test_footer_maps_rules_to_names():
    rules = parse_rules_footer(_read("products.html"))
    assert rules["7681"] == "РенКап – Высокодоходные облигации"
    assert rules["8100"] == "Долгосрочная рублевая стратегия РенКап: широкий рынок – облигации и РЕПО"
    assert rules["7118-СД"] == "Тенденция Первый"


def test_footer_missing_raises():
    with pytest.raises(UnknownFormat):
        parse_rules_footer(_text("products.html").replace("Сведения о регистрации правил", "Сведения"))


def test_products_lists_etf_pages():
    assert parse_products(_read("products.html")) == {
        "vdor": f"{BASE}/products/etf/vdor/", "robl": f"{BASE}/products/etf/robl/"}


def test_product_page_chart_config():
    page = parse_product_page(_read("etf_vdor.html"))
    assert page.name == "РенКап Высокодоходные облигации"
    assert page.currency == "RUB"
    assert page.price_view == "marketdata.fund_vdora_share_price"
    assert page.nav_view == "marketdata.fund_vdora_nav"
    robl = parse_product_page(_read("etf_robl.html"))
    assert robl.nav_view == "marketdata.fund_robl_nav"


@pytest.mark.parametrize("cut", ['"id": "navl"', '"view": "marketdata.fund_vdora_share_price"', "data-chart-config"])
def test_product_page_broken_config_raises(cut):
    with pytest.raises(UnknownFormat):
        parse_product_page(_text("etf_vdor.html").replace(cut, '"x": 1'))


# ---------- ряд ----------

def test_chart_points_vdor_values():
    prices = parse_chart_points(_read("cp_fund_vdora_share_price.json"), "пай")
    navs = parse_chart_points(_read("cp_fund_vdora_nav.json"), "СЧА")
    assert min(prices) == date(2026, 4, 16)                    # завершение формирования
    assert prices[date(2026, 11, 2)] == 1069.77 and navs[date(2026, 11, 2)] == 987351576.56
    assert prices[date(2026, 10, 30)] == 1070.95 and navs[date(2026, 10, 30)] == 991796766.4
    assert prices[date(2026, 9, 30)] == 1063.7 and navs[date(2026, 9, 30)] == 1030941273.87
    assert set(prices) == set(navs)


def test_chart_points_robl_whole_history():
    prices = parse_chart_points(_read("cp_fund_robl_share_price.json"), "пай")
    navs = parse_chart_points(_read("cp_fund_robl_nav.json"), "СЧА")
    assert len(prices) == 13 and min(prices) == date(2026, 10, 15)
    assert prices[date(2026, 11, 2)] == 1006.57 and navs[date(2026, 11, 2)] == 25164266.92


def test_chart_points_moscow_midnight_keeps_day():
    # если УК начнёт отдавать полночь по Москве (21:00 UTC накануне), дата не должна съехать
    assert parse_chart_points('[{"x": 1793566800000, "value": 1.5}]', "пай") == {date(2026, 11, 2): 1.5}


@pytest.mark.parametrize("field", ["x", "value"])
def test_chart_points_missing_field_raises(field):
    rows = json.loads(_read("cp_fund_robl_nav.json"))
    del rows[2][field]
    with pytest.raises(UnknownFormat):
        parse_chart_points(json.dumps(rows), "СЧА")


def test_chart_points_not_a_list_raises():
    with pytest.raises(UnknownFormat):
        parse_chart_points('{"error": "view not found"}', "СЧА")
    with pytest.raises(UnknownFormat):
        parse_chart_points("<html>", "СЧА")


# ---------- раскрытие и КИД ----------

def test_regulations_and_kid_lists():
    regs = parse_regulations(_read("regulations.html"))
    assert regs["ренкап высокодоходные облигации"] == f"{BASE}/regulations/1014/"
    assert regs["долгосрочная рублевая стратегия ренкап широкий рынок облигации и репо"] == f"{BASE}/regulations/1027/"
    assert parse_section(_read("reg_1014.html"), f"{BASE}/regulations/1014/") == f"{BASE}/regulations/1014/1015/"
    assert parse_section(_read("reg_1027.html"), f"{BASE}/regulations/1027/") == f"{BASE}/regulations/1027/1028/"
    kids = parse_kid_list(_read("reg_1014_kid.html"))
    assert kids[0] == (date(2026, 9, 30), f"{BASE}/regulations/1014/1015/doc/69/219/")
    # за 29.05.2026 два КИД: «уточненный» (загружен 19.06) идёт раньше исходного
    may = [url for d, url in kids if d == date(2026, 5, 29)]
    assert may == [f"{BASE}/regulations/1014/1015/doc/42/93/", f"{BASE}/regulations/1014/1015/doc/36/79/"]


def test_kid_vdor():
    kid = parse_kid_text(_text("kid_vdor_20260930.txt"))
    assert kid.doc_date == date(2026, 9, 30) and kid.rules == "7681"
    assert kid.returns == {"1m": 0.65, "3m": 3.02, "6m": None, "1y": None}
    assert kid.price == 1063.7 and kid.nav == 1030941273.87


def test_kid_robl_not_yet_formed_has_no_values():
    kid = parse_kid_text(_text("kid_robl_20260930.txt"))
    assert kid.rules == "8100"
    assert kid.returns == {"1m": None, "3m": None, "6m": None, "1y": None}   # «-*» — недостаточно данных
    assert kid.price is None and kid.nav is None                           # «____ руб.»


@pytest.mark.parametrize("cut", ["6 месяцев", "Раздел 5.", "Расчетная  стоимость"])
def test_kid_broken_raises(cut):
    with pytest.raises(UnknownFormat):
        parse_kid_text(_text("kid_vdor_20260930.txt").replace(cut, ""))


# ---------- сбор целиком без сети ----------

def _fund(rules: str, name: str, ticker: str | None) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"БПИФ РФИ «{name}»", type="exchange",
                category="Фонд рыночных финансовых инструментов", status="Сформирован",
                reg_date="2026-03-26", formation_end=None,
                uk_name="ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «УПРАВЛЯЮЩАЯ КОМПАНИЯ РЕНКАП»",
                uk_short="ООО «УК РЕНКАП»", uk_ogrn="1247700531517", uk_inn="9703186884",
                uk_site="https://www.rencapwealth.ru", exchange_tradable=True, ticker=ticker)


ROUTES = {
    "/products/": "products.html",
    "/products/etf/vdor/": "etf_vdor.html",
    "/products/etf/robl/": "etf_robl.html",
    "/api/marketdata/chart-points?view=marketdata.fund_vdora_share_price": "cp_fund_vdora_share_price.json",
    "/api/marketdata/chart-points?view=marketdata.fund_vdora_nav": "cp_fund_vdora_nav.json",
    "/api/marketdata/chart-points?view=marketdata.fund_robl_share_price": "cp_fund_robl_share_price.json",
    "/api/marketdata/chart-points?view=marketdata.fund_robl_nav": "cp_fund_robl_nav.json",
    "/regulations/": "regulations.html",
    "/regulations/1014/": "reg_1014.html",
    "/regulations/1027/": "reg_1027.html",
    "/regulations/1014/1015/": "reg_1014_kid.html",
    "/regulations/1027/1028/": "reg_1027_kid.html",
    "/regulations/1014/1015/doc/69/219/": "kid_vdor_20260930.txt",
    "/regulations/1027/1028/doc/68/218/": "kid_robl_20260930.txt",
}


def _ctx(tmp_path: Path, monkeypatch) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        seen.append(path)
        if request.url.host != "www.rencapwealth.ru" or path not in ROUTES:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        name = ROUTES[path]
        ctype = {"json": "application/json", "txt": "application/pdf"}.get(name.rsplit(".", 1)[1], "text/html")
        return httpx.Response(200, content=_read(name), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    # вместо PDF фикстура отдаёт уже извлечённый текст КИД
    monkeypatch.setattr(rencap, "kid_pdf_text", lambda content: content.decode("utf-8"))
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.rencap")), seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    funds = [_fund("7681", "РенКап – Высокодоходные облигации", "VDOR"),
             # без тикера страница находится перебором по названию h1
             _fund("8100", "Долгосрочная рублевая стратегия РенКап: широкий рынок - облигации и РЕПО", None),
             _fund("9999", "Нет такого", None)]
    vdor, robl, missing = RenCapAdapter().collect(ctx, funds)

    assert vdor.status == "ok" and vdor.currency == "RUB"
    assert vdor.page_url == f"{BASE}/products/etf/vdor/"
    price_seg, nav_seg = vdor.segments
    assert price_seg.raw_url == f"{BASE}/api/marketdata/chart-points?view=marketdata.fund_vdora_share_price"
    p = {x.date: x for x in price_seg.points}
    n = {x.date: x for x in nav_seg.points}
    assert p[date(2026, 11, 2)].price == 1069.77 and p[date(2026, 11, 2)].nav is None
    assert n[date(2026, 11, 2)].nav == 987351576.56 and n[date(2026, 11, 2)].price is None
    assert {r.period: r.value_pct for r in vdor.official_returns} == {"1m": 0.65, "3m": 3.02}
    assert {r.as_of for r in vdor.official_returns} == {date(2026, 9, 30)}
    assert vdor.official_returns[0].source_url == f"{BASE}/regulations/1014/1015/doc/69/219/"
    assert vdor.payouts == [] and vdor.warnings == []

    # очень молодой фонд: история короче периода, но это всё, что публикует УК, — не partial
    assert robl.status == "ok" and robl.page_url == f"{BASE}/products/etf/robl/"
    assert robl.segments[0].points[0].date == date(2026, 10, 15)
    assert robl.official_returns == []
    assert any("недостаточно данных" in w for w in robl.warnings)

    assert missing.status == "not_found" and "9999" in missing.reason
    assert seen.count("/products/etf/vdor/") == 1                # VDOR — сразу по тикеру


def test_collect_without_pypdf_keeps_series(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)

    def no_pypdf(content):
        raise ImportError("No module named 'pypdf'")

    monkeypatch.setattr(rencap, "kid_pdf_text", no_pypdf)
    (fd,) = RenCapAdapter().collect(ctx, [_fund("7681", "РенКап – Высокодоходные облигации", "VDOR")])
    assert fd.status == "ok" and all(s.points for s in fd.segments)
    assert fd.official_returns == []
    assert any("pypdf" in w for w in fd.warnings)
