"""Доходность пая по собранному ряду и сверка с официально опубликованной УК.

Для фондов с выплатой дохода доходность считается с учётом выплат, как её публикуют УК:
каждая выплата D на дату t как бы реинвестируется по стоимости пая на эту дату —
множитель (1 + D / пай_t). Без этого цена пая после выплаты падает, и доходность занижается.
"""
from __future__ import annotations

import bisect
import calendar
from datetime import date, timedelta

from .normalize import Point

PERIODS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12}
TOLERANCE_PP = 0.5
# точка начала/конца периода может отстоять от нужной даты на выходные и праздники
MAX_LAG_DAYS = 7


def minus_months(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def price_at(points: list[Point], d: date) -> Point | None:
    """Последняя точка с ценой не позже d (и не раньше d − MAX_LAG_DAYS)."""
    priced = [p for p in points if p.price is not None]
    i = bisect.bisect_right([p.date for p in priced], d) - 1
    if i < 0:
        return None
    p = priced[i]
    return p if (d - p.date) <= timedelta(days=MAX_LAG_DAYS) else None


def period_return(points: list[Point], end: date, months: int, payouts: list[dict] | None = None) -> float | None:
    """Доходность пая в % за months месяцев, заканчивая датой end (с учётом выплат дохода, если они есть)."""
    p1 = price_at(points, end)
    p0 = price_at(points, minus_months(end, months))
    if p0 is None or p1 is None:
        return None
    factor = 1.0
    for pay in payouts or []:
        d = date.fromisoformat(str(pay["date"])[:10])
        if p0.date < d <= p1.date:
            pt = price_at(points, d)
            if pt is None:
                return None          # выплата есть, а цены на её дату нет — честно не считаем
            factor *= 1 + pay["amount"] / pt.price
    return (p1.price * factor / p0.price - 1) * 100


def own_returns(points: list[Point], payouts: list[dict] | None = None) -> tuple[str | None, dict[str, float | None]]:
    priced = [p for p in points if p.price is not None]
    if not priced:
        return None, {k: None for k in PERIODS}
    end = priced[-1].date
    return end.isoformat(), {k: period_return(priced, end, m, payouts) for k, m in PERIODS.items()}


def compare_official(rules: str, points: list[Point], official: list[dict],
                     payouts: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Сверка с официальной доходностью. -> (все сравнения, расхождения > TOLERANCE_PP)."""
    checks, bad = [], []
    for o in official:
        months = PERIODS.get(o["period"])
        if months is None or o.get("value_pct") is None:
            continue
        as_of = date.fromisoformat(str(o["as_of"])[:10]) if o.get("as_of") else None
        ours = period_return(points, as_of, months, payouts) if as_of else None
        row = {"rules": rules, "period": o["period"], "as_of": as_of.isoformat() if as_of else None,
               "official": round(o["value_pct"], 4), "ours": round(ours, 4) if ours is not None else None,
               "diff_pp": round(ours - o["value_pct"], 4) if ours is not None else None,
               "source_url": o.get("source_url")}
        if as_of is None:
            row["status"] = "нет даты расчёта у официальной доходности — сверить нельзя"
        elif ours is None:
            row["status"] = "собранный ряд не покрывает период"
        elif abs(row["diff_pp"]) > TOLERANCE_PP:
            row["status"] = f"расхождение больше {TOLERANCE_PP} п. п."
            bad.append(row)
        else:
            row["status"] = "сходится"
        checks.append(row)
    return checks, bad
