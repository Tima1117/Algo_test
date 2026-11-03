"""АО «БКС Управление благосостоянием» (бывшая УК «Уралсиб»): CSV-выгрузка СЧА и пая с сайта УК.

Источники (www.bcswm.ru, Битрикс):
* список фондов:  https://www.bcswm.ru/funds/                  — HTML, ссылки /funds/<код>/ с названиями;
* страница фонда: https://www.bcswm.ru/funds/<код>/values/      — «Стоимость пая и СЧА»: внутренний id фонда
  (input#fund_id), номер правил в сноске, валюта в подписи строки, официальная «Динамика стоимости пая
  <дата>» за 1/3/6 мес и 1 год;
* ряд:            https://www.bcswm.ru/ajax/getCSV.php?fund_id=<id> — кнопка «Экспорт в excel»: CSV (cp1251)
  «Дата;СЧА, руб.;Стоимость пая, руб.» за всю историю, по рабочим дням. Параметры from/to сервер
  игнорирует (проверено: отдаёт всю историю), поэтому запрашиваем без них — адрес стабилен.
Сопоставление: по точному названию в списке фондов, затем сверка номера правил на странице фонда.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://www.bcswm.ru"
FUNDS_URL = f"{BASE}/funds/"
CSV_URL = f"{BASE}/ajax/getCSV.php"

_CURRENCY = {"руб": "RUB", "долл": "USD", "usd": "USD", "евро": "EUR", "eur": "EUR", "юан": "CNY", "cny": "CNY"}
_PERIODS = {"1 месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}
_DATE_ROW = re.compile(r"^\d{2}\.\d{2}\.\d{4};")
CSV_HEADER = ("Дата", "СЧА, руб.", "Стоимость пая, руб.")


# ---------- чистые разборщики ----------

def parse_fund_list(html: bytes | str) -> dict[str, str]:
    """Страница /funds/ -> {название фонда: адрес страницы «Стоимость пая и СЧА»}."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.fullmatch(r"/funds/([\w-]+)/", a["href"])
        name = a.get_text(" ", strip=True)
        if m and name and name not in out:
            out[name] = f"{BASE}/funds/{m.group(1)}/values/"
    require(len(out) >= 2, "список фондов: не нашёл ссылок вида /funds/<код>/ с названиями")
    return out


def _currency(label: str) -> str:
    """'Расчетная стоимость пая, руб.' -> 'RUB'."""
    tail = label.split(",")[-1].strip().lower()
    for k, v in _CURRENCY.items():
        if tail.startswith(k):
            return v
    raise UnknownFormat(f"страница фонда: не понял валюту пая в подписи {label!r}")


def parse_values_page(html: bytes | str, source_url: str) -> dict:
    """Страница «Стоимость пая и СЧА» -> {fund_id, rules, currency, returns}."""
    soup = BeautifulSoup(html, "lxml")
    inp = soup.find("input", id="fund_id")
    require(inp is not None and str(inp.get("value", "")).isdigit(),
            "страница фонда: нет input#fund_id с числовым id (нужен для выгрузки CSV)")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Правила зарегистрированы[^№]{0,40}№\s*([\d-]+)", text)
    require(m is not None, "страница фонда: не нашёл номер правил («Правила зарегистрированы … № …»)")
    label = next((td.get_text(" ", strip=True) for td in soup.select("section.fund-dynamics td")
                  if td.get_text(strip=True).startswith("Расчетная стоимость пая")), None)
    require(label is not None, "страница фонда: нет строки «Расчетная стоимость пая, <валюта>»")
    return {"fund_id": inp["value"], "rules": m.group(1), "currency": _currency(label),
            "returns": parse_returns(soup, source_url)}


def parse_returns(html, source_url: str) -> list[OfficialReturn]:
    """Таблица «Динамика стоимости пая <дата>»: 1 месяц / 3 месяца / 6 месяцев* / 1 год* / … .

    Пустая ячейка («%») — УК не публикует значение за период, пропускаем.
    """
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    title = next((p for p in soup.find_all("p")
                  if p.get_text(strip=True).startswith("Динамика стоимости пая")), None)
    if title is None:
        return []
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})", title.get_text())
    require(m is not None, f"доходность: нет даты в заголовке {title.get_text(strip=True)!r}")
    as_of = day(m.group(1))
    table = title.find_next("table")
    require(table is not None, "доходность: после заголовка нет таблицы")
    heads = [th.get_text(" ", strip=True).rstrip("*").strip() for th in table.select("thead th")]
    cells = [td.get_text(" ", strip=True) for td in table.select("tbody tr td")]
    require(len(heads) == len(cells) and "1 месяц" in heads and "1 год" in heads,
            f"доходность: незнакомая таблица, заголовки {heads}, ячеек {len(cells)}")
    out = []
    for head, cell in zip(heads, cells):
        period = _PERIODS.get(head)
        value = pct(cell)
        if period and value is not None:
            out.append(OfficialReturn(period, value, as_of, source_url))
    return out


def parse_csv(content: bytes | str) -> list[SeriesPoint]:
    """CSV «Дата;СЧА, руб.;Стоимость пая, руб.» -> точки (СЧА в рублях, цена в рублях).

    В файле бывают текстовые примечания (например, о дроблении паёв 04.06.2021) — строки без «;»
    пропускаем. Нули в первые дни после регистрации — «значения ещё нет», а не ноль: None.
    """
    text = content.decode("cp1251") if isinstance(content, bytes) else content
    lines = [ln.strip() for ln in text.splitlines()]
    require(bool(lines), "CSV: пустой файл")
    # Заголовок сверяем целиком: в нём и порядок колонок, и единицы (рубли, не тысячи).
    head = [h.strip() for h in lines[0].split(";")]
    require(head == list(CSV_HEADER),
            f"CSV: незнакомый заголовок {lines[0]!r}, ожидался {';'.join(CSV_HEADER)!r}")
    points = []
    for ln in lines[1:]:
        if not ln or ";" not in ln:
            continue                      # пустая строка или примечание УК
        require(bool(_DATE_ROW.match(ln)), f"CSV: строка не начинается с даты: {ln[:80]!r}")
        cols = ln.split(";")
        require(len(cols) == 3, f"CSV: ожидалось 3 колонки: {ln[:80]!r}")
        nav, price = num(cols[1]), num(cols[2])
        points.append(SeriesPoint(day(cols[0]), nav or None, price or None))
    require(bool(points), "CSV: нет ни одной строки с данными")
    return sorted(points, key=lambda p: p.date)


# ---------- адаптер ----------

class BcsWmAdapter(Adapter):
    key = "bcswm"
    title = "АО «БКС Управление благосостоянием»"
    uk_inns = ("7702172846",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        lst = ctx.http.get(FUNDS_URL, fmt="html", funds=[fund.rules])
        by_name = {norm_text(name): url for name, url in parse_fund_list(lst.content()).items()}
        page_url = by_name.get(norm_text(fund.name))
        if not page_url:
            return FundData(fund.rules, status="not_found",
                            reason=f"на {FUNDS_URL} нет фонда с названием «{fund.name}»")
        page = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
        info = parse_values_page(page.content(), page_url)
        if info["rules"] != fund.rules:
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"на {page_url} правила № {info['rules']}, а в реестре № {fund.rules}")
        raw = ctx.http.get(CSV_URL, params={"fund_id": info["fund_id"]}, fmt="csv", funds=[fund.rules])
        points = parse_csv(raw.content())
        # CSV всегда в рублях (заголовок проверен); пай в другой валюте здесь не разберём молча
        require(info["currency"] == "RUB",
                f"пай фонда в {info['currency']}, а выгрузка CSV — в рублях; нужен отдельный разбор")
        fd = FundData(fund.rules, currency=info["currency"], page_url=page_url,
                      segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)],
                      official_returns=info["returns"])
        if not fd.segments[0].points:
            fd.status, fd.reason = "partial", f"в выгрузке УК нет точек после {ctx.since}"
        return fd
