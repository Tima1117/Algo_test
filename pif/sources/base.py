"""Базовый класс источника данных одной управляющей компании.

Как добавить УК:
1. Создать модуль pif/sources/<ключ>.py с классом-наследником Adapter.
2. Указать uk_inns (ИНН УК из реестра лицензий ЦБ) — по нему фонды попадают в адаптер.
3. Реализовать collect(): для каждого фонда вернуть FundData — сегменты ряда с источниками,
   официальную доходность (если есть) и статус с причиной, если данных нет.
4. Разбор каждого формата — отдельная чистая функция parse_*(bytes) -> list[SeriesPoint],
   которая на незнакомых данных бросает UnknownFormat. На неё — тест на настоящей фикстуре
   в tests/fixtures/<ключ>/.
Модули подхватываются автоматически (pif/sources/__init__.py).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..http import Blocked, FetchError, Fetcher, RawDoc
from ..model import Fund, FundData, Segment, SeriesPoint
from ..parsing import UnknownFormat


@dataclass
class Context:
    http: Fetcher
    since: date
    log: logging.Logger


class Adapter:
    key: str = ""
    title: str = ""
    uk_inns: tuple[str, ...] = ()

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        raise NotImplementedError

    # ---- помощники для наследников ----
    @staticmethod
    def segment(raw: RawDoc, points: list[SeriesPoint], page_url: str | None = None,
                since: date | None = None) -> Segment:
        if since:
            points = [p for p in points if p.date >= since]
        return Segment(raw_url=raw.url, sha256=raw.sha256, fetched_at=raw.fetched_at,
                       points=points, page_url=page_url, stale=raw.stale)

    @staticmethod
    def guarded(ctx: Context, fund: Fund, fn: Callable[[], FundData]) -> FundData:
        """Один сломанный фонд не роняет остальные: ошибка превращается в статус."""
        try:
            fd = fn()
        except Blocked as e:
            return FundData(fund.rules, status="blocked", reason=e.reason)
        except FetchError as e:
            return FundData(fund.rules, status="unavailable", reason=e.reason)
        except UnknownFormat as e:
            ctx.log.error("[%s] %s: незнакомый формат: %s", fund.uk_short, fund.rules, e)
            return FundData(fund.rules, status="parse_error", reason=f"незнакомый формат: {e}",
                            errors=[f"разбор: {e}"])
        except Exception as e:  # noqa: BLE001 — любая ошибка кода тоже не должна ронять сбор
            ctx.log.exception("[%s] %s: сбой сборщика", fund.uk_short, fund.rules)
            return FundData(fund.rules, status="parse_error", reason=f"сбой сборщика: {type(e).__name__}",
                            errors=[f"сбой: {type(e).__name__}: {e}"])
        if any(s.stale for s in fd.segments) and fd.status == "ok":
            fd.status = "stale"
            fd.reason = "сайт УК не ответил, использована последняя сохранённая копия"
        return fd
