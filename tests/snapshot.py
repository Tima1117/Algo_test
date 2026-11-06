"""Снимки рядов для тестов разбора: число точек, первая и последняя точка, SHA-256 всего ряда.

Проверки отдельных значений ловят порчу только в проверяемых точках. assert_series сверяет весь ряд
со снимком tests/snapshots/<name>.json, поэтому испорченная точка в середине тоже роняет тест.

    assert_series(points, "alfa_opif_akop")

Создать или обновить снимки: UPDATE_SNAPSHOTS=1 python -m pytest tests/...
Без переменной отсутствующий снимок — ошибка теста (снимок не создаётся молча).

Точки: SeriesPoint или любой объект с атрибутами date/nav/price; dict с ключами date/nav/price;
кортеж (date, nav, price) или (date, value). Вместо списка можно передать словарь {дата: значение}
(точки берутся по возрастанию даты).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_seen: dict[str, str] = {}   # имя снимка -> хэш в текущем прогоне: одно имя для двух разных рядов — ошибка


def _update_mode() -> bool:
    return os.environ.get("UPDATE_SNAPSHOTS", "").strip().lower() in ("1", "true", "yes")


def _norm_date(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        return v
    raise TypeError(f"assert_series: дата точки должна быть date/datetime/str, получено {type(v).__name__}: {v!r}")


def _norm_num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        raise TypeError(f"assert_series: значение точки — bool ({v!r}), ожидалось число или None")
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return repr(f)               # NaN/inf в JSON не пишутся — сохраняем строкой, чтобы их было видно
        return round(f, 10) + 0.0        # + 0.0: -0.0 -> 0.0
    raise TypeError(f"assert_series: значение точки должно быть числом или None, получено {type(v).__name__}: {v!r}")


def _norm_point(p, i: int) -> dict:
    if isinstance(p, Mapping):
        if "date" not in p:
            raise TypeError(f"assert_series: точка #{i} — dict без ключа 'date': {p!r}")
        if "value" in p and "nav" not in p and "price" not in p:
            return {"date": _norm_date(p["date"]), "value": _norm_num(p["value"])}
        return {"date": _norm_date(p["date"]), "nav": _norm_num(p.get("nav")), "price": _norm_num(p.get("price"))}
    if isinstance(p, (tuple, list)):
        if len(p) == 3:
            return {"date": _norm_date(p[0]), "nav": _norm_num(p[1]), "price": _norm_num(p[2])}
        if len(p) == 2:
            return {"date": _norm_date(p[0]), "value": _norm_num(p[1])}
        raise TypeError(f"assert_series: точка #{i} — кортеж из {len(p)} элементов, ожидалось (date, nav, price) "
                        f"или (date, value): {p!r}")
    if hasattr(p, "date") and (hasattr(p, "nav") or hasattr(p, "price")):
        return {"date": _norm_date(p.date), "nav": _norm_num(getattr(p, "nav", None)),
                "price": _norm_num(getattr(p, "price", None))}
    raise TypeError(f"assert_series: точка #{i} не похожа на точку ряда (нужны date и nav/price): {p!r}")


def series_summary(points) -> dict:
    """Канонический отпечаток ряда: число точек, первая/последняя точка и SHA-256 всех точек по порядку."""
    if isinstance(points, Mapping):
        points = sorted(points.items())
    norm = [_norm_point(p, i) for i, p in enumerate(points)]
    canon = json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "count": len(norm),
        "first": norm[0] if norm else None,
        "last": norm[-1] if norm else None,
        "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def assert_series(points, name: str) -> None:
    """Сверить ряд со снимком tests/snapshots/<name>.json (при UPDATE_SNAPSHOTS=1 — записать снимок)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"assert_series: недопустимое имя снимка {name!r} (латиница, цифры, _ . -)")
    got = series_summary(points)
    prev = _seen.get(name)
    if prev is not None and prev != got["sha256"]:
        raise AssertionError(f"снимок {name!r}: в одном прогоне это имя использовано для двух разных рядов — "
                             f"дайте каждому ряду своё имя")
    _seen[name] = got["sha256"]

    path = SNAPSHOT_DIR / f"{name}.json"
    if _update_mode():
        _write(path, {"name": name, **got})
        return
    if not path.exists():
        raise AssertionError(f"нет снимка ряда {name!r} ({path.relative_to(SNAPSHOT_DIR.parent.parent)}). "
                             f"Создайте его: UPDATE_SNAPSHOTS=1 python -m pytest <этот тест>")
    want = json.loads(path.read_text(encoding="utf-8"))

    problems = []
    if want.get("count") != got["count"]:
        problems.append(f"число точек: в снимке {want.get('count')}, сейчас {got['count']}")
    for key, label in (("first", "первая точка"), ("last", "последняя точка")):
        if want.get(key) != got[key]:
            problems.append(f"{label}: в снимке {want.get(key)}, сейчас {got[key]}")
    if want.get("sha256") != got["sha256"]:
        hint = "" if problems else " (число точек и крайние точки совпали — изменилась точка в середине ряда)"
        problems.append(f"SHA-256 ряда: в снимке {want.get('sha256')}, сейчас {got['sha256']}{hint}")
    if problems:
        raise AssertionError(f"ряд {name!r} не совпал со снимком {path.name}:\n  - " + "\n  - ".join(problems)
                             + "\nЕсли изменение ожидаемое — обновите снимок: UPDATE_SNAPSHOTS=1 python -m pytest ...")
