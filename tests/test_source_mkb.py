"""УК МКБ Инвестиции (mkb): главная (номер правил -> id страницы), страница раскрытия, сбор — без сети.

Фикстуры — настоящие ответы www.mkb-am.ru, урезанные по длине (сайт отвечает из GitHub Actions на первый
запрос запуска, их и снимали тем же вежливым клиентом):
* home.html — главная от 04.11.2026: форма подписки со списком фондов и их id и подвал с номерами правил;
* products_31747.html — страница раскрытия MKBD от 04.11.2026: заголовок, вкладки и вкладка «Стоимость
  пая и СЧА» (за ноябрь 2026 г. пока одна строка — расчёт за 02.11.2026);
* products_<id>_2025-11.html — страницы MKBD (31747) и SUGB (29150) из веб-архива от 20.11.2025 и 21.11.2025:
  свежую страницу SUGB снять не удалось, а здесь полная таблица за месяц. Вёрстка та же, что сегодня.

Контрольные значения (сверены с таблицей на странице): MKBD — пай 997,80 ₽ и СЧА 202 757 358,36 ₽ на
02.11.2026; 925,56 ₽ и 153 086 567,04 ₽ на 01.11.2025, 928,72 ₽ и 165 544 077,13 ₽ на 18.11.2025 (изменение
пая к пред. дате +1,39 % = 928,72 / 915,96); SUGB — 1 383,31 ₽ и 517 115 375,88 ₽ на 01.11.2025,
1 396,23 ₽ и 514 964 795,84 ₽ на 19.11.2025.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import mkb
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "mkb"
MKBD_URL = "https://www.mkb-am.ru/information/products/31747/"
SUGB_URL = "https://www.mkb-am.ru/information/products/29150/"

# Фонды УК из реестра ЦБ (data/registry.json, uk_inn 7710186602): правила -> (название, тикер, ISIN).
REGISTRY = {
    "4474": ("МКБ – Российские Дивидендные Акции", "MKBD", "RU000A103BX8"),
    "4121": ("РЕГИОН – Индекс МосБиржи государственных облигаций (1-3 года)", "SUGB", "RU000A1020A8"),
}


def read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def make_fund(rules: str, name: str | None = None, *, ftype: str = "exchange", status: str = "Сформирован") -> Fund:
    reg_name, ticker, isin = REGISTRY.get(rules, (name, None, None))
    name = name or reg_name
    return Fund(rules=rules, name=name, full_name=f"Биржевой паевой инвестиционный фонд «{name}»", type=ftype,
                category="Фонд рыночных финансовых инструментов", status=status, reg_date="2021-06-21",
                formation_end=None, uk_name="АО «УК МКБ Инвестиции»", uk_short="АО \"УК МКБ Инвестиции\"",
                uk_ogrn="1027700590301", uk_inn="7710186602", uk_site="https://mkb-am.ru",
                exchange_tradable=ftype == "exchange", isin=isin, ticker=ticker)


def by_date(points):
    return {p.date: p for p in points}


# ---------- главная: номер правил -> id страницы раскрытия ----------

def test_home_maps_rules_to_pages():
    site = mkb.parse_home(read("home.html"))
    assert (site["4474"].page_id, site["4474"].kind) == ("31747", "exchange")
    assert (site["4121"].page_id, site["4121"].kind) == ("29150", "exchange")
    assert site["4474"].name == mkb.name_key(REGISTRY["4474"][0])
    assert site["4121"].name == mkb.name_key(REGISTRY["4121"][0])
    # закрытые фонды тоже в подвале и в форме (в реестре розничных их нет); вложенные «» не мешают
    assert (site["5919"].page_id, site["5919"].kind) == ("43057", "closed")
    assert (site["3146"].page_id, site["6016"].page_id) == ("1476", "43514")
    assert len(site) == 7


def test_name_key():
    assert mkb.name_key("БПИФ рыночных финансовых инструментов «МКБ – Российские Дивидендные Акции»") == \
        mkb.name_key("МКБ – Российские Дивидендные Акции") == "мкб российские дивидендные акции"
    assert mkb.name_key("ЗПИФ рыночных финансовых инструментов «Заблокированные активы паевого инвестиционного "
                        "фонда «МКБ Мультивалютный доход»") == \
        "заблокированные активы паевого инвестиционного фонда мкб мультивалютный доход"
    assert mkb.fund_kind("Биржевой паевой инвестиционный фонд …") == mkb.fund_kind("БПИФ …") == "exchange"
    assert mkb.norm_rules("№ 4474") == "4474"


@pytest.mark.parametrize("mutate", [
    lambda h: h.replace("правила доверительного управления зарегистрированы", "правила зарегистрированы"),
    lambda h: re.sub(r'<input [^>]*name="pif"[^>]*/>', "", h),
    lambda h: h.replace("за №4474", "за №4121"),                       # один номер правил у двух фондов
    lambda h: h.replace('<input id="31747"', '<input id="99999" name="pif" value="БПИФ рыночных финансовых '
                        'инструментов «МКБ – Российские Дивидендные Акции»"/><input id="31747"'),   # два id у фонда
])
def test_home_broken(mutate):
    with pytest.raises(UnknownFormat):
        mkb.parse_home(mutate(read("home.html")))


# ---------- страница раскрытия: таблица «Стоимость пая и СЧА» ----------

def test_fund_page_today():
    page = mkb.parse_fund_page(read("products_31747.html"))
    assert mkb.name_key(page.title) == mkb.name_key(REGISTRY["4474"][0])
    assert (page.month, page.years, page.currency) == ((2026, 11), [2021, 2022, 2023, 2024, 2025, 2026], "RUB")
    assert [(p.date, p.price, p.nav) for p in page.points] == [(date(2026, 11, 2), 997.8, 202757358.36)]


def test_fund_page_mkbd():
    page = mkb.parse_fund_page(read("products_31747_2025-11.html"))
    assert page.title == "БПИФ рыночных финансовых инструментов «МКБ – Российские Дивидендные Акции»"
    assert (page.month, page.years, page.currency) == ((2025, 11), [2021, 2022, 2023, 2024, 2025], "RUB")
    pts = by_date(page.points)
    assert len(pts) == 11 and min(pts) == date(2025, 11, 1) and max(pts) == date(2025, 11, 18)
    assert (pts[date(2025, 11, 1)].price, pts[date(2025, 11, 1)].nav) == (925.56, 153086567.04)
    assert (pts[date(2025, 11, 18)].price, pts[date(2025, 11, 18)].nav) == (928.72, 165544077.13)
    assert date(2025, 11, 3) not in pts and date(2025, 11, 4) not in pts      # праздники — без расчёта
    # колонки не перепутаны: опубликованное «изменение пая к пред. дате» +1,39 % = 928,72 / 915,96
    assert round((928.72 / pts[date(2025, 11, 17)].price - 1) * 100, 2) == 1.39


def test_fund_page_sugb():
    page = mkb.parse_fund_page(read("products_29150_2025-11.html"))
    assert mkb.name_key(page.title) == mkb.name_key(REGISTRY["4121"][0])
    assert page.month == (2025, 11) and page.years[0] == 2020
    pts = by_date(page.points)
    assert len(pts) == 12
    assert (pts[date(2025, 11, 1)].price, pts[date(2025, 11, 1)].nav) == (1383.31, 517115375.88)
    assert (pts[date(2025, 11, 19)].price, pts[date(2025, 11, 19)].nav) == (1396.23, 514964795.84)


@pytest.mark.parametrize("mutate", [
    lambda h: h.replace('class="info-section__content-title h3"', 'class="h3"'),       # нет заголовка
    lambda h: h.replace('id="tab-1"', 'id="tab-9"'),                                    # нет вкладки
    lambda h: h.replace("<span>СЧА</span>", "<span>Активы</span>"),                     # нет колонки СЧА в шапке
    lambda h: h.replace("<span>Стоимость пая</span>", "<span>Стоимость пая, тыс. руб.</span>")
               .replace("<span>СЧА</span>", "<span>СЧА, тыс. руб.</span>"),            # не те единицы
    lambda h: h.replace("18.11.2025                    </div>", "18 ноября                    </div>", 1),
    lambda h: h.replace("928,72", "928,72 ₽ / пай"),                                    # не число
    lambda h: h.replace('<option selected="" value="month">', '<option value="month">'),  # период не выбран
    lambda h: h.replace("18.11.2025                    </div>", "18.09.2025                    </div>", 1),
    lambda h: h.replace("165 544 077,13", "165,54"),                                    # СЧА меньше пая
])
def test_fund_page_broken(mutate):
    html = read("products_31747_2025-11.html")
    broken = mutate(html)
    assert broken != html
    with pytest.raises(UnknownFormat):
        mkb.parse_fund_page(broken)


def test_month_response_checks_period():
    html = read("products_31747_2025-11.html")
    # ответ — вся страница или только вкладка #tab-1: и так, и так берём таблицу
    tab = html[html.index('<div class="info-section__tabs-content active" id="tab-1">'):]
    assert len(mkb.parse_nav_month(html, 2025, 11)) == len(mkb.parse_nav_month(tab, 2025, 11)) == 11
    # сайт проигнорировал параметры и показал текущий месяц — это ошибка, а не «нет данных»
    with pytest.raises(UnknownFormat, match="просили"):
        mkb.parse_nav_month(html, 2025, 10)
    # без фильтра в ответе проверяем даты строк
    no_filter = re.sub(r"<select.*?</select>", "", tab, flags=re.S)
    assert len(mkb.parse_nav_month(no_filter, 2025, 11)) == 11
    with pytest.raises(UnknownFormat, match="других месяцев"):
        mkb.parse_nav_month(no_filter, 2025, 9)


def test_months_between():
    assert mkb.months_between(date(2025, 9, 1), (2025, 11)) == [(2025, 9), (2025, 10)]
    assert mkb.months_between(date(2025, 11, 20), (2026, 2)) == [(2025, 11), (2025, 12), (2026, 1)]
    assert mkb.months_between(date(2025, 11, 4), (2025, 11)) == []


# ---------- сбор целиком (без сети) ----------

class FakeHttp:
    """Вместо сети — файлы фикстур. POST за прошлые месяцы по умолчанию «не отвечает»."""

    def __init__(self, files: dict[tuple[str, str], str] | None = None, errors: dict | None = None):
        self.files = {("GET", mkb.HOME_URL): "home.html", ("GET", MKBD_URL): "products_31747_2025-11.html",
                      ("GET", SUGB_URL): "products_29150_2025-11.html", **(files or {})}
        self.errors = errors or {}
        self.post_files: dict[tuple[str, str], str] = {}     # (год, месяц) -> ответ на POST за этот месяц
        self.calls: list[tuple[str, str, dict | None, dict | None, list | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, method="GET", data=None, headers=None, **kw):
        self.calls.append((method, url, dict(data) if data else None, headers, funds))
        if url in self.errors:
            raise self.errors[url]
        name = self.files.get((method, url))
        if method == "POST" and data and (data["year"], data["month"]) in self.post_files:
            name = self.post_files[(data["year"], data["month"])]
        if name is None or not (FIX / name).exists():
            raise FetchError(url, "сайт не отвечает (ConnectTimeout)")
        path = FIX / name
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-04T12:00:00+00:00", fmt,
                      key=f"{method} {url} {sorted((data or {}).items())}")


def run(http, funds, since=date(2025, 11, 1), last_dates=None):
    ctx = Context(http=http, since=since, log=logging.getLogger("test"), last_dates=last_dates or {})
    return {fd.rules: fd for fd in mkb.MkbAdapter().collect(ctx, funds)}


def test_collect_both_funds_current_month():
    http = FakeHttp()
    res = run(http, [make_fund("4474"), make_fund("4121"), make_fund("9999", "МКБ – Нет такого фонда")])

    mkbd = res["4474"]
    assert (mkbd.status, mkbd.currency, mkbd.page_url) == ("ok", "RUB", MKBD_URL)
    (seg,) = mkbd.segments
    assert seg.page_url == MKBD_URL and seg.raw_url == MKBD_URL
    assert (seg.points[0].date, seg.points[0].price, seg.points[0].nav) == (date(2025, 11, 1), 925.56, 153086567.04)
    assert seg.points[-1].date == date(2025, 11, 18)
    assert mkbd.official_returns == [] and any("доходность" in w for w in mkbd.warnings)

    sugb = res["4121"]
    assert sugb.status == "ok" and sugb.page_url == SUGB_URL and len(sugb.segments[0].points) == 12
    assert not any("отличается от реестра" in w for w in sugb.warnings)

    assert res["9999"].status == "not_found" and "9999" in res["9999"].reason
    assert sum(1 for c in http.calls if c[1] == mkb.HOME_URL) == 1      # главная — один раз
    assert all(c[0] == "GET" for c in http.calls)                         # прошлых месяцев не нужно


def test_collect_past_months_by_post():
    """С 01.09.2025 — сентябрь и октябрь тем же POST, что шлёт скрипт страницы; сайт не ответил."""
    http = FakeHttp()
    fd = run(http, [make_fund("4474")], since=date(2025, 9, 1))["4474"]
    posts = [c for c in http.calls if c[0] == "POST"]
    # первый же месяц не ответил — дальше не идём
    assert [(c[1], c[2]) for c in posts] == [
        (MKBD_URL, {"period": "month", "tabId": "tab-1", "year": "2025", "month": "09"})]
    assert posts[0][3] == {"X-Requested-With": "XMLHttpRequest"} and posts[0][4] == ["4474"]
    assert fd.status == "unavailable"                                     # не отвечает — не «нет истории»


def test_collect_from_today_page():
    """Страница за ноябрь 2026: с 01.11.2025 — POST за каждый прошлый месяц по порядку; ответ за ноябрь 2025
    (настоящий, из архива) принят, декабрь не ответил — фонд «недоступен», а не обрезан."""
    http = FakeHttp({("GET", MKBD_URL): "products_31747.html"})
    http.post_files = {("2025", "11"): "products_31747_2025-11.html"}
    fd = run(http, [make_fund("4474")], since=date(2025, 11, 1))["4474"]
    assert [(c[2]["year"], c[2]["month"]) for c in http.calls if c[0] == "POST"] == [("2025", "11"), ("2025", "12")]
    assert fd.status == "unavailable"

    fd = run(FakeHttp({("GET", MKBD_URL): "products_31747.html"}), [make_fund("4474")], since=date(2026, 11, 1))["4474"]
    assert fd.status == "ok" and [(p.date, p.price) for p in fd.segments[0].points] == [(date(2026, 11, 2), 997.8)]


def test_collect_post_ignored_is_parse_error():
    """Если сайт на POST за октябрь отдаёт текущий месяц — ошибка разбора, а не подмена данных."""
    http = FakeHttp({("POST", MKBD_URL): "products_31747_2025-11.html"})
    fd = run(http, [make_fund("4474")], since=date(2025, 10, 1))["4474"]
    assert fd.status == "parse_error" and "просили" in fd.reason


def test_collect_tail_only():
    """Докачка: последняя точка 14.11 -> запрос с 04.11, это текущий месяц страницы — без POST."""
    http = FakeHttp()
    fd = run(http, [make_fund("4474")], since=date(2025, 9, 1), last_dates={"4474": date(2025, 11, 14)})["4474"]
    assert fd.status == "ok" and all(c[0] == "GET" for c in http.calls)
    assert len(fd.segments[0].points) == 11                               # since режет только начало истории


def test_collect_other_fund_on_page_is_not_found():
    http = FakeHttp({("GET", MKBD_URL): "products_29150_2025-11.html"})
    fd = run(http, [make_fund("4474")])["4474"]
    assert fd.status == "not_found" and "другой фонд" in fd.reason


def test_collect_type_mismatch_is_not_found():
    fd = run(FakeHttp(), [make_fund("4474", ftype="open")])["4474"]
    assert fd.status == "not_found" and "другого типа" in fd.reason


def test_collect_renamed_fund_matches_by_rules():
    fd = run(FakeHttp(), [make_fund("4121", "МКБ – Индекс МосБиржи государственных облигаций (1-3 года)")])["4121"]
    assert fd.status == "ok" and any("отличается от реестра" in w for w in fd.warnings)


def test_collect_home_down():
    http = FakeHttp(errors={mkb.HOME_URL: FetchError(mkb.HOME_URL, "сайт не отвечает (ConnectTimeout)")})
    res = run(http, [make_fund("4474"), make_fund("4121")])
    assert {fd.status for fd in res.values()} == {"unavailable"}
    assert len(http.calls) == 1


def test_collect_fund_page_down():
    http = FakeHttp(errors={SUGB_URL: FetchError(SUGB_URL, "сайт не отвечает (ConnectTimeout)")})
    res = run(http, [make_fund("4474"), make_fund("4121")])
    assert (res["4474"].status, res["4121"].status) == ("ok", "unavailable")
