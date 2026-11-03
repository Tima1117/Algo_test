"""УК «ДОХОДЪ» (dohod): разбор настоящих ответов www.dohod.ru (снято 03.11.2026, урезано по длине), без сети.

Контрольные значения сверены на сайте:
- «Прирост расчетной стоимости пая согласно 5609-У» (раздел раскрытия <код>/unit-value), таблица
  «Ежедневное изменение стоимости пая и СЧА»: «Дивидендные акции. Россия» — пай 3 282,44 / 3 222,08 и
  СЧА 33 143 204,02 / 32 552 517,03 на 30.10 / 02.11.2026; GROD — 747,06 / 736,15 и
  193 552 721,89 / 190 726 541,64;
- страница фонда «Дивидендные акции. Россия», «Прирост расчетной стоимости пая на 02 ноября 2026»:
  1 мес. ↑7,93, 3 мес. ↑7,08, 6 мес. ↓1,34, 1 год ↑5,29;
- раздел official-information: «Номер правил доверительного управления» 0238-58232991 и 4444.
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
from pif.sources.base import Context
from pif.sources.dohod import (CHART_URL, DISCLOSURE_URL, LIST_PAGES, DohodAdapter, _msk_day, parse_chart,
                               parse_fund_links, parse_fund_page, parse_returns_fund_page, parse_rules_number,
                               parse_series)

FIX = Path(__file__).parent / "fixtures" / "dohod"
DA_URL = "https://www.dohod.ru/individuals/mutual-funds/open-funds/da"
GROD_URL = "https://www.dohod.ru/individuals/mutual-funds/bpif/grod"


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def text(name: str) -> str:
    return read(name).decode("utf-8")


def load(name: str):
    return json.loads(read(name))


# ---------- каталог: списки, страница фонда, номер правил ----------

def test_fund_links_only_fund_cards():
    # в списке есть и ссылки на «Автопополнение» / «Детский портфель» — это не фонды
    assert parse_fund_links(read("list_open.html")) == [DA_URL]
    assert parse_fund_links(read("list_bpif.html")) == [GROD_URL]
    with pytest.raises(UnknownFormat):
        parse_fund_links("<html><body><a href='/individuals/mutual-funds/open-funds/da'>x</a></body></html>")


def test_fund_page():
    da = parse_fund_page(read("fund_da.html"), DA_URL)
    assert (da.title, da.res_id, da.code, da.currency) == ("Дивидендные акции. Россия", "5", "di", "RUB")
    grod = parse_fund_page(read("fund_grod.html"), GROD_URL)
    assert (grod.title, grod.res_id, grod.code, grod.currency) == ("GROD", "889", "grod", "RUB")


@pytest.mark.parametrize("cut", ['data-res-id="5"', "information-disclosure/mutual-fund-reports/di/",
                                 '<span class="about-header">СЧА</span>'])
def test_fund_page_broken(cut):
    html = text("fund_da.html")
    assert cut in html
    with pytest.raises(UnknownFormat):
        parse_fund_page(html.replace(cut, ""))


def test_rules_number():
    assert parse_rules_number(read("oi_di.html")) == "0238-58232991"
    assert parse_rules_number(read("oi_grod.html")) == "4444"
    with pytest.raises(UnknownFormat):
        parse_rules_number(text("oi_grod.html").replace("Номер правил доверительного управления", "Номер"))
    with pytest.raises(UnknownFormat):
        parse_rules_number(text("oi_grod.html").replace(">4444<", ">№ 4444-б<"))


# ---------- ряды СЧА и пая ----------

def test_series_opif():
    pts = {p.date: p for p in parse_series(read("chart_5_pai.json"), read("chart_5_cha.json"))}
    assert (pts[date(2026, 10, 30)].nav, pts[date(2026, 10, 30)].price) == (33143204.02, 3282.44)
    assert (pts[date(2026, 11, 2)].nav, pts[date(2026, 11, 2)].price) == (32552517.03, 3222.08)
    assert min(pts) == date(2017, 10, 25)          # история с запуска стратегии, режет segment(since)
    assert date(2026, 10, 31) not in pts and date(2026, 11, 1) not in pts   # выходные не придумываем


def test_series_bpif_from_opif_endpoint():
    # для БПИФ costdynamics/opif тоже отдаёт СЧА и пай — числа как в таблице 5609-У
    pts = {p.date: p for p in parse_series(read("chart_889_pai.json"), read("chart_889_cha.json"))}
    assert (pts[date(2026, 10, 30)].nav, pts[date(2026, 10, 30)].price) == (193552721.89, 747.06)
    assert (pts[date(2026, 11, 2)].nav, pts[date(2026, 11, 2)].price) == (190726541.64, 736.15)


def test_dates_are_moscow_days():
    # у ОПИФ метка — полночь UTC, у БПИФ (action=bpif) — полночь МСК = 21:00 UTC предыдущего дня
    assert _msk_day(1793577600000) == date(2026, 11, 2)     # 2026-11-02T00:00Z
    assert _msk_day(1793566800000) == date(2026, 11, 2)     # 2026-11-01T21:00Z
    with pytest.raises(UnknownFormat):
        _msk_day("2026-11-02")


def test_chart_duplicates():
    data = load("chart_5_pai.json")
    last = data["items"][-1]
    same = {"items": data["items"] + [list(last)]}
    points, bad = parse_chart(same)
    assert bad == [] and points[-1] == (date(2026, 11, 2), 3222.08)
    other = {"items": data["items"] + [[last[0], 3300.0]]}
    points, bad = parse_chart(other)
    assert bad == [date(2026, 11, 2)] and points[-1][0] == date(2026, 10, 30)


def test_chart_zero_is_missing_not_value():
    data = load("chart_5_cha.json")
    data["items"][-1][1] = 0
    points, _ = parse_chart(data)
    assert points[-1][0] == date(2026, 10, 30)


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("items"),                                           # нет поля
    lambda d: d.update(items={"Пай по СЧА": d["items"]}),               # формат action=bpif
    lambda d: d["items"][-1].pop(),                                     # [время] без значения
    lambda d: d["items"][-1].__setitem__(1, "н/з"),                     # не число
    lambda d: d["items"][-1].__setitem__(1, -5),                        # отрицательное
])
def test_chart_broken(mutate):
    data = copy.deepcopy(load("chart_5_pai.json"))
    mutate(data)
    with pytest.raises(UnknownFormat):
        parse_chart(data)


# ---------- официальная доходность ----------

def test_returns_opif():
    rets = parse_returns_fund_page(read("fund_da.html"), DA_URL)
    assert {r.period: r.value_pct for r in rets} == {"1m": 7.93, "3m": 7.08, "6m": -1.34, "1y": 5.29}
    assert {r.as_of for r in rets} == {date(2026, 11, 2)} and {r.source_url for r in rets} == {DA_URL}


def test_returns_bpif_is_exchange_price():
    # на странице БПИФ — «Прирост биржевой стоимости пая»: это не расчётная стоимость, не берём
    assert parse_returns_fund_page(read("fund_grod.html")) is None


@pytest.mark.parametrize("cut", ['<span>3 мес.</span>', 'на 02 ноября 2026', '<span class="arrow loss">↓</span>'])
def test_returns_broken(cut):
    html = text("fund_da.html")
    assert cut in html
    with pytest.raises(UnknownFormat):
        parse_returns_fund_page(html.replace(cut, "", 1))


# ---------- сбор целиком (без сети) ----------

class FakeHttp:
    """Вместо сети — файлы фикстур; errors: url -> исключение."""

    def __init__(self, errors: dict | None = None):
        self.errors = errors or {}
        self.calls: list[tuple[str, dict | None, list | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw):
        self.calls.append((url, params, funds))
        if url in self.errors:
            raise self.errors[url]
        names = {LIST_PAGES[0]: "list_open.html", LIST_PAGES[1]: "list_bpif.html",
                 DA_URL: "fund_da.html", GROD_URL: "fund_grod.html",
                 DISCLOSURE_URL.format(code="di", page="official-information"): "oi_di.html",
                 DISCLOSURE_URL.format(code="grod", page="official-information"): "oi_grod.html"}
        if url == CHART_URL:
            assert params["action"] == "costdynamics/opif"
            name = f"chart_{params['resId']}_{params['tab']}.json"
        else:
            name = names[url]
        path = FIX / name
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def make_fund(rules: str, name: str, type_: str = "open", status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type=type_, category="", status=status,
                reg_date="2004-07-21", formation_end=None, uk_name="", uk_short="ДОХОДЪ", uk_ogrn="",
                uk_inn="7826685368", uk_site="https://www.dohod.ru", exchange_tradable=type_ == "exchange")


FUNDS = [make_fund("0238-58232991", "ДОХОДЪ. Дивидендные акции. Россия"),
         make_fund("4444", "ДОХОДЪ Индекс акций роста", "exchange"),
         make_fund("9999", "ДОХОДЪ. Нет на сайте")]


def run(http, funds=FUNDS):
    ctx = Context(http=http, since=date(2026, 9, 1), log=logging.getLogger("test"))
    return {fd.rules: fd for fd in DohodAdapter().collect(ctx, funds)}


def test_collect_end_to_end():
    http = FakeHttp()
    res = run(http)
    da, grod, missing = res["0238-58232991"], res["4444"], res["9999"]

    assert (da.status, da.currency, da.page_url) == ("ok", "RUB", DA_URL)
    price_seg, nav_seg = da.segments
    assert "tab=pai" in price_seg.raw_url and "tab=cha" in nav_seg.raw_url
    assert all(p.nav is None for p in price_seg.points) and all(p.price is None for p in nav_seg.points)
    assert price_seg.points[0].date == date(2026, 9, 1)              # отрезано по since
    assert (nav_seg.points[-1].date, nav_seg.points[-1].nav) == (date(2026, 11, 2), 32552517.03)
    assert price_seg.page_url == DA_URL
    assert {r.period: r.value_pct for r in da.official_returns}["6m"] == -1.34

    assert grod.status == "ok" and grod.official_returns == []
    assert any("5609-У" in w and "grod/unit-value" in w for w in grod.warnings)
    assert grod.segments[0].points[-1].price == 736.15

    assert missing.status == "not_found" and "9999" in missing.reason
    # страница фонда и раздел раскрытия помечены в манифесте номером правил фонда
    assert (DA_URL, None, ["0238-58232991"]) in http.calls


def test_collect_list_page_down():
    res = run(FakeHttp(errors={LIST_PAGES[0]: FetchError(LIST_PAGES[0], "сайт не отвечает")}))
    assert {fd.status for fd in res.values()} == {"unavailable"}


def test_collect_broken_fund_page_is_not_silent():
    # страница одного фонда не разобралась: фонд не «не найден», а с причиной; второй собран
    res = run(FakeHttp(errors={GROD_URL: FetchError(GROD_URL, "HTTP 500")}))
    assert res["4444"].status == "unavailable" and res["0238-58232991"].status == "ok"
