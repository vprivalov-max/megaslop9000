"""Scene heading detection (mirrors static/app.js _matchSceneHeading)."""
import re

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
    # Time-coded beat ("0:00—0:05 — Hook" / "1:30 — On the way") added 2026-05-19
    # — short-drama scripts often mark scene breaks by timestamp instead of
    # INT./EXT. slug. Each beat tends to be a new location.
    r'^[\s*_#>]*(Локация\s*[:：]|Location\s*[:：]|СЦЕНА\s*\d|Сцена\s*\d|SCENE\s*\d|\d{1,2}:\d{2}\s*[—–\-])',
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
