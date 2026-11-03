"""Доходность пая по собранному ряду и сверка с официально опубликованной УК.

Для фондов с выплатой дохода доходность считается с учётом выплат так, как её публикуют УК:

    доходность = (пай_конец + Σ выплат на пай за (начало; конец]) / пай_начало − 1

Выплаты просто прибавляются, без реинвестирования. Формула подобрана не «на глаз»: ею до 0,01 п. п.
воспроизводятся все официальные цифры УК «Первая» по фондам с выплатами, а вариант с реинвестированием
по цене на дату выплаты систематически завышал доходность.
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


def is_month_end(d: date) -> bool:
    """Последний рабочий день месяца (до конца месяца остались только выходные)."""
    last = calendar.monthrange(d.year, d.month)[1]
    return all(date(d.year, d.month, k).weekday() >= 5 for k in range(d.day + 1, last + 1))


def period_start(end: date, months: int) -> date:
    """Начало периода. УК считают доходность на конец месяца «от конца месяца до конца месяца»:
    за 3 месяца на 30.10.2026 (пятница, последний рабочий день) — от 31.07.2026, а не от 30.07.
    Для прочих дат — тот же день N месяцев назад."""
    if is_month_end(end):
        start = minus_months(date(end.year, end.month, 1), months)
        return date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    return minus_months(end, months)


def period_return(points: list[Point], end: date, months: int, payouts: list[dict] | None = None) -> float | None:
    """Доходность пая в % за months месяцев, заканчивая датой end (с учётом выплат дохода, если они есть)."""
    p1 = price_at(points, end)
    p0 = price_at(points, period_start(end, months))
    if p0 is None or p1 is None:
        return None
    paid = sum(pay["amount"] for pay in payouts or []
               if p0.date < date.fromisoformat(str(pay["date"])[:10]) <= p1.date)
    return ((p1.price + paid) / p0.price - 1) * 100


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
