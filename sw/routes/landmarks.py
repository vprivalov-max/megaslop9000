"""Story landmarks: checkpoints, finale, trajectory builder, bridge plans
(helpers + API routes)."""
import json
import re
import time

from flask import jsonify, request

from sw.core import app
from sw.jsonutils import strip_json
from sw.llm import claude_ask_fast
from sw.logging_utils import _log_event
from sw.storage import (chunk_range, is_batch_mode, list_episodes, load_canon,
                        load_series, save_series)
from sw.story_prompts import (_canonical_cast_block, _format_canon_for_prompt,
                              _format_mode_block)

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
    """Use Claude to draft a strong story-twist for a checkpoint at episode `ep`.

    Includes the actual series state (cast / canon / episode synopses up to ep-1)
    so the checkpoint can only use characters and facts that actually exist.
    """
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

    # Cast — the only characters allowed
    cast_block = _canonical_cast_block(s)

    # Canon — facts, open threads
    try:
        canon = load_canon(sid)
        canon_block = _format_canon_for_prompt(canon, max_facts=25, max_timeline=6)
        if canon_block:
            canon_block = f'=== CANON (live story state) ===\n{canon_block}\n\n'
    except Exception:
        canon_block = ''

    # Episode synopses — ONLY episodes before the checkpoint (so model doesn't
    # see future episodes; future events should follow from the checkpoint)
    try:
        prior_eps = sorted(
            [e for e in list_episodes(sid)
             if (e.get('synopsis') or '').strip()
             and int(e.get('number', 0) or 0) < int(ep)],
            key=lambda e: int(e.get('number', 0) or 0),
        )
        if prior_eps:
            ep_lines = []
            for e in prior_eps:
                n = e.get('number', '?')
                syn = (e.get('synopsis') or '').strip().replace('\n', ' ')[:220]
                ep_lines.append(f"  Ep{n}: {syn}")
            synopses_block = (
                f'=== EPISODE SYNOPSES SO FAR (Eps 1–{prior_eps[-1].get("number","?")}) ===\n'
                + '\n'.join(ep_lines) + '\n\n'
            )
        else:
            synopses_block = ''
    except Exception:
        synopses_block = ''

    fmt_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Logline: {s.get("synopsis","")}\nArc: {s.get("arc","")}\n\n'
        + fmt_block
        + cast_block
        + canon_block
        + synopses_block
        + other_block + fin_block +
        f'\nDraft ONE strong story checkpoint that should hit at EPISODE {ep}. '
        'HARD RULE: use ONLY characters from the CANONICAL CAST above. '
        'NEVER invent new named characters — reuse the canonical cast. If a new role is genuinely unavoidable, give it a short UNIQUE proper name (e.g. "Detective Cole"), never a bare role word — every on-camera character must be nameable so its reference portrait can bind. '
        'HARD RULE: build on threads / facts that already exist in the canon and prior episodes. '
        'Do NOT introduce backstory or plot points that were never set up. '
        'The checkpoint must be a single sharp dramatic event — a major reversal, betrayal, reveal, '
        'public scandal, death, return, identity exposed, or power flip — that the writer '
        'must steer toward and that will reshape the whole arc afterwards. '
        'Be SPECIFIC: name the canonical character(s) involved, name the action, name the consequence. '
        '2–4 sentences max. '
        'Output ONLY the checkpoint description, no preface, no JSON, no markdown.'
    )
    try:
        text = claude_ask_fast(
            prompt,
            system=(
                'You are a short-drama showrunner pitching mid-arc twists. Write in Russian. '
                'Use ONLY exact character names from the canonical cast provided. '
                'Build on actual events from the episode synopses — never invent characters or backstory. '
                'Keep it punchy, concrete, irreversible.'
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


@app.route('/api/series/<sid>/trajectory-validation', methods=['GET'])
def trajectory_validation(sid):
    """Return character-name mismatches between the canonical cast and the
    user-pinned landmarks (finale + checkpoints). UI surfaces this as a
    warning when the user opens the finale modal so they know to regenerate.
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    try:
        mismatches = _extract_landmark_character_mismatch(s)
    except Exception as e:
        return jsonify({'error': str(e), 'mismatches': []}), 200
    cast_names = [(c.get('name') or '').strip()
                  for c in (s.get('characters') or [])
                  if (c.get('name') or '').strip()]
    return jsonify({
        'mismatches': [{'where': label, 'unknown_names': names} for label, names in mismatches],
        'cast_names': cast_names,
        'has_problem': bool(mismatches),
    })


@app.route('/api/series/<sid>/finale/generate', methods=['POST'])
def generate_finale(sid):
    """Use Claude to draft a finale for a given episode number.

    Includes the actual series state (cast / canon / episode synopses) so the
    finale can only use characters and facts that actually exist — no inventing
    Сергей-the-lawyer who never appeared in any episode.
    """
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

    # Cast — the only characters that may appear in the finale
    cast_block = _canonical_cast_block(s)

    # Canon — facts, open threads, character knowledge state
    try:
        canon = load_canon(sid)
        canon_block = _format_canon_for_prompt(canon, max_facts=30, max_timeline=8)
        if canon_block:
            canon_block = f'=== CANON (live story state) ===\n{canon_block}\n\n'
    except Exception:
        canon_block = ''

    # Episode synopses — ALL existing episodes with their synopses, so the finale
    # can build on actual story events, not hallucinated ones.
    try:
        all_eps = sorted(
            [e for e in list_episodes(sid) if (e.get('synopsis') or '').strip()],
            key=lambda e: int(e.get('number', 0) or 0),
        )
        if all_eps:
            ep_lines = []
            for e in all_eps:
                n = e.get('number', '?')
                syn = (e.get('synopsis') or '').strip().replace('\n', ' ')[:250]
                ep_lines.append(f"  Ep{n}: {syn}")
            synopses_block = (
                f'=== EPISODE SYNOPSES (Eps 1–{all_eps[-1].get("number","?")}) ===\n'
                + '\n'.join(ep_lines) + '\n\n'
            )
        else:
            synopses_block = ''
    except Exception:
        synopses_block = ''

    fmt_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Logline: {s.get("synopsis","")}\nArc: {s.get("arc","")}\n\n'
        + fmt_block
        + cast_block
        + canon_block
        + synopses_block
        + cps_block +
        f'\nWrite the SERIES FINALE for episode {ep}. '
        'HARD RULE: use ONLY characters from the CANONICAL CAST above. '
        'NEVER invent new named characters (no Сергей-the-lawyer, no Маша-the-sister) — '
        'if a role is needed and no canonical character fits, use a role label (Адвокат, Свидетель). '
        'HARD RULE: build the finale on events that ACTUALLY happened in the episode synopses above '
        '(or on threads opened in the canon). Do NOT reference plot points that were never set up. '
        'It must be a satisfying climax that pays off the central conflict and the romance/revenge/identity arc. '
        'Specify: (1) WHO is alive, dead, exposed, redeemed, in power; (2) WHAT the final emotional beat is; '
        '(3) WHAT explicitly cannot change between now and then (e.g. character X must be alive, '
        'character Y must still hold the secret, the contract must remain unsigned, etc.). '
        '4–7 sentences. Russian. Output ONLY the finale description.'
    )
    try:
        text = claude_ask_fast(
            prompt,
            system=(
                'You are a short-drama showrunner writing series finales. Russian. '
                'Concrete characters and stakes. Use ONLY characters that already exist in the series. '
                'Build the finale on events that already happened on screen.'
            ),
        )
    except Exception as e:
        return jsonify({'error': f'generation failed: {e}'}), 500
    return jsonify({'description': (text or '').strip(), 'episode': ep})


def _extract_landmark_character_mismatch(s):
    """Compare named characters mentioned in checkpoints + finale text against the
    canonical cast. Returns a list of (label, [unknown_names]) tuples for any
    landmark text that references characters NOT in the cast.

    This catches the failure mode where the finale was generated with broken code
    (used wrong names like Дарья/Виктория/Сергей while the cast is Emma/Clara/Vivian).
    """
    cast = (s or {}).get('characters') or []
    cast_names = set()
    for c in cast:
        n = (c.get('name') or '').strip()
        if n:
            cast_names.add(n.lower())
    if not cast_names:
        return []  # no cast — can't validate

    # Capitalised Latin or Cyrillic tokens of 3+ letters.
    name_pat = re.compile(r'\b([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё]{2,})\b')
    # Common Russian role / location nouns that look capitalised in context but aren't names.
    ROLE_WORDS = {
        'детектив', 'адвокат', 'доктор', 'свидетель', 'полиция', 'охрана', 'соседи',
        'семья', 'мать', 'отец', 'брат', 'сестра', 'муж', 'жена', 'сын', 'дочь',
        'коронер', 'инспектор', 'мачеха', 'мама', 'папа', 'эпизод', 'серия', 'финал',
        'кабинет', 'офис', 'дом', 'сад', 'улица', 'город', 'школа', 'поместье', 'estate',
        'episode', 'series', 'finale', 'house', 'office', 'court', 'mr', 'mrs', 'ms', 'dr',
    }

    mismatches = []
    sources = [('finale', ((s.get('finale') or {}).get('description') or ''))]
    for c in (s.get('checkpoints') or []):
        sources.append((f"checkpoint Ep{c.get('episode','?')}", c.get('description') or ''))

    for label, text in sources:
        if not text.strip():
            continue
        names_in_text = set()
        for m in name_pat.finditer(text):
            w = m.group(1)
            if w.lower() in ROLE_WORDS:
                continue
            names_in_text.add(w)
        unknown = sorted({n for n in names_in_text if n.lower() not in cast_names})
        if unknown:
            mismatches.append((label, unknown[:8]))
    return mismatches


# ── Story trajectory builder (used by writer prompts) ────────────────────────
def build_trajectory_block(s, current_ep):
    """Render upcoming checkpoints + finale into a HIGH-VISIBILITY context block
    injected into writer / synopsis prompts.

    The writer ignores soft suggestions in long prompts. This block is loud,
    labeled MANDATORY, and includes a per-distance "what this episode must do
    about it" rule so the model has no excuse to drift away from the finale.
    """
    cps_all = s.get('checkpoints') or []
    fin = s.get('finale') or None
    upcoming = [c for c in cps_all if int(c.get('episode', 0)) >= current_ep and (c.get('description') or '').strip()]
    upcoming.sort(key=lambda c: int(c['episode']))
    fin_relevant = bool(fin and fin.get('description', '').strip()
                        and int(fin.get('episode', 0)) >= current_ep)

    if not upcoming and not fin_relevant:
        return ''

    lines = [
        '╔══════════════════════════════════════════════════════════════════╗',
        '║ ⚡ NARRATIVE TRAJECTORY — MANDATORY. READ BEFORE WRITING ANYTHING ⚡║',
        '╚══════════════════════════════════════════════════════════════════╝',
        '',
        'The series has user-pinned story landmarks below. Every word you write',
        'must move the plot TOWARD them. Drift = automatic rewrite.',
        '',
    ]

    if upcoming:
        lines.append('▶ UPCOMING CHECKPOINTS (you must seed setup so these can hit on time):')
        for c in upcoming:
            ep_n = int(c['episode'])
            distance = ep_n - current_ep
            if distance == 0:
                tag = '🎯 THIS EPISODE — events below MUST happen here'
            elif distance == 1:
                tag = '⚠ NEXT EPISODE — final setup window, get pieces in place'
            elif distance <= 3:
                tag = f'⏰ {distance} ep(s) away — tighten setup, do not delay'
            else:
                tag = f'{distance} ep(s) away — plant subtle seeds'
            lines.append(f'  Ep {ep_n} [{tag}]:')
            lines.append(f'    {c["description"].strip()}')
        if any(int(c["episode"]) - current_ep > 0 for c in upcoming):
            lines.append('')
            lines.append('  RULE: do NOT prematurely fire a future checkpoint. Plant seeds now')
            lines.append('  (entrances, unspoken motives, prop placements, whispered allusions)')
            lines.append('  so the payoff feels earned and inevitable when its episode arrives.')

    if fin_relevant:
        ep_n = int(fin['episode'])
        distance = ep_n - current_ep
        if distance == 0:
            when = '🎯 THIS EPISODE IS THE FINALE'
        elif distance == 1:
            when = '⚠ FINALE IS NEXT EPISODE'
        else:
            when = f'{distance} ep(s) away'
        lines.append('')
        lines.append(f'▶ 🏁 SERIES FINALE (Ep {ep_n}, {when}):')
        lines.append(f'    {fin["description"].strip()}')
        lines.append('')
        lines.append('  HARD CONSTRAINTS FROM FINALE:')
        lines.append('  • Any character / power-state / secret / relationship the finale relies on')
        lines.append('    MUST remain achievable from this episode onward.')
        lines.append('  • Do NOT kill, expose, remove, or otherwise neutralize anyone the finale needs.')
        lines.append('  • Do NOT resolve a conflict the finale needs unresolved.')
        lines.append('  • Do NOT introduce a competing climax that would steal the finale\'s moment.')
        if distance == 0:
            lines.append('  • The finale\'s exact events MUST happen in this episode — use the')
            lines.append('    specified characters and actions, not similar substitutes.')
        elif distance <= 3:
            lines.append(f'  • Only {distance} ep(s) until the finale — every plot move should now')
            lines.append('    visibly converge toward the finale\'s climax.')

    # CHARACTER-NAME MISMATCH WARNING — if the finale / checkpoints reference
    # names that don't exist in the canonical cast, the writer must MAP them
    # by role, not abandon the trajectory.
    try:
        mismatches = _extract_landmark_character_mismatch(s)
    except Exception:
        mismatches = []
    if mismatches:
        lines.append('')
        lines.append('⚠ ⚠ ⚠  CHARACTER NAME MISMATCH IN PINNED LANDMARKS  ⚠ ⚠ ⚠')
        lines.append('The following landmark texts reference names that DO NOT EXIST in the canonical cast:')
        for label, names in mismatches:
            lines.append(f'  · {label}: {", ".join(names)}')
        # Render canonical cast list inline for mapping
        cast_inline = ', '.join((c.get('name') or '').strip()
                                for c in (s.get('characters') or [])
                                if (c.get('name') or '').strip())
        if cast_inline:
            lines.append(f'CANONICAL CAST: {cast_inline}')
        lines.append('')
        lines.append('RULE: the landmark texts above were likely generated with the wrong names.')
        lines.append('Do NOT abandon the landmark just because the names differ. Instead:')
        lines.append('  1. Identify each unknown name by its ROLE in the landmark description')
        lines.append('     (stepmother, lawyer, sister, protagonist, daughter, etc.).')
        lines.append('  2. Map that role to the closest CANONICAL CAST member.')
        lines.append('     Example: landmark says "мачеха Виктория" → use canonical "Vivian".')
        lines.append('     Example: landmark says "адвокат Сергей" → if no lawyer in cast, use role label "Lawyer".')
        lines.append('  3. Execute the landmark EVENTS using the canonical names — keep the plot beats,')
        lines.append('     swap the names.')
        lines.append('  4. NEVER add an unknown name into the script — always use the canonical equivalent.')

    lines.append('')
    lines.append('═══════════════════════════════════════════════════════════════════')
    return '\n'.join(lines) + '\n\n'


# ── Finale awareness — is THIS episode the pinned series finale? ──────────────
def finale_episode_num(s):
    """Return the pinned finale episode (chunk) number, or None.

    In batch mode the finale is pinned by chunk number (milestones collapse to
    chunk numbers — see milestone_episode_numbers), so the value is directly
    comparable to the `num` passed to generate_episode_script in both modes."""
    fin = (s or {}).get('finale') or None
    if not fin or not (fin.get('description') or '').strip():
        return None
    try:
        return int(fin.get('episode', 0)) or None
    except (TypeError, ValueError):
        return None


def is_finale_episode(s, num):
    """True iff `num` is the unit (episode or chunk) the user pinned as the finale."""
    fe = finale_episode_num(s)
    try:
        return fe is not None and int(num) == fe
    except (TypeError, ValueError):
        return False


def build_finale_contract_block(s, num):
    """LOUD, mandatory FINALE block. Returns '' unless `num` is the pinned finale.

    Root cause this fixes: the base writer system prompt and the per-episode
    instruction MANDATE a cliffhanger and FORBID any wrap-up — calibrated for
    mid-season episodes. The finale must do the OPPOSITE: resolve every open
    thread and end conclusively. Without an explicit override the writer obeys
    the cliffhanger mandate, leaves threads "for tomorrow", and even writes a
    "Setup for next episode" line — i.e. the finale does not read as a finale.

    This block is appended LAST in the user prompt so it overrides the earlier
    cliffhanger mandate (later instructions win — the same principle
    _build_script_system relies on for the length override)."""
    if not is_finale_episode(s, num):
        return ''
    fin = (s or {}).get('finale') or {}
    desc = (fin.get('description') or '').strip()
    batch = is_batch_mode(s)
    if batch:
        a, b = chunk_range(s, num)
        unit_line = (
            f'║ 🏁  THIS CHUNK CONTAINS THE SERIES FINALE — sub-eps {a}–{b}.            🏁 ║\n'
        )
        scope = (
            f'All sub-episodes BEFORE the last one (sub-ep {b}) still end on their own\n'
            f'cliffhangers as usual. But the LAST sub-episode (sub-ep {b}) is the SERIES\n'
            f'FINALE — it must FULLY RESOLVE the series and end conclusively, NOT on a\n'
            f'cliffhanger. There is no chunk {int(num)+1}.\n'
        )
    else:
        unit_line = (
            f'║ 🏁🏁🏁  THIS IS THE SERIES FINALE — Ep {int(num)}. THERE IS NO Ep {int(num)+1}.  🏁🏁🏁 ║\n'
        )
        scope = (
            f'This OVERRIDES every "end on a cliffhanger" / "FORBIDDEN: wrap-up" rule\n'
            f'above (including the HARD CONTRACT and the per-episode instruction). Those\n'
            f'rules are for mid-season episodes. A cliffhanger here is a HARD FAILURE.\n'
        )
    return (
        '\n\n'
        '╔══════════════════════════════════════════════════════════════════════╗\n'
        + unit_line +
        '╚══════════════════════════════════════════════════════════════════════╝\n'
        + scope +
        '\nTHE FINALE CONTRACT — all mandatory:\n'
        '  1. CLOSE EVERY OPEN THREAD on screen, in THIS episode. No "...promised for\n'
        '     tomorrow", no "to be addressed", no "reckoning later", no deferred fate.\n'
        '     Every character the finale touches gets a decided, on-screen outcome.\n'
        '     The viewer must leave with NO open questions about the main plot.\n'
        '  2. EXECUTE THE PINNED FINALE EVENTS exactly — the specified characters,\n'
        '     reconciliations, judgments, reunions and FINAL IMAGE below MUST happen,\n'
        '     not similar substitutes.\n'
        '  3. DELIVER CATHARSIS — the emotional payoff the whole series built toward\n'
        '     (the protagonist\'s definitive triumph / reckoning / reunion). Earned\n'
        '     resolution is the POINT of a finale, not a "wrap-up" to be avoided.\n'
        '  4. END ON A CONCLUSIVE FINAL BEAT — a "button" that signals THE END: a\n'
        '     settled final image and a last line that lands with finality. NOT a new\n'
        '     arrival, NOT a new threat, NOT a new mystery, NOT a hook into a next\n'
        '     episode.\n'
        '  5. FORBIDDEN in the finale: "Setup for next episode", "to be continued",\n'
        '     any teaser for an episode that does not exist, any NEW unresolved\n'
        '     question introduced in the closing beat.\n'
        '  6. In EPISODE NOTES write literally: "Cliffhanger type: NONE — SERIES\n'
        '     FINALE (full resolution)" and "Setup for next episode: NONE — series\n'
        '     complete".\n'
        '\nPINNED FINALE — this is the END STATE you must deliver in full:\n'
        f'{desc}\n'
        '════════════════════════════════════════════════════════════════════════\n'
    )


# ── Finale bridge plan — concrete plot bridge from current state to finale ───
_BRIDGE_CACHE = {}  # in-memory: (sid, finale_ep, finale_hash, last_ep_with_script) -> plan dict


def build_finale_bridge_plan(s, current_ep):
    """Generate a concrete plot bridge from current series state to the pinned finale.

    The problem this solves: prev_script keeps the writer in the existing plot thread,
    while the finale describes events that may diverge. Without an explicit bridge,
    the writer just continues prev_script and never converges to the finale.

    This function asks Haiku to produce a per-episode plan (current_ep ... finale_ep),
    where each episode is described as ONE sentence of "what concretely happens that
    moves the plot toward the finale." The plan is cached per series-state hash so
    repeated generation in close succession is cheap.

    Returns a dict {block, this_beat, finale_ep} or {} if not applicable.
    """
    _EMPTY = {}
    fin = (s or {}).get('finale') or None
    if not fin or not (fin.get('description') or '').strip():
        return _EMPTY
    try:
        fin_ep = int(fin.get('episode', 0) or 0)
    except (TypeError, ValueError):
        return _EMPTY
    if fin_ep < current_ep:
        return _EMPTY  # finale already past
    distance = fin_ep - current_ep
    if distance > 12:
        # Too far out — bridge plan would be vague; rely on trajectory_block alone.
        return _EMPTY

    sid = s.get('id', '')
    # Cache key: series id + finale ep + hash of finale text + last episode with a script
    try:
        all_eps = sorted(
            [e for e in list_episodes(sid) if (e.get('script') or '').strip()],
            key=lambda e: int(e.get('number', 0) or 0),
        )
        last_with_script = all_eps[-1].get('number', 0) if all_eps else 0
    except Exception:
        all_eps = []
        last_with_script = 0
    fin_hash = abs(hash(fin.get('description', '') + str(fin_ep))) % (10 ** 8)
    cache_key = (sid, fin_ep, fin_hash, last_with_script, current_ep)
    cached = _BRIDGE_CACHE.get(cache_key)
    if cached:
        return cached

    # Build research context for Haiku: cast + canon + last 5 episode synopses + finale
    cast_inline = ', '.join((c.get('name') or '').strip()
                            for c in (s.get('characters') or [])
                            if (c.get('name') or '').strip())
    try:
        canon = load_canon(sid)
        canon_block = _format_canon_for_prompt(canon, max_facts=20, max_timeline=6)
    except Exception:
        canon_block = ''

    # Last 5 episodes worth of synopses for grounding
    try:
        prior = sorted(
            [e for e in list_episodes(sid) if (e.get('synopsis') or '').strip()
             and int(e.get('number', 0) or 0) < current_ep],
            key=lambda e: int(e.get('number', 0) or 0),
        )[-5:]
        prior_lines = []
        for e in prior:
            n = e.get('number', '?')
            syn = (e.get('synopsis') or '').strip().replace('\n', ' ')[:220]
            prior_lines.append(f"  Ep{n}: {syn}")
        prior_block = '\n'.join(prior_lines) if prior_lines else '(no prior episodes)'
    except Exception:
        prior_block = '(no prior episodes)'

    # Also include checkpoints between current and finale
    cps_between = sorted(
        [c for c in (s.get('checkpoints') or [])
         if current_ep <= int(c.get('episode', 0) or 0) < fin_ep
         and (c.get('description') or '').strip()],
        key=lambda c: int(c.get('episode', 0) or 0),
    )
    cps_block = ''
    if cps_between:
        cps_block = 'CHECKPOINTS BETWEEN HERE AND FINALE:\n' + '\n'.join(
            f"  Ep{c['episode']}: {c['description'].strip()}" for c in cps_between
        ) + '\n\n'

    prompt = (
        f'You are a TV showrunner planning the BRIDGE from the current state of a series to its pinned finale.\n\n'
        f'SERIES: "{s.get("title", "")}"\n'
        f'CANONICAL CAST: {cast_inline}\n\n'
        f'CANON STATE:\n{canon_block}\n\n'
        f'LAST 5 EPISODE SYNOPSES (current state of the plot):\n{prior_block}\n\n'
        f'{cps_block}'
        f'FINALE (Ep {fin_ep}) — this is the END STATE, after all bridge episodes have played out:\n{fin["description"].strip()}\n\n'
        f'TASK: write a step-by-step bridge plan from Ep {current_ep} through Ep {fin_ep} (finale).\n'
        f'For EACH episode in the range Ep {current_ep}..Ep {fin_ep}, write ONE sentence in Russian describing the '
        f'concrete plot beat that MOVES THE STORY TOWARD THE FINALE — and clearly indicate the '
        f'TEMPORAL STATE of major characters (free, arrested, alive, etc.) at the START of that episode.\n\n'
        f'CRITICAL RULES:\n'
        f'• TEMPORAL DISCIPLINE — the FINALE describes the END STATE of Ep {fin_ep}. Any major status change '
        f'  mentioned in the finale (arrest, sentencing, exposure, death, marriage, inheritance) has NOT yet '
        f'  happened in earlier bridge episodes. Example: if finale says "Vivian is sentenced to life", then '
        f'  in Eps {current_ep}..{fin_ep - 1} Vivian is STILL FREE, still living her normal life, possibly '
        f'  evading suspicion. Do NOT place her in prison early.\n'
        f'• Each bridge episode must build the EVIDENCE / PRESSURE / SETUP that makes the finale\'s climax '
        f'  inevitable — depositions, autopsies, financial subpoenas, confrontations, allies arriving — '
        f'  NOT the finale events themselves.\n'
        f'• The plot in last 5 synopses may have diverged from finale events. Your job is to BRIDGE the divergence — '
        f'  pick the plot threads from current state that can plausibly converge to the finale, '
        f'  and PARK or RESOLVE-OFFSCREEN any threads that the finale ignores.\n'
        f'• If a subplot in recent episodes (e.g. "hunt for Dr. X") doesn\'t appear in the finale, '
        f'  give it a quick wrap or fold it back into a finale-relevant beat — do NOT let it dominate the bridge.\n'
        f'• Use ONLY characters from the canonical cast. NEVER invent new named characters.\n'
        f'• Each bridge step must be a concrete event (someone does something, evidence surfaces, '
        f'  a confrontation happens) — not vague phrases like "tensions rise" or "secrets are uncovered".\n'
        f'• The LAST step (Ep {fin_ep}) must match the finale description directly.\n\n'
        f'OUTPUT FORMAT — strict JSON:\n'
        f'{{"bridge": [{{"ep": <int>, "state_before": "<one short Russian sentence: where major characters '
        f'are at start of this episode (e.g. \'Vivian still living at the estate, posing as innocent\')>", '
        f'"beat": "<one Russian sentence: concrete event THIS episode>"}}, ...]}}\n'
        f'Include exactly one entry per episode from {current_ep} to {fin_ep} inclusive.'
    )

    print(f'[bridge-plan] generating for ep {current_ep} → finale ep {fin_ep} (distance {distance})', flush=True)
    try:
        raw = claude_ask_fast(
            prompt,
            system=(
                'You are a TV showrunner. Plan plot bridges that converge to a known ending. '
                'Use only characters from the provided cast — never invent new named characters. '
                'Every beat must logically derive from finale events or set them up. '
                'Return strict JSON, no markdown.'
            ),
        )
        data = json.loads(strip_json(raw))
        bridge = data.get('bridge') or []
    except Exception as e:
        print(f'[bridge-plan] FAILED: {e}', flush=True)
        _log_event('WARN', 'finale_bridge_plan_failed', err=str(e)[:200])
        return _EMPTY

    if not bridge:
        print('[bridge-plan] empty bridge from LLM', flush=True)
        return _EMPTY
    print(f'[bridge-plan] generated {len(bridge)} beats', flush=True)
    for entry in bridge:
        try:
            ep_n = entry.get('ep')
            beat = (entry.get('beat') or '').strip()[:120]
            print(f'[bridge-plan]   Ep{ep_n}: {beat}', flush=True)
        except Exception:
            pass

    # Render the plan as a high-visibility block, highlighting THIS episode's job.
    lines = [
        '╔══════════════════════════════════════════════════════════════════╗',
        '║ 🌉 FINALE BRIDGE PLAN — мост от текущей серии к финалу ║',
        '╚══════════════════════════════════════════════════════════════════╝',
        f'Финал зафиксирован на Ep {fin_ep}. Осталось {distance + 1} серий до финала.',
        'План моста (по сериям) — каждая серия должна выполнить СВОЙ шаг:',
        '',
    ]
    this_state_before = ''
    for entry in bridge:
        try:
            ep_n = int(entry.get('ep'))
        except (TypeError, ValueError):
            continue
        beat = (entry.get('beat') or '').strip()
        state_before = (entry.get('state_before') or '').strip()
        if not beat:
            continue
        if ep_n == current_ep:
            lines.append(f'  ▶▶▶ Ep {ep_n} (ЭТА СЕРИЯ — ОБЯЗАТЕЛЬНОЕ СОБЫТИЕ):')
            if state_before:
                lines.append(f'      [состояние мира на старте серии]: {state_before}')
                this_state_before = state_before
            lines.append(f'      [beat]: {beat}')
        elif ep_n == fin_ep:
            lines.append(f'  🏁 Ep {ep_n} (ФИНАЛ): {beat}')
        else:
            tag = 'СЛЕДУЮЩАЯ' if ep_n == current_ep + 1 else f'+{ep_n - current_ep}'
            extra = f' [состояние: {state_before}]' if state_before else ''
            lines.append(f'  Ep {ep_n} ({tag}){extra}: {beat}')
    lines += [
        '',
        f'HARD RULE: ЭТА СЕРИЯ (Ep {current_ep}) ОБЯЗАНА выполнить свой beat выше. '
        f'Не уходи в подсюжет которого нет в этом плане. Если prev_script тянет тебя в '
        f'другую сторону — этот план перевешивает: либо аккуратно сверни уходящие линии, '
        f'либо переориентируй на финальные события.',
        '',
        f'TEMPORAL RULE: финальные события (Ep {fin_ep}) — ЕЩЁ НЕ ПРОИЗОШЛИ. Если финал говорит '
        f'"X арестован", "Y осуждён", "Z раскрыт" — в этой серии (Ep {current_ep}) X/Y/Z ВСЕ ЕЩЁ '
        f'в свободном/неразоблачённом состоянии. Эта серия строит ЕВИДЕНЦИЮ или ДАВЛЕНИЕ, '
        f'которые в итоге приведут к финальному событию. Не перескакивай в "пост-финальное" состояние.',
        '═══════════════════════════════════════════════════════════════════',
        '',
    ]
    plan_text = '\n'.join(lines)
    # Extract THIS episode's beat for separate (loud) injection downstream
    this_beat = ''
    for entry in bridge:
        try:
            if int(entry.get('ep')) == current_ep:
                this_beat = (entry.get('beat') or '').strip()
                break
        except (TypeError, ValueError):
            continue
    result = {'block': plan_text, 'this_beat': this_beat,
              'this_state_before': this_state_before, 'finale_ep': fin_ep}
    _BRIDGE_CACHE[cache_key] = result
    # Cap cache size — drop oldest
    if len(_BRIDGE_CACHE) > 64:
        for k in list(_BRIDGE_CACHE.keys())[:32]:
            _BRIDGE_CACHE.pop(k, None)
    return result


@app.route('/api/series/<sid>/archive', methods=['POST'])
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

