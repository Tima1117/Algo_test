"""Т-Капитал (t_capital): разбор настоящих ответов t-capital-funds.ru и его API (снято 03.11.2026,
урезано по длине), без сети.

Контрольные значения сверены на сайте:
- «Статистика фондов» (https://t-capital-funds.ru/statistics/): на 30.10.2026 TMOS — СЧА 15 268 739 458,96 ₽,
  пай 6,3065 ₽; TGLD — СЧА 171 917 982,32 $, пай 0,1530 $; на 02.11.2026 пай TMOS 6,1626 ₽;
- КИД TMOS на 30.09.2026: СЧА 13 586 534 817,03 руб., расчётная стоимость пая 5,6597 руб.;
- раскрытие /documents/mutual_funds/: «Правила доверительного управления № 4100» у папки TMOS,
  «… зарегистрированы Банком России 22.06.2026 за № 7909» у TRES.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import ssl
from datetime import date
from pathlib import Path

import pytest

from pif.http import Fetcher, FetchError, Manifest, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.t_capital import (API_HOST, DYNAMIC_API, FOLDERS_PAGE, NAV_API, PRICE_API, RELEASES_PAGE,
                                   RU_ROOT_CA, STATS_PAGE, NoData, TCapitalAdapter, _trust_ru_root_ca,
                                   parse_dynamic, parse_fair_price, parse_folders, parse_nav,
                                   parse_payout_notices)

FIX = Path(__file__).parent / "fixtures" / "t_capital"


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def load(name: str):
    return json.loads(read(name))


# ---------- раскрытие: номер правил -> тикер ----------

def test_folders_rules_to_ticker():
    folders = parse_folders(read("mutual_funds.html"))
    got = {r: folders[r].ticker for r in ("4100", "4098", "6287", "7909", "4677", "3898", "8150", "5435-СД")}
    assert got == {"4100": "TMOS", "4098": "TGLD", "6287": "TPAY", "7909": "TRES", "4677": "RU000A109KH9",
                   "3898": "TUSD", "8150": "TCNY", "5435-СД": "TRBR"}
    assert folders["4100"].name == "БПИФ РФИ «Т-Капитал Индекс МосБиржи»"


def test_folders_broken():
    html = read("mutual_funds.html").decode("utf-8")
    with pytest.raises(UnknownFormat):
        parse_folders(html.replace("__REACT_QUERY_STATE__capital", "__STATE__"))
    with pytest.raises(UnknownFormat):
        parse_folders(html.replace('"getFolderTree"', '"getSomethingElse"'))
    with pytest.raises(UnknownFormat):          # описание без «№ …» у всех папок
        parse_folders(html.replace("№", "N"))


# ---------- статистика: валюта ----------

def test_dynamic_currency():
    dyn = parse_dynamic(read("dynamic.json"))
    assert {t: dyn[t].currency for t in ("TMOS", "TGLD", "TBEU", "TPAY")} == \
        {"TMOS": "RUB", "TGLD": "USD", "TBEU": "EUR", "TPAY": "RUB"}
    assert dyn["TMOS"].isin == "RU000A101X76"


@pytest.mark.parametrize("mutate", [
    lambda d: d["payload"]["funds"][0].__setitem__("currency", "GBP"),
    lambda d: d["payload"]["funds"][0].pop("ticker"),
    lambda d: d["payload"]["funds"][0].__setitem__("metrics", []),
    lambda d: d.__setitem__("status", "Error"),
    lambda d: d["payload"].__setitem__("funds", []),
])
def test_dynamic_broken(mutate):
    data = copy.deepcopy(load("dynamic.json"))
    mutate(data)
    with pytest.raises(UnknownFormat):
        parse_dynamic(data)


# ---------- история СЧА и пая ----------

def test_history_tmos():
    nav = {p.date: p.nav for p in parse_nav(read("nav_TMOS.json"))}
    price = {p.date: p.price for p in parse_fair_price(read("price_TMOS.json"))}
    assert nav[date(2026, 9, 30)] == 13586534817.03 and price[date(2026, 9, 30)] == 5.6597     # КИД
    assert nav[date(2026, 10, 30)] == 15268739458.96 and price[date(2026, 10, 30)] == 6.3065    # статистика
    assert price[date(2026, 11, 2)] == 6.1626
    assert date(2026, 10, 31) not in nav            # выходные не придумываем
    assert all(p.price is None for p in parse_nav(read("nav_TMOS.json")))


def test_history_gold_in_usd():
    nav = {p.date: p.nav for p in parse_nav(read("nav_TGLD.json"))}
    price = {p.date: p.price for p in parse_fair_price(read("price_TGLD.json"))}
    assert nav[date(2026, 10, 30)] == 171917982.32 and price[date(2026, 10, 30)] == 0.153


def test_history_no_data_is_not_format_error():
    # формирующийся фонд: API отвечает статусом Error и текстом
    with pytest.raises(NoData, match="Нет информации"):
        parse_nav(read("error_TCNY.json"))


@pytest.mark.parametrize("mutate", [
    lambda d: d["payload"].pop("nav"),
    lambda d: d["payload"]["nav"][0].pop("value"),
    lambda d: d["payload"]["nav"][0].__setitem__("date", "30/02/2026x"),
    lambda d: d["payload"]["nav"][0].__setitem__("value", -1.0),
    lambda d: d["payload"]["nav"].append({**d["payload"]["nav"][0], "value": 1.0}),   # две разные СЧА на дату
    lambda d: d.__setitem__("status", "Maybe"),
])
def test_history_broken(mutate):
    data = copy.deepcopy(load("nav_TMOS.json"))
    mutate(data)
    with pytest.raises(UnknownFormat):
        parse_nav(data)


def test_history_zero_is_missing():
    data = copy.deepcopy(load("price_TMOS.json"))
    data["payload"]["fairPrice"][-1]["value"] = 0
    assert parse_fair_price(data)[-1].date == date(2026, 10, 30)


# ---------- сообщения о выплате дохода ----------

def test_payout_notices():
    notices = parse_payout_notices(read("releases.html"))
    assert list(notices) == ["т капитал пассивный доход"]
    tpay = notices["т капитал пассивный доход"]
    assert len(tpay) == 6
    assert tpay[0]["link"].endswith("soobshenie-o-vyplate-dohoda-tpay-03-11-2026.pdf")
    assert tpay[0]["available"] == date(2026, 11, 3)


# ---------- доверие к НУЦ Минцифры только для API ----------

def test_embedded_root_ca_fingerprint():
    der = ssl.PEM_cert_to_DER_cert(RU_ROOT_CA)
    assert hashlib.sha256(der).hexdigest().upper() == (
        "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31")


def test_trust_only_api_host(tmp_path):
    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path)
    try:
        assert _trust_ru_root_ca(http) is True
        api_client = http._client(API_HOST)
        assert api_client is not http._client("t-capital-funds.ru")
        assert _trust_ru_root_ca(http) is True and http._client(API_HOST) is api_client   # идемпотентно
        assert api_client.headers["User-Agent"] == http._client("t-capital-funds.ru").headers["User-Agent"]
    finally:
        http.close()
    assert _trust_ru_root_ca(object()) is False         # не Fetcher


# ---------- сбор целиком (без сети) ----------

class FakeHttp:
    """Вместо сети — файлы фикстур; no_data: тикеры, по которым API отвечает «нет информации»."""

    def __init__(self, errors: dict | None = None, no_data: set | None = None):
        self.errors = errors or {}
        self.no_data = no_data or set()
        self.calls: list[tuple[str, dict | None, list | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw):
        self.calls.append((url, params, funds))
        if url in self.errors:
            raise self.errors[url]
        if url in (NAV_API, PRICE_API):
            t = params["ticker"]
            name = "error_TCNY.json" if t in self.no_data else f"{'nav' if url == NAV_API else 'price'}_{t}.json"
        else:
            name = {FOLDERS_PAGE: "mutual_funds.html", RELEASES_PAGE: "releases.html",
                    DYNAMIC_API: "dynamic.json"}[url]
        path = FIX / name
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def make_fund(rules: str, name: str, isin: str | None = None, status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type="exchange", category="", status=status,
                reg_date="2020-07-13", formation_end=None, uk_name="", uk_short="Т-Капитал", uk_ogrn="",
                uk_inn="7743304530", uk_site="https://t-capital-funds.ru/", exchange_tradable=True, isin=isin)


def run(http, funds, since=date(2026, 10, 1), last_dates=None):
    ctx = Context(http=http, since=since, log=logging.getLogger("test"), last_dates=last_dates or {})
    return {fd.rules: fd for fd in TCapitalAdapter().collect(ctx, funds)}


def test_collect_end_to_end():
    funds = [make_fund("4100", "Т-Капитал Индекс МосБиржи", "RU000A101X76"),
             make_fund("4098", "Т-Капитал Золото", "RU000A101X50"),
             make_fund("6287", "Т-Капитал Пассивный Доход", "RU000A108WX3"),
             make_fund("7909", "Т-Капитал Денежный рынок 2"),
             make_fund("8150", "Т-Капитал Юаневая ликвидность", status="Формируется"),
             make_fund("4099", "Т-Капитал Технологии"),          # есть в статистике, а API — «нет информации»
             make_fund("5665", "Заблокированные активы"),         # есть в раскрытии, нет в статистике
             make_fund("9999", "Нет такого фонда")]
    http = FakeHttp(no_data={"TECH"})
    res = run(http, funds, last_dates={"4100": date(2026, 10, 30)})

    tmos = res["4100"]
    assert (tmos.status, tmos.currency) == ("ok", "RUB")
    assert tmos.page_url == "https://t-capital-funds.ru/documents/mutual_funds/TMOS/"
    nav_seg, price_seg = tmos.segments
    assert nav_seg.page_url == STATS_PAGE and "portfolio/history/nav" in nav_seg.raw_url
    assert nav_seg.points[0].date == date(2026, 10, 1)                # отрезано по since
    assert price_seg.points[-1].price == 6.1626 and price_seg.points[-1].nav is None
    assert tmos.official_returns == [] and any("КИД" in w for w in tmos.warnings)
    # докачка: с последней собранной точки минус нахлёст
    assert (NAV_API, {"ticker": "TMOS", "from": "2026-10-20"}, ["4100"]) in http.calls

    assert res["4098"].currency == "USD" and res["4098"].status == "ok"
    assert any("выплачивает доход" in w for w in res["6287"].warnings)
    assert res["6287"].payouts == []
    assert res["7909"].status == "ok" and res["7909"].segments[0].points[-1].date == date(2026, 10, 30)
    assert res["8150"].status == "not_formed" and "TCNY" in res["8150"].reason
    assert res["4099"].status == "no_history" and "Нет информации" in res["4099"].reason
    assert res["5665"].status == "no_history" and "TECH2" in res["5665"].reason
    assert res["9999"].status == "not_found"


def test_collect_isin_mismatch_is_error():
    res = run(FakeHttp(), [make_fund("4100", "Т-Капитал Индекс МосБиржи", "RU000A0000X0")])
    assert res["4100"].status == "parse_error"


def test_collect_api_down():
    res = run(FakeHttp(errors={DYNAMIC_API: FetchError(DYNAMIC_API, "сайт недоступен (ConnectError)")}),
              [make_fund("4100", "Т-Капитал Индекс МосБиржи")])
    assert res["4100"].status == "unavailable"
