"""Адаптер ООО «УК «А-Капитал»: разбор настоящих ответов сайта.

Фикстуры (сбор 03.11.2026):
* graph_5806_2026-10.json, graph_5806_2026-11.json — GET /api/opif/5806/rub/graph за октябрь и ноябрь
  2026 («А-Капитал Акции»); graph_5570_2023-11_empty.json — окно до формирования «Рантье» (пустой ряд).
* export_5806_2025-09.xlsx — «Экспорт в Excel» /api/opif/5806/rub/excel/2025-09-01/2025-09-30.
* page_aktsii_cut.html — страница /opif/a-kapital-aktsii/, opif_list_cut.html — /opif/. Обе — настоящие
  ответы, оборванные сетью примерно на 24 КБ (дальше соединение с этого хоста замирает), но блок
  доходности и все карточки фондов в них целиком.
Сверено глазами: карточка «А-Капитал Акции» на /opif/ — «Стоимость пая: 787,41 ₽, СЧА: 615 660 088,02 ₽,
Дата: 02.11.2026»; выгрузка Excel за октябрь — 30.10.2026: 637 873 518,74 / 804,33 и 01.10.2026:
603 823 035,17 / 719,96; доходность на странице «на 30.10.2026»: 12,50% / 13,81% / −6,90% / −1,86%
(совпадает с расчётом по ряду от последнего рабочего дня месяца).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import acapital
from pif.sources.acapital import (EXPORT_URL, FUND_PAGE, GRAPH_URL, SITE_FUNDS, ACapitalAdapter, find_gaps,
                                  match_funds, month_windows, parse_export_title, parse_graph, parse_returns)
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "acapital"
OCT = FIX / "graph_5806_2026-10.json"
NOV = FIX / "graph_5806_2026-11.json"
EMPTY = FIX / "graph_5570_2023-11_empty.json"
EXPORT = FIX / "export_5806_2025-09.xlsx"
PAGE = FIX / "page_aktsii_cut.html"
LIST = FIX / "opif_list_cut.html"
AKTSII_PAGE = "https://www.acapital-am.ru/opif/a-kapital-aktsii/"

# Названия — как в реестре ЦБ (data/registry.json).
REGISTRY = [
    ("5996", "А-Капитал Акции", 5806),
    ("0211-58233714", "А-Капитал Валютные облигации", 859),
    ("5989", "А-Капитал Золото", 5807),
    ("0450-75409623", "А-Капитал Облигации", 858),
    ("5739", "А-Капитал Облигации Рантье", 5570),
    ("0047-18548678", "А-Капитал Сбалансированный", 857),
]


def make_fund(rules: str, name: str, status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name,
                full_name=f"Открытый паевой инвестиционный фонд рыночных финансовых инструментов «{name}»",
                type="open", category="Фонд рыночных финансовых инструментов", status=status,
                reg_date="2024-01-29", formation_end=None,
                uk_name="Общество с ограниченной ответственностью «УК «А-Капитал»", uk_short="ООО «УК «А-Капитал»",
                uk_ogrn="1067746469757", uk_inn="7707583543", uk_site="https://www.acapital-am.ru",
                exchange_tradable=False)


def graph(path: Path = OCT) -> dict:
    return json.loads(path.read_text("utf-8"))


# ---------------- ряд (API графика) ----------------

def test_graph_october():
    pts = parse_graph(OCT.read_bytes(), fund_id=5806)
    assert len(pts) == 22
    assert (pts[0].date, pts[-1].date) == (date(2026, 10, 1), date(2026, 10, 30))
    by = {p.date: p for p in pts}
    assert (by[date(2026, 10, 30)].nav, by[date(2026, 10, 30)].price) == (637873518.74, 804.33)
    assert (by[date(2026, 10, 1)].nav, by[date(2026, 10, 1)].price) == (603823035.17, 719.96)
    assert date(2026, 10, 3) not in by                        # суббота — расчёта нет


def test_graph_current_month():
    # текущий месяц: окно до конца месяца, пока одна точка — та же, что в карточке на /opif/
    pts = parse_graph(NOV.read_bytes(), fund_id=5806)
    assert [(p.date, p.nav, p.price) for p in pts] == [(date(2026, 11, 2), 615660088.02, 787.41)]


def test_graph_empty_window():
    assert parse_graph(EMPTY.read_bytes(), fund_id=5570) == []


def _row(d):
    return d["data"]["data"][0]


@pytest.mark.parametrize("broken", [
    lambda d: _row(d).pop("asset_cost"),
    lambda d: _row(d).pop("fund_cost"),
    lambda d: _row(d).pop("date_original"),
    lambda d: _row(d).update(fund_cost="0.00"),
    lambda d: _row(d).update(asset_cost="-603823035.17"),
    lambda d: _row(d).update(fund_cost="719,96 у.е."),
    lambda d: _row(d).update(date_original="2026-09-30"),           # вне окна from/to
    lambda d: _row(d).update(id=858),                               # точка чужого фонда
    lambda d: d["data"]["data"].append(dict(_row(d))),             # дубль даты
    lambda d: d["data"].update(currency="usd"),                     # пересчёт в доллары, а не валюта пая
    lambda d: d["data"].update(data={"2026-10-01": "719.96"}),
    lambda d: d["data"].pop("fundId"),
    lambda d: d.update(data=None),
    lambda d: d.pop("data"),
])
def test_graph_broken(broken):
    d = graph()
    broken(d)
    with pytest.raises(UnknownFormat):
        parse_graph(d, fund_id=5806)


def test_graph_other_fund():
    with pytest.raises(UnknownFormat, match="fundId"):
        parse_graph(OCT.read_bytes(), fund_id=858)


# ---------------- выгрузка Excel: название фонда ----------------

def test_export_title():
    assert parse_export_title(EXPORT.read_bytes()) == ("А-Капитал Акции", "rub")


def _patched_xlsx(cell: str, value) -> bytes:
    wb = openpyxl.load_workbook(io.BytesIO(EXPORT.read_bytes()))
    wb.worksheets[0][cell] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("content", [
    lambda: _patched_xlsx("A1", "А-Капитал Акции"),          # нет «Валюта: …»
    lambda: _patched_xlsx("A1", None),
    lambda: _patched_xlsx("C2", "Цена"),                      # шапка таблицы сменилась
    lambda: OCT.read_bytes(),                                  # не xlsx
])
def test_export_title_broken(content):
    with pytest.raises(UnknownFormat):
        parse_export_title(content())


# ---------------- официальная доходность ----------------

def test_returns_page():
    rets = parse_returns(PAGE.read_bytes(), AKTSII_PAGE)
    assert {r.period: r.value_pct for r in rets} == {"1m": 12.50, "3m": 13.81, "6m": -6.90, "1y": -1.86}
    assert {r.as_of for r in rets} == {date(2026, 10, 30)}
    assert {r.source_url for r in rets} == {AKTSII_PAGE}


@pytest.mark.parametrize("old, new", [
    ('class="strategy__points', 'class="strategy__other'),                 # блока нет
    ("инвестиционного пая на 30.10.2026", "инвестиционного пая"),         # нет даты расчёта
    ("<td>1 год</td>", "<td>12 мес.</td>"),                               # подписи сменились
    ("<td>12,50%</td>", ""),                                              # значений меньше подписей
    ("<td>13,81%</td>", "<td>13,81 п.п.</td>"),                           # не число
])
def test_returns_broken(old, new):
    html = PAGE.read_text("utf-8")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_returns(html.replace(old, new, 1), AKTSII_PAGE)


def test_returns_dash_is_not_published():
    html = PAGE.read_text("utf-8").replace("<td>-6,90%</td>", "<td>-</td>", 1)
    assert {r.period for r in parse_returns(html, AKTSII_PAGE)} == {"1m", "3m", "1y"}


# ---------------- фонды сайта и сопоставление ----------------

def test_site_funds_as_on_list_page():
    """SITE_FUNDS выписаны из карточек /opif/: id элемента, адрес страницы и название."""
    html = LIST.read_text("utf-8")
    cards = re.findall(r'id="bx_\d+_(\d+)">\s*<a href="/opif/([a-z-]+)/" class="entry__lnk"></a>\s*'
                       r'<div class="entry__title"><span>([^<]+)</span>', html)
    assert {(int(i), slug, name) for i, slug, name in cards} == {(f.id, f.slug, f.name) for f in SITE_FUNDS}


def test_match_funds():
    funds = [make_fund(rules, name) for rules, name, _ in REGISTRY]
    matched = match_funds(funds)
    assert {r: sf.id for r, sf in matched.items()} == {rules: fid for rules, _, fid in REGISTRY}
    # без натяжек: «Облигации» не находит «Облигации Рантье» и наоборот, чужой фонд — не находится
    assert match_funds([make_fund("x", "А-Капитал Облигации плюс")]) == {}
    assert match_funds([make_fund("y", "Капитал Акции")]) == {}


def test_month_windows():
    w = month_windows(date(2025, 9, 1), date(2026, 11, 3))
    assert len(w) == 15
    assert w[0] == (date(2025, 9, 1), date(2025, 9, 30))
    assert w[3:5] == [(date(2025, 12, 1), date(2025, 12, 31)), (date(2026, 1, 1), date(2026, 1, 31))]
    assert w[5] == (date(2026, 2, 1), date(2026, 2, 28))
    assert w[-1] == (date(2026, 11, 1), date(2026, 11, 30))
    # хвост с середины месяца — окно всё равно с 1-го числа (адрес стабилен)
    assert month_windows(date(2026, 10, 15), date(2026, 11, 3)) == w[-2:]


def test_find_gaps():
    ds = [date(2025, 12, 30), date(2026, 1, 12), date(2026, 1, 13)]      # каникулы — не пропуск
    assert find_gaps(ds) == []
    assert find_gaps([date(2026, 3, 2), date(2026, 4, 1)]) == [(date(2026, 3, 2), date(2026, 4, 1))]


# ---------------- сбор целиком (без сети) ----------------

class FakeHttp:
    """Отвечает фикстурами: выгрузка Excel и окна графика фонда 5806; страница — по флагу."""

    def __init__(self, page_ok: bool = False, export: Path = EXPORT):
        self.page_ok = page_ok
        self.export = export
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        params = kw.get("params") or {}
        if url.startswith(acapital.SITE + "/api/opif/") and "/excel/" in url:
            path = self.export
        elif url == GRAPH_URL.format(id=5806):
            path = {"2026-10-01": OCT, "2026-11-01": NOV}[params["from"]]
        elif url.startswith(acapital.SITE + "/opif/"):
            if not self.page_ok:
                raise FetchError(url, "сайт не отвечает (ReadTimeout)")
            path = PAGE
        else:
            raise AssertionError(f"неожиданный запрос {url} {params}")
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(),
                      "2026-11-03T12:00:00+00:00", kw.get("fmt", "html"))


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(acapital, "_today", lambda: date(2026, 11, 3))


def run(http: FakeHttp, funds: list[Fund], last: date = date(2026, 10, 25)):
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"),
                  last_dates={f.rules: last for f in funds})
    return {fd.rules: fd for fd in ACapitalAdapter().collect(ctx, funds)}


def test_collect_tail_without_pages():
    http = FakeHttp(page_ok=False)
    fd = run(http, [make_fund("5996", "А-Капитал Акции")])["5996"]
    assert fd.status == "ok", fd.reason
    assert fd.currency == "RUB" and fd.page_url == AKTSII_PAGE
    # докачка хвоста: последняя точка 25.10 -> окна октября и ноября
    assert [s.raw_url for s in fd.segments] == [
        "https://www.acapital-am.ru/api/opif/5806/rub/graph?from=2026-10-01&to=2026-10-31",
        "https://www.acapital-am.ru/api/opif/5806/rub/graph?from=2026-11-01&to=2026-11-30"]
    assert [len(s.points) for s in fd.segments] == [22, 1]
    assert {s.page_url for s in fd.segments} == {AKTSII_PAGE}
    assert fd.segments[-1].points[-1].price == 787.41
    # страница не загрузилась -> доходности нет, но ряд собран
    assert fd.official_returns == []
    assert any("доходность не получена" in w for w in fd.warnings)
    # каждый запрос к API — отдельным соединением, с номером правил в манифесте
    api_calls = [(u, kw) for u, kw in http.calls if "/api/" in u]
    assert api_calls[0][0] == EXPORT_URL.format(id=5806, start="2025-09-01", end="2025-09-30")
    assert all(kw["headers"] == {"Connection": "close"} and kw["funds"] == ["5996"] for _, kw in api_calls)


def test_collect_with_returns():
    fd = run(FakeHttp(page_ok=True), [make_fund("5996", "А-Капитал Акции")])["5996"]
    assert fd.status == "ok" and not fd.warnings
    assert {r.period: r.value_pct for r in fd.official_returns} == {"1m": 12.5, "3m": 13.81, "6m": -6.9, "1y": -1.86}


def test_collect_pages_probe_once():
    """После сетевого сбоя первой страницы остальные страницы фондов не запрашиваются."""
    http = FakeHttp(page_ok=False)
    res = run(http, [make_fund("5996", "А-Капитал Акции"), make_fund("5996-2", "А-Капитал Акции")])
    pages = [u for u, _ in http.calls if u == FUND_PAGE.format(slug="a-kapital-aktsii")]
    assert len(pages) == 1
    assert all(fd.status == "ok" for fd in res.values())
    assert any("страницы сайта не загружаются" in w for w in res["5996-2"].warnings)


def test_collect_wrong_fund_under_id():
    # под id «Облигаций» выгрузка вернула бы «Акции» — данные такого фонда не берём
    fd = run(FakeHttp(), [make_fund("0450-75409623", "А-Капитал Облигации")])["0450-75409623"]
    assert fd.status == "parse_error" and "А-Капитал Акции" in fd.reason
    assert not fd.segments


def test_collect_not_found_and_not_formed():
    res = run(FakeHttp(), [make_fund("1", "А-Капитал Облигации плюс"),
                           make_fund("2", "А-Капитал Акции", status="Формируется")])
    assert res["1"].status == "not_found" and not res["1"].segments
    assert res["2"].status == "not_formed"


def test_collect_payout_fund_warns(monkeypatch):
    # фонд с выплатами (на сайте это «Рантье»): суммы УК публикует только в PDF — предупреждение, не ошибка
    assert acapital.PAYOUT_FUNDS == {5570}
    monkeypatch.setattr(acapital, "PAYOUT_FUNDS", {5806})
    fd = run(FakeHttp(page_ok=True), [make_fund("5996", "А-Капитал Акции")])["5996"]
    assert fd.status == "ok" and fd.payouts == []
    assert any("выплаты не собраны" in w for w in fd.warnings)
