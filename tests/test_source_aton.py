"""Атон-менеджмент: разбор настоящих ответов JSON-API сайта www.am-aton.ru (урезанных) и доверие к TLS.

Значения сверены со страницами фондов 06.11.2026:
https://www.am-aton.ru/pif/global_markets («Дивидендные акции: Рантье») — «СЧА, руб. на 02.11.2026
462 250 351.05, на 03.11.2026 459 566 202.7; расчётная стоимость пая 2 081.97 и 2 074.76; изменение стоимости
пая на 31.10.2026 за 1 мес. 10.76%, 3 мес. 11.78%, 6 мес. 2.21%, 1 год 14.1%»;
https://www.am-aton.ru/pif/amvd («Валютные облигации в долларах») — «СЧА, долл. на 03.11.2026
12 194 234.23, пай 9.665; за 1 мес. 2.41%, за 3 мес. 0.06%, за 6 мес. —».
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import ssl
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif import config
from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import aton
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "aton"

# Фонды реестра ЦБ (ИНН 7701253764): номер правил -> code страницы на сайте (сверено по карточкам)
REGISTRY_CODES = {
    "0009-46349328": "pyotr_stolypin", "0568-94120101": "eurobond_fund", "0729-94125699": "bond_fund",
    "1008-94131435": "infrastructure", "1009-94131352": "high_technology_fund", "2487": "global_markets",
    "7410": "reserve_capital", "7680": "dszh_ofz", "7682": "dszh_shares",
    "4330": "AMRE", "4506": "AMRH", "6763": "amfl", "6764": "amgb", "6221": "cumul_yuan", "7136": "amgl",
    "7838": "amvd", "5936": "cumul", "7839": "amix", "7840": "amvy",
}
NAMES = {"2487": "Атон – Дивидендные акции: Рантье", "0568-94120101": "Атон - Фонд Еврооблигаций",
         "7838": "АТОН – Валютные облигации в долларах"}


def _json(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def _fund(rules: str, name: str = "", kind: str = "open") -> Fund:
    return Fund(rules=rules, name=name or NAMES.get(rules, "фонд"), full_name="", type=kind, category="",
                status="Сформирован", reg_date="2000-01-01", formation_end=None,
                uk_name="ООО «УК «Атон-менеджмент»", uk_short='ООО "УК "Атон-менеджмент"',
                uk_ogrn="1027700027233", uk_inn="7701253764", uk_site="https://www.am-aton.ru",
                exchange_tradable=kind == "exchange")


# ---------- TLS: недостающий промежуточный сертификат ----------
def test_embedded_intermediate_fingerprint():
    der = ssl.PEM_cert_to_DER_cert(aton.GLOBALSIGN_OV_CA_2018)
    assert hashlib.sha256(der).hexdigest().upper() == (
        "B676FFA3179E8812093A1B5EAFEE876AE7A6AAF231078DAD1BFB21CD2893764A")
    ctx = ssl.create_default_context()
    before = len(ctx.get_ca_certs())
    ctx.load_verify_locations(cadata=aton.GLOBALSIGN_OV_CA_2018)
    certs = ctx.get_ca_certs()
    added = [c for c in certs if dict(x[0] for x in c["subject"]).get("commonName") == "GlobalSign RSA OV SSL CA 2018"]
    assert len(certs) >= before and len(added) == 1
    issuer = dict(x[0] for x in added[0]["issuer"])
    assert issuer["organizationalUnitName"] == "GlobalSign Root CA - R3"   # корень — из системного хранилища


def test_trust_only_site_host(tmp_path):
    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path)
    try:
        assert aton._trust_intermediate(http) is True
        assert http._extra_ca == {"www.am-aton.ru": aton.GLOBALSIGN_OV_CA_2018}   # только домен сайта УК
        site = http._client(aton.HOST)
        assert site is not http._client("am-aton.ru") and site is not http._client("www.cbr.ru")
        assert aton._trust_intermediate(http) is True and http._client(aton.HOST) is site   # идемпотентно
        assert site.headers["User-Agent"] == config.USER_AGENT
    finally:
        http.close()
    assert aton._trust_intermediate(object()) is False         # не Fetcher


# ---------- каталог и сопоставление ----------
def test_list_has_exactly_retail_funds():
    items = aton.parse_list((FIX / "list.json").read_bytes())
    assert {it["code"] for it in items} == set(REGISTRY_CODES.values())
    kinds = [it["type"] for it in items]
    assert kinds.count("open") == 9 and kinds.count("exchange") == 10
    by = {it["code"]: it for it in items}
    assert by["amvd"]["currency"] == "USD" and by["cumul_yuan"]["currency"] == "CNY"
    assert by["global_markets"]["title"] == "«Атон – Дивидендные акции: Рантье»"
    # для квалифицированных, «индикативная стоимость» и ЗПИФ «Заблокированные активы…» — не розничные
    assert not {"global_investments", "indicative_global_investments", "upif2025", "uun"} & set(by)


def test_rules_from_fund_cards():
    assert aton.detail_rules((FIX / "fund_global_markets.json").read_bytes()) == "2487"
    assert aton.detail_rules((FIX / "fund_eurobond_fund.json").read_bytes()) == "0568-94120101"
    assert aton.detail_rules((FIX / "fund_amvd.json").read_bytes()) == "7838"
    assert aton.rules_in("Правила Фонда зарегистрированы ФКЦБ России за № 0009-46349328 от 04.03.1997 г.") \
        == "0009-46349328"
    assert aton.rules_in("внесены Банком России в реестр паевых инвестиционных фондов за №&nbsp;4330 от 25.03 2021") \
        == "4330"
    assert aton.rules_in("внесены Банком России в реестр за № 6812-СД от 17.01.2025") is None
    assert aton.rules_in(None) is None


def test_match_by_name_only_exact():
    items = [{"code": "global_markets", "title": "«Атон – Дивидендные акции: Рантье»",
              "full_name": "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Атон – "
                           "Дивидендные акции: Рантье»"}]
    assert aton.match_by_name(_fund("2487"), items)["code"] == "global_markets"
    assert aton.match_by_name(_fund("2487", "Атон – Дивидендные акции"), items) is None


# ---------- история, выплаты ----------
def test_history_rantier():
    raw = (FIX / "cost_global_markets.json").read_bytes()
    assert aton.history_currency(raw) == "RUB"
    pts = aton.parse_history(raw)
    assert_series(pts, "aton_global_markets_cost")
    by = {p.date: p for p in pts}
    assert [p.date for p in pts] == sorted(by)
    assert pts[0].date == date(2025, 6, 2) and pts[-1].date == date(2026, 11, 3)
    assert (by[date(2025, 9, 1)].nav, by[date(2025, 9, 1)].price) == (673046593.97, 2492.86)
    assert (by[date(2026, 11, 2)].nav, by[date(2026, 11, 2)].price) == (462250351.05, 2081.97)
    assert (by[date(2026, 11, 3)].nav, by[date(2026, 11, 3)].price) == (459566202.7, 2074.76)
    # в день выплаты пай уже без неё: 2 033,73 -> 1 990,97 при выплате 45,81
    assert by[date(2026, 6, 29)].price == 2033.73 and by[date(2026, 6, 30)].price == 1990.97


def test_payouts_rantier():
    pays = aton.parse_payouts((FIX / "cost_global_markets.json").read_bytes(), "u")
    assert [(p.date, p.amount) for p in pays] == [
        (date(2025, 6, 30), 43.55), (date(2025, 9, 30), 198.53), (date(2025, 12, 30), 42.02),
        (date(2026, 3, 31), 44.31), (date(2026, 6, 30), 45.81), (date(2026, 9, 30), 87.86)]
    assert all(p.source_url == "u" for p in pays)
    assert aton.parse_payouts((FIX / "cost_amvd.json").read_bytes(), "u") == []


def test_history_eurobond_takes_fund_currency_block():
    raw = (FIX / "cost_eurobond_fund.json").read_bytes()
    assert aton.history_currency(raw) == "RUB"
    pts = aton.parse_history(raw)
    assert_series(pts, "aton_eurobond_fund_cost")
    assert len(pts) == 24 and pts[0].date == date(2026, 10, 1)
    assert (pts[-1].date, pts[-1].nav, pts[-1].price) == (date(2026, 11, 3), 3484911277.82, 2142.97)  # не USD 26.11


def test_history_amvd_usd_from_formation():
    raw = (FIX / "cost_amvd.json").read_bytes()
    assert aton.history_currency(raw) == "USD"
    pts = aton.parse_history(raw)
    assert_series(pts, "aton_amvd_cost")
    by = {p.date: p for p in pts}
    assert len(pts) == 96 and pts[0].date == date(2026, 6, 23)             # окончание формирования 23.06.2026
    assert (by[date(2026, 6, 23)].nav, by[date(2026, 6, 23)].price) == (7431642.26, 9.9298)
    assert (by[date(2026, 11, 2)].nav, by[date(2026, 11, 2)].price) == (12202167.56, 9.6712)
    assert (by[date(2026, 11, 3)].nav, by[date(2026, 11, 3)].price) == (12194234.23, 9.665)


# ---------- официальная доходность ----------
def test_returns_rantier_and_young_fund():
    url = "https://www.am-aton.ru/pif/global_markets"
    rets = aton.parse_returns((FIX / "profit_global_markets.json").read_bytes(), "global_markets", url)
    assert {r.period: r.value_pct for r in rets} == {"1m": 10.76, "3m": 11.78, "6m": 2.21, "1y": 14.1}
    assert all(r.as_of == date(2026, 10, 30) and r.source_url == url for r in rets)
    young = aton.parse_returns((FIX / "profit_amvd.json").read_bytes(), "amvd", "u")
    assert {r.period: r.value_pct for r in young} == {"1m": 2.41, "3m": 0.06}   # 6 мес. и год — «—»


# ---------- испорченные ответы ----------
def _items(doc: dict, block: int = 0) -> list:
    return doc["data"]["chart"][block]["items"]


@pytest.mark.parametrize("mutate", [
    lambda d: _items(d)[3].pop("PRICE"),
    lambda d: _items(d)[3].pop("SCHA"),
    lambda d: _items(d)[3].pop("DATE"),
    lambda d: _items(d)[3].update(DATE="3 ноября 2026"),
    lambda d: _items(d)[3].update(PRICE="цена"),
    lambda d: _items(d)[3].update(SCHA="-1"),
    lambda d: _items(d).append(dict(_items(d)[-1], PRICE="1.00")),   # одна дата дважды с разными ценами
    lambda d: d["data"].pop("chart"),
    lambda d: d["data"].pop("currency"),
    lambda d: d["data"].update(currency="XAU"),
    lambda d: d["data"]["chart"][0]["currency"].update(code="USD"),   # нет блока в валюте фонда
    lambda d: d["data"]["chart"][0].pop("items"),
    lambda d: d.update(status="error"),
])
def test_history_broken_structure_raises(mutate):
    doc = copy.deepcopy(_json("cost_global_markets.json"))
    mutate(doc)
    with pytest.raises(UnknownFormat):
        aton.parse_history(doc)


def test_history_not_json_raises():
    with pytest.raises(UnknownFormat):
        aton.parse_history(b"<html>maintenance</html>")


@pytest.mark.parametrize("mutate", [
    lambda d: _items(d)[-1].pop("PAYMENT"),
    lambda d: _items(d)[-1].update(PAYMENT="-5.00"),
    lambda d: _items(d).append(dict(_items(d)[-1], PAYMENT="1.00")),
])
def test_payouts_broken_structure_raises(mutate):
    doc = copy.deepcopy(_json("cost_global_markets.json"))
    _items(doc)[-1]["PAYMENT"] = "10.00"             # последняя точка — с выплатой
    assert aton.parse_payouts(doc, "u")[-1].amount == 10.0
    mutate(doc)
    with pytest.raises(UnknownFormat):
        aton.parse_payouts(doc, "u")


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("LAST_DATE"),
    lambda b: b.pop("1_m"),
    lambda b: b["3_m"].pop("change_price"),
    lambda b: b["1_y"].update(change_price="рост"),
])
def test_returns_broken_structure_raises(mutate):
    doc = copy.deepcopy(_json("profit_global_markets.json"))
    mutate(doc["data"]["global_markets"])
    with pytest.raises(UnknownFormat):
        aton.parse_returns(doc, "global_markets", "u")
    with pytest.raises(UnknownFormat):
        aton.parse_returns(_json("profit_global_markets.json"), "amvd", "u")   # блок другого фонда


def test_list_and_card_broken_raise():
    doc = _json("list.json")
    no_section = copy.deepcopy(doc)
    no_section["data"]["group"] = [g for g in no_section["data"]["group"] if g["sectionCode"] != "available_pif"]
    no_code = copy.deepcopy(doc)
    next(g for g in no_code["data"]["group"] if g["sectionCode"] == "available_pif")["items"][0].pop("code")
    no_retail = copy.deepcopy(doc)
    for it in next(g for g in no_retail["data"]["group"] if g["sectionCode"] == "available_pif")["items"]:
        it["qualified_investor"] = True
    for bad in (no_section, no_code, no_retail, {**doc, "status": "error"}, b"<html></html>"):
        with pytest.raises(UnknownFormat):
            aton.parse_list(bad)
    card = _json("fund_amvd.json")
    card["data"].pop("number")
    with pytest.raises(UnknownFormat):
        aton.detail_rules(card)


# ---------- весь адаптер без сети ----------
def _ctx(tmp_path: Path, monkeypatch, routes: dict[str, tuple[int, bytes]]) -> tuple[Context, list]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        status, body = routes.get(str(request.url), (404, b'{"status":"error"}'))
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    monkeypatch.setattr(config, "MIN_INTERVAL_SEC", 0.0)
    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.aton")), seen


CODES = ("global_markets", "eurobond_fund", "amvd")


def _short_list() -> bytes:
    """Каталог, где из розничных фондов остались только три фонда с фикстурами (прочие группы — как были)."""
    doc = _json("list.json")
    for g in doc["data"]["group"]:
        if g["sectionCode"] == "available_pif":
            g["items"] = [it for it in g["items"] if it["code"] in CODES or it["qualified_investor"]]
    return json.dumps(doc, ensure_ascii=False).encode()


def _routes(list_body: bytes | None = None, **override) -> dict[str, tuple[int, bytes]]:
    r = {aton.LIST_URL: (200, list_body or _short_list())}
    for code in CODES:
        r[aton.DETAIL_URL.format(code=code)] = (200, (FIX / f"fund_{code}.json").read_bytes())
        r[aton.COST_URL.format(code=code)] = (200, (FIX / f"cost_{code}.json").read_bytes())
        r[aton.PROFIT_URL.format(code=code)] = (200, (FIX / f"profit_{code}.json").read_bytes())
    r.update(override)
    return r


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, _routes())
    funds = [_fund("2487"), _fund("0568-94120101"), _fund("7838", kind="exchange"), _fund("9999", "Нет такого")]
    res = {fd.rules: fd for fd in aton.AtonAdapter().collect(ctx, funds)}
    # каталог один раз + по 3 запроса на фонд (карточка, график, доходность)
    assert len(seen) == 10 and seen.count(aton.LIST_URL) == 1
    assert ctx.http._extra_ca == {aton.HOST: aton.GLOBALSIGN_OV_CA_2018}

    r = res["2487"]
    assert r.status == "ok" and r.currency == "RUB" and r.page_url == "https://www.am-aton.ru/pif/global_markets"
    seg = r.segments[0]
    assert seg.raw_url == aton.COST_URL.format(code="global_markets") and seg.page_url == r.page_url
    assert seg.points[0].date == date(2025, 9, 1) and seg.points[-1].date == date(2026, 11, 3)   # отрезано по since
    assert [p.date for p in r.payouts] == [date(2025, 9, 30), date(2025, 12, 30), date(2026, 3, 31),
                                          date(2026, 6, 30), date(2026, 9, 30)]
    assert r.payouts[-1].amount == 87.86 and r.payouts[-1].source_url == r.page_url
    assert any("с учётом выплат" in w for w in r.warnings)
    assert {o.period: o.value_pct for o in r.official_returns}["1y"] == 14.1

    e = res["0568-94120101"]
    assert e.status == "ok" and e.currency == "RUB" and e.segments[0].points[-1].price == 2142.97
    assert e.payouts == [] and not e.warnings

    d = res["7838"]
    assert d.status == "ok" and d.currency == "USD" and d.segments[0].points[0].date == date(2026, 6, 23)
    assert {o.period for o in d.official_returns} == {"1m", "3m"}

    assert res["9999"].status == "not_found" and "9999" in res["9999"].reason


def test_collect_unread_cards_give_unavailable_not_not_found(tmp_path, monkeypatch):
    # полный каталог, но карточки 16 фондов не открылись: найденные фонды собираются, а про ненайденный
    # нельзя утверждать «нет на сайте»
    ctx, _ = _ctx(tmp_path, monkeypatch, _routes((FIX / "list.json").read_bytes()))
    res = {fd.rules: fd for fd in aton.AtonAdapter().collect(ctx, [_fund("2487"), _fund("9999")])}
    assert res["2487"].status == "ok"
    assert res["9999"].status == "unavailable" and "HTTP 404" in res["9999"].reason


def test_collect_list_down_gives_status_per_fund(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, _routes(**{aton.LIST_URL: (404, b"{}")}))
    res = aton.AtonAdapter().collect(ctx, [_fund("2487"), _fund("7838")])
    assert [fd.status for fd in res] == ["unavailable", "unavailable"]
    assert len(seen) == 1                                             # не долбим упавший сайт


def test_collect_returns_down_keeps_series(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, _routes(**{aton.PROFIT_URL.format(code="amvd"): (404, b"{}")}))
    (fd,) = aton.AtonAdapter().collect(ctx, [_fund("7838")])
    assert fd.status == "ok" and fd.segments and fd.official_returns == []
    assert any("доходность не получена" in w for w in fd.warnings)
