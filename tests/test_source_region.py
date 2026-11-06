"""РЕГИОН Эссет Менеджмент (region-am.ru): страница раскрытия фонда и таблицы СЧА по годам.

Фикстуры — настоящие ответы 2026-11-03, урезанные по длине:
* catalog.html — со страницы /disclosure/paevye-investitsionnye-fondy/ только ссылки на фонды;
* fund_*.html — со страницы раскрытия только блок вкладок; во вкладке «Стоимость пая и СЧА»
  из таблицы оставлено несколько строк, прочие вкладки очищены (у «Купонного дохода» во
  «Официальных сообщениях» оставлены сообщения о выплате дохода), убран отладочный комментарий;
* table_2025_*.json — ответы /ajax/table.php за 2025 год, в html оставлено несколько строк
  (у «Фонд Золото» — с колонками отклонения от индекса и строкой подзаголовка).
Значения сверены с таблицей «Стоимость пая и СЧА» на странице «РЕГИОН Акции»: 02.11.2026 —
РСИП 19 616,73 ₽, СЧА 247 323 994,45 ₽; «Изменение стоимости пая» на 30.10.2026 — 6.44 / 3.85 /
−4.23 / 7.07 % за месяц / 3 / 6 мес. / 1 год.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import region as rg
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "region"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _point(points, d: date):
    [p] = [p for p in points if p.date == d]
    return p


def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ «{name}»", type="open", category="",
                status="Сформирован", reg_date="2003-06-18", formation_end="2003-09-01",
                uk_name="АО «РЕГИОН ЭсМ»", uk_short="РЕГИОН ЭсМ", uk_ogrn="1027739046895",
                uk_inn="7730149408", uk_site="www.region-am.ru", exchange_tradable=False)


# ---------- страница раскрытия ----------
def test_catalog():
    cat = rg.parse_catalog_region(_read("catalog.html"))
    assert len(cat) == 9
    assert cat["https://region-am.ru/disclosure/paevye-investitsionnye-fondy/opif-skaner/"] == \
        "ОПИФ рыночных финансовых инструментов «Сканер»"


def test_fund_page_rules_and_ajax_params():
    page = rg.parse_fund_page_region(_read("fund_aktsii.html"))
    assert page.rules == "0120-14241368"            # «… за № 0120–14241368» (длинное тире)
    assert (page.page_id, page.tab_id, page.scha_tpl) == ("73007", "Button__opif-stock__os-tab-1", "filter_by_year")
    assert page.years[:3] == [2026, 2025, 2024] and page.years[-1] == 2003
    gold = rg.parse_fund_page_region(_read("fund_zoloto.html"))
    assert gold.rules == "6853" and gold.scha_tpl == "filter_by_year_index" and gold.years == [2026, 2025]


def test_returns_values_match_site():
    rets = rg.parse_returns_region(_read("fund_aktsii.html"), "https://x/")
    assert [(r.period, r.value_pct, r.as_of) for r in rets] == [
        ("1m", 6.44, date(2026, 10, 30)), ("3m", 3.85, date(2026, 10, 30)),
        ("6m", -4.23, date(2026, 10, 30)), ("1y", 7.07, date(2026, 10, 30))]
    # «---» — у фонда ещё нет периода: значение пропускаем, а не превращаем в ноль
    html = _read("fund_aktsii.html").replace("7.07%", "---")
    assert [r.period for r in rg.parse_returns_region(html, "u")] == ["1m", "3m", "6m"]


def test_current_year_table_values_match_site():
    html = _read("fund_aktsii.html")
    assert rg.nav_currency_region(html) == "RUB"
    pts = rg.parse_nav_region(html)
    assert_series(pts, "region_aktsii_page")
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    p = _point(pts, date(2026, 11, 2))
    assert p.price == 19616.73 and p.nav == 247323994.45
    p = _point(pts, date(2026, 10, 30))
    assert p.price == 19875.27 and p.nav == 250583608.50


def test_ajax_year_tables_including_index_fund():
    pts = rg.parse_nav_ajax_region((FIX / "table_2025_aktsii.json").read_bytes())
    assert_series(pts, "region_aktsii_2025")
    p = _point(pts, date(2025, 12, 30))
    assert p.price == 20342.50 and p.nav == 360064458.84
    assert _point(pts, date(2025, 9, 1)).price == 20706.97
    gold = rg.parse_nav_ajax_region((FIX / "table_2025_zoloto.json").read_bytes())
    assert_series(gold, "region_zoloto_2025")
    assert [p.date for p in gold] == [date(2025, 8, 29), date(2025, 9, 1), date(2025, 12, 30)]
    assert _point(gold, date(2025, 12, 30)).nav == 15074293.88


def test_tables_broken_are_unknown_format():
    html = _read("fund_aktsii.html")
    with pytest.raises(UnknownFormat, match="не найдены колонки"):
        rg.parse_nav_region(html.replace("Стоимость чистых активов (СЧА), руб.</td>", "Активы</td>"))
    with pytest.raises(UnknownFormat, match="не в рублях"):
        rg.parse_nav_region(html.replace("Стоимость чистых активов (СЧА), руб.</td>",
                                         "Стоимость чистых активов (СЧА), тыс. руб.</td>"))
    with pytest.raises(UnknownFormat, match="не число"):
        rg.parse_nav_region(html.replace("247 323 994,45", "247 323 994,45 млн"))
    with pytest.raises(UnknownFormat, match="html"):
        rg.parse_nav_ajax_region(json.dumps({"table": "<table></table>"}))
    with pytest.raises(UnknownFormat, match="зарегистрированы"):
        rg.parse_fund_page_region(html.replace("зарегистрированы", "утверждены"))
    with pytest.raises(UnknownFormat, match="нет колонок"):
        rg.parse_returns_region(html.replace("6\xa0месяцев", "полгода"), "u")


def test_payouts_messages():
    payouts, pdf_only = rg.parse_payouts_region(_read("fund_kuponnyy_dokhod.html"), "https://x/")
    # «Размер дохода … на один инвестиционный пай — 32 руб. 26 коп.», «Дата, на которую … — 30.09.2026»
    assert [(p.date, p.amount) for p in payouts] == [(date(2026, 9, 30), 32.26)]
    assert len(pdf_only) == 4 and pdf_only[-1] == (
        date(2025, 10, 1), "https://region-am.ru/upload/files/5306/5306_soob_dohod_2025_3q.pdf")
    # в дату выплаты пай уже без дохода: 29.09 — 1 072,62 ₽, 30.09 — 1 041,46 ₽
    pts = rg.parse_nav_region(_read("fund_kuponnyy_dokhod.html"))
    assert_series(pts, "region_kuponnyy_dokhod_page")
    assert _point(pts, date(2026, 9, 29)).price == 1072.62 and _point(pts, date(2026, 9, 30)).price == 1041.46
    with pytest.raises(UnknownFormat, match="сумм"):
        rg.parse_payouts_region(_read("fund_kuponnyy_dokhod.html").replace("32 руб. 26 коп.", "тридцать два рубля"), "u")


# ---------- сбор целиком (без сети) ----------
PAGES = {
    "/disclosure/paevye-investitsionnye-fondy/": "catalog.html",
    "/disclosure/paevye-investitsionnye-fondy/opif-region-aktsii/": "fund_aktsii.html",
    "/disclosure/paevye-investitsionnye-fondy/opif-region-kuponnyy-dokhod/": "fund_kuponnyy_dokhod.html",
    "/disclosure/paevye-investitsionnye-fondy/opif-region-fond-zoloto/": "fund_zoloto.html",
}
TABLES = {("2025", "73007"): "table_2025_aktsii.json", ("2025", "73675"): "table_2025_zoloto.json"}


def _ctx(tmp_path: Path, monkeypatch, last_dates=None) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/ajax/table.php":
            q = request.url.params
            name = TABLES.get((q.get("year"), q.get("idPage")))
            # как настоящий сервер: без X-Requested-With — пустой 404
            if name is None or request.headers.get("x-requested-with") != "XMLHttpRequest" \
                    or q.get("idTab") != "Button__opif-stock__os-tab-1":
                return httpx.Response(404, content=b"", headers={"content-type": "text/html"})
            return httpx.Response(200, content=(FIX / name).read_bytes(), headers={"content-type": "application/json"})
        name = PAGES.get(request.url.path)
        if name is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, content=(FIX / name).read_bytes(), headers={"content-type": "text/html; charset=UTF-8"})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.region"),
                  last_dates=last_dates or {})
    return ctx, seen


def test_collect_end_to_end(tmp_path, monkeypatch):
    # «Купонный доход» уже собран до 23.10.2026 — докачка внутри текущего года, без /ajax/table.php
    ctx, seen = _ctx(tmp_path, monkeypatch, last_dates={"5306": date(2026, 10, 23)})
    funds = [
        _fund("0120-14241368", "РЕГИОН Акции"),
        _fund("5306", "РЕГИОН Купонный доход"),
        _fund("6853", "РЕГИОН Золото — название в реестре другое"),   # найдётся по номеру правил
        _fund("9999", "РЕГИОН Нет такого"),
    ]
    res = {fd.rules: fd for fd in rg.RegionAdapter().collect(ctx, funds)}
    assert list(res) == [f.rules for f in funds]

    ak = res["0120-14241368"]
    assert ak.status == "ok" and ak.currency == "RUB" and not ak.warnings
    assert ak.page_url == "https://region-am.ru/disclosure/paevye-investitsionnye-fondy/opif-region-aktsii/"
    page_seg, year_seg = ak.segments
    assert page_seg.raw_url == ak.page_url and _point(page_seg.points, date(2026, 11, 2)).price == 19616.73
    assert "year=2025" in year_seg.raw_url and year_seg.page_url == ak.page_url
    assert [p.date for p in year_seg.points][0] == date(2025, 9, 1)        # август 2025 (до since) отрезан
    assert {r.period: r.value_pct for r in ak.official_returns} == {"1m": 6.44, "3m": 3.85, "6m": -4.23, "1y": 7.07}
    assert not any("year=2024" in u for u in seen)                         # раньше since не ходим

    kd = res["5306"]
    assert kd.status == "ok" and len(kd.segments) == 1
    assert not any("idPage=73674" in u for u in seen)
    # официальная доходность без учёта выплат — выплаты не в payouts, а в предупреждении
    assert kd.payouts == []
    [warn] = kd.warnings
    assert "32.26 RUB на пай на 30.09.2026" in warn and "5306_soob_dohod_2025_3q.pdf" in warn

    gold = res["6853"]
    assert gold.status == "ok" and gold.page_url.endswith("/opif-region-fond-zoloto/")
    assert {s.raw_url.split("?")[0] for s in gold.segments} == {gold.page_url, rg.TABLE_URL}

    # остальные 6 страниц из списка в фикстурах нет (404): фонд не найден, но и сбой честно виден
    assert res["9999"].status == "unavailable"
