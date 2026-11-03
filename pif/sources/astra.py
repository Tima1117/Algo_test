"""АО «Астра Управление активами» (astra-am.ru) — один розничный фонд: ОПИФ РФИ «Астра - Ликвидность»
(правила 7938, сформирован 16.07.2026).

Источники (ProcessWire, обычный HTML):
* https://astra-am.ru/compliance/pif/ — «Раскрытие информации → ПИФ»: ссылки на страницы фондов
  (сейчас одна — «Астра - Ликвидность» → /compliance/astra-likvidnost/), ищем по названию (norm_text);
* https://astra-am.ru/compliance/astra-likvidnost/ — раскрытие фонда, аккордеон разделов:
  - «Сведения о фонде»: «Номер правил … и дата присвоения этого номера: 7938, 06.07.2026» — сверка
    с реестром; там же «ДИСКЛЕЙМЕР ОПИФ → Доходность ОПИФ» — HTML-таблица 1 мес. | 3 мес. | 6 мес. |
    1 год | 3 года | С начала года | С даты формирования и «Доходности представлены по состоянию на
    30.09.2026». Пустые ячейки — фонд моложе периода (сейчас заполнены только 1 мес. и с даты
    формирования): периода нет, а не ноль. Это официальная доходность — берём;
  - «Документы фонда», «КИД», «Отчётность», «Динамика стоимости пая и СЧА» — списки PDF
    («<strong>Название</strong><br> Дата раскрытия: ДД.ММ.ГГГГ …» + кнопка-ссылка).

Что УК публикует о СЧА и пае: ТОЛЬКО PDF — ежедневные «Сообщение о стоимости чистых активов ПИФ и
РСП» (раздел «Динамика стоимости пая и СЧА», по одному PDF на дату) и ежемесячные «Справка о
стоимости чистых активов» («Отчётность»). Таблицы, графика или выгрузки значений на сайте нет.
PDF адаптер не разбирает (PDF-библиотеки нет в зависимостях проекта), поэтому статус 'no_history' с
причиной «история только в PDF» и числом найденных сообщений; сегментов нет.

Сеть: с некоторых хостингов (в т. ч. из нашей песочницы) ответы astra-am.ru длиннее ~16 КБ
обрываются — соединение «замирает», сжатие сервер не включает. Тогда Fetcher даёт 'unavailable'.
Структура страницы фонда изучена по её копии в web.archive.org от 08.10.2026 (снимок «id_» — байты
ответа самого сайта); живой ответ совпадает с ней в первых 16 КБ.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn
from ..parsing import day, norm_text, pct, require
from .base import Adapter, Context

BASE = "https://astra-am.ru"
LIST_URL = f"{BASE}/compliance/pif/"

NAV_SECTION = "Динамика стоимости пая и СЧА"
REPORTS_SECTION = "Отчётность"

_DISCLOSED = re.compile(r"Дата раскрытия:\s*(\d{2}\.\d{2}\.\d{4})")
_PERIODS = {"1 мес.": "1m", "3 мес.": "3m", "6 мес.": "6m", "1 год": "1y"}
_FUND_HREF = re.compile(r"^(?:https?://(?:www\.)?astra-am\.ru)?/compliance/([a-z0-9-]+)/?$")


def _soup(content) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()


@dataclass
class Doc:
    title: str
    disclosed: date | None
    url: str


# ---------- разбор ----------

def parse_fund_list(content) -> dict[str, str]:
    """«Раскрытие → ПИФ» -> {название фонда: адрес страницы фонда}.

    Ссылки фондов — в блоке содержимого (div.content), меню раздела туда не входит.
    """
    s = _soup(content)
    out: dict[str, str] = {}
    for box in s.select("div.content"):
        for a in box.find_all("a", href=True):
            m = _FUND_HREF.match(a["href"].strip())
            name = _text(a)
            if m and name and m.group(1) != "pif":
                out[name] = f"{BASE}/compliance/{m.group(1)}/"
    require(bool(out), f"на {LIST_URL} в блоке содержимого нет ссылок на страницы фондов")
    return out


def parse_fund_page(content, page_url: str) -> dict[str, list[Doc]]:
    """Страница фонда -> {раздел аккордеона: [документы]}."""
    s = _soup(content)
    items = s.select("div.accordion-item")
    require(bool(items), "на странице фонда нет разделов-аккордеонов (div.accordion-item)")
    out: dict[str, list[Doc]] = {}
    for it in items:
        head = it.select_one(".accordion-header span")
        require(head is not None and it.select_one(".accordion-body") is not None,
                "раздел аккордеона без заголовка или содержимого")
        docs = []
        # Ссылки — из всего раздела: самое свежее «Сообщение о СЧА» сайт кладёт рядом с .accordion-body.
        for a in it.find_all("a", href=True):
            if a.find_parent(class_="accordion-header") is not None:
                continue
            box = a.find_parent(class_="min-h-[50px]") or a.find_parent("div")
            info = _text(box) if box is not None else ""
            m = _DISCLOSED.search(info)
            # Название — текст до «Дата раскрытия» (в одних разделах в <strong>, в других — просто текст).
            title = info[:m.start()].strip() if m else info
            docs.append(Doc(title, day(m.group(1)) if m else None, urljoin(page_url, a["href"])))
        out[_text(head)] = docs
    require(NAV_SECTION in out, f"на странице фонда нет раздела «{NAV_SECTION}»: {list(out)}")
    return out


def parse_rules(content) -> set[str]:
    """Номер правил из «Сведения о фонде» («… и дата присвоения этого номера: 7938, 06.07.2026»)
    и из дисклеймера («зарегистрированы Банком России … за № 7938»)."""
    text = _text(_soup(content))
    found = set(re.findall(r"дата присвоения этого номера:\s*(\d{4,})\s*,", text))
    found |= set(re.findall(r"зарегистрированы Банком России[^№]{0,40}№\s*(\d{4,})\b", text))
    require(bool(found), "на странице фонда нет номера правил («дата присвоения этого номера: …»)")
    return found


def parse_returns(content, source_url: str) -> list[OfficialReturn]:
    """«Доходность ОПИФ»: таблица 1 мес. | 3 мес. | 6 мес. | 1 год | … и «по состоянию на ДД.ММ.ГГГГ».

    Пустая ячейка — УК доходность за период не публикует (фонд моложе периода): периода нет, не ноль.
    """
    s = _soup(content)
    label = s.find(lambda t: t.name in ("strong", "b", "p", "h3", "h4") and _text(t) == "Доходность ОПИФ")
    require(label is not None, "на странице фонда нет блока «Доходность ОПИФ»")
    table = label.find_next("table")
    require(table is not None, "после «Доходность ОПИФ» нет таблицы")
    rows = [[_text(td) for td in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
    require(len(rows) >= 2 and len(rows[0]) == len(rows[1]), f"таблица доходности не «подписи + значения»: {rows[:2]}")
    note = table.find_next(string=re.compile(r"по состоянию на"))
    m = re.search(r"по состоянию на\s*(\d{2}\.\d{2}\.\d{4})", str(note or ""))
    require(m is not None, "под таблицей доходности нет «по состоянию на ДД.ММ.ГГГГ»")
    as_of = day(m.group(1))
    out = []
    for lbl, period in _PERIODS.items():
        require(lbl in rows[0], f"в таблице доходности нет колонки {lbl!r}: {rows[0]}")
        v = pct(rows[1][rows[0].index(lbl)])
        if v is not None:
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def describe_pdf_only(sections: dict[str, list[Doc]]) -> str:
    """Причина статуса: сколько PDF с СЧА и паем опубликовано и за какие даты раскрытия."""
    msgs = [d for d in sections.get(NAV_SECTION, []) if "стоимости чистых активов" in d.title.lower()]
    refs = [d for d in sections.get(REPORTS_SECTION, []) if d.title.lower().startswith("справка о стоимости чистых")]
    parts = ["УК публикует СЧА и расчётную стоимость пая только в PDF"]
    dated = sorted((d for d in msgs if d.disclosed), key=lambda d: d.disclosed)
    if msgs:
        span = f" (раскрыты {dated[0].disclosed:%d.%m.%Y}–{dated[-1].disclosed:%d.%m.%Y})" if dated else ""
        last = (dated[-1] if dated else msgs[0]).url
        parts.append(f"«Сообщение о СЧА ПИФ и РСП» — {len(msgs)} шт.{span}, последнее: {last}")
    if refs:
        parts.append(f"ежемесячные справки о СЧА — {len(refs)} шт.")
    parts.append("таблицы/графика/выгрузки значений на сайте нет, PDF не разбираем")
    return "; ".join(parts)


# ---------- адаптер ----------

class AstraAdapter(Adapter):
    key = "astra"
    title = "Астра Управление активами"
    uk_inns = ("7722691889",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def fund_list() -> dict[str, str]:
            if "list" not in state:
                state["list"] = parse_fund_list(ctx.http.get(LIST_URL, fmt="html", funds=all_rules).content())
            return state["list"]

        def one(fund: Fund) -> FundData:
            pages = fund_list()
            hits = [url for name, url in pages.items() if norm_text(name) == norm_text(fund.name)]
            require(len(hits) <= 1, f"фонду «{fund.name}» соответствуют несколько страниц: {hits}")
            if not hits:
                return FundData(fund.rules, status="not_found", page_url=LIST_URL,
                                reason=f"на {LIST_URL} нет фонда «{fund.name}» (есть: {', '.join(pages)})")
            page_url = hits[0]
            raw = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
            sections = parse_fund_page(raw.content(), page_url)
            rules = parse_rules(raw.content())
            if fund.rules not in rules:
                return FundData(fund.rules, status="not_found", page_url=page_url,
                                reason=f"на {page_url} фонд с правилами № {', '.join(sorted(rules))}, а не {fund.rules}")
            return FundData(fund.rules, currency="RUB", page_url=page_url,
                            status="no_history", reason=describe_pdf_only(sections),
                            official_returns=parse_returns(raw.content(), page_url))

        out = []
        for f in funds:
            fd = self.guarded(ctx, f, lambda f=f: one(f))
            fd.page_url = fd.page_url or LIST_URL
            if fd.status == "unavailable":
                fd.reason += ("; ответы astra-am.ru длиннее ~16 КБ из сети сборщика обрываются "
                              "(соединение замирает), страница фонда не дочитывается")
            out.append(fd)
        return out
