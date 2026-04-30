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

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

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
    """Returns the logged-in user's email, or None if not authenticated."""
    if not AUTH_ENABLED:
        return DEV_USER_EMAIL
    return (session.get('user') or {}).get('email')

def _is_path_allowed(path):
    """Paths that bypass the auth gate."""
    if path.startswith('/static/'):
        return True
    return path in ('/login', '/auth/google', '/auth/google/callback', '/auth/logout', '/healthz')

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
        return jsonify({'email': None}), 401
    info = (session.get('user') or {}) if AUTH_ENABLED else {'email': email, 'name': 'Local Dev'}
    return jsonify({
        'email': email,
        'name': info.get('name') or email.split('@')[0],
        'picture': info.get('picture') or '',
        'auth_enabled': AUTH_ENABLED,
        'domain': AUTH_ALLOWED_DOMAIN,
    })

@app.route('/healthz')
def healthz():
    return {'ok': True, 'auth': AUTH_ENABLED}


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
    `timeout`/`idle_timeout` are accepted but ignored (SDK handles its own timeouts)."""
    sdk_model = _MODEL_ALIAS.get(model, model) if model else 'claude-sonnet-4-5'
    prompt_kb = len((system + prompt).encode('utf-8')) / 1024
    t0 = time.time()
    print(f'[claude_ask] {prompt_kb:.1f}KB → {sdk_model}', flush=True)
    client = _get_anthropic_client()
    kwargs = {'model': sdk_model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': prompt}]}
    if system:
        kwargs['system'] = system
    msg = client.messages.create(**kwargs)
    text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
    out_kb = len(text.encode('utf-8')) / 1024
    print(f'[claude_ask] done in {time.time()-t0:.1f}s ({prompt_kb:.1f}KB→{out_kb:.1f}KB, stop={msg.stop_reason})', flush=True)
    return text


def anthropic_ask(prompt: str, system: str = '', model: str = 'claude-haiku-4-5') -> str:
    """Direct Anthropic SDK call. Alias of claude_ask."""
    return claude_ask(prompt, system=system, model=model, max_tokens=4096)

def claude_ask_fast(prompt: str, system: str = '') -> str:
    """Haiku — quick tasks (extraction, classification)."""
    return claude_ask(prompt, system=system, model='haiku', max_tokens=4096)

def claude_ask_quality(prompt: str, system: str = '') -> str:
    """Sonnet — creative tasks (scripts, synopses, ideas)."""
    return claude_ask(prompt, system=system, model='sonnet', max_tokens=8192)

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

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

def rtl_headers():
    return {'Authorization': f'Bearer {RETELLER_KEY}'}

def _avai_call(provider: str, prompt: str, reference_url: str = None, aspect_ratio: str = '9:16') -> str:
    """One AVAI call with the given provider. Returns image URL or raises."""
    payload = {
        'provider': provider,
        'prompt': prompt,
        'num_images': 1,
        'aspect_ratio': aspect_ratio,
        'image_size': '2K',
        'output_format': 'png',
    }
    if provider == 'banana':
        payload['model'] = 'pro'
    if reference_url:
        payload['contextImages'] = [{'url': reference_url}]
    headers = {'x-api-key': AVAI_KEY, 'content-type': 'application/json'}
    resp = requests.post(AVAI_API, json=payload, headers=headers, timeout=180)
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


def avai_generate(prompt: str, output_path: Path, reference_url: str = None, aspect_ratio: str = '9:16') -> str:
    """Generate via AVAI. Tries Banana (Gemini Image Pro) first; on failure
    falls back to Seedream (NOT Seedance — Seedance is video, we need an image)
    with the same prompt + reference. Returns the remote image URL on success,
    raises with a combined error message on total failure.
    aspect_ratio: '9:16' (vertical, default — characters/portraits) or '16:9' (horizontal — locations)."""
    errors = []
    image_url = None
    for provider in ('banana', 'seedream'):
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
        raise RuntimeError(
            'Оба провайдера AVAI отказали. '
            + ' | '.join(errors)
            + ' — попробуй переписать описание более нейтрально '
              '(без слов lingerie / bare chest / boxers).'
        )
    # Download and save
    img_resp = requests.get(image_url, timeout=60)
    img_resp.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_resp.content)
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

def load_series(sid):
    f = series_file(sid)
    if not f.exists():
        return None
    data = json.loads(f.read_text())
    # Forward-compat defaults so old series.json don't break new features.
    data.setdefault('video_provider', 'reteller')        # 'reteller' | 'seedance'
    data.setdefault('auto_reteller_prompt', True)        # auto-build Reteller prompt after script gen
    return data

def save_series(sid, data):
    series_path(sid).mkdir(exist_ok=True)
    series_file(sid).write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_episode(sid, num):
    f = episodes_dir(sid) / f'{int(num):03d}.json'
    return json.loads(f.read_text()) if f.exists() else None

def save_episode(sid, num, data):
    episodes_dir(sid).mkdir(exist_ok=True)
    (episodes_dir(sid) / f'{int(num):03d}.json').write_text(
        json.dumps(data, indent=2, ensure_ascii=False)
    )

def list_episodes(sid):
    d = episodes_dir(sid)
    if not d.exists():
        return []
    return [json.loads(f.read_text(encoding='utf-8')) for f in sorted(d.glob('*.json')) if not f.name.startswith('._')]


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
    return render_template('index.html')

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
    if request.method == 'POST':
        save_config(request.json)
        return jsonify({'ok': True})
    return jsonify(load_config())


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
        'characters': [],
        'locations': [],
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
            'image_size':           '2K'
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
    if healed:
        save_series(sid, s)
    # Heal episode refs against current series state (idempotent, cheap).
    # Catches the failure mode where chars/outfits in scripts aren't reflected in series.json.
    try:
        for ep in list_episodes(sid):
            if (ep.get('script') or '').strip():
                sync_episode_with_cast_block(sid, ep['number'])
    except Exception as e:
        print(f'[get_series {sid}] heal failed: {e}')
    # Re-load post-heal so client gets the fresh state
    s = load_series(sid)
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
    s.update(data)
    save_series(sid, s)
    return jsonify(s)

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
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400

    loc_dir = assets_dir(sid) / 'locations' / loc_id
    loc_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    stem = Path(filename).stem
    ext = Path(filename).suffix
    final = loc_dir / filename
    counter = 1
    while final.exists():
        final = loc_dir / f'{stem}_{counter}{ext}'
        counter += 1

    file.save(final)
    rel_path = str(final.relative_to(series_path(sid)))
    for loc in s.get('locations', []):
        if loc['id'] == loc_id:
            loc.setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}'})

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
            f'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose, arms at sides. '
            f'Uniform solid gray background, #808080. No shadows on background. '
            f'Studio lighting, soft and even. Photorealistic, cinematic quality.'
        )
    else:
        # No reference — generate from scratch with description
        prompt = (
            f'Full body portrait of {char["name"]}, a {gender}. '
            f'{char.get("appearance", "")}. '
            f'Wearing: {outfit["label"]}. {outfit.get("description", "")}. '
            f'Standing facing camera, slight 3/4 angle. Neutral pose, arms at sides. '
            f'Uniform solid gray background, #808080. No shadows on background. '
            f'Studio lighting, soft and even. Photorealistic, cinematic quality.'
        )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    char_slug = slugify(char['name'])
    out_dir = assets_dir(sid) / 'characters' / char_slug / 'outfits'
    out_path = out_dir / f'{asset_name(char["name"], outfit["label"])}.png'

    try:
        image_url = avai_generate(prompt, out_path, reference_url=reference_url)
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
    prompt = (
        f"Full body portrait of {char['name']}, a {gender}. "
        f"{char.get('appearance', '')}. {char.get('description', '')}. "
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose, arms at sides. "
        f"Uniform solid gray background, #808080. No shadows or reflections on background. "
        f"Studio lighting, soft and even, no harsh shadows on face or body. "
        f"Photorealistic, cinematic quality, high detail on face and clothing."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    out_path = char_dir / f'{asset_name(char["name"], "BASE")}.png'

    try:
        image_url = avai_generate(prompt, out_path)
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = char.setdefault('ref_images', [])
        # Replace or prepend
        refs[:] = [r for r in refs if not r.endswith(out_path.name)]
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

    # Persist constraints onto the character so future inline generations
    # also respect them. Empty wishes => clear them.
    char['image_constraints'] = wishes

    gender = 'woman' if char.get('gender') == 'female' else 'man'
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    prompt = (
        f"Full body portrait of {char['name']}, a {gender}. "
        f"{char.get('appearance', '')}. {char.get('description', '')}.{constraints_clause} "
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose, arms at sides. "
        f"Uniform solid gray background, #808080. No shadows or reflections on background. "
        f"Studio lighting, soft and even, no harsh shadows on face or body. "
        f"Photorealistic, cinematic quality, high detail on face and clothing."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    out_path = char_dir / f'{asset_name(char["name"], "BASE")}.png'

    # 1) Regenerate base
    try:
        # Drop the old file so AVAI doesn't accidentally serve it cached.
        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass
        image_url = avai_generate(prompt, out_path)
    except Exception as e:
        return jsonify({'error': f'Не удалось сгенерировать основной образ: {e}'}), 500

    rel_path = str(out_path.relative_to(series_path(sid)))
    refs = char.setdefault('ref_images', [])
    # Replace any prior copy of this filename, then put fresh one first
    refs[:] = [r for r in refs if not r.endswith(out_path.name)]
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
                    f'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose, arms at sides. '
                    f'Uniform solid gray background, #808080. No shadows on background. '
                    f'Studio lighting, soft and even. Photorealistic, cinematic quality.'
                )
                ref_prompt = re.sub(r'\s+', ' ', ref_prompt).strip()
                outfit_dir = char_dir / 'outfits'
                outfit_dir.mkdir(parents=True, exist_ok=True)
                outfit_out = outfit_dir / f'{asset_name(char["name"], outfit["label"])}.png'
                # Remove old generated photo before regen so we don't leave orphans
                if outfit.get('photo'):
                    old_full = series_path(sid) / outfit['photo']
                    if old_full.exists() and old_full != outfit_out:
                        try: old_full.unlink()
                        except Exception: pass
                if outfit_out.exists():
                    try: outfit_out.unlink()
                    except Exception: pass
                outfit_url = avai_generate(ref_prompt, outfit_out, reference_url=new_base_url)
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
    filename = f'{asset_name(char["name"], "BASE")}.{ext}'  # e.g. CLAIRE_BASE.jpg
    (char_dir / filename).write_bytes(f.read())
    rel_path = f'assets/characters/{char_slug}/{filename}'
    refs = char.setdefault('ref_images', [])
    if rel_path not in refs:
        refs.insert(0, rel_path)
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'series': s})


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
    filename = f'{asset_name(loc["name"])}.{ext}'  # e.g. THE_NETWORKING_EVENT_VENUE.jpg
    (loc_dir / filename).write_bytes(f.read())
    rel_path = f'assets/locations/{loc_slug}/{filename}'
    refs = loc.setdefault('ref_images', [])
    if rel_path not in refs:
        refs.insert(0, rel_path)
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'series': s})


# ── Open folder in Finder ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/open-folder', methods=['POST'])
def open_folder(sid):
    folder_type = (request.json or {}).get('type', 'assets')
    base = assets_dir(sid)
    paths = {
        'characters': base / 'characters',
        'locations':  base / 'locations',
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
    prompt = (
        f"{loc['name']}. {loc.get('description', '')}. "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
        f"Photorealistic, cinematic quality, high detail. Atmospheric lighting."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    out_path = loc_dir / f'{asset_name(loc["name"])}.png'

    try:
        image_url = avai_generate(prompt, out_path, aspect_ratio='16:9')
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = loc.setdefault('ref_images', [])
        refs[:] = [r for r in refs if not r.endswith(out_path.name)]
        refs.insert(0, rel_path)
        loc['avai_url'] = image_url  # used by Seedance for video refs
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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


# ── Auto-generate missing assets (background sweep) ──────────────────────────

import threading

# Per-series lock so a sweep doesn't run twice in parallel for the same series
_AUTOGEN_LOCKS = {}
_AUTOGEN_STATUS = {}  # sid -> {'running': bool, 'queue': int, 'done': int, 'errors': []}

def _autogen_status(sid):
    return _AUTOGEN_STATUS.setdefault(sid, {'running': False, 'queue': 0, 'done': 0, 'errors': []})

def _gen_char_base_inline(s, sid, char):
    """Generate base ref for character. Mutates s, saves at end."""
    if char.get('ref_images'):
        return
    gender = 'woman' if char.get('gender') == 'female' else 'man'
    constraints = (char.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    prompt = (
        f"Full body portrait of {char['name']}, a {gender}. "
        f"{char.get('appearance', '')}. {char.get('description', '')}.{constraints_clause} "
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose, arms at sides. "
        f"Uniform solid gray background, #808080. No shadows or reflections on background. "
        f"Studio lighting, soft and even, no harsh shadows on face or body. "
        f"Photorealistic, cinematic quality, high detail on face and clothing."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / f'{asset_name(char["name"], "BASE")}.png'
    image_url = avai_generate(prompt, out_path)
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
    gender = 'woman' if char.get('gender') == 'female' else 'man'
    constraints = (char.get('image_constraints') or '').strip()
    constraints_clause = f' IMPORTANT — strictly follow these constraints: {constraints}. ' if constraints else ''
    prompt = (
        (f'Same {gender} as the reference image. ' if reference_url else f'Full body portrait of {char["name"]}, a {gender}. {char.get("appearance","")}. ')
        + f'Now wearing: {outfit["label"]}. {outfit.get("description", "")}. '
        + ('Same face, same hair, same body — only the clothing changes. ' if reference_url else '')
        + constraints_clause
        + 'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose, arms at sides. '
          'Uniform solid gray background, #808080. No shadows on background. '
          'Studio lighting, soft and even. Photorealistic, cinematic quality.'
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / 'outfits' / f'{asset_name(char["name"], outfit["label"])}.png'
    image_url = avai_generate(prompt, out_path, reference_url=reference_url)
    outfit['photo'] = str(out_path.relative_to(series_path(sid)))
    outfit['avai_url'] = image_url

def _gen_loc_inline(s, sid, loc):
    if loc.get('ref_images'):
        return
    tone = s.get('tone', '')
    prompt = (
        f"{loc['name']}. {loc.get('description', '')}. "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
        f"Photorealistic, cinematic quality, high detail. Atmospheric lighting."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    loc_slug = slugify(loc['name'])
    out_path = assets_dir(sid) / 'locations' / loc_slug / f'{asset_name(loc["name"])}.png'
    image_url = avai_generate(prompt, out_path, aspect_ratio='16:9')
    loc.setdefault('ref_images', []).insert(0, str(out_path.relative_to(series_path(sid))))


def auto_generate_missing_assets(sid):
    """Background sweep: generate base photos for chars without refs,
    outfit photos for outfits without photos, location refs for locations without refs.
    Order: char-bases first (so outfit i2i has a reference), then outfits, then locations.
    Idempotent — skips anything already generated.
    Also pre-syncs every episode's cast block so chars/outfits referenced in scripts
    but missing from series.json get created before the sweep runs."""
    lock = _AUTOGEN_LOCKS.setdefault(sid, threading.Lock())
    if not lock.acquire(blocking=False):
        print(f'[autogen {sid}] already running, skipping')
        return
    st = _autogen_status(sid)
    st.update({'running': True, 'queue': 0, 'done': 0, 'errors': []})
    try:
        # ─── Pre-sweep: ensure every episode's cast block is reflected in series.json
        for ep in list_episodes(sid):
            if (ep.get('script') or '').strip():
                try:
                    sync_episode_with_cast_block(sid, ep['number'])
                except Exception as e:
                    print(f'[autogen {sid}] sync ep{ep["number"]} failed: {e}')

        s = load_series(sid)
        if not s: return
        # Build task list
        tasks = []
        for c in s.get('characters', []):
            if not c.get('ref_images'):
                tasks.append(('char', c['id'], None))
        for c in s.get('characters', []):
            for o in c.get('outfits', []):
                if not o.get('photo') and not o.get('is_base'):
                    tasks.append(('outfit', c['id'], o['id']))
        for l in s.get('locations', []):
            if not l.get('ref_images'):
                tasks.append(('loc', l['id'], None))
        st['queue'] = len(tasks)
        print(f'[autogen {sid}] {len(tasks)} assets to generate')
        for kind, parent_id, child_id in tasks:
            try:
                # Re-load each iteration so we don't clobber concurrent edits
                s = load_series(sid)
                if not s: break
                if kind == 'char':
                    char = next((c for c in s['characters'] if c['id'] == parent_id), None)
                    if char and not char.get('ref_images'):
                        _gen_char_base_inline(s, sid, char)
                        save_series(sid, s)
                elif kind == 'outfit':
                    char = next((c for c in s['characters'] if c['id'] == parent_id), None)
                    if char:
                        outfit = next((o for o in char.get('outfits', []) if o['id'] == child_id), None)
                        if outfit and not outfit.get('photo'):
                            _gen_outfit_inline(s, sid, char, outfit)
                            save_series(sid, s)
                elif kind == 'loc':
                    loc = next((l for l in s.get('locations', []) if l['id'] == parent_id), None)
                    if loc and not loc.get('ref_images'):
                        _gen_loc_inline(s, sid, loc)
                        save_series(sid, s)
                st['done'] += 1
            except Exception as e:
                err_msg = f'{kind}/{parent_id}: {str(e)[:200]}'
                st['errors'].append(err_msg)
                print(f'[autogen {sid}] FAILED {err_msg}')
    finally:
        st['running'] = False
        lock.release()
        print(f'[autogen {sid}] done — {st["done"]}/{st["queue"]} ok, {len(st["errors"])} errors')
        # Re-check: if new chars/outfits/locs appeared during the sweep (e.g. a parallel
        # extract-characters or sync_episode_with_cast_block added rows mid-flight), the
        # current run has already left them un-generated. Kick off another sweep so the
        # user doesn't have to click "regenerate" manually.
        try:
            s2 = load_series(sid)
            if s2 and s2.get('auto_generate_assets'):
                pending = (
                    sum(1 for c in s2.get('characters', []) if not c.get('ref_images')) +
                    sum(1 for c in s2.get('characters', []) for o in c.get('outfits', [])
                        if not o.get('photo') and not o.get('is_base')) +
                    sum(1 for l in s2.get('locations', []) if not l.get('ref_images'))
                )
                if pending > 0:
                    print(f'[autogen {sid}] {pending} new assets queued during sweep — re-running')
                    threading.Thread(target=auto_generate_missing_assets, args=(sid,), daemon=True).start()
        except Exception as e:
            print(f'[autogen {sid}] re-check failed: {e}')


def trigger_autogen_if_enabled(sid):
    """Public entry point: kick off background sweep if the series has auto_generate_assets on."""
    s = load_series(sid)
    if not s or not s.get('auto_generate_assets'):
        return False
    t = threading.Thread(target=auto_generate_missing_assets, args=(sid,), daemon=True)
    t.start()
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
    return jsonify(_autogen_status(sid))


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
    t = threading.Thread(target=auto_generate_missing_assets, args=(sid,), daemon=True)
    t.start()
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

Title templates that work (use these structures):
  • "Never [Verb] a [Hidden Identity]" → "Never Divorce a Secret Billionaire Heiress"
  • "My [Dismissible Role] Is Actually a [Shocking Reality]" → "My Poor Husband Is a Billionaire"
  • "[Verb]-ing My [Forbidden Person]" → "Craving My Brother's Best Friend"
  • "[Shocking Premise in One Line]" → "I Got Pregnant at My Ex's Wedding"
  • "[Contract/Flash/Fake] [Marriage/Dating] with [Twist]" → "Flash Marriage with My Bodyguard Boss"
  • "[Exclamation about situation]" → "Oh No, I Married the Mafia King!"
  • "[Character] [Does Extreme Thing]" → "I Went to the Mafia Boss for a Baby"
  • "Pregnant by [Powerful/Forbidden Person]" → "Pregnant by My Billionaire Stepbrother"

Power nouns to use freely: Billionaire, CEO, Mafia Boss, Alpha, King, Heiress, Ruthless, Secret, Forbidden, Contract, Rejected, Rival, Stepbrother, Boss
Relationship modifiers: My husband, my ex, my boss, my stepbrother, my brother's best friend, my enemy

FORBIDDEN title styles: metaphorical ("Shadows of Yesterday"), vague ("The Choices We Make"), literary ("When Light Finds Darkness"), anything that sounds like an indie film or a book club pick.

SYNOPSIS RULES:
- 3 sentences max. Every word is plot, zero atmosphere-setting.
- Sentence 1: The injustice/humiliation done to the protagonist OR the shocking inciting situation
- Sentence 2: The forced entanglement / power imbalance / secret collision
- Sentence 3: The twist that reframes everything or the impossible choice she faces
- Tone: punchy, present-tense energy, zero hedging. Sound like a trailer voiceover.
- synopsis_ru: same energy in Russian — 2-3 предложения, как будто рассказываешь подруге что только что посмотрела

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
    'corporate boardroom', 'small coastal town', 'luxury hotel', 'underground music scene',
    'high-end fashion house', 'elite university', 'family vineyard', 'tech startup',
    'hospital ER', 'old money estate', 'art gallery', 'professional sports team',
    'law firm', 'private island resort', 'foreign city expat community', 'royal court',
    'film set', 'military base', 'crime family', 'political campaign',
]
_IDEA_TWISTS = [
    'secret identity', 'forbidden love', 'revenge plot', 'hidden past',
    'class war', 'betrayal within family', 'blackmail', 'second chance romance',
    'deadly competition', 'love triangle', 'corporate sabotage', 'fake relationship',
    'long-lost sibling', 'obsessive ex', 'hidden heir', 'dangerous obsession',
    'secret pregnancy', 'hidden billionaire status', 'mistaken identity humiliation',
    'mafia protection deal', 'company inheritance shock', 'paternity reveal',
    'the rescuer has an agenda', 'fall from grace and rebuild', 'murder attempt survived',
    'the ally is the real villain', 'hidden recording surfaces', 'DNA test twist',
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
]

@app.route('/api/generate-series-ideas', methods=['POST'])
def generate_series_ideas():
    data_in = request.json or {}
    genres = data_in.get('genres') or []

    if genres:
        genre_rule = (
            f"STRICT GENRE REQUIREMENT: Every single one of the 5 ideas MUST incorporate ALL of these genres: {', '.join(genres)}.\n"
            f"This is not optional. Each idea must feel like a genuine mix of {' + '.join(genres)}.\n"
            "If an idea does not fit ALL selected genres, replace it — do not submit it.\n\n"
        )
    else:
        genre_rule = ""

    seed_settings = random.sample(_IDEA_SETTINGS, 5)
    seed_twists   = random.sample(_IDEA_TWISTS, 5)
    seed_tones    = random.sample(_IDEA_TONES, 5)
    constraints   = '\n'.join(
        f'{i+1}. Setting: {seed_settings[i]} | Twist: {seed_twists[i]} | Mood: {seed_tones[i][0]}'
        for i in range(5)
    )
    prompt = (
        "Generate exactly 5 SHORT DRAMA series concepts for TikTok/Reels.\n\n"
        + genre_rule
        + "Use these creative constraints (one per idea) to ensure variety:\n"
        f"{constraints}\n\n"
        "Rules:\n"
        "- All titles and English fields must be in English\n"
        "- synopsis_ru must be in Russian — short (2-3 sentences), vivid, makes you want to watch\n"
        "- No generic titles. No predictable plots. Surprise me.\n\n"
        f"Return JSON matching this schema:\n{_IDEAS_SCHEMA}"
    )
    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data.get('ideas', data))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


_FROM_IDEA_ANGLES = [
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
]

@app.route('/api/generate-series-from-idea', methods=['POST'])
def generate_series_from_idea():
    data_in = request.json or {}
    idea   = data_in.get('idea', '').strip()
    genres = data_in.get('genres') or []
    if not idea:
        return jsonify({'error': 'Опиши идею'}), 400
    angle   = random.choice(_FROM_IDEA_ANGLES)
    setting = random.choice(_IDEA_SETTINGS)
    twist   = random.choice(_IDEA_TWISTS)
    genre_rule = (
        f"The series MUST be a blend of these genres: {', '.join(genres)}. "
        "Every element of the concept should feel like it belongs in all of them simultaneously. "
    ) if genres else ""
    prompt = (
        f"Based on this idea: \"{idea}\"\n\n"
        + genre_rule
        + f"Creative angle to explore: {angle}\n"
        f"Consider incorporating: setting={setting}, twist element={twist}\n\n"
        "Create a UNIQUE short drama series concept for TikTok/Reels that feels fresh and specific — "
        "avoid generic plots. Give it a surprising title. "
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
        data = json.loads(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
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
        data = json.loads(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
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
CHARACTER: [name] | OUTFIT: [outfit_label] | OUTFIT_DESC: [garments + colors, what they're wearing this scene]
=== END CAST ===

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
    if added_chars or added_locs:
        trigger_autogen_if_enabled(sid)
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
        'YOUR JOB — return five lists in JSON:\n\n'
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
        '   explicit director\'s notes. The old portrait will be discarded and regenerated from this new text.\n\n'
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
        '  "appearance_updates": [{"id":"existingId","appearance":"...","reason":"..."}]\n'
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
    save_episode(sid, num, ep)

    if added_chars or added_locs or appearance_updates:
        trigger_autogen_if_enabled(sid)

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
                f'Pick up from the previous chunk\'s cliffhanger. 4–6 sentences outlining: (1) chunk hook, (2) all {bs} mini-cliffhangers in order, (3) chunk midpoint reversal, (4) chunk-end cliffhanger.\n'
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
                f'Continue naturally from where episode {prev_ep["number"]} left off — pick up from the cliffhanger. '
                f'Include: an immediate-stakes opening (no warm-up), a mid-episode reversal, and end on a new cliffhanger. '
                f'TIMELINE — also output `days_since_previous` (integer): in-world days between Ep {prev_ep["number"]} and Ep {next_num}. '
                f'Use real-world biology (pregnancy test 10+ days post conception, undercover ops 30+ days setup). 0 = same-day continuation. '
                'Return JSON: {"synopsis": "...", "days_since_previous": int}'
            )

    try:
        data = json.loads(strip_json(claude_ask(prompt, system=_WRITER_SYSTEM)))
        syn = data.get('synopsis', '')
        dsp = data.get('days_since_previous')
        # Persist the synopsis + timeline metadata onto the episode (create if missing)
        ep = load_episode(sid, next_num)
        if ep is None:
            ep = {
                'number': next_num, 'title': f'Эпизод {next_num}', 'synopsis': syn,
                'script': '', 'characters_used': [], 'character_outfits': {},
                'days_since_previous': int(dsp) if dsp is not None else (0 if next_num == 1 else 1),
                'notes': '', 'status': 'draft',
                'reteller': {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None},
            }
        else:
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
        prev_block = (
            f'{prev_label} SCRIPT (FULL — read it all):\n{prev_script}\n\n'
            f'═══ ENDING STATE OF {prev_label} — read this carefully ═══\n'
            f'These are the LAST beats of the previous unit. If they show a scene still in motion '
            f'(someone just arrived, a question hangs in the air, two characters mid-confrontation, '
            f'a reaction shot is the cliffhanger) — THIS unit MUST open in the SAME scene, SAME '
            f'location, SAME characters present, continuing dialogue from the very next beat. '
            f'Do NOT teleport to a new room or "later that day".\n'
            f'{tail}\n'
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

        # ─── Reconcile cast block → series chars/outfits → episode refs.
        # This is idempotent: re-parses from a fresh series load, persists chars/outfits,
        # rewrites episode IDs to point to valid series entries, drops stale refs.
        sync_result = sync_episode_with_cast_block(sid, num)
        print(f'[script-gen {sid}/ep{num}] sync: created_chars={sync_result.get("created_characters")} '
              f'created_outfits={sync_result.get("created_outfits")} '
              f'created_locations={sync_result.get("created_locations")} '
              f'detected_locations={sync_result.get("detected_locations")} '
              f'healed={sync_result.get("healed_refs")}')

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
    heading_re = re.compile(
        r'^\s*(?:INT\.|EXT\.|ИНТА?\.|ЭКС?\.|INT/EXT\.|EXT/INT\.)\s*([^—\-\n]+?)\s*[—\-]',
        re.IGNORECASE | re.MULTILINE,
    )
    for m in heading_re.finditer(ep.get('script') or ''):
        nm = m.group(1).strip().strip('"').strip("'")
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

        # Auto-create unknown character
        if not char:
            gender = fields.get('GENDER', '').lower()
            if gender not in ('male', 'female'):
                gender = 'female' if any(t in raw_name.lower() for t in ['claire','elena','victoria','anna','maria','lina']) else 'male'
            look = fields.get('LOOK', '') or fields.get('APPEARANCE', '')
            # Fallback: if writer omitted LOOK, synthesize a baseline appearance from OUTFIT_DESC
            # so the autogen sweep has *something* to feed the image model — otherwise the base
            # portrait would be a generic empty-prompt result.
            if not look:
                od = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
                gword = 'woman' if gender == 'female' else 'man'
                look = (
                    f"young {gword}, photogenic, neutral attractive features, mid-20s to mid-30s"
                    + (f", wearing {od[:140]}" if od else "")
                )
            char = {
                'id': str(uuid.uuid4())[:8],
                'name': raw_name,
                'description': fields.get('ROLE', '') or f'Появляется в сценарии',
                'appearance':  look,
                'gender':      gender,
                'voice_id':    '',
                'ref_images':  [],
                'outfits':     [],
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

        # Find existing outfit by label
        outfit = next((o for o in char.get('outfits', [])
                       if o.get('label','').lower() == raw_outfit.lower()), None)

        # IS_BASE marker: this outfit IS the character's base look — link to ref_images, no separate gen
        is_base_flag = fields.get('IS_BASE', '').lower() in ('true', 'yes', '1')

        # Auto-create new outfit if scene declares one with description
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

        # Apply IS_BASE: link photo/avai_url to character's base ref, mark is_base
        if outfit and is_base_flag and char.get('ref_images'):
            # Unmark other outfits as base (only one base per char)
            for o in char.get('outfits', []):
                if o['id'] != outfit['id'] and o.get('is_base'):
                    o['is_base'] = False
            if not outfit.get('photo'):
                outfit['photo'] = char['ref_images'][0]
            if not outfit.get('avai_url'):
                outfit['avai_url'] = char.get('avai_base_url', '')
            outfit['is_base'] = True

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
    return jsonify(list_episodes(sid))

_CREATE_EP_LOCKS = {}

@app.route('/api/series/<sid>/episodes', methods=['POST'])
def create_episode(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    # Critical section: list_episodes → number-assign → save must be atomic per series.
    # Without this, two near-simultaneous POSTs racing on a slow disk produce
    # duplicate episodes (first grabs N, second sees N taken and falls to N+1).
    lock = _CREATE_EP_LOCKS.setdefault(sid, threading.Lock())
    with lock:
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
            'title': data.get('title', f'Эпизод {num}'),
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
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400

    char_dir = assets_dir(sid) / 'characters' / char_id
    char_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    # Avoid collisions
    stem = Path(filename).stem
    ext = Path(filename).suffix
    final = char_dir / filename
    counter = 1
    while final.exists():
        final = char_dir / f'{stem}_{counter}{ext}'
        counter += 1

    file.save(final)
    rel_path = str(final.relative_to(series_path(sid)))

    for char in s['characters']:
        if char['id'] == char_id:
            char.setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}'})

@app.route('/api/series/<sid>/assets/character/<char_id>/<path:filename>', methods=['DELETE'])
def delete_character_asset(sid, char_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'characters' / char_id / filename
    if full.exists():
        full.unlink()
    rel = f'assets/characters/{char_id}/{filename}'
    for char in s['characters']:
        if char['id'] == char_id:
            char['ref_images'] = [r for r in char.get('ref_images', []) if r != rel]
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
    return send_from_directory(str(asset_path.parent), asset_path.name)


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
            'imageSize':           merged.get('image_size', '2K'),
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

def _avai_seedance_start(prompt, ref_urls, duration, resolution, moderation_bypass,
                          aspect_ratio='9:16', generate_audio=True,
                          moderation_bypass_prompt=None):
    """Kick off an async Seedance 2.0 reference-pro job.
    Returns dict {job_id, status_url, raw}."""
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
    headers = {'x-api-key': AVAI_KEY, 'content-type': 'application/json'}
    # Async mode: server returns 202 with job_id+status_url immediately
    resp = requests.post(
        AVAI_API + '?async=true', json=payload, headers=headers, timeout=60
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

def _avai_seedance_status(job_id, status_url=None):
    """Poll job. Returns dict {status, progress, video_url, cost, error, raw}."""
    headers = {'x-api-key': AVAI_KEY}
    url = status_url or f'https://avai-gen.com/api/public/generate/jobs/{job_id}'
    if url.startswith('/'):
        url = 'https://avai-gen.com' + url
    resp = requests.get(url, headers=headers, timeout=30)
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
    headers = {'x-api-key': AVAI_KEY, 'content-type': 'application/json'}
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

def _resolve_ref_url(s, ref, sid=None):
    """ref = {'kind':'char'|'outfit'|'loc', 'id':..., 'outfit':...}.
    Returns public AVAI URL or None."""
    if not ref:
        return None
    kind = ref.get('kind')
    if kind == 'char':
        c = next((x for x in s.get('characters', []) if x['id'] == ref.get('id')), None)
        if not c:
            return None
        if ref.get('outfit'):
            for o in c.get('outfits', []) or []:
                if o.get('label') == ref['outfit']:
                    return o.get('avai_url') or c.get('avai_base_url')
        return c.get('avai_base_url')
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
    if kind == 'url':
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
            headers = {'x-api-key': AVAI_KEY, 'content-type': 'application/json'}
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
        headers = {'x-api-key': AVAI_KEY, 'content-type': 'application/json'}
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
    return jsonify({'chunks': _seedance_chunks(ep)})

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

    # Previous chunk continuity
    prev_chunks = _seedance_chunks(ep)
    prev = prev_chunks[-1] if prev_chunks else None
    prev_block = ''
    if prev and prev.get('ending_state'):
        prev_block = (
            f"\nPREVIOUS CHUNK ENDING STATE (continue physically if same scene):\n"
            f"{prev.get('ending_state')}\n"
        )

    # Compact char/loc rosters
    chars_lines = []
    for c in s.get('characters', []) or []:
        if c.get('avai_base_url'):
            chars_lines.append(f"- {c['name']} (id={c['id']}): {c.get('appearance','')[:120]}")
    locs_lines = []
    for l in s.get('locations', []) or []:
        # Include all locations that have any image (ref_images OR avai_url) — LLM can still pick them.
        if l.get('avai_url') or l.get('ref_images'):
            tag = '' if l.get('avai_url') else ' [NO_AVAI_URL — нужно перегенерировать]'
            locs_lines.append(f"- {l['name']} (id={l['id']}){tag}: {l.get('description','')[:120]}")

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
        "  1) SUBJECT — кто в кадре, со ссылками @Image1, @Image2... — каждый ключевой перс упомянут.\n"
        "  2) ACTION — что они делают (одно главное действие/конфликт, без каши).\n"
        "  3) SCENE — где (ссылка на @Image<N> локации) + время суток / атмосфера.\n"
        "  4) CAMERA — конкретно: 'tracking shot', 'medium close-up', 'slow dolly in', 'handheld', 'over-the-shoulder', 'low angle'.\n"
        "  5) DIALOGUE — для КАЖДОЙ реплики из сценария формат: '@ImageN, <эмоция/тон на русском>, говорит: \"<точная реплика как в сценарии>\"'.\n"
        "     Пример: '@Image2, ухмыляясь с презрением, говорит: \"Lost, Aria? The garbage dump is three blocks down.\"'.\n"
        "     Эмоция/тон по-русски ОБЯЗАТЕЛЬНА перед каждой репликой — без неё лип-синк хуже.\n"
        "  6) STYLE/ATMOSPHERE — короткие фразы (cinematic, harsh fluorescent light, cold colour palette).\n"
        "  7) CONSTRAINTS — ТОЛЬКО позитивные формулировки ('smooth gimbal motion', 'stable framing'). "
        "     НЕ пиши 'no shake', 'without distortion' — модель плохо понимает отрицания.\n\n"
        "АНАЛИЗ КОНТЕКСТА — ОБЯЗАТЕЛЬНО ДЕЛАЙ ЭТО ДО ПРОМПТА:\n"
        "Тебе дают: (а) полный сценарий эпизода, (б) выделенный кусок (CHUNK), (в) активный состав персов и локаций эпизода.\n"
        "Прочитай ПОЛНЫЙ сценарий, найди в нём CHUNK и ответь себе на вопросы:\n"
        "  • В какой ЛОКАЦИИ происходит этот кусок? (Сцен-хедер сценария 'INT./EXT. <LOCATION> — <TIME>' — ищи последний перед CHUNK.)\n"
        "  • Какие ПЕРСОНАЖИ физически находятся в кадре? Это НЕ только говорящие. "
        "Если в сцене сказано что Liam стоит рядом и наблюдает — он в кадре, даже если в этом куске молчит. "
        "Если предыдущий чанк закончился тем что Selena вошла в комнату — она всё ещё в кадре в новом чанке той же сцены.\n"
        "  • Где каждый персонаж стоит/находится относительно других? (за столом, у двери, на коленях, и т.п.)\n\n"
        "РЕФЕРЕНСЫ — ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:\n"
        "1. Сначала персонажи, в порядке важности в кадре → @Image1, @Image2, @Image3...\n"
        "   В refs включай ВСЕХ персов в кадре (не только говорящих). Молчащий перс рядом — это часть мизансцены.\n"
        "2. ПОСЛЕДНИМ обязательно идёт ЛОКАЦИЯ → @Image<N+1>. ЭТО НЕ ОПЦИЯ.\n"
        "   Если в AVAILABLE LOCATIONS есть локация, совпадающая с местом действия (по сцен-хедеру или контексту) — "
        "   ОБЯЗАТЕЛЬНО прикрепи её последним @Image. Без локации фон будет рандомным и серия развалится визуально.\n"
        "   Если в roster нет идеально совпадающей локации — выбери максимально близкую по описанию (офис, лобби, спальня и т.п.).\n"
        "   Локацию НЕ ВКЛЮЧАЙ только если в roster вообще нет ни одной подходящей локации с фото.\n"
        "3. В тексте промпта в блоке SCENE явно упомяни локацию: 'Действие в @Image<N+1> — стеклянное лобби корпорации, холодное освещение'.\n"
        "4. Максимум 5 референсов (обычно 1–3 перса + 1 локация).\n\n"
        "CONTINUITY: если есть PREVIOUS CHUNK ENDING STATE и сцена та же (локация + состав персов + нет скачка во времени) — "
        "отрази стартовую позу/состояние. Если локация/время/состав сменились — начинай свежо, состояние не тащим."
    )
    # Active episode cast & locations (already checked off in sidebar)
    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    active_chars_block = '\n'.join(
        f"- {c['name']} (id={c['id']}): {c.get('appearance','')[:120]}"
        for c in active_chars
    ) or '(не отмечены)'
    active_locs_block = '\n'.join(
        f"- {l['name']} (id={l['id']}): {l.get('description','')[:120]}"
        for l in active_locs
    ) or '(не отмечены)'

    full_script = (ep.get('script') or '').strip()
    full_script_block = full_script[:8000]  # safety cap

    userprompt = (
        f"AVAILABLE CHARACTERS (весь roster серии):\n{chr(10).join(chars_lines) or '(none)'}\n\n"
        f"AVAILABLE LOCATIONS (весь roster серии):\n{chr(10).join(locs_lines) or '(none)'}\n\n"
        f"ACTIVE THIS EPISODE (отмечены в эпизоде — приоритет при выборе):\n"
        f"  Characters:\n{active_chars_block}\n"
        f"  Locations:\n{active_locs_block}\n"
        f"{prev_block}\n"
        f"FULL EPISODE SCRIPT (читай ВЕСЬ — тут scene headings, ремарки, кто где находится):\n"
        f"```\n{full_script_block}\n```\n\n"
        f"CHUNK — выделенный кусок для генерации (его репликам сохраняй verbatim):\n"
        f"```\n{chunk_text}\n```\n\n"
        f"Найди CHUNK внутри FULL SCRIPT, посмотри ближайший SCENE HEADING выше него — оттуда возьми локацию и время суток. "
        f"Посмотри ремарки/[действия] вокруг CHUNK — оттуда возьми кто физически в кадре (включая молчащих). "
        f"Эти персонажи ОБЯЗАТЕЛЬНО идут в refs, даже если в CHUNK у них нет реплик.\n\n"
        "Верни JSON и НИЧЕГО кроме JSON:\n"
        "{\n"
        '  "prompt": "ru/en motion prompt, ~60-110 слов, по структуре выше, с эмоциями перед каждой репликой и финальным @Image<N> локации",\n'
        '  "refs": [{"kind":"char","id":"...","outfit":"label_or_null"}, ..., {"kind":"loc","id":"..."}],\n'
        '  "scene_continuity": true|false,\n'
        '  "reasoning": "одно предложение — почему именно эти референсы и continuity"\n'
        "}\n\n"
        "ПРОВЕРКА перед выводом:\n"
        "- ВСЕ описания на русском (кроме реплик в кавычках)? Если нашёл английское слово вне кавычек — перепиши.\n"
        "- Каждая реплика из chunk имеет эмоцию/тон по-русски перед ней? Если нет — допиши.\n"
        "- Локация прикреплена последним @Image и упомянута в prompt? Если в roster есть подходящая — обязательно.\n"
        "- Все @ImageN из prompt совпадают по индексу с порядком в refs?\n"
        "- В prompt нет 'no <X>'? Замени на позитив."
    )
    try:
        raw = claude_ask(userprompt, system=sysprompt)
        data = json.loads(strip_json(raw))
    except Exception as e:
        return jsonify({'error': f'compose failed: {e}'}), 500

    refs = data.get('refs') or []
    ref_urls = []
    ref_meta = []
    unresolved = []
    for r in refs:
        url = _resolve_ref_url(s, r, sid=sid)
        if url:
            ref_urls.append(url)
            ref_meta.append({**r, 'url': url})
        else:
            unresolved.append({**r, 'reason': 'нет фото / avai_url не получился'})
    return jsonify({
        'prompt': data.get('prompt', ''),
        'refs': ref_meta,
        'ref_urls': ref_urls,
        'unresolved_refs': unresolved,
        'scene_continuity': data.get('scene_continuity'),
        'reasoning': data.get('reasoning', ''),
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
    if mod not in ('off', 'grid', 'collage_grid'):
        mod = 'collage_grid'
    aspect = body.get('aspect_ratio') or '9:16'
    gen_audio = bool(body.get('generate_audio', True))

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
    }
    chunks.append(chunk)
    save_episode(sid, num, ep)

    # Kick off the AVAI call in a background thread so the UI gets the chunk
    # card instantly. The poll endpoint will then track job_id/status as soon
    # as the thread finishes the submission.
    def _submit():
        try:
            job = _avai_seedance_start(
                prompt=prompt, ref_urls=ref_urls,
                duration=duration, resolution=resolution,
                moderation_bypass=mod, aspect_ratio=aspect,
                generate_audio=gen_audio,
            )
            ep2 = load_episode(sid, num)
            for c in _seedance_chunks(ep2):
                if c.get('idx') == idx:
                    c['job_id'] = job['job_id']
                    c['status_url'] = job['status_url']
                    c['status'] = 'pending'
                    break
            save_episode(sid, num, ep2)
        except Exception as e:
            ep2 = load_episode(sid, num)
            for c in _seedance_chunks(ep2):
                if c.get('idx') == idx:
                    c['status'] = 'failed'
                    c['error'] = f'submit failed: {e}'
                    break
            save_episode(sid, num, ep2)

    threading.Thread(target=_submit, daemon=True).start()
    return jsonify({'chunk': chunk})

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/poll', methods=['POST'])
def seedance_poll(sid, num):
    """Poll all non-final chunks; download finished mp4s; extract ending_state."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    chunks = _seedance_chunks(ep)
    changed = False
    for c in chunks:
        if c.get('status') in ('completed', 'failed'):
            continue
        if not c.get('job_id'):
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
            try:
                vid_local = vid_dir(sid) / f'seedance_ep{int(num):03d}_chunk{c["idx"]:03d}.mp4'
                _download_video(st['video_url'], vid_local)
                c['video_url'] = st['video_url']
                c['video_path'] = str(vid_local.relative_to(series_path(sid)))
                # Extract ending state from chunk_text via Claude (best-effort)
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
    print('Series Writer запущен → http://localhost:8080')
    app.run(debug=True, port=8080, host='0.0.0.0', threaded=True)
