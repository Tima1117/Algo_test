"""Адаптер «Альфа-Капитал»: разбор настоящих ответов сайта УК (tests/fixtures/alfa/).

Фикстуры:
- catalog.html — страница https://www.alfacapital.ru/disclosure/pifs, урезанная до <title> и части
  window.__SERVER_STATE__ (page, disclosureFunds);
- nav_<slug>_p<n>.json — https://www.alfacapital.ru/gate/v1/fundDisclosureRegData/v1?slug=<slug>&page=<n>
  (akmc-yuan и ipifsi_ak урезаны по числу строк), nav_empty.json — ответ за последней страницей;
- funds_data.json — https://www.alfacapital.ru/gate/v1/fundsData/v1, оставлено 5 продуктов.
Значения в тестах сверены с таблицей «Стоимость пая и СЧА» на /disclosure/pifs/<slug>/cost и с блоком
«Динамика фонда» на странице фонда.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest

from pif.http import RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.alfa import (NAV_URL, YIELDS_URL, AlfaCapitalAdapter, match_fund, parse_catalog,
                              parse_nav_currency, parse_nav_page, parse_returns)
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "alfa"


def load(name: str) -> bytes:
    return (FIX / name).read_bytes()


def load_json(name: str):
    return json.loads(load(name))


def fund(rules: str, name: str, kind: str, reg_date: str = "2003-01-01", formation_end: str | None = None) -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type=kind, category="Фонд рыночных финансовых инструментов",
                status="Сформирован", reg_date=reg_date, formation_end=formation_end,
                uk_name='ООО УК "Альфа-Капитал"', uk_short='ООО УК "Альфа-Капитал"', uk_ogrn="1027739292283",
                uk_inn="7728142469", uk_site="https://www.alfacapital.ru", exchange_tradable=kind == "exchange")


# Розничные фонды УК из реестра ЦБ на 03.11.2026: номер правил, название, тип -> slug на сайте УК.
REGISTRY = [
    ("4929", "Альфа - Капитал Сбалансированный с выплатой дохода", "open", "opif_akcf"),
    ("0034-18810975", "Альфа-Капитал", "interval", "ipifsi_ak"),
    ("5732", "Альфа-Капитал Акции компаний роста", "open", "opif_ak_growth"),
    ("5626", "Альфа-Капитал Акции с выплатой дохода", "open", "opif_akstockip"),
    ("6527", "Альфа-Капитал Антиинфляционный", "exchange", "bpif_anti_inflati"),
    ("0500-94103344", "Альфа-Капитал Баланс", "open", "opif_aks"),
    ("5425", "Альфа-Капитал Валютные облигации", "open", "opif_fcbond"),
    ("6267", "Альфа-Капитал Валютные облигации с выплатой дохода", "open", "opif_fcbond_ip"),
    ("6183", "Альфа-Капитал Высокодоходные облигации", "open", "opif_h-y-bonds"),
    ("5012", "Альфа-Капитал Денежный рынок", "exchange", "bpif-akmm"),
    ("6848", "Альфа-Капитал Денежный рынок Плюс", "exchange", "bpif-akmm-plus"),
    ("7781", "Альфа-Капитал Денежный рынок Юани", "exchange", "bpif-akmc-yuan"),
    ("7851", "Альфа-Капитал Длинные ОФЗ", "open", "opif-ac-longOFZ"),
    ("4713", "Альфа-Капитал Золото", "exchange", "bpif-gold"),
    ("6580", "Альфа-Капитал ИТ Лидеры", "exchange", "bpif_it_leaders"),
    ("7246", "Альфа-Капитал Индекс МосБиржи потребительского сектора полной доходности", "exchange", "bpif-conssec"),
    ("7245", "Альфа-Капитал Индекс МосБиржи финансов полной доходности", "exchange", "bpif-fullincfin"),
    ("6911", "Альфа-Капитал Индекс голубых фишек МосБиржи", "exchange", "bpif-bluechip"),
    ("4580", "Альфа-Капитал Квант", "exchange", "bpif-quantum"),
    ("0387-78483850", "Альфа-Капитал Ликвидные акции", "open", "opifa_akliq"),
    ("6991", "Альфа-Капитал Накопительный", "open", "opif-cumulative"),
    ("6107", "Альфа-Капитал Новые имена", "open", "opif_ak_new_n"),
    ("0095-59893492", "Альфа-Капитал Облигации Плюс", "open", "opif_akop"),
    ("5473", "Альфа-Капитал Облигации с выплатой дохода", "open", "opif_akipo"),
    ("6541", "Альфа-Капитал Облигации с переменным купоном", "exchange", "bpif_varcoup_bonds"),
    ("6838", "Альфа-Капитал Платина - Палладий", "exchange", "bpif_plat_pallad"),
    ("0698-94121750", "Альфа-Капитал Ресурсы", "open", "opifa_akn"),
    ("5955", "Альфа-Капитал Умный портфель", "exchange", "bpif-sbc"),
    ("4213", "Альфа-Капитал Управляемые акции", "exchange", "bpif_akmrs"),
    ("6467", "Альфа-Капитал Управляемые акции с выплатой дохода", "exchange", "bpif_akmrs_inc"),
    ("6996", "Альфа-Капитал Управляемые валютные облигации", "exchange", "bpif_manfcbond"),
    ("4039", "Альфа-Капитал Управляемые облигации", "exchange", "bpif-ctrlbonds"),
    ("7146", "Биткоган: фонд рыночных возможностей", "open", "opif-bitkogan"),
    ("6932", "Блэк лайн", "open", "opif-blackline"),
    ("6917", "Великолепная семерка", "open", "opif-magsev"),
    ("7593", "Долгосрочные инвестиции", "open", "opif-long-invest"),
    ("0645-94120472", "Инвестбаланс", "interval", "inbal"),
    ("7335", "Консервативная стратегия на МосБирже", "open", "opif-consstratme"),
    ("7926", "Лягушка, слон и брокколи", "open", "opif-frelbroc"),
    ("6931", "Матрешка а-ля Рус", "open", "opif-matryoshka"),
    ("7852", "НФК-Сбережения - Ликвидность", "exchange", "bpif-NFC-savliq"),
    ("0017-46538911", "Первый", "interval", "nfprir"),
    ("7374", "Сбалансированные Возможности", "open", "opif-balancopp"),
    ("0055-56612034", "Универсальный", "interval", "hightech"),
]


# ---------------- каталог и сопоставление ----------------

def test_catalog_parsed():
    cat = parse_catalog(load("catalog.html"))
    by_slug = {s.slug: s for s in cat}
    assert len(cat) == 56          # 25 открытых, 18 биржевых, 13 интервальных (вкл. для квалов)
    assert by_slug["opif_akop"].name == "ОПИФ Альфа-Капитал Облигации Плюс"
    assert by_slug["opif_akop"].type == "open"
    assert by_slug["bpif-akmc-yuan"].type == "exchange"
    assert by_slug["hightech"].name == "ИПИФ рыночных финансовых инструментов «Универсальный»"
    assert by_slug["hightech"].type == "interval"


def test_all_registry_funds_matched_uniquely():
    cat = parse_catalog(load("catalog.html"))
    got = {}
    for rules, name, kind, slug in REGISTRY:
        site, why = match_fund(fund(rules, name, kind), cat)
        assert site is not None, f"{rules} {name}: {why}"
        got[rules] = site.slug
    assert got == {rules: slug for rules, _, _, slug in REGISTRY}
    assert len(set(got.values())) == 44


def test_match_is_strict():
    cat = parse_catalog(load("catalog.html"))
    # «Ликвидные акции» не должны склеиться с квал. «Ликвидные акции Плюс»
    site, _ = match_fund(fund("0387-78483850", "Альфа-Капитал Ликвидные акции", "open"), cat)
    assert site.slug == "opifa_akliq"
    site, why = match_fund(fund("9999", "Альфа-Капитал Несуществующий", "open"), cat)
    assert site is None and "не найден" in why
    site, why = match_fund(fund("0095-59893492", "Альфа-Капитал Облигации Плюс", "exchange"), cat)
    assert site is None and "другого типа" in why


def test_catalog_unknown_format():
    with pytest.raises(UnknownFormat):
        parse_catalog(b"<html><body>Технические работы</body></html>")
    html = load("catalog.html").decode("utf-8")
    with pytest.raises(UnknownFormat):
        parse_catalog(html.replace('"disclosureFunds"', '"disclosureFundsV2"'))
    with pytest.raises(UnknownFormat):
        parse_catalog(html.replace('"slug"', '"code"'))


# ---------------- ряд СЧА и пая ----------------

def test_nav_page_values():
    pts = parse_nav_page(load("nav_opif_akop_p0.json"))
    by = {p.date: p for p in pts}
    assert len(pts) == 60
    assert pts[0].date == date(2026, 8, 11) and pts[-1].date == date(2026, 11, 2)   # по возрастанию
    # как в таблице на /disclosure/pifs/opif_akop/cost: 02.11.2026 7 311,93 ₽ 41 338 619 954,98 ₽
    assert by[date(2026, 11, 2)].price == 7311.93
    assert by[date(2026, 11, 2)].nav == 41338619954.98
    assert by[date(2026, 10, 30)].price == 7316.61
    assert by[date(2026, 10, 30)].nav == 41400723716.81
    assert date(2026, 11, 1) not in by          # выходные УК не публикует
    assert parse_nav_currency(load("nav_opif_akop_p0.json")) == "RUB"


def test_nav_page_yuan_and_interval():
    pts = parse_nav_page(load("nav_bpif-akmc-yuan_p0.json"))
    assert pts[-1].date == date(2026, 11, 2)
    assert pts[-1].price == 105.79405 and pts[-1].nav == 215509809.63
    assert parse_nav_currency(load("nav_bpif-akmc-yuan_p0.json")) == "CNY"
    # интервальный фонд: конец месяца + даты интервалов
    dates = [p.date for p in parse_nav_page(load("nav_ipifsi_ak_p0.json"))]
    assert dates[-3:] == [date(2026, 9, 30), date(2026, 10, 23), date(2026, 10, 30)]
    assert parse_nav_page(load("nav_ipifsi_ak_p0.json"))[-1].nav == 3083840128.28


def test_nav_empty_page():
    assert parse_nav_page(load("nav_empty.json")) == []
    assert parse_nav_currency(load("nav_empty.json")) is None


def test_nav_zero_is_not_a_value():
    data = load_json("nav_opif_akop_p0.json")
    data["data"][0]["netAssetValue"] = 0
    first = max(parse_nav_page(data), key=lambda p: p.date)
    assert first.nav is None and first.price == 7311.93


@pytest.mark.parametrize("breakage", [
    lambda d: d.pop("data"),
    lambda d: d["data"][3].pop("shareCost"),
    lambda d: d["data"][3].pop("netAssetValue"),
    lambda d: d["data"][3].pop("date"),
    lambda d: d["data"][3].update(netAssetValue="н/д млн"),
    lambda d: d["data"][3].update(shareCost=-1),
    lambda d: d["data"][3].update(date="вчера"),
    lambda d: d["data"][4].update(date=d["data"][3]["date"]),
])
def test_nav_unknown_format(breakage):
    data = load_json("nav_opif_akop_p0.json")
    breakage(data)
    with pytest.raises(UnknownFormat):
        parse_nav_page(data)


def test_nav_currency_strict():
    data = load_json("nav_opif_akop_p0.json")
    data["data"][5]["currency"] = "USD"
    with pytest.raises(UnknownFormat):
        parse_nav_currency(data)
    data["data"][5]["currency"] = "XYZ"
    with pytest.raises(UnknownFormat):
        parse_nav_currency(data)


# ---------------- официальная доходность ----------------

def test_returns_values():
    rets = parse_returns(load("funds_data.json"), YIELDS_URL)
    akop = {r.period: r for r in rets["opif_akop"]}
    # на странице фонда: 1 мес −1,48%, 3 мес 0,64%, 6 мес −0,15%, 1 год 8,49%
    assert {p: r.value_pct for p, r in akop.items()} == {"1m": -1.477, "3m": 0.637, "6m": -0.15, "1y": 8.488}
    assert all(r.as_of == date(2026, 9, 30) and r.source_url == YIELDS_URL for r in akop.values())
    assert [r.period for r in rets["opif-ac-longOFZ"]] == ["1m", "3m"]     # фонд моложе полугода
    assert rets["opifa-akliq-plus"] == []


@pytest.mark.parametrize("breakage", [
    lambda d: d["data"].pop("products"),
    lambda d: d["data"]["products"][0]["share"].pop("yields"),
    lambda d: d["data"]["products"][0]["share"]["yields"].pop("sixMonths"),
    lambda d: d["data"]["products"][1]["share"]["yields"]["oneMonth"].pop("to"),
    lambda d: d["data"]["products"][1]["share"]["yields"]["oneMonth"].update(value="много"),
])
def test_returns_unknown_format(breakage):
    data = load_json("funds_data.json")
    breakage(data)
    with pytest.raises(UnknownFormat):
        parse_returns(data, YIELDS_URL)


# ---------------- сбор целиком (без сети) ----------------

class FakeHttp:
    """Отдаёт фикстуры вместо сайта; страницы, которых нет в фикстурах, — пустой ответ сайта."""

    def __init__(self, nav_pages: dict[tuple[str, int], str], page0_from_cache: bool = True):
        self.nav_pages = nav_pages
        self.page0_from_cache = page0_from_cache
        self.calls: list[tuple[str, dict | None, float | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, fresh_hours=None, **_):
        self.calls.append((url, params, fresh_hours))
        from_cache = True
        if url == NAV_URL:
            name = self.nav_pages.get((params["slug"], params["page"]), "nav_empty.json")
            from_cache = self.page0_from_cache or params["page"] > 0
        elif url == YIELDS_URL:
            name = "funds_data.json"
        else:
            name = "catalog.html"
        path = FIX / name
        full = url + ("?" + urlencode(params) if params else "")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00",
                      fmt, from_cache=from_cache)


def run(http, funds, since):
    ctx = Context(http=http, since=since, log=logging.getLogger("test"))
    return {fd.rules: fd for fd in AlfaCapitalAdapter().collect(ctx, funds)}


PAGES = {("opif_akop", 0): "nav_opif_akop_p0.json", ("opif_akop", 1): "nav_opif_akop_p1.json",
         ("opif-frelbroc", 0): "nav_opif-frelbroc_p0.json", ("opif-frelbroc", 1): "nav_opif-frelbroc_p1.json",
         ("bpif-akmc-yuan", 0): "nav_bpif-akmc-yuan_p0.json"}


def test_collect_offline():
    http = FakeHttp(PAGES)
    funds = [fund("0095-59893492", "Альфа-Капитал Облигации Плюс", "open"),
             fund("7926", "Лягушка, слон и брокколи", "open", "2026-06-29", "2026-07-24"),
             fund("9999", "Альфа-Капитал Несуществующий", "open")]
    res = run(http, funds, date(2026, 8, 1))

    akop = res["0095-59893492"]
    assert akop.status == "ok" and akop.currency == "RUB"
    pts = {p.date: p for s in akop.segments for p in s.points}
    assert min(pts) == date(2026, 8, 3) and max(pts) == date(2026, 11, 2)
    assert sum(len(s.points) for s in akop.segments) == len(pts)        # без дублей между страницами
    assert pts[date(2026, 8, 10)].price == 7366.01                      # точка со второй страницы
    assert akop.segments[0].page_url == "https://www.alfacapital.ru/disclosure/pifs/opif_akop/cost"
    assert akop.page_url == "https://www.alfacapital.ru/individual/pifs/opif_akop"
    assert {r.period for r in akop.official_returns} == {"1m", "3m", "6m", "1y"}
    # листание остановилось на странице, где ряд ушёл раньше начала периода
    nav_calls = [(p["slug"], p["page"]) for u, p, _ in http.calls if u == NAV_URL]
    assert ("opif_akop", 2) not in nav_calls and ("opif-frelbroc", 2) in nav_calls

    frel = res["7926"]
    assert frel.status == "ok"
    assert frel.official_returns == [] and any("КИД" in w for w in frel.warnings)
    assert frel.page_url.endswith("/disclosure/pifs/opif-frelbroc/cost")

    assert res["9999"].status == "not_found"
    # общий каталог и витрина скачаны по одному разу на весь прогон
    assert sum(1 for u, _, _ in http.calls if u == YIELDS_URL) == 1
    assert sum(1 for u, _, _ in http.calls if u.endswith("/disclosure/pifs")) == 1


def test_collect_young_fund_and_yuan():
    funds = [fund("7926", "Лягушка, слон и брокколи", "open", "2026-06-29", "2026-07-24"),
             fund("7781", "Альфа-Капитал Денежный рынок Юани", "exchange", "2026-05-07", "2026-05-22")]
    res = run(FakeHttp(PAGES), funds, date(2026, 7, 1))
    frel = res["7926"]
    assert frel.status == "ok" and "сформирован позже" in frel.reason
    assert min(p.date for s in frel.segments for p in s.points) == date(2026, 7, 24)

    res = run(FakeHttp(PAGES), funds[1:], date(2026, 10, 26))
    yuan = res["7781"]
    assert yuan.status == "ok" and yuan.currency == "CNY"
    # доходность на сайте рублёвая (пай пересчитан по курсу ЦБ) — в сверку с юаневым рядом не идёт
    assert yuan.official_returns == [] and any("рублёвая" in w for w in yuan.warnings)


def test_collect_refreshes_deeper_pages_when_first_page_is_new():
    http = FakeHttp(PAGES, page0_from_cache=False)
    run(http, [fund("0095-59893492", "Альфа-Капитал Облигации Плюс", "open")], date(2026, 8, 1))
    fresh = {p["page"]: fh for u, p, fh in http.calls if u == NAV_URL}
    assert fresh == {0: None, 1: 0}


def test_collect_broken_page_is_parse_error_for_one_fund(tmp_path):
    broken = load_json("nav_opif_akop_p0.json")
    del broken["data"][0]["shareCost"]
    (tmp_path / "nav.json").write_text(json.dumps(broken))
    pages = dict(PAGES)
    pages[("opif_akop", 0)] = str(tmp_path / "nav.json")   # FIX / абсолютный путь = абсолютный путь
    funds = [fund("0095-59893492", "Альфа-Капитал Облигации Плюс", "open"),
             fund("7926", "Лягушка, слон и брокколи", "open", "2026-06-29", "2026-07-24")]
    res = run(FakeHttp(pages), funds, date(2026, 8, 1))
    assert res["0095-59893492"].status == "parse_error"
    assert res["7926"].status == "ok"
