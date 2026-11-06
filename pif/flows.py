"""Месячные точки и чистые притоки.

Чистый приток за месяц m (в валюте пая):

    приток_m = СЧА_m − СЧА_{m−1} × (пай_m / пай_{m−1})

СЧА_{m−1} × (пай_m / пай_{m−1}) — сколько стоила бы СЧА прошлого месяца, если бы
число паёв не менялось и менялась только их стоимость. Разница — деньги, которые
пайщики принесли (выдача паёв) или забрали (погашение). В рубли приток валютного фонда
пересчитывается по курсу ЦБ на дату точки m.

Берётся последняя опубликованная точка месяца, где есть и СЧА, и стоимость пая.
Если точки за прошлый месяц нет, приток не считается (пропуск, а не ноль).
В месяц формирования фонда приток = СЧА на конец месяца (до этого денег в фонде не было).
"""
from __future__ import annotations

from datetime import date

from .fx import Rates
from .normalize import Point


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(key: str, n: int) -> str:
    y, m = map(int, key.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def month_range(first: str, last: str) -> list[str]:
    out, k = [], first
    while k <= last:
        out.append(k)
        k = add_months(k, 1)
    return out


def month_end_points(points: list[Point]) -> dict[str, Point]:
    out: dict[str, Point] = {}
    for p in points:
        if p.nav is not None and p.price is not None:
            out[month_key(p.date)] = p   # точки отсортированы — останется последняя в месяце
    return out


def monthly_rows(points: list[Point], currency: str, rates: Rates, formation_end: str | None,
                 last_month: str) -> list[dict]:
    """Строки по месяцам от первой точки до last_month включительно, с притоками."""
    ends = month_end_points(points)
    if not ends:
        return []
    first = min(ends)
    rows: list[dict] = []
    formed_in = month_key(date.fromisoformat(formation_end)) if formation_end else None
    for k in month_range(first, last_month):
        p = ends.get(k)
        if p is None:
            rows.append({"month": k, "date": None, "nav": None, "price": None, "nav_rub": None,
                         "flow": None, "flow_rub": None, "note": "нет опубликованной точки за месяц"})
            continue
        rate = rates.on(currency, p.date)
        row = {"month": k, "date": p.date.isoformat(), "nav": p.nav, "price": p.price,
               "nav_rub": p.nav * rate if rate else None, "flow": None, "flow_rub": None, "note": "",
               "src": p.src, "src_nav": p.src_nav}
        prev = ends.get(add_months(k, -1))
        if prev is not None:
            row["flow"] = p.nav - prev.nav * (p.price / prev.price)
        elif k == first and formed_in is not None and formed_in >= add_months(k, -1):
            row["flow"] = p.nav
            row["note"] = "месяц формирования: приток = СЧА на конец месяца"
        else:
            row["note"] = "нет точки за предыдущий месяц — приток не считается"
        if row["flow"] is not None and rate:
            row["flow_rub"] = row["flow"] * rate
        rows.append(row)
    return rows


def period_flow(rows: list[dict], last_month: str, months: int) -> tuple[float | None, int]:
    """Сумма притоков за months месяцев, заканчивая last_month, по тем месяцам, где приток посчитан.

    Возвращает (сумма, сколько месяцев в неё вошло). Месяцы без данных не додумываются — сумма просто
    короче, и это видно по счётчику. Так же считаются своды по УК, типам и рынку, поэтому сумма по
    фондам всегда равна итогам.
    """
    by = {r["month"]: r for r in rows}
    vals = [r["flow_rub"] for i in range(months)
            if (r := by.get(add_months(last_month, -i))) is not None and r["flow_rub"] is not None]
    return (sum(vals) if vals else None), len(vals)


def aggregate(fund_rows: dict[str, list[dict]], groups: dict[str, str], months: list[str]) -> dict[str, dict]:
    """Свод притоков и СЧА (в рублях) по группе: group_key -> month -> {flow_rub, nav_rub, funds}."""
    out: dict[str, dict] = {}
    for rules, rows in fund_rows.items():
        g = groups.get(rules)
        if g is None:
            continue
        for r in rows:
            if r["month"] not in months:
                continue
            cell = out.setdefault(g, {}).setdefault(r["month"], {"flow_rub": 0.0, "nav_rub": 0.0,
                                                                 "funds_flow": 0, "funds_nav": 0})
            if r["flow_rub"] is not None:
                cell["flow_rub"] += r["flow_rub"]
                cell["funds_flow"] += 1
            if r["nav_rub"] is not None:
                cell["nav_rub"] += r["nav_rub"]
                cell["funds_nav"] += 1
    return out
