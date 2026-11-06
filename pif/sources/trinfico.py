"""АО «УК ТРИНФИКО» (www.trinfico-am.ru) — 5 открытых фондов.

Источники (обычный HTML без параметров в адресе: robots.txt сайта запрещает «/*?*», /upload/, /local/,
/disclosure/*):
* список фондов — https://www.trinfico-am.ru/opif/ (меню «Паевые инвестиционные фонды»): ссылки
  /opif/<slug>/ с подписью «ОПИФ РФИ «Лидар»», «ЗПИФ РФИ …»;
* страница фонда https://www.trinfico-am.ru/opif/<slug>/ — всё нужное есть в самой странице:
  - вкладка «Таблица стоимости пая и СЧА» (#cost-dynamics-table-pai-and-scha) — ВСЯ история,
    которую показывает сайт, по рабочим дням: дата, «Расчетная стоимость инвестиционного пая
    (РСИП), рубли», «Стоимость чистых активов (СЧА), рубли» (без множителей, валюта — в заголовке);
    диапазон дат страница не принимает, поэтому ctx.start_for не нужен — берём таблицу и режем
    по ctx.since;
  - таблица «Динамика стоимости пая на <дата>» (table.cost-table--dynamic) — официальная доходность
    за 1/3/6 мес., 1 год (и 3/5 лет — не берём) на последний календарный день прошлого месяца.
    Считается от конца месяца до конца месяца по цене пая без учёта выплат: на 31.10.2026 все
    30 цифр пяти фондов воспроизводятся по таблице цен до 0,01 п. п.;
  - вкладка «Сведения о фонде» (#tab-info): «Правила доверительного управления зарегистрированы
    Банком России … за № 3863» — по этому номеру и сопоставляем фонд с реестром.

Ловушки:
* Графики страницы берут данные из /include/data.php?fund=…ВыпЦБ&… — адрес с параметрами закрыт
  robots.txt, туда не ходим (таблица на странице содержит те же даты, что и календарь графика).
* «За период 06.09.2019-06.09.2022 34,54 %» над графиком — заглушка шаблона (одинакова на всех
  страницах, браузер заменяет её расчётом) — это НЕ официальная доходность.
* КИД и справки о СЧА лежат в /upload/iblock/ (закрыто robots.txt) — не нужны, всё есть в HTML.
* «Лидар» выплачивает квартальный доход, но сумм выплат на странице фонда нет (сообщения о выплатах —
  в /disclosure/, закрыто robots.txt). Официальная доходность УК — по цене пая без выплат, ряд
  пая — тоже; об этом пишем предупреждение.
* Раньше сайт отвечал 403 на User-Agent со ссылкой; с нынешним UA (без ссылки) отвечает 200.
* В <head> каждой страницы подключён виджет Яндекс SmartCaptcha для форм заявок
  (<script src="https://captcha-api.yandex.ru/captcha.js…">). Это не проверка: страница отдаётся
  целиком (1,7–4 МБ, с данными). Но эвристика pif/http.py считает «smartcaptcha» в первых 20 КБ
  ответа 200 антиботом -> Blocked -> статус 'blocked', пока её не поправят в общем модуле.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://www.trinfico-am.ru"
LIST_URL = f"{BASE}/opif/"

# Подпись колонки доходности на сайте -> наш код периода. «3 года», «5 лет» не берём.
PERIODS = {"1 мес.": "1m", "3 мес.": "3m", "6 мес.": "6m", "1 год": "1y"}
# Валюта в заголовках колонок таблицы истории: «(РСИП), рубли», «(СЧА), рубли».
CURRENCIES = {"рубли": "RUB", "рублях": "RUB", "доллары сша": "USD", "долларах сша": "USD",
              "евро": "EUR", "юани": "CNY", "юанях": "CNY"}
STALE_DAYS = 10   # последняя точка старше — предупреждение «УК давно не обновляла»

_PATH_RE = re.compile(r"^/opif/[a-z0-9-]+/$")
_RULES_RE = re.compile(r"за\s*№\s*(\d[\d\-]*\d|\d)")
_AS_OF_RE = re.compile(r"на\s+(\d{2}\.\d{2}\.\d{4})")
_INCOME_RE = re.compile(r"выплат\w*\s+(?:\w+\s+){0,2}дохода", re.I)


# ---------- разбор ----------
def _soup(content) -> BeautifulSoup:
    """bytes/str/готовый BeautifulSoup -> BeautifulSoup (страница фонда — до 4 МБ, разбираем один раз)."""
    if isinstance(content, BeautifulSoup):
        return content
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    require(isinstance(content, str) and "<" in content, "ожидалась HTML-страница")
    return BeautifulSoup(content, "lxml")


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def name_key(s: str) -> str:
    """'ОПИФ РФИ «ТРИНФИКО Капиталист»' и 'ТРИНФИКО Капиталист' -> 'тринфико капиталист'."""
    return re.sub(r"\s+", " ", re.sub(r"\bрфи\b", " ", norm_text(s))).strip()


def parse_fund_list(content) -> dict[str, str]:
    """Список фондов -> {путь страницы: подпись}, например {'/opif/opif-rfi-lidar/': 'ОПИФ РФИ «Лидар»'}.

    Ссылки на страницы встречаются по нескольку раз (меню, карточки «ПОДРОБНЕЕ») — берём подпись с
    названием фонда (содержит «ПИФ»).
    """
    soup = _soup(content)
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        title = _text(a)
        if _PATH_RE.match(href) and "ПИФ" in title:
            out.setdefault(href, title)
    require(bool(out), "список фондов: нет ссылок /opif/<страница>/ с названием фонда")
    return out


def parse_fund_info(content) -> tuple[str, str]:
    """Вкладка «Сведения о фонде» -> (номер правил, полное название фонда)."""
    tab = _soup(content).select_one("#tab-info")
    require(tab is not None, "страница фонда: нет вкладки «Сведения о фонде» (#tab-info)")
    fields: dict[str, str] = {}
    for tr in tab.select("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) == 2:
            fields[_text(tds[0])] = _text(tds[1])
    require("Правила Фонда" in fields and "Полное наименование Фонда" in fields,
            f"«Сведения о фонде»: нет строк «Правила Фонда» / «Полное наименование Фонда»; есть {sorted(fields)}")
    m = _RULES_RE.search(fields["Правила Фонда"])
    require(m is not None, f"«Правила Фонда»: нет номера правил в {fields['Правила Фонда']!r}")
    return m.group(1), fields["Полное наименование Фонда"]


def _currency(header: str) -> str:
    """'Расчетная стоимость инвестиционного пая (РСИП), рубли' -> 'RUB'."""
    tail = header.rsplit(",", 1)[-1].strip().lower() if "," in header else ""
    require(tail in CURRENCIES, f"таблица истории: незнакомая валюта в заголовке {header!r}")
    return CURRENCIES[tail]


def _history_table(soup: BeautifulSoup):
    table = soup.select_one("#cost-dynamics-table-pai-and-scha table")
    require(table is not None, "страница фонда: нет «Таблицы стоимости пая и СЧА» (#cost-dynamics-table-pai-and-scha)")
    heads = [_text(th) for th in table.select("thead th")]

    def col(prefix: str) -> int:
        hits = [i for i, h in enumerate(heads) if h.startswith(prefix)]
        require(len(hits) == 1, f"таблица истории: колонка «{prefix}…» не найдена однозначно; заголовки {heads}")
        return hits[0]

    i_date = col("Дата")
    i_price = col("Расчетная стоимость инвестиционного пая")
    i_nav = col("Стоимость чистых активов")
    currency = _currency(heads[i_price])
    require(_currency(heads[i_nav]) == currency, f"таблица истории: СЧА и пай в разных валютах: {heads}")
    return table, heads, (i_date, i_price, i_nav), currency


def history_currency(content) -> str:
    """Валюта пая и СЧА из заголовков таблицы истории."""
    return _history_table(_soup(content))[3]


def _positive(value: str, where: str) -> float | None:
    v = num(value)
    if v is None:
        return None
    require(v > 0, f"{where}: неположительное значение {value!r}")
    return v


def parse_history(content) -> list[SeriesPoint]:
    """«Таблица стоимости пая и СЧА» -> ряд (дата, СЧА, пай) по возрастанию дат, в валюте пая.

    Пустая таблица (фонд формируется) -> []; изменения «за день, в %» не берём.
    """
    table, heads, (i_date, i_price, i_nav), _ = _history_table(_soup(content))
    out: dict[date, SeriesPoint] = {}
    for n, tr in enumerate(table.select("tbody tr")):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue                    # строки-разделители tr.cost-dynamics__table--tr-space
        require(len(tds) == len(heads), f"таблица истории, строка {n}: {len(tds)} ячеек вместо {len(heads)}")
        d = day(_text(tds[i_date]))
        p = SeriesPoint(d, _positive(_text(tds[i_nav]), f"СЧА на {d}"),
                        _positive(_text(tds[i_price]), f"пай на {d}"))
        if d in out and out[d] != p:
            raise UnknownFormat(f"таблица истории: два разных значения на {d}")
        if p.nav is not None or p.price is not None:
            out[d] = p
    return [out[d] for d in sorted(out)]


def parse_returns(content, source_url: str) -> list[OfficialReturn] | None:
    """«Динамика стоимости пая на ДД.ММ.ГГГГ» -> доходность за 1/3/6/12 мес.

    Таблицы нет на странице -> None (УК не опубликовала); таблица есть, но незнакомая -> UnknownFormat.
    Пустая ячейка (фонд моложе периода) -> период пропускается, а не 0.
    """
    table = _soup(content).select_one("table.cost-table--dynamic")
    if table is None:
        return None
    heads = [_text(th) for th in table.select("thead th")]
    require(len(heads) >= 2, f"таблица доходности: мало колонок {heads}")
    m = _AS_OF_RE.search(heads[0])
    require(m is not None, f"таблица доходности: нет даты в заголовке {heads[0]!r}")
    as_of = datetime.strptime(m.group(1), "%d.%m.%Y").date()
    missing = [p for p in PERIODS if p not in heads]
    require(not missing, f"таблица доходности: нет колонок {missing}; есть {heads}")
    rows = [tr for tr in table.select("tbody tr") if tr.find_all("td", recursive=False)]
    require(len(rows) == 1, f"таблица доходности: ожидалась одна строка фонда, получено {len(rows)}")
    tds = rows[0].find_all("td", recursive=False)
    require(len(tds) == len(heads), f"таблица доходности: {len(tds)} ячеек вместо {len(heads)}")
    out = []
    for label, code in PERIODS.items():
        cell = _text(tds[heads.index(label)])
        require(cell == "" or cell in ("-", "—") or re.fullmatch(r"[+\-−]?\d[\d\s]*[.,]?\d*\s*%", cell) is not None,
                f"таблица доходности, {label}: не процент {cell!r}")
        value = pct(cell.replace("−", "-"))
        if value is not None:
            out.append(OfficialReturn(code, value, as_of, source_url))
    return out


def pays_income(content) -> bool:
    """В «Описании фонда» сказано о выплате дохода пайщикам (у ТРИНФИКО — «Лидар», ежеквартально)."""
    desc = _soup(content).select_one("#pif-desc")
    return desc is not None and _INCOME_RE.search(_text(desc)) is not None


# ---------- сбор ----------
class TrinficoAdapter(Adapter):
    key = "trinfico"
    title = "УК ТРИНФИКО"
    uk_inns = ("7701155020",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        state: dict = {"pages": {}}

        def site_list() -> dict[str, str]:
            # Один запрос списка на всю УК; ошибку запоминаем и повторяем для каждого фонда,
            # чтобы guarded() поставил каждому свой статус с причиной.
            if "error" in state:
                raise state["error"]
            if "list" not in state:
                try:
                    raw = ctx.http.get(LIST_URL, fmt="html", funds=[f.rules for f in funds])
                    state["list"] = parse_fund_list(raw.content())
                except Exception as e:  # noqa: BLE001 — статус определит guarded()
                    state["error"] = e
                    raise
            return state["list"]

        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, site_list(), state["pages"]))
                for f in funds]

    @staticmethod
    def _page(ctx: Context, path: str, fund: Fund, pages: dict) -> tuple:
        """Страница фонда -> (RawDoc, soup, номер правил, название); в пределах сбора — один раз."""
        if path not in pages:
            raw = ctx.http.get(BASE + path, fmt="html", funds=[fund.rules])
            soup = _soup(raw.content())
            pages[path] = (raw, soup, *parse_fund_info(soup))
        return pages[path]

    def _one(self, ctx: Context, fund: Fund, site: dict[str, str], pages: dict) -> FundData:
        # Розничные фонды реестра — ОПИФ/ИПИФ/БПИФ; страницы ЗПИФ не открываем. Сначала страница
        # с тем же названием, но принадлежность решает только номер правил на самой странице.
        want = name_key(fund.name)
        candidates = sorted((p for p, t in site.items() if not t.upper().startswith("ЗПИФ")),
                            key=lambda p: name_key(site[p]) != want)
        page = None
        for path in candidates:
            got = self._page(ctx, path, fund, pages)
            if got[2] == fund.rules:
                page = (path, *got)
                break
            if name_key(site[path]) == want:
                ctx.log.warning("[trinfico] %s: на странице %s с тем же названием — правила № %s",
                                fund.rules, path, got[2])
        if page is None:
            return FundData(fund.rules, status="not_found", page_url=LIST_URL,
                            reason=f"ни на одной из страниц открытых фондов в {LIST_URL} нет правил "
                                   f"№ {fund.rules} (проверено: {', '.join(candidates) or 'нет страниц'})")
        path, raw, soup, _, site_name = page
        page_url = BASE + path

        points = parse_history(soup)
        fd = FundData(fund.rules, currency=history_currency(soup), page_url=page_url,
                      segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)])
        kept = fd.segments[0].points
        if not points:
            if fund.status == "Формируется":
                fd.status, fd.reason = "not_formed", f"фонд формируется: таблица СЧА и пая на {page_url} пуста"
            else:
                fd.status, fd.reason = "no_history", f"таблица стоимости пая и СЧА на {page_url} пуста"
            return fd
        if not kept:
            fd.status = "partial"
            fd.reason = f"на сайте УК нет точек с {ctx.since:%d.%m.%Y} (последняя — {points[-1].date:%d.%m.%Y})"
        elif any(p.nav is None or p.price is None for p in kept):
            fd.warnings.append("в таблице сайта есть даты без СЧА или без стоимости пая")
        fetched = datetime.fromisoformat(raw.fetched_at).date() if raw.fetched_at else date.today()
        if (fetched - points[-1].date).days > STALE_DAYS:
            fd.warnings.append(f"последнее значение на сайте УК — {points[-1].date:%d.%m.%Y}")

        try:
            returns = parse_returns(soup, page_url)
            if returns is None:
                fd.warnings.append("на странице фонда нет таблицы «Динамика стоимости пая» с доходностью за периоды")
            else:
                fd.official_returns = returns
                missing = [label for label, code in PERIODS.items() if code not in {r.period for r in returns}]
                if missing:
                    fd.warnings.append(f"УК не опубликовала доходность за {', '.join(missing)}")
        except UnknownFormat as e:
            fd.errors.append(f"разбор доходности на странице фонда: {e}")
        if pays_income(soup):
            fd.warnings.append("фонд выплачивает доход пайщикам, но сумм выплат на странице фонда нет "
                               "(раздел раскрытия /disclosure/ закрыт robots.txt): ряд пая и официальная "
                               "доходность УК — без учёта выплат")
        return fd
