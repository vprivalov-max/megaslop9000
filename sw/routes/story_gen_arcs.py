"""Story generation routes: arcs, milestones, synopses, script history,
doctor, character extraction."""
import json
import time
import uuid

from flask import jsonify, request

from sw.anthro import (_anthro_world_block, _casting_aesthetics_block,
                       _is_anthro_world, _revision_instructions_block)
from sw.core import app
from sw.jsonutils import strip_json
from sw.llm import _resolve_writer_model, claude_ask_fast, llm_ask
from sw.logging_utils import _log_event
# routes->routes: landmarks hosts the trajectory/bridge builders used by
# writer prompts; no import cycle (landmarks never imports story_gen).
from sw.routes.landmarks import build_finale_bridge_plan, build_trajectory_block
# routes->routes: ideas hosts the beats-episode-block builder (no import cycle).
from sw.routes.ideas import _series_beats_episode_block
from sw.storage import (TOTAL_SUB_EPS, _extract_end_position,
                        _sync_script_outfits, anchor_chunks, batch_size,
                        chunk_count, chunk_range, is_batch_mode, list_episodes,
                        load_episode, load_series, save_episode, save_series)
from sw.story_prompts import (_build_cast_block, _canonical_cast_block,
                              _format_mode_block, _format_mode_of, _outfit_ids)
from sw.story_logic import (MILESTONE_EPS, _WRITER_SYSTEM, audit_logic_holes,
                            audit_script, build_logic_brief, doctor_script,
                            extract_canon_updates, rollback_canon_for_episode)

@app.route('/api/series/<sid>/generate-arcs', methods=['POST'])
def generate_arcs(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    synopsis = s.get('synopsis', '')
    if not synopsis:
        return jsonify({'error': 'Сначала создай синопсис сериала'}), 400
    prompt = (
        f'Series: "{s["title"]}"\nSynopsis: "{synopsis}"\n\n'
        "Generate exactly 3 different arc variants for this 70-episode short drama for TikTok/Reels. "
        "Each arc describes: setup (ep 1-15), escalation (ep 15-55), climax & resolution (ep 55-70). "
        "Make each variant meaningfully different in twists and ending. "
        'Return JSON: {"arcs": [{"title": "...", "summary": "2-3 paragraphs"}, ...]}'
    )
    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
        arcs = data.get('arcs', data)
        s['arc_variants'] = arcs
        save_series(sid, s)
        return jsonify(arcs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/confirm-arc', methods=['POST'])
def confirm_arc(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    arc = (request.json or {}).get('arc', '').strip()
    if not arc:
        return jsonify({'error': 'Выбери или напиши арку'}), 400
    s['arc'] = arc
    s['stage'] = 2
    s.pop('arc_variants', None)
    save_series(sid, s)
    return jsonify(s)


@app.route('/api/series/<sid>/generate-milestones', methods=['POST'])
def generate_milestones(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    # Auto-generate the three anchor points. In single mode → eps 1/10/70.
    # In batch mode → chunks 1 / chunk-of-10 / last (e.g. for bs=5: chunks 1, 2, 14).
    synopsis = s.get('synopsis', '')
    arc = s.get('arc', '')
    batch = is_batch_mode(s)
    bs = batch_size(s)
    a1, a2, a3 = anchor_chunks(s) if batch else (1, 10, TOTAL_SUB_EPS)
    cast_pin = _canonical_cast_block(s) + _revision_instructions_block(s)
    series_format_mode = _format_mode_of(s)
    fmt_block = _format_mode_block(s, sections=['title_rule', 'synopsis_rule', 'episode_rule', 'pace_rule'])
    if series_format_mode == 'instagram_series':
        # 6-10 episodes total. Anchors: 1 / mid (3-4) / finale (6 or 7).
        ig_a1, ig_a2, ig_a3 = 1, 3, 6
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{synopsis}"\nArc: "{arc}".\n\n'
            + fmt_block
            + cast_pin +
            f'This is an INSTAGRAM SERIES — 6-10 episodes total, compact serialized mini-drama. '
            f'Each episode is a self-contained chapter with SHARP HOOK + clear conflict + CLIFFHANGER.\n'
            f'Write synopsis (2-3 punchy sentences) for THREE anchor episodes: {ig_a1}, {ig_a2}, {ig_a3}.\n'
            f'Ep {ig_a1} — pilot: drops the viewer into the central conflict / secret / hook in the first '
            f'5 seconds. Establishes who is at stake and what the lie/intrigue is. Ends on a cliffhanger '
            f'that locks the viewer into the season.\n'
            f'Ep {ig_a2} — midseason reversal: the situation viewers thought they understood gets flipped. '
            f'A secret breaks, an ally turns, a deadline closes in. The arc accelerates. New location is '
            f'fine — the plot moves wherever it needs to. Ends on a sharper cliffhanger than ep {ig_a1}.\n'
            f'Ep {ig_a3} — season finale: all threads converge. The central conflict resolves (or '
            f'deliberately fractures into a season-2 promise). Earn the ending — no anticlimax, no '
            f'"warm hug" close.\n'
            'FORBIDDEN: "introduces the recurring cast / weekly hook", "soft opening hook", "satisfying '
            'beat — no cliffhanger", "slice-of-life", "sitcom", "warm earned arc", "see-you-next-week", '
            'anthology framing, location-locked premises (kitchen-only / café-only). '
            'FORBIDDEN plot engines: lawsuits, court hearings, trials, depositions, legal filings, '
            'tenant union files suit, "she takes them to court", "the judge rules". Drama lives in '
            'face-to-face confrontation / chase / betrayal / reveal / blackmail / escape — NOT in '
            'courtrooms. If the synopsis above leans on legal escalation, REPLACE the anchor beats '
            'with personal confrontations and direct action.\n'
            'Locations vary across the three anchors as the story demands.\n'
            f'Return JSON: {{"milestones": {{"{ig_a1}": "...", "{ig_a2}": "...", "{ig_a3}": "..."}}}}'
        )
    elif batch:
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{synopsis}"\nArc: "{arc}".\n\n'
            + fmt_block
            + cast_pin +
            f'BATCH MODE: each storage chunk = {bs} consecutive ~1-min sub-episodes (~{bs*60}s total = ~{bs} min). '
            f'Total chunks = {chunk_count(s)}. There are NO standalone episodes in the UI — only chunks.\n'
            f'Write synopsis (4–6 sentences) for THREE anchor chunks: {a1} (covers sub-eps {chunk_range(s,a1)[0]}–{chunk_range(s,a1)[1]}), '
            f'{a2} (sub-eps {chunk_range(s,a2)[0]}–{chunk_range(s,a2)[1]}), '
            f'{a3} (sub-eps {chunk_range(s,a3)[0]}–{chunk_range(s,a3)[1]}).\n'
            f'Chunk {a1} — premiere chunk: hooks with immediate stakes, contains {bs} mini-cliffhangers, ends locking the viewer in.\n'
            f'Chunk {a2} — first major turning point: the situation viewers thought they understood gets completely flipped. Mid-series reversal.\n'
            f'Chunk {a3} — finale chunk: maximum stakes, all threads converge in the last {bs} sub-episodes.\n'
            'SHORT DRAMA: each synopsis must outline the chunk\'s 5 mini-cliffhangers + main midpoint reversal + chunk-end cliffhanger.\n'
            f'Return JSON: {{"milestones": {{"{a1}": "...", "{a2}": "...", "{a3}": "..."}}}}'
        )
    else:
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{synopsis}"\nArc: "{arc}".\n\n'
            + fmt_block
            + cast_pin +
            "Write a synopsis (2-4 sentences) for exactly THREE anchor episodes: 1, 10, and 70.\n"
            "Ep 1 — series premiere: hooks with immediate stakes, establishes the central conflict and main character, ends on a cliffhanger that locks the viewer in.\n"
            "Ep 10 — first major turning point: the situation the viewer thought they understood gets completely flipped. A secret explodes or a power shift happens that changes everything.\n"
            "Ep 70 — finale: maximum stakes, all threads converge, the central conflict resolves (or deliberately doesn't). Must feel earned.\n"
            "SHORT DRAMA FORMAT: each synopsis must include a reversal and end on the biggest cliffhanger possible for that point in the series. No slow burns.\n"
            'Return JSON: {"milestones": {"1": "...", "10": "...", "70": "..."}}'
        )
    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
        milestones = data.get('milestones', {})
        s.setdefault('milestone_synopses', {}).update({str(k): v for k, v in milestones.items()})
        save_series(sid, s)
        return jsonify(milestones)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/milestones/<int:ep_num>', methods=['PUT'])
def update_milestone(sid, ep_num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    s.setdefault('milestone_synopses', {})[str(ep_num)] = (request.json or {}).get('synopsis', '')
    save_series(sid, s)
    return jsonify({'ok': True})


@app.route('/api/series/<sid>/milestones/<int:ep_num>/regenerate', methods=['POST'])
def regenerate_milestone(sid, ep_num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ms = s.get('milestone_synopses', {})
    batch = is_batch_mode(s)
    bs = batch_size(s)
    unit = (lambda k: f'Chunk {k} (sub-eps {chunk_range(s, int(k))[0]}–{chunk_range(s, int(k))[1]})') if batch else (lambda k: f'Ep {k}')
    prev = '\n'.join(f'{unit(k)}: {v}' for k, v in sorted(ms.items(), key=lambda x: int(x[0])) if int(k) < ep_num)
    nxt  = '\n'.join(f'{unit(k)}: {v}' for k, v in sorted(ms.items(), key=lambda x: int(x[0])) if int(k) > ep_num)
    target_label = unit(ep_num)
    series_format_mode = _format_mode_of(s)
    extra = (
        f' This is a CHUNK of {bs} consecutive ~1-min sub-episodes — outline the {bs} mini-cliffhangers + midpoint reversal + chunk-end cliffhanger.'
        if batch and series_format_mode == 'short_drama' else
        f' This is a CHUNK of {bs} consecutive ~1-min self-contained chapters — each ends on its own cliffhanger, plus a chunk-end turn that escalates into the next chunk.'
        if batch else ''
    )
    format_tail = (
        'INSTAGRAM SERIES FORMAT: self-contained chapter of a serialized story. Sharp cold-open hook, '
        'one clear conflict, CLIFFHANGER ending that pulls into the next episode. Dialogue clipped — '
        'conflict readable in two exchanges. NO sitcom / slice-of-life / "satisfying close" energy. '
        'FORBIDDEN plot engines: lawsuits, courts, trials, legal filings, "she sues them", '
        '"the case goes to court". Drama lives in face-to-face confrontation / chase / betrayal / '
        'reveal / blackmail — NOT in courtrooms or paperwork.'
        if series_format_mode == 'instagram_series' else
        'SHORT DRAMA FORMAT: open in conflict (not setup), include a mid-point reversal, end on a cliffhanger.'
    )
    prompt = (
        f'Series: "{s["title"]}" | Arc: "{s.get("arc","")}".\n'
        + _format_mode_block(s, sections=['synopsis_rule', 'episode_rule'])
        + _canonical_cast_block(s) +
        f'Previous milestones:\n{prev or "—"}\n'
        f'Next milestones:\n{nxt or "—"}\n\n'
        f'Write a new synopsis (4-6 sentences for chunks, 2-4 for episodes) for {target_label} that fits logically between the above.{extra} '
        f'{format_tail} '
        'Return JSON: {"synopsis": "..."}'
    )
    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
        syn = data.get('synopsis', '')
        s.setdefault('milestone_synopses', {})[str(ep_num)] = syn
        save_series(sid, s)
        return jsonify({'synopsis': syn})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/extract-from-story', methods=['POST'])
def extract_from_story(sid):
    """Extract characters and locations from synopsis/arc/milestones and create them in the series."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404

    ms = s.get('milestone_synopses', {})
    ms_text = '\n'.join(f'Ep {k}: {v}' for k, v in sorted(ms.items(), key=lambda x: int(x[0])) if v)
    story_text = '\n\n'.join(filter(None, [
        f'Synopsis: {s.get("synopsis","")}',
        f'Arc: {s.get("arc","")}',
        f'Milestone synopses:\n{ms_text}' if ms_text else '',
    ]))

    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
        f'{story_text}\n\n'
        'Extract all NAMED characters and specific locations from this story. '
        'For each character infer: name, gender (male/female), a 1-sentence description of their role, '
        'and a 1-sentence appearance description (for AI image generation). '
        'For each location infer: name and a 1-sentence description. '
        'CRITICAL — character "name" AND location "name" fields MUST be in ENGLISH (e.g. "Hotel Room", '
        '"Base HQ Office", "Boardroom", "Penthouse"). FORBIDDEN: Russian/Cyrillic names like '
        '"Гостиничный номер", "Военная база" — translate to English. The "description" field stays Russian. '
        'Only include characters and locations that are meaningfully present — no extras. '
        'Return JSON:\n'
        '{"characters": [{"name":"...","gender":"female","description":"...","appearance":"..."},...], '
        '"locations": [{"name":"...","description":"..."},...] }'
    )
    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    existing_char_names = {c['name'].lower() for c in s.get('characters', [])}
    existing_loc_names  = {l['name'].lower() for l in s.get('locations',  [])}
    added_chars, added_locs = [], []

    for c in data.get('characters', []):
        if c.get('name','').lower() not in existing_char_names:
            char = {
                'id': str(uuid.uuid4())[:8],
                'name': c['name'],
                'description': c.get('description', ''),
                'appearance':  c.get('appearance', ''),
                'gender':      c.get('gender', 'female'),
                'voice_id':    '',
                'ref_images':  [],
                'outfits':     [],
            }
            s.setdefault('characters', []).append(char)
            added_chars.append(char['name'])

    for l in data.get('locations', []):
        if l.get('name','').lower() not in existing_loc_names:
            loc = {
                'id': str(uuid.uuid4())[:8],
                'name': l['name'],
                'description': l.get('description', ''),
                'ref_images':  [],
            }
            s.setdefault('locations', []).append(loc)
            added_locs.append(loc['name'])

    save_series(sid, s)
    # Don't trigger autogen here. The frontend acceptScript flow shows a modal
    # FIRST so the user can drag photos / mark «🚫 Не генерить» / pick custom
    # styles. Frontend kicks off autogen explicitly via /auto-generate/sweep
    # in _proceedAfterAccept after the modal closes. Auto-firing here meant
    # generation started BEFORE the user even saw the modal, defeating the
    # whole point.
    return jsonify({
        'added_characters': added_chars,
        'added_locations':  added_locs,
        'series': s,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/script-history', methods=['GET'])
def list_script_history(sid, num):
    """List archived previous versions of an episode's script."""
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    history = ep.get('script_history', []) or []
    cur = (ep.get('script') or '')
    return jsonify({
        'current': {
            'length': len(cur),
            'preview': cur[:300],
        },
        'versions': [
            {
                'index': i,
                'ts': h.get('ts'),
                'reason': h.get('reason', '?'),
                'length': len(h.get('script', '')),
                'preview': (h.get('script') or '')[:300],
            }
            for i, h in enumerate(history)
        ],
    })


@app.route('/api/series/<sid>/episodes/<int:num>/script-history/<int:idx>', methods=['GET'])
def get_script_history_full(sid, num, idx):
    """Return the full text of one archived version (for preview before restore)."""
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    history = ep.get('script_history', []) or []
    if idx < 0 or idx >= len(history):
        return jsonify({'error': 'index out of range'}), 400
    h = history[idx]
    return jsonify({
        'index': idx,
        'ts': h.get('ts'),
        'reason': h.get('reason', '?'),
        'script': h.get('script', ''),
    })


@app.route('/api/series/<sid>/episodes/<int:num>/script-restore/<int:idx>', methods=['POST'])
def restore_script_version(sid, num, idx):
    """Restore a previous version. The current script is archived as a new history entry
    so the restore itself is reversible."""
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    history = ep.get('script_history', []) or []
    if idx < 0 or idx >= len(history):
        return jsonify({'error': 'index out of range'}), 400

    # Save current as new history entry before swapping
    cur_script = (ep.get('script') or '').strip()
    if cur_script:
        history.append({
            'ts': time.time(),
            'reason': 'pre-restore',
            'script': cur_script[:80000],
        })
    # Pop the chosen version → make it current
    chosen = history.pop(idx)
    ep['script'] = chosen.get('script', '')
    ep['script_history'] = history[-10:]
    save_episode(sid, num, ep)
    # Re-sync outfits — restored script may reference labels that never made
    # it into series.json (e.g. restoring a version generated before
    # outfit-sync code was working).
    new_outfits = []
    try:
        new_outfits = _sync_script_outfits(sid, ep['script'])
    except Exception as _oe:
        _log_event('WARN', 'outfit_sync_after_restore_failed', err=str(_oe)[:200])
    return jsonify({
        'restored_from': {
            'ts': chosen.get('ts'),
            'reason': chosen.get('reason'),
        },
        'script': ep['script'],
        '_new_outfits': new_outfits,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/doctor-script', methods=['POST'])
def doctor_episode_script(sid, num):
    """Run the logic-hole auditor on the current script and apply minimal fixes.
    Two-stage: AUDIT (find holes) → DOCTOR (rewrite with surgical edits).
    Returns either the fixed script (and saves it) or — if user passed dry_run —
    just the violations list so they can review before applying."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'episode not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Сценарий пустой — нечего лечить'}), 400

    body = request.json or {}
    dry_run = bool(body.get('dry_run'))
    extra_notes = (body.get('extra_notes') or '').strip()  # user can pass own pain points
    # Optional: caller passes a pre-selected list of violations (e.g. user ticked
    # a subset of issues from the inline checker UI) — skip re-auditing in that case.
    selected_violations = body.get('violations')

    if isinstance(selected_violations, list) and not dry_run:
        violations = [v for v in selected_violations if isinstance(v, dict)]
        report = {'passes': not violations, 'violations': violations}
        critical = [v for v in violations if v.get('severity', 'critical') == 'critical']
        minor    = [v for v in violations if v.get('severity') != 'critical']
    else:
        # Stage 1: audit (with previous-episode context — see audit_logic_holes)
        report = audit_logic_holes(sid, num, script)
        violations = report.get('violations', [])
        critical = [v for v in violations if v.get('severity') == 'critical']
        minor    = [v for v in violations if v.get('severity') != 'critical']

    if dry_run:
        return jsonify({
            'violations': violations,
            'critical_count': len(critical),
            'minor_count': len(minor),
            'audit_error': report.get('audit_error'),
        })

    # Optionally inject user's extra notes as additional violations to fix
    if extra_notes:
        violations.append({
            'type': 'user_note',
            'severity': 'critical',
            'where': 'user-specified',
            'explanation': extra_notes,
            'fix': 'apply the user\'s requested change',
        })

    if not violations:
        return jsonify({
            'changed': False,
            'message': 'Логических дыр не найдено — сценарий чист.',
            'violations': [],
        })

    # Stage 2: doctor
    try:
        fixed = doctor_script(sid, num, script, violations).strip()
    except Exception as e:
        return jsonify({'error': f'Doctor failed: {e}', 'violations': violations}), 500

    # Save backup of previous script + new version
    ep.setdefault('script_history', []).append({
        'ts': time.time(),
        'reason': 'doctor-script',
        'script': script[:50000],  # cap
    })
    ep['script_history'] = ep['script_history'][-10:]
    ep['script'] = fixed
    end_pos = _extract_end_position(fixed)
    if end_pos:
        ep['end_position'] = end_pos
    ep['last_doctor_run'] = {
        'ts': time.time(),
        'violations_fixed': len(violations),
        'violations': violations,
    }
    save_episode(sid, num, ep)

    # Re-sync outfits — doctor may have rewritten [BLOCKING] (adding/changing
    # OUTFIT labels) and we want any new outfit objects auto-queued for image
    # generation just like the manual save/accept path.
    new_outfits = []
    try:
        new_outfits = _sync_script_outfits(sid, fixed)
    except Exception as _oe:
        _log_event('WARN', 'outfit_sync_after_doctor_failed', err=str(_oe)[:200])

    return jsonify({
        'changed': True,
        'script': fixed,
        'violations_fixed': len(violations),
        'violations': violations,
        '_new_outfits': new_outfits,
    })
