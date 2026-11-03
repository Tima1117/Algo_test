"""УК «Финстар Капитал»: разбор настоящих страниц finstar-capital.ru (урезанные копии от 2026-11-03;
массивы графиков оставлены с 25.09.2026, у «Рантье» — с 25.06.2026) и текста КИД, извлечённого pypdf
из PDF сайта.

Значения сверены со страницами фондов (таблица «Стоимость пая и СЧА» и подсказки графика) и с КИД:
«Облигации роста - Рантье» (№ 7276) на 02.11.2026 — пай 977.42 RUR, СЧА 51 174 202.85 RUR;
    на 30.09.2026 — пай 1 011,12 руб., СЧА 52 691 664,04 руб. (так же в КИД на 30.09.2026);
    КИД на 30.09.2026: 1 месяц 0,69 %, 3 месяца 2,72 %, 6 месяцев 5,81 %, год — пусто;
    выплаты дохода (отметки на графике и «Сообщения о выплате дохода»): 42,44 руб./пай за II кв.
    (пай 30.06 — 1 025,66, 01.07 — 983,65) и 41,93 руб./пай за III кв. (30.09 — 1 011,12, 01.10 — 970,19);
«ФИНСТАР - Денежный рынок ПЛЮС» (№ 6396) на 02.11.2026 — пай 1 448.67 RUR, СЧА 46 716 135.59 RUR;
    КИД на 30.09.2026: 1,14 / 3,55 / 7,24 / 15,92 %;
«Купонный доход - ВДО» (№ 7976): ряд с 23.10.2026 (пай 999.82, СЧА 9 999 178.28), в КИД на 30.09.2026
    доходность пустая.
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
from pif.sources import finstar
from pif.sources.base import Context
from pif.sources.finstar import (LIST_URL, FinstarAdapter, parse_fund_list, parse_kid_links, parse_kid_returns,
                                 parse_payout_marks, parse_rules, parse_series, parse_table, payout_ex_dates)

FIX = Path(__file__).parent / "fixtures" / "finstar"
BASE = "https://finstar-capital.ru"
RANTE = f"{BASE}/funds/opif-rynochnykh-finansovykh-instrumentov-obligatsii-rosta-rante/"
VDO = f"{BASE}/funds/kuponnyy-dokhod-vdo/"
BPIF = f"{BASE}/funds/bpif-rynochnykh-finansovykh-instrumentov-finstar-denezhnyy-rynok-plyus/"
KID_RANTE = f"{BASE}/upload/iblock/2b0/56fdlrpgs9e3wcjw2o53jdprqaixqgqy.pdf"
KID_BPIF = f"{BASE}/upload/iblock/8a2/67ta2a2ioko6ijnhxn938rdf5kfj3ypt.pdf"
KID_VDO = f"{BASE}/upload/iblock/5da/c2839cxx3au890u3qqgqykuvswx36aqw.pdf"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


# ---------- список и страница фонда ----------

def test_fund_list_cards():
    funds = parse_fund_list(_read("list.html"))
    assert len(funds) == 20
    urls = {f.name: f.url for f in funds}
    assert urls["ОПИФ рыночных финансовых инструментов «Облигации роста - Рантье»"] == RANTE
    assert urls["ОПИФ рыночных финансовых инструментов «Купонный доход - ВДО»"] == VDO
    assert urls["БПИФ рыночных финансовых инструментов «ФИНСТАР - Денежный рынок ПЛЮС»"] == BPIF


def test_fund_list_without_cards_is_unknown_format():
    with pytest.raises(UnknownFormat):
        parse_fund_list(_read("list.html").replace('class="title"', 'class="name"'))


def test_rules_both_spellings():
    assert parse_rules(_read("fund_7276.html")) == "7276"      # «08.09.2025 № 7276»
    assert parse_rules(_read("fund_7976.html")) == "7976"      # «№ 7976 от 20.07.2026 г.»
    assert parse_rules(_read("fund_6396.html")) == "6396"


def test_rules_row_missing_is_unknown_format():
    html = _read("fund_7276.html").replace("Дата регистрации и", "Дата чего-то")
    with pytest.raises(UnknownFormat):
        parse_rules(html)


def test_series_rante_values():
    points = parse_series(_read("fund_7276.html"))
    assert [p.date for p in points] == sorted(p.date for p in points)
    by = {p.date: p for p in points}
    assert (by[date(2026, 11, 2)].price, by[date(2026, 11, 2)].nav) == (977.42, 51174202.85)
    assert (by[date(2026, 9, 30)].price, by[date(2026, 9, 30)].nav) == (1011.12, 52691664.04)
    # точка выплаты дохода (с подписью на графике) — обычная точка ряда
    assert by[date(2026, 10, 1)].price == 970.19


def test_series_bpif_and_young_fund():
    bpif = parse_series(_read("fund_6396.html"))
    assert (bpif[-1].date, bpif[-1].price, bpif[-1].nav) == (date(2026, 11, 2), 1448.67, 46716135.59)
    vdo = parse_series(_read("fund_7976.html"))
    assert len(vdo) == 7
    assert (vdo[0].date, vdo[0].price, vdo[0].nav) == (date(2026, 10, 23), 999.82, 9999178.28)


@pytest.mark.parametrize("old,new", [
    ("let chart2Data", "let chartNavData"),          # пропал массив СЧА
    ('"y":977.42', '"value":977.42'),                # точка без y
    ('"y":977.42', '"y":"977,42 руб/пай x"'),        # мусор вместо числа
])
def test_broken_chart_is_unknown_format(old, new):
    html = _read("fund_7276.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_series(html.replace(old, new))


def test_table_currency_and_last_rows():
    cur, rows = parse_table(_read("fund_7276.html"))
    assert cur == "RUB"                               # на сайте «RUR»
    assert [(r.date, r.price, r.nav) for r in rows] == [
        (date(2026, 10, 30), 976.58, 51127108.82), (date(2026, 11, 2), 977.42, 51174202.85)]


def test_table_unknown_currency_or_columns_is_unknown_format():
    html = _read("fund_7276.html")
    with pytest.raises(UnknownFormat):
        parse_table(html.replace("Стоимость пая, RUR", "Стоимость пая, XYZ"))
    with pytest.raises(UnknownFormat):
        parse_table(html.replace("СЧА, RUR", "Активы"))


# ---------- выплаты дохода ----------

def test_payout_marks_and_ex_dates():
    html = _read("fund_7276.html")
    marks = parse_payout_marks(html)
    assert marks == [(date(2026, 6, 30), 42.44), (date(2026, 10, 1), 41.93)]
    # 30.06 — последний день «с доходом» (пай упал 01.07), 01.10 — уже первый день «без»
    assert payout_ex_dates(parse_series(html), marks) == [(date(2026, 7, 1), 42.44), (date(2026, 10, 1), 41.93)]


def test_no_payouts_at_bpif():
    assert parse_payout_marks(_read("fund_6396.html")) == []


def test_unknown_chart_mark_is_unknown_format():
    html = _read("fund_7276.html").replace("Ежеквартальная выплата инвестиционного дохода в размере 41,93 руб./ пай",
                                           "Дробление паёв 1:10")
    with pytest.raises(UnknownFormat):
        parse_payout_marks(html)


def test_payout_without_price_drop_is_unknown_format():
    html = _read("fund_7276.html")
    with pytest.raises(UnknownFormat):
        payout_ex_dates(parse_series(html), [(date(2026, 8, 31), 42.44)])


# ---------- КИД ----------

def test_kid_links_latest_first_and_corrected_wins():
    kids = parse_kid_links(_read("fund_6396.html"))
    assert kids[0].url == KID_BPIF and kids[0].as_of == date(2026, 9, 30)
    march = [k for k in kids if k.as_of == date(2026, 3, 31)]
    assert len(march) == 2 and march[0].corrected and not march[1].corrected


def test_kid_returns_rante_without_year():
    rets = parse_kid_returns(_read("kid_7276.txt"), KID_RANTE)
    assert {r.period: r.value_pct for r in rets} == {"1m": 0.69, "3m": 2.72, "6m": 5.81}
    assert {r.as_of for r in rets} == {date(2026, 9, 30)}
    assert {r.source_url for r in rets} == {KID_RANTE}


def test_kid_returns_bpif_full_year():
    rets = parse_kid_returns(_read("kid_6396.txt"), KID_BPIF)
    assert {r.period: r.value_pct for r in rets} == {"1m": 1.14, "3m": 3.55, "6m": 7.24, "1y": 15.92}


def test_kid_returns_empty_table_for_young_fund():
    assert parse_kid_returns(_read("kid_7976.txt"), KID_VDO) == []


def test_kid_layout_from_pdftotext_also_parses():
    # тот же раздел 5 в раскладке pdftotext -layout (колонки выровнены пробелами)
    text = ("Ключевой информационный документ по состоянию на 30.09.2026.\n"
            "Раздел 5. Основные результаты инвестирования\n\n"
            "Период                    Доходность                Отклонение доходности от\n"
            "1 месяц                   1,14                      1,22                    -0,02\n"
            "3 месяца                  3,55                      3,17                    2,39\n"
            "6 месяцев                 7,24                      5,67                    6,08\n"
            "Год                       15,92                     10,03                   14,76\n"
            "3 года\n5 лет\nРаздел 6. Комиссии\n")
    assert [r.value_pct for r in parse_kid_returns(text, KID_BPIF)] == [1.14, 3.55, 7.24, 15.92]


@pytest.mark.parametrize("old,new", [
    ("Раздел 5", "Раздел 9"),                            # нет раздела с результатами
    ("6 месяцев", "Полгода"),                            # пропала строка периода
    ("по состоянию на", "на дату"),                      # нет даты КИД
])
def test_broken_kid_is_unknown_format(old, new):
    text = _read("kid_6396.txt")
    assert old in text
    with pytest.raises(UnknownFormat):
        parse_kid_returns(text.replace(old, new), KID_BPIF)


# ---------- collect целиком: настоящий Fetcher + MockTransport ----------

PAGES = {LIST_URL: "list.html", RANTE: "fund_7276.html", VDO: "fund_7976.html", BPIF: "fund_6396.html",
         KID_RANTE: "kid_7276.txt", KID_BPIF: "kid_6396.txt", KID_VDO: "kid_7976.txt"}


def _fund(rules: str, name: str, type_: str = "open", status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ПИФ «{name}»", type=type_, category="",
                status=status, reg_date="2025-09-08", formation_end=None,
                uk_name="ООО УК «Финстар Капитал»", uk_short="ООО УК «Финстар Капитал»",
                uk_ogrn="1237700591919", uk_inn="9703154610", uk_site="http://finstar-capital.ru",
                exchange_tradable=type_ == "exchange")


def _ctx(tmp_path: Path, monkeypatch, override: dict[str, str] | None = None) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url not in PAGES:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        body = (override or {}).get(url) or _read(PAGES[url])
        ctype = "application/pdf" if url.endswith(".pdf") else "text/html; charset=UTF-8"
        return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr(http, "_wait_turn", lambda domain: None)
    # PDF в фикстурах не храним (крупные): «КИД» отдаётся уже извлечённым текстом
    monkeypatch.setattr(finstar, "PdfReader", object())
    monkeypatch.setattr(finstar, "kid_text", lambda pdf: pdf.decode("utf-8"))
    return Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.finstar")), seen


FUNDS = [_fund("7976", "Купонный доход - ВДО"), _fund("7276", "Облигации роста - Рантье"),
         _fund("6396", "ФИНСТАР - Денежный рынок ПЛЮС", type_="exchange")]


def test_collect_all_three(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    by = {fd.rules: fd for fd in FinstarAdapter().collect(ctx, FUNDS)}

    fd = by["7276"]
    assert fd.status == "ok", fd.reason
    assert fd.currency == "RUB" and fd.page_url == RANTE
    assert len(fd.segments) == 1 and fd.segments[0].raw_url == RANTE
    assert fd.segments[0].points[-1].nav == 51174202.85
    assert {r.period: r.value_pct for r in fd.official_returns} == {"1m": 0.69, "3m": 2.72, "6m": 5.81}
    assert [(p.date, p.amount, p.source_url) for p in fd.payouts] == [
        (date(2026, 7, 1), 42.44, RANTE), (date(2026, 10, 1), 41.93, RANTE)]
    assert fd.warnings == []                  # таблица на странице совпала с графиком

    fd = by["6396"]
    assert fd.status == "ok" and len(fd.official_returns) == 4 and fd.payouts == []

    fd = by["7976"]
    assert fd.status == "ok"                  # молодой фонд: ряд «с даты начала» — не partial
    assert len(fd.segments[0].points) == 7
    assert fd.official_returns == [] and "не заполнена" in fd.warnings[0]
    # каждый КИД скачан один раз, лишних страниц не открывали
    assert sorted(u for u in seen if u.endswith(".pdf")) == sorted([KID_RANTE, KID_BPIF, KID_VDO])


def test_collect_without_pypdf_keeps_series(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(finstar, "PdfReader", None)
    [fd] = FinstarAdapter().collect(ctx, [FUNDS[1]])
    assert fd.status == "ok" and fd.segments
    assert fd.official_returns == []
    assert "pypdf не установлен" in fd.warnings[0] and KID_RANTE in fd.warnings[0]
    assert not any(u.endswith(".pdf") for u in seen)


def test_collect_broken_kid_is_warning_not_error(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(finstar, "kid_text", lambda pdf: "совсем не КИД")
    [fd] = FinstarAdapter().collect(ctx, [FUNDS[1]])
    assert fd.status == "ok" and fd.segments and fd.errors == []
    assert "не разобрана" in fd.warnings[0]


def test_collect_kid_404_is_warning_not_error(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    monkeypatch.delitem(PAGES, KID_RANTE)
    [fd] = FinstarAdapter().collect(ctx, [FUNDS[1]])
    assert fd.status == "ok" and fd.segments and fd.errors == []
    assert "не скачан" in fd.warnings[0]


def test_collect_rules_mismatch_is_not_found(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    [fd] = FinstarAdapter().collect(ctx, [_fund("9999", "ФИНСТАР - Денежный рынок ПЛЮС", type_="exchange")])
    assert fd.status == "not_found"
    assert "№ 6396" in fd.reason


def test_collect_table_chart_mismatch_is_warning(tmp_path, monkeypatch):
    broken = _read("fund_7276.html").replace('"y":977.42', '"y":977.43')
    ctx, _ = _ctx(tmp_path, monkeypatch, override={RANTE: broken})
    [fd] = FinstarAdapter().collect(ctx, [FUNDS[1]])
    assert fd.status == "ok"
    assert any("02.11.2026" in w and "не совпала с графиком" in w for w in fd.warnings)
