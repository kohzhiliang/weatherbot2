# Weatherbot V2

Paper-trading bot for Polymarket weather markets. Trades temperature bucket markets across 20 global cities using ECMWF/HRRR weather forecasts with Kelly Criterion position sizing and self-calibrating probability estimates.

**Currently running:** paper mode, live on VPS at `159.65.130.226`

---

## Architecture

```
src/
  main.py          — entry point, scan loop, signal handling
  config.py        — config loading + validation
  state.py         — SQLite state DB (positions, resolved, calibration)
  forecast.py      — weather forecast engine (ECMWF, HRRR, METAR)
  polymarket.py    — Polymarket Gamma API client
  scanner.py       — market scanner + EV filter + trailing stops
  betsizing.py     — Kelly Criterion position sizing
  whale_monitor.py — Polymarket whale wallet tracker + auto-copy trades

watchdog.sh       — auto-restart script (monitors liveness + activity)
config.json       — user configuration (gitignored — contains secrets)
```

---

## How It Works

### Weather Scanning
1. Fetches all active Polymarket temperature bucket markets via Gamma API
2. For each of 20 cities, compares market price to forecast probability
3. Forecasts come from 3 sources:
   - **ECMWF** via Open-Meteo — global, 7-day horizon
   - **HRRR/GFS** via Open-Meteo — US only, 48h horizon (more accurate near-term)
   - **METAR** — real-time airport station observations, D+0 only
4. Selects best forecast source per city per horizon (HRRR for US D+0/D+1, ECMWF otherwise)

### Entry Filters (ALL must pass)
- `volume >= min_volume` ($500 default)
- spread (ask–bid) <= `max_slippage` ($0.03 default)
- ask price < `max_price` ($0.45 default)
- EV >= `min_ev` (15% default)
- hours to resolution: 2–72 hours

### EV Formula
```
EV = P(win) * (1/price - 1) - P(loss)
```

### Position Sizing
- Fractional Kelly at 25% (quarter Kelly)
- `bet_size = min(Kelly * balance, max_bet)`
- `max_bet` default $20

### Exit Rules
- **Stop-loss:** 20% trailing stop — if price rises 20%+ above entry, stop moves to breakeven
- **Forecast-shift exit:** close if forecast moves 2+ degrees outside traded bucket
- **Take-profit by horizon:** <24h hold to resolution, 24–48h take profit at $0.85, 48h+ take profit at $0.75

### Self-Calibration
After each market resolves, fetches actual temperature via Visual Crossing API and recalculates forecast error (sigma) per city/source. Uses calibrated sigma in probability calculations after 30+ resolved markets.

### Whale Copy Trading
Runs alongside weather scanning. Polls Polymarket Data API every 2 minutes for trades from known whale wallets. Auto-copies at 10% position size (min $10, max `max_bet`). Paper trades only.

Known whales: `Untried-Android`, `Anon-Cypher`, `Anon-Flux`, `Used-Scheme`, `Bogus-Fix`

---

## Setup

### Prerequisites
- Python 3.10+
- Visual Crossing API key (free at visualcrossing.com)
- Polymarket account (for live trading)

### Installation
```bash
git clone https://github.com/kohzhiliang/weatherbot2.git
cd weatherbot2
```

### Configuration
Edit `config.json`:
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
    "vc_key": "YOUR_VISUAL_CROSSING_KEY",
    "polygon_wallet_pk": "",
    "log_level": "INFO"
}
```

- `vc_key` — required for self-calibration (forecasts work without it, calibration doesn't)
- `polygon_wallet_pk` — leave empty for paper mode, add key for live trading
- `scan_interval_seconds` — how often to run a full market scan (3600 = 1 hour)
- `monitor_interval_seconds` — how often to check positions (600 = 10 min)

### Run
```bash
# Start watchdog (auto-restarts on crash/hang)
bash watchdog.sh start

# Check status
bash watchdog.sh status

# Stop
bash watchdog.sh stop
```

### Run tests
```bash
pytest tests/
```

---

## Cities Tracked

| Region | Cities |
|--------|--------|
| US | NYC (KLGA), Chicago (KORD), Miami (KMIA), Dallas (KDAL), Seattle (KSEA), Atlanta (KATL) |
| Europe | London (EGLC), Paris (LFPG), Munich (EDDM), Ankara (LTAC) |
| Asia | Seoul (RKSI), Tokyo (RJTT), Shanghai (ZSPD), Singapore (WSSS), Lucknow (VILK), Tel Aviv (LLBG) |
| Canada | Toronto (CYYZ) |
| South America | Sao Paulo (SBGR), Buenos Aires (SAEZ) |
| Oceania | Wellington (NZWN) |

> **Critical:** Polymarket resolves on specific airport weather station coordinates, NOT city centers. Bot uses correct airport station IDs. Using city center coordinates introduces 3–8°F error on 1–2°F bucket markets.

---

## Deploy

### Mac (local dev)
```bash
# Edit code
# Push to GitHub
git add . && git commit -m "description" && git push
```

### VPS (production)
```bash
# Pull latest from GitHub
cd /home/hermes/weatherbot2 && git pull

# Watchdog auto-restarts the bot on next health check
```

> Note: `watchdog.sh` contains hardcoded paths. After `git pull`, verify `BOT_DIR` in `watchdog.sh` points to the correct directory.

---

## File Structure

```
weatherbot2/
├── config.json       — secrets + configuration (DO NOT COMMIT)
├── src/
│   ├── main.py       — scan loop + whale monitoring
│   ├── config.py     — config dataclass
│   ├── state.py      — SQLite position tracking
│   ├── forecast.py   — ECMWF/HRRR/METAR weather fetching
│   ├── polymarket.py — Polymarket Gamma API client
│   ├── scanner.py    — EV filtering + trailing stops
│   ├── betsizing.py  — Kelly Criterion calculator
│   └── whale_monitor.py — whale wallet cloner
├── watchdog.sh       — auto-restart script
├── run.sh            — simple start script
├── tests/            — pytest unit tests
├── data/             — SQLite DB + per-market JSON (gitignored)
└── logs/             — bot + watchdog logs (gitignored)
```
