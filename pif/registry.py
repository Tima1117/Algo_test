"""Реестр фондов: реестр ПИФ Банка России + реестр лицензий УК (ИНН, сайт) + ISIN/тикеры Мосбиржи."""
from __future__ import annotations

import io
import logging
import re
import warnings
from datetime import date

import openpyxl

warnings.filterwarnings("ignore", message="Workbook contains no default style")

from . import config
from .http import FetchError, Fetcher, RawDoc
from .model import FUND_TYPES, Fund
from .parsing import UnknownFormat, day

log = logging.getLogger("pif.registry")

ACTIVE_STATUSES = ("Сформирован", "Формируется")

FUND_COLUMNS = {
    "type": "Тип", "status": "Статус", "full_name": "Полное название", "short_name": "Краткое название",
    "category": "Категория", "rules": "Номер правил доверительного управления",
    "reg_date": "Дата присвоения номера", "formation_end": "Дата окончания формирования",
    "qual": "Инвестиционные паи предназначены для квалифицированного инвестора",
    "tradable": "Инвестиционные паи могут обращаться на организованных торгах",
    "uk_name": "Полное фирменное наименование", "uk_ogrn": "ОГРН",
}
UK_COLUMNS = {"full": "Полное фирменное наименование", "short": "Сокращенное фирменное наименование",
              "inn": "ИНН", "ogrn": "ОГРН", "site": "Адрес официального сайта"}


def _header_index(rows: list[tuple], wanted: dict[str, str], where: str, max_scan: int = 15) -> tuple[int, dict[str, int]]:
    for i, row in enumerate(rows[:max_scan]):
        cells = [str(c).strip() if c is not None else "" for c in row]
        idx: dict[str, int] = {}
        for key, title in wanted.items():
            # первое совпадение по началу заголовка (в реестре ПИФ «ОГРН» встречается трижды: УК, СД, регистратор)
            for j, c in enumerate(cells):
                if c.startswith(title):
                    idx[key] = j
                    break
        if len(idx) == len(wanted):
            return i, idx
    raise UnknownFormat(f"{where}: не нашёл строку заголовков с колонками {list(wanted.values())}")


def _xlsx_rows(content: bytes) -> list[tuple]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise UnknownFormat(f"не xlsx: {e}") from e
    return list(wb.worksheets[0].iter_rows(values_only=True))


def _iso(value) -> str | None:
    if value in (None, ""):
        return None
    return day(value).isoformat()


def short_title(name: str) -> str:
    """'ОПИФ рыночных финансовых инструментов «ВИМ - Индекс МосБиржи»' -> 'ВИМ - Индекс МосБиржи'."""
    m = re.search(r"[«\"“](.+)[»\"”]", name or "")
    return (m.group(1) if m else (name or "")).strip().strip("«»\"").strip()


def fund_title(short: str, full: str) -> str:
    for candidate in (short, full):
        if re.search(r"[«\"“]", candidate or ""):
            return short_title(candidate)
    return (short or full).strip()


def parse_uk_registry(content: bytes, min_rows: int = 50) -> dict[str, dict]:
    rows = _xlsx_rows(content)
    h, idx = _header_index(rows, UK_COLUMNS, "реестр лицензий УК")
    out: dict[str, dict] = {}
    for r in rows[h + 1:]:
        if not r or not r[idx["ogrn"]]:
            continue
        ogrn = str(r[idx["ogrn"]]).strip()
        site = str(r[idx["site"]] or "").split(",")[0].strip()
        if site and not site.startswith("http"):
            site = "https://" + site
        out[ogrn] = {"inn": str(r[idx["inn"]]).strip(), "full": str(r[idx["full"]]).strip(),
                     "short": str(r[idx["short"]] or r[idx["full"]]).strip(), "site": site}
    if len(out) < min_rows:
        raise UnknownFormat(f"реестр лицензий УК: подозрительно мало строк ({len(out)})")
    return out


def as_of_from_uk_registry(content: bytes) -> str | None:
    for r in _xlsx_rows(content)[:5]:
        for c in r:
            m = re.search(r"по состоянию на (\d{2}\.\d{2}\.\d{4})", str(c or ""))
            if m:
                return day(m.group(1)).isoformat()
    return None


def parse_fund_registry(content: bytes, uks: dict[str, dict], min_funds: int = 100) -> tuple[list[Fund], dict]:
    """Действующие розничные открытые, интервальные и биржевые фонды + счётчики того, что отброшено."""
    rows = _xlsx_rows(content)
    h, idx = _header_index(rows, FUND_COLUMNS, "реестр ПИФ")
    funds: list[Fund] = []
    skipped = {"qualified_only": 0, "other_type_or_status": 0, "no_uk_in_license_registry": 0}
    for r in rows[h + 1:]:
        if not r or not r[idx["rules"]]:
            continue
        type_ru = str(r[idx["type"]] or "").strip()
        status = str(r[idx["status"]] or "").strip()
        if type_ru not in FUND_TYPES or status not in ACTIVE_STATUSES:
            skipped["other_type_or_status"] += 1
            continue
        if str(r[idx["qual"]] or "").strip().lower() == "да":
            skipped["qualified_only"] += 1
            continue
        ogrn = str(r[idx["uk_ogrn"]] or "").strip()
        uk = uks.get(ogrn)
        if uk is None:
            skipped["no_uk_in_license_registry"] += 1
            log.warning("УК с ОГРН %s (%s) не найдена в реестре лицензий", ogrn, r[idx["uk_name"]])
            uk = {"inn": "", "full": str(r[idx["uk_name"]]), "short": str(r[idx["uk_name"]]), "site": ""}
        full_name = str(r[idx["full_name"]] or "").strip()
        funds.append(Fund(
            rules=str(r[idx["rules"]]).strip(),
            name=fund_title(str(r[idx["short_name"]] or ""), full_name),
            full_name=full_name,
            type=FUND_TYPES[type_ru],
            category=str(r[idx["category"]] or "").strip(),
            status=status,
            reg_date=_iso(r[idx["reg_date"]]) or "",
            formation_end=_iso(r[idx["formation_end"]]),
            uk_name=uk["full"], uk_short=uk["short"], uk_ogrn=ogrn, uk_inn=uk["inn"], uk_site=uk["site"],
            exchange_tradable=str(r[idx["tradable"]] or "").strip().lower() == "да",
        ))
    if len(funds) < min_funds:
        raise UnknownFormat(f"реестр ПИФ: подозрительно мало действующих фондов ({len(funds)})")
    funds.sort(key=lambda f: (f.uk_short, f.name))
    return funds, skipped


def parse_moex_page(doc: RawDoc) -> list[dict]:
    data = doc.json()
    try:
        cols = data["securities"]["columns"]
        rows = data["securities"]["data"]
    except (KeyError, TypeError) as e:
        raise UnknownFormat(f"ISS Мосбиржи: нет блока securities ({e})") from e
    for need in ("secid", "regnumber", "isin", "is_traded"):
        if need not in cols:
            raise UnknownFormat(f"ISS Мосбиржи: нет колонки {need}")
    return [dict(zip(cols, r)) for r in rows]


def fetch_moex(http: Fetcher) -> dict[str, dict]:
    """Номер правил -> {isin, ticker}. ISIN — у любой бумаги справочника (в том числе у паёв, которые
    сейчас не торгуются), тикер — только у торгуемых (is_traded=1)."""
    out: dict[str, dict] = {}
    start = 0
    while True:
        doc = http.get(config.MOEX_SECURITIES_URL, fmt="json", params={
            "iss.meta": "off", "engine": "stock", "market": "shares", "group_by": "group",
            "group_by_filter": "stock_ppif", "limit": 100, "start": start})
        page = parse_moex_page(doc)
        for s in page:
            reg = str(s.get("regnumber") or "").strip()
            if not reg or not s.get("isin"):
                continue
            entry = {"isin": s["isin"], "ticker": s.get("secid") if s.get("is_traded") else None}
            # у одного фонда может быть несколько бумаг — предпочитаем торгуемую
            if reg not in out or (entry["ticker"] and not out[reg]["ticker"]):
                out[reg] = entry
        if len(page) < 100:
            break
        start += 100
    return out


def previous_isins() -> dict[str, dict]:
    path = config.DATA_DIR / "registry.json"
    if not path.exists():
        return {}
    import json

    funds = json.loads(path.read_text("utf-8"))["funds"]
    return {f["rules"]: {"isin": f["isin"], "ticker": f["ticker"]} for f in funds if f.get("isin")}


def build_registry(http: Fetcher) -> tuple[list[Fund], dict]:
    uk_doc = http.get(config.CBR_UK_URL, fmt="xlsx")
    uks = parse_uk_registry(uk_doc.content())
    fund_doc = http.get(config.CBR_FUNDS_URL, fmt="xlsx")
    funds, skipped = parse_fund_registry(fund_doc.content(), uks)
    try:
        moex = fetch_moex(http)
    except FetchError as e:   # без ISIN реестр всё равно полезен — берём их из прошлого сбора
        log.warning("Мосбиржа недоступна (%s): ISIN и тикеры — из прошлого реестра", e.reason)
        moex = previous_isins()
    for f in funds:
        m = moex.get(f.rules)
        if m:
            f.isin, f.ticker = m["isin"], m["ticker"]
    meta = {
        "source": config.CBR_FUNDS_URL,
        "source_page": config.CBR_REGISTRY_PAGE,
        "uk_source": config.CBR_UK_URL,
        "isin_source": "https://iss.moex.com/iss/securities.json (группа stock_ppif)",
        "fetched_at": fund_doc.fetched_at,
        "fund_registry_sha256": fund_doc.sha256,
        "uk_registry_as_of": as_of_from_uk_registry(uk_doc.content()),
        "skipped": skipped,
        "with_isin": sum(1 for f in funds if f.isin),
        "collected_on": date.today().isoformat(),
    }
    log.info("Реестр: %d действующих розничных фондов, %d УК, ISIN у %d (отброшено: %s)",
             len(funds), len({f.uk_ogrn for f in funds}), meta["with_isin"], skipped)
    return funds, meta
