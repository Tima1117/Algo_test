"""Пути и общие настройки сборщика."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Сырые ответы источников: в git не попадают, их восстанавливает повторный сбор по манифесту.
RAW_DIR = Path(os.environ.get("PIF_RAW_DIR", ROOT / "raw"))
# Обработанные данные: манифест, реестр, ряды, отчёт качества. Лежат в git.
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

USER_AGENT = os.environ.get(
    "PIF_USER_AGENT",
    "pif-registry/0.1 (+https://github.com/Tima1117/Algo_test; "
    "test assignment for algosphere.tech; max 1 request/second)",
)

# Не чаще одного запроса в секунду к одному домену.
MIN_INTERVAL_SEC = float(os.environ.get("PIF_MIN_INTERVAL", "1.05"))
TIMEOUT_SEC = float(os.environ.get("PIF_TIMEOUT", "30"))

# Сколько часов ответ считается свежим: повторный запуск в этот срок не ходит в сеть.
FRESH_HOURS = float(os.environ.get("PIF_FRESH_HOURS", "20"))

# Начало истории: 12 полных месяцев + запас, чтобы посчитать приток первого месяца
# и доходность за год.
HISTORY_START = date.fromisoformat(os.environ.get("PIF_HISTORY_START", "2025-09-01"))

CBR_FUNDS_URL = "https://www.cbr.ru/vfs/finmarkets/files/supervision/list_pif.xlsx"
CBR_UK_URL = "https://www.cbr.ru/vfs/finmarkets/files/supervision/list_managementcompanies.xlsx"
CBR_REGISTRY_PAGE = "https://www.cbr.ru/registries/RSCI/"
MOEX_SECURITIES_URL = "https://iss.moex.com/iss/securities.json"
CBR_RATES_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
