"""Расчёт притоков на примере с заранее известными числами."""
from datetime import date

import pytest

from pif.flows import aggregate, monthly_rows, period_flow
from pif.fx import Rates
from pif.normalize import Point

RUB = Rates({})


def pts(*rows):
    return [Point(date.fromisoformat(d), nav, price, 0) for d, nav, price in rows]


def test_flow_known_numbers():
    # 100 паёв по 10 ₽ → +10% и выдали 20 паёв → −10% и погасили 10 паёв
    series = pts(("2026-01-30", 1000.0, 10.0),
                 ("2026-02-15", 1100.0, 10.5),   # промежуточная точка месяца — не используется
                 ("2026-02-27", 1320.0, 11.0),
                 ("2026-03-31", 1089.0, 9.9))
    rows = monthly_rows(series, "RUB", RUB, None, "2026-03")
    by = {r["month"]: r for r in rows}
    assert by["2026-01"]["flow"] is None                    # нет прошлого месяца — пропуск, а не ноль
    assert by["2026-02"]["date"] == "2026-02-27"            # последняя точка месяца
    assert by["2026-02"]["flow"] == pytest.approx(220.0)    # 20 паёв × 11 ₽
    assert by["2026-03"]["flow"] == pytest.approx(-99.0)    # −10 паёв × 9,9 ₽
    assert period_flow(rows, "2026-03", 2) == pytest.approx(121.0)
    assert period_flow(rows, "2026-03", 3) is None          # январь неизвестен


def test_missing_month_is_gap_not_zero():
    series = pts(("2026-01-30", 1000.0, 10.0), ("2026-03-31", 1200.0, 10.0))
    rows = monthly_rows(series, "RUB", RUB, None, "2026-03")
    by = {r["month"]: r for r in rows}
    assert by["2026-02"]["nav"] is None and by["2026-02"]["flow"] is None
    assert by["2026-03"]["flow"] is None


def test_formation_month():
    series = pts(("2026-05-29", 5000.0, 100.0), ("2026-06-30", 7000.0, 101.0))
    rows = monthly_rows(series, "RUB", RUB, "2026-05-20", "2026-06")
    assert rows[0]["flow"] == pytest.approx(5000.0)
    assert "формирования" in rows[0]["note"]
    assert rows[1]["flow"] == pytest.approx(7000 - 5000 * 1.01)


def test_currency_flow_in_rubles():
    rates = Rates({"USD": [("2026-01-30", 80.0), ("2026-02-27", 90.0)]})
    series = pts(("2026-01-30", 100.0, 1.0), ("2026-02-27", 150.0, 1.0))
    rows = monthly_rows(series, "USD", rates, None, "2026-02")
    assert rows[1]["flow"] == pytest.approx(50.0)
    assert rows[1]["flow_rub"] == pytest.approx(50.0 * 90.0)
    assert rows[1]["nav_rub"] == pytest.approx(150.0 * 90.0)


def test_aggregates_sum_up():
    a = monthly_rows(pts(("2026-01-30", 1000.0, 10.0), ("2026-02-27", 1320.0, 11.0)), "RUB", RUB, None, "2026-02")
    b = monthly_rows(pts(("2026-01-30", 500.0, 5.0), ("2026-02-27", 450.0, 5.0)), "RUB", RUB, None, "2026-02")
    rows = {"A": a, "B": b}
    by_uk = aggregate(rows, {"A": "uk1", "B": "uk2"}, ["2026-02"])
    market = aggregate(rows, {"A": "all", "B": "all"}, ["2026-02"])
    assert by_uk["uk1"]["2026-02"]["flow_rub"] == pytest.approx(220.0)
    assert by_uk["uk2"]["2026-02"]["flow_rub"] == pytest.approx(-50.0)
    assert market["all"]["2026-02"]["flow_rub"] == pytest.approx(170.0)
    assert market["all"]["2026-02"]["nav_rub"] == pytest.approx(1770.0)
