"""ООО СК «Сбербанк страхование жизни» (sberbank-insurance.ru) — страховщик с лицензией УК;
два ОПИФ РФИ для долевого страхования жизни: «Первый долевой» и «Пульс рынка».

Где данные (найдено по скрипту /newFront/dist/js/financeDoc.js страницы раскрытия):
* страница для человека — https://sberbank-insurance.ru/about/info, блок «Паевые инвестиционные
  фонды»: у каждого фонда таблица «Дата расчета | Стоимость пая | СЧА | Изменение …» по годам.
  Таблицу рисует JS из двух JSON того же домена:
* {BASE}/disclosure/get-portfolios — список «портфелей»: id и название
  («ОПИФ рыночных финансовых инструментов «Первый долевой»»). Кроме двух витринных фондов там
  есть служебные копии «…»-2» (id 3, 4: те же числа за 14.01–09.02.2026, поле vitrina пустое)
  и «Фонд Сбалансированный» — их сопоставление по полному названию отсекает;
* {BASE}/disclosure/get-params — вся история по всем портфелям: {portfolio_id, "2025": […],
  "2026": […]}; запись — quotesDate (дата расчёта), quote (пай), nav (СЧА), currency.
  Ключ года — год ПУБЛИКАЦИИ, а не расчёта (значение за 30.12.2025 лежит в "2026"), поэтому
  точки собираем по quotesDate из всех лет. Частота — каждый рабочий день, с даты завершения
  формирования (10.04.2025 и 23.09.2025). Диапазон дат API не принимает — ряд всегда целиком.
  СЧА — в рублях (8 940 705 379,88 руб. на 30.09.2026 в КИД «Первого долевого» = nav API),
  пай — в рублях (currency = "RUB").

Сопоставление: полное название портфеля после norm_text должно совпасть с названием фонда в
реестре (norm_text). Номера правил на сайте нет. Ровно одно совпадение, иначе not_found /
UnknownFormat.

Официальная доходность: на HTML-странице её нет. 1/3/6/12 мес есть только в ежемесячном КИД
(PDF в {BASE}/front-api/disclosure → «Документы фонда»), но у этих PDF нет текстового слоя
(цифры доходности нарисованы, pdftotext/pypdf достают только пару строк) — без OCR не прочитать.
Поэтому official_returns пустой + предупреждение со ссылкой на последний КИД.
Сообщений о выплате дохода пайщикам в разделе фондов нет — payouts не заполняем.
"""
from __future__ import annotations

import json
import re
from datetime import date

from ..model import Fund, FundData, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require, require_keys
from .base import Adapter, Context

BASE = "https://sberbank-insurance.ru"
PORTFOLIOS_URL = f"{BASE}/disclosure/get-portfolios"
PARAMS_URL = f"{BASE}/disclosure/get-params"
DOCS_URL = f"{BASE}/front-api/disclosure"
PAGE_URL = f"{BASE}/about/info"

_KID = re.compile(r"\bКИД\b")


def _json(content):
    if isinstance(content, (bytes, str)):
        try:
            return json.loads(content)
        except ValueError as e:
            raise UnknownFormat(f"ответ не JSON: {e}") from e
    return content


def _ok_data(obj, where: str) -> list:
    require_keys(obj, ("success", "data"), where)
    require(obj["success"] is True, f"{where}: success={obj['success']!r}")
    require(isinstance(obj["data"], list), f"{where}: data не список")
    return obj["data"]


# ---------- разбор ----------

def parse_portfolios(content) -> dict[int, str]:
    """get-portfolios -> {id портфеля: название}."""
    out: dict[int, str] = {}
    for item in _ok_data(_json(content), "get-portfolios"):
        require_keys(item, ("id", "name"), "портфель get-portfolios")
        require(isinstance(item["id"], int), f"id портфеля не число: {item['id']!r}")
        out[item["id"]] = str(item["name"])
    require(bool(out), "get-portfolios: пустой список портфелей")
    return out


def parse_params(content) -> dict[int, tuple[str, list[SeriesPoint]]]:
    """get-params -> {id портфеля: (валюта, точки по возрастанию даты)}.

    Одна и та же дата в двух записях допустима только с теми же числами, иначе UnknownFormat.
    """
    out: dict[int, tuple[str, list[SeriesPoint]]] = {}
    for block in _ok_data(_json(content), "get-params"):
        require_keys(block, ("portfolio_id",), "блок get-params")
        pid = block["portfolio_id"]
        years = [k for k in block if k != "portfolio_id"]
        require(all(re.fullmatch(r"\d{4}", k) for k in years),
                f"портфель {pid}: неожиданные ключи {years} (ждали годы)")
        by_date: dict[date, SeriesPoint] = {}
        currencies: set[str] = set()
        for y in years:
            require(isinstance(block[y], list), f"портфель {pid}, {y}: не список")
            for e in block[y]:
                require_keys(e, ("quotesDate", "quote", "nav", "currency"), f"запись портфеля {pid}")
                d = day(e["quotesDate"])
                nav, price = num(e["nav"]), num(e["quote"])
                nav = nav if nav and nav > 0 else None
                price = price if price and price > 0 else None
                if nav is None and price is None:
                    continue
                currencies.add(str(e["currency"]).upper())
                p = SeriesPoint(d, nav, price)
                old = by_date.get(d)
                require(old is None or old == p, f"портфель {pid}: две разные записи на {d}: {old} и {p}")
                by_date[d] = p
        require(len(currencies) <= 1, f"портфель {pid}: несколько валют {sorted(currencies)}")
        ccy = currencies.pop() if currencies else "RUB"
        require(ccy in ("RUB", "USD", "EUR", "CNY"), f"портфель {pid}: незнакомая валюта {ccy!r}")
        require(pid not in out, f"портфель {pid} встречается в get-params дважды")
        out[pid] = (ccy, [by_date[d] for d in sorted(by_date)])
    require(bool(out), "get-params: нет ни одного портфеля")
    return out


def parse_kid_links(content) -> dict[str, list[tuple[int, str, str]]]:
    """front-api/disclosure -> {название фонда: [(uploaded_at, заголовок КИД, ссылка), …] свежие первыми}.

    Берём раздел «Паевые инвестиционные фонды» (id 2): в нём по фонду, у фонда вкладки с документами.
    """
    data = _json(content)
    require(isinstance(data, list), "front-api/disclosure: ожидался список разделов")
    out: dict[str, list[tuple[int, str, str]]] = {}
    for section in data:
        require_keys(section, ("id", "children"), "раздел раскрытия")
        if section["id"] != 2:
            continue
        for child in section["children"] or []:
            require_keys(child, ("title", "tabs"), "фонд в разделе ПИФ")
            kids = []
            for tab in child["tabs"] or []:
                for doc in tab.get("documents") or []:
                    require_keys(doc, ("title", "file"), "документ фонда")
                    if _KID.search(doc["title"]):
                        kids.append((int(doc.get("uploaded_at") or 0), doc["title"].strip(), doc["file"]))
            out[child["title"].strip()] = sorted(kids, reverse=True)
    return out


def match_portfolio(fund: Fund, portfolios: dict[int, str]) -> int | None:
    """id портфеля, чьё полное название после norm_text совпадает с названием фонда в реестре."""
    want = norm_text(fund.name)
    hits = [pid for pid, name in portfolios.items() if norm_text(name) == want]
    require(len(hits) <= 1, f"фонду «{fund.name}» соответствуют несколько портфелей: {hits}")
    return hits[0] if hits else None


# ---------- адаптер ----------

class SberlifeAdapter(Adapter):
    key = "sberlife"
    title = "СК «Сбербанк страхование жизни»"
    uk_inns = ("7744002123",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        memo: dict[str, tuple[str, object]] = {}

        def once(key: str, fn):
            # Общие для УК документы качаем один раз; ошибку запоминаем, чтобы guarded()
            # поставил её каждому фонду.
            if key not in memo:
                try:
                    memo[key] = ("ok", fn())
                except Exception as e:  # noqa: BLE001
                    memo[key] = ("err", e)
            kind, val = memo[key]
            if kind == "err":
                raise val
            return val

        def portfolios():
            return parse_portfolios(ctx.http.get(PORTFOLIOS_URL, fmt="json", funds=all_rules).content())

        def params():
            raw = ctx.http.get(PARAMS_URL, fmt="json", funds=all_rules)
            return raw, parse_params(raw.content())

        def kid_links():
            return parse_kid_links(ctx.http.get(DOCS_URL, fmt="json", funds=all_rules).content())

        def one(fund: Fund) -> FundData:
            pid = match_portfolio(fund, once("portfolios", portfolios))
            if pid is None:
                return FundData(fund.rules, status="not_found", page_url=PAGE_URL,
                                reason=f"в {PORTFOLIOS_URL} нет фонда «{fund.name}»")
            raw, series = once("params", params)
            if pid not in series:
                return FundData(fund.rules, status="no_history", page_url=PAGE_URL,
                                reason=f"фонд есть на сайте (портфель {pid}), но в {PARAMS_URL} нет его значений")
            ccy, points = series[pid]
            fd = FundData(fund.rules, currency=ccy, page_url=PAGE_URL,
                          segments=[self.segment(raw, points, page_url=PAGE_URL, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status = "no_history"
                fd.reason = f"в {PARAMS_URL} нет точек начиная с {ctx.since:%d.%m.%Y}"
            fd.warnings.append(self._returns_warning(ctx, fund, once, kid_links))
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]

    @staticmethod
    def _returns_warning(ctx: Context, fund: Fund, once, kid_links) -> str:
        text = ("официальная доходность за 1/3/6/12 мес публикуется только в ежемесячном КИД (PDF без "
                "текстового слоя — без OCR не читается), поэтому не заполнена")
        try:
            links = once("kids", kid_links)
        except Exception as e:  # noqa: BLE001 — список документов нужен только для подсказки
            ctx.log.warning("[sberlife] %s: список документов: %s", fund.rules, e)
            return text
        want = norm_text(fund.name)
        for title, kids in links.items():
            if want and want in norm_text(title) and kids:
                return f"{text}; последний КИД фонда: {kids[0][2]}"
        return text
