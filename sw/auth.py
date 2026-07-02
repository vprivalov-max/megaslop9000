"""Auth: Google OAuth gate, current-user identity, per-user API keys and
thread-local key propagation for background workers."""
import datetime
import os
import secrets
import threading

from flask import session

try:
    from authlib.integrations.flask_client import OAuth
    _AUTHLIB_AVAILABLE = True
except ImportError:
    _AUTHLIB_AVAILABLE = False

from sw.config import (BASE, PRIMARY_USER_EMAIL, AVAI_KEY, RETELLER_KEY,
                       _load_user_keys)
from sw.core import app

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
