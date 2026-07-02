"""Config: project paths, secrets, per-user keys and settings storage."""
import json
import os
import re
from pathlib import Path

# Project root (this file lives in <root>/sw/config.py).
BASE = Path(__file__).resolve().parent.parent
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


DEFAULT_AUTO_REVISE_INSTRUCTION = (
    'следи чтоб персонажи в чанках не перемещались незаметно в пространстве и не '
    'появлялись из неотткуда и чтоб на видео было понятно кто что кому говорит , где '
    'находится, что делает, куда передвигается, чтоб они внезапно не телепортировались '
    'из ниоткуда или не меняли за кадром положение или состояние между чанками (между '
    'концом одного чанка и началом другого.) Чанки должны монтажно между собой '
    'склеиваться. и сохраняться общая стилистика.  Должно быть понятно что происходит '
    'в серии с сохранением логики и диалогов. Внимательно следи за внешним видом/'
    'состоянием персонажей и описывай состояние внешного вида в каждом чанке '
    '(например наушник или кепка или что в руках держит). Следи за длинной реплик '
    'особенно в конце чанка. Если есть риск что реплика не успеет произнестись по '
    'факту - укороти реплики сохранив их смысл и эмоции.'
)

def _user_settings_path(email):
    safe = re.sub(r'[^a-z0-9]+', '_', (email or '').lower()).strip('_') or 'anon'
    return DATA_ROOT / safe / 'settings.json'

def _load_user_settings(email):
    """Returns dict with auto-revise + future per-user UI prefs."""
    p = _user_settings_path(email)
    if not p.exists():
        return {
            'auto_revise_enabled': True,
            'auto_revise_instruction': DEFAULT_AUTO_REVISE_INSTRUCTION,
        }
    try:
        d = json.loads(p.read_text())
        return {
            'auto_revise_enabled': bool(d.get('auto_revise_enabled', True)),
            'auto_revise_instruction': (d.get('auto_revise_instruction') or DEFAULT_AUTO_REVISE_INSTRUCTION).strip(),
        }
    except Exception:
        return {
            'auto_revise_enabled': True,
            'auto_revise_instruction': DEFAULT_AUTO_REVISE_INSTRUCTION,
        }

def _save_user_settings(email, settings):
    p = _user_settings_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        'auto_revise_enabled': bool(settings.get('auto_revise_enabled', True)),
        'auto_revise_instruction': (settings.get('auto_revise_instruction') or DEFAULT_AUTO_REVISE_INSTRUCTION).strip(),
    }
    p.write_text(json.dumps(safe, indent=2, ensure_ascii=False))


# All secrets are loaded once at startup. Env vars are the canonical source for
# production; config.json is a dev-only convenience fallback.
ANTHROPIC_KEY  = _load_secret('ANTHROPIC_API_KEY',  'anthropic_key')
AVAI_KEY       = _load_secret('AVAI_API_KEY',       'avai_key')
RETELLER_KEY   = _load_secret('RETELLER_API_KEY',   'reteller_key')
ELEVENLABS_KEY = _load_secret('ELEVENLABS_API_KEY', 'elevenlabs_key')
# OPENAI key used ONLY by the chunk-QC pipeline (Whisper language detection).
# Optional — if missing, language QC stage degrades to a no-op (pass-through).
OPENAI_KEY     = _load_secret('OPENAI_API_KEY',     'openai_key')

# ── EXPERIMENTAL feature flags ──────────────────────────────────────────────
# STRICT_CHAR_FILTER: drop ALL character refs from compose if their name is
# not in chunk_text. Catches composer over-attaching characters from episode
# roster (e.g. «Sophie sits in chair» when chunk only has Emma+Adrian dialogue).
# Trial flag — easy rollback: flip to '0' or remove env var.
# Risk: false-positive drop of chars who appear physically but aren't named
# (e.g. «her hand visible at edge of frame» — hand's owner not named).
# Dropped chars are logged in compose_warnings so the regression is visible.
STRICT_CHAR_FILTER = os.environ.get('STRICT_CHAR_FILTER', '1') == '1'

# Warn loudly at startup if anything is missing — easier than debugging 401s later.
for _name, _val in (('ANTHROPIC_API_KEY',  ANTHROPIC_KEY),
                    ('AVAI_API_KEY',       AVAI_KEY),
                    ('RETELLER_API_KEY',   RETELLER_KEY),
                    ('ELEVENLABS_API_KEY', ELEVENLABS_KEY),
                    ('OPENAI_API_KEY',     OPENAI_KEY)):
    if not _val:
        print(f'[config] WARNING {_name} is not set — related features will fail')
