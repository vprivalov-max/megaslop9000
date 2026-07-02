"""Entity CRUD routes: characters, locations, items, outfits."""
import re
import shutil
import time
import uuid
from pathlib import Path

import requests
from flask import jsonify, request
from werkzeug.utils import secure_filename

from sw.anthro import (_detect_animal_species_raw, _is_anthro_world,
                       _llm_infer_species_for_char,
                       _patch_appearance_with_species)
from sw.avai import allowed_file, avai_generate, rtl_headers, _series_image_provider
from sw.config import RETELLER_API
from sw.core import app
from sw.era import _clothing_clause, _series_era_hint
from sw.llm import claude_ask_fast
from sw.logging_utils import _log_event
from sw.storage import (assets_dir, list_episodes, load_series, save_episode,
                        save_series, series_path)
from sw.textrules_sanitizer import _sanitize_appearance_for_moderation
from sw.utils import asset_name, slugify

def _gen_char_base_inline(*a, **kw):
    # lazy proxy: image generation lives in a later-extracted module
    try:
        from sw.routes.images_char import _gen_char_base_inline as f
    except ImportError:
        from app import _gen_char_base_inline as f
    return f(*a, **kw)

from sw.style import (
    _DEFAULT_VISUAL_STYLE,
    _VISUAL_STYLE_PRESETS,
    _series_visual_style,
    _series_style_clause,
    _location_crowd_clause,
)
@app.route('/api/series/<sid>/characters', methods=['POST'])
def add_character(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    char = {
        'id': str(uuid.uuid4())[:8],
        'name': data['name'],
        'description': data.get('description', ''),
        'appearance': data.get('appearance', ''),
        'gender': data.get('gender', 'female'),
        'voice_id': data.get('voice_id', ''),
        'ref_images': []
    }
    s['characters'].append(char)
    save_series(sid, s)
    return jsonify(char), 201

@app.route('/api/series/<sid>/fix-anthro-species', methods=['POST'])
def fix_anthro_species(sid):
    """Bulk-fix endpoint: scan every character, and when the series is an
    anthro/furry world but a character lacks species in name/appearance, infer
    the species via LLM and patch the appearance. Clears the character's
    portrait so the UI can show "regenerate" prompts. Returns the list of
    patched characters with old/new appearance for review.
    Real bug: "The Landlord's Daughter" generated 3 human portraits in a furry
    world because the cast extractor dropped species words. This endpoint lets
    the user repair an existing series in one click."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    if not _is_anthro_world(s):
        return jsonify({
            'patched': [],
            'message': 'Этот сериал не определяется как анthrо-мир — нечего чинить. Если это ошибка, добавь слово «furry» или «anthropomorphic» в synopsis / world_description.',
        })
    patched = []
    for char in (s.get('characters') or []):
        existing_species = _detect_animal_species_raw(char.get('name'), char.get('appearance'))
        if existing_species:
            continue  # already species-coded — skip
        inferred = _llm_infer_species_for_char(s, char)
        if not inferred or inferred == 'human':
            continue
        old_appearance = char.get('appearance', '')
        new_appearance = _patch_appearance_with_species(old_appearance, inferred, char.get('gender', ''))
        if new_appearance == old_appearance:
            continue
        char['appearance'] = new_appearance
        # Drop stale portrait so the UI shows "regenerate".
        char['avai_base_url'] = ''
        char['updated_at'] = int(time.time())
        patched.append({
            'id': char.get('id'),
            'name': char.get('name'),
            'inferred_species': inferred,
            'old_appearance': old_appearance,
            'new_appearance': new_appearance,
        })
    if patched:
        save_series(sid, s)
    return jsonify({
        'patched': patched,
        'count': len(patched),
        'message': (f'Пропатчено персонажей: {len(patched)}. Теперь жми «Перегенерить» на каждом — портрет переснимется как {", ".join(p["inferred_species"] for p in patched)}.'
                    if patched else 'Все персонажи уже имеют species в appearance — патчить нечего.')
    })


@app.route('/api/series/<sid>/characters/<char_id>', methods=['PUT'])
def update_character(sid, char_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    for i, c in enumerate(s['characters']):
        if c['id'] == char_id:
            s['characters'][i].update(request.json)
            save_series(sid, s)
            return jsonify(s['characters'][i])
    return jsonify({'error': 'character not found'}), 404

@app.route('/api/series/<sid>/characters/<char_id>', methods=['DELETE'])
def delete_character(sid, char_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['characters'] = [c for c in s['characters'] if c['id'] != char_id]
    # Remove from all episodes
    for ep in list_episodes(sid):
        if char_id in ep.get('characters_used', []):
            ep['characters_used'].remove(char_id)
            save_episode(sid, ep['number'], ep)
    # Remove asset folder
    char_dir = assets_dir(sid) / 'characters' / char_id
    if char_dir.exists():
        shutil.rmtree(char_dir)
    save_series(sid, s)
    return jsonify({'ok': True})


# ── Locations ────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/locations', methods=['POST'])
def add_location(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    loc = {
        'id': str(uuid.uuid4())[:8],
        'name': data['name'],
        'description': data.get('description', ''),
        'ref_images': []
    }
    s.setdefault('locations', []).append(loc)
    save_series(sid, s)
    return jsonify(loc), 201

@app.route('/api/series/<sid>/locations/<loc_id>', methods=['PUT'])
def update_location(sid, loc_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    for i, l in enumerate(s.get('locations', [])):
        if l['id'] == loc_id:
            s['locations'][i].update(request.json)
            save_series(sid, s)
            return jsonify(s['locations'][i])
    return jsonify({'error': 'not found'}), 404

@app.route('/api/series/<sid>/locations/<loc_id>', methods=['DELETE'])
def delete_location(sid, loc_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['locations'] = [l for l in s.get('locations', []) if l['id'] != loc_id]
    loc_dir = assets_dir(sid) / 'locations' / loc_id
    if loc_dir.exists():
        shutil.rmtree(loc_dir)
    for ep in list_episodes(sid):
        if loc_id in ep.get('locations_used', []):
            ep['locations_used'].remove(loc_id)
            save_episode(sid, ep['number'], ep)
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/assets/location/<loc_id>', methods=['POST'])
def upload_location_asset(sid, loc_id):
    """Replace-semantics location upload (mirrors character endpoint above)."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'location not found'}), 404
    loc_dir = assets_dir(sid) / 'locations' / loc_id
    loc_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    final = loc_dir / filename
    rel_path = str(final.relative_to(series_path(sid)))
    base = series_path(sid)
    for old_rel in (loc.get('ref_images') or []):
        if old_rel == rel_path: continue
        try: (base / old_rel).unlink(missing_ok=True)
        except Exception: pass
    file.save(final)
    loc['ref_images'] = [rel_path]
    loc['avai_url'] = ''
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}', 'series': s})

@app.route('/api/series/<sid>/assets/location/<loc_id>/<path:filename>', methods=['DELETE'])
def delete_location_asset(sid, loc_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'locations' / loc_id / filename
    if full.exists():
        full.unlink()
    rel = f'assets/locations/{loc_id}/{filename}'
    for loc in s.get('locations', []):
        if loc['id'] == loc_id:
            loc['ref_images'] = [r for r in loc.get('ref_images', []) if r != rel]
    save_series(sid, s)
    return jsonify({'ok': True})


# ── Items (story-relevant props: handbag, gun, locket, etc.) ─────────────────
# Items are like locations but for THINGS. Same shape: { id, name, description,
# ref_images, image_constraints?, avai_url? }. Used by Seedance/Reteller as
# additional reference images when the item is plot-critical (e.g. the stolen
# handbag passed between characters across episodes).

@app.route('/api/series/<sid>/items', methods=['POST'])
def add_item(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    item = {
        'id': str(uuid.uuid4())[:8],
        'name': data['name'],
        'description': data.get('description', ''),
        'ref_images': []
    }
    s.setdefault('items', []).append(item)
    save_series(sid, s)
    return jsonify(item), 201

@app.route('/api/series/<sid>/items/<item_id>', methods=['PUT'])
def update_item(sid, item_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    for i, it in enumerate(s.get('items', [])):
        if it['id'] == item_id:
            s['items'][i].update(request.json)
            save_series(sid, s)
            return jsonify(s['items'][i])
    return jsonify({'error': 'not found'}), 404

@app.route('/api/series/<sid>/items/<item_id>', methods=['DELETE'])
def delete_item(sid, item_id):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    # Capture name BEFORE removing so we can also clean a slug-named dir
    # (in case generate-image used slug-based path).
    item_obj = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    item_slug = slugify(item_obj['name']) if item_obj else None

    s['items'] = [it for it in s.get('items', []) if it['id'] != item_id]
    for d in (assets_dir(sid) / 'items' / item_id, ):
        if d.exists():
            shutil.rmtree(d)
    if item_slug:
        d2 = assets_dir(sid) / 'items' / item_slug
        if d2.exists():
            shutil.rmtree(d2)
    for ep in list_episodes(sid):
        if item_id in ep.get('items_used', []):
            ep['items_used'].remove(item_id)
            save_episode(sid, ep['number'], ep)
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/assets/item/<item_id>', methods=['POST'])
def upload_item_asset(sid, item_id):
    """Replace-semantics item upload (mirrors character endpoint above)."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'invalid file type'}), 400
    item = next((x for x in s.get('items', []) if x['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'item not found'}), 404
    item_dir = assets_dir(sid) / 'items' / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    final = item_dir / filename
    rel_path = str(final.relative_to(series_path(sid)))
    base = series_path(sid)
    for old_rel in (item.get('ref_images') or []):
        if old_rel == rel_path: continue
        try: (base / old_rel).unlink(missing_ok=True)
        except Exception: pass
    file.save(final)
    item['ref_images'] = [rel_path]
    item['avai_url'] = ''
    save_series(sid, s)
    return jsonify({'path': rel_path, 'url': f'/assets/{sid}/{rel_path}'})

@app.route('/api/series/<sid>/assets/item/<item_id>/<path:filename>', methods=['DELETE'])
def delete_item_asset(sid, item_id, filename):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    full = series_path(sid) / 'assets' / 'items' / item_id / filename
    if full.exists():
        full.unlink()
    rel = f'assets/items/{item_id}/{filename}'
    for it in s.get('items', []):
        if it['id'] == item_id:
            it['ref_images'] = [r for r in it.get('ref_images', []) if r != rel]
    save_series(sid, s)
    return jsonify({'ok': True})


# ── Outfits ───────────────────────────────────────────────────────────────────

def get_char(s, char_id):
    return next((c for c in s['characters'] if c['id'] == char_id), None)

def get_outfit(char, outfit_id):
    return next((o for o in char.get('outfits', []) if o['id'] == outfit_id), None)

@app.route('/api/series/<sid>/characters/<char_id>/outfits', methods=['POST'])
def add_outfit(sid, char_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    outfit = {
        'id': str(uuid.uuid4())[:8],
        'label': data['label'],
        'description': data.get('description', ''),
        'photo': None,
        'reteller_project_id': None,
    }
    char.setdefault('outfits', []).append(outfit)
    save_series(sid, s)
    return jsonify(outfit), 201

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>', methods=['PUT'])
def update_outfit(sid, char_id, outfit_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404
    outfit.update({k: v for k, v in request.json.items() if k != 'id'})
    save_series(sid, s)
    return jsonify(outfit)

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>', methods=['DELETE'])
def delete_outfit(sid, char_id, outfit_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if outfit and outfit.get('photo'):
        p = series_path(sid) / outfit['photo']
        if p.exists():
            p.unlink()
    char['outfits'] = [o for o in char.get('outfits', []) if o['id'] != outfit_id]
    save_series(sid, s)
    return jsonify({'ok': True})

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>/use-base', methods=['POST'])
def link_outfit_to_base(sid, char_id, outfit_id):
    """Mark outfit as 'this IS the base look' — no separate generation needed.
    Sets outfit.photo = char.ref_images[0] and outfit.is_base = True."""
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'character not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404
    if not char.get('ref_images'):
        return jsonify({'error': 'У персонажа нет базового фото'}), 400

    # Clear any existing standalone outfit photo file (the one specific to this outfit)
    if outfit.get('photo') and outfit['photo'] != char['ref_images'][0]:
        old = series_path(sid) / outfit['photo']
        if old.exists():
            try:
                old.unlink()
            except Exception:
                pass

    outfit['photo'] = char['ref_images'][0]
    outfit['avai_url'] = char.get('avai_base_url', '')
    outfit['is_base'] = True
    # Unmark any other outfits as base (only one base per character)
    for o in char.get('outfits', []):
        if o['id'] != outfit_id and o.get('is_base'):
            o['is_base'] = False
    save_series(sid, s)
    return jsonify({'ok': True, 'photo': outfit['photo']})


@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>/generate', methods=['POST'])
def generate_outfit_image(sid, char_id, outfit_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'character not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404
    _nd_anthro, _fl_anthro = _anthro_preflight(s, [char])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    if not char.get('ref_images'):
        # Auto-generate the base portrait first — the user shouldn't have to chase a "load base"
        # error when we have the appearance text and can produce one inline.
        if not (char.get('appearance') or '').strip():
            return jsonify({
                'error': 'Сначала впиши описание внешности персонажа (поле appearance) — без него базовое фото не сгенерится'
            }), 400
        try:
            _gen_char_base_inline(s, sid, char)
            save_series(sid, s)
        except Exception as e:
            return jsonify({'error': f'Не удалось автоматически создать базовое фото: {e}'}), 500

    # Use i2i reference URL if available (AVAI Supabase URL from base generation)
    reference_url = char.get('avai_base_url')

    gender = 'woman' if char.get('gender') == 'female' else 'man'
    if reference_url:
        # i2i: same face, new clothes
        prompt = (
            f'Same {gender} as the reference image. Now wearing: {outfit["label"]}. '
            f'{outfit.get("description", "")}. '
            f'Same face, same hair, same body — only the clothing changes. '
            f'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
            f'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
            f'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
            f'Soft even diffused illumination on the character only (no visible lights or equipment), no harsh shadows. Photorealistic, cinematic quality.'
        )
    else:
        # No reference — generate from scratch with description
        prompt = (
            f'Full body portrait of {char["name"]}, a {gender}. '
            f'{char.get("appearance", "")}. '
            f'Wearing: {outfit["label"]}. {outfit.get("description", "")}. '
            f'Standing facing camera, slight 3/4 angle. Neutral relaxed pose. '
            'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
            f'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
            f'Soft even diffused illumination on the character only (no visible lights or equipment), no harsh shadows. Photorealistic, cinematic quality.'
        )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    # Variation token — provider-level caching (Banana/Seedream deduplicate
    # identical prompt strings) was returning the same image on every regen,
    # so deleting + re-generating an outfit pulled back the cached old one.
    # User-reported 2026-05-23. Same fix as `regenerate_character`.
    variation_token = uuid.uuid4().hex[:8]
    prompt = f"{prompt} [v{variation_token}]"

    char_slug = slugify(char['name'])
    out_dir = assets_dir(sid) / 'characters' / char_slug / 'outfits'
    out_path = out_dir / f'{asset_name(char["name"], outfit["label"])}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, reference_url=reference_url, preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        # Remove old photo if exists
        if outfit.get('photo'):
            old = series_path(sid) / outfit['photo']
            if old.exists() and str(old) != str(out_path):
                old.unlink()
        outfit['photo'] = rel_path
        outfit['avai_url'] = image_url  # store for future i2i variants
        # Bump per-asset version so frontend cache-buster `?v=N` flips and the
        # browser actually re-fetches. Without this, even after AVAI returns
        # a new image, the local file path stays the same and browser shows
        # the cached old image.
        outfit['image_version'] = int(time.time())
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url, 'image_version': outfit['image_version']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/series/<sid>/characters/<char_id>/outfits/<outfit_id>/save-frame/<project_id>', methods=['POST'])
def save_outfit_frame(sid, char_id, outfit_id, project_id):
    s = load_series(sid)
    char = get_char(s, char_id)
    if not char:
        return jsonify({'error': 'not found'}), 404
    outfit = get_outfit(char, outfit_id)
    if not outfit:
        return jsonify({'error': 'outfit not found'}), 404

    hdrs = rtl_headers()
    status_resp = requests.get(f'{RETELLER_API}/projects/{project_id}', headers=hdrs, timeout=15)
    if not status_resp.ok:
        return jsonify({'error': status_resp.text}), 500
    if status_resp.json().get('status') != 'completed':
        return jsonify({'ready': False, 'status': status_resp.json().get('status')})

    frames_resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}/assets/list?types=frames',
        headers=hdrs, timeout=15,
    )
    if not frames_resp.ok:
        return jsonify({'error': frames_resp.text}), 500

    frames = [a for a in frames_resp.json().get('assets', []) if a['type'] == 'frames']
    if not frames:
        return jsonify({'ready': False, 'status': 'no_frames'})

    img_resp = requests.get(frames[0]['url'], timeout=30)
    if not img_resp.ok:
        return jsonify({'error': 'download failed'}), 500

    char_slug = slugify(char['name'])
    out_dir = assets_dir(sid) / 'characters' / char_slug / 'outfits'
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_name(char["name"], outfit.get("label", outfit_id))}.jpg'  # e.g. CLAIRE_WORK_BLAZER.jpg
    (out_dir / filename).write_bytes(img_resp.content)

    rel_path = f'assets/characters/{char_slug}/outfits/{filename}'
    # Remove old generated photo if exists
    if outfit.get('photo'):
        old = series_path(sid) / outfit['photo']
        if old.exists():
            old.unlink()
    outfit['photo'] = rel_path
    save_series(sid, s)

    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}'})


from sw.anthro import (
    _ANIMAL_SPECIES,
    _AMBIGUOUS_APPEARANCE_SPECIES,
    _SKIN_FEATURE_CONTEXT_WORDS,
    _is_skin_feature_context,
    _NONANATOMICAL_MARKER_CONTEXT,
    _is_nonanatomical_marker_context,
    _detect_animal_species_raw,
    AnthroPermissionRequired,
    _anthro_unlocked,
    _anthro_decided,
    _detect_animal_species,
    _anthro_preflight,
    _ANTHRO_WORLD_KEYWORDS,
    _detect_anthro_world_raw,
    _is_anthro_world,
    _anthro_world_block,
    _casting_aesthetics_block,
    _revision_instructions_block,
    _llm_apply_revisions_to_bible,
    _llm_infer_species_for_char,
    _patch_appearance_with_species,
    _SCRIPT_POSE_PATTERNS,
    _PREP_EN_TO_RU,
    _detect_script_pose_for_char,
    _override_vision_with_script_poses,
)
