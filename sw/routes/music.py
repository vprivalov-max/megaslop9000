"""Music generation routes: per-scene ElevenLabs tracks, polling, download.
Heavy lifting lives in services/ (music_builder, elevenlabs_music, postprocess)."""
import re
import shutil
import threading
import time
import traceback

from flask import jsonify, request, send_file

from sw.auth import _spawn_with_keys
from sw.config import ELEVENLABS_KEY
from sw.core import app
from sw.llm import anthropic_ask
from sw.locks import _episode_lock
from sw.scriptparse import is_scene_heading
from sw.seedance import _seedance_chunks
from sw.state import RENDER_SEMAPHORE
from sw.storage import load_episode, load_series, save_episode, series_path

# ════════════════════════════════════════════════════════════════════════════
# One instrumental track per scene (NOT per chunk). Length = sum of segment
# durations in that scene × 0.9 (trim 10% — average drop from final cut).
# Auto-fires after auto-assemble if series.settings.enable_music is truthy.
# Manual trigger via /music/generate for ad-hoc regen / disabled-by-default.
# State persisted on episode['music_scenes'] = [{sceneIdx, status, audio_path,
# duration_ms, composition_plan, user_hint, seed, error, generated_at}].

from services import elevenlabs_music as _el_music
from services import music_builder as _music_builder
from services import music_postprocess as _music_post

# ONE track per episode at a fixed length (1:10 = 70s). The legacy per-scene
# pipeline is kept intact behind this flag — flip to False to restore it.
MUSIC_SINGLE_TRACK = True
MUSIC_TRACK_DURATION_MS = 70_000        # 1:10
MUSIC_SINGLE_SCENE_IDX = 0              # the whole episode lives at sceneIdx=0

_MUSIC_LOCKS = {}
_MUSIC_LOCKS_GUARD = threading.Lock()

def _music_lock(sid, num):
    key = (sid, int(num))
    with _MUSIC_LOCKS_GUARD:
        lock = _MUSIC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _MUSIC_LOCKS[key] = lock
    return lock


def _music_scenes(ep):
    return ep.setdefault('music_scenes', [])


def _music_scene_record(ep, scene_idx):
    for r in _music_scenes(ep):
        if int(r.get('sceneIdx', -1)) == int(scene_idx):
            return r
    return None


def _safe_series_filename(title):
    """Series title → safe-for-filesystem suffix. Spaces → '_', strip exotic
    chars. Mirrors auto_assemble's safe_title but slightly more permissive on
    spaces (we want them as underscores, not collapsed)."""
    t = (title or 'Series').strip()
    # remove anything that's not letter/digit/space/dash/underscore
    t = re.sub(r'[^\w\s\-]+', '', t, flags=re.UNICODE)
    t = re.sub(r'\s+', '_', t)
    return (t[:80] or 'Series')


def _backfill_scene_meta_from_batch_prompts(ep):
    """Legacy chunks (pre-music release) lack sceneIdx/segIdx/durationSec on
    their record. Re-derive them from episode.batch_prompts when present:
    every anchor key in that dict carries sceneIdx/segIdx, and chunk.chunk_text
    starts with the anchor (anchor is the 60-char prefix of segment text).

    Mutates chunks in-place; returns count of chunks patched."""
    batch_prompts = ep.get('batch_prompts') or {}
    if not batch_prompts:
        return 0
    # Pre-compute anchor → meta lookup. Anchors are stored as 60-char prefixes
    # (see seedance_batch_compose), so matching is "chunk_text startswith anchor".
    anchor_meta = []
    for anchor, entry in batch_prompts.items():
        if not isinstance(entry, dict):
            continue
        si = entry.get('sceneIdx')
        gi = entry.get('segIdx')
        plan = entry.get('plan') or {}
        dur = plan.get('durationSec')
        if si is None:
            continue
        anchor_meta.append({
            'anchor': anchor.strip(),
            'sceneIdx': int(si),
            'segIdx': int(gi) if gi is not None else None,
            'durationSec': int(dur) if dur else None,
        })
    if not anchor_meta:
        return 0
    patched = 0
    for c in _seedance_chunks(ep):
        if c.get('sceneIdx') is not None:
            continue
        ct = (c.get('chunk_text') or '').strip()
        if not ct:
            continue
        match = None
        for m in anchor_meta:
            a = m['anchor']
            if a and (ct.startswith(a) or a in ct[:80]):
                match = m
                break
        if not match:
            continue
        c['sceneIdx'] = match['sceneIdx']
        if c.get('segIdx') is None and match['segIdx'] is not None:
            c['segIdx'] = match['segIdx']
        if c.get('durationSec') is None:
            c['durationSec'] = match['durationSec'] or int(c.get('duration') or 15)
        patched += 1
    return patched


def _group_chunks_by_scene(chunks):
    """Return ordered list of {sceneIdx, chunks, total_sec} for chunks that
    carry sceneIdx. Legacy chunks without sceneIdx are coalesced into a single
    synthetic scene (sceneIdx=0) — degraded mode lets the user still get a
    track for an old episode, just one long one instead of per-scene splits."""
    by_idx = {}
    orphans = []  # completed chunks without sceneIdx
    for c in chunks:
        if c.get('status') != 'completed':
            continue
        si = c.get('sceneIdx')
        if si is None:
            orphans.append(c)
            continue
        try:
            si = int(si)
        except Exception:
            orphans.append(c)
            continue
        by_idx.setdefault(si, []).append(c)
    out = []
    for si in sorted(by_idx.keys()):
        grp = by_idx[si]
        grp.sort(key=lambda c: (
            c.get('segIdx') if c.get('segIdx') is not None else 999,
            c.get('script_order') if c.get('script_order') is not None else 999,
            c.get('idx') or 0,
        ))
        total = sum(int(c.get('durationSec') or c.get('duration') or 0) for c in grp) or (len(grp) * 15)
        out.append({'sceneIdx': si, 'chunks': grp, 'total_sec': total})
    # Coalesce orphans into one synthetic scene appended at the end. Pick an
    # sceneIdx that does not collide with existing keys.
    if orphans and not out:
        orphans.sort(key=lambda c: (
            c.get('script_order') if c.get('script_order') is not None else 999,
            c.get('idx') or 0,
        ))
        total = sum(int(c.get('durationSec') or c.get('duration') or 15) for c in orphans)
        out.append({'sceneIdx': 0, 'chunks': orphans, 'total_sec': total})
    return out


def _split_script_by_scenes(script):
    """Split script text into a list of scene blocks. Uses is_scene_heading().
    Returns a list of strings (text of each scene including the heading).
    If no headings found, the whole script is one scene."""
    if not script:
        return ['']
    lines = script.splitlines()
    scenes = []
    cur = []
    for ln in lines:
        if is_scene_heading(ln) and cur:
            scenes.append('\n'.join(cur))
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        scenes.append('\n'.join(cur))
    return scenes or ['']


def _episode_music_dir(sid):
    p = series_path(sid) / 'MUSIC'
    p.mkdir(exist_ok=True)
    return p


def _scene_wav_path(sid, num, scene_idx):
    return _episode_music_dir(sid) / f'ep{int(num):03d}_sc{int(scene_idx)}.wav'


def _generate_scene_music_worker(sid, num, scene_idx, user_hint, force, attempts_max=3, target_duration_ms_override=None):
    """Pipeline for ONE scene. Runs in a daemon thread (spawn via _spawn_with_keys).
    Idempotent unless `force=True`: skip if status already 'completed'."""
    try:
        with _episode_lock(sid, num):
            ep = load_episode(sid, num)
            if not ep:
                return
            rec = _music_scene_record(ep, scene_idx)
            if rec and rec.get('status') == 'completed' and not force:
                return
            if rec is None:
                rec = {'sceneIdx': int(scene_idx)}
                _music_scenes(ep).append(rec)
            rec.update({
                'status': 'generating',
                'user_hint': user_hint or rec.get('user_hint', ''),
                'error': None,
                'started_at': int(time.time()),
            })
            save_episode(sid, num, ep)

        if not ELEVENLABS_KEY:
            raise RuntimeError('ELEVENLABS_KEY_MISSING: задай ELEVENLABS_API_KEY (env) или elevenlabs_key (config.json)')

        # Snapshot inputs.
        with _episode_lock(sid, num):
            ep = load_episode(sid, num)
            s = load_series(sid)
        if not ep or not s:
            raise RuntimeError('series or episode disappeared during music gen')

        if MUSIC_SINGLE_TRACK:
            # ONE track for the whole episode, fixed length. No per-scene sizing,
            # no early-fire duration override — the length is constant.
            target_ms = MUSIC_TRACK_DURATION_MS
            all_chunks = [c for c in _seedance_chunks(ep) if c.get('status') == 'completed']
            all_chunks.sort(key=lambda c: (
                c.get('script_order') if c.get('script_order') is not None else 999,
                c.get('idx') or 0,
            ))
            chunk_texts = [c.get('chunk_text') or '' for c in all_chunks]
            # Whole script is the context; no neighbouring-scene snippets.
            scene_text = ep.get('script') or ''
            prev_tail = ''
            next_head = ''
        else:
            groups = _group_chunks_by_scene(_seedance_chunks(ep))
            grp = next((g for g in groups if g['sceneIdx'] == int(scene_idx)), None)

            if grp:
                target_sec = max(5.0, grp['total_sec'] * 0.9)
                target_ms = int(round(target_sec * 1000))
                chunk_texts = [c.get('chunk_text') or '' for c in grp['chunks']]
            elif target_duration_ms_override:
                # Early-fire path: music kicked before video generation starts.
                # Duration comes from the script-based duration estimate on the client.
                target_ms = int(target_duration_ms_override)
                chunk_texts = []  # no video chunks yet; script context is still used below
            else:
                raise RuntimeError(f'no completed chunks for sceneIdx={scene_idx}')

            # Build context.
            scene_blocks = _split_script_by_scenes(ep.get('script') or '')
            scene_text = scene_blocks[int(scene_idx)] if 0 <= int(scene_idx) < len(scene_blocks) else (ep.get('script') or '')
            prev_tail = scene_blocks[int(scene_idx) - 1][-400:] if int(scene_idx) - 1 >= 0 and int(scene_idx) - 1 < len(scene_blocks) else ''
            next_head = scene_blocks[int(scene_idx) + 1][:400] if int(scene_idx) + 1 < len(scene_blocks) else ''

        # ElevenLabs hard bound: sections in [8s, 18s] each, 4..6 sections → 32s..108s
        target_ms = max(32_000, min(target_ms, 110_000))

        episode_blocking = (ep.get('batch_episode_blocking') or ep.get('scene_blocking') or '')

        ffmpeg_bin = shutil.which('ffmpeg')
        if not ffmpeg_bin:
            raise RuntimeError('ffmpeg не установлен. brew install ffmpeg')

        out_path = _scene_wav_path(sid, num, scene_idx)
        composition_plan = None
        last_error = None
        success_info = None

        for attempt in range(attempts_max):
            seed = int(time.time() * 1000) % 1_000_000 + attempt * 7919
            try:
                # Build plan once on attempt 0, reuse on retries (we vary seed only).
                if composition_plan is None:
                    composition_plan = _music_builder.call_claude_for_plan(
                        series=s,
                        episode_number=int(num),
                        scene_idx=int(scene_idx),
                        scene_script_text=scene_text,
                        chunk_texts=chunk_texts,
                        prev_scene_tail=prev_tail,
                        next_scene_head=next_head,
                        target_duration_ms=target_ms,
                        user_hint=user_hint or '',
                        episode_blocking=episode_blocking,
                        claude_fn=anthropic_ask,
                    )

                raw_mp3 = out_path.with_suffix('.raw.mp3')
                raw_wav = out_path.with_suffix('.raw.wav')
                trimmed = out_path.with_suffix('.trim.wav')

                _el_music.generate_music(
                    api_key=ELEVENLABS_KEY,
                    composition_plan=composition_plan,
                    out_path=raw_mp3,
                    seed=seed,
                )

                with RENDER_SEMAPHORE:
                    _music_post.mp3_to_wav(ffmpeg_bin, raw_mp3, raw_wav)
                    _music_post.silence_trim(ffmpeg_bin, raw_wav, trimmed)
                    # Use the CLAMPED target (≤110s) — ElevenLabs physically cannot
                    # exceed ~110s regardless of what we ask. Comparing against the
                    # raw scene duration (e.g. 189s from 13 chunks) would always fail.
                    activity = _music_post.analyze_activity(ffmpeg_bin, trimmed, target_ms / 1000.0)
                    if not activity['ok']:
                        last_error = f'attempt {attempt+1} failed quality check: {activity["reason"]}'
                        print(f'[music] {sid}/ep{num}/sc{scene_idx} {last_error}', flush=True)
                        for p in (raw_mp3, raw_wav, trimmed):
                            try: p.unlink(missing_ok=True)
                            except Exception: pass
                        continue
                    _music_post.loudnorm(ffmpeg_bin, trimmed, out_path)
                    for p in (raw_mp3, raw_wav, trimmed):
                        try: p.unlink(missing_ok=True)
                        except Exception: pass

                success_info = {
                    'seed': seed,
                    'attempts_used': attempt + 1,
                    'duration_ms_target': target_ms,
                    'composition_plan': composition_plan,
                }
                break
            except Exception as e:
                last_error = f'attempt {attempt+1}: {type(e).__name__}: {e}'
                print(f'[music] {sid}/ep{num}/sc{scene_idx} {last_error}', flush=True)
                continue

        with _episode_lock(sid, num):
            ep = load_episode(sid, num)
            rec = _music_scene_record(ep, scene_idx)
            if rec is None:
                rec = {'sceneIdx': int(scene_idx)}
                _music_scenes(ep).append(rec)
            if success_info:
                rec.update({
                    'status': 'completed',
                    'audio_path': str(out_path.relative_to(series_path(sid))),
                    'duration_ms': success_info['duration_ms_target'],
                    'composition_plan': success_info['composition_plan'],
                    'seed': success_info['seed'],
                    'attempts_used': success_info['attempts_used'],
                    'user_hint': user_hint or rec.get('user_hint', ''),
                    'error': None,
                    'generated_at': int(time.time()),
                })
            else:
                rec.update({
                    'status': 'failed',
                    'error': last_error or 'unknown error',
                    'generated_at': int(time.time()),
                })
            save_episode(sid, num, ep)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            with _episode_lock(sid, num):
                ep = load_episode(sid, num)
                if ep is not None:
                    rec = _music_scene_record(ep, scene_idx)
                    if rec is None:
                        rec = {'sceneIdx': int(scene_idx)}
                        _music_scenes(ep).append(rec)
                    rec.update({'status': 'failed', 'error': f'{type(e).__name__}: {e}'})
                    save_episode(sid, num, ep)
        except Exception:
            pass


def _kick_music_generation(sid, num, scene_indices, user_hint, force, scenes_plan=None):
    """Spawn one worker per scene index. Workers are independent (ElevenLabs
    handles concurrency fine for low-N parallelism, and Claude has its own
    retry/backoff). Returns the list of (sceneIdx, status_after_spawn) for
    immediate response to the client.

    scenes_plan: optional dict {sceneIdx (int) → target_duration_ms (int)}.
    When provided, passed to the worker so it can generate music even before
    video chunks exist (early-fire mode — music starts while videos are rendering).
    """
    scenes_plan = scenes_plan or {}
    started = []
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return []
        for si in scene_indices:
            rec = _music_scene_record(ep, si)
            if rec and rec.get('status') == 'completed' and not force:
                started.append({'sceneIdx': si, 'status': 'skipped_completed'})
                continue
            if rec is None:
                rec = {'sceneIdx': int(si), 'status': 'pending'}
                _music_scenes(ep).append(rec)
            else:
                rec['status'] = 'pending'
            started.append({'sceneIdx': si, 'status': 'pending'})
        save_episode(sid, num, ep)

    for entry in started:
        if entry['status'] == 'pending':
            si = int(entry['sceneIdx'])
            override_ms = scenes_plan.get(si)
            _spawn_with_keys(
                _generate_scene_music_worker,
                sid, int(num), si,
                user_hint or '', bool(force),
                target_duration_ms_override=override_ms,
            )
    return started


@app.route('/api/series/<sid>/episodes/<int:num>/music/generate', methods=['POST'])
def music_generate(sid, num):
    """Generate music for all scenes (or a subset). Idempotent — completed
    scenes are skipped unless `force=true` in body. Body:
      { scene_indices?: [int], user_hint?: str, force?: bool }
    """
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    user_hint = (body.get('user_hint') or '').strip()
    force = bool(body.get('force'))
    requested = body.get('scene_indices')

    # Single-track mode: one episode-level track at sceneIdx=0, fixed duration.
    # scene_indices / scenes_plan from the client are intentionally ignored.
    if MUSIC_SINGLE_TRACK:
        started = _kick_music_generation(sid, num, [MUSIC_SINGLE_SCENE_IDX], user_hint, force)
        return jsonify({'ok': True, 'scenes': started, 'total_scenes': 1})

    # Backfill scene metadata on legacy chunks (pre-music release) from
    # batch_prompts. Persists the patched chunks so subsequent polls see it.
    with _episode_lock(sid, num):
        ep_fresh = load_episode(sid, num) or ep
        if _backfill_scene_meta_from_batch_prompts(ep_fresh):
            save_episode(sid, num, ep_fresh)
            ep = ep_fresh

    # Optional early-fire plan: [{sceneIdx, target_duration_ms}] — sent by the
    # frontend right after the script is segmented, before any video chunk exists.
    scenes_plan_raw = body.get('scenes_plan') or []
    scenes_plan = {}
    for sp in scenes_plan_raw:
        try:
            scenes_plan[int(sp['sceneIdx'])] = int(sp['target_duration_ms'])
        except (KeyError, TypeError, ValueError):
            pass

    groups = _group_chunks_by_scene(_seedance_chunks(ep))
    if groups:
        all_indices = [g['sceneIdx'] for g in groups]
    elif scenes_plan:
        # Early-fire: no video chunks yet, but client sent a duration plan.
        all_indices = sorted(scenes_plan.keys())
    else:
        return jsonify({
            'error': 'нет готовых чанков со сценами для генерации музыки. '
                     'Запусти сначала видео-генерацию (она проставит sceneIdx).',
        }), 400

    if isinstance(requested, list) and requested:
        try:
            scene_indices = [int(x) for x in requested if int(x) in all_indices]
        except (TypeError, ValueError):
            return jsonify({'error': 'scene_indices must be ints'}), 400
    else:
        scene_indices = all_indices

    started = _kick_music_generation(sid, num, scene_indices, user_hint, force, scenes_plan=scenes_plan)
    return jsonify({'ok': True, 'scenes': started, 'total_scenes': len(all_indices)})


@app.route('/api/series/<sid>/episodes/<int:num>/music/regenerate', methods=['POST'])
def music_regenerate(sid, num):
    """Force-regenerate ONE scene with an optional user_hint. Body:
      { sceneIdx: int, user_hint?: str }
    """
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    if MUSIC_SINGLE_TRACK:
        scene_idx = MUSIC_SINGLE_SCENE_IDX
    else:
        try:
            scene_idx = int(body.get('sceneIdx'))
        except (TypeError, ValueError):
            return jsonify({'error': 'sceneIdx required'}), 400
    user_hint = (body.get('user_hint') or '').strip()
    started = _kick_music_generation(sid, num, [scene_idx], user_hint, force=True)
    return jsonify({'ok': True, 'scenes': started})


_MUSIC_STALE_TIMEOUT_S = 15 * 60  # 15 min — worker likely crashed


@app.route('/api/series/<sid>/episodes/<int:num>/music/poll', methods=['GET'])
def music_poll(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404

    # Backfill scene metadata if missing so the UI sees legacy episodes too.
    if _backfill_scene_meta_from_batch_prompts(ep):
        with _episode_lock(sid, num):
            ep_fresh = load_episode(sid, num) or ep
            if _backfill_scene_meta_from_batch_prompts(ep_fresh):
                save_episode(sid, num, ep_fresh)
                ep = ep_fresh

    now = int(time.time())

    # 1) Reset stale 'generating' records (worker crashed).
    stale = [r for r in _music_scenes(ep)
             if r.get('status') == 'generating'
             and now - int(r.get('started_at') or 0) > _MUSIC_STALE_TIMEOUT_S]
    if stale:
        with _episode_lock(sid, num):
            ep2 = load_episode(sid, num) or ep
            changed = False
            for r in _music_scenes(ep2):
                if (r.get('status') == 'generating'
                        and now - int(r.get('started_at') or 0) > _MUSIC_STALE_TIMEOUT_S):
                    r['status'] = 'failed'
                    r['error'] = 'timeout — worker crashed, click ↻ to retry'
                    changed = True
            if changed:
                save_episode(sid, num, ep2)
                ep = ep2

    groups = _group_chunks_by_scene(_seedance_chunks(ep))

    # 2) Auto-kick: completed chunks exist but no music record yet → missed trigger.
    if MUSIC_SINGLE_TRACK:
        has_completed = any(c.get('status') == 'completed' for c in _seedance_chunks(ep))
        if (has_completed and not _music_scenes(ep)
                and (ep.get('script') or '').strip() and ELEVENLABS_KEY):
            _kick_music_generation(sid, num, [MUSIC_SINGLE_SCENE_IDX], '', force=False)
            with _episode_lock(sid, num):
                ep = load_episode(sid, num) or ep
    elif (groups and not _music_scenes(ep)
            and (ep.get('script') or '').strip() and ELEVENLABS_KEY):
        _kick_music_generation(sid, num, [g['sceneIdx'] for g in groups], '', force=False)
        with _episode_lock(sid, num):
            ep = load_episode(sid, num) or ep

    # 3) Re-spawn workers for 'pending' scenes that have no active worker
    #    (server restarted after a manual reset, or early-fire wrote pending but crashed).
    pending = [r for r in _music_scenes(ep) if r.get('status') == 'pending']
    if pending and ELEVENLABS_KEY:
        _kick_music_generation(sid, num, [int(r['sceneIdx']) for r in pending], '', force=False)
        with _episode_lock(sid, num):
            ep = load_episode(sid, num) or ep

    if MUSIC_SINGLE_TRACK:
        n_chunks = sum(1 for c in _seedance_chunks(ep) if c.get('status') == 'completed')
        # One synthetic episode-level track; expose it once music exists or any
        # chunk is ready, so the UI shows the single 1/1 track row.
        if _music_scenes(ep) or n_chunks:
            scenes_meta = [{'sceneIdx': MUSIC_SINGLE_SCENE_IDX,
                            'total_sec': MUSIC_TRACK_DURATION_MS / 1000.0,
                            'chunks': n_chunks}]
        else:
            scenes_meta = []
    else:
        scenes_meta = [{'sceneIdx': g['sceneIdx'], 'total_sec': g['total_sec'],
                        'chunks': len(g['chunks'])} for g in groups]
    return jsonify({
        'music_scenes': _music_scenes(ep),
        'available_scenes': scenes_meta,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/music/download', methods=['GET'])
def music_download(sid, num):
    """Concat all scene WAVs (in sceneIdx order) into one file and stream it.
    Filename: MUS_<Safe_Series_Title>_<num>.wav. If some scenes are missing
    audio, they're replaced with silence of expected length so the resulting
    file still maps to the assembled video timeline."""
    from flask import send_file
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    base = series_path(sid)
    safe = _safe_series_filename(s.get('title') or s.get('name') or 'Series')
    out_name = f'MUS_{safe}_{int(num)}.wav'

    # Single-track mode: one file, stream it directly (no concat needed).
    if MUSIC_SINGLE_TRACK:
        rec = _music_scene_record(ep, MUSIC_SINGLE_SCENE_IDX)
        if rec and rec.get('status') == 'completed' and rec.get('audio_path'):
            p = base / rec['audio_path']
            if p.exists():
                return send_file(str(p), mimetype='audio/wav',
                                 as_attachment=True, download_name=out_name)
        return jsonify({'error': 'музыка ещё не готова'}), 404

    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return jsonify({'error': 'ffmpeg не установлен'}), 500

    groups = _group_chunks_by_scene(_seedance_chunks(ep))
    if not groups:
        return jsonify({'error': 'нет сцен — нечего собирать'}), 400

    base = series_path(sid)
    parts = []
    tmp_silences = []
    for g in groups:
        rec = _music_scene_record(ep, g['sceneIdx'])
        if rec and rec.get('status') == 'completed' and rec.get('audio_path'):
            p = base / rec['audio_path']
            if p.exists():
                parts.append(p)
                continue
        # Filler silence for missing scenes.
        sil = base / 'MUSIC' / f'_silence_ep{int(num):03d}_sc{g["sceneIdx"]}.wav'
        sil.parent.mkdir(exist_ok=True)
        _music_post.make_silence_wav(ffmpeg_bin, sil, g['total_sec'] * 0.9)
        parts.append(sil)
        tmp_silences.append(sil)

    safe = _safe_series_filename(s.get('title') or s.get('name') or 'Series')
    out_name = f'MUS_{safe}_{int(num)}.wav'
    out_dir = base / 'OUT'
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / out_name
    try:
        with RENDER_SEMAPHORE:
            _music_post.concat_wavs(ffmpeg_bin, parts, out_path)
    except Exception as e:
        return jsonify({'error': f'concat failed: {e}'}), 500
    finally:
        for s_ in tmp_silences:
            try: s_.unlink(missing_ok=True)
            except Exception: pass

    return send_file(
        str(out_path),
        mimetype='audio/wav',
        as_attachment=True,
        download_name=out_name,
    )


