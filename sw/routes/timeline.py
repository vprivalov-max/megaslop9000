"""Timeline editor: one shared timeline per series (helpers + API routes)."""
import json
import shutil
import subprocess
import time

from flask import jsonify, request

from sw.core import app
from sw.state import RENDER_SEMAPHORE
from sw.storage import load_episode, load_series, series_path

# ─────────────────────────────────────────────────────────────────────────────
# Timeline editor (one shared timeline per series)
# ─────────────────────────────────────────────────────────────────────────────

def _timeline_file(sid): return series_path(sid) / 'timeline.json'

def _load_timeline(sid):
    f = _timeline_file(sid)
    if not f.exists():
        return {'clips': [], 'updated_at': 0}
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return {'clips': [], 'updated_at': 0}

def _save_timeline(sid, tl):
    tl['updated_at'] = int(time.time())
    _timeline_file(sid).write_text(
        json.dumps(tl, ensure_ascii=False, indent=2), encoding='utf-8'
    )

def _timeline_history_file(sid):
    return series_path(sid) / 'timeline_history.json'

def _timeline_redo_file(sid):
    return series_path(sid) / 'timeline_redo.json'

UNDO_CAP = 30

def _read_stack(f):
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return []

def _write_stack(f, stack):
    f.write_text(json.dumps(stack, ensure_ascii=False), encoding='utf-8')

def _push_undo(sid, label='', clear_redo=True):
    """Snapshot current timeline state into undo stack BEFORE a mutation.
    Any new user-driven mutation invalidates the redo stack (linear history)."""
    cur = _load_timeline(sid)
    hist = _read_stack(_timeline_history_file(sid))
    hist.append({'label': label, 'ts': int(time.time()), 'snapshot': cur})
    if len(hist) > UNDO_CAP:
        hist = hist[-UNDO_CAP:]
    _write_stack(_timeline_history_file(sid), hist)
    if clear_redo:
        _write_stack(_timeline_redo_file(sid), [])

def _pop_undo(sid):
    """Pop the latest undo snapshot. Pushes the *current* state into redo
    stack first, so undo is reversible via redo. Returns the popped item."""
    hist = _read_stack(_timeline_history_file(sid))
    if not hist:
        return None
    cur = _load_timeline(sid)
    redo = _read_stack(_timeline_redo_file(sid))
    redo.append({'label': hist[-1].get('label', ''), 'ts': int(time.time()), 'snapshot': cur})
    if len(redo) > UNDO_CAP:
        redo = redo[-UNDO_CAP:]
    _write_stack(_timeline_redo_file(sid), redo)
    item = hist.pop()
    _write_stack(_timeline_history_file(sid), hist)
    return item

def _pop_redo(sid):
    """Apply the latest redo snapshot. Pushes the current state back into
    undo stack so a redo is itself reversible. Does NOT clear redo stack."""
    redo = _read_stack(_timeline_redo_file(sid))
    if not redo:
        return None
    cur = _load_timeline(sid)
    hist = _read_stack(_timeline_history_file(sid))
    hist.append({'label': redo[-1].get('label', ''), 'ts': int(time.time()), 'snapshot': cur})
    if len(hist) > UNDO_CAP:
        hist = hist[-UNDO_CAP:]
    _write_stack(_timeline_history_file(sid), hist)
    item = redo.pop()
    _write_stack(_timeline_redo_file(sid), redo)
    return item

def _resolve_seedance_chunk(sid, ep_num, idx):
    ep = load_episode(sid, ep_num)
    if not ep:
        return None
    # lazy: seedance helpers live in a peer cluster (falls back to app.py until extracted)
    try:
        from sw.seedance_pipeline import _seedance_chunks
    except ImportError:
        from app import _seedance_chunks
    for c in _seedance_chunks(ep):
        if c.get('idx') == idx:
            return c
    return None

@app.route('/api/series/<sid>/timeline')
def timeline_get(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    tl = _load_timeline(sid)
    # enrich clips with current video_path / poster from source episode chunks
    enriched = []
    for c in tl.get('clips', []):
        info = dict(c)
        if c.get('source') == 'seedance':
            ch = _resolve_seedance_chunk(sid, c.get('episode'), c.get('chunk_idx'))
            if ch:
                info['video_path'] = ch.get('video_path') or ''
                info['video_url'] = ch.get('video_url') or ''
                info['orig_duration'] = ch.get('duration') or 0
                info['prompt_preview'] = (ch.get('prompt') or '')[:120]
        enriched.append(info)
    return jsonify({'clips': enriched, 'updated_at': tl.get('updated_at', 0)})

@app.route('/api/series/<sid>/timeline/clips/add', methods=['POST'])
def timeline_add(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    ep_num = int(body.get('episode'))
    chunk_idx = int(body.get('chunk_idx'))
    ch = _resolve_seedance_chunk(sid, ep_num, chunk_idx)
    if not ch:
        return jsonify({'error': 'chunk not found'}), 404
    if not ch.get('video_path'):
        return jsonify({'error': 'chunk has no rendered video yet'}), 400
    _push_undo(sid, 'add clip')
    tl = _load_timeline(sid)
    new_id = f"clip_{int(time.time()*1000)}_{len(tl.get('clips', []))}"
    clip = {
        'id': new_id,
        'source': 'seedance',
        'episode': ep_num,
        'chunk_idx': chunk_idx,
        'in': 0.0,
        'out': float(ch.get('duration') or 0),  # full clip by default
        'added_at': int(time.time()),
    }
    tl.setdefault('clips', []).append(clip)
    _save_timeline(sid, tl)
    return jsonify({'clip': clip, 'count': len(tl['clips'])})

@app.route('/api/series/<sid>/timeline/clips/<clip_id>', methods=['DELETE'])
def timeline_delete(sid, clip_id):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    _push_undo(sid, 'delete clip')
    tl = _load_timeline(sid)
    before = len(tl.get('clips', []))
    tl['clips'] = [c for c in tl.get('clips', []) if c.get('id') != clip_id]
    _save_timeline(sid, tl)
    return jsonify({'removed': before - len(tl['clips'])})

@app.route('/api/series/<sid>/timeline/reorder', methods=['POST'])
def timeline_reorder(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    order = body.get('order') or []  # list of clip ids
    _push_undo(sid, 'reorder')
    tl = _load_timeline(sid)
    by_id = {c['id']: c for c in tl.get('clips', [])}
    new_clips = [by_id[i] for i in order if i in by_id]
    # append any clip not present in order (defensive)
    for c in tl.get('clips', []):
        if c['id'] not in order:
            new_clips.append(c)
    tl['clips'] = new_clips
    _save_timeline(sid, tl)
    return jsonify({'ok': True, 'count': len(new_clips)})

@app.route('/api/series/<sid>/timeline/clips/<clip_id>/crop', methods=['PATCH'])
def timeline_crop(sid, clip_id):
    """Set or clear a crop rectangle on a clip.
    Body: {x, y, w, h} as fractions in [0..1] of the source frame, or
          {clear: true} to remove crop.
    Aspect of (w/h) should match output aspect (we don't enforce — just warn)."""
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    _push_undo(sid, 'crop')
    tl = _load_timeline(sid)
    for c in tl.get('clips', []):
        if c['id'] == clip_id:
            if body.get('clear'):
                c.pop('crop', None)
            else:
                x = max(0.0, min(1.0, float(body.get('x', 0))))
                y = max(0.0, min(1.0, float(body.get('y', 0))))
                w = max(0.05, min(1.0 - x, float(body.get('w', 1))))
                h = max(0.05, min(1.0 - y, float(body.get('h', 1))))
                c['crop'] = {'x': x, 'y': y, 'w': w, 'h': h}
            _save_timeline(sid, tl)
            return jsonify({'clip': c})
    return jsonify({'error': 'clip not found'}), 404

@app.route('/api/series/<sid>/timeline/clips/<clip_id>/trim', methods=['PATCH'])
def timeline_trim(sid, clip_id):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    _push_undo(sid, 'trim')
    tl = _load_timeline(sid)
    for c in tl.get('clips', []):
        if c['id'] == clip_id:
            if 'in' in body:  c['in']  = max(0.0, float(body['in']))
            if 'out' in body: c['out'] = max(0.1, float(body['out']))
            if c['out'] <= c['in']:
                c['out'] = c['in'] + 0.1
            _save_timeline(sid, tl)
            return jsonify({'clip': c})
    return jsonify({'error': 'clip not found'}), 404

@app.route('/api/series/<sid>/timeline/undo', methods=['POST'])
def timeline_undo(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    item = _pop_undo(sid)
    if not item:
        return jsonify({'error': 'nothing to undo'}), 400
    snap = item.get('snapshot') or {'clips': []}
    _save_timeline(sid, snap)
    return jsonify({'restored': item.get('label', ''), 'count': len(snap.get('clips') or [])})

@app.route('/api/series/<sid>/timeline/redo', methods=['POST'])
def timeline_redo(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    item = _pop_redo(sid)
    if not item:
        return jsonify({'error': 'nothing to redo'}), 400
    snap = item.get('snapshot') or {'clips': []}
    _save_timeline(sid, snap)
    return jsonify({'restored': item.get('label', ''), 'count': len(snap.get('clips') or [])})

@app.route('/api/series/<sid>/timeline/history')
def timeline_history(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    hist = _read_stack(_timeline_history_file(sid))
    redo = _read_stack(_timeline_redo_file(sid))
    last = hist[-1] if hist else None
    next_ = redo[-1] if redo else None
    return jsonify({
        'depth': len(hist),
        'redo_depth': len(redo),
        'last': {'label': last.get('label'), 'ts': last.get('ts')} if last else None,
        'next': {'label': next_.get('label'), 'ts': next_.get('ts')} if next_ else None,
    })

@app.route('/api/series/<sid>/timeline/clips/<clip_id>/split', methods=['POST'])
def timeline_split(sid, clip_id):
    """Split a clip at local time `at` (seconds from clip start).
    `at` is the playhead position relative to the clip's IN point — i.e. the
    moment in the *trimmed* clip where the user wants to cut.
    Produces two adjacent clips A=[in..in+at], B=[in+at..out]."""
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    at = float(body.get('at') or 0)
    _push_undo(sid, 'split')
    tl = _load_timeline(sid)
    clips = tl.get('clips') or []
    for i, c in enumerate(clips):
        if c.get('id') != clip_id:
            continue
        cin  = float(c.get('in', 0))
        cout = float(c.get('out', 0))
        cut_abs = cin + at
        # Need at least 0.1s on both sides
        if cut_abs <= cin + 0.05 or cut_abs >= cout - 0.05:
            return jsonify({'error': 'too close to edge — нечего резать'}), 400
        a = dict(c)
        b = dict(c)
        a['out'] = cut_abs
        b['id'] = f"clip_{int(time.time()*1000)}_{i}b"
        b['in'] = cut_abs
        b['added_at'] = int(time.time())
        clips[i] = a
        clips.insert(i + 1, b)
        _save_timeline(sid, tl)
        return jsonify({'a': a, 'b': b})
    return jsonify({'error': 'clip not found'}), 404

@app.route('/api/series/<sid>/timeline/render', methods=['POST'])
def timeline_render(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    tl = _load_timeline(sid)
    clips = tl.get('clips') or []
    if not clips:
        return jsonify({'error': 'timeline пустой'}), 400

    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return jsonify({'error': 'ffmpeg не установлен. brew install ffmpeg'}), 500

    # Resolve each clip to an absolute video path + check trims/crops
    seg_paths = []
    needs_trim = False
    needs_crop = False
    for c in clips:
        if c.get('source') != 'seedance':
            continue
        ch = _resolve_seedance_chunk(sid, c.get('episode'), c.get('chunk_idx'))
        if not ch or not ch.get('video_path'):
            return jsonify({'error': f"clip {c['id']} без видео"}), 400
        abs_path = series_path(sid) / ch['video_path']
        if not abs_path.exists():
            return jsonify({'error': f"file missing: {ch['video_path']}"}), 400
        cin  = float(c.get('in', 0))
        cout = float(c.get('out', ch.get('duration') or 0))
        full_dur = float(ch.get('duration') or 0)
        if cin > 0.05 or (full_dur and abs(cout - full_dur) > 0.05):
            needs_trim = True
        crop = c.get('crop')
        if crop and (crop.get('w', 1) < 0.999 or crop.get('h', 1) < 0.999
                     or crop.get('x', 0) > 0.001 or crop.get('y', 0) > 0.001):
            needs_crop = True
        seg_paths.append({'path': str(abs_path), 'in': cin, 'out': cout, 'crop': crop})

    renders_dir = series_path(sid) / 'renders'
    renders_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    out_path = renders_dir / f'timeline_{ts}.mp4'

    # Determine output dimensions: probe first clip and round down to even
    out_w, out_h = 720, 1280  # fallback
    try:
        ffprobe_bin = shutil.which('ffprobe') or (ffmpeg_bin.replace('ffmpeg', 'ffprobe'))
        if seg_paths and ffprobe_bin:
            pr = subprocess.run([
                ffprobe_bin, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=s=x:p=0', seg_paths[0]['path']
            ], capture_output=True, text=True, timeout=10)
            wh = (pr.stdout or '').strip().split('x')
            if len(wh) == 2:
                out_w = int(wh[0]) - (int(wh[0]) % 2)
                out_h = int(wh[1]) - (int(wh[1]) % 2)
    except Exception:
        pass

    if not needs_trim and not needs_crop:
        # fast concat-demuxer, no re-encode
        list_file = renders_dir / f'_concat_{ts}.txt'
        list_file.write_text(
            '\n'.join(f"file '{seg['path']}'" for seg in seg_paths),
            encoding='utf-8',
        )
        cmd = [
            ffmpeg_bin, '-y', '-f', 'concat', '-safe', '0',
            '-i', str(list_file), '-c', 'copy', str(out_path),
        ]
    else:
        # filter_complex per segment: optional trim → optional crop → scale to common size
        inputs = []
        filt = []
        for i, seg in enumerate(seg_paths):
            inputs += ['-i', seg['path']]
            crop = seg.get('crop')
            # Build video filter chain
            v_steps = [f"trim={seg['in']}:{seg['out']}", "setpts=PTS-STARTPTS"]
            if crop and (crop.get('w', 1) < 0.999 or crop.get('h', 1) < 0.999
                         or crop.get('x', 0) > 0.001 or crop.get('y', 0) > 0.001):
                cw = crop.get('w', 1); ch_ = crop.get('h', 1)
                cx = crop.get('x', 0); cy = crop.get('y', 0)
                v_steps.append(
                    f"crop=trunc(iw*{cw}/2)*2:trunc(ih*{ch_}/2)*2:"
                    f"trunc(iw*{cx}/2)*2:trunc(ih*{cy}/2)*2"
                )
            v_steps.append(
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease"
            )
            v_steps.append(f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black")
            v_steps.append("setsar=1")
            filt.append(f"[{i}:v]{','.join(v_steps)}[v{i}]")
            filt.append(
                f"[{i}:a]atrim={seg['in']}:{seg['out']},asetpts=PTS-STARTPTS[a{i}]"
            )
        n = len(seg_paths)
        concat_inputs = ''.join(f"[v{i}][a{i}]" for i in range(n))
        filt.append(f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]")
        cmd = [ffmpeg_bin, '-y', *inputs, '-filter_complex', ';'.join(filt),
               '-map', '[v]', '-map', '[a]',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
               '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '128k', str(out_path)]

    # Acquire the global render slot. If both slots are busy this blocks until
    # one frees, naturally queueing concurrent renders.
    queue_wait_start = time.time()
    with RENDER_SEMAPHORE:
        queue_waited = time.time() - queue_wait_start
        if queue_waited > 0.5:
            print(f'[render] {sid} waited {queue_waited:.1f}s in queue')
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'ffmpeg timeout (>10 min)'}), 500
        if proc.returncode != 0:
            return jsonify({
                'error': 'ffmpeg failed',
                'stderr': proc.stderr[-2000:],
                'cmd': ' '.join(cmd[:8]) + ' ...',
            }), 500
    try:
        if not needs_trim and not needs_crop:
            list_file.unlink(missing_ok=True)
    except Exception:
        pass
    rel = out_path.relative_to(series_path(sid))
    size_mb = round(out_path.stat().st_size / 1024 / 1024, 2)
    mode = 'concat-copy' if (not needs_trim and not needs_crop) else 'filter-complex'
    return jsonify({
        'path': str(rel),
        'url': f'/assets/{sid}/{rel}',
        'size_mb': size_mb,
        'mode': mode,
        'clips': len(seg_paths),
        'out_dims': f'{out_w}x{out_h}',
    })

@app.route('/api/series/<sid>/timeline/renders')
def timeline_renders(sid):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    rd = series_path(sid) / 'renders'
    if not rd.exists():
        return jsonify({'renders': []})
    items = []
    for p in sorted(rd.glob('timeline_*.mp4'), reverse=True):
        items.append({
            'name': p.name,
            'url': f'/assets/{sid}/renders/{p.name}',
            'size_mb': round(p.stat().st_size / 1024 / 1024, 2),
            'created_at': int(p.stat().st_mtime),
        })
    return jsonify({'renders': items})

@app.route('/api/series/<sid>/timeline/renders/<name>', methods=['DELETE'])
def timeline_render_delete(sid, name):
    if not load_series(sid):
        return jsonify({'error': 'not found'}), 404
    if not name.startswith('timeline_') or not name.endswith('.mp4'):
        return jsonify({'error': 'bad name'}), 400
    p = series_path(sid) / 'renders' / name
    if p.exists():
        p.unlink()
    return jsonify({'ok': True})


