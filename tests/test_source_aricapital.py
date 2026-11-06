"""Адаптер УК «Арикапитал»: разбор настоящих страниц aricapital.ru (урезанных) и сбор целиком без сети.

Значения сверены с таблицами «Архив: СЧА и стоимость пая»
https://www.aricapital.ru/disclosure/archive.html?type=navprice:<id> и страницами фондов (2026-11-03).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.aricapital import (AricapitalAdapter, FUND_URL, LIST_URL, match_site_ids, parse_fund_card,
                                    parse_fund_list, parse_returns, parse_series)
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "aricapital"


def read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def fund(rules: str, name: str, status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name,
                full_name=f"Открытый паевой инвестиционный фонд рыночных финансовых инструментов «{name}»",
                type="open", category="Фонд рыночных финансовых инструментов", status=status,
                reg_date="2013-03-14", formation_end=None,
                uk_name='Общество с ограниченной ответственностью Управляющая компания "Арикапитал"',
                uk_short='ООО УК "Арикапитал"', uk_ogrn="1127747149155", uk_inn="7705525627",
                uk_site="https://www.aricapital.ru", exchange_tradable=False)


REGISTRY = {  # названия — как в реестре ЦБ (data/registry.json)
    "2565": "Арикапитал - Чистые деньги",
    "4445": "Арикапитал – Мировые рынки",
    "3542": "Арикапитал – Мультистратегия",
    "3208": "Арикапитал – Финансовая свобода",
    "6022": "Разумный инвестор",
    "3606": "Харизматичные идеи",
}


# ---------- список фондов и сопоставление ----------

def test_fund_list_and_matching():
    site = parse_fund_list(read("funds_index.html"))
    # порядок блоков на странице сайт меняет от загрузки к загрузке — сравниваем множеством
    assert {i for i, _ in site} == {"fund-cm", "fund-gi", "fund-rs", "fund-ha", "fund-mr", "fund-gi-za",
                                    "fund-cm-za", "fund-ri", "fund-rk"}
    assert len(site) == 9
    assert dict(site)["fund-ri"] == "ОПИФ рыночных финансовых инструментов «Разумный инвестор»"
    expected = {"2565": "fund-cm", "4445": "fund-mr", "3542": "fund-rs", "3208": "fund-gi",
                "6022": "fund-ri", "3606": "fund-ha"}
    for rules, name in REGISTRY.items():
        # ровно одно совпадение: ЗПИФ «Заблокированные активы … Чистые деньги» не путается с ОПИФ
        assert match_site_ids(fund(rules, name), site) == [expected[rules]], rules
    assert match_site_ids(fund("9999", "Арикапитал – Несуществующий"), site) == []


def test_fund_list_broken():
    html = read("funds_index.html").replace('class="content__title"', 'class="content__heading"')
    with pytest.raises(UnknownFormat):
        parse_fund_list(html)


# ---------- карточка фонда ----------

def test_fund_card_rules():
    assert parse_fund_card(read("fund-cm.html"), "fund-cm").rules_field == "2565"
    card = parse_fund_card(read("fund-ri.html"), "fund-ri")
    # на сайте в поле registrationNumber у «Разумного инвестора» стоит номер ЗПИФ, в дисклеймере — верный
    assert (card.rules_field, card.rules_disclaimer) == ("5902", "6022")
    assert card.name == "ОПИФ рыночных финансовых инструментов «Разумный инвестор»"


def test_fund_card_broken():
    html = read("fund-cm.html").replace('"registrationNumber":', '"regNumber":')
    with pytest.raises(UnknownFormat):
        parse_fund_card(html, "fund-cm")
    with pytest.raises(UnknownFormat):  # карточки нужного фонда нет
        parse_fund_card(read("fund-cm.html"), "fund-rs")


# ---------- ряд ----------

def test_parse_series_cm():
    pts = parse_series(read("fund-cm.html"), "fund-cm")
    assert_series(pts, "aricapital_series_fund_cm")
    by = {p.date: p for p in pts}
    assert len(pts) == 25                     # 2 первые точки + 23 рабочих дня с 01.10.2026
    # первая точка истории: 2013-04-07T20:00Z — полночь по Москве (тогда UTC+4), в архиве «08 апреля 2013»
    assert pts[0].date == date(2013, 4, 8)
    assert (pts[0].nav, pts[0].price) == (9998630.18, 999.86)
    assert (by[date(2026, 10, 1)].nav, by[date(2026, 10, 1)].price) == (1524952521.82, 2702.86)
    assert (by[date(2026, 10, 30)].nav, by[date(2026, 10, 30)].price) == (1479579584.80, 2628.61)
    assert pts[-1].date == date(2026, 11, 2)  # 2026-11-01T21:00Z
    assert (pts[-1].nav, pts[-1].price) == (1485939641.84, 2639.91)
    assert date(2026, 10, 31) not in by       # суббота
    assert [p.date for p in pts] == sorted({p.date for p in pts})


def test_parse_series_skips_benchmark_funds():
    # на странице «Мультистратегии» в том же массиве лежат ряды «Харизматичных идей» и «Мировых рынков»
    pts = parse_series(read("fund-rs.html"), "fund-rs")
    assert_series(pts, "aricapital_series_fund_rs")
    by = {p.date: p for p in pts}
    assert pts[0].date == date(2018, 8, 24) and pts[0].price == 999.96
    assert (by[date(2026, 10, 30)].nav, by[date(2026, 10, 30)].price) == (481250156.24, 2922.68)
    assert (pts[-1].nav, pts[-1].price) == (477406078.75, 2899.33)


def test_parse_series_ri():
    pts = parse_series(read("fund-ri.html"), "fund-ri")
    assert_series(pts, "aricapital_series_fund_ri")
    assert pts[0].date == date(2024, 3, 19) and (pts[0].nav, pts[0].price) == (16698451.35, 999.91)
    assert (pts[-1].date, pts[-1].nav, pts[-1].price) == (date(2026, 11, 2), 335914234.07, 868.62)


@pytest.mark.parametrize("old,new", [
    ('"type":"nav",', ""),                                                 # нет ряда СЧА
    ("var lineGraphData", "var graphData"),                                # переименовали переменную
    ('"currency":"RUB","currencyShortName":"руб.","value":"fund-cm","type":"price"',
     '"currency":"RUR","currencyShortName":"руб.","value":"fund-cm","type":"price"'),  # нет рублёвого ряда цены
    ("[1365364800000,999.86]", "[1365364800000,0]"),                       # нулевая цена
    ("[1365364800000,999.86]", "[1365364800000,999.86],[1365364800000,999.86]"),  # дата дважды
    ("[1365364800000,999.86]", '["2013-04-08",999.86]'),                   # дата не epoch-ms
])
def test_parse_series_broken(old, new):
    html = read("fund-cm.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_series(html.replace(old, new, 1), "fund-cm")


# ---------- официальная доходность ----------

def test_parse_returns():
    url = "https://www.aricapital.ru/funds/fund-rs"
    rs = parse_returns(read("fund-rs.html"), "fund-rs", url)
    # блок «Прирост стоимости пая за период, %», «Расчёты на 30 октября 2026», валюта RUB
    assert {r.period: r.value_pct for r in rs} == {"1m": 5.21, "3m": 7.07, "6m": 6.65, "1y": 16.79}
    assert {r.as_of for r in rs} == {date(2026, 10, 30)}   # в JSON 2026-10-29T21:00Z — это 30.10 по Москве
    assert {r.source_url for r in rs} == {url}
    cm = parse_returns(read("fund-cm.html"), "fund-cm", url)
    assert {r.period: r.value_pct for r in cm} == {"1m": -2.98, "3m": 1.03, "6m": 7.27, "1y": 11.89}


def test_parse_returns_empty_period_is_skipped():
    html = read("fund-ri.html").replace('"growth3m":"+8,91"', '"growth3m":""', 1)
    rs = parse_returns(html, "fund-ri", "u")
    assert [r.period for r in rs] == ["1m", "6m", "1y"]


@pytest.mark.parametrize("old,new", [
    ('"RUB":{"fundID"', '"RUR":{"fundID"'),                       # нет рублёвого блока
    ('"dateStr":"30 октября 2026"', '"dateStr":"29 октября 2026"'),  # подпись не совпала с датой
    ('"growth1y":"+16,79"', '"growth12m":"+16,79"'),             # переименовали поле
    ("var growths", "var yields"),
])
def test_parse_returns_broken(old, new):
    html = read("fund-rs.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_returns(html.replace(old, new, 1), "fund-rs", "u")


# ---------- сбор целиком (без сети) ----------

class FakeHttp:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.calls: list[tuple[str, list[str] | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw) -> RawDoc:
        self.calls.append((url, funds))
        if url not in self.files:
            raise FetchError(url, "HTTP 404", 404)
        path = FIX / self.files[url]
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T15:07:56+00:00", fmt)


def ctx_with(http) -> Context:
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))


def test_collect():
    http = FakeHttp({LIST_URL: "funds_index.html",
                     FUND_URL.format(site_id="fund-cm"): "fund-cm.html",
                     FUND_URL.format(site_id="fund-rs"): "fund-rs.html",
                     FUND_URL.format(site_id="fund-ri"): "fund-ri.html"})
    funds = [fund(r, REGISTRY[r]) for r in ("2565", "3542", "6022", "3208")]
    funds += [fund("9999", "Арикапитал – Несуществующий"), fund("9998", "Новый фонд", status="Формируется"),
              fund("1234", "Разумный инвестор")]
    res = {fd.rules: fd for fd in AricapitalAdapter().collect(ctx_with(http), funds)}

    cm = res["2565"]
    assert (cm.status, cm.currency, cm.page_url) == ("ok", "RUB", "https://www.aricapital.ru/funds/fund-cm")
    seg = cm.segments[0]
    assert seg.page_url == "https://www.aricapital.ru/disclosure/archive.html?type=navprice:fund-cm"
    assert len(seg.points) == 23 and seg.points[0].date == date(2026, 10, 1)   # точки 2013 г. отрезаны окном
    assert {r.period for r in cm.official_returns} == {"1m", "3m", "6m", "1y"}
    assert cm.warnings == []

    assert res["3542"].status == "ok" and res["3542"].segments[0].points[-1].price == 2899.33
    ri = res["6022"]
    assert ri.status == "ok" and "registrationNumber=5902" in ri.warnings[0]
    assert res["3208"].status == "unavailable"               # страницы нет в фейковом http
    assert res["9999"].status == "not_found"
    assert res["9998"].status == "not_formed"
    assert res["1234"].status == "not_found" and "6022" in res["1234"].reason   # название то, номер правил — нет

    # список фондов скачан один раз; каждый запрос помечен своими фондами
    assert [u for u, _ in http.calls].count(LIST_URL) == 1
    assert (FUND_URL.format(site_id="fund-cm"), ["2565"]) in http.calls


def test_collect_list_unavailable():
    funds = [fund(r, REGISTRY[r]) for r in ("2565", "6022")]
    res = AricapitalAdapter().collect(ctx_with(FakeHttp({})), funds)
    assert [fd.status for fd in res] == ["unavailable", "unavailable"]
