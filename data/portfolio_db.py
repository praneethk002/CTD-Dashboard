"""
data/portfolio_db.py

SQLite persistence for the demo Fixed Income Performance & Analytics
Workbench. Deliberately a SEPARATE database file (portfolio_analytics.db)
and a separate class from data/db.py:BasisDB — this keeps the new portfolio
demo fully independent of the existing CTD basis-monitor data. Resetting or
reseeding one has zero effect on the other.

Two tables:
  pf_securities — one row per demo security (static reference data:
                  coupon, maturity, classification, weights)
  pf_snapshots  — one row per security per day (price, yield, accrued
                  interest, and precomputed duration/convexity/DV01)

Design principle (mirrors data/db.py): this module is the only place that
touches portfolio_analytics.db. All other layers (Flask API) go through
PortfolioDB methods — no raw SQL outside this file.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).parent.parent / "portfolio_analytics.db"


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


class PortfolioDB:

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pf_securities (
                    security_id       TEXT PRIMARY KEY,
                    label             TEXT NOT NULL,
                    coupon            REAL,
                    maturity          TEXT NOT NULL,
                    classification    TEXT,
                    region            TEXT NOT NULL DEFAULT 'US',
                    portfolio_weight  REAL NOT NULL,
                    benchmark_weight  REAL,
                    par_amount        REAL
                );

                CREATE TABLE IF NOT EXISTS pf_snapshots (
                    snapshot_dt        TEXT NOT NULL,
                    security_id        TEXT NOT NULL,
                    price              REAL NOT NULL,
                    yield              REAL,
                    accrued_interest   REAL NOT NULL,
                    modified_duration  REAL NOT NULL,
                    convexity          REAL NOT NULL,
                    dv01               REAL NOT NULL,
                    UNIQUE(snapshot_dt, security_id)
                );

                CREATE INDEX IF NOT EXISTS idx_pf_snapshots_dt
                    ON pf_snapshots(snapshot_dt);

                CREATE INDEX IF NOT EXISTS idx_pf_snapshots_sec
                    ON pf_snapshots(security_id, snapshot_dt);
            """)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write_security(self, security: dict):
        self.write_securities([security])

    def write_securities(self, securities: list[dict]):
        rows = [
            (
                s["security_id"], s["label"], s.get("coupon"),
                s["maturity"].isoformat() if hasattr(s["maturity"], "isoformat") else s["maturity"],
                s.get("classification"), s.get("region", "US"),
                s["portfolio_weight"], s.get("benchmark_weight"), s.get("par_amount"),
            )
            for s in securities
        ]
        with self._connect() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO pf_securities
                    (security_id, label, coupon, maturity, classification,
                     region, portfolio_weight, benchmark_weight, par_amount)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, rows)

    def write_snapshot(self, snapshot: dict):
        self.write_snapshots_bulk([snapshot])

    def write_snapshots_bulk(self, snapshots: list[dict]):
        rows = [
            (
                s["date"].isoformat() if hasattr(s["date"], "isoformat") else s["date"],
                s["security_id"], s["price"], s.get("yield"), s["accrued_interest"],
                s["modified_duration"], s["convexity"], s["dv01"],
            )
            for s in snapshots
        ]
        with self._connect() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO pf_snapshots
                    (snapshot_dt, security_id, price, yield, accrued_interest,
                     modified_duration, convexity, dv01)
                VALUES (?,?,?,?,?,?,?,?)
            """, rows)

    # ------------------------------------------------------------------
    # Reads — securities
    # ------------------------------------------------------------------

    def get_securities(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pf_securities ORDER BY security_id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["maturity"] = _parse_date(d["maturity"])
            out.append(d)
        return out

    def get_security(self, security_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pf_securities WHERE security_id = ?", (security_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["maturity"] = _parse_date(d["maturity"])
        return d

    # ------------------------------------------------------------------
    # Reads — snapshots
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_snapshot(row) -> dict:
        d = dict(row)
        d["date"] = _parse_date(d.pop("snapshot_dt"))
        return d

    def get_snapshot(self, security_id: str, on: date) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pf_snapshots WHERE security_id = ? AND snapshot_dt = ?",
                (security_id, on.isoformat()),
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def get_snapshot_all(self, on: date) -> dict[str, dict]:
        """All securities' snapshots on one date, keyed by security_id."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pf_snapshots WHERE snapshot_dt = ?", (on.isoformat(),)
            ).fetchall()
        return {r["security_id"]: self._row_to_snapshot(r) for r in rows}

    def get_history(self, security_id: str, start: date, end: date) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM pf_snapshots
                WHERE security_id = ? AND snapshot_dt BETWEEN ? AND ?
                ORDER BY snapshot_dt ASC
            """, (security_id, start.isoformat(), end.isoformat())).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_history_all(self, start: date, end: date) -> dict[str, list[dict]]:
        """All securities' history in a date range, keyed by security_id, each chronological."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM pf_snapshots
                WHERE snapshot_dt BETWEEN ? AND ?
                ORDER BY security_id ASC, snapshot_dt ASC
            """, (start.isoformat(), end.isoformat())).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            snap = self._row_to_snapshot(r)
            out.setdefault(snap["security_id"], []).append(snap)
        return out

    def get_latest_date(self) -> Optional[date]:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(snapshot_dt) AS d FROM pf_snapshots").fetchone()
        return _parse_date(row["d"]) if row and row["d"] else None

    def get_earliest_date(self) -> Optional[date]:
        with self._connect() as conn:
            row = conn.execute("SELECT MIN(snapshot_dt) AS d FROM pf_snapshots").fetchone()
        return _parse_date(row["d"]) if row and row["d"] else None

    def _all_dates(self) -> list[date]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT snapshot_dt FROM pf_snapshots ORDER BY snapshot_dt ASC").fetchall()
        return [_parse_date(r["snapshot_dt"]) for r in rows]

    def resolve_horizon_dates(self, horizon: str, as_of: Optional[date] = None) -> tuple[date, date]:
        """
        Map a horizon ("1D"|"MTD"|"QTD"|"YTD") to the nearest available
        (begin_date, end_date) pair given the actually-seeded snapshot dates.

        end_date defaults to the latest available snapshot date.
        begin_date is the latest available date on/before the horizon's
        calendar boundary (or, for "1D", the prior available trading date).
        """
        from core.returns import horizon_start_date

        dates = self._all_dates()
        if not dates:
            raise ValueError("No snapshots available — run data.portfolio_seed first")

        end_date = as_of or dates[-1]
        available_on_or_before_end = [d for d in dates if d <= end_date]
        if not available_on_or_before_end:
            raise ValueError(f"No snapshots available on or before {end_date}")
        end_date = available_on_or_before_end[-1]

        if horizon == "1D":
            prior = [d for d in available_on_or_before_end if d < end_date]
            if not prior:
                raise ValueError("Not enough history for a 1D return")
            return prior[-1], end_date

        boundary = horizon_start_date(horizon, end_date)
        on_or_before_boundary = [d for d in dates if d <= boundary]
        begin_date = on_or_before_boundary[-1] if on_or_before_boundary else dates[0]
        return begin_date, end_date

    def reset(self):
        with self._connect() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS pf_securities;
                DROP TABLE IF EXISTS pf_snapshots;
            """)
        self._init_schema()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
