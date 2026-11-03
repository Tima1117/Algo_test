"""Сборка данных: нормализация → месячные точки и притоки → доходность и сверка → отчёт качества → выгрузка.

Работает только с уже собранными данными (data/collected, data/fx.json), в сеть не ходит.
Результат детерминирован: повторная сборка на тех же данных даёт те же файлы.
"""
from __future__ import annotations

import csv
import json
import logging
import random
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

from . import config
from .flows import add_months, aggregate, month_key, month_range, monthly_rows, period_flow
from .fx import Rates
from .model import STATUS_LABELS, TYPE_LABELS, Fund
from .normalize import normalize
from .returns import PERIODS, compare_official, own_returns

log = logging.getLogger("pif.build")

SITE_DIR = config.DATA_DIR / "site"
SERIES_DIR = config.DATA_DIR / "series"
MANUAL_CHECK = config.DATA_DIR / "manual_check.csv"
MANUAL_FIELDS = ["n", "rules", "fund", "uk", "date", "field", "our_value", "page_url", "raw_url",
                 "value_on_site", "match", "checked_by", "checked_at", "comment"]


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=None, sort_keys=True,
                               separators=(",", ":")) + "\n", "utf-8")


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def load_inputs() -> tuple[list[Fund], dict, dict[str, dict], Rates]:
    reg = json.loads((config.DATA_DIR / "registry.json").read_text("utf-8"))
    funds = [Fund(**f) for f in reg["funds"]]
    collected = {}
    cdir = config.DATA_DIR / "collected"
    for f in funds:
        p = cdir / f"{f.rules}.json"
        if p.exists():
            collected[f.rules] = json.loads(p.read_text("utf-8"))
    fx_path = config.DATA_DIR / "fx.json"
    rates = Rates(json.loads(fx_path.read_text("utf-8")) if fx_path.exists() else {})
    return funds, reg["meta"], collected, rates


def manual_sample(funds_by: dict[str, Fund], series: dict[str, dict], k: int = 15) -> list[dict]:
    """15 случайных значений для ручной сверки. Файл создаётся один раз и потом не перезаписывается,
    чтобы не потерять уже заполненные руками колонки."""
    pool = []
    for rules, s in sorted(series.items()):
        for p in s["points"]:
            for fld, label in (("nav", "СЧА"), ("price", "Стоимость пая")):
                if p[fld] is not None:
                    pool.append((rules, p, fld, label))
    rnd = random.Random(20261103)
    rows = []
    for n, (rules, p, fld, label) in enumerate(rnd.sample(pool, min(k, len(pool))), 1):
        f, src = funds_by[rules], series[rules]["sources"][p["src"]]
        rows.append({"n": n, "rules": rules, "fund": f.name, "uk": f.uk_short, "date": p["date"],
                     "field": label, "our_value": p[fld], "page_url": src.get("page") or "",
                     "raw_url": src["url"], "value_on_site": "", "match": "", "checked_by": "",
                     "checked_at": "", "comment": ""})
    return rows


def build_all() -> bool:
    funds, reg_meta, collected, rates = load_inputs()
    funds_by = {f.rules: f for f in funds}
    errors, warnings, all_corrections, all_anomalies = [], [], [], []
    normalized = {}
    for f in funds:
        c = collected.get(f.rules)
        if not c:
            continue
        for e in c.get("errors", []):
            errors.append({"rules": f.rules, "uk": f.uk_short, "message": e})
        for w in c.get("warnings", []):
            warnings.append({"rules": f.rules, "uk": f.uk_short, "message": w})
        if c.get("status") in ("unavailable", "blocked", "stale"):
            warnings.append({"rules": f.rules, "uk": f.uk_short, "message": c.get("reason", c["status"])})
        if c.get("segments"):
            n = normalize(f.rules, c["segments"])
            if n.points:
                normalized[f.rules] = n
                all_corrections += n.corrections
                all_anomalies += n.anomalies

    as_of = max((n.points[-1].date for n in normalized.values()), default=date.today())
    last_month = add_months(month_key(as_of), -1)          # последний полный месяц
    months12 = month_range(add_months(last_month, -11), last_month)

    summaries, series_out, fund_rows, all_checks, discrepancies = [], {}, {}, [], []
    for f in funds:
        c = collected.get(f.rules) or {"status": "no_adapter", "reason": "сбор с сайта этой УК не реализован",
                                       "currency": "RUB", "official_returns": []}
        status, reason = c.get("status", "ok"), c.get("reason", "")
        n = normalized.get(f.rules)
        currency = c.get("currency") or "RUB"
        s = {**f.to_dict(), "type_label": TYPE_LABELS[f.type], "currency": currency,
             "page_url": c.get("page_url"), "last_date": None, "nav": None, "nav_rub": None, "price": None,
             "flow_m": None, "flow_q": None, "flow_y": None, "ret_as_of": None,
             **{f"ret_{k}": None for k in PERIODS}, "official": {}, "discrepancy": False,
             "n_points": 0, "first_date": None, "frequency": None, "corrections": 0, "anomalies": 0, "gaps": 0}
        if n is not None:
            pts = n.points
            rows = monthly_rows(pts, currency, rates, f.formation_end, last_month)
            fund_rows[f.rules] = rows
            last = next((p for p in reversed(pts) if p.nav is not None), None)
            lastp = next((p for p in reversed(pts) if p.price is not None), None)
            rate = rates.on(currency, last.date) if last else None
            payouts = c.get("payouts", [])
            ret_as_of, own = own_returns(pts, payouts)
            checks, bad = compare_official(f.rules, pts, c.get("official_returns", []), payouts)
            for ch in checks:
                ch.update(fund=f.name, uk=f.uk_short)
            all_checks += checks
            discrepancies += bad
            s.update(
                last_date=(last or lastp).date.isoformat(), nav=_r(last.nav) if last else None,
                nav_rub=_r(last.nav * rate) if last and rate else None, price=lastp.price if lastp else None,
                flow_m=_r(period_flow(rows, last_month, 1)), flow_q=_r(period_flow(rows, last_month, 3)),
                flow_y=_r(period_flow(rows, last_month, 12)), ret_as_of=ret_as_of,
                **{f"ret_{k}": _r(v) for k, v in own.items()},
                official={ch["period"]: {"value": ch["official"], "as_of": ch["as_of"], "ours": ch["ours"],
                                         "status": ch["status"], "url": ch["source_url"]} for ch in checks},
                discrepancy=bool(bad), n_points=len(pts), first_date=pts[0].date.isoformat(),
                frequency=n.frequency, corrections=len(n.corrections), anomalies=len(n.anomalies),
                gaps=len(n.gaps))
            if last is None:
                status, reason = "partial", (reason + "; " if reason else "") + "УК публикует только стоимость пая, без СЧА"
            elif lastp is None:
                status, reason = "partial", (reason + "; " if reason else "") + "УК публикует только СЧА, без стоимости пая"
            series_out[f.rules] = {
                "rules": f.rules, "currency": currency, "frequency": n.frequency,
                "points": [{"date": p.date.isoformat(), "nav": p.nav, "price": p.price,
                            "nav_rub": _r(p.nav * r) if p.nav is not None and (r := rates.on(currency, p.date)) else None,
                            "src": p.src} for p in pts],
                "sources": n.sources, "monthly": [{**r, "nav": _r(r["nav"]), "nav_rub": _r(r["nav_rub"]),
                                                   "flow": _r(r["flow"]), "flow_rub": _r(r["flow_rub"])} for r in rows],
                "corrections": n.corrections, "anomalies": n.anomalies, "gaps": n.gaps, "checks": checks,
                "payouts": payouts,
            }
        elif status == "ok":
            status, reason = "no_history", reason or "УК не публикует историю"
        s.update(data_status=status, data_status_label=STATUS_LABELS.get(status, status), data_reason=reason)
        summaries.append(s)

    # --- своды: УК, тип, категория, рынок ---
    groups = {
        "uk": {f.rules: f.uk_inn for f in funds},
        "type": {f.rules: f.type for f in funds},
        "category": {f.rules: f.category for f in funds},
        "market": {f.rules: "all" for f in funds},
    }
    agg = {name: aggregate(fund_rows, g, months12) for name, g in groups.items()}

    # проверка сходимости: сумма притоков по фондам = итог по УК = итог по типам = рынок
    consistency = []
    for m in months12:
        by_fund = sum(r["flow_rub"] for rows in fund_rows.values() for r in rows
                      if r["month"] == m and r["flow_rub"] is not None)
        totals = {name: sum(cells.get(m, {}).get("flow_rub", 0.0) for cells in a.values()) for name, a in agg.items()}
        ok = all(abs(v - by_fund) < 1.0 for v in totals.values())
        consistency.append({"month": m, "funds": _r(by_fund), **{k: _r(v) for k, v in totals.items()}, "ok": ok})
        if not ok:
            errors.append({"rules": None, "uk": None, "message": f"своды притоков не сходятся за {m}: {totals} vs {by_fund}"})

    uk_list = []
    for inn in sorted({f.uk_inn for f in funds}):
        fs = [s for s in summaries if s["uk_inn"] == inn]
        uk_list.append({
            "inn": inn, "ogrn": fs[0]["uk_ogrn"], "name": fs[0]["uk_name"], "short": fs[0]["uk_short"],
            "site": fs[0]["uk_site"], "funds": len(fs), "funds_with_data": sum(1 for s in fs if s["n_points"]),
            "nav_rub": _r(sum(s["nav_rub"] or 0 for s in fs)),
            "flow_m": _r(sum(s["flow_m"] or 0 for s in fs)), "flow_q": _r(sum(s["flow_q"] or 0 for s in fs)),
            "flow_y": _r(sum(s["flow_y"] or 0 for s in fs)),
            "monthly": [{"month": m, **{k: _r(v) if isinstance(v, float) else v
                                        for k, v in agg["uk"].get(inn, {}).get(m, {}).items()}} for m in months12],
            "statuses": dict(Counter(s["data_status"] for s in fs)),
        })

    def agg_table(name: str) -> list[dict]:
        out = []
        for key, cells in sorted(agg[name].items()):
            out.append({"key": key, "monthly": [{"month": m, **{k: _r(v) if isinstance(v, float) else v
                                                               for k, v in cells.get(m, {}).items()}}
                                                for m in months12]})
        return out

    status_counts = Counter(s["data_status"] for s in summaries)
    publish_allowed = not errors
    quality = {
        "as_of": as_of.isoformat(), "last_month": last_month, "months": months12,
        "registry_fetched_at": reg_meta.get("fetched_at"),
        "totals": {"funds": len(funds), "with_series": len(normalized), "by_status": dict(status_counts),
                   "nav_rub_covered": _r(sum(s["nav_rub"] or 0 for s in summaries))},
        "errors": errors, "warnings": warnings,
        "checks": {"total": len(all_checks), "ok": sum(1 for c in all_checks if c["status"] == "сходится"),
                   "discrepancies": len(discrepancies)},
        "discrepancies": discrepancies, "anomalies": all_anomalies,
        "corrections_count": len(all_corrections), "consistency": consistency,
        "publish_allowed": publish_allowed,
    }

    # --- ручная сверка ---
    if not MANUAL_CHECK.exists() and series_out:
        with open(MANUAL_CHECK, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=MANUAL_FIELDS)
            w.writeheader()
            w.writerows(manual_sample(funds_by, series_out))
    manual = list(csv.DictReader(open(MANUAL_CHECK, encoding="utf-8"))) if MANUAL_CHECK.exists() else []

    # --- запись ---
    if SERIES_DIR.exists():
        for old in SERIES_DIR.glob("*.json"):
            if old.stem not in series_out:
                old.unlink()
    for rules, s in series_out.items():
        _dump(SERIES_DIR / f"{rules}.json", s)
    meta = {"registry": reg_meta, "as_of": as_of.isoformat(), "last_month": last_month, "months": months12,
            "history_start": config.HISTORY_START.isoformat(), "status_labels": STATUS_LABELS,
            "type_labels": TYPE_LABELS, "funds": len(funds), "with_series": len(normalized),
            "publish_allowed": publish_allowed}
    _dump(SITE_DIR / "meta.json", meta)
    _dump(SITE_DIR / "funds.json", summaries)
    _dump(SITE_DIR / "uk.json", uk_list)
    _dump(SITE_DIR / "market.json", {"type": agg_table("type"), "category": agg_table("category"),
                                     "market": agg_table("market")})
    _dump(SITE_DIR / "quality.json", {**quality, "all_checks": all_checks})
    _dump(SITE_DIR / "corrections.json", all_corrections)
    _dump(SITE_DIR / "manual_check.json", manual)
    write_quality_md(quality, all_corrections)
    log.info("Сборка: %d фондов с рядом из %d; статусы: %s; сверок доходности %d, расхождений %d; "
             "исправлений рядов %d; ошибок %d → публикация %s",
             len(normalized), len(funds), dict(status_counts), len(all_checks), len(discrepancies),
             len(all_corrections), len(errors), "разрешена" if publish_allowed else "ЗАПРЕЩЕНА")
    return publish_allowed


def write_quality_md(q: dict, corrections: list[dict]) -> None:
    L = [f"# Отчёт качества — данные на {q['as_of']}", "",
         f"Публикация: **{'разрешена' if q['publish_allowed'] else 'запрещена'}** "
         f"(ошибок сбора и разбора: {len(q['errors'])}).", "",
         f"Фондов в реестре: {q['totals']['funds']}, с рядом: {q['totals']['with_series']}.", "",
         "| Статус | Фондов |", "|---|---|"]
    L += [f"| {STATUS_LABELS.get(k, k)} | {v} |" for k, v in sorted(q["totals"]["by_status"].items(), key=lambda x: -x[1])]
    L += ["", f"## Сверка доходности с официальной (допуск {0.5} п. п.)", "",
          f"Сравнений: {q['checks']['total']}, сошлось: {q['checks']['ok']}, расхождений: {q['checks']['discrepancies']}.", ""]
    if q["discrepancies"]:
        L += ["| Фонд | УК | Период | На дату | Наша, % | Официальная, % | Разница, п. п. | Источник |", "|---|---|---|---|---|---|---|---|"]
        L += [f"| {d.get('fund', d['rules'])} ({d['rules']}) | {d.get('uk', '')} | {d['period']} | {d['as_of']} | {d['ours']} | "
              f"{d['official']} | {d['diff_pp']} | {d['source_url']} |" for d in q["discrepancies"]]
    L += ["", "## Ошибки сбора и разбора (блокируют публикацию)", ""]
    L += [f"- {e['uk'] or ''} {e['rules'] or ''}: {e['message']}" for e in q["errors"]] or ["Нет."]
    L += ["", "## Предупреждения (сайт недоступен, данные не обновились)", ""]
    L += [f"- {w['uk']} {w['rules']}: {w['message']}" for w in q["warnings"][:200]] or ["Нет."]
    L += ["", f"## Исправления рядов: {len(corrections)} (полный журнал — data/site/corrections.json)", ""]
    L += [f"- {c['rules']} {c['date']} {c['field']}: {c['action']} — {c['reason']}" for c in corrections[:100]]
    L += ["", f"## Аномалии без исправления: {len(q['anomalies'])}", ""]
    L += [f"- {a['rules']} {a['date']}: цена {a['change_pct']}%, СЧА {a['nav_change_pct']}%" for a in q["anomalies"][:100]]
    L += ["", "## Сходимость сводов притоков", "", "| Месяц | Фонды | УК | Типы | Категории | Рынок | ОК |", "|---|---|---|---|---|---|---|"]
    L += [f"| {c['month']} | {c['funds']} | {c['uk']} | {c['type']} | {c['category']} | {c['market']} | {'да' if c['ok'] else 'НЕТ'} |"
          for c in q["consistency"]]
    (config.DATA_DIR / "quality_report.md").write_text("\n".join(L) + "\n", "utf-8")
