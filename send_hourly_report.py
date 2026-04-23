#!/usr/bin/env python3
"""Weatherbot2 hourly report — sent via babadeee_bot."""
import sqlite3, urllib.request, urllib.parse, json
from datetime import datetime, timezone, timedelta

BOT_TOKEN = "8781793865:AAHP9tgAmio7DHdXUtdjRHASloCIvtR6QcU"
CHAT_ID   = "1031318564"
DB        = "/home/hermes/weatherbot2/data/weatherbot.db"

CELSIUS_CITIES = {
    'singapore','shanghai','paris','seoul','tokyo','munich','ankara',
    'lucknow','tel-aviv','toronto','sao-paulo','buenos-aires',
    'wellington','london'
}

def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
        if not result.get("ok"):
            print("Telegram error:", result)
        return result

conn = sqlite3.connect(DB)

# ── Balance ──────────────────────────────────────────────────────────
bal_row = conn.execute(
    "SELECT balance, delta, reason FROM balance_log ORDER BY rowid DESC LIMIT 1"
).fetchone()
balance    = bal_row[0]
last_delta = bal_row[1]
last_reason = bal_row[2] or ""

# ── Open positions ───────────────────────────────────────────────────
open_pos = conn.execute("""
    SELECT city, bucket_low, bucket_high, entry_price, shares, cost, ev, p, forecast_src, side
    FROM positions WHERE status="open"
    ORDER BY ev DESC
""").fetchall()

# ── Resolved (last ~24h) ──────────────────────────────────────────────
one_day_ago = (datetime.now(timezone.utc) - timedelta(hours=26)).isoformat()
resolved = conn.execute("""
    SELECT city, bucket_low, bucket_high, entry_price, exit_price,
           shares, pnl, resolved_outcome, side, resolved_at
    FROM resolved
    WHERE resolved_at > ?
    ORDER BY resolved_at DESC
""", (one_day_ago,)).fetchall()

# ── Whale copy ───────────────────────────────────────────────────────
whale_closed = conn.execute("""
    SELECT whale_name, COUNT(*), SUM(pnl), SUM(cost)
    FROM whale_positions WHERE resolved=1 GROUP BY whale_name
""").fetchall()
whale_open = conn.execute("""
    SELECT whale_name, COUNT(*), SUM(cost)
    FROM whale_positions WHERE resolved=0 GROUP BY whale_name
""").fetchall()
whale_total_pnl = conn.execute(
    "SELECT COALESCE(SUM(delta), 0) FROM balance_log WHERE reason LIKE 'whale_win%'"
).fetchone()[0]

# ── Strategy P&L ─────────────────────────────────────────────────────
weather_pnl  = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM resolved").fetchone()[0]
weather_cnt  = conn.execute("SELECT COUNT(*) FROM resolved").fetchone()[0]
whale_wins   = conn.execute(
    "SELECT COUNT(*) FROM balance_log WHERE reason LIKE 'whale_win%'"
).fetchone()[0]

# ── Watchdog PID ─────────────────────────────────────────────────────
import os
wd_pid = "unknown"
wd_path = "/home/hermes/weatherbot2/.watchdog.pid"
if os.path.exists(wd_path):
    wd_pid = open(wd_path).read().strip()

# ── Build message ────────────────────────────────────────────────────
lines = []
lines.append(f"🌤 <b>Weatherbot2 Report</b>")
lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
lines.append(f"")

lines.append(f"💰 <b>Balance:</b> ${balance:,.2f}")
if last_delta != 0:
    sign = "+" if last_delta > 0 else ""
    lines.append(f"   Last: {sign}${last_delta:,.2f} ({last_reason[:50]})")
lines.append(f"")

# ── Resolved (24h) ────────────────────────────────────────────────────
if resolved:
    total_pnl = sum(r[6] for r in resolved if r[6] is not None)
    wins  = [r for r in resolved if r[6] and r[6] > 0]
    loss  = [r for r in resolved if r[6] and r[6] < 0]
    lines.append(f"📊 <b>Resolved (24h)</b> — {len(resolved)} trades | PnL: {total_pnl:+.2f} ({len(wins)}W / {len(loss)}L)")
    for r in resolved:
        unit   = '°C' if r[0].lower() in CELSIUS_CITIES else '°F'
        bucket = f"{r[1]:.0f}-{r[2]:.0f}{unit}"
        emoji  = "✅" if r[6] and r[6] > 0 else "❌"
        lines.append(
            f"   {emoji} {r[0].title()} {bucket} | {r[8]} @ "
            f"${r[3]:.4f}→${r[4]:.4f} | {r[6]:+.2f}"
        )
    lines.append(f"")

# ── Open positions ───────────────────────────────────────────────────
if open_pos:
    total_cost = sum(r[5] for r in open_pos)
    lines.append(f"📋 <b>Open Positions</b> — {len(open_pos)} | Tied: ${total_cost:,.2f}")
    for p in open_pos[:10]:
        unit   = '°C' if p[0].lower() in CELSIUS_CITIES else '°F'
        bucket = f"{p[1]:.0f}-{p[2]:.0f}{unit}" if p[1] != -999 else f"??-{p[2]:.0f}{unit}"
        ev_pct  = (p[6] or 0) * 100
        lines.append(
            f"   • {p[0].title()} {bucket} | {p[9]} | "
            f"EV={ev_pct:.0f}% | p={p[7]*100:.0f}% | ${p[5]:.2f}"
        )
    if len(open_pos) > 10:
        lines.append(f"   ...and {len(open_pos) - 10} more")
    lines.append(f"")

# ── Whale copy ───────────────────────────────────────────────────────
whale_closed_total = sum(r[2] for r in whale_closed) if whale_closed else 0
whale_open_cost    = sum(r[2] for r in whale_open)    if whale_open    else 0
lines.append(f"🐋 <b>Whale Copy</b>")
lines.append(f"   Closed: {sum(r[1] for r in whale_closed) or 0} trades | P&L: ${whale_closed_total:,.2f}")
lines.append(f"   Open: {sum(r[1] for r in whale_open) or 0} positions | Cost: ${whale_open_cost:,.2f}")
if whale_closed:
    for w in sorted(whale_closed, key=lambda x: x[2], reverse=True):
        lines.append(f"   • {w[0]}: {w[1]} trades | ${w[2]:+.2f}")
lines.append(f"")

# ── Strategy P&L ─────────────────────────────────────────────────────
combined = weather_pnl + whale_total_pnl
lines.append(f"📈 <b>Strategy P&L (all time)</b>")
lines.append(f"   Weather: ${weather_pnl:,.2f} over {weather_cnt} resolved trades")
lines.append(f"   Whale:   ${whale_total_pnl:,.2f} over {whale_wins} wins")
lines.append(f"   Combined: ${combined:,.2f}")
lines.append(f"")

lines.append(f"🔧 Bot running on VPS | Watchdog: {wd_pid}")

msg = '\n'.join(lines)
send(msg)
print("Hourly report sent ✓")
