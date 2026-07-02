"""Asset routes: serve/upload character, location and item images."""
import datetime
import mimetypes
import time
from pathlib import Path

from flask import jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from sw.avai import allowed_file
from sw.core import app
from sw.logging_utils import _log_event
from sw.storage import assets_dir, load_series, save_series, series_path
from sw.utils import asset_name, slugify

# ── Assets ───────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/assets/character/<char_id>', methods=['POST'])
def upload_character_asset(sid, char_id):
    """Upload a user-supplied photo. REPLACES the existing ref images
    (any prior auto-generated portraits get removed from disk + dropped
    from ref_images). Mirrors the behaviour of /upload-photo so both
    upload entrypoints are consistent — user reported "uploaded photo
    disappears, old one stays" because the two endpoints had different
    semantics (this one was append-only, /upload-photo replaces).
    Now both replace; delete-button in the gallery still works for
    individual ref removal."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400

    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'character not found'}), 404

    char_dir = assets_dir(sid) / 'characters' / char_id
    char_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    stem = Path(filename).stem
    ext = Path(filename).suffix or '.jpg'
    final = char_dir / filename
    # Don't fight name collisions with old refs — those refs are about to be
    # wiped anyway. Just use the user's filename verbatim, overwriting if needed.
    rel_path = str(final.relative_to(series_path(sid)))

    # Wipe prior on-disk files + ref_images entries (skip the path we're about
    # to write so we don't accidentally delete the new file in case of overlap).
    base = series_path(sid)
    for old_rel in (char.get('ref_images') or []):
        if old_rel == rel_path:
            continue
        try: (base / old_rel).unlink(missing_ok=True)
        except Exception: pass

    file.save(final)
    char['ref_images'] = [rel_path]
    char['avai_base_url'] = ''  # invalidate — old AVAI URL pointed at the old (deleted) gen
    char['updated_at'] = int(time.time())   # cache-bust signal for frontend URLs
    save_series(sid, s)
    # Log so we can trace user-reported "uploaded photo vanished" cases.
    try:
        size = final.stat().st_size if final.exists() else -1
    except Exception:
        size = -1
    _log_event('INFO', 'upload_character_asset', sid=sid, char_id=char_id,
               filename=filename, rel_path=rel_path, file_size=size,
               file_exists_after_save=final.exists())
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}', 'series': s})

@app.route('/api/series/<sid>/assets/character/<char_id>/<path:filename>', methods=['DELETE'])
def delete_character_asset(sid, char_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'character not found'}), 404
    # Files live under assets/characters/<slug>/, NOT assets/characters/<char_id>/.
    # Find the matching ref_image by basename, delete the actual file at its stored path.
    refs = char.get('ref_images') or []
    matched_rel = next((r for r in refs if Path(r).name == filename), None)
    if matched_rel:
        full = series_path(sid) / matched_rel
        if full.exists():
            full.unlink(missing_ok=True)
        char['ref_images'] = [r for r in refs if r != matched_rel]
        # Clear avai_base_url if this was the base portrait
        if asset_name(char.get('name', ''), 'BASE') in filename:
            char.pop('avai_base_url', None)
        char['updated_at'] = int(time.time())   # cache-bust signal for frontend URLs
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/assets/style', methods=['POST'])
def upload_style_asset(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400

    style_dir = assets_dir(sid) / 'style'
    style_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    stem = Path(filename).stem
    ext = Path(filename).suffix
    final = style_dir / filename
    counter = 1
    while final.exists():
        final = style_dir / f'{stem}_{counter}{ext}'
        counter += 1

    file.save(final)
    rel_path = str(final.relative_to(series_path(sid)))
    s['style'].setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}'})

@app.route('/api/series/<sid>/assets/style/<path:filename>', methods=['DELETE'])
def delete_style_asset(sid, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'style' / filename
    if full.exists():
        full.unlink()
    rel = f'assets/style/{filename}'
    s['style']['ref_images'] = [r for r in s['style'].get('ref_images', []) if r != rel]
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/assets/<sid>/<path:filepath>')
def serve_asset(sid, filepath):
    asset_path = series_path(sid) / filepath
    resp = send_from_directory(str(asset_path.parent), asset_path.name)
    # Long-cache (1 year) since the URL itself includes a `?v=<ts>` cache
    # buster from assetUrl() — when the file changes, the FE bumps the
    # version → URL changes → browser fetches the new bytes. While the
    # version stays the same, browser serves from cache → page reload
    # is instant instead of refetching every image.
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@app.route('/api/series/<sid>/skip-autogen', methods=['POST'])
def set_skip_autogen(sid):
    """Marks specific entities (chars / locs / items) as opted-out of the
    autogen sweep. Body: {chars: [id, ...], locs: [...], items: [...], skip: true}.
    With skip=false → unsets the flag (re-includes them in future sweeps).
    Used by the Accept-script modal's "🚫 Не генерить эту группу" checkbox."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    skip = bool(body.get('skip', True))
    char_ids = set(body.get('chars') or [])
    loc_ids  = set(body.get('locs')  or [])
    item_ids = set(body.get('items') or [])
    touched = 0
    for c in s.get('characters', []):
        if c['id'] in char_ids:
            if skip: c['_skip_autogen'] = True
            else:    c.pop('_skip_autogen', None)
            touched += 1
    for l in s.get('locations', []):
        if l['id'] in loc_ids:
            if skip: l['_skip_autogen'] = True
            else:    l.pop('_skip_autogen', None)
            touched += 1
    for it in s.get('items', []):
        if it['id'] in item_ids:
            if skip: it['_skip_autogen'] = True
            else:    it.pop('_skip_autogen', None)
            touched += 1
    if touched:
        save_series(sid, s)
    return jsonify({'touched': touched, 'skip': skip})


@app.route('/api/series/<sid>/relink-assets', methods=['POST'])
def relink_assets(sid):
    """Walks the assets/ folder and re-attaches orphaned files back into
    series.json. Use case: a character/loc/item has a photo on disk in
    assets/characters/<slug>/<NAME>_BASE.{jpg,png,webp} but its `ref_images`
    list is empty — this happens when a corrupted save_series wiped the refs
    (the atomic-write fix prevents NEW occurrences but doesn't heal old data).

    For every char/loc/item whose ref_images is empty:
      - look in assets/<kind>/<slug>/ for files matching `<NAME_STEM>_BASE.*`
        or `<NAME_STEM>.*` (loc/item) ignoring `._*` and `.tmp.*`
      - if found, prepend the relative path to ref_images and update
        outfit.photo / avai_url where applicable
      - if multiple candidates, pick the freshest mtime

    Returns {'relinked': [{kind, id, name, files: [...]}, ...]}.
    Idempotent — running twice does nothing the second time."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    base = series_path(sid)
    relinked = []

    def _scan_dir(d, name_stem):
        """Find files in dir whose stem matches name_stem (case-insensitive),
        sorted by mtime descending. Skip hidden / tmp."""
        if not d.exists():
            return []
        out = []
        target = name_stem.upper()
        for p in d.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith('._') or '.tmp.' in p.name:
                continue
            if p.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                continue
            stem_upper = p.stem.upper()
            if stem_upper == target or stem_upper.startswith(target + '_') or stem_upper == target + '_BASE':
                out.append(p)
        out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return out

    # Characters
    for c in s.get('characters', []):
        if c.get('ref_images'):
            continue
        slug = slugify(c['name'])
        char_dir = base / 'assets' / 'characters' / slug
        stem = asset_name(c['name'], 'BASE')  # canonical
        # Try BASE first, then bare name.
        cands = _scan_dir(char_dir, stem)
        if not cands:
            cands = _scan_dir(char_dir, asset_name(c['name']))
        if not cands:
            continue
        rels = [str(p.relative_to(base)) for p in cands]
        c['ref_images'] = rels
        # Restore base outfit photo if it's marked is_base and empty.
        for o in c.get('outfits', []) or []:
            if o.get('is_base') and not o.get('photo'):
                o['photo'] = rels[0]
        relinked.append({'kind': 'char', 'id': c['id'], 'name': c['name'], 'files': rels})

    # Locations
    for l in s.get('locations', []):
        if l.get('ref_images'):
            continue
        slug = slugify(l['name'])
        loc_dir = base / 'assets' / 'locations' / slug
        cands = _scan_dir(loc_dir, asset_name(l['name']))
        if not cands:
            continue
        rels = [str(p.relative_to(base)) for p in cands]
        l['ref_images'] = rels
        relinked.append({'kind': 'loc', 'id': l['id'], 'name': l['name'], 'files': rels})

    # Items
    for it in s.get('items', []):
        if it.get('ref_images'):
            continue
        slug = slugify(it['name'])
        item_dir = base / 'assets' / 'items' / slug
        cands = _scan_dir(item_dir, asset_name(it['name']))
        if not cands:
            continue
        rels = [str(p.relative_to(base)) for p in cands]
        it['ref_images'] = rels
        relinked.append({'kind': 'item', 'id': it['id'], 'name': it['name'], 'files': rels})

    if relinked:
        save_series(sid, s)
    return jsonify({'relinked': relinked, 'count': len(relinked)})


@app.route('/api/series/<sid>/debug-asset')
def debug_asset(sid):
    """Diagnostic for the broken-image placeholder. Reports filesystem state
    of an asset path the UI failed to load: existence, size, mtime, mime, and
    whether the path appears in the parent series.json's ref lists. The user
    pastes this back to support so we can tell whether the file vanished off
    disk vs got dropped from refs vs was never there."""
    rel_path = (request.args.get('path') or '').strip()
    if not rel_path:
        return jsonify({'error': 'path required'}), 400
    base = series_path(sid).resolve()
    full = (base / rel_path).resolve()
    # Path traversal guard.
    try:
        full.relative_to(base)
    except ValueError:
        return jsonify({'error': 'path escapes series dir'}), 400
    out = {
        'sid': sid,
        'rel_path': rel_path,
        'absolute_path': str(full),
        'exists': full.exists(),
        'is_file': full.is_file() if full.exists() else False,
        'parent_exists': full.parent.exists(),
        'parent_listing': [],
        'in_refs': [],
    }
    if full.exists() and full.is_file():
        try:
            st = full.stat()
            import mimetypes
            out['size_bytes'] = st.st_size
            out['mtime_iso']  = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
            out['mime_guess'] = mimetypes.guess_type(str(full))[0]
            with open(full, 'rb') as f:
                head = f.read(16)
            out['magic_hex'] = head.hex()
            out['magic_kind'] = (
                'jpeg' if head[:3] == b'\xff\xd8\xff' else
                'png'  if head[:8] == b'\x89PNG\r\n\x1a\n' else
                'webp' if head[8:12] == b'WEBP' else
                'unknown'
            )
        except Exception as e:
            out['stat_error'] = str(e)
    if full.parent.exists():
        try:
            out['parent_listing'] = sorted([
                p.name for p in full.parent.iterdir()
                if not p.name.startswith('._') and '.tmp.' not in p.name
            ])[:50]
        except Exception as e:
            out['parent_listing_error'] = str(e)
    # Look up the ref in series.json so we know whether the path is even valid
    # from the data layer's POV.
    try:
        s = load_series(sid)
        if s:
            for c in s.get('characters', []):
                if rel_path in (c.get('ref_images') or []):
                    out['in_refs'].append({'kind': 'char', 'id': c.get('id'), 'name': c.get('name')})
                for o in (c.get('outfits') or []):
                    if o.get('photo') == rel_path:
                        out['in_refs'].append({'kind': 'outfit', 'char_id': c.get('id'),
                                               'outfit_id': o.get('id'), 'label': o.get('label')})
            for l in s.get('locations', []):
                if rel_path in (l.get('ref_images') or []):
                    out['in_refs'].append({'kind': 'loc', 'id': l.get('id'), 'name': l.get('name')})
            for it in s.get('items', []):
                if rel_path in (it.get('ref_images') or []):
                    out['in_refs'].append({'kind': 'item', 'id': it.get('id'), 'name': it.get('name')})
    except Exception as e:
        out['series_load_error'] = str(e)
    return jsonify(out)
