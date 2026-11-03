"""ТКБ Инвестмент Партнерс (tkbip): разбор настоящих ответов www.tkbip.ru (снято 03.11.2026), без сети.

Контрольные значения сверены со страницами сайта:
- «Сообщение о СЧА и расчетной стоимости пая по ОПИФ» (/funds/dnm/) на 02.11.2026 и 30.10.2026;
- «Результаты работы паевых фондов» (/funds/) и подвал сайта: прирост пая на 31.10.2026.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.tkbip import (BASE, CATALOG_URL, TkbipAdapter, norm_rules, parse_catalog,
                               parse_history, parse_returns, parse_rules)

FIX = Path(__file__).parent / "fixtures" / "tkbip"

SLUGS = {   # номер правил -> slug страницы фонда (сверено со страницами «Информация о фонде»)
    "0081-58233855": "tkb-bnp-pariba-fond-obligatsiy",
    "0078-58234010": "tkb-bnp-pariba-fond-sbalansirovannyy-konservativnyy",   # «Фонд сбалансированный»
    "0096-58227323": "tkb-bnp-pariba-globalnye-investitsii",   # «…сбалансированный с выплатой дохода»
    "0478-75408434": "tkb-bnp-pariba-premium-fond-aktsiy",
    "0219-14281681": "opif-aktsiy-agana-ekstrim",               # «Перспектива»
    "0991-94131990": "tkb-bnp-pariba-fond-valyutnykh-obligatsiy",
    "2026-94198244": "tkb-bnp-pariba-zoloto",
}


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


# ---------- история: CSV-выгрузка ----------

def test_history_bond_fund():
    pts = parse_history(read("export_tkb-bnp-pariba-fond-obligatsiy.csv"))
    assert len(pts) == 292
    assert [p.date for p in pts] == sorted({p.date for p in pts})     # по возрастанию, без дублей
    first, last = pts[0], pts[-1]
    assert (first.date, first.nav, first.price) == (date(2025, 9, 1), 1102771931.34, 8399.62)
    # /funds/dnm/: Фонд облигаций — пай 9451.67 / 9447.80, СЧА 776913379.36 / 776522868.13
    assert (last.date, last.nav, last.price) == (date(2026, 11, 2), 776913379.36, 9451.67)
    prev = next(p for p in pts if p.date == date(2026, 10, 30))
    assert (prev.nav, prev.price) == (776522868.13, 9447.80)


def test_history_currency_fund_is_in_rubles():
    pts = parse_history(read("export_tkb-bnp-pariba-fond-valyutnykh-obligatsiy.csv"))
    last = pts[-1]
    # Официальные рублёвые значения (а не справочные USD* 412.62 / 6324655.25)
    assert (last.date, last.nav, last.price) == (date(2026, 11, 2), 509497150.76, 33239.43)
    assert len(pts) == 292


def test_history_broken_csv():
    text = read("export_tkb-bnp-pariba-fond-obligatsiy.csv").decode("utf-8-sig")
    # удалена колонка СЧА
    no_nav = "\n".join(";".join(c for i, c in enumerate(line.split(";")) if i != 1)
                       for line in text.splitlines())
    with pytest.raises(UnknownFormat):
        parse_history(no_nav)
    # мусор вместо числа
    with pytest.raises(UnknownFormat):
        parse_history(text.replace('"9451.67"', '"н.д.x"'))
    # вместо CSV пришла HTML-страница
    with pytest.raises(UnknownFormat):
        parse_history(read("funds.html"))
    # одна дата с двумя разными значениями
    lines = text.splitlines()
    with pytest.raises(UnknownFormat):
        parse_history("\n".join(lines + [lines[1].replace("9451.67", "9000.00")]))


# ---------- сопоставление: номер правил ----------

def test_rules_from_information_pages():
    for rules, slug in SLUGS.items():
        assert parse_rules(read(f"info_{slug}.html")) == rules
    # в подвале каждой страницы — правила всех фондов; берётся только блок «Регистрация правил»
    html = read("info_tkb-bnp-pariba-premium-fond-aktsiy.html").decode()
    assert "0081-58233855" in html and "№ 0478 — 75408434" in html


def test_norm_rules():
    assert norm_rules("№ 0478 — 75408434 от 28.02.2006") == "0478-75408434"
    assert norm_rules("№0991-94131990 от 20.09.2007") == "0991-94131990"
    assert norm_rules("без номера") is None


def test_rules_broken_page():
    html = read("info_tkb-bnp-pariba-zoloto.html").decode()
    with pytest.raises(UnknownFormat):
        parse_rules(html.replace("Регистрация правил фонда", "Правила фонда"))
    with pytest.raises(UnknownFormat):
        parse_rules(html.replace("№2026-94198244", "№ скоро"))


def test_catalog_slugs():
    slugs = parse_catalog(read("funds.html"))
    assert sorted(slugs) == sorted(SLUGS.values())      # без служебных /funds/dnm/, /funds/ceif/ …


# ---------- официальная доходность ----------

def test_returns_table():
    rets = parse_returns(read("funds.html"), CATALOG_URL)
    assert set(rets) == set(SLUGS.values())

    def as_dict(slug):
        return {r.period: r.value_pct for r in rets[slug]}

    assert as_dict("tkb-bnp-pariba-fond-obligatsiy") == {"1m": 1.05, "3m": 2.04, "6m": 1.69, "1y": 10.27}
    assert as_dict("tkb-bnp-pariba-globalnye-investitsii") == {"1m": 1.45, "3m": 2.03, "6m": 1.76, "1y": 9.02}
    assert as_dict("tkb-bnp-pariba-fond-sbalansirovannyy-konservativnyy") == {
        "1m": 1.55, "3m": 0.92, "6m": -3.60, "1y": 2.78}
    # валютные фонды: рублёвая строка, а не справочная USD* (у USD 1 м = 2.65 и -3.70)
    assert as_dict("tkb-bnp-pariba-fond-valyutnykh-obligatsiy") == {"1m": -1.91, "3m": 2.41, "6m": 4.12, "1y": 3.55}
    assert as_dict("tkb-bnp-pariba-zoloto")["1m"] == -7.98
    r = rets["tkb-bnp-pariba-premium-fond-aktsiy"][0]
    assert (r.as_of, r.source_url) == (date(2026, 10, 31), CATALOG_URL)


def test_returns_broken_table():
    html = read("funds.html").decode()
    with pytest.raises(UnknownFormat):      # нет колонки «1 м»
        parse_returns(html.replace("1&nbsp;м<", "1&nbsp;мес<").replace("1\xa0м<", "1\xa0мес<"), CATALOG_URL)
    with pytest.raises(UnknownFormat):      # нет даты доходности в шапке
        parse_returns(re.sub(r"(Прирост[^<]*?)\d{2}\.\d{2}\.\d{4}", r"\1", html), CATALOG_URL)
    with pytest.raises(UnknownFormat):      # нет таблиц вовсе
        parse_returns("<html><body><p>Технические работы</p></body></html>", CATALOG_URL)


# ---------- адаптер целиком на фикстурах ----------

class FakeHttp:
    """Отдаёт фикстуры вместо сети; можно «уронить» отдельные адреса."""

    def __init__(self, fail: tuple[str, ...] = (), catalog: Path | None = None):
        self.fail = fail
        self.catalog = catalog
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw):
        self.calls.append((url, params))
        if any(f in url for f in self.fail):
            raise FetchError(url, "сайт не отвечает (ReadTimeout)")
        if url == CATALOG_URL:
            name = "funds.html"
        elif m := re.fullmatch(rf"{BASE}/funds/([a-z0-9-]+)/information/", url):
            name = f"info_{m.group(1)}.html"
        elif m := re.fullmatch(rf"{BASE}/funds/([a-z0-9-]+)/export/", url):
            assert params["format"] == "csv" and params["dateFrom"] == "01.09.2025"
            name = f"export_{m.group(1)}.csv"
        else:
            raise AssertionError(f"неожиданный адрес {url}")
        path = self.catalog if (name == "funds.html" and self.catalog) else FIX / name
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def fund(rules: str) -> Fund:
    return Fund(rules=rules, name="", full_name="", type="open", category="", status="Сформирован",
                reg_date="", formation_end=None, uk_name="", uk_short="ТКБ", uk_ogrn="",
                uk_inn="7825489723", uk_site=BASE, exchange_tradable=False)


def run(http, rules):
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.tkbip"))
    return {fd.rules: fd for fd in TkbipAdapter().collect(ctx, [fund(r) for r in rules])}


def test_adapter_matches_by_rules():
    http = FakeHttp()
    res = run(http, list(SLUGS) + ["9999-00000000"])
    for rules, slug in SLUGS.items():
        fd = res[rules]
        assert fd.status == "ok", (rules, fd.reason)
        assert fd.currency == "RUB"
        assert fd.page_url == f"{BASE}/funds/{slug}/results/"
        assert fd.segments[0].points[-1].date == date(2026, 11, 2)
        assert {r.period for r in fd.official_returns} == {"1m", "3m", "6m", "1y"}
    # два «сбалансированных» не перепутаны
    assert res["0078-58234010"].segments[0].points[-1].price == 8845.15
    assert res["0096-58227323"].segments[0].points[-1].price == 357.97
    assert {r.period: r.value_pct for r in res["0096-58227323"].official_returns}["1y"] == 9.02
    assert res["9999-00000000"].status == "not_found"
    # общий каталог и страницы фондов качаются один раз на всю УК
    urls = [u for u, _ in http.calls]
    assert urls.count(CATALOG_URL) == 1 and len(urls) == 1 + 7 + 7


def test_adapter_info_page_down_is_not_not_found():
    # страница «Информация о фонде» Золота не ответила: Золото — unavailable, а не «не найден»
    res = run(FakeHttp(fail=("tkb-bnp-pariba-zoloto/information",)), list(SLUGS))
    assert res["2026-94198244"].status == "unavailable"
    assert all(fd.status == "ok" for r, fd in res.items() if r != "2026-94198244")


def test_adapter_catalog_down():
    res = run(FakeHttp(fail=(CATALOG_URL,)), list(SLUGS))
    assert {fd.status for fd in res.values()} == {"unavailable"}


def test_adapter_broken_returns_keeps_history(tmp_path):
    # таблица доходности сменила формат: ряд собирается, а поломка видна в errors, а не молча
    broken = tmp_path / "funds.html"
    html = read("funds.html").decode()
    broken.write_text(html.replace("1&nbsp;год<", "12&nbsp;мес<").replace("1\xa0год<", "12\xa0мес<"), "utf-8")
    res = run(FakeHttp(catalog=broken), list(SLUGS))
    for fd in res.values():
        assert fd.status == "ok" and fd.segments[0].points
        assert fd.official_returns == []
        assert fd.errors and "доходность" in fd.errors[0]
