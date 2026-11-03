"""АО «БКС Управление благосостоянием» (bcswm.ru): разбор настоящих ответов сайта и сбор без сети.

Фикстуры (tests/fixtures/bcswm/) — настоящие ответы сайта 03.11.2026:
* funds.html — /funds/, оставлен блок div.fund-list со ссылками на фонды;
* values_uf1.html, values_drs.html — /funds/<код>/values/, оставлена section.fund-dynamics
  (таблица последних значений, input#fund_id, «Динамика стоимости пая 31.10.2026», сноска с правилами);
* uf1.csv, drs.csv — getCSV.php?fund_id=44 / 58 (cp1251): заголовок, первые строки 1999/2013 года,
  примечание о дроблении паёв 04.06.2021 и все строки с 01.08.2025.
Значения сверены с таблицами на https://www.bcswm.ru/funds/uf1/values/ и /funds/drs/values/.
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
from pif.sources.bcswm import BcsWmAdapter, parse_csv, parse_fund_list, parse_values_page

FIX = Path(__file__).parent / "fixtures" / "bcswm"
UF1_PAGE = "https://www.bcswm.ru/funds/uf1/values/"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- список фондов и страница фонда ----------

def test_fund_list_maps_names_to_values_pages():
    funds = parse_fund_list(_read("funds.html"))
    assert funds["Фонд Первый"] == UF1_PAGE
    assert funds["Глобальный рынок облигаций"] == "https://www.bcswm.ru/funds/drs/values/"
    # ЗПИФ с похожим названием — отдельная запись, точное сопоставление его не зацепит
    assert "ЗПИФ «Глобальный рынок облигаций (заблокированные активы)»" in funds


def test_fund_list_without_links_raises():
    with pytest.raises(UnknownFormat):
        parse_fund_list(_text("funds.html").replace('href="/funds/', 'href="/catalog/'))


def test_values_page_uf1():
    info = parse_values_page(_read("values_uf1.html"), UF1_PAGE)
    assert info["fund_id"] == "44"
    assert info["rules"] == "0029-18610555"
    assert info["currency"] == "RUB"
    rets = {r.period: r.value_pct for r in info["returns"]}
    assert rets == {"1m": 8.45, "3m": 8.56, "6m": -0.23, "1y": 6.89}
    assert {r.as_of for r in info["returns"]} == {date(2026, 10, 31)}
    assert info["returns"][0].source_url == UF1_PAGE


def test_values_page_drs_empty_cell_is_skipped():
    info = parse_values_page(_read("values_drs.html"), "https://www.bcswm.ru/funds/drs/values/")
    assert info["fund_id"] == "58" and info["rules"] == "2608" and info["currency"] == "RUB"
    # «3 года» в таблице пустое («%») — в 1m/3m/6m/1y не входит, остальное на месте
    assert {r.period: r.value_pct for r in info["returns"]} == {"1m": -1.4, "3m": 2.36, "6m": 7.04, "1y": 6.06}


@pytest.mark.parametrize("old,new", [
    ('id="fund_id"', 'id="fund_code"'),                       # нет внутреннего id фонда
    ("Правила зарегистрированы", "Правила утверждены"),         # нет номера правил
    ("Расчетная стоимость пая, руб.", "Расчетная стоимость пая, тугр."),   # незнакомая валюта
    ("<th>1 год*</th>", ""),                                   # таблица доходности поехала
])
def test_values_page_broken_raises(old, new):
    html = _text("values_uf1.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_values_page(html.replace(old, new), UF1_PAGE)


# ---------- CSV ----------

def test_csv_uf1_values_in_rubles():
    pts = {p.date: p for p in parse_csv(_read("uf1.csv"))}
    # строка таблицы на странице фонда: 02.11.2026 — 187.51 руб. и 3 333 025 544.67 руб.
    assert pts[date(2026, 11, 2)].price == 187.51
    assert pts[date(2026, 11, 2)].nav == 3333025544.67
    assert pts[date(2026, 10, 30)].price == 190.8
    assert pts[date(2026, 10, 30)].nav == 3391979697.26
    assert pts[date(2025, 9, 1)].price == 200.04
    # до дробления 1:100 (04.06.2021) цена в CSV — как публиковалась тогда, примечание не мешает
    assert pts[date(2021, 6, 3)].price == 22628.49 and pts[date(2021, 6, 4)].price == 226.29


def test_csv_drs_zero_rows_are_none():
    pts = parse_csv(_read("drs.csv"))
    assert pts[0].date == date(2013, 6, 13) and pts[0].nav is None and pts[0].price is None
    last = pts[-1]
    assert (last.date, last.nav, last.price) == (date(2026, 11, 2), 80790734.21, 217.24)


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("Дата;СЧА, руб.;Стоимость пая, руб.", "Дата;Стоимость пая, руб."),     # нет колонки
    lambda t: t.replace("СЧА, руб.", "СЧА, тыс. руб."),                                       # другие единицы
    lambda t: t.replace("02.11.2026;", "2026/11/02;"),                                         # дата не та
    lambda t: t.replace("30.10.2026;3391979697,26;190,8", "30.10.2026;3391979697,26"),         # колонка пропала
])
def test_csv_broken_raises(mutate):
    text = _read("uf1.csv").decode("cp1251")
    with pytest.raises(UnknownFormat):
        parse_csv(mutate(text).encode("cp1251"))


# ---------- сбор целиком (без сети) ----------

ROUTES = {
    "/funds/": ("funds.html", "text/html; charset=UTF-8"),
    "/funds/uf1/values/": ("values_uf1.html", "text/html; charset=UTF-8"),
    "/ajax/getCSV.php?fund_id=44": ("uf1.csv", "application/vnd.ms-excel"),
}


def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type="open", category="", status="Сформирован",
                reg_date="1998-09-14", formation_end=None, uk_name="", uk_short="БКС УБ", uk_ogrn="",
                uk_inn="7702172846", uk_site="https://www.bcswm.ru", exchange_tradable=False)


def test_collect_end_to_end(tmp_path, monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        seen.append(path)
        if path not in ROUTES:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        name, ctype = ROUTES[path]
        return httpx.Response(200, content=_read(name), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.bcswm"))

    first, wrong_rules, missing = BcsWmAdapter().collect(ctx, [
        _fund("0029-18610555", "Фонд Первый"),
        _fund("1234", "Фонд Первый"),            # название совпало, правила — нет
        _fund("9999", "Нет такого фонда"),
    ])
    assert first.status == "ok" and first.currency == "RUB" and first.page_url == UF1_PAGE
    (seg,) = first.segments
    assert seg.raw_url == "https://www.bcswm.ru/ajax/getCSV.php?fund_id=44"
    assert seg.points[0].date == date(2025, 9, 1)            # всё до since отрезано
    assert seg.points[-1].date == date(2026, 11, 2)
    assert {r.period for r in first.official_returns} == {"1m", "3m", "6m", "1y"}

    assert wrong_rules.status == "not_found" and "0029-18610555" in wrong_rules.reason
    assert missing.status == "not_found" and "Нет такого фонда" in missing.reason
    assert seen.count("/funds/") == 1                          # список фондов — из кэша для всех фондов
