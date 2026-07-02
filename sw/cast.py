"""Cast-matching helper: robust character-name detection in script text."""
import re

def _char_name_in_text(char_name: str, text: str, aliases=None) -> bool:
    """True if any component of char_name (or any registered alias) appears in
    text as a whole-word match. Robust to names with punctuation (Mrs. Vale,
    Dr. Brown, Officer Jenkins).

    `aliases` — optional list of script-side labels a character is also addressed
    by (e.g. «CLIENT» for «Mrs. Park»). Lets STRICT_CHAR_FILTER keep a character's
    reference when the chunk only uses the role label, not the canonical name.

    Old buggy version used `\\b{first}\\b` directly — broke on «Mrs.» because
    \\b after `.` requires word/non-word transition and `.` followed by space
    is non-word→non-word (no boundary). Result: Mrs. Vale silently dropped
    from refs by STRICT_CHAR_FILTER → composer's @ImageN slots got mis-
    assigned → boy's role given to woman in «The Maid Who Raised the
    Billionaire's Son» ep 17 chunk 1 (2026-05-25 prod incident).

    Strategy: try each whitespace-separated component, stripping trailing
    punctuation. Match if ANY component is in text. Skips very short
    components (single letter / common particles) to avoid false positives.
    """
    if not char_name or not text:
        return False
    SKIP = {'mr', 'mrs', 'ms', 'dr', 'st', 'sir', 'lady', 'lord', 'г', 'мр', 'мс', 'г-н', 'г-жа'}
    candidates = []
    for part in char_name.split():
        cleaned = part.strip(' .,;:!?"\'«»')
        if not cleaned or len(cleaned) < 2:
            continue
        if cleaned.lower() in SKIP:
            continue
        candidates.append(cleaned)
    # Always try the full name too (matches «Mrs. Vale» as a phrase if it
    # appears verbatim).
    if char_name.strip():
        candidates.insert(0, char_name.strip())
    for cand in candidates:
        # Phrase-aware: match against whitespace-normalized text.
        try:
            pat = re.compile(rf'\b{re.escape(cand)}\b', re.IGNORECASE)
        except re.error:
            continue
        if pat.search(text):
            return True
        # Fallback for trailing-dot names: pattern may fail on the trailing
        # boundary. Try a looser match — name followed by space/punctuation/EOS.
        if '.' in cand or "'" in cand:
            loose = re.compile(rf'(^|\W){re.escape(cand)}(\W|$)', re.IGNORECASE)
            if loose.search(text):
                return True
    # Alias fallback — script-side labels the character is also addressed by
    # (e.g. «CLIENT» → Mrs. Park). Plain whole-phrase match per alias.
    for alias in (aliases or []):
        alias = (alias or '').strip()
        if len(alias) < 2:
            continue
        try:
            if re.search(rf'\b{re.escape(alias)}\b', text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False

