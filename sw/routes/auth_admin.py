"""Auth + admin routes: client-log, request logging hooks, admin logs/users,
auth gate (before_request), Google OAuth login/callback/logout, /api/me,
healthz, AVAI key validation.

Endpoint function names `login` and `auth_google_callback` MUST stay verbatim —
url_for() builds the Google OAuth redirect from them."""
import datetime
import json
import time
import traceback

import requests
from flask import jsonify, redirect, render_template, request, session, url_for

from sw.config import DATA_ROOT, PRIMARY_USER_EMAIL
from sw.core import app

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


