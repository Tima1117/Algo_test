"""ТКБ Инвестмент Партнерс (tkbip): разбор настоящих ответов www.tkbip.ru (снято 03.11.2026), без сети.

Контрольные значения сверены со страницами сайта:
- «Сообщение о СЧА и расчетной стоимости пая по ОПИФ» (/funds/dnm/) на 02.11.2026 и 30.10.2026;
- «Результаты работы паевых фондов» (/funds/) и подвал сайта: прирост пая на 31.10.2026.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from snapshot import assert_series
from urllib.parse import unquote

from pif.sources import tkbip
from pif.sources.tkbip import (BASE, CATALOG_URL, IncomePeriod, TkbipAdapter, assign_record_dates,
                               norm_rules, parse_catalog, parse_disclosure_docs, parse_disclosure_link,
                               parse_history, parse_payouts, parse_record_notice, parse_returns,
                               parse_rules)

FIX = Path(__file__).parent / "fixtures" / "tkbip"
PDF = FIX / "pdf"          # текст настоящих PDF-сообщений (как его отдаёт pypdf), без шаблонных абзацев
BAL_INCOME = "0096-58227323"
DISC_BAL_INCOME = f"{BASE}/disclosure-of-information/reporting-of-mutual-funds/21535000000580922/"

SLUGS = {   # номер правил -> slug страницы фонда (сверено со страницами «Информация о фонде»)
    "0081-58233855": "tkb-bnp-pariba-fond-obligatsiy",
    "0078-58234010": "tkb-bnp-pariba-fond-sbalansirovannyy-konservativnyy",   # «Фонд сбалансированный»
    "0096-58227323": "tkb-bnp-pariba-globalnye-investitsii",   # «…сбалансированный с выплатой дохода»
    "0478-75408434": "tkb-bnp-pariba-premium-fond-aktsiy",
    "0219-14281681": "opif-aktsiy-agana-ekstrim",               # «Перспектива»
    "0991-94131990": "tkb-bnp-pariba-fond-valyutnykh-obligatsiy",
    "2026-94198244": "tkb-bnp-pariba-zoloto",
}


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


# ---------- история: CSV-выгрузка ----------

def test_history_bond_fund():
    pts = parse_history(read("export_tkb-bnp-pariba-fond-obligatsiy.csv"))
    assert len(pts) == 292
    assert [p.date for p in pts] == sorted({p.date for p in pts})     # по возрастанию, без дублей
    first, last = pts[0], pts[-1]
    assert (first.date, first.nav, first.price) == (date(2025, 9, 1), 1102771931.34, 8399.62)
    # /funds/dnm/: Фонд облигаций — пай 9451.67 / 9447.80, СЧА 776913379.36 / 776522868.13
    assert (last.date, last.nav, last.price) == (date(2026, 11, 2), 776913379.36, 9451.67)
    prev = next(p for p in pts if p.date == date(2026, 10, 30))
    assert (prev.nav, prev.price) == (776522868.13, 9447.80)
    assert_series(pts, "tkbip_export_fond-obligatsiy")


def test_history_currency_fund_is_in_rubles():
    pts = parse_history(read("export_tkb-bnp-pariba-fond-valyutnykh-obligatsiy.csv"))
    last = pts[-1]
    # Официальные рублёвые значения (а не справочные USD* 412.62 / 6324655.25)
    assert (last.date, last.nav, last.price) == (date(2026, 11, 2), 509497150.76, 33239.43)
    assert len(pts) == 292
    assert_series(pts, "tkbip_export_fond-valyutnykh-obligatsiy")


def test_history_broken_csv():
    text = read("export_tkb-bnp-pariba-fond-obligatsiy.csv").decode("utf-8-sig")
    # удалена колонка СЧА
    no_nav = "\n".join(";".join(c for i, c in enumerate(line.split(";")) if i != 1)
                       for line in text.splitlines())
    with pytest.raises(UnknownFormat):
        parse_history(no_nav)
    # мусор вместо числа
    with pytest.raises(UnknownFormat):
        parse_history(text.replace('"9451.67"', '"н.д.x"'))
    # вместо CSV пришла HTML-страница
    with pytest.raises(UnknownFormat):
        parse_history(read("funds.html"))
    # одна дата с двумя разными значениями
    lines = text.splitlines()
    with pytest.raises(UnknownFormat):
        parse_history("\n".join(lines + [lines[1].replace("9451.67", "9000.00")]))


# ---------- сопоставление: номер правил ----------

def test_rules_from_information_pages():
    for rules, slug in SLUGS.items():
        assert parse_rules(read(f"info_{slug}.html")) == rules
    # в подвале каждой страницы — правила всех фондов; берётся только блок «Регистрация правил»
    html = read("info_tkb-bnp-pariba-premium-fond-aktsiy.html").decode()
    assert "0081-58233855" in html and "№ 0478 — 75408434" in html


def test_norm_rules():
    assert norm_rules("№ 0478 — 75408434 от 28.02.2006") == "0478-75408434"
    assert norm_rules("№0991-94131990 от 20.09.2007") == "0991-94131990"
    assert norm_rules("без номера") is None


def test_rules_broken_page():
    html = read("info_tkb-bnp-pariba-zoloto.html").decode()
    with pytest.raises(UnknownFormat):
        parse_rules(html.replace("Регистрация правил фонда", "Правила фонда"))
    with pytest.raises(UnknownFormat):
        parse_rules(html.replace("№2026-94198244", "№ скоро"))


def test_catalog_slugs():
    slugs = parse_catalog(read("funds.html"))
    assert sorted(slugs) == sorted(SLUGS.values())      # без служебных /funds/dnm/, /funds/ceif/ …


# ---------- официальная доходность ----------

def test_returns_table():
    rets = parse_returns(read("funds.html"), CATALOG_URL)
    assert set(rets) == set(SLUGS.values())

    def as_dict(slug):
        return {r.period: r.value_pct for r in rets[slug]}

    assert as_dict("tkb-bnp-pariba-fond-obligatsiy") == {"1m": 1.05, "3m": 2.04, "6m": 1.69, "1y": 10.27}
    assert as_dict("tkb-bnp-pariba-globalnye-investitsii") == {"1m": 1.45, "3m": 2.03, "6m": 1.76, "1y": 9.02}
    assert as_dict("tkb-bnp-pariba-fond-sbalansirovannyy-konservativnyy") == {
        "1m": 1.55, "3m": 0.92, "6m": -3.60, "1y": 2.78}
    # валютные фонды: рублёвая строка, а не справочная USD* (у USD 1 м = 2.65 и -3.70)
    assert as_dict("tkb-bnp-pariba-fond-valyutnykh-obligatsiy") == {"1m": -1.91, "3m": 2.41, "6m": 4.12, "1y": 3.55}
    assert as_dict("tkb-bnp-pariba-zoloto")["1m"] == -7.98
    r = rets["tkb-bnp-pariba-premium-fond-aktsiy"][0]
    assert (r.as_of, r.source_url) == (date(2026, 10, 31), CATALOG_URL)


def test_returns_broken_table():
    html = read("funds.html").decode()
    with pytest.raises(UnknownFormat):      # нет колонки «1 м»
        parse_returns(html.replace("1&nbsp;м<", "1&nbsp;мес<").replace("1\xa0м<", "1\xa0мес<"), CATALOG_URL)
    with pytest.raises(UnknownFormat):      # нет даты доходности в шапке
        parse_returns(re.sub(r"(Прирост[^<]*?)\d{2}\.\d{2}\.\d{4}", r"\1", html), CATALOG_URL)
    with pytest.raises(UnknownFormat):      # нет таблиц вовсе
        parse_returns("<html><body><p>Технические работы</p></body></html>", CATALOG_URL)


# ---------- выплаты дохода «Фонда сбалансированного с выплатой дохода» ----------

def pdf(name: str) -> str:
    return (PDF / f"{name}.txt").read_text("utf-8")


def as_rows(periods):
    return [(p.start, p.end, p.amount, p.record_date) for p in periods]


def test_payouts_message_list_without_dates():
    # вид A (до 01.2026): доход на пай по полумесячным периодам, дат определения дохода нет
    ps = parse_payouts(pdf("25-10-08_Сообщение о выплате дохода_ОПИФ Сбал ВД"), BAL_INCOME)
    assert as_rows(ps) == [
        (date(2025, 7, 1), date(2025, 7, 15), 2.539803665, None),
        (date(2025, 7, 16), date(2025, 7, 31), 2.8457059342, None),
        (date(2025, 8, 1), date(2025, 8, 15), 2.8155017265, None),
        (date(2025, 8, 16), date(2025, 8, 31), 1.8516068257, None),
        (date(2025, 9, 1), date(2025, 9, 15), 1.605096252, None),
        (date(2025, 9, 16), date(2025, 9, 30), 3.487054483, None),
    ]


def test_payouts_message_list_with_dates():
    # вид B (05.2026): то же + блок «Даты, на которые … выплачивается доход»
    ps = parse_payouts(pdf("26-05-12_Сообщение о выплате дохода_ОПИФ Сбал ВД"), BAL_INCOME)
    assert len(ps) == 6
    assert as_rows(ps)[0] == (date(2026, 1, 1), date(2026, 1, 15), 0.941345043, date(2026, 1, 22))
    assert as_rows(ps)[-1] == (date(2026, 3, 16), date(2026, 3, 31), 2.3774939358, date(2026, 4, 7))


def test_payouts_message_table():
    # вид C (с 08.2026): таблица «период | всего, руб. | на пай, руб. | дата»
    ps = parse_payouts(pdf("26-08-03_Сообщение о выплате дохода_ОПИФ Сбал ВД"), BAL_INCOME)
    assert [(p.record_date, p.amount) for p in ps] == [
        (date(2026, 4, 22), 1.0889134505), (date(2026, 5, 8), 3.8069161113),
        (date(2026, 5, 22), 1.1854816499), (date(2026, 6, 5), 1.4428734241),
        (date(2026, 6, 22), 1.3150906795), (date(2026, 7, 7), 4.8417853976)]


def test_payouts_broken_message():
    a = pdf("25-10-08_Сообщение о выплате дохода_ОПИФ Сбал ВД")
    c = pdf("26-08-03_Сообщение о выплате дохода_ОПИФ Сбал ВД")
    with pytest.raises(UnknownFormat):      # нет блока «на один пай»
        parse_payouts(a.replace("по одному инвестиционному паю", "на все паи"), BAL_INCOME)
    with pytest.raises(UnknownFormat):      # блоки «сумма дохода» и «на пай» расходятся по периодам
        parse_payouts(a.replace("с 16 по 30 сентября 2025 г.: 4 493 006.3", "с 16 по 30 сентября 2025 г.: н/д"),
                      BAL_INCOME)
    with pytest.raises(UnknownFormat):      # строка таблицы не разобралась (сумма на пай пропала)
        parse_payouts(c.replace("3.8069161113 ", ""), BAL_INCOME)
    with pytest.raises(UnknownFormat):      # сообщение другого фонда
        parse_payouts(a, "0078-58234010")
    with pytest.raises(UnknownFormat):      # вообще не то сообщение
        parse_payouts(pdf("25-10-22_Сообщение о дате составления списка лиц ОПИФ Сбал с ВД"), BAL_INCOME)


def test_record_notices():
    assert parse_record_notice(pdf("25-10-22_Сообщение о дате составления списка лиц ОПИФ Сбал с ВД"),
                               BAL_INCOME) == (date(2025, 10, 22), None)
    # имя файла «25-10-7…», в тексте — 7 октября 2025 г.
    assert parse_record_notice(pdf("25-10-7_Сообщение о дате составления списка лиц ОПИФ Сбал с ВД"),
                               BAL_INCOME) == (date(2025, 10, 7), None)
    with pytest.raises(UnknownFormat):
        parse_record_notice(pdf("25-10-22_Сообщение о дате составления списка лиц ОПИФ Сбал с ВД")
                            .replace("по состоянию на", "на"), BAL_INCOME)


def test_assign_record_dates_from_notices():
    periods = parse_payouts(pdf("26-01-19_Сообщение о выплате дохода_ОПИФ Сбал ВД"), BAL_INCOME)
    notices = [parse_record_notice(f.read_text("utf-8"), BAL_INCOME)
               for f in PDF.glob("*списка лиц*.txt")]
    got = {(p.start, p.end): p.record_date for p in assign_record_dates(periods, notices)}
    assert got[(date(2025, 10, 16), date(2025, 10, 31))] == date(2025, 11, 10)   # 4 ноября — выходной
    # 16–31 декабря -> 16 января (январские праздники), а не ближайшее 22 января
    assert got[(date(2025, 12, 16), date(2025, 12, 31))] == date(2026, 1, 16)
    assert sorted(got.values()) == [date(2025, 10, 22), date(2025, 11, 10), date(2025, 11, 21),
                                    date(2025, 12, 5), date(2025, 12, 22), date(2026, 1, 16)]
    # даты у периода нет нигде -> не угадываем
    lonely = [IncomePeriod(date(2024, 1, 1), date(2024, 1, 15), 1.0, None)]
    with pytest.raises(UnknownFormat):
        assign_record_dates(lonely, notices)


def test_disclosure_page():
    assert parse_disclosure_link(read("info_tkb-bnp-pariba-globalnye-investitsii.html")) == DISC_BAL_INCOME
    docs = parse_disclosure_docs(read("disc_21535000000580922.html"), BAL_INCOME)
    pays = [d for d in docs if d.title.startswith("Сообщение о выплате дохода")]
    assert [d.published for d in pays] == [date(2026, 8, 3), date(2026, 5, 12), date(2026, 1, 19),
                                           date(2025, 10, 8), date(2025, 7, 8)]
    assert pays[0].url.startswith(BASE + "/Lawyers/attachments/") and " " not in pays[0].url
    assert unquote(pays[0].url).endswith("/26-08-03_Сообщение о выплате дохода_ОПИФ Сбал ВД.pdf")
    with pytest.raises(UnknownFormat):      # страница раскрытия не того фонда
        parse_disclosure_docs(read("disc_21535000000580922.html"), "0078-58234010")


# ---------- адаптер целиком на фикстурах ----------

class FakeHttp:
    """Отдаёт фикстуры вместо сети; можно «уронить» отдельные адреса."""

    def __init__(self, fail: tuple[str, ...] = (), catalog: Path | None = None):
        self.fail = fail
        self.catalog = catalog
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, *, params=None, fmt="html", funds=None, **kw):
        self.calls.append((url, params))
        if any(f in url for f in self.fail):
            raise FetchError(url, "сайт не отвечает (ReadTimeout)")
        if url == CATALOG_URL:
            path = self.catalog or FIX / "funds.html"
        elif m := re.fullmatch(rf"{BASE}/funds/([a-z0-9-]+)/information/", url):
            path = FIX / f"info_{m.group(1)}.html"
        elif m := re.fullmatch(rf"{BASE}/funds/([a-z0-9-]+)/export/", url):
            # конец диапазона фиксированный, а не «сегодня»: адрес выгрузки не меняется по дням
            assert params == {"dateFrom": "01.09.2025", "dateTo": "31.12.2099", "format": "csv"}
            path = FIX / f"export_{m.group(1)}.csv"
        elif m := re.fullmatch(rf"{BASE}/disclosure-of-information/reporting-of-mutual-funds/(\d+)/", url):
            path = FIX / f"disc_{m.group(1)}.html"
        elif url.endswith(".pdf"):
            assert fmt == "pdf"
            path = PDF / (unquote(url.rsplit("/", 1)[1])[:-4] + ".txt")
        else:
            raise AssertionError(f"неожиданный адрес {url}")
        assert path.exists(), f"нет фикстуры для {unquote(url)}"
        return RawDoc(url, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


@pytest.fixture(autouse=True)
def fake_pypdf(monkeypatch):
    # в фикстурах вместо PDF — уже извлечённый текст: pypdf в тестах не нужен
    monkeypatch.setattr(tkbip, "pdf_text", lambda content: content.decode("utf-8"))
    monkeypatch.setattr(tkbip, "_has_pypdf", lambda: True)


def fund(rules: str) -> Fund:
    return Fund(rules=rules, name="", full_name="", type="open", category="", status="Сформирован",
                reg_date="", formation_end=None, uk_name="", uk_short="ТКБ", uk_ogrn="",
                uk_inn="7825489723", uk_site=BASE, exchange_tradable=False)


def run(http, rules):
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.tkbip"))
    return {fd.rules: fd for fd in TkbipAdapter().collect(ctx, [fund(r) for r in rules])}


def test_adapter_matches_by_rules():
    http = FakeHttp()
    res = run(http, list(SLUGS) + ["9999-00000000"])
    for rules, slug in SLUGS.items():
        fd = res[rules]
        assert fd.status == "ok" and not fd.errors, (rules, fd.reason, fd.errors)
        assert fd.currency == "RUB"
        assert fd.page_url == f"{BASE}/funds/{slug}/results/"
        assert fd.segments[0].points[-1].date == date(2026, 11, 2)
        assert {r.period for r in fd.official_returns} == {"1m", "3m", "6m", "1y"}
    # два «сбалансированных» не перепутаны
    assert res["0078-58234010"].segments[0].points[-1].price == 8845.15
    assert res["0096-58227323"].segments[0].points[-1].price == 357.97
    assert {r.period: r.value_pct for r in res["0096-58227323"].official_returns}["1y"] == 9.02
    assert res["9999-00000000"].status == "not_found"
    # каждый адрес — один раз: каталог, «Информация о фонде», выгрузки, раскрытие, PDF о доходе
    urls = [u for u, _ in http.calls]
    assert urls.count(CATALOG_URL) == 1 and len(urls) == len(set(urls))


def test_adapter_payouts():
    res = run(FakeHttp(), list(SLUGS))
    pays = res[BAL_INCOME].payouts
    # с 01.09.2025: даты определения дохода 05.09.2025 … 07.07.2026, по одной на отчётный период
    assert len(pays) == 21
    assert (pays[0].date, pays[0].amount) == (date(2025, 9, 5), 1.8516068257)     # период 16–31.08.2025
    assert (pays[-1].date, pays[-1].amount) == (date(2026, 7, 7), 4.8417853976)   # период 16–30.06.2026
    by = {p.date: p for p in pays}
    assert by[date(2026, 1, 16)].amount == 3.2063621519                           # период 16–31.12.2025
    assert unquote(by[date(2026, 1, 16)].source_url).endswith("26-01-19_Сообщение о выплате дохода_ОПИФ Сбал ВД.pdf")
    # за периоды с июля 2026 размер дохода ещё не опубликован — это видно в предупреждении
    assert any("после 07.07.2026" in w and "с тех пор: 8" in w for w in res[BAL_INCOME].warnings)
    # остальные фонды доход не выплачивают: ни выплат, ни предупреждений
    for rules in SLUGS:
        if rules != BAL_INCOME:
            assert res[rules].payouts == [] and res[rules].warnings == []


def test_adapter_payouts_without_pypdf(monkeypatch):
    monkeypatch.setattr(tkbip, "_has_pypdf", lambda: False)
    http = FakeHttp()
    res = run(http, [BAL_INCOME])
    assert res[BAL_INCOME].status == "ok" and res[BAL_INCOME].payouts == []
    assert any("pypdf" in w for w in res[BAL_INCOME].warnings)
    assert not any(u.endswith(".pdf") for u, _ in http.calls)        # PDF зря не качаем


def test_adapter_broken_payout_message(tmp_path, monkeypatch):
    # сообщение о выплате сменило вид: ряд и доходность есть, выплат нет, поломка — в errors
    broken = tmp_path / "pdf"
    broken.mkdir()
    for f in PDF.glob("*.txt"):
        t = f.read_text("utf-8")
        if f.name.startswith("26-08-03"):
            t = t.replace("3.8069161113 ", "")          # строка таблицы без суммы на пай
        (broken / f.name).write_text(t, "utf-8")
    monkeypatch.setattr(sys.modules[__name__], "PDF", broken)
    res = run(FakeHttp(), [BAL_INCOME])
    fd = res[BAL_INCOME]
    assert fd.status == "ok" and fd.segments[0].points and fd.official_returns
    assert fd.payouts == [] and fd.errors and "выплаты дохода" in fd.errors[0]


def test_adapter_info_page_down_is_not_not_found():
    # страница «Информация о фонде» Золота не ответила: Золото — unavailable, а не «не найден»
    res = run(FakeHttp(fail=("tkb-bnp-pariba-zoloto/information",)), list(SLUGS))
    assert res["2026-94198244"].status == "unavailable"
    assert all(fd.status == "ok" for r, fd in res.items() if r != "2026-94198244")


def test_adapter_catalog_down():
    res = run(FakeHttp(fail=(CATALOG_URL,)), list(SLUGS))
    assert {fd.status for fd in res.values()} == {"unavailable"}


def test_adapter_disclosure_down_keeps_history():
    res = run(FakeHttp(fail=("reporting-of-mutual-funds/",)), [BAL_INCOME])
    fd = res[BAL_INCOME]
    assert fd.status == "ok" and fd.segments[0].points and fd.payouts == []
    assert any("выплаты дохода не собраны" in w for w in fd.warnings)


def test_adapter_broken_returns_keeps_history(tmp_path):
    # таблица доходности сменила формат: ряд собирается, а поломка видна в errors, а не молча
    broken = tmp_path / "funds.html"
    html = read("funds.html").decode()
    broken.write_text(html.replace("1&nbsp;год<", "12&nbsp;мес<").replace("1\xa0год<", "12\xa0мес<"), "utf-8")
    res = run(FakeHttp(catalog=broken), list(SLUGS))
    for fd in res.values():
        assert fd.status == "ok" and fd.segments[0].points
        assert fd.official_returns == []
        assert fd.errors and "доходность" in fd.errors[0]
