"""ПАО «УК «Арсагера» (arsagera.ru): разбор настоящих ответов API «Метрики фонда» и сбор без сети.

Фикстуры (tests/fixtures/arsagera/) — настоящие ответы API 03.11.2026, без урезания:
* fa_2025q3.json, fa_2026q3.json, fa_2026q4.json — /api/v1/funds/fa/fund-metrics/?from=…&to=… за кварталы;
* fo_2026q4.json — то же для «фонда облигаций КР 1.55».
Значения сверены с блоком «Стоимость пая и чистых активов» на странице фонда
https://arsagera.ru/products/opifa_arsagera_-_fond_akcij/ (на 30.10.2026 и 02.11.2026), а доходности,
посчитанные по ряду, — с таблицей «изменение стоимости пая … на 30.09.2026» там же.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources import arsagera
from pif.sources.arsagera import ArsageraAdapter, match_code, parse_metrics, quarter_windows
from pif.sources.base import Context

FIX = Path(__file__).parent / "fixtures" / "arsagera"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _fund(rules: str, name: str, full_name: str) -> Fund:
    return Fund(rules=rules, name=name, full_name=full_name, type="open", category="", status="Сформирован",
                reg_date="2005-06-01", formation_end=None, uk_name="", uk_short="Арсагера", uk_ogrn="",
                uk_inn="7840303927", uk_site="https://www.arsagera.ru", exchange_tradable=True)


FA = _fund("0363-75409054", "Арсагера – ФА",
           "открытый паевой инвестиционный фонд рыночных финансовых инструментов «Арсагера - фонд акций»")
FO = _fund("2721", "Арсагера – КР 1.55",
           'открытый паевой инвестиционный фонд рыночных финансовых инструментов "Арсагера - фонд облигаций КР 1.55"')


# ---------- разбор ----------

def test_metrics_fa_values_match_fund_page():
    pts = {p.date: p for p in parse_metrics(_read("fa_2026q4.json"))}
    # страница фонда: «на 30.10.2026 13 694,11 / 2 615 024 369,54», «на 02.11.2026 13 591,50 / 2 595 771 594,86»
    assert (pts[date(2026, 10, 30)].price, pts[date(2026, 10, 30)].nav) == (13694.11, 2615024369.54)
    assert (pts[date(2026, 11, 2)].price, pts[date(2026, 11, 2)].nav) == (13591.5, 2595771594.86)
    assert date(2026, 10, 31) not in pts           # выходные в свежей истории не публикуются


def test_metrics_reproduce_official_returns_on_page():
    """Ряд API даёт те же цифры, что УК публикует на странице фонда (на 30.09.2026)."""
    pts = {p.date: p.price for f in ("fa_2025q3.json", "fa_2026q3.json") for p in parse_metrics(_read(f))}
    end = pts[date(2026, 9, 30)]
    assert round((end / pts[date(2026, 8, 31)] - 1) * 100, 2) == -0.22
    assert round((end / pts[date(2025, 9, 30)] - 1) * 100, 2) == -15.16


def test_metrics_empty_window_is_not_an_error():
    assert parse_metrics(b'{"data": []}') == []


@pytest.mark.parametrize("broken", [
    lambda d: {"error": "Invalid fund code."},                                   # ответ-ошибка вместо данных
    lambda d: {"data": {"2026-10-01": d["data"][0]}},                            # data — не список
    lambda d: {"data": [{k: v for k, v in r.items() if k != "total_net_assets"} for r in d["data"]]},  # нет СЧА
    lambda d: {"data": [{**r, "date": "01/10/2026 г."} for r in d["data"]]},     # дата не та
])
def test_metrics_broken_raises(broken):
    data = json.loads(_read("fa_2026q4.json"))
    with pytest.raises(UnknownFormat):
        parse_metrics(json.dumps(broken(data)))


# ---------- окна и сопоставление ----------

def test_quarter_windows_cover_range_with_stable_bounds():
    assert quarter_windows(date(2025, 9, 1), date(2026, 1, 5)) == [
        (date(2025, 7, 1), date(2025, 9, 30)), (date(2025, 10, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 3, 31))]
    assert quarter_windows(date(2026, 10, 24), date(2026, 11, 3)) == [(date(2026, 10, 1), date(2026, 12, 31))]


def test_match_code_by_quoted_full_name():
    assert match_code(FA) == "fa"
    assert match_code(FO) == "fo"
    assert match_code(_fund("0364-75409132", "Арсагера – СИ", "… «Арсагера - фонд смешанных инвестиций»")) == "f4si"
    assert match_code(_fund("0439-75408664", "Арсагера – А 6.4", "… «Арсагера – акции 6.4»")) == "f64"
    assert match_code(_fund("1", "Арсагера – АМ", "закрытый … «Арсагера – акции мира»")) is None


# ---------- сбор целиком (без сети) ----------

ROUTES = {
    "/api/v1/funds/fa/fund-metrics/?from=2026-10-01&to=2026-12-31": "fa_2026q4.json",
    "/api/v1/funds/fo/fund-metrics/?from=2026-10-01&to=2026-12-31": "fo_2026q4.json",
}


def test_collect_tail_only_quarter_by_quarter(tmp_path, monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        seen.append(path)
        if path not in ROUTES:
            return httpx.Response(404, content=b'{"error":"Invalid fund code."}',
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=_read(ROUTES[path]), headers={"content-type": "application/json"})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(arsagera, "_today", lambda: date(2026, 11, 3))
    # повторный сбор: последняя точка уже есть -> запрашиваем только квартал с хвостом
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.arsagera"),
                  last_dates={FA.rules: date(2026, 10, 20), FO.rules: date(2026, 10, 20)})
    missing = _fund("9999", "Арсагера – АМ", "закрытый паевой инвестиционный фонд «Арсагера – акции мира»")
    fa, fo, nf = ArsageraAdapter().collect(ctx, [FA, FO, missing])

    assert fa.status == "ok" and fa.currency == "RUB"
    assert fa.page_url == "https://arsagera.ru/products/opifa_arsagera_-_fond_akcij/"
    (seg,) = fa.segments
    assert seg.raw_url == "https://arsagera.ru/api/v1/funds/fa/fund-metrics/?from=2026-10-01&to=2026-12-31"
    assert (seg.points[-1].date, seg.points[-1].price) == (date(2026, 11, 2), 13591.5)
    assert fa.official_returns == [] and "доходность" in fa.warnings[0]

    assert fo.status == "ok" and fo.segments[0].points[-1].nav == 335741188.26
    assert nf.status == "not_found" and "акции мира" in nf.reason
    assert len(seen) == 2


def test_collect_api_error_becomes_status(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"Invalid fund code."}', headers={"content-type": "application/json"})

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(arsagera, "_today", lambda: date(2026, 11, 3))
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.arsagera"))
    (fa,) = ArsageraAdapter().collect(ctx, [FA])
    assert fa.status == "unavailable" and "404" in fa.reason
