"""АО «СОЛИД Менеджмент» (solid-mn.ru): история СЧА и пая, официальная доходность.

Сайт — Nuxt-приложение, данные страниц он берёт из собственного JSON-API (тот же ответ
вшит в HTML страницы фонда как __NUXT_DATA__). Берём API напрямую:

- POST https://solid-mn.ru/api/v1/public/products/ — каталог продуктов (страница «Все продукты»
  /services/): code, название, описание с «Правила доверительного управления № …»;
- POST https://solid-mn.ru/api/v1/public/productDetail/<code>/ — карточка фонда
  (страница https://solid-mn.ru/services/<code>/):
  * product.paiPrice.data.items.pai / .sha — ВСЯ история фонда с даты формирования, по рабочим
    дням: OperDate (дд.мм.гггг), Price — расчётная стоимость пая, Value — СЧА, оба в рублях.
    Диапазон дат API не принимает (график фильтрует период на клиенте), поэтому качаем целиком
    (до ~7,5 МБ на фонд) и отрезаем по ctx.since;
  * product.profitPeriods.list — доходность «за 1 месяц / 3 / 6 месяцев / 1 год», которую УК
    показывает на странице фонда; дата — paramsBlock.updateDate («Обновлено», = дата последней цены).
    У БПИФ с выплатами дохода УК считает её с учётом выплат (сверено с рядом выплат payouts).
    Берём значения сервера (они же в каталоге и меню сайта); заголовок графика на странице БПИФ
    браузер пересчитывает сам от соседней базовой даты, там бывает расхождение ~0,1 п.п.

Сопоставление: по номеру правил из описания в каталоге (есть у всех фондов УК), запасной путь —
точное совпадение названия (norm_text); номер правил на странице фонда перепроверяется.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://solid-mn.ru"
CATALOG_URL = f"{BASE}/api/v1/public/products/"
DETAIL_URL = BASE + "/api/v1/public/productDetail/{code}/"
PAGE_URL = BASE + "/services/{code}/"

# Ключи периодов на сайте -> наши коды. «1 день», «3 года», «5 лет», «весь период» не берём.
PERIODS = {"1_month": "1m", "3_month": "3m", "6_month": "6m", "1_year": "1y"}
CURRENCY_SIGNS = {"₽": "RUB", "руб": "RUB", "$": "USD", "€": "EUR", "¥": "CNY"}
STALE_DAYS = 10   # последняя цена старше — предупреждение «УК давно не обновляла»

_RULES_RE = re.compile(r"Правила\s+доверительного\s+управления\s*№\s*(\d[\d\-]*\d|\d)", re.I)


# ---------- разбор ----------
def _load(content) -> dict:
    if isinstance(content, dict):
        return content
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        return json.loads(content)
    except ValueError as e:
        raise UnknownFormat(f"ответ API не JSON: {str(content)[:80]!r}") from e


def _dig(obj, path: tuple[str, ...], where: str):
    """obj[k1][k2]… с понятной ошибкой, на каком ключе сломалась структура."""
    cur = obj
    for i, k in enumerate(path):
        if not isinstance(cur, dict) or k not in cur:
            raise UnknownFormat(f"{where}: нет {'.'.join(path[:i + 1])}")
        cur = cur[k]
    return cur


def _workarea(doc: dict, where: str) -> dict:
    require_keys(doc, ["status", "data"], where)
    require(doc["status"] == "success", f"{where}: status={doc['status']!r}")
    return _dig(doc, ("data", "result", "fields", "workarea", "items"), where)


def _product(content) -> dict:
    return _dig(_workarea(_load(content), "productDetail"), ("items", "product"), "productDetail")


def norm_rules(s) -> str:
    """'№ 0040-52697973' / '0040 - 52697973' -> '0040-52697973'."""
    return re.sub(r"[\s №]", "", str(s or "")).lower()


def rules_in(text) -> str | None:
    m = _RULES_RE.search(html.unescape(str(text or "")))
    return m.group(1) if m else None


def parse_catalog(content) -> list[dict]:
    """Каталог продуктов -> [{code, title, rules, pai}] (rules=None у не-фондов: ДУ, СФО и т. п.)."""
    raw = _dig(_workarea(_load(content), "products"), ("items", "items"), "products")
    require(isinstance(raw, list) and raw, "products: пустой или не список items.items")
    out = []
    for i, it in enumerate(raw):
        require_keys(it, ["code", "title", "description"], f"products[{i}]")
        require(bool(it["code"]), f"products[{i}]: пустой code")
        out.append({"code": it["code"], "title": it["title"], "rules": rules_in(it["description"]),
                    "pai": it.get("pai")})
    require(any(c["rules"] for c in out), "products: ни у одного продукта нет номера правил ДУ")
    return out


def match_fund(fund: Fund, catalog: list[dict]) -> dict | None:
    """По номеру правил; если в каталоге номера нет — по точному совпадению norm_text названия."""
    want = norm_rules(fund.rules)
    for c in catalog:
        if c["rules"] and norm_rules(c["rules"]) == want:
            return c
    name = norm_text(fund.name)
    hits = [c for c in catalog if not c["rules"] and norm_text(c["title"]) == name]
    return hits[0] if len(hits) == 1 else None


def currency_of(pai_text) -> str | None:
    """'992.19₽' -> 'RUB'; без знака валюты -> None; незнакомый знак -> UnknownFormat."""
    s = str(pai_text or "").strip()
    if not s:
        return None
    for sign, code in CURRENCY_SIGNS.items():
        if s.endswith(sign) or s.startswith(sign):
            return code
    if re.fullmatch(r"[\d\s.,]+", s):
        return None
    raise UnknownFormat(f"незнакомая валюта пая в каталоге: {s!r}")


def _positive(value, where: str) -> float | None:
    """Сайт пишет '.00', когда значения нет (СЧА в период формирования) — это None, не 0."""
    v = num(value)
    if v is None or v == 0:
        return None
    require(v > 0, f"{where}: отрицательное значение {value!r}")
    return v


def _rows(items: dict, key: str, field: str) -> dict[date, float | None]:
    rows = items[key]
    require(isinstance(rows, list), f"paiPrice.{key}: ожидался список")
    out: dict[date, float | None] = {}
    fund_ids = set()
    for i, r in enumerate(rows):
        require_keys(r, ["OperDate", field], f"paiPrice.{key}[{i}]")
        d = day(r["OperDate"])
        v = _positive(r[field], f"paiPrice.{key}[{i}].{field}")
        if d in out and out[d] != v:
            raise UnknownFormat(f"paiPrice.{key}: два разных значения на {d}: {out[d]} и {v}")
        out[d] = v
        fund_ids.add(r.get("FundID"))
    require(len(fund_ids) <= 1, f"paiPrice.{key}: в ряду несколько фондов {sorted(map(str, fund_ids))}")
    return out


def parse_history(content) -> list[SeriesPoint]:
    """Карточка фонда -> ряд (дата, СЧА руб., пай руб.), по возрастанию дат.

    Цена — из списка pai (Price), СЧА — из списка sha (Value), как их рисует график сайта.
    """
    items = _dig(_product(content), ("paiPrice", "data", "items"), "productDetail.product")
    require_keys(items, ["pai", "sha"], "paiPrice.data.items")
    prices = _rows(items, "pai", "Price")
    navs = _rows(items, "sha", "Value")
    points = [SeriesPoint(d, navs.get(d), prices.get(d)) for d in sorted(prices.keys() | navs.keys())]
    return [p for p in points if p.nav is not None or p.price is not None]


def parse_payouts(content) -> list[date]:
    """Даты выплат дохода пайщикам (есть только у БПИФ «Денежный поток»), иначе []."""
    items = _dig(_product(content), ("paiPrice", "data", "items"), "productDetail.product")
    rows = items.get("payouts") or []
    require(isinstance(rows, list), "paiPrice.payouts: ожидался список")
    out = []
    for i, r in enumerate(rows):
        require_keys(r, ["OperDate", "Payment"], f"paiPrice.payouts[{i}]")
        if (num(r["Payment"]) or 0) > 0:
            out.append(day(r["OperDate"]))
    return out


def parse_returns(content, source_url: str) -> list[OfficialReturn]:
    """Доходность за 1/3/6/12 мес. со страницы фонда на дату «Обновлено»."""
    product = _product(content)
    periods = _dig(product, ("profitPeriods", "list"), "productDetail.product")
    upd = _dig(product, ("paramsBlock", "items", "updateDate"), "productDetail.product")
    require_keys(upd, ["value"], "paramsBlock.items.updateDate")
    as_of = day(upd["value"]) if upd["value"] else None
    if isinstance(periods, list):          # на случай, если API отдаст список вместо словаря
        periods = {p.get("code"): p for p in periods if isinstance(p, dict)}
    require(isinstance(periods, dict), "profitPeriods.list: ожидался словарь периодов")
    out = []
    for code, period in PERIODS.items():
        if code not in periods:            # молодой фонд: длинных периодов может не быть
            continue
        require_keys(periods[code], ["percent"], f"profitPeriods.list.{code}")
        v = pct(periods[code]["percent"])
        if v is not None:
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def detail_rules(content) -> str | None:
    """Номер правил из блока описания на странице фонда (для перепроверки сопоставления)."""
    return rules_in(" ".join(_strings(_product(content).get("info"))))


# ---------- адаптер ----------
class SolidAdapter(Adapter):
    key = "solid"
    title = "СОЛИД Менеджмент"
    uk_inns = ("7706150949",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def catalog() -> list[dict]:
            # Один запрос каталога на всю УК; ошибку запоминаем и отдаём каждому фонду,
            # чтобы guarded() поставил ему статус с причиной.
            if "error" in state:
                raise state["error"]
            if "items" not in state:
                try:
                    raw = ctx.http.get(CATALOG_URL, method="POST", fmt="json", funds=all_rules)
                    state["items"] = parse_catalog(raw.content())
                except Exception as e:  # noqa: BLE001 — статус определит guarded()
                    state["error"] = e
                    raise
            return state["items"]

        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, catalog())) for f in funds]

    def _one(self, ctx: Context, fund: Fund, catalog: list[dict]) -> FundData:
        item = match_fund(fund, catalog)
        if item is None:
            return FundData(fund.rules, page_url=f"{BASE}/services/", status="not_found",
                            reason=f"в каталоге сайта УК нет фонда с правилами № {fund.rules}")
        page = PAGE_URL.format(code=item["code"])
        raw = ctx.http.get(DETAIL_URL.format(code=item["code"]), method="POST", fmt="json",
                           funds=[fund.rules])
        content = _load(raw.content())   # до ~7,5 МБ: разбираем JSON один раз
        shown = detail_rules(content)
        require(shown is None or norm_rules(shown) == norm_rules(fund.rules),
                f"на странице {page} правила № {shown}, а ищем № {fund.rules}")

        points = parse_history(content)
        fd = FundData(fund.rules, currency=currency_of(item["pai"]) or "RUB", page_url=page,
                      official_returns=parse_returns(content, page))
        if not points:
            fd.status, fd.reason = "no_history", "на странице фонда нет ряда цены пая и СЧА"
            return fd
        seg = self.segment(raw, points, page_url=page, since=ctx.since)
        last = points[-1].date
        if not seg.points:
            fd.status = "partial"
            fd.reason = f"последнее значение на сайте УК — {last:%d.%m.%Y}, раньше начала периода"
            return fd
        fd.segments.append(seg)
        if all(p.nav is None for p in seg.points):
            fd.status, fd.reason = "partial", "УК публикует только цену пая, СЧА в ряду нет"
        no_nav = [p.date for p in seg.points if p.nav is None]
        if no_nav and fd.status == "ok":
            fd.warnings.append("СЧА не опубликована на " + ", ".join(f"{d:%d.%m.%Y}" for d in no_nav[:3])
                               + (f" и ещё {len(no_nav) - 3} дат" if len(no_nav) > 3 else "")
                               + " (цена пая есть; у нового фонда — период формирования)")
        if (date.today() - last).days > STALE_DAYS:
            fd.warnings.append(f"последнее значение на сайте УК — {last:%d.%m.%Y}")
        if parse_payouts(content):
            fd.warnings.append("официальная доходность УК — с учётом выплат дохода пайщикам; "
                               "расчётная стоимость пая в ряду — без выплат")
        return fd
