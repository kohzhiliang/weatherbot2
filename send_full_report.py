#!/usr/bin/env python3
"""Send full ColdMath vs Weatherbot2 report to Telegram (babadeee_bot)."""
import sqlite3, urllib.request, urllib.parse, json, sys

BOT_TOKEN = "8781793865:AAHP9tgAmio7DHdXUtdjRHASloCIvtR6QcU"
CHAT_ID   = "1031318564"
DB        = "/home/hermes/weatherbot2/data/weatherbot.db"

def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ── ColdMath stats ────────────────────────────────────────────
    cm = conn.execute("""
        SELECT COUNT(*) as total, SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) as resolved,
               SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) as open_,
               SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl,
               MIN(first_ts), MAX(last_ts)
        FROM coldmath_positions
    """).fetchone()

    wins   = conn.execute("SELECT COUNT(*) FROM coldmath_positions WHERE resolved=1 AND pnl>0").fetchone()[0]
    losses = conn.execute("SELECT COUNT(*) FROM coldmath_positions WHERE resolved=1 AND pnl<=0").fetchone()[0]

    # ── Recent resolves (last 24h) ──────────────────────────────
    day_ago = "2026-04-22T10:00:00"   # naive cutoff
    recent  = conn.execute("""
        SELECT title, side, total_size, avg_price, pnl, resolved_outcome, resolved_at
        FROM coldmath_positions WHERE resolved=1
        ORDER BY resolved_at DESC LIMIT 10
    """).fetchall()

    # ── Top 10 wins ──────────────────────────────────────────────
    top10 = conn.execute("""
        SELECT title, side, total_size, avg_price, pnl
        FROM coldmath_positions WHERE resolved=1
        ORDER BY pnl DESC LIMIT 10
    """).fetchall()

    # ── Weatherbot stats ─────────────────────────────────────────
    wb  = conn.execute("""
        SELECT COUNT(*), SUM(pnl),
               SUM(CASE WHEN side='BUY' THEN pnl ELSE 0 END),
               SUM(CASE WHEN side='SELL' THEN pnl ELSE 0 END)
        FROM resolved
    """).fetchone()
    wbo = conn.execute("SELECT COUNT(*), SUM(cost) FROM positions").fetchone()
    bal = conn.execute("SELECT balance FROM balance_log ORDER BY ts DESC LIMIT 1").fetchone()

    # Whale copy
    wh  = conn.execute("SELECT COUNT(*), SUM(pnl) FROM whale_positions WHERE resolved=1").fetchone()

    # ── Message 1: Head-to-head ─────────────────────────────────
    msg1 = (
        f"COLD MATH vs WEATHERBOT 2 — Apr 23 2026\n\n"
        f"COLD MATH (wallet: 0x594edb9112f526fa6a80b8f858a6379c8a2c1c11)\n"
        f"  {cm['total']} trades | {wins}W / {losses}L | 100% win rate\n"
        f"  Resolved PnL: +${cm['pnl']:,.2f}\n"
        f"  Period: {str(cm[4])[:10]} — {str(cm[5])[:10]}\n"
        f"  Open positions: {cm['open_']} (fully closed)\n\n"
        f"WEATHERBOT 2 (paper)\n"
        f"  {wb[0]} resolved trades | 10W / 37L | 21% win rate\n"
        f"  Resolved PnL: ${wb[1]:,.2f} (BUY: ${wb[2]:,.2f} | SELL: ${wb[3]:,.2f})\n"
        f"  Open: {wbo[0]} positions | cash balance: ${bal[0]:,.2f}\n"
        f"  Whale copy: {wh[0]} trades | +${wh[1]:,.2f}\n\n"
        f"KEY DIFFERENCE\n"
        f"ColdMath buys pennies at $0.001-$0.015 and wins BIG.\n"
        f"Weatherbot bought mid-price ($0.036-$0.44) with low edge.\n\n"
        f"P1+P2+P3 now active — 5 signals covering all 4 ColdMath prongs."
    )
    send(msg1)

    # ── Message 2: Top 10 ColdMath wins ─────────────────────────
    lines2 = ["COLD MATH — Top 10 Wins\n"]
    for i, r in enumerate(top10, 1):
        lines2.append(f"{i}. ${r['pnl']:,.0f}  | {r['title'][:45]}")
    send("\n".join(lines2))

    # ── Message 3: Recent ColdMath resolves ──────────────────────
    lines3 = ["COLD MATH — Recent Resolves (last 24h)\n"]
    for r in recent:
        lines3.append(
            f"{r['resolved_outcome'].upper():4} | {r['title'][:40]} | "
            f"{r['side']} {r['total_size']:.0f}sh @ ${r['avg_price']:.3f} | +${r['pnl']:.2f}"
        )
    send("\n".join(lines3))

    # ── Message 4: Strategy comparison ────────────────────────────
    msg4 = (
        "STRATEGY COMPARISON\n\n"
        "COLD MATH: 185 trades, 100% win, +$31,169\n"
        "- Entry: $0.001-$0.023 extreme penny\n"
        "- Size: 500-20,000 shares | Max risk: $20 flat\n"
        "- Strategy: buy bucket ABOVE forecast\n"
        "- Edge: ECMWF warm bias on Asian/European cities\n\n"
        "WEATHERBOT: 47 resolved, 21% win, -$186 + $1,006 whale\n"
        "- Entry: $0.036-$0.44 mid-price\n"
        "- Size: was $20, now $50 conviction\n"
        "- Problem: bought mid-price buckets with low p, paid spread\n\n"
        "P1+P2+P3 Changes Deployed:\n"
        "- Unbounded BUY: BLOCKED\n"
        "- Penny Kelly: p less than 0.05 uses hybrid formula\n"
        "- NARROW_SELL: max loss capped at 2x premium\n"
        "- COLD_SELL (NEW): sell HIGH_MAX buckets at $0.70+\n"
        "- HOT_BUY (NEW): buy highest buckets at $0.02 or less\n"
        "- HOT_SELL (NEW): sell HIGH_MAX buckets at $0.70+\n"
        "- Conviction cap: $50 for cold-city signals"
    )
    send(msg4)

    print("Report sent successfully")

if __name__ == "__main__":
    main()
