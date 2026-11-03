"""Вежливый HTTP-клиент со своим кэшем и манифестом источников.

* не чаще одного запроса в секунду к домену, честный User-Agent;
* каждый ответ сохраняется в raw/ и записывается в манифест (адрес, дата, формат, SHA-256);
* свежий ответ (моложе FRESH_HOURS) повторно не скачивается; старый перепроверяется
  условным запросом (ETag / Last-Modified);
* если сайт не отвечает, можно вернуть последнюю сохранённую копию с пометкой stale;
* капчи и антибот-заглушки не обходим: такой ответ — ошибка Blocked.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from . import config

log = logging.getLogger("pif.http")

CAPTCHA_MARKERS = (
    "captcha", "smartcaptcha", "ddos-guard", "qrator", "cf-chl", "__cf_chl",
    "checking your browser", "проверка браузера", "i'm not a robot", "я не робот",
)


class FetchError(Exception):
    """Источник не отдал данные. reason — человекочитаемая причина для статуса фонда."""

    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason
        self.status = status


class Blocked(FetchError):
    """Капча, антибот или закрытый раздел. Не обходим."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass
class RawDoc:
    url: str
    path: Path
    sha256: str
    fetched_at: str
    fmt: str
    from_cache: bool = False
    stale: bool = False
    stale_reason: str = ""

    def content(self) -> bytes:
        return self.path.read_bytes()

    def text(self, encoding: str | None = None) -> str:
        data = self.content()
        if encoding:
            return data.decode(encoding)
        for enc in ("utf-8", "cp1251"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text())


class Manifest:
    """Манифест источников: data/manifest.json (+ manifest.csv для людей)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        if path.exists():
            for e in json.loads(path.read_text("utf-8")):
                self.entries[e["key"]] = e

    def get(self, key: str) -> dict | None:
        with self.lock:
            e = self.entries.get(key)
            return dict(e) if e else None

    def put(self, entry: dict) -> None:
        with self.lock:
            old = self.entries.get(entry["key"], {})
            funds = sorted(set(old.get("funds", [])) | set(entry.get("funds", [])))
            self.entries[entry["key"]] = {**old, **entry, "funds": funds}

    def add_funds(self, key: str, funds: list[str]) -> None:
        with self.lock:
            e = self.entries.get(key)
            if e is not None:
                e["funds"] = sorted(set(e.get("funds", [])) | set(funds))

    def save(self) -> None:
        """Сохранить, слив с тем, что на диске (параллельные прогоны по разным УК не затирают друг друга)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            on_disk = {}
            if self.path.exists():
                on_disk = {e["key"]: e for e in json.loads(self.path.read_text("utf-8"))}
            with self.lock:
                merged = {**on_disk, **self.entries}
                rows = sorted(merged.values(), key=lambda e: (e["domain"], e["url"], e["key"]))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", "utf-8")
            tmp.replace(self.path)
            self._write_csv(rows)

    def _write_csv(self, rows: list[dict]) -> None:
        with open(self.path.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["funds", "url", "format", "fetched_at", "sha256", "bytes", "status"])
            for e in rows:
                w.writerow([" ".join(e.get("funds", [])), e["url"], e["fmt"], e.get("fetched_at", ""),
                            e.get("sha256", ""), e.get("bytes", ""), e.get("status", "")])


@dataclass
class Stats:
    downloaded: int = 0
    unchanged: int = 0
    cached: int = 0
    errors: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, name: str) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)


class Fetcher:
    def __init__(self, manifest: Manifest, raw_dir: Path = config.RAW_DIR, offline: bool = False,
                 fresh_hours: float = config.FRESH_HOURS, proxies: dict[str, str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.manifest = manifest
        self.raw_dir = raw_dir
        self.offline = offline
        self.fresh = timedelta(hours=fresh_hours)
        self.stats = Stats()
        self._domain_locks: dict[str, threading.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._guard = threading.Lock()
        self.proxies = proxies or {}
        self._clients: dict[tuple, httpx.Client] = {}
        self._transport = transport   # для тестов: httpx.MockTransport
        self._extra_ca: dict[str, str] = {}   # домен -> дополнительный корневой сертификат (PEM)

    # ---------- внутреннее ----------
    def trust_extra_ca(self, domain: str, pem: str) -> None:
        """Доверять дополнительному корневому сертификату — только для одного домена.

        Нужно для сайтов с сертификатом российского НУЦ Минцифры, которого нет в стандартных
        хранилищах. Проверка TLS не отключается: к системным корням добавляется ровно один.
        """
        with self._guard:
            self._extra_ca[domain] = pem

    def _client(self, domain: str) -> httpx.Client:
        proxy = self.proxies.get(domain)
        ca = self._extra_ca.get(domain)
        key = (proxy, domain if ca else None)
        with self._guard:
            if key not in self._clients:
                kw: dict = {"transport": self._transport} if self._transport else {"proxy": proxy}
                if ca and not self._transport:
                    ctx = ssl.create_default_context()
                    ctx.load_verify_locations(cadata=ca)
                    kw["verify"] = ctx
                self._clients[key] = httpx.Client(
                    headers={"User-Agent": config.USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
                    timeout=config.TIMEOUT_SEC, follow_redirects=True, **kw)
            return self._clients[key]

    def _domain_lock(self, domain: str) -> threading.Lock:
        with self._guard:
            return self._domain_locks.setdefault(domain, threading.Lock())

    def _wait_turn(self, domain: str) -> None:
        last = self._last_hit.get(domain, 0.0)
        delay = config.MIN_INTERVAL_SEC - (time.monotonic() - last)
        if delay > 0:
            time.sleep(delay)
        self._last_hit[domain] = time.monotonic()

    @staticmethod
    def make_key(method: str, url: str, params: dict | None, body: bytes | None) -> str:
        q = urlencode(sorted((params or {}).items()), doseq=True)
        h = hashlib.sha1(f"{method} {url}?{q}".encode())
        if body:
            h.update(body)
        return h.hexdigest()[:20]

    # ---------- API ----------
    def get(self, url: str, *, params: dict | None = None, fmt: str = "html",
            funds: list[str] | None = None, method: str = "GET", json_body=None,
            data: dict | None = None, headers: dict | None = None,
            fresh_hours: float | None = None, allow_stale: bool = True) -> RawDoc:
        """Скачать (или взять из кэша) документ. Возвращает RawDoc с путём к сырому файлу."""
        body = None
        if json_body is not None:
            body = json.dumps(json_body, sort_keys=True, ensure_ascii=False).encode()
        elif data is not None:
            body = urlencode(sorted(data.items())).encode()
        key = self.make_key(method, url, params, body)
        domain = urlsplit(url).hostname or "unknown"
        path = self.raw_dir / domain / f"{key}.{fmt}"
        entry = self.manifest.get(key)
        fresh = self.fresh if fresh_hours is None else timedelta(hours=fresh_hours)
        full_url = url + ("?" + urlencode(params, doseq=True) if params else "")

        def cached(stale_reason: str = "") -> RawDoc:
            if funds:
                self.manifest.add_funds(key, funds)
            return RawDoc(full_url, path, entry["sha256"], entry["fetched_at"], fmt,
                          from_cache=True, stale=bool(stale_reason), stale_reason=stale_reason)

        have_copy = entry is not None and path.exists() and entry.get("sha256")
        if have_copy:
            age = _now() - datetime.fromisoformat(entry["fetched_at"])
            if self.offline or age < fresh:
                self.stats.inc("cached")
                return cached()
        if self.offline:
            raise FetchError(full_url, "нет сохранённой копии (офлайн-режим)")

        req_headers = dict(headers or {})
        if have_copy:
            if entry.get("etag"):
                req_headers["If-None-Match"] = entry["etag"]
            if entry.get("last_modified"):
                req_headers["If-Modified-Since"] = entry["last_modified"]
        if json_body is not None:
            req_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        base = {"key": key, "url": full_url, "domain": domain, "fmt": fmt, "funds": funds or [],
                "method": method, "path": str(path.relative_to(self.raw_dir))}
        try:
            resp = self._request(domain, method, url, params, body, req_headers)
        except FetchError as err:
            self.stats.inc("errors")
            self.manifest.put({**base, "status": f"error: {err.reason}", "checked_at": _now().isoformat()})
            if have_copy and allow_stale:
                log.warning("%s — отдаю сохранённую копию от %s", err, entry["fetched_at"])
                return cached(err.reason)
            raise

        now = _now().isoformat()
        if resp.status_code == 304 and have_copy:
            self.stats.inc("unchanged")
            self.manifest.put({**base, "fetched_at": now, "status": "ok (304, не изменился)", "http_status": 304})
            entry["fetched_at"] = now
            return cached()

        content = resp.content
        sha = hashlib.sha256(content).hexdigest()
        unchanged = have_copy and sha == entry.get("sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not unchanged:
            path.write_bytes(content)
        self.stats.inc("unchanged" if unchanged else "downloaded")
        self.manifest.put({**base, "fetched_at": now, "sha256": sha, "bytes": len(content),
                           "http_status": resp.status_code, "etag": resp.headers.get("etag"),
                           "last_modified": resp.headers.get("last-modified"),
                           "content_type": resp.headers.get("content-type"),
                           "status": "ok (не изменился)" if unchanged else "ok"})
        return RawDoc(full_url, path, sha, now, fmt)

    def _request(self, domain, method, url, params, body, headers) -> httpx.Response:
        attempts = 3
        for attempt in range(1, attempts + 1):
            with self._domain_lock(domain):
                self._wait_turn(domain)
                try:
                    resp = self._client(domain).request(method, url, params=params, content=body,
                                                        headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError) as e:
                    reason = "сайт не отвечает" if isinstance(e, httpx.TimeoutException) else "сайт недоступен"
                    if attempt == attempts:
                        raise FetchError(url, f"{reason} ({type(e).__name__})") from e
                    time.sleep(2 * attempt)
                    continue
            if resp.status_code in (500, 502, 503, 504) and attempt < attempts:
                time.sleep(2 * attempt)
                continue
            break
        head = resp.content[:20000].decode("utf-8", errors="ignore").lower()
        if resp.status_code in (401, 403, 429) or (
                "text/html" in resp.headers.get("content-type", "") and any(m in head for m in CAPTCHA_MARKERS)
                and resp.status_code != 200):
            raise Blocked(url, f"доступ закрыт сайтом (HTTP {resp.status_code})", resp.status_code)
        if resp.status_code == 304:
            return resp
        if resp.status_code >= 400:
            raise FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
        # антибот может отдать заглушку и с кодом 200 (например, ServicePipe на bcs.ru)
        if "text/html" in resp.headers.get("content-type", "") and (
                any(m in head for m in ("smartcaptcha", "ddos-guard", "qrator", "__cf_chl", "servicepipe"))
                or (len(resp.content) < 20000 and "captcha" in head)):
            raise Blocked(url, "антибот-проверка вместо данных", resp.status_code)
        return resp

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
