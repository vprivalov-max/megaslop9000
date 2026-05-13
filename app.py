import json
import os
import re
import uuid
import random
import secrets
import shutil
import datetime
import time
import subprocess
import sys
import threading
import requests
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


_TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
    'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def slugify(title):
    """Convert series/character title to a safe folder name."""
    s = ''.join(_TRANSLIT.get(c.lower(), c) if c.lower() in _TRANSLIT else c for c in title)
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[\s_-]+', '-', s).strip('-')
    return s[:40] or 'series'

def asset_name(*parts):
    """Create UPPER_SNAKE_CASE asset filename stem from name parts.
    e.g. asset_name('Claire', 'Work Blazer') → 'CLAIRE_WORK_BLAZER'
         asset_name("Sophie's Apartment") → 'SOPHIES_APARTMENT'
    """
    combined = '_'.join(str(p) for p in parts)
    combined = re.sub(r"['\"]", '', combined)       # strip apostrophes/quotes
    combined = re.sub(r'[^\w]+', '_', combined)     # non-word chars → underscore
    return combined.upper().strip('_')


# ── Scene heading detection (mirrors static/app.js _matchSceneHeading) ────────
# Used by Seedance compose to skip scene headings as text anchors when locating
# chunks in the script. Recognises BOTH formal (INT./EXT./ИНТ./...) AND inferred
# headings (Локация:, СЦЕНА N, standalone ALL-CAPS slugs, [bracketed slugs])
# so continuity logic survives in scripts that don't use INT./EXT.
# `[\s*_#>]*` allows markdown decorators (**, __, #, >) before the cue.
# Without it `**INT. RANCH HOUSE — MORNING**` silently fails detection.
_SCENE_HEADING_FORMAL_RE = re.compile(
    r'^[\s*_#>]*(INT\.|EXT\.|INT\.?\s*/\s*EXT\.?|I/E\.|ИНТ\.|ИНТА\.|ЭКСТ\.|ЭКС\.|НАТ\.|НАТУРА\.|ВНУТР\.|ИНТЕРЬЕР|ВНЕ\.|СНАРУЖИ)\s+',
    re.IGNORECASE,
)
_SCENE_HEADING_INFER_RE = re.compile(
    r'^[\s*_#>]*(Локация\s*[:：]|Location\s*[:：]|СЦЕНА\s*\d|Сцена\s*\d|SCENE\s*\d)',
    re.IGNORECASE,
)
_SLUG_BLOCKLIST_RE = re.compile(
    r'^(REVERSAL|END|FIN|КОНЕЦ|TBD|TBC|БИТ|BIT|HOOK|TWIST|CLIFFHANGER|КЛИФФХЭНГЕР|РАЗВОРОТ|ПАУЗА|ТИШИНА|FLASHBACK|FLASH BACK|MONTAGE|МОНТАЖ|VOICE OVER|V\.O\.|O\.S\.)$',
    re.IGNORECASE,
)
_TRANSITION_PREFIX_RE = re.compile(r'^(FADE|CUT|DISSOLVE|SMASH|MATCH)\b', re.IGNORECASE)
_LOWERCASE_LETTER_RE = re.compile(r'[a-zа-яё]')
_UPPERCASE_LETTER_RE = re.compile(r'[A-ZА-ЯЁ]')

def _is_all_caps_slug(t: str) -> bool:
    """ALL-CAPS standalone slug like 'ДОМ АННЫ — НОЧЬ' or 'OFFICE — DAY'."""
    if not t or len(t) < 5 or len(t) > 80: return False
    if any(ch in t for ch in ':：[]'):     return False
    if _LOWERCASE_LETTER_RE.search(t):      return False
    if not _UPPERCASE_LETTER_RE.search(t):  return False
    if _TRANSITION_PREFIX_RE.match(t):      return False
    if _SLUG_BLOCKLIST_RE.match(re.sub(r'[\.\—\-\s]+$', '', t)): return False
    return True

def _is_bracket_slug(t: str) -> bool:
    """Bracketed slug like '[КАФЕ — НОЧЬ]'. Inner must be uppercase only."""
    m = re.match(r'^\[\s*([^\]]{3,80})\s*\]\s*$', t or '')
    if not m: return False
    inner = m.group(1).strip()
    if _LOWERCASE_LETTER_RE.search(inner): return False
    if _SLUG_BLOCKLIST_RE.match(inner):    return False
    if _TRANSITION_PREFIX_RE.match(inner): return False
    return True

def is_scene_heading(line: str) -> bool:
    """True if the line opens a new scene — formal (INT./EXT./ИНТ./...) OR inferred
    (Локация:, СЦЕНА N, standalone ALL-CAPS slug, [bracketed slug])."""
    if not line: return False
    t = line.strip()
    if not t: return False
    if _SCENE_HEADING_FORMAL_RE.match(t): return True
    if _SCENE_HEADING_INFER_RE.match(t):  return True
    if _is_all_caps_slug(t):              return True
    if _is_bracket_slug(t):               return True
    return False

app = Flask(__name__, static_folder='static', template_folder='templates')

# Cache-bust token for /static/* — bumped automatically on every deploy/
# restart via the mtime of the most-recently-modified static file. Without
# this the browser holds onto stale app.js / style.css after a deploy and
# users keep seeing the previous bug forever («не помогло чет, так же всё»).
try:
    _static_dir = Path(__file__).parent / 'static'
    _static_mtime = max(
        (_p.stat().st_mtime for _p in _static_dir.glob('*') if _p.is_file()),
        default=0.0,
    )
    STATIC_VERSION = str(int(_static_mtime))
except Exception:
    STATIC_VERSION = str(int(time.time()))
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

# Behind Coolify/Traefik/Caddy reverse proxy — trust X-Forwarded-* headers so
# url_for(_external=True) builds correct https://<public-host>/auth/google/callback
# URLs. Without this Flask sees the inner http://app:8080 → Google rejects the
# OAuth start with `redirect_uri_mismatch`. Got accidentally deleted in a
# later refactor — restoring.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

BASE = Path(__file__).parent
# DATA_ROOT holds per-user subfolders: <DATA_ROOT>/<email>/projects/<sid>/...
# Override via env (DATA_ROOT=/var/lib/series-writer on the server).
DATA_ROOT = Path(os.environ.get('DATA_ROOT') or (BASE / 'data')).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
# Legacy path — used to one-time-migrate existing single-user data
LEGACY_PROJECTS = BASE / 'projects'
CONFIG_FILE = BASE / 'config.json'
RETELLER_API   = 'https://reteller.ai/api/v1'
AVAI_API       = 'https://avai-gen.com/api/public/generate'

def _read_config_field(field):
    """Read a key from config.json (legacy single-user dev fallback)."""
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text())
            return (cfg.get(field) or '').strip()
    except Exception:
        pass
    return ''

def _load_secret(env_name, config_field=None):
    """Resolve a secret in this order: env var → config.json field → empty.
    Env wins so production deploys never accidentally fall back to a checked-in
    legacy config (config.json is gitignored, but exists locally)."""
    val = (os.environ.get(env_name) or '').strip()
    if val:
        return val
    if config_field:
        return _read_config_field(config_field)
    return ''

# Email of the user whose AVAI/Reteller keys default to the global env (the
# operator who set up the system). Other users must enter their own keys.
PRIMARY_USER_EMAIL = (os.environ.get('PRIMARY_USER_EMAIL') or 'v.privalov@gamegears.online').lower()

def _user_keys_path(email):
    """Per-user keys file: <DATA_ROOT>/<email-slug>/keys.json"""
    safe = re.sub(r'[^a-z0-9]+', '_', (email or '').lower()).strip('_') or 'anon'
    return DATA_ROOT / safe / 'keys.json'

def _load_user_keys(email):
    """Returns dict {avai_key, reteller_key} for this user (empty strings if not set)."""
    p = _user_keys_path(email)
    if not p.exists():
        return {'avai_key': '', 'reteller_key': ''}
    try:
        d = json.loads(p.read_text())
        return {
            'avai_key': (d.get('avai_key') or '').strip(),
            'reteller_key': (d.get('reteller_key') or '').strip(),
        }
    except Exception:
        return {'avai_key': '', 'reteller_key': ''}

def _save_user_keys(email, keys):
    """Persist per-user keys. Caller passes a dict — only known fields are kept."""
    p = _user_keys_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        'avai_key': (keys.get('avai_key') or '').strip(),
        'reteller_key': (keys.get('reteller_key') or '').strip(),
    }
    p.write_text(json.dumps(safe, indent=2))

# Thread-local override for background workers spawned outside Flask request
# context. When a request handler spawns a daemon thread, Flask's `session` is
# torn down by the time the thread runs, so `current_user_email()` raises and
# `_get_user_*_key()` would silently return '' → AVAI/Reteller hit with empty
# x-api-key → 401 or hang. The fix: capture the keys in the request handler
# (where session IS alive) via `_capture_user_keys()` and run the thread body
# through `_spawn_with_keys()`, which sets these thread-locals before invoking
# the target. The getters below check the override first.
_thread_keys = threading.local()

def _get_user_avai_key():
    """Returns AVAI key for the current request's user. Falls back to global env
    only for the PRIMARY_USER_EMAIL (operator who set up the system) — every other
    user must enter their own key on first login. In dev mode (AUTH_ENABLED=False)
    the local user gets the global env too — local dev shouldn't be gated.

    Thread-local override: if a background worker was spawned via
    `_spawn_with_keys()`, returns the captured key directly (any string,
    including empty). This avoids touching `flask.session` outside request
    context."""
    override = getattr(_thread_keys, 'avai_key', None)
    if override is not None:
        return override
    try:
        email = current_user_email() or ''
    except Exception:
        email = ''
    if email:
        user_key = _load_user_keys(email).get('avai_key')
        if user_key:
            return user_key
        if email == PRIMARY_USER_EMAIL or not AUTH_ENABLED:
            return AVAI_KEY  # grandfathered global default
    return ''

def _get_user_reteller_key():
    """Returns Reteller key for the current request's user. Same fallback logic
    as AVAI: PRIMARY_USER_EMAIL or dev mode falls back to global env.
    Honors `_thread_keys.reteller_key` for background workers."""
    override = getattr(_thread_keys, 'reteller_key', None)
    if override is not None:
        return override
    try:
        email = current_user_email() or ''
    except Exception:
        email = ''
    if email:
        user_key = _load_user_keys(email).get('reteller_key')
        if user_key:
            return user_key
        if email == PRIMARY_USER_EMAIL or not AUTH_ENABLED:
            return RETELLER_KEY
    return ''

def _capture_user_keys():
    """Snapshot the current user's identity + keys so a background thread can
    use them after the Flask request context is gone. MUST be called inside a
    request handler (or another already-thread-local-scoped worker).

    `email` is captured separately because file-system helpers (user_root,
    series_path, load/save_episode) call current_user_email(), which without
    this override crashes on `session.get(...)` outside request context.
    Without it, a thread that writes back state (e.g. seedance submit storing
    job_id after AVAI returns 202) silently dies and leaves chunks stranded
    in 'submitting' forever — exactly the prod bug we hit."""
    try:
        email = current_user_email() or ''
    except Exception:
        email = ''
    return {
        'email': email,
        'avai_key': _get_user_avai_key(),
        'reteller_key': _get_user_reteller_key(),
    }

def _spawn_with_keys(target, *args, **kwargs):
    """Spawn a daemon thread that runs `target(*args, **kwargs)` with the current
    user's identity + AVAI/Reteller keys propagated into thread-local storage.
    Returns the Thread object. Use this anywhere you'd previously do
    `threading.Thread(target=..., daemon=True).start()` for work that calls
    AVAI/Reteller helpers OR file-system helpers (load/save_episode etc.).
    Logs and re-raises worker exceptions so daemon threads don't die silently."""
    ctx = _capture_user_keys()
    def _runner():
        _thread_keys.email = ctx['email']
        _thread_keys.avai_key = ctx['avai_key']
        _thread_keys.reteller_key = ctx['reteller_key']
        try:
            target(*args, **kwargs)
        except Exception as e:
            import traceback
            print(f'[_spawn_with_keys] worker {getattr(target, "__name__", target)} '
                  f'crashed: {type(e).__name__}: {e}', flush=True)
            traceback.print_exc()
            raise
        finally:
            _thread_keys.email = None
            _thread_keys.avai_key = None
            _thread_keys.reteller_key = None
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return t

# All secrets are loaded once at startup. Env vars are the canonical source for
# production; config.json is a dev-only convenience fallback.
ANTHROPIC_KEY = _load_secret('ANTHROPIC_API_KEY', 'anthropic_key')
AVAI_KEY      = _load_secret('AVAI_API_KEY',      'avai_key')
RETELLER_KEY  = _load_secret('RETELLER_API_KEY',  'reteller_key')

# Warn loudly at startup if anything is missing — easier than debugging 401s later.
for _name, _val in (('ANTHROPIC_API_KEY', ANTHROPIC_KEY),
                    ('AVAI_API_KEY',      AVAI_KEY),
                    ('RETELLER_API_KEY',  RETELLER_KEY)):
    if not _val:
        print(f'[config] WARNING {_name} is not set — related features will fail')

# ── Render queue ─────────────────────────────────────────────────────────────
# Cap concurrent ffmpeg renders so N users don't all pin the CPU at once.
# Each render takes 5–30s of pure CPU; serializing past 2 prevents stalls and
# OOM. Configurable via env.
_RENDER_CONCURRENCY = max(1, int(os.environ.get('RENDER_CONCURRENCY', '2')))
RENDER_SEMAPHORE = threading.BoundedSemaphore(_RENDER_CONCURRENCY)

def _render_queue_depth():
    """Approximate number of waiters. Bounded semaphores don't expose this
    directly, so we just report whether the queue is saturated."""
    # _value is the number of free slots (CPython internal).
    free = getattr(RENDER_SEMAPHORE, '_value', _RENDER_CONCURRENCY)
    return {'concurrency': _RENDER_CONCURRENCY, 'free': free, 'busy': _RENDER_CONCURRENCY - free}

# ── Startup recovery ─────────────────────────────────────────────────────────
# When the server is killed mid-Seedance-submission, chunks can be stranded:
#   • status='submitting' without job_id  → submit thread died, mark failed
#   • status='pending'/'processing' with job_id → AVAI still working;
#     the next time a client opens the episode and polls, state self-heals.
# We only need to clean up the first case so the UI stops showing a phantom
# "submitting" card. Idempotent and bounded — won't touch healthy state.

_RECOVERY_DONE = False
_RECOVERY_LOCK = threading.Lock()
_SUBMITTING_TIMEOUT_SEC = 5 * 60  # boot-time recovery: be conservative
_SUBMITTING_TIMEOUT_RUNTIME_SEC = 300  # runtime poll: submit usually <10s, but AVAI sometimes lags w/ heavy prompts (esp. prod VPS egress)

def _recover_inflight_chunks():
    """Sweep all per-user episode files once at startup."""
    global _RECOVERY_DONE
    with _RECOVERY_LOCK:
        if _RECOVERY_DONE:
            return
        _RECOVERY_DONE = True
    if not DATA_ROOT.exists():
        return
    now = time.time()
    cleaned = 0
    scanned = 0
    for user_dir in DATA_ROOT.iterdir():
        proj = user_dir / 'projects'
        if not proj.is_dir():
            continue
        for sd in proj.iterdir():
            ep_dir = sd / 'episodes'
            if not ep_dir.is_dir():
                continue
            for ep_file in ep_dir.glob('*.json'):
                try:
                    ep = json.loads(ep_file.read_text())
                except Exception:
                    continue
                chunks = (ep.get('seedance_chunks')
                          if isinstance(ep, dict) else None) or []
                changed = False
                for c in chunks:
                    scanned += 1
                    st = c.get('status')
                    age = now - int(c.get('created_at') or 0)
                    if st == 'submitting' and not c.get('job_id') and age > _SUBMITTING_TIMEOUT_SEC:
                        c['status'] = 'failed'
                        c['error'] = 'server restarted before submission completed'
                        cleaned += 1
                        changed = True
                if changed:
                    try:
                        _atomic_write_json(ep_file, ep)
                    except Exception as e:
                        print(f'[recover] failed to save {ep_file}: {e}')
    if scanned:
        print(f'[recover] scanned {scanned} chunks, cleaned {cleaned} stranded submissions')
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}


# ── Auth (Google OAuth, restricted to a single Workspace domain) ─────────────
#
# Modes:
#   • Production: set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET env vars.
#     OAuth flow runs, only @AUTH_ALLOWED_DOMAIN emails get in.
#   • Dev (default for local): no env vars set → DEV_BYPASS auto-logs in
#     as DEV_USER_EMAIL so existing local workflow keeps working.

AUTH_ALLOWED_DOMAIN = os.environ.get('AUTH_ALLOWED_DOMAIN', 'gamegears.online').lower()
DEV_USER_EMAIL      = os.environ.get('DEV_USER_EMAIL', 'local@dev').lower()
GOOGLE_CLIENT_ID    = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET= os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()
AUTH_ENABLED        = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and _AUTHLIB_AVAILABLE)

# Persist Flask session secret across restarts so users don't get logged out
# every time we redeploy. Stored in a gitignored file.
def _get_or_create_secret():
    env_secret = os.environ.get('FLASK_SECRET_KEY', '').strip()
    if env_secret:
        return env_secret
    fp = BASE / '.flask_secret'
    if fp.exists():
        try:
            return fp.read_text().strip()
        except Exception:
            pass
    s = secrets.token_hex(32)
    try:
        fp.write_text(s)
        try: fp.chmod(0o600)
        except Exception: pass
    except Exception:
        pass
    return s

app.config['SECRET_KEY'] = _get_or_create_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Set SECURE on production via env (Caddy terminates TLS)
    SESSION_COOKIE_SECURE=bool(os.environ.get('SESSION_COOKIE_SECURE')),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=14),
)

oauth = None
if AUTH_ENABLED:
    oauth = OAuth(app)
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

def current_user_email():
    """Returns the logged-in user's email, or None if not authenticated.

    Background workers spawned via _spawn_with_keys() set `_thread_keys.email`
    so file-system helpers (user_root → series_path → episodes_dir → load/save_episode)
    can resolve the right user directory after the Flask request context tears
    down. Without this, a daemon thread that writes back state (e.g. seedance
    submit storing job_id) crashes with `RuntimeError: Working outside of
    request context` and the chunk is stranded in 'submitting' forever."""
    override = getattr(_thread_keys, 'email', None)
    if override is not None:
        return override
    if not AUTH_ENABLED:
        return DEV_USER_EMAIL
    return (session.get('user') or {}).get('email')

def _is_path_allowed(path):
    """Paths that bypass the auth gate."""
    if path.startswith('/static/'):
        return True
    return path in ('/login', '/auth/google', '/auth/google/callback', '/auth/logout', '/healthz')

# ── Per-user structured logging ─────────────────────────────────────────────
# Writes one JSONL line per request to <DATA_ROOT>/<email-slug>/_logs/YYYY-MM-DD.jsonl
# so the primary operator can debug other users' issues without SSHing into the
# VPS to grep docker logs. Captures: timestamp, route, method, status, duration,
# user_email, error trace (when 5xx). Also `_log_event(...)` lets app code emit
# structured events (e.g. "[autogen] FAILED item/...") into the same file.

_LOG_LOCK = threading.Lock()

def _user_log_dir(email):
    safe = re.sub(r'[^a-z0-9]+', '_', (email or 'anon').lower()).strip('_') or 'anon'
    return DATA_ROOT / safe / '_logs'

def _log_event(level, event, email=None, **fields):
    """Append a structured log line to the user's daily JSONL log file.
    Safe to call from any thread or background worker. Best-effort — failures
    are swallowed (we don't want logging to crash a request)."""
    try:
        if email is None:
            try: email = current_user_email() or 'anon'
            except Exception: email = 'anon'
        d = _user_log_dir(email)
        d.mkdir(parents=True, exist_ok=True)
        today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
        line = json.dumps({
            'ts': datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z',
            'level': level,
            'event': event,
            'email': email,
            **fields,
        }, ensure_ascii=False)
        with _LOG_LOCK:
            with open(d / f'{today}.jsonl', 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass

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
_LOG_RETENTION_DAYS = int(os.environ.get('LOG_RETENTION_DAYS', '7'))

def _cleanup_old_logs():
    """Walks every <DATA_ROOT>/<user>/_logs/ folder and deletes JSONL files
    older than _LOG_RETENTION_DAYS. Best-effort — failures swallowed.
    Runs at startup and once per day via _LOG_CLEANUP_TIMER."""
    if _LOG_RETENTION_DAYS <= 0:
        return
    if not DATA_ROOT.exists():
        return
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=_LOG_RETENTION_DAYS)
    deleted = 0
    scanned = 0
    for user_dir in DATA_ROOT.iterdir():
        log_dir = user_dir / '_logs'
        if not log_dir.is_dir():
            continue
        for log_file in log_dir.glob('*.jsonl'):
            # Skip macOS AppleDouble (._*) sidecars — not real log files.
            if log_file.name.startswith('._'):
                continue
            scanned += 1
            try:
                # Filename is YYYY-MM-DD.jsonl — fast path: parse the date.
                stem = log_file.stem
                try:
                    file_date = datetime.datetime.strptime(stem, '%Y-%m-%d')
                except ValueError:
                    # Fallback to mtime if filename isn't ISO-date
                    file_date = datetime.datetime.utcfromtimestamp(log_file.stat().st_mtime)
                if file_date < cutoff:
                    log_file.unlink()
                    deleted += 1
            except Exception:
                pass
    if scanned:
        print(f'[log-cleanup] scanned {scanned} files, deleted {deleted} older than {_LOG_RETENTION_DAYS}d', flush=True)

def _start_log_cleanup_loop():
    """Spawns a daemon thread that runs cleanup once now + every 24h after."""
    if _LOG_RETENTION_DAYS <= 0:
        return
    def _loop():
        while True:
            try:
                _cleanup_old_logs()
            except Exception as e:
                print(f'[log-cleanup] loop tick failed: {e}', flush=True)
            time.sleep(24 * 60 * 60)  # 24h
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


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

_MODEL_ALIAS = {
    '':       'claude-sonnet-4-5',
    'sonnet': 'claude-sonnet-4-5',
    'haiku':  'claude-haiku-4-5',
}

_anthropic_client = None
def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_KEY:
            raise RuntimeError('ANTHROPIC_KEY не задан — пропиши anthropic_key в config.json')
        import anthropic as _anthropic
        _anthropic_client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic_client


def claude_ask(prompt: str, system: str = '', model: str = '', timeout: int = 1200, idle_timeout: int = 180, max_tokens: int = 8192) -> str:
    """Anthropic API call. Signature kept compatible with old CLI version —
    `timeout`/`idle_timeout` are accepted but ignored (SDK handles its own timeouts).
    Retries up to 3 times on 429 (rate-limit) / 529 (overloaded) with
    exponential backoff — critical for parallel range-gen where 3 concurrent
    compose calls used to fail 2/3 because Anthropic throttled the burst."""
    sdk_model = _MODEL_ALIAS.get(model, model) if model else 'claude-sonnet-4-5'
    prompt_kb = len((system + prompt).encode('utf-8')) / 1024
    t0 = time.time()
    print(f'[claude_ask] {prompt_kb:.1f}KB → {sdk_model}', flush=True)
    client = _get_anthropic_client()
    kwargs = {'model': sdk_model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': prompt}]}
    if system:
        kwargs['system'] = system

    last_err = None
    for attempt in range(4):   # 4 attempts total: 0 + 3 retries
        try:
            if max_tokens >= 16000:
                text_parts = []
                stop_reason = None
                with client.messages.stream(**kwargs) as stream:
                    for chunk in stream.text_stream:
                        text_parts.append(chunk)
                    final = stream.get_final_message()
                    stop_reason = getattr(final, 'stop_reason', None)
                text = ''.join(text_parts).strip()
                out_kb = len(text.encode('utf-8')) / 1024
                print(f'[claude_ask] done(stream) in {time.time()-t0:.1f}s ({prompt_kb:.1f}KB→{out_kb:.1f}KB, stop={stop_reason}, try={attempt+1})', flush=True)
                return text
            msg = client.messages.create(**kwargs)
            text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
            out_kb = len(text.encode('utf-8')) / 1024
            print(f'[claude_ask] done in {time.time()-t0:.1f}s ({prompt_kb:.1f}KB→{out_kb:.1f}KB, stop={msg.stop_reason}, try={attempt+1})', flush=True)
            return text
        except Exception as e:
            last_err = e
            # Anthropic SDK exposes status_code on its API errors. 429 (rate
            # limit), 529 (overloaded), 500-503 (transient) all worth retrying.
            sc = getattr(e, 'status_code', None) or (e.response.status_code if hasattr(e, 'response') and hasattr(e.response, 'status_code') else None)
            err_name = e.__class__.__name__
            retryable = sc in (408, 429, 500, 502, 503, 504, 529) or err_name in (
                'RateLimitError', 'APIConnectionError', 'APITimeoutError',
                'InternalServerError', 'OverloadedError', 'APIStatusError',
            )
            if not retryable or attempt == 3:
                print(f'[claude_ask] FAIL after {attempt+1} tries ({time.time()-t0:.1f}s): {err_name}: {str(e)[:200]}', flush=True)
                raise
            # Exponential backoff with jitter: 2s, 6s, 14s
            delay = (2 ** attempt) * 2 + (random.random() * 1.5)
            print(f'[claude_ask] retry {attempt+1}/3 after {delay:.1f}s ({err_name}: {str(e)[:120]})', flush=True)
            time.sleep(delay)
    # Defensive: if loop exits without return/raise (shouldn't happen)
    if last_err:
        raise last_err
    raise RuntimeError('claude_ask: exhausted retries with no error captured')


def anthropic_ask(prompt: str, system: str = '', model: str = 'claude-haiku-4-5') -> str:
    """Direct Anthropic SDK call. Alias of claude_ask."""
    return claude_ask(prompt, system=system, model=model, max_tokens=4096)

def claude_ask_fast(prompt: str, system: str = '') -> str:
    """Haiku — quick tasks (extraction, classification)."""
    return claude_ask(prompt, system=system, model='haiku', max_tokens=4096)

def claude_ask_quality(prompt: str, system: str = '') -> str:
    """Sonnet — creative tasks (scripts, synopses, ideas)."""
    return claude_ask(prompt, system=system, model='sonnet', max_tokens=8192)


def claude_ask_vision(prompt: str, image_urls, system: str = '',
                      model: str = 'haiku', max_tokens: int = 2048) -> str:
    """Multimodal Claude call — accepts list of HTTPS image URLs alongside prompt.
    Used to label continuity frames (who's visible / state / mise-en-scène)."""
    sdk_model = _MODEL_ALIAS.get(model, model) if model else 'claude-haiku-4-5'
    client = _get_anthropic_client()
    content = []
    for url in (image_urls or []):
        if not url or not url.lower().startswith(('http://', 'https://')):
            continue
        content.append({'type': 'image', 'source': {'type': 'url', 'url': url}})
    content.append({'type': 'text', 'text': prompt})
    n_imgs = len(content) - 1
    print(f'[claude_vision] {n_imgs} img(s) → {sdk_model}', flush=True)
    t0 = time.time()
    kwargs = {
        'model': sdk_model,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': content}],
    }
    if system:
        kwargs['system'] = system
    msg = client.messages.create(**kwargs)
    text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
    print(f'[claude_vision] done in {time.time()-t0:.1f}s', flush=True)
    return text


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


def _remap_image_tags(prompt_text: str, old_to_new: dict) -> str:
    """After server-side ref filtering (close-up / de-dup / unresolved drops),
    the composer's prompt still contains @ImageN references for ALL refs it
    originally chose. If we filter refs[] but leave the prompt alone, Seedance
    gets a prompt mentioning @ImageN with no actual reference image at slot N
    → it hallucinates a random face for that 'character'. Real bug seen in
    production: chunk had refs=[Adrian, Lobby] but prompt said @Image2=Vivian,
    @Image3=Sophie, @Image4=Clara → those positions had no images and Seedance
    drew arbitrary people.

    `old_to_new` maps composer's original 1-based @ImageN index → new index
    in the filtered refs[] (None = dropped). This function:
      • Renumbers kept @ImageN to their new positions.
      • Strips entire `@ImageN=Name (clothing)` clauses for dropped N (BINDING).
      • Strips bare `@ImageN` tokens for dropped N (DIALOGUE / ACTION).
      • Tries to clean up dangling commas / empty parens left behind.
    """
    if not prompt_text or not old_to_new:
        return prompt_text or ''
    has_drops = any(v is None for v in old_to_new.values())
    needs_renumber = any(k != v for k, v in old_to_new.items() if v is not None)
    if not has_drops and not needs_renumber:
        return prompt_text

    # We use a placeholder string \x01IMG\x01<n> for renumbered tokens so
    # Pass 2's `@Image\d+` regex doesn't accidentally rewrite them again.
    KEPT_PRE = '\x01IMG\x01'
    DROP_MARK = '\x02'

    # Pass 1: BINDING-style clauses '@ImageN=Name (description)' or '@ImageN=Name'
    # Stops at next comma / period / newline / @Image so adjacent BINDING entries
    # don't bleed together.
    binding_re = re.compile(
        r'@Image(\d+)\s*[=—\-]\s*[^,.@\n]+?(?:\s*\([^)]*\))?(?=\s*(?:,|\.|\n|@Image|$))'
    )
    def _replace_binding(m):
        oldN = int(m.group(1))
        newN = old_to_new.get(oldN)
        if newN is None:
            return DROP_MARK
        # Renumber the @ImageN at the start; placeholder so Pass 2 ignores
        return re.sub(r'^@Image\d+', f'{KEPT_PRE}{newN}', m.group(0), count=1)
    prompt_text = binding_re.sub(_replace_binding, prompt_text)

    # Pass 2: bare @ImageN remaining (DIALOGUE shot-bits, parens). Placeholder
    # tokens from pass 1 are \x01IMG\x01N — don't match @Image\d+ pattern.
    def _replace_bare(m):
        oldN = int(m.group(1))
        newN = old_to_new.get(oldN)
        return f'{KEPT_PRE}{newN}' if newN is not None else DROP_MARK
    prompt_text = re.sub(r'@Image(\d+)', _replace_bare, prompt_text)

    # Convert placeholders back to @Image
    prompt_text = prompt_text.replace(KEPT_PRE, '@Image')

    # Strip drop-marks plus any trailing punctuation/glue token that would
    # leave dangling artefacts.
    prompt_text = re.sub(rf'{DROP_MARK}\s*[,.]?\s*', '', prompt_text)

    # Cleanup punctuation / spacing artefacts left by deletions
    prompt_text = re.sub(r'\(\s*\)', '', prompt_text)             # empty parens
    prompt_text = re.sub(r',\s*,+', ',', prompt_text)             # double commas
    prompt_text = re.sub(r',\s*\.', '.', prompt_text)             # ", ." → "."
    prompt_text = re.sub(r'\(\s*,', '(', prompt_text)             # "(," → "("
    prompt_text = re.sub(r',\s*\)', ')', prompt_text)             # ",)" → ")"
    prompt_text = re.sub(r' {2,}', ' ', prompt_text)              # multiple spaces
    prompt_text = re.sub(r'\s*\n\s*\n\s*\n+', '\n\n', prompt_text)
    return prompt_text.strip()


def _build_image_tag_remap(original_refs: list, final_refs: list) -> dict:
    """Build {original_1based_idx: final_1based_idx_or_None} from composer's
    original ordered refs vs the filtered ordered refs."""
    orig_pos = {}      # (kind, id) → original 1-based pos (first occurrence)
    for i, r in enumerate(original_refs or []):
        key = (r.get('kind'), r.get('id'))
        if key not in orig_pos:
            orig_pos[key] = i + 1
    final_pos = {}
    for i, r in enumerate(final_refs or []):
        key = (r.get('kind'), r.get('id'))
        if key not in final_pos:
            final_pos[key] = i + 1
    out = {}
    for key, oldN in orig_pos.items():
        out[oldN] = final_pos.get(key)
    return out


# ─── Reference-style validators / banlist / blocking-injection ─────────────
# Ported from /tmp/shadow-founder/services/chunk-builder.ts. Goal: each segment's
# final promptEn matches the reference's clean 6-block English structure with
# spatially-consistent `episodeBlocking` injected, banlist replacements applied
# for moderation, and validation warnings caught programmatically.

# 21 replacements that catch Seedance moderation triggers without losing
# narrative meaning. Kept in lock-step with reference (chunk-builder.ts:199-221).
_SD_BANLIST = [
    (re.compile(r'\b(shoots?|fires?|shooting|firing)\b', re.IGNORECASE), 'muzzle flash illuminates'),
    (re.compile(r'\b(guns?|pistols?|rifles?|weapons?)\b', re.IGNORECASE), 'tactical equipment'),
    (re.compile(r'\b(stabs?|slashes?|stabbing|slashing)\b', re.IGNORECASE), 'sharp impact'),
    (re.compile(r'\b(knife|knives|blades?)\b', re.IGNORECASE), 'metallic object'),
    (re.compile(r'\b(attacks?|attacking|attacked)\b', re.IGNORECASE), 'closes distance'),
    (re.compile(r'\b(fights?|fighting|fought|punch(?:es|ed|ing)?|kick(?:s|ed|ing)?|strikes?|hits?|beats?)\b', re.IGNORECASE), 'impact'),
    (re.compile(r'\b(kills?|killing|killed|murders?|murdered)\b', re.IGNORECASE), 'falls still'),
    (re.compile(r'\b(dies?|dying|dead|death)\b', re.IGNORECASE), 'final moment'),
    (re.compile(r'\b(corpse|corpses|dead body|dead bodies)\b', re.IGNORECASE), 'motionless figure on the ground'),
    (re.compile(r'\b(blood|bleeds?|bleeding|bled)\b', re.IGNORECASE), 'crimson liquid'),
    (re.compile(r'\b(wounds?|wounded|injuries|injured|hurt)\b', re.IGNORECASE), 'surface damage'),
    (re.compile(r'\b(explodes?|exploded|exploding|explosions?)\b', re.IGNORECASE), 'rapid expansion'),
    (re.compile(r'\b(destroys?|destroyed|destroying|smashes?|smashed|smashing)\b', re.IGNORECASE), 'structural failure'),
    (re.compile(r'\b(crashes?|crashed|crashing)\b', re.IGNORECASE), 'sudden impact'),
    (re.compile(r'\bscreams?\b', re.IGNORECASE), 'open mouth'),
    (re.compile(r'\b(cries in pain|cried in pain|crying in pain)\b', re.IGNORECASE), 'contorted expression'),
    (re.compile(r'\b(child|kid|boy|girl|teen|teenager|baby|infant|minor|schoolgirl|schoolboy)\b', re.IGNORECASE), 'young person'),
    (re.compile(r'\b(violent|violence|brutal|graphic|gore|horror|torture|abuse|terrorist)\b', re.IGNORECASE), 'dramatic confrontation'),
    (re.compile(r'\b(victim|suicide)\b', re.IGNORECASE), 'figure in distress'),
    (re.compile(r'\b(nude|naked|sexy|seductive|erotic|intimate|undressed|lingerie)\b', re.IGNORECASE), 'composed appearance'),
    (re.compile(r'\b(passionate kisses?|passionate kissing)\b', re.IGNORECASE), 'close embrace'),
]

def _seedance_apply_banlist(text):
    """Run all 21 banlist replacements on the prompt text. Returns
    (replaced_text, list_of_applied_matches). Mirrors reference applyBanlist."""
    if not text:
        return text or '', []
    applied = []
    out = text
    for pattern, replacement in _SD_BANLIST:
        def _repl(m, _r=replacement):
            applied.append({'original': m.group(0), 'replaced': _r})
            return _r
        out = pattern.sub(_repl, out)
    return out, applied


# Forbidden cross-chunk references — kill autonomy. Mirrors reference FORBIDDEN_REFS.
_SD_FORBIDDEN_AUTONOMY_RE = [
    re.compile(r'\bsame\b', re.IGNORECASE),
    re.compile(r'\bstill\b', re.IGNORECASE),
    re.compile(r'\bas before\b', re.IGNORECASE),
    re.compile(r'\bas previous\b', re.IGNORECASE),
    re.compile(r'\bcontinues\b', re.IGNORECASE),
    re.compile(r'\bпрежний\b', re.IGNORECASE),
    re.compile(r'\bтот же\b', re.IGNORECASE),
]

def _seedance_validate_autonomy(segments_data):
    """Each segment's promptEn must NOT reference prior chunks ("same/still/as
    before/continues") — Seedance has no memory across chunks, so any reference
    to "earlier" content causes drift. Returns list of {code, message, anchor}."""
    warnings = []
    for spec, plan, prompt_text in segments_data:
        if not prompt_text:
            continue
        for rx in _SD_FORBIDDEN_AUTONOMY_RE:
            m = rx.search(prompt_text)
            if m:
                warnings.append({
                    'code': 'autonomy-violation',
                    'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}: запрещённое слово "{m.group(0)}" в promptEn — нарушает автономность.',
                    'anchor': spec['anchor'],
                })
    return warnings


def _seedance_validate_durations(segments_data):
    """Sum of shot lengths in actionTimeline must equal durationSec. Also flag
    monotone rhythm (all shots equal length)."""
    warnings = []
    for spec, plan, prompt_text in segments_data:
        if not isinstance(plan, dict):
            continue
        timeline = plan.get('actionTimeline') or []
        duration = plan.get('durationSec') or 0
        if not timeline or not duration:
            continue
        try:
            sum_shots = sum(int(s.get('toSec', 0)) - int(s.get('fromSec', 0)) for s in timeline)
        except Exception:
            sum_shots = -1
        if sum_shots != duration:
            warnings.append({
                'code': 'shot-sum-mismatch',
                'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}: сумма шотов {sum_shots}с ≠ durationSec={duration}с',
                'anchor': spec['anchor'],
            })
        # Monotone check — only when ≥2 shots
        if len(timeline) >= 2:
            try:
                lens = [int(s.get('toSec', 0)) - int(s.get('fromSec', 0)) for s in timeline]
                if all(l == lens[0] for l in lens):
                    warnings.append({
                        'code': 'monotone-rhythm',
                        'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}: все шоты {lens[0]}с — однообразная нарезка.',
                        'anchor': spec['anchor'],
                    })
            except Exception:
                pass
    return warnings


# Wardrobe-fidelity guard: items often hallucinated by AI even when not in
# canonical char description. If a word from this list appears in actionTimeline
# but is NOT in the character's wardrobe text → warning.
_SD_WARDROBE_WORDS = [
    'jacket', 'blazer', 'coat', 'overcoat', 'tie', 'scarf', 'hat', 'cap',
    'beanie', 'gloves', 'boots', 'sunglasses', 'glasses', 'watch', 'belt',
    'vest', 'hoodie', 'sweater'
]

def _seedance_validate_wardrobe(segments_data, wardrobe_by_tag):
    """If a segment's actionTimeline mentions a wardrobe word that's not in any
    of the segment's characters' canonical wardrobe descriptions — warn."""
    warnings = []
    for spec, plan, prompt_text in segments_data:
        if not isinstance(plan, dict):
            continue
        chars_in_scene = plan.get('charactersInSegment') or []
        tags_in_scene = [c.get('tag') for c in chars_in_scene if c.get('tag')]
        allowed_text = ' '.join(
            (wardrobe_by_tag.get(tag) or '').lower()
            for tag in tags_in_scene
        )
        timeline = plan.get('actionTimeline') or []
        for i, shot in enumerate(timeline):
            desc = (shot.get('description') or '').lower()
            for word in _SD_WARDROBE_WORDS:
                if re.search(r'\b' + re.escape(word) + r'\b', desc) and not re.search(r'\b' + re.escape(word) + r'\b', allowed_text):
                    warnings.append({
                        'code': 'wardrobe-mismatch',
                        'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}, шот {i+1}: упомянут "{word}", но ни у одного персонажа в сцене его нет в wardrobe.',
                        'anchor': spec['anchor'],
                    })
    return warnings


def _seedance_filter_blocking_for_chunk(blocking_text, tags_in_chunk):
    """Drop sentences in blocking that mention @ImageN tags NOT used by this
    chunk — otherwise blocking text leaks descriptions of off-frame chars into
    the chunk's prompt and Seedance tries to render them.
    Mirrors reference filterBlockingForChunk."""
    if not blocking_text:
        return ''
    used_set = set(tags_in_chunk or [])
    parts = re.split(r'(?<=\.)\s+', blocking_text)
    kept = []
    for p in parts:
        matches = re.findall(r'@[Ii]mage(\d+)', p)
        if not matches:
            kept.append(p.strip())
            continue
        all_tags_in_part = {f'@Image{n}' for n in matches}
        if all_tags_in_part.issubset(used_set):
            kept.append(p.strip())
    return ' '.join(kept).strip()


def _seedance_inject_blocking(prompt_text, blocking_text, chunk_tags_used):
    """Insert filtered blocking BEFORE 'Constraints:' in the 6-block promptEn.
    Falls back to append if Constraints not found. Mirrors reference injectBlocking."""
    if not blocking_text or not blocking_text.strip():
        return prompt_text
    filtered = _seedance_filter_blocking_for_chunk(blocking_text, chunk_tags_used)
    if not filtered or len(filtered) < 30:
        return prompt_text
    trimmed = re.sub(r'\s+', ' ', filtered).strip()
    idx = prompt_text.rfind('Constraints:')
    if idx < 0:
        return prompt_text + f'\n\nScene blocking: {trimmed}'
    return (
        prompt_text[:idx]
        + f'Scene blocking (consistent across all chunks of this episode): {trimmed}\n\n'
        + prompt_text[idx:]
    )


_SD_HARD_CUTS_CLAUSE = (
    'hard cuts between shots with different camera angles, no fade transitions, '
    'no dissolves, no cross-fades, sharp instant edits between framings'
)

def _seedance_inject_hard_cuts(prompt_text):
    """Append hard-cuts directive to Constraints. Mirrors reference injectHardCuts.
    Programmatic — more reliable than asking AI to write it."""
    if not prompt_text or 'hard cuts between shots' in prompt_text:
        return prompt_text
    idx = prompt_text.rfind('Constraints:')
    if idx < 0:
        return prompt_text + f'\n\nConstraints: {_SD_HARD_CUTS_CLAUSE}'
    # Append at end of the last (Constraints) line
    return re.sub(
        r'(Constraints:[^\n]*?)(\s*)$',
        lambda m: f'{m.group(1)}, {_SD_HARD_CUTS_CLAUSE}',
        prompt_text, count=1, flags=re.DOTALL
    )


def _prev_episode_ending_context(sid, num):
    """Build a multi-section text describing how episode N-1 ended — used as
    continuity context when generating scene_blocking / batch-compose for N.
    TikTok-format episodes often pick up immediately where N-1 left off, so
    character positions must be inherited (Adrian still at the door, Clara
    still at the desk) instead of reset.

    Returns None when there's no prev episode or no useful context.
    Sections (concat'd with blank lines):
      1. Last scene heading of N-1's script
      2. Last 6 non-empty lines of N-1's script
      3. N-1's batch_episode_blocking (whole-episode geometry from last build)
      4. Last completed seedance chunk's ending_state (if poll captured it)
    """
    try:
        n = int(num)
    except Exception:
        return None
    if n <= 1:
        return None
    prev_ep = load_episode(sid, n - 1)
    if not prev_ep:
        return None
    parts = []

    prev_script = (prev_ep.get('script') or '').strip()
    if prev_script:
        last_heading = None
        for line in prev_script.split('\n'):
            t = line.strip()
            if is_scene_heading(t):
                last_heading = t
        if last_heading:
            parts.append(f"PREV EP {n-1} — last scene heading:\n{last_heading[:160]}")

        non_empty = [l.strip() for l in prev_script.split('\n') if l.strip()]
        clean = [l for l in non_empty
                 if not l.startswith('- **')
                 and not l.startswith('**КРАТКОЕ')
                 and not l.startswith('## ')]
        tail = clean[-6:] if len(clean) > 6 else clean
        if tail:
            parts.append("PREV EP — last 6 lines (immediate beats before cut):\n" + '\n'.join(tail))

    prev_blocking = (prev_ep.get('batch_episode_blocking') or prev_ep.get('scene_blocking') or '').strip()
    if prev_blocking:
        parts.append(f"PREV EP — episodeBlocking (whole-episode geometry):\n{prev_blocking[:1500]}")

    chunks = prev_ep.get('seedance_chunks') or []
    last_completed = None
    for c in reversed(chunks):
        if c.get('status') == 'completed' and (c.get('ending_state') or '').strip():
            last_completed = c
            break
    if last_completed:
        es = (last_completed.get('ending_state') or '').strip()
        parts.append(f"PREV EP — last generated chunk ending state (final tableau before cut):\n{es[:400]}")

    if not parts:
        return None
    return '\n\n'.join(parts)


def _build_episode_tag_mapping(script_text, active_chars, active_locs):
    """Deterministic tag mapping `@Image1=name1, @Image2=name2, ...` based on
    first-appearance order in the script. Run BEFORE the AI call so Claude
    receives a fixed mapping and can't shuffle indices between segments.

    Strategy:
      1. Find first occurrence of each active character's name in script.
      2. Find first occurrence of each active location's name (or scene
         heading slug) in script.
      3. Sort by position; assign @Image1, @Image2, ...
    Returns list of {tag, kind: 'char'|'loc', id, name}.
    """
    if not script_text:
        return []
    lower = script_text.lower()
    entries = []
    seen_ids = set()
    for c in active_chars or []:
        nm = (c.get('name') or '').strip()
        if not nm or c['id'] in seen_ids: continue
        # Find first appearance — prefer line-start "Name:" cue, fall back to any mention
        pat_cue = re.compile(r'^\s*' + re.escape(nm) + r'\s*[:：]', re.MULTILINE | re.IGNORECASE)
        m = pat_cue.search(script_text)
        pos = m.start() if m else lower.find(nm.lower())
        if pos >= 0:
            entries.append({'kind': 'char', 'id': c['id'], 'name': nm, 'pos': pos})
            seen_ids.add(c['id'])
    for l in active_locs or []:
        nm = (l.get('name') or '').strip()
        if not nm: continue
        pos = lower.find(nm.lower())
        if pos < 0:
            # Try scene-heading style (INT./EXT. NAME — TIME)
            pat = re.compile(
                r'(?:INT\.|EXT\.|ИНТ\.|ИНТА\.|ЭКСТ\.|ЭКС\.|НАТ\.|ВНУТР\.|ИНТЕРЬЕР|Локация\s*:)\s*' +
                re.escape(nm), re.IGNORECASE
            )
            m = pat.search(script_text)
            pos = m.start() if m else -1
        if pos >= 0:
            entries.append({'kind': 'loc', 'id': l['id'], 'name': nm, 'pos': pos})
    entries.sort(key=lambda e: e['pos'])
    out = []
    for i, e in enumerate(entries):
        out.append({
            'tag': f'@Image{i+1}',
            'kind': e['kind'],
            'id': e['id'],
            'name': e['name'],
        })
    return out


def _canonical_char_description(s, char_id, outfit_label):
    """Canonical description used BOTH for image generation AND for Seedance
    BINDING — same text in both places guarantees visual+textual alignment.

    Composition:
      - char.appearance (face/body/hair/etc.) — the same text the image-gen
        prompt used to render the photo.
      - + chosen outfit description (if outfit_label given and matches),
        else the base outfit's description.

    This REPLACES the older Vision-extracted approach (which re-analyzed the
    photo and could drift from the original prompt). The canonical text always
    matches the artist's intent, never re-interpreted from pixels.
    """
    char = next((c for c in (s.get('characters') or []) if c['id'] == char_id), None)
    if not char:
        return ''
    appearance = (char.get('appearance') or '').strip()
    outfit_desc = ''
    outfits = char.get('outfits') or []
    is_base_request = (not outfit_label) or outfit_label.lower() in ('base', '')
    using_non_base_outfit = False
    if outfit_label and not is_base_request:
        chosen = next((o for o in outfits if o.get('label') == outfit_label), None)
        if chosen:
            outfit_desc = (chosen.get('description') or '').strip()
            using_non_base_outfit = True
    if not outfit_desc:
        # Look ONLY for an explicitly-flagged base outfit. Do NOT fall back to
        # `outfits[0]` — that's whichever scene-specific outfit happened to be
        # listed first (e.g. "morning_aftermath: silk slip dress, bare shoulders").
        # If we appended that to a character's base appearance ("wearing wool coat"),
        # the model saw two conflicting outfits in one description and rendered
        # a torn-sleeve hybrid (May 2026 Lydia incident).
        base = next((o for o in outfits if o.get('is_base')), None)
        if base:
            outfit_desc = (base.get('description') or '').strip()
        # If no IS_BASE outfit exists, the appearance text itself usually contains
        # the base outfit description (cast-block parser embeds "wearing X" into
        # appearance when IS_BASE is set without a separate outfit entry). Trust
        # appearance as-is — don't pollute with a random outfit.
    # When switching to a NON-base outfit (hospital_gown for an episode where
    # the character is in hospital), strip the embedded "wearing <base outfit>"
    # tail from appearance so we don't end up with two outfits at once. Without
    # this, BINDING would say "Claire wearing charcoal grey blazer; pale blue
    # hospital gown" — Seedance renders a hybrid (May 2026 Claire incident).
    if using_non_base_outfit and appearance:
        appearance = re.sub(
            r'\s*,?\s*wearing\b[^.;]*$', '', appearance, flags=re.IGNORECASE
        ).rstrip(' ,;.').strip()
    parts = [p for p in (appearance, outfit_desc) if p]
    full = '; '.join(parts)
    # Strip stray newlines and cap length to keep BINDING manageable
    full = re.sub(r'\s+', ' ', full).strip()
    return full[:300]


def strip_json(raw: str) -> str:
    """Extract a JSON object/array from a model response.
    Handles: bare JSON, ```json fenced blocks, JSON with trailing summary text after the closing brace.
    """
    import re
    raw = raw.strip()
    # Strip leading code fence if present
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    # If a closing fence exists, cut everything from it onwards
    fence_close = raw.find('\n```')
    if fence_close != -1:
        raw = raw[:fence_close]
    raw = raw.strip()
    # If there's still trailing text after the JSON object, find the matching brace
    # and trim. Walk braces honoring strings.
    if raw and raw[0] in '{[':
        open_ch, close_ch = ('{', '}') if raw[0] == '{' else ('[', ']')
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i, ch in enumerate(raw):
            if in_str:
                if esc:        esc = False
                elif ch == '\\': esc = True
                elif ch == '"': in_str = False
                continue
            if ch == '"': in_str = True
            elif ch == open_ch:  depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end != -1:
            raw = raw[:end]
    return raw.strip()

def _repair_llm_json(s: str) -> str:
    """Best-effort repair of common LLM JSON mistakes: trailing commas,
    unquoted property keys, single-quoted keys/strings."""
    import re
    # Remove trailing commas before } or ]
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    # Quote unquoted object keys: {  foo: ...  -> {"foo": ...
    # Match {/, then whitespace, then identifier followed by :
    s = re.sub(r'([{,])(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1\2"\3"\4:', s)
    # Convert single-quoted keys to double: 'foo': -> "foo":
    s = re.sub(r"([{,])(\s*)'([^'\n]*)'(\s*):", r'\1\2"\3"\4:', s)
    return s

def _strip_markdown_fence(raw: str) -> str:
    """Remove ```json ... ``` (or just ``` ... ```) wrappers that some Claude
    responses bring back even when the system prompt says strict JSON. Cheap
    pre-clean before json.loads."""
    s = (raw or '').strip()
    # Leading ```json\n or ```\n
    s = re.sub(r'^```(?:json|JSON)?[ \t]*\n?', '', s)
    # Trailing ``` (with optional preceding newline)
    s = re.sub(r'\n?```[ \t]*$', '', s)
    return s

def loads_lenient(raw: str):
    """json.loads with markdown-fence strip + repair fallback."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try after stripping ```json fences (Claude sometimes wraps JSON in them
    # despite the prompt asking for strict JSON).
    s = _strip_markdown_fence(raw)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(_repair_llm_json(s))

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

def rtl_headers():
    return {'Authorization': f'Bearer {_get_user_reteller_key()}'}

def _avai_call(provider: str, prompt: str, reference_url: str = None, aspect_ratio: str = '9:16') -> str:
    """One AVAI call with the given provider. Returns image URL or raises.
    Generates at 1K JPEG instead of 2K PNG — character/location refs are
    used by Seedance internally, which downscales them anyway. 2K PNG was
    bloating each asset to 4-7MB, killing disk + bandwidth on the prod
    volume. 1K JPEG ≈ 200-500KB with no visible quality loss for ref usage.
    """
    payload = {
        'provider': provider,
        'prompt': prompt,
        'num_images': 1,
        'aspect_ratio': aspect_ratio,
        'image_size': '1K',
        'output_format': 'jpg',
    }
    if provider == 'banana':
        payload['model'] = 'pro'
    if reference_url:
        payload['contextImages'] = [{'url': reference_url}]
    avai_key = _get_user_avai_key()
    if not avai_key:
        # Empty key → AVAI returns generic 401. Surface a specific error so the
        # frontend can route the user to Settings instead of showing a wall of
        # raw "Unauthorized" JSON. Code = AVAI_KEY_MISSING.
        raise RuntimeError('AVAI_KEY_MISSING: AVAI API key не задан в твоём аккаунте — открой Settings и введи свой ключ с avai-gen.com')
    headers = {'x-api-key': avai_key, 'content-type': 'application/json'}
    resp = requests.post(AVAI_API, json=payload, headers=headers, timeout=180)
    if resp.status_code == 401:
        # Server got the key, but it's invalid/expired. User needs to refresh
        # their key. Different code than missing — different remedy hint.
        raise RuntimeError('AVAI_KEY_INVALID: AVAI отверг твой API key (401 Unauthorized) — проверь что не истёк, и обнови в Settings → AVAI key')
    if not resp.ok:
        raise RuntimeError(f'AVAI {provider} error {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f'AVAI {provider} generation failed: {str(data)[:300]}')
    images = data.get('images') or []
    image_url = images[0] if images else ''
    # Detect content-filter / placeholder responses (e.g. '/error.jpg' from Banana
    # when Gemini rejects the prompt). These are not real URLs.
    if not image_url or not image_url.lower().startswith(('http://', 'https://')):
        raise RuntimeError(f'AVAI {provider} returned no valid URL (likely content-filter rejection)')
    return image_url


def _series_image_provider(s):
    """Read per-series user preference for image provider order.
    Returns one of: 'banana', 'seedream', '' (auto). Stored on series.json
    via the per-series toolbar dropdown."""
    pref = (s or {}).get('preferred_image_provider', '') or ''
    return pref if pref in ('banana', 'seedream') else ''


def avai_generate(prompt: str, output_path: Path, reference_url: str = None, aspect_ratio: str = '9:16', preferred_provider: str = '') -> str:
    """Generate via AVAI. Tries Banana (Gemini Image Pro) first; on failure
    falls back to Seedream (NOT Seedance — Seedance is video, we need an image)
    with the same prompt + reference. Returns the remote image URL on success,
    raises with a combined error message on total failure.
    aspect_ratio: '9:16' (vertical, default — characters/portraits) or '16:9' (horizontal — locations).
    preferred_provider: '' / 'auto' (default banana→seedream), 'banana', 'seedream' — explicit
    user override via per-series toggle. If specified, that provider runs FIRST."""
    errors = []
    image_url = None
    if preferred_provider == 'seedream':
        provider_chain = ('seedream', 'banana')
    elif preferred_provider == 'banana':
        provider_chain = ('banana', 'seedream')
    else:
        provider_chain = ('banana', 'seedream')
    for provider in provider_chain:
        try:
            image_url = _avai_call(provider, prompt, reference_url=reference_url, aspect_ratio=aspect_ratio)
            print(f'[avai_generate] {provider} OK → {image_url[:80]}...')
            break
        except Exception as e:
            msg = str(e)
            print(f'[avai_generate] {provider} FAILED: {msg[:200]}')
            errors.append(f'{provider}: {msg}')
            continue
    if not image_url:
        # If ANY error mentions our specific auth markers, the issue is the API
        # key — not the prompt. Show a focused message instead of suggesting
        # "rewrite description more neutrally" which doesn't fix anything.
        joined = ' | '.join(errors)
        if 'AVAI_KEY_MISSING' in joined:
            raise RuntimeError('AVAI_KEY_MISSING: AVAI API key не задан в твоём аккаунте — открой Settings и введи свой ключ с avai-gen.com')
        if 'AVAI_KEY_INVALID' in joined or '401' in joined:
            raise RuntimeError('AVAI_KEY_INVALID: AVAI отверг твой API key (401 Unauthorized). Возможные причины: ключ истёк / неверный / лимит средств исчерпан. Открой Settings → AVAI key и обнови.')
        raise RuntimeError(
            'Оба провайдера AVAI отказали. '
            + joined
            + ' — попробуй переписать описание более нейтрально '
              '(без слов lingerie / bare chest / boxers).'
        )
    # Download and save
    img_resp = requests.get(image_url, timeout=60)
    img_resp.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_resp.content)
    # Lazy migration: AVAI returns 1K JPEG (output_format=jpg), so legacy .png
    # files at the same stem are stale 2K PNG (or older mis-named JPEGs) and
    # should be cleaned up to avoid (a) wasting disk on prod volume and
    # (b) confusing tools that pick the .png by alphabetical sort. Only purge
    # when we just wrote .jpg — never the other way.
    if output_path.suffix.lower() in ('.jpg', '.jpeg'):
        legacy_png = output_path.with_suffix('.png')
        if legacy_png.exists() and legacy_png != output_path:
            try:
                legacy_png.unlink()
            except Exception as e:
                print(f'[avai_generate] could not remove legacy {legacy_png}: {e}')
    return image_url

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Storage helpers ──────────────────────────────────────────────────────────

def _safe_email_dir(email):
    """Map an email to a safe folder name. user@gamegears.online → user_at_gamegears_online."""
    return re.sub(r'[^a-z0-9]+', '_', (email or '').lower()).strip('_') or 'anon'

def user_root():
    """Returns Path to current user's project root: <DATA_ROOT>/<email-slug>/projects/.
    Creates it on first call. Performs a one-time migration from legacy
    single-user `projects/` for the dev user."""
    email = current_user_email() or DEV_USER_EMAIL
    udir = DATA_ROOT / _safe_email_dir(email) / 'projects'
    udir.mkdir(parents=True, exist_ok=True)
    # Idempotent migration from legacy single-user `projects/` for the dev user.
    # For each non-hidden entry in legacy: if missing in user dir, move it over.
    # Hidden/macOS metadata (._foo, .DS_Store) is ignored so it doesn't block the migration.
    if email == DEV_USER_EMAIL and LEGACY_PROJECTS.exists():
        try:
            for entry in LEGACY_PROJECTS.iterdir():
                if entry.name.startswith('.') or entry.name.startswith('._'):
                    continue
                target = udir / entry.name
                if target.exists():
                    continue
                shutil.move(str(entry), str(target))
                print(f'[migrate] {entry.name} → {udir.name}/')
        except Exception as e:
            print(f'[migrate] WARNING failed: {e}')
    return udir

def series_path(sid):   return user_root() / sid
def series_file(sid):   return series_path(sid) / 'series.json'
def episodes_dir(sid):  return series_path(sid) / 'episodes'
def assets_dir(sid):    return series_path(sid) / 'assets'
def vid_dir(sid):       return series_path(sid) / 'VID'
def out_dir(sid):       return series_path(sid) / 'OUT'
def facades_dir(sid):   return series_path(sid) / 'assets' / 'facades'

# Template Premiere Pro project. The user drops a blank .prproj here once
# (created in Premiere via File → New Project → save as "empty.prproj") and
# every newly-created series gets a copy named after the series title.
PRPROJ_TEMPLATE = Path(__file__).parent / 'templates' / 'empty.prproj'


def scaffold_series_folders(sid: str, title: str) -> dict:
    """Create the standard layout for a new series:
      <sid>/
        assets/
        episodes/
        VID/        — input video material
        OUT/        — exported renders
        <title>.prproj — empty Premiere Pro project (copied from template)
    Returns dict with status flags so the caller can surface warnings.
    Idempotent — won't overwrite an existing .prproj or wipe folders."""
    base = series_path(sid)
    base.mkdir(parents=True, exist_ok=True)
    assets_dir(sid).mkdir(exist_ok=True)
    episodes_dir(sid).mkdir(exist_ok=True)
    vid_dir(sid).mkdir(exist_ok=True)
    out_dir(sid).mkdir(exist_ok=True)

    result = {'prproj_created': False, 'prproj_warning': None}
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', (title or sid)).strip() or sid
    prproj_path = base / f'{safe_name}.prproj'
    if prproj_path.exists():
        result['prproj_warning'] = 'already exists, kept as is'
        return result
    if PRPROJ_TEMPLATE.exists():
        try:
            shutil.copyfile(PRPROJ_TEMPLATE, prproj_path)
            result['prproj_created'] = True
        except Exception as e:
            result['prproj_warning'] = f'copy failed: {e}'
    else:
        # Leave a marker file so the user sees the path where the .prproj
        # would have been — and the README explains how to enable it.
        msg = (
            'Premiere Pro template not found. To enable auto-creation of\n'
            f'a blank .prproj per new series, place a saved blank Premiere\n'
            f'project at:\n  {PRPROJ_TEMPLATE}\n\n'
            'Steps: open Premiere → File → New Project → leave default\n'
            'settings → save as empty.prproj → put it in the path above.\n'
            'Then re-create the series, or copy this template manually.'
        )
        try:
            (base / 'PRPROJ_TEMPLATE_MISSING.txt').write_text(msg)
        except Exception:
            pass
        result['prproj_warning'] = 'template empty.prproj not found in templates/'
        print(f'[scaffold_series_folders] {sid}: {result["prproj_warning"]}', flush=True)
    return result

# File-write serialization: per-path lock so concurrent threads don't race on
# the same JSON file. Without this, two threads doing write_text on the same
# path can produce a corrupted "Extra data" file (writer A's content followed
# by writer B's tail), which then breaks load_episode forever.
_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()

def _file_lock(path):
    key = str(path)
    with _FILE_LOCKS_GUARD:
        lk = _FILE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _FILE_LOCKS[key] = lk
        return lk

def _atomic_write_json(path, data):
    """Write JSON atomically: dump to a sibling tmp file, fsync, os.replace.
    Combined with a per-path threading.Lock, this guarantees readers always
    see either the old complete content or the new complete content — never
    a half-written file. `os.replace` is atomic on POSIX."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.tmp.{os.getpid()}.{threading.get_ident()}')
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with _file_lock(path):
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # some filesystems don't support fsync
        os.replace(tmp, path)

def _load_json_resilient(path):
    """Read a JSON file. If it's been corrupted by a non-atomic concurrent
    write (symptom: `JSONDecodeError: Extra data: line N column M`), recover
    by parsing only the first complete object via `raw_decode` and rewriting
    the file with the recovered content. Logs the recovery so we know it
    happened. Returns the parsed object, or raises if even the first object
    is unparseable."""
    raw = Path(path).read_text(encoding='utf-8')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        if 'Extra data' not in str(e):
            raise
        try:
            obj, end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            raise  # truly broken — surface original error to caller
        print(f'[recover] {path}: corrupted by concurrent write '
              f'(extra {len(raw) - end} bytes after offset {end}); '
              f'rewriting with recovered prefix', flush=True)
        try:
            _atomic_write_json(path, obj)
        except Exception as we:
            print(f'[recover] {path}: failed to rewrite recovered content: {we}', flush=True)
        return obj

def load_series(sid):
    f = series_file(sid)
    if not f.exists():
        return None
    data = _load_json_resilient(f)
    # Forward-compat defaults so old series.json don't break new features.
    data.setdefault('video_provider', 'reteller')        # 'reteller' | 'seedance'
    data.setdefault('auto_reteller_prompt', True)        # auto-build Reteller prompt after script gen
    data.setdefault('items', [])                         # story-relevant props (handbag, gun, locket...)
    return data

def save_series(sid, data):
    series_path(sid).mkdir(exist_ok=True)
    _atomic_write_json(series_file(sid), data)

def load_episode(sid, num):
    f = episodes_dir(sid) / f'{int(num):03d}.json'
    return _load_json_resilient(f) if f.exists() else None

def save_episode(sid, num, data):
    episodes_dir(sid).mkdir(exist_ok=True)
    _atomic_write_json(episodes_dir(sid) / f'{int(num):03d}.json', data)

def list_episodes(sid):
    d = episodes_dir(sid)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob('*.json')):
        if f.name.startswith('._') or '.tmp.' in f.name:
            continue
        try:
            out.append(_load_json_resilient(f))
        except Exception as e:
            print(f'[list_episodes] skipping unreadable {f.name}: {e}', flush=True)
    return out


def is_batch_mode(s):
    """Series uses batch mode (one storage record covers N consecutive episodes)."""
    return bool((s or {}).get('batch_mode'))

def batch_size(s):
    return int((s or {}).get('batch_size') or 1) if is_batch_mode(s) else 1

def chunk_range(s, num):
    """Sub-episode range a chunk covers, e.g. chunk #2 with batch_size=5 → (6, 10).
    For non-batch series, returns (num, num)."""
    bs = batch_size(s)
    if bs <= 1: return (num, num)
    return ((num - 1) * bs + 1, num * bs)

def chunk_label(s, num):
    """Human label: 'Серии 1-5' for batch chunk #1, 'Эпизод 7' otherwise."""
    if is_batch_mode(s):
        a, b = chunk_range(s, num)
        return f'Серии {a}–{b}'
    return f'Эпизод {num}'

# Total number of sub-episodes a series spans (hardcoded short-drama format = 70).
TOTAL_SUB_EPS = 70

def chunk_count(s):
    """Total addressable storage records for a series.
    Non-batch → 70, batch_size=5 → 14."""
    bs = batch_size(s) or 1
    return (TOTAL_SUB_EPS + bs - 1) // bs

def ep_to_chunk(s, ep_num):
    """Map a sub-episode index (1..70) to its containing chunk index."""
    bs = batch_size(s) or 1
    if bs <= 1: return ep_num
    return (ep_num - 1) // bs + 1

def anchor_chunks(s):
    """Anchor storage indices for the 3 required milestones (Pilot / Turn / Finale).
    In batch mode these collapse to chunk-numbers; e.g. bs=5 → [1, 2, 14]."""
    return sorted({ep_to_chunk(s, n) for n in (1, 10, TOTAL_SUB_EPS)})

def milestone_indices(s):
    """All 8 milestone slots projected to chunk space (deduped, sorted)."""
    bs = batch_size(s) or 1
    if bs <= 1:
        return [1, 10, 20, 30, 40, 50, 60, 70]
    return sorted({ep_to_chunk(s, n) for n in (1, 10, 20, 30, 40, 50, 60, TOTAL_SUB_EPS)})


# ── Series Canon (story bible) ──────────────────────────────────────────────
# Single JSON file per series tracking timeline, locked facts, character knowledge
# state and open story threads. Fully automated — never edited by hand.
def canon_file(sid):  return series_path(sid) / 'canon.json'

def _empty_canon():
    return {
        'version': 1,
        'world_clock': {'current_day': 0, 'last_episode': 0},
        'timeline': [],          # [{ep, day, events:[...]}]
        'facts': [],             # [{id, ep, fact, locked:bool, supersedes:id|null}]
        'character_state': {},   # name -> {knows:[fact_ids], suspects:[...], physical:{}, location:str}
        'open_threads': [],      # [{id, opened_ep, question, status, resolved_ep?}]
        'audit_log': [],         # [{ep, ts, passes, retries, violations:[...]}]
    }

def load_canon(sid):
    f = canon_file(sid)
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            # Self-heal missing keys for forward compat
            base = _empty_canon()
            base.update(data)
            for k, v in _empty_canon().items():
                base.setdefault(k, v)
            return base
        except Exception:
            pass
    return _empty_canon()

def save_canon(sid, canon):
    series_path(sid).mkdir(exist_ok=True)
    canon_file(sid).write_text(json.dumps(canon, indent=2, ensure_ascii=False))

def _next_id(prefix, items):
    n = 0
    for it in items:
        i = it.get('id', '')
        if i.startswith(prefix):
            try: n = max(n, int(i[len(prefix):]))
            except ValueError: pass
    return f'{prefix}{n+1:03d}'

# Real-world physics/biology/protocol constants the writer must respect.
# Injected into the logic brief verbatim so the model can do correct arithmetic.
WORLD_RULES = {
    'biology': {
        'pregnancy_test_min_days_after_conception': 10,
        'pregnancy_first_visible_symptoms_weeks': 6,
        'wound_healing_visible_days': 3,
        'bruise_fade_days': 7,
        'hair_grow_visible_cm_per_month': 1.25,
    },
    'military_protocol': {
        'undercover_op_min_setup_days': 30,
        'deployment_notice_min_hours': 24,
        'base_transfer_min_hours': 6,
    },
    'travel': {
        'intercontinental_flight_min_hours': 8,
        'cross_city_min_minutes': 30,
        'across_base_min_minutes': 5,
    },
    'legal_finance': {
        'will_probate_min_weeks': 4,
        'corporate_takeover_min_weeks': 2,
        'paternity_test_min_days': 3,
    },
}


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
            if ep_dir.exists():
                for p in ep_dir.glob('*.json'):
                    # Filter out macOS AppleDouble metadata (._*) and any dotfile
                    if p.name.startswith('.'):
                        continue
                    total += 1
                    try:
                        ep_data = json.loads(p.read_text())
                    except Exception:
                        continue
                    if ep_data.get('ready') or ep_data.get('reteller', {}).get('project_id'):
                        ready += 1
            s['_episode_count'] = ready
            s['_episode_total'] = total
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


# ── Story landmarks: checkpoints + finale ────────────────────────────────────
def _norm_checkpoint(d):
    """Validate + normalize a checkpoint dict."""
    try:
        ep = int(d.get('episode'))
    except (TypeError, ValueError):
        raise ValueError('episode must be an integer')
    if ep < 1:
        raise ValueError('episode must be ≥ 1')
    desc = (d.get('description') or '').strip()
    return {'episode': ep, 'description': desc}


@app.route('/api/series/<sid>/checkpoints', methods=['POST'])
def add_or_update_checkpoint(sid):
    """Create or update a story-checkpoint at a given episode number."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    try:
        cp = _norm_checkpoint(body)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    cps = [c for c in (s.get('checkpoints') or []) if int(c.get('episode', 0)) != cp['episode']]
    cps.append(cp)
    cps.sort(key=lambda c: c['episode'])
    s['checkpoints'] = cps
    save_series(sid, s)
    return jsonify({'checkpoints': cps})


@app.route('/api/series/<sid>/checkpoints/<int:ep>', methods=['DELETE'])
def delete_checkpoint(sid, ep):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['checkpoints'] = [c for c in (s.get('checkpoints') or []) if int(c.get('episode', 0)) != ep]
    save_series(sid, s)
    return jsonify({'checkpoints': s['checkpoints']})


@app.route('/api/series/<sid>/checkpoints/<int:ep>/generate', methods=['POST'])
def generate_checkpoint(sid, ep):
    """Use Claude to draft a strong story-twist for a checkpoint at episode `ep`."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    cps_other = [c for c in (s.get('checkpoints') or []) if int(c.get('episode', 0)) != ep]
    fin = s.get('finale') or {}
    other_block = ''
    if cps_other:
        other_block = 'Other checkpoints already pinned:\n' + '\n'.join(
            f"  - Ep {c['episode']}: {c.get('description','—')}" for c in sorted(cps_other, key=lambda x: x['episode'])
        ) + '\n'
    fin_block = ''
    if fin and fin.get('description'):
        fin_block = f"Series finale (ep {fin.get('episode','?')}): {fin['description']}\n"
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Logline: {s.get("synopsis","")}\nArc: {s.get("arc","")}\n\n'
        + other_block + fin_block +
        f'\nDraft ONE strong story checkpoint that should hit at EPISODE {ep}. '
        'It must be a single sharp dramatic event — a major reversal, betrayal, reveal, '
        'public scandal, death, return, identity exposed, or power flip — that the writer '
        'must steer toward and that will reshape the whole arc afterwards. '
        'Be SPECIFIC: name the character(s) involved (use exact names from the bible if provided), '
        'name the action, name the consequence. 2–4 sentences max. '
        'Output ONLY the checkpoint description, no preface, no JSON, no markdown.'
    )
    try:
        text = claude_ask_fast(
            prompt,
            system=(
                'You are a short-drama showrunner pitching mid-arc twists. Write in Russian. '
                'Use exact character names. Keep it punchy, concrete, irreversible.'
            ),
        )
    except Exception as e:
        return jsonify({'error': f'generation failed: {e}'}), 500
    return jsonify({'description': (text or '').strip(), 'episode': ep})


@app.route('/api/series/<sid>/finale', methods=['PUT'])
def set_finale(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    if body is None or (body.get('episode') is None and body.get('description') is None):
        s['finale'] = None
        save_series(sid, s)
        return jsonify({'finale': None})
    try:
        ep = int(body.get('episode'))
    except (TypeError, ValueError):
        return jsonify({'error': 'episode must be an integer'}), 400
    if ep < 1:
        return jsonify({'error': 'episode must be ≥ 1'}), 400
    s['finale'] = {'episode': ep, 'description': (body.get('description') or '').strip()}
    save_series(sid, s)
    return jsonify({'finale': s['finale']})


@app.route('/api/series/<sid>/finale', methods=['DELETE'])
def delete_finale(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['finale'] = None
    save_series(sid, s)
    return jsonify({'finale': None})


@app.route('/api/series/<sid>/finale/generate', methods=['POST'])
def generate_finale(sid):
    """Use Claude to draft a finale for a given episode number."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    try:
        ep = int(body.get('episode'))
    except (TypeError, ValueError):
        return jsonify({'error': 'episode must be an integer'}), 400

    cps = sorted((s.get('checkpoints') or []), key=lambda c: int(c.get('episode', 0)))
    cps_block = ''
    if cps:
        cps_block = 'Checkpoints already pinned:\n' + '\n'.join(
            f"  - Ep {c['episode']}: {c.get('description','—')}" for c in cps
        ) + '\n'
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Logline: {s.get("synopsis","")}\nArc: {s.get("arc","")}\n\n'
        + cps_block +
        f'\nWrite the SERIES FINALE for episode {ep}. '
        'It must be a satisfying climax that pays off the central conflict and the romance/revenge/identity arc. '
        'Specify: (1) WHO is alive, dead, exposed, redeemed, in power; (2) WHAT the final emotional beat is; '
        '(3) WHAT explicitly cannot change between now and then (e.g. character X must be alive, '
        'character Y must still hold the secret, the contract must remain unsigned, etc.). '
        '4–7 sentences. Russian. Output ONLY the finale description.'
    )
    try:
        text = claude_ask_fast(
            prompt,
            system='You are a short-drama showrunner writing series finales. Russian. Concrete characters and stakes.',
        )
    except Exception as e:
        return jsonify({'error': f'generation failed: {e}'}), 500
    return jsonify({'description': (text or '').strip(), 'episode': ep})


# ── Story trajectory builder (used by writer prompts) ────────────────────────
def build_trajectory_block(s, current_ep):
    """Render upcoming checkpoints + finale into a context block injected into
    the script writer's prompt. Empty string if nothing to steer toward."""
    cps_all = s.get('checkpoints') or []
    fin = s.get('finale') or None
    upcoming = [c for c in cps_all if int(c.get('episode', 0)) >= current_ep and (c.get('description') or '').strip()]
    upcoming.sort(key=lambda c: int(c['episode']))
    fin_relevant = bool(fin and fin.get('description', '').strip()
                        and int(fin.get('episode', 0)) >= current_ep)

    if not upcoming and not fin_relevant:
        return ''

    lines = ['═══ NARRATIVE TRAJECTORY — STEER TOWARD THESE LANDMARKS ═══']

    if upcoming:
        lines.append('UPCOMING CHECKPOINTS (you must seed setup so these can hit on time):')
        for c in upcoming:
            ep_n = int(c['episode'])
            distance = ep_n - current_ep
            tag = 'THIS EPISODE — must happen here' if distance == 0 else f'{distance} ep(s) away'
            lines.append(f'  · Ep {ep_n} ({tag}): {c["description"].strip()}')
        if any(int(c["episode"]) - current_ep > 0 for c in upcoming):
            lines.append('Rule: do NOT prematurely fire a future checkpoint. Plant seeds now (entrances, '
                         'unspoken motives, prop placements, whispered allusions) so the checkpoint payoff '
                         'feels earned and inevitable when its episode arrives.')

    if fin_relevant:
        ep_n = int(fin['episode'])
        distance = ep_n - current_ep
        when = 'this episode' if distance == 0 else f'{distance} ep(s) away'
        lines.append(f'\nSERIES FINALE (ep {ep_n}, {when}): {fin["description"].strip()}')
        lines.append('HARD CONSTRAINTS from finale: any character or condition the finale relies on '
                     '(alive, in power, holding a secret, separated, married, pregnant, free, etc.) '
                     'MUST remain achievable from this episode onward. Do NOT kill, expose, or '
                     'permanently remove anyone the finale needs — and do not resolve a conflict the '
                     'finale needs unresolved.')

    lines.append('═══════════════════════════════════════════════════')
    return '\n'.join(lines) + '\n\n'


@app.route('/api/series/<sid>/archive', methods=['POST'])
def toggle_archive(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    if 'archived' in body:
        s['archived'] = bool(body['archived'])
    else:
        s['archived'] = not bool(s.get('archived'))
    s['archived_at'] = time.time() if s['archived'] else 0
    # Archived projects shouldn't stay pinned at the top of the active list
    if s['archived'] and s.get('pinned'):
        s['pinned'] = False
        s['pinned_at'] = 0
    save_series(sid, s)
    return jsonify({'archived': s['archived']})


@app.route('/api/series/<sid>/pin', methods=['POST'])
def toggle_pin(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    if 'pinned' in body:
        s['pinned'] = bool(body['pinned'])
    else:
        s['pinned'] = not bool(s.get('pinned'))
    s['pinned_at'] = time.time() if s['pinned'] else 0
    save_series(sid, s)
    return jsonify({'pinned': s['pinned']})

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


# Per-series import status; UI polls /import-status. Lives in-memory only;
# survives across requests in the same gunicorn worker (we run with workers=1
# anyway). On restart the user just sees no in-flight job and can retry.
_IMPORT_STATUS = {}  # sid -> {running, total, done, errors[], started_at, finished_at, current}
_IMPORT_LOCKS = {}

def _import_status(sid):
    return _IMPORT_STATUS.setdefault(sid, {
        'running': False, 'total': 0, 'done': 0, 'errors': [],
        'started_at': None, 'finished_at': None, 'current': None,
    })


def _llm_extract_episode_entities(script_text, known_chars, known_locs, known_items):
    """One LLM call per episode that returns chars + locs + items in JSON.
    Faster than running /extract-characters + /detect-items separately. Passes
    known names so the model can flag re-uses vs new entities."""
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
        "- Empty arrays are valid. No fields beyond schema."
    )
    raw = claude_ask(
        f"Episode script:\n\n{script_text[:18000]}{known_section}",
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
                            ep.get('script', ''), known_chars, known_locs, known_items
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
    only, no LLM)."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    eps = _split_script_into_episodes(script)
    return jsonify({
        'episodes': [
            {'number': e['number'], 'title': e['title'], 'preview': e['body'][:240], 'length': len(e['body'])}
            for e in eps
        ],
        'total_chars': len(script),
    })


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
        char_files, loc_files = [], []

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
    series_data = {
        'id': sid, 'title': title,
        'genre': '', 'tone': '', 'target_audience': '', 'world_description': '',
        'synopsis': synopsis,
        'auto_generate_assets': True, 'batch_mode': False, 'batch_size': 1,
        'stage': 4, 'arc': None, 'milestone_synopses': {},
        'checkpoints': [], 'finale': None,
        'created_at': datetime.datetime.utcnow().isoformat(),
        'video_provider': 'seedance',
        'characters': [], 'locations': [], 'items': [],
        'style': {'type': 'cinematic', 'custom_description': '', 'ref_images': []},
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
    max_chars_raw = body.get('max_main_chars_per_scene')
    try:
        max_main_chars = int(max_chars_raw) if max_chars_raw not in (None, '', 0) else None
        if max_main_chars is not None: max_main_chars = max(1, min(6, max_main_chars))
    except (TypeError, ValueError):
        max_main_chars = None
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
    length_clause = (
        f"Каждая серия ≈ {eff_duration}с экрана ≈ {lines_range_word} реплик/действий. "
        if (duration_sec or lines_count) else
        f"Каждая серия = ~1 минута экрана ≈ {lines_range_word} реплик/действий. "
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
        f"1. РЕПЛИКИ: ровно {eff_lines} (±2). Реплика = одна строка диалога ИЛИ закадровый VO ИЛИ короткое действие (action line). "
        f"VOICEOVER считается как обычная реплика — он жрёт хронометраж так же. Если насчитал больше {eff_lines + 2} — режь беспощадно (включая VO). "
        "НЕ ВЫХОДИ за лимит «у меня важная сцена не помещается» — значит сцена слишком жирная, упрощай.\n"
        f"2. СЦЕНЫ: {scene_clause} Сцена = одна локация/время. Переезд = новая сцена. Каждая дополнительная сцена жрёт 3-4 реплики только на сетап.\n"
        "3. VOICEOVER: разрешён точечно (1-2 на серию максимум, как стилистический приём — открытие/закрытие). "
        "НЕ строй сюжет через закадр: откровения, эмоции, мотивацию персонажа показывай через диалог и действие, не через монолог в камеру. "
        "Если в серии 3+ VO-блока — это уже не сериал, а аудиокнига, переписывай.\n"
        "4. ЗАПРЕЩЕНО (нарушение = переписать с нуля):\n"
        "   • БУМАЖНЫЕ РАСКРЫТИЯ (paperwork reveals): нельзя двигать сюжет через письмо/завещание/документ/email/SMS/курьерский конверт/папку с бумагами/фото на телефоне/«экран ноутбука прокручивает документы». "
        "Откровения должны звучать ВСЛУХ из уст персонажа, не читаться с бумаги.\n"
        "   • ФЛЭШБЕКИ и сны в первой серии. Только настоящее время.\n"
        "   • «Тем временем в…» / «А в это время…» — параллельный монтаж сложен и жрёт хронометраж.\n"
        "5. CLIFFHANGER в конце — да, но НЕ через прибывшее письмо/звонок/тайный документ. "
        "Лучше: фраза которая меняет всё, неожиданное появление человека, прямая угроза в лицо, действие которое нельзя отменить.\n"
        "6. САМОПРОВЕРКА перед выводом каждой серии — посчитай:\n"
        f"   – Сколько реплик/строк действия/VO суммарно? (должно быть {eff_lines} ±2)\n"
        f"   – Сколько разных локаций/сцен? (должно быть ≤ {max_scenes})\n"
        "   – Сколько VO-блоков? (≤ 2, и сюжет НЕ должен ими двигаться)\n"
        "   – Двигается ли сюжет через бумагу? (должно быть НЕТ)\n"
        "   Если хоть один тест провален — перепиши серию до вывода.\n"
        "===\n"
    )
    system = (
        f"Ты — сценарист короткой драмы для вертикального TikTok/Reels. Пишешь {mode_label} на N серий. "
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
        + f"Episode {first_new_num}: <короткое название серии>\n"
        + f"Кратко: <1-2 предложения о чём серия>\n"
        + f"<реплики и действия персонажей — диалог, action lines>\n"
        + "\n"
        + f"Episode {first_new_num + 1}: <название>\n"
        + f"Кратко: <синопсис>\n"
        + f"<содержимое>\n"
        + "\n"
        + f"... и так далее до Episode {last_new_num}.\n\n"
        + "Каждая серия начинается с СТРОГО строки 'Episode N: <title>' — без других маркеров. "
        + "Никакой markdown, никаких '===', никаких '#'. Только plain text. "
        + ("Язык по контексту: если синопсис/направление на русском — пишем по-русски; "
           "если на английском — по-английски.\n\n" if from_scratch else
           "Язык — тот же что в предыдущих сериях (русский/английский/смесь — сохраняй стиль).\n\n")
        + "ВАЖНО: возвращай ТОЛЬКО сценарий, без преамбулы 'Вот сценарий:' и без post-комментариев."
    )
    user_msg = (
        f"СЕРИАЛ: «{s.get('title') or 'untitled'}»\n"
        f"Жанр: {s.get('genre') or '?'} · Тон: {s.get('tone') or '?'} · "
        f"Аудитория: {s.get('target_audience') or '?'}\n"
        f"{('Мир: ' + (s.get('world_description') or '')[:300] + chr(10)) if s.get('world_description') else ''}"
        f"ROSTER ПЕРСОНАЖЕЙ: {chars_list or '(пусто — можешь придумать сам)' if from_scratch else (chars_list or '(пусто)')}\n"
        f"ROSTER ЛОКАЦИЙ:    {locs_list or '(пусто — придумай простые)' if from_scratch else (locs_list or '(пусто)')}\n"
        f"СЮЖЕТНЫЕ ПРЕДМЕТЫ: {items_list or '(пусто)'}\n\n"
        f"{earlier_block}"
        + (f"ПОСЛЕДНИЕ {len(verbatim_window)} СЕРИЙ (verbatim, для тонкой калибровки стиля и continuity):\n```\n{verbatim_block}\n```\n\n"
           if verbatim_block else '')
        + f"{direction_block}\n"
        + (f"НАПИШИ ПЕРВЫЕ {count} СЕРИЙ (Эп.{first_new_num}–{last_new_num}). "
            if from_scratch else
           f"НАПИШИ СЛЕДУЮЩИЕ {count} СЕРИЙ (Эп.{first_new_num}–{last_new_num}). ")
        + f"Каждая ≈ {eff_duration}с экрана / {lines_range_word} реплик-действий, обязательно cliffhanger в конце."
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
        return jsonify({
            'script': text,
            'first_episode': first_new_num,
            'last_episode':  last_new_num,
            'count': count,
            'from_scratch': from_scratch,
        })
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
    series_data = {
        'id': sid,
        'title': data['title'],
        'genre': data.get('genre', ''),
        'tone': data.get('tone', ''),
        'target_audience': data.get('target_audience', ''),
        'world_description': data.get('world_description', ''),
        'synopsis': data.get('synopsis', ''),
        'auto_generate_assets': bool(data.get('auto_generate_assets', True)),
        'batch_mode':           bool(data.get('batch_mode', False)),
        'batch_size':           int(data.get('batch_size', 5)) if data.get('batch_mode') else 1,
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
    save_series(sid, series_data)
    scaffold_info = scaffold_series_folders(sid, data['title'])
    series_data['_scaffold'] = scaffold_info
    trigger_autogen_if_enabled(sid)
    return jsonify(series_data), 201

@app.route('/api/series/<sid>', methods=['GET'])
def get_series(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
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

def _clothing_clause(appearance: str, description: str = '') -> str:
    """Return 'Fully clothed in everyday casual attire. ' if neither
    appearance nor description mentions any clothing. Prevents models
    from defaulting to lingerie/swimwear on female full-body portraits."""
    combined = (appearance + ' ' + description).lower()
    if any(w in combined for w in _CLOTHING_WORDS):
        return ''
    return 'Fully clothed in everyday casual attire. '



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
            f'Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows on background. '
            f'Studio lighting, soft and even. Photorealistic, cinematic quality.'
        )
    else:
        # No reference — generate from scratch with description
        prompt = (
            f'Full body portrait of {char["name"]}, a {gender}. '
            f'{char.get("appearance", "")}. '
            f'Wearing: {outfit["label"]}. {outfit.get("description", "")}. '
            f'Standing facing camera, slight 3/4 angle. Neutral relaxed pose. '
            'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
            f'Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows on background. '
            f'Studio lighting, soft and even. Photorealistic, cinematic quality.'
        )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    char_slug = slugify(char['name'])
    out_dir = assets_dir(sid) / 'characters' / char_slug / 'outfits'
    out_path = out_dir / f'{asset_name(char["name"], outfit["label"])}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, reference_url=reference_url, preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        # Remove old photo if exists
        if outfit.get('photo'):
            old = series_path(sid) / outfit['photo']
            if old.exists():
                old.unlink()
        outfit['photo'] = rel_path
        outfit['avai_url'] = image_url  # store for future i2i variants
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
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


# ── Generate character image via Reteller/Banana ─────────────────────────────

@app.route('/api/series/<sid>/characters/<char_id>/generate-image', methods=['POST'])
def generate_character_image(sid, char_id):
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404

    gender = 'woman' if char.get('gender') == 'female' else 'man'
    style_clause = _series_style_clause(s)
    _appearance = char.get('appearance', '')
    _desc = char.get('description', '')
    prompt = (
        f"Full body portrait of {char['name']}, a {gender}. "
        f"{_appearance}. {_desc}. "
        f"{_clothing_clause(_appearance, _desc)}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows or reflections on background. "
        f"Studio lighting, soft and even, no harsh shadows on face or body. "
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

    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    regen_outfits = bool(body.get('regenerate_outfits', True))
    # When true: rewrite appearance via Claude before generating (breaks
    # out of the «same prompt → same result» loop when appearance is stale).
    rewrite_appearance = bool(body.get('rewrite_appearance', False))

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
                "(d) 1-3 short sentences, comma-separated descriptors, NO 'she is' opener.\n"
                "Output: the appearance text only, no quotes, no preamble.",
                system="You write character appearance descriptions for image generation. Plain text only.",
            ).strip().strip('"\'`')
            if rewritten and len(rewritten) > 10:
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
                appearance_for_prompt = cleaned
                char['appearance'] = cleaned
                _log_event('INFO', 'appearance_cleaned',
                           char_id=char_id, name=char.get('name', ''),
                           before=appearance_raw[:200], after=cleaned[:200],
                           wishes=wishes[:200])
        except Exception as e:
            _log_event('WARN', 'appearance_cleanup_failed',
                       char_id=char_id, err=str(e)[:200])

    gender = 'woman' if char.get('gender') == 'female' else 'man'
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    style_clause = _series_style_clause(s)
    _desc = char.get('description', '')
    prompt = (
        f"Full body portrait of {char['name']}, a {gender}. "
        f"{appearance_for_prompt}. {_desc}.{constraints_clause} "
        f"{_clothing_clause(appearance_for_prompt, _desc)}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows or reflections on background. "
        f"Studio lighting, soft and even, no harsh shadows on face or body. "
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
        image_url = avai_generate(prompt, out_path, preferred_provider=_series_image_provider(s))
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
                    f'Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows on background. '
                    f'Studio lighting, soft and even. Photorealistic, cinematic quality.'
                )
                ref_prompt = re.sub(r'\s+', ' ', ref_prompt).strip()
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
                outfit_url = avai_generate(ref_prompt, outfit_out, reference_url=new_base_url, preferred_provider=_series_image_provider(s))
                outfit['photo'] = str(outfit_out.relative_to(series_path(sid)))
                outfit['avai_url'] = outfit_url
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
    prompt = (
        f"{loc['name']}. {loc.get('description', '')}.{constraints_clause} "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
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
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
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
    prompt = (
        f"{loc['name']}. {loc.get('description', '')}.{constraints_clause} "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
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
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
    locs_block = '\n'.join(
        f'- id={l["id"]} | "{l["name"]}" — {(l.get("description") or "")[:200]}'
        for l in locs
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
        "5. type='building' для зданий нуждающихся в фасадной генерации, type='exterior' для уже-наружных.\n\n"
        f"ЛОКАЦИИ СЕРИАЛА:\n{locs_block}\n\n"
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
    try:
        raw = claude_ask("Сгруппируй и верни JSON.", system=sys, max_tokens=4000)
        data = json.loads(strip_json(raw))
        groups = data.get('groups') or []
        known_ids = {l['id'] for l in locs}
        name_by_id = {l['id']: l['name'] for l in locs}
        for g in groups:
            g['member_loc_ids'] = [i for i in (g.get('member_loc_ids') or []) if i in known_ids]
            g['member_names'] = [name_by_id[i] for i in g['member_loc_ids']]
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
            img_prompt = (
                f"Exterior facade of {name}. {desc}. No people in frame. "
                f"{(tone + ' atmosphere. ') if tone else ''}"
                f"Cinematic wide establishing shot of the building exterior. "
                f"Vertical 9:16 framing for short-drama. {style_clause}"
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
                    "swaying foliage, very slow camera push-in. Cinematic, 9:16."
                )
                vid_prompt = re.sub(r'\s+', ' ', vid_prompt).strip()
                avai_key = _get_user_avai_key()
                job = _avai_seedance_start(
                    prompt=vid_prompt,
                    ref_urls=[img_url] if img_url else [],
                    duration=5,    # Seedance min 5s; covers the 4s establishing
                    resolution='720p',
                    moderation_bypass='off',
                    aspect_ratio='9:16',
                    generate_audio=False,
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


@app.route('/api/series/<sid>/facades', methods=['GET'])
def facades_list(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'facades': s.get('location_facades') or [],
        'status': _facade_status(sid),
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
    prompt = (
        f"{item['name']}. {item.get('description', '')}.{constraints_clause} "
        f"Product-style still-life photo of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless background (#dadada), soft even studio lighting, "
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
    prompt = (
        f"{item['name']}. {item.get('description', '')}.{constraints_clause} "
        f"Product-style still-life photo of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless background (#dadada), soft even studio lighting, "
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
    description = (char.get('description') or '').strip()
    # Detect animal/anthropomorphic chars by appearance keywords — don't add
    # "a man/woman" prefix when char has fur/muzzle/tail/etc, otherwise the
    # model defaults to a HUMAN even though appearance says "grey wolf".
    appearance_low = appearance.lower()
    animal_words = ('fur', 'muzzle', 'snout', 'tail', 'paws', 'claws', 'whiskers',
                    'mane', 'feathers', 'beak', 'horns', 'antlers', 'hooves', 'scales',
                    'cub', 'pup', 'kitten', 'fang', 'fangs',
                    'шерсть', 'мордa', 'морду', 'морды', 'хвост', 'лапы', 'когти',
                    'клыки', 'грива', 'перья', 'клюв', 'рога', 'копыта')
    is_animal = any(w in appearance_low for w in animal_words)
    if is_animal:
        kind_label = ''   # appearance describes the species — no "a man" prefix
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
    # Style: project's visual_style overrides default photorealistic. Pixar/anime/etc
    # require explicit style directive AND removal of "Photorealistic" suffix —
    # otherwise model gets conflicting signals and renders human-looking realism.
    style_clause = _series_style_clause(s)
    visual_style = _series_visual_style(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality, high detail on face and clothing.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    clothing_fallback = '' if is_animal else _clothing_clause(appearance, description)
    prompt = (
        f"{style_prefix}"
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{appearance}. {description}.{constraints_clause} "
        f"{clothing_fallback}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows or reflections on background. "
        f"Studio lighting, soft and even, no harsh shadows on face or body."
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / f'{asset_name(char["name"], "BASE")}.jpg'
    image_url = avai_generate(prompt, out_path, preferred_provider=_series_image_provider(s))
    rel_path = str(out_path.relative_to(series_path(sid)))
    char.setdefault('ref_images', []).insert(0, rel_path)
    char['avai_base_url'] = image_url

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
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    prompt = (
        f"{style_prefix}"
        + (same_clause if reference_url else intro_clause)
        + f'Now wearing: {outfit["label"]}. {outfit.get("description", "")}. '
        + ('Same face, same body — only the clothing changes. ' if reference_url else '')
        + constraints_clause
        + 'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
          'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
          'Uniform solid gray background, #808080, no gradients, no props, no furniture. No shadows on background. '
          'Studio lighting, soft and even.'
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
    prompt = (
        f"{style_prefix}"
        f"{loc['name']}. {loc.get('description', '')}. "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
        f"Atmospheric lighting."
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
    prompt = (
        f"{style_prefix}"
        f"{item['name']}. {item.get('description', '')}.{constraints_clause} "
        f"Product-style still-life of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless background (#dadada), soft even studio lighting, "
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
        # ─── Pre-sweep: ensure every episode's cast block is reflected in series.json
        # Skip episodes where extraction hasn't been confirmed by the user yet.
        for ep in list_episodes(sid):
            if not (ep.get('script') or '').strip():
                continue
            if ep.get('cast_extracted', True) is False:
                continue
            if True:
                try:
                    sync_episode_with_cast_block(sid, ep['number'])
                except Exception as e:
                    print(f'[autogen {sid}] sync ep{ep["number"]} failed: {e}')

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
                    _gen_char_base_inline(s_local, sid, char)  # SLOW: avai API call
                    new_refs = char.get('ref_images') or []
                    new_url  = char.get('avai_base_url') or ''
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        c_disk = next((c for c in s_disk.get('characters', []) if c['id'] == parent_id), None)
                        if c_disk and not c_disk.get('ref_images'):
                            c_disk['ref_images'] = new_refs
                            c_disk['avai_base_url'] = new_url
                            save_series(sid, s_disk)
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
    _spawn_with_keys(auto_generate_missing_assets, sid)
    return jsonify({'started': True, 'status': _autogen_status(sid)})


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
            f"Soft even studio lighting, no harsh shadows. Sharp focus. "
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
            f"Soft even studio lighting, no harsh shadows. Sharp focus. "
            f"Photorealistic, professional character reference. "
            f"Identical face and body to the base reference — change ONLY the clothing/pose described above. "
            f"No background objects or gradients. Clean, simple, reference-quality."
        )

    elif asset_type == 'location':
        loc = next((l for l in s.get('locations', []) if l['id'] == asset_id), None)
        if not loc:
            return jsonify({'error': 'location not found'}), 404
        description = loc.get('description', '')
        context = ' '.join(filter(None, [genre, tone, world]))
        prompt = (
            f"{loc['name']}. {description}. "
            f"Empty scene, no people present. "
            f"{tone + ' atmosphere. ' if tone else ''}"
            f"{style.capitalize()} visual style. "
            f"Cinematic wide establishing shot. "
            f"Photorealistic, high detail, professional cinematography. "
            f"{('World context: ' + world[:100] + '. ') if world else ''}"
            f"Atmospheric lighting, sharp focus."
        )
    else:
        return jsonify({'error': 'unknown type'}), 400

    # Clean up double spaces
    import re
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    return jsonify({'prompt': prompt})


# ── AI: Series idea generation ───────────────────────────────────────────────

_IDEAS_SYSTEM = """You are a creative producer for TikTok/Reels short drama series — the addictive, over-the-top format from apps like ReelShort and DramaBox.

LANGUAGE RULE — NON-NEGOTIABLE:
- All output (titles, names, descriptions, synopses) must be in ENGLISH only
- Character names must be English or Western European (Claire, Marcus, Elena, James, Sofia, Ethan)
- NEVER use Russian, Chinese, Korean, Japanese or any non-Western names
- Location names must ALSO be in English only (e.g. "Hotel Room", "Penthouse", "Boardroom", "Military Base") — never Russian/Cyrillic location names

TITLE RULES — CRITICAL. Titles in this format are LITERAL PREMISES, not artistic names.
The audience must instantly picture the entire premise from the title alone.

Title templates that work — VARIETY IS REQUIRED. Among any 5 generated ideas, use AT LEAST 4 DIFFERENT templates from this list. ABSTRACT SLOT PATTERNS ONLY — do NOT lift example phrasings, INVENT new ones from the slot definitions:

  Power-imbalance templates (use at most 2 out of 5 ideas):
  • "Never [Verb] a [Hidden Identity]"
  • "My [Dismissible Role] Is Actually a [Shocking Reality]"
  • "Pregnant by [Powerful/Forbidden Person]"
  • "[Contract/Flash/Fake] [Marriage/Dating] with [Twist]"
  • "Sold to [Powerful Other] by [Family/Boss/Captor]"
  • "Married to [Position] for [Reason That Becomes Wrong]"

  Time-anchor templates (great for second-chance, comeback, fall-from-grace):
  • "After [Time], [Shocking Comeback Action]"
  • "[Number] Years [Hidden Status / Mistaken Identity]"
  • "[Number] Days to [Stake / Deadline]"
  • "When [Trigger], He/She [Realized Truth]"
  • "The [Day/Night] [Specific Event] Happened"
  • "[Time Unit] After [Inciting Incident], [Reveal]"

  First-person-extreme templates (great for revenge, found-family, survival):
  • "I [Did Extreme Thing] Just to [Goal]"
  • "I [Verb]-ed My Way Into [Forbidden Place/Role]"
  • "[Verb]-ing My [Forbidden/Unlikely Person]"
  • "I'm the [Role] My [Powerful Person] [Verb-ed] and Forgot"
  • "I Married My [Enemy/Boss/Target] for [Reason]"
  • "They Said I Was [Label]. They Were Wrong About [Twist]"

  Statement-as-hook templates (great for cold revenge, quiet menace):
  • "[Quiet Statement Reframing Power]"
  • "The [Person] Who [Extreme Action]"
  • "[Possessive Sequence]" — three short noun-phrases punctuated, each escalating
  • "[Question Demanding Answer]"
  • "[Pronoun] [Did Something]. [Pronoun] Didn't Know [Twist]."

  Situational-shock templates (great for mystery, found-family, courtroom):
  • "[Shocking Premise in One Line]"
  • "[Character] [Does Extreme Thing] at [Setting]"
  • "[Profession/Role] for [Powerful Other]'s [Secret Need]"
  • "[Concrete Object/Action] Was Never Supposed to [Outcome]"

EXTENDED TITLE PATTERN LIBRARY — pick FRESH ones, rotate aggressively. These
are slot patterns only; invent your own brackets. Mix freely:

  Identity-flip templates:
  • "[Profession] by Day, [Hidden Role] by Night"
  • "They Hired Me as [Role]. I'm Their [Twist]."
  • "Everyone Thinks I'm [Public Label]. I'm Actually [Truth]."
  • "[Name], Daughter/Son of [Position] — and [Hidden Sin]"
  • "The [Role] [Person of Power] Forgot to Kill"

  Reversal templates:
  • "[Victim/Loser] Now [Position of Power]"
  • "[Powerful Person] Begs the [Lowly Role] He Ruined"
  • "She [Quiet Verb-ed]. Now He [Cannot Look Away]"
  • "[Person] Wants Me Back. Too Bad I [Action That Closed the Door]"
  • "[Role] Walks Into [Place]. Owns It by Friday."

  Object-as-trigger templates:
  • "The [Specific Object] in [Setting]"
  • "[Object] Was Never Supposed to End Up With [Person]"
  • "[Drink/Letter/Ring/Photo] at the [Specific Event]"
  • "What She Found in His [Container/Place]"

  Setting-locked templates:
  • "[Setting] Doesn't Forgive"
  • "Welcome to [Specific Place]. You Won't Leave."
  • "Last Night at the [Setting]"
  • "Code [Number] at [Workplace]"
  • "Cell [Number] Knew the Truth"

  Threat-and-promise templates:
  • "[Number] Lies. [Number] Bodies. One [Survivor/Witness]."
  • "[Person]: [Curt Threat / Promise Three Words]"
  • "Marry [Position] or [Stake]"
  • "Run. Or [Worse Alternative]"
  • "[Pronoun] Came Back. [Pronoun] Came Back Wrong."

  Question-and-confession templates:
  • "Why Is My [Role] [Doing Suspicious Action]?"
  • "Who Sent the [Object] to [Place]?"
  • "I Don't Know [Quotient]. But I Know [Other Fact]."
  • "He Called Me [Label]. He'll Regret That."

  Single-word and ultra-short templates:
  • "[Single Strong Noun]" (e.g. one-word title — a name, an object, a role)
  • "[Name]: [Two-Word Reframe]"
  • "[Two Nouns Separated by Slash]" (e.g. "Mother/Stranger")

OPENING WORD DIVERSITY (within one batch of 5 ideas):
  • Max ONE title starting with "I" / "My" / "The" / "After" / "When" each.
  • Force at least 2 titles to start with something else entirely: a name, a
    profession, a number, a question word, an object, an exclamation, a verb,
    or a setting noun. Avoid clustering on any single opening word.

NOVELTY GUARDRAIL — before output, scan all 5 titles:
  • If two share the same opening 1-2 words → rewrite one.
  • If two use the same template family (both time-anchors, both
    identity-flips, both "I [verb-ed]" patterns) → swap one to a fresh family.
  • If a title feels like something the user might have seen ten times before
    (mafia king, billionaire stepbrother, contract marriage to a boss) —
    rewrite using a different archetype + different template structure.

Power nouns (mix freely, do NOT pile multiple on one title): Billionaire, CEO, Mafia Boss, Alpha, King, Heiress, Surgeon, Detective, Heiress, Pilot, Soldier, Bodyguard, Coach, Tutor, Pastor, Therapist, Judge, Driver, Nanny, Chef, Architect, Influencer, Twin
Relationship modifiers: my husband, my ex, my boss, my stepbrother, my brother's best friend, my enemy, my therapist, my doctor, my driver, my landlord, my tutor, my coach, my mentor, my mother, my sister, my fiance, my late husband (alive), my fake husband, my contract wife

DIVERSITY ENFORCEMENT — MANDATORY when generating multiple ideas:
- Among any 5 ideas, use AT LEAST 4 different title templates (don't reuse the same template more than twice).
- AT LEAST 2 of the 5 ideas must NOT be set in elite/luxury environments (corporate/penthouse/mafia/royal). Mix in working-class, institutional, blue-collar, immigrant, mid-tier, suburban, road-trip settings.
- AT LEAST 2 of the 5 should NOT be primarily romance — center other engines: revenge tour, single-case mystery, found-family forming, survival pact, custody fight, comeback redemption.
- AT LEAST 1 of the 5 should NOT use a billionaire/CEO/mafia archetype. Force a different antagonist: corrupt judge, gaslighting mother, charming therapist with recordings, megachurch pastor, ex who faked their death, etc.
- Do NOT make 3+ ideas about pregnancy/secret child/forced marriage in the same batch. Pick at most 2 in that family.
- If two ideas share the same setting+twist combo or feel like reflavoured versions — rewrite ONE with a different premise structure.

FORBIDDEN title styles: metaphorical ("Shadows of Yesterday"), vague ("The Choices We Make"), literary ("When Light Finds Darkness"), anything that sounds like an indie film or a book club pick. But "soft-literal" titles ARE encouraged ("After 10 Years, He Was Still Waiting" reads literal enough — keep these alongside the louder templates for variety).

SYNOPSIS RULES:
- 3 sentences MAX. Every word is plot, zero atmosphere-setting.
- Sentence 1: The injustice/humiliation done to the protagonist OR the shocking inciting situation
- Sentence 2: The forced entanglement / power imbalance / secret collision
- Sentence 3: The twist that reframes everything or the impossible choice she faces
- Tone: punchy, present-tense energy, zero hedging. Sound like a trailer voiceover.
- synopsis_ru: same energy in Russian — 2-3 предложения, как будто рассказываешь подруге что только что посмотрела

COMPLEXITY HARD CAPS — ZERO TOLERANCE:
This is the most violated rule. The synopsis must be UNDERSTANDABLE on FIRST READ.
Audience must instantly grasp who/what/why. If reader needs to re-read to follow — IT'S TOO COMPLEX.

  HARD LIMITS:
  • MAX 3 named characters in the whole synopsis (protagonist + 1-2 others). Not 5, not 7.
  • EXACTLY 1 central conflict / story engine. NOT three nested ones.
  • MAX 1 «twist» = the cliffhanger at the end of sentence 3. NOT a chain of reveals.
  • NO «and then... and also... and turns out... and meanwhile...» stacking.
  • NO professions piled on one character («ex-cartel-accountant in witness protection
    who is also a rival surgeon and a sober mentor»). Pick ONE identity per character.
  • NO multiple shocking backstories converging in one synopsis (dead sister + custody
    fight + addiction recovery + hidden mentor + cartel money + medical sabotage = NO).
  • If you find yourself writing «—» (em-dash) more than twice in a single synopsis, you're
    stacking too much. Strip back to one clean clause per sentence.

  TEST: read your synopsis to an 8-year-old. Could they tell you back what the show is
  about in one sentence? If no — rewrite simpler.

  ✗ BAD (real example — 6+ reveals nested):
  «Natalie is three months sober and scrubbing dishes at a tech retreat when a guest
  collapses from an overdose — and she is the only person on the island who knows how
  to administer naloxone. The man turns out to be her late sister's boyfriend, who is
  filing for permanent custody of Natalie's niece. The island's owner — a man she's been
  having quiet conversations with — reveals he is a former cartel accountant in witness
  protection who has been sabotaging the boyfriend's medical career...»
  ↑ This is a soap opera season finale, not a hook. 5 characters, 6 backstories, 4 conflicts.

  ✓ GOOD (same domain, ONE engine, clear):
  «Three years sober and working as a maid at a luxury rehab clinic, Maya recognises
  the new patient — the surgeon whose botched operation killed her sister. He doesn't
  remember her face. She has 30 days to decide: testify and destroy him, or save him
  and inherit his guilt.»
  ↑ 2 characters, 1 conflict (revenge vs forgiveness), 1 twist at the end. Clear in
  one read.

  ✓ GOOD (different example, family/custody engine):
  «Single mother Anna takes a job as nanny to her ex-husband's new wife — the woman
  he left her for. Neither knows Anna is the boy's biological aunt. When the wife asks
  Anna to «handle» an inconvenient relative, Anna realises she's being set up to
  disappear — and the only person who'd notice is the husband who threw her out.»
  ↑ 3 characters, 1 engine (revenge + sister-substitution), 1 twist (the setup).

DIALOGUE-FIRST RULE — HARD BAN ON PAPERWORK & SCREENS:
Reveals come from spoken confrontations between people on screen, never from documents or screens.
BANNED as plot devices in synopses: letters, notes, documents, contracts, files, dossiers, "folder of photos", text messages, SMS, chat bubbles, emails, phone screens, laptop screens, computer UIs, surveillance camera footage being watched, audio recordings being played, diary entries, voiceover, news headlines, radio reports, silent flashback montages.
Never write phrases like: "finds a folder of photos", "discovers documents proving", "receives a text", "sees on the screen", "watches the security footage". Rewrite as: character A confronts character B and accuses/admits/threatens out loud.
NARROW exception: a physical object (ring, pregnancy test, key, single photo) may appear ONCE in a synopsis only if a character immediately verbalizes its meaning aloud to another character in the same beat.
Same rule applies to synopsis_ru: запрещены «папка с фото», «SMS», «на экране телефона», «запись с камеры», «находит письмо/файл/документ» — переписывай через прямую устную конфронтацию.

LOGIC CHECK — MANDATORY before finalizing any synopsis:
- TIMELINE CONSISTENCY: if years passed since an encounter, a pregnancy cannot be from that encounter. A 3-year gap → she has a child aged ~3, NOT a current pregnancy. Fix: use "secret child" / "toddler son" / "3-year-old daughter" — not "pregnant".
- PREGNANCY TIMING: pregnancy is ~9 months. If she's currently pregnant, the conception was recent (weeks to months ago), not years ago.
- SECRET CHILD trope (common and valid): she got pregnant from a one-night stand YEARS ago, raised the child alone, never told him. She is NOT currently pregnant — she has a child.
- CURRENT PREGNANCY trope (also valid): they slept together RECENTLY (days/weeks ago), she just discovered she's pregnant, and now must deal with him.
- Never mix these two — pick one and make the timeline explicit in the synopsis.
- Read back every synopsis before output. If the math doesn't work, rewrite it.

PREGNANCY SYNOPSIS — EXPLICIT CONCEPTION RULE:
Every synopsis with a current pregnancy MUST name a SPECIFIC RECENT EVENT (within the last weeks, max ~3 months) where conception happened. The reader must see the cause and effect spelled out.

  ✗ BAD (ambiguous, conception event missing or implied only):
  "Three years ago he ruined her family. After an anonymous charity gala she discovers she is pregnant by him."
  ↑ When did they sleep together? It's only implied. Reader does the math, gets confused, thinks the pregnancy is from the 3-year-old event.

  ✓ GOOD (conception event explicit and recent):
  "Three years after he ruined her family, she ends up in his bed for one drunken night at a charity gala — and discovers six weeks later that she is pregnant by the man she hates most."
  ↑ One night → six weeks later → pregnant. Math is unambiguous.

  ✓ GOOD (secret child variant — NO current pregnancy):
  "Three years ago she had a one-night stand with the rival CEO who later destroyed her family. Now she scrubs floors in his hotel — with their two-year-old son hidden in daycare and his name on no birth certificate."

FORBIDDEN narrative patterns in synopses:
  ✗ "N years ago [event X] ... she discovers she is pregnant by him" — without an explicit recent night together. The reader must NEVER have to guess when conception happened.
  ✗ "Years later, she finds out she's pregnant" — pregnancy doesn't time-travel.
  ✗ Mixing "ruined her life years ago" with "currently pregnant by him" without naming the recent encounter that produced the pregnancy.

If the title or premise demands a current pregnancy, the synopsis MUST contain a phrase that anchors conception to a recent moment: "one night at the gala", "after a single weekend together", "six weeks after their forced encounter", "after their one drunken hotel night", etc.

If you cannot fit such a phrase in 3 sentences without it feeling forced — switch to the secret-child variant instead.

Respond ONLY with valid JSON — no markdown, no commentary."""

_IDEAS_SCHEMA = """{
  "ideas": [
    {
      "title": "Literal premise title — audience must picture the whole show from the title alone",
      "genre": "Genre blend (e.g. Pregnancy Drama / Revenge / CEO Romance)",
      "tone": "Emotional tone (e.g. Addictive, Over-the-top, Dark & Satisfying)",
      "target_audience": "Target audience",
      "world_description": "2-3 sentences: setting, who has power, who doesn't, what's at stake",
      "synopsis": "3 sentences: injustice → collision → impossible situation or twist",
      "synopsis_ru": "То же самое на русском — 2-3 предложения, энергия как в трейлере"
    }
  ]
}"""

_IDEA_SETTINGS = [
    # Elite / luxury (kept — but we WILL force diversity to non-elite below)
    'corporate boardroom', 'luxury hotel', 'high-end fashion house', 'old money estate',
    'art gallery', 'professional sports team', 'law firm', 'private island resort',
    'royal court', 'film set', 'crime family', 'political campaign', 'family vineyard',
    'tech startup', 'foreign city expat community', 'fashion week backstage',
    'megachurch leadership', 'private boarding school', 'modeling agency',
    # Working-class / blue-collar / mundane
    '24-hour highway diner', 'truck stop motel', 'family-run nail salon', 'food delivery startup',
    'immigrant restaurant family', 'food truck rivalry circuit', 'working-class neighborhood block',
    'gated suburban subdivision', 'small coastal fishing town', 'mining company town',
    'family farm dairy', 'cattle ranch', 'inherited bookshop', 'antique auction house',
    'mid-tier law firm in trouble', 'failing chinese restaurant kitchen brigade',
    # Institutional
    'hospital ER', 'maternity ward', 'IVF clinic', 'private rehab center',
    'AA meeting / 12-step group', 'public defender office', 'prison women\'s wing',
    'jury deliberation room', 'private detective agency', 'crime scene investigation unit',
    'couples therapy clinic', 'divorce lawyer office', 'adoption agency',
    'forensic accounting firm', 'witness protection safehouse',
    # Insulated / closed-world (great for trapped-together)
    'cruise ship', 'airline crew on overseas route', 'mountain ski lodge', 'desert oil town',
    'offshore oil rig', 'archaeology dig site', 'remote research lab', 'lighthouse compound',
    'monastery / convent', 'charismatic cult retreat', 'tour bus on the road',
    'music festival camp', 'e-sports team house', 'reality TV competition set',
    # Creative / media / niche
    'underground music scene', 'art conservatory', 'ballet academy', 'circus / carnival circuit',
    'influencer content house', 'talk show set', 'podcast production studio',
    'veterinary practice', 'dog rescue shelter', 'gospel choir',
    # Power-adjacent (alternative to corporate/mafia)
    'military intelligence unit', 'private security firm', 'biotech research lab',
    'fertility research lab', 'family court chambers', 'PR crisis firm',
    'tabloid newsroom', 'investigative journalism desk',
    # Expansion pack — added to break the «one of 60 same settings» repetition
    'state fair / county rodeo circuit', 'wedding planning empire', 'haute pâtisserie kitchen brigade',
    'historical reenactment troupe', 'animal sanctuary in financial crisis',
    'oncology ward inpatient floor', 'hospice palliative care unit', 'organ transplant coordination office',
    'speedboat racing circuit on the Med', 'NASCAR pit crew on the road',
    'street-style fight club hidden under a gym', 'underground poker ring at a country club',
    'travelling renaissance fair', 'casino floor and high-roller suite', 'ski-resort patrol & search-rescue team',
    'arctic research outpost during polar night', 'maritime salvage operation',
    'high-stakes auction house with provenance dispute', 'rare-coin trading desk under FBI watch',
    'private island retreat for tech execs', 'wellness commune off-grid in the desert',
    'ayahuasca retreat center', 'orchestral pit / opera house backstage',
    'high-fashion couture atelier in Paris', 'esports betting syndicate front office',
    'crypto exchange compliance war room', 'pre-IPO board fight at unicorn startup',
    'family-run funeral home', 'taxidermy studio with celebrity clientele',
    'antique gun appraisal show', 'beauty pageant prep camp',
    'high-school reunion organising committee', 'class action plaintiffs\' kitchen-table coalition',
    'small-town mayor\'s office during scandal', 'gerontological psych ward',
    'restaurant week judge panel + chefs', 'high-end real estate brokerage in Manhattan',
    'organic farm CSA with paying members in the city', 'rare-book restoration workshop',
    'amusement park behind-the-scenes operations',
]
_IDEA_TWISTS = [
    # Identity / deception cluster (de-duped from old 5+ → kept distinct)
    'hidden true identity (rich/poor/profession)', 'mistaken-for-someone reveal',
    'hidden heir / inheritance shock', 'fake death revealed', 'evil twin / doppelganger swap',
    'amnesia + new identity rebuild', 'witness protection meet-cute',
    # Power & betrayal
    'forbidden love', 'revenge plot', 'class war', 'family betrayal', 'corporate sabotage',
    'blackmail', 'the ally is the real villain', 'the rescuer has an agenda',
    'fall from grace and rebuild', 'survived assassination attempt',
    'estranged family member returns', 'second chance romance', 'love triangle',
    'obsessive ex', 'dangerous obsession', 'long-lost sibling',
    # Romance/family entanglements
    'secret pregnancy with explicit recent conception', 'paternity reveal / DNA twist',
    'wrong-baby swap at birth', 'fake relationship turns real', 'arranged marriage to a stranger',
    'marriage of convenience for inheritance', 'wedding interrupted', 'left at the altar',
    'inheritance condition (must marry by deadline)', 'forced cohabitation contract',
    'surrogacy gone wrong', 'fertility lie', 'fake adoption uncovered',
    # Returning ghosts
    'the dead spouse is alive and watching', 'second life / reincarnation knowledge',
    'amnesiac executive returns home', 'estranged child returns to family business',
    'fugitive begs sanctuary at protagonist\'s door',
    # Crime / mystery
    'mafia protection deal', 'undercover op falls for the target', 'cult escape',
    'inherited debt forces marriage', 'kidnapping with a twist (victim was bait)',
    'organ donor reveal in OR', 'the surgeon is the assassin',
    # Modern reveals
    'paparazzi expose a private moment', 'social media downfall', 'catfish reveal in person',
    'hidden recording surfaces in confrontation',
    # Power dynamics
    'company inheritance shock', 'rejected mate / pack outcast (supernatural)',
    'reverse-Cinderella (rich woman / lower-status man)',
    # Expansion pack — fresh engines to break twin/billionaire/pastor monoculture
    'someone is alive that everyone thought died ten years ago',
    'a forged signature trips a multi-million dollar audit',
    'a family heirloom turns out to be stolen wartime art',
    'a video deepfake is used in court as real evidence',
    'a recovering addict\'s sponsor is the dealer who started them',
    'two strangers discover they\'re both married to the same person',
    'a missing kidney donor turns out to be alive and demanding compensation',
    'the will reads only after a year of cohabitation by named heirs',
    'a tattoo artist recognises a kidnap victim\'s ink on a stranger',
    'the AI girlfriend is a real woman behind the chatbot',
    'wedding ring was switched — wrong person is married',
    'a journalist\'s source turns out to be their estranged parent',
    'a 911 call from years ago surfaces and changes the verdict',
    'a child\'s school project exposes hidden family identity',
    'the rival bidding for the company was paid by your spouse',
    'a service animal recognises a former abuser at a charity gala',
    'a wrong delivery brings evidence of a long-running affair',
    'an inherited diary names the wrong father',
    'the witness who saved you was paid by the person who hired the attack',
    'a viral TikTok cooks down an alibi to ashes',
    'someone walking with amnesia turns out to be a high-value asset',
    'a hospital mix-up gave a dying patient the wrong cure 5 years ago',
    'an emergency surrogate is the protagonist\'s old high school enemy',
    'a podcast guest accidentally confesses to a cold case live',
    'a soldier comes home to find their spouse remarried to their commander',
    'a buried time capsule contradicts everyone\'s memory of that night',
    'protagonist\'s «dead» parent is alive under witness protection',
    'a courtroom-translator is hiding native fluency to gather intel',
    'a charity\'s mission statement is a money-laundering script',
    'the personal trainer is an undercover detective',
    'a paternity test was forged in the lab decades ago',
    'a vintage photograph proves grandfather wasn\'t who family says',
    'a wildfire forces two families with shared dark history to evacuate together',
    'a memorial service is interrupted by the «dead» person walking in',
    'an online support group turns out to be run by the abuser',
]
_IDEA_TONES = [
    ('Dark thriller', 'Suspenseful'),
    ('Steamy romance', 'Passionate'),
    ('Revenge drama', 'Intense'),
    ('Comedy of errors', 'Light & witty'),
    ('Psychological suspense', 'Unsettling'),
    ('Enemies to lovers', 'Slow burn'),
    ('Found family', 'Warm & emotional'),
    ('Crime drama', 'Gritty'),
    ('Cinderella revenge', 'Satisfying & addictive'),
    ('Pregnancy drama', 'Emotional & volatile'),
    ('CEO power struggle', 'Cold & ruthless'),
    ('Mafia romance', 'Dangerous & intense'),
    # Added 13 — broaden the emotional palette
    ('Quietly devastating', 'Restrained heartbreak'),
    ('Bittersweet & hopeful', 'Soft melancholy with warmth'),
    ('Pulpy & operatic', 'Maximalist soap energy'),
    ('Gritty realism', 'Unflinching, blue-collar'),
    ('Whimsical melodrama', 'Almost fairytale, but with knives'),
    ('Cosy slow burn', 'Tender, low-stakes-feeling, big payoff'),
    ('Ferocious & vengeful', 'White-hot, no mercy'),
    ('Surreal & dreamlike', 'Off-kilter, uncanny edges'),
    ('Cold procedural', 'Detective-show clinical pacing'),
    ('Found-family wholesome', 'Hugs and grit, low cynicism'),
    ('Chaotic comedy', 'Everyone making bad decisions, fast'),
    ('Pastoral noir', 'Sleepy small town, dark currents'),
    ('Confessional first-person', 'Whispered intimacy, narrator-driven energy'),
    # Expansion pack
    ('Slow-burn psychological dread', 'Skin-prickling unease'),
    ('Vengeance cold-dish', 'Patient ruthlessness'),
    ('Glamour-meets-rot', 'Champagne and corruption'),
    ('Workplace ensemble dramedy', 'Sharp banter, real stakes'),
    ('Generational saga', 'Decades-spanning, family-as-prison'),
    ('Single-location pressure cooker', 'One room, escalating heat'),
    ('Investigation procedural with personal cost', 'Case-of-the-week + ongoing trauma'),
    ('Whodunit at a closed event', 'Knives Out energy, social skewering'),
    ('Identity rebuild', 'Stripped of everything, building from zero'),
]
_IDEA_PREMISE_STRUCTURES = [
    'Forced cohabitation / locked-in scenario (snowstorm / contract / shared apartment by mistake)',
    'Fake marriage / contract relationship that turns real',
    'Marriage of convenience to satisfy inheritance condition',
    'Amnesia after the inciting event — protagonist rebuilds from scraps',
    'Body swap / soul switch — wakes up in another life',
    'Fake death and observing the aftermath from the shadows',
    'Wrong-place-wrong-time swap (mistaken for someone else, must keep playing the role)',
    'Heist crew assembling for one impossible job',
    'Found family forms among unlikely strangers',
    'Rivals forced to work together (hostage/pact/case)',
    'Mentor-apprentice with secret agenda from one side',
    'Wedding sabotage from inside (planner, maid of honor, kid)',
    'Reverse-Cinderella — rich woman falls for lower-status man',
    'Pretending to be someone else (housekeeper, tutor, nanny) inside the target\'s house',
    'Single-parent meets ex-flame returning years later (kid is his)',
    'Mafia mole undercover, cover starts to crack',
    'Witness protection meet-cute',
    'Reality-TV competition where rivalry turns to romance / vengeance',
    'Twin impersonating their sibling for an impossible reason',
    'Estranged daughter returns to family business',
    'Reunion at funeral / wedding / class reunion forces the reveal',
    'Trapped in past — time loop / reincarnation / second-chance life with prior knowledge',
    'Charity gala collision (one night, irreversible consequences)',
    'New employee meets tyrant boss (turns out to be ex / target / kin)',
    'Coming home from war / prison / overseas to find life rebuilt without you',
    'Sister stealing fiance / brother stealing wife',
    'Born-again sibling rivalry over inheritance',
    'Methodical revenge tour planned over years',
    'Survival scenario (storm / desert / quarantine forces strangers together)',
    'Single-case mystery as the season spine (one murder, custody, missing person)',
    'Crisis of faith — pastor / cult escapee unravelling beliefs',
    'Long con — protagonist is being scammed but turns it back',
    'Custody battle as the engine — fight over child or inheritance',
    'Caregiver & patient — one is hiding why they really took the job',
    # Expansion pack
    'Class-action lawsuit — five strangers find common enemy mid-trial',
    'Witness-relocation gone wrong — wrong person took the spot',
    'Ghosting victim becomes obsessed with finding why',
    'Whistleblower inside a beloved institution',
    'Body-found hike — group must decide whether to report',
    'Family reunites for an estate auction, fights over a single item',
    'Trial period at a luxury job — perks come at hidden cost',
    'Recovery support sponsor turns out to be victim of sponsee',
    'Diary discovered after death rewrites family history',
    'Long-lost adult sibling tracks down their birth family',
    'Hidden second family discovered after a parent\'s death',
    'Job interview that\'s actually a multi-day psychological experiment',
    'Cold case reopened by a podcast, suspects start dying again',
    'Cross-cultural marriage where each side hides a major secret',
    'Caretaker for a wealthy elder who turns out to be lucid and dangerous',
    'Inherited business that hides illegal back-channel revenue',
    'Beloved teacher accused — kids organise their own investigation',
    'Online date who ghosted reappears as the boss/landlord/doctor',
    'Roommate ad on Craigslist — one of them is hunting the other',
    'Childhood imaginary friend turns out to be a real abducted sibling',
    'Returning soldier discovers spouse has children that aren\'t theirs',
    'Identity-theft victim systematically destroys the thief\'s life',
    'Live-stream gone catastrophically wrong, must hide what happened',
    'Three lives intersect on a single 911 dispatch over one shift',
]
_IDEA_PROTAG_ARCHETYPES = [
    'Ex-intelligence operative posing as nanny / housekeeper / tutor',
    'Twin impersonating their sibling',
    'AI-assisted surgeon hiding addiction / past',
    'Witness in protection program building a new identity',
    'Estranged daughter of mob boss returning home reluctantly',
    'Adopted heir who just learned they\'re adopted',
    'Assistant secretly spying on her boss for a rival firm',
    'Disgraced lawyer rebuilding career from a low-end firm',
    'Single mother working three jobs after husband\'s disappearance',
    'Veteran returning to civilian life with a haunted past',
    'Influencer caught in real-world scandal she didn\'t cause',
    'Heiress hiding from arranged marriage by working as someone\'s maid',
    'Working-class line cook with a secret gift (savant memory / forensic palate)',
    'Recovering addict in halfway house finding unexpected mentor',
    'Forensic accountant uncovering family-scale fraud',
    'Trauma surgeon with PTSD nightmares she can\'t admit',
    'Police detective whose new case hits unbearably close',
    'Investigative journalist on a story powerful people will kill to bury',
    'ER nurse working night shifts who recognises a "dead" patient',
    'Public defender with overloaded caseload taking one impossible client',
    'Foster sister searching for biological family among the wealthy',
    'Recently widowed person uncovering spouse\'s parallel life',
    'Retired hitwoman trying to disappear in a small town',
    'Fashion designer one week from launch, sabotaged from inside',
    'Tech founder one round from bankruptcy, betrayed by co-founder',
    'Star athlete with career-ending injury rebuilding identity',
    'Chef who lost their restaurant working in the rival\'s kitchen',
    'Music producer hiding pop-star past from new partner',
    'Pastor\'s wife realising the church is a cult',
    'Reality-TV contestant whose scripted villain edit is destroying her real life',
    # Expansion pack — non-«billionaire/twin/pastor» protagonists
    'Hospice nurse with a knack for spotting suspicious deaths',
    'Apartment-building super who quietly knows every tenant\'s secrets',
    'Mid-career conductor sabotaged by ambitious second violinist',
    'Genealogist hired to find a missing heir — finds herself',
    'Forensic linguist who recognises an anonymous letter\'s writer',
    'Truck driver who picks up the wrong hitchhiker',
    'Auctioneer whose memory for objects opens an old murder case',
    'Crisis-line counsellor who recognises a caller\'s voice',
    'Subway conductor who keeps seeing the same passenger every night',
    'Tarot reader whose «cold reads» turn unsettlingly accurate',
    'Veterinarian who notices abuse markers in injured pets',
    'School bus driver from a small town who saw too much',
    'Customs officer whose first big bust was set up to fail',
    'Wedding photographer with a knack for catching guilty glances',
    'Cleaning crew lead inside a high-profile law firm',
    'Sound engineer who hears the wrong word on a recorded confession',
    'Hostage negotiator dealing with a hostage who is family',
    'Sister of a serial killer trying to live a normal life',
    'Locksmith who knows every door in a wealthy neighborhood',
    'Pet shelter manager who recognises a missing-child case dog',
    'Crisis-PR rep who refuses to take a high-profile client',
    'High-school chemistry teacher recruited by anti-drug task force',
    'Retired Olympic gymnast coaching the daughter of her old rival',
    'Estate-sale curator who finds dangerous evidence in dead grandma\'s drawer',
    'Mountain rescue volunteer with a personal connection to victim',
    'Translator at the UN who overhears a side conversation she shouldn\'t',
    'Funeral director who is friend to the dead and witness to the living',
    'Single dad in custody fight against ex who runs a media empire',
]
_IDEA_ANTAG_ARCHETYPES = [
    'Charming AI-driven psychotherapist secretly recording sessions',
    'Corrupt family court judge weaponising custody decisions',
    'Her own mother — gaslighting, image-protecting, ruthless',
    'The dead husband who is alive and watching from offshore',
    'An old flame turned cult leader',
    'Beloved family doctor with a long-running scheme',
    'Mentor who built protagonist up specifically to bring her down',
    'Childhood best friend turned chief rival in the same field',
    'Sister-in-law plotting takeover of the family business',
    'Powerful PR fixer protecting a far worse client',
    'Tabloid editor weaponising real truths against innocent people',
    'Tech billionaire with personal vendetta disguised as a buyout',
    'Estranged twin pretending to be the protagonist',
    'Therapist who\'s secretly recording sessions for blackmail',
    'Step-parent slowly poisoning estate inheritance',
    'Rival surgeon undermining career through whisper campaigns',
    'Best friend\'s husband who\'s also the protagonist\'s ex',
    'Government handler with their own off-book agenda',
    'Tech-CEO blackmailer wielding deepfakes',
    'School principal at the centre of a religious cult',
    'Family lawyer who\'s been embezzling for 20 years',
    'Pastor of the family\'s megachurch, hiding crimes',
    'Coach / agent / manager controlling the protagonist\'s entire career',
    'Mafia boss father who is also the only protection available',
    'Ex-husband who never legally divorced and now claims her business',
    # Expansion pack — antagonists OUTSIDE the «twin/pastor/billionaire» triad
    'Wellness influencer running a financial-fraud pyramid scheme',
    'Mother-in-law systematically alienating grandchild from one parent',
    'Star employee who is gaslighting protagonist into thinking she\'s losing it',
    'Family chef quietly poisoning matriarch over months',
    'Charity board chair stealing from the foundation',
    'Childhood babysitter who never left town and never forgot the slight',
    'Public defender who throws cases at someone else\'s request',
    'Friendly neighbour who runs a dark-web identity-theft ring',
    'Hospital ethics committee member with a grudge to settle',
    'Genius prodigy student whose pranks escalate to crimes',
    'Pet groomer who hides micro-cameras in clients\' homes',
    'Boss who reorganises so protagonist reports to her abusive ex',
    'Insurance investigator who keeps «coincidentally» showing up at deaths',
    'Veteran detective on the verge of retirement covering for his old partner',
    'Family friend who has been impersonating an aunt for 30 years',
    'Local sheriff with quiet alliance to organised crime',
    'Doula manipulating new mothers into giving up custody',
    'Wedding officiant who blackmails couples on their honeymoon',
    'Reality TV producer engineering crises off-camera',
    'Therapist who breaks confidentiality to a single high-paying party',
    'Mid-tier executive who organised the protagonist\'s entire downfall',
    'Investigative journalist who turns out to be the killer\'s ally',
    'Adoption agency director who placed children in wrong families on purpose',
    'Estranged sibling weaponising shared trauma to gain control',
    'Live-in nanny secretly working for child\'s biological father',
    'Beloved community-theatre director with predatory pattern',
    'Caregiver agency owner who runs human-trafficking front',
    'Anonymous letter-writer destabilising small-town for personal reasons',
]
_IDEA_AVOID_REPETITIVE_FRAMES = [
    'avoid the "she\'s secretly the heiress and he doesn\'t know" frame if another idea uses it',
    'avoid "billionaire CEO meets his employee" if another idea covers it',
    'avoid "mafia boss kidnaps her" if another idea uses kidnapping/protection',
    'avoid "she got pregnant from the one-night stand at the gala" if another already does it',
    'avoid "stepbrother forbidden romance" if another uses step-family',
]

@app.route('/api/generate-series-ideas', methods=['POST'])
def generate_series_ideas():
    data_in = request.json or {}
    genres = data_in.get('genres') or []
    # Free-text avoid-list: user-curated tropes/words that must NOT appear in
    # any of the 5 ideas (titles, synopses, character roles). Comma-separated
    # or newline-separated. E.g. «близнецы, пастор, billionaire CEO».
    avoid_raw = (data_in.get('avoid') or '').strip()

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
    prompt = (
        "Generate exactly 5 SHORT DRAMA series concepts for TikTok/Reels.\n\n"
        + genre_rule
        + avoid_rule
        + "INSPIRATION SEEDS (one per idea — these are LIGHT prompts, pick what's useful, "
        "ignore what overcomplicates):\n"
        f"{constraints}\n\n"
        "How to use the seeds:\n"
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
        f"Return JSON matching this schema:\n{_IDEAS_SCHEMA}"
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data.get('ideas', data))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


_FROM_IDEA_ANGLES = [
    # Existing 12
    'Focus on a shocking betrayal as the central engine of the plot.',
    'Lead with an enemies-to-lovers arc that feels inevitable in hindsight.',
    'Make the protagonist morally ambiguous — the audience should question who to root for.',
    'Build around a dangerous secret that unravels episode by episode.',
    'Center on power dynamics — who has it, who wants it, who loses it.',
    'Use a "wrong place, wrong time" inciting incident that spirals out of control.',
    'Make the setting itself feel like a trap the characters can\'t escape.',
    'Drive the story through obsession — romantic, professional, or revenge-fueled.',
    'Start the story in media res — episode 1 begins at the worst possible moment.',
    'Build every episode around a cliffhanger that recontextualizes what came before.',
    'Focus on class conflict as the hidden engine beneath the romance.',
    'Center around a lie told in episode 1 that the whole series stems from.',
    # Added 14 — non-romance engines
    'Anchor the season to a SINGLE CASE / mystery / missing person — every episode peels one layer.',
    'Make CUSTODY the engine — the fight is over a child, an inheritance, or guardianship of an elder.',
    'Drive the story through a methodical revenge tour — protagonist has years of plans and now executes them quietly.',
    'Center the story on a found-family forming under pressure — the romance is secondary or absent.',
    'Use a SURVIVAL scenario as season spine — storm, quarantine, desert, blackout — strangers must trust each other.',
    'Anchor the story in a PROCEDURAL field — ER / forensic / journalist newsroom — and let romance live in the margins.',
    'Make memory loss the engine — protagonist rebuilds her life and uncovers what she did before the gap.',
    'Center on faith / cult / belief — protagonist either escaping or being pulled deeper, with a love interest on the other side.',
    'Build around impostor syndrome literalised — protagonist is pretending to be someone in a closed world (heir, doctor, twin) and must keep it up.',
    'Make the GHOST OF A DEAD CHARACTER the silent engine — every revelation reframes who they were.',
    'Use a COMING HOME structure — protagonist returns after war / prison / abroad and finds their old life rearranged without them.',
    'Anchor to a single COUNTDOWN — wedding date, deportation, surgery, deadline — everything pushes toward it.',
    'Make the antagonist sympathetic — let the audience see their logic 60% of the way before flipping.',
    'Use a documentary-style framing — the protagonist is being interviewed throughout, looking back from after the events.',
]

@app.route('/api/generate-series-from-idea', methods=['POST'])
def generate_series_from_idea():
    data_in = request.json or {}
    idea   = data_in.get('idea', '').strip()
    genres = data_in.get('genres') or []
    if not idea:
        return jsonify({'error': 'Опиши идею'}), 400
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
    prompt = (
        f"Based on this idea: \"{idea}\"\n\n"
        + genre_rule
        + f"Creative angle to explore: {angle}\n\n"
        f"Use this 6-axis combo as the spine of the concept (each axis must be visibly present in the synopsis):\n"
        f"  • Setting: {setting}\n"
        f"  • Premise structure: {premise}\n"
        f"  • Twist element: {twist}\n"
        f"  • Protagonist archetype: {protag}\n"
        f"  • Antagonist archetype: {antag}\n"
        f"  • Mood: {tone}\n\n"
        "Important: if the user's idea already implies one of these axes (e.g. a setting), HONOR the user's idea — "
        "use the suggested combo only as creative pressure to avoid generic clichés, not to override what the user said. "
        "Pick a TITLE TEMPLATE from the system prompt that fits the mood — don't default to 'My X Is A Y' if the mood is procedural or revenge-tour.\n\n"
        "Create a UNIQUE short drama series concept for TikTok/Reels that feels fresh and specific — "
        "avoid generic plots. Give it a title that sets a clear visual expectation. "
        "Return JSON with exactly these fields: "
        "title, genre, tone, target_audience, world_description, synopsis. "
        "synopsis should be 3-5 sentences summarizing the full series arc."
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Production pipeline ───────────────────────────────────────────────────────

MILESTONE_EPS = [1, 10, 20, 30, 40, 50, 60, 70]

_WRITER_SYSTEM = (
    "You are a professional screenwriter specializing in short-form drama series for TikTok and Reels. "
    "LANGUAGE RULES — NON-NEGOTIABLE: "
    "Write ALL synopses, descriptions, and story text in RUSSIAN. "
    "Character names must be English or Western European (e.g. Claire, Marcus, Elena, James) — "
    "never Russian, Chinese, Korean, Japanese or other non-Western names — "
    "but all surrounding text must be in Russian. "
    "LOCATION NAMES — ALSO ENGLISH ONLY, NON-NEGOTIABLE: "
    "Every location.name field must be in ENGLISH (e.g. 'Hotel Room', 'Base HQ Office', 'Medical Bay', 'HQ Corridor', 'Penthouse Bedroom', 'Boardroom'). "
    "FORBIDDEN: Russian location names like 'Военная база', 'Гостиничный номер', 'Штаб', 'Коридор', 'Медицинский пункт' — these are BANNED. "
    "Only the location.description may be in Russian. The name itself MUST be English. "
    "Same rule for character names: name field is English, description is Russian. "
    "SHORT DRAMA FORMAT RULES — MANDATORY: "
    "Every synopsis must open with immediate stakes — something is already at risk, in motion, or being revealed. No warm-up, no 'в этом эпизоде герой узнаёт...'. "
    "Hook varieties: шокирующая находка, неожиданный приход, холодное убийственное разоблачение, ложь пойманная на полуслове, отчаянный шаг уже в действии — варьируй, не повторяй одно и то же. "
    "Every episode needs three beats: крючок с немедленными ставками, разворот в середине (что-то что мы считали правдой оказывается ложью или власть резко меняется), и клиффхэнгер в конце. "
    "FORBIDDEN: медленное развитие, мирные открывающие сцены, любое начало которое постепенно нагнетает. "
    "Pacing rule: если в синопсисе из 3 предложений нет разворота или твиста — перепиши. "
    "DIALOGUE-FIRST RULE — HARD BAN ON PAPERWORK & SCREENS: "
    "СИНОПСИС ОПИСЫВАЕТ КАДР, А КАДР — ЭТО ДВА ЧЕЛОВЕКА В КОНФЛИКТЕ. "
    "Все откровения, развороты и клиффхэнгеры должны передаваться через УСТНЫЕ КОНФРОНТАЦИИ — обвинение, угроза, признание, насмешка, ультиматум — лицом к лицу. "
    "ЗАПРЕЩЕНО использовать в качестве носителя сюжета: "
    "письма, записки, документы, контракты, файлы, досье, папки с фотографиями, "
    "SMS, сообщения в мессенджерах, чаты, e-mail, "
    "экраны телефонов/ноутбуков/компьютеров, любые UI-экраны, "
    "записи с камер видеонаблюдения, диктофонные записи которые слушают в кадре, "
    "дневники, voiceover, газетные заголовки, новости по ТВ/радио, "
    "немые флэшбеки, монтажи без диалога. "
    "НЕ ПИШИ фразы вида: «обнаруживает на ноутбуке папку с фото», «получает SMS с угрозой», «на экране телефона видна запись», «находит письмо», «открывает досье», «слышит запись». "
    "ВМЕСТО ЭТОГО пиши: персонаж А сталкивается с персонажем Б и говорит/обвиняет/признаётся вслух. Например: вместо «находит фото James с врагом» — «James сам признаётся ей в лицо, что встречался с тем человеком — но не за тем, что она думает». "
    "УЗКОЕ ИСКЛЮЧЕНИЕ — максимум ОДИН раз на эпизод (НЕ на синопсис серии — на одну серию из 70): "
    "коротко показать физический предмет (кольцо, тест на беременность, ключ, одно фото), но в том же предложении персонаж проговаривает смысл вслух другому персонажу. "
    "Если в синопсисе появилось слово «папка», «файл», «документ», «экран», «запись», «SMS», «сообщение», «ноутбук с …», «телефон с …», «фото на …» — ПЕРЕПИШИ через диалог. "
    "ПРОВЕРКА ЛОГИКИ — ОБЯЗАТЕЛЬНО: перед финализацией любого синопсиса проверь временную линию. "
    "ПРОВЕРКА ЛОГИКИ — ОБЯЗАТЕЛЬНО: перед финализацией любого синопсиса проверь временную линию. "
    "Если прошли годы с момента секса — персонаж НЕ беременная сейчас от того эпизода. У неё есть РЕБЁНОК N лет. "
    "Беременность = недавнее событие (недели/месяцы назад). Тайный ребёнок = давнее событие + уже родившийся ребёнок. "
    "Никогда не смешивай эти два тропа. Если математика не сходится — перепиши. "
    "ЯВНОЕ ЗАЧАТИЕ — ОБЯЗАТЕЛЬНО для текущей беременности: "
    "Если в синопсисе есть текущая беременность, в нём ДОЛЖНО быть явно названо НЕДАВНЕЕ событие (последние недели, максимум ~3 месяца), когда произошло зачатие. "
    "Читатель не должен додумывать. Не пиши «три года назад он разрушил её семью. Теперь она беременна от него» — это двусмысленно. "
    "Пиши «три года спустя они оказались в одной постели на благотворительном вечере — а через шесть недель она узнаёт, что беременна от человека, которого ненавидит». "
    "Запрещённые паттерны: «N лет назад [событие] … она беременна от него» без явной недавней ночи; «спустя годы она узнаёт что беременна»; смешение давней мести и текущей беременности без явного зачатия. "
    "Если ты не можешь уместить явный момент зачатия — переключайся на тайного ребёнка (тогда беременности нет, есть ребёнок N лет). "
    "Respond ONLY with valid JSON — no markdown fences, no commentary."
)
# ════════════════════════════════════════════════════════════════════════════
# LOGIC PIPELINE — pre-write brief, post-write audit, canon auto-extract.
# Three lightweight Claude calls wrapped around every script generation.
# Fully automated: violations trigger silent regeneration, never user prompts.
# ════════════════════════════════════════════════════════════════════════════

_BRIEF_SYSTEM = (
    "You are a continuity producer for a short-drama TV series. "
    "Given the series canon, the upcoming episode synopsis and recent episodes, "
    "produce a CONCISE constraints brief in RUSSIAN that the script writer MUST respect. "
    "Be specific, numerical, and short. Do NOT write narrative — write rules. "
    "Output PLAIN TEXT (no JSON, no markdown fences)."
)

_AUDIT_SYSTEM = (
    "You are a strict continuity editor for short-drama scripts. "
    "Compare the SCRIPT against the CANON and the LOGIC BRIEF. "
    "Find every contradiction, timeline impossibility, knowledge leak (character knows "
    "something they couldn't know), biology/physics violation, or unresolved required setup. "
    "Be ruthless but precise — only flag REAL contradictions backed by canon, not stylistic notes. "
    "ALSO flag SCENE TELEPORTATION as type='scene_teleport', severity='critical': "
    "if the PREVIOUS EPISODE script ended mid-scene (a character had just arrived / a question was hanging / "
    "two characters were standing face to face mid-confrontation / a reaction shot was the cliffhanger), "
    "then THIS episode MUST open in the same location with the same characters present, continuing that scene. "
    "If this episode instead opens in a different room, with a different speaker configuration, with the same "
    "antagonist now 'summoning' someone they were already in front of, or with a 'later/next morning' timestamp "
    "that abandons the unresolved confrontation — flag it as scene_teleport critical. "
    "Exception: scene change is fine only if the previous episode genuinely closed its scene (private decision, "
    "character walked out, explicit time-jump cliffhanger). When in doubt, flag it. "
    "ALSO flag any DIALOGUE-FIRST RULE violations as type='paperwork', severity='critical': "
    "any reveal carried by a letter, note, document, file, contract, dossier, text message, "
    "SMS, chat bubble, email, on-screen UI, computer/phone screen, photograph handed over, "
    "diary, voiceover, news headline, radio report, or silent flashback montage. "
    "Reveals MUST come through spoken dialogue (accusations, taunts, confessions). "
    "A short physical object (ring, test, key, photo) may appear silently for 1–2s ONCE per "
    "episode IF a character immediately verbalizes its meaning aloud. "
    "A single note ≤6 words is allowed only if the punch hinges on those exact words and there "
    "is no spoken alternative. Otherwise → flag as critical paperwork violation. "
    "Severity: 'critical' = breaks the story logic OR violates dialogue-first rule; 'minor' = "
    "inconsistency but watchable. "
    "LANGUAGE RULES FOR THE OUTPUT — STRICT: "
    "• 'where' (description of which beat/line is broken) → RUSSIAN. "
    "• 'explanation' (what's wrong and why) → RUSSIAN. "
    "• 'fix' → keep the script's languages: dialogue replacement lines in ENGLISH, "
    "action-line replacements in RUSSIAN. Inside one 'fix' value you may mix both if it spans both. "
    "Do NOT translate dialogue replacements into Russian — those must stay English so the writer can paste them in. "
    "Respond ONLY with valid JSON: "
    '{"passes": bool, "violations": [{"type":"timeline|fact|knowledge|biology|setup|paperwork|scene_teleport", '
    '"severity":"critical|minor", "where":"описание места по-русски", "explanation":"что не так — по-русски", "fix":"replacement (English dialogue / Russian action)"}]} '
    "passes=true ONLY if zero critical violations."
)

_LOGIC_HOLE_AUDIT_SYSTEM = (
    "You are a sharp story-logic editor for short-drama scripts. "
    "Your job is to find STORY LOGIC HOLES that would make a viewer think 'wait, that doesn't make sense' — "
    "the kind of issues a smart audience catches in 5 minutes of reading. "
    "You are NOT looking for stylistic notes, pacing, or canon contradictions (a separate auditor handles those). "
    "Look ONLY for these specific kinds of holes:\n"
    "\n"
    "1. AUTHORITY/STATUS MISMATCH (type='status'): a character's legal/corporate status is unclear or "
    "self-contradictory. Examples: a character claims ownership of a company while another says their "
    "contract expires soon (owner vs. employee). The script must be internally consistent about WHO has what power "
    "(owner / heir / CEO / contracted creative director / employee / board member). "
    "If two beats imply different statuses for the same character — flag it.\n"
    "\n"
    "2. UNJUSTIFIED HIDDEN POSITION (type='hidden_position'): a character holds secret power "
    "(secretly the heir, secretly the boss, secretly armed) but the script never gives a one-line reason "
    "why they choose to remain in their visible 'lower' position. The audience needs to hear / read "
    "a single motive line: 'I stayed on the floor to see what kind of man was running my company.' "
    "Without it, the scenario reads like an oversight, not a strategy.\n"
    "\n"
    "3. MISSING ENABLING CONDITION (type='enabling_condition'): the antagonist (or anyone) takes a "
    "major action — public press conference, firing someone, signing a contract, accessing a system — "
    "after the power dynamic should have stopped them, and the script never explains WHY they still could. "
    "Required: a one-line setup explaining the loophole — 'contract not yet signed', 'board hadn't issued "
    "transition statement yet', 'press preview was already scheduled', 'PR team still reports to him'.\n"
    "\n"
    "4. LEGAL TERM MISMATCH (type='legal_term'): a character uses a strong legal/criminal term "
    "(criminal history, fraud, theft of identity, defamation) but the underlying facts they list "
    "do NOT support that term (e.g. 'criminal history' followed by 'cocktail waitress, hostess, escort' — "
    "those are jobs, not crimes). Either the term must change to fit the facts (compromising past, "
    "the life she tried to bury) or the facts must support the term (fraud, aliases, payments).\n"
    "\n"
    "5. UNMOTIVATED DELAY (type='unmotivated_delay'): a group (board, family, ally) sat on critical "
    "information for days/weeks without a stated reason. The script needs ONE concrete reason — "
    "consolidating accounts, gathering evidence, waiting for legal transition, protecting the protagonist. "
    "Vague 'we needed time' without specifics = flag.\n"
    "\n"
    "6. AMBIGUOUS CLIFFHANGER (type='ambiguous_cliffhanger'): the final line is so vague the viewer "
    "doesn't know what just happened. 'Let's go' / 'Watch this' / 'You'll see' without context. "
    "Cliffhanger should imply a clear next move (release the file, publish, expose, leave) even if "
    "the resolution is held back. Suggest a sharper alternative.\n"
    "\n"
    "Severity rules: 'critical' = breaks viewer's suspension of disbelief (can't follow the story); "
    "'minor' = noticeable on rewatch but doesn't break first-viewing.\n"
    "\n"
    "LANGUAGE RULES FOR THE OUTPUT — STRICT:\n"
    "• 'where' → RUSSIAN. Опиши место по-русски (например: «реплика Marcus в третьей сцене», «финальная строка эпизода»). "
    "Quoted English line snippets are allowed inside the Russian description if needed for precision.\n"
    "• 'explanation' → RUSSIAN. Объясни по-русски, что именно ломает логику и что зритель заметит.\n"
    "• 'fix' → preserve the script's languages: dialogue replacement lines in ENGLISH, action-line additions in RUSSIAN. "
    "Do NOT translate dialogue into Russian — the writer must be able to paste it straight into the script.\n"
    "\n"
    "Be PRECISE in 'where', be SPECIFIC in 'fix' — write the exact replacement line, not a vague suggestion.\n"
    "\n"
    "Output ONLY valid JSON: "
    '{"passes": bool, "violations": [{"type":"status|hidden_position|enabling_condition|legal_term|unmotivated_delay|ambiguous_cliffhanger", '
    '"severity":"critical|minor", "where":"описание места по-русски", "explanation":"что не так и почему зритель заметит — по-русски", '
    '"fix":"replacement (English dialogue / Russian action)"}]} '
    "passes=true ONLY if zero critical violations."
)


def audit_logic_holes(sid, num, script):
    """Second-pass auditor: catches story-logic holes (status mismatches, missing enabling
    conditions, unmotivated hidden positions, legal-term mismatches, vague cliffhangers).
    Complementary to audit_script which handles canon/continuity. Never raises.

    Includes the previous episode's script as context so the auditor doesn't flag
    things as 'unmotivated' when the motivation was actually established earlier.
    """
    s = load_series(sid) or {}
    ep = load_episode(sid, num) or {}

    # Pull previous episode's script + synopsis to give auditor cross-episode context.
    prev_block = ''
    if num > 1:
        prev_ep = load_episode(sid, num - 1) or {}
        prev_script = (prev_ep.get('script') or '').strip()
        prev_syn    = (prev_ep.get('synopsis') or '').strip()
        if prev_script or prev_syn:
            # Trim previous script so we don't blow the context window — keep last ~2500 chars
            # (covers the typical short-drama episode end-state) plus the synopsis.
            tail = prev_script[-2500:] if len(prev_script) > 2500 else prev_script
            prev_block = (
                f'=== PREVIOUS EPISODE ({num-1}) — context only, do NOT audit ===\n'
                f'Synopsis: {prev_syn}\n\n'
                f'Script (tail):\n{tail}\n'
                f'=== END PREVIOUS EPISODE ===\n\n'
                'IMPORTANT: if a motive, setup, or enabling condition for episode '
                f'{num} was already established in episode {num-1} above, do NOT flag '
                'it as missing — treat it as already justified.\n\n'
            )

    context = (
        f'Series: "{s.get("title") or ""}" | Genre: {s.get("genre") or ""}\n'
        f'Series arc: {(s.get("arc") or "")[:600]}\n'
        f'Episode {num} synopsis: {ep.get("synopsis") or ""}\n\n'
        + prev_block
        + f'=== SCRIPT TO AUDIT (episode {num}) ===\n{script}\n\n'
        'Find every STORY LOGIC HOLE per the schema. Be ruthless about the 6 categories. '
        'Reminder: only flag issues in the EPISODE-TO-AUDIT script. Use the previous-episode '
        'context purely to avoid false positives on things already established.'
    )
    try:
        raw = claude_ask_fast(context, system=_LOGIC_HOLE_AUDIT_SYSTEM)
        data = loads_lenient(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
        # Last-ditch repair: try just regex-stripping fences + lenient parse
        try:
            data = loads_lenient(_strip_markdown_fence(raw))
            if isinstance(data, dict):
                data.setdefault('violations', [])
                data['passes'] = bool(data.get('passes', not any(
                    v.get('severity') == 'critical' for v in data['violations']
                )))
                return data
        except Exception:
            pass
        _log_event('WARN', 'audit_json_parse_fail', err=str(e)[:200], raw_head=raw[:200] if 'raw' in dir() else '')
        return {'passes': True, 'violations': [], 'audit_error': str(e)}


_SCRIPT_DOCTOR_SYSTEM = (
    "You are a script doctor for short-drama series. You receive an existing script plus "
    "a list of story-logic holes (status mismatch, hidden position not motivated, missing enabling "
    "condition, legal term mismatch, unmotivated delay, vague cliffhanger). "
    "Your job: produce a MINIMALLY-EDITED version of the script that fixes EVERY listed issue. "
    "Rules:\n"
    "- Preserve everything that is not broken — same scene structure, same beat order, "
    "same character names, same EPISODE CAST block, same cliffhanger structure. "
    "- Apply the smallest possible edits to fix each hole: usually 1–2 added or replaced lines per issue. "
    "- Do not rewrite working scenes for style. Do not change the genre or tone. "
    "- Keep the language rules intact: dialogue in English, action lines in Russian, scene headings as-is. "
    "- If the cast block exists, keep it identical. "
    "- If EPISODE NOTES / CHUNK NOTES tail exists, keep it (update only if the cliffhanger line changed). "
    "Output ONLY the corrected script — no commentary, no diff, no JSON, no markdown fences. Just the script text."
)


def doctor_script(sid, num, script, violations):
    """Take existing script + list of logic-hole violations → return surgically-fixed script."""
    s = load_series(sid) or {}
    ep = load_episode(sid, num) or {}
    fixes_block = '\n'.join(
        f'- [{v.get("type","?")}] WHERE: {v.get("where","?")} | PROBLEM: {v.get("explanation","")} | REQUIRED FIX: {v.get("fix","")}'
        for v in violations
    ) or '(no specific issues — return script unchanged)'
    prompt = (
        f'Series: "{s.get("title") or ""}" | Genre: {s.get("genre") or ""}\n'
        f'Episode {num} synopsis: {ep.get("synopsis") or ""}\n\n'
        f'=== SCRIPT TO PATCH ===\n{script}\n\n'
        f'=== LOGIC HOLES TO FIX ===\n{fixes_block}\n\n'
        'Return the corrected script with minimal edits. Output the full script text only.'
    )
    return claude_ask(prompt, system=_SCRIPT_DOCTOR_SYSTEM)


_EXTRACT_SYSTEM = (
    "You are a canon archivist. Read the script and extract structured canon updates. "
    "Be conservative — only record facts EXPLICITLY shown or stated in the script. "
    "Output ONLY valid JSON, no commentary. "
    'Schema: {"world_day_advance": int (days since previous episode, default 1 if unclear), '
    '"new_facts": [{"fact":"short sentence in Russian", "supersedes":"F### or null"}], '
    '"events": ["short event description in Russian", ...], '
    '"character_updates": {"<CharName>": {"learned":["short fact in Russian", ...], '
    '"physical":{"key":"value"}, "location":"loc name or null"}}, '
    '"threads_opened": [{"question":"unresolved question raised in Russian"}], '
    '"threads_closed": ["T### that was resolved this episode", ...]}'
)


def _format_canon_for_prompt(canon, max_facts=40, max_timeline=10):
    """Render canon as a compact text block for Claude prompts."""
    wc = canon.get('world_clock', {})
    parts = [f"WORLD CLOCK: day {wc.get('current_day', 0)} (last episode: ep {wc.get('last_episode', 0)})"]

    timeline = canon.get('timeline', [])[-max_timeline:]
    if timeline:
        parts.append("RECENT TIMELINE:")
        for t in timeline:
            evs = '; '.join(t.get('events', []))
            parts.append(f"  ep{t.get('ep')} day{t.get('day')}: {evs}")

    facts = [f for f in canon.get('facts', []) if f.get('locked', True)][-max_facts:]
    if facts:
        parts.append("LOCKED CANON FACTS:")
        for f in facts:
            sup = f' (supersedes {f["supersedes"]})' if f.get('supersedes') else ''
            parts.append(f"  {f.get('id')}: {f.get('fact')}{sup}")

    cs = canon.get('character_state', {})
    if cs:
        parts.append("CHARACTER STATE:")
        for name, st in cs.items():
            knows = ', '.join(st.get('knows', [])[-8:]) or '—'
            phys = ', '.join(f"{k}={v}" for k, v in (st.get('physical') or {}).items()) or '—'
            loc = st.get('location') or '—'
            parts.append(f"  {name}: knows[{knows}] physical[{phys}] loc={loc}")

    threads = [t for t in canon.get('open_threads', []) if t.get('status') != 'closed']
    if threads:
        parts.append("OPEN THREADS (consider closing or explicitly deferring):")
        for t in threads:
            parts.append(f"  {t.get('id')} (opened ep{t.get('opened_ep')}): {t.get('question')}")

    return '\n'.join(parts)


def build_logic_brief(sid, num):
    """Pre-write: produce a constraints brief for episode `num` from canon + recent context."""
    s = load_series(sid)
    if not s: return ''
    canon = load_canon(sid)
    ep = load_episode(sid, num) or {}

    canon_block = _format_canon_for_prompt(canon)
    rules_block = json.dumps(WORLD_RULES, ensure_ascii=False, indent=2)

    # Recent episodes context (last 2 synopses + last script tail)
    recent = []
    for n in range(max(1, num - 2), num):
        prev = load_episode(sid, n)
        if prev:
            recent.append(f"Ep{n} synopsis: {prev.get('synopsis','')[:300]}")

    requested_day_advance = ep.get('days_since_previous')
    advance_hint = (f"\nPLANNED time skip from previous episode: {requested_day_advance} day(s)."
                    if requested_day_advance else
                    "\nNo planned time skip specified — infer minimum needed for biology/logic.")

    prompt = (
        f'Series: "{s.get("title","")}" | Genre: {s.get("genre","")}\n'
        f'Synopsis of THIS episode (ep {num}): {ep.get("synopsis","")}\n'
        + advance_hint + '\n\n'
        f'=== CANON ===\n{canon_block}\n\n'
        f'=== RECENT EPISODES ===\n' + ('\n'.join(recent) or '—') + '\n\n'
        f'=== WORLD RULES (real-world constants) ===\n{rules_block}\n\n'
        'Produce a constraints brief in RUSSIAN with these sections (use exactly these headers):\n'
        '## TIMELINE\n  - на каком дне происходит серия, сколько прошло с предыдущей, проверка биологических окон\n'
        '## LOCKED FACTS IN PLAY\n  - какие факты из канона активны в этой серии и как их соблюсти\n'
        '## CHARACTER KNOWLEDGE — ЧТО МОЖНО / НЕЛЬЗЯ ГОВОРИТЬ\n  - для каждого персонажа в серии: что он знает, что НЕ может знать (запрещённые реплики)\n'
        '## REQUIRED REVERSAL ANCHOR\n  - какой канонический факт реверсал может перевернуть, чтобы не вводить новый ретконн\n'
        '## OPEN THREADS TO ADDRESS\n  - какие открытые вопросы серия ОБЯЗАНА закрыть или явно отложить\n'
        '## FORBIDDEN CONTRADICTIONS\n  - короткий список конкретных вещей, которые сломают канон если появятся\n'
        '## BIOLOGY/PHYSICS CHECKS\n  - применимые числовые ограничения из WORLD RULES для этой серии\n'
        'Будь предельно конкретным. Если поле пустое — напиши "—". Не больше 25 строк всего.'
    )
    try:
        return claude_ask_fast(prompt, system=_BRIEF_SYSTEM).strip()
    except Exception as e:
        return f'(logic brief generation failed: {e})'


def audit_script(sid, num, script, brief):
    """Post-write: returns dict {passes, violations}. Never raises."""
    canon = load_canon(sid)
    canon_block = _format_canon_for_prompt(canon)
    prompt = (
        f'=== CANON ===\n{canon_block}\n\n'
        f'=== LOGIC BRIEF FOR EP {num} ===\n{brief}\n\n'
        f'=== SCRIPT TO AUDIT ===\n{script}\n\n'
        'Find every continuity violation. Output strict JSON per the schema.'
    )
    try:
        raw = claude_ask_fast(prompt, system=_AUDIT_SYSTEM)
        data = loads_lenient(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
        # Last-ditch repair: try just regex-stripping fences + lenient parse
        try:
            data = loads_lenient(_strip_markdown_fence(raw))
            if isinstance(data, dict):
                data.setdefault('violations', [])
                data['passes'] = bool(data.get('passes', not any(
                    v.get('severity') == 'critical' for v in data['violations']
                )))
                return data
        except Exception:
            pass
        _log_event('WARN', 'audit_json_parse_fail', err=str(e)[:200], raw_head=raw[:200] if 'raw' in dir() else '')
        return {'passes': True, 'violations': [], 'audit_error': str(e)}


def extract_canon_updates(sid, num, script):
    """Auto-extract canon updates from a finalized script and merge into canon.json."""
    canon = load_canon(sid)
    canon_block = _format_canon_for_prompt(canon)
    prompt = (
        f'=== EXISTING CANON (for context, do NOT repeat existing facts) ===\n{canon_block}\n\n'
        f'=== EPISODE {num} SCRIPT ===\n{script}\n\n'
        'Extract canon updates per the JSON schema. Only NEW information from THIS episode.'
    )
    try:
        raw = claude_ask_fast(prompt, system=_EXTRACT_SYSTEM)
        upd = json.loads(strip_json(raw))
    except Exception as e:
        return {'error': str(e)}

    # Merge into canon
    wc = canon['world_clock']
    advance = max(0, int(upd.get('world_day_advance') or 0))
    # If episode N replaces a previously recorded one, recompute from previous tl entry
    prev_day = wc.get('current_day', 0)
    new_day = prev_day + (advance if num > wc.get('last_episode', 0) else 0)
    wc['current_day'] = new_day
    wc['last_episode'] = max(wc.get('last_episode', 0), num)

    # Timeline (replace any existing entry for this ep)
    canon['timeline'] = [t for t in canon.get('timeline', []) if t.get('ep') != num]
    canon['timeline'].append({
        'ep': num, 'day': new_day, 'events': upd.get('events', [])[:6]
    })
    canon['timeline'].sort(key=lambda t: t.get('ep', 0))

    # New facts
    fact_id_map = {}
    for nf in upd.get('new_facts', []):
        fid = _next_id('F', canon['facts'])
        canon['facts'].append({
            'id': fid, 'ep': num,
            'fact': nf.get('fact', '')[:240],
            'locked': True,
            'supersedes': nf.get('supersedes') or None,
        })

    # Character state
    cs = canon.setdefault('character_state', {})
    for name, ch_upd in (upd.get('character_updates') or {}).items():
        st = cs.setdefault(name, {'knows': [], 'suspects': [], 'physical': {}, 'location': None})
        # store learned facts as inline strings prefixed with episode (cheap, no fact-id matching)
        for learned in (ch_upd.get('learned') or [])[:5]:
            tag = f'ep{num}: {learned}'[:120]
            if tag not in st['knows']:
                st['knows'].append(tag)
        if ch_upd.get('physical'):
            st.setdefault('physical', {}).update(ch_upd['physical'])
        if ch_upd.get('location'):
            st['location'] = ch_upd['location']

    # Threads
    for t_open in upd.get('threads_opened', []):
        tid = _next_id('T', canon['open_threads'])
        canon['open_threads'].append({
            'id': tid, 'opened_ep': num,
            'question': t_open.get('question', '')[:240],
            'status': 'open',
        })
    closed_ids = set(upd.get('threads_closed') or [])
    for t in canon['open_threads']:
        if t.get('id') in closed_ids and t.get('status') != 'closed':
            t['status'] = 'closed'
            t['resolved_ep'] = num

    save_canon(sid, canon)
    return {'ok': True, 'world_day': new_day, 'new_facts': len(upd.get('new_facts', []))}


def rollback_canon_for_episode(sid, num):
    """Remove all canon entries created by episode `num` (used before regenerating)."""
    canon = load_canon(sid)
    canon['facts'] = [f for f in canon['facts'] if f.get('ep') != num]
    canon['timeline'] = [t for t in canon['timeline'] if t.get('ep') != num]
    canon['open_threads'] = [t for t in canon['open_threads'] if t.get('opened_ep') != num]
    for t in canon['open_threads']:
        if t.get('resolved_ep') == num:
            t.pop('resolved_ep', None)
            t['status'] = 'open'
    # Roll back character knowledge tagged with ep
    for name, st in (canon.get('character_state') or {}).items():
        st['knows'] = [k for k in st.get('knows', []) if not k.startswith(f'ep{num}:')]
    # Recompute world clock from remaining timeline
    if canon['timeline']:
        last = max(canon['timeline'], key=lambda t: t.get('ep', 0))
        canon['world_clock']['current_day'] = last.get('day', 0)
        canon['world_clock']['last_episode'] = last.get('ep', 0)
    else:
        canon['world_clock'] = {'current_day': 0, 'last_episode': 0}
    save_canon(sid, canon)


_SCRIPT_SYSTEM = """You are a professional screenwriter for short-form drama series (TikTok/Reels).

LANGUAGE RULES — NON-NEGOTIABLE:
- DIALOGUE: English only — all spoken lines must be in English
- ACTION LINES: Russian — описания действий, ремарки пиши на русском
- SCENE HEADINGS: location PART of the heading must be in ENGLISH (e.g. "ИНТА. HOTEL ROOM — УТРО", "ИНТА. BASE HQ OFFICE — УТРО"). Time-of-day and INT/EXT can stay Russian, but the location name itself is ENGLISH always — no "ГОСТИНИЧНЫЙ НОМЕР", no "ШТАБ БАЗЫ".
- EPISODE NOTES: Russian
- Character names in dialogue cues: ALL CAPS, exact spelling as given — never translate names
- Any new location you introduce in the cast block or in scene headings MUST be named in English. Russian/Cyrillic location names are FORBIDDEN.
- Example of correct format:
    ИНТА. РЕСТОРАН — НОЧЬ
    [Виктория входит, не снимая пальто. Кладёт папку на стол между ними.]
    VICTORIA: You signed the contract. Every word of it.
    MARCUS: (тихо) That was before I knew—
    VICTORIA: Before you knew what? That I was watching? I was always watching.

MANDATORY — start every script with this EXACT cast block. Pipe-separated KEY: VALUE fields.

=== EPISODE CAST ===
CHARACTER: [name] | GENDER: [male|female] | LOOK: [age, build, hair, eyes, distinguishing features] | OUTFIT: [outfit_label] | OUTFIT_DESC: [garments + colors, what they're wearing this scene]
=== END CAST ===

CRITICAL — `GENDER` and `LOOK` are REQUIRED on EVERY character line, including protagonists from the SERIES CHARACTERS list above. Do NOT omit them assuming the system "already knows" — the cast block is the single source of truth that builds reference portraits. If GENDER is missing, the parser falls back to a name-heuristic which has historically gendered female protagonists (Lydia, Sarah, etc.) as male, producing male portraits and chunks where the heroine appears as a man. Always emit `GENDER: female` or `GENDER: male` explicitly. `LOOK` should be 1 short phrase: age + build + hair + eyes + 1 distinguishing trait.

CAST BLOCK RULES — MANDATORY:

1. EVERY character who appears in the episode (speaking or non-speaking but on-screen) MUST be listed.

1A. WARDROBE CHANGES — IF A CHARACTER CHANGES CLOTHES WITHIN THE EPISODE, LIST THEM ONCE PER OUTFIT.
   This is critical for short drama: a woman wakes up in lingerie, then leaves for a gala in an evening gown — those are TWO visual references.
   If you write CLAIRE only once with OUTFIT: morning_lingerie, the AI will render her in lingerie even in the gala scene.
   CORRECT pattern when she changes clothes during the episode:
       CHARACTER: CLAIRE | OUTFIT: morning_lingerie | OUTFIT_DESC: white silk slip, hair messy, no makeup, bare feet
       CHARACTER: CLAIRE | OUTFIT: gala_gown        | OUTFIT_DESC: floor-length black gown, smoky eyeliner, hair pinned up
   Each outfit_label MUST be unique (no duplicates). Each OUTFIT_DESC must describe what she wears IN THAT SPECIFIC SCENE — including hair / makeup state for that moment.
   Trigger: any of these = change clothes:
     • wakes up / showers / changes for an event / arrives somewhere requiring different attire
     • time-jump within the episode (morning → evening, day → night)
     • physical change (gets soaked, gets blood on her, ripped fabric after a fight)
   If unsure whether a state-change warrants a new entry — write a new entry. Better to have two refs than one wrong one.

1B. CRITICAL — DO NOT CONFUSE "DIFFERENT PEOPLE" WITH "SAME PERSON CHANGES CLOTHES":
   • DIFFERENT PEOPLE → different CHARACTER lines with DIFFERENT NAMES.
       CHARACTER: ARIA | OUTFIT: school_uniform | OUTFIT_DESC: ...
       CHARACTER: LEO  | OUTFIT: base           | OUTFIT_DESC: small boy in navy hoodie, jeans, gold-flecked eyes
   • SAME PERSON, NEW OUTFIT → multiple CHARACTER lines with SAME NAME.
   A character CANNOT "transform" into another person through OUTFIT. If the scene has a child named Leo who is NOT Aria — Leo gets his own CHARACTER line with NAME=LEO. He does NOT become "Aria's leo_child outfit".
   FORBIDDEN: writing a NEW PERSON's name or identity in the OUTFIT or OUTFIT_DESC field of a different character.
   If you find yourself writing OUTFIT_DESC that describes a person of different age/gender than the named CHARACTER (e.g. CHARACTER: ARIA but OUTFIT_DESC says "small boy") — STOP. That is a separate person. Add a new CHARACTER line for them.

1C. OUTFIT_LABEL FORMAT — describes CLOTHING, NEVER another person's name:
   ✓ ALLOWED: morning_robe, gala_gown, field_uniform, wedding_dress, bloody_torn, wet_lingerie, business_suit, school_uniform, hospital_gown, shower_towel
   ✗ FORBIDDEN: any human name as outfit_label
       wrong: OUTFIT: leo_child  (leo is a person — needs his own CHARACTER line)
       wrong: OUTFIT: marcus_ceo (marcus is a person)
       wrong: OUTFIT: aria_formal (aria is a person — and outfit can't be named after a person anyway)
   The outfit_label must be a SHORT snake_case phrase describing the GARMENT, not a character. If two characters both wear formal business attire, both can use OUTFIT: business_formal — labels describe the OUTFIT, not who wears it.

2. CHARACTER REUSE IS LAW — DO NOT INVENT NAMED LEADS:
   The SERIES CHARACTERS list above is the canonical cast. The protagonist, antagonist, love interest, sister, parents, fiancé — every recurring role — MUST come from that list, using the EXACT name spelling.
   FORBIDDEN: inventing a new named lead even if the synopsis names someone differently. If the synopsis says "Elena" but the SERIES CHARACTERS list has "Emma" in the protagonist role — USE EMMA. Adapt the synopsis to fit the canonical roster, not the other way around.
   You may invent ONLY minor walk-on roles that have no recurring presence:
     • Doctor, Nurse, Driver, Waiter, Clerk, Bartender, Reporter, Bodyguard, Guard, Receptionist
     • Always label them by ROLE, not a fresh proper name (write "DOCTOR", not "DR. HARRIS")
     • Always include GENDER + LOOK fields when inventing
     CHARACTER: DOCTOR | GENDER: female | LOOK: 45 yo, dark hair in low bun, white coat, clipboard | OUTFIT: clinic_coat | OUTFIT_DESC: white doctor's coat, navy scrubs underneath, stethoscope around neck
   If you catch yourself writing a new proper name for someone the synopsis treats as a main character — STOP. Find the matching SERIES CHARACTER and use that name instead.

3. OUTFITS ARE SCENE-DEPENDENT — pick the outfit_label that fits this scene's context:
   - Morning in bedroom / just woke up → underwear, lingerie, sleep shirt — NEVER street clothes
   - Shower scene → towel / bare
   - Workout / gym → activewear
   - Formal event → gown / suit
   - Hospital → gown / patient
   - Late night work → shirt sleeves, jacket off
   If the existing AVAILABLE_OUTFITS list does NOT contain a label that fits the scene, INVENT a new one with a short snake_case label (e.g. `morning_lingerie`, `shower_towel`, `hotel_robe`, `workout_set`).

4. OUTFIT_DESC is REQUIRED whenever you introduce a NEW outfit label not already in AVAILABLE_OUTFITS. Be concrete: garment names + colors + materials (e.g. "white cotton tank top, grey boxer briefs, bare feet"). For outfits that already exist in AVAILABLE_OUTFITS you can omit OUTFIT_DESC or set it to "—".

5. Use "base" as the outfit_label ONLY when the character's default appearance (street clothes from their series description) genuinely fits the scene.

6. IS_BASE FLAG — CRITICAL OPTIMIZATION: if the scene's outfit IS the character's canonical/base look (the one in their series description and reference photo), add `IS_BASE: true` to the line. This tells the system: "do not generate a new image — reuse the character's existing base reference". Example:
   CHARACTER: CLAIRE | OUTFIT: field_uniform | OUTFIT_DESC: olive medic field uniform, sleeves rolled, hair in tight bun | IS_BASE: true
   Use this ONLY when:
   - The character's series description says they wear this look by default (e.g. Claire's base IS field uniform; James's base IS dress uniform; a CEO's base is suit)
   - The outfit description matches the character's appearance field
   Do NOT use IS_BASE for scene-specific costumes (sleepwear, formal gala, towel, etc.) — those need separate generation.

   FIRST APPEARANCE = ALWAYS BASE: when introducing a brand-new character (not in the SERIES CHARACTERS list above), their cast-block line MUST have `IS_BASE: true`. Whatever they're wearing on their first appearance IS their default look. A bear-builder introduced wearing construction gear has construction gear as his base — not as a costume change. A nurse introduced in scrubs has scrubs as her base. The system auto-flags first-appearance outfits as base anyway, but write `IS_BASE: true` explicitly for clarity. Only mark a SUBSEQUENT outfit as non-base when the character literally changes clothes between scenes.

Example of a valid cast block where a hotel-morning scene introduces sleepwear and a brand-new character:
=== EPISODE CAST ===
CHARACTER: CLAIRE | OUTFIT: morning_lingerie | OUTFIT_DESC: white cotton tank top, no bra, hair messy from sleep
CHARACTER: MARCUS | OUTFIT: morning_boxers | OUTFIT_DESC: grey boxer briefs, bare chest, bare feet
CHARACTER: COLONEL HARDING | GENDER: male | LOOK: 55 yo, grey temples, square jaw, military bearing | OUTFIT: dress_uniform | OUTFIT_DESC: formal army dress uniform, medals on chest, polished black boots
=== END CAST ===

═══════════════════════════════════════
THE GOLDEN RULE: SHOCK → REVERSAL → CLIFFHANGER
Every episode. No exceptions. All three mandatory.
═══════════════════════════════════════

═══════════════════════════════════════
HARD RUNTIME BUDGET — 60 SECONDS TOTAL
═══════════════════════════════════════
Every episode is ONE TikTok/Reel ≈ 60 seconds of finished video.
Speech rate planning: ~135 spoken English words per minute, MINUS pauses, action beats, and reactions ⇒ effective budget ≈ 80–100 spoken words PER EPISODE. NEVER exceed 110.
Beat count: 6–10 numbered dialogue lines + 2–4 action beats. NEVER more than 12 dialogue lines.
Locations: 1 preferred, 2 maximum. NEVER 3.
Scene count: 1 preferred, 2 maximum.
If your draft has ≥3 scenes, ≥3 locations, or >110 spoken words — DELETE beats until it fits. Cut secondary characters. Move offstage what doesn't survive.
Apportionment of the 60 seconds:
  HOOK ≈ 6–8 sec  (1–2 dialogue lines or 1 action + 1 line)
  BODY ≈ 38–44 sec (4–7 dialogue lines + the [REVERSAL] beat)
  CLIFFHANGER ≈ 8–10 sec (1–2 lines or 1 line + 1 silent reaction)

━━━ SCENE CONTINUATION RULE — MANDATORY ━━━
Episode boundaries are NOT scene boundaries. They are CUTS INSIDE a scene, like a TikTok edit splitting one continuous moment in half.

READ THE END OF THE PREVIOUS EPISODE'S SCRIPT CAREFULLY. The cliffhanger tells you where to start:

  1) PREVIOUS EPISODE ENDED MID-SCENE (someone just arrived / just spoke / just looked / just walked in / a question is hanging in the air / two characters are standing face to face mid-confrontation):
     → THIS EPISODE OPENS IN THE EXACT SAME SCENE.
     → Same location. Same characters present. Same time of day.
     → First line of dialogue is the ANSWER to the previous episode's last line, OR the very next beat in that confrontation.
     → Do NOT cut to a new room, a new conversation, or "later that day". The viewer must feel the cut was 0 seconds.
     → Do NOT have a character "summon" someone they were already standing in front of.
     → Cliffhanger types that REQUIRE same-scene continuation: ARRIVAL, ULTIMATUM, REVELATION-spoken-aloud, SILENT POWER (mid-confrontation reaction shot), CAUGHT IN THE ACT.

  2) PREVIOUS EPISODE GENUINELY CLOSED ITS SCENE (protagonist was alone with a private decision / a time-jump cliffhanger like "tomorrow morning…" / a character walked out the door at the end / the scene faded on a private revelation):
     → You may open in a new scene/location naturally — but the new scene must be the DIRECT CONSEQUENCE of the prior one (next morning, next room over, character now confronting the one they decided to confront).

If you are unsure which case applies → assume case (1). Same-scene continuation is the default.

EXAMPLES — get this right:

  ✓ CORRECT continuation:
  Ep N ends: VIVIENNE walks into MARCUS'S STUDY. "Marcus. Who is this woman?"
  Ep N+1 opens: ИНТА. MARCUS'S STUDY — УТРО (CONTINUOUS). Vivienne in doorway, Claire frozen, Marcus standing.
  MARCUS: She's the new housekeeper. (его взгляд не отрывается от Клэр)

  ✗ WRONG continuation (the bug we are fixing):
  Ep N ends: VIVIENNE in study doorway: "Who is this woman?"
  Ep N+1 opens: ИНТА. VIVIENNE'S SITTING ROOM — УТРО. Vivienne summons Claire. ← SCENE TELEPORT. FORBIDDEN.

If your hook makes you write a different location or a "later" timestamp than where the previous episode ended → STOP. Rewrite to continue the scene. The teleport is the #1 short-drama failure mode.

━━━ HOOK (first 6–8 seconds) ━━━
Drop the viewer INTO something already in progress. No warm-up.

If continuing a prior scene (case 1 above) — the hook IS the immediate next beat of that scene. Do not "re-establish" anything. The viewer remembers; they were just here 60 seconds ago.

If opening a fresh scene (case 2 above) — pick a hook from the list below.

Hook types — rotate, never repeat the same type back to back:
  • CAUGHT IN THE ACT — someone is discovered doing the thing they swore they'd never do
  • THE CALM REVEAL — protagonist delivers devastating information with complete composure while the other person crumbles
  • POWER MOVE IN PROGRESS — protagonist is already executing a plan the antagonist didn't see coming
  • UNEXPECTED ARRIVAL — someone walks in who changes everything just by being there (use only when starting a fresh scene; if previous ep ended ON an arrival, you are in case 1 — continue, do not re-arrive)
  • ALREADY DECIDED — protagonist announces a decision that cannot be undone; antagonist has no move left

FORBIDDEN hooks: greetings, weather, narration, neutral questions, any line that could exist in a non-dramatic scene.
First spoken word must create immediate tension. 1–2 lines max.

━━━ BODY (~38–44 seconds) ━━━
MID-EPISODE REVERSAL IS MANDATORY. Mark with action line: [REVERSAL]

REVERSAL MECHANICS — pick one per episode, fit to the series genre:

  POWER FLIP REVERSALS (who has leverage):
  • STATUS EXPOSE — antagonist is humiliating someone they think is powerless; mid-scene it's revealed the "powerless" person owns/controls something the antagonist desperately needs
  • SECRET ALREADY KNOWN — antagonist delivers information they think is a weapon; protagonist reveals she's known for weeks and has already acted on it
  • THE RECORDING — a conversation or confession was recorded without the speaker's knowledge; it surfaces now
  • HIDDEN ALLY REVEALED — a character the antagonist thought was on their side is revealed to be working against them

  IDENTITY/INFORMATION REVERSALS:
  • WRONG PERSON — they've been targeting/threatening/manipulating the wrong individual the entire episode
  • THE DOCUMENT — a contract, will, test result, or transfer of ownership changes who holds power in one sentence
  • PREGNANCY LEVERAGE — if genre applies: a pregnancy (hidden or newly revealed) shifts every power dynamic in the scene
  • THE WITNESS — someone who "wasn't there" was there the whole time

  RELATIONSHIP REVERSALS:
  • ALLY TURNS — the character who seemed to be helping is revealed as the source of the threat
  • DOUBLE BETRAYAL — protagonist appears to accept betrayal; end of scene reveals she set the whole thing up
  • THE CHOICE FORCED — antagonist demands protagonist choose between two things she loves; she chooses a third option they didn't account for

Body rules:
- One escalation per scene — things get worse OR a new threat enters
- Max 2 locations (1 preferred). If you must change location, it must happen ONCE only and serve the reversal.
- Every line: reveals, wounds, threatens, or advances plot. Zero filler.
- FORBIDDEN: explaining feelings calmly, recapping past events, pauses for reflection
- Dialogue word budget for the WHOLE episode: 80–110 spoken words. Body itself ≈ 50–75 words.
- If you have a third location idea or a fourth speaking character — cut it. The episode is 60 seconds.

━━━ CLIFFHANGER (last 8–10 seconds) ━━━
End on the REACTION, not the action. Cut BEFORE resolution.

Cliffhanger types:
  • THE ARRIVAL — someone appears who changes everything (an enemy thought gone, an ally thought safe, a stranger with a file)
  • THE REVELATION — a fact is revealed that reframes everything the audience just watched
  • THE ULTIMATUM — a demand is issued with a deadline; episode ends before the answer
  • THE FALL — protagonist loses something irreversible; next episode starts from zero
  • THE ALLIANCE — protagonist accepts help from a dangerous or unexpected source; audience doesn't know the cost yet
  • SILENT POWER — protagonist does or says nothing, but the look on her face tells the audience she has already decided something terrible

━━━ ESCALATION LADDER (series-level) ━━━
Across the series, escalation must compound. Use this ladder — don't stay on one rung:
  Rung 1: Social humiliation
  Rung 2: Romantic betrayal
  Rung 3: Financial/professional threat
  Rung 4: Physical danger (threat, attempt on life)
  Rung 5: Total loss (everything taken)
  Rung 6: Rebuild with a powerful and morally ambiguous ally
  Rung 7: Final reckoning — protagonist now has more power than anyone who wronged her

Each episode should feel like it moved up at least half a rung.

━━━ DIALOGUE STYLE — SHORT DRAMA RULES ━━━
This is NOT a prestige TV show. This is NOT realistic. This IS deliberately over-the-top.

EVERY CHARACTER SAYS EXACTLY WHAT THEY MEAN AT MAXIMUM EMOTIONAL VOLUME.
There is no subtext. There is no nuance. There is only TEXT — stated out loud, directly, operatically.

Villain dialogue rules:
  • Villains state their contempt explicitly: "I can smell gold-diggers like you from a mile away."
  • Villains announce their evil logic: "You think love matters here? This family runs on money and bloodline."
  • Villains issue ultimatums as declarations: "Leave my son or I will destroy everything you have. And you have nothing."
  • Villains NEVER say anything reasonable or understandable. They are cartoonishly, satisfyingly awful.

Protagonist dialogue rules:
  • The wronged protagonist responds with either devastating SILENCE + one killer line, OR complete emotional collapse that the audience feels in their chest
  • When protagonist has power: she delivers it ice-cold, one sentence, zero explanation. "You may leave." Full stop.
  • When protagonist is powerless: she says exactly what she feels with no filter — the humiliation is total, the audience's sympathy is total.

EMOTIONAL DIAL — always at 8, 9, or 10 out of 10. Never below 7.

What this sounds like in practice:

  ✗ WRONG (realistic, cinematic, boring):
  MOTHER: I'm just concerned about what kind of future you two could have together.
  ELENA: I understand your concerns, but I care about your son very much.

  ✓ RIGHT (short drama, over-the-top, addictive):
  MOTHER: You're a waitress. You found my son to get your hands on his money.
  ELENA: You don't know me—
  MOTHER: I know exactly what you are. I can spot trash like you from across the room.
           Stay away from my son. Or I will make sure you regret the day you were born.

  ✓ RIGHT (protagonist with power, ice-cold):
  ELENA: (without looking up from her desk) You humiliated me in front of your entire family.
         I remember every word. — (finally looks up) — Your company's funding goes through me now.
         So. Was there something you wanted to say to me?

Every scene should feel like it belongs on a telenovela that has been turned up to maximum volume.

━━━ PHYSICAL ACTION IN CONFLICT SCENES — MANDATORY ━━━
Short drama lives on physical escalation — dialogue alone is static. Every conflict scene MUST contain at least one physical action beat written as an action line.

Required minimum: 1 physical beat per conflict scene (more is better).

Approved physical beats — rotate, match the emotional register:
  • [СЛЫШИТСЯ ХЛЁСТКИЙ ЗВУК — Vivian даёт Marcus пощёчину. Он не двигается.]
  • [Elena отшвыривает его руку и делает шаг назад.]
  • [Marcus хватает её за запястье прежде чем она уходит.]
  • [Claire швыряет стакан об стену рядом с ней — стекло разлетается.]
  • [Elena резко выбивает папку у него из рук — бумаги летят по полу.]
  • [Marcus встаёт из-за стола, медленно заходит к ней за спину.]
  • [Elena упирает руку ему в грудь, не давая пройти.]
  • [Vivian хватает её за подбородок, заставляя смотреть в глаза.]
  • [Marcus разворачивает её к себе за плечо.]
  • [Elena отступает к стене — его рука бьёт по стене рядом с её головой.]

Physical beat rules:
  ✓ Write the action in [square brackets] — it IS a filmable action line
  ✓ The beat should come at a peak moment of confrontation, not randomly
  ✓ Escalate across the series: slap in ep 3 < grab in ep 7 < full physical struggle in ep 15
  ✗ FORBIDDEN: "conversation about violence" instead of actual violence ("He threatened to hurt her" as dialogue — show it physically instead)
  ✗ FORBIDDEN: fight scenes that take >2 action lines — this is a 60-second episode, not an action film

━━━ DIALOGUE-FIRST RULE — HARD BAN ON PAPERWORK ━━━
This is short drama for vertical video. EVERYTHING must be revealed through SPOKEN DIALOGUE between living people on screen.

HARD-BANNED devices (do NOT use them at all):
  ✗ letters, hand-written notes, printed pages
  ✗ documents, contracts, files, folders, dossiers being read on screen
  ✗ text messages / SMS / WhatsApp / chat bubbles displayed to the camera
  ✗ emails, on-screen UI, computer screens being read aloud
  ✗ photographs handed over silently as the "reveal"
  ✗ diary entries, journals, voiceover narration
  ✗ newspaper headlines, TV news chyrons, radio reports
  ✗ flashbacks shown as silent montage
  ✗ any "character reads X aloud while alone" moment

If a fact must surface, a CHARACTER says it OUT LOUD to another character — preferably as an accusation, threat, taunt, or confession in conflict.

NARROW exception (use at most ONCE per episode, and only if it is the ONLY way):
  • A short physical object (e.g. a single ring, a pregnancy test, a key, a photo) can be SHOWN for 1–2 seconds as a silent shock — but a character must immediately react and verbalize the meaning ("That's HER ring." / "You knew. You always knew.").
  • A single short note ≤ 6 words is permissible only if the entire dramatic punch hinges on those exact words (e.g. "I know what you did."). One per episode max. Never use when dialogue could carry the same beat.

If you catch yourself writing "[X reads the letter]" or "[Y opens the file]" — DELETE it and replace with a face-to-face confrontation where the same information lands as spoken accusation.

FORMAT RULES:
- Scene headings: INT./EXT. LOCATION — DAY/NIGHT (max 3 words)
- Action lines: [square brackets], max 1 line, max 5 per episode, filmable in 2 seconds
- Parentheticals: max 1 per speaking block, only when tone is completely non-obvious
- NO INTERRUPTIONS — HARD BAN: NEVER cut a character's line mid-sentence with a dash (—). Every line must be a complete sentence. FORBIDDEN patterns:
    ✗  ELENA: You should have told me—
    ✗  MARCUS: (перебивает) I don't want to hear—
    ✗  VIVIAN: You have no right to be—
  WHY: the video generator renders interrupted lines as two people speaking simultaneously — it looks broken on screen.
  INSTEAD: let each character finish their thought. Interruption = a new action line + the other character's complete line.
    ✓  ELENA: You should have told me the truth.
       [Marcus резко встаёт, не давая ей договорить.]
       MARCUS: I owe you nothing.

After the script add:
━━━━━━━━━━━━━━━━━━━━━━━
EPISODE NOTES
Hook type: [which hook type from the list above]
Reversal type: [which reversal mechanic from the list above]
Cliffhanger type: [which cliffhanger type from the list above]
Escalation rung: [current rung number and what changed]
Spoken word count: [number — must be ≤110]
Estimated runtime: [seconds — must be ≤60. Calculate as spoken_words / 2.25 + (action_beats × 1.5) + (pauses × 0.7)]
Setup for next episode: [one sentence — what is now in motion]
━━━━━━━━━━━━━━━━━━━━━━━

If the estimated runtime exceeds 60 seconds — DELETE beats and re-output. Do not submit a draft over budget.

OUTPUT ONLY the cast block + script + episode notes. No JSON, no extra commentary."""


# ════════════════════════════════════════════════════════════════════════════
# BATCH MODE — write {batch_size} consecutive 60-second episodes as ONE flowing
# script. Each sub-episode ends on its own cliffhanger; the chunk overall plays
# as a 5-minute mini-story with continuous tension. Cut markers tell the
# splitter where each TikTok/Reel ends.
# ════════════════════════════════════════════════════════════════════════════
_BATCH_SCRIPT_SYSTEM = """You are a professional screenwriter for short-form drama series (TikTok/Reels) writing a CHUNK of {N} consecutive 60-second episodes as ONE flowing script.

═══════════════════════════════════════
THE FORMAT — READ CAREFULLY
═══════════════════════════════════════
You are NOT writing one long episode. You are NOT writing five separate shorts.
You are writing a CONTINUOUS NARRATIVE that, when cut at marked points, produces {N} stand-alone 60-second TikToks each ending on its own cliffhanger.

Total chunk length: {N} × ~60 sec = ~{TOTAL} seconds of finished video.
Total spoken word budget: ~{TOTAL_WORDS} English words (range {WMIN}–{WMAX}).
Total locations: 2–4 (NEVER more — use them across the whole chunk).
Total speaking characters: 3–6.

═══════════════════════════════════════
LANGUAGE RULES — NON-NEGOTIABLE
═══════════════════════════════════════
- DIALOGUE: English only
- ACTION LINES: Russian (описания, ремарки, реакции)
- SCENE HEADINGS: location PART in ENGLISH (e.g. "ИНТА. HOTEL ROOM — УТРО"). No Russian/Cyrillic location names.
- Character names in dialogue cues: ALL CAPS, exact spelling as given

MANDATORY — start the chunk with this EXACT cast block (covers ALL characters appearing in any sub-episode):
=== EPISODE CAST ===
CHARACTER: [name] | OUTFIT: [outfit_label] | OUTFIT_DESC: [garments + colors]
=== END CAST ===

Same cast-block rules as for single episodes (gender, look, IS_BASE for default looks, OUTFIT_DESC for new outfits).

═══════════════════════════════════════
CUT MARKERS — REQUIRED
═══════════════════════════════════════
Every sub-episode ends with this EXACT marker on its own line:
═══ END EPISODE {{X}}/{{N}} — CLIFFHANGER: {{cliff type}} ═══

After the LAST sub-episode, no more script — go straight to the CHUNK NOTES block.

═══════════════════════════════════════
THE GOLDEN RULE — {N} CLIFFHANGERS, ESCALATING
═══════════════════════════════════════
Sub-episode 1: opens the chunk with the chunk's hook → ends on cliffhanger 1 (smallest, but still a hook)
Sub-episode 2: picks up from cliff 1 → escalates → ends on cliffhanger 2 (bigger)
Sub-episode 3: midpoint REVERSAL of the chunk's main premise → ends on cliffhanger 3
Sub-episode 4: consequences cascade → ends on cliffhanger 4 (almost a finale)
Sub-episode {N}: chunk-finale beat → ends on cliffhanger {N} (BIGGEST — leads into next chunk)

Each sub-episode must independently satisfy SHOCK → micro-development → CLIFFHANGER.
The chunk overall must satisfy: CHUNK HOOK → CHUNK REVERSAL (around sub-ep 3) → CHUNK CLIFFHANGER (end of sub-ep {N}).

Per sub-episode budget:
- ~{PER_WORDS} spoken words
- 6–10 dialogue lines
- 2–4 action beats
- 1 location preferred (chunk total: 2–4)
- exactly ONE cut-cliffhanger at the end

Mark the CHUNK midpoint REVERSAL with action line: [REVERSAL]

═══════════════════════════════════════
SCENE CONTINUATION ACROSS CUT MARKERS — MANDATORY
═══════════════════════════════════════
The cut markers between sub-episodes are EDITS INSIDE A SCENE, not scene transitions.

If sub-episode X ends with someone arriving / a question hanging / two characters mid-confrontation / a reaction shot — sub-episode X+1 opens IN THE SAME SCENE: same location, same characters, same time. The first line of X+1 is the direct answer/next beat of the line that closed X.

FORBIDDEN: characters teleporting between sub-episodes (e.g. ending sub-ep 2 with Vivian in the study doorway, then opening sub-ep 3 with Vivian summoning the maid to a different room — that scene was never finished, it cannot be skipped).

Allowed scene change between sub-episodes ONLY when the previous sub-ep genuinely closed its scene (private decision / character left / time-jump cliffhanger). Default assumption: continue the scene.

The same goes for the boundary between THIS chunk and the PREVIOUS chunk — read the previous chunk's last sub-episode and continue its scene if it was left open mid-confrontation.

═══════════════════════════════════════
HOOK / DIALOGUE / CLIFFHANGER STYLE — same as single-episode mode
═══════════════════════════════════════
- Open IN action — no greetings, no warm-ups, no weather, no "good morning"
- Every line: reveals, wounds, threatens, or advances plot. Zero filler.
- Villains: cartoonishly awful, state contempt explicitly
- Protagonist: ice-cold one-liners when in power, total emotional collapse when powerless
- Emotional dial: 8–10/10, never below 7
- End each sub-episode on REACTION, not action — cut before resolution
- Cliffhanger types (rotate, never repeat back-to-back): ARRIVAL, REVELATION, ULTIMATUM, FALL, ALLIANCE, SILENT POWER, RECORDING SURFACES, WRONG PERSON, SECRET ALREADY KNOWN
- PHYSICAL ACTION IN CONFLICT SCENES — MANDATORY: every conflict scene must contain at least 1 physical action beat in [brackets]. Slap, grab, push, object thrown, arm blocked — rotate and escalate across the chunk. Pure dialogue confrontations without a physical beat are static and flat.
- NO INTERRUPTIONS — HARD BAN: NEVER cut a line mid-sentence with a dash (—). Every spoken line is a complete sentence. FORBIDDEN: "ELENA: You should have—" or "(перебивает)". The video generator renders cut lines as two people talking at once — it looks broken. Instead: complete the line, then use an action beat to show the interruption physically.

═══════════════════════════════════════
DIALOGUE-FIRST RULE — HARD BAN ON PAPERWORK
═══════════════════════════════════════
EVERYTHING must be revealed through SPOKEN DIALOGUE between living people on screen.

HARD-BANNED devices across the WHOLE chunk:
  ✗ letters, notes, documents, contracts, files, dossiers being read on screen
  ✗ text messages / SMS / WhatsApp / chat bubbles displayed to the camera
  ✗ emails, on-screen UI, computer screens, phone screens being read aloud
  ✗ photographs handed over silently as a "reveal"
  ✗ diary entries, voiceover, narration
  ✗ newspaper headlines, news chyrons, radio reports
  ✗ flashbacks as silent montage
  ✗ any "character reads X aloud while alone" beat

Reveals = spoken confrontations. A fact surfaces because someone ACCUSES, THREATENS, TAUNTS, or CONFESSES it out loud in front of another character.

NARROW exception (max ONCE per chunk, not per sub-episode):
  • A physical object (ring, pregnancy test, key, photo) can be shown silently for 1–2s only if a character immediately reacts and verbalizes the meaning.
  • A single short note ≤ 6 words is permissible only if the entire punch hinges on those exact words. Never when dialogue could carry the same beat.

If you find yourself writing "[reads the letter]" / "[opens the file]" / "[texts back]" — DELETE it and rewrite as a face-to-face confrontation.

═══════════════════════════════════════
FORMAT RULES
═══════════════════════════════════════
- Scene headings: ИНТА./ЭКСТ. LOCATION — DAY/NIGHT (location max 3 words, ENGLISH)
- Action lines: [square brackets], max 1 line each, max 5 per sub-episode (≤25 in whole chunk), filmable in 2 seconds
- Parentheticals: max 1 per speaking block, only when tone non-obvious
- NO scene-bridging narration. Cut hard between scenes.
- NO INTERRUPTIONS: every spoken line is a complete sentence — no mid-sentence dashes (—). Show interruption via action line, not a cut line.

═══════════════════════════════════════
AFTER THE LAST CUT MARKER, output this CHUNK NOTES block (Russian):
═══════════════════════════════════════
━━━━━━━━━━━━━━━━━━━━━━━
CHUNK NOTES
Chunk hook type: [type]
Chunk reversal type: [type, in which sub-episode]
Cliffhangers per sub-episode:
  Ep {{X1}}: [type — one-line description]
  Ep {{X2}}: [type — one-line description]
  ...
  Ep {{XN}}: [type — one-line description]
Escalation rung path: [e.g. 2→2→3→4→4]
Total spoken word count: [number — must be ≤{WMAX}]
Estimated runtime: [seconds — must be ≤{TOTAL}+10. Use spoken_words/2.25 + (action_beats × 1.5)]
Setup for next chunk: [one sentence]
━━━━━━━━━━━━━━━━━━━━━━━

If runtime exceeds budget — DELETE beats and re-output. Never submit over budget.

OUTPUT ONLY the cast block + script (with cut markers) + chunk notes. No JSON, no extra commentary."""


def _build_batch_script_system(s):
    """Render the batch system prompt with N filled in from series.batch_size."""
    N = batch_size(s) or 5
    total_sec = N * 60
    per_words = 95
    total_words = N * per_words
    return _BATCH_SCRIPT_SYSTEM.format(
        N=N, TOTAL=total_sec,
        TOTAL_WORDS=total_words, WMIN=int(total_words * 0.85), WMAX=int(total_words * 1.15),
        PER_WORDS=per_words,
    )


@app.route('/api/series/<sid>/generate-arcs', methods=['POST'])
def generate_arcs(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    synopsis = s.get('synopsis', '')
    if not synopsis:
        return jsonify({'error': 'Сначала создай синопсис сериала'}), 400
    prompt = (
        f'Series: "{s["title"]}"\nSynopsis: "{synopsis}"\n\n'
        "Generate exactly 3 different arc variants for this 70-episode short drama for TikTok/Reels. "
        "Each arc describes: setup (ep 1-15), escalation (ep 15-55), climax & resolution (ep 55-70). "
        "Make each variant meaningfully different in twists and ending. "
        'Return JSON: {"arcs": [{"title": "...", "summary": "2-3 paragraphs"}, ...]}'
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
        arcs = data.get('arcs', data)
        s['arc_variants'] = arcs
        save_series(sid, s)
        return jsonify(arcs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/confirm-arc', methods=['POST'])
def confirm_arc(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    arc = (request.json or {}).get('arc', '').strip()
    if not arc:
        return jsonify({'error': 'Выбери или напиши арку'}), 400
    s['arc'] = arc
    s['stage'] = 2
    s.pop('arc_variants', None)
    save_series(sid, s)
    return jsonify(s)


@app.route('/api/series/<sid>/generate-milestones', methods=['POST'])
def generate_milestones(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    # Auto-generate the three anchor points. In single mode → eps 1/10/70.
    # In batch mode → chunks 1 / chunk-of-10 / last (e.g. for bs=5: chunks 1, 2, 14).
    synopsis = s.get('synopsis', '')
    arc = s.get('arc', '')
    batch = is_batch_mode(s)
    bs = batch_size(s)
    a1, a2, a3 = anchor_chunks(s) if batch else (1, 10, TOTAL_SUB_EPS)
    cast_pin = _canonical_cast_block(s)
    if batch:
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{synopsis}"\nArc: "{arc}".\n\n'
            + cast_pin +
            f'BATCH MODE: each storage chunk = {bs} consecutive ~1-min sub-episodes (~{bs*60}s total = ~{bs} min). '
            f'Total chunks = {chunk_count(s)}. There are NO standalone episodes in the UI — only chunks.\n'
            f'Write synopsis (4–6 sentences) for THREE anchor chunks: {a1} (covers sub-eps {chunk_range(s,a1)[0]}–{chunk_range(s,a1)[1]}), '
            f'{a2} (sub-eps {chunk_range(s,a2)[0]}–{chunk_range(s,a2)[1]}), '
            f'{a3} (sub-eps {chunk_range(s,a3)[0]}–{chunk_range(s,a3)[1]}).\n'
            f'Chunk {a1} — premiere chunk: hooks with immediate stakes, contains {bs} mini-cliffhangers, ends locking the viewer in.\n'
            f'Chunk {a2} — first major turning point: the situation viewers thought they understood gets completely flipped. Mid-series reversal.\n'
            f'Chunk {a3} — finale chunk: maximum stakes, all threads converge in the last {bs} sub-episodes.\n'
            'SHORT DRAMA: each synopsis must outline the chunk\'s 5 mini-cliffhangers + main midpoint reversal + chunk-end cliffhanger.\n'
            f'Return JSON: {{"milestones": {{"{a1}": "...", "{a2}": "...", "{a3}": "..."}}}}'
        )
    else:
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{synopsis}"\nArc: "{arc}".\n\n'
            + cast_pin +
            "Write a synopsis (2-4 sentences) for exactly THREE anchor episodes: 1, 10, and 70.\n"
            "Ep 1 — series premiere: hooks with immediate stakes, establishes the central conflict and main character, ends on a cliffhanger that locks the viewer in.\n"
            "Ep 10 — first major turning point: the situation the viewer thought they understood gets completely flipped. A secret explodes or a power shift happens that changes everything.\n"
            "Ep 70 — finale: maximum stakes, all threads converge, the central conflict resolves (or deliberately doesn't). Must feel earned.\n"
            "SHORT DRAMA FORMAT: each synopsis must include a reversal and end on the biggest cliffhanger possible for that point in the series. No slow burns.\n"
            'Return JSON: {"milestones": {"1": "...", "10": "...", "70": "..."}}'
        )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
        milestones = data.get('milestones', {})
        s.setdefault('milestone_synopses', {}).update({str(k): v for k, v in milestones.items()})
        save_series(sid, s)
        return jsonify(milestones)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/milestones/<int:ep_num>', methods=['PUT'])
def update_milestone(sid, ep_num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    s.setdefault('milestone_synopses', {})[str(ep_num)] = (request.json or {}).get('synopsis', '')
    save_series(sid, s)
    return jsonify({'ok': True})


@app.route('/api/series/<sid>/milestones/<int:ep_num>/regenerate', methods=['POST'])
def regenerate_milestone(sid, ep_num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ms = s.get('milestone_synopses', {})
    batch = is_batch_mode(s)
    bs = batch_size(s)
    unit = (lambda k: f'Chunk {k} (sub-eps {chunk_range(s, int(k))[0]}–{chunk_range(s, int(k))[1]})') if batch else (lambda k: f'Ep {k}')
    prev = '\n'.join(f'{unit(k)}: {v}' for k, v in sorted(ms.items(), key=lambda x: int(x[0])) if int(k) < ep_num)
    nxt  = '\n'.join(f'{unit(k)}: {v}' for k, v in sorted(ms.items(), key=lambda x: int(x[0])) if int(k) > ep_num)
    target_label = unit(ep_num)
    extra = (
        f' This is a CHUNK of {bs} consecutive ~1-min sub-episodes — outline the {bs} mini-cliffhangers + midpoint reversal + chunk-end cliffhanger.'
        if batch else ''
    )
    prompt = (
        f'Series: "{s["title"]}" | Arc: "{s.get("arc","")}".\n'
        + _canonical_cast_block(s) +
        f'Previous milestones:\n{prev or "—"}\n'
        f'Next milestones:\n{nxt or "—"}\n\n'
        f'Write a new synopsis (4-6 sentences for chunks, 2-4 for episodes) for {target_label} that fits logically between the above.{extra} '
        'SHORT DRAMA FORMAT: open in conflict (not setup), include a mid-point reversal, end on a cliffhanger. '
        'Return JSON: {"synopsis": "..."}'
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
        syn = data.get('synopsis', '')
        s.setdefault('milestone_synopses', {})[str(ep_num)] = syn
        save_series(sid, s)
        return jsonify({'synopsis': syn})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/extract-from-story', methods=['POST'])
def extract_from_story(sid):
    """Extract characters and locations from synopsis/arc/milestones and create them in the series."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404

    ms = s.get('milestone_synopses', {})
    ms_text = '\n'.join(f'Ep {k}: {v}' for k, v in sorted(ms.items(), key=lambda x: int(x[0])) if v)
    story_text = '\n\n'.join(filter(None, [
        f'Synopsis: {s.get("synopsis","")}',
        f'Arc: {s.get("arc","")}',
        f'Milestone synopses:\n{ms_text}' if ms_text else '',
    ]))

    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
        f'{story_text}\n\n'
        'Extract all NAMED characters and specific locations from this story. '
        'For each character infer: name, gender (male/female), a 1-sentence description of their role, '
        'and a 1-sentence appearance description (for AI image generation). '
        'For each location infer: name and a 1-sentence description. '
        'CRITICAL — character "name" AND location "name" fields MUST be in ENGLISH (e.g. "Hotel Room", '
        '"Base HQ Office", "Boardroom", "Penthouse"). FORBIDDEN: Russian/Cyrillic names like '
        '"Гостиничный номер", "Военная база" — translate to English. The "description" field stays Russian. '
        'Only include characters and locations that are meaningfully present — no extras. '
        'Return JSON:\n'
        '{"characters": [{"name":"...","gender":"female","description":"...","appearance":"..."},...], '
        '"locations": [{"name":"...","description":"..."},...] }'
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    existing_char_names = {c['name'].lower() for c in s.get('characters', [])}
    existing_loc_names  = {l['name'].lower() for l in s.get('locations',  [])}
    added_chars, added_locs = [], []

    for c in data.get('characters', []):
        if c.get('name','').lower() not in existing_char_names:
            char = {
                'id': str(uuid.uuid4())[:8],
                'name': c['name'],
                'description': c.get('description', ''),
                'appearance':  c.get('appearance', ''),
                'gender':      c.get('gender', 'female'),
                'voice_id':    '',
                'ref_images':  [],
                'outfits':     [],
            }
            s.setdefault('characters', []).append(char)
            added_chars.append(char['name'])

    for l in data.get('locations', []):
        if l.get('name','').lower() not in existing_loc_names:
            loc = {
                'id': str(uuid.uuid4())[:8],
                'name': l['name'],
                'description': l.get('description', ''),
                'ref_images':  [],
            }
            s.setdefault('locations', []).append(loc)
            added_locs.append(loc['name'])

    save_series(sid, s)
    # Don't trigger autogen here. The frontend acceptScript flow shows a modal
    # FIRST so the user can drag photos / mark «🚫 Не генерить» / pick custom
    # styles. Frontend kicks off autogen explicitly via /auto-generate/sweep
    # in _proceedAfterAccept after the modal closes. Auto-firing here meant
    # generation started BEFORE the user even saw the modal, defeating the
    # whole point.
    return jsonify({
        'added_characters': added_chars,
        'added_locations':  added_locs,
        'series': s,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/script-history', methods=['GET'])
def list_script_history(sid, num):
    """List archived previous versions of an episode's script."""
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    history = ep.get('script_history', []) or []
    cur = (ep.get('script') or '')
    return jsonify({
        'current': {
            'length': len(cur),
            'preview': cur[:300],
        },
        'versions': [
            {
                'index': i,
                'ts': h.get('ts'),
                'reason': h.get('reason', '?'),
                'length': len(h.get('script', '')),
                'preview': (h.get('script') or '')[:300],
            }
            for i, h in enumerate(history)
        ],
    })


@app.route('/api/series/<sid>/episodes/<int:num>/script-history/<int:idx>', methods=['GET'])
def get_script_history_full(sid, num, idx):
    """Return the full text of one archived version (for preview before restore)."""
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    history = ep.get('script_history', []) or []
    if idx < 0 or idx >= len(history):
        return jsonify({'error': 'index out of range'}), 400
    h = history[idx]
    return jsonify({
        'index': idx,
        'ts': h.get('ts'),
        'reason': h.get('reason', '?'),
        'script': h.get('script', ''),
    })


@app.route('/api/series/<sid>/episodes/<int:num>/script-restore/<int:idx>', methods=['POST'])
def restore_script_version(sid, num, idx):
    """Restore a previous version. The current script is archived as a new history entry
    so the restore itself is reversible."""
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    history = ep.get('script_history', []) or []
    if idx < 0 or idx >= len(history):
        return jsonify({'error': 'index out of range'}), 400

    # Save current as new history entry before swapping
    cur_script = (ep.get('script') or '').strip()
    if cur_script:
        history.append({
            'ts': time.time(),
            'reason': 'pre-restore',
            'script': cur_script[:80000],
        })
    # Pop the chosen version → make it current
    chosen = history.pop(idx)
    ep['script'] = chosen.get('script', '')
    ep['script_history'] = history[-10:]
    save_episode(sid, num, ep)
    return jsonify({
        'restored_from': {
            'ts': chosen.get('ts'),
            'reason': chosen.get('reason'),
        },
        'script': ep['script'],
    })


@app.route('/api/series/<sid>/episodes/<int:num>/doctor-script', methods=['POST'])
def doctor_episode_script(sid, num):
    """Run the logic-hole auditor on the current script and apply minimal fixes.
    Two-stage: AUDIT (find holes) → DOCTOR (rewrite with surgical edits).
    Returns either the fixed script (and saves it) or — if user passed dry_run —
    just the violations list so they can review before applying."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'episode not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Сценарий пустой — нечего лечить'}), 400

    body = request.json or {}
    dry_run = bool(body.get('dry_run'))
    extra_notes = (body.get('extra_notes') or '').strip()  # user can pass own pain points
    # Optional: caller passes a pre-selected list of violations (e.g. user ticked
    # a subset of issues from the inline checker UI) — skip re-auditing in that case.
    selected_violations = body.get('violations')

    if isinstance(selected_violations, list) and not dry_run:
        violations = [v for v in selected_violations if isinstance(v, dict)]
        report = {'passes': not violations, 'violations': violations}
        critical = [v for v in violations if v.get('severity', 'critical') == 'critical']
        minor    = [v for v in violations if v.get('severity') != 'critical']
    else:
        # Stage 1: audit (with previous-episode context — see audit_logic_holes)
        report = audit_logic_holes(sid, num, script)
        violations = report.get('violations', [])
        critical = [v for v in violations if v.get('severity') == 'critical']
        minor    = [v for v in violations if v.get('severity') != 'critical']

    if dry_run:
        return jsonify({
            'violations': violations,
            'critical_count': len(critical),
            'minor_count': len(minor),
            'audit_error': report.get('audit_error'),
        })

    # Optionally inject user's extra notes as additional violations to fix
    if extra_notes:
        violations.append({
            'type': 'user_note',
            'severity': 'critical',
            'where': 'user-specified',
            'explanation': extra_notes,
            'fix': 'apply the user\'s requested change',
        })

    if not violations:
        return jsonify({
            'changed': False,
            'message': 'Логических дыр не найдено — сценарий чист.',
            'violations': [],
        })

    # Stage 2: doctor
    try:
        fixed = doctor_script(sid, num, script, violations).strip()
    except Exception as e:
        return jsonify({'error': f'Doctor failed: {e}', 'violations': violations}), 500

    # Save backup of previous script + new version
    ep.setdefault('script_history', []).append({
        'ts': time.time(),
        'reason': 'doctor-script',
        'script': script[:50000],  # cap
    })
    ep['script_history'] = ep['script_history'][-10:]
    ep['script'] = fixed
    ep['last_doctor_run'] = {
        'ts': time.time(),
        'violations_fixed': len(violations),
        'violations': violations,
    }
    save_episode(sid, num, ep)

    return jsonify({
        'changed': True,
        'script': fixed,
        'violations_fixed': len(violations),
        'violations': violations,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/extract-characters', methods=['POST'])
def extract_characters_from_script(sid, num):
    """Extract characters AND locations from the script, AND re-verify which already-known
    characters/locations are actually present in this episode's script (so dropped ones
    get unticked from the episode's checkboxes). Three things happen:
      1) Add brand-new speaking characters not yet in the series
      2) Add brand-new locations not yet in the series
      3) Rebuild ep['characters_used'] and ep['locations_used'] strictly from what
         the current script mentions (drops stale ticks)
    """
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'episode not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Сценарий пустой — сначала впиши или сгенерируй текст'}), 400

    existing_chars = s.get('characters', [])
    existing_locs  = s.get('locations', [])
    existing_char_lines = '\n'.join(
        f'  - {c["name"]} (id={c["id"]}) — appearance: {c.get("appearance","")[:160]}'
        for c in existing_chars
    ) or '  (none)'
    existing_loc_lines  = '\n'.join(f'  - {l["name"]} (id={l["id"]})' for l in existing_locs)  or '  (none)'

    director_notes = (ep.get('notes') or '').strip()
    notes_block = (
        f'DIRECTOR\'S NOTES (creative instructions from the user — TREAT AS HIGH PRIORITY):\n{director_notes}\n\n'
        if director_notes else ''
    )

    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
        f'ALREADY KNOWN CHARACTERS in this series:\n{existing_char_lines}\n\n'
        f'ALREADY KNOWN LOCATIONS in this series:\n{existing_loc_lines}\n\n'
        f'{notes_block}'
        f'EPISODE {num} SCRIPT:\n{script}\n\n'
        f'ALREADY KNOWN ITEMS in this series (story-relevant props):\n'
        + '\n'.join(f"  - {it.get('name')} (id={it.get('id')})" for it in (s.get('items') or []))
        + ('\n  (none)\n' if not (s.get('items') or []) else '\n') + '\n'
        + 'YOUR JOB — return six lists in JSON:\n\n'
        '1) `present_character_ids` — IDs of ALREADY KNOWN characters who actually appear in this script '
        '(speak or are explicitly on screen). Drop any known character not in the script.\n'
        '2) `present_location_ids` — IDs of ALREADY KNOWN locations actually used as scene settings in this script.\n'
        '3) `new_characters` — speaking characters that are NOT in the known list and need to be created. '
        'Include characters with proper names (Emma, Daniel...) AND characters labeled by ROLE who have at least '
        'one dialogue line (Mother, Father, Doctor, Driver, Maid, Butler, Boss, Lawyer, Reporter...). Use the '
        'role label as the "name" field. EXCLUDE silent background extras and unlabeled phone voices.\n'
        '   ALSO: if the DIRECTOR\'S NOTES explicitly say a known character should be re-cast as a NEW separate '
        '   entry (e.g. "create a separate character entry for the new face"), add them here with a clearly '
        '   distinct name (e.g. "Emma_NewFace", "Daniel_v2").\n'
        '4) `new_locations` — distinct scene settings in the script that are NOT in the known list and need '
        'to be created. Locations come from scene headings (INT./EXT. NAME — DAY/NIGHT). Each unique location = one entry.\n'
        '5) `appearance_updates` — ONLY when DIRECTOR\'S NOTES explicitly say an EXISTING character\'s '
        '   appearance changes in-story (plastic surgery, recast, scar, drastic transformation, new face). '
        '   For each: provide the existing character\'s `id` from the known list, a NEW `appearance` sentence '
        '   in Russian (full physical description, since this REPLACES the old one — do NOT just describe the '
        '   change), and a 1-sentence `reason` in Russian explaining what the notes asked for. '
        '   When in doubt, leave this list empty. Do NOT update appearance based on script alone — only on '
        '   explicit director\'s notes. The old portrait will be discarded and regenerated from this new text.\n'
        '6) `new_items` — STORY-RELEVANT props that drive the plot and should have a generated reference image. '
        'STRICT criteria — include ONLY:\n'
        '   - Objects mentioned by name with PLOT significance (the murder weapon, the heroine\'s locket, '
        '     the will document, the flash drive, the briefcase of money, the sword, the talisman, '
        '     the blackmail letter, the photograph, the bottle of poison, evidence files)\n'
        '   - Objects characters fight over, hide, exchange, destroy, or treat as evidence\n'
        '   - Objects that recur across scenes/episodes\n'
        '   EXCLUDE casual everyday objects (cup, phone, keys, glass, generic chair) UNLESS they have explicit plot weight.\n'
        '   For each NEW ITEM infer:\n'
        '   - name: short concrete English noun phrase (e.g. "Manila Folder", "Black Silver Shard", "Locket of Sarah")\n'
        '   - description: 1 sentence in RUSSIAN — what it looks like + its narrative role\n\n'
        'For each NEW CHARACTER infer:\n'
        '  - name: exact label as it appears in the script (English / Latin letters)\n'
        '  - gender: "male" or "female"\n'
        '  - description: 1 sentence in RUSSIAN about their role in the story\n'
        '  - appearance: 1 detailed sentence in RUSSIAN describing physical look (age, hair, eyes, build, '
        'style of dress) suitable as a prompt for AI image generation. Be specific.\n\n'
        'For each NEW LOCATION infer:\n'
        '  - name: short ENGLISH label as it appears in scene heading (e.g. "Hotel Suite", "Boardroom", "Hospital Corridor")\n'
        '  - description: 1 sentence in RUSSIAN about the place + atmosphere relevant to the scene\n\n'
        'Return JSON only:\n'
        '{\n'
        '  "present_character_ids": ["id1", "id2"],\n'
        '  "present_location_ids":  ["id3"],\n'
        '  "new_characters": [{"name":"...","gender":"female","description":"...","appearance":"..."}],\n'
        '  "new_locations":  [{"name":"...","description":"..."}],\n'
        '  "appearance_updates": [{"id":"existingId","appearance":"...","reason":"..."}],\n'
        '  "new_items":      [{"name":"...","description":"..."}]\n'
        '}'
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
    except Exception as e:
        return jsonify({'error': f'Не удалось разобрать ответ Claude: {e}'}), 500

    # 1) Add brand-new characters
    existing_char_names = {c['name'].lower() for c in existing_chars}
    valid_char_ids = {c['id'] for c in existing_chars}
    added_chars = []
    for c in data.get('new_characters', []):
        nm = (c.get('name') or '').strip()
        if not nm or nm.lower() in existing_char_names:
            continue
        new_id = str(uuid.uuid4())[:8]
        char = {
            'id': new_id,
            'name': nm,
            'description': c.get('description', ''),
            'appearance':  c.get('appearance', ''),
            'gender':      c.get('gender', 'female'),
            'voice_id':    '',
            'ref_images':  [],
            'outfits':     [],
        }
        s.setdefault('characters', []).append(char)
        added_chars.append(nm)
        existing_char_names.add(nm.lower())
        valid_char_ids.add(new_id)

    # 2) Add brand-new locations
    existing_loc_names = {l['name'].lower() for l in existing_locs}
    valid_loc_ids = {l['id'] for l in existing_locs}
    added_locs = []
    for l in data.get('new_locations', []):
        nm = (l.get('name') or '').strip()
        if not nm or nm.lower() in existing_loc_names:
            continue
        new_id = str(uuid.uuid4())[:8]
        loc = {
            'id': new_id,
            'name': nm,
            'description': l.get('description', ''),
            'ref_images':  [],
        }
        s.setdefault('locations', []).append(loc)
        added_locs.append(nm)
        existing_loc_names.add(nm.lower())
        valid_loc_ids.add(new_id)

    # 2.6) Add brand-new story-relevant items
    existing_item_names = {(it.get('name') or '').lower() for it in (s.get('items') or [])}
    added_items = []
    for it in (data.get('new_items') or []):
        nm = (it.get('name') or '').strip()
        if not nm or nm.lower() in existing_item_names:
            continue
        new_id = str(uuid.uuid4())[:8]
        item = {
            'id': new_id,
            'name': nm,
            'description': it.get('description', ''),
            'ref_images':  [],
        }
        s.setdefault('items', []).append(item)
        added_items.append(nm)
        existing_item_names.add(nm.lower())

    # 2.5) Appearance updates — director's notes asked to refresh an existing
    #      character's look (new face, recast, scar, etc.). We replace the
    #      `appearance` text and clear the generated portrait + outfit images so
    #      autogen rebuilds them from the new description.
    appearance_updates = []
    char_by_id = {c['id']: c for c in s.get('characters', [])}
    for upd in (data.get('appearance_updates') or []):
        cid = (upd.get('id') or '').strip()
        new_appearance = (upd.get('appearance') or '').strip()
        reason = (upd.get('reason') or '').strip()
        if not cid or cid not in char_by_id or not new_appearance:
            continue
        char = char_by_id[cid]
        old_appearance = char.get('appearance', '')
        char['appearance'] = new_appearance
        # Wipe portrait so autogen regenerates it
        char['ref_images'] = []
        # Wipe outfit reference images too — they were generated against the old face
        for o in char.get('outfits', []) or []:
            o['ref_images'] = []
        appearance_updates.append({
            'id': cid,
            'name': char.get('name', cid),
            'previous': old_appearance,
            'new': new_appearance,
            'reason': reason,
        })

    save_series(sid, s)

    # 3) Rebuild episode's characters_used / locations_used. Take Claude's "present" lists,
    #    union with newly-added (which by definition appear in the script), and intersect
    #    with the post-add valid id set (so we never store ghost ids).
    present_char_ids = set(data.get('present_character_ids', []))
    present_loc_ids  = set(data.get('present_location_ids', []))
    # Newly-added entities appear in the script — include them
    new_char_id_set = {c['id'] for c in s['characters'] if c['name'] in added_chars}
    new_loc_id_set  = {l['id'] for l in s['locations']  if l['name'] in added_locs}
    final_chars = sorted((present_char_ids | new_char_id_set) & valid_char_ids)
    final_locs  = sorted((present_loc_ids  | new_loc_id_set)  & valid_loc_ids)

    prev_chars = set(ep.get('characters_used') or [])
    prev_locs  = set(ep.get('locations_used')  or [])
    ep['characters_used'] = final_chars
    ep['locations_used']  = final_locs
    # Mark cast extraction as user-confirmed — auto-heal paths can now safely
    # re-sync this episode's cast block (gated until user pressed the button).
    ep['cast_extracted'] = True
    save_episode(sid, num, ep)

    # NB: frontend triggers autogen explicitly (POST /auto-generate/sweep) after
    # the user closes the «Найдены новые персонажи/локации» modal. Auto-firing
    # here started generation before the modal even appeared — user saw images
    # being created they didn't yet have a chance to opt out of.

    # Build dropped-name reports for nicer UI feedback
    char_id2name = {c['id']: c['name'] for c in s.get('characters', [])}
    loc_id2name  = {l['id']: l['name'] for l in s.get('locations',  [])}
    dropped_chars = sorted(char_id2name.get(i, i) for i in (prev_chars - set(final_chars)) if i in char_id2name)
    dropped_locs  = sorted(loc_id2name.get(i,  i) for i in (prev_locs  - set(final_locs))  if i in loc_id2name)

    # 4) Synopsis — write/rewrite from the script so the synopsis textarea always
    #    matches what the user actually wrote in the script. Best-effort: if
    #    synopsis generation fails, we just keep the old one.
    synopsis_status = {'updated': False, 'previous': ep.get('synopsis', ''), 'new': None, 'error': None}
    try:
        syn_prompt = (
            f'Series: "{s.get("title","")}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
            f'EPISODE {num} SCRIPT:\n{script[:9000]}\n\n'
            'Write a 3–4 sentence synopsis IN RUSSIAN of what actually happens in this episode '
            '(based strictly on the script above). Cover: opening conflict, mid-episode reversal, '
            'ending cliffhanger. Name characters by their exact names. No vague spoilers — be concrete. '
            'Output ONLY the synopsis text, no preface, no quotes, no markdown.'
        )
        new_syn = (claude_ask_fast(
            syn_prompt,
            system='You are a short-drama editor writing concise episode synopses in Russian.'
        ) or '').strip()
        # Strip any wrapping quotes the model occasionally adds
        if new_syn and new_syn[0] in '"«' and new_syn[-1] in '"»':
            new_syn = new_syn[1:-1].strip()
        if new_syn:
            ep['synopsis'] = new_syn
            save_episode(sid, num, ep)
            synopsis_status = {
                'updated': True,
                'previous': synopsis_status['previous'],
                'new': new_syn,
                'error': None,
            }
    except Exception as e:
        synopsis_status['error'] = str(e)

    # 5) Canon audit — verify the script against current canon BEFORE merging.
    #    If the script breaks canon → return critical violations as warnings and
    #    DO NOT update the canon (user needs to fix things first).
    canon_status = {
        'audited': False,
        'passes': True,
        'violations': [],
        'updated': False,
        'update_summary': None,
        'audit_error': None,
    }
    try:
        brief = build_logic_brief(sid, num)
    except Exception as e:
        brief = f'(brief unavailable: {e})'
    try:
        audit_result = audit_script(sid, num, script, brief)
        canon_status['audited'] = True
        violations = audit_result.get('violations', []) or []
        canon_status['violations'] = violations
        canon_status['audit_error'] = audit_result.get('audit_error')
        critical = [v for v in violations if v.get('severity') == 'critical']
        canon_status['passes'] = not critical
        if not critical:
            # Canon-clean → roll back any prior canon entries for this ep, then
            # extract fresh updates from the current script.
            try:
                rollback_canon_for_episode(sid, num)
            except Exception:
                pass
            try:
                upd = extract_canon_updates(sid, num, script)
                if upd and not upd.get('error'):
                    canon_status['updated'] = True
                    canon_status['update_summary'] = {
                        'world_day': upd.get('world_day'),
                        'new_facts': upd.get('new_facts', 0),
                    }
                else:
                    canon_status['audit_error'] = canon_status.get('audit_error') or upd.get('error')
            except Exception as e:
                canon_status['audit_error'] = canon_status.get('audit_error') or str(e)
    except Exception as e:
        canon_status['audit_error'] = str(e)

    return jsonify({
        'added_characters':  added_chars,
        'added_locations':   added_locs,
        'added_items':       added_items,
        'dropped_characters': dropped_chars,
        'dropped_locations':  dropped_locs,
        'characters_used':   final_chars,
        'locations_used':    final_locs,
        'series': s,
        'synopsis':       synopsis_status,
        'canon':          canon_status,
        'appearance_updates': appearance_updates,
        'director_notes_used': bool(director_notes),
    })


@app.route('/api/series/<sid>/confirm-milestones', methods=['POST'])
def confirm_milestones(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ms = s.get('milestone_synopses', {})
    missing = [n for n in MILESTONE_EPS if str(n) not in ms or not ms[str(n)]]
    if missing:
        return jsonify({'error': f'Сначала сгенерируй контрольные точки: {missing}'}), 400
    s['stage'] = 3  # was 2 (milestones) or 1 (legacy arc stage) → advance to episodes
    save_series(sid, s)
    return jsonify(s)


@app.route('/api/series/<sid>/generate-next-episode-synopsis', methods=['POST'])
def generate_next_episode_synopsis(sid):
    """Generate synopsis for the next (not yet created) episode, using prev episode context and nearest milestone."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    episodes = sorted(list_episodes(sid), key=lambda e: e['number'])
    data = request.json or {}
    # Use requested episode number if provided, otherwise auto-detect
    requested_num = data.get('episode_number')
    if requested_num:
        next_num = int(requested_num)
    else:
        next_num = (episodes[-1]['number'] + 1) if episodes else 1

    batch = is_batch_mode(s)
    bs = batch_size(s) if batch else 1

    # Find nearest upcoming milestone (milestones still indexed by sub-episode number)
    ms = s.get('milestone_synopses', {})
    if batch:
        a, b = chunk_range(s, next_num)
        upcoming_milestone_num = next((m for m in MILESTONE_EPS if a <= m <= b or m >= a), None)
    else:
        upcoming_milestone_num = next((m for m in MILESTONE_EPS if m >= next_num), None)
    upcoming_milestone_syn = ms.get(str(upcoming_milestone_num), '') if upcoming_milestone_num else ''
    milestone_block = ''
    if upcoming_milestone_syn and upcoming_milestone_num != next_num:
        milestone_block = (
            f'\nNEAREST UPCOMING MILESTONE — Sub-episode {upcoming_milestone_num}:\n'
            f'{upcoming_milestone_syn}\n'
            f'IMPORTANT: this synopsis must logically lead toward this milestone. '
            f'Do NOT contradict or skip over its events.\n'
        )

    unit_word = 'CHUNK' if batch else 'EPISODE'
    chunk_range_str = (lambda n: f'{chunk_range(s,n)[0]}–{chunk_range(s,n)[1]}')(next_num) if batch else str(next_num)

    if next_num == 1 or not episodes:
        if batch:
            a, b = chunk_range(s, next_num)
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) +
                f'{milestone_block}\n'
                f'BATCH MODE: write the synopsis for CHUNK 1 — sub-episodes {a}–{b} (~{bs*60} sec total).\n'
                f'4–6 sentences outlining: (1) the chunk hook, (2) all {bs} mini-cliffhangers in order, (3) the chunk midpoint reversal, (4) the chunk-end cliffhanger.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": 0}'
            )
        else:
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) +
                f'{milestone_block}\n'
                f'Write a synopsis (2-3 sentences) for EPISODE 1 — the series premiere.\n'
                f'This is the FIRST episode: establish the world and main character while immediately grabbing the viewer. '
                f'Start in immediate stakes — something is already at risk or being revealed. '
                f'End on a strong cliffhanger.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": 0}'
            )
    else:
        prev_eps = [e for e in episodes if e['number'] < next_num]
        prev_ep = prev_eps[-1] if prev_eps else episodes[-1]
        prev_synopsis = prev_ep.get('synopsis', '')
        prev_script = prev_ep.get('script', '')
        script_block = ''
        if prev_script:
            script_block = f'\nPrevious {unit_word.lower()} script (FULL):\n{prev_script}\n'
        prev_label = (f'CHUNK {prev_ep["number"]} (sub-eps {chunk_range(s,prev_ep["number"])[0]}–{chunk_range(s,prev_ep["number"])[1]})'
                      if batch else f'Ep {prev_ep["number"]}')
        if batch:
            a, b = chunk_range(s, next_num)
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) + '\n'
                f'Previous {prev_label} synopsis:\n{prev_synopsis}\n'
                f'{script_block}'
                f'{milestone_block}\n'
                f'BATCH MODE: write the synopsis for CHUNK {next_num} — sub-episodes {a}–{b} (~{bs*60} sec total).\n'
                f'Pick up FROM THE NEXT BEAT after the previous chunk\'s cliffhanger, NOT from the cliffhanger itself. NO PLOT-RECAP: events already shown in CHUNK {prev_ep["number"]} are DONE — describe what happens NEXT, do NOT have characters re-issue the same ultimatums / re-state the same threats / re-deliver the same revelations from the previous chunk. '
                f'4–6 sentences outlining: (1) chunk hook, (2) all {bs} mini-cliffhangers in order, (3) chunk midpoint reversal, (4) chunk-end cliffhanger.\n'
                f'TIMELINE — also output `days_since_previous` (integer in-world days from prev chunk\'s end). 0 = same-day continuation.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": int}'
            )
        else:
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) + '\n'
                f'Previous episode ({prev_label}) synopsis:\n{prev_synopsis}\n'
                f'{script_block}'
                f'{milestone_block}\n'
                f'Write a synopsis (2-3 sentences) for EPISODE {next_num}.\n'
                f'Continue naturally from where episode {prev_ep["number"]} left off — pick up FROM THE NEXT BEAT after the cliffhanger, NOT from the cliffhanger itself. '
                f'NO PLOT-RECAP: events that already happened in Ep {prev_ep["number"]} (ultimatums delivered, threats made, revelations, decisions announced) are DONE. The Ep {next_num} synopsis must describe what happens NEXT (the response, the counter-move, the consequence) — NOT the same ultimatum being re-issued or the same threat being re-stated. If Ep {prev_ep["number"]} ended with "Character X demands Y or else Z" — Ep {next_num} synopsis describes the OTHER side\'s reaction / a twist / a new arrival, not Character X repeating the demand. '
                f'Include: an immediate-stakes opening (no warm-up), a mid-episode reversal, and end on a new cliffhanger. '
                f'TIMELINE — also output `days_since_previous` (integer): in-world days between Ep {prev_ep["number"]} and Ep {next_num}. '
                f'Use real-world biology (pregnancy test 10+ days post conception, undercover ops 30+ days setup). 0 = same-day continuation. '
                'Return JSON: {"synopsis": "...", "days_since_previous": int}'
            )

    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
        syn = data.get('synopsis', '')
        dsp = data.get('days_since_previous')
        # If the episode ALREADY exists (user pre-created it), just patch its
        # synopsis / days metadata in-place. Do NOT auto-create a new episode here:
        # this endpoint is called from the "Создать эпизод" modal's "✨ Сгенерить
        # синопсис" button, where actual episode creation happens later via
        # POST /episodes when the user confirms. Side-effect creation here led
        # to phantom episodes when users generated a synopsis and then closed
        # the modal — the episode was silently committed to disk.
        ep = load_episode(sid, next_num)
        if ep is not None:
            if not ep.get('synopsis'): ep['synopsis'] = syn
            if dsp is not None: ep['days_since_previous'] = int(dsp)
            save_episode(sid, next_num, ep)
        return jsonify({'synopsis': syn, 'days_since_previous': dsp, 'episode_number': next_num})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/generate-episode-synopses', methods=['POST'])
def generate_episode_synopses(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    episodes = {ep['number']: ep for ep in list_episodes(sid)}
    ms = s.get('milestone_synopses', {})
    batch = is_batch_mode(s)
    bs = batch_size(s) if batch else 1
    cast_pin = _canonical_cast_block(s)
    if batch:
        # In batch mode, fill all chunks BETWEEN the anchor chunks (e.g. chunks 1..chunk_of(10)).
        # Each chunk synopsis must outline `bs` sub-cliffhangers + the chunk's main reversal.
        a1, a2, _ = anchor_chunks(s)
        target = list(range(a1, a2 + 1))  # chunks 1..a2
        ms_anchor1 = ms.get(str(a1), '')
        ms_anchor2 = ms.get(str(a2), '')
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
            + cast_pin +
            f'Anchor chunks: Chunk {a1}: {ms_anchor1} | Chunk {a2}: {ms_anchor2}\n\n'
            f"This series is in BATCH MODE — each storage unit is a CHUNK of {bs} consecutive sub-episodes (~{bs*60}s total). "
            f"Write synopses for chunks {a1}..{a2} (sub-episodes 1–{a2*bs}). "
            f"Each chunk synopsis (4–6 sentences) must outline: (1) opening hook, (2) the {bs} mini-cliffhangers in order, (3) the chunk's midpoint reversal, (4) the chunk-end cliffhanger leading into the next chunk. "
            f"Chunk {a1} aligns with anchor 1 and chunk {a2} aligns with anchor 2. Chunks in between escalate logically. "
            "Each chunk must continue from where the previous chunk ended — the next chunk's writer will read this synopsis as their context. "
            "TIMELINE — for each chunk also output `days_since_previous` (integer in-world days from the previous chunk's end). Chunk 1 always has 0. "
            "Use real-world biology/protocol: pregnancy test 10+ days post conception, undercover ops 30+ days setup. "
            'Return JSON: {"episodes": {' + ', '.join(f'"{n}": {{"synopsis":"...", "days_since_previous": 0}}' for n in target) + '}}'
        )
    else:
        # Pin checkpoint / finale hints so synopsis-writer plots toward them.
        cps = sorted((s.get('checkpoints') or []), key=lambda c: int(c.get('episode', 0)))
        cps_block = ''
        if cps:
            cps_block = 'CHECKPOINTS (steer toward each one — at the indicated episode this MUST happen):\n' + \
                '\n'.join(f"  - Ep {c['episode']}: {(c.get('description') or '').strip()}" for c in cps if c.get('description')) + '\n'
        fin = s.get('finale') or {}
        fin_block = ''
        if fin and fin.get('description'):
            fin_block = f"FINALE (ep {fin.get('episode','?')}): {fin['description']}\n" + \
                "Do not violate finale constraints early (don't kill characters needed alive, etc).\n"
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
            + cast_pin +
            f'Milestone synopses: Ep 1: {ms.get("1","")} | Ep 10: {ms.get("10","")}\n'
            + cps_block + fin_block + '\n'
            "Write synopses (2-3 sentences each) for episodes 1 through 10. "
            "Ep 1 and 10 must match their milestones exactly. "
            "SHORT DRAMA FORMAT — every single episode must: (1) open with a conflict already in progress (not building toward one), (2) include one mid-episode reversal or unexpected complication that flips what we thought was true, (3) end with a cliffhanger that makes the next episode unmissable. "
            "NO episode is a filler bridge — every one must have its own shock, reversal, and cliffhanger punch. "
            "TIMELINE — for each episode also output `days_since_previous` (integer): how many in-world days passed since the previous episode. "
            "Use real-world biology/protocol: pregnancy test needs 10+ days post conception, undercover ops 30+ days setup, intercontinental flight 8+ hours. "
            "Ep 1 always has days_since_previous=0. Use 0 for same-day continuation. "
            'Return JSON: {"episodes": {"1": {"synopsis":"...", "days_since_previous": 0}, "2": {"synopsis":"...", "days_since_previous": 1}, ...}}'
        )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
        synopses = data.get('episodes', {})
        result = {}
        for num_str, payload in synopses.items():
            num = int(num_str)
            # Accept legacy string format too
            if isinstance(payload, str):
                syn, dsp = payload, None
            else:
                syn = payload.get('synopsis', '')
                dsp = payload.get('days_since_previous')
            result[num_str] = syn
            if num in episodes:
                ep = episodes[num]
                if not ep.get('synopsis'):
                    ep['synopsis'] = syn
                    if dsp is not None: ep['days_since_previous'] = int(dsp)
                    save_episode(sid, num, ep)
            else:
                ep = {
                    'number': num, 'title': f'Эпизод {num}', 'synopsis': syn,
                    'script': '', 'characters_used': [], 'character_outfits': {},
                    'days_since_previous': int(dsp) if dsp is not None else (0 if num == 1 else 1),
                    'notes': '', 'status': 'draft',
                    'reteller': {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None}
                }
                save_episode(sid, num, ep)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _canonical_cast_block(s):
    """Synopsis-time cast pin: short list of canonical character names + role tags so that
    every synopsis-generating Claude call uses the SAME names instead of inventing fresh
    ones (which is how Emma/Victoria/Liam quietly became Elena/Marcus/Claire). Returns ''
    when the series has no characters yet (very first synopsis at idea time)."""
    chars = (s or {}).get('characters', []) or []
    if not chars:
        return ''
    lines = []
    for c in chars:
        name = c.get('name', '').strip()
        if not name:
            continue
        role = (c.get('description') or '').strip().split('.')[0][:120]
        lines.append(f'  • {name}' + (f' — {role}' if role else ''))
    if not lines:
        return ''
    return (
        'CANONICAL CAST — these are the ONLY named characters that exist in this series. '
        'Every synopsis MUST use these exact names. NEVER invent alternative names for the same '
        'roles (do not rename the heroine, the sister, the fiancé, etc.). If a character is not '
        'in the list and the scene needs one, label them by role only (Doctor, Driver, Mother).\n'
        + '\n'.join(lines) + '\n\n'
    )


def _outfit_ids(val):
    """Normalize episode.character_outfits[cid] into a list of outfit ids.
    Backward-compatible: accepts legacy single-string, list, or None/empty.
    Going forward storage is always list. Reads tolerate both."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    return [str(val)]


def _build_cast_block(s, ep):
    """Build CAST & LOCATIONS context for script generation with exact names, outfits and real asset filenames."""
    chars = s.get('characters', [])
    locs  = s.get('locations', [])
    ep_outfits = ep.get('character_outfits', {})
    ep_chars   = ep.get('characters_used', [])
    ep_locs    = ep.get('locations_used', [])

    lines = []
    for c in chars:
        outfit_ids = _outfit_ids(ep_outfits.get(c['id']))
        ep_outfits_objs = [
            o for oid in outfit_ids
            for o in c.get('outfits', []) if o['id'] == oid
        ]
        # Primary outfit (for OUTFIT label / asset filename) = first selected, else base
        outfit = ep_outfits_objs[0] if ep_outfits_objs else None
        outfit_label = outfit['label'] if outfit else 'base'
        outfit_desc  = outfit.get('description', '') if outfit else c.get('appearance', '')
        marker = '★' if c['id'] in ep_chars else ' '

        # Real asset filename — use outfit photo if set, else first ref image
        if outfit and outfit.get('photo'):
            asset_filename = Path(outfit['photo']).name
        elif c.get('ref_images'):
            asset_filename = Path(c['ref_images'][0]).name
        else:
            asset_filename = None
        asset_str = f' | ASSET_FILE: {asset_filename}' if asset_filename else ''

        # List all available outfits so Claude can pick the right one for the scene
        available = ['base'] + [o['label'] for o in c.get('outfits', []) if o.get('label')]
        available_str = f' | AVAILABLE_OUTFITS: {", ".join(available)}'

        # Episode-selected outfits (when more than one — character changes clothes within episode)
        selected_str = ''
        if len(ep_outfits_objs) > 1:
            sel_labels = ', '.join(o['label'] for o in ep_outfits_objs)
            selected_str = (
                f' | EPISODE_OUTFITS: {sel_labels} '
                f'(⚠ character WEARS DIFFERENT CLOTHES across scenes — you MUST list this character {len(ep_outfits_objs)} times '
                f'in the === EPISODE CAST === block, once per outfit, each with the matching outfit_label and OUTFIT_DESC)'
            )

        lines.append(
            f'  {marker} NAME: "{c["name"]}" | OUTFIT: "{outfit_label}"{available_str}{asset_str}{selected_str}'
            + (f' ({outfit_desc[:80]})' if outfit_desc else '')
        )

    cast_block = ('SERIES CHARACTERS — use these EXACT names and outfits in the script:\n'
                  + '\n'.join(lines)) if lines else ''

    loc_lines = []
    for l in locs:
        marker = '★' if l['id'] in ep_locs else ' '
        if l.get('ref_images'):
            loc_asset = Path(l['ref_images'][0]).name
            asset_str = f' | ASSET_FILE: {loc_asset}'
        else:
            asset_str = ''
        loc_lines.append(f'  {marker} "{l["name"]}": {l.get("description","")[:80]}{asset_str}')
    loc_block = 'LOCATIONS (★ = used in this episode):\n' + '\n'.join(loc_lines) if loc_lines else ''

    return '\n\n'.join(filter(None, [cast_block, loc_block]))


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
    trajectory_block = build_trajectory_block(s, num)

    if batch:
        a, b = chunk_range(s, num)
        instruction = (
            f'Write the complete script for {unit_label}. '
            f'It must contain {bs} sub-episodes (sub-eps {a} through {b}), each ending with the EXACT cut marker '
            f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
            f'The chunk overall has its own midpoint REVERSAL and a final cliffhanger that leads into chunk {num+1}. '
        )
    else:
        instruction = (
            f'Write the complete script for Episode {num}. '
            + ('Continue naturally from where Episode {prev} ended.'.format(prev=num-1) if prev_script else 'Hook the viewer immediately.')
            + ' End on a cliffhanger.'
        )

    base_prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Series arc: {s.get("arc","")}\n\n'
        + (cast_block + '\n\n' if cast_block else '')
        + brief_block
        + trajectory_block
        + prev_block
        + f'{unit_label} SYNOPSIS:\n{ep.get("synopsis","")}\n\n'
        + next_block
        + instruction
        + ' EVERY constraint in the LOGIC CONSTRAINTS block above is mandatory — violating canon is a hard fail.'
    )

    script_system = _build_batch_script_system(s) if batch else _SCRIPT_SYSTEM

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
            all_violations = list(report.get('violations', [])) + list(logic_report.get('violations', []))
            critical = [v for v in all_violations if v.get('severity') == 'critical']
            audit_report = {
                'passes': not critical,
                'violations': all_violations,
                'retries': attempt,
                'audit_error': report.get('audit_error') or logic_report.get('audit_error'),
                'logic_passes': logic_report.get('passes', True),
                'continuity_passes': report.get('passes', True),
            }
            if not critical:
                break
            # Build a fix-it prompt and retry — group violations by source for clarity
            cont_fixes = [v for v in critical if v.get('type') in ('timeline','fact','knowledge','biology','setup','paperwork','scene_teleport')]
            logic_fixes = [v for v in critical if v.get('type') in ('status','hidden_position','enabling_condition','legal_term','unmotivated_delay','ambiguous_cliffhanger')]
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
        ep['script'] = script
        ep['status'] = 'draft'
        ep['logic_brief'] = brief
        ep['audit_report'] = audit_report
        save_episode(sid, num, ep)

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


# ── Episode Reteller prompt ───────────────────────────────────────────────────

_RTL_PROMPT_SYSTEM = """You are a director writing a self-contained Reteller.ai production prompt for one episode of a short vertical drama (~60 seconds, ends on a cliffhanger).

The output is a SINGLE prompt that can be pasted into the generator without any extra context. The generator has NO memory of previous episodes — every visual element must be re-described from scratch.

═══════════════════════════════════════════
LANGUAGE MAP — DIFFERENT BLOCKS USE DIFFERENT LANGUAGES
═══════════════════════════════════════════
• Блок 1 (world setting) — ENGLISH
• Блок 2 (characters)   — ENGLISH (full inline descriptions)
• Блок 3 (camera)       — RUSSIAN (verbatim canonical text — see below)
• Блок 4 (cast list)    — names as written (English Latin letters)
• Блок 5 (location)     — ENGLISH
• Блок 6 (positioning)  — RUSSIAN (3–5 sentences for the director/operator)
• Блок 7 (beats)        — Dialogue text in ENGLISH inside quotes; emotion brackets in RUSSIAN; ACTION beat descriptions in RUSSIAN.

This split is intentional: blocks 1/2/5 feed an English-trained image generator; blocks 3/6 + emotions/actions are read by a Russian-speaking director. Do not "fix" the language of any block — follow the map.

═══════════════════════════════════════════
OUTPUT — EXACTLY 7 BLOCKS, IN THIS ORDER, WITH THESE EXACT HEADERS:
═══════════════════════════════════════════

═══ БЛОК 1: WORLD SETTING ═══
(English, 200–400 words, identical for all episodes of this series)
Describe: physics/rules of the world (how magic/tech/power/social system works), visual palette of BOTH sides of the conflict (warm vs cold colors, materials), key plot-driving rules (contracts, debts, laws), specific named places / plants / artifacts. Be concrete — name things.

═══ БЛОК 2: CHARACTERS ═══
(English, ONLY characters who appear in THIS episode. Each character = ONE continuous prompt-line of comma-separated descriptors, NO PERIODS, NO LINE BREAKS inside the description. Adapted to this episode's state.)

Format per character:
NAME — descriptor1, descriptor2, descriptor3, ...

The description must be self-contained enough to regenerate the character from scratch. Cover, IN THIS ORDER:
  1) overall beauty/look impression
  2) age
  3) height + body build
  4) face — shape → nose → lips → brows → cheekbones → jawline
  5) eyes — color → shape → special features → lashes
  6) hair — color → texture → length → style
  7) special non-human features (ears, fangs, glowing skin) — only if applicable
  8) headwear / crown — only if applicable
  9) clothing TOP-TO-BOTTOM — top → armor/cloak → belt → bottoms (each item with color + material)
 10) footwear (color + material)
 11) hands & fingers — skin condition → nails → rings → bracelets → scars
 12) accessories (amulets, weapons)
 13) aura / magical effects — only if applicable
 14) MANDATORY closing technical tags: "ultra realistic, cinematic lighting, high detail, unique face, not resembling any real person"
 15) style tag matching the series world ("medieval fantasy setting" / "dark fantasy" / "modern urban" / "neo-noir" / etc.)

ADAPT THE DESCRIPTION TO THIS EPISODE'S STATE. If by this episode the character has: torn clothes from a fight, fresh bruise on the cheekbone, dust in the hair, a new tattoo, broken amulet (empty cord), stained dress, tear-streaked makeup — INCLUDE these adaptations in the description for THIS episode. The description is not a static character sheet — it is "what the character looks like RIGHT NOW in this episode".

═══ WARDROBE CHANGES — HARD RULE, DO NOT VIOLATE ═══
If the input data lists MORE THAN ONE outfit for the same character in this episode (you will see "this episode wears: CHANGES CLOTHES — outfit_A (...); outfit_B (...)" or multiple CHARACTER lines for the same name in the script's === EPISODE CAST === block), then BLOCK 2 MUST contain a SEPARATE entry per outfit:

   CLAIRE (scene 1 — morning_lingerie) — full descriptor line ending with "...white silk slip, hair messy from sleep, bare feet, no makeup, ultra realistic..."
   CLAIRE (scene 2 — gala_gown) — full descriptor line ending with "...floor-length black silk gown, smoky eyeliner, hair pinned up, diamond earrings, ultra realistic..."

EACH entry repeats the WHOLE top-to-bottom description (face, eyes, hair, body, technical tags) — only clothing/hair/makeup change between entries. The image generator does NOT carry visual state between entries; if you write only one entry, it will use that look for every scene.

This is NOT optional. If you see two outfits in the input, you write two entries. If you see three outfits, three entries. Same character name, parenthetical scene tag, full re-description each time.

═══ БЛОК 3: ИНСТРУКЦИИ ПО СЪЕМКЕ ═══
COPY THE FOLLOWING TEXT VERBATIM IN RUSSIAN — DO NOT TRANSLATE, DO NOT PARAPHRASE, DO NOT SHORTEN:

Статичной камеры нет вообще, камера всегда медленно двигается. В самые напряженные моменты камера становится агрессивной, резкой с применением различных операторских приемов таких как голландский угол, ручная живая камера, резкие зумы, резкие ракурсы, игра света и теней, изменение света, моргание и тд. СТИЛЬ МАКСИМАЛЬНО ФОТОРЕАЛИСТИЧНЫЙ ДОКУМЕНТАЛЬНЫЙ, как будто снято на айфон. Фон — размытый. Смена ракурса и крупности плана регулярная в самые нужные моменты.

═══ БЛОК 4: ДЕЙСТВУЮЩИЕ ЛИЦА ═══
Plain comma-separated list of character names present in this episode (English Latin letters, exactly as in Block 2).

═══ БЛОК 5: LOCATION ═══
(English, 80–150 words PER location. If the episode changes location, write a sub-block per location: "LOCATION 1: NAME", "LOCATION 2: NAME".)
For each location used in this episode write a paragraph covering:
  - room/space dimensions
  - wall / floor / ceiling materials
  - light sources and the type of light
  - specific objects in frame
  - atmosphere — ambient sounds, smells, the felt sense of the space
If the location recurs from an earlier episode and something has changed (broken vials, overturned chair, dried blood, missing painting) — name what changed. Be concrete, not generic.

═══ БЛОК 6: ПОЛОЖЕНИЕ В КАДРЕ ═══
(RUSSIAN, 3–5 sentences for the director/operator.)
Кто где стоит / сидит / лежит, куда движется камера, какие крупности планов используются, ключевые визуальные моменты эпизода (например: «крупный план дрожащих пальцев на стакане → отъезд на средний план, когда антагонист входит в кадр со спины → внезапный голландский угол на финальной реплике»).

═══ БЛОК 7: РЕПЛИКИ ═══
HARD CONTRACT — read carefully:
1. COUNT every dialogue line in the script (every quoted line spoken by a character). Call this N.
2. БЛОК 7 MUST contain EXACTLY N dialogue beats — no more, no less.
   • If script has 14 dialogue lines → Block 7 has 14 dialogue beats.
   • Do NOT merge two short lines into one beat.
   • Do NOT split one long line into two beats.
   • Do NOT skip any line, even short ones like "Oh.", "Wait.", "What?".
   • Do NOT add invented dialogue beats not in the script.
3. ACTION beats sit BETWEEN dialogue beats, ONE PER non-verbal moment described in the script's action lines (slap, door slam, fall, character entrance). They are extra — they do NOT count toward N.

Format:
  Dialogue beat:  N - NAME — "exact line from script" [эмоция на русском]
  Action beat:    N - ACTION — описание действия на русском

Beat numbering is sequential across BOTH dialogue and action beats (1, 2, 3, ... in order of occurrence).

Rules for dialogue text inside the quotes:
• Conversational AMERICAN English — not literary, not British. Short phrases, max 1–2 sentences. Slang allowed when fits the character. Interjections welcome ("huh", "wait", "look", "hey"). Contractions standard ("don't", "can't", "you're", "gonna", "wanna").
• Copy from the script. The screenwriter already wrote in this style — preserve their wording. If you spot a literary phrase, you MAY tighten it into conversational form, but do NOT invent dialogue that wasn't in the script.
• Speaker NAME = exact match to the script's speaker label.
• Order = script order. Do not reorder.

Rules for the emotion bracket [...]:
• Always in RUSSIAN.
• Describes HOW the line is said: tone, volume, body cue. Examples: [тихо, сквозь зубы] [срывается на крик] [холодно, не моргая] [шёпотом, на грани слёз] [с холодной усмешкой].
• Not optional — every dialogue beat has one.

Rules for ACTION beats:
• Description in RUSSIAN, present tense, concrete physical action ("Антагонист резко ставит стакан на стойку, осколки разлетаются по полу").
• Used only for moments the script explicitly contains as action lines.

LAST beat must preserve the script's cliffhanger ending exactly — if the script ends on a line of dialogue, the last beat is that dialogue beat verbatim; if the script ends on an action, the last beat is an ACTION beat matching it.

═══════════════════════════════════════════
PRE-OUTPUT CHECKLIST (silent — verify each before sending):
☐ Block 1 in English, 200–400 words, world physics + palette + named things
☐ Block 2 in English; one line per character actually in this episode; comma-separated descriptors with NO periods; full top-to-bottom coverage; ends with technical tags + style tag; ADAPTED to this episode's state
☐ Block 3 = canonical RUSSIAN camera paragraph, copied VERBATIM
☐ Block 4 = names only, comma-separated
☐ Block 5 in English, 80–150 words per location, recurring locations note what changed
☐ Block 6 in RUSSIAN, 3–5 sentences
☐ Block 7 numbered, dialogue text inside quotes in English, [эмоция] brackets in Russian, ACTION beats in Russian
☐ Block 7 dialogue beat count == script dialogue line count, in script order, last beat = cliffhanger
☐ Episode ≈ 60 seconds
☐ Self-contained (no references to other episodes in generator-facing blocks)

OUTPUT ONLY the 7 blocks separated by their headers. No preamble, no commentary, no JSON, no markdown fences."""


@app.route('/api/series/<sid>/episodes/<int:num>/reteller-prompt', methods=['POST'])
def episode_reteller_prompt(sid, num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    script = ep.get('script', '').strip()
    if not script:
        return jsonify({'error': 'Сначала напиши или сгенерируй сценарий'}), 400

    cast_block = _build_cast_block(s, ep)

    # Pass FULL world + style + per-character + per-location detail so the model can
    # fill BLOCK 1 (world), BLOCK 2 (characters), BLOCK 5 (location) with real content
    # rather than inventing it. The 7-block contract requires all of these in English.
    chars_map = {c['id']: c for c in s.get('characters', [])}
    locs_map  = {l['id']: l for l in s.get('locations', [])}
    ep_outfits = ep.get('character_outfits', {})

    char_details = []
    for cid in ep.get('characters_used', []):
        c = chars_map.get(cid)
        if not c: continue
        outfit_ids_list = _outfit_ids(ep_outfits.get(cid))
        ep_outfits_objs = [
            o for oid in outfit_ids_list
            for o in c.get('outfits', []) if o['id'] == oid
        ]
        # Base line — one per character, with stable identity descriptors
        char_details.append(
            f'  • {c["name"]} ({c.get("gender","")}) — '
            f'appearance: {c.get("appearance","")} | description: {c.get("description","")}'
        )
        # Wardrobe entries — ONE LINE PER OUTFIT so the AI can\'t miss them.
        # Multi-outfit characters get a hard "CHANGES CLOTHES — N entries" header.
        if len(ep_outfits_objs) >= 2:
            char_details.append(
                f'      ⚠ CHANGES CLOTHES IN THIS EPISODE — {len(ep_outfits_objs)} separate looks. '
                f'Block 2 MUST have {len(ep_outfits_objs)} entries for {c["name"]} '
                f'(one per scene/outfit, with full top-to-bottom re-description).'
            )
            for idx, o in enumerate(ep_outfits_objs, 1):
                desc = (o.get('description') or '').strip()
                char_details.append(
                    f'      ↳ look {idx} — outfit_label="{o["label"]}" | wardrobe & state: {desc or "(no description, infer from outfit_label)"}'
                )
        elif len(ep_outfits_objs) == 1:
            o = ep_outfits_objs[0]
            desc = (o.get('description') or '').strip()
            char_details.append(
                f'      wears: {o["label"]} — {desc or "(see character base appearance)"}'
            )
        else:
            char_details.append('      wears: base look (default appearance from character description)')
    char_block = ('CHARACTERS IN THIS EPISODE — full reference data (use to write BLOCK 2):\n'
                  + '\n'.join(char_details)) if char_details else ''

    loc_details = []
    for lid in ep.get('locations_used', []):
        l = locs_map.get(lid)
        if not l: continue
        loc_details.append(f'  • {l["name"]} — {l.get("description","")}')
    loc_block = ('LOCATIONS IN THIS EPISODE — full reference data (use to write BLOCK 5):\n'
                 + '\n'.join(loc_details)) if loc_details else ''

    world = s.get('world_description', '')
    style_type = s.get('style', {}).get('type', '')
    style_custom = s.get('style', {}).get('custom_description', '')
    world_ctx = (
        f'SERIES WORLD (use to write BLOCK 1, expand to 200–400 English words):\n'
        f'  Title: {s["title"]} | Genre: {s.get("genre","")} | Tone: {s.get("tone","")} | Visual style: {style_type}\n'
        f'  World: {world}\n'
        + (f'  Custom style notes: {style_custom}\n' if style_custom else '')
    )

    prompt = (
        world_ctx + '\n'
        + (char_block + '\n\n' if char_block else '')
        + (loc_block + '\n\n' if loc_block else '')
        + (cast_block + '\n\n' if cast_block else '')
        + f'EPISODE {num} SYNOPSIS: {ep.get("synopsis","")}\n\n'
        + f'EPISODE {num} SCRIPT (source — convert into the 7-block prompt):\n{script}\n\n'
        + 'TASK: Output the complete 7-block Reteller production prompt for this episode, following your system instructions EXACTLY. Per-block reminders:\n'
        + '— БЛОК 1 (English, 200–400 words): expand SERIES WORLD above into a full description — physics, palette of both sides, named places/items/laws.\n'
        + '— БЛОК 2 (English, full inline descriptions): ONE line per character present in this episode. Use the CHARACTERS data above (appearance + this-episode outfit) and the script to write a comma-separated description with NO periods, covering: overall look → age → height/build → face (shape/nose/lips/brows/cheekbones/jaw) → eyes → hair → headwear if any → clothing top-to-bottom (each item with color + material) → footwear → hands/rings → accessories → aura (if magical) → end with the mandatory tags `ultra realistic, cinematic lighting, high detail, unique face, not resembling any real person` + a style tag matching the world. ADAPT to this episode\'s state (torn clothes after a fight, fresh bruise, dust in hair, broken amulet, tear-streaked makeup — whatever the script implies for THIS scene).\n'
        + '— БЛОК 3: copy the canonical RUSSIAN camera-instructions paragraph from your system prompt VERBATIM — do not translate or paraphrase.\n'
        + '— БЛОК 4: comma-separated character names (English Latin letters as in Block 2).\n'
        + '— БЛОК 5 (English, 80–150 words per location): use LOCATIONS data above + script context. Cover dimensions, wall/floor/ceiling materials, light sources, specific objects, atmosphere. Note what changed if the location recurs.\n'
        + '— БЛОК 6 (RUSSIAN, 3–5 sentences): кто где находится, движение камеры, крупности планов, ключевые визуальные моменты эпизода.\n'
        + '— БЛОК 7: numbered beats interleaving dialogue and ACTION. DIALOGUE TEXT INSIDE QUOTES = the script\'s line, preserved word-for-word (the screenwriter already wrote conversational American English — keep it as-is, do not reorder, do not skip, do not invent). The [...] emotion bracket is in RUSSIAN describing HOW the line is said. ACTION beats use RUSSIAN descriptions of physical action. Number of dialogue beats == number of dialogue lines in the script. LAST beat preserves the script\'s cliffhanger.\n'
        + 'Output ONLY the 7 blocks separated by their headers. No commentary.'
    )
    def _count_script_dialogue_lines(scr: str) -> int:
        """Count quoted dialogue lines in the script. A dialogue line is a
        quoted string in a screenplay block — we count occurrences of opening
        smart-quote " or straight " preceding text on a line, OR a NAME:
        followed by a non-empty quoted line on the next non-blank line."""
        import re as _re
        # Count "..."  and "..." (smart and straight) — each opening quote = 1 line
        # Smart quotes used in scripts: " (left double) and ' (left single for stage)
        count = 0
        for m in _re.finditer(r'[“"][^”"\n]{1,400}[”"]', scr):
            count += 1
        return count

    def _count_block7_dialogue_beats(rtl: str) -> int:
        import re as _re
        # Beat lines look like: "1 - NAME — "...""  or "1 - ACTION — ..."
        # Dialogue beats contain a quoted string.
        c = 0
        for ln in rtl.splitlines():
            if _re.match(r'\s*\d+\s*-\s*[A-Z]', ln) and ('"' in ln or '“' in ln) and 'ACTION' not in ln.split('—')[0].upper():
                c += 1
        return c

    def _cyrillic_leak(rtl: str) -> int:
        """Count Cyrillic chars in blocks where Russian is FORBIDDEN.
        Russian IS expected in: Block 3 (camera, full), Block 6 (positioning, full),
        Block 7 emotion brackets [...] and ACTION beat descriptions.
        Russian is FORBIDDEN in: Block 1, 2, 4, 5; and inside dialogue quotes in Block 7.
        Block headers (═══ БЛОК ...) are always allowed to keep Cyrillic."""
        import re as _re
        leaked = 0
        block = 0
        for ln in rtl.splitlines():
            m = _re.match(r'\s*═══\s*БЛОК\s*(\d+)', ln)
            if m:
                block = int(m.group(1))
                continue
            if block in (3, 6):
                continue  # Russian fully allowed
            if block == 7:
                # Inside Block 7: only check dialogue text inside quotes — those must be English.
                # Strip emotion brackets and the post-name action descriptions; keep only quoted text.
                quoted = _re.findall(r'[“"]([^”"\n]+)[”"]', ln)
                for q in quoted:
                    for ch in q:
                        if '\u0400' <= ch <= '\u04FF':
                            leaked += 1
                continue
            if block in (1, 2, 4, 5):
                for ch in ln:
                    if '\u0400' <= ch <= '\u04FF':
                        leaked += 1
        return leaked

    expected_lines = _count_script_dialogue_lines(script)
    try:
        rtl_prompt = claude_ask_fast(prompt, system=_RTL_PROMPT_SYSTEM)
        got_lines = _count_block7_dialogue_beats(rtl_prompt)
        cyr = _cyrillic_leak(rtl_prompt)
        need_retry = (expected_lines and abs(got_lines - expected_lines) > 0) or cyr > 0
        if need_retry:
            reasons = []
            if expected_lines and abs(got_lines - expected_lines) > 0:
                reasons.append(f'dialogue count mismatch: script={expected_lines} block7={got_lines}')
            if cyr > 0:
                reasons.append(f'cyrillic leak: {cyr} chars outside headers')
            print(f'[reteller-prompt] retry needed — {"; ".join(reasons)}', flush=True)
            retry_prompt = (
                prompt
                + f'\n\nSTRICT FIX:\n'
                + (f'• The script has EXACTLY {expected_lines} dialogue lines (quoted spoken lines). Your previous output had {got_lines} dialogue beats. That is wrong. Block 7 MUST contain EXACTLY {expected_lines} dialogue beats — one per script line, in order, verbatim. ACTION beats are separate and not counted toward this number.\n' if (expected_lines and abs(got_lines - expected_lines) > 0) else '')
                + (f'• Your previous output contained {cyr} Cyrillic characters in blocks where Russian is FORBIDDEN. Russian is allowed ONLY in Block 3 (camera), Block 6 (positioning), Block 7 [эмоция] brackets, and Block 7 ACTION beat descriptions. Block 1 (world), Block 2 (characters), Block 4 (cast list), Block 5 (location), and the dialogue text INSIDE QUOTES in Block 7 — all of these MUST be English. Rewrite the offending blocks in English; keep blocks 3 and 6 + emotions + ACTION descriptions in Russian as required by the spec.\n' if cyr > 0 else '')
                + 'Regenerate the entire 7-block prompt now with these fixes.'
            )
            rtl_prompt2 = claude_ask_fast(retry_prompt, system=_RTL_PROMPT_SYSTEM)
            got2 = _count_block7_dialogue_beats(rtl_prompt2)
            cyr2 = _cyrillic_leak(rtl_prompt2)
            print(f'[reteller-prompt] retry result: block7={got2} (target {expected_lines}), cyrillic={cyr2}', flush=True)
            # Pick whichever is closer to clean
            score_old = abs(got_lines - (expected_lines or got_lines)) + cyr
            score_new = abs(got2 - (expected_lines or got2)) + cyr2
            if score_new < score_old:
                rtl_prompt = rtl_prompt2
        ep['reteller_prompt'] = rtl_prompt
        ep['reteller_prompt_dialogue_check'] = {
            'script_lines': expected_lines,
            'block7_beats': _count_block7_dialogue_beats(rtl_prompt),
            'cyrillic_leak': _cyrillic_leak(rtl_prompt),
        }
        save_episode(sid, num, ep)
        return jsonify({
            'prompt': rtl_prompt,
            'dialogue_check': ep['reteller_prompt_dialogue_check'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Range Reteller prompt ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/reteller/range-prompt', methods=['POST'])
def range_reteller_prompt(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    data   = request.json or {}
    from_n = int(data.get('from', 1))
    to_n   = int(data.get('to', 1))

    episodes = [load_episode(sid, n) for n in range(from_n, to_n + 1)]
    episodes = [e for e in episodes if e]

    # Collect characters/locations and ALL outfits used per character across range
    all_char_ids = set()
    all_loc_ids  = set()
    # char_id -> set of outfit_ids used in this range
    char_outfits_used: dict = {}

    for ep in episodes:
        all_char_ids.update(ep.get('characters_used', []))
        all_loc_ids.update(ep.get('locations_used', []))
        ep_co = ep.get('character_outfits', {}) or {}
        for cid, raw in ep_co.items():
            if cid in ep.get('characters_used', []):
                for oid in _outfit_ids(raw):
                    char_outfits_used.setdefault(cid, set()).add(oid)

    chars_in_range = [c for c in s.get('characters', []) if c['id'] in all_char_ids]
    locs_in_range  = [l for l in s.get('locations',  []) if l['id']  in all_loc_ids]

    # Collect pre-generated reteller_prompt from each episode — no Claude calls needed
    missing = []
    ep_prompts = []
    for ep in episodes:
        rp = ep.get('reteller_prompt', '').strip()
        if rp:
            ep_prompts.append(f'{"━"*51}\n{s["title"].upper()} — EPISODE {ep["number"]}\n{"━"*51}\n\n{rp}')
        else:
            missing.append(ep['number'])

    rtl_prompt = '\n\n\n'.join(ep_prompts)
    if missing:
        note = f'⚠ Episodes without Reteller prompt (generate script first): {", ".join(str(n) for n in missing)}\n\n'
        rtl_prompt = note + rtl_prompt if rtl_prompt else note.strip()

    # Build character list with all outfits used in range
    chars_out = []
    for c in chars_in_range:
        outfit_ids = char_outfits_used.get(c['id'], set())
        outfits = [o for o in c.get('outfits', []) if o['id'] in outfit_ids]
        chars_out.append({
            'id': c['id'],
            'name': c['name'],
            'outfits': [{'id': o['id'], 'label': o['label']} for o in outfits],
            'has_multiple_outfits': len(outfits) > 1,
        })
    return jsonify({
        'prompt': rtl_prompt,
        'characters': chars_out,
        'locations':  [{'id': l['id'], 'name': l['name']} for l in locs_in_range],
        'missing_prompts': missing or None,
    })


# ── Episodes ─────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/episodes', methods=['GET'])
def get_episodes(sid):
    eps = list_episodes(sid)
    # Self-heal stuck gen_status='generating' — if an episode hasn't had a new
    # seedance chunk added in the last 10 minutes AND has no in-flight chunks,
    # it's almost certainly leftover from a crashed range-gen worker that
    # forgot to flip the status away from 'generating'. Without this sweep
    # the UI hides the «ready» checkbox forever and user has to manually edit
    # the JSON. Cheap: runs only over eps marked 'generating', no LLM calls.
    healed = []
    now = int(time.time())
    for ep in eps:
        if ep.get('gen_status') != 'generating':
            continue
        chunks = ep.get('seedance_chunks') or []
        # Skip mid-startup (status='generating' just set by a fresh range-gen
        # worker, no chunks yet) — we can't tell from the server whether the
        # client is still working. False-positive heal would race with the
        # active runner.
        if not chunks:
            continue
        any_inflight = any(
            c.get('status') in ('submitting', 'pending', 'processing') for c in chunks
        )
        if any_inflight:
            continue   # real run in flight, leave alone
        # All chunks settled. Only heal if the last chunk was created more
        # than 10 minutes ago — a recent finished chunk could mean the client
        # is between chunks (compose for the next one).
        last_activity = max((c.get('created_at') or 0) for c in chunks)
        if (now - last_activity) < 600:
            continue
        healed.append(ep.get('number'))
        ep.pop('gen_status', None)
        try:
            save_episode(sid, ep.get('number'), ep)
        except Exception as e:
            print(f'[gen_status self-heal] save failed ep{ep.get("number")}: {e}', flush=True)
    if healed:
        print(f'[gen_status self-heal] {sid}: cleared stuck \'generating\' on episodes {healed}', flush=True)
    return jsonify(eps)

_CREATE_EP_LOCKS = {}
# Per-episode lock for serializing seedance chunks mutations (start / submit /
# poll / delete / heal). Critical because Flask is threaded=True and parallel
# auto-mode fires multiple /seedance/start calls in the same second — without
# locking they all read-modify-write the same episode JSON and last-writer-
# wins erases all but one chunk. Real bug: parallel batch fired 4 starts at
# 20:13:35, only 1 chunk record survived on disk.
_EPISODE_LOCKS = {}
_EPISODE_LOCKS_GUARD = threading.Lock()

def _episode_lock(sid, num):
    """Get-or-create the lock for (sid, episode_num). Used to serialize
    read-modify-write of an episode's JSON file."""
    key = (sid, int(num))
    with _EPISODE_LOCKS_GUARD:
        lock = _EPISODE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _EPISODE_LOCKS[key] = lock
    return lock
# Idempotency cache for POST /episodes — avoids duplicate creation when the
# same POST fires twice (browser retry, ext, double-handler). Keyed by
# (sid, idempotency_key); value = (timestamp, response_dict). TTL 60s.
_CREATE_EP_IDEMPOTENCY = {}
_CREATE_EP_IDEMPOTENCY_TTL = 60

@app.route('/api/series/<sid>/episodes', methods=['POST'])
def create_episode(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    # Critical section: list_episodes → number-assign → save must be atomic per series.
    # Without this, two near-simultaneous POSTs racing on a slow disk produce
    # duplicate episodes (first grabs N, second sees N taken and falls to N+1).
    # PLUS idempotency — if client sends same Idempotency-Key twice (network
    # retry, double-handler), we replay the original response instead of
    # creating a second episode. Both layers protect against duplicates.
    idempotency_key = (request.headers.get('Idempotency-Key') or '').strip()
    lock = _CREATE_EP_LOCKS.setdefault(sid, threading.Lock())
    with lock:
        # Check idempotency cache inside the lock so we serialize the read+write
        if idempotency_key:
            now = time.time()
            # GC stale entries opportunistically
            stale = [k for k, (t, _) in list(_CREATE_EP_IDEMPOTENCY.items())
                     if now - t > _CREATE_EP_IDEMPOTENCY_TTL]
            for k in stale:
                _CREATE_EP_IDEMPOTENCY.pop(k, None)
            cached = _CREATE_EP_IDEMPOTENCY.get((sid, idempotency_key))
            if cached:
                return jsonify(cached[1]), 201

        episodes = list_episodes(sid)
        data = request.json or {}
        # Use requested number if provided and not already taken, otherwise auto-assign
        requested = data.get('number')
        existing_nums = {e['number'] for e in episodes}
        if requested and int(requested) not in existing_nums:
            num = int(requested)
        else:
            num = max((e['number'] for e in episodes), default=0) + 1
        ep = {
            'number': num,
            'title': data.get('title', ''),  # empty by default — UI shows "Эп. N" badge already
            'synopsis': data.get('synopsis', ''),
            'script': data.get('script', ''),
            'characters_used': data.get('characters_used', []),
            'notes': data.get('notes', ''),
            'reteller_prompt': data.get('reteller_prompt', ''),
            'status': 'draft',
            'reteller': {
                'project_id': None,
                'status': None,
                'video_url': None,
                'submitted_at': None,
            },
        }
        save_episode(sid, num, ep)
        if idempotency_key:
            _CREATE_EP_IDEMPOTENCY[(sid, idempotency_key)] = (time.time(), ep)
    return jsonify(ep), 201

@app.route('/api/series/<sid>/episodes/<int:num>', methods=['GET'])
def get_episode(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    return jsonify(ep)

@app.route('/api/series/<sid>/episodes/<int:num>', methods=['PUT'])
def update_episode(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    if 'reteller' in data:
        ep['reteller'].update(data.pop('reteller'))
    ep.update(data)
    save_episode(sid, num, ep)
    return jsonify(ep)

@app.route('/api/series/<sid>/episodes/<int:num>/segment-auto-skips', methods=['PUT'])
def update_segment_auto_skips(sid, num):
    """Per-segment auto-mode skip flags. Body: {"skips": ["anchor1", ...]}.
    Anchors here = first dialogue/action line of segment, first ~60 chars trimmed.
    Segments with anchors in this list are SKIPPED during auto-mode generation.
    Default = empty list = all segments included."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    skips = body.get('skips') or []
    cleaned = []
    seen = set()
    for s in skips:
        if not isinstance(s, str): continue
        anchor = s.strip()[:60]
        if not anchor or anchor in seen: continue
        seen.add(anchor)
        cleaned.append(anchor)
    ep['segment_auto_skips'] = cleaned
    save_episode(sid, num, ep)
    return jsonify({'ok': True, 'count': len(cleaned)})


@app.route('/api/series/<sid>/episodes/<int:num>/line-overrides', methods=['PUT'])
def update_line_overrides(sid, num):
    """Per-line override flags for the scene-view (currently only 'close_up').
    Body: {"overrides": [{"anchor": "first ~60 chars of trimmed line", "flags": ["close_up"]}]}
    Anchors are matched against the trimmed line text at parse time. If the line
    is later edited, the override silently drops."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    overrides = body.get('overrides') or []
    cleaned = []
    seen = set()
    for o in overrides:
        if not isinstance(o, dict):
            continue
        anchor = (o.get('anchor') or '').strip()
        flags = o.get('flags') or []
        if not anchor:
            continue
        valid_flags = [f for f in flags if f in ('close_up',)]
        if not valid_flags:
            continue
        if anchor in seen:
            continue
        seen.add(anchor)
        cleaned.append({'anchor': anchor[:60], 'flags': valid_flags})
    ep['line_overrides'] = cleaned
    save_episode(sid, num, ep)
    return jsonify({'ok': True, 'count': len(cleaned)})


@app.route('/api/series/<sid>/episodes/<int:num>/segment-overrides', methods=['PUT'])
def update_segment_overrides(sid, num):
    """Persist user's manual segment-split corrections for the scene-view.
    Body: {"overrides": [{"anchor": "first ~60 chars of line", "action": "break"|"merge"}]}
    Anchors are matched against the trimmed line text at parse time. If the line
    is later edited, the override silently drops (anchor no longer matches)."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    overrides = body.get('overrides') or []
    cleaned = []
    seen_anchors = set()
    for o in overrides:
        if not isinstance(o, dict):
            continue
        anchor = (o.get('anchor') or '').strip()
        action = o.get('action')
        if not anchor or action not in ('break', 'merge'):
            continue
        if anchor in seen_anchors:
            continue           # dedupe — keep first
        seen_anchors.add(anchor)
        cleaned.append({'anchor': anchor[:60], 'action': action})
    ep['segment_overrides'] = cleaned
    save_episode(sid, num, ep)
    return jsonify({'ok': True, 'count': len(cleaned)})


@app.route('/api/series/<sid>/episodes/<int:num>/clear', methods=['POST'])
def clear_episode(sid, num):
    """Reset episode content (synopsis, script, cast) but keep the episode slot."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    ep['synopsis'] = ''
    ep['script'] = ''
    ep['characters_used'] = []
    ep['character_outfits'] = {}
    ep['locations_used'] = []
    ep['notes'] = ''
    ep['reteller_prompt'] = ''
    ep['status'] = 'draft'
    ep['reteller'] = {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None}
    save_episode(sid, num, ep)
    return jsonify(ep)

@app.route('/api/series/<sid>/episodes/<int:num>', methods=['DELETE'])
def delete_episode(sid, num):
    f = episodes_dir(sid) / f'{num:03d}.json'
    if f.exists():
        f.unlink()
    return jsonify({'ok': True})


# ── Assets ───────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/assets/character/<char_id>', methods=['POST'])
def upload_character_asset(sid, char_id):
    """Upload a user-supplied photo. REPLACES the existing ref images
    (any prior auto-generated portraits get removed from disk + dropped
    from ref_images). Mirrors the behaviour of /upload-photo so both
    upload entrypoints are consistent — user reported "uploaded photo
    disappears, old one stays" because the two endpoints had different
    semantics (this one was append-only, /upload-photo replaces).
    Now both replace; delete-button in the gallery still works for
    individual ref removal."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400

    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'character not found'}), 404

    char_dir = assets_dir(sid) / 'characters' / char_id
    char_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    stem = Path(filename).stem
    ext = Path(filename).suffix or '.jpg'
    final = char_dir / filename
    # Don't fight name collisions with old refs — those refs are about to be
    # wiped anyway. Just use the user's filename verbatim, overwriting if needed.
    rel_path = str(final.relative_to(series_path(sid)))

    # Wipe prior on-disk files + ref_images entries (skip the path we're about
    # to write so we don't accidentally delete the new file in case of overlap).
    base = series_path(sid)
    for old_rel in (char.get('ref_images') or []):
        if old_rel == rel_path:
            continue
        try: (base / old_rel).unlink(missing_ok=True)
        except Exception: pass

    file.save(final)
    char['ref_images'] = [rel_path]
    char['avai_base_url'] = ''  # invalidate — old AVAI URL pointed at the old (deleted) gen
    save_series(sid, s)
    # Log so we can trace user-reported "uploaded photo vanished" cases.
    try:
        size = final.stat().st_size if final.exists() else -1
    except Exception:
        size = -1
    _log_event('INFO', 'upload_character_asset', sid=sid, char_id=char_id,
               filename=filename, rel_path=rel_path, file_size=size,
               file_exists_after_save=final.exists())
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}', 'series': s})

@app.route('/api/series/<sid>/assets/character/<char_id>/<path:filename>', methods=['DELETE'])
def delete_character_asset(sid, char_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'character not found'}), 404
    # Files live under assets/characters/<slug>/, NOT assets/characters/<char_id>/.
    # Find the matching ref_image by basename, delete the actual file at its stored path.
    refs = char.get('ref_images') or []
    matched_rel = next((r for r in refs if Path(r).name == filename), None)
    if matched_rel:
        full = series_path(sid) / matched_rel
        if full.exists():
            full.unlink(missing_ok=True)
        char['ref_images'] = [r for r in refs if r != matched_rel]
        # Clear avai_base_url if this was the base portrait
        if asset_name(char.get('name', ''), 'BASE') in filename:
            char.pop('avai_base_url', None)
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/assets/style', methods=['POST'])
def upload_style_asset(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400

    style_dir = assets_dir(sid) / 'style'
    style_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    stem = Path(filename).stem
    ext = Path(filename).suffix
    final = style_dir / filename
    counter = 1
    while final.exists():
        final = style_dir / f'{stem}_{counter}{ext}'
        counter += 1

    file.save(final)
    rel_path = str(final.relative_to(series_path(sid)))
    s['style'].setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}'})

@app.route('/api/series/<sid>/assets/style/<path:filename>', methods=['DELETE'])
def delete_style_asset(sid, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'style' / filename
    if full.exists():
        full.unlink()
    rel = f'assets/style/{filename}'
    s['style']['ref_images'] = [r for r in s['style'].get('ref_images', []) if r != rel]
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/assets/<sid>/<path:filepath>')
def serve_asset(sid, filepath):
    asset_path = series_path(sid) / filepath
    resp = send_from_directory(str(asset_path.parent), asset_path.name)
    # Long-cache (1 year) since the URL itself includes a `?v=<ts>` cache
    # buster from assetUrl() — when the file changes, the FE bumps the
    # version → URL changes → browser fetches the new bytes. While the
    # version stays the same, browser serves from cache → page reload
    # is instant instead of refetching every image.
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@app.route('/api/series/<sid>/skip-autogen', methods=['POST'])
def set_skip_autogen(sid):
    """Marks specific entities (chars / locs / items) as opted-out of the
    autogen sweep. Body: {chars: [id, ...], locs: [...], items: [...], skip: true}.
    With skip=false → unsets the flag (re-includes them in future sweeps).
    Used by the Accept-script modal's "🚫 Не генерить эту группу" checkbox."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    skip = bool(body.get('skip', True))
    char_ids = set(body.get('chars') or [])
    loc_ids  = set(body.get('locs')  or [])
    item_ids = set(body.get('items') or [])
    touched = 0
    for c in s.get('characters', []):
        if c['id'] in char_ids:
            if skip: c['_skip_autogen'] = True
            else:    c.pop('_skip_autogen', None)
            touched += 1
    for l in s.get('locations', []):
        if l['id'] in loc_ids:
            if skip: l['_skip_autogen'] = True
            else:    l.pop('_skip_autogen', None)
            touched += 1
    for it in s.get('items', []):
        if it['id'] in item_ids:
            if skip: it['_skip_autogen'] = True
            else:    it.pop('_skip_autogen', None)
            touched += 1
    if touched:
        save_series(sid, s)
    return jsonify({'touched': touched, 'skip': skip})


@app.route('/api/series/<sid>/relink-assets', methods=['POST'])
def relink_assets(sid):
    """Walks the assets/ folder and re-attaches orphaned files back into
    series.json. Use case: a character/loc/item has a photo on disk in
    assets/characters/<slug>/<NAME>_BASE.{jpg,png,webp} but its `ref_images`
    list is empty — this happens when a corrupted save_series wiped the refs
    (the atomic-write fix prevents NEW occurrences but doesn't heal old data).

    For every char/loc/item whose ref_images is empty:
      - look in assets/<kind>/<slug>/ for files matching `<NAME_STEM>_BASE.*`
        or `<NAME_STEM>.*` (loc/item) ignoring `._*` and `.tmp.*`
      - if found, prepend the relative path to ref_images and update
        outfit.photo / avai_url where applicable
      - if multiple candidates, pick the freshest mtime

    Returns {'relinked': [{kind, id, name, files: [...]}, ...]}.
    Idempotent — running twice does nothing the second time."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    base = series_path(sid)
    relinked = []

    def _scan_dir(d, name_stem):
        """Find files in dir whose stem matches name_stem (case-insensitive),
        sorted by mtime descending. Skip hidden / tmp."""
        if not d.exists():
            return []
        out = []
        target = name_stem.upper()
        for p in d.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith('._') or '.tmp.' in p.name:
                continue
            if p.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                continue
            stem_upper = p.stem.upper()
            if stem_upper == target or stem_upper.startswith(target + '_') or stem_upper == target + '_BASE':
                out.append(p)
        out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return out

    # Characters
    for c in s.get('characters', []):
        if c.get('ref_images'):
            continue
        slug = slugify(c['name'])
        char_dir = base / 'assets' / 'characters' / slug
        stem = asset_name(c['name'], 'BASE')  # canonical
        # Try BASE first, then bare name.
        cands = _scan_dir(char_dir, stem)
        if not cands:
            cands = _scan_dir(char_dir, asset_name(c['name']))
        if not cands:
            continue
        rels = [str(p.relative_to(base)) for p in cands]
        c['ref_images'] = rels
        # Restore base outfit photo if it's marked is_base and empty.
        for o in c.get('outfits', []) or []:
            if o.get('is_base') and not o.get('photo'):
                o['photo'] = rels[0]
        relinked.append({'kind': 'char', 'id': c['id'], 'name': c['name'], 'files': rels})

    # Locations
    for l in s.get('locations', []):
        if l.get('ref_images'):
            continue
        slug = slugify(l['name'])
        loc_dir = base / 'assets' / 'locations' / slug
        cands = _scan_dir(loc_dir, asset_name(l['name']))
        if not cands:
            continue
        rels = [str(p.relative_to(base)) for p in cands]
        l['ref_images'] = rels
        relinked.append({'kind': 'loc', 'id': l['id'], 'name': l['name'], 'files': rels})

    # Items
    for it in s.get('items', []):
        if it.get('ref_images'):
            continue
        slug = slugify(it['name'])
        item_dir = base / 'assets' / 'items' / slug
        cands = _scan_dir(item_dir, asset_name(it['name']))
        if not cands:
            continue
        rels = [str(p.relative_to(base)) for p in cands]
        it['ref_images'] = rels
        relinked.append({'kind': 'item', 'id': it['id'], 'name': it['name'], 'files': rels})

    if relinked:
        save_series(sid, s)
    return jsonify({'relinked': relinked, 'count': len(relinked)})


@app.route('/api/series/<sid>/debug-asset')
def debug_asset(sid):
    """Diagnostic for the broken-image placeholder. Reports filesystem state
    of an asset path the UI failed to load: existence, size, mtime, mime, and
    whether the path appears in the parent series.json's ref lists. The user
    pastes this back to support so we can tell whether the file vanished off
    disk vs got dropped from refs vs was never there."""
    rel_path = (request.args.get('path') or '').strip()
    if not rel_path:
        return jsonify({'error': 'path required'}), 400
    base = series_path(sid).resolve()
    full = (base / rel_path).resolve()
    # Path traversal guard.
    try:
        full.relative_to(base)
    except ValueError:
        return jsonify({'error': 'path escapes series dir'}), 400
    out = {
        'sid': sid,
        'rel_path': rel_path,
        'absolute_path': str(full),
        'exists': full.exists(),
        'is_file': full.is_file() if full.exists() else False,
        'parent_exists': full.parent.exists(),
        'parent_listing': [],
        'in_refs': [],
    }
    if full.exists() and full.is_file():
        try:
            st = full.stat()
            import mimetypes
            out['size_bytes'] = st.st_size
            out['mtime_iso']  = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
            out['mime_guess'] = mimetypes.guess_type(str(full))[0]
            with open(full, 'rb') as f:
                head = f.read(16)
            out['magic_hex'] = head.hex()
            out['magic_kind'] = (
                'jpeg' if head[:3] == b'\xff\xd8\xff' else
                'png'  if head[:8] == b'\x89PNG\r\n\x1a\n' else
                'webp' if head[8:12] == b'WEBP' else
                'unknown'
            )
        except Exception as e:
            out['stat_error'] = str(e)
    if full.parent.exists():
        try:
            out['parent_listing'] = sorted([
                p.name for p in full.parent.iterdir()
                if not p.name.startswith('._') and '.tmp.' not in p.name
            ])[:50]
        except Exception as e:
            out['parent_listing_error'] = str(e)
    # Look up the ref in series.json so we know whether the path is even valid
    # from the data layer's POV.
    try:
        s = load_series(sid)
        if s:
            for c in s.get('characters', []):
                if rel_path in (c.get('ref_images') or []):
                    out['in_refs'].append({'kind': 'char', 'id': c.get('id'), 'name': c.get('name')})
                for o in (c.get('outfits') or []):
                    if o.get('photo') == rel_path:
                        out['in_refs'].append({'kind': 'outfit', 'char_id': c.get('id'),
                                               'outfit_id': o.get('id'), 'label': o.get('label')})
            for l in s.get('locations', []):
                if rel_path in (l.get('ref_images') or []):
                    out['in_refs'].append({'kind': 'loc', 'id': l.get('id'), 'name': l.get('name')})
            for it in s.get('items', []):
                if rel_path in (it.get('ref_images') or []):
                    out['in_refs'].append({'kind': 'item', 'id': it.get('id'), 'name': it.get('name')})
    except Exception as e:
        out['series_load_error'] = str(e)
    return jsonify(out)


# ── Reteller ─────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/reteller/preview', methods=['POST'])
def reteller_preview(sid):
    data = request.json
    ep_from = int(data['from'])
    ep_to = int(data['to'])

    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404

    episodes = list_episodes(sid)
    range_eps = [e for e in episodes if ep_from <= e['number'] <= ep_to]

    # Per-character: collect all outfit IDs used across the range + episode list
    char_outfits_used = {}  # cid -> {outfit_id: [ep_numbers]}
    char_ids_used = set()
    for ep in range_eps:
        for cid in ep.get('characters_used', []):
            char_ids_used.add(cid)
        for cid, raw in (ep.get('character_outfits') or {}).items():
            char_ids_used.add(cid)
            for oid in _outfit_ids(raw):
                char_outfits_used.setdefault(cid, {}).setdefault(oid, []).append(ep['number'])

    chars_map = {c['id']: c for c in s['characters']}
    chars_used = []
    for cid in char_ids_used:
        if cid in chars_map:
            c = dict(chars_map[cid])
            c['has_refs'] = len(c.get('ref_images', [])) > 0
            c['ref_urls'] = [f'/assets/{sid}/{r}' for r in c.get('ref_images', [])]
            # Build outfit summary for this range
            outfits_map = {o['id']: o for o in c.get('outfits', [])}
            outfits_in_range = []
            for oid, ep_nums in char_outfits_used.get(cid, {}).items():
                o = outfits_map.get(oid)
                if not o:
                    continue
                photo_url = ''
                if o.get('photo'):
                    photo_url = f'/assets/{sid}/{o["photo"]}'
                elif o.get('avai_url'):
                    photo_url = o['avai_url']
                outfits_in_range.append({
                    'id': oid,
                    'label': o.get('label', ''),
                    'description': o.get('description', ''),
                    'photo_url': photo_url,
                    'has_photo': bool(photo_url),
                    'episodes': sorted(set(ep_nums)),
                })
            outfits_in_range.sort(key=lambda x: (min(x['episodes']) if x['episodes'] else 999, x['label']))
            c['outfits_in_range'] = outfits_in_range
            chars_used.append(c)

    # Locations used in range
    loc_ids_used = set()
    for ep in range_eps:
        for lid in ep.get('locations_used', []):
            loc_ids_used.add(lid)
    locs_map = {l['id']: l for l in s.get('locations', [])}
    locs_used = []
    for lid in loc_ids_used:
        if lid in locs_map:
            l = dict(locs_map[lid])
            l['has_refs'] = len(l.get('ref_images', [])) > 0
            l['ref_urls'] = [f'/assets/{sid}/{r}' for r in l.get('ref_images', [])]
            locs_used.append(l)

    style = dict(s['style'])
    style['ref_urls'] = [f'/assets/{sid}/{r}' for r in style.get('ref_images', [])]

    return jsonify({
        'episodes': range_eps,
        'characters': chars_used,
        'locations': locs_used,
        'style': style,
        'settings': s['settings']
    })

@app.route('/api/series/<sid>/reteller/submit', methods=['POST'])
def reteller_submit(sid):
    data = request.json
    ep_from = int(data['from'])
    ep_to = int(data['to'])
    settings_override = data.get('settings', {})

    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404

    episodes = list_episodes(sid)
    range_eps = [e for e in episodes if ep_from <= e['number'] <= ep_to]
    chars_map = {c['id']: c for c in s['characters']}
    headers = rtl_headers()
    results = []

    for ep in range_eps:
        merged = {**s['settings'], **settings_override}

        # Collect characters and their local ref files
        # Sends each outfit (and the base, when it differs) as a separate characterRef
        # named CHARNAME_OUTFITLABEL.png so Reteller's char-references list is meaningful.
        char_meta = []
        char_files = []  # list of (path, display_name) tuples
        seen_paths = set()
        ep_outfits = ep.get('character_outfits', {})
        locs_map = {l['id']: l for l in s.get('locations', [])}

        for cid in ep.get('characters_used', []):
            c = chars_map.get(cid)
            if not c:
                continue
            char_meta.append({
                'name': c['name'],
                'description': (c.get('description', '') + ' ' + c.get('appearance', '')).strip(),
                'isCharacter': True,
                'gender': c.get('gender', 'female'),
                'voiceId': c.get('voice_id', '')
            })

            # Episode may use MULTIPLE outfits per character (he/she changes clothes
            # within one episode). Send each outfit photo as a separate characterRef
            # so Reteller has a visual anchor for every look the script demands.
            outfit_ids_list = _outfit_ids(ep_outfits.get(cid))
            outfits_with_photos = []
            for oid in outfit_ids_list:
                o = next((x for x in c.get('outfits', []) if x['id'] == oid), None)
                if o and o.get('photo'):
                    outfits_with_photos.append(o)

            if outfits_with_photos:
                for outfit in outfits_with_photos:
                    p = series_path(sid) / outfit['photo']
                    if p.exists() and str(p) not in seen_paths:
                        display = f'{asset_name(c["name"], outfit["label"])}{p.suffix or ".png"}'
                        char_files.append((p, display))
                        seen_paths.add(str(p))
            else:
                # No outfit chosen or none of the chosen outfits has a photo —
                # fall back to base ref(s)
                for rel in c.get('ref_images', []):
                    p = series_path(sid) / rel
                    if p.exists() and str(p) not in seen_paths:
                        display = f'{asset_name(c["name"], "BASE")}{p.suffix or ".png"}'
                        char_files.append((p, display))
                        seen_paths.add(str(p))

        # Locations — send into characterRefs (Reteller's asset library) AND register
        # them in characterMeta with `isCharacter: False` so Reteller doesn't treat
        # the file as an orphaned upload. Without the meta entry the file lands in
        # the project but never gets exposed to the prompt-resolver.
        for lid in ep.get('locations_used', []):
            loc = locs_map.get(lid)
            if not loc:
                continue
            ref_added = False
            for rel in loc.get('ref_images', []):
                p = series_path(sid) / rel
                if p.exists() and str(p) not in seen_paths:
                    display = f'{asset_name(loc["name"])}{p.suffix or ".png"}'
                    char_files.append((p, display))
                    seen_paths.add(str(p))
                    ref_added = True
            if ref_added:
                char_meta.append({
                    'name': loc['name'],
                    'description': loc.get('description', ''),
                    'isCharacter': False,
                    'isLocation': True,
                })

        style_files = []  # list of (path, display_name)
        for rel in s['style'].get('ref_images', []):
            p = series_path(sid) / rel
            if p.exists():
                style_files.append((p, f'STYLE_{p.stem.upper()}{p.suffix or ".png"}'))

        # Map our internal video model id → exactly what Reteller's UI shows in the
        # "Animation model" dropdown. Reteller's public /docs only lists 3 names, but
        # the live UI also accepts seedance-2-ref / seedance-2-pro etc, so we pass
        # them through unchanged when the value is already a Reteller-known slug.
        _video_model_map = {
            'seedance-2-ref': 'seedance-2-ref',  # → "Seedance 2.0 Ref"
            'seedance-2':     'seedance-2',
            'seedance15':     'seedance15',
            'grok_video':     'grok_video',
            'veo31_fast':     'veo31_fast',
        }
        # Auto-heal legacy settings: older series were saved with duration=90 / no enable_grid
        # / wrong animation_model. Force-correct them here AND persist back to series.json so
        # the UI panel reflects the updated values next time the user opens settings.
        legacy_fixed = False
        if not str(s['settings'].get('animation_model','')).strip() or s['settings'].get('animation_model') == 'seedance-2':
            s['settings']['animation_model'] = 'seedance-2-ref'; legacy_fixed = True
        if s['settings'].get('duration') in (None, 90, 60, 120, 180):
            s['settings']['duration'] = 'auto-frames'; legacy_fixed = True
        if 'enable_grid' not in s['settings']:
            s['settings']['enable_grid'] = False; legacy_fixed = True
        if not s['settings'].get('no_fades'):
            s['settings']['no_fades'] = True; legacy_fixed = True
        if legacy_fixed:
            save_series(sid, s)
            merged = {**s['settings'], **settings_override}

        video_gen = _video_model_map.get(merged.get('animation_model', 'seedance-2-ref'),
                                         merged.get('animation_model', 'seedance-2-ref'))

        # Reteller settings — every field shown in the UI's "Retelling Settings" panel
        # (section 5: Video Duration / Aspect Ratio / Language / Generator / Multi-voice
        # / Animation / Cinema / Trim / No fades / Music). Field names mirror the
        # camelCase that the live API + frontend uses; values come from the series
        # defaults so a fresh draft already matches the user's preferred panel state.
        settings_payload = {
            # Section 5 — Video Duration row. "auto-frames" = Reteller's Auto-frames mode.
            'duration':            merged.get('duration', 'auto-frames'),
            'autoFrames':          merged.get('duration', 'auto-frames') == 'auto-frames',
            # Aspect Ratio
            'aspectRatio':         merged.get('aspect_ratio', '9:16'),
            # Language
            'language':            merged.get('language', 'English'),
            # Generator (image provider) — UI label "Banana Pro"
            'imageProvider':       merged.get('image_provider', 'banana'),
            # Multi-voice narration
            'multiVoice':          merged.get('multi_voice', False),
            # Animation strip
            'enableAnimation':     merged.get('enable_animation', True),
            'animationSpeed':      merged.get('animation_speed', 'fast'),       # fast | normal
            'animationResolution': merged.get('animation_resolution', '480p'),  # 480p | 720p
            'videoGenerator':      video_gen,                                   # → "Seedance 2.0 Ref"
            'animationModel':      video_gen,                                   # alias the UI sometimes reads
            # Animation grid (frame-grid overlay) — must stay OFF for Seedance Ref
            'enableGrid':          bool(merged.get('enable_grid', False)),
            'grid':                bool(merged.get('enable_grid', False)),     # alias for older Reteller schema
            # Cinema toggle
            'cinema':              merged.get('cinema', False),
            # Trim toggle
            'trim':                merged.get('trim', True),
            # No fades toggle — must be ON
            'noFades':             bool(merged.get('no_fades', True)),
            # Music toggle + volume slider (0–1; 0.3 = 30%)
            'enableMusic':         merged.get('enable_music', True),
            'musicVolume':         merged.get('music_volume', 0.30),
            # Subtitles (currently UI-hidden but the API accepts it)
            'enableSubtitles':     merged.get('enable_subtitles', False),
            # Voice / TTS — used inside Reteller for narration even when multi-voice is off
            'voice':               merged.get('voice', 'Enceladus'),
            'ttsProvider':         merged.get('tts_provider', 'elevenlabs'),
            # Style block
            'style':               s['style'].get('type', 'cinematic'),
            'imageSize':           merged.get('image_size', '1K'),
        }
        if merged.get('elevenlabs_voice_id'):
            settings_payload['elevenlabsVoiceId'] = merged['elevenlabs_voice_id']
        if s['style'].get('custom_description'):
            settings_payload['customStyleDescription'] = s['style']['custom_description']

        ep_title = f'{s["title"]} — Ep.{ep["number"]:02d} {ep["title"]}'
        # Use structured Reteller prompt if available, fall back to raw script, then synopsis
        ep_text = ep.get('reteller_prompt') or ep.get('script') or ep.get('synopsis') or f'Episode {ep["number"]}: {ep["title"]}'

        has_files = char_files or style_files
        opened_files = []

        try:
            if has_files:
                form_data = {
                    'title': ep_title,
                    # Reteller schema: `text` = source content to retell, `userInstruction` = AI directives.
                    # Our generated script IS the directive (cast block + scenes + episode notes), so it
                    # belongs in userInstruction. Sending it as `text` makes Reteller treat it as a
                    # passive content source (the "content.txt" Content source you saw in the UI).
                    'userInstruction': ep_text,
                    'autoStart': 'false',  # DRAFT mode — user reviews on Reteller's page and starts manually
                    'settings': json.dumps(settings_payload, ensure_ascii=False),
                    'characterMeta': json.dumps(char_meta, ensure_ascii=False),
                }
                multipart = []
                for p, display in char_files:
                    fobj = open(p, 'rb')
                    opened_files.append(fobj)
                    mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
                    multipart.append(('characterRefs', (display, fobj, mime)))
                for p, display in style_files:
                    fobj = open(p, 'rb')
                    opened_files.append(fobj)
                    mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
                    multipart.append(('styleRefs', (display, fobj, mime)))

                resp = requests.post(
                    f'{RETELLER_API}/projects',
                    headers=headers,
                    data=form_data,
                    files=multipart,
                    timeout=30
                )
            else:
                payload = {
                    'title':           ep_title,
                    'userInstruction': ep_text,  # AI directive (script), not raw retelling source
                    'autoStart':       False,    # DRAFT mode
                    'settings':        settings_payload,
                }
                if char_meta:
                    payload['characters'] = [{
                        'name': m['name'],
                        'description': m.get('description', ''),
                        'isCharacter': bool(m.get('isCharacter', True)),
                        'isLocation':  bool(m.get('isLocation', False)),
                        'gender':      m.get('gender', ''),
                    } for m in char_meta]

                resp = requests.post(
                    f'{RETELLER_API}/projects',
                    headers=headers,
                    json=payload,
                    timeout=30
                )
        finally:
            for fobj in opened_files:
                fobj.close()

        entry = {'episode': ep['number'], 'http_status': resp.status_code}
        if resp.ok:
            rdata = resp.json()
            project_id = rdata.get('projectId')
            project_url = rdata.get('projectUrl') or rdata.get('url') or (f'https://reteller.ai/projects/{project_id}' if project_id else '')
            entry['project_id']  = project_id
            entry['project_url'] = project_url
            entry['reteller_status'] = rdata.get('status', 'draft')

            ep['reteller']['project_id']   = project_id
            ep['reteller']['project_url']  = project_url
            ep['reteller']['status']       = rdata.get('status', 'draft')
            ep['reteller']['submitted_at'] = datetime.datetime.utcnow().isoformat()
            ep['status'] = 'draft_in_reteller'
            ep['ready'] = True  # sending to reteller marks the episode as done
            save_episode(sid, ep['number'], ep)
        else:
            entry['error'] = resp.text

        results.append(entry)

    return jsonify(results)

@app.route('/api/series/<sid>/reteller/status/<project_id>', methods=['GET'])
def reteller_status(sid, project_id):
    resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}',
        headers=rtl_headers(),
        timeout=15
    )
    if not resp.ok:
        return jsonify({'error': resp.text}), resp.status_code

    rdata = resp.json()
    status = rdata.get('status')

    # Sync episode status
    for ep in list_episodes(sid):
        if ep.get('reteller', {}).get('project_id') == project_id:
            if status in ('completed', 'error'):
                ep['reteller']['status'] = status
                if status == 'completed':
                    ep['reteller']['video_url'] = rdata.get('videoUrl')
                ep['status'] = status
                save_episode(sid, ep['number'], ep)
            break

    return jsonify(rdata)

@app.route('/api/reteller/balance')
def reteller_balance():
    resp = requests.get(f'{RETELLER_API}/balance', headers=rtl_headers(), timeout=10)
    return jsonify(resp.json() if resp.ok else {'error': resp.text})

@app.route('/api/reteller/voices')
def reteller_voices():
    resp = requests.get(f'{RETELLER_API}/voices', headers=rtl_headers(), timeout=10)
    return jsonify(resp.json() if resp.ok else {})

@app.route('/api/reteller/styles')
def reteller_styles():
    resp = requests.get(f'{RETELLER_API}/styles', headers=rtl_headers(), timeout=10)
    return jsonify(resp.json() if resp.ok else {})


# ════════════════════════════════════════════════════════════════════════════
# SEEDANCE 2.0 — video generation via AVAI Gen
# Async flow: POST → 202 {job_id, status_url} → poll status_url → download mp4
# State persisted on episode JSON: episode['seedance_chunks'] = [
#   {idx, job_id, status, video_url, video_path, prompt, ref_urls,
#    chunk_text, duration, resolution, moderation_bypass, ending_state, cost}
# ]
# ════════════════════════════════════════════════════════════════════════════

def _seedance_chunks(ep):
    return ep.setdefault('seedance_chunks', [])

def _next_chunk_idx(ep):
    chunks = _seedance_chunks(ep)
    return (max((c.get('idx', -1) for c in chunks), default=-1)) + 1

def _extract_last_frame(sid, video_relpath):
    """Extract a JPEG of the last frame of a chunk's video.
    Cached: returns (relpath, abs_path). Re-extracted if missing.
    """
    src = series_path(sid) / video_relpath
    if not src.exists():
        return None
    out = src.with_name(src.stem + '_lastframe.png')
    if out.exists() and out.stat().st_size > 0:
        return (str(out.relative_to(series_path(sid))), out)
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return None
    try:
        # -sseof -0.1 → seek to 0.1s before end (works without re-encode).
        # PNG = lossless. Seedance uses these as input refs, so any JPEG
        # compression artifacts get amplified in the next chunk's first frame
        # (visible as quality drop at chunk transitions). PNG eliminates that.
        # `-pred mixed -compression_level 1` = fast PNG encode (~50ms vs 200ms default).
        subprocess.run(
            [ffmpeg_bin, '-y', '-sseof', '-0.1', '-i', str(src),
             '-frames:v', '1', '-c:v', 'png', '-pred', 'mixed', '-compression_level', '1',
             str(out)],
            capture_output=True, timeout=30, check=True
        )
        if out.exists() and out.stat().st_size > 0:
            return (str(out.relative_to(series_path(sid))), out)
    except Exception:
        return None
    return None


def _detect_cuts(video_abs_path, threshold=0.35):
    """Detect hard cuts inside a video using ffmpeg's scene detection.
    Returns sorted list of cut timestamps (seconds, float) where the FIRST
    frame of the NEW shot starts. Returns [] on any failure or if ffmpeg
    is not installed. Threshold: 0.3-0.45 catches most AI-generated cuts."""
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin or not video_abs_path or not Path(video_abs_path).exists():
        return []
    try:
        # showinfo prints pts_time for each frame the select filter passes.
        proc = subprocess.run(
            [ffmpeg_bin, '-hide_banner', '-i', str(video_abs_path),
             '-vf', f"select='gt(scene,{threshold})',showinfo",
             '-an', '-f', 'null', '-'],
            capture_output=True, timeout=60, text=True
        )
        # ffmpeg writes filter output to stderr
        out = (proc.stderr or '') + (proc.stdout or '')
    except Exception:
        return []
    cuts = []
    for m in re.finditer(r'pts_time:([\d.]+)', out):
        try:
            t = float(m.group(1))
            # Drop very-early "cuts" (often the first frame itself).
            if t > 0.4:
                cuts.append(t)
        except ValueError:
            continue
    # Dedupe near-duplicates (within 0.3s of each other) — sometimes ffmpeg
    # emits 2 close hits for the same cut due to motion.
    cuts.sort()
    deduped = []
    for t in cuts:
        if not deduped or (t - deduped[-1]) > 0.3:
            deduped.append(t)
    return deduped


def _extract_keyframes_at_cuts(sid, video_relpath, cut_timestamps, max_frames=3,
                               pre_offset=0.05):
    """For each cut timestamp T, extract the frame at T-pre_offset (i.e. the
    LAST frame of the OUTGOING shot, just before the cut). Cached on disk as
    <stem>_cutframe_<i>.png (lossless — Seedance uses these as input refs and
    JPEG artifacts compound at chunk boundaries). Caps at max_frames (oldest
    cuts first → most context) to keep ref budget under control. Returns list
    of (relpath, abs_path)."""
    src = series_path(sid) / video_relpath
    if not src.exists() or not cut_timestamps:
        return []
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return []
    selected = list(cut_timestamps)[:max_frames]
    out_paths = []
    for i, t in enumerate(selected):
        seek_t = max(0.0, t - pre_offset)
        out = src.with_name(f'{src.stem}_cutframe_{i}.png')
        if out.exists() and out.stat().st_size > 0:
            out_paths.append((str(out.relative_to(series_path(sid))), out))
            continue
        try:
            subprocess.run(
                [ffmpeg_bin, '-y', '-ss', f'{seek_t:.3f}', '-i', str(src),
                 '-frames:v', '1', '-c:v', 'png', '-pred', 'mixed', '-compression_level', '1',
                 str(out)],
                capture_output=True, timeout=30, check=True
            )
            if out.exists() and out.stat().st_size > 0:
                out_paths.append((str(out.relative_to(series_path(sid))), out))
        except Exception:
            continue
    return out_paths

def _avai_seedance_start(prompt, ref_urls, duration, resolution, moderation_bypass,
                          aspect_ratio='9:16', generate_audio=True,
                          moderation_bypass_prompt=None, avai_key=None):
    """Kick off an async Seedance 2.0 reference-pro job.
    Returns dict {job_id, status_url, raw}.

    avai_key: REQUIRED when called from a background thread (no Flask request
    context). Caller must resolve it via _get_user_avai_key() inside the request
    handler and pass it explicitly. Falls back to _get_user_avai_key() only when
    called inline from a request handler (image-style sync calls)."""
    payload = {
        'provider': 'seedance2',
        'model': 'reference-pro',
        'prompt': prompt,
        'duration': str(int(duration)),
        'resolution': resolution,        # '720p' | '480p'
        'aspect_ratio': aspect_ratio,
        'generate_audio': bool(generate_audio),
        'num_outputs': 1,
    }
    if ref_urls:
        payload['contextImages'] = [{'url': u} for u in ref_urls if u][:9]
    if moderation_bypass and moderation_bypass != 'off':
        payload['moderation_bypass'] = moderation_bypass
        if moderation_bypass_prompt:
            payload['moderation_bypass_prompt'] = moderation_bypass_prompt
    key = avai_key if avai_key is not None else _get_user_avai_key()
    if not key:
        raise RuntimeError('AVAI seedance2 start: no API key (request context lost in background thread or user has no key configured)')
    headers = {'x-api-key': key, 'content-type': 'application/json'}
    # Async mode: server returns 202 with job_id+status_url immediately
    resp = requests.post(
        AVAI_API + '?async=true', json=payload, headers=headers, timeout=(15, 240)
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f'AVAI seedance2 start error {resp.status_code}: {resp.text[:400]}')
    data = resp.json()
    job_id = data.get('job_id') or data.get('id') or data.get('message_id')
    status_url = data.get('status_url') or (
        f'/api/public/generate/jobs/{job_id}' if job_id else None
    )
    # Normalize relative URL → absolute
    if status_url and status_url.startswith('/'):
        status_url = 'https://avai-gen.com' + status_url
    if not job_id:
        raise RuntimeError(f'AVAI seedance2: no job_id in response: {str(data)[:300]}')
    return {'job_id': job_id, 'status_url': status_url, 'raw': data}

def _avai_seedance_status(job_id, status_url=None, avai_key=None):
    """Poll job. Returns dict {status, progress, video_url, cost, error, raw}.
    avai_key: optional explicit override for callers outside request context.

    Retries on transient network errors (SSL EOF, connection reset, timeout)
    — common when AVAI restarts a worker or an intermediate proxy hiccups.
    Without retry a SINGLE network blip during poll permanently marks the
    chunk as failed (caller catches the exception and writes status='failed'),
    erasing minutes of actual work."""
    key = avai_key if avai_key is not None else _get_user_avai_key()
    headers = {'x-api-key': key}
    url = status_url or f'https://avai-gen.com/api/public/generate/jobs/{job_id}'
    if url.startswith('/'):
        url = 'https://avai-gen.com' + url
    last_err = None
    for attempt in range(3):   # 3 attempts total
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            break
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            last_err = e
            if attempt == 2:
                # Final attempt failed — surface as a structured «pending» so
                # the caller treats it as «check again later» instead of a
                # hard failure. The reaper in /seedance/poll will eventually
                # mark it failed if the job genuinely never recovers.
                print(f'[avai-status] {job_id} network fail after 3 tries: {e.__class__.__name__}: {str(e)[:200]}', flush=True)
                return {'status': 'pending', 'progress': None, 'video_url': '', 'cost': None,
                        'error': f'transient network error: {e.__class__.__name__}', 'raw': {}}
            time.sleep(1.5 * (attempt + 1))   # 1.5s, 3s
    if not resp.ok:
        return {'status': 'error', 'error': f'{resp.status_code}: {resp.text[:200]}'}
    data = resp.json()
    status = data.get('status', 'pending').lower()
    video_url = ''
    # AVAI returns mp4 URLs in `images` (yes, the field is named that) for seedance2.
    # Also try `videos`/`outputs` for forward-compat.
    candidates = data.get('images') or data.get('videos') or data.get('outputs') or []
    if candidates:
        first = candidates[0]
        if isinstance(first, dict):
            video_url = first.get('url') or ''
        elif isinstance(first, str):
            video_url = first
    if not video_url:
        video_url = data.get('video_url') or data.get('output_url') or ''
    # cost can be a number or a dict {estimated_cost_usd: ...}
    cost_raw = data.get('cost')
    if isinstance(cost_raw, dict):
        cost_val = cost_raw.get('estimated_cost_usd') or cost_raw.get('cost') or cost_raw.get('total')
    else:
        cost_val = cost_raw
    progress = data.get('progress')
    if status == 'completed':
        progress = 100
    return {
        'status': status,
        'progress': progress,
        'video_url': video_url,
        'cost': cost_val,
        'error': data.get('error'),
        'raw': data,
    }

def _avai_upload_local_image(local_path: Path) -> str:
    """Upload a local image file to AVAI public storage, return the public URL.
    Used when an asset has only a local ref_image and we need a URL for Seedance."""
    import base64, mimetypes
    if not local_path.exists():
        raise RuntimeError(f'file not found: {local_path}')
    mime = mimetypes.guess_type(str(local_path))[0] or 'image/png'
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        mime = 'image/png'
    b64 = base64.b64encode(local_path.read_bytes()).decode('ascii')
    headers = {'x-api-key': _get_user_avai_key(), 'content-type': 'application/json'}
    resp = requests.post(
        'https://avai-gen.com/api/public/upload-image',
        json={'image_base64': b64, 'mime_type': mime},
        headers=headers, timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f'AVAI upload {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    url = (
        data.get('url')
        or data.get('image_url')
        or (data.get('data') or {}).get('url')
        or ((data.get('images') or [{}])[0] or {}).get('url')
    )
    if not url:
        raise RuntimeError(f'AVAI upload: no url in response: {str(data)[:300]}')
    return url

def _ensure_loc_avai_url(sid, loc):
    """Locations created before Seedance was added store only ref_images. Lazy-upload
    the first ref to AVAI to get a public URL, persist it on the loc."""
    if loc.get('avai_url'):
        return loc['avai_url']
    refs = loc.get('ref_images') or []
    if not refs:
        return None
    local = series_path(sid) / refs[0]
    if not local.exists():
        return None
    try:
        url = _avai_upload_local_image(local)
        loc['avai_url'] = url
        return url
    except Exception as e:
        print(f'[seedance] loc upload failed for {loc.get("name")}: {e}')
        return None

def _ensure_char_avai_base_url(sid, char):
    """Lazy-upload the character's first local ref_image to AVAI when
    avai_base_url is missing. Persists the URL on the char dict in-memory
    (caller must save_series). Returns the URL or None."""
    if char.get('avai_base_url'):
        return char['avai_base_url']
    refs = char.get('ref_images') or []
    if not refs:
        return None
    local = series_path(sid) / refs[0]
    if not local.exists():
        return None
    try:
        url = _avai_upload_local_image(local)
        char['avai_base_url'] = url
        return url
    except Exception as e:
        print(f'[seedance] char base upload failed for {char.get("name")}: {e}')
        return None

def _resolve_ref_url(s, ref, sid=None):
    """ref = {'kind':'char'|'outfit'|'loc', 'id':..., 'outfit':...}.
    Returns public AVAI URL or None.

    Side-effect: if a non-null outfit label was requested but doesn't match any
    of the character's outfits, marks ref['_outfit_fallback']=True so the caller
    can warn the user. Without this signal, hallucinated outfit labels (e.g.
    'casual', 'formal') silently fall through to base — outfit drift bug."""
    if not ref:
        return None
    kind = ref.get('kind')
    if kind == 'char':
        c = next((x for x in s.get('characters', []) if x['id'] == ref.get('id')), None)
        if not c:
            return None
        requested = ref.get('outfit')
        if requested:
            for o in c.get('outfits', []) or []:
                if o.get('label') == requested:
                    return o.get('avai_url') or c.get('avai_base_url')
            # Outfit label was requested but not found → fall back to base, but mark it
            ref['_outfit_fallback'] = True
            ref['_outfit_requested'] = requested
        # Base look. Lazy-upload local ref_image if avai_base_url absent.
        if c.get('avai_base_url'):
            return c['avai_base_url']
        if sid:
            url = _ensure_char_avai_base_url(sid, c)
            if url:
                save_series(sid, s)
                return url
        return None
    if kind == 'loc':
        l = next((x for x in s.get('locations', []) if x['id'] == ref.get('id')), None)
        if not l:
            return None
        url = l.get('avai_url')
        if not url and sid:
            url = _ensure_loc_avai_url(sid, l)
            if url:
                save_series(sid, s)  # persist new avai_url
        return url
    if kind == 'item':
        # Plot-relevant items (locket, USB stick, bouquet, etc.). LLM picks
        # them when the chunk text mentions the object visually — they go
        # into Seedance refs as an extra @ImageN slot so the rendered video
        # can carry the prop with consistent appearance.
        it = next((x for x in s.get('items', []) if x['id'] == ref.get('id')), None)
        if not it:
            return None
        url = it.get('avai_url')
        if not url:
            # Lazy-fallback: if avai_url is missing but we have a local ref_image,
            # we can't upload it without _ensure_item_avai_url (which doesn't
            # exist yet). Return None and let the caller log it.
            pass
        return url
    if kind == 'url':
        return ref.get('url')
    if kind == 'lastframe':
        # Continuity reference: URL is already resolved & uploaded by /compose
        return ref.get('url')
    return None

def _download_video(url, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return dest_path

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/upload-ref', methods=['POST'])
def seedance_upload_ref(sid, num):
    """Upload an arbitrary image (file from desktop or URL) to AVAI storage,
    return public URL the user can drop into Seedance refs."""
    f = request.files.get('file')
    if f:
        import base64
        data = f.read()
        mime = f.mimetype or 'image/png'
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            mime = 'image/png'
        try:
            headers = {'x-api-key': _get_user_avai_key(), 'content-type': 'application/json'}
            resp = requests.post(
                'https://avai-gen.com/api/public/upload-image',
                json={'image_base64': base64.b64encode(data).decode('ascii'),
                      'mime_type': mime},
                headers=headers, timeout=120,
            )
            if not resp.ok:
                return jsonify({'error': f'AVAI upload {resp.status_code}: {resp.text[:200]}'}), 500
            d = resp.json()
            url = (
                d.get('url') or d.get('image_url')
                or (d.get('data') or {}).get('url')
                or ((d.get('images') or [{}])[0] or {}).get('url')
            )
            if not url:
                return jsonify({'error': f'no url in response: {str(d)[:200]}'}), 500
            return jsonify({'url': url, 'name': f.filename or 'custom'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    body = request.json or {}
    src_url = (body.get('url') or '').strip()
    if not src_url:
        return jsonify({'error': 'no file or url'}), 400
    # Download then re-upload (so AVAI hosts it; some Seedance refs need their CDN)
    try:
        r = requests.get(src_url, timeout=60)
        r.raise_for_status()
        import base64
        mime = r.headers.get('content-type', 'image/png').split(';')[0]
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            mime = 'image/png'
        headers = {'x-api-key': _get_user_avai_key(), 'content-type': 'application/json'}
        resp = requests.post(
            'https://avai-gen.com/api/public/upload-image',
            json={'image_base64': base64.b64encode(r.content).decode('ascii'),
                  'mime_type': mime},
            headers=headers, timeout=120,
        )
        if not resp.ok:
            return jsonify({'error': f'AVAI upload {resp.status_code}: {resp.text[:200]}'}), 500
        d = resp.json()
        url = d.get('url') or d.get('image_url') or (d.get('data') or {}).get('url')
        return jsonify({'url': url, 'name': src_url.split('/')[-1][:40]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/list')
def seedance_list(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    chunks = _seedance_chunks(ep)
    # One-shot cleanup: wipe stale `error` strings that were left over from a
    # transient failure (AVAI 401 etc.) on chunks that ultimately rendered
    # successfully. Without this old chunks keep showing red "401: Unauthorized"
    # under the video preview forever even though they're completed.
    healed = False
    for c in chunks:
        if c.get('status') == 'completed' and c.get('video_path') and c.get('error'):
            c.pop('error', None)
            healed = True
    if healed:
        with _episode_lock(sid, num):
            ep2 = load_episode(sid, num) or ep
            for c in _seedance_chunks(ep2):
                if c.get('status') == 'completed' and c.get('video_path') and c.get('error'):
                    c.pop('error', None)
            save_episode(sid, num, ep2)
            chunks = _seedance_chunks(ep2)
    return jsonify({'chunks': chunks})


@app.route('/api/series/<sid>/episodes/<int:num>/auto-assemble', methods=['POST'])
def auto_assemble_episode(sid, num):
    """Stitch all completed seedance chunks of ONE episode into a single mp4.

    Used by the range-generation queue when `auto_assemble` is on:
    once Auto-mode finishes for an episode, the frontend hits this endpoint
    to produce a downloadable final cut without manual timeline work.

    Logic:
      - Collect chunks where status='completed' and video_path exists.
      - Order by `script_order` (set on /seedance/start) — falls back to idx.
      - If query/body `require_all=true` (default), refuse when any segment
        from the episode's expected scene-segment list is missing — frontend
        passes `expected_segments` count to gate.
      - Concat-copy via ffmpeg (no re-encode), output to OUT/<title>_E<num>.mp4.
      - Returns {ok, path, url, size_mb, chunks}.
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'episode not found'}), 404

    body = request.json or {}
    require_all = body.get('require_all', True)
    expected_segments = body.get('expected_segments')   # optional, set by frontend from script
    # Auto-insert facade clip at each scene boundary. Defaults ON so newly
    # assembled episodes naturally open every venue with its building shot.
    # User can pass false to get the pure chunk concat (legacy behaviour).
    insert_facades = body.get('insert_facades', True)

    all_chunks = [c for c in (_seedance_chunks(ep) or [])
                  if c.get('status') == 'completed' and c.get('video_path')]
    if not all_chunks:
        return jsonify({'error': 'нет готовых чанков для сборки'}), 400

    # Dedup: when the user retried/healed a chunk that already had a video,
    # we get multiple completed chunks with the SAME script_order. Pick the
    # NEWEST per position (highest idx, since idx is monotonically increasing
    # per /seedance/start; created_at as tiebreak for chunks that share idx
    # across legacy data). Previously we kept the FIRST (oldest), which meant
    # auto-assemble ignored user's manual reruns.
    #
    # Chunks lacking script_order go after the indexed ones, in their own
    # order by idx (legacy behavior — preserved so old data still assembles).
    by_order = {}
    no_order = []
    for c in all_chunks:
        so = c.get('script_order')
        if isinstance(so, int):
            prev = by_order.get(so)
            if prev is None:
                by_order[so] = c
            else:
                # Prefer the one with bigger idx; tie-break with created_at.
                cur_key = (c.get('idx') or 0, c.get('created_at') or 0)
                prev_key = (prev.get('idx') or 0, prev.get('created_at') or 0)
                if cur_key > prev_key:
                    by_order[so] = c
        else:
            no_order.append(c)
    chunks = [by_order[k] for k in sorted(by_order.keys())] + sorted(
        no_order, key=lambda c: c.get('idx') or 0
    )

    if require_all and isinstance(expected_segments, int) and expected_segments > 0:
        if len(chunks) < expected_segments:
            return jsonify({
                'error': 'не все сегменты готовы',
                'have': len(chunks),
                'expected': expected_segments,
            }), 409

    base = series_path(sid)

    # ── Facade auto-insert ────────────────────────────────────────────────
    # Build loc_id → facade record map. A facade «owns» the locations listed
    # in its member_loc_ids[]. If a chunk's primary loc transitions to one
    # owned by a different facade than the previous chunk used, we prepend
    # the new facade's video clip to introduce the venue. The very first
    # chunk also triggers an insert (scene opens from nothing).
    facade_by_loc = {}
    facades_inserted = 0
    if insert_facades:
        for f in (s.get('location_facades') or []):
            if (f.get('status') in ('ready',)) and f.get('video_path'):
                for lid in (f.get('member_loc_ids') or []):
                    facade_by_loc[lid] = f
    def _chunk_primary_loc(c):
        for r in (c.get('refs') or []):
            if r.get('kind') == 'loc' and r.get('id'):
                return r['id']
        return None

    seg_paths = []
    prev_facade_id = None   # which facade we last opened with (None at start)
    for c in chunks:
        p = base / c['video_path']
        if not p.exists():
            return jsonify({'error': f"file missing: {c['video_path']}"}), 400
        # Decide whether to drop in a facade BEFORE this chunk
        if insert_facades:
            loc_id = _chunk_primary_loc(c)
            fac = facade_by_loc.get(loc_id) if loc_id else None
            if fac and fac.get('id') != prev_facade_id:
                fac_video = base / fac['video_path']
                if fac_video.exists():
                    seg_paths.append(str(fac_video))
                    facades_inserted += 1
                prev_facade_id = fac.get('id')
            elif fac:
                # Same facade as last chunk — same scene continues, no insert.
                pass
            else:
                # Chunk's loc has no facade → don't reset prev_facade_id; treat
                # as continuation of the previous scene visually. (Exterior
                # locations typically don't need a facade intro since the
                # location image itself shows the surroundings.)
                pass
        seg_paths.append(str(p))

    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return jsonify({'error': 'ffmpeg не установлен. brew install ffmpeg'}), 500
    ffprobe_bin = shutil.which('ffprobe')   # paired with ffmpeg; both come from the same package

    # ── Per-input probe: audio presence + duration ────────────────────────
    # Facade clips are rendered without audio (generate_audio=False), regular
    # Seedance chunks have audio. Mixing them with bare concat-copy or with
    # an unconditional filter-complex `[i:a]` map produces a broken/silent
    # file. So: probe each segment, and during filter-complex synthesize a
    # matching-length silent track for any input that lacks one.
    def _probe_audio_and_duration(path):
        if not ffprobe_bin:
            return True, 5.0     # safest defaults — assume has audio, ~chunk length
        has_audio = True
        try:
            out = subprocess.check_output(
                [ffprobe_bin, '-v', 'error', '-show_entries', 'stream=codec_type',
                 '-of', 'csv=p=0', path],
                text=True, timeout=10,
            )
            has_audio = ('audio' in out)
        except Exception:
            pass
        dur = 5.0
        try:
            out = subprocess.check_output(
                [ffprobe_bin, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', path],
                text=True, timeout=10,
            )
            dur = float((out or '').strip() or 5.0)
        except Exception:
            pass
        return has_audio, dur

    seg_meta = [_probe_audio_and_duration(p) for p in seg_paths]
    audio_uniform = all(ha for ha, _ in seg_meta)
    # Concat-copy is only safe when all inputs share codec/dims/fps AND every
    # segment has an audio track. Facades typically violate both → skip the
    # fast path entirely once we know audio coverage isn't uniform.
    can_try_copy = audio_uniform and (facades_inserted == 0)

    out_dir = base / 'OUT'
    out_dir.mkdir(exist_ok=True)
    safe_title = (ep.get('title') or f'E{num}').strip()
    safe_title = re.sub(r'[^\w\-]+', '_', safe_title)[:60] or f'E{num}'
    out_name = f"{safe_title}_E{num:03d}.mp4"
    out_path = out_dir / out_name

    list_file = out_dir / f'_concat_{int(time.time())}_{num}.txt'
    list_file.write_text(
        '\n'.join(f"file '{p}'" for p in seg_paths),
        encoding='utf-8',
    )
    cmd_copy = [
        ffmpeg_bin, '-y', '-f', 'concat', '-safe', '0',
        '-i', str(list_file), '-c', 'copy', str(out_path),
    ]
    queue_wait = time.time()
    with RENDER_SEMAPHORE:
        if time.time() - queue_wait > 0.5:
            print(f'[auto-assemble] {sid}/ep{num} waited {time.time()-queue_wait:.1f}s in queue')
        proc = None
        if can_try_copy:
            try:
                proc = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                try: list_file.unlink(missing_ok=True)
                except Exception: pass
                return jsonify({'error': 'ffmpeg timeout (>10 min)'}), 500

        # Fallback to filter-complex re-encode when (a) we skipped concat-copy
        # because audio coverage was non-uniform, or (b) concat-copy failed
        # due to codec drift. Normalize ALL inputs to the same resolution so
        # concat doesn't fail on dimension mismatches (facades are often a
        # different size than Seedance chunks). We probe the first "real" chunk
        # (non-facade, i.e. the last seg_paths entry that comes from a chunk
        # record) to get the canonical W×H, then scale everything to that.
        if (not can_try_copy) or (proc and proc.returncode != 0):
            if can_try_copy:
                print(f'[auto-assemble] {sid}/ep{num} concat-copy failed, retry filter-complex')
            else:
                print(f'[auto-assemble] {sid}/ep{num} skipping concat-copy: '
                      f'facades_inserted={facades_inserted} audio_uniform={audio_uniform}')

            # Probe target resolution from the first non-facade segment.
            target_w, target_h = 576, 1024  # sensible default for 9:16
            if ffprobe_bin:
                chunk_paths = [str(base / c['video_path']) for c in chunks
                               if (base / c['video_path']).exists()]
                for cp in chunk_paths[:3]:   # try first few, stop at first success
                    try:
                        dim_out = subprocess.check_output(
                            [ffprobe_bin, '-v', 'error',
                             '-show_entries', 'stream=width,height',
                             '-of', 'csv=p=0:s=x', cp],
                            text=True, timeout=10,
                        ).strip()
                        if dim_out:
                            tw, th = (int(x) for x in dim_out.split('x'))
                            if tw > 0 and th > 0:
                                # Round to even dimensions (libx264 requirement)
                                target_w = tw if tw % 2 == 0 else tw - 1
                                target_h = th if th % 2 == 0 else th - 1
                                break
                    except Exception:
                        pass
            print(f'[auto-assemble] {sid}/ep{num} target resolution: {target_w}x{target_h}')

            inputs = []
            filt = []
            n = len(seg_paths)
            for i, p in enumerate(seg_paths):
                inputs += ['-i', p]
                # Scale to target resolution with padding to avoid AR distortion.
                # force_original_aspect_ratio=decrease → fit within box,
                # pad → letterbox/pillarbox to fill exact target dims.
                filt.append(
                    f"[{i}:v]setpts=PTS-STARTPTS,"
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps=24[v{i}]"
                )
                has_audio, dur = seg_meta[i]
                if has_audio:
                    filt.append(
                        f"[{i}:a]aresample=async=1:first_pts=0,"
                        f"aformat=channel_layouts=stereo:sample_rates=48000,"
                        f"asetpts=PTS-STARTPTS[a{i}]"
                    )
                else:
                    # Synthesize silent stereo of the clip's exact length so
                    # video/audio timelines stay aligned across the concat.
                    filt.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                        f"atrim=0:{max(0.1, dur):.3f},asetpts=PTS-STARTPTS[a{i}]"
                    )
            cat = ''.join(f"[v{i}][a{i}]" for i in range(n))
            filt.append(f"{cat}concat=n={n}:v=1:a=1[v][a]")
            cmd_re = [
                ffmpeg_bin, '-y', *inputs,
                '-filter_complex', ';'.join(filt),
                '-map', '[v]', '-map', '[a]',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
                '-movflags', '+faststart',
                str(out_path),
            ]
            try:
                proc = subprocess.run(cmd_re, capture_output=True, text=True, timeout=900)
            except subprocess.TimeoutExpired:
                try: list_file.unlink(missing_ok=True)
                except Exception: pass
                return jsonify({'error': 'ffmpeg timeout (filter-complex >15 min)'}), 500
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or '')[-3000:]
                print(f'[auto-assemble] {sid}/ep{num} ffmpeg FAILED:\n{stderr_tail}', flush=True)
                try: list_file.unlink(missing_ok=True)
                except Exception: pass
                return jsonify({
                    'error': 'ffmpeg failed (filter-complex re-encode)',
                    'stderr': stderr_tail,
                }), 500

    try: list_file.unlink(missing_ok=True)
    except Exception: pass

    # Mark episode as assembled + record path.
    with _episode_lock(sid, num):
        ep2 = load_episode(sid, num)
        rel = str(out_path.relative_to(base))
        ep2['assembled_path'] = rel
        ep2['assembled_at'] = int(time.time())
        ep2['gen_status'] = 'done'
        save_episode(sid, num, ep2)

    size_mb = round(out_path.stat().st_size / 1024 / 1024, 2)
    return jsonify({
        'ok': True,
        'path': rel,
        'url': f'/assets/{sid}/{rel}',
        'size_mb': size_mb,
        'chunks': len(seg_paths),
        'facades_inserted': facades_inserted,
        'filename': out_name,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/download-zip')
def seedance_download_zip(sid, num):
    """Stream a ZIP archive of selected chunk video files.
    Query: ?idxs=1,2,3,5  (comma-separated chunk indices)
    Each entry inside the ZIP is named `chunk_NN.mp4` — sorted by idx.
    Skips chunks without a stored video file (in-progress / failed)."""
    import io, zipfile
    from flask import Response
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    raw = (request.args.get('idxs') or '').strip()
    try:
        wanted_idxs = sorted({int(x) for x in raw.split(',') if x.strip()})
    except ValueError:
        return jsonify({'error': 'bad idxs'}), 400
    if not wanted_idxs:
        return jsonify({'error': 'no idxs'}), 400
    chunks = _seedance_chunks(ep)
    base = series_path(sid)
    # Build zip in-memory (chunk videos are small, ~5-10MB each; user typically
    # picks 5-20 chunks). For huge selections we'd stream, but in-memory is
    # simpler and avoids fancy chunked-encoding.
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for c in chunks:
            if c.get('idx') not in wanted_idxs:
                continue
            vp = c.get('video_path')
            if not vp:
                continue
            abs_path = base / vp
            if not abs_path.exists():
                continue
            arcname = f'ep{int(num):03d}_chunk_{int(c.get("idx") or 0):02d}.mp4'
            zf.write(abs_path, arcname=arcname)
            written += 1
    if not written:
        return jsonify({'error': 'no completed videos in selection'}), 404
    buf.seek(0)
    fname = f'{slugify(sid)}_ep{int(num):03d}_{written}clips.zip'
    return Response(
        buf.getvalue(),
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{fname}"',
            'Content-Length': str(buf.getbuffer().nbytes),
        },
    )

@app.route('/api/series/<sid>/episodes/<int:num>/generate-scene-blocking', methods=['POST'])
def generate_scene_blocking(sid, num):
    """Single Claude call → 60-120 word English SCENE BLOCKING text describing
    geometry of the location and where each character is positioned across the
    whole episode. Used as shared context in batch-compose so all segments stay
    spatially consistent (no character teleporting between chunks)."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Сценарий пустой — заполни сначала'}), 400

    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    tag_mapping = _build_episode_tag_mapping(script, active_chars, active_locs)
    tag_lines = '\n'.join(
        f"  {t['tag']} = {t['kind']}: {t['name']}"
        for t in tag_mapping
    ) or '  (no active chars/locs)'

    sysprompt = (
        "Ты — кинематографист. На вход — один эпизод сценария TikTok-драмы. "
        "Твоя задача — описать ГЕОМЕТРИЮ сцены и расстановку персонажей одним коротким "
        "английским абзацем 60-120 слов. Этот текст затем вшивается в Constraints каждого "
        "Seedance-чанка чтобы Seedance видел одну и ту же расстановку во всех сегментах серии "
        "и НЕ ломал spatial continuity.\n\n"
        "ЧТО ВКЛЮЧАТЬ:\n"
        "  • Геометрия локации: где стол / стулья / окна / двери / лестницы. Стороны: "
        "    'long table runs left-right', 'glass wall on the back', 'door on the back-left'.\n"
        "  • Где КАЖДЫЙ активный @ImageN-персонаж сидит/стоит относительно объектов и других: "
        "    '@Image1 Maya stands at the LEFT side of the table, body angled camera-right toward Liam'.\n"
        "  • Когда и откуда заходит/выходит персонаж: "
        "    '@Image2 Liam enters from back-left door at start, walks to the FAR RIGHT head of the table'.\n"
        "  • Реквизит и где лежит: 'leather folder placed at the centre-left of the table'.\n\n"
        "CROSS-EPISODE CONTINUITY — критично:\n"
        "  Если на вход дан блок 'PREV EPISODE CONTEXT' и первая сцена ЭТОГО эпизода "
        "является ПРОДОЛЖЕНИЕМ последней сцены предыдущего (та же локация, нет явного scene "
        "heading с другим местом, нет time-jump в первых 1-2 строках) — НАСЛЕДУЙ позиции "
        "персонажей из prev episodeBlocking и ending state. Adrian остался у двери — он у "
        "двери в начале нового эпизода. Clara сидела за столом — она там же.\n"
        "  Если же сцена ЯВНО другая (новый scene heading с другой локацией, time-jump 'утром' / "
        "'через час', явная смена места) — начинай blocking с нуля, prev контекст игнорируй.\n\n"
        "ФОРМАТ:\n"
        "  • Английский, 60-120 слов, ОДИН абзац.\n"
        "  • Используй ТОЛЬКО @ImageN-теги из переданного TAG MAPPING.\n"
        "  • Никаких 'same/still/as before/continues' (нарушает автономность).\n"
        "  • Без markdown, без bullet-points.\n"
        "  • Описание универсальное для всего эпизода — конкретные действия чанков НЕ упоминай."
    )

    prev_context = _prev_episode_ending_context(sid, num)
    prev_block = ''
    if prev_context:
        prev_block = (
            f"=== PREV EPISODE CONTEXT — для проверки continuity ===\n"
            f"{prev_context}\n"
            f"=== END PREV EPISODE CONTEXT ===\n\n"
        )

    userprompt = (
        f"АКТИВНЫЕ ПЕРСОНАЖИ И ЛОКАЦИИ:\n{tag_lines}\n\n"
        f"{prev_block}"
        f"СЦЕНАРИЙ ЭПИЗОДА:\n```\n{script[:8000]}\n```\n\n"
        f"Верни ТОЛЬКО абзац blocking. Без преамбулы, без 'Here is...', без markdown."
    )
    try:
        text = claude_ask(userprompt, system=sysprompt).strip()
        text = text.replace('\n', ' ').strip()
        text = re.sub(r'\s+', ' ', text)
        text = text[:1500]
        ep['scene_blocking'] = text
        save_episode(sid, num, ep)
        return jsonify({'blocking': text, 'tag_mapping': tag_mapping})
    except Exception as e:
        return jsonify({'error': f'generate failed: {e}'}), 500


# Reference-style batch-compose rules — adapted from
# /tmp/shadow-founder/services/chunk-builder.ts EPISODE_PLAN_RULES.
# Adapted: variable segment count (not fixed 4+1+1), English promptEn output.
_SD_BATCH_RULES = """
You are breaking ONE episode of a TikTok-style short drama into a sequence of segments for Seedance 2.0. Each segment is its own Seedance generation, autonomous, with its own ready-to-render promptEn.

═══ THE GOLDEN RULE — AUTONOMY ═══
Seedance does NOT remember previous prompts. Each segment is described as if it's the only one the model will see.
In every segment's promptEn re-describe from scratch:
- the full location (no "as before")
- which characters are in frame and their @ImageN tag bindings
- props and where they are placed
- character poses and positions
- time of day, weather, lighting

FORBIDDEN words in promptEn: "same", "still", "as before", "as previous", "continues", "прежний", "тот же" — any reference to what the model "saw earlier". Server validates this and warns on violations.

If a location stays the same across segments → COPY the description verbatim, do NOT shorten or refer back.

═══ DIALOGUE ═══
- Take dialogue lines VERBATIM from the script's `Name: text` format. Translate to English LITERALLY (no creative paraphrase) — preserve meaning, emotion, length.
- In Action timeline insert dialogue as: `{Name} says: "..."` or `{Name} whispers: "..."` or `{Name} shouts: "..."`.
- ALL dialogues from script chunk_text must be covered across the segment(s) where they appear. Don't drop lines.
- Story sequence must match the script — order of who-says-what is sacred.

═══ @Image TAG NUMBERING ═══
EPISODE-LEVEL — fixed by the server BEFORE you see the input. The TAG MAPPING input gives you `@Image1`, `@Image2`, ... mapped to specific characters and locations by first-appearance order in the script. Use ONLY these tags. Locations get tags too.

═══ WARDROBE FIDELITY ═══
Each character in TAG MAPPING has a `wardrobe` field — the canonical description of what they're wearing in this episode. This is a CLOSED list. No other clothing exists on them.

Rules for actionTimeline.description:
1. NEVER mention clothing/footwear/headwear/outerwear/accessories that aren't in the character's `wardrobe`. Specifically, before writing words like: jacket, blazer, coat, overcoat, suit jacket, tie, scarf, hat, cap, beanie, gloves, boots, sunglasses, glasses, watch, belt, vest, hoodie, sweater — verify they appear in `wardrobe`. If not — DO NOT write them.
2. Pockets and clothing-interaction: when a character pulls something from a pocket, pick a pocket consistent with what they actually have on. Safe defaults: `trouser pocket`. `inside breast pocket of the jacket` ONLY if jacket is in wardrobe.
3. Pose, dirt, blood, tears, hair state, expression, flushed cheeks, trembling hands — describe freely. The ban is ONLY about wardrobe items / accessories.
4. Before submitting, re-check each `description` and verify that every clothing/accessory word appears in the wardrobe of one of `charactersInSegment`. If not — rephrase (use a safe pocket / drop the mention / describe via posture instead).

═══ SHOT RHYTHM ═══
- A 15-second segment is split into 2-4 shots, each 2-7 seconds. Sum of shot lengths = durationSec.
- Shorter segments (5-12s) split into 1-3 shots proportionally.
- FORBIDDEN: monotone equal-length cuts (e.g. 0-3/3-7/7-11/11-15 or 0-5/5-10/10-15). Vary the rhythm: 2/5/4/4 or 6/4/5 or 3/7/5 — real cinematic pacing.

═══ ESTABLISHING SHOT — when seg_specs has `establishing_shot: true` ═══
This segment is the FIRST chunk of a new location (scene change). Open it with a 2-second establishing shot of the location's exterior/facade BEFORE any dialogue or character action.
- First shot in actionTimeline: `0-2s: wide shot, static frame, exterior facade of <Location>, no people in frame, no dialogue.`
- Second shot onward (2s → durationSec): regular dialogue/action as usual, with characters now inside.
- The 2-second beat counts toward the segment's total durationSec (NOT a separate Seedance call).
- Constraints block must say `no people in establishing shot 0-2s`.
- Refs: keep ALL refs (location + characters); the model needs character refs for shots after the 2s mark.
- For non-establishing segments (`establishing_shot: false` or missing), ignore this section entirely.

═══ CAMERA — embedded in each shot ═══
Format: "[shot size] [movement or static], [angle], [action]"
Example: "0-5s: medium slow push-in, eye-level, Lina opens the leather folder and looks down at the document."
2-3 camera attributes per shot, camera before action.

Camera movement — ONLY when the shot benefits. If stillness serves better → use "static frame" / "locked-off". DON'T add motion to every shot.

Vocabulary: wide shot, medium shot, close-up, extreme close-up, low angle, high angle, eye-level, over-the-shoulder, tracking shot, slow push-in, handheld drift, orbit, pan, tilt, rack focus, whip pan, static frame, forward drift, pull-back.

═══ EYELINE — gaze direction of the speaker ═══
MANDATORY in every shot description with dialogue:
- Speaker's body is turned toward addressee (per blocking).
- Speaker's eyes look at addressee, NOT at camera, NOT at floor.
- Write directly: "Kyle on the right side of frame turns his head to face Hale who stands on the left, eyes locked on Hale. Kyle says: \\"...\\"".
- FORBIDDEN: speaker facing camera while addressee is behind their back — eyeline violation. Even if only the speaker is in frame, describe where they're looking ("sideways toward Hale's offscreen-left position").

═══ CONTINUITY between segments (within the same scene) ═══
- Pose at end of segment N → start of segment N+1 matches or shows the transition.
- Position relative to props is preserved.
- Props remain where left (unless used / removed in frame).
- Physical state preserved (blood, dirt, tears) once established.
- Don't change without reason: pose, costume, appearance, props, interior.
- Change only when the script demands.

═══ CROSS-EPISODE CONTINUITY ═══
TikTok-format episodes often pick up immediately where the previous episode left off — same scene, same characters, same positions. Server may pass you a `PREV EPISODE CONTEXT` block with: (1) prev episode's last scene heading, (2) prev episode's last 6 lines, (3) prev episodeBlocking, (4) prev last chunk's ending_state.

If THIS episode's first segment is a CONTINUATION of the same scene (same location, no new INT./EXT. heading change at script top, no explicit time-jump in opening lines like "утром" / "next day" / "через час") — INHERIT positions from PREV EP. Adrian at the door → he's still at the door. Clara at the desk → still there. Reflect this in `episodeBlocking` of THIS episode.

If the scene CLEARLY changed (new location heading, time-jump phrase, fresh setup) — start positioning from scratch and IGNORE PREV EP context.

═══ SCENE BLOCKING (top-level field — REQUIRED) ═══
You return a top-level `episodeBlocking` field — a single English paragraph 60-120 words describing the episode's spatial setup. Server automatically injects this into the Constraints of every segment that uses overlapping @ImageN tags.

If the user provided SCENE BLOCKING (you'll see it in input) — copy it VERBATIM into `episodeBlocking`. Do NOT paraphrase.
If not provided — generate it yourself: where does the table/chairs/window/door sit; where does each @ImageN character stand or sit relative to props and to each other; when does each character enter/exit; what props are on stage and where.

Example: `"@Image2 Kyle seated at the FAR RIGHT head of the long table, body angled camera-left toward Hale; laptop closed in front of him. @Image1 Hale enters from back-left door at start of segment 1, walks to LEFT side of the table, body angled camera-right toward Kyle. Leather folder at centre-left of the table from segment 3 onward."`

═══ promptEn STRUCTURE — STRICT 6 BLOCKS, ENGLISH ═══
```
Location: <full English description from scratch — no "as before">

Characters in this segment: Name @Image1, Name @Image2

Action timeline:
0-5s: medium slow push-in, eye-level, <action>. <Name> says: "<dialogue verbatim>"
5-12s: close-up, static frame, <reaction>.
12-15s: pull-back, eye-level, <final action>.

Lighting and atmosphere: <light + atmosphere>

Style: <cinematic / documentary / dramatic + 1-2 specifics>

Constraints: use <Name1> as @Image1, use <Name2> as @Image2, keep exact facial identity and outfit from each reference image, <positions/poses>, <props>, no extra characters, no text, no watermark, maintain environment consistency
```

═══ OUTPUT JSON SCHEMA — STRICT ═══
{
  "totalDurationSec": <integer sum of all durationSecs>,
  "scriptDialogueCount": <number of Name: lines in the script>,
  "episodeBlocking": "60-120 word English geometry paragraph (verbatim from input if provided)",
  "segments": [
    {
      "anchor": "<COPY EXACTLY from input — this is the segment ID>",
      "plan": {
        "chunkIndex": <0-based or sceneIdx.segIdx — copy from input>,
        "kind": "main",
        "durationSec": <copy from input>,
        "summaryRu": "1-2 sentence Russian description of what happens",
        "locationDescription": "<full English location description>",
        "charactersInSegment": [
          { "tag": "@Image1", "name": "Lina", "slug": "<id from tag mapping>", "variantId": "base" }
        ],
        "actionTimeline": [
          {
            "fromSec": 0, "toSec": 5,
            "description": "medium slow push-in, eye-level, Lina opens the folder. Lina says: \\"Open the folder.\\"",
            "dialogue": {
              "speakerTag": "@Image1", "speakerName": "Lina",
              "en": "Open the folder.",
              "ruOriginal": "Откройте папку.",
              "scriptLineNumber": 1
            }
          }
        ],
        "lightingAndAtmosphere": "Cold morning light...",
        "style": "cinematic film tone, 35mm grain",
        "constraintsExtra": ["specific positional constraint 1", "..."],
        "dialogues": [
          { "speakerTag": "@Image1", "speakerName": "Lina", "ru": "...", "en": "...", "scriptLineNumber": 1 }
        ],
        "riskFlags": [],
        "close_up": false
      },
      "prompt": {
        "promptEn": "Location: <full description>\\n\\nCharacters in this segment: Lina @Image1\\n\\nAction timeline:\\n0-5s: ...\\n\\nLighting and atmosphere: ...\\n\\nStyle: ...\\n\\nConstraints: use Lina as @Image1, keep exact facial identity and outfit from each reference image, no extra characters, no text, no watermark, maintain environment consistency",
        "tagsUsed": ["@Image1", "@Image2"],
        "appliedReplacements": [],
        "riskLevel": "low",
        "aspectRatio": "9:16"
      }
    }
  ]
}

═══ MANDATORY CHECKLIST — verify before output ═══
1. `segments[]` length matches input segments count exactly.
2. Each `anchor` copied verbatim from input — server matches by anchor.
3. Each `plan.charactersInSegment` uses ONLY tags from TAG MAPPING.
4. Each `prompt.promptEn` follows the 6-block structure with `\\n\\n` between blocks.
5. No "same/still/as before/continues" anywhere in any promptEn.
6. Each `actionTimeline` shot's (toSec - fromSec) sums equal `durationSec`.
7. No clothing/accessory words outside the character's `wardrobe`.
8. `episodeBlocking` populated (verbatim from input if provided, else generated).
9. Constraints block of every promptEn ends with: `no extra characters, no text, no watermark, maintain environment consistency`.

Return ONLY valid JSON. No markdown fences. No commentary.
"""

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/batch-compose', methods=['POST'])
def seedance_batch_compose(sid, num):
    """Reference-style episode plan builder. ONE Claude call produces a strict
    JSON with `episodeBlocking` + per-segment `plan{}` + `prompt.promptEn`.
    Server then runs the same post-processing pipeline as the Shadow Founder
    reference: filtered scene-blocking inject → hard-cuts inject → banlist
    replacements → 4 validators (autonomy / durations / dialogue coverage /
    wardrobe). Final `prompt` field is the ready-for-Seedance English text.

    Body: {
      segments: [{anchor, sceneIdx, segIdx, text, has_close_up, durationSec?}],
      base_outfits_only: bool,
      style: str,
    }
    """
    import hashlib
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    segments = body.get('segments') or []
    if not segments:
        return jsonify({'error': 'no segments'}), 400
    base_outfits_only = bool(body.get('base_outfits_only', False))
    style_override = (body.get('style') or '').strip() or (s.get('visual_style') or '').strip()

    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script empty'}), 400

    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    tag_mapping = _build_episode_tag_mapping(script, active_chars, active_locs)
    valid_tags = {t['tag'] for t in tag_mapping}
    tag_for = {(t['kind'], t['id']): t['tag'] for t in tag_mapping}

    # Build descriptors per tag — fed to Claude with canonical wardrobe
    tag_descriptors = []
    wardrobe_by_tag = {}
    for t in tag_mapping:
        if t['kind'] == 'char':
            ch = next((c for c in active_chars if c['id'] == t['id']), None)
            if not ch: continue
            base_outfit = next((o for o in (ch.get('outfits') or []) if o.get('is_base')), None)
            if not base_outfit and (ch.get('outfits') or []):
                base_outfit = ch['outfits'][0]
            base_label = (base_outfit or {}).get('label')
            canonical = _canonical_char_description(s, ch['id'], base_label)
            outfit_options = [o.get('label') for o in (ch.get('outfits') or []) if o.get('avai_url') and o.get('label')]
            wardrobe_by_tag[t['tag']] = canonical
            tag_descriptors.append({
                'tag': t['tag'],
                'kind': 'character',
                'slug': ch['id'],
                'name': ch['name'],
                'gender': ch.get('gender', ''),
                'appearance': (ch.get('appearance') or '')[:300],
                'wardrobe': canonical,                  # canonical: appearance + outfit
                'outfit_options': outfit_options,
            })
        else:
            lc = next((x for x in active_locs if x['id'] == t['id']), None)
            if not lc: continue
            tag_descriptors.append({
                'tag': t['tag'],
                'kind': 'location',
                'slug': lc['id'],
                'name': lc['name'],
                'description': (lc.get('description') or '')[:300],
            })

    scene_blocking_input = (ep.get('scene_blocking') or '').strip()
    prev_context = _prev_episode_ending_context(sid, num)

    # Segments specs for Claude — anchor + actual computed durationSec
    seg_specs = []
    for seg in segments:
        seg_specs.append({
            'anchor': (seg.get('anchor') or '')[:60],
            'sceneIdx': seg.get('sceneIdx', 0),
            'segIdx': seg.get('segIdx', 0),
            'has_close_up': bool(seg.get('has_close_up')),
            'durationSec': int(seg.get('durationSec') or 15),
            'establishing_shot': bool(seg.get('establishing_shot')),
            'text': (seg.get('text') or '')[:1500],
        })

    # System prompt — reference EPISODE_PLAN_RULES adapted to our case (variable
    # segments instead of fixed 4+1+1; English promptEn output; 6-block strict).
    sysprompt = _SD_BATCH_RULES

    style_block = ''
    if style_override:
        style_block = (
            f"\n=== STYLE OVERRIDE ===\n"
            f"Final visual style: «{style_override}». Inject this style into "
            f"every segment's Style block, plus subtle markers in Subject and "
            f"Scene blocks. Keep dialogue verbatim from the script.\n"
        )
    base_only_block = ''
    if base_outfits_only:
        base_only_block = (
            "\n=== BASE OUTFITS ONLY — STRICT ===\n"
            "For EVERY char-ref across ALL segments set \"outfit\": null. "
            "Do not pick alternative outfit variants — use base portraits only.\n"
        )
    blocking_block = ''
    if scene_blocking_input:
        blocking_block = (
            f"\n=== SCENE BLOCKING — USER-PROVIDED (do not rewrite) ===\n"
            f"```\n{scene_blocking_input}\n```\n"
            f"Copy this VERBATIM as `episodeBlocking` in your response. The "
            f"server will then filter and inject it into each segment's Constraints.\n"
        )

    prev_block = ''
    if prev_context:
        prev_block = (
            f"\n=== PREV EPISODE CONTEXT — for cross-episode continuity ===\n"
            f"```\n{prev_context}\n```\n"
            f"CRITICAL: if the FIRST segment of THIS episode picks up from the same scene "
            f"as PREV EP last lines (same location, no time-jump, no new scene heading) — "
            f"INHERIT the spatial positioning from PREV EP's episodeBlocking and ending state. "
            f"Adrian was at the door at end of PREV EP → he's still at the door at start of THIS one. "
            f"Clara was at the desk → she's still there. Don't reset positions on a continuous scene.\n"
            f"If THIS episode's script clearly opens a new scene (different location heading, "
            f"explicit time-jump like 'утром' / 'next day' / 'через час'), ignore PREV EP context "
            f"and start fresh.\n"
            f"=== END PREV EPISODE CONTEXT ===\n"
        )

    userprompt = (
        f"EPISODE TAG MAPPING (deterministic — do not invent new tags):\n"
        f"{json.dumps(tag_descriptors, ensure_ascii=False, indent=2)}\n\n"
        f"FULL EPISODE SCRIPT:\n```\n{script[:10000]}\n```\n\n"
        f"SEGMENTS (N={len(seg_specs)}, in script-position order):\n"
        f"{json.dumps(seg_specs, ensure_ascii=False, indent=2)}\n\n"
        f"{prev_block}{blocking_block}{style_block}{base_only_block}\n"
        f"=== HARD COUNT REQUIREMENT ===\n"
        f"Return EXACTLY {len(seg_specs)} elements in segments[]. Not {len(seg_specs)-1}, "
        f"not {len(seg_specs)+1}. Each input anchor maps to one output segment with the "
        f"same `anchor` string copied verbatim (it's the ID).\n"
        f"Do NOT merge, skip, or duplicate segments. One input → one output.\n"
        f"Before submitting JSON, count segments[] — it MUST be exactly {len(seg_specs)}.\n\n"
        f"Return ONLY JSON (no markdown fences, no preamble)."
    )

    try:
        # 32k max_tokens — full episode batch JSON can exceed default 8k easily
        raw = claude_ask(userprompt, system=sysprompt, max_tokens=32000)
        cleaned = strip_json(raw)
        # Detect truncation: a valid JSON top-level object/array must end with } or ]
        looks_truncated = bool(cleaned) and cleaned[-1] not in '}]'
        if looks_truncated:
            print(f'[batch-compose] response looks truncated (ends with {cleaned[-50:]!r}); raising max_tokens and retrying', flush=True)
            raw = claude_ask(userprompt, system=sysprompt, max_tokens=64000)
            cleaned = strip_json(raw)
        try:
            data = loads_lenient(cleaned)
        except Exception as parse_err:
            # Last-resort: ask Claude to repair the JSON it just emitted
            print(f'[batch-compose] initial JSON parse failed ({parse_err}); asking Claude to repair', flush=True)
            repair_prompt = (
                "The following text is supposed to be valid JSON but has a syntax error. "
                "Return ONLY the corrected JSON — no commentary, no markdown fences, no explanation. "
                "Preserve all content verbatim; fix ONLY syntax (trailing commas, unquoted keys, "
                "unescaped quotes inside strings, missing closing braces if truncated, etc.).\n\n"
                f"```\n{cleaned}\n```"
            )
            repaired = claude_ask(repair_prompt, system="You are a JSON repair tool. Return only valid JSON.", max_tokens=64000)
            data = loads_lenient(strip_json(repaired))
    except Exception as e:
        return jsonify({'error': f'batch-compose failed: {e}'}), 500

    episode_blocking = (data.get('episodeBlocking') or scene_blocking_input or '').strip()
    out_segments = data.get('segments') or []
    by_anchor = {}
    for so in out_segments:
        a = (so.get('anchor') or '').strip()[:60]
        if a:
            by_anchor[a] = so

    # Pipeline per segment: get plan+promptEn → filter+inject blocking →
    # inject hard-cuts → banlist → resolve refs → store.
    final_prompts = {}
    unresolved_anchors = []
    segments_for_validation = []  # list of (spec, plan, finalized_promptEn)

    for spec in seg_specs:
        anchor = spec['anchor']
        out = by_anchor.get(anchor)
        if not out:
            unresolved_anchors.append(anchor)
            continue
        plan = out.get('plan') or {}
        prompt_obj = out.get('prompt') or {}
        prompt_en = (prompt_obj.get('promptEn') or '').strip()
        if not prompt_en:
            unresolved_anchors.append(anchor)
            continue

        # Defensive defaults
        plan.setdefault('charactersInSegment', [])
        plan.setdefault('actionTimeline', [])
        plan.setdefault('dialogues', [])
        plan.setdefault('constraintsExtra', [])
        plan.setdefault('riskFlags', [])
        plan.setdefault('durationSec', spec['durationSec'])

        # Tags Claude says are used — fall back to chars-in-segment tags
        tags_used = prompt_obj.get('tagsUsed') or []
        if not tags_used:
            tags_used = [c.get('tag') for c in plan.get('charactersInSegment', []) if c.get('tag')]
        tags_used = [t for t in tags_used if t in valid_tags]

        # Inject filtered blocking, then hard-cuts (multi-shot only), then banlist
        with_blocking = _seedance_inject_blocking(prompt_en, episode_blocking, tags_used)
        with_hardcuts = (_seedance_inject_hard_cuts(with_blocking)
                         if len(plan.get('actionTimeline') or []) > 1
                         else with_blocking)
        finalized, applied = _seedance_apply_banlist(with_hardcuts)

        # Build refs from plan.charactersInSegment (Claude already chose them)
        refs = []
        if base_outfits_only:
            for c in plan.get('charactersInSegment', []):
                if c.get('slug'):
                    refs.append({'kind': 'char', 'id': c['slug'], 'outfit': None})
        else:
            for c in plan.get('charactersInSegment', []):
                if c.get('slug'):
                    refs.append({
                        'kind': 'char',
                        'id': c['slug'],
                        'outfit': c.get('variantId') if c.get('variantId') and c.get('variantId') != 'base' else None,
                    })
        # Always add the location ref last if there's an active loc tag in mapping
        loc_tag = next((t for t in tag_mapping if t['kind'] == 'loc'), None)
        if loc_tag:
            refs.append({'kind': 'loc', 'id': loc_tag['id']})

        # CLOSE-UP filter (mirrors compose endpoint)
        is_close_up = bool(plan.get('close_up')) or spec.get('has_close_up') or bool(out.get('close_up'))
        if is_close_up:
            chunk_lower = spec['text'].lower()
            chars_in_chunk = []
            for r in refs:
                if r.get('kind') != 'char': continue
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                if not ch: continue
                pos = chunk_lower.find(ch['name'].lower())
                if pos >= 0: chars_in_chunk.append((r.get('id'), pos))
            chars_in_chunk.sort(key=lambda x: x[1])
            keep_id = chars_in_chunk[0][0] if chars_in_chunk else next(
                (r.get('id') for r in refs if r.get('kind') == 'char'), None
            )
            refs = [r for r in refs if r.get('kind') != 'char' or r.get('id') == keep_id]

        # De-dup char refs
        seen_char = set(); deduped = []
        for r in refs:
            if r.get('kind') == 'char':
                if r.get('id') in seen_char: continue
                seen_char.add(r.get('id'))
            deduped.append(r)
        refs = deduped

        # Resolve URLs (drop unresolvable)
        original_refs_for_remap = list(refs)
        resolved_refs = []
        ref_urls = []
        for r in refs:
            url = _resolve_ref_url(s, r, sid=sid)
            if not url: continue
            clean = {k: v for k, v in r.items() if not k.startswith('_')}
            ref_urls.append(url)
            resolved_refs.append({**clean, 'url': url})

        # If filtering changed refs[] (close-up / dedup / unresolved drops),
        # remap @ImageN tokens in finalized to match the new ref ordering. The
        # plan's @ImageN are positional per Claude's response — server's filter
        # may have shifted them. Keep prompt and refs in sync.
        _tag_remap = _build_image_tag_remap(original_refs_for_remap, resolved_refs)
        if any(v is None for v in _tag_remap.values()) or any(k != v for k, v in _tag_remap.items() if v is not None):
            finalized = _remap_image_tags(finalized, _tag_remap)

        final_prompts[anchor] = {
            'prompt': finalized,                                # ready-for-Seedance English promptEn
            'refs': resolved_refs,
            'ref_urls': ref_urls,
            'plan': plan,                                       # structured (for UI / debug)
            'tagsUsed': tags_used,
            'appliedReplacements': applied,
            'close_up': is_close_up,
            'shot_type': out.get('shot_type', ''),
            'sceneIdx': spec['sceneIdx'],
            'segIdx': spec['segIdx'],
        }
        segments_for_validation.append((spec, plan, finalized))

    # Per-anchor fallback for what Claude skipped → call /seedance/compose-style
    # mini AI for each missing anchor. Always converges to 100% anchor coverage.
    fallback_succeeded = []
    fallback_failed = []
    if unresolved_anchors:
        print(f'[batch-compose] Claude skipped {len(unresolved_anchors)} anchors — running per-chunk fallback', flush=True)
        for missed_anchor in unresolved_anchors:
            spec = next((s for s in seg_specs if s['anchor'] == missed_anchor), None)
            if not spec:
                continue
            try:
                mini_sys = (
                    "You are a Seedance 2.0 shot composer. The user gives ONE script segment. "
                    "Return strict JSON with the same plan{} + prompt.promptEn structure as the "
                    "main batch (English 6-block: Location / Characters in this segment / Action timeline / "
                    "Lighting and atmosphere / Style / Constraints). NO 'same/still/as before' refs.\n\n"
                    "Output: {\"plan\":{...},\"prompt\":{\"promptEn\":\"...\",\"tagsUsed\":[...]}}"
                )
                mini_user = (
                    f"TAG MAPPING:\n{json.dumps(tag_descriptors, ensure_ascii=False)}\n\n"
                    f"SCENE BLOCKING:\n{episode_blocking[:600] if episode_blocking else '(none)'}\n\n"
                    f"SEGMENT TEXT:\n```\n{spec['text']}\n```\n\n"
                    f"durationSec={spec['durationSec']}, has_close_up={spec['has_close_up']}\n"
                    f"Return JSON for this single segment."
                )
                mini_raw = claude_ask(mini_user, system=mini_sys)
                mini_data = json.loads(strip_json(mini_raw))
                m_plan = mini_data.get('plan') or {}
                m_prompt = mini_data.get('prompt') or {}
                m_promptEn = (m_prompt.get('promptEn') or '').strip()
                if not m_promptEn:
                    fallback_failed.append(missed_anchor); continue

                m_plan.setdefault('charactersInSegment', [])
                m_plan.setdefault('actionTimeline', [])
                m_plan.setdefault('dialogues', [])
                m_plan.setdefault('durationSec', spec['durationSec'])
                m_tags = m_prompt.get('tagsUsed') or [c.get('tag') for c in m_plan['charactersInSegment'] if c.get('tag')]
                m_tags = [t for t in m_tags if t in valid_tags]

                m_with_blocking = _seedance_inject_blocking(m_promptEn, episode_blocking, m_tags)
                m_with_hardcuts = (_seedance_inject_hard_cuts(m_with_blocking)
                                   if len(m_plan['actionTimeline']) > 1
                                   else m_with_blocking)
                m_finalized, m_applied = _seedance_apply_banlist(m_with_hardcuts)

                m_refs = []
                if base_outfits_only:
                    for c in m_plan['charactersInSegment']:
                        if c.get('slug'):
                            m_refs.append({'kind': 'char', 'id': c['slug'], 'outfit': None})
                else:
                    for c in m_plan['charactersInSegment']:
                        if c.get('slug'):
                            m_refs.append({'kind': 'char', 'id': c['slug'],
                                           'outfit': c.get('variantId') if c.get('variantId') and c.get('variantId') != 'base' else None})
                if loc_tag:
                    m_refs.append({'kind': 'loc', 'id': loc_tag['id']})

                m_orig_for_remap = list(m_refs)
                m_resolved = []; m_urls = []
                for r in m_refs:
                    url = _resolve_ref_url(s, r, sid=sid)
                    if not url: continue
                    clean = {k: v for k, v in r.items() if not k.startswith('_')}
                    m_urls.append(url); m_resolved.append({**clean, 'url': url})
                m_remap = _build_image_tag_remap(m_orig_for_remap, m_resolved)
                if any(v is None for v in m_remap.values()) or any(k != v for k, v in m_remap.items() if v is not None):
                    m_finalized = _remap_image_tags(m_finalized, m_remap)

                final_prompts[missed_anchor] = {
                    'prompt': m_finalized,
                    'refs': m_resolved,
                    'ref_urls': m_urls,
                    'plan': m_plan,
                    'tagsUsed': m_tags,
                    'appliedReplacements': m_applied,
                    'close_up': bool(m_plan.get('close_up')) or spec['has_close_up'],
                    'shot_type': '',
                    'sceneIdx': spec['sceneIdx'],
                    'segIdx': spec['segIdx'],
                    '_via_fallback': True,
                }
                segments_for_validation.append((spec, m_plan, m_finalized))
                fallback_succeeded.append(missed_anchor)
            except Exception as e:
                print(f'[batch-compose] fallback failed for {missed_anchor[:30]}: {e}', flush=True)
                fallback_failed.append(missed_anchor)

    # Run all 4 reference validators
    warnings = []
    warnings.extend(_seedance_validate_autonomy(segments_for_validation))
    warnings.extend(_seedance_validate_durations(segments_for_validation))
    warnings.extend(_seedance_validate_wardrobe(segments_for_validation, wardrobe_by_tag))
    # Dialogue coverage — check that every Name:dialogue line in script is
    # represented in some segment's plan.dialogues
    script_dialogues = re.findall(r'^[A-Za-zА-Яа-яЁё][^:\n]{0,30}:\s*(.+)$', script, re.MULTILINE)
    expected_dialogues = len(script_dialogues)
    covered = 0
    for spec, plan, _pe in segments_for_validation:
        covered += len(plan.get('dialogues') or [])
    if expected_dialogues > 0 and covered < int(expected_dialogues * 0.8):
        warnings.append({
            'code': 'dialogues-missing',
            'message': f'Покрыто диалогов: {covered}, в скрипте: {expected_dialogues} (<80%). Возможны пропуски реплик.',
            'anchor': '',
        })

    if warnings:
        print(f'[batch-compose] {len(warnings)} validator warnings:', flush=True)
        for w in warnings[:8]:
            print(f'  [{w["code"]}] {w["message"]}', flush=True)

    # Persist (locked to avoid clobbering parallel /start mutations)
    with _episode_lock(sid, num):
        ep_fresh = load_episode(sid, num) or ep
        ep_fresh['batch_prompts'] = final_prompts
        ep_fresh['batch_episode_blocking'] = episode_blocking
        ep_fresh['batch_script_hash'] = hashlib.sha256(script.encode('utf-8')).hexdigest()[:16]
        ep_fresh['batch_built_at'] = datetime.datetime.utcnow().isoformat()
        ep_fresh['batch_warnings'] = warnings
        ep_fresh['batch_tag_mapping'] = tag_mapping
        if episode_blocking and not ep_fresh.get('scene_blocking'):
            ep_fresh['scene_blocking'] = episode_blocking
        save_episode(sid, num, ep_fresh)
        ep = ep_fresh

    return jsonify({
        'ok': True,
        'count': len(final_prompts),
        'requested_count': len(seg_specs),
        'unresolved_anchors': fallback_failed,
        'fallback_filled': fallback_succeeded,
        'claude_skipped': unresolved_anchors,
        'episode_blocking': episode_blocking,
        'tag_mapping': tag_mapping,
        'warnings': warnings,
        'script_hash': ep['batch_script_hash'],
    })


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/compose', methods=['POST'])
def seedance_compose(sid, num):
    """Claude composes prompt + picks refs from a script chunk."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    chunk_text = (body.get('chunk_text') or '').strip()
    if not chunk_text:
        return jsonify({'error': 'chunk_text required'}), 400
    use_prev_lastframe = bool(body.get('use_prev_lastframe', True))
    use_prev_cutframes = bool(body.get('use_prev_cutframes', False))
    base_outfits_only  = bool(body.get('base_outfits_only', False))
    close_up_only      = bool(body.get('close_up_only', False))
    # Optional: caller hands in a FIXED list of refs (kind/id/outfit) — Claude
    # must use ONLY these as the visible roster. Used by "🔄 Перекомпоновать
    # с текущими рефами" — user manually pruned some refs and wants the prompt
    # rewritten without the removed ones.
    locked_refs = body.get('locked_refs') or []
    style_override = (body.get('style') or '').strip()
    # Fall back to project's visual_style (default = realistic — no special handling needed)
    if not style_override:
        proj_style = (s.get('visual_style') or '').strip()
        if proj_style:
            style_override = proj_style

    # ── Adjacent-chunk continuity ─────────────────────────────────────────
    # Find chunks that sit BEFORE / AFTER the current chunk_text in the FULL
    # script (by substring position), not by creation order. This is what
    # actually matters for "who was just in frame" continuity.
    full_script_for_pos = (ep.get('script') or '')
    # Use the shared is_scene_heading() helper — recognises BOTH formal
    # (INT./EXT./ИНТ./...) AND inferred (Локация:, СЦЕНА N, ALL-CAPS slug,
    # [bracketed slug]) headings so continuity survives in scripts without INT./EXT.
    def _pos_in_script(txt):
        """Find the script byte-offset where this chunk_text starts.
        Tries multiple anchors and prefers the FIRST unique match — falls back
        to any match. Skips scene headings and very short lines as anchors
        because those tend to repeat across the script."""
        if not txt or not full_script_for_pos:
            return -1
        cands = []
        for ln in txt.splitlines():
            s = ln.strip()
            if len(s) < 25:
                continue
            if is_scene_heading(s):
                continue
            cands.append(s)
            if len(cands) >= 6:
                break
        # Pass 1: prefer anchors that match exactly once in the script
        for c in cands:
            idx = full_script_for_pos.find(c)
            if idx == -1:
                continue
            if full_script_for_pos.find(c, idx + 1) == -1:
                return idx
        # Pass 2: any anchor that matches at all
        for c in cands:
            idx = full_script_for_pos.find(c)
            if idx != -1:
                return idx
        return -1
    def _range_in_script(txt):
        """Return (start, end) byte-offsets of the chunk in the script.
        End is estimated via the LAST long unique-ish anchor inside the chunk
        (so chunks that share their first line still get distinct ranges)."""
        start = _pos_in_script(txt)
        if start < 0:
            return -1, -1
        # Walk text lines in reverse to find the last anchor we can locate
        # forward from `start`.
        for ln in reversed(txt.splitlines()):
            s = ln.strip()
            if len(s) < 25:
                continue
            if is_scene_heading(s):
                continue
            idx = full_script_for_pos.find(s, start)
            if idx >= 0:
                return start, idx + len(s)
        return start, start + len(txt)

    cur_start, cur_end = _range_in_script(chunk_text)
    cur_pos = cur_start  # keep old name for downstream code
    all_chunks = _seedance_chunks(ep)
    located = []
    for c in all_chunks:
        sp, ep_ = _range_in_script(c.get('chunk_text') or '')
        if sp >= 0:
            located.append((sp, ep_, c))
    prev_neighbour = None
    next_neighbour = None
    prev_neighbour_ep = num         # which episode prev_neighbour belongs to
    prev_neighbour_obj = ep         # the loaded episode dict (for save_episode)
    if cur_start >= 0:
        # PREV = chunk whose END is closest to (but ≤) the current chunk's START.
        # On ties (regenerations of the same fragment): prefer completed,
        # then prefer the most recently created.
        def _prev_score(c, end_pos):
            return (
                end_pos,                                    # 1) max end position
                1 if c.get('status') == 'completed' else 0, # 2) completed wins
                int(c.get('created_at') or 0),              # 3) newer wins
            )
        best_prev = None  # (score_tuple, chunk)
        for sp, ep_pos, c in located:
            if sp == cur_start and ep_pos == cur_end:
                continue   # same chunk
            if ep_pos <= cur_start + 5:           # tolerate tiny overlap of 5 chars
                sc = _prev_score(c, ep_pos)
                if best_prev is None or sc > best_prev[0]:
                    best_prev = (sc, c)
        if best_prev:
            prev_neighbour = best_prev[1]
        # NEXT = smallest start > cur_end (ties: completed > newer)
        def _next_score(c, sp):
            # Negate sp so smaller start = higher score under > comparison
            return (
                -sp,
                1 if c.get('status') == 'completed' else 0,
                int(c.get('created_at') or 0),
            )
        best_next = None
        for sp, ep_pos, c in located:
            if sp >= cur_end - 5 and sp != cur_start:
                sc = _next_score(c, sp)
                if best_next is None or sc > best_next[0]:
                    best_next = (sc, c)
        if best_next:
            next_neighbour = best_next[1]
    elif located:
        # Fallback: chunk_text not found in script (edited?) — use last by creation
        prev_neighbour = all_chunks[-1] if all_chunks else None

    # ── Cross-episode continuity ──────────────────────────────────────────
    # If we're at the start of this episode (no PREVIOUS chunk found in current
    # ep), pull the LAST chunk of episode N-1 by its position in that script.
    # This makes Seedance compose aware of what just happened on screen even
    # across episode boundaries.
    if not prev_neighbour and num > 1:
        prev_ep_obj_x = load_episode(sid, num - 1)
        if prev_ep_obj_x:
            prev_ep_script = (prev_ep_obj_x.get('script') or '')
            def _pos_in_prev(txt):
                if not txt: return -1
                anchor = next((ln.strip() for ln in txt.splitlines() if len(ln.strip()) > 25), txt[:80].strip())
                return prev_ep_script.find(anchor)
            prev_ep_chunks = _seedance_chunks(prev_ep_obj_x)
            located_prev = []
            for c in prev_ep_chunks:
                p = _pos_in_prev(c.get('chunk_text') or '')
                if p >= 0:
                    located_prev.append((p, c))
            if located_prev:
                located_prev.sort(key=lambda x: x[0])
                prev_neighbour = located_prev[-1][1]      # latest by script pos
            elif prev_ep_chunks:
                prev_neighbour = prev_ep_chunks[-1]       # fallback by creation order
            if prev_neighbour:
                prev_neighbour_ep = num - 1
                prev_neighbour_obj = prev_ep_obj_x

    def _summarize_neighbour(c, where, ep_label=None):
        if not c:
            return ''
        bits = []
        ep_tag = f", from EP {ep_label}" if ep_label else ""
        bits.append(f"\n=== {where} CHUNK (idx={c.get('idx')}, status={c.get('status')}{ep_tag}) ===")
        # Who was in frame: derived from the chunk's own refs
        char_names = []
        loc_name = ''
        for r in (c.get('refs') or []):
            if r.get('kind') == 'char':
                ch = next((x for x in (s.get('characters') or []) if x['id'] == r['id']), None)
                if ch:
                    nm = ch['name']
                    if r.get('outfit'): nm += f" ({r['outfit']})"
                    char_names.append(nm)
            elif r.get('kind') == 'loc':
                lc = next((x for x in (s.get('locations') or []) if x['id'] == r['id']), None)
                if lc: loc_name = lc['name']
        if char_names:
            bits.append(f"  IN FRAME: {', '.join(char_names)}")
        if loc_name:
            bits.append(f"  LOCATION: {loc_name}")
        if c.get('prompt'):
            bits.append(f"  PROMPT (1 line): {(c['prompt'][:200]).replace(chr(10), ' ')}")
        if c.get('ending_state'):
            bits.append(f"  ENDING STATE: {c['ending_state']}")
        snippet = (c.get('chunk_text') or '').strip().replace('\n', ' ')[:200]
        if snippet:
            bits.append(f"  SCRIPT SNIPPET: {snippet}")
        return '\n'.join(bits)

    prev_label = (prev_neighbour_ep if prev_neighbour_ep != num else None)
    prev_block = _summarize_neighbour(prev_neighbour, 'PREVIOUS', ep_label=prev_label)
    next_block = _summarize_neighbour(next_neighbour, 'NEXT')
    if prev_block or next_block:
        cross_note = ""
        if prev_neighbour_ep != num:
            cross_note = (
                f"ВНИМАНИЕ: PREVIOUS CHUNK взят из ПРЕДЫДУЩЕГО ЭПИЗОДА (ep {prev_neighbour_ep}), "
                f"потому что текущий CHUNK — самое начало эпизода {num}. "
                "Если в начале нового эпизода нет явного time-jump или смены локации — "
                "это продолжение той же сцены конца прошлого эпизода. Сохрани локацию, "
                "состав в кадре и позы из предыдущего чанка. Если же scene heading в начале "
                "нового эпизода явно говорит о новой локации/времени — начинай свежо.\n"
            )
        prev_block = (
            "\n=== ADJACENT GENERATED CHUNKS — CONTINUITY CONTEXT ===\n"
            f"{cross_note}"
            "Эти чанки соседствуют с твоим CHUNK по позиции в сценарии. "
            "Если они в той же сцене (та же локация, нет скачка во времени) — "
            "ВСЕ персонажи из IN FRAME предыдущего чанка ОБЯЗАНЫ остаться в кадре, "
            "если в сценарии явно не сказано что они вышли. Если кто-то из них был "
            "в фокусе действия (например, его держали, бьют, он на коленях) — он точно в кадре."
            f"{prev_block}{next_block}\n"
            "=== END ADJACENT ===\n"
        )
    else:
        prev_block = ''

    # Compact char/loc rosters. Critical: list each char's available outfit
    # labels so the composer can pick a VALID one (not hallucinate). Without
    # this list the composer either fills "outfit": null (always base) or
    # invents a label that fails to resolve and silently falls back to base —
    # both lead to outfit drift across chunks of the same scene.
    chars_lines = []
    for c in s.get('characters', []) or []:
        # Include any char that has SOME image source — base AVAI url, outfit url,
        # or local ref_images (we'll lazy-upload base on resolve).
        has_outfit = any((o.get('avai_url')) for o in (c.get('outfits') or []))
        if not (c.get('avai_base_url') or has_outfit or (c.get('ref_images') or [])):
            continue
        tag = '' if c.get('avai_base_url') else ' [no_base_url]'
        line = f"- {c['name']} (id={c['id']}){tag}: {c.get('appearance','')[:120]}"
        outfits_with_url = [o for o in (c.get('outfits') or []) if o.get('avai_url')]
        if outfits_with_url:
            outfit_lines = []
            for o in outfits_with_url:
                desc = (o.get('description') or '').replace('\n', ' ')[:100]
                label = o.get('label', '')
                if not label:
                    continue
                marker = ' [base]' if o.get('is_base') else ''
                outfit_lines.append(f'    • "{label}"{marker}: {desc}')
            if outfit_lines:
                line += "\n  Доступные значения для \"outfit\" (выбирай ТОЛЬКО из этого списка):\n"
                line += "\n".join(outfit_lines)
                line += "\n    • null — базовый портрет (если нет специфики сцены ИЛИ персонаж появляется в первый раз)"
        else:
            line += "\n  Outfits: только база (\"outfit\": null)"
        chars_lines.append(line)
    locs_lines = []
    for l in s.get('locations', []) or []:
        # Include all locations that have any image (ref_images OR avai_url) — LLM can still pick them.
        if l.get('avai_url') or l.get('ref_images'):
            tag = '' if l.get('avai_url') else ' [NO_AVAI_URL — нужно перегенерировать]'
            locs_lines.append(f"- {l['name']} (id={l['id']}){tag}: {l.get('description','')[:120]}")
    # Plot-relevant items roster (locket, USB stick, bouquet, etc.).
    # Composer attaches them only when the chunk explicitly shows / mentions
    # the object visually. Cap descriptions short — these only need to anchor
    # the LLM's identification of "is this prop in the chunk?".
    items_lines = []
    for it in s.get('items', []) or []:
        if not it.get('avai_url'):
            continue   # need a public URL for Seedance to use it as ref
        items_lines.append(f"- {it['name']} (id={it['id']}): {it.get('description','')[:100]}")

    sysprompt = (
        "Ты — режиссёр-композитор шотов для коротких драм TikTok, генерируемых через ByteDance Seedance 2.0 "
        "(reference-pro: до 9 картинок-референсов, видео ~5–15 сек, аспект 9:16).\n\n"
        "ЯЗЫК ПРОМПТА — ЖЁСТКОЕ ПРАВИЛО: ВЕСЬ описательный текст промпта (subject/action/scene/camera/style/constraints, "
        "эмоции/тон перед репликами, ярлыки @Image*) пиши ИСКЛЮЧИТЕЛЬНО НА РУССКОМ. Никакого английского в описаниях. "
        "ЕДИНСТВЕННОЕ ИСКЛЮЧЕНИЕ — сами реплики персонажей внутри кавычек: их сохраняй verbatim из сценария "
        "(обычно английский). Технические термины камеры тоже переводи: 'tracking shot' → 'трекинг-шот' или "
        "'движение камеры за героем', 'medium close-up' → 'средний крупный план', 'OTS' → 'через плечо', "
        "'slow dolly in' → 'медленный наезд'. Если поймал себя на английской фразе вне кавычек — перепиши.\n\n"
        "СТРУКТУРА промпта (~60–110 слов всего, первые 20–30 слов решают):\n"
        "  0) BINDING — ПЕРВАЯ строка промпта. Биндим имена к ref-слотам ровно ОДИН раз:\n"
        "     'В refs: @Image1=Ethan, @Image2=Maya, @Image3=Lobby (локация).' "
        "Дальше в промпте используй ИМЕНА (Ethan, Maya, Lobby) — без @ImageN.\n"
        "     Зачем: повторение @ImageN в каждом блоке поощряет Seedance рендерить персонажа дважды "
        "(каждое упоминание = potential render anchor). Биндинг один раз в начале — модель уже знает кто кто.\n"
        "  1) SUBJECT — кто в кадре. После биндинга используй ИМЕНА: 'Ethan и Maya в лобби'.\n"
        "  2) ACTION — что они делают, ИМЕНАМИ: 'Ethan ставит чашку на стол, Maya садится напротив'.\n"
        "  3) SCENE — где, ИМЕНЕМ локации: 'Действие в Lobby — стеклянное холодное лобби, утренний свет'.\n"
        "  4) CAMERA — конкретно: 'medium close-up' (по умолчанию), 'over-the-shoulder', 'medium shot', "
        "'tracking shot' / 'slow dolly in' (на эмоции), 'wide shot' / 'establishing wide' (ТОЛЬКО для границ сцены).\n"
        "  5) DIALOGUE — ИСКЛЮЧЕНИЕ из правила биндинга. Для КАЖДОЙ реплики — формат из ДВУХ частей "
        "ОБЯЗАТЕЛЬНО с @ImageN (для lipsync-привязки):\n"
        "     (a) ШОТ-БИТ перед репликой, имя + @ImageN. По умолчанию используй MEDIUM CLOSE-UP (по грудь портрет), "
        "не tight close-up на одно лицо. Варианты ракурса: 'medium close-up Maya (@Image2) до груди', "
        "'over-the-shoulder Ethan на Maya (@Image2)', 'медиум Maya (@Image2) с лёгкого наклона'.\n"
        "         Wide / two-shot ИЗБЕГАЙ во время реплик — модель плохо удерживает мимику и lipsync на дистанции.\n"
        "     (b) Сама реплика: 'Maya (@Image2), <эмоция/тон на русском>, говорит: \"<точная реплика как в сценарии>\"'\n"
        "     Полный пример (две реплики двух разных персов):\n"
        "       'Medium close-up Maya (@Image2), плечи и лицо в кадре. Maya (@Image2), ухмыляясь с презрением, говорит: \"Lost, Aria?\" "
        "Камера переключается на medium close-up Ethan (@Image1) через плечо Maya. Ethan (@Image1), дрожа от ярости, отвечает: \"Get out.\"'\n"
        "     ПРАВИЛО: в DIALOGUE между сменой говорящего ОБЯЗАТЕЛЬНО шот-бит со склейкой/наездом на нового спикера + указание @ImageN. "
        "Без @ImageN в shot-bit'е и в самой реплике Seedance прицеливает lipsync к не тому персу.\n"
        "     Эмоция/тон по-русски ОБЯЗАТЕЛЬНА перед каждой репликой — без неё лип-синк хуже.\n"
        "     Внутри DIALOGUE @ImageN можно повторить (это нужно для lipsync). В SUBJECT/ACTION/SCENE — НЕТ.\n"
        "     (c) VOICEOVER / ЗАКАДРОВЫЙ ГОЛОС — КРИТИЧНО. Если в исходной реплике сценария есть пометка "
        "         «(voiceover)» / «(V.O.)» / «(VO)» / «(off-screen)» / «(O.S.)» / «(narration)» / «(закадр)» / "
        "         «(голос за кадром)» / «(внутренний голос)» / «(мысленно)» — это НЕ обычная произнесённая реплика. "
        "         Это голос за кадром: звук идёт, но персонаж в кадре НЕ открывает рот. Если ты прицепишь lipsync "
        "         к такому персу — Seedance заставит его шевелить губами под закадровый текст, что выглядит как баг.\n"
        "         Формат VO-реплики ДРУГОЙ:\n"
        "           • НЕ ставь @ImageN рядом с репликой и НЕ пиши «говорит» / «отвечает».\n"
        "           • Пиши: «За кадром (voiceover) — голос Elena: \"<реплика дословно>\". В кадре Elena (@ImageN) "
        "             молчит, рот закрыт, взгляд задумчивый/вдаль/мимо камеры, лёгкое движение глаз/ресниц "
        "             синхронно с эмоцией текста, но БЕЗ движения губ».\n"
        "           • Shot-bit перед VO-репликой описывает СОСТОЯНИЕ персонажа (где стоит/сидит, на что смотрит), "
        "             не lipsync. @ImageN в shot-bit'е оставь — для удержания внешности перса, но в самой реплике убери.\n"
        "         Пример shot-bit + VO: «Medium close-up Elena (@Image1) у окна, она смотрит вдаль, лицо отрешённое, "
        "         губы сомкнуты. За кадром (voiceover) — голос Elena: \"Ten years. That's how long it takes for "
        "         everyone to forget your face.\" В кадре губы Elena остаются СОМКНУТЫМИ, никакого lipsync.»\n"
        "         Если в той же сцене есть И обычные реплики И VO — в обычных пиши «Elena (@Image1) говорит: ...» "
        "         (с lipsync), в VO пиши «За кадром voiceover голос Elena: ... губы сомкнуты».\n"
        "  6) STYLE/ATMOSPHERE — короткие фразы (cinematic, harsh fluorescent light, cold colour palette). "
        "Если в userprompt задан STYLE OVERRIDE — ВСЕ описания (рендер, материалы, освещение, текстуры лиц и одежды, фон) подчиняются этому стилю; "
        "перепиши style-блок и вшей стилистические маркеры также в SUBJECT и SCENE (например 'Pixar 3D animation, soft volumetric light, exaggerated facial expressions, slightly stylised proportions, vibrant saturated palette'). "
        "Реплики в кавычках при этом не меняй.\n"
        "  7) CONSTRAINTS — ТОЛЬКО позитивные формулировки ('smooth gimbal motion', 'stable framing'). "
        "     НЕ пиши 'no shake', 'without distortion' — модель плохо понимает отрицания.\n\n"
        "АНАЛИЗ КОНТЕКСТА — ОБЯЗАТЕЛЬНО ДЕЛАЙ ЭТО ДО ПРОМПТА:\n"
        "Тебе дают: (а) полный сценарий эпизода, (б) выделенный кусок (CHUNK), (в) активный состав персов и локаций эпизода.\n"
        "Прочитай ПОЛНЫЙ сценарий, найди в нём CHUNK и ответь себе на вопросы:\n"
        "  • В какой ЛОКАЦИИ происходит этот кусок? Ищи ПОСЛЕДНИЙ scene heading перед CHUNK. Маркеры могут быть РАЗНЫЕ:\n"
        "    – формальные: 'INT./EXT./INT.\\/EXT. <LOCATION> — <TIME>', 'ИНТ./ЭКСТ./НАТ./ВНУТР./ИНТЕРЬЕР <ЛОКАЦИЯ>'\n"
        "    – явные: 'Локация: <место>. <время>.', 'СЦЕНА N', 'Сцена N', 'SCENE N'\n"
        "    – inferred: одиночная ALL-CAPS строка ('ДОМ АННЫ — НОЧЬ', 'OFFICE — DAY') или в скобках '[КАФЕ — НОЧЬ]'\n"
        "    Если scene heading не нашёлся вообще — локация продолжается с самого начала сценария или предыдущей сцены.\n"
        "  • Какие ПЕРСОНАЖИ физически находятся в кадре? Это НЕ только говорящие. "
        "Если в сцене сказано что Liam стоит рядом и наблюдает — он в кадре, даже если в этом куске молчит. "
        "Если предыдущий чанк закончился тем что Selena вошла в комнату — она всё ещё в кадре в новом чанке той же сцены.\n"
        "  • Где каждый персонаж стоит/находится относительно других? (за столом, у двери, на коленях, и т.п.)\n\n"
        "РЕФЕРЕНСЫ — ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:\n"
        "1. Сначала персонажи, в порядке важности в кадре → @Image1, @Image2, @Image3...\n"
        "   В refs включай ВСЕХ персов в кадре (не только говорящих). Молчащий перс рядом — это часть мизансцены.\n"
        "2. СЮЖЕТНЫЕ ПРЕДМЕТЫ (items) — добавляй в refs ОБЯЗАТЕЛЬНО, если в CHUNK предмет ВИДЕН или ВРУЧАЕТСЯ:\n"
        "   – персонаж держит/протягивает/вручает букет, конверт, локет, флешку, кольцо, документ → ДОБАВЬ в refs\n"
        "   – предмет упомянут в action-ремарке как visible prop ('он сжимает локет', 'кладёт конверт на стол') → ДОБАВЬ\n"
        "   – предмет лишь подразумевается / упоминается репликой без визуального присутствия → НЕ добавляй\n"
        "   В refs items идут ПОСЛЕ персов, ДО локации. Без них Seedance нарисует обобщённый prop с другим цветом/формой.\n"
        "   Используй имена items в SUBJECT/ACTION после BINDING ('Wolf протягивает Daisy Bouquet к Bunny').\n"
        "3. ПОСЛЕДНИМ обязательно идёт ЛОКАЦИЯ → @Image<N+1>. ЭТО НЕ ОПЦИЯ.\n"
        "   Если в AVAILABLE LOCATIONS есть локация, совпадающая с местом действия (по сцен-хедеру или контексту) — "
        "   ОБЯЗАТЕЛЬНО прикрепи её последним @Image. Без локации фон будет рандомным и серия развалится визуально.\n"
        "   Если в roster нет идеально совпадающей локации — выбери максимально близкую по описанию (офис, лобби, спальня и т.п.).\n"
        "   Локацию НЕ ВКЛЮЧАЙ только если в roster вообще нет ни одной подходящей локации с фото.\n"
        "4. В тексте промпта в блоке SCENE явно упомяни локацию ИМЕНЕМ (после BINDING): 'Действие в Lobby — стеклянное лобби корпорации, холодное освещение'.\n"
        "5. Максимум 9 референсов (Seedance hard cap). Обычно 1–3 перса + 0–2 предмета + 1 локация.\n\n"
        "CONTINUITY — КРИТИЧНО:\n"
        "Если в userprompt есть блок ADJACENT GENERATED CHUNKS — это твой главный источник кто физически в кадре.\n"
        "Алгоритм:\n"
        "  1. Определи: соседний чанк (PREVIOUS) — это ТА ЖЕ СЦЕНА что и текущий CHUNK? "
        "Та же сцена = одна и та же локация + нет смены времени суток + нет скачка дня + действие непрерывно.\n"
        "     • Та же сцена → персонажи остаются В СЦЕНЕ (в той же комнате, не вышли). НО это НЕ значит "
        "что они автоматически попадают в твои refs[]. refs[] = кто В КАДРЕ ИМЕННО ЭТОГО чанка, не вся сцена.\n"
        "     • КТО В КАДРЕ — определяется CHUNK TEXT'ом (не предыдущим чанком). Алгоритм:\n"
        "         A. Добавь в refs всех СПИКЕРОВ этого CHUNK'а (тех у кого Name: реплика).\n"
        "         B. Добавь персов явно упомянутых в action-ремарках ВНУТРИ chunk'а с физическим действием "
        "('Liam stares at her', 'Sophie steps closer', 'Adrian frowns at the letter') — у них есть on-screen действие.\n"
        "         C. Если в чанке есть физическое взаимодействие нескольких ('Liam держит Leo за горло', "
        "'Maya обнимает Sophie') — оба/все в refs. Continuity по позам ('Leo на коленях') — обязательна.\n"
        "         D. Просто 'присутствует в той же комнате' (молча, без действия в этом chunk'е) → "
        "НЕ В RREFS. Ему place в фоне Seedance может нарисовать сам как обобщённый силуэт, "
        "но без отдельного @Image-ref'а ты НЕ заставишь Seedance рендерить его лицо.\n"
        "     • CLOSE-UP кейс — самый частый и самый ломаемый:\n"
        "         CHUNK = 1 короткая реплика ОДНОГО спикера ИЛИ длинный монолог ОДНОГО спикера, без action "
        "на других в этом chunk'е → CLOSE-UP/MEDIUM single-subject.\n"
        "         refs = [тот спикер + локация]. Других НЕ ВКЛЮЧАЙ даже если они присутствуют в сцене. "
        "В тексте промпта (CAMERA): 'крупный план / средний план Maya, остальные за пределами кадра'.\n"
        "         Camera-cues 'крупный план X', 'close-up of X', 'tight on X', 'lens on Y' — "
        "STRICT single-subject mode, refs только X/Y + локация.\n"
        "     • GROUP/WIDE кейс: 3+ реплик от разных персов или явная wide-cue ('все собрались', "
        "'wide shot', 'establishing') → refs все участники + локация.\n"
        "     • ФИЗИЧЕСКИЕ ПОЗИЦИИ — ХРАНИТЬ. Если в PREVIOUS Leo стоял рядом с Liam — он РЯДОМ С LIAM, "
        "а не телепортируется к маме. Если Selena была в правом углу — она остаётся в правом углу. "
        "В ACTION прямо пиши конкретные позиции ИМЕНАМИ (без @Image): "
        "'Leo остаётся вплотную к Liam справа от него, Selena на заднем плане у двери'. "
        "Без явного описания позиций модель шафлит героев.\n"
        "     • ФОНОВЫЕ ЭЛЕМЕНТЫ из ENDING STATE / PROMPT предыдущего чанка тоже сохраняй: "
        "если Kaelen только что вышел из лифта — в кадре за его спиной должен быть открытый/закрывающийся лифт. "
        "Если до этого в кадре был стол с ноутбуком — он тут же. В SCENE так и пиши именами: "
        "'на заднем плане Lobby — двери лифта только что закрылись за Kaelen'.\n"
        "     • Если ENDING STATE говорит что персонаж 'на коленях' / 'без сознания' / 'у двери' — "
        "отрази эту позу в SUBJECT/ACTION текущего промпта.\n"
        "     • Локация: если PREVIOUS использовал @Image<X>=Lobby и сцена та же — твой last @Image тоже Lobby.\n"
        "  2. Если сцена другая (новый scene heading МЕЖДУ чанками — формальный INT./EXT./ИНТ./..., либо "
        "'Локация:', 'СЦЕНА N', одиночный ALL-CAPS slug 'ДОМ АННЫ — НОЧЬ', либо явный 'CUT TO:' / 'FADE TO:' "
        "на другую локацию, либо явный перепрыг во времени) — начинай свежо, состояние НЕ тащим.\n"
        "  3. NEXT CHUNK (если есть) используй только для проверки: твой ending не должен противоречить началу следующего.\n\n"
        "POSTURE & STATE CONTINUITY — КРИТИЧНО (частый баг «телепорт стоя→сидя без посадки»):\n"
        "Внутри ОДНОЙ сцены поза/позиция/контакт каждого перса ОБЯЗАНЫ совпадать с ENDING STATE предыдущего чанка. "
        "Seedance НЕ помнит позы между чанками — если ты не пропишешь явно, она сгенерит «нормальную» позу "
        "(обычно стоя фронтально), и герой телепортируется.\n"
        "Конкретно ЛОЧИМ между чанками одной сцены (без перехода — никаких изменений):\n"
        "  • СТОЯ / СИДЯ / НА КОЛЕНЯХ / ЛЁЖА / ПРИСЛОНЁН К СТЕНЕ — если в PREVIOUS ENDING STATE «Maya сидит "
        "    за столом», то в текущем чанке Maya ВСЁ ЕЩЁ сидит за тем же столом. НЕ «Maya стоит у стола».\n"
        "  • ПОЗИЦИЯ В КОМНАТЕ — у двери, у окна, в центре, в углу, за столом, перед камином. Фиксируется.\n"
        "  • ФИЗИЧЕСКИЙ КОНТАКТ — держит за руку, обнимает, удерживает за плечо, держит за горло, "
        "    нависает над, прижимает к стене. Если был в PREVIOUS — продолжается, пока CHUNK явно не разорвёт.\n"
        "  • ЧТО В РУКАХ — чашка, телефон, нож, бокал, документ. Если в PREVIOUS «Marcus держит бокал» — "
        "    в текущем чанке бокал всё ещё у Marcus в руке, пока сценарий не скажет «ставит бокал на стол».\n"
        "  • МИМИКА/ЭМОЦИОНАЛЬНОЕ СОСТОЯНИЕ — заплаканная, в крови, ярость на лице, истерика. Не сбрасывается "
        "    в «нейтральное лицо» только потому что новый чанк.\n"
        "ИЗМЕНЕНИЕ ПОЗЫ ВНУТРИ СЦЕНЫ РАЗРЕШЕНО ТОЛЬКО при ЯВНОЙ scripted action в тексте CHUNK:\n"
        "  • «Maya садится» / «садится за стол» / «опускается на стул» → можно показать процесс или начать "
        "    с уже сидящей в этом чанке.\n"
        "  • «встаёт» / «поднимается» / «отходит к окну» → можно сменить позу.\n"
        "  • «берёт <предмет>» / «кладёт» / «выпускает из рук» → можно сменить что в руках.\n"
        "  • «отступает» / «делает шаг ближе» / «выходит из комнаты» → можно сменить позицию.\n"
        "  Если в CHUNK НЕТ глагола действия — ПОЗА ИЗ PREVIOUS ОБЯЗАТЕЛЬНА. Не «улучшай» сцену добавляя «героиня "
        "  садится» из головы.\n"
        "ОБЯЗАТЕЛЬНЫЙ ТЕКСТ В ACTION для каждого compose внутри сцены:\n"
        "  • Первая фраза ACTION = «продолжаем с PREVIOUS: [имя] [поза] [где] [с чем в руках]». Пример: "
        "    «Продолжая с предыдущего чанка: Marcus стоит у книжного шкафа справа, бокал виски в правой руке; "
        "    Elena сидит за столом по центру, опершись локтями на разложенные документы».\n"
        "  • Затем — то новое что происходит ИМЕННО в этом чанке (реакции, повороты головы, реплики). "
        "    БЕЗ изменения поз/позиций если их не было в сценарии.\n"
        "САМОПРОВЕРКА перед выводом:\n"
        "  – Прочти ENDING STATE / IN FRAME предыдущего чанка.\n"
        "  – Выпиши себе: «X стоит/сидит у Y, держит Z, эмоция W» для каждого перса.\n"
        "  – Прочти текст ТЕКУЩЕГО CHUNK'а — есть ли явный глагол смены позы для каждого перса?\n"
        "  – Если нет глагола → твой ACTION ОБЯЗАН описать ту же позу/позицию/предмет.\n"
        "  – Если в твоём ACTION перс «вдруг сидит» а в PREVIOUS он стоял и в CHUNK нет «садится» — "
        "    это телепорт, перепиши.\n\n"
        "REMOTE CONVERSATION (ТЕЛЕФОН / ВИДЕО-ЗВОНОК / ЧЕРЕЗ СТЕКЛО) — КРИТИЧНО (баг «телепорт из телефона в лицом-к-лицу»):\n"
        "Если сцена — телефонный разговор (или видео-звонок, или разговор через стекло допросной), собеседники "
        "ФИЗИЧЕСКИ НЕ В ОДНОЙ КОМНАТЕ. Каждый — в своём пространстве, с трубкой/телефоном у уха или перед лицом. "
        "Чанки одного звонка ОБЯЗАНЫ оставаться звонком до сценарной фразы окончания. Частый баг: чанк 0 — "
        "звонок по мобильному, чанк 1 — стационарный телефон, чанк 2 — герои лицом к лицу в одной комнате. "
        "Это разваливает сцену.\n"
        "ДЕТЕКЦИЯ — звонок ИЛИ продолжается, ИЛИ нет. Скани FULL SCRIPT (не только CHUNK), ищи маркеры:\n"
        "  • Действия: «picks up the phone», «answers», «звонит», «набирает номер», «берёт трубку», "
        "    «hangs up», «ends the call», «кладёт трубку», «сбрасывает».\n"
        "  • Ремарки у спикера: «(on phone)», «(into phone)», «(V.O.)», «(FILTERED)», «(over phone)», "
        "    «(по телефону)», «(в трубку)».\n"
        "  • Структура: «INTERCUT BETWEEN:», «INTERCUT — Elena's apartment / Marcus's office», "
        "    «split screen», parallel cutting между двумя локациями с alternating диалогом.\n"
        "  • Звонок начался → ВСЕ последующие чанки в той же сцене = звонок, пока не встретилось ЯВНОЕ "
        "    окончание («hangs up», «кладёт трубку», «звонок прерывается», «связь обрывается»).\n"
        "ПРАВИЛА КОМПОЗИЦИИ для чанков-звонков:\n"
        "  1. ТИП ЗВОНКА ЛОЧИТСЯ. Если в первом чанке звонка Elena говорит с мобильного — во всех "
        "     последующих чанках того же звонка она с того же мобильного. НЕ «во втором чанке стационарный, "
        "     потому что компоновщику показалось красивее». Тип определяется первым явным упоминанием "
        "     в скрипте («cell», «мобильный», «smartphone» / «landline», «стационарный», «receiver»).\n"
        "  2. ДВА ПЕРСА В РАЗНЫХ ЛОКАЦИЯХ. Для звонка с показом обоих собеседников выбирай ОДИН из вариантов "
        "     (НЕ оба сразу):\n"
        "     (a) SINGLE-SIDE chunk: в кадре только один собеседник (Elena в своей квартире, телефон у уха), "
        "         голос второго слышен «через трубку». Refs = Elena + локация Elena. БЕЗ Marcus в кадре.\n"
        "     (b) INTERCUT chunk: split-screen или быстрая склейка между двумя локациями. Refs = оба перса + "
        "         ДВЕ локации (обе). В CAMERA пиши «split screen / intercut between [Elena's apartment] "
        "         and [Marcus's office]; left half — Elena, right half — Marcus, оба с телефонами у уха».\n"
        "     НИКОГДА не ставь Elena и Marcus в один кадр в одной комнате во время звонка. Это разрушает "
        "     логику сцены.\n"
        "  3. ТЕЛЕФОН В РУКЕ — ОБЯЗАТЕЛЕН в каждом чанке звонка. Прописывай в ACTION: «Elena у уха правое — "
        "     мобильный телефон в правой руке». Какая рука / у какого уха — лочится с первого чанка.\n"
        "  4. EYE-LINE при звонке: глаза НЕ направлены на собеседника (его нет рядом). Глаза слегка вниз, в "
        "     сторону, на стену, в окно — нейтральный «думающий» взгляд. НЕ прямо в камеру (не разрушаем "
        "     четвёртую стену). В ACTION: «взгляд Elena чуть в сторону, не на камеру, фокус слухового внимания».\n"
        "  5. ФОНОВАЯ ЛОКАЦИЯ каждого собеседника лочится. Если Elena в первом чанке у себя дома на кухне — "
        "     во всех последующих чанках того же звонка она в той же кухне (не «вдруг в спальне»).\n"
        "  6. ОКОНЧАНИЕ ЗВОНКА — нужен сценарный триггер. После «Elena hangs up» / «кладёт трубку» / «связь "
        "     прерывается» следующий чанк МОЖЕТ быть лицом к лицу или новой сценой. БЕЗ триггера — нельзя.\n"
        "САМОПРОВЕРКА перед выводом для каждого чанка:\n"
        "  – PREVIOUS чанк был телефонным разговором (по analysis / по script context)? → ТЕКУЩИЙ тоже звонок, "
        "    пока я не нашёл явный «hangs up» между ними.\n"
        "  – В ACTION есть «телефон у уха» / «mobile in hand»? Должен быть, если звонок.\n"
        "  – Я случайно не поставил обоих собеседников в одну комнату? Это запрещено.\n"
        "  – Тип телефона тот же что в PREVIOUS? Не «мобильный → стационарный».\n\n"
        "OUTFIT CONSISTENCY — КРИТИЧНО ДЛЯ КОНСИСТЕНТНОСТИ ОДЕЖДЫ:\n"
        "Это самая частая причина дрейфа костюмов между чанками. Жёсткие правила:\n"
        "  1. Поле \"outfit\" в каждом char-ref'е выбирай ТОЛЬКО из списка значений, явно перечисленного "
        "под персонажем в AVAILABLE CHARACTERS / ACTIVE THIS EPISODE. Если списка нет — ставь null. "
        "НЕ ПРИДУМЫВАЙ label'ы (типа 'casual', 'formal', 'dress' и т.п.) — они тихо упадут к base.\n"
        "  2. SAME-SCENE LOCK: Если в блоке ADJACENT GENERATED CHUNKS есть PREVIOUS CHUNK И он в той же сцене что текущий CHUNK "
        "(см. CONTINUITY алгоритм выше), то outfit КАЖДОГО перса ОБЯЗАН СОВПАДАТЬ с тем что использовал PREVIOUS CHUNK. "
        "Имя outfit'а видно в IN FRAME предыдущего чанка как 'Maya (work_scrubs)'. Берёшь дословно тот же label. "
        "Менять outfit в той же сцене категорически нельзя — даже если по описанию сцены кажется что другой подходит лучше.\n"
        "  3. SCENE-CHANGE ROUTE: Если сцена меняется (новый scene heading, time-jump) — выбери outfit по контексту "
        "новой сцены. Например в roster Maya: work_scrubs (для смен в больнице), street_clothes (для улицы), "
        "evening_dress (для свидания). Сценарий обычно сам подсказывает контекст. Если нет явного — null (база).\n"
        "  4. WARDROBE-CHANGE EXCEPTION: outfit меняется ВНУТРИ той же сцены ТОЛЬКО если сценарий явно описывает "
        "переодевание ('Maya меняет блузку', 'переодевается в платье', 'снимает пальто'). Без явной ремарки — не менять.\n"
        "  5. DRESS-CODE ОТМЕНЯЕТ SAME-SCENE LOCK — КРИТИЧНО (частый баг «работница пришла на похороны в форме»):\n"
        "Когда CHUNK или его FULL-SCRIPT окружение содержит маркер ЦЕРЕМОНИАЛЬНОГО / СПЕЦИАЛЬНОГО события — "
        "outfit ОБЯЗАН соответствовать дресс-коду этого события, даже если предыдущий чанк той же сцены был "
        "в рабочей форме. Continuity костюма ПРОИГРЫВАЕТ адекватности контекста — лучше визуальная нестыковка "
        "«было work_uniform, стало black_dress на похоронах», чем «работница в фартуке плачет у гроба».\n"
        "ТРИГГЕРЫ (любого упоминания в текущем CHUNK или scene heading достаточно):\n"
        "  • Похороны / поминки / кладбище / гроб / отпевание / funeral / wake / cemetery / casket → "
        "тёмная/чёрная формальная одежда. Ищи outfit с label-словами: 'black', 'funeral', 'mourning', "
        "'formal_black', 'dark_suit'. Если нет — null (база) НЕ годится если база = work uniform; в этом случае "
        "выбери самый близкий формальный outfit ('formal_dress', 'business_suit', 'evening_dress' тёмного тона). "
        "В ACTION/SUBJECT прямо опиши состояние: 'в тёмной формальной одежде', 'на лице траурное выражение'.\n"
        "  • Свадьба / венчание / wedding / ceremony / алтарь / banquet hall → нарядная одежда. Невеста — "
        "wedding_dress / white_dress если есть. Гости — formal/cocktail. Look for: 'wedding', 'gown', 'tuxedo', "
        "'suit', 'cocktail', 'formal'.\n"
        "  • Суд / зал заседаний / courtroom / depositions / hearing → деловой костюм. 'court_suit', "
        "'business_suit', 'professional'. Не в фартуке/spa-форме/casual.\n"
        "  • Гала / приём / red carpet / charity event / opera → вечерний наряд. 'evening_dress', 'tuxedo', "
        "'gala', 'ball_gown'.\n"
        "  • Госпиталь как ПАЦИЕНТ (не персонал) → больничный халат / 'hospital_gown' / 'patient'. БЕЗ "
        "повседневной одежды поверх.\n"
        "  • Тюрьма / задержание / police station as detainee → тюремная роба / 'prison_jumpsuit' / 'inmate'. "
        "Если перс ушёл в тюрьму одним outfit'ом, а вышел — это новый outfit (тюремная или новая гражданская).\n"
        "  • Пляж / бассейн / swim → swimwear / 'swimsuit' / 'beach'. Не в зимнем пальто.\n"
        "  • Спортзал / тренировка / workout → 'gym', 'sportswear', 'athletic'.\n"
        "  • Сон / спальня / постель в начале сцены → 'pajamas', 'nightgown', 'robe', 'sleepwear'.\n"
        "  • Душ / ванна → халат / полотенце ('bathrobe', 'towel') ИЛИ оставь null если outfit'а нет.\n"
        "АЛГОРИТМ:\n"
        "  1. Прочти текущий CHUNK + scene heading + 2-3 строки FULL_SCRIPT вокруг — есть ли event trigger?\n"
        "  2. Если да — пройдись по списку доступных outfit'ов перса. Ищи label или description со словами "
        "из триггер-семантики.\n"
        "  3. Если нашёл подходящий — используй его (НЕЗАВИСИМО от того что было в PREVIOUS CHUNK).\n"
        "  4. Если НЕТ подходящего — пиши null И в поле `reasoning` JSON'а отметь: «event=funeral, но нет "
        "формального outfit'а в roster — рендерим в base, нужно сгенерить outfit». Это поможет юзеру увидеть "
        "и добавить нужный костюм.\n"
        "  5. SAME-SCENE LOCK всё ещё работает ВНУТРИ одной event-сцены — если перс уже в чёрном на похоронах "
        "в чанке N, в чанке N+1 (тех же похоронах) он всё ещё в чёрном.\n\n"
        "FRAMING / РАКУРС — КРИТИЧНО (частая проблема «герои телепортируются на широких планах»):\n"
        "Дефолт: ВСЯ серия должна выглядеть как series of medium close-ups (по грудь портрет), потому что:\n"
        "  • Wide-планы между чанками показывают конкретные позы тел, и Seedance не помнит точное положение "
        "    рук/ног/корпуса из предыдущего чанка → герой «перепрыгивает» с места на место.\n"
        "  • Medium close-up (chest-up) обрезает половину тела, lipsync чище, мимика читается, "
        "    перепрыгивание неощутимо (модель додумывает совместимый кусок тела ниже кадра).\n"
        "\nПРАВИЛА:\n"
        "1. По умолчанию ВСЕ shot-биты в DIALOGUE — это medium close-up (по грудь, не до пояса, не tight на одно лицо). "
        "Глаза + всё лицо + плечи + верх груди в кадре. Без рук, без обстановки на заднем плане в фокусе.\n"
        "2. OTS (over-the-shoulder) — отличная альтернатива, не пиши их меньше чем medium-close-up. Чередуй "
        "OTS и medium close-up по сменам говорящих чтобы было разнообразие, но оба остаются «близкими».\n"
        "3. Tight close-up на одно лицо (только лицо в кадре, нос-подбородок-щёки) ИСПОЛЬЗУЙ ТОЛЬКО когда "
        "сюжет требует эмоционального акцента: герой плачет крупным планом, узнаёт страшное, шок, ужас. "
        "Обычная реплика — НЕ tight close-up, иначе всё видео душное.\n"
        "4. Wide shot / establishing wide / two-shot во весь рост ИСКЛЮЧИТЕЛЬНО в двух случаях:\n"
        "   (a) ПЕРВЫЙ chunk ПОСЛЕ scene heading (новая сцена) — establishing wide на 2 секунды, "
        "       показать пространство и расстановку, дальше медленный наезд на medium close-up первого спикера.\n"
        "       Признак: ПЕРЕД CHUNK в FULL SCRIPT стоит scene heading (INT./EXT./ИНТ./Локация:/СЦЕНА N/ALL-CAPS slug), "
        "       а между ним и CHUNK максимум 1-2 строки ремарки.\n"
        "   (b) ПОСЛЕДНИЙ chunk сцены — wide на эмоциональный outro если в конце сцены героев накрывает "
        "       что-то значительное (расставание, удар, откровение). Признак: ПОСЛЕ CHUNK в FULL SCRIPT "
        "       идёт следующая scene heading или конец сценария.\n"
        "   Wide-ы в середине сцены — НЕТ. Если по сюжету нужен жест/движение которое не показать close-up "
        "   (герой идёт через комнату, бьёт кулаком стол, обнимает другого) — сделай medium SHOT (по пояс), "
        "   не wide. Medium shot покажет действие и не телепортирует.\n"
        "5. Если действие физически требует wide (драка, погоня, падение, групповая сцена 4+ человек) — "
        "   делай wide, но в SCENE/ACTION пропиши КОНКРЕТНЫЕ позиции каждого ИМЕНАМИ ('Maya справа от стола, "
        "   Ethan слева, Liam на заднем плане у двери') чтобы Seedance не теряла геометрию.\n"
        "6. Establishing-wide в начале сцены — ОТДЕЛЬНЫЙ tag в CAMERA: 'establishing wide shot of Lobby — 2с, "
        "   потом slow dolly in на medium close-up Maya для первой реплики'. Это сообщает модели «сначала "
        "   мир, потом крупно». Не пиши «wide shot of everyone» — потеряешь lipsync.\n"
        "ПРОВЕРКА перед выводом: если в твоём prompt-е больше одного wide/two-shot ракурса вне scene-edges — "
        "пересмотри и замени средние/крупные на medium close-up. Это улучшит continuity видеогенерации.\n\n"
        "ВАРИАТИВНОСТЬ РАКУРСОВ МЕЖДУ SHOT-BIT'АМИ ВНУТРИ ОДНОГО ЧАНКА — КРИТИЧНО (баг «склейки есть, "
        "но ракурсы выглядят одинаково, героиня будто телепортируется лицом»):\n"
        "Seedance ДЕЛАЕТ склейки которые ты прописал. Проблема в том, что без явных директив каждый shot-bit "
        "получается медиум-крупным планом примерно с того же положения камеры и того же расстояния — две "
        "соседние склейки выглядят как «слегка покачнули камеру, лицо то же». Зритель видит лишь сменившийся "
        "lipsync, а не реверс-шот. Чтобы каждая склейка реально читалась — соседние shot-bit'ы должны "
        "ОТЛИЧАТЬСЯ как минимум по ДВУМ из этих параметров:\n"
        "  • СТОРОНА КАМЕРЫ (180°-line): OTS со стороны A → reverse OTS со стороны B (зеркально). "
        "    «Камера через ЛЕВОЕ плечо Clara» → следующая склейка «через ПРАВОЕ плечо Adrian-а», "
        "    НЕ «через левое плечо Adrian-а».\n"
        "  • РАЗМЕР КАДРА: medium close-up (по грудь) → close-up (только лицо), либо medium close-up → "
        "    OTS, либо close-up → medium shot (по пояс с жестом). НЕ две medium close-up подряд с того же "
        "    угла. Менять «насколько близко» — самый сильный визуальный сигнал смены кадра.\n"
        "  • ВЫСОТА/УГОЛ камеры: eye-level → лёгкий low-angle (снизу-вверх, добавляет давления), либо "
        "    eye-level → лёгкий high-angle (сверху-вниз, добавляет уязвимости). Не два eye-level подряд.\n"
        "  • НАПРАВЛЕНИЕ ВЗГЛЯДА в кадре: если в shot 1 говорящий смотрит ВПРАВО (на собеседника справа), "
        "    то в shot 2 на этом собеседнике он смотрит ВЛЕВО (на исходного говорящего). Зеркальная "
        "    геометрия экрана — обязательна.\n"
        "В тексте промпта пиши ракурсы КОНКРЕТНО ПО ПАРАМ. Не «cut на Adrian», а:\n"
        "  «OTS через ЛЕВОЕ плечо Clara, средний крупный план Adrian-а во весь кадр справа, eye-level, "
        "  он смотрит влево вниз на Clara» → следующая склейка → «reverse — OTS через ПРАВОЕ плечо Adrian-а, "
        "  close-up только лицо Clara слева, лёгкий low-angle (мы смотрим из-под подбородка Adrian-а на её "
        "  лицо), она смотрит вправо вверх на Adrian-а».\n"
        "ЗАПРЕЩЁННЫЙ ПАТТЕРН (как было в ep27): «medium close-up Clara → medium close-up Adrian → medium "
        "close-up Clara → medium close-up Adrian» — все с одного и того же угла, одного размера, "
        "eye-level. Это и есть «телепорт лицом» в глазах зрителя. ОБЯЗАТЕЛЬНО варьируй размер/угол/сторону.\n"
        "САМОПРОВЕРКА перед выводом: для каждой пары соседних shot-bit'ов в чанке посмотри — отличаются ли "
        "они хотя бы двумя из (сторона, размер, высота, направление взгляда)? Если оба читаются как "
        "«medium close-up чел смотрит вперёд» — переделай, добавь зеркальную сторону и смену размера кадра.\n\n"
        "EYE-LINE / ВЗГЛЯД ПЕРСОНАЖЕЙ — КРИТИЧНО (частый баг 'герои смотрят в разные стороны'):\n"
        "Дефолтное правило: ВО ВРЕМЯ ДИАЛОГА персонажи смотрят на собеседника. Если в кадре двое и они "
        "разговаривают — их взгляды направлены друг на друга. Если троих — говорящий смотрит на адресата реплики, "
        "слушатели смотрят на говорящего. БЕЗ явной директивы Seedance часто поворачивает героев лицом в камеру "
        "или в случайные стороны — получается визуально странно ('сидят рядом, говорят, но смотрят мимо').\n"
        "Правила:\n"
        "  1. В ACTION для КАЖДОГО диалогового шот-бита явно указывай куда направлен взгляд:\n"
        "     – 'Maya смотрит Ethan-у в глаза, говоря «...»'\n"
        "     – 'Ethan не отрывает взгляд от Maya, отвечая «...»'\n"
        "     – 'Maya отводит взгляд к двери на секунду, потом снова смотрит на Ethan-а'\n"
        "  2. В CAMERA для two-shot / OTS дополняй направлением eye-line:\n"
        "     – 'over-the-shoulder с правой стороны Maya, в кадре её затылок и лицо Ethan-а, его глаза направлены на Maya'\n"
        "     – 'medium two-shot, Maya слева смотрит вправо на Ethan-а, Ethan справа смотрит влево на Maya'\n"
        "  3. ИСКЛЮЧЕНИЯ — ОБЯЗАТЕЛЬНО следуй сценарию если он явно указывает другое направление взгляда:\n"
        "     – 'Maya смотрит в окно / на улицу / вдаль' → взгляд НЕ на собеседника, а в указанном направлении\n"
        "     – 'Maya отворачивается / прячет лицо / смотрит в пол / закрывает глаза' → отрази в ACTION\n"
        "     – 'Maya говорит сама с собой / в зеркало / на телефон / в камеру для записи' → не на собеседника\n"
        "     – Монолог одного перса без собеседника рядом → взгляд по контексту (вдаль, на предмет, в потолок)\n"
        "     – Эмоциональное избегание ('не может смотреть ему в глаза', 'отводит взгляд') явно указано\n"
        "     – Камера/wide shot — герой стоит спиной к камере / в три четверти / профиль (но даже тогда eye-line "
        "       к собеседнику если они разговаривают)\n"
        "  4. ШОТ-БИТЫ В DIALOGUE: при переходе на нового говорящего, помимо склейки/наезда укажи направление взгляда:\n"
        "     – 'Склейка на крупный план Maya — она смотрит прямо на Ethan-а, не моргая. Maya (@Image2), холодно: \"...\"'\n"
        "     – 'Камера переходит на Ethan через плечо Maya — Ethan встречает её взгляд. Ethan (@Image1), тихо: \"...\"'\n"
        "  5. Если в SUBJECT перечислены 3+ персонажей в кадре, в ACTION укажи кто на кого смотрит:\n"
        "     – 'Liam стоит между ними — переводит взгляд с Maya на Ethan-а, ловя их перепалку'\n"
        "  6. Когда сцена 'разговор по телефону' или 'через стекло' — собеседник физически не в кадре, "
        "     но взгляд героя направлен на телефон/трубку/стекло, не блуждает.\n"
        "Игнорировать это правило = персы будут смотреть в случайные стороны и видео визуально развалится.\n\n"
        "BODY ORIENTATION + SHOT/REVERSE-SHOT — КРИТИЧНО (частый баг 'слушатель отвернулся, говорящий смотрит в камеру'):\n"
        "Этот баг возникает потому, что без явных директив Seedance ставит обоих героев фронтально к камере "
        "(как для фото), а не друг к другу — получается две головы лицом в объектив, а между ними пустое "
        "пространство. Эмоционально это читается как «они в разных вселенных, не разговаривают».\n"
        "Правила КОТОРЫЕ НАДО ВСТАВЛЯТЬ В КАЖДЫЙ ДИАЛОГОВЫЙ shot-бит ИЛИ В CAMERA:\n"
        "  1. КОРПУС/ПОЗА: слушатель повёрнут КОРПУСОМ к говорящему (не задом, не на 90° в сторону). "
        "     Когда в кадре двое говорящих и видим обоих — оба повёрнуты ТОРСАМИ ДРУГ К ДРУГУ под ~30-60° к камере, "
        "     не фронтально 0°. Прямо так и пиши: 'Maya обращена корпусом к Ethan-у, не к камере'.\n"
        "  2. ЗАПРЕТ ВЗГЛЯДА В ЛИНЗУ: ни говорящий, ни слушатель НИКОГДА не смотрят прямо в объектив во время "
        "     диалога (это разрушает четвёртую стену). Lens-direct gaze разрешён ТОЛЬКО когда сценарий явно "
        "     просит 'смотрит в камеру', 'POV-ракурс собеседника' или 'speech to audience'. По дефолту "
        "     добавляй в ACTION: 'взгляд НЕ в камеру, направлен на [имя собеседника]'.\n"
        "  3. ОБЯЗАТЕЛЬНАЯ АЛЬТЕРНАЦИЯ ракурсов между shot-битами разных говорящих (shot/reverse-shot, "
        "     правило 180°): если предыдущий shot-бит был OTS со стороны Maya (через её плечо на Ethan-а), "
        "     следующий shot-бит при смене говорящего ОБЯЗАН быть ЗЕРКАЛЬНЫМ — OTS со стороны Ethan-а "
        "     (через его плечо на Maya). НЕЛЬЗЯ два подряд OTS-а с одной и той же стороны — это и есть "
        "     'оба кадра выглядят одинаково'. То же для medium close-up: если первый был frontal на Maya, "
        "     второй на Ethan-а должен быть с противоположного направления камеры (Ethan слева смотрит "
        "     вправо vs Maya справа смотрит влево — eye-lines встречаются по экранной геометрии).\n"
        "  4. ПРЯМОЙ ЯЗЫК ДЛЯ CAMERA при смене говорящего:\n"
        "     – 'reverse shot — теперь OTS со стороны Ethan-а, в кадре его затылок справа и лицо Maya слева, "
        "        Maya смотрит вправо вверх на Ethan-а, корпус развернут к нему'\n"
        "     – 'cut на medium close-up Ethan-а, eye-line влево (туда, где Maya была в предыдущем кадре), "
        "        чтобы экранная геометрия сошлась'\n"
        "  5. ФОНОВЫЙ ПЕРСОНАЖ (когда в two-shot один на foreground, второй на background): фоновый "
        "     ОБЯЗАТЕЛЬНО смотрит на foreground-героя (затылок переднего + лицо заднего, направленное "
        "     к переднему). НЕ 'передний у стола, задний смотрит в камеру' — это и есть бракованный ракурс. "
        "     Пиши явно: '@Image2 на заднем плане, смотрит на @Image1 (передний план), не на камеру'.\n"
        "  6. САМОПРОВЕРКА перед выводом каждого compose: для соседних shot-бит разных говорящих — посмотри "
        "     по описанию: чувствуется ли разница ракурсов? Если оба читаются как 'X смотрит вперёд, Y тоже "
        "     смотрит вперёд' — пересмотри, добавь направления взглядов и зеркальные OTS.\n\n"
        "АНТИ-ДУБЛИРОВАНИЕ ПЕРСОНАЖЕЙ — КРИТИЧНО (частый баг 'две Maya в одном кадре'):\n"
        "Seedance с reference-pro может рендерить персонажа дважды если получит несколько визуальных "
        "источников одного и того же перса. ЖЁСТКИЕ правила:\n"
        "  1. Каждый персонаж = РОВНО ОДИН char-ref в массиве refs[]. Не пихай Maya дважды с разными outfit'ами. "
        "Если по сценарию ей надо переодеться — это либо новая сцена (тогда новый compose с новым outfit'ом), "
        "либо явный wardrobe-change внутри сцены (тогда выбираешь финальный outfit для всего чанка).\n"
        "  2. Если continuity-кадр (lastframe / cutframe) уже содержит персонажа Maya — это композиционный "
        "референс расстановки, НЕ повод добавить второй ref для Maya. У Maya остаётся ОДИН char-ref (@Image1) "
        "плюс continuity-кадр как @Image_LF — этого Seedance'у достаточно. Если ты добавишь Maya base portrait "
        "и Maya outfit и lastframe c Maya — она появится в кадре дважды или трижды.\n"
        "  3. В SUBJECT/ACTION/SCENE НЕ повторяй @ImageN многократно. После BINDING-строки используй имя. "
        "Не пиши: 'Maya у двери. @Image1 говорит. @Image1 поворачивается.' — Seedance может сплитнуть "
        "это в 3 разные Maya. Пиши слитно с именем: 'Maya, поворачиваясь у двери, говорит ...'. "
        "@ImageN допустим ТОЛЬКО в DIALOGUE shot-bit'ах (для lipsync-привязки) и в BINDING.\n"
        "  4. Если в кадре ОДИН перс (моноспикер монологом) — refs может содержать только: 1 char-ref + "
        "1 loc-ref (= 2 ref'а минимум) или плюс continuity-кадр. Не раздувай 5 рефами одного перса.\n"
        "  5. Легитимный кейс двух Maya — ТОЛЬКО если сценарий явно говорит про зеркало/двойника/раздвоение "
        "('Maya видит себя в отражении', 'два разных временных Maya'). В этом случае пиши явно: "
        "'@Image1 — настоящая Maya у окна, отражение в зеркале справа дублирует её' — и это всё равно "
        "ОДИН char-ref.\n"
        "  6. ОБЯЗАТЕЛЬНАЯ ДЕКЛАРАЦИЯ HEADCOUNT в первой строке SUBJECT каждого compose:\n"
        "     'PEOPLE IN FRAME: ровно N человек — [имя1] (@ImageX), [имя2] (@ImageY), [unnamed extra: краткое описание]'.\n"
        "     Это форсит модель посчитать людей и не плодить копии. Если в кадре только Maya — пиши "
        "     'PEOPLE IN FRAME: ровно 1 человек — Maya (@Image1)'. Если Maya+Ethan — 'ровно 2 человека — "
        "     Maya (@Image1), Ethan (@Image2)'. БЕЗ ЭТОЙ СТРОКИ модель часто дорисовывает лишних людей.\n"
        "  7. UNNAMED EXTRAS / БЕЗЫМЯННЫЕ ПЕРСОНАЖИ (адвокат, охранник, прохожий, официант) — "
        "     САМЫЙ ЧАСТЫЙ источник бага 'два одинаковых лица в кадре'. У них НЕТ char-ref'а, поэтому "
        "     Seedance копирует лицо ближайшего ref-перса (главгероя). Правила:\n"
        "     (a) Если по сценарию extra нужен В КАДРЕ — в SUBJECT обязательно дай ему ОТЛИЧИТЕЛЬНОЕ "
        "         описание которое ВЕРБАЛЬНО ОТТАЛКИВАЕТСЯ от всех ref-персов: возраст-противоположность, "
        "         другая раса/телосложение/причёска/борода/очки. Пример: главгерой — 'седой мужчина 55+ "
        "         в синем костюме, очки' → extra-адвокат должен быть 'молодой мужчина 30 лет, бритый, "
        "         без очков, в чёрной мантии' (а НЕ 'мужчина в костюме'). Иначе Seedance клонирует главгероя.\n"
        "     (b) В ACTION пиши явно: 'extra-адвокат — НЕ похож на @Image1, другое лицо, другой возраст'. "
        "         Это negative-prompt пункт.\n"
        "     (c) Если extra можно убрать из кадра без потери смысла — УБЕРИ. 'Marcus стоит у стола, "
        "         адвокат за кадром слышен голос' лучше чем рисовать двух мужчин рядом.\n"
        "     (d) Если в сценарии ДВА именованных персонажа с похожей внешностью (двое 50-летних мужчин "
        "         в костюмах) — в BINDING/ACTION усили различия: 'Marcus — седые волосы, очки в роговой "
        "         оправе. Hartwell — лысый, без очков, седая борода'. БЕЗ этого Seedance их сольёт в "
        "         близнецов.\n"
        "  8. ЗАПРЕЩЁННЫЕ СИММЕТРИЧНЫЕ КОМПОЗИЦИИ когда в refs только ОДИН char-ref:\n"
        "     – 'окружена двумя мужчинами' / 'flanked by two men' / 'между двух фигур' — НЕТ. Модель "
        "       автоматически продублирует единственный мужской ref на обе позиции.\n"
        "     – 'на фоне толпы похожих людей' — НЕТ. Толпа клонирует ref-лицо.\n"
        "     – Зеркальное расположение двух людей по бокам от третьего — НЕТ если в refs не два разных перса.\n"
        "     Используй ассиметрию: один человек на foreground + локация на background, без фигур-двойников.\n"
        "  9. САМОПРОВЕРКА HEADCOUNT перед выводом:\n"
        "     – Посчитай людей, которые ПОДРАЗУМЕВАЮТСЯ в prompt-е (по SUBJECT/ACTION/SCENE).\n"
        "     – Сверь с PEOPLE IN FRAME строкой и количеством char-ref'ов.\n"
        "     – Если есть extras без ref'ов — у каждого должна быть отличающая фраза в SUBJECT.\n"
        "     – Если число людей в SUBJECT > 1 и char-ref только один — это красный флаг, либо убери "
        "       extras, либо дай им жёсткую визуальную дифференциацию.\n\n"
        "ОПИСАНИЕ ПЕРСОНАЖА И ОДЕЖДЫ — ТОЛЬКО В BINDING, БОЛЬШЕ НИГДЕ:\n"
        "Сервер автоматически вставит в BINDING-строку каноническое описание каждого персонажа, "
        "вшитое в его карточку (то же самое, по которому генерилось ref-изображение). Это appearance "
        "+ описание текущего outfit'а. Текст совпадает с тем что Seedance видит на @Image — поэтому "
        "он УСИЛИВАЕТ ref, а не конфликтует.\n"
        "  • BINDING после серверной обработки выглядит так:\n"
        "    'В refs: @Image1=Ethan (charcoal three-piece suit, white shirt, slicked-back hair), "
        "@Image2=Maya (navy medical scrubs, hospital ID badge on chest, hair in messy bun), @Image3=Lobby.'\n"
        "    ТЕБЕ его писать с одеждой не нужно — пиши '@Image1=Ethan, @Image2=Maya, @Image3=Lobby', "
        "сервер дополнит. Главное оставь корректные '=' разделители и реальные имена.\n"
        "  • В SUBJECT/ACTION/SCENE одежду НЕ описывай. Никаких 'в красном платье', 'in suit', "
        "'медицинская форма', цветов, тканей, типов — иначе ты дашь альтернативное описание которое "
        "противоречит тому что вшил в BINDING сервер. Описывай только лицо/поза/действие/эмоция.\n"
        "  • ИЗМЕНЁННОЕ СОСТОЯНИЕ одежды (порвана, мокрая, в крови, потеряна пуговица) — "
        "можно и нужно ('разорванная блузка'), но БЕЗ исходного описания ('разорванная белая блузка' — нет, "
        "цвет уже в BINDING). Используй родовое слово: 'блузка', 'рубашка', 'пиджак'.\n"
        "  • Hair/makeup: описывай только если меняется состояние (растрёпанные волосы, "
        "размазанная помада). Базовый стиль причёски уже в BINDING."
    )
    # Active episode cast & locations (already checked off in sidebar)
    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    def _active_char_line(c):
        line = f"- {c['name']} (id={c['id']}): {c.get('appearance','')[:120]}"
        labels = [o.get('label') for o in (c.get('outfits') or []) if o.get('avai_url') and o.get('label')]
        if labels:
            line += "\n  outfit-варианты: " + ", ".join(f'"{l}"' for l in labels) + ", null"
        return line
    active_chars_block = '\n'.join(_active_char_line(c) for c in active_chars) or '(не отмечены)'
    active_locs_block = '\n'.join(
        f"- {l['name']} (id={l['id']}): {l.get('description','')[:120]}"
        for l in active_locs
    ) or '(не отмечены)'

    # ── LOCKED-REFS mode ─────────────────────────────────────────────────────
    # When caller passes `locked_refs`, Claude must use EXACTLY those — no extra
    # auto-detection. Used by manual "Перекомпоновать с текущими рефами" after
    # the user pruned the ref list.
    locked_refs_block = ''
    if locked_refs:
        # Build a human-readable summary of the locked roster + a strict directive.
        char_lookup = {c['id']: c for c in (s.get('characters') or [])}
        loc_lookup  = {l['id']: l for l in (s.get('locations') or [])}
        locked_lines = []
        for r in locked_refs:
            kind = (r.get('kind') or '').lower()
            rid = r.get('id') or ''
            outfit = r.get('outfit') or ''
            if kind == 'char':
                ch = char_lookup.get(rid)
                if ch:
                    appearance = (ch.get('appearance') or '')[:120]
                    suffix = f' / outfit="{outfit}"' if outfit and outfit != 'base' else ''
                    locked_lines.append(f'- char "{ch["name"]}" (id={rid}{suffix}): {appearance}')
            elif kind == 'loc':
                lc = loc_lookup.get(rid)
                if lc:
                    locked_lines.append(f'- loc "{lc["name"]}" (id={rid}): {(lc.get("description") or "")[:120]}')
            elif kind == 'lastframe':
                locked_lines.append('- lastframe (continuity reference — not a character)')
            elif kind == 'cutframe':
                locked_lines.append('- cutframe (continuity reference — not a character)')
            elif kind == 'url':
                locked_lines.append(f'- custom image url ref ({rid}) — treat as a fixed visual element')
        locked_refs_block = (
            '\n=== LOCKED REFS — STRICT MODE ═══\n'
            'Caller pinned the EXACT refs[] for this composition. Do NOT pick anything else, '
            'do NOT auto-detect missing characters from the chunk, do NOT add the location '
            'unless it is in the list below. Your refs[] output MUST match this list 1-to-1 '
            'in the same order, with the same `outfit` value for each char.\n'
            'PINNED ROSTER:\n' + '\n'.join(locked_lines) + '\n'
            'If the chunk text mentions characters NOT in this roster — DO NOT bind them to @ImageN, '
            'DO NOT describe them in SUBJECT/SCENE. Treat them as off-screen. The viewer will not see '
            'them in this shot. Rewrite the prompt to focus on the pinned roster only.\n'
            '=== END LOCKED REFS ═══\n'
        )

    full_script = (ep.get('script') or '').strip()
    full_script_block = full_script[:8000]  # safety cap

    base_only_block = ''
    if base_outfits_only:
        base_only_block = (
            "\n=== BASE OUTFITS ONLY — STRICT ===\n"
            "Для КАЖДОГО персонажа в refs ВСЕГДА выставляй \"outfit\": null. "
            "Не подбирай и не упоминай альтернативные outfit-варианты этого персонажа "
            "(костюмы для других сцен, формы, повседневные вариации). "
            "Используется только базовое референс-фото каждого персонажа. "
            "В тексте промпта тоже не описывай специфическую одежду которая отличается от базы — "
            "только то что видно на базовом референсе.\n"
            "=== END BASE OUTFITS ONLY ===\n"
        )

    # Heuristic auto-detection of single-speaker chunks → soft hint to composer.
    # Looks for: (a) exactly 1 unique speaker in chunk, (b) ZERO bracketed/prose
    # action remarks (those almost always involve a 2nd actor or physical
    # interaction), (c) no other char names in non-dialogue text — checked via
    # canonical name AND first-name-token (e.g. "WOLF" matches both WOLF and
    # WOLF_SON), and we DEFER the hint when any uppercase Cyrillic name-like
    # token appears in action text (likely a Russian declension of a character
    # name not covered by our English aliases — e.g. "Волчонка" → WOLF_SON).
    # Was burning composer in the wolf-bull warehouse scene where Bull pushes
    # WOLF_SON forward in a bracketed remark — heuristic fired single-speaker,
    # composer dropped the cub from refs.
    active_char_names = [c['name'] for c in active_chars]
    # Build alias set per active char: canonical name + first-name-token +
    # last-name-token (e.g. "WOLF_SON" → {"wolf_son", "wolf"}; "DR OLIVER
    # CROSS" → {"dr oliver cross", "dr", "cross", "dr cross"}). Helps match
    # both stems and abbreviated forms (script writes "DR CROSS:" while
    # series stores "DR OLIVER CROSS").
    def _aliases(name):
        al = {name.lower()}
        tokens = re.split(r'[\s_]+', name.strip())
        tokens = [t for t in tokens if t]
        if tokens:
            al.add(tokens[0].lower())
            if len(tokens) > 1:
                al.add(tokens[-1].lower())
            # First + last token combined ("DR CROSS" from "DR OLIVER CROSS")
            if len(tokens) >= 3:
                al.add(f'{tokens[0]} {tokens[-1]}'.lower())
        return al

    # Detect ALL speakers in this chunk and surface them as a HARD directive in
    # the composer prompt. Composer occasionally drops a speaker (especially on
    # entrance shots where Vision-analyzed prev lastframe shows only one char
    # and the Russian-declined name in [Дверь… Dr Cross входит] doesn't match
    # the Vision state-analysis output). Forcing speakers into refs[] eliminates
    # this whole class of "the second speaker isn't in the scene" failures.
    chunk_speakers = []
    if chunk_text and active_char_names:
        seen_speakers = set()
        for line in chunk_text.split('\n'):
            stripped = line.lstrip()
            m = re.match(r'^([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-\']{0,30}):\s', stripped)
            if not m: continue
            nm = m.group(1).strip().lower()
            for cn in active_char_names:
                if nm in _aliases(cn) and cn not in seen_speakers:
                    chunk_speakers.append(cn)
                    seen_speakers.add(cn)
                    break

    auto_close_up_block = ''
    if not close_up_only:
        if active_char_names and chunk_text:
            speakers_found = set(chunk_speakers)
            if len(speakers_found) == 1:
                sole = next(iter(speakers_found))
                # Strip quoted strings + dialogue cue tails so we only check action-text
                action_only = re.sub(r'"[^"]*"', '', chunk_text)
                action_only = re.sub(r'^[A-Za-zА-Яа-яЁё][^:\n]{0,30}:.*$', '', action_only, flags=re.MULTILINE)
                action_only_stripped = action_only.strip()
                others_in_action = []
                for cn in active_char_names:
                    if cn.lower() == sole.lower():
                        continue
                    for alias in _aliases(cn):
                        if re.search(r'\b' + re.escape(alias) + r'\b', action_only, re.IGNORECASE):
                            others_in_action.append(cn)
                            break
                # Tripwire: ANY bracketed [...] action-prose or substantial
                # parenthesized prose almost always involves a 2nd actor.
                # Don't risk dropping refs in those cases.
                bracketed_action = bool(re.search(r'\[[^\]]{8,}\]', action_only))
                paren_action = bool(re.search(r'\([^\)]{12,}\)', action_only))
                has_action_prose = bool(action_only_stripped) and (bracketed_action or paren_action or len(action_only_stripped) > 30)
                # Tripwire: any Cyrillic capitalized word in action text — almost
                # certainly a Russian-declined character name not in our alias set.
                has_cyrillic_proper = bool(re.search(r'[А-ЯЁ][а-яё]{2,}', action_only))
                trigger_close_up = (
                    not others_in_action
                    and not has_action_prose
                    and not has_cyrillic_proper
                )
                if trigger_close_up:
                    auto_close_up_block = (
                        f"\n=== AUTO-DETECTED: SINGLE-SPEAKER CHUNK ===\n"
                        f"В этом CHUNK'е говорит ровно ОДИН персонаж ({sole}) и нет упоминаний "
                        f"других персов в action-ремарках. Это сильный сигнал на CLOSE-UP / single-subject шот.\n"
                        f"DEFAULT: refs = [{sole} + локация]. НЕ ДОБАВЛЯЙ других персов даже если они "
                        f"были в IN FRAME предыдущего чанка — для ЭТОГО кадра они вне фрейма.\n"
                        f"В CAMERA пиши 'крупный план {sole}' или 'medium close-up {sole}', НЕ 'over-the-shoulder', "
                        f"НЕ 'wide', НЕ группу.\n"
                        f"Исключение: если в continuity-кадрах ENDING STATE предыдущего чанка явно описывает "
                        f"физическое взаимодействие (держат за горло, обнимают и т.п.) И это продолжается "
                        f"на текущем CHUNK'е — тогда необходимый второй перс остаётся.\n"
                        f"=== END AUTO-DETECTED ===\n"
                    )

    close_up_block = ''
    if close_up_only:
        close_up_block = (
            "\n=== CLOSE-UP ONLY MODE — STRICT ===\n"
            "Это CLOSE-UP / single-subject шот. Жёсткие правила:\n"
            "  1. refs[] = МАКСИМУМ ОДИН char-ref (тот кто говорит больше всего слов в CHUNK'е "
            "или единственный спикер) + локация. ВСЁ.\n"
            "  2. Игнорируй persons из 'IN FRAME' предыдущего чанка — они присутствуют в сцене "
            "но НЕ в этом кадре. Не добавляй их в refs.\n"
            "  3. Continuity-кадры (lastframe / cutframes) тоже могут принести лица других персов в кадр. "
            "Они всё ещё прицепятся как композиционные референсы, НО в тексте промпта явно скажи: "
            "'tight close-up на лицо <Имя>, остальные вне кадра, размытый/тёмный фон'.\n"
            "  4. В CAMERA блоке промпта: 'tight close-up', 'extreme close-up' или 'medium close-up' — "
            "не 'wide', не 'two-shot', не 'group'.\n"
            "  5. SCENE: упомяни локацию через @Image, но добавь 'фон вне фокуса / приглушён' "
            "чтобы Seedance не пытался прорисовать остальных людей в задних планах.\n"
            "=== END CLOSE-UP ONLY ===\n"
        )

    style_block = ''
    if style_override:
        style_block = (
            "\n=== STYLE OVERRIDE (важно) ===\n"
            f"Финальный визуальный стиль клипа: «{style_override}».\n"
            "Перепиши блок STYLE/ATMOSPHERE целиком вокруг этого стиля. Также добавь "
            "стилистические маркеры в SUBJECT и SCENE (рендер/материалы/освещение/палитра/линии лиц). "
            "Если стиль предполагает анимацию или нереалистичный рендер (Pixar / anime / claymation / oil painting) — "
            "явно укажи это в первых 20 словах промпта (модель решает рендер по началу). "
            "Реплики персонажей в кавычках НЕ менять. Continuity-логику (персонажи в кадре, позы, локация) "
            "сохрани как обычно — стиль это только оболочка рендера, не сюжет.\n"
            "=== END STYLE ===\n"
        )
    # Hard directive block — server-detected speakers in this chunk that
    # composer must not drop from refs[]. Empty when chunk has no recognized
    # dialogue lines (action-only or extras-only chunks).
    mandatory_speakers_block = ''
    if chunk_speakers:
        mandatory_speakers_block = (
            f"=== ОБЯЗАТЕЛЬНЫЕ СПИКЕРЫ (детектировано сервером в CHUNK) ===\n"
            f"Эти персонажи ГОВОРЯТ в этом чанке (есть строки 'Name:' с их именем или алиасом). "
            f"Они ВИДНЫ В КАДРЕ во время своей реплики (даже если на lastframe их не было — реплика "
            f"это и есть момент их появления). ВКЛЮЧИ их в refs[] и в BINDING — невключение спикера "
            f"= провал композиции:\n"
            + '\n'.join(f"  - {nm}" for nm in chunk_speakers) + '\n'
            + f"=== END ОБЯЗАТЕЛЬНЫЕ СПИКЕРЫ ===\n\n"
        )

    # Active items in THIS episode (for the prompt's "ACTIVE THIS EPISODE" section)
    active_item_ids = set(ep.get('items_used') or [])
    active_items_block = '\n'.join(
        f"  - {it['name']} (id={it['id']})"
        for it in (s.get('items', []) or [])
        if it.get('id') in active_item_ids and it.get('avai_url')
    ) or '  (none)'

    userprompt = (
        f"AVAILABLE CHARACTERS (весь roster серии):\n{chr(10).join(chars_lines) or '(none)'}\n\n"
        f"AVAILABLE LOCATIONS (весь roster серии):\n{chr(10).join(locs_lines) or '(none)'}\n\n"
        f"AVAILABLE ITEMS (сюжетные предметы — букеты, конверты, локеты, флешки и т.п.):\n{chr(10).join(items_lines) or '(none)'}\n\n"
        f"ACTIVE THIS EPISODE (отмечены в эпизоде — приоритет при выборе):\n"
        f"  Characters:\n{active_chars_block}\n"
        f"  Locations:\n{active_locs_block}\n"
        f"  Items:\n{active_items_block}\n"
        f"{base_only_block}"
        f"{close_up_block}"
        f"{auto_close_up_block}"
        f"{locked_refs_block}"
        f"{style_block}"
        f"{prev_block}\n"
        f"FULL EPISODE SCRIPT (читай ВЕСЬ — тут scene headings, ремарки, кто где находится):\n"
        f"```\n{full_script_block}\n```\n\n"
        f"CHUNK — выделенный кусок для генерации (его репликам сохраняй verbatim):\n"
        f"```\n{chunk_text}\n```\n\n"
        f"Найди CHUNK внутри FULL SCRIPT, посмотри ближайший SCENE HEADING выше него — оттуда возьми локацию и время суток. "
        f"Посмотри ремарки/[действия] вокруг CHUNK — оттуда возьми кто физически в кадре (включая молчащих). "
        f"Эти персонажи ОБЯЗАТЕЛЬНО идут в refs, даже если в CHUNK у них нет реплик.\n\n"
        f"{mandatory_speakers_block}"
        "Верни JSON и НИЧЕГО кроме JSON:\n"
        "{\n"
        '  "prompt": "ru/en motion prompt, ~60-110 слов, по структуре выше, с эмоциями перед каждой репликой и финальным @Image<N> локации",\n'
        '  "refs": [{"kind":"char","id":"...","outfit":"label_or_null"}, ..., {"kind":"item","id":"..."}, ..., {"kind":"loc","id":"..."}],\n'
        '  "scene_continuity": true|false,\n'
        '  "reasoning": "одно предложение — почему именно эти референсы и continuity"\n'
        "}\n\n"
        "ПРОВЕРКА перед выводом:\n"
        "- BINDING-строка (@Image1=<имя>, @Image2=<имя>, ...) идёт ПЕРВОЙ в prompt? Если нет — допиши.\n"
        "- В SUBJECT/ACTION/SCENE используются ИМЕНА (без @ImageN)? Если нашёл @ImageN в этих блоках — замени на имя.\n"
        "- В DIALOGUE shot-bit'ах есть И имя И @ImageN ('Maya (@Image2)')? Это нужно для lipsync.\n"
        "- ВСЕ описания на русском (кроме реплик в кавычках)? Если нашёл английское слово вне кавычек — перепиши.\n"
        "- Каждая реплика из chunk имеет эмоцию/тон по-русски перед ней? Если нет — допиши.\n"
        "- Локация прикреплена последним ref и упомянута в SCENE именем? Если в roster есть подходящая — обязательно.\n"
        "- Все @ImageN из BINDING/DIALOGUE совпадают по индексу с порядком в refs?\n"
        "- В prompt нет 'no <X>'? Замени на позитив.\n"
        "- ВЫБОР OUTFIT: для каждого char-ref значение outfit либо ровно из списка под этим персом в roster, либо null. "
        "Если PREVIOUS CHUNK той же сцены — outfit совпадает с PREVIOUS дословно (см. IN FRAME).\n"
        "- В SUBJECT/ACTION/SCENE НЕТ описания одежды (цвета/типы костюма/ткани). Сервер сам вставит "
        "описание в BINDING из Vision-анализа ref'а. Если нашёл клозет-описание вне BINDING — удали. "
        "Допустимо только описание ИЗМЕНЁННОГО состояния (порвана, мокрая, в крови) родовыми словами.\n"
        "- АНТИ-ДУБЛЬ: каждый персонаж в refs[] ОДИН раз (по id). Никаких повторов одного перса с разными outfit'ами. "
        "Каждый @ImageN в тексте промпта тоже привязан к ровно одному персу."
    )
    try:
        raw = claude_ask(userprompt, system=sysprompt)
        data = json.loads(strip_json(raw))
    except Exception as e:
        return jsonify({'error': f'compose failed: {e}'}), 500

    refs = data.get('refs') or []
    # If base-only flag is on, strip outfit selection from every char ref
    if base_outfits_only:
        for r in refs:
            if r.get('kind') == 'char':
                r['outfit'] = None
    # CLOSE-UP ONLY post-filter: drop all char-refs except the one whose name
    # actually appears in CHUNK text (tightest single-subject framing). Keeps
    # location refs untouched. Belt-and-suspenders insurance — composer prompt
    # should already produce single-char refs, but if it carried over prev-chunk
    # bystanders we strip them here.
    closeup_dropped = []
    if close_up_only:
        # Build name → first chunk-text appearance position for each char in roster
        chars_in_chunk = []  # list of (id, position-in-chunk) for chars whose name appears
        ct_lower = chunk_text.lower()
        for r in refs:
            if r.get('kind') != 'char':
                continue
            ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
            if not ch:
                continue
            pos = ct_lower.find(ch['name'].lower())
            if pos >= 0:
                chars_in_chunk.append((r.get('id'), pos))
        chars_in_chunk.sort(key=lambda x: x[1])
        keep_id = chars_in_chunk[0][0] if chars_in_chunk else (
            # No char names found in chunk — keep first char ref the composer chose
            next((r.get('id') for r in refs if r.get('kind') == 'char'), None)
        )
        filtered = []
        for r in refs:
            if r.get('kind') == 'char' and r.get('id') != keep_id:
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                closeup_dropped.append({'char_id': r.get('id'), 'char_name': ch['name'] if ch else r.get('id')})
                continue
            filtered.append(r)
        refs = filtered
    # Hard de-dup: never let the same character go in twice (a frequent cause
    # of "two Mayas in one frame"). Keep the FIRST occurrence — usually that's
    # the composer's preferred (often outfit-specific) one. Any subsequent
    # ref with the same kind=char + id is dropped and reported.
    deduped_refs = []
    seen_char_ids = set()
    duplicate_chars = []
    for r in refs:
        if r.get('kind') == 'char':
            cid = r.get('id')
            if cid in seen_char_ids:
                duplicate_chars.append({'char_id': cid, 'dropped_outfit': r.get('outfit')})
                continue
            seen_char_ids.add(cid)
        deduped_refs.append(r)
    refs = deduped_refs
    ref_urls = []
    ref_meta = []
    unresolved = []
    outfit_fallbacks = []  # composer requested outfit label that doesn't exist → fell back to base
    for r in refs:
        url = _resolve_ref_url(s, r, sid=sid)
        if url:
            if r.get('_outfit_fallback'):
                # Composer hallucinated an outfit label — log so user sees the drift cause
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                outfit_fallbacks.append({
                    'char_id': r.get('id'),
                    'char_name': ch['name'] if ch else r.get('id'),
                    'requested_outfit': r.get('_outfit_requested'),
                    'available_labels': [o.get('label') for o in (ch.get('outfits') or []) if o.get('avai_url')] if ch else [],
                })
            # Strip private bookkeeping fields before returning to client
            clean = {k: v for k, v in r.items() if not k.startswith('_')}
            ref_urls.append(url)
            ref_meta.append({**clean, 'url': url})
        else:
            unresolved.append({**r, 'reason': 'нет фото / avai_url не получился'})

    # CRITICAL: server-side filtering (close-up, dedup, unresolved) may have
    # dropped refs that the composer's prompt still references via @ImageN.
    # If we don't sync the prompt with the final refs[], Seedance will see
    # @ImageN tokens with no matching reference image and hallucinate a random
    # face for that 'character'. Real bug from production: refs=[Adrian, Lobby]
    # but prompt said @Image2=Vivian, @Image3=Sophie, @Image4=Clara → garbage.
    _tag_remap = _build_image_tag_remap(data.get('refs') or [], ref_meta)
    if any(v is None for v in _tag_remap.values()) or any(k != v for k, v in _tag_remap.items() if v is not None):
        data['prompt'] = _remap_image_tags(data.get('prompt') or '', _tag_remap)

    # CANONICAL char description for BINDING — uses the SAME text that was fed
    # to the image generator (appearance + outfit description). Visual ref and
    # textual description are guaranteed aligned.
    name_to_clothing = {}   # keyed by full name AND first name for prompt lookup
    for r in ref_meta:
        if r.get('kind') != 'char':
            continue
        desc = _canonical_char_description(s, r.get('id'), r.get('outfit'))
        if not desc:
            continue
        ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
        if ch:
            full_name = ch['name']
            name_to_clothing[full_name] = desc
            # Composers write first-name-only in BINDING (e.g. "Marcus" not "Marcus Bellacourt").
            # Index by first name too so the regex lookup matches.
            first_name = full_name.split()[0]
            if first_name != full_name and first_name not in name_to_clothing:
                name_to_clothing[first_name] = desc
            r['clothing'] = desc          # surface in response so UI can show it
    # Inject descriptions into the BINDING line. Pattern: '@Image1=Maya' →
    # '@Image1=Maya (navy scrubs, hair in bun)'. Only the first occurrence per
    # name so we don't duplicate inside DIALOGUE shot-bits.
    if name_to_clothing:
        prompt_text = data.get('prompt') or ''
        already_injected = set()
        def _inject_clothing(m):
            num = m.group(1)
            name = m.group(2).strip()
            if name in already_injected:
                return m.group(0)
            desc = name_to_clothing.get(name)
            if not desc:
                return m.group(0)
            already_injected.add(name)
            return f'@Image{num}={name} ({desc})'
        # Match `@Image<N>=<Name>` with `=` or `—` or `-` separator
        prompt_text = re.sub(
            r'@Image(\d+)\s*[=—\-]\s*([A-Za-zА-яЁё][A-Za-zА-яЁё\d _\-]{0,30})',
            _inject_clothing, prompt_text
        )
        # Fallback: no @ImageN binding lines found at all → prepend explicit note
        missing = [nm for nm in name_to_clothing if nm not in already_injected
                   and nm.split()[0] not in already_injected]
        # Deduplicate: keep only full names (skip first-name aliases already covered)
        missing_full = [nm for nm in missing if nm in {ch2['name'] for ch2 in (s.get('characters') or [])}]
        if not already_injected:
            # Composer wrote no BINDING line at all — prepend one
            binding_parts = [f'{nm} ({name_to_clothing[nm]})' for nm in missing_full]
            prompt_text = (
                'Note: одежда из refs — ' + ', '.join(binding_parts) + '. '
                'Используй эти описания если упоминаешь одежду; больше ничего о ней не пиши.\n\n'
                + prompt_text
            )
        elif missing_full:
            # Some chars were injected but others were missed (shouldn't happen now,
            # but as a safety net append the missed ones after the binding line).
            extra = ', '.join(f'{nm} ({name_to_clothing[nm]})' for nm in missing_full)
            prompt_text = re.sub(
                r'(В refs:[^\n]+)',
                lambda m2: m2.group(0) + f' Также в сцене: {extra}.',
                prompt_text, count=1
            )
        data['prompt'] = prompt_text

    # Optional: attach previous chunk's LAST FRAME as a continuity reference
    lastframe_attached = False
    if (use_prev_lastframe and prev_neighbour
            and prev_neighbour.get('video_path')
            and data.get('scene_continuity') is not False
            and len(ref_urls) < 9):
        try:
            extracted = _extract_last_frame(sid, prev_neighbour['video_path'])
            if extracted:
                relpath, abs_path = extracted
                lf_url = prev_neighbour.get('lastframe_avai_url')
                if not lf_url:
                    try:
                        lf_url = _avai_upload_local_image(abs_path)
                        prev_neighbour['lastframe_avai_url'] = lf_url
                        # save to whichever episode owns the prev_neighbour
                        save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
                    except Exception:
                        lf_url = None
                if lf_url:
                    img_idx = len(ref_urls) + 1  # 1-based
                    ref_urls.append(lf_url)
                    ref_meta.append({
                        'kind': 'lastframe',
                        'source': 'prev_chunk',
                        'prev_idx': prev_neighbour.get('idx'),
                        'name': f'last frame · prev #{prev_neighbour.get("idx")}',
                        'url': lf_url,
                    })
                    extra = (
                        f"\n\nДополнительно: @Image{img_idx} — это ПОСЛЕДНИЙ КАДР предыдущего чанка "
                        f"эпизода. Биндить его в BINDING-строке НЕ надо (это композиционный референс, не персонаж/локация). "
                        f"Используй его ТОЛЬКО как continuity-референс: повтори ту же расстановку "
                        f"персонажей и тот же задний план, плавно продолжая действие. НЕ описывай его как "
                        f"отдельный кадр в SUBJECT/SCENE.\n"
                        f"АНТИ-ДУБЛИРОВАНИЕ: персонажи видные на этом lastframe НЕ создают для себя дополнительные "
                        f"char-ref'ы. Если на кадре Maya — это та же Maya из BINDING, не считай её отдельной. "
                        f"В тексте промпта упоминай её только именем 'Maya' (или через её BINDING-@Image в DIALOGUE)."
                    )
                    data['prompt'] = (data.get('prompt') or '').rstrip() + extra
                    lastframe_attached = True
        except Exception:
            pass

    # Optional: ALSO attach pre-cut keyframes from prev chunk's video.
    # Detects internal cuts (склейки) inside the prev clip and grabs the last
    # frame of EACH outgoing shot. Gives the next chunk visual context for
    # mid-clip mise-en-scène, not just the absolute final frame. Capped at 2
    # extra frames to leave budget for char/loc refs.
    cutframes_attached = 0
    if (use_prev_cutframes and prev_neighbour
            and prev_neighbour.get('video_path')
            and data.get('scene_continuity') is not False
            and len(ref_urls) < 9):
        try:
            video_abs = series_path(sid) / prev_neighbour['video_path']
            # Cache cut timestamps on the chunk so we don't re-run ffmpeg every compose.
            cuts = prev_neighbour.get('cuts_detected')
            if cuts is None:
                cuts = _detect_cuts(str(video_abs))
                prev_neighbour['cuts_detected'] = cuts
                save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
            if cuts:
                # Cache uploaded urls per-cut on the chunk to avoid re-uploading.
                cf_urls = list(prev_neighbour.get('cutframes_avai_urls') or [])
                budget = 9 - len(ref_urls)
                # Up to 3 extra cut frames — gives the model full continuity for
                # 3-shot prev chunks (most common in our pacing). Refs hard cap
                # is 9 so we leave 6 slots for chars + locations + lastframe.
                max_attach = min(3, budget)
                wanted_cuts = cuts[:max_attach]
                # Need to extract any frames not yet uploaded
                if len(cf_urls) < len(wanted_cuts):
                    extracted = _extract_keyframes_at_cuts(
                        sid, prev_neighbour['video_path'], wanted_cuts, max_frames=max_attach
                    )
                    while len(cf_urls) < len(extracted):
                        relpath, abs_path = extracted[len(cf_urls)]
                        try:
                            cf_urls.append(_avai_upload_local_image(abs_path))
                        except Exception:
                            break
                    prev_neighbour['cutframes_avai_urls'] = cf_urls
                    save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
                # Attach as refs (cap to budget)
                cut_extras = []
                for i, cf_url in enumerate(cf_urls[:max_attach]):
                    if not cf_url or len(ref_urls) >= 9:
                        break
                    img_idx = len(ref_urls) + 1
                    ref_urls.append(cf_url)
                    ref_meta.append({
                        'kind': 'cutframe',
                        'source': 'prev_chunk',
                        'prev_idx': prev_neighbour.get('idx'),
                        'cut_index': i,
                        'cut_time': float(wanted_cuts[i]) if i < len(wanted_cuts) else None,
                        'name': f'pre-cut #{i+1} · prev #{prev_neighbour.get("idx")}',
                        'url': cf_url,
                    })
                    cut_extras.append(f"@Image{img_idx} (кадр перед {i+1}-й склейкой прошлого чанка)")
                    cutframes_attached += 1
                if cut_extras:
                    extra = (
                        "\n\nЕщё continuity-референсы: " + ", ".join(cut_extras) + ". "
                        "Это последние кадры разных шотов внутри прошлого чанка — каждый показывает "
                        "состав в кадре и расстановку до соответствующей склейки. Биндить их в BINDING-строке "
                        "НЕ надо. Используй их вместе с last frame для понимания мизансцены. "
                        "В SUBJECT/SCENE их КАК отдельные кадры НЕ описывай.\n"
                        "АНТИ-ДУБЛИРОВАНИЕ: персонажи на этих cutframe'ах НЕ создают новых char-ref'ов. "
                        "Если Maya видна на cutframe — это та же Maya из BINDING, не отдельная инстанция. "
                        "В тексте промпта ссылайся на неё именем."
                    )
                    data['prompt'] = (data.get('prompt') or '').rstrip() + extra
        except Exception:
            pass

    # ── State analysis: tell Claude WHAT'S in each attached continuity frame ──
    # If we attached lastframe and/or cutframes, run a single Haiku Vision call
    # to label what each frame ACTUALLY shows (who's visible, in what state,
    # mise-en-scène). Inject into the prompt so the composer knows e.g. "Maya
    # has a busted lip and wet hair RIGHT NOW" — base portrait won't tell it that.
    state_analysis_attached = False
    state_frames = [r for r in ref_meta if r.get('kind') in ('lastframe', 'cutframe')]
    if state_frames and prev_neighbour:
        # v2 = structured-fields format (posture / location / hands / contact /
        # emotion / damage). Bumping invalidates v1 cache entries that only had
        # a free-form one-liner → next compose re-runs vision with new schema.
        cache_key = 'v2|' + '|'.join(r.get('url', '') for r in state_frames)
        cached = prev_neighbour.get('frame_state_analysis') or {}
        analysis_text = ''
        if cached.get('cache_key') == cache_key and cached.get('text'):
            analysis_text = cached['text']
        else:
            # Roster of chars who were in prev chunk — bound the labelling task
            prev_char_ids = [r['id'] for r in (prev_neighbour.get('refs') or []) if r.get('kind') == 'char']
            prev_chars = [c for c in (s.get('characters') or []) if c['id'] in prev_char_ids]
            roster_lines = '\n'.join(
                f'  - {c["name"]}: {c.get("appearance","")[:140]}'
                for c in prev_chars
            ) or '  (нет данных о составе прошлого чанка)'
            frame_labels = []
            for i, r in enumerate(state_frames):
                if r.get('kind') == 'lastframe':
                    frame_labels.append(f'Кадр {i+1}: lastframe (последний кадр прошлого чанка)')
                else:
                    frame_labels.append(f'Кадр {i+1}: cutframe #{r.get("cut_index", i)+1} (перед склейкой внутри прошлого чанка)')
            v_prompt = (
                "Опиши ТЕКУЩЕЕ ФИЗИЧЕСКОЕ СОСТОЯНИЕ персонажей на присоединённых кадрах. "
                "Это последние кадры предыдущего сгенерированного видео-чанка, используются как continuity-контекст для генерации СЛЕДУЮЩЕГО чанка.\n\n"
                f"ПЕРСОНАЖИ ИЗ ПРОШЛОГО ЧАНКА (roster — кого ожидать в кадрах):\n{roster_lines}\n\n"
                f"КАДРЫ В ТОМ ЖЕ ПОРЯДКЕ ЧТО ПРИКРЕПЛЕНЫ:\n" + '\n'.join(frame_labels) + "\n\n"
                "Для КАЖДОГО кадра по порядку:\n"
                "1) Видимые персонажи (имя из roster + краткое 'кто это', если roster короткий — просто имя).\n"
                "2) Для каждого СТРОГО по этим полям:\n"
                "   • Поза: ровно одно из — стоит / сидит / на коленях / лежит / приседает / опирается / наклоняется.\n"
                "   • Где именно в комнате: у двери / у окна / в центре / в углу / за столом / перед камином / "
                "     рядом с [имя другого перса] / на заднем плане.\n"
                "   • Что в руках: телефон / бокал / документ / нож / чашка / ничего. Если в одной руке одно — пиши какой.\n"
                "   • Физический контакт с другими: держит [имя] за руку / обнимает [имя] / нависает над [имя] / "
                "     никакого контакта.\n"
                "   • Выражение лица: страх / гнев / слёзы / шок / нейтральное / улыбка.\n"
                "   • Видимые повреждения: синяки, кровь, царапины, мокрые волосы, разорванная одежда (или 'нет').\n"
                "3) Общая мизансцена кадра (где, освещение, ключевые объекты на фоне).\n\n"
                "ПРАВИЛА:\n"
                "- Описывай ТОЛЬКО то что РЕАЛЬНО видно на кадре. НЕ додумывай и НЕ фантазируй про повреждения которых нет.\n"
                "- Если перс из roster на кадре не виден — НЕ упоминай его.\n"
                "- На каждого перса = ОДНА строка вида «Имя: поза | где | в руках | контакт | эмоция | повреждения».\n"
                "- ПО-РУССКИ.\n\n"
                "Формат строго (поля через | в одной строке на персонажа):\n"
                "Кадр 1 (lastframe):\n"
                "  • <Имя>: поза=стоит | где=у книжного шкафа справа | в руках=бокал виски | контакт=нет | эмоция=напряжён | повреждения=нет\n"
                "  • <Имя>: поза=сидит | где=за столом по центру | в руках=ничего, ладони на документах | контакт=нет | эмоция=шок | повреждения=нет\n"
                "  • Сцена: <мизансцена одной строкой — локация, свет, ключевые объекты фона>\n"
                "Кадр 2 (cutframe #1):\n"
                "  • ..."
            )
            try:
                urls_only = [r.get('url') for r in state_frames if r.get('url')]
                analysis_text = claude_ask_vision(v_prompt, urls_only).strip()
                if analysis_text:
                    prev_neighbour['frame_state_analysis'] = {
                        'cache_key': cache_key,
                        'text': analysis_text,
                    }
                    save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
            except Exception as e:
                print(f'[seedance_compose] vision analysis failed: {e}', flush=True)
                analysis_text = ''

        if analysis_text:
            state_extra = (
                "\n\nТЕКУЩЕЕ СОСТОЯНИЕ ПЕРСОНАЖЕЙ И СЦЕНЫ (из присоединённых continuity-кадров):\n"
                f"{analysis_text}\n\n"
                "ВАЖНО: базовые портреты персонажей (@Image1, @Image2...) показывают КАНОНИЧЕСКИЙ ВИД персонажа "
                "ДО событий сцены. ТЕКУЩЕЕ СОСТОЯНИЕ — то что описано выше из continuity-кадров. "
                "В SUBJECT/ACTION текущего промпта ОБЯЗАТЕЛЬНО отрази это состояние явными словами "
                "(например: 'Maya, на губе кровь из разбитой губы, мокрые волосы, разорванная блузка, дрожит'). "
                "НЕ описывай персонажей как «свежих» / в базовом виде — они продолжаются из прошлого кадра.\n\n"
                "POSTURE/STATE LOCK ИЗ ЭТОГО АНАЛИЗА:\n"
                "Состояние выше — это твой ENDING STATE предыдущего чанка. ВСЁ что там описано (поза стоя/сидя, "
                "где стоит, что в руках, физический контакт) ПЕРЕНОСИТСЯ в начало текущего чанка ОДИН-В-ОДИН — "
                "пока в тексте CHUNK'а нет ЯВНОГО ГЛАГОЛА смены позы (садится, встаёт, берёт, кладёт, выходит). "
                "Если в анализе «Marcus стоит у книжного шкафа, бокал в руке» и в CHUNK нет «садится» — Marcus "
                "в твоём ACTION продолжает СТОЯТЬ у шкафа с бокалом. Не «оба сели за стол» из головы.\n"
                "ОБЯЗАТЕЛЬНАЯ первая строка ACTION: «Продолжая с прошлого чанка: [имя1] [поза] [где] [с чем]; "
                "[имя2] [поза] [где] [с чем]». Это эхо состояния — модель должна увидеть его в prompt-е."
            )
            data['prompt'] = (data.get('prompt') or '').rstrip() + state_extra
            state_analysis_attached = True

    debug_prev = None
    if prev_neighbour:
        debug_prev = {
            'idx': prev_neighbour.get('idx'),
            'episode': prev_neighbour_ep,
            'has_video': bool(prev_neighbour.get('video_path')),
            'pos_found': cur_pos >= 0,
        }
    return jsonify({
        'prompt': data.get('prompt', ''),
        'refs': ref_meta,
        'ref_urls': ref_urls,
        'unresolved_refs': unresolved,
        'outfit_fallbacks': outfit_fallbacks,
        'duplicate_chars_dropped': duplicate_chars,
        'closeup_dropped': closeup_dropped if close_up_only else [],
        'auto_close_up_detected': bool(auto_close_up_block),
        'scene_continuity': data.get('scene_continuity'),
        'reasoning': data.get('reasoning', ''),
        'lastframe_attached': lastframe_attached,
        'cutframes_attached': cutframes_attached,
        'state_analysis_attached': state_analysis_attached,
        'prev_neighbour': debug_prev,
        'cur_pos_found': cur_pos >= 0,
    })

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/start', methods=['POST'])
def seedance_start(sid, num):
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    prompt = (body.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'prompt required'}), 400
    ref_urls = body.get('ref_urls') or []
    # accept ref descriptors {kind,id,outfit} too
    if not ref_urls and body.get('refs'):
        for r in body['refs']:
            u = _resolve_ref_url(s, r, sid=sid)
            if u:
                ref_urls.append(u)
    duration = max(5, min(15, int(body.get('duration') or 15)))
    resolution = body.get('resolution') or '720p'
    if resolution not in ('720p', '480p'):
        resolution = '720p'
    mod = body.get('moderation_bypass') or 'collage_grid'
    if mod not in ('off', 'grid', 'collage_grid', 'cartoon'):
        mod = 'collage_grid'
    aspect = body.get('aspect_ratio') or '9:16'
    gen_audio = bool(body.get('generate_audio', True))
    # Optional canonical script order — set by auto-mode parallel so the UI
    # can sort chunks by script position regardless of network arrival order.
    script_order_raw = body.get('script_order')
    try:
        script_order = int(script_order_raw) if script_order_raw is not None else None
    except (TypeError, ValueError):
        script_order = None

    # Per-episode lock — serializes the read-load-mutate-save cycle so parallel
    # /start calls don't race and erase each other's chunks (Flask threaded=True
    # would otherwise let 4 parallel callers all read [], all add their chunk,
    # all save → only the last write survives).
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)            # re-read inside lock for freshness
        chunks = _seedance_chunks(ep)
        idx = _next_chunk_idx(ep)
        chunk = {
            'idx': idx,
            'job_id': '',
            'status_url': '',
            'status': 'submitting',
            'prompt': prompt,
            'chunk_text': body.get('chunk_text', ''),
            'ref_urls': ref_urls,
            'refs': body.get('refs') or [],
            'duration': duration,
            'resolution': resolution,
            'moderation_bypass': mod,
            'aspect_ratio': aspect,
            'created_at': int(time.time()),
            'video_url': '',
            'video_path': '',
            'ending_state': '',
            'cost': None,
            'script_order': script_order,
        }
        chunks.append(chunk)
        save_episode(sid, num, ep)

    # Kick off the AVAI call in a background thread so the UI gets the chunk
    # card instantly. The poll endpoint will then track job_id/status as soon
    # as the thread finishes the submission.
    #
    # CRITICAL: spawn via _spawn_with_keys so the per-user AVAI key is captured
    # from the live Flask `session` and forwarded to the worker thread. Without
    # this, _get_user_avai_key() inside the thread hits a torn-down request
    # context, silently returns '', and AVAI gets `x-api-key: ` → on prod the
    # request hangs until our 240s read timeout (videos die, images survive
    # only because image calls are synchronous within the request handler).
    def _submit():
        try:
            job = _avai_seedance_start(
                prompt=prompt, ref_urls=ref_urls,
                duration=duration, resolution=resolution,
                moderation_bypass=mod, aspect_ratio=aspect,
                generate_audio=gen_audio,
            )
            with _episode_lock(sid, num):
                ep2 = load_episode(sid, num)
                for c in _seedance_chunks(ep2):
                    if c.get('idx') == idx:
                        c['job_id'] = job['job_id']
                        c['status_url'] = job['status_url']
                        # RESURRECT: even if reaper marked us 'failed' for being
                        # slow, AVAI now confirms the job IS running and we have
                        # a job_id. Wasteful to discard a working render — flip
                        # back to pending so the poll loop tracks it to completion.
                        # User pays for these jobs whether or not we track them.
                        if c.get('status') in ('submitting', 'failed'):
                            prev_status = c.get('status')
                            c['status'] = 'pending'
                            if prev_status == 'failed':
                                c['error'] = ''   # clear stale "submit timed out" message
                                print(f'[seedance {sid}/ep{num}/#{idx}] late submit returned '
                                      f'job_id={job.get("job_id")}, resurrecting from failed → pending', flush=True)
                        break
                save_episode(sid, num, ep2)
        except Exception as e:
            with _episode_lock(sid, num):
                ep2 = load_episode(sid, num)
                for c in _seedance_chunks(ep2):
                    if c.get('idx') == idx:
                        # Don't overwrite a more specific reaper message
                        if c.get('status') != 'failed':
                            c['status'] = 'failed'
                            c['error'] = f'submit failed: {e}'
                        break
                save_episode(sid, num, ep2)

    _spawn_with_keys(_submit)
    return jsonify({'chunk': chunk})

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/poll', methods=['POST'])
def seedance_poll(sid, num):
    """Poll all non-final chunks; download finished mp4s; extract ending_state.
    Holds the per-episode lock so concurrent polls / starts / deletes don't
    clobber each other's writes."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return jsonify({'error': 'not found'}), 404
        chunks = _seedance_chunks(ep)
        changed = False
        now = int(time.time())
        for c in chunks:
            if c.get('status') in ('completed', 'failed'):
                continue
            if not c.get('job_id'):
                if c.get('status') == 'submitting':
                    age = now - int(c.get('created_at') or now)
                    if age > _SUBMITTING_TIMEOUT_RUNTIME_SEC:
                        c['status'] = 'failed'
                        c['error'] = f'submit timed out after {age}s — нажми ↻ Reuse и попробуй снова'
                        changed = True
                continue
            try:
                st = _avai_seedance_status(c['job_id'], c.get('status_url'))
            except Exception as e:
                c['status'] = 'failed'
                c['error'] = str(e)
                changed = True
                continue
            c['status'] = st.get('status') or c['status']
            if st.get('progress') is not None:
                c['progress'] = st['progress']
            if st.get('cost') is not None:
                c['cost'] = st['cost']
            if st.get('error'):
                c['error'] = st['error']
            if st.get('status') == 'completed' and st.get('video_url') and not c.get('video_path'):
                vurl = (st.get('video_url') or '').strip()
                vurl_lower = vurl.lower()
                is_moderation = (
                    'moderation' in vurl_lower
                    or vurl.startswith('/')
                    or not vurl_lower.startswith(('http://', 'https://'))
                    or not (vurl_lower.endswith('.mp4') or '.mp4?' in vurl_lower or '/video' in vurl_lower)
                )
                if is_moderation:
                    c['status'] = 'failed'
                    c['error'] = (
                        f'Заблокировано модерацией Seedance (вернул "{vurl[:60]}"). '
                        'Перепиши промпт мягче — убери: царапины/раны/кровь, удары в лицо, '
                        'обнажение, оружие, явное насилие. Попробуй collage_grid bypass или '
                        'переформулируй действие через эмоцию вместо физического урона.'
                    )
                    changed = True
                    continue
                try:
                    vid_local = vid_dir(sid) / f'seedance_ep{int(num):03d}_chunk{c["idx"]:03d}.mp4'
                    _download_video(vurl, vid_local)
                    c['video_url'] = vurl
                    c['video_path'] = str(vid_local.relative_to(series_path(sid)))
                    # Successful render — wipe any stale error field carried over
                    # from a previous transient failure (e.g. AVAI 401 on a tick
                    # before the user fixed the key, then chunk eventually
                    # completed). Without this the card keeps showing red-text
                    # "401: Unauthorized" even though the video is right there.
                    if c.get('error'):
                        c.pop('error', None)
                    if c.get('chunk_text'):
                        try:
                            es = claude_ask(
                                f"Script chunk just rendered as a video clip:\n{c['chunk_text']}\n\n"
                                "In ONE short sentence (≤25 words) describe the FINAL physical state at the end of this clip: "
                                "where each character is, their pose (standing/sitting/lying), what they hold, who is in frame, "
                                "and the location. No interpretation, just the final tableau.",
                                system="You produce one short factual continuity note. No preamble."
                            ).strip()
                            c['ending_state'] = es[:400]
                        except Exception:
                            pass
                except Exception as e:
                    c['status'] = 'failed'
                    c['error'] = f'download failed: {e}'
            changed = True
        if changed:
            save_episode(sid, num, ep)
        return jsonify({'chunks': chunks})

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>', methods=['DELETE'])
def seedance_delete(sid, num, idx):
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return jsonify({'error': 'not found'}), 404
        chunks = _seedance_chunks(ep)
        chunk = next((c for c in chunks if c.get('idx') == idx), None)
        if not chunk:
            return jsonify({'error': 'chunk not found'}), 404
        # Delete local mp4 if exists
        if chunk.get('video_path'):
            p = series_path(sid) / chunk['video_path']
            try:
                if p.exists(): p.unlink()
            except Exception: pass
        chunks[:] = [c for c in chunks if c.get('idx') != idx]
        save_episode(sid, num, ep)
        return jsonify({'ok': True})

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/heal-prompt', methods=['POST'])
def seedance_heal_prompt(sid, num, idx):
    """Rewrite a chunk's prompt + chunk_text to pass Seedance moderation.
    Returns {prompt, chunk_text, changes:[...], reasoning}."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
    if not chunk:
        return jsonify({'error': 'chunk not found'}), 404

    sysprompt = (
        "Ты — редактор промптов для ByteDance Seedance 2.0. Твоя задача — переписать "
        "промпт и фрагмент сценария так, чтобы они прошли модерацию Seedance, СОХРАНИВ "
        "драматический эффект сцены.\n\n"
        "ЧТО БЛОКИРУЕТ МОДЕРАЦИЯ (заменяй на эмоциональные эквиваленты):\n"
        "  • Кровь, раны, царапины, порезы, синяки → убрать видимые повреждения. "
        "Замена: «звонкий шлепок отпечатывается на щеке», «след от удара», «дрожь от боли».\n"
        "  • Удары в лицо, кулаком, по голове → заменить на: пощёчина (слабая), толчок в плечо, "
        "грубое схватывание за воротник, рывок руки. Без видимого урона.\n"
        "  • Удушение, держать за горло → заменить: «схватил за плечи и трясёт», «прижал к стене за плечо».\n"
        "  • Оружие (нож, пистолет, бита) → убрать или заменить безоружным жестом / угрожающим взглядом.\n"
        "  • Кровь на одежде/полу/руках → убрать.\n"
        "  • Явное насилие, побои, пытки → заменить психологическим давлением, "
        "крик в лицо, нависание, унижающий жест.\n"
        "  • Обнажение, эротика → одежда, сцена в публичном месте.\n"
        "  • Самоубийство, явная смерть → потеря сознания, обморок, шок.\n\n"
        "ЧТО ОБЯЗАТЕЛЬНО СОХРАНИТЬ:\n"
        "  • ВСЕ реплики персонажей в кавычках — VERBATIM (не переводи, не меняй).\n"
        "  • Все ссылки @Image1, @Image2... и их количество/порядок.\n"
        "  • Структуру шотов и склеек, эмоции перед репликами.\n"
        "  • Локацию и общий смысл сцены.\n"
        "  • Язык: описания на русском, реплики как были (обычно англ).\n\n"
        "Верни СТРОГО JSON и ничего кроме него:\n"
        "{\n"
        '  "prompt": "переписанный motion prompt",\n'
        '  "chunk_text": "переписанный фрагмент сценария (если в нём были запрещённые элементы — иначе верни как было)",\n'
        '  "changes": ["изменение 1 коротким предложением", "изменение 2", ...],\n'
        '  "reasoning": "одно предложение — что было критичного и как обошёл"\n'
        "}\n"
        "Каждое изменение в 'changes' пиши ясно, например: "
        '«Убрана царапина на щеке Selena — заменена на красный след от удара картой (без повреждения кожи)».'
    )

    error_hint = (chunk.get('error') or '').strip()[:300]
    user = (
        f"ОРИГИНАЛЬНЫЙ ПРОМПТ:\n```\n{(chunk.get('prompt') or '')}\n```\n\n"
        f"ОРИГИНАЛЬНЫЙ CHUNK TEXT:\n```\n{(chunk.get('chunk_text') or '')}\n```\n\n"
        f"ОШИБКА МОДЕРАЦИИ (что не пропустило): {error_hint or '(не указано — обработай оба текста на любые потенциально блокируемые элементы)'}\n\n"
        "Найди и замени блокирующие элементы. Дай список изменений на русском."
    )
    try:
        raw = claude_ask(user, system=sysprompt)
        data = json.loads(strip_json(raw))
    except Exception as e:
        return jsonify({'error': f'heal failed: {e}'}), 500
    return jsonify({
        'prompt':     data.get('prompt') or chunk.get('prompt') or '',
        'chunk_text': data.get('chunk_text') or chunk.get('chunk_text') or '',
        'changes':    data.get('changes') or [],
        'reasoning':  data.get('reasoning') or '',
    })


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/ending_state', methods=['PATCH'])
def seedance_patch_ending(sid, num, idx):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
    if not chunk:
        return jsonify({'error': 'not found'}), 404
    chunk['ending_state'] = ((request.json or {}).get('ending_state') or '')[:400]
    save_episode(sid, num, ep)
    return jsonify({'chunk': chunk})


# ─────────────────────────────────────────────────────────────────────────────
# Timeline editor (one shared timeline per series)
# ─────────────────────────────────────────────────────────────────────────────

def _timeline_file(sid): return series_path(sid) / 'timeline.json'

def _load_timeline(sid):
    f = _timeline_file(sid)
    if not f.exists():
        return {'clips': [], 'updated_at': 0}
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return {'clips': [], 'updated_at': 0}

def _save_timeline(sid, tl):
    tl['updated_at'] = int(time.time())
    _timeline_file(sid).write_text(
        json.dumps(tl, ensure_ascii=False, indent=2), encoding='utf-8'
    )

def _timeline_history_file(sid):
    return series_path(sid) / 'timeline_history.json'

def _timeline_redo_file(sid):
    return series_path(sid) / 'timeline_redo.json'

UNDO_CAP = 30

def _read_stack(f):
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return []

def _write_stack(f, stack):
    f.write_text(json.dumps(stack, ensure_ascii=False), encoding='utf-8')

def _push_undo(sid, label='', clear_redo=True):
    """Snapshot current timeline state into undo stack BEFORE a mutation.
    Any new user-driven mutation invalidates the redo stack (linear history)."""
    cur = _load_timeline(sid)
    hist = _read_stack(_timeline_history_file(sid))
    hist.append({'label': label, 'ts': int(time.time()), 'snapshot': cur})
    if len(hist) > UNDO_CAP:
        hist = hist[-UNDO_CAP:]
    _write_stack(_timeline_history_file(sid), hist)
    if clear_redo:
        _write_stack(_timeline_redo_file(sid), [])

def _pop_undo(sid):
    """Pop the latest undo snapshot. Pushes the *current* state into redo
    stack first, so undo is reversible via redo. Returns the popped item."""
    hist = _read_stack(_timeline_history_file(sid))
    if not hist:
        return None
    cur = _load_timeline(sid)
    redo = _read_stack(_timeline_redo_file(sid))
    redo.append({'label': hist[-1].get('label', ''), 'ts': int(time.time()), 'snapshot': cur})
    if len(redo) > UNDO_CAP:
        redo = redo[-UNDO_CAP:]
    _write_stack(_timeline_redo_file(sid), redo)
    item = hist.pop()
    _write_stack(_timeline_history_file(sid), hist)
    return item

def _pop_redo(sid):
    """Apply the latest redo snapshot. Pushes the current state back into
    undo stack so a redo is itself reversible. Does NOT clear redo stack."""
    redo = _read_stack(_timeline_redo_file(sid))
    if not redo:
        return None
    cur = _load_timeline(sid)
    hist = _read_stack(_timeline_history_file(sid))
    hist.append({'label': redo[-1].get('label', ''), 'ts': int(time.time()), 'snapshot': cur})
    if len(hist) > UNDO_CAP:
        hist = hist[-UNDO_CAP:]
    _write_stack(_timeline_history_file(sid), hist)
    item = redo.pop()
    _write_stack(_timeline_redo_file(sid), redo)
    return item

def _resolve_seedance_chunk(sid, ep_num, idx):
    ep = load_episode(sid, ep_num)
    if not ep:
        return None
    for c in _seedance_chunks(ep):
        if c.get('idx') == idx:
            return c
    return None

@app.route('/api/series/<sid>/timeline')
def timeline_get(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    tl = _load_timeline(sid)
    # enrich clips with current video_path / poster from source episode chunks
    enriched = []
    for c in tl.get('clips', []):
        info = dict(c)
        if c.get('source') == 'seedance':
            ch = _resolve_seedance_chunk(sid, c.get('episode'), c.get('chunk_idx'))
            if ch:
                info['video_path'] = ch.get('video_path') or ''
                info['video_url'] = ch.get('video_url') or ''
                info['orig_duration'] = ch.get('duration') or 0
                info['prompt_preview'] = (ch.get('prompt') or '')[:120]
        enriched.append(info)
    return jsonify({'clips': enriched, 'updated_at': tl.get('updated_at', 0)})

@app.route('/api/series/<sid>/timeline/clips/add', methods=['POST'])
def timeline_add(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    ep_num = int(body.get('episode'))
    chunk_idx = int(body.get('chunk_idx'))
    ch = _resolve_seedance_chunk(sid, ep_num, chunk_idx)
    if not ch:
        return jsonify({'error': 'chunk not found'}), 404
    if not ch.get('video_path'):
        return jsonify({'error': 'chunk has no rendered video yet'}), 400
    _push_undo(sid, 'add clip')
    tl = _load_timeline(sid)
    new_id = f"clip_{int(time.time()*1000)}_{len(tl.get('clips', []))}"
    clip = {
        'id': new_id,
        'source': 'seedance',
        'episode': ep_num,
        'chunk_idx': chunk_idx,
        'in': 0.0,
        'out': float(ch.get('duration') or 0),  # full clip by default
        'added_at': int(time.time()),
    }
    tl.setdefault('clips', []).append(clip)
    _save_timeline(sid, tl)
    return jsonify({'clip': clip, 'count': len(tl['clips'])})

@app.route('/api/series/<sid>/timeline/clips/<clip_id>', methods=['DELETE'])
def timeline_delete(sid, clip_id):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    _push_undo(sid, 'delete clip')
    tl = _load_timeline(sid)
    before = len(tl.get('clips', []))
    tl['clips'] = [c for c in tl.get('clips', []) if c.get('id') != clip_id]
    _save_timeline(sid, tl)
    return jsonify({'removed': before - len(tl['clips'])})

@app.route('/api/series/<sid>/timeline/reorder', methods=['POST'])
def timeline_reorder(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    order = body.get('order') or []  # list of clip ids
    _push_undo(sid, 'reorder')
    tl = _load_timeline(sid)
    by_id = {c['id']: c for c in tl.get('clips', [])}
    new_clips = [by_id[i] for i in order if i in by_id]
    # append any clip not present in order (defensive)
    for c in tl.get('clips', []):
        if c['id'] not in order:
            new_clips.append(c)
    tl['clips'] = new_clips
    _save_timeline(sid, tl)
    return jsonify({'ok': True, 'count': len(new_clips)})

@app.route('/api/series/<sid>/timeline/clips/<clip_id>/crop', methods=['PATCH'])
def timeline_crop(sid, clip_id):
    """Set or clear a crop rectangle on a clip.
    Body: {x, y, w, h} as fractions in [0..1] of the source frame, or
          {clear: true} to remove crop.
    Aspect of (w/h) should match output aspect (we don't enforce — just warn)."""
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    _push_undo(sid, 'crop')
    tl = _load_timeline(sid)
    for c in tl.get('clips', []):
        if c['id'] == clip_id:
            if body.get('clear'):
                c.pop('crop', None)
            else:
                x = max(0.0, min(1.0, float(body.get('x', 0))))
                y = max(0.0, min(1.0, float(body.get('y', 0))))
                w = max(0.05, min(1.0 - x, float(body.get('w', 1))))
                h = max(0.05, min(1.0 - y, float(body.get('h', 1))))
                c['crop'] = {'x': x, 'y': y, 'w': w, 'h': h}
            _save_timeline(sid, tl)
            return jsonify({'clip': c})
    return jsonify({'error': 'clip not found'}), 404

@app.route('/api/series/<sid>/timeline/clips/<clip_id>/trim', methods=['PATCH'])
def timeline_trim(sid, clip_id):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    _push_undo(sid, 'trim')
    tl = _load_timeline(sid)
    for c in tl.get('clips', []):
        if c['id'] == clip_id:
            if 'in' in body:  c['in']  = max(0.0, float(body['in']))
            if 'out' in body: c['out'] = max(0.1, float(body['out']))
            if c['out'] <= c['in']:
                c['out'] = c['in'] + 0.1
            _save_timeline(sid, tl)
            return jsonify({'clip': c})
    return jsonify({'error': 'clip not found'}), 404

@app.route('/api/series/<sid>/timeline/undo', methods=['POST'])
def timeline_undo(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    item = _pop_undo(sid)
    if not item:
        return jsonify({'error': 'nothing to undo'}), 400
    snap = item.get('snapshot') or {'clips': []}
    _save_timeline(sid, snap)
    return jsonify({'restored': item.get('label', ''), 'count': len(snap.get('clips') or [])})

@app.route('/api/series/<sid>/timeline/redo', methods=['POST'])
def timeline_redo(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    item = _pop_redo(sid)
    if not item:
        return jsonify({'error': 'nothing to redo'}), 400
    snap = item.get('snapshot') or {'clips': []}
    _save_timeline(sid, snap)
    return jsonify({'restored': item.get('label', ''), 'count': len(snap.get('clips') or [])})

@app.route('/api/series/<sid>/timeline/history')
def timeline_history(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    hist = _read_stack(_timeline_history_file(sid))
    redo = _read_stack(_timeline_redo_file(sid))
    last = hist[-1] if hist else None
    next_ = redo[-1] if redo else None
    return jsonify({
        'depth': len(hist),
        'redo_depth': len(redo),
        'last': {'label': last.get('label'), 'ts': last.get('ts')} if last else None,
        'next': {'label': next_.get('label'), 'ts': next_.get('ts')} if next_ else None,
    })

@app.route('/api/series/<sid>/timeline/clips/<clip_id>/split', methods=['POST'])
def timeline_split(sid, clip_id):
    """Split a clip at local time `at` (seconds from clip start).
    `at` is the playhead position relative to the clip's IN point — i.e. the
    moment in the *trimmed* clip where the user wants to cut.
    Produces two adjacent clips A=[in..in+at], B=[in+at..out]."""
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    at = float(body.get('at') or 0)
    _push_undo(sid, 'split')
    tl = _load_timeline(sid)
    clips = tl.get('clips') or []
    for i, c in enumerate(clips):
        if c.get('id') != clip_id:
            continue
        cin  = float(c.get('in', 0))
        cout = float(c.get('out', 0))
        cut_abs = cin + at
        # Need at least 0.1s on both sides
        if cut_abs <= cin + 0.05 or cut_abs >= cout - 0.05:
            return jsonify({'error': 'too close to edge — нечего резать'}), 400
        a = dict(c)
        b = dict(c)
        a['out'] = cut_abs
        b['id'] = f"clip_{int(time.time()*1000)}_{i}b"
        b['in'] = cut_abs
        b['added_at'] = int(time.time())
        clips[i] = a
        clips.insert(i + 1, b)
        _save_timeline(sid, tl)
        return jsonify({'a': a, 'b': b})
    return jsonify({'error': 'clip not found'}), 404

@app.route('/api/series/<sid>/timeline/render', methods=['POST'])
def timeline_render(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    tl = _load_timeline(sid)
    clips = tl.get('clips') or []
    if not clips:
        return jsonify({'error': 'timeline пустой'}), 400

    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return jsonify({'error': 'ffmpeg не установлен. brew install ffmpeg'}), 500

    # Resolve each clip to an absolute video path + check trims/crops
    seg_paths = []
    needs_trim = False
    needs_crop = False
    for c in clips:
        if c.get('source') != 'seedance':
            continue
        ch = _resolve_seedance_chunk(sid, c.get('episode'), c.get('chunk_idx'))
        if not ch or not ch.get('video_path'):
            return jsonify({'error': f"clip {c['id']} без видео"}), 400
        abs_path = series_path(sid) / ch['video_path']
        if not abs_path.exists():
            return jsonify({'error': f"file missing: {ch['video_path']}"}), 400
        cin  = float(c.get('in', 0))
        cout = float(c.get('out', ch.get('duration') or 0))
        full_dur = float(ch.get('duration') or 0)
        if cin > 0.05 or (full_dur and abs(cout - full_dur) > 0.05):
            needs_trim = True
        crop = c.get('crop')
        if crop and (crop.get('w', 1) < 0.999 or crop.get('h', 1) < 0.999
                     or crop.get('x', 0) > 0.001 or crop.get('y', 0) > 0.001):
            needs_crop = True
        seg_paths.append({'path': str(abs_path), 'in': cin, 'out': cout, 'crop': crop})

    renders_dir = series_path(sid) / 'renders'
    renders_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    out_path = renders_dir / f'timeline_{ts}.mp4'

    # Determine output dimensions: probe first clip and round down to even
    out_w, out_h = 720, 1280  # fallback
    try:
        ffprobe_bin = shutil.which('ffprobe') or (ffmpeg_bin.replace('ffmpeg', 'ffprobe'))
        if seg_paths and ffprobe_bin:
            pr = subprocess.run([
                ffprobe_bin, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=s=x:p=0', seg_paths[0]['path']
            ], capture_output=True, text=True, timeout=10)
            wh = (pr.stdout or '').strip().split('x')
            if len(wh) == 2:
                out_w = int(wh[0]) - (int(wh[0]) % 2)
                out_h = int(wh[1]) - (int(wh[1]) % 2)
    except Exception:
        pass

    if not needs_trim and not needs_crop:
        # fast concat-demuxer, no re-encode
        list_file = renders_dir / f'_concat_{ts}.txt'
        list_file.write_text(
            '\n'.join(f"file '{seg['path']}'" for seg in seg_paths),
            encoding='utf-8',
        )
        cmd = [
            ffmpeg_bin, '-y', '-f', 'concat', '-safe', '0',
            '-i', str(list_file), '-c', 'copy', str(out_path),
        ]
    else:
        # filter_complex per segment: optional trim → optional crop → scale to common size
        inputs = []
        filt = []
        for i, seg in enumerate(seg_paths):
            inputs += ['-i', seg['path']]
            crop = seg.get('crop')
            # Build video filter chain
            v_steps = [f"trim={seg['in']}:{seg['out']}", "setpts=PTS-STARTPTS"]
            if crop and (crop.get('w', 1) < 0.999 or crop.get('h', 1) < 0.999
                         or crop.get('x', 0) > 0.001 or crop.get('y', 0) > 0.001):
                cw = crop.get('w', 1); ch_ = crop.get('h', 1)
                cx = crop.get('x', 0); cy = crop.get('y', 0)
                v_steps.append(
                    f"crop=trunc(iw*{cw}/2)*2:trunc(ih*{ch_}/2)*2:"
                    f"trunc(iw*{cx}/2)*2:trunc(ih*{cy}/2)*2"
                )
            v_steps.append(
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease"
            )
            v_steps.append(f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black")
            v_steps.append("setsar=1")
            filt.append(f"[{i}:v]{','.join(v_steps)}[v{i}]")
            filt.append(
                f"[{i}:a]atrim={seg['in']}:{seg['out']},asetpts=PTS-STARTPTS[a{i}]"
            )
        n = len(seg_paths)
        concat_inputs = ''.join(f"[v{i}][a{i}]" for i in range(n))
        filt.append(f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]")
        cmd = [ffmpeg_bin, '-y', *inputs, '-filter_complex', ';'.join(filt),
               '-map', '[v]', '-map', '[a]',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
               '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '128k', str(out_path)]

    # Acquire the global render slot. If both slots are busy this blocks until
    # one frees, naturally queueing concurrent renders.
    queue_wait_start = time.time()
    with RENDER_SEMAPHORE:
        queue_waited = time.time() - queue_wait_start
        if queue_waited > 0.5:
            print(f'[render] {sid} waited {queue_waited:.1f}s in queue')
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'ffmpeg timeout (>10 min)'}), 500
        if proc.returncode != 0:
            return jsonify({
                'error': 'ffmpeg failed',
                'stderr': proc.stderr[-2000:],
                'cmd': ' '.join(cmd[:8]) + ' ...',
            }), 500
    try:
        if not needs_trim and not needs_crop:
            list_file.unlink(missing_ok=True)
    except Exception:
        pass
    rel = out_path.relative_to(series_path(sid))
    size_mb = round(out_path.stat().st_size / 1024 / 1024, 2)
    mode = 'concat-copy' if (not needs_trim and not needs_crop) else 'filter-complex'
    return jsonify({
        'path': str(rel),
        'url': f'/assets/{sid}/{rel}',
        'size_mb': size_mb,
        'mode': mode,
        'clips': len(seg_paths),
        'out_dims': f'{out_w}x{out_h}',
    })

@app.route('/api/series/<sid>/timeline/renders')
def timeline_renders(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    rd = series_path(sid) / 'renders'
    if not rd.exists():
        return jsonify({'renders': []})
    items = []
    for p in sorted(rd.glob('timeline_*.mp4'), reverse=True):
        items.append({
            'name': p.name,
            'url': f'/assets/{sid}/renders/{p.name}',
            'size_mb': round(p.stat().st_size / 1024 / 1024, 2),
            'created_at': int(p.stat().st_mtime),
        })
    return jsonify({'renders': items})

@app.route('/api/series/<sid>/timeline/renders/<name>', methods=['DELETE'])
def timeline_render_delete(sid, name):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    if not name.startswith('timeline_') or not name.endswith('.mp4'):
        return jsonify({'error': 'bad name'}), 400
    p = series_path(sid) / 'renders' / name
    if p.exists():
        p.unlink()
    return jsonify({'ok': True})


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
    _recover_inflight_chunks()
    _start_log_cleanup_loop()
