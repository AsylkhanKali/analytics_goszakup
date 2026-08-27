#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

if [ -d "$DIR/venv" ]; then
    PYTHON="$DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

echo "Starting Goszakup Full Scraper Daemon at $(date)..." >> "$DIR/scraper_full.log"
exec "$PYTHON" -u "$DIR/scraper_full.py" --start 2024-01-01 --end 2026-08-31 --concurrency 30 >> "$DIR/scraper_full.log" 2>&1
