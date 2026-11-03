"""ООО «УК «Восток-Запад» (ew-mc.ru, Битрикс): ряд СЧА и пая — массивы графика на странице фонда,
доходность — КИД (PDF).

Источники (сайт УК; www.ew-mc.ru перенаправляет на ew-mc.ru, поэтому ходим сразу туда):
* https://ew-mc.ru/funds/ — «Паевые фонды»: карточки .fund с названием (.fund-name, «ОПИФ рыночных
  финансовых инструментов «Первый»») и ссылкой «Подробнее» на страницу фонда;
* https://ew-mc.ru/funds/<slug>/ — страница фонда. В ней сервер сам пишет данные графиков
  «Стоимость пая и стоимость чистых активов» в JS: lineCurr = 'RUB'; shareRows = [[new Date(Г, М, Д),
  цена], …]; chaRows = [[new Date(Г, М, Д), СЧА], …] — вся история фонда по рабочим дням.
  Месяц в new Date() — с нуля (JS): new Date(2026, 10, 02) — это 02.11.2026 (сверено с таблицей
  «Сообщение о стоимости чистых активов…» на той же странице и с КИД). Цена пая — в валюте lineCurr,
  СЧА — в рублях, не в тысячах (таблица: «Стоимость чистых активов, RUB 67 867 794.76»).
  Там же «Сведения о фонде»: «Дата регистрации и Регистрационный номер фонда 12.11.2019 № 3902»,
  и раздел «Ключевой информационный документ» — КИД (PDF) за каждый месяц.
  Диапазон дат страница не принимает — берём целиком и режем по ctx.since.
* КИД https://ew-mc.ru/upload/iblock/<xxx>/KID_<правила>_<ГГГГММДД>.pdf, раздел 5 «Основные результаты
  инвестирования»: 1 месяц / 3 месяца / 6 месяцев / 1 год («-**» — УК не приводит значение).
  Других публикаций доходности за эти периоды нет: селектор «1 месяц / 3 месяца …» под графиком
  только меняет окно графика. Текст из PDF достаём pypdf (его нет в зависимостях pyproject);
  без него доходность не собирается, а ряд — собирается.
Выплаты дохода: по КИД обоих фондов правилами выплата дохода по паям не предусмотрена — payouts пустые.
Сопоставление: по названию в «» из списка фондов (norm_text), затем сверка номера правил на странице фонда.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://ew-mc.ru"
FUNDS_URL = f"{BASE}/funds/"
# КИД по ссылке не меняется (в имени файла — дата) — повторно не перекачиваем
KID_FRESH_HOURS = 24 * 30

_CURRENCIES = ("RUB", "USD", "EUR", "CNY")
_KID_PERIODS = (("1 месяц", "1m"), ("3 месяца", "3m"), ("6 месяцев", "6m"), ("1 год", "1y"))
_ROW = re.compile(r"\[\s*new Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*,\s*([^\],\s]+)\s*\]")
_EMPTY_ROW = re.compile(r"\[\s*new Date\(\s*,\s*-1\s*,\s*\)\s*,\s*\]")
_AMOUNT = r"(\d[\d  ]*(?:[.,]\d+)?)\s*руб\.?\s*на\s*(\d{2}\.\d{2}\.\d{4})"


@dataclass
class FundPage:
    rules: str
    currency: str
    points: list[SeriesPoint]
    kids: list[tuple[date, str]] = field(default_factory=list)   # (дата КИД, адрес PDF), свежие первыми


@dataclass
class Kid:
    doc_date: date                        # «по состоянию на» из раздела 1
    rules: str | None                     # раздел 7: «№ 3902 от 12.11.2019»
    price: float | None                   # «Расчетная стоимость инвестиционного пая … руб. на <дата>»
    nav: float | None                     # «Стоимость чистых активов … руб. на <дата>»
    data_date: date | None                # дата, на которую приведены цена и СЧА
    returns: dict[str, float | None]      # 1m / 3m / 6m / 1y


# ---------- чистые разборщики ----------

def _fund_title(name: str) -> str:
    """'ОПИФ рыночных финансовых инструментов «Первый»' -> 'Первый' (текст в первых «» до конца)."""
    m = re.search(r"«(.+)»", name)
    return m.group(1) if m else name


def parse_fund_list(html: bytes | str) -> dict[str, str]:
    """Страница /funds/ -> {norm_text(название в «»): адрес страницы фонда}."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for card in soup.select("div.fund"):
        name = card.select_one(".fund-name")
        a = card.select_one("a.button[href]")
        if name is None or a is None or not a["href"].startswith("/funds/"):
            continue
        url = urljoin(BASE, a["href"].rstrip("/") + "/")
        out.setdefault(norm_text(_fund_title(name.get_text(" ", strip=True))), url)
    require(len(out) >= 2, "список фондов: не нашёл карточек .fund с .fund-name и ссылкой /funds/<slug>")
    return out


def _js_array(html: str, name: str) -> str:
    m = re.search(rf"\b{name}\s*=\s*\[(.*?)\]\s*;", html, re.S)
    require(m is not None, f"страница фонда: нет массива графика {name} = [...];")
    return m.group(1)


def _js_rows(body: str, name: str) -> dict[date, float | None]:
    """'[new Date(2026, 10, 02), 8999.11] ,…' -> {date: значение}. Месяц в JS — с нуля."""
    rows = _ROW.findall(body)
    # Пустая запись шаблона УК без даты и значения: «[new Date(, -1, ), ]» (бывает первой строкой
    # у молодых фондов; браузер рисует её как Invalid Date) — не точка, пропускаем.
    # Всё, кроме известных строк и запятых между ними, — незнакомый формат.
    rest = _EMPTY_ROW.sub("", _ROW.sub("", body))
    require(re.fullmatch(r"[\s,]*", rest) is not None,
            f"{name}: незнакомая строка графика {rest.strip(' ,')[:60]!r} "
            "(ожидалось [new Date(Г, М, Д), число])")
    out: dict[date, float | None] = {}
    for y, m, d, v in rows:
        try:
            dt = date(int(y), int(m) + 1, int(d))
        except ValueError as e:
            raise UnknownFormat(f"{name}: не дата new Date({y}, {m}, {d})") from e
        require(dt not in out, f"{name}: дата {dt} повторяется")
        value = num(v)
        out[dt] = value or None          # 0 — «значения нет», а не ноль
    return out


def parse_chart(html: bytes | str) -> list[SeriesPoint]:
    """JS графиков страницы фонда -> точки (цена пая — shareRows, СЧА — chaRows)."""
    text = html.decode("utf-8") if isinstance(html, bytes) else html
    prices = _js_rows(_js_array(text, "shareRows"), "shareRows")
    navs = _js_rows(_js_array(text, "chaRows"), "chaRows")
    return [SeriesPoint(d, navs.get(d), prices.get(d)) for d in sorted(set(prices) | set(navs))]


def parse_last_rows(soup: BeautifulSoup) -> tuple[str, list[tuple[date, float | None, float | None]]]:
    """Таблица «Сообщение о стоимости чистых активов…» (последние дни) -> (валюта, [(дата, цена, СЧА)])."""
    h2 = next((h for h in soup.find_all("h2")
               if h.get_text(strip=True) == "Стоимость пая и стоимость чистых активов"), None)
    require(h2 is not None, "страница фонда: нет раздела «Стоимость пая и стоимость чистых активов»")
    table = h2.find_next("div", class_="requisites")
    require(table is not None, "страница фонда: в разделе СЧА и пая нет таблицы последних дней")
    head = [c.get_text(" ", strip=True) for c in table.select(".row .cell.title2")]
    require(len(head) == 3 and head[0] == "Дата" and head[1].startswith("Стоимость пая,")
            and head[2].startswith("Стоимость чистых активов,"),
            f"страница фонда: незнакомая шапка таблицы СЧА и пая {head}")
    cur = head[1].rsplit(",", 1)[1].strip().upper()
    require(cur in _CURRENCIES and head[2].endswith("RUB"),
            f"страница фонда: незнакомые единицы в шапке {head}")
    rows = []
    for row in table.select(".row.top5"):
        cells = row.select(".cell")
        require(len(cells) == 3, "страница фонда: в строке таблицы СЧА и пая не 3 ячейки")
        vals = []
        for c in cells[1:]:
            for span in c.select("span"):   # «-1.53%» — изменение к прошлому дню
                span.decompose()
            vals.append(num(c.get_text(" ", strip=True)))
        rows.append((day(cells[0].get_text(strip=True)), vals[0], vals[1]))
    return cur, rows


def parse_fund_page(html: bytes | str) -> FundPage:
    """Страница фонда -> номер правил, валюта, ряд и список КИД.

    Ряд сверяется с таблицей последних дней на той же странице: расхождение значит, что
    поменялся формат (например, единицы СЧА) — это UnknownFormat, а не тихая ошибка в данных."""
    text = html.decode("utf-8") if isinstance(html, bytes) else html
    soup = BeautifulSoup(text, "lxml")

    rules = None
    for row in soup.select(".requisites .row"):
        cells = row.select(".cell")
        if len(cells) == 2 and cells[0].get_text(strip=True).startswith("Дата регистрации и Регистрационный"):
            m = re.search(r"№\s*([\d-]+)", cells[1].get_text(" ", strip=True))
            rules = m.group(1) if m else None
    require(rules is not None, "страница фонда: нет «Дата регистрации и Регистрационный номер фонда … № N»")

    m = re.search(r"\blineCurr\s*=\s*'(\w+)'", text)
    require(m is not None and m.group(1) in _CURRENCIES, "страница фонда: нет lineCurr = '<валюта>'")
    currency = m.group(1)
    points = parse_chart(text)
    # у фонда без точек (формируется) таблицы последних дней может не быть — сверять нечего
    head_cur, last = parse_last_rows(soup) if points else (currency, [])
    require(head_cur == currency, f"страница фонда: валюта графика {currency}, а в таблице {head_cur}")
    by_date = {p.date: p for p in points}
    for d, price, nav in last:
        p = by_date.get(d)
        if p is None:
            continue        # дня из таблицы нет на графике — не формат, а просто не попал в ряд
        require(p.price == price and p.nav == nav,
                f"страница фонда: таблица на {d} (пай {price}, СЧА {nav}) не совпала с графиком "
                f"({p.price}, {p.nav})")

    kids: list[tuple[date, bool, str]] = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        # ссылка-вкладка «Ключевой информационный документ» (href="6") — не документ
        if not label.startswith("Ключевой информационный документ") or not a["href"].startswith("/upload/"):
            continue
        dm = re.search(r"по состоянию на\s*(\d{2}\.\d{2}\.\d{4})", label)
        require(dm is not None and a["href"].lower().endswith(".pdf"),
                f"страница фонда: незнакомая ссылка на КИД {label!r} -> {a['href']!r}")
        kids.append((day(dm.group(1)), "исправлен" in label.lower(), urljoin(BASE, a["href"])))
    # за одну дату бывает исходный и исправленный КИД — берём исправленный
    kids.sort(key=lambda k: (k[0], k[1]), reverse=True)
    return FundPage(rules=rules, currency=currency, points=points,
                    kids=[(d, url) for d, _, url in kids])


def kid_pdf_text(content: bytes) -> str:
    """Текст КИД из PDF. pypdf нет в зависимостях проекта: без него -> ImportError."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # noqa: BLE001 — битый PDF не должен ронять ряд фонда
        raise UnknownFormat(f"КИД: не читается как PDF ({type(e).__name__}: {e})") from e


def _kid_value(token: str) -> float | None:
    """'1,48' -> 1.48; '-13,49' -> -13.49; '-**' / '-' — значение не приводится -> None."""
    if re.fullmatch(r"[-–—]?\**", token):
        return None
    return pct(token)


def parse_kid_text(text: str) -> Kid:
    """Текст КИД (как его отдаёт pypdf) -> дата, правила, цена пая, СЧА и доходность за 1/3/6/12 мес."""
    m = re.search(r"по\s+состоянию\s+на\s*(\d{2}\.\d{2}\.\d{4})", text)
    require(m is not None, "КИД: нет «по состоянию на ДД.ММ.ГГГГ»")
    doc_date = day(m.group(1))
    sec = re.search(r"Раздел\s+5\.(.*?)Раздел\s+6\.", text, re.S)
    require(sec is not None, "КИД: нет раздела 5 «Основные результаты инвестирования»")
    body = sec.group(1)
    require("Доходность" in body, "КИД: в разделе 5 нет таблицы доходности")
    returns: dict[str, float | None] = {}
    for label, code in _KID_PERIODS:
        r = re.search(rf"^\s*{label}\s+(\S+)", body, re.M)
        require(r is not None, f"КИД: нет строки «{label}» в таблице доходности")
        returns[code] = _kid_value(r.group(1))
    price = re.search(r"Расчетная\s+стоимость\s+инвестиционного\s+пая\s*" + _AMOUNT, body)
    nav = re.search(r"Стоимость\s+чистых\s+активов\s+паевого\s+инвестиционного\s+фонда\s*" + _AMOUNT, body)
    require(price is not None, "КИД: нет «Расчетная стоимость инвестиционного пая … руб. на ДД.ММ.ГГГГ»")
    rules = re.search(r"регистрации\s+правил\s+доверительного\s+управления.{0,80}?№\s*([\d-]+)", text, re.S)
    return Kid(doc_date=doc_date, rules=rules.group(1) if rules else None,
               price=num(price.group(1)), nav=num(nav.group(1)) if nav else None,
               data_date=day(price.group(2)), returns=returns)


# ---------- адаптер ----------

class EwMcAdapter(Adapter):
    key = "ew_mc"
    title = "ООО «УК «Восток-Запад»"
    uk_inns = ("6454074653",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # Список фондов качается внутри guarded: повторный get берётся из кэша,
        # а сбой превращается в статус фонда, а не роняет адаптер.
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        lst = ctx.http.get(FUNDS_URL, fmt="html", funds=[fund.rules])
        page_url = parse_fund_list(lst.content()).get(norm_text(fund.name))
        if not page_url:
            return FundData(fund.rules, status="not_found",
                            reason=f"на {FUNDS_URL} нет фонда с названием «{fund.name}»")
        raw = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
        page = parse_fund_page(raw.content())
        if page.rules != fund.rules:
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"на {page_url} правила № {page.rules}, а в реестре № {fund.rules}")
        seg = self.segment(raw, page.points, page_url=page_url, since=ctx.since)
        fd = FundData(fund.rules, currency=page.currency, page_url=page_url, segments=[seg])
        if not page.points:
            if fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", "фонд формируется: УК ещё не публикует СЧА и пай"
            else:
                fd.status, fd.reason = "partial", f"на {page_url} пустой график СЧА и пая"
            return fd
        if not seg.points:
            fd.status, fd.reason = "partial", f"УК не отдала точек с {ctx.since}"
        fd.official_returns = self._returns(ctx, fund, page, fd)
        return fd

    def _returns(self, ctx: Context, fund: Fund, page: FundPage, fd: FundData) -> list[OfficialReturn]:
        """Доходность из последнего КИД. Сбой КИД не отменяет ряд — только предупреждение."""
        if not page.kids:
            fd.warnings.append("на странице фонда нет КИД (PDF) — официальной доходности нет")
            return []
        url = page.kids[0][1]
        try:
            doc = ctx.http.get(url, fmt="pdf", funds=[fund.rules], fresh_hours=KID_FRESH_HOURS)
            kid = parse_kid_text(kid_pdf_text(doc.content()))
        except ImportError:
            fd.warnings.append("доходность есть только в КИД (PDF), а pypdf не установлен — не собрана")
            return []
        except (FetchError, UnknownFormat) as e:
            ctx.log.error("[%s] %s: КИД %s: %s", fund.uk_short, fund.rules, url, e)
            fd.warnings.append(f"КИД {url} не разобран: {e}")
            return []
        if kid.rules and kid.rules != fund.rules:
            fd.warnings.append(f"в КИД {url} правила № {kid.rules}, а в реестре № {fund.rules} — доходность не взята")
            return []
        as_of = kid.data_date or kid.doc_date
        p = next((p for p in page.points if p.date == as_of), None)
        if p is None or p.price is None or abs(p.price - kid.price) >= 0.005:
            fd.warnings.append(f"цена пая из КИД ({kid.price} на {as_of:%d.%m.%Y}) не совпала с рядом УК")
        out = [OfficialReturn(period=code, value_pct=v, as_of=as_of, source_url=url)
               for code, v in kid.returns.items() if v is not None]
        if not out:
            fd.warnings.append(f"в КИД на {kid.doc_date:%d.%m.%Y} доходность за 1–12 мес. ещё не приводится")
        return out
