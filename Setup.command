#!/bin/zsh
set -e
cd "${0:A:h}"
if ! command -v uv >/dev/null 2>&1; then
  print 'This setup uses uv. Install it with: brew install uv'
  read '?Press Enter to close.'
  exit 1
fi
uv sync --locked --no-dev
.venv/bin/python bot.py setup
read '?Press Enter to close.'
