"""HTTP-клиент: повторный запуск ничего не скачивает, капча — ошибка, манифест с SHA-256."""
import hashlib

import httpx
import pytest

from pif.http import Blocked, FetchError, Fetcher, Manifest


def make(tmp_path, handler, **kw):
    m = Manifest(tmp_path / "manifest.json")
    return Fetcher(m, raw_dir=tmp_path / "raw", transport=httpx.MockTransport(handler), **kw), m


def test_second_run_downloads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0)
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, content=b"data", headers={"content-type": "text/plain", "etag": '"1"'})

    f, m = make(tmp_path, handler)
    doc = f.get("https://example.ru/a", fmt="txt", funds=["123"])
    assert doc.sha256 == hashlib.sha256(b"data").hexdigest()
    m.save()
    # новый процесс, тот же манифест и кэш
    f2, _ = make(tmp_path, handler)
    doc2 = f2.get("https://example.ru/a", fmt="txt")
    assert doc2.from_cache and len(calls) == 1
    entry = Manifest(tmp_path / "manifest.json").entries[next(iter(m.entries))]
    assert entry["funds"] == ["123"] and entry["sha256"] == doc.sha256


def test_conditional_request_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0)
    seen = []

    def handler(req):
        seen.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == '"1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"data", headers={"etag": '"1"'})

    f, m = make(tmp_path, handler, fresh_hours=0)
    f.get("https://example.ru/a", fmt="txt")
    doc = f.get("https://example.ru/a", fmt="txt")
    assert seen == [None, '"1"'] and doc.from_cache and f.stats.unchanged == 1


def test_captcha_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0)
    f, _ = make(tmp_path, lambda req: httpx.Response(403, content=b"<html>SmartCaptcha</html>",
                                                     headers={"content-type": "text/html"}))
    with pytest.raises(Blocked):
        f.get("https://example.ru/a")


def test_down_site_returns_stale_copy(tmp_path, monkeypatch):
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("pif.http.time.sleep", lambda s: None)
    state = {"up": True}

    def handler(req):
        if state["up"]:
            return httpx.Response(200, content=b"old")
        raise httpx.ConnectError("down")

    f, _ = make(tmp_path, handler, fresh_hours=0)
    f.get("https://example.ru/a", fmt="txt")
    state["up"] = False
    doc = f.get("https://example.ru/a", fmt="txt")
    assert doc.stale and doc.content() == b"old"
    with pytest.raises(FetchError):
        f.get("https://example.ru/b", fmt="txt")


def test_antibot_stub_with_200_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("pif.config.MIN_INTERVAL_SEC", 0)
    stub = b"<html><head><script src='/servicepipe.js'></script></head><body>captcha</body></html>"
    f, _ = make(tmp_path, lambda req: httpx.Response(200, content=stub, headers={"content-type": "text/html"}))
    with pytest.raises(Blocked):
        f.get("https://example.ru/am")
