#!/usr/bin/env python3
"""
ECMWF Bias Validator — empirical calibration against actual resolved outcomes.

This script:
  1. Reads all resolved trades from the DB (city + date + bucket + outcome)
  2. Fetches the ACTUAL daily high/low for each city/date via Open-Meteo Archive
  3. Compares actual temps against what ECMWF predicted (forecast_temp in positions)
  4. Computes per-city bias = mean(actual - ecmwf_forecast)
  5. Updates src/forecast.py ECMWF_BIAS_CORRECTION with validated values
  6. Optionally persists per-city bias records to a new ecmwf_bias DB table

Usage:
    python3 ecmwf_bias_validator.py              # dry run (print only)
    python3 ecmwf_bias_validator.py --write       # apply to forecast.py
    python3 ecmwf_bias_validator.py --db          # also write to DB table
"""
import sqlite3, requests, re, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB     = "/home/hermes/weatherbot2/data/weatherbot.db"
SRC    = Path("/home/hermes/weatherbot2/src/forecast.py")
ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1/archive"

# ── Location map ──────────────────────────────────────────────────────────────
LOCATIONS = {
    "nyc":           (40.7772,  -73.8726,  "F"),
    "chicago":       (41.9742,  -87.9073,  "F"),
    "miami":         (25.7959,  -80.2870,  "F"),
    "dallas":        (32.8471,  -96.8518,  "F"),
    "seattle":       (47.4502, -122.3088,  "F"),
    "atlanta":       (33.6407,  -84.4277,  "F"),
    "london":        (51.5048,    0.0495,  "C"),
    "paris":         (48.9962,    2.5979,  "C"),
    "munich":        (48.3537,   11.7750,  "C"),
    "ankara":        (40.1281,   32.9951,  "C"),
    "seoul":         (37.4691,  126.4505,  "C"),
    "tokyo":         (35.7647,  140.3864,  "C"),
    "shanghai":      (31.1443,  121.8083,  "C"),
    "singapore":      (1.3502,  103.9940,  "C"),
    "lucknow":       (26.7606,   80.8893,  "C"),
    "tel-aviv":      (32.0114,   34.8867,  "C"),
    "toronto":       (43.6772,  -79.6306,  "C"),
    "sao-paulo":    (-23.4356,  -46.4731,  "C"),
    "buenos-aires": (-34.8222,  -58.5358,  "C"),
    "wellington":   (-41.3272,  174.8052,  "C"),
    "beijing":       (40.0795,  116.5972,  "C"),
    "shenzhen":      (22.5431,  114.0579,  "C"),
    "chongqing":     (29.5630,  106.5516,  "C"),
    "chengdu":       (30.6598,  104.0658,  "C"),
    "guangzhou":     (23.1291,  113.2644,  "C"),
    "busan":         (35.1796,  129.0756,  "C"),
    "jakarta":       (-6.2088,  106.8456,  "C"),
    "karachi":       (24.8607,   67.0111,  "C"),
    "houston":       (29.7604,  -95.3698,  "F"),
    "austin":        (30.2672,  -97.7431,  "F"),
    "denver":        (39.7392, -104.9903,  "F"),
    "mexico-city":   (19.4326,  -99.1332,  "C"),
    "helsinki":      (60.1699,   24.9384,  "C"),
    "lagos":          (6.5244,    3.3792,  "C"),
    "new-york-city": (40.7772,  -73.8726,  "F"),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def c_to_f(c): return c * 9/5 + 32
def f_to_c(f): return (f - 32) * 5/9


def fetch_actual(city_slug: str, date: str, unit: str) -> float | None:
    """Fetch actual daily high temperature from Open-Meteo Archive for city/date."""
    if city_slug not in LOCATIONS:
        return None
    lat, lon, _ = LOCATIONS[city_slug]

    # Open-Meteo archive API
    url = (
        f"{ARCHIVE_BASE}"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={date}&end_date={date}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&timezone=auto"
    )
    try:
        data = requests.get(url, timeout=10).json()
        daily = data.get("daily", {})
        max_t = daily.get("temperature_2m_max", [None])[0]
        min_t = daily.get("temperature_2m_min", [None])[0]
        if max_t is None:
            return None
        # Return the daily high in the city's native unit
        if unit == "F":
            return round(c_to_f(max_t))
        return round(max_t, 1)
    except Exception as e:
        print(f"  [WARN] {city_slug} {date}: {e}")
        return None


def get_market_type(title: str) -> str:
    """Return 'high', 'low', 'between', or 'other' from market title."""
    t = title.lower()
    if "highest" in t: return "high"
    if "lowest" in t: return "low"
    if "between" in t: return "between"
    return "other"


def bucket_to_temp(bucket_low: float, bucket_high: float, unit: str) -> tuple[float, float]:
    """Return (low, high) temps from bucket bounds, converting if needed."""
    lo, hi = bucket_low, bucket_high
    if unit == "F":
        if lo != -999:
            lo = round(f_to_c(lo))
        hi = round(f_to_c(hi))
    return lo, hi


# ── Core bias calculation ─────────────────────────────────────────────────────

def validate_all() -> dict[str, dict]:
    """
    For each resolved trade:
      - Determine the actual temperature from archive
      - Compute bias = actual - ecmwf_forecast
    Returns dict[city_slug] -> {bias_values: [], n: int, unit: str}
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Get resolved trades with dates and forecast info
    # Get resolved trades with dates and ECMWF forecast
    # forecast_temp lives in positions table; join on pos id + key fields
    # resolved table doesn't have 'title'; market_name is in positions
    rows = conn.execute("""
        SELECT
            r.city, r.date, r.bucket_low, r.bucket_high,
            p.forecast_temp, r.forecast_src, r.resolved_outcome,
            r.entry_price, r.side,
            COALESCE(p.market_name, r.city) as title
        FROM resolved r
        JOIN positions p ON p.id = r.id
        WHERE r.date IS NOT NULL
          AND p.forecast_temp IS NOT NULL
          AND r.forecast_src = 'ecmwf'
        ORDER BY r.date
    """).fetchall()

    results: dict[str, dict] = {}

    for r in rows:
        city   = r["city"]
        date   = r["date"][:10]   # "YYYY-MM-DD"
        blo, bhi = r["bucket_low"], r["bucket_high"]
        fc_temp  = r["forecast_temp"]
        unit     = LOCATIONS.get(city, (None, None, "C"))[2]

        if unit is None:
            continue

        actual = fetch_actual(city, date, unit)
        if actual is None:
            continue

        # Bias = actual minus ECMWF forecast
        bias = round(actual - fc_temp, 1)

        mtype = get_market_type(r["title"])
        won   = r["resolved_outcome"] == "win"

        if city not in results:
            results[city] = {"bias_values": [], "unit": unit, "samples": []}

        results[city]["bias_values"].append(bias)
        results[city]["samples"].append({
            "date": date,
            "actual": actual,
            "ecmwf": fc_temp,
            "bias": bias,
            "bucket": f"{blo:.0f}-{bhi:.0f}",
            "mtype": mtype,
            "won": won,
        })

    return results


def compute_city_bias(results: dict[str, dict], min_samples: int = 2) -> dict[str, float]:
    """
    Compute per-city bias as the rolling mean of bias values.
    Uses exponential weighting (recent samples count more).
    Returns dict[city_slug] -> bias (float).
    """
    import statistics
    final_bias = {}

    for city, data in sorted(results.items()):
        values = data["bias_values"]
        unit   = data["unit"]

        if len(values) < min_samples:
            print(f"  {city}: only {len(values)} samples — skipping")
            continue

        # Simple mean (could use exp weighted)
        mean_bias = round(statistics.mean(values), 1)

        # Weighted mean (recent samples weighted more)
        # More recent = higher weight
        n = len(values)
        weights = [i + 1 for i in range(n)]  # linear weight: oldest=1, newest=n
        weighted_mean = round(sum(w * v for w, v in zip(weights, values)) / sum(weights), 1)

        final_bias[city] = {
            "bias": mean_bias,
            "weighted_bias": weighted_mean,
            "n": n,
            "unit": unit,
            "values": values,
        }

    return final_bias


# ── Print report ─────────────────────────────────────────────────────────────

def print_report(final_bias: dict, results: dict):
    print()
    print("=" * 80)
    print("ECMWF BIAS VALIDATION REPORT")
    print("=" * 80)
    print()
    print(f"{'City':<15} | {'N'} | {'Unit'} | {'Mean Bias':>10} | {'Weighted':>10} | {'Individual samples'}")
    print("-" * 90)

    for city, data in sorted(final_bias.items(), key=lambda x: -abs(x[1].get("bias", 0))):
        b = data["bias"]
        wb = data["weighted_bias"]
        n  = data["n"]
        unit = data["unit"]
        vals = data["values"]

        arrow = "↑" if b > 0 else "↓" if b < 0 else "─"
        print(f"{city:<15} | {n:>2} | {unit:>4} | {b:>+8.1f}° {arrow} | {wb:>+8.1f}° | {vals}")

    print()
    print("NOTE: Positive bias = ECMWF forecasts COLD (actual warmer than ECMWF predicts)")
    print("      Negative bias = ECMWF forecasts WARM (actual colder than ECMWF predicts)")
    print()

    # Warnings for large discrepancies
    print("⚠️  Cities with bias magnitude > 2.0°C:")
    for city, data in sorted(final_bias.items(), key=lambda x: -abs(x[1].get("bias", 0))):
        b = data["bias"]
        if abs(b) > 2.0:
            print(f"  {city}: {b:+.1f}°C — {'ECMWF runs COLD' if b > 0 else 'ECMWF runs WARM'}")


# ── Update forecast.py ────────────────────────────────────────────────────────

def build_new_bias_table(final_bias: dict) -> str:
    """Build new ECMWF_BIAS_CORRECTION dict string for forecast.py."""
    lines = []
    # All known cities
    all_cities = {
        "singapore": "+3.5", "seoul": "+3.0", "tokyo": "+2.5",
        "lucknow": "+2.5", "shanghai": "-1.0", "wellington": "-1.0",
        "paris": "+1.0", "london": "+1.0", "beijing": "+2.0",
        "shenzhen": "+2.0", "hong-kong": "+1.5", "chongqing": "+1.5",
        "chengdu": "+1.5", "guangzhou": "+1.5", "busan": "+2.0",
        "tel-aviv": "+1.0", "manila": "+1.5", "sao-paulo": "-0.5",
        "buenos-aires": "-0.5", "ankara": "+1.0", "munich": "+0.5",
        "toronto": "+0.5", "jakarta": "+1.5", "karachi": "+2.0",
        "helsinki": "+0.5", "lagos": "+1.5", "mexico-city": "+1.5",
    }

    # Override with empirically computed values
    for city, data in final_bias.items():
        b = data["bias"]
        if len(data["values"]) >= 2:
            sign = "+" if b >= 0 else ""
            all_cities[city] = f"{sign}{b:.1f}"

    for city in sorted(all_cities.keys()):
        bias = all_cities[city]
        comment = ""
        if city in final_bias and len(final_bias[city]["values"]) >= 2:
            n = final_bias[city]["n"]
            comment = f"  # empirically validated ({n} samples)"
        lines.append(f'    "{city}": {bias},{comment}')

    return "\n".join(lines)


def write_forecast_py(final_bias: dict):
    """Update the ECMWF_BIAS_CORRECTION dict in forecast.py with validated values."""
    new_table_lines = build_new_bias_table(final_bias)

    new_block = f'''# ── ECMWF Bias Correction (auto-validated against Open-Meteo archive) ─────────
# POSITIVE value = ECMWF forecasts cold, actual is warmer → subtract from ECMWF
# NEGATIVE value = ECMWF forecasts warm, actual is colder → add to ECMWF
# Empirically computed from resolved outcomes (validated samples ≥ 2):
ECMWF_BIAS_CORRECTION = {{
{new_table_lines}
}}

# ColdMath cities — prioritized for cold-event strategy (spring 2026)'''

    content = SRC.read_text()

    # Find the line: ECMWF_BIAS_CORRECTION = { ... }}
    import re

    # Match from the dict start to its closing brace
    pattern = r'(ECMWF_BIAS_CORRECTION = \{\n[\s\S]*?\n\})'
    match = re.search(pattern, content)

    if match:
        old_block = match.group(1)
        # Also capture the old comment block before it
        old_full = old_block
        content = content.replace(old_block, new_block)
        print(f"Replaced ECMWF_BIAS_CORRECTION block")
    else:
        print("ERROR: Could not find ECMWF_BIAS_CORRECTION block!")
        return

    # Also update the old explanatory comment
    old_comment = "# ColdMath research: ECMWF has systematic warm bias for Asian cities."
    new_comment = "# Empirically validated — see ecmwf_bias_validator.py"
    content = content.replace(old_comment, new_comment)

    SRC.write_text(content)
    print(f"Written to {SRC}")


# ── DB persistence ────────────────────────────────────────────────────────────

def ensure_db_table():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ecmwf_bias (
            city          TEXT PRIMARY KEY,
            bias          REAL NOT NULL,
            weighted_bias REAL NOT NULL,
            n_samples     INTEGER NOT NULL,
            last_updated  TEXT NOT NULL,
            raw_samples   TEXT NOT NULL   -- JSON list of individual bias values
        )
    """)
    conn.commit()
    return conn


def write_to_db(final_bias: dict):
    """Write validated biases to the weatherbot DB via StateDB interface."""
    import math
    from src.state import StateDB
    state = StateDB(DB)
    written = 0
    for city, data in final_bias.items():
        if len(data["values"]) < 2:
            continue
        bias = data["bias"]
        n = data["n"]
        values = data["values"]
        rmse = math.sqrt(sum((v - bias) ** 2 for v in values) / n) if n > 0 else 0.0
        state.upsert_ecmwf_bias(city=city, bias=bias, n=n, rmse=rmse)
        written += 1
    print(f"Wrote {written} cities to DB ecmwf_bias table via StateDB")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ECMWF Bias Validator")
    parser.add_argument("--write", action="store_true", help="Write results to forecast.py")
    parser.add_argument("--db",    action="store_true", help="Also persist to ecmwf_bias DB table")
    parser.add_argument("--min-samples", type=int, default=2,
                        help="Minimum resolved trades per city to compute bias (default: 2)")
    args = parser.parse_args()

    print("Fetching and validating ECMWF bias from resolved trades...")
    print()

    results    = validate_all()
    final_bias = compute_city_bias(results, min_samples=args.min_samples)

    print_report(final_bias, results)

    if args.db:
        write_to_db(final_bias)

    if args.write:
        write_forecast_py(final_bias)
    else:
        print("[Dry run — use --write to apply, --db to persist, or both]")


# ── Auto-calibration (called by scanner each scan) ─────────────────────────

AUTOCAL_MIN_SAMPLES = 3   # need at least 3 resolved trades before updating bias
AUTOCAL_MIN_INTERVAL = 12 * 3600  # minimum 12 hours between auto-updates


def maybe_update(state: "StateDB", min_interval: int = AUTOCAL_MIN_INTERVAL,
                 min_samples: int = AUTOCAL_MIN_SAMPLES) -> bool:
    """
    Run ECMWF bias auto-calibration if:
      1. It's been > min_interval seconds since last run, AND
      2. We have ≥ min_samples resolved trades for at least one city

    Returns True if an update was performed, False otherwise.
    This is safe to call on every scan — it self-throttles.
    """
    import os, time
    lock_path = os.path.join(os.path.dirname(__file__), ".bias_calibration.lock")
    now = time.time()

    # Atomic read: open with O_EXCL to detect concurrent writes
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        last_run = 0.0
    except FileExistsError:
        # Lock file exists — another process is running or just finished
        try:
            with open(lock_path, "r") as f:
                last_run = float(f.read().strip())
        except (ValueError, OSError):
            last_run = 0.0

    if now - last_run < min_interval:
        return False

    # Check we have enough resolved trades
    results = validate_all()
    final_bias = compute_city_bias(results, min_samples=min_samples)
    if not final_bias:
        return False  # not enough data yet

    # Write to DB only (live calibration — no forecast.py change)
    write_to_db(final_bias)

    # Atomic write: touch the lock
    try:
        with open(lock_path, "w") as f:
            f.write(str(int(now)))
    except OSError:
        pass  # best-effort lock update

    print(f"[ECM] Bias auto-update complete ({len(final_bias)} cities calibrated)")
    return True


if __name__ == "__main__":
    main()
