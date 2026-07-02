import json
import os
import sys

# ── macOS fork-safety ────────────────────────────────────────────────────
# This process is multithreaded (ThreadPoolExecutor drives the QC pipeline
# and autogen). When a worker thread shells out via subprocess (fork+exec of
# ffmpeg / `open` / ffprobe), macOS's Objective-C runtime detects a fork from
# a multithreaded Obj-C-initialized process and SIGKILLs the child *between*
# fork and exec — crash signature:
#     Termination Reason: Namespace OBJC, Code 1
#     "crashed on child side of fork pre-exec"  (Thread: ThreadPoolExecutor-0_0)
# The Obj-C runtime gets initialized in the parent by `requests`/urllib doing
# a CFNetwork system-proxy lookup. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
# tells the runtime not to abort the forked child.
#
# CRITICAL: libobjc reads this env var exactly once, when its runtime
# initializes (_objc_init) — which on a Python.framework build can happen at
# interpreter startup, BEFORE this module runs. Setting os.environ here would
# then be too late and the crash returns. So when launched directly (dev:
# `python app.py`) and the var isn't already present, we re-exec the
# interpreter with the var set in the environment — guaranteeing it's seen
# before libobjc initializes. The value-check guards against an exec loop and
# also covers Werkzeug's debug reloader child (which inherits the env).
if __name__ == '__main__' and os.environ.get('OBJC_DISABLE_INITIALIZE_FORK_SAFETY') != 'YES':
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    os.execv(sys.executable, [sys.executable] + sys.argv)
# Production runs under gunicorn (imports this module, __name__ != '__main__')
# on Linux, where this whole class of crash doesn't exist — setdefault is a
# harmless no-op signal there. Local `gunicorn app:app` on macOS would still
# want the var in its launch env; the re-exec above only covers `python app.py`.
os.environ.setdefault('OBJC_DISABLE_INITIALIZE_FORK_SAFETY', 'YES')

import re
import uuid
import random
import secrets
import shutil
import copy
import datetime
import time
import subprocess
import threading
import hashlib
import requests
from collections import Counter
from pathlib import Path
from functools import wraps
from urllib.parse import urlencode
from flask import (Flask, request, jsonify, render_template, send_from_directory,
                   session, redirect, url_for, abort)
from werkzeug.utils import secure_filename
try:
    from authlib.integrations.flask_client import OAuth
    _AUTHLIB_AVAILABLE = True
except ImportError:
    _AUTHLIB_AVAILABLE = False


from sw.utils import (
    _TRANSLIT,
    slugify,
    asset_name,
)

from sw.scriptparse import (
    _SCENE_HEADING_FORMAL_RE,
    _SCENE_HEADING_INFER_RE,
    _SLUG_BLOCKLIST_RE,
    _TRANSITION_PREFIX_RE,
    _LOWERCASE_LETTER_RE,
    _UPPERCASE_LETTER_RE,
    _is_all_caps_slug,
    _is_bracket_slug,
    is_scene_heading,
)

from sw.core import app, STATIC_VERSION

from sw.config import (
    BASE,
    DATA_ROOT,
    LEGACY_PROJECTS,
    CONFIG_FILE,
    RETELLER_API,
    AVAI_API,
    _read_config_field,
    _load_secret,
    PRIMARY_USER_EMAIL,
    _user_keys_path,
    _load_user_keys,
    _save_user_keys,
)

# ── Per-user app settings (auto-revise instruction, future toggles) ──────────
# Stored separately from keys.json so concerns don't mix. Default-text mirrors
# the colleague's `DEFAULT_AUTO_REVISE_INSTRUCTION` (src/shared/lib/auto-revise.ts)
# but extended per the operator's preferred wording (see /Volumes/T7 S 2TB
# screenshot — Settings → «Автоматическая правка» tab).
from sw.config import (
    DEFAULT_AUTO_REVISE_INSTRUCTION,
    _user_settings_path,
    _load_user_settings,
    _save_user_settings,
)

from sw.auth import (
    _thread_keys,
    _get_user_avai_key,
    _get_user_reteller_key,
    _capture_user_keys,
    _spawn_with_keys,
)

from sw.config import (
    ANTHROPIC_KEY,
    AVAI_KEY,
    RETELLER_KEY,
    ELEVENLABS_KEY,
    OPENAI_KEY,
    STRICT_CHAR_FILTER,
)

from sw.state import (
    _RENDER_CONCURRENCY,
    RENDER_SEMAPHORE,
    _render_queue_depth,
)
from sw.recovery import (
    _SUBMITTING_TIMEOUT_SEC,
    _SUBMITTING_TIMEOUT_RUNTIME_SEC,
    _recover_inflight_chunks,
)
from sw.state import ALLOWED_EXTENSIONS


from sw.auth import (
    AUTH_ALLOWED_DOMAIN,
    DEV_USER_EMAIL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    AUTH_ENABLED,
    _get_or_create_secret,
    oauth,
    current_user_email,
    _is_path_allowed,
)
from sw.logging_utils import (
    _LOG_LOCK,
    _user_log_dir,
    _log_event,
)
@app.route('/api/client-log', methods=['POST'])
def client_log():
    """Frontend → backend logging bridge. Lets the JS side emit structured
    events into the same per-user JSONL log file that backend uses, so
    user-visible auto-mode milestones / errors are debuggable from the admin
    Logs viewer (instead of asking the user to open DevTools and screenshot).
    Body: {level: 'INFO'|'WARN'|'ERROR', event: str, ...fields}.
    Hard cap on field sizes to prevent abuse (chatty client could spam disk)."""
    body = request.get_json(silent=True) or {}
    level = (body.get('level') or 'INFO').upper()
    if level not in ('INFO', 'WARN', 'ERROR'):
        level = 'INFO'
    event = (body.get('event') or 'client').strip()[:80]
    fields = {}
    for k, v in body.items():
        if k in ('level', 'event'):
            continue
        if isinstance(v, str):
            fields[k[:40]] = v[:500]
        elif isinstance(v, (int, float, bool)) or v is None:
            fields[k[:40]] = v
        else:
            try:
                fields[k[:40]] = json.dumps(v)[:500]
            except Exception:
                fields[k[:40]] = str(v)[:500]
    _log_event(level, f'client.{event}', **fields)
    return jsonify({'ok': True})


@app.before_request
def _log_request_start():
    if request.path.startswith('/static/') or request.path == '/healthz':
        return None
    request._t0 = time.time()

@app.after_request
def _log_request_end(resp):
    try:
        if request.path.startswith('/static/') or request.path == '/healthz':
            return resp
        t0 = getattr(request, '_t0', None)
        ms = round((time.time() - t0) * 1000) if t0 else None
        # Skip 200s on chatty polling endpoints — log only the interesting stuff.
        if resp.status_code < 400:
            chatty = ('auto-generate/status', 'seedance/poll', 'seedance/list',
                      'auto-generate/sweep', 'import-status')
            if any(p in request.path for p in chatty):
                return resp
        level = 'ERROR' if resp.status_code >= 500 else ('WARN' if resp.status_code >= 400 else 'INFO')
        _log_event(level, 'http',
                   method=request.method, path=request.path,
                   status=resp.status_code, ms=ms,
                   ip=(request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip())
    except Exception:
        pass
    return resp

@app.errorhandler(Exception)
def _log_uncaught(e):
    try:
        import traceback
        _log_event('ERROR', 'uncaught',
                   method=request.method, path=request.path,
                   exc_type=type(e).__name__, exc_msg=str(e)[:500],
                   trace=traceback.format_exc()[-2000:])
    except Exception:
        pass
    # Re-raise so Flask's default handling still runs (returns 500 to client)
    raise


@app.route('/api/admin/logs')
def admin_logs():
    """Read recent log lines.
    - Primary user (operator): can read ANY user's logs via ?email=...
    - Regular user: can read ONLY their own logs — email param is forced
      to their own email regardless of what they pass.
    Query params:
      email   = user email or slug (primary only — non-primary forced to self)
      date    = YYYY-MM-DD (defaults to today)
      lines   = max lines to return (default 500, max 5000)
      level   = ERROR | WARN | INFO (filter)
      grep    = case-insensitive substring filter on the JSON line
    Returns {lines: [parsed_json, ...], total_lines, file}."""
    actor = current_user_email() or ''
    if not actor and AUTH_ENABLED:
        return jsonify({'error': 'auth required'}), 401
    is_primary = (actor == PRIMARY_USER_EMAIL) or (not AUTH_ENABLED)
    requested = (request.args.get('email') or actor).strip()
    target_email = requested if is_primary else actor
    date_str = (request.args.get('date') or datetime.datetime.utcnow().strftime('%Y-%m-%d')).strip()
    try:
        max_lines = max(1, min(5000, int(request.args.get('lines') or 500)))
    except ValueError:
        max_lines = 500
    level_filter = (request.args.get('level') or '').upper().strip() or None
    grep = (request.args.get('grep') or '').lower().strip() or None
    log_path = _user_log_dir(target_email) / f'{date_str}.jsonl'
    if not log_path.exists():
        return jsonify({'lines': [], 'total_lines': 0, 'file': str(log_path), 'exists': False})
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
    except Exception as e:
        return jsonify({'error': f'read failed: {e}'}), 500
    parsed = []
    for raw in all_lines:
        if grep and grep not in raw.lower():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if level_filter and obj.get('level') != level_filter:
            continue
        parsed.append(obj)
    # Tail to max_lines (most recent N)
    parsed = parsed[-max_lines:]
    return jsonify({
        'lines': parsed,
        'total_lines': len(all_lines),
        'returned': len(parsed),
        'file': str(log_path.relative_to(DATA_ROOT)),
        'exists': True,
    })


# Retention period for per-user JSONL log files. Configurable via env var.
# Default 7 days — small files (a few KB/day per active user) but enough for
# debugging recent issues. Set LOG_RETENTION_DAYS=0 to disable cleanup.
from sw.logging_utils import (
    _LOG_RETENTION_DAYS,
    _cleanup_old_logs,
    _start_log_cleanup_loop,
)
@app.route('/api/admin/users')
def admin_users():
    """List users that have any data on the server (so primary operator can
    pick from a dropdown). Gated to PRIMARY_USER_EMAIL."""
    actor = current_user_email() or ''
    if actor != PRIMARY_USER_EMAIL and not (not AUTH_ENABLED):
        return jsonify({'error': 'admin only'}), 403
    users = []
    if DATA_ROOT.exists():
        for child in sorted(DATA_ROOT.iterdir()):
            if not child.is_dir() or child.name.startswith('.'):
                continue
            log_dir = child / '_logs'
            log_files = []
            if log_dir.exists():
                log_files = sorted([p.name.replace('.jsonl', '') for p in log_dir.glob('*.jsonl')], reverse=True)[:14]
            # Try to read original email from keys file or projects dir
            users.append({
                'slug': child.name,
                'has_logs': bool(log_files),
                'log_dates': log_files,
                'project_count': len(list((child / 'projects').glob('*'))) if (child / 'projects').exists() else 0,
            })
    return jsonify({'users': users})


@app.before_request
def _require_auth():
    if not AUTH_ENABLED:
        return None  # dev bypass
    if _is_path_allowed(request.path):
        return None
    if current_user_email():
        return None
    # Browser request → redirect to login. API call (XHR/fetch) → 401 JSON.
    if request.path.startswith('/api/') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'error': 'auth_required'}), 401
    return redirect(url_for('login'))

@app.route('/login')
def login():
    if not AUTH_ENABLED:
        return redirect('/')
    if current_user_email():
        return redirect('/')
    return render_template('login.html', domain=AUTH_ALLOWED_DOMAIN)

@app.route('/auth/google')
def auth_google_start():
    if not AUTH_ENABLED:
        return redirect('/')
    redirect_uri = url_for('auth_google_callback', _external=True)
    # `hd` hint restricts the chooser to a single Workspace domain.
    return oauth.google.authorize_redirect(redirect_uri, hd=AUTH_ALLOWED_DOMAIN)

@app.route('/auth/google/callback')
def auth_google_callback():
    if not AUTH_ENABLED:
        return redirect('/')
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        return render_template('login.html', domain=AUTH_ALLOWED_DOMAIN,
                               error=f'OAuth error: {e}'), 400
    info = token.get('userinfo') or {}
    email = (info.get('email') or '').lower().strip()
    domain = email.rsplit('@', 1)[-1] if '@' in email else ''
    if not info.get('email_verified') or domain != AUTH_ALLOWED_DOMAIN:
        return render_template('login.html', domain=AUTH_ALLOWED_DOMAIN,
                               error=f'Доступ только для @{AUTH_ALLOWED_DOMAIN}. Вошли как: {email or "?"}'), 403
    session.permanent = True
    session['user'] = {
        'email': email,
        'name': info.get('name') or email.split('@')[0],
        'picture': info.get('picture') or '',
    }
    return redirect('/')

@app.route('/auth/logout', methods=['GET', 'POST'])
def auth_logout():
    session.pop('user', None)
    if AUTH_ENABLED:
        return redirect(url_for('login'))
    return redirect('/')

@app.route('/api/me')
def api_me():
    email = current_user_email()
    if not email:
        return jsonify({'email': None, 'authenticated': False}), 401
    info = (session.get('user') or {}) if AUTH_ENABLED else {'email': email, 'name': 'Local Dev'}
    return jsonify({
        'email': email,
        'authenticated': True,
        'name': info.get('name') or email.split('@')[0],
        'picture': info.get('picture') or '',
        'auth_enabled': AUTH_ENABLED,
        'domain': AUTH_ALLOWED_DOMAIN,
        # Per-user API key flags — FE uses these to gate generation + show
        # setup modal on first login.
        'is_primary': email == PRIMARY_USER_EMAIL,
        'has_avai_key':     bool(_get_user_avai_key()),
        'has_reteller_key': bool(_get_user_reteller_key()),
    })

@app.route('/healthz')
def healthz():
    return {'ok': True, 'auth': AUTH_ENABLED}


@app.route('/api/me/validate-avai-key')
def validate_avai_key():
    """Live-checks the current user's AVAI key by hitting a cheap auth-only
    endpoint (no generation, no cost). Returns:
      {state: 'ok'|'missing'|'invalid'|'unreachable', detail?, balance?}
    Frontend calls this once per page load (or on demand) so we can pop the
    fix-it modal proactively the moment we know the key is dead — instead
    of waiting for the user to click generate and getting a wall of 401."""
    key = _get_user_avai_key()
    if not key:
        return jsonify({'state': 'missing'})
    try:
        r = requests.get('https://avai-gen.com/api/public/balance',
                         headers={'x-api-key': key}, timeout=10)
    except Exception as e:
        # Network blip / AVAI downtime — don't pop a key-error modal because
        # the key might be fine. Tell FE to retry later.
        return jsonify({'state': 'unreachable', 'detail': str(e)[:200]})
    if r.status_code == 401:
        return jsonify({'state': 'invalid', 'detail': 'AVAI returned 401 Unauthorized'})
    if not r.ok:
        return jsonify({'state': 'unreachable', 'detail': f'HTTP {r.status_code}: {r.text[:120]}'})
    try:
        data = r.json()
    except Exception:
        data = {}
    return jsonify({'state': 'ok', 'balance': data.get('balance') or data.get('credits')})


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


# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {'reteller_key': ''}

from sw.llm import (
    _MODEL_ALIAS,
    WRITER_MODEL_DEFAULT,
    WRITER_MODEL_WHITELIST,
    _get_openai_client,
    _openai_ask,
    _resolve_writer_model,
    llm_ask,
    _get_anthropic_client,
    claude_ask,
    anthropic_ask,
    claude_ask_fast,
    claude_ask_quality,
    claude_web_research,
    claude_ask_vision,
)
from sw.vision import (
    _describe_character_visual,
    _backfill_uploaded_char_appearances,
    _describe_outfit_visual,
)
# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', static_v=STATIC_VERSION)

@app.route('/api/translate', methods=['POST'])
def translate_text():
    text = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'error': 'no text'}), 400
    result = claude_ask(
        f'Переведи следующий текст на русский язык. Верни только перевод, без пояснений.\n\n{text}',
        system='You are a professional literary translator. Translate accurately, preserving tone, style, and screenplay formatting if present.'
    )
    return jsonify({'translation': result})


@app.route('/api/config', methods=['GET', 'POST'])
def config_route():
    """Per-user API key storage. Anthropic stays global (operator's billing).
    AVAI / Reteller are per-user — primary user falls back to global env so
    nothing breaks for the operator. Other users must enter their own keys.
    GET returns masked status (has_X flags) — never the actual key string."""
    email = current_user_email()
    if not email:
        return jsonify({'error': 'auth required'}), 401
    if request.method == 'POST':
        body = request.json or {}
        existing = _load_user_keys(email)
        # Only update fields that were sent (non-empty); empty string = clear
        if 'avai_key' in body:
            existing['avai_key'] = (body.get('avai_key') or '').strip()
        if 'reteller_key' in body:
            existing['reteller_key'] = (body.get('reteller_key') or '').strip()
        _save_user_keys(email, existing)
        return jsonify({'ok': True,
                        'has_avai_key': bool(existing['avai_key']) or email == PRIMARY_USER_EMAIL,
                        'has_reteller_key': bool(existing['reteller_key']) or email == PRIMARY_USER_EMAIL})
    keys = _load_user_keys(email)
    return jsonify({
        'email': email,
        'is_primary': email == PRIMARY_USER_EMAIL,
        # NEVER return raw keys to client — only presence flags
        'has_avai_key': bool(_get_user_avai_key()),
        'has_reteller_key': bool(_get_user_reteller_key()),
        'avai_key_masked': ('•' * 6 + (keys['avai_key'][-4:] if keys.get('avai_key') else '')) if keys.get('avai_key') else '',
        'reteller_key_masked': ('•' * 6 + (keys['reteller_key'][-4:] if keys.get('reteller_key') else '')) if keys.get('reteller_key') else '',
    })


@app.route('/api/user/auto-revise', methods=['GET', 'POST'])
def user_auto_revise_route():
    """Per-user storage for the «🎬 Автоматическая правка» instruction used by
    Turbo-mode pipelines after batch-compose. Mirrors colleague's
    AppSettings.autoReviseInstructionRu (src/shared/lib/auto-revise.ts)."""
    email = current_user_email()
    if not email:
        return jsonify({'error': 'auth required'}), 401
    if request.method == 'POST':
        body = request.json or {}
        cur = _load_user_settings(email)
        if 'auto_revise_enabled' in body:
            cur['auto_revise_enabled'] = bool(body.get('auto_revise_enabled'))
        if 'auto_revise_instruction' in body:
            text = (body.get('auto_revise_instruction') or '').strip()
            cur['auto_revise_instruction'] = text or DEFAULT_AUTO_REVISE_INSTRUCTION
        _save_user_settings(email, cur)
        return jsonify({'ok': True, **cur, 'default': DEFAULT_AUTO_REVISE_INSTRUCTION})
    cur = _load_user_settings(email)
    return jsonify({**cur, 'default': DEFAULT_AUTO_REVISE_INSTRUCTION})


from sw.routes.series_list import (
    list_series,
    rename_out_files,
    update_series_meta,
)

from sw.routes.landmarks import (
    _norm_checkpoint,
    add_or_update_checkpoint,
    delete_checkpoint,
    generate_checkpoint,
    set_finale,
    delete_finale,
    trajectory_validation,
    generate_finale,
    _extract_landmark_character_mismatch,
    build_trajectory_block,
    finale_episode_num,
    is_finale_episode,
    build_finale_contract_block,
    _BRIDGE_CACHE,
    build_finale_bridge_plan,
    toggle_archive,
    toggle_pin,
)
# ── Import series from existing script ───────────────────────────────────────
# Lets the user paste / upload a 70-episode script and get a fully populated
# series in one shot: episodes split + chars/locs/items extracted per-episode.
# Two-step UX: (1) preview boundaries before commit, (2) bulk create + start
# background extraction worker. Status polled via /import-status.

# Regex matchers for episode-boundary detection. Tried in order; first that
# yields ≥2 matches wins. Without this fallback chain a script that uses an
# unusual marker (just "5." at the start of a line) would land in episode 1
# alone.
# NOTE: leading whitespace is `[ \t]*` (NOT `\s*`) on purpose. `\s` matches
# newlines too — with multiline `^`, a `\s*` quantifier can consume entire
# lines BETWEEN the boundary markers, swallowing actual episode body into the
# next match. Restricting to spaces/tabs keeps each match anchored to a single
# line.
# `[*_]{0,3}` tolerates markdown bold/italic wrappers like `**СЕРИЯ 1 — "TITLE"**`
# or `__Episode 5__`. Without this, a script copy-pasted from chat (where the
# author wrapped headings in `**`) silently parsed as a single mega-episode.
# User-reported bug: Natia uploaded 5-episode RU script, preview said 1.
_EPISODE_BOUNDARY_PATTERNS = [
    # Triple-equals fenced: === ЭПИЗОД 5 === / === EPISODE 5 === (±**bold**)
    r'(?im)^[ \t]*[*_]{0,3}[ \t]*={2,}[ \t]*(?:эпизод|серия|episode|ep\.?)[ \t]*(\d+)[^\n]*$',
    # Markdown headers: ## ЭПИЗОД 5 / # Episode 5
    r'(?im)^#{1,6}[ \t]*(?:эпизод|серия|episode|ep\.?)[ \t]*(\d+)[^\n]*$',
    # Plain bare line: ЭПИЗОД 5 / Episode 5 / Серия 5 / **СЕРИЯ 5 — "TITLE"**
    r'(?im)^[ \t]*[*_]{0,3}[ \t]*(?:эпизод|серия|episode|ep\.?)[ \t]+(\d+)[ \t]*[:\-—]?[ \t]*[^\n]*$',
    # Numbered with period only: 5. (when on its own line)
    r'(?m)^[ \t]*(\d+)\.[ \t]*$',
]

def _split_script_into_episodes(text):
    """Returns [{'number': int, 'title': str, 'body': str}, ...] or [] if
    no boundaries could be found. Pattern chain tries the most-specific
    markers first and falls back to looser ones."""
    if not text or not text.strip():
        return []
    for pattern in _EPISODE_BOUNDARY_PATTERNS:
        matches = list(re.finditer(pattern, text))
        if len(matches) < 2:
            continue
        episodes = []
        # Prefix content — anything before the FIRST marker. Often the user
        # pastes a series where the very first episode has no «Episode N:»
        # header (just a body or «Кратко: …» summary). Previously this got
        # silently dropped. Now: if the prefix has more than 50 non-whitespace
        # chars, treat it as a leading episode (number = first_match_num - 1,
        # or 1 if that goes < 1). Title is taken from the first non-empty line.
        first_start = matches[0].start()
        prefix = text[:first_start].strip()
        if len(re.sub(r'\s+', '', prefix)) > 50:
            try:
                first_num = int(matches[0].group(1))
            except (ValueError, IndexError):
                first_num = 2
            prefix_num = max(1, first_num - 1)
            # Title from first non-empty line of prefix (strip «Кратко:» etc).
            prefix_title = ''
            for ln in prefix.split('\n'):
                t = ln.strip()
                if t:
                    t = re.sub(r'^(кратко|brief|summary|синопсис)\s*[:\-—]\s*', '', t, flags=re.IGNORECASE)
                    prefix_title = t[:80]
                    break
            episodes.append({'number': prefix_num, 'title': prefix_title, 'body': prefix})
        for i, m in enumerate(matches):
            try:
                num = int(m.group(1))
            except (ValueError, IndexError):
                num = i + 1
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            # Title = the matched line (without the marker prefix and any
            # trailing decorators), trimmed. Strip markdown bold/italic
            # wrappers (** __ *) — for `**СЕРИЯ 1 — "YOUR CEILING"**` the
            # title should come out as `"YOUR CEILING"`, not `**"YOUR CEILING"**`.
            line = m.group(0).strip()
            title = re.sub(r'^[#=*_\s]+', '', line)                                              # leading # = * _
            title = re.sub(r'[#=*_\s]+$', '', title)                                             # trailing # = * _
            title = re.sub(r'^(эпизод|серия|episode|ep\.?)\s*\d+\s*[:\-—]?\s*', '', title, flags=re.IGNORECASE)
            title = title.strip(' \t*_#"\'')                                                     # final polish for stray quotes/decorators
            episodes.append({'number': num, 'title': title, 'body': body})
        if episodes:
            # Renumber sequentially if numbers are dup or non-monotonic.
            seen = set()
            for ep in episodes:
                if ep['number'] in seen or ep['number'] < 1:
                    ep['number'] = max(seen, default=0) + 1
                seen.add(ep['number'])
            return episodes
    # No boundaries found → treat whole text as a single episode.
    return [{'number': 1, 'title': '', 'body': text.strip()}]


from sw.scriptparse import (
    _DIALOGUE_LINE_RE,
    _ACTION_LINE_RE,
    _CYRILLIC_RE,
    _CJK_RE,
    _HIRAGANA_RE,
    _KATAKANA_RE,
    _HANGUL_RE,
    _LATIN_RE,
    _detect_dialogue_language,
    _IMPORT_STATUS,
    _IMPORT_LOCKS,
)
def _import_status(sid):
    return _IMPORT_STATUS.setdefault(sid, {
        'running': False, 'total': 0, 'done': 0, 'errors': [],
        'started_at': None, 'finished_at': None, 'current': None,
    })


def _llm_extract_episode_entities(script_text, known_chars, known_locs, known_items, series=None):
    """One LLM call per episode that returns chars + locs + items in JSON.
    Faster than running /extract-characters + /detect-items separately. Passes
    known names so the model can flag re-uses vs new entities.
    `series` (optional) carries world-context — if it describes an anthropomorphic-
    animal world, we inject a directive forcing species into every appearance."""
    if not script_text or not script_text.strip():
        return {'characters': [], 'locations': [], 'items': []}
    known_section = ''
    if known_chars or known_locs or known_items:
        known_section = (
            f"\n\nALREADY KNOWN ENTITIES (REUSE these names where the script mentions them):\n"
            f"  characters: {', '.join(sorted(known_chars)) or '(none)'}\n"
            f"  locations:  {', '.join(sorted(known_locs))  or '(none)'}\n"
            f"  items:      {', '.join(sorted(known_items)) or '(none)'}\n"
        )
    # World-context block — only emitted when the series is anthropomorphic.
    # Forces appearance text to start with the species marker so portrait gen
    # later renders an animal, not a human.
    world_block = ''
    if isinstance(series, dict) and _is_anthro_world(series):
        world_block = (
            f"\n\nSERIES WORLD CONTEXT (CRITICAL):\n"
            f"  title: {series.get('title','')}\n"
            f"  world: {(series.get('world_description') or '')[:600]}\n"
            f"  synopsis: {(series.get('synopsis') or '')[:600]}\n\n"
            + _anthro_world_block(series)
        )
    # Casting aesthetics — leads & romance/intimacy roles must read as attractive.
    # Needs synopsis context, so emit it for every series (anthro or human).
    casting_block = ''
    if isinstance(series, dict):
        if not world_block:
            casting_block = (
                f"\n\nSERIES CONTEXT:\n"
                f"  title: {series.get('title','')}\n"
                f"  genre: {series.get('genre','')}\n"
                f"  synopsis: {(series.get('synopsis') or '')[:600]}\n\n"
            )
        casting_block += _casting_aesthetics_block(series)
    system = (
        "You extract structured cast/crew data from a single short-drama episode script. "
        "Return STRICT JSON, no prose, no markdown.\n\n"
        "Schema:\n"
        '{\n'
        '  "characters": [{"name": "...", "gender": "male|female", "appearance": "1 sentence visual description"}],\n'
        '  "locations":  [{"name": "...", "description": "1 sentence about the place"}],\n'
        '  "items":      [{"name": "...", "description": "1 sentence visual description"}]\n'
        '}\n\n'
        "Rules:\n"
        "- characters: every named person who SPEAKS or ACTS. Skip extras and crowd ('официант', 'прохожий').\n"
        "- locations: every distinct setting where action happens. Use INT/EXT slug as the name when present.\n"
        "- items: ONLY plot-relevant objects (the locket revealed at climax, the USB stick with evidence,\n"
        "  the stolen handbag). NOT random props (coffee cups, generic furniture).\n"
        "- Names: prefer the canonical full-name as it first appears in the script.\n"
        "- If an entity matches an already-known name (case-insensitive), use the EXACT known spelling so dedup works.\n"
        "- Empty arrays are valid. No fields beyond schema.\n"
        "- If a WORLD CONVENTION block is present in the user message, OBEY it for the 'appearance' field of every character.\n"
        "- A CASTING & APPEARANCE AESTHETICS block is present in the user message — OBEY it: cast looks by narrative role; leads and any romance/seduction/intimacy role must read as attractive and age-appropriate."
    )
    raw = claude_ask(
        f"Episode script:\n\n{script_text[:18000]}{known_section}{world_block}{casting_block}",
        system=system, model='', max_tokens=2500,
    )
    try:
        return loads_lenient(raw)
    except Exception as e:
        print(f'[import-extract] LLM JSON parse failed: {e}; raw[:400]={raw[:400]!r}', flush=True)
        return {'characters': [], 'locations': [], 'items': []}


def _import_worker(sid, episode_records, create_chars=True, create_locs=True, create_items=True):
    """Background worker: walks every episode, runs one LLM extraction per ep,
    merges results into series.characters/locations/items + ep.characters_used /
    locations_used / items_used. Updates _IMPORT_STATUS as it goes so the UI
    can show progress.

    create_{chars,locs,items}: when False, the worker still RUNS the LLM
    extraction (so per-episode *_used lists get linked to existing roster
    entries by name), but it will NOT create NEW entities of that type. Used
    by the import-from-script flow when the user pre-uploaded their own
    characters/locations and only wants their explicit roster — extracted
    names that don't match existing get silently dropped from *_used.
    """
    st = _import_status(sid)
    st.update({
        'running': True, 'total': len(episode_records), 'done': 0,
        'errors': [], 'started_at': datetime.datetime.utcnow().isoformat(),
        'finished_at': None, 'current': None,
    })
    try:
        for ep_record in episode_records:
            num = ep_record['number']
            st['current'] = f'Эп. {num}'
            try:
                # Reload series each iteration so we get the freshest known set
                # (other ticks may have added entities).
                s = load_series(sid)
                if not s:
                    st['errors'].append({'episode': num, 'error': 'series vanished'})
                    continue
                ep = load_episode(sid, num)
                if not ep:
                    st['errors'].append({'episode': num, 'error': 'episode missing'})
                    continue
                known_chars = {c['name'] for c in s.get('characters', [])}
                known_locs  = {l['name'] for l in s.get('locations', [])}
                known_items = {it['name'] for it in s.get('items', [])}
                # Retry LLM extraction up to 3 times. Common failure modes:
                # - claude returned a markdown-fenced JSON we couldn't parse
                # - rate-limit retry inside claude_ask ran out (rare but happens)
                # - random empty-list result on transient overload
                # Each retry waits a few seconds. If all 3 fail, mark the
                # episode as cast_extracted=True anyway BUT with empty used
                # arrays, and append a clear error so the user knows to retry
                # extraction manually via the «🔁 Принять заново» path.
                extracted = None
                last_err = None
                for try_idx in range(3):
                    try:
                        extracted = _llm_extract_episode_entities(
                            ep.get('script', ''), known_chars, known_locs, known_items, series=s
                        )
                        # A valid response has at least one of the three lists
                        # populated (rare to have an episode with literally no
                        # entities). If all empty, it's almost certainly a
                        # parse error swallowed by the lenient loader.
                        nonempty = (
                            len(extracted.get('characters') or []) +
                            len(extracted.get('locations')  or []) +
                            len(extracted.get('items')      or [])
                        )
                        if nonempty > 0 or len((ep.get('script') or '').strip()) < 200:
                            break  # accept (short scripts may legit have no entities)
                        last_err = 'LLM returned empty entity lists for non-trivial script'
                    except Exception as e:
                        last_err = str(e)[:300]
                        print(f'[import-worker] ep {num} LLM try {try_idx+1}/3 failed: {last_err}', flush=True)
                    if try_idx < 2:
                        time.sleep(3 + try_idx * 2)   # 3s, 5s
                if extracted is None:
                    # All retries threw — leave script as-is, log, skip merge.
                    st['errors'].append({'episode': num, 'error': f'LLM extract failed after 3 tries: {last_err}'})
                    extracted = {'characters': [], 'locations': [], 'items': []}

                # Merge characters
                ep_char_ids = []
                for c in (extracted.get('characters') or [])[:30]:
                    name = (c.get('name') or '').strip()
                    if not name:
                        continue
                    existing = next((x for x in s['characters'] if x['name'].lower() == name.lower()), None)
                    if existing:
                        ep_char_ids.append(existing['id'])
                    elif create_chars:
                        new_c = {
                            'id': str(uuid.uuid4())[:8],
                            'name': name,
                            'description': '',
                            'appearance': (c.get('appearance') or '').strip(),
                            'gender': (c.get('gender') or 'female').lower(),
                            'voice_id': '',
                            'ref_images': [],
                            'outfits': [],
                            'base_outfit_label': 'base',
                        }
                        s['characters'].append(new_c)
                        ep_char_ids.append(new_c['id'])
                    # else: create_chars=False and no roster match → drop

                # Merge locations
                ep_loc_ids = []
                for l in (extracted.get('locations') or [])[:30]:
                    name = (l.get('name') or '').strip()
                    if not name:
                        continue
                    existing = next((x for x in s['locations'] if x['name'].lower() == name.lower()), None)
                    if existing:
                        ep_loc_ids.append(existing['id'])
                    elif create_locs:
                        new_l = {
                            'id': str(uuid.uuid4())[:8],
                            'name': name,
                            'description': (l.get('description') or '').strip(),
                            'ref_images': [],
                            'avai_url': '',
                        }
                        s['locations'].append(new_l)
                        ep_loc_ids.append(new_l['id'])
                    # else: create_locs=False and no roster match → drop

                # Merge items — fuzzy dedup so cross-language re-imports of the
                # same prop don't make duplicates ("Hidden Recorder" / "скрытый
                # диктофон" / "Recording Device" → all collapse to one entry).
                ep_item_ids = []
                for it in (extracted.get('items') or [])[:20]:
                    name = (it.get('name') or '').strip()
                    desc = (it.get('description') or '').strip()
                    if not name:
                        continue
                    existing = _fuzzy_find_item(s['items'], name, desc)
                    if existing:
                        ep_item_ids.append(existing['id'])
                    elif create_items:
                        new_it = {
                            'id': str(uuid.uuid4())[:8],
                            'name': name,
                            'description': (it.get('description') or '').strip(),
                            'ref_images': [],
                            'avai_url': '',
                            'image_constraints': '',
                        }
                        s['items'].append(new_it)
                        ep_item_ids.append(new_it['id'])

                ep['characters_used'] = ep_char_ids
                ep['locations_used']  = ep_loc_ids
                ep['items_used']      = ep_item_ids
                # Mark cast as user-confirmed so the scene-view auto-opens on
                # next page-load — user already "accepted" the script by
                # importing it. Without this flag the FE thinks they still
                # need to click «✅ Принять сценарий» on every episode.
                ep['cast_extracted'] = True
                save_series(sid, s)
                save_episode(sid, num, ep)

                # ── Canon update: per-episode extraction of timeline events,
                # canon facts (locked story-truths), character knowledge state,
                # and open story-threads. Without this the series canon stays
                # empty when user adds episodes via «📜 Добавить сценарий» or
                # «✨ Сгенерировать новые» — only manual /reaccept rebuilds it.
                # Best-effort: failures logged but don't block the worker.
                try:
                    st['current'] = f'Эп. {num} · обновляю канон…'
                    rollback_canon_for_episode(sid, num)   # idempotent re-imports
                    upd = extract_canon_updates(sid, num, ep.get('script', ''))
                    if upd and not upd.get('error'):
                        # Annotate stats so the frontend pipeline banner can
                        # surface canon-update progress if it wants to.
                        st.setdefault('canon', {'updated': 0, 'facts': 0, 'threads': 0, 'errors': 0})
                        st['canon']['updated']  = st['canon'].get('updated', 0) + 1
                        st['canon']['facts']   += int(upd.get('new_facts')   or 0)
                        st['canon']['threads'] += int(upd.get('new_threads') or 0)
                    elif upd and upd.get('error'):
                        st.setdefault('canon', {'updated': 0, 'facts': 0, 'threads': 0, 'errors': 0})
                        st['canon']['errors'] = st['canon'].get('errors', 0) + 1
                        print(f'[import-worker] canon ep{num} error: {upd.get("error")}', flush=True)
                except Exception as e:
                    print(f'[import-worker] canon update ep{num} crashed: {e}', flush=True)
            except Exception as e:
                import traceback
                print(f'[import-worker] ep {num} crashed: {e}', flush=True)
                traceback.print_exc()
                st['errors'].append({'episode': num, 'error': str(e)})
            finally:
                st['done'] += 1
    finally:
        st['running'] = False
        st['current'] = None
        st['finished_at'] = datetime.datetime.utcnow().isoformat()
    # Hand off to autogen sweep: now that every episode has its
    # characters_used / locations_used / items_used populated, fire the asset
    # sweep so portraits / outfit shots / location stills / item images all
    # start generating in the background. Frontend transitions its progress
    # banner to «🎨 Генерация ассетов» when it sees autogen-status running.
    try:
        s = load_series(sid)
        if s and s.get('auto_generate_assets'):
            print(f'[import-worker] {sid}: handoff → autogen sweep', flush=True)
            _spawn_with_keys(auto_generate_missing_assets, sid)
    except Exception as e:
        print(f'[import-worker] autogen handoff failed: {e}', flush=True)


@app.route('/api/series/<sid>/episodes/logic-check-multi', methods=['POST'])
def episodes_logic_check_multi(sid):
    """Cross-episode logic audit on a SELECTED set of already-saved episodes.
    Mirrors /import-from-script/logic-check but reads scripts from disk
    instead of taking a pasted script. Body: {episode_numbers: [int, ...]}.
    Returns the same {issues, episodes_analyzed} shape so the same UI can
    render results."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    body = request.json or {}
    nums = body.get('episode_numbers') or []
    if not isinstance(nums, list) or not nums:
        return jsonify({'error': 'episode_numbers required (non-empty list of ints)'}), 400
    nums = sorted({int(n) for n in nums if isinstance(n, (int, float)) or (isinstance(n, str) and n.strip().isdigit())})
    eps = []
    for n in nums:
        e = load_episode(sid, n)
        if e and (e.get('script') or '').strip():
            eps.append(e)
    if not eps:
        return jsonify({'error': 'у выбранных серий нет сценариев'}), 400
    blocks = []
    for e in eps:
        head = f"--- Episode {e.get('number')}: {(e.get('title') or '').strip()} ---"
        body_txt = (e.get('script') or '')[:18000]
        blocks.append(f"{head}\n{body_txt}")
    joined = '\n\n'.join(blocks)
    system = (
        "You are a strict logic auditor for a short-drama TV series. "
        "Read all episodes in order and find INCONSISTENCIES: "
        "(a) factual contradictions between episodes, "
        "(b) plot holes, "
        "(c) forgotten threads, "
        "(d) character continuity (knowledge/state/location jumps), "
        "(e) timeline errors. "
        "Output STRICT JSON, no prose, no markdown:\n"
        '{"issues": [{"severity":"critical|high|medium|low","type":"contradiction|plot_hole|forgotten_thread|continuity|timeline","episodes":[int],"summary":"...","evidence":"...","fix":"..."}]}\n'
        "No commentary outside JSON. Empty issues list is valid. Output in the language of the script."
    )
    try:
        raw = claude_ask(joined, system=system, max_tokens=4000)
        parsed = loads_lenient(raw)
        issues = parsed.get('issues') if isinstance(parsed, dict) else None
        if not isinstance(issues, list):
            return jsonify({'error': 'LLM returned malformed JSON', 'raw': raw[:400]}), 500
        return jsonify({
            'episodes_analyzed': len(eps),
            'episode_numbers':   [e.get('number') for e in eps],
            'issues':            issues,
        })
    except Exception as e:
        _log_event('WARN', 'logic_check_multi_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/episodes/logic-apply-multi', methods=['POST'])
def episodes_logic_apply_multi(sid):
    """Apply selected logic fixes to a SET of already-saved episodes. Body:
    {episode_numbers: [int...], issues: [{...}]}. Pipeline:
      1. Re-read each episode's script
      2. Build the same Episode-N-headered concat as /logic-check-multi
      3. Ask Claude to rewrite minimally addressing the listed issues
      4. Split the rewritten text back into per-episode scripts
      5. Write each updated script to disk (preserving everything else on ep)
      6. Return per-episode before/after lengths + which were modified
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    body = request.json or {}
    nums = body.get('episode_numbers') or []
    issues = body.get('issues') or []
    if not isinstance(nums, list) or not nums:
        return jsonify({'error': 'episode_numbers required'}), 400
    if not isinstance(issues, list) or not issues:
        return jsonify({'error': 'issues required (non-empty list)'}), 400
    nums = sorted({int(n) for n in nums if isinstance(n, (int, float)) or (isinstance(n, str) and n.strip().isdigit())})

    eps_by_num = {}
    blocks = []
    for n in nums:
        e = load_episode(sid, n)
        if not e or not (e.get('script') or '').strip():
            continue
        eps_by_num[n] = e
        head = f"--- Episode {e.get('number')}: {(e.get('title') or '').strip()} ---"
        blocks.append(f"{head}\n{(e.get('script') or '')[:18000]}")
    if not eps_by_num:
        return jsonify({'error': 'у выбранных серий нет сценариев'}), 400
    joined = '\n\n'.join(blocks)

    fix_lines = []
    for i, it in enumerate(issues, 1):
        if not isinstance(it, dict):
            continue
        eps_ref = it.get('episodes') or []
        fix_lines.append(
            f"{i}. [{(it.get('severity') or '?').upper()}] {it.get('type','?')} · Эп.{','.join(map(str, eps_ref))}\n"
            f"   PROBLEM:  {it.get('summary','')}\n"
            f"   EVIDENCE: {it.get('evidence','')}\n"
            f"   FIX:      {it.get('fix','')}"
        )
    fixes_block = '\n\n'.join(fix_lines) or '(no fixes provided)'
    system = (
        "You are a surgical script editor for a short-drama TV series. "
        "Apply the listed logic-fixes to the MULTI-EPISODE script with MINIMAL edits. "
        "Preserve EVERY '--- Episode N: Title ---' header line exactly. "
        "Preserve every other character and dialogue line verbatim. Only change what's "
        "strictly needed to address each listed issue. Keep the same language as the "
        "original script. Output STRICT JSON, no prose, no markdown:\n"
        '{"script": "full rewritten multi-episode text with \\n line breaks", '
        '"changes": [{"issue_index": int, "summary": "1 sentence what you changed"}]}\n'
        "issue_index is the 1-based number from the input list."
    )
    user_msg = (
        f"=== MULTI-EPISODE SCRIPT TO PATCH ===\n{joined[:90000]}\n\n"
        f"=== ISSUES TO FIX ===\n{fixes_block}\n\n"
        "Return the corrected full multi-episode text + per-issue change summary. JSON only."
    )
    try:
        raw = claude_ask(user_msg, system=system, max_tokens=20000)
        parsed = loads_lenient(raw)
        new_script = parsed.get('script') if isinstance(parsed, dict) else None
        changes = parsed.get('changes') if isinstance(parsed, dict) else []
        if not isinstance(new_script, str) or not new_script.strip():
            return jsonify({'error': 'LLM returned no script', 'raw': raw[:400]}), 500
    except Exception as e:
        _log_event('WARN', 'logic_apply_multi_llm_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500

    # Split rewritten multi-episode text by «--- Episode N: ... ---» headers.
    # Tolerant: matches lines starting with «---» that have «Episode <N>» token.
    pattern = re.compile(r'^\s*---\s*Episode\s+(\d+)[^\n-]*---\s*$', re.IGNORECASE | re.MULTILINE)
    matches = list(pattern.finditer(new_script))
    if not matches:
        # Fallback — maybe Claude dropped the «---» fences. Try plain «Episode N:» markers.
        pattern2 = re.compile(r'(?im)^[ \t]*episode[ \t]+(\d+)[ \t]*[:\-—]?[^\n]*$')
        matches = list(pattern2.finditer(new_script))
        if not matches:
            return jsonify({'error': 'Не удалось разбить переписанный сценарий по сериям — Claude сломал разметку. Попробуй ещё раз или применяй фиксы по одному.'}), 500

    updated = []
    skipped = []
    for i, m in enumerate(matches):
        try:
            num = int(m.group(1))
        except (ValueError, IndexError):
            continue
        if num not in eps_by_num:
            skipped.append({'number': num, 'reason': 'not in selected set'})
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(new_script)
        body_txt = new_script[start:end].strip()
        if not body_txt:
            skipped.append({'number': num, 'reason': 'empty body'})
            continue
        ep = eps_by_num[num]
        before_len = len(ep.get('script') or '')
        # Archive previous script as a history entry so the user can revert.
        try:
            history = ep.setdefault('script_history', [])
            history.append({
                'script': ep.get('script') or '',
                'saved_at': datetime.datetime.utcnow().isoformat(),
                'reason': 'logic-apply-multi',
            })
            ep['script_history'] = history[-15:]   # cap history
        except Exception:
            pass
        ep['script'] = body_txt
        end_pos = _extract_end_position(body_txt)
        if end_pos:
            ep['end_position'] = end_pos
        save_episode(sid, num, ep)
        updated.append({'number': num, 'before_len': before_len, 'after_len': len(body_txt)})

    return jsonify({
        'updated':       updated,
        'skipped':       skipped,
        'applied_count': len(issues),
        'changes':       changes if isinstance(changes, list) else [],
    })


@app.route('/api/series/import-from-script/logic-check', methods=['POST'])
def import_from_script_logic_check():
    """Cross-episode logic audit BEFORE creating/appending. Splits the pasted
    script the same way as /preview, then asks Claude to read every episode in
    order and surface inconsistencies — contradictions, plot holes, forgotten
    threads, character continuity issues. Returns structured list of issues
    with severity + episode references. The user fixes the script in the
    textarea and re-runs, or accepts as-is and clicks «Добавить серии»."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    eps = _split_script_into_episodes(script)
    if not eps:
        return jsonify({'error': 'не удалось разбить сценарий на серии'}), 400
    # Cap to first 18000 chars per episode to keep prompt sane on huge series.
    blocks = []
    for e in eps:
        head = f"--- Episode {e['number']}: {e.get('title') or ''} ---"
        body = (e.get('body') or '')[:18000]
        blocks.append(f"{head}\n{body}")
    joined = '\n\n'.join(blocks)
    system = (
        "You are a strict logic auditor for a short-drama TV series. "
        "Read all episodes in order and find INCONSISTENCIES: "
        "(a) factual contradictions between episodes (character was dead, then alive), "
        "(b) plot holes (an action has no setup or no consequence), "
        "(c) forgotten threads (a question/promise/item introduced and never resolved), "
        "(d) character continuity (knowledge/state/location jumps without explanation), "
        "(e) timeline errors (event order impossible). "
        "Output STRICT JSON, no prose, no markdown:\n"
        '{\n'
        '  "issues": [\n'
        '    {\n'
        '      "severity": "critical|high|medium|low",\n'
        '      "type":     "contradiction|plot_hole|forgotten_thread|continuity|timeline",\n'
        '      "episodes": [int, int],   // episode numbers involved\n'
        '      "summary":  "1 sentence — what is wrong",\n'
        '      "evidence": "short direct quote(s) showing it",\n'
        '      "fix":      "1 concrete suggestion how to fix"\n'
        '    }\n'
        '  ]\n'
        '}\n'
        'No commentary outside JSON. Empty issues list is valid. Output in the language of the script (Russian if Russian, English if English).'
    )
    try:
        raw = claude_ask(joined, system=system, max_tokens=4000)
        parsed = loads_lenient(raw)
        issues = parsed.get('issues') if isinstance(parsed, dict) else None
        if not isinstance(issues, list):
            return jsonify({'error': 'LLM returned malformed JSON', 'raw': raw[:400]}), 500
        return jsonify({
            'episodes_analyzed': len(eps),
            'issues': issues,
        })
    except Exception as e:
        _log_event('WARN', 'logic_check_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/import-from-script/apply-fixes', methods=['POST'])
def import_from_script_apply_fixes():
    """Apply selected logic-check fixes to the script. Body:
      {script: str, issues: [{summary, type, episodes, evidence, fix, ...}]}
    Claude rewrites the script with MINIMAL edits — only addressing the listed
    issues, preserving everything else verbatim. Returns {script: <new>,
    changes_summary: <1-line per issue what was changed>}.
    UI puts the rewritten text back into the textarea and lets the user
    re-run logic-check until clean."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    issues = data.get('issues') or []
    if not script:
        return jsonify({'error': 'script required'}), 400
    if not issues or not isinstance(issues, list):
        return jsonify({'error': 'issues required (non-empty list)'}), 400
    # Render the fix-list as compact instructions for Claude.
    fix_lines = []
    for i, it in enumerate(issues, 1):
        if not isinstance(it, dict):
            continue
        eps = it.get('episodes') or []
        fix_lines.append(
            f"{i}. [{it.get('severity','?').upper()}] {it.get('type','?')} · Эп.{','.join(map(str, eps))}\n"
            f"   PROBLEM:  {it.get('summary','')}\n"
            f"   EVIDENCE: {it.get('evidence','')}\n"
            f"   FIX:      {it.get('fix','')}"
        )
    fixes_block = '\n\n'.join(fix_lines) or '(no fixes provided)'
    system = (
        "You are a surgical script editor for a short-drama TV series. "
        "Apply the listed logic-fixes to the script with MINIMAL edits. "
        "Preserve episode boundaries (lines like «Episode 17: Title»), preserve every "
        "other character and dialogue line verbatim. Only change what's strictly "
        "needed to address each listed issue (rewrite, add 1-2 lines for setup, "
        "remove a contradictory line — whichever is most surgical). "
        "Keep the same language as the original script (Russian if Russian, English if English). "
        "Output STRICT JSON, no prose, no markdown:\n"
        '{\n'
        '  "script":  "the full rewritten script as one string with \\n line breaks",\n'
        '  "changes": [{"issue_index": int, "summary": "1 sentence what you changed"}]\n'
        '}\n'
        "issue_index is the 1-based number from the input list."
    )
    user_msg = (
        f"=== SCRIPT TO PATCH ===\n{script[:60000]}\n\n"
        f"=== ISSUES TO FIX ===\n{fixes_block}\n\n"
        "Return the corrected full script + a short list of what you changed. JSON only."
    )
    try:
        raw = claude_ask(user_msg, system=system, max_tokens=16000)
        parsed = loads_lenient(raw)
        new_script = parsed.get('script') if isinstance(parsed, dict) else None
        changes = parsed.get('changes') if isinstance(parsed, dict) else []
        if not isinstance(new_script, str) or not new_script.strip():
            return jsonify({'error': 'LLM returned no script', 'raw': raw[:400]}), 500
        return jsonify({
            'script':  new_script,
            'changes': changes if isinstance(changes, list) else [],
            'applied_count': len(issues),
        })
    except Exception as e:
        _log_event('WARN', 'logic_fix_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/import-from-script/preview', methods=['POST'])
def import_from_script_preview():
    """Returns the proposed episode breakdown for a pasted script WITHOUT
    creating anything. UI shows it as a confirmable preview. Cheap (regex-
    only, no LLM).

    Also runs `_detect_dialogue_language()` — if more than 15% of dialogue
    lines are non-English, returns a `dialogue_lang_warning` payload so
    the UI can offer to adapt the script to English before commit."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    eps = _split_script_into_episodes(script)
    lang_info = _detect_dialogue_language(script)
    payload = {
        'episodes': [
            {'number': e['number'], 'title': e['title'], 'preview': e['body'][:240], 'length': len(e['body'])}
            for e in eps
        ],
        'total_chars': len(script),
    }
    # 15% threshold — below that the few stray non-EN words are probably
    # quoted phrases or names, not the dominant dialogue language.
    if lang_info['ratio'] > 0.15 and lang_info['non_english_lines'] > 0:
        payload['dialogue_lang_warning'] = lang_info
    return jsonify(payload)


_TRANSLATE_DIALOGUES_SYSTEM = """You are a screenplay localization editor. \
Your only job: rewrite the dialogue lines in the user's script so the spoken \
text is natural conversational ENGLISH, while preserving everything else \
EXACTLY as it appears in the input.

WHAT TO TRANSLATE:
- Spoken dialogue body — the text AFTER `CHARACTER:` (or `CHARACTER (action):`).
  Make it natural spoken English. Preserve emotional tone and meaning. Keep
  the same approximate length (±20% words). Use contractions ("I'm", "don't")
  for realistic speech.

WHAT TO LEAVE UNTOUCHED, BYTE-FOR-BYTE:
- Episode/scene headers (e.g. `**СЕРИЯ 1 — "TITLE"**`, `=== ЭПИЗОД 5 ===`,
  `## EPISODE 1`, scene slug lines like `ИНТА. РЕСТОРАН — НОЧЬ`).
- Character name cues — leave the speaker label in original casing
  (e.g. `VICTORIA:`, `МАРКУС:` — DON'T transliterate Cyrillic names).
- Action lines / stage directions wrapped in `[...]`, `(...)`, or `*(...)*`.
  These may stay in their original language (per project convention).
- Blank lines, separators (`---`, `===`), markdown formatting (`**`, `*`).
- English dialogue lines that are ALREADY in English — output them unchanged.

OUTPUT FORMAT:
- Return ONLY the rewritten script. No preamble, no explanation, no code fence.
- Preserve line order exactly. Preserve line breaks. Same number of lines as input.

EXAMPLE:
Input:
  **СЕРИЯ 1 — "ПОТОЛОК"**
  *(awkward silence in the store)*
  VICTORIA *(nervous laugh)*: Подожди... нет, это шутка. Ты серьёзно?
  MARCUS: Это было до того, как я узнал.

Output:
  **СЕРИЯ 1 — "ПОТОЛОК"**
  *(awkward silence in the store)*
  VICTORIA *(nervous laugh)*: Wait... no, this has to be a joke. Are you serious?
  MARCUS: That was before I knew.
"""

@app.route('/api/series/import-from-script/translate-dialogues', methods=['POST'])
def import_from_script_translate_dialogues():
    """Single Claude pass that rewrites only the spoken-text portion of each
    dialogue line to natural English, leaving headers / action lines / scene
    slugs / character cues untouched. UI calls this when the user clicks
    «Адаптировать на английский» in the preview modal.

    Body: {script: str}
    Returns: {translated_script: str, lines_changed_estimate: int,
              before_ratio: float, after_ratio: float}
    """
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    if len(script) > 200000:
        # Sanity guard — gigantic paste would blow up Claude's context. We
        # could split-and-stitch but that's an iteration-2 feature; for now
        # tell the user to split manually.
        return jsonify({'error': 'script too long (>200k chars). Split into smaller batches and translate each.'}), 400

    before = _detect_dialogue_language(script)
    try:
        translated = claude_ask(
            script,
            system=_TRANSLATE_DIALOGUES_SYSTEM,
            model='sonnet',  # need translation quality, not haiku speed
            max_tokens=24000,
            timeout=600,
        ).strip()
    except Exception as e:
        _log_event('WARN', 'translate_dialogues_failed', err=str(e)[:300],
                   script_chars=len(script))
        return jsonify({'error': f'Не удалось адаптировать сценарий: {e}'}), 500

    # Strip accidental code-fence wrappers if the model still added them.
    if translated.startswith('```'):
        translated = re.sub(r'^```[a-zA-Z]*\n?', '', translated)
        translated = re.sub(r'\n?```\s*$', '', translated)
        translated = translated.strip()

    after = _detect_dialogue_language(translated)
    _log_event('INFO', 'translate_dialogues_ok',
               before_ratio=before['ratio'], after_ratio=after['ratio'],
               before_lang=before.get('detected_lang'),
               script_chars=len(script), translated_chars=len(translated))

    return jsonify({
        'translated_script': translated,
        'before_ratio': before['ratio'],
        'after_ratio':  after['ratio'],
        'before_lang':  before.get('detected_lang', 'en'),
        'lines_total':  before['dialogue_lines'],
        'lines_changed_estimate': before['non_english_lines'] - after['non_english_lines'],
    })


_ADAPT_TO_STANDARD_SYSTEM = (
    "You are a script formatter for short-form drama. Your task has THREE parts:\n\n"

    "PART 1 — POSITION BLOCKS\n"
    "Add POSITION BLOCKS to every scene:\n"
    "[BLOCKING] — insert immediately after EVERY scene heading (ИНТА./ЭКСТ. line):\n"
    "  [BLOCKING]\n"
    "  LOCATION: <English location name>\n"
    "  CHARACTER_NAME: <position in Russian> :: OUTFIT: <Outfit Name>\n"
    "  [/BLOCKING]\n\n"
    "[BLOCKING_END] — insert at the very end of each episode (absolute last thing):\n"
    "  [BLOCKING_END]\n"
    "  LOCATION: <English location name>\n"
    "  CHARACTER_NAME: <final position at cut — in Russian>\n"
    "  [/BLOCKING_END]\n\n"
    "Rules for position blocks:\n"
    "- [BLOCKING] lists ONLY characters PRESENT at scene START\n"
    "- OUTFIT FIELD = a short Title Case NAME of the outfit asset (NOT a clothing description). Examples: `Business Suit`, `Casual`, `Pajamas`, `Red Dress`, `School Uniform`, `Hospital Gown`, `Swimsuit`. The system uses this label to reuse the same outfit asset across scenes.\n"
    "- When the outfit NAME is new (not seen for this character before) add a description after a pipe: `OUTFIT: Pajamas | OUTFIT_DESC: light blue cotton pajamas, bare feet`. For names that were already introduced in a previous scene of this or earlier episode, OMIT `| OUTFIT_DESC:` — the system already has the description.\n"
    "- DEDUP: don't invent 10 names for nearly-identical looks. If the character is in their default clothes use `Base` or the existing label they already have. New label = real wardrobe change.\n"
    "- [BLOCKING_END] lists only characters present at end of episode (no OUTFIT needed — it's still the same outfit as in [BLOCKING])\n"
    "- If consecutive episodes continue the same scene, [BLOCKING] of episode N+1 MUST match [BLOCKING_END] of episode N\n\n"

    "PART 2 — DIALOGUE TRANSLATION\n"
    "Translate any dialogue lines NOT in English to English. "
    "Pattern: CHARACTER_NAME_ALLCAPS: \"dialogue\" or CHARACTER_NAME_ALLCAPS: (parenthetical) dialogue. "
    "Do NOT translate action lines (in [brackets]) or scene headings or EPISODE NOTES sections. "
    "Preserve character names exactly as written (ALL CAPS). "
    "Already-English dialogue — leave unchanged.\n\n"

    "PART 3 — SEEDANCE MODERATION SCAN\n"
    "After adapting the script, scan ALL dialogue lines for content that may trigger Seedance AI video generation moderation filters. "
    "Seedance flags: explicit violence (killing, blood, gore, graphic weapon use), sexual content, suicide/self-harm references, "
    "explicit drug use, death threats. "
    "For each flagged line provide 2-3 NATURAL alternative rewrites that sound organic in context — "
    "based on the surrounding scene context and character relationships. "
    "CRITICAL: rewrites must feel like real human speech in the moment. "
    "Bad example: 'Put down the gun' → 'Remove the tactical equipment' (robotic, unnatural). "
    "Good example: 'Put down the gun' → 'Put that down!' or 'Drop it, now!' (natural, urgent, fits the scene). "
    "Only flag lines that are genuinely likely to cause moderation failure — do NOT flag mild drama, "
    "emotional conflict, or normal thriller tension.\n\n"

    "Output format: return a JSON object with THREE keys:\n"
    "  script: the full adapted script as a string\n"
    "  changes: array of short strings describing what was done, e.g. "
    "[\"Added BLOCKING to episode 1\", \"Added BLOCKING_END to episode 3\", \"Translated 5 dialogue lines\"]\n"
    "  moderation_warnings: array of objects, each: "
    "{\"original\": \"JOHN: \\\"I'll kill you\\\"\", \"reason\": \"Explicit death threat\", "
    "\"suggestions\": [\"JOHN: \\\"You'll regret this!\\\"\", \"JOHN: \\\"I swear you'll pay for this!\\\"\"]}\n"
    "If no moderation issues found, moderation_warnings must be an empty array [].\n\n"
    "Output ONLY valid JSON. No markdown fences."
)


@app.route('/api/adapt-script-to-standard', methods=['POST'])
def adapt_script_to_standard():
    """Takes a raw multi-episode script and adapts it to tool standard:
    1. Adds [BLOCKING] after each scene heading and [BLOCKING_END] at end of each episode
    2. Translates non-English dialogue to English (preserves already-English dialogue)
    3. Does NOT change plot, character names, or action lines
    Body: {script: str}
    Returns: {script: str, changes: [str]}
    """
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    if len(script) > 300000:
        return jsonify({'error': 'script too long (>300k chars). Split into smaller batches.'}), 400

    prompt = (
        "Adapt the following multi-episode script to the position-blocks standard. "
        "Add [BLOCKING] blocks after every scene heading and [BLOCKING_END] at the end of every episode. "
        "Translate any non-English dialogue lines to English. "
        "Return JSON with 'script' and 'changes' keys.\n\n"
        "SCRIPT:\n" + script
    )

    try:
        raw = claude_ask(
            prompt,
            system=_ADAPT_TO_STANDARD_SYSTEM,
            model='claude-sonnet-4-5',
            max_tokens=32000,
            timeout=180,
        ).strip()
    except Exception as e:
        _log_event('WARN', 'adapt_script_to_standard_failed', err=str(e)[:300])
        return jsonify({'error': f'Не удалось адаптировать сценарий: {e}'}), 500

    # Strip accidental code-fence wrappers
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)
        raw = raw.strip()

    try:
        result = json.loads(strip_json(raw))
    except Exception:
        # If JSON parse fails, return the raw text as script with no change list
        _log_event('WARN', 'adapt_script_to_standard_json_parse_fail', raw_chars=len(raw))
        return jsonify({'script': raw, 'changes': ['(не удалось распарсить список изменений)']})

    adapted_script = result.get('script', raw)
    # Same deterministic backstop as /api/check-moderation — scan the ADAPTED
    # script so figurative/third-person triggers ("that's suicide", "slaughter")
    # the LLM rationalized away still surface.
    merged_warnings = _merge_moderation_warnings(
        result.get('moderation_warnings', []), adapted_script
    )
    return jsonify({
        'script':              adapted_script,
        'changes':             result.get('changes', []),
        'moderation_warnings': merged_warnings,
    })


from sw.textrules_moderation import (
    _MOD_TRIGGER_GROUPS,
    _NON_SPEAKER_LABELS,
    _lexical_moderation_scan,
    _SOFTEN_MAP,
    _soften_line,
    _fallback_suggestions,
    _REWRITE_SYSTEM,
    _author_rewrites,
    _modkey,
    _merge_moderation_warnings,
    _PHRASE_CHECK_SYSTEM,
)
@app.route('/api/check-moderation', methods=['POST'])
def check_moderation():
    """Fast phrase scan: checks script dialogue for Seedance moderation risk.
    No position blocks, no translation — only moderation_warnings.
    Body: {script: str}
    Returns: {moderation_warnings: [{original, reason, suggestions}]}
    """
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'moderation_warnings': []})
    if len(script) > 200000:
        return jsonify({'error': 'script too long (>200k chars)'}), 400

    # LLM advisor (best-effort — adds nuance + authors rewrites). Its failure
    # must NOT swallow the deterministic lexical scan, which is the real recall
    # guarantee. So we never 500 here: worst case the lexical scan stands alone.
    llm_warnings = []
    try:
        raw = claude_ask(
            f"Scan this script for Seedance moderation risks:\n\n{script}",
            system=_PHRASE_CHECK_SYSTEM,
            model='claude-haiku-4-5',   # fast + cheap — just a scan
            max_tokens=4096,
            timeout=60,
        ).strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
            raw = re.sub(r'\n?```\s*$', '', raw).strip()
        llm_warnings = (json.loads(strip_json(raw)) or {}).get('moderation_warnings', []) or []
    except Exception as e:
        _log_event('WARN', 'check_moderation_llm_failed', err=str(e)[:200])

    merged = _merge_moderation_warnings(llm_warnings, script)
    return jsonify({'moderation_warnings': merged})


@app.route('/api/series/import-from-script', methods=['POST'])
def import_from_script():
    """Two-phase commit: create series + episodes (synchronous, fast),
    then kick off background extraction. Returns immediately so UI can
    redirect to the new series page and start polling /import-status.

    Accepts EITHER:
      • JSON body { title, script, extract_entities?, synopsis? } — classic path.
      • multipart/form-data with the same fields + optional file uploads:
          character_files[]  — image files; name = filename stem (uppercased)
          location_files[]   — same, for locations
          extract_characters / extract_locations / extract_items — per-type bool
            flags (each defaults to extract_entities). When false, the worker
            still links existing roster entries by name but won't CREATE new
            entities of that type — so the user's pre-uploaded set is final.
    """
    is_multipart = request.content_type and request.content_type.startswith('multipart/')
    if is_multipart:
        form = request.form
        title = (form.get('title') or '').strip()
        script = (form.get('script') or '').strip()
        synopsis = form.get('synopsis') or ''
        def _flag(name, default):
            v = form.get(name)
            if v is None or v == '': return default
            return v not in ('0', 'false', 'False', 'off', 'no')
        do_extract        = _flag('extract_entities', True)
        do_extract_chars  = _flag('extract_characters', do_extract)
        do_extract_locs   = _flag('extract_locations',  do_extract)
        do_extract_items  = _flag('extract_items',      do_extract)
        dialogue_lang_hint = (form.get('dialogue_language_hint') or '').strip().lower()
        style_type = (form.get('style_type') or 'cinematic').strip().lower() or 'cinematic'
        style_custom_desc = (form.get('style_custom_description') or '').strip()
        writer_model_in = (form.get('writer_model') or '').strip().lower()
        char_files = request.files.getlist('character_files') or request.files.getlist('character_files[]')
        loc_files  = request.files.getlist('location_files')  or request.files.getlist('location_files[]')
    else:
        data = request.json or {}
        title = (data.get('title') or '').strip()
        script = (data.get('script') or '').strip()
        synopsis = data.get('synopsis', '')
        do_extract = bool(data.get('extract_entities', True))
        do_extract_chars = bool(data.get('extract_characters', do_extract))
        do_extract_locs  = bool(data.get('extract_locations',  do_extract))
        do_extract_items = bool(data.get('extract_items',      do_extract))
        dialogue_lang_hint = (data.get('dialogue_language_hint') or '').strip().lower()
        style_type = (data.get('style_type') or 'cinematic').strip().lower() or 'cinematic'
        style_custom_desc = (data.get('style_custom_description') or '').strip()
        writer_model_in = (data.get('writer_model') or '').strip().lower()
        char_files, loc_files = [], []
    writer_model_value = writer_model_in if writer_model_in in WRITER_MODEL_WHITELIST else WRITER_MODEL_DEFAULT

    if not title:
        return jsonify({'error': 'title required'}), 400
    if not script:
        return jsonify({'error': 'script required'}), 400

    eps = _split_script_into_episodes(script)
    if not eps:
        return jsonify({'error': 'script split produced no episodes'}), 400

    # Build the series shell — same defaults as create_series().
    slug = slugify(title)
    sid = slug if slug and not (user_root() / slug).exists() else f"{slug}-{str(uuid.uuid4())[:6]}"
    # Resolve user-chosen style: preset → canonical desc from _VISUAL_STYLE_PRESETS,
    # or 'custom' → use user-supplied description. visual_style is what the
    # background extraction worker reads when generating char/loc portraits,
    # so it MUST be populated correctly BEFORE the worker fires below.
    visual_style_value = ''
    if style_type == 'custom' and style_custom_desc:
        visual_style_value = style_custom_desc
    elif style_type in _VISUAL_STYLE_PRESETS:
        visual_style_value = _VISUAL_STYLE_PRESETS[style_type].get('desc', '') or ''
    series_data = {
        'id': sid, 'title': title,
        # Deterministic episode titles `<Series_Title>_E<N>` for all new series.
        'episode_title_format': 'series_indexed',
        'genre': '', 'tone': '', 'target_audience': '', 'world_description': '',
        'synopsis': synopsis,
        'auto_generate_assets': True, 'batch_mode': False, 'batch_size': 1,
        'stage': 4, 'arc': None, 'milestone_synopses': {},
        'checkpoints': [], 'finale': None,
        'devices_index': {},
        'cadence_policy': {'default_min_gap': 4, 'hard_limit': 3},
        'created_at': datetime.datetime.utcnow().isoformat(),
        'video_provider': 'seedance',
        'characters': [], 'locations': [], 'items': [],
        'style': {
            'type': style_type if style_type in _VISUAL_STYLE_PRESETS or style_type == 'custom' else 'cinematic',
            'custom_description': style_custom_desc if style_type == 'custom' else '',
            'ref_images': [],
        },
        'visual_style': visual_style_value,
        'writer_model': writer_model_value,
        'settings': {
            'voice': 'Enceladus', 'tts_provider': 'elevenlabs',
            'image_provider': 'banana', 'aspect_ratio': '9:16',
            'language': 'English', 'duration': 'auto-frames',
            'enable_music': True, 'music_volume': 0.30,
            'enable_animation': True, 'animation_speed': 'fast',
            'animation_resolution': '480p', 'animation_model': 'seedance-2-ref',
            'enable_grid': False, 'cinema': False, 'trim': True,
            'no_fades': True, 'multi_voice': False, 'enable_subtitles': False,
            'image_size': '1K',
        },
    }
    # User imported a non-English script and explicitly chose «оставить как
    # есть» in the preview warning — remember it on the series so the UI can
    # show a persistent badge `🌐 Диалоги: русский` and the user isn't
    # surprised later. Empty / 'en' = no badge.
    if dialogue_lang_hint and dialogue_lang_hint not in ('en', 'english'):
        series_data['dialogue_language_hint'] = dialogue_lang_hint
    save_series(sid, series_data)
    scaffold_series_folders(sid, title)

    # ── Persist pre-uploaded characters / locations BEFORE the worker runs.
    # Filename stem becomes the entity name (so the worker's name-based dedup
    # picks them up when the script mentions them). Image saved as the canonical
    # asset → user sees their character/location with an image right away,
    # before any AI generation.
    def _stem_to_name(filename):
        # «mia_chen.jpg» → «Mia Chen»; «ОСОБНЯК БЕЛЛАКУРТОВ.png» → «Особняк
        # Беллакуртов» (Title-cased — looks better in roster than ALL-CAPS).
        # Strip extension, replace separators with spaces, collapse, title-case.
        stem = re.sub(r'\.[^.]+$', '', filename or '').strip()
        stem = re.sub(r'[._\-]+', ' ', stem).strip()
        stem = re.sub(r'\s+', ' ', stem)
        if not stem: return ''
        # If user typed ALL CAPS, preserve as Title Case for legibility.
        if stem.isupper(): stem = stem.title()
        return stem

    uploaded_chars_count = 0
    for f in (char_files or []):
        if not f or not getattr(f, 'filename', ''): continue
        if not allowed_file(f.filename):
            _log_event('WARN', 'import_char_skip_badtype', name=f.filename); continue
        name = _stem_to_name(f.filename)
        if not name: continue
        # Skip name duplicates within this batch.
        if any(c['name'].lower() == name.lower() for c in series_data['characters']):
            continue
        cid = str(uuid.uuid4())[:8]
        char_dir = assets_dir(sid) / 'characters' / cid
        char_dir.mkdir(parents=True, exist_ok=True)
        safe = secure_filename(f.filename) or f'{cid}.jpg'
        dst = char_dir / safe
        try: f.save(dst)
        except Exception as e:
            _log_event('WARN', 'import_char_save_failed', name=name, err=str(e)[:160]); continue
        rel_path = str(dst.relative_to(series_path(sid)))
        series_data['characters'].append({
            'id': cid, 'name': name,
            'description': '', 'appearance': '',
            'gender': 'female', 'voice_id': '',
            'ref_images': [rel_path],
            'outfits': [], 'base_outfit_label': 'base',
        })
        uploaded_chars_count += 1

    uploaded_locs_count = 0
    for f in (loc_files or []):
        if not f or not getattr(f, 'filename', ''): continue
        if not allowed_file(f.filename):
            _log_event('WARN', 'import_loc_skip_badtype', name=f.filename); continue
        name = _stem_to_name(f.filename)
        if not name: continue
        if any(l['name'].lower() == name.lower() for l in series_data['locations']):
            continue
        lid = str(uuid.uuid4())[:8]
        loc_dir = assets_dir(sid) / 'locations' / lid
        loc_dir.mkdir(parents=True, exist_ok=True)
        safe = secure_filename(f.filename) or f'{lid}.jpg'
        dst = loc_dir / safe
        try: f.save(dst)
        except Exception as e:
            _log_event('WARN', 'import_loc_save_failed', name=name, err=str(e)[:160]); continue
        rel_path = str(dst.relative_to(series_path(sid)))
        series_data['locations'].append({
            'id': lid, 'name': name,
            'description': '',
            'ref_images': [rel_path], 'avai_url': '',
        })
        uploaded_locs_count += 1

    # Re-save now that pre-uploaded entities are baked in. The worker will pick
    # this up via load_series() inside its per-episode loop.
    if uploaded_chars_count or uploaded_locs_count:
        save_series(sid, series_data)

    # Create each episode with the script body pre-filled.
    ep_records = []
    for e in eps:
        ep_dict = {
            'number': e['number'],
            'title':  e['title'],
            'synopsis': '',
            'script':   e['body'],
            'characters_used': [],
            'locations_used':  [],
            'items_used':      [],
            'notes': '', 'reteller_prompt': '',
            'status': 'draft', 'ready': False,
            'created_at': datetime.datetime.utcnow().isoformat(),
        }
        save_episode(sid, e['number'], ep_dict)
        ep_records.append({'number': e['number']})
        # Sync [BLOCKING] outfits right after save — bulk-import is the most
        # common path where Margaret-style outfits get missed (a long import
        # of N episodes with many one-off labels could otherwise silently lose
        # them all until the user runs autogen). Idempotent + cheap.
        try:
            _sync_script_outfits(sid, e['body'])
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_import_failed',
                       sid=sid, ep=e['number'], err=str(_oe)[:200])

    # Kick off the extraction worker in the background. _spawn_with_keys
    # carries the user's auth context across the thread boundary. We always
    # run the worker if ANY per-type extraction is enabled (so episode
    # *_used arrays get populated by name-matching against pre-uploaded
    # roster) — even when no new-entity creation is allowed of that type.
    worker_should_run = do_extract_chars or do_extract_locs or do_extract_items
    if worker_should_run:
        _spawn_with_keys(
            _import_worker, sid, ep_records,
            create_chars=do_extract_chars,
            create_locs=do_extract_locs,
            create_items=do_extract_items,
        )

    # Auto-Vision appearance backfill for pre-uploaded characters. Each char
    # uploaded by the user lands with empty `appearance` — BINDING line for
    # Seedance is just the name, no text anchor for outfit / build / hair.
    # Run a background job per uploaded char that: (1) uploads the local
    # ref image to AVAI public storage to get a URL, (2) asks Claude Haiku
    # Vision for a Russian appearance description, (3) writes it to the
    # char's `appearance` field. Latency ~5-8s per char in parallel.
    if uploaded_chars_count:
        uploaded_char_ids = [c['id'] for c in series_data['characters'][-uploaded_chars_count:]]
        _spawn_with_keys(_backfill_uploaded_char_appearances, sid, uploaded_char_ids)

    return jsonify({
        'sid': sid,
        'episodes_created': len(eps),
        'extraction_started': worker_should_run,
        'characters_uploaded': uploaded_chars_count,
        'locations_uploaded': uploaded_locs_count,
        'extract_characters': do_extract_chars,
        'extract_locations':  do_extract_locs,
        'extract_items':      do_extract_items,
    }), 201


@app.route('/api/series/<sid>/import-status')
def import_status(sid):
    return jsonify(_import_status(sid))


@app.route('/api/series/<sid>/generate-script-batch', methods=['POST'])
def generate_script_batch(sid):
    """Generate N new episodes for an existing series. Returns the generated
    text as ONE multi-episode script (with «Episode N:» headers) ready to be
    pasted into the append-flow textarea. After generation user can preview-
    split, logic-check, fix, and commit via /append-from-script.

    Body: {count: int, direction?: str}
      - count: how many episodes to write (1-20)
      - direction: optional plot-direction hint
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    body = request.json or {}
    count = max(1, min(20, int(body.get('count') or 5)))
    direction = (body.get('direction') or '').strip()
    # Optional advanced parameters — empty/None = Claude decides.
    duration_sec_raw = body.get('duration_sec')
    lines_count_raw  = body.get('lines_count')
    try:
        duration_sec = int(duration_sec_raw) if duration_sec_raw not in (None, '', 0) else None
        if duration_sec is not None: duration_sec = max(30, min(240, duration_sec))
    except (TypeError, ValueError):
        duration_sec = None
    try:
        lines_count = int(lines_count_raw) if lines_count_raw not in (None, '', 0) else None
        if lines_count is not None: lines_count = max(3, min(40, lines_count))
    except (TypeError, ValueError):
        lines_count = None
    style_preset = (body.get('style') or '').strip()
    no_interruptions = bool(body.get('no_interruptions', True))  # default: interruptions forbidden
    max_chars_raw = body.get('max_main_chars_per_scene') or s.get('max_main_chars_per_scene')
    try:
        max_main_chars = int(max_chars_raw) if max_chars_raw not in (None, '', 0) else None
        if max_main_chars is not None: max_main_chars = max(1, min(6, max_main_chars))
    except (TypeError, ValueError):
        max_main_chars = None
    # Persist to series so individual episode generation picks it up too
    if max_main_chars and s.get('max_main_chars_per_scene') != max_main_chars:
        s['max_main_chars_per_scene'] = max_main_chars
        save_series(sid, s)
    # Style presets translated to Claude-friendly directives.
    _STYLE_PRESETS = {
        'short_punchy': 'Реплики КОРОТКИЕ и рваные (1-7 слов). TikTok-ритм: быстрые удары, шок-фразы, paus'
                        'е через действие. Никаких длинных монологов. Цель — макс эмоциональная плотность.',
        'balanced':     'Реплики СБАЛАНСИРОВАННЫЕ (5-15 слов). Средний темп. Можно изредка длинные эмоциональные '
                        'удары, основное — короткие.',
        'long_meaty':   'Реплики ДЛИННЫЕ и насыщенные (10-25 слов). Эмоциональные монологи, развёрнутые откровения, '
                        'весомые угрозы. Подходит для драматических кульминаций.',
    }
    style_clause = _STYLE_PRESETS.get(style_preset, '')

    # Pull existing episodes for context. Cap content to keep prompt sane:
    # last 8 episodes verbatim, earlier ones as synopsis-only.
    # `from_scratch` mode kicks in when the series has no episodes yet — we
    # write from Эп.1 using ONLY the series bible (title, genre, tone,
    # audience, world, roster) + user-provided direction.
    existing = sorted([e for e in list_episodes(sid) if e.get('script')], key=lambda e: e.get('number', 0))
    from_scratch = not existing
    if from_scratch:
        last_num = 0
    else:
        last_num = existing[-1].get('number', 0)
    first_new_num = last_num + 1
    last_new_num = last_num + count

    # Context window (skipped in from-scratch mode — no prior episodes)
    verbatim_window = existing[-8:] if not from_scratch else []
    earlier = existing[:-8] if not from_scratch else []
    earlier_block = ''
    if earlier:
        earlier_lines = []
        for e in earlier:
            syn = (e.get('synopsis') or '').strip()[:200]
            if not syn:
                syn = (e.get('script') or '')[:200].replace('\n', ' ').strip()
            earlier_lines.append(f"Эп.{e.get('number')}: {syn}")
        earlier_block = "СИНОПСИСЫ РАННИХ СЕРИЙ (краткий контекст):\n" + '\n'.join(earlier_lines) + '\n\n'

    verbatim_block = '\n\n'.join(
        f"=== Эп.{e.get('number')}: {(e.get('title') or '').strip()} ===\n{(e.get('script') or '')[:6000]}"
        for e in verbatim_window
    ) if verbatim_window else ''

    # Roster of known entities so the generated script reuses them by name.
    chars_list = ', '.join(c.get('name', '') for c in (s.get('characters') or []) if c.get('name'))[:1000]
    locs_list  = ', '.join(l.get('name', '') for l in (s.get('locations') or []) if l.get('name'))[:1000]
    items_list = ', '.join(it.get('name', '') for it in (s.get('items') or []) if it.get('name'))[:1000]

    if from_scratch:
        # From-scratch needs a real direction OR a decent synopsis — without
        # either we're writing fanfic with no idea what the series is about.
        if not direction and not (s.get('synopsis') or '').strip():
            return jsonify({
                'error': 'У сериала нет ни синопсиса в Bible, ни направления от тебя — '
                         'не из чего писать первую серию. Заполни синопсис в Bible '
                         'или укажи направление сюжета в поле «Куда сюжет идёт дальше».'
            }), 400
        direction_block = (
            f"\nЭТО СТАРТ СЕРИАЛА — пишешь С НУЛЯ, Эп.1–{count}.\n"
            f"СИНОПСИС / IDEA СЕРИАЛА:\n{(s.get('synopsis') or '').strip() or '(не задан в Bible)'}\n"
        )
        if direction:
            direction_block += f"\nНАПРАВЛЕНИЕ ОТ ПОЛЬЗОВАТЕЛЯ:\n{direction}\n"
        direction_block += (
            "\nТРЕБОВАНИЯ К ПИЛОТУ (Эп.1):\n"
            "- Открой сериал hook'ом за первые 5 секунд (визуальный шок, провокационная фраза, "
            "  острый конфликт). НЕ начинай с экспозиции.\n"
            "- Представь главных героев через действие, не через рассказ о них.\n"
            "- Заложи центральный конфликт + 1-2 побочные сюжетные линии для будущих серий.\n"
            "- Финал пилота — мощный cliffhanger, после которого хочется смотреть Эп.2.\n"
        )
        if count > 1:
            direction_block += (
                "\nТРЕБОВАНИЯ К ДУГЕ:\n"
                f"- За {count} серий построй полный мини-арк: пилот → нарастающие осложнения → "
                "точка невозврата → кульминация → финал последней серии (либо завершение арки, "
                "либо большой cliffhanger для продолжения).\n"
                "- Каждая серия развивает не менее одной сюжетной линии. Не дублируй конфликты.\n"
            )
    elif direction:
        direction_block = f"\nЖЕЛАЕМОЕ НАПРАВЛЕНИЕ СЮЖЕТА ОТ ПОЛЬЗОВАТЕЛЯ:\n{direction}\n"
    else:
        direction_block = (
            "\nПОЛЬЗОВАТЕЛЬ НЕ УКАЗАЛ НАПРАВЛЕНИЕ — придумай развитие сам, опираясь на открытые сюжетные линии "
            "из последних серий, нерешённые загадки, и эмоциональные арки персонажей. Не повторяй уже произошедшее.\n"
        )

    mode_label = 'старт сериала с нуля' if from_scratch else 'продолжение существующего сериала'
    # Compute effective length / lines targets. Speech delivery ≈ 3.8 wps
    # (matches the SPEECH_WPS calibration in the segmenter). Default episode
    # = ~60s ≈ 12-15 lines (user-tunable). If user specifies one but not the
    # other, we derive a sensible default for the missing one so Claude has
    # a coherent target.
    eff_duration = duration_sec if duration_sec else 60
    if lines_count:
        eff_lines = lines_count
    else:
        # Roughly: 1 line ≈ 4-5s of screen (dialogue + action beat). So a
        # 60s episode ~ 12-15 lines; 90s ~ 18-22; 30s ~ 6-8.
        eff_lines = max(3, min(40, round(eff_duration / 4.5)))
    lines_range_word = (
        f"{max(3, eff_lines-2)}-{eff_lines+2}"  # ±2 wiggle so Claude isn't pinned to exact number
    )
    # ── SPOKEN-WORD BUDGET (the real driver of episode runtime & chunk count) ──
    # The renderer (static/app.js segmenter — DO NOT TOUCH) packs the script into
    # ~14-15s Seedance chunks driven almost entirely by SPOKEN WORDS @2.65 wps.
    # A 60s episode needs ~100-110 spoken words to fill 4+ chunks. The batch path
    # historically framed length as a CEILING only ("≤N lines, режь беспощадно")
    # with NO word floor — so the writer skimped (e.g. 9 dialogue lines / 51
    # words → ~50s → only 2-3 chunks). We now give an explicit FLOOR tied to the
    # ≥4-chunks-per-minute requirement, separate from the action-line budget.
    eff_spoken_target  = round(eff_duration / 60 * 110)        # words actually spoken
    eff_spoken_floor   = round(eff_duration / 60 * 95)         # hard floor — below this = too few chunks
    eff_spoken_ceiling = round(eff_duration / 60 * 125)        # don't overflow the minute
    eff_dlg_lines      = eff_lines                              # DIALOGUE lines only (action excluded)
    eff_dlg_floor      = max(4, round(eff_dlg_lines * 0.8))
    eff_dlg_ceiling    = eff_dlg_lines + 3
    eff_action_budget  = max(3, round(eff_duration / 12))      # action lines — SEPARATE budget
    eff_min_chunks     = max(4, round(eff_duration / 15))      # renderer ≈ 1 chunk / 14-15s
    length_clause = (
        f"Каждая серия ≈ {eff_duration}с экрана: ~{eff_spoken_target} ПРОИЗНЕСЁННЫХ слов "
        f"(диапазон {eff_spoken_floor}–{eff_spoken_ceiling}), ~{eff_dlg_lines} реплик диалога, "
        f"+ до {eff_action_budget} action-строк. Это НИЖНЯЯ планка тоже — не недобирай, "
        f"иначе серия порежется всего на 2-3 чанка вместо нужных {eff_min_chunks}+. "
    )
    style_block = (f"\nСТИЛЬ РЕПЛИК: {style_clause}\n" if style_clause else '')
    # Scene-character cap directive. NOT about total cast size — about how
    # many MAIN characters actively drive any given scene. Crowds/extras
    # don't count. Default is 2, max 4 only for emotional climaxes.
    if max_main_chars:
        if max_main_chars == 1:
            crowd_clause = 'ОДИН главный персонаж на сцену (моно-сцены). Изредка может быть второй на короткую реплику.'
        elif max_main_chars == 2:
            crowd_clause = '2 главных персонажа в большинстве сцен (диалог). Изредка 3 на ключевые моменты. Никаких сцен где 4+ главных героев постоянно обсуждают.'
        else:
            crowd_clause = f'В большинстве сцен 2 главных персонажа, изредка 3, МАКСИМУМ {max_main_chars} ТОЛЬКО для эмоциональной кульминации (откровение, конфронтация всей семьи). Не делай сцен где {max_main_chars} главных героев постоянно мусолят одно — это вяло.'
        crowd_block = (
            f"\nЛИМИТ ПЕРСОНАЖЕЙ В СЦЕНЕ (главных): {max_main_chars}.\n"
            f"{crowd_clause}\n"
            "ВАЖНО — это НЕ запрет на массовку: сцены на свадьбе, вечеринке, "
            "митинге, в зале суда МОГУТ иметь толпу фоновых персонажей. Лимит "
            "только на основных героев которые активно ведут сцену (имеют реплики/действия).\n"
        )
    else:
        crowd_block = ''
    # HARD numeric caps + banned soap-opera tropes. Without this Claude
    # defaults to «voiceover-narrated paperwork-reveal» style and triples the
    # line count. Mirrors the DIALOGUE-FIRST rule from _IDEAS_SYSTEM but
    # applied at the script-writing stage where it actually constrains output.
    if eff_duration <= 35:
        max_scenes = 1
        scene_clause = "1 СЦЕНА на серию (одна локация, без переездов)."
    elif eff_duration <= 75:
        max_scenes = 2
        scene_clause = "МАКСИМУМ 2 сцены/локации на серию. Лучше — 1 непрерывная сцена."
    elif eff_duration <= 120:
        max_scenes = 3
        scene_clause = "МАКСИМУМ 3 сцены на серию. Не разбрасывайся локациями."
    else:
        max_scenes = 4
        scene_clause = f"МАКСИМУМ {max_scenes} сцены — не больше."
    hard_caps_block = (
        "\n=== ЖЁСТКИЕ ЛИМИТЫ (обязательные, проверяй САМ перед выводом) ===\n"
        "0. 🚨 ЛОКАЦИЯ — ПРАВИЛО №1, СТРОЖАЙШЕЕ:\n"
        "   ПОСЛЕ строки «Кратко: ...» ПЕРВАЯ строка серии = ЗАГОЛОВОК СЦЕНЫ С ЛОКАЦИЕЙ.\n"
        "   НЕ диалог. НЕ действие. СНАЧАЛА ЛОКАЦИЯ.\n"
        "   Формат: ИНТА. ENGLISH LOCATION NAME — ВРЕМЯ\n"
        "   Примеры: ИНТА. STORAGE UNIT — ДЕНЬ / ИНТА. HOTEL SUITE — УТРО / ИНТА. ROOFTOP TERRACE — НОЧЬ / ИНТА. HOSPITAL CORRIDOR — ВЕЧЕР\n"
        "   ❌ Избегай как основной локации: COURTROOM, LAW FIRM, JUDGE'S CHAMBERS, DEPOSITION ROOM, PROSECUTOR'S OFFICE, PRISON VISITING ROOM, EVIDENCE LOCKER — драма живёт в спальнях, кухнях, коридорах, отелях, машинах, на крышах, в больницах, НЕ в зданиях суда.\n"
        "   ❌ ЗАПРЕЩЕНО начинать серию так: 'SOPHIE: There's one more box.' (диалог без локации)\n"
        "   ❌ ЗАПРЕЩЕНО начинать серию так: 'Sophie открывает коробку.' (действие без локации)\n"
        "   ✅ ПРАВИЛЬНО: 'ИНТА. STORAGE UNIT — ДЕНЬ\\nSophie открывает коробку.'\n"
        "   Это правило применяется к КАЖДОЙ серии, даже если она продолжает ту же локацию.\n"
        "   Русские названия локаций в заголовках ЗАПРЕЩЕНЫ (не КАБИНЕТ — пиши FATHER'S STUDY).\n\n"
        "0b. 📍 БЛОКИ ПОЗИЦИЙ — ОБЯЗАТЕЛЬНО В КАЖДОЙ СЕРИИ:\n"
        "   [BLOCKING] — сразу после КАЖДОГО заголовка сцены (первого и каждого нового внутри серии):\n"
        "     [BLOCKING]\n"
        "     LOCATION: <English location name>\n"
        "     ИМЯ_ПЕРСОНАЖА: <что делает, где стоит/сидит> :: OUTFIT: <Outfit Name>\n"
        "     [/BLOCKING]\n"
        "   [BLOCKING_END] — в самом конце серии (последнее перед ничем):\n"
        "     [BLOCKING_END]\n"
        "     LOCATION: <English location name>\n"
        "     ИМЯ_ПЕРСОНАЖА: <финальная позиция> (по-русски)\n"
        "     [/BLOCKING_END]\n"
        "   Правила: [BLOCKING] перечисляет ТОЛЬКО персонажей ПРИСУТСТВУЮЩИХ В НАЧАЛЕ сцены. "
        "Описания позиций — по-русски, 1 строка на персонажа.\n"
        "   OUTFIT — КОРОТКОЕ Title Case ИМЯ ассета outfit'а (НЕ описание). Примеры: `Business Suit`, `Casual`, `Pajamas`, `Red Dress`, `School Uniform`, `Hospital Gown`, `Swimsuit`. Система по этому имени переиспользует тот же визуальный ассет в разных сценах.\n"
        "   Если в этой сцене НОВЫЙ outfit (ранее у этого перса такого имени не было) — добавь описание через пайп: `OUTFIT: Pajamas | OUTFIT_DESC: light blue cotton pajamas, bare feet`. Для уже введённых имён OUTFIT_DESC можно опустить — система знает описание.\n"
        "   DEDUP: НЕ плоди 10 имён для практически одинаковой одежды. Если перс в своём базовом образе — пиши `Base` или существующий лейбл. Новое имя = реальная смена костюма.\n"
        "   Если предоставлен PREV_END_POSITION и серия открывается в той же локации — [BLOCKING] ДОЛЖЕН СОВПАДАТЬ с ним.\n"
        "   ⛔ ЗАПРЕЩЕНО писать [SCENE_OPEN], [EPISODE_END] — это старые устаревшие теги. Только [BLOCKING]/[BLOCKING_END].\n\n"
        f"1. 🎯 ДЛИНА КАЖДОЙ СЕРИИ — это ДИАПАЗОН, который НУЖНО ПОПАСТЬ (не потолок!):\n"
        f"   • ПРОИЗНЕСЁННЫЕ СЛОВА (то что персонажи реально говорят вслух — диалог + VO): "
        f"ЦЕЛЬ ~{eff_spoken_target}, ДИАПАЗОН {eff_spoken_floor}–{eff_spoken_ceiling}. "
        f"⚠ НЕ НЕДОБИРАЙ ниже {eff_spoken_floor} — иначе серия выходит на {eff_duration//2}с и режется всего на 2-3 чанка "
        f"вместо нужных {eff_min_chunks}+ (рендер режет по словам ~2.65 сл/сек).\n"
        f"   • РЕПЛИКИ ДИАЛОГА: {eff_dlg_floor}–{eff_dlg_ceiling} строк (цель {eff_dlg_lines}). "
        f"Каждая реплика 4-9 слов; >12 слов — разбей на две короткие.\n"
        f"   • ACTION-СТРОКИ — ОТДЕЛЬНЫЙ бюджет, НЕ заменяют диалог: максимум {eff_action_budget}. "
        f"Нельзя добивать длину серии действиями вместо реплик — слова важнее.\n"
        f"   • VOICEOVER считается в слова, но НЕ строй серию на нём (макс 1-2 блока).\n"
        f"   САМОПРОВЕРКА: посчитай произнесённые слова. Меньше {eff_spoken_floor}? → ДОПИШИ диалог. "
        f"Больше {eff_spoken_ceiling}? → сократи. Цель — РОВНО на {eff_duration}с, не короче.\n"
        f"2. СЦЕНЫ: {scene_clause} Сцена = одна локация/время. Переезд = новая сцена. Каждая дополнительная сцена жрёт 3-4 реплики только на сетап.\n"
        "3. VOICEOVER: разрешён точечно (1-2 на серию максимум, как стилистический приём — открытие/закрытие). "
        "НЕ строй сюжет через закадр: откровения, эмоции, мотивацию персонажа показывай через диалог и действие, не через монолог в камеру. "
        "Если в серии 3+ VO-блока — это уже не сериал, а аудиокнига, переписывай.\n"
        "4. ЗАПРЕЩЕНО (нарушение = переписать с нуля):\n"
        "   • БУМАЖНЫЕ РАСКРЫТИЯ (paperwork reveals): нельзя двигать сюжет через письмо/завещание/документ/email/SMS/курьерский конверт/папку с бумагами/фото на телефоне/«экран ноутбука прокручивает документы»/диктофонную запись/USB-флешку/«запись с камеры». "
        "Откровения должны звучать ВСЛУХ из уст персонажа, не читаться с бумаги и не доставаться из конверта.\n"
        "   • ЮРИДИЧЕСКИЕ ДВИЖКИ (legal/courtroom engines): нельзя сводить сюжет к иску, суду, заседанию, прокурору, адвокату, судье, сбору улик/доказательств, «свидетели против него», «выиграем в суде», «возбуждено дело», «дача показаний», «экспертиза покажет». Зрителю вертикального видео не интересно смотреть процесс. "
        "Замена: прямая личная конфронтация / шантаж в лицо / преследование / похищение / физическое столкновение / разоблачение лицом к лицу / предательство близкого / угроза ребёнку / публичное унижение. Люди против людей, не люди против бумаг и не люди против системы правосудия.\n"
        "   • ФЛЭШБЕКИ и сны в первой серии. Только настоящее время.\n"
        "   • «Тем временем в…» / «А в это время…» — параллельный монтаж сложен и жрёт хронометраж.\n"
        "5. CLIFFHANGER в конце — да, но НЕ через прибывшее письмо/звонок/тайный документ/USB-флешку/запись с камеры. И НЕ через «увидимся в суде», «подаю иск завтра», «дело передано в суд». "
        "Лучше: фраза которая меняет всё, неожиданное появление человека, прямая угроза в лицо, действие которое нельзя отменить, оружие в кадре, удар, объятия с тем кого считали врагом.\n"
        "6. САМОПРОВЕРКА перед выводом каждой серии — посчитай:\n"
        f"   – Сколько ПРОИЗНЕСЁННЫХ слов? (ДОЛЖНО быть {eff_spoken_floor}–{eff_spoken_ceiling}, цель {eff_spoken_target}. "
        f"Меньше {eff_spoken_floor} = серия слишком короткая, ДОПИШИ диалог!)\n"
        f"   – Сколько реплик диалога? (должно быть {eff_dlg_floor}–{eff_dlg_ceiling})\n"
        f"   – Сколько action-строк? (≤ {eff_action_budget} — НЕ добивай длину действиями)\n"
        f"   – Сколько разных локаций/сцен? (должно быть ≤ {max_scenes})\n"
        "   – Сколько VO-блоков? (≤ 2, и сюжет НЕ должен ими двигаться)\n"
        "   – Двигается ли сюжет через бумагу/экран/запись? (должно быть НЕТ)\n"
        "   – Двигается ли сюжет через суд/иск/прокурора/сбор улик? (должно быть НЕТ)\n"
        "   – Локация сцены — не суд/юр.фирма/прокуратура как основное место действия? (должно быть НЕТ)\n"
        "   Если хоть один тест провален — перепиши серию до вывода.\n"
        + (
        "7. ПЕРЕБИВАНИЯ — ЖЁСТКИЙ ЗАПРЕТ: НИКОГДА не обрывай реплику персонажа на полуслове тире (—). "
        "Каждая произнесённая реплика — законченное предложение. ЗАПРЕЩЕНО: «ELENA: You should have—» или «(перебивает)». "
        "Видеогенератор рендерит обрезанные реплики как двух одновременно говорящих — это выглядит сломанным. "
        "Хочешь показать перебивание — заверши реплику + action line показывает физическое вмешательство + следующий персонаж говорит полную реплику.\n"
        if no_interruptions else
        "7. ПЕРЕБИВАНИЯ — РАЗРЕШЕНЫ: персонажи могут перебивать друг друга (обрыв фразы тире, «(перебивает)»). "
        "Это создаёт живой темп, но не злоупотребляй — не более 2-3 перебиваний на серию, только в эмоциональных пиках.\n"
        )
        + "===\n"
    )
    # ════════════════════════════════════════════════════════════════════════
    # HARD CONTRACT — TOP OF SYSTEM PROMPT. Two failure modes have plagued
    # this generator: (1) every episode synopsis defaults to paperwork-reveals
    # («показывает фото», «протягивает файл», «находит конверт», «достаёт USB
    # с записью») because that's the easiest 1-sentence device, (2) arcs
    # converge to courtroom / lawsuit / evidence-gathering because that's the
    # easiest macro engine. Both kill short-drama pacing. The contract below
    # is placed BEFORE everything else in the system message so the model
    # cannot skip it. The `Кратко:` line spec further down is bound to this
    # contract — if any banned token appears in Кратко, regenerate.
    # ════════════════════════════════════════════════════════════════════════
    hard_contract_block = (
        "╔════════════════════════════════════════════════════════════════════╗\n"
        "║  ЖЁСТКИЙ КОНТРАКТ — НАРУШЕНИЕ = ПЕРЕПИСАТЬ СЕРИЮ ЦЕЛИКОМ          ║\n"
        "╚════════════════════════════════════════════════════════════════════╝\n"
        "A. БУМАЖНЫЕ/ЭКРАННЫЕ НОСИТЕЛИ СЮЖЕТА — ЗАПРЕЩЕНЫ ВО ВСЕХ СЕРИЯХ.\n"
        "   Сюжет НЕ ДВИГАЕТСЯ через: фотографии, фотоснимки, фото на телефоне,\n"
        "   распечатанные фото, альбом с фото, ✦ВСЯКИЕ ФОТО ВООБЩЕ✦,\n"
        "   письма, записки, конверты, запечатанные пакеты, визитки, флаеры,\n"
        "   документы, контракты, договоры, завещания, файлы, папки, досье,\n"
        "   улики, доказательства собранные в папку, evidence binders,\n"
        "   SMS, мессенджеры, чаты, e-mail, переписку, скриншоты переписки,\n"
        "   экраны телефонов/ноутбуков/планшетов, любые UI на экране,\n"
        "   USB-флешки, micro-SD, жёсткие диски, «вот тут вся правда»,\n"
        "   диктофонные записи, voice memo, hidden mic, аудиозаписи,\n"
        "   запись с камер видеонаблюдения / CCTV / телеобъектив издалека,\n"
        "   дневники, voiceover, news headlines, газеты, новости по ТВ.\n"
        "   ❌ ЗАПРЕЩЕНО писать в Кратко: «показывает фото», «протягивает\n"
        "   конверт», «вручает визитку», «открывает папку», «приносит файл»,\n"
        "   «играет запись», «достаёт диктофон», «на флешке доказательства»,\n"
        "   «получает SMS», «на экране видно».\n"
        "   ✓ ВМЕСТО ЭТОГО: персонаж А ВСЛУХ обвиняет/угрожает/признаётся\n"
        "   персонажу Б в лицо. Откровения = устные конфронтации.\n"
        "   УЗКОЕ ИСКЛЮЧЕНИЕ — ОДИН раз на ВЕСЬ СЕРИАЛ (не на эпизод):\n"
        "   короткий физический предмет (кольцо, тест, ключ) показан 1-2с +\n"
        "   персонаж в той же фразе ВСЛУХ называет смысл. Если этот лимит уже\n"
        "   израсходован в предыдущих сериях — НИКАКИХ предметов вообще.\n"
        "\n"
        "B. ЮРИДИЧЕСКИЕ/СУДЕБНЫЕ ДВИЖКИ — ЗАПРЕЩЕНЫ КАК ДРАЙВЕР СЮЖЕТА.\n"
        "   Сериал НЕ должен сводиться к: иску, суду, заседанию, слушанию,\n"
        "   депозиции, приговору, обвинительному заключению, mediation,\n"
        "   сбору улик/доказательств как самостоятельной арке,\n"
        "   подготовке к процессу, поиску свидетелей для суда,\n"
        "   встречам с адвокатом/прокурором/детективом как климаксу,\n"
        "   опеке через суд, выселению через суд, разводу через суд как\n"
        "   главному движку, расследованию полиции как процедурной арке,\n"
        "   рейду как климаксу, ордеру на арест как кульминации.\n"
        "   ❌ ЗАПРЕЩЕНО в Кратко: «суд», «иск», «адвокат» как двигатель,\n"
        "   «прокурор», «свидетель против», «доказательства против него»,\n"
        "   «детектив приносит улики», «расследование вскрывает», «суд решит»,\n"
        "   «выходим в суд», «подаёт иск», «пересмотр опеки в суде»,\n"
        "   «передача дела в суд», «адвокатская фирма», «частный детектив\n"
        "   собрал доказательства», «инвестигатор показывает файл», «открывает\n"
        "   дело», «свидетельские показания», «слушание по опеке».\n"
        "   ✓ ВМЕСТО ЭТОГО (замены 1-к-1):\n"
        "   • «суд решит опеку»     → похищение ребёнка одним из родителей\n"
        "   • «адвокат принёс файл»  → бывший муж/любовник появляется на\n"
        "                              пороге и говорит правду в лицо\n"
        "   • «прокурор обвиняет»    → жертва даёт пощёчину в публичном месте\n"
        "   • «детектив принёс улики»→ родственник звонит в дверь со словами\n"
        "                              «я знаю что ты сделал» — устная сцена\n"
        "   • «сбор свидетелей»      → свидетель сам приходит и устраивает\n"
        "                              скандал на свадьбе/похоронах\n"
        "   • «расследование»        → личное преследование, погоня, засада\n"
        "   Закон может СУЩЕСТВОВАТЬ в мире (один намёк на одну серию: «мой\n"
        "   адвокат уже едет», полицейский в дверях на 5 секунд), но НЕ как\n"
        "   двигатель сюжета и НЕ как место действия.\n"
        "\n"
        "C. ЛОКАЦИИ — избегай как ОСНОВНОЕ место сцены: COURTROOM, LAW FIRM,\n"
        "   JUDGE'S CHAMBERS, DEPOSITION ROOM, PROSECUTOR'S OFFICE, DA'S\n"
        "   OFFICE, EVIDENCE LOCKER, PRISON VISITING ROOM (как повторяющееся),\n"
        "   POLICE STATION INTERROGATION ROOM (как климакс). Драма живёт в\n"
        "   спальнях, кухнях, коридорах, отелях, машинах, на крышах, в\n"
        "   больницах, в местах работы героев — НЕ в зданиях правосудия.\n"
        "\n"
        "D. САМОПРОВЕРКА КАЖДОЙ СЕРИИ перед выводом:\n"
        "   1) Содержит ли строка «Кратко: …» хоть одно слово из списка A или\n"
        "      B? → ДА = ПЕРЕПИШИ Кратко через устную конфронтацию.\n"
        "   2) Двигается ли центральное событие серии через бумагу/экран/\n"
        "      запись? → ДА = ПЕРЕПИШИ сцену через устное обвинение.\n"
        "   3) Это судебная/следственная сцена или подготовка к ней?\n"
        "      → ДА = ПЕРЕПИШИ через личное столкновение.\n"
        "   4) Локация сцены — не суд/прокуратура/юр.фирма? → ДОЛЖНО быть НЕТ.\n"
        "   Если хоть одна проверка провалена — НЕ ВЫВОДИ серию, перепиши.\n"
        "════════════════════════════════════════════════════════════════════\n\n"
    )
    system = (
        hard_contract_block
        + f"Ты — сценарист короткой драмы для вертикального TikTok/Reels. Пишешь {mode_label} на N серий. "
        f"{length_clause}Формат: "
        "имена ВЕРХНИМ регистром перед репликами, диалог короткий и накалённый, обязательный cliffhanger "
        "в конце КАЖДОЙ серии (открытый вопрос или новая угроза которая толкает к следующей).\n"
        f"{style_block}"
        f"{crowd_block}"
        f"{hard_caps_block}\n"
        + ("ПРАВИЛА ПИЛОТА И СТАРТОВОЙ ДУГИ:\n"
           "1. Если в roster уже есть персонажи — используй их имена дословно. Если roster пустой — "
           "сам придумай героев, дай каждому отчётливое имя и личность.\n"
           "2. Локации: если в roster есть — используй. Если нет — придумай простые однозначные "
           "(КОФЕЙНЯ, ОФИС, КВАРТИРА БРАТА). Не уходи в фэнтези-сеттинг если жанр reality/драма.\n"
           "3. Стиль/тон бери из жанра + tone из Bible. Если они пустые — пиши как короткая драма "
           "для соцсетей: высокая эмоция, простые конфликты, неожиданные повороты.\n"
           "4. Каждая серия имеет свой arc (начало → обострение → cliffhanger).\n"
           "5. За {count} серий построй мини-арк со сквозным конфликтом.\n\n"
            if from_scratch else
           "ПРАВИЛА ПРОДОЛЖЕНИЯ:\n"
           "1. Используй СУЩЕСТВУЮЩИХ персонажей и локации из roster (имена дословно). Новых вводи только "
           "если без них не обойтись по сюжету.\n"
           "2. Сохраняй tone и стиль предыдущих серий — посмотри последние 8 серий для калибровки.\n"
           "3. Каждая серия должна иметь свой arc (начало → обострение → cliffhanger), но быть частью общей дуги.\n"
           "4. Не повторяй уже произошедшие события дословно — двигай сюжет вперёд.\n"
           "5. Используй существующие сюжетные предметы (items) когда они уместны.\n"
           "6. Открытые линии из предыдущих серий — либо двигай их, либо логично откладывай.\n\n")
        + "ФОРМАТ ВЫХОДА — СТРОГО:\n"
        + f"Episode {first_new_num}: <короткое название серии — БЕЗ слов 'Photo', 'File', 'Evidence', 'Investigator', 'Letter', 'Court', 'Trial', 'Hearing', 'Lawyer', 'Witness'>\n"
        + f"Кратко: <1-2 предложения о чём серия — ОБЯЗАТЕЛЬНО через устную конфронтацию двух людей; БЕЗ упоминания фото/файла/конверта/визитки/папки/USB/SMS/экрана/записи/иска/суда/адвоката/прокурора/детектива-с-уликами>\n"
        + f"ИНТА. LOCATION NAME — ВРЕМЯ  ← ОБЯЗАТЕЛЬНО, первая строка до любого диалога/действия. Не COURTROOM / LAW FIRM / DA'S OFFICE как основная локация.\n"
        + f"<реплики и действия персонажей — диалог, action lines. Сюжет двигается только через устную речь и физическое действие, НЕ через бумагу/экран/запись>\n"
        + "\n"
        + f"Episode {first_new_num + 1}: <название без запретных слов>\n"
        + f"Кратко: <синопсис — устная конфронтация, без запретных носителей>\n"
        + f"ИНТА. LOCATION NAME — ВРЕМЯ  ← обязательно каждый раз\n"
        + f"<содержимое>\n"
        + "\n"
        + f"... и так далее до Episode {last_new_num}.\n\n"
        + f"⏱ ХРОНОМЕТРАЖ — ЖЁСТКО И В ОБЕ СТОРОНЫ: каждая серия = ~{eff_duration}с экрана. "
        + f"Это значит {eff_spoken_floor}–{eff_spoken_ceiling} ПРОИЗНЕСЁННЫХ слов (цель {eff_spoken_target}). "
        + f"⚠ НЕДОБОР так же плох как перебор: серия на {eff_spoken_floor-20} слов выходит на {eff_duration//2}с и режется на 2-3 чанка вместо {eff_min_chunks}+. "
        + f"Если слов меньше {eff_spoken_floor} — ДОПИШИ живой диалог (короткие реплики, реакции, обострение), НЕ растягивай action. "
        + f"Если больше {eff_spoken_ceiling} — сократи или перенеси в следующую серию. РОВНО ~{eff_duration}с на каждую серию.\n"
        + "Каждая серия начинается с СТРОГО строки 'Episode N: <title>' — без других маркеров. "
        + "Никакой markdown, никаких '===', никаких '#'. Только plain text. "
        + ("Язык по контексту: если синопсис/направление на русском — пишем по-русски; "
           "если на английском — по-английски.\n\n" if from_scratch else
           "Язык — тот же что в предыдущих сериях (русский/английский/смесь — сохраняй стиль).\n\n")
        + "ВАЖНО: возвращай ТОЛЬКО сценарий, без преамбулы 'Вот сценарий:' и без post-комментариев."
    )
    # Build narrative history + device history blocks for batch gen
    try:
        _batch_devices_block = _build_plot_device_history(sid, first_new_num)
    except Exception:
        _batch_devices_block = ''
    try:
        _batch_narrative_block = _build_narrative_state_block(sid, first_new_num)
    except Exception:
        _batch_narrative_block = ''
    _beats_block = _series_beats_episode_block(s)
    _source_outline_block = _source_outline_episode_block(s, first_new_num, last_new_num)
    # Finale awareness — if the pinned finale falls inside the [first..last] range
    # this bulk write covers, the finale episode must RESOLVE (no cliffhanger),
    # overriding the per-episode "обязательно cliffhanger" rule below.
    _fin_ep = finale_episode_num(s)
    _finale_in_range = _fin_ep is not None and first_new_num <= _fin_ep <= last_new_num
    _batch_finale_note = ''
    if _finale_in_range:
        _fin_desc = ((s.get('finale') or {}).get('description') or '').strip()
        _batch_finale_note = (
            f"\n\n🏁🏁🏁 ВНИМАНИЕ: Эп.{_fin_ep} В ЭТОМ ДИАПАЗОНЕ — ЭТО ФИНАЛ СЕРИАЛА (последняя серия). "
            f"Эп.{_fin_ep+1} НЕ СУЩЕСТВУЕТ.\n"
            f"Для Эп.{_fin_ep} правило «обязательный cliffhanger в конце» НЕ ДЕЙСТВУЕТ — наоборот:\n"
            f"• закрой ВСЕ открытые сюжетные линии прямо на экране — никаких «решится завтра», "
            f"«to be addressed», «setup for next episode», отложенной расплаты;\n"
            f"• исполни зафиксированные события финала ТОЧНО (те самые персонажи, развязки, примирения, финальный кадр);\n"
            f"• дай эмоциональный катарсис и заверши КОНКЛЮЗИВНЫМ финальным битом — НЕ клиффхэнгером, "
            f"НЕ новой угрозой/загадкой, НЕ заделом на следующую серию.\n"
            f"Все серии ДО Эп.{_fin_ep} в этом диапазоне заканчиваются клиффхэнгером как обычно.\n"
            f"ЗАФИКСИРОВАННЫЙ ФИНАЛ (исполни как конечное состояние полностью):\n{_fin_desc}\n"
        )
    user_msg = (
        f"СЕРИАЛ: «{s.get('title') or 'untitled'}»\n"
        f"Жанр: {s.get('genre') or '?'} · Тон: {s.get('tone') or '?'} · "
        f"Аудитория: {s.get('target_audience') or '?'}\n"
        f"{('Мир: ' + (s.get('world_description') or '')[:300] + chr(10)) if s.get('world_description') else ''}"
        f"ROSTER ПЕРСОНАЖЕЙ: {chars_list or '(пусто — можешь придумать сам)' if from_scratch else (chars_list or '(пусто)')}\n"
        f"ROSTER ЛОКАЦИЙ:    {locs_list or '(пусто — придумай простые)' if from_scratch else (locs_list or '(пусто)')}\n"
        f"СЮЖЕТНЫЕ ПРЕДМЕТЫ: {items_list or '(пусто)'}\n"
        f"{_beats_block}\n"
        f"{_source_outline_block}"
        f"{earlier_block}"
        + (f"ПОСЛЕДНИЕ {len(verbatim_window)} СЕРИЙ (verbatim, для тонкой калибровки стиля и continuity):\n```\n{verbatim_block}\n```\n\n"
           if verbatim_block else '')
        + f"{direction_block}\n"
        + (_batch_narrative_block if _batch_narrative_block else '')
        + (_batch_devices_block if _batch_devices_block else '')
        + (f"НАПИШИ ПЕРВЫЕ {count} СЕРИЙ (Эп.{first_new_num}–{last_new_num}). "
            if from_scratch else
           f"НАПИШИ СЛЕДУЮЩИЕ {count} СЕРИЙ (Эп.{first_new_num}–{last_new_num}). ")
        + f"Каждая РОВНО ≈ {eff_duration}с экрана = {eff_spoken_floor}–{eff_spoken_ceiling} произнесённых слов "
        + f"(цель {eff_spoken_target}), ~{eff_dlg_lines} реплик диалога, до {eff_action_budget} action-строк. "
        + f"НЕ НЕДОБИРАЙ (короткая серия = 2-3 чанка вместо {eff_min_chunks}+) и не переполняй. Обязательно cliffhanger в конце.\n\n"
        + "🚨 ПОСЛЕДНЯЯ ПРОВЕРКА ПЕРЕД ВЫВОДОМ — пройдись по КАЖДОЙ серии:\n"
        + "  ✗ Если в строке «Кратко: …» есть слова: фото, фотограф, фотоснимок, файл, папка, конверт, "
        + "записка, письмо, визитка, USB, флешка, диктофон, запись, камера наблюдения, телеобъектив, SMS, "
        + "переписка, экран, документ, контракт, завещание, дневник, газета — ПЕРЕПИШИ Кратко с нуля через "
        + "устную конфронтацию двух людей лицом к лицу.\n"
        + "  ✗ Если в Кратко или в теле серии есть: иск, суд, адвокат, прокурор, детектив-с-уликами, "
        + "слушание, заседание, депозиция, опека-через-суд, расследование-как-арка, ордер, рейд, evidence — "
        + "ПЕРЕПИШИ через личное столкновение (конфронтация, шантаж в лицо, преследование, похищение, "
        + "публичное унижение, физический удар, неожиданное появление человека).\n"
        + "  ✗ Если локация сцены — COURTROOM / LAW FIRM / DA'S OFFICE / JUDGE'S CHAMBERS / DEPOSITION ROOM "
        + "— ПЕРЕПИШИ сцену в спальне / кухне / отеле / коридоре / больнице / машине / на крыше.\n"
        + f"  ✗ Если в серии МЕНЬШЕ {eff_spoken_floor} произнесённых слов — серия СЛИШКОМ КОРОТКАЯ, ДОПИШИ диалог до ~{eff_spoken_target} "
        + f"(порежется на 2-3 чанка вместо {eff_min_chunks}+). Если больше {eff_spoken_ceiling} — сократи/перенеси.\n"
        + "Эти проверки делай для КАЖДОЙ из серий перед выводом. Не выводи серию, которая хоть одну проверку провалила."
        + _batch_finale_note   # ← finale override, appended LAST so it wins for the finale episode
    )
    try:
        # Allow up to 24K output for 5+ episodes.
        raw = claude_ask(user_msg, system=system, max_tokens=24000)
        # Strip any code-fence accidents
        text = raw.strip()
        if text.startswith('```'):
            # Drop first line + last line if they're fence markers
            lines = text.split('\n')
            if lines[0].startswith('```'): lines = lines[1:]
            if lines and lines[-1].startswith('```'): lines = lines[:-1]
            text = '\n'.join(lines).strip()

        # ── PER-EPISODE LENGTH SAFETY NET ──────────────────────────────────
        # Instructions alone don't guarantee length — the model still skimps on
        # some episodes (esp. early ones in a batch), producing scripts that the
        # renderer slices into only 2-3 chunks. We measure EACH episode with the
        # same detector the single-episode path uses and, if any UNDERSHOOT, do
        # ONE corrective pass that names the short episodes + their word deficit
        # and asks for the full batch back with those episodes expanded. We do
        # NOT touch the chunking system — we only push the WRITER to hit length.
        _len_series = {'target_duration_sec': eff_duration}
        def _short_episodes(script_text):
            shorts = []
            for ep in (_split_script_into_episodes(script_text) or []):
                body = ep.get('body') or ''
                m = _script_runtime_metrics(body)
                # Undershoot = rendered runtime well under target OR spoken words
                # below the floor (either → too few chunks).
                if (m['est_runtime_sec'] < eff_duration * 0.85) or (m['dialogue_words'] < eff_spoken_floor):
                    shorts.append({
                        'number': ep.get('number'),
                        'title': ep.get('title') or '',
                        'words': m['dialogue_words'],
                        'est': m['est_runtime_sec'],
                        'deficit': max(0, eff_spoken_target - m['dialogue_words']),
                    })
            return shorts

        shorts = _short_episodes(text)
        if shorts:
            short_list = '; '.join(
                f"Эп.{x['number']} ({x['words']} слов ≈ {x['est']}с — добавь ещё ~{x['deficit']} слов)"
                for x in shorts
            )
            print(f'[batch-length] {len(shorts)} short episode(s): {short_list}', flush=True)
            fix_msg = (
                "Ниже — сгенерированный многосерийный сценарий. ЧАСТЬ СЕРИЙ СЛИШКОМ КОРОТКИЕ: "
                "у них мало ПРОИЗНЕСЁННЫХ слов, поэтому рендер порежет их всего на 2-3 чанка "
                f"вместо нужных {eff_min_chunks}+.\n\n"
                f"СЕРИИ ТРЕБУЮЩИЕ РАСШИРЕНИЯ: {short_list}.\n\n"
                f"ЗАДАЧА: верни ВЕСЬ сценарий целиком (все {count} серий, Эп.{first_new_num}–{last_new_num}, "
                "в том же формате 'Episode N: …' + 'Кратко: …' + тело), но КАЖДУЮ помеченную серию "
                f"допиши до {eff_spoken_floor}–{eff_spoken_ceiling} произнесённых слов (цель {eff_spoken_target}). "
                "КАК расширять — правильно:\n"
                "• добавляй КОРОТКИЕ живые реплики (4-9 слов): реакции, возражения, подколы, угрозы, признания;\n"
                "• углубляй конфликт сцены — больше обмена ударами между персонажами;\n"
                "• НЕ добивай длину action-строками («он смотрит», «пауза») и НЕ растягивай монологами;\n"
                "• сохрани cliffhanger, локации, [BLOCKING] блоки и сюжет — меняется только плотность диалога;\n"
                "• серии, которые НЕ помечены, оставь как есть.\n"
                "Соблюдай ВСЕ прежние запреты (никаких бумаг/экранов/судов). Верни ТОЛЬКО сценарий."
            )
            try:
                raw2 = claude_ask(fix_msg + "\n\n=== СЦЕНАРИЙ ДЛЯ ДОРАБОТКИ ===\n" + text,
                                  system=system, max_tokens=24000)
                text2 = raw2.strip()
                if text2.startswith('```'):
                    l2 = text2.split('\n')
                    if l2[0].startswith('```'): l2 = l2[1:]
                    if l2 and l2[-1].startswith('```'): l2 = l2[:-1]
                    text2 = '\n'.join(l2).strip()
                # Accept the retry only if it actually reduced the shortfall and
                # still splits into the expected episode count (guard against the
                # model returning a partial / mangled batch).
                eps2 = _split_script_into_episodes(text2)
                if eps2 and len(_short_episodes(text2)) < len(shorts):
                    print(f'[batch-length] corrective pass improved: '
                          f'{len(shorts)} → {len(_short_episodes(text2))} short', flush=True)
                    text = text2
                else:
                    print('[batch-length] corrective pass did not improve — keeping original', flush=True)
            except Exception as _re:
                print(f'[batch-length] corrective pass FAILED: {_re}', flush=True)

        payload = {
            'script': text,
            'first_episode': first_new_num,
            'last_episode':  last_new_num,
            'count': count,
            'from_scratch': from_scratch,
        }
        # Generated text SHOULD be English dialogue per _BATCH_SCRIPT_SYSTEM,
        # but Claude occasionally drifts to Russian when the bible / direction
        # is in Russian. Detect and warn so the UI can offer adaptation
        # before the user appends these episodes to the series.
        lang_info = _detect_dialogue_language(text)
        if lang_info['ratio'] > 0.15 and lang_info['non_english_lines'] > 0:
            payload['dialogue_lang_warning'] = lang_info
        return jsonify(payload)
    except Exception as e:
        _log_event('WARN', 'generate_script_batch_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/append-from-script', methods=['POST'])
def append_from_script(sid):
    """Append a multi-episode script to an EXISTING series. Splits the pasted
    text into episodes (using the same regex pipeline as /import-from-script),
    numbers them continuing from the highest existing episode in this series,
    and kicks off the entity-extraction worker. Returns immediately so the UI
    can poll /import-status for progress.

    Body:
      {script: str, extract_entities?: bool}
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    do_extract = bool(data.get('extract_entities', True))
    dialogue_lang_hint = (data.get('dialogue_language_hint') or '').strip().lower()
    if dialogue_lang_hint and dialogue_lang_hint not in ('en', 'english'):
        s['dialogue_language_hint'] = dialogue_lang_hint
        save_series(sid, s)

    eps = _split_script_into_episodes(script)
    if not eps:
        return jsonify({'error': 'script split produced no episodes'}), 400

    # Find the next free episode number — keep continuous numbering so the
    # editor's «соседи» strip stays usable.
    existing_eps = list_episodes(sid)
    next_num = (max((e.get('number') or 0) for e in existing_eps) + 1) if existing_eps else 1

    ep_records = []
    for offset, e in enumerate(eps):
        num = next_num + offset
        ep_dict = {
            'number': num,
            'title':  e['title'] or f'Эпизод {num}',
            'synopsis': '',
            'script':   e['body'],
            'characters_used': [],
            'locations_used':  [],
            'items_used':      [],
            'notes': '', 'reteller_prompt': '',
            'status': 'draft', 'ready': False,
            'created_at': datetime.datetime.utcnow().isoformat(),
        }
        save_episode(sid, num, ep_dict)
        # Sync [BLOCKING] outfits on append too — same Margaret-prevention rule
        # as the bulk import path. Idempotent & cheap.
        try:
            _sync_script_outfits(sid, e['body'])
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_append_failed',
                       sid=sid, ep=num, err=str(_oe)[:200])
        # Extract plot devices for anti-repetition tracking (best-effort, non-blocking)
        try:
            append_devices = _extract_devices_from_script(e['body'])
            if append_devices:
                ep_dict['plot_devices'] = append_devices
                save_episode(sid, num, ep_dict)
                _update_devices_index(sid, num, append_devices)
        except Exception as _ade:
            _log_event('WARN', 'device_extract_append_failed', ep=num, err=str(_ade)[:200])
        ep_records.append({'number': num})

    if do_extract and ep_records:
        _spawn_with_keys(_import_worker, sid, ep_records)

    return jsonify({
        'sid': sid,
        'first_episode': ep_records[0]['number'] if ep_records else None,
        'last_episode':  ep_records[-1]['number'] if ep_records else None,
        'episodes_appended': len(ep_records),
        'extraction_started': do_extract and bool(ep_records),
    }), 201


@app.route('/api/series/<sid>/backfill-devices', methods=['POST'])
def backfill_devices(sid):
    """Extract plot_devices and narrative_state for all episodes that don't have them yet.
    Useful for series created before the device/narrative registry was introduced.
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    episodes = list_episodes(sid)
    updated_devices = 0
    updated_narrative = 0
    for ep in sorted(episodes, key=lambda e: e.get('number', 0)):
        ep_num = ep.get('number', 0)
        script = ep.get('script', '')
        if not script:
            continue
        if not ep.get('plot_devices'):
            devices = _extract_devices_from_script(script)
            ep['plot_devices'] = devices or []
            save_episode(sid, ep_num, ep)
            if devices:
                _update_devices_index(sid, ep_num, devices)
            updated_devices += 1
        if not ep.get('narrative_state'):
            narrative = _extract_narrative_state_from_script(script)
            if narrative:
                ep['narrative_state'] = narrative
                save_episode(sid, ep_num, ep)
                _update_narrative_index(sid, ep_num, narrative)
                updated_narrative += 1
    return jsonify({'updated_devices': updated_devices, 'updated_narrative': updated_narrative})


@app.route('/api/series/<sid>/reextract', methods=['POST'])
def reextract_series(sid):
    """Re-runs the per-episode entity extractor on an already-imported series.
    Use case: an earlier import partially failed (LLM JSON parse error,
    server restart killed the worker, etc.) and chars/items are missing.
    Walks every existing episode that has a script and queues the same worker
    used by /import-from-script. Skips episodes that already have ALL three
    of (characters_used, locations_used, items_used) populated unless
    body.force is true."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    force = bool(body.get('force', False))
    eps = list_episodes(sid)
    targets = []
    for ep in sorted(eps, key=lambda e: e['number']):
        if not (ep.get('script') or '').strip():
            continue
        if not force:
            has_chars = bool(ep.get('characters_used'))
            has_locs  = bool(ep.get('locations_used'))
            has_items = bool(ep.get('items_used'))
            if has_chars and has_locs and has_items:
                continue
        targets.append({'number': ep['number']})
    if not targets:
        return jsonify({'queued': 0, 'message': 'all episodes already have entities (use force=true to redo)'}), 200
    _spawn_with_keys(_import_worker, sid, targets)
    return jsonify({'queued': len(targets), 'started': True}), 202


@app.route('/api/series', methods=['POST'])
def create_series():
    data = request.json
    slug = slugify(data.get('title', ''))
    sid = slug if slug and not (user_root() / slug).exists() else f"{slug}-{str(uuid.uuid4())[:6]}"
    # Format mode controls generators across the board: 'short_drama' (default, TikTok/ReelShort
    # addictive serial) or 'instagram_series' (standalone episodes, simpler titles, character-of-week).
    _format_mode_raw = (data.get('format_mode') or 'short_drama').strip().lower()
    _format_mode = _format_mode_raw if _format_mode_raw in ('short_drama', 'instagram_series') else 'short_drama'
    series_data = {
        'id': sid,
        'title': data['title'],
        # Deterministic episode titles `<Series_Title>_E<N>` for all new series.
        'episode_title_format': 'series_indexed',
        'genre': data.get('genre', ''),
        'tone': data.get('tone', ''),
        'target_audience': data.get('target_audience', ''),
        'world_description': data.get('world_description', ''),
        'synopsis': data.get('synopsis', ''),
        'format_mode': _format_mode,
        # Scenario constructor: ORDERED hook-beat sequence (ноды) assembled in
        # the create modal. Stored resolved as {id, ru, beat} (id='' for custom
        # free-text beats) so episode generators replay it in order — see
        # _series_beats_episode_block. Order is significant.
        'beat_sequence': _resolve_beats(data.get('beats') or []),
        # Per-episode outline carried in from a deep-analyzed top drama: the
        # first episodes are written to these beats, with THIS series' own cast
        # (the source names in the beats are placeholders). See _source_outline_episode_block.
        'source_episode_outline': [str(x).strip() for x in (data.get('source_episode_outline') or []) if str(x).strip()][:5],
        # Creative-writing model selector (ideas + episode scripts). Set at
        # creation time, can be overridden per-call from UI. Whitelist enforced
        # in _resolve_writer_model. Unknown / missing → default Claude.
        'writer_model': (data.get('writer_model') or '').strip().lower() or WRITER_MODEL_DEFAULT,
        'auto_generate_assets': bool(data.get('auto_generate_assets', True)),
        'batch_mode':           bool(data.get('batch_mode', False)),
        'batch_size':           int(data.get('batch_size', 5)) if data.get('batch_mode') else 1,
        # Episode duration target — accepts None (server default = 60s applied
        # downstream by the writer prompt). Clamp to writer-safe range matching
        # the bible modal's validator. Skip the override entirely on bad input.
        'target_duration_sec':  (
            max(30, min(240, int(data['target_duration_sec'])))
            if str(data.get('target_duration_sec') or '').strip().lstrip('-').isdigit()
            else None
        ),
        # Skip the legacy stage-1/2 milestones pipeline — new series start with empty
        # episode list. User adds episodes manually + optionally pins checkpoints / finale.
        'stage': 4,
        'arc': None,
        'milestone_synopses': {},
        'checkpoints': [],   # [{episode: int, description: str}]  story landmarks
        'finale': None,      # {episode: int, description: str} | None
        'created_at': datetime.datetime.utcnow().isoformat(),
        'video_provider': 'seedance',  # default for NEW series — Seedance mode active
        'characters': [],
        'locations': [],
        'items': [],                   # story-relevant props (handbag, gun, locket...)
        'devices_index': {},           # plot-device anti-repetition registry
        'cadence_policy': {'default_min_gap': 4, 'hard_limit': 3},
        'style': {
            'type': 'cinematic',
            'custom_description': '',
            'ref_images': []
        },
        'settings': {
            'voice':                'Enceladus',
            'tts_provider':         'elevenlabs',
            'image_provider':       'banana',         # → Reteller "Banana Pro"
            'aspect_ratio':         '9:16',
            'language':             'English',
            'duration':             'auto-frames',    # Reteller "Auto-frames" mode
            'enable_music':         True,
            'music_volume':         0.30,             # 30%
            'enable_animation':     True,
            'animation_speed':      'fast',
            'animation_resolution': '480p',
            'animation_model':      'seedance-2-ref', # → Reteller "Seedance 2.0 Ref"
            'enable_grid':          False,            # animation grid (frame grid overlay) OFF
            'cinema':               False,
            'trim':                 True,
            'no_fades':             True,
            'multi_voice':          False,
            'enable_subtitles':     False,
            'image_size':           '1K'
        }
    }
    # Pre-confirm the era for asset generation from the create-modal pick, so
    # character/portrait generation uses the right period immediately and the
    # user isn't re-asked via the «Ваш сериал в сеттинге X?» banner (which, if
    # ignored, used to silently generate modern-day characters). Only applied
    # for an EXPLICIT non-default pick — pure modern+realistic is left on 'auto'
    # so free-text-described periods still get auto-detection + the banner.
    _era_pick   = (data.get('era') or 'modern').strip().lower()
    _world_pick = (data.get('world_setting') or 'realistic').strip().lower()
    if _era_pick not in ('', 'modern') or _world_pick not in ('', 'realistic'):
        _mapped_era = _modal_setting_to_era_choice(
            data.get('era'), data.get('era_custom'),
            data.get('world_setting'), data.get('world_custom'),
            synopsis_text=' '.join(filter(None, [
                series_data.get('synopsis', ''), series_data.get('world_description', ''),
            ])),
        )
        if _mapped_era:
            series_data['era_choice'] = _mapped_era
            series_data['era_confirmed'] = True
    save_series(sid, series_data)
    scaffold_info = scaffold_series_folders(sid, data['title'])
    series_data['_scaffold'] = scaffold_info
    trigger_autogen_if_enabled(sid)
    return jsonify(series_data), 201


@app.route('/api/series/clone-from', methods=['POST'])
def clone_series_from():
    """Create a NEW series as a revised clone of an existing one.

    Body: {
      source_sid: str,                # series to clone from (required)
      title: str,                     # new series title (optional — defaults to «<src> (вариант)»)
      revision_instructions: str,     # free-text edits, e.g. «главная героиня молодая и красивая»
      episodes_to_copy: int,          # how many leading episodes to copy (0/absent = all)
      writer_model: str,              # optional override for the LLM revision pass
    }

    Behaviour (per the user's spec):
      • Deep-copies the source bible + assets + first N episode scripts into the new series.
      • Applies the revision instructions to the BIBLE and CAST immediately (one LLM pass):
        synopsis/world/tone/arc get rewritten only if the plot changes; each character's
        look/name/age is updated; changed portraits are wiped so autogen regenerates them
        with the new (e.g. beautiful) description.
      • Episode scripts are copied verbatim. We do NOT mass-rewrite them. Renamed characters
        are fixed in-place with a programmatic find/replace. Episodes that genuinely need a
        rewrite (the plot changed in them, or an age move could create a timeline
        contradiction) are queued in revision_plan.pending_episodes for one-by-one rewriting
        via /episodes/<num>/apply-revisions.
      • Future script/synopsis generation honours revision_instructions automatically
        (see _revision_instructions_block).
    """
    data = request.json or {}
    source_sid = (data.get('source_sid') or '').strip()
    title = (data.get('title') or '').strip()
    revision_instructions = (data.get('revision_instructions') or '').strip()
    try:
        episodes_to_copy = int(data.get('episodes_to_copy'))
    except (TypeError, ValueError):
        episodes_to_copy = 0  # 0 / missing → copy all
    if episodes_to_copy < 0:
        episodes_to_copy = 0

    if not source_sid:
        return jsonify({'error': 'source_sid required'}), 400
    src = load_series(source_sid)
    if not src:
        return jsonify({'error': 'source series not found'}), 404
    if not title:
        title = f"{src.get('title', 'Series')} (вариант)"

    slug = slugify(title)
    sid = slug if slug and not (user_root() / slug).exists() else f"{slug}-{str(uuid.uuid4())[:6]}"

    # ── 1) Deep-copy the bible, re-stamp identity, drop instance-specific state.
    new_series = copy.deepcopy(src)
    new_series['id'] = sid
    new_series['title'] = title
    # Deterministic episode titles `<Series_Title>_E<N>` for all new series
    # (clones included — copied episodes get re-titled to the new series name).
    new_series['episode_title_format'] = 'series_indexed'
    new_series['created_at'] = datetime.datetime.utcnow().isoformat()
    new_series['cloned_from'] = source_sid
    new_series['revision_instructions'] = revision_instructions
    if (data.get('writer_model') or '').strip().lower():
        new_series['writer_model'] = data['writer_model'].strip().lower()
    # Fresh cover (poster reflects the new title / possibly new looks).
    new_series['cover_image'] = ''
    new_series['cover_image_url'] = ''
    new_series['cover_image_version'] = 0
    for k in ('archived', 'pinned', 'pinned_at', '_scaffold', '_clone'):
        new_series.pop(k, None)

    save_series(sid, new_series)
    scaffold_info = scaffold_series_folders(sid, title)

    # ── 2) Copy asset reference images so character/location ref_images resolve.
    try:
        src_assets = assets_dir(source_sid)
        if src_assets.exists():
            shutil.copytree(str(src_assets), str(assets_dir(sid)), dirs_exist_ok=True)
    except Exception as e:
        print(f'[clone] asset copy warning: {e}', flush=True)

    # ── 3) Copy episode scripts (first N, or all). We copy ONLY story content and
    #       drop ALL generation/render state — seedance_chunks, assembled video,
    #       music scenes, reteller prompt/project, batch caches, etc. all reference
    #       rendered media in the source's OUT/VID dirs (which we do NOT copy), so
    #       carrying them over leaves "generated chunks with empty videos" in the
    #       clone (the exact bug this guards against). Allowlist (not denylist) so
    #       any future render field is dropped by default rather than leaking.
    _EP_CONTENT_KEYS = {
        'number', 'title', 'synopsis', 'script', 'characters_used', 'locations_used',
        'items_used', 'notes', 'character_outfits', 'ready', 'status', 'cast_extracted',
        'created_at', 'plot_devices', 'days_since_previous', 'scene_blocking',
    }
    src_eps = sorted(list_episodes(source_sid), key=lambda e: int(e.get('number', 0) or 0))
    src_total = len(src_eps)
    if episodes_to_copy > 0:
        src_eps = [e for e in src_eps if int(e.get('number', 0) or 0) <= episodes_to_copy]
    copied_numbers = []
    for ep in src_eps:
        num = int(ep.get('number', 0) or 0)
        if num <= 0:
            continue
        ep_copy = {k: copy.deepcopy(v) for k, v in ep.items() if k in _EP_CONTENT_KEYS}
        ep_copy['number'] = num
        # Fresh, un-generated render state — nothing is assembled yet for the clone.
        ep_copy['gen_status'] = ''
        ep_copy['reteller'] = {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None}
        save_episode(sid, num, ep_copy)
        copied_numbers.append(num)

    # ── 4) Apply revisions to the bible + cast (one LLM pass).
    revision_plan = {
        'scope': 'character_only',
        'reason': '',
        'pending_episodes': [],
        'renames': [],
        'applied_at': datetime.datetime.utcnow().isoformat(),
    }
    if revision_instructions:
        result = _llm_apply_revisions_to_bible(new_series, revision_instructions)
        if result:
            bible = result.get('bible') or {}
            for k in ('genre', 'tone', 'world_description', 'synopsis', 'arc'):
                v = (bible.get(k) or '').strip()
                if v:
                    new_series[k] = v
            char_by_id = {c.get('id'): c for c in new_series.get('characters', [])}
            renames = []
            for upd in (result.get('characters') or []):
                c = char_by_id.get(upd.get('id'))
                if not c:
                    continue
                old_name = c.get('name', '')
                if upd.get('name_changed') and (upd.get('name') or '').strip():
                    new_name = upd['name'].strip()
                    if new_name != old_name:
                        c['name'] = new_name
                        renames.append({'old': old_name, 'new': new_name})
                if upd.get('appearance_changed') and (upd.get('appearance') or '').strip():
                    # Scrub before persisting — a revision like «героиня должна
                    # быть очень красивой и сексуальной» must NOT bake sexualized
                    # wording into the canonical appearance that rides into every
                    # downstream prompt (this was the moderation bug).
                    c['appearance'] = _sanitize_appearance_for_moderation(upd['appearance'].strip())
                    # Wipe portrait + outfit refs so autogen regenerates the new look.
                    c['ref_images'] = []
                    for o in (c.get('outfits') or []):
                        o['ref_images'] = []
                if (upd.get('gender') or '').strip().lower() in ('male', 'female'):
                    c['gender'] = upd['gender'].strip().lower()
                if (upd.get('description') or '').strip():
                    c['description'] = upd['description'].strip()
            for r in (result.get('renames') or []):
                o = (r.get('old') or '').strip()
                n = (r.get('new') or '').strip()
                if o and n and o != n and not any(x['old'] == o for x in renames):
                    renames.append({'old': o, 'new': n})
            revision_plan['renames'] = renames
            revision_plan['scope'] = (result.get('revision_scope') or 'character_only').strip().lower()
            revision_plan['reason'] = (result.get('rewrite_reason') or '').strip()

            # Programmatic name find/replace across copied scripts (word-boundary).
            if renames and copied_numbers:
                for num in copied_numbers:
                    ce = load_episode(sid, num)
                    if not ce:
                        continue
                    sc = ce.get('script') or ''
                    changed = False
                    for r in renames:
                        new_sc, n = re.subn(r'\b' + re.escape(r['old']) + r'\b', r['new'], sc)
                        if n:
                            sc, changed = new_sc, True
                    if changed:
                        ce['script'] = sc
                        save_episode(sid, num, ce)

            # Queue episodes for one-by-one rewrite when the plot changed or an age
            # move risks a timeline contradiction. Pure name/appearance edits → no rewrite.
            ages_changed = any(u.get('age_changed') for u in (result.get('characters') or []))
            if (revision_plan['scope'] == 'plot' or ages_changed) and copied_numbers:
                revision_plan['pending_episodes'] = list(copied_numbers)

    new_series['revision_plan'] = revision_plan
    save_series(sid, new_series)

    # ── 5) Regenerate wiped portraits with the new descriptions + any missing assets.
    trigger_autogen_if_enabled(sid)

    new_series['_scaffold'] = scaffold_info
    new_series['_clone'] = {
        'source_sid': source_sid,
        'source_total_episodes': src_total,
        'copied_episodes': len(copied_numbers),
        'scope': revision_plan['scope'],
        'pending_rewrites': len(revision_plan['pending_episodes']),
        'reason': revision_plan['reason'],
        'renames': revision_plan['renames'],
    }
    return jsonify(new_series), 201


@app.route('/api/series/<sid>/episodes/<int:num>/apply-revisions', methods=['POST'])
def apply_revisions_to_episode(sid, num):
    """Rewrite ONE already-copied episode script so it obeys the series'
    revision_instructions (used after clone-from). Revises the EXISTING script in
    place — same beats / structure / hook / cliffhanger / length — changing only
    what the revisions (and world-consistency) require. Pops the episode off
    revision_plan.pending_episodes on success."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'episode not found'}), 404
    ri = (s.get('revision_instructions') or '').strip()
    if not ri:
        return jsonify({'error': 'у этого сериала нет правок — перезапись не требуется'}), 400
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'сценарий пустой — нечего переписывать'}), 400

    cast_block = _build_cast_block(s, ep)
    system = _build_script_system(s)
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
        + _anthro_world_block(s)
        + _revision_instructions_block(s)
        + (cast_block + '\n\n' if cast_block else '')
        + f'═══ EXISTING EPISODE {num} SCRIPT — REVISE IT IN PLACE ═══\n{script}\n'
        '═══════════════════════════════════════════════\n\n'
        'Rewrite THIS episode\'s script so it obeys the SERIES REVISION INSTRUCTIONS above. '
        'Change ONLY what the revisions require, plus whatever is needed to keep the world '
        'internally consistent (names, ages, timelines, who-met-whom-when). PRESERVE '
        'everything else: the same scene beats, the same structure, the same opening hook '
        'and the same closing cliffhanger, roughly the same length, and the existing dialogue '
        'wherever the revision does not touch it. Keep the [BLOCKING]/[BLOCKING_END] tags and '
        'scene headings intact. Output ONLY the rewritten script text — no commentary, no JSON.'
    )
    try:
        body = request.get_json(silent=True) or {}
        new_script = llm_ask(_resolve_writer_model(body, s), prompt, system=system)
    except Exception as e:
        return jsonify({'error': f'Не удалось переписать сценарий: {e}'}), 502
    new_script = _normalize_blocking_tags((new_script or '').strip())
    if not new_script:
        return jsonify({'error': 'модель вернула пустой сценарий'}), 502

    ep['script'] = new_script
    # Cast may have shifted — let the user re-extract characters for this episode.
    ep['cast_extracted'] = False
    save_episode(sid, num, ep)

    rp = s.get('revision_plan') or {}
    rp['pending_episodes'] = [n for n in (rp.get('pending_episodes') or []) if int(n) != int(num)]
    s['revision_plan'] = rp
    save_series(sid, s)
    return jsonify({'ok': True, 'script': new_script, 'pending_episodes': rp['pending_episodes']}), 200


@app.route('/api/series/<sid>', methods=['GET'])
def get_series(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    # ── Cache-buster ground truth: derive image_version from file mtime ──
    # Real production bug: user clicks «Перегенерировать локацию», server
    # overwrites the JPG in place under the SAME path, but the asset URL
    # `/assets/<sid>/<rel_path>` is byte-identical → browser serves the old
    # bytes from its 1-year cache. The asset serve handler explicitly relies
    # on the frontend appending `?v=<ts>` to bypass cache (see serve_asset's
    # Cache-Control header). We compute that `?v` from the FILE'S actual
    # mtime so any disk-level change forces a fresh fetch — works on every
    # ref kind regardless of whether the regenerate endpoint wrote a
    # persisted version field. Cheap: one stat per ref image per GET.
    def _stat_mtime(p):
        try:
            return int(p.stat().st_mtime)
        except Exception:
            return 0
    sp = series_path(sid)
    for kind_key in ('locations', 'characters', 'items'):
        for ent in (s.get(kind_key) or []):
            refs = ent.get('ref_images') or []
            if not refs:
                continue
            primary = sp / refs[0]
            mtime = _stat_mtime(primary)
            if mtime:
                ent['image_version'] = max(int(ent.get('image_version') or 0), mtime)
    # Auto-heal: default auto_generate_assets to True for legacy series, sync stale episode refs.
    healed = False
    if 'auto_generate_assets' not in s:
        s['auto_generate_assets'] = True
        healed = True
    if 'checkpoints' not in s or not isinstance(s.get('checkpoints'), list):
        s['checkpoints'] = []
        healed = True
    if 'finale' not in s:
        s['finale'] = None
        healed = True
    # Heal mis-gendered characters — STRICT version. Only flips when:
    #   1) appearance text embeds wrong sex tag ("young man" vs "young woman"),
    #   2) inference comes from speaker-attribution lines ("she said", "he replied")
    #      DIRECTLY tied to this character — NOT bare pronouns in surrounding text
    #      (which leak from other characters in the same scene),
    #   3) signal is unambiguous (≥4 attributed hits, ratio >3:1 against the embed).
    # Earlier loose version flipped MARCUS to female because pronouns near his
    # name were mostly Lydia's ("she lifted her hand to Marcus's chest").
    try:
        all_scripts = '\n'.join((ep.get('script') or '') for ep in list_episodes(sid))
        for c in (s.get('characters') or []):
            app_lower = (c.get('appearance') or '').lower()
            embeds_male   = 'young man'   in app_lower
            embeds_female = 'young woman' in app_lower
            if not (embeds_male or embeds_female):
                continue
            name = (c.get('name') or '').strip()
            if not name:
                continue
            # Speaker-attributed pronouns ONLY: '<Name>, she said', '<Name>, he replied'.
            # This is far more reliable than "pronouns near the name in any context".
            attrib_re = re.compile(
                r'\b' + re.escape(name) + r'\b[^\.\n]{0,40}\b(he|she|он|она)\b',
                re.IGNORECASE,
            )
            he_attrib = 0
            she_attrib = 0
            for m in attrib_re.finditer(all_scripts):
                tok = m.group(1).lower()
                if tok in ('he', 'он'):
                    he_attrib += 1
                else:
                    she_attrib += 1
            confident_female = she_attrib >= 4 and she_attrib > he_attrib * 3
            confident_male   = he_attrib  >= 4 and he_attrib  > she_attrib * 3
            should_flip = (confident_female and embeds_male) or (confident_male and embeds_female)
            if not should_flip:
                continue
            new_gender = 'female' if confident_female else 'male'
            old_gender = c.get('gender')
            c['gender'] = new_gender
            if new_gender == 'female':
                c['appearance'] = re.sub(r'young man\b', 'young woman', c.get('appearance') or '', flags=re.IGNORECASE)
            else:
                c['appearance'] = re.sub(r'young woman\b', 'young man', c.get('appearance') or '', flags=re.IGNORECASE)
            c['ref_images'] = []
            c.pop('avai_base_url', None)
            for o in (c.get('outfits') or []):
                o['photo'] = ''
                o.pop('avai_url', None)
            print(f'[heal {sid}] flipped {name}: gender {old_gender}→{new_gender} '
                  f'(she-attrib={she_attrib}, he-attrib={he_attrib}), portraits cleared', flush=True)
            healed = True
    except Exception as e:
        print(f'[heal {sid}] gender-heal failed: {e}', flush=True)
    if healed:
        save_series(sid, s)
        # If we cleared portraits (gender heal flipped a character), trigger
        # autogen so the user doesn't have to remember to click "Сгенерить".
        try:
            trigger_autogen_if_enabled(sid)
        except Exception as e:
            print(f'[heal {sid}] autogen trigger after heal failed: {e}', flush=True)
    # Heal episode refs against current series state (idempotent, cheap).
    # Catches the failure mode where chars/outfits in scripts aren't reflected in series.json.
    # Skip episodes where the user hasn't yet clicked "Извлечь персонажей и локации"
    # — auto-creating chars/outfits/locations behind their back is what we're trying to avoid.
    try:
        for ep in list_episodes(sid):
            if not (ep.get('script') or '').strip():
                continue
            # Default True for legacy episodes (they were already extracted before this gate).
            if ep.get('cast_extracted', True) is False:
                continue
            sync_episode_with_cast_block(sid, ep['number'])
    except Exception as e:
        print(f'[get_series {sid}] heal failed: {e}')
    # Re-load post-heal so client gets the fresh state
    s = load_series(sid)
    # No self-heal autogen kick here. Triggering generation as a side-effect of
    # opening a series page surprised users (work started without a click) and
    # racing modals (script-accept flow couldn't show «Не генерить» options
    # because the sweep was already running). User explicitly drives autogen
    # via the «🎨 Сгенерировать недостающее» button when they want it.
    #
    # Era-detection telemetry for UI — non-persisted, recomputed each GET.
    # If detection finds a non-modern era AND the user hasn't confirmed/picked
    # yet, the client shows a confirmation banner before any non-modern style
    # is applied to characters/outfits.
    try:
        _era_det, _era_kw = _detect_series_era(s)
        s['_era_detected'] = _era_det or ''
        s['_era_detected_keyword'] = _era_kw or ''
        s['_era_detected_label'] = _ERA_LABELS.get(_era_det or '', '')
        s['_era_choice'] = (s.get('era_choice') or 'auto')
        s['_era_confirmed'] = bool(s.get('era_confirmed'))
        s['_era_options'] = [{'key': k, 'label': v} for k, v in _ERA_LABELS.items()]
    except Exception as e:
        print(f'[get_series {sid}] era-telemetry failed: {e}', flush=True)
    # Anthro-detection telemetry — same gate pattern as era. If the detector
    # would flag this as an anthropomorphic-animal world (furry universe)
    # and the user hasn't confirmed, the UI shows a banner asking accept /
    # «это человеческий мир» before any species features are propagated to
    # secondary characters.
    try:
        _anthro_raw = _detect_anthro_world_raw(s)
        # Per-character pending: ANY character that trips the raw non-human
        # detector while the series is still undecided also surfaces the
        # banner (catches single-char misfires like «doe eyes» / «wolf
        # spirit» that the world-level detector alone would miss).
        _nd_pre, _fl_pre = _anthro_preflight(s, s.get('characters') or [])
        s['_anthro_detected'] = bool(_anthro_raw['anthro']) or bool(_nd_pre)
        s['_anthro_flagged_chars'] = _fl_pre
        _ev = list(_anthro_raw['evidence'] or [])
        if _nd_pre and _fl_pre:
            _ev.append('возможные не-люди: ' + ', '.join(_fl_pre))
        s['_anthro_evidence'] = _ev
        s['_anthro_choice'] = (s.get('anthro_choice') or 'auto')
        s['_anthro_confirmed'] = bool(s.get('anthro_confirmed'))
    except Exception as e:
        print(f'[get_series {sid}] anthro-telemetry failed: {e}', flush=True)
    return jsonify(s)

@app.route('/api/series/<sid>', methods=['PUT'])
def update_series(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    # Deep merge settings and style
    if 'settings' in data:
        s['settings'].update(data.pop('settings'))
    if 'style' in data:
        s['style'].update(data.pop('style'))
        # Sync visual_style with style preset choice — this is what gen prompts
        # actually read. Without this sync, picking "Кинематограф" in the modal
        # changed style.type but generation still used the default look.
        st = s['style']
        t = (st.get('type') or '').strip()
        if t == 'custom':
            cd = (st.get('custom_description') or '').strip()
            if cd:
                s['visual_style'] = cd
        elif t in _VISUAL_STYLE_PRESETS:
            s['visual_style'] = _VISUAL_STYLE_PRESETS[t]['desc']  # may be '' for 'auto'
    s.update(data)
    save_series(sid, s)
    return jsonify(s)


@app.route('/api/series/<sid>/era', methods=['POST'])
def set_series_era(sid):
    """Set the user's choice for the historical/genre era used during asset
    generation. Body: {choice: 'modern'|'auto'|<era_key>, clear_portraits?: bool}.

    Behavior:
      • Stores `era_choice` + `era_confirmed=True` on the series so that
        `_series_era_hint` returns the correct guide (or '' for modern).
      • When `clear_portraits` is true (default false), wipes existing
        character/outfit images so the user can regenerate them with the new
        era applied — useful when the prior auto-detect picked the wrong era
        and the user wants to retake the photos."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    choice = (body.get('choice') or '').strip().lower()
    # Validate: only allow 'auto', 'modern', 'none', or a known era key
    valid = {'auto', 'modern', 'none'} | set(_ERA_GUIDES.keys())
    if choice not in valid:
        return jsonify({'error': f'invalid choice; must be one of {sorted(valid)}'}), 400
    s['era_choice'] = choice
    # 'auto' explicitly means "let the detector pick" — only counts as
    # confirmed when the user actually picks a specific value (otherwise the
    # UI banner would never go away).
    s['era_confirmed'] = (choice != 'auto')
    cleared = []
    if body.get('clear_portraits'):
        sp = series_path(sid)
        for c in (s.get('characters') or []):
            for rel in (c.get('ref_images') or []):
                try:
                    (sp / rel).unlink(missing_ok=True)
                except Exception:
                    pass
            c['ref_images'] = []
            c.pop('avai_base_url', None)
            c['image_version'] = int(time.time())
            for o in (c.get('outfits') or []):
                if o.get('photo'):
                    try:
                        (sp / o['photo']).unlink(missing_ok=True)
                    except Exception:
                        pass
                o['photo'] = ''
                o.pop('avai_url', None)
            cleared.append(c.get('name') or c.get('id'))
    save_series(sid, s)
    return jsonify({
        'ok': True,
        'era_choice': s['era_choice'],
        'era_confirmed': s['era_confirmed'],
        'cleared_portraits': cleared,
    })


@app.route('/api/series/<sid>/anthro', methods=['POST'])
def set_series_anthro(sid):
    """Set the user's choice for anthropomorphic-animal world.
    Body: {choice: 'human'|'anthro'|'auto', strip_species?: bool, clear_portraits?: bool}.

    When `strip_species` is true (default true for choice='human'), removes
    «anthropomorphic <species>», animal-anatomy markers and species-bearing
    name tokens from character.appearance — fixes the case where a cast
    extractor incorrectly tagged human characters as furries.

    When `clear_portraits` is true, also wipes character ref images so the
    next autogen produces fresh portraits under the corrected world."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    choice = (body.get('choice') or '').strip().lower()
    if choice not in ('auto', 'human', 'anthro', 'none'):
        return jsonify({'error': 'choice must be one of: auto, human, anthro'}), 400
    if choice == 'none':
        choice = 'human'
    s['anthro_choice'] = choice
    s['anthro_confirmed'] = (choice != 'auto')
    stripped = []
    cleared_portraits = []
    if choice == 'human' and body.get('strip_species', True):
        sp = series_path(sid)
        # Pass 1: kill long phrases like "anthropomorphic deer female".
        anthro_phrase_re = re.compile(
            r'\b(?:anthropomorphic|anthro|furry)\s+\w+(?:\s+(?:female|male))?\b',
            re.IGNORECASE)
        # Pass 2: kill bare anatomy markers (the ones _detect_animal_species
        # uses as a fallback signal, plus compound forms).
        anatomy_words = (
            'fur', 'fur-tied', 'thick-furred', 'furred',
            'muzzle', 'snout', 'whiskers',
            'paws', 'claws', 'fang', 'fangs',
            'antler', 'antlers', 'mane', 'tail', 'tails',
            'feathers', 'beak', 'scales', 'tusks',
        )
        anatomy_re = re.compile(
            r'\b(?:' + '|'.join(re.escape(w) for w in anatomy_words) + r')\b',
            re.IGNORECASE)
        # Pass 3: kill bare species words ("deer", "wolf", etc.).
        species_word_re = re.compile(
            r'\b(?:' + '|'.join(re.escape(sp_kw) for sp_kw in _ANIMAL_SPECIES.keys()) + r')\b',
            re.IGNORECASE)
        # Pass 4: sweep up the connective filler left behind ("with soft brown ,
        # delicate ,") so the cleaned string reads cleanly.
        sweep_filler_re = re.compile(
            r'\b(?:with|has)\s+(?:soft|sharp|short|long|tied|visible|delicate|gentle|thick)?\s*(?=[,\s])',
            re.IGNORECASE)
        for c in (s.get('characters') or []):
            orig = c.get('appearance') or ''
            new = anthro_phrase_re.sub('', orig)
            new = anatomy_re.sub('', new)
            new = species_word_re.sub('', new)
            new = sweep_filler_re.sub('', new)
            new = re.sub(r'\s*,\s*,+', ',', new)
            new = re.sub(r'\s{2,}', ' ', new)
            new = re.sub(r'^[\s,;:.]+|[\s,;:]+$', '', new)
            if not new:
                gender = (c.get('gender') or '').strip()
                new = ('Young woman' if gender == 'female' else 'Young man') + ', neutral appearance'
            if new != orig:
                c['appearance'] = new
                stripped.append(c.get('name') or c.get('id'))
            if body.get('clear_portraits'):
                for rel in (c.get('ref_images') or []):
                    try: (sp / rel).unlink(missing_ok=True)
                    except Exception: pass
                c['ref_images'] = []
                c.pop('avai_base_url', None)
                c['image_version'] = int(time.time())
                for o in (c.get('outfits') or []):
                    if o.get('photo'):
                        try: (sp / o['photo']).unlink(missing_ok=True)
                        except Exception: pass
                    o['photo'] = ''
                    o.pop('avai_url', None)
                cleared_portraits.append(c.get('name') or c.get('id'))
    save_series(sid, s)
    return jsonify({
        'ok': True,
        'anthro_choice': s['anthro_choice'],
        'anthro_confirmed': s['anthro_confirmed'],
        'stripped_appearances': stripped,
        'cleared_portraits': cleared_portraits,
    })


@app.route('/api/style-presets')
def style_presets():
    """Return the catalog of built-in visual styles for the picker UI."""
    return jsonify({
        'presets': [
            {'id': k, **v} for k, v in _VISUAL_STYLE_PRESETS.items()
        ]
    })


@app.route('/api/style-sample', methods=['POST'])
def style_sample():
    """Generate ONE sample image for a custom style description. UI shows it
    in the style-picker so the user can preview their custom desc before
    committing the whole series to that look. Cached by description hash so
    re-clicking on the same desc reuses the prior generation.

    Body: {description: str, base?: 'snoop'|'man'|'woman'} — base picks the
    canonical subject for the sample. Default = a generic young man portrait
    so the user sees how chars in their series will look."""
    body = request.get_json(silent=True) or {}
    desc = (body.get('description') or '').strip()
    if not desc:
        return jsonify({'error': 'description required'}), 400
    base = (body.get('base') or 'man').lower()
    base_subject = {
        'snoop':  'a Black male rapper in his 50s with long braids, gold chains, sunglasses, smoking pose',
        'man':    'a young man in his late 20s, neutral expression, photogenic features, casual shirt',
        'woman':  'a young woman in her late 20s, neutral expression, photogenic features, casual blouse',
    }.get(base, base)
    # Cache key — sha256(desc + base) so re-running the same prompt is free.
    import hashlib
    key = hashlib.sha256((desc + '|' + base).encode('utf-8')).hexdigest()[:16]
    # Persistent location — survives redeploys (DATA_ROOT is mounted volume).
    # `static/img/...` gets wiped on every `git pull` / image rebuild. New URL
    # is /style-samples-cache/<key>.jpg, served by serve_style_sample below.
    cache_dir = DATA_ROOT / '_global' / 'style-samples-cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f'{key}.jpg'
    if cache_path.exists():
        return jsonify({'url': f'/style-samples-cache/{key}.jpg', 'cached': True})
    # Build prompt with the requested style
    prompt = (
        f"{base_subject}. {desc}. Centered portrait composition, neutral background. "
        f"Square 1:1 framing."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    try:
        avai_url = _avai_call('banana', prompt, aspect_ratio='1:1')
        # Download and save to cache
        import requests
        r = requests.get(avai_url, timeout=60)
        r.raise_for_status()
        cache_path.write_bytes(r.content)
        return jsonify({'url': f'/style-samples-cache/{key}.jpg', 'cached': False})
    except Exception as e:
        _log_event('WARN', 'style_sample_fail', desc=desc[:120], err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/regenerate-style-samples', methods=['POST'])
def regenerate_style_samples():
    """Primary-only one-shot: generates the baseline preset samples (cinematic,
    photorealistic, anime, pixar, noir) using a canonical subject so the style
    picker shows real previews. Saves to static/img/style-samples/<id>.jpg.
    Run once per deploy when AVAI prompts change. ~5 LLM calls × ~10s each."""
    actor = current_user_email() or ''
    if actor != PRIMARY_USER_EMAIL and AUTH_ENABLED:
        return jsonify({'error': 'admin only'}), 403
    # Persistent location — see notes on /api/style-sample above.
    out_dir = DATA_ROOT / '_global' / 'style-samples'
    out_dir.mkdir(parents=True, exist_ok=True)
    base_subject = 'a Black male rapper in his 50s with long braids, gold chains, sunglasses'
    results = []
    for preset_id, preset in _VISUAL_STYLE_PRESETS.items():
        if not preset.get('desc'):
            continue   # skip 'auto' — no fixed style
        prompt = (
            f"{base_subject}. {preset['desc']}. Centered portrait composition, "
            f"neutral background. Square 1:1 framing."
        )
        prompt = re.sub(r'\s+', ' ', prompt).strip()
        try:
            avai_url = _avai_call('banana', prompt, aspect_ratio='1:1')
            import requests
            r = requests.get(avai_url, timeout=60)
            r.raise_for_status()
            out_path = out_dir / f'{preset_id}.jpg'
            out_path.write_bytes(r.content)
            results.append({'id': preset_id, 'ok': True, 'path': str(out_path)})
        except Exception as e:
            results.append({'id': preset_id, 'ok': False, 'err': str(e)[:200]})
    return jsonify({'results': results})


@app.route('/style-samples/<path:filename>')
def serve_style_sample(filename):
    """Serve baseline preset samples from the persistent DATA_ROOT location.
    Falls back to the old static/img/style-samples/<filename> if a sample
    hasn't been migrated yet — keeps existing series working during the
    transition. The first time admin clicks 'Перегенерировать стили' all five
    baseline samples land in DATA_ROOT/_global/style-samples/ and stay there
    across deploys."""
    from flask import send_from_directory, abort
    persistent = DATA_ROOT / '_global' / 'style-samples'
    target = persistent / filename
    if target.exists():
        return send_from_directory(persistent, filename)
    legacy = BASE / 'static' / 'img' / 'style-samples'
    if (legacy / filename).exists():
        return send_from_directory(legacy, filename)
    abort(404)


@app.route('/style-samples-cache/<path:filename>')
def serve_style_sample_cache(filename):
    """Serve user-generated custom-style samples from the persistent cache."""
    from flask import send_from_directory, abort
    cache = DATA_ROOT / '_global' / 'style-samples-cache'
    if (cache / filename).exists():
        return send_from_directory(cache, filename)
    legacy = BASE / 'static' / 'img' / 'style-samples-cache'
    if (legacy / filename).exists():
        return send_from_directory(legacy, filename)
    abort(404)

def _rmtree_hard(path):
    """Permanently wipe a directory tree. Robust against AppleDouble (`._*`)
    metadata races on macOS external (exFAT/HFS+) drives where Python's
    shutil.rmtree silently leaves stragglers. Falls back to /bin/rm -rf which
    handles those cases atomically.

    Returns (ok: bool, msg: str).
    """
    p = Path(path)
    if not p.exists():
        return True, ''
    # First try shutil — fast path on clean drives.
    try:
        shutil.rmtree(p)
    except Exception:
        pass
    if not p.exists():
        return True, ''
    # Fallback: shell out to /bin/rm -rf. Handles AppleDouble + locked metadata.
    try:
        result = subprocess.run(
            ['/bin/rm', '-rf', '--', str(p)],
            capture_output=True, text=True, timeout=60
        )
        if p.exists():
            return False, (result.stderr or 'rm -rf не смог удалить папку').strip()
        return True, ''
    except Exception as e:
        return False, str(e)


@app.route('/api/series/<sid>', methods=['DELETE'])
def delete_series(sid):
    p = series_path(sid)
    ok, msg = _rmtree_hard(p)
    if not ok:
        return jsonify({'error': f'Не удалось удалить папку с диска: {msg}'}), 500
    return jsonify({'ok': True})


from sw.routes.entities import (
    add_character,
    fix_anthro_species,
    update_character,
    delete_character,
    add_location,
    update_location,
    delete_location,
    upload_location_asset,
    delete_location_asset,
    add_item,
    update_item,
    delete_item,
    upload_item_asset,
    delete_item_asset,
    get_char,
    get_outfit,
    add_outfit,
    update_outfit,
    delete_outfit,
    link_outfit_to_base,
    generate_outfit_image,
    save_outfit_frame,
    generate_character_image,
    regenerate_character,
    save_character_frame,
)
    # Download first frame
from sw.routes.images_loc import (
    upload_char_photo,
    upload_loc_photo,
    open_folder,
    generate_location_image,
    regenerate_location,
)
from sw.routes.images_cover import (
    _cover_lead_refs,
    _cover_art_direction_source,
    _COVER_AD_KEYS,
    _series_cover_art_direction,
    _build_cover_prompt,
    _translate_synopsis_to_en,
    get_series_synopsis_en,
    generate_series_cover,
)
from sw.routes.facades import (
    _FACADE_STATUS,
    _facade_status,
    _claude_group_locations,
    facades_group,
    _facade_worker,
    _merge_facade,
    facades_generate,
    auto_facades_for_new_locations,
    facades_list,
    facades_delete,
    facades_regenerate,
    facades_generate_for_location,
    facades_open_folder,
    save_location_frame,
)
from sw.routes.images_items import (
    upload_item_photo,
    generate_item_image,
    regenerate_item,
    _TRANSLIT_DEDUP,
    _norm_for_dedup,
    _llm_dedupe_against_existing,
    _fuzzy_find_item,
    dedupe_series_items,
    detect_items_in_episode,
)
from sw.autogen import (
    _AUTOGEN_LOCKS,
    _AUTOGEN_STATUS,
    _autogen_status,
    _gen_char_base_inline,
    _gen_outfit_inline,
    _gen_loc_inline,
    _gen_item_inline,
    _AUTOGEN_SAVE_LOCKS,
    _AUTOGEN_PARALLELISM,
    auto_generate_missing_assets,
    trigger_autogen_if_enabled,
    toggle_auto_generate,
    autogen_status_endpoint,
    trigger_autogen_sweep,
    reanalyze_outfits,
)

from sw.routes.canon import (
    get_canon,
    rebuild_canon,
    generate_asset_prompt,
)


from sw.routes.ideas import (
    _IDEAS_HISTORY_MAX,
    _ideas_history_path,
    _load_ideas_history,
    _ideas_signature,
    _save_ideas_history,
    _ideas_avoid_recent_block,
    _ideas_research_digest,
    _run_ideas_pipeline_v2,
    _TOP_DRAMAS_SCHEMA,
    _research_top_dramas,
    research_top_dramas,
    _IDEAS_SIMILAR_SYSTEM,
    _ideas_from_drama,
    ideas_from_drama,
    _DRAMA_ANALYSIS_SCHEMA,
    _top_dramas_path,
    _load_top_dramas,
    _save_top_dramas,
    _drama_slug,
    _analyze_drama,
    top_dramas_get,
    top_dramas_scan,
    top_dramas_analyze,
    _resolve_beats,
    _beats_ideas_block,
    _source_outline_episode_block,
    _series_beats_episode_block,
    _modal_setting_to_era_choice,
    _era_setting_block,
    _IDEA_ANTI_MONOTONY,
    _IDEA_ROMANCE_DIRECTIVE,
    _IDEA_FAMILY_SPREAD,
    list_story_beats,
    generate_series_ideas,
    generate_series_from_idea,
)

from sw.story_logic import (
    MILESTONE_EPS,
    _WRITER_SYSTEM,
    _BRIEF_SYSTEM,
    _AUDIT_SYSTEM,
    _LOGIC_HOLE_AUDIT_SYSTEM,
    audit_logic_holes,
    _SCRIPT_DOCTOR_SYSTEM,
    doctor_script,
    _EXTRACT_SYSTEM,
    _build_crowd_constraint_block,
    _count_speaking_characters_per_scene,
    detect_scene_overcrowding,
    _GENERIC_ROLE_WORDS,
    _NAME_TITLES,
    _NAME_FILLER,
    _label_is_unnamed,
    _extract_cast_block_names,
    _extract_speaker_and_blocking_labels,
    detect_unnamed_characters,
    _script_runtime_metrics,
    detect_script_overlength,
    _build_narrative_state_block,
    build_logic_brief,
    audit_script,
    extract_canon_updates,
    rollback_canon_for_episode,
)
from sw.story_writer import (
    _SCRIPT_SYSTEM,
    _build_script_system,
    _BATCH_SCRIPT_SYSTEM,
    _build_batch_script_system,
)
from sw.routes.story_gen import (
    generate_arcs,
    confirm_arc,
    generate_milestones,
    update_milestone,
    regenerate_milestone,
    extract_from_story,
    list_script_history,
    get_script_history_full,
    restore_script_version,
    doctor_episode_script,
    extract_characters_from_script,
    confirm_milestones,
    generate_next_episode_synopsis,
    generate_episode_synopses,
)
from sw.routes.scripts import (
    _LANDMARK_PROGRESS,
    _LANDMARK_LOCK,
    _landmark_progress_set,
    _landmark_progress_get,
    _run_generate_to_landmark_bg,
    generate_to_landmark_status,
    generate_to_landmark,
    generate_episode_script,
    sync_episode_with_cast_block,
    _infer_gender_from_script,
    _parse_cast_block,
)

from sw.routes.reteller_prompts import (
    _RTL_PROMPT_SYSTEM,
    episode_reteller_prompt,
    range_reteller_prompt,
)


from sw.routes.episodes import (
    get_episodes,
    _CREATE_EP_LOCKS,
    _CREATE_EP_IDEMPOTENCY,
    _CREATE_EP_IDEMPOTENCY_TTL,
    create_episode,
    get_episode,
    update_episode,
    update_segment_auto_skips,
    update_line_overrides,
    update_segment_overrides,
    clear_episode,
    delete_episode,
)

from sw.routes.assets import (
    upload_character_asset,
    delete_character_asset,
    upload_style_asset,
    delete_style_asset,
    serve_asset,
    set_skip_autogen,
    relink_assets,
    debug_asset,
)


from sw.routes.reteller import (
    reteller_preview,
    reteller_submit,
    reteller_status,
    reteller_balance,
    reteller_voices,
    reteller_styles,
)
# ════════════════════════════════════════════════════════════════════════════
from sw.seedance import (
    _seedance_chunks,
    _next_chunk_idx,
    _extract_last_frame,
    _detect_cuts,
    _extract_keyframes_at_cuts,
    _purge_continuity_sidecars,
    _AVAI_AUDIT_LOG,
    _AVAI_KILL_SWITCH,
    _avai_rate_lock,
    _avai_recent_submits,
    _AVAI_MAX_PER_FP_10MIN,
    _AVAI_MAX_PER_MINUTE,
    _AVAI_KILLSWITCH_5MIN,
    AVAICircuitBreakerError,
    _SEEDANCE_MODERATION_CHECKER_SYS,
    SEEDANCE_PRECHECK_ENABLED,
    _seedance_moderation_precheck,
    _avai_kill_switch_status,
    _avai_fingerprint,
    _avai_circuit_breaker_check,
    _avai_seedance_start,
    _avai_seedance_status,
    _avai_upload_local_image,
    QC_MAX_RETRIES,
    _QC_NON_ENGLISH_RE,
    _qc_extract_audio,
    _qc_extract_frame_at,
    _qc_whisper_detect,
    _qc_vision_grid_and_subs,
    _qc_check_prompt_english,
    _qc_run_chunk,
    _qc_can_pass,
    _ensure_loc_avai_url,
    _ensure_char_avai_base_url,
    _resolve_ref_url,
    _download_video,
)

from sw.routes.seedance_assets import (
    seedance_upload_ref,
    seedance_list,
    auto_assemble_episode,
    seedance_download_zip,
    generate_scene_blocking,
)


# Reference-style batch-compose rules — adapted from
# /tmp/shadow-founder/services/chunk-builder.ts EPISODE_PLAN_RULES.
# Adapted: variable segment count (not fixed 4+1+1), English promptEn output.
from sw.routes.seedance_batch import (
    _SD_BATCH_RULES,
    _SD_REVISE_BATCH_RULES,
    seedance_revise_batch,
    seedance_batch_compose,
)


from sw.routes.seedance_compose import seedance_compose

import sw.routes.timeline  # registers timeline routes
from sw.routes.seedance_run import (
    seedance_start,
    seedance_poll,
    avai_kill_switch_get,
    avai_kill_switch_clear,
    seedance_qc,
    seedance_delete,
    _heal_chunk_via_claude,
    _HEAL_PROMPT_SYSPROMPT,
    seedance_heal_prompt,
    seedance_rewrite_chunk,
    seedance_patch_ending,
)


if __name__ == '__main__':
    # Dev mode entrypoint. In production we run under gunicorn (see Dockerfile),
    # which hits the `else` branch below.
    debug = os.environ.get('FLASK_DEBUG', '1').lower() not in ('', '0', 'false', 'no')
    port = int(os.environ.get('PORT', '8080'))
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _recover_inflight_chunks()
        _start_log_cleanup_loop()
    print(f'Series Writer запущен → http://localhost:{port}  (debug={debug})')
    app.run(debug=debug, port=port, host='0.0.0.0', threaded=True)
else:
    # Production: gunicorn imports this module. Run recovery + start log
    # cleanup loop once on boot.
    # SW_SKIP_BOOT=1 imports the module without side effects (route-map checks).
    if os.environ.get('SW_SKIP_BOOT') != '1':
        _recover_inflight_chunks()
        _start_log_cleanup_loop()
