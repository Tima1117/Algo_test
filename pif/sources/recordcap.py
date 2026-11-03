"""ООО «УК «РЕКОРД КАПИТАЛ» (recordcap.ru): СЧА и пай — таблица раздела «Раскрытие информации».

Источники (сайт УК; www.recordcap.ru отвечает редиректом на recordcap.ru, поэтому ходим сразу туда):
* список фондов: https://recordcap.ru/documents/funds — карточки a.fund-card (название, метка
  «Неквал./Квал. инвесторам» или «Фонд прекращён») со ссылками /documents/funds/<slug>;
* страница раскрытия фонда: https://recordcap.ru/documents/funds/<slug> —
  «Общая информация о фонде» (строка «Правила доверительного управления…»: «№ 3343 от 06.07.2017»)
  и таблица «Стоимость инвестиционного пая и стоимость чистых активов» (колонки «Дата»,
  «Стоимость пая», «СЧА», у фондов с индикатором ещё «Бенчмарк» и отклонения). Строки от новых
  к старым, сервер отдаёт первые 50, остальное догружает кнопка «Показать еще»;
* догрузка таблицы: https://recordcap.ru/documents/funds/<slug>/values?offset=N&limit=200 —
  HTML-фрагмент из тех же <tr>, без заголовка (limit сервер режет до 200). Даты в запросе нет,
  поэтому листаем страницы, пока самая старая строка не окажется раньше ctx.start_for(rules).
Частота — как публикует УК: открытые фонды — каждый рабочий день, интервальный «О3 – Три Кита» —
два раза в месяц (≈25-е число и последний рабочий день). СЧА — в рублях, пай — в рублях
(числа «22 752,62», без множителей; валюта на сайте не подписана, все фонды УК рублёвые).
Те же ряды сайт рисует на странице /funds/<slug> (массивы PAI/SCHA в скрипте графика) — значения
совпадают, берём официальную таблицу раскрытия.

Сопоставление: кандидаты из списка — сначала совпавшие по названию (norm_text), затем остальные
действующие фонды того же типа (ОПИФ/ИПИФ/БПИФ); номер правил на странице раскрытия обязан совпасть
с rules. Перебор нужен из-за «О3 – Три Кита»: на сайте «О3» с цифрой 3, в реестре ЦБ «ОЗ» с буквой З.

Официальная доходность: в подвале каждой страницы сайта — блок по каждому фонду
«Правила доверительного управления … за № 3343. Доходность за 1 месяц +14.32%; Доходность за
3 месяца …; … за 6 месяцев …; … за 1 год …; … за 3 года …. Данные на 30.10.2026.» У молодых
фондов только периоды, которые уже прошли (у «О3» — 1 и 3 месяца). Берём из подвала страницы
раскрытия фонда (отдельный запрос не нужен).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://recordcap.ru"
LIST_URL = f"{BASE}/documents/funds"
PAGE_LIMIT = 200          # больше сервер за раз не отдаёт
MAX_PAGES = 30            # предохранитель от бесконечного листания

TYPE_PREFIX = {"open": "ОПИФ", "interval": "ИПИФ", "exchange": "БПИФ"}
# «№ 3343», «№7663.», «№ 6285-СД», «№0603-97103681» (точка в конце предложения не входит)
RULES_RE = re.compile(r"№\s*(\d+(?:-[0-9А-ЯЁA-Z]+)*)")
RETURN_PERIODS = {"1 месяц": "1m", "3 месяца": "3m", "6 месяцев": "6m", "1 год": "1y"}


@dataclass
class ListedFund:
    name: str
    url: str          # абсолютный адрес страницы раскрытия
    archived: bool    # «Фонд прекращён»


def _soup(content: bytes | str) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return BeautifulSoup(content, "lxml")


def _text(tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True).replace("\xa0", " ")).strip()


# ---------- чистые разборщики (на них тесты с настоящими фикстурами) ----------

def parse_fund_list(content: bytes | str) -> list[ListedFund]:
    """Страница «Документы фондов» -> карточки фондов в порядке сайта."""
    cards = _soup(content).select("a.fund-card[href]")
    require(bool(cards), "список фондов: нет карточек a.fund-card")
    out = []
    for a in cards:
        name = a.select_one(".fund-name")
        require(name is not None, f"список фондов: у карточки {a['href']!r} нет .fund-name")
        require("/documents/funds/" in a["href"], f"список фондов: неожиданная ссылка {a['href']!r}")
        out.append(ListedFund(name=_text(name), url=urljoin(BASE, a["href"]),
                              archived="is-archived" in (a.get("class") or [])))
    return out


def parse_rules(content: bytes | str) -> str:
    """Страница раскрытия фонда -> номер правил ДУ из «Общей информации о фонде»."""
    for key in _soup(content).select("td.gi-key"):
        if _text(key).startswith("Правила доверительного управления"):
            val = key.find_next_sibling("td")
            require(val is not None, "общая информация: у строки «Правила…» нет значения")
            m = RULES_RE.search(_text(val))
            require(m is not None, f"общая информация: не нашёл номер правил в {_text(val)!r}")
            return m.group(1)
    raise UnknownFormat("общая информация: нет строки «Правила доверительного управления…» (td.gi-key)")


def parse_values_columns(content: bytes | str) -> dict[str, int]:
    """Заголовок таблицы «Стоимость пая и СЧА» -> номера колонок даты, пая и СЧА."""
    head = _soup(content).select_one("table.table-values thead tr.table-header-main")
    require(head is not None, "таблица стоимости: нет заголовка table.table-values tr.table-header-main")
    names = [_text(th) for th in head.find_all("th")]
    want = {"date": "Дата", "price": "Стоимость пая", "nav": "СЧА"}
    cols = {}
    for k, title in want.items():
        idx = [i for i, n in enumerate(names) if n == title]
        require(len(idx) == 1, f"таблица стоимости: колонка «{title}» не найдена; есть {names}")
        cols[k] = idx[0]
    return cols


def parse_values_rows(content: bytes | str, cols: dict[str, int]) -> list[SeriesPoint]:
    """Строки tr.table-row (со страницы фонда или из догрузки /values) -> точки, от старых к новым."""
    soup = _soup(content)
    rows = soup.select("tr.table-row")
    if not rows:
        # пустой ответ догрузки — конец таблицы; любой другой текст — незнакомый формат
        require(not _text(soup), "таблица стоимости: в ответе нет строк tr.table-row")
        return []
    need = max(cols.values()) + 1
    points = []
    for tr in rows:
        cells = tr.find_all("td")
        require(len(cells) >= need, f"таблица стоимости: в строке {len(cells)} ячеек, нужно ≥ {need}")
        require("date-cell" in (cells[cols["date"]].get("class") or []),
                "таблица стоимости: в колонке даты нет ячейки td.date-cell")
        points.append(SeriesPoint(date=day(_text(cells[cols["date"]])),
                                  nav=num(_text(cells[cols["nav"]])),
                                  price=num(_text(cells[cols["price"]]))))
    return sorted(points, key=lambda p: p.date)


def parse_values_page(content: bytes | str) -> tuple[dict[str, int], list[SeriesPoint], bool]:
    """Страница раскрытия -> (колонки, первая порция строк, есть ли кнопка «Показать еще»)."""
    soup = _soup(content)
    body = soup.select_one("table.table-values tbody#valuesBody")
    require(body is not None, "таблица стоимости: нет table.table-values tbody#valuesBody")
    cols = parse_values_columns(content)
    points = parse_values_rows(str(body), cols) if body.select("tr") else []
    more = soup.select_one("#loadMoreValues") is not None
    return cols, points, more


def parse_returns(content: bytes | str, source_url: str) -> dict[str, list[OfficialReturn]]:
    """Подвал сайта -> {номер правил: доходность за 1/3/6/12 мес}.

    Нет ни одного блока с «Доходность за» -> {} (сайт просто не показывает); блок есть,
    но без номера правил или даты «Данные на» -> UnknownFormat.
    """
    out: dict[str, list[OfficialReturn]] = {}
    for div in _soup(content).find_all("div"):
        if div.find("div") is not None:
            continue                      # нужны только «листовые» блоки подвала
        text = _text(div)
        if "Доходность за" not in text:
            continue
        m_rules = RULES_RE.search(text)
        m_asof = re.search(r"Данные на\s*(\d{2}\.\d{2}\.\d{4})", text)
        require(m_rules is not None and m_asof is not None,
                f"доходность в подвале: нет номера правил или даты «Данные на» в {text[:120]!r}")
        as_of = day(m_asof.group(1))
        values = re.findall(r"Доходность за ([^;.%]+?)\s*([+\-−]?\d+(?:[.,]\d+)?)\s*%", text)
        require(len(values) == text.count("Доходность за"),
                f"доходность в подвале: не все значения разобраны в {text[:200]!r}")
        rets = [OfficialReturn(period=RETURN_PERIODS[label], value_pct=pct(value.replace("−", "-")),
                               as_of=as_of, source_url=source_url)
                for label, value in values if label in RETURN_PERIODS]
        out[m_rules.group(1)] = rets
    return out


# ---------- адаптер ----------

class RecordcapAdapter(Adapter):
    key = "recordcap"
    title = "УК «Рекорд Капитал»"
    uk_inns = ("7718919844",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        # Список и страницы раскрытия качаются внутри guarded: повторный get того же адреса
        # берётся из кэша, а сбой списка становится статусом фонда, а не роняет адаптер.
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f)) for f in funds]

    def _find(self, ctx: Context, fund: Fund, listed: list[ListedFund]):
        """Страница раскрытия с тем же номером правил -> (адрес, RawDoc, что проверили)."""
        want = norm_text(fund.name)
        prefix = TYPE_PREFIX.get(fund.type, "")
        by_name = [f for f in listed if norm_text(f.name) == want]
        same_type = [f for f in listed if not f.archived and f not in by_name
                     and re.search(rf"\b{prefix}\b", f.name)]
        checked = []
        for cand in by_name + same_type:
            doc = ctx.http.get(cand.url, fmt="html")
            rules = parse_rules(doc.content())
            checked.append(f"«{cand.name}» — № {rules}")
            if rules == fund.rules:
                if norm_text(cand.name) != want:
                    ctx.log.info("[recordcap] %s: на сайте «%s», совпал номер правил", fund.rules, cand.name)
                # повторный запрос берётся из кэша и лишь помечает документ этим фондом в манифесте
                return cand.url, ctx.http.get(cand.url, fmt="html", funds=[fund.rules]), checked
        return None, None, checked

    def _fund(self, ctx: Context, fund: Fund) -> FundData:
        lst = ctx.http.get(LIST_URL, fmt="html", funds=[fund.rules])
        page_url, doc, checked = self._find(ctx, fund, parse_fund_list(lst.content()))
        if doc is None:
            return FundData(fund.rules, status="not_found",
                            reason=f"на {LIST_URL} нет фонда с правилами № {fund.rules} "
                                   f"(проверены: {'; '.join(checked) or 'кандидатов нет'})")

        html = doc.content()
        cols, first, more = parse_values_page(html)
        segments = [self.segment(doc, first, page_url=page_url, since=ctx.since)]
        # Догрузка: листаем, пока не дойдём до даты, с которой нужен ряд, или до конца таблицы.
        start = ctx.start_for(fund.rules)
        offset, oldest, pages = len(first), (first[0].date if first else None), 0
        while more and oldest is not None and oldest > start:
            pages += 1
            require(pages <= MAX_PAGES, f"таблица стоимости: больше {MAX_PAGES} страниц догрузки")
            raw = ctx.http.get(f"{page_url}/values", params={"offset": offset, "limit": PAGE_LIMIT},
                               fmt="html", funds=[fund.rules])
            pts = parse_values_rows(raw.content(), cols)
            segments.append(self.segment(raw, pts, page_url=page_url, since=ctx.since))
            offset += len(pts)
            more = len(pts) == PAGE_LIMIT
            oldest = pts[0].date if pts else None

        fd = FundData(fund.rules, currency="RUB", page_url=page_url,
                      segments=[s for s in segments if s.points])
        fd.official_returns = parse_returns(html, page_url).get(fund.rules, [])
        if not fd.official_returns:
            fd.warnings.append(f"в блоке доходности на сайте УК нет данных для фонда № {fund.rules}")

        if not fd.segments:
            if first or more:
                fd.status, fd.reason = "partial", f"в таблице УК нет строк начиная с {ctx.since:%d.%m.%Y}"
            elif fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", "фонд формируется, таблица стоимости пая и СЧА пуста"
            else:
                fd.status, fd.reason = "no_history", "на странице раскрытия таблица стоимости пая и СЧА пуста"
        return fd
