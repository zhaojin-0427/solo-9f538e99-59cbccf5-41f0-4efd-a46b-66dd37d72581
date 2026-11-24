"""Core domain, SQLite persistence, subtotal keys, and rank queries.

This module is framework-neutral so the important guarantees can be tested
without Flask.
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, Iterator, Optional


WEIGHT_MIN = 0
WEIGHT_MAX = 65535
WEIGHT_SIZE = 65536  # 2 ** 16
SUBTOTAL_KEY_LIMIT = 576

# A whole-day zero offset may sit in [-65535, 65535]; 0 means "cancel".
CORRECTION_MIN = -WEIGHT_MAX
CORRECTION_MAX = WEIGHT_MAX

SCOPE_ALL = "all"
SCOPE_ACTIVE = "active"


class ValidationError(ValueError):
    """Invalid user input. Messages are safe to show on the review page."""

    def __init__(self, message: str, errors: Optional[Iterable[str]] = None):
        super().__init__(message)
        self.message = message
        self.errors = list(errors or [])


# ---------------------------------------------------------------------------
# Date and weight canonical segments
# ---------------------------------------------------------------------------


def days_in_year(year: int) -> int:
    return 366 if date(year, 12, 31).timetuple().tm_yday == 366 else 365


def parse_iso_date(value: str, field_name: str = "日期") -> date:
    if value is None:
        raise ValidationError(f"{field_name}不能为空")
    text = str(value).strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValidationError(f"{field_name}必须是 YYYY-MM-DD 格式")
    try:
        year = int(text[0:4])
        month = int(text[5:7])
        day = int(text[8:10])
        return date(year, month, day)
    except ValueError as exc:
        raise ValidationError(f"{field_name}不是有效日期：{text}") from exc


@dataclass(frozen=True)
class DateSeg:
    year: int
    lo: int  # zero-based day-of-year offset, inclusive
    hi: int  # zero-based offset, exclusive

    @property
    def start(self) -> date:
        return date(self.year, 1, 1) + timedelta(days=self.lo)

    @property
    def end_inclusive(self) -> date:
        return date(self.year, 1, 1) + timedelta(days=self.hi - 1)

    @property
    def key(self) -> str:
        return f"date:{self.start.isoformat()}..{self.end_inclusive.isoformat()}"

    @property
    def label(self) -> str:
        if self.lo + 1 == self.hi:
            return self.start.isoformat()
        return f"{self.start.isoformat()} ~ {self.end_inclusive.isoformat()}"


def _date_seg(year: int, lo: int, hi: int) -> DateSeg:
    return DateSeg(year=year, lo=lo, hi=hi)


def date_ancestors(day: date) -> list[DateSeg]:
    """Balanced-tree segments from the full year down to one day."""
    year = day.year
    n = days_in_year(year)
    idx = (day - date(year, 1, 1)).days
    lo, hi = 0, n
    result: list[DateSeg] = []
    while True:
        result.append(_date_seg(year, lo, hi))
        if hi - lo == 1:
            return result
        mid = (lo + hi) // 2
        if idx < mid:
            hi = mid
        else:
            lo = mid


def date_cover(start: date, end: date) -> list[DateSeg]:
    """Canonical balanced-tree segments covering [start, end] inclusively."""
    if start.year != end.year:
        raise ValidationError("查询起止日必须在同一年")
    if start > end:
        raise ValidationError("起始日不能晚于结束日")

    year = start.year
    n = days_in_year(year)
    lo = (start - date(year, 1, 1)).days
    hi = (end - date(year, 1, 1)).days + 1
    if not (0 <= lo < hi <= n):
        raise ValidationError("日期超出该年范围")

    found: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = [(0, n)]
    while stack:
        a, b = stack.pop()
        if hi <= a or b <= lo:
            continue
        if lo <= a and b <= hi:
            found.append((a, b))
            continue
        mid = (a + b) // 2
        stack.append((a, mid))
        stack.append((mid, b))

    found.sort()
    return [_date_seg(year, a, b) for a, b in found]


@dataclass(frozen=True)
class WeightSeg:
    lo: int  # inclusive
    hi: int  # exclusive

    @property
    def key(self) -> str:
        return f"weight:{self.lo:05d}..{self.hi - 1:05d}"

    @property
    def label(self) -> str:
        if self.hi - self.lo == 1:
            return f"{self.lo} kg"
        return f"{self.lo}–{self.hi - 1} kg"


def weight_ancestors(weight: int) -> list[WeightSeg]:
    """Dyadic segments from the exact weight leaf up to the 0..65535 root."""
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValidationError("净重必须在 0～65535 之间")

    lo = weight
    size = 1
    result = [WeightSeg(lo, lo + size)]
    while size < WEIGHT_SIZE:
        size *= 2
        lo = (lo // size) * size
        result.append(WeightSeg(lo, lo + size))
    return result


def weight_cover(lo: int, hi: int) -> list[WeightSeg]:
    """Minimal aligned dyadic cover of the half-open range [lo, hi)."""
    if lo >= hi:
        return []
    if not (0 <= lo < hi <= WEIGHT_SIZE):
        raise ValidationError("重量区间超出 0～65535")

    result: list[WeightSeg] = []
    pos = lo
    while pos < hi:
        size = 1
        while size * 2 <= hi - pos and pos % (size * 2) == 0:
            size *= 2
        result.append(WeightSeg(pos, pos + size))
        pos += size
    return result


def weight_greater_cover(weight: int) -> list[WeightSeg]:
    return weight_cover(weight + 1, WEIGHT_SIZE)


# ---------------------------------------------------------------------------
# SQLite schema and transaction helpers
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS tickets (
    slip_date   TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    net_weight  INTEGER NOT NULL CHECK (net_weight BETWEEN 0 AND 65535),
    PRIMARY KEY (slip_date, ticket_no)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS holds (
    slip_date   TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slip_date, ticket_no)
) WITHOUT ROWID;

-- One row = one date segment × one weight band × one caliber.
CREATE TABLE IF NOT EXISTS subtotals (
    date_key     TEXT NOT NULL,
    weight_key   TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('all', 'active')),
    ticket_count INTEGER NOT NULL CHECK (ticket_count >= 0),
    PRIMARY KEY (date_key, weight_key, scope)
) WITHOUT ROWID;

-- At most one correction day per calendar year. Rows with offset 0 are
-- deleted (cancellation), so only active corrections are stored.
CREATE TABLE IF NOT EXISTS corrections (
    slip_date  TEXT PRIMARY KEY,
    offset_kg  INTEGER NOT NULL CHECK (offset_kg BETWEEN -65535 AND 65535)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_corrections_year
    ON corrections(substr(slip_date, 1, 4));

CREATE INDEX IF NOT EXISTS idx_holds_date ON holds(slip_date);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def init_db_path(db_path: str) -> None:
    if db_path != ":memory:":
        import os

        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

TICKET_HEADERS = {
    "单号",
    "秤单号",
    "提单号",
    "票号",
    "车单号",
    "ticket",
    "ticketno",
    "ticket_no",
    "id",
    "no",
}
WEIGHT_HEADERS = {
    "净重",
    "净重kg",
    "净重(kg)",
    "净重（kg）",
    "重量",
    "weight",
    "netweight",
    "net_weight",
    "kg",
}


def _normalise_header(value: object) -> str:
    return "".join(str(value or "").strip().lower().replace(" ", "").split())


def parse_weight(value: object, row_number: int) -> int:
    text = str(value if value is not None else "").strip()
    compact = text.replace(",", "").replace("_", "")
    if not compact or not (compact[0] in "+-" and compact[1:].isdigit() or compact.isdigit()):
        raise ValidationError(f"第 {row_number} 行净重不是整数：{text!r}")
    weight = int(compact)
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValidationError(
            f"第 {row_number} 行净重 {weight} 超出范围（0～65535 千克）"
        )
    return weight


def parse_csv_text(text: str) -> list[tuple[str, int]]:
    if text is None or not text.strip():
        raise ValidationError("CSV 为空，请选择包含“单号,净重”数据的文件")

    # Support common Chinese Excel exports which use GB18030.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        reader = csv.reader(io.StringIO(text), dialect)
    except csv.Error:
        reader = csv.reader(io.StringIO(text))

    raw_rows = list(reader)
    rows = [row for row in raw_rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise ValidationError("CSV 没有有效数据行")

    first = [_normalise_header(cell) for cell in rows[0]]
    ticket_idx = next((i for i, h in enumerate(first) if h in TICKET_HEADERS), None)
    weight_idx = next((i for i, h in enumerate(first) if h in WEIGHT_HEADERS), None)

    if ticket_idx is not None and weight_idx is not None:
        data_rows = rows[1:]
        start_line = 2
    else:
        # Headerless format is explicitly supported: first two columns are
        # ticket number then net weight.
        ticket_idx, weight_idx = 0, 1
        data_rows = rows
        start_line = 1

    tickets: list[tuple[str, int]] = []
    seen: set[str] = set()
    errors: list[str] = []

    for offset, row in enumerate(data_rows, start=start_line):
        line_no = offset
        if len(row) <= max(ticket_idx, weight_idx):
            errors.append(f"第 {line_no} 行缺少单号或净重：{row!r}")
            continue

        ticket_no = str(row[ticket_idx]).strip()
        weight_text = row[weight_idx]
        if not ticket_no:
            errors.append(f"第 {line_no} 行单号为空")
            continue
        try:
            weight = parse_weight(weight_text, line_no)
        except ValidationError as exc:
            errors.append(exc.message)
            continue

        if ticket_no in seen:
            errors.append(f"第 {line_no} 行单号在同一 CSV 内重复：{ticket_no}")
            continue
        seen.add(ticket_no)
        tickets.append((ticket_no, weight))

    if errors:
        raise ValidationError("CSV 校验未通过，未写入任何数据", errors=errors)
    if not tickets:
        raise ValidationError("CSV 没有可导入的数据行")
    return tickets


def decode_csv_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Zero-point corrections
#
# tickets.net_weight always keeps the *original* reading. The subtotals table
# always stores the *effective* weight (original plus the day's registered
# offset), so rank queries need no special casing. A correction registration
# therefore only shifts that day's exact-weight leaf counts between weight
# keys, and every guard is answered from subtotals alone -- the tickets table
# is never read during registration.
# ---------------------------------------------------------------------------


def parse_correction_offset(value: object) -> int:
    """Parse the -65535..65535 integer offset; 0 means cancellation."""
    if value is None:
        raise ValidationError("纠偏量不能为空")
    text = str(value).strip().replace(",", "").replace("_", "")
    if not text:
        raise ValidationError("纠偏量不能为空")
    if not (
        (text[0] in "+-" and text[1:].isdigit()) or text.isdigit()
    ):
        raise ValidationError(f"纠偏量必须是整数：{text!r}")
    offset = int(text)
    if not CORRECTION_MIN <= offset <= CORRECTION_MAX:
        raise ValidationError(
            f"纠偏量 {offset} 超出范围（-65535～65535 千克）"
        )
    return offset


def day_date_key(day: date) -> str:
    """Subtotal date key of the single-day leaf segment for ``day``."""
    seg = next(s for s in date_ancestors(day) if s.hi - s.lo == 1)
    return seg.key


def _parse_weight_leaf_key(weight_key: str) -> Optional[int]:
    """Return the exact weight of a weight leaf key, else None for wider bands."""
    if not weight_key.startswith("weight:"):
        return None
    body = weight_key[len("weight:") :]
    lo_text, hi_text = body.split("..", 1)
    lo = int(lo_text)
    hi = int(hi_text)
    if hi != lo:  # leaf keys look like weight:00123..00123
        return None
    return lo


def read_single_day_leaf_counts(
    conn: sqlite3.Connection, day_text: str
) -> dict[tuple[int, str], int]:
    """Read leaf counts of the single-day date segment only (no ancestors)."""
    day = parse_iso_date(day_text, "日期")
    leaf_key = day_date_key(day)
    rows = conn.execute(
        """
        SELECT weight_key, scope, ticket_count
        FROM subtotals
        WHERE date_key = ?
        """,
        (leaf_key,),
    ).fetchall()
    leaves: dict[tuple[int, str], int] = {}
    for row in rows:
        weight = _parse_weight_leaf_key(row["weight_key"])
        if weight is None:
            continue
        leaves[(weight, row["scope"])] = int(row["ticket_count"])
    return leaves


def _check_leaf_scope_consistency(
    leaves: dict[tuple[int, str], int]
) -> None:
    """Cross-check the two calibers using only subtotal counts."""
    for (weight, scope), count in leaves.items():
        if count < 0:
            raise sqlite3.IntegrityError(f"小计票数为负：重量 {weight} / {scope}")
        if scope == SCOPE_ACTIVE:
            total = leaves.get((weight, SCOPE_ALL), 0)
            if count > total:
                raise sqlite3.IntegrityError(
                    f"未暂扣票数 {count} 超过全部票数 {total}（重量 {weight}）"
                )


def validate_shift_bounds(
    leaves: dict[tuple[int, str], int], shift: int
) -> None:
    """Reject a shift that would move any original ticket outside 0..65535."""
    if shift == 0:
        return
    for (weight, scope), count in leaves.items():
        if scope != SCOPE_ALL or count <= 0:
            continue
        corrected = weight + shift
        if not WEIGHT_MIN <= corrected <= WEIGHT_MAX:
            raise ValidationError(
                f"登记被拒：原单净重 {weight} kg 纠偏 {shift:+d} 后为 "
                f"{corrected} kg，超出 0～65535 千克范围；该纠偏量未登记。"
            )


def shift_day_subtotals(
    conn: sqlite3.Connection,
    day_text: str,
    leaves: dict[tuple[int, str], int],
    shift: int,
) -> None:
    """Move the day's effective weights by ``shift`` kg on every date ancestor.

    ``leaves`` is the single-day segment's current leaf distribution. The new
    leaf distribution is derived in memory (weights shifted, colliding weights
    merged). The difference between the old and new dyadic weight trees is
    then applied as a delta to every date segment of the day's ancestor
    chain -- leaves and every wider weight band the rank query reads.

    Applying a *delta* (rather than rewriting whole segments) is essential:
    ancestor segments also aggregate other days, whose rows must survive.
    Bounds must have been validated before calling this.
    """
    if shift == 0:
        return
    day = parse_iso_date(day_text, "日期")
    date_keys = [seg.key for seg in date_ancestors(day)]
    active_scopes = {scope for (_, scope), count in leaves.items() if count > 0}

    new_leaves: dict[tuple[int, str], int] = {}
    for (weight, scope), count in leaves.items():
        if count <= 0:
            continue
        new_key = (weight + shift, scope)
        new_leaves[new_key] = new_leaves.get(new_key, 0) + count

    # Per-scope delta of dyadic band counts: new tree minus old tree.
    delta: dict[tuple[str, str], int] = {}
    for scope in active_scopes:
        old_bands: dict[str, int] = {}
        for (weight, s), count in leaves.items():
            if s != scope:
                continue
            for seg in weight_ancestors(weight):
                old_bands[seg.key] = old_bands.get(seg.key, 0) + count
        new_bands: dict[str, int] = {}
        for (weight, s), count in new_leaves.items():
            if s != scope:
                continue
            for seg in weight_ancestors(weight):
                new_bands[seg.key] = new_bands.get(seg.key, 0) + count
        for weight_key in set(old_bands) | set(new_bands):
            amount = new_bands.get(weight_key, 0) - old_bands.get(weight_key, 0)
            if amount:
                delta[(weight_key, scope)] = amount

    for date_key in date_keys:
        for (weight_key, scope), amount in delta.items():
            if amount > 0:
                conn.execute(
                    """
                    INSERT INTO subtotals(date_key, weight_key, scope, ticket_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(date_key, weight_key, scope) DO UPDATE SET
                        ticket_count = subtotals.ticket_count + excluded.ticket_count
                    """,
                    (date_key, weight_key, scope, amount),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE subtotals
                    SET ticket_count = ticket_count + ?
                    WHERE date_key = ? AND weight_key = ? AND scope = ?
                      AND ticket_count + ? >= 0
                    """,
                    (amount, date_key, weight_key, scope, amount),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        f"缺少或无法扣减小计键：{date_key} / {weight_key} / {scope} / {amount}"
                    )
    conn.execute("DELETE FROM subtotals WHERE ticket_count = 0")


def get_correction(conn: sqlite3.Connection, day_text: str) -> int:
    """Return the registered offset for a day (0 when none)."""
    day = parse_iso_date(day_text, "日期").isoformat()
    row = conn.execute(
        "SELECT offset_kg FROM corrections WHERE slip_date = ?",
        (day,),
    ).fetchone()
    return int(row["offset_kg"]) if row is not None else 0


def list_corrections(
    conn: sqlite3.Connection, year: Optional[int] = None
) -> list[dict[str, object]]:
    if year is not None:
        rows = conn.execute(
            """
            SELECT slip_date, offset_kg
            FROM corrections
            WHERE substr(slip_date, 1, 4) = ?
            ORDER BY slip_date
            """,
            (str(year),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT slip_date, offset_kg FROM corrections ORDER BY slip_date"
        ).fetchall()
    return [
        {
            "slip_date": row["slip_date"],
            "year": int(row["slip_date"][:4]),
            "offset_kg": int(row["offset_kg"]),
        }
        for row in rows
    ]


def register_correction(
    conn: sqlite3.Connection, slip_date: str, offset_value: object
) -> dict[str, object]:
    """Register (or cancel with 0) the single correction day of a year.

    The bounds check reads subtotal leaf counts only; the tickets table is
    never touched here. Everything runs in one write transaction, so any
    failure leaves the previously committed state intact.
    """
    day = parse_iso_date(slip_date, "纠偏日期")
    day_text = day.isoformat()
    offset = parse_correction_offset(offset_value)

    with write_transaction(conn):
        existing = conn.execute(
            "SELECT offset_kg FROM corrections WHERE slip_date = ?",
            (day_text,),
        ).fetchone()
        old_offset = int(existing["offset_kg"]) if existing is not None else 0

        # One correction day per natural year (the unique expression index is
        # the hard guarantee; this gives a friendly message).
        other = conn.execute(
            """
            SELECT slip_date, offset_kg
            FROM corrections
            WHERE substr(slip_date, 1, 4) = ? AND slip_date <> ?
            """,
            (str(day.year), day_text),
        ).fetchone()
        if other is not None:
            raise ValidationError(
                f"{day.year} 年已登记纠偏日 {other['slip_date']}"
                f"（纠偏量 {int(other['offset_kg']):+d} kg），"
                "每个自然年只能设一个纠偏日；请先把该日纠偏量改为 0 取消。"
            )

        # Only the single-day segment describes this day's own tickets;
        # ancestor segments aggregate other days too and must never feed the
        # bounds check or the shift.
        leaves = read_single_day_leaf_counts(conn, day_text)
        _check_leaf_scope_consistency(leaves)

        if offset == 0:
            if old_offset == 0:
                action = "unchanged"
                shift = 0
            else:
                action = "cancelled"
                shift = -old_offset
                validate_shift_bounds(leaves, shift)
                shift_day_subtotals(conn, day_text, leaves, shift)
                conn.execute(
                    "DELETE FROM corrections WHERE slip_date = ?", (day_text,)
                )
        else:
            action = "updated" if existing is not None else "registered"
            shift = offset - old_offset
            validate_shift_bounds(leaves, shift)
            shift_day_subtotals(conn, day_text, leaves, shift)
            conn.execute(
                """
                INSERT INTO corrections(slip_date, offset_kg)
                VALUES (?, ?)
                ON CONFLICT(slip_date) DO UPDATE SET
                    offset_kg = excluded.offset_kg
                """,
                (day_text, offset),
            )

        touched_leaves = sum(1 for (_, scope), c in leaves.items() if scope == SCOPE_ALL and c > 0)
        return {
            "date": day_text,
            "offset_kg": offset,
            "previous_offset_kg": old_offset,
            "shift_kg": shift,
            "action": action,
            "leaf_weights_checked": touched_leaves,
        }


# ---------------------------------------------------------------------------
# Aggregate updates
# ---------------------------------------------------------------------------


def _add_delta(delta: dict[tuple[str, str, str], int], key: tuple[str, str, str], value: int) -> None:
    delta[key] = delta.get(key, 0) + value


def add_ticket_to_delta(
    delta: dict[tuple[str, str, str], int],
    slip_date: str | date,
    weight: int,
    held: bool,
    sign: int,
    include_all: bool = True,
    include_active: bool | None = None,
) -> None:
    day = slip_date if isinstance(slip_date, date) else parse_iso_date(slip_date, "单据日期")
    date_keys = [seg.key for seg in date_ancestors(day)]
    weight_keys = [seg.key for seg in weight_ancestors(weight)]
    if include_active is None:
        include_active = not held

    for date_key in date_keys:
        for weight_key in weight_keys:
            if include_all:
                _add_delta(delta, (date_key, weight_key, SCOPE_ALL), sign)
            if include_active:
                _add_delta(delta, (date_key, weight_key, SCOPE_ACTIVE), sign)


def apply_delta(conn: sqlite3.Connection, delta: dict[tuple[str, str, str], int]) -> int:
    changes = 0
    for (date_key, weight_key, scope), amount in delta.items():
        if amount == 0:
            continue
        if amount > 0:
            conn.execute(
                """
                INSERT INTO subtotals(date_key, weight_key, scope, ticket_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date_key, weight_key, scope) DO UPDATE SET
                    ticket_count = subtotals.ticket_count + excluded.ticket_count
                """,
                (date_key, weight_key, scope, amount),
            )
        else:
            # Do not INSERT a negative proposed count: SQLite can evaluate the
            # CHECK before UPSERT conflict handling. A decremented key must
            # already exist because the corresponding original ticket exists.
            cursor = conn.execute(
                """
                UPDATE subtotals
                SET ticket_count = ticket_count + ?
                WHERE date_key = ? AND weight_key = ? AND scope = ?
                  AND ticket_count + ? >= 0
                """,
                (amount, date_key, weight_key, scope, amount),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    f"缺少或无法扣减小计键：{date_key} / {weight_key} / {scope} / {amount}"
                )
        changes += 1
    conn.execute("DELETE FROM subtotals WHERE ticket_count = 0")
    return changes


def adjust_existing_ticket_active(
    conn: sqlite3.Connection, slip_date: str, weight: int, sign: int
) -> None:
    delta: dict[tuple[str, str, str], int] = {}
    add_ticket_to_delta(
        delta,
        slip_date,
        weight,
        held=False,
        sign=sign,
        include_all=False,
        include_active=True,
    )
    apply_delta(conn, delta)


# ---------------------------------------------------------------------------
# Upload / replacement and holds
# ---------------------------------------------------------------------------


def effective_weight(original_weight: int, offset: int) -> int:
    return original_weight + offset


def replace_day_tickets(
    conn: sqlite3.Connection, slip_date: str, tickets: list[tuple[str, int]]
) -> dict[str, int]:
    day = parse_iso_date(slip_date, "上传日期")
    date_text = day.isoformat()

    with write_transaction(conn):
        offset = get_correction(conn, date_text)

        # Whole-day re-reports keep entering original weights; the registered
        # correction still applies. One out-of-range corrected ticket rejects
        # the whole day before any row is rewritten.
        out_of_range = [
            (no, weight, weight + offset)
            for no, weight in tickets
            if not WEIGHT_MIN <= weight + offset <= WEIGHT_MAX
        ]
        if out_of_range:
            no, original, corrected = out_of_range[0]
            raise ValidationError(
                f"整日重报被拒：单号 {no} 原净重 {original} kg 按已登记纠偏量 "
                f"{offset:+d} 后为 {corrected} kg，超出 0～65535 千克；"
                "当日旧秤单保持不变。",
                errors=[
                    f"单号 {no}：{original} kg 纠偏后 {corrected} kg 越界"
                    for no, original, corrected in out_of_range
                ],
            )

        old_rows = conn.execute(
            "SELECT ticket_no, net_weight FROM tickets WHERE slip_date = ?",
            (date_text,),
        ).fetchall()
        held_rows = conn.execute(
            "SELECT ticket_no FROM holds WHERE slip_date = ?",
            (date_text,),
        ).fetchall()
        held_numbers = {row["ticket_no"] for row in held_rows}

        delta: dict[tuple[str, str, str], int] = {}
        for row in old_rows:
            # Subtotals hold effective weights: original reading plus offset.
            add_ticket_to_delta(
                delta,
                date_text,
                effective_weight(int(row["net_weight"]), offset),
                held=row["ticket_no"] in held_numbers,
                sign=-1,
            )
        for ticket_no, weight in tickets:
            add_ticket_to_delta(
                delta,
                date_text,
                effective_weight(weight, offset),
                held=ticket_no in held_numbers,
                sign=+1,
            )

        conn.execute("DELETE FROM tickets WHERE slip_date = ?", (date_text,))
        conn.executemany(
            "INSERT INTO tickets(slip_date, ticket_no, net_weight) VALUES (?, ?, ?)",
            [(date_text, no, weight) for no, weight in tickets],
        )
        changed_keys = apply_delta(conn, delta)

    return {
        "date": date_text,
        "old_count": len(old_rows),
        "new_count": len(tickets),
        "correction_offset_kg": offset,
        "subtotal_delta_keys": changed_keys,
    }


def _get_ticket_row(conn: sqlite3.Connection, day: str, ticket_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT slip_date, ticket_no, net_weight
        FROM tickets
        WHERE slip_date = ? AND ticket_no = ?
        """,
        (day, ticket_no),
    ).fetchone()


def hold_ticket(
    conn: sqlite3.Connection,
    slip_date: str,
    ticket_no: str,
    reason: Optional[str] = None,
) -> dict[str, object]:
    day = parse_iso_date(slip_date, "单据日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    with write_transaction(conn):
        existing_hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (day, ticket_no),
        ).fetchone()
        ticket = _get_ticket_row(conn, day, ticket_no)

        if existing_hold:
            action = "already_held"
            if reason is not None:
                conn.execute(
                    "UPDATE holds SET reason = ? WHERE slip_date = ? AND ticket_no = ?",
                    (reason.strip() or None, day, ticket_no),
                )
        else:
            action = "held"
            conn.execute(
                "INSERT INTO holds(slip_date, ticket_no, reason) VALUES (?, ?, ?)",
                (day, ticket_no, (reason or "").strip() or None),
            )
            if ticket is not None:
                offset = get_correction(conn, day)
                adjust_existing_ticket_active(
                    conn,
                    day,
                    effective_weight(int(ticket["net_weight"]), offset),
                    -1,
                )

        hold = conn.execute(
            "SELECT slip_date, ticket_no, reason, created_at FROM holds WHERE slip_date=? AND ticket_no=?",
            (day, ticket_no),
        ).fetchone()

    return {
        "action": action,
        "hold": dict(hold) if hold else None,
        "ticket": dict(ticket) if ticket else None,
        "original_missing": ticket is None,
    }


def release_ticket(conn: sqlite3.Connection, slip_date: str, ticket_no: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "单据日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    with write_transaction(conn):
        existing_hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (day, ticket_no),
        ).fetchone()
        ticket = _get_ticket_row(conn, day, ticket_no)

        if existing_hold:
            action = "released"
            conn.execute(
                "DELETE FROM holds WHERE slip_date = ? AND ticket_no = ?",
                (day, ticket_no),
            )
            if ticket is not None:
                offset = get_correction(conn, day)
                adjust_existing_ticket_active(
                    conn,
                    day,
                    effective_weight(int(ticket["net_weight"]), offset),
                    +1,
                )
        else:
            action = "not_held"

    return {
        "action": action,
        "ticket": dict(ticket) if ticket else None,
        "original_missing": ticket is None,
    }


def get_day_readback(conn: sqlite3.Connection, slip_date: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "日期").isoformat()
    offset = get_correction(conn, day)
    tickets = conn.execute(
        """
        SELECT t.slip_date, t.ticket_no, t.net_weight,
               CASE WHEN h.ticket_no IS NULL THEN 0 ELSE 1 END AS held,
               h.reason
        FROM tickets AS t
        LEFT JOIN holds AS h
          ON h.slip_date = t.slip_date AND h.ticket_no = t.ticket_no
        WHERE t.slip_date = ?
        ORDER BY t.ticket_no
        """,
        (day,),
    ).fetchall()
    ticket_dicts: list[dict[str, object]] = []
    for row in tickets:
        item = dict(row)
        item["correction_offset_kg"] = offset
        item["effective_weight"] = int(row["net_weight"]) + offset
        ticket_dicts.append(item)
    missing_holds = conn.execute(
        """
        SELECT h.slip_date, h.ticket_no, h.reason, h.created_at
        FROM holds AS h
        LEFT JOIN tickets AS t
          ON t.slip_date = h.slip_date AND t.ticket_no = h.ticket_no
        WHERE h.slip_date = ? AND t.ticket_no IS NULL
        ORDER BY h.ticket_no
        """,
        (day,),
    ).fetchall()
    return {
        "date": day,
        "correction_offset_kg": offset,
        "tickets": ticket_dicts,
        "missing_holds": [dict(row) for row in missing_holds],
    }


def get_ticket_readback(conn: sqlite3.Connection, slip_date: str, ticket_no: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")
    with read_transaction(conn):
        ticket = _get_ticket_row(conn, day, ticket_no)
        hold = conn.execute(
            "SELECT slip_date, ticket_no, reason, created_at FROM holds WHERE slip_date=? AND ticket_no=?",
            (day, ticket_no),
        ).fetchone()
        offset = get_correction(conn, day)
    ticket_dict = dict(ticket) if ticket else None
    effective = int(ticket_dict["net_weight"]) + offset if ticket_dict is not None else None
    return {
        "ticket": ticket_dict,
        "hold": dict(hold) if hold else None,
        "held": hold is not None,
        "correction_offset_kg": offset,
        "effective_weight": effective,
    }


# ---------------------------------------------------------------------------
# Rank query and counted subtotal access
# ---------------------------------------------------------------------------


@dataclass
class KeyMeter:
    accessed: int = 0
    empty: int = 0
    details: list[dict[str, object]] = field(default_factory=list)

    def get(
        self,
        conn: sqlite3.Connection,
        date_seg: DateSeg,
        weight_seg: WeightSeg,
        scope: str,
    ) -> int:
        self.accessed += 1
        row = conn.execute(
            """
            SELECT ticket_count
            FROM subtotals
            WHERE date_key = ? AND weight_key = ? AND scope = ?
            """,
            (date_seg.key, weight_seg.key, scope),
        ).fetchone()
        is_empty = row is None
        if is_empty:
            self.empty += 1
            value = 0
        else:
            value = int(row["ticket_count"])
        self.details.append(
            {
                "n": self.accessed,
                "scope": scope,
                "date_segment": date_seg.label,
                "date_key": date_seg.key,
                "weight_band": weight_seg.label,
                "weight_key": weight_seg.key,
                "count": value,
                "empty": is_empty,
            }
        )
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "accessed": self.accessed,
            "empty": self.empty,
            "limit": SUBTOTAL_KEY_LIMIT,
            "within_limit": self.accessed <= SUBTOTAL_KEY_LIMIT,
            "details": self.details,
        }


def _rank_payload(total: int, higher: int, equal: int) -> dict[str, object]:
    if equal <= 0:
        return {
            "total": total,
            "higher": higher,
            "equal": equal,
            "rank_start": None,
            "rank_end": None,
            "rank_text": "无法计算",
        }
    start_rank = higher + 1
    end_rank = higher + equal
    if start_rank == end_rank:
        rank_text = f"第 {start_rank} 名"
    else:
        rank_text = f"第 {start_rank}-{end_rank} 名（{equal} 单同重）"
    return {
        "total": total,
        "higher": higher,
        "equal": equal,
        "rank_start": start_rank,
        "rank_end": end_rank,
        "rank_text": rank_text,
    }


def query_rank(
    conn: sqlite3.Connection,
    slip_date: str,
    ticket_no: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, object]:
    target_day = parse_iso_date(slip_date, "单据日期")
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    start = parse_iso_date(start_date, "起始日") if start_date else date(target_day.year, 1, 1)
    end = parse_iso_date(end_date, "结束日") if end_date else date(target_day.year, 12, 31)

    if start.year != end.year:
        raise ValidationError("起止日必须在同一年")
    if not (start <= target_day <= end):
        raise ValidationError("单据日期必须包含在起止区间内（含首尾）")
    if start.year != target_day.year:
        raise ValidationError("起止日与单据日期必须在同一年")

    date_segs = date_cover(start, end)
    root_weight = WeightSeg(0, WEIGHT_SIZE)
    greater_weights: Optional[list[WeightSeg]] = None
    equal_weight: Optional[WeightSeg] = None
    meter = KeyMeter()

    with read_transaction(conn):
        target = _get_ticket_row(conn, target_day.isoformat(), ticket_no)
        hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (target_day.isoformat(), ticket_no),
        ).fetchone()
        held = hold is not None
        offset = get_correction(conn, target_day.isoformat())

        if target is None:
            return {
                "found": False,
                "held": held,
                "query": {
                    "date": target_day.isoformat(),
                    "ticket_no": ticket_no,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "correction_offset_kg": offset,
                "date_segments": [seg.label for seg in date_segs],
                "date_segment_count": len(date_segs),
                "meter": meter.to_dict(),
                "all": None,
                "active": {
                    "held": held,
                    "rank_text": "暂扣中，名次留空" if held else "原单不存在，无法排名",
                },
            }

        original_weight = int(target["net_weight"])
        # Ranks are always computed on corrected (effective) weights.
        weight = original_weight + offset
        if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
            raise sqlite3.IntegrityError(
                f"目标单纠偏后重量 {weight} 越界；小计状态可能已损坏"
            )
        greater_weights = weight_greater_cover(weight)
        equal_weight = WeightSeg(weight, weight + 1)

        all_total = all_higher = all_equal = 0
        active_total = active_higher = active_equal = 0

        for ds in date_segs:
            all_total += meter.get(conn, ds, root_weight, SCOPE_ALL)
            for ws in greater_weights:
                all_higher += meter.get(conn, ds, ws, SCOPE_ALL)
            all_equal += meter.get(conn, ds, equal_weight, SCOPE_ALL)

            active_total += meter.get(conn, ds, root_weight, SCOPE_ACTIVE)
            if not held:
                for ws in greater_weights:
                    active_higher += meter.get(conn, ds, ws, SCOPE_ACTIVE)
                active_equal += meter.get(conn, ds, equal_weight, SCOPE_ACTIVE)

    all_result = _rank_payload(all_total, all_higher, all_equal)
    active_result: dict[str, object]
    if held:
        active_result = {
            "total": active_total,
            "higher": None,
            "equal": None,
            "rank_start": None,
            "rank_end": None,
            "rank_text": "",
            "held": True,
            "note": "目标单已暂扣；未暂扣口径只显示总数，名次按要求留空。",
        }
    else:
        active_result = _rank_payload(active_total, active_higher, active_equal)
        active_result["held"] = False

    return {
        "found": True,
        "held": held,
        "target": dict(target),
        "query": {
            "date": target_day.isoformat(),
            "ticket_no": ticket_no,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "original_weight": original_weight,
            "correction_offset_kg": offset,
            "weight": weight,
        },
        "correction_offset_kg": offset,
        "original_weight": original_weight,
        "effective_weight": weight,
        "date_segments": [seg.label for seg in date_segs],
        "date_segment_count": len(date_segs),
        "weight_greater_key_count": len(greater_weights),
        "meter": meter.to_dict(),
        "all": all_result,
        "active": active_result,
    }
