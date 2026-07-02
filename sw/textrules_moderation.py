"""Deterministic moderation-trigger lexicon + LLM rewrite helpers (RECALL backstop)."""
import json
import re

from sw.jsonutils import strip_json
from sw.llm import claude_ask
from sw.logging_utils import _log_event
from sw.scriptparse import _DIALOGUE_LINE_RE

# ─── Deterministic moderation-trigger lexicon (RECALL backstop) ───────────
# Seedance moderation is largely SURFACE-LEXICAL: a token like "suicide",
# "slaughter", "kill" or "blood" trips the filter regardless of whether the
# line is a literal threat, third-person, or figurative ("that's suicide").
# The LLM advisor reasons about *intent* and therefore systematically misses
# this whole class (the "Cassius will slaughter them" / "that's suicide" case,
# ep22, Jun 2026). This deterministic scan flags a dialogue line whenever it
# contains a trigger token — no matter what the LLM thinks. Keep the vocabulary
# a superset of _SD_BANLIST's left-hand sides plus the obvious gaps it omits.
_MOD_TRIGGER_GROUPS = [
    (re.compile(r'\b(?:kill(?:s|ed|ing|er)?|murder(?:s|ed|ing)?|slaughter(?:s|ed|ing)?'
                r'|massacre[sd]?|butcher(?:s|ed|ing)?|behead(?:s|ed|ing)?'
                r'|execute[sd]?|executing|execution|assassinate[sd]?|assassin'
                r'|slay|slain|slays|exterminate[sd]?)\b', re.IGNORECASE),
     'Насилие/убийство — Seedance ловит токен (kill/murder/slaughter/massacre/…) даже в переносном или 3-м лице'),
    (re.compile(r'\b(?:suicide|suicidal|kill\s+myself|killing\s+myself|end\s+my\s+life'
                r'|take\s+my\s+(?:own\s+)?life|hang\s+myself|slit\s+my\s+wrists?'
                r'|self[\s-]?harm|overdose)\b', re.IGNORECASE),
     "Суицид/селф-харм — токен suicide триггерит модерацию даже в идиоме «that's suicide»"),
    (re.compile(r'\b(?:shoot(?:s|ing)?|shot|gun(?:s|ned|man|men)?|pistols?|rifles?'
                r'|firearms?|stab(?:s|bed|bing)?|knife|knives|blades?|strangle[sd]?'
                r'|strangling|choke[sd]?|choking|drown(?:s|ed|ing)?|poison(?:s|ed|ing)?'
                r'|torture[sd]?|torturing|rape[sd]?|raping|rapist)\b', re.IGNORECASE),
     'Оружие/способ насилия — surface-токен (gun/shoot/stab/strangle/poison/rape/…)'),
    (re.compile(r'\b(?:blood(?:y|ied)?|bleed(?:s|ing)?|bled|gore|gory'
                r'|dismember(?:s|ed|ing)?|mutilate[sd]?|decapitate[sd]?'
                r'|corpses?|dead\s+bod(?:y|ies))\b', re.IGNORECASE),
     'Кровь/увечья — графический токен'),
    (re.compile(r"\b(?:you(?:'re|\s+are)\s+(?:so\s+)?dead|i(?:'ll|\s+will)\s+end\s+you"
                r"|i(?:'ll|\s+will)\s+destroy\s+you|i(?:'ll|\s+will)\s+make\s+you\s+pay"
                r"\s+with\s+your\s+life)\b", re.IGNORECASE),
     'Прямая угроза смертью'),
]

# Labels that match the ALLCAPS speaker pattern but aren't real speakers.
_NON_SPEAKER_LABELS = {
    'TIME', 'LOCATION', 'DAY', 'NIGHT', 'NOTE', 'BRIEF', 'SUMMARY', 'SCENE',
    'EPISODE', 'CAST', 'CHARACTER', 'CHARACTERS', 'PLACE', 'INT', 'EXT',
    'ВРЕМЯ', 'МЕСТО', 'СЦЕНА', 'ЭПИЗОД', 'СЕРИЯ', 'ЛОКАЦИЯ', 'ПЕРСОНАЖИ', 'ПЕРСОНАЖ',
}


def _lexical_moderation_scan(script: str) -> list:
    """Deterministic dialogue scan for moderation-trigger tokens. Walks every
    "SPEAKER: spoken" line and flags any that contains a trigger word. This is
    the RECALL guarantee — it does not depend on the LLM advisor's judgment.
    Returns [{original, reason, trigger}]."""
    if not script:
        return []
    out = []
    for m in _DIALOGUE_LINE_RE.finditer(script):
        speaker = m.group(1).strip()
        spoken = m.group(2).strip()
        if speaker.upper() in _NON_SPEAKER_LABELS or len(spoken) < 3:
            continue
        for rx, reason in _MOD_TRIGGER_GROUPS:
            hit = rx.search(spoken)
            if hit:
                out.append({
                    'original': f'{speaker}: "{spoken}"',
                    'reason': reason,
                    'trigger': hit.group(0),
                })
                break   # one warning per line is enough
    return out


# Last-resort euphemisms — used ONLY to fabricate a non-empty suggestion when
# the LLM rewrite call fails, so a real moderation risk is never silently
# dropped by the UI (which hides warnings whose `suggestions` array is empty).
_SOFTEN_MAP = [
    (re.compile(r'\bslaughter(s|ed|ing)?\b', re.IGNORECASE), 'crush'),
    (re.compile(r'\bmassacre[sd]?\b', re.IGNORECASE), 'overwhelm'),
    (re.compile(r'\bbutcher(s|ed|ing)?\b', re.IGNORECASE), 'crush'),
    (re.compile(r'\b(?:behead|execute|assassinate)[sd]?\b', re.IGNORECASE), 'finish'),
    (re.compile(r'\b(?:murder|kill)(?:s|ed|ing)?\b', re.IGNORECASE), 'finish'),
    (re.compile(r'\b(?:slay|slain|slays)\b', re.IGNORECASE), 'defeat'),
    (re.compile(r'\bsuicide\b', re.IGNORECASE), 'madness'),
    (re.compile(r'\b(?:stab(?:s|bed|bing)?)\b', re.IGNORECASE), 'strike'),
    (re.compile(r'\b(?:blood(?:y)?|gore|gory)\b', re.IGNORECASE), 'wreckage'),
    (re.compile(r'\b(?:strangle[sd]?|choke[sd]?)\b', re.IGNORECASE), 'silence'),
]


def _soften_line(text: str) -> str:
    out = text
    for rx, repl in _SOFTEN_MAP:
        out = rx.sub(repl, out)
    return out


def _fallback_suggestions(original: str) -> list:
    """Deterministic euphemism rewrite so a flagged line always carries at least
    one suggestion (UI hides suggestion-less warnings)."""
    m = re.match(r'^\s*([^:]{1,40}):\s*"?(.*?)"?\s*$', original, re.S)
    if m:
        sp, body = m.group(1).strip(), m.group(2).strip()
        soft = _soften_line(body)
        return [f'{sp}: "{soft}"'] if soft != body else []
    soft = _soften_line(original)
    return [soft] if soft != original else []


_REWRITE_SYSTEM = (
    "You rewrite short vertical-drama dialogue lines that trip Seedance's "
    "keyword moderation. For each numbered line, REMOVE the trigger word(s) "
    "(kill / slaughter / suicide / gun / blood / stab / …) while keeping the "
    "dramatic punch and a natural in-character voice. Never produce robotic "
    "euphemisms ('tactical equipment'). Return ONLY valid JSON, no markdown: "
    "{\"rewrites\": {\"1\": [\"alt a\", \"alt b\"], \"2\": [...]}} — 2-3 "
    "alternatives per numbered line."
)


def _author_rewrites(lines: list, script: str) -> dict:
    """Batch-ask the LLM for natural rewrites of the flagged lines (only the
    lines, not the whole script). Returns {original_line: [alt, ...]}.
    Best-effort — returns {} on any failure."""
    if not lines:
        return {}
    numbered = '\n'.join(f'{i+1}. {l}' for i, l in enumerate(lines))
    try:
        raw = claude_ask(
            f"Context (tone only):\n{(script or '')[:4000]}\n\nLines to rewrite:\n{numbered}",
            system=_REWRITE_SYSTEM,
            model='claude-haiku-4-5',
            max_tokens=2048,
            timeout=60,
        ).strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
            raw = re.sub(r'\n?```\s*$', '', raw).strip()
        obj = json.loads(strip_json(raw))
        rw = obj.get('rewrites') or {}
        out = {}
        for i, line in enumerate(lines):
            alts = rw.get(str(i + 1)) or rw.get(i + 1) or []
            alts = [a for a in alts if isinstance(a, str) and a.strip()]
            if alts:
                out[line] = alts[:3]
        return out
    except Exception as e:
        _log_event('WARN', 'author_rewrites_failed', err=str(e)[:200])
        return {}


def _modkey(s: str) -> str:
    """Normalize a warning's `original` for dedup across LLM/lexical sources
    (tolerates quote/spacing/format differences)."""
    return re.sub(r'[^a-z0-9а-яё]', '', (s or '').lower())


def _merge_moderation_warnings(llm_warnings, script):
    """Union of the LLM advisor's warnings and the deterministic lexical scan,
    deduped by line. The lexical scan guarantees recall; the LLM supplies
    nuance + natural rewrites. For lexical-only lines (LLM missed them) we
    author rewrites in one batched call, falling back to euphemisms so every
    surfaced warning has a non-empty `suggestions` array (else the UI hides it)."""
    by, order = {}, []
    for w in (llm_warnings or []):
        if not isinstance(w, dict) or not w.get('original'):
            continue
        k = _modkey(w['original'])
        if k not in by:
            order.append(k)
        by[k] = {
            'original': w.get('original'),
            'reason': (w.get('reason') or 'Возможный триггер модерации').strip(),
            'suggestions': [s for s in (w.get('suggestions') or []) if isinstance(s, str) and s.strip()],
        }
    needs = []
    for w in _lexical_moderation_scan(script):
        k = _modkey(w['original'])
        if k in by:
            continue   # already covered by the LLM (with suggestions)
        by[k] = {'original': w['original'], 'reason': w['reason'], 'suggestions': []}
        order.append(k)
        needs.append(w)
    if needs:
        rewrites = _author_rewrites([w['original'] for w in needs], script)
        for w in needs:
            k = _modkey(w['original'])
            sug = rewrites.get(w['original']) or _fallback_suggestions(w['original'])
            by[k]['suggestions'] = sug
    # UI hides suggestion-less warnings; only surface actionable ones.
    return [by[k] for k in order if by[k]['suggestions']]


_PHRASE_CHECK_SYSTEM = (
    "You are a content moderation advisor for short-form drama videos generated by Seedance AI. "
    "Scan the provided script for dialogue lines that will realistically trigger Seedance moderation failure.\n\n"

    "HOW SEEDANCE MODERATION ACTUALLY WORKS: it is largely KEYWORD-DRIVEN. A single surface word — "
    "kill, murder, slaughter, massacre, suicide, blood, gun, shoot, stab, knife, strangle, poison, rape, "
    "torture, corpse — can fail the whole clip, EVEN when the word is figurative, third-person, or about "
    "the past. Treat the presence of the word as the risk, NOT the intent behind it.\n\n"

    "FLAG any dialogue line containing such vocabulary:\n"
    "- Killing / death-violence: kill, murder, slaughter, massacre, butcher, behead, execute, assassinate, slay\n"
    "- Suicide / self-harm: suicide (INCLUDING figurative 'that's suicide'), 'kill myself', 'end my life', overdose, self-harm\n"
    "- Weapons / methods: gun, shoot, shot, stab, knife, blade, strangle, choke, drown, poison, torture, rape\n"
    "- Gore: blood, bloody, gore, dismember, mutilate, decapitate, corpse, dead body\n"
    "- Direct death threats: \"you're dead\", \"I'll end you\", \"I'll destroy you\"\n"
    "- Sexual content: explicit acts or body parts in sexual context; explicit drug use ('inject heroin')\n\n"

    "EXPLICIT EXAMPLES THAT MUST BE FLAGGED (do not rationalize them away):\n"
    "- \"That's suicide.\"  → contains 'suicide'\n"
    "- \"Cassius will slaughter them.\"  → contains 'slaughter' (third-person is still flagged)\n"
    "- \"He was killed years ago.\"  → contains 'killed'\n"
    "- \"This job gets people killed.\"  → contains 'killed'\n\n"

    "DO NOT FLAG lines with NO trigger vocabulary, however tense: 'I'll ruin you', 'you'll regret this', "
    "'you have no idea what's coming' — these carry no keyword and pass fine.\n\n"

    "STRICT RULE: If you flag a line, you MUST provide exactly 2-3 natural rewrite suggestions that REMOVE "
    "the trigger word while keeping the dramatic meaning. Never include a warning with an empty suggestions array.\n\n"

    "Suggestions must sound like real speech in context — organic and human, never robotic:\n"
    "BAD: 'Drop the weapon' → 'Relinquish your tactical equipment'\n"
    "GOOD: 'Drop the weapon' → 'Put it down!' / 'Drop it, now!'\n"
    "GOOD: \"That's suicide.\" → \"That's madness.\" / \"You'll never make it out.\"\n"
    "GOOD: \"Cassius will slaughter them.\" → \"Cassius will tear them apart.\" / \"Cassius won't leave one standing.\"\n\n"

    "Return ONLY valid JSON (no markdown):\n"
    "{\"moderation_warnings\": [{\"original\": \"CHAR: \\\"line\\\"\", \"reason\": \"one line — what specifically is the risk\", "
    "\"suggestions\": [\"CHAR: \\\"alt1\\\"\", \"CHAR: \\\"alt2\\\"\"]}]}\n"
    "If nothing found: {\"moderation_warnings\": []}"
)


