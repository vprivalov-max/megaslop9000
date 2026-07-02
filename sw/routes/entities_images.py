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
from sw.routes.entities_crud import _anthro_preflight, _detect_animal_species, _series_style_clause

# ── Generate character image via Reteller/Banana ─────────────────────────────

@app.route('/api/series/<sid>/characters/<char_id>/generate-image', methods=['POST'])
def generate_character_image(sid, char_id):
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404

    _nd_anthro, _fl_anthro = _anthro_preflight(s, [char])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    style_clause = _series_style_clause(s)
    era_clause = _series_era_hint(s)
    _appearance = char.get('appearance', '')
    _desc = char.get('description', '')
    # Species-aware framing — see _detect_animal_species docstring for context.
    species_hint = _detect_animal_species(char.get('name'), _appearance, series=s)
    # Self-heal for anthro worlds: if the SERIES is anthropomorphic but THIS
    # character has no species in name/appearance, infer the species from the
    # series synopsis (one short LLM call) and patch the appearance so future
    # generations stay consistent. Fixes the bug where Sofia/Marcus/Anita
    # rendered as humans in a furry world because the cast extractor dropped
    # species words from their appearance text.
    if not species_hint and _is_anthro_world(s):
        inferred = _llm_infer_species_for_char(s, char)
        if inferred and inferred != 'human':
            patched = _patch_appearance_with_species(_appearance, inferred, char.get('gender', ''))
            if patched and patched != _appearance:
                char['appearance'] = patched
                _appearance = patched
                save_series(sid, s)
                print(f'[anthro-heal] char {char.get("name")} → species={inferred}; appearance patched', flush=True)
            species_hint = _detect_animal_species(char.get('name'), _appearance, series=s)
    if species_hint:
        gender_word = 'female' if char.get('gender') == 'female' else 'male'
        kind_label = f', a {gender_word} {species_hint}'
        species_override = (
            f" CRITICAL: {char['name']} is an ANTHROPOMORPHIC {species_hint.split()[-1].upper()}, "
            f"NOT a human. The character has a {species_hint.split()[-1]}'s head/face "
            f"(realistic snout, ears, eyes typical of the species) with appropriate fur/feathers/scales, "
            f"walking upright with anthropomorphic body proportions, wearing human-style clothing. "
            f"Zootopia/Pixar-style anthropomorphic animal — DO NOT render as a plain human. "
            f"Ignore any wording like «a man» / «a woman» in the description below — those describe "
            f"the character's gender role, not human anatomy."
        )
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
        species_override = ''
    prompt = (
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{_appearance}. {_desc}.{species_override} "
        f"{era_clause + ' ' if era_clause else ''}"
        f"{_clothing_clause(_appearance, _desc, era_hint=era_clause)}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. "
        f"Soft even diffused illumination on the character only (lighting source NOT visible in frame), no harsh shadows on face or body, no visible lights or equipment. "
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    out_path = char_dir / f'{asset_name(char["name"], "BASE")}.jpg'

    try:
        image_url = avai_generate(prompt, out_path, preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = char.setdefault('ref_images', [])
        # Replace or prepend
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        # Store remote URL for i2i outfit variants later
        char['avai_base_url'] = image_url
        char['updated_at'] = int(time.time())   # cache-bust signal for frontend URLs
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/characters/<char_id>/regenerate', methods=['POST'])
def regenerate_character(sid, char_id):
    """Regenerate the character's base portrait with user-supplied constraints
    ("без шрама", "глаза карие" и т.п.), then optionally re-roll all outfits
    using the new base as i2i reference. Persists the constraints on the char
    so future generations honor them too."""
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'character not found'}), 404

    _nd_anthro, _fl_anthro = _anthro_preflight(s, [char])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    regen_outfits = bool(body.get('regenerate_outfits', True))
    # When true: rewrite appearance via Claude before generating (breaks
    # out of the «same prompt → same result» loop when appearance is stale).
    rewrite_appearance = bool(body.get('rewrite_appearance', False))
    # Per-call provider override (UI dropdown on the character lightbox).
    # Empty → fall back to series-level preference. Accepts: '', 'banana',
    # 'seedream', 'openai' (gpt-image-1 via AVAI). Used for BOTH base
    # portrait and outfit regeneration on this call so the look stays
    # consistent. Per-series default unchanged on this path.
    provider_override = (body.get('provider') or '').strip().lower()
    if provider_override not in ('', 'banana', 'seedream', 'openai'):
        provider_override = ''
    effective_provider = provider_override or _series_image_provider(s)

    # AUTO-rewrite when name implies an anthropomorphic animal AND appearance
    # text currently describes a plain human. Without this, the canonical
    # APPEARANCE field that gets fed into the Seedance binding later still
    # reads "A man holding interview papers" → Seedance keeps drawing a man
    # even when the ref portrait is now an anthropomorphic hyena. Bug case:
    # «The Fox CEO's Trap» — user clicked regenerate, got a Pixar man back.
    species_hint_pre = _detect_animal_species(char.get('name'), char.get('appearance'), series=s)
    # Self-heal for anthro worlds: char has no species but series IS anthro.
    # Infer species from synopsis, patch appearance, and proceed as if the
    # species had been there all along. Without this the regenerate flow
    # falls through to the human-rendering branch.
    if not species_hint_pre and _is_anthro_world(s):
        inferred = _llm_infer_species_for_char(s, char)
        if inferred and inferred != 'human':
            patched = _patch_appearance_with_species(char.get('appearance', ''), inferred, char.get('gender', ''))
            if patched and patched != char.get('appearance'):
                char['appearance'] = patched
                print(f'[anthro-heal] regenerate: char {char.get("name")} → species={inferred}', flush=True)
            species_hint_pre = _detect_animal_species(char.get('name'), char.get('appearance'), series=s)
    if species_hint_pre:
        appearance_raw_check = (char.get('appearance') or '').lower()
        species_word = species_hint_pre.split()[-1].lower()
        already_animal = (
            species_word in appearance_raw_check
            or 'anthropomorphic' in appearance_raw_check
            or any(w in appearance_raw_check for w in (
                'fur', 'muzzle', 'snout', 'tail', 'paws', 'whiskers', 'mane',
                'fang', 'fangs', 'feathers', 'beak'))
        )
        looks_human = any(w in appearance_raw_check for w in (
            ' man ', ' man.', ' man,', ' woman ', ' woman.', ' woman,',
            ' boy ', ' boy.', ' boy,', ' girl ', ' girl.', ' girl,',
            'a man', 'a woman', 'a boy', 'a girl',
            'young man', 'young woman', 'beautiful woman', 'businessman',
        ))
        if looks_human and not already_animal:
            rewrite_appearance = True
            # Stuff the species hint into wishes so the rewriter knows what
            # to make. Preserve user-provided wishes too.
            extra = (f"Character is an ANTHROPOMORPHIC {species_word.upper()} — "
                     f"rewrite appearance with {species_word} features (snout, ears, "
                     f"fur color/pattern, body type), NOT a human. Keep human-style "
                     f"clothing and the named props/gender role.")
            wishes = (wishes + ' ' + extra).strip() if wishes else extra

    # Persist constraints onto the character so future inline generations
    # also respect them. Empty wishes => clear them.
    char['image_constraints'] = wishes

    # ── PROMPT CLEANUP — fix the «приходит constraint но в appearance уже
    # сидит конфликтующая фраза» class of bugs.
    #
    # Real case (Mia Chen): appearance = "Young fashion blogger with multiple
    # monitors", constraints = "Убери мониторы". Image model sees both
    # «multiple monitors» (positive) and «убери мониторы» (negative). Negatives
    # are weak signal — model renders monitors regardless. After 3-4 regen
    # attempts user gives up.
    #
    # When the user passes wishes/constraints, run a fast Claude pass to:
    # 1. Detect if `appearance` contains scene/context-words that conflict
    #    with constraints (props/locations/objects, not person looks).
    # 2. Rewrite appearance to person-only (face, hair, build, vibe, age,
    #    typical clothing if relevant). Persist the cleaned version.
    # 3. Use the cleaned appearance in the generation prompt.
    appearance_raw = (char.get('appearance') or '').strip()
    appearance_for_prompt = appearance_raw

    # ── Rewrite appearance from scratch via Claude (breaks same-prompt loop) ──
    # Triggered when user clicks "Перегенерить с новым описанием" or when the
    # appearance text is clearly scene-specific (emotional states, actions, etc.)
    # and the user wants a completely fresh canonical visual description.
    if rewrite_appearance:
        desc_source = char.get('description') or ''
        try:
            rewritten = claude_ask_fast(
                f"CHARACTER NAME: {char.get('name', '')}\n"
                f"CHARACTER DESCRIPTION (story role, personality): {desc_source}\n"
                f"CURRENT APPEARANCE TEXT: {appearance_raw}\n"
                + (f"USER CONSTRAINTS: {wishes}\n" if wishes else '')
                + "\nTask: write a fresh canonical APPEARANCE field for image generation. "
                "Rules: (a) physical traits ONLY — age, build, hair color/length/style, "
                "eye color, face shape, distinguishing features, typical clothing; "
                "(b) NO scene context, NO emotions, NO actions, NO props; "
                "(c) concrete and specific — 'shoulder-length auburn hair' not 'beautiful hair'; "
                "(d) 1-3 short sentences, comma-separated descriptors, NO 'she is' opener; "
                "(e) MODERATION-SAFE — describe an attractive person with NEUTRAL words "
                "(elegant, graceful, soft features, slim) but NEVER use sexual / explicit / "
                "nudity wording (no 'sexy', 'sensual', 'seductive', 'cleavage', 'nude', "
                "'lingerie', 'sexual', «сексуальная», «чувственные», «голая», «декольте» etc.). "
                "If the user constraints ask for something sexual or nude, IGNORE that for this "
                "field — it only affects the rendered image, never the stored description.\n"
                "Output: the appearance text only, no quotes, no preamble.",
                system="You write character appearance descriptions for image generation. Plain text only.",
            ).strip().strip('"\'`')
            if rewritten and len(rewritten) > 10:
                # Hard scrub: the LLM is instructed to stay clean, but never trust
                # it — the persisted field rides into every future prompt. The
                # render still gets the raw wish via constraints_clause below.
                rewritten = _sanitize_appearance_for_moderation(rewritten)
                appearance_for_prompt = rewritten
                char['appearance'] = rewritten
                _log_event('INFO', 'appearance_rewritten',
                           char_id=char_id, name=char.get('name', ''),
                           before=appearance_raw[:200], after=rewritten[:200])
        except Exception as e:
            _log_event('WARN', 'appearance_rewrite_failed', char_id=char_id, err=str(e)[:200])

    elif wishes and appearance_raw:
        # ── Cleanup: remove scene-context from appearance that conflicts with wishes ──
        try:
            cleaned = claude_ask_fast(
                f"CHARACTER APPEARANCE FIELD: {appearance_raw}\n"
                f"USER CONSTRAINTS FOR IMAGE GENERATION: {wishes}\n\n"
                "Task: rewrite the appearance field so it (a) describes ONLY the "
                "person's physical traits (face, hair, build, age, characteristic "
                "clothing), NOT scene/context (props in background, locations, "
                "moods, activities); AND (b) does not contradict the user's "
                "constraints (if user said «убери мониторы», don't mention monitors).\n\n"
                "Output: 1-2 short sentences, plain text, no preamble, no quotes. "
                "Keep the language of the original appearance text.",
                system="You are a surgical text editor. Output the rewritten sentence(s) and nothing else.",
            ).strip()
            cleaned = cleaned.strip('"\'`')
            if cleaned and len(cleaned) < len(appearance_raw) * 2 and len(cleaned) > 5:
                cleaned = _sanitize_appearance_for_moderation(cleaned)
                appearance_for_prompt = cleaned
                char['appearance'] = cleaned
                _log_event('INFO', 'appearance_cleaned',
                           char_id=char_id, name=char.get('name', ''),
                           before=appearance_raw[:200], after=cleaned[:200],
                           wishes=wishes[:200])
        except Exception as e:
            _log_event('WARN', 'appearance_cleanup_failed',
                       char_id=char_id, err=str(e)[:200])

    # Whatever path produced appearance_for_prompt (rewrite, cleanup, or
    # untouched legacy text), guarantee both the prompt text AND the persisted
    # description are moderation-clean. The spicy wish still reaches the RENDER
    # via constraints_clause below — only the stored/BINDING text is scrubbed.
    appearance_for_prompt = _sanitize_appearance_for_moderation(appearance_for_prompt)
    if appearance_for_prompt and appearance_for_prompt != (char.get('appearance') or '').strip():
        char['appearance'] = appearance_for_prompt

    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    style_clause = _series_style_clause(s)
    era_clause = _series_era_hint(s)
    _desc = char.get('description', '')
    # Species-aware framing — same logic as generate_character_image. Without
    # this, regenerate_character would re-render a Wolf/Hyena/Fox as a human
    # because the hardcoded ", a man/woman" prefix overpowers any anthro hint.
    species_hint = _detect_animal_species(char.get('name'), appearance_for_prompt, series=s)
    if species_hint:
        gender_word = 'female' if char.get('gender') == 'female' else 'male'
        kind_label = f', a {gender_word} {species_hint}'
        species_override = (
            f" CRITICAL: {char['name']} is an ANTHROPOMORPHIC {species_hint.split()[-1].upper()}, "
            f"NOT a human. The character has a {species_hint.split()[-1]}'s head/face "
            f"(realistic snout, ears, eyes typical of the species) with appropriate fur/feathers/scales, "
            f"walking upright with anthropomorphic body proportions, wearing human-style clothing. "
            f"Zootopia/Pixar-style anthropomorphic animal — DO NOT render as a plain human. "
            f"Ignore any wording like «a man» / «a woman» in the description below — those describe "
            f"the character's gender role, not human anatomy."
        )
        clothing_fallback = ''
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
        species_override = ''
        clothing_fallback = _clothing_clause(appearance_for_prompt, _desc, era_hint=era_clause)
    prompt = (
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{appearance_for_prompt}. {_desc}.{constraints_clause}{species_override} "
        f"{era_clause + ' ' if era_clause else ''}"
        f"{clothing_fallback}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. "
        f"Soft even diffused illumination on the character only (lighting source NOT visible in frame), no harsh shadows on face or body, no visible lights or equipment. "
        f"{style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    # Append a short random token so each regeneration is a unique API request —
    # provider-level caching (Banana/Seedream deduplicate identical prompt strings)
    # was causing the same image to come back on every regen.
    variation_token = uuid.uuid4().hex[:8]
    prompt = f"{prompt} [v{variation_token}]"

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    out_path = char_dir / f'{asset_name(char["name"], "BASE")}.jpg'

    # 1) Regenerate base
    try:
        # Don't preemptively delete the old file — if avai_generate fails
        # (network blip, 401, content-filter), we'd have killed the user's
        # existing photo with no replacement, leaving ref_images pointing at
        # a vanished path. avai_generate will overwrite out_path anyway when
        # it succeeds. User-reported: "Подменил фотку, обновил страницу,
        # фото утеряно" was caused by this preemptive delete + AVAI failure.
        image_url = avai_generate(prompt, out_path, preferred_provider=effective_provider)
    except Exception as e:
        _log_event('WARN', 'regenerate_character_failed', char_id=char_id,
                   name=char.get('name', ''), err=str(e)[:300])
        return jsonify({'error': f'Не удалось сгенерировать основной образ: {e}'}), 500

    rel_path = str(out_path.relative_to(series_path(sid)))
    refs = char.setdefault('ref_images', [])
    # Replace any prior copy of this filename, then put fresh one first
    refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
    refs.insert(0, rel_path)
    char['avai_base_url'] = image_url
    # Bump updated_at so frontend URL cache-busters change → browser fetches
    # the new file instead of serving the prior generation from HTTP cache.
    # User-reported: regenerated portrait visible in lightbox but sidebar
    # miniature kept showing the old one because the URL stayed identical
    # (canonical filename never changes). All views compose `?v=<updated_at>`
    # so a fresh bump invalidates every cached copy in one shot.
    char['updated_at'] = int(time.time())

    # If any outfit was flagged is_base, point it at the new base photo too.
    for o in (char.get('outfits') or []):
        if o.get('is_base'):
            o['photo'] = rel_path
            o['avai_url'] = image_url

    save_series(sid, s)

    regenerated = []
    failed = []

    # 2) Regenerate outfits via i2i, one by one (sequential so each uses
    #    the canonical fresh base instead of fanning out and reusing stale state).
    if regen_outfits:
        new_base_url = char['avai_base_url']
        for outfit in (char.get('outfits') or []):
            if outfit.get('is_base'):
                continue  # already handled above
            try:
                out_constraints = f' IMPORTANT — strictly follow these constraints: {wishes}. ' if wishes else ''
                ref_prompt = (
                    f'Same {gender} as the reference image. '
                    f'Now wearing: {outfit["label"]}. {outfit.get("description", "")}. '
                    f'Same face, same hair, same body — only the clothing changes.'
                    f'{out_constraints}'
                    f'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
                    f'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
                    f'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
                    f'Soft even diffused illumination on the character only (no visible lights or equipment), no harsh shadows. Photorealistic, cinematic quality.'
                )
                ref_prompt = re.sub(r'\s+', ' ', ref_prompt).strip()
                # Provider-level cache buster (same fix as character base regen)
                ref_prompt = f"{ref_prompt} [v{uuid.uuid4().hex[:8]}]"
                outfit_dir = char_dir / 'outfits'
                outfit_dir.mkdir(parents=True, exist_ok=True)
                outfit_out = outfit_dir / f'{asset_name(char["name"], outfit["label"])}.jpg'
                # Remove old generated photo before regen so we don't leave orphans
                if outfit.get('photo'):
                    old_full = series_path(sid) / outfit['photo']
                    if old_full.exists() and old_full != outfit_out:
                        try: old_full.unlink()
                        except Exception: pass
                if outfit_out.exists():
                    try: outfit_out.unlink()
                    except Exception: pass
                outfit_url = avai_generate(ref_prompt, outfit_out, reference_url=new_base_url, preferred_provider=effective_provider)
                outfit['photo'] = str(outfit_out.relative_to(series_path(sid)))
                outfit['avai_url'] = outfit_url
                outfit['image_version'] = int(time.time())
                regenerated.append(outfit.get('label') or outfit['id'])
                save_series(sid, s)  # save progressively so partial work isn't lost
            except Exception as e:
                failed.append(outfit.get('label') or outfit['id'])
                app.logger.warning(f'regenerate outfit failed for {outfit.get("label")}: {e}')

    save_series(sid, s)
    return jsonify({
        'ready': True,
        'base_url': f'/assets/{sid}/{rel_path}',
        'image_url': char['avai_base_url'],
        'regenerated_outfits': regenerated,
        'failed_outfits': failed,
        'image_constraints': char['image_constraints'],
    })


@app.route('/api/series/<sid>/characters/<char_id>/save-frame/<project_id>', methods=['POST'])
def save_character_frame(sid, char_id, project_id):
    """Download first generated frame from reteller project and save as char ref."""
    s = load_series(sid)
    char = next((c for c in s['characters'] if c['id'] == char_id), None)
    if not char:
        return jsonify({'error': 'not found'}), 404

    hdrs = rtl_headers()

    # Check project status first
    status_resp = requests.get(f'{RETELLER_API}/projects/{project_id}', headers=hdrs, timeout=15)
    if not status_resp.ok:
        return jsonify({'error': status_resp.text}), status_resp.status_code
    status_data = status_resp.json()
    if status_data.get('status') != 'completed':
        return jsonify({'ready': False, 'status': status_data.get('status')})

    # Get frames list
    frames_resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}/assets/list?types=frames',
        headers=hdrs, timeout=15
    )
    if not frames_resp.ok:
        return jsonify({'error': frames_resp.text}), 500

    frame_assets = [a for a in frames_resp.json().get('assets', []) if a['type'] == 'frames']
    if not frame_assets:
        return jsonify({'ready': False, 'status': 'no_frames'})
    img_resp = requests.get(frame_assets[0]['url'], timeout=30)
    if not img_resp.ok:
        return jsonify({'error': 'download failed'}), 500

    char_slug = slugify(char['name'])
    char_dir = assets_dir(sid) / 'characters' / char_slug
    char_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_name(char["name"], "BASE")}.jpg'  # e.g. CLAIRE_BASE.jpg
    (char_dir / filename).write_bytes(img_resp.content)

    rel_path = f'assets/characters/{char_slug}/{filename}'
    char.setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)

    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}'})
