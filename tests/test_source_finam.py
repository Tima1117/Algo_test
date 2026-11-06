"""УК «Финам Менеджмент» (finam): разбор настоящих ответов www.fdu.ru (снято 06.11.2026; вырезаны шапка,
подвал, скрипты и форма звонка, в таблице страницы динамики — середина, в выгрузке — строки до 01.08.2025), без сети.

Контрольные значения сверены на сайте:
- «Динамика доходности» ОПИФ «Финам Первый» (/documents/disclosure/first_fund/dynamics_profit_first_fund/):
  на 03.11.2026 пай 269,6200, СЧА 30 751 855,86; на 30.09.2026 — 252,2600 и 29 250 345,74;
  «Пай**» за 1/3/6/12 мес.: 6,7464 / 5,1888 / −6,6833 / −0,4725 %;
- БПИФ «Финам – Денежный рынок»: на 03.11.2026 пай 14,0891, СЧА 8 431 717 461,92; «Пай**» за 3 мес. 3,4169 %;
- БПИФ «Финам Активное управление»: первая дата 10.06.2026 (10,0065 / 60 458 280,74), за 6 и 12 мес.
  сайт показывает тот же прирост с начала 4,4611 %;
- меню раскрытия: «… «Финам Первый», правила доверительного управления фондом №0097-59837006 от 01.04.2003»
  (в реестре ЦБ — «0097а-59837006»).
"""
from __future__ import annotations

import hashlib
import logging
import ssl
from datetime import date
from pathlib import Path

import pytest

import pif.sources.finam as finam
from pif.http import Fetcher, FetchError, Manifest, RawDoc
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.finam import (DISCLOSURE_PAGE, EXPORT_URL, FDU_INTERMEDIATE_CA, HOST, FinamAdapter, _rules_key,
                               _trust_fdu_ca, export_period, months_back, parse_disclosure, parse_dynamics,
                               parse_export, parse_fund_menu)
from snapshot import assert_series

FIX = Path(__file__).parent / "fixtures" / "finam"
DISC = "https://www.fdu.ru/documents/disclosure/"
FF_DYN = DISC + "first_fund/dynamics_profit_first_fund/"
AC_DYN = DISC + "finam_activecontrol/dynamics_profit_finam_activecontrol/"


def read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def text(name: str) -> str:
    return read(name).decode("utf-8")


# ---------- раскрытие: номер правил -> раздел фонда -> «Динамика доходности» ----------

def test_disclosure_rules():
    site = parse_disclosure(read("disclosure.html"))
    assert sorted(site) == ["0097-59837006", "1879-94167953", "6459", "7306", "7802"]
    assert site["6459"].name == "Финам - Денежный рынок"
    assert site["6459"].url == DISC + "money_market/"
    assert site["0097-59837006"].url == DISC + "first_fund/"
    assert site["7802"].title == "БПИФ рыночных финансовых инструментов «Финам Активное управление»"


def test_disclosure_broken():
    with pytest.raises(UnknownFormat):
        parse_disclosure(text("disclosure.html").replace("правила доверительного управления фондом", "правила"))


def test_fund_menu():
    assert parse_fund_menu(read("root_first_fund.html"), DISC + "first_fund/") == FF_DYN
    assert parse_fund_menu(read("root_finam_activecontrol.html"), DISC + "finam_activecontrol/") == AC_DYN
    with pytest.raises(UnknownFormat):
        parse_fund_menu(text("root_first_fund.html").replace("Динамика доходности", "Динамика"), DISC + "first_fund/")
    with pytest.raises(UnknownFormat):        # ссылка есть, но чужого раздела
        parse_fund_menu(read("root_first_fund.html"), DISC + "money_market/")


def test_rules_key():
    assert _rules_key("0097а-59837006") == _rules_key("0097-59837006") == "0097-59837006"   # кириллическая «а»
    assert _rules_key("5435-СД") == "5435-сд"         # буквы после дефиса — часть номера, не трогаем
    assert _rules_key("6459") == "6459"


# ---------- «Динамика доходности»: прирост пая за период ----------

def test_dynamics_values():
    d = parse_dynamics(read("dyn_first_fund_ThreeMonths.html"))
    assert (d.period, d.growth_pct, d.currency) == ("ThreeMonths", 5.1888, "RUB")
    assert (d.latest, d.earliest) == (date(2026, 11, 3), date(2026, 8, 3))
    assert d.export_name == "dynamics_profit_first_fund"
    assert parse_dynamics(read("dyn_first_fund_Year.html")).growth_pct == -0.4725
    mm = parse_dynamics(read("dyn_money_market_ThreeMonths.html"))
    assert (mm.growth_pct, mm.export_name, mm.latest) == (3.4169, "dynamics_profit_money_market", date(2026, 11, 3))
    ac = parse_dynamics(read("dyn_finam_activecontrol_Year.html"))
    assert (ac.period, ac.growth_pct, ac.earliest, ac.latest) == ("Year", 4.4611, date(2026, 6, 10), date(2026, 11, 5))


@pytest.mark.parametrize("old,new", [
    ("Пай**:", "Пай:"),                                          # нет строки итогов
    ("прирост расчетной стоимости пая", "изменение"),            # непонятно, что за процент
    ("российских рублях", "у.е."),                               # непонятная валюта
    ("<th style=\"text-align: left;\">СЧА*</th>", "<th>Сумма</th>"),   # колонки таблицы
    ("/documents/fund/report/", "/documents/other/"),            # нет ссылки на выгрузку
    ("<option selected=\"\" ", "<option "),                      # не отмечен выбранный период
])
def test_dynamics_broken(old, new):
    html = text("dyn_first_fund_ThreeMonths.html")
    assert old in html
    with pytest.raises(UnknownFormat):
        parse_dynamics(html.replace(old, new))


# ---------- выгрузка «Экспорт данных в Excel»: история СЧА и пая ----------

def test_export_first_fund():
    pts = parse_export(read("export_first_fund_1.html"))
    by_date = {p.date: p for p in pts}
    assert (pts[0].date, pts[-1].date) == (date(2025, 8, 1), date(2026, 11, 3))
    assert (by_date[date(2026, 11, 3)].nav, by_date[date(2026, 11, 3)].price) == (30751855.86, 269.62)
    assert (by_date[date(2026, 9, 30)].nav, by_date[date(2026, 9, 30)].price) == (29250345.74, 252.26)
    assert date(2026, 4, 3) in by_date       # в выгрузке «03.04.2026 11:16:00» — дата со временем
    assert all(a.date < b.date for a, b in zip(pts, pts[1:]))
    assert_series(pts, "finam_export_first_fund")


def test_export_money_market_and_young_fund():
    mm = parse_export(read("export_money_market_1.html"))
    assert (mm[-1].date, mm[-1].nav, mm[-1].price) == (date(2026, 11, 3), 8431717461.92, 14.0891)
    assert_series(mm, "finam_export_money_market")
    ac = parse_export(read("export_finam_activecontrol_1.html"))
    assert (ac[0].date, ac[0].nav, ac[0].price) == (date(2026, 6, 10), 60458280.74, 10.0065)
    assert (ac[-1].date, ac[-1].price) == (date(2026, 11, 5), 10.4529)
    assert_series(ac, "finam_export_finam_activecontrol")


def test_export_broken():
    html = text("export_finam_activecontrol_1.html")
    with pytest.raises(UnknownFormat):        # переименована колонка
        parse_export(html.replace("<th>Стоимость одного пая</th>", "<th>Пай</th>"))
    with pytest.raises(UnknownFormat):        # удалена колонка
        parse_export(html.replace("<th>Стоимость чистых активов</th>", ""))
    with pytest.raises(UnknownFormat):        # в строке не хватает ячейки
        parse_export(html.replace("<td>64228722,89</td>", "", 1))
    with pytest.raises(UnknownFormat):        # дата повторяется
        parse_export(html.replace("05.11.2026 0:00:00", "02.11.2026 0:00:00"))
    with pytest.raises(UnknownFormat):        # не число
        parse_export(html.replace("<td>10,4529</td>", "<td>н.д.%</td>", 1))
    with pytest.raises(UnknownFormat):
        parse_export("<html><body><p>нет данных</p></body></html>")


# ---------- периоды ----------

def test_months_back():
    assert months_back(date(2026, 11, 3), 1) == date(2026, 10, 3)
    assert months_back(date(2026, 11, 3), 12) == date(2025, 11, 3)
    assert months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_back(date(2026, 1, 15), 3) == date(2025, 10, 15)


def test_export_period():
    today = date(2026, 11, 6)
    assert export_period(date(2025, 9, 1), today) == 1        # первый сбор: три года (год начался бы 06.11.2025)
    assert export_period(date(2026, 10, 24), today) == 6      # докачка хвоста: месяц
    assert export_period(date(2026, 9, 1), today) == 3        # три месяца
    assert export_period(date(2026, 6, 1), today) == 4        # шесть месяцев
    assert export_period(date(2026, 5, 1), today) == 5        # 6 мес. назад — 06.05.2026, позже нужного: год
    assert export_period(date(2025, 11, 30), today) == 5      # год
    assert export_period(date(2022, 1, 1), today) == 7        # пять лет
    assert export_period(date(2003, 1, 1), today) == 0        # весь период


# ---------- TLS: недостающий промежуточный сертификат только для www.fdu.ru ----------

def test_embedded_intermediate_fingerprint():
    der = ssl.PEM_cert_to_DER_cert(FDU_INTERMEDIATE_CA)
    assert hashlib.sha256(der).hexdigest().upper() == (
        "B59410F367B986E98E53A9789A6580553A74BE515211D9F6ECB6E8A11E83AEAA")
    ctx = ssl.create_default_context()
    before = len(ctx.get_ca_certs())
    ctx.load_verify_locations(cadata=FDU_INTERMEDIATE_CA)
    loaded = [c for c in ctx.get_ca_certs()
              if dict(x[0] for x in c["subject"]).get("commonName") == "GlobalSign GCC R46 DV TLS CA 2025"]
    assert len(ctx.get_ca_certs()) >= before and len(loaded) == 1
    assert dict(x[0] for x in loaded[0]["issuer"])["commonName"] == "GlobalSign Root R46"


def test_trust_only_fdu_host(tmp_path):
    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path)
    try:
        assert _trust_fdu_ca(http) is True
        assert http._extra_ca == {HOST: FDU_INTERMEDIATE_CA}
        site_client = http._client(HOST)
        assert site_client is not http._client("cdn-lpbr01.finam.ru")
        assert _trust_fdu_ca(http) is True and http._client(HOST) is site_client   # идемпотентно
    finally:
        http.close()
    assert _trust_fdu_ca(object()) is False         # не Fetcher


# ---------- сбор целиком (без сети) ----------

class FakeHttp:
    """Вместо сети — файлы фикстур."""

    SECTIONS = {DISC + "first_fund/": "first_fund", DISC + "finam_activecontrol/": "finam_activecontrol"}
    DYN = {FF_DYN: "first_fund", AC_DYN: "finam_activecontrol"}
    EXPORT = {"dynamics_profit_first_fund": "first_fund", "dynamics_profit_finam_activecontrol": "finam_activecontrol"}

    def __init__(self, errors: dict | None = None):
        self.errors = errors or {}
        self.calls: list[tuple] = []

    def get(self, url, *, params=None, fmt="html", funds=None, method="GET", data=None, **kw):
        self.calls.append((method, url, params, data, funds))
        err = self.errors.get((url, (data or {}).get("timePeriod")), self.errors.get(url))
        if err is not None:
            raise err
        if url == DISCLOSURE_PAGE:
            name = "disclosure.html"
        elif url in self.SECTIONS:
            name = f"root_{self.SECTIONS[url]}.html"
        elif url in self.DYN and method == "POST":
            name = f"dyn_{self.DYN[url]}_{data['timePeriod']}.html"
        elif url == EXPORT_URL:
            name = f"export_{self.EXPORT[params['name']]}_1.html"     # любой код периода — одна фикстура
        else:
            raise FetchError(url, "HTTP 404", 404)
        path = FIX / name
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        return RawDoc(full, path, hashlib.sha256(path.read_bytes()).hexdigest(), "2026-11-06T08:00:00+00:00", fmt)


def make_fund(rules: str, name: str, type_: str = "exchange", status: str = "Сформирован") -> Fund:
    return Fund(rules=rules, name=name, full_name=name, type=type_, category="", status=status,
                reg_date="2024-09-05", formation_end=None, uk_name="", uk_short="Финам Менеджмент", uk_ogrn="",
                uk_inn="7744002606", uk_site="https://www.fdu.ru", exchange_tradable=True)


FUNDS = [make_fund("0097а-59837006", "Финам Первый", "open"),
         make_fund("7802", "Финам Активное управление"),
         make_fund("9999", "Финам Нет такого")]


def run(http, funds, since=date(2025, 9, 1), last_dates=None, monkeypatch=None):
    monkeypatch.setattr(finam, "_today", lambda: date(2026, 11, 6))
    ctx = Context(http=http, since=since, log=logging.getLogger("test"), last_dates=last_dates or {})
    return {fd.rules: fd for fd in FinamAdapter().collect(ctx, funds)}


def test_collect_end_to_end(monkeypatch):
    http = FakeHttp()
    res = run(http, FUNDS, monkeypatch=monkeypatch)

    ff = res["0097а-59837006"]
    assert (ff.status, ff.currency, ff.page_url) == ("ok", "RUB", FF_DYN)
    assert any("0097-59837006" in w and "0097а-59837006" in w for w in ff.warnings)
    (seg,) = ff.segments
    assert seg.page_url == FF_DYN and "name=dynamics_profit_first_fund&timePeriod=1" in seg.raw_url
    assert seg.points[0].date == date(2025, 9, 1)                   # отрезано по since
    assert (seg.points[-1].date, seg.points[-1].nav, seg.points[-1].price) == (date(2026, 11, 3), 30751855.86, 269.62)
    assert [(r.period, r.value_pct, r.as_of, r.source_url) for r in ff.official_returns] == [
        ("1m", 6.7464, date(2026, 11, 3), FF_DYN), ("3m", 5.1888, date(2026, 11, 3), FF_DYN),
        ("6m", -6.6833, date(2026, 11, 3), FF_DYN), ("1y", -0.4725, date(2026, 11, 3), FF_DYN)]
    assert ("POST", FF_DYN, None, {"timePeriod": "Year"}, ["0097а-59837006"]) in http.calls
    assert ("GET", EXPORT_URL, {"name": "dynamics_profit_first_fund", "timePeriod": 1}, None,
            ["0097а-59837006"]) in http.calls

    ac = res["7802"]
    assert ac.status == "ok" and ac.segments[0].points[0].date == date(2026, 6, 10)
    # за 6 и 12 мес. сайт показывает прирост с 10.06.2026 — это не доходность за период
    assert [(r.period, r.value_pct) for r in ac.official_returns] == [("1m", 6.0842), ("3m", 0.6112)]
    assert any("6 мес., 1 год" in w and "10.06.2026" in w for w in ac.warnings)

    assert res["9999"].status == "not_found" and "№ 9999" in res["9999"].reason


def test_collect_tail_uses_short_period(monkeypatch):
    http = FakeHttp()
    res = run(http, FUNDS[:1], last_dates={"0097а-59837006": date(2026, 10, 30)}, monkeypatch=monkeypatch)
    assert res["0097а-59837006"].status == "ok"
    assert ("GET", EXPORT_URL, {"name": "dynamics_profit_first_fund", "timePeriod": 6}, None,
            ["0097а-59837006"]) in http.calls


def test_collect_letter_mismatch_needs_same_name(monkeypatch):
    res = run(FakeHttp(), [make_fund("0097а-59837006", "Финам Другой", "open")], monkeypatch=monkeypatch)
    assert res["0097а-59837006"].status == "not_found"


def test_collect_site_down(monkeypatch):
    down = FakeHttp(errors={DISCLOSURE_PAGE: FetchError(DISCLOSURE_PAGE, "сайт недоступен (ConnectError)")})
    res = run(down, FUNDS[:2], monkeypatch=monkeypatch)
    assert {fd.status for fd in res.values()} == {"unavailable"}


def test_collect_one_fund_broken(monkeypatch):
    http = FakeHttp(errors={(AC_DYN, "SixMonths"): FetchError(AC_DYN, "HTTP 500", 500)})
    res = run(http, FUNDS[:2], monkeypatch=monkeypatch)
    assert res["7802"].status == "unavailable"
    assert res["0097а-59837006"].status == "ok"
