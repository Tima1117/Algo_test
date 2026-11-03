"""УК «Первая» (first-am.ru): разбор страниц фондов и раздела раскрытия.

Фикстуры — настоящие ответы сайта 2026-11-03, урезанные по длине: из HTML оставлены в исходной
вложенности <title>, h1, h3 с полным названием, блок графика (chart-fund-data — первая точка и
последние недели), таблица «Пай/СЧА», #pif-table и юридическая справка (из неё — первые пять
предложений и предложение про сам фонд; в disclosure_fund.html — целиком). Значения в тестах
сверены с таблицей «Пай / СЧА» и блоком «Прирост расчетной стоимости» на страницах сайта, а точка
30.09.2026 Фонда российских акций — ещё и со справкой о СЧА за сентябрь 2026 (PDF на сайте УК).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import first_am as fa
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "first_am"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _point(points, d: date):
    [p] = [p for p in points if p.date == d]
    return p


def _fund(rules: str, name: str, *, ticker: str | None = None, status: str = "Сформирован",
          formation_end: str | None = "2000-01-01", ftype: str = "open") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ПИФ «{name}»", type=ftype, category="",
                status=status, reg_date="2000-01-01", formation_end=formation_end,
                uk_name="Акционерное общество «Управляющая компания «Первая»", uk_short="АО УК «Первая»",
                uk_ogrn="1027739007570", uk_inn="7710183778", uk_site="https://www.first-am.ru",
                exchange_tradable=ftype == "exchange", isin=None, ticker=ticker)


# ---------- ряд ----------
def test_open_fund_series_values_match_site():
    chart = fa.parse_chart(_read("fund_ros_akc.html"))
    assert chart.currency == "RUB"
    assert chart.title == "Фонд российских акций"
    pts = chart.points
    assert pts[0].date == date(1997, 6, 5) and pts[0].price == 500 and pts[0].nav == 303599
    # таблица «Пай / СЧА» на странице: 30.10.2026 — 17 208,28 ₽ / 13 017 466 804,00 ₽
    p = _point(pts, date(2026, 10, 30))
    assert p.price == 17208.28 and p.nav == 13017466804.0
    p = _point(pts, date(2026, 11, 2))
    assert p.price == 16798.3 and p.nav == 12701116188.99
    # справка о СЧА за сентябрь 2026 (PDF на сайте УК): 11 924 828 726,36 ₽, пай 15 577,43 ₽
    p = _point(pts, date(2026, 9, 30))
    assert p.nav == 11924828726.36 and p.price == 15577.43
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    assert chart.payouts == {}


def test_income_fund_payouts_are_kept_apart_from_price():
    chart = fa.parse_chart(_read("fund_valyutnykh_obligatsiy_s_vyplatoy.html"))
    assert chart.payouts == {date(2026, 9, 30): 889.88}
    # в дату выплаты пай уже без дохода: 29.09 — 51 704,75 ₽, 30.09 — 50 353,21 ₽
    assert _point(chart.points, date(2026, 9, 29)).price == 51704.75
    p = _point(chart.points, date(2026, 9, 30))
    assert p.price == 50353.21 and p.nav == 651843265.99
    with pytest.raises(UnknownFormat, match="выплата"):
        fa.parse_chart(_read("fund_valyutnykh_obligatsiy_s_vyplatoy.html").replace(
            '"pay_profit":889.88', '"pay_profit":-889.88'))


def test_split_etf_uses_actual_unit_price_not_chart_scaled():
    # SBMX: паи раздроблены 04.06.2021 в 100 раз; на графике price = 1765, на сайте пай — 17,65 ₽
    pts = fa.parse_series(_read("etf_sbmx.html"))
    p = _point(pts, date(2026, 11, 2))
    assert p.price == 17.65 and p.nav == 20195799632.62
    assert _point(pts, date(2026, 10, 30)).price == 18.06
    assert pts[0].date == date(2018, 8, 30) and pts[0].price == 999.96   # до дробления


def test_usd_interval_fund_simplified_page():
    chart = fa.parse_chart(_read("ipif_valyutnye_obligatsii.html"))
    assert chart.currency == "USD"
    p = _point(chart.points, date(2026, 11, 2))             # 36,13 $ / 15 144 280,35 $
    assert p.price == 36.13 and p.nav == 15144280.35
    assert _point(chart.points, date(2026, 10, 30)).nav == 15037972.59


def test_forming_fund_page_has_no_chart():
    content = _read("opif_new_impressions.html")
    assert not fa.has_chart(content)
    with pytest.raises(UnknownFormat):
        fa.parse_chart(content)


def test_broken_chart_raises():
    good = _read("fund_ros_akc.html")
    with pytest.raises(UnknownFormat, match="originalPrice"):
        fa.parse_chart(good.replace('"originalPrice":', '"origPrice":'))
    with pytest.raises(UnknownFormat, match="валюта"):
        fa.parse_chart(good.replace('"currencyCode":"RUB"', '"currencyCode":"XXX"'))
    # price без дробления обязан совпадать с originalPrice
    with pytest.raises(UnknownFormat, match="без дробления"):
        fa.parse_chart(good.replace('"price":16798.3,', '"price":16798.4,'))
    # после дробления price обязан быть originalPrice × crushingRatio
    split = _read("etf_sbmx.html")
    assert '"price":1765,"originalPrice":17.65' in split
    with pytest.raises(UnknownFormat, match="crushingRatio"):
        fa.parse_chart(split.replace('"price":1765,"originalPrice":17.65', '"price":17.65,"originalPrice":17.65'))


def _uk_return(chart, base: date, end: date) -> float:
    """Как считает УК «Первая»: (пай в конце + сумма выплат на пай за (base, end]) / пай в начале − 1."""
    px = {p.date: p.price for p in chart.points}
    paid = sum(v for d, v in chart.payouts.items() if base < d <= end)
    return round(((px[end] + paid) / px[base] - 1) * 100, 2)


def test_monthly_payouts_reproduce_official_return():
    # FLOW платит доход каждый месяц; дата выплаты — день, когда пай уже без дохода
    chart = fa.parse_chart(_read("etf_flow.html"))
    assert chart.payouts == {date(2026, 7, 31): 11.12, date(2026, 8, 31): 10.02,
                             date(2026, 9, 30): 10.94, date(2026, 10, 30): 13.31}
    px = {p.date: p.price for p in chart.points}
    assert px[date(2026, 7, 30)] == 1023.37 and px[date(2026, 7, 31)] == 1012.6     # падение на 11,12 в день выплаты
    official = fa.parse_returns_page(_read("etf_flow.html"))
    assert official.as_of == date(2026, 10, 30)
    # база — последний рабочий день месяца N месяцев назад; выплаты просто прибавляются к паю
    assert _uk_return(chart, date(2026, 9, 30), date(2026, 10, 30)) == official.values["1m"] == 1.07
    assert _uk_return(chart, date(2026, 7, 31), date(2026, 10, 30)) == official.values["3m"] == 3.19
    # реинвестирование по паю на дату выплаты даёт другое число (3,22%) — УК так не считает
    reinvest = px[date(2026, 10, 30)] / px[date(2026, 7, 31)]
    for d, v in chart.payouts.items():
        if date(2026, 7, 31) < d:
            reinvest *= 1 + v / px[d]
    assert round((reinvest - 1) * 100, 2) == 3.22


# ---------- сопоставление ----------
def test_rules_names_from_legal_notice():
    legal = fa.parse_rules_names(_read("disclosure_fund.html"))
    assert legal["0011-46360962"] == "Первая – Фонд российских акций"
    assert legal["4471-СД"] == "Валютные облигации"
    assert legal["6312"] == "Первая – Фонд АйПиО"
    assert legal["5763"] == "Заблокированные активы паевого инвестиционного фонда «Первая – Золото»"
    assert legal["8124"] == "Первая - Фонд Разумный капитал"
    with pytest.raises(UnknownFormat):
        fa.parse_rules_names(re.sub(r"правила доверительного управления", "правила", _read("disclosure_fund.html")))


def test_page_matched_by_rules_ticker_or_rejected():
    ros = _read("fund_ros_akc.html")
    names, legal = fa.parse_page_names(ros), fa.parse_rules_names(ros)
    assert fa.match_page(_fund("0011-46360962", "Первая – Фонд российских акций"), names, legal) == "правила"
    # чужой фонд на этой странице не опознаётся
    assert fa.match_page(_fund("4980", "Первая - Фонд акций с выплатой дохода"), names, legal) is None

    # SIPO: на сайте «Фонд IPO», в правилах — «Фонд АйПиО»; совпадает биржевой тикер
    sipo = _read("etf_sipo.html")
    fund = _fund("6312", "Первая – Фонд АйПиО", ticker="SIPO", ftype="exchange")
    assert fa.match_page(fund, fa.parse_page_names(sipo), fa.parse_rules_names(sipo)) == "тикер"
    other = _fund("6312", "Первая – Фонд АйПиО", ticker="SBMX", ftype="exchange")
    assert fa.match_page(other, fa.parse_page_names(sipo), {}) is None

    usd = _read("ipif_valyutnye_obligatsii.html")       # упрощённый шаблон: название только в <title> и графике
    assert fa.match_page(_fund("4471-СД", "Валютные облигации"), fa.parse_page_names(usd),
                         fa.parse_rules_names(usd)) == "правила"


# ---------- доходность ----------
def test_returns_from_disclosure_table():
    rows = fa.parse_returns_disclosure(_read("disclosure_fund.html"))
    r = rows["/individuals/fund/opif-aktsiy-fond-ros-aktsiy"]
    assert r.as_of == date(2026, 10, 30)
    assert r.values == {"1m": 10.47, "3m": 12.86, "6m": -0.57, "1y": 2.65}
    # интервальный фонд — в отдельной таблице
    assert rows["/individuals/fund/ipif-fi-mezhdunarodnye-obligatsii"].values == \
        {"1m": 1.96, "3m": 1.5, "6m": 0.81, "1y": 4.61}
    assert rows["/individuals/etf/etf-sbcn"].values["1m"] == 0.0          # «0,00%» — настоящий ноль
    assert rows["/individuals/etf/etf-aiiq"].values == {"1m": 8.01}        # «-» за 3/6/12 мес — None
    secr = rows["/individuals/etf/etf-secr"]
    assert secr.as_of is None and secr.values == {}


def test_returns_from_fund_page():
    r = fa.parse_returns_page(_read("fund_ros_akc.html"))
    assert r.as_of == date(2026, 10, 30)
    assert r.values == {"1m": 10.47, "3m": 12.86, "6m": -0.57, "1y": 2.65}
    # «Инструменты в рублях» нет в таблице раскрытия — только на странице фонда
    r = fa.parse_returns_page(_read("fund_instrumenty_v_rublyakh.html"))
    assert r.values == {"1m": 1.39, "3m": 1.75, "6m": 2.42}
    assert fa.parse_returns_page(_read("ipif_valyutnye_obligatsii.html")) is None


def test_broken_returns_tables_raise():
    disc = _read("disclosure_fund.html")
    with pytest.raises(UnknownFormat, match="колонки"):
        fa.parse_returns_disclosure(disc.replace("<span>3 мес.</span>", "<span>квартал</span>"))
    with pytest.raises(UnknownFormat, match="Прирост"):
        fa.parse_returns_disclosure(disc.replace('id="pif-table"', 'id="other"'))
    page = _read("fund_ros_akc.html")
    with pytest.raises(UnknownFormat, match="колонки"):
        fa.parse_returns_page(page.replace("6 мес</th>", "полгода</th>"))
    with pytest.raises(UnknownFormat, match="не дата"):
        fa.parse_returns_page(page.replace(">30.10.2026</td>", ">вчера</td>", 1))


# ---------- адаптер целиком, без сети ----------
PAGE_FIXTURES = {
    "/individuals/fund/opif-aktsiy-fond-ros-aktsiy": "fund_ros_akc.html",
    "/individuals/etf/etf-moex": "etf_sbmx.html",
    "/individuals/fund/ipif-fi-mezhdunarodnye-obligatsii": "ipif_valyutnye_obligatsii.html",
    "/individuals/fund/opif-new-impressions": "opif_new_impressions.html",
    "/individuals/etf/etf-sipo": "etf_sipo.html",
    "/individuals/fund/fond-rublevye-sberezheniia-2": "fund_instrumenty_v_rublyakh.html",
    "/individuals/fund/fond-valiutnykh-obligatsii-s-vyplatoi-dokhoda": "fund_valyutnykh_obligatsiy_s_vyplatoy.html",
    "/individuals/etf/fond-ezhemesiachnyi-dokhod": "etf_flow.html",
    "/disclosure/fund": "disclosure_fund.html",
}


def _ctx(tmp_path: Path, monkeypatch) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        name = PAGE_FIXTURES.get(request.url.path.rstrip("/"))
        if name is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, content=(FIX / name).read_bytes(),
                              headers={"content-type": "text/html; charset=UTF-8"})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.first_am")), seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    # «чужая» страница по подсказке: фонд 4980 якобы живёт на странице Фонда российских акций
    monkeypatch.setitem(fa.PAGES, "4980", "/individuals/fund/opif-aktsiy-fond-ros-aktsiy")
    funds = [
        _fund("0011-46360962", "Первая – Фонд российских акций"),
        _fund("3555", "Первая – Фонд Топ Российских акций", ticker="SBMX", ftype="exchange"),
        _fund("4471-СД", "Валютные облигации"),
        _fund("7905", "Первая - Фонд Новые впечатления", status="Формируется", formation_end=None),
        _fund("6312", "Первая – Фонд АйПиО", ticker="SIPO", ftype="exchange"),
        _fund("7594", "Первая – Инструменты в рублях", formation_end="2026-04-20"),
        _fund("4980", "Первая - Фонд акций с выплатой дохода"),
        _fund("9999", "Первая – Фонд, которого нет"),
        _fund("7160", "Первая - Фонд валютных облигаций с выплатой дохода", formation_end="2025-07-28"),
    ]
    res = {fd.rules: fd for fd in fa.FirstAmAdapter().collect(ctx, funds)}
    assert list(res) == [f.rules for f in funds]

    ros = res["0011-46360962"]
    assert ros.status == "ok" and ros.currency == "RUB" and not ros.errors
    assert ros.page_url == "https://www.first-am.ru/individuals/fund/opif-aktsiy-fond-ros-aktsiy"
    [seg] = ros.segments
    assert seg.points[0].date == date(2026, 9, 30)            # точка 1997 года (до since) отрезана
    assert {r.period: r.value_pct for r in ros.official_returns} == \
        {"1m": 10.47, "3m": 12.86, "6m": -0.57, "1y": 2.65}
    assert all(r.source_url == fa.DISCLOSURE_URL and r.as_of == date(2026, 10, 30) for r in ros.official_returns)

    assert res["3555"].segments[0].points[-1].price == 17.65
    assert res["4471-СД"].currency == "USD" and len(res["4471-СД"].official_returns) == 4
    assert res["7905"].status == "not_formed" and not res["7905"].segments
    assert res["6312"].status == "ok"
    inst = res["7594"]
    assert inst.status == "ok" and {r.period for r in inst.official_returns} == {"1m", "3m", "6m"}
    assert inst.official_returns[0].source_url == inst.page_url
    assert res["4980"].status == "not_found" and "другой фонд" in res["4980"].reason
    assert res["9999"].status == "not_found"
    inc = res["7160"]
    assert inc.status == "ok" and {r.period: r.value_pct for r in inc.official_returns}["1y"] == 4.18
    assert [(p.date, p.amount, p.source_url) for p in inc.payouts] == \
        [(date(2026, 9, 30), 889.88, inc.page_url)]
    assert ros.payouts == []
    # раздел раскрытия запрошен один раз на всю УК
    assert seen.count("/disclosure/fund/") == 1


def test_collect_marks_parse_error_on_broken_page(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    broken = tmp_path / "broken.html"
    broken.write_text(_read("fund_ros_akc.html").replace('"net_assets":', '"nav":'), "utf-8")
    monkeypatch.setitem(PAGE_FIXTURES, "/individuals/fund/opif-aktsiy-fond-ros-aktsiy", str(broken))
    [fd] = fa.FirstAmAdapter().collect(ctx, [_fund("0011-46360962", "Первая – Фонд российских акций")])
    assert fd.status == "parse_error" and "net_assets" in fd.reason


def test_collect_without_disclosure_falls_back_to_fund_page(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    monkeypatch.delitem(PAGE_FIXTURES, "/disclosure/fund")          # раздел раскрытия отвечает 404
    funds = [_fund("0011-46360962", "Первая – Фонд российских акций"), _fund("4471-СД", "Валютные облигации")]
    ros, usd = fa.FirstAmAdapter().collect(ctx, funds)

    assert ros.status == "ok" and not ros.errors
    assert {r.period: r.value_pct for r in ros.official_returns}["1y"] == 2.65
    assert all(r.source_url == ros.page_url for r in ros.official_returns)
    assert any("раздел раскрытия недоступен" in w for w in ros.warnings)
    # у страницы упрощённого шаблона своей таблицы нет — честно пусто и подсказка про КИД
    assert usd.status == "ok" and usd.official_returns == [] and usd.segments
    assert any("ключевом информационном документе" in w and usd.page_url + "/documents" in w
               for w in usd.warnings)


def test_collect_payouts_only_since(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    ctx.since = date(2026, 9, 15)
    [fd] = fa.FirstAmAdapter().collect(
        ctx, [_fund("7137", "Первая – Фонд Ежемесячный доход", ticker="FLOW", ftype="exchange",
                    formation_end="2025-07-07")])
    assert fd.status == "ok"
    assert [(p.date, p.amount) for p in fd.payouts] == [(date(2026, 9, 30), 10.94), (date(2026, 10, 30), 13.31)]
    assert all(p.source_url == "https://www.first-am.ru/individuals/etf/fond-ezhemesiachnyi-dokhod" for p in fd.payouts)
    assert not any("выплат" in w for w in fd.warnings)
