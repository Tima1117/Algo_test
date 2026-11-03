"""Адаптер АО УК «Ингосстрах - Инвестиции»: разбор настоящих ответов сайта (без сети).

Фикстуры сняты с www.ingosinvest-am.ru 03.11.2026:
- catalog.json — /api/content/open-mutual-funds (длинные html-поля урезаны);
- quotes_639383.json — /api/Info/FundQuotesDynamic/639383 («Мой сейф») с 2025-09-01, целиком;
- quotes_48141850.json — хвост ряда «Облигации в юанях»;
- returns.json — POST /api/Info/FundProfitabilityRegular по 7 фондам;
- pif.html, bpif.html — витрины фондов (оставлены блоки Next.js с карточками и футером).
Контрольные значения на 02.11.2026 и 30.10.2026 сверены с таблицей «Раскрытие информации → О ПИФах»
(/open-info/required/about-pifs), доходность — с блоком «Правовая информация» на страницах сайта.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date
from pathlib import Path

import pytest

from pif.http import FetchError, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import ingosstrakh as ing
from pif.sources.base import Context

FX = Path(__file__).parent / "fixtures" / "ingosstrakh"

REGISTRY = {  # номер правил -> (тип, название в реестре ЦБ, fundId на сайте)
    "4742": ("exchange", "Ингосстрах Инфляционный", 39270561),
    "4483": ("exchange", "Ингосстрах – корпоративные облигации", 36081295),
    "2622": ("open", "Ингосстрах-валютные облигации", 7596108),
    "1149-94139555": ("open", "Мои акции", 541872),
    "1148-94139715": ("open", "Мой сейф", 639383),
    "0050-54904637": ("open", "Моя пенсия", 17587),
    "5248": ("exchange", "Облигации в юанях", 48141850),
}


def load(name: str):
    return json.loads((FX / name).read_text("utf-8"))


def fund(rules: str, kind: str = "open", name: str = "") -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type=kind, category="", status="Сформирован",
                reg_date="2000-01-01", formation_end=None, uk_name="", uk_short="Ингосстрах",
                uk_ogrn="", uk_inn="7705136973", uk_site=ing.BASE, exchange_tradable=kind == "exchange")


# ---------- каталог и сопоставление ----------

@pytest.mark.parametrize("raw, rules", [
    ("№ 0050-54904637 от 15.11.2000 г.", "0050-54904637"),
    ("1148-94139715 от 27.12.2007 г.\n", "1148-94139715"),
    ("№ 4742 от 02.12.2021\n", "4742"),
    ("4742", "4742"),
])
def test_norm_rules(raw, rules):
    assert ing.norm_rules(raw) == rules


def test_norm_rules_garbage():
    with pytest.raises(UnknownFormat):
        ing.norm_rules("правила зарегистрированы")


def test_catalog_matches_all_registry_funds_by_rules():
    catalog = ing.parse_catalog((FX / "catalog.json").read_bytes())
    assert len(catalog) == 14          # вместе с закрытыми и прекращёнными фондами
    for rules, (_, name, fund_id) in REGISTRY.items():
        site = catalog[ing.norm_rules(rules)]
        assert site.fund_id == fund_id
    assert catalog["1148-94139715"].title == "Мой сейф"
    assert catalog["2622"].type == "open-mutual-pif"
    assert catalog["5248"].type == "exchange-traded-pif"


def test_catalog_broken():
    data = load("catalog.json")
    del data["data"][0]["attributes"]["numberAndDateOfRuleRegistration"]
    with pytest.raises(UnknownFormat):
        ing.parse_catalog(data)
    data = load("catalog.json")
    data["meta"]["pagination"]["total"] = 30          # отдан не весь каталог
    with pytest.raises(UnknownFormat):
        ing.parse_catalog(data)


# ---------- ряд СЧА и пая ----------

def test_history_my_safe():
    pts = ing.parse_history((FX / "quotes_639383.json").read_bytes(), 639383)
    by = {p.date: p for p in pts}
    assert len(pts) == 292 and len(by) == 292
    assert pts[0].date == date(2025, 9, 1) and pts[-1].date == date(2026, 11, 2)
    assert [p.date for p in pts] == sorted(by)
    # как в «Раскрытие информации → О ПИФах»: РСП и СЧА в рублях
    assert by[date(2026, 11, 2)].price == 16123.56
    assert by[date(2026, 11, 2)].nav == 13199178718.57
    assert by[date(2026, 10, 30)].price == 16106.20
    assert by[date(2026, 10, 30)].nav == 13184963815.17
    assert by[date(2025, 9, 1)].price == 13843.93
    assert by[date(2025, 9, 1)].nav == 962613645.48
    assert all(p.nav and p.price for p in pts)


def test_history_yuan_in_rubles():
    pts = ing.parse_history((FX / "quotes_48141850.json").read_bytes(), 48141850)
    by = {p.date: p for p in pts}
    assert by[date(2026, 11, 2)].price == 129.07           # «РСП, руб.»; на бирже YUAN тоже в рублях
    assert by[date(2026, 11, 2)].nav == 507732048.83
    assert by[date(2026, 10, 30)].price == 127.15
    assert by[date(2026, 10, 30)].nav == 500184496.60


def test_history_missing_value_is_none():
    rows = load("quotes_48141850.json")
    rows[0]["assetsValue"] = None
    rows[1]["unitValue"] = 0
    pts = ing.parse_history(rows, 48141850)
    assert pts[0].nav is None and pts[0].price is not None
    assert pts[1].price is None


@pytest.mark.parametrize("breaker", [
    lambda rows: rows[3].pop("unitValue"),
    lambda rows: rows[3].pop("assetsValue"),
    lambda rows: rows[3].pop("date"),
    lambda rows: rows[3].__setitem__("date", "вчера"),
    lambda rows: rows[3].__setitem__("unitValue", "n/a"),
    lambda rows: rows[3].__setitem__("fundId", 1),                 # точка чужого фонда
    lambda rows: rows.append(dict(rows[0])),                        # дубль даты
])
def test_history_broken(breaker):
    rows = load("quotes_48141850.json")
    breaker(rows)
    with pytest.raises(UnknownFormat):
        ing.parse_history(rows, 48141850)


def test_history_not_a_list():
    with pytest.raises(UnknownFormat):
        ing.parse_history(b'{"error": "x"}')
    with pytest.raises(UnknownFormat):
        ing.parse_history(b"<html>")


# ---------- официальная доходность ----------

def test_returns_my_safe():
    rets = ing.parse_returns((FX / "returns.json").read_bytes(), 639383, ing.RETURNS_URL)
    got = {r.period: round(r.value_pct, 2) for r in rets}
    assert got == {"1m": 1.21, "3m": 3.11, "6m": 5.88, "1y": 13.78}
    assert {r.as_of for r in rets} == {date(2026, 10, 31)}
    assert {r.source_url for r in rets} == {ing.RETURNS_URL}


def test_returns_all_funds_and_null_5y():
    data = load("returns.json")
    for rules, (_, _, fund_id) in REGISTRY.items():
        rets = ing.parse_returns(data, fund_id, ing.RETURNS_URL)
        assert {r.period for r in rets} == {"1m", "3m", "6m", "1y"}, rules
    # у «Облигаций в юанях» нет 5 лет (null) — это не мешает 1/3/6/12 мес.
    yuan = {r.period: round(r.value_pct, 2) for r in ing.parse_returns(data, 48141850, ing.RETURNS_URL)}
    assert yuan == {"1m": -0.90, "3m": 3.59, "6m": 10.43, "1y": 11.41}


def _footer_returns(html: str) -> dict[str, dict[str, float]]:
    """Числа из блока «Правовая информация» (так их видит человек на сайте)."""
    # внутри данных Next.js переводы строк футера экранированы ещё раз ("\\n")
    text = re.sub(r"<[^>]+>", " ", ing._flight_text(html).replace("\\n", " "))
    out = {}
    for m in re.finditer(r"«([^»]+)»:\s*1 мес\.\s*(-?[\d,]+)%;\s*3 мес\.\s*(-?[\d,]+)%;\s*"
                         r"6 мес\.\s*(-?[\d,]+)%;\s*1 год\s*(-?[\d,]+)%", text):
        out[m.group(1)] = {p: float(v.replace(",", ".")) for p, v in zip(("1m", "3m", "6m", "1y"), m.groups()[1:])}
    assert "Доходность рассчитана на 31.10.2026" in text
    return out


def test_returns_equal_published_footer():
    footer = _footer_returns((FX / "pif.html").read_text("utf-8"))
    assert len(footer) == 6       # «Ингосстрах-валютные облигации» в футере не приводится
    names = {"Мой сейф": 639383, "Моя пенсия": 17587, "Мои акции": 541872,
             "Ингосстрах - Корпоративные облигации": 36081295, "Ингосстрах Инфляционный": 39270561,
             "Облигации в юанях": 48141850}
    data = load("returns.json")
    for name, fund_id in names.items():
        api = {r.period: round(r.value_pct, 2) for r in ing.parse_returns(data, fund_id, ing.RETURNS_URL)}
        assert api == footer[name], name


def test_returns_consistent_with_series():
    """Официальные 1m/1y «Мой сейф» на 31.10.2026 сходятся с ценой пая из ряда (последняя дата ≤ 31.10)."""
    by = {p.date: p.price for p in ing.parse_history((FX / "quotes_639383.json").read_bytes())}
    rets = {r.period: r.value_pct for r in ing.parse_returns(load("returns.json"), 639383, ing.RETURNS_URL)}
    assert abs((by[date(2026, 10, 30)] / by[date(2025, 10, 31)] - 1) * 100 - rets["1y"]) < 0.01
    assert abs((by[date(2026, 10, 30)] / by[date(2026, 9, 30)] - 1) * 100 - rets["1m"]) < 0.01


@pytest.mark.parametrize("breaker", [
    lambda d: d["639383"][0].pop("profitability"),
    lambda d: d["639383"][1].pop("startDate"),
    lambda d: d["639383"][0].__setitem__("periodId", 2),       # periodId не совпадает с датами
    lambda d: d.pop("639383"),
    lambda d: d.__setitem__("639383", {"1m": 1.2}),
])
def test_returns_broken(breaker):
    data = load("returns.json")
    breaker(data)
    with pytest.raises(UnknownFormat):
        ing.parse_returns(data, 639383, ing.RETURNS_URL)


# ---------- витрина: страницы фондов ----------

def test_cards():
    assert ing.parse_cards((FX / "pif.html").read_text("utf-8")) == {
        639383: "/pif/pif5", 541872: "/pif/pif6", 17587: "/pif/pif1"}
    assert ing.parse_cards((FX / "bpif.html").read_text("utf-8")) == {
        36081295: "/bpif/bpif3", 39270561: "/bpif/bpif1", 48141850: "/bpif/bpif4"}


def test_cards_broken():
    with pytest.raises(UnknownFormat):
        ing.parse_cards("<html><body>Технические работы</body></html>")
    html = (FX / "pif.html").read_text("utf-8").replace("detailLink", "detail")
    with pytest.raises(UnknownFormat):
        ing.parse_cards(html)


# ---------- collect() целиком на фикстурах ----------

class FakeHttp:
    """Отдаёт фикстуры вместо сайта; для фондов без фикстуры ряда — ошибка сети."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.calls: list[tuple] = []

    def get(self, url, *, params=None, fmt="html", funds=None, method="GET", json_body=None, **kw):
        self.calls.append((method, url, params, json_body, tuple(funds or ())))
        if url == ing.CATALOG_URL:
            name = "catalog.json"
        elif url in ing.LISTING_PAGES:
            name = url.rsplit("/", 1)[1] + ".html"
        elif url == ing.RETURNS_URL:
            name = "returns.json"
        else:
            name = "quotes_" + url.rsplit("/", 1)[1] + ".json"
        src = FX / name
        if not src.exists():
            raise FetchError(url, "HTTP 500", 500)
        body = src.read_bytes()
        path = self.tmp / name
        path.write_bytes(body)
        return RawDoc(url, path, hashlib.sha256(body).hexdigest(), "2026-11-03T12:00:00+00:00", fmt)


def test_collect_on_fixtures(tmp_path):
    http = FakeHttp(tmp_path)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))
    funds = [fund(r, kind, name) for r, (kind, name, _) in REGISTRY.items()] + [fund("9999", "open", "Нет такого")]
    res = {fd.rules: fd for fd in ing.IngosstrakhAdapter().collect(ctx, funds)}

    safe = res["1148-94139715"]
    assert safe.status == "ok" and safe.currency == "RUB" and not safe.errors
    assert safe.page_url == "https://www.ingosinvest-am.ru/pif/pif5"
    assert len(safe.segments) == 1 and len(safe.segments[0].points) == 292
    assert safe.segments[0].page_url == "https://www.ingosinvest-am.ru/pif/pif5"
    assert {r.period for r in safe.official_returns} == {"1m", "3m", "6m", "1y"}

    yuan = res["5248"]
    assert yuan.status == "ok" and yuan.page_url.endswith("/bpif/bpif4") and yuan.currency == "RUB"

    assert res["9999"].status == "not_found"
    # ряд остальных фондов в фикстурах не сохранён — сбой сети одного фонда не роняет других
    assert res["2622"].status == "unavailable"
    assert {res[r].status for r in ("4742", "4483", "1149-94139555", "0050-54904637")} == {"unavailable"}

    # каталог и витрины — по одному запросу на все фонды; ряд — с начала истории по сегодня
    urls = [c[1] for c in http.calls]
    assert urls.count(ing.CATALOG_URL) == 1 and urls.count(ing.LISTING_PAGES[0]) == 1
    q = next(c for c in http.calls if c[1].endswith("/639383"))
    assert q[2]["startDate"] == "2025-09-01" and q[2]["endDate"] == date.today().isoformat()
    assert q[4] == ("1148-94139715",)
    r = next(c for c in http.calls if c[1] == ing.RETURNS_URL and c[3] == [639383])
    assert r[0] == "POST"


def test_collect_fund_without_page(tmp_path, monkeypatch):
    """Фонд без карточки на витрине (2622) — ссылка на раздел раскрытия и предупреждение."""
    http = FakeHttp(tmp_path)
    real_get = http.get

    def get(url, **kw):
        if url.endswith("/7596108"):          # ряд 2622 подменяем рядом «Мой сейф» — проверяем только ссылки
            url = url.replace("7596108", "639383")
            raw = real_get(url, **kw)
            rows = [dict(r, fundId=7596108) for r in json.loads(raw.content())]
            raw.path.write_text(json.dumps(rows))
            return raw
        return real_get(url, **kw)

    monkeypatch.setattr(http, "get", get)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test"))
    (fd,) = ing.IngosstrakhAdapter().collect(ctx, [fund("2622", "open", "Ингосстрах-валютные облигации")])
    assert fd.status == "ok"
    assert fd.page_url == ing.DISCLOSURE_PAGE
    assert any("нет страницы" in w for w in fd.warnings)
    assert {r.period for r in fd.official_returns} == {"1m", "3m", "6m", "1y"}
