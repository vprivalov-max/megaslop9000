"""Series list/meta routes: list, rename-out, meta update."""
import json
import re
import uuid

from flask import jsonify, request

from sw.core import app
from sw.storage import load_series, out_dir, save_series, user_root

# ── Series ───────────────────────────────────────────────────────────────────

@app.route('/api/series', methods=['GET'])
def list_series():
    # ?archived=1 → only archived projects. Default → only non-archived.
    want_archived = request.args.get('archived') in ('1', 'true', 'yes')
    result = []
    root = user_root()
    if not root.exists():
        return jsonify([])
    for d in sorted(root.iterdir()):
        sf = d / 'series.json'
        if sf.exists():
            s = json.loads(sf.read_text())
            is_archived = bool(s.get('archived'))
            if want_archived and not is_archived:
                continue
            if not want_archived and is_archived:
                continue
            ep_dir = d / 'episodes'
            ready = 0
            total = 0
            # Track latest mtime across series.json + every episode file so the
            # main-page "sort by modification" reflects real editing activity
            # (writing/regenerating an episode doesn't touch series.json itself).
            try:
                latest_mtime = int(sf.stat().st_mtime)
            except Exception:
                latest_mtime = 0
            if ep_dir.exists():
                for p in ep_dir.glob('*.json'):
                    # Filter out macOS AppleDouble metadata (._*) and any dotfile
                    if p.name.startswith('.'):
                        continue
                    total += 1
                    try:
                        mt = int(p.stat().st_mtime)
                        if mt > latest_mtime:
                            latest_mtime = mt
                    except Exception:
                        pass
                    try:
                        ep_data = json.loads(p.read_text())
                    except Exception:
                        continue
                    if ep_data.get('ready') or ep_data.get('reteller', {}).get('project_id'):
                        ready += 1
            s['_episode_count'] = ready
            s['_episode_total'] = total
            s['_updated_at'] = latest_mtime
            result.append(s)
    if want_archived:
        # Most recently archived first
        result.sort(key=lambda s: -(s.get('archived_at') or 0))
    else:
        # Pinned projects first, then unpinned. Within each group keep alphabetic order
        # (already sorted by directory name above).
        result.sort(key=lambda s: (0 if s.get('pinned') else 1, -(s.get('pinned_at') or 0)))
    return jsonify(result)


@app.route('/api/series/<sid>/rename-out', methods=['POST'])
def rename_out_files(sid):
    """Rename every file inside <series>/OUT/ to the studio's delivery
    convention:
      • Video → <Series_Name>_E<N>(.ext) or <Series_Name>_E<a>-<b>(.ext) for ranges
      • Audio (VO / MUS / SFX) → <KIND>_<Series_Name>_<idx>.<ext>
    Idempotent — files already in canonical form are skipped.
    Files we can't classify are reported in `skipped` so the user can rename
    them manually."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    folder = out_dir(sid)
    if not folder.exists():
        return jsonify({'error': 'OUT folder not found'}), 404

    title = (s.get('title') or sid).strip()
    # series_safe: alnum/underscore only, exactly the form used in the spec ("Series_Name")
    safe = re.sub(r'[^\w]+', '_', title, flags=re.UNICODE).strip('_') or sid

    VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
    AUDIO_EXT = {'.wav', '.mp3', '.m4a', '.aac', '.ogg', '.flac'}

    files = sorted([p for p in folder.iterdir() if p.is_file() and not p.name.startswith('.')])

    def detect_episode_token(stem):
        # Range first: E1-5 / ep1-5 / 1-5
        m = re.search(r'\b[Ee]?p?(\d{1,3})\s*[-–]\s*(\d{1,3})\b', stem)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < b and b - a < 200:
                return f'E{a}-{b}'
        m = re.search(r'\b[Ee]p?(?:isode)?[\s_-]?(\d{1,4})\b', stem)
        if m: return f'E{int(m.group(1))}'
        # Bare number as last resort
        m = re.search(r'(?<!\w)(\d{1,4})(?!\w)', stem)
        if m: return f'E{int(m.group(1))}'
        return None

    AUDIO_KIND_PATTERNS = [
        ('VO',  re.compile(r'(?:^|[_\-\s])(vo|voice|vocal|voiceover|dial|dialog|dialogue|repl|replicas?|reps)(?:[_\-\s.\d]|$)', re.I)),
        ('MUS', re.compile(r'(?:^|[_\-\s])(mus|music|track|score|bgm|theme|song|ost)(?:[_\-\s.\d]|$)', re.I)),
        ('SFX', re.compile(r'(?:^|[_\-\s])(sfx|fx|sound|effect|noise|amb|ambient|foley)(?:[_\-\s.\d]|$)', re.I)),
    ]

    def detect_audio_kind(stem):
        for kind, pat in AUDIO_KIND_PATTERNS:
            if pat.search(stem):
                return kind
        return None

    plans = []           # list of (Path, new_name)
    skipped = []         # list of {name, reason}
    audio_buckets = {'VO': [], 'MUS': [], 'SFX': []}

    for p in files:
        ext = p.suffix.lower()
        if ext in VIDEO_EXT:
            tok = detect_episode_token(p.stem)
            if not tok:
                skipped.append({'name': p.name, 'reason': 'не удалось определить номер эпизода'})
                continue
            plans.append((p, f'{safe}_{tok}{ext}'))
        elif ext in AUDIO_EXT:
            kind = detect_audio_kind(p.stem)
            if not kind:
                skipped.append({'name': p.name, 'reason': 'не определилось VO/MUS/SFX'})
                continue
            audio_buckets[kind].append(p)
        else:
            skipped.append({'name': p.name, 'reason': f'неподдерживаемое расширение {ext}'})

    # Number audio within each bucket alphabetically — stable & predictable
    for kind, lst in audio_buckets.items():
        for i, p in enumerate(sorted(lst, key=lambda x: x.name.lower()), 1):
            plans.append((p, f'{kind}_{safe}_{i}{p.suffix.lower()}'))

    # Two-stage rename to avoid collisions when N files want the same target
    tmp_moves = []  # (tmp_path, new_name, original_name)
    errors = []
    for p, new_name in plans:
        if p.name == new_name:
            continue  # already canonical
        tmp = p.with_name(f'.__rnm_{uuid.uuid4().hex[:8]}_{p.name}')
        try:
            p.rename(tmp)
            tmp_moves.append((tmp, new_name, p.name))
        except Exception as e:
            errors.append({'name': p.name, 'error': f'stage1: {e}'})

    renamed = []
    for tmp, new_name, orig in tmp_moves:
        target = tmp.parent / new_name
        if target.exists():
            # someone else already occupies the target — restore and report
            errors.append({'name': orig, 'error': f'цель {new_name} уже существует'})
            try: tmp.rename(tmp.parent / orig)
            except Exception: pass
            continue
        try:
            tmp.rename(target)
            renamed.append({'from': orig, 'to': new_name})
        except Exception as e:
            errors.append({'name': orig, 'error': f'stage2: {e}'})
            try: tmp.rename(tmp.parent / orig)
            except Exception: pass

    return jsonify({
        'series_safe_name': safe,
        'total_files': len(files),
        'renamed': renamed,
        'skipped': skipped,
        'errors': errors,
    })


@app.route('/api/series/<sid>/meta', methods=['POST'])
def update_series_meta(sid):
    """Lightweight metadata patch (color label, starred flag) — used by the
    projects-grid UI without touching settings/style/episodes."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    if 'color' in body:
        c = (body.get('color') or '').strip()
        # Whitelist: empty (clear) or one of the swatch slugs
        allowed = {'', 'red', 'orange', 'yellow', 'green', 'teal', 'blue', 'purple', 'pink', 'gray'}
        if c not in allowed:
            return jsonify({'error': f'invalid color: {c}'}), 400
        s['color'] = c
    if 'starred' in body:
        s['starred'] = bool(body['starred'])
    save_series(sid, s)
    return jsonify({'color': s.get('color', ''), 'starred': bool(s.get('starred'))})

