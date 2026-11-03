"""ОАО «РОНИН Траст» (ronintrust.ru) — один ОПИФ РФИ «РОНИН фонд сбалансированный» (правила 2003-94172428).

Источники (Drupal, обычный HTML):
* https://ronintrust.ru/opif — страница фонда: «Правила доверительного управления №2003-94172428»,
  СЧА и пай за две последние даты и ссылка «История стоимости пая и чистых активов»;
* https://ronintrust.ru/sites/default/files/trustopif.xls — эта история: Excel 97–2003 (BIFF8 в
  OLE2-контейнере), лист TDSheet: строка с названием фонда, шапка «Дата | Расчетная стоимость пая,
  руб. | Стоимость чистых активов, руб.», дальше каждый рабочий день с 16.05.2010, новые сверху.
  Дата — текстом «ДД.ММ.ГГГГ», числа — числами. Файл пересобирается УК ежедневно; диапазона дат нет.
  СЧА — в рублях (35 648 600,99 на 02.11.2026 — как на странице фонда), пай — в рублях.

Библиотеки для .xls (xlrd) в зависимостях проекта нет, openpyxl читает только .xlsx — поэтому ниже
свой минимальный читатель: OLE2 (FAT/мини-FAT не нужен — поток Workbook больше 4 КБ) и записи BIFF8
SST/LABELSST/LABEL/NUMBER/RK/MULRK первого листа. Всё незнакомое — UnknownFormat.

Официальная доходность: на сайте не публикуется — страница «Ключевой информационный документ»
(/node/128) пуста, других таблиц доходности нет. official_returns пустой + предупреждение.
Сообщений о выплате дохода пайщикам на сайте нет — payouts не заполняем.
"""
from __future__ import annotations

import re
import struct
from datetime import date

from bs4 import BeautifulSoup

from ..model import Fund, FundData, SeriesPoint
from ..parsing import UnknownFormat, day, norm_text, num, require
from .base import Adapter, Context

BASE = "https://ronintrust.ru"
PAGE_URL = f"{BASE}/opif"
XLS_URL = f"{BASE}/sites/default/files/trustopif.xls"
KID_URL = f"{BASE}/node/128"

_RULES = re.compile(r"№\s*(\d{4})\s*[-–—]\s*(\d{8})(?![-\d])")
NO_RETURNS = ("официальную доходность за 1/3/6/12 мес УК на сайте не публикует "
              f"(страница КИД {KID_URL} пуста) — не заполнена")


# ---------- минимальный читатель .xls (BIFF8) ----------

_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_END_OF_CHAIN, _FREE = 0xFFFFFFFE, 0xFFFFFFFF


def _ole_stream(data: bytes, names=("Workbook", "Book")) -> bytes:
    """Поток книги из OLE2-контейнера (только обычные сектора: поток книги всегда > 4 КБ)."""
    require(data[:8] == _OLE_MAGIC, "файл не OLE2 (не .xls): нет сигнатуры D0CF11E0")
    ssz = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    require(ssz in (512, 4096), f"странный размер сектора OLE2: {ssz}")
    n_fat, dir_start = struct.unpack_from("<I", data, 0x2C)[0], struct.unpack_from("<I", data, 0x30)[0]
    cutoff = struct.unpack_from("<I", data, 0x38)[0]
    difat_start, n_difat = struct.unpack_from("<I", data, 0x44)[0], struct.unpack_from("<I", data, 0x48)[0]

    def sector(i: int) -> bytes:
        off = (i + 1) * ssz
        require(off + ssz <= len(data), f"сектор OLE2 {i} за концом файла")
        return data[off:off + ssz]

    fat_secs = [s for s in struct.unpack_from("<109I", data, 0x4C) if s != _FREE][:n_fat]
    nxt, per = difat_start, ssz // 4 - 1
    for _ in range(n_difat):
        vals = struct.unpack(f"<{ssz // 4}I", sector(nxt))
        fat_secs += [s for s in vals[:per] if s != _FREE]
        nxt = vals[per]
    fat: list[int] = []
    for s in fat_secs[:n_fat]:
        fat += struct.unpack(f"<{ssz // 4}I", sector(s))

    def chain(start: int) -> bytes:
        out, i, guard = [], start, 0
        while i != _END_OF_CHAIN:
            require(i < len(fat) and guard <= len(fat), "битая цепочка секторов OLE2")
            out.append(sector(i))
            i, guard = fat[i], guard + 1
        return b"".join(out)

    dirs = chain(dir_start)
    for off in range(0, len(dirs), 128):
        e = dirs[off:off + 128]
        nlen = struct.unpack_from("<H", e, 64)[0]
        name = e[:max(nlen - 2, 0)].decode("utf-16-le", errors="replace")
        if e[66] == 2 and name in names:                       # 2 — поток
            start, size = struct.unpack_from("<II", e, 116)
            require(size >= cutoff, f"поток {name} в мини-потоке ({size} байт) — такие .xls не разбираем")
            return chain(start)[:size]
    raise UnknownFormat("в .xls нет потока Workbook")


class _Chunks:
    """Чтение SST, разрезанной на записи CONTINUE: на стыке массива символов — новый байт флагов."""

    def __init__(self, parts: list[bytes]):
        self.parts, self.i, self.pos = parts, 0, 0

    def _next(self) -> None:
        self.i, self.pos = self.i + 1, 0
        require(self.i < len(self.parts), "SST обрывается посреди строки")

    def take(self, n: int) -> bytes:
        out = b""
        while n:
            if self.pos >= len(self.parts[self.i]):
                self._next()
            piece = self.parts[self.i][self.pos:self.pos + n]
            out, self.pos, n = out + piece, self.pos + len(piece), n - len(piece)
        return out

    def chars(self, cch: int, high: bool) -> str:
        out = []
        while cch:
            if self.pos >= len(self.parts[self.i]):
                self._next()
                high = bool(self.take(1)[0] & 1)
            width = 2 if high else 1
            n = min(cch, (len(self.parts[self.i]) - self.pos) // width)
            require(n > 0, "SST: символ разрезан между записями")
            raw = self.parts[self.i][self.pos:self.pos + n * width]
            out.append(raw.decode("utf-16-le" if high else "latin-1"))
            self.pos, cch = self.pos + n * width, cch - n
        return "".join(out)


def _sst(parts: list[bytes]) -> list[str]:
    r = _Chunks(parts)
    _, unique = struct.unpack("<II", r.take(8))
    out = []
    for _ in range(unique):
        cch, flags = struct.unpack("<HB", r.take(3))
        runs = struct.unpack("<H", r.take(2))[0] if flags & 0x08 else 0
        ext = struct.unpack("<i", r.take(4))[0] if flags & 0x04 else 0
        out.append(r.chars(cch, bool(flags & 0x01)))
        r.take(4 * runs + ext)
    return out


def _rk(v: int) -> float:
    if v & 2:
        x = float(v >> 2)
    else:
        x = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return x / 100 if v & 1 else x


def xls_rows(content: bytes) -> list[list]:
    """Первый лист .xls -> строки значений (str | float | None) по порядку."""
    wb = _ole_stream(content)
    pos, sst_parts, in_sst, sheets_seen = 0, [], False, 0
    cells: dict[tuple[int, int], object] = {}
    sst: list[str] | None = None
    while pos + 4 <= len(wb):
        rt, ln = struct.unpack_from("<HH", wb, pos)
        d = wb[pos + 4:pos + 4 + ln]
        pos += 4 + ln
        if rt == 0x00FC:                                       # SST
            sst_parts, in_sst = [d], True
            continue
        if rt == 0x003C and in_sst:                            # CONTINUE для SST
            sst_parts.append(d)
            continue
        if in_sst:
            sst, in_sst = _sst(sst_parts), False
        if rt == 0x0809:                                       # BOF
            if struct.unpack_from("<H", d, 2)[0] == 0x0010:    # рабочий лист
                sheets_seen += 1
            continue
        if sheets_seen != 1:
            continue
        if rt == 0x000A:                                       # EOF первого листа
            break
        if rt == 0x00FD:                                       # LABELSST
            row, col, _, idx = struct.unpack_from("<HHHI", d)
            require(sst is not None and idx < len(sst), "ссылка на строку вне SST")
            cells[row, col] = sst[idx]
        elif rt == 0x0203:                                     # NUMBER
            row, col, _, val = struct.unpack_from("<HHHd", d)
            cells[row, col] = val
        elif rt == 0x027E:                                     # RK
            row, col, _, val = struct.unpack_from("<HHHi", d)
            cells[row, col] = _rk(val)
        elif rt == 0x00BD:                                     # MULRK
            row, first = struct.unpack_from("<HH", d)
            for k in range((ln - 6) // 6):
                cells[row, first + k] = _rk(struct.unpack_from("<i", d, 4 + 6 * k + 2)[0])
        elif rt == 0x0204:                                     # LABEL (старый вид строки)
            row, col, _, cch, flags = struct.unpack_from("<HHHHB", d)
            cells[row, col] = d[9:9 + cch * (2 if flags & 1 else 1)].decode("utf-16-le" if flags & 1 else "latin-1")
        elif rt in (0x0006, 0x0205):                           # FORMULA / BOOLERR — в этом файле не ждём
            raise UnknownFormat(f"в листе есть формула/логическое значение (запись 0x{rt:04X}) — разбор не умеет")
    require(sheets_seen >= 1, "в .xls нет ни одного рабочего листа")
    if not cells:
        return []
    nrows = max(r for r, _ in cells) + 1
    ncols = max(c for _, c in cells) + 1
    return [[cells.get((r, c)) for c in range(ncols)] for r in range(nrows)]


# ---------- разбор ----------

def parse_history(content: bytes) -> tuple[str, list[SeriesPoint]]:
    """trustopif.xls -> (название фонда из первой строки, точки по возрастанию даты)."""
    rows = [r for r in xls_rows(content) if any(v not in (None, "") for v in r)]
    require(len(rows) >= 2, "в .xls нет строк с данными")
    title = str(rows[0][0] or "")
    hi = next((i for i, r in enumerate(rows[:5]) if str(r[0] or "").strip() == "Дата"), None)
    require(hi is not None, f"нет строки-шапки «Дата | …» в начале листа: {rows[:3]}")
    head = [str(x or "").strip().lower() for x in rows[hi]]
    require(len(head) >= 3 and "стоимость пая" in head[1] and "чистых активов" in head[2],
            f"шапка не «Дата | Расчетная стоимость пая | Стоимость чистых активов»: {rows[hi]}")
    by_date: dict[date, SeriesPoint] = {}
    for r in rows[hi + 1:]:
        require(isinstance(r[0], str), f"дата не текстом: {r}")
        d = day(r[0])
        price, nav = num(r[1]), num(r[2])
        price = price if price and price > 0 else None
        nav = nav if nav and nav > 0 else None
        if price is None and nav is None:
            continue
        p = SeriesPoint(d, nav, price)
        require(by_date.get(d, p) == p, f"две разные строки на {d}")
        by_date[d] = p
    return title, [by_date[d] for d in sorted(by_date)]


def parse_page(content) -> tuple[str, str]:
    """Страница фонда -> (номер правил, ссылка на историю .xls)."""
    s = BeautifulSoup(content.decode("utf-8") if isinstance(content, bytes) else content, "lxml")
    text = re.sub(r"\s+", " ", s.get_text(" ", strip=True))
    m = _RULES.search(text)
    require(m is not None, "на странице фонда нет «Правила доверительного управления №XXXX-XXXXXXXX»")
    a = s.find("a", string=re.compile("История стоимости пая"), href=True)
    require(a is not None, "на странице фонда нет ссылки «История стоимости пая и чистых активов»")
    href = a["href"]
    return f"{m.group(1)}-{m.group(2)}", href if href.startswith("http") else BASE + href


# ---------- адаптер ----------

class RoninAdapter(Adapter):
    key = "ronin"
    title = "РОНИН Траст"
    uk_inns = ("7709379423",)

    def collect(self, ctx: Context, funds: list[Fund]) -> list[FundData]:
        all_rules = [f.rules for f in funds]
        state: dict = {}

        def page():
            if "page" not in state:
                state["page"] = parse_page(ctx.http.get(PAGE_URL, fmt="html", funds=all_rules).content())
            return state["page"]

        def one(fund: Fund) -> FundData:
            rules, xls_url = page()
            if rules != fund.rules:
                return FundData(fund.rules, status="not_found", page_url=PAGE_URL,
                                reason=f"на {PAGE_URL} фонд с правилами № {rules}, а не {fund.rules}")
            raw = ctx.http.get(xls_url, fmt="xls", funds=[fund.rules])
            title, points = parse_history(raw.content())
            require(norm_text(fund.name) in norm_text(title),
                    f"в файле истории фонд «{title}», а не «{fund.name}»")
            fd = FundData(fund.rules, currency="RUB", page_url=PAGE_URL,
                          segments=[self.segment(raw, points, page_url=PAGE_URL, since=ctx.since)])
            if not fd.segments[0].points:
                fd.status, fd.reason = "no_history", f"в {xls_url} нет точек с {ctx.since:%d.%m.%Y}"
            fd.warnings.append(NO_RETURNS)
            return fd

        return [self.guarded(ctx, f, lambda f=f: one(f)) for f in funds]
