"""УК ПСБ (upravlyaem.ru): каталог, страница фонда, выгрузка CSV и выплаты дохода.

Фикстуры — настоящие ответы сайта 2026-11-03, урезанные по длине:
* catalog.html — из /funds/mutual_funds/ только короткие ссылки меню /funds/<slug>/;
* fund_*.html — со страниц фондов только h1, таблица «Динамика фонда», блоки графиков
  (атрибуты data-json / data-json-payment) и ссылка на выгрузку CSV;
* costs_*_full.csv — первые 300 строк выгрузки (с 02.11.2026 назад до августа 2025);
  costs_998_head_tail.csv — начало и конец выгрузки «Курса на Восток» (последняя строка —
  день формирования без значений);
* datav_27.json — выплаты дохода «Дивидендных акций» целиком.
Значения сверены со страницей фонда: «Финансовая подушка» — 8 477.13 ₽ на 30.10.2026 в таблице
«Динамика фонда» и за 1/3/6 мес. и 1 год +0.98 / +3.14 / +6.44 / +15.45 %.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import psb
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "psb"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _point(points, d: date):
    [p] = [p for p in points if p.date == d]
    return p


def _fund(rules: str, name: str, *, ticker: str | None = None, ftype: str = "open") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ПИФ «{name}»", type=ftype, category="",
                status="Сформирован", reg_date="2004-09-10", formation_end="2004-12-21",
                uk_name="ООО «УК ПРОМСВЯЗЬ»", uk_short="УК ПСБ", uk_ogrn="1027718000067",
                uk_inn="7718218817", uk_site="www.upravlyaem.ru", exchange_tradable=ftype == "exchange",
                isin=None, ticker=ticker)


# ---------- каталог и страница фонда ----------
def test_catalog_lists_all_funds_with_names():
    cat = psb.parse_catalog(_read("catalog.html"))
    assert len(cat) == 16
    assert "Финансовая подушка" in cat["https://www.upravlyaem.ru/funds/Finansovaya-podushka/"]
    assert "ПСБ – Денежный рынок - PSMM" in cat["https://www.upravlyaem.ru/funds/denezhnyy-rynok/"]
    # служебные разделы каталога — не фонды
    assert not any(u.endswith(("/mutual_funds/", "/bpif/")) for u in cat)


def test_fund_page_returns_and_csv_link():
    page = psb.parse_fund_page(_read("fund_finansovaya_podushka.html"))
    assert page.title == "Финансовая подушка"
    assert page.csv_url == "https://www.upravlyaem.ru/upload/costs/costs_412_full.csv"
    assert page.currency == "RUB"
    assert page.as_of == date(2026, 10, 30) and page.price_as_of == 8477.13
    assert page.returns == {"1m": 0.98, "3m": 3.14, "6m": 6.44, "1y": 15.45}
    assert page.payout_fund is None

    rets = psb.parse_returns_psb(_read("fund_finansovaya_podushka.html"), "https://x/")
    assert [(r.period, r.value_pct, r.as_of) for r in rets] == [
        ("1m", 0.98, date(2026, 10, 30)), ("3m", 3.14, date(2026, 10, 30)),
        ("6m", 6.44, date(2026, 10, 30)), ("1y", 15.45, date(2026, 10, 30))]


def test_fund_page_with_payouts_and_etf_title():
    div = psb.parse_fund_page(_read("fund_dividendnye_aktsii.html"))
    assert div.payout_fund == "27"
    assert div.returns == {"1m": 10.02, "3m": 7.67, "6m": -2.79, "1y": 4.0}
    gold = psb.parse_fund_page(_read("fund_zolotoy_rynok.html"))
    assert gold.title == "ПСБ – Золотой рынок - PSGM" and gold.price_as_of == 11.7983


def test_fund_page_broken_markup_is_unknown_format():
    html = _read("fund_finansovaya_podushka.html")
    with pytest.raises(UnknownFormat, match="Динамика фонда"):
        psb.parse_fund_page(html.replace('class="fund-dynamics"', 'class="fund-other"'))
    with pytest.raises(UnknownFormat, match="нет колонок"):
        psb.parse_fund_page(html.replace("За 3 мес.", "За квартал"))
    with pytest.raises(UnknownFormat, match="costs"):
        psb.parse_fund_page(html.replace("costs_412_full.csv", "costs_412_full.xlsx"))
    with pytest.raises(UnknownFormat, match="валюту"):
        psb.parse_fund_page(html.replace("Стоимость пая, руб.", "Стоимость пая, у.е."))


# ---------- выгрузка CSV ----------
def test_costs_csv_values_match_site():
    pts = psb.parse_costs_csv((FIX / "costs_412_full.csv").read_bytes())
    assert_series(pts, "psb_costs_412")
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    p = _point(pts, date(2026, 10, 30))          # = «8 477.13 на 30.10.2026» в «Динамике фонда»
    assert p.price == 8477.13 and p.nav == 10224758280.53
    p = _point(pts, date(2026, 11, 2))
    assert p.price == 8485.05 and p.nav == 10233544079.89
    p = _point(pts, date(2025, 9, 1))
    assert p.price == 7343.62 and p.nav == 4766056958.99
    assert len(pts) == 300


def test_costs_csv_skips_formation_day_without_values():
    pts = psb.parse_costs_csv(_read("costs_998_head_tail.csv"))
    assert_series(pts, "psb_costs_998_head_tail")
    assert pts[0].date == date(2023, 1, 27) and pts[0].price == 999.94
    assert date(2023, 1, 24) not in {p.date for p in pts}
    assert _point(pts, date(2026, 11, 2)).nav == 1474861872.01


def test_costs_csv_broken_is_unknown_format():
    text = _read("costs_412_full.csv")
    with pytest.raises(UnknownFormat, match="3 колонки"):           # пропала колонка СЧА
        psb.parse_costs_csv("\n".join(",".join(line.split(",")[:2]) for line in text.splitlines()))
    with pytest.raises(UnknownFormat, match="заголовок"):
        psb.parse_costs_csv("Дата,Пай,СЧА\n" + text)
    with pytest.raises(UnknownFormat, match="порядок колонок"):     # колонки переставили
        psb.parse_costs_csv("\n".join(",".join([c[0], c[2], c[1]]) for c in
                                      (line.split(",") for line in text.splitlines())))
    with pytest.raises(UnknownFormat, match="не число"):
        psb.parse_costs_csv(text.replace("8477.13", "8477.13 USD"))


# ---------- выплаты ----------
def test_payouts_values_and_dates():
    pays = psb.parse_payouts_psb((FIX / "datav_27.json").read_bytes(), "https://x/datav")
    assert len(pays) == 14 and pays[0].date == date(2023, 6, 30)
    last = pays[-1]
    # 30.09.2026: в выгрузке пай уже без дохода — 767,49 ₽ (29.09 — 837,16 ₽), выплата 71,54 ₽
    assert last.date == date(2026, 9, 30) and last.amount == 71.53905 and last.source_url == "https://x/datav"


def test_payouts_broken_is_unknown_format():
    text = _read("datav_27.json")
    with pytest.raises(UnknownFormat, match="ap"):
        psb.parse_payouts_psb(text.replace('"ap"', '"amount"'), "u")
    # метка «полночь UTC» вместо полуночи по Москве — дата бы съехала на день
    with pytest.raises(UnknownFormat, match="полночь"):
        psb.parse_payouts_psb(text.replace("1790715600000", "1790726400000"), "u")


# ---------- сбор целиком (без сети) ----------
ROUTES = {
    "/funds/mutual_funds/": "catalog.html",
    "/funds/Finansovaya-podushka/": "fund_finansovaya_podushka.html",
    "/funds/dividendnye-aktsii/": "fund_dividendnye_aktsii.html",
    "/funds/zolotoy-rynok/": "fund_zolotoy_rynok.html",
    "/upload/costs/costs_412_full.csv": "costs_412_full.csv",
    "/upload/costs/costs_997_full.csv": "costs_997_full.csv",
    "/upload/costs/costs_994_full.csv": "costs_994_full.csv",
    "/ajax/datav.php": "datav_27.json",
}


def _ctx(tmp_path: Path, monkeypatch) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        name = ROUTES.get(request.url.path)
        if name is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        ctype = "text/html; charset=UTF-8" if name.endswith(".html") else "text/plain"
        return httpx.Response(200, content=(FIX / name).read_bytes(), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.psb")), seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    funds = [
        _fund("0258-74112789", "Финансовая подушка"),
        _fund("5210", "Дивидендные акции"),
        _fund("6919", "ПСБ – Золотой рынок", ticker="PSGM", ftype="exchange"),
        _fund("9999", "Несуществующий фонд"),
    ]
    res = {fd.rules: fd for fd in psb.PsbAdapter().collect(ctx, funds)}
    assert list(res) == [f.rules for f in funds]

    fp = res["0258-74112789"]
    assert fp.status == "ok" and fp.currency == "RUB" and not fp.warnings and not fp.payouts
    assert fp.page_url == "https://www.upravlyaem.ru/funds/Finansovaya-podushka/"
    [seg] = fp.segments
    assert seg.raw_url == "https://www.upravlyaem.ru/upload/costs/costs_412_full.csv"
    assert seg.page_url == fp.page_url
    assert seg.points[0].date == date(2025, 9, 1)          # август 2025 (до since) отрезан
    assert {r.period: r.value_pct for r in fp.official_returns} == {"1m": 0.98, "3m": 3.14, "6m": 6.44, "1y": 15.45}

    div = res["5210"]
    assert div.status == "ok"
    assert_series(div.segments[0].points, "psb_collect_997_dividendnye_aktsii")
    assert [(p.date, p.amount) for p in div.payouts] == [
        (date(2025, 9, 30), 87.8902), (date(2025, 12, 30), 13.0709), (date(2026, 3, 31), 21.01779),
        (date(2026, 6, 30), 10.30837), (date(2026, 9, 30), 71.53905)]
    assert "https://www.upravlyaem.ru/ajax/datav.php?fund=27&type=price" in seen   # без date_to

    gold = res["6919"]                                      # БПИФ: на сайте к названию добавлен тикер
    assert gold.status == "ok" and gold.page_url.endswith("/funds/zolotoy-rynok/")
    assert_series(gold.segments[0].points, "psb_collect_994_zolotoy_rynok")
    assert _point(gold.segments[0].points, date(2026, 10, 30)).price == 11.7983

    assert res["9999"].status == "not_found" and "Несуществующий фонд" in res["9999"].reason


def test_collect_catalog_down_marks_every_fund(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    monkeypatch.delitem(ROUTES, "/funds/mutual_funds/")
    res = psb.PsbAdapter().collect(ctx, [_fund("0258-74112789", "Финансовая подушка"), _fund("5210", "Дивидендные акции")])
    assert [fd.status for fd in res] == ["unavailable", "unavailable"]
