"""ЗАО «Лидер» (leader-invest.ru): разбор настоящих ответов сайта и сбор без сети.

Фикстуры (tests/fixtures/leader/) — настоящие ответы сайта 03.11.2026, урезанные:
* pif_nav.html — раздел «Паевые инвестиционные фонды» (без шапки/подвала/скриптов);
* fund_akcii.html — страница «Лидер – акции» с последней ежедневной точкой;
* kid_list_*.html — списки КИД, оставлено по 2–4 ссылки (в т. ч. ссылка с опечаткой в дате
  у «облигаций» и ссылка у «смешанных инвестиций», за которой лежит КИД «облигаций»);
* kid_*.docx — КИД, в архиве оставлены только [Content_Types].xml и word/document.xml.
Значения сверены с сайтом: страница фонда (02.11.2026), КИД раздел 5, справка о СЧА
«Лидер – акции» за 30.09.2026 (СЧА 23 834 963,84, пай 30 248,93).
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.leader import (PIF_URL, LeaderAdapter, parse_fund_nav, parse_kid, parse_kid_list,
                                parse_latest)

FIX = Path(__file__).parent / "fixtures" / "leader"
AKCII = f"{PIF_URL}/opif-rfi-lider-akcii"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _docx_replace(name: str, old: str, new: str) -> bytes:
    """Копия фикстуры-КИД с заменой текста в word/document.xml (для «испорченных» тестов)."""
    src = zipfile.ZipFile(io.BytesIO(_read(name)))
    xml = src.read("word/document.xml").decode("utf-8")
    assert old in xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("[Content_Types].xml", src.read("[Content_Types].xml"))
        out.writestr("word/document.xml", xml.replace(old, new))
    return buf.getvalue()


# ---------- меню раздела, страница фонда, список КИД ----------

def test_nav_finds_three_open_funds_with_kid_pages():
    nav = parse_fund_nav(_read("pif_nav.html"))
    assert set(nav) == {"лидер акции", "лидер облигации", "лидер смешанные инвестиции"}
    assert nav["лидер акции"].name == "Лидер – акции"
    assert nav["лидер акции"].page_url == AKCII
    assert nav["лидер акции"].kid_url == f"{AKCII}/klucevoj-informacionnyj-dokument"


def test_nav_without_kid_links_raises():
    html = _text("pif_nav.html").replace("Ключевой информационный документ", "КИД")
    with pytest.raises(UnknownFormat):
        parse_fund_nav(html)


def test_latest_point_from_fund_page():
    p = parse_latest(_read("fund_akcii.html"))
    # https://leader-invest.ru/…/opif-rfi-lider-akcii: «02.11.2026 Пай: 32 372.85 руб. СЧА: 25 508 534.80 руб.»
    assert p.date == date(2026, 11, 2)
    assert p.price == 32372.85
    assert p.nav == 25508534.8


@pytest.mark.parametrize("old, new", [("СЧА:", "xxx"), ("share-date", "xxx"), ("руб.</div>", "USD</div>")])
def test_latest_point_broken_raises(old, new):
    html = _text("fund_akcii.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_latest(html.replace(old, new))


def test_kid_list_uses_publication_date_not_title_with_typo():
    links = parse_kid_list(_read("kid_list_obl.html"))
    # у второй ссылки в названии «по состоянию на 30.10. 2026», опубликована 10.02.2026
    assert [k.published for k in links] == [date(2026, 10, 9), date(2026, 2, 10)]
    assert links[0].url == "https://leader-invest.ru/uploads/2026/10/file_6ac8b977d0c07.docx?2026-10-09T"


def test_kid_list_without_publication_date_raises():
    with pytest.raises(UnknownFormat):
        parse_kid_list(_text("kid_list_akcii.html").replace("Опубликовано:", "Размещено:"))


# ---------- КИД ----------

def test_kid_akcii_values():
    kid = parse_kid(_read("kid_akcii_20260930.docx"))
    assert kid.rules == "0297-74549782"
    assert kid.as_of == date(2026, 9, 30)
    assert kid.price == 30248.93          # = справка о СЧА за 30.09.2026, строка 16
    assert kid.nav == 23834963.84         # = справка о СЧА за 30.09.2026, строка 14
    assert kid.returns == {"1m": -0.56, "3m": -3.19, "6m": -18.69, "1y": -16.03}


def test_kid_with_title_typo_has_correct_date_inside():
    kid = parse_kid(_read("kid_obl_20260130.docx"))
    assert kid.rules == "0296-74549865"
    assert kid.as_of == date(2026, 1, 30)
    assert (kid.price, kid.nav) == (25514.84, 658742407.13)


def test_misplaced_kid_is_detected_by_rules():
    """На странице «Лидер – смешанные инвестиции» за 30.12.2025 лежит КИД «Лидер – облигации»."""
    kid = parse_kid(_read("kid_smes_20251230_misplaced.docx"))
    assert kid.rules == "0296-74549865"


@pytest.mark.parametrize("old, new", [
    ("Доходность инвестиций", "Результат"),
    ("1 год", "12 мес"),
    ("Расчетная стоимость инвестиционного пая", "Цена пая"),
    ("по состоянию ", "на дату "),      # в docx фраза разбита на несколько w:t
])
def test_kid_broken_raises(old, new):
    with pytest.raises(UnknownFormat):
        parse_kid(_docx_replace("kid_akcii_20260930.docx", old, new))


def test_kid_not_docx_raises():
    with pytest.raises(UnknownFormat):
        parse_kid(b"<html>404</html>")


# ---------- сбор целиком без сети ----------

def _fund(rules: str, name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=f"ОПИФ РФИ \"{name}\"", type="open",
                category="Фонд рыночных финансовых инструментов", status="Сформирован",
                reg_date="2004-12-08", formation_end="2005-03-27", uk_name="ЗАО \"Лидер\"",
                uk_short="ЗАО \"Лидер\"", uk_ogrn="1025002040250", uk_inn="5018026672",
                uk_site="https://www.leader-invest.ru", exchange_tradable=False)


ROUTES = {
    "/raskrytie-informacii/paevye-investicionnye-fondy-raskritie": "pif_nav.html",
    "/raskrytie-informacii/paevye-investicionnye-fondy-raskritie/opif-rfi-lider-akcii": "fund_akcii.html",
    "/raskrytie-informacii/paevye-investicionnye-fondy-raskritie/opif-rfi-lider-akcii/"
    "klucevoj-informacionnyj-dokument": "kid_list_akcii.html",
    "/raskrytie-informacii/paevye-investicionnye-fondy-raskritie/opif-rfi-lider-smesannye-investicii/"
    "klucevoj-informacionnyj-dokument": "kid_list_smes.html",
    "/uploads/2026/10/file_6ac8b8988d9db.docx": "kid_akcii_20260930.docx",
    "/uploads/2026/09/file_6aa1097349e53.docx": "kid_akcii_20260831.docx",
    "/uploads/2026/10/file_6ac8b904b8b00.docx": "kid_smes_20260930.docx",
    "/uploads/2026/01/file_696f4f3ba7122.docx": "kid_smes_20251230_misplaced.docx",
}


def _ctx(tmp_path: Path, monkeypatch, since: date) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path not in ROUTES:   # в т. ч. страница «смешанных инвестиций» — проверяем сбой страницы
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        name = ROUTES[path]
        ctype = "text/html" if name.endswith(".html") else "application/octet-stream"
        return httpx.Response(200, content=_read(name), headers={"content-type": ctype})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    return Context(http=http, since=since, log=logging.getLogger("test.leader")), seen


def test_collect_akcii_monthly_kid_plus_latest_point(tmp_path, monkeypatch):
    ctx, seen = _ctx(tmp_path, monkeypatch, since=date(2026, 9, 1))
    (fd,) = LeaderAdapter().collect(ctx, [_fund("0297-74549782", "Лидер - акции")])

    assert fd.status == "ok" and fd.currency == "RUB" and fd.page_url == AKCII
    pts = sorted((p for s in fd.segments for p in s.points), key=lambda p: p.date)
    assert [(p.date, p.nav, p.price) for p in pts] == [
        (date(2026, 9, 30), 23834963.84, 30248.93),     # КИД на 30.09.2026
        (date(2026, 11, 2), 25508534.8, 32372.85),      # страница фонда
    ]
    # КИД на 31.08.2026 скачан (опубликован 09.09.2026), но точка раньше since отрезана
    assert "/uploads/2026/09/file_6aa1097349e53.docx" in seen
    # КИД, опубликованные раньше since, не качаем
    assert "/uploads/2026/08/file_6a7af37eaa7d6.docx" not in seen
    assert {r.period: r.value_pct for r in fd.official_returns} == {"1m": -0.56, "3m": -3.19,
                                                                    "6m": -18.69, "1y": -16.03}
    assert {r.as_of for r in fd.official_returns} == {date(2026, 9, 30)}
    assert fd.official_returns[0].source_url.endswith("file_6ac8b8988d9db.docx?2026-10-09T")
    assert any("месячный" in w for w in fd.warnings)


def test_collect_skips_kid_of_other_fund_and_survives_missing_page(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, since=date(2025, 12, 10))
    funds = [_fund("0298-74549948", "Лидер - смешанные инвестиции"), _fund("0000-00000000", "Лидер - золото")]
    smes, missing = LeaderAdapter().collect(ctx, funds)

    assert smes.status == "ok"
    pts = [p for s in smes.segments for p in s.points]
    assert [(p.date, p.price) for p in pts] == [(date(2026, 9, 30), 29838.33)]   # КИД «облигаций» не взят
    assert any("0296-74549865" in w for w in smes.warnings)
    assert any("ежедневная точка не получена" in w for w in smes.warnings)       # страница фонда 404

    assert missing.status == "not_found" and "Лидер - золото" in missing.reason
