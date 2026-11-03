"""УК ТРИНФИКО: сайт закрывает доступ честному User-Agent (403).

Проверяем всю цепочку без сети: настоящий ответ сайта (фикстура) отдаётся через
httpx.MockTransport настоящему Fetcher, который должен превратить 403 в Blocked,
а адаптер — в статус 'blocked' у каждого фонда, одним запросом на всю УК.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pif.http import Fetcher, Manifest
from pif.model import Fund
from pif.parsing import UnknownFormat
from pif.sources.base import Context
from pif.sources.trinfico import LIST_URL, TrinficoAdapter, check_fund_list

FIX = Path(__file__).parent / "fixtures" / "trinfico"

RULES = ["3863", "4344", "4275", "3264", "2984"]


def _fund(rules: str) -> Fund:
    return Fund(rules=rules, name=f"фонд {rules}", full_name=f"ОПИФ РФИ {rules}", type="open",
                category="", status="Сформирован", reg_date="2019-01-01", formation_end=None,
                uk_name="АО «УК ТРИНФИКО»", uk_short="АО «УК ТРИНФИКО»", uk_ogrn="1027700084730",
                uk_inn="7701155020", uk_site="https://www.trinfico-am.ru", exchange_tradable=False)


def _ctx(tmp_path: Path, handler, monkeypatch) -> tuple[Context, list[str]]:
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    http = Fetcher(Manifest(tmp_path / "manifest.json"), raw_dir=tmp_path / "raw")
    client = httpx.Client(transport=httpx.MockTransport(wrapped), follow_redirects=True)
    monkeypatch.setattr(http, "_client", lambda domain: client)
    ctx = Context(http=http, since=date(2025, 9, 1), log=logging.getLogger("test.trinfico"))
    return ctx, seen


def test_real_403_gives_blocked_for_every_fund_with_one_request(tmp_path, monkeypatch):
    body = (FIX / "opif_403.txt").read_bytes()
    assert body.startswith(b"Forbidden")
    ctx, seen = _ctx(tmp_path, lambda r: httpx.Response(403, content=body,
                                                        headers={"content-type": "text/plain"}),
                     monkeypatch)
    result = TrinficoAdapter().collect(ctx, [_fund(r) for r in RULES])

    assert [fd.rules for fd in result] == RULES
    for fd in result:
        assert fd.status == "blocked"
        assert "403" in fd.reason and "User-Agent" in fd.reason
        assert fd.segments == [] and fd.official_returns == []
        assert fd.page_url == LIST_URL
    assert seen == [LIST_URL]          # закрытый сайт не долбим запросом на каждый фонд


def test_network_error_gives_unavailable(tmp_path, monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr("pif.http.time.sleep", lambda s: None)   # без пауз между повторами
    ctx, seen = _ctx(tmp_path, down, monkeypatch)
    result = TrinficoAdapter().collect(ctx, [_fund(r) for r in RULES[:2]])
    assert [fd.status for fd in result] == ["unavailable", "unavailable"]
    assert len(seen) == 3              # повторы делает Fetcher, адаптер второй раз не идёт


def test_open_site_is_parse_error_not_silent_empty_series(tmp_path, monkeypatch):
    # Если сайт откроется, пустой «успешный» ряд недопустим: нужен разбор по живой странице.
    with pytest.raises(UnknownFormat):
        check_fund_list(b"<html><body>fund list</body></html>")
    ctx, _ = _ctx(tmp_path, lambda r: httpx.Response(200, content=b"<html></html>",
                                                     headers={"content-type": "text/html"}),
                  monkeypatch)
    result = TrinficoAdapter().collect(ctx, [_fund("3863")])
    assert result[0].status == "parse_error"
    assert result[0].errors
