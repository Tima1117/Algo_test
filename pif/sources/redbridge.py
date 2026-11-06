"""ООО «УК «КРАСНЫЙ МОСТ» (ИНН 7838059090): CSV-архив СЧА и пая и доходность со страницы фонда.

Источники (сайт УК www.redbridge-am.com, Битрикс; обычный HTML и статичные файлы, без API):
* список фондов:  https://www.redbridge-am.com/foundations/            — HTML, плитки-ссылки
  /foundations/<код>/ с названиями («ОПИФ «КМ Меркури»», «ЗПИФ …»);
* страница фонда: https://www.redbridge-am.com/foundations/<код>/      — номер правил в строке
  «Правила доверительного управления (дата, №)»; ссылка «Архив значений стоимости инвестиционного пая
  и СЧА»; таблица «Стоимость пая и чистых активов на <дата> и <дата>» (два последних расчёта);
  официальная «Динамика стоимости пая на <дата>» за 1/3/6 мес и 1 год (дата — конец месяца,
  значения сверены с архивом до сотых процента);
* ряд:            https://www.redbridge-am.com/upload/iblock/<xxx>/<имя>.csv — тот самый «Архив»:
  CSV (cp1251) «Дата;Стоимость чистых активов;Стоимость 1 пая», от новых к старым, за всю историю
  фонда (с 2006–2007 гг.), каждый рабочий день. СЧА в рублях с копейками, пай в рублях. Имя файла
  Битрикс генерирует при перезаливке, поэтому адрес каждый раз берём со страницы фонда. Диапазон дат
  файл не принимает — качаем целиком (сервер отдаёт ETag/Last-Modified, повтор — условным запросом).
Сопоставление: по названию в списке фондов (norm_text), затем сверка номера правил на странице фонда.
Все запросы — на хост www (на него УК ссылается для файлов), чтобы ограничение «1 запрос/с»
действовало на весь сайт, а не отдельно на redbridge-am.com и www.redbridge-am.com.

Валюта: в CSV и таблицах валюта не подписана; суммы на странице фонда — в рублях, реквизиты для оплаты
паёв — рублёвый счёт, поэтому пай — RUB.
Выплаты дохода (payouts): правила обоих ОПИФ выплату дохода пайщикам не предусматривают — не заполняем.
Особенности архива: строка «28.02.2022;;» (расчёт не публиковался) пропускается; в архиве «КМ Меркури»
есть опечатка в годе («26.03.2025» между 27.03.2026 и 25.03.2026) — такую дату разборщик исправляет
только если год соседей ставит её ровно между ними, и пишет об этом в warnings фонда.

robots.txt (одинаковый на обоих хостах): закрыты /bitrix/, */index.php и служебные параметры
(PAGEN, print, action, bxajaxid, …); /foundations/ и /upload/ открыты, параметров в адресах нет.
За прогон — 5 запросов (список, две страницы фондов, два CSV).
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://www.redbridge-am.com"
FUNDS_URL = f"{BASE}/foundations/"

CSV_HEADER = ("Дата", "Стоимость чистых активов", "Стоимость 1 пая")
ARCHIVE_LINK = "Архив значений стоимости"
_PERIODS = {"1 месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}
_FUND_HREF = re.compile(r"^(?:https?://(?:www\.)?redbridge-am\.com)?/foundations/([\w-]+)/?$")
_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_CSV_ROW = re.compile(r"^(\d{2}\.\d{2}\.\d{4});([^;]*);([^;]*)$")


def _soup(html) -> BeautifulSoup:
    return html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")


def _cell(tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())


# ---------- чистые разборщики ----------

def parse_fund_list(html: bytes | str) -> dict[str, str]:
    """Страница /foundations/ -> {название фонда: адрес страницы фонда}.

    Берём только плитки списка (div.plates): в шапке сайта есть ещё ссылки «Описание фонда»,
    «Документы» и т. п. на страницу первого фонда."""
    out: dict[str, str] = {}
    for a in _soup(html).select(".plates a[href]"):
        m = _FUND_HREF.match(a["href"].strip())
        name = _cell(a)
        if m and name and name not in out:
            out[name] = f"{BASE}/foundations/{m.group(1)}/"
    require(len(out) >= 2, "список фондов: не нашёл плиток .plates со ссылками /foundations/<код>/")
    return out


def parse_returns(html, source_url: str) -> list[OfficialReturn]:
    """Таблица «Динамика стоимости пая на <дата>»: с начала года / 1 месяц / … / 5 лет.

    Блока нет — [] (это не ошибка, adapter пишет warning); блок есть, но незнакомый — UnknownFormat.
    Закомментированные старые таблицы (2017 г.) в HTML страницы — комментарии, BeautifulSoup их не видит.
    """
    soup = _soup(html)
    title = next((h for h in soup.find_all("h3") if _cell(h).startswith("Динамика стоимости пая")), None)
    if title is None:
        return []
    m = _DATE.search(_cell(title))
    require(m is not None, f"доходность: нет даты в заголовке {_cell(title)!r}")
    as_of = day(m.group(0))
    table = title.find_next("table")
    require(table is not None, "доходность: после заголовка нет таблицы")
    heads = [_cell(th) for th in table.select("thead th")]
    cells = [_cell(td) for td in table.select("tbody tr td")]
    require(len(heads) == len(cells) and "1 месяц" in heads and "1 год" in heads,
            f"доходность: незнакомая таблица, заголовки {heads}, ячеек {len(cells)}")
    out = []
    for head, cell in zip(heads, cells):
        period = _PERIODS.get(head)
        value = pct(cell)
        if period and value is not None:
            out.append(OfficialReturn(period, value, as_of, source_url))
    return out


def parse_latest(html) -> list[SeriesPoint]:
    """Таблица «Стоимость пая и чистых активов на <дата> и <дата>» -> две последние точки.

    У «КМ Меркури» справа ещё колонки отклонения от индекса — берём только колонки с датой в заголовке.
    """
    soup = _soup(html)
    title = next((h for h in soup.find_all("h3")
                  if _cell(h).startswith("Стоимость пая и чистых активов")), None)
    require(title is not None, "страница фонда: нет таблицы «Стоимость пая и чистых активов на …»")
    table = title.find_next("table")
    require(table is not None, "последние значения: после заголовка нет таблицы")
    heads = [_cell(th) for th in table.select("thead th")]
    cols = {i: day(h) for i, h in enumerate(heads) if _DATE.fullmatch(h)}
    rows = {}
    for tr in table.select("tbody tr"):
        cells = [_cell(td) for td in tr.find_all("td")]
        if cells and cells[0] in ("Стоимость 1 пая", "Стоимость чистых активов"):
            rows[cells[0]] = cells
    require(bool(cols) and len(rows) == 2,
            f"последние значения: незнакомая таблица, заголовки {heads}, строки {sorted(rows)}")
    price_row, nav_row = rows["Стоимость 1 пая"], rows["Стоимость чистых активов"]
    require(all(i < len(price_row) and i < len(nav_row) for i in cols),
            "последние значения: в строке меньше ячеек, чем колонок с датами")
    return sorted((SeriesPoint(d, num(nav_row[i]) or None, num(price_row[i]) or None)
                   for i, d in cols.items()), key=lambda p: p.date)


class ArchiveNotCsv(FetchError):
    """УК выложила архив значений не в CSV (например, картинкой) — ряд не обновить, прошлые точки остаются."""

    def __init__(self, url: str):
        ext = url.rsplit(".", 1)[-1].upper() if "." in url.rsplit("/", 1)[-1] else "не CSV"
        super().__init__(url, f"УК выложила архив значений стоимости пая и СЧА в формате {ext} вместо CSV — "
                              "машиночитаемых новых данных нет")


def parse_fund_page(html: bytes | str, source_url: str) -> dict:
    """Страница фонда -> {rules, csv_url, latest, returns}."""
    soup = _soup(html)
    rules = None
    for tr in soup.find_all("tr"):
        cells = [_cell(c) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 2 and cells[0].startswith("Правила доверительного управления"):
            m = re.search(r"№\s*(\d{4}-\d{8})", cells[1])
            rules = m.group(1) if m else None
            break
    require(rules is not None, "страница фонда: не нашёл номер правил в строке «Правила доверительного "
                               "управления (дата, №)»")
    archive = [a for a in soup.find_all("a", href=True) if _cell(a).startswith(ARCHIVE_LINK)]
    link = next((a for a in archive if a["href"].lower().endswith(".csv")), None)
    if link is None and archive and archive[0]["href"].lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "pdf"):
        # ссылка на архив есть, но ведёт не на CSV (06.11.2026 у «КМ Меркури» — на картинку JPG):
        # формат разбора не менялся, просто УК выложила архив немашиночитаемым — новых данных нет
        raise ArchiveNotCsv(urljoin(source_url, archive[0]["href"].strip()))
    require(link is not None, f"страница фонда: нет ссылки «{ARCHIVE_LINK} …» на CSV-файл")
    return {"rules": rules, "csv_url": urljoin(source_url, link["href"].strip()),
            "latest": parse_latest(soup), "returns": parse_returns(soup, source_url)}


def _fix_out_of_order(rows: list[tuple[int, date, str, str]], notes: list[str]) -> list:
    """Строки архива идут от новых к старым. Строка, выпавшая из порядка при согласных между собой
    соседях, — опечатка: если год соседей ставит дату ровно между ними, исправляем год, иначе строку
    пропускаем. Каждое такое решение — в notes."""
    out = []
    for i, (ln, d, nav, price) in enumerate(rows):
        prev = rows[i - 1][1] if i > 0 else None
        nxt = rows[i + 1][1] if i + 1 < len(rows) else None
        if prev is None or nxt is None or not prev > nxt or prev > d > nxt:
            out.append((ln, d, nav, price))
            continue
        fixed = None
        for year in {prev.year, nxt.year}:
            try:
                cand = d.replace(year=year)
            except ValueError:            # 29.02 в невисокосный год
                continue
            if prev > cand > nxt:
                fixed = cand
        where = f"строка {ln}: дата {d:%d.%m.%Y} стоит между {prev:%d.%m.%Y} и {nxt:%d.%m.%Y}"
        if fixed:
            notes.append(f"архив УК, {where} — опечатка в годе, принята как {fixed:%d.%m.%Y}")
            out.append((ln, fixed, nav, price))
        else:
            notes.append(f"архив УК, {where} — выпала из порядка, пропущена")
    return out


def parse_csv(content: bytes | str, notes: list[str] | None = None) -> list[SeriesPoint]:
    """CSV «Дата;Стоимость чистых активов;Стоимость 1 пая» -> точки (СЧА и пай в рублях).

    notes (если передан список) получает описания исправленных/пропущенных строк.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("cp1251")
    else:
        text = content
    lines = [ln.strip() for ln in text.lstrip("\ufeff").splitlines()]
    require(bool(lines) and bool(lines[0]), "CSV: пустой файл")
    # заголовок сверяем целиком: в нём порядок колонок (СЧА, потом пай)
    head = tuple(h.strip() for h in lines[0].split(";"))
    require(head == CSV_HEADER, f"CSV: незнакомый заголовок {lines[0]!r}, ожидался {';'.join(CSV_HEADER)!r}")
    rows = []
    for ln_no, ln in enumerate(lines[1:], start=2):
        if not ln:
            continue
        m = _CSV_ROW.match(ln)
        require(m is not None, f"CSV: строка {ln_no} не «дата;СЧА;пай»: {ln[:80]!r}")
        rows.append((ln_no, day(m.group(1)), m.group(2), m.group(3)))
    require(bool(rows), "CSV: нет ни одной строки с данными")
    rows = _fix_out_of_order(rows, notes if notes is not None else [])
    dates = [r[1] for r in rows]
    bad = [rows[i][0] for i in range(1, len(dates)) if not dates[i - 1] > dates[i]]
    require(not bad, f"CSV: даты не идут строго от новых к старым (строки {bad[:5]})")
    points = []
    for _, d, nav, price in rows:
        nav_v, price_v = num(nav) or None, num(price) or None
        if nav_v is None and price_v is None:
            continue                      # «28.02.2022;;» — расчёт за дату не публиковался
        points.append(SeriesPoint(d, nav_v, price_v))
    return sorted(points, key=lambda p: p.date)


# ---------- адаптер ----------

class RedBridgeAdapter(Adapter):
    key = "redbridge"
    title = "ООО «УК «КРАСНЫЙ МОСТ»"
    uk_inns = ("7838059090",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # список фондов качается внутри guarded: второй фонд берёт его из кэша Fetcher
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        lst = ctx.http.get(FUNDS_URL, fmt="html", funds=[fund.rules])
        by_name = {norm_text(name): url for name, url in parse_fund_list(lst.content()).items()}
        page_url = by_name.get(norm_text(fund.name))
        if not page_url:
            return FundData(fund.rules, status="not_found",
                            reason=f"на {FUNDS_URL} нет фонда с названием «{fund.name}»")
        page = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
        info = parse_fund_page(page.content(), page_url)
        if info["rules"] != fund.rules:
            return FundData(fund.rules, status="not_found", page_url=page_url,
                            reason=f"на {page_url} правила № {info['rules']}, а в реестре № {fund.rules}")
        raw = ctx.http.get(info["csv_url"], fmt="csv", funds=[fund.rules])
        notes: list[str] = []
        points = parse_csv(raw.content(), notes)
        fd = FundData(fund.rules, currency="RUB", page_url=page_url,
                      segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)],
                      official_returns=info["returns"], warnings=notes)
        self._check_latest(fd, page, points, info["latest"], page_url, ctx.since)
        missing = [p for p in _PERIODS.values() if p not in {r.period for r in fd.official_returns}]
        if missing:
            fd.warnings.append(f"на странице фонда нет официальной доходности за {', '.join(missing)}")
        if not any(s.points for s in fd.segments):
            fd.status, fd.reason = "partial", f"в архиве УК нет точек после {ctx.since}"
        return fd

    def _check_latest(self, fd: FundData, page, points: list[SeriesPoint], latest: list[SeriesPoint],
                      page_url: str, since: date) -> None:
        """Сверка архива с таблицей последних значений на странице фонда.

        Расхождение за одну дату — warning. Если архив отстаёт от таблицы, более свежие даты берём из
        таблицы отдельным сегментом (источник — сама страница фонда)."""
        by_date = {p.date: p for p in points}
        last = points[-1].date if points else None
        tail = [p for p in latest if last is None or p.date > last]
        for p in latest:
            have = by_date.get(p.date)
            if have is None and p not in tail:
                fd.warnings.append(f"в архиве нет даты {p.date:%d.%m.%Y}, которая есть в таблице на странице фонда")
            elif have is not None and any(a is not None and b is not None and abs(a - b) > 0.005
                                          for a, b in ((have.nav, p.nav), (have.price, p.price))):
                fd.warnings.append(f"{p.date:%d.%m.%Y}: в архиве СЧА {have.nav} / пай {have.price}, "
                                   f"на странице фонда {p.nav} / {p.price}")
        if tail:
            fd.segments.append(self.segment(page, tail, page_url=page_url, since=since))
            fd.warnings.append(f"архив CSV отстаёт от страницы фонда: "
                               f"{', '.join(f'{p.date:%d.%m.%Y}' for p in tail)} — из таблицы последних значений")
