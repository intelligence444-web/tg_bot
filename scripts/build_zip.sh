#!/usr/bin/env bash
# Собирает dist/bot.zip для Cloud Functions: в корне архива только index.py, пакет bot/ и requirements.txt.
# Зависимости Cloud Functions ставит сам по requirements.txt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p dist
rm -f dist/bot.zip
zip -r -q dist/bot.zip index.py bot requirements.txt -x '*__pycache__*' '*.pyc'
echo "Готово: $ROOT/dist/bot.zip"
