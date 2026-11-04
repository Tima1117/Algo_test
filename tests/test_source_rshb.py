"""РСХБ Управление Активами (rshb): каталог, страницы фондов, ряды /ajax/*.php и выплаты — без сети.

Фикстуры — настоящие ответы www.rshb-am.ru от 03–04.11.2026 (сайт открывается не из всех сетей,
снимали из GitHub Actions тем же вежливым клиентом), урезанные по длине:
* catalog.html — из /paevye-investicionnye-fondy-pif/ только ссылки на страницы фондов (и одна новость
  пресс-центра с «opif» в адресе — она не фонд);
* <slug>.html — со страниц всех 14 фондов: h1, «Опубликовано», div графиков с data-json, шапка дневной
  таблицы (валюта), блок «Динамика стоимости пая» с подсказкой периодов и таблицей доходности (без svg);
* price_<id>.json / nav_<id>.json — /ajax/data.php и /ajax/data-sha.php: для fund=6 («Фонд Облигаций») —
  три первые точки 2013 г. и всё с 01.10.2025; для 10 («Денежный поток») — с 01.09.2025; 51 («СмартАгро») —
  целиком; 23 (RSHU) — ответ на date_from=2025-09-01 и СЧА с 01.09.2025;
  *_6_from_2026-10-20.json — целиком ответы на date_from=2026-10-20 (сервер учитывает date_from);
* datapotok_10.json — /ajax/datapotok.php «Денежного потока»: выплаты и periodYields целиком,
  ряды графика — последние 5 точек.

Контрольные значения: «Фонд Облигаций» — пай 3 281,20 ₽ на 30.09.2026, 3 332,20 ₽ на 30.10.2026,
3 334,34 ₽ и СЧА 26 601 016 805,59 ₽ на 02.11.2026; таблица на странице — +1,55 / +0,90 / +1,61 / +9,66 %
за месяц / 3 / 6 мес. / год по 31.10.2026, и из ряда эти цифры воспроизводятся до 0,01 п. п.
«Денежный поток» — выплата 42,2681 ₽ на пай 30.09.2026 (пай 818,80 -> 776,02 ₽), с выплатами ряд даёт
ровно опубликованные +2,91 / +2,51 / +1,86 / +8,98 %.
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
from pif.normalize import Point
from pif.parsing import UnknownFormat
from pif.returns import period_return
from pif.sources import rshb
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "rshb"
OPIF = "https://www.rshb-am.ru/paevye-investicionnye-fondy-pif/"
BPIF = "https://www.rshb-am.ru/birzhevye-paevye-investicionnye-fondy-bpif/"

# Фонды УК из реестра ЦБ: номер правил -> (краткое название, тип, тикер), как в data/registry.json.
REGISTRY = {
    "4057": ("РСХБ - Индекс МосБиржи - РСПП Вектор устойчивого развития, полной доходности, брутто",
             "exchange", "ESGR"),
    "2566": ("РСХБ - Фонд Сбалансированный", "open", "RU000A0ZZML0"),
    "2795": ("РСХБ – Валютные облигации", "open", None),
    "5585": ("РСХБ – Денежный поток", "open", None),
    "4140": ("РСХБ – Долговой рынок", "open", "RU000A105RJ8"),
    "2797": ("РСХБ – Казначейский", "open", "RU000A0ZZME5"),
    "3906": ("РСХБ – Корпоративные облигации смарт бета", "exchange", "RSHU"),
    "2800": ("РСХБ – Лучшие отрасли", "open", None),
    "6673": ("РСХБ – Новый импульс", "open", None),
    "4362": ("РСХБ – Облигации, вектор И.Эс.Джи", "exchange", "PRIE"),
    "7837": ("РСХБ – СмартАгро", "open", "RU000A10FDU9"),
    "2568": ("РСХБ – Фонд Акций", "open", "RU000A0ZZMN6"),
    "0959-94131180": ("РСХБ – Фонд Компаний малой и средней капитализации", "open", None),
    "2567": ("РСХБ – Фонд Облигаций", "open", "RU000A0ZZMD7"),
}
# Что сопоставление обязано найти: номер правил -> (адрес страницы, id фонда в /ajax/*.php).
EXPECTED = {
    "4057": (BPIF + "bpif-rshb-indeks-mosbirzhi-rspp-vektor-ustoychivogo-razvitiya/", "22"),
    "2566": (OPIF + "opif-fond-sbalansirovannyy/", "4"),
    "2795": (OPIF + "opif-valyutnye-obligacii/", "7"),
    "5585": (OPIF + "opif-denezhnyy-potok/", "10"),
    "4140": (OPIF + "opif-dolgovoy-rynok/", "9"),
    "2797": (OPIF + "opif-kaznacheyskiy/", "5"),
    "3906": (BPIF + "bpif-rshb-korporativnye-obligatsii-smart-beta-rshu/", "23"),
    "2800": (OPIF + "opif-luchshie-otrasli/", "3"),
    "6673": (OPIF + "opif-novyy-impuls/", "35"),
    "4362": (BPIF + "bpif-rshb-obligatsii-vektor-i-es-dzhi/", "36"),
    "7837": (OPIF + "opif-smartagro/", "51"),
    "2568": (OPIF + "opif-fond-aktsiy/", "2"),
    "0959-94131180": (OPIF + "opif-fond-kompaniy-maloy-i-sredney-kapitalizatsii/", "1"),
    "2567": (OPIF + "opif-fond-obligatsiy/", "6"),
}


def read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def load(name: str):
    return json.loads(read(name))


def page_file(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] + ".html"


def make_fund(rules: str, name: str | None = None, *, ticker: str | None = None, ftype: str | None = None,
              status: str = "Сформирован") -> Fund:
    reg_name, reg_type, reg_ticker = REGISTRY.get(rules, (name, "open", None))
    ftype = ftype or reg_type
    return Fund(rules=rules, name=name or reg_name, full_name=name or reg_name, type=ftype, category="",
                status=status, reg_date="2013-03-26", formation_end=None,
                uk_name="ООО «РСХБ Управление Активами»", uk_short="РСХБ УА", uk_ogrn="",
                uk_inn="7704814182", uk_site="https://www.rshb-am.ru", exchange_tradable=ftype == "exchange",
                isin=None, ticker=ticker if ticker is not None else reg_ticker)


def by_date(points):
    return {p.date: p for p in points}


# ---------- каталог и сопоставление 14/14 ----------

def test_catalog_lists_all_funds():
    cat = rshb.parse_catalog(read("catalog.html"))
    assert sorted(cat) == sorted(u for u, _ in EXPECTED.values())          # новость пресс-центра — не фонд
    assert "Фонд Облигаций" in cat[OPIF + "opif-fond-obligatsiy/"]
    assert "РСХБ – Облигации, вектор И.Эс.Джи" in cat[BPIF + "bpif-rshb-obligatsii-vektor-i-es-dzhi/"]


def test_catalog_broken():
    with pytest.raises(UnknownFormat):
        rshb.parse_catalog(read("catalog.html").replace("/opif-", "/fund-").replace("/bpif-", "/fund-"))


def test_match_all_14_by_name_and_page_confirms():
    funds = [make_fund(r) for r in REGISTRY]
    matched = rshb.match_funds(funds, rshb.parse_catalog(read("catalog.html")))
    assert {r: url for r, (url, _) in matched.items()} == {r: url for r, (url, _) in EXPECTED.items()}
    for f in funds:
        page = rshb.parse_fund_page(read(page_file(EXPECTED[f.rules][0])))
        assert rshb.name_key(page.title) == rshb.name_key(f.name), f.rules
        assert page.fund_id == EXPECTED[f.rules][1]
        assert page.currency == "RUB"
        assert page.ticker in (None, f.ticker)


def test_name_key():
    assert rshb.name_key("РСХБ – Фонд Облигаций") == rshb.name_key("ОПИФ «Фонд Облигаций»") == "облигаций"
    assert rshb.name_key("ОПИФ «РСХБ — СмартАгро»") == rshb.name_key("РСХБ – СмартАгро")
    assert rshb.name_key("Фонд Акций") != rshb.name_key("Фонд Облигаций")


def test_match_ambiguous_and_missing():
    cat = rshb.parse_catalog(read("catalog.html"))
    funds = [make_fund("2567"), make_fund("9999", "РСХБ – Фонд Облигаций"), make_fund("8888", "РСХБ – Нет такого")]
    m = rshb.match_funds(funds, cat)
    assert m["2567"][0] is None and m["9999"][0] is None and "нескольким фондам" in m["2567"][1]
    assert m["8888"] == (None, f"в каталоге {rshb.CATALOG_URL} нет фонда с названием «РСХБ – Нет такого»")


# ---------- страница фонда ----------

def test_fund_page_bonds():
    page = rshb.parse_fund_page(read("opif-fond-obligatsiy.html"))
    assert (page.title, page.ticker, page.fund_id, page.currency) == ("ОПИФ «Фонд Облигаций»", None, "6", "RUB")
    assert page.price_params == {"fund": "6", "type": "", "period": "begin", "date_from": "", "date_to": "2026-11-02"}
    assert page.nav_params["fund"] == "6" and page.payouts_params is None
    assert page.returns == [("1m", 1.55, date(2026, 10, 31)), ("3m", 0.9, date(2026, 10, 31)),
                            ("6m", 1.61, date(2026, 10, 31)), ("1y", 9.66, date(2026, 10, 31))]


def test_fund_page_bpif_ticker_in_title():
    page = rshb.parse_fund_page(read("bpif-rshb-korporativnye-obligatsii-smart-beta-rshu.html"))
    assert (page.title, page.ticker, page.fund_id) == ("БПИФ «РСХБ – Корпоративные облигации смарт бета»", "RSHU", "23")
    assert [(p, v) for p, v, _ in page.returns] == [("1m", -2.3), ("3m", 2.26), ("6m", 8.06), ("1y", 4.72)]


def test_fund_page_payouts_source():
    page = rshb.parse_fund_page(read("opif-denezhnyy-potok.html"))
    assert page.payouts_params == {"fund": "10", "type": "pro", "period": "begin", "date_from": "",
                                   "date_to": "2026-11-02"}
    assert [v for _, v, _ in page.returns] == [2.91, 2.51, 1.86, 8.98]


def test_fund_page_young_fund_dash_is_none():
    page = rshb.parse_fund_page(read("opif-smartagro.html"))
    assert [(p, v) for p, v, _ in page.returns] == [("1m", 0.89), ("3m", 2.63), ("6m", None), ("1y", None)]
    url = OPIF + "opif-smartagro/"
    got = rshb.parse_returns_rshb(read("opif-smartagro.html"), url)
    assert [(r.period, r.value_pct, r.as_of, r.source_url) for r in got] == \
        [("1m", 0.89, date(2026, 10, 31), url), ("3m", 2.63, date(2026, 10, 31), url)]


@pytest.mark.parametrize("old,new", [
    ('id="graph-sha"', 'id="graph-other"'),                                   # нет графика СЧА
    ("/ajax/data.php?fund=6", "/ajax/data2.php?fund=6"),                      # другой источник графика
    ("/ajax/data-sha.php?fund=6", "/ajax/data-sha.php?fund=7"),               # графики про разные фонды
    ("<th>Месяц</th>", "<th>Мес.</th>"),                                      # переименовали колонку
    ("Месяц - c 30.09.2026", "Месяц: 30.09.2026"),                            # подсказка без периода
    ("(СЧА), рубли", "(СЧА), тыс. рублей"),                                   # СЧА не в рублях
    ("(РСИП), рубли", "(РСИП), фунты"),                                       # незнакомая валюта
    ("(СЧА), рубли", "(СЧА), доллары США"),                                   # валюты пая и СЧА разные
    ("+9,66%", "+9,66%x"),                                                    # мусор вместо числа
    ("Динамика стоимости пая", "Динамика"),                                   # нет блока доходности
])
def test_fund_page_broken(old, new):
    html = read("opif-fond-obligatsiy.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        rshb.parse_fund_page(html.replace(old, new, 1))


# ---------- ряды стоимости пая и СЧА ----------

def test_series_bonds_values():
    price = by_date(rshb.parse_prices(read("price_6.json")))
    nav = by_date(rshb.parse_navs(read("nav_6.json")))
    assert price[date(2026, 9, 30)].price == 3281.2 and price[date(2026, 10, 30)].price == 3332.2
    assert price[date(2026, 11, 2)].price == 3334.34 and price[date(2026, 11, 2)].nav is None
    assert nav[date(2026, 11, 2)].nav == 26601016805.59 and nav[date(2026, 10, 30)].nav == 26681115555.62
    assert date(2026, 10, 31) not in price                    # выходные не придумываем
    # 1372885200000 = 03.07.2013 21:00 UTC = 04.07.2013 01:00 по Москве (тогда UTC+4): день — по Москве
    assert min(price) == date(2013, 7, 4) and price[date(2013, 7, 4)].price == 1000.0
    dates = [p.date for p in rshb.parse_prices(read("price_6.json"))]
    assert dates == sorted(dates)


def test_series_reproduce_official_returns():
    points = [Point(p.date, None, p.price, 0) for p in rshb.parse_prices(read("price_6.json"))]
    got = [round(period_return(points, date(2026, 10, 31), m), 2) for m in (1, 3, 6, 12)]
    assert got == [1.55, 0.9, 1.61, 9.66]                     # = таблица «Динамика стоимости пая»


def test_series_date_from_answer():
    price = rshb.parse_prices(read("price_6_from_2026-10-20.json"))
    nav = rshb.parse_navs(read("nav_6_from_2026-10-20.json"))
    assert [p.date for p in price] == [p.date for p in nav]
    assert (price[0].date, price[-1].date, len(price)) == (date(2026, 10, 20), date(2026, 11, 2), 10)


def test_series_young_fund_and_bpif():
    agro = rshb.parse_prices(read("price_51.json"))
    assert (agro[0].date, agro[0].price, agro[-1].price) == (date(2026, 6, 4), 999.89, 1035.68)
    assert by_date(rshb.parse_navs(read("nav_23.json")))[date(2026, 11, 2)].nav == 596664668.67


@pytest.mark.parametrize("mutate", [
    lambda d: {"data": d},                                    # объект вместо списка
    lambda d: d.__setitem__(-1, [d[-1][0]]),                  # точка без значения
    lambda d: d.__setitem__(-1, [d[-1][0], 1, 2]),            # лишнее поле
    lambda d: d.__setitem__(-1, [d[-1][0] // 1000, d[-1][1]]),          # секунды вместо миллисекунд
    lambda d: d.__setitem__(-1, [d[-1][0] + 3 * 3600_000, d[-1][1]]),   # полночь UTC, а не Москвы
    lambda d: d.__setitem__(-1, [str(d[-1][0]), d[-1][1]]),             # метка строкой
    lambda d: d.__setitem__(-1, [d[-1][0], "n/a"]),           # мусор вместо числа
    lambda d: d.__setitem__(-1, [d[-1][0], -5.0]),            # отрицательная цена
    lambda d: d.append([d[-1][0], d[-1][1] + 1]),             # две разные цены на дату
])
def test_series_broken(mutate):
    data = copy.deepcopy(load("price_6.json"))
    data = mutate(data) or data
    with pytest.raises(UnknownFormat):
        rshb.parse_prices(data)


def test_series_not_json():
    with pytest.raises(UnknownFormat):
        rshb.parse_navs(b"<html>503</html>")


def test_series_zero_and_null_are_missing():
    data = copy.deepcopy(load("price_6.json"))
    data[-1][1] = 0
    data[-2][1] = None
    pts = rshb.parse_prices(data)
    assert pts[-1].date == date(2026, 10, 29) and all(p.price for p in pts)


# ---------- выплаты дохода ----------

def test_payouts_and_official_returns_with_payouts():
    pays = rshb.parse_payouts(read("datapotok_10.json"), "src")
    assert len(pays) == 12 and pays[0].date == date(2023, 12, 29)
    assert (pays[-1].date, pays[-1].amount) == (date(2026, 9, 30), 42.2681)
    price = by_date(rshb.parse_prices(read("price_10.json")))
    assert (price[date(2026, 9, 29)].price, price[date(2026, 9, 30)].price) == (818.8, 776.02)
    points = [Point(d, None, p.price, 0) for d, p in sorted(price.items())]
    payouts = [{"date": p.date.isoformat(), "amount": p.amount} for p in pays]
    got = [round(period_return(points, date(2026, 10, 31), m, payouts), 2) for m in (1, 3, 6, 12)]
    assert got == [2.91, 2.51, 1.86, 8.98]                    # = таблица на странице «Денежного потока»


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("payouts"),
    lambda d: d.__setitem__("payouts", {"x": 1}),
    lambda d: d["payouts"][0].pop("amount"),
    lambda d: d["payouts"][0].__setitem__("amount", 0),
    lambda d: d["payouts"].append(dict(d["payouts"][0])),
])
def test_payouts_broken(mutate):
    data = copy.deepcopy(load("datapotok_10.json"))
    mutate(data)
    with pytest.raises(UnknownFormat):
        rshb.parse_payouts(data, "src")


# ---------- сбор целиком (без сети) ----------

class FakeHttp:
    """Вместо сети — файлы фикстур. Нет фикстуры ряда — как будто сайт не ответил."""

    def __init__(self, files: dict[tuple[str, str | None], str] | None = None, errors: dict | None = None):
        self.files = files or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dict | None, list | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw):
        self.calls.append((url, dict(params) if params else None, funds))
        if url in self.errors:
            raise self.errors[url]
        if url == rshb.CATALOG_URL:
            name = "catalog.html"
        elif url in (rshb.PRICE_API, rshb.NAV_API, rshb.PAYOUTS_API):
            kind = {rshb.PRICE_API: "price", rshb.NAV_API: "nav", rshb.PAYOUTS_API: "datapotok"}[url]
            name = self.files.get((kind, params["fund"]), f"{kind}_{params['fund']}.json")
        else:
            name = self.files.get((url, None), page_file(url))
        path = FIX / name
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        if not path.exists():
            raise FetchError(full, "сайт недоступен (ConnectTimeout)")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-04T12:00:00+00:00", fmt)


def run(http, funds, since=date(2025, 9, 1), last_dates=None):
    ctx = Context(http=http, since=since, log=logging.getLogger("test"), last_dates=last_dates or {})
    return {fd.rules: fd for fd in rshb.RshbAdapter().collect(ctx, funds)}


def test_collect_end_to_end():
    funds = [make_fund(r) for r in ("2567", "5585", "7837", "3906", "2568")] + \
            [make_fund("9999", "РСХБ – Нет такого фонда")]
    http = FakeHttp()
    res = run(http, funds, since=date(2026, 10, 1))
    today = date.today().isoformat()

    bonds = res["2567"]
    assert (bonds.status, bonds.currency, bonds.page_url) == ("ok", "RUB", OPIF + "opif-fond-obligatsiy/")
    nav_seg, price_seg = bonds.segments
    assert nav_seg.page_url == bonds.page_url and "/ajax/data-sha.php?fund=6" in nav_seg.raw_url
    assert nav_seg.points[0].date == date(2026, 10, 1)                 # отрезано по since
    assert (price_seg.points[-1].date, price_seg.points[-1].price, price_seg.points[-1].nav) == \
        (date(2026, 11, 2), 3334.34, None)
    assert [(r.period, r.value_pct, r.as_of) for r in bonds.official_returns] == \
        [("1m", 1.55, date(2026, 10, 31)), ("3m", 0.9, date(2026, 10, 31)),
         ("6m", 1.61, date(2026, 10, 31)), ("1y", 9.66, date(2026, 10, 31))]
    assert bonds.official_returns[0].source_url == bonds.page_url and bonds.payouts == [] and not bonds.warnings
    # запрос как на странице, но с since и сегодняшней датой
    assert (rshb.PRICE_API, {"fund": "6", "type": "", "period": "begin", "date_from": "2026-10-01",
                             "date_to": today}, ["2567"]) in http.calls

    potok = res["5585"]
    assert potok.status == "ok" and [(p.date, p.amount) for p in potok.payouts] == []     # выплата 30.09 < since
    assert any(c[0] == rshb.PAYOUTS_API and c[1]["fund"] == "10" and c[1]["date_to"] == today for c in http.calls)

    agro = res["7837"]
    assert agro.status == "ok" and [r.period for r in agro.official_returns] == ["1m", "3m"]
    assert any("6m, 1y" in w for w in agro.warnings)

    assert res["3906"].status == "ok" and res["3906"].segments[1].points[-1].price == 131.64
    assert res["2568"].status == "unavailable"                         # ряд «Фонда Акций» не ответил
    assert res["9999"].status == "not_found" and "Нет такого фонда" in res["9999"].reason
    assert sum(1 for c in http.calls if c[0] == rshb.CATALOG_URL) == 1  # каталог — один раз


def test_collect_payouts_since_and_tail():
    res = run(FakeHttp(), [make_fund("5585")], since=date(2025, 9, 1))
    assert [(p.date, p.amount) for p in res["5585"].payouts][:2] == [(date(2025, 9, 30), 48.9635),
                                                                     (date(2025, 12, 30), 33.3873)]
    assert len(res["5585"].payouts) == 5

    # докачка: с последней собранной точки минус нахлёст
    http = FakeHttp({("price", "6"): "price_6_from_2026-10-20.json", ("nav", "6"): "nav_6_from_2026-10-20.json"})
    fd = run(http, [make_fund("2567")], last_dates={"2567": date(2026, 10, 30)})["2567"]
    assert fd.status == "ok" and http.calls[2][1]["date_from"] == "2026-10-20"
    assert [len(s.points) for s in fd.segments] == [10, 10]


def test_collect_page_of_other_fund_is_not_found():
    http = FakeHttp({(OPIF + "opif-fond-obligatsiy/", None): "opif-fond-aktsiy.html"})
    fd = run(http, [make_fund("2567")])["2567"]
    assert fd.status == "not_found" and "другой фонд" in fd.reason
    assert all(c[0] not in (rshb.PRICE_API, rshb.NAV_API) for c in http.calls)


def test_collect_ticker_mismatch_is_not_found():
    fd = run(FakeHttp(), [make_fund("3906", ticker="RSHB")])["3906"]
    assert fd.status == "not_found" and "RSHU" in fd.reason


def test_collect_empty_series(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    files = {("price", "6"): str(empty), ("nav", "6"): str(empty)}     # абсолютный путь побеждает FIX /
    assert run(FakeHttp(files), [make_fund("2567")])["2567"].status == "no_history"
    fd = run(FakeHttp(files), [make_fund("2567")], last_dates={"2567": date(2026, 10, 30)})["2567"]
    assert fd.status == "unavailable" and "раньше были" in fd.reason


def test_collect_broken_series_is_parse_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"error": "fund not found"}')
    fd = run(FakeHttp({("nav", "6"): str(bad)}), [make_fund("2567")])["2567"]
    assert fd.status == "parse_error" and fd.errors


def test_collect_catalog_down():
    http = FakeHttp(errors={rshb.CATALOG_URL: FetchError(rshb.CATALOG_URL, "сайт недоступен (ConnectTimeout)")})
    res = run(http, [make_fund("2567"), make_fund("3906")])
    assert {fd.status for fd in res.values()} == {"unavailable"}
    assert sum(1 for c in http.calls if c[0] == rshb.CATALOG_URL) == 1
