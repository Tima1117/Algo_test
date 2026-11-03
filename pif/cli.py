"""Одна команда на весь путь: python -m pif all

Шаги:
  registry  — реестр фондов (ЦБ + Мосбиржа)
  collect   — сбор рядов и официальной доходности с сайтов УК
  build     — нормализация, притоки, самопроверка, выгрузка данных для сайта
  all       — всё подряд
Флаги: --offline (только сохранённые копии), --only <ключ адаптера>, --strict (код 1, если
отчёт качества запрещает публикацию).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date
from pathlib import Path

from . import config
from .http import Fetcher, Manifest
from .model import Fund, FundData
from .sources import Context, load_adapters

log = logging.getLogger("pif")


def setup_logging() -> Path:
    config.LOG_DIR.mkdir(exist_ok=True)
    path = config.LOG_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path


def load_registry() -> list[Fund]:
    data = json.loads((config.DATA_DIR / "registry.json").read_text("utf-8"))
    return [Fund(**f) for f in data["funds"]]


def step_registry(http: Fetcher) -> None:
    from .registry import build_registry

    funds, meta = build_registry(http)
    out = {"meta": meta, "funds": [f.to_dict() for f in funds]}
    (config.DATA_DIR / "registry.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", "utf-8")


def _collect_one(adapter, ctx: Context, funds: list[Fund]) -> list[FundData]:
    t0 = time.monotonic()
    try:
        result = adapter.collect(ctx, funds)
    except Exception as e:  # noqa: BLE001 — сломанный сайт не роняет весь сбор
        log.exception("Адаптер %s упал целиком", adapter.key)
        result = [FundData(f.rules, status="parse_error", reason=f"сбой сборщика УК: {type(e).__name__}",
                           errors=[f"{adapter.key}: {type(e).__name__}: {e}"]) for f in funds]
    got = {fd.rules for fd in result}
    for f in funds:
        if f.rules not in got:
            result.append(FundData(f.rules, status="not_found", reason="фонд не найден на сайте УК"))
    ok = sum(1 for fd in result if fd.segments)
    log.info("[%s] %d/%d фондов с рядом за %.0f с", adapter.key, ok, len(funds), time.monotonic() - t0)
    return result


def merge_with_previous(new: dict, prev: dict | None) -> dict:
    """Докачка: новые сегменты добавляются к уже собранным, а не заменяют их.

    Сегмент с тем же адресом источника заменяется свежим. Если в этот раз сайт не отдал
    ничего, остаются прошлые точки, а статус показывает, что они не обновились.
    """
    if not prev or not prev.get("segments"):
        return new
    # выплаты дохода: адаптер отвечает за период, который скачал сейчас; из прошлого сбора остаются
    # только выплаты раньше этого периода (докачка хвоста их не видит)
    new_dates = [str(pt["date"])[:10] for seg in new["segments"] for pt in seg["points"]]
    since = min(new_dates) if new_dates else "9999"
    pays = {str(p["date"])[:10]: p for p in prev.get("payouts", []) if str(p["date"])[:10] < since}
    pays.update({str(p["date"])[:10]: p for p in new.get("payouts", [])})
    new["payouts"] = [pays[d] for d in sorted(pays)]
    def seg_id(seg: dict) -> str:
        return seg.get("source_key") or seg["raw_url"]

    # тот же источник скачан заново: из старой копии остаются только даты, которых в новой нет
    # (источники «последние N дней» так накапливают историю, полные выгрузки просто заменяются)
    fresh_dates: dict[str, set] = {}
    for seg in new["segments"]:
        fresh_dates.setdefault(seg_id(seg), set()).update(str(p["date"])[:10] for p in seg["points"])
    kept = []
    for seg in prev["segments"]:
        sid = seg_id(seg)
        if sid in fresh_dates:
            rest = [p for p in seg["points"] if str(p["date"])[:10] not in fresh_dates[sid]]
            if not rest:
                continue
            seg = {**seg, "points": rest}
        kept.append(seg)
    if new["segments"]:
        new["segments"] = kept + new["segments"]
        return new
    if new["status"] in ("unavailable", "blocked", "parse_error"):
        for s in kept:
            s["stale"] = True
        new["segments"] = kept
        new["reason"] = f"не обновилось: {new['reason']}; показаны данные прошлого сбора"
        new["status"] = "stale" if new["status"] != "parse_error" else "parse_error"
        if not new.get("official_returns"):
            new["official_returns"] = prev.get("official_returns", [])
        new["page_url"] = new.get("page_url") or prev.get("page_url")
        new["currency"] = prev.get("currency", new.get("currency"))
    return new


def probe_uncovered(http: Fetcher, funds: list[Fund]) -> list[FundData]:
    """УК без адаптера: один запрос к главной странице, чтобы честно отличить
    «сайт недоступен / закрыт» от «сбор ещё не реализован»."""
    from .http import Blocked, FetchError

    out = []
    by_site: dict[str, list[Fund]] = {}
    for f in funds:
        by_site.setdefault(f.uk_site, []).append(f)
    for site, group in sorted(by_site.items()):
        status, reason = "no_adapter", "сбор с сайта этой УК ещё не реализован"
        if not site:
            status, reason = "unavailable", "в реестре лицензий ЦБ не указан сайт УК"
        else:
            try:
                # одна попытка: здесь нужно только понять, отвечает ли сайт
                http.get(site, fmt="html", funds=[f.rules for f in group], allow_stale=False,
                         check_robots=False, attempts=1)
            except Blocked as e:
                status, reason = "blocked", f"{e.reason}; капчи и закрытые разделы не обходим"
            except FetchError as e:
                status, reason = "unavailable", f"сайт УК {site} из сети сборщика: {e.reason}"
        log.info("[без адаптера] %s: %s (%d фондов)", site, reason, len(group))
        out += [FundData(f.rules, status=status, reason=reason) for f in group]
    return out


def fetch_fx(http: Fetcher, results: list[FundData]) -> None:
    from .fx import fetch_rates

    currencies = {fd.currency for fd in results if fd.segments} - {"RUB"}
    path = config.DATA_DIR / "fx.json"
    if not currencies:
        if not path.exists():
            path.write_text("{}\n", "utf-8")
        return
    rates = fetch_rates(http, currencies, config.HISTORY_START, date.today())
    path.write_text(json.dumps(rates, ensure_ascii=False) + "\n", "utf-8")
    log.info("Курсы ЦБ: %s", {c: len(v) for c, v in rates.items()})


def step_collect(http: Fetcher, only: str | None = None) -> None:
    funds = load_registry()
    adapters = load_adapters()
    by_inn: dict[str, list[Fund]] = {}
    for f in funds:
        by_inn.setdefault(f.uk_inn, []).append(f)
    out_dir = config.DATA_DIR / "collected"
    previous = {p.stem: json.loads(p.read_text("utf-8")) for p in out_dir.glob("*.json")} if out_dir.exists() else {}
    last_dates = {}
    for rules, prev in previous.items():
        dates = [pt["date"] for seg in prev.get("segments", []) for pt in seg["points"]]
        if dates:
            last_dates[rules] = date.fromisoformat(max(dates))
    ctx = Context(http=http, since=config.HISTORY_START, log=logging.getLogger("pif.collect"),
                  last_dates=last_dates)
    jobs = []
    covered: set[str] = set()
    for a in adapters:
        if only and a.key != only:
            continue
        group = [f for inn in a.uk_inns for f in by_inn.get(inn, [])]
        covered.update(a.uk_inns)
        if group:
            jobs.append((a, group))
    results: list[FundData] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for res in pool.map(lambda job: _collect_one(job[0], ctx, job[1]), jobs):
            results.extend(res)
    if not only:
        results += probe_uncovered(http, [f for f in funds if f.uk_inn not in covered])
        fetch_fx(http, results)
    out_dir.mkdir(parents=True, exist_ok=True)
    for fd in results:
        record = merge_with_previous(asdict(fd), previous.get(fd.rules))
        (out_dir / f"{fd.rules}.json").write_text(
            json.dumps(record, ensure_ascii=False, default=str, indent=None) + "\n", "utf-8")
    s = http.stats
    log.info("Сбор: скачано %d, не изменилось %d, из кэша %d, ошибок сети %d",
             s.downloaded, s.unchanged, s.cached, s.errors)


def step_build(strict: bool = False) -> int:
    from .build import build_all

    ok = build_all()
    if not ok:
        log.error("Отчёт качества запрещает публикацию (есть ошибки сбора/разбора)")
        return 1 if strict else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pif", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["registry", "collect", "build", "all"])
    p.add_argument("--offline", action="store_true")
    p.add_argument("--only")
    p.add_argument("--strict", action="store_true")
    args = p.parse_args(argv)
    log_path = setup_logging()
    log.info("Старт: %s (лог: %s)", args.step, log_path.name)
    manifest = Manifest(config.DATA_DIR / "manifest.json")
    http = Fetcher(manifest, offline=args.offline)
    code = 0
    try:
        if args.step in ("registry", "all"):
            step_registry(http)
        if args.step in ("collect", "all"):
            step_collect(http, args.only)
    finally:
        manifest.save()
        http.close()
    if args.step in ("build", "all"):
        code = step_build(args.strict)
    log.info("Готово")
    return code
