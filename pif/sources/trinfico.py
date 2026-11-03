"""АО «УК ТРИНФИКО» (www.trinfico-am.ru) — 5 открытых фондов.

Источник: страницы фондов на сайте УК — список https://www.trinfico-am.ru/opif/, карточки
https://www.trinfico-am.ru/opif/<slug>/ («Доходность, график стоимости, цены пая»; адреса
известны из поискового индекса сайта, например /opif/opif-rfi-lidar/).

Состояние на 2026-11-03: сайт отвечает «403 Forbidden» (text/plain, «Transaction ID: …»)
на любой адрес, включая главную и robots.txt, если в User-Agent стоит наш честный UA из
pif/config.py. Это фильтр на стороне сайта, а не сбой: тот же запрос без комментария в UA
проходит. Подмену UA мы не делаем, поэтому структура страниц не изучена и разборщика нет.

Что делает адаптер:
* один запрос к списку фондов (не долбим закрытый сайт пятью запросами);
* 403 / капча -> Blocked -> статус 'blocked' у всех фондов (ставит guarded());
  сеть недоступна -> 'unavailable';
* если сайт однажды откроется, адаптер НЕ выдаёт пустой «успешный» ряд, а бросает
  UnknownFormat -> 'parse_error': это сигнал дописать разбор по живой странице.
"""
from __future__ import annotations

from ..model import Fund, FundData
from ..parsing import UnknownFormat
from .base import Adapter, Context

BASE = "https://www.trinfico-am.ru"
LIST_URL = f"{BASE}/opif/"


def check_fund_list(content: bytes) -> None:
    """Страница списка фондов пришла с данными — разбор ещё не реализован (структура не изучена)."""
    size = len(content or b"")
    raise UnknownFormat(
        f"сайт УК отдал список фондов ({size} байт), но разбор не реализован: при разведке "
        "сайт закрывал доступ честному User-Agent, структура страниц не изучена")


class TrinficoAdapter(Adapter):
    key = "trinfico"
    title = "УК ТРИНФИКО"
    uk_inns = ("7701155020",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        rules = [f.rules for f in funds]
        state: dict = {}

        def fetch_list():
            # Один запрос на всю УК; исключение запоминаем и повторяем для каждого фонда,
            # чтобы guarded() поставил каждому свой статус с причиной.
            if "error" in state:
                raise state["error"]
            if "raw" not in state:
                try:
                    state["raw"] = ctx.http.get(LIST_URL, fmt="html", funds=rules)
                except Exception as e:  # noqa: BLE001 — статус определит guarded()
                    state["error"] = e
                    raise
            return state["raw"]

        def one(fund: Fund) -> FundData:
            raw = fetch_list()
            check_fund_list(raw.content())
            return FundData(fund.rules, page_url=LIST_URL)   # недостижимо, пока нет разбора

        out = []
        for fund in funds:
            fd = self.guarded(ctx, fund, lambda f=fund: one(f))
            fd.page_url = fd.page_url or LIST_URL
            if fd.status == "blocked":
                fd.reason += "; сайт фильтрует наш честный User-Agent, подмену UA не делаем"
            out.append(fd)
        return out
