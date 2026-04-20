"""SQLite-backed persistent state — positions, balance, calibration, history."""
import sqlite3, json, logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

class StateDB:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY, city TEXT, date TEXT,
                    bucket_low REAL, bucket_high REAL,
                    entry_price REAL, shares REAL, cost REAL,
                    p REAL, ev REAL, kelly REAL,
                    forecast_temp REAL, forecast_src TEXT,
                    opened_at TEXT, status TEXT DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS resolved (
                    id TEXT PRIMARY KEY, city TEXT, date TEXT,
                    bucket_low REAL, bucket_high REAL,
                    entry_price REAL, exit_price REAL, shares REAL, cost REAL,
                    resolved_outcome TEXT, pnl REAL, resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS calibration (
                    city TEXT, source TEXT, sigma REAL, n INTEGER, updated_at TEXT,
                    PRIMARY KEY (city, source)
                );
                CREATE TABLE IF NOT EXISTS balance_log (
                    ts TEXT, balance REAL, delta REAL, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS nws_bias (
                    city TEXT,
                    month INTEGER,
                    nws_error_sum REAL,
                    nws_error_n INTEGER,
                    nws_biased_sum REAL,
                    nws_biased_n INTEGER,
                    updated_at TEXT,
                    PRIMARY KEY (city, month)
                );
            """)
            # Migration: add nws_forecast column if it doesn't exist
            try:
                conn.execute("ALTER TABLE positions ADD COLUMN nws_forecast REAL")
            except sqlite3.OperationalError:
                pass  # column already exists

    # ---- balance ----
    def get_balance(self) -> float:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT balance FROM balance_log ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else 0.0

    def update_balance(self, new_balance: float, reason: str):
        ts = datetime.now(timezone.utc).isoformat()
        delta = new_balance - self.get_balance()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO balance_log (ts, balance, delta, reason) VALUES (?, ?, ?, ?)",
                (ts, round(new_balance, 2), round(delta, 4), reason)
            )

    # ---- positions ----
    def open_position(self, pos: dict) -> bool:
        """Returns True if opened, False if duplicate or insufficient balance."""
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT status FROM positions WHERE id=? AND status='open'",
                (pos["id"],)
            ).fetchone()
            if existing:
                log.warning("Duplicate position rejected: %s", pos["id"])
                return False
            conn.execute("""
                INSERT INTO positions
                (id, city, date, bucket_low, bucket_high, entry_price, shares, cost,
                 p, ev, kelly, forecast_temp, forecast_src, nws_forecast, opened_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """, (
                pos["id"], pos["city"], pos["date"],
                pos["bucket_low"], pos["bucket_high"],
                pos["entry_price"], pos["shares"], pos["cost"],
                pos["p"], pos["ev"], pos["kelly"],
                pos["forecast_temp"], pos["forecast_src"],
                pos.get("nws_forecast"),
                datetime.now(timezone.utc).isoformat()
            ))
        bal = self.get_balance()
        self.update_balance(bal - pos["cost"], f"open:{pos['id']}")
        log.info("Opened position %s cost=%.2f new_balance=%.2f", pos["id"], pos["cost"], bal - pos["cost"])
        return True

    def close_position(self, pos_id: str, exit_price: float, reason: str):
        with sqlite3.connect(self.db_path) as conn:
            pos = conn.execute(
                "SELECT * FROM positions WHERE id=? AND status='open'", (pos_id,)
            ).fetchone()
            if not pos:
                log.warning("Cannot close position %s: not found or already closed", pos_id)
                return
            cols = [desc[0] for desc in conn.execute("SELECT * FROM positions WHERE 1=0").description]
            pos_dict = dict(zip(cols, pos))

            shares = pos_dict["shares"]
            cost = pos_dict["cost"]
            entry = pos_dict["entry_price"]
            pnl = round((exit_price - entry) * shares, 2)
            new_balance = self.get_balance() + cost + pnl
            self.update_balance(new_balance, f"close:{reason}:{pos_id}")

            conn.execute(
                "UPDATE positions SET status='closed' WHERE id=?", (pos_id,)
            )
            conn.execute("""
                INSERT INTO resolved
                (id, city, date, bucket_low, bucket_high, entry_price, exit_price,
                 shares, cost, resolved_outcome, pnl, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pos_id, pos_dict["city"], pos_dict["date"],
                pos_dict["bucket_low"], pos_dict["bucket_high"],
                entry, exit_price, shares, cost,
                "win" if pnl > 0 else "loss", pnl,
                datetime.now(timezone.utc).isoformat()
            ))
        log.info("Closed position %s exit=%.3f pnl=%s", pos_id, exit_price, pnl)

    def get_open_positions(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
            cols = [desc[0] for desc in conn.execute("SELECT * FROM positions WHERE 1=0").description]
            return [dict(zip(cols, r)) for r in rows]

    def get_resolved(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM resolved ORDER BY resolved_at DESC LIMIT ?", (limit,)
            ).fetchall()
            cols = [desc[0] for desc in conn.execute("SELECT * FROM resolved WHERE 1=0").description]
            return [dict(zip(cols, r)) for r in rows]

    # ---- calibration ----
    def get_calibration(self, city: str, source: str) -> Optional[float]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT sigma FROM calibration WHERE city=? AND source=?",
                (city, source)
            ).fetchone()
        return row[0] if row else None

    def update_calibration(self, city: str, source: str, sigma: float, n: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO calibration (city, source, sigma, n, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(city, source) DO UPDATE SET sigma=excluded.sigma,
                    n=excluded.n, updated_at=excluded.updated_at
            """, (city, source, sigma, n, datetime.now(timezone.utc).isoformat()))

    # ---- NWS bias tracking ----
    def record_nws_bias(self, city: str, actual_temp: float,
                        nws_forecast: float | None, model_forecast: float | None):
        """
        Record forecast error after market resolution.
        - nws_error = actual - nws_forecast (positive = NWS underforecasts, NWS has cold bias)
        - model_error = actual - model_forecast (positive = model underforecasts)
        Monthly buckets capture seasonal bias shifts.
        """
        if nws_forecast is None:
            return
        month = datetime.now(timezone.utc).month
        nws_error = actual_temp - nws_forecast
        model_error = 0.0
        if model_forecast is not None:
            model_error = actual_temp - model_forecast
        ts = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            # Upsert NWS error
            conn.execute("""
                INSERT INTO nws_bias (city, month, nws_error_sum, nws_error_n, nws_biased_sum, nws_biased_n, updated_at)
                VALUES (?, ?, ?, 1, ?, 1, ?)
                ON CONFLICT(city, month) DO UPDATE SET
                    nws_error_sum = nws_error_sum + excluded.nws_error_sum,
                    nws_error_n = nws_error_n + 1,
                    nws_biased_sum = nws_biased_sum + excluded.nws_biased_sum,
                    nws_biased_n = nws_biased_n + 1,
                    updated_at = excluded.updated_at
            """, (city, month, nws_error, model_error, ts))

    def get_nws_bias(self, city: str, month: int | None = None) -> dict | None:
        """Get average NWS bias for city. If month not specified, use current month."""
        if month is None:
            month = datetime.now(timezone.utc).month
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT nws_error_sum, nws_error_n, nws_biased_sum, nws_biased_n "
                "FROM nws_bias WHERE city=? AND month=?",
                (city, month)
            ).fetchone()
        if not row or row[1] == 0:
            return None
        return {
            "nws_error_mean": round(row[0] / row[1], 2),
            "nws_error_n": row[1],
            "model_error_mean": round(row[2] / row[3], 2) if row[3] > 0 else None,
            "model_error_n": row[3],
        }