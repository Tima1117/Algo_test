"""СОЛИД Менеджмент: разбор настоящих ответов JSON-API сайта solid-mn.ru (урезанных).

Значения сверены со страницами фондов: например, https://solid-mn.ru/services/solid-invest/
на 03.11.2026 показывает «Пай 721.29, СЧА 146 531 514.42, Обновлено 02.11.2026»,
https://solid-mn.ru/services/solid-cash-flow/ — «Пай 992.19, СЧА 271 218 253.7».
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif import config
from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import solid
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "solid"

# Фонды УК из реестра ЦБ: номер правил -> (название в реестре, code на сайте)
REGISTRY = {
    "6833": ("СОЛИД – Денежный поток", "solid-cash-flow"),
    "0065-58549452": ("Солид - российские облигации", "solid-rossiyskie-obligatsii"),
    "0040-52697973": ("Солид -Инвест", "solid-invest"),
    "1903-94169099": ("Солид – Глобус", "solid-globus"),
    "0351-76578546": ("Солид – Индекс МосБиржи", "solid-indeks-mosbirzhi"),
    "0045-54859434": ("Солид – пенсионный капитал", "solid-pensionnyy-kapital"),
    "7286": ("Финголд", "fingold"),
}


def _json(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def _product(doc: dict) -> dict:
    return doc["data"]["result"]["fields"]["workarea"]["items"]["items"]["product"]


def _fund(rules: str, name: str = "") -> Fund:
    return Fund(rules=rules, name=name or REGISTRY.get(rules, ("фонд",))[0], full_name="", type="open",
                category="", status="Сформирован", reg_date="2000-01-01", formation_end=None,
                uk_name="Акционерное общество «СОЛИД Менеджмент»", uk_short="АО «СОЛИД Менеджмент»",
                uk_ogrn="1027700227180", uk_inn="7706150949", uk_site="http://solid-mn.ru/",
                exchange_tradable=False)


# ---------- каталог и сопоставление ----------
def test_catalog_matches_every_registry_fund_by_rules():
    catalog = solid.parse_catalog((FIX / "products.json").read_bytes())
    assert len(catalog) == 15
    for rules, (name, code) in REGISTRY.items():
        item = solid.match_fund(_fund(rules, name), catalog)
        assert item is not None and item["code"] == code, rules
    # «Заблокированные активы СОЛИД - Глобус» (правила 5954) — отдельный фонд, не путаем с «Глобусом»
    assert solid.match_fund(_fund("0000-00000000", "Солид – Глобус"), catalog) is None
    assert solid.match_fund(_fund("5954", "что угодно"), catalog)["code"] == \
        "zablokirovannye-aktivy-pif-solid-globus"
    assert solid.currency_of(catalog[0]["pai"]) == "RUB"      # '992.19₽'


def test_rules_normalization():
    assert solid.norm_rules("№ 0040 - 52697973") == "0040-52697973"
    assert solid.rules_in("Правила доверительного управления №&nbsp;0040-52697973 зарегистрированы") \
        == "0040-52697973"
    assert solid.rules_in("Указание Банка России от 29.06.2026 № 7402-У") is None


# ---------- история ----------
def test_history_solid_invest():
    pts = solid.parse_history((FIX / "detail_solid-invest.json").read_bytes())
    by = {p.date: p for p in pts}
    assert [p.date for p in pts] == sorted(by)                       # по возрастанию, без дублей
    assert by[date(2025, 9, 1)].price == 816.64
    assert by[date(2025, 9, 1)].nav == 164934404.98
    assert by[date(2026, 10, 30)].price == 735.37
    assert by[date(2026, 10, 30)].nav == 149392433.69
    assert by[date(2026, 11, 2)].price == 721.29                     # «Пай 721.29» на странице
    assert by[date(2026, 11, 2)].nav == 146531514.42                 # «СЧА 146 531 514.42»
    # 2000 год: сайт пишет СЧА '.00' — это «нет значения», а не ноль
    assert by[date(2000, 4, 17)].price == 10.0 and by[date(2000, 4, 17)].nav is None


def test_history_fingold_formation_point_has_no_nav():
    pts = solid.parse_history((FIX / "detail_fingold.json").read_bytes())
    by = {p.date: p for p in pts}
    assert pts[0].date == date(2025, 9, 23)
    assert by[date(2025, 9, 23)].price == 1000.0 and by[date(2025, 9, 23)].nav is None
    assert by[date(2025, 11, 21)].price == 999.87 and by[date(2025, 11, 21)].nav == 20210464.47
    assert by[date(2026, 11, 2)].price == 926.98 and by[date(2026, 11, 2)].nav == 52302425.02


# ---------- доходность ----------
def test_returns_solid_invest():
    url = "https://solid-mn.ru/services/solid-invest/"
    rets = solid.parse_returns((FIX / "detail_solid-invest.json").read_bytes(), url)
    assert {r.period: r.value_pct for r in rets} == {"1m": 6.08, "3m": 7.39, "6m": -4.39, "1y": 1.06}
    assert all(r.as_of == date(2026, 11, 2) and r.source_url == url for r in rets)


def test_returns_and_payouts_cash_flow():
    raw = (FIX / "detail_solid-cash-flow.json").read_bytes()
    rets = solid.parse_returns(raw, "u")
    assert {r.period: r.value_pct for r in rets} == {"1m": 1.0, "3m": 2.53, "6m": 6.62, "1y": 13.87}
    pays = solid.parse_payouts(raw, "u")
    assert len(pays) == 15 and pays[0].date == date(2025, 8, 29)
    assert [(p.date, p.amount) for p in pays[-2:]] == [(date(2026, 9, 30), 13.69), (date(2026, 10, 30), 12.24)]
    assert all(p.source_url == "u" for p in pays)
    assert solid.parse_payouts((FIX / "detail_solid-invest.json").read_bytes(), "u") == []
    assert solid.detail_rules(raw) == "6833"


# ---------- испорченные ответы ----------
def _broken(mutate) -> dict:
    doc = copy.deepcopy(_json("detail_solid-invest.json"))
    mutate(_product(doc))
    return doc


@pytest.mark.parametrize("mutate", [
    lambda p: p["paiPrice"]["data"]["items"]["pai"][3].pop("Price"),
    lambda p: p["paiPrice"]["data"]["items"]["sha"][3].pop("Value"),
    lambda p: p["paiPrice"]["data"]["items"]["pai"][3].pop("OperDate"),
    lambda p: p["paiPrice"]["data"]["items"].pop("sha"),
    lambda p: p.pop("paiPrice"),
    lambda p: p["paiPrice"]["data"]["items"]["pai"][3].update(OperDate="30 октября 2026"),
    lambda p: p["paiPrice"]["data"]["items"]["pai"][3].update(Price="н.д. руб"),
    lambda p: p["paiPrice"]["data"]["items"]["pai"][3].update(FundID=999),
    # одна дата дважды с разными ценами
    lambda p: p["paiPrice"]["data"]["items"]["pai"].append(
        dict(p["paiPrice"]["data"]["items"]["pai"][-1], Price="1.00")),
])
def test_history_broken_structure_raises(mutate):
    with pytest.raises(UnknownFormat):
        solid.parse_history(_broken(mutate))


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("profitPeriods"),
    lambda p: p["profitPeriods"]["list"]["1_month"].pop("percent"),
    lambda p: p["paramsBlock"]["items"].pop("updateDate"),
])
def test_returns_broken_structure_raises(mutate):
    with pytest.raises(UnknownFormat):
        solid.parse_returns(_broken(mutate), "u")


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[-1].pop("Payment"),
    lambda rows: rows[-1].pop("OperDate"),
    lambda rows: rows[-1].update(Payment="-5.00"),
    lambda rows: rows.append(dict(rows[-1], Payment="1.00")),     # две выплаты на одну дату
])
def test_payouts_broken_structure_raises(mutate):
    doc = copy.deepcopy(_json("detail_solid-cash-flow.json"))
    mutate(_product(doc)["paiPrice"]["data"]["items"]["payouts"])
    with pytest.raises(UnknownFormat):
        solid.parse_payouts(doc, "u")


def test_catalog_broken_raises():
    doc = _json("products.json")
    items = doc["data"]["result"]["fields"]["workarea"]["items"]["items"]["items"]
    bad = copy.deepcopy(doc)
    bad["data"]["result"]["fields"]["workarea"]["items"]["items"]["items"][0].pop("description")
    with pytest.raises(UnknownFormat):
        solid.parse_catalog(bad)
    no_rules = copy.deepcopy(doc)
    for it in no_rules["data"]["result"]["fields"]["workarea"]["items"]["items"]["items"]:
        it["description"] = ""
    with pytest.raises(UnknownFormat):
        solid.parse_catalog(no_rules)
    with pytest.raises(UnknownFormat):
        solid.parse_catalog({**doc, "status": "error"})
    with pytest.raises(UnknownFormat):
        solid.parse_catalog(b"<html>maintenance</html>")
    assert items  # исходная фикстура не испорчена


# ---------- весь адаптер без сети ----------
def _ctx(tmp_path: Path, monkeypatch, routes: dict[str, tuple[int, bytes]]) -> tuple[Context, list]:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        status, body = routes.get(str(request.url), (404, b'{"status":"error"}'))
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    monkeypatch.setattr(config, "MIN_INTERVAL_SEC", 0.0)
    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.solid")), seen


def _routes(**override) -> dict[str, tuple[int, bytes]]:
    r = {solid.CATALOG_URL: (200, (FIX / "products.json").read_bytes())}
    for code in ("solid-invest", "solid-cash-flow", "fingold"):
        r[solid.DETAIL_URL.format(code=code)] = (200, (FIX / f"detail_{code}.json").read_bytes())
    r.update(override)
    return r


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, _routes())
    funds = [_fund("0040-52697973"), _fund("6833"), _fund("7286"), _fund("9999", "Нет такого фонда")]
    res = {fd.rules: fd for fd in solid.SolidAdapter().collect(ctx, funds)}

    assert [m for m, _ in seen] == ["POST"] * 4                       # каталог один раз + 3 карточки
    assert sum(1 for _, u in seen if u == solid.CATALOG_URL) == 1

    inv = res["0040-52697973"]
    assert inv.status == "ok" and inv.currency == "RUB"
    assert inv.page_url == "https://solid-mn.ru/services/solid-invest/"
    pts = inv.segments[0].points
    assert pts[0].date == date(2025, 9, 1) and pts[-1].date == date(2026, 11, 2)   # отрезано по since
    assert inv.segments[0].raw_url == solid.DETAIL_URL.format(code="solid-invest")
    assert {r.period for r in inv.official_returns} == {"1m", "3m", "6m", "1y"}

    cf = res["6833"]
    assert cf.status == "ok" and any("с учётом выплат" in w for w in cf.warnings)
    # выплаты до начала истории (29.08.2025) отрезаны, остальные — с суммой на пай и страницей фонда
    assert [p.date for p in cf.payouts][:2] == [date(2025, 9, 30), date(2025, 10, 31)]
    assert cf.payouts[-1].amount == 12.24 and cf.payouts[-1].source_url == cf.page_url
    assert res["0040-52697973"].payouts == []

    fg = res["7286"]
    assert fg.status == "ok" and fg.segments[0].points[0].nav is None
    assert any("23.09.2025" in w for w in fg.warnings)

    assert res["9999"].status == "not_found" and "9999" in res["9999"].reason


def test_collect_catalog_down_gives_status_per_fund(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, _routes(**{solid.CATALOG_URL: (404, b"{}")}))
    res = solid.SolidAdapter().collect(ctx, [_fund("0040-52697973"), _fund("6833")])
    assert [fd.status for fd in res] == ["unavailable", "unavailable"]
    assert len(seen) == 1                                             # не долбим упавший сайт


def test_collect_rules_mismatch_on_fund_page_is_parse_error(tmp_path, monkeypatch):
    doc = _json("detail_fingold.json")
    info = _product(doc)["info"]["info"]
    info["html"] = info["html"].replace("7286", "1234")
    ctx, _ = _ctx(tmp_path, monkeypatch, _routes(**{
        solid.DETAIL_URL.format(code="fingold"): (200, json.dumps(doc, ensure_ascii=False).encode())}))
    (fd,) = solid.SolidAdapter().collect(ctx, [_fund("7286")])
    assert fd.status == "parse_error" and "1234" in fd.reason
