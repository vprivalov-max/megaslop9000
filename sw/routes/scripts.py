"""Script generation routes: generate-script, generate-to-landmark,
cast-block sync, gender inference, character/outfit extraction."""
import json
import re
import threading
import time
import uuid

from flask import jsonify, request

from sw.anthro import _anthro_world_block, _revision_instructions_block
from sw.canon_index import (_build_plot_device_history,
                            _extract_devices_from_script,
                            _extract_narrative_state_from_script,
                            _update_devices_index, _update_narrative_index)
from sw.core import app
from sw.llm import claude_ask
from sw.logging_utils import _log_event
# routes->routes: landmark builders shared by writer prompts (no cycle).
from sw.routes.landmarks import (build_finale_bridge_plan,
                                 build_finale_contract_block,
                                 build_trajectory_block, is_finale_episode)
from sw.storage import (_extract_end_position, _normalize_blocking_tags,
                        _resolve_char_by_script_name, _sync_script_outfits,
                        batch_size, chunk_range, is_batch_mode, list_episodes,
                        load_canon, load_episode, load_series, save_canon,
                        save_episode, save_series)
from sw.story_logic import (_build_crowd_constraint_block,
                            _build_narrative_state_block,
                            _extract_speaker_and_blocking_labels,
                            _label_is_unnamed, audit_logic_holes, audit_script,
                            build_logic_brief, detect_scene_overcrowding,
                            detect_script_overlength,
                            detect_unnamed_characters, extract_canon_updates,
                            rollback_canon_for_episode)
from sw.story_prompts import (_build_cast_block, _format_mode_block,
                              _format_mode_of, _outfit_ids)
from sw.story_writer import _build_batch_script_system, _build_script_system


def _series_beats_episode_block(s):
    # lazy proxy: beats helper lives in sw.routes.ideas (no cycle at call time)
    from sw.routes.ideas import _series_beats_episode_block as f
    return f(s)


def trigger_autogen_if_enabled(sid):
    # lazy proxy: avoids module-level routes->autogen import
    from sw.autogen import trigger_autogen_if_enabled as f
    return f(sid)

# Generate-to-landmark — convergence-mode generation
# ─────────────────────────────────────────────────────────────────────────────
# When the user clicks "До финала" / "До чекпоинта", we run a sequential pass:
# for each episode in [current_unwritten .. target_episode]:
#   1. Generate / refresh the bridge plan (cached after first call).
#   2. OVERWRITE the episode's synopsis with the bridge beat — this kills the
#      "stale-synopsis vs new-bridge-plan" disconnect that caused the model to
#      keep writing in the old subplot direction.
#   3. Generate the script via the existing pipeline — now the synopsis itself
#      describes the bridge beat, so prev_script pressure can't pull the writer
#      back into the divergent thread.
#
# This is the "max-weight landmark steering" mode the user requested.

# In-memory progress tracker for generate-to-landmark.
# Keyed by sid → {state, start, target, current_ep, completed, total, results, error, started_at, finished_at}
_LANDMARK_PROGRESS = {}
_LANDMARK_LOCK = threading.Lock()


def _landmark_progress_set(sid, **fields):
    with _LANDMARK_LOCK:
        cur = _LANDMARK_PROGRESS.get(sid, {})
        cur.update(fields)
        _LANDMARK_PROGRESS[sid] = cur


def _landmark_progress_get(sid):
    with _LANDMARK_LOCK:
        return dict(_LANDMARK_PROGRESS.get(sid) or {})


def _run_generate_to_landmark_bg(sid, landmark_type, target_ep, start_ep, model):
    """Background worker — does the per-episode generation loop. Mirrors the logic
    that used to run inline in the request handler, but writes progress to
    _LANDMARK_PROGRESS so the UI can poll status.
    """
    try:
        _landmark_progress_set(
            sid,
            state='running',
            landmark_type=landmark_type,
            target_episode=target_ep,
            start_episode=start_ep,
            current_ep=start_ep,
            completed=0,
            total=target_ep - start_ep + 1,
            results=[],
            error=None,
        )
        for n in range(start_ep, target_ep + 1):
            _landmark_progress_set(sid, current_ep=n)
            s_fresh = load_series(sid)
            if not s_fresh:
                _landmark_progress_set(sid, error='series not found mid-run', state='failed',
                                       finished_at=time.time())
                return
            ep = load_episode(sid, n)
            if ep is None:
                ep = {'number': n, 'title': f'Episode {n}', 'synopsis': '', 'script': '', 'status': 'draft'}
                save_episode(sid, n, ep)
            # Bridge plan + synopsis overwrite
            try:
                bridge = build_finale_bridge_plan(s_fresh, n) or {}
            except Exception as be:
                print(f'[generate-to-landmark/bg] ep{n}: bridge_plan FAILED: {be}', flush=True)
                bridge = {}
            this_beat = (bridge.get('this_beat') or '').strip()
            state_before = (bridge.get('this_state_before') or '').strip()
            if this_beat:
                parts = []
                if state_before:
                    parts.append(f'[Состояние мира на старте серии: {state_before}]')
                parts.append(this_beat)
                new_synopsis = ' '.join(parts)
                old_synopsis = (ep.get('synopsis') or '').strip()
                if old_synopsis != new_synopsis:
                    ep.setdefault('synopsis_history', []).append({
                        'ts': time.time(),
                        'reason': f'overwritten by generate-to-landmark ({landmark_type})',
                        'synopsis': old_synopsis[:2000],
                    })
                    ep['synopsis_history'] = ep['synopsis_history'][-10:]
                    ep['synopsis'] = new_synopsis
                    save_episode(sid, n, ep)
                    print(f'[generate-to-landmark/bg] ep{n}: synopsis overwritten from bridge beat', flush=True)
            # Generate script via existing endpoint logic
            try:
                with app.test_request_context(
                    f'/api/series/{sid}/episodes/{n}/generate-script',
                    method='POST',
                    json={'model': model},
                ):
                    resp = generate_episode_script(sid, n)
                payload = resp.json if hasattr(resp, 'json') else {}
                status_code = getattr(resp, 'status_code', 200)
                if status_code >= 400:
                    err_msg = (payload or {}).get('error') or f'HTTP {status_code}'
                    with _LANDMARK_LOCK:
                        cur = _LANDMARK_PROGRESS.get(sid) or {}
                        cur.setdefault('results', []).append({'episode': n, 'ok': False, 'error': err_msg})
                        cur['error'] = err_msg
                        cur['state'] = 'failed'
                        cur['finished_at'] = time.time()
                        _LANDMARK_PROGRESS[sid] = cur
                    print(f'[generate-to-landmark/bg] ep{n}: FAILED — {err_msg}', flush=True)
                    return
                with _LANDMARK_LOCK:
                    cur = _LANDMARK_PROGRESS.get(sid) or {}
                    cur.setdefault('results', []).append({
                        'episode': n,
                        'ok': True,
                        'retries': ((payload or {}).get('audit_report') or {}).get('retries'),
                    })
                    cur['completed'] = cur.get('completed', 0) + 1
                    _LANDMARK_PROGRESS[sid] = cur
                print(f'[generate-to-landmark/bg] ep{n}: OK', flush=True)
            except Exception as e:
                with _LANDMARK_LOCK:
                    cur = _LANDMARK_PROGRESS.get(sid) or {}
                    cur.setdefault('results', []).append({'episode': n, 'ok': False, 'error': str(e)[:300]})
                    cur['error'] = str(e)[:300]
                    cur['state'] = 'failed'
                    cur['finished_at'] = time.time()
                    _LANDMARK_PROGRESS[sid] = cur
                print(f'[generate-to-landmark/bg] ep{n}: EXCEPTION — {e}', flush=True)
                return
        _landmark_progress_set(sid, state='done', current_ep=None, finished_at=time.time())
        print(f'[generate-to-landmark/bg] sid={sid} DONE', flush=True)
    except Exception as outer:
        _landmark_progress_set(sid, state='failed', error=str(outer)[:300], finished_at=time.time())
        print(f'[generate-to-landmark/bg] sid={sid} OUTER EXCEPTION — {outer}', flush=True)


@app.route('/api/series/<sid>/generate-to-landmark/status', methods=['GET'])
def generate_to_landmark_status(sid):
    return jsonify(_landmark_progress_get(sid) or {'state': 'idle'})


@app.route('/api/series/<sid>/generate-to-landmark', methods=['POST'])
def generate_to_landmark(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    landmark_type = (body.get('landmark_type') or '').strip().lower()
    if landmark_type not in ('finale', 'checkpoint'):
        return jsonify({'error': 'landmark_type must be "finale" or "checkpoint"'}), 400

    # Resolve target episode
    if landmark_type == 'finale':
        fin = s.get('finale') or None
        if not fin or not (fin.get('description') or '').strip():
            return jsonify({'error': 'no finale pinned for this series'}), 400
        try:
            target_ep = int(fin.get('episode', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'finale has invalid episode number'}), 400
    else:
        # checkpoint — caller may specify which one; otherwise the NEAREST upcoming.
        cps = sorted(
            [c for c in (s.get('checkpoints') or []) if (c.get('description') or '').strip()],
            key=lambda c: int(c.get('episode', 0) or 0),
        )
        if not cps:
            return jsonify({'error': 'no checkpoints pinned for this series'}), 400
        requested_cp = body.get('landmark_episode')
        if requested_cp:
            try:
                target_ep = int(requested_cp)
                if not any(int(c.get('episode', 0) or 0) == target_ep for c in cps):
                    return jsonify({'error': f'no checkpoint at episode {target_ep}'}), 400
            except (TypeError, ValueError):
                return jsonify({'error': 'landmark_episode must be an integer'}), 400
        else:
            # Pick nearest upcoming checkpoint relative to first unwritten episode
            episodes = sorted(list_episodes(sid), key=lambda e: int(e.get('number', 0) or 0))
            first_unwritten = next((e['number'] for e in episodes if not (e.get('script') or '').strip()),
                                   (episodes[-1]['number'] + 1) if episodes else 1)
            upcoming = [c for c in cps if int(c.get('episode', 0) or 0) >= first_unwritten]
            if not upcoming:
                return jsonify({'error': 'all checkpoints are already past the current episode'}), 400
            target_ep = int(upcoming[0]['episode'])

    # Resolve start episode:
    #   1. If caller passed explicit start_episode → use it (supports overwrite mode)
    #   2. Else: first episode without a script
    #   3. Else: error — there's nothing to do
    episodes = sorted(list_episodes(sid), key=lambda e: int(e.get('number', 0) or 0))
    explicit_start = body.get('start_episode')
    if explicit_start is not None and str(explicit_start).strip() != '':
        try:
            start_ep = int(explicit_start)
            if start_ep < 1:
                return jsonify({'error': 'start_episode must be >= 1'}), 400
        except (TypeError, ValueError):
            return jsonify({'error': 'start_episode must be an integer'}), 400
    else:
        start_ep = None
        for e in episodes:
            if not (e.get('script') or '').strip():
                start_ep = int(e.get('number', 0))
                break
        if start_ep is None:
            return jsonify({
                'error': 'no unwritten episodes — pass start_episode to overwrite from a specific point'
            }), 400

    if target_ep < start_ep:
        return jsonify({'error': f'target ep {target_ep} is before start ep {start_ep}'}), 400
    span = target_ep - start_ep + 1
    MAX_SPAN = 8
    if span > MAX_SPAN:
        return jsonify({
            'error': f'span too large ({span} episodes) — max {MAX_SPAN} per call. '
                     f'Run multiple times, or move the landmark closer.'
        }), 400

    print(f'[generate-to-landmark] sid={sid} type={landmark_type} target=Ep{target_ep} start=Ep{start_ep} span={span}', flush=True)

    # Refuse if a previous run for this sid is still active
    prev = _landmark_progress_get(sid)
    if prev.get('state') == 'running':
        return jsonify({
            'error': 'a landmark generation is already running for this series',
            'progress': prev,
        }), 409

    # Initialize fresh progress
    _landmark_progress_set(
        sid,
        state='starting',
        landmark_type=landmark_type,
        target_episode=target_ep,
        start_episode=start_ep,
        current_ep=start_ep,
        completed=0,
        total=span,
        results=[],
        error=None,
        started_at=time.time(),
        finished_at=None,
    )

    model = body.get('model') or s.get('writer_model')
    t = threading.Thread(
        target=_run_generate_to_landmark_bg,
        args=(sid, landmark_type, target_ep, start_ep, model),
        name=f'gen-to-landmark-{sid}',
        daemon=True,
    )
    t.start()

    return jsonify({
        'started': True,
        'landmark_type': landmark_type,
        'target_episode': target_ep,
        'start_episode': start_ep,
        'span': span,
        'poll_url': f'/api/series/{sid}/generate-to-landmark/status',
    }), 202


@app.route('/api/series/<sid>/episodes/<int:num>/generate-script', methods=['POST'])
def generate_episode_script(sid, num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'Episode not found'}), 404

    if num > 1:
        prev_ep = load_episode(sid, num - 1)
        if not prev_ep or not prev_ep.get('script', '').strip():
            return jsonify({'error': f'Generate script for episode {num - 1} first'}), 400
        prev_script = prev_ep['script']
    else:
        prev_script = ''

    next_ep = load_episode(sid, num + 1)
    next_synopsis = next_ep.get('synopsis', '') if next_ep else ''
    chars = s.get('characters', [])
    locs  = s.get('locations', [])
    cast_block = _build_cast_block(s, ep)

    batch = is_batch_mode(s)
    bs = batch_size(s)
    _is_finale = is_finale_episode(s, num)
    if batch:
        a, b = chunk_range(s, num)
        unit_label = f'CHUNK {num} (sub-episodes {a}–{b})'
        prev_a, prev_b = chunk_range(s, num - 1) if num > 1 else (0, 0)
        prev_label = f'PREVIOUS CHUNK {num-1} (sub-episodes {prev_a}–{prev_b})'
        next_label = f'NEXT CHUNK {num+1}'
    else:
        unit_label = f'EPISODE {num}'
        prev_label = f'PREVIOUS EPISODE {num-1}'
        next_label = f'NEXT EPISODE {num+1}'

    if prev_script:
        # Surface the FULL previous unit (no truncation) and additionally pull out its last beats
        # into a dedicated ENDING STATE block so the writer cannot accidentally ignore where the
        # previous scene was left.
        prev_lines = [ln for ln in prev_script.splitlines() if ln.strip() and not ln.strip().startswith('━')]
        # Drop the EPISODE NOTES / CHUNK NOTES tail if present
        for stop_kw in ('EPISODE NOTES', 'CHUNK NOTES'):
            if stop_kw in prev_lines:
                prev_lines = prev_lines[:prev_lines.index(stop_kw)]
        tail = '\n'.join(prev_lines[-25:])
        # Last ~10 dialogue lines specifically — these are most likely to be re-staged
        # by a writer who treats "continuation" as "rewind the climax".
        recent_dialogue = []
        for ln in reversed(prev_lines):
            if re.match(r'^\s*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-\']{0,30}:\s', ln.strip()):
                recent_dialogue.insert(0, ln.strip())
                if len(recent_dialogue) >= 10:
                    break
        forbidden_dialogue_block = ''
        if recent_dialogue:
            forbidden_dialogue_block = (
                f'\n═══ FORBIDDEN — DO NOT REPEAT OR PARAPHRASE THESE LINES ═══\n'
                f'These dialogue beats already happened in {prev_label}. The viewer SAW them. '
                f'You may NOT have any character say them again, paraphrase them, or re-deliver '
                f'the same threat / ultimatum / revelation in different words.\n'
                + '\n'.join(recent_dialogue)
                + f'\n═══════════════════════════════════════════════════\n'
            )
        prev_block = (
            f'{prev_label} SCRIPT (FULL — read it all):\n{prev_script}\n\n'
            f'═══ ENDING STATE OF {prev_label} — read this carefully ═══\n'
            f'These are the LAST beats of the previous unit. If they show a scene still in motion '
            f'(someone just arrived, a question hangs in the air, two characters mid-confrontation, '
            f'a reaction shot is the cliffhanger) — THIS unit MUST open in the SAME scene, SAME '
            f'location, SAME characters present, continuing dialogue from the very next beat. '
            f'Do NOT teleport to a new room or "later that day".\n'
            f'{tail}\n'
            f'═══════════════════════════════════════════════════\n'
            f'{forbidden_dialogue_block}\n'
            f'═══ NO PLOT-RECAP — HARD RULE ═══\n'
            f'"Continue from the next beat" means PUSH FORWARD, not REWIND. Events that '
            f'happened in {prev_label} (ultimatums delivered, threats made, revelations, '
            f'character entrances/exits, decisions stated out loud) are DONE. Do NOT have a '
            f'character re-issue the same ultimatum, re-state the same threat, or re-deliver '
            f'the same key line in slightly different words just to "remind" the audience. '
            f'A short drama viewer just watched {prev_label} 3 seconds ago — they remember. '
            f'If the cliffhanger of {prev_label} was "Claire demands he confess on camera or '
            f'she releases the files", THIS unit must show what happens NEXT — Cross\'s '
            f'reaction, Melissa\'s move, an arrival, a counter-twist — NOT Claire saying the '
            f'same demand again. RECAP-AS-OPENING IS A FAILURE STATE.\n'
            f'═══════════════════════════════════════════════════\n\n'
        )
    else:
        prev_block = f'This is the FIRST {("CHUNK" if batch else "EPISODE")} — no previous.\n\n'
    next_block = f'{next_label} SYNOPSIS (for continuity):\n{next_synopsis}\n\n' if next_synopsis else ''

    # ─── PRE-WRITE: rollback canon for this ep (in case of regeneration) and build logic brief
    rollback_canon_for_episode(sid, num)
    try:
        brief = build_logic_brief(sid, num)
    except Exception as e:
        brief = f'(brief unavailable: {e})'
    brief_block = f'═══ LOGIC CONSTRAINTS — MUST RESPECT ALL OF THESE ═══\n{brief}\n═══════════════════════════════════════════════════\n\n'
    try:
        devices_block = _build_plot_device_history(sid, num)
    except Exception:
        devices_block = ''
    try:
        narrative_block = _build_narrative_state_block(sid, num)
    except Exception:
        narrative_block = ''
    try:
        crowd_block = _build_crowd_constraint_block(s)
    except Exception:
        crowd_block = ''
    trajectory_block = build_trajectory_block(s, num)
    # Keep the ordered beat skeleton (ноды) steering the script — prepend so it
    # sits above the per-episode trajectory in the prompt. The opening episodes
    # march through the beats in order, then improvise (see
    # _series_beats_episode_block).
    _beats_block = _series_beats_episode_block(s)
    if _beats_block:
        trajectory_block = _beats_block + trajectory_block
    # Concrete plot bridge from THIS episode to the finale — generated by a quick Haiku
    # planning call. This is the load-bearing fix for "writer ignores the finale":
    # prev_script tends to dominate, so we explicitly tell the writer what THIS episode
    # must do to converge to the finale.
    try:
        bridge_data = build_finale_bridge_plan(s, num) or {}
    except Exception as _be:
        _log_event('WARN', 'bridge_plan_skip', err=str(_be)[:200])
        bridge_data = {}
    bridge_block = bridge_data.get('block', '')
    bridge_this_beat = (bridge_data.get('this_beat') or '').strip()
    bridge_state_before = (bridge_data.get('this_state_before') or '').strip()
    bridge_finale_ep = bridge_data.get('finale_ep')
    # Format-mode block (short_drama vs instagram_series) — drives ending style,
    # cliffhanger requirement, standalone-ness, pacing.
    series_format_mode = _format_mode_of(s)
    format_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])

    # Hard mandatory block for IG-series — every episode of every series MUST
    # open on a hook and close on a cliffhanger, no exceptions, regardless of
    # how soft the synopsis reads. Plus explicit ban on legal-procedural plot
    # engines (lawsuit / court / paperwork) that kill the pace of a 60s show.
    ig_hard_block = (
        '\n╔══════════════════════════════════════════════════════════════════════╗\n'
        '║ INSTAGRAM SERIES — HARD CONTRACT FOR THIS SCRIPT — VIOLATING = FAIL  ║\n'
        '╚══════════════════════════════════════════════════════════════════════╝\n'
        '1. COLD-OPEN HOOK (seconds 0-5) — MANDATORY. Drop the viewer into action / '
        '   conflict / question already in motion. Examples of valid opens:\n'
        '     • Mid-shout / mid-slap / mid-grab\n'
        '     • A line of dialogue that contains a reveal or accusation\n'
        '     • A door slamming open / a phone ringing repeatedly / a body on floor\n'
        '     • A character running mid-stride\n'
        '   FORBIDDEN opens: establishing shot of a building, slow zoom, narration, '
        '   "next morning", "two days later", small-talk warm-up, character making '
        '   coffee / sewing / journaling.\n'
        '2. ESCALATION — every dialogue exchange must reveal, flip, or raise stakes. '
        '   No filler. No "checking in" scenes. No tea-time chat. If a scene does not '
        '   move the plot, delete it.\n'
        '3. CLIFFHANGER on the LAST LINE / LAST FRAME — MANDATORY. Examples of valid '
        '   closes:\n'
        '     • A reveal / name spoken / face seen\n'
        '     • An arrival (door opens, car pulls up, knock)\n'
        '     • A phone showing a message / a photograph\n'
        '     • A weapon raised, a shot fired offscreen\n'
        '     • A line of dialogue that flips everything ("That\'s not my daughter.")\n'
        '   FORBIDDEN closes: character looking at sunset, journaling, smiling, '
        '   resolved hug, "everything will be okay", "and so they...", any wrap-up.\n'
        '4. DROP-IN FRIENDLY — assume the viewer never saw earlier episodes. Within '
        '   the first 10 seconds they must understand WHO this is and WHAT is at '
        '   stake through context (a costume cue, a line, a reaction). No exposition '
        '   dump, no "previously on", no narrator.\n'
        '5. BANNED PLOT ENGINES (none of these may drive the episode):\n'
        '     • lawsuits, court hearings, trials, depositions, judges, lawyers, '
        '       paralegals, legal motions, evidence binders\n'
        '     • eviction filings, paperwork submissions, signing contracts as climax\n'
        '     • tenant unions filing complaints, building inspectors arriving with forms\n'
        '   If the synopsis above leans on any of these — REWRITE the beat into a '
        '   FACE-TO-FACE confrontation, chase, betrayal, reveal, blackmail, escape, '
        '   or physical clash. People doing things to people. Not paperwork.\n'
        '6. DIALOGUE — short and clipped. Two-line clarity: one line states, the next '
        '   flips. No monologues. No "let me explain" speeches. No banter padding.\n'
        '7. LOCATIONS — vary as the plot demands. Do NOT lock the whole episode (or '
        '   the season) to one room. Move where the action goes.\n'
        '═══════════════════════════════════════════════════════════════════════\n\n'
    )

    if batch:
        a, b = chunk_range(s, num)
        if series_format_mode == 'instagram_series':
            # Self-contained chapters of a serialized mini-drama — each sub-ep ends
            # on its own cliffhanger; the chunk overall escalates into chunk+1.
            instruction = (
                ig_hard_block +
                f'Write the complete script for {unit_label}. '
                f'It must contain {bs} self-contained chapters (sub-eps {a} through {b}), each ending with the EXACT cut marker '
                f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
                f'Each sub-episode = SHARP cold-open hook (first 3-5s) → escalating conflict → cliffhanger turn at the end. '
                f'Apply ALL 7 rules of the HARD CONTRACT to every single sub-episode in this chunk. '
                f'The chunk overall ends on the sharpest cliffhanger pulling into chunk {num+1}. '
            )
        else:
            instruction = (
                f'Write the complete script for {unit_label}. '
                f'It must contain {bs} sub-episodes (sub-eps {a} through {b}), each ending with the EXACT cut marker '
                f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
                f'The chunk overall has its own midpoint REVERSAL and a final cliffhanger that leads into chunk {num+1}. '
            )
    else:
        if series_format_mode == 'instagram_series':
            instruction = (
                ig_hard_block +
                f'Write the complete script for Episode {num}. '
                + ('Open on a SHARP COLD-OPEN HOOK in the first 3-5 seconds — drop us into action / conflict / line-mid-confrontation. NO setup, NO establishing shot. ' if not prev_script else
                   'Open on a SHARP COLD-OPEN HOOK tied to where the story left off — drop us back in mid-action. A fresh viewer must catch up in 10 seconds through context, NOT exposition. ')
                + 'Escalate through clipped dialogue — every line reveals, flips, or raises stakes. '
                + ('This is the SERIES FINALE — resolve every thread and end on a CONCLUSIVE final beat (see the FINALE CONTRACT below). Do NOT end on a cliffhanger and ignore HARD CONTRACT rule 3. '
                   if _is_finale else
                   'End on a HARD CLIFFHANGER on the final line / frame. Apply ALL 7 rules of the HARD CONTRACT above.')
            )
        else:
            # Length budget — series-level target_duration_sec (default 60s).
            try:
                _target_sec = int(s.get('target_duration_sec') or 60)
            except (TypeError, ValueError):
                _target_sec = 60
            # Spoken words ≈ 100 words per 60s of TikTok-paced drama (135 wpm
            # speaking rate w/ pauses). Floor at 75% of target — model was
            # observed undershooting to ~50 spoken words on a 100-word target
            # because the prior phrasing was «MAXIMUM, never exceed, cut
            # mercilessly». Now framed as a RANGE the model must HIT, not a
            # ceiling to fear.
            _spoken_target = round(_target_sec / 60 * 100)
            _spoken_floor = round(_spoken_target * 0.75)
            _spoken_ceiling = round(_spoken_target * 1.10)
            # Dialogue lines (each NAME: line) — separate from action.
            _dlg_target = max(3, min(40, round(_target_sec / 4.5)))
            _dlg_floor = max(3, round(_dlg_target * 0.75))
            _dlg_ceiling = _dlg_target + 3
            _action_budget = max(3, round(_target_sec / 12))
            instruction = (
                f'Write the complete script for Episode {num}. '
                + ('Continue naturally from where Episode {prev} ended.'.format(prev=num-1) if prev_script else 'Hook the viewer immediately.')
                + (' This is the SERIES FINALE — resolve every open thread and end conclusively (see the FINALE CONTRACT below); do NOT end on a cliffhanger. ' if _is_finale else ' End on a cliffhanger. ')
                + f'\n\n⚠ БЮДЖЕТ ДЛИНЫ — серия ≈ {_target_sec} секунд экрана.\n'
                + f'\n📣 РЕЧЕВЫЕ СЛОВА (только то, что произносят персонажи — слова после «NAME:» / в кавычках):\n'
                + f'• ЦЕЛЬ: {_spoken_target} слов. ДИАПАЗОН: {_spoken_floor}–{_spoken_ceiling}.\n'
                + f'• НЕ НЕДОБИРАЙ ниже {_spoken_floor} — иначе сцена пустая, аудио короче нужного.\n'
                + f'• НЕ ПРЕВЫШАЙ {_spoken_ceiling} — иначе сцена не влезет в {_target_sec}с.\n'
                + f'• Каждая реплика: 3-8 слов в среднем, максимум 10. >10 слов → РАЗБЕЙ на две короткие реплики.\n'
                + f'• МОНОЛОГОВ НЕТ. Короткие рваные удары. Никаких речей «Your Honor, I would like to explain…».\n'
                + f'\n💬 РЕПЛИКИ (количество строк диалога):\n'
                + f'• ЦЕЛЬ: {_dlg_target} строк диалога. ДИАПАЗОН: {_dlg_floor}–{_dlg_ceiling}.\n'
                + f'\n🎬 ACTION / BLOCKING (описание движения, [BLOCKING] блоки, ремарки):\n'
                + f'• Это ОТДЕЛЬНЫЙ бюджет — НЕ СЧИТАЕТСЯ как речевые слова.\n'
                + f'• Максимум ~{_action_budget} action-строк. Описывай только ключевое движение, не каждое мелкое.\n'
                + f'• [BLOCKING] / [BLOCKING_END] блоки пиши столько сколько нужно — они вне счёта.\n'
                + f'• ЗАПРЕЩЕНЫ time-markers в action-lines: «слушает две минуты молча», «молчит десять секунд», '
                + f'«проходит минута» — это раздувает хронометраж экрана.\n'
                + f'\n✅ ПРОВЕРЬ ПЕРЕД ВЫВОДОМ:\n'
                + f'• Речевых слов в диапазоне {_spoken_floor}–{_spoken_ceiling}? (action и BLOCKING НЕ В СЧЁТ)\n'
                + f'• Строк диалога в диапазоне {_dlg_floor}–{_dlg_ceiling}?\n'
                + f'• Action-строк не больше {_action_budget}?\n'
                + f'• Это короткая драма для TikTok/Reels — НЕ полнометражный сценарий, но и НЕ обрубок на 50 слов.\n'
                + f'• Программный детектор проверит спикерские слова + длину каждой реплики; нарушения → автоматическая перезапись.'
            )

    base_prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Series arc: {s.get("arc","")}\n\n'
        + _anthro_world_block(s)  # ← furry/anthro world directive (empty for human worlds)
        + _revision_instructions_block(s)  # ← clone-time revisions (empty unless cloned w/ edits)
        + trajectory_block        # ← user-pinned finale + checkpoints, FIRST so it's impossible to miss
        + bridge_block            # ← concrete per-episode plan from current state to finale
        + crowd_block             # ← HARD limit on characters per scene, lifted up front
        + format_block
        + (cast_block + '\n\n' if cast_block else '')
        + brief_block
        + devices_block
        + prev_block
        + f'{unit_label} SYNOPSIS (may be stale — see THIS EPISODE\'S BEAT below):\n{ep.get("synopsis","")}\n\n'
        + (
            (
                f'╔══════════════════════════════════════════════════════════════════╗\n'
                f'║ 🎯 THIS EPISODE\'S BEAT — execute exactly this, override synopsis ║\n'
                f'╚══════════════════════════════════════════════════════════════════╝\n'
                + (f'СОСТОЯНИЕ МИРА В НАЧАЛЕ ЭТОЙ СЕРИИ (Ep {num}):\n  {bridge_state_before}\n\n'
                   if bridge_state_before else '')
                + (f'TEMPORAL ANCHOR: финальные события (Ep {bridge_finale_ep or "—"}) ЕЩЁ НЕ ПРОИЗОШЛИ. '
                   f'Не пиши сцены так будто кто-то уже арестован/осуждён/мёртв, если по плану это случится позже.\n\n'
                   if bridge_finale_ep else '')
                + f'Эта серия (Ep {num}) ОБЯЗАНА выполнить следующий beat из плана-моста к финалу:\n\n'
                f'  ▶▶▶ {bridge_this_beat}\n\n'
                f'IF THE SYNOPSIS ABOVE CONTRADICTS THIS BEAT — THE BEAT WINS. The synopsis may have\n'
                f'been generated before the finale was pinned and is now stale. The beat above is\n'
                f'the authoritative instruction. Write the script to deliver THIS BEAT.\n'
                f'Do NOT invent new named characters not in the cast.\n'
                f'Do NOT introduce a subplot that the bridge plan does not include.\n'
                f'Do NOT depict characters in their FINALE-state (arrested, sentenced, exposed) — '
                f'use their CURRENT state from the СОСТОЯНИЕ МИРА block above.\n'
                f'═══════════════════════════════════════════════════════════════════\n\n'
            ) if bridge_this_beat else ''
        )
        + next_block
        + narrative_block
        + instruction
        + ' EVERY constraint in the LOGIC CONSTRAINTS block above is mandatory — violating canon is a hard fail. '
        + 'And EVERY landmark in the NARRATIVE TRAJECTORY block at the top is mandatory — drift from the finale is a hard fail. '
        + ('The THIS EPISODE\'S BEAT above is the authoritative scene direction — execute it. '
           'If the synopsis or prev_script set up a different subplot, fold it into the beat or park it; '
           'NEVER continue a subplot the bridge plan does not include.' if bridge_this_beat else '')
        # ← SERIES FINALE override: appended LAST so it trumps the cliffhanger
        #   mandate above. Empty string for every non-finale episode.
        + build_finale_contract_block(s, num)
    )

    script_system = _build_batch_script_system(s) if batch else _build_script_system(s)

    try:
        # ─── WRITE → AUDIT → RETRY loop (max 2 retries, fully automated)
        MAX_RETRIES = 2
        script = ''
        audit_report = {'passes': True, 'violations': [], 'retries': 0}
        prompt = base_prompt
        for attempt in range(MAX_RETRIES + 1):
            script = claude_ask(prompt, system=script_system)
            report = audit_script(sid, num, script, brief)
            # Run logic-hole auditor in parallel with continuity auditor — different concerns.
            logic_report = audit_logic_holes(sid, num, script)
            # Programmatic check for scene overcrowding — reliable signal that Claude-based
            # auditor sometimes misses. We count NAMED-CAST speakers per scene and flag any
            # scene that exceeds the user's max_main_chars_per_scene setting.
            crowd_violations = detect_scene_overcrowding(s, script)
            crowd_critical = []
            for cv in crowd_violations:
                names_str = ', '.join(cv['characters'])
                crowd_critical.append({
                    'type': 'scene_overcrowding',
                    'severity': 'critical',
                    'where': f"сцена {cv['scene_idx']} ({cv['location']})",
                    'explanation': (
                        f"в сцене {cv['count']} именованных персонажа из каста "
                        f"({names_str}), а лимит сериала = {cv['limit']}. "
                        f"Программный детектор посчитал диалоговые реплики и BLOCKING-разметку."
                    ),
                    'fix': (
                        f"Перепиши сцену так чтобы говорящих/действующих именованных персонажей "
                        f"было НЕ БОЛЕЕ {cv['limit']}. Варианты: (a) убери из сцены лишних персонажей "
                        f"(они могут появиться в ОТДЕЛЬНОЙ последовательной сцене); "
                        f"(b) разбей сцену на две — сначала одна группа, потом другая входит после ухода первой; "
                        f"(c) оставь лишних только в фоне без реплик и без [BLOCKING] упоминания."
                    ),
                })
            if crowd_critical:
                print(f'[scene-crowd] ep {num} attempt {attempt+1}: {len(crowd_critical)} overcrowded scene(s) detected programmatically', flush=True)
                for cv in crowd_violations:
                    print(f'[scene-crowd]   scene {cv["scene_idx"]} ({cv["location"]}): {cv["count"]}>{cv["limit"]} — {", ".join(cv["characters"])}', flush=True)
            # Programmatic no-name-character check — every on-camera/speaking character
            # MUST have a unique proper name + cast-block line so its reference binds.
            # Bare role cues (CLIENT, OLD WOMAN) and uncast named speakers force a rewrite.
            naming_critical = detect_unnamed_characters(s, script)
            if naming_critical:
                print(f'[name-check] ep {num} attempt {attempt+1}: {len(naming_critical)} unnamed/uncast character(s) — '
                      + ', '.join(v['where'] for v in naming_critical), flush=True)
            # ── Programmatic over-length detector — count dialogue lines and spoken words,
            # estimate runtime, fail if >30% over the target duration.
            length_critical = []
            length_violation = detect_script_overlength(s, script)
            if length_violation:
                lv = length_violation
                reasons_str = '; '.join(lv.get('reasons') or [])
                # Branch the fix message based on direction. Undershoot
                # (writer delivered too few spoken words) gets a different
                # corrective than overshoot (writer was too verbose).
                _floor_words = round(lv['target_words'] * 0.75)
                # Undershoot = rendered runtime falls below target (the renderer
                # would produce a clip shorter than the series setting). Word
                # count is the secondary signal.
                _undershoot = (lv['est_sec'] < lv['target_sec'] * 0.9) or (lv['dialogue_words'] < _floor_words)
                if _undershoot:
                    fix_msg = (
                        f"ДОПИШИ диалог до целевого объёма. Конкретно: "
                        f"(a) у тебя сейчас {lv['dialogue_words']} спикерских слов, нужно ~{lv['target_words']} "
                        f"(минимум {_floor_words}) — НЕДОБОР почти в два раза; "
                        f"(b) добавь {lv['target_words'] - lv['dialogue_words']}+ спикерских слов через "
                        f"новые короткие реплики (3-7 слов каждая), НЕ через длинные монологи; "
                        f"(c) ACTION/BLOCKING НЕ СЧИТАЮТСЯ — речь только то, что произносят персонажи "
                        f"после «NAME:» или в кавычках; "
                        f"(d) сцена развивается через диалог — добавь обмены репликами, реакции, "
                        f"подколы, угрозы, признания. НЕ через action-описания «он смотрит на неё»; "
                        + (f"(e) это ФИНАЛ — концовка остаётся конклюзивной (без клиффхэнгера), но к ней ведёт больше реплик."
                           if _is_finale else
                           f"(e) cliffhanger остаётся, но к нему ведёт больше реплик.")
                    )
                else:
                    fix_msg = (
                        f"СОКРАТИ беспощадно. Конкретно: "
                        f"(a) каждая реплика МАКСИМУМ 7 слов, идеал 3-5 (короткие рваные удары); "
                        f"(b) если есть монолог >10 слов — разрежь на короткие реплики ИЛИ удали лишнее; "
                        f"(c) action-строк не больше {max(3, round(lv['target_sec']/12))} — убери все 'смотрит / встаёт / делает паузу' если они не двигают сцену; "
                        f"(d) общий лимит: ~{lv['target_words']} спикерских слов, ~{lv['target_lines']} диалоговых строк суммарно; "
                        f"(e) НИКАКИХ time-markers вроде 'в течение двух минут' / 'десять секунд молча' — это раздувает хронометраж; "
                        + (f"(f) удали экспозицию и повторы — только живые удары + конклюзивная развязка ФИНАЛА (без клиффхэнгера). "
                           if _is_finale else
                           f"(f) удали экспозицию и повторы — только живые удары + cliffhanger. ")
                        + f"Это короткая драма для TikTok ({lv['target_sec']}с), не полнометражный сценарий."
                    )
                length_critical.append({
                    'type': 'script_overlength',
                    'severity': 'critical',
                    'where': 'весь сценарий серии',
                    'explanation': (
                        f"сценарий нарушает бюджет длины: {reasons_str}. "
                        f"Метрики: {lv['dialogue_lines']} реплик · {lv['dialogue_words']} слов · "
                        f"{lv['action_lines']} action-строк · самая длинная реплика {lv['longest_line_words']} слов · "
                        f"средняя {lv['avg_line_words']} слов · оценка ~{lv['est_sec']}с экрана. "
                        f"Лимит сериала: {lv['target_sec']}с, ~{lv['target_words']} слов, ~{lv['target_lines']} строк."
                    ),
                    'fix': fix_msg,
                })
                print(f'[script-length] ep {num} attempt {attempt+1}: {lv["est_sec"]}s ({lv["ratio"]}×), '
                      f'lines={lv["dialogue_lines"]} words={lv["dialogue_words"]} '
                      f'longest={lv["longest_line_words"]} avg={lv["avg_line_words"]} actions={lv["action_lines"]} '
                      f'reasons=[{reasons_str}]', flush=True)
            all_violations = list(report.get('violations', [])) + list(logic_report.get('violations', [])) + crowd_critical + length_critical + naming_critical
            critical = [v for v in all_violations if v.get('severity') == 'critical']
            audit_report = {
                'passes': not critical,
                'violations': all_violations,
                'retries': attempt,
                'audit_error': report.get('audit_error') or logic_report.get('audit_error'),
                'logic_passes': logic_report.get('passes', True),
                'continuity_passes': report.get('passes', True),
                'crowd_violations': crowd_violations,
            }
            if not critical:
                break
            # Build a fix-it prompt and retry — group violations by source for clarity
            cont_fixes = [v for v in critical if v.get('type') in ('timeline','fact','knowledge','biology','setup','paperwork','scene_teleport')]
            logic_fixes = [v for v in critical if v.get('type') in ('status','hidden_position','enabling_condition','legal_term','unmotivated_delay','ambiguous_cliffhanger','protagonist_stagnation','emotional_monotony','scene_overcrowding','finale_drift','script_overlength','unnamed_character','uncast_character')]
            fixes_parts = []
            if cont_fixes:
                fixes_parts.append('CONTINUITY/CANON ISSUES:\n' + '\n'.join(
                    f"- [{v.get('type','?')}] {v.get('explanation','')} → FIX: {v.get('fix','')}" for v in cont_fixes))
            if logic_fixes:
                fixes_parts.append('STORY-LOGIC HOLES:\n' + '\n'.join(
                    f"- [{v.get('type','?')}] WHERE: {v.get('where','?')} | {v.get('explanation','')} → FIX: {v.get('fix','')}" for v in logic_fixes))
            prompt = (
                base_prompt
                + '\n\n═══ PREVIOUS DRAFT FAILED AUDIT ═══\n'
                + 'You MUST address every issue below. Do not repeat the same mistakes.\n'
                + '\n\n'.join(fixes_parts)
                + '\n═══════════════════════════════════════════════════\n'
                + 'Rewrite the entire script from scratch fixing ALL issues above while still respecting the LOGIC CONSTRAINTS.'
            )

        # ─── Save script + audit/brief metadata first
        # If there was a previous script, archive it before overwriting
        prev_script_text = (ep.get('script') or '').strip()
        if prev_script_text:
            ep.setdefault('script_history', []).append({
                'ts': time.time(),
                'reason': 'regenerate',
                'script': prev_script_text[:80000],
            })
            ep['script_history'] = ep['script_history'][-10:]
        ep['script'] = _normalize_blocking_tags(script)
        script = ep['script']   # use normalized version downstream
        ep['status'] = 'draft'
        ep['logic_brief'] = brief
        ep['audit_report'] = audit_report
        # Store end_position for continuity context (used by _prev_episode_ending_context)
        end_pos = _extract_end_position(script)
        if end_pos:
            ep['end_position'] = end_pos
        save_episode(sid, num, ep)
        # Extract and register plot devices + narrative state for anti-repetition tracking
        try:
            gen_devices = _extract_devices_from_script(script)
            if gen_devices:
                ep['plot_devices'] = gen_devices
                save_episode(sid, num, ep)
                _update_devices_index(sid, num, gen_devices)
        except Exception as _de:
            _log_event('WARN', 'device_extract_after_gen_failed', err=str(_de)[:200])
        try:
            gen_narrative = _extract_narrative_state_from_script(script)
            if gen_narrative:
                ep['narrative_state'] = gen_narrative
                save_episode(sid, num, ep)
                _update_narrative_index(sid, num, gen_narrative)
        except Exception as _ne:
            _log_event('WARN', 'narrative_extract_after_gen_failed', err=str(_ne)[:200])
        # Sync outfits from SCENE_OPEN blocks (non-blocking — failures are logged)
        try:
            _sync_script_outfits(sid, script)
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_gen_failed', err=str(_oe)[:200])

        # NOTE: cast-block sync is INTENTIONALLY NOT run after script-gen.
        # User wants explicit control — they'll click "🤖 Извлечь персонажей и локации"
        # to trigger /extract-characters when ready. Mark the episode so the auto-heal
        # paths (get_series, autogen pre-sweep) skip it until extraction happens.
        ep['cast_extracted'] = False
        save_episode(sid, num, ep)
        sync_result = {'created_characters': [], 'created_outfits': [], 'created_locations': [],
                       'detected_locations': [], 'healed_refs': 0,
                       'characters_used': ep.get('characters_used', []),
                       'character_outfits': ep.get('character_outfits', {}),
                       'locations_used': ep.get('locations_used', [])}
        print(f'[script-gen {sid}/ep{num}] script saved, cast extraction pending — user must click extract', flush=True)

        # Fallback: scan script text for location names (flexible match)
        s = load_series(sid)  # reload to get the synced state
        ep = load_episode(sid, num)
        locs = s.get('locations', [])
        script_upper = script.upper()
        def loc_in_script(name):
            u = name.upper()
            if u in script_upper: return True
            no_article = re.sub(r'^(THE|AN?)\s+', '', u)
            if no_article != u and no_article in script_upper: return True
            words = [w for w in u.split() if len(w) > 2]
            return bool(words) and all(w in script_upper for w in words)
        detected_locs = [l['id'] for l in locs if loc_in_script(l['name'])]
        ep['locations_used'] = list(set(ep.get('locations_used', [])) | set(detected_locs))
        save_episode(sid, num, ep)

        # ─── POST-WRITE: extract canon updates from finalized script
        try:
            extract_result = extract_canon_updates(sid, num, script)
        except Exception as e:
            extract_result = {'error': str(e)}

        # Append to canon audit log
        try:
            canon = load_canon(sid)
            canon.setdefault('audit_log', []).append({
                'ep': num,
                'ts': time.time(),
                'passes': audit_report['passes'],
                'retries': audit_report['retries'],
                'violations': [
                    {'type': v.get('type'), 'severity': v.get('severity'), 'explanation': v.get('explanation')}
                    for v in audit_report['violations']
                ],
            })
            canon['audit_log'] = canon['audit_log'][-100:]  # cap
            save_canon(sid, canon)
        except Exception:
            pass

        # Auto-generate any newly-introduced character/outfit/location images in background
        trigger_autogen_if_enabled(sid)
        return jsonify({
            'script': script,
            'characters_used':  ep['characters_used'],
            'character_outfits': ep['character_outfits'],
            'locations_used':   ep['locations_used'],
            'logic_brief': brief,
            'audit_report': audit_report,
            'canon_update': extract_result,
            'series': s,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def sync_episode_with_cast_block(sid, num):
    """Reconcile episode's character_used / character_outfits IDs with current series state.
    Idempotent — safe to call repeatedly. Re-parses the cast block from the script and:
      - re-creates any missing characters / outfits in series.json (with new UUIDs)
      - rewrites episode.characters_used and character_outfits to use current valid IDs
      - drops any stale IDs that no longer resolve
    Returns dict {created_chars: [...], created_outfits: [...], healed_refs: int}."""
    s = load_series(sid)
    if not s: return {'error': 'series not found'}
    ep = load_episode(sid, num)
    if not ep or not (ep.get('script') or '').strip(): return {'error': 'episode has no script'}

    chars = s.setdefault('characters', [])
    pre_char_ids = {c['id'] for c in chars}
    pre_outfit_ids = {(c['id'], o['id']) for c in chars for o in c.get('outfits', [])}

    cast_chars, cast_outfits = _parse_cast_block(ep['script'], chars)

    # Track what was just created
    created_chars = [c['name'] for c in chars if c['id'] not in pre_char_ids]
    created_outfits = [
        f"{c['name']}/{o['label']}"
        for c in chars for o in c.get('outfits', [])
        if (c['id'], o['id']) not in pre_outfit_ids
    ]

    # Heal episode refs: drop IDs that no longer exist; merge in cast_block IDs
    valid_char_ids = {c['id'] for c in chars}
    valid_outfit_ids = {(c['id'], o['id']) for c in chars for o in c.get('outfits', [])}

    # characters_used — start from cast_chars (authoritative), keep any extra valid IDs
    healed = 0
    if cast_chars:
        new_used = list(dict.fromkeys(cast_chars))  # dedup, preserve order
        # Keep any extra valid pre-existing IDs (e.g. silent characters not in cast block)
        for cid in ep.get('characters_used', []):
            if cid in valid_char_ids and cid not in new_used:
                new_used.append(cid)
        if ep.get('characters_used') != new_used:
            healed += 1
            ep['characters_used'] = new_used
    else:
        # No cast block parsed — just drop dead IDs
        cleaned = [cid for cid in ep.get('characters_used', []) if cid in valid_char_ids]
        if cleaned != ep.get('characters_used'):
            healed += 1
            ep['characters_used'] = cleaned

    # character_outfits — rewrite from cast_outfits (now LISTS), drop dead refs.
    # cast_outfits is the authoritative source for which outfits the script demands.
    # We also union in any pre-existing valid outfit refs the user manually added.
    new_outfits = {}
    for cid, oids in (cast_outfits or {}).items():
        oids_list = oids if isinstance(oids, list) else [oids]
        kept = [oid for oid in oids_list if (cid, oid) in valid_outfit_ids]
        if kept:
            new_outfits[cid] = list(dict.fromkeys(kept))  # dedup preserve order
    # Preserve any pre-existing valid outfit refs (legacy single-string OR list)
    for cid, raw in (ep.get('character_outfits') or {}).items():
        prev_ids = _outfit_ids(raw)
        existing = new_outfits.setdefault(cid, [])
        for oid in prev_ids:
            if (cid, oid) in valid_outfit_ids and oid not in existing:
                existing.append(oid)
        if not existing:
            new_outfits.pop(cid, None)
    if ep.get('character_outfits') != new_outfits:
        healed += 1
        ep['character_outfits'] = new_outfits

    # ─── Locations — parse scene headings (INT./EXT./ИНТА./ЭКС.) and tick matching series locations.
    # Scene heading format we emit: "ИНТА. HOTEL ROOM — УТРО" / "INT. BOARDROOM — DAY".
    # We grab the middle slug (location name) and fuzzy-match against series.locations by name.
    locs = s.setdefault('locations', [])
    detected_loc_names = []
    # Allow leading markdown decorators (**, __, #, >) before the INT./EXT. cue —
    # writers sometimes emit `**INT. RANCH HOUSE — MORNING**` for visual emphasis,
    # and the old regex silently dropped those whole headings, leaving the
    # location un-tied to the episode.
    heading_re = re.compile(
        r'^[\s*_#>]*(?:INT\.|EXT\.|ИНТА?\.|ЭКС?\.|INT/EXT\.|EXT/INT\.)\s*([^—\-\n]+?)\s*[—\-]',
        re.IGNORECASE | re.MULTILINE,
    )
    for m in heading_re.finditer(ep.get('script') or ''):
        # Strip trailing markdown closers (**, __) that may be on the last token
        nm = m.group(1).strip().strip('"').strip("'").rstrip('*_').strip()
        # Strip leading time-of-day artefacts that sometimes leak into the name
        nm = re.sub(r'^(DAY|NIGHT|MORNING|EVENING|УТРО|НОЧЬ|ДЕНЬ|ВЕЧЕР)\s+', '', nm, flags=re.IGNORECASE).strip()
        if nm and nm.lower() not in (x.lower() for x in detected_loc_names):
            detected_loc_names.append(nm)

    detected_loc_ids = []
    created_loc_names = []
    for nm in detected_loc_names:
        nl = nm.lower()
        match = next((l for l in locs if l['name'].lower() == nl), None)
        if not match:
            # fuzzy: substring either way (e.g. "HOTEL ROOM" vs "Hotel Suite Room")
            match = next(
                (l for l in locs if nl in l['name'].lower() or l['name'].lower() in nl),
                None,
            )
        if not match:
            # AUTO-CREATE: if the script uses a scene heading we don't have on file,
            # add it to series.locations on the fly so the checkbox CAN be ticked.
            # This is the fix for "локации без галочек" — previously we silently dropped
            # any heading that didn't match an existing entry. The series-locations list
            # was often populated from the synopsis, while the script invented its own
            # places, so nothing ever matched. Now the script is the source of truth.
            new_loc = {
                'id': str(uuid.uuid4())[:8],
                'name': nm.title() if nm.isupper() or nm.islower() else nm,
                'description': 'Auto-created from episode script scene heading',
                'ref_images': [],
            }
            locs.append(new_loc)
            created_loc_names.append(new_loc['name'])
            match = new_loc
        if match:
            if match['id'] not in detected_loc_ids:
                detected_loc_ids.append(match['id'])

    valid_loc_ids = {l['id'] for l in locs}
    new_locs_used = list(detected_loc_ids)
    # Keep any pre-existing user-ticked locations that are still valid (don't drop manual picks)
    for lid in ep.get('locations_used', []) or []:
        if lid in valid_loc_ids and lid not in new_locs_used:
            new_locs_used.append(lid)
    if ep.get('locations_used') != new_locs_used:
        healed += 1
        ep['locations_used'] = new_locs_used

    # ── No-name cue → card auto-alias (heals already-written episodes) ──────────
    # If the script addresses a character by a bare role label (e.g. «CLIENT:»)
    # that differs from its card name (e.g. «Mrs. Park»), the binding filter
    # would drop the reference in any chunk that uses only the role label →
    # the model renders the wrong face. We conservatively link the orphan label
    # to its card as an alias so `_char_name_in_text` keeps the ref. Only fires
    # when the mapping is UNAMBIGUOUS: exactly one un-cued role label and exactly
    # one episode card that nothing else claims. Anything ambiguous is left for
    # the write-time detector / user to fix.
    linked_aliases = []
    unnamed_warnings = []
    try:
        ep_cards = [c for c in chars if c['id'] in set(ep.get('characters_used', []))]
        if ep_cards:
            def _resolve_label(lab):
                ll = lab.lower()
                for c in ep_cards:
                    if (c.get('name') or '').lower() == ll:
                        return c
                    if ll in {(a or '').lower() for a in (c.get('aliases') or [])}:
                        return c
                return _resolve_char_by_script_name(lab, ep_cards)
            claimed, generic_orphans, named_orphans = set(), {}, {}
            for lab, _kind in _extract_speaker_and_blocking_labels(ep['script']):
                c = _resolve_label(lab)
                if c:
                    claimed.add(c['id'])
                elif _label_is_unnamed(lab):
                    generic_orphans.setdefault(lab.lower(), lab)
                else:
                    named_orphans.setdefault(lab.lower(), lab)
            unclaimed = [c for c in ep_cards if c['id'] not in claimed]
            # Auto-link ONLY when unambiguous: a single bare role label and a
            # single un-cued card. A named-but-uncast orphan is NOT auto-aliased
            # (it needs its own card) — it's surfaced as a warning instead.
            if len(generic_orphans) == 1 and not named_orphans and len(unclaimed) == 1:
                lab_low, lab_orig = next(iter(generic_orphans.items()))
                card = unclaimed[0]
                al = card.setdefault('aliases', [])
                if lab_low not in {(a or '').lower() for a in al}:
                    al.append(lab_low)
                    linked_aliases.append({'alias': lab_orig, 'char': card.get('name'), 'char_id': card['id']})
                    _log_event('INFO', 'cue_auto_aliased', sid=sid, ep_num=num,
                               alias=lab_orig, char=card.get('name'), char_id=card['id'])
            else:
                for lab in list(generic_orphans.values()) + list(named_orphans.values()):
                    unnamed_warnings.append(lab)
    except Exception as _ae:
        _log_event('WARN', 'cue_auto_alias_failed', sid=sid, ep_num=num, err=str(_ae)[:200])

    save_series(sid, s)
    save_episode(sid, num, ep)
    return {
        'created_characters': created_chars,
        'created_outfits': created_outfits,
        'created_locations': created_loc_names,
        'detected_locations': detected_loc_names,
        'healed_refs': healed,
        'characters_used': ep['characters_used'],
        'character_outfits': ep['character_outfits'],
        'locations_used': ep['locations_used'],
        'linked_aliases': linked_aliases,
        'unnamed_warnings': unnamed_warnings,
    }


def _infer_gender_from_script(name, script):
    """Count gendered pronouns within ±3 lines of any line mentioning `name`.
    Returns 'female' / 'male' / None. Handles English + Russian.
    Used as a robust fallback when the cast block omits GENDER for a character —
    the previous tiny hardcoded female-name whitelist defaulted everyone-not-listed
    to 'male', which silently turned protagonists like LYDIA into men."""
    if not name or not script:
        return None
    import re as _re
    name_lower = name.lower()
    lines = script.splitlines()
    he_count = 0
    she_count = 0
    EN_HE  = _re.compile(r"\b(he|his|him|himself|he's|he'd|he'll)\b", _re.IGNORECASE)
    EN_SHE = _re.compile(r"\b(she|her|hers|herself|she's|she'd|she'll)\b", _re.IGNORECASE)
    RU_HE  = _re.compile(r"\b(он|его|ему|им|нём|него)\b", _re.IGNORECASE)
    RU_SHE = _re.compile(r"\b(она|её|ее|ей|ней|неё)\b", _re.IGNORECASE)
    for i, line in enumerate(lines):
        if name_lower not in line.lower():
            continue
        window = ' '.join(lines[max(0, i-3): min(len(lines), i+4)])
        he_count  += len(EN_HE.findall(window))  + len(RU_HE.findall(window))
        she_count += len(EN_SHE.findall(window)) + len(RU_SHE.findall(window))
    # Require a meaningful margin to avoid noise from generic "he was talking to her"
    if she_count >= 3 and she_count > he_count * 1.3:
        return 'female'
    if he_count >= 3 and he_count > she_count * 1.3:
        return 'male'
    return None


def _parse_cast_block(script, chars):
    """Parse === EPISODE CAST === block, auto-create new characters & outfits.
    Mutates `chars` (the series characters list) — caller must save_series afterwards.
    Returns (char_id_list, {char_id: outfit_id})."""
    import re as _re
    match = _re.search(r'=== EPISODE CAST ===(.*?)=== END CAST ===', script, _re.DOTALL)
    if not match:
        return [], {}

    def _norm(s):
        return (s or '').strip().strip('"').strip("'")

    def _find_char(name, chars_list):
        nl = name.lower()
        for c in chars_list:
            if c['name'].lower() == nl:
                return c
        # fuzzy: first word match
        first = nl.split()[0] if nl.split() else nl
        for c in chars_list:
            if first and first in c['name'].lower():
                return c
        return None

    char_ids, outfit_map = [], {}  # outfit_map: {char_id: [outfit_id, ...]} (preserves order, dedups)

    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line or not line.upper().startswith('CHARACTER:'):
            continue

        # Parse pipe-separated KEY: VALUE pairs
        fields = {}
        parts = line.split('|')
        for p in parts:
            if ':' not in p:
                continue
            k, v = p.split(':', 1)
            fields[k.strip().upper()] = _norm(v)

        raw_name = fields.get('CHARACTER', '')
        if not raw_name:
            continue

        # Strip trailing parentheticals like "Claire (mother)"
        raw_name = _re.sub(r'\s*\(.*?\)\s*$', '', raw_name).strip()

        char = _find_char(raw_name, chars)
        char_was_just_created = False

        # Auto-create unknown character
        if not char:
            char_was_just_created = True
            gender = fields.get('GENDER', '').lower()
            if gender not in ('male', 'female'):
                # Robust fallback: scan the script for gendered pronouns near this
                # character's name. Beats the old tiny female-name whitelist which
                # silently defaulted everyone (Lydia, Sarah, Emma, etc.) to male.
                inferred = _infer_gender_from_script(raw_name, script)
                if inferred:
                    gender = inferred
                else:
                    gender = 'female' if any(t in raw_name.lower() for t in [
                        'claire','elena','victoria','anna','maria','lina','lydia','sarah',
                        'emma','sophia','olivia','ava','isabella','mia','charlotte',
                        'amelia','harper','evelyn','abigail','grace','chloe','luna',
                        'ella','aurora','clara','rose','ruby','hazel','lily',
                    ]) else 'male'
            look = fields.get('LOOK', '') or fields.get('APPEARANCE', '')
            od = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
            # Fallback: if writer omitted LOOK, synthesize a baseline appearance.
            if not look:
                gword = 'woman' if gender == 'female' else 'man'
                look = f"young {gword}, photogenic, neutral attractive features, mid-20s to mid-30s"
            # First appearance defines the BASE outfit. Whatever this character is
            # wearing on their introduction — that's their default look (a bear-builder
            # IS in construction gear by default; he doesn't have separate "regular"
            # clothes hidden somewhere). Embed OUTFIT_DESC into appearance so the base
            # portrait gen has the clothing description; the outfit entry below will
            # also be flagged is_base so we never duplicate-generate it as a costume change.
            if od and 'wearing' not in look.lower():
                look = look.rstrip(' ,;') + f", wearing {od[:200]}"
            # Remember the OUTFIT label this character was introduced wearing.
            # Subsequent scenes that mention the SAME label are NOT a costume
            # change — they're just the character in their default look. Stored
            # on the character so the outfit-attachment logic below can skip
            # creating a redundant separate outfit entry. A real costume change
            # = a NEW label appearing in a later scene; only THEN do we create
            # an outfit (and never auto-flag it is_base — the base IS implicit).
            base_label_intro = (fields.get('OUTFIT', 'base').split('(')[0].strip().lower() or 'base')
            char = {
                'id': str(uuid.uuid4())[:8],
                'name': raw_name,
                'description': fields.get('ROLE', '') or f'Появляется в сценарии',
                'appearance':  look,
                'gender':      gender,
                'voice_id':    '',
                'ref_images':  [],
                'outfits':     [],
                'base_outfit_label': base_label_intro,  # implicit-base marker
            }
            chars.append(char)

        char_ids.append(char['id'])

        raw_outfit = fields.get('OUTFIT', 'base')
        raw_outfit = raw_outfit.split('(')[0].strip()
        if raw_outfit.lower() == 'base' or not raw_outfit:
            continue

        # ─── DEFENSIVE CHECK: writer encoded a SEPARATE PERSON in OUTFIT field ───
        # Common Claude failure mode: writes "CHARACTER: ARIA | OUTFIT: leo_child |
        # OUTFIT_DESC: small boy in navy hoodie..." — meaning Leo is a separate person,
        # not Aria's outfit. Detect this via two signals:
        #   1) outfit_label is the first-name token of an existing or canonical character
        #   2) OUTFIT_DESC contains a different-gender / different-age-class person word
        # If detected, treat the line as a NEW CHARACTER, not an outfit of the parent.
        outfit_desc_for_check = (fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')).lower()
        person_words_male   = (' boy ', ' man ', ' male ', ' father ', ' brother ', ' uncle ', ' grandfather ')
        person_words_female = (' girl ', ' woman ', ' female ', ' mother ', ' sister ', ' aunt ', ' grandmother ')
        person_words_child  = (' child ', ' kid ', ' toddler ', ' infant ', ' baby ')
        od_padded = ' ' + outfit_desc_for_check + ' '
        char_gender = (char.get('gender') or '').lower()
        # Gender mismatch — outfit_desc describes someone of the opposite gender
        gender_mismatch = (
            (char_gender == 'female' and any(w in od_padded for w in person_words_male))
            or (char_gender == 'male' and any(w in od_padded for w in person_words_female))
        )
        # Child mismatch — adult character but outfit_desc describes a child (and char is not flagged child)
        child_mismatch = any(w in od_padded for w in person_words_child) and 'child' not in (char.get('description','') + char.get('appearance','')).lower()
        # Outfit label looks like a person name (matches another character's first-name token)
        label_first = raw_outfit.split('_')[0].lower()
        label_is_person_name = False
        if label_first and len(label_first) >= 3:
            for other in chars:
                if other['id'] == char['id']:
                    continue
                if other['name'].lower().split()[0] == label_first:
                    label_is_person_name = True
                    break

        if (gender_mismatch or child_mismatch) and (label_is_person_name or '_' in raw_outfit):
            # Promote this line into its own CHARACTER. Use the outfit_label's first token
            # (capitalized) as the candidate name. Skip the outfit-attachment for the
            # parent character — the line was misencoded.
            new_name_guess = label_first.capitalize() if label_first else None
            if new_name_guess:
                # Try to find / create the standalone character
                spawned = _find_char(new_name_guess, chars)
                if not spawned:
                    if any(w in od_padded for w in person_words_child):
                        guessed_gender = 'male' if any(w in od_padded for w in person_words_male) else (
                            'female' if any(w in od_padded for w in person_words_female) else 'male'
                        )
                    else:
                        guessed_gender = 'male' if any(w in od_padded for w in person_words_male) else (
                            'female' if any(w in od_padded for w in person_words_female) else char_gender or 'female'
                        )
                    spawned = {
                        'id': str(uuid.uuid4())[:8],
                        'name': new_name_guess,
                        'description': f'Извлечён из OUTFIT_DESC ошибочно вписанного как образ персонажа {char["name"]}',
                        'appearance': fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', ''),
                        'gender': guessed_gender,
                        'voice_id': '',
                        'ref_images': [],
                        'outfits': [],
                    }
                    chars.append(spawned)
                # Roll back: remove parent char_id we appended IF this line was the only
                # reason it was added (parent had no other valid outfit refs in this pass).
                # Simpler: just append spawned and DROP the misencoded outfit. Keep parent
                # listed (they still appear in the script).
                if spawned['id'] not in char_ids:
                    char_ids.append(spawned['id'])
                print(f'[parse_cast] PROMOTED misencoded outfit "{raw_outfit}" of {char["name"]} '
                      f'→ standalone character "{spawned["name"]}" (od_excerpt="{outfit_desc_for_check[:60]}")')
                continue  # skip the outfit-attachment below

        # Skip outfit machinery entirely when this scene's OUTFIT matches the
        # character's IMPLICIT BASE LABEL — i.e. the look they were first
        # introduced wearing. That's not a costume change, it's the default
        # appearance, already baked into char.appearance. Creating a separate
        # outfit entry here would clutter the UI with a fake "outfit" chip on
        # the character card. The scene gets no outfit_map entry → renderers
        # fall back to the base ref photo automatically.
        base_label = (char.get('base_outfit_label') or '').lower()
        if base_label and raw_outfit.lower() == base_label:
            continue

        # Find existing outfit by label
        outfit = next((o for o in char.get('outfits', [])
                       if o.get('label','').lower() == raw_outfit.lower()), None)

        # IS_BASE marker: this outfit IS the character's base look — link to ref_images, no separate gen
        is_base_flag = fields.get('IS_BASE', '').lower() in ('true', 'yes', '1')

        # Auto-create new outfit if scene declares one with description.
        # NOTE: previously we auto-flagged the FIRST outfit of a freshly-created
        # character as is_base ("AUTO-BASE"). That's now obsolete — the implicit
        # base look is captured via base_outfit_label on the character itself
        # (see new-character creation block above), and matching scenes are
        # short-circuited via the `continue` above. Anything reaching this point
        # is a REAL costume change and gets a normal (non-base) outfit entry.
        if not outfit:
            outfit_desc = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
            if outfit_desc or raw_outfit.lower() != 'base':
                outfit = {
                    'id':          str(uuid.uuid4())[:8],
                    'label':       raw_outfit,
                    'description': outfit_desc,
                    'photo':       '',
                    'avai_url':    '',
                }
                char.setdefault('outfits', []).append(outfit)

        # Apply explicit IS_BASE flag from the cast block (writer override).
        if outfit and is_base_flag:
            # Unmark other outfits as base (only one base per char)
            for o in char.get('outfits', []):
                if o['id'] != outfit['id'] and o.get('is_base'):
                    o['is_base'] = False
            if char.get('ref_images') and not outfit.get('photo'):
                outfit['photo'] = char['ref_images'][0]
            if char.get('avai_base_url') and not outfit.get('avai_url'):
                outfit['avai_url'] = char.get('avai_base_url', '')
            outfit['is_base'] = True
            # Keep base_outfit_label in sync so subsequent scenes with this label
            # also short-circuit instead of re-creating.
            char['base_outfit_label'] = outfit.get('label', '').lower()

        if outfit:
            lst = outfit_map.setdefault(char['id'], [])
            if outfit['id'] not in lst:
                lst.append(outfit['id'])

    return char_ids, outfit_map

