"""УК «МЕТРОПОЛЬ»: главная раздела ПИФ и «Экспорт котировок» «Метрополь Золотое руно».

Фикстуры — настоящие ответы pif.am-metropol.ru 03.11.2026:
- index.html — главная целиком (таблица СЧА с доходностью, подвал с номерами правил);
- golden_fleece_export.html — «Экспорт котировок», из ~5600 строк оставлено 15 (конец месяцев для
  сверки доходности, 04–06.03.2026 с ошибкой сайта — цена и СЧА переставлены местами за 05.03,
  начало истории с «.00» вместо СЧА), остальная страница без изменений.
Сверено на сайте: 02.11.2026 — пай 354,29 руб., СЧА 11 977 302,22 руб. (на главной 11 977 тыс.).
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund, SeriesPoint
from pif.parsing import UnknownFormat
from pif.sources import metropol as m
from pif.sources.base import Context
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "metropol"
EXPORT = m.EXPORT_URL.format(slug="golden_fleece")


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str = "0147-70232539") -> Fund:
    return Fund(rules=rules, name="Метрополь Золотое руно» под управлением ООО «УК «МЕТРОПОЛЬ",
                full_name="ОПИФ РФИ «Метрополь Золотое руно» под управлением ООО «УК «МЕТРОПОЛЬ»",
                type="open", category="", status="Сформирован", reg_date="2003-11-12",
                formation_end="2003-12-22", uk_name="ООО «УК «МЕТРОПОЛЬ»", uk_short="УК МЕТРОПОЛЬ",
                uk_ogrn="1027706025885", uk_inn="7706285907", uk_site="http://www.am-metropol.ru",
                exchange_tradable=False)


def test_home_rules_and_returns():
    assert m.parse_rules_names(raw("index.html"))["0147-70232539"] == "Метрополь Золотое руно"
    published, rows = m.parse_home(raw("index.html"))
    assert published == date(2026, 11, 2)
    gf = rows["golden_fleece"]
    assert "Золото еруно" in gf["name"]                    # опечатка сайта — сопоставление без пробелов
    assert m.name_key(gf["name"]) == m.name_key("Метрополь Золотое руно")
    assert gf["price"] == 354.29 and gf["nav_thousands"] == 11977
    assert gf["returns"] == {"3m": 6.26, "6m": -5.78, "1y": -2.68}


def test_export_series_values_and_units():
    name, pts, skipped = m.parse_export(raw("golden_fleece_export.html"))
    assert_series(pts, "metropol_golden_fleece_export")
    assert name == "Метрополь Золотое руно"
    # на сайте за 05.03.2026 «Цена 14546018.65, СЧА 413.64» — строку не берём, но и не молчим
    assert date(2026, 3, 5) not in {p.date for p in pts}
    assert len(skipped) == 1 and skipped[0].startswith("05.03.2026")
    by = {p.date: p for p in pts}
    assert by[date(2026, 11, 2)] == SeriesPoint(date(2026, 11, 2), 11977302.22, 354.29)   # рубли
    assert by[date(2026, 10, 30)] == SeriesPoint(date(2026, 10, 30), 12210215.86, 361.27)
    assert by[date(2003, 12, 8)] == SeriesPoint(date(2003, 12, 8), None, 100.0)   # «.00» — не ноль
    assert [p.date for p in pts] == sorted(by)


def test_returns_are_month_end_and_match_series():
    _, pts, _ = m.parse_export(raw("golden_fleece_export.html"))
    as_of = m.returns_as_of(date(2026, 11, 2), pts)
    assert as_of == date(2026, 10, 30)
    assert m.check_returns(pts, as_of, {"3m": 6.26, "6m": -5.78, "1y": -2.68}) == []
    # на последнюю дату ряда (02.11) значения не сходятся — значит дата выбрана правильно
    assert m.check_returns(pts, date(2026, 11, 2), {"3m": 6.26}) != []


def test_broken_pages_raise():
    home = raw("index.html").decode()
    with pytest.raises(UnknownFormat):
        m.parse_home(home.replace(">6\xa0мес<", ">полгода<"))
    with pytest.raises(UnknownFormat):
        m.parse_home(home.replace("опубликовано", "размещено"))
    assert "0147-70232539" not in m.parse_rules_names(home.replace("0147-70232539", "б/н"))
    with pytest.raises(UnknownFormat):
        m.parse_rules_names(home.replace("0147-70232539", "б/н").replace("1702-94164529", "б/н"))
    exp = raw("golden_fleece_export.html").decode()
    with pytest.raises(UnknownFormat):
        m.parse_export(exp.replace("<b>СЧА</b>", "<b>Объём</b>"))
    with pytest.raises(UnknownFormat):
        m.parse_export(exp.replace('nowrap>354.29</td>', 'nowrap>354.29</td><td>лишняя</td>'))
    with pytest.raises(UnknownFormat):
        m.parse_export(exp.replace("Экспорт котировок", "Котировки"))


def _ctx(tmp_path, monkeypatch, routes):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url in routes:
            return httpx.Response(200, content=routes[url], headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, content=b"")

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.metropol")), seen


def test_adapter_end_to_end(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, {m.HOME_URL: raw("index.html"), EXPORT: raw("golden_fleece_export.html")})
    res = m.MetropolAdapter().collect(ctx, [_fund(), _fund("9999-99999999")])
    fd = res[0]
    assert fd.status == "ok" and fd.currency == "RUB"
    assert fd.page_url == m.PAGE_URL.format(slug="golden_fleece")
    pts = fd.segments[0].points
    assert pts[0].date == date(2025, 9, 1) and pts[-1].date == date(2026, 11, 2)
    rets = {r.period: (r.value_pct, r.as_of) for r in fd.official_returns}
    assert rets == {"3m": (6.26, date(2026, 10, 30)), "6m": (-5.78, date(2026, 10, 30)),
                    "1y": (-2.68, date(2026, 10, 30))}
    assert not any("не сходится" in w for w in fd.warnings)
    assert any("05.03.2026" in w and "перепутаны" in w for w in fd.warnings)
    assert res[1].status == "not_found"
    assert seen == [m.HOME_URL, EXPORT]


def test_adapter_rejects_export_of_other_fund(tmp_path, monkeypatch):
    other = raw("golden_fleece_export.html").decode().replace("Метрополь Золотое руно&quot;", "Метрополь Зевс&quot;")
    ctx, _ = _ctx(tmp_path, monkeypatch, {m.HOME_URL: raw("index.html"), EXPORT: other.encode()})
    fd = m.MetropolAdapter().collect(ctx, [_fund()])[0]
    assert fd.status == "parse_error" and fd.segments == []
