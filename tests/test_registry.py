"""Реестр ЦБ и Мосбиржа: разбор настоящих (урезанных) выгрузок."""
import io
import json

import openpyxl
import pytest

from conftest import FIXTURES
from pif.http import RawDoc
from pif.parsing import UnknownFormat
from pif.registry import fund_title, parse_fund_registry, parse_moex_page, parse_uk_registry


def load(name):
    return (FIXTURES / "cbr" / name).read_bytes()


def test_uk_registry_has_inn_and_site():
    uks = parse_uk_registry(load("list_uk_sample.xlsx"), min_rows=1)
    first = uks["1027739007570"]  # УК «Первая»
    assert first["inn"] == "7710183778"
    assert first["site"].startswith("https://")


def test_fund_registry_filters_active_retail_types():
    uks = parse_uk_registry(load("list_uk_sample.xlsx"), min_rows=1)
    funds, skipped = parse_fund_registry(load("list_pif_sample.xlsx"), uks, min_funds=1)
    assert funds, "должны остаться действующие розничные фонды"
    assert {f.type for f in funds} <= {"open", "interval", "exchange"}
    assert all(f.status in ("Сформирован", "Формируется") for f in funds)
    assert skipped["qualified_only"] >= 1      # фонды для квалов отброшены
    assert skipped["other_type_or_status"] >= 1  # закрытые и исключённые отброшены
    assert all(f.uk_inn for f in funds)


def test_fund_registry_broken_header_is_loud():
    wb = openpyxl.load_workbook(io.BytesIO(load("list_pif_sample.xlsx")))
    ws = wb.active
    for row in ws.iter_rows(min_row=1, max_row=5):
        for c in row:
            if c.value == "Номер правил доверительного управления":
                c.value = "Номер"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(UnknownFormat, match="заголовков"):
        parse_fund_registry(buf.getvalue(), {}, min_funds=1)


def test_title_from_quotes():
    assert fund_title("ОПИФ рыночных финансовых инструментов «ВИМ - Индекс МосБиржи»", "") == "ВИМ - Индекс МосБиржи"
    assert fund_title("", "Открытый паевой инвестиционный фонд «Астра - Ликвидность»") == "Астра - Ликвидность"


def test_moex_page(tmp_path):
    p = FIXTURES / "moex" / "securities_ppif.json"
    rows = parse_moex_page(RawDoc("x", p, "", "", "json"))
    aiiq = next(r for r in rows if r["secid"] == "AIIQ")
    assert aiiq["regnumber"] == "7894" and aiiq["isin"] == "RU000A10FF36"
    bad = tmp_path / "bad.json"
    data = json.loads(p.read_text("utf-8"))
    data["securities"]["columns"].remove("regnumber")
    bad.write_text(json.dumps(data), "utf-8")
    with pytest.raises(UnknownFormat):
        parse_moex_page(RawDoc("x", bad, "", "", "json"))
