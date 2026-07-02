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


# Pattern for "CHARACTER: spoken text" — captures the speaker (uppercase
# letters Latin/Cyrillic, 2+ chars) and the spoken body. Allows an optional
# parenthetical action note between the name and the colon, e.g.
#   VICTORIA *(nervous laugh)*: Wait... no.
#   МАРКУС (тихо): That was before I knew.
# The negative lookbehind via `[A-ZА-ЯЁ]` requires the FIRST char to be
# uppercase so we don't false-match "Time:" or "Location:" labels.
_DIALOGUE_LINE_RE = re.compile(
    r'^[ \t]*([A-ZА-ЯЁ][A-ZА-ЯЁ0-9 \-\.]{1,30})\s*(?:\*?\([^)\n]+\)\*?)?\s*:\s*(.+?)\s*$',
    re.MULTILINE,
)
# Action-only line wrapped in `*(...)*` or `[...]` — we explicitly DON'T
# include these in dialogue detection (action lines may legitimately stay
# in Russian per _SCRIPT_SYSTEM convention).
_ACTION_LINE_RE = re.compile(
    r'^\s*(?:\*\([^)]+\)\*|\[[^\]]+\])\s*$', re.MULTILINE,
)
_CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')
_CJK_RE      = re.compile(r'[一-鿿]')          # Chinese / shared Han
_HIRAGANA_RE = re.compile(r'[぀-ゟ]')
_KATAKANA_RE = re.compile(r'[゠-ヿ]')
_HANGUL_RE   = re.compile(r'[가-힯]')
_LATIN_RE    = re.compile(r'[A-Za-z]')

def _detect_dialogue_language(text: str) -> dict:
    """Walk every "CHARACTER: spoken text" line, measure how much of the
    spoken body is non-Latin script. Returns a dict the preview endpoint
    can pass straight to the UI:

        {dialogue_lines, non_english_lines, ratio,
         sample_lines: [str], detected_lang: 'ru'|'zh'|'ja'|'ko'|'other'|'en'}

    Action lines wrapped in `*(...)*` or `[...]` are ignored — they may
    legitimately stay in Russian per the _SCRIPT_SYSTEM convention.
    The caller decides whether to warn (suggested threshold: ratio > 0.15).
    """
    if not text or not text.strip():
        return {'dialogue_lines': 0, 'non_english_lines': 0, 'ratio': 0.0,
                'sample_lines': [], 'detected_lang': 'en'}

    total = 0
    non_en = 0
    lang_counts = {'ru': 0, 'zh': 0, 'ja': 0, 'ko': 0, 'other': 0}
    samples = []

    for m in _DIALOGUE_LINE_RE.finditer(text):
        speaker = m.group(1).strip()
        spoken  = m.group(2).strip()
        # Filter false positives — labels like "TIME:", "LOCATION:" that
        # match the uppercase pattern but aren't real dialogue. Real
        # character names usually contain no whitespace OR are 1-2 words
        # max; if it's a single common label word, skip.
        if speaker.upper() in ('TIME', 'LOCATION', 'DAY', 'NIGHT', 'NOTE',
                               'BRIEF', 'SUMMARY', 'SCENE', 'EPISODE',
                               'CAST', 'CHARACTER', 'CHARACTERS', 'PLACE',
                               'ВРЕМЯ', 'МЕСТО', 'СЦЕНА', 'ЭПИЗОД', 'СЕРИЯ',
                               'ЛОКАЦИЯ', 'ПЕРСОНАЖИ', 'ПЕРСОНАЖ'):
            continue
        if not spoken or len(spoken) < 3:
            continue
        total += 1
        latin_n    = len(_LATIN_RE.findall(spoken))
        cyr_n      = len(_CYRILLIC_RE.findall(spoken))
        cjk_n      = len(_CJK_RE.findall(spoken))
        hira_n     = len(_HIRAGANA_RE.findall(spoken))
        kata_n     = len(_KATAKANA_RE.findall(spoken))
        hangul_n   = len(_HANGUL_RE.findall(spoken))
        non_latin  = cyr_n + cjk_n + hira_n + kata_n + hangul_n
        alpha_total = latin_n + non_latin
        if alpha_total == 0:
            continue  # all punctuation/digits — undecidable, skip
        non_latin_ratio = non_latin / alpha_total
        if non_latin_ratio > 0.4:
            non_en += 1
            # Classify which script dominated this line.
            buckets = (('ru', cyr_n), ('zh', cjk_n),
                       ('ja', hira_n + kata_n), ('ko', hangul_n))
            top = max(buckets, key=lambda b: b[1])
            lang_counts[top[0] if top[1] > 0 else 'other'] += 1
            if len(samples) < 3:
                # Trim long lines for UI display.
                sample = f'{speaker}: {spoken}'
                samples.append(sample[:140] + ('…' if len(sample) > 140 else ''))

    ratio = (non_en / total) if total else 0.0
    detected = 'en'
    if non_en > 0:
        # Pick the dominant non-EN script across all flagged lines.
        top = max(lang_counts.items(), key=lambda kv: kv[1])
        detected = top[0] if top[1] > 0 else 'other'

    return {
        'dialogue_lines': total,
        'non_english_lines': non_en,
        'ratio': round(ratio, 3),
        'sample_lines': samples,
        'detected_lang': detected,
    }


# Per-series import status; UI polls /import-status. Lives in-memory only;
# survives across requests in the same gunicorn worker (we run with workers=1
# anyway). On restart the user just sees no in-flight job and can retry.
_IMPORT_STATUS = {}  # sid -> {running, total, done, errors[], started_at, finished_at, current}
_IMPORT_LOCKS = {}

