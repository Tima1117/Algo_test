"""УК Райффайзен (raiffeisen-capital.ru): таблица «Результаты работы ПИФов» и ряд графика.

Фикстуры — настоящие ответы 2026-11-03, урезанные по длине:
* pifs_table_last_month.html — /pifs-table-last-month/; в __NEXT_DATA__ оставлены apiFunds
  целиком, apiFundsFiltered (часть полей), первый пункт menuItems и два абзаца дисклеймера
  (про доходность и про номера правил);
* chart_*.json — ответы my.raiffeisen-capital.ru/api/v1/external/funds/<uuid>/chart с 01.09.2025,
  из середины года удалены точки (оставлены начало периода и последние недели).
Значения сверены с сайтом: на странице «Результаты работы ПИФов» «Данные на 30.10.2026», «Акции» —
10.08 / 11.61 / −0.95 / 2.01 % за 1/3/6 мес. и 1 год; стоимость пая «Акций» в ответе графика
(51 049,87 ₽ на 30.10 и 50 134,76 ₽ на 02.11.2026) совпадает с currentCostShare на страницах сайта.
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
from pif.sources import raiffeisen as rf
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "raiffeisen"
EQUITIES = "a4baea00-7af9-11db-a626-0040ca6957f7"
BONDS_INCOME = "fb00b672-8550-11ee-acf7-005056b9fcae"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _props() -> dict:
    return rf.next_data(_read("pifs_table_last_month.html"))


def _point(points, d: date):
    [p] = [p for p in points if p.date == d]
    return p


def _fund(rules: str, name: str, formation_end: str = "2004-11-30") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ «{name}»", type="open", category="",
                status="Сформирован", reg_date="2004-08-04", formation_end=formation_end,
                uk_name="ООО «УК Райффайзен»", uk_short="УК Райффайзен", uk_ogrn="1037702037680",
                uk_inn="7702358512", uk_site="www.raiffeisen-capital.ru", exchange_tradable=False)


# ---------- страница «Результаты работы ПИФов» ----------
def test_rules_from_disclaimer():
    rules = rf.parse_rules_raif(_props())
    assert rules["0242-74050284"] == "Райффайзен — Облигации"
    assert rules["2241"] == "Райффайзен — Золото"
    assert rules["5097"] == "Райффайзен - Накопительный"
    assert len([r for r in rules if r[0].isdigit()]) == 11      # 10 ОПИФ + ЗПИФ


def test_funds_and_page_links():
    funds = {f.title: f for f in rf.parse_funds_raif(_props())}
    assert len(funds) == 10
    bonds = funds["Облигации"]
    assert bonds.nid == "a4baea02-7af9-11db-a626-0040ca6957f7" and bonds.last_date == date(2026, 11, 2)
    assert bonds.page_url == "https://www.raiffeisen-capital.ru/pifs/raiffeisen-bonds/"
    assert funds["Накопительный"].page_url == "https://www.raiffeisen-capital.ru/landings/opif-rayffayzen-mm-strategy/"


def test_official_returns_are_month_end_table_not_dynamic():
    rets = rf.parse_returns_raif(_props(), rf.TABLE_URL)
    eq = {r.period: (r.value_pct, r.as_of) for r in rets[EQUITIES]}
    # «Результаты работы ПИФов», данные на 30.10.2026; динамическая доходность за год была бы 0,68 %
    assert eq == {"1m": (10.08, date(2026, 10, 30)), "3m": (11.61, date(2026, 10, 30)),
                  "6m": (-0.95, date(2026, 10, 30)), "1y": (2.01, date(2026, 10, 30))}
    assert {r.period: r.value_pct for r in rets[BONDS_INCOME]} == {"1m": 1.14, "3m": 3.19, "6m": 5.98, "1y": 13.83}


def test_page_broken_is_unknown_format():
    html = _read("pifs_table_last_month.html")
    with pytest.raises(UnknownFormat, match="__NEXT_DATA__"):
        rf.next_data(html.replace('id="__NEXT_DATA__"', 'id="__OTHER__"'))
    with pytest.raises(UnknownFormat, match="oneYear"):
        rf.parse_returns_raif(rf.next_data(html.replace('"oneYear": 2.01', '"year": 2.01')), "u")
    with pytest.raises(UnknownFormat, match="Номера и даты"):
        rf.parse_rules_raif(rf.next_data(html.replace("Номера и даты регистрации правил", "Правила")))


# ---------- ряд графика и выплаты ----------
def test_chart_values_match_site():
    pts = rf.parse_chart_raif((FIX / "chart_equities.json").read_bytes())
    assert_series(pts, "raiffeisen_chart_equities")
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    p = _point(pts, date(2026, 11, 2))          # currentCostShare на сайте: 50 134,76 ₽ на 02.11.2026
    assert p.price == 50134.76 and p.nav == 10715287039.77
    p = _point(pts, date(2026, 10, 30))
    assert p.price == 51049.87 and p.nav == 10910645310.40
    assert pts[0].date == date(2025, 9, 1) and pts[0].price == 57163.53


def test_payouts_on_record_date_and_price_already_ex_payout():
    raw = (FIX / "chart_bonds_with_income.json").read_bytes()
    pays = rf.parse_payouts_raif(raw, "https://x/chart")
    assert [(p.date, p.amount) for p in pays] == [(date(2025, 9, 30), 2613.61), (date(2026, 9, 30), 1371.36)]
    pts = rf.parse_chart_raif(raw)
    assert_series(pts, "raiffeisen_chart_bonds_with_income")
    assert _point(pts, date(2025, 9, 29)).price == 52894.21
    assert _point(pts, date(2025, 9, 30)).price == 50209.39       # уже без выплаты
    assert rf.parse_payouts_raif((FIX / "chart_equities.json").read_bytes(), "u") == []


def test_chart_broken_is_unknown_format():
    text = _read("chart_equities.json")
    with pytest.raises(UnknownFormat, match="scha"):
        rf.parse_chart_raif(text.replace('"scha"', '"nav"'))
    # метка не в полночь UTC — дата могла бы съехать на день
    with pytest.raises(UnknownFormat, match="метка даты"):
        rf.parse_chart_raif(text.replace("2026-11-02T00:00:00.000Z", "2026-11-01T21:00:00.000Z"))
    with pytest.raises(UnknownFormat, match="payout"):
        rf.parse_payouts_raif(text.replace('"payout"', '"pay"'), "u")
    data = json.loads(text)
    data["scha"]["items"], data["shares"]["items"] = data["shares"]["items"], data["scha"]["items"]
    with pytest.raises(UnknownFormat, match="перепутаны"):
        rf.parse_chart_raif(data)


# ---------- сбор целиком (без сети) ----------
CHARTS = {EQUITIES: "chart_equities.json", BONDS_INCOME: "chart_bonds_with_income.json"}


def _ctx(tmp_path: Path, monkeypatch, last_dates=None) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path == "/pifs-table-last-month/":
            return httpx.Response(200, content=(FIX / "pifs_table_last_month.html").read_bytes(),
                                  headers={"content-type": "text/html; charset=utf-8"})
        for nid, name in CHARTS.items():
            if path == f"/api/v1/external/funds/{nid}/chart":
                return httpx.Response(200, content=(FIX / name).read_bytes(),
                                      headers={"content-type": "application/json; charset=utf-8"})
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.raiffeisen"),
                  last_dates=last_dates or {})
    return ctx, seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, last_dates={"5778": date(2026, 10, 20)})
    funds = [
        _fund("0241-74050443", "Райффайзен – Акции"),
        _fund("5778", "Райффайзен — Облигации с выплатой дохода", formation_end="2023-12-06"),
        _fund("9999", "Райффайзен – Фонд, которого нет"),
    ]
    res = {fd.rules: fd for fd in rf.RaiffeisenAdapter().collect(ctx, funds)}
    assert list(res) == [f.rules for f in funds]

    eq = res["0241-74050443"]
    assert eq.status == "ok" and eq.currency == "RUB" and not eq.payouts
    assert eq.page_url == "https://www.raiffeisen-capital.ru/pifs/raiffeisen-equities/"
    [seg] = eq.segments
    assert seg.page_url == f"https://my.raiffeisen-capital.ru/client/catalogue/funds/{EQUITIES}"
    assert seg.raw_url.endswith(f"/funds/{EQUITIES}/chart?fromDate=2025-09-01&toDate=2026-11-02")
    assert _point(seg.points, date(2026, 11, 2)).price == 50134.76
    assert {r.period: r.value_pct for r in eq.official_returns} == {"1m": 10.08, "3m": 11.61, "6m": -0.95, "1y": 2.01}

    inc = res["5778"]
    assert inc.status == "ok" and [p.date for p in inc.payouts] == [date(2025, 9, 30), date(2026, 9, 30)]
    # повторный сбор — только хвост с нахлёстом 10 дней от последней собранной точки
    assert any(u.endswith(f"/funds/{BONDS_INCOME}/chart?fromDate=2026-10-10&toDate=2026-11-02") for u in seen)

    assert res["9999"].status == "not_found"


def test_collect_page_broken_marks_every_fund(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(rf, "TABLE_URL", "https://www.raiffeisen-capital.ru/missing/")
    res = rf.RaiffeisenAdapter().collect(ctx, [_fund("0241-74050443", "Райффайзен – Акции")])
    assert res[0].status == "unavailable"
