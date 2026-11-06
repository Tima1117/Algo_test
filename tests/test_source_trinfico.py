"""УК ТРИНФИКО: разбор настоящих страниц www.trinfico-am.ru (сняты 06.11.2026, данные по 03.11.2026), без сети.

Фикстуры — страницы фондов, урезанные дословными кусками: из таблицы истории оставлены новейшие строки
и две самые старые. Контрольные значения сверены со страницами фондов на сайте
(вкладка «Таблица стоимости пая и СЧА», таблица «Динамика стоимости пая на 31.10.2026»,
вкладка «Сведения о фонде»).
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
from pif.robots import Robots
from pif.sources.base import Context
from pif.sources.trinfico import (BASE, LIST_URL, TrinficoAdapter, history_currency, name_key,
                                  parse_fund_info, parse_fund_list, parse_history, parse_returns, pays_income)
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "trinfico"

# номер правил -> (название в реестре ЦБ, страница на сайте)
FUNDS = {
    "3863": ("Лидар", "opif-rfi-lidar"),
    "4344": ("Мировые доходные облигации", "mirovye-dokhodnye-obligatsii"),
    "4275": ("Российские доходные облигации", "rossiyskie-dokhodnye-obligatsii"),
    "3264": ("ТРИНФИКО Капиталист", "trinfico-kapitalist"),
    "2984": ("ТРИНФИКО ЭКОФИНАНС", "trinfico-ekofinans"),
}


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def page(slug: str) -> str:
    return read(f"{slug}.html").decode("utf-8")


def make_fund(rules: str, name: str | None = None, status: str = "Сформирован") -> Fund:
    name = name or FUNDS[rules][0]
    return Fund(rules=rules, name=name, full_name=f"ОПИФ рыночных финансовых инструментов «{name}»", type="open",
                category="", status=status, reg_date="2019-09-30", formation_end=None,
                uk_name="Акционерное общество \"Управляющая Компания ТРИНФИКО\"",
                uk_short="АО \"Управляющая Компания ТРИНФИКО\"", uk_ogrn="1027700084730",
                uk_inn="7701155020", uk_site="https://www.trinfico-am.ru", exchange_tradable=False)


# ---------- список фондов и сопоставление ----------

def test_fund_list():
    site = parse_fund_list(read("opif.html"))
    assert site["/opif/opif-rfi-lidar/"] == "ОПИФ РФИ «Лидар»"
    assert site["/opif/trinfico-ekofinans/"] == "ОПИФ РФИ «ТРИНФИКО ЭКОФИНАНС»"
    assert site["/opif/trinfico-global-ru/"] == "ЗПИФ РФИ «ТРИНФИКО Глобал.ру»"
    assert len(site) == 8                       # 5 ОПИФ + 3 ЗПИФ; карточки «ПОДРОБНЕЕ» — те же адреса
    assert {name_key(site[f"/opif/{slug}/"]) for _, slug in FUNDS.values()} == {name_key(n) for n, _ in FUNDS.values()}


def test_fund_list_broken():
    html = read("opif.html").decode()
    with pytest.raises(UnknownFormat):
        parse_fund_list(html.replace('href="/opif/', 'href="/funds/'))
    with pytest.raises(UnknownFormat):
        parse_fund_list(b"Forbidden\nTransaction ID: 123")


@pytest.mark.parametrize("rules", list(FUNDS))
def test_fund_info_rules(rules):
    name, slug = FUNDS[rules]
    got_rules, full_name = parse_fund_info(page(slug))
    assert got_rules == rules
    assert full_name == f"Открытый паевой инвестиционный фонд рыночных финансовых инструментов «{name}»"


def test_fund_info_broken():
    html = page("opif-rfi-lidar")
    with pytest.raises(UnknownFormat):        # нет номера правил в строке «Правила Фонда»
        parse_fund_info(html.replace("за № 3863", "за номером"))
    with pytest.raises(UnknownFormat):        # переименована строка
        parse_fund_info(html.replace("Правила Фонда", "Документы фонда"))
    with pytest.raises(UnknownFormat):        # нет вкладки
        parse_fund_info(html.replace('id="tab-info"', 'id="tab-other"'))


# ---------- история СЧА и пая ----------

def test_history_lidar_values():
    pts = parse_history(page("opif-rfi-lidar"))
    by_date = {p.date: p for p in pts}
    assert len(pts) == 27                                      # 25 новейших + 2 самые старые строки
    assert [p.date for p in pts] == sorted(by_date)
    assert pts[-1].date == date(2026, 11, 3)
    assert (pts[-1].nav, pts[-1].price) == (223_496_642.75, 700.62)       # СЧА в рублях, без множителей
    assert (by_date[date(2026, 10, 30)].nav, by_date[date(2026, 10, 30)].price) == (224_401_553.32, 702.24)
    assert (by_date[date(2026, 9, 30)].nav, by_date[date(2026, 9, 30)].price) == (233_076_124.30, 684.91)
    assert (pts[0].date, pts[0].nav, pts[0].price) == (date(2021, 10, 28), 125_342_403.51, 1012.84)
    assert history_currency(page("opif-rfi-lidar")) == "RUB"
    assert_series(pts, "trinfico_3863_lidar")


@pytest.mark.parametrize("rules,last", [
    ("4344", (166_309_928.26, 1074.24)),
    ("4275", (1_353_344_842.55, 1698.99)),
    ("3264", (65_522_907.88, 1163.91)),
    ("2984", (195_683_060.28, 1279.32)),
])
def test_history_last_point(rules, last):
    slug = FUNDS[rules][1]
    pts = parse_history(page(slug))
    assert len(pts) == 6
    assert pts[-1].date == date(2026, 11, 3)
    assert (pts[-1].nav, pts[-1].price) == last
    assert history_currency(page(slug)) == "RUB"
    assert_series(pts, f"trinfico_{rules}_{slug.replace('-', '_')}")


def test_history_broken():
    html = page("trinfico-kapitalist")
    with pytest.raises(UnknownFormat):        # пропала колонка СЧА
        parse_history(html.replace("Стоимость чистых активов <br>", "Чистые активы <br>"))
    with pytest.raises(UnknownFormat):        # незнакомая валюта в заголовке
        parse_history(html.replace("(РСИП), рубли", "(РСИП), тугрики"))
    with pytest.raises(UnknownFormat):        # в строке не хватает ячейки
        parse_history(html.replace('<td class="cost-dynamics__table--td">1 163.91</td>', "", 1))
    with pytest.raises(UnknownFormat):        # мусор вместо числа
        parse_history(html.replace(">1 163.91<", ">н.д.руб<", 1))
    with pytest.raises(UnknownFormat):        # нет таблицы
        parse_history(html.replace('id="cost-dynamics-table-pai-and-scha"', 'id="x"'))


def test_history_dash_is_none_not_zero():
    html = page("trinfico-kapitalist").replace(">65 522 907.88<", "> - <", 1)
    pts = parse_history(html)
    assert pts[-1].date == date(2026, 11, 3) and pts[-1].nav is None and pts[-1].price == 1163.91


# ---------- официальная доходность ----------

def test_returns_lidar():
    url = BASE + "/opif/opif-rfi-lidar/"
    got = parse_returns(page("opif-rfi-lidar"), url)
    assert [(r.period, r.value_pct, r.as_of, r.source_url) for r in got] == [
        ("1m", 2.53, date(2026, 10, 31), url), ("3m", -1.61, date(2026, 10, 31), url),
        ("6m", -5.13, date(2026, 10, 31), url), ("1y", -4.54, date(2026, 10, 31), url)]
    # УК считает от конца месяца до конца месяца по цене пая: 702,24 (30.10) / 684,91 (30.09) − 1
    by_date = {p.date: p.price for p in parse_history(page("opif-rfi-lidar"))}
    assert round((by_date[date(2026, 10, 30)] / by_date[date(2026, 9, 30)] - 1) * 100, 2) == 2.53


@pytest.mark.parametrize("rules,values", [
    ("4344", [-2.55, 1.40, 5.39, 2.81]),
    ("4275", [0.65, 1.47, 4.12, 11.71]),
    ("3264", [8.50, 3.40, -7.23, -1.33]),
    ("2984", [3.30, 3.19, 1.86, 7.50]),
])
def test_returns_values(rules, values):
    got = parse_returns(page(FUNDS[rules][1]), "u")
    assert [r.period for r in got] == ["1m", "3m", "6m", "1y"]
    assert [r.value_pct for r in got] == values
    assert {r.as_of for r in got} == {date(2026, 10, 31)}


def test_returns_broken_and_absent():
    html = page("trinfico-ekofinans")
    with pytest.raises(UnknownFormat):        # нет колонки «1 год»
        parse_returns(html.replace('<th class="cost-table__th">1 год</th>', '<th class="cost-table__th">2 года</th>'), "u")
    with pytest.raises(UnknownFormat):        # нет даты в заголовке
        parse_returns(html.replace("Динамика стоимости пая\n                            на 31.10.2026",
                                   "Динамика стоимости пая"), "u")
    with pytest.raises(UnknownFormat):        # не процент
        parse_returns(html.replace(">+3.30%<", ">n/a<"), "u")
    # Пустая ячейка (фонд моложе периода) — период пропускается, а не 0.
    assert [r.period for r in parse_returns(html.replace(">+7.50%<", ">-<"), "u")] == ["1m", "3m", "6m"]
    # Таблицы нет вовсе — None: «УК не опубликовала», а не ошибка разбора.
    assert parse_returns(html.replace("cost-table--dynamic", "cost-table--other"), "u") is None


def test_pays_income():
    assert pays_income(page("opif-rfi-lidar"))                 # «с выплатой ежеквартального дохода»
    assert not any(pays_income(page(slug)) for r, (_, slug) in FUNDS.items() if r != "3863")


def test_robots_allows_pages_but_not_chart_api():
    robots = Robots(read("robots.txt").decode(), "pif-registry")
    assert robots.allowed(LIST_URL)
    assert robots.allowed(BASE + "/opif/opif-rfi-lidar/")
    # график берёт данные отсюда — закрыто «Disallow: /*?*»; КИД и справки — «Disallow: /upload/»
    assert not robots.allowed(BASE + "/include/data.php?fund=4609ВыпЦБ&type=COST&period=begin")
    assert not robots.allowed(BASE + "/upload/iblock/f56/z30u435nxrt7v4igrzxoymwl6fgwhl8i/Лидар 30.09.2026.pdf")


# ---------- адаптер целиком: настоящий Fetcher, сеть подменена фикстурами ----------

@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0)


def _ctx(tmp_path: Path, handler) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw",
                   transport=httpx.MockTransport(wrapped))
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.trinfico")), seen


def _site(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    name = "opif.html" if path == "/opif/" else path.strip("/").split("/")[-1] + ".html"
    if not (FIX / name).exists():
        return httpx.Response(404, content=b"not found")
    return httpx.Response(200, content=read(name), headers={"content-type": "text/html; charset=UTF-8"})


def test_adapter_collects_all_funds(tmp_path):
    ctx, seen = _ctx(tmp_path, _site)
    result = TrinficoAdapter().collect(ctx, [make_fund(r) for r in FUNDS])
    assert [fd.rules for fd in result] == list(FUNDS)
    # список + 5 страниц ОПИФ; страницы ЗПИФ не открываем
    assert seen == [LIST_URL] + [f"{BASE}/opif/{slug}/" for _, slug in FUNDS.values()]
    for fd in result:
        slug = FUNDS[fd.rules][1]
        assert fd.status == "ok", (fd.rules, fd.reason, fd.errors)
        assert not fd.errors
        assert fd.currency == "RUB"
        assert fd.page_url == f"{BASE}/opif/{slug}/"
        seg = fd.segments[0]
        assert seg.raw_url == fd.page_url and seg.page_url == fd.page_url
        assert seg.points and min(p.date for p in seg.points) >= date(2025, 9, 1)   # строки 2017–2021 отрезаны
        assert seg.points[-1].date == date(2026, 11, 3)
        assert [r.period for r in fd.official_returns] == ["1m", "3m", "6m", "1y"]
    lidar = result[0]
    assert len(lidar.segments[0].points) == 25
    assert any("выплачивает доход" in w for w in lidar.warnings)
    assert not any("выплачивает доход" in w for fd in result[1:] for w in fd.warnings)


def test_adapter_not_found_checks_only_open_funds(tmp_path):
    ctx, seen = _ctx(tmp_path, _site)
    fd = TrinficoAdapter().collect(ctx, [make_fund("9999", "Неизвестный фонд")])[0]
    assert fd.status == "not_found"
    assert "№ 9999" in fd.reason
    # открыты все пять страниц ОПИФ (по разу), страницы ЗПИФ — нет
    assert seen[0] == LIST_URL
    assert sorted(seen[1:]) == sorted(f"{BASE}/opif/{slug}/" for _, slug in FUNDS.values())


def test_adapter_page_with_other_rules_is_not_taken(tmp_path):
    # Название совпало, но на странице правила другого фонда — фонд не подставляем.
    ctx, _ = _ctx(tmp_path, _site)
    fd = TrinficoAdapter().collect(ctx, [make_fund("1111", "Лидар")])[0]
    assert fd.status == "not_found"


def test_adapter_forming_fund_with_empty_table(tmp_path):
    html = page("trinfico-ekofinans")
    start = html.index('<tbody class="cost-dynamics__table--tbody">')
    end = html.index("</tbody>", start)
    empty = html[:start] + '<tbody class="cost-dynamics__table--tbody">' + html[end:]

    def handler(request):
        if request.url.path == "/opif/trinfico-ekofinans/":
            return httpx.Response(200, content=empty.encode(), headers={"content-type": "text/html"})
        return _site(request)

    ctx, _ = _ctx(tmp_path, handler)
    fd = TrinficoAdapter().collect(ctx, [make_fund("2984", status="Формируется")])[0]
    assert fd.status == "not_formed"


def test_adapter_list_403_is_blocked_with_one_request(tmp_path):
    ctx, seen = _ctx(tmp_path, lambda r: httpx.Response(403, content=b"Forbidden\nTransaction ID: 1",
                                                        headers={"content-type": "text/plain"}))
    result = TrinficoAdapter().collect(ctx, [make_fund(r) for r in FUNDS])
    assert [fd.status for fd in result] == ["blocked"] * 5
    assert seen == [LIST_URL]


def test_adapter_broken_page_is_parse_error_for_that_fund_only(tmp_path):
    def handler(request):
        if request.url.path == "/opif/trinfico-kapitalist/":
            bad = page("trinfico-kapitalist").replace("(РСИП), рубли", "(РСИП)")
            return httpx.Response(200, content=bad.encode(), headers={"content-type": "text/html"})
        return _site(request)

    ctx, _ = _ctx(tmp_path, handler)
    result = {fd.rules: fd for fd in TrinficoAdapter().collect(ctx, [make_fund(r) for r in FUNDS])}
    assert result["3264"].status == "parse_error" and result["3264"].errors
    assert all(result[r].status == "ok" for r in FUNDS if r != "3264")
