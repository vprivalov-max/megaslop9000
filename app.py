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
from sw.routes.import_series import (
    _EPISODE_BOUNDARY_PATTERNS,
    _split_script_into_episodes,
    _import_status,
    _llm_extract_episode_entities,
    _import_worker,
    episodes_logic_check_multi,
    episodes_logic_apply_multi,
    import_from_script_logic_check,
    import_from_script_apply_fixes,
    import_from_script_preview,
    _TRANSLATE_DIALOGUES_SYSTEM,
    import_from_script_translate_dialogues,
    _ADAPT_TO_STANDARD_SYSTEM,
    adapt_script_to_standard,
    check_moderation,
    import_from_script,
    import_status,
    generate_script_batch,
    append_from_script,
    backfill_devices,
    reextract_series,
    create_series,
    clone_series_from,
    apply_revisions_to_episode,
    get_series,
    update_series,
    set_series_era,
    set_series_anthro,
    style_presets,
    style_sample,
    regenerate_style_samples,
    serve_style_sample,
    serve_style_sample_cache,
    _rmtree_hard,
    delete_series,
)

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
