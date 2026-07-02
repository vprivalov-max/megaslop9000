#!/bin/zsh
# Double-click to start the Series Writer dev server on http://localhost:8080
# Kills any stale instance first so two processes never share the port.
cd "$(dirname "$0")" || exit 1

echo "→ stopping any running instance..."
pkill -f "python3.14 app.py" 2>/dev/null
sleep 1

PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
echo "→ starting Series Writer on http://localhost:8080"
echo "  (leave this window open; press Ctrl+C to stop)"
exec "$PY" app.py
