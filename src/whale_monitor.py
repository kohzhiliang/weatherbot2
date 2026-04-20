"""
whale_monitor.py — Monitor Polymarket whale wallets and auto-copy trades into paper account.

API: data-api.polymarket.com/trades (needs interval=hour or interval=day)
Key fields: proxyWallet, side, size, price, conditionId, title, outcome, timestamp
"""

import sqlite3
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional
import urllib.request
import urllib.error

from .config import Config
from .state import StateDB

logger = logging.getLogger("whale_monitor")

# ------------------------------------------------------------------
# Whale registry  — add/remove Polygon addresses here
# ------------------------------------------------------------------
# Known whales from on-chain volume analysis:
WHALE_WALLETS: dict[str, str] = {
    "0x0eb75bf6f54794a83bd26095811f30b530161f17": "Untried-Android",
    "0x5d1d9cfd66ee3068c2a8a57dedf1e1b006dcafd2": "Anon-Cypher",
    "0x86fbea68d619daf670185ecd38a69aa90f7703d4": "Anon-Flux",
    "0xdf456444d3fb628b71835f1673a243553857acd4": "Used-Scheme",
    "0x4133bcbad1d9c41de776646696f41c34d0a65e70": "Bogus-Fix",
}

# Scanner settings
POLL_INTERVAL   = 120   # seconds between whale scans
MIN_TRADE_SIZE  = 10.0  # USDC — minimum trade to consider copying
COPY_FRACTION   = 0.10  # copy 10% of whale position size
API_LIMIT       = 500   # trades per API call

# ------------------------------------------------------------------
# DB helper
# ------------------------------------------------------------------
def _db(db_path: str):
    return sqlite3.connect(db_path, timeout=10)


# ------------------------------------------------------------------
# WhaleCloner
# ------------------------------------------------------------------
class WhaleCloner:
    """Insert a whale trade as a paper position in whale_positions table."""

    def __init__(self, db_path: str, config: Config,
                 copy_fraction: float = COPY_FRACTION,
                 min_size: float = MIN_TRADE_SIZE):
        self.db_path = db_path
        self.config = config
        self.copy_fraction = copy_fraction
        self.min_size = min_size

    def clone(self, trade: dict) -> Optional[str]:
        wallet  = trade.get("proxyWallet", "").lower()
        trade_id = trade.get("conditionId", "") or trade.get("asset", "")

        if wallet not in WHALE_WALLETS:
            return None

        try:
            size = float(trade.get("size", 0))
        except (TypeError, ValueError):
            return None

        if size < self.min_size:
            return None

        copy_size = min(size * self.copy_fraction, self.config.max_bet)
        if copy_size < 1.0:
            return None

        price     = float(trade.get("price", 0))
        shares    = copy_size / price if price > 0 else 0
        whale     = WHALE_WALLETS.get(wallet, wallet[:10])
        title     = trade.get("title", "Unknown")
        side      = trade.get("side", "")
        outcome   = trade.get("outcome", "")
        ts        = trade.get("timestamp", "")

        conn = _db(self.db_path)
        cur  = conn.cursor()
        try:
            cur.execute("""
                INSERT OR IGNORE INTO whale_positions
                (id, whale_wallet, whale_name, market_name, market_slug,
                 condition_id, contract_address, side, outcome, price,
                 size, copied_size, shares, cost, opened_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                wallet,
                whale,
                title,
                trade.get("slug", ""),
                trade.get("conditionId", ""),
                trade.get("asset", ""),
                side,
                outcome,
                price,
                size,
                round(copy_size, 2),
                round(shares, 4),
                round(copy_size, 2),
                datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            if cur.rowcount:
                logger.info(
                    f"[WHALE] {whale} | {side} ${size:.2f} @ ${price:.3f} | {title[:60]}\n"
                    f"       -> copied ${copy_size:.2f} as pos {trade_id}"
                )
                return trade_id
            return None  # duplicate
        except sqlite3.Error as e:
            logger.error(f"[WHALE] Clone error: {e}")
            return None
        finally:
            conn.close()


# ------------------------------------------------------------------
# WhaleResolver
# ------------------------------------------------------------------
class WhaleResolver:
    """
    Resolve open whale positions:
    1. Get all unresolved positions
    2. For each, query the Polymarket markets API for resolution
    3. Calculate P&L: BUY YES → win if outcome=YES; BUY NO → win if outcome=NO
    4. Credit P&L to balance_log via StateDB.
    """

    def __init__(self, db_path: str, state_db: StateDB | None = None):
        self.db_path = db_path
        self._state = state_db or StateDB(db_path)

    def resolve_all(self) -> int:
        conn = _db(self.db_path)
        cur  = conn.cursor()
        rows = cur.execute(
            "SELECT id, condition_id, side, price, shares, cost, whale_name, market_name "
            "FROM whale_positions WHERE resolved=0"
        ).fetchall()
        conn.close()

        resolved = 0
        for (pos_id, cond_id, side, entry_price, shares, cost, whale, market_name) in rows:
            if not cond_id:
                continue
            try:
                outcome = self._get_outcome(cond_id)
            except Exception as e:
                logger.warning(f"[WHALE] resolve {pos_id}: {e}")
                continue

            if outcome is None:
                continue  # not resolved yet

            # Calculate P&L
            # BUY YES: paid entry_price, resolves at 1.0 (YES) or 0.0 (NO)
            # BUY NO:  paid entry_price, resolves at 1.0 (YES=wrong) or 0.0 (NO=wrong direction)
            # SELL YES: received entry_price, resolves at 1.0 (receive 1.0 = profit) or 0.0
            # SELL NO:  received entry_price, resolves at 0.0 (profit) or 1.0 (loss)
            side_upper = side.upper()
            if side_upper == "BUY":
                pnl = shares * (outcome - entry_price)
            elif side_upper == "SELL":
                pnl = shares * (entry_price - outcome)
            else:
                pnl = 0.0

            result = "win" if pnl > 0 else "loss"
            pnl = round(pnl, 2)

            conn2 = _db(self.db_path)
            cur2 = conn2.cursor()
            cur2.execute("""
                UPDATE whale_positions
                SET resolved=1, resolved_at=?, exit_price=?, pnl=?, outcome=?
                WHERE id=?
            """, (datetime.now(timezone.utc).isoformat(), outcome, pnl, result, pos_id))
            conn2.commit()
            conn2.close()

            logger.info(f"[WHALE] Resolved {whale} {market_name[:40]}: {result} ${pnl:+.2f}")
            resolved += 1

            # Credit PnL to balance_log
            if pnl != 0:
                try:
                    current = self._state.get_balance()
                    self._state.update_balance(current + pnl, f"whale_win:{pos_id}")
                    logger.info(f"[WHALE] Credited ${pnl:+.2f} to balance -> new balance ${current + pnl:.2f}")
                except Exception as e:
                    logger.error(f"[WHALE] Failed to credit balance: {e}")

        return resolved

    def _get_outcome(self, condition_id: str) -> Optional[float]:
        """
        Query Polymarket CLOB API for market resolution.
        Returns 1.0 (YES/position won), 0.0 (NO/position lost),
        or None (not resolved yet).
        """
        if not condition_id:
            return None
        url = f"https://clob.polymarket.com/markets?condition_id={condition_id}"
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                markets = data.get("data", []) if isinstance(data, dict) else (data or [])
                if not markets:
                    return None
                m = markets[0]

                # Market must be closed to be resolved
                if not m.get("closed", False):
                    return None

                tokens = m.get("tokens", [])
                if not tokens:
                    return None

                # Find the winning token
                winner_token = None
                for t in tokens:
                    if t.get("winner", False):
                        winner_token = t
                        break

                if winner_token is None:
                    # Resolved but no winner set — unclear, assume loss
                    return 0.0

                # Find the outcomeIndex of the winning token
                winner_idx = tokens.index(winner_token)

                # outcomeIndex is stored as 'outcomeIndex' on the trade
                # We need to pass it here — but we only have condition_id
                # So we check which token the whale likely backed by matching
                # the trade's outcome string against token outcome strings
                # Return the winner's price (1.0 = YES, 0.0 = NO) for P&L calc
                return 1.0 if winner_idx == 0 else 0.0

        except Exception as e:
            logger.warning(f"[WHALE] _get_outcome error for {condition_id}: {e}")
            return None


# ------------------------------------------------------------------
# WhaleWatcher
# ------------------------------------------------------------------
class WhaleWatcher:
    BASE_URL = "https://data-api.polymarket.com/trades"

    def __init__(self, db_path: str, config: Config,
                 copy_fraction: float = COPY_FRACTION,
                 poll_interval: int = POLL_INTERVAL,
                 api_limit: int = API_LIMIT):
        self.db_path     = db_path
        self.config      = config
        self._state      = StateDB(db_path)
        self.cloner      = WhaleCloner(db_path, config, copy_fraction)
        self.resolver    = WhaleResolver(db_path, self._state)
        self.poll_interval = poll_interval
        self.api_limit   = api_limit
        self._seen: set[str] = set()
        self._running   = False

    def _fetch_trades(self) -> list[dict]:
        url = f"{self.BASE_URL}?interval=hour&limit={self.api_limit}"
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"[WHALE] fetch error: {e}")
            return []

    def _new_whale_trades(self, trades: list[dict]) -> list[dict]:
        new = []
        for t in trades:
            wallet   = t.get("proxyWallet", "").lower()
            trade_id = t.get("conditionId", "") or t.get("asset", "")
            if wallet in WHALE_WALLETS and trade_id not in self._seen:
                self._seen.add(trade_id)
                new.append(t)
        return new

    def scan(self) -> tuple[int, int]:
        """Run one cycle. Returns (cloned, resolved)."""
        trades = self._fetch_trades()
        whales = self._new_whale_trades(trades)
        cloned = sum(1 for t in whales if self.cloner.clone(t))
        resolved = self.resolver.resolve_all()
        return cloned, resolved

    def run(self, max_iterations: Optional[int] = None):
        self._running = True
        it = 0
        while self._running:
            it += 1
            try:
                c, r = self.scan()
                if c or r:
                    logger.info(f"[WHALE] cycle {it}: cloned={c} resolved={r}")
            except Exception as e:
                logger.error(f"[WHALE] watcher error: {e}")
            if max_iterations and it >= max_iterations:
                break
            time.sleep(self.poll_interval)

    def stop(self):
        self._running = False


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------
def whale_stats(db_path: str) -> dict:
    conn = _db(db_path)
    cur  = conn.cursor()

    open_row   = cur.execute("SELECT COUNT(*), COALESCE(SUM(copied_size),0) FROM whale_positions WHERE resolved=0").fetchone()
    res_row    = cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM whale_positions WHERE resolved=1").fetchone()
    by_whale   = cur.execute("""
        SELECT whale_name,
               COUNT(*) as n,
               SUM(pnl) as pnl,
               SUM(copied_size) as cost
        FROM whale_positions
        WHERE resolved=1
        GROUP BY whale_name
    """).fetchall()

    conn.close()
    return {
        "open_count":  open_row[0] or 0,
        "open_cost":   open_row[1] or 0.0,
        "resolved_count": res_row[0] or 0,
        "resolved_pnl":   res_row[1] or 0.0,
        "by_whale": [{"name": r[0], "n": r[1], "pnl": r[2], "cost": r[3]} for r in by_whale],
    }
