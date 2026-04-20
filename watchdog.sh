#!/bin/bash
# Weatherbot2 Watchdog — monitors liveness AND activity, restarts on hang
# Run: ./watchdog.sh start | stop | status

set -e

BOT_DIR="/Users/zhiliangkoh/Desktop/weatherbot2"
LOG_FILE="$BOT_DIR/logs/watchdog.log"
PID_FILE="$BOT_DIR/.watchdog.pid"
BOT_PID_FILE="$BOT_DIR/.bot.pid"
RESTART_LOG="/tmp/weatherbot_restarts.txt"
MAX_RESTARTS=10
RESTART_WINDOW=300
STALE_SECONDS=150

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

get_bot_pid() {
    [ -f "$BOT_PID_FILE" ] && cat "$BOT_PID_FILE" || echo ""
}

is_running() {
    pid=$(get_bot_pid)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

get_last_log_time() {
    [ -f "$BOT_DIR/logs/bot.log" ] && stat -f %m "$BOT_DIR/logs/bot.log" 2>/dev/null || echo "0"
}

get_last_log_size() {
    [ -f "$BOT_DIR/logs/bot.log" ] && stat -f %s "$BOT_DIR/logs/bot.log" 2>/dev/null || echo "0"
}

start_bot() {
    log "=== Starting weatherbot2 ==="
    
    if [ -f "$BOT_DIR/logs/bot.log" ]; then
        last_mod=$(get_last_log_time)
        now=$(date +%s)
        age=$((now - last_mod))
        if [ "$age" -gt 1200 ]; then
            log "Log stale (${age}s), archiving..."
            mv "$BOT_DIR/logs/bot.log" "$BOT_DIR/logs/bot_$(date +%Y%m%d_%H%M%S).log"
        fi
    fi
    
    cd "$BOT_DIR"
    ./run.sh
    sleep 2
    
    if is_running; then
        log "Bot started, PID=$(get_bot_pid)"
    else
        log "ERROR: Bot failed to start"
    fi
}

stop_bot() {
    pid=$(get_bot_pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "Stopping bot PID=$pid"
        kill "$pid" 2>/dev/null || true
        sleep 2
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$BOT_PID_FILE"
}

count_recent_restarts() {
    now=$(date +%s)
    count=0
    [ -f "$RESTART_LOG" ] || touch "$RESTART_LOG"
    while read ts; do
        [ -n "$ts" ] && [ $((now - ts)) -lt "$RESTART_WINDOW" ] && count=$((count + 1))
    done < "$RESTART_LOG"
    echo "$count"
}

record_restart() {
    echo "$(date +%s)" >> "$RESTART_LOG"
}

prune_restart_log() {
    now=$(date +%s)
    tmp=$(mktemp)
    [ -f "$RESTART_LOG" ] || touch "$RESTART_LOG"
    while read ts; do
        [ -n "$ts" ] && [ $((now - ts)) -lt "$RESTART_WINDOW" ] && echo "$ts"
    done < "$RESTART_LOG" > "$tmp"
    mv "$tmp" "$RESTART_LOG"
}

check_flood() {
    count=$(count_recent_restarts)
    if [ "$count" -ge "$MAX_RESTARTS" ]; then
        log "!!! RESTART FLOOD: $count restarts in ${RESTART_WINDOW}s — pausing for manual review"
        return 1
    fi
    return 0
}

monitor() {
    log "Watchdog active, PID=$$"
    echo $$ > "$PID_FILE"
    mkdir -p "$BOT_DIR/logs"
    touch "$LOG_FILE"
    prune_restart_log
    
    last_size=$(get_last_log_size)
    stale_count=0
    
    while true; do
        if ! is_running; then
            log "Bot not running"
            record_restart
            prune_restart_log
            
            if ! check_flood; then
                log "Watchdog pausing 60s..."
                sleep 60
                exit 1
            fi
            
            log "Restart #$(count_recent_restarts) — starting bot..."
            start_bot
            stale_count=0
            last_size=$(get_last_log_size)
        else
            current_size=$(get_last_log_size)
            delta=$((current_size - last_size))
            
            if [ "$delta" -gt 0 ]; then
                last_size=$current_size
                stale_count=0
            else
                stale_count=$((stale_count + 30))
                if [ "$stale_count" -ge "$STALE_SECONDS" ]; then
                    log "!!! Bot STUCK (no output for ${stale_count}s) — killing and restarting"
                    stop_bot
                    record_restart
                    prune_restart_log
                    
                    if ! check_flood; then
                        log "Watchdog pausing 60s..."
                        sleep 60
                        exit 1
                    fi
                    
                    log "Restarting..."
                    start_bot
                    stale_count=0
                    last_size=$(get_last_log_size)
                fi
            fi
        fi
        
        sleep 30
    done
}

case "$1" in
    start)
        if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
            echo "Watchdog already running, PID=$(cat $PID_FILE)"
        else
            log "Starting watchdog..."
            nohup bash "$0" monitor > "$BOT_DIR/logs/watchdog_stdout.log" 2>&1 &
            sleep 1
            [ -f "$PID_FILE" ] && echo "Watchdog running, PID=$(cat $PID_FILE)"
        fi
        ;;
    stop)
        log "Stopping watchdog..."
        [ -f "$PID_FILE" ] && kill $(cat "$PID_FILE") 2>/dev/null || true
        rm -f "$PID_FILE"
        stop_bot
        log "Watchdog stopped"
        ;;
    status)
        if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
            echo "Watchdog: running, PID=$(cat $PID_FILE)"
        else
            echo "Watchdog: not running"
        fi
        if is_running; then
            pid=$(get_bot_pid)
            age=$(($(date +%s) - $(get_last_log_time)))
            echo "Bot: running, PID=$pid, last log ${age}s ago"
        else
            echo "Bot: not running"
        fi
        ;;
    restart)
        "$0" stop
        sleep 2
        "$0" start
        ;;
    monitor)
        monitor
        ;;
    *)
        echo "Usage: ./watchdog.sh start|stop|status|restart"
        ;;
esac
