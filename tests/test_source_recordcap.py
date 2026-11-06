"""УК «Рекорд Капитал»: разбор настоящих страниц recordcap.ru (урезанные копии от 2026-11-03).

Значения сверены с разделом «Раскрытие информации» → «Стоимость пая и СЧА» на сайте:
«Алёнка – Капитал» (№ 3343) на 02.11.2026 — пай 22 752,62 руб., СЧА 1 089 914 315,66 руб.;
                            на 01.09.2025 — пай 26 214,66 руб., СЧА 1 316 859 923,89 руб.;
«О3 – Три Кита» (№ 7663)    на 30.10.2026 — пай 10 100,99 руб., СЧА 27 192 260,69 руб.,
                            первая строка 19.05.2026 — пай 9 997,98 руб., СЧА 24 216 286,89 руб.
Доходность из подвала сайта («Данные на 30.10.2026»): № 3343 — 1 мес +14.32 %, 3 мес +8.52 %,
6 мес −7.05 %, 1 год +0.25 %; № 7663 — 1 мес +1.90 %, 3 мес +2.85 %.
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
from pif.sources.base import Context
from pif.sources.recordcap import (LIST_URL, RecordcapAdapter, parse_fund_list, parse_returns, parse_rules,
                                   parse_values_columns, parse_values_page, parse_values_rows)
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "recordcap"
ALENKA = "https://recordcap.ru/documents/funds/alenka"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- список фондов ----------

def test_fund_list_cards():
    funds = parse_fund_list(_read("list.html"))
    assert len(funds) == 32
    by_url = {f.url: f for f in funds}
    assert by_url[ALENKA].name == "ОПИФ рыночных финансовых инструментов «Алёнка – Капитал»"
    assert not by_url[ALENKA].archived
    assert by_url["https://recordcap.ru/documents/funds/03"].name == \
        "ИПИФ рыночных финансовых инструментов «О3 – Три Кита»"
    assert by_url["https://recordcap.ru/documents/funds/algo"].archived


def test_fund_list_without_cards_is_unknown_format():
    with pytest.raises(UnknownFormat):
        parse_fund_list(_read("list.html").replace("fund-card", "other-card"))


# ---------- страница раскрытия фонда ----------

def test_rules_from_disclosure_page():
    assert parse_rules(_read("doc_alenka.html")) == "3343"
    assert parse_rules(_read("doc_levchenko.html")) == "7565"
    assert parse_rules(_read("doc_03.html")) == "7663"


def test_rules_row_missing_is_unknown_format():
    html = _read("doc_alenka.html").replace("Правила доверительного управления паевым", "Что-то другое")
    with pytest.raises(UnknownFormat):
        parse_rules(html)


def test_values_open_fund_with_benchmark_columns():
    cols, points, more = parse_values_page(_read("doc_alenka.html"))
    assert_series(points, "recordcap_doc_alenka")
    assert cols == {"date": 0, "price": 1, "nav": 2}
    assert more is True                         # есть кнопка «Показать еще»
    assert [p.date for p in points] == sorted(p.date for p in points)
    last = points[-1]
    assert last.date == date(2026, 11, 2)
    assert last.price == 22752.62
    assert last.nav == 1089914315.66


def test_values_interval_fund_whole_table_on_page():
    cols, points, more = parse_values_page(_read("doc_03.html"))
    assert_series(points, "recordcap_doc_03")
    assert more is False and len(points) == 13
    assert (points[0].date, points[0].price, points[0].nav) == (date(2026, 5, 19), 9997.98, 24216286.89)
    assert (points[-1].date, points[-1].price, points[-1].nav) == (date(2026, 10, 30), 10100.99, 27192260.69)


def test_values_fragment_from_load_more():
    points = parse_values_rows(_read("values_alenka.html"), {"date": 0, "price": 1, "nav": 2})
    assert_series(points, "recordcap_values_alenka")
    assert len(points) == 8
    sep1 = next(p for p in points if p.date == date(2025, 9, 1))
    assert (sep1.price, sep1.nav) == (26214.66, 1316859923.89)


def test_empty_load_more_response_is_end_of_table():
    assert parse_values_rows("\n\n", {"date": 0, "price": 1, "nav": 2}) == []


@pytest.mark.parametrize("old,new", [
    ('<th class="text-right">СЧА</th>', '<th class="text-right">Активы</th>'),      # колонка пропала
    ('id="valuesBody"', 'id="otherBody"'),                                            # нет тела таблицы
    ("22\xa0752,62", "22\xa0752,62 руб/пай x"),                                             # мусор в числе
])
def test_broken_values_table_is_unknown_format(old, new):
    html = _read("doc_alenka.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_values_page(html.replace(old, new))


def test_fragment_without_date_cell_is_unknown_format():
    html = _read("values_alenka.html").replace("date-cell", "first-cell")
    with pytest.raises(UnknownFormat):
        parse_values_rows(html, {"date": 0, "price": 1, "nav": 2})


def test_fragment_with_garbage_instead_of_rows_is_unknown_format():
    with pytest.raises(UnknownFormat):
        parse_values_rows("<div>Сервис временно недоступен</div>", {"date": 0, "price": 1, "nav": 2})


def test_columns_found_by_header_not_position():
    html = _read("doc_03.html")
    assert parse_values_columns(html) == {"date": 0, "price": 1, "nav": 2}


# ---------- официальная доходность (подвал сайта) ----------

def test_returns_from_footer():
    rets = parse_returns(_read("doc_alenka.html"), ALENKA)
    a = {r.period: r.value_pct for r in rets["3343"]}
    assert a == {"1m": 14.32, "3m": 8.52, "6m": -7.05, "1y": 0.25}     # «за 3 года» не берём
    assert {r.as_of for r in rets["3343"]} == {date(2026, 10, 30)}
    assert all(r.source_url == ALENKA for r in rets["3343"])
    assert {r.period: r.value_pct for r in rets["7663"]} == {"1m": 1.90, "3m": 2.85}
    assert {r.period: r.value_pct for r in rets["7565"]} == {"1m": 9.75, "3m": 5.48, "6m": -9.77}
    assert "0603-97103681" in rets        # длинный номер правил старого ЗПИФ не обрезается


def test_returns_without_date_is_unknown_format():
    html = _read("doc_alenka.html").replace("Данные на 30.10.2026.", "")
    with pytest.raises(UnknownFormat):
        parse_returns(html, ALENKA)


def test_returns_value_without_number_is_unknown_format():
    html = _read("doc_alenka.html").replace("Доходность за 6 месяцев -7.05%", "Доходность за 6 месяцев н/д")
    with pytest.raises(UnknownFormat):
        parse_returns(html, ALENKA)


def test_no_returns_block_is_empty():
    html = _read("doc_alenka.html").replace("Доходность за", "Прирост за")
    assert parse_returns(html, ALENKA) == {}


# ---------- collect целиком: настоящий Fetcher + MockTransport ----------

PAGES = {
    LIST_URL: "list.html",
    ALENKA: "doc_alenka.html",
    "https://recordcap.ru/documents/funds/levchenko": "doc_levchenko.html",
    "https://recordcap.ru/documents/funds/03": "doc_03.html",
    # в урезанной странице «Алёнки» 15 строк, поэтому догрузка начинается с offset=15
    f"{ALENKA}/values?offset=15&limit=200": "values_alenka.html",
}


def _fund(rules: str, name: str, type_: str = "open", status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ПИФ «{name}»", type=type_, category="",
                status=status, reg_date="2017-07-06", formation_end=None,
                uk_name="ООО «УК «РЕКОРД КАПИТАЛ»", uk_short="ООО \"УК \"РЕКОРД КАПИТАЛ\"",
                uk_ogrn="1137746080768", uk_inn="7718919844", uk_site="https://www.recordcap.ru",
                exchange_tradable=False)


def _ctx(tmp_path: Path, monkeypatch, last_dates=None) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        name = PAGES.get(str(request.url))
        if name is None:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        return httpx.Response(200, content=(FIX / name).read_bytes(),
                              headers={"content-type": "text/html; charset=utf-8"})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr(http, "_wait_turn", lambda domain: None)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.recordcap"),
                  last_dates=last_dates or {})
    return ctx, seen


def test_collect_statuses_and_matching(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    funds = [_fund("3343", "Алёнка – Капитал"),
             _fund("7663", "ОЗ – Три Кита", type_="interval"),      # в реестре «ОЗ» с буквой З
             _fund("9999", "Несуществующий")]
    by = {fd.rules: fd for fd in RecordcapAdapter().collect(ctx, funds)}

    fd = by["3343"]
    assert fd.status == "ok", fd.reason
    assert fd.currency == "RUB" and fd.page_url == ALENKA
    pts = sorted((p for s in fd.segments for p in s.points), key=lambda p: p.date)
    assert pts[0].date == date(2025, 9, 1)             # строки до since отрезаны
    assert pts[-1].date == date(2026, 11, 2) and pts[-1].nav == 1089914315.66
    assert [s.raw_url for s in fd.segments] == [ALENKA, f"{ALENKA}/values?offset=15&limit=200"]
    assert {r.period for r in fd.official_returns} == {"1m", "3m", "6m", "1y"}

    fd = by["7663"]
    assert fd.status == "ok", fd.reason
    assert fd.page_url == "https://recordcap.ru/documents/funds/03"
    assert len(fd.segments[0].points) == 13
    assert {r.period: r.value_pct for r in fd.official_returns} == {"1m": 1.90, "3m": 2.85}

    fd = by["9999"]
    assert fd.status == "not_found"
    assert "№ 3343" in fd.reason and "№ 7565" in fd.reason   # перебрали действующие ОПИФ


def test_collect_incremental_does_not_page(tmp_path, monkeypatch):
    # хвост собран до 30.10.2026 -> нужен ряд с 20.10.2026; на странице строки с 13.10.2026 -> догрузка не нужна
    ctx, seen = _ctx(tmp_path, monkeypatch, last_dates={"3343": date(2026, 10, 30)})
    [fd] = RecordcapAdapter().collect(ctx, [_fund("3343", "Алёнка – Капитал")])
    assert fd.status == "ok"
    assert not any("/values" in u for u in seen)
    assert len(fd.segments) == 1 and len(fd.segments[0].points) == 15


def test_list_403_gives_blocked(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    forbidden = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, content=b"forbidden", headers={"content-type": "text/html"})))
    monkeypatch.setattr(ctx.http, "_client", lambda domain: forbidden)
    [fd] = RecordcapAdapter().collect(ctx, [_fund("3343", "Алёнка – Капитал")])
    assert fd.status == "blocked"
