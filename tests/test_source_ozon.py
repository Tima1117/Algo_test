"""ОЗОН Управление Активами (ozon): разбор настоящих ответов JSON-API ozon-am.ru (снято 03.11.2026), без сети.

Контрольные значения сверены со страницами сайта:
- «Статистика → СЧА» (https://ozon-am.ru/statistics): СЧА и пай на 30.10.2026 и 02.11.2026;
- «Статистика → Отклонение» (https://ozon-am.ru/statistics/deviation): «Цена пая T-20» на 05.10.2026;
- страницы фондов https://ozon-am.ru/general/mutual-funds/<тикер>: «Правила доверительного управления № …».
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path

import pytest

from pif.http import Blocked, FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.ozon import (DEVIATION_API, DOCS_API, FUNDS_API, STATS_API, OzonAdapter, SiteFund,
                              _name_key, match_fund, parse_deviation, parse_documents, parse_fund_list,
                              parse_statistics)

FIX = Path(__file__).parent / "fixtures" / "ozon"

# номер правил -> (название в реестре, тикер на сайте)
FUNDS = {
    "7797": ("Озон Денежный рынок", "OLIQA"),
    "7798": ("Акции индекса Московской Биржи", "TOPSA"),
    "7799": ("Корпоративные рублевые облигации РФ", "CORPA"),
    "7800": ("Корпоративные валютные облигации РФ", "RUFOA"),
    "7801": ("Российские государственные облигации", "OFZSA"),
}


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def load(name: str):
    return json.loads(read(name))


def make_fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type="exchange", category="", status="Сформирован",
                reg_date="2026-05-14", formation_end=None, uk_name="", uk_short="ОЗОН", uk_ogrn="",
                uk_inn="9703217363", uk_site="http://ozon-am.ru/", exchange_tradable=True)


# ---------- список фондов ----------

def test_fund_list():
    sf = parse_fund_list(read("mutual-funds.json"))
    assert [f.ticker for f in sf] == ["OLIQA", "RUFOA", "TOPSA", "CORPA", "OFZSA", "X1OZNSLR"]
    assert sf[0] == SiteFund("2", "БПИФ РФИ «Озон Денежный рынок»", "OLIQA")


def test_fund_list_broken():
    data = load("mutual-funds.json")
    del data["data"][2]["ticker"]
    with pytest.raises(UnknownFormat):
        parse_fund_list(data)
    with pytest.raises(UnknownFormat):
        parse_fund_list({"data": []})
    with pytest.raises(UnknownFormat):
        parse_fund_list({"items": load("mutual-funds.json")["data"]})
    with pytest.raises(UnknownFormat):
        parse_fund_list(b"<!doctype html><html></html>")


# ---------- документы фонда: номер правил, КИД, справки о СЧА ----------

@pytest.mark.parametrize("rules,ticker", [(r, t) for r, (_, t) in FUNDS.items()])
def test_documents_rules_number(rules, ticker):
    assert parse_documents(read(f"documents_{ticker}.json")).rules == {rules}


def test_documents_reports():
    docs = parse_documents(read("documents_OLIQA.json"))
    assert [d for d, _ in docs.nav_reports] == [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31),
                                               date(2026, 9, 30)]
    assert len(docs.kid) == 7
    as_of, link = docs.kid[-1]
    assert as_of == date(2026, 9, 30)
    assert link.startswith("https://cdn1.ozonusercontent.com/") and link.endswith("30.09.2026.pdf")
    # Фонд из списка без документов: настоящий ответ {"data":[]} — не ошибка
    empty = parse_documents(read("documents_X1OZNSLR.json"))
    assert (empty.rules, empty.kid, empty.nav_reports) == (set(), [], [])


def test_documents_broken():
    data = load("documents_OLIQA.json")
    del data["data"][0]["items"][0]["documentName"]
    with pytest.raises(UnknownFormat):
        parse_documents(data)
    data = load("documents_OLIQA.json")
    del data["data"][1]["items"]
    with pytest.raises(UnknownFormat):
        parse_documents(data)
    with pytest.raises(UnknownFormat):
        parse_documents({"documents": []})


# ---------- таблица «Статистика → СЧА» ----------

def test_statistics_values():
    st = parse_statistics(read("statistics.json"))
    assert len(st) == 5
    oliq = st["БПИФ РФИ «Озон Денежный рынок»"]
    assert oliq.currency == "RUB"
    assert [(p.date, p.nav, p.price) for p in oliq.points] == [
        (date(2026, 10, 30), 52353994.53, 104.7080),
        (date(2026, 11, 2), 52414080.98, 104.8282),
    ]
    # СЧА в рублях, а не в тысячах: 500 000 паёв × 104,8282
    assert round(oliq.points[-1].nav / oliq.points[-1].price) == 500000
    tops = st["БПИФ РФИ «Акции индекса Московской Биржи»"]
    assert [(p.date, p.nav, p.price) for p in tops.points] == [
        (date(2026, 10, 30), 30070651.11, 117.9241),
        (date(2026, 11, 2), 29501813.92, 115.6934),
    ]
    rufo = st["БПИФ РФИ «Корпоративные валютные облигации РФ»"]
    assert rufo.currency == "RUB"      # валютный по активам, пай — в рублях
    assert (rufo.points[-1].nav, rufo.points[-1].price) == (27389527.43, 107.4099)


def _stats_with(**changes):
    data = load("statistics.json")
    data["fundStats"][0].update(changes)
    return data


def test_statistics_broken():
    data = load("statistics.json")
    del data["fundStats"][1]["lastSharePrice"]
    with pytest.raises(UnknownFormat):
        parse_statistics(data)
    with pytest.raises(UnknownFormat):          # нулевая цена пая не пропускается молча
        parse_statistics(_stats_with(lastSharePrice="0"))
    with pytest.raises(UnknownFormat):
        parse_statistics(_stats_with(prevNetAssetValue="-5"))
    with pytest.raises(UnknownFormat):
        parse_statistics(_stats_with(currency="XXX"))
    with pytest.raises(UnknownFormat):
        parse_statistics(_stats_with(lastStatDate="02.11.2026 вечером"))
    with pytest.raises(UnknownFormat):          # предыдущий день не раньше последнего
        parse_statistics(_stats_with(prevStatDate="2026-11-02 00:00:00 +0000 UTC"))
    with pytest.raises(UnknownFormat):
        parse_statistics(_stats_with(lastSharePrice="н/д x"))
    data = load("statistics.json")
    data["fundStats"].append(data["fundStats"][0])
    with pytest.raises(UnknownFormat):          # один фонд дважды
        parse_statistics(data)
    with pytest.raises(UnknownFormat):
        parse_statistics({"fundStats": []})


def test_statistics_empty_previous_day():
    # сайт показывает «—», если значения нет: такая точка пропускается, а не превращается в ноль
    st = parse_statistics(_stats_with(prevStatDate="", prevNetAssetValue="", prevSharePrice=""))
    pts = st["БПИФ РФИ «Акции индекса Московской Биржи»"].points
    assert [(p.date, p.nav, p.price) for p in pts] == [(date(2026, 11, 2), 29501813.92, 115.6934)]
    with pytest.raises(UnknownFormat):          # значения без даты
        parse_statistics(_stats_with(prevStatDate=""))


# ---------- таблица «Отклонение»: цена пая T-20 ----------

def test_deviation_values():
    dev = parse_deviation(read("deviation.json"))
    assert len(dev) == 5
    tops = dev["БПИФ РФИ «Акции индекса Московской Биржи»"]
    assert [(p.date, p.nav, p.price) for p in tops] == [
        (date(2026, 10, 5), None, 109.0026),
        (date(2026, 11, 2), None, 115.6934),
    ]
    assert dev["БПИФ РФИ «Озон Денежный рынок»"][0].price == 103.7718


def test_deviation_broken():
    data = load("deviation.json")
    del data["funds"][0]["sharePrice20"]
    with pytest.raises(UnknownFormat):
        parse_deviation(data)
    data = load("deviation.json")
    data["funds"][0]["date20"] = data["funds"][0]["lastDate"]
    with pytest.raises(UnknownFormat):
        parse_deviation(data)
    with pytest.raises(UnknownFormat):
        parse_deviation({"fundStats": []})


# ---------- сопоставление ----------

def test_name_key():
    assert _name_key("БПИФ РФИ «Озон Денежный рынок»") == _name_key("Озон Денежный рынок")
    assert _name_key("БПИФ РФИ «Корпоративные рублевые облигации РФ»") != _name_key(
        "Корпоративные валютные облигации РФ")


def _docs_for(sf: SiteFund):
    return parse_documents(read(f"documents_{sf.ticker}.json"))


def test_match_fund_by_rules_number():
    site = parse_fund_list(read("mutual-funds.json"))
    for rules, (name, ticker) in FUNDS.items():
        sf, docs, how = match_fund(make_fund(rules, name), site, _docs_for)
        assert (sf.ticker, how) == (ticker, "по номеру правил")
    # название в реестре другое, но номер правил на сайте есть — находим по номеру
    sf, _, how = match_fund(make_fund("7799", "Облигации компаний"), site, _docs_for)
    assert (sf.ticker, how) == ("CORPA", "по номеру правил")
    # то же название, но чужой номер правил — это не наш фонд
    sf, _, _ = match_fund(make_fund("9999", "Озон Денежный рынок"), site, _docs_for)
    assert sf is None


# ---------- collect целиком на фикстурах ----------

class FakeHttp:
    """Вместо сети — файлы фикстур; errors: url -> исключение."""

    def __init__(self, errors: dict | None = None):
        self.errors = errors or {}
        self.calls: list[tuple[str, dict | None, list | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw):
        self.calls.append((url, params, funds))
        if url in self.errors:
            raise self.errors[url]
        name = {FUNDS_API: "mutual-funds.json", STATS_API: "statistics.json",
                DEVIATION_API: "deviation.json"}.get(url)
        if url == DOCS_API:
            name = f"documents_{params['ticker']}.json"
        path = FIX / name
        full = url + (f"?ticker={params['ticker']}" if params else "")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def run(http: FakeHttp, funds: list[Fund]):
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))
    return {fd.rules: fd for fd in OzonAdapter().collect(ctx, funds)}


def test_collect_end_to_end():
    funds = [make_fund(r, n) for r, (n, _) in FUNDS.items()] + [make_fund("0000-00000000", "Нет такого фонда")]
    http = FakeHttp()
    res = run(http, funds)
    for rules, (_, ticker) in FUNDS.items():
        fd = res[rules]
        assert fd.status == "partial", fd.reason
        assert "только за два последних рабочих дня" in fd.reason and "PDF-справки" in fd.reason
        assert fd.currency == "RUB"
        assert fd.page_url == f"https://ozon-am.ru/general/mutual-funds/{ticker}"
        assert [s.raw_url for s in fd.segments] == [STATS_API, DEVIATION_API]
        assert [s.page_url for s in fd.segments] == ["https://ozon-am.ru/statistics",
                                                     "https://ozon-am.ru/statistics/deviation"]
        assert fd.official_returns == [] and "КИД" in fd.warnings[0] and "30.09.2026" in fd.warnings[0]
        assert fd.errors == []
    oliq = res["7797"]
    assert [(p.date, p.nav, p.price) for p in oliq.segments[0].points][-1] == (date(2026, 11, 2), 52414080.98,
                                                                                104.8282)
    assert "4 шт. с 30.06.2026 по 30.09.2026" in oliq.reason
    assert res["0000-00000000"].status == "not_found"
    # общие запросы — по одному на прогон, документы — по одному на фонд сайта
    urls = [(u, (p or {}).get("ticker")) for u, p, _ in http.calls]
    assert len(urls) == len(set(urls))
    assert sum(1 for u, _ in urls if u == DOCS_API) == 6


def test_collect_site_unavailable():
    funds = [make_fund(r, n) for r, (n, _) in FUNDS.items()]
    res = run(FakeHttp({FUNDS_API: FetchError(FUNDS_API, "сайт не отвечает (ConnectTimeout)")}), funds)
    assert {fd.status for fd in res.values()} == {"unavailable"}
    res = run(FakeHttp({STATS_API: Blocked(STATS_API, "доступ закрыт сайтом (HTTP 403)", 403)}), funds)
    assert {fd.status for fd in res.values()} == {"blocked"}


def test_collect_deviation_failure_is_only_warning():
    funds = [make_fund("7798", FUNDS["7798"][0])]
    res = run(FakeHttp({DEVIATION_API: FetchError(DEVIATION_API, "HTTP 502", 502)}), funds)
    fd = res["7798"]
    assert fd.status == "partial"
    assert [s.raw_url for s in fd.segments] == [STATS_API]
    assert any("отклонения" in w and "HTTP 502" in w for w in fd.warnings)
