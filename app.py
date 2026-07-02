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
def _describe_character_visual(image_url: str, char_name: str = '') -> str:
    """Vision-extract a FULL character appearance description from a user-
    uploaded portrait. Used by import-from-script to backfill an empty
    `appearance` field on chars the user pre-uploaded (only ref_images
    saved, no text → BINDING line was just the name → Seedance lost the
    text-side anchor for outfit/style continuity).

    Returns a Russian descriptive sentence covering: возраст, телосложение,
    волосы, лицо, одежда. Suitable as drop-in for `char.appearance` which
    feeds `_canonical_char_description` → Seedance BINDING.
    """
    if not image_url or not image_url.lower().startswith(('http://', 'https://')):
        return ''
    name_hint = f' Персонаж в сериале называется "{char_name}".' if char_name else ''
    prompt = (
        "Опиши внешность человека на фото для AI-генерации последующих "
        "изображений того же персонажа в разных сценах." + name_hint + "\n\n"
        "Ровно 1-2 предложения на русском, comma-separated descriptors, "
        "максимум 250 символов. Покрой:\n"
        "  • возраст (примерный диапазон, e.g. «около 30 лет», «лет 50»)\n"
        "  • пол / телосложение (e.g. «худощавый мужчина», «стройная женщина»)\n"
        "  • волосы (цвет, длина, причёска)\n"
        "  • лицо (один-два запоминающихся черта: «волевая челюсть», «миндалевидные глаза»)\n"
        "  • одежда (top + bottom + ключевые аксессуары)\n\n"
        "Примеры выходов:\n"
        "  «Молодая женщина около 28 лет, стройная, длинные тёмно-каштановые волосы убраны в "
        "    хвост, миндалевидные карие глаза, тёмно-синие медицинские scrubs, бейдж на груди»\n"
        "  «Мужчина средних лет, около 45, плотного телосложения, короткие седеющие волосы, "
        "    тёплые серые глаза, угольно-серый трёхпредметный костюм, белая рубашка»\n"
        "  «Подросток лет 16, худощавый, коротко стриженые чёрные волосы, бледная кожа, "
        "    потёртая джинсовая куртка, серая толстовка»\n\n"
        "ВАЖНО: НЕ описывай фон, освещение, позу, выражение эмоций, мимику. "
        "ТОЛЬКО физический look + одежда. Без кавычек, без префикса «Это:», "
        "без «На фото мы видим». Только описание."
    )
    try:
        text = claude_ask_vision(prompt, [image_url]).strip()
        text = text.replace('\n', ' ').strip().strip('"').strip('«»').strip("'").rstrip('.')
        # Hard-cap so we don't pollute BINDING with a wall of text.
        if len(text) > 280:
            text = text[:277] + '...'
        # A user-uploaded portrait may be risqué; keep the persisted description
        # moderation-safe so it never trips the filter on downstream prompts.
        return _sanitize_appearance_for_moderation(text)
    except Exception as e:
        print(f'[char_vision] failed for {char_name or image_url}: {e}', flush=True)
        return ''


def _backfill_uploaded_char_appearances(sid, char_ids):
    """Background worker for import-from-script: for each char id, lazy-upload
    the first ref image to AVAI storage, call Vision to extract a Russian
    `appearance` description, and persist back to series. Safe to run in
    parallel with `_import_worker` — each takes a fresh `load_series` and
    only mutates fields the worker doesn't touch.

    Uses a ThreadPoolExecutor with 3 lanes so 5+ uploads finish in ~10-15s
    instead of 30-60s sequential. Each char's Vision call is independent."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(cid):
        try:
            s = load_series(sid)
            if not s:
                return
            char = next((c for c in (s.get('characters') or []) if c['id'] == cid), None)
            if not char:
                return
            if (char.get('appearance') or '').strip():
                return   # already populated — don't overwrite user-edited or worker-set text
            refs = char.get('ref_images') or []
            if not refs:
                return
            primary_rel = refs[0]
            primary_abs = series_path(sid) / primary_rel
            if not primary_abs.exists():
                return
            # AVAI public URL (Claude Vision needs HTTPS, can't read local files).
            try:
                avai_url = char.get('avai_base_url') or _avai_upload_local_image(primary_abs)
            except Exception as e:
                print(f'[backfill_char] AVAI upload failed for {char.get("name")}: {e}', flush=True)
                return
            description = _describe_character_visual(avai_url, char.get('name', ''))
            if not description:
                return
            # Persist atomically — re-load to avoid clobbering parallel updates.
            with _series_lock(sid):
                s2 = load_series(sid)
                if not s2:
                    return
                ch2 = next((c for c in (s2.get('characters') or []) if c['id'] == cid), None)
                if not ch2:
                    return
                # Don't overwrite if some other code path filled it meanwhile.
                if not (ch2.get('appearance') or '').strip():
                    ch2['appearance'] = description
                    if avai_url and not ch2.get('avai_base_url'):
                        ch2['avai_base_url'] = avai_url
                    save_series(sid, s2)
                    print(f'[backfill_char] filled appearance for {ch2.get("name")}: {description[:80]}...', flush=True)
        except Exception as e:
            print(f'[backfill_char] crashed for cid={cid}: {type(e).__name__}: {e}', flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(_one, char_ids))


def _describe_outfit_visual(image_url: str) -> str:
    """Vision-extract a tight clothing description from a character/outfit photo.
    Returns short string like 'navy medical scrubs, ID badge on chest, hair in
    messy bun'. Used as a TEXT REINFORCEMENT alongside the @Image-ref so Seedance
    gets aligned signals (visual + textual say the same thing) — fixes outfit
    drift that the visual ref alone doesn't prevent."""
    if not image_url or not image_url.lower().startswith(('http://', 'https://')):
        return ''
    prompt = (
        "Describe ONLY what this person is wearing and their immediate look "
        "(hair styling, makeup if striking) in 8-15 words. English. Comma-separated.\n\n"
        "Format: '<garment top>, <garment bottom or accessories>, <hair>'\n"
        "Examples:\n"
        "  navy blue medical scrubs, hospital ID badge on chest, hair in messy bun\n"
        "  charcoal three-piece suit, white shirt, slicked-back hair\n"
        "  cream silk blouse, dark fitted skirt, hair in loose waves\n"
        "  denim jacket, white tee, dark blue jeans, short tousled hair\n\n"
        "DO NOT describe face features, age, gender, expression, body, "
        "background, or pose. ONLY clothing + hair styling. Under 15 words. "
        "No quotes, no leading 'The person is wearing' — just the description."
    )
    try:
        text = claude_ask_vision(prompt, [image_url]).strip()
        text = text.replace('\n', ' ').strip().strip('"').strip("'").rstrip('.')
        return text[:200]
    except Exception as e:
        print(f'[outfit_vision] failed for {image_url}: {e}', flush=True)
        return ''


from sw.imagetags import (
    _remap_image_tags,
    _build_image_tag_remap,
)

from sw.textrules_banlists import (
    _SD_BANLIST,
    _seedance_apply_banlist,
    _SD_FORBIDDEN_AUTONOMY_RE,
    _seedance_validate_autonomy,
    _seedance_validate_durations,
    _SD_WARDROBE_WORDS,
    _seedance_validate_wardrobe,
    _seedance_filter_blocking_for_chunk,
    _extract_script_blocking,
    _seedance_inject_blocking,
    _SD_HARD_CUTS_CLAUSE,
    _seedance_inject_hard_cuts,
    _prev_episode_ending_context,
    _build_episode_tag_mapping,
    _VAGUE_CLOTHING_TAIL_RE,
    _strip_vague_clothing_tail,
    _GARMENT_NOUN,
    _CLOTHING_CLAUSE_RE,
    _BARE_GARMENT_CLAUSE_RE,
    _strip_concrete_clothing,
    _HAIR_LOOK_WORDS,
    _OUTFIT_HAIR_RE,
    _HAIR_ADJ,
    _HAIR_CLAUSE_RE,
    _outfit_hair_phrase,
    _override_hair_in_appearance,
)
from sw.textrules_sanitizer import (
    _ID_HAIR_TOKENS,
    _ID_HAIR_CHANGE_RE,
    _ID_HAIR_WORD_RE,
    _ID_ALIAS_RE,
    _ID_RESTORE_RE,
    _norm_hair_token,
    _detect_identity_shift,
    _UNDRESSED_STATE_RE,
    _DRESSED_OUTFIT_RE,
    _UNDRESSED_OUTFIT_RE,
    _detect_char_undressed_states,
    _undress_state_clothing,
    _binding_desc_with_undress,
    _APPEARANCE_ADJ_MAP_RU,
    _APPEARANCE_ADJ_MAP_EN,
    _APPEARANCE_NUDE_RE,
    _APPEARANCE_NUDE_RU_RE,
    _APPEARANCE_SOFTEN,
    _APPEARANCE_EN_ADJ_RE,
    _sanitize_appearance_for_moderation,
    _canonical_char_description,
)
from sw.jsonutils import (
    strip_json,
    _repair_llm_json,
    _strip_markdown_fence,
    loads_lenient,
)
from sw.avai import (
    save_config,
    rtl_headers,
    _avai_call,
    _series_image_provider,
    avai_generate,
    allowed_file,
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


# ── Series ───────────────────────────────────────────────────────────────────

@app.route('/api/series', methods=['GET'])
def list_series():
    # ?archived=1 → only archived projects. Default → only non-archived.
    want_archived = request.args.get('archived') in ('1', 'true', 'yes')
    result = []
    root = user_root()
    if not root.exists():
        return jsonify([])
    for d in sorted(root.iterdir()):
        sf = d / 'series.json'
        if sf.exists():
            s = json.loads(sf.read_text())
            is_archived = bool(s.get('archived'))
            if want_archived and not is_archived:
                continue
            if not want_archived and is_archived:
                continue
            ep_dir = d / 'episodes'
            ready = 0
            total = 0
            # Track latest mtime across series.json + every episode file so the
            # main-page "sort by modification" reflects real editing activity
            # (writing/regenerating an episode doesn't touch series.json itself).
            try:
                latest_mtime = int(sf.stat().st_mtime)
            except Exception:
                latest_mtime = 0
            if ep_dir.exists():
                for p in ep_dir.glob('*.json'):
                    # Filter out macOS AppleDouble metadata (._*) and any dotfile
                    if p.name.startswith('.'):
                        continue
                    total += 1
                    try:
                        mt = int(p.stat().st_mtime)
                        if mt > latest_mtime:
                            latest_mtime = mt
                    except Exception:
                        pass
                    try:
                        ep_data = json.loads(p.read_text())
                    except Exception:
                        continue
                    if ep_data.get('ready') or ep_data.get('reteller', {}).get('project_id'):
                        ready += 1
            s['_episode_count'] = ready
            s['_episode_total'] = total
            s['_updated_at'] = latest_mtime
            result.append(s)
    if want_archived:
        # Most recently archived first
        result.sort(key=lambda s: -(s.get('archived_at') or 0))
    else:
        # Pinned projects first, then unpinned. Within each group keep alphabetic order
        # (already sorted by directory name above).
        result.sort(key=lambda s: (0 if s.get('pinned') else 1, -(s.get('pinned_at') or 0)))
    return jsonify(result)


@app.route('/api/series/<sid>/rename-out', methods=['POST'])
def rename_out_files(sid):
    """Rename every file inside <series>/OUT/ to the studio's delivery
    convention:
      • Video → <Series_Name>_E<N>(.ext) or <Series_Name>_E<a>-<b>(.ext) for ranges
      • Audio (VO / MUS / SFX) → <KIND>_<Series_Name>_<idx>.<ext>
    Idempotent — files already in canonical form are skipped.
    Files we can't classify are reported in `skipped` so the user can rename
    them manually."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    folder = out_dir(sid)
    if not folder.exists():
        return jsonify({'error': 'OUT folder not found'}), 404

    title = (s.get('title') or sid).strip()
    # series_safe: alnum/underscore only, exactly the form used in the spec ("Series_Name")
    safe = re.sub(r'[^\w]+', '_', title, flags=re.UNICODE).strip('_') or sid

    VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
    AUDIO_EXT = {'.wav', '.mp3', '.m4a', '.aac', '.ogg', '.flac'}

    files = sorted([p for p in folder.iterdir() if p.is_file() and not p.name.startswith('.')])

    def detect_episode_token(stem):
        # Range first: E1-5 / ep1-5 / 1-5
        m = re.search(r'\b[Ee]?p?(\d{1,3})\s*[-–]\s*(\d{1,3})\b', stem)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < b and b - a < 200:
                return f'E{a}-{b}'
        m = re.search(r'\b[Ee]p?(?:isode)?[\s_-]?(\d{1,4})\b', stem)
        if m: return f'E{int(m.group(1))}'
        # Bare number as last resort
        m = re.search(r'(?<!\w)(\d{1,4})(?!\w)', stem)
        if m: return f'E{int(m.group(1))}'
        return None

    AUDIO_KIND_PATTERNS = [
        ('VO',  re.compile(r'(?:^|[_\-\s])(vo|voice|vocal|voiceover|dial|dialog|dialogue|repl|replicas?|reps)(?:[_\-\s.\d]|$)', re.I)),
        ('MUS', re.compile(r'(?:^|[_\-\s])(mus|music|track|score|bgm|theme|song|ost)(?:[_\-\s.\d]|$)', re.I)),
        ('SFX', re.compile(r'(?:^|[_\-\s])(sfx|fx|sound|effect|noise|amb|ambient|foley)(?:[_\-\s.\d]|$)', re.I)),
    ]

    def detect_audio_kind(stem):
        for kind, pat in AUDIO_KIND_PATTERNS:
            if pat.search(stem):
                return kind
        return None

    plans = []           # list of (Path, new_name)
    skipped = []         # list of {name, reason}
    audio_buckets = {'VO': [], 'MUS': [], 'SFX': []}

    for p in files:
        ext = p.suffix.lower()
        if ext in VIDEO_EXT:
            tok = detect_episode_token(p.stem)
            if not tok:
                skipped.append({'name': p.name, 'reason': 'не удалось определить номер эпизода'})
                continue
            plans.append((p, f'{safe}_{tok}{ext}'))
        elif ext in AUDIO_EXT:
            kind = detect_audio_kind(p.stem)
            if not kind:
                skipped.append({'name': p.name, 'reason': 'не определилось VO/MUS/SFX'})
                continue
            audio_buckets[kind].append(p)
        else:
            skipped.append({'name': p.name, 'reason': f'неподдерживаемое расширение {ext}'})

    # Number audio within each bucket alphabetically — stable & predictable
    for kind, lst in audio_buckets.items():
        for i, p in enumerate(sorted(lst, key=lambda x: x.name.lower()), 1):
            plans.append((p, f'{kind}_{safe}_{i}{p.suffix.lower()}'))

    # Two-stage rename to avoid collisions when N files want the same target
    tmp_moves = []  # (tmp_path, new_name, original_name)
    errors = []
    for p, new_name in plans:
        if p.name == new_name:
            continue  # already canonical
        tmp = p.with_name(f'.__rnm_{uuid.uuid4().hex[:8]}_{p.name}')
        try:
            p.rename(tmp)
            tmp_moves.append((tmp, new_name, p.name))
        except Exception as e:
            errors.append({'name': p.name, 'error': f'stage1: {e}'})

    renamed = []
    for tmp, new_name, orig in tmp_moves:
        target = tmp.parent / new_name
        if target.exists():
            # someone else already occupies the target — restore and report
            errors.append({'name': orig, 'error': f'цель {new_name} уже существует'})
            try: tmp.rename(tmp.parent / orig)
            except Exception: pass
            continue
        try:
            tmp.rename(target)
            renamed.append({'from': orig, 'to': new_name})
        except Exception as e:
            errors.append({'name': orig, 'error': f'stage2: {e}'})
            try: tmp.rename(tmp.parent / orig)
            except Exception: pass

    return jsonify({
        'series_safe_name': safe,
        'total_files': len(files),
        'renamed': renamed,
        'skipped': skipped,
        'errors': errors,
    })


@app.route('/api/series/<sid>/meta', methods=['POST'])
def update_series_meta(sid):
    """Lightweight metadata patch (color label, starred flag) — used by the
    projects-grid UI without touching settings/style/episodes."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    if 'color' in body:
        c = (body.get('color') or '').strip()
        # Whitelist: empty (clear) or one of the swatch slugs
        allowed = {'', 'red', 'orange', 'yellow', 'green', 'teal', 'blue', 'purple', 'pink', 'gray'}
        if c not in allowed:
            return jsonify({'error': f'invalid color: {c}'}), 400
        s['color'] = c
    if 'starred' in body:
        s['starred'] = bool(body['starred'])
    save_series(sid, s)
    return jsonify({'color': s.get('color', ''), 'starred': bool(s.get('starred'))})


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


# ── Style helpers ────────────────────────────────────────────────────────────

_DEFAULT_VISUAL_STYLE = (
    "Photorealistic, cinematic quality, high detail. "
    "Realistic short-drama TV-series look (TikTok/Reels), natural skin textures, "
    "subtle film grain, professional cinematography lighting."
)

_VISUAL_STYLE_PRESETS = {
    'cinematic': {
        'label': 'Кинематограф',
        'desc':  'Cinematic film look — shallow depth of field, professional color grading (teal/orange or analog film), 35mm aesthetic, soft natural lighting, subtle film grain. Photorealistic skin and materials.',
        'sample': '/style-samples/cinematic.jpg',
    },
    'photorealistic': {
        'label': 'Фотореализм',
        'desc':  'Photorealistic, sharp focus, neutral color grading, even lighting. Skin pores, fabric weave, micro-detail visible. No stylization.',
        'sample': '/style-samples/photorealistic.jpg',
    },
    'anime': {
        'label': 'Аниме',
        'desc':  'Anime style, cel-shaded, clean line art, vibrant flat colors, large expressive eyes, stylized proportions, smooth gradients. Studio-quality animation frame look.',
        'sample': '/style-samples/anime.jpg',
    },
    'pixar': {
        'label': '3D Pixar',
        'desc':  'Pixar 3D animation style, soft volumetric lighting, exaggerated facial expressions, slightly stylised proportions, vibrant saturated palette, cinematic composition.',
        'sample': '/style-samples/pixar.jpg',
    },
    'noir': {
        'label': 'Film Noir',
        'desc':  'Film noir, high-contrast black-and-white, dramatic chiaroscuro lighting, venetian blind shadows, smoky atmosphere, 1940s aesthetic.',
        'sample': '/style-samples/noir.jpg',
    },
    'auto': {
        'label': 'Авто (AI выберет)',
        'desc':  '',
        'sample': '',
    },
}

def _series_visual_style(s):
    """Returns the project's visual style override or the realistic default.
    Two storage paths kept in sync:
      - s['visual_style'] : free-text description (what generation prompts read)
      - s['style']['type']: preset key OR 'custom' (what UI binds to)
    When type is set to a preset, visual_style is force-synced to the preset's
    desc string so picking 'cinematic' actually drives the gen prompts."""
    val = ((s or {}).get('visual_style') or '').strip()
    return val or _DEFAULT_VISUAL_STYLE

def _series_style_clause(s):
    """Inline-style clause for character/location image generation prompts."""
    v = _series_visual_style(s)
    if not v:
        return ""
    # Some hint phrasing depending on whether project picked a stylised look
    low = v.lower()
    stylised = any(k in low for k in (
        'pixar', 'anime', 'manga', 'cartoon', 'claymation', 'oil painting',
        'cyberpunk', 'noir', 'film noir', 'watercolor', 'graphic novel',
        'studio ghibli', 'arcane', 'comic',
    ))
    if stylised:
        return (
            f"VISUAL STYLE — strict: {v}. The whole image MUST be rendered in this style "
            f"(materials, faces, lighting, palette). Do NOT mix with photorealism."
        )
    return f"Visual style: {v}"


def _location_crowd_clause(loc):
    """Decide how a location's establishing shot should be populated.

    The original rule was a blanket "No people, no characters in frame" — meant
    to keep the MAIN cast out of establishing stills (they get rendered later in
    shots). But that also stripped out the ambient crowd/audience that makes a
    venue read as alive, leaving courtrooms, theatres and streets eerily empty.

    The intent: keep the named/foreground cast out, but let anonymous background
    extras populate venues that would realistically have them. Genuinely private
    or intimate spaces (someone's apartment, a private office) stay empty. The
    image model sees the location name + description in the prompt, so it has the
    context to judge public-vs-private; we just instruct it explicitly.

    A per-location `image_constraints` field can override (e.g. "empty courtroom",
    "deserted street") — that text is injected separately and takes precedence.
    """
    return (
        "No main or foreground characters in frame (the named cast is rendered "
        "separately). However, populate the scene with anonymous background "
        "extras appropriate to this kind of place so it feels naturally alive — "
        "e.g. spectators filling the seats of an auditorium, a gallery of people "
        "in a courtroom in session, patrons in a restaurant, passersby and "
        "traffic on a street — while keeping any central stage / focal action "
        "area clear. EXCEPTION: if this is a private or intimate space that "
        "realistically has no bystanders (someone's apartment, a private office, "
        "a bedroom, a closed back room), or if the description implies it is "
        "empty/deserted, then render it with no people at all. "
    )


# Period/era markers — looked up in series genre + synopsis to bias character
# generation away from modern-default clothing. Without this, a series set in
# Ancient Egypt produced characters in leather jackets because the prompt
# fallback was «everyday casual attire».
# Real prod bug 2026-05-25: colleague's Ancient Egypt series rendered modern
# clothes for every character.
_ERA_KEYWORDS = {
    'ancient_egypt': ('egypt', 'pharaoh', 'pyramid', 'nile', 'фараон', 'египет', 'древнего египта', 'нил'),
    'ancient_rome':  ('rome', 'roman', 'caesar', 'gladiator', 'рим', 'цезарь', 'гладиатор'),
    'ancient_greece':('greece', 'greek', 'sparta', 'athen', 'грец', 'спарт', 'афин'),
    'medieval':      ('medieval', 'middle ages', 'knight', 'castle', 'crusade', 'kingdom', 'средневек', 'рыцар', 'замок', 'королевство'),
    'renaissance':   ('renaissance', 'tudor', 'elizabethan', 'florence', 'возрожден', 'тюдор'),
    'victorian':     ('victorian', 'georgian', 'regency', 'edwardian', 'викториан', 'эдуардовск'),
    'wild_west':     ('wild west', 'western', 'cowboy', 'gunslinger', 'frontier', 'вестерн', 'ковбой', 'дикий запад'),
    'edwardian_20s': ('1920s', 'jazz age', 'prohibition', 'roaring twenties', '20-е', 'двадцатые'),
    'wwii':          ('world war ii', 'wwii', 'second world war', '1940s', 'вторая мировая', '40-е'),
    '1950s':         ('1950s', '50s', 'post-war', 'постwar', '50-е', 'пятидесятые'),
    'cold_war_60s':  ('1960s', '60s', 'mod era', 'cold war', '60-е', 'шестидесятые'),
    '70s':           ('1970s', '70s', 'disco era', '70-е', 'семидесятые'),
    '80s':           ('1980s', '80s', 'reagan', '80-е', 'восьмидесятые'),
    '90s':           ('1990s', '90s', '90-е', 'девяностые'),
    'feudal_japan':  ('samurai', 'shogun', 'edo period', 'feudal japan', 'самурай', 'сёгун', 'феодальная япония'),
    'victorian_steampunk': ('steampunk', 'стимпанк'),
    'fantasy':       ('fantasy', 'dragon', 'elf', 'wizard', 'sorcery', 'фэнтези', 'дракон', 'эльф', 'маг', 'волшеб'),
    'post_apocalyptic': ('post-apocalyptic', 'post apocalyptic', 'wasteland', 'постапокал'),
    'sci_fi':        ('sci-fi', 'sci fi', 'science fiction', 'space opera', 'futuristic', 'фантастика', 'космич'),
}

from sw.cast import _char_name_in_text

def _detect_series_era(s):
    """Pure detection: return (era_key, matched_keyword) or (None, None).

    Word-boundary matching — `\\b…\\b` covers both Latin and Cyrillic under
    Python's default Unicode `re`. This prevents the 2026-05-30 incident
    where keyword `'нил'` (Nile river) was substring-matching inside
    «ра**нил**и» in a Russian synopsis and turning a modern crime series
    into an Ancient Egypt asset palette."""
    hay = ' '.join([
        (s.get('genre') or ''),
        (s.get('synopsis') or '')[:500],
        (s.get('title') or ''),
        (s.get('tone') or ''),
        (s.get('logline') or ''),
        (s.get('world_description') or '')[:500],
    ]).lower()
    if not hay.strip():
        return (None, None)
    for era, kws in _ERA_KEYWORDS.items():
        for kw in kws:
            try:
                pat = re.compile(rf'\b{re.escape(kw)}\b', re.UNICODE)
            except re.error:
                continue
            if pat.search(hay):
                return (era, kw)
    return (None, None)


# Period-specific guidance per matched era — clothing, accessories,
# silhouette cues that Banana/Seedream need to render correctly.
_ERA_GUIDES = {
        'ancient_egypt':
            "ERA CONTEXT: Ancient Egypt — period-accurate attire (linen kalasiris/schenti, "
            "gold collar (usekh), kohl eye makeup, sandals or barefoot, bronze/gold jewelry, "
            "natural fabrics, traditional headdresses for nobility). NO modern clothing, NO jeans, "
            "NO leather jackets, NO contemporary accessories.",
        'ancient_rome':
            "ERA CONTEXT: Ancient Rome — period-accurate attire (tunic, toga, palla, stola, "
            "leather sandals/caligae, simple jewelry, period hairstyles). NO modern clothing.",
        'ancient_greece':
            "ERA CONTEXT: Ancient Greece — period-accurate attire (chiton, himation, peplos, "
            "sandals, laurel wreaths for ceremonies). NO modern clothing.",
        'medieval':
            "ERA CONTEXT: Medieval European — period-accurate attire (tunics, gambeson, surcoats, "
            "kirtle, hose, leather boots, hooded cloaks, period-appropriate armor for warriors). "
            "NO modern clothing, NO synthetic fabrics, NO contemporary cuts.",
        'renaissance':
            "ERA CONTEXT: Renaissance — period-accurate attire (doublet, hose, ruff collars, "
            "farthingale skirts, embroidered fabrics, leather boots). NO modern clothing.",
        'victorian':
            "ERA CONTEXT: Victorian/Edwardian — period-accurate attire (frock coats, waistcoats, "
            "high collars, corseted bodices, bustled skirts, top hats, button boots). "
            "NO modern clothing.",
        'wild_west':
            "ERA CONTEXT: American Wild West (1860s-1890s) — period-accurate attire (denim/canvas "
            "trousers, vests, button shirts, dusters, cowboy hats, leather boots, gun belts). "
            "NO modern jeans/jackets — vintage cuts only.",
        'edwardian_20s':
            "ERA CONTEXT: 1920s Jazz Age — period-accurate attire (flapper dresses, drop waists, "
            "cloche hats, finger waves, three-piece suits, fedoras, oxford shoes). NO modern clothing.",
        '1950s':
            "ERA CONTEXT: 1950s post-war — period-accurate attire (full circle skirts, fitted "
            "bodices, petticoats, tailored suits with hats, victory-curl/pin-curl hair, saddle "
            "shoes, horn-rimmed glasses). NO modern clothing, NO contemporary cuts.",
        'wwii':
            "ERA CONTEXT: WWII / 1940s — period-accurate attire (military uniforms of the era, "
            "wide-shouldered suits, A-line skirts, victory rolls hair, utility wear). NO modern clothing.",
        'cold_war_60s':
            "ERA CONTEXT: 1960s — period-accurate attire (mod fashion, mini skirts, slim suits, "
            "go-go boots, beehive hair, bouffant). NO modern clothing.",
        '70s':
            "ERA CONTEXT: 1970s — period-accurate attire (bell-bottoms, wide collars, polyester, "
            "platform shoes, feathered hair). NO modern clothing cuts.",
        '80s':
            "ERA CONTEXT: 1980s — period-accurate attire (shoulder pads, neon, big hair, "
            "high-waisted jeans, leg warmers, oversized blazers). NO 2020s cuts.",
        '90s':
            "ERA CONTEXT: 1990s — period-accurate attire (grunge, baggy jeans, plaid flannel, "
            "slip dresses, choker necklaces). NO 2020s cuts.",
        'feudal_japan':
            "ERA CONTEXT: Feudal Japan — period-accurate attire (kimono, hakama, obi, samurai "
            "armor for warriors, period hairstyles like chonmage). NO modern clothing.",
        'victorian_steampunk':
            "ERA CONTEXT: Victorian Steampunk — Victorian silhouette + brass/copper accessories, "
            "goggles, mechanical details. No 21st-century clothing or tech.",
        'fantasy':
            "ERA CONTEXT: High fantasy — period-inspired attire (medieval/renaissance silhouettes "
            "with fantasy elements; armor for warriors, robes for mages, leather for rogues). "
            "NO modern clothing.",
        'post_apocalyptic':
            "ERA CONTEXT: Post-apocalyptic — improvised/salvaged attire (patched fabrics, layered "
            "scavenged clothing, gas masks/goggles, weathered leather). No pristine modern clothes.",
        'sci_fi':
            "ERA CONTEXT: Sci-fi / futuristic — futuristic attire (sleek bodysuits, tech "
            "accessories, smart fabrics, asymmetric cuts). NO contemporary 2020s casual wear.",
}


# Human-readable era labels — used by UI confirmation banner.
_ERA_LABELS = {
    'ancient_egypt':       'Древний Египет',
    'ancient_rome':        'Древний Рим',
    'ancient_greece':      'Древняя Греция',
    'medieval':            'Средневековье',
    'renaissance':         'Возрождение',
    'victorian':           'Викторианская эпоха',
    'wild_west':           'Дикий Запад',
    'edwardian_20s':       '1920-е',
    '1950s':               '1950-е',
    'wwii':                '1940-е / Вторая Мировая',
    'cold_war_60s':        '1960-е',
    '70s':                 '1970-е',
    '80s':                 '1980-е',
    '90s':                 '1990-е',
    'feudal_japan':        'Феодальная Япония',
    'victorian_steampunk': 'Стимпанк',
    'fantasy':             'Фэнтези',
    'post_apocalyptic':    'Постапокалипсис',
    'sci_fi':              'Sci-fi / Будущее',
}


def _series_era_hint(s):
    """Return era-context guide string for asset generation, or '' for modern.

    Respects the user's explicit choice stored on the series:
      • era_choice='modern' (or 'none')   → '' (always modern)
      • era_choice=<era key in _ERA_GUIDES> → that era's guide
      • era_choice='auto'/missing/'pending':
          – run detection;
          – if nothing detected → ''
          – if detected AND `era_confirmed` is True → guide for detected era
          – if detected AND not confirmed → '' (safe default; UI surfaces
            a confirmation banner asking the user to accept / change /
            decline before any non-modern look is applied)

    Why the «not-confirmed → ''» branch: detection has fired false-positives
    in production (substring match of «нил» in «ранили» → Ancient Egypt for
    a modern crime series). The user wants a confirmation gate before any
    non-modern style is locked in."""
    choice = (s.get('era_choice') or 'auto').strip().lower()
    if choice in ('modern', 'none', ''):
        return ''
    if choice in _ERA_GUIDES:
        return _ERA_GUIDES[choice]
    # 'auto' / 'pending' / anything else — fall back to detection
    era, _kw = _detect_series_era(s)
    if not era:
        return ''
    if not s.get('era_confirmed'):
        return ''  # gated — wait for user to confirm via UI
    return _ERA_GUIDES.get(era, '')


# Keywords that indicate clothing is already described in appearance/description.
_CLOTHING_WORDS = (
    'wearing', 'dressed', 'outfit', 'shirt', 'blouse', 'dress', 'skirt',
    'pants', 'trousers', 'jeans', 'jacket', 'coat', 'suit', 'uniform',
    'sweater', 'hoodie', 'vest', 'shorts', 'gown', 'robe', 'cloak',
    'clothes', 'clothing', 'attire', 'wardrobe', 'fabric', 'garment',
    # Russian equivalents
    'одет', 'носит', 'костюм', 'платье', 'рубашка', 'блузка', 'юбка',
    'брюки', 'джинсы', 'куртка', 'пальто', 'свитер', 'худи', 'шорты',
    'халат', 'мантия', 'одежда', 'форма',
)

def _clothing_clause(appearance: str, description: str = '', era_hint: str = '') -> str:
    """Return a clothing-fallback clause when neither appearance nor description
    mentions clothing. Prevents models from defaulting to lingerie/swimwear.

    If `era_hint` is set (series is historical/non-modern), the fallback is
    period-aware: «Fully clothed in period-appropriate attire matching the
    series setting.» Without era_hint, defaults to «everyday casual» which
    biases toward modern clothing — wrong for Ancient Egypt, Wild West, etc.
    """
    combined = (appearance + ' ' + description).lower()
    if any(w in combined for w in _CLOTHING_WORDS):
        return ''
    if era_hint:
        return 'Fully clothed in period-appropriate attire matching the series setting. '
    return 'Fully clothed in everyday casual attire. '


# Document-prop keywords. When an item's name OR description matches one of
# these, Banana defaults to «antique parchment with wax seal» because most
# training data tagged «document/contract/inheritance/registry» is historical.
# We catch this and inject a modern-document directive instead.
_DOCUMENT_KEYWORDS = (
    # English roots
    'document', 'contract', 'paper', 'letter', 'note', 'folder', 'file',
    'dossier', 'registry', 'register', 'certificate', 'form', 'report',
    'draft', 'statement', 'agreement', 'deed', 'will', 'testament', 'license',
    'permit', 'passport', 'manuscript', 'ledger', 'log', 'record', 'envelope',
    'invoice', 'receipt', 'bill', 'lease', 'résumé', 'resume', 'cv',
    'application', 'memo', 'dispatch', 'photograph', 'photo', 'photos',
    'newspaper', 'magazine', 'flyer', 'pamphlet', 'brochure', 'card', 'pass',
    # Russian roots
    'документ', 'докум', 'контракт', 'договор', 'бумаг', 'письм', 'записк',
    'папка', 'досье', 'регистр', 'реестр', 'сертификат', 'свидетельств',
    'форма', 'отчёт', 'отчет', 'заявлен', 'черновик', 'акт', 'заявка',
    'лицензия', 'паспорт', 'манускрипт', 'рукопис', 'грамот', 'дело',
    'протокол', 'конверт', 'счёт', 'счет', 'квитанция', 'дневник',
    'фотограф', 'фото', 'снимок', 'газета', 'журнал', 'листовк', 'буклет',
    'карточка', 'пропуск', 'удостоверение', 'визитка',
)


def _modern_document_directive(item) -> str:
    """If the item looks like a document/paper prop, return a directive that
    forces a modern crisp office-aesthetic look. Banana/Gemini Image Pro
    defaults to «yellowed antique parchment with wax seal + cursive» for
    anything that smells like «document», «contract», «registry», «грамота».
    This pulls it back to «something you'd see on a desk today».

    Skip when:
      • The item already has user-set `image_constraints` — user has spoken,
        don't contradict their explicit wish.
      • Name/description hit none of the document keywords.
    """
    if (item.get('image_constraints') or '').strip():
        return ''
    haystack = (
        (item.get('name') or '') + ' ' + (item.get('description') or '')
    ).lower()
    if not any(kw in haystack for kw in _DOCUMENT_KEYWORDS):
        return ''
    return (
        " MODERN DOCUMENT STYLE — STRICTLY ENFORCE: crisp clean modern paper "
        "(A4 / US letter size if applicable), contemporary printed or laser-"
        "printed text in standard digital typography (Times / Arial / Helvetica), "
        "white or very pale cream paper, sharp clean edges, today's office "
        "aesthetic — looks like it was printed yesterday. "
        "ABSOLUTELY NO: yellowing, no aging, no fading, no tea-stained paper, "
        "no parchment, no scrolls, no medieval / 19th-century styling, "
        "no leather-bound antique books, no calligraphic cursive handwriting, "
        "no illuminated manuscript decorations, no wax seals (red / brown / any), "
        "no ribbon binding, no string-tied stacks, no rough deckle edges, "
        "no quill / inkwell / fountain-pen drama. "
        "If signatures appear — modern blue or black ballpoint / fountain-pen ink "
        "on the signature line, NOT elaborate cursive flourishes. "
        "If multiple pages — neatly stacked or stapled like in an office, "
        "not bundled with string. "
    )


# Genitive/possessive endings we strip off a cast name so "Виски Маркуса" and
# "Marcus's whiskey" both collapse to the bare object. Russian genitive +
# common case endings; English handled separately via the apostrophe form.
_NAME_INFLECTIONS = (
    'а', 'я', 'ы', 'и', 'у', 'ю', 'е', 'ом', 'ём', 'ой', 'ей', 'ью',
    'ах', 'ях', 'ов', 'ев', 'ин', 'ина', 'ум',
)

def _strip_cast_names_for_visual(text, s):
    """Remove KNOWN cast names (and their possessive/genitive inflections) from
    a visual-subject string.

    Root cause of the «надпись на предмете» bug: asset names are possessive
    labels — «Виски Маркуса», «Квартира Маркуса», «Marcus's whiskey». They are
    fed as the LEADING subject of the image prompt, and Banana/Gemini/Seedance
    read a leading noun phrase as a CAPTION and literally stamp it onto the
    render (a whisky label reading «Виски Маркуса», a nameplate on the building
    reading «Квартира Маркуса»). Stripping the owner's name leaves the bare
    object/place — exactly what should be drawn. Targeted to cast names only,
    so it can't mangle generic descriptions.
    """
    if not text:
        return text
    names = sorted(
        ((c.get('name') or '').strip() for c in (s.get('characters') or [])),
        key=len, reverse=True,
    )
    for nm in names:
        if len(nm) < 3:
            continue  # too short → false-positive risk inside other words
        esc = re.escape(nm)
        # English possessive: Marcus's / Marcus' / Marcus’s
        text = re.sub(rf"\b{esc}['’]s?\b", '', text, flags=re.IGNORECASE)
        # Bare name + optional RU genitive/case ending: Маркуса, Маркусу, Marcus
        endings = '|'.join(sorted(_NAME_INFLECTIONS, key=len, reverse=True))
        text = re.sub(rf"\b{esc}(?:{endings})?\b", '', text, flags=re.IGNORECASE)
    # Tidy up the holes left behind ("Виски  ." → "Виски").
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\s+([.,;:])', r'\1', text)
    return text.strip(' .,-—«»"')


_TEXT_REQUEST_WORDS = (
    'text', 'sign', 'signage', 'label', 'lettering', 'word', 'caption',
    'logo', 'brand', 'plaque', 'banner', 'inscription', 'engrav', 'written',
    'надпис', 'текст', 'вывеск', 'этикетк', 'логотип', 'буква', 'слов',
    'табличк', 'баннер', 'гравиров', 'написан',
)

def _no_caption_text_clause(user_constraints: str = '') -> str:
    """Hard directive forbidding the image model from stamping the asset's name
    (or any person's name) onto props / buildings / locations as literal text.

    This is COSMETIC-text suppression, not document suppression — document
    props that legitimately need printed text get _modern_document_directive
    instead and must SKIP this clause (see call sites).

    If the user's own constraints explicitly ask for text/signage/a label,
    return nothing — don't fight an explicit wish."""
    if user_constraints and any(w in user_constraints.lower() for w in _TEXT_REQUEST_WORDS):
        return ''
    return (
        " NO TEXT ON THE IMAGE — STRICTLY ENFORCE: do not render any text, "
        "letters, words, names, captions, titles, labels, nameplates, logos, "
        "brand names, signage or writing anywhere in the frame. Never spell "
        "out the name of this object / place or any person's name on it. Any "
        "surface that would normally carry text (a bottle label, a shop sign, "
        "a door plaque, a banner) must be left blank or show only abstract, "
        "illegible, non-lettered marks. "
    )


# ── Characters ───────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/characters', methods=['POST'])
def add_character(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    char = {
        'id': str(uuid.uuid4())[:8],
        'name': data['name'],
        'description': data.get('description', ''),
        'appearance': data.get('appearance', ''),
        'gender': data.get('gender', 'female'),
        'voice_id': data.get('voice_id', ''),
        'ref_images': []
    }
    s['characters'].append(char)
    save_series(sid, s)
    return jsonify(char), 201

@app.route('/api/series/<sid>/fix-anthro-species', methods=['POST'])
def fix_anthro_species(sid):
    """Bulk-fix endpoint: scan every character, and when the series is an
    anthro/furry world but a character lacks species in name/appearance, infer
    the species via LLM and patch the appearance. Clears the character's
    portrait so the UI can show "regenerate" prompts. Returns the list of
    patched characters with old/new appearance for review.
    Real bug: "The Landlord's Daughter" generated 3 human portraits in a furry
    world because the cast extractor dropped species words. This endpoint lets
    the user repair an existing series in one click."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    if not _is_anthro_world(s):
        return jsonify({
            'patched': [],
            'message': 'Этот сериал не определяется как анthrо-мир — нечего чинить. Если это ошибка, добавь слово «furry» или «anthropomorphic» в synopsis / world_description.',
        })
    patched = []
    for char in (s.get('characters') or []):
        existing_species = _detect_animal_species_raw(char.get('name'), char.get('appearance'))
        if existing_species:
            continue  # already species-coded — skip
        inferred = _llm_infer_species_for_char(s, char)
        if not inferred or inferred == 'human':
            continue
        old_appearance = char.get('appearance', '')
        new_appearance = _patch_appearance_with_species(old_appearance, inferred, char.get('gender', ''))
        if new_appearance == old_appearance:
            continue
        char['appearance'] = new_appearance
        # Drop stale portrait so the UI shows "regenerate".
        char['avai_base_url'] = ''
        char['updated_at'] = int(time.time())
        patched.append({
            'id': char.get('id'),
            'name': char.get('name'),
            'inferred_species': inferred,
            'old_appearance': old_appearance,
            'new_appearance': new_appearance,
        })
    if patched:
        save_series(sid, s)
    return jsonify({
        'patched': patched,
        'count': len(patched),
        'message': (f'Пропатчено персонажей: {len(patched)}. Теперь жми «Перегенерить» на каждом — портрет переснимется как {", ".join(p["inferred_species"] for p in patched)}.'
                    if patched else 'Все персонажи уже имеют species в appearance — патчить нечего.')
    })


@app.route('/api/series/<sid>/characters/<char_id>', methods=['PUT'])
def update_character(sid, char_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    for i, c in enumerate(s['characters']):
        if c['id'] == char_id:
            s['characters'][i].update(request.json)
            save_series(sid, s)
            return jsonify(s['characters'][i])
    return jsonify({'error': 'character not found'}), 404

@app.route('/api/series/<sid>/characters/<char_id>', methods=['DELETE'])
def delete_character(sid, char_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['characters'] = [c for c in s['characters'] if c['id'] != char_id]
    # Remove from all episodes
    for ep in list_episodes(sid):
        if char_id in ep.get('characters_used', []):
            ep['characters_used'].remove(char_id)
            save_episode(sid, ep['number'], ep)
    # Remove asset folder
    char_dir = assets_dir(sid) / 'characters' / char_id
    if char_dir.exists():
        shutil.rmtree(char_dir)
    save_series(sid, s)
    return jsonify({'ok': True})


# ── Locations ────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/locations', methods=['POST'])
def add_location(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    loc = {
        'id': str(uuid.uuid4())[:8],
        'name': data['name'],
        'description': data.get('description', ''),
        'ref_images': []
    }
    s.setdefault('locations', []).append(loc)
    save_series(sid, s)
    return jsonify(loc), 201

@app.route('/api/series/<sid>/locations/<loc_id>', methods=['PUT'])
def update_location(sid, loc_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    for i, l in enumerate(s.get('locations', [])):
        if l['id'] == loc_id:
            s['locations'][i].update(request.json)
            save_series(sid, s)
            return jsonify(s['locations'][i])
    return jsonify({'error': 'not found'}), 404

@app.route('/api/series/<sid>/locations/<loc_id>', methods=['DELETE'])
def delete_location(sid, loc_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['locations'] = [l for l in s.get('locations', []) if l['id'] != loc_id]
    loc_dir = assets_dir(sid) / 'locations' / loc_id
    if loc_dir.exists():
        shutil.rmtree(loc_dir)
    for ep in list_episodes(sid):
        if loc_id in ep.get('locations_used', []):
            ep['locations_used'].remove(loc_id)
            save_episode(sid, ep['number'], ep)
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/assets/location/<loc_id>', methods=['POST'])
def upload_location_asset(sid, loc_id):
    """Replace-semantics location upload (mirrors character endpoint above)."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'location not found'}), 404
    loc_dir = assets_dir(sid) / 'locations' / loc_id
    loc_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    final = loc_dir / filename
    rel_path = str(final.relative_to(series_path(sid)))
    base = series_path(sid)
    for old_rel in (loc.get('ref_images') or []):
        if old_rel == rel_path: continue
        try: (base / old_rel).unlink(missing_ok=True)
        except Exception: pass
    file.save(final)
    loc['ref_images'] = [rel_path]
    loc['avai_url'] = ''
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}', 'series': s})

@app.route('/api/series/<sid>/assets/location/<loc_id>/<path:filename>', methods=['DELETE'])
def delete_location_asset(sid, loc_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'locations' / loc_id / filename
    if full.exists():
        full.unlink()
    rel = f'assets/locations/{loc_id}/{filename}'
    for loc in s.get('locations', []):
        if loc['id'] == loc_id:
            loc['ref_images'] = [r for r in loc.get('ref_images', []) if r != rel]
    save_series(sid, s)
    return jsonify({'ok': True})


# ── Items (story-relevant props: handbag, gun, locket, etc.) ─────────────────
# Items are like locations but for THINGS. Same shape: { id, name, description,
# ref_images, image_constraints?, avai_url? }. Used by Seedance/Reteller as
# additional reference images when the item is plot-critical (e.g. the stolen
# handbag passed between characters across episodes).

@app.route('/api/series/<sid>/items', methods=['POST'])
def add_item(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    item = {
        'id': str(uuid.uuid4())[:8],
        'name': data['name'],
        'description': data.get('description', ''),
        'ref_images': []
    }
    s.setdefault('items', []).append(item)
    save_series(sid, s)
    return jsonify(item), 201

@app.route('/api/series/<sid>/items/<item_id>', methods=['PUT'])
def update_item(sid, item_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    for i, it in enumerate(s.get('items', [])):
        if it['id'] == item_id:
            s['items'][i].update(request.json)
            save_series(sid, s)
            return jsonify(s['items'][i])
    return jsonify({'error': 'not found'}), 404

@app.route('/api/series/<sid>/items/<item_id>', methods=['DELETE'])
def delete_item(sid, item_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    # Capture name BEFORE removing so we can also clean a slug-named dir
    # (in case generate-image used slug-based path).
    item_obj = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    item_slug = slugify(item_obj['name']) if item_obj else None

    s['items'] = [it for it in s.get('items', []) if it['id'] != item_id]
    for d in (assets_dir(sid) / 'items' / item_id, ):
        if d.exists():
            shutil.rmtree(d)
    if item_slug:
        d2 = assets_dir(sid) / 'items' / item_slug
        if d2.exists():
            shutil.rmtree(d2)
    for ep in list_episodes(sid):
        if item_id in ep.get('items_used', []):
            ep['items_used'].remove(item_id)
            save_episode(sid, ep['number'], ep)
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/assets/item/<item_id>', methods=['POST'])
def upload_item_asset(sid, item_id):
    """Replace-semantics item upload (mirrors character endpoint above)."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400
    item = next((x for x in s.get('items', []) if x['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'item not found'}), 404
    item_dir = assets_dir(sid) / 'items' / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    final = item_dir / filename
    rel_path = str(final.relative_to(series_path(sid)))
    base = series_path(sid)
    for old_rel in (item.get('ref_images') or []):
        if old_rel == rel_path: continue
        try: (base / old_rel).unlink(missing_ok=True)
        except Exception: pass
    file.save(final)
    item['ref_images'] = [rel_path]
    item['avai_url'] = ''
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}'})

@app.route('/api/series/<sid>/assets/item/<item_id>/<path:filename>', methods=['DELETE'])
def delete_item_asset(sid, item_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'items' / item_id / filename
    if full.exists():
        full.unlink()
    rel = f'assets/items/{item_id}/{filename}'
    for it in s.get('items', []):
        if it['id'] == item_id:
            it['ref_images'] = [r for r in it.get('ref_images', []) if r != rel]
    save_series(sid, s)
    return jsonify({'ok': True})


# ── Outfits ───────────────────────────────────────────────────────────────────

def get_char(s, char_id):
    return next((c for c in s['characters'] if c['id'] == char_id), None)

def get_outfit(char, outfit_id):
    return next((o for o in char.get('outfits', []) if o['id'] == outfit_id), None)

@app.route('/api/series/<sid>/characters/<char_id>/outfits', methods=['POST'])
def add_outfit(sid, char_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    outfit = {
        'id': str(uuid.uuid4())[:8],
        'label': data['label'],
        'description': data.get('description', ''),
        'photo': None,
        'reteller_project_id': None,
    }
    char.setdefault('outfits', []).append(outfit)
    save_series(sid, s)
    return jsonify(outfit), 201

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>', methods=['PUT'])
def update_outfit(sid, char_id, outfit_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404
    outfit.update({k: v for k, v in request.json.items() if k != 'id'})
    save_series(sid, s)
    return jsonify(outfit)

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>', methods=['DELETE'])
def delete_outfit(sid, char_id, outfit_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if outfit and outfit.get('photo'):
        p = series_path(sid) / outfit['photo']
        if p.exists():
            p.unlink()
    char['outfits'] = [o for o in char.get('outfits', []) if o['id'] != outfit_id]
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>/use-base', methods=['POST'])
def link_outfit_to_base(sid, char_id, outfit_id):
    """Mark outfit as 'this IS the base look' — no separate generation needed.
    Sets outfit.photo = char.ref_images[0] and outfit.is_base = True."""
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'character not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404
    if not char.get('ref_images'):
        return jsonify({'error': 'У персонажа нет базового фото'}), 400

    # Clear any existing standalone outfit photo file (the one specific to this outfit)
    if outfit.get('photo') and outfit['photo'] != char['ref_images'][0]:
        old = series_path(sid) / outfit['photo']
        if old.exists():
            try:
                old.unlink()
            except Exception:
                pass

    outfit['photo'] = char['ref_images'][0]
    outfit['avai_url'] = char.get('avai_base_url', '')
    outfit['is_base'] = True
    # Unmark any other outfits as base (only one base per character)
    for o in char.get('outfits', []):
        if o['id'] != outfit_id and o.get('is_base'):
            o['is_base'] = False
    save_series(sid, s)
    return jsonify({'ok': True, 'photo': outfit['photo']})


@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>/generate', methods=['POST'])
def generate_outfit_image(sid, char_id, outfit_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'character not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404
    _nd_anthro, _fl_anthro = _anthro_preflight(s, [char])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    if not char.get('ref_images'):
        # Auto-generate the base portrait first — the user shouldn't have to chase a "load base"
        # error when we have the appearance text and can produce one inline.
        if not (char.get('appearance') or '').strip():
            return jsonify({
                'error': 'Сначала впиши описание внешности персонажа (поле appearance) — без него базовое фото не сгенерится'
            }), 400
        try:
            _gen_char_base_inline(s, sid, char)
            save_series(sid, s)
        except Exception as e:
            return jsonify({'error': f'Не удалось автоматически создать базовое фото: {e}'}), 500

    # Use i2i reference URL if available (AVAI Supabase URL from base generation)
    reference_url = char.get('avai_base_url')

    gender = 'woman' if char.get('gender') == 'female' else 'man'
    if reference_url:
        # i2i: same face, new clothes
        prompt = (
            f'Same {gender} as the reference image. Now wearing: {outfit["label"]}. '
            f'{outfit.get("description", "")}. '
            f'Same face, same hair, same body — only the clothing changes. '
            f'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
            f'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
            f'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
            f'Soft even diffused illumination on the character only (no visible lights or equipment), no harsh shadows. Photorealistic, cinematic quality.'
        )
    else:
        # No reference — generate from scratch with description
        prompt = (
            f'Full body portrait of {char["name"]}, a {gender}. '
            f'{char.get("appearance", "")}. '
            f'Wearing: {outfit["label"]}. {outfit.get("description", "")}. '
            f'Standing facing camera, slight 3/4 angle. Neutral relaxed pose. '
            'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
            f'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
            f'Soft even diffused illumination on the character only (no visible lights or equipment), no harsh shadows. Photorealistic, cinematic quality.'
        )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    # Variation token — provider-level caching (Banana/Seedream deduplicate
    # identical prompt strings) was returning the same image on every regen,
    # so deleting + re-generating an outfit pulled back the cached old one.
    # User-reported 2026-05-23. Same fix as `regenerate_character`.
    variation_token = uuid.uuid4().hex[:8]
    prompt = f"{prompt} [v{variation_token}]"

    char_slug = slugify(char['name'])
    out_dir = assets_dir(sid) / 'characters' / char_slug / 'outfits'
    out_path = out_dir / f'{asset_name(char["name"], outfit["label"])}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, reference_url=reference_url, preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        # Remove old photo if exists
        if outfit.get('photo'):
            old = series_path(sid) / outfit['photo']
            if old.exists() and str(old) != str(out_path):
                old.unlink()
        outfit['photo'] = rel_path
        outfit['avai_url'] = image_url  # store for future i2i variants
        # Bump per-asset version so frontend cache-buster `?v=N` flips and the
        # browser actually re-fetches. Without this, even after AVAI returns
        # a new image, the local file path stays the same and browser shows
        # the cached old image.
        outfit['image_version'] = int(time.time())
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url, 'image_version': outfit['image_version']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>/save-frame/<project_id>', methods=['POST'])
def save_outfit_frame(sid, char_id, outfit_id, project_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404

    hdrs = rtl_headers()
    status_resp = requests.get(f'{RETELLER_API}/projects/{project_id}', headers=hdrs, timeout=15)
    if not status_resp.ok:
        return jsonify({'error': status_resp.text}), 500
    if status_resp.json().get('status') != 'completed':
        return jsonify({'ready': False, 'status': status_resp.json().get('status')})

    frames_resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}/assets/list?types=frames',
        headers=hdrs, timeout=15,
    )
    if not frames_resp.ok:
        return jsonify({'error': frames_resp.text}), 500

    frames = [a for a in frames_resp.json().get('assets', []) if a['type'] == 'frames']
    if not frames:
        return jsonify({'ready': False, 'status': 'no_frames'})

    img_resp = requests.get(frames[0]['url'], timeout=30)
    if not img_resp.ok:
        return jsonify({'error': 'download failed'}), 500

    char_slug = slugify(char['name'])
    out_dir = assets_dir(sid) / 'characters' / char_slug / 'outfits'
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_name(char["name"], outfit.get("label", outfit_id))}.jpg'  # e.g. CLAIRE_WORK_BLAZER.jpg
    (out_dir / filename).write_bytes(img_resp.content)

    rel_path = f'assets/characters/{char_slug}/outfits/{filename}'
    # Remove old generated photo if exists
    if outfit.get('photo'):
        old = series_path(sid) / outfit['photo']
        if old.exists():
            old.unlink()
    outfit['photo'] = rel_path
    save_series(sid, s)

    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}'})


from sw.anthro import (
    _ANIMAL_SPECIES,
    _AMBIGUOUS_APPEARANCE_SPECIES,
    _SKIN_FEATURE_CONTEXT_WORDS,
    _is_skin_feature_context,
    _NONANATOMICAL_MARKER_CONTEXT,
    _is_nonanatomical_marker_context,
    _detect_animal_species_raw,
    AnthroPermissionRequired,
    _anthro_unlocked,
    _anthro_decided,
    _detect_animal_species,
    _anthro_preflight,
    _ANTHRO_WORLD_KEYWORDS,
    _detect_anthro_world_raw,
    _is_anthro_world,
    _anthro_world_block,
    _casting_aesthetics_block,
    _revision_instructions_block,
    _llm_apply_revisions_to_bible,
    _llm_infer_species_for_char,
    _patch_appearance_with_species,
    _SCRIPT_POSE_PATTERNS,
    _PREP_EN_TO_RU,
    _detect_script_pose_for_char,
    _override_vision_with_script_poses,
)
# ── Generate character image via Reteller/Banana ─────────────────────────────

@app.route('/api/series/<sid>/characters/<char_id>/generate-image', methods=['POST'])
def generate_character_image(sid, char_id):
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404

    _nd_anthro, _fl_anthro = _anthro_preflight(s, [char])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    style_clause = _series_style_clause(s)
    era_clause = _series_era_hint(s)
    _appearance = char.get('appearance', '')
    _desc = char.get('description', '')
    # Species-aware framing — see _detect_animal_species docstring for context.
    species_hint = _detect_animal_species(char.get('name'), _appearance, series=s)
    # Self-heal for anthro worlds: if the SERIES is anthropomorphic but THIS
    # character has no species in name/appearance, infer the species from the
    # series synopsis (one short LLM call) and patch the appearance so future
    # generations stay consistent. Fixes the bug where Sofia/Marcus/Anita
    # rendered as humans in a furry world because the cast extractor dropped
    # species words from their appearance text.
    if not species_hint and _is_anthro_world(s):
        inferred = _llm_infer_species_for_char(s, char)
        if inferred and inferred != 'human':
            patched = _patch_appearance_with_species(_appearance, inferred, char.get('gender', ''))
            if patched and patched != _appearance:
                char['appearance'] = patched
                _appearance = patched
                save_series(sid, s)
                print(f'[anthro-heal] char {char.get("name")} → species={inferred}; appearance patched', flush=True)
            species_hint = _detect_animal_species(char.get('name'), _appearance, series=s)
    if species_hint:
        gender_word = 'female' if char.get('gender') == 'female' else 'male'
        kind_label = f', a {gender_word} {species_hint}'
        species_override = (
            f" CRITICAL: {char['name']} is an ANTHROPOMORPHIC {species_hint.split()[-1].upper()}, "
            f"NOT a human. The character has a {species_hint.split()[-1]}'s head/face "
            f"(realistic snout, ears, eyes typical of the species) with appropriate fur/feathers/scales, "
            f"walking upright with anthropomorphic body proportions, wearing human-style clothing. "
            f"Zootopia/Pixar-style anthropomorphic animal — DO NOT render as a plain human. "
            f"Ignore any wording like «a man» / «a woman» in the description below — those describe "
            f"the character's gender role, not human anatomy."
        )
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
        species_override = ''
    prompt = (
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{_appearance}. {_desc}.{species_override} "
        f"{era_clause + ' ' if era_clause else ''}"
        f"{_clothing_clause(_appearance, _desc, era_hint=era_clause)}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. "
        f"Soft even diffused illumination on the character only (lighting source NOT visible in frame), no harsh shadows on face or body, no visible lights or equipment. "
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    out_path = char_dir / f'{asset_name(char["name"], "BASE")}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = char.setdefault('ref_images', [])
        # Replace or prepend
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        # Store remote URL for i2i outfit variants later
        char['avai_base_url'] = image_url
        char['updated_at'] = int(time.time())   # cache-bust signal for frontend URLs
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/characters/<char_id>/regenerate', methods=['POST'])
def regenerate_character(sid, char_id):
    """Regenerate the character's base portrait with user-supplied constraints
    ("без шрама", "глаза карие" и т.п.), then optionally re-roll all outfits
    using the new base as i2i reference. Persists the constraints on the char
    so future generations honor them too."""
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'character not found'}), 404

    _nd_anthro, _fl_anthro = _anthro_preflight(s, [char])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    regen_outfits = bool(body.get('regenerate_outfits', True))
    # When true: rewrite appearance via Claude before generating (breaks
    # out of the «same prompt → same result» loop when appearance is stale).
    rewrite_appearance = bool(body.get('rewrite_appearance', False))
    # Per-call provider override (UI dropdown on the character lightbox).
    # Empty → fall back to series-level preference. Accepts: '', 'banana',
    # 'seedream', 'openai' (gpt-image-1 via AVAI). Used for BOTH base
    # portrait and outfit regeneration on this call so the look stays
    # consistent. Per-series default unchanged on this path.
    provider_override = (body.get('provider') or '').strip().lower()
    if provider_override not in ('', 'banana', 'seedream', 'openai'):
        provider_override = ''
    effective_provider = provider_override or _series_image_provider(s)

    # AUTO-rewrite when name implies an anthropomorphic animal AND appearance
    # text currently describes a plain human. Without this, the canonical
    # APPEARANCE field that gets fed into the Seedance binding later still
    # reads "A man holding interview papers" → Seedance keeps drawing a man
    # even when the ref portrait is now an anthropomorphic hyena. Bug case:
    # «The Fox CEO's Trap» — user clicked regenerate, got a Pixar man back.
    species_hint_pre = _detect_animal_species(char.get('name'), char.get('appearance'), series=s)
    # Self-heal for anthro worlds: char has no species but series IS anthro.
    # Infer species from synopsis, patch appearance, and proceed as if the
    # species had been there all along. Without this the regenerate flow
    # falls through to the human-rendering branch.
    if not species_hint_pre and _is_anthro_world(s):
        inferred = _llm_infer_species_for_char(s, char)
        if inferred and inferred != 'human':
            patched = _patch_appearance_with_species(char.get('appearance', ''), inferred, char.get('gender', ''))
            if patched and patched != char.get('appearance'):
                char['appearance'] = patched
                print(f'[anthro-heal] regenerate: char {char.get("name")} → species={inferred}', flush=True)
            species_hint_pre = _detect_animal_species(char.get('name'), char.get('appearance'), series=s)
    if species_hint_pre:
        appearance_raw_check = (char.get('appearance') or '').lower()
        species_word = species_hint_pre.split()[-1].lower()
        already_animal = (
            species_word in appearance_raw_check
            or 'anthropomorphic' in appearance_raw_check
            or any(w in appearance_raw_check for w in (
                'fur', 'muzzle', 'snout', 'tail', 'paws', 'whiskers', 'mane',
                'fang', 'fangs', 'feathers', 'beak'))
        )
        looks_human = any(w in appearance_raw_check for w in (
            ' man ', ' man.', ' man,', ' woman ', ' woman.', ' woman,',
            ' boy ', ' boy.', ' boy,', ' girl ', ' girl.', ' girl,',
            'a man', 'a woman', 'a boy', 'a girl',
            'young man', 'young woman', 'beautiful woman', 'businessman',
        ))
        if looks_human and not already_animal:
            rewrite_appearance = True
            # Stuff the species hint into wishes so the rewriter knows what
            # to make. Preserve user-provided wishes too.
            extra = (f"Character is an ANTHROPOMORPHIC {species_word.upper()} — "
                     f"rewrite appearance with {species_word} features (snout, ears, "
                     f"fur color/pattern, body type), NOT a human. Keep human-style "
                     f"clothing and the named props/gender role.")
            wishes = (wishes + ' ' + extra).strip() if wishes else extra

    # Persist constraints onto the character so future inline generations
    # also respect them. Empty wishes => clear them.
    char['image_constraints'] = wishes

    # ── PROMPT CLEANUP — fix the «приходит constraint но в appearance уже
    # сидит конфликтующая фраза» class of bugs.
    #
    # Real case (Mia Chen): appearance = "Young fashion blogger with multiple
    # monitors", constraints = "Убери мониторы". Image model sees both
    # «multiple monitors» (positive) and «убери мониторы» (negative). Negatives
    # are weak signal — model renders monitors regardless. After 3-4 regen
    # attempts user gives up.
    #
    # When the user passes wishes/constraints, run a fast Claude pass to:
    # 1. Detect if `appearance` contains scene/context-words that conflict
    #    with constraints (props/locations/objects, not person looks).
    # 2. Rewrite appearance to person-only (face, hair, build, vibe, age,
    #    typical clothing if relevant). Persist the cleaned version.
    # 3. Use the cleaned appearance in the generation prompt.
    appearance_raw = (char.get('appearance') or '').strip()
    appearance_for_prompt = appearance_raw

    # ── Rewrite appearance from scratch via Claude (breaks same-prompt loop) ──
    # Triggered when user clicks "Перегенерить с новым описанием" or when the
    # appearance text is clearly scene-specific (emotional states, actions, etc.)
    # and the user wants a completely fresh canonical visual description.
    if rewrite_appearance:
        desc_source = char.get('description') or ''
        try:
            rewritten = claude_ask_fast(
                f"CHARACTER NAME: {char.get('name', '')}\n"
                f"CHARACTER DESCRIPTION (story role, personality): {desc_source}\n"
                f"CURRENT APPEARANCE TEXT: {appearance_raw}\n"
                + (f"USER CONSTRAINTS: {wishes}\n" if wishes else '')
                + "\nTask: write a fresh canonical APPEARANCE field for image generation. "
                "Rules: (a) physical traits ONLY — age, build, hair color/length/style, "
                "eye color, face shape, distinguishing features, typical clothing; "
                "(b) NO scene context, NO emotions, NO actions, NO props; "
                "(c) concrete and specific — 'shoulder-length auburn hair' not 'beautiful hair'; "
                "(d) 1-3 short sentences, comma-separated descriptors, NO 'she is' opener; "
                "(e) MODERATION-SAFE — describe an attractive person with NEUTRAL words "
                "(elegant, graceful, soft features, slim) but NEVER use sexual / explicit / "
                "nudity wording (no 'sexy', 'sensual', 'seductive', 'cleavage', 'nude', "
                "'lingerie', 'sexual', «сексуальная», «чувственные», «голая», «декольте» etc.). "
                "If the user constraints ask for something sexual or nude, IGNORE that for this "
                "field — it only affects the rendered image, never the stored description.\n"
                "Output: the appearance text only, no quotes, no preamble.",
                system="You write character appearance descriptions for image generation. Plain text only.",
            ).strip().strip('"\'`')
            if rewritten and len(rewritten) > 10:
                # Hard scrub: the LLM is instructed to stay clean, but never trust
                # it — the persisted field rides into every future prompt. The
                # render still gets the raw wish via constraints_clause below.
                rewritten = _sanitize_appearance_for_moderation(rewritten)
                appearance_for_prompt = rewritten
                char['appearance'] = rewritten
                _log_event('INFO', 'appearance_rewritten',
                           char_id=char_id, name=char.get('name', ''),
                           before=appearance_raw[:200], after=rewritten[:200])
        except Exception as e:
            _log_event('WARN', 'appearance_rewrite_failed', char_id=char_id, err=str(e)[:200])

    elif wishes and appearance_raw:
        # ── Cleanup: remove scene-context from appearance that conflicts with wishes ──
        try:
            cleaned = claude_ask_fast(
                f"CHARACTER APPEARANCE FIELD: {appearance_raw}\n"
                f"USER CONSTRAINTS FOR IMAGE GENERATION: {wishes}\n\n"
                "Task: rewrite the appearance field so it (a) describes ONLY the "
                "person's physical traits (face, hair, build, age, characteristic "
                "clothing), NOT scene/context (props in background, locations, "
                "moods, activities); AND (b) does not contradict the user's "
                "constraints (if user said «убери мониторы», don't mention monitors).\n\n"
                "Output: 1-2 short sentences, plain text, no preamble, no quotes. "
                "Keep the language of the original appearance text.",
                system="You are a surgical text editor. Output the rewritten sentence(s) and nothing else.",
            ).strip()
            cleaned = cleaned.strip('"\'`')
            if cleaned and len(cleaned) < len(appearance_raw) * 2 and len(cleaned) > 5:
                cleaned = _sanitize_appearance_for_moderation(cleaned)
                appearance_for_prompt = cleaned
                char['appearance'] = cleaned
                _log_event('INFO', 'appearance_cleaned',
                           char_id=char_id, name=char.get('name', ''),
                           before=appearance_raw[:200], after=cleaned[:200],
                           wishes=wishes[:200])
        except Exception as e:
            _log_event('WARN', 'appearance_cleanup_failed',
                       char_id=char_id, err=str(e)[:200])

    # Whatever path produced appearance_for_prompt (rewrite, cleanup, or
    # untouched legacy text), guarantee both the prompt text AND the persisted
    # description are moderation-clean. The spicy wish still reaches the RENDER
    # via constraints_clause below — only the stored/BINDING text is scrubbed.
    appearance_for_prompt = _sanitize_appearance_for_moderation(appearance_for_prompt)
    if appearance_for_prompt and appearance_for_prompt != (char.get('appearance') or '').strip():
        char['appearance'] = appearance_for_prompt

    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    style_clause = _series_style_clause(s)
    era_clause = _series_era_hint(s)
    _desc = char.get('description', '')
    # Species-aware framing — same logic as generate_character_image. Without
    # this, regenerate_character would re-render a Wolf/Hyena/Fox as a human
    # because the hardcoded ", a man/woman" prefix overpowers any anthro hint.
    species_hint = _detect_animal_species(char.get('name'), appearance_for_prompt, series=s)
    if species_hint:
        gender_word = 'female' if char.get('gender') == 'female' else 'male'
        kind_label = f', a {gender_word} {species_hint}'
        species_override = (
            f" CRITICAL: {char['name']} is an ANTHROPOMORPHIC {species_hint.split()[-1].upper()}, "
            f"NOT a human. The character has a {species_hint.split()[-1]}'s head/face "
            f"(realistic snout, ears, eyes typical of the species) with appropriate fur/feathers/scales, "
            f"walking upright with anthropomorphic body proportions, wearing human-style clothing. "
            f"Zootopia/Pixar-style anthropomorphic animal — DO NOT render as a plain human. "
            f"Ignore any wording like «a man» / «a woman» in the description below — those describe "
            f"the character's gender role, not human anatomy."
        )
        clothing_fallback = ''
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
        species_override = ''
        clothing_fallback = _clothing_clause(appearance_for_prompt, _desc, era_hint=era_clause)
    prompt = (
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{appearance_for_prompt}. {_desc}.{constraints_clause}{species_override} "
        f"{era_clause + ' ' if era_clause else ''}"
        f"{clothing_fallback}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. "
        f"Soft even diffused illumination on the character only (lighting source NOT visible in frame), no harsh shadows on face or body, no visible lights or equipment. "
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    # Append a short random token so each regeneration is a unique API request —
    # provider-level caching (Banana/Seedream deduplicate identical prompt strings)
    # was causing the same image to come back on every regen.
    variation_token = uuid.uuid4().hex[:8]
    prompt = f"{prompt} [v{variation_token}]"

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    out_path = char_dir / f'{asset_name(char["name"], "BASE")}.jpg'

    # 1) Regenerate base
    try:
        # Don't preemptively delete the old file — if avai_generate fails
        # (network blip, 401, content-filter), we'd have killed the user's
        # existing photo with no replacement, leaving ref_images pointing at
        # a vanished path. avai_generate will overwrite out_path anyway when
        # it succeeds. User-reported: "Подменил фотку, обновил страницу,
        # фото утеряно" was caused by this preemptive delete + AVAI failure.
        image_url = avai_generate(prompt, out_path, preferred_provider=effective_provider)
    except Exception as e:
        _log_event('WARN', 'regenerate_character_failed', char_id=char_id,
                   name=char.get('name', ''), err=str(e)[:300])
        return jsonify({'error': f'Не удалось сгенерировать основной образ: {e}'}), 500

    rel_path = str(out_path.relative_to(series_path(sid)))
    refs = char.setdefault('ref_images', [])
    # Replace any prior copy of this filename, then put fresh one first
    refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
    refs.insert(0, rel_path)
    char['avai_base_url'] = image_url
    # Bump updated_at so frontend URL cache-busters change → browser fetches
    # the new file instead of serving the prior generation from HTTP cache.
    # User-reported: regenerated portrait visible in lightbox but sidebar
    # miniature kept showing the old one because the URL stayed identical
    # (canonical filename never changes). All views compose `?v=<updated_at>`
    # so a fresh bump invalidates every cached copy in one shot.
    char['updated_at'] = int(time.time())

    # If any outfit was flagged is_base, point it at the new base photo too.
    for o in (char.get('outfits') or []):
        if o.get('is_base'):
            o['photo'] = rel_path
            o['avai_url'] = image_url

    save_series(sid, s)

    regenerated = []
    failed = []

    # 2) Regenerate outfits via i2i, one by one (sequential so each uses
    #    the canonical fresh base instead of fanning out and reusing stale state).
    if regen_outfits:
        new_base_url = char['avai_base_url']
        for outfit in (char.get('outfits') or []):
            if outfit.get('is_base'):
                continue  # already handled above
            try:
                out_constraints = f' IMPORTANT — strictly follow these constraints: {wishes}. ' if wishes else ''
                ref_prompt = (
                    f'Same {gender} as the reference image. '
                    f'Now wearing: {outfit["label"]}. {outfit.get("description", "")}. '
                    f'Same face, same hair, same body — only the clothing changes.'
                    f'{out_constraints}'
                    f'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
                    f'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
                    f'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
                    f'Soft even diffused illumination on the character only (no visible lights or equipment), no harsh shadows. Photorealistic, cinematic quality.'
                )
                ref_prompt = re.sub(r'\s+', ' ', ref_prompt).strip()
                # Provider-level cache buster (same fix as character base regen)
                ref_prompt = f"{ref_prompt} [v{uuid.uuid4().hex[:8]}]"
                outfit_dir = char_dir / 'outfits'
                outfit_dir.mkdir(parents=True, exist_ok=True)
                outfit_out = outfit_dir / f'{asset_name(char["name"], outfit["label"])}.jpg'
                # Remove old generated photo before regen so we don't leave orphans
                if outfit.get('photo'):
                    old_full = series_path(sid) / outfit['photo']
                    if old_full.exists() and old_full != outfit_out:
                        try: old_full.unlink()
                        except Exception: pass
                if outfit_out.exists():
                    try: outfit_out.unlink()
                    except Exception: pass
                outfit_url = avai_generate(ref_prompt, outfit_out, reference_url=new_base_url, preferred_provider=effective_provider)
                outfit['photo'] = str(outfit_out.relative_to(series_path(sid)))
                outfit['avai_url'] = outfit_url
                outfit['image_version'] = int(time.time())
                regenerated.append(outfit.get('label') or outfit['id'])
                save_series(sid, s)  # save progressively so partial work isn't lost
            except Exception as e:
                failed.append(outfit.get('label') or outfit['id'])
                app.logger.warning(f'regenerate outfit failed for {outfit.get("label")}: {e}')

    save_series(sid, s)
    return jsonify({
        'ready': True,
        'base_url': f'/assets/{sid}/{rel_path}',
        'image_url': char['avai_base_url'],
        'regenerated_outfits': regenerated,
        'failed_outfits': failed,
        'image_constraints': char['image_constraints'],
    })


@app.route('/api/series/<sid>/characters/<char_id>/save-frame/<project_id>', methods=['POST'])
def save_character_frame(sid, char_id, project_id):
    """Download first generated frame from reteller project and save as char ref."""
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404

    hdrs = rtl_headers()

    # Check project status first
    status_resp = requests.get(f'{RETELLER_API}/projects/{project_id}', headers=hdrs, timeout=15)
    if not status_resp.ok:
        return jsonify({'error': status_resp.text}), status_resp.status_code
    status_data = status_resp.json()
    if status_data.get('status') != 'completed':
        return jsonify({'ready': False, 'status': status_data.get('status')})

    # Get frames list
    frames_resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}/assets/list?types=frames',
        headers=hdrs, timeout=15
    )
    if not frames_resp.ok:
        return jsonify({'error': frames_resp.text}), 500

    frame_assets = [a for a in frames_resp.json().get('assets', []) if a['type'] == 'frames']
    if not frame_assets:
        return jsonify({'ready': False, 'status': 'no_frames'})

    # Download first frame
    img_resp = requests.get(frame_assets[0]['url'], timeout=30)
    if not img_resp.ok:
        return jsonify({'error': 'download failed'}), 500

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_name(char["name"], "BASE")}.jpg'  # e.g. CLAIRE_BASE.jpg
    (char_dir / filename).write_bytes(img_resp.content)

    rel_path = f'assets/characters/{char_slug}/{filename}'
    char.setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)

    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}'})


# ── Upload photo via drag & drop ─────────────────────────────────────────────

@app.route('/api/series/<sid>/characters/<char_id>/upload-photo', methods=['POST'])
def upload_char_photo(sid, char_id):
    s = load_series(sid)
    char = next((c for c in s.get('characters', []) if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no file'}), 400
    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    filename = f'{asset_name(char["name"], "BASE")}.{ext}'
    new_rel = f'assets/characters/{char_slug}/{filename}'
    new_full = char_dir / filename
    # Replace semantics: when user uploads their own photo, drop ALL prior
    # base refs + delete the on-disk files (not just append). Old photos
    # were accumulating in the gallery — user complained it's confusing.
    # Skip deletion of files that share the new path (overwrite case).
    base = series_path(sid)
    for old_rel in (char.get('ref_images') or []):
        if old_rel == new_rel:
            continue
        try:
            (base / old_rel).unlink(missing_ok=True)
        except Exception:
            pass
    new_full.write_bytes(f.read())
    char['ref_images'] = [new_rel]
    char['avai_base_url'] = ''  # invalidate cached AVAI URL — was for the old auto-gen
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{new_rel}', 'series': s})


@app.route('/api/series/<sid>/locations/<loc_id>/upload-photo', methods=['POST'])
def upload_loc_photo(sid, loc_id):
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no file'}), 400
    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    loc_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    filename = f'{asset_name(loc["name"])}.{ext}'
    new_rel = f'assets/locations/{loc_slug}/{filename}'
    new_full = loc_dir / filename
    base = series_path(sid)
    for old_rel in (loc.get('ref_images') or []):
        if old_rel == new_rel:
            continue
        try:
            (base / old_rel).unlink(missing_ok=True)
        except Exception:
            pass
    new_full.write_bytes(f.read())
    loc['ref_images'] = [new_rel]
    loc['avai_url'] = ''  # invalidate cached AVAI URL
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{new_rel}', 'series': s})


# ── Open folder in Finder ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/open-folder', methods=['POST'])
def open_folder(sid):
    folder_type = (request.json or {}).get('type', 'assets')
    base = assets_dir(sid)
    paths = {
        'characters': base / 'characters',
        'locations':  base / 'locations',
        'items':      base / 'items',
        'assets':     base,
    }
    folder = paths.get(folder_type, base)
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(['open', str(folder)])
    return jsonify({'ok': True, 'path': str(folder)})


# ── Location image generation ────────────────────────────────────────────────

@app.route('/api/series/<sid>/locations/<loc_id>/generate-image', methods=['POST'])
def generate_location_image(sid, loc_id):
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'not found'}), 404

    tone = s.get('tone', '')
    style_clause = _series_style_clause(s)
    constraints = (loc.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    loc_name = _strip_cast_names_for_visual(loc['name'], s)
    loc_desc = _strip_cast_names_for_visual(loc.get('description', ''), s)
    prompt = (
        f"{loc_name}. {loc_desc}.{constraints_clause} "
        f"{_location_crowd_clause(loc)}"
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing.{_no_caption_text_clause(constraints)}"
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    out_path = loc_dir / f'{asset_name(loc["name"])}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, aspect_ratio='16:9', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = loc.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        loc['avai_url'] = image_url  # used by Seedance for video refs
        loc['image_version'] = int(time.time())  # cache-bust marker for UI
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}',
                        'image_url': image_url, 'image_version': loc['image_version']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/locations/<loc_id>/regenerate', methods=['POST'])
def regenerate_location(sid, loc_id):
    """Regenerate the location's establishing shot with user-supplied constraints.
    Persists constraints on the location."""
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'location not found'}), 404
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    loc['image_constraints'] = wishes
    tone = s.get('tone', '')
    style_clause = _series_style_clause(s)
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    loc_name = _strip_cast_names_for_visual(loc['name'], s)
    loc_desc = _strip_cast_names_for_visual(loc.get('description', ''), s)
    prompt = (
        f"{loc_name}. {loc_desc}.{constraints_clause} "
        f"{_location_crowd_clause(loc)}"
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing.{_no_caption_text_clause(wishes)}"
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    loc_dir.mkdir(parents=True, exist_ok=True)
    out_path = loc_dir / f'{asset_name(loc["name"])}.jpg'
    try:
        if out_path.exists():
            try: out_path.unlink()
            except Exception: pass
        image_url = avai_generate(prompt, out_path, aspect_ratio='16:9', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = loc.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        loc['avai_url'] = image_url
        loc['image_version'] = int(time.time())
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}',
                        'image_url': image_url, 'image_version': loc['image_version']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Series cover art (short-drama poster) ───────────────────────────────────
# One JPEG at assets/cover.jpg, used as the background of the project card on
# the main menu and openable in a viewer modal with Regenerate / Download.
# Banana ('pro' model) accepts up to 2 contextImages → we pull the top 2
# characters with portraits as visual refs so the leads look on-model.

def _cover_lead_refs(s, sid):
    """Pick up to 2 main characters' AVAI portrait URLs to use as references.
    Order: characters that already have an `avai_base_url` (portrait was
    generated via AVAI), then characters with local `ref_images` (uploaded)
    which we upload to AVAI on the fly. Skips characters with no portrait."""
    refs = []
    leads = []
    for c in (s.get('characters') or []):
        if c.get('avai_base_url'):
            refs.append(c['avai_base_url'])
            leads.append(c)
            if len(refs) >= 2:
                return refs, leads
    # Fallback: upload first local ref image for any remaining slots.
    for c in (s.get('characters') or []):
        if c in leads:
            continue
        rels = c.get('ref_images') or []
        if not rels:
            continue
        local = series_path(sid) / rels[0]
        if not local.exists():
            continue
        try:
            url = _avai_upload_local_image(local)
            refs.append(url)
            leads.append(c)
            # Cache it on the character so we don't re-upload next time.
            c['avai_base_url'] = url
        except Exception as e:
            print(f'[cover] upload ref for {c.get("name")} failed: {e}', flush=True)
            continue
        if len(refs) >= 2:
            break
    return refs, leads


# Fields whose change should invalidate a cached cover art-direction brief.
def _cover_art_direction_source(s):
    """Signature of the bible fields the cover art-direction depends on.
    When any of them change, the cached brief is re-derived so the cover keeps
    tracking the story."""
    parts = [
        (s.get('title') or '').strip(),
        (s.get('synopsis') or '').strip(),
        (s.get('world_description') or '').strip(),
        (s.get('genre') or '').strip(),
        (s.get('tone') or '').strip(),
        (s.get('era_choice') or '').strip(),
        '1' if s.get('era_confirmed') else '0',
        _series_era_hint(s),
        _series_visual_style(s),
    ]
    return hashlib.sha1('␟'.join(parts).encode('utf-8')).hexdigest()


# What a well-formed art-direction brief must contain. Each value is a short
# concrete art-direction phrase, NOT prose — it gets dropped verbatim into the
# image prompt.
_COVER_AD_KEYS = ('palette', 'lighting', 'composition', 'typography',
                  'atmosphere', 'background')


def _series_cover_art_direction(s, force=False):
    """Per-series cover art-direction brief, tailored to the story's atmosphere.

    The old cover prompt baked ONE recipe (teal/magenta palette, look-into-
    camera close-up, white drop-shadow title) into every poster, so covers
    came out indistinguishable and ignored genre/era/mood. This asks Claude to
    design a bespoke brief — palette, lighting, composition archetype,
    genre-matched TYPOGRAPHY, atmosphere and background — from the bible.

    Cached on series.json keyed on a signature of the bible fields it depends
    on (`cover_art_direction_source`); re-derived only when those change or
    when `force=True`. Returns a dict with `_COVER_AD_KEYS`, or {} on failure
    (caller falls back to a generic clause)."""
    sig = _cover_art_direction_source(s)
    cached = s.get('cover_art_direction')
    if (not force and isinstance(cached, dict)
            and s.get('cover_art_direction_source') == sig
            and all(cached.get(k) for k in _COVER_AD_KEYS)):
        return cached

    title = (s.get('title') or '').strip() or 'Untitled'
    synopsis = (s.get('synopsis') or '').strip()[:900]
    world = (s.get('world_description') or '').strip()[:400]
    genre = (s.get('genre') or '').strip()
    tone = (s.get('tone') or '').strip()
    era_hint = _series_era_hint(s)
    visual_style = _series_visual_style(s)

    ctx = [f'TITLE: {title}']
    if genre:        ctx.append(f'GENRE: {genre}')
    if tone:         ctx.append(f'TONE: {tone}')
    if synopsis:     ctx.append(f'SYNOPSIS: {synopsis}')
    if world:        ctx.append(f'WORLD: {world}')
    if era_hint:     ctx.append(f'PERIOD/ERA: {era_hint}')
    if visual_style: ctx.append(f'VISUAL STYLE: {visual_style}')
    ctx_block = '\n'.join(ctx)

    system = (
        'You are an award-winning key-art director for short-form vertical '
        'mobile drama series (ReelShort / DramaBox). You design the cover '
        'poster that makes THIS specific story unmistakable at a glance. '
        'Every series you brief must look DISTINCT from every other — never '
        'fall back on a generic template. In particular DO NOT default to the '
        'overused teal-and-magenta-with-gold-accents palette, the generic '
        '"two leads staring into camera" close-up, or plain white drop-shadow '
        'lettering unless the story genuinely calls for exactly that. Match '
        'the palette, lighting, composition, TYPOGRAPHY and mood to the '
        "story's genre, era and emotional core. Typography especially must "
        'fit the genre — e.g. elegant high-contrast serif for period '
        'romance, distressed condensed sans for revenge thrillers, ornate '
        'gilded blackletter for historical/royal sagas, sleek neon/chrome for '
        'sci-fi, warm rounded script for family melodrama, hand-painted '
        'brush for wuxia/eastern. Be concrete and specific.'
    )
    prompt = (
        f'{ctx_block}\n\n'
        'Design the cover-poster art direction for this series. Respond with '
        'STRICT JSON only (no markdown, no commentary) with EXACTLY these '
        'keys, each a single concrete art-direction phrase (12-30 words), '
        'written to be dropped directly into an image-generation prompt:\n'
        '{\n'
        '  "palette": "specific colors + relationships that fit this story\'s '
        'mood (name actual hues, not just \'warm\'); avoid the generic '
        'teal/magenta/gold default unless truly fitting",\n'
        '  "lighting": "lighting setup + color grade that sells the genre and '
        'era (key direction, contrast, practical sources, grade)",\n'
        '  "composition": "the hero staging / poster archetype for this story '
        '(not necessarily a centered look-into-camera close-up) — framing, '
        'where leads sit, what tension it conveys",\n'
        '  "typography": "title lettering style that matches the genre/era — '
        'typeface character (serif/sans/script/blackletter/etc.), weight, '
        'treatment (foil, distress, glow, engraved), color and placement",\n'
        '  "atmosphere": "overall emotional mood + texture/film-grain/'
        'weather/particle cues that set the tone",\n'
        '  "background": "what the evocative background depicts — the '
        'world/setting hint behind the leads"\n'
        '}'
    )
    try:
        raw = claude_ask_quality(prompt, system=system)
        brief = loads_lenient(raw)
        if not isinstance(brief, dict):
            raise ValueError('brief is not an object')
        out = {k: (str(brief.get(k) or '').strip()) for k in _COVER_AD_KEYS}
        if not all(out.values()):
            raise ValueError('brief missing keys: '
                             + ','.join(k for k in _COVER_AD_KEYS if not out[k]))
        s['cover_art_direction'] = out
        s['cover_art_direction_source'] = sig
        return out
    except Exception as e:
        print(f'[cover/art-direction] failed: {e}', flush=True)
        return {}


def _build_cover_prompt(s, leads):
    """Compose a short-drama poster prompt from the series bible + lead
    characters. The visual recipe (palette / lighting / composition /
    typography / atmosphere) comes from a per-series art-direction brief so
    every cover tracks its own story instead of sharing one template."""
    title = (s.get('title') or '').strip() or 'Untitled'
    synopsis = (s.get('synopsis') or '').strip()
    world = (s.get('world_description') or '').strip()
    genre = (s.get('genre') or '').strip()
    tone = (s.get('tone') or '').strip()
    style_clause = _series_style_clause(s)
    era_hint = _series_era_hint(s)
    ad = _series_cover_art_direction(s)

    # Per-character one-liner: «Name — appearance (short)»
    lead_lines = []
    for c in leads:
        name = (c.get('name') or '').strip()
        app = (c.get('appearance') or c.get('description') or '').strip()
        # Trim long appearance to keep the prompt focused.
        if len(app) > 220:
            app = app[:217].rstrip() + '...'
        if name and app:
            lead_lines.append(f'{name} — {app}')
        elif name:
            lead_lines.append(name)
    leads_clause = ''
    if lead_lines:
        leads_clause = (
            'HERO CAST — feature these lead character(s) (match the '
            'reference images for face / hair / build): '
            + '; '.join(lead_lines)
            + '. '
        )

    syn_short = synopsis[:400].strip()
    world_short = world[:200].strip()
    story_clause = ''
    if syn_short:
        story_clause = f'STORY VIBE: {syn_short} '
    if world_short:
        story_clause += f'World: {world_short}. '

    genre_clause = ''
    bits = [b for b in (genre, tone) if b]
    if bits:
        genre_clause = f'Genre/mood: {" / ".join(bits)}. '

    safe_title = title.replace('"', '\\"')

    if ad:
        # Bespoke art direction drives palette / light / comp / type / mood.
        typography = ad['typography']
        art_block = (
            f'TITLE — render the words "{safe_title}" as the main title '
            f'lettering. TYPOGRAPHY (match exactly): {typography} '
            f'Title must be perfectly legible, correctly spelled, no typos, '
            f'no extra words. '
            f'{leads_clause}'
            f'{story_clause}'
            f'{genre_clause}'
            f'COMPOSITION: {ad["composition"]} '
            f'COLOR PALETTE: {ad["palette"]} '
            f'LIGHTING & GRADE: {ad["lighting"]} '
            f'ATMOSPHERE: {ad["atmosphere"]} '
            f'BACKGROUND: {ad["background"]} '
        )
    else:
        # Fallback when the LLM brief is unavailable — still better than the
        # old fixed teal/magenta recipe by leaning on genre/tone text.
        art_block = (
            f'TITLE — render the words "{safe_title}" as bold large display '
            f'typography, styled to fit the genre/mood above, perfectly '
            f'legible, no typos, no extra words. '
            f'{leads_clause}'
            f'{story_clause}'
            f'{genre_clause}'
            f'Composition: leads staged with intense emotion, dramatic '
            f'cinematic key-light, high contrast, shallow depth of field, '
            f'a palette and lighting that match the story\'s genre and mood, '
            f'evocative background hinting at the world of the story. '
        )

    era_clause = f'{era_hint} ' if era_hint else ''

    prompt = (
        f'Vertical 3:4 key-art poster for a short-form mobile drama series '
        f'(ReelShort / DramaBox style), cinematic and emotional. '
        f'{art_block}'
        f'{era_clause}'
        f'No watermarks, no captions other than the title, no episode '
        f'numbers, no UI elements, no frame borders. '
        f'{style_clause}'
    )
    return re.sub(r'\s+', ' ', prompt).strip()


def _translate_synopsis_to_en(text: str) -> str:
    """Translate a synopsis blurb to English via Claude Haiku. Idempotent on
    text already in English (Claude returns it unchanged)."""
    text = (text or '').strip()
    if not text:
        return ''
    try:
        out = claude_ask_fast(
            f'Translate the following short series logline / synopsis to natural English. '
            f'Return ONLY the translation, no quotes, no preface, no labels. If the text '
            f'is already in English, return it unchanged.\n\n{text}',
            system='You are a professional translator for short-form drama loglines. Preserve tone and meaning, output English prose only.'
        )
        return (out or '').strip().strip('"').strip()
    except Exception as e:
        print(f'[cover/translate] failed: {e}', flush=True)
        return ''


@app.route('/api/series/<sid>/cover/synopsis-en', methods=['GET'])
def get_series_synopsis_en(sid):
    """Returns the series' synopsis + world_description + genre/tone/audience
    chips in English. Caches every translation on series.json with a
    `<field>_en_source` companion so we re-translate only when the source
    text changes."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    fields = ('synopsis', 'world_description', 'genre')
    changed = False
    # If genre is blank, infer one short English label from the synopsis so the
    # viewer has something to show. Bible-author can still override manually.
    if not (s.get('genre') or '').strip() and (s.get('synopsis') or '').strip():
        try:
            inferred = claude_ask_fast(
                'Read this short-drama series synopsis and reply with ONE short '
                'English genre label (2-4 words, e.g. "Cinderella revenge", '
                '"Family melodrama", "Billionaire romance", "Revenge thriller"). '
                'Reply with only the label, no quotes, no period.\n\n'
                + (s.get('synopsis') or '').strip()[:1200],
                system='You classify short-form mobile drama series into concise English genre labels.'
            )
            inferred = (inferred or '').strip().strip('"').strip().split('\n')[0][:60]
            if inferred:
                s['genre'] = inferred
                s['genre_en'] = inferred
                s['genre_en_source'] = inferred
                changed = True
        except Exception as e:
            print(f'[cover/genre-infer] failed: {e}', flush=True)
    for f in fields:
        src = (s.get(f) or '').strip()
        en_key = f + '_en'
        src_key = en_key + '_source'
        if src and (s.get(src_key) != src or not s.get(en_key)):
            en = _translate_synopsis_to_en(src)
            if en:
                s[en_key] = en
                s[src_key] = src
                changed = True
    if changed:
        save_series(sid, s)
    # Return both source + EN so the frontend can refresh stale chips after
    # server-side genre inference, not just the translations.
    out = {f + '_en': s.get(f + '_en') or '' for f in fields}
    out.update({f: s.get(f) or '' for f in fields})
    return jsonify(out)


@app.route('/api/series/<sid>/cover/generate', methods=['POST'])
def generate_series_cover(sid):
    """Generate (or regenerate) the series cover poster. 3:4 JPEG 1K.
    Body (optional): {
        wishes: 'extra art direction from the user',
        new_art_direction: bool  # force a fresh per-series art-direction brief
                                  # instead of reusing the cached one
    }
    Persists rel path to series.cover_image + bumps cover_image_version."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    _nd_anthro, _fl_anthro = _anthro_preflight(s, s.get('characters') or [])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()

    # Re-derive the art-direction brief on demand (user wants a different look).
    if body.get('new_art_direction'):
        _series_cover_art_direction(s, force=True)

    refs, leads = _cover_lead_refs(s, sid)
    prompt = _build_cover_prompt(s, leads)
    if wishes:
        prompt += f' Additional art direction from the user (follow strictly): {wishes}.'

    out_path = assets_dir(sid) / 'cover.jpg'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Pass refs as a list — _avai_call handles list-or-str.
        image_url = avai_generate(
            prompt, out_path,
            reference_url=refs if refs else None,
            aspect_ratio='3:4',
            preferred_provider=_series_image_provider(s),
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    rel_path = str(out_path.relative_to(series_path(sid)))
    s['cover_image'] = rel_path
    s['cover_image_url'] = image_url
    s['cover_image_version'] = int(time.time())
    save_series(sid, s)
    return jsonify({
        'ready': True,
        'url': f'/assets/{sid}/{rel_path}',
        'image_url': image_url,
        'image_version': s['cover_image_version'],
        'used_refs': len(refs),
    })


# ── Building facades ────────────────────────────────────────────────────────
# Per-series feature: cluster interior locations into their parent building
# (e.g. «Marcus's office» + «Marcus's bedroom» → «Bellacourt Mansion»), then
# generate ONE exterior facade image + a short Seedance video per building.
# Used as «open every new venue with a 4s facade shot» before the dialogue
# starts inside. Storage: assets/facades/<facade_id>/{facade.jpg, facade.mp4}.
# series.location_facades[] persists the linkage back to series.locations.

_FACADE_STATUS = {}   # sid → status dict

def _facade_status(sid):
    return _FACADE_STATUS.setdefault(sid, {
        'running': False, 'total': 0, 'done': 0, 'errors': [], 'current': None,
    })


def _claude_group_locations(locs, existing_building_names=None):
    """Cluster the given locations into building groups via Claude. Reused
    by the manual `/facades/group` endpoint and by the post-accept auto
    sweep. `existing_building_names` (optional) is passed as context so
    Claude prefers reusing an existing facade's name when a new location
    belongs to a building already in the library — keeps grouping stable
    across multiple script-accept cycles.

    Returns: list of dicts with keys
      building_name, type ('building'|'exterior'), facade_description,
      member_loc_ids, member_names.
    """
    locs_block = '\n'.join(
        f'- id={l["id"]} | "{l["name"]}" — {(l.get("description") or "")[:200]}'
        for l in locs
    )
    existing_block = ''
    if existing_building_names:
        existing_block = (
            "\nУЖЕ СУЩЕСТВУЮЩИЕ ЗДАНИЯ В ЭТОМ СЕРИАЛЕ (если новая локация "
            "принадлежит одному из них — используй его имя ДОСЛОВНО):\n"
            + '\n'.join(f'  • {n}' for n in existing_building_names) + '\n'
        )
    sys = (
        "Ты — продюсер визуальной библиотеки сериала. Тебе дают список локаций. "
        "Сгруппируй их по ЗДАНИЯМ-ОБЛАДАТЕЛЯМ. Цель: для каждого здания сгенерируем "
        "ОДИН фасадный плановый кадр снаружи, который будет показывать «вот это место» "
        "перед интерьерными сценами внутри.\n\n"
        "ПРАВИЛА:\n"
        "1. Локации-интерьеры одного и того же здания группируй вместе. Примеры:\n"
        "   • «Marcus's office» + «Marcus's bedroom» + «Marcus's wine cellar» → одно здание «Bellacourt Mansion»\n"
        "   • «Hospital reception» + «ICU ward» + «Hospital cafeteria» → одно «City Hospital»\n"
        "   • «Elena's motel room» + «Motel hallway» → одно «Roadside Motel»\n"
        "2. Самодостаточные exterior-локации (улица, виноградник, парк, набережная, лес) — каждая "
        "СВОЯ группа из 1 элемента с type='exterior'. У них уже есть наружный кадр в ref-картинках, "
        "отдельный facade не нужен.\n"
        "3. Если для двух локаций по описанию НЕ ясно одно ли это здание — держи их отдельно. "
        "Лучше лишний фасад, чем неправильное склеивание.\n"
        "4. Имя здания должно быть КОНКРЕТНЫМ (имя владельца / название учреждения / города), "
        "не «building» / «structure» / «place». «Bellacourt Mansion», не «Mansion».\n"
        "5. type='building' для зданий нуждающихся в фасадной генерации, type='exterior' для уже-наружных.\n"
        f"{existing_block}"
        f"\nЛОКАЦИИ СЕРИАЛА:\n{locs_block}\n\n"
        "Верни JSON и ТОЛЬКО JSON:\n"
        '{\n'
        '  "groups": [\n'
        '    {\n'
        '      "building_name": "Bellacourt Mansion",\n'
        '      "type": "building",\n'
        '      "facade_description": "Three-storey stone mansion covered in vines, ornate front entrance with double oak doors, gravel driveway, daylight",\n'
        '      "member_loc_ids": ["id1", "id2"]\n'
        '    }\n'
        '  ]\n'
        '}\n'
    )
    raw = claude_ask("Сгруппируй и верни JSON.", system=sys, max_tokens=4000)
    data = json.loads(strip_json(raw))
    groups = data.get('groups') or []
    known_ids = {l['id'] for l in locs}
    name_by_id = {l['id']: l['name'] for l in locs}
    for g in groups:
        g['member_loc_ids'] = [i for i in (g.get('member_loc_ids') or []) if i in known_ids]
        g['member_names']   = [name_by_id[i] for i in g['member_loc_ids']]
    return groups


@app.route('/api/series/<sid>/facades/group', methods=['POST'])
def facades_group(sid):
    """Have Claude cluster the series' locations into parent buildings.
    Returns preview groupings — frontend lets the user accept / edit before
    kicking off generation. Pure read; no side effects."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    locs = [l for l in (s.get('locations') or []) if l.get('name')]
    if not locs:
        return jsonify({'error': 'У сериала нет локаций для группировки'}), 400
    try:
        groups = _claude_group_locations(locs)
        return jsonify({'groups': groups})
    except Exception as e:
        return jsonify({'error': f'Group failed: {e}'}), 500


def _facade_worker(sid, groups):
    """Background: for each `building` group, generate facade image then a
    short Seedance video. Saves to assets/facades/<facade_id>/; updates
    series.location_facades[] incrementally so the UI sees progress."""
    st = _facade_status(sid)
    work_groups = [g for g in groups
                   if g.get('type') == 'building'
                   and (g.get('member_loc_ids') or g.get('member_names'))]
    st.update({
        'running': True, 'total': len(work_groups), 'done': 0,
        'errors': [], 'started_at': datetime.datetime.utcnow().isoformat(),
        'finished_at': None, 'current': None,
    })
    try:
        s0 = load_series(sid)
        if not s0:
            st['errors'].append({'error': 'series gone'})
            return
        # Ensure the facade list exists
        s0.setdefault('location_facades', [])
        save_series(sid, s0)
        for g in work_groups:
            name = (g.get('building_name') or '').strip() or 'Unnamed Building'
            desc = (g.get('facade_description') or '').strip()
            st['current'] = name
            fid = 'fac_' + str(uuid.uuid4())[:8]
            fac_dir = facades_dir(sid) / fid
            fac_dir.mkdir(parents=True, exist_ok=True)
            facade = {
                'id': fid,
                'building_name': name,
                'facade_description': desc,
                'member_loc_ids': list(g.get('member_loc_ids') or []),
                'image_path': '',
                'image_avai_url': '',
                'video_path': '',
                'video_avai_url': '',
                'status': 'generating',
                'created_at': datetime.datetime.utcnow().isoformat(),
            }
            # Initial save so UI sees the in-progress card
            s = load_series(sid)
            s.setdefault('location_facades', []).append(facade)
            save_series(sid, s)

            # ── Image generation ──────────────────────────────────────────
            tone = s.get('tone', '')
            style_clause = _series_style_clause(s)
            fac_name = _strip_cast_names_for_visual(name, s)
            fac_desc = _strip_cast_names_for_visual(desc, s)
            img_prompt = (
                f"Exterior facade of {fac_name}. {fac_desc}. "
                f"{_location_crowd_clause(None)}"
                f"{(tone + ' atmosphere. ') if tone else ''}"
                f"Cinematic wide establishing shot of the building exterior. "
                f"Vertical 9:16 framing for short-drama.{_no_caption_text_clause()}{style_clause}"
            )
            img_prompt = re.sub(r'\s+', ' ', img_prompt).strip()
            img_path = fac_dir / 'facade.jpg'
            img_url = ''
            try:
                img_url = avai_generate(
                    img_prompt, img_path, aspect_ratio='9:16',
                    preferred_provider=_series_image_provider(s),
                )
                facade['image_avai_url'] = img_url
                facade['image_path'] = str(img_path.relative_to(series_path(sid)))
            except Exception as e:
                facade['status'] = 'failed'
                facade['error'] = f'image: {str(e)[:200]}'
                st['errors'].append({'facade_id': fid, 'building': name, 'error': str(e)[:200]})
                _merge_facade(sid, fid, facade)
                continue
            _merge_facade(sid, fid, facade)

            # ── Video generation (4-5 second static establishing) ─────────
            try:
                vid_prompt = (
                    f"Static cinematic establishing wide shot of {name} exterior. {desc}. "
                    "No people, no characters. Subtle ambient motion only — drifting clouds, "
                    "swaying foliage, very slow camera push-in. "
                    "Ambient location sounds only — wind, distant traffic, birds, rustling foliage, "
                    "rain or city hum depending on setting. No speech, no music, no dialogue. "
                    "Cinematic, 9:16."
                )
                vid_prompt = re.sub(r'\s+', ' ', vid_prompt).strip()
                avai_key = _get_user_avai_key()
                # Generate ambient audio for the establishing shot — gives the
                # facade clip atmosphere (wind / city / rain depending on
                # location) instead of dead silence before the next chunk
                # begins. Assembly already handles mixed-audio sources
                # (auto-asssemble probes per-input audio presence) so this is
                # back-compat with older facades rendered silent.
                job = _avai_seedance_start(
                    prompt=vid_prompt,
                    ref_urls=[img_url] if img_url else [],
                    duration=5,    # Seedance min 5s; covers the 4s establishing
                    resolution='720p',
                    moderation_bypass='off',
                    aspect_ratio='9:16',
                    generate_audio=True,
                    avai_key=avai_key,
                )
                vid_path_local = fac_dir / 'facade.mp4'
                vid_url = ''
                for _ in range(180):   # 12-min ceiling
                    time.sleep(4)
                    pst = _avai_seedance_status(job['job_id'], status_url=job['status_url'], avai_key=avai_key)
                    if pst.get('status') == 'completed':
                        vid_url = pst.get('video_url', '')
                        break
                    if pst.get('status') in ('failed', 'error'):
                        raise RuntimeError(pst.get('error') or 'seedance failed')
                if not vid_url:
                    raise RuntimeError('seedance timeout')
                r = requests.get(vid_url, timeout=120, stream=True)
                r.raise_for_status()
                with open(vid_path_local, 'wb') as fp:
                    for chk in r.iter_content(1 << 16):
                        fp.write(chk)
                facade['video_avai_url'] = vid_url
                facade['video_path'] = str(vid_path_local.relative_to(series_path(sid)))
                facade['status'] = 'ready'
            except Exception as e:
                facade['status'] = 'image_only'
                facade['error'] = f'video: {str(e)[:200]}'
                st['errors'].append({'facade_id': fid, 'building': name, 'error': f'video: {str(e)[:200]}'})
            _merge_facade(sid, fid, facade)
            st['done'] += 1
    finally:
        st['running'] = False
        st['finished_at'] = datetime.datetime.utcnow().isoformat()
        st['current'] = None


def _merge_facade(sid, fid, facade):
    """Re-read series, update the facade entry by id, save. Avoids clobbering
    concurrent writes from other endpoints."""
    s = load_series(sid)
    if not s: return
    facs = s.setdefault('location_facades', [])
    found = False
    for i, f in enumerate(facs):
        if f.get('id') == fid:
            facs[i] = {**f, **facade}
            found = True
            break
    if not found:
        facs.append(facade)
    save_series(sid, s)


@app.route('/api/series/<sid>/facades/generate', methods=['POST'])
def facades_generate(sid):
    """Body: { groups: [...] } where groups is the Claude-grouped list (or
    user-edited variant). Returns immediately, worker writes facades back
    incrementally; poll /facades for state."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    groups = body.get('groups') or []
    if not groups:
        return jsonify({'error': 'no groups provided'}), 400
    st = _facade_status(sid)
    if st.get('running'):
        return jsonify({'error': 'generation already running'}), 409
    _spawn_with_keys(_facade_worker, sid, groups)
    work_count = len([g for g in groups if g.get('type') == 'building'])
    return jsonify({'started': True, 'count': work_count})


def auto_facades_for_new_locations(sid):
    """Idempotent post-sweep hook: find locations that have a generated
    interior but aren't yet a member of any existing facade group, ask
    Claude to cluster them (using existing building names as anchors so
    similar locs reuse an existing facade), then either MERGE into the
    existing facade by name or spawn `_facade_worker` for genuinely new
    buildings.

    Called from the tail of `auto_generate_missing_assets` so facade gen
    fires automatically after interiors finish. Safe to call repeatedly —
    on a second invocation with no new locations it's a no-op.

    Honors:
      • `series.auto_facades = False`  → skip (explicit user opt-out)
      • `location._skip_autogen = True` → loc never participates
      • `_facade_status(sid).running`   → don't double-run
    """
    s = load_series(sid)
    if not s:
        return
    if s.get('auto_facades') is False:
        return  # explicit opt-out at series level
    if s.get('auto_generate_assets') is False:
        return  # broader opt-out — user disabled autogen entirely
    st = _facade_status(sid)
    if st.get('running'):
        print(f'[auto-facades {sid}] worker already running — skipping', flush=True)
        return

    existing_facades = s.get('location_facades') or []
    existing_member_ids = {
        mid for f in existing_facades for mid in (f.get('member_loc_ids') or [])
    }
    existing_name_to_facade = {
        (f.get('building_name') or '').strip().lower(): f
        for f in existing_facades if f.get('building_name')
    }
    all_locs = s.get('locations') or []
    new_locs = [
        l for l in all_locs
        if l.get('id') and l['id'] not in existing_member_ids
        and not l.get('_skip_autogen')
        and (l.get('ref_images') or [])   # interior already rendered
    ]
    if not new_locs:
        return  # nothing to do — fully idempotent

    # Mark `running` during the Claude-grouping phase too — otherwise the
    # facades modal renders «ещё нет сгенерированных фасадов» for the 3-5s
    # while Claude clusters, then suddenly flips to «генерирую» when
    # _facade_worker takes over. Setting it here gives the UI a continuous
    # signal that something IS happening. Reset to False in the no-work-
    # found branches below.
    st.update({'running': True, 'phase': 'grouping', 'current': 'Группирую локации…',
               'total': 0, 'done': 0, 'errors': []})
    print(f'[auto-facades {sid}] {len(new_locs)} new location(s) need facade — grouping…', flush=True)
    try:
        existing_names = sorted({f.get('building_name', '').strip() for f in existing_facades if f.get('building_name')})
        groups = _claude_group_locations(new_locs, existing_building_names=existing_names)
    except Exception as e:
        print(f'[auto-facades {sid}] grouping failed: {e}', flush=True)
        _log_event('WARN', 'auto_facades_group_failed', sid=sid, err=str(e)[:200])
        st.update({'running': False, 'phase': None, 'current': None,
                   'errors': [{'error': f'grouping failed: {str(e)[:200]}'}]})
        return

    # Split: groups whose building_name matches an existing facade → merge.
    # Groups with a new building_name → queue for _facade_worker.
    work_groups = []
    s_disk = load_series(sid)
    if not s_disk:
        return
    s_disk.setdefault('location_facades', [])
    merges = 0
    for g in groups:
        if g.get('type') != 'building':
            continue  # exteriors don't need facade gen
        bname = (g.get('building_name') or '').strip()
        if not bname:
            continue
        new_mids = [m for m in (g.get('member_loc_ids') or []) if m not in existing_member_ids]
        if not new_mids:
            continue
        existing = existing_name_to_facade.get(bname.lower())
        if existing:
            # Merge: extend the existing facade's member list. No re-render.
            for f in s_disk.get('location_facades', []):
                if f.get('id') == existing.get('id'):
                    f.setdefault('member_loc_ids', [])
                    for mid in new_mids:
                        if mid not in f['member_loc_ids']:
                            f['member_loc_ids'].append(mid)
                    merges += 1
                    break
        else:
            work_groups.append({
                'building_name': bname,
                'type': 'building',
                'facade_description': (g.get('facade_description') or '').strip(),
                'member_loc_ids': new_mids,
            })
    if merges:
        save_series(sid, s_disk)
        print(f'[auto-facades {sid}] merged into {merges} existing facade(s)', flush=True)

    if work_groups:
        print(f'[auto-facades {sid}] kicking off facade gen for {len(work_groups)} new building(s): '
              + ', '.join(g["building_name"] for g in work_groups), flush=True)
        # _facade_worker re-sets status.running with its own total/done counters,
        # so the grouping-phase flag we set above is transparently superseded.
        _spawn_with_keys(_facade_worker, sid, work_groups)
    else:
        # Pure-merge path or grouping yielded only exteriors — no renders to
        # do. Clear the grouping-phase flag we set at the top so the UI stops
        # showing «Группирую…».
        st.update({'running': False, 'phase': None, 'current': None})


@app.route('/api/series/<sid>/facades', methods=['GET'])
def facades_list(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    # Belt-and-suspenders auto-trigger: if the user is looking at the facades
    # panel and there are interior locations that aren't a member of any
    # facade group yet, kick off `auto_facades_for_new_locations` in the
    # background. The function is fully idempotent (running-flag check +
    # no-op when nothing new), so polling this endpoint every 6s while the
    # modal is open is safe. Catches series whose autogen sweep ended before
    # the facade auto-trigger was wired up — and any future case where the
    # sweep-tail trigger failed silently.
    if s.get('auto_facades') is not False:
        existing_facades = s.get('location_facades') or []
        member_ids = {mid for f in existing_facades for mid in (f.get('member_loc_ids') or [])}
        has_ungrouped = any(
            l.get('id') and l['id'] not in member_ids
            and not l.get('_skip_autogen')
            and (l.get('ref_images') or [])
            for l in (s.get('locations') or [])
        )
        if has_ungrouped and not _facade_status(sid).get('running'):
            try:
                _spawn_with_keys(auto_facades_for_new_locations, sid)
                # Surface an «about to start» hint immediately so the first
                # response (before the worker thread has ticked) tells the UI
                # to poll. Worker itself overwrites status.running with the
                # full grouping/rendering state within milliseconds. We use
                # a hint flag instead of running=True to avoid tripping the
                # worker's «already running — skip» guard.
                _facade_status(sid)['_pending_autostart'] = True
            except Exception as e:
                print(f'[facades_list {sid}] auto-trigger spawn failed: {e}', flush=True)
    live = _facade_status(sid)
    pending = bool(live.pop('_pending_autostart', False))
    st_out = dict(live)
    if pending and not st_out.get('running'):
        # Pre-populate the running flag for THIS response so the frontend's
        # facadesRefresh() sees running=true and starts polling. Subsequent
        # polls read the worker's real state.
        st_out.update({'running': True, 'phase': 'grouping',
                       'current': 'Группирую локации…',
                       'total': 0, 'done': 0, 'errors': []})
    return jsonify({
        'facades': s.get('location_facades') or [],
        'status': st_out,
        'folder': str(facades_dir(sid).resolve()),
    })


@app.route('/api/series/<sid>/facades/<fid>', methods=['DELETE'])
def facades_delete(sid, fid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['location_facades'] = [f for f in (s.get('location_facades') or []) if f.get('id') != fid]
    save_series(sid, s)
    d = facades_dir(sid) / fid
    if d.exists():
        try: shutil.rmtree(d)
        except Exception: pass
    return jsonify({'ok': True})


@app.route('/api/series/<sid>/facades/<fid>/regenerate', methods=['POST'])
def facades_regenerate(sid, fid):
    """Re-run image+video for one facade. Optional body overrides {building_name,
    facade_description, member_loc_ids}."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    fac = next((f for f in (s.get('location_facades') or []) if f.get('id') == fid), None)
    if not fac:
        return jsonify({'error': 'facade not found'}), 404
    body = request.json or {}
    name = (body.get('building_name') or fac.get('building_name') or '').strip()
    desc = (body.get('facade_description') or fac.get('facade_description') or '').strip()
    members = body.get('member_loc_ids') if 'member_loc_ids' in body else fac.get('member_loc_ids')
    # Wipe the existing entry so worker creates a fresh one with a new fid
    s['location_facades'] = [f for f in s['location_facades'] if f.get('id') != fid]
    save_series(sid, s)
    d = facades_dir(sid) / fid
    if d.exists():
        try: shutil.rmtree(d)
        except Exception: pass
    st = _facade_status(sid)
    if st.get('running'):
        return jsonify({'error': 'generation already running'}), 409
    group = {
        'building_name': name,
        'type': 'building',
        'facade_description': desc,
        'member_loc_ids': list(members or []),
    }
    _spawn_with_keys(_facade_worker, sid, [group])
    return jsonify({'started': True})


@app.route('/api/series/<sid>/facades/generate-for-location', methods=['POST'])
def facades_generate_for_location(sid):
    """Generate ONE facade for ONE specific location (or attach the location
    to a new single-member facade group). Lets the user iterate per-loc
    instead of «сгенерировать все».

    Body: { loc_id: str, building_name?: str, facade_description?: str }
    If building_name/facade_description are missing, Claude derives them
    from the location's name + description on-the-fly."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    loc_id = (body.get('loc_id') or '').strip()
    if not loc_id:
        return jsonify({'error': 'loc_id required'}), 400
    loc = next((l for l in (s.get('locations') or []) if l.get('id') == loc_id), None)
    if not loc:
        return jsonify({'error': 'location not found'}), 404
    name = (body.get('building_name') or '').strip()
    desc = (body.get('facade_description') or '').strip()
    if not name or not desc:
        # Quick Claude call — turn the location's own info into a building
        # name + facade description. Cheap, single-shot Haiku-equivalent.
        try:
            sys = (
                "Ты — продюсер визуальной библиотеки сериала. Тебе дают ОДНУ локацию "
                "(имя + описание интерьера/места действия). Если это интерьер — "
                "определи название здания-обладателя (вилла, больница, отель, школа) "
                "и опиши его ФАСАД СНАРУЖИ. Если это уже наружная локация — оставь "
                "имя как есть, опиши вид издалека. Только JSON.\n"
                f'Локация: "{loc["name"]}" — {(loc.get("description") or "")[:300]}\n\n'
                'Верни:\n{\n'
                '  "building_name": "...",\n'
                '  "facade_description": "..." (1-2 предложения, английский)\n'
                '}\n'
            )
            raw = claude_ask("Reply with JSON only.", system=sys, max_tokens=600)
            data = json.loads(strip_json(raw))
            name = name or (data.get('building_name') or loc['name']).strip()
            desc = desc or (data.get('facade_description') or '').strip()
        except Exception as e:
            return jsonify({'error': f'Derive failed: {e}'}), 500
    st = _facade_status(sid)
    if st.get('running'):
        return jsonify({'error': 'generation already running'}), 409
    group = {
        'building_name': name or loc['name'],
        'type': 'building',
        'facade_description': desc or f"Exterior of {loc['name']}.",
        'member_loc_ids': [loc_id],
    }
    _spawn_with_keys(_facade_worker, sid, [group])
    return jsonify({'started': True, 'building_name': group['building_name']})


@app.route('/api/series/<sid>/facades/folder', methods=['POST'])
def facades_open_folder(sid):
    """macOS / Linux / Windows-friendly: opens the facades folder in the OS
    file manager when the app runs locally. On the deployed server this is
    a no-op; the UI uses the returned `folder` path as a copyable hint."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    d = facades_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    folder = str(d.resolve())
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        elif sys.platform == 'win32':
            subprocess.Popen(['explorer', folder])
        elif sys.platform.startswith('linux'):
            subprocess.Popen(['xdg-open', folder])
    except Exception:
        pass
    return jsonify({'folder': folder})


@app.route('/api/series/<sid>/locations/<loc_id>/save-frame/<project_id>', methods=['POST'])
def save_location_frame(sid, loc_id, project_id):
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'not found'}), 404

    hdrs = rtl_headers()
    status_resp = requests.get(f'{RETELLER_API}/projects/{project_id}', headers=hdrs, timeout=15)
    if not status_resp.ok:
        return jsonify({'error': status_resp.text}), status_resp.status_code
    if status_resp.json().get('status') != 'completed':
        return jsonify({'ready': False, 'status': status_resp.json().get('status')})

    frames_resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}/assets/list?types=frames',
        headers=hdrs, timeout=15
    )
    if not frames_resp.ok:
        return jsonify({'error': frames_resp.text}), 500

    frame_assets = [a for a in frames_resp.json().get('assets', []) if a['type'] == 'frames']
    if not frame_assets:
        return jsonify({'ready': False, 'status': 'no_frames'})

    img_resp = requests.get(frame_assets[0]['url'], timeout=30)
    if not img_resp.ok:
        return jsonify({'error': 'download failed'}), 500

    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    loc_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_name(loc["name"])}.jpg'  # e.g. THE_NETWORKING_EVENT_VENUE.jpg
    (loc_dir / filename).write_bytes(img_resp.content)

    rel_path = f'assets/locations/{loc_slug}/{filename}'
    loc.setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}'})


# ── Item image generation (story prop reference) ─────────────────────────────

@app.route('/api/series/<sid>/items/<item_id>/upload-photo', methods=['POST'])
def upload_item_photo(sid, item_id):
    s = load_series(sid)
    item = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no file'}), 400
    item_slug = slugify(item['name'])
    item_dir = assets_dir(sid) / 'items' / item_slug
    item_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    filename = f'{asset_name(item["name"])}.{ext}'
    (item_dir / filename).write_bytes(f.read())
    rel_path = f'assets/items/{item_slug}/{filename}'
    refs = item.setdefault('ref_images', [])
    if rel_path not in refs:
        refs.insert(0, rel_path)
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'series': s})


@app.route('/api/series/<sid>/items/<item_id>/generate-image', methods=['POST'])
def generate_item_image(sid, item_id):
    s = load_series(sid)
    item = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'not found'}), 404

    style_clause = _series_style_clause(s)
    constraints = (item.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    modern_doc_clause = _modern_document_directive(item)
    # Documents legitimately carry printed text → skip the no-text clause for them.
    no_text_clause = '' if modern_doc_clause else _no_caption_text_clause(constraints)
    it_name = _strip_cast_names_for_visual(item['name'], s)
    it_desc = _strip_cast_names_for_visual(item.get('description', ''), s)
    prompt = (
        f"{it_name}. {it_desc}.{constraints_clause}{modern_doc_clause}{no_text_clause} "
        f"Product-style still-life photo of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless gray background (#dadada) — flat color field NOT a photo studio set (no lighting rigs, no trusses, no equipment visible), soft even diffused illumination on the subject only, "
        f"subtle shadow on ground, sharp focus on object texture and details. "
        f"Square 1:1 framing. Photorealistic, high detail. {style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    item_slug = slugify(item['name'])
    item_dir = assets_dir(sid) / 'items' / item_slug
    out_path = item_dir / f'{asset_name(item["name"])}.jpg'

    try:
        # Items use square aspect — works as a portable reference for both
        # vertical (Reteller) and horizontal (Seedance) compositions.
        image_url = avai_generate(prompt, out_path, aspect_ratio='1:1', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = item.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        item['avai_url'] = image_url  # used by Seedance for video refs
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/items/<item_id>/regenerate', methods=['POST'])
def regenerate_item(sid, item_id):
    """Regenerate the item's reference image with user-supplied constraints.
    Persists constraints on the item for future regenerations."""
    s = load_series(sid)
    item = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'item not found'}), 404
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    item['image_constraints'] = wishes
    style_clause = _series_style_clause(s)
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    modern_doc_clause = _modern_document_directive(item)
    no_text_clause = '' if modern_doc_clause else _no_caption_text_clause(wishes)
    it_name = _strip_cast_names_for_visual(item['name'], s)
    it_desc = _strip_cast_names_for_visual(item.get('description', ''), s)
    prompt = (
        f"{it_name}. {it_desc}.{constraints_clause}{modern_doc_clause}{no_text_clause} "
        f"Product-style still-life photo of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless gray background (#dadada) — flat color field NOT a photo studio set (no lighting rigs, no trusses, no equipment visible), soft even diffused illumination on the subject only, "
        f"subtle shadow on ground, sharp focus on object texture and details. "
        f"Square 1:1 framing. Photorealistic, high detail. {style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    item_slug = slugify(item['name'])
    item_dir = assets_dir(sid) / 'items' / item_slug
    item_dir.mkdir(parents=True, exist_ok=True)
    out_path = item_dir / f'{asset_name(item["name"])}.jpg'
    try:
        if out_path.exists():
            try: out_path.unlink()
            except Exception: pass
        image_url = avai_generate(prompt, out_path, aspect_ratio='1:1', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = item.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        item['avai_url'] = image_url
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Cyrillic → Latin transliteration table for cross-language item dedup.
# Tiny on purpose — we only need it for the simple case where the same prop
# is described in Russian and English versions of the same script (e.g.
# "диктофон" / "dictaphone", "локет" / "locket", "флешка" / "flash drive").
_TRANSLIT_DEDUP = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f',
    'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y',
    'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})

def _norm_for_dedup(s):
    """Lowercase + translit + alnum-only for fuzzy comparisons."""
    s = (s or '').lower().translate(_TRANSLIT_DEDUP)
    return re.sub(r'[^a-z0-9]+', '', s)

def _llm_dedupe_against_existing(existing_items, new_items):
    """Ask Claude to merge synonym/translation duplicates between newly-detected
    items and the existing series.items list. The lexical _fuzzy_find_item misses
    synonyms — 'dictaphone' / 'voice recorder' / 'recording device' are all the
    same prop but share no substring or 3-word description overlap.

    Returns {new_idx: existing_id} for items the model says are duplicates.
    Items without a mapping are kept as truly new.

    No-op (empty mapping) when no existing items or no new items."""
    if not existing_items or not new_items:
        return {}
    payload = {
        'existing': [
            {'id': it['id'], 'name': it.get('name', ''), 'description': (it.get('description') or '')[:160]}
            for it in existing_items
        ],
        'new': [
            {'idx': i, 'name': (it.get('name') or ''), 'description': (it.get('description') or '')[:160]}
            for i, it in enumerate(new_items)
        ],
    }
    system = (
        "You merge duplicate plot-items. Two items are DUPLICATES if they refer to "
        "the same physical prop in the story, regardless of:\n"
        "  - language drift (Russian vs English description)\n"
        "  - synonyms (dictaphone = voice recorder = recording device; locket = pendant; "
        "gun = pistol = revolver; flashdrive = USB stick = USB drive)\n"
        "  - paraphrasing (silver locket vs antique silver pendant with photo)\n\n"
        "They are NOT duplicates if:\n"
        "  - one is a SECOND distinct copy of the same kind of object the script "
        "treats as a separate plot-prop (e.g. 'second dictaphone' that's NOT the "
        "hidden one — both can exist)\n"
        "  - they're different objects that just look similar\n\n"
        "Return STRICT JSON, no prose, no markdown:\n"
        '{"merges":[{"new_idx":INT,"existing_id":"..."}]}\n'
        "Include ONLY confirmed duplicates. Items not listed in `merges` are kept "
        "as new entries. If nothing duplicates, return {\"merges\":[]}."
    )
    try:
        raw = claude_ask(json.dumps(payload, ensure_ascii=False), system=system, max_tokens=1024)
        parsed = loads_lenient(raw)
        out = {}
        for m in (parsed.get('merges') or []):
            try:
                idx = int(m.get('new_idx'))
                eid = str(m.get('existing_id') or '').strip()
                if eid and any(it['id'] == eid for it in existing_items):
                    out[idx] = eid
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f'[item-dedupe-llm] failed: {e}', flush=True)
        return {}


def _fuzzy_find_item(items, name, desc):
    """Find an existing item matching the new name/description, even when
    the LLM returned a slight rewording or a translation. Tiered match:
      1. Exact case-insensitive name (fast path, current behaviour)
      2. Normalised name (strip punctuation, transliterate Cyrillic → Latin)
      3. Substring overlap of normalised name (e.g. 'hidden recorder' vs
         'recorder') — both ways
      4. Description overlap (≥3 words shared in normalised form) — handles
         total renames like 'диктофон' → 'recording device'
    Returns the matched dict or None."""
    if not items:
        return None
    name_l = name.lower()
    # Tier 1: exact ci match
    for it in items:
        if it.get('name', '').lower() == name_l:
            return it
    name_n = _norm_for_dedup(name)
    if not name_n:
        return None
    # Tier 2 + 3: normalised exact / substring
    for it in items:
        ex = _norm_for_dedup(it.get('name', ''))
        if not ex:
            continue
        if ex == name_n:
            return it
        # Substring either direction (longer-than-3 to skip noise like 'a')
        if len(name_n) >= 4 and len(ex) >= 4:
            if name_n in ex or ex in name_n:
                return it
    # Tier 4: description-word overlap
    if desc:
        desc_words = set(re.findall(r'[a-zа-яё]{4,}', desc.lower()))
        desc_words_n = {_norm_for_dedup(w) for w in desc_words}
        desc_words_n.discard('')
        for it in items:
            ex_desc = it.get('description', '')
            if not ex_desc:
                continue
            ex_words = set(re.findall(r'[a-zа-яё]{4,}', ex_desc.lower()))
            ex_words_n = {_norm_for_dedup(w) for w in ex_words}
            ex_words_n.discard('')
            if len(desc_words_n & ex_words_n) >= 3:
                return it
    return None


@app.route('/api/series/<sid>/dedupe-items', methods=['POST'])
def dedupe_series_items(sid):
    """Walks series.items, finds dups via fuzzy + LLM-synonym pass, merges
    them into a canonical set. Used to clean up dups accumulated BEFORE the
    detect-items dedup logic was added (e.g. 'Hidden Voice Recorder' +
    'hidden dictaphone' + 'desk dictaphone' all referring to one prop).
    For each dup group:
      - keeps the entry with the longest description (or earliest by id)
        as canonical
      - rewrites every episode.items_used to point at canonical
      - removes the dup entry from series.items
    Returns {'merged': N, 'kept': N, 'groups': [...]}.
    Idempotent — running twice does nothing the second time."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    items = list(s.get('items', []) or [])
    if len(items) < 2:
        return jsonify({'merged': 0, 'kept': len(items), 'groups': []}), 200

    # Build groups by walking pairs through fuzzy + LLM. Greedy: each item is
    # tested against already-formed group leads; if matches → joins that group.
    groups = []  # list of [item, item, ...]
    for it in items:
        joined = False
        for grp in groups:
            lead = grp[0]
            if _fuzzy_find_item([lead], it['name'], it.get('description', '')):
                grp.append(it); joined = True; break
        if not joined:
            groups.append([it])

    # Stage B: try to merge groups that didn't match lexically — pass each
    # group's lead vs all OTHER leads through the LLM dedup.
    if len(groups) >= 2:
        leads = [grp[0] for grp in groups]
        # Use LLM to find synonym-pairs among leads. Treat first lead as
        # "existing", every other lead as "new" — then iterate pairs.
        # To keep prompt small we batch all leads and ask for transitive
        # equivalence sets.
        try:
            sys = (
                "Cluster these plot-items into groups where each group is the "
                "SAME prop (across language, synonyms, paraphrasing). Return "
                'JSON: {"groups":[["id1","id2",...], ["id3"], ...]}. Items not '
                'paired with any duplicate go in their own singleton group. '
                'No prose, strict JSON.'
            )
            payload = json.dumps({
                'items': [
                    {'id': lead['id'], 'name': lead.get('name', ''), 'description': (lead.get('description') or '')[:160]}
                    for lead in leads
                ]
            }, ensure_ascii=False)
            raw = claude_ask(payload, system=sys, max_tokens=1024)
            parsed = loads_lenient(raw)
            llm_groups = parsed.get('groups') or []
            # Validate: collect lead-id → llm-group-idx
            id_to_grp = {}
            for gi, grp_ids in enumerate(llm_groups):
                for iid in grp_ids:
                    id_to_grp[iid] = gi
            # Re-cluster `groups` according to llm groupings.
            if id_to_grp:
                new_groups = {}
                for grp in groups:
                    lead_id = grp[0]['id']
                    gi = id_to_grp.get(lead_id)
                    if gi is None:
                        # LLM dropped it — keep as own group
                        new_groups[f'orphan-{lead_id}'] = new_groups.get(f'orphan-{lead_id}', []) + grp
                    else:
                        new_groups.setdefault(gi, []).extend(grp)
                groups = list(new_groups.values())
        except Exception as e:
            print(f'[dedupe-items-llm] failed (using fuzzy-only): {e}', flush=True)

    # Apply merges: pick canonical, rewrite items_used in all episodes,
    # remove dups from series.items.
    canonical_by_dup_id = {}  # dup_id → canonical_id
    final_items = []
    merged_groups_log = []
    for grp in groups:
        if len(grp) == 1:
            final_items.append(grp[0])
            continue
        # Canonical = longest description (most info), tie-break on shortest name.
        grp_sorted = sorted(grp, key=lambda x: (-len(x.get('description', '')), len(x.get('name', ''))))
        canonical = grp_sorted[0]
        # Merge ref_images / avai_url from any group member if canonical is empty
        for member in grp:
            if member is canonical:
                continue
            if not canonical.get('ref_images') and member.get('ref_images'):
                canonical['ref_images'] = member['ref_images']
            if not canonical.get('avai_url') and member.get('avai_url'):
                canonical['avai_url'] = member['avai_url']
            canonical_by_dup_id[member['id']] = canonical['id']
        final_items.append(canonical)
        merged_groups_log.append({
            'canonical': {'id': canonical['id'], 'name': canonical['name']},
            'merged': [{'id': m['id'], 'name': m['name']} for m in grp if m is not canonical],
        })

    if not canonical_by_dup_id:
        return jsonify({'merged': 0, 'kept': len(items), 'groups': []})

    s['items'] = final_items
    save_series(sid, s)
    # Rewrite every episode's items_used to use canonical ids only.
    for ep in list_episodes(sid):
        used = ep.get('items_used') or []
        if not used:
            continue
        rewritten = []
        seen = set()
        for iid in used:
            cid = canonical_by_dup_id.get(iid, iid)
            if cid not in seen:
                rewritten.append(cid); seen.add(cid)
        if rewritten != used:
            ep['items_used'] = rewritten
            save_episode(sid, ep['number'], ep)

    return jsonify({
        'merged': len(canonical_by_dup_id),
        'kept': len(final_items),
        'groups': merged_groups_log,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/detect-items', methods=['POST'])
def detect_items_in_episode(sid, num):
    """LLM extracts PLOT-RELEVANT items from the episode's script. Plot-relevant
    means the item is load-bearing for the story (the locket revealed at the
    climax, the USB stick with evidence, the stolen handbag) — NOT every random
    prop in frame (coffee cups, generic furniture, background dressing).

    For each detected item:
      - if a series.items entry with the same lowercased name exists → reuse its id
      - otherwise create a new series.items entry (no ref_image yet — autogen
        sweep or manual click handles photo)
      - add the id to episode.items_used (idempotent)

    Returns {'detected': [{name, description, status: 'created'|'existing'}, ...]}.
    """
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'episode has no script yet'}), 400

    # KNOWN-items context so re-runs across language barriers don't dup. The
    # LLM was previously fed the script with no awareness of what's already
    # in series.json — so e.g. a Russian script containing "скрытый диктофон"
    # extracted as "Hidden Recorder" / "Recording Device" / "Скрытый диктофон"
    # on different runs, producing 3 separate item entries for the same prop.
    # Now the prompt explicitly lists known items (name + description) and
    # tells the model to reuse the EXACT existing name when it sees the same
    # plot-prop, regardless of language drift in the script.
    known_items_block = ''
    existing_items = s.get('items', []) or []
    if existing_items:
        known_lines = [
            f"  - {it['name']!r}: {(it.get('description') or '').strip()[:120]}"
            for it in existing_items if it.get('name')
        ]
        known_items_block = (
            "\n\n=== ALREADY KNOWN ITEMS (use the EXACT existing name when the script "
            "describes the same prop, even if the script uses a different language or "
            "synonym; do NOT create a duplicate with a translated/paraphrased name) ===\n"
            + '\n'.join(known_lines) + '\n'
        )

    system = (
        "You extract PLOT-RELEVANT items from a short-drama script. Return STRICT JSON.\n"
        "PLOT-RELEVANT = the item is load-bearing for the story: it gets revealed,\n"
        "stolen, exchanged, hidden, used as evidence, gifted, broken, found,\n"
        "carried by a character through multiple scenes, or its presence/absence\n"
        "drives a beat. Examples: a locket with a photo, a USB stick with files,\n"
        "a wedding ring, a stolen handbag, a contract document, a vial of poison.\n\n"
        "NOT plot-relevant — DO NOT extract: generic furniture, coffee cups,\n"
        "phones used only for routine calls, clothing (covered separately by\n"
        "outfits), food eaten without significance, background dressing.\n\n"
        "Return JSON: {\"items\": [{\"name\": \"...\", \"description\": \"...\"}, ...]}\n"
        "name: short concrete noun phrase. PREFER the EXACT existing name from the\n"
        "  KNOWN ITEMS list when the script is talking about the same prop, even\n"
        "  across languages (Russian script + English known name = use the English\n"
        "  known name). Only invent a new name when the prop is genuinely new.\n"
        "description: 1 sentence describing visual appearance for image gen.\n"
        "If nothing qualifies (or all qualifying items are already in KNOWN), return\n"
        "{\"items\": []}. No prose, no preamble."
    )
    raw = claude_ask(
        f"Script:\n\n{script[:18000]}{known_items_block}",
        system=system, model='', max_tokens=2048,
    )
    try:
        parsed = loads_lenient(raw)
        detected_raw = parsed.get('items') or []
    except Exception as e:
        return jsonify({'error': f'LLM returned unparseable JSON: {e}; raw={raw[:300]}'}), 502

    s.setdefault('items', [])
    if not isinstance(ep.get('items_used'), list):
        ep['items_used'] = []
    # Cap detected list early so the dedup-pass payload stays small.
    detected_capped = detected_raw[:20]
    # Two-stage dedup vs existing items:
    #   Stage A: cheap lexical _fuzzy_find_item (handles exact + transliteration
    #            + substring + description-word-overlap)
    #   Stage B: if anything remains "new", ask Claude to merge synonyms
    #            (dictaphone↔voice recorder, etc.) — single small LLM call.
    pre_matches = {}  # idx → existing item dict
    leftovers   = []  # [(idx, name, desc), ...] for stage B
    for i, d in enumerate(detected_capped):
        name = (d.get('name') or '').strip()
        desc = (d.get('description') or '').strip()
        if not name:
            continue
        match = _fuzzy_find_item(s['items'], name, desc)
        if match:
            pre_matches[i] = match
        else:
            leftovers.append((i, name, desc))
    llm_merges = {}
    if leftovers and s['items']:
        new_for_llm = [{'name': n, 'description': desc} for (_, n, desc) in leftovers]
        merged = _llm_dedupe_against_existing(s['items'], new_for_llm)
        # `merged` keys are indices into new_for_llm; map back to detected_capped indices.
        for j, eid in merged.items():
            if 0 <= j < len(leftovers):
                orig_idx = leftovers[j][0]
                existing = next((it for it in s['items'] if it['id'] == eid), None)
                if existing:
                    llm_merges[orig_idx] = existing

    detected_summary = []
    for i, d in enumerate(detected_capped):
        name = (d.get('name') or '').strip()
        desc = (d.get('description') or '').strip()
        if not name:
            continue
        existing = pre_matches.get(i) or llm_merges.get(i)
        if existing:
            item_id = existing['id']
            status = 'existing'
            # Refresh description if currently empty.
            if not (existing.get('description') or '').strip() and desc:
                existing['description'] = desc
        else:
            item_id = str(uuid.uuid4())[:8]
            s['items'].append({
                'id': item_id,
                'name': name,
                'description': desc,
                'ref_images': [],
                'avai_url': '',
                'image_constraints': '',
            })
            status = 'created'
        if item_id not in ep['items_used']:
            ep['items_used'].append(item_id)
        detected_summary.append({'name': name, 'description': desc, 'status': status})

    save_series(sid, s)
    save_episode(sid, num, ep)
    return jsonify({'detected': detected_summary, 'items_used': ep['items_used']})


# ── Auto-generate missing assets (background sweep) ──────────────────────────

import threading
import concurrent.futures

# Per-series lock so a sweep doesn't run twice in parallel for the same series
_AUTOGEN_LOCKS = {}
_AUTOGEN_STATUS = {}  # sid -> {'running': bool, 'queue': int, 'done': int, 'errors': [], 'in_progress': [...]}

def _autogen_status(sid):
    return _AUTOGEN_STATUS.setdefault(sid, {
        'running': False, 'queue': 0, 'done': 0, 'errors': [],
        # in_progress: list of {kind, parent_id, child_id?, name} entries currently
        # being generated. Frontend uses this to show spinners on specific cards.
        'in_progress': [],
    })

def _gen_char_base_inline(s, sid, char):
    """Generate base ref for character. Mutates s, saves at end."""
    if char.get('ref_images'):
        return
    constraints = (char.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    appearance = (char.get('appearance') or '').strip()
    # Scrub the canonical appearance at creation time too: it's persisted and
    # rides into every later prompt/BINDING. The one-off spicy intent stays in
    # image_constraints (constraints_clause), which is NOT persisted into appearance.
    _appearance_clean = _sanitize_appearance_for_moderation(appearance)
    if _appearance_clean != appearance:
        char['appearance'] = _appearance_clean
        appearance = _appearance_clean
    description = (char.get('description') or '').strip()
    # Detect anthropomorphic species — check NAME first (catches «Wolf»/
    # «Hyena»/«Fox Woman» where appearance text was written as «a man in
    # worn-out clothes»), fall back to appearance keywords for human-named
    # chars described with fur/muzzle markers.
    species_hint = _detect_animal_species(char.get('name'), appearance, series=s)
    # Self-heal for anthro worlds — same logic as generate_character_image.
    # Catches the auto_generate_assets path where chars came from extractors
    # without species in appearance.
    if not species_hint and _is_anthro_world(s):
        inferred = _llm_infer_species_for_char(s, char)
        if inferred and inferred != 'human':
            patched = _patch_appearance_with_species(appearance, inferred, char.get('gender', ''))
            if patched and patched != appearance:
                char['appearance'] = patched
                appearance = patched
                print(f'[anthro-heal] inline: char {char.get("name")} → species={inferred}', flush=True)
            species_hint = _detect_animal_species(char.get('name'), appearance, series=s)
    is_animal = bool(species_hint)
    species_override = ''
    if is_animal:
        gender_word = 'female' if char.get('gender') == 'female' else 'male'
        kind_label = f', a {gender_word} {species_hint}'
        species_override = (
            f" CRITICAL: {char['name']} is an ANTHROPOMORPHIC {species_hint.split()[-1].upper()}, "
            f"NOT a human. The character has a {species_hint.split()[-1]}'s head/face "
            f"(realistic snout, ears, eyes typical of the species) with appropriate fur/feathers/scales, "
            f"walking upright with anthropomorphic body proportions, wearing human-style clothing. "
            f"Zootopia/Pixar-style anthropomorphic animal — DO NOT render as a plain human. "
            f"Ignore any wording like «a man» / «a woman» in the description above — those describe "
            f"the character's gender role, not human anatomy."
        )
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
    # Style: project's visual_style overrides default photorealistic. Pixar/anime/etc
    # require explicit style directive AND removal of "Photorealistic" suffix —
    # otherwise model gets conflicting signals and renders human-looking realism.
    style_clause = _series_style_clause(s)
    visual_style = _series_visual_style(s)
    era_clause = _series_era_hint(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality, high detail on face and clothing.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    clothing_fallback = '' if is_animal else _clothing_clause(appearance, description, era_hint=era_clause)
    prompt = (
        f"{style_prefix}"
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{appearance}. {description}.{constraints_clause}{species_override} "
        f"{era_clause + ' ' if era_clause else ''}"
        f"{clothing_fallback}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. "
        f"Soft even diffused illumination on the character only (lighting source NOT visible in frame), no harsh shadows on face or body, no visible lights or equipment."
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / f'{asset_name(char["name"], "BASE")}.jpg'
    # Race-safe path: write to a unique temp file FIRST, never the canonical
    # path directly. The autogen orchestrator (_run_task) atomically moves
    # the temp into place ONLY if no concurrent user-driven regenerate has
    # populated `char['ref_images']` in the meantime.
    # Without this, the following sequence corrupts user state:
    #   T0  autogen worker reads stale snapshot (ref_images empty)
    #   T0+5  autogen avai_generate writes canonical CATHERINE_BASE.jpg
    #   T0+10 user clicks Regenerate, regen avai_generate ALSO writes
    #         canonical CATHERINE_BASE.jpg (user's version)
    #   T0+15 autogen avai_generate (slow API for a different concurrent
    #         worker) finishes and overwrites CATHERINE_BASE.jpg with the
    #         stale autogen image — silently undoing the user's regen on
    #         disk. User-reported on series «Six Weeks After the Gala».
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f'.autogen-{uuid.uuid4().hex[:8]}-{out_path.name}')
    image_url = avai_generate(prompt, tmp_path, preferred_provider=_series_image_provider(s))
    rel_path = str(out_path.relative_to(series_path(sid)))
    # Stash the staging info on the char so _run_task can commit it under lock.
    # `_autogen_pending` is intentionally NOT persisted to disk — orchestrator
    # consumes it before any save_series.
    char['_autogen_pending'] = {
        'tmp_path': str(tmp_path),
        'canonical_path': str(out_path),
        'rel_path': rel_path,
        'image_url': image_url,
    }

def _gen_outfit_inline(s, sid, char, outfit):
    """Generate outfit photo via i2i from char base. Mutates outfit."""
    if outfit.get('photo'):
        return
    if outfit.get('is_base') and char.get('ref_images'):
        outfit['photo'] = char['ref_images'][0]
        outfit['avai_url'] = char.get('avai_base_url', '')
        return
    if not char.get('ref_images'):
        raise RuntimeError(f'Char "{char["name"]}" has no base ref yet')
    reference_url = char.get('avai_base_url')
    constraints = (char.get('image_constraints') or '').strip()
    constraints_clause = f' IMPORTANT — strictly follow these constraints: {constraints}. ' if constraints else ''
    appearance = (char.get('appearance') or '').strip()
    appearance_low = appearance.lower()
    animal_words = ('fur', 'muzzle', 'snout', 'tail', 'paws', 'claws', 'whiskers',
                    'mane', 'feathers', 'beak', 'horns', 'antlers', 'hooves', 'scales',
                    'cub', 'pup', 'kitten', 'fang', 'fangs',
                    'шерсть', 'мордa', 'морду', 'морды', 'хвост', 'лапы', 'когти',
                    'клыки', 'грива', 'перья', 'клюв', 'рога', 'копыта')
    is_animal = any(w in appearance_low for w in animal_words)
    if is_animal:
        same_clause = 'Same character as the reference image (same species, same fur/markings, same age). '
        intro_clause = f'Full body portrait of {char["name"]}. {appearance}. '
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        same_clause = f'Same {gender} as the reference image. '
        intro_clause = f'Full body portrait of {char["name"]}, a {gender}. {appearance}. '
    # Alternate-identity HAIR: a disguise look (dyed / wig / new identity) must be
    # ALLOWED to change the hair. The default i2i instruction below hard-locks hair
    # to the base portrait ("only the clothing changes"), which is exactly why a
    # "blonde" disguise rendered brunette. When the look declares a hair override,
    # keep only the FACE identical and restyle the hair instead.
    hair_phrase = _outfit_hair_phrase(outfit)
    changes_hair = bool(hair_phrase) and not is_animal
    if changes_hair:
        if reference_url:
            same_clause = (f'Same {gender} as the reference image — keep the EXACT same face, '
                           f'facial features and bone structure. ')
        else:
            # No base ref: render from appearance, but with the disguised hair.
            intro_clause = (f'Full body portrait of {char["name"]}, a {gender}. '
                            f'{_override_hair_in_appearance(appearance, hair_phrase)}. ')
        hair_change_clause = (
            f'IMPORTANT — this is a deliberate new look / disguise: the HAIR is now {hair_phrase}. '
            f'Restyle the hair to {hair_phrase}; do NOT keep the reference hair colour or style. '
            f'The FACE stays identical — only the hair and wardrobe change. '
        )
        clothing_lock_clause = ''
    else:
        hair_change_clause = ''
        clothing_lock_clause = 'Same face, same body — only the clothing changes. ' if reference_url else ''
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    prompt = (
        f"{style_prefix}"
        + (same_clause if reference_url else intro_clause)
        + f'Now wearing: {outfit["label"]}. {outfit.get("description", "")}. '
        + clothing_lock_clause
        + hair_change_clause
        + constraints_clause
        + 'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
          'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
          'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
          'Soft even diffused illumination on the character only (no visible lights or equipment).'
        + realism_suffix
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / 'outfits' / f'{asset_name(char["name"], outfit["label"])}.jpg'
    image_url = avai_generate(prompt, out_path, reference_url=reference_url, preferred_provider=_series_image_provider(s))
    outfit['photo'] = str(out_path.relative_to(series_path(sid)))
    outfit['avai_url'] = image_url

def _gen_loc_inline(s, sid, loc):
    if loc.get('ref_images'):
        return
    tone = s.get('tone', '')
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality, high detail.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    loc_name = _strip_cast_names_for_visual(loc['name'], s)
    loc_desc = _strip_cast_names_for_visual(loc.get('description', ''), s)
    prompt = (
        f"{style_prefix}"
        f"{loc_name}. {loc_desc}. "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
        f"Atmospheric lighting.{_no_caption_text_clause()}"
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    loc_slug = slugify(loc['name'])
    out_path = assets_dir(sid) / 'locations' / loc_slug / f'{asset_name(loc["name"])}.jpg'
    image_url = avai_generate(prompt, out_path, aspect_ratio='16:9', preferred_provider=_series_image_provider(s))
    loc.setdefault('ref_images', []).insert(0, str(out_path.relative_to(series_path(sid))))


def _gen_item_inline(s, sid, item):
    """Generate ref image for a story-prop item. Mutates item, caller saves.
    Uses square 1:1 product-still-life style — works as portable ref for both
    Seedance (9:16) and Reteller (vertical) compositions.
    Idempotent: skips if item already has refs."""
    if item.get('ref_images'):
        return
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, high detail.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    constraints = (item.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    modern_doc_clause = _modern_document_directive(item)
    no_text_clause = '' if modern_doc_clause else _no_caption_text_clause(constraints)
    it_name = _strip_cast_names_for_visual(item['name'], s)
    it_desc = _strip_cast_names_for_visual(item.get('description', ''), s)
    prompt = (
        f"{style_prefix}"
        f"{it_name}. {it_desc}.{constraints_clause}{modern_doc_clause}{no_text_clause} "
        f"Product-style still-life of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless gray background (#dadada) — flat color field NOT a photo studio set (no lighting rigs, no trusses, no equipment visible), soft even diffused illumination on the subject only, "
        f"subtle shadow on ground, sharp focus on object texture and details. "
        f"Square 1:1 framing."
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    item_slug = slugify(item['name'])
    out_path = assets_dir(sid) / 'items' / item_slug / f'{asset_name(item["name"])}.jpg'
    image_url = avai_generate(prompt, out_path, aspect_ratio='1:1', preferred_provider=_series_image_provider(s))
    rel_path = str(out_path.relative_to(series_path(sid)))
    item.setdefault('ref_images', []).insert(0, rel_path)
    item['avai_url'] = image_url


_AUTOGEN_SAVE_LOCKS: dict[str, threading.Lock] = {}
_AUTOGEN_PARALLELISM = 4

def auto_generate_missing_assets(sid):
    """Background sweep: generate base photos for chars without refs,
    outfit photos for outfits without photos, location refs for locations without refs.
    Pipeline:
      Phase 1 — char-bases in parallel (outfits need them as i2i refs)
      Phase 2 — outfits + locations in parallel (independent of each other)
    Each worker calls the slow avai_generate API outside any lock; only the
    final read-modify-write of series.json runs under a per-series save-lock,
    so 4 concurrent workers can image-gen at once without trashing the file.
    Idempotent — skips anything already generated.
    Also pre-syncs every episode's cast block so chars/outfits referenced in scripts
    but missing from series.json get created before the sweep runs."""
    lock = _AUTOGEN_LOCKS.setdefault(sid, threading.Lock())
    if not lock.acquire(blocking=False):
        print(f'[autogen {sid}] already running, skipping')
        return
    save_lock = _AUTOGEN_SAVE_LOCKS.setdefault(sid, threading.Lock())
    st = _autogen_status(sid)
    st.update({'running': True, 'queue': 0, 'done': 0, 'errors': [], 'in_progress': []})
    try:
        # ─── Pre-sweep: ensure every episode's cast block AND [BLOCKING] outfits
        # are reflected in series.json. This is the GUARANTEE that no outfit
        # mentioned in any saved script gets missed — even if the path that
        # saved the script (import worker, history restore, direct file edit,
        # legacy versions before sync was hooked everywhere) forgot to call
        # _sync_script_outfits. The autogen sweep runs whenever any asset
        # generation is requested, so this acts as the final reconciliation
        # layer. Idempotent: existing outfits match by label and are skipped.
        # Skip episodes where extraction hasn't been confirmed by the user yet.
        for ep in list_episodes(sid):
            if not (ep.get('script') or '').strip():
                continue
            if ep.get('cast_extracted', True) is False:
                continue
            try:
                sync_episode_with_cast_block(sid, ep['number'])
            except Exception as e:
                print(f'[autogen {sid}] cast-block sync ep{ep["number"]} failed: {e}')
            # ALSO sync [BLOCKING] outfits — separate from cast-block sync.
            # This is the layer that catches scenarios like Margaret's missing
            # Prison Jumpsuit in ep 21 (script had the OUTFIT line but no save
            # path triggered the sync). Note: _sync_script_outfits internally
            # tries to _spawn_with_keys(auto_generate_missing_assets, sid)
            # again — that nested spawn is BLOCKED by _AUTOGEN_LOCKS lock,
            # so no recursion; the newly-created outfits will be picked up
            # by THIS sweep's task list (built right below after a reload).
            try:
                _sync_script_outfits(sid, ep.get('script') or '')
            except Exception as e:
                print(f'[autogen {sid}] blocking-outfit sync ep{ep["number"]} failed: {e}')

        s = load_series(sid)
        if not s: return
        # Build task list — phased
        char_tasks = []
        outfit_tasks = []
        loc_tasks = []
        # `_skip_autogen=true` on a char/loc/item explicitly opts out of the
        # sweep (set via the Accept-script modal's "🚫 Не генерить эту группу"
        # checkbox). Honored at task-build time — the entity is never queued
        # so it stays empty until the user clicks generate manually later.
        for c in s.get('characters', []):
            if c.get('_skip_autogen'):
                continue
            if not c.get('ref_images'):
                char_tasks.append(('char', c['id'], None))
        for c in s.get('characters', []):
            if c.get('_skip_autogen'):
                continue
            for o in c.get('outfits', []):
                if o.get('_skip_autogen'):
                    continue
                if not o.get('photo') and not o.get('is_base'):
                    outfit_tasks.append(('outfit', c['id'], o['id']))
        for l in s.get('locations', []):
            if l.get('_skip_autogen'):
                continue
            if not l.get('ref_images'):
                loc_tasks.append(('loc', l['id'], None))
        item_tasks = []
        for it in s.get('items', []):
            if it.get('_skip_autogen'):
                continue
            if not it.get('ref_images'):
                item_tasks.append(('item', it['id'], None))
        total_tasks = len(char_tasks) + len(outfit_tasks) + len(loc_tasks) + len(item_tasks)
        st['queue'] = total_tasks
        print(f'[autogen {sid}] {total_tasks} assets to generate '
              f'(chars={len(char_tasks)}, outfits={len(outfit_tasks)}, locs={len(loc_tasks)}, items={len(item_tasks)}, parallel={_AUTOGEN_PARALLELISM})',
              flush=True)

        def _ip_add(entry):
            # status['in_progress'] is a plain list — protect with the save_lock
            with save_lock:
                st['in_progress'].append(entry)

        def _ip_remove(parent_id, child_id):
            with save_lock:
                st['in_progress'] = [e for e in st['in_progress']
                                      if not (e.get('parent_id') == parent_id and e.get('child_id') == child_id)]

        # Snapshot the user-keys context HERE while we're still on the parent
        # thread (which had keys propagated by _spawn_with_keys). Each child
        # worker copies this into its own threading.local at the top of its
        # task — without this, ThreadPoolExecutor's child threads run with an
        # empty _thread_keys and every helper that calls user_root() /
        # current_user_email() / _get_user_avai_key() crashes with
        # "Working outside of request context". This was the root cause of the
        # "[autogen ...] FAILED item/...: Working outside of request context"
        # spam — items don't have any other auth-attached call paths so they
        # showed it loudest, but chars/locs would have hit it too if they
        # didn't already have refs (ref_images guard skips before keys are used).
        _ctx_email     = getattr(_thread_keys, 'email', None)
        _ctx_avai_key  = getattr(_thread_keys, 'avai_key', None)
        _ctx_rtl_key   = getattr(_thread_keys, 'reteller_key', None)

        def _run_task(kind, parent_id, child_id):
            # Apply captured user-keys context to THIS worker thread.
            _thread_keys.email        = _ctx_email
            _thread_keys.avai_key     = _ctx_avai_key
            _thread_keys.reteller_key = _ctx_rtl_key
            ip_entry = None
            try:
                # Re-load LOCALLY so each worker has a fresh read for its mutation
                s_local = load_series(sid)
                if not s_local:
                    return
                if kind == 'char':
                    char = next((c for c in s_local.get('characters', []) if c['id'] == parent_id), None)
                    if not char or char.get('ref_images'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'char', 'parent_id': parent_id, 'child_id': None, 'name': char.get('name', '')}
                    _ip_add(ip_entry)
                    _gen_char_base_inline(s_local, sid, char)  # SLOW: avai API call → writes to temp path
                    pending = char.get('_autogen_pending') or {}
                    tmp_p   = Path(pending.get('tmp_path', ''))
                    canon_p = Path(pending.get('canonical_path', ''))
                    new_url = pending.get('image_url', '')
                    new_rel = pending.get('rel_path', '')
                    if not tmp_p or not tmp_p.exists():
                        return  # generation failed before producing a file
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk:
                            tmp_p.unlink(missing_ok=True)
                            return
                        c_disk = next((c for c in s_disk.get('characters', []) if c['id'] == parent_id), None)
                        if c_disk and not c_disk.get('ref_images'):
                            # Commit: rename temp → canonical, persist series.json
                            try:
                                canon_p.parent.mkdir(parents=True, exist_ok=True)
                                tmp_p.replace(canon_p)  # atomic on same filesystem
                            except Exception as e:
                                _log_event('WARN', 'autogen_char_commit_failed',
                                           char_id=parent_id, err=str(e)[:200])
                                tmp_p.unlink(missing_ok=True)
                                return
                            c_disk['ref_images'] = [new_rel]
                            c_disk['avai_base_url'] = new_url
                            c_disk['updated_at'] = int(time.time())
                            save_series(sid, s_disk)
                        else:
                            # User regen / manual upload already populated this
                            # char between our stale snapshot and this commit.
                            # Discard our work — keep the user's version intact.
                            tmp_p.unlink(missing_ok=True)
                            _log_event('INFO', 'autogen_char_skipped_by_user_regen',
                                       char_id=parent_id, name=char.get('name', ''))
                elif kind == 'outfit':
                    char = next((c for c in s_local.get('characters', []) if c['id'] == parent_id), None)
                    if not char: return
                    outfit = next((o for o in char.get('outfits', []) if o['id'] == child_id), None)
                    if not outfit or outfit.get('photo'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'outfit', 'parent_id': parent_id, 'child_id': child_id,
                                'name': f"{char.get('name','')}/{outfit.get('label','')}"}
                    _ip_add(ip_entry)
                    _gen_outfit_inline(s_local, sid, char, outfit)  # SLOW: avai i2i call
                    new_photo = outfit.get('photo') or ''
                    new_url   = outfit.get('avai_url') or ''
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        c_disk = next((c for c in s_disk.get('characters', []) if c['id'] == parent_id), None)
                        if not c_disk: return
                        o_disk = next((o for o in c_disk.get('outfits', []) if o['id'] == child_id), None)
                        if o_disk and not o_disk.get('photo'):
                            o_disk['photo'] = new_photo
                            o_disk['avai_url'] = new_url
                            save_series(sid, s_disk)
                elif kind == 'loc':
                    loc = next((l for l in s_local.get('locations', []) if l['id'] == parent_id), None)
                    if not loc or loc.get('ref_images'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'loc', 'parent_id': parent_id, 'child_id': None, 'name': loc.get('name', '')}
                    _ip_add(ip_entry)
                    _gen_loc_inline(s_local, sid, loc)  # SLOW: avai API call
                    new_refs = loc.get('ref_images') or []
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        l_disk = next((l for l in s_disk.get('locations', []) if l['id'] == parent_id), None)
                        if l_disk and not l_disk.get('ref_images'):
                            l_disk['ref_images'] = new_refs
                            save_series(sid, s_disk)
                elif kind == 'item':
                    item = next((it for it in s_local.get('items', []) if it['id'] == parent_id), None)
                    if not item or item.get('ref_images'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'item', 'parent_id': parent_id, 'child_id': None, 'name': item.get('name', '')}
                    _ip_add(ip_entry)
                    _gen_item_inline(s_local, sid, item)  # SLOW: avai API call
                    new_refs = item.get('ref_images') or []
                    new_url = item.get('avai_url') or ''
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        i_disk = next((it for it in s_disk.get('items', []) if it['id'] == parent_id), None)
                        if i_disk and not i_disk.get('ref_images'):
                            i_disk['ref_images'] = new_refs
                            i_disk['avai_url'] = new_url
                            save_series(sid, s_disk)
                st['done'] += 1
            except Exception as e:
                err_msg = f'{kind}/{parent_id}: {str(e)[:200]}'
                st['errors'].append(err_msg)
                print(f'[autogen {sid}] FAILED {err_msg}', flush=True)
            finally:
                _ip_remove(parent_id, child_id)
                # Clear the worker-thread's _thread_keys so a recycled pool
                # thread doesn't leak the previous task's user context into a
                # later task (or another series's sweep on the same process).
                _thread_keys.email        = None
                _thread_keys.avai_key     = None
                _thread_keys.reteller_key = None

        # Phase 1: char bases — outfits depend on these
        if char_tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_AUTOGEN_PARALLELISM) as pool:
                list(pool.map(lambda t: _run_task(*t), char_tasks))
        # Phase 2: outfits + locations + items together (none depend on chars)
        phase2 = outfit_tasks + loc_tasks + item_tasks
        if phase2:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_AUTOGEN_PARALLELISM) as pool:
                list(pool.map(lambda t: _run_task(*t), phase2))
    finally:
        st['running'] = False
        st['in_progress'] = []
        lock.release()
        print(f'[autogen {sid}] done — {st["done"]}/{st["queue"]} ok, {len(st["errors"])} errors')
        # Re-check: if new chars/outfits/locs appeared during the sweep (e.g. a parallel
        # extract-characters or sync_episode_with_cast_block added rows mid-flight), the
        # current run has already left them un-generated. Kick off another sweep so the
        # user doesn't have to click "regenerate" manually.
        try:
            s2 = load_series(sid)
            if s2 and s2.get('auto_generate_assets'):
                # NB: must mirror the _skip_autogen logic from task-collection
                # above. Otherwise entities the user explicitly opted-out of
                # auto-gen (via "🚫 Не генерить" tickbox in the Accept-script
                # modal) are counted as pending forever — sweep finishes with
                # queue=0 (everything skipped at task-build time), recheck sees
                # pending>0 (skip flag ignored here), spawns another sweep,
                # loops infinitely. Symptom on the frontend: the heartbeat
                # catches every brief running=true blip and the «ничего не
                # нужно генерить» line keeps re-painting under the button.
                pending = (
                    sum(1 for c in s2.get('characters', [])
                        if not c.get('ref_images') and not c.get('_skip_autogen')) +
                    sum(1 for c in s2.get('characters', []) if not c.get('_skip_autogen')
                        for o in c.get('outfits', [])
                        if not o.get('photo') and not o.get('is_base') and not o.get('_skip_autogen')) +
                    sum(1 for l in s2.get('locations', [])
                        if not l.get('ref_images') and not l.get('_skip_autogen')) +
                    sum(1 for it in s2.get('items', [])
                        if not it.get('ref_images') and not it.get('_skip_autogen'))
                )
                if pending > 0:
                    print(f'[autogen {sid}] {pending} new assets queued during sweep — re-running')
                    # We're already inside a thread-local-scoped worker (spawned via
                    # _spawn_with_keys), so _capture_user_keys() reads the propagated
                    # keys and forwards them to the recursive sweep.
                    _spawn_with_keys(auto_generate_missing_assets, sid)
                # ALWAYS kick off facade auto-grouping after a sweep — not gated
                # on pending==0. If a recursive sweep is also spawned above,
                # auto_facades_for_new_locations is idempotent (its own running-
                # flag check + no-op when no ungrouped locs) so the duplicate
                # spawn is harmless. Without this, a sweep that ends with
                # pending>0 only fires facades on the recursive tail — which
                # could fail silently (Claude outage, key issue) and never
                # retry. Firing here too gives us a second chance.
                try:
                    _spawn_with_keys(auto_facades_for_new_locations, sid)
                except Exception as e:
                    print(f'[autogen {sid}] facade auto-trigger failed to spawn: {e}')
        except Exception as e:
            print(f'[autogen {sid}] re-check failed: {e}')


def trigger_autogen_if_enabled(sid):
    """Public entry point: kick off background sweep if the series has auto_generate_assets on.
    Uses _spawn_with_keys so AVAI calls inside the sweep have a valid x-api-key
    after the request context tears down."""
    s = load_series(sid)
    if not s or not s.get('auto_generate_assets'):
        return False
    _spawn_with_keys(auto_generate_missing_assets, sid)
    return True


@app.route('/api/series/<sid>/auto-generate', methods=['POST'])
def toggle_auto_generate(sid):
    """Toggle auto_generate_assets flag. If enabling, immediately kick off a sweep."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    data = request.json or {}
    enabled = bool(data.get('enabled', True))
    if enabled:
        _nd_anthro, _fl_anthro = _anthro_preflight(s, s.get('characters') or [])
        if _nd_anthro:
            return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                            'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    s['auto_generate_assets'] = enabled
    save_series(sid, s)
    started = False
    if enabled:
        started = trigger_autogen_if_enabled(sid)
    return jsonify({'enabled': enabled, 'sweep_started': started, 'status': _autogen_status(sid)})


@app.route('/api/series/<sid>/auto-generate/status', methods=['GET'])
def autogen_status_endpoint(sid):
    st = _autogen_status(sid)
    # Phantom-running self-heal: if running=true but the worker thread is
    # actually dead (queue=0, nothing in progress, lock acquired but never
    # released), reset the state. Happens when the daemon thread dies mid-
    # run (server reload kills daemon threads, KeyboardInterrupt, hard
    # crash) and the finally{} that flips running=false never executed.
    # Without this self-heal the UI spinner spins forever at 0/0.
    if st.get('running') and not st.get('queue') and not (st.get('in_progress') or []):
        # Try to acquire the per-series lock non-blocking. If we can grab
        # it, the worker is definitely not running anymore — release it
        # back and reset the status.
        lock = _AUTOGEN_LOCKS.get(sid)
        if lock is None or lock.acquire(blocking=False):
            if lock is not None:
                lock.release()
            st['running'] = False
            st['in_progress'] = []
            print(f'[autogen {sid}] phantom-running detected — reset', flush=True)
    return jsonify(st)


@app.route('/api/series/<sid>/auto-generate/sweep', methods=['POST'])
def trigger_autogen_sweep(sid):
    """Manual sweep — runs regardless of auto_generate_assets toggle.
    Pre-syncs every episode's cast block, then generates all missing assets."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    # Force-enable the toggle (user explicitly asked for a sweep, this is what they want)
    if not s.get('auto_generate_assets'):
        s['auto_generate_assets'] = True
        save_series(sid, s)
    _nd_anthro, _fl_anthro = _anthro_preflight(s, s.get('characters') or [])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    _spawn_with_keys(auto_generate_missing_assets, sid)
    return jsonify({'started': True, 'status': _autogen_status(sid)})


@app.route('/api/series/<sid>/reanalyze-outfits', methods=['POST'])
def reanalyze_outfits(sid):
    """Bulk-reparse [BLOCKING] outfits across selected episodes (or all
    episodes with a script when `episode_numbers` is omitted). Fix for the
    common case where an episode was generated under an older writer prompt
    or before character-name matching was lenient — re-running the sync now
    detects outfits the first pass missed and queues image generation.

    Also back-fills WEAK descriptions on existing outfits — when the writer
    only provided a label (e.g. `OUTFIT: Business Casual` with no OUTFIT_DESC),
    the outfit was created with `description == label`, which is a useless
    text anchor and makes Seedance reinvent the cloth on every chunk. We
    detect these via `description == label` (case-insensitive) and expand them
    via a single Claude call each, plus clear the outfit photo so the autogen
    sweep regenerates the image with the new concrete description.

    Body: {"episode_numbers": [int, ...]}  // optional. Omit/empty → all-with-script.
    Returns: {episodes_processed, total_new_outfits, total_weak_descs_fixed,
              by_episode: [{number, new_outfits: [...]}]}
    """
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    requested = body.get('episode_numbers') or []
    requested_set = set(int(n) for n in requested) if requested else None

    all_eps = list_episodes(sid)
    targets = [ep for ep in all_eps
               if (ep.get('script') or '').strip()
               and (requested_set is None or ep['number'] in requested_set)]

    by_episode = []
    total = 0
    for ep in targets:
        try:
            new_outfits = _sync_script_outfits(sid, ep.get('script') or '')
        except Exception as e:
            _log_event('WARN', 'reanalyze_outfits_failed',
                       sid=sid, ep=ep['number'], err=str(e)[:200])
            new_outfits = []
        by_episode.append({
            'number': ep['number'],
            'title': ep.get('title', ''),
            'new_outfits': new_outfits,
        })
        total += len(new_outfits)

    # ── Back-fill weak descriptions on existing outfits ─────────────────
    # An outfit is "weak" when description is empty OR equals the label —
    # legacy state from before _sync_script_outfits enforced expansion.
    s = load_series(sid)  # reload — _sync_script_outfits may have written
    weak_fixed = 0
    weak_changed = False
    for c in (s.get('characters') or []):
        for o in (c.get('outfits') or []):
            label = (o.get('label') or '').strip()
            desc  = (o.get('description') or '').strip()
            if not label:
                continue
            if desc and desc.lower() != label.lower():
                continue  # already has a real description
            # Expand via Claude
            new_desc = _expand_outfit_label_to_desc(
                label,
                c.get('appearance', ''),
                c.get('gender', ''),
            )
            if not new_desc or new_desc.strip().lower() == label.lower():
                continue  # expansion failed or returned the same label
            o['description'] = new_desc.strip()
            # Clear the existing photo so the autogen sweep regenerates the
            # outfit reference image with the concrete description — without
            # this the old weakly-anchored ref keeps causing chunk-to-chunk drift.
            o['photo'] = None
            o['avai_url'] = ''
            weak_fixed += 1
            weak_changed = True
            _log_event('INFO', 'outfit_weak_desc_expanded',
                       sid=sid, char=c.get('name'), label=label,
                       new_desc=new_desc[:120])
    if weak_changed:
        save_series(sid, s)

    # Fire one consolidated autogen sweep — covers both newly-created outfits
    # AND the photo-cleared ones from back-fill above.
    if total > 0 or weak_fixed > 0:
        try:
            _spawn_with_keys(auto_generate_missing_assets, sid)
        except Exception as e:
            print(f'[reanalyze-outfits] autogen spawn failed for {sid}: {e}')

    return jsonify({
        'episodes_processed': len(targets),
        'total_new_outfits': total,
        'total_weak_descs_fixed': weak_fixed,
        'by_episode': by_episode,
    })


# ── Series Canon endpoints ──────────────────────────────────────────────────
@app.route('/api/series/<sid>/canon', methods=['GET'])
def get_canon(sid):
    if not series_file(sid).exists(): return jsonify({'error': 'not found'}), 404
    return jsonify(load_canon(sid))

@app.route('/api/series/<sid>/canon/rebuild', methods=['POST'])
def rebuild_canon(sid):
    """Wipe canon and re-extract from all existing scripts in order. Fully automated."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    save_canon(sid, _empty_canon())
    eps = sorted(list_episodes(sid), key=lambda e: e['number'])
    results = []
    for ep in eps:
        if not (ep.get('script') or '').strip():
            continue
        try:
            r = extract_canon_updates(sid, ep['number'], ep['script'])
        except Exception as e:
            r = {'error': str(e)}
        results.append({'ep': ep['number'], **(r if isinstance(r, dict) else {'ok': False})})
    return jsonify({'ok': True, 'episodes_processed': results, 'canon': load_canon(sid)})


# ── Generate asset prompt ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/generate-prompt', methods=['POST'])
def generate_asset_prompt(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    asset_type = data['type']
    asset_id = data['id']
    style = s['style'].get('type', 'cinematic')
    tone = s.get('tone', '')
    genre = s.get('genre', '')
    world = s.get('world_description', '')

    if asset_type == 'character':
        char = next((c for c in s['characters'] if c['id'] == asset_id), None)
        if not char:
            return jsonify({'error': 'character not found'}), 404
        gender_word = 'woman' if char.get('gender') == 'female' else 'man'
        appearance = char.get('appearance', '')
        description = char.get('description', '')
        prompt = (
            f"Character reference sheet. {char['name']}, a {gender_word}. "
            f"{appearance}. {description}. "
            f"Full body, front-facing neutral pose, arms relaxed at sides. "
            f"Isolated on solid uniform light gray background (#E0E0E0). "
            f"Soft even diffused illumination on the character only (no visible lights or studio equipment), no harsh shadows. Sharp focus. "
            f"Photorealistic, professional character design reference. "
            f"No background objects or gradients. Clean, simple, reference-quality."
        )

    elif asset_type == 'outfit':
        char_id = data.get('char_id')
        char = next((c for c in s['characters'] if c['id'] == char_id), None)
        if not char:
            return jsonify({'error': 'character not found'}), 404
        outfit = next((o for o in char.get('outfits', []) if o['id'] == asset_id), None)
        if not outfit:
            return jsonify({'error': 'outfit not found'}), 404
        gender_word = 'woman' if char.get('gender') == 'female' else 'man'
        appearance = char.get('appearance', '')
        outfit_desc = outfit.get('description', '')
        prompt = (
            f"Same character — {char['name']}, a {gender_word}. {appearance}. "
            f"Now wearing/posed: {outfit_desc}. "
            f"Full body, front-facing neutral pose unless the outfit description specifies otherwise. "
            f"Isolated on solid uniform light gray background (#E0E0E0). "
            f"Soft even diffused illumination on the character only (no visible lights or studio equipment), no harsh shadows. Sharp focus. "
            f"Photorealistic, professional character reference. "
            f"Identical face and body to the base reference — change ONLY the clothing/pose described above. "
            f"No background objects or gradients. Clean, simple, reference-quality."
        )

    elif asset_type == 'location':
        loc = next((l for l in s.get('locations', []) if l['id'] == asset_id), None)
        if not loc:
            return jsonify({'error': 'location not found'}), 404
        description = _strip_cast_names_for_visual(loc.get('description', ''), s)
        loc_name = _strip_cast_names_for_visual(loc['name'], s)
        context = ' '.join(filter(None, [genre, tone, world]))
        prompt = (
            f"{loc_name}. {description}. "
            f"Empty scene, no people present. "
            f"{tone + ' atmosphere. ' if tone else ''}"
            f"{style.capitalize()} visual style. "
            f"Cinematic wide establishing shot. "
            f"Photorealistic, high detail, professional cinematography. "
            f"{('World context: ' + world[:100] + '. ') if world else ''}"
            f"Atmospheric lighting, sharp focus.{_no_caption_text_clause()}"
        )
    else:
        return jsonify({'error': 'unknown type'}), 400

    # Clean up double spaces
    import re
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    return jsonify({'prompt': prompt})


# ── AI: Series idea generation ───────────────────────────────────────────────

from sw.data.idea_prompts import (
    _IDEAS_SYSTEM,
    _IDEAS_SCHEMA,
    IDEAS_V2,
    _IDEAS_SYSTEM_V2,
    _IDEAS_RESEARCH_SYSTEM,
    _IDEAS_RESEARCH_TAIL,
    _IDEAS_LOGIC_SYSTEM,
    _IDEAS_SCHEMA_V2,
    _IDEA_CORE_ENGINES,
    _IDEA_CORE_LEADS,
    _IDEA_CORE_WORLDS,
    _IDEA_CORE_TWISTS,
    _IDEA_ENGINE_FAMILIES,
    _IDEA_PROTAG_ARCS,
    _ideas_diversity_lanes,
)
_IDEAS_HISTORY_MAX = 40

def _ideas_history_path():
    return DATA_ROOT / 'ideas_history.json'

def _load_ideas_history():
    try:
        p = _ideas_history_path()
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(x) for x in data if x]
    except Exception as e:
        print(f'[ideas] history load failed ({e.__class__.__name__})', flush=True)
    return []

def _ideas_signature(idea):
    if not isinstance(idea, dict):
        return ''
    title = (idea.get('title') or '').strip()
    hook = (idea.get('synopsis') or idea.get('world_description') or '').strip()
    hook = ' '.join(hook.split())[:90]
    sig = f'{title} - {hook}' if hook else title
    return sig.strip(' -')

def _save_ideas_history(ideas):
    try:
        sigs = [s for s in (_ideas_signature(i) for i in (ideas or [])) if s]
        if not sigs:
            return
        hist = _load_ideas_history()
        hist.extend(sigs)
        hist = hist[-_IDEAS_HISTORY_MAX:]
        _ideas_history_path().write_text(json.dumps(hist, ensure_ascii=False, indent=0), encoding='utf-8')
    except Exception as e:
        print(f'[ideas] history save failed ({e.__class__.__name__})', flush=True)

def _ideas_avoid_recent_block():
    hist = _load_ideas_history()
    if not hist:
        return ''
    recent = hist[-18:]
    lines = '\n'.join(f'- {s}' for s in recent)
    return (
        "RECENTLY SHOWN - DO NOT REPEAT (hard): the user has already seen these concepts in "
        "previous batches. Every one of your 5 ideas must be clearly distinct from ALL of them - "
        "different title, different hook, different mechanic. Do not reflavour or rename any of these:\n"
        f"{lines}\n\n"
    )


def _ideas_research_digest(brief):
    """Stage 1: trend digest. Tries live web search (Anthropic web_search),
    falls back silently to model knowledge. Always returns a string."""
    research_prompt = (
        "Find the BEST-performing vertical short dramas right now for this brief:\n"
        f"{brief}\n\n"
        "Search the live web for current TOP-RATED and MOST-VIEWED titles across ReelShort, DramaBox, "
        "GoodShort, ShortMax, NetShort and viral verticals on YouTube / TikTok. "
        + _IDEAS_RESEARCH_TAIL
    )
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=5)
        if digest and len(digest.strip()) > 40:
            print('[ideas] research: web', flush=True)
            return digest.strip()
        print('[ideas] research: web returned thin result -> fallback', flush=True)
    except Exception as e:
        print(f'[ideas] research: web failed ({e.__class__.__name__}) -> fallback', flush=True)
    fb_prompt = (
        "From your own knowledge of the best-performing vertical short dramas (ReelShort / DramaBox / "
        f"GoodShort style), for this brief:\n{brief}\n\n"
        + _IDEAS_RESEARCH_TAIL
    )
    digest = claude_ask_quality(fb_prompt, system=_IDEAS_RESEARCH_SYSTEM)
    print('[ideas] research: fallback', flush=True)
    return (digest or '').strip()


def _run_ideas_pipeline_v2(writer_model, user_controls, genres, idea_hint,
                           era_world_override=False, avoid_rule=''):
    """3-stage idea generation. Returns a list of <=5 idea dicts (legacy schema
    fields preserved). Raises on hard failure so the caller can fall back to the
    legacy single-call path."""
    # Stage 0 — research brief from user controls.
    bits = []
    if genres:
        bits.append('genres: ' + ', '.join(genres))
    if idea_hint:
        bits.append('creator hint: ' + idea_hint)
    if era_world_override:
        bits.append('a non-modern era/world is set — surface period/world-appropriate hits')
    brief = '; '.join(bits) if bits else 'the overall most popular, highest-rated and most-viewed vertical short dramas right now, across all genres'

    # Stage 1 — trend digest (web -> fallback).
    digest = _ideas_research_digest(brief)

    # Stage 2 — generate 5 diverse ideas.
    gen_prompt = (
        "Generate exactly 5 short-drama series concepts for vertical mobile video.\n\n"
        + user_controls
        + "TOP-PERFORMING SHORT DRAMAS RIGHT NOW (researched live from the market — study what makes these "
          "win, then write FRESH concepts in the same vein; never copy a title or plot):\n"
        + digest + "\n\n"
        + _ideas_avoid_recent_block()
        + "Build the 5 so each is inspired by a DIFFERENT one of the hits above — mirror the real RANGE that is "
          "winning, do NOT funnel them into one repeated template. Follow the creator's chosen genre / preferences "
          "/ era EXACTLY (above). Each must be a clearly different story: different premise, lead and hook.\n\n"
        + "Each synopsis = the HOOK only, 1-2 short sentences, ~40 words MAX — a punchy logline a viewer "
          "grasps in three seconds, NOT a plot recap. Lead with the gut-punch and stop. Set the \"style\" field to \"logline\".\n\n"
        + "JSON SAFETY: output strict valid JSON. Do NOT use the double-quote character inside any "
          "field value — if someone speaks, paraphrase or use single quotes. No line breaks inside values.\n\n"
        + f"Return JSON matching this schema:\n{_IDEAS_SCHEMA_V2}"
    )
    ideas = loads_lenient(strip_json(llm_ask(writer_model, gen_prompt, system=_IDEAS_SYSTEM_V2)))
    ideas = ideas.get('ideas', ideas) if isinstance(ideas, dict) else ideas
    if not isinstance(ideas, list) or not ideas:
        raise ValueError('ideas stage-2 returned no list')
    print(f'[ideas] stage-2 generated {len(ideas)} ideas', flush=True)

    # Stage 3 — logic-check + polish (honors writer_model).
    era_note = ''
    if era_world_override:
        era_note = (
            "- ERA/WORLD: a non-modern era/world is in play (see directive at top of the "
            "original brief). Any idea that reads like a present-day realistic story, or uses a "
            "prop/role/event impossible in that era/world, must be rewritten so the era/world is "
            "unmistakable in the first sentence.\n"
        )
    genre_note = ''
    if genres:
        genre_note = "- GENRES: " + ', '.join(genres) + " must genuinely be present in every idea.\n"
    avoid_note = ''
    if avoid_rule:
        _ban = " ".join(avoid_rule.split())[:700]
        avoid_note = ("- BAN LIST (hard, includes synonyms/translations): " + _ban + " If ANY idea contains a banned concept — even in subtext — rewrite that idea onto a completely different premise.\n")
    check_prompt = (
        "Here are 5 short-drama concepts as JSON. Audit and polish them.\n\n"
        + json.dumps({'ideas': ideas}, ensure_ascii=False)
        + "\n\nFor EACH idea, verify and FIX in place:\n"
          "- LOGIC: timeline holds, who-knows-what is consistent, the premise does not collapse "
          "under one obvious question. If it breaks, rewrite the idea so it holds.\n"
          "- SIMPLICITY: one clear premise a person can picture; no fact-stacking, no piled-on "
          "jobs or backstories.\n"
          "- VOICE: the synopsis must read like a person describing a show to a friend, NOT a "
          "checklist of answers («she works as X, she is N months pregnant»). Rewrite stiff/listy "
          "synopses into natural, propulsive prose.\n"
          "- DISTINCTNESS: if two ideas are reflavoured copies, rewrite one onto a different engine.\n"
          "- LENGTH (HARD): every synopsis MUST be 1-2 short sentences, ~40 words MAX. If a draft runs longer, "
          "or chains morning-after / weeks-later / years-later beats, CUT it down to the single gut-punch hook. "
          "Aggressively shorten — short and primal beats long and clever.\n"
          "- ON-BRIEF: every idea must fit the creator's chosen genre / preferences / era (see below). If an "
          "idea drifts off the chosen genre, rewrite it to fit. If nothing was chosen, keep it in the vein of the "
          "proven hits the writer was given.\n"
          "- VARIETY: the 5 must mirror the real range of what is winning — do NOT let them collapse into one "
          "repeated template (e.g. every lead a wronged woman who turns out to be secretly powerful). If they bunch "
          "up, rewrite the duplicates into genuinely different stories.\n"
        + era_note
        + genre_note
        + avoid_note
        + "\nKeep exactly 5 ideas. Return JSON in the SAME schema (title, genre, tone, "
          "target_audience, world_description, synopsis, synopsis_ru; style optional). Output strict valid JSON — never use the double-quote character inside a field value; use single quotes for any spoken line."
    )
    try:
        checked = loads_lenient(strip_json(llm_ask(writer_model, check_prompt, system=_IDEAS_LOGIC_SYSTEM)))
        checked = checked.get('ideas', checked) if isinstance(checked, dict) else checked
        if isinstance(checked, list) and checked:
            print(f'[ideas] stage-3 logic-check returned {len(checked)} ideas', flush=True)
            return checked
        print('[ideas] stage-3 returned empty -> keeping stage-2 ideas', flush=True)
    except Exception as e:
        print(f'[ideas] stage-3 logic-check failed ({e.__class__.__name__}) -> keeping stage-2 ideas', flush=True)
    return ideas


_TOP_DRAMAS_SCHEMA = """{
  "dramas": [
    {
      "title": "Real show title",
      "premise": "One-line hook/premise in English",
      "premise_ru": "То же по-русски, живо",
      "genre": "Genre / tone",
      "why_hook": "Why it hooks viewers (short phrase)",
      "popularity": "Views / rating / chart signal if known, else empty string"
    }
  ]
}"""


def _research_top_dramas(genres=None, idea_hint='', era_hint='', n=10):
    """Live-web research of the ACTUAL top-performing short dramas, returned as a
    structured list (powers the 'Find top short dramas' button). Genre / hint /
    era filter the search when given; otherwise the overall best across genres."""
    bits = []
    if genres:
        bits.append('genres: ' + ', '.join(genres))
    if idea_hint:
        bits.append('creator hint: ' + idea_hint)
    if era_hint:
        bits.append(era_hint)
    brief = '; '.join(bits) if bits else 'the overall most popular, highest-rated and most-viewed vertical short dramas right now, across all genres'
    # Step 1 — live web research returns a PROSE list of real shows (the search
    # tool wraps output in commentary, so we do NOT ask it for JSON here).
    research_prompt = (
        "Find the BEST-performing vertical short dramas right now for this brief:\n"
        f"{brief}\n\n"
        "Search the live web for current TOP-RATED and MOST-VIEWED titles across ReelShort, DramaBox, "
        "GoodShort, ShortMax, NetShort and viral verticals on YouTube / TikTok. List 8-12 of the strongest "
        "REAL shows; for each give: title, one-line premise, genre/tone, why it hooks viewers, and any "
        "popularity signal (views / rating / chart position) you can find. Be concrete with real titles."
    )
    digest = ''
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=6)
        if digest and len(digest.strip()) > 40:
            print('[top-dramas] research: web', flush=True)
        else:
            digest = ''
    except Exception as e:
        print(f'[top-dramas] web failed ({e.__class__.__name__}) -> fallback', flush=True)
        digest = ''
    if not digest:
        digest = claude_ask_quality(research_prompt, system=_IDEAS_RESEARCH_SYSTEM) or ''
        print('[top-dramas] research: fallback', flush=True)
    # Step 2 — structure the prose into strict JSON with a plain (no-tool) call.
    fmt_prompt = (
        "Here is research on the current top-performing vertical short dramas:\n\n"
        f"{digest}\n\n"
        f"Convert it into STRICT valid JSON, {n}-12 entries, matching this schema. Output ONLY the JSON "
        f"(no prose, no code fences) and do NOT use the double-quote character inside any value:\n{_TOP_DRAMAS_SCHEMA}"
    )
    raw = claude_ask_quality(fmt_prompt, system='You output ONLY strict valid JSON — no prose, no code fences, no commentary.') or ''
    data = loads_lenient(strip_json(raw))
    dramas = data.get('dramas', data) if isinstance(data, dict) else data
    if not isinstance(dramas, list):
        raise ValueError('top-dramas: no list parsed')
    out = []
    for d in dramas:
        if not isinstance(d, dict):
            continue
        title = (d.get('title') or '').strip()
        if not title:
            continue
        out.append({
            'title': title,
            'premise': (d.get('premise') or '').strip(),
            'premise_ru': (d.get('premise_ru') or '').strip(),
            'genre': (d.get('genre') or '').strip(),
            'why_hook': (d.get('why_hook') or d.get('why') or '').strip(),
            'popularity': (d.get('popularity') or '').strip(),
        })
    return out


@app.route('/api/research-top-dramas', methods=['POST'])
def research_top_dramas():
    """Return a structured list of the current top short dramas for the picker."""
    data_in = request.json or {}
    genres = data_in.get('genres') or []
    idea_hint = (data_in.get('idea') or '').strip()
    era_dir = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    era_hint = 'a non-modern era/world is set — surface period/world-appropriate hits' if era_dir else ''
    try:
        dramas = _research_top_dramas(genres=genres, idea_hint=idea_hint, era_hint=era_hint)
        print(f'[top-dramas] returned {len(dramas)} dramas', flush=True)
        return jsonify({'dramas': dramas})
    except Exception as e:
        _log_event('WARN', 'top_dramas_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500



_IDEAS_SIMILAR_SYSTEM = """You convert a proven short-drama HIT into a ready-to-use series concept for our app, keeping the hit's premise EXACTLY — 1 to 1.

ABSOLUTE RULE: do NOT reinterpret, do NOT invent a new profession, company, setting or twist that is not in the given premise, and do NOT change who hides what or who discovers what. The concept must describe the SAME story as the hit, in the same shape. If the hit is «a wife discovers her humble husband is secretly a billionaire», the synopsis is exactly that — a wife discovers her ordinary husband is secretly a billionaire — NOT a janitor, NOT a taxi driver, NOT a new twist. Keep it that clean and that faithful.

The ONLY new things you create: an original short English title (do NOT reuse the hit's own title) and the genre / tone / target_audience / world_description fields. Voice natural and short, PG-13 register, never vulgar sexual verbs in any language. synopsis in English and synopsis_ru in natural Russian, BOTH stating the premise 1-to-1. Return ONLY valid JSON for the given schema."""


def _ideas_from_drama(drama, genres=None, era_hint='', writer_model=''):
    """Turn ONE chosen hit into a ready-to-use series concept that keeps its premise
    1-TO-1 (powers the per-card 'Сделать подобный сериал' button). No reinterpretation,
    no invented specifics — the output IS the chosen drama's idea, just retitled."""
    title   = (drama.get('title') or '').strip()
    premise = (drama.get('premise_ru') or drama.get('premise') or '').strip()
    genre   = (drama.get('genre') or '').strip()
    controls = []
    if genres:
        controls.append('Chosen genres (must fit): ' + ', '.join(genres))
    if era_hint:
        controls.append(era_hint)
    controls_s = ('\n'.join(controls) + '\n\n') if controls else ''
    prompt = (
        "The proven HIT to turn into a series, KEEPING ITS PREMISE 1-TO-1:\n"
        f"TITLE: {title}\nGENRE: {genre}\nPREMISE: {premise}\n\n"
        + controls_s
        + "Produce exactly 1 series concept whose premise is the SAME as this hit — identical setup, roles and "
          "reveal. Do NOT reinterpret it, do NOT invent a profession / company / setting / twist that is not in "
          "the premise above, do NOT change who hides what or who discovers what. Keep it as clean and general as "
          "the premise itself. Create only an original short title plus genre / tone / target_audience / "
          "world_description.\n\n"
        + "synopsis + synopsis_ru = 1-2 short sentences stating that EXACT premise (the same story as the hit). "
          "Set the \"style\" field to \"logline\".\n\n"
        + "JSON SAFETY: strict valid JSON; no double-quote character inside any value; no line breaks inside values.\n\n"
        + f"Return JSON matching this schema (a single idea inside the ideas array):\n{_IDEAS_SCHEMA_V2}"
    )
    ideas = loads_lenient(strip_json(llm_ask(writer_model, prompt, system=_IDEAS_SIMILAR_SYSTEM)))
    ideas = ideas.get('ideas', ideas) if isinstance(ideas, dict) else ideas
    if not isinstance(ideas, list) or not ideas:
        raise ValueError('ideas-from-drama: no list parsed')
    print(f'[ideas-from-drama] 1-to-1 concept from {title!r}', flush=True)
    return ideas


@app.route('/api/ideas-from-drama', methods=['POST'])
def ideas_from_drama():
    """Generate 5 concepts in the vein of ONE chosen top drama."""
    data_in = request.json or {}
    drama = data_in.get('drama') or {}
    if not isinstance(drama, dict) or not (drama.get('title') or drama.get('premise') or drama.get('premise_ru')):
        return jsonify({'error': 'no drama provided'}), 400
    genres = data_in.get('genres') or []
    writer_model = _resolve_writer_model(data_in)
    era_dir = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    era_hint = 'a non-modern era/world is set — keep every idea in that period/world' if era_dir else ''
    try:
        ideas = _ideas_from_drama(drama, genres=genres, era_hint=era_hint, writer_model=writer_model)
        return jsonify(ideas)
    except Exception as e:
        _log_event('WARN', 'ideas_from_drama_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


_DRAMA_ANALYSIS_SCHEMA = """{
  "title": "The show title",
  "detailed_synopsis": "4-6 sentence rich English synopsis — detailed enough to build the first 5 episodes from",
  "detailed_synopsis_ru": "То же по-русски, подробно и живо",
  "main_characters": ["Name — who they are and what they want", "..."],
  "central_conflict": "1-2 sentences on the core conflict",
  "hook": "why viewers binge it",
  "setting": "where / when it takes place",
  "first_5_episodes": ["Серия 1: что происходит + клиффхэнгер", "Серия 2: ...", "Серия 3: ...", "Серия 4: ...", "Серия 5: ..."]
}"""


def _top_dramas_path():
    return DATA_ROOT / 'top_dramas.json'


def _load_top_dramas():
    try:
        p = _top_dramas_path()
        if p.exists():
            d = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(d, dict):
                return d
    except Exception as e:
        print(f'[top-dramas] load failed ({e.__class__.__name__})', flush=True)
    return {'scanned_at': '', 'genres': [], 'dramas': []}


def _save_top_dramas(store):
    try:
        _top_dramas_path().write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception as e:
        print(f'[top-dramas] save failed ({e.__class__.__name__})', flush=True)


def _drama_slug(title, i):
    import re as _re
    base = _re.sub(r'[^a-z0-9]+', '-', (title or '').lower()).strip('-')[:40]
    return base or f'drama-{i}'


def _analyze_drama(drama):
    """Deep two-step study of ONE real show -> detailed synopsis + first-5-episode
    setup (powers the per-card 'Изучить подробнее')."""
    title   = (drama.get('title') or '').strip()
    premise = (drama.get('premise_ru') or drama.get('premise') or '').strip()
    genre   = (drama.get('genre') or '').strip()
    research_prompt = (
        f'Study the vertical short drama "{title}" ({genre}) as thoroughly as possible.\n'
        f'Known premise: {premise}\n\n'
        'Search the live web for everything about THIS specific show: its full plot and premise, the main '
        'characters and what each of them wants, the central conflict, the hook that makes viewers binge, the '
        'setting, and how the opening episodes actually unfold. Be concrete and detailed.'
    )
    digest = ''
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=6)
        if digest and len(digest.strip()) > 40:
            print(f'[analyze-drama] web ok for {title!r}', flush=True)
        else:
            digest = ''
    except Exception as e:
        print(f'[analyze-drama] web failed ({e.__class__.__name__})', flush=True)
        digest = ''
    if not digest:
        digest = claude_ask_quality(research_prompt, system=_IDEAS_RESEARCH_SYSTEM) or ''
        print(f'[analyze-drama] fallback for {title!r}', flush=True)
    fmt_prompt = (
        f'Here is research about the short drama "{title}":\n\n{digest}\n\n'
        'Produce a DETAILED breakdown as STRICT JSON. detailed_synopsis must be rich (4-6 sentences) and '
        'concrete enough to build the SETUP across the first 5 episodes from it. first_5_episodes = exactly 5 '
        'entries, one per episode, each a concrete beat ending on a cliffhanger. Output ONLY the JSON, no prose, '
        f'and do NOT use the double-quote character inside any value:\n{_DRAMA_ANALYSIS_SCHEMA}'
    )
    raw = claude_ask_quality(fmt_prompt, system='You output ONLY strict valid JSON — no prose, no code fences, no commentary.') or ''
    data = loads_lenient(strip_json(raw))
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError('analyze-drama: no object parsed')
    eps = data.get('first_5_episodes') or []
    if not isinstance(eps, list):
        eps = []
    chars = data.get('main_characters') or []
    if not isinstance(chars, list):
        chars = []
    return {
        'title': (data.get('title') or title).strip(),
        'detailed_synopsis': (data.get('detailed_synopsis') or '').strip(),
        'detailed_synopsis_ru': (data.get('detailed_synopsis_ru') or '').strip(),
        'main_characters': [str(c).strip() for c in chars if str(c).strip()],
        'central_conflict': (data.get('central_conflict') or '').strip(),
        'hook': (data.get('hook') or '').strip(),
        'setting': (data.get('setting') or '').strip(),
        'first_5_episodes': [str(e).strip() for e in eps if str(e).strip()],
    }


@app.route('/api/top-dramas', methods=['GET'])
def top_dramas_get():
    """Return the persisted top-dramas board (survives page reload)."""
    return jsonify(_load_top_dramas())


@app.route('/api/top-dramas/scan', methods=['POST'])
def top_dramas_scan():
    """Re-scan the live market for top short dramas and persist the result."""
    data_in = request.json or {}
    genres = data_in.get('genres') or []
    era_dir = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    era_hint = 'a non-modern era/world is set — surface period/world-appropriate hits' if era_dir else ''
    try:
        dramas = _research_top_dramas(genres=genres, idea_hint=(data_in.get('idea') or '').strip(), era_hint=era_hint)
    except Exception as e:
        _log_event('WARN', 'top_dramas_scan_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
    for i, d in enumerate(dramas):
        d['id'] = _drama_slug(d.get('title'), i)
    store = {
        'scanned_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'genres': genres,
        'dramas': dramas,
    }
    _save_top_dramas(store)
    print(f'[top-dramas] scanned + saved {len(dramas)}', flush=True)
    return jsonify(store)


@app.route('/api/top-dramas/analyze', methods=['POST'])
def top_dramas_analyze():
    """Deep-analyze one drama (by stored id or by inline drama) and persist it."""
    data_in = request.json or {}
    did = (data_in.get('id') or '').strip()
    drama = data_in.get('drama') or {}
    store = _load_top_dramas()
    target = None
    if did:
        for d in store.get('dramas', []):
            if d.get('id') == did:
                target = d
                break
    if target is None and isinstance(drama, dict) and (drama.get('title') or drama.get('premise')):
        target = drama
    if not target:
        return jsonify({'error': 'drama not found'}), 404
    try:
        analysis = _analyze_drama(target)
    except Exception as e:
        _log_event('WARN', 'top_dramas_analyze_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
    if did:
        for d in store.get('dramas', []):
            if d.get('id') == did:
                d['analysis'] = analysis
                break
        _save_top_dramas(store)
    return jsonify({'analysis': analysis})


from sw.data.idea_seeds import (
    _IDEA_SETTINGS,
    _IDEA_TWISTS,
    _IDEA_TONES,
    _IDEA_PREMISE_STRUCTURES,
    _IDEA_PROTAG_ARCHETYPES,
    _IDEA_ANTAG_ARCHETYPES,
    _IDEA_AVOID_REPETITIVE_FRAMES,
)
# ─── ERA + WORLD SETTING ──────────────────────────────────────────────────
# Two new axes the user can pick BEFORE generating ideas. The keys come from
# the frontend selects (templates/index.html → #series-era / #series-world).
# Default is always 'modern' + 'realistic' (the most-used combo). For each
# non-default pick we inject a hard directive so EVERY generated idea is
# period/world-consistent (title, character names allowed by era, props,
# technology, social rules). 'custom' uses the user's free-text verbatim.
from sw.data.era_beats import (
    _ERA_CHOICES,
    _ERA_LABELS_RU,
    _WORLD_CHOICES,
    _WORLD_LABELS_RU,
    _BEAT_OPTIONS,
    _BEATS_BY_ID,
    _BEAT_MAX,
)
def _resolve_beats(tokens):
    """Map an ORDERED list of beat tokens (from the create-series modal) to
    ordered dicts {id, ru, beat}. A token is either a known catalog id or a
    free-text custom beat (passed through verbatim). Preserves order, drops
    blanks and exact duplicates, caps at _BEAT_MAX."""
    if not tokens:
        return []
    seen, out = set(), []
    for tok in tokens:
        if isinstance(tok, dict):
            tok = tok.get('id') or tok.get('ru') or tok.get('custom') or ''
        tok = (tok or '').strip()
        if not tok or tok.lower() in seen:
            continue
        seen.add(tok.lower())
        if tok in _BEATS_BY_ID:
            o = _BEATS_BY_ID[tok]
            out.append({'id': o['id'], 'ru': o['ru'], 'beat': o['beat']})
        else:
            # Custom free-text beat — the user's own hook moment.
            out.append({'id': '', 'ru': tok, 'beat': tok})
        if len(out) >= _BEAT_MAX:
            break
    return out


def _beats_ideas_block(tokens):
    """Directive for the 5-ideas / from-idea generators: build every concept so
    the opening arc unfolds through the chosen beats in this EXACT order.
    Returns '' when nothing was picked."""
    picks = _resolve_beats(tokens)
    if not picks:
        return ''
    lines = '\n'.join(f'  {i+1}. {p["ru"]} — {p["beat"]}' for i, p in enumerate(picks))
    first = picks[0]['ru']
    return (
        "━━━ КОНСТРУКТОР СЦЕНАРИЯ — ФУНДАМЕНТ КАЖДОЙ ИДЕИ (СТРОГИЙ ПОРЯДОК) ━━━\n"
        "The user has assembled an ORDERED skeleton of opening hook-beats. Build all 5 concepts so "
        "the story unfolds through these beats in THIS EXACT ORDER — beat 1 is (or directly triggers) "
        "the inciting incident, and each later beat follows in sequence as the opening arc escalates. "
        "Do NOT reorder them and do NOT resolve a later beat before an earlier one.\n"
        f"{lines}\n"
        f"Each synopsis must clearly set up beat 1 («{first}») as the opening hook and gesture at the "
        "escalation to come. Vary setting / protagonist / world across the 5, but every idea rides the "
        "SAME beat order. Honor any era/world/genre constraints above at the same time.\n\n"
    )


def _source_outline_episode_block(s, first_new_num, last_new_num):
    """If the series was created from a deep-analyzed top drama, emit the per-episode
    outline for episodes in [first_new_num, last_new_num], with a hard rename rule so
    the result is an ORIGINAL adaptation (own cast/details), not a copy of the source."""
    outline = s.get('source_episode_outline') or []
    if not isinstance(outline, list) or not outline:
        return ''
    lines = []
    for i, beat in enumerate(outline):
        ep = i + 1
        if not beat or ep < first_new_num or ep > last_new_num:
            continue
        lines.append(f"Эп.{ep}: {beat}")
    if not lines:
        return ''
    return (
        "\n📋 ПЛАН-ЗАВЯЗКА ПО СЕРИЯМ (адаптация успешной шорт-драммы — следуй сюжетным битам как ОСНОВЕ каждой серии):\n"
        + "\n".join(lines) + "\n"
        "⚠ ЭТО ОРИГИНАЛЬНАЯ АДАПТАЦИЯ, НЕ КОПИЯ:\n"
        "• Имена персонажей в плане выше — ПЛЕЙСХОЛДЕРЫ исходника. Используй ТОЛЬКО имена из РОСТЕРА ПЕРСОНАЖЕЙ этого сериала (или придумай свои) — НИКОГДА не переноси имена из плана.\n"
        "• Сохраняй сюжетные биты, повороты и клиффхэнгер каждой серии, но меняй мелкие конкретные детали (места, бренды, обстоятельства), чтобы это была своя история, а не пересказ.\n"
        "• Каждая из этих серий ОБЯЗАНА реализовать свой бит плана в правильном порядке.\n\n"
    )


def _series_beats_episode_block(s):
    """Block injected into episode generators so the opening episodes deliver the
    stored beat sequence IN ORDER, then hand off to free improvisation once the
    last beat has happened. Free-paced: 1-3 episodes per beat, writer decides.
    Returns '' when the series has no stored beat sequence."""
    picks = _resolve_beats(s.get('beat_sequence') or [])
    if not picks:
        return ''
    lines = '\n'.join(f'  {i+1}. {p["ru"]} — {p["beat"]}' for i, p in enumerate(picks))
    last = picks[-1]['ru']
    return (
        "\n━━━ КОНСТРУКТОР СЦЕНАРИЯ — КОСТЯК ОТКРЫВАЮЩИХ СЕРИЙ (СТРОГИЙ ПОРЯДОК) ━━━\n"
        "При создании сериала задана упорядоченная последовательность хук-нод. Открывающие серии "
        "ОБЯЗАНЫ проходить их строго в этом порядке:\n"
        f"{lines}\n"
        "ПРАВИЛА:\n"
        "• Иди по нодам ПО ПОРЯДКУ. Не переставляй, не пропускай, не отыгрывай позднюю ноду раньше ранней.\n"
        "• Темп свободный: на одну ноду может уйти 1-3 серии — полностью отыграй (заверши) текущую ноду, "
        "прежде чем переходить к следующей.\n"
        "• Смотри предыдущие серии: определи, какие ноды уже отыграны, и продолжай со следующей неотыгранной.\n"
        f"• Как только отыграна ПОСЛЕДНЯЯ нода («{last}») — костяк закончился: дальше пиши свободно, "
        "импровизируй как обычно (открытые линии, эмоциональные арки, неожиданные повороты).\n\n"
    )



# Map the create-series modal era/world picks to an asset-generation era_choice
# (a key in _ERA_GUIDES, or 'modern'). Returned value is stored on the series so
# character/portrait generation uses the right period WITHOUT re-asking the user
# via the confirmation banner. Returns None when the pick is ambiguous/custom —
# then we leave era_choice='auto' and the normal detection+banner flow applies.
def _modal_setting_to_era_choice(era_key, era_custom, world_key, world_custom, synopsis_text=''):
    era_key   = (era_key or 'modern').strip().lower()
    world_key = (world_key or 'realistic').strip().lower()
    # World axis dominates when non-realistic and maps cleanly to a guide.
    world_map = {'fantasy': 'fantasy', 'scifi': 'sci_fi', 'postapoc': 'post_apocalyptic'}
    if world_key in world_map:
        return world_map[world_key]
    # supernatural / dystopian worlds: clothing is usually modern-or-era-driven —
    # fall through to the era axis (no forced world guide).
    era_map = {
        'modern': 'modern', 'near_future': 'sci_fi', 'far_future': 'sci_fi',
        '1980s': '80s', '1950s': '1950s', '1920s': 'edwardian_20s',
        'victorian': 'victorian', 'medieval': 'medieval',
    }
    if era_key in era_map:
        return era_map[era_key]
    if era_key == 'ancient':
        # Antiquity is ambiguous (Egypt / Greece / Rome). Sniff the synopsis;
        # default to Rome (togas) which reads as generic antiquity.
        hay = (synopsis_text or '').lower()
        if any(k in hay for k in ('egypt', 'pharaoh', 'nile', 'египет', 'фараон', 'нил')):
            return 'ancient_egypt'
        if any(k in hay for k in ('greece', 'greek', 'sparta', 'athen', 'грец', 'спарт', 'афин')):
            return 'ancient_greece'
        return 'ancient_rome'
    if era_key == '__custom__':
        # Try to recognize the free text against the asset-era keyword table.
        hay = f"{era_custom} {world_custom}".lower()
        for era, kws in _ERA_KEYWORDS.items():
            if any(re.search(rf'\b{re.escape(k)}\b', hay, re.UNICODE) for k in kws):
                return era
        return None  # unknown custom → leave to auto-detect + banner
    return None

def _era_setting_block(era_key: str, era_custom: str, world_key: str, world_custom: str) -> str:
    """Build the period/world directive injected into idea generation. Returns
    '' for the default modern+realistic combo (no directive needed — the base
    prompts already assume that world). For any non-default pick, returns a
    HARD directive every idea must obey."""
    era_key   = (era_key or 'modern').strip()
    world_key = (world_key or 'realistic').strip()
    era_custom   = (era_custom or '').strip()
    world_custom = (world_custom or '').strip()

    era_desc = None
    if era_key == '__custom__' and era_custom:
        era_desc = f'CUSTOM ERA defined by the user: "{era_custom}". Honor it precisely — period-correct props, technology, clothing, social rules.'
    elif era_key in _ERA_CHOICES and era_key != 'modern':
        era_desc = _ERA_CHOICES[era_key]

    world_desc = None
    if world_key == '__custom__' and world_custom:
        world_desc = f'CUSTOM SETTING defined by the user: "{world_custom}". Build every idea inside this world.'
    elif world_key in _WORLD_CHOICES and world_key != 'realistic':
        world_desc = _WORLD_CHOICES[world_key]

    if not era_desc and not world_desc:
        return ''  # default modern + realistic — no directive needed

    lines = ['━━━ ERA & WORLD SETTING — MANDATORY FOR EVERY IDEA ━━━']
    if era_desc:
        lines.append(f'TIME PERIOD: {era_desc}')
    if world_desc:
        lines.append(f'WORLD TYPE: {world_desc}')
    lines.append(
        'EVERY one of the 5 ideas MUST be set in this period/world — no exceptions, no "modern day" slip-ups. '
        'Props, technology, clothing, professions, social rules, and the plot engine must all be period/world-correct. '
        'Character names must fit the era (no anachronistic names). '
        'The `world_description` field MUST open by establishing this era/setting explicitly, and `synopsis` / `synopsis_ru` must read as belonging to it. '
        'Do NOT let a banned modern device (smartphone, social media, DNA test, etc.) sneak in if the era predates it — '
        'translate the same beat into a period-correct equivalent (an overheard confession, a returning letter-bearer, a witness).\n'
    )
    return '\n'.join(lines) + '\n'


# ─── ANTI-MONOTONY: thriller cap + freshness directive ────────────────────
# The user reported (a) too many thrillers / psychological thrillers and
# (b) the 5 synopses feel too similar and not interesting. This block is
# injected into idea generation to force genre spread and punchier hooks.
_IDEA_ANTI_MONOTONY = (
    "━━━ GENRE SPREAD — HARD CAP ON THRILLER ━━━\n"
    "AT MOST 1 of the 5 ideas may be a thriller / psychological-thriller / suspense / crime-mystery. "
    "The OTHER 4 must each have a CLEARLY DIFFERENT primary genre — pick from: romance, "
    "betrayal/affair melodrama, revenge, Cinderella/rags-to-riches, found-family, comedy/dramedy, "
    "forbidden love, second-chance, scandal, family-secrets, power-struggle, coming-of-age. "
    "If you notice 2+ ideas drifting into 'dark / tense / someone is hiding a deadly secret / "
    "she's being watched' territory — rewrite all but one into a warmer, more emotional, or more "
    "romantic register. Variety of FEELING across the 5 is as important as variety of plot.\n\n"
    "━━━ ANTI-SAMENESS CHECK (the 5 must NOT feel interchangeable) ━━━\n"
    "Before output, read all 5 synopses as a set. If swapping two protagonists' names would make "
    "the synopses interchangeable — they are too similar; rewrite. Each idea must differ on AT "
    "LEAST THREE of: setting, era-flavor, primary emotion, who holds power, the central relationship, "
    "and the engine (love vs revenge vs survival vs mystery vs comedy). "
    "No 'boring' or generic premises — every synopsis must contain ONE concrete, surprising, "
    "specific detail that makes a viewer stop scrolling. Vague = rejected.\n\n"
)

# ─── ROMANCE / AFFAIR / INTIMACY — periodic, organic ──────────────────────
# The user noted that affairs, kisses, passion, betrayal-of-the-heart never
# happen. We want these to show up REGULARLY but organically (not forced into
# every idea). Stays within the video generator's content bounds: on-screen
# kissing / embracing / passion / charged tension are allowed; explicit sexual
# acts and nudity are NOT — intimacy beyond a kiss is implied off-screen
# (cut-to-black, morning-after). This directive shapes idea generation.
_IDEA_ROMANCE_DIRECTIVE = (
    "━━━ ROMANCE, DESIRE & BETRAYAL — BUILD THEM IN ━━━\n"
    "Short drama runs on the heart. Across the 5 ideas, romantic/sexual tension and betrayal of "
    "the heart should be present and VISIBLE — not sanitized away:\n"
    "  • At least 2-3 of the 5 ideas must carry a real romantic or desire-driven thread "
    "(attraction, a forbidden pull, a slow-burn, a marriage with real heat, a love triangle).\n"
    "  • At least 1 of the 5 should center on or prominently feature INFIDELITY / an AFFAIR — "
    "a cheating spouse, an emotional affair discovered, a partner caught with someone else, "
    "the 'other woman/man' POV, or a marriage cracking from a betrayal of the heart. "
    "This is a core melodrama engine that has been missing — use it.\n"
    "  • Make passion concrete: a stolen kiss, a charged near-miss, a confrontation about a "
    "betrayal, a one-night entanglement with consequences. These belong in the world_description "
    "and synopsis where the premise calls for them.\n"
    "  • CONTENT BOUND: kissing, embracing, passion, attraction and affairs are all fair game on "
    "screen. Explicit sexual acts / nudity are NOT depicted — intimacy beyond a kiss is implied "
    "(a closing door, a morning-after). Write to that line, don't write past it.\n\n"
)

# ─── ENGINE-FAMILY SPREAD — variety by round-robin, NOT by banning ────────
# The user is sick of the «I scrub floors» / «hired as a nanny» / secret-heiress
# / fake-marriage sameness, but does NOT want those tropes banned — they want
# the 5 ideas to come from DIFFERENT families so any one trope appears at most
# once and naturally dissolves into a varied batch.
_IDEA_FAMILY_SPREAD = (
    "━━━ ENGINE VARIETY — EACH OF THE 5 FROM A DIFFERENT FAMILY ━━━\n"
    "Assign each of the 5 ideas to a DIFFERENT story-engine family. Use each family AT MOST ONCE "
    "so no single trope dominates the batch:\n"
    "  A) service-job + hidden truth (maid / nanny / janitor / waitress / driver whose real "
    "identity or power no one knows)\n"
    "  B) fake / contract / substitute / arranged marriage\n"
    "  C) revenge or comeback after being wronged / fall-from-grace\n"
    "  D) affair / infidelity / forbidden desire / love triangle\n"
    "  E) survival / trapped-together / disaster / pressure-cooker\n"
    "  F) mystery / single case / whodunit / something doesn't add up\n"
    "  G) found-family / unlikely alliance forming\n"
    "  H) rivalry / power struggle / hostile takeover / sabotage from within\n"
    "  I) second-chance / reunion / a presumed-dead person returns\n"
    "  J) identity reveal / body-or-life swap / mistaken for someone else\n"
    "RULE: pick 5 DIFFERENT families. Families A and B (service-job-secret and the marriage "
    "tropes) are the MOST overused — together they may appear AT MOST ONCE total across the 5. "
    "Never open more than one synopsis with a menial-job-secret setup, and never start a Russian "
    "synopsis with «мою полы» / «устроилась няней» / «вышла замуж за». The remaining ideas must "
    "come from the fresher families (C–J). (If the user selected specific genres, keep the genre "
    "but still vary the family within it.)\n\n"
)

# ═══════════════════════════════════════════════════════════════════════════
# SETTING-SPECIFIC PREMISE POOLS
# ═══════════════════════════════════════════════════════════════════════════
# Distilled from research into REAL hit vertical short-dramas (ReelShort,
# DramaBox, GoodShort, ShortMax, Chinese 短剧). Each entry is a concrete,
# build-ready story ENGINE — not a vague theme. When the user picks a
# non-default era/world, we sample from the matching pool and inject these as
# POSITIVE seeds, instead of leaving the model to lazily reskin its modern
# defaults (which produced "moping floors / hired as a nanny" sameness for
# every setting). Variety here is the whole point — they are deliberately
# different from each other in protagonist, injustice, relationship, and twist.
# ─────────────────────────────────────────────────────────────────────────

from sw.data.premise_pools import (
    _POOL_SUPERNATURAL,
    _POOL_FANTASY,
    _POOL_SCIFI,
    _POOL_POSTAPOC,
    _POOL_DYSTOPIAN,
    _POOL_ANCIENT_DYNASTIC,
    _POOL_MEDIEVAL,
    _POOL_VICTORIAN,
    _POOL_1920S,
    _POOL_1950S,
    _POOL_PERIOD_GENERAL,
    _POOL_MODERN_EXTRA,
    _CUSTOM_SETTING_KEYWORDS,
    _keyword_pool,
    _resolve_setting_premise_pool,
    _setting_premise_seeds_block,
)
@app.route('/api/story-beats', methods=['GET'])
def list_story_beats():
    """Catalog of curated hook-beats (ноды) for the create-series scenario
    constructor. Returns id + RU chip label + RU group section (the English
    `beat` directive stays server-side — only used inside prompts)."""
    return jsonify([
        {'id': o['id'], 'ru': o['ru'], 'group': o['group']}
        for o in _BEAT_OPTIONS
    ])


@app.route('/api/generate-series-ideas', methods=['POST'])
def generate_series_ideas():
    data_in = request.json or {}
    writer_model = _resolve_writer_model(data_in)
    genres = data_in.get('genres') or []
    idea_hint = (data_in.get('idea') or '').strip()  # optional free-text from the idea input field
    # Format mode: 'short_drama' (TikTok addictive serial) or 'instagram_series' (sitcom-style standalone).
    format_mode = (data_in.get('format_mode') or 'short_drama').strip().lower()
    if format_mode not in _FORMAT_MODE_RULES:
        format_mode = 'short_drama'
    format_block = _format_mode_block(format_mode)
    format_ideas_directive = _FORMAT_MODE_RULES[format_mode]['ideas_directive']
    # Free-text avoid-list: user-curated tropes/words that must NOT appear in
    # any of the 5 ideas (titles, synopses, character roles). Comma-separated
    # or newline-separated. E.g. «близнецы, пастор, billionaire CEO».
    avoid_raw = (data_in.get('avoid') or '').strip()

    # Era + world setting (picked in the create-series modal before generating).
    # Defaults: modern + realistic → _era_setting_block returns '' (no directive).
    era_setting_directive = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    # Scenario constructor: ORDERED hook-beats (ноды) picked in the modal — the
    # 5 ideas must unfold through them in this exact order.
    beats_directive = _beats_ideas_block(data_in.get('beats') or [])

    # Detect non-standard format from the idea hint to avoid injecting human-drama seeds
    _idea_lower = idea_hint.lower()
    _nonstandard_format = idea_hint and any(w in _idea_lower for w in [
        'мультик', 'мульт', 'анимац', 'cartoon', 'animated', 'anime', 'аниме',
        'pixar', 'пиксар', 'furry', 'фури', 'фурри', 'fantasy', 'фэнтези',
        'sci-fi', 'science fiction', 'космос', 'space', 'horror', 'хоррор',
        'superhero', 'супергерой', 'игра', 'game', 'видеоигр',
    ])

    if genres:
        genre_rule = (
            f"STRICT GENRE REQUIREMENT: Every single one of the 5 ideas MUST incorporate ALL of these genres: {', '.join(genres)}.\n"
            f"This is not optional. Each idea must feel like a genuine mix of {' + '.join(genres)}.\n"
            "If an idea does not fit ALL selected genres, replace it — do not submit it.\n\n"
        )
    else:
        genre_rule = ""

    # Build avoid-rule. Split user's text into tokens, normalise, and pass as
    # a HARD ban list. Claude is told to reject ideas that contain any of
    # these words/concepts and regenerate.
    avoid_rule = ""
    if avoid_raw:
        # Accept commas, newlines, semicolons, slashes. Drop empties + dedup.
        tokens = [t.strip() for t in re.split(r'[,;\n/]+', avoid_raw) if t.strip()]
        if tokens:
            ban_list = ', '.join(f'«{t}»' for t in tokens[:40])
            avoid_rule = (
                f"HARD BAN LIST (пользователь устал от этих троп — НИ ОДНА из 5 идей НЕ должна содержать эти концепты):\n"
                f"{ban_list}\n"
                "Проверяй title, synopsis_ru, и все ключевые роли/архетипы каждой идеи. Если идея содержит "
                "что-то из бан-списка (даже если только в подтексте архетипа) — выбрось её и сгенери замену.\n"
                "Распознавай синонимы и переводы: если бан = «pastor», то «cleric / priest / preacher / "
                "religious leader / cult founder» тоже под запретом. Если бан = «близнецы», то «twin / "
                "doppelganger / mirror sibling / identical» тоже.\n\n"
            )

    # Six-axis sampling — produces ~ millions of unique combos so back-to-back
    # batches don't repeat. Each idea gets ONE pick from every axis.
    seed_settings   = random.sample(_IDEA_SETTINGS, 5)
    seed_twists     = random.sample(_IDEA_TWISTS, 5)
    seed_tones      = random.sample(_IDEA_TONES, 5)
    seed_premises   = random.sample(_IDEA_PREMISE_STRUCTURES, 5)
    seed_protag     = random.sample(_IDEA_PROTAG_ARCHETYPES, 5)
    seed_antag      = random.sample(_IDEA_ANTAG_ARCHETYPES, 5)
    constraints = '\n'.join(
        f'{i+1}. Setting: {seed_settings[i]}\n'
        f'   Premise structure: {seed_premises[i]}\n'
        f'   Twist element: {seed_twists[i]}\n'
        f'   Protagonist archetype: {seed_protag[i]}\n'
        f'   Antagonist archetype: {seed_antag[i]}\n'
        f'   Mood: {seed_tones[i][0]}'
        for i in range(5)
    )
    format_convention = _get_format_convention(idea_hint)
    # Whether a non-default era/world was picked (post-apoc, 1920s, fantasy…).
    # When it is, the concrete modern-drama seeds (corporate boardroom, forensic
    # accountant, podcast confession, tech billionaire…) actively FIGHT the era
    # directive: they are far more vivid/specific than the abstract "make it
    # post-apocalyptic" line, so the model anchors on them and drifts straight
    # back into modern realism. So we suppress those seeds and keep only mood,
    # exactly like the non-standard-format path does.
    _era_world_override = bool(era_setting_directive)
    # For non-standard formats (animation, furry, sci-fi, etc.) the human-drama seeds
    # are irrelevant — replace them with just mood seeds to avoid archetype contamination.
    if _nonstandard_format:
        mood_seeds = '\n'.join(f'{i+1}. Mood: {seed_tones[i][0]}' for i in range(5))
        seeds_section = (
            f"USER FORMAT BRIEF (PRIMARY — все 5 идей ОБЯЗАНЫ соответствовать этому формату): \"{idea_hint}\"\n\n"
            + (format_convention if format_convention else
               "ВАЖНО: формат пользователя определяет всё — жанр, сеттинг, архетипы персонажей. "
               "НЕ используй человеческие drama-архетипы (CEO, горничная, мачеха, миллиардер) если только "
               "идея пользователя явно не включает людей. Придумывай архетипы исходя из заданного формата.\n")
            + "\nMOOD SEEDS (один на идею):\n"
            f"{mood_seeds}\n\n"
        )
    elif _era_world_override:
        # Era/world picked → the modern-drama seeds would contaminate. Replace
        # them with CURATED era/world premise seeds (real hit-drama engines that
        # actually fit the period/world) + mood, plus an instruction to invent
        # era-appropriate settings/professions/antagonists rather than reskin.
        mood_seeds = '\n'.join(f'{i+1}. Mood: {seed_tones[i][0]}' for i in range(5))
        premise_seeds = _setting_premise_seeds_block(
            data_in.get('era'), data_in.get('era_custom'),
            data_in.get('world_setting'), data_in.get('world_custom'),
        )
        seeds_section = (
            (f"USER IDEA HINT (учти при генерации): \"{idea_hint}\"\n\n" if idea_hint else "")
            + "⚠ The ERA & WORLD SETTING above is the PRIMARY constraint — it overrides everything else.\n"
            "Do NOT reuse stock modern-day short-drama settings or roles (corporate boardroom, CEO, "
            "billionaire, nanny, hospital ER, podcast, social-media scandal, forensic accountant, etc.) "
            "unless they genuinely exist in the chosen era/world. INVENT settings, professions, social "
            "structures, props and antagonists that BELONG to that period/world. Keep the same emotional "
            "DNA of short drama (humiliation, betrayal, power flip, forbidden love, revenge) but dress every "
            "beat in era/world-correct clothing.\n\n"
            + premise_seeds
            + "MOOD SEEDS (one per idea — pair each with a different premise seed above):\n"
            f"{mood_seeds}\n\n"
        )
    else:
        seeds_section = (
            (f"USER IDEA HINT (учти при генерации): \"{idea_hint}\"\n\n" if idea_hint else "")
            + "INSPIRATION SEEDS (one per idea — these are LIGHT prompts, pick what's useful, "
            "ignore what overcomplicates):\n"
            f"{constraints}\n\n"
        )

    # ── IDEAS V2: 3-stage pipeline (research → 5 diverse ideas → logic-check) ──
    # Reuses every context block computed above so all user controls still steer.
    # On any failure, falls through to the legacy single mega-prompt below.
    if IDEAS_V2:
        _user_controls = (
            format_block
            + f"FORMAT-SPECIFIC DIRECTIVE: {format_ideas_directive}\n\n"
            + era_setting_directive
            + beats_directive
            + genre_rule
            + avoid_rule
            + seeds_section
        )
        try:
            _ideas = _run_ideas_pipeline_v2(
                writer_model=writer_model,
                user_controls=_user_controls,
                genres=genres,
                idea_hint=idea_hint,
                era_world_override=_era_world_override,
                avoid_rule=avoid_rule,
            )
            try:
                _save_ideas_history(_ideas)
            except Exception:
                pass
            return jsonify(_ideas)
        except Exception as _e:
            print(f'[ideas] V2 pipeline failed -> legacy single-call fallback: {_e}', flush=True)

    prompt = (
        "Generate exactly 5 series concepts for short-form vertical video.\n\n"
        + format_block
        + f"FORMAT-SPECIFIC DIRECTIVE: {format_ideas_directive}\n\n"
        + era_setting_directive
        + beats_directive
        # Anti-monotony (thriller cap + sameness check) and the romance/affair
        # directive only apply in free-creative mode. When the user has
        # explicitly picked genres OR a beat sequence they're steering on purpose —
        # don't override (capping thrillers / forcing variety would fight a
        # deliberate "Thriller" pick or the chosen ordered beat skeleton).
        + (_IDEA_ANTI_MONOTONY if (not _nonstandard_format and not genres and not beats_directive) else "")
        + (_IDEA_ROMANCE_DIRECTIVE if (not _nonstandard_format and not genres and not beats_directive) else "")
        # Engine-family spread enforces variety (each of 5 from a different
        # family, overused tropes capped at 1) WITHOUT banning anything. Applies
        # even when genres are picked — it varies the family within the genre.
        # Suppressed when a beat sequence is picked: forcing 5 different families
        # would contradict "all 5 ride the SAME ordered beat skeleton".
        + (_IDEA_FAMILY_SPREAD if (not _nonstandard_format and not beats_directive) else "")
        + genre_rule
        + avoid_rule
        + seeds_section
        + "How to use the seeds:\n"
        "- Treat each row as 6 OPTIONAL ingredients. Pick 2-3 that combine cleanly into ONE simple premise.\n"
        "- IGNORE seeds that would force complexity. Better a clean «setting + protagonist + 1 twist» than\n"
        "  a Frankenstein with every seed jammed in.\n"
        "- The synopsis must read like one clear hook (see SYNOPSIS RULES + COMPLEXITY HARD CAPS in system).\n"
        "- DO NOT stack the seeds into one nested backstory. Simpler is always better.\n\n"
        "Diversity check before output (mandatory):\n"
        "- No two ideas may share the same setting category (urban-elite vs blue-collar vs institutional vs road/island vs creative-niche).\n"
        "- No two ideas may use the same TITLE TEMPLATE — pick from different rows of the title rules above.\n"
        "- AT LEAST 2 of the 5 must NOT center primarily on a romantic relationship as the engine — pick revenge, mystery, found-family, custody, or comeback as the spine.\n"
        "- AT LEAST 1 idea must use a non-billionaire/non-CEO/non-mafia antagonist.\n"
        "- If two ideas feel like reflavoured copies — rewrite one with a different premise structure.\n\n"
        "SIMPLICITY CHECK before output (mandatory — re-read each synopsis):\n"
        "- Could you describe the show in ONE sentence to a friend? If no, it's too tangled — strip back.\n"
        "- Count named characters per synopsis. Strictly ≤ 3. More = simplify.\n"
        "- Count «turns out / actually / and also» phrases. Strictly ≤ 1 per synopsis.\n"
        "- If the protagonist has 2+ jobs/roles stacked («ex-cartel-accountant in witness protection who is\n"
        "  also a rival surgeon») — strip to ONE identity.\n"
        "- Count em-dashes («—»). ≤ 2 per synopsis. More = stacking.\n\n"
        "Rules:\n"
        "- All titles and English fields must be in English\n"
        "- synopsis_ru must be in Russian — short (2-3 sentences), vivid, makes you want to watch\n"
        "- No generic titles. No predictable plots. Surprise me — but stay SIMPLE.\n\n"
        # FINAL era/world reinforcement — placed last on purpose: later
        # instructions dominate, and this is the rule that kept getting ignored
        # (post-apoc / 1920s ideas drifting back to plain modern-day synopses).
        + (
            "🚨 FINAL CHECK — ERA & WORLD (do this LAST, before returning JSON):\n"
            "Re-read all 5 synopses. ANY synopsis that reads like a present-day realistic story — "
            "or that contains a prop/role/event impossible in the chosen era/world (smartphone, social "
            "media, DNA test, modern corporation, etc. when the era predates them; or a mundane modern "
            "setting when a fantasy/post-apocalyptic/sci-fi world was chosen) — is WRONG. Rewrite it from "
            "scratch so the era/world is unmistakable in the first sentence. The chosen ERA & WORLD SETTING "
            "is non-negotiable and applies to ALL 5 ideas.\n\n"
            if _era_world_override else ""
        )
        + f"Return JSON matching this schema:\n{_IDEAS_SCHEMA}"
    )
    try:
        data = json.loads(strip_json(llm_ask(writer_model, prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data.get('ideas', data))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


from sw.data.format_modes import (
    _FROM_IDEA_ANGLES,
    _FORMAT_CONVENTIONS,
    _get_format_convention,
    _FORMAT_MODE_RULES,
)
from sw.story_prompts import (
    _format_mode_of,
    _format_mode_block,
)
@app.route('/api/generate-series-from-idea', methods=['POST'])
def generate_series_from_idea():
    data_in = request.json or {}
    writer_model = _resolve_writer_model(data_in)
    idea   = data_in.get('idea', '').strip()
    genres = data_in.get('genres') or []
    if not idea:
        return jsonify({'error': 'Опиши идею'}), 400
    # Format mode: 'short_drama' (default) or 'instagram_series'
    format_mode = (data_in.get('format_mode') or 'short_drama').strip().lower()
    if format_mode not in _FORMAT_MODE_RULES:
        format_mode = 'short_drama'
    format_block = _format_mode_block(format_mode)
    format_ideas_directive = _FORMAT_MODE_RULES[format_mode]['ideas_directive']
    era_setting_directive = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    beats_directive = _beats_ideas_block(data_in.get('beats') or [])
    angle    = random.choice(_FROM_IDEA_ANGLES)
    setting  = random.choice(_IDEA_SETTINGS)
    twist    = random.choice(_IDEA_TWISTS)
    premise  = random.choice(_IDEA_PREMISE_STRUCTURES)
    protag   = random.choice(_IDEA_PROTAG_ARCHETYPES)
    antag    = random.choice(_IDEA_ANTAG_ARCHETYPES)
    tone     = random.choice(_IDEA_TONES)[0]
    genre_rule = (
        f"The series MUST be a blend of these genres: {', '.join(genres)}. "
        "Every element of the concept should feel like it belongs in all of them simultaneously. "
    ) if genres else ""
    # Detect if the idea specifies a non-standard format (animation, anime, furry, etc.)
    # so we know whether the human-drama seeds are relevant or should be skipped.
    _idea_lower = idea.lower()
    _nonstandard_format = any(w in _idea_lower for w in [
        'мультик', 'мульт', 'анимац', 'cartoon', 'animated', 'anime', 'аниме',
        'pixar', 'пиксар', 'furry', 'фури', 'фурри', 'fantasy', 'фэнтези',
        'sci-fi', 'science fiction', 'космос', 'space', 'horror', 'хоррор',
        'superhero', 'супергерой', 'игра', 'game', 'видеоигр',
    ])
    format_convention = _get_format_convention(idea)
    # Era/world picked → suppress the modern-drama seeds (Setting/Protagonist/
    # Antagonist) which otherwise drag the concept back to present-day realism.
    _era_world_override = bool(era_setting_directive)

    if _nonstandard_format:
        # Seeds are for human drama archetypes — skip them entirely when format is non-standard.
        # Let the idea brief + genre convention dominate completely.
        seeds_block = (
            f"Creative angle to explore: {angle}\n"
            f"Mood / emotional register: {tone}\n\n"
            + (format_convention if format_convention else
               "NOTE: The user's idea defines a specific format (animation, fantasy, sci-fi, etc.). "
               "Do NOT force human-drama archetypes (CEO, billionaire, maid, stepmother, etc.) into this concept. "
               "Character archetypes, setting, and premise must match the user's stated format.\n")
            + "\n"
        )
    elif _era_world_override:
        # Drop the modern Setting/Protagonist/Antagonist seeds — give curated
        # era/world premise seeds + angle/mood, and instruct the model to invent
        # era/world-appropriate everything rather than reskin a modern story.
        premise_seeds = _setting_premise_seeds_block(
            data_in.get('era'), data_in.get('era_custom'),
            data_in.get('world_setting'), data_in.get('world_custom'),
            n=4,
        )
        seeds_block = (
            f"Creative angle to explore: {angle}\n"
            f"Mood / emotional register: {tone}\n\n"
            "⚠ The ERA & WORLD SETTING above is the PRIMARY constraint. Do NOT reuse stock modern-day "
            "settings or roles (CEO, billionaire, nanny, hospital, podcast, social-media scandal) unless they "
            "genuinely exist in that era/world. INVENT settings, professions, props and antagonists that BELONG "
            "to the chosen period/world, while keeping the emotional DNA of short drama.\n\n"
            + premise_seeds
        )
    else:
        seeds_block = (
            f"Creative angle to explore: {angle}\n\n"
            f"Optional inspiration seeds — use 2-3 that fit cleanly, discard the rest:\n"
            f"  • Setting: {setting}\n"
            f"  • Premise structure: {premise}\n"
            f"  • Twist element: {twist}\n"
            f"  • Protagonist archetype: {protag}\n"
            f"  • Antagonist archetype: {antag}\n"
            f"  • Mood: {tone}\n\n"
        )

    # Adjust synopsis length per format
    _synopsis_len_rule = (
        "synopsis should be 2-3 plain-language sentences (Instagram series format — see rules above)."
        if format_mode == 'instagram_series' else
        "synopsis should be 3-5 sentences summarizing the full series arc."
    )
    prompt = (
        format_block
        + f"FORMAT-SPECIFIC DIRECTIVE: {format_ideas_directive}\n\n"
        + era_setting_directive
        + beats_directive
        + f"USER'S IDEA (PRIMARY BRIEF — honor this above everything else): \"{idea}\"\n\n"
        + genre_rule
        + seeds_block
        + "RULES:\n"
        "- The user's idea is the brief. Seeds and genre tags are SECONDARY creative pressure — "
        "discard any seed that conflicts with what the user described.\n"
        "- The format (animation vs live-action drama vs thriller vs fantasy) must match the user's idea.\n"
        "- TITLE must follow the format-mode title rule above and stay SHORT — 3-7 words, hard cap 8. "
        "One sharp hook, not the whole plot crammed into a run-on sentence.\n"
        "- Avoid generic plots. Give it a title that sets a clear visual expectation.\n\n"
        + (
            "🚨 FINAL CHECK — ERA & WORLD: the concept MUST be unmistakably set in the chosen era/world "
            "(see ERA & WORLD SETTING above), established in the first sentence of world_description and "
            "synopsis. No present-day-realism drift, no anachronistic props.\n\n"
            if _era_world_override else ""
        )
        + "Create a UNIQUE series concept for short-form vertical video that feels fresh and specific. "
        "Return JSON with exactly these fields: "
        "title, genre, tone, target_audience, world_description, synopsis. "
        f"{_synopsis_len_rule}"
    )
    try:
        data = json.loads(strip_json(llm_ask(writer_model, prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
# Generate-to-landmark — convergence-mode generation
# ─────────────────────────────────────────────────────────────────────────────
# When the user clicks "До финала" / "До чекпоинта", we run a sequential pass:
# for each episode in [current_unwritten .. target_episode]:
#   1. Generate / refresh the bridge plan (cached after first call).
#   2. OVERWRITE the episode's synopsis with the bridge beat — this kills the
#      "stale-synopsis vs new-bridge-plan" disconnect that caused the model to
#      keep writing in the old subplot direction.
#   3. Generate the script via the existing pipeline — now the synopsis itself
#      describes the bridge beat, so prev_script pressure can't pull the writer
#      back into the divergent thread.
#
# This is the "max-weight landmark steering" mode the user requested.

# In-memory progress tracker for generate-to-landmark.
# Keyed by sid → {state, start, target, current_ep, completed, total, results, error, started_at, finished_at}
_LANDMARK_PROGRESS = {}
_LANDMARK_LOCK = threading.Lock()


def _landmark_progress_set(sid, **fields):
    with _LANDMARK_LOCK:
        cur = _LANDMARK_PROGRESS.get(sid, {})
        cur.update(fields)
        _LANDMARK_PROGRESS[sid] = cur


def _landmark_progress_get(sid):
    with _LANDMARK_LOCK:
        return dict(_LANDMARK_PROGRESS.get(sid) or {})


def _run_generate_to_landmark_bg(sid, landmark_type, target_ep, start_ep, model):
    """Background worker — does the per-episode generation loop. Mirrors the logic
    that used to run inline in the request handler, but writes progress to
    _LANDMARK_PROGRESS so the UI can poll status.
    """
    try:
        _landmark_progress_set(
            sid,
            state='running',
            landmark_type=landmark_type,
            target_episode=target_ep,
            start_episode=start_ep,
            current_ep=start_ep,
            completed=0,
            total=target_ep - start_ep + 1,
            results=[],
            error=None,
        )
        for n in range(start_ep, target_ep + 1):
            _landmark_progress_set(sid, current_ep=n)
            s_fresh = load_series(sid)
            if not s_fresh:
                _landmark_progress_set(sid, error='series not found mid-run', state='failed',
                                       finished_at=time.time())
                return
            ep = load_episode(sid, n)
            if ep is None:
                ep = {'number': n, 'title': f'Episode {n}', 'synopsis': '', 'script': '', 'status': 'draft'}
                save_episode(sid, n, ep)
            # Bridge plan + synopsis overwrite
            try:
                bridge = build_finale_bridge_plan(s_fresh, n) or {}
            except Exception as be:
                print(f'[generate-to-landmark/bg] ep{n}: bridge_plan FAILED: {be}', flush=True)
                bridge = {}
            this_beat = (bridge.get('this_beat') or '').strip()
            state_before = (bridge.get('this_state_before') or '').strip()
            if this_beat:
                parts = []
                if state_before:
                    parts.append(f'[Состояние мира на старте серии: {state_before}]')
                parts.append(this_beat)
                new_synopsis = ' '.join(parts)
                old_synopsis = (ep.get('synopsis') or '').strip()
                if old_synopsis != new_synopsis:
                    ep.setdefault('synopsis_history', []).append({
                        'ts': time.time(),
                        'reason': f'overwritten by generate-to-landmark ({landmark_type})',
                        'synopsis': old_synopsis[:2000],
                    })
                    ep['synopsis_history'] = ep['synopsis_history'][-10:]
                    ep['synopsis'] = new_synopsis
                    save_episode(sid, n, ep)
                    print(f'[generate-to-landmark/bg] ep{n}: synopsis overwritten from bridge beat', flush=True)
            # Generate script via existing endpoint logic
            try:
                with app.test_request_context(
                    f'/api/series/{sid}/episodes/{n}/generate-script',
                    method='POST',
                    json={'model': model},
                ):
                    resp = generate_episode_script(sid, n)
                payload = resp.json if hasattr(resp, 'json') else {}
                status_code = getattr(resp, 'status_code', 200)
                if status_code >= 400:
                    err_msg = (payload or {}).get('error') or f'HTTP {status_code}'
                    with _LANDMARK_LOCK:
                        cur = _LANDMARK_PROGRESS.get(sid) or {}
                        cur.setdefault('results', []).append({'episode': n, 'ok': False, 'error': err_msg})
                        cur['error'] = err_msg
                        cur['state'] = 'failed'
                        cur['finished_at'] = time.time()
                        _LANDMARK_PROGRESS[sid] = cur
                    print(f'[generate-to-landmark/bg] ep{n}: FAILED — {err_msg}', flush=True)
                    return
                with _LANDMARK_LOCK:
                    cur = _LANDMARK_PROGRESS.get(sid) or {}
                    cur.setdefault('results', []).append({
                        'episode': n,
                        'ok': True,
                        'retries': ((payload or {}).get('audit_report') or {}).get('retries'),
                    })
                    cur['completed'] = cur.get('completed', 0) + 1
                    _LANDMARK_PROGRESS[sid] = cur
                print(f'[generate-to-landmark/bg] ep{n}: OK', flush=True)
            except Exception as e:
                with _LANDMARK_LOCK:
                    cur = _LANDMARK_PROGRESS.get(sid) or {}
                    cur.setdefault('results', []).append({'episode': n, 'ok': False, 'error': str(e)[:300]})
                    cur['error'] = str(e)[:300]
                    cur['state'] = 'failed'
                    cur['finished_at'] = time.time()
                    _LANDMARK_PROGRESS[sid] = cur
                print(f'[generate-to-landmark/bg] ep{n}: EXCEPTION — {e}', flush=True)
                return
        _landmark_progress_set(sid, state='done', current_ep=None, finished_at=time.time())
        print(f'[generate-to-landmark/bg] sid={sid} DONE', flush=True)
    except Exception as outer:
        _landmark_progress_set(sid, state='failed', error=str(outer)[:300], finished_at=time.time())
        print(f'[generate-to-landmark/bg] sid={sid} OUTER EXCEPTION — {outer}', flush=True)


@app.route('/api/series/<sid>/generate-to-landmark/status', methods=['GET'])
def generate_to_landmark_status(sid):
    return jsonify(_landmark_progress_get(sid) or {'state': 'idle'})


@app.route('/api/series/<sid>/generate-to-landmark', methods=['POST'])
def generate_to_landmark(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    landmark_type = (body.get('landmark_type') or '').strip().lower()
    if landmark_type not in ('finale', 'checkpoint'):
        return jsonify({'error': 'landmark_type must be "finale" or "checkpoint"'}), 400

    # Resolve target episode
    if landmark_type == 'finale':
        fin = s.get('finale') or None
        if not fin or not (fin.get('description') or '').strip():
            return jsonify({'error': 'no finale pinned for this series'}), 400
        try:
            target_ep = int(fin.get('episode', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'finale has invalid episode number'}), 400
    else:
        # checkpoint — caller may specify which one; otherwise the NEAREST upcoming.
        cps = sorted(
            [c for c in (s.get('checkpoints') or []) if (c.get('description') or '').strip()],
            key=lambda c: int(c.get('episode', 0) or 0),
        )
        if not cps:
            return jsonify({'error': 'no checkpoints pinned for this series'}), 400
        requested_cp = body.get('landmark_episode')
        if requested_cp:
            try:
                target_ep = int(requested_cp)
                if not any(int(c.get('episode', 0) or 0) == target_ep for c in cps):
                    return jsonify({'error': f'no checkpoint at episode {target_ep}'}), 400
            except (TypeError, ValueError):
                return jsonify({'error': 'landmark_episode must be an integer'}), 400
        else:
            # Pick nearest upcoming checkpoint relative to first unwritten episode
            episodes = sorted(list_episodes(sid), key=lambda e: int(e.get('number', 0) or 0))
            first_unwritten = next((e['number'] for e in episodes if not (e.get('script') or '').strip()),
                                   (episodes[-1]['number'] + 1) if episodes else 1)
            upcoming = [c for c in cps if int(c.get('episode', 0) or 0) >= first_unwritten]
            if not upcoming:
                return jsonify({'error': 'all checkpoints are already past the current episode'}), 400
            target_ep = int(upcoming[0]['episode'])

    # Resolve start episode:
    #   1. If caller passed explicit start_episode → use it (supports overwrite mode)
    #   2. Else: first episode without a script
    #   3. Else: error — there's nothing to do
    episodes = sorted(list_episodes(sid), key=lambda e: int(e.get('number', 0) or 0))
    explicit_start = body.get('start_episode')
    if explicit_start is not None and str(explicit_start).strip() != '':
        try:
            start_ep = int(explicit_start)
            if start_ep < 1:
                return jsonify({'error': 'start_episode must be >= 1'}), 400
        except (TypeError, ValueError):
            return jsonify({'error': 'start_episode must be an integer'}), 400
    else:
        start_ep = None
        for e in episodes:
            if not (e.get('script') or '').strip():
                start_ep = int(e.get('number', 0))
                break
        if start_ep is None:
            return jsonify({
                'error': 'no unwritten episodes — pass start_episode to overwrite from a specific point'
            }), 400

    if target_ep < start_ep:
        return jsonify({'error': f'target ep {target_ep} is before start ep {start_ep}'}), 400
    span = target_ep - start_ep + 1
    MAX_SPAN = 8
    if span > MAX_SPAN:
        return jsonify({
            'error': f'span too large ({span} episodes) — max {MAX_SPAN} per call. '
                     f'Run multiple times, or move the landmark closer.'
        }), 400

    print(f'[generate-to-landmark] sid={sid} type={landmark_type} target=Ep{target_ep} start=Ep{start_ep} span={span}', flush=True)

    # Refuse if a previous run for this sid is still active
    prev = _landmark_progress_get(sid)
    if prev.get('state') == 'running':
        return jsonify({
            'error': 'a landmark generation is already running for this series',
            'progress': prev,
        }), 409

    # Initialize fresh progress
    _landmark_progress_set(
        sid,
        state='starting',
        landmark_type=landmark_type,
        target_episode=target_ep,
        start_episode=start_ep,
        current_ep=start_ep,
        completed=0,
        total=span,
        results=[],
        error=None,
        started_at=time.time(),
        finished_at=None,
    )

    model = body.get('model') or s.get('writer_model')
    t = threading.Thread(
        target=_run_generate_to_landmark_bg,
        args=(sid, landmark_type, target_ep, start_ep, model),
        name=f'gen-to-landmark-{sid}',
        daemon=True,
    )
    t.start()

    return jsonify({
        'started': True,
        'landmark_type': landmark_type,
        'target_episode': target_ep,
        'start_episode': start_ep,
        'span': span,
        'poll_url': f'/api/series/{sid}/generate-to-landmark/status',
    }), 202


@app.route('/api/series/<sid>/episodes/<int:num>/generate-script', methods=['POST'])
def generate_episode_script(sid, num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'Episode not found'}), 404

    if num > 1:
        prev_ep = load_episode(sid, num - 1)
        if not prev_ep or not prev_ep.get('script', '').strip():
            return jsonify({'error': f'Generate script for episode {num - 1} first'}), 400
        prev_script = prev_ep['script']
    else:
        prev_script = ''

    next_ep = load_episode(sid, num + 1)
    next_synopsis = next_ep.get('synopsis', '') if next_ep else ''
    chars = s.get('characters', [])
    locs  = s.get('locations', [])
    cast_block = _build_cast_block(s, ep)

    batch = is_batch_mode(s)
    bs = batch_size(s)
    _is_finale = is_finale_episode(s, num)
    if batch:
        a, b = chunk_range(s, num)
        unit_label = f'CHUNK {num} (sub-episodes {a}–{b})'
        prev_a, prev_b = chunk_range(s, num - 1) if num > 1 else (0, 0)
        prev_label = f'PREVIOUS CHUNK {num-1} (sub-episodes {prev_a}–{prev_b})'
        next_label = f'NEXT CHUNK {num+1}'
    else:
        unit_label = f'EPISODE {num}'
        prev_label = f'PREVIOUS EPISODE {num-1}'
        next_label = f'NEXT EPISODE {num+1}'

    if prev_script:
        # Surface the FULL previous unit (no truncation) and additionally pull out its last beats
        # into a dedicated ENDING STATE block so the writer cannot accidentally ignore where the
        # previous scene was left.
        prev_lines = [ln for ln in prev_script.splitlines() if ln.strip() and not ln.strip().startswith('━')]
        # Drop the EPISODE NOTES / CHUNK NOTES tail if present
        for stop_kw in ('EPISODE NOTES', 'CHUNK NOTES'):
            if stop_kw in prev_lines:
                prev_lines = prev_lines[:prev_lines.index(stop_kw)]
        tail = '\n'.join(prev_lines[-25:])
        # Last ~10 dialogue lines specifically — these are most likely to be re-staged
        # by a writer who treats "continuation" as "rewind the climax".
        recent_dialogue = []
        for ln in reversed(prev_lines):
            if re.match(r'^\s*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-\']{0,30}:\s', ln.strip()):
                recent_dialogue.insert(0, ln.strip())
                if len(recent_dialogue) >= 10:
                    break
        forbidden_dialogue_block = ''
        if recent_dialogue:
            forbidden_dialogue_block = (
                f'\n═══ FORBIDDEN — DO NOT REPEAT OR PARAPHRASE THESE LINES ═══\n'
                f'These dialogue beats already happened in {prev_label}. The viewer SAW them. '
                f'You may NOT have any character say them again, paraphrase them, or re-deliver '
                f'the same threat / ultimatum / revelation in different words.\n'
                + '\n'.join(recent_dialogue)
                + f'\n═══════════════════════════════════════════════════\n'
            )
        prev_block = (
            f'{prev_label} SCRIPT (FULL — read it all):\n{prev_script}\n\n'
            f'═══ ENDING STATE OF {prev_label} — read this carefully ═══\n'
            f'These are the LAST beats of the previous unit. If they show a scene still in motion '
            f'(someone just arrived, a question hangs in the air, two characters mid-confrontation, '
            f'a reaction shot is the cliffhanger) — THIS unit MUST open in the SAME scene, SAME '
            f'location, SAME characters present, continuing dialogue from the very next beat. '
            f'Do NOT teleport to a new room or "later that day".\n'
            f'{tail}\n'
            f'═══════════════════════════════════════════════════\n'
            f'{forbidden_dialogue_block}\n'
            f'═══ NO PLOT-RECAP — HARD RULE ═══\n'
            f'"Continue from the next beat" means PUSH FORWARD, not REWIND. Events that '
            f'happened in {prev_label} (ultimatums delivered, threats made, revelations, '
            f'character entrances/exits, decisions stated out loud) are DONE. Do NOT have a '
            f'character re-issue the same ultimatum, re-state the same threat, or re-deliver '
            f'the same key line in slightly different words just to "remind" the audience. '
            f'A short drama viewer just watched {prev_label} 3 seconds ago — they remember. '
            f'If the cliffhanger of {prev_label} was "Claire demands he confess on camera or '
            f'she releases the files", THIS unit must show what happens NEXT — Cross\'s '
            f'reaction, Melissa\'s move, an arrival, a counter-twist — NOT Claire saying the '
            f'same demand again. RECAP-AS-OPENING IS A FAILURE STATE.\n'
            f'═══════════════════════════════════════════════════\n\n'
        )
    else:
        prev_block = f'This is the FIRST {("CHUNK" if batch else "EPISODE")} — no previous.\n\n'
    next_block = f'{next_label} SYNOPSIS (for continuity):\n{next_synopsis}\n\n' if next_synopsis else ''

    # ─── PRE-WRITE: rollback canon for this ep (in case of regeneration) and build logic brief
    rollback_canon_for_episode(sid, num)
    try:
        brief = build_logic_brief(sid, num)
    except Exception as e:
        brief = f'(brief unavailable: {e})'
    brief_block = f'═══ LOGIC CONSTRAINTS — MUST RESPECT ALL OF THESE ═══\n{brief}\n═══════════════════════════════════════════════════\n\n'
    try:
        devices_block = _build_plot_device_history(sid, num)
    except Exception:
        devices_block = ''
    try:
        narrative_block = _build_narrative_state_block(sid, num)
    except Exception:
        narrative_block = ''
    try:
        crowd_block = _build_crowd_constraint_block(s)
    except Exception:
        crowd_block = ''
    trajectory_block = build_trajectory_block(s, num)
    # Keep the ordered beat skeleton (ноды) steering the script — prepend so it
    # sits above the per-episode trajectory in the prompt. The opening episodes
    # march through the beats in order, then improvise (see
    # _series_beats_episode_block).
    _beats_block = _series_beats_episode_block(s)
    if _beats_block:
        trajectory_block = _beats_block + trajectory_block
    # Concrete plot bridge from THIS episode to the finale — generated by a quick Haiku
    # planning call. This is the load-bearing fix for "writer ignores the finale":
    # prev_script tends to dominate, so we explicitly tell the writer what THIS episode
    # must do to converge to the finale.
    try:
        bridge_data = build_finale_bridge_plan(s, num) or {}
    except Exception as _be:
        _log_event('WARN', 'bridge_plan_skip', err=str(_be)[:200])
        bridge_data = {}
    bridge_block = bridge_data.get('block', '')
    bridge_this_beat = (bridge_data.get('this_beat') or '').strip()
    bridge_state_before = (bridge_data.get('this_state_before') or '').strip()
    bridge_finale_ep = bridge_data.get('finale_ep')
    # Format-mode block (short_drama vs instagram_series) — drives ending style,
    # cliffhanger requirement, standalone-ness, pacing.
    series_format_mode = _format_mode_of(s)
    format_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])

    # Hard mandatory block for IG-series — every episode of every series MUST
    # open on a hook and close on a cliffhanger, no exceptions, regardless of
    # how soft the synopsis reads. Plus explicit ban on legal-procedural plot
    # engines (lawsuit / court / paperwork) that kill the pace of a 60s show.
    ig_hard_block = (
        '\n╔══════════════════════════════════════════════════════════════════════╗\n'
        '║ INSTAGRAM SERIES — HARD CONTRACT FOR THIS SCRIPT — VIOLATING = FAIL  ║\n'
        '╚══════════════════════════════════════════════════════════════════════╝\n'
        '1. COLD-OPEN HOOK (seconds 0-5) — MANDATORY. Drop the viewer into action / '
        '   conflict / question already in motion. Examples of valid opens:\n'
        '     • Mid-shout / mid-slap / mid-grab\n'
        '     • A line of dialogue that contains a reveal or accusation\n'
        '     • A door slamming open / a phone ringing repeatedly / a body on floor\n'
        '     • A character running mid-stride\n'
        '   FORBIDDEN opens: establishing shot of a building, slow zoom, narration, '
        '   "next morning", "two days later", small-talk warm-up, character making '
        '   coffee / sewing / journaling.\n'
        '2. ESCALATION — every dialogue exchange must reveal, flip, or raise stakes. '
        '   No filler. No "checking in" scenes. No tea-time chat. If a scene does not '
        '   move the plot, delete it.\n'
        '3. CLIFFHANGER on the LAST LINE / LAST FRAME — MANDATORY. Examples of valid '
        '   closes:\n'
        '     • A reveal / name spoken / face seen\n'
        '     • An arrival (door opens, car pulls up, knock)\n'
        '     • A phone showing a message / a photograph\n'
        '     • A weapon raised, a shot fired offscreen\n'
        '     • A line of dialogue that flips everything ("That\'s not my daughter.")\n'
        '   FORBIDDEN closes: character looking at sunset, journaling, smiling, '
        '   resolved hug, "everything will be okay", "and so they...", any wrap-up.\n'
        '4. DROP-IN FRIENDLY — assume the viewer never saw earlier episodes. Within '
        '   the first 10 seconds they must understand WHO this is and WHAT is at '
        '   stake through context (a costume cue, a line, a reaction). No exposition '
        '   dump, no "previously on", no narrator.\n'
        '5. BANNED PLOT ENGINES (none of these may drive the episode):\n'
        '     • lawsuits, court hearings, trials, depositions, judges, lawyers, '
        '       paralegals, legal motions, evidence binders\n'
        '     • eviction filings, paperwork submissions, signing contracts as climax\n'
        '     • tenant unions filing complaints, building inspectors arriving with forms\n'
        '   If the synopsis above leans on any of these — REWRITE the beat into a '
        '   FACE-TO-FACE confrontation, chase, betrayal, reveal, blackmail, escape, '
        '   or physical clash. People doing things to people. Not paperwork.\n'
        '6. DIALOGUE — short and clipped. Two-line clarity: one line states, the next '
        '   flips. No monologues. No "let me explain" speeches. No banter padding.\n'
        '7. LOCATIONS — vary as the plot demands. Do NOT lock the whole episode (or '
        '   the season) to one room. Move where the action goes.\n'
        '═══════════════════════════════════════════════════════════════════════\n\n'
    )

    if batch:
        a, b = chunk_range(s, num)
        if series_format_mode == 'instagram_series':
            # Self-contained chapters of a serialized mini-drama — each sub-ep ends
            # on its own cliffhanger; the chunk overall escalates into chunk+1.
            instruction = (
                ig_hard_block +
                f'Write the complete script for {unit_label}. '
                f'It must contain {bs} self-contained chapters (sub-eps {a} through {b}), each ending with the EXACT cut marker '
                f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
                f'Each sub-episode = SHARP cold-open hook (first 3-5s) → escalating conflict → cliffhanger turn at the end. '
                f'Apply ALL 7 rules of the HARD CONTRACT to every single sub-episode in this chunk. '
                f'The chunk overall ends on the sharpest cliffhanger pulling into chunk {num+1}. '
            )
        else:
            instruction = (
                f'Write the complete script for {unit_label}. '
                f'It must contain {bs} sub-episodes (sub-eps {a} through {b}), each ending with the EXACT cut marker '
                f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
                f'The chunk overall has its own midpoint REVERSAL and a final cliffhanger that leads into chunk {num+1}. '
            )
    else:
        if series_format_mode == 'instagram_series':
            instruction = (
                ig_hard_block +
                f'Write the complete script for Episode {num}. '
                + ('Open on a SHARP COLD-OPEN HOOK in the first 3-5 seconds — drop us into action / conflict / line-mid-confrontation. NO setup, NO establishing shot. ' if not prev_script else
                   'Open on a SHARP COLD-OPEN HOOK tied to where the story left off — drop us back in mid-action. A fresh viewer must catch up in 10 seconds through context, NOT exposition. ')
                + 'Escalate through clipped dialogue — every line reveals, flips, or raises stakes. '
                + ('This is the SERIES FINALE — resolve every thread and end on a CONCLUSIVE final beat (see the FINALE CONTRACT below). Do NOT end on a cliffhanger and ignore HARD CONTRACT rule 3. '
                   if _is_finale else
                   'End on a HARD CLIFFHANGER on the final line / frame. Apply ALL 7 rules of the HARD CONTRACT above.')
            )
        else:
            # Length budget — series-level target_duration_sec (default 60s).
            try:
                _target_sec = int(s.get('target_duration_sec') or 60)
            except (TypeError, ValueError):
                _target_sec = 60
            # Spoken words ≈ 100 words per 60s of TikTok-paced drama (135 wpm
            # speaking rate w/ pauses). Floor at 75% of target — model was
            # observed undershooting to ~50 spoken words on a 100-word target
            # because the prior phrasing was «MAXIMUM, never exceed, cut
            # mercilessly». Now framed as a RANGE the model must HIT, not a
            # ceiling to fear.
            _spoken_target = round(_target_sec / 60 * 100)
            _spoken_floor = round(_spoken_target * 0.75)
            _spoken_ceiling = round(_spoken_target * 1.10)
            # Dialogue lines (each NAME: line) — separate from action.
            _dlg_target = max(3, min(40, round(_target_sec / 4.5)))
            _dlg_floor = max(3, round(_dlg_target * 0.75))
            _dlg_ceiling = _dlg_target + 3
            _action_budget = max(3, round(_target_sec / 12))
            instruction = (
                f'Write the complete script for Episode {num}. '
                + ('Continue naturally from where Episode {prev} ended.'.format(prev=num-1) if prev_script else 'Hook the viewer immediately.')
                + (' This is the SERIES FINALE — resolve every open thread and end conclusively (see the FINALE CONTRACT below); do NOT end on a cliffhanger. ' if _is_finale else ' End on a cliffhanger. ')
                + f'\n\n⚠ БЮДЖЕТ ДЛИНЫ — серия ≈ {_target_sec} секунд экрана.\n'
                + f'\n📣 РЕЧЕВЫЕ СЛОВА (только то, что произносят персонажи — слова после «NAME:» / в кавычках):\n'
                + f'• ЦЕЛЬ: {_spoken_target} слов. ДИАПАЗОН: {_spoken_floor}–{_spoken_ceiling}.\n'
                + f'• НЕ НЕДОБИРАЙ ниже {_spoken_floor} — иначе сцена пустая, аудио короче нужного.\n'
                + f'• НЕ ПРЕВЫШАЙ {_spoken_ceiling} — иначе сцена не влезет в {_target_sec}с.\n'
                + f'• Каждая реплика: 3-8 слов в среднем, максимум 10. >10 слов → РАЗБЕЙ на две короткие реплики.\n'
                + f'• МОНОЛОГОВ НЕТ. Короткие рваные удары. Никаких речей «Your Honor, I would like to explain…».\n'
                + f'\n💬 РЕПЛИКИ (количество строк диалога):\n'
                + f'• ЦЕЛЬ: {_dlg_target} строк диалога. ДИАПАЗОН: {_dlg_floor}–{_dlg_ceiling}.\n'
                + f'\n🎬 ACTION / BLOCKING (описание движения, [BLOCKING] блоки, ремарки):\n'
                + f'• Это ОТДЕЛЬНЫЙ бюджет — НЕ СЧИТАЕТСЯ как речевые слова.\n'
                + f'• Максимум ~{_action_budget} action-строк. Описывай только ключевое движение, не каждое мелкое.\n'
                + f'• [BLOCKING] / [BLOCKING_END] блоки пиши столько сколько нужно — они вне счёта.\n'
                + f'• ЗАПРЕЩЕНЫ time-markers в action-lines: «слушает две минуты молча», «молчит десять секунд», '
                + f'«проходит минута» — это раздувает хронометраж экрана.\n'
                + f'\n✅ ПРОВЕРЬ ПЕРЕД ВЫВОДОМ:\n'
                + f'• Речевых слов в диапазоне {_spoken_floor}–{_spoken_ceiling}? (action и BLOCKING НЕ В СЧЁТ)\n'
                + f'• Строк диалога в диапазоне {_dlg_floor}–{_dlg_ceiling}?\n'
                + f'• Action-строк не больше {_action_budget}?\n'
                + f'• Это короткая драма для TikTok/Reels — НЕ полнометражный сценарий, но и НЕ обрубок на 50 слов.\n'
                + f'• Программный детектор проверит спикерские слова + длину каждой реплики; нарушения → автоматическая перезапись.'
            )

    base_prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Series arc: {s.get("arc","")}\n\n'
        + _anthro_world_block(s)  # ← furry/anthro world directive (empty for human worlds)
        + _revision_instructions_block(s)  # ← clone-time revisions (empty unless cloned w/ edits)
        + trajectory_block        # ← user-pinned finale + checkpoints, FIRST so it's impossible to miss
        + bridge_block            # ← concrete per-episode plan from current state to finale
        + crowd_block             # ← HARD limit on characters per scene, lifted up front
        + format_block
        + (cast_block + '\n\n' if cast_block else '')
        + brief_block
        + devices_block
        + prev_block
        + f'{unit_label} SYNOPSIS (may be stale — see THIS EPISODE\'S BEAT below):\n{ep.get("synopsis","")}\n\n'
        + (
            (
                f'╔══════════════════════════════════════════════════════════════════╗\n'
                f'║ 🎯 THIS EPISODE\'S BEAT — execute exactly this, override synopsis ║\n'
                f'╚══════════════════════════════════════════════════════════════════╝\n'
                + (f'СОСТОЯНИЕ МИРА В НАЧАЛЕ ЭТОЙ СЕРИИ (Ep {num}):\n  {bridge_state_before}\n\n'
                   if bridge_state_before else '')
                + (f'TEMPORAL ANCHOR: финальные события (Ep {bridge_finale_ep or "—"}) ЕЩЁ НЕ ПРОИЗОШЛИ. '
                   f'Не пиши сцены так будто кто-то уже арестован/осуждён/мёртв, если по плану это случится позже.\n\n'
                   if bridge_finale_ep else '')
                + f'Эта серия (Ep {num}) ОБЯЗАНА выполнить следующий beat из плана-моста к финалу:\n\n'
                f'  ▶▶▶ {bridge_this_beat}\n\n'
                f'IF THE SYNOPSIS ABOVE CONTRADICTS THIS BEAT — THE BEAT WINS. The synopsis may have\n'
                f'been generated before the finale was pinned and is now stale. The beat above is\n'
                f'the authoritative instruction. Write the script to deliver THIS BEAT.\n'
                f'Do NOT invent new named characters not in the cast.\n'
                f'Do NOT introduce a subplot that the bridge plan does not include.\n'
                f'Do NOT depict characters in their FINALE-state (arrested, sentenced, exposed) — '
                f'use their CURRENT state from the СОСТОЯНИЕ МИРА block above.\n'
                f'═══════════════════════════════════════════════════════════════════\n\n'
            ) if bridge_this_beat else ''
        )
        + next_block
        + narrative_block
        + instruction
        + ' EVERY constraint in the LOGIC CONSTRAINTS block above is mandatory — violating canon is a hard fail. '
        + 'And EVERY landmark in the NARRATIVE TRAJECTORY block at the top is mandatory — drift from the finale is a hard fail. '
        + ('The THIS EPISODE\'S BEAT above is the authoritative scene direction — execute it. '
           'If the synopsis or prev_script set up a different subplot, fold it into the beat or park it; '
           'NEVER continue a subplot the bridge plan does not include.' if bridge_this_beat else '')
        # ← SERIES FINALE override: appended LAST so it trumps the cliffhanger
        #   mandate above. Empty string for every non-finale episode.
        + build_finale_contract_block(s, num)
    )

    script_system = _build_batch_script_system(s) if batch else _build_script_system(s)

    try:
        # ─── WRITE → AUDIT → RETRY loop (max 2 retries, fully automated)
        MAX_RETRIES = 2
        script = ''
        audit_report = {'passes': True, 'violations': [], 'retries': 0}
        prompt = base_prompt
        for attempt in range(MAX_RETRIES + 1):
            script = claude_ask(prompt, system=script_system)
            report = audit_script(sid, num, script, brief)
            # Run logic-hole auditor in parallel with continuity auditor — different concerns.
            logic_report = audit_logic_holes(sid, num, script)
            # Programmatic check for scene overcrowding — reliable signal that Claude-based
            # auditor sometimes misses. We count NAMED-CAST speakers per scene and flag any
            # scene that exceeds the user's max_main_chars_per_scene setting.
            crowd_violations = detect_scene_overcrowding(s, script)
            crowd_critical = []
            for cv in crowd_violations:
                names_str = ', '.join(cv['characters'])
                crowd_critical.append({
                    'type': 'scene_overcrowding',
                    'severity': 'critical',
                    'where': f"сцена {cv['scene_idx']} ({cv['location']})",
                    'explanation': (
                        f"в сцене {cv['count']} именованных персонажа из каста "
                        f"({names_str}), а лимит сериала = {cv['limit']}. "
                        f"Программный детектор посчитал диалоговые реплики и BLOCKING-разметку."
                    ),
                    'fix': (
                        f"Перепиши сцену так чтобы говорящих/действующих именованных персонажей "
                        f"было НЕ БОЛЕЕ {cv['limit']}. Варианты: (a) убери из сцены лишних персонажей "
                        f"(они могут появиться в ОТДЕЛЬНОЙ последовательной сцене); "
                        f"(b) разбей сцену на две — сначала одна группа, потом другая входит после ухода первой; "
                        f"(c) оставь лишних только в фоне без реплик и без [BLOCKING] упоминания."
                    ),
                })
            if crowd_critical:
                print(f'[scene-crowd] ep {num} attempt {attempt+1}: {len(crowd_critical)} overcrowded scene(s) detected programmatically', flush=True)
                for cv in crowd_violations:
                    print(f'[scene-crowd]   scene {cv["scene_idx"]} ({cv["location"]}): {cv["count"]}>{cv["limit"]} — {", ".join(cv["characters"])}', flush=True)
            # Programmatic no-name-character check — every on-camera/speaking character
            # MUST have a unique proper name + cast-block line so its reference binds.
            # Bare role cues (CLIENT, OLD WOMAN) and uncast named speakers force a rewrite.
            naming_critical = detect_unnamed_characters(s, script)
            if naming_critical:
                print(f'[name-check] ep {num} attempt {attempt+1}: {len(naming_critical)} unnamed/uncast character(s) — '
                      + ', '.join(v['where'] for v in naming_critical), flush=True)
            # ── Programmatic over-length detector — count dialogue lines and spoken words,
            # estimate runtime, fail if >30% over the target duration.
            length_critical = []
            length_violation = detect_script_overlength(s, script)
            if length_violation:
                lv = length_violation
                reasons_str = '; '.join(lv.get('reasons') or [])
                # Branch the fix message based on direction. Undershoot
                # (writer delivered too few spoken words) gets a different
                # corrective than overshoot (writer was too verbose).
                _floor_words = round(lv['target_words'] * 0.75)
                # Undershoot = rendered runtime falls below target (the renderer
                # would produce a clip shorter than the series setting). Word
                # count is the secondary signal.
                _undershoot = (lv['est_sec'] < lv['target_sec'] * 0.9) or (lv['dialogue_words'] < _floor_words)
                if _undershoot:
                    fix_msg = (
                        f"ДОПИШИ диалог до целевого объёма. Конкретно: "
                        f"(a) у тебя сейчас {lv['dialogue_words']} спикерских слов, нужно ~{lv['target_words']} "
                        f"(минимум {_floor_words}) — НЕДОБОР почти в два раза; "
                        f"(b) добавь {lv['target_words'] - lv['dialogue_words']}+ спикерских слов через "
                        f"новые короткие реплики (3-7 слов каждая), НЕ через длинные монологи; "
                        f"(c) ACTION/BLOCKING НЕ СЧИТАЮТСЯ — речь только то, что произносят персонажи "
                        f"после «NAME:» или в кавычках; "
                        f"(d) сцена развивается через диалог — добавь обмены репликами, реакции, "
                        f"подколы, угрозы, признания. НЕ через action-описания «он смотрит на неё»; "
                        + (f"(e) это ФИНАЛ — концовка остаётся конклюзивной (без клиффхэнгера), но к ней ведёт больше реплик."
                           if _is_finale else
                           f"(e) cliffhanger остаётся, но к нему ведёт больше реплик.")
                    )
                else:
                    fix_msg = (
                        f"СОКРАТИ беспощадно. Конкретно: "
                        f"(a) каждая реплика МАКСИМУМ 7 слов, идеал 3-5 (короткие рваные удары); "
                        f"(b) если есть монолог >10 слов — разрежь на короткие реплики ИЛИ удали лишнее; "
                        f"(c) action-строк не больше {max(3, round(lv['target_sec']/12))} — убери все 'смотрит / встаёт / делает паузу' если они не двигают сцену; "
                        f"(d) общий лимит: ~{lv['target_words']} спикерских слов, ~{lv['target_lines']} диалоговых строк суммарно; "
                        f"(e) НИКАКИХ time-markers вроде 'в течение двух минут' / 'десять секунд молча' — это раздувает хронометраж; "
                        + (f"(f) удали экспозицию и повторы — только живые удары + конклюзивная развязка ФИНАЛА (без клиффхэнгера). "
                           if _is_finale else
                           f"(f) удали экспозицию и повторы — только живые удары + cliffhanger. ")
                        + f"Это короткая драма для TikTok ({lv['target_sec']}с), не полнометражный сценарий."
                    )
                length_critical.append({
                    'type': 'script_overlength',
                    'severity': 'critical',
                    'where': 'весь сценарий серии',
                    'explanation': (
                        f"сценарий нарушает бюджет длины: {reasons_str}. "
                        f"Метрики: {lv['dialogue_lines']} реплик · {lv['dialogue_words']} слов · "
                        f"{lv['action_lines']} action-строк · самая длинная реплика {lv['longest_line_words']} слов · "
                        f"средняя {lv['avg_line_words']} слов · оценка ~{lv['est_sec']}с экрана. "
                        f"Лимит сериала: {lv['target_sec']}с, ~{lv['target_words']} слов, ~{lv['target_lines']} строк."
                    ),
                    'fix': fix_msg,
                })
                print(f'[script-length] ep {num} attempt {attempt+1}: {lv["est_sec"]}s ({lv["ratio"]}×), '
                      f'lines={lv["dialogue_lines"]} words={lv["dialogue_words"]} '
                      f'longest={lv["longest_line_words"]} avg={lv["avg_line_words"]} actions={lv["action_lines"]} '
                      f'reasons=[{reasons_str}]', flush=True)
            all_violations = list(report.get('violations', [])) + list(logic_report.get('violations', [])) + crowd_critical + length_critical + naming_critical
            critical = [v for v in all_violations if v.get('severity') == 'critical']
            audit_report = {
                'passes': not critical,
                'violations': all_violations,
                'retries': attempt,
                'audit_error': report.get('audit_error') or logic_report.get('audit_error'),
                'logic_passes': logic_report.get('passes', True),
                'continuity_passes': report.get('passes', True),
                'crowd_violations': crowd_violations,
            }
            if not critical:
                break
            # Build a fix-it prompt and retry — group violations by source for clarity
            cont_fixes = [v for v in critical if v.get('type') in ('timeline','fact','knowledge','biology','setup','paperwork','scene_teleport')]
            logic_fixes = [v for v in critical if v.get('type') in ('status','hidden_position','enabling_condition','legal_term','unmotivated_delay','ambiguous_cliffhanger','protagonist_stagnation','emotional_monotony','scene_overcrowding','finale_drift','script_overlength','unnamed_character','uncast_character')]
            fixes_parts = []
            if cont_fixes:
                fixes_parts.append('CONTINUITY/CANON ISSUES:\n' + '\n'.join(
                    f"- [{v.get('type','?')}] {v.get('explanation','')} → FIX: {v.get('fix','')}" for v in cont_fixes))
            if logic_fixes:
                fixes_parts.append('STORY-LOGIC HOLES:\n' + '\n'.join(
                    f"- [{v.get('type','?')}] WHERE: {v.get('where','?')} | {v.get('explanation','')} → FIX: {v.get('fix','')}" for v in logic_fixes))
            prompt = (
                base_prompt
                + '\n\n═══ PREVIOUS DRAFT FAILED AUDIT ═══\n'
                + 'You MUST address every issue below. Do not repeat the same mistakes.\n'
                + '\n\n'.join(fixes_parts)
                + '\n═══════════════════════════════════════════════════\n'
                + 'Rewrite the entire script from scratch fixing ALL issues above while still respecting the LOGIC CONSTRAINTS.'
            )

        # ─── Save script + audit/brief metadata first
        # If there was a previous script, archive it before overwriting
        prev_script_text = (ep.get('script') or '').strip()
        if prev_script_text:
            ep.setdefault('script_history', []).append({
                'ts': time.time(),
                'reason': 'regenerate',
                'script': prev_script_text[:80000],
            })
            ep['script_history'] = ep['script_history'][-10:]
        ep['script'] = _normalize_blocking_tags(script)
        script = ep['script']   # use normalized version downstream
        ep['status'] = 'draft'
        ep['logic_brief'] = brief
        ep['audit_report'] = audit_report
        # Store end_position for continuity context (used by _prev_episode_ending_context)
        end_pos = _extract_end_position(script)
        if end_pos:
            ep['end_position'] = end_pos
        save_episode(sid, num, ep)
        # Extract and register plot devices + narrative state for anti-repetition tracking
        try:
            gen_devices = _extract_devices_from_script(script)
            if gen_devices:
                ep['plot_devices'] = gen_devices
                save_episode(sid, num, ep)
                _update_devices_index(sid, num, gen_devices)
        except Exception as _de:
            _log_event('WARN', 'device_extract_after_gen_failed', err=str(_de)[:200])
        try:
            gen_narrative = _extract_narrative_state_from_script(script)
            if gen_narrative:
                ep['narrative_state'] = gen_narrative
                save_episode(sid, num, ep)
                _update_narrative_index(sid, num, gen_narrative)
        except Exception as _ne:
            _log_event('WARN', 'narrative_extract_after_gen_failed', err=str(_ne)[:200])
        # Sync outfits from SCENE_OPEN blocks (non-blocking — failures are logged)
        try:
            _sync_script_outfits(sid, script)
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_gen_failed', err=str(_oe)[:200])

        # NOTE: cast-block sync is INTENTIONALLY NOT run after script-gen.
        # User wants explicit control — they'll click "🤖 Извлечь персонажей и локации"
        # to trigger /extract-characters when ready. Mark the episode so the auto-heal
        # paths (get_series, autogen pre-sweep) skip it until extraction happens.
        ep['cast_extracted'] = False
        save_episode(sid, num, ep)
        sync_result = {'created_characters': [], 'created_outfits': [], 'created_locations': [],
                       'detected_locations': [], 'healed_refs': 0,
                       'characters_used': ep.get('characters_used', []),
                       'character_outfits': ep.get('character_outfits', {}),
                       'locations_used': ep.get('locations_used', [])}
        print(f'[script-gen {sid}/ep{num}] script saved, cast extraction pending — user must click extract', flush=True)

        # Fallback: scan script text for location names (flexible match)
        s = load_series(sid)  # reload to get the synced state
        ep = load_episode(sid, num)
        locs = s.get('locations', [])
        script_upper = script.upper()
        def loc_in_script(name):
            u = name.upper()
            if u in script_upper: return True
            no_article = re.sub(r'^(THE|AN?)\s+', '', u)
            if no_article != u and no_article in script_upper: return True
            words = [w for w in u.split() if len(w) > 2]
            return bool(words) and all(w in script_upper for w in words)
        detected_locs = [l['id'] for l in locs if loc_in_script(l['name'])]
        ep['locations_used'] = list(set(ep.get('locations_used', [])) | set(detected_locs))
        save_episode(sid, num, ep)

        # ─── POST-WRITE: extract canon updates from finalized script
        try:
            extract_result = extract_canon_updates(sid, num, script)
        except Exception as e:
            extract_result = {'error': str(e)}

        # Append to canon audit log
        try:
            canon = load_canon(sid)
            canon.setdefault('audit_log', []).append({
                'ep': num,
                'ts': time.time(),
                'passes': audit_report['passes'],
                'retries': audit_report['retries'],
                'violations': [
                    {'type': v.get('type'), 'severity': v.get('severity'), 'explanation': v.get('explanation')}
                    for v in audit_report['violations']
                ],
            })
            canon['audit_log'] = canon['audit_log'][-100:]  # cap
            save_canon(sid, canon)
        except Exception:
            pass

        # Auto-generate any newly-introduced character/outfit/location images in background
        trigger_autogen_if_enabled(sid)
        return jsonify({
            'script': script,
            'characters_used':  ep['characters_used'],
            'character_outfits': ep['character_outfits'],
            'locations_used':   ep['locations_used'],
            'logic_brief': brief,
            'audit_report': audit_report,
            'canon_update': extract_result,
            'series': s,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def sync_episode_with_cast_block(sid, num):
    """Reconcile episode's character_used / character_outfits IDs with current series state.
    Idempotent — safe to call repeatedly. Re-parses the cast block from the script and:
      - re-creates any missing characters / outfits in series.json (with new UUIDs)
      - rewrites episode.characters_used and character_outfits to use current valid IDs
      - drops any stale IDs that no longer resolve
    Returns dict {created_chars: [...], created_outfits: [...], healed_refs: int}."""
    s = load_series(sid)
    if not s: return {'error': 'series not found'}
    ep = load_episode(sid, num)
    if not ep or not (ep.get('script') or '').strip(): return {'error': 'episode has no script'}

    chars = s.setdefault('characters', [])
    pre_char_ids = {c['id'] for c in chars}
    pre_outfit_ids = {(c['id'], o['id']) for c in chars for o in c.get('outfits', [])}

    cast_chars, cast_outfits = _parse_cast_block(ep['script'], chars)

    # Track what was just created
    created_chars = [c['name'] for c in chars if c['id'] not in pre_char_ids]
    created_outfits = [
        f"{c['name']}/{o['label']}"
        for c in chars for o in c.get('outfits', [])
        if (c['id'], o['id']) not in pre_outfit_ids
    ]

    # Heal episode refs: drop IDs that no longer exist; merge in cast_block IDs
    valid_char_ids = {c['id'] for c in chars}
    valid_outfit_ids = {(c['id'], o['id']) for c in chars for o in c.get('outfits', [])}

    # characters_used — start from cast_chars (authoritative), keep any extra valid IDs
    healed = 0
    if cast_chars:
        new_used = list(dict.fromkeys(cast_chars))  # dedup, preserve order
        # Keep any extra valid pre-existing IDs (e.g. silent characters not in cast block)
        for cid in ep.get('characters_used', []):
            if cid in valid_char_ids and cid not in new_used:
                new_used.append(cid)
        if ep.get('characters_used') != new_used:
            healed += 1
            ep['characters_used'] = new_used
    else:
        # No cast block parsed — just drop dead IDs
        cleaned = [cid for cid in ep.get('characters_used', []) if cid in valid_char_ids]
        if cleaned != ep.get('characters_used'):
            healed += 1
            ep['characters_used'] = cleaned

    # character_outfits — rewrite from cast_outfits (now LISTS), drop dead refs.
    # cast_outfits is the authoritative source for which outfits the script demands.
    # We also union in any pre-existing valid outfit refs the user manually added.
    new_outfits = {}
    for cid, oids in (cast_outfits or {}).items():
        oids_list = oids if isinstance(oids, list) else [oids]
        kept = [oid for oid in oids_list if (cid, oid) in valid_outfit_ids]
        if kept:
            new_outfits[cid] = list(dict.fromkeys(kept))  # dedup preserve order
    # Preserve any pre-existing valid outfit refs (legacy single-string OR list)
    for cid, raw in (ep.get('character_outfits') or {}).items():
        prev_ids = _outfit_ids(raw)
        existing = new_outfits.setdefault(cid, [])
        for oid in prev_ids:
            if (cid, oid) in valid_outfit_ids and oid not in existing:
                existing.append(oid)
        if not existing:
            new_outfits.pop(cid, None)
    if ep.get('character_outfits') != new_outfits:
        healed += 1
        ep['character_outfits'] = new_outfits

    # ─── Locations — parse scene headings (INT./EXT./ИНТА./ЭКС.) and tick matching series locations.
    # Scene heading format we emit: "ИНТА. HOTEL ROOM — УТРО" / "INT. BOARDROOM — DAY".
    # We grab the middle slug (location name) and fuzzy-match against series.locations by name.
    locs = s.setdefault('locations', [])
    detected_loc_names = []
    # Allow leading markdown decorators (**, __, #, >) before the INT./EXT. cue —
    # writers sometimes emit `**INT. RANCH HOUSE — MORNING**` for visual emphasis,
    # and the old regex silently dropped those whole headings, leaving the
    # location un-tied to the episode.
    heading_re = re.compile(
        r'^[\s*_#>]*(?:INT\.|EXT\.|ИНТА?\.|ЭКС?\.|INT/EXT\.|EXT/INT\.)\s*([^—\-\n]+?)\s*[—\-]',
        re.IGNORECASE | re.MULTILINE,
    )
    for m in heading_re.finditer(ep.get('script') or ''):
        # Strip trailing markdown closers (**, __) that may be on the last token
        nm = m.group(1).strip().strip('"').strip("'").rstrip('*_').strip()
        # Strip leading time-of-day artefacts that sometimes leak into the name
        nm = re.sub(r'^(DAY|NIGHT|MORNING|EVENING|УТРО|НОЧЬ|ДЕНЬ|ВЕЧЕР)\s+', '', nm, flags=re.IGNORECASE).strip()
        if nm and nm.lower() not in (x.lower() for x in detected_loc_names):
            detected_loc_names.append(nm)

    detected_loc_ids = []
    created_loc_names = []
    for nm in detected_loc_names:
        nl = nm.lower()
        match = next((l for l in locs if l['name'].lower() == nl), None)
        if not match:
            # fuzzy: substring either way (e.g. "HOTEL ROOM" vs "Hotel Suite Room")
            match = next(
                (l for l in locs if nl in l['name'].lower() or l['name'].lower() in nl),
                None,
            )
        if not match:
            # AUTO-CREATE: if the script uses a scene heading we don't have on file,
            # add it to series.locations on the fly so the checkbox CAN be ticked.
            # This is the fix for "локации без галочек" — previously we silently dropped
            # any heading that didn't match an existing entry. The series-locations list
            # was often populated from the synopsis, while the script invented its own
            # places, so nothing ever matched. Now the script is the source of truth.
            new_loc = {
                'id': str(uuid.uuid4())[:8],
                'name': nm.title() if nm.isupper() or nm.islower() else nm,
                'description': 'Auto-created from episode script scene heading',
                'ref_images': [],
            }
            locs.append(new_loc)
            created_loc_names.append(new_loc['name'])
            match = new_loc
        if match:
            if match['id'] not in detected_loc_ids:
                detected_loc_ids.append(match['id'])

    valid_loc_ids = {l['id'] for l in locs}
    new_locs_used = list(detected_loc_ids)
    # Keep any pre-existing user-ticked locations that are still valid (don't drop manual picks)
    for lid in ep.get('locations_used', []) or []:
        if lid in valid_loc_ids and lid not in new_locs_used:
            new_locs_used.append(lid)
    if ep.get('locations_used') != new_locs_used:
        healed += 1
        ep['locations_used'] = new_locs_used

    # ── No-name cue → card auto-alias (heals already-written episodes) ──────────
    # If the script addresses a character by a bare role label (e.g. «CLIENT:»)
    # that differs from its card name (e.g. «Mrs. Park»), the binding filter
    # would drop the reference in any chunk that uses only the role label →
    # the model renders the wrong face. We conservatively link the orphan label
    # to its card as an alias so `_char_name_in_text` keeps the ref. Only fires
    # when the mapping is UNAMBIGUOUS: exactly one un-cued role label and exactly
    # one episode card that nothing else claims. Anything ambiguous is left for
    # the write-time detector / user to fix.
    linked_aliases = []
    unnamed_warnings = []
    try:
        ep_cards = [c for c in chars if c['id'] in set(ep.get('characters_used', []))]
        if ep_cards:
            def _resolve_label(lab):
                ll = lab.lower()
                for c in ep_cards:
                    if (c.get('name') or '').lower() == ll:
                        return c
                    if ll in {(a or '').lower() for a in (c.get('aliases') or [])}:
                        return c
                return _resolve_char_by_script_name(lab, ep_cards)
            claimed, generic_orphans, named_orphans = set(), {}, {}
            for lab, _kind in _extract_speaker_and_blocking_labels(ep['script']):
                c = _resolve_label(lab)
                if c:
                    claimed.add(c['id'])
                elif _label_is_unnamed(lab):
                    generic_orphans.setdefault(lab.lower(), lab)
                else:
                    named_orphans.setdefault(lab.lower(), lab)
            unclaimed = [c for c in ep_cards if c['id'] not in claimed]
            # Auto-link ONLY when unambiguous: a single bare role label and a
            # single un-cued card. A named-but-uncast orphan is NOT auto-aliased
            # (it needs its own card) — it's surfaced as a warning instead.
            if len(generic_orphans) == 1 and not named_orphans and len(unclaimed) == 1:
                lab_low, lab_orig = next(iter(generic_orphans.items()))
                card = unclaimed[0]
                al = card.setdefault('aliases', [])
                if lab_low not in {(a or '').lower() for a in al}:
                    al.append(lab_low)
                    linked_aliases.append({'alias': lab_orig, 'char': card.get('name'), 'char_id': card['id']})
                    _log_event('INFO', 'cue_auto_aliased', sid=sid, ep_num=num,
                               alias=lab_orig, char=card.get('name'), char_id=card['id'])
            else:
                for lab in list(generic_orphans.values()) + list(named_orphans.values()):
                    unnamed_warnings.append(lab)
    except Exception as _ae:
        _log_event('WARN', 'cue_auto_alias_failed', sid=sid, ep_num=num, err=str(_ae)[:200])

    save_series(sid, s)
    save_episode(sid, num, ep)
    return {
        'created_characters': created_chars,
        'created_outfits': created_outfits,
        'created_locations': created_loc_names,
        'detected_locations': detected_loc_names,
        'healed_refs': healed,
        'characters_used': ep['characters_used'],
        'character_outfits': ep['character_outfits'],
        'locations_used': ep['locations_used'],
        'linked_aliases': linked_aliases,
        'unnamed_warnings': unnamed_warnings,
    }


def _infer_gender_from_script(name, script):
    """Count gendered pronouns within ±3 lines of any line mentioning `name`.
    Returns 'female' / 'male' / None. Handles English + Russian.
    Used as a robust fallback when the cast block omits GENDER for a character —
    the previous tiny hardcoded female-name whitelist defaulted everyone-not-listed
    to 'male', which silently turned protagonists like LYDIA into men."""
    if not name or not script:
        return None
    import re as _re
    name_lower = name.lower()
    lines = script.splitlines()
    he_count = 0
    she_count = 0
    EN_HE  = _re.compile(r"\b(he|his|him|himself|he's|he'd|he'll)\b", _re.IGNORECASE)
    EN_SHE = _re.compile(r"\b(she|her|hers|herself|she's|she'd|she'll)\b", _re.IGNORECASE)
    RU_HE  = _re.compile(r"\b(он|его|ему|им|нём|него)\b", _re.IGNORECASE)
    RU_SHE = _re.compile(r"\b(она|её|ее|ей|ней|неё)\b", _re.IGNORECASE)
    for i, line in enumerate(lines):
        if name_lower not in line.lower():
            continue
        window = ' '.join(lines[max(0, i-3): min(len(lines), i+4)])
        he_count  += len(EN_HE.findall(window))  + len(RU_HE.findall(window))
        she_count += len(EN_SHE.findall(window)) + len(RU_SHE.findall(window))
    # Require a meaningful margin to avoid noise from generic "he was talking to her"
    if she_count >= 3 and she_count > he_count * 1.3:
        return 'female'
    if he_count >= 3 and he_count > she_count * 1.3:
        return 'male'
    return None


def _parse_cast_block(script, chars):
    """Parse === EPISODE CAST === block, auto-create new characters & outfits.
    Mutates `chars` (the series characters list) — caller must save_series afterwards.
    Returns (char_id_list, {char_id: outfit_id})."""
    import re as _re
    match = _re.search(r'=== EPISODE CAST ===(.*?)=== END CAST ===', script, _re.DOTALL)
    if not match:
        return [], {}

    def _norm(s):
        return (s or '').strip().strip('"').strip("'")

    def _find_char(name, chars_list):
        nl = name.lower()
        for c in chars_list:
            if c['name'].lower() == nl:
                return c
        # fuzzy: first word match
        first = nl.split()[0] if nl.split() else nl
        for c in chars_list:
            if first and first in c['name'].lower():
                return c
        return None

    char_ids, outfit_map = [], {}  # outfit_map: {char_id: [outfit_id, ...]} (preserves order, dedups)

    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line or not line.upper().startswith('CHARACTER:'):
            continue

        # Parse pipe-separated KEY: VALUE pairs
        fields = {}
        parts = line.split('|')
        for p in parts:
            if ':' not in p:
                continue
            k, v = p.split(':', 1)
            fields[k.strip().upper()] = _norm(v)

        raw_name = fields.get('CHARACTER', '')
        if not raw_name:
            continue

        # Strip trailing parentheticals like "Claire (mother)"
        raw_name = _re.sub(r'\s*\(.*?\)\s*$', '', raw_name).strip()

        char = _find_char(raw_name, chars)
        char_was_just_created = False

        # Auto-create unknown character
        if not char:
            char_was_just_created = True
            gender = fields.get('GENDER', '').lower()
            if gender not in ('male', 'female'):
                # Robust fallback: scan the script for gendered pronouns near this
                # character's name. Beats the old tiny female-name whitelist which
                # silently defaulted everyone (Lydia, Sarah, Emma, etc.) to male.
                inferred = _infer_gender_from_script(raw_name, script)
                if inferred:
                    gender = inferred
                else:
                    gender = 'female' if any(t in raw_name.lower() for t in [
                        'claire','elena','victoria','anna','maria','lina','lydia','sarah',
                        'emma','sophia','olivia','ava','isabella','mia','charlotte',
                        'amelia','harper','evelyn','abigail','grace','chloe','luna',
                        'ella','aurora','clara','rose','ruby','hazel','lily',
                    ]) else 'male'
            look = fields.get('LOOK', '') or fields.get('APPEARANCE', '')
            od = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
            # Fallback: if writer omitted LOOK, synthesize a baseline appearance.
            if not look:
                gword = 'woman' if gender == 'female' else 'man'
                look = f"young {gword}, photogenic, neutral attractive features, mid-20s to mid-30s"
            # First appearance defines the BASE outfit. Whatever this character is
            # wearing on their introduction — that's their default look (a bear-builder
            # IS in construction gear by default; he doesn't have separate "regular"
            # clothes hidden somewhere). Embed OUTFIT_DESC into appearance so the base
            # portrait gen has the clothing description; the outfit entry below will
            # also be flagged is_base so we never duplicate-generate it as a costume change.
            if od and 'wearing' not in look.lower():
                look = look.rstrip(' ,;') + f", wearing {od[:200]}"
            # Remember the OUTFIT label this character was introduced wearing.
            # Subsequent scenes that mention the SAME label are NOT a costume
            # change — they're just the character in their default look. Stored
            # on the character so the outfit-attachment logic below can skip
            # creating a redundant separate outfit entry. A real costume change
            # = a NEW label appearing in a later scene; only THEN do we create
            # an outfit (and never auto-flag it is_base — the base IS implicit).
            base_label_intro = (fields.get('OUTFIT', 'base').split('(')[0].strip().lower() or 'base')
            char = {
                'id': str(uuid.uuid4())[:8],
                'name': raw_name,
                'description': fields.get('ROLE', '') or f'Появляется в сценарии',
                'appearance':  look,
                'gender':      gender,
                'voice_id':    '',
                'ref_images':  [],
                'outfits':     [],
                'base_outfit_label': base_label_intro,  # implicit-base marker
            }
            chars.append(char)

        char_ids.append(char['id'])

        raw_outfit = fields.get('OUTFIT', 'base')
        raw_outfit = raw_outfit.split('(')[0].strip()
        if raw_outfit.lower() == 'base' or not raw_outfit:
            continue

        # ─── DEFENSIVE CHECK: writer encoded a SEPARATE PERSON in OUTFIT field ───
        # Common Claude failure mode: writes "CHARACTER: ARIA | OUTFIT: leo_child |
        # OUTFIT_DESC: small boy in navy hoodie..." — meaning Leo is a separate person,
        # not Aria's outfit. Detect this via two signals:
        #   1) outfit_label is the first-name token of an existing or canonical character
        #   2) OUTFIT_DESC contains a different-gender / different-age-class person word
        # If detected, treat the line as a NEW CHARACTER, not an outfit of the parent.
        outfit_desc_for_check = (fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')).lower()
        person_words_male   = (' boy ', ' man ', ' male ', ' father ', ' brother ', ' uncle ', ' grandfather ')
        person_words_female = (' girl ', ' woman ', ' female ', ' mother ', ' sister ', ' aunt ', ' grandmother ')
        person_words_child  = (' child ', ' kid ', ' toddler ', ' infant ', ' baby ')
        od_padded = ' ' + outfit_desc_for_check + ' '
        char_gender = (char.get('gender') or '').lower()
        # Gender mismatch — outfit_desc describes someone of the opposite gender
        gender_mismatch = (
            (char_gender == 'female' and any(w in od_padded for w in person_words_male))
            or (char_gender == 'male' and any(w in od_padded for w in person_words_female))
        )
        # Child mismatch — adult character but outfit_desc describes a child (and char is not flagged child)
        child_mismatch = any(w in od_padded for w in person_words_child) and 'child' not in (char.get('description','') + char.get('appearance','')).lower()
        # Outfit label looks like a person name (matches another character's first-name token)
        label_first = raw_outfit.split('_')[0].lower()
        label_is_person_name = False
        if label_first and len(label_first) >= 3:
            for other in chars:
                if other['id'] == char['id']:
                    continue
                if other['name'].lower().split()[0] == label_first:
                    label_is_person_name = True
                    break

        if (gender_mismatch or child_mismatch) and (label_is_person_name or '_' in raw_outfit):
            # Promote this line into its own CHARACTER. Use the outfit_label's first token
            # (capitalized) as the candidate name. Skip the outfit-attachment for the
            # parent character — the line was misencoded.
            new_name_guess = label_first.capitalize() if label_first else None
            if new_name_guess:
                # Try to find / create the standalone character
                spawned = _find_char(new_name_guess, chars)
                if not spawned:
                    if any(w in od_padded for w in person_words_child):
                        guessed_gender = 'male' if any(w in od_padded for w in person_words_male) else (
                            'female' if any(w in od_padded for w in person_words_female) else 'male'
                        )
                    else:
                        guessed_gender = 'male' if any(w in od_padded for w in person_words_male) else (
                            'female' if any(w in od_padded for w in person_words_female) else char_gender or 'female'
                        )
                    spawned = {
                        'id': str(uuid.uuid4())[:8],
                        'name': new_name_guess,
                        'description': f'Извлечён из OUTFIT_DESC ошибочно вписанного как образ персонажа {char["name"]}',
                        'appearance': fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', ''),
                        'gender': guessed_gender,
                        'voice_id': '',
                        'ref_images': [],
                        'outfits': [],
                    }
                    chars.append(spawned)
                # Roll back: remove parent char_id we appended IF this line was the only
                # reason it was added (parent had no other valid outfit refs in this pass).
                # Simpler: just append spawned and DROP the misencoded outfit. Keep parent
                # listed (they still appear in the script).
                if spawned['id'] not in char_ids:
                    char_ids.append(spawned['id'])
                print(f'[parse_cast] PROMOTED misencoded outfit "{raw_outfit}" of {char["name"]} '
                      f'→ standalone character "{spawned["name"]}" (od_excerpt="{outfit_desc_for_check[:60]}")')
                continue  # skip the outfit-attachment below

        # Skip outfit machinery entirely when this scene's OUTFIT matches the
        # character's IMPLICIT BASE LABEL — i.e. the look they were first
        # introduced wearing. That's not a costume change, it's the default
        # appearance, already baked into char.appearance. Creating a separate
        # outfit entry here would clutter the UI with a fake "outfit" chip on
        # the character card. The scene gets no outfit_map entry → renderers
        # fall back to the base ref photo automatically.
        base_label = (char.get('base_outfit_label') or '').lower()
        if base_label and raw_outfit.lower() == base_label:
            continue

        # Find existing outfit by label
        outfit = next((o for o in char.get('outfits', [])
                       if o.get('label','').lower() == raw_outfit.lower()), None)

        # IS_BASE marker: this outfit IS the character's base look — link to ref_images, no separate gen
        is_base_flag = fields.get('IS_BASE', '').lower() in ('true', 'yes', '1')

        # Auto-create new outfit if scene declares one with description.
        # NOTE: previously we auto-flagged the FIRST outfit of a freshly-created
        # character as is_base ("AUTO-BASE"). That's now obsolete — the implicit
        # base look is captured via base_outfit_label on the character itself
        # (see new-character creation block above), and matching scenes are
        # short-circuited via the `continue` above. Anything reaching this point
        # is a REAL costume change and gets a normal (non-base) outfit entry.
        if not outfit:
            outfit_desc = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
            if outfit_desc or raw_outfit.lower() != 'base':
                outfit = {
                    'id':          str(uuid.uuid4())[:8],
                    'label':       raw_outfit,
                    'description': outfit_desc,
                    'photo':       '',
                    'avai_url':    '',
                }
                char.setdefault('outfits', []).append(outfit)

        # Apply explicit IS_BASE flag from the cast block (writer override).
        if outfit and is_base_flag:
            # Unmark other outfits as base (only one base per char)
            for o in char.get('outfits', []):
                if o['id'] != outfit['id'] and o.get('is_base'):
                    o['is_base'] = False
            if char.get('ref_images') and not outfit.get('photo'):
                outfit['photo'] = char['ref_images'][0]
            if char.get('avai_base_url') and not outfit.get('avai_url'):
                outfit['avai_url'] = char.get('avai_base_url', '')
            outfit['is_base'] = True
            # Keep base_outfit_label in sync so subsequent scenes with this label
            # also short-circuit instead of re-creating.
            char['base_outfit_label'] = outfit.get('label', '').lower()

        if outfit:
            lst = outfit_map.setdefault(char['id'], [])
            if outfit['id'] not in lst:
                lst.append(outfit['id'])

    return char_ids, outfit_map


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
