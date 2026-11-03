"""Доходность по ряду, сверка с официальной и курсы ЦБ."""
from datetime import date, timedelta

import pytest

from conftest import FIXTURES
from pif.fx import Rates, parse_cbr_dynamic
from pif.normalize import Point
from pif.parsing import UnknownFormat
from pif.returns import compare_official, period_return


def daily(start, days, price0, step):
    return [Point(start + timedelta(days=i), 1.0, price0 + step * i, 0) for i in range(days)]


def test_period_return():
    pts = daily(date(2026, 1, 1), 120, 100.0, 0.1)
    # 31.03 → цена 100 + 0.1*89 = 108.9; 28.02 → 100 + 0.1*58 = 105.8
    assert period_return(pts, date(2026, 3, 31), 1) == pytest.approx((108.9 / 105.8 - 1) * 100)


def test_compare_official_flags_discrepancy():
    pts = daily(date(2025, 12, 1), 150, 100.0, 0.1)
    ours = (112.0 / 108.9 - 1) * 100  # 31.03: 100 + 0.1*120; 28.02: 100 + 0.1*89
    official = [{"period": "1m", "value_pct": ours + 0.2, "as_of": "2026-03-31", "source_url": "s"},
                {"period": "3m", "value_pct": 1.0, "as_of": "2026-03-31", "source_url": "s"}]
    checks, bad = compare_official("X", pts, official)
    assert checks[0]["status"] == "сходится"
    assert len(bad) == 1 and bad[0]["period"] == "3m"


def test_cbr_rates_fixture():
    rows = parse_cbr_dynamic((FIXTURES / "cbr" / "usd_dynamic.xml").read_bytes())
    assert rows[0] == ("2026-10-01", pytest.approx(83.5588))
    r = Rates({"USD": rows})
    assert r.on("USD", "2026-10-01") == pytest.approx(83.5588)
    assert r.on("USD", "2026-09-01") is None
    assert r.on("RUB", "2026-10-01") == 1.0
    with pytest.raises(UnknownFormat):
        parse_cbr_dynamic(b"<html>error</html>")


def test_return_with_payout_added():
    # пай 100 → выплата 5 ₽ на 15.02 (цена падает до 95) → к 31.03 снова 100: (100 + 5) / 100 − 1 = 5%
    pts = [Point(date(2026, 1, 31), 1.0, 100.0, 0), Point(date(2026, 2, 14), 1.0, 100.0, 0),
           Point(date(2026, 2, 15), 1.0, 100.0, 0), Point(date(2026, 2, 16), 1.0, 95.0, 0),
           Point(date(2026, 3, 31), 1.0, 100.0, 0)]
    pay = [{"date": "2026-02-15", "amount": 5.0, "source_url": "s"}]
    assert period_return(pts, date(2026, 3, 31), 2) == pytest.approx(0.0)              # только цена
    assert period_return(pts, date(2026, 3, 31), 2, pay) == pytest.approx(5.0)          # с выплатой


def test_period_start_month_end_convention():
    from pif.returns import period_start
    # 30.10.2026 — пятница, последний рабочий день октября: период от конца месяца
    assert period_start(date(2026, 10, 30), 3) == date(2026, 7, 31)
    assert period_start(date(2026, 10, 30), 12) == date(2025, 10, 31)
    # 02.11.2026 — обычный день: тот же день N месяцев назад
    assert period_start(date(2026, 11, 2), 1) == date(2026, 10, 2)


def test_payout_on_period_start_not_counted():
    pts = [Point(date(2026, 1, 30), 1.0, 95.0, 0), Point(date(2026, 2, 27), 1.0, 96.0, 0)]
    pay = [{"date": "2026-01-30", "amount": 5.0, "source_url": "s"}]   # цена на эту дату уже без выплаты
    assert period_return(pts, date(2026, 2, 27), 1, pay) == pytest.approx((96 / 95 - 1) * 100)
