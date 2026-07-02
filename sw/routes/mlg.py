"""MLG easter-egg kill leaderboard routes."""
import json
import re
import time

from flask import jsonify, request

from sw.auth import current_user_email
from sw.config import DATA_ROOT
from sw.core import app

# ── MLG kill leaderboard ─────────────────────────────────────────────────────
# Per-user kill stats live in <DATA_ROOT>/<email-slug>/mlg_stats.json.
# Schema: {"kills": int, "first_kill": ts, "last_kill": ts, "by_target": {kind: count}}
def _mlg_stats_path(email):
    safe = re.sub(r'[^a-z0-9]+', '_', (email or '').lower()).strip('_') or 'anon'
    return DATA_ROOT / safe / 'mlg_stats.json'

def _load_mlg_stats(email):
    p = _mlg_stats_path(email)
    if not p.exists():
        return {'kills': 0, 'first_kill': None, 'last_kill': None, 'by_target': {}}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {'kills': 0, 'first_kill': None, 'last_kill': None, 'by_target': {}}

def _save_mlg_stats(email, stats):
    p = _mlg_stats_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stats, indent=2))


@app.route('/api/mlg/kill', methods=['POST'])
def api_mlg_kill():
    """Increment current user's MLG kill counter. Idempotent on bad input —
    body is optional ({"target": "snoop"} or empty)."""
    email = current_user_email()
    if not email:
        return jsonify({'error': 'auth required'}), 401
    body = request.json or {}
    target = (body.get('target') or 'unknown').strip().lower()[:32] or 'unknown'
    stats = _load_mlg_stats(email)
    now = int(time.time())
    stats['kills'] = int(stats.get('kills') or 0) + 1
    if not stats.get('first_kill'):
        stats['first_kill'] = now
    stats['last_kill'] = now
    by_target = stats.setdefault('by_target', {})
    by_target[target] = int(by_target.get(target) or 0) + 1
    _save_mlg_stats(email, stats)
    return jsonify({'ok': True, 'total': stats['kills'], 'by_target': by_target})


@app.route('/api/mlg/leaderboard', methods=['GET'])
def api_mlg_leaderboard():
    """Returns top users by kill count. No auth check (everyone in the org
    can see the board) but 401 when not logged in.
    Iterates user dirs under DATA_ROOT looking for mlg_stats.json — small
    enough for our team that O(N) scan is fine."""
    if not current_user_email():
        return jsonify({'error': 'auth required'}), 401
    rows = []
    if DATA_ROOT.exists():
        for user_dir in DATA_ROOT.iterdir():
            if not user_dir.is_dir() or user_dir.name.startswith('.'):
                continue
            sf = user_dir / 'mlg_stats.json'
            if not sf.exists():
                continue
            try:
                s = json.loads(sf.read_text())
            except Exception:
                continue
            kills = int(s.get('kills') or 0)
            if kills <= 0:
                continue
            # Recover original email from slug — best-effort. Actual stored
            # email isn't kept, but slug is reversible enough for display
            # (replace _ with various punctuation guesses). For now, return
            # the slug AS IS — operator-only data, clarity > prettiness.
            rows.append({
                'user': user_dir.name,
                'kills': kills,
                'last_kill': s.get('last_kill'),
                'by_target': s.get('by_target') or {},
            })
    rows.sort(key=lambda r: (-r['kills'], r['last_kill'] or 0))
    return jsonify({'leaderboard': rows[:50], 'total_users': len(rows)})


