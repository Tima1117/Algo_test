"""УК «Альфа-Капитал» (ИНН 7728142469): история СЧА и пая, официальная доходность.

Источники (сайт УК www.alfacapital.ru):
- каталог фондов раздела «Раскрытие информации → ПИФы»: https://www.alfacapital.ru/disclosure/pifs —
  HTML, данные страницы лежат JSON-ом в window.__SERVER_STATE__ (disclosureFunds: slug, полное
  название, тип). Номер правил сайт не показывает, поэтому фонд сопоставляем по типу и названию.
- история: https://www.alfacapital.ru/gate/v1/fundDisclosureRegData/v1?slug=<slug>&page=<n> — JSON,
  тот же ряд, что в таблице «Стоимость пая и СЧА» на https://www.alfacapital.ru/disclosure/pifs/<slug>/cost
  (кнопка «Показать ещё»). 60 строк на страницу, от новых к старым. Частота — как публикует УК:
  открытые и биржевые фонды — каждый рабочий день, интервальные — конец месяца и даты интервалов.
  СЧА и цена пая — в валюте пая (поле currency), СЧА в единицах валюты с копейками.
- официальная доходность: https://www.alfacapital.ru/gate/v1/fundsData/v1 — JSON витрины фондов,
  share.yields.{oneMonth,threeMonths,sixMonths,oneYear} = {from, to, value в %}. Те же числа
  показаны в блоке «Динамика фонда» на странице фонда /individual/pifs|bpifs/<slug>; подпись даты
  там — последний рабочий день, но значения посчитаны на конец прошлого месяца (поле to) — проверено
  по ряду пая.

Чего не берём и почему:
- JSON-RPC /api (getFundNav/getFundShare — график на странице фонда): закрыт robots.txt
  (Disallow: /api); к тому же заполняет выходные копией пятницы и отдаёт 0 за ещё не рассчитанный день.
- КИД (PDF с доходностью за 1/3/6/12 мес.) лежат под /disclosure/file/ — тоже Disallow в robots.txt.
  Поэтому у фондов, которых нет в витрине (авторские, интервальные, часть биржевых), официальной
  доходности нет — это видно в warnings фонда.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.alfacapital.ru"
CATALOG_URL = f"{BASE}/disclosure/pifs"
NAV_URL = f"{BASE}/gate/v1/fundDisclosureRegData/v1"
YIELDS_URL = f"{BASE}/gate/v1/fundsData/v1"

# Предохранитель листания: 60 страниц по 60 строк — больше 14 лет ежедневных публикаций.
MAX_PAGES = 60

# Тип фонда на сайте -> тип в реестре.
SITE_TYPES = {"opened": "open", "exchangeTraded": "exchange", "interval": "interval"}
# Раздел витрины, где у фонда есть страница для человека.
PRODUCT_SECTIONS = {"open": "pifs", "exchange": "bpifs"}
PERIODS = {"oneMonth": "1m", "threeMonths": "3m", "sixMonths": "6m", "oneYear": "1y"}
CURRENCIES = {"RUB", "USD", "EUR", "CNY"}


@dataclass(frozen=True)
class SiteFund:
    slug: str
    name: str      # полное название на сайте: «ОПИФ Альфа-Капитал Облигации Плюс»
    type: str      # open / exchange / interval
    fund_id: int


def cost_page(slug: str) -> str:
    """Страница «Стоимость пая и СЧА» фонда — там человек видит те же точки ряда."""
    return f"{BASE}/disclosure/pifs/{slug}/cost"


def product_page(site: SiteFund) -> str | None:
    section = PRODUCT_SECTIONS.get(site.type)
    return f"{BASE}/individual/{section}/{site.slug}" if section else None


def _json(content):
    if isinstance(content, (dict, list)):
        return content
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"ответ не JSON: {e}") from e


# ---------------- каталог фондов ----------------

def _server_state(content) -> dict:
    text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
    marker = "window.__SERVER_STATE__"
    i = text.find(marker)
    require(i >= 0, "на странице раскрытия нет window.__SERVER_STATE__")
    j = text.find("=", i + len(marker))
    require(j >= 0, "window.__SERVER_STATE__ без присваивания")
    try:
        state, _ = json.JSONDecoder().raw_decode(text[j + 1:].lstrip())
    except json.JSONDecodeError as e:
        raise UnknownFormat(f"window.__SERVER_STATE__ — не JSON: {e}") from e
    require(isinstance(state, dict), "window.__SERVER_STATE__ — не объект")
    return state


def parse_catalog(content) -> list[SiteFund]:
    """Список фондов раздела «Раскрытие информации → ПИФы» (открытые, биржевые, интервальные)."""
    state = _server_state(content)
    require_keys(state, ["disclosureFunds"], "__SERVER_STATE__")
    groups = state["disclosureFunds"]
    require_keys(groups, ["fundsOpened"], "disclosureFunds")
    groups = groups["fundsOpened"]
    require(isinstance(groups, list) and groups, "disclosureFunds.fundsOpened пуст или не список")
    out: list[SiteFund] = []
    for g in groups:
        require_keys(g, ["fundType", "subCategories"], "группа фондов каталога")
        require(isinstance(g["subCategories"], list), "subCategories — не список")
        for sub in g["subCategories"]:
            require_keys(sub, ["funds"], "подкатегория каталога")
            for f in sub["funds"]:
                require_keys(f, ["fundId", "fullName", "slug", "fundType"], "фонд каталога")
                kind = SITE_TYPES.get(f["fundType"])
                if kind is None:      # закрытые и прочие нерозничные типы нам не нужны
                    continue
                require(bool(f["slug"]) and bool(f["fullName"]), f"фонд каталога без slug/названия: {f}")
                out.append(SiteFund(str(f["slug"]), str(f["fullName"]), kind, int(f["fundId"])))
    require(bool(out), "в каталоге нет ни одного открытого/биржевого/интервального фонда")
    return out


def match_fund(fund: Fund, catalog: list[SiteFund]) -> tuple[SiteFund | None, str]:
    """Фонд реестра -> фонд сайта: точное совпадение нормализованного названия и типа."""
    key = norm_text(fund.name)
    same_name = [s for s in catalog if norm_text(s.name) == key]
    same = [s for s in same_name if s.type == fund.type]
    if len(same) == 1:
        return same[0], ""
    if len(same) > 1:
        return None, f"на сайте УК несколько фондов с названием «{fund.name}»: {[s.slug for s in same]}"
    if same_name:
        other = ", ".join(f"{s.name} ({s.type})" for s in same_name)
        return None, f"на сайте УК фонд с таким названием другого типа: {other}"
    return None, "фонд не найден в разделе «Раскрытие информации → ПИФы» на сайте УК"


# ---------------- ряд СЧА и пая ----------------

def _nav_rows(content) -> list[dict]:
    data = _json(content)
    require_keys(data, ["data"], "ответ fundDisclosureRegData")
    rows = data["data"]
    require(isinstance(rows, list), "fundDisclosureRegData.data — не список")
    for r in rows:
        require_keys(r, ["date", "shareCost", "netAssetValue", "currency"], "строка СЧА/пая")
    return rows


def _value(v, what: str) -> float | None:
    x = num(v)
    if x is None:
        return None
    if x < 0:
        raise UnknownFormat(f"{what} отрицательное: {v!r}")
    # ноль вместо значения — это «нет значения», а не настоящий ноль
    return x or None


def parse_nav_page(content) -> list[SeriesPoint]:
    """Одна страница таблицы «Стоимость пая и СЧА» -> точки по возрастанию даты."""
    points: list[SeriesPoint] = []
    seen: set[date] = set()
    for r in _nav_rows(content):
        d = day(r["date"])
        require(d not in seen, f"дата {d} повторяется на одной странице")
        seen.add(d)
        nav = _value(r["netAssetValue"], "СЧА")
        price = _value(r["shareCost"], "стоимость пая")
        if nav is None and price is None:
            continue
        points.append(SeriesPoint(d, nav, price))
    return sorted(points, key=lambda p: p.date)


def parse_nav_currency(content) -> str | None:
    """Валюта пая на странице ряда (одна на всю страницу) или None, если страница пуста."""
    found = {r["currency"] for r in _nav_rows(content)}
    unknown = found - CURRENCIES
    require(not unknown, f"незнакомая валюта пая: {sorted(map(str, unknown))}")
    require(len(found) <= 1, f"на одной странице несколько валют: {sorted(found)}")
    return next(iter(found), None)


# ---------------- официальная доходность ----------------

def parse_returns(content, source_url: str) -> dict[str, list[OfficialReturn]]:
    """Витрина фондов -> {slug: доходности пая за 1/3/6/12 мес. на конец прошлого месяца}."""
    data = _json(content)
    require_keys(data, ["data"], "ответ fundsData")
    require_keys(data["data"], ["products"], "fundsData.data")
    products = data["data"]["products"]
    require(isinstance(products, list) and products, "fundsData.data.products пуст или не список")
    out: dict[str, list[OfficialReturn]] = {}
    for p in products:
        require_keys(p, ["slug", "share"], "продукт витрины")
        require_keys(p["share"], ["yields"], f"{p['slug']}: share")
        yields = p["share"]["yields"]
        require_keys(yields, list(PERIODS), f"{p['slug']}: share.yields")
        rets = []
        for key, period in PERIODS.items():
            y = yields[key]
            if y is None:          # фонд моложе периода — УК значение не публикует
                continue
            require_keys(y, ["from", "to", "value"], f"{p['slug']}: доходность {key}")
            value = pct(y["value"])
            if value is None:
                continue
            rets.append(OfficialReturn(period, value, day(y["to"]), source_url))
        out[str(p["slug"])] = rets
    return out


# ---------------- адаптер ----------------

class AlfaCapitalAdapter(Adapter):
    key = "alfa"
    title = "Альфа-Капитал"
    uk_inns = ("7728142469",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        self._shared: dict[str, tuple] = {}
        self._all_rules = [f.rules for f in funds]
        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f)) for f in funds]

    def _once(self, key: str, load):
        """Общий документ (каталог, витрина) качаем один раз за прогон; ошибку тоже запоминаем,
        чтобы недоступный сайт не дёргать заново для каждого фонда."""
        if key not in self._shared:
            try:
                self._shared[key] = (load(), None)
            except Exception as e:  # noqa: BLE001 — превратится в статус в guarded()
                self._shared[key] = (None, e)
        value, err = self._shared[key]
        if err is not None:
            raise err
        return value

    def _catalog(self, ctx: Context) -> list[SiteFund]:
        raw = ctx.http.get(CATALOG_URL, fmt="html", funds=self._all_rules)
        return parse_catalog(raw.content())

    def _yields(self, ctx: Context) -> dict[str, list[OfficialReturn]]:
        raw = ctx.http.get(YIELDS_URL, fmt="json", funds=self._all_rules)
        return parse_returns(raw.content(), raw.url)

    def _one(self, ctx: Context, fund: Fund) -> FundData:
        site, why = match_fund(fund, self._once("catalog", lambda: self._catalog(ctx)))
        if site is None:
            return FundData(fund.rules, status="not_found", reason=why)
        fd = FundData(fund.rules, page_url=cost_page(site.slug))
        oldest_published = self._history(ctx, fund, site, fd)
        self._judge(ctx, fund, fd, oldest_published)
        self._returns(ctx, fund, site, fd)
        return fd

    def _history(self, ctx: Context, fund: Fund, site: SiteFund, fd: FundData) -> date | None:
        """Листает таблицу раскрытия от новых строк к старым до начала периода.

        Диапазон дат источник не принимает, а страницы сдвигаются с каждой новой публикацией.
        Поэтому всегда листаем до ctx.since (а не до ctx.start_for): иначе при слиянии сегментов по
        адресу страницы точки, уехавшие с нулевой страницы на первую, потерялись бы. Если нулевая
        страница скачана заново, остальные тоже перекачиваем (fresh_hours=0), чтобы не сшить
        вчерашнюю страницу 1 с сегодняшней страницей 0 (между ними выпала бы строка).
        """
        seen: set[date] = set()
        oldest: date | None = None
        currency: str | None = None
        refresh = False
        for page in range(MAX_PAGES):
            raw = ctx.http.get(NAV_URL, params={"slug": site.slug, "page": page}, fmt="json",
                               funds=[fund.rules], fresh_hours=0 if refresh else None)
            if page == 0:
                refresh = not raw.from_cache
            data = _json(raw.content())
            rows = _nav_rows(data)
            if not rows:
                break
            page_currency = parse_nav_currency(data)
            if currency and page_currency != currency:
                raise UnknownFormat(f"валюта пая сменилась между страницами: {currency} -> {page_currency}")
            currency = fd.currency = page_currency
            points = parse_nav_page(data)
            fresh = [p for p in points if p.date not in seen]
            seen.update(p.date for p in points)
            page_oldest = min(day(r["date"]) for r in rows)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
            seg = self.segment(raw, fresh, page_url=cost_page(site.slug), since=ctx.since)
            if seg.points:
                fd.segments.append(seg)
            if page_oldest < ctx.since:
                break
        else:
            fd.warnings.append(f"история длиннее {MAX_PAGES} страниц, дальше не листали")
        return oldest

    def _judge(self, ctx: Context, fund: Fund, fd: FundData, oldest: date | None) -> None:
        """Статус по собранному ряду: полный, короче публикуемого, пустой."""
        dates = sorted(p.date for s in fd.segments for p in s.points)
        if oldest is None:
            fd.status, fd.reason = "no_history", "в разделе раскрытия фонда нет ни одной публикации СЧА и пая"
            return
        if not dates:
            fd.status = "partial"
            fd.reason = f"после {ctx.since:%d.%m.%Y} УК не публиковала СЧА и пай (последняя публикация раньше)"
            return
        if not any(p.nav is not None for s in fd.segments for p in s.points):
            fd.status, fd.reason = "partial", "УК публикует только стоимость пая, без СЧА"
        elif not any(p.price is not None for s in fd.segments for p in s.points):
            fd.status, fd.reason = "partial", "УК публикует только СЧА, без стоимости пая"
        first = dates[0]
        # Публикации до начала периода есть — значит, история покрывает его целиком (у интервальных
        # фондов первая точка периода может прийтись на конец месяца — это частота, а не обрыв).
        if oldest >= ctx.since and first > ctx.since + timedelta(days=15):
            started = date.fromisoformat(fund.formation_end or fund.reg_date)
            if first <= max(started, ctx.since) + timedelta(days=15):
                # фонд моложе периода: ряд начинается с первой публикации — это полная история
                fd.reason = fd.reason or (f"фонд сформирован позже начала периода: первая публикация УК — "
                                          f"{first:%d.%m.%Y}")
            elif fd.status == "ok":
                fd.status = "partial"
                fd.reason = f"УК публикует историю только с {first:%d.%m.%Y}"
        lag = (date.today() - dates[-1]).days
        if lag > (45 if fund.type == "interval" else 10):
            fd.warnings.append(f"последняя публикация УК — {dates[-1]:%d.%m.%Y}, {lag} дн. назад")

    def _returns(self, ctx: Context, fund: Fund, site: SiteFund, fd: FundData) -> None:
        try:
            by_slug = self._once("yields", lambda: self._yields(ctx))
        except FetchError as e:
            fd.warnings.append(f"официальная доходность не получена: {e.reason}")
            return
        except UnknownFormat as e:
            fd.errors.append(f"разбор официальной доходности: {e}")
            return
        rets = by_slug.get(site.slug)
        if rets is None:
            fd.warnings.append("УК не публикует доходность за 1/3/6/12 мес. на странице фонда; она есть только "
                               "в КИД (PDF в разделе раскрытия, путь закрыт robots.txt) — не собрана")
            return
        fd.page_url = product_page(site) or fd.page_url
        if not rets:
            fd.warnings.append("УК ещё не публикует доходность за 1/3/6/12 мес.: фонд моложе месяца")
            return
        if fd.currency != "RUB":
            # Витрина считает доходность в рублях (пай пересчитан по курсу ЦБ) — с рядом в валюте пая
            # её сравнивать нельзя.
            shown = ", ".join(f"{r.period} {r.value_pct:+.2f}%" for r in rets)
            fd.warnings.append(f"доходность на сайте УК рублёвая, а пай в {fd.currency} — для сверки не "
                               f"используем ({shown} на {rets[0].as_of:%d.%m.%Y})")
            return
        fd.official_returns = rets
        if "с выплатой дохода" in norm_text(fund.name):
            # Проверено по ряду: у фондов с выплатой дохода УК считает доходность с учётом выплат
            # пайщикам, поэтому рост расчётной стоимости пая её не повторяет.
            fd.warnings.append("фонд с выплатой дохода: официальная доходность УК учитывает выплаченный доход, "
                               "изменение стоимости пая с ней не совпадает")
