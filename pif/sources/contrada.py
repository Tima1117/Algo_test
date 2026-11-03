"""АО «Контрада Капитал» (contrada-capital.ru): история СЧА и пая, официальная доходность.

Сайт на Next.js собирается из блоков CMS. Те же блоки браузер дочитывает через открытый
клиентский API сайта https://contrada-capital.ru/client/ — берём JSON оттуда (HTML страницы
весит 2+ МБ и содержит те же данные).

Источники (сайт УК):
- https://contrada-capital.ru/client/pages/urls — список страниц сайта (url -> id страницы);
- https://contrada-capital.ru/client/pages/<id> для /inform/pif/{opif,ipif,bpif}/<slug> («Раскрытие информации» по
  фонду): блок «О фонде» с номером правил (по нему сопоставляем фонд реестра), таблица
  «Стоимость чистых активов и расчетная стоимость пая» — вся история с даты формирования
  (открытые и биржевой — каждый рабочий день, интервальные — раз в месяц), СЧА и пай в рублях;
  блок «Ключевой информационный документ» со ссылками на КИД (PDF);
- https://contrada-capital.ru/client/pages/<id> для /products/<тип>/<slug> (витрина фонда): виджет над графиком
  «Пай: За день / С начала года / За 1 год / …» — доходность пая за 1 год на последнюю дату цены;
- КИД (https://storage.contrada-capital.ru/…pdf), раздел 5 «Доходность за период»: 1 месяц,
  3 месяца, 6 месяцев, 1 год — на последнюю дату прошлого месяца. Читается через pypdf
  (необязательная зависимость, импорт внутри функции); без pypdf остаётся 1 год из виджета.

Валюта: все фонды УК, включая «Силу Валюты» (облигации в иностранной валюте), считают СЧА и
пай в рублях (КИД: «Расчетная стоимость инвестиционного пая: … руб.», таблица «(руб.)»).
"""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://contrada-capital.ru"
URLS_URL = f"{BASE}/client/pages/urls"
PAGE_URL = BASE + "/client/pages/{id}"
CURRENCY = "RUB"

# Раскрытие по розничным фондам; ЗПИФ и прекращённые (/inform/archive/…) не нужны.
_INFORM_URL = re.compile(r"^/inform/pif/(opif|ipif|bpif)/([^/]+)$")
_PRODUCT_URL = re.compile(r"^/products/[^/]+/([^/]+)$")
_RULES = re.compile(r"номер и дата регистрации правил доверительного управления[^:№]{0,40}:?\s*"
                    r"№\s*(\d{3,5})\b", re.I)
_DATE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
NAV_TABLE_TITLE = "Стоимость чистых активов и расчетная стоимость пая"
KID_BLOCK_TITLE = "Ключевой информационный документ"
PERIODS = ("1m", "3m", "6m", "1y")


def _json(content) -> dict:
    if isinstance(content, dict):
        return content
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        return json.loads(content)
    except ValueError as e:
        raise UnknownFormat(f"не JSON: {str(content)[:80]!r}") from e


def _plain(html: str | None) -> str:
    text = BeautifulSoup(html or "", "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def norm_rules(s: str) -> str:
    """'№ 4693' / ' 4693 ' -> '4693'."""
    return re.sub(r"[^\d]", "", str(s or ""))


# ---------------------------------------------------------------- разбор JSON сайта

def parse_page_urls(content) -> list[tuple[str, str]]:
    """/client/pages/urls -> [(url, id)]."""
    data = _json(content)
    require_keys(data, ["data"], "список страниц")
    rows = data["data"]
    require(isinstance(rows, list) and rows, "список страниц: пустой или не список")
    out = []
    for r in rows:
        require_keys(r, ["url", "id"], "страница в списке")
        out.append((str(r["url"]), str(r["id"])))
    return out


def page_data(content) -> dict:
    """/client/pages/<id> -> объект страницы с блоками."""
    data = _json(content)
    require_keys(data, ["data"], "страница")
    page = data["data"]
    require_keys(page, ["id", "url", "title", "text_blocks", "tables", "doc_blocks", "graphs"], "страница")
    return page


def parse_rules(content) -> str | None:
    """Номер правил ДУ из блока «О фонде» страницы раскрытия ('4693') или None."""
    page = page_data(content)
    for block in page["text_blocks"] or []:
        m = _RULES.search(_plain(block.get("text")))
        if m:
            return m.group(1)
    return None


def parse_history(content) -> list[SeriesPoint]:
    """Таблица «Стоимость чистых активов и расчетная стоимость пая» со страницы фонда.

    Формат: {"table": {"22.12.2021": {"date": "22.12.2021", "nav": 29996599.57,
    "piePrice": 99.99, "changePriceOfActive": …, "prevPriceOfPie": …}, …}} — СЧА в рублях,
    цена пая в рублях. Изменения в % не берём (их считает сборщик).
    """
    page = page_data(content)
    tables = [t for t in page["tables"] or []
              if norm_text((t or {}).get("title")) == norm_text(NAV_TABLE_TITLE)]
    require(len(tables) == 1, f"на странице {page['url']} таблиц «{NAV_TABLE_TITLE}»: {len(tables)}, ожидалась 1")
    table = tables[0]
    require_keys(table, ["table"], "таблица СЧА")
    rows = table["table"]
    require(isinstance(rows, dict) and rows, "таблица СЧА: пустая или не объект {дата: строка}")
    points: dict[date, SeriesPoint] = {}
    for key, row in rows.items():
        require_keys(row, ["date", "nav", "piePrice"], f"строка таблицы СЧА {key}")
        d = day(row["date"])
        require(d == day(key), f"строка таблицы СЧА: ключ {key} не совпадает с датой {row['date']}")
        nav, price = num(row["nav"]), num(row["piePrice"])
        # 0 в таблице — это «нет значения», а не нулевая СЧА
        nav = nav if nav else None
        price = price if price else None
        require(nav is not None or price is not None, f"строка таблицы СЧА {key}: нет ни СЧА, ни пая")
        require(d not in points, f"таблица СЧА: дата {key} повторяется")
        points[d] = SeriesPoint(d, nav, price)
    return [points[d] for d in sorted(points)]


def parse_widget_returns(content, source_url: str) -> list[OfficialReturn]:
    """Виджет «Пай … За 1 год» над графиком на витрине фонда (/products/…) -> 1y.

    change_price_data = {"prev_workday_1": "30.10.2026", "prev_workday_2": "02.11.2026",
    "rows": [{"type": "pai", "price_by_periods": {"prev_day_1": …, "prev_day_2": …,
    "peer_day": …, "ytd": …, "year_1": 6.09, …}}, {"type": "nav", …}]}. На странице: колонки
    «30.10.2026 | 02.11.2026 | За день | С начала года | За 1 год | …», строки «Пай» и «СЧА».
    Строка «СЧА» — изменение СЧА, а не доходность, её не берём.
    """
    page = page_data(content)
    blocks = [g.get("change_price_data") for g in page["graphs"] or [] if (g or {}).get("change_price_data")]
    require(len(blocks) == 1, f"на витрине {page['url']} виджетов изменения пая: {len(blocks)}, ожидался 1")
    cpd = blocks[0]
    require_keys(cpd, ["prev_workday_1", "prev_workday_2", "rows"], "виджет изменения пая")
    pai = [r for r in cpd["rows"] or [] if (r or {}).get("type") == "pai"]
    require(len(pai) == 1, "виджет изменения пая: нет строки «Пай»")
    require_keys(pai[0], ["price_by_periods"], "виджет, строка «Пай»")
    periods = pai[0]["price_by_periods"]
    require_keys(periods, ["year_1"], "виджет, строка «Пай»")
    # последняя дата цены — поздняя из двух колонок с ценами
    as_of = max(day(cpd["prev_workday_1"]), day(cpd["prev_workday_2"]))
    value = pct(periods["year_1"])
    if value is None:
        return []          # фонду меньше года — «-»
    return [OfficialReturn("1y", value, as_of, source_url)]


@dataclass
class KidDoc:
    url: str
    date: date            # «по состоянию на …» из заголовка документа
    title: str


def latest_kid(content) -> KidDoc | None:
    """Последний КИД из блока «Ключевой информационный документ» страницы раскрытия."""
    page = page_data(content)
    docs: list[KidDoc] = []
    for block in page["doc_blocks"] or []:
        if norm_text(block.get("title")) != norm_text(KID_BLOCK_TITLE):
            continue
        require_keys(block, ["documents"], "блок КИД")
        for doc in block["documents"] or []:
            require_keys(doc, ["title", "file", "is_active"], "документ КИД")
            if not doc["is_active"] or not str(doc["file"]).lower().endswith(".pdf"):
                continue
            m = _DATE.search(doc["title"] or "") or _DATE.search(doc.get("name") or "")
            if m:
                docs.append(KidDoc(doc["file"], day(m.group(1)), " ".join(doc["title"].split())))
    return max(docs, key=lambda k: k.date) if docs else None


# ---------------------------------------------------------------- КИД (PDF)

@dataclass
class KidInfo:
    date: date                                   # «Ключевой информационный документ по состоянию на …»
    price: float                                 # «Расчетная стоимость инвестиционного пая: … руб.»
    nav: float                                   # «Стоимость чистых активов Фонда: … руб.»
    returns: dict[str, float | None] = field(default_factory=dict)
    pays_income: bool = False                    # фонд выплачивает доход пайщикам


_KID_ROWS = {"1m": r"1[ \t]+месяц", "3m": r"3[ \t]+месяца", "6m": r"6[ \t]+месяцев", "1y": r"1[ \t]+год"}
_RET = r"([-−–]?[ ]?\d+(?:,\d+)?)"                 # доходность: «-3,05»
_NUM = r"(\d[\d \xa0]*(?:,\d+)?)"                   # сумма с пробелами-разрядами: «1 686,51»


def parse_kid_text(text: str) -> KidInfo:
    """Раздел 5 КИД «Доходность за период, %»: первая колонка после названия периода.

    Пример строки (pypdf): «1 месяц -3,05  -2,97» -> 1m = -3,05 (вторая колонка — отклонение
    от инфляции). Пустая ячейка или «-» -> None (фонд моложе периода).
    """
    text = text.replace("\xa0", " ")
    m = re.search(r"ключевой информационный документ по состоянию на\s*(\d{2}\.\d{2}\.\d{4})", text, re.I)
    require(m is not None, "КИД: нет «Ключевой информационный документ по состоянию на ДД.ММ.ГГГГ»")
    kid_date = day(m.group(1))
    start = text.find("Доходность за период")
    require(start >= 0, "КИД: нет раздела «Доходность за период»")
    end = text.find("Раздел 6", start)
    section = text[start:end if end > 0 else len(text)]
    returns: dict[str, float | None] = {}
    for period, label in _KID_ROWS.items():
        row = re.search(rf"^[ \t]*{label}(?![а-я])[ \t]*(.*)$", section, re.M | re.I)
        require(row is not None, f"КИД: в разделе «Доходность за период» нет строки «{label}»")
        cell = re.match(rf"\s*{_RET}(?=\s|$)", row.group(1))
        returns[period] = pct(cell.group(1).replace("−", "-").replace("–", "-").replace(" ", "")) if cell else None
    price = re.search(rf"Расчетная стоимость инвестиционного пая:\s*{_NUM}", section)
    nav = re.search(rf"Стоимость чистых активов Фонда:\s*{_NUM}", section)
    require(price is not None and nav is not None, "КИД: нет цены пая или СЧА на дату расчёта доходности")
    return KidInfo(kid_date, num(price.group(1)), num(nav.group(1)), returns,
                   pays_income="распределяется среди владельцев" in text)


def kid_pdf_text(pdf: bytes) -> str:
    """Текст КИД из PDF. pypdf — необязательная зависимость: импорт здесь."""
    import io

    from pypdf import PdfReader  # noqa: PLC0415

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


def has_pypdf() -> bool:
    return importlib.util.find_spec("pypdf") is not None


def kid_as_of(info: KidInfo, points: list[SeriesPoint]) -> date | None:
    """Дата, на которую в КИД посчитана доходность: строка таблицы с теми же СЧА и паем.

    КИД «по состоянию на 29.10.2026» содержит доходность, пай и СЧА на 30.09.2026 (конец
    прошлого месяца), поэтому дату заголовка брать нельзя.
    """
    hits = [p.date for p in points if p.date <= info.date and p.nav is not None and p.price is not None
            and abs(p.nav - info.nav) < 0.01 and abs(p.price - info.price) < 0.005]
    return max(hits) if hits else None


# ---------------------------------------------------------------- адаптер

class ContradaAdapter(Adapter):
    key = "contrada"
    title = "АО «Контрада Капитал»"
    uk_inns = ("9703206844",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        memo: dict[str, tuple[str, object]] = {}

        def catalog():
            # Каталог качаем один раз; ошибку тоже запоминаем, чтобы guarded() поставил её
            # каждому фонду, а не уронил весь адаптер.
            if "catalog" not in memo:
                try:
                    memo["catalog"] = ("ok", self._catalog(ctx))
                except Exception as e:  # noqa: BLE001
                    memo["catalog"] = ("err", e)
            kind, val = memo["catalog"]
            if kind == "err":
                raise val
            return val

        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, catalog())) for f in funds]

    # ---- каталог: номер правил -> страница раскрытия
    def _catalog(self, ctx: Context) -> dict:
        urls = parse_page_urls(ctx.http.get(URLS_URL, fmt="json").content())
        inform = list(dict.fromkeys((u, i) for u, i in urls if _INFORM_URL.match(u)))
        require(bool(inform), "в списке страниц сайта нет раскрытия по ПИФ (/inform/pif/…)")
        by_rules: dict[str, tuple[str, str]] = {}
        failures: list[Exception] = []
        for url, pid in inform:
            try:
                rules = parse_rules(ctx.http.get(PAGE_URL.format(id=pid), fmt="json").content())
            except (FetchError, UnknownFormat) as e:
                failures.append(e)
                continue
            if rules is None:
                failures.append(UnknownFormat(f"на странице {BASE}{url} нет номера правил в блоке «О фонде»"))
                continue
            by_rules.setdefault(rules, (url, pid))
        products = {}
        for url, pid in urls:
            m = _PRODUCT_URL.match(url)
            if m:
                products.setdefault(m.group(1).lower(), (url, pid))
        return {"by_rules": by_rules, "products": products, "failures": failures}

    def _one(self, ctx: Context, fund: Fund, cat: dict) -> FundData:
        hit = cat["by_rules"].get(norm_rules(fund.rules))
        if hit is None:
            if cat["failures"]:
                # фонд мог быть на странице, которую не удалось скачать/разобрать
                raise cat["failures"][0]
            if str(fund.status).lower().startswith("формир"):
                return FundData(fund.rules, status="not_formed",
                                reason="фонд формируется, на сайте УК его страницы ещё нет")
            return FundData(fund.rules, status="not_found",
                            reason=f"на сайте УК нет страницы раскрытия с правилами № {fund.rules}")
        url, pid = hit
        page_url = BASE + url
        # та же страница из кэша этого запуска — заодно связываем её с фондом в манифесте
        raw = ctx.http.get(PAGE_URL.format(id=pid), fmt="json", funds=[fund.rules])
        content = raw.content()
        points = parse_history(content)
        seg = self.segment(raw, points, page_url=page_url, since=ctx.since)
        fd = FundData(fund.rules, currency=CURRENCY, page_url=page_url)
        if seg.points:
            fd.segments = [seg]
        else:
            fd.status = "no_history"
            fd.reason = f"в таблице СЧА на сайте УК нет точек с {ctx.since:%d.%m.%Y} (последняя — {points[-1].date:%d.%m.%Y})"
        fd.official_returns = self._returns(ctx, fund, cat, url, content, points, fd.warnings)
        return fd

    # ---- официальная доходность: КИД (1/3/6/12 мес) + виджет витрины (12 мес)
    def _returns(self, ctx: Context, fund: Fund, cat: dict, inform_url: str, page_content,
                 points: list[SeriesPoint], warnings: list[str]) -> list[OfficialReturn]:
        out: dict[str, OfficialReturn] = {}
        kid = latest_kid(page_content)
        if kid is None:
            warnings.append("на странице раскрытия нет КИД — доходность за 1/3/6 мес не опубликована")
        elif not has_pypdf():
            warnings.append("доходность за 1/3/6 мес УК публикует только в КИД (PDF), а pypdf не установлен — "
                            "взята только доходность за 1 год с витрины фонда (если фонду больше года)")
        else:
            try:
                raw = ctx.http.get(kid.url, fmt="pdf", funds=[fund.rules])
                info = parse_kid_text(kid_pdf_text(raw.content()))
                as_of = kid_as_of(info, points)
                if as_of is None:
                    warnings.append(f"КИД на {info.date:%d.%m.%Y}: дата расчёта доходности не найдена в таблице СЧА")
                for period, value in info.returns.items():
                    if value is not None:
                        out[period] = OfficialReturn(period, value, as_of, kid.url)
                if info.pays_income and out:
                    warnings.append("доходность в КИД учитывает выплаченный пайщикам доход, ряд — только стоимость пая")
            except (FetchError, UnknownFormat, ImportError) as e:
                warnings.append(f"КИД {kid.url} не разобран: {e}")
        if "1y" not in out:
            m = _INFORM_URL.match(inform_url)
            product = cat["products"].get(m.group(2).lower()) if m else None
            if product is None:
                warnings.append("на сайте нет витрины фонда (/products/…) — доходность за 1 год не взята")
            else:
                purl, pid = product
                try:
                    raw = ctx.http.get(PAGE_URL.format(id=pid), fmt="json", funds=[fund.rules])
                    for r in parse_widget_returns(raw.content(), BASE + purl):
                        out.setdefault(r.period, r)
                except (FetchError, UnknownFormat) as e:
                    warnings.append(f"доходность за 1 год с витрины {BASE}{purl} не взята: {e}")
        return [out[p] for p in PERIODS if p in out]
