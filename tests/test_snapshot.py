"""Помощник снимков рядов: порча точки в середине ряда должна ронять тест."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

import snapshot
from pif.model import SeriesPoint
from snapshot import assert_series, series_summary


@pytest.fixture
def snapdir(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(snapshot, "_seen", {})
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)
    return tmp_path


def _series(n: int = 50) -> list[SeriesPoint]:
    d0 = date(2026, 1, 1)
    return [SeriesPoint(d0 + timedelta(days=i), 1_000_000.0 + i * 1234.56, 100.0 + i * 0.01) for i in range(n)]


def _record(points, name, monkeypatch):
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")
    assert_series(points, name)
    monkeypatch.delenv("UPDATE_SNAPSHOTS")
    snapshot._seen.clear()


def test_unchanged_series_passes(snapdir, monkeypatch):
    pts = _series()
    _record(pts, "demo", monkeypatch)
    snap = json.loads((snapdir / "demo.json").read_text(encoding="utf-8"))
    assert snap["count"] == 50
    assert snap["first"] == {"date": "2026-01-01", "nav": 1000000.0, "price": 100.0}
    assert snap["last"]["date"] == "2026-02-19"
    assert_series(_series(), "demo")          # тот же ряд, заново построенный, — проходит


def test_corrupted_middle_point_is_caught(snapdir, monkeypatch):
    pts = _series()
    _record(pts, "demo", monkeypatch)
    bad = list(pts)
    bad[25] = replace(bad[25], price=bad[25].price + 0.01)   # одна точка в середине, края не тронуты
    assert series_summary(bad)["count"] == series_summary(pts)["count"]
    assert series_summary(bad)["first"] == series_summary(pts)["first"]
    assert series_summary(bad)["last"] == series_summary(pts)["last"]
    with pytest.raises(AssertionError, match="SHA-256") as e:
        assert_series(bad, "demo")
    assert "в середине ряда" in str(e.value)


@pytest.mark.parametrize("mutate, what", [
    (lambda p: p[:-1], "число точек"),
    (lambda p: [replace(p[0], nav=None)] + p[1:], "первая точка"),
    (lambda p: p[:-1] + [replace(p[-1], date=p[-1].date + timedelta(days=1))], "последняя точка"),
    (lambda p: p[:10] + [replace(p[10], nav=None)] + p[11:], "SHA-256"),
    (lambda p: p[:10] + [p[11], p[10]] + p[12:], "SHA-256"),             # порядок точек тоже важен
])
def test_mismatch_message_names_the_difference(snapdir, monkeypatch, mutate, what):
    pts = _series()
    _record(pts, "demo", monkeypatch)
    with pytest.raises(AssertionError, match=what):
        assert_series(mutate(pts), "demo")


def test_missing_snapshot_fails_without_update(snapdir):
    with pytest.raises(AssertionError, match="нет снимка.*UPDATE_SNAPSHOTS=1"):
        assert_series(_series(), "absent")
    assert not (snapdir / "absent.json").exists()


def test_update_overwrites_snapshot(snapdir, monkeypatch):
    _record(_series(50), "demo", monkeypatch)
    _record(_series(40), "demo", monkeypatch)
    assert json.loads((snapdir / "demo.json").read_text(encoding="utf-8"))["count"] == 40
    assert_series(_series(40), "demo")


def test_point_shapes_are_equivalent():
    pts = _series(5)
    as_dicts = [{"date": p.date, "nav": p.nav, "price": p.price} for p in pts]
    as_tuples = [(p.date, p.nav, p.price) for p in pts]
    assert series_summary(pts) == series_summary(as_dicts) == series_summary(as_tuples)
    # {дата: значение} == [(дата, значение)] по возрастанию даты
    mapping = {p.date: p.price for p in reversed(pts)}
    assert series_summary(mapping) == series_summary([(p.date, p.price) for p in pts])


def test_rounding_and_none():
    d = date(2026, 1, 1)
    # шум за 10-м знаком не меняет хэш, int и float одного значения равны, None -> null
    assert series_summary([(d, 1.0, 2)]) == series_summary([(d, 1.00000000000001, 2.0)])
    assert series_summary([(d, None, 2.0)])["first"] == {"date": "2026-01-01", "nav": None, "price": 2.0}
    assert series_summary([(d, 1.0, 2.0)])["sha256"] != series_summary([(d, 1.0, 2.0000000001)])["sha256"]


def test_same_name_for_two_series_in_one_run_fails(snapdir, monkeypatch):
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")
    assert_series(_series(5), "dup")
    with pytest.raises(AssertionError, match="двух разных рядов"):
        assert_series(_series(6), "dup")


def test_bad_name_rejected(snapdir):
    with pytest.raises(ValueError):
        assert_series(_series(1), "../x")
