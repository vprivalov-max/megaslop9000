"""Series canon endpoints + asset prompt generation."""
from flask import jsonify, request

from sw.core import app
from sw.era import _no_caption_text_clause, _strip_cast_names_for_visual
from sw.storage import _empty_canon, list_episodes, load_canon, load_series, save_canon, series_file
from sw.story_logic import extract_canon_updates

# ── Series Canon endpoints ──────────────────────────────────────────────────
@app.route('/api/series/<sid>/canon', methods=['GET'])
def get_canon(sid):
    if not series_file(sid).exists(): return jsonify({'error': 'not found'}), 404
    return jsonify(load_canon(sid))

@app.route('/api/series/<sid>/canon/rebuild', methods=['POST'])
def rebuild_canon(sid):
    """Wipe canon and re-extract from all existing scripts in order. Fully automated."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    save_canon(sid, _empty_canon())
    eps = sorted(list_episodes(sid), key=lambda e: e['number'])
    results = []
    for ep in eps:
        if not (ep.get('script') or '').strip():
            continue
        try:
            r = extract_canon_updates(sid, ep['number'], ep['script'])
        except Exception as e:
            r = {'error': str(e)}
        results.append({'ep': ep['number'], **(r if isinstance(r, dict) else {'ok': False})})
    return jsonify({'ok': True, 'episodes_processed': results, 'canon': load_canon(sid)})


# ── Generate asset prompt ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/generate-prompt', methods=['POST'])
def generate_asset_prompt(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    asset_type = data['type']
    asset_id = data['id']
    style = s['style'].get('type', 'cinematic')
    tone = s.get('tone', '')
    genre = s.get('genre', '')
    world = s.get('world_description', '')

    if asset_type == 'character':
        char = next((c for c in s['characters'] if c['id'] == asset_id), None)
        if not char:
            return jsonify({'error': 'character not found'}), 404
        gender_word = 'woman' if char.get('gender') == 'female' else 'man'
        appearance = char.get('appearance', '')
        description = char.get('description', '')
        prompt = (
            f"Character reference sheet. {char['name']}, a {gender_word}. "
            f"{appearance}. {description}. "
            f"Full body, front-facing neutral pose, arms relaxed at sides. "
            f"Isolated on solid uniform light gray background (#E0E0E0). "
            f"Soft even diffused illumination on the character only (no visible lights or studio equipment), no harsh shadows. Sharp focus. "
            f"Photorealistic, professional character design reference. "
            f"No background objects or gradients. Clean, simple, reference-quality."
        )

    elif asset_type == 'outfit':
        char_id = data.get('char_id')
        char = next((c for c in s['characters'] if c['id'] == char_id), None)
        if not char:
            return jsonify({'error': 'character not found'}), 404
        outfit = next((o for o in char.get('outfits', []) if o['id'] == asset_id), None)
        if not outfit:
            return jsonify({'error': 'outfit not found'}), 404
        gender_word = 'woman' if char.get('gender') == 'female' else 'man'
        appearance = char.get('appearance', '')
        outfit_desc = outfit.get('description', '')
        prompt = (
            f"Same character — {char['name']}, a {gender_word}. {appearance}. "
            f"Now wearing/posed: {outfit_desc}. "
            f"Full body, front-facing neutral pose unless the outfit description specifies otherwise. "
            f"Isolated on solid uniform light gray background (#E0E0E0). "
            f"Soft even diffused illumination on the character only (no visible lights or studio equipment), no harsh shadows. Sharp focus. "
            f"Photorealistic, professional character reference. "
            f"Identical face and body to the base reference — change ONLY the clothing/pose described above. "
            f"No background objects or gradients. Clean, simple, reference-quality."
        )

    elif asset_type == 'location':
        loc = next((l for l in s.get('locations', []) if l['id'] == asset_id), None)
        if not loc:
            return jsonify({'error': 'location not found'}), 404
        description = _strip_cast_names_for_visual(loc.get('description', ''), s)
        loc_name = _strip_cast_names_for_visual(loc['name'], s)
        context = ' '.join(filter(None, [genre, tone, world]))
        prompt = (
            f"{loc_name}. {description}. "
            f"Empty scene, no people present. "
            f"{tone + ' atmosphere. ' if tone else ''}"
            f"{style.capitalize()} visual style. "
            f"Cinematic wide establishing shot. "
            f"Photorealistic, high detail, professional cinematography. "
            f"{('World context: ' + world[:100] + '. ') if world else ''}"
            f"Atmospheric lighting, sharp focus.{_no_caption_text_clause()}"
        )
    else:
        return jsonify({'error': 'unknown type'}), 400

    # Clean up double spaces
    import re
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    return jsonify({'prompt': prompt})
