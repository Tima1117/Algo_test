"""Адаптер АО «Контрада Капитал»: разбор настоящих (урезанных) ответов сайта, без сети.

Фикстуры tests/fixtures/contrada/:
- pages_urls.json — /client/pages/urls целиком;
- inform_<тип>_<slug>.json — /client/pages/<id> страниц раскрытия: таблица СЧА урезана до
  нескольких дат, из документов оставлен только блок КИД (2 последних);
- products_<тип>_<slug>.json — /client/pages/<id> витрин: точки графика урезаны, виджет целиком;
- kid_*.txt, payout_*.txt — текст КИД и «Сообщений о выплате дохода» (PDF с сайта), извлечённый
  pypdf: PDF по 400+ КБ в фикстуры не влезают, а pypdf нет в зависимостях проекта.
Контрольные значения сверены со страницами https://contrada-capital.ru/products/opif/sila-balansa
(таблица «Стоимость чистых активов и расчетная стоимость пая», виджет «Пай … За 1 год +6,09%»).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import contrada as c
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "contrada"

SLUG_RULES = {
    "opif_sila-balansa": "4693",
    "opif_sila-dohoda": "5712",
    "opif_marketpower": "5900",
    "opif_powercurrency": "7199",
    "bpif_powerliquidity": "7307",
    "ipif_HighBonds": "5427",
    "ipif_KontradaBonds": "7499",
}


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def kid_text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def by_date(points):
    return {p.date: p for p in points}


# ---------------------------------------------------------------- история СЧА и пая

def test_history_sila_balansa():
    pts = c.parse_history((FIX / "inform_opif_sila-balansa.json").read_bytes())
    assert_series(pts, "contrada_history_opif_sila-balansa")
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    m = by_date(pts)
    # как на странице: «02.11.2026 | 484 606 124,85 | -0,21 | 170,97 | -0,20»
    assert m[date(2026, 11, 2)].nav == 484606124.85 and m[date(2026, 11, 2)].price == 170.97
    assert m[date(2026, 9, 30)].nav == 475363632.76 and m[date(2026, 9, 30)].price == 167.72
    assert m[date(2025, 9, 1)].price is not None
    assert pts[0].date == date(2021, 12, 22)          # история с даты формирования


def test_history_bpif_table_with_benchmark_column():
    m = by_date(c.parse_history(load("inform_bpif_powerliquidity.json")))
    assert_series(c.parse_history(load("inform_bpif_powerliquidity.json")), "contrada_history_bpif_powerliquidity")
    assert m[date(2026, 11, 2)].nav == 725111934.34 and m[date(2026, 11, 2)].price == 114.23
    assert min(m) == date(2025, 10, 23)                 # дата завершения формирования


def test_history_interval_fund_is_monthly():
    m = by_date(c.parse_history(load("inform_ipif_HighBonds.json")))
    assert_series(c.parse_history(load("inform_ipif_HighBonds.json")), "contrada_history_ipif_HighBonds")
    assert m[date(2026, 10, 30)].nav == 20675033815.75 and m[date(2026, 10, 30)].price == 1710.35
    assert m[date(2026, 9, 30)].price == 1686.51


@pytest.mark.parametrize("slug,rules", sorted(SLUG_RULES.items()))
def test_rules_from_about_block(slug, rules):
    assert c.parse_rules(load(f"inform_{slug}.json")) == rules


def test_broken_history_raises():
    page = load("inform_opif_sila-balansa.json")
    bad = copy.deepcopy(page)
    row = next(iter(bad["data"]["tables"][0]["table"].values()))
    del row["piePrice"]
    with pytest.raises(UnknownFormat):
        c.parse_history(bad)

    bad = copy.deepcopy(page)
    bad["data"]["tables"] = []
    with pytest.raises(UnknownFormat):
        c.parse_history(bad)

    bad = copy.deepcopy(page)
    tbl = bad["data"]["tables"][0]["table"]
    key = next(iter(tbl))
    tbl[key]["date"] = "31.12.1999"
    with pytest.raises(UnknownFormat):
        c.parse_history(bad)

    with pytest.raises(UnknownFormat):
        c.parse_history(b"<html>not json</html>")


# ---------------------------------------------------------------- доходность

def test_widget_one_year_return():
    url = "https://contrada-capital.ru/products/opif/sila-balansa"
    (r,) = c.parse_widget_returns(load("products_opif_sila-balansa.json"), url)
    assert (r.period, r.value_pct, r.as_of, r.source_url) == ("1y", 6.09, date(2026, 11, 2), url)
    # интервальный: последняя цена 30.10.2026
    (r,) = c.parse_widget_returns(load("products_ipif_HighBonds.json"), "u")
    assert (r.value_pct, r.as_of) == (22.26, date(2026, 10, 30))
    # фонду меньше года — «-»
    assert c.parse_widget_returns(load("products_ipif_KontradaBonds.json"), "u") == []


def test_broken_widget_raises():
    page = load("products_opif_sila-balansa.json")
    bad = copy.deepcopy(page)
    for row in bad["data"]["graphs"][0]["change_price_data"]["rows"]:
        row["price_by_periods"].pop("year_1")
    with pytest.raises(UnknownFormat):
        c.parse_widget_returns(bad, "u")
    bad = copy.deepcopy(page)
    bad["data"]["graphs"][0]["change_price_data"]["rows"] = [
        r for r in bad["data"]["graphs"][0]["change_price_data"]["rows"] if r["type"] != "pai"]
    with pytest.raises(UnknownFormat):
        c.parse_widget_returns(bad, "u")


def test_kid_returns_section():
    info = c.parse_kid_text(kid_text("kid_sila-balansa_2026-09-30.txt"))
    assert info.date == date(2026, 9, 30)
    assert info.returns == {"1m": -3.05, "3m": -1.44, "6m": -2.86, "1y": 4.92}
    assert (info.price, info.nav, info.pays_income) == (167.72, 475363632.76, False)

    # фонд с выплатой дохода и колонкой «отклонение от индикатора»
    info = c.parse_kid_text(kid_text("kid_sila-dohoda_2026-10-23.txt"))
    assert info.date == date(2026, 10, 23)
    assert info.returns == {"1m": -0.36, "3m": 2.03, "6m": 4.43, "1y": 12.97}
    assert (info.price, info.nav, info.pays_income) == (97.49, 641241867.11, True)

    # фонду меньше года — ячейка «1 год» пустая
    info = c.parse_kid_text(kid_text("kid_kontradabonds_2026-09-30.txt"))
    assert info.returns == {"1m": 0.48, "3m": 3.58, "6m": 8.09, "1y": None}
    assert info.price == 1138.85 and info.nav == 14956392077.62


def test_broken_kid_raises():
    text = kid_text("kid_sila-balansa_2026-09-30.txt")
    with pytest.raises(UnknownFormat):
        c.parse_kid_text(text.replace("6 месяцев", "полгода"))
    with pytest.raises(UnknownFormat):
        c.parse_kid_text(text.replace("Доходность за период", "Результаты"))


def test_kid_as_of_is_month_end_not_document_date():
    # КИД «по состоянию на 23.10.2026» — пай и СЧА на 30.09.2026
    info = c.parse_kid_text(kid_text("kid_sila-dohoda_2026-10-23.txt"))
    pts = c.parse_history(load("inform_opif_sila-dohoda.json"))
    assert_series(pts, "contrada_history_opif_sila-dohoda")
    assert c.kid_as_of(info, pts) == date(2026, 9, 30)


def test_latest_kid():
    kid = c.latest_kid(load("inform_opif_sila-balansa.json"))
    assert kid.date == date(2026, 9, 30)
    assert kid.url.endswith("/docs-pyrus/КИД ОПИФ СБ_30.09.2026.pdf")


# ---------------------------------------------------------------- выплаты дохода («Сила Дохода»)

SD_PRODUCT = "https://contrada-capital.ru/products/opif/sila-dohoda"


def test_payouts_from_graph():
    pays = c.parse_payouts(load("products_opif_sila-dohoda.json"), SD_PRODUCT)
    got = {p.date: p.amount for p in pays}
    # суммы совпадают с «Сообщениями о выплате дохода» за соответствующие кварталы
    assert got[date(2025, 9, 30)] == 2.8903765526
    assert got[date(2025, 12, 30)] == 4.6488841665
    assert got[date(2026, 3, 31)] == 3.4558789341
    assert got[date(2026, 6, 30)] == 4.5291892165
    assert date(2026, 9, 30) not in got            # график ещё не обновлён (сообщение от 02.11.2026)
    assert all(p.source_url == SD_PRODUCT for p in pays)
    assert c.pays_income_graph(load("products_opif_sila-dohoda.json"))
    # фонды без выплат
    for slug in SLUG_RULES:
        if slug != "opif_sila-dohoda":
            assert c.parse_payouts(load(f"products_{slug}.json"), "u") == []
            assert not c.pays_income_graph(load(f"products_{slug}.json"))


def test_broken_payouts_raise():
    page = load("products_opif_sila-dohoda.json")
    bad = copy.deepcopy(page)
    pt = next(x for x in bad["data"]["graphs"][0]["primary"]["points"] if x["payout"])
    del pt["payoutValue"]
    with pytest.raises(UnknownFormat):
        c.parse_payouts(bad, "u")
    bad = copy.deepcopy(page)
    pt = next(x for x in bad["data"]["graphs"][0]["primary"]["points"] if x["payout"])
    pt["payoutValue"] = 0
    with pytest.raises(UnknownFormat):
        c.parse_payouts(bad, "u")


def test_payout_messages_list():
    msgs = c.payout_messages(load("inform_opif_sila-dohoda.json"))
    by_q = {m.quarter_end: m for m in msgs}
    assert by_q[date(2026, 9, 30)].url.endswith("/2026/11/02/b6ebeafb-ca23-460b-a24c-5e4a3bf83634.pdf")
    # скорректированное сообщение за 1-й квартал 2026 заменяет исходное
    assert "скорректирован" in by_q[date(2026, 3, 31)].title
    assert len(msgs) == len(by_q)
    assert c.payout_messages(load("inform_opif_sila-balansa.json")) == []


def test_payout_message_text_both_templates():
    p = c.parse_payout_message_text(kid_text("payout_sila-dohoda_q3-2026.txt"), "5712", "u")
    assert (p.date, p.amount) == (date(2026, 9, 30), 3.3194188717472)
    p = c.parse_payout_message_text(kid_text("payout_sila-dohoda_q3-2025.txt"), "5712", "u")
    assert (p.date, p.amount) == (date(2025, 9, 30), 2.8903765526)


def test_broken_payout_message_raises():
    text = kid_text("payout_sila-dohoda_q3-2026.txt")
    with pytest.raises(UnknownFormat):
        c.parse_payout_message_text(text, "4693", "u")                       # чужие правила
    with pytest.raises(UnknownFormat):
        c.parse_payout_message_text(text.replace("3,3194188717472", "—"), "5712", "u")
    with pytest.raises(UnknownFormat):
        c.parse_payout_message_text(text.replace("Дата, на которую", "Дата"), "5712", "u")


# ---------------------------------------------------------------- сбор целиком (без сети)

class FakeHttp:
    """Отдаёт фикстуры вместо сайта; PDF — текстом из фикстур (pdf_text в тестах подменён)."""

    def __init__(self, fail_ids=(), pdfs: dict[str, str] | None = None):
        urls = load("pages_urls.json")["data"]
        self.files = {c.URLS_URL: FIX / "pages_urls.json"}
        for row in urls:
            name = row["url"].strip("/").replace("/", "_")
            for prefix, fx in (("inform_pif_", "inform_"), ("products_", "products_")):
                if name.startswith(prefix) and (FIX / f"{fx}{name[len(prefix):]}.json").exists():
                    self.files[c.PAGE_URL.format(id=row["id"])] = FIX / f"{fx}{name[len(prefix):]}.json"
        for url, fixture in (pdfs or {}).items():
            self.files[url] = FIX / fixture
        self.fail = {c.PAGE_URL.format(id=i) for i in fail_ids}
        self.calls: list[tuple[str, tuple]] = []

    def get(self, url, *, fmt="html", funds=None, **kw):
        self.calls.append((url, tuple(funds or ())))
        if url in self.fail:
            raise FetchError(url, "сайт не отвечает (ReadTimeout)")
        path = self.files.get(url)
        if path is None:
            raise FetchError(url, "HTTP 404", 404)
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def fund(rules: str, status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=f"фонд {rules}", full_name="", type="open", category="", status=status,
                reg_date="", formation_end=None, uk_name="", uk_short="АО «Контрада Капитал»",
                uk_ogrn="", uk_inn="9703206844", uk_site="", exchange_tradable=False)


def ctx_for(http) -> Context:
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))


def with_pypdf(monkeypatch):
    monkeypatch.setattr(c, "has_pypdf", lambda: True)
    monkeypatch.setattr(c, "pdf_text", lambda b: b.decode("utf-8"))


def test_collect_matches_by_rules(monkeypatch):
    monkeypatch.setattr(c, "has_pypdf", lambda: False)
    http = FakeHttp()
    rules = sorted(SLUG_RULES.values()) + ["9999"]
    res = {fd.rules: fd for fd in c.ContradaAdapter().collect(ctx_for(http), [fund(r) for r in rules])}
    assert res["9999"].status == "not_found"
    for slug, r in SLUG_RULES.items():
        fd = res[r]
        assert fd.status == "ok", (r, fd.reason)
        assert fd.currency == "RUB"
        pts = fd.segments[0].points
        assert_series(pts, f"contrada_collect_{slug}")
        assert pts and min(p.date for p in pts) >= date(2025, 9, 1)
        assert fd.page_url == f"https://contrada-capital.ru/inform/pif/{slug.replace('_', '/', 1)}"
        assert any("pypdf" in w for w in fd.warnings)
        assert (fd.payouts != []) == (r == "5712")       # доход платит только «Сила Дохода»
    sb = res["4693"]
    assert by_date(sb.segments[0].points)[date(2026, 11, 2)].price == 170.97
    assert [(o.period, o.value_pct, o.as_of) for o in sb.official_returns] == [("1y", 6.09, date(2026, 11, 2))]
    assert sb.official_returns[0].source_url == "https://contrada-capital.ru/products/opif/sila-balansa"
    assert res["7499"].official_returns == []           # фонду меньше года
    # интервальный: виджет считает «За 1 год» от 30.09.2025 — предупреждаем
    assert any("от 30.09.2025" in w for w in res["5427"].warnings)
    # «Сила Дохода»: выплаты с графика с 01.09.2025, виджет без выплат не берём, 3-й кв. 2026 — только PDF
    sd = res["5712"]
    assert [(p.date, p.amount) for p in sd.payouts] == [
        (date(2025, 9, 30), 2.8903765526), (date(2025, 12, 30), 4.6488841665),
        (date(2026, 3, 31), 3.4558789341), (date(2026, 6, 30), 4.5291892165)]
    assert sd.official_returns == []
    assert any("3-й квартал 2026" in w for w in sd.warnings)
    # страницы фондов привязаны к своему фонду в манифесте
    assert any(url.endswith("9acb8ff5-a0d6-4a3b-b507-7bc44637e715") and fs == ("4693",) for url, fs in http.calls)


def test_collect_uses_kid_when_pypdf_available(monkeypatch):
    with_pypdf(monkeypatch)
    kid = "https://storage.contrada-capital.ru/contrada-prod/docs-pyrus/КИД ОПИФ СБ_30.09.2026.pdf"
    (fd,) = c.ContradaAdapter().collect(ctx_for(FakeHttp(pdfs={kid: "kid_sila-balansa_2026-09-30.txt"})),
                                        [fund("4693")])
    got = {o.period: (o.value_pct, o.as_of) for o in fd.official_returns}
    assert got == {"1m": (-3.05, date(2026, 9, 30)), "3m": (-1.44, date(2026, 9, 30)),
                   "6m": (-2.86, date(2026, 9, 30)), "1y": (4.92, date(2026, 9, 30))}
    assert fd.official_returns[0].source_url == kid
    assert not fd.warnings


def test_sila_dohoda_kid_matches_series_with_payouts(monkeypatch):
    """КИД «Силы Дохода» (с выплатами) сходится с рядом + выплатами по формуле сборки."""
    from pif.normalize import normalize
    from pif.returns import compare_official

    with_pypdf(monkeypatch)
    base = "https://storage.contrada-capital.ru/contrada-prod/"
    http = FakeHttp(pdfs={base + "docs-pyrus/КИД ОПИФ СД_23.10.2026.pdf": "kid_sila-dohoda_2026-10-23.txt",
                          base + "documents/2026/11/02/b6ebeafb-ca23-460b-a24c-5e4a3bf83634.pdf":
                              "payout_sila-dohoda_q3-2026.txt"})
    (fd,) = c.ContradaAdapter().collect(ctx_for(http), [fund("5712")])
    assert fd.payouts[-1].date == date(2026, 9, 30) and fd.payouts[-1].amount == 3.3194188717472
    assert fd.payouts[-1].source_url.endswith("b6ebeafb-ca23-460b-a24c-5e4a3bf83634.pdf")
    seg = {"raw_url": "x", "sha256": "x", "fetched_at": "x", "page_url": None, "stale": False,
           "points": [{"date": p.date.isoformat(), "nav": p.nav, "price": p.price} for p in fd.segments[0].points]}
    pts = normalize("5712", [seg]).points
    official = [{"period": o.period, "value_pct": o.value_pct, "as_of": o.as_of.isoformat(),
                 "source_url": o.source_url} for o in fd.official_returns]
    payouts = [{"date": p.date.isoformat(), "amount": p.amount} for p in fd.payouts]
    checks, bad = compare_official("5712", pts, official, payouts)
    assert {ch["period"] for ch in checks} == {"1m", "3m", "6m", "1y"}
    assert bad == [], checks
    # без выплаты за 3-й квартал 2026 (только в PDF) год не сходится: 9,66 против 12,97
    _, bad = compare_official("5712", pts, official, payouts[:-1])
    assert {b["period"] for b in bad} >= {"1y"}


def test_collect_one_page_down_does_not_break_others(monkeypatch):
    monkeypatch.setattr(c, "has_pypdf", lambda: False)
    # страница раскрытия «Силы Баланса» не отвечает
    http = FakeHttp(fail_ids=["9acb8ff5-a0d6-4a3b-b507-7bc44637e715"])
    res = {fd.rules: fd for fd in c.ContradaAdapter().collect(ctx_for(http), [fund("4693"), fund("5712")])}
    assert res["4693"].status == "unavailable"
    assert res["5712"].status == "ok"
