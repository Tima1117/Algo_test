"""Нормализация: выбросы, дробление паёв, масштаб СЧА, пропуски."""
import pytest

from pif.normalize import normalize


def seg(points, fetched="2026-11-01T00:00:00+00:00", url="u"):
    return {"raw_url": url, "sha256": url, "fetched_at": fetched,
            "points": [{"date": d, "nav": n, "price": p} for d, n, p in points]}


def test_split_rescales_history_and_is_logged():
    n = normalize("X", [seg([("2026-01-01", 1000.0, 100.0), ("2026-01-02", 1001.0, 100.1),
                             ("2026-01-03", 1002.0, 10.02), ("2026-01-04", 1003.0, 10.03)])])
    prices = [p.price for p in n.points]
    assert prices == pytest.approx([10.0, 10.01, 10.02, 10.03])
    assert any(c["action"] == "rescaled" and c["field"] == "price" for c in n.corrections)
    assert not n.anomalies


def test_nav_in_thousands_fixed():
    n = normalize("X", [seg([("2026-01-01", 1_000_000.0, 10.0), ("2026-01-02", 1_001.0, 10.01),
                             ("2026-01-03", 1_002.0, 10.02)])])
    assert [p.nav for p in n.points] == pytest.approx([1_000_000.0, 1_001_000.0, 1_002_000.0])
    assert any(c["field"] == "nav" and c["action"] == "rescaled" for c in n.corrections)


def test_single_spike_dropped():
    n = normalize("X", [seg([("2026-01-01", 100.0, 10.0), ("2026-01-02", 100.0, 99.0),
                             ("2026-01-03", 100.0, 10.1)])])
    assert n.points[1].price is None
    assert n.corrections[0]["action"] == "dropped"


def test_real_big_move_is_anomaly_not_fixed():
    n = normalize("X", [seg([("2026-01-01", 100.0, 10.0), ("2026-01-02", 60.0, 6.0),
                             ("2026-01-03", 61.0, 6.1)])])
    assert [p.price for p in n.points] == [10.0, 6.0, 6.1]
    assert n.anomalies and n.anomalies[0]["date"] == "2026-01-02"


def test_newer_source_wins_and_conflict_logged():
    old = seg([("2026-01-01", 100.0, 10.0)], fetched="2026-10-01T00:00:00+00:00", url="old")
    new = seg([("2026-01-01", 101.0, 10.0)], fetched="2026-11-01T00:00:00+00:00", url="new")
    n = normalize("X", [new, old])
    assert n.points[0].nav == 101.0
    assert n.sources[n.points[0].src]["url"] == "new"
    assert n.corrections[0]["action"] == "replaced"


def test_gaps_are_reported_not_filled():
    pts = [(f"2026-01-{d:02d}", 100.0, 10.0) for d in range(1, 10)] + \
          [(f"2026-03-{d:02d}", 100.0, 10.0) for d in range(1, 10)]
    n = normalize("X", [seg(pts)])
    assert n.frequency == "daily"
    assert len(n.points) == 18
    assert n.gaps and n.gaps[0]["from"] == "2026-01-09"
