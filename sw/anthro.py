"""Anthropomorphic-animal detection (character and series level) and
script-pose extraction for the Vision-override guard."""
import json
import re

from sw.jsonutils import loads_lenient, strip_json
from sw.llm import _resolve_writer_model, claude_ask, llm_ask

# ── Anthropomorphic-animal detection (shared) ────────────────────────────────
# Real production bug: «The Fox CEO's Trap» — script extraction wrote
# appearance "A tired man in worn-out clothes" for a character named "Wolf",
# "A man holding interview papers" for "Hyena", etc. Pixar style + "a man"
# prefix → image gen produced regular humans. Animal nature ONLY readable
# from the name, not from appearance. Detect species from the name and
# override "a man/woman" framing with explicit anthropomorphic species hint.
_ANIMAL_SPECIES = {
    # canonical singular → species hint phrase
    'fox':       'anthropomorphic fox',
    'vixen':     'anthropomorphic fox',
    'wolf':      'anthropomorphic wolf',
    'bear':      'anthropomorphic bear',
    'hyena':     'anthropomorphic hyena',
    'raccoon':   'anthropomorphic raccoon',
    'lion':      'anthropomorphic lion',
    'lioness':   'anthropomorphic lioness',
    'tiger':     'anthropomorphic tiger',
    'cat':       'anthropomorphic cat',
    'kitten':    'anthropomorphic kitten',
    'dog':       'anthropomorphic dog',
    'pup':       'anthropomorphic puppy',
    'puppy':     'anthropomorphic puppy',
    'rabbit':    'anthropomorphic rabbit',
    'bunny':     'anthropomorphic rabbit',
    'hare':      'anthropomorphic hare',
    'rat':       'anthropomorphic rat',
    'mouse':     'anthropomorphic mouse',
    'mice':      'anthropomorphic mouse',
    'deer':      'anthropomorphic deer',
    'fawn':      'anthropomorphic fawn',
    'stag':      'anthropomorphic stag',
    'doe':       'anthropomorphic doe',
    'horse':     'anthropomorphic horse',
    'pony':      'anthropomorphic pony',
    'sheep':     'anthropomorphic sheep',
    'lamb':      'anthropomorphic lamb',
    'goat':      'anthropomorphic goat',
    'cow':       'anthropomorphic cow',
    'bull':      'anthropomorphic bull',
    'pig':       'anthropomorphic pig',
    'boar':      'anthropomorphic boar',
    'panda':     'anthropomorphic panda',
    'koala':     'anthropomorphic koala',
    'sloth':     'anthropomorphic sloth',
    'monkey':    'anthropomorphic monkey',
    'ape':       'anthropomorphic ape',
    'gorilla':   'anthropomorphic gorilla',
    'elephant':  'anthropomorphic elephant',
    'rhino':     'anthropomorphic rhinoceros',
    'rhinoceros':'anthropomorphic rhinoceros',
    'hippo':     'anthropomorphic hippopotamus',
    'hippopotamus':'anthropomorphic hippopotamus',
    'giraffe':   'anthropomorphic giraffe',
    'zebra':     'anthropomorphic zebra',
    'camel':     'anthropomorphic camel',
    'crocodile': 'anthropomorphic crocodile',
    'alligator': 'anthropomorphic alligator',
    'lizard':    'anthropomorphic lizard',
    'frog':      'anthropomorphic frog',
    'penguin':   'anthropomorphic penguin',
    'bat':       'anthropomorphic bat',
    'skunk':     'anthropomorphic skunk',
    'hedgehog':  'anthropomorphic hedgehog',
    'mole':      'anthropomorphic mole',
    'badger':    'anthropomorphic badger',
    'weasel':    'anthropomorphic weasel',
    'ferret':    'anthropomorphic ferret',
    'otter':     'anthropomorphic otter',
    'beaver':    'anthropomorphic beaver',
    'squirrel':  'anthropomorphic squirrel',
    'cheetah':   'anthropomorphic cheetah',
    'leopard':   'anthropomorphic leopard',
    'jaguar':    'anthropomorphic jaguar',
    'panther':   'anthropomorphic panther',
    'lynx':      'anthropomorphic lynx',
    'bobcat':    'anthropomorphic bobcat',
    'coyote':    'anthropomorphic coyote',
    'jackal':    'anthropomorphic jackal',
    'bird':      'anthropomorphic bird',
    'hawk':      'anthropomorphic hawk',
    'eagle':     'anthropomorphic eagle',
    'owl':       'anthropomorphic owl',
    'snake':     'anthropomorphic snake',
    'shark':     'anthropomorphic shark',
    'whale':     'anthropomorphic whale',
    'dolphin':   'anthropomorphic dolphin',
    'donkey':    'anthropomorphic donkey',
    'mule':      'anthropomorphic mule',
    'buffalo':   'anthropomorphic buffalo',
    'bison':     'anthropomorphic bison',
    'moose':     'anthropomorphic moose',
    'elk':       'anthropomorphic elk',
}

# Species tokens that are also common human-skin-feature words. When matched
# in appearance text we must check the surrounding context — otherwise a
# description like «distinctive mole near temple» (a beauty mark on a human
# face) gets read as «this character is an anthropomorphic mole».
_AMBIGUOUS_APPEARANCE_SPECIES = {'mole'}

_SKIN_FEATURE_CONTEXT_WORDS = (
    # descriptors that almost always precede a face/body mark
    'beauty', 'birth', 'birthmark', 'small', 'tiny', 'little', 'dark',
    'distinctive', 'prominent', 'visible', 'faint', 'subtle', 'noticeable',
    # spatial/anatomical context — «mole on/near/above/below <face part>»
    'on', 'near', 'above', 'below', 'under', 'beside', 'next',
    'cheek', 'cheeks', 'temple', 'temples', 'chin', 'jaw', 'jawline',
    'lip', 'lips', 'mouth', 'nose', 'brow', 'eyebrow', 'eye', 'eyes',
    'eyelid', 'forehead', 'neck', 'ear', 'ears', 'face', 'hairline',
    'shoulder', 'collarbone', 'wrist', 'hand',
)


def _is_skin_feature_context(low, start, end, term):
    """Return True if `term` (at offsets [start,end) within `low`) is being
    used as a human-skin-feature word rather than the species name.

    Currently only `mole` is treated as ambiguous — it's both a burrowing
    mammal and the standard English word for a small dark skin mark, and
    short-drama character descriptions overwhelmingly use it in the latter
    sense («distinctive mole near temple», «small mole above her lip»).
    """
    if term not in _AMBIGUOUS_APPEARANCE_SPECIES:
        return False
    window_start = max(0, start - 40)
    window_end = min(len(low), end + 40)
    window = low[window_start:window_end]
    neighbour_tokens = re.findall(r"[a-zа-яё]+", window)
    for tok in neighbour_tokens:
        if tok == term:
            continue
        if tok in _SKIN_FEATURE_CONTEXT_WORDS:
            return True
    return False


# Generic anthro-anatomy markers that double as everyday HUMAN/clothing words:
#   • fur / feathers → garment material or trim (a noble's fur-trimmed cloak,
#     a feathered cap), not the animal's own pelt/plumage.
#   • mane → a human's thick «mane of hair», not a lion's mane.
# Without context filtering these silently flip human characters into beasts.
# (2026-06-09 incident: Lord Calder — a human noble in a crimson velvet cloak
#  «trimmed with fur» — rendered as an anthropomorphic animal because the bare
#  `\bfur\b` marker fired.) Mirrors the `mole` homograph guard above.
_NONANATOMICAL_MARKER_CONTEXT = {
    'fur': (
        'trim', 'trimmed', 'trimming', 'lined', 'lining', 'collar', 'collared',
        'cloak', 'cloaks', 'coat', 'coats', 'hat', 'cap', 'hood', 'hooded',
        'cuff', 'cuffs', 'stole', 'mantle', 'robe', 'robes', 'cape', 'capes',
        'jacket', 'shawl', 'wrap', 'muff', 'scarf', 'edged', 'edging',
        'velvet', 'wool', 'silk', 'leather', 'garment', 'garments', 'sleeve',
        'sleeves', 'hem', 'lapel', 'lapels', 'boots', 'gloves',
    ),
    'feathers': (
        'hat', 'cap', 'feathered', 'plume', 'plumed', 'headdress', 'brooch',
        'fan', 'quill', 'trim', 'trimmed', 'collar', 'cloak', 'hood',
        'wearing', 'pinned', 'adorned',
    ),
    'mane': (
        'hair', 'curls', 'curly', 'locks', 'waves', 'wavy', 'braided',
        'braids', 'braid', 'ponytail', 'tresses', 'flowing',
    ),
}


def _is_nonanatomical_marker_context(low, start, end, term):
    """Return True when an ambiguous anthro-anatomy marker (`fur`, `feathers`,
    `mane`) at offsets [start,end) within `low` is being used as a
    human/clothing descriptor rather than animal anatomy — e.g.
    «cloak trimmed with fur», «feathered cap», «mane of dark hair».
    Mirrors `_is_skin_feature_context` (the `mole` homograph guard)."""
    ctx = _NONANATOMICAL_MARKER_CONTEXT.get(term)
    if not ctx:
        return False
    window_start = max(0, start - 40)
    window_end = min(len(low), end + 40)
    window = low[window_start:window_end]
    for tok in re.findall(r"[a-zа-яё]+", window):
        if tok == term:
            continue
        if tok in ctx:
            return True
    return False


def _detect_animal_species_raw(name, appearance=None):
    """Detect anthropomorphic species from a character's name (and as a
    secondary signal, from appearance keywords). Returns the species hint
    string (e.g. "anthropomorphic fox") or '' if the character is human.

    Tolerates plurals («Bear Guards» → bear), gender qualifiers («Fox Woman»
    → fox, «Lioness» → lioness), and compound names («Mr. Wolf» → wolf).
    """
    if not name:
        return ''
    # Token-scan the name. Strip plural 's' from each token before lookup.
    raw = re.sub(r"[^A-Za-zА-Яа-яЁё ]+", ' ', name or '').lower()
    for tok in raw.split():
        # Plural normalization: «foxes» → «fox», «bears» → «bear», «wolves» → «wolf»
        candidates = {tok}
        if tok.endswith('ies') and len(tok) > 4:
            candidates.add(tok[:-3] + 'y')
        if tok.endswith('ves') and len(tok) > 4:
            candidates.add(tok[:-3] + 'f')
        if tok.endswith('es') and len(tok) > 3:
            candidates.add(tok[:-2])
        if tok.endswith('s') and len(tok) > 2:
            candidates.add(tok[:-1])
        for c in candidates:
            if c in _ANIMAL_SPECIES:
                return _ANIMAL_SPECIES[c]
    # Secondary: appearance text — first try EXACT species words (e.g. "panther"
    # in "Elegant panther in a business suit"), then generic anthropomorphic
    # markers as a last-ditch fallback.
    if appearance:
        low = appearance.lower()
        # Scan for exact species name as a whole word.
        for sp_name, sp_hint in _ANIMAL_SPECIES.items():
            for m in re.finditer(rf'\b{re.escape(sp_name)}\b', low):
                if _is_skin_feature_context(low, m.start(), m.end(), sp_name):
                    # «distinctive mole near temple» — homograph (mole = beauty
                    # mark, not the burrowing animal). 2026-06-04 incident:
                    # Ethan Morgan rendered as anthropomorphic mole in a hoodie
                    # because his appearance string described a face mole.
                    continue
                return sp_hint
        # Generic anthropomorphic markers — word-boundary match REQUIRED.
        # Substring `in` was matching 'tail' inside 'tailcoat' (a stage
        # magician costume!) and 'mane' inside 'manage'/'maneuver' — every
        # gentleman-in-tailcoat got flagged as anthropomorphic animal and
        # the series-level detector then flipped to «furry universe» on
        # 2+ such hits, producing deer/wolf companions for human leads.
        # (2026-05-30 incident: «I Became My Dead Brother's Wife's Assistant»)
        for w in ('fur', 'muzzle', 'snout', 'tail', 'paws',
                  'claws', 'whiskers', 'mane', 'feathers',
                  'beak', 'fang', 'fangs'):
            for m in re.finditer(rf'\b{re.escape(w)}\b', low):
                if _is_nonanatomical_marker_context(low, m.start(), m.end(), w):
                    # «cloak trimmed with fur», «feathered cap», «mane of hair»
                    # — clothing/hair, not animal anatomy. Keep scanning in case
                    # a later occurrence IS anatomical.
                    continue
                return 'anthropomorphic animal'
    return ''




class AnthroPermissionRequired(Exception):
    """A generation would render a NON-human character in a series that has not
    granted non-human rendering. Carries the flagged character names."""
    def __init__(self, chars):
        self.chars = list(chars or [])
        super().__init__('anthro permission required for: ' + ', '.join(self.chars))


def _anthro_unlocked(series) -> bool:
    """True iff the user EXPLICITLY granted non-human (anthro/furry) rendering
    for this series (anthro_choice == 'anthro'). This is the ONLY state in which
    a species hint may enter a generation prompt. Everything else is default-deny."""
    return isinstance(series, dict) and (series.get('anthro_choice') or '').strip().lower() == 'anthro'


def _anthro_decided(series) -> bool:
    """True iff the user made an explicit per-series decision either way:
    'human'/'none' (humans only) or 'anthro' (non-humans allowed). 'auto'/unset
    is UNDECIDED — we must ask before rendering any non-human."""
    if not isinstance(series, dict):
        return False
    choice = (series.get('anthro_choice') or 'auto').strip().lower()
    return choice in ('human', 'none', 'anthro')


def _detect_animal_species(name, appearance=None, series=None):
    """GATED species detector — the single chokepoint deciding whether a
    non-human species hint may enter a generation prompt.

    Hard rule: a non-human renders ONLY when the series explicitly granted it
    (anthro_choice == 'anthro'). Undecided, 'human', or no series context at all
    -> returns '' so the character renders as a HUMAN. The fail-safe direction is
    always "human", never an unauthorized animal. The raw, ungated signal lives in
    `_detect_animal_species_raw` and is used only to decide whether to ASK the user."""
    raw = _detect_animal_species_raw(name, appearance)
    if not raw:
        return ''
    return raw if _anthro_unlocked(series) else ''


def _anthro_preflight(series, chars):
    """Pre-flight gate for generation entry points. Returns (needs_decision,
    flagged_names). needs_decision is True when >=1 character trips the raw
    non-human detector AND the series is still undecided — caller must STOP and
    ask. Once the user has chosen (human or anthro) it returns False and never
    blocks again."""
    flagged = [(c.get('name') or c.get('id') or '?')
               for c in (chars or [])
               if _detect_animal_species_raw(c.get('name'), c.get('appearance'))]
    if _anthro_decided(series):
        return (False, flagged)
    return (bool(flagged), flagged)

# ── Anthro-world detection (series-level) ──────────────────────────────────
# Real bug: series "The Landlord's Daughter" — synopsis explicitly described
# a furry world ("seamstress rabbit Sofia... panther property manager") but
# genre was "Revenge Drama / Class Warfare" with no furry/anthro keyword.
# Result: writer DID render Victoria as panther (caught the word) but
# Sofia/Marcus/Anita got generic-human appearance with no species → portraits
# rendered as humans. _detect_animal_species can't help — it only reads one
# character's own name+appearance. We need a SERIES-level signal that says
# "this world is anthropomorphic — every character must carry a species."

_ANTHRO_WORLD_KEYWORDS = (
    'furry', 'фури', 'фурри', 'anthropomorphic', 'anthro',
    'zootopia', 'beastars', 'bojack', 'redwall', 'anthropomorph',
)


def _detect_anthro_world_raw(s) -> dict:
    """Pure detection — returns the raw signals without consulting the user's
    explicit choice. Used by `_is_anthro_world` and by the UI confirmation
    banner. Returns:
      {
        'anthro': bool,             # would this world be classified anthro?
        'keyword': str,             # explicit keyword found, or ''
        'blob_species_count': int,  # species mentions in title/world/synopsis
        'char_species_count': int,  # characters carrying a species
        'evidence': list[str],      # human-readable explanation, max 4 items
      }
    """
    out = {'anthro': False, 'keyword': '', 'blob_species_count': 0,
           'char_species_count': 0, 'evidence': []}
    if not isinstance(s, dict):
        return out
    text_blob = ' '.join(str(s.get(k) or '') for k in (
        'title', 'genre', 'tone', 'world_description', 'synopsis',
        'target_audience',
    )).lower()
    for kw in _ANTHRO_WORLD_KEYWORDS:
        if re.search(rf'\b{re.escape(kw)}\b', text_blob):
            out['keyword'] = kw
            out['anthro'] = True
            out['evidence'].append(f"ключевое слово «{kw}» в описании сериала")
            break
    char_hits = []
    for c in (s.get('characters') or []):
        sp = _detect_animal_species_raw(c.get('name'), c.get('appearance'))
        if sp:
            out['char_species_count'] += 1
            if len(char_hits) < 3:
                char_hits.append(f"{c.get('name','?')} = {sp.replace('anthropomorphic ','')}")
    if char_hits:
        out['evidence'].append('персонажи: ' + ', '.join(char_hits))
    blob_species_hits = []
    for sp in _ANIMAL_SPECIES.keys():
        if re.search(rf'\b{re.escape(sp)}\b', text_blob):
            out['blob_species_count'] += 1
            if len(blob_species_hits) < 3:
                blob_species_hits.append(sp)
    if blob_species_hits:
        out['evidence'].append('виды животных в описании: ' + ', '.join(blob_species_hits))
    if out['blob_species_count'] >= 2:
        out['anthro'] = True
    elif out['char_species_count'] >= 1 and out['blob_species_count'] >= 1:
        out['anthro'] = True
    elif out['char_species_count'] >= 2:
        out['anthro'] = True
    return out


def _is_anthro_world(s) -> bool:
    """Return True when the series lives in an anthropomorphic-animal world.

    Respects the user's explicit choice stored on the series:
      • anthro_choice='human' (or 'none')   → False (always human)
      • anthro_choice='anthro'              → True (always furry)
      • anthro_choice='auto'/missing/'pending':
          – run detection;
          – if detector says NOT anthro → False
          – if detector says anthro AND `anthro_confirmed`=True → True
          – if detector says anthro AND NOT confirmed → False (safe default;
            UI surfaces a confirmation banner). Without this gate, a single
            substring bug (e.g. `'tail' in 'tailcoat'`) tagged every magician
            in a tailcoat as anthropomorphic and propagated furry features
            to side characters via the LLM species inferrer."""
    if not isinstance(s, dict):
        return False
    choice = (s.get('anthro_choice') or 'auto').strip().lower()
    if choice in ('human', 'none'):
        return False
    if choice == 'anthro':
        return True
    raw = _detect_anthro_world_raw(s)
    if not raw['anthro']:
        return False
    if not s.get('anthro_confirmed'):
        return False  # gated — wait for the user
    return True


def _anthro_world_block(s) -> str:
    """Directive block for LLM prompts when the series is anthro. Empty for
    human-world series — safe to concat unconditionally."""
    if not _is_anthro_world(s):
        return ''
    known = []
    for c in (s.get('characters') or []):
        sp = _detect_animal_species_raw(c.get('name'), c.get('appearance'))
        if sp:
            known.append(f"{c.get('name','')} = {sp}")
    known_clause = (
        f"Known character species (REUSE these exactly): {'; '.join(known)}.\n"
        if known else ''
    )
    return (
        "═══ WORLD CONVENTION — ANTHROPOMORPHIC ANIMAL UNIVERSE ═══\n"
        "Every character in this series is an ANTHROPOMORPHIC ANIMAL — NOT a human. "
        "Zootopia/Beastars/Bojack style: walks upright, talks, wears human clothing, "
        "but has the head/face/fur/tail of their species.\n"
        "MANDATORY for EVERY character description / appearance / LOOK field:\n"
        "  • Start with the SPECIES (e.g. 'anthropomorphic rabbit female...', "
        "    'anthropomorphic bear male...', 'female panther in business suit...').\n"
        "  • Replace human anatomy words (hair, eyes, skin) with anatomy that "
        "    matches the species — fur color and pattern, snout/muzzle, ears, tail, "
        "    paws, claws, whiskers, mane, feathers, scales, etc.\n"
        "  • Clothing stays human-style (suits, dresses, uniforms, hoodies).\n"
        "  • Species choice should fit role/social position when not already specified.\n"
        "FORBIDDEN: 'a man', 'a woman', 'human male/female', generic human facial "
        "descriptors (skin tone, hair color without fur context). "
        "If you write 'a young woman in a hoodie' for this world — STOP and rewrite "
        "as 'an anthropomorphic <species> female in a hoodie'.\n"
        + known_clause +
        "═══════════════════════════════════════════════\n\n"
    )


def _casting_aesthetics_block(s) -> str:
    """Directive injected into EVERY character-appearance generation prompt so the
    writer casts looks by NARRATIVE ROLE instead of describing people at random.

    The core problem this fixes: short-drama hooks revolve around desire — a boss
    who pursues the heroine, a forbidden affair, a seduction, an implied bed scene.
    If the appearance generator hands the heroine a plain/aging look, the viewer
    has no reason to want the romance, and the whole hook collapses. So any
    character the plot frames as desirable — and the leads especially — must read
    as genuinely attractive and age-appropriate to that role.

    Works for human AND anthropomorphic worlds (attractiveness is expressed in
    species-appropriate terms when the anthro convention is active — it composes
    with _anthro_world_block, it does not override it). Always on."""
    anthro = _is_anthro_world(s)
    species_note = (
        " Express attractiveness in SPECIES-APPROPRIATE terms (sleek fur, striking "
        "markings, youthful muzzle, well-groomed) — never with human features.\n"
        if anthro else "\n"
    )
    return (
        "═══ CASTING & APPEARANCE AESTHETICS (MANDATORY) ═══\n"
        "Cast each character's LOOK from their ROLE in the story, not at random:\n"
        "  1. First infer the narrative role of every character from the synopsis, "
        "genre and the script: protagonist (главный герой/героиня), love interest / "
        "romantic lead, object of desire or seduction, anyone in or implied to be in "
        "an intimate / bedroom / flirtation / 'who-they-sleep-with' storyline, "
        "antagonist, supporting, background.\n"
        "  2. The PROTAGONIST and any LOVE INTEREST / object of desire / character "
        "involved in (or implied toward) romance, seduction, intimacy or a bed scene "
        "MUST be described as genuinely ATTRACTIVE and YOUNG-to-PRIME age for that "
        "role (typically 20s–early 30s unless the plot explicitly demands otherwise). "
        "Give them specific flattering, desirable features so the AI image renders "
        "someone the audience would believe in as a romantic lead — NOT plain, NOT "
        "frumpy, NOT aged-up. This is the single most important rule: if the plot "
        "hints that someone is desired, pursued, seduced, or shares a bed, that "
        "person reads as beautiful/handsome." + species_note +
        "  3. Lean attractive for MAIN characters in general (this is glossy short "
        "drama, not gritty realism) while keeping looks grounded and believable — "
        "specific and real, never plastic caricature or a list of clichés.\n"
        "  4. Age must be CONSISTENT with the plot: do not make someone 20 if the "
        "story has them with decades of backstory, an adult child, or a long-ago "
        "relationship. Pick the youngest attractive age the plot actually allows.\n"
        "  5. Antagonists, rivals and 'the other woman/man' are usually attractive "
        "too (the threat is part of the drama) unless the script paints them "
        "otherwise. Genuinely old/plain/unglamorous looks are reserved for roles the "
        "plot truly requires them for (an elderly grandparent, a frail patient, a "
        "comic side character) — never for a romantic lead.\n"
        "═══════════════════════════════════════════════\n\n"
    )


def _revision_instructions_block(s) -> str:
    """Directive injected into episode-script + synopsis generation when this
    series was cloned from another one WITH revision instructions (e.g. «главная
    героиня молодая и красивая», or a plot change). Empty when the series carries
    no revision instructions — safe to concat unconditionally."""
    ri = (s.get('revision_instructions') or '').strip() if isinstance(s, dict) else ''
    if not ri:
        return ''
    src = (s.get('cloned_from') or '').strip()
    src_clause = f' (this series is a revised clone of «{src}»)' if src else ''
    return (
        "═══ SERIES REVISION INSTRUCTIONS — APPLY THROUGHOUT (HIGH PRIORITY) ═══\n"
        f"The user adapted this series{src_clause} with the following changes. They "
        "OVERRIDE the inherited synopsis / cast / prior scripts wherever they "
        "conflict. Honour them in everything you write:\n"
        f"{ri}\n"
        "Keep everything else faithful to the original story. Do not let these "
        "revisions silently drift the plot beyond what they ask for, and keep the "
        "world internally consistent (ages, timelines, who-knew-whom-when must "
        "still add up after the change).\n"
        "═══════════════════════════════════════════════\n\n"
    )


def _llm_apply_revisions_to_bible(s, revision_instructions: str) -> dict:
    """One LLM pass that rewrites the series bible + cast to apply the user's
    clone-time revision instructions. Returns a parsed dict (see schema below) or
    {} on failure. Pure read — caller mutates the series and persists."""
    ri = (revision_instructions or '').strip()
    if not ri:
        return {}
    cast = []
    for c in (s.get('characters') or []):
        cast.append({
            'id': c.get('id'),
            'name': c.get('name', ''),
            'gender': c.get('gender', ''),
            'appearance': (c.get('appearance') or '')[:400],
            'description': (c.get('description') or '')[:300],
        })
    system = (
        "You revise a short-drama series bible and its cast to apply the user's "
        "revision instructions. Return STRICT JSON only — no prose, no markdown.\n\n"
        "Schema:\n"
        "{\n"
        '  "bible": {"genre":"...","tone":"...","world_description":"...","synopsis":"...","arc":"..."},\n'
        '  "characters": [{"id":"<existing id>","name":"...","name_changed":false,'
        '"appearance":"<full RU description>","appearance_changed":false,"age_changed":false,'
        '"gender":"male|female","description":"<RU>"}],\n'
        '  "revision_scope": "character_only" | "plot",\n'
        '  "rewrite_reason": "<1 sentence RU: why episode scripts may need rewriting, or \'none\'>",\n'
        '  "renames": [{"old":"OldName","new":"NewName"}]\n'
        "}\n\n"
        "Rules:\n"
        "- bible: echo each field unchanged UNLESS the instructions require a plot/world/tone change. "
        "Keep the same language as the source.\n"
        "- characters: return EVERY character from the cast (keep the same id). Update only what the "
        "instructions require. Set appearance_changed=true when you rewrote the appearance, "
        "name_changed=true when you renamed, age_changed=true when the character's age moved.\n"
        "- A full new `appearance` sentence is required whenever appearance_changed=true (it REPLACES "
        "the old one — describe the whole look, not just the delta).\n"
        "- revision_scope: 'character_only' if the changes only touch how characters look / their names / "
        "ages and nothing in the plotline itself changes; 'plot' if the storyline, relationships or events "
        "change (then episode scripts will need rewriting).\n"
        "- renames: one entry per renamed character (old → new exact tokens) so scripts can be updated.\n"
        "- OBEY the CASTING & APPEARANCE AESTHETICS block: leads and any romance/seduction/intimacy role "
        "must read as attractive and age-appropriate.\n"
        "- The `appearance` field is attached to EVERY image/video prompt, so it MUST be moderation-safe: "
        "convey attractiveness with NEUTRAL words (elegant, graceful, soft features, slim, striking eyes) "
        "and NEVER use sexual / explicit / nudity wording — no «сексуальная», «чувственная», «соблазнительная», "
        "«голая», «декольте», no 'sexy', 'sensual', 'seductive', 'cleavage', 'nude', 'lingerie'. If an "
        "instruction asks to make a character 'sexual'/'nude', render that intent ONLY as tasteful "
        "attractiveness in this field — the explicit part belongs to image constraints, not the stored description."
    )
    anthro_block = _anthro_world_block(s)
    casting_block = _casting_aesthetics_block(s)
    user = (
        f'SERIES TITLE: {s.get("title","")}\n'
        f'GENRE: {s.get("genre","")}\nTONE: {s.get("tone","")}\n'
        f'WORLD: {(s.get("world_description") or "")[:1200]}\n'
        f'SYNOPSIS: {(s.get("synopsis") or "")[:2000]}\n'
        f'ARC: {(s.get("arc") or "")[:1200]}\n\n'
        + anthro_block
        + casting_block
        + f'CURRENT CAST (JSON):\n{json.dumps(cast, ensure_ascii=False)}\n\n'
        f'═══ USER REVISION INSTRUCTIONS (apply these) ═══\n{ri}\n'
        '═══════════════════════════════════════════════\n\n'
        'Return the JSON described in the system message.'
    )
    try:
        raw = llm_ask(_resolve_writer_model(None, s), user, system=system, max_tokens=4000)
        return loads_lenient(strip_json(raw)) or {}
    except Exception as e:
        print(f'[clone-revise] LLM revision pass failed: {e}', flush=True)
        return {}


def _llm_infer_species_for_char(s, char) -> str:
    """When the series IS anthro but THIS char has no species in name/appearance,
    ask LLM to infer species from synopsis + role. Returns species noun ('rabbit',
    'fox') or '' on failure. Single short Haiku call per missing character."""
    if not _is_anthro_world(s):
        return ''
    name = (char.get('name') or '').strip()
    if not name:
        return ''
    if _detect_animal_species_raw(name, char.get('appearance')):
        return ''
    known_lines = []
    for c in (s.get('characters') or []):
        sp = _detect_animal_species_raw(c.get('name'), c.get('appearance'))
        if sp:
            known_lines.append(f"  - {c.get('name','')}: {sp}")
    known_block = ('Already-assigned species in this world:\n' + '\n'.join(known_lines) + '\n\n') if known_lines else ''
    species_menu = ', '.join(sorted(_ANIMAL_SPECIES.keys()))
    prompt = (
        f"Series title: {s.get('title','')}\n"
        f"World: {s.get('world_description','')}\n"
        f"Synopsis: {s.get('synopsis','')}\n\n"
        f"{known_block}"
        f"This series lives in an anthropomorphic-animal world (Zootopia/Beastars style). "
        f"What ANIMAL SPECIES is the character named '{name}' (role: {(char.get('description') or '—')[:200]}; "
        f"current appearance: {(char.get('appearance') or '—')[:200]})?\n\n"
        f"Pick ONE word from this list (or propose another common-English animal noun if a clearer fit): "
        f"{species_menu}\n\n"
        "Rules:\n"
        "- If the synopsis EXPLICITLY assigns this character a species, return that species verbatim.\n"
        "- Otherwise pick a species that fits the character's role/temperament and avoids collision with already-assigned species above.\n"
        "- Return STRICT JSON only: {\"species\": \"<one lowercase animal noun>\"}.\n"
    )
    try:
        raw = claude_ask(prompt, system='You return one-word animal species in strict JSON.', model='claude-haiku-4-5', max_tokens=80)
        data = loads_lenient(strip_json(raw))
        sp = (data.get('species') or '').strip().lower()
        sp = re.sub(r'[^a-z\- ]', '', sp).strip()
        return sp
    except Exception as e:
        print(f'[anthro-infer] failed for {name}: {e}', flush=True)
        return ''


def _patch_appearance_with_species(appearance: str, species: str, gender: str = '') -> str:
    """Rewrite a human-coded appearance so it starts with the species marker.
    Idempotent — if species already present, returns unchanged."""
    if not species:
        return appearance or ''
    base = (appearance or '').strip()
    low = base.lower()
    if species.lower() in low or 'anthropomorphic' in low:
        return base
    gword = 'female' if gender == 'female' else ('male' if gender == 'male' else '')
    prefix = f"Anthropomorphic {species}"
    if gword:
        prefix += f" {gword}"
    if not base:
        return prefix
    base = re.sub(r'^\s*(A|An)\s+(young\s+|middle-aged\s+|older\s+)?(man|woman|guy|girl|male|female|gentleman|lady)\b[,\.]?\s*',
                  '', base, flags=re.IGNORECASE)
    return f"{prefix}. {base}"


# ── Script-pose extraction (Vision-override guard) ──────────────────────────
# Pose verbs we recognise in chunk_text. Each maps char-name-appearance → the
# canonical pose label that Vision uses, plus a short script-clip we'll embed
# in the override so the composer LLM (and downstream Seedance) sees WHY we
# overrode and what the exact scripted pose is. Patterns are intentionally
# loose — script writing is messy.
_SCRIPT_POSE_PATTERNS = [
    # (regex with optional named groups «prep» + «obj» for location capture,
    #  pose label). When prep+obj matched, override layer also rewrites the
    #  «где=» field on the Vision line, not just «поза=» — see user bug
    #  «Fox Woman: поза=лежит | где=рядом с машиной» where pose was correct
    #  but location field still said «next to» instead of «under».
    (r'\b(?:lies?|lying|lay|laid)\s+(?:halfway\s+)?(?P<prep>under|underneath|beneath)\s+(?P<obj>(?:the|a|an)\s+[A-Za-z][A-Za-z\s\-\']{0,30}?)(?=[\s.,;!?]|$)', 'лежит'),
    (r'\b(?:lies?|lying|lay|laid)\s+(?P<prep>on top of|on|across|over)\s+(?P<obj>(?:the|a|an)\s+[A-Za-z][A-Za-z\s\-\']{0,30}?)(?=[\s.,;!?]|$)', 'лежит'),
    (r'\b(?:lies?|lying|lay|laid)\s+(?:down|flat|prone|supine)\b',                  'лежит'),
    (r'\bkneel(?:s|ing|ed)?\s+(?P<prep>in front of|beside|next to)\s+(?P<obj>(?:the|a|an)\s+[A-Za-z][A-Za-z\s\-\']{0,30}?)(?=[\s.,;!?]|$)', 'на коленях'),
    (r'\bkneel(?:s|ing|ed)?\b',                                                    'на коленях'),
    (r'\bsits?\s+(?P<prep>on|at|behind|in)\s+(?P<obj>(?:the|a|an)\s+[A-Za-z][A-Za-z\s\-\']{0,30}?)(?=[\s.,;!?]|$)', 'сидит'),
    (r'\bsits?\s+(?:down|cross-legged)\b',                                         'сидит'),
    (r'\bsit(?:ting|s)?\b',                                                        'сидит'),
    (r'\bcrouch(?:es|ing|ed)?\b',                                                  'приседает'),
    (r'\bsquat(?:s|ting)?\b',                                                      'приседает'),
    (r'\bleans?\s+(?P<prep>against|on|onto)\s+(?P<obj>(?:the|a|an)\s+[A-Za-z][A-Za-z\s\-\']{0,30}?)(?=[\s.,;!?]|$)', 'опирается'),
    (r'\bleans?\s+(?:against|on|onto)\b',                                          'опирается'),
    (r'\bslump(?:s|ed|ing)?\b',                                                    'опирается'),
    (r'\bstands?\b',                                                               'стоит'),
    (r'\bstand(?:s|ing)?\b',                                                       'стоит'),
    # Russian patterns (in case script is bilingual / russian)
    (r'\bлежит\b|\bлёжа\b|\bлежу\b|\bлежал[аи]?\b',                                'лежит'),
    (r'\bна коленях\b|\bопустил[аи]?ся\b|\bприклонил[аи]? колен',                  'на коленях'),
    (r'\bсидит\b|\bсидя\b|\bсадится\b|\bуселся\b|\bусел[аи]?сь\b',                 'сидит'),
    (r'\bстоит\b|\bстоя\b|\bвстал[аи]?\b',                                          'стоит'),
]

# English preposition → Russian preposition (used by location override).
_PREP_EN_TO_RU = {
    'under': 'под', 'underneath': 'под', 'beneath': 'под',
    'on': 'на', 'on top of': 'на', 'over': 'над', 'across': 'поперёк',
    'in front of': 'перед', 'beside': 'рядом с', 'next to': 'рядом с',
    'at': 'у', 'behind': 'за', 'in': 'в',
    'against': 'прислонился к', 'onto': 'на',
}

def _detect_script_pose_for_char(chunk_text, char_name):
    """Scan chunk_text for explicit pose verbs near the character's name.
    Returns (pose_label, sentence_clip, where_phrase). where_phrase is a
    server-side translation of the matched prepositional context («under
    the car» → «под the car») used to override the «где=» field on the
    Vision-analysis line — without this, the location was preserved from
    a misread render («рядом с машиной») even when the pose was patched
    to «лежит». Empty strings when nothing matches."""
    if not chunk_text or not char_name:
        return '', '', ''
    # Normalize name for fuzzy match: try full name and first token.
    name_tokens = [char_name.strip()]
    first = char_name.split()[0] if char_name.split() else char_name
    if first and first != char_name:
        name_tokens.append(first)
    # Walk sentences (split on . ! ? newline) — we want pose verbs in the same
    # sentence as the char's name to avoid cross-character leakage.
    sentences = re.split(r'(?<=[.!?])\s+|\n+', chunk_text)
    for sent in sentences:
        s_low = sent.lower()
        if not any(t.lower() in s_low for t in name_tokens):
            continue
        for pat, pose in _SCRIPT_POSE_PATTERNS:
            m = re.search(pat, sent, re.IGNORECASE)
            if not m:
                continue
            clip = sent.strip()
            if len(clip) > 140:
                # Trim at the last word boundary inside the budget so we
                # don't slice mid-word («sticking out fro...»). Falls back
                # to a hard slice only if there's no space in range.
                cut = clip.rfind(' ', 0, 137)
                clip = (clip[:cut] if cut > 60 else clip[:137]) + '...'
            # Compose where-phrase from named «prep» / «obj» groups when present.
            where_phrase = ''
            try:
                prep = (m.groupdict().get('prep') or '').strip().lower()
                obj  = (m.groupdict().get('obj') or '').strip()
                if prep and obj:
                    rus_prep = _PREP_EN_TO_RU.get(prep, prep)
                    # Strip leading article ("the car" → "the car" kept; LLM
                    # composer understands "под the car" fine and will write
                    # "под машиной" in the final Russian SUBJECT line).
                    where_phrase = f'{rus_prep} {obj}'
            except (IndexError, AttributeError):
                pass
            return pose, clip, where_phrase
    return '', '', ''


def _override_vision_with_script_poses(analysis_text, prev_chunk_text, prev_chars):
    """Edit a Vision-analysis block in-place: for each character listed,
    look up the pose word in `prev_chunk_text` and replace the «поза=X»
    field on the Vision line if it disagrees. Adds «(по сценарию: <clip>)»
    so the downstream composer sees the source of truth and can override
    its own state echo with the script's pose."""
    if not analysis_text or not prev_chunk_text or not prev_chars:
        return analysis_text
    # Pre-compute script pose + where-phrase per known character.
    name_to_pose = {}
    for c in prev_chars:
        nm = (c.get('name') or '').strip()
        if not nm:
            continue
        pose, clip, where_phrase = _detect_script_pose_for_char(prev_chunk_text, nm)
        if pose:
            name_to_pose[nm.lower()] = (pose, clip, where_phrase)
            first = nm.split()[0] if nm.split() else nm
            if first.lower() != nm.lower():
                name_to_pose.setdefault(first.lower(), (pose, clip, where_phrase))
    if not name_to_pose:
        return analysis_text
    out_lines = []
    overrode_any = False
    # Tolerate markdown bold around the name («**Fox Woman**: поза=...») —
    # Haiku Vision sometimes returns markdown-formatted analysis. Earlier
    # regex required a bare letter start which silently skipped every
    # «**Name**» line → override didn't fire → bug stayed alive.
    # Capture pose-field and the trailing «| где=X | ...» tail SEPARATELY
    # so we can patch the «где=» segment when script gives explicit location.
    line_re = re.compile(
        r'^(\s*[•\-\*]\s*)(\*{0,2})([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-\']{0,40})(\*{0,2})(:\s*поза=)(\S[^|]*?)(\s*\|.*)$'
    )
    where_re = re.compile(r'(\|\s*где=)(\S[^|]*?)(\s*\|)', re.UNICODE)
    for ln in analysis_text.splitlines():
        m = line_re.match(ln)
        if not m:
            out_lines.append(ln)
            continue
        bullet, lpad, name, rpad, sep, current_pose, rest = m.groups()
        norm_name = name.strip().lower()
        forced = name_to_pose.get(norm_name)
        if not forced:
            first = norm_name.split()[0] if norm_name.split() else ''
            forced = name_to_pose.get(first) if first else None
        if not forced:
            out_lines.append(ln)
            continue
        scripted_pose, clip, where_phrase = forced
        pose_matches = (current_pose.strip().lower() == scripted_pose.lower())
        # When script also nailed location (under X / on X / etc.), override
        # the «где=» field too. Patch is applied to the line tail before we
        # rejoin everything. If the line has no где= field (some edge cases)
        # the regex sub silently no-ops.
        rest_patched = rest
        if where_phrase:
            def _patch_where(wm):
                # Don't overwrite if the existing где= already mentions our
                # location phrase (composer / Vision occasionally already
                # got it right) — avoid double labels.
                if where_phrase.lower() in wm.group(2).lower():
                    return wm.group(0)
                return f'{wm.group(1)}{where_phrase} (по сценарию){wm.group(3)}'
            rest_patched = where_re.sub(_patch_where, rest, count=1)
        if pose_matches and rest_patched == rest:
            # Nothing to patch — leave the line untouched.
            out_lines.append(ln)
            continue
        # Replace pose field (if needed); embed script note for composer.
        # Preserve original markdown-bold wrappers around the name.
        new_pose = scripted_pose if not pose_matches else current_pose
        note = ''
        if not pose_matches:
            note = f' (СЦЕНАРИЙ ВЫШЕ АНАЛИЗА: «{clip}» → {scripted_pose})'
        out_lines.append(f'{bullet}{lpad}{name}{rpad}{sep}{new_pose}{note}{rest_patched}')
        overrode_any = True
    if not overrode_any:
        return analysis_text
    # Detect if any override involved a «hard» pose (lying under / beneath /
    # halfway sticking out etc.) — those need extra cinematographic anchors
    # because vanilla Pixar/Banana gen has weak priors for «person halfway
    # under car, torso visible from beneath the bumper» and defaults to
    # «person lying next to car» 75% of the time. User-reported: 4 retakes
    # of «Fox Woman lies halfway under the car», only 1 placed her under,
    # and even that one put her behind the car instead of sticking out
    # from the side. Adding photography-style framing vocabulary to the
    # composer's instructions improves the hit rate.
    hard_pose_hints = []
    for pose, clip, where_phrase in name_to_pose.values():
        wl = (where_phrase or '').lower()
        cl = (clip or '').lower()
        if pose == 'лежит' and ('под' in wl or 'under' in cl or 'beneath' in cl or 'underneath' in cl):
            hard_pose_hints.append('hard-pose:under-car')
            break
    nb = (
        '\n\n[NB] Поля «поза=» / «где=» помеченные «(СЦЕНАРИЙ ВЫШЕ АНАЛИЗА:...)» или '
        '«(по сценарию)» — это server-side override Vision-анализа сценарным текстом '
        'прошлого чанка. Считай эту позу/локацию аутентичной; Vision видел рендер, '
        'рендер мог не справиться со сложной позой и нарисовал персонажа стоящим/сбоку. '
        'Сценарий — ground truth.'
    )
    if 'hard-pose:under-car' in hard_pose_hints:
        nb += (
            '\n\nДОПОЛНИТЕЛЬНО — РЕНДЕРНЫЕ ПОДСКАЗКИ для позы «лежит под объектом» (Pixar/Banana '
            'часто проваливают сложные позы и кладут персонажа РЯДОМ с машиной вместо ПОД ней). '
            'В SUBJECT/ACTION текущего промпта используй ТЕХНИЧЕСКУЮ КИНЕМАТОГРАФИЧЕСКУЮ ФОРМУЛИРОВКУ '
            'с явными визуальными якорями:\n'
            '  • «low-angle shot of [name]\'s head, shoulders and upper torso EMERGING from beneath '
            '    the front bumper / underside of the car, the rest of her body HIDDEN under the chassis, '
            '    she lies SUPINE (на спине) on the asphalt, face turned up toward [other char]»;\n'
            '  • не пиши «lying next to the car» / «лежит рядом с машиной» — эти формулировки '
            '    модель интерпретирует как «на боку у машины» и теряет «under» полностью;\n'
            '  • не пиши «lying behind the car» — модель прячет её за машиной целиком;\n'
            '  • явно укажи СТОРОНУ откуда она торчит: «her torso sticks out from the LEFT/RIGHT side '
            '    of the front of the vehicle» (или какая сторона прописана в lastframe / cutframe);\n'
            '  • CAMERA: low angle, knee-height looking slightly downward; визуально подсказывает '
            '    модели что персонаж НИЖЕ машины, а не рядом с ней.\n'
            'Composition anchor: если среди cutframe\'ов прошлого чанка ЕСТЬ кадр где этот '
            'персонаж правильно показан полу-под машиной — explicitly reference it («композиция '
            'персонажа повторяет @ImageN cutframe — голова и плечи выглядывают из-под передней '
            'части автомобиля»). Это сильнее всего повышает шанс корректного рендера.'
        )
    return '\n'.join(out_lines) + nb


