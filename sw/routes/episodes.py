"""Episode CRUD routes: list, create (idempotent), get, update, overrides,
clear, delete."""
import threading
import time

from flask import jsonify, request

from sw.canon_index import (_extract_devices_from_script,
                            _extract_narrative_state_from_script,
                            _update_devices_index, _update_narrative_index)
from sw.core import app
from sw.logging_utils import _log_event
from sw.storage import (_normalize_blocking_tags, _sync_script_outfits,
                        episodes_dir, list_episodes, load_episode, load_series,
                        save_episode)

# ── Episodes ─────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/episodes', methods=['GET'])
def get_episodes(sid):
    eps = list_episodes(sid)
    # Self-heal stuck gen_status='generating' — if an episode hasn't had a new
    # seedance chunk added in the last 10 minutes AND has no in-flight chunks,
    # it's almost certainly leftover from a crashed range-gen worker that
    # forgot to flip the status away from 'generating'. Without this sweep
    # the UI hides the «ready» checkbox forever and user has to manually edit
    # the JSON. Cheap: runs only over eps marked 'generating', no LLM calls.
    healed = []
    now = int(time.time())
    for ep in eps:
        if ep.get('gen_status') != 'generating':
            continue
        chunks = ep.get('seedance_chunks') or []
        # Skip mid-startup (status='generating' just set by a fresh range-gen
        # worker, no chunks yet) — we can't tell from the server whether the
        # client is still working. False-positive heal would race with the
        # active runner.
        if not chunks:
            continue
        any_inflight = any(
            c.get('status') in ('submitting', 'pending', 'processing') for c in chunks
        )
        if any_inflight:
            continue   # real run in flight, leave alone
        # All chunks settled. Only heal if the last chunk was created more
        # than 10 minutes ago — a recent finished chunk could mean the client
        # is between chunks (compose for the next one).
        last_activity = max((c.get('created_at') or 0) for c in chunks)
        if (now - last_activity) < 600:
            continue
        healed.append(ep.get('number'))
        ep.pop('gen_status', None)
        try:
            save_episode(sid, ep.get('number'), ep)
        except Exception as e:
            print(f'[gen_status self-heal] save failed ep{ep.get("number")}: {e}', flush=True)
    if healed:
        print(f'[gen_status self-heal] {sid}: cleared stuck \'generating\' on episodes {healed}', flush=True)
    return jsonify(eps)

_CREATE_EP_LOCKS = {}
# Per-episode lock for serializing seedance chunks mutations (start / submit /
# poll / delete / heal). Critical because Flask is threaded=True and parallel
# auto-mode fires multiple /seedance/start calls in the same second — without
# locking they all read-modify-write the same episode JSON and last-writer-
# wins erases all but one chunk. Real bug: parallel batch fired 4 starts at
# 20:13:35, only 1 chunk record survived on disk.
from sw.locks import (
    _EPISODE_LOCKS,
    _EPISODE_LOCKS_GUARD,
    _episode_lock,
    _SERIES_LOCKS,
    _SERIES_LOCKS_GUARD,
    _series_lock,
)
# Idempotency cache for POST /episodes — avoids duplicate creation when the
# same POST fires twice (browser retry, ext, double-handler). Keyed by
# (sid, idempotency_key); value = (timestamp, response_dict). TTL 60s.
_CREATE_EP_IDEMPOTENCY = {}
_CREATE_EP_IDEMPOTENCY_TTL = 60

@app.route('/api/series/<sid>/episodes', methods=['POST'])
def create_episode(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    # Critical section: list_episodes → number-assign → save must be atomic per series.
    # Without this, two near-simultaneous POSTs racing on a slow disk produce
    # duplicate episodes (first grabs N, second sees N taken and falls to N+1).
    # PLUS idempotency — if client sends same Idempotency-Key twice (network
    # retry, double-handler), we replay the original response instead of
    # creating a second episode. Both layers protect against duplicates.
    idempotency_key = (request.headers.get('Idempotency-Key') or '').strip()
    lock = _CREATE_EP_LOCKS.setdefault(sid, threading.Lock())
    with lock:
        # Check idempotency cache inside the lock so we serialize the read+write
        if idempotency_key:
            now = time.time()
            # GC stale entries opportunistically
            stale = [k for k, (t, _) in list(_CREATE_EP_IDEMPOTENCY.items())
                     if now - t > _CREATE_EP_IDEMPOTENCY_TTL]
            for k in stale:
                _CREATE_EP_IDEMPOTENCY.pop(k, None)
            cached = _CREATE_EP_IDEMPOTENCY.get((sid, idempotency_key))
            if cached:
                return jsonify(cached[1]), 201

        episodes = list_episodes(sid)
        data = request.json or {}
        # Use requested number if provided and not already taken, otherwise auto-assign
        requested = data.get('number')
        existing_nums = {e['number'] for e in episodes}
        if requested and int(requested) not in existing_nums:
            num = int(requested)
        else:
            num = max((e['number'] for e in episodes), default=0) + 1
        ep = {
            'number': num,
            'title': data.get('title', ''),  # empty by default — UI shows "Эп. N" badge already
            'synopsis': data.get('synopsis', ''),
            'script': data.get('script', ''),
            'characters_used': data.get('characters_used', []),
            'notes': data.get('notes', ''),
            'reteller_prompt': data.get('reteller_prompt', ''),
            'status': 'draft',
            'reteller': {
                'project_id': None,
                'status': None,
                'video_url': None,
                'submitted_at': None,
            },
        }
        save_episode(sid, num, ep)
        if idempotency_key:
            _CREATE_EP_IDEMPOTENCY[(sid, idempotency_key)] = (time.time(), ep)
    return jsonify(ep), 201

@app.route('/api/series/<sid>/episodes/<int:num>', methods=['GET'])
def get_episode(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    return jsonify(ep)

@app.route('/api/series/<sid>/episodes/<int:num>', methods=['PUT'])
def update_episode(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    prev_script = (ep.get('script') or '').strip()
    if 'reteller' in data:
        ep['reteller'].update(data.pop('reteller'))
    # Normalize legacy [SCENE_OPEN]/[EPISODE_END] tags to [BLOCKING]/[BLOCKING_END] on save
    if 'script' in data and data['script']:
        data['script'] = _normalize_blocking_tags(data['script'])
    ep.update(data)
    save_episode(sid, num, ep)
    # If script was updated, sync outfits and extract plot devices
    new_outfits = []
    new_script = (ep.get('script') or '').strip()
    if 'script' in data and new_script and new_script != prev_script:
        try:
            new_outfits = _sync_script_outfits(sid, data['script'])
        except Exception as _e:
            _log_event('WARN', 'outfit_sync_failed', err=str(_e)[:200])
        try:
            upd_devices = _extract_devices_from_script(new_script)
            if upd_devices:
                ep['plot_devices'] = upd_devices
                save_episode(sid, num, ep)
                _update_devices_index(sid, num, upd_devices)
        except Exception as _de:
            _log_event('WARN', 'device_extract_on_update_failed', err=str(_de)[:200])
        try:
            upd_narrative = _extract_narrative_state_from_script(new_script)
            if upd_narrative:
                ep['narrative_state'] = upd_narrative
                save_episode(sid, num, ep)
                _update_narrative_index(sid, num, upd_narrative)
        except Exception as _ne:
            _log_event('WARN', 'narrative_extract_on_update_failed', err=str(_ne)[:200])
    elif 'script' in data:
        try:
            new_outfits = _sync_script_outfits(sid, data['script'])
        except Exception as _e:
            _log_event('WARN', 'outfit_sync_failed', err=str(_e)[:200])
    resp = dict(ep)
    if new_outfits:
        resp['_new_outfits'] = new_outfits
    return jsonify(resp)

@app.route('/api/series/<sid>/episodes/<int:num>/segment-auto-skips', methods=['PUT'])
def update_segment_auto_skips(sid, num):
    """Per-segment auto-mode skip flags. Body: {"skips": ["anchor1", ...]}.
    Anchors here = first dialogue/action line of segment, first ~60 chars trimmed.
    Segments with anchors in this list are SKIPPED during auto-mode generation.
    Default = empty list = all segments included."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    skips = body.get('skips') or []
    cleaned = []
    seen = set()
    for s in skips:
        if not isinstance(s, str): continue
        anchor = s.strip()[:60]
        if not anchor or anchor in seen: continue
        seen.add(anchor)
        cleaned.append(anchor)
    ep['segment_auto_skips'] = cleaned
    save_episode(sid, num, ep)
    return jsonify({'ok': True, 'count': len(cleaned)})


@app.route('/api/series/<sid>/episodes/<int:num>/line-overrides', methods=['PUT'])
def update_line_overrides(sid, num):
    """Per-line override flags for the scene-view (currently only 'close_up').
    Body: {"overrides": [{"anchor": "first ~60 chars of trimmed line", "flags": ["close_up"]}]}
    Anchors are matched against the trimmed line text at parse time. If the line
    is later edited, the override silently drops."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    overrides = body.get('overrides') or []
    cleaned = []
    seen = set()
    for o in overrides:
        if not isinstance(o, dict):
            continue
        anchor = (o.get('anchor') or '').strip()
        flags = o.get('flags') or []
        if not anchor:
            continue
        valid_flags = [f for f in flags if f in ('close_up',)]
        if not valid_flags:
            continue
        if anchor in seen:
            continue
        seen.add(anchor)
        cleaned.append({'anchor': anchor[:60], 'flags': valid_flags})
    ep['line_overrides'] = cleaned
    save_episode(sid, num, ep)
    return jsonify({'ok': True, 'count': len(cleaned)})


@app.route('/api/series/<sid>/episodes/<int:num>/segment-overrides', methods=['PUT'])
def update_segment_overrides(sid, num):
    """Persist user's manual segment-split corrections for the scene-view.
    Body: {"overrides": [{"anchor": "first ~60 chars of line", "action": "break"|"merge"}]}
    Anchors are matched against the trimmed line text at parse time. If the line
    is later edited, the override silently drops (anchor no longer matches)."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    overrides = body.get('overrides') or []
    cleaned = []
    seen_anchors = set()
    for o in overrides:
        if not isinstance(o, dict):
            continue
        anchor = (o.get('anchor') or '').strip()
        action = o.get('action')
        if not anchor or action not in ('break', 'merge'):
            continue
        if anchor in seen_anchors:
            continue           # dedupe — keep first
        seen_anchors.add(anchor)
        cleaned.append({'anchor': anchor[:60], 'action': action})
    ep['segment_overrides'] = cleaned
    save_episode(sid, num, ep)
    return jsonify({'ok': True, 'count': len(cleaned)})


@app.route('/api/series/<sid>/episodes/<int:num>/clear', methods=['POST'])
def clear_episode(sid, num):
    """Reset episode content (synopsis, script, cast) but keep the episode slot."""
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    ep['synopsis'] = ''
    ep['script'] = ''
    ep['characters_used'] = []
    ep['character_outfits'] = {}
    ep['locations_used'] = []
    ep['notes'] = ''
    ep['reteller_prompt'] = ''
    ep['status'] = 'draft'
    ep['reteller'] = {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None}
    save_episode(sid, num, ep)
    return jsonify(ep)

@app.route('/api/series/<sid>/episodes/<int:num>', methods=['DELETE'])
def delete_episode(sid, num):
    f = episodes_dir(sid) / f'{num:03d}.json'
    if f.exists():
        f.unlink()
    return jsonify({'ok': True})

