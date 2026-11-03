"""ООО «Т-Капитал» (t-capital-funds.ru): история СЧА и расчётной стоимости пая.

Сайт — приложение на Tramvai/React. Страницы рендерятся на сервере, исходные данные лежат в HTML
в <script id="__REACT_QUERY_STATE__capital" type="application/json"> (разведка 03.11.2026):

* /documents/mutual_funds/ — дерево документов по фондам (запрос getFolderTree). У папки фонда в
  описании: ["<логотип>", "<тикер>", "Правила доверительного управления № 4100, …"] — отсюда номер
  правил (по нему сопоставляем с реестром) и тикер (по нему API). Страница фонда для человека —
  /documents/mutual_funds/<тикер>/ (отчётность фонда).
* /statistics/ — «Статистика фондов»: СЧА и расчётная стоимость пая за два последних рабочих дня,
  валюта. Те же данные — JSON {API}portfolio/dynamic (берём его: ещё и валюта фонда; у TUSD и TGLD
  СЧА и пай в долларах, у TEUR и TBEU — в евро, у остальных наших фондов — в рублях).
* История — JSON-API сайта {API}portfolio/history/fair-price?ticker=&from=&considerSplits=true
  (его вызывает график на страницах фондов сайта) и {API}portfolio/history/nav?ticker=&from= —
  {"status": "Ok", "payload": {"fairPrice"|"nav": [{"date": "2025-09-01", "value": 6.6597}, …]}}.
  Диапазон дат принимается (from), без to отдаёт до последней даты. СЧА — в валюте фонда, не в тысячах:
  на 30.09.2026 у TMOS 13 586 534 817,03 и 5,6597 — ровно как в КИД на эту дату. Частота — по рабочим
  дням (у интервального «Денежный рынок 2» — по датам расчёта СЧА, 1–2 раза в неделю).
  Для неизвестного/формирующегося тикера API отвечает HTTP 202 и {"status": "Error",
  "payload": {"message": "Нет информации по переданному isin или ticker фонда"}}.

API живёт на api-invest-gw.tinkoff.ru, а его сертификат выдан НУЦ Минцифры (Russian Trusted Root CA),
которого нет в стандартных хранилищах: без него (Fetcher.trust_extra_ca) запросы падают на проверке TLS.

Официальная доходность за 1/3/6/12 мес. на сайте есть только в КИД (PDF, раздел 5 «Доходность за
период», ссылки в /documents/mutual_funds/<тикер>/documents/). PDF не разбираем — official_returns
пустые, в warnings ссылка. Страница «Отклонение» даёт прирост пая за 20 и 250 рабочих дней — это
не 1 и 12 месяцев, не берём.

Выплаты дохода: из наших фондов платит только «Пассивный доход» (TPAY, ежемесячно). Суммы на пай —
только в PDF «Сообщение о выплате дохода по инвестиционным паям» (/documents/releases/). PDF не
разбираем: payouts пустые, а наличие таких сообщений превращаем в предупреждение — доходность по
ряду пая без выплат занижена.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date


from ..model import Fund, FundData, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require, require_keys
from .base import Adapter, Context

BASE = "https://t-capital-funds.ru"
FOLDERS_PAGE = f"{BASE}/documents/mutual_funds/"
RELEASES_PAGE = f"{BASE}/documents/releases/"
STATS_PAGE = f"{BASE}/statistics/"
FUND_PAGE = BASE + "/documents/mutual_funds/{ticker}/"
KIDS_PAGE = BASE + "/documents/mutual_funds/{ticker}/documents/"

API_HOST = "api-invest-gw.tinkoff.ru"
API = f"https://{API_HOST}/capital/funds/v1/"
DYNAMIC_API = API + "portfolio/dynamic"
NAV_API = API + "portfolio/history/nav"
PRICE_API = API + "portfolio/history/fair-price"

CURRENCIES = {"RUB", "USD", "EUR", "CNY"}
PAYOUT_NOTICE = "Сообщение о выплате дохода по инвестиционным паям"

# Корневой сертификат НУЦ Минцифры России (Russian Trusted Root CA), публичный: https://www.gosuslugi.ru/crt
# SHA-256 отпечаток D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
# (проверяется в тесте). Им подписан сертификат api-invest-gw.tinkoff.ru.
RU_ROOT_CA = """-----BEGIN CERTIFICATE-----
MIIFwjCCA6qgAwIBAgICEAAwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjIwMzAxMjEwNDE1WhcNMzIwMjI3MjEwNDE1WjBwMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMSAwHgYDVQQDDBdSdXNzaWFuIFRydXN0ZWQgUm9v
dCBDQTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAMfFOZ8pUAL3+r2n
qqE0Zp52selXsKGFYoG0GM5bwz1bSFtCt+AZQMhkWQheI3poZAToYJu69pHLKS6Q
XBiwBC1cvzYmUYKMYZC7jE5YhEU2bSL0mX7NaMxMDmH2/NwuOVRj8OImVa5s1F4U
zn4Kv3PFlDBjjSjXKVY9kmjUBsXQrIHeaqmUIsPIlNWUnimXS0I0abExqkbdrXbX
YwCOXhOO2pDUx3ckmJlCMUGacUTnylyQW2VsJIyIGA8V0xzdaeUXg0VZ6ZmNUr5Y
Ber/EAOLPb8NYpsAhJe2mXjMB/J9HNsoFMBFJ0lLOT/+dQvjbdRZoOT8eqJpWnVD
U+QL/qEZnz57N88OWM3rabJkRNdU/Z7x5SFIM9FrqtN8xewsiBWBI0K6XFuOBOTD
4V08o4TzJ8+Ccq5XlCUW2L48pZNCYuBDfBh7FxkB7qDgGDiaftEkZZfApRg2E+M9
G8wkNKTPLDc4wH0FDTijhgxR3Y4PiS1HL2Zhw7bD3CbslmEGgfnnZojNkJtcLeBH
BLa52/dSwNU4WWLubaYSiAmA9IUMX1/RpfpxOxd4Ykmhz97oFbUaDJFipIggx5sX
ePAlkTdWnv+RWBxlJwMQ25oEHmRguNYf4Zr/Rxr9cS93Y+mdXIZaBEE0KS2iLRqa
OiWBki9IMQU4phqPOBAaG7A+eP8PAgMBAAGjZjBkMB0GA1UdDgQWBBTh0YHlzlpf
BKrS6badZrHF+qwshzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qwshzAS
BgNVHRMBAf8ECDAGAQH/AgEEMA4GA1UdDwEB/wQEAwIBhjANBgkqhkiG9w0BAQsF
AAOCAgEAALIY1wkilt/urfEVM5vKzr6utOeDWCUczmWX/RX4ljpRdgF+5fAIS4vH
tmXkqpSCOVeWUrJV9QvZn6L227ZwuE15cWi8DCDal3Ue90WgAJJZMfTshN4OI8cq
W9E4EG9wglbEtMnObHlms8F3CHmrw3k6KmUkWGoa+/ENmcVl68u/cMRl1JbW2bM+
/3A+SAg2c6iPDlehczKx2oa95QW0SkPPWGuNA/CE8CpyANIhu9XFrj3RQ3EqeRcS
AQQod1RNuHpfETLU/A2gMmvn/w/sx7TB3W5BPs6rprOA37tutPq9u6FTZOcG1Oqj
C/B7yTqgI7rbyvox7DEXoX7rIiEqyNNUguTk/u3SZ4VXE2kmxdmSh3TQvybfbnXV
4JbCZVaqiZraqc7oZMnRoWrXRG3ztbnbes/9qhRGI7PqXqeKJBztxRTEVj8ONs1d
WN5szTwaPIvhkhO3CO5ErU2rVdUr89wKpNXbBODFKRtgxUT70YpmJ46VVaqdAhOZ
D9EUUn4YaeLaS8AjSF/h7UkjOibNc4qVDiPP+rkehFWM66PVnP1Msh93tc+taIfC
EYVMxjh8zNbFuoc7fzvvrFILLe7ifvEIUqSVIC/AzplM/Jxw7buXFeGP1qVCBEHq
391d/9RAfaZ12zkwFsl+IKwE/OZxW8AHa9i1p4GO0YSNuczzEm4=
-----END CERTIFICATE-----
"""


class NoData(Exception):
    """API ответило «нет информации по тикеру» (фонд ещё не считает СЧА или тикер не тот)."""


# ---------------------------------------------------------------- TLS для API

def _trust_ru_root_ca(http) -> bool:
    """Доверять НУЦ Минцифры только для домена API (общий Fetcher.trust_extra_ca)."""
    trust = getattr(http, "trust_extra_ca", None)
    if trust is None:
        return False                 # не Fetcher (тестовый двойник)
    trust(API_HOST, RU_ROOT_CA)
    return True


# ---------------------------------------------------------------- разбор

def _query_state(content: bytes | str, key: str):
    """Данные запроса react-query из серверного HTML страницы (queryKey[0] == key)."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    m = re.search(r'<script[^>]*id="__REACT_QUERY_STATE__capital"[^>]*>(.*?)</script>', text, re.S)
    require(m is not None, "на странице нет __REACT_QUERY_STATE__capital")
    try:
        state = json.loads(m.group(1))
    except ValueError as e:
        raise UnknownFormat(f"__REACT_QUERY_STATE__capital — не JSON: {e}") from e
    require_keys(state, ["queries"], "__REACT_QUERY_STATE__capital")
    for q in state["queries"]:
        if isinstance(q, dict) and (q.get("queryKey") or [None])[0] == key:
            require_keys(q.get("state"), ["data"], f"запрос {key}")
            return q["state"]["data"]
    raise UnknownFormat(f"в состоянии страницы нет запроса {key}")


@dataclass
class Folder:
    ticker: str
    name: str


_DESCR = re.compile(r'\[\s*"[^"]*"\s*,\s*"(?P<ticker>[^"]+)"\s*,\s*["\'](?P<text>[\s\S]*?)["\']\s*\]')
_RULES = re.compile(r"№\s*(\d{3,5}(?:-[0-9А-Яа-я]+)?)")


def parse_folders(content: bytes | str) -> dict[str, Folder]:
    """/documents/mutual_funds/ -> {номер правил: Folder(тикер, название папки)}."""
    data = _query_state(content, "getFolderTree")
    require(isinstance(data, list) and data, "getFolderTree: ожидался непустой список папок фондов")
    out: dict[str, Folder] = {}
    for folder in data:
        require_keys(folder, ["name", "description"], "папка фонда")
        m = _DESCR.search(folder.get("description") or "")
        if not m:
            continue                 # папка без карточки фонда
        r = _RULES.search(m.group("text"))
        if not r:
            continue
        rules = r.group(1)
        require(rules not in out, f"номер правил {rules} у двух папок: {out.get(rules)} и {folder['name']}")
        out[rules] = Folder(ticker=m.group("ticker").strip(), name=folder["name"].strip())
    require(bool(out), "ни у одной папки фонда нет тикера и номера правил в описании")
    return out


@dataclass
class DynFund:
    ticker: str
    isin: str | None
    name: str
    currency: str


def parse_dynamic(content: bytes | str | dict) -> dict[str, DynFund]:
    """JSON portfolio/dynamic (таблица «Статистика фондов») -> {тикер: DynFund}."""
    data = content if isinstance(content, dict) else json.loads(content)
    require_keys(data, ["status", "payload"], "portfolio/dynamic")
    require(data["status"] == "Ok", f"portfolio/dynamic: статус {data['status']!r}")
    require_keys(data["payload"], ["funds"], "portfolio/dynamic.payload")
    out: dict[str, DynFund] = {}
    for f in data["payload"]["funds"]:
        require_keys(f, ["ticker", "isin", "name", "currency", "metrics"], "portfolio/dynamic: фонд")
        require(f["currency"] in CURRENCIES, f"{f['ticker']}: незнакомая валюта {f['currency']!r}")
        types = {m.get("type") for m in f["metrics"] if isinstance(m, dict)}
        require({"nav", "fairPrice"} <= types, f"{f['ticker']}: в metrics нет nav/fairPrice ({types})")
        out[f["ticker"]] = DynFund(f["ticker"], f["isin"], f["name"], f["currency"])
    require(bool(out), "portfolio/dynamic: пустой список фондов")
    return out


def _history(content: bytes | str | dict, field: str) -> list[tuple]:
    data = content if isinstance(content, dict) else json.loads(content)
    require_keys(data, ["status", "payload"], f"history/{field}")
    if data["status"] == "Error":
        msg = data["payload"].get("message") if isinstance(data["payload"], dict) else None
        raise NoData(msg or "API вернуло ошибку без текста")
    require(data["status"] == "Ok", f"history/{field}: статус {data['status']!r}")
    require_keys(data["payload"], [field], f"history/{field}.payload")
    rows = data["payload"][field]
    require(isinstance(rows, list), f"history/{field}: ожидался список точек")
    out: dict = {}
    for i, row in enumerate(rows):
        require_keys(row, ["date", "value"], f"history/{field}[{i}]")
        d, v = day(row["date"]), num(row["value"])
        if v is None or v == 0:
            continue                 # нет значения — не 0
        require(v > 0, f"history/{field}: отрицательное значение {v} на {d}")
        require(out.get(d, v) == v, f"history/{field}: два разных значения на {d}")
        out[d] = v
    return sorted(out.items())


def parse_nav(content: bytes | str | dict) -> list[SeriesPoint]:
    """portfolio/history/nav -> точки СЧА (в валюте фонда)."""
    return [SeriesPoint(d, v, None) for d, v in _history(content, "nav")]


def parse_fair_price(content: bytes | str | dict) -> list[SeriesPoint]:
    """portfolio/history/fair-price -> точки расчётной стоимости пая (в валюте фонда)."""
    return [SeriesPoint(d, None, v) for d, v in _history(content, "fairPrice")]


def parse_payout_notices(content: bytes | str) -> dict[str, list[dict]]:
    """/documents/releases/ -> {norm_text(название фонда): [{"name", "link", "available"}]} —
    сообщения о выплате дохода (сами суммы — только в PDF)."""
    data = _query_state(content, "getFolderTree")
    require(isinstance(data, list) and data, "getFolderTree: ожидался непустой список папок")
    out: dict[str, list[dict]] = {}

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "file" and PAYOUT_NOTICE in re.sub(r"\s+", " ", node.get("name", "")):
            fund = re.search(r"«(.+)»", node["name"])
            require(fund is not None, f"в сообщении о выплате нет названия фонда: {node['name']!r}")
            when = re.search(r"Доступен с:\s*(\d{2}\.\d{2}\.\d{4})", node.get("description") or "")
            out.setdefault(norm_text(fund.group(1)), []).append(
                {"name": node["name"], "link": node.get("link"), "available": day(when.group(1)) if when else None})
        for child in node.get("children") or []:
            walk(child)

    for node in data:
        walk(node)
    return out


# ---------------------------------------------------------------- сбор

class TCapitalAdapter(Adapter):
    key = "t_capital"
    title = "Т-Капитал"
    uk_inns = ("7743304530",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        if not _trust_ru_root_ca(ctx.http):
            ctx.log.warning("[t_capital] не удалось подключить корневой сертификат НУЦ Минцифры для %s", API_HOST)
        all_rules = [f.rules for f in funds]
        failure: Exception | None = None
        folders: dict[str, Folder] = {}
        dynamic: dict[str, DynFund] = {}
        notices: dict[str, list[dict]] = {}
        try:
            folders = parse_folders(ctx.http.get(FOLDERS_PAGE, fmt="html", funds=all_rules).content())
            dynamic = parse_dynamic(ctx.http.get(DYNAMIC_API, fmt="json", funds=all_rules).content())
        except Exception as e:  # noqa: BLE001 — превратится в статус каждого фонда через guarded()
            failure = e
        if failure is None:
            try:
                notices = parse_payout_notices(ctx.http.get(RELEASES_PAGE, fmt="html").content())
            except Exception as e:  # noqa: BLE001 — сообщения о выплатах нужны только для предупреждения
                ctx.log.warning("[t_capital] сообщения о выплатах не разобраны: %s", e)
        return [self.guarded(ctx, f, lambda f=f: self._fund(ctx, f, folders, dynamic, notices, failure))
                for f in funds]

    def _fund(self, ctx: Context, fund: Fund, folders: dict[str, Folder], dynamic: dict[str, DynFund],
              notices: dict[str, list[dict]], failure: Exception | None) -> FundData:
        if failure is not None:
            raise failure
        forming = fund.status == "Формируется"
        folder = folders.get(fund.rules)
        if folder is None:
            return FundData(fund.rules, status="not_formed" if forming else "not_found",
                            reason=f"в раскрытии {FOLDERS_PAGE} нет фонда с правилами № {fund.rules}")
        ticker = folder.ticker
        page_url = FUND_PAGE.format(ticker=ticker)
        dyn = dynamic.get(ticker)
        if dyn is None:
            if forming:
                return FundData(fund.rules, page_url=page_url, status="not_formed",
                                reason=f"фонд формируется: в раскрытии есть ({ticker}), а СЧА и стоимость пая "
                                       f"на сайте УК ещё не публикуются")
            return FundData(fund.rules, page_url=page_url, status="no_history",
                            reason=f"фонд есть в раскрытии ({ticker}), но в статистике СЧА на сайте УК "
                                   f"его нет — СЧА и стоимость пая не публикуются")
        if fund.isin and dyn.isin and fund.isin != dyn.isin:
            raise UnknownFormat(f"правила № {fund.rules} -> {ticker}, но ISIN на сайте {dyn.isin}, "
                                f"а в реестре {fund.isin}")

        rules = [fund.rules]
        fd = FundData(fund.rules, currency=dyn.currency, page_url=page_url)
        params = {"ticker": ticker, "from": ctx.start_for(fund.rules).isoformat()}
        raw_nav = ctx.http.get(NAV_API, params=params, fmt="json", funds=rules)
        raw_price = ctx.http.get(PRICE_API, params={**params, "considerSplits": "true"}, fmt="json", funds=rules)
        empty: list[str] = []
        for raw, parse in ((raw_nav, parse_nav), (raw_price, parse_fair_price)):
            try:
                points = parse(raw.content())
            except NoData as e:
                empty.append(str(e))
                points = []
            fd.segments.append(self.segment(raw, points, page_url=STATS_PAGE, since=ctx.since))
        n_nav, n_price = (len(s.points) for s in fd.segments)

        fd.warnings.append(f"официальная доходность за 1/3/6/12 мес. публикуется УК только в КИД (PDF): "
                           f"{KIDS_PAGE.format(ticker=ticker)} — PDF не разбираем")
        paid = notices.get(norm_text(fund.name)) or notices.get(norm_text(dyn.name.split("«")[-1].rstrip("»")))
        if paid:
            recent = [n for n in paid if n["available"] is None or n["available"] >= ctx.since]
            latest = max(paid, key=lambda n: n["available"] or date.min)
            fd.warnings.append(f"фонд выплачивает доход пайщикам ({len(recent)} сообщений о выплате с "
                               f"{ctx.since:%d.%m.%Y}, последнее: {latest['link']}); суммы на пай есть только "
                               f"в PDF — payouts не заполнены, доходность по ряду пая без выплат занижена")

        if not n_nav and not n_price:
            fd.status = "not_formed" if forming else "no_history"
            fd.reason = (f"API сайта УК: {empty[0]}" if empty else
                         f"API сайта УК не вернуло точек с {params['from']}")
        elif not n_nav or not n_price:
            fd.status = "partial"
            fd.reason = "на сайте УК есть только " + ("стоимость пая, без СЧА" if not n_nav else "СЧА, без стоимости пая")
        return fd
