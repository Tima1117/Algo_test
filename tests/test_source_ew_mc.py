"""ООО «УК «Восток-Запад» (ew-mc.ru): разбор настоящих ответов сайта и сбор без сети.

Фикстуры (tests/fixtures/ew_mc/) — настоящие ответы сайта 03.11.2026:
* funds.html — «Паевые фонды» (без отступов в разметке);
* fund_pervyy.html / fund_cifra.html — страницы ОПИФ «Первый» и ИПИФ «Цифра. Всепогодный портфель»:
  массивы графика shareRows/chaRows урезаны до окон (начало истории, 27.08–05.09.2025, 30.06–03.07.2026,
  с 25.09.2026), списки документов — до первых N; у «Цифры» сохранена пустая запись шаблона
  «[new Date(, -1, ), ]»;
* kid_*.txt — текст КИД на 30.09.2026, как его отдаёт pypdf (сами PDF по ~210 КБ в фикстуры не кладём).
Значения сверены с таблицей «Сообщение о стоимости чистых активов…» на страницах фондов и с КИД (раздел 5).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import ew_mc
from pif.sources.base import Context
from pif.sources.ew_mc import EwMcAdapter, parse_chart, parse_fund_list, parse_fund_page, parse_kid_text
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "ew_mc"
PERVYY = "https://ew-mc.ru/funds/otkrytyy-paevoy-investitsionnyy-fond-rynochnykh-finansovykh-instrumentov-pervyy/"
CIFRA = "https://ew-mc.ru/funds/intervalnyy-paevoy-investitsionnyy-fond-rynochnykh-finansovykh-instrumentov-ts-vsepogodnyy/"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- список фондов ----------

def test_fund_list_maps_names_to_pages():
    pages = parse_fund_list(_read("funds.html"))
    assert pages["первый"] == PERVYY
    assert pages["цифра всепогодный портфель"] == CIFRA
    # ЗПИФ «Заблокированные активы фонда «Первый»» — отдельный фонд, не путается с «Первый»
    assert "заблокированные активы фонда первый" in pages


def test_fund_list_without_cards_raises():
    with pytest.raises(UnknownFormat):
        parse_fund_list(_text("funds.html").replace('class="fund-name"', 'class="title"'))


# ---------- страница фонда: ряд, правила, КИД ----------

def test_pervyy_page_series_rules_and_kids():
    page = parse_fund_page(_read("fund_pervyy.html"))
    assert_series(page.points, "ew_mc_pervyy")
    assert page.rules == "3902" and page.currency == "RUB"
    pts = {p.date: p for p in page.points}
    # таблица на странице: 02.11.2026 «8 999.11» / «67 867 794.76», 30.10.2026 «9 138.66» / «71 012 970.82»
    assert (pts[date(2026, 11, 2)].price, pts[date(2026, 11, 2)].nav) == (8999.11, 67867794.76)
    assert (pts[date(2026, 10, 30)].price, pts[date(2026, 10, 30)].nav) == (9138.66, 71012970.82)
    # КИД на 30.09.2026: «8 457,36 руб.», «65 718 852,49 руб.»
    assert (pts[date(2026, 9, 30)].price, pts[date(2026, 9, 30)].nav) == (8457.36, 65718852.49)
    # месяц в new Date() — с нуля: new Date(2025, 8, 01) — это 01.09.2025, а не 01.08
    assert pts[date(2025, 9, 1)].price == 10196.99 and pts[date(2025, 9, 1)].nav == 79236829.06
    assert page.points[0].date == date(2020, 1, 31)
    assert page.kids[0] == (date(2026, 9, 30), "https://ew-mc.ru/upload/iblock/542/KID_3902_20260930.pdf")


def test_corrected_kid_goes_before_original_of_same_date():
    kids = parse_fund_page(_read("fund_pervyy.html")).kids
    march = [url for d, url in kids if d == date(2026, 3, 31)]
    assert march == ["https://ew-mc.ru/upload/iblock/44f/KID_3902_20260331-_isprav._.pdf",
                     "https://ew-mc.ru/upload/iblock/1dd/KID_3902_20260331.pdf"]


def test_cifra_skips_empty_template_row():
    page = parse_fund_page(_read("fund_cifra.html"))
    assert_series(page.points, "ew_mc_cifra")
    assert page.rules == "7695"
    assert page.points[0].date == date(2026, 6, 30)            # завершение формирования
    assert (page.points[0].price, page.points[0].nav) == (999.83, 84764772.92)
    last = page.points[-1]
    assert (last.date, last.price, last.nav) == (date(2026, 11, 2), 1077.14, 143953266.32)
    assert all(p.price and p.nav for p in page.points)


@pytest.mark.parametrize("cut", ["shareRows", "chaRows"])
def test_missing_chart_array_raises(cut):
    with pytest.raises(UnknownFormat):
        parse_chart(_text("fund_pervyy.html").replace(cut, "otherRows"))


def test_unknown_chart_row_raises():
    html = _text("fund_pervyy.html").replace("[new Date(2026, 10, 02), 8999.11]", "[Date.UTC(2026, 10, 02), 8999.11]")
    with pytest.raises(UnknownFormat):
        parse_chart(html)


def test_chart_disagreeing_with_table_raises():
    # СЧА графика в тысячах — сверка с таблицей последних дней на той же странице это ловит
    html = _text("fund_pervyy.html").replace("[new Date(2026, 10, 02), 67867794.76]",
                                             "[new Date(2026, 10, 02), 67867.79476]")
    with pytest.raises(UnknownFormat):
        parse_fund_page(html)


def test_page_without_rules_raises():
    with pytest.raises(UnknownFormat):
        parse_fund_page(_text("fund_pervyy.html").replace("Дата регистрации и Регистрационный номер фонда",
                                                          "Дата регистрации"))


# ---------- КИД ----------

def test_kid_pervyy():
    kid = parse_kid_text(_text("kid_3902_20260930.txt"))
    assert kid.doc_date == date(2026, 9, 30) and kid.data_date == date(2026, 9, 30)
    assert kid.rules == "3902"
    assert kid.returns == {"1m": 1.48, "3m": 0.47, "6m": -13.49, "1y": -9.76}
    assert kid.price == 8457.36 and kid.nav == 65718852.49


def test_kid_young_fund_has_no_6m_1y():
    kid = parse_kid_text(_text("kid_7695_20260930.txt"))
    assert kid.rules == "7695"
    assert kid.returns == {"1m": -0.59, "3m": 3.87, "6m": None, "1y": None}   # «-**» в КИД
    assert kid.price == 1038.52 and kid.nav == 138792319.98


@pytest.mark.parametrize("cut", ["1 год", "Раздел 5.", "Расчетная стоимость"])
def test_kid_broken_raises(cut):
    with pytest.raises(UnknownFormat):
        parse_kid_text(_text("kid_3902_20260930.txt").replace(cut, ""))


# ---------- сбор целиком без сети ----------

def _fund(rules: str, name: str, ftype: str = "open") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ПИФ РФИ «{name}»", type=ftype,
                category="Фонд рыночных финансовых инструментов", status="Сформирован",
                reg_date="2019-11-12", formation_end=None,
                uk_name='Общество с ограниченной ответственностью "Управляющая компания "Восток-Запад"',
                uk_short='ООО "Управляющая компания "Восток-Запад"', uk_ogrn="1056405422875",
                uk_inn="6454074653", uk_site="https://www.ew-mc.ru", exchange_tradable=True)


ROUTES = {
    "/funds/": "funds.html",
    PERVYY.removeprefix("https://ew-mc.ru"): "fund_pervyy.html",
    CIFRA.removeprefix("https://ew-mc.ru"): "fund_cifra.html",
    "/upload/iblock/542/KID_3902_20260930.pdf": "kid_3902_20260930.txt",
    "/upload/iblock/dd1/KID_7695_20260930.pdf": "kid_7695_20260930.txt",
}


def _ctx(tmp_path: Path, monkeypatch) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        seen.append(path)
        if request.url.host != "ew-mc.ru" or path not in ROUTES:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        ctype = "application/pdf" if path.endswith(".pdf") else "text/html; charset=UTF-8"
        return httpx.Response(200, content=_read(ROUTES[path]), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    # вместо PDF фикстура отдаёт уже извлечённый текст КИД
    monkeypatch.setattr(ew_mc, "kid_pdf_text", lambda content: content.decode("utf-8"))
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.ew_mc")), seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    funds = [_fund("3902", "Первый"), _fund("7695", "Цифра. Всепогодный портфель", "interval"),
             _fund("9999", "Нет такого")]
    pervyy, cifra, missing = EwMcAdapter().collect(ctx, funds)

    assert pervyy.status == "ok" and pervyy.currency == "RUB" and pervyy.page_url == PERVYY
    (seg,) = pervyy.segments
    assert seg.raw_url == PERVYY
    assert seg.points[0].date == date(2025, 9, 1)               # история до since отрезана
    assert seg.points[-1].date == date(2026, 11, 2)
    assert {r.period: r.value_pct for r in pervyy.official_returns} == {"1m": 1.48, "3m": 0.47,
                                                                       "6m": -13.49, "1y": -9.76}
    assert {r.as_of for r in pervyy.official_returns} == {date(2026, 9, 30)}
    assert pervyy.official_returns[0].source_url == "https://ew-mc.ru/upload/iblock/542/KID_3902_20260930.pdf"
    assert pervyy.payouts == [] and pervyy.warnings == []

    assert cifra.status == "ok"
    assert cifra.segments[0].points[0].date == date(2026, 6, 30)  # молодой фонд: вся история с начала
    assert {r.period: r.value_pct for r in cifra.official_returns} == {"1m": -0.59, "3m": 3.87}

    assert missing.status == "not_found" and "Нет такого" in missing.reason
    assert seen.count("/funds/") == 1                            # список фондов — один запрос на УК


def test_collect_rules_mismatch_is_not_found(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    (fd,) = EwMcAdapter().collect(ctx, [_fund("1234", "Первый")])
    assert fd.status == "not_found" and "3902" in fd.reason


def test_collect_without_pypdf_keeps_series(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)

    def no_pypdf(content):
        raise ImportError("No module named 'pypdf'")

    monkeypatch.setattr(ew_mc, "kid_pdf_text", no_pypdf)
    (fd,) = EwMcAdapter().collect(ctx, [_fund("3902", "Первый")])
    assert fd.status == "ok" and fd.segments[0].points
    assert fd.official_returns == []
    assert any("pypdf" in w for w in fd.warnings)


def test_collect_empty_chart_of_forming_fund_is_not_formed(tmp_path, monkeypatch):
    html = _text("fund_cifra.html")
    for name in ("shareRows", "chaRows"):
        html = re.sub(rf"({name}\s*=\s*\[).*?(\]\s*;)", r"\1\2", html, flags=re.S)
    assert parse_fund_page(html).points == []
    forming = tmp_path / "fund_cifra_forming.html"
    forming.write_text(html, "utf-8")
    monkeypatch.setitem(ROUTES, CIFRA.removeprefix("https://ew-mc.ru"), str(forming))   # FIX / абс. путь = путь
    ctx, _ = _ctx(tmp_path, monkeypatch)
    fund = _fund("7695", "Цифра. Всепогодный портфель", "interval")
    fund.status = "Формируется"
    (fd,) = EwMcAdapter().collect(ctx, [fund])
    assert fd.status == "not_formed" and fd.segments[0].points == [] and fd.official_returns == []
