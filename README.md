# Weatherbot V2

Paper-trading bot for Polymarket temperature bucket markets. Trades 20 global cities using ECMWF/HRRR weather forecasts with Kelly Criterion sizing and self-calibrating probabilities.

**Running:** paper mode

## Quick Start

```bash
# Clone
git clone https://github.com/kohzhiliang/weatherbot2.git
cd weatherbot2

# Configure — edit config.json and add your vc_key
cp config.json.example config.json

# Run
bash watchdog.sh start
```

## How It Works

1. Scans Polymarket for temperature bucket markets across 20 cities
2. Compares market price vs ECMWF/HRRR/METAR forecast probability
3. Enters only when EV >= 15% and price <= $0.45
4. Sizes positions with fractional Kelly (25%)
5. Auto-copies whale wallet trades every 2 minutes

## Key Settings (config.json)

| Param | Default | Description |
|-------|---------|-------------|
| `balance` | 100 | Starting paper balance |
| `max_bet` | 20 | Max bet per trade |
| `min_ev` | 0.15 | Minimum expected value % |
| `max_price` | 0.45 | Only buy if price <= this |
| `kelly_fraction` | 0.25 | Kelly fraction (0.25 = quarter Kelly) |
| `vc_key` | — | Visual Crossing API key for calibration |
| `polygon_wallet_pk` | — | Leave empty for paper mode |

## Deploy

Push to GitHub, then on your server:
```bash
git pull
```

## Architecture

```
src/
  main.py          — scan loop
  forecast.py      — ECMWF + HRRR + METAR weather data
  polymarket.py    — Polymarket API client
  scanner.py       — EV filter + trailing stops
  betsizing.py     — Kelly Criterion sizing
  state.py         — SQLite position tracking
  whale_monitor.py — whale wallet auto-cloner
```

> **Airports, not city centers.** Polymarket resolves on specific airport weather stations (KLGA = NYC, KORD = Chicago, etc.). Using city coordinates introduces 3–8°F error.
