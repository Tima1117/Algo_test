"""ООО «МАКС-Жизнь» (makclife.ru) — страховщик с лицензией УК; один ОПИФ РФИ «МАКС-Жизнь Первый для
долевого страхования жизни» (правила 7662, сформирован 25.09.2026).

Источники:
* https://makclife.ru/o-kompanii/upravlyayushchaya-kompaniya.php — раскрытие УК, блок «Фонды под
  управлением»: ссылка https://makclife.ru/invest/opif/<id> с полным названием фонда. По названию в
  «» (norm_text) сопоставляем фонд реестра и id фонда на сайте;
* https://makclife.ru/invest/opif/<id> — страница фонда (одностраничное приложение на Vue: «Правила
  фонда №7662», график, «Таблица стоимости пая и СЧА»). Данные она берёт из JSON-API:
* https://makclife.ru/invest-pif/api/etf/v1/calculations?opifId=<id>&date_from=ГГГГ-ММ-ДД&
  date_to=ГГГГ-ММ-ДД&page=N&page_size=100 — {"state": "success", "items": [{date, rsip — расчётная
  стоимость пая, cha — СЧА, difRsip, difCha, …}], "pagination": {…, "has_next_page"}}; новые
  сверху; page_size не больше 100 (на 500 API отвечает 400). Каждый рабочий день с 25.09.2026.
  Запрашиваем с ctx.start_for() и листаем страницы. date_to обязателен (без него 422 «Необходимо
  заполнить «Date To»»), но дату в будущем API принимает: с date_to=2099-12-31 ответ байт в байт тот же,
  что с сегодняшней датой (проверено 06.11.2026) — поэтому граница фиксированная, адрес не меняется по дням. Единицы — как в таблице на странице:
  «Расчетная стоимость инвестиционного пая (РСИП), рубли», «Стоимость чистых активов (СЧА), рубли»
  (СЧА фонда действительно мала: 141 993,63 руб. на 02.11.2026 при паях по 1 051,80 руб.).

Официальная доходность: в документах фонда КИД нет, на странице только «Динамика стоимости пая и
СЧА» (за день, с начала года, за 1/2/3/5 лет), которую браузер считает из того же API; фонду меньше
двух месяцев, за 1 год — «—». official_returns пустой + предупреждение. Сообщений о выплате дохода нет — payouts пуст.
"""
from __future__ import annotations

import json
import re
from datetime import date

from bs4 import BeautifulSoup

from ..model import Fund, FundData, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require, require_keys
from .base import Adapter, Context

BASE = "https://makclife.ru"
UK_URL = f"{BASE}/o-kompanii/upravlyayushchaya-kompaniya.php"
API_URL = f"{BASE}/invest-pif/api/etf/v1/calculations"
FUND_URL = BASE + "/invest/opif/{id}"
PAGE_SIZE = 100
MAX_PAGES = 60          # 6000 рабочих дней — заведомо больше, чем бывает; защита от зацикливания
# Фиксированный конец диапазона вместо «сегодня»: API требует date_to, а с сегодняшней датой адрес менялся бы
# каждый день и повторный сбор качал бы ряд заново, хотя на сайте ничего не изменилось.
DATE_TO = "2099-12-31"

NO_RETURNS = ("официальная доходность за 1/3/6/12 мес УК не публикует: КИД в документах фонда нет, "
              "блок «Динамика» на странице считается браузером из ряда — не заполнена")

_OPIF_HREF = re.compile(r"^(?:https?://(?:www\.)?makclife\.ru)?/invest/opif/(\d+)/?$")
_QUOTED = re.compile(r"«(.+)»")


# ---------- разбор ----------

def parse_fund_links(content) -> dict[str, str]:
    """Страница УК -> {id фонда на сайте: название фонда в «»}."""
    s = BeautifulSoup(content.decode("utf-8") if isinstance(content, bytes) else content, "lxml")
    out: dict[str, str] = {}
    for a in s.find_all("a", href=True):
        m = _OPIF_HREF.match(a["href"].strip())
        if not m:
            continue
        t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        q = _QUOTED.search(t)
        if q:
            out[m.group(1)] = q.group(1).strip()
    require(bool(out), f"на {UK_URL} нет ссылок /invest/opif/<id> с названием фонда")
    return out


def parse_page(content) -> tuple[list[SeriesPoint], bool]:
    """Одна страница calculations -> (точки по возрастанию даты, есть ли следующая страница)."""
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except ValueError as e:
            raise UnknownFormat(f"ответ API не JSON: {e}") from e
    require_keys(content, ("state", "items", "pagination"), "calculations")
    require(content["state"] == "success", f"API: state={content['state']!r}, {content.get('error')}")
    require(isinstance(content["items"], list), "API: items не список")
    require_keys(content["pagination"], ("has_next_page",), "calculations.pagination")
    by_date: dict[date, SeriesPoint] = {}
    for it in content["items"]:
        require_keys(it, ("date", "rsip", "cha"), "строка calculations")
        d = day(it["date"])
        price, nav = num(it["rsip"]), num(it["cha"])
        price = price if price and price > 0 else None
        nav = nav if nav and nav > 0 else None
        if price is None and nav is None:
            continue
        p = SeriesPoint(d, nav, price)
        require(by_date.get(d, p) == p, f"две разные строки на {d}")
        by_date[d] = p
    return [by_date[d] for d in sorted(by_date)], bool(content["pagination"]["has_next_page"])


def match_fund(fund: Fund, links: dict[str, str]) -> str | None:
    want = norm_text(fund.name)
    hits = [i for i, name in links.items() if norm_text(name) == want]
    require(len(hits) <= 1, f"фонду «{fund.name}» соответствуют несколько страниц: {hits}")
    return hits[0] if hits else None


# ---------- адаптер ----------

class MakslifeAdapter(Adapter):
    key = "makslife"
    title = "МАКС-Жизнь"
    uk_inns = ("7724510200",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def links() -> dict[str, str]:
            if "links" not in state:
                state["links"] = parse_fund_links(ctx.http.get(UK_URL, fmt="html", funds=all_rules).content())
            return state["links"]

        def one(fund: Fund) -> FundData:
            opif_id = match_fund(fund, links())
            if opif_id is None:
                return FundData(fund.rules, status="not_found", page_url=UK_URL,
                                reason=f"на {UK_URL} нет фонда «{fund.name}»")
            page_url = FUND_URL.format(id=opif_id)
            start = ctx.start_for(fund.rules)
            segments = []
            for page in range(1, MAX_PAGES + 1):
                raw = ctx.http.get(API_URL, fmt="json", funds=[fund.rules], params={
                    "opifId": opif_id, "date_from": start.isoformat(), "date_to": DATE_TO,
                    "page": page, "page_size": PAGE_SIZE})
                points, more = parse_page(raw.content())
                segments.append(self.segment(raw, points, page_url=page_url, since=ctx.since))
                if not more:
                    break
            else:
                raise UnknownFormat(f"API отдаёт больше {MAX_PAGES} страниц по {PAGE_SIZE} строк")
            fd = FundData(fund.rules, currency="RUB", page_url=page_url, segments=segments)
            if not any(s.points for s in segments):
                fd.status = "not_formed" if fund.status == "Формируется" else "no_history"
                fd.reason = f"API УК не вернуло точек с {start:%d.%m.%Y}"
            fd.warnings.append(NO_RETURNS)
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]
