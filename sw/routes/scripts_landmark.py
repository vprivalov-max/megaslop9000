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
            # lazy: forward call into the generate section (runs post-boot in a
            # background thread, so the module is fully loaded by then)
            from sw.routes.scripts_generate import generate_episode_script
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
