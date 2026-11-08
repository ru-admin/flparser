#!/bin/sh
# Точка входа контейнера: стартовая проверка, затем планировщик supercronic.
set -eu

: "${CRON_PARSE:?CRON_PARSE не задан}"
: "${CRON_STATS:?CRON_STATS не задан}"

# Стартовая проверка конфигурации, БД и лога. Пустой FL_BOT_TOKEN — статистика не уходит в Telegram.
FL_BOT_TOKEN= python3 /app/flparser.py --stats

CRONTAB=/tmp/crontab
printf '%s /app/flparser\n%s /app/flparser --stats-today\n' "$CRON_PARSE" "$CRON_STATS" > "$CRONTAB"

exec supercronic "$CRONTAB"
