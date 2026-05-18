"""Build an ElevenLabs `composition_plan` for ONE scene of an episode.

Pipeline:
  1. Caller gathers scene context (script slice, chunk texts, series DNA,
     adjacent-scene snippets, target_duration_ms, user_hint).
  2. We assemble a prompt and call Claude (opus tier — same client app.py uses
     via `anthropic_ask`).
  3. Claude returns JSON with `positive_global_styles`, `negative_global_styles`,
     `sections[]` (4–6 items, each 8–18s, lines:[] always).
  4. `normalize_composition_plan` clamps section count, line:[] enforcement,
     re-scales durations proportionally to hit exact target_duration_ms.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable


# ── Prompt building blocks ────────────────────────────────────────────────────

_NEGATIVE_FLOOR = [
    'vocals', 'lyrics', 'copyrighted artist references',
    'pure ambient drone', 'sound design only', 'noise wash',
    'silence', 'empty space',
    'trap drums', '808 bass', 'trap hi-hats',
    'hip-hop beat', 'modern pop production',
    'EDM drop', 'dance music', 'club beat',
]

_BANNED_VOLUME_WORDS = [
    'crescendo', 'swelling', 'fade in', 'fade out', 'fades in', 'fades out',
    'barely audible',
]

_MUSIC_PLAN_RULES = """\
═══ RULES FOR composition_plan ═══

⚠️ HARD COPYRIGHT RULE — VIOLATING THIS REJECTS THE ENTIRE REQUEST AT THE API:
NEVER name any real composer, artist, band, film, TV show, video game,
album, song, or any other copyrighted IP in ANY field. Not in
positive_global_styles, not in positive_local_styles, not in
section_name. Describe the AESTHETIC purely with generic adjectives
(e.g. "modern hybrid orchestral", "dark minimalist piano score",
"haunting chamber strings") — never "{artist} style", "{film}-inspired",
"{game} score aesthetic". If your plan contains any proper name from
real-world music or media, the API rejects it with HTTP 400 and we lose
the generation.

GOAL: produce a real FILM SCORE — cinematic orchestral / hybrid-orchestral
underscore for short-drama TV. Vertical short-drama needs EMOTIONAL DRAMA,
not a club track. Music must be LISTENABLE (motif, harmonic motion,
rhythmic forward motion) but unmistakably ORCHESTRAL / SCORE — strings,
piano, woodwinds, brass, choir, atmospheric synths, hybrid orchestral
percussion. NOT trap. NOT pop. NOT EDM.

STEP 1 — MUSIC STRATEGY (think first, write JSON second):
  Decide:
    - SUB-GENRE within film-score family. READ THE SCENE MOOD FIRST,
      then pick the sub-genre that fits. Do NOT default to thriller.
      Available sub-genres — choose based on scene emotion:

      TENSION / CONFRONTATION / CHASE / ACTION:
        * Hybrid orchestral thriller — large strings, taiko / epic hybrid
          drums, brass stabs, electronic bass pulse, aggressive ostinato.
        * Tense orchestral action score — driving brass, full hybrid
          percussion ensemble, tremolo strings, urgent rhythmic figure.
        * Dark chamber thriller — solo cello or bass clarinet, col legno
          strings, sparse unpitched percussion, angular dissonance.

      ROMANCE / LONGING / TENDER MOMENT / REUNION:
        * Neo-classical piano romance — grand or felt piano legato melody,
          warm cello countermelody, soft string pad, harp shimmer.
        * Lyrical string score — expressive violin solo cantabile, lush
          string orchestra, gentle harmonic motion, no percussion.
        * Chamber intimacy score — piano trio (piano + violin + cello),
          delicate texture, rubato feel, intimate hall reverb.

      MYSTERY / SUSPENSE / SLOW REVEAL / DREAD:
        * Minimalist suspense score — low string ostinato, sparse piano
          tones, long silences filled with harmonic tension, no melody.
        * Dark electronic score with orchestral layers — sparse analog
          synths + real strings, prepared piano clusters, sub-bass pulse,
          NO trap drums.
        * Period-orchestral noir — muted strings, French horn low register,
          harp harmonics, muted piano, walking bass figure.

      TRIUMPH / HOPE / EMOTIONAL PEAK / RESOLUTION:
        * Sweeping orchestral drama — full string orchestra, choir swells,
          brass fanfare, timpani rolls, triumphant major-key resolution.
        * Epic hybrid score — large brass ensemble, massive hybrid
          percussion, choir, synthetic texture layers, cinematic swell.

      MELANCHOLY / GRIEF / LOSS / REFLECTION:
        * Minimal piano elegy — solo piano, few notes, long silences,
          sparse cello or violin comment, no percussion.
        * Orchestral elegy — sustained strings pppp, solo oboe or cor
          anglais melody, harp accents, grief-laden minor key.
        * Chamber string lament — string quartet, sul tasto tone,
          sorrow arc across the section, barely any movement.

      COMEDY / LIGHTNESS / PLAYFUL / RELIEF:
        * Whimsical orchestral score — pizzicato strings, light woodwind
          melody (flute / clarinet), staccato brass comic punctuation,
          bright major key, light percussion (triangle, wood block).
        * Neo-baroque playful score — harpsichord or celesta lead,
          busy string runs, comic timing hits, bouncy rhythm.

      NEUTRAL / TRANSITION / DIALOGUE / EXPOSITION:
        * Minimalist string score — slow repeating ostinato strings,
          subtle harmonic shifts, background pulse, no strong lead.
        * Warm orchestral underscore — gentle string pad, soft piano
          arpeggio, harp fill, background presence only.

      AVOID ALWAYS: cinematic trap, pop-cinematic, R&B-noir, EDM hybrid.
      IMPORTANT: vary your choice. Different scenes in the SAME episode
      SHOULD use DIFFERENT sub-genres — match the emotional beat of THIS
      specific scene, not the overall series tone.
    - TEMPO (BPM) — usually 60–110 for drama; 110–140 only for chases.
      Meter 4/4 default, 3/4 for waltz / lullaby, 6/8 for flowing tension.
    - KEY — minor keys for drama (D minor / F# minor / A minor / C minor
      / E minor). Major keys reserved for tender / hopeful moments.
    - LEAD INSTRUMENT(s) — concrete and acoustic-first:
      piano (felt / prepared / grand), solo strings (violin / viola /
      cello), woodwinds (clarinet / oboe / cor anglais), choir / vocal
      pads (wordless ooh/ahh — NOT lyrics), harp, celesta, music box.
      Synths allowed as TEXTURE / ambient pad, NOT as the lead hook.
    - RHYTHMIC FOUNDATION — orchestral / hybrid percussion:
      pizzicato strings ostinato, timpani, low-string pulse, taiko hits,
      epic hybrid drums, heartbeat kick, brushed snare, bowed bass tremolo,
      orchestral snare roll, hybrid percussion ensemble, frame drums,
      cinematic hybrid kit. Use real drums whenever the scene calls for
      tension, action, or urgency — DO NOT omit percussion by default.
      NEVER trap hi-hats, NEVER 808 bass, NEVER modern hip-hop drum kit.
      Use PERCUSSION when the scene has: confrontation, chase, action,
      revelation/shock, emotional climax. Chamber/intimate scenes may omit
      drums, but ANY thriller / action / dramatic scene MUST include them.
    - HARMONIC MOVEMENT — real chord progression, modulations, suspended
      chords resolving, parallel motion. Not one static pedal-point drone.
    - ENERGY ARC across sections.
    - DIALOGUE STRATEGY — per-section flag. In dialogue sections lower
      LEAD complexity but keep score character (sustained strings, soft
      piano arpeggio, choir pad) — never collapse to ticking-clock bed.

STEP 2 — SECTIONS (4 to 6):
  Total sum of duration_ms MUST equal the target duration provided below.
  Each section duration in [8000, 18000] ms.
  Each section performs ONE function from this score-vocabulary, named in
  section_name:
    motif statement, theme exposition, ostinato build, harmonic shift,
    string swell, brass hit, breakdown, motif reprise / variation,
    tension build, unresolved cliffhanger / suspension, resolution cadence.
  Adjacent sections must differ by ≥2 elements (instrument family /
  rhythmic figure / harmonic register / texture density) — no flat
  continuation.

STEP 3 — DYNAMICS:
  Volume stays uniform across the cue (engine-driven fades break in
  ElevenLabs). FORBIDDEN words anywhere in styles:
    crescendo, swelling, fade in, fade out, barely audible.
  The first section starts AT FULL DENSITY from 0s — main theme /
  ostinato / lead established immediately. No cold "motif enters at 6s".
  No section is allowed to collapse to pure drone / room tone / sound
  design — there must be melodic OR rhythmic OR harmonic content at all
  times.

STEP 4 — DIALOGUE-FRIENDLY SECTIONS:
  When characters talk, keep score MUSICAL: sustained strings under
  the line, soft piano arpeggio, choir pad, low brass sustain. Lead
  motif may simplify to a single sustained note, but harmonic motion
  and pulse continue. Never reduce to ticking clocks or low drones.

STEP 5 — STYLE FORMULA per section:
  positive_local_styles[i] = "<film-score sub-genre tag> + <key/tempo cue> +
                              <lead instrument with character> +
                              <rhythmic figure> + <harmonic motion> +
                              <emotion> + <scene context>"
  4–7 descriptors per section. Make instruments SPECIFIC:
    "felt piano" / "prepared piano" / "grand piano"
    "solo cello legato" / "muted cello pizzicato"
    "violin section sul ponticello" / "violin solo cantabile"
    "clarinet low register sustained"
    "wordless female choir ooh"
    "harp arpeggio descending"
    "timpani roll soft"
    "low string tremolo divisi"
  not generic ("synth", "drums", "strings").

STEP 6 — GLOBAL STYLES:
  positive_global_styles: 5–8 elements covering: series DNA + the
  sub-genre YOU chose for THIS scene + tempo + key + lead-instrument
  family + production aesthetic. Make these SPECIFIC to this scene's
  chosen sub-genre, NOT generic. Examples by sub-genre:
    thriller: "modern hybrid score production", "aggressive orchestral mix"
    romance: "warm intimate chamber recording", "lyrical string aesthetic"
    mystery: "dark minimal orchestral texture", "cold reverb hall"
    triumph: "epic orchestral swell", "cinematic concert hall reverb"
    elegy: "sparse intimate piano recording", "dry close-mic cello"
    comedy: "bright staccato orchestral production", "light playful mix"
  ALWAYS include at least one "cinematic film score" descriptor.
  negative_global_styles MUST include (at minimum):
    vocals, lyrics, copyrighted artist references,
    pure ambient drone, sound design only, noise wash,
    silence, empty space,
    trap drums, 808 bass, trap hi-hats,
    hip-hop beat, modern pop production,
    EDM drop, dance music, club beat.

STEP 7 — JSON OUTPUT (STRICT):
  Return ONLY a JSON object, NO markdown fences, NO commentary.
  Each section must have:
    section_name: string ≤100 chars, format "<from>-<to>s: <function>, <scene phrase>"
    duration_ms: integer
    positive_local_styles: array of 4–7 strings
    negative_local_styles: array of 0–4 strings
    lines: [] (ALWAYS an empty array — instrumental score)
"""

_EXAMPLE_JSON = '''\
{
  "positive_global_styles": [
    "cinematic film score",
    "hybrid orchestral thriller",
    "modern epic hybrid orchestral aesthetic",
    "D minor",
    "78 BPM half-time pulse",
    "low strings ostinato with felt piano lead",
    "wordless female choir layer",
    "warm analog hybrid orchestral mix"
  ],
  "negative_global_styles": [
    "vocals", "lyrics", "copyrighted artist references",
    "pure ambient drone", "sound design only", "noise wash",
    "silence", "empty space",
    "trap drums", "808 bass", "trap hi-hats",
    "hip-hop beat", "modern pop production",
    "EDM drop", "dance music", "club beat"
  ],
  "sections": [
    {
      "section_name": "0-12s: motif statement, discovery — frozen shock",
      "duration_ms": 12000,
      "positive_local_styles": [
        "hybrid orchestral thriller, D minor 78 BPM half-time",
        "felt piano motif descending fifth A-D-A",
        "low strings divisi pedal tone sustained",
        "timpani slow heartbeat pulse beats 1 and 3",
        "orchestral snare ghost notes sparse",
        "wordless choir pad distant atmosphere",
        "frozen dread to dawning realization arc"
      ],
      "negative_local_styles": ["bright major chords", "trap hi-hats"],
      "lines": []
    },
    {
      "section_name": "12-27s: tension build with percussion, confrontation erupts",
      "duration_ms": 15000,
      "positive_local_styles": [
        "hybrid orchestral thriller, D minor 92 BPM",
        "epic hybrid percussion ensemble full kit driving",
        "taiko hits on downbeats accenting peaks",
        "low brass stab syncopated rhythm",
        "violin section tremolo sul ponticello high register",
        "felt piano cluster chords off-beat",
        "mounting confrontation — control giving way to chaos"
      ],
      "negative_local_styles": ["808 bass", "trap hi-hats"],
      "lines": []
    },
    {
      "section_name": "27-39s: ostinato build dialogue-friendly, accusation",
      "duration_ms": 12000,
      "positive_local_styles": [
        "chamber-score intimate drama, 78 BPM",
        "pizzicato strings ostinato sixteenth notes",
        "cello legato sustained mid register",
        "brushed snare quiet pulse underneath",
        "soft piano broken arpeggio supporting",
        "choir pad held suspended chord D minor 9",
        "claustrophobic mounting accusation tension"
      ],
      "negative_local_styles": ["lead melody on top"],
      "lines": []
    }
  ]
}'''


# ── Public API ────────────────────────────────────────────────────────────────


def build_prompt(
    *,
    series: dict,
    episode_number: int,
    scene_idx: int,
    scene_script_text: str,
    chunk_texts: list[str],
    prev_scene_tail: str,
    next_scene_head: str,
    target_duration_ms: int,
    user_hint: str = '',
    episode_blocking: str = '',
) -> str:
    """Assemble the user-side prompt for Claude. The composition_plan rules go
    as system; this returns the scene-specific data payload."""

    dna_bits = []
    for field, label in [
        ('genre', 'Genre'),
        ('tone', 'Tone'),
        ('visual_style', 'Visual style'),
    ]:
        v = (series.get(field) or '').strip()
        if v:
            dna_bits.append(f'- {label}: {v}')
    synopsis = (series.get('synopsis_global') or series.get('synopsis') or '').strip()
    if synopsis:
        dna_bits.append(f'- Series synopsis: {synopsis[:600]}')
    dna_block = '\n'.join(dna_bits) or '(not specified)'

    chunks_block = ''
    if chunk_texts:
        joined = '\n---\n'.join((t or '').strip()[:600] for t in chunk_texts if t)
        chunks_block = f'\n=== CHUNKS IN THIS SCENE ===\n{joined}\n'

    prev_block = ''
    if prev_scene_tail.strip():
        prev_block = f'\n=== PREVIOUS SCENE TAIL (~400ch) ===\n{prev_scene_tail.strip()[:400]}\n'
    next_block = ''
    if next_scene_head.strip():
        next_block = f'\n=== NEXT SCENE OPENING (~400ch) ===\n{next_scene_head.strip()[:400]}\n'

    eb_block = ''
    if episode_blocking.strip():
        eb_block = f'\n=== EPISODE BLOCKING (constant across episode) ===\n{episode_blocking.strip()[:800]}\n'

    hint_block = ''
    if user_hint.strip():
        hint_block = (
            f'\n=== 🎯 OPERATOR OVERRIDE (must respect, overrides defaults) ===\n'
            f'{user_hint.strip()[:400]}\n'
        )

    target_sec = round(target_duration_ms / 1000)

    return f"""You are a cinematic-score composer building a composition_plan for the ElevenLabs Music API.

You score ONE scene (sceneIdx={scene_idx}) of episode {episode_number}.

=== TARGET LENGTH ===
duration_ms MUST sum to EXACTLY {target_duration_ms} ms (~{target_sec}s).

=== SERIES DNA ===
{dna_block}
{eb_block}{prev_block}
=== SCENE SCRIPT (this scene only) ===
{(scene_script_text or '').strip()[:3500]}
{chunks_block}{next_block}{hint_block}

{_MUSIC_PLAN_RULES}

=== EXAMPLE OUTPUT FORMAT ===
{_EXAMPLE_JSON}

═══ FINAL CHECKLIST before output ═══
1. sum(sections[].duration_ms) == {target_duration_ms}
2. 4 ≤ len(sections) ≤ 6
3. Each section: 8000 ≤ duration_ms ≤ 18000
4. Every section has lines: []
5. negative_global_styles includes ALL of: {', '.join(_NEGATIVE_FLOOR)}
6. No banned volume words anywhere: {', '.join(_BANNED_VOLUME_WORDS)}
7. First section starts at FULL density, last section closes the emotional arc.
8. PERCUSSION CHECK: if ANY section involves tension / action / confrontation /
   revelation / urgency — that section MUST include orchestral or hybrid
   percussion in its positive_local_styles (timpani, taiko, hybrid drums,
   brushed snare, orchestral snare roll, heartbeat kick, etc.). Pure chamber/
   intimate sections may omit drums, but thriller/drama scenes cannot.
9. STYLE VARIETY CHECK: re-read the scene. Does your chosen sub-genre
   actually match the dominant emotion? "Hybrid orchestral thriller" is
   correct ONLY for tension/action. Romance, grief, comedy, mystery each
   need a DIFFERENT sub-genre from the list in STEP 1. Do not default to
   thriller because the series is dramatic — match THIS scene specifically.

Return ONLY the JSON object. No markdown fences, no prose.
"""


def call_claude_for_plan(
    *,
    series: dict,
    episode_number: int,
    scene_idx: int,
    scene_script_text: str,
    chunk_texts: list[str],
    prev_scene_tail: str,
    next_scene_head: str,
    target_duration_ms: int,
    user_hint: str,
    episode_blocking: str,
    claude_fn: Callable[..., str],
) -> dict:
    """Build prompt → run Claude → parse JSON → normalize.

    `claude_fn` is `anthropic_ask` or equivalent: `fn(prompt, system='', model='...')`.
    """
    prompt = build_prompt(
        series=series,
        episode_number=episode_number,
        scene_idx=scene_idx,
        scene_script_text=scene_script_text,
        chunk_texts=chunk_texts,
        prev_scene_tail=prev_scene_tail,
        next_scene_head=next_scene_head,
        target_duration_ms=target_duration_ms,
        user_hint=user_hint,
        episode_blocking=episode_blocking,
    )
    raw = claude_fn(prompt, system='You return STRICTLY valid JSON. No markdown, no commentary.',
                    model='sonnet')
    plan = _parse_json_lenient(raw)
    return normalize_composition_plan(plan, target_duration_ms=target_duration_ms)


def _parse_json_lenient(text: str) -> dict:
    """Strip ``` fences and parse JSON. Tolerate trailing commas / preamble."""
    if not text:
        raise ValueError('empty response from Claude')
    s = text.strip()
    # Strip ```json … ``` fences if present.
    s = re.sub(r'^```(?:json)?\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*```\s*$', '', s)
    # Cut to outermost {...} if Claude prefixed prose.
    if not s.startswith('{'):
        m = re.search(r'\{.*\}', s, re.DOTALL)
        if not m:
            raise ValueError(f'no JSON object in response: {text[:200]}')
        s = m.group(0)
    return json.loads(s)


def normalize_composition_plan(plan: dict, target_duration_ms: int) -> dict:
    """Enforce contract before sending to ElevenLabs.

      - clamp section count to [4, 6] (truncate if too many; if too few, accept
        as-is and let ElevenLabs reject — this is a Claude bug we surface);
      - section_name ≤ 100 chars;
      - lines = [] always;
      - re-scale section durations proportionally to hit target_duration_ms
        exactly (last section eats the rounding remainder);
      - inject negative_global_styles floor if missing;
      - strip banned volume words from local/global styles.
    """
    if not isinstance(plan, dict):
        raise ValueError('plan must be an object')
    sections = plan.get('sections') or []
    if not isinstance(sections, list) or not sections:
        raise ValueError('plan.sections is empty')

    if len(sections) > 6:
        sections = sections[:6]

    cleaned = []
    for s in sections:
        if not isinstance(s, dict):
            continue
        name = (s.get('section_name') or '')[:100]
        dur = int(s.get('duration_ms') or 0)
        if dur <= 0:
            continue
        pos = [_sanitize_style(x) for x in (s.get('positive_local_styles') or []) if x]
        neg = [_sanitize_style(x) for x in (s.get('negative_local_styles') or []) if x]
        cleaned.append({
            'section_name': name,
            'duration_ms': dur,
            'positive_local_styles': [x for x in pos if x],
            'negative_local_styles': [x for x in neg if x],
            'lines': [],
        })
    if not cleaned:
        raise ValueError('no valid sections after cleanup')

    # Proportional re-scale to hit target_duration_ms exactly.
    total = sum(s['duration_ms'] for s in cleaned)
    if total != target_duration_ms:
        ratio = target_duration_ms / total
        scaled = []
        for s in cleaned[:-1]:
            new_d = max(3000, int(round(s['duration_ms'] * ratio)))
            scaled.append({**s, 'duration_ms': new_d})
        consumed = sum(s['duration_ms'] for s in scaled)
        tail = max(3000, target_duration_ms - consumed)
        scaled.append({**cleaned[-1], 'duration_ms': tail})
        cleaned = scaled

    pos_g = [_sanitize_style(x) for x in (plan.get('positive_global_styles') or []) if x]
    neg_g = [_sanitize_style(x) for x in (plan.get('negative_global_styles') or []) if x]
    # Floor: ensure every mandatory negative is present.
    have = {x.lower() for x in neg_g}
    for needed in _NEGATIVE_FLOOR:
        if needed.lower() not in have:
            neg_g.append(needed)

    return {
        'positive_global_styles': [x for x in pos_g if x][:8],
        'negative_global_styles': [x for x in neg_g if x],
        'sections': cleaned,
    }


# Frequently-leaked copyrighted names — even after the HARD COPYRIGHT RULE in
# the prompt, Claude sometimes slips composer names through. Drop any style
# descriptor that contains one of these substrings (case-insensitive). The
# list is non-exhaustive; the prompt rule plus this catch covers most cases.
_COPYRIGHT_NAMES = [
    'zimmer', 'hildur', 'guðnadóttir', 'gudnadottir', 'richter', 'reznor',
    'atticus ross', 'göransson', 'goransson', 'jóhannsson', 'johansson',
    'jóhann jóhannsson', 'martinez', 'desplat', 'arnalds', 'olafur',
    'einaudi', 'beltrami', 'horner', 'williams', 'morricone', 'glass',
    'newman', 'shore', 'powell', 'badelt', 'silvestri', 'elfman',
    'howard shore', 'newton howard', 'lorne balfe',
    'hans ', 'philip ', 'thomas ',
    'inception', 'interstellar', 'dune', 'joker', 'arrival',
    'chernobyl', 'mandalorian', 'sicario', 'blade runner',
]


def _sanitize_style(s: Any) -> str:
    if not isinstance(s, str):
        return ''
    txt = s.strip()
    if not txt:
        return ''
    low = txt.lower()
    for banned in _BANNED_VOLUME_WORDS:
        if banned in low:
            return ''
    for name in _COPYRIGHT_NAMES:
        if name in low:
            # Drop the whole descriptor — partial scrubbing leaves dangling
            # adjectives like "late-era aesthetic" that confuse the engine.
            return ''
    return txt[:120]
