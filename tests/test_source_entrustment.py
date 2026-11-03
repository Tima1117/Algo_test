"""АО УК «Доверительная» (entrustment.ru): разбор настоящих ответов сайта, без сети.

Фикстуры — урезанные ответы от 03.11.2026: каталог (таблица «Динамика фондов» + дисклеймеры с номерами
правил), меню раздела раскрытия, страница раскрытия «Сберегательного» (3194), фрагменты графика его
страницы (пай и СЧА, массив урезан до точек с 25.08.2025) и долларовый фрагмент «Валютных накоплений».
Значения сверены со страницей фонда («Данные на 30.10.2026: 135,37 ₽, СЧА 737 972 405,73 ₽»)
и страницей раскрытия («Стоимость пая и СЧА на 02.11.2026»).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.entrustment import (
    CATALOG_URL, DISCLOSURE_INDEX_URL, EntrustmentAdapter, _msk_day, parse_catalog,
    parse_disclosure_index, parse_history, parse_latest, parse_returns, pick_window,
)

FIX = Path(__file__).parent / "fixtures" / "entrustment"
DISC_3194 = "https://entrustment.ru/disclosure_old/opif-obligatsiy-sistema-kapital-mobilnyy/"


def read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------------------------------------------------------------- история

def test_history_price():
    pts = {p.date: p for p in parse_history(read("chart_3194_price.html"), "price")}
    assert pts[date(2025, 9, 1)].price == 118.86
    assert pts[date(2025, 10, 30)].price == 120.03
    assert pts[date(2026, 10, 30)].price == 135.37     # «Данные на 30.10.2026» на странице фонда
    assert pts[date(2026, 11, 2)].price == 135.51      # «Стоимость пая и СЧА на 02.11.2026» в раскрытии
    assert all(p.nav is None for p in pts.values())
    assert min(pts) == date(2025, 8, 25) and max(pts) == date(2026, 11, 2)
    assert date(2025, 8, 30) not in pts                # выходные не публикуются


def test_history_nav():
    pts = {p.date: p for p in parse_history(read("chart_3194_nav.html"), "nav")}
    assert pts[date(2025, 9, 1)].nav == 637946414.17
    assert pts[date(2026, 10, 30)].nav == 737972405.73
    assert pts[date(2026, 11, 2)].nav == 738739153.03
    assert all(p.price is None for p in pts.values())


def test_epoch_is_moscow_midnight():
    # 1761771600000 = 2025-10-29T21:00Z = 30.10.2025 00:00 МСК; через UTC было бы 29.10
    assert _msk_day(1761771600000) == date(2025, 10, 30)
    with pytest.raises(UnknownFormat):
        _msk_day(1761771600000 + 3600_000)             # не полночь — дата неоднозначна


def test_history_rejects_wrong_kind_and_currency():
    with pytest.raises(UnknownFormat, match="data-type"):
        parse_history(read("chart_3194_price.html"), "nav")
    # «Валютные накопления» с valute=usd: долларовый эквивалент, не официальный рублёвый пай
    with pytest.raises(UnknownFormat, match="data-currency"):
        parse_history(read("chart_3193_price_usd.html"), "price")


@pytest.mark.parametrize("old,new", [
    ('id="chart-data"', 'id="chart-data-x"'),                  # нет блока данных
    ("data-array-price=", "data-array-prices="),                # нет поля с массивом
    ("[[1756069200000,", "[[1756072800000,"),                   # метка не в полночь МСК
    ("[[1756069200000,", "[[1756069200000,1,"),                 # точка из трёх элементов
])
def test_history_broken(old, new):
    text = read("chart_3194_price.html")
    assert old in text
    with pytest.raises(UnknownFormat):
        parse_history(text.replace(old, new), "price")


def test_history_null_is_none_not_zero():
    text = read("chart_3194_price.html")
    assert "[[1756069200000,118.62]" in text
    pts = parse_history(text.replace("[[1756069200000,118.62]", "[[1756069200000,null]"), "price")
    assert pts[0].date == date(2025, 8, 25) and pts[0].price is None


# ---------------------------------------------------------------- доходность и раскрытие

def test_returns():
    rets = {r.period: r for r in parse_returns(read("disclosure_3194.html"), DISC_3194)}
    assert {k: r.value_pct for k, r in rets.items()} == {"1m": 1.53, "3m": 1.71, "6m": 3.22, "1y": 12.68}
    assert all(r.as_of == date(2026, 10, 31) and r.source_url == DISC_3194 for r in rets.values())


def test_returns_broken_column():
    text = read("disclosure_3194.html").replace("<th>за 1<br/>год</th>", "<th>за 12<br/>мес.</th>")
    with pytest.raises(UnknownFormat, match="за 1 год"):
        parse_returns(text, DISC_3194)


def test_returns_block_absent():
    # пока УК выкладывает данные дня, блока прироста РСП на странице может не быть — это не сбой формата
    text = read("disclosure_3194.html").replace("Расчет прироста РСП %", "Что-то другое")
    assert parse_returns(text, DISC_3194) is None
    # а страница без обоих известных блоков — незнакомый формат
    with pytest.raises(UnknownFormat):
        parse_returns(text.replace("Стоимость пая и СЧА на", "Стоимость на"), DISC_3194)


def test_latest():
    assert parse_latest(read("disclosure_3194.html")) == (date(2026, 11, 2), 135.51, 738739153.03)
    with pytest.raises(UnknownFormat):
        parse_latest(read("disclosure_3194.html").replace("Стоимость пая (руб.)", "Стоимость пая ($)"))


# ---------------------------------------------------------------- каталог и сопоставление

REGISTRY = {   # номер правил -> название в реестре ЦБ
    "3193": "Доверительная - фонд Валютные накопления",
    "4692": "Доверительная - фонд Неоновый",
    "2204-94177868": "Доверительная - фонд Резервный",
    "2671": "Доверительная - фонд Резервный. Валютный",
    "2744": "Доверительная - фонд Российские акции",
    "3194": "Доверительная - фонд Сберегательный",
    "6268": "Ликвидный",
}
SLUGS = {
    "3193": "sistema-investitsii", "4692": "globalnye-dokhodnye-obligatsii",
    "2204-94177868": "sistema-investitsii-5", "2671": "sistema-investitsii-4",
    "2744": "sistema-investitsii-3", "3194": "sistema-investitsii-2", "6268": "likvidnyy",
}


def make_fund(rules: str, name: str | None = None) -> Fund:
    return Fund(rules=rules, name=name or REGISTRY[rules], full_name="", type="open", category="",
                status="Сформирован", reg_date="2016-08-12", formation_end="2016-11-21",
                uk_name="Акционерное общество УК «Доверительная»", uk_short="АО УК «Доверительная»",
                uk_ogrn="1257700129158", uk_inn="9725182361", uk_site="https://entrustment.ru/",
                exchange_tradable=False)


def test_catalog_matches_registry():
    catalog = parse_catalog(read("catalog.html"))
    for rules, slug in SLUGS.items():
        site = EntrustmentAdapter.match(make_fund(rules), catalog)
        assert site is not None and site.slug == slug, rules
    # «Резервный» и «Резервный. Валютный» — разные фонды
    assert catalog["2204-94177868"].short_name == "Резервный"
    assert catalog["2671"].short_name == "Резервный. Валютный"
    # закрытые фонды есть в дисклеймерах, но страницы в каталоге у них нет
    assert catalog["3723"].slug is None
    # номера нет — сопоставление по точному названию, без натяжек
    by_name = EntrustmentAdapter.match(make_fund("0000", "Доверительная - фонд Резервный"), catalog)
    assert by_name.rules == "2204-94177868"
    assert EntrustmentAdapter.match(make_fund("0000", "Доверительная - фонд Валютный"), catalog) is None


def test_catalog_broken():
    text = read("catalog.html")
    with pytest.raises(UnknownFormat, match="таблицы"):
        parse_catalog(text.replace("js-mutual-funds-table", "js-other-table"))
    with pytest.raises(UnknownFormat, match="дисклеймер"):
        parse_catalog(text.replace("в настоящем материале", "далее"))


def test_disclosure_index():
    index = parse_disclosure_index(read("disclosure_index.html"))
    assert index["доверительная сберегательный"] == DISC_3194
    assert index["доверительная резервный"].endswith("/opif-obligatsiy-rezervnyy/")
    assert index["доверительная резервный валютный"].endswith("/opif-obligatsiy-rezervnyy-valyutnye-investitsii/")
    assert index["доверительная российские акции"].endswith("/opif-aktsiy-sistema-investitsii-fond-aktsiy/")
    assert index["ликвидный"].endswith("/bpif-rynochnykh-finansovykh-instrumentov-likvidnyy/")


def test_pick_window():
    assert pick_window(date(2025, 9, 1), date(2026, 11, 3)) == "3year"
    assert pick_window(date(2026, 1, 1), date(2026, 11, 3)) == "year"
    assert pick_window(date(2020, 1, 1), date(2026, 11, 3)) == "all"


# ---------------------------------------------------------------- адаптер целиком, на фикстурах

class FakeHttp:
    """Вместо сети — фикстуры по адресу; неизвестный адрес = сайт недоступен."""

    def __init__(self, tmp: Path, broken: set[str] = frozenset()):
        self.tmp, self.broken, self.calls = tmp, broken, []

    def get(self, url, *, params=None, fmt="html", funds=None, headers=None, **kw):
        self.calls.append((url, params, funds))
        name = {CATALOG_URL: "catalog.html", DISCLOSURE_INDEX_URL: "disclosure_index.html",
                DISC_3194: "disclosure_3194.html"}.get(url)
        if url == "https://entrustment.ru/catalog/sistema-investitsii-2/":
            assert params["valute"] == "rub" and headers == {"X-Requested-With": "XMLHttpRequest"}
            name = {"index_inv": "chart_3194_price.html", "scha": "chart_3194_nav.html"}[params["type"]]
        if name is None or url in self.broken:
            raise FetchError(url, "сайт недоступен (тест)")
        data = (FIX / name).read_bytes()
        path = self.tmp / name
        path.write_bytes(data)
        return RawDoc(url, path, hashlib.sha256(data).hexdigest(), "2026-11-03T15:00:00+00:00", fmt)


def run(tmp_path, funds, broken=frozenset()):
    http = FakeHttp(tmp_path, set(broken))
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))
    return {fd.rules: fd for fd in EntrustmentAdapter().collect(ctx, funds)}, http


def test_collect_offline(tmp_path):
    out, http = run(tmp_path, [make_fund("3194"), make_fund("9999", "Доверительная - фонд Несуществующий")])
    fd = out["3194"]
    assert fd.status == "ok", fd.reason
    assert fd.currency == "RUB" and fd.page_url == "https://entrustment.ru/catalog/sistema-investitsii-2/"
    price, nav = fd.segments
    assert price.points[0].date == date(2025, 9, 1)     # точки до начала истории отрезаны
    assert {p.date: p.price for p in price.points}[date(2026, 10, 30)] == 135.37
    assert {p.date: p.nav for p in nav.points}[date(2026, 11, 2)] == 738739153.03
    assert [r.period for r in fd.official_returns] == ["1m", "3m", "6m", "1y"]
    assert not fd.errors and not fd.warnings            # последняя точка сошлась с раскрытием
    assert out["9999"].status == "not_found"
    # каталог скачан один раз и в манифесте отнесён ко всем фондам УК
    catalog_calls = [c for c in http.calls if c[0] == CATALOG_URL]
    assert len(catalog_calls) == 1 and catalog_calls[0][2] == ["3194", "9999"]


def test_collect_disclosure_down_keeps_series(tmp_path):
    out, _ = run(tmp_path, [make_fund("3194")], broken={DISC_3194})
    fd = out["3194"]
    assert fd.status == "ok" and len(fd.segments) == 2 and not fd.official_returns
    assert any("раскрытие" in w for w in fd.warnings)


def test_collect_catalog_down(tmp_path):
    out, http = run(tmp_path, [make_fund("3194"), make_fund("3193")], broken={CATALOG_URL})
    assert {fd.status for fd in out.values()} == {"unavailable"}
    assert sum(1 for c in http.calls if c[0] == CATALOG_URL) == 1   # сбой запомнен, не долбим сайт
