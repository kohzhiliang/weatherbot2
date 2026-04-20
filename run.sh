#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p logs data
python3 -u src/main.py >> logs/bot.log 2>&1 &
echo "Weatherbot PID: $!"
echo $! > .bot.pid
echo "Log: logs/bot.log"
