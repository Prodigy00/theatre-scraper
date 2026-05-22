"""
CSV output validator for theatre scraper data.

Checks that a scraped CSV file matches the expected schema and data quality
rules before it is uploaded or used downstream.

Usage:
    python src/utils/csv_validator.py path/to/output.csv
    python -m utils.csv_validator path/to/output.csv

Exit codes:
    0  all checks passed (warnings are OK)
    1  one or more checks failed
"""

import ast
import io
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
from dateutil import parser as dateutil_parser

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

# Every output CSV must contain exactly these columns, in this order.
COLUMN_ORDER = [
    "title",
    "venue_url",
    "category",
    "venue",
    "address",
    "city",
    "country",
    "open_date",
    "close_date",
    "booking_start_date",
    "booking_end_date",
    "upcoming_performances",
    "capacity",
    "currency",
    "is_limited_run",
    "seat_pricing",
    "scrape_datetime",
]
REQUIRED_COLUMNS = set(COLUMN_ORDER)

# Fields that must never be empty in any row.
REQUIRED_NON_EMPTY = [
    "title",
    "venue_url",
    "venue",
    "city",
    "country",
    "address",
    "scrape_datetime",
    "upcoming_performances",
    "seat_pricing",
]

ALLOWED_CATEGORIES = {"musical", "play"}

# Compiled patterns reused across checks.
TIME_24H = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_YYYYMMDD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_KEY = re.compile(r"^\d{4}-\d{2}-\d{2} ([01]\d|2[0-3]):[0-5]\d$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
URL = re.compile(r"^https?://.+")
DOUBLE_QUOTE_RE = re.compile(r'(?<![\\])"')

# Threshold below which "all seats share one ID" is too small to be meaningful.
GENERIC_SEAT_MIN_ENTRIES = 3

# Minimum performance count for the cross-performance duplicate-seat-map check.
# 2–4 identical performances → WARN; ≥ DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS → FAIL.
DUPLICATE_SEAT_MAP_MIN_PERFS = 2
DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS = 5

# A performance with seat count below this fraction of capacity (and capacity
# above SEAT_COUNT_MIN_CAPACITY) likely captures price tiers, not real seats.
SEAT_COUNT_MIN_RATIO = 0.25
SEAT_COUNT_MIN_CAPACITY = 20


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_cell(value: Any) -> Any:
    """Parse a CSV cell that may contain a Python literal (list / dict).

    Returns None for blank cells; the parsed object otherwise. Falls back to
    JSON, then the original string, when literal_eval fails.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict)):
        return value

    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None

    try:
        return ast.literal_eval(text)
    except Exception:
        pass

    try:
        return json.loads(text)
    except Exception:
        return text


def _is_empty(value: Any) -> bool:
    """Return True if a cell value should be treated as missing / empty."""
    if value is None:
        return True
    if isinstance(value, float):
        try:
            if pd.isna(value):
                return True
        except Exception:
            pass
    return str(value).strip().lower() in ("", "nan", "none", "null")


def _examples(items: list, fmt: Callable, limit: int = 5) -> str:
    """Format up to `limit` items as a short example string."""
    shown = "; ".join(fmt(x) for x in items[:limit])
    return shown + (" …" if len(items) > limit else "")


def _try_parse_datetime(value: Any) -> Optional[datetime]:
    """Parse a value into a datetime, or return None."""
    if _is_empty(value):
        return None
    try:
        return dateutil_parser.parse(str(value).strip(), yearfirst=True)
    except Exception:
        return None


def _try_parse_date(value: Any) -> Optional[date]:
    """Parse a value into a date, or return None."""
    parsed = _try_parse_datetime(value)
    return parsed.date() if parsed else None


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────


class Report:
    """Collects check results and renders a formatted summary."""

    def __init__(self) -> None:
        self._checks: list[tuple[str, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self._checks.append(("PASS", name, detail))

    def warn(self, name: str, detail: str) -> None:
        self._checks.append(("WARN", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self._checks.append(("FAIL", name, detail))

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(name, detail) for lvl, name, detail in self._checks if lvl == "FAIL"]

    @property
    def passed(self) -> bool:
        return not any(lvl == "FAIL" for lvl, _, _ in self._checks)

    def counts(self) -> dict[str, int]:
        return {
            "pass": sum(1 for lvl, _, _ in self._checks if lvl == "PASS"),
            "warn": sum(1 for lvl, _, _ in self._checks if lvl == "WARN"),
            "fail": sum(1 for lvl, _, _ in self._checks if lvl == "FAIL"),
        }

    def render(self, color: bool = True, only_failures: bool = False) -> str:
        """Render the report as a multi-line string.

        color: ANSI codes for terminal output (False for logs / Slack).
        only_failures: include just FAIL entries (compact output for notifications).
        """
        WIDTH = 88
        COLOUR = (
            {"PASS": "\033[92m", "WARN": "\033[93m", "FAIL": "\033[91m"}
            if color
            else {"PASS": "", "WARN": "", "FAIL": ""}
        )
        RESET = "\033[0m" if color else ""

        lines = ["=" * WIDTH, "  CSV VALIDATION REPORT", "=" * WIDTH]
        entries = (
            [(lvl, n, d) for lvl, n, d in self._checks if lvl == "FAIL"]
            if only_failures
            else self._checks
        )
        for level, name, detail in entries:
            tag = f"{COLOUR[level]}[{level}]{RESET}"
            lines.append(f"  {tag:<20} {name}")
            if detail:
                for ln in detail.split("\n"):
                    lines.append(f"               {ln}")

        c = self.counts()
        result_colour = (
            COLOUR["FAIL"]
            if c["fail"]
            else (COLOUR["WARN"] if c["warn"] else COLOUR["PASS"])
        )
        result_word = "FAILED" if c["fail"] else "PASSED"

        lines.append("-" * WIDTH)
        lines.append(
            f"  Result: {result_colour}{result_word}{RESET}  "
            f"({c['pass']} passed · {c['warn']} warnings · {c['fail']} failures)"
        )
        lines.append("=" * WIDTH)
        return "\n".join(lines)

    def print_summary(self) -> bool:
        print(self.render(color=True))
        return self.passed


# ─────────────────────────────────────────────────────────────────────────────
# Validation context
# ─────────────────────────────────────────────────────────────────────────────


class _Ctx:
    """Shared validation state — pre-parses dict/list columns once.

    Checks read `parsed_seat_pricing[i]` and `parsed_perfs[i]` instead of
    re-running `ast.literal_eval` on every iteration. `perf_dates[i]` is the
    set of distinct performance dates per row, consulted by multiple boundary
    checks.
    """

    __slots__ = ("df", "report", "parsed_seat_pricing", "parsed_perfs", "perf_dates")

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df
        self.report = Report()
        self.parsed_seat_pricing: list[Any] = self._parse_column("seat_pricing")
        self.parsed_perfs: list[Any] = self._parse_column("upcoming_performances")
        self.perf_dates: list[set[date]] = self._compute_perf_dates()

    def _parse_column(self, col: str) -> list[Any]:
        if col not in self.df.columns:
            return [None] * len(self.df)
        return [_parse_cell(v) for v in self.df[col]]

    def _compute_perf_dates(self) -> list[set[date]]:
        result: list[set[date]] = []
        for perfs in self.parsed_perfs:
            dates: set[date] = set()
            if isinstance(perfs, list):
                for entry in perfs:
                    if isinstance(entry, dict) and entry.get("date"):
                        d = _try_parse_date(entry["date"])
                        if d is not None:
                            dates.add(d)
            result.append(dates)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Schema checks
# ─────────────────────────────────────────────────────────────────────────────


def check_columns(ctx: _Ctx) -> None:
    df, report = ctx.df, ctx.report
    missing = REQUIRED_COLUMNS - set(df.columns)
    extra = set(df.columns) - REQUIRED_COLUMNS

    if missing:
        report.fail("Required columns present", f"Missing: {sorted(missing)}")
    else:
        report.ok("Required columns present", "All required columns found")

    if extra:
        report.warn("No unexpected columns", f"Extra columns: {sorted(extra)}")


def check_column_order(ctx: _Ctx) -> None:
    df, report = ctx.df, ctx.report
    actual = [c for c in df.columns if c in REQUIRED_COLUMNS]
    expected = [c for c in COLUMN_ORDER if c in df.columns]

    if actual == expected:
        report.ok("Column order", "Required columns follow the expected order")
        return

    mismatches = [
        f"'{col}' at position {actual.index(col) + 1} "
        f"(expected {expected.index(col) + 1})"
        for col in expected
        if actual.index(col) != expected.index(col)
    ]
    report.fail(
        "Column order",
        "Required columns are not in the expected order:\n"
        + f"  Expected: {', '.join(expected)}\n"
        + f"  Got:      {', '.join(actual)}\n"
        + f"  Differences: {'; '.join(mismatches)}",
    )


def check_required_fields_not_empty(ctx: _Ctx) -> None:
    df, report = ctx.df, ctx.report
    has_title = "title" in df.columns
    issues: dict[str, list[tuple[int, str]]] = {}
    for field in REQUIRED_NON_EMPTY:
        if field not in df.columns:
            continue
        empty_rows: list[tuple[int, str]] = []
        for i, v in df[field].items():
            if _is_empty(v):
                title = str(df["title"].iat[i]) if has_title else ""
                title = title.strip() if not _is_empty(title) else "<no title>"
                empty_rows.append((i + 2, title))
        if empty_rows:
            issues[field] = empty_rows

    if issues:
        detail = "; ".join(
            f"'{f}': "
            + ", ".join(f"row {r} ('{t}')" for r, t in rows[:5])
            + ("…" if len(rows) > 5 else "")
            for f, rows in issues.items()
        )
        report.fail("Required fields not empty", detail)
    else:
        report.ok(
            "Required fields not empty", f"All of {REQUIRED_NON_EMPTY} are populated"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Content checks
# ─────────────────────────────────────────────────────────────────────────────


def check_one_row_per_show_venue(ctx: _Ctx) -> None:
    """Each (title, venue) pair should appear exactly once.

    Multiple rows for the same show+venue suggest the scraper produced one row
    per performance instead of folding them into upcoming_performances.
    """
    df, report = ctx.df, ctx.report
    if "title" not in df.columns or "venue" not in df.columns:
        report.warn("One row per show+venue", "Skipped — missing title or venue column")
        return

    dupes = df[df.duplicated(subset=["title", "venue"], keep=False)]
    if dupes.empty:
        report.ok("One row per show+venue", "No duplicate (title, venue) pairs found")
        return

    pairs = dupes[["title", "venue"]].drop_duplicates().values.tolist()
    report.fail(
        "One row per show+venue",
        f"{len(pairs)} duplicate pair(s): "
        + _examples(pairs, lambda p: f"'{p[0]}' @ '{p[1]}'"),
    )


def check_categories(ctx: _Ctx) -> None:
    """Category must be 'Musical' or 'Play' (case-insensitive). Blank is OK."""
    df, report = ctx.df, ctx.report
    if "category" not in df.columns:
        return

    bad = [
        (i + 2, str(v).strip())
        for i, v in df["category"].items()
        if not _is_empty(v) and str(v).strip().lower() not in ALLOWED_CATEGORIES
    ]
    if bad:
        report.fail(
            "Category is 'Musical' or 'Play'",
            f"{len(bad)} disallowed value(s): "
            + _examples(bad, lambda x: f"row {x[0]}: '{x[1]}'"),
        )
    else:
        report.ok("Category is 'Musical' or 'Play'")


def check_seat_ids(ctx: _Ctx) -> None:
    """Two related rules over seat_pricing seat IDs, in one pass:

    1. FAIL — within a performance, all seat IDs identical. Unreserved
       venues must return `{}` for seat_pricing rather than repeated
       placeholder seat entries, so this shape always indicates a bug.
    2. FAIL — within a performance, seat IDs not unique (the scraper
       merged or duplicated rows from the seat map).

    The all-identical case satisfies (1) and is excluded from (2).
    """
    df, report = ctx.df, ctx.report
    if "seat_pricing" not in df.columns:
        return

    all_identical: list[tuple] = []
    duplicates: list[tuple] = []

    for i, pricing in enumerate(ctx.parsed_seat_pricing):
        if not isinstance(pricing, dict):
            continue
        for perf_dt, seats in pricing.items():
            if not isinstance(seats, list):
                continue
            seat_ids = [
                str(entry.get("seat", "")).strip()
                for entry in seats
                if isinstance(entry, dict)
            ]
            distinct = set(seat_ids)
            if len(distinct) == 1 and len(seat_ids) >= GENERIC_SEAT_MIN_ENTRIES:
                all_identical.append((i + 2, perf_dt, seat_ids[0], len(seat_ids)))
            elif len(seat_ids) > len(distinct):
                prices_by_seat: dict[str, set] = {}
                for entry in seats:
                    if not isinstance(entry, dict):
                        continue
                    sid = str(entry.get("seat", "")).strip()
                    prices_by_seat.setdefault(sid, set()).add(entry.get("ticket_price"))
                multi_price = sorted(
                    s for s, ps in prices_by_seat.items() if len(ps) > 1
                )
                repeats = [s for s in distinct if seat_ids.count(s) > 1]
                duplicates.append(
                    (
                        i + 2,
                        perf_dt,
                        len(seat_ids),
                        len(distinct),
                        repeats,
                        multi_price,
                    )
                )

    if all_identical:
        report.fail(
            "Seat IDs are not all identical within a performance",
            f"{len(all_identical)} performance(s) where every seat shares the same ID. "
            f"Unreserved venues must return seat_pricing={{}} rather than repeated "
            f"placeholder seat entries: "
            + _examples(
                all_identical,
                lambda x: f"row {x[0]} ({x[1]}): '{x[2]}' repeated {x[3]}x",
            ),
        )
    else:
        report.ok(
            "Seat IDs are not all identical within a performance",
            "Seat IDs vary within each performance — no generic placeholders detected",
        )

    if duplicates:
        report.fail(
            "Seat IDs are unique within each performance",
            f"{len(duplicates)} performance(s) with duplicate seat IDs "
            f"(seats listed at multiple price tiers suggests the scraper split "
            f"each seat into one row per price): "
            + _examples(
                duplicates,
                lambda x: (
                    f"row {x[0]} ({x[1]}): {x[2]} seats, {x[3]} distinct — "
                    f"repeats: {x[4][:3]}{'…' if len(x[4]) > 3 else ''}"
                    + (
                        f"; {len(x[5])} seat(s) at >1 price (e.g. {x[5][:3]})"
                        if x[5]
                        else ""
                    )
                ),
            ),
        )
    else:
        report.ok(
            "Seat IDs are unique within each performance",
            "No duplicate seat IDs detected within any performance",
        )


def check_seat_map_duplicated_across_performances(ctx: _Ctx) -> None:
    """Flag rows where every performance shares the exact same seat map.

    Severity scales with how many performances are involved:
      2–4 identical performances → WARN (possible scraper reuse, but small
        venues with untouched seats can legitimately match).
      ≥5 identical performances  → FAIL (statistically near-impossible without
        the scraper copying one performance's data across the rest).
    """
    df, report = ctx.df, ctx.report
    if "seat_pricing" not in df.columns:
        return

    suspicious: list[tuple[int, str, int]] = []
    for i, pricing in enumerate(ctx.parsed_seat_pricing):
        if not isinstance(pricing, dict) or len(pricing) < DUPLICATE_SEAT_MAP_MIN_PERFS:
            continue

        fingerprints = set()
        skip = False
        for seats in pricing.values():
            if not isinstance(seats, list) or not seats:
                skip = True
                break
            pairs = []
            for entry in seats:
                if not isinstance(entry, dict):
                    skip = True
                    break
                pairs.append(
                    (
                        str(entry.get("seat", "")).strip(),
                        entry.get("ticket_price"),
                    )
                )
            if skip:
                break
            fingerprints.add(tuple(sorted(pairs)))
            if len(fingerprints) > 1:
                break

        if skip or len(fingerprints) != 1:
            continue

        title = (
            str(df["title"].iat[i]).strip()
            if "title" in df.columns and not _is_empty(df["title"].iat[i])
            else "<no title>"
        )
        suspicious.append((i + 2, title, len(pricing)))

    warn_name = f"Seat map differs across performances (2–{DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS - 1})"
    fail_name = (
        f"Seat map differs across performances (≥{DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS})"
    )
    warn_rows = [s for s in suspicious if s[2] < DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS]
    fail_rows = [s for s in suspicious if s[2] >= DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS]

    if fail_rows:
        report.fail(
            fail_name,
            f"{len(fail_rows)} row(s) with ≥{DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS} "
            f"performances sharing an identical (seat, ticket_price) map — "
            f"scraper is reusing one performance's seat data for the rest: "
            + _examples(
                fail_rows,
                lambda x: f"row {x[0]} ('{x[1]}'): {x[2]} performances",
            ),
        )
    if warn_rows:
        report.warn(
            warn_name,
            f"{len(warn_rows)} row(s) with 2–{DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS - 1} "
            f"performances sharing an identical (seat, ticket_price) map — "
            f"possible scraper reuse, verify manually: "
            + _examples(
                warn_rows,
                lambda x: f"row {x[0]} ('{x[1]}'): {x[2]} performances",
            ),
        )
    if not suspicious:
        report.ok(
            "Seat map differs across performances",
            "No rows where the seat map is identical across all performances",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Format checks
# ─────────────────────────────────────────────────────────────────────────────


def check_upcoming_performances_format(ctx: _Ctx) -> None:
    """upcoming_performances must be a list of {date: YYYY-MM-DD, time: HH:MM} dicts."""
    df, report = ctx.df, ctx.report
    if "upcoming_performances" not in df.columns:
        return

    not_a_list: list[tuple] = []
    bad_entries: list[tuple] = []

    raw_col = df["upcoming_performances"]
    for i, perfs in enumerate(ctx.parsed_perfs):
        raw = raw_col.iat[i]
        if _is_empty(raw):
            continue
        if not isinstance(perfs, list):
            actual = type(perfs).__name__ if perfs is not None else "unparseable"
            not_a_list.append((i + 2, str(raw)[:60], actual))
            continue
        for entry in perfs:
            if not isinstance(entry, dict):
                bad_entries.append((i + 2, f"entry is not a dict: {entry}"))
                continue
            if "date" not in entry or "time" not in entry:
                bad_entries.append((i + 2, f"missing 'date' or 'time' key: {entry}"))
                continue
            extra = set(entry) - {"date", "time"}
            if extra:
                bad_entries.append(
                    (i + 2, f"unexpected key(s) {sorted(extra)} in entry: {entry}")
                )
                continue
            if not DATE_YYYYMMDD.match(str(entry["date"]).strip()):
                bad_entries.append(
                    (i + 2, f"date '{entry['date']}' should be YYYY-MM-DD")
                )
            if not TIME_24H.match(str(entry["time"]).strip()):
                bad_entries.append(
                    (i + 2, f"time '{entry['time']}' should be HH:MM (24h)")
                )

    if not_a_list:
        report.fail(
            "upcoming_performances is a list",
            f"{len(not_a_list)} row(s) where upcoming_performances is not a list: "
            + _examples(not_a_list, lambda x: f"row {x[0]} (got {x[2]}): '{x[1]}'"),
        )
    else:
        report.ok(
            "upcoming_performances is a list",
            "All non-empty upcoming_performances values are lists",
        )

    if bad_entries:
        report.fail(
            "upcoming_performances structure",
            f"{len(bad_entries)} issue(s) — expected [{{date: YYYY-MM-DD, time: HH:MM}}]: "
            + _examples(bad_entries, lambda x: f"row {x[0]}: {x[1]}"),
        )
    else:
        report.ok(
            "upcoming_performances structure",
            "All entries follow [{date: YYYY-MM-DD, time: HH:MM}]",
        )


def check_seat_pricing_format(ctx: _Ctx) -> None:
    """seat_pricing must be {"YYYY-MM-DD HH:MM": [{seat, ticket_price}, …], …}."""
    df, report = ctx.df, ctx.report
    if "seat_pricing" not in df.columns:
        return

    not_a_dict: list[tuple] = []
    bad_structure: list[tuple] = []
    bad_prices: list[tuple] = []

    raw_col = df["seat_pricing"]
    for i, pricing in enumerate(ctx.parsed_seat_pricing):
        raw = raw_col.iat[i]
        if _is_empty(raw):
            continue
        if not isinstance(pricing, dict):
            actual = type(pricing).__name__ if pricing is not None else "unparseable"
            not_a_dict.append((i + 2, str(raw)[:60], actual))
            continue

        for key, seats in pricing.items():
            if not DATETIME_KEY.match(str(key).strip()):
                bad_structure.append(
                    (i + 2, f"key '{key}' is not in YYYY-MM-DD HH:MM format")
                )
                continue
            if seats is None:
                continue  # None = data unavailable (scrape failed), not a structure error
            if not isinstance(seats, list):
                bad_structure.append((i + 2, f"value for key '{key}' is not a list"))
                continue
            for entry in seats:
                if not isinstance(entry, dict):
                    bad_structure.append((i + 2, f"seat entry is not a dict: {entry}"))
                    continue
                if "seat" not in entry or "ticket_price" not in entry:
                    bad_structure.append(
                        (i + 2, f"missing 'seat' or 'ticket_price': {entry}")
                    )
                    continue
                extra = set(entry) - {"seat", "ticket_price"}
                if extra:
                    bad_structure.append(
                        (
                            i + 2,
                            f"unexpected key(s) {sorted(extra)} in seat entry: {entry}",
                        )
                    )
                    continue
                price = entry["ticket_price"]
                if isinstance(price, bool) or not isinstance(price, (int, float)):
                    bad_prices.append(
                        (
                            i + 2,
                            key,
                            entry["seat"],
                            price,
                            f"not numeric (got {type(price).__name__})",
                        )
                    )
                elif price < 0:
                    bad_prices.append(
                        (i + 2, key, entry["seat"], price, "negative value")
                    )

    if not_a_dict:
        report.fail(
            "seat_pricing is a dictionary",
            f"{len(not_a_dict)} row(s) where seat_pricing is not a dict: "
            + _examples(not_a_dict, lambda x: f"row {x[0]} (got {x[2]}): '{x[1]}'"),
        )
    else:
        report.ok(
            "seat_pricing is a dictionary",
            "All non-empty seat_pricing values are dicts",
        )

    if bad_structure:
        report.fail(
            "seat_pricing structure",
            f"{len(bad_structure)} issue(s) — expected "
            f"{{datetime: [{{seat, ticket_price}}]}}: "
            + _examples(bad_structure, lambda x: f"row {x[0]}: {x[1]}"),
        )
    else:
        report.ok(
            "seat_pricing structure",
            "All entries follow {datetime: [{seat: str, ticket_price: float}]}",
        )

    if bad_prices:
        report.fail(
            "ticket_price values are non-negative floats",
            f"{len(bad_prices)} invalid price(s): "
            + _examples(
                bad_prices,
                lambda x: f"row {x[0]} ({x[1]}) seat '{x[2]}': {x[3]!r} — {x[4]}",
            ),
        )
    else:
        report.ok("ticket_price values are non-negative floats")


def check_quote_style(ctx: _Ctx) -> None:
    """upcoming_performances and seat_pricing must use single-quoted strings."""
    df, report = ctx.df, ctx.report
    FIELDS = ("upcoming_performances", "seat_pricing")

    bad: dict[str, list] = defaultdict(list)
    for field in FIELDS:
        if field not in df.columns:
            continue
        for i, raw in df[field].items():
            if _is_empty(raw):
                continue
            raw_str = str(raw).strip()
            if DOUBLE_QUOTE_RE.search(raw_str):
                bad[field].append((i + 2, raw_str[:60]))

    if bad:
        lines = [
            f"[{field}] " + _examples(rows, lambda x: f"row {x[0]}: '{x[1]}'")
            for field, rows in bad.items()
        ]
        report.fail(
            "upcoming_performances / seat_pricing use single quotes",
            "Double-quoted strings found — expected single-quote format "
            "(e.g. {{'key': 'value'}}), not JSON format ({{\"key\": \"value\"}}):\n"
            + "\n".join(f"  {ln}" for ln in lines),
        )
    else:
        report.ok(
            "upcoming_performances / seat_pricing use single quotes",
            "All entries use single-quote string delimiters",
        )


def check_currency_format(ctx: _Ctx) -> None:
    """Currency must be a 3-letter ISO 4217 code."""
    df, report = ctx.df, ctx.report
    if "currency" not in df.columns:
        return

    col = df["currency"]
    populated = ~col.apply(_is_empty)
    stripped = col.astype(str).str.strip()
    valid = stripped.str.match(CURRENCY.pattern, na=False)
    bad_mask = populated & ~valid

    if bad_mask.any():
        bad = [(i + 2, stripped.iat[i]) for i in df.index[bad_mask]]
        report.fail(
            "Currency is a 3-letter ISO code",
            f"{len(bad)} invalid value(s): "
            + _examples(bad, lambda x: f"row {x[0]}: '{x[1]}'"),
        )
    else:
        report.ok("Currency is a 3-letter ISO code")


def check_url_format(ctx: _Ctx) -> None:
    """venue_url must be a valid http / https URL."""
    df, report = ctx.df, ctx.report
    if "venue_url" not in df.columns:
        return

    col = df["venue_url"]
    populated = ~col.apply(_is_empty)
    stripped = col.astype(str).str.strip()
    valid = stripped.str.match(URL.pattern, na=False)
    bad_mask = populated & ~valid

    if bad_mask.any():
        bad = [(i + 2, stripped.iat[i]) for i in df.index[bad_mask]]
        report.fail(
            "venue_url is a valid http(s) URL",
            f"{len(bad)} invalid URL(s): "
            + _examples(bad, lambda x: f"row {x[0]}: '{x[1]}'"),
        )
    else:
        report.ok("venue_url is a valid http(s) URL")


def check_is_limited_run(ctx: _Ctx) -> None:
    """is_limited_run must be a boolean (True / False)."""
    df, report = ctx.df, ctx.report
    if "is_limited_run" not in df.columns:
        return

    valid = {True, False, "True", "False", "true", "false", "1", "0", 1, 0}
    bad = [
        (i + 2, str(v))
        for i, v in df["is_limited_run"].items()
        if not _is_empty(v) and v not in valid
    ]
    if bad:
        report.fail(
            "is_limited_run is boolean",
            "Invalid value(s): " + _examples(bad, lambda x: f"row {x[0]}: '{x[1]}'"),
        )
    else:
        report.ok("is_limited_run is boolean")


def check_capacity(ctx: _Ctx) -> None:
    """Capacity must be a non-negative integer (0 allowed; negative is not)."""
    df, report = ctx.df, ctx.report
    if "capacity" not in df.columns:
        return

    col = df["capacity"]
    populated = ~col.apply(_is_empty)
    numeric = pd.to_numeric(col, errors="coerce")
    bad_mask = populated & (numeric.isna() | (numeric < 0))

    if bad_mask.any():
        bad = [(i + 2, str(col.iat[i])) for i in df.index[bad_mask]]
        report.fail(
            "Capacity is a non-negative integer",
            f"{len(bad)} invalid value(s): "
            + _examples(bad, lambda x: f"row {x[0]}: '{x[1]}'"),
        )
    else:
        report.ok("Capacity is a non-negative integer")


def check_capacity_currency_required(ctx: _Ctx) -> None:
    """Currency and capacity rules vs. seat_pricing:
    Capacity:
      seat_pricing has real seat data  → must be populated (FAIL if missing)
      seat_pricing is sold-out only    → WARN if missing
        (all values are empty lists, e.g. {'YYYY-MM-DD HH:MM': []})
      seat_pricing is empty ({})       → may be populated (unreserved-venue
                                         exception) or blank
    Currency:
      may be blank or populated regardless of seat_pricing
      (unreserved venues return seat_pricing={} but still have a currency)
    """
    df, report = ctx.df, ctx.report
    if "capacity" not in df.columns:
        return

    missing_capacity: list[int] = []
    sold_out_missing: list[int] = []
    for i in range(len(df)):
        pricing = ctx.parsed_seat_pricing[i]
        if not (isinstance(pricing, dict) and pricing):
            continue
        if not _is_empty(df["capacity"].iat[i]):
            continue
        has_real_seats = any(
            isinstance(v, list) and len(v) > 0 for v in pricing.values()
        )
        if has_real_seats:
            missing_capacity.append(i + 2)
        else:
            sold_out_missing.append(i + 2)

    check_name = "Capacity matches seat_pricing presence"
    if missing_capacity:
        report.fail(
            check_name,
            f"{len(missing_capacity)} row(s) missing capacity despite having a "
            f"seat map: rows {missing_capacity[:5]}"
            f"{'…' if len(missing_capacity) > 5 else ''}",
        )
    elif sold_out_missing:
        report.warn(
            check_name,
            f"{len(sold_out_missing)} row(s) missing capacity with sold-out "
            f"seat_pricing (empty seat lists): rows {sold_out_missing[:5]}"
            f"{'…' if len(sold_out_missing) > 5 else ''}",
        )
    else:
        report.ok(check_name, "Capacity populated whenever seat_pricing has data")


# ─────────────────────────────────────────────────────────────────────────────
# Date checks
# ─────────────────────────────────────────────────────────────────────────────


def check_date_fields(ctx: _Ctx) -> None:
    """Validate date and datetime columns are parseable and in the right format."""
    df, report = ctx.df, ctx.report
    FIELD_FORMATS = (
        ("open_date", DATE_YYYYMMDD),
        ("close_date", DATE_YYYYMMDD),
        ("booking_start_date", DATE_YYYYMMDD),
        ("booking_end_date", DATE_YYYYMMDD),
        ("scrape_datetime", DATETIME_KEY),
    )

    unparseable: dict[str, list] = defaultdict(list)
    wrong_format: dict[str, list] = defaultdict(list)

    for field, fmt_re in FIELD_FORMATS:
        if field not in df.columns:
            continue
        for i, raw in df[field].items():
            if _is_empty(raw):
                continue
            val = str(raw).strip()
            if _try_parse_datetime(val) is None:
                unparseable[field].append((i + 2, val))
            elif not fmt_re.match(val):
                wrong_format[field].append((i + 2, val))

    if unparseable:
        lines = [
            f"[{field}] " + _examples(rows, lambda x: f"row {x[0]}: '{x[1]}'")
            for field, rows in unparseable.items()
        ]
        report.fail(
            "Date fields are valid dates",
            "Unparseable value(s):\n" + "\n".join(f"  {ln}" for ln in lines),
        )
    else:
        report.ok(
            "Date fields are valid dates", "All date/datetime values are parseable"
        )

    if wrong_format:
        lines = [
            f"[{field}] " + _examples(rows, lambda x: f"row {x[0]}: '{x[1]}'")
            for field, rows in wrong_format.items()
        ]
        report.fail(
            "Date fields use expected format",
            "Values are valid dates but not in the expected format.\n"
            "  Date fields should be YYYY-MM-DD; datetime fields YYYY-MM-DD HH:MM\n"
            + "\n".join(f"  {ln}" for ln in lines),
        )
    else:
        report.ok(
            "Date fields use expected format",
            "All dates use YYYY-MM-DD; all datetimes use YYYY-MM-DD HH:MM",
        )

    _check_date_order(
        ctx,
        "open_date",
        "close_date",
        "open_date is not after close_date",
        lambda x: f"row {x[0]}: open={x[1]} close={x[2]}",
    )
    _check_date_order(
        ctx,
        "booking_start_date",
        "booking_end_date",
        "booking_start_date is not after booking_end_date",
        lambda x: f"row {x[0]}: start={x[1]} end={x[2]}",
    )


def _check_date_order(
    ctx: _Ctx,
    start_field: str,
    end_field: str,
    check_name: str,
    fmt: Callable,
) -> None:
    df, report = ctx.df, ctx.report
    if start_field not in df.columns or end_field not in df.columns:
        return

    bad: list[tuple] = []
    for i in range(len(df)):
        s = _try_parse_datetime(df[start_field].iat[i])
        e = _try_parse_datetime(df[end_field].iat[i])
        if s and e and e < s:
            bad.append((i + 2, str(df[start_field].iat[i]), str(df[end_field].iat[i])))
    if bad:
        report.fail(check_name, _examples(bad, fmt))
    else:
        report.ok(check_name)


def check_dates_within_performance_boundary(ctx: _Ctx) -> None:
    """open_date / close_date must bound the upcoming_performances dates."""
    df, report = ctx.df, ctx.report
    if "upcoming_performances" not in df.columns:
        return
    if "open_date" not in df.columns and "close_date" not in df.columns:
        return

    open_after_first: list[tuple] = []
    close_before_last: list[tuple] = []

    for i, dates in enumerate(ctx.perf_dates):
        if not dates:
            continue
        first_perf = min(dates)
        last_perf = max(dates)

        if "open_date" in df.columns:
            open_d = _try_parse_date(df["open_date"].iat[i])
            if open_d is not None and open_d > first_perf:
                open_after_first.append(
                    (i + 2, str(df["open_date"].iat[i]).strip(), str(first_perf))
                )

        if "close_date" in df.columns:
            close_d = _try_parse_date(df["close_date"].iat[i])
            if close_d is not None and close_d < last_perf:
                close_before_last.append(
                    (i + 2, str(df["close_date"].iat[i]).strip(), str(last_perf))
                )

    if open_after_first:
        report.fail(
            "open_date is not after earliest upcoming performance",
            f"{len(open_after_first)} row(s) where open_date is after the first "
            f"upcoming performance: "
            + _examples(
                open_after_first,
                lambda x: f"row {x[0]}: open={x[1]} first_perf={x[2]}",
            ),
        )
    else:
        report.ok("open_date is not after earliest upcoming performance")

    if close_before_last:
        report.fail(
            "close_date is not before latest upcoming performance",
            f"{len(close_before_last)} row(s) where close_date is before the last "
            f"upcoming performance: "
            + _examples(
                close_before_last,
                lambda x: f"row {x[0]}: close={x[1]} last_perf={x[2]}",
            ),
        )
    else:
        report.ok("close_date is not before latest upcoming performance")


def check_no_collapsed_date_ranges(ctx: _Ctx) -> None:
    """Flag rows where open_date == close_date despite upcoming_performances
    spanning more than one date. A real one-night-only show is allowed to have
    equal start/end values.
    """
    df, report = ctx.df, ctx.report
    if "upcoming_performances" not in df.columns:
        return
    if "open_date" not in df.columns or "close_date" not in df.columns:
        return

    open_close_collapsed: list[tuple] = []
    for i, dates in enumerate(ctx.perf_dates):
        if len(dates) <= 1:
            continue
        open_d = _try_parse_date(df["open_date"].iat[i])
        close_d = _try_parse_date(df["close_date"].iat[i])
        if open_d and close_d and open_d == close_d:
            open_close_collapsed.append(
                (i + 2, str(df["open_date"].iat[i]).strip(), len(dates))
            )

    if open_close_collapsed:
        report.fail(
            "open_date and close_date differ for multi-date shows",
            f"{len(open_close_collapsed)} row(s) where open_date == close_date "
            f"despite performances spanning multiple dates: "
            + _examples(
                open_close_collapsed,
                lambda x: f"row {x[0]}: both='{x[1]}', {x[2]} performance dates",
            ),
        )
    else:
        report.ok("open_date and close_date differ for multi-date shows")


def check_single_day_open_close_equal(ctx: _Ctx) -> None:
    """For rows with exactly one performance date, open_date and close_date
    must both equal that date — a single-performance show has no date range.
    """
    df, report = ctx.df, ctx.report
    if (
        "open_date" not in df.columns
        or "close_date" not in df.columns
        or "upcoming_performances" not in df.columns
    ):
        return

    mismatched: list[tuple] = []
    for i, dates in enumerate(ctx.perf_dates):
        if len(dates) != 1:
            continue
        perf_d = _try_parse_date(next(iter(dates)))
        open_d = _try_parse_date(df["open_date"].iat[i])
        close_d = _try_parse_date(df["close_date"].iat[i])
        if not (perf_d and open_d and close_d):
            continue
        if open_d != perf_d or close_d != perf_d:
            mismatched.append(
                (
                    i + 2,
                    str(df["open_date"].iat[i]).strip(),
                    str(df["close_date"].iat[i]).strip(),
                    str(perf_d),
                )
            )

    check_name = (
        "Single-performance shows have open_date == close_date == performance date"
    )
    if mismatched:
        report.warn(
            check_name,
            f"{len(mismatched)} single-performance row(s) where open_date and "
            f"close_date do not both equal the performance date: "
            + _examples(
                mismatched,
                lambda x: (
                    f"row {x[0]}: open='{x[1]}', close='{x[2]}', performance='{x[3]}'"
                ),
            ),
        )
    else:
        report.ok(check_name)


def check_seat_count_vs_capacity(ctx: _Ctx) -> None:
    """WARN when seat count is implausibly small relative to capacity.

    A handful of seat entries against a capacity in the hundreds usually means
    the scraper captured price tiers rather than individual seats.
    """
    df, report = ctx.df, ctx.report
    if "seat_pricing" not in df.columns or "capacity" not in df.columns:
        return

    suspicious: list[tuple] = []
    for i, pricing in enumerate(ctx.parsed_seat_pricing):
        if not isinstance(pricing, dict) or not pricing:
            continue
        try:
            capacity = int(float(df["capacity"].iat[i]))
        except (TypeError, ValueError):
            continue
        if capacity < SEAT_COUNT_MIN_CAPACITY:
            continue
        for perf_dt, seats in pricing.items():
            if not isinstance(seats, list):
                continue
            n = len(seats)
            if n and n < capacity * SEAT_COUNT_MIN_RATIO:
                suspicious.append((i + 2, perf_dt, n, capacity))

    if suspicious:
        report.warn(
            "Seat count plausible vs. capacity",
            f"{len(suspicious)} performance(s) with seat count far below capacity "
            f"(<{int(SEAT_COUNT_MIN_RATIO * 100)}%) — likely price tiers, not seats: "
            + _examples(
                suspicious,
                lambda x: f"row {x[0]} ({x[1]}): {x[2]} seats vs capacity {x[3]}",
            ),
        )
    else:
        report.ok(
            "Seat count plausible vs. capacity",
            "Seat counts are consistent with venue capacity",
        )


def check_performances_have_seat_pricing(ctx: _Ctx) -> None:
    """Every upcoming_performances entry must have a matching seat_pricing key."""
    df, report = ctx.df, ctx.report
    if "upcoming_performances" not in df.columns or "seat_pricing" not in df.columns:
        return

    missing: list[tuple] = []
    for i in range(len(df)):
        perfs = ctx.parsed_perfs[i]
        pricing = ctx.parsed_seat_pricing[i]
        if not isinstance(perfs, list) or not perfs:
            continue

        if not isinstance(pricing, dict) or not pricing:
            continue

        pricing_keys = {str(k).strip() for k in pricing.keys()}
        for entry in perfs:
            if not isinstance(entry, dict):
                continue
            d = str(entry.get("date", "")).strip()
            t = str(entry.get("time", "")).strip()
            if not d or not t:
                continue
            key = f"{d} {t}"
            if key not in pricing_keys:
                missing.append((i + 2, key))

    if missing:
        report.fail(
            "Performances have matching seat_pricing entry",
            f"{len(missing)} performance(s) in upcoming_performances with no "
            f"matching seat_pricing key: "
            + _examples(missing, lambda x: f"row {x[0]}: '{x[1]}'"),
        )
    else:
        report.ok(
            "Performances have matching seat_pricing entry",
            "Every upcoming performance has a corresponding seat_pricing key",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cross-row checks
# ─────────────────────────────────────────────────────────────────────────────


def check_cross_row_consistency(ctx: _Ctx) -> None:
    """For rows sharing a venue_url, key fields should be populated in all rows
    or none — a mix suggests a scraping bug."""
    df, report = ctx.df, ctx.report
    if "venue_url" not in df.columns:
        return

    fields_to_check = ["address", "city", "country", "currency", "capacity"]
    issues: dict[str, list[str]] = defaultdict(list)

    for url, group in df.groupby("venue_url"):
        for field in fields_to_check:
            if field not in df.columns:
                continue
            has_value = group[field].apply(lambda v: not _is_empty(v))
            if has_value.any() and not has_value.all():
                missing_rows = [r + 2 for r in group.index[~has_value].tolist()]
                issues[field].append(
                    f"'{url}': {len(missing_rows)} row(s) missing "
                    f"(rows {missing_rows[:3]}{'…' if len(missing_rows) > 3 else ''})"
                )

    if issues:
        lines = [
            f"[{field}] " + "; ".join(details[:2]) + ("…" if len(details) > 2 else "")
            for field, details in issues.items()
        ]
        report.warn(
            "Cross-row field consistency",
            "Some rows are missing fields that other rows for the same venue have:\n"
            + "\n".join(f"  {line}" for line in lines),
        )
    else:
        report.ok(
            "Cross-row field consistency",
            "All fields consistent across rows for each venue",
        )


def check_venue_capacity_consistency(ctx: _Ctx) -> None:
    """If any row for a venue name has capacity, every row for that venue must."""
    df, report = ctx.df, ctx.report
    if "venue" not in df.columns or "capacity" not in df.columns:
        return

    missing: list[str] = []
    venue_key = df["venue"].apply(
        lambda v: str(v).strip().lower() if not _is_empty(v) else ""
    )
    for venue, group in df.groupby(venue_key):
        if not venue:
            continue
        has_value = group["capacity"].apply(lambda v: not _is_empty(v))
        if has_value.any() and not has_value.all():
            blank_rows = [r + 2 for r in group.index[~has_value].tolist()]
            missing.append(
                f"'{venue}': {len(blank_rows)} row(s) missing capacity "
                f"(rows {blank_rows[:5]}{'…' if len(blank_rows) > 5 else ''})"
            )

    if missing:
        report.warn(
            "Venue capacity completeness",
            f"{len(missing)} venue(s) where some rows have capacity and others don't: "
            + "; ".join(missing[:3])
            + ("…" if len(missing) > 3 else ""),
        )
    else:
        report.ok(
            "Venue capacity completeness",
            "Every row for a venue has capacity whenever any row does",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────


# Order matters for readability of the rendered report; group similar checks.
_CHECKS: tuple[Callable[[_Ctx], None], ...] = (
    # Schema
    check_columns,
    check_column_order,
    check_required_fields_not_empty,
    # Content
    check_one_row_per_show_venue,
    check_categories,
    check_seat_ids,
    check_seat_map_duplicated_across_performances,
    # Format / date
    check_date_fields,
    check_quote_style,
    check_upcoming_performances_format,
    check_dates_within_performance_boundary,
    check_no_collapsed_date_ranges,
    check_single_day_open_close_equal,
    check_seat_pricing_format,
    check_seat_count_vs_capacity,
    check_performances_have_seat_pricing,
    check_currency_format,
    check_url_format,
    check_is_limited_run,
    check_capacity,
    check_capacity_currency_required,
    # Cross-row
    check_cross_row_consistency,
    check_venue_capacity_consistency,
)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Strip column whitespace and reset to a positional integer index."""
    return df.rename(columns=str.strip).reset_index(drop=True)


def _round_trip_to_string(df: pd.DataFrame) -> pd.DataFrame:
    """Serialize and reparse so cells match what would land on disk / GCS.

    Format checks need the same string representation a reader would see, so we
    round-trip through CSV with `dtype=str`.
    """
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    return _normalize(pd.read_csv(buffer, dtype=str))


def _run_checks(ctx: _Ctx) -> Report:
    for check in _CHECKS:
        check(ctx)
    return ctx.report


def validate_dataframe(df: pd.DataFrame, print_report: bool = False) -> Report:
    """Run all checks against an in-memory DataFrame.

    Round-trips `df` through CSV with `dtype=str` so format checks fire
    correctly even when called on a typed DataFrame from the pipeline.
    """
    ctx = _Ctx(_round_trip_to_string(df))
    report = _run_checks(ctx)
    if print_report:
        report.print_summary()
    return report


def validate_csv(path: str) -> bool:
    """Load a CSV from disk and run all checks. Returns True if no failures."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: File not found: {path}")
        return False

    print(f"\nValidating: {p.name}  ({p.stat().st_size // 1024} KB)")
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        # Python engine is more tolerant of malformed CSVs.
        try:
            df = pd.read_csv(path, dtype=str, engine="python", on_bad_lines="warn")
        except Exception as e:
            print(f"ERROR: Could not parse CSV — {e}")
            return False

    df = _normalize(df)
    print(f"Loaded {len(df)} row(s), {len(df.columns)} column(s)\n")

    # Already string-typed from the read; no need for the round-trip.
    return _run_checks(_Ctx(df)).print_summary()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/utils/csv_validator.py <path/to/output.csv>")
        sys.exit(1)
    passed = validate_csv(sys.argv[1])
    sys.exit(0 if passed else 1)
