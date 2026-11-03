"""ООО «УК «МЕТРОПОЛЬ» (am-metropol.ru, раздел ПИФ — pif.am-metropol.ru) — один действующий ОПИФ РФИ
«Метрополь Золотое руно» (правила 0147-70232539). Зевс, Афина, Посейдон на сайте остались, но их
значения заморожены на датах 2017–2018 гг. (фонды прекращены).

Источники (старый ASP.NET-сайт, обычный HTML без JS-данных):
* https://pif.am-metropol.ru/index.wbp — главная раздела ПИФ:
  - «Сообщение о стоимости чистых активов и расчетной стоимости инвестиционного пая»: строка фонда
    со ссылкой /<slug>/ (у «Золотого руна» — /golden_fleece/; в названии на сайте опечатка
    «Метрополь Золото еруно»), цена пая (руб.), СЧА (тыс. руб.), дата, и «Изменение … %» за 3 мес,
    6 мес, год, 3 года с пометкой «опубликовано ДД.ММ.ГГГГ …, доступно 1 месяц». Дата в заголовке
    этих колонок («Изменение на 18.10.2018») — застывшая, от прекращённого фонда; на деле значения —
    на последний рабочий день прошлого месяца: 6,26 / -5,78 / -2,68 / -27,29 % от 02.11.2026 ровно
    пересчитываются из ряда на 30.10.2026 (от 31.07.2026, 30.04.2026, 31.10.2025, 31.10.2023).
    Поэтому as_of = последняя дата ряда не позже конца месяца перед датой публикации, и адаптер
    проверяет это пересчётом (не сошлось — предупреждение). Доходности за 1 мес УК не публикует;
  - в подвале: «… «Метрополь Золотое руно» … Правила … зарегистрированы … за № 0147-70232539» —
    по этому тексту фонд реестра (rules) связывается с названием фонда на сайте;
* https://pif.am-metropol.ru/<slug>/graph/export.wbp — «Экспорт котировок "Метрополь Золотое руно"»:
  HTML-таблица «Дата | Цена | СЧА» за всю историю с 08.12.2003, каждый рабочий день, новые сверху
  (~1 МБ). Параметров дат страница не принимает (start-date/end-date игнорируются). СЧА здесь — в
  рублях (11 977 302,22 на 02.11.2026; на главной то же в тыс. руб.: 11 977), цена — в рублях.
  Заголовок страницы сверяем с названием фонда. Ошибка данных УК: за 05.03.2026 в экспорте цена и
  СЧА переставлены местами (14 546 018,65 / 413,64) — такую строку не берём, пишем предупреждение.

Сеть: домен отдаёт два IP; с некоторых хостингов ответы длиннее ~16 КБ с одного из них обрываются
(TCP-соединение «замирает»). Fetcher повторяет запрос, а если не вышло — 'unavailable'/'stale'.
Сообщений о выплате дохода пайщикам на сайте нет — payouts не заполняем.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import day, norm_text, num, pct, require
from .base import Adapter, Context

BASE = "https://pif.am-metropol.ru"
HOME_URL = f"{BASE}/index.wbp"
EXPORT_URL = BASE + "/{slug}/graph/export.wbp"
PAGE_URL = BASE + "/{slug}/graph/"

_SLUG = re.compile(r"^/([a-z_]+)/$")
_FOOTER_FUND = re.compile(r"фонд[^«»]{0,80}«([^«»]+)»[^№]{0,250}?№\s*(\d{4}-\d{8})")
_PUBLISHED = re.compile(r"опубликовано\s*(\d{2}\.\d{2}\.\d{4})")
_EXPORT_TITLE = re.compile(r"Экспорт котировок\s*[\"«](.+?)[\"»]")
# Колонки доходности в строке фонда на главной (после названия, значка, цены, СЧА, двух изменений, даты).
_RET_COLS = {"3m": 7, "6m": 8, "1y": 9}


def _soup(content) -> BeautifulSoup:
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="strict")
    return BeautifulSoup(content, "lxml")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def name_key(s: str) -> str:
    """Ключ названия без пробелов: на сайте «Метрополь Золото еруно» вместо «Золотое руно»."""
    return norm_text(s).replace(" ", "")


# ---------- разбор ----------

def parse_rules_names(content) -> dict[str, str]:
    """Главная -> {номер правил: название фонда} из подвала «… «Название» … Правила … № XXXX-XXXXXXXX»."""
    txt = _text(_soup(content))
    out = {rules: name.strip() for name, rules in _FOOTER_FUND.findall(txt)}
    require(bool(out), "на главной нет текста «… «фонд» … Правила … № XXXX-XXXXXXXX»")
    return out


def parse_home(content) -> tuple[date, dict[str, dict]]:
    """Главная -> (дата публикации доходности, {slug: {name, price, nav_thousands, returns}}).

    Строка фонда: <a href="/slug/">название</a> | значок | цена | СЧА, тыс. | изм. цены | изм. СЧА |
    ДД.ММ | 3 мес | 6 мес | год | 3 года. Вторая строка без ссылки — предыдущая дата, её пропускаем.
    """
    s = _soup(content)
    # Сайт свёрстан вложенными таблицами: берём самую внутреннюю ячейку с этой шапкой.
    heads = [td for td in s.find_all("td") if "Изменение на" in _text(td) and "опубликовано" in _text(td)]
    head = min(heads, key=lambda td: len(_text(td))) if heads else None
    require(head is not None, "на главной нет таблицы «Сообщение о СЧА…» с колонками «Изменение на …»")
    m = _PUBLISHED.search(_text(head))
    require(m is not None, f"в шапке доходности нет «опубликовано ДД.ММ.ГГГГ»: {_text(head)!r}")
    published = day(m.group(1))
    table = head.find_parent("table")
    labels = [_text(td) for td in table.find_all("td")]
    for need in ("3 мес", "6 мес", "год", "Цена пая,", "СЧА,"):
        require(need in labels, f"в таблице СЧА нет колонки {need!r}")
    rows: dict[str, dict] = {}
    for tr in table.find_all("tr"):
        a = tr.find("a", href=True)
        if a is None:
            continue
        m = _SLUG.match(a["href"])
        require(m is not None, f"ссылка фонда не вида /slug/: {a['href']!r}")
        tds = tr.find_all("td", recursive=False)
        require(len(tds) == 11, f"в строке фонда {a['href']} {len(tds)} ячеек, ожидали 11")
        cells = [_text(td) for td in tds]
        rows[m.group(1)] = {
            "name": _text(a),
            "price": num(cells[2]),
            "nav_thousands": num(cells[3]),
            "returns": {p: pct(cells[i]) for p, i in _RET_COLS.items()},
        }
    require(bool(rows), "в таблице СЧА на главной нет ни одной строки фонда")
    return published, rows


def parse_export(content) -> tuple[str, list[SeriesPoint], list[str]]:
    """«Экспорт котировок» -> (название фонда из заголовка, точки по возрастанию даты, пропущенные строки).

    Строку, где цена пая больше СЧА (так не бывает: СЧА = цена × число паёв), не берём и описываем:
    на сайте за 05.03.2026 стоит «Цена 14546018.65, СЧА 413.64» — колонки перепутаны УК.
    """
    s = _soup(content)
    title = s.find(string=_EXPORT_TITLE)
    require(title is not None, "нет заголовка «Экспорт котировок \"…\"»")
    name = _EXPORT_TITLE.search(str(title)).group(1)
    table = title.find_parent("p").find_next("table")
    require(table is not None, "после заголовка экспорта нет таблицы")
    trs = table.find_all("tr")
    head = [_text(td) for td in trs[0].find_all("td")] if trs else []
    require(head == ["Дата", "Цена", "СЧА"], f"шапка экспорта не «Дата | Цена | СЧА»: {head}")
    by_date: dict[date, SeriesPoint] = {}
    skipped: list[str] = []
    for tr in trs[1:]:
        cells = [_text(td) for td in tr.find_all("td")]
        require(len(cells) == 3, f"строка экспорта не из трёх ячеек: {cells}")
        d = day(cells[0])
        price, nav = num(cells[1]), num(cells[2])
        # «.00» в первые дни 2003 г. — СЧА ещё не считалась: это отсутствие данных, не ноль.
        price = price if price and price > 0 else None
        nav = nav if nav and nav > 0 else None
        if price is None and nav is None:
            continue
        if price and nav and price > nav:
            skipped.append(f"{d:%d.%m.%Y}: цена {cells[1]} > СЧА {cells[2]} (колонки перепутаны на сайте)")
            continue
        p = SeriesPoint(d, nav, price)
        require(by_date.get(d, p) == p, f"две разные строки экспорта на {d}")
        by_date[d] = p
    require(bool(by_date), "в экспорте котировок нет ни одной строки с данными")
    return name, [by_date[d] for d in sorted(by_date)], skipped


def returns_as_of(published: date, points: list[SeriesPoint]) -> date | None:
    """Последняя дата ряда не позже конца месяца, предшествующего публикации."""
    month_end = published.replace(day=1) - timedelta(days=1)
    dates = [p.date for p in points if p.date <= month_end and p.price]
    return max(dates) if dates else None


def check_returns(points: list[SeriesPoint], as_of: date, rets: dict[str, float]) -> list[str]:
    """Сверка опубликованной доходности с рядом: расхождения > 0,02 п.п. — список описаний."""
    price = {p.date: p.price for p in points if p.price}
    months = {"3m": 3, "6m": 6, "1y": 12}
    bad = []
    for period, value in rets.items():
        y, m = divmod(as_of.year * 12 + as_of.month - 1 - months[period], 12)
        base_end = date(y, m + 1, calendar.monthrange(y, m + 1)[1])
        base = [d for d in price if d <= base_end]
        if not base or as_of not in price:
            bad.append(f"{period}: нет точки ряда для сверки")
            continue
        calc = (price[as_of] / price[max(base)] - 1) * 100
        if abs(calc - value) > 0.02:
            bad.append(f"{period}: опубликовано {value}, по ряду {calc:.2f}")
    return bad


# ---------- адаптер ----------

class MetropolAdapter(Adapter):
    key = "metropol"
    title = "УК «МЕТРОПОЛЬ»"
    uk_inns = ("7706285907",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def home():
            if "home" not in state:
                raw = ctx.http.get(HOME_URL, fmt="html", funds=all_rules)
                state["home"] = (parse_rules_names(raw.content()), *parse_home(raw.content()))
            return state["home"]

        def one(fund: Fund) -> FundData:
            names, published, rows = home()
            name = names.get(fund.rules)
            if name is None:
                return FundData(fund.rules, status="not_found", page_url=HOME_URL,
                                reason=f"на {HOME_URL} нет фонда с правилами № {fund.rules}")
            slugs = [slug for slug, r in rows.items() if name_key(r["name"]) == name_key(name)]
            require(len(slugs) == 1, f"фонду «{name}» в таблице СЧА соответствуют строки {slugs}")
            slug = slugs[0]
            page_url = PAGE_URL.format(slug=slug)
            raw = ctx.http.get(EXPORT_URL.format(slug=slug), fmt="html", funds=[fund.rules])
            title, points, skipped = parse_export(raw.content())
            require(name_key(title) == name_key(name),
                    f"экспорт /{slug}/ озаглавлен «{title}», а фонд с правилами {fund.rules} — «{name}»")
            fd = FundData(fund.rules, currency="RUB", page_url=page_url,
                          segments=[self.segment(raw, points, page_url=page_url, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status, fd.reason = "no_history", f"в экспорте котировок нет точек с {ctx.since:%d.%m.%Y}"
            recent = [x for x in skipped if day(x[:10]) >= ctx.since]
            if recent:
                fd.warnings.append(f"строки экспорта пропущены как ошибочные: {'; '.join(recent)}")

            rets = {p: v for p, v in rows[slug]["returns"].items() if v is not None}
            as_of = returns_as_of(published, points)
            if as_of is None:
                fd.warnings.append("не удалось определить дату доходности на главной — доходность не взята")
            else:
                fd.official_returns = [OfficialReturn(p, v, as_of, HOME_URL) for p, v in rets.items()]
                bad = check_returns(points, as_of, rets)
                if bad:
                    fd.warnings.append(f"доходность с {HOME_URL} не сходится с рядом на {as_of}: {'; '.join(bad)}")
            fd.warnings.append("доходность за 1 мес УК на сайте не публикует (только 3/6/12 мес и 3 года)")
            # Сверка единиц: на главной СЧА в тыс. руб., в экспорте — в рублях.
            last = fd.segments[0].points[-1] if fd.segments[0].points else None
            home_nav = rows[slug]["nav_thousands"]
            if last and last.nav and home_nav and abs(last.nav / 1000 - home_nav) > max(2.0, home_nav * 0.05):
                fd.warnings.append(f"СЧА на главной {home_nav} тыс. руб. не похожа на последнюю точку "
                                   f"экспорта {last.nav} руб. ({last.date})")
            return fd

        out = []
        for f in funds:
            fd = self.guarded(ctx, f, lambda f=f: one(f))
            fd.page_url = fd.page_url or HOME_URL
            out.append(fd)
        return out
