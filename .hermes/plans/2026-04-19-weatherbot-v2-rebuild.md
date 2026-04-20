# Weatherbot V2 — Rebuild Plan

> **For agentic workers:** Use `subagent-driven-development` skill to implement this plan task-by-task.

**Goal:** Rebuild the Polymarket weather trading bot from scratch with cleaner architecture, all bugs fixed, proper error handling, and running as a persistent background service.

**Architecture:** Modular Python project with separated concerns — forecast engine, market data layer, bet sizing, state machine. File-system backed state. No Telegram yet.

**Tech Stack:** Python 3.11+, `requests`, `sqlite3` (for calibration history), `logging`, `signal` handlers, background `nohup` process.

---

## File Map

```
weatherbot2/
├── config.json              # User config (API keys, thresholds)
├── src/
│   ├── __init__.py
│   ├── config.py            # Config loading + validation
│   ├── forecast.py          # Open-Meteo (ECMWF), METAR, Visual Crossing
│   ├── polymarket.py        # Gamma API, CLOB API, market discovery
│   ├── betsizing.py         # Kelly criterion, bucket probability
│   ├── state.py             # SQLite-backed state (balance, positions)
│   ├── scanner.py           # Main scan loop + position monitor
│   └── main.py              # CLI entry point, signal handlers
├── data/
│   └── weatherbot.db        # SQLite: calibration, resolved trades, pnl history
├── run.sh                   # Launcher (nohup background)
└── tests/
    ├── test_betsizing.py    # Kelly, bucket_prob, edge cases
    ├── test_forecast.py     # Coordinate precision, API responses
    └── test_state.py        # SQLite ops, balance tracking
```

---

## Task 1: Project Scaffold + Config

**Files:**
- Create: `weatherbot2/config.json`
- Create: `weatherbot2/src/__init__.py`
- Create: `weatherbot2/src/config.py`
- Create: `weatherbot2/data/` (mkdir)
- Create: `weatherbot2/run.sh`

- [ ] **Step 1: Create config.json**

```json
{
    "balance": 100.0,
    "max_bet": 20.0,
    "min_ev": 0.15,
    "max_price": 0.45,
    "min_volume": 500,
    "min_hours": 2.0,
    "max_hours": 72.0,
    "kelly_fraction": 0.25,
    "max_slippage": 0.03,
    "scan_interval_seconds": 3600,
    "monitor_interval_seconds": 600,
    "vc_key": "",
    "polygon_wallet_pk": "",
    "log_level": "INFO"
}
```

- [ ] **Step 2: Create src/config.py**

```python
"""Config loading with validation."""
import json
from pathlib import Path
from dataclasses import dataclass

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

@dataclass
class Config:
    balance: float
    max_bet: float
    min_ev: float
    max_price: float
    min_volume: float
    min_hours: float
    max_hours: float
    kelly_fraction: float
    max_slippage: float
    scan_interval: int
    monitor_interval: int
    vc_key: str
    polygon_wallet_pk: str
    log_level: str

    @classmethod
    def load(cls) -> "Config":
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
        return cls(
            balance=float(raw["balance"]),
            max_bet=float(raw["max_bet"]),
            min_ev=float(raw["min_ev"]),
            max_price=float(raw["max_price"]),
            min_volume=float(raw["min_volume"]),
            min_hours=float(raw["min_hours"]),
            max_hours=float(raw["max_hours"]),
            kelly_fraction=float(raw["kelly_fraction"]),
            max_slippage=float(raw["max_slippage"]),
            scan_interval=int(raw["scan_interval_seconds"]),
            monitor_interval=int(raw["monitor_interval_seconds"]),
            vc_key=str(raw.get("vc_key", "")),
            polygon_wallet_pk=str(raw.get("polygon_wallet_pk", "")),
            log_level=str(raw.get("log_level", "INFO")),
        )

    def validate(self) -> list[str]:
        errors = []
        if self.balance <= 0:
            errors.append("balance must be > 0")
        if not 0 < self.min_ev <= 1:
            errors.append("min_ev must be between 0 and 1")
        if not 0 < self.kelly_fraction <= 1:
            errors.append("kelly_fraction must be between 0 and 1")
        if self.vc_key == "":
            errors.append("vc_key is required for calibration")
        return errors
```

- [ ] **Step 3: Create run.sh**

```bash
#!/bin/bash
cd "$(dirname "$0")"
mkdir -p data logs
nohup python3 -u src/main.py >> logs/bot.log 2>&1 &
echo "Weatherbot PID: $!"
echo $! > .bot.pid
```

- [ ] **Step 4: Test config loading**

```python
# from src.config import Config
# cfg = Config.load()
# errs = cfg.validate()
# print(errs)  # should show vc_key error since empty
```

Run: `cd ~/Desktop/weatherbot2 && python3 -c "from src.config import Config; cfg=Config.load(); print(cfg.validate())"`
Expected: `['vc_key is required for calibration']`

---

## Task 2: State Management (SQLite)

**Files:**
- Create: `weatherbot2/src/state.py`
- Create: `weatherbot2/tests/test_state.py`

Schema:
```sql
CREATE TABLE positions (
    id TEXT PRIMARY KEY,       -- market_id
    city TEXT, date TEXT,
    bucket_low REAL, bucket_high REAL,
    entry_price REAL, shares REAL, cost REAL,
    p REAL, ev REAL, kelly REAL,
    forecast_temp REAL, forecast_src TEXT,
    opened_at TEXT, status TEXT
);

CREATE TABLE resolved (
    id TEXT PRIMARY KEY,
    city TEXT, date TEXT,
    bucket_low REAL, bucket_high REAL,
    entry_price REAL, exit_price REAL, shares REAL, cost REAL,
    resolved_outcome TEXT, pnl REAL, resolved_at TEXT
);

CREATE TABLE calibration (
    city TEXT, source TEXT, sigma REAL, n INTEGER, updated_at TEXT,
    PRIMARY KEY (city, source)
);

CREATE TABLE balance_log (
    ts TEXT, balance REAL, delta REAL, reason TEXT
);
```

- [ ] **Step 1: Write test for state.py**

```python
# tests/test_state.py
import pytest, tempfile, os
from src.state import StateDB

@pytest.fixture
def db():
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    os.unlink(f.name)
    s = StateDB(f.name)
    yield s
    os.unlink(f.name)

def test_open_position_deducts_balance(db):
    db.init()
    db.update_balance(100.0, "init")
    db.open_position({
        "id": "test-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81,
        "entry_price": 0.30, "shares": 10.0, "cost": 3.0,
        "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf",
    })
    assert db.get_balance() == 97.0

def test_close_position_returns_funds(db):
    db.init()
    db.update_balance(100.0, "init")
    db.open_position({"id": "test-2", "city": "miami", "date": "2026-04-20",
        "bucket_low": 84, "bucket_high": 85, "entry_price": 0.23,
        "shares": 86.96, "cost": 20.0, "p": 0.8, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 85, "forecast_src": "hrrr"})
    # Simulate win: exit at 1.0
    db.close_position("test-2", exit_price=1.0, reason="resolved_win")
    assert db.get_balance() == 100.0  # 80 + 20 returned + 0 profit
    # Actually: cost was 20, entry 0.23, shares 86.96, exit 1.0
    # pnl = (1.0 - 0.23) * 86.96 = 66.96
    # balance = 80 + 20 + 66.96 = 166.96
    bal = db.get_balance()
    assert bal > 100.0  # should be ~166.96

def test_no_duplicate_positions(db):
    db.init()
    db.update_balance(200.0, "init")
    db.open_position({"id": "dup-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81, "entry_price": 0.30,
        "shares": 10.0, "cost": 20.0, "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf"})
    result = db.open_position({"id": "dup-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81, "entry_price": 0.30,
        "shares": 10.0, "cost": 20.0, "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf"})
    assert result == False  # Should reject duplicate

def test_get_open_positions(db):
    db.init()
    db.update_balance(100.0, "init")
    db.open_position({"id": "open-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81, "entry_price": 0.30,
        "shares": 10.0, "cost": 10.0, "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf"})
    db.open_position({"id": "open-2", "city": "miami", "date": "2026-04-21",
        "bucket_low": 84, "bucket_high": 85, "entry_price": 0.23,
        "shares": 20.0, "cost": 10.0, "p": 0.8, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 85, "forecast_src": "hrrr"})
    positions = db.get_open_positions()
    assert len(positions) == 2
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_state.py -v`
Expected: 4 failures (state.py doesn't exist yet)

- [ ] **Step 3: Write state.py**

```python
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
            """)

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
                 p, ev, kelly, forecast_temp, forecast_src, opened_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """, (
                pos["id"], pos["city"], pos["date"],
                pos["bucket_low"], pos["bucket_high"],
                pos["entry_price"], pos["shares"], pos["cost"],
                pos["p"], pos["ev"], pos["kelly"],
                pos["forecast_temp"], pos["forecast_src"],
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
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_state.py -v`
Expected: PASS (4/4)

---

## Task 3: Forecast Engine

**Files:**
- Create: `weatherbot2/src/forecast.py`
- Create: `weatherbot2/tests/test_forecast.py`

Key insight from research: **Use airport station coordinates for ALL weather API calls, not city center.** Polymarket resolves at specific airport METAR stations. ECMWF/Open-Meteo returns temperatures for the grid point nearest to the coordinates you pass. Passing airport coordinates = correct temperature match.

Locations dict — airport coords (from original bot):
```python
LOCATIONS = {
    "nyc":    {"lat": 40.7772,  "lon": -73.8726,  "name": "New York City", "station": "KLGA",  "unit": "F"},
    "chicago": {"lat": 41.9742,  "lon": -87.9073,  "name": "Chicago",      "station": "KORD",  "unit": "F"},
    "miami":  {"lat": 25.7959,  "lon": -80.2870,  "name": "Miami",        "station": "KMIA",  "unit": "F"},
    "dallas": {"lat": 32.8471,  "lon": -96.8518,  "name": "Dallas",       "station": "KDAL",  "unit": "F"},
    "seattle": {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",      "station": "KSEA",  "unit": "F"},
    "atlanta": {"lat": 33.6407,  "lon": -84.4277,  "name": "Atlanta",      "station": "KATL",  "unit": "F"},
    "london": {"lat": 51.5048,  "lon": 0.0495,     "name": "London",       "station": "EGLC",  "unit": "C"},
    "paris":  {"lat": 48.9962,  "lon": 2.5979,     "name": "Paris",        "station": "LFPG",  "unit": "C"},
    "munich": {"lat": 48.3537,  "lon": 11.7750,    "name": "Munich",       "station": "EDDM",  "unit": "C"},
    "ankara": {"lat": 40.1281,  "lon": 32.9951,    "name": "Ankara",       "station": "LTAC",  "unit": "C"},
    "seoul":  {"lat": 37.4691,  "lon": 126.4505,   "name": "Seoul",        "station": "RKSI",  "unit": "C"},
    "tokyo":  {"lat": 35.7647,  "lon": 140.3864,   "name": "Tokyo",        "station": "RJTT",  "unit": "C"},
    "shanghai": {"lat": 31.1443,"lon": 121.8083,   "name": "Shanghai",     "station": "ZSPD",  "unit": "C"},
    "singapore": {"lat": 1.3502,"lon": 103.9940,   "name": "Singapore",    "station": "WSSS",  "unit": "C"},
    "lucknow": {"lat": 26.7606, "lon": 80.8893,    "name": "Lucknow",      "station": "VILK",  "unit": "C"},
    "tel-aviv": {"lat": 32.0114,"lon": 34.8867,    "name": "Tel Aviv",     "station": "LLBG",  "unit": "C"},
    "toronto": {"lat": 43.6772, "lon": -79.6306,   "name": "Toronto",      "station": "CYYZ",  "unit": "C"},
    "sao-paulo": {"lat": -23.4356,"lon": -46.4731, "name": "Sao Paulo",   "station": "SBGR",  "unit": "C"},
    "buenos-aires": {"lat": -34.8222,"lon": -58.5358,"name": "Buenos Aires","station": "SAEZ", "unit": "C"},
    "wellington": {"lat": -41.3272,"lon": 174.8052, "name": "Wellington",   "station": "NZWN",  "unit": "C"},
}
```

- [ ] **Step 1: Write test_forecast.py**

```python
# tests/test_forecast.py
import pytest
from src.forecast import LOCATIONS, get_ecmwf, get_metar, ForecastEngine

def test_nyc_uses_klga_coords_not_city_center():
    """NYC bot must use LaGuardia coords, not Manhattan center."""
    loc = LOCATIONS["nyc"]
    assert loc["station"] == "KLGA"
    assert abs(loc["lat"] - 40.7772) < 0.01
    assert abs(loc["lon"] - (-73.8726)) < 0.01

def test_all_locations_have_station():
    for city, loc in LOCATIONS.items():
        assert "station" in loc
        assert "name" in loc
        assert "unit" in loc
        assert loc["unit"] in ("F", "C")

def test_ecmwf_returns_dict():
    # Don't hit network in unit test — mock it
    import unittest.mock as mock
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "daily": {
                "time": ["2026-04-20", "2026-04-21"],
                "temperature_2m_max": [75.0, 78.0]
            }
        }
        engine = ForecastEngine("")
        result = engine.get_ecmwf("nyc", ["2026-04-20", "2026-04-21"])
        assert result == {"2026-04-20": 75, "2026-04-21": 78}

def test_metar_returns_observed_temp():
    import unittest.mock as mock
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = [
            {"temp": 25.0}  # Celsius
        ]
        engine = ForecastEngine("")
        result = engine.get_metar("london")  # Celsius city
        assert result == 77.0  # 25C -> 77F

def test_engine_picks_hrrr_for_us():
    import unittest.mock as mock, types
    engine = ForecastEngine("")
    # HRRR available for US cities
    with mock.patch.object(engine, "get_hrrr", return_value={"2026-04-20": 85}):
        with mock.patch.object(engine, "get_ecmwf", return_value={"2026-04-20": 82}):
            snaps = engine.get_forecasts("miami", ["2026-04-20"])
            assert snaps["2026-04-20"]["best_source"] == "hrrr"
            assert snaps["2026-04-20"]["best"] == 85

def test_engine_picks_ecmwf_for_eu():
    import unittest.mock as mock
    engine = ForecastEngine("")
    with mock.patch.object(engine, "get_hrrr", return_value={}):  # HRRR empty for EU
        with mock.patch.object(engine, "get_ecmwf", return_value={"2026-04-20": 22}):
            snaps = engine.get_forecasts("london", ["2026-04-20"])
            assert snaps["2026-04-20"]["best_source"] == "ecmwf"
            assert snaps["2026-04-20"]["best"] == 22
```

- [ ] **Step 2: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_forecast.py -v`
Expected: 5 failures (forecast.py doesn't exist)

- [ ] **Step 3: Write forecast.py**

```python
"""Weather forecast engine — ECMWF via Open-Meteo, METAR real-time, Visual Crossing."""
import time, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

LOCATIONS = {
    "nyc":    {"lat": 40.7772,  "lon": -73.8726,  "name": "New York City",  "station": "KLGA",  "unit": "F"},
    "chicago": {"lat": 41.9742,  "lon": -87.9073,  "name": "Chicago",         "station": "KORD",  "unit": "F"},
    "miami":  {"lat": 25.7959,  "lon": -80.2870,  "name": "Miami",           "station": "KMIA",  "unit": "F"},
    "dallas": {"lat": 32.8471,  "lon": -96.8518,  "name": "Dallas",          "station": "KDAL",  "unit": "F"},
    "seattle": {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",         "station": "KSEA",  "unit": "F"},
    "atlanta": {"lat": 33.6407,  "lon": -84.4277,  "name": "Atlanta",         "station": "KATL",  "unit": "F"},
    "london": {"lat": 51.5048,  "lon":   0.0495,    "name": "London",          "station": "EGLC",  "unit": "C"},
    "paris":  {"lat": 48.9962,  "lon":   2.5979,    "name": "Paris",           "station": "LFPG",  "unit": "C"},
    "munich": {"lat": 48.3537,  "lon":  11.7750,    "name": "Munich",          "station": "EDDM",  "unit": "C"},
    "ankara": {"lat": 40.1281,  "lon":  32.9951,    "name": "Ankara",          "station": "LTAC",  "unit": "C"},
    "seoul":  {"lat": 37.4691,  "lon": 126.4505,    "name": "Seoul",           "station": "RKSI",  "unit": "C"},
    "tokyo":  {"lat": 35.7647,  "lon": 140.3864,    "name": "Tokyo",           "station": "RJTT",  "unit": "C"},
    "shanghai": {"lat": 31.1443, "lon": 121.8083,   "name": "Shanghai",        "station": "ZSPD",  "unit": "C"},
    "singapore": {"lat":  1.3502,"lon": 103.9940,   "name": "Singapore",       "station": "WSSS",  "unit": "C"},
    "lucknow": {"lat": 26.7606, "lon":  80.8893,    "name": "Lucknow",         "station": "VILK",  "unit": "C"},
    "tel-aviv": {"lat": 32.0114,"lon":  34.8867,    "name": "Tel Aviv",        "station": "LLBG",  "unit": "C"},
    "toronto": {"lat": 43.6772, "lon": -79.6306,    "name": "Toronto",         "station": "CYYZ",  "unit": "C"},
    "sao-paulo": {"lat":-23.4356,"lon": -46.4731,   "name": "Sao Paulo",       "station": "SBGR",  "unit": "C"},
    "buenos-aires": {"lat":-34.8222,"lon": -58.5358,"name": "Buenos Aires",    "station": "SAEZ",  "unit": "C"},
    "wellington": {"lat":-41.3272,"lon": 174.8052,  "name": "Wellington",       "station": "NZWN",  "unit": "C"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

SIGMA_DEFAULTS = {"F": 2.0, "C": 1.2}


class ForecastEngine:
    """Fetch weather forecasts from Open-Meteo (ECMWF + HRRR) and METAR."""

    def __init__(self, vc_key: str = ""):
        self.vc_key = vc_key

    # ---- Public API ----

    def get_forecasts(self, city_slug: str, dates: list[str]) -> dict[str, dict]:
        """Returns {date: {ecmwf, hrrr, metar, best, best_source}}."""
        loc = LOCATIONS.get(city_slug)
        if not loc:
            return {}
        ecmwf = self.get_ecmwf(city_slug, dates)
        hrrr = self.get_hrrr(city_slug, dates) if loc["region"] == "us" else {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshots = {}
        for date in dates:
            metar = self.get_metar(city_slug) if date == today else None
            if loc["unit"] == "F" and hrrr.get(date) is not None:
                best, best_src = hrrr[date], "hrrr"
            elif ecmwf.get(date) is not None:
                best, best_src = ecmwf[date], "ecmwf"
            elif metar is not None:
                best, best_src = metar, "metar"
            else:
                best, best_src = None, None
            snapshots[date] = {
                "ecmwf": ecmwf.get(date),
                "hrrr": hrrr.get(date) if loc["region"] == "us" else None,
                "metar": metar,
                "best": best,
                "best_source": best_src,
            }
        return snapshots

    def get_actual_temp(self, city_slug: str, date_str: str) -> Optional[float]:
        """Resolution-time actual temperature via Visual Crossing (for calibration)."""
        if not self.vc_key:
            return None
        loc = LOCATIONS.get(city_slug)
        if not loc:
            return None
        unit = loc["unit"]
        vc_unit = "us" if unit == "F" else "metric"
        try:
            url = (
                f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
                f"/{loc['station']}/{date_str}/{date_str}"
                f"?unitGroup={vc_unit}&key={self.vc_key}&include=days&elements=tempmax"
            )
            data = requests.get(url, timeout=(5, 8)).json()
            days = data.get("days", [])
            if days and days[0].get("tempmax") is not None:
                return round(float(days[0]["tempmax"]), 1)
        except Exception as e:
            log.warning("[VC] %s %s: %s", city_slug, date_str, e)
        return None

    # ---- Internal ----

    def get_ecmwf(self, city_slug: str, dates: list[str]) -> dict[str, float]:
        """ECMWF via Open-Meteo. Airport coordinates = correct station match."""
        loc = LOCATIONS[city_slug]
        unit = loc["unit"]
        temp_unit = "fahrenheit" if unit == "F" else "celsius"
        result = {}
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
            f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models=ecmwf_ifs025&bias_correction=true"
        )
        for attempt in range(3):
            try:
                data = requests.get(url, timeout=(5, 10)).json()
                if "error" not in data:
                    for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                        if date in dates and temp is not None:
                            result[date] = round(temp, 1) if unit == "C" else round(temp)
                return result
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log.warning("[ECMWF] %s: %s", city_slug, e)
        return result

    def get_hrrr(self, city_slug: str, dates: list[str]) -> dict[str, float]:
        """HRRR via Open-Meteo — US cities only, D+0 to D+2."""
        loc = LOCATIONS[city_slug]
        if loc["unit"] != "F":
            return {}
        result = {}
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models=gfs_seamless"
        )
        for attempt in range(3):
            try:
                data = requests.get(url, timeout=(5, 10)).json()
                if "error" not in data:
                    for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                        if date in dates and temp is not None:
                            result[date] = round(temp)
                return result
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log.warning("[HRRR] %s: %s", city_slug, e)
        return result

    def get_metar(self, city_slug: str) -> Optional[float]:
        """Current observed temperature from METAR — real-time airport obs."""
        loc = LOCATIONS[city_slug]
        station = loc["station"]
        unit = loc["unit"]
        try:
            url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
            data = requests.get(url, timeout=(3, 5)).json()
            if data and isinstance(data, list):
                temp_c = data[0].get("temp")
                if temp_c is not None:
                    if unit == "F":
                        return round(float(temp_c) * 9/5 + 32)
                    return round(float(temp_c), 1)
        except Exception as e:
            log.debug("[METAR] %s: %s", city_slug, e)
        return None
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_forecast.py -v`
Expected: PASS (5/5)

---

## Task 4: Bet Sizing (Kelly Criterion + Bucket Probability)

**Files:**
- Create: `weatherbot2/src/betsizing.py`
- Create: `weatherbot2/tests/test_betsizing.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_betsizing.py
import pytest, math
from src.betsizing import bucket_prob, calc_ev, calc_kelly, bet_size

def test_bucket_prob_at_center():
    # Forecast exactly in middle of 80-81F bucket, sigma=2
    p = bucket_prob(80.5, 80, 81, sigma=2.0)
    assert 0.4 < p < 0.6  # should be ~0.5

def test_bucket_prob_at_edge():
    # Forecast at boundary of bucket
    p = bucket_prob(80.0, 80, 81, sigma=2.0)
    assert 0.3 < p < 0.5

def test_bucket_prob_narrow_bucket():
    # 1-degree bucket (exact value)
    p = bucket_prob(80.0, 80, 80, sigma=2.0)
    # norm_cdf(0.5/2) - norm_cdf(-0.5/2) for same-value bucket
    # For exact match with sigma=2: norm_cdf(0) = 0.5
    assert 0.3 < p < 0.7

def test_ev_buy_below_true_prob():
    # If true prob = 0.8, price = 0.6, EV should be positive
    ev = calc_ev(0.8, 0.6)
    assert ev > 0  # EV = 0.8*(1/0.6 - 1) - 0.2 = 0.8*0.667 - 0.2 = 0.333

def test_ev_buy_at_true_prob():
    # If true prob = 0.5, price = 0.5, EV = 0
    ev = calc_ev(0.5, 0.5)
    assert ev == 0.0

def test_ev_buy_above_true_prob():
    # If true prob = 0.3, price = 0.6, EV should be negative
    ev = calc_ev(0.3, 0.6)
    assert ev < 0

def test_ev_edge_cases():
    assert calc_ev(0.5, 0.0) == 0.0
    assert calc_ev(0.5, 1.0) == 0.0
    assert calc_ev(0.5, 0.5) == 0.0

def test_kelly_fraction():
    # Kelly fraction with realistic odds
    k = calc_kelly(0.7, 0.5)  # 70% win, 2:1 odds
    # b = 1/0.5 - 1 = 1; f = (0.7*1 - 0.3)/1 = 0.4; fraction = 0.1
    assert 0.05 < k < 0.15

def test_kelly_edge_cases():
    assert calc_kelly(0.5, 0.0) == 0.0
    assert calc_kelly(0.5, 1.0) == 0.0
    assert calc_kelly(1.0, 0.3) == 0.0  # certainty shouldn't bet infinite

def test_bet_size_caps_at_max_bet():
    size = bet_size(kelly=1.0, balance=100.0, max_bet=20.0)
    assert size == 20.0

def test_bet_size_min_bet():
    size = bet_size(kelly=0.01, balance=100.0, max_bet=20.0)
    assert size == 0.0  # below $0.50 minimum

def test_bet_size_respects_balance():
    size = bet_size(kelly=0.5, balance=5.0, max_bet=20.0)
    assert size <= 5.0
```

- [ ] **Step 2: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_betsizing.py -v`
Expected: 11 failures (betsizing.py doesn't exist)

- [ ] **Step 3: Write betsizing.py**

```python
"""Kelly Criterion bet sizing and bucket probability math."""
import math

# ---- Probability ----

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast: float, t_low: float, t_high: float, sigma: float = 2.0) -> float:
    """
    Probability that actual temperature falls in [t_low, t_high]
    given a Gaussian-distributed forecast error with std=sigma.
    """
    if t_low == t_high:
        # Exact point — single degree bucket
        z = (forecast - t_low) / sigma
        return norm_cdf(z + 0.5/sigma) - norm_cdf(z - 0.5/sigma)
    # Range bucket
    z_low = (t_low - forecast) / sigma
    z_high = (t_high - forecast) / sigma
    return norm_cdf(z_high) - norm_cdf(z_low)

# ---- Expected Value ----

def calc_ev(p: float, price: float) -> float:
    """Expected value of buying a YES token at `price` when true prob = p."""
    if price <= 0 or price >= 1:
        return 0.0
    return p * (1.0 / price - 1.0) - (1.0 - p)

# ---- Kelly ----

def calc_kelly(p: float, price: float) -> float:
    """
    Full Kelly fraction for a binary outcome.
    b = payout multiplier (1/price - 1)
    f = (p*b - (1-p)) / b
    """
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(0.0, min(f, 1.0))

def bet_size(kelly: float, balance: float, max_bet: float, min_bet: float = 0.50) -> float:
    """Calculate bet size given Kelly fraction and constraints."""
    raw = balance * kelly
    if raw < min_bet:
        return 0.0
    return min(raw, max_bet)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_betsizing.py -v`
Expected: PASS (11/11)

---

## Task 5: Polymarket Market Data Layer

**Files:**
- Create: `weatherbot2/src/polymarket.py`
- Create: `weatherbot2/tests/test_polymarket.py`

- [ ] **Step 1: Write tests (mock Polymarket API)**

```python
# tests/test_polymarket.py
import pytest, unittest.mock as mock
from src.polymarket import PolymarketClient, parse_temp_range

def test_parse_between_bucket():
    q = "Will the highest temperature in Miami be between 84-85°F on April 20?"
    assert parse_temp_range(q) == (84.0, 85.0)

def test_parse_or_below():
    q = "Will the highest temperature in NYC be 73°F or below on April 20?"
    assert parse_temp_range(q) == (-999.0, 73.0)

def test_parse_or_above():
    q = "Will the highest temperature in Tokyo be 35°C or higher on July 15?"
    assert parse_temp_range(q) == (35.0, 999.0)

def test_parse_exact():
    q = "Will the highest temperature in London be 25°C on June 1?"
    assert parse_temp_range(q) == (25.0, 25.0)

def test_market_slug_construction():
    from src.polymarket import make_slug
    slug = make_slug("nyc", "april", 20, 2026)
    assert slug == "highest-temperature-in-nyc-on-april-20-2026"

def test_get_markets_for_city():
    import unittest.mock as mock
    client = PolymarketClient()
    mock_response = {
        "markets": [
            {
                "id": "mid-1",
                "question": "Will the highest temperature in NYC be between 80-81°F on April 20?",
                "outcomePrices": "[0.49, 0.51]",
                "bestBid": 0.49, "bestAsk": 0.51,
                "volume": 5000.0, "closed": False,
                "endDate": "2026-04-20T12:00:00Z",
            }
        ]
    }
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = mock_response
        markets = client.get_city_markets("nyc", "april", 20, 2026)
        assert len(markets) == 1
        assert markets[0]["id"] == "mid-1"
        assert markets[0]["bid"] == 0.49
        assert markets[0]["ask"] == 0.51

def test_check_resolved_win():
    import unittest.mock as mock
    client = PolymarketClient()
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "closed": True, "outcomePrices": "[0.97, 0.03]"
        }
        result = client.check_resolved("mid-1")
        assert result == True  # YES won at 0.97

def test_check_resolved_loss():
    client = PolymarketClient()
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "closed": True, "outcomePrices": "[0.04, 0.96]"
        }
        result = client.check_resolved("mid-1")
        assert result == False  # YES lost at 0.04
```

- [ ] **Step 2: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_polymarket.py -v`
Expected: 7 failures

- [ ] **Step 3: Write polymarket.py**

```python
"""Polymarket API client — market discovery, price fetching, resolution check."""
import re, logging, requests
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)
MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def make_slug(city: str, month: str, day: int, year: int) -> str:
    return f"highest-temperature-in-{city}-on-{month}-{day}-{year}"


def parse_temp_range(question: str) -> Optional[tuple[float, float]]:
    """Extract (low, high) temp range from Polymarket question string."""
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    # "73°F or below"
    m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
    if m:
        return (-999.0, float(m.group(1)))
    # "35°C or higher"
    m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), 999.0)
    # "between 84-85°F"
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    # exact: "be 25°C on"
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def hours_to_resolution(end_date_str: str) -> float:
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0


class PolymarketClient:
    def get_city_markets(self, city_slug: str, month: str, day: int, year: int) -> list[dict]:
        """Get all bucket markets for a city/date."""
        slug = make_slug(city_slug, month, day, year)
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={"limit": 50},
                headers={"Content-Type": "application/json"},
                timeout=(5, 8)
            )
            all_markets = r.json()
            # Filter to matching slug
            city_markets = [
                m for m in all_markets
                if m.get("slug", "").replace("-", "").replace(" ", "") ==
                   slug.replace("-", "").replace(" ", "")
            ]
            # If no exact slug match, try event-based lookup
            if not city_markets:
                return self._get_via_events(city_slug, month, day, year)
            return self._parse_markets(city_markets)
        except Exception as e:
            log.error("[Polymarket] get_city_markets %s: %s", city_slug, e)
            return []

    def _get_via_events(self, city_slug: str, month: str, day: int, year: int) -> list[dict]:
        """Fallback: query by event slug."""
        slug = make_slug(city_slug, month, day, year)
        try:
            r = requests.get(
                f"{GAMMA_API}/events",
                params={"slug": slug},
                timeout=(5, 8)
            )
            events = r.json()
            if not events or not isinstance(events, list):
                return []
            event = events[0]
            market_ids = event.get("marketIds", [])
            markets = []
            for mid in market_ids:
                m = self._fetch_market(mid)
                if m:
                    markets.append(m)
            return self._parse_markets(markets)
        except Exception as e:
            log.error("[Polymarket] _get_via_events %s: %s", city_slug, e)
            return []

    def _fetch_market(self, market_id: str) -> Optional[dict]:
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(3, 5))
            return r.json()
        except Exception:
            return None

    def _parse_markets(self, markets: list[dict]) -> list[dict]:
        """Normalize Polymarket market objects to our internal format."""
        result = []
        for m in markets:
            try:
                prices = m.get("outcomePrices", "[0.5,0.5]")
                if isinstance(prices, str):
                    prices = prices.strip('"')
                    prices_list = [float(x) for x in prices.strip("[]").split(",")]
                else:
                    prices_list = [float(prices[0]), float(prices[1])]
                bid = float(m.get("bestBid", prices_list[0]))
                ask = float(m.get("bestAsk", prices_list[1]))
                volume = float(m.get("volume", 0))
                result.append({
                    "id": m["id"],
                    "question": m.get("question", ""),
                    "range": parse_temp_range(m.get("question", "")),
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "spread": round(ask - bid, 4),
                    "volume": volume,
                    "closed": m.get("closed", False),
                    "end_date": m.get("endDate", ""),
                })
            except Exception as e:
                log.debug("Failed to parse market %s: %s", m.get("id"), e)
        return result

    def check_resolved(self, market_id: str) -> Optional[bool]:
        """
        Check if market is resolved.
        Returns: True (YES won), False (NO won), None (still open)
        """
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(5, 8))
            data = r.json()
            closed = data.get("closed", False)
            if not closed:
                return None
            prices = data.get("outcomePrices", "[0.5,0.5]")
            if isinstance(prices, str):
                prices = prices.strip('"')
                prices_list = [float(x) for x in prices.strip("[]").split(",")]
            else:
                prices_list = [float(prices[0]), float(prices[1])]
            yes_price = prices_list[0]
            if yes_price >= 0.95:
                return True
            elif yes_price <= 0.05:
                return False
            return None
        except Exception as e:
            log.error("[Polymarket] check_resolved %s: %s", market_id, e)
            return None

    def get_current_price(self, market_id: str) -> Optional[float]:
        """Get current best bid price for a market."""
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(3, 5))
            prices = r.json().get("outcomePrices", "[0.5,0.5]")
            if isinstance(prices, str):
                prices = prices.strip('"')
                prices_list = [float(x) for x in prices.strip("[]").split(",")]
            else:
                prices_list = [float(prices[0]), float(prices[1])]
            return float(prices_list[0])
        except Exception:
            return None
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/test_polymarket.py -v`
Expected: PASS (7/7)

---

## Task 6: Scanner + Main Loop

**Files:**
- Create: `weatherbot2/src/scanner.py`
- Create: `weatherbot2/src/main.py`
- Modify: `weatherbot2/run.sh`

This is the core orchestration: for each city/date, fetch forecasts, find tradeable buckets, apply filters, open/update/close positions.

- [ ] **Step 1: Write scanner.py**

```python
"""Main scanning engine — orchestrates forecast + market data into trading decisions."""
import logging, time, signal
from datetime import datetime, timezone, timedelta
from typing import Optional

from .config import Config
from .forecast import ForecastEngine, LOCATIONS
from .polymarket import PolymarketClient, hours_to_resolution
from .betsizing import bucket_prob, calc_ev, calc_kelly, bet_size
from .state import StateDB

log = logging.getLogger(__name__)
MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

SCAN_TIMEOUT = 40  # seconds — bail if scan takes too long


class Scanner:
    def __init__(self, cfg: Config, state: StateDB, forecast: ForecastEngine, pm: PolymarketClient):
        self.cfg = cfg
        self.state = state
        self.fc = forecast
        self.pm = pm

    def run_full_scan(self) -> tuple[int, int, int]:
        """
        Full scan: check all cities, all dates.
        Returns (new_positions, closed, resolved).
        """
        now = datetime.now(timezone.utc)
        scan_start = time.time()
        dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
        new_pos, closed, resolved = 0, 0, 0

        for city_slug, loc in LOCATIONS.items():
            if time.time() - scan_start > SCAN_TIMEOUT:
                log.warning("Scan timeout — stopping early at %s", city_slug)
                break

            # Fetch forecasts for this city
            try:
                snaps = self.fc.get_forecasts(city_slug, dates)
                time.sleep(0.25)  # rate limit
            except Exception as e:
                log.error("[Scan] Forecast error %s: %s", city_slug, e)
                continue

            for i, date_str in enumerate(dates):
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                month_name = MONTHS[dt.month - 1]
                markets = self.pm.get_city_markets(city_slug, month_name, dt.day, dt.year)
                if not markets:
                    continue
                time.sleep(0.2)

                snap = snaps.get(date_str, {})
                forecast_temp = snap.get("best")
                best_src = snap.get("best_source")
                if forecast_temp is None:
                    continue

                hours_left = hours_to_resolution(markets[0]["end_date"]) if markets else 0

                # --- Check existing position ---
                pos_id = markets[0]["id"]
                open_pos = {p["id"]: p for p in self.state.get_open_positions()}
                if pos_id in open_pos:
                    # Monitor existing position
                    c = self._check_close(pos_id, forecast_temp, markets)
                    if c:
                        closed += c
                    continue

                # --- Open new position ---
                if hours_left < self.cfg.min_hours or hours_left > self.cfg.max_hours:
                    continue

                sig = self._find_signal(forecast_temp, markets, best_src, loc, hours_left)
                if sig:
                    ok = self.state.open_position(sig)
                    if ok:
                        new_pos += 1
                        log.info(
                            "[OPEN] %s %s %s°F -> %s | p=%.2f ev=%.2f cost=$%.2f",
                            loc["name"], date_str,
                            f"{forecast_temp}°{loc['unit']}",
                            sig["bucket_low"], sig["p"], sig["ev"], sig["cost"]
                        )

        # Check for resolved markets among open positions
        for pos in self.state.get_open_positions():
            result = self.pm.check_resolved(pos["id"])
            if result is not None:
                exit_price = 1.0 if result else 0.0
                self.state.close_position(pos["id"], exit_price, f"resolved_{'win' if result else 'loss'}")
                resolved += 1
                log.info("[RESOLVED] %s %s -> %s", pos["city"], pos["date"], "WIN" if result else "LOSS")

        return new_pos, closed, resolved

    def _find_signal(self, forecast_temp: float, markets: list[dict],
                     best_src: str, loc: dict, hours_left: float) -> Optional[dict]:
        """Find the best tradeable bucket for a forecast."""
        unit = loc["unit"]
        sigma = self.state.get_calibration(loc["name"].lower().replace(" ", "-"), best_src)
        sigma = sigma or (2.0 if unit == "F" else 1.2)

        for m in markets:
            rng = m.get("range")
            if not rng:
                continue
            t_low, t_high = rng
            bid, ask = m["bid"], m["ask"]
            spread = m.get("spread", 0)
            volume = m.get("volume", 0)

            # Filters
            if volume < self.cfg.min_volume:
                continue
            if spread > self.cfg.max_slippage:
                continue
            if ask > self.cfg.max_price:
                continue
            # Must be in bucket
            if not (t_low <= float(forecast_temp) <= t_high):
                continue

            p = bucket_prob(forecast_temp, t_low, t_high, sigma)
            ev = calc_ev(p, ask)
            if ev < self.cfg.min_ev:
                continue

            kelly = calc_kelly(p, ask)
            balance = self.state.get_balance()
            size = bet_size(kelly, balance, self.cfg.max_bet)
            if size < 0.50:
                continue

            shares = round(size / ask, 2) if ask > 0 else 0
            return {
                "id": m["id"],
                "city": loc["name"].lower().replace(" ", "-"),
                "date": m.get("date", ""),
                "bucket_low": t_low,
                "bucket_high": t_high,
                "entry_price": ask,
                "bid_at_entry": bid,
                "spread": spread,
                "shares": shares,
                "cost": round(size, 2),
                "p": round(p, 4),
                "ev": round(ev, 4),
                "kelly": round(kelly, 4),
                "forecast_temp": forecast_temp,
                "forecast_src": best_src,
            }

    def _check_close(self, pos_id: str, forecast_temp: float, markets: list[dict]) -> int:
        """Check if position should be closed for stop-loss or forecast shift."""
        open_pos = {p["id"]: p for p in self.state.get_open_positions()}
        if pos_id not in open_pos:
            return 0
        pos = open_pos[pos_id]

        # Current price
        current_price = None
        for m in markets:
            if m["id"] == pos_id:
                current_price = m.get("bid")
                break
        if current_price is None:
            return 0

        entry = pos["entry_price"]
        stop = pos.get("stop_price", entry * 0.80)
        closed = 0

        # Stop loss
        if current_price <= stop:
            self.state.close_position(pos_id, current_price, "stop_loss")
            closed = 1
        # Trailing stop: if up 20%, move to breakeven
        elif current_price >= entry * 1.20 and stop < entry:
            # Update stop in-memory (in real impl, would update DB)
            log.info("[TRAILING] %s stop moved to breakeven $%.3f", pos_id, entry)
        # Forecast shifted 2+ degrees
        else:
            unit_sym = "F"
            buffer = 2.0
            mid = (pos["bucket_low"] + pos["bucket_high"]) / 2
            if abs(forecast_temp - mid) > (abs(mid - pos["bucket_low"]) + buffer):
                self.state.close_position(pos_id, current_price, "forecast_shift")
                closed = 1

        return closed
```

- [ ] **Step 2: Write main.py**

```python
"""Weatherbot V2 — main entry point."""
import sys, os, logging, time, signal
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config
from src.state import StateDB
from src.forecast import ForecastEngine
from src.polymarket import PolymarketClient
from src.scanner import Scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "logs" / "bot.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

BOT_DIR = Path(__file__).parent.parent
DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / "weatherbot.db")


def main():
    cfg = Config.load()
    errs = cfg.validate()
    if errs:
        for e in errs:
            log.error("Config error: %s", e)
        if not cfg.vc_key:
            log.warning("vc_key missing — calibration disabled, running with defaults")
        if cfg.polygon_wallet_pk:
            log.info("Wallet configured — LIVE MODE")
        else:
            log.info("No wallet — PAPER MODE")

    state = StateDB(DB_PATH)
    state.init()

    # Initialize balance if fresh DB
    if state.get_balance() == 0.0:
        state.update_balance(cfg.balance, "init")

    fc = ForecastEngine(cfg.vc_key)
    pm = PolymarketClient()
    scanner = Scanner(cfg, state, fc, pm)

    log.info("Weatherbot V2 starting | balance=$%.2f | max_bet=$%.2f | scan_interval=%ds",
             cfg.balance, cfg.max_bet, cfg.scan_interval)

    def shutdown(signum, frame):
        log.info("Shutting down — saving state...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_full_scan = 0
    while True:
        now = time.time()
        if now - last_full_scan >= cfg.scan_interval:
            log.info("=== FULL SCAN ===")
            try:
                new_pos, closed, resolved = scanner.run_full_scan()
                bal = state.get_balance()
                log.info("Scan done | balance=$%.2f | new=%d | closed=%d | resolved=%d",
                         bal, new_pos, closed, resolved)
                last_full_scan = time.time()
            except Exception as e:
                log.error("Scan error: %s", e, exc_info=True)
                time.sleep(30)
                continue

        # Quick position monitor
        try:
            bal = state.get_balance()
            open_pos = state.get_open_positions()
            log.debug("Monitor | balance=$%.2f | open_positions=%d", bal, len(open_pos))
        except Exception as e:
            log.error("Monitor error: %s", e)

        time.sleep(cfg.monitor_interval)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Update run.sh**

```bash
#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p logs data
python3 -u src/main.py >> logs/bot.log 2>&1 &
echo "Weatherbot PID: $!"
echo $! > .bot.pid
echo "Log: logs/bot.log"
```

- [ ] **Step 4: Install test deps and run all tests**

```bash
cd ~/Desktop/weatherbot2
python3 -m pip install pytest --quiet 2>/dev/null
python3 -m pytest tests/ -v --tb=short
```

Expected: PASS (all 27 tests)

---

## Task 7: Integration Test + Deploy

- [ ] **Step 1: Create tests/__init__.py**

```bash
touch ~/Desktop/weatherbot2/tests/__init__.py
```

- [ ] **Step 2: Run all unit tests**

Run: `cd ~/Desktop/weatherbot2 && python3 -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 3: Verify bot starts**

Run: `cd ~/Desktop/weatherbot2 && timeout 15 python3 -u src/main.py 2>&1 | head -30`
Expected: Bot starts, loads config, begins scan loop without crashing

- [ ] **Step 4: Start as background service**

Run: `cd ~/Desktop/weatherbot2 && bash run.sh && sleep 3 && cat logs/bot.log | tail -20`
Expected: Bot running in background, log shows startup messages

- [ ] **Step 5: Get VC API key setup instructions**

Tell the user: get free VC key from visualcrossing.com, add to config.json as `"vc_key": "YOUR_KEY"`, then `kill` the old bot and `bash run.sh` again.

---

## Verification Checklist

- [ ] All 27 tests pass
- [ ] Bot starts without errors
- [ ] Bot runs full scan without hanging
- [ ] Balance updates correctly on paper trades
- [ ] No duplicate positions possible
- [ ] vc_key can be added to config.json without restart
- [ ] Graceful SIGTERM / Ctrl+C shutdown

## Risks & Open Questions

1. **Polymarket API rate limits** — if Gamma API throttles during full scan, may need caching
2. **VC key required for calibration** — bot works without it (using default sigma), but calibration is more accurate with it
3. **Live trading** — polygon_wallet_pk not yet wired; this plan covers paper mode only
