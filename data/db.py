"""
data/db.py

SQLite persistence layer for the CTD Basis Monitor.

Two tables:
  basis_snapshots — one row per bond per day, all computed metrics
  ctd_log         — records every CTD transition with spread at time of switch

Design principle:
  This module is the only place in the codebase that touches SQLite.
  All other layers (MCP server, Flask API) go through BasisDB methods.
  No raw SQL outside this file.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

# default database location — project root
DEFAULT_DB_PATH = Path(__file__).parent.parent / "basis_monitor.db"


class BasisDB:

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS basis_snapshots (
                    snapshot_dt       TEXT NOT NULL,
                    contract          TEXT NOT NULL,
                    cusip             TEXT NOT NULL,
                    label             TEXT,
                    coupon            REAL,
                    maturity          TEXT,
                    cash_price        REAL,
                    futures_price     REAL,
                    conv_factor       REAL,
                    gross_basis       REAL,
                    net_basis         REAL,
                    net_basis_ticks   REAL,
                    implied_repo      REAL,
                    dv01              REAL,
                    is_ctd            INTEGER,
                    repo_rate         REAL,
                    days_to_delivery  INTEGER,
                    UNIQUE(snapshot_dt, contract, cusip)
                );

                CREATE TABLE IF NOT EXISTS ctd_log (
                    change_dt               TEXT NOT NULL,
                    contract                TEXT NOT NULL,
                    prev_ctd_cusip          TEXT,
                    new_ctd_cusip           TEXT,
                    implied_repo_spread_bps REAL
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_dt_contract
                    ON basis_snapshots(snapshot_dt, contract);

                CREATE INDEX IF NOT EXISTS idx_snapshots_cusip
                    ON basis_snapshots(cusip, contract, snapshot_dt);
            """)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write_snapshot(
        self,
        snapshot_dt: date,
        contract: str,
        basket_df,
        futures_price: float,
        repo_rate: float,
        days_to_delivery: int,
    ):
        """
        Write a full basket snapshot for one day.

        Also auto-detects CTD transitions by comparing today's CTD
        against the previous day's CTD. If a switch is detected,
        writes a record to ctd_log.

        Args:
            snapshot_dt:      date of this snapshot
            contract:         futures contract e.g. "TYM26"
            basket_df:        DataFrame from core.ctd.rank_basket()
            futures_price:    futures price used for this snapshot
            repo_rate:        repo rate used for this snapshot
            days_to_delivery: calendar days from snapshot_dt to delivery
        """
        dt_str = snapshot_dt.isoformat()

        rows = [
            (
                dt_str,
                contract,
                row["cusip"],
                row["label"],
                row["coupon"],
                row["maturity"],
                row["cash_price"],
                futures_price,
                row["conv_factor"],
                row["gross_basis"],
                row["net_basis"],
                row["net_basis_ticks"],
                row["implied_repo_pct"] / 100,   # store as decimal
                row["dv01"],
                int(row["is_ctd"]),
                repo_rate,
                days_to_delivery,
            )
            for _, row in basket_df.iterrows()
        ]

        with self._connect() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO basis_snapshots
                    (snapshot_dt, contract, cusip, label, coupon, maturity,
                     cash_price, futures_price, conv_factor, gross_basis,
                     net_basis, net_basis_ticks, implied_repo, dv01,
                     is_ctd, repo_rate, days_to_delivery)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)

        # check for CTD transition
        self._detect_and_log_transition(snapshot_dt, contract, basket_df)

    def _detect_and_log_transition(self, snapshot_dt: date, contract: str, basket_df):
        """
        Compare today's CTD to the previous day's CTD.
        If different, write a record to ctd_log.
        """
        today_ctd = basket_df[basket_df["is_ctd"]].iloc[0]["cusip"]

        prev = self.get_latest_ctd(contract, before=snapshot_dt)

        if prev is None or prev["cusip"] == today_ctd:
            return  # no transition

        # compute implied repo spread at time of switch
        runner = basket_df[~basket_df["is_ctd"]].iloc[0]
        ctd    = basket_df[basket_df["is_ctd"]].iloc[0]
        spread_bps = (ctd["implied_repo_pct"] - runner["implied_repo_pct"]) * 100

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO ctd_log
                    (change_dt, contract, prev_ctd_cusip, new_ctd_cusip, implied_repo_spread_bps)
                VALUES (?,?,?,?,?)
            """, (
                snapshot_dt.isoformat(),
                contract,
                prev["cusip"],
                today_ctd,
                round(spread_bps, 2),
            ))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_latest_snapshot(self, contract: str) -> list[dict]:
        """
        Return the most recent full basket snapshot for a contract.

        Returns list of dicts, one per bond, sorted by implied_repo descending.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT *
                FROM   basis_snapshots
                WHERE  contract    = ?
                  AND  snapshot_dt = (
                      SELECT MAX(snapshot_dt)
                      FROM   basis_snapshots
                      WHERE  contract = ?
                  )
                ORDER BY implied_repo DESC
            """, (contract, contract)).fetchall()

        return [dict(r) for r in rows]

    def get_basis_history(
        self,
        cusip: str,
        contract: str,
        days: int = 90,
    ) -> list[dict]:
        """
        Return net_basis_ticks history for a specific bond over the last N days.

        Returns list of dicts with snapshot_dt and net_basis_ticks,
        sorted ascending by date.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT   snapshot_dt,
                         net_basis_ticks,
                         implied_repo
                FROM     basis_snapshots
                WHERE    cusip      = ?
                  AND    contract   = ?
                ORDER BY snapshot_dt DESC
                LIMIT    ?
            """, (cusip, contract, days)).fetchall()

        # reverse to chronological order
        return [dict(r) for r in reversed(rows)]

    def get_basis_percentile(self, contract: str, days: int = 90) -> dict:
        """
        Return where today's CTD net basis sits in its N-day distribution.

        Percentile rank: what fraction of historical values are BELOW today's value.
        A low percentile = historically tight basis (bond cheap vs futures).
        """
        history = self._get_ctd_basis_history(contract, days)

        if not history:
            return {}

        values       = [r["net_basis_ticks"] for r in history]
        today_value  = values[-1]
        percentile   = sum(v < today_value for v in values) / len(values) * 100

        return {
            "today_net_basis_ticks": round(today_value, 3),
            "percentile_rank":       round(percentile, 1),
            "min_90d":               round(min(values), 3),
            "max_90d":               round(max(values), 3),
            "mean_90d":              round(sum(values) / len(values), 3),
        }

    def get_transition_proximity(self, contract: str) -> dict:
        """
        Return the implied repo spread between CTD and runner-up.

        Uses a ROW_NUMBER CTE to reliably identify CTD and runner-up
        from the latest snapshot.

        Returns spread in bps, trend (NARROWING/WIDENING/STABLE), and risk flag.
        """
        with self._connect() as conn:
            # use ROW_NUMBER to rank bonds by implied_repo on the latest date
            rows = conn.execute("""
                WITH latest AS (
                    SELECT MAX(snapshot_dt) AS max_dt
                    FROM   basis_snapshots
                    WHERE  contract = ?
                ),
                ranked AS (
                    SELECT  s.*,
                            ROW_NUMBER() OVER (
                                ORDER BY s.implied_repo DESC
                            ) AS rn
                    FROM    basis_snapshots s
                    JOIN    latest ON s.snapshot_dt = latest.max_dt
                    WHERE   s.contract = ?
                )
                SELECT cusip, label, implied_repo, rn
                FROM   ranked
                WHERE  rn <= 2
                ORDER BY rn
            """, (contract, contract)).fetchall()

        if len(rows) < 2:
            return {}

        ctd    = dict(rows[0])
        runner = dict(rows[1])

        current_spread_bps = (ctd["implied_repo"] - runner["implied_repo"]) * 10_000

        # trend: compare to spread 5 days ago
        trend     = self._compute_spread_trend(contract, current_spread_bps)
        risk_flag = self._risk_flag(current_spread_bps)

        return {
            "ctd_cusip":           ctd["cusip"],
            "ctd_label":           ctd["label"],
            "runner_cusip":        runner["cusip"],
            "runner_label":        runner["label"],
            "current_spread_bps":  round(current_spread_bps, 2),
            "trend":               trend,
            "risk_flag":           risk_flag,
        }

    def get_ctd_transitions(self, contract: str) -> list[dict]:
        """Return the full CTD transition log for a contract."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT *
                FROM   ctd_log
                WHERE  contract = ?
                ORDER BY change_dt DESC
            """, (contract,)).fetchall()

        return [dict(r) for r in rows]

    def get_latest_ctd(self, contract: str, before: Optional[date] = None) -> Optional[dict]:
        """
        Return the most recent CTD bond before a given date.
        Used for transition detection.
        """
        dt_filter = before.isoformat() if before else "9999-12-31"

        with self._connect() as conn:
            row = conn.execute("""
                SELECT cusip, label, implied_repo
                FROM   basis_snapshots
                WHERE  contract    = ?
                  AND  is_ctd      = 1
                  AND  snapshot_dt < ?
                ORDER BY snapshot_dt DESC
                LIMIT  1
            """, (contract, dt_filter)).fetchone()

        return dict(row) if row else None

    def reset(self):
        """Drop and recreate all tables. Used by seed script."""
        with self._connect() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS basis_snapshots;
                DROP TABLE IF EXISTS ctd_log;
            """)
        self._init_schema()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_ctd_basis_history(self, contract: str, days: int) -> list[dict]:
        """Return net_basis_ticks history for the CTD bond over last N days."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT snapshot_dt, net_basis_ticks
                FROM   basis_snapshots
                WHERE  contract = ?
                  AND  is_ctd   = 1
                ORDER BY snapshot_dt DESC
                LIMIT  ?
            """, (contract, days)).fetchall()

        return [dict(r) for r in reversed(rows)]

    def _compute_spread_trend(self, contract: str, current_spread_bps: float) -> str:
        """
        Compare current spread to the spread 5 days ago.
        NARROWING if tightened > 2bps, WIDENING if widened > 2bps, else STABLE.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                WITH latest AS (
                    SELECT MAX(snapshot_dt) AS max_dt
                    FROM   basis_snapshots
                    WHERE  contract = ?
                ),
                five_days_ago AS (
                    SELECT snapshot_dt
                    FROM   basis_snapshots
                    WHERE  contract    = ?
                      AND  snapshot_dt < (SELECT max_dt FROM latest)
                    ORDER BY snapshot_dt DESC
                    LIMIT  1
                    OFFSET 4
                ),
                ranked_old AS (
                    SELECT  s.implied_repo,
                            ROW_NUMBER() OVER (ORDER BY s.implied_repo DESC) AS rn
                    FROM    basis_snapshots s
                    JOIN    five_days_ago ON s.snapshot_dt = five_days_ago.snapshot_dt
                    WHERE   s.contract = ?
                )
                SELECT implied_repo FROM ranked_old WHERE rn <= 2 ORDER BY rn
            """, (contract, contract, contract)).fetchall()

        if len(rows) < 2:
            return "STABLE"

        old_spread_bps = (rows[0]["implied_repo"] - rows[1]["implied_repo"]) * 10_000
        delta = current_spread_bps - old_spread_bps

        if delta < -2:
            return "NARROWING"
        elif delta > 2:
            return "WIDENING"
        return "STABLE"

    @staticmethod
    def _risk_flag(spread_bps: float) -> str:
        """
        Classify transition risk based on implied repo spread.
          CRITICAL  — spread < 5bps  → CTD switch imminent
          ELEVATED  — 5-15bps        → heightened monitoring
          LOW       — > 15bps        → no near-term risk
        """
        if spread_bps < 5:
            return "CRITICAL"
        elif spread_bps < 15:
            return "ELEVATED"
        return "LOW"

    @contextmanager
    def _connect(self):
        """Context manager for SQLite connections with row factory."""
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
