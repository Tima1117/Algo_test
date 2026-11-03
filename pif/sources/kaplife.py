"""ООО «Капитал Лайф Страхование Жизни» (kaplife.ru) — страховщик с лицензией УК; два ОПИФ РФИ для
долевого страхования жизни: «Капитал Лайф Фонд акций …» (правила 7101) и «… Фонд смешанных
инвестиций …» (7100).

Где данные (сайт на Next.js, запросы страницы фонда подсмотрены в браузере):
* https://kaplife.ru/uk/disclosure/ — раскрытие УК, ссылки на страницы фондов /uk/opif1, /uk/opif2;
* https://kaplife.ru/uk/opifN/ — страница фонда: график и «Таблица стоимости пая и СЧА», документы
  (правила, КИД, справки о СЧА). Номер правил виден в документах: «Изменения и дополнения в ПДУ
  № 7101-1 …» — по нему страница сопоставляется с фондом реестра;
* https://web-api.kaplife.ru/dszh/get/ — JSON, из которого страница рисует график и таблицу:
  {«ОПИФ РФИ «Капитал Лайф Фонд …»»: [{date: "02.11.2026", scha, price, …}, …]} — все фонды и вся
  история с даты формирования (25.07.2025) одним ответом, каждый рабочий день, новые сверху.
  Запрос — POST без тела; без заголовка Origin: https://kaplife.ru API отвечает пустым телом
  (так устроен фронтенд сайта, это не авторизация — тот же заголовок шлёт любой браузер).
  Диапазон дат не принимает. СЧА и пай — в рублях: на 30.09.2026 у фонда акций 135 143 488,97 и
  823,73 — ровно как в КИД и справке о СЧА.
Сопоставление ряда: название в «» ключа JSON после norm_text = название фонда в реестре.

Официальная доходность 1/3/6/12 мес — только в ежемесячном КИД (PDF, раздел 5 «Основные
результаты инвестирования», «по состоянию на» — конец прошлого месяца). Текст PDF достаём pypdf,
если он установлен (в зависимостях проекта его нет): без него official_returns пустой и
предупреждение со ссылкой на КИД.

16.10.2026 УК раскрыла «Сообщение о принятии решения о передаче прав и обязанностей … другой
УК» по обоим фондам: после передачи данные, вероятно, переедут на сайт новой УК.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://kaplife.ru"
LIST_URL = f"{BASE}/uk/disclosure/"
API_URL = "https://web-api.kaplife.ru/dszh/get/"
API_HEADERS = {"Origin": BASE}

_QUOTED = re.compile(r"«(.+)»")
_PAGE_HREF = re.compile(r"^(?:https?://(?:www\.)?kaplife\.ru)?/uk/(opif\d+)/?$")
_RULES = re.compile(r"ПДУ\s*№\s*(\d{4,})-\d+")
_PUBLISHED = re.compile(r"Дата публикации:\s*(\d{2}\.\d{2}\.\d{4})")
_KID_PERIODS = {"1m": r"1\s*месяц", "3m": r"3\s*месяца", "6m": r"6\s*месяцев", "1y": r"1\s*год"}


def _soup(content) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


# ---------- разбор ----------

def parse_series(content) -> dict[str, list[SeriesPoint]]:
    """dszh/get -> {название фонда из «» : точки по возрастанию даты}."""
    if isinstance(content, (bytes, str)):
        require(bool(content and content.strip()), "API ответило пустым телом (нет заголовка Origin?)")
        try:
            content = json.loads(content)
        except ValueError as e:
            raise UnknownFormat(f"ответ API не JSON: {e}") from e
    require(isinstance(content, dict) and content, "API: ожидался непустой объект {фонд: [строки]}")
    out: dict[str, list[SeriesPoint]] = {}
    for key, rows in content.items():
        m = _QUOTED.search(key)
        require(m is not None, f"ключ API без названия в кавычках: {key!r}")
        require(isinstance(rows, list), f"{key}: ожидался список строк")
        by_date: dict[date, SeriesPoint] = {}
        for r in rows:
            require_keys(r, ("date", "scha", "price"), f"строка API {key}")
            d = day(r["date"])
            nav, price = num(r["scha"]), num(r["price"])
            nav = nav if nav and nav > 0 else None
            price = price if price and price > 0 else None
            if nav is None and price is None:
                continue
            p = SeriesPoint(d, nav, price)
            require(by_date.get(d, p) == p, f"{key}: две разные строки на {d}")
            by_date[d] = p
        out[m.group(1).strip()] = [by_date[d] for d in sorted(by_date)]
    return out


def parse_fund_links(content) -> list[str]:
    """Страница раскрытия УК -> адреса страниц фондов /uk/opifN/ (без повторов, по порядку)."""
    pages: list[str] = []
    for a in _soup(content).find_all("a", href=True):
        m = _PAGE_HREF.match(a["href"].strip())
        if m:
            url = f"{BASE}/uk/{m.group(1)}/"
            if url not in pages:
                pages.append(url)
    require(bool(pages), f"на {LIST_URL} нет ссылок на страницы фондов /uk/opifN")
    return pages


@dataclass
class FundPage:
    rules: set[str]
    kid_url: str | None
    kid_published: date | None


def parse_fund_page(content, page_url: str) -> FundPage:
    """Страница фонда -> номера правил из документов и самый свежий КИД (по дате публикации)."""
    s = _soup(content)
    rules: set[str] = set()
    kid_url, kid_pub = None, None
    for a in s.find_all("a", href=True):
        t = _text(a)
        rules.update(_RULES.findall(t))
        if "Ключевой информационный документ" in t and a["href"].lower().endswith(".pdf"):
            m = _PUBLISHED.search(t)
            pub = day(m.group(1)) if m else None
            if kid_url is None or (pub and (kid_pub is None or pub > kid_pub)):
                kid_url, kid_pub = urljoin(page_url, a["href"]), pub
    require(bool(rules) or kid_url is not None,
            f"на странице фонда {page_url} нет ни документов «ПДУ № …», ни КИД — вёрстка изменилась?")
    return FundPage(rules, kid_url, kid_pub)


def parse_kid_text(text: str, source_url: str) -> list[OfficialReturn]:
    """Текст КИД -> доходность за 1/3/6/12 мес из раздела 5 «Основные результаты инвестирования».

    Строка таблицы: «1 месяц 1.71% 1.79% -» — первое число доходность фонда, дальше отклонения
    от инфляции и индекса. «-» (нет данных за период) — периода нет, а не ноль.
    """
    t = re.sub(r"[ \t\xa0]+", " ", text or "")
    m = re.search(r"по состоянию на\s*(\d{2}\.\d{2}\.\d{4})", t)
    require(m is not None, "в КИД нет «по состоянию на ДД.ММ.ГГГГ»")
    as_of = day(m.group(1))
    i = t.find("Основные результаты инвестирования")
    require(i >= 0, "в КИД нет раздела «Основные результаты инвестирования»")
    sect = t[i:i + 3000]
    out = []
    for period, label in _KID_PERIODS.items():
        # число — первым в альтернативе, иначе «-0.64%» поймается как прочерк «-»
        m = re.search(rf"(?m)^\s*{label}\s+(?:([-−]?\d+(?:[.,]\d+)?)\s*%|-(?=\s|$))", sect)
        require(m is not None, f"в разделе доходности КИД нет строки периода {period}")
        v = pct(m.group(1).replace("−", "-")) if m.group(1) else None
        if v is not None:
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def has_pypdf() -> bool:
    return importlib.util.find_spec("pypdf") is not None


def kid_pdf_text(pdf: bytes) -> str:
    """Текст КИД из PDF. pypdf — необязательная зависимость: импорт здесь."""
    from pypdf import PdfReader  # noqa: PLC0415

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


def match_series(fund: Fund, series: dict[str, list[SeriesPoint]]) -> str | None:
    want = norm_text(fund.name)
    hits = [k for k in series if norm_text(k) == want]
    require(len(hits) <= 1, f"фонду «{fund.name}» соответствуют несколько рядов API: {hits}")
    return hits[0] if hits else None


# ---------- адаптер ----------

class KaplifeAdapter(Adapter):
    key = "kaplife"
    title = "Капитал Лайф Страхование Жизни"
    uk_inns = ("7706548313",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        memo: dict[str, tuple[str, object]] = {}

        def once(key: str, fn):
            if key not in memo:
                try:
                    memo[key] = ("ok", fn())
                except Exception as e:  # noqa: BLE001 — ошибку получит каждый фонд через guarded()
                    memo[key] = ("err", e)
            kind, val = memo[key]
            if kind == "err":
                raise val
            return val

        def api():
            raw = ctx.http.get(API_URL, method="POST", headers=API_HEADERS, fmt="json", funds=all_rules)
            return raw, parse_series(raw.content())

        def pages() -> dict[str, tuple[str, FundPage]]:
            """rules -> (адрес страницы, разбор). Сбой одной страницы не мешает другим."""
            links = parse_fund_links(ctx.http.get(LIST_URL, fmt="html", funds=all_rules).content())
            found: dict[str, tuple[str, FundPage]] = {}
            for url in links:
                try:
                    fp = parse_fund_page(ctx.http.get(url, fmt="html", funds=all_rules).content(), url)
                except Exception as e:  # noqa: BLE001
                    ctx.log.warning("[kaplife] страница %s: %s", url, e)
                    continue
                for r in fp.rules:
                    found.setdefault(r, (url, fp))
            return found

        def one(fund: Fund) -> FundData:
            raw, series = once("api", api)
            key = match_series(fund, series)
            if key is None:
                return FundData(fund.rules, status="not_found", page_url=LIST_URL,
                                reason=f"в данных графиков {API_URL} нет фонда «{fund.name}»")
            page_url, fp = LIST_URL, None
            try:
                page_url, fp = once("pages", pages).get(fund.rules, (LIST_URL, None))
            except Exception as e:  # noqa: BLE001 — ряд уже есть, страница нужна для ссылки и КИД
                ctx.log.warning("[kaplife] %s: страницы фондов: %s", fund.rules, e)
            fd = FundData(fund.rules, currency="RUB", page_url=page_url,
                          segments=[self.segment(raw, series[key], page_url=page_url, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status, fd.reason = "no_history", f"в {API_URL} нет точек начиная с {ctx.since:%d.%m.%Y}"
            if fp is None:
                fd.warnings.append(f"страница фонда с правилами № {fund.rules} на {LIST_URL} не найдена")
            self._returns(ctx, fund, fd, fp)
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]

    @staticmethod
    def _returns(ctx: Context, fund: Fund, fd: FundData, fp: FundPage | None) -> None:
        if fp is None or fp.kid_url is None:
            fd.warnings.append("КИД фонда не найден — официальная доходность не заполнена")
            return
        if not has_pypdf():
            fd.warnings.append(f"доходность за 1/3/6/12 мес УК публикует только в КИД (PDF), а pypdf "
                               f"не установлен — не заполнена; КИД: {fp.kid_url}")
            return
        try:
            raw = ctx.http.get(fp.kid_url, fmt="pdf", funds=[fund.rules])
            fd.official_returns = parse_kid_text(kid_pdf_text(raw.content()), fp.kid_url)
        except Exception as e:  # noqa: BLE001 — ряд важнее доходности: сбой КИД — предупреждение
            ctx.log.warning("[kaplife] %s: КИД %s: %s", fund.rules, fp.kid_url, e)
            fd.warnings.append(f"КИД {fp.kid_url} не разобран: {e}")
