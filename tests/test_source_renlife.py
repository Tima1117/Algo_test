"""СК «Ренессанс Жизнь»: разбор настоящих страниц renlife.ru (урезанные копии от 2026-11-03).

Значения сверены со страницами фондов (вкладка «Динамика стоимости» → «Таблица стоимости пая и СЧА»):
«Разумные инвестиции» (№ 7200) на 02.11.2026 — пай 99.7109 руб., СЧА 3 108 212.70 руб.;
«Сбалансированный» (№ 7217) на 02.11.2026 — пай 102.9796 руб., СЧА 51 717 775.26 руб.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat, norm_text
from pif.sources.base import Context
from pif.sources.renlife import (LIST_URL, NO_RETURNS_WARNING, RenlifeAdapter, parse_dynamics_placeholder,
                                 parse_fund_list, parse_fund_rules, parse_series)

FIX = Path(__file__).parent / "fixtures" / "renlife"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- список фондов ----------

def test_fund_list_has_all_five_funds_with_pages():
    funds = parse_fund_list(_read("list.html"))
    assert len(funds) == 5
    title, url = funds["ренессанс жизнь разумные инвестиции"]
    assert title == "ОПИФ рыночных финансовых инструментов «Ренессанс Жизнь – Разумные инвестиции»"
    assert url == "https://www.renlife.ru/funds/grow_your_capital/"
    assert funds["ренессанс жизнь сбалансированный"][1] == "https://www.renlife.ru/funds/balanced/"
    assert funds["ренессанс жизнь накопительный"][1] == "https://www.renlife.ru/funds/cumulative/"
    assert funds["ренессанс жизнь информационные технологии"][1] == "https://www.renlife.ru/funds/technologies/"
    assert funds["ренессанс жизнь финансовый сектор"][1] == "https://www.renlife.ru/funds/finance/"


def test_registry_names_match_site_names():
    funds = parse_fund_list(_read("list.html"))
    registry = ["Ренессанс Жизнь – Информационные технологии", "Ренессанс Жизнь – Накопительный",
                "Ренессанс Жизнь – Разумные инвестиции", "Ренессанс Жизнь – Сбалансированный",
                "Ренессанс Жизнь – Финансовый сектор"]
    assert all(norm_text(n) in funds for n in registry)


def test_fund_list_without_block_is_unknown_format():
    html = _read("list.html").replace("opif-list", "other-list")
    with pytest.raises(UnknownFormat):
        parse_fund_list(html)


# ---------- страница фонда ----------

def test_rules_from_fund_page():
    assert parse_fund_rules(_read("fund_7200.html")) == "7200"
    assert parse_fund_rules(_read("fund_7217.html")) == "7217"     # «07.08.2025 г. за № 7217»
    assert parse_fund_rules(_read("fund_8116.html")) == "8116"


def test_rules_row_missing_is_unknown_format():
    with pytest.raises(UnknownFormat):
        parse_fund_rules(_read("fund_7200.html").replace("Правила Фонда", "Правила"))


def test_series_7200_values_and_order():
    pts = parse_series(_read("fund_7200.html"))
    assert len(pts) == 16                                  # 12 свежих + 4 первых строки исходной таблицы
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    by = {p.date: p for p in pts}
    last = by[date(2026, 11, 2)]
    assert last.price == 99.7109 and last.nav == 3108212.7
    assert pts[-1] is last
    first = pts[0]                                         # дата завершения формирования
    assert first.date == date(2025, 8, 29) and first.price == 99.9874 and first.nav == 100927.33
    assert by[date(2025, 9, 1)].price == 99.9749 and by[date(2025, 9, 1)].nav == 100914.67
    assert by[date(2026, 10, 27)].price == 100.2455 and by[date(2026, 10, 27)].nav == 3124876.7


def test_series_7217_values():
    pts = parse_series(_read("fund_7217.html"))
    by = {p.date: p for p in pts}
    assert len(pts) == 12
    assert pts[0].date == date(2025, 12, 23) and pts[0].price == 99.9972 and pts[0].nav == 118096.65
    assert by[date(2026, 11, 2)].price == 102.9796 and by[date(2026, 11, 2)].nav == 51717775.26
    assert by[date(2026, 10, 30)].price == 104.2963 and by[date(2026, 10, 30)].nav == 52298004.84


def test_forming_fund_has_placeholder_instead_of_table():
    assert parse_dynamics_placeholder(_read("fund_8116.html")) == "Данные обновляются"
    assert parse_dynamics_placeholder(_read("fund_7200.html")) is None
    with pytest.raises(UnknownFormat):
        parse_series(_read("fund_8116.html"))


def test_empty_dynamics_tab_is_unknown_format():
    html = _read("fund_8116.html").replace("<p>Данные обновляются</p>", "")
    with pytest.raises(UnknownFormat):
        parse_dynamics_placeholder(html)


@pytest.mark.parametrize("old,new", [
    # пропала колонка СЧА
    ("<th>Стоимость чистых активов<br/>(СЧА), руб.</th>", ""),
    # единицы сменились на тысячи рублей
    ("(СЧА), руб.</th>", "(СЧА), тыс. руб.</th>"),
    # первая колонка больше не дата
    ("<th>Дата</th>", "<th>Период</th>"),
    # дубль даты
    ("<td>30.10.2026</td>", "<td>02.11.2026</td>"),
    # нулевая цена пая
    ("<td>99.7109</td>", "<td>0</td>"),
    # мусор вместо числа
    ("<td>3108212.7</td>", "<td>н.д.</td>"),
])
def test_broken_table_is_unknown_format(old, new):
    html = _read("fund_7200.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_series(html.replace(old, new, 1))


# ---------- collect целиком: настоящий Fetcher + MockTransport ----------

PAGES = {
    LIST_URL: "list.html",
    "https://www.renlife.ru/funds/grow_your_capital/": "fund_7200.html",
    "https://www.renlife.ru/funds/balanced/": "fund_7217.html",
    "https://www.renlife.ru/funds/cumulative/": "fund_8116.html",
}


def _fund(rules: str, name: str, status: str = "Сформирован", formation_end: str | None = None) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ РФИ «{name}»", type="open", category="",
                status=status, reg_date="2025-07-31", formation_end=formation_end,
                uk_name="ООО «СК «Ренессанс Жизнь»", uk_short="ООО «СК «Ренессанс Жизнь»",
                uk_ogrn="1047796714404", uk_inn="7725520440", uk_site="https://www.renlife.ru",
                exchange_tradable=False)


def _ctx(tmp_path: Path, monkeypatch, handler) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(wrapped), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr(http, "_wait_turn", lambda domain: None)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.renlife")), seen


def _site(request: httpx.Request) -> httpx.Response:
    name = PAGES.get(str(request.url))
    if name is None:
        return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
    return httpx.Response(200, content=(FIX / name).read_bytes(),
                          headers={"content-type": "text/html; charset=UTF-8"})


def test_collect_statuses(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, _site)
    funds = [
        _fund("7200", "Ренессанс Жизнь – Разумные инвестиции", formation_end="2025-08-29"),
        _fund("7217", "Ренессанс Жизнь – Сбалансированный", formation_end="2025-12-23"),
        _fund("8116", "Ренессанс Жизнь – Накопительный", status="Формируется"),
        _fund("9999", "Ренессанс Жизнь – Несуществующий"),
    ]
    by = {fd.rules: fd for fd in RenlifeAdapter().collect(ctx, funds)}

    fd = by["7200"]
    assert fd.status == "ok", fd.reason
    assert fd.currency == "RUB" and fd.page_url == "https://www.renlife.ru/funds/grow_your_capital/"
    pts = fd.segments[0].points
    assert pts[0].date == date(2025, 9, 1)                 # 29.08.2025 отрезана окном since
    assert pts[-1].date == date(2026, 11, 2) and pts[-1].price == 99.7109
    assert fd.segments[0].raw_url == "https://www.renlife.ru/funds/grow_your_capital/"
    assert fd.official_returns == [] and fd.warnings == [NO_RETURNS_WARNING]

    fd = by["7217"]                                        # молодой фонд: история с завершения формирования
    assert fd.status == "ok", fd.reason
    assert fd.segments[0].points[0].date == date(2025, 12, 23)

    fd = by["8116"]
    assert fd.status == "not_formed" and "Данные обновляются" in fd.reason and fd.segments == []

    fd = by["9999"]
    assert fd.status == "not_found" and fd.segments == []

    assert seen.count(LIST_URL) == 1                       # список фондов — один запрос на всю УК


def test_collect_rules_mismatch_is_not_found(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, _site)
    # Название совпало, а на странице фонда другой номер правил — не натягиваем.
    [fd] = RenlifeAdapter().collect(ctx, [_fund("7201", "Ренессанс Жизнь – Разумные инвестиции")])
    assert fd.status == "not_found" and "№ 7200" in fd.reason


def test_collect_formed_fund_with_late_history_is_partial(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, _site)
    # Будь «Сбалансированный» сформирован ещё в августе 2025, таблица с 23.12.2025 — неполная история.
    [fd] = RenlifeAdapter().collect(ctx, [_fund("7217", "Ренессанс Жизнь – Сбалансированный",
                                                formation_end="2025-08-29")])
    assert fd.status == "partial" and "23.12.2025" in fd.reason


def test_list_403_gives_blocked_for_every_fund(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch,
                     lambda r: httpx.Response(403, content=b"Forbidden", headers={"content-type": "text/plain"}))
    funds = [_fund("7200", "Ренессанс Жизнь – Разумные инвестиции"),
             _fund("7217", "Ренессанс Жизнь – Сбалансированный")]
    result = RenlifeAdapter().collect(ctx, funds)
    assert [fd.status for fd in result] == ["blocked", "blocked"]
    assert seen == [LIST_URL]
