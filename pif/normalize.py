"""Нормализация ряда фонда: одна непрерывная история без ложных скачков.

Правила (каждое исправление пишется в журнал с причиной):
* точки из разных сегментов сливаются по дате; при расхождении берётся более свежая выгрузка;
* значения <= 0 отбрасываются;
* одиночный выброс (точка резко уходит и сразу возвращается) удаляется;
* дробление/консолидация паёв (цена прыгает в целое число раз, а СЧА — нет) пересчитывает
  прошлые цены к новому номиналу;
* СЧА, прыгнувшая ровно в ~1000 раз (тысячи vs рубли), приводится к рублям;
* прочие большие движения не исправляются, а попадают в отчёт качества как аномалии.
Недостающие точки не придумываются: пропуски остаются пропусками.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

SPLIT_FACTORS = (2, 3, 4, 5, 10, 20, 25, 50, 100, 1000, 10000)
BIG_MOVE = 0.25        # движение цены пая больше 25% за шаг — проверяем
SPIKE = 0.4            # одиночный выброс: отклонение > 40% от обоих соседей
GAP_DAYS = {"daily": 10, "weekly": 21, "monthly": 45}


@dataclass
class Point:
    date: date
    nav: float | None
    price: float | None
    src: int


@dataclass
class Normalized:
    points: list[Point]
    sources: list[dict]
    corrections: list[dict] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    frequency: str = "unknown"


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _near(x: float, target: float, tol: float = 0.03) -> bool:
    return abs(x / target - 1) < tol


def merge_segments(rules: str, segments: list[dict]) -> tuple[list[Point], list[dict], list[dict]]:
    """Сливает сегменты в один ряд. Возвращает точки, таблицу источников и журнал исправлений."""
    sources: list[dict] = []
    src_index: dict[tuple, int] = {}
    by_date: dict[str, dict] = {}
    corrections: list[dict] = []
    # старые выгрузки первыми, чтобы свежие их перекрывали
    for seg in sorted(segments, key=lambda s: s["fetched_at"]):
        k = (seg["raw_url"], seg["sha256"])
        if k not in src_index:
            src_index[k] = len(sources)
            sources.append({"id": len(sources), "url": seg["raw_url"], "page": seg.get("page_url"),
                            "sha256": seg["sha256"], "fetched_at": seg["fetched_at"],
                            "stale": bool(seg.get("stale"))})
        sid = src_index[k]
        for p in seg["points"]:
            d = str(p["date"])[:10]
            cur = by_date.get(d)
            for fld in ("nav", "price"):
                v = p.get(fld)
                if v is None:
                    continue
                if cur and cur.get(fld) is not None and abs(cur[fld] - v) > 1e-9 * max(1.0, abs(v)):
                    corrections.append({"rules": rules, "date": d, "field": fld, "action": "replaced",
                                        "old": cur[fld], "new": v,
                                        "reason": "источники расходятся за одну дату — взята более свежая выгрузка"})
                by_date.setdefault(d, {"nav": None, "price": None, "src": sid})
                by_date[d][fld] = v
                by_date[d]["src"] = sid
    points = [Point(date.fromisoformat(d), v["nav"], v["price"], v["src"]) for d, v in sorted(by_date.items())]
    return points, sources, corrections


def detect_frequency(points: list[Point]) -> str:
    if len(points) < 3:
        return "unknown"
    steps = sorted((b.date - a.date).days for a, b in zip(points, points[1:]))
    med = steps[len(steps) // 2]
    if med <= 4:
        return "daily"
    if med <= 10:
        return "weekly"
    if med <= 40:
        return "monthly"
    return "irregular"


def normalize(rules: str, segments: list[dict]) -> Normalized:
    points, sources, corrections = merge_segments(rules, segments)
    log = corrections

    def note(p: Point, fld: str, action: str, old, new, reason: str, **extra) -> None:
        log.append({"rules": rules, "date": p.date.isoformat(), "field": fld, "action": action,
                    "old": old, "new": new, "reason": reason, **extra})

    # 1. мусорные значения
    for p in points:
        for fld in ("nav", "price"):
            v = getattr(p, fld)
            if v is not None and v <= 0:
                note(p, fld, "dropped", v, None, "значение ≤ 0")
                setattr(p, fld, None)

    # 2. одиночные выбросы (по каждому полю отдельно)
    for fld in ("price", "nav"):
        idx = [i for i, p in enumerate(points) if getattr(p, fld) is not None]
        for j in range(1, len(idx) - 1):
            a, b, c = (getattr(points[idx[k]], fld) for k in (j - 1, j, j + 1))
            up, down = b / a, c / b
            if ((up > 1 + SPIKE and down < 1 / (1 + SPIKE)) or (up < 1 / (1 + SPIKE) and down > 1 + SPIKE)) \
                    and 0.9 < c / a < 1.1:
                p = points[idx[j]]
                note(p, fld, "dropped", b, None,
                     f"одиночный выброс: {b:,.4f} при соседях {a:,.4f} и {c:,.4f}".replace(",", " "))
                setattr(p, fld, None)

    def rescale(run: list[Point], fld: str, factor: float, reason: str) -> None:
        for pk in run:
            setattr(pk, fld, getattr(pk, fld) * factor)
        log.append({"rules": rules, "date": run[0].date.isoformat(), "date_to": run[-1].date.isoformat(),
                    "points": len(run), "field": fld, "action": "rescaled", "factor": factor, "reason": reason})

    # 3. СЧА в тысячах вместо рублей: скачок ровно в ~1000 раз при спокойной цене пая.
    #    Ряд режется на участки по таким скачкам, все участки приводятся к рублёвому (крупному) масштабу.
    navs = [p for p in points if p.nav is not None]
    runs, exps = [[navs[0]]] if navs else [], [0]
    for prev, cur in zip(navs, navs[1:]):
        r, pr = cur.nav / prev.nav, _ratio(cur.price, prev.price)
        calm = pr is None or 0.8 < pr < 1.25
        if calm and (_near(r, 1000, 0.15) or _near(r, 0.001, 0.15)):
            runs.append([cur])
            exps.append(exps[-1] + (1 if r > 1 else -1))
        else:
            runs[-1].append(cur)
    if len(runs) > 1:
        top = max(exps)
        for run, e in zip(runs, exps):
            if e < top:
                rescale(run, "nav", 1000.0 ** (top - e),
                        "СЧА опубликована в тысячах, а соседние точки — в рублях; приведено к рублям")

    # 4. дробление/консолидация паёв: цена прыгает в целое число раз, СЧА — нет.
    #    Прошлые цены приводятся к текущему номиналу пая. Прочие большие движения — в аномалии.
    anomalies: list[dict] = []
    prices = [p for p in points if p.price is not None]
    runs, ratios = [[prices[0]]] if prices else [], []
    for prev, cur in zip(prices, prices[1:]):
        r = cur.price / prev.price
        if abs(r - 1) <= BIG_MOVE:
            runs[-1].append(cur)
            continue
        nav_r = _ratio(cur.nav, prev.nav)
        factor = next((k for k in SPLIT_FACTORS if _near(r, k)), None) or \
            next((1 / k for k in SPLIT_FACTORS if _near(r, 1 / k)), None)
        if factor and (nav_r is None or 0.8 < nav_r < 1.25):
            runs.append([cur])
            ratios.append(factor)
        else:
            runs[-1].append(cur)
            anomalies.append({"rules": rules, "date": cur.date.isoformat(), "field": "price",
                              "change_pct": round((r - 1) * 100, 2),
                              "nav_change_pct": round((nav_r - 1) * 100, 2) if nav_r else None,
                              "note": "большое движение цены пая, не исправлялось"})
    cum = 1.0
    for i in range(len(runs) - 2, -1, -1):
        cum *= ratios[i]
        rescale(runs[i], "price", cum,
                f"дробление/консолидация паёв {runs[i + 1][0].date.isoformat()}: цена изменилась в "
                f"{ratios[i]:g} раз при неизменной СЧА; прошлые цены приведены к новому номиналу")

    kept = [p for p in points if p.price is not None or p.nav is not None]
    freq = detect_frequency(kept)
    limit = GAP_DAYS.get(freq, 45)
    gaps = [{"from": a.date.isoformat(), "to": b.date.isoformat(), "days": (b.date - a.date).days}
            for a, b in zip(kept, kept[1:]) if (b.date - a.date).days > limit]
    return Normalized(kept, sources, log, anomalies, gaps, freq)
