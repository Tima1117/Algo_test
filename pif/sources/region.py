"""АО «РЕГИОН Эссет Менеджмент» (АО «РЕГИОН ЭсМ») — region-am.ru, ОПИФ.

Где что лежит (разведка 2026-11-03):
* список фондов: https://region-am.ru/disclosure/paevye-investitsionnye-fondy/ — ссылки на страницы
  раскрытия информации /disclosure/paevye-investitsionnye-fondy/<slug>/ с полными названиями фондов;
* страница раскрытия фонда (тяжёлая, 0,7–3 МБ: в ней все документы фонда), вкладка «Стоимость пая и СЧА»:
  - «Правила доверительного управления зарегистрированы … за № 0120–14241368» — по номеру правил
    фонд реестра и сопоставляется с сайтом;
  - «Дата определения результатов: 30.10.2026» и таблица «Изменение стоимости пая» за месяц,
    3 и 6 месяцев, 1, 3 и 5 лет — официальная доходность (начало периода — последний рабочий
    день месяца: в комментариях вёрстки видно «0-3-31.07.2026»); «---» — периода у фонда ещё нет;
  - таблица «Дата и время публикации | Дата расчета | РСИП, руб. | СЧА, руб. | …» за текущий год,
    ежедневно; годы выбираются слайдером 2026, 2025, … ;
* прошлые годы той же таблицы: GET /ajax/table.php?year=<год>&idPage=<data-page>&idTab=Button__<data-name>__os-tab-1
  &idSchaTpl=<data-schatpl> с заголовком X-Requested-With: XMLHttpRequest (без него — 404),
  ответ {"html": "<таблица>"}. Диапазона дат нет — докачка идёт по годам от ctx.start_for().
* выплаты дохода (из 9 фондов платит только «РЕГИОН Купонный доход», раз в квартал): вкладка
  «Официальные сообщения», «Сообщение о выплате дохода…»: размер дохода на один пай и «Дата,
  на которую … выплачивается доход». Текстом на странице опубликовано только последнее сообщение
  (32,26 ₽ на 30.09.2026), предыдущие — PDF-файлами (/upload/files/5306/…pdf), их не разбираем.

Ловушки:
* Сервер за QRATOR: без X-Requested-With /ajax/table.php отвечает 404 с пустым телом.
* У фондов с индексом («Денежный рынок», «Фонд Золото») в таблице лишние колонки отклонения от
  индекса и строка подзаголовка «За 20 / 250 рабочих дней» — колонки ищем по заголовкам.
* Внутри ячеек есть подписи для мобильной вёрстки (div.t-mobil-title) — их убираем перед чтением числа.
* Официальная доходность РЕГИОН — буквально «изменение стоимости пая», БЕЗ учёта выплат: у
  «Купонного дохода» на 30.10.2026 за 3 мес. УК пишет −0,10 % — ровно столько даёт ряд без выплат,
  а с выплатой 32,26 ₽ вышло бы +2,97 %. Поэтому выплаты в FundData.payouts НЕ кладём (сборка
  посчитала бы с ними и разошлась бы с официальной цифрой; к тому же 4 из 5 сообщений — PDF),
  а пишем о них в warnings. parse_payouts_region() готова, если сборке понадобится полная доходность.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, Payout, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

BASE = "https://region-am.ru"
CATALOG_URL = f"{BASE}/disclosure/paevye-investitsionnye-fondy/"
TABLE_URL = f"{BASE}/ajax/table.php"
AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

RETURN_COLUMNS = {"месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")


@dataclass
class RegionFundPage:
    rules: str                # номер правил с сайта, в записи реестра ЦБ
    page_id: str              # data-page — для /ajax/table.php
    tab_id: str               # Button__<data-name>__<вкладка «Стоимость пая и СЧА»>
    scha_tpl: str             # data-schatpl
    years: list[int] = field(default_factory=list)   # годы в слайдере таблицы СЧА


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm_rules(s: str) -> str:
    """'0120–14241368' / '№4266' -> '0120-14241368' / '4266' (как в реестре ЦБ)."""
    return re.sub(r"[\s№]", "", str(s)).replace("–", "-").replace("—", "-").replace("‑", "-")


def parse_catalog_region(html: str) -> dict[str, str]:
    """Список фондов: адрес страницы раскрытия -> полное название фонда."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.fullmatch(r"(?:https?://(?:www\.)?region-am\.ru)?(/disclosure/paevye-investitsionnye-fondy/[^/#?]+/)",
                         a["href"].strip())
        text = _clean(a.get_text(" "))
        if m and text:
            out.setdefault(urljoin(BASE, m.group(1)), text)
    require(len(out) >= 3, f"в списке фондов найдено только {len(out)} ссылок — сменилась вёрстка?")
    return out


def _nav_tab(soup: BeautifulSoup):
    """Блок вкладок фонда и вкладка «Стоимость пая и СЧА» (её data-target)."""
    tabs = soup.select_one("div.ionTabs[data-page]")
    require(tabs is not None, "нет блока вкладок div.ionTabs[data-page]")
    target = None
    for t in tabs.select(".ionTabs__tab"):
        if "стоимость пая и сча" in _clean(t.get_text(" ")).lower():
            target = t.get("data-target")
    require(bool(target), "нет вкладки «Стоимость пая и СЧА»")
    return tabs, target


def parse_fund_page_region(html: str) -> RegionFundPage:
    """Страница раскрытия: номер правил и всё, что нужно для запроса прошлых лет таблицы СЧА."""
    soup = BeautifulSoup(html, "lxml")
    rules = None
    for el in soup.select(".rem-rules-text"):
        text = _clean(el.get_text(" "))
        m = re.search(r"Правила доверительного управления.*?зарегистрированы.*?№\s*(\d+(?:\s*[–—‑-]\s*\d+)?)", text)
        if m:
            rules = norm_rules(m.group(1))
            break
    require(rules is not None, "на странице нет «Правила … зарегистрированы … за № …»")
    tabs, target = _nav_tab(soup)
    require_keys(tabs.attrs, ["data-name", "data-schatpl"], "div.ionTabs")
    item = tabs.select_one(f'.ionTabs__item[data-name="{target}"]')
    require(item is not None, f"нет содержимого вкладки {target}")
    years = sorted({int(_clean(s.get_text())) for s in item.select(".slider-pension-nav__slide")
                    if re.fullmatch(r"\d{4}", _clean(s.get_text()))}, reverse=True)
    require(bool(years), "нет слайдера годов у таблицы СЧА — прошлые годы не запросить")
    return RegionFundPage(rules=rules, page_id=tabs["data-page"], scha_tpl=tabs["data-schatpl"],
                          tab_id=f"Button__{tabs['data-name']}__{target}", years=years)


def parse_returns_region(html: str, source_url: str) -> list[OfficialReturn]:
    """«Изменение стоимости пая» за месяц / 3 / 6 месяцев / 1 год на «Дату определения результатов»."""
    soup = BeautifulSoup(html, "lxml")
    block = None
    for rem in soup.select("div.rem-remark"):
        if "Дата определения результатов" in rem.get_text():
            block = rem
            break
    require(block is not None, "нет блока «Дата определения результатов»")
    m = re.search(r"Дата определения результатов:\s*(\d{2}\.\d{2}\.\d{4})", _clean(block.get_text(" ")))
    require(m is not None, "не нашёл дату определения результатов")
    as_of = day(m.group(1))
    rows = block.select("table tr")
    require(len(rows) >= 2, "в таблице доходности нет строки со значениями")
    heads = [_clean(td.get_text(" ")).lower() for td in rows[0].find_all("td")]
    values = None
    for tr in rows[1:]:
        cells = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
        if cells and cells[0].lower().startswith("изменение стоимости пая"):
            values = cells
    require(values is not None, "нет строки «Изменение стоимости пая»")
    missing = [h for h in RETURN_COLUMNS if h not in heads]
    require(not missing, f"в таблице доходности нет колонок {missing}; есть {heads}")
    out = []
    for h, period in RETURN_COLUMNS.items():
        i = heads.index(h)
        require(i < len(values), f"нет значения в колонке «{h}»")
        raw = values[i]
        if re.fullmatch(r"-{2,}|—|–|", raw):      # «---» — у фонда ещё нет такого периода
            continue
        value = pct(raw)
        if value is not None:
            out.append(OfficialReturn(period, value, as_of, source_url))
    return out


def _cell(td) -> str:
    for t in td.select(".t-mobil-title"):
        t.decompose()
    return _clean(td.get_text(" "))


def _currency(label: str) -> str:
    s = label.lower()
    require("тыс" not in s and "млн" not in s, f"значения не в рублях, а в «{label}»")
    for marker, code in (("руб", "RUB"), ("долл", "USD"), ("юан", "CNY"), ("евро", "EUR")):
        if marker in s:
            return code
    raise UnknownFormat(f"не понял валюту из заголовка {label!r}")


def _nav_table(soup: BeautifulSoup):
    """Таблица СЧА и индексы колонок (дата расчёта, РСИП, СЧА) по заголовкам."""
    for tr in soup.select("tr.first-row-orange"):
        heads = [_clean(td.get_text(" ")) for td in tr.find_all("td", recursive=False)]
        low = [h.lower() for h in heads]
        if any("дата расчета" in h for h in low):
            idx = {}
            for i, h in enumerate(low):
                if "дата расчета" in h:
                    idx["date"] = i
                elif "(рсип)" in h and "изменение" not in h:
                    idx["price"] = i
                elif "(сча)" in h and "изменение" not in h:
                    idx["nav"] = i
            require(set(idx) == {"date", "price", "nav"}, f"в таблице СЧА не найдены колонки: {heads}")
            return tr.find_parent("table"), tr, idx, heads
    raise UnknownFormat("нет таблицы «Дата расчета / РСИП / СЧА»")


def nav_currency_region(html: str) -> str:
    """Валюта пая из заголовков таблицы СЧА («…(РСИП), руб.»); СЧА должна быть в той же валюте."""
    _, _, idx, heads = _nav_table(BeautifulSoup(html, "lxml"))
    cur = _currency(heads[idx["price"]])
    require(_currency(heads[idx["nav"]]) == cur, f"СЧА и пай в разных валютах: {heads}")
    return cur


def parse_nav_region(html: str) -> list[SeriesPoint]:
    """Таблица «Стоимость пая и СЧА» (страница фонда или html из /ajax/table.php) -> ряд по возрастанию дат."""
    soup = BeautifulSoup(html, "lxml")
    table, head_tr, idx, heads = _nav_table(soup)
    require(_currency(heads[idx["price"]]) == _currency(heads[idx["nav"]]), f"СЧА и пай в разных валютах: {heads}")
    need = max(idx.values())
    points: dict[date, SeriesPoint] = {}
    for tr in table.find_all("tr"):
        if tr is head_tr:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) <= need:
            # подзаголовок «За 20 / 250 рабочих дней» у фондов с индексом — без дат
            row = _clean(tr.get_text(" "))
            require(not DATE_RE.search(row), f"строка с датой без нужных колонок: {row[:120]}")
            continue
        raw_date = _cell(tds[idx["date"]])
        require(bool(DATE_RE.fullmatch(raw_date)), f"в колонке «Дата расчета» не дата: {raw_date!r}")
        d = day(raw_date)
        price, nav = num(_cell(tds[idx["price"]])), num(_cell(tds[idx["nav"]]))
        if price is None and nav is None:
            continue
        if price is not None and nav is not None:
            require(nav >= price, f"{d}: СЧА {nav} меньше РСИП {price} — перепутаны колонки?")
        require(d not in points, f"дата расчёта {d} повторяется в таблице")
        points[d] = SeriesPoint(d, nav, price)
    return [points[d] for d in sorted(points)]


def parse_nav_ajax_region(content: bytes | str | dict) -> list[SeriesPoint]:
    """Ответ /ajax/table.php: {"html": "<таблица за год>"}."""
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as e:
            raise UnknownFormat(f"ответ /ajax/table.php не JSON: {e}") from e
    require_keys(content, ["html"], "/ajax/table.php")
    require(isinstance(content["html"], str), "/ajax/table.php: html не строка")
    return parse_nav_region(content["html"])


def _rub_kop(s: str) -> float:
    """'32 руб. 26 коп.' -> 32.26."""
    m = re.fullmatch(r"(\d[\d\s]*)\s*руб\.?(?:\s*(\d{1,2})\s*коп\.?)?", _clean(s))
    require(m is not None, f"не понял сумму {s!r}")
    return int(re.sub(r"\s", "", m.group(1))) + int(m.group(2) or 0) / 100


def parse_payouts_region(html: str, source_url: str) -> tuple[list[Payout], list[tuple[date | None, str]]]:
    """«Сообщение о выплате дохода»: выплаты на пай из текстовых сообщений
    и (дата публикации, ссылка) сообщений, опубликованных только PDF-файлом."""
    soup = BeautifulSoup(html, "lxml")
    payouts: dict[date, Payout] = {}
    pdf_only: list[tuple[date | None, str]] = []
    for msg in soup.select("div.o-message"):
        title = msg.select_one(".o-message__title")
        body = msg.select_one(".o-message__text")
        if title is None or body is None or not re.search(r"о\s+выплате\s+дохода", title.get_text(), re.I):
            continue
        amount = when = None
        for tr in body.select("tr"):
            cells = [_clean(td.get_text(" ")) for td in tr.find_all(["th", "td"])]   # подпись — в <th>
            if len(cells) < 2:
                continue
            label = cells[0].lower()
            if "приходящегося на один инвестиционный пай" in label:
                amount = _rub_kop(cells[1])
            elif label.startswith("дата, на которую") and "выплачивается доход" in label:
                require(bool(DATE_RE.fullmatch(cells[1])), f"дата выплаты дохода не дата: {cells[1]!r}")
                when = day(cells[1])
        if amount is None and when is None:
            links = [urljoin(BASE, a["href"]) for a in body.find_all("a", href=True)
                     if a["href"].lower().endswith(".pdf")]
            if links:
                pub = msg.select_one(".o-message__date")
                m = DATE_RE.search(pub.get_text()) if pub else None
                pdf_only.extend((day(m.group(0)) if m else None, link) for link in links)
                continue
            raise UnknownFormat(f"сообщение о выплате без суммы и без PDF: {_clean(title.get_text())[:100]}")
        require(amount is not None and when is not None,
                f"в сообщении о выплате нет суммы на пай или даты: {_clean(title.get_text())[:100]}")
        require(when not in payouts or payouts[when].amount == amount, f"две разные выплаты на {when}")
        payouts[when] = Payout(when, amount, source_url)
    return [payouts[d] for d in sorted(payouts)], pdf_only


class RegionAdapter(Adapter):
    key = "region"
    title = "РЕГИОН Эссет Менеджмент"
    uk_inns = ("7730149408",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        state: dict = {}

        def catalog() -> dict[str, str]:
            if "data" not in state:
                try:
                    doc = ctx.http.get(CATALOG_URL, fmt="html", funds=[f.rules for f in funds])
                    state["data"] = parse_catalog_region(doc.text())
                except Exception as e:  # noqa: BLE001 — ошибка списка становится статусом каждого фонда
                    state["data"] = e
            if isinstance(state["data"], Exception):
                raise state["data"]
            return state["data"]

        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, catalog())) for f in funds]

    def _find_page(self, ctx: Context, fund: Fund, cat: dict[str, str]):
        """Страница фонда: сначала та, что совпала по названию, затем остальные — решает номер правил.

        Страницы кэширует Fetcher, так что перебор для следующих фондов в сеть не ходит.
        Сбой чужой страницы поиск не прерывает; если фонд так и не найден — поднимаем первый сбой
        (статус unavailable/parse_error честнее, чем not_found).
        """
        key = norm_text(fund.name)
        ordered = sorted(cat, key=lambda url: norm_text(cat[url]) != key)
        first_error: Exception | None = None
        for url in ordered:
            try:
                doc = ctx.http.get(url, fmt="html", funds=[fund.rules])
                html = doc.text()
                page = parse_fund_page_region(html)
            except (FetchError, UnknownFormat) as e:
                first_error = first_error or e
                continue
            if page.rules == norm_rules(fund.rules):
                return url, doc, html, page
            if norm_text(cat[url]) == key:
                ctx.log.warning("[region] %s: страница «%s» с похожим названием, но правила №%s",
                                fund.rules, cat[url], page.rules)
        if first_error is not None:
            raise first_error
        return None

    def _fund(self, ctx: Context, fund: Fund, cat: dict[str, str]) -> FundData:
        found = self._find_page(ctx, fund, cat)
        if found is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"ни на одной странице раскрытия УК нет правил № {fund.rules}")
        url, doc, html, page = found
        fd = FundData(fund.rules, currency=nav_currency_region(html), page_url=url,
                      official_returns=parse_returns_region(html, url))

        # Текущий год — в самой странице, прошлые годы — через /ajax/table.php.
        start = ctx.start_for(fund.rules)
        current = parse_nav_region(html)
        cur_years = {p.date.year for p in current}
        require(len(cur_years) <= 1, f"в таблице на странице несколько лет: {sorted(cur_years)}")
        seg = self.segment(doc, current, page_url=url, since=ctx.since)
        if seg.points:
            fd.segments.append(seg)
        for year in page.years:
            if year < start.year or year in cur_years:
                continue
            ydoc = ctx.http.get(TABLE_URL, fmt="json", funds=[fund.rules], headers=AJAX_HEADERS,
                                params={"year": year, "idPage": page.page_id, "idTab": page.tab_id,
                                        "idSchaTpl": page.scha_tpl})
            points = parse_nav_ajax_region(ydoc.content())
            bad = sorted({p.date.year for p in points} - {year})
            require(not bad, f"в таблице за {year} год есть даты других лет: {bad}")
            yseg = self.segment(ydoc, points, page_url=url, since=ctx.since)
            if yseg.points:
                fd.segments.append(yseg)

        # Выплаты дохода: официальная доходность УК их не учитывает (см. «Ловушки»), поэтому
        # в payouts не кладём, а сообщаем, что фонд платит доход и где это опубликовано.
        payouts, pdf_only = parse_payouts_region(html, url)
        payouts = [p for p in payouts if p.date >= ctx.since]
        pdf_only = [link for published, link in pdf_only if published is None or published >= ctx.since]
        if payouts or pdf_only:
            known = "; ".join(f"{p.amount:.2f} {fd.currency} на пай на {p.date:%d.%m.%Y}" for p in payouts)
            fd.warnings.append(
                "фонд выплачивает доход пайщикам; официальная доходность УК — изменение стоимости пая "
                "без учёта выплат, поэтому payouts не заполнены"
                + (f"; в тексте сообщений: {known}" if known else "")
                + (f"; сообщения только в PDF: {', '.join(pdf_only)}" if pdf_only else ""))

        points = [p for s in fd.segments for p in s.points]
        if not points:
            fd.status, fd.reason = "partial", f"в таблицах УК нет точек после {ctx.since}"
        elif all(p.nav is None for p in points):
            fd.status, fd.reason = "partial", "УК публикует только стоимость пая, без СЧА"
        elif all(p.price is None for p in points):
            fd.status, fd.reason = "partial", "УК публикует только СЧА, без стоимости пая"
        return fd
