"""Photo upload (drag&drop), open-in-Finder, location image generation routes."""
import re
import subprocess
import time
from pathlib import Path

from flask import jsonify, request

from sw.avai import _series_image_provider, avai_generate
from sw.core import app
from sw.era import _no_caption_text_clause, _strip_cast_names_for_visual
from sw.storage import assets_dir, load_series, save_series, series_path
from sw.style import _location_crowd_clause, _series_style_clause
from sw.utils import asset_name, slugify

# ── Upload photo via drag & drop ─────────────────────────────────────────────

@app.route('/api/series/<sid>/characters/<char_id>/upload-photo', methods=['POST'])
def upload_char_photo(sid, char_id):
    s = load_series(sid)
    char = next((c for c in s.get('characters', []) if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no file'}), 400
    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    filename = f'{asset_name(char["name"], "BASE")}.{ext}'
    new_rel = f'assets/characters/{char_slug}/{filename}'
    new_full = char_dir / filename
    # Replace semantics: when user uploads their own photo, drop ALL prior
    # base refs + delete the on-disk files (not just append). Old photos
    # were accumulating in the gallery — user complained it's confusing.
    # Skip deletion of files that share the new path (overwrite case).
    base = series_path(sid)
    for old_rel in (char.get('ref_images') or []):
        if old_rel == new_rel:
            continue
        try:
            (base / old_rel).unlink(missing_ok=True)
        except Exception:
            pass
    new_full.write_bytes(f.read())
    char['ref_images'] = [new_rel]
    char['avai_base_url'] = ''  # invalidate cached AVAI URL — was for the old auto-gen
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{new_rel}', 'series': s})


@app.route('/api/series/<sid>/locations/<loc_id>/upload-photo', methods=['POST'])
def upload_loc_photo(sid, loc_id):
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no file'}), 400
    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    loc_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    filename = f'{asset_name(loc["name"])}.{ext}'
    new_rel = f'assets/locations/{loc_slug}/{filename}'
    new_full = loc_dir / filename
    base = series_path(sid)
    for old_rel in (loc.get('ref_images') or []):
        if old_rel == new_rel:
            continue
        try:
            (base / old_rel).unlink(missing_ok=True)
        except Exception:
            pass
    new_full.write_bytes(f.read())
    loc['ref_images'] = [new_rel]
    loc['avai_url'] = ''  # invalidate cached AVAI URL
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{new_rel}', 'series': s})


# ── Open folder in Finder ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/open-folder', methods=['POST'])
def open_folder(sid):
    folder_type = (request.json or {}).get('type', 'assets')
    base = assets_dir(sid)
    paths = {
        'characters': base / 'characters',
        'locations':  base / 'locations',
        'items':      base / 'items',
        'assets':     base,
    }
    folder = paths.get(folder_type, base)
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(['open', str(folder)])
    return jsonify({'ok': True, 'path': str(folder)})


# ── Location image generation ────────────────────────────────────────────────

@app.route('/api/series/<sid>/locations/<loc_id>/generate-image', methods=['POST'])
def generate_location_image(sid, loc_id):
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'not found'}), 404

    tone = s.get('tone', '')
    style_clause = _series_style_clause(s)
    constraints = (loc.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    loc_name = _strip_cast_names_for_visual(loc['name'], s)
    loc_desc = _strip_cast_names_for_visual(loc.get('description', ''), s)
    prompt = (
        f"{loc_name}. {loc_desc}.{constraints_clause} "
        f"{_location_crowd_clause(loc)}"
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing.{_no_caption_text_clause(constraints)}"
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    out_path = loc_dir / f'{asset_name(loc["name"])}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, aspect_ratio='16:9', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = loc.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        loc['avai_url'] = image_url  # used by Seedance for video refs
        loc['image_version'] = int(time.time())  # cache-bust marker for UI
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}',
                        'image_url': image_url, 'image_version': loc['image_version']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/locations/<loc_id>/regenerate', methods=['POST'])
def regenerate_location(sid, loc_id):
    """Regenerate the location's establishing shot with user-supplied constraints.
    Persists constraints on the location."""
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'location not found'}), 404
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    loc['image_constraints'] = wishes
    tone = s.get('tone', '')
    style_clause = _series_style_clause(s)
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    loc_name = _strip_cast_names_for_visual(loc['name'], s)
    loc_desc = _strip_cast_names_for_visual(loc.get('description', ''), s)
    prompt = (
        f"{loc_name}. {loc_desc}.{constraints_clause} "
        f"{_location_crowd_clause(loc)}"
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing.{_no_caption_text_clause(wishes)}"
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    loc_dir.mkdir(parents=True, exist_ok=True)
    out_path = loc_dir / f'{asset_name(loc["name"])}.jpg'
    try:
        if out_path.exists():
            try: out_path.unlink()
            except Exception: pass
        image_url = avai_generate(prompt, out_path, aspect_ratio='16:9', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = loc.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        loc['avai_url'] = image_url
        loc['image_version'] = int(time.time())
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}',
                        'image_url': image_url, 'image_version': loc['image_version']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


