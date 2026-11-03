"""ООО УК «Профицит» (myproficit.ru, Омск) — один розничный фонд: ОПИФ РФИ «Профицит» (правила 6492).

Источники (WordPress):
* https://myproficit.ru/lk_fund_values/ — CSV, из которого страница фонда рисует график и таблицу
  (перенаправляет на /wp-content/uploads/ГГГГ/ММ/lk_fund_values.csv): «pif_id;date;value;scha_value»,
  pif_id — номер правил фонда (6492; 7127 — ИПИФ «Хедж-фонд Профицит» для квалов), date — ГГГГ-ММ-ДД,
  value — расчётная стоимость пая, scha_value — СЧА. Каждый рабочий день с 02.10.2024 (дата
  завершения формирования), старые сверху. СЧА и пай — в рублях: на 30.09.2026 пай 1 015,70 и
  СЧА 19 662 055,77 руб. — как в КИД. Диапазона дат нет, файл всегда целиком (~20 КБ).
* https://myproficit.ru/proficit_fund/ — страница фонда для человека; её скрипт фильтрует CSV по
  pif_id == 6492 (так сверяем страницу с фондом); вкладка документов — ежемесячные КИД (PDF).
  Блок «Доходность фонда за период» на странице считается браузером из того же CSV (1 и 3 мес,
  с начала года) — это не опубликованные УК числа, не берём.

Официальная доходность 1/3/6/12 мес — в КИД (PDF, раздел 5 «Основные результаты
инвестирования», «по состоянию на 30 сентября 2026»). Текст PDF достаём pypdf, если он установлен
(в зависимостях проекта его нет): без него official_returns пустой + предупреждение со ссылкой.
Доход фонда не выплачивается, а капитализируется (так в КИД) — payouts не нужны.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import re
from datetime import date

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import day, num, pct, require
from .base import Adapter, Context

BASE = "https://myproficit.ru"
CSV_URL = f"{BASE}/lk_fund_values/"
FUND_URL = f"{BASE}/proficit_fund/"

_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_KID_PERIODS = {"1m": r"1\s*месяц\w*", "3m": r"3\s*месяц\w*", "6m": r"6\s*месяц\w*", "1y": r"1\s*год"}
_KID_TITLE = re.compile(r"Ключевой информационный документ\s*(\d{2}\.\d{2}\.\d{4})")


# ---------- разбор ----------

def parse_csv(content) -> dict[str, list[SeriesPoint]]:
    """lk_fund_values.csv -> {pif_id (номер правил): точки по возрастанию даты}."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    rows = [r for r in csv.reader(io.StringIO(content.lstrip("﻿")), delimiter=";") if any(c.strip() for c in r)]
    require(bool(rows), "пустой CSV")
    head = [c.strip() for c in rows[0]]
    for col in ("pif_id", "date", "value", "scha_value"):
        require(col in head, f"в шапке CSV нет колонки {col!r}: {head}")
    i_id, i_d, i_p, i_n = (head.index(c) for c in ("pif_id", "date", "value", "scha_value"))
    out: dict[str, dict[date, SeriesPoint]] = {}
    for r in rows[1:]:
        require(len(r) == len(head), f"строка CSV не по шапке: {r}")
        d = day(r[i_d])
        price, nav = num(r[i_p]), num(r[i_n])
        price = price if price and price > 0 else None
        nav = nav if nav and nav > 0 else None
        if price is None and nav is None:
            continue
        p = SeriesPoint(d, nav, price)
        series = out.setdefault(r[i_id].strip(), {})
        require(series.get(d, p) == p, f"фонд {r[i_id]}: две разные строки на {d}")
        series[d] = p
    require(bool(out), "в CSV нет ни одной строки с данными")
    return {k: [v[d] for d in sorted(v)] for k, v in out.items()}


def parse_fund_page(content) -> tuple[set[str], list[tuple[date, str]]]:
    """Страница фонда -> (pif_id из скрипта графика, [(дата КИД, ссылка)] свежие первыми)."""
    html = content.decode("utf-8") if isinstance(content, bytes) else content
    ids = set(re.findall(r"pif_id\s*==\s*['\"]?(\d+)", html)) | set(re.findall(r"row\[0\]\s*===\s*['\"](\d+)['\"]", html))
    require(bool(ids), "в скриптах страницы фонда нет фильтра pif_id — вёрстка изменилась?")
    kids: list[tuple[date, str]] = []
    s = BeautifulSoup(html, "lxml")
    for node in s.find_all(string=_KID_TITLE):
        row = node.find_parent(class_="vc_row")
        a = row.find("a", href=True) if row is not None else None
        if a is not None and a["href"].lower().endswith(".pdf"):
            kids.append((day(_KID_TITLE.search(str(node)).group(1)), a["href"]))
    # Свежие первыми; при равной дате — порядок страницы (сверху исправленная версия).
    kids.sort(key=lambda k: k[0], reverse=True)
    return ids, kids


def parse_kid_text(text: str, source_url: str) -> list[OfficialReturn]:
    """Текст КИД -> доходность 1/3/6/12 мес. Строка: «1 месяц -0,87 -0,95» (фонд, отклонение от инфляции)."""
    t = re.sub(r"[ \t\xa0]+", " ", text or "")
    m = re.search(r"по состоянию на\s*(\d{1,2})\s+([а-я]+)\s+(\d{4})", t)
    require(m is not None and m.group(2) in _MONTHS, "в КИД нет «по состоянию на ДД месяца ГГГГ»")
    as_of = date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    i = t.find("Основные результаты инвестирования")
    require(i >= 0, "в КИД нет раздела «Основные результаты инвестирования»")
    sect = t[i:i + 3000]
    out = []
    for period, label in _KID_PERIODS.items():
        mm = re.search(rf"(?m)^\s*{label}\s+(?:([-−]?\d+(?:[.,]\d+)?)\s*%?|-(?=\s|$))", sect)
        require(mm is not None, f"в разделе доходности КИД нет строки периода {period}")
        v = pct(mm.group(1).replace("−", "-")) if mm.group(1) else None
        if v is not None:
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def has_pypdf() -> bool:
    return importlib.util.find_spec("pypdf") is not None


def kid_pdf_text(pdf: bytes) -> str:
    """Текст КИД из PDF. pypdf — необязательная зависимость: импорт здесь."""
    from pypdf import PdfReader  # noqa: PLC0415

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


# ---------- адаптер ----------

class ProficitAdapter(Adapter):
    key = "proficit"
    title = "УК «Профицит»"
    uk_inns = ("5501283302",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def data():
            if "csv" not in state:
                raw = ctx.http.get(CSV_URL, fmt="csv", funds=all_rules)
                state["csv"] = (raw, parse_csv(raw.content()))
            return state["csv"]

        def page():
            if "page" not in state:
                try:
                    state["page"] = parse_fund_page(ctx.http.get(FUND_URL, fmt="html", funds=all_rules).content())
                except Exception as e:  # noqa: BLE001 — страница нужна для ссылки и КИД, ряд есть и без неё
                    ctx.log.warning("[proficit] страница фонда: %s", e)
                    state["page"] = e
            return state["page"]

        def one(fund: Fund) -> FundData:
            raw, series = data()
            if fund.rules not in series:
                return FundData(fund.rules, status="not_found", page_url=FUND_URL,
                                reason=f"в {CSV_URL} нет строк с pif_id = {fund.rules} (есть {sorted(series)})")
            pg = page()
            page_url = FUND_URL if not isinstance(pg, Exception) and fund.rules in pg[0] else None
            fd = FundData(fund.rules, currency="RUB", page_url=page_url or BASE + "/",
                          segments=[self.segment(raw, series[fund.rules], page_url=page_url, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status, fd.reason = "no_history", f"в {CSV_URL} нет точек с {ctx.since:%d.%m.%Y}"
            if isinstance(pg, Exception):
                fd.warnings.append(f"страница фонда {FUND_URL} не получена/не разобрана: {pg}")
                return fd
            if page_url is None:
                fd.warnings.append(f"страница {FUND_URL} показывает pif_id {sorted(pg[0])}, а не {fund.rules}")
                return fd
            self._returns(ctx, fund, fd, pg[1])
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]

    @staticmethod
    def _returns(ctx: Context, fund: Fund, fd: FundData, kids: list[tuple[date, str]]) -> None:
        if not kids:
            fd.warnings.append(f"на {FUND_URL} нет КИД — официальная доходность не заполнена")
            return
        url = kids[0][1]
        if not has_pypdf():
            fd.warnings.append(f"доходность за 1/3/6/12 мес УК публикует только в КИД (PDF), а pypdf "
                               f"не установлен — не заполнена; КИД: {url}")
            return
        try:
            raw = ctx.http.get(url, fmt="pdf", funds=[fund.rules])
            fd.official_returns = parse_kid_text(kid_pdf_text(raw.content()), url)
        except Exception as e:  # noqa: BLE001 — ряд важнее: сбой КИД — предупреждение
            ctx.log.warning("[proficit] %s: КИД %s: %s", fund.rules, url, e)
            fd.warnings.append(f"КИД {url} не разобран: {e}")
