"""ООО «УК «А-Капитал» (ИНН 7707583543), сайт https://www.acapital-am.ru (1С-Битрикс).

Источники
* GET https://www.acapital-am.ru/api/opif/<id>/rub/graph?from=YYYY-MM-DD&to=YYYY-MM-DD — JSON-API,
  которым страница фонда рисует график «Динамика стоимости пая и СЧА» (кнопка «Перестроить график»).
  Ответ: {"data": {"currency", "fundId", "from", "to", "dates": {"min", "max"}, "last": [...],
  "data": [{"id", "date", "date_original", "asset_cost", "fund_cost", "fund_code"}]}}.
  * date_original — дата расчёта (ISO). Поле date — epoch-мс полуночи по Москве (UTC+3): в UTC это
    21:00 предыдущего дня, поэтому его не используем.
  * asset_cost — СЧА в рублях, fund_cost — расчётная стоимость пая в рублях (строки с копейками).
  * Частота — каждый рабочий день; история с даты формирования (dates.min: 2004 г. у старых фондов).
  * Границы from/to включаются. Ряд запрашиваем окнами по календарным месяцам (с 1-го по последнее
    число): адреса прошлых месяцев не меняются, кэш и слияние сегментов (по raw_url) работают, а при
    повторном сборе с ctx.start_for() докачиваются только последние месяцы. Будущая граница to
    (конец текущего месяца) API принимает.
* GET https://www.acapital-am.ru/api/opif/<id>/rub/excel/<from>/<to> — кнопка «Экспорт в Excel»:
  xlsx, первая строка «<название фонда>. Валюта: rub». Берём один месяц (месяц ctx.since — адрес
  стабилен), чтобы сверить, что под этим id на сайте именно наш фонд (по названию с сайта).
* https://www.acapital-am.ru/opif/<slug>/ — страница фонда для человека. Там же официальная доходность:
  блок «Прирост расчетной стоимости инвестиционного пая на <дата>» — 1 мес., 3 мес., 6 мес., 1 год,
  3 года, 5 лет (УК считает от последнего рабочего дня месяца; «-» — не опубликовано).

Почему окна по месяцам и «Connection: close». С хоста сборщика (зарубежный хостинг) любое
TCP-соединение с сайтом УК замирает примерно после 16 КБ ответа: страницы (~100 КБ) не догружаются
вовсе (ReadTimeout), а при keep-alive замирают и следующие мелкие ответы. Поэтому каждый запрос к API —
отдельным соединением (заголовок Connection: close), и каждый ответ мал (месяц JSON ~3,5 КБ,
xlsx ~7 КБ). Страница фонда с доходностью с такого хоста не загружается: адаптер пробует первую
страницу, а после сетевого сбоя остальные не запрашивает (иначе по ~1,5 мин тайм-аутов на фонд) —
доходность остаётся пустой с предупреждением. Из российской сети страница грузится, и доходность
разбирается parse_returns().

Фонды и id. id в API — номер элемента инфоблока; он же стоит в карточках фондов на /opif/
(<div class="opif__entry" id="bx_3218110189_<id>"> со ссылкой /opif/<slug>/ и названием). Список
/opif/ с этого хоста целиком не загружается (≈25 КБ), поэтому id, адреса и названия выписаны в
SITE_FUNDS по этой странице (проверено 03.11.2026), а на каждом сборе название под id сверяется
с выгрузкой Excel. Номеров правил ДУ на сайте в API нет — сопоставление с реестром по названию
(norm_text, точное совпадение).

Валюта. Для «Валютных облигаций» и «Золота» сайт предлагает пересчёт в доллары (currency=usd), но
расчётная стоимость пая определяется в рублях: по умолчанию график и карточка на /opif/ — в рублях
(«164,22 ₽»), долларовые значения — пересчёт с округлением до центов. Берём rub для всех фондов.

Выплаты дохода. «А-Капитал Облигации Рантье» выплачивает доход ежеквартально; размер на пай
УК публикует только PDF-сообщениями в «Раскрытии информации» (/info/, «Сообщение о выплате дохода
по инвестиционным паям …»: на пай, дата, на которую определяется доход, дата выплаты). PDF не
разбираем (нет библиотеки), payouts не заполняем — предупреждение в данных фонда.
"""
from __future__ import annotations

import calendar
import io
import json
import re
from dataclasses import dataclass
from datetime import date

import openpyxl
from bs4 import BeautifulSoup

from ..http import Blocked, FetchError
from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

SITE = "https://www.acapital-am.ru"
GRAPH_URL = SITE + "/api/opif/{id}/rub/graph"                  # ?from=YYYY-MM-DD&to=YYYY-MM-DD
EXPORT_URL = SITE + "/api/opif/{id}/rub/excel/{start}/{end}"   # xlsx, «Экспорт в Excel»
FUND_PAGE = SITE + "/opif/{slug}/"
FUNDS_PAGE = SITE + "/opif/"
CURRENCY = "rub"
# Отдельное соединение на каждый запрос: длинное соединение с сайтом замирает после ~16 КБ.
NO_KEEPALIVE = {"Connection": "close"}
# Пропуск больше этого между соседними датами — значит, API не отдал часть ряда
# (новогодние каникулы: 30.12.2025 -> 12.01.2026 — 13 дней).
MAX_GAP_DAYS = 16


@dataclass(frozen=True)
class SiteFund:
    id: int          # id фонда в API = id элемента в карточке на /opif/
    slug: str        # адрес страницы /opif/<slug>/
    name: str        # название на сайте


# Карточки на https://www.acapital-am.ru/opif/ (03.11.2026).
SITE_FUNDS = (
    SiteFund(858, "a-kapital-obligacii", "А-Капитал Облигации"),
    SiteFund(5570, "a-kapital-rante", "А-Капитал Облигации Рантье"),
    SiteFund(859, "a-kapital-val-obligacii", "А-Капитал Валютные облигации"),
    SiteFund(857, "a-kapital-sbalansir", "А-Капитал Сбалансированный"),
    SiteFund(5806, "a-kapital-aktsii", "А-Капитал Акции"),
    SiteFund(5807, "a-kapital-zoloto", "А-Капитал Золото"),
)

# Фонды, которые выплачивают доход пайщикам (по описанию на странице фонда).
PAYOUT_FUNDS = {5570}

# Подпись столбца в блоке доходности -> период OfficialReturn (3 года и 5 лет не нужны).
RETURN_LABELS = {"1 мес.": "1m", "3 мес.": "3m", "6 мес.": "6m", "1 год": "1y"}

_GRAPH_KEYS = ("currency", "fundId", "from", "to", "data")
_ROW_KEYS = ("id", "date_original", "asset_cost", "fund_cost")
_TITLE_RE = re.compile(r"^(?P<name>.+?)\.\s*Валюта:\s*(?P<cur>[a-z]{3})$")
_RETURNS_TITLE_RE = re.compile(r"Прирост расчетной стоимости инвестиционного пая на (\d{2}\.\d{2}\.\d{4})")


# ---------------- разбор ----------------

def _positive(value, what: str) -> float | None:
    v = num(value)
    require(v is None or v > 0, f"{what}: неположительное значение {value!r}")
    return v


def parse_graph(content: bytes | str | dict, fund_id: int | None = None) -> list[SeriesPoint]:
    """Ответ /api/opif/<id>/rub/graph -> точки (дата, СЧА руб., цена пая руб.).

    Пустой список — нормальный ответ для окна без расчётов (месяц до формирования фонда или
    первые выходные текущего месяца); нужен ли ряд вообще, решает вызывающий.
    """
    data = json.loads(content) if isinstance(content, (bytes, str)) else content
    require_keys(data, ("data",), "ответ graph")
    body = data["data"]
    require_keys(body, _GRAPH_KEYS, "ответ graph.data")
    require(body["currency"] == CURRENCY, f"ответ graph: валюта {body['currency']!r}, ожидалась {CURRENCY!r}")
    if fund_id is not None:
        require(str(body["fundId"]) == str(fund_id), f"ответ graph: fundId {body['fundId']!r} вместо {fund_id}")
    start, end = day(body["from"]), day(body["to"])
    rows = body["data"]
    require(isinstance(rows, list), "ответ graph: data.data — не список")
    points: dict[date, SeriesPoint] = {}
    for row in rows:
        require_keys(row, _ROW_KEYS, "точка graph")
        require(str(row["id"]) == str(body["fundId"]), f"точка graph: чужой фонд id={row['id']!r}")
        d = day(row["date_original"])
        require(start <= d <= end, f"точка graph {d} вне запрошенного окна {start}…{end}")
        require(d not in points, f"ответ graph: дата {d} встречается дважды")
        points[d] = SeriesPoint(d, nav=_positive(row["asset_cost"], f"СЧА на {d}"),
                                price=_positive(row["fund_cost"], f"цена пая на {d}"))
    return [points[d] for d in sorted(points)]


def parse_export_title(content: bytes) -> tuple[str, str]:
    """Выгрузка «Экспорт в Excel» -> (название фонда, валюта). Проверяет и шапку таблицы."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception as e:  # noqa: BLE001 — битый/не тот файл
        raise UnknownFormat(f"выгрузка Excel не читается: {type(e).__name__}: {e}") from e
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(min_row=1, max_row=2, max_col=3, values_only=True))
    require(len(rows) == 2, "выгрузка Excel: меньше двух строк")
    m = _TITLE_RE.match(str(rows[0][0] or "").strip())
    require(m is not None, f"выгрузка Excel: заголовок {rows[0][0]!r} не «<фонд>. Валюта: <код>»")
    header = tuple(str(c or "").strip() for c in rows[1])
    require(header == ("Дата", "СЧА", "ПАЙ"), f"выгрузка Excel: шапка {header!r} вместо ('Дата', 'СЧА', 'ПАЙ')")
    return m["name"].strip(), m["cur"]


def parse_returns(html: bytes | str, source_url: str) -> list[OfficialReturn]:
    """Блок «Прирост расчетной стоимости инвестиционного пая на <дата>» -> доходность 1/3/6/12 мес."""
    soup = BeautifulSoup(html, "lxml")
    block = soup.select_one("div.strategy__points")
    require(block is not None, "на странице фонда нет блока «Прирост расчетной стоимости пая»")
    title = block.select_one(".main-title")
    m = _RETURNS_TITLE_RE.search(" ".join(title.get_text(" ").split()) if title else "")
    require(m is not None, "блок доходности: нет заголовка «Прирост … пая на <дата>»")
    as_of = day(m.group(1))
    rows = [[" ".join(td.get_text(" ").split()) for td in tr.find_all("td")] for tr in block.find_all("tr")]
    require(len(rows) == 2, f"блок доходности: ожидалось 2 строки таблицы, есть {len(rows)}")
    labels, values = rows
    require(len(labels) == len(values), "блок доходности: число подписей и значений не совпадает")
    missing = [lb for lb in RETURN_LABELS if lb not in labels]
    require(not missing, f"блок доходности: нет столбцов {missing}; есть {labels}")
    out = []
    for label, value in zip(labels, values):
        period = RETURN_LABELS.get(label)
        if period is None:
            continue
        v = pct(value)
        if v is not None:                    # «-» — не опубликовано
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def match_funds(funds: list[Fund], site_funds=SITE_FUNDS) -> dict[str, SiteFund]:
    """Фонд реестра -> фонд сайта по точному совпадению названия (norm_text)."""
    by_name: dict[str, SiteFund] = {}
    for sf in site_funds:
        key = norm_text(sf.name)
        require(key not in by_name, f"два фонда на сайте с одинаковым названием: {sf.name!r}")
        by_name[key] = sf
    out = {}
    for f in funds:
        for name in (f.name, f.full_name):
            sf = by_name.get(norm_text(name))
            if sf is not None:
                out[f.rules] = sf
                break
    return out


def month_windows(start: date, today: date) -> list[tuple[date, date]]:
    """Календарные месяцы от месяца start до месяца today включительно: [(1-е число, последнее)]."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (today.year, today.month):
        out.append((date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _today() -> date:
    """Сегодня (отдельной функцией, чтобы тесты могли зафиксировать дату)."""
    return date.today()


def find_gaps(dates: list[date], max_days: int = MAX_GAP_DAYS) -> list[tuple[date, date]]:
    return [(a, b) for a, b in zip(dates, dates[1:]) if (b - a).days > max_days]


# ---------------- адаптер ----------------

class ACapitalAdapter(Adapter):
    key = "acapital"
    title = "А-Капитал"
    uk_inns = ("7707583543",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        matched = match_funds(funds)
        self._pages_down: str | None = None     # причина, по которой страницы фондов не грузятся
        out = []
        for f in funds:
            sf = matched.get(f.rules)
            if f.status.startswith("Формир"):
                out.append(FundData(f.rules, status="not_formed", reason="фонд формируется",
                                    page_url=FUND_PAGE.format(slug=sf.slug) if sf else FUNDS_PAGE))
            elif sf is None:
                out.append(FundData(f.rules, page_url=FUNDS_PAGE, status="not_found",
                                    reason="фонда с таким названием нет среди ОПИФ на сайте УК"))
            else:
                out.append(self.guarded(ctx, f, lambda f=f, sf=sf: self._fund(ctx, f, sf)))
        return out

    def _fund(self, ctx: Context, fund: Fund, sf: SiteFund) -> FundData:
        rules = [fund.rules]
        page_url = FUND_PAGE.format(slug=sf.slug)
        today = _today()

        # 1. Под этим id на сайте — наш фонд: название из выгрузки Excel за месяц начала истории.
        first, last = month_windows(ctx.since, ctx.since)[0]
        raw = ctx.http.get(EXPORT_URL.format(id=sf.id, start=first, end=last), fmt="xlsx",
                           funds=rules, headers=NO_KEEPALIVE)
        name, cur = parse_export_title(raw.content())
        require(norm_text(name) == norm_text(sf.name),
                f"под id {sf.id} на сайте фонд {name!r}, а не {sf.name!r}")
        require(cur == CURRENCY, f"выгрузка Excel в валюте {cur!r}")

        # 2. Ряд — окнами по календарным месяцам с начала (или хвоста) истории.
        segments, dates = [], []
        for start, end in month_windows(ctx.start_for(fund.rules), today):
            raw = ctx.http.get(GRAPH_URL.format(id=sf.id), params={"from": start.isoformat(), "to": end.isoformat()},
                               fmt="json", funds=rules, headers=NO_KEEPALIVE)
            seg = self.segment(raw, parse_graph(raw.content(), fund_id=sf.id), page_url=page_url, since=ctx.since)
            if seg.points:
                segments.append(seg)
                dates += [p.date for p in seg.points]
        require(bool(segments), f"API графика не вернуло ни одной точки с {ctx.start_for(fund.rules)}")

        fd = FundData(fund.rules, currency="RUB", segments=segments, page_url=page_url)
        gaps = find_gaps(sorted(dates))
        if gaps:
            fd.status = "partial"
            fd.reason = "в ряду пропуски: " + ", ".join(f"{a}…{b}" for a, b in gaps)

        # 3. Официальная доходность — со страницы фонда (если страница грузится с этого хоста).
        fd.official_returns = self._returns(ctx, fund, page_url, fd)
        if sf.id in PAYOUT_FUNDS:
            fd.warnings.append("фонд ежеквартально выплачивает доход; размер на пай УК публикует только "
                               "PDF-сообщениями в «Раскрытии информации» — выплаты не собраны")
        return fd

    def _returns(self, ctx: Context, fund: Fund, page_url: str, fd: FundData) -> list[OfficialReturn]:
        if self._pages_down:
            fd.warnings.append(f"официальная доходность не получена: страницы сайта не загружаются "
                               f"({self._pages_down})")
            return []
        try:
            raw = ctx.http.get(page_url, fmt="html", funds=[fund.rules])
        except FetchError as e:
            if isinstance(e, Blocked) or e.status is None:   # сеть/антибот — не мучаем остальные страницы
                self._pages_down = e.reason
            fd.warnings.append(f"официальная доходность не получена: страница фонда не загрузилась ({e.reason})")
            return []
        try:
            rets = parse_returns(raw.content(), page_url)
        except UnknownFormat as e:
            ctx.log.warning("[%s] %s: доходность не разобрана: %s", fund.uk_short, fund.rules, e)
            fd.warnings.append(f"официальная доходность не разобрана: {e}")
            return []
        missing = sorted(set(RETURN_LABELS.values()) - {r.period for r in rets})
        if missing:
            fd.warnings.append(f"УК не публикует доходность за {', '.join(missing)}")
        return rets
