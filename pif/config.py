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
    # Честно называем себя и цель. Ссылку на репозиторий убрали: ТРИНФИКО отвечает 403 на любой UA со ссылкой,
    # а подпись без неё остаётся такой же честной (и одинаковой для всех сайтов — не уловка под один сайт).
    "pif-registry/0.1 (test assignment for algosphere.tech; max 1 request/second)",
)

# robots.txt соблюдается для всех сайтов. Исключения — только официально документированные публичные API,
# которые и предназначены для программного доступа (их robots.txt закрывает сайт от поисковиков целиком).
ROBOTS_EXCEPTIONS = {
    "iss.moex.com": "ISS — официальный документированный программный интерфейс Московской биржи "
                    "(iss.moex.com/iss/reference); robots.txt закрывает его от поисковых роботов",
    "arsagera.ru": "УК публикует документированный публичный API без аутентификации "
                   "(в документации: «API является публичным»); robots.txt закрывает сайт целиком",
    # Ниже — сайты УК, где robots.txt закрывает от поисковиков и единственный источник обязательного
    # раскрытия (СЧА и стоимость пая по Указанию Банка России № 5609-У). Это публичные сведения, а не
    # закрытый раздел: берём их точечно, не чаще 1 запроса в секунду и с честным User-Agent.
    "www.upravlyaem.ru": "шаблонный robots.txt Битрикса закрывает /upload/ и /ajax/, где лежат выгрузки СЧА и пая",
    "www.ingosinvest-am.ru": "robots.txt закрывает адреса с параметрами (/*?*), а API истории СЧА принимает даты параметрами",
    "www.raiffeisen-capital.ru": "robots.txt закрывает сайт целиком (Disallow: /), включая таблицу результатов ПИФ",
    "my.raiffeisen-capital.ru": "robots.txt закрывает сайт целиком (Disallow: /), включая публичный график СЧА и пая",
    "web-api.kaplife.ru": "robots.txt закрывает API целиком; через него сайт УК показывает СЧА и пай",
    "ozon-am.ru": "robots.txt закрывает сайт целиком (Disallow: /), включая последние СЧА и пай",
    "www.rshb-am.ru": "шаблонный robots.txt Битрикса закрывает адреса с параметрами (/*?), а график СЧА и пая "
                      "на странице фонда загружается из /ajax/data.php?fund=…",
}

# Сайты, которые отвечают 403 на любые запросы с зарубежных IP (сборщик и GitHub Actions работают за рубежом),
# а из России открываются. Это не закрытый раздел и не антибот — честно так и пишем в статусе фонда.
FOREIGN_IP_BLOCKED = {"www.wealthim.ru", "wealthim.ru", "www.wimsavings.ru", "aaacapital.ru",
                      "www.aaacapital.ru", "www.akbars-capital.ru", "akbars-capital.ru"}

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
