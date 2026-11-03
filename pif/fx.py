"""Официальные курсы Банка России для пересчёта валютных паёв в рубли."""
from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from datetime import date

from . import config
from .http import Fetcher
from .parsing import UnknownFormat, day, num

CBR_CODES = {"USD": "R01235", "EUR": "R01239", "CNY": "R01375"}


def parse_cbr_dynamic(content: bytes) -> list[tuple[str, float]]:
    """XML_dynamic.asp -> [(дата ISO, рублей за 1 единицу валюты)]."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise UnknownFormat(f"курсы ЦБ: не XML ({e})") from e
    if root.tag != "ValCurs":
        raise UnknownFormat(f"курсы ЦБ: корневой тег {root.tag}, ожидался ValCurs")
    out = []
    for rec in root.findall("Record"):
        nominal = num(rec.findtext("Nominal"))
        value = num(rec.findtext("Value"))
        if not nominal or not value:
            raise UnknownFormat("курсы ЦБ: запись без Nominal/Value")
        out.append((day(rec.get("Date")).isoformat(), value / nominal))
    return out


def fetch_rates(http: Fetcher, currencies: set[str], start: date, end: date) -> dict[str, list]:
    rates: dict[str, list] = {}
    for cur in sorted(currencies):
        if cur == "RUB":
            continue
        code = CBR_CODES.get(cur)
        if code is None:
            raise UnknownFormat(f"нет кода ЦБ для валюты {cur}")
        doc = http.get(config.CBR_RATES_URL, fmt="xml", params={
            "date_req1": start.strftime("%d/%m/%Y"), "date_req2": end.strftime("%d/%m/%Y"), "VAL_NM_RQ": code})
        rates[cur] = parse_cbr_dynamic(doc.content())
    return rates


class Rates:
    def __init__(self, data: dict[str, list]):
        self.data = {c: sorted(v) for c, v in data.items()}
        self.dates = {c: [d for d, _ in v] for c, v in self.data.items()}

    def on(self, currency: str, d: date | str) -> float | None:
        """Курс, действовавший на дату (последний установленный не позже неё)."""
        if currency == "RUB":
            return 1.0
        if currency not in self.data:
            return None
        ds = d.isoformat() if isinstance(d, date) else d
        i = bisect.bisect_right(self.dates[currency], ds) - 1
        return self.data[currency][i][1] if i >= 0 else None
