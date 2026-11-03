"""ООО УК «ГЕРОИ» (ih-capital.ru): разбор настоящих ответов сайта и сбор без сети.

Фикстуры (tests/fixtures/geroi/) — настоящие ответы сайта 03.11.2026:
* home.html — главная целиком (карточки фондов + подвал с номерами правил);
* disclosure.json — урезан: 4 раздела фондов с несколькими документами, 1 раздел УК;
* funds_3.json / funds_1.json — урезанные ряды «Валютных возможностей» и «Флагманского»
  (окна 29.10–05.11.2025 и 25.09–02.11.2026);
* kid_*.txt — текст КИД, как его отдаёт pypdf (сами PDF по ~250 КБ в фикстуры не кладём).
Значения сверены со страницами https://ih-capital.ru/feo, /flagship (таблица «стоимости пая и СЧА»)
и с КИД (раздел 5).
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
from pif.sources import geroi
from pif.sources.base import Context
from pif.sources.geroi import (GeroiAdapter, kid_data_date, parse_disclosure, parse_history, parse_home,
                               parse_kid_text)

FIX = Path(__file__).parent / "fixtures" / "geroi"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- главная и disclosure.json ----------

def test_home_maps_rules_to_site_name_and_page():
    site = parse_home(_read("home.html"))
    assert set(site) == {"6092", "6100", "6315", "6450"}
    assert site["6100"].name == "Валютные возможности"
    assert site["6100"].page_url == "https://ih-capital.ru/feo"
    assert site["6450"].page_url == "https://ih-capital.ru/rub-perspective"
    assert site["6092"].page_url == "https://ih-capital.ru/flagship"


def test_home_without_rules_footer_raises():
    html = _text("home.html").replace("за номером", "под номером")
    with pytest.raises(UnknownFormat):
        parse_home(html)


def test_disclosure_gives_series_url_and_latest_kid_pdf():
    secs = parse_disclosure(_read("disclosure.json"))
    assert set(secs) == {"флагманский", "валютные возможности", "истории роста", "рублевые перспективы"}
    feo = secs["валютные возможности"]
    assert feo.data_url == "https://ih-capital.ru/funds?id=3"
    assert feo.kids[0]["url"] == "https://ih-capital.ru/documents/feo/key_info_30_09_2026.pdf"
    # «Порядок расчета составного индикатора» (docx) того же типа — не КИД
    flag = secs["флагманский"]
    assert flag.kids[0]["url"] == "https://ih-capital.ru/documents/flagship/key_info_22_10_2026.pdf"
    assert all(k["url"].endswith(".pdf") for s in secs.values() for k in s.kids)


def test_disclosure_broken_raises():
    data = json.loads(_read("disclosure.json"))
    del data[0]["files"]
    with pytest.raises(UnknownFormat):
        parse_disclosure(json.dumps(data))
    data = json.loads(_read("disclosure.json"))
    del data[1]["files"][0]["url"]
    with pytest.raises(UnknownFormat):
        parse_disclosure(json.dumps(data))


# ---------- ряд ----------

def test_history_feo_values_in_rubles():
    pts = {p.date: p for p in parse_history(_read("funds_3.json"))}
    # таблица на https://ih-capital.ru/feo и КИД на 30.09.2026: «1 107.32 руб.», «392 947 117.01 руб.»
    assert pts[date(2026, 9, 30)].price == 1107.32
    assert pts[date(2026, 9, 30)].nav == 392947117.01
    assert pts[date(2026, 11, 2)].price == 1081.45
    assert pts[date(2026, 11, 2)].nav == 378921597.8


def test_history_drops_weekend_copies_but_keeps_working_saturday():
    pts = parse_history(_read("funds_3.json"))
    dates = [p.date for p in pts]
    assert dates == sorted(dates)
    # 31.10–01.11.2026 (сб, вс) — копии пятницы 30.10, их нет
    assert date(2026, 10, 30) in dates
    assert date(2026, 10, 31) not in dates and date(2026, 11, 1) not in dates
    # 01.11.2025 — рабочая суббота: своя цена 1052.43 есть, а 02–04.11 (копии) — нет
    assert date(2025, 11, 1) in dates
    assert {date(2025, 11, 2), date(2025, 11, 3), date(2025, 11, 4)}.isdisjoint(dates)
    assert next(p for p in pts if p.date == date(2025, 11, 1)).price == 1052.43


@pytest.mark.parametrize("field", ["scha", "share", "date"])
def test_history_missing_field_raises(field):
    rows = json.loads(_read("funds_3.json"))
    del rows[3][field]
    with pytest.raises(UnknownFormat):
        parse_history(json.dumps(rows))


def test_history_not_a_list_raises():
    with pytest.raises(UnknownFormat):
        parse_history('{"error": "not found"}')
    with pytest.raises(UnknownFormat):
        parse_history("<html>")


# ---------- КИД ----------

def test_kid_feo_returns_price_nav():
    kid = parse_kid_text(_text("kid_feo_20260930.txt"))
    assert kid.doc_date == date(2026, 9, 30)
    assert kid.returns == {"1m": -2.4, "3m": 2.12, "6m": 1.59, "1y": 3.56}
    assert kid.price == 1107.32
    assert kid.nav == 392947117.01


def test_interim_kid_figures_are_as_of_last_month_end():
    """КИД «по состоянию на 22.10.2026» (после изменения правил) несёт цифры на 30.09.2026."""
    kid = parse_kid_text(_text("kid_flagship_20261022.txt"))
    assert kid.doc_date == date(2026, 10, 22)
    assert kid.price == 873.85 and kid.nav == 694740025.13
    assert kid.returns == {"1m": 1.2, "3m": 0.21, "6m": -7.4, "1y": -0.58}
    assert kid_data_date(kid, parse_history(_read("funds_1.json"))) == date(2026, 9, 30)


@pytest.mark.parametrize("cut", ["1 год", "Доходность за период", "Расчетная стоимость"])
def test_kid_broken_raises(cut):
    text = _text("kid_feo_20260930.txt").replace(cut, "")
    with pytest.raises(UnknownFormat):
        parse_kid_text(text)


# ---------- сбор целиком без сети ----------

def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ РФИ «{name}»", type="open",
                category="Фонд рыночных финансовых инструментов", status="Сформирован",
                reg_date="2024-04-01", formation_end=None,
                uk_name="Общество с ограниченной ответственностью Управляющая компания «ГЕРОИ»",
                uk_short="ООО УК «ГЕРОИ»", uk_ogrn="1237700621245", uk_inn="9725134382",
                uk_site="https://ih-capital.ru", exchange_tradable=False)


ROUTES = {
    "/": ("home.html", "text/html"),
    "/disclosure.json": ("disclosure.json", "application/json"),
    "/funds?id=3": ("funds_3.json", "application/json"),
    "/funds?id=1": ("funds_1.json", "application/json"),
    "/documents/feo/key_info_30_09_2026.pdf": ("kid_feo_20260930.txt", "application/pdf"),
    "/documents/flagship/key_info_22_10_2026.pdf": ("kid_flagship_20261022.txt", "application/pdf"),
}


def _ctx(tmp_path: Path, monkeypatch) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        seen.append(path)
        if path not in ROUTES:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        name, ctype = ROUTES[path]
        return httpx.Response(200, content=_read(name), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    # вместо PDF фикстура отдаёт уже извлечённый текст КИД
    monkeypatch.setattr(geroi, "kid_pdf_text", lambda content: content.decode("utf-8"))
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.geroi")), seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    funds = [_fund("6100", "Валютные возможности"), _fund("6092", "Флагманский"),
             _fund("9999", "Нет такого")]
    feo, flag, missing = GeroiAdapter().collect(ctx, funds)

    assert feo.status == "ok" and feo.currency == "RUB"
    assert feo.page_url == "https://ih-capital.ru/feo"
    (seg,) = feo.segments
    assert seg.raw_url == "https://ih-capital.ru/funds?id=3"
    assert seg.points[0].date == date(2025, 10, 29)          # окно фикстуры, всё после since
    assert {r.period: r.value_pct for r in feo.official_returns} == {"1m": -2.4, "3m": 2.12,
                                                                    "6m": 1.59, "1y": 3.56}
    assert {r.as_of for r in feo.official_returns} == {date(2026, 9, 30)}
    assert feo.official_returns[0].source_url == "https://ih-capital.ru/documents/feo/key_info_30_09_2026.pdf"

    assert flag.status == "ok"
    assert {r.as_of for r in flag.official_returns} == {date(2026, 9, 30)}
    assert any("22.10.2026" in w for w in flag.warnings)

    assert missing.status == "not_found" and "9999" in missing.reason
    # главная и disclosure.json — по одному запросу на всю УК
    assert seen.count("/") == 1 and seen.count("/disclosure.json") == 1


def test_collect_without_pypdf_keeps_series(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)

    def no_pypdf(content):
        raise ImportError("No module named 'pypdf'")

    monkeypatch.setattr(geroi, "kid_pdf_text", no_pypdf)
    (fd,) = GeroiAdapter().collect(ctx, [_fund("6100", "Валютные возможности")])
    assert fd.status == "ok" and fd.segments[0].points
    assert fd.official_returns == []
    assert any("pypdf" in w for w in fd.warnings)
