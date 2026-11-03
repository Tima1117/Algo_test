"""Общие структуры данных."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

FUND_TYPES = {"Открытый": "open", "Интервальный": "interval", "Биржевой": "exchange"}
TYPE_LABELS = {"open": "Открытый", "interval": "Интервальный", "exchange": "Биржевой"}

# Статусы данных фонда. Код -> подпись для сайта.
STATUS_LABELS = {
    "ok": "Ряд собран",
    "partial": "Ряд собран частично",
    "stale": "Сайт УК не ответил, показана последняя сохранённая копия",
    "no_history": "УК не публикует историю СЧА и пая в открытом доступе",
    "unavailable": "Сайт УК недоступен",
    "blocked": "Сайт УК закрыл доступ (антибот / 403), не обходим",
    "not_found": "Фонд не найден на сайте УК",
    "not_formed": "Фонд ещё формируется, СЧА не рассчитывается",
    "parse_error": "Ошибка разбора данных УК",
    "no_adapter": "Сбор с сайта этой УК не реализован",
}


@dataclass
class Fund:
    rules: str            # номер правил доверительного управления — ключ фонда
    name: str             # краткое название из реестра ЦБ
    full_name: str
    type: str             # open / interval / exchange
    category: str
    status: str           # «Сформирован» / «Формируется»
    reg_date: str         # дата присвоения номера правил (ISO)
    formation_end: str | None
    uk_name: str
    uk_short: str
    uk_ogrn: str
    uk_inn: str
    uk_site: str
    exchange_tradable: bool
    isin: str | None = None
    ticker: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SeriesPoint:
    date: date
    nav: float | None     # СЧА в валюте пая
    price: float | None   # расчётная стоимость пая в валюте пая


@dataclass
class Segment:
    """Кусок ряда из одного сырого документа: у каждой точки есть источник."""
    raw_url: str          # что именно скачали (файл / API)
    sha256: str
    fetched_at: str
    points: list[SeriesPoint]
    page_url: str | None = None   # страница фонда на сайте УК, где это видно человеку
    stale: bool = False
    source_key: str = ""          # ключ манифеста: различает POST-запросы с разным телом на один адрес


@dataclass
class OfficialReturn:
    period: str           # 1m / 3m / 6m / 1y
    value_pct: float
    as_of: date | None
    source_url: str


@dataclass
class Payout:
    """Выплата дохода пайщикам: сумма на один пай в валюте пая на дату, на которую определяется доход."""
    date: date
    amount: float
    source_url: str


@dataclass
class FundData:
    rules: str
    currency: str = "RUB"
    segments: list[Segment] = field(default_factory=list)
    official_returns: list[OfficialReturn] = field(default_factory=list)
    payouts: list[Payout] = field(default_factory=list)   # для доходности с учётом выплат
    page_url: str | None = None
    status: str = "ok"
    reason: str = ""
    errors: list[str] = field(default_factory=list)   # ошибки сбора/разбора — блокируют публикацию
    warnings: list[str] = field(default_factory=list)
