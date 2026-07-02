"""Identity-shift detection, undressed-state handling and the moderation-safe
appearance sanitizer."""
import re

from sw.logging_utils import _log_event
from sw.textrules_banlists import (_GARMENT_NOUN, _OUTFIT_HAIR_RE,
                                   _override_hair_in_appearance,
                                   _strip_concrete_clothing,
                                   _strip_vague_clothing_tail)

# ── Narrative identity-shift detection (disguise / new identity / hair change) ──
# Generalises the disguise problem: parse the SCRIPT for cues that a character
# changes hair / takes a new identity, so the system can (a) persist that state
# on the character, (b) feed it to the writer so the disguise look is tagged in
# every later episode, and (c) surface a compose-time mismatch if a disguised
# character is about to render with the wrong hair.
_ID_HAIR_TOKENS = {
    'blonde': 'blonde', 'blond': 'blonde', 'platinum': 'platinum-blonde',
    'brunette': 'brunette', 'redhead': 'red', 'red-head': 'red', 'red': 'red',
    'raven': 'jet-black', 'auburn': 'auburn', 'ginger': 'ginger', 'silver': 'silver',
    'grey': 'grey', 'gray': 'grey', 'black': 'jet-black', 'brown': 'brown',
    'блондинк': 'blonde', 'брюнетк': 'brunette', 'рыж': 'red', 'шатенк': 'brown',
    'седой': 'silver', 'седая': 'silver',
}
# A hair-state word appearing in a CHANGE context ("now", "dye", "wig", "new").
_ID_HAIR_CHANGE_RE = re.compile(
    r'(?:'
    r"(?:you(?:'re| are)|she(?:'s| is)|he(?:'s| is)|now|now\s+a|as\s+a|becomes?\s+a)\s+"
    r'(platinum|blonde|blond|brunette|redhead|red[-\s]?head|raven|auburn|ginger|silver)\b'
    r'|(?:dye[ds]?|dyed|bleach(?:ed|es)?|colou?rs?|cut)\s+(?:her|his|their|the)?\s*hair'
    r'|(platinum|blonde|blond|brunette|redhead|raven|auburn|ginger)[-\s]+wig'
    r'|(?:теперь|стала|стал|стань)\s+(блондинк\w*|брюнетк\w*|рыж\w*|шатенк\w*)'
    r'|перекрас\w*\s+волос\w*|парик\w*'
    r')',
    re.IGNORECASE,
)
_ID_HAIR_WORD_RE = re.compile(
    r'\b(platinum|blonde|blond|brunette|redhead|red[-\s]?head|raven|auburn|ginger|silver|grey|gray'
    r'|блондинк\w*|брюнетк\w*|рыж\w*|шатенк\w*)\b',
    re.IGNORECASE,
)
_ID_ALIAS_RE = re.compile(
    r'(?:new\s+name\s+(?:is|:)|go(?:es)?\s+by|name\'?s\s+now|alias(?:\s+is)?|new\s+identity\s+(?:is|:)|'
    r'новое\s+имя\s*[:—-]?|теперь\s+(?:ты|вы)\s+)\s*'
    r'([A-ZА-Я][a-zа-я]+(?:\s+[A-ZА-Я][a-zа-я]+){0,2})',
    re.IGNORECASE,
)
# Cues that the disguise ENDS / true identity is restored.
_ID_RESTORE_RE = re.compile(
    r'(?:reveals?\s+(?:her|his|their)\s+(?:true|real)\s+identity'
    r'|(?:her|his|their)\s+(?:true|real)\s+(?:name|identity|self)'
    r'|removes?\s+(?:the|her|his)\s+wig|takes?\s+off\s+(?:the|her|his)\s+wig'
    r'|(?:back\s+to|returns?\s+to)\s+(?:her|his)\s+(?:natural|real|own)\s+hair'
    r'|natural\s+hair\s+again'
    r'|снимает\s+парик|(?:её|его)\s+настоящее\s+имя|настоящ\w*\s+личност\w*)',
    re.IGNORECASE,
)


def _norm_hair_token(raw: str) -> str:
    """Map a detected hair-state word to a canonical EN hair phrase head."""
    if not raw:
        return ''
    low = raw.strip().lower().replace(' ', '-')
    if low in _ID_HAIR_TOKENS:
        return _ID_HAIR_TOKENS[low]
    for k, v in _ID_HAIR_TOKENS.items():
        if low.startswith(k):
            return v
    return ''


def _detect_identity_shift(script: str):
    """Scan a script for a narrative identity/appearance change.
    Returns {'active': bool, 'hair': '<canonical hair phrase head>', 'alias': str,
    'cue': '<raw matched text>'} or None when nothing strong is found.
    `active=False` means a RESTORE cue (disguise ends) was detected.

    Conservative: fires only on a strong signal — an explicit alias, OR a hair
    word inside a change context (now / dye / wig / new). The cue is series-
    protagonist-scoped (the caller resolves which character it belongs to)."""
    if not script:
        return None
    if _ID_RESTORE_RE.search(script):
        return {'active': False, 'hair': '', 'alias': '', 'cue': 'identity restored'}
    hair = ''
    cue = ''
    mh = _ID_HAIR_CHANGE_RE.search(script)
    if mh:
        cue = mh.group(0).strip()
        # pull the hair word from the matched change context
        mw = _ID_HAIR_WORD_RE.search(mh.group(0)) or _ID_HAIR_WORD_RE.search(script)
        if mw:
            hair = _norm_hair_token(mw.group(1))
    alias = ''
    ma = _ID_ALIAS_RE.search(script)
    if ma:
        alias = re.sub(r'\s+', ' ', ma.group(1).strip()).strip('.,;:')
        if not cue:
            cue = ma.group(0).strip()
    if not hair and not alias:
        return None
    return {'active': True, 'hair': hair, 'alias': alias, 'cue': cue[:160]}


# Strong cues that a character is staged in an undressed / transitional wardrobe
# state (post-shower, towel, robe, sleepwear, shirtless). When the script stages
# this but the character has only a formal outfit, the composer silently defaults
# to e.g. a business suit and the wardrobe FLIPS between chunks — towel in the
# beat that mentions it, suit everywhere else (Damien towel→suit, ep3 My_Affair,
# Jun 2026). There's no code fix that conjures a towel reference image, so we
# surface the cause loudly instead of shipping a silent contradiction.
_UNDRESSED_STATE_RE = re.compile(
    r'\b(?:'
    r'towel(?:ed)?\s+(?:around|round|about)|wrapped\s+in\s+a\s+towel|in\s+(?:a|his|her)\s+towel'
    r'|out\s+of\s+the\s+(?:shower|bath)|steps?\s+out\s+of\s+the\s+(?:shower|bath)'
    r'|fresh\s+from\s+the\s+shower|dripping\s+wet|just\s+showered'
    r'|shirtless|bare[-\s]chested|bare\s+chest|topless|half[-\s]naked|naked|nude|undressed'
    r'|in\s+(?:a|his|her)\s+(?:bathrobe|robe|dressing\s+gown)'
    r'|in\s+(?:her|his)\s+(?:underwear|lingerie|nightgown|nightie|slip|nightdress)'
    r'|полуголы\w*|голы[йаяе]\w*|без\s+рубашк\w*|в\s+полотенц\w*|из\s+душа|в\s+халате|в\s+нижнем\s+белье'
    r')\b',
    re.IGNORECASE,
)
# Outfit labels/descriptions that are clearly "fully dressed" — these CONTRADICT
# an undressed scene state.
_DRESSED_OUTFIT_RE = re.compile(
    r'\b(?:suit|tuxedo|business|formal|blazer|gown|dress|uniform|coat|jacket'
    r'|armou?r|cassock|kimono|sari|costume|scrubs)\b', re.IGNORECASE,
)
# Outfit labels/descriptions that ALREADY are an undressed/transitional state —
# no mismatch to flag.
_UNDRESSED_OUTFIT_RE = re.compile(
    r'\b(?:towel|post[-\s]?shower|shower|shirtless|bare|naked|nude|robe|bathrobe'
    r'|sleepwear|pyjamas|pajamas|nightgown|nightie|lingerie|underwear|swimsuit|bikini|trunks)\b',
    re.IGNORECASE,
)


def _detect_char_undressed_states(script: str, characters: list) -> dict:
    """Return {char_id: matched_cue} for characters the script stages in an
    undressed/transitional wardrobe state. A cue counts only when it appears in
    the SAME sentence/line as the character's name, so it binds to the right
    character and doesn't bleed across the scene."""
    out = {}
    if not script or not characters:
        return out
    units = re.split(r'(?<=[.!?\n])\s+', script)
    for unit in units:
        if not _UNDRESSED_STATE_RE.search(unit):
            continue
        m = _UNDRESSED_STATE_RE.search(unit)
        for c in characters:
            name = (c.get('name') or '').strip()
            # lazy: cast helpers live higher (falls back to app.py until extracted)
            try:
                from sw.cast import _char_name_in_text
            except ImportError:
                from app import _char_name_in_text
            if name and _char_name_in_text(name, unit, c.get('aliases')):
                out.setdefault(c.get('id'), m.group(0))
    return out


def _undress_state_clothing(cue: str) -> str:
    """Map a detected undress cue to a concrete wardrobe phrase for BINDING.
    Used to OVERRIDE a contradicting catalogued outfit (e.g. a business suit) so
    Seedance renders the transitional state the script staged — and holds it
    across every chunk of the scene span instead of flipping back to the suit
    on a dialogue-only chunk (Damien towel→suit, ep3)."""
    cl = (cue or '').lower()
    def has(*ws):
        return any(w in cl for w in ws)
    if has('towel', 'полотенц', 'shower', 'душ', 'bath', 'dripping', 'showered'):
        return 'wrapped in a white bath towel around the waist, bare chest, skin still damp, hair wet'
    if has('robe', 'халат', 'dressing gown', 'gown'):
        return 'wearing a loose open bathrobe, loosely tied'
    if has('lingerie', 'underwear', 'бель', 'slip', 'night'):
        return 'in plain sleepwear'
    if has('shirtless', 'bare', 'topless', 'chest', 'голы', 'рубашк'):
        return 'shirtless, bare chest'
    if has('naked', 'nude', 'undressed'):
        return 'undressed, bare shoulders (framed modestly above the chest)'
    return 'in a post-shower state of undress (no formal clothing)'


def _binding_desc_with_undress(s, char_id, cue: str) -> str:
    """Canonical BINDING description for a character the script stages undressed:
    the PERSON part (face / build / hair, all clothing stripped) + the undress
    wardrobe phrase. Same shape as `_canonical_char_description` but the clothing
    is the scene's transient state, not a catalogued outfit. The reference PHOTO
    still anchors the face; this text drives the wardrobe."""
    ch = next((c for c in (s.get('characters') or []) if c['id'] == char_id), None)
    if not ch:
        return ''
    app = (ch.get('appearance') or '').strip()
    app = _strip_vague_clothing_tail(app)
    app = _strip_concrete_clothing(app)
    app = re.sub(r'\s+', ' ', app).strip().rstrip('.,;')
    phrase = _undress_state_clothing(cue)
    parts = [p for p in (app, phrase) if p]
    return '; '.join(parts)[:300]


# ── Moderation-safe APPEARANCE sanitizer ─────────────────────────────────────
# A character's `appearance` text is attached to EVERY downstream image/video
# prompt (see _canonical_char_description → Seedance BINDING, and the inline
# image-gen prompts). A user regeneration wish like «сделай красивой и
# сексуальной» — or even «голой» — must influence ONLY the one-off render (it
# rides in via the separate image_constraints/constraints_clause), and must
# NEVER leak into the persisted description, otherwise sexualized descriptors
# ride along into all 70 episodes' prompts and the provider's content filter
# rejects everything. (Root cause of the «My Boss Wants Me To Sleep With Him…»
# clone where every shot went to moderation: the clone revision rewrote Emma's
# appearance to «…чувственными губами; стройная фигура…».)
#
# This scrubber is the hard, LLM-independent guarantee. Run it (1) on any
# user-influenced appearance BEFORE persisting, and (2) at the BINDING
# chokepoint, so legacy/already-polluted descriptions get cleaned in-flight too.
# It rewrites sexualizing adjectives to neutral synonyms and strips explicit
# nudity / sexual-act terms — it never sexualizes, only de-escalates, so a face
# that's meant to read as attractive still does (via the surviving neutral
# descriptors), it just stops tripping moderation.
_APPEARANCE_ADJ_MAP_RU = [
    (r'сексуальн', 'привлекательн'), (r'чувственн', 'выразительн'),
    (r'соблазнительн', 'привлекательн'), (r'обольстительн', 'привлекательн'),
    (r'эротичн', 'элегантн'), (r'эротическ', 'элегантн'), (r'развратн', 'элегантн'),
    (r'распутн', 'элегантн'), (r'похотлив', 'спокойн'), (r'вызывающ', 'элегантн'),
    (r'пышногруд', 'стройн'), (r'грудаст', 'стройн'), (r'сладострастн', 'спокойн'),
    (r'откровенн', 'элегантн'),
]
_APPEARANCE_ADJ_MAP_EN = {
    'sexy': 'attractive', 'sexual': 'elegant', 'seductive': 'graceful', 'sensual': 'soft',
    'sensuous': 'soft', 'erotic': 'elegant', 'provocative': 'elegant', 'sultry': 'calm',
    'alluring': 'graceful', 'voluptuous': 'graceful', 'busty': 'slender', 'curvaceous': 'graceful',
    'raunchy': 'elegant', 'naughty': 'calm', 'kinky': 'calm', 'lustful': 'calm', 'horny': 'calm',
    'titillating': 'elegant', 'steamy': 'calm', 'skimpy': 'modest', 'revealing': 'modest',
    'scantily': 'modestly', 'plunging': 'modest',
}
_APPEARANCE_NUDE_RE = re.compile(
    r'\b(?:'
    r'nude|naked|topless|bottomless|unclothed|undressed|fully\s+exposed|stark\s+naked|'
    r'bare[\s-]?(?:breast|chest|bosom|butt|behind|ass|skin)s?|'
    r'exposed\s+(?:breast|chest|bosom|skin|body|flesh)s?|'
    r'nipples?|areola[e]?|genital(?:s|ia)?|crotch|'
    r'(?:deep\s+|low[\s-]?cut\s+|plunging\s+)?cleavage|low[\s-]?cut|'
    r'(?:deep|plunging|low)\s+neckline|d[eé]collet[aá]?ge?|'
    r'thigh[\s-]?high\s+slit|high\s+slit|see[\s-]?through|sheer\s+(?:top|fabric|dress|blouse)|'
    r'lingerie|negligee|g[\s-]?string|thong'
    r')\b',
    re.IGNORECASE,
)
_APPEARANCE_NUDE_RU_RE = re.compile(
    r'\b(?:'
    r'гол(?:ая|ый|ую|ым|ом|ой|ою|ые|ых|а|о)|нагая|нагой|нагую|нагие|'
    r'обнажённ\w*|обнаженн\w*|оголённ\w*|оголенн\w*|раздет\w*|'
    r'без\s+одежды|топлесс|соски?|сосков|декольте|'
    r'разрез\s+до\s+бедра|прозрачн\w*\s+(?:ткан\w*|плать\w*|блуз\w*|топ\w*)|'
    r'нижнее\s+бель[ёе]|стринги|пеньюар\w*'
    r')\b',
    re.IGNORECASE,
)
_APPEARANCE_SOFTEN = [
    (re.compile(r'\bbare\s+(shoulders?|legs?|midriff|stomach|thighs?|arms?)\b', re.I), r'\1'),
    (re.compile(r'\b(?:обнажённ\w*|оголённ\w*)\s+(плеч\w*|ног\w*|живот\w*|бёдр\w*|бедр\w*|рук\w*)', re.I), r'\1'),
]
_APPEARANCE_EN_ADJ_RE = re.compile(
    r'\b(' + '|'.join(_APPEARANCE_ADJ_MAP_EN.keys()) + r')\b', re.IGNORECASE)


def _sanitize_appearance_for_moderation(text):
    """Strip/neutralize sexualizing & NSFW language from a character appearance
    or BINDING string so it never trips the image/video provider's content
    filter. De-escalates only — see block comment above. Returns cleaned text;
    pass-through for empty/non-str."""
    if not text or not isinstance(text, str):
        return text
    out = text
    # 1) Soften charged body-part phrases first (so the noun survives).
    for rx, repl in _APPEARANCE_SOFTEN:
        out = rx.sub(repl, out)
    # 2) Remove explicit nudity / sexual-act terms.
    out = _APPEARANCE_NUDE_RE.sub('', out)
    out = _APPEARANCE_NUDE_RU_RE.sub('', out)
    # 3) Replace sexualizing adjectives with neutral ones (RU keeps inflection).
    for stem, repl in _APPEARANCE_ADJ_MAP_RU:
        out = re.sub(stem + r'([а-яё]*)',
                     lambda m, r=repl: r + (m.group(1) or ''), out, flags=re.IGNORECASE)
    out = _APPEARANCE_EN_ADJ_RE.sub(
        lambda m: _APPEARANCE_ADJ_MAP_EN.get(m.group(0).lower(), m.group(0)), out)
    # 4) Tidy punctuation left behind by removals.
    out = re.sub(r'\s+', ' ', out)
    out = re.sub(r'\s+([,.;])', r'\1', out)
    out = re.sub(r'([,;])(?=\S)', r'\1 ', out)
    out = re.sub(r'(?:[,;]\s*){2,}', ', ', out)
    out = re.sub(r'^[,;\s]+', '', out)
    out = re.sub(r'[,;\s]+$', '', out).strip()
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
    # Voice-only characters (e.g. someone on the other end of a phone call)
    # often have the appearance field misused by the script-writer LLM to
    # describe their ROLE ("Voice on phone delivering urgent summons...")
    # instead of physical traits. If we dump that into BINDING it confuses
    # the composer into placing them in frame. Replace with a clear off-screen
    # marker so BINDING stays declarative but signals voice-only intent.
    _VOICE_ONLY_PREFIX_RE = re.compile(
        r'^\s*(?:voice\s+(?:on|via|through|over)\s+phone|voice-?on-?phone'
        r'|off[\s\-]?screen\s+voice|voiceover|voice[\s\-]?only|via\s+phone'
        r'|on\s+the\s+phone\s+(?:from|in)|phone\s+voice|голос\s+по\s+телефону'
        r'|голос\s+за\s+кадром|закадровый\s+голос)\b',
        re.IGNORECASE,
    )
    if _VOICE_ONLY_PREFIX_RE.match(appearance):
        # Replace misused appearance with explicit off-screen voice marker.
        # Reference image still drives Seedance lipsync, but BINDING no longer
        # tells the composer to render this character in the room.
        return 'off-screen voice via phone — NOT visible in frame'
    # Vague clothing tails ("and open casual clothing.", "in everyday attire")
    # cause Seedance to render a different concrete outfit per chunk because
    # the model treats them as creative freedom rather than a constraint.
    # The specific outfit_desc set below carries the authoritative wardrobe
    # info; appearance only needs to describe the PERSON (face/build/hair).
    appearance = _strip_vague_clothing_tail(appearance)
    outfit_desc = ''
    outfits = char.get('outfits') or []
    is_base_request = (not outfit_label) or outfit_label.lower() in ('base', '')
    using_non_base_outfit = False
    active_outfit = None
    if outfit_label and not is_base_request:
        chosen = next((o for o in outfits if o.get('label') == outfit_label), None)
        if chosen:
            outfit_desc = (chosen.get('description') or '').strip()
            using_non_base_outfit = True
            active_outfit = chosen
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
            active_outfit = base
        # If no IS_BASE outfit exists, the appearance text itself usually contains
        # the base outfit description (cast-block parser embeds "wearing X" into
        # appearance when IS_BASE is set without a separate outfit entry). Trust
        # appearance as-is — don't pollute with a random outfit.
    # Whenever we have an authoritative outfit description to append, the
    # appearance must contribute ONLY the person — strip any clothing it embeds,
    # in ANY phrasing ("in a grey dress", "wearing X", "dressed in Y", "в платье").
    # Otherwise BINDING lists two outfits at once and Seedance renders a hybrid
    # (Elena grey-seamstress-dress + Dark-Cloak, Jun 2026; Claire & Lydia before
    # that). Applies to base outfits too: if a separate IS_BASE outfit_desc
    # exists, the clothing baked into `appearance` is redundant — drop it so the
    # outfit asset is the single source of wardrobe truth.
    _ = using_non_base_outfit  # historical flag; clothing strip now keys off outfit_desc
    if outfit_desc and appearance:
        appearance = _strip_concrete_clothing(appearance)
    # Alternate-identity HAIR: when the ACTIVE look deliberately changes hair
    # (disguise / dyed / wig), the single base `appearance` would otherwise force
    # the original hair into BINDING and contradict the look ("…pulled-back
    # [brunette] hair…; platinum-blonde wig" → Seedance keeps the brunette ref).
    # Swap it so the look's hair is the only hair stated. Source priority:
    #   1) explicit outfit `appearance_override` ("platinum-blonde hair") → REPLACE
    #      the base hair clause with it;
    #   2) hair words already inside the outfit description → STRIP the base hair
    #      clause (the description supplies the hair, so BINDING isn't doubled).
    if appearance:
        _hair_override = (active_outfit.get('appearance_override') or '').strip() if active_outfit else ''
        if _hair_override:
            appearance = _override_hair_in_appearance(appearance, _hair_override)
        elif outfit_desc and _OUTFIT_HAIR_RE.search(outfit_desc):
            appearance = _override_hair_in_appearance(appearance, '')
    # ── BINDING REDUCTION — the reference portrait carries identity ───────────
    # Seedance is a reference model: the character's portrait (@ImageN) already
    # encodes face, hair and build. Repeating facial/figure prose ("beautiful …
    # sensual lips, slim figure") in the BINDING is redundant for likeness AND
    # it is exactly what the content filter reads — next to intimate/forceful
    # staging it tips an IDENTICAL scene from "drama" into "sexual content" and
    # the output gets moderated. (Verified: the «My Boss…» clone failed on the
    # same kiss/closet beats where the original — whose Emma binding was only
    # "exhausted woman + wardrobe" — passed. The single difference was the
    # facial/figure prose in the clone's binding.) So once a ref portrait exists
    # AND we have an authoritative outfit, drop the appearance prose and let the
    # IMAGE carry identity; keep only wardrobe + anchors the image can't
    # disambiguate on its own (anthro species, a disguise wig that differs from
    # the base ref). The full appearance text is still used verbatim when the
    # PORTRAIT itself is generated — that path doesn't go through here.
    has_ref_portrait = bool(char.get('avai_base_url') or char.get('ref_images'))
    if has_ref_portrait and outfit_desc:
        anchors = []
        # lazy: anthro detection lives higher (falls back to app.py until extracted)
        try:
            from sw.anthro import _detect_animal_species
        except ImportError:
            from app import _detect_animal_species
        _species = _detect_animal_species(char.get('name'), char.get('appearance') or '', series=s)
        if _species:
            anchors.append(f'anthropomorphic {_species.split()[-1]}')
        _disguise_hair = (active_outfit.get('appearance_override') or '').strip() if active_outfit else ''
        if _disguise_hair:
            anchors.append(_disguise_hair)
        appearance = ', '.join(anchors)
    parts = [p for p in (appearance, outfit_desc) if p]
    full = '; '.join(parts)
    # Strip stray newlines and cap length to keep BINDING manageable
    full = re.sub(r'\s+', ' ', full).strip()
    # Safety net: if a clothing phrasing slipped past the stripper, surface it
    # in the log instead of silently shipping a double-wardrobe BINDING.
    if outfit_desc and appearance and re.search(r'\b' + _GARMENT_NOUN + r'\b', appearance, re.IGNORECASE):
        try:
            _log_event('WARN', 'binding_residual_clothing',
                       char_id=char_id, outfit=(outfit_label or ''),
                       residual=appearance[:160])
        except Exception:
            pass
    # Final hard guarantee: scrub sexualizing / NSFW language so no BINDING line
    # ever trips the provider's content filter — even if a legacy appearance or
    # an outfit description still carries charged terms. De-escalates only.
    full = _sanitize_appearance_for_moderation(full)
    return full[:300]


