"""Общие помощники разбора: числа, даты, проверка формата."""
from __future__ import annotations

import re
from datetime import date, datetime


class UnknownFormat(Exception):
    """Источник пришёл не в том виде, который умеет разбирать разборщик.

    Это всегда ошибка разбора: тихо вернуть пустой ряд или ноль нельзя.
    """


_SPACES = re.compile(r"[\s  ]")


def num(value) -> float | None:
    """'1 234 567,89' / '1,234,567.89' / 1234.5 / '' -> float | None. Мусор -> UnknownFormat."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise UnknownFormat(f"ожидалось число, получено {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    s = _SPACES.sub("", str(value)).replace("₽", "").replace("руб.", "").replace("руб", "")
    if s in ("", "-", "—", "–", "н/д", "null", "None"):
        return None
    if "," in s and "." in s:
        # последний разделитель — десятичный
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError as e:
        raise UnknownFormat(f"не число: {value!r}") from e


def pct(value) -> float | None:
    if isinstance(value, str):
        value = value.replace("%", "")
    return num(value)


_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d.%m.%y", "%Y%m%d")


def day(value) -> date:
    """Дата из строки/datetime/epoch-ms. Непонятное -> UnknownFormat."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = value / 1000 if value > 10**11 else value
        return datetime.utcfromtimestamp(ts).date()
    s = str(value).strip()
    s = re.sub(r"(T|\s)\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", "", s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise UnknownFormat(f"не дата: {value!r}")


def require(cond: bool, message: str) -> None:
    if not cond:
        raise UnknownFormat(message)


def require_keys(obj, keys, where: str) -> None:
    if not isinstance(obj, dict):
        raise UnknownFormat(f"{where}: ожидался объект, получено {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise UnknownFormat(f"{where}: нет полей {missing}; есть {sorted(obj)[:15]}")


def norm_text(s: str) -> str:
    """Для сопоставления названий фондов: нижний регистр, без кавычек, тире и типовых слов."""
    s = str(s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„()\[\].,:;!?–—\-/\\]", " ", s)
    stop = (r"\b(открытый|интервальный|биржевой|паевой|инвестиционный|фонд|опиф|бпиф|ипиф|пиф|"
            r"рыночных|финансовых|инструментов|под управлением|ук|ооо|ао)\b")
    s = re.sub(stop, " ", s)
    return re.sub(r"\s+", " ", s).strip()
