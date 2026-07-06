"""Storage: per-user project folders, series/episode persistence (atomic JSON),
SCENE_OPEN outfit sync, batch-mode chunk math, plot-device registry, series canon."""
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from sw.auth import DEV_USER_EMAIL, _spawn_with_keys, current_user_email
from sw.config import BASE, DATA_ROOT, LEGACY_PROJECTS
from sw.llm import claude_ask
from sw.logging_utils import _log_event

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
PRPROJ_TEMPLATE = BASE / 'templates' / 'empty.prproj'


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
    data.setdefault('devices_index', {})                 # plot-device anti-repetition registry
    data.setdefault('cadence_policy', {'default_min_gap': 4, 'hard_limit': 3})
    data.setdefault('source_drama', None)                # {id,title,genre,premise,attribution,analyzed_through} | None
    data.setdefault('source_episode_outline', [])        # per-episode source beats (index i → episode i+1)
    # Cover art (short-drama style poster) — shown as background on the
    # project card in the main menu. Generated via /api/series/<sid>/cover/generate.
    data.setdefault('cover_image', '')          # rel path inside series dir, e.g. 'assets/cover.jpg'
    data.setdefault('cover_image_url', '')      # AVAI-hosted URL (set when freshly generated)
    data.setdefault('cover_image_version', 0)   # cache-buster bumped on every regeneration
    return data

def save_series(sid, data):
    series_path(sid).mkdir(exist_ok=True)
    _atomic_write_json(series_file(sid), data)

def load_episode(sid, num):
    f = episodes_dir(sid) / f'{int(num):03d}.json'
    return _load_json_resilient(f) if f.exists() else None

def save_episode(sid, num, data):
    episodes_dir(sid).mkdir(exist_ok=True)
    # New series use a DETERMINISTIC episode title: `<Series_Title>_E<N>`
    # (e.g. `Claimed_by_Two_Alphas_E1`) instead of the writer's creative
    # per-episode title. Gated per-series via `episode_title_format` so
    # existing series keep their writer-given titles untouched.
    try:
        if isinstance(data, dict):
            s = load_series(sid)
            if s and s.get('episode_title_format') == 'series_indexed':
                safe = re.sub(r'[^\w]+', '_', (s.get('title') or sid), flags=re.UNICODE).strip('_') or sid
                data = dict(data)
                data['title'] = f'{safe}_E{int(num)}'
    except Exception:
        pass
    _atomic_write_json(episodes_dir(sid) / f'{int(num):03d}.json', data)


_OLD_TAG_MAP = [
    ('[SCENE_OPEN]',    '[BLOCKING]'),
    ('[/SCENE_OPEN]',   '[/BLOCKING]'),
    ('[EPISODE_END]',   '[BLOCKING_END]'),
    ('[/EPISODE_END]',  '[/BLOCKING_END]'),
]

def _normalize_blocking_tags(text: str) -> str:
    """Replace legacy [SCENE_OPEN]/[EPISODE_END] tags with canonical [BLOCKING]/[BLOCKING_END].
    Safe to call on any script text — no-op if already on new format."""
    for old, new in _OLD_TAG_MAP:
        text = text.replace(old, new)
    return text


def _extract_end_position(script_text: str) -> str | None:
    """Extract [BLOCKING_END]...[/BLOCKING_END] block from script text.
    Returns the block string (including tags) or None if not present."""
    if not script_text:
        return None
    script_text = _normalize_blocking_tags(script_text)
    start = script_text.find('[BLOCKING_END]')
    end = script_text.find('[/BLOCKING_END]')
    if start == -1 or end == -1 or end <= start:
        return None
    return script_text[start:end + len('[/BLOCKING_END]')].strip()

# ── SCENE_OPEN outfit sync ─────────────────────────────────────────────────────

def _expand_outfit_label_to_desc(label: str, char_appearance: str = '', char_gender: str = '') -> str:
    """Cheap one-shot Claude call to turn a SHORT outfit label like
    `Business Casual` / `Simple Dress` / `Pajamas` into a CONCRETE wardrobe
    description (`tailored charcoal blazer, white shirt, dark trousers, ...`).

    Why: when the writer omits `| OUTFIT_DESC:` and we fall back to
    `description = label`, both the outfit-image gen prompt AND the seedance
    BINDING text become weak — Seedance reinvents the cloth on each chunk
    because there's no concrete anchor.

    Cost: ~600 input + 100 output tokens of Sonnet ≈ $0.003. Runs at most ONCE
    per (character × new-label) pair — cached as `outfit.description` forever.
    Returns the expanded description string, or the original label on error.
    """
    label = (label or '').strip()
    if not label:
        return ''
    gender_hint = ''
    if char_gender:
        gender_hint = f' for a {char_gender.lower()}' if char_gender.lower() in ('male', 'female') else ''
    sys_prompt = (
        "You expand a short outfit label into a concrete wardrobe description "
        "for a stable-diffusion image prompt. Output ONLY the description — "
        "no preamble, no quotes, no labels. 8-22 words. Comma-separated garments + colors + materials. "
        "No body parts, no poses, no scenery. Suitable for any episode this character appears in "
        "(don't tie it to a specific scene). Plain text only."
    )
    user_prompt = (
        f"Outfit label: {label}\n"
        f"Character context{gender_hint}: {(char_appearance or '')[:200] or '(none)'}\n\n"
        f"Expand into a concrete wardrobe description (garments + colors + materials)."
    )
    try:
        resp = claude_ask(user_prompt, system=sys_prompt, max_tokens=120, timeout=60, idle_timeout=30)
        resp = (resp or '').strip().strip('"').strip()
        # Sanity caps — never return raw model markup or paragraphs
        resp = resp.split('\n')[0].strip()
        if len(resp) < 8 or len(resp) > 300:
            return label
        return resp
    except Exception as e:
        _log_event('WARN', 'outfit_label_expand_failed', label=label, err=str(e)[:200])
        return label


# Russian-Cyrillic → Latin transliteration used ONLY to bridge Cyrillic script
# cues (`ДА ХИ`, `СО ДЖИН`) to a Latin/romanized roster (`Lee Da Hee`,
# `Kim Seo-jin`). `дж` is folded to `j` first so romanized Korean/CJK names land
# on their English spelling (`ДЖИН`→`jin`, not `dzhin`).
_CYR_DIGRAPHS = (('дж', 'j'),)
_CYR2LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def _translit_cyrillic_to_latin(s: str) -> str:
    s = (s or '').lower()
    for a, b in _CYR_DIGRAPHS:
        s = s.replace(a, b)
    return ''.join(_CYR2LAT.get(ch, ch) for ch in s)


def _phonetic_fold(token: str) -> str:
    """Collapse romanized-Korean long vowels + doubled letters to a single form
    so a transliterated cue (`hi`, `so`, `hyon`) lines up with the roster's
    romanization (`hee`, `seo`, `hyeon`). `ee→i`, `eo→o`, `oo→u`, then any
    remaining doubled letter is de-duplicated (`lee→li`)."""
    t = (token or '').lower()
    t = t.replace('eo', 'o').replace('ee', 'i').replace('oo', 'u')
    out = []
    for ch in t:
        if out and out[-1] == ch:
            continue
        out.append(ch)
    return ''.join(out)


def _resolve_char_by_script_name(script_name: str, chars: list) -> dict | None:
    """Match a character-name token taken from a script line (typically ALL-CAPS
    first name like `ADRIAN`, `MRS. VALE`) against the series character roster
    where canonical names are full names like `Adrian Blackwell` / `Mrs. Vale`.

    Strategy (in order):
      1. Exact case-insensitive full-name match.
      2. Script name matches the FIRST whitespace-separated token of a
         character name (`ADRIAN` ↔ `Adrian Blackwell`). Common short-drama
         pattern — scripts always use the first name in dialogue cues.
      3. Script name matches ANY whitespace-token in a character name
         (`VALE` ↔ `Mrs. Vale`) — last-name fallback.
      4. Cyrillic cue vs Latin/romanized roster — the writer emits blocking cues
         in Cyrillic (`ДА ХИ`) while the roster stores romanized names
         (`Lee Da Hee`). Transliterate the cue, then require EVERY cue token to
         fuzzy-match some name token; return the unambiguous best. Only fires
         when tiers 1-3 found nothing AND the cue actually contains Cyrillic, so
         pure-Latin projects are untouched.

    Returns the matching character dict, or None. If multiple chars match
    at the same tier the first one wins (stable order).
    """
    if not script_name or not chars:
        return None
    sn = re.sub(r'\s+', ' ', script_name.strip()).lower().rstrip('.,;:')
    if not sn:
        return None
    # Tier 1 — exact full-name
    for c in chars:
        nm = (c.get('name') or '').strip().lower()
        if nm == sn:
            return c
    # Tier 2 — first token of character name matches script name
    sn_first = sn.split()[0]
    for c in chars:
        nm_parts = (c.get('name') or '').strip().lower().split()
        if nm_parts and nm_parts[0].rstrip('.,;:') == sn_first:
            return c
    # Tier 3 — any token of character name matches script name
    for c in chars:
        nm_parts = [p.rstrip('.,;:') for p in (c.get('name') or '').strip().lower().split()]
        if sn_first in nm_parts:
            return c
    # Tier 4 — Cyrillic cue → romanized Latin roster (fuzzy, unambiguous only)
    if re.search(r'[а-яё]', sn):
        from difflib import SequenceMatcher
        cue_tokens = [_phonetic_fold(t) for t in re.split(r'[\s\-]+', _translit_cyrillic_to_latin(sn)) if t]
        if cue_tokens:
            scored = []
            for c in chars:
                name_tokens = [_phonetic_fold(t) for t in re.split(r'[\s\-.,;:]+', (c.get('name') or '').lower()) if t]
                if not name_tokens:
                    continue
                # every cue token must find a strong match in the name tokens;
                # the character's score is the weakest of those best-matches.
                per_token = [
                    max((SequenceMatcher(None, ct, nt).ratio() for nt in name_tokens), default=0.0)
                    for ct in cue_tokens
                ]
                scored.append((min(per_token), c))
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored and scored[0][0] >= 0.72:
                # require an unambiguous winner (avoids Student 1 vs Student 2 ties)
                if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08:
                    return scored[0][1]
    return None


def _normalize_outfit_label(label: str) -> str:
    """Canonicalize an outfit label for case-insensitive matching.
    'Business Suit' / 'business suit' / ' BUSINESS  SUIT ' → 'business suit'.
    Strips trailing punctuation, collapses whitespace.
    """
    if not label:
        return ''
    s = re.sub(r'\s+', ' ', label.strip().lower()).strip(' .,;:|')
    return s


def _parse_scene_open_outfits(script: str) -> dict:
    """Extract character→(label, description) mapping from EVERY [BLOCKING] block
    in the script, merging across scenes (later scene wins for the same label).

    New format: `CHAR: <position> :: OUTFIT: <Outfit Name> | OUTFIT_DESC: <desc>`
    Legacy format (no `|`): treat the text after `OUTFIT:` as both label and
    description so old scripts still parse.

    Returns {CHAR_NAME_UPPER: [(label, description), ...]} — list because one
    character can wear multiple outfits across scenes in one episode.
    """
    script = _normalize_blocking_tags(script)
    blocks = re.findall(r'\[BLOCKING\](.*?)\[/BLOCKING\]', script, re.DOTALL)
    if not blocks:
        return {}
    outfits: dict = {}
    for block in blocks:
        for line in block.split('\n'):
            line = line.strip()
            if ':: OUTFIT:' not in line:
                continue
            pos_part, outfit_part = line.split(':: OUTFIT:', 1)
            outfit_part = outfit_part.strip()
            if not outfit_part:
                continue
            # Split label from optional description.
            if '|' in outfit_part:
                head, tail = outfit_part.split('|', 1)
                label = head.strip().rstrip(',').strip()
                m_desc = re.search(r'OUTFIT[_ ]?DESC\s*[:\-]\s*(.+)$', tail.strip(), re.IGNORECASE)
                description = m_desc.group(1).strip() if m_desc else tail.strip()
            else:
                # Legacy: whole text is both label and description.
                label = outfit_part
                description = outfit_part
            if ':' in pos_part:
                char_name = pos_part.split(':', 1)[0].strip().upper()
                if char_name and char_name != 'LOCATION':
                    outfits.setdefault(char_name, []).append((label, description))
    return outfits


def _outfit_word_similarity(desc1: str, desc2: str) -> float:
    """Simple word-overlap similarity. Returns 0.0–1.0."""
    w1 = set(re.findall(r'[a-z]+', desc1.lower()))
    w2 = set(re.findall(r'[a-z]+', desc2.lower()))
    # Ignore trivial stop words
    stop = {'a', 'an', 'the', 'and', 'with', 'in', 'on', 'of', 'at', 'to'}
    w1 -= stop
    w2 -= stop
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


def _series_protagonist(s):
    """Best-guess lead character for series-level narrative cues (an identity
    change is virtually always about the protagonist). Prefers an explicit role
    flag, else the most-dressed character (leads change clothes most), else the
    first in the roster. Returns the character dict or None."""
    chars = list(s.get('characters') or [])
    if not chars:
        return None
    for c in chars:
        if str(c.get('role', '')).lower() in ('lead', 'protagonist', 'main', 'heroine', 'hero'):
            return c
    return max(chars, key=lambda c: len(c.get('outfits') or []))


def _apply_identity_shift_state(s, script: str) -> bool:
    """Detect a narrative identity/appearance change in `script` and persist it on
    the protagonist so it HOLDS across episodes:
      • active shift  → stamp char['identity_shift'] = {active, hair, alias, cue}
        and register the alias so name-matching finds the disguised name;
      • restore cue   → deactivate any existing identity_shift.
    Mutates `s` in place; returns True if anything changed (caller saves)."""
    # lazy: sanitizer is a higher layer (falls back to app.py until extracted)
    try:
        from sw.textrules_sanitizer import _detect_identity_shift
    except ImportError:
        from app import _detect_identity_shift
    shift = _detect_identity_shift(script)
    if not shift:
        return False
    proto = _series_protagonist(s)
    if not proto:
        return False
    cur = proto.get('identity_shift') or {}
    changed = False
    if not shift['active']:
        if cur.get('active'):
            cur['active'] = False
            proto['identity_shift'] = cur
            changed = True
            _log_event('INFO', 'identity_shift_restored', char=proto.get('name'))
        return changed
    # Active shift — merge (keep an already-known hair/alias if this cue omitted it).
    new_state = {
        'active': True,
        'hair':  shift['hair']  or cur.get('hair', ''),
        'alias': shift['alias'] or cur.get('alias', ''),
        'cue':   shift['cue'],
    }
    if new_state != cur:
        proto['identity_shift'] = new_state
        changed = True
        _log_event('INFO', 'identity_shift_detected', char=proto.get('name'),
                   hair=new_state['hair'], alias=new_state['alias'], cue=new_state['cue'])
    # Register a clean proper-name alias so future scripts that call the character
    # by the disguised name still resolve to the same refs.
    alias = (shift['alias'] or '').strip()
    if alias and re.fullmatch(r'[A-ZА-Я][a-zа-я]+(?:\s+[A-ZА-Я][a-zа-я]+){0,2}', alias):
        existing_names = {(c.get('name') or '').strip().lower() for c in (s.get('characters') or [])}
        if alias.lower() not in existing_names:
            aliases = proto.setdefault('aliases', [])
            if alias not in aliases:
                aliases.append(alias)
                changed = True
    return changed


def _sync_script_outfits(sid: str, script: str) -> list:
    """Parse [BLOCKING] outfit names from the script and reconcile against
    the character's outfit roster. Three-tier match logic:

      1. Exact label match (case-insensitive, normalized) → REUSE existing outfit.
         If the script provides a fresh OUTFIT_DESC and the existing outfit
         has no description yet, fill it in. Otherwise keep canonical.
      2. No label match BUT description fuzzy-similarity >= 0.65 to an existing
         outfit → REUSE that outfit (user's «<2 параметров отличия» rule —
         don't proliferate near-duplicates).
      3. Otherwise → CREATE a new outfit object with the writer's exact label
         + description, and force-trigger background image generation so the
         asset is ready by the time the user opens compose.

    Returns list of newly created outfit dicts for UI notification.
    """
    scene_outfits = _parse_scene_open_outfits(script)
    if not scene_outfits:
        return []

    s = load_series(sid)
    chars = s.get('characters', [])
    created = []
    changed = False

    for char_name_upper, label_desc_pairs in scene_outfits.items():
        # Resolve script name to series character — handles `ADRIAN` ↔ `Adrian Blackwell`
        # via the multi-tier helper (exact / first-token / any-token).
        char = _resolve_char_by_script_name(char_name_upper, chars)
        if not char:
            _log_event('INFO', 'outfit_sync_char_not_found', sid=sid, script_name=char_name_upper)
            continue

        # Skip the special «Base» label — that's the character's default look,
        # which already lives as the base reference image, not a separate outfit.
        existing = char.setdefault('outfits', [])

        # Dedup within this episode's pairs (the writer can name the same outfit
        # multiple scenes in a row — we only need to reconcile each label once).
        seen_in_episode = set()
        for label, description in label_desc_pairs:
            norm = _normalize_outfit_label(label)
            if not norm or norm in ('base', 'baseline', 'default'):
                continue
            if norm in seen_in_episode:
                continue
            seen_in_episode.add(norm)

            # 1) Exact-label match (case-insensitive)
            match = next(
                (o for o in existing if _normalize_outfit_label(o.get('label', '')) == norm),
                None
            )
            if match:
                # Backfill description if writer just provided one for an
                # outfit object that was created without it earlier.
                if description and description != label and not (match.get('description') or '').strip():
                    match['description'] = description
                    changed = True
                continue

            # 2) Fuzzy description match — block near-duplicate outfits
            best_score = 0.0
            best_obj = None
            if description:
                for o in existing:
                    o_desc = (o.get('description') or '').strip()
                    if not o_desc:
                        continue
                    score = _outfit_word_similarity(description, o_desc)
                    if score > best_score:
                        best_score = score
                        best_obj = o
            if best_obj is not None and best_score >= 0.65:
                _log_event('INFO', 'outfit_deduped_by_desc', sid=sid,
                           char=char.get('name'),
                           new_label=label,
                           merged_into=best_obj.get('label'),
                           score=round(best_score, 2))
                continue

            # 3) Create new outfit; force auto-gen so the image is ready by render time.
            # CRITICAL: if writer omitted OUTFIT_DESC the fallback is description=label
            # (e.g. "Business Casual"). That's a USELESS text anchor — both the i2i
            # outfit-image gen prompt AND Seedance BINDING become weak, so each chunk
            # renders the cloth differently. Expand via Claude to a concrete description
            # before creating the object so downstream renders have something to anchor.
            final_description = description.strip()
            if (not final_description) or (final_description.lower() == label.strip().lower()):
                final_description = _expand_outfit_label_to_desc(
                    label.strip(),
                    char.get('appearance', ''),
                    char.get('gender', ''),
                ) or label.strip()
            outfit = {
                'id':                  str(uuid.uuid4())[:8],
                'label':               label.strip()[:48],
                'description':         final_description,
                'photo':               None,
                'reteller_project_id': None,
                'auto_from_script':    True,
            }
            existing.append(outfit)
            created.append({'char_name': char.get('name'), 'char_id': char.get('id'), 'outfit': outfit})
            changed = True
            _log_event('INFO', 'outfit_auto_created', sid=sid,
                       char=char.get('name'), label=outfit['label'])

    # Persist any narrative identity/hair change (disguise / new identity) so the
    # disguised look holds across later episodes (writer-feed + compose guard).
    try:
        if _apply_identity_shift_state(s, script):
            changed = True
    except Exception as e:
        _log_event('WARN', 'identity_shift_apply_failed', sid=sid, err=str(e)[:200])

    if changed:
        save_series(sid, s)

    # Force background generation regardless of the series-level
    # auto_generate_assets flag — when the writer introduces a new outfit
    # in a script we always want the image ready by the time the user
    # opens compose (user request: "сразу уйти в генерацию персу").
    if created:
        try:
            # lazy: autogen is a higher layer (falls back to app.py until extracted)
            try:
                from sw.autogen import auto_generate_missing_assets
            except ImportError:
                from app import auto_generate_missing_assets
            _spawn_with_keys(auto_generate_missing_assets, sid)
        except Exception as e:
            print(f'[sync_script_outfits] autogen spawn failed for {sid}: {e}')

    return created


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


# ── Plot-device anti-repetition registry ─────────────────────────────────────
DEVICE_TAXONOMY = [
    "written_message",        # letter, note, diary, envelope
    "overheard_dialogue",     # character accidentally hears
    "phone_call_stranger",    # unknown caller delivers info
    "dream_flashback",        # memory or dream sequence
    "confession_direct",      # character admits/tells directly
    "discovery_object",       # finding significant physical item
    "confrontation_domestic", # argument/conflict at home/private
    "confrontation_public",   # argument/conflict in public
    "betrayal_reveal",        # ally revealed as enemy/traitor
    "rescue_escape",          # physical rescue or escape
    "legal_threat",           # lawsuit, police, official action
    "ally_arrives",           # unexpected helper appears
    "surveillance_caught",    # character discovered watching/recording
    "blackmail",              # coercive leverage
    "accident_staged",        # arranged accident or near-miss
]
DEVICE_FUNCTIONS = ["revelation", "tension", "bonding", "relief", "escalation"]

NARRATIVE_ARCHETYPES = [
    "investigation",   # protagonist actively gathers information
    "escalation",      # protagonist gains ground, antagonist weakens
    "setback",         # protagonist loses ground, antagonist wins
    "revelation",      # major truth uncovered that changes everything
    "alliance",        # new ally gained or relationship shifts
    "confrontation",   # direct clash between protagonist and antagonist
    "consequence",     # characters deal with fallout from prior actions
    "twist",           # unexpected turn that subverts expectations
    "execution",       # protagonist's plan finally plays out
]
NARRATIVE_POWER_DELTA = ["protagonist_wins", "antagonist_wins", "stasis"]
NARRATIVE_EMOTIONS    = ["triumph", "hope", "dread", "grief", "rage", "confusion", "relief", "suspense"]
NARRATIVE_ANT_MOMENTUM = ["escalating", "plateauing", "declining", "absent"]

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


