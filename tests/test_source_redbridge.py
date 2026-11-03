"""ООО «УК «КРАСНЫЙ МОСТ» (redbridge-am.com): разбор настоящих ответов сайта и сбор без сети.

Фикстуры (tests/fixtures/redbridge/) — настоящие ответы сайта 03.11.2026:
* foundations.html — /foundations/: блок плиток со ссылками на фонды и ссылки шапки «Описание фонда»,
  «Документы» … на страницу первого фонда (их разборщик брать не должен);
* fund_sek.html, fund_merk.html — /foundations/<код>/: вкладки «Описание фонда» (ссылка на архив CSV,
  таблица последних значений, «Динамика стоимости пая на 31.10.2026», закомментированные таблицы
  2017 года) и «Условия инвестирования» (номер правил); без скриптов, картинок и остальных вкладок;
* sek.csv, merk.csv — «Архив значений стоимости инвестиционного пая и СЧА» (cp1251): заголовок, все
  строки с 01.08.2025, конец марта 2025, февраль–март 2022 (строка «28.02.2022;;»), три первые строки
  истории. В merk.csv — настоящая опечатка УК «26.03.2025» между 27.03.2026 и 25.03.2026.
Значения сверены с таблицами на https://www.redbridge-am.com/foundations/opif-km-sekyuritiz/ и
/foundations/opif-km-merkuri/; доходность за 1/3/6/12 мес совпадает с расчётом по архиву до сотых.
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
from pif.sources.redbridge import RedBridgeAdapter, parse_csv, parse_fund_list, parse_fund_page, parse_returns

FIX = Path(__file__).parent / "fixtures" / "redbridge"
SEK_PAGE = "https://www.redbridge-am.com/foundations/opif-km-sekyuritiz/"
MERK_PAGE = "https://www.redbridge-am.com/foundations/opif-km-merkuri/"
SEK_CSV = "https://www.redbridge-am.com/upload/iblock/f06/ro9y5qqr615h9ivy5z16k7n4w8w352mn.csv"
MERK_CSV = "https://www.redbridge-am.com/upload/iblock/95f/mf1lzv6455j0qgufm4da7ssx0lox8kc5.csv"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _csv_text(name: str) -> str:
    return _read(name).decode("cp1251")


# ---------- список фондов ----------

def test_fund_list_maps_names_to_pages():
    funds = parse_fund_list(_read("foundations.html"))
    assert funds == {
        "ОПИФ «КМ Секьюритиз»": SEK_PAGE,
        "ЗПИФ «КМ Оверсиз»": "https://www.redbridge-am.com/foundations/zpif-km-oversiz/",
        "ОПИФ «КМ Меркури»": MERK_PAGE,
        "ЗПИФ «Георгиевский»": "https://www.redbridge-am.com/foundations/zpif-georgievskiy/",
    }                                   # ссылки шапки «Описание фонда», «Документы» — не фонды


def test_fund_list_without_plates_raises():
    with pytest.raises(UnknownFormat):
        parse_fund_list(_text("foundations.html").replace('class="plates"', 'class="cards"'))


# ---------- страница фонда ----------

def test_fund_page_securities():
    info = parse_fund_page(_read("fund_sek.html"), SEK_PAGE)
    assert info["rules"] == "0976-58229693"
    assert info["csv_url"] == SEK_CSV
    # таблица «Стоимость пая и чистых активов на 30.10.2026 и 02.11.2026»
    assert [(p.date, p.nav, p.price) for p in info["latest"]] == [
        (date(2026, 10, 30), 1753615.99, 4513.08), (date(2026, 11, 2), 1747796.45, 4498.1)]
    # «Динамика стоимости пая на 31.10.2026»; закомментированная таблица 2017 г. не попала
    assert {r.period: r.value_pct for r in info["returns"]} == {"1m": 2.06, "3m": 1.51, "6m": -1.03, "1y": 6.18}
    assert {r.as_of for r in info["returns"]} == {date(2026, 10, 31)}
    assert {r.source_url for r in info["returns"]} == {SEK_PAGE}


def test_fund_page_mercury_with_index_columns():
    info = parse_fund_page(_read("fund_merk.html"), MERK_PAGE)
    assert info["rules"] == "0668-94120070"
    assert info["csv_url"] == MERK_CSV
    # справа в таблице — отклонение от индекса за 20/250 дней; в точки не попадает
    assert [(p.date, p.nav, p.price) for p in info["latest"]] == [
        (date(2026, 10, 30), 12555346.59, 2694.75), (date(2026, 11, 2), 12308917.08, 2641.86)]
    assert {r.period: r.value_pct for r in info["returns"]} == {"1m": 11.0, "3m": 10.46, "6m": -3.65, "1y": 3.37}


def test_returns_block_absent_is_empty():
    html = _text("fund_sek.html").replace("Динамика стоимости пая на 31.10.2026", "Структура портфеля")
    assert parse_returns(html, SEK_PAGE) == []


@pytest.mark.parametrize("old,new", [
    ("Правила доверительного управления </span>", "Регистрация </span>"),          # нет строки с правилами
    ("за № 0976-58229693", "за № б/н"),                                             # номер правил не тот
    ("Архив значений стоимости", "Архив"),                                         # нет ссылки на архив
    ("ro9y5qqr615h9ivy5z16k7n4w8w352mn.csv", "ro9y5qqr615h9ivy5z16k7n4w8w352mn.xlsx"),   # архив не CSV
    ("Стоимость пая и чистых активов на", "Стоимость на"),                          # нет таблицы последних
    ("Стоимость чистых активов<!--", "СЧА<!--"),                                    # строка СЧА пропала
    ("<strong>\n                                                1 год", "<strong>\n"),    # заголовок «1 год» пропал
])
def test_fund_page_broken_raises(old, new):
    html = _text("fund_sek.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_fund_page(html.replace(old, new), SEK_PAGE)


# ---------- архив CSV ----------

def test_csv_securities_values_in_rubles():
    notes: list[str] = []
    pts = {p.date: p for p in parse_csv(_read("sek.csv"), notes)}
    assert notes == []
    # как в таблице на странице фонда: 02.11.2026 — 4 498,10 руб. и 1 747 796,45 руб.
    assert (pts[date(2026, 11, 2)].nav, pts[date(2026, 11, 2)].price) == (1747796.45, 4498.1)
    assert (pts[date(2026, 10, 30)].nav, pts[date(2026, 10, 30)].price) == (1753615.99, 4513.08)
    assert (pts[date(2026, 9, 30)].nav, pts[date(2026, 9, 30)].price) == (1718302.82, 4422.2)
    assert (pts[date(2025, 9, 1)].nav, pts[date(2025, 9, 1)].price) == (1694648.92, 4341.14)
    assert date(2025, 11, 1) in pts                 # рабочая суббота 01.11.2025 — настоящая публикация
    assert date(2022, 2, 28) not in pts             # «28.02.2022;;» — значений нет, точки нет
    first = min(pts.values(), key=lambda p: p.date)
    assert (first.date, first.nav, first.price) == (date(2007, 12, 24), 10000000.0, 5000.0)


def test_csv_mercury_year_typo_is_fixed_with_note():
    notes: list[str] = []
    pts = {p.date: p for p in parse_csv(_read("merk.csv"), notes)}
    assert pts[date(2026, 3, 26)].price == 2909.88 and pts[date(2026, 3, 26)].nav == 13557704.66
    assert pts[date(2025, 3, 26)].price == 3206.13            # настоящая строка 26.03.2025 не затёрта
    assert notes == ["архив УК, строка 156: дата 26.03.2025 стоит между 27.03.2026 и 25.03.2026 — "
                     "опечатка в годе, принята как 26.03.2026"]
    assert pts[date(2026, 11, 2)].price == 2641.86 and pts[date(2026, 11, 2)].nav == 12308917.08


def test_csv_row_out_of_order_without_year_fix_is_dropped():
    text = _csv_text("merk.csv").replace("26.03.2025;13 557 704,66", "29.03.2025;13 557 704,66")
    notes: list[str] = []
    pts = {p.date for p in parse_csv(text.encode("cp1251"), notes)}
    assert date(2026, 3, 26) not in pts and date(2026, 3, 29) not in pts
    assert len(notes) == 1 and "пропущена" in notes[0]


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("Дата;Стоимость чистых активов;Стоимость 1 пая", "Дата;Стоимость 1 пая"),      # нет колонки
    lambda t: t.replace("Стоимость чистых активов;Стоимость 1 пая", "Стоимость 1 пая;Стоимость чистых активов"),
    lambda t: t.replace("02.11.2026;", "2026-11-02;"),                                              # дата не та
    lambda t: t.replace("30.10.2026;1 753 615,99;4 513,08", "30.10.2026;1 753 615,99"),             # колонка пропала
    lambda t: t.replace("02.11.2026;", "02.11.2025;"),                                              # первая строка старше второй
    lambda t: t.replace("4 513,08", "4 513,08 руб/пай"),                                            # не число
])
def test_csv_broken_raises(mutate):
    with pytest.raises(UnknownFormat):
        parse_csv(mutate(_csv_text("sek.csv")).encode("cp1251"))


# ---------- сбор целиком (без сети) ----------

ROUTES = {
    "/foundations/": ("foundations.html", "text/html; charset=UTF-8"),
    "/foundations/opif-km-sekyuritiz/": ("fund_sek.html", "text/html; charset=UTF-8"),
    "/foundations/opif-km-merkuri/": ("fund_merk.html", "text/html; charset=UTF-8"),
    "/upload/iblock/f06/ro9y5qqr615h9ivy5z16k7n4w8w352mn.csv": ("sek.csv", "application/octet-stream"),
    "/upload/iblock/95f/mf1lzv6455j0qgufm4da7ssx0lox8kc5.csv": ("merk.csv", "application/octet-stream"),
}


def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type="open", category="", status="Сформирован",
                reg_date="2006-11-23", formation_end=None, uk_name="", uk_short="УК КРАСНЫЙ МОСТ", uk_ogrn="",
                uk_inn="7838059090", uk_site="https://redbridge-am.com", exchange_tradable=False)


def _ctx(tmp_path, monkeypatch, overrides: dict[str, bytes] | None = None, seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        assert request.url.host == "www.redbridge-am.com"
        if seen is not None:
            seen.append(path)
        if path not in ROUTES:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        name, ctype = ROUTES[path]
        body = (overrides or {}).get(name, None) or _read(name)
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.redbridge"))


def test_collect_end_to_end(tmp_path, monkeypatch):
    seen: list[str] = []
    ctx = _ctx(tmp_path, monkeypatch, seen=seen)
    merk, sek, wrong_rules, missing = RedBridgeAdapter().collect(ctx, [
        _fund("0668-94120070", "КМ Меркури"),
        _fund("0976-58229693", "КМ Секьюритиз"),
        _fund("1234-56789012", "КМ Секьюритиз"),     # название совпало, правила — нет
        _fund("9999-99999999", "КМ Нет такого"),
    ])
    assert sek.status == "ok" and sek.currency == "RUB" and sek.page_url == SEK_PAGE
    (seg,) = sek.segments                                    # архив свежий — таблица страницы не нужна
    assert seg.raw_url == SEK_CSV and seg.page_url == SEK_PAGE
    assert seg.points[0].date == date(2025, 9, 1)             # всё до since отрезано
    assert (seg.points[-1].date, seg.points[-1].price) == (date(2026, 11, 2), 4498.1)
    assert {r.period: r.value_pct for r in sek.official_returns} == {"1m": 2.06, "3m": 1.51, "6m": -1.03, "1y": 6.18}
    assert sek.warnings == [] and sek.payouts == []

    assert merk.status == "ok" and merk.segments[0].raw_url == MERK_CSV
    assert any("26.03.2026" in w for w in merk.warnings)       # исправленная опечатка УК видна
    assert date(2026, 3, 26) in {p.date for p in merk.segments[0].points}

    assert wrong_rules.status == "not_found" and "0976-58229693" in wrong_rules.reason
    assert missing.status == "not_found" and "КМ Нет такого" in missing.reason
    assert seen.count("/foundations/") == 1                    # список фондов — из кэша для всех фондов


def test_collect_archive_lagging_takes_tail_from_page(tmp_path, monkeypatch):
    # архив без последней строки 02.11.2026 — эту дату берём из таблицы на странице фонда
    lag = _csv_text("sek.csv").replace("02.11.2026;1 747 796,45;4 498,10\r\n", "").encode("cp1251")
    ctx = _ctx(tmp_path, monkeypatch, overrides={"sek.csv": lag})
    (sek,) = RedBridgeAdapter().collect(ctx, [_fund("0976-58229693", "КМ Секьюритиз")])
    assert sek.status == "ok"
    csv_seg, page_seg = sek.segments
    assert csv_seg.points[-1].date == date(2026, 10, 30)
    assert page_seg.raw_url == SEK_PAGE
    assert [(p.date, p.nav, p.price) for p in page_seg.points] == [(date(2026, 11, 2), 1747796.45, 4498.1)]
    assert any("отстаёт" in w for w in sek.warnings)


def test_collect_broken_archive_is_parse_error(tmp_path, monkeypatch):
    bad = _csv_text("sek.csv").replace("Стоимость 1 пая", "Пай").encode("cp1251")
    ctx = _ctx(tmp_path, monkeypatch, overrides={"sek.csv": bad})
    (sek,) = RedBridgeAdapter().collect(ctx, [_fund("0976-58229693", "КМ Секьюритиз")])
    assert sek.status == "parse_error" and "заголовок" in sek.reason
