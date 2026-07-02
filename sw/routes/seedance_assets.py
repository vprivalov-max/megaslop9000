"""Seedance asset routes: ref upload, chunk list, auto-assemble (ffmpeg),
download-zip, scene-blocking generation."""
import base64
import io
import re
import shutil
import subprocess
import time
import zipfile

import requests
from flask import Response, jsonify, request

from sw.auth import _get_user_avai_key
from sw.core import app
from sw.llm import claude_ask
from sw.locks import _episode_lock
from sw.seedance import _seedance_chunks
from sw.state import RENDER_SEMAPHORE
from sw.storage import load_episode, load_series, save_episode, series_path
from sw.textrules_banlists import (_build_episode_tag_mapping,
                                   _prev_episode_ending_context)
from sw.utils import slugify

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/upload-ref', methods=['POST'])
def seedance_upload_ref(sid, num):
    """Upload an arbitrary image (file from desktop or URL) to AVAI storage,
    return public URL the user can drop into Seedance refs."""
    f = request.files.get('file')
    if f:
        import base64
        data = f.read()
        mime = f.mimetype or 'image/png'
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            mime = 'image/png'
        try:
            headers = {'x-api-key': _get_user_avai_key(), 'content-type': 'application/json'}
            resp = requests.post(
                'https://avai-gen.com/api/public/upload-image',
                json={'image_base64': base64.b64encode(data).decode('ascii'),
                      'mime_type': mime},
                headers=headers, timeout=120,
            )
            if not resp.ok:
                return jsonify({'error': f'AVAI upload {resp.status_code}: {resp.text[:200]}'}), 500
            d = resp.json()
            url = (
                d.get('url') or d.get('image_url')
                or (d.get('data') or {}).get('url')
                or ((d.get('images') or [{}])[0] or {}).get('url')
            )
            if not url:
                return jsonify({'error': f'no url in response: {str(d)[:200]}'}), 500
            return jsonify({'url': url, 'name': f.filename or 'custom'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    body = request.json or {}
    src_url = (body.get('url') or '').strip()
    if not src_url:
        return jsonify({'error': 'no file or url'}), 400
    # Download then re-upload (so AVAI hosts it; some Seedance refs need their CDN)
    try:
        r = requests.get(src_url, timeout=60)
        r.raise_for_status()
        import base64
        mime = r.headers.get('content-type', 'image/png').split(';')[0]
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            mime = 'image/png'
        headers = {'x-api-key': _get_user_avai_key(), 'content-type': 'application/json'}
        resp = requests.post(
            'https://avai-gen.com/api/public/upload-image',
            json={'image_base64': base64.b64encode(r.content).decode('ascii'),
                  'mime_type': mime},
            headers=headers, timeout=120,
        )
        if not resp.ok:
            return jsonify({'error': f'AVAI upload {resp.status_code}: {resp.text[:200]}'}), 500
        d = resp.json()
        url = d.get('url') or d.get('image_url') or (d.get('data') or {}).get('url')
        return jsonify({'url': url, 'name': src_url.split('/')[-1][:40]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/list')
def seedance_list(sid, num):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    chunks = _seedance_chunks(ep)
    # One-shot cleanup: wipe stale `error` strings that were left over from a
    # transient failure (AVAI 401 etc.) on chunks that ultimately rendered
    # successfully. Without this old chunks keep showing red "401: Unauthorized"
    # under the video preview forever even though they're completed.
    healed = False
    for c in chunks:
        if c.get('status') == 'completed' and c.get('video_path') and c.get('error'):
            c.pop('error', None)
            healed = True
    if healed:
        with _episode_lock(sid, num):
            ep2 = load_episode(sid, num) or ep
            for c in _seedance_chunks(ep2):
                if c.get('status') == 'completed' and c.get('video_path') and c.get('error'):
                    c.pop('error', None)
            save_episode(sid, num, ep2)
            chunks = _seedance_chunks(ep2)
    return jsonify({'chunks': chunks})


@app.route('/api/series/<sid>/episodes/<int:num>/auto-assemble', methods=['POST'])
def auto_assemble_episode(sid, num):
    """Stitch all completed seedance chunks of ONE episode into a single mp4.

    Used by the range-generation queue when `auto_assemble` is on:
    once Auto-mode finishes for an episode, the frontend hits this endpoint
    to produce a downloadable final cut without manual timeline work.

    Logic:
      - Collect chunks where status='completed' and video_path exists.
      - Order by `script_order` (set on /seedance/start) — falls back to idx.
      - If query/body `require_all=true` (default), refuse when any segment
        from the episode's expected scene-segment list is missing — frontend
        passes `expected_segments` count to gate.
      - Concat-copy via ffmpeg (no re-encode), output to OUT/<title>_E<num>.mp4.
      - Returns {ok, path, url, size_mb, chunks}.
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'episode not found'}), 404

    body = request.json or {}
    require_all = body.get('require_all', True)
    expected_segments = body.get('expected_segments')   # optional, set by frontend from script
    # Auto-insert facade clip at each scene boundary. Defaults ON so newly
    # assembled episodes naturally open every venue with its building shot.
    # User can pass false to get the pure chunk concat (legacy behaviour).
    insert_facades = body.get('insert_facades', True)

    all_chunks = [c for c in (_seedance_chunks(ep) or [])
                  if c.get('status') == 'completed' and c.get('video_path')]
    if not all_chunks:
        return jsonify({'error': 'нет готовых чанков для сборки'}), 400

    # Dedup: when the user retried/healed a chunk that already had a video,
    # we get multiple completed chunks with the SAME script_order. Pick the
    # NEWEST per position (highest idx, since idx is monotonically increasing
    # per /seedance/start; created_at as tiebreak for chunks that share idx
    # across legacy data). Previously we kept the FIRST (oldest), which meant
    # auto-assemble ignored user's manual reruns.
    #
    # Chunks lacking script_order go after the indexed ones, in their own
    # order by idx (legacy behavior — preserved so old data still assembles).
    by_order = {}
    no_order = []
    for c in all_chunks:
        so = c.get('script_order')
        if isinstance(so, int):
            prev = by_order.get(so)
            if prev is None:
                by_order[so] = c
            else:
                # Prefer the one with bigger idx; tie-break with created_at.
                cur_key = (c.get('idx') or 0, c.get('created_at') or 0)
                prev_key = (prev.get('idx') or 0, prev.get('created_at') or 0)
                if cur_key > prev_key:
                    by_order[so] = c
        else:
            no_order.append(c)
    # Legacy-retry rescue: chunks created via the old sdRetry flow (before
    # script_order propagation) land here with so=None but their chunk_text
    # matches an existing by_order chunk verbatim. Treat them as retries of
    # that script slot so the dedup picks the NEWER take and we don't double-
    # play the segment. Without this rescue, the auto-assemble would stitch
    # [orig1, orig2, orig3, retry1, retry2] producing the user-reported
    # "clothing changes every shot" mess in «My Roommate From Craigslist…».
    chunk_text_to_so = {
        (by_order[k].get('chunk_text') or '').strip(): k
        for k in by_order
        if (by_order[k].get('chunk_text') or '').strip()
    }
    truly_orphan = []
    for c in no_order:
        ct = (c.get('chunk_text') or '').strip()
        so = chunk_text_to_so.get(ct) if ct else None
        if so is not None:
            prev = by_order[so]
            cur_key = (c.get('idx') or 0, c.get('created_at') or 0)
            prev_key = (prev.get('idx') or 0, prev.get('created_at') or 0)
            if cur_key > prev_key:
                by_order[so] = c
        else:
            truly_orphan.append(c)
    # Interleave orphans (chunks lacking script_order AND not matched to any
    # existing slot by chunk_text) BY idx instead of dumping them at the end.
    # idx is monotonically increasing per /seedance/start, so it preserves
    # the user's creation order. We insert each orphan AFTER the latest
    # ordered chunk whose idx is below the orphan's idx — that keeps
    # manually-regenerated chunks visually near their original siblings
    # instead of appearing at the tail of the final cut.
    ordered_list = [by_order[k] for k in sorted(by_order.keys())]
    truly_orphan_sorted = sorted(truly_orphan, key=lambda c: c.get('idx') or 0)

    # Orphan placement — scene-heading-based grouping.
    # Real prod bug 2026-05-25 «My Stepmother» ep 54: orphan chunks (chunks
    # without script_order — typically manual recomposes) were placed by
    # raw idx, which scattered them away from their scene siblings. An
    # orphan from scene 1 with idx=6 ended up after scene 5's chunk because
    # idx=6 > idx of all other chunks of scene 1.
    #
    # Fix: extract the first meaningful line (the slug «INT. CAR — MORNING»
    # or «LOCATION: ...») as a scene-key, then insert each orphan immediately
    # after the LAST ordered chunk sharing the same scene-key. Falls back to
    # idx-based insertion for orphans whose scene heading doesn't match any
    # ordered chunk (truly novel scenes).
    def _scene_key(text):
        for line in (text or '').split('\n'):
            line = line.strip()
            if line:
                # Normalize whitespace + uppercase so minor differences don't
                # break the match («INT. Car — Morning» == «INT.  CAR — MORNING»).
                return ' '.join(line.upper().split())
        return ''
    chunks = list(ordered_list)
    # Map scene-key → last position of that scene in the current chunks list
    def _rebuild_heading_index(lst):
        idx_map = {}
        for i, c in enumerate(lst):
            k = _scene_key(c.get('chunk_text'))
            if k:
                idx_map[k] = i
        return idx_map
    heading_last_pos = _rebuild_heading_index(chunks)
    fallback_orphans = []
    for orph in truly_orphan_sorted:
        k = _scene_key(orph.get('chunk_text'))
        pos = heading_last_pos.get(k) if k else None
        if pos is not None:
            # Insert right after the last chunk of this scene
            chunks.insert(pos + 1, orph)
            heading_last_pos = _rebuild_heading_index(chunks)
        else:
            fallback_orphans.append(orph)
    # Truly novel scenes (no heading match) — fall back to old idx-based
    # interleaving to keep them in creation order.
    for orph in fallback_orphans:
        oi = orph.get('idx') or 0
        insert_at = len(chunks)
        for j, ch in enumerate(chunks):
            ci = ch.get('idx') or 0
            if ci > oi:
                insert_at = j
                break
        chunks.insert(insert_at, orph)

    if require_all and isinstance(expected_segments, int) and expected_segments > 0:
        if len(chunks) < expected_segments:
            return jsonify({
                'error': 'не все сегменты готовы',
                'have': len(chunks),
                'expected': expected_segments,
            }), 409

    base = series_path(sid)

    # ── Facade auto-insert ────────────────────────────────────────────────
    # Build loc_id → facade record map. A facade «owns» the locations listed
    # in its member_loc_ids[]. If a chunk's primary loc transitions to one
    # owned by a different facade than the previous chunk used, we prepend
    # the new facade's video clip to introduce the venue. The very first
    # chunk also triggers an insert (scene opens from nothing).
    facade_by_loc = {}
    facades_inserted = 0
    if insert_facades:
        for f in (s.get('location_facades') or []):
            if (f.get('status') in ('ready',)) and f.get('video_path'):
                for lid in (f.get('member_loc_ids') or []):
                    facade_by_loc[lid] = f
    def _chunk_primary_loc(c):
        for r in (c.get('refs') or []):
            if r.get('kind') == 'loc' and r.get('id'):
                return r['id']
        return None

    seg_paths = []
    prev_facade_id = None   # which facade we last opened with (None at start)
    for c in chunks:
        p = base / c['video_path']
        if not p.exists():
            return jsonify({'error': f"file missing: {c['video_path']}"}), 400
        # Decide whether to drop in a facade BEFORE this chunk
        if insert_facades:
            loc_id = _chunk_primary_loc(c)
            fac = facade_by_loc.get(loc_id) if loc_id else None
            if fac and fac.get('id') != prev_facade_id:
                fac_video = base / fac['video_path']
                if fac_video.exists():
                    seg_paths.append(str(fac_video))
                    facades_inserted += 1
                prev_facade_id = fac.get('id')
            elif fac:
                # Same facade as last chunk — same scene continues, no insert.
                pass
            else:
                # Chunk's loc has no facade → don't reset prev_facade_id; treat
                # as continuation of the previous scene visually. (Exterior
                # locations typically don't need a facade intro since the
                # location image itself shows the surroundings.)
                pass
        seg_paths.append(str(p))

    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return jsonify({'error': 'ffmpeg не установлен. brew install ffmpeg'}), 500
    ffprobe_bin = shutil.which('ffprobe')   # paired with ffmpeg; both come from the same package

    # ── Per-input probe: audio presence + duration ────────────────────────
    # Facade clips are rendered without audio (generate_audio=False), regular
    # Seedance chunks have audio. Mixing them with bare concat-copy or with
    # an unconditional filter-complex `[i:a]` map produces a broken/silent
    # file. So: probe each segment, and during filter-complex synthesize a
    # matching-length silent track for any input that lacks one.
    def _probe_audio_and_duration(path):
        if not ffprobe_bin:
            return True, 5.0     # safest defaults — assume has audio, ~chunk length
        has_audio = True
        try:
            out = subprocess.check_output(
                [ffprobe_bin, '-v', 'error', '-show_entries', 'stream=codec_type',
                 '-of', 'csv=p=0', path],
                text=True, timeout=10,
            )
            has_audio = ('audio' in out)
        except Exception:
            pass
        dur = 5.0
        try:
            out = subprocess.check_output(
                [ffprobe_bin, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', path],
                text=True, timeout=10,
            )
            dur = float((out or '').strip() or 5.0)
        except Exception:
            pass
        return has_audio, dur

    seg_meta = [_probe_audio_and_duration(p) for p in seg_paths]
    audio_uniform = all(ha for ha, _ in seg_meta)
    # Concat-copy (-c copy via concat demuxer) is disabled. Seedance chunks
    # have non-aligned B-frame pyramids + non-zero PTS offsets, so bitstream
    # append showed 2-4 reordered buffer frames across each seam (looked like
    # last/first frame flickering back and forth several times). Filter-complex
    # re-encode with per-segment setpts=PTS-STARTPTS rewrites the timeline
    # cleanly. Costs ~30-90s CPU per episode — worth it for clean cuts.
    can_try_copy = False

    out_dir = base / 'OUT'
    out_dir.mkdir(exist_ok=True)
    safe_title = (ep.get('title') or f'E{num}').strip()
    safe_title = re.sub(r'[^\w\-]+', '_', safe_title)[:60] or f'E{num}'
    out_name = f"{safe_title}_E{num:03d}.mp4"
    out_path = out_dir / out_name

    list_file = out_dir / f'_concat_{int(time.time())}_{num}.txt'
    list_file.write_text(
        '\n'.join(f"file '{p}'" for p in seg_paths),
        encoding='utf-8',
    )
    cmd_copy = [
        ffmpeg_bin, '-y', '-f', 'concat', '-safe', '0',
        '-i', str(list_file), '-c', 'copy', str(out_path),
    ]
    queue_wait = time.time()
    with RENDER_SEMAPHORE:
        if time.time() - queue_wait > 0.5:
            print(f'[auto-assemble] {sid}/ep{num} waited {time.time()-queue_wait:.1f}s in queue')
        proc = None
        if can_try_copy:
            try:
                proc = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                try: list_file.unlink(missing_ok=True)
                except Exception: pass
                return jsonify({'error': 'ffmpeg timeout (>10 min)'}), 500

        # Fallback to filter-complex re-encode when (a) we skipped concat-copy
        # because audio coverage was non-uniform, or (b) concat-copy failed
        # due to codec drift. Normalize ALL inputs to the same resolution so
        # concat doesn't fail on dimension mismatches (facades are often a
        # different size than Seedance chunks). We probe the first "real" chunk
        # (non-facade, i.e. the last seg_paths entry that comes from a chunk
        # record) to get the canonical W×H, then scale everything to that.
        if (not can_try_copy) or (proc and proc.returncode != 0):
            if can_try_copy:
                print(f'[auto-assemble] {sid}/ep{num} concat-copy failed, retry filter-complex')
            else:
                print(f'[auto-assemble] {sid}/ep{num} skipping concat-copy: '
                      f'facades_inserted={facades_inserted} audio_uniform={audio_uniform}')

            # Probe target resolution from the first non-facade segment.
            target_w, target_h = 576, 1024  # sensible default for 9:16
            if ffprobe_bin:
                chunk_paths = [str(base / c['video_path']) for c in chunks
                               if (base / c['video_path']).exists()]
                for cp in chunk_paths[:3]:   # try first few, stop at first success
                    try:
                        dim_out = subprocess.check_output(
                            [ffprobe_bin, '-v', 'error',
                             '-show_entries', 'stream=width,height',
                             '-of', 'csv=p=0:s=x', cp],
                            text=True, timeout=10,
                        ).strip()
                        if dim_out:
                            tw, th = (int(x) for x in dim_out.split('x'))
                            if tw > 0 and th > 0:
                                # Round to even dimensions (libx264 requirement)
                                target_w = tw if tw % 2 == 0 else tw - 1
                                target_h = th if th % 2 == 0 else th - 1
                                break
                    except Exception:
                        pass
            print(f'[auto-assemble] {sid}/ep{num} target resolution: {target_w}x{target_h}')

            inputs = []
            filt = []
            n = len(seg_paths)
            # Seam-flicker root cause (after empirical testing): libx264 with
            # default B-frame settings reorders frames around seam boundaries
            # during re-encode. Combined with `aresample=async=1` stretching
            # audio, the concat filter pads video with held frames → the visible
            # "last frame + first frame alternating" flash at every seam.
            #
            # Fix (no content trimming, audio preserved bit-for-bit):
            #   1. `-bf 0` disables B-frames → no reorder possible.
            #   2. `-force_key_frames` at every seam timestamp → encoder treats
            #      each segment as an independent GOP; no inter-seam references.
            #   3. Drop `async=1` from aresample → no audio stretch, no concat
            #      video padding to compensate.
            #   4. `-fps_mode cfr` → strict constant frame rate, ffmpeg will
            #      not duplicate frames at any point.
            FPS = 24
            for i, p in enumerate(seg_paths):
                inputs += ['-i', p]
                has_audio, dur = seg_meta[i]
                # Scale to target resolution with padding to avoid AR distortion.
                # force_original_aspect_ratio=decrease → fit within box,
                # pad → letterbox/pillarbox to fill exact target dims.
                filt.append(
                    f"[{i}:v]setpts=PTS-STARTPTS,"
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps={FPS}[v{i}]"
                )
                if has_audio:
                    # No `async=1` — that allowed ffmpeg to stretch/pad audio,
                    # which made the concat filter pad video with held frames
                    # at seams. Plain resample to 48k stereo, preserve original
                    # timing exactly.
                    filt.append(
                        f"[{i}:a]aresample=48000,"
                        f"aformat=channel_layouts=stereo:sample_rates=48000,"
                        f"asetpts=PTS-STARTPTS[a{i}]"
                    )
                else:
                    # Synthesize silent stereo of the clip's exact length so
                    # video/audio timelines stay aligned across the concat.
                    filt.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                        f"atrim=0:{max(0.1, dur):.3f},asetpts=PTS-STARTPTS[a{i}]"
                    )
            cat = ''.join(f"[v{i}][a{i}]" for i in range(n))
            filt.append(f"{cat}concat=n={n}:v=1:a=1[v][a]")

            # Cumulative seam timestamps in the OUTPUT timeline. We pass these
            # to `-force_key_frames` so libx264 starts a fresh IDR exactly at
            # each segment boundary — no cross-seam motion estimation, no
            # reorder artifacts.
            seam_times = []
            acc = 0.0
            for i in range(n):
                if i > 0:
                    seam_times.append(acc)
                acc += seg_meta[i][1] or 0.0
            kf_arg = ','.join(f'{t:.3f}' for t in seam_times) if seam_times else '0'

            cmd_re = [
                ffmpeg_bin, '-y', *inputs,
                '-filter_complex', ';'.join(filt),
                '-map', '[v]', '-map', '[a]',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-pix_fmt', 'yuv420p',
                '-bf', '0',                          # no B-frames → no reorder at seams
                '-force_key_frames', kf_arg,         # IDR at every seam
                '-fps_mode', 'cfr',                  # strict CFR, no auto-duplication
                '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
                '-movflags', '+faststart',
                str(out_path),
            ]
            try:
                proc = subprocess.run(cmd_re, capture_output=True, text=True, timeout=900)
            except subprocess.TimeoutExpired:
                try: list_file.unlink(missing_ok=True)
                except Exception: pass
                return jsonify({'error': 'ffmpeg timeout (filter-complex >15 min)'}), 500
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or '')[-3000:]
                print(f'[auto-assemble] {sid}/ep{num} ffmpeg FAILED:\n{stderr_tail}', flush=True)
                try: list_file.unlink(missing_ok=True)
                except Exception: pass
                return jsonify({
                    'error': 'ffmpeg failed (filter-complex re-encode)',
                    'stderr': stderr_tail,
                }), 500

    try: list_file.unlink(missing_ok=True)
    except Exception: pass

    # Mark episode as assembled + record path.
    with _episode_lock(sid, num):
        ep2 = load_episode(sid, num)
        rel = str(out_path.relative_to(base))
        ep2['assembled_path'] = rel
        ep2['assembled_at'] = int(time.time())
        ep2['gen_status'] = 'done'
        save_episode(sid, num, ep2)

    size_mb = round(out_path.stat().st_size / 1024 / 1024, 2)
    return jsonify({
        'ok': True,
        'path': rel,
        'url': f'/assets/{sid}/{rel}',
        'size_mb': size_mb,
        'chunks': len(seg_paths),
        'facades_inserted': facades_inserted,
        'filename': out_name,
    })


# ════════════════════════════════════════════════════════════════════════════
# 🎵 MUSIC — ElevenLabs Music API per scene
from sw.routes.music import (
    MUSIC_SINGLE_TRACK,
    MUSIC_TRACK_DURATION_MS,
    MUSIC_SINGLE_SCENE_IDX,
    _MUSIC_LOCKS,
    _MUSIC_LOCKS_GUARD,
    _music_lock,
    _music_scenes,
    _music_scene_record,
    _safe_series_filename,
    _backfill_scene_meta_from_batch_prompts,
    _group_chunks_by_scene,
    _split_script_by_scenes,
    _episode_music_dir,
    _scene_wav_path,
    _generate_scene_music_worker,
    _kick_music_generation,
    music_generate,
    music_regenerate,
    _MUSIC_STALE_TIMEOUT_S,
    music_poll,
    music_download,
)
@app.route('/api/series/<sid>/episodes/<int:num>/seedance/download-zip')
def seedance_download_zip(sid, num):
    """Stream a ZIP archive of selected chunk video files.
    Query: ?idxs=1,2,3,5  (comma-separated chunk indices)
    Each entry inside the ZIP is named `chunk_NN.mp4` — sorted by idx.
    Skips chunks without a stored video file (in-progress / failed)."""
    import io, zipfile
    from flask import Response
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    raw = (request.args.get('idxs') or '').strip()
    try:
        wanted_idxs = sorted({int(x) for x in raw.split(',') if x.strip()})
    except ValueError:
        return jsonify({'error': 'bad idxs'}), 400
    if not wanted_idxs:
        return jsonify({'error': 'no idxs'}), 400
    chunks = _seedance_chunks(ep)
    base = series_path(sid)
    # Build zip in-memory (chunk videos are small, ~5-10MB each; user typically
    # picks 5-20 chunks). For huge selections we'd stream, but in-memory is
    # simpler and avoids fancy chunked-encoding.
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for c in chunks:
            if c.get('idx') not in wanted_idxs:
                continue
            vp = c.get('video_path')
            if not vp:
                continue
            abs_path = base / vp
            if not abs_path.exists():
                continue
            arcname = f'ep{int(num):03d}_chunk_{int(c.get("idx") or 0):02d}.mp4'
            zf.write(abs_path, arcname=arcname)
            written += 1
    if not written:
        return jsonify({'error': 'no completed videos in selection'}), 404
    buf.seek(0)
    fname = f'{slugify(sid)}_ep{int(num):03d}_{written}clips.zip'
    return Response(
        buf.getvalue(),
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{fname}"',
            'Content-Length': str(buf.getbuffer().nbytes),
        },
    )

@app.route('/api/series/<sid>/episodes/<int:num>/generate-scene-blocking', methods=['POST'])
def generate_scene_blocking(sid, num):
    """Single Claude call → 60-120 word English SCENE BLOCKING text describing
    geometry of the location and where each character is positioned across the
    whole episode. Used as shared context in batch-compose so all segments stay
    spatially consistent (no character teleporting between chunks)."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Сценарий пустой — заполни сначала'}), 400

    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    tag_mapping = _build_episode_tag_mapping(script, active_chars, active_locs)
    tag_lines = '\n'.join(
        f"  {t['tag']} = {t['kind']}: {t['name']}"
        for t in tag_mapping
    ) or '  (no active chars/locs)'

    sysprompt = (
        "Ты — кинематографист. На вход — один эпизод сценария TikTok-драмы. "
        "Твоя задача — описать ГЕОМЕТРИЮ сцены и расстановку персонажей одним коротким "
        "английским абзацем 60-120 слов. Этот текст затем вшивается в Constraints каждого "
        "Seedance-чанка чтобы Seedance видел одну и ту же расстановку во всех сегментах серии "
        "и НЕ ломал spatial continuity.\n\n"
        "ЧТО ВКЛЮЧАТЬ:\n"
        "  • Геометрия локации: где стол / стулья / окна / двери / лестницы. Стороны: "
        "    'long table runs left-right', 'glass wall on the back', 'door on the back-left'.\n"
        "  • Где КАЖДЫЙ активный @ImageN-персонаж сидит/стоит относительно объектов и других: "
        "    '@Image1 Maya stands at the LEFT side of the table, body angled camera-right toward Liam'.\n"
        "  • Когда и откуда заходит/выходит персонаж: "
        "    '@Image2 Liam enters from back-left door at start, walks to the FAR RIGHT head of the table'.\n"
        "  • Реквизит и где лежит: 'leather folder placed at the centre-left of the table'.\n\n"
        "CROSS-EPISODE CONTINUITY — критично:\n"
        "  Если на вход дан блок 'PREV EPISODE CONTEXT' и первая сцена ЭТОГО эпизода "
        "является ПРОДОЛЖЕНИЕМ последней сцены предыдущего (та же локация, нет явного scene "
        "heading с другим местом, нет time-jump в первых 1-2 строках) — НАСЛЕДУЙ позиции "
        "персонажей из prev episodeBlocking и ending state. Adrian остался у двери — он у "
        "двери в начале нового эпизода. Clara сидела за столом — она там же.\n"
        "  Если же сцена ЯВНО другая (новый scene heading с другой локацией, time-jump 'утром' / "
        "'через час', явная смена места) — начинай blocking с нуля, prev контекст игнорируй.\n\n"
        "ФОРМАТ:\n"
        "  • Английский, 60-120 слов, ОДИН абзац.\n"
        "  • Используй ТОЛЬКО @ImageN-теги из переданного TAG MAPPING.\n"
        "  • Никаких 'same/still/as before/continues' (нарушает автономность).\n"
        "  • Без markdown, без bullet-points.\n"
        "  • Описание универсальное для всего эпизода — конкретные действия чанков НЕ упоминай."
    )

    prev_context = _prev_episode_ending_context(sid, num)
    prev_block = ''
    if prev_context:
        prev_block = (
            f"=== PREV EPISODE CONTEXT — для проверки continuity ===\n"
            f"{prev_context}\n"
            f"=== END PREV EPISODE CONTEXT ===\n\n"
        )

    userprompt = (
        f"АКТИВНЫЕ ПЕРСОНАЖИ И ЛОКАЦИИ:\n{tag_lines}\n\n"
        f"{prev_block}"
        f"СЦЕНАРИЙ ЭПИЗОДА:\n```\n{script[:8000]}\n```\n\n"
        f"Верни ТОЛЬКО абзац blocking. Без преамбулы, без 'Here is...', без markdown."
    )
    try:
        text = claude_ask(userprompt, system=sysprompt).strip()
        text = text.replace('\n', ' ').strip()
        text = re.sub(r'\s+', ' ', text)
        text = text[:1500]
        ep['scene_blocking'] = text
        save_episode(sid, num, ep)
        return jsonify({'blocking': text, 'tag_mapping': tag_mapping})
    except Exception as e:
        return jsonify({'error': f'generate failed: {e}'}), 500
