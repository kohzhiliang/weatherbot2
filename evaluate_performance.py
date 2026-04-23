#!/usr/bin/env python3
"""
Weatherbot2 Performance Evaluation
Queries the SQLite DB, computes all metrics, sends report to Telegram.
"""
import sqlite3
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone, timedelta

# ---- Config ----
DB = '/home/hermes/weatherbot2/data/weatherbot.db'
TOKEN = '8773489569:AAH5TyR4N_EwRoLTiG6A7Z8VHCpTwTyR4'
CHAT_ID = '1031318564'

CELSIUS_CITIES = {
    'singapore', 'shanghai', 'paris', 'seoul', 'tokyo', 'munich',
    'ankara', 'lucknow', 'tel-aviv', 'toronto', 'sao-paulo',
    'buenos-aires', 'wellington', 'london'
}


def temp_str(city, bucket_low, bucket_high):
    unit = '°C' if city.lower() in CELSIUS_CITIES else '°F'
    if bucket_low == bucket_high:
        return f"{bucket_low:.0f}{unit}"
    return f"{bucket_low:.0f}-{bucket_high:.0f}{unit}"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id': CHAT_ID,
        'text': text,
        'parse_mode': 'HTML'
    }).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main():
    conn = sqlite3.connect(DB)
    now_utc = datetime.now(timezone.utc)
    one_day_ago = (now_utc - timedelta(hours=24)).isoformat()

    # ── Balance ──────────────────────────────────────────────
    bal_row = conn.execute(
        'SELECT balance, delta, reason FROM balance_log ORDER BY rowid DESC LIMIT 1'
    ).fetchone()
    balance = bal_row[0]
    last_delta = bal_row[1]
    last_reason = bal_row[2]

    # ── Open positions ───────────────────────────────────────
    open_pos = conn.execute('''
        SELECT city, bucket_low, bucket_high, entry_price, shares, cost, ev, p, forecast_src, side
        FROM positions WHERE status='open' ORDER BY ev DESC
    ''').fetchall()

    # ── Resolved (all time + 24h) ────────────────────────────
    all_resolved = conn.execute('''
        SELECT city, bucket_low, bucket_high, entry_price, exit_price, shares, pnl,
               resolved_outcome, side, resolved_at, forecast_src
        FROM resolved ORDER BY resolved_at DESC
    ''').fetchall()

    resolved_24h = conn.execute('''
        SELECT city, bucket_low, bucket_high, entry_price, exit_price, shares, pnl,
               resolved_outcome, side, resolved_at
        FROM resolved WHERE resolved_at >= ? ORDER BY resolved_at DESC
    ''', (one_day_ago,)).fetchall()

    # ── Whale stats ───────────────────────────────────────────
    whale_pnl = conn.execute(
        "SELECT SUM(delta) FROM balance_log WHERE reason LIKE 'whale_win%'"
    ).fetchone()[0] or 0.0
    whale_open = conn.execute(
        'SELECT COUNT(*) FROM whale_positions WHERE resolved=0'
    ).fetchone()[0]
    whale_closed = conn.execute(
        'SELECT COUNT(*) FROM whale_positions WHERE resolved=1'
    ).fetchone()[0]
    whale_total_pnl = conn.execute(
        'SELECT SUM(pnl) FROM whale_positions'
    ).fetchone()[0] or 0.0
    whale_breakdown = conn.execute('''
        SELECT whale_name, SUM(pnl), COUNT(*)
        FROM whale_positions GROUP BY whale_name ORDER BY SUM(pnl) DESC
    ''').fetchall()

    # ── Strategy P&L ──────────────────────────────────────────
    total_pnl = sum(r[6] for r in all_resolved)
    wins = [r for r in all_resolved if r[6] > 0]
    losses = [r for r in all_resolved if r[6] <= 0]
    win_rate = len(wins) / len(all_resolved) * 100 if all_resolved else 0
    pnl_24h = sum(r[6] for r in resolved_24h)

    best = max(all_resolved, key=lambda r: r[6]) if all_resolved else None
    worst = min(all_resolved, key=lambda r: r[6]) if all_resolved else None

    # P&L by source
    source_pnl = {}
    for r in all_resolved:
        src = r[10] or 'unknown'
        if src not in source_pnl:
            source_pnl[src] = {'pnl': 0, 'count': 0, 'wins': 0}
        source_pnl[src]['pnl'] += r[6]
        source_pnl[src]['count'] += 1
        if r[6] > 0:
            source_pnl[src]['wins'] += 1

    conn.close()

    # ── Build message ─────────────────────────────────────────
    lines = [
        "📊 <b>Weatherbot2 Performance Report</b>",
        f"🕐 {now_utc.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "💰 <b>Balance</b>",
        f"  ${balance:,.2f}  (Δ${last_delta:+.2f} · {last_reason})",
        "",
        "📋 <b>Open Positions</b>",
    ]

    if open_pos:
        for p in open_pos:
            city, blo, bhi, entry, shares, cost, ev, prob, src, side = p
            bucket_str = temp_str(city, blo, bhi)
            cost_abs = abs(cost)
            lines.append(
                f"  {side} {city} {bucket_str} @ ${entry:.3f}  "
                f"EV={ev:.1f}×  Kelly={prob:.1f}×  ${cost_abs:.2f}  [{src}]"
            )
    else:
        lines.append("  None")

    lines += ["", "📈 <b>Strategy P&L</b>"]
    lines.append(
        f"  {len(all_resolved)} resolved  |  "
        f"{len(wins)}W/{len(losses)}L  |  "
        f"Win rate {win_rate:.1f}%  |  "
        f"Total P&L ${total_pnl:+.2f}"
    )
    lines.append(f"  24h: {len(resolved_24h)} resolved  |  P&L ${pnl_24h:+.2f}")

    if best:
        lines.append(
            f"  🏆 Best: {best[0]} {temp_str(best[0], best[1], best[2])}  "
            f"${best[6]:+.2f}"
        )
    if worst:
        lines.append(
            f"  💩 Worst: {worst[0]} {temp_str(worst[0], worst[1], worst[2])}  "
            f"${worst[6]:+.2f}"
        )

    lines += ["", "🔍 <b>By Source</b>"]
    for src, d in sorted(source_pnl.items(), key=lambda x: -x[1]['pnl']):
        wr = d['wins'] / d['count'] * 100 if d['count'] else 0
        lines.append(
            f"  {src}: {d['count']} trades  |  {d['wins']}W  |  "
            f"WR {wr:.0f}%  |  ${d['pnl']:+.2f}"
        )

    lines += ["", "🐋 <b>Whale Copy</b>"]
    lines.append(
        f"  Open: {whale_open}  |  Closed: {whale_closed}  |  "
        f"Total P&L ${whale_total_pnl:+.2f}  (log ${whale_pnl:+.2f})"
    )
    for wname, wp, wc in whale_breakdown:
        lines.append(f"  {wname}: {int(wc)} trades  |  ${wp:+.2f}")

    msg = "\n".join(lines)
    print(msg)
    print("\n" + "="*50)
    print("Sending to Telegram...")
    result = send_telegram(msg)
    print(f"✅ Sent! Message ID: {result.get('result', {}).get('message_id')}")


if __name__ == '__main__':
    main()
