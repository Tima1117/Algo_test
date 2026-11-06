"""ООО «УК «Атон-менеджмент» (www.am-aton.ru): история СЧА и пая, выплаты, официальная доходность.

Сайт — Next.js-приложение, все данные страниц оно берёт из собственного JSON-API /api/ (разведка 06.11.2026):

* /api/fund/ — каталог (страница «Паевые фонды» /pif): группа sectionCode="available_pif", в items у фонда
  code (адрес страницы /pif/<code>), title, full_name, type_fund_xml (opif / ipif / bpif / zpif…),
  currency_xml, qualified_investor. Номера правил в каталоге нет. Розничные фонды — opif/ipif/bpif без
  qualified_investor (это ровно 9 ОПИФ и 10 БПИФ реестра); «индикативная стоимость» и ЗПИФ туда не попадают.
* /api/fund/<code>/ — карточка фонда: поле number, например «Правила Фонда зарегистрированы ФКЦБ России
  за № 0009-46349328 от 04.03.1997 г.» — по нему сопоставляем с реестром.
* /api/fund/<code>/cost/ — график «Стоимость пая и СЧА»: data.currency (валюта фонда: RUR / USD / CNY) и
  data.chart — по блоку на валюту ({"currency": {"code": "RUR"}, "items": [{"DATE", "SCHA", "PRICE",
  "PAYMENT"}]}). Берём блок валюты фонда; у «Фонда Еврооблигаций» есть ещё пересчёт в доллары — не он.
  ВСЯ история с даты формирования по рабочим дням; диапазон дат API не принимает (страница вызывает его
  без параметров), поэтому качаем целиком (до ~1 МБ) и отрезаем по ctx.since. СЧА — в валюте фонда, не в
  тысячах: на 03.11.2026 у «Рантье» 459 566 202,7 ₽ и пай 2 074,76 ₽ — как на странице фонда.
  PAYMENT — выплата дохода на пай (у «Дивидендные акции: Рантье» раз в квартал), стоит на дате, когда
  пай уже без выплаты (30.06.2026: 2 033,73 -> 1 990,97 при выплате 45,81).
* /api/fund/<code>/profit/ — блок «Изменение стоимости пая на <дата>» страницы фонда: change_price за
  1_m / 3_m / 6_m / 1_y (3_y, 5_y не берём), '-' — периода ещё нет. LAST_DATE — последний рабочий день
  месяца, по ценам которого посчитано; страница показывает конец этого месяца (30.10.2026 -> «на
  31.10.2026»), as_of берём из LAST_DATE. У «Рантье» значения — с учётом выплат без реинвестирования:
  (пай в конце + выплаты) / пай в начале − 1 (так воспроизводится 14,1% за год).

TLS: сервер отдаёт только свой сертификат *.am-aton.ru без промежуточного «GlobalSign RSA OV SSL CA 2018»,
поэтому проверка цепочки падает («unable to get local issuer certificate»), а сборщик видел сайт
«недоступным» (ConnectError). Промежуточный сертификат скачан по адресу из расширения Authority
Information Access сертификата сайта и встроен ниже; доверяем ему только для www.am-aton.ru
(Fetcher.trust_extra_ca). Проверка TLS не ослабляется: цепочка по-прежнему должна дойти до корня
GlobalSign Root CA - R3 из системного хранилища.
"""
from __future__ import annotations

import json
import re
from datetime import date

from ..http import FetchError
from ..model import Fund, FundData, OfficialReturn, Payout, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, pct, require, require_keys
from .base import Adapter, Context

HOST = "www.am-aton.ru"
BASE = f"https://{HOST}"
LIST_URL = f"{BASE}/api/fund/"
DETAIL_URL = BASE + "/api/fund/{code}/"
COST_URL = BASE + "/api/fund/{code}/cost/"
PROFIT_URL = BASE + "/api/fund/{code}/profit/"
LIST_PAGE = f"{BASE}/pif"
PAGE_URL = BASE + "/pif/{code}"

RETAIL_TYPES = {"opif": "open", "ipif": "interval", "bpif": "exchange"}
CURRENCIES = {"RUR": "RUB", "RUB": "RUB", "USD": "USD", "EUR": "EUR", "CNY": "CNY"}
# Ключи периодов в /profit/ -> наши коды. «3 года» и «5 лет» не берём.
PERIODS = {"1_m": "1m", "3_m": "3m", "6_m": "6m", "1_y": "1y"}
STALE_DAYS = 10   # последняя точка старше — предупреждение «УК давно не обновляла»

# «№ 0009-46349328», «№ 2487»; «№ 6812-СД» (фонд для квалифицированных) — не номер правил розничного фонда
_RULES_RE = re.compile(r"№\s*(\d+(?:-\d+)?)(?![\d-])")

# Промежуточный сертификат GlobalSign RSA OV SSL CA 2018 (выдан GlobalSign Root CA - R3, до 21.11.2028),
# публичный: http://secure.globalsign.com/cacert/gsrsaovsslca2018.crt — адрес из AIA (CA Issuers)
# сертификата *.am-aton.ru; DER переведён в PEM. SHA-256 отпечаток
# B6:76:FF:A3:17:9E:88:12:09:3A:1B:5E:AF:EE:87:6A:E7:A6:AA:F2:31:07:8D:AD:1B:FB:21:CD:28:93:76:4A
# (проверяется в тесте).
GLOBALSIGN_OV_CA_2018 = """-----BEGIN CERTIFICATE-----
MIIETjCCAzagAwIBAgINAe5fIh38YjvUMzqFVzANBgkqhkiG9w0BAQsFADBMMSAw
HgYDVQQLExdHbG9iYWxTaWduIFJvb3QgQ0EgLSBSMzETMBEGA1UEChMKR2xvYmFs
U2lnbjETMBEGA1UEAxMKR2xvYmFsU2lnbjAeFw0xODExMjEwMDAwMDBaFw0yODEx
MjEwMDAwMDBaMFAxCzAJBgNVBAYTAkJFMRkwFwYDVQQKExBHbG9iYWxTaWduIG52
LXNhMSYwJAYDVQQDEx1HbG9iYWxTaWduIFJTQSBPViBTU0wgQ0EgMjAxODCCASIw
DQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAKdaydUMGCEAI9WXD+uu3Vxoa2uP
UGATeoHLl+6OimGUSyZ59gSnKvuk2la77qCk8HuKf1UfR5NhDW5xUTolJAgvjOH3
idaSz6+zpz8w7bXfIa7+9UQX/dhj2S/TgVprX9NHsKzyqzskeU8fxy7quRU6fBhM
abO1IFkJXinDY+YuRluqlJBJDrnw9UqhCS98NE3QvADFBlV5Bs6i0BDxSEPouVq1
lVW9MdIbPYa+oewNEtssmSStR8JvA+Z6cLVwzM0nLKWMjsIYPJLJLnNvBhBWk0Cq
o8VS++XFBdZpaFwGue5RieGKDkFNm5KQConpFmvv73W+eka440eKHRwup08CAwEA
AaOCASkwggElMA4GA1UdDwEB/wQEAwIBhjASBgNVHRMBAf8ECDAGAQH/AgEAMB0G
A1UdDgQWBBT473/yzXhnqN5vjySNiPGHAwKz6zAfBgNVHSMEGDAWgBSP8Et/qC5F
JK5NUPpjmove4t0bvDA+BggrBgEFBQcBAQQyMDAwLgYIKwYBBQUHMAGGImh0dHA6
Ly9vY3NwMi5nbG9iYWxzaWduLmNvbS9yb290cjMwNgYDVR0fBC8wLTAroCmgJ4Yl
aHR0cDovL2NybC5nbG9iYWxzaWduLmNvbS9yb290LXIzLmNybDBHBgNVHSAEQDA+
MDwGBFUdIAAwNDAyBggrBgEFBQcCARYmaHR0cHM6Ly93d3cuZ2xvYmFsc2lnbi5j
b20vcmVwb3NpdG9yeS8wDQYJKoZIhvcNAQELBQADggEBAJmQyC1fQorUC2bbmANz
EdSIhlIoU4r7rd/9c446ZwTbw1MUcBQJfMPg+NccmBqixD7b6QDjynCy8SIwIVbb
0615XoFYC20UgDX1b10d65pHBf9ZjQCxQNqQmJYaumxtf4z1s4DfjGRzNpZ5eWl0
6r/4ngGPoJVpjemEuunl1Ig423g7mNA2eymw0lIYkN5SQwCuaifIFJ6GlazhgDEw
fpolu4usBCOmmQDo8dIm7A9+O4orkjgTHY+GzYZSR+Y0fFukAj6KYXwidlNalFMz
hriSqHKvoflShx8xpfywgVcvzfTO3PYkz6fiNJBonf6q8amaEsybwMbDqKWwIX7e
SPY=
-----END CERTIFICATE-----
"""


# ---------------------------------------------------------------- TLS

def _trust_intermediate(http) -> bool:
    """Доверять недостающему промежуточному сертификату только для сайта УК (Fetcher.trust_extra_ca)."""
    trust = getattr(http, "trust_extra_ca", None)
    if trust is None:
        return False                 # не Fetcher (тестовый двойник)
    trust(HOST, GLOBALSIGN_OV_CA_2018)
    return True


# ---------------------------------------------------------------- разбор

def _doc(content, where: str) -> dict:
    """bytes/str/dict -> разобранный JSON ответа API."""
    if isinstance(content, (bytes, str)):
        try:
            return json.loads(content)
        except ValueError as e:
            raise UnknownFormat(f"{where}: ответ API не JSON: {str(content)[:80]!r}") from e
    return content


def _data(content, where: str) -> dict:
    """Ответ API {"status": "success", "data": {...}} -> data."""
    content = _doc(content, where)
    require_keys(content, ["status", "data"], where)
    require(content["status"] == "success", f"{where}: status={content['status']!r}")
    require(isinstance(content["data"], dict), f"{where}: data — не объект")
    return content["data"]


def norm_rules(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).replace("–", "-").replace("—", "-")


def rules_in(text: str | None) -> str | None:
    """Номер правил из фразы «… зарегистрированы … за № 0009-46349328 от 04.03.1997 г.»."""
    m = _RULES_RE.search(str(text or "").replace("&nbsp;", " ").replace("\xa0", " "))
    return norm_rules(m.group(1)) if m else None


def parse_list(content) -> list[dict]:
    """Розничные фонды из каталога /api/fund/: [{code, title, full_name, type, currency}]."""
    data = _data(content, "/api/fund/")
    require_keys(data, ["group"], "/api/fund/")
    require(isinstance(data["group"], list), "/api/fund/: group — не список")
    groups = [g for g in data["group"] if isinstance(g, dict) and g.get("sectionCode") == "available_pif"]
    require(len(groups) == 1, f"/api/fund/: групп available_pif {len(groups)}, ожидалась одна")
    require_keys(groups[0], ["items"], "/api/fund/ available_pif")
    require(isinstance(groups[0]["items"], list), "/api/fund/ available_pif: items — не список")
    out = []
    for i, it in enumerate(groups[0]["items"]):
        require_keys(it, ["code", "title", "full_name", "type_fund_xml", "currency_xml", "qualified_investor"],
                     f"available_pif.items[{i}]")
        kind = RETAIL_TYPES.get(it["type_fund_xml"])
        if kind is None or it["qualified_investor"]:
            continue                 # ЗПИФ, фонды для квалифицированных, «индикативная стоимость»
        require(isinstance(it["code"], str) and re.fullmatch(r"[A-Za-z0-9_]+", it["code"]) is not None,
                f"available_pif.items[{i}]: code {it['code']!r}")
        out.append({"code": it["code"], "title": it["title"] or "", "full_name": it["full_name"] or "",
                    "type": kind, "currency": CURRENCIES.get(it["currency_xml"])})
    require(bool(out), "/api/fund/: в available_pif нет ни одного розничного фонда")
    return out


def detail_rules(content) -> str | None:
    """Номер правил из карточки фонда /api/fund/<code>/ (поле number)."""
    data = _data(content, "/api/fund/<code>/")
    require_keys(data, ["code", "number"], "/api/fund/<code>/")
    return rules_in(data["number"])


def _chart(data: dict) -> tuple[str, list]:
    """(валюта фонда, items блока графика в этой валюте); пустой график -> (валюта, [])."""
    require_keys(data, ["currency", "chart"], "/cost/")
    currency = CURRENCIES.get(data["currency"])
    require(currency is not None, f"/cost/: незнакомая валюта фонда {data['currency']!r}")
    require(isinstance(data["chart"], list), "/cost/: chart — не список")
    blocks = []
    for i, blk in enumerate(data["chart"]):
        require_keys(blk, ["currency", "items"], f"/cost/ chart[{i}]")
        require_keys(blk["currency"], ["code"], f"/cost/ chart[{i}].currency")
        require(isinstance(blk["items"], list), f"/cost/ chart[{i}]: items — не список")
        if blk["currency"]["code"] == data["currency"]:
            blocks.append(blk["items"])
    if not data["chart"]:
        return currency, []
    require(len(blocks) == 1, f"/cost/: блоков графика в валюте фонда {data['currency']}: {len(blocks)}")
    return currency, blocks[0]


def _positive(value, where: str) -> float | None:
    v = num(value)
    require(v is None or v > 0, f"{where}: {value!r} — не положительное число")
    return v


def history_currency(content) -> str:
    """Валюта пая и СЧА ('RUB', 'USD', 'CNY', …) — как в графике на странице фонда."""
    return _chart(_data(content, "/cost/"))[0]


def parse_history(content) -> list[SeriesPoint]:
    """Ряд СЧА и расчётной стоимости пая из /api/fund/<code>/cost/ (валюта фонда)."""
    _, items = _chart(_data(content, "/cost/"))
    by: dict[date, SeriesPoint] = {}
    for i, it in enumerate(items):
        require_keys(it, ["DATE", "SCHA", "PRICE"], f"/cost/ items[{i}]")
        d = day(it["DATE"])
        p = SeriesPoint(d, _positive(it["SCHA"], f"items[{i}].SCHA"), _positive(it["PRICE"], f"items[{i}].PRICE"))
        if p.nav is None and p.price is None:
            continue
        prev = by.get(d)
        require(prev is None or prev == p, f"/cost/: на {d} две разные точки: {prev} и {p}")
        by[d] = p
    return [by[d] for d in sorted(by)]


def parse_payouts(content, source_url: str) -> list[Payout]:
    """Выплаты дохода на пай (PAYMENT) из того же графика /cost/."""
    _, items = _chart(_data(content, "/cost/"))
    out: dict[date, Payout] = {}
    for i, it in enumerate(items):
        require_keys(it, ["DATE", "PAYMENT"], f"/cost/ items[{i}]")
        amount = _positive(it["PAYMENT"], f"items[{i}].PAYMENT")
        if amount is None:
            continue
        d = day(it["DATE"])
        require(d not in out, f"/cost/: две выплаты на {d}")
        out[d] = Payout(d, amount, source_url)
    return [out[d] for d in sorted(out)]


def parse_returns(content, code: str, source_url: str) -> list[OfficialReturn]:
    """«Изменение стоимости пая за 1/3/6 мес. и 1 год» из /api/fund/<code>/profit/."""
    data = _data(content, "/profit/")
    require_keys(data, [code], "/profit/")
    block = data[code]
    require_keys(block, ["LAST_DATE", *PERIODS], f"/profit/ {code}")
    as_of = day(block["LAST_DATE"])
    out = []
    for key, period in PERIODS.items():
        require_keys(block[key], ["change_price"], f"/profit/ {code}.{key}")
        v = pct(block[key]["change_price"])     # '-' — периода ещё нет (молодой фонд)
        if v is not None:
            out.append(OfficialReturn(period, v, as_of, source_url))
    return out


def match_by_name(fund: Fund, items: list[dict]) -> dict | None:
    """Запасной путь для фондов без номера правил в карточке: точное совпадение названия."""
    names = {norm_text(fund.name), norm_text(fund.full_name)} - {""}
    found = [it for it in items if {norm_text(it["title"]), norm_text(it["full_name"])} & names]
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------- сбор

class AtonAdapter(Adapter):
    key = "aton"
    title = "Атон-менеджмент"
    uk_inns = ("7701253764",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        if not _trust_intermediate(ctx.http):
            ctx.log.warning("[aton] не удалось подключить промежуточный сертификат GlobalSign для %s", HOST)
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def index() -> dict:
            # Каталог и карточки — один раз на всю УК; ошибку каталога запоминаем и отдаём каждому фонду,
            # чтобы guarded() поставил ему статус с причиной (и не долбим упавший сайт).
            if "error" in state:
                raise state["error"]
            if "by_rules" not in state:
                try:
                    items = parse_list(ctx.http.get(LIST_URL, fmt="json", funds=all_rules).content())
                except Exception as e:  # noqa: BLE001 — статус определит guarded()
                    state["error"] = e
                    raise
                self._index(ctx, items, state)
            return state

        return [self.guarded(ctx, f, lambda f=f: self._one(ctx, f, index())) for f in funds]

    @staticmethod
    def _index(ctx: Context, items: list[dict], state: dict) -> None:
        """Номер правил -> фонд сайта по карточкам розничных фондов."""
        by_rules: dict[str, dict] = {}
        unnumbered: list[dict] = []
        failures: list[Exception] = []
        for it in items:
            try:
                rules = detail_rules(ctx.http.get(DETAIL_URL.format(code=it["code"]), fmt="json").content())
            except (FetchError, UnknownFormat) as e:
                ctx.log.warning("[aton] карточка %s: %s", it["code"], e)
                failures.append(e)
                continue
            if rules is None:
                unnumbered.append(it)
            elif rules in by_rules:
                ctx.log.warning("[aton] правила № %s у %s и %s", rules, by_rules[rules]["code"], it["code"])
            else:
                by_rules[rules] = it
        state.update(by_rules=by_rules, unnumbered=unnumbered, failures=failures)

    def _one(self, ctx: Context, fund: Fund, state: dict) -> FundData:
        item = state["by_rules"].get(norm_rules(fund.rules)) or match_by_name(fund, state["unnumbered"])
        if item is None:
            if state["failures"]:
                raise state["failures"][0]   # не все карточки прочитаны — «не найден» утверждать нельзя
            return FundData(fund.rules, page_url=LIST_PAGE, status="not_found",
                            reason=f"среди розничных фондов на сайте УК нет фонда с правилами № {fund.rules}")
        code = item["code"]
        page = PAGE_URL.format(code=code)
        raw = ctx.http.get(COST_URL.format(code=code), fmt="json", funds=[fund.rules])
        content = _doc(raw.content(), "/cost/")     # до ~1 МБ: разбираем JSON один раз
        points = parse_history(content)
        fd = FundData(fund.rules, currency=history_currency(content), page_url=page)
        if item["currency"] and item["currency"] != fd.currency:
            fd.warnings.append(f"в каталоге сайта валюта {item['currency']}, в графике фонда — {fd.currency}")
        try:
            prof = ctx.http.get(PROFIT_URL.format(code=code), fmt="json", funds=[fund.rules])
            fd.official_returns = parse_returns(prof.content(), code, page)
        except FetchError as e:
            fd.warnings.append(f"официальная доходность не получена: {e.reason}")

        if not points:
            fd.status, fd.reason = "no_history", "на странице фонда нет графика стоимости пая и СЧА"
            return fd
        seg = self.segment(raw, points, page_url=page, since=ctx.since)
        last = points[-1].date
        if not seg.points:
            fd.status = "partial"
            fd.reason = f"последнее значение на сайте УК — {last:%d.%m.%Y}, раньше начала периода"
            return fd
        fd.segments.append(seg)
        if all(p.nav is None for p in seg.points):
            fd.status, fd.reason = "partial", "УК публикует только цену пая, СЧА в ряду нет"
        if (date.today() - last).days > STALE_DAYS:
            fd.warnings.append(f"последнее значение на сайте УК — {last:%d.%m.%Y}")
        fd.payouts = [p for p in parse_payouts(content, page) if p.date >= ctx.since]
        if fd.payouts:
            fd.warnings.append("официальная доходность УК — с учётом выплат дохода пайщикам, сложенных "
                               "без реинвестирования: (пай в конце + выплаты) / пай в начале − 1")
        return fd
