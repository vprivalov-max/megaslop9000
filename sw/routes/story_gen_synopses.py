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
from sw.greenlight import get_playbook_block
from sw.story_logic import (MILESTONE_EPS, _WRITER_SYSTEM, audit_logic_holes,
                            audit_script, build_logic_brief, doctor_script,
                            extract_canon_updates, rollback_canon_for_episode)

@app.route('/api/series/<sid>/episodes/<int:num>/extract-characters', methods=['POST'])
def extract_characters_from_script(sid, num):
    """Extract characters AND locations from the script, AND re-verify which already-known
    characters/locations are actually present in this episode's script (so dropped ones
    get unticked from the episode's checkboxes). Three things happen:
      1) Add brand-new speaking characters not yet in the series
      2) Add brand-new locations not yet in the series
      3) Rebuild ep['characters_used'] and ep['locations_used'] strictly from what
         the current script mentions (drops stale ticks)
    """
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'episode not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Сценарий пустой — сначала впиши или сгенерируй текст'}), 400

    existing_chars = s.get('characters', [])
    existing_locs  = s.get('locations', [])
    existing_char_lines = '\n'.join(
        f'  - {c["name"]} (id={c["id"]}) — appearance: {c.get("appearance","")[:160]}'
        for c in existing_chars
    ) or '  (none)'
    existing_loc_lines  = '\n'.join(f'  - {l["name"]} (id={l["id"]})' for l in existing_locs)  or '  (none)'

    director_notes = (ep.get('notes') or '').strip()
    notes_block = (
        f'DIRECTOR\'S NOTES (creative instructions from the user — TREAT AS HIGH PRIORITY):\n{director_notes}\n\n'
        if director_notes else ''
    )

    anthro_block = _anthro_world_block(s)
    casting_block = _casting_aesthetics_block(s)
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Series world: {(s.get("world_description") or "")[:600]}\n'
        f'Series synopsis: {(s.get("synopsis") or "")[:600]}\n\n'
        + anthro_block
        + casting_block
        + f'ALREADY KNOWN CHARACTERS in this series:\n{existing_char_lines}\n\n'
        f'ALREADY KNOWN LOCATIONS in this series:\n{existing_loc_lines}\n\n'
        f'{notes_block}'
        f'EPISODE {num} SCRIPT:\n{script}\n\n'
        f'ALREADY KNOWN ITEMS in this series (story-relevant props):\n'
        + '\n'.join(f"  - {it.get('name')} (id={it.get('id')})" for it in (s.get('items') or []))
        + ('\n  (none)\n' if not (s.get('items') or []) else '\n') + '\n'
        + 'YOUR JOB — return six lists in JSON:\n\n'
        '1) `present_character_ids` — IDs of ALREADY KNOWN characters who actually appear in this script '
        '(speak or are explicitly on screen). Drop any known character not in the script.\n'
        '2) `present_location_ids` — IDs of ALREADY KNOWN locations actually used as scene settings in this script.\n'
        '3) `new_characters` — speaking characters that are NOT in the known list and need to be created. '
        'Include characters with proper names (Emma, Daniel...) AND characters labeled by ROLE who have at least '
        'one dialogue line (Mother, Father, Doctor, Driver, Maid, Butler, Boss, Lawyer, Reporter...). Use the '
        'role label as the "name" field. EXCLUDE silent background extras and unlabeled phone voices.\n'
        '   ALSO: if the DIRECTOR\'S NOTES explicitly say a known character should be re-cast as a NEW separate '
        '   entry (e.g. "create a separate character entry for the new face"), add them here with a clearly '
        '   distinct name (e.g. "Emma_NewFace", "Daniel_v2").\n'
        '4) `new_locations` — distinct scene settings in the script that are NOT in the known list and need '
        'to be created. Locations come from scene headings (INT./EXT. NAME — DAY/NIGHT). Each unique location = one entry.\n'
        '5) `appearance_updates` — ONLY when DIRECTOR\'S NOTES explicitly say an EXISTING character\'s '
        '   appearance changes in-story (plastic surgery, recast, scar, drastic transformation, new face). '
        '   For each: provide the existing character\'s `id` from the known list, a NEW `appearance` sentence '
        '   in Russian (full physical description, since this REPLACES the old one — do NOT just describe the '
        '   change), and a 1-sentence `reason` in Russian explaining what the notes asked for. '
        '   When in doubt, leave this list empty. Do NOT update appearance based on script alone — only on '
        '   explicit director\'s notes. The old portrait will be discarded and regenerated from this new text.\n'
        '6) `new_items` — STORY-RELEVANT props that drive the plot and should have a generated reference image. '
        'STRICT criteria — include ONLY:\n'
        '   - Objects mentioned by name with PLOT significance (the murder weapon, the heroine\'s locket, '
        '     the will document, the flash drive, the briefcase of money, the sword, the talisman, '
        '     the blackmail letter, the photograph, the bottle of poison, evidence files)\n'
        '   - Objects characters fight over, hide, exchange, destroy, or treat as evidence\n'
        '   - Objects that recur across scenes/episodes\n'
        '   EXCLUDE casual everyday objects (cup, phone, keys, glass, generic chair) UNLESS they have explicit plot weight.\n'
        '   For each NEW ITEM infer:\n'
        '   - name: short concrete English noun phrase (e.g. "Manila Folder", "Black Silver Shard", "Locket of Sarah")\n'
        '   - description: 1 sentence in RUSSIAN — what it looks like + its narrative role\n\n'
        'For each NEW CHARACTER infer:\n'
        '  - name: exact label as it appears in the script (English / Latin letters)\n'
        '  - gender: "male" or "female"\n'
        '  - description: 1 sentence in RUSSIAN about their role in the story\n'
        + ('  - appearance: 1 detailed sentence in RUSSIAN describing physical look. '
           'MANDATORY first word: the ANTHROPOMORPHIC SPECIES of this character '
           '(e.g. "Антропоморфный кролик-самка..." / "Anthropomorphic rabbit female..."). '
           'Then fur color/pattern, ears, snout, tail, build, and style of dress. '
           'No human anatomy (no skin tone, no human hair, no human eyes) — replace with species-appropriate features.\n\n'
           if _is_anthro_world(s) else
           '  - appearance: 1 detailed sentence in RUSSIAN describing physical look (age, hair, eyes, build, '
           'style of dress) suitable as a prompt for AI image generation. Be specific.\n\n')
        + 'For each NEW LOCATION infer:\n'
        '  - name: short ENGLISH label as it appears in scene heading (e.g. "Hotel Suite", "Boardroom", "Hospital Corridor")\n'
        '  - description: 1 sentence in RUSSIAN about the place + atmosphere relevant to the scene\n\n'
        'Return JSON only:\n'
        '{\n'
        '  "present_character_ids": ["id1", "id2"],\n'
        '  "present_location_ids":  ["id3"],\n'
        '  "new_characters": [{"name":"...","gender":"female","description":"...","appearance":"..."}],\n'
        '  "new_locations":  [{"name":"...","description":"..."}],\n'
        '  "appearance_updates": [{"id":"existingId","appearance":"...","reason":"..."}],\n'
        '  "new_items":      [{"name":"...","description":"..."}]\n'
        '}'
    )
    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
    except Exception as e:
        return jsonify({'error': f'Не удалось разобрать ответ Claude: {e}'}), 500

    # 1) Add brand-new characters
    existing_char_names = {c['name'].lower() for c in existing_chars}
    valid_char_ids = {c['id'] for c in existing_chars}
    added_chars = []
    for c in data.get('new_characters', []):
        nm = (c.get('name') or '').strip()
        if not nm or nm.lower() in existing_char_names:
            continue
        new_id = str(uuid.uuid4())[:8]
        char = {
            'id': new_id,
            'name': nm,
            'description': c.get('description', ''),
            'appearance':  c.get('appearance', ''),
            'gender':      c.get('gender', 'female'),
            'voice_id':    '',
            'ref_images':  [],
            'outfits':     [],
        }
        s.setdefault('characters', []).append(char)
        added_chars.append(nm)
        existing_char_names.add(nm.lower())
        valid_char_ids.add(new_id)

    # 2) Add brand-new locations
    existing_loc_names = {l['name'].lower() for l in existing_locs}
    valid_loc_ids = {l['id'] for l in existing_locs}
    added_locs = []
    for l in data.get('new_locations', []):
        nm = (l.get('name') or '').strip()
        if not nm or nm.lower() in existing_loc_names:
            continue
        new_id = str(uuid.uuid4())[:8]
        loc = {
            'id': new_id,
            'name': nm,
            'description': l.get('description', ''),
            'ref_images':  [],
        }
        s.setdefault('locations', []).append(loc)
        added_locs.append(nm)
        existing_loc_names.add(nm.lower())
        valid_loc_ids.add(new_id)

    # 2.6) Add brand-new story-relevant items
    existing_item_names = {(it.get('name') or '').lower() for it in (s.get('items') or [])}
    added_items = []
    for it in (data.get('new_items') or []):
        nm = (it.get('name') or '').strip()
        if not nm or nm.lower() in existing_item_names:
            continue
        new_id = str(uuid.uuid4())[:8]
        item = {
            'id': new_id,
            'name': nm,
            'description': it.get('description', ''),
            'ref_images':  [],
        }
        s.setdefault('items', []).append(item)
        added_items.append(nm)
        existing_item_names.add(nm.lower())

    # 2.5) Appearance updates — director's notes asked to refresh an existing
    #      character's look (new face, recast, scar, etc.). We replace the
    #      `appearance` text and clear the generated portrait + outfit images so
    #      autogen rebuilds them from the new description.
    appearance_updates = []
    char_by_id = {c['id']: c for c in s.get('characters', [])}
    for upd in (data.get('appearance_updates') or []):
        cid = (upd.get('id') or '').strip()
        new_appearance = (upd.get('appearance') or '').strip()
        reason = (upd.get('reason') or '').strip()
        if not cid or cid not in char_by_id or not new_appearance:
            continue
        char = char_by_id[cid]
        old_appearance = char.get('appearance', '')
        char['appearance'] = new_appearance
        # Wipe portrait so autogen regenerates it
        char['ref_images'] = []
        # Wipe outfit reference images too — they were generated against the old face
        for o in char.get('outfits', []) or []:
            o['ref_images'] = []
        appearance_updates.append({
            'id': cid,
            'name': char.get('name', cid),
            'previous': old_appearance,
            'new': new_appearance,
            'reason': reason,
        })

    save_series(sid, s)

    # 3) Rebuild episode's characters_used / locations_used. Take Claude's "present" lists,
    #    union with newly-added (which by definition appear in the script), and intersect
    #    with the post-add valid id set (so we never store ghost ids).
    present_char_ids = set(data.get('present_character_ids', []))
    present_loc_ids  = set(data.get('present_location_ids', []))
    # Newly-added entities appear in the script — include them
    new_char_id_set = {c['id'] for c in s['characters'] if c['name'] in added_chars}
    new_loc_id_set  = {l['id'] for l in s['locations']  if l['name'] in added_locs}
    final_chars = sorted((present_char_ids | new_char_id_set) & valid_char_ids)
    final_locs  = sorted((present_loc_ids  | new_loc_id_set)  & valid_loc_ids)

    prev_chars = set(ep.get('characters_used') or [])
    prev_locs  = set(ep.get('locations_used')  or [])
    ep['characters_used'] = final_chars
    ep['locations_used']  = final_locs
    # Mark cast extraction as user-confirmed — auto-heal paths can now safely
    # re-sync this episode's cast block (gated until user pressed the button).
    ep['cast_extracted'] = True
    save_episode(sid, num, ep)

    # NB: frontend triggers autogen explicitly (POST /auto-generate/sweep) after
    # the user closes the «Найдены новые персонажи/локации» modal. Auto-firing
    # here started generation before the modal even appeared — user saw images
    # being created they didn't yet have a chance to opt out of.

    # Build dropped-name reports for nicer UI feedback
    char_id2name = {c['id']: c['name'] for c in s.get('characters', [])}
    loc_id2name  = {l['id']: l['name'] for l in s.get('locations',  [])}
    dropped_chars = sorted(char_id2name.get(i, i) for i in (prev_chars - set(final_chars)) if i in char_id2name)
    dropped_locs  = sorted(loc_id2name.get(i,  i) for i in (prev_locs  - set(final_locs))  if i in loc_id2name)

    # 4) Synopsis — write/rewrite from the script so the synopsis textarea always
    #    matches what the user actually wrote in the script. Best-effort: if
    #    synopsis generation fails, we just keep the old one.
    synopsis_status = {'updated': False, 'previous': ep.get('synopsis', ''), 'new': None, 'error': None}
    try:
        syn_prompt = (
            f'Series: "{s.get("title","")}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
            f'EPISODE {num} SCRIPT:\n{script[:9000]}\n\n'
            'Write a 3–4 sentence synopsis IN RUSSIAN of what actually happens in this episode '
            '(based strictly on the script above). Cover: opening conflict, mid-episode reversal, '
            'ending cliffhanger. Name characters by their exact names. No vague spoilers — be concrete. '
            'Output ONLY the synopsis text, no preface, no quotes, no markdown.'
        )
        new_syn = (claude_ask_fast(
            syn_prompt,
            system='You are a short-drama editor writing concise episode synopses in Russian.'
        ) or '').strip()
        # Strip any wrapping quotes the model occasionally adds
        if new_syn and new_syn[0] in '"«' and new_syn[-1] in '"»':
            new_syn = new_syn[1:-1].strip()
        if new_syn:
            ep['synopsis'] = new_syn
            save_episode(sid, num, ep)
            synopsis_status = {
                'updated': True,
                'previous': synopsis_status['previous'],
                'new': new_syn,
                'error': None,
            }
    except Exception as e:
        synopsis_status['error'] = str(e)

    # 5) Canon audit — verify the script against current canon BEFORE merging.
    #    If the script breaks canon → return critical violations as warnings and
    #    DO NOT update the canon (user needs to fix things first).
    canon_status = {
        'audited': False,
        'passes': True,
        'violations': [],
        'updated': False,
        'update_summary': None,
        'audit_error': None,
    }
    try:
        brief = build_logic_brief(sid, num)
    except Exception as e:
        brief = f'(brief unavailable: {e})'
    try:
        audit_result = audit_script(sid, num, script, brief)
        canon_status['audited'] = True
        violations = audit_result.get('violations', []) or []
        canon_status['violations'] = violations
        canon_status['audit_error'] = audit_result.get('audit_error')
        critical = [v for v in violations if v.get('severity') == 'critical']
        canon_status['passes'] = not critical
        if not critical:
            # Canon-clean → roll back any prior canon entries for this ep, then
            # extract fresh updates from the current script.
            try:
                rollback_canon_for_episode(sid, num)
            except Exception:
                pass
            try:
                upd = extract_canon_updates(sid, num, script)
                if upd and not upd.get('error'):
                    canon_status['updated'] = True
                    canon_status['update_summary'] = {
                        'world_day': upd.get('world_day'),
                        'new_facts': upd.get('new_facts', 0),
                    }
                else:
                    canon_status['audit_error'] = canon_status.get('audit_error') or upd.get('error')
            except Exception as e:
                canon_status['audit_error'] = canon_status.get('audit_error') or str(e)
    except Exception as e:
        canon_status['audit_error'] = str(e)

    return jsonify({
        'added_characters':  added_chars,
        'added_locations':   added_locs,
        'added_items':       added_items,
        'dropped_characters': dropped_chars,
        'dropped_locations':  dropped_locs,
        'characters_used':   final_chars,
        'locations_used':    final_locs,
        'series': s,
        'synopsis':       synopsis_status,
        'canon':          canon_status,
        'appearance_updates': appearance_updates,
        'director_notes_used': bool(director_notes),
    })


@app.route('/api/series/<sid>/confirm-milestones', methods=['POST'])
def confirm_milestones(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ms = s.get('milestone_synopses', {})
    missing = [n for n in MILESTONE_EPS if str(n) not in ms or not ms[str(n)]]
    if missing:
        return jsonify({'error': f'Сначала сгенерируй контрольные точки: {missing}'}), 400
    s['stage'] = 3  # was 2 (milestones) or 1 (legacy arc stage) → advance to episodes
    save_series(sid, s)
    return jsonify(s)


@app.route('/api/series/<sid>/generate-next-episode-synopsis', methods=['POST'])
def generate_next_episode_synopsis(sid):
    """Generate synopsis for the next (not yet created) episode, using prev episode context and nearest milestone."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    episodes = sorted(list_episodes(sid), key=lambda e: e['number'])
    data = request.json or {}
    # Use requested episode number if provided, otherwise auto-detect
    requested_num = data.get('episode_number')
    if requested_num:
        next_num = int(requested_num)
    else:
        next_num = (episodes[-1]['number'] + 1) if episodes else 1

    batch = is_batch_mode(s)
    bs = batch_size(s) if batch else 1

    # Find nearest upcoming milestone (milestones still indexed by sub-episode number)
    ms = s.get('milestone_synopses', {})
    if batch:
        a, b = chunk_range(s, next_num)
        upcoming_milestone_num = next((m for m in MILESTONE_EPS if a <= m <= b or m >= a), None)
    else:
        upcoming_milestone_num = next((m for m in MILESTONE_EPS if m >= next_num), None)
    upcoming_milestone_syn = ms.get(str(upcoming_milestone_num), '') if upcoming_milestone_num else ''
    milestone_block = ''
    if upcoming_milestone_syn and upcoming_milestone_num != next_num:
        milestone_block = (
            f'\nNEAREST UPCOMING MILESTONE — Sub-episode {upcoming_milestone_num}:\n'
            f'{upcoming_milestone_syn}\n'
            f'IMPORTANT: this synopsis must logically lead toward this milestone. '
            f'Do NOT contradict or skip over its events.\n'
        )

    # ─── CRITICAL: user-pinned trajectory (checkpoints + finale) ───
    # The static MILESTONE_EPS above are setup-time anchors. The REAL steering
    # signal is user-defined: s['checkpoints'] (story landmarks at specific eps)
    # and s['finale'] (the planned ending). Without injecting these, the
    # synopsis generator drifts and ignores the finale entirely.
    trajectory_block = build_trajectory_block(s, next_num)
    # GREEN LIGHT playbook — proven craft from top dramas + green-marked series.
    # Prepended once here so it rides into every prompt branch via {trajectory_block}.
    _pb_block = get_playbook_block()
    if _pb_block:
        trajectory_block = _pb_block + trajectory_block
    # Keep the ordered beat skeleton (ноды) steering every synopsis — prepend
    # so it sits above the per-episode trajectory. Included in all prompt
    # branches below via {trajectory_block}.
    _beats_block = _series_beats_episode_block(s)
    if _beats_block:
        trajectory_block = _beats_block + trajectory_block
    # Concrete per-episode bridge plan from current state to finale.
    try:
        _bridge_data = build_finale_bridge_plan(s, next_num) or {}
    except Exception:
        _bridge_data = {}
    _bridge_block_txt = _bridge_data.get('block', '')
    _bridge_this_beat = (_bridge_data.get('this_beat') or '').strip()
    if _bridge_block_txt:
        trajectory_block = trajectory_block + _bridge_block_txt
    if _bridge_this_beat:
        trajectory_block = (
            trajectory_block
            + f'\n🎯 ЭТА СЕРИЯ (Ep {next_num}) — конкретный beat из плана-моста: {_bridge_this_beat}\n'
            f'Синопсис ниже ОБЯЗАН отражать этот beat. Если ранее запланированные подсюжеты '
            f'не вписываются в beat — сверни или адаптируй их.\n\n'
        )
    # Build a sharp "must lead toward X" steering instruction tied to the
    # NEAREST landmark (next checkpoint OR finale, whichever is closer).
    steering_instruction = ''
    cps_all = s.get('checkpoints') or []
    fin = s.get('finale') or None
    upcoming_cps = sorted(
        [c for c in cps_all if int(c.get('episode', 0) or 0) >= next_num and (c.get('description') or '').strip()],
        key=lambda c: int(c.get('episode', 0) or 0),
    )
    nearest_landmark = None
    if upcoming_cps:
        # nearest checkpoint wins if it's before or equal to the finale episode
        nc = upcoming_cps[0]
        nc_ep = int(nc['episode'])
        fin_ep = int(fin['episode']) if fin and fin.get('description') else None
        if fin_ep is None or nc_ep <= fin_ep:
            nearest_landmark = ('checkpoint', nc_ep, nc.get('description', '').strip())
        else:
            nearest_landmark = ('finale', fin_ep, fin.get('description', '').strip())
    elif fin and fin.get('description', '').strip() and int(fin.get('episode', 0) or 0) >= next_num:
        nearest_landmark = ('finale', int(fin['episode']), fin['description'].strip())

    if nearest_landmark:
        kind, land_ep, land_desc = nearest_landmark
        dist = land_ep - next_num
        when = 'in THIS episode' if dist == 0 else f'in {dist} episode(s)'
        steering_instruction = (
            f'\n═══ TRAJECTORY STEERING — MANDATORY ═══\n'
            f'NEAREST USER-PINNED LANDMARK: {kind.upper()} at Ep {land_ep} ({when}).\n'
            f'Landmark description:\n{land_desc}\n\n'
            f'HARD RULE — the Ep {next_num} synopsis MUST advance the plot toward this landmark. '
            f'Plant a SEED for it (a clue / a confrontation setup / a character moving into position / '
            f'an unresolved tension that the landmark will resolve). '
        )
        if dist == 0:
            steering_instruction += (
                f'\nTHIS IS THE LANDMARK EPISODE — the events in the landmark description MUST happen in this synopsis. '
                f'Do NOT delay them, do NOT substitute them with similar-but-different events. '
                f'Use the exact characters and the exact actions from the description.\n'
            )
        elif dist <= 3:
            steering_instruction += (
                f'\nLandmark is CLOSE ({dist} ep(s) away) — this synopsis is the FINAL setup. '
                f'Every character the landmark needs must already be in position by end of this episode. '
                f'Do NOT introduce a new subplot that delays the landmark.\n'
            )
        else:
            steering_instruction += (
                f'\nLandmark is {dist} ep(s) away — plant subtle setup without firing the landmark prematurely. '
                f'Move pieces into place, do not pre-resolve any condition the landmark relies on.\n'
            )
        # If a finale exists at a LATER episode than the nearest checkpoint, also surface it.
        if kind == 'checkpoint' and fin and fin.get('description', '').strip() and int(fin.get('episode', 0) or 0) >= next_num:
            fin_ep = int(fin['episode'])
            steering_instruction += (
                f'\nSERIES FINALE (Ep {fin_ep}, {fin_ep - next_num} ep(s) away): {fin["description"].strip()}\n'
                f'Whatever character / power-state / secret the finale relies on MUST remain achievable '
                f'from this episode onward — do NOT kill, expose, or permanently remove anyone the finale needs.\n'
            )
        steering_instruction += '═══════════════════════════════════════════════\n'

    unit_word = 'CHUNK' if batch else 'EPISODE'
    chunk_range_str = (lambda n: f'{chunk_range(s,n)[0]}–{chunk_range(s,n)[1]}')(next_num) if batch else str(next_num)

    if next_num == 1 or not episodes:
        if batch:
            a, b = chunk_range(s, next_num)
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) +
                trajectory_block +
                steering_instruction +
                f'{milestone_block}\n'
                f'BATCH MODE: write the synopsis for CHUNK 1 — sub-episodes {a}–{b} (~{bs*60} sec total).\n'
                f'4–6 sentences outlining: (1) the chunk hook, (2) all {bs} mini-cliffhangers in order, (3) the chunk midpoint reversal, (4) the chunk-end cliffhanger.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": 0}'
            )
        else:
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) +
                trajectory_block +
                steering_instruction +
                f'{milestone_block}\n'
                f'Write a synopsis (2-3 sentences) for EPISODE 1 — the series premiere.\n'
                f'This is the FIRST episode: establish the world and main character while immediately grabbing the viewer. '
                f'Start in immediate stakes — something is already at risk or being revealed. '
                f'End on a strong cliffhanger.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": 0}'
            )
    else:
        prev_eps = [e for e in episodes if e['number'] < next_num]
        prev_ep = prev_eps[-1] if prev_eps else episodes[-1]
        prev_synopsis = prev_ep.get('synopsis', '')
        prev_script = prev_ep.get('script', '')
        script_block = ''
        if prev_script:
            script_block = f'\nPrevious {unit_word.lower()} script (FULL):\n{prev_script}\n'
        prev_label = (f'CHUNK {prev_ep["number"]} (sub-eps {chunk_range(s,prev_ep["number"])[0]}–{chunk_range(s,prev_ep["number"])[1]})'
                      if batch else f'Ep {prev_ep["number"]}')
        if batch:
            a, b = chunk_range(s, next_num)
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) + '\n'
                + trajectory_block
                + steering_instruction +
                f'Previous {prev_label} synopsis:\n{prev_synopsis}\n'
                f'{script_block}'
                f'{milestone_block}\n'
                f'BATCH MODE: write the synopsis for CHUNK {next_num} — sub-episodes {a}–{b} (~{bs*60} sec total).\n'
                f'Pick up FROM THE NEXT BEAT after the previous chunk\'s cliffhanger, NOT from the cliffhanger itself. NO PLOT-RECAP: events already shown in CHUNK {prev_ep["number"]} are DONE — describe what happens NEXT, do NOT have characters re-issue the same ultimatums / re-state the same threats / re-deliver the same revelations from the previous chunk. '
                f'4–6 sentences outlining: (1) chunk hook, (2) all {bs} mini-cliffhangers in order, (3) chunk midpoint reversal, (4) chunk-end cliffhanger.\n'
                f'TIMELINE — also output `days_since_previous` (integer in-world days from prev chunk\'s end). 0 = same-day continuation.\n'
                f'TRAJECTORY: the chunk MUST move pieces toward the nearest pinned landmark above. Re-read the TRAJECTORY STEERING block before finalizing.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": int}'
            )
        else:
            prompt = (
                f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
                f'Series synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
                + _canonical_cast_block(s) + '\n'
                + trajectory_block
                + steering_instruction +
                f'Previous episode ({prev_label}) synopsis:\n{prev_synopsis}\n'
                f'{script_block}'
                f'{milestone_block}\n'
                f'Write a synopsis (2-3 sentences) for EPISODE {next_num}.\n'
                f'Continue naturally from where episode {prev_ep["number"]} left off — pick up FROM THE NEXT BEAT after the cliffhanger, NOT from the cliffhanger itself. '
                f'NO PLOT-RECAP: events that already happened in Ep {prev_ep["number"]} (ultimatums delivered, threats made, revelations, decisions announced) are DONE. The Ep {next_num} synopsis must describe what happens NEXT (the response, the counter-move, the consequence) — NOT the same ultimatum being re-issued or the same threat being re-stated. If Ep {prev_ep["number"]} ended with "Character X demands Y or else Z" — Ep {next_num} synopsis describes the OTHER side\'s reaction / a twist / a new arrival, not Character X repeating the demand. '
                f'Include: an immediate-stakes opening (no warm-up), a mid-episode reversal, and end on a new cliffhanger. '
                f'TIMELINE — also output `days_since_previous` (integer): in-world days between Ep {prev_ep["number"]} and Ep {next_num}. '
                f'Use real-world biology (pregnancy test 10+ days post conception, undercover ops 30+ days setup). 0 = same-day continuation. '
                f'TRAJECTORY: this episode MUST move pieces toward the nearest pinned landmark above. Re-read the TRAJECTORY STEERING block before finalizing the synopsis.\n'
                'Return JSON: {"synopsis": "...", "days_since_previous": int}'
            )

    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
        syn = data.get('synopsis', '')
        dsp = data.get('days_since_previous')
        # If the episode ALREADY exists (user pre-created it), just patch its
        # synopsis / days metadata in-place. Do NOT auto-create a new episode here:
        # this endpoint is called from the "Создать эпизод" modal's "✨ Сгенерить
        # синопсис" button, where actual episode creation happens later via
        # POST /episodes when the user confirms. Side-effect creation here led
        # to phantom episodes when users generated a synopsis and then closed
        # the modal — the episode was silently committed to disk.
        ep = load_episode(sid, next_num)
        if ep is not None:
            if not ep.get('synopsis'): ep['synopsis'] = syn
            if dsp is not None: ep['days_since_previous'] = int(dsp)
            save_episode(sid, next_num, ep)
        return jsonify({'synopsis': syn, 'days_since_previous': dsp, 'episode_number': next_num})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/generate-episode-synopses', methods=['POST'])
def generate_episode_synopses(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    episodes = {ep['number']: ep for ep in list_episodes(sid)}
    ms = s.get('milestone_synopses', {})
    batch = is_batch_mode(s)
    bs = batch_size(s) if batch else 1
    cast_pin = get_playbook_block() + _canonical_cast_block(s) + _revision_instructions_block(s)
    if batch:
        # In batch mode, fill all chunks BETWEEN the anchor chunks (e.g. chunks 1..chunk_of(10)).
        # Each chunk synopsis must outline `bs` sub-cliffhangers + the chunk's main reversal.
        a1, a2, _ = anchor_chunks(s)
        target = list(range(a1, a2 + 1))  # chunks 1..a2
        ms_anchor1 = ms.get(str(a1), '')
        ms_anchor2 = ms.get(str(a2), '')
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
            + cast_pin +
            f'Anchor chunks: Chunk {a1}: {ms_anchor1} | Chunk {a2}: {ms_anchor2}\n\n'
            f"This series is in BATCH MODE — each storage unit is a CHUNK of {bs} consecutive sub-episodes (~{bs*60}s total). "
            f"Write synopses for chunks {a1}..{a2} (sub-episodes 1–{a2*bs}). "
            f"Each chunk synopsis (4–6 sentences) must outline: (1) opening hook, (2) the {bs} mini-cliffhangers in order, (3) the chunk's midpoint reversal, (4) the chunk-end cliffhanger leading into the next chunk. "
            f"Chunk {a1} aligns with anchor 1 and chunk {a2} aligns with anchor 2. Chunks in between escalate logically. "
            "Each chunk must continue from where the previous chunk ended — the next chunk's writer will read this synopsis as their context. "
            "TIMELINE — for each chunk also output `days_since_previous` (integer in-world days from the previous chunk's end). Chunk 1 always has 0. "
            "Use real-world biology/protocol: pregnancy test 10+ days post conception, undercover ops 30+ days setup. "
            'Return JSON: {"episodes": {' + ', '.join(f'"{n}": {{"synopsis":"...", "days_since_previous": 0}}' for n in target) + '}}'
        )
    else:
        # Pin checkpoint / finale hints so synopsis-writer plots toward them.
        cps = sorted((s.get('checkpoints') or []), key=lambda c: int(c.get('episode', 0)))
        cps_block = ''
        if cps:
            cps_block = 'CHECKPOINTS (steer toward each one — at the indicated episode this MUST happen):\n' + \
                '\n'.join(f"  - Ep {c['episode']}: {(c.get('description') or '').strip()}" for c in cps if c.get('description')) + '\n'
        fin = s.get('finale') or {}
        fin_block = ''
        if fin and fin.get('description'):
            fin_block = f"FINALE (ep {fin.get('episode','?')}): {fin['description']}\n" + \
                "Do not violate finale constraints early (don't kill characters needed alive, etc).\n"
        prompt = (
            f'Series: "{s["title"]}"\nGenre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
            f'Synopsis: "{s.get("synopsis","")}" | Arc: "{s.get("arc","")}".\n'
            + cast_pin +
            f'Milestone synopses: Ep 1: {ms.get("1","")} | Ep 10: {ms.get("10","")}\n'
            + cps_block + fin_block + '\n'
            "Write synopses (2-3 sentences each) for episodes 1 through 10. "
            "Ep 1 and 10 must match their milestones exactly. "
            "SHORT DRAMA FORMAT — every single episode must: (1) open with a conflict already in progress (not building toward one), (2) include one mid-episode reversal or unexpected complication that flips what we thought was true, (3) end with a cliffhanger that makes the next episode unmissable. "
            "NO episode is a filler bridge — every one must have its own shock, reversal, and cliffhanger punch. "
            "TIMELINE — for each episode also output `days_since_previous` (integer): how many in-world days passed since the previous episode. "
            "Use real-world biology/protocol: pregnancy test needs 10+ days post conception, undercover ops 30+ days setup, intercontinental flight 8+ hours. "
            "Ep 1 always has days_since_previous=0. Use 0 for same-day continuation. "
            'Return JSON: {"episodes": {"1": {"synopsis":"...", "days_since_previous": 0}, "2": {"synopsis":"...", "days_since_previous": 1}, ...}}'
        )
    try:
        data = json.loads(strip_json(llm_ask(_resolve_writer_model(request.get_json(silent=True) or {}, s), prompt, system=_WRITER_SYSTEM)))
        synopses = data.get('episodes', {})
        result = {}
        for num_str, payload in synopses.items():
            num = int(num_str)
            # Accept legacy string format too
            if isinstance(payload, str):
                syn, dsp = payload, None
            else:
                syn = payload.get('synopsis', '')
                dsp = payload.get('days_since_previous')
            result[num_str] = syn
            if num in episodes:
                ep = episodes[num]
                if not ep.get('synopsis'):
                    ep['synopsis'] = syn
                    if dsp is not None: ep['days_since_previous'] = int(dsp)
                    save_episode(sid, num, ep)
            else:
                ep = {
                    'number': num, 'title': f'Эпизод {num}', 'synopsis': syn,
                    'script': '', 'characters_used': [], 'character_outfits': {},
                    'days_since_previous': int(dsp) if dsp is not None else (0 if num == 1 else 1),
                    'notes': '', 'status': 'draft',
                    'reteller': {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None}
                }
                save_episode(sid, num, ep)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
