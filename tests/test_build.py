"""Сборка целиком: детерминированность, сходимость сводов, блокировка публикации ошибками разбора."""
import json
from datetime import date, timedelta

import pytest

from pif import build, config


def fund(rules, inn, typ="open", cat="Фонд рыночных финансовых инструментов"):
    return {"rules": rules, "name": f"Фонд {rules}", "full_name": f"ОПИФ «Фонд {rules}»", "type": typ,
            "category": cat, "status": "Сформирован", "reg_date": "2020-01-01", "formation_end": "2020-02-01",
            "uk_name": f"УК {inn}", "uk_short": f"УК {inn}", "uk_ogrn": "1" * 13, "uk_inn": inn,
            "uk_site": "https://uk.example", "exchange_tradable": False, "isin": None, "ticker": None}


def collected(rules, units_by_month, errors=()):
    """Ежедневный ряд: цена растёт на 0,05% в день, число паёв меняется помесячно — приток известен заранее."""
    pts, d, price = [], date(2026, 1, 1), 100.0
    while d <= date(2026, 6, 30):
        if d.weekday() < 5:
            units = units_by_month[d.month]
            pts.append({"date": d.isoformat(), "nav": round(units * price, 2), "price": round(price, 6)})
        price *= 1.0005
        d += timedelta(days=1)
    seg = {"raw_url": f"https://uk.example/{rules}.json", "sha256": "0" * 64, "fetched_at": "2026-07-01T00:00:00+00:00",
           "points": pts, "page_url": "https://uk.example/fund", "stale": False}
    return {"rules": rules, "currency": "RUB", "segments": [seg], "official_returns": [], "payouts": [],
            "page_url": "https://uk.example/fund", "status": "ok", "reason": "", "errors": list(errors), "warnings": []}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(build, "SITE_DIR", tmp_path / "site")
    monkeypatch.setattr(build, "SERIES_DIR", tmp_path / "series")
    monkeypatch.setattr(build, "MANUAL_CHECK", tmp_path / "manual_check.csv")
    (tmp_path / "collected").mkdir()
    funds = [fund("A", "111"), fund("B", "222", typ="exchange")]
    (tmp_path / "registry.json").write_text(json.dumps({"meta": {"fetched_at": "2026-07-01"}, "funds": funds}), "utf-8")
    return tmp_path


def write(data_dir, rec):
    (data_dir / "collected" / f"{rec['rules']}.json").write_text(json.dumps(rec), "utf-8")


def snapshot(d):
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()
            and "collected" not in p.parts}


def test_build_is_deterministic_and_sums_match(data_dir):
    write(data_dir, collected("A", {1: 100, 2: 100, 3: 120, 4: 120, 5: 110, 6: 110}))
    write(data_dir, collected("B", {1: 50, 2: 60, 3: 60, 4: 60, 5: 60, 6: 60}))
    assert build.build_all() is True
    first = snapshot(data_dir)
    assert build.build_all() is True
    assert snapshot(data_dir) == first                       # повторная сборка — те же байты

    q = json.loads((data_dir / "site" / "quality.json").read_text("utf-8"))
    assert q["last_month"] == "2026-05" and all(c["ok"] for c in q["consistency"])
    funds = {f["rules"]: f for f in json.loads((data_dir / "site" / "funds.json").read_text("utf-8"))}
    # в мае фонд A погасил 10 паёв по цене конца мая
    a = json.loads((data_dir / "series" / "A.json").read_text("utf-8"))
    may = next(r for r in a["monthly"] if r["month"] == "2026-05")
    assert funds["A"]["flow_m"] == pytest.approx(-10 * may["price"], abs=0.05)   # СЧА округлена до копеек
    assert funds["B"]["flow_m"] == pytest.approx(0, abs=1)


def test_parse_error_blocks_publication(data_dir):
    write(data_dir, collected("A", {m: 100 for m in range(1, 7)}))
    write(data_dir, collected("B", {m: 100 for m in range(1, 7)}, errors=["разбор: нет колонки «СЧА»"]))
    assert build.build_all() is False
    q = json.loads((data_dir / "site" / "quality.json").read_text("utf-8"))
    assert q["publish_allowed"] is False and q["errors"][0]["rules"] == "B"
