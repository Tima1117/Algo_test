"""ООО «УК «Финам Менеджмент» (www.fdu.ru): история СЧА и расчётной стоимости пая, официальная доходность.

Где что лежит на сайте (разведка 06.11.2026):
* /documents/disclosure/ — раскрытие информации; в меню у каждого фонда ссылка на его раздел с текстом
  «БПИФ … «Финам – Денежный рынок», правила доверительного управления фондом № 6459 от 05.09.2024» —
  отсюда номер правил (по нему сопоставляем с реестром) и адрес раздела фонда;
* в разделе фонда (/documents/disclosure/<раздел>/) пункт меню «Динамика доходности» — страница
  с таблицей «Дата / Пай* / СЧА*» и блоком итогов «СЧА: … / Пай**: … %» за выбранный период
  (форма POST timePeriod=Month|ThreeMonths|SixMonths|Year|ThreeYears|FiveYears|All, по умолчанию
  ThreeMonths; GET-параметр сайт игнорирует). Сноски: «* — значение указано в российских рублях»,
  «** — прирост расчетной стоимости пая». Адреса страниц нерегулярные (dynamics_profit_bonds,
  dynamics_profit_finam_activecontrol …), поэтому берём их из меню, а не собираем по шаблону;
* «Экспорт данных в Excel» — GET /documents/fund/report/?name=<страница>&timePeriod=<код> — на деле
  HTML-таблица «Дата / Стоимость чистых активов / Стоимость одного пая» (content-type application/ms-excel).
  Коды периодов (проверено по датам выгрузки): 0 — весь период, 1 — три года, 2 — с начала года,
  3 — три месяца, 4 — шесть месяцев, 5 — год, 6 — месяц, 7 — пять лет. Произвольный диапазон дат
  не принимается, поэтому для докачки хвоста берём самый короткий период, который покрывает
  ctx.start_for(). СЧА в рублях с копейками (не в тысячах), пай — до 4 знаков: «Финам Первый» на 03.11.2026 —
  30 751 855,86 и 269,62 (на странице 269,6200), «Финам – Денежный рынок» — 8 431 717 461,92 и 14,0891.
  Частота — по рабочим дням; у «Финам Первый» история с 10.06.2003.

Официальная доходность — «Пай**» со страницы «Динамика доходности» за периоды Month/ThreeMonths/SixMonths/Year:
прирост расчётной стоимости пая от последней даты в таблице до последней даты не позже, чем ровно
1/3/6/12 мес. назад (ThreeMonths на 03.11.2026 — от 03.08.2026; Year — от 01.11.2025, потому что
03.11.2025 не рабочий день). Если история фонда короче периода, сайт показывает прирост с начала —
это не доходность за период, такие значения не берём (warning). Выплаты дохода прирост пая не учитывает.
КИД лежат на cdn-lpbr01.finam.ru, чей robots.txt закрывает все PDF (Disallow: /*.pdf), — не берём.

TLS: сервер www.fdu.ru отдаёт не тот промежуточный сертификат (AlphaSSL CA 2025 вместо выпустившего
DV TLS CA 2025), поэтому стандартная проверка падает с «unable to get local issuer certificate».
Проверку не отключаем: недостающий промежуточный сертификат взят по адресу из расширения Authority
Information Access сертификата сайта и встроен ниже; доверяем ему только для www.fdu.ru
(Fetcher.trust_extra_ca), цепочка по-прежнему проверяется до корня GlobalSign Root R46 из системного хранилища.
robots.txt на www.fdu.ru нет (404) — ограничений нет.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..model import Fund, FundData, OfficialReturn, SeriesPoint
from ..parsing import day, norm_text, num, pct, require
from .base import Adapter, Context

HOST = "www.fdu.ru"
BASE = f"https://{HOST}"
DISCLOSURE_PAGE = f"{BASE}/documents/disclosure/"
EXPORT_URL = f"{BASE}/documents/fund/report/"

# Промежуточный сертификат «GlobalSign GCC R46 DV TLS CA 2025» (выдан GlobalSign Root R46, действует до
# 23.06.2029). Источник — CA Issuers из Authority Information Access сертификата www.fdu.ru:
# http://secure.globalsign.com/cacert/gsgccr46dvtlsca2025.crt (DER, переведён в PEM).
# SHA-256 отпечаток B5:94:10:F3:67:B9:86:E9:8E:53:A9:78:9A:65:80:55:3A:74:BE:51:52:11:D9:F6:EC:B6:E8:A1:1E:83:AE:AA
# (проверяется в тесте).
FDU_INTERMEDIATE_CA = """-----BEGIN CERTIFICATE-----
MIIFfDCCA2SgAwIBAgIRAIRDWIEh21qyhJn/roBnC6QwDQYJKoZIhvcNAQELBQAw
RjELMAkGA1UEBhMCQkUxGTAXBgNVBAoTEEdsb2JhbFNpZ24gbnYtc2ExHDAaBgNV
BAMTE0dsb2JhbFNpZ24gUm9vdCBSNDYwHhcNMjUwOTE3MDI1NTQ0WhcNMjkwNjIz
MDAwMDAwWjBUMQswCQYDVQQGEwJCRTEZMBcGA1UEChMQR2xvYmFsU2lnbiBudi1z
YTEqMCgGA1UEAxMhR2xvYmFsU2lnbiBHQ0MgUjQ2IERWIFRMUyBDQSAyMDI1MIIB
IjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApT+6pung92y3k3sp2FQMmKv2
OqcOBNr9uk8IJUf/EyIHKfYRcIdgRuUu5T2PxIsvGYCAHQ3uSG/p6E1a16aMu9LV
EG9DMXcW+rx6Why5IDOIOvRHjfPyQzAGurD4V/e71U4fW81n7pKIDtcWDFeSXoai
t1cemN088vJH/L4iCSeQQ1qWsmo9VV4F8jrwWJEjOSrLLYBGHvvuteMlXzpjX2A7
pKcjOooEYU78ygS2K0YOdyN7W2hIUvScVv5yG3sA/q4/tWXY1kyUC/Bb70IgL0/n
wAYMia+OgDo5RPb+U8RUsBN4QH9qxgM75nBiOlqh/aTrrMYfYrbWOR3vioZQDQID
AQABo4IBVTCCAVEwDgYDVR0PAQH/BAQDAgGGMBMGA1UdJQQMMAoGCCsGAQUFBwMB
MBIGA1UdEwEB/wQIMAYBAf8CAQAwHQYDVR0OBBYEFAhp6X+lq/TDYeXNjntH74sp
ZI3sMB8GA1UdIwQYMBaAFANcq3OBh6jMsKbVlOI2lkn/BZksMHsGCCsGAQUFBwEB
BG8wbTAuBggrBgEFBQcwAYYiaHR0cDovL29jc3AuZ2xvYmFsc2lnbi5jb20vcm9v
dHI0NjA7BggrBgEFBQcwAoYvaHR0cDovL3NlY3VyZS5nbG9iYWxzaWduLmNvbS9j
YWNlcnQvcm9vdHI0Ni5jcnQwNgYDVR0fBC8wLTAroCmgJ4YlaHR0cDovL2NybC5n
bG9iYWxzaWduLmNvbS9yb290cjQ2LmNybDAhBgNVHSAEGjAYMAgGBmeBDAECATAM
BgorBgEEAaAyCgEDMA0GCSqGSIb3DQEBCwUAA4ICAQAqoA/ThPjFFFZKSDkoKTKx
KA/K5/1469Z9wYkqd2P5btK/Hi+pUOpuJo39cHmXaNJrmVgYwTxo4n3VAB7DQWsm
OXYOz5wOGLl6Aj5eR8aJ0GXGz1zS2iXsWwFjakS2WfLC25HE2t6XI2pQgNG0HyEY
oS90ddxMW51dsm5fUDy9AMGitsgHFjzibI7UbHr4i3srXk08E4Br8N7Sz0YAxjhC
PANw4A8FaB9Q17CpGZI47CRLJSr4kzxk7sIB39FoqPYd5gtfL6NugWt/x2oRQ1xM
NVa5QQDWRbiVxYoWkBmXwAWLYir/eRiEB195/CbzTDCEtqQBaId9jTFONPjkm0TZ
ryomaCABk93+GUYgeZzb/ovJ4QS6wxXTzq+ZDxHCTPF+dksaqD/CW6jmCJvEZqPy
NhgQ96lPYc7ejqY9yET/4CtYMNPupjVbfepeLzE3n/wnTLKlR3n9V0dIhCjcrlJl
m7WCAJy9nxkx0xmfUFzhFweDOCDsxfsdJ8GnOcFD2b0hyEIDnC+OSVF2G8MCwfn9
0yfY/6Vg8dmStmLCpfmlndG9aFhxQkuXTBn/5Ojy/mJaT2mt8Idr2m3pxVSpGL1h
qfCsCW+4rC3XQhQlmfaBc0205o3G8jVadKn/1yNrraM/BOBGESALCWJZTca/Z3+z
zTYbjxDvBHjVJOwXrqfjyQ==
-----END CERTIFICATE-----
"""

# Периоды выгрузки: (код timePeriod, сколько месяцев назад от последней даты; None — весь период),
# от короткого к длинному.
EXPORT_PERIODS = ((6, 1), (3, 3), (4, 6), (5, 12), (1, 36), (7, 60), (0, None))

# Официальная доходность: наш период -> значение timePeriod формы и длина в месяцах.
RETURN_PERIODS = (("1m", "Month", 1), ("3m", "ThreeMonths", 3), ("6m", "SixMonths", 6), ("1y", "Year", 12))
PERIOD_LABELS = {"1m": "1 мес.", "3m": "3 мес.", "6m": "6 мес.", "1y": "1 год"}

CURRENCY_WORDS = (("российских рубл", "RUB"), ("доллар", "USD"), ("евро", "EUR"), ("юан", "CNY"))

EXPORT_HEADERS = ("Дата", "Стоимость чистых активов", "Стоимость одного пая")
PAGE_HEADERS = ("Дата", "Пай*", "СЧА*")


# ---------------------------------------------------------------- TLS

def _trust_fdu_ca(http) -> bool:
    """Доверять недостающему промежуточному сертификату только для www.fdu.ru (Fetcher.trust_extra_ca)."""
    trust = getattr(http, "trust_extra_ca", None)
    if trust is None:
        return False                 # не Fetcher (тестовый двойник)
    trust(HOST, FDU_INTERMEDIATE_CA)
    return True


# ---------------------------------------------------------------- разбор

def _soup(content: bytes | str) -> BeautifulSoup:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    return BeautifulSoup(text, "lxml")


def _cell(el) -> str:
    return el.get_text(" ", strip=True).replace("\xa0", " ")


@dataclass
class SiteFund:
    rules: str        # номер правил, как написан на сайте: «6459», «0097-59837006»
    name: str         # название в кавычках: «Финам – Денежный рынок»
    title: str        # вся подпись ссылки
    url: str          # раздел фонда в раскрытии


_FUND_LINK = re.compile(r"^(?P<title>.+?),\s*правила доверительного управления фондом\s*№\s*"
                        r"(?P<rules>[0-9A-Za-zА-Яа-яЁё-]+)\s+от\s+\d{2}\.\d{2}\.\d{4}", re.I)
_QUOTED = re.compile(r"[«\"“](.+?)[»\"”]")


def parse_disclosure(content: bytes | str) -> dict[str, SiteFund]:
    """Меню раскрытия -> {номер правил на сайте: фонд}."""
    soup = _soup(content)
    funds: dict[str, SiteFund] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not urlsplit(urljoin(BASE, href)).path.startswith("/documents/disclosure/"):
            continue
        m = _FUND_LINK.match(_cell(a))
        if not m:
            continue
        title, rules = m.group("title").strip(), m.group("rules")
        q = _QUOTED.search(title)
        require(q is not None, f"раскрытие: у фонда с правилами № {rules} нет названия в кавычках: {title!r}")
        url = urljoin(BASE, href.strip())
        old = funds.get(rules)
        require(old is None or old.url == url, f"раскрытие: правила № {rules} у двух разделов ({old and old.url}, {url})")
        funds[rules] = SiteFund(rules=rules, name=q.group(1).strip(), title=title, url=url)
    require(bool(funds), "в меню раскрытия нет ссылок «…, правила доверительного управления фондом № … от …»")
    return funds


def parse_fund_menu(content: bytes | str, fund_url: str) -> str:
    """Раздел фонда -> адрес страницы «Динамика доходности» этого фонда."""
    soup = _soup(content)
    root = urlsplit(fund_url).path
    found = {urljoin(BASE, a["href"].strip()) for a in soup.find_all("a", href=True)
             if _cell(a).lower() == "динамика доходности"
             and urlsplit(urljoin(BASE, a["href"].strip())).path.startswith(root)}
    require(len(found) == 1, f"в разделе {fund_url} не найдено (или несколько) ссылок «Динамика доходности»: "
                             f"{sorted(found)}")
    return found.pop()


@dataclass
class Dynamics:
    """Страница «Динамика доходности» за один период."""
    period: str               # выбранный период формы: Month, ThreeMonths, …
    growth_pct: float | None  # «Пай**» — прирост расчётной стоимости пая за период, %
    latest: date              # последняя дата в таблице
    earliest: date            # первая дата в таблице (база прироста)
    currency: str
    export_name: str          # имя для выгрузки /documents/fund/report/?name=…


def parse_dynamics(content: bytes | str) -> Dynamics:
    soup = _soup(content)
    sel = soup.find("select", attrs={"name": "timePeriod"})
    require(sel is not None, "на странице «Динамика доходности» нет выбора периода (select timePeriod)")
    opt = sel.find("option", selected=True)
    require(opt is not None and opt.get("value"), "в выборе периода не отмечен текущий период")

    table = soup.find("table", class_="pies-table")
    require(table is not None, "нет таблицы динамики (table.pies-table)")
    heads = tuple(_cell(th) for th in table.find_all("th"))
    require(heads == PAGE_HEADERS, f"таблица динамики: колонки {heads}, ожидались {PAGE_HEADERS}")
    dates = [day(_cell(tr.find("td"))) for tr in table.select("tbody tr") if tr.find("td")]
    require(bool(dates), "таблица динамики пуста")

    res = soup.find("table", class_="results-table")
    require(res is not None, "нет блока итогов за период (table.results-table)")
    rows = {_cell(tds[0]): _cell(tds[1]) for tds in (tr.find_all("td") for tr in res.find_all("tr")) if len(tds) == 2}
    require("Пай**:" in rows, f"в итогах за период нет строки «Пай**:», есть {sorted(rows)}")

    notes = " ".join(_cell(p) for p in soup.select(".footnote p")).lower()
    require("прирост расчетной стоимости пая" in notes.replace("ё", "е"),
            f"сноска «** — прирост расчетной стоимости пая» не найдена: {notes!r}")
    currency = next((c for word, c in CURRENCY_WORDS if word in notes), None)
    require(currency is not None, f"валюта не указана в сноске «* — значение указано в …»: {notes!r}")

    link = next((a["href"] for a in soup.find_all("a", href=True) if "/documents/fund/report/" in a["href"]), None)
    require(link is not None, "нет ссылки «Экспорт данных в Excel» (/documents/fund/report/)")
    names = parse_qs(urlsplit(link).query).get("name") or []
    require(len(names) == 1 and bool(names[0].strip()), f"в ссылке выгрузки нет параметра name: {link!r}")

    return Dynamics(period=opt["value"], growth_pct=pct(rows["Пай**:"]), latest=max(dates), earliest=min(dates),
                    currency=currency, export_name=names[0].strip())


def parse_export(content: bytes | str) -> list[SeriesPoint]:
    """«Экспорт данных в Excel» (HTML-таблица) -> точки по возрастанию даты; СЧА и пай в валюте пая."""
    soup = _soup(content)
    table = soup.find("table")
    require(table is not None, "в выгрузке нет таблицы")
    heads = tuple(_cell(th) for th in table.find_all("th"))
    require(heads == EXPORT_HEADERS, f"выгрузка: колонки {heads}, ожидались {EXPORT_HEADERS}")
    body = table.find("tbody")
    require(body is not None, "в выгрузке нет tbody")
    points: dict[date, SeriesPoint] = {}
    for tr in body.find_all("tr"):
        cells = [_cell(td) for td in tr.find_all("td")]
        if not cells:
            continue
        require(len(cells) == 3, f"выгрузка: в строке {len(cells)} ячеек вместо 3: {cells}")
        stamp = cells[0].split(" ")[0]          # «03.11.2026 0:00:00», «03.04.2026 11:16:00» — время не нужно
        d = day(stamp)
        require(d not in points, f"выгрузка: дата {d:%d.%m.%Y} повторяется")
        p = SeriesPoint(date=d, nav=num(cells[1]), price=num(cells[2]))
        require(p.nav is not None or p.price is not None, f"выгрузка: на {d:%d.%m.%Y} нет ни СЧА, ни пая")
        points[d] = p
    return [points[d] for d in sorted(points)]


# ---------------------------------------------------------------- периоды

def months_back(d: date, months: int) -> date:
    """Та же дата n месяцев назад (31-е -> последний день более короткого месяца)."""
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, min(d.day, (nxt - timedelta(days=1)).day))


def export_period(start: date, today: date) -> int:
    """Самый короткий период выгрузки, который точно начинается не позже start.

    Период отсчитывается от последней опубликованной даты (не позже today), поэтому его начало —
    не позже, чем today минус длина периода; неделя запаса — на праздники и поздние публикации.
    """
    for code, months in EXPORT_PERIODS:
        if months is None or months_back(today, months) <= start - timedelta(days=7):
            return code
    raise AssertionError("недостижимо: последний период — весь")


def _today() -> date:
    """Сегодня (вынесено, чтобы тесты не зависели от даты запуска)."""
    return date.today()


def _rules_key(rules: str) -> str:
    """Номер правил без буквы при номере: «0097а-59837006» (реестр ЦБ) и «0097-59837006» (сайт)."""
    s = re.sub(r"\s+", "", str(rules)).lower()
    return re.sub(r"^(\d+)[a-zа-яё](?=-)", r"\1", s)


# ---------------------------------------------------------------- адаптер

class FinamAdapter(Adapter):
    key = "finam"
    title = "Финам Менеджмент"
    uk_inns = ("7744002606",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        if not _trust_fdu_ca(ctx.http):
            ctx.log.warning("[finam] не удалось подключить промежуточный сертификат для %s", HOST)
        failure: Exception | None = None
        site: dict[str, SiteFund] = {}
        try:
            site = parse_disclosure(ctx.http.get(DISCLOSURE_PAGE, fmt="html", funds=[f.rules for f in funds]).content())
        except Exception as e:  # noqa: BLE001 — превратится в статус каждого фонда через guarded()
            failure = e
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, site, failure)) for f in funds]

    @staticmethod
    def _match(fund: Fund, site: dict[str, SiteFund]) -> tuple[SiteFund | None, str]:
        if fund.rules in site:
            return site[fund.rules], ""
        # Номер без буквы при номере принимаем, только если совпало и название фонда.
        same = [s for s in site.values() if _rules_key(s.rules) == _rules_key(fund.rules)
                and norm_text(s.name) == norm_text(fund.name)]
        if len(same) == 1:
            s = same[0]
            return s, (f"номер правил на сайте УК «{s.rules}», в реестре ЦБ «{fund.rules}»: сопоставлено по цифрам "
                       f"номера и названию «{s.name}»")
        return None, ""

    def _fund(self, ctx: Context, fund: Fund, site: dict[str, SiteFund], failure: Exception | None) -> FundData:
        if failure is not None:
            raise failure
        sf, note = self._match(fund, site)
        if sf is None:
            listed = ", ".join(f"№ {s.rules}" for s in site.values())
            return FundData(fund.rules, status="not_found",
                            reason=f"в раскрытии {DISCLOSURE_PAGE} нет фонда с правилами № {fund.rules} "
                                   f"(есть: {listed})")
        dyn_url = parse_fund_menu(ctx.http.get(sf.url, fmt="html", funds=[fund.rules]).content(), sf.url)
        fd = FundData(fund.rules, page_url=dyn_url)
        if note:
            fd.warnings.append(note)

        # Страницы «Динамика доходности» за 1/3/6/12 мес.: доходность, валюта и имя выгрузки.
        pages: dict[str, Dynamics] = {}
        for period, form_value, _ in RETURN_PERIODS:
            raw = ctx.http.get(dyn_url, method="POST", data={"timePeriod": form_value}, fmt="html",
                               funds=[fund.rules])
            dyn = parse_dynamics(raw.content())
            require(dyn.period == form_value, f"запрошен период {form_value}, страница показала {dyn.period}")
            pages[period] = dyn
        first = pages["1m"]
        require(all(d.export_name == first.export_name and d.currency == first.currency for d in pages.values()),
                "страницы одного фонда за разные периоды расходятся в валюте или имени выгрузки")
        fd.currency = first.currency
        self._returns(fd, pages, dyn_url)

        # История — выгрузка за самый короткий период, покрывающий нужное начало.
        code = export_period(ctx.start_for(fund.rules), _today())
        raw = ctx.http.get(EXPORT_URL, params={"name": first.export_name, "timePeriod": code}, fmt="html",
                           funds=[fund.rules])
        points = parse_export(raw.content())
        if not points:
            status = "not_formed" if fund.status == "Формируется" else "no_history"
            return FundData(fund.rules, page_url=dyn_url, status=status,
                            reason=f"в выгрузке «Динамики доходности» нет ни одной даты ({raw.url})")
        fd.segments = [self.segment(raw, points, page_url=dyn_url, since=ctx.since)]

        lag = (_today() - points[-1].date).days
        if lag > 10:
            fd.warnings.append(f"последняя публикация УК — {points[-1].date:%d.%m.%Y}, {lag} дн. назад")
        if "с выплатой" in norm_text(fund.name):
            fd.warnings.append("фонд с выплатой дохода: на сайте УК «Пай**» — прирост расчётной стоимости пая, "
                               "выплаты он не учитывает; суммы выплат в машиночитаемом виде на сайте не найдены")
        return fd

    @staticmethod
    def _returns(fd: FundData, pages: dict[str, Dynamics], url: str) -> None:
        short = []
        for period, _, months in RETURN_PERIODS:
            dyn = pages[period]
            if dyn.growth_pct is None:
                continue
            # История короче периода: сайт считает прирост с первой даты фонда — это не доходность за период.
            if dyn.earliest > months_back(dyn.latest, months) + timedelta(days=3):
                short.append(PERIOD_LABELS[period])
                continue
            fd.official_returns.append(OfficialReturn(period, dyn.growth_pct, dyn.latest, url))
        if short:
            since = min(d.earliest for d in pages.values())
            fd.warnings.append(f"доходность за {', '.join(short)} не берём: история фонда на сайте УК с "
                               f"{since:%d.%m.%Y}, короче периода (сайт показывает прирост с начала)")
