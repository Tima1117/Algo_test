"""Адаптер АО «Контрада Капитал»: разбор настоящих (урезанных) ответов сайта, без сети.

Фикстуры tests/fixtures/contrada/:
- pages_urls.json — /client/pages/urls целиком;
- inform_<тип>_<slug>.json — /client/pages/<id> страниц раскрытия: таблица СЧА урезана до
  нескольких дат, из документов оставлен только блок КИД (2 последних);
- products_<тип>_<slug>.json — /client/pages/<id> витрин: точки графика урезаны, виджет целиком;
- kid_*.txt — текст КИД (PDF с сайта), извлечённый pypdf: PDF по 450+ КБ в фикстуры не влезают,
  а pypdf нет в зависимостях проекта.
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
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    m = by_date(pts)
    # как на странице: «02.11.2026 | 484 606 124,85 | -0,21 | 170,97 | -0,20»
    assert m[date(2026, 11, 2)].nav == 484606124.85 and m[date(2026, 11, 2)].price == 170.97
    assert m[date(2026, 9, 30)].nav == 475363632.76 and m[date(2026, 9, 30)].price == 167.72
    assert m[date(2025, 9, 1)].price is not None
    assert pts[0].date == date(2021, 12, 22)          # история с даты формирования


def test_history_bpif_table_with_benchmark_column():
    m = by_date(c.parse_history(load("inform_bpif_powerliquidity.json")))
    assert m[date(2026, 11, 2)].nav == 725111934.34 and m[date(2026, 11, 2)].price == 114.23
    assert min(m) == date(2025, 10, 23)                 # дата завершения формирования


def test_history_interval_fund_is_monthly():
    m = by_date(c.parse_history(load("inform_ipif_HighBonds.json")))
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
    assert c.kid_as_of(info, pts) == date(2026, 9, 30)


def test_latest_kid():
    kid = c.latest_kid(load("inform_opif_sila-balansa.json"))
    assert kid.date == date(2026, 9, 30)
    assert kid.url.endswith("/docs-pyrus/КИД ОПИФ СБ_30.09.2026.pdf")


# ---------------------------------------------------------------- сбор целиком (без сети)

class FakeHttp:
    """Отдаёт фикстуры вместо сайта; запоминает запросы."""

    def __init__(self, fail_ids=(), pdf_path: Path | None = None):
        urls = load("pages_urls.json")["data"]
        self.files = {c.URLS_URL: FIX / "pages_urls.json"}
        for row in urls:
            name = row["url"].strip("/").replace("/", "_")
            for prefix, fx in (("inform_pif_", "inform_"), ("products_", "products_")):
                if name.startswith(prefix) and (FIX / f"{fx}{name[len(prefix):]}.json").exists():
                    self.files[c.PAGE_URL.format(id=row["id"])] = FIX / f"{fx}{name[len(prefix):]}.json"
        self.fail = {c.PAGE_URL.format(id=i) for i in fail_ids}
        self.pdf_path = pdf_path
        self.calls: list[tuple[str, tuple]] = []

    def get(self, url, *, fmt="html", funds=None, **kw):
        self.calls.append((url, tuple(funds or ())))
        if url in self.fail:
            raise FetchError(url, "сайт не отвечает (ReadTimeout)")
        path = self.pdf_path if fmt == "pdf" else self.files.get(url)
        if path is None:
            raise FetchError(url, "HTTP 404", 404)
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def fund(rules: str, status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=f"фонд {rules}", full_name="", type="open", category="", status=status,
                reg_date="", formation_end=None, uk_name="", uk_short="АО «Контрада Капитал»",
                uk_ogrn="", uk_inn="9703206844", uk_site="", exchange_tradable=False)


def ctx_for(http) -> Context:
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))


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
        assert pts and min(p.date for p in pts) >= date(2025, 9, 1)
        assert fd.page_url == f"https://contrada-capital.ru/inform/pif/{slug.replace('_', '/', 1)}"
        assert any("pypdf" in w for w in fd.warnings)
    sb = res["4693"]
    assert by_date(sb.segments[0].points)[date(2026, 11, 2)].price == 170.97
    assert [(o.period, o.value_pct, o.as_of) for o in sb.official_returns] == [("1y", 6.09, date(2026, 11, 2))]
    assert sb.official_returns[0].source_url == "https://contrada-capital.ru/products/opif/sila-balansa"
    assert res["7499"].official_returns == []           # фонду меньше года
    # страницы фондов привязаны к своему фонду в манифесте
    assert any(url.endswith("9acb8ff5-a0d6-4a3b-b507-7bc44637e715") and fs == ("4693",) for url, fs in http.calls)


def test_collect_uses_kid_when_pypdf_available(monkeypatch):
    monkeypatch.setattr(c, "has_pypdf", lambda: True)
    monkeypatch.setattr(c, "kid_pdf_text", lambda b: kid_text("kid_sila-balansa_2026-09-30.txt"))
    http = FakeHttp(pdf_path=FIX / "kid_sila-balansa_2026-09-30.txt")
    (fd,) = c.ContradaAdapter().collect(ctx_for(http), [fund("4693")])
    got = {o.period: (o.value_pct, o.as_of) for o in fd.official_returns}
    assert got == {"1m": (-3.05, date(2026, 9, 30)), "3m": (-1.44, date(2026, 9, 30)),
                   "6m": (-2.86, date(2026, 9, 30)), "1y": (4.92, date(2026, 9, 30))}
    assert fd.official_returns[0].source_url.endswith("КИД ОПИФ СБ_30.09.2026.pdf")
    assert not fd.warnings


def test_collect_one_page_down_does_not_break_others(monkeypatch):
    monkeypatch.setattr(c, "has_pypdf", lambda: False)
    # страница раскрытия «Силы Баланса» не отвечает
    http = FakeHttp(fail_ids=["9acb8ff5-a0d6-4a3b-b507-7bc44637e715"])
    res = {fd.rules: fd for fd in c.ContradaAdapter().collect(ctx_for(http), [fund("4693"), fund("5712")])}
    assert res["4693"].status == "unavailable"
    assert res["5712"].status == "ok"
