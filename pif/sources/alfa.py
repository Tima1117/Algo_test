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
- официальная доходность: главная страница https://www.alfacapital.ru/ — обязательные «Сведения о
  приросте расчётной стоимости инвестиционного пая на <дата> за 1 мес., 3 мес., 6 мес., 1 год…» с номером
  правил фонда (блок alfa-capital-product-yield-disclaimer в данных React Router: аргумент
  streamController.enqueue — плоский массив turbo-stream). Дата — последний рабочий день прошлого месяца,
  период — от конца предыдущего месяца; у обычных фондов совпадает с рядом пая до сотых.
  У фондов «с выплатой дохода» прирост учитывает выплаты пайщикам, у юаневого фонда — посчитан в рублях.

Чего не берём и почему:
- JSON-RPC /api (getFundNav/getFundShare — график на странице фонда): закрыт robots.txt
  (Disallow: /api); к тому же заполняет выходные копией пятницы и отдаёт 0 за ещё не рассчитанный день.
- КИД (PDF с доходностью) и отчёты лежат под /disclosure/file/ — тоже Disallow в robots.txt.
  Поэтому у фондов без сведений о приросте на сайте (авторские, интервальные, часть биржевых)
  официальной доходности нет — это видно в warnings фонда.
- Блок «Динамика фонда на <дата>» на странице фонда (и витрина /gate/v1/fundsData/v1): значения в нём
  на месяц старше подписи (при подписи 30.10.2026 — прирост по 30.09.2026); для сверки не годится.
- Суммы выплат дохода (payouts): в разделе «Сообщения» УК публикует только даты, на которые
  выплачивается доход, без сумм на пай; суммы — в PDF-отчётах под /disclosure/file/.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://www.alfacapital.ru"
CATALOG_URL = f"{BASE}/disclosure/pifs"
NAV_URL = f"{BASE}/gate/v1/fundDisclosureRegData/v1"
HOME_URL = f"{BASE}/"

# Предохранитель листания: 60 страниц по 60 строк — больше 14 лет ежедневных публикаций.
MAX_PAGES = 60

# Тип фонда на сайте -> тип в реестре.
SITE_TYPES = {"opened": "open", "exchangeTraded": "exchange", "interval": "interval"}
# Раздел сайта со страницами фондов для человека (у интервальных и авторских таких страниц нет).
PRODUCT_SECTIONS = {"open": "pifs", "exchange": "bpifs"}
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

# Специальные значения turbo-stream (react-router single fetch).
_TS_SPECIAL = {-1: None, -2: float("nan"), -3: float("-inf"), -4: -0.0, -5: None, -6: float("inf"), -7: None}


def _turbo_stream(content):
    """Данные React Router со страницы: первый streamController.enqueue("<JSON-строка>") -> дерево."""
    text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
    marker = "streamController.enqueue("
    i = text.find(marker)
    require(i >= 0, "на странице нет данных React Router (streamController.enqueue)")
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[i + len(marker):])
        flat = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        raise UnknownFormat(f"данные React Router — не turbo-stream: {e}") from e
    require(isinstance(flat, list) and bool(flat), "turbo-stream: ожидался непустой массив")
    memo: dict[int, object] = {}

    def val(i: int):
        if i < 0:
            require(i in _TS_SPECIAL, f"turbo-stream: незнакомая константа {i}")
            return _TS_SPECIAL[i]
        if i in memo:
            return memo[i]
        require(i < len(flat), f"turbo-stream: ссылка {i} за пределами массива")
        x = flat[i]
        if isinstance(x, dict):
            memo[i] = obj = {}
            for k, v in x.items():
                require(k.startswith("_") and k[1:].isdigit(), f"turbo-stream: незнакомый ключ {k!r}")
                obj[flat[int(k[1:])]] = val(v)
            return obj
        if isinstance(x, list) and x and isinstance(x[0], str):
            memo[i] = None          # даты, промисы и прочие типы нам не нужны
            return None
        if isinstance(x, list):
            memo[i] = arr = []
            arr.extend(val(v) for v in x)
            return arr
        memo[i] = x
        return x

    return val(0)


_DISCLAIMER_KEY = "alfa-capital-product-yield-disclaimer"
_GROWTH_MARK = "Сведения о приросте расчетной стоимости инвестиционного пая"
_PCT = r"-?\d+(?:,\d+)?%|н/д"     # «1,57%», «-0,36%», «н/д» (не опубликовано: фонд моложе периода)
_DISCLAIMER = re.compile(
    r"Правила доверительного управления № (?P<rules>\d[\d\-]*) зарегистрированы .*?\. "
    + _GROWTH_MARK + r" на (?P<date>\d{2}\.\d{2}\.\d{4}) "
    rf"за 1 мес\. (?P<m1>{_PCT}), за 3 мес\. (?P<m3>{_PCT}), за 6 мес\. (?P<m6>{_PCT}), "
    rf"за 1 год (?P<y1>{_PCT})[,.]")
_DISCLAIMER_PERIODS = {"m1": "1m", "m3": "3m", "m6": "6m", "y1": "1y"}


def parse_returns(content, source_url: str) -> dict[str, list[OfficialReturn]]:
    """Сведения о приросте пая со страницы сайта -> {номер правил: доходности за 1/3/6/12 мес.}."""
    tree = _turbo_stream(content)
    require_keys(tree, ["loaderData"], "данные React Router")
    blocks = [r["data"][_DISCLAIMER_KEY] for r in tree["loaderData"].values()
              if isinstance(r, dict) and isinstance(r.get("data"), dict) and _DISCLAIMER_KEY in r["data"]]
    require(len(blocks) == 1, f"на странице нет блока {_DISCLAIMER_KEY} (или он не один)")
    require_keys(blocks[0], ["main"], _DISCLAIMER_KEY)
    texts = blocks[0]["main"]
    require(isinstance(texts, list), f"{_DISCLAIMER_KEY}.main — не список")
    out: dict[str, list[OfficialReturn]] = {}
    for text in texts:
        if not isinstance(text, str) or _GROWTH_MARK not in text:
            continue                  # прочие оговорки (стратегии ДУ и т. п.)
        m = _DISCLAIMER.search(" ".join(text.replace("\u2212", "-").split()))
        require(m is not None, f"сведения о приросте пая в незнакомом виде: {text[:160]!r}")
        as_of = day(m["date"])
        rets = [OfficialReturn(period, pct(m[g]), as_of, source_url)
                for g, period in _DISCLAIMER_PERIODS.items() if m[g] != "н/д"]
        rules = m["rules"]
        if rules in out:
            require(out[rules] == rets, f"для правил № {rules} на странице разные сведения о приросте")
        out[rules] = rets
    require(bool(out), "в блоке оговорок нет ни одного сведения о приросте стоимости пая")
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
        """Общий документ (каталог, главная) качаем один раз за прогон; ошибку тоже запоминаем,
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
        raw = ctx.http.get(HOME_URL, fmt="html", funds=self._all_rules)
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
            if fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", "фонд формируется, УК ещё не публикует СЧА и пай"
            else:
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
            by_rules = self._once("yields", lambda: self._yields(ctx))
        except FetchError as e:
            fd.warnings.append(f"официальная доходность не получена: {e.reason}")
            return
        except UnknownFormat as e:
            fd.errors.append(f"разбор официальной доходности: {e}")
            return
        rets = by_rules.get(fund.rules)
        if rets is None:
            fd.warnings.append("официальная доходность не собрана: сведений о приросте пая для фонда на страницах "
                               "сайта нет, только в КИД (PDF под /disclosure/file/, закрыт robots.txt)")
            return
        fd.page_url = product_page(site) or fd.page_url
        if not rets:
            fd.warnings.append("УК пишет «н/д» за все периоды 1/3/6/12 мес.: фонд моложе месяца")
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
