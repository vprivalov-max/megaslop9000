"""Import-series flow: script splitting, per-episode entity extraction worker,
batch script generation, append-from-script, create/clone series, adapt and
phrase-check, series get/update/era/anthro/style-samples/delete."""
import copy
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

import requests
from flask import abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from sw.anthro import (_ANIMAL_SPECIES, _anthro_preflight, _anthro_world_block,
                       _casting_aesthetics_block, _detect_anthro_world_raw,
                       _is_anthro_world, _llm_apply_revisions_to_bible,
                       _revision_instructions_block)
from sw.auth import AUTH_ENABLED, PRIMARY_USER_EMAIL, _spawn_with_keys, current_user_email
from sw.avai import _avai_call, allowed_file
from sw.canon_index import (_build_plot_device_history,
                            _extract_devices_from_script,
                            _extract_narrative_state_from_script,
                            _update_devices_index, _update_narrative_index)
from sw.config import BASE, DATA_ROOT
from sw.core import app
from sw.era import _ERA_GUIDES, _ERA_LABELS, _detect_series_era
from sw.jsonutils import loads_lenient, strip_json
from sw.llm import (WRITER_MODEL_DEFAULT, WRITER_MODEL_WHITELIST,
                    _resolve_writer_model, claude_ask, llm_ask)
from sw.logging_utils import _log_event
from sw.autogen import auto_generate_missing_assets, trigger_autogen_if_enabled
from sw.routes.ideas import (_modal_setting_to_era_choice, _resolve_beats,
                             _series_beats_episode_block,
                             _source_outline_episode_block)
from sw.scriptparse import (_IMPORT_LOCKS, _IMPORT_STATUS,
                            _detect_dialogue_language)
from sw.seedance import _seedance_chunks
from sw.storage import (_extract_end_position, _normalize_blocking_tags,
                        _sync_script_outfits, assets_dir, list_episodes,
                        load_episode, load_series, save_episode, save_series,
                        scaffold_series_folders, series_path, user_root)
from sw.story_logic import (_build_narrative_state_block,
                            _script_runtime_metrics, audit_script,
                            build_logic_brief, extract_canon_updates,
                            rollback_canon_for_episode)
from sw.story_prompts import _build_cast_block
from sw.story_writer import _build_script_system
from sw.style import _VISUAL_STYLE_PRESETS
from sw.textrules_moderation import (_PHRASE_CHECK_SYSTEM,
                                     _lexical_moderation_scan,
                                     _merge_moderation_warnings)
from sw.textrules_sanitizer import _sanitize_appearance_for_moderation
from sw.utils import slugify
from sw.vision import _backfill_uploaded_char_appearances

@app.route('/api/series/<sid>', methods=['GET'])
def get_series(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    # ── Cache-buster ground truth: derive image_version from file mtime ──
    # Real production bug: user clicks «Перегенерировать локацию», server
    # overwrites the JPG in place under the SAME path, but the asset URL
    # `/assets/<sid>/<rel_path>` is byte-identical → browser serves the old
    # bytes from its 1-year cache. The asset serve handler explicitly relies
    # on the frontend appending `?v=<ts>` to bypass cache (see serve_asset's
    # Cache-Control header). We compute that `?v` from the FILE'S actual
    # mtime so any disk-level change forces a fresh fetch — works on every
    # ref kind regardless of whether the regenerate endpoint wrote a
    # persisted version field. Cheap: one stat per ref image per GET.
    def _stat_mtime(p):
        try:
            return int(p.stat().st_mtime)
        except Exception:
            return 0
    sp = series_path(sid)
    for kind_key in ('locations', 'characters', 'items'):
        for ent in (s.get(kind_key) or []):
            refs = ent.get('ref_images') or []
            if not refs:
                continue
            primary = sp / refs[0]
            mtime = _stat_mtime(primary)
            if mtime:
                ent['image_version'] = max(int(ent.get('image_version') or 0), mtime)
    # Auto-heal: default auto_generate_assets to True for legacy series, sync stale episode refs.
    healed = False
    if 'auto_generate_assets' not in s:
        s['auto_generate_assets'] = True
        healed = True
    if 'checkpoints' not in s or not isinstance(s.get('checkpoints'), list):
        s['checkpoints'] = []
        healed = True
    if 'finale' not in s:
        s['finale'] = None
        healed = True
    # Heal mis-gendered characters — STRICT version. Only flips when:
    #   1) appearance text embeds wrong sex tag ("young man" vs "young woman"),
    #   2) inference comes from speaker-attribution lines ("she said", "he replied")
    #      DIRECTLY tied to this character — NOT bare pronouns in surrounding text
    #      (which leak from other characters in the same scene),
    #   3) signal is unambiguous (≥4 attributed hits, ratio >3:1 against the embed).
    # Earlier loose version flipped MARCUS to female because pronouns near his
    # name were mostly Lydia's ("she lifted her hand to Marcus's chest").
    try:
        all_scripts = '\n'.join((ep.get('script') or '') for ep in list_episodes(sid))
        for c in (s.get('characters') or []):
            app_lower = (c.get('appearance') or '').lower()
            embeds_male   = 'young man'   in app_lower
            embeds_female = 'young woman' in app_lower
            if not (embeds_male or embeds_female):
                continue
            name = (c.get('name') or '').strip()
            if not name:
                continue
            # Speaker-attributed pronouns ONLY: '<Name>, she said', '<Name>, he replied'.
            # This is far more reliable than "pronouns near the name in any context".
            attrib_re = re.compile(
                r'\b' + re.escape(name) + r'\b[^\.\n]{0,40}\b(he|she|он|она)\b',
                re.IGNORECASE,
            )
            he_attrib = 0
            she_attrib = 0
            for m in attrib_re.finditer(all_scripts):
                tok = m.group(1).lower()
                if tok in ('he', 'он'):
                    he_attrib += 1
                else:
                    she_attrib += 1
            confident_female = she_attrib >= 4 and she_attrib > he_attrib * 3
            confident_male   = he_attrib  >= 4 and he_attrib  > she_attrib * 3
            should_flip = (confident_female and embeds_male) or (confident_male and embeds_female)
            if not should_flip:
                continue
            new_gender = 'female' if confident_female else 'male'
            old_gender = c.get('gender')
            c['gender'] = new_gender
            if new_gender == 'female':
                c['appearance'] = re.sub(r'young man\b', 'young woman', c.get('appearance') or '', flags=re.IGNORECASE)
            else:
                c['appearance'] = re.sub(r'young woman\b', 'young man', c.get('appearance') or '', flags=re.IGNORECASE)
            c['ref_images'] = []
            c.pop('avai_base_url', None)
            for o in (c.get('outfits') or []):
                o['photo'] = ''
                o.pop('avai_url', None)
            print(f'[heal {sid}] flipped {name}: gender {old_gender}→{new_gender} '
                  f'(she-attrib={she_attrib}, he-attrib={he_attrib}), portraits cleared', flush=True)
            healed = True
    except Exception as e:
        print(f'[heal {sid}] gender-heal failed: {e}', flush=True)
    if healed:
        save_series(sid, s)
        # If we cleared portraits (gender heal flipped a character), trigger
        # autogen so the user doesn't have to remember to click "Сгенерить".
        try:
            trigger_autogen_if_enabled(sid)
        except Exception as e:
            print(f'[heal {sid}] autogen trigger after heal failed: {e}', flush=True)
    # Heal episode refs against current series state (idempotent, cheap).
    # Catches the failure mode where chars/outfits in scripts aren't reflected in series.json.
    # Skip episodes where the user hasn't yet clicked "Извлечь персонажей и локации"
    # — auto-creating chars/outfits/locations behind their back is what we're trying to avoid.
    try:
        for ep in list_episodes(sid):
            if not (ep.get('script') or '').strip():
                continue
            # Default True for legacy episodes (they were already extracted before this gate).
            if ep.get('cast_extracted', True) is False:
                continue
            # lazy: lives in sw.routes.scripts (same pattern as sw.autogen)
            from sw.routes.scripts import sync_episode_with_cast_block
            sync_episode_with_cast_block(sid, ep['number'])
    except Exception as e:
        print(f'[get_series {sid}] heal failed: {e}')
    # Re-load post-heal so client gets the fresh state
    s = load_series(sid)
    # No self-heal autogen kick here. Triggering generation as a side-effect of
    # opening a series page surprised users (work started without a click) and
    # racing modals (script-accept flow couldn't show «Не генерить» options
    # because the sweep was already running). User explicitly drives autogen
    # via the «🎨 Сгенерировать недостающее» button when they want it.
    #
    # Era-detection telemetry for UI — non-persisted, recomputed each GET.
    # If detection finds a non-modern era AND the user hasn't confirmed/picked
    # yet, the client shows a confirmation banner before any non-modern style
    # is applied to characters/outfits.
    try:
        _era_det, _era_kw = _detect_series_era(s)
        s['_era_detected'] = _era_det or ''
        s['_era_detected_keyword'] = _era_kw or ''
        s['_era_detected_label'] = _ERA_LABELS.get(_era_det or '', '')
        s['_era_choice'] = (s.get('era_choice') or 'auto')
        s['_era_confirmed'] = bool(s.get('era_confirmed'))
        s['_era_options'] = [{'key': k, 'label': v} for k, v in _ERA_LABELS.items()]
    except Exception as e:
        print(f'[get_series {sid}] era-telemetry failed: {e}', flush=True)
    # Anthro-detection telemetry — same gate pattern as era. If the detector
    # would flag this as an anthropomorphic-animal world (furry universe)
    # and the user hasn't confirmed, the UI shows a banner asking accept /
    # «это человеческий мир» before any species features are propagated to
    # secondary characters.
    try:
        _anthro_raw = _detect_anthro_world_raw(s)
        # Per-character pending: ANY character that trips the raw non-human
        # detector while the series is still undecided also surfaces the
        # banner (catches single-char misfires like «doe eyes» / «wolf
        # spirit» that the world-level detector alone would miss).
        _nd_pre, _fl_pre = _anthro_preflight(s, s.get('characters') or [])
        s['_anthro_detected'] = bool(_anthro_raw['anthro']) or bool(_nd_pre)
        s['_anthro_flagged_chars'] = _fl_pre
        _ev = list(_anthro_raw['evidence'] or [])
        if _nd_pre and _fl_pre:
            _ev.append('возможные не-люди: ' + ', '.join(_fl_pre))
        s['_anthro_evidence'] = _ev
        s['_anthro_choice'] = (s.get('anthro_choice') or 'auto')
        s['_anthro_confirmed'] = bool(s.get('anthro_confirmed'))
    except Exception as e:
        print(f'[get_series {sid}] anthro-telemetry failed: {e}', flush=True)
    return jsonify(s)

@app.route('/api/series/<sid>', methods=['PUT'])
def update_series(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    data = request.json
    # Deep merge settings and style
    if 'settings' in data:
        s['settings'].update(data.pop('settings'))
    if 'style' in data:
        s['style'].update(data.pop('style'))
        # Sync visual_style with style preset choice — this is what gen prompts
        # actually read. Without this sync, picking "Кинематограф" in the modal
        # changed style.type but generation still used the default look.
        st = s['style']
        t = (st.get('type') or '').strip()
        if t == 'custom':
            cd = (st.get('custom_description') or '').strip()
            if cd:
                s['visual_style'] = cd
        elif t in _VISUAL_STYLE_PRESETS:
            s['visual_style'] = _VISUAL_STYLE_PRESETS[t]['desc']  # may be '' for 'auto'
    s.update(data)
    save_series(sid, s)
    return jsonify(s)


@app.route('/api/series/<sid>/era', methods=['POST'])
def set_series_era(sid):
    """Set the user's choice for the historical/genre era used during asset
    generation. Body: {choice: 'modern'|'auto'|<era_key>, clear_portraits?: bool}.

    Behavior:
      • Stores `era_choice` + `era_confirmed=True` on the series so that
        `_series_era_hint` returns the correct guide (or '' for modern).
      • When `clear_portraits` is true (default false), wipes existing
        character/outfit images so the user can regenerate them with the new
        era applied — useful when the prior auto-detect picked the wrong era
        and the user wants to retake the photos."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    choice = (body.get('choice') or '').strip().lower()
    # Validate: only allow 'auto', 'modern', 'none', or a known era key
    valid = {'auto', 'modern', 'none'} | set(_ERA_GUIDES.keys())
    if choice not in valid:
        return jsonify({'error': f'invalid choice; must be one of {sorted(valid)}'}), 400
    s['era_choice'] = choice
    # 'auto' explicitly means "let the detector pick" — only counts as
    # confirmed when the user actually picks a specific value (otherwise the
    # UI banner would never go away).
    s['era_confirmed'] = (choice != 'auto')
    cleared = []
    if body.get('clear_portraits'):
        sp = series_path(sid)
        for c in (s.get('characters') or []):
            for rel in (c.get('ref_images') or []):
                try:
                    (sp / rel).unlink(missing_ok=True)
                except Exception:
                    pass
            c['ref_images'] = []
            c.pop('avai_base_url', None)
            c['image_version'] = int(time.time())
            for o in (c.get('outfits') or []):
                if o.get('photo'):
                    try:
                        (sp / o['photo']).unlink(missing_ok=True)
                    except Exception:
                        pass
                o['photo'] = ''
                o.pop('avai_url', None)
            cleared.append(c.get('name') or c.get('id'))
    save_series(sid, s)
    return jsonify({
        'ok': True,
        'era_choice': s['era_choice'],
        'era_confirmed': s['era_confirmed'],
        'cleared_portraits': cleared,
    })


@app.route('/api/series/<sid>/anthro', methods=['POST'])
def set_series_anthro(sid):
    """Set the user's choice for anthropomorphic-animal world.
    Body: {choice: 'human'|'anthro'|'auto', strip_species?: bool, clear_portraits?: bool}.

    When `strip_species` is true (default true for choice='human'), removes
    «anthropomorphic <species>», animal-anatomy markers and species-bearing
    name tokens from character.appearance — fixes the case where a cast
    extractor incorrectly tagged human characters as furries.

    When `clear_portraits` is true, also wipes character ref images so the
    next autogen produces fresh portraits under the corrected world."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    choice = (body.get('choice') or '').strip().lower()
    if choice not in ('auto', 'human', 'anthro', 'none'):
        return jsonify({'error': 'choice must be one of: auto, human, anthro'}), 400
    if choice == 'none':
        choice = 'human'
    s['anthro_choice'] = choice
    s['anthro_confirmed'] = (choice != 'auto')
    stripped = []
    cleared_portraits = []
    if choice == 'human' and body.get('strip_species', True):
        sp = series_path(sid)
        # Pass 1: kill long phrases like "anthropomorphic deer female".
        anthro_phrase_re = re.compile(
            r'\b(?:anthropomorphic|anthro|furry)\s+\w+(?:\s+(?:female|male))?\b',
            re.IGNORECASE)
        # Pass 2: kill bare anatomy markers (the ones _detect_animal_species
        # uses as a fallback signal, plus compound forms).
        anatomy_words = (
            'fur', 'fur-tied', 'thick-furred', 'furred',
            'muzzle', 'snout', 'whiskers',
            'paws', 'claws', 'fang', 'fangs',
            'antler', 'antlers', 'mane', 'tail', 'tails',
            'feathers', 'beak', 'scales', 'tusks',
        )
        anatomy_re = re.compile(
            r'\b(?:' + '|'.join(re.escape(w) for w in anatomy_words) + r')\b',
            re.IGNORECASE)
        # Pass 3: kill bare species words ("deer", "wolf", etc.).
        species_word_re = re.compile(
            r'\b(?:' + '|'.join(re.escape(sp_kw) for sp_kw in _ANIMAL_SPECIES.keys()) + r')\b',
            re.IGNORECASE)
        # Pass 4: sweep up the connective filler left behind ("with soft brown ,
        # delicate ,") so the cleaned string reads cleanly.
        sweep_filler_re = re.compile(
            r'\b(?:with|has)\s+(?:soft|sharp|short|long|tied|visible|delicate|gentle|thick)?\s*(?=[,\s])',
            re.IGNORECASE)
        for c in (s.get('characters') or []):
            orig = c.get('appearance') or ''
            new = anthro_phrase_re.sub('', orig)
            new = anatomy_re.sub('', new)
            new = species_word_re.sub('', new)
            new = sweep_filler_re.sub('', new)
            new = re.sub(r'\s*,\s*,+', ',', new)
            new = re.sub(r'\s{2,}', ' ', new)
            new = re.sub(r'^[\s,;:.]+|[\s,;:]+$', '', new)
            if not new:
                gender = (c.get('gender') or '').strip()
                new = ('Young woman' if gender == 'female' else 'Young man') + ', neutral appearance'
            if new != orig:
                c['appearance'] = new
                stripped.append(c.get('name') or c.get('id'))
            if body.get('clear_portraits'):
                for rel in (c.get('ref_images') or []):
                    try: (sp / rel).unlink(missing_ok=True)
                    except Exception: pass
                c['ref_images'] = []
                c.pop('avai_base_url', None)
                c['image_version'] = int(time.time())
                for o in (c.get('outfits') or []):
                    if o.get('photo'):
                        try: (sp / o['photo']).unlink(missing_ok=True)
                        except Exception: pass
                    o['photo'] = ''
                    o.pop('avai_url', None)
                cleared_portraits.append(c.get('name') or c.get('id'))
    save_series(sid, s)
    return jsonify({
        'ok': True,
        'anthro_choice': s['anthro_choice'],
        'anthro_confirmed': s['anthro_confirmed'],
        'stripped_appearances': stripped,
        'cleared_portraits': cleared_portraits,
    })


@app.route('/api/style-presets')
def style_presets():
    """Return the catalog of built-in visual styles for the picker UI."""
    return jsonify({
        'presets': [
            {'id': k, **v} for k, v in _VISUAL_STYLE_PRESETS.items()
        ]
    })


@app.route('/api/style-sample', methods=['POST'])
def style_sample():
    """Generate ONE sample image for a custom style description. UI shows it
    in the style-picker so the user can preview their custom desc before
    committing the whole series to that look. Cached by description hash so
    re-clicking on the same desc reuses the prior generation.

    Body: {description: str, base?: 'snoop'|'man'|'woman'} — base picks the
    canonical subject for the sample. Default = a generic young man portrait
    so the user sees how chars in their series will look."""
    body = request.get_json(silent=True) or {}
    desc = (body.get('description') or '').strip()
    if not desc:
        return jsonify({'error': 'description required'}), 400
    base = (body.get('base') or 'man').lower()
    base_subject = {
        'snoop':  'a Black male rapper in his 50s with long braids, gold chains, sunglasses, smoking pose',
        'man':    'a young man in his late 20s, neutral expression, photogenic features, casual shirt',
        'woman':  'a young woman in her late 20s, neutral expression, photogenic features, casual blouse',
    }.get(base, base)
    # Cache key — sha256(desc + base) so re-running the same prompt is free.
    import hashlib
    key = hashlib.sha256((desc + '|' + base).encode('utf-8')).hexdigest()[:16]
    # Persistent location — survives redeploys (DATA_ROOT is mounted volume).
    # `static/img/...` gets wiped on every `git pull` / image rebuild. New URL
    # is /style-samples-cache/<key>.jpg, served by serve_style_sample below.
    cache_dir = DATA_ROOT / '_global' / 'style-samples-cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f'{key}.jpg'
    if cache_path.exists():
        return jsonify({'url': f'/style-samples-cache/{key}.jpg', 'cached': True})
    # Build prompt with the requested style
    prompt = (
        f"{base_subject}. {desc}. Centered portrait composition, neutral background. "
        f"Square 1:1 framing."
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    try:
        avai_url = _avai_call('banana', prompt, aspect_ratio='1:1')
        # Download and save to cache
        import requests
        r = requests.get(avai_url, timeout=60)
        r.raise_for_status()
        cache_path.write_bytes(r.content)
        return jsonify({'url': f'/style-samples-cache/{key}.jpg', 'cached': False})
    except Exception as e:
        _log_event('WARN', 'style_sample_fail', desc=desc[:120], err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/regenerate-style-samples', methods=['POST'])
def regenerate_style_samples():
    """Primary-only one-shot: generates the baseline preset samples (cinematic,
    photorealistic, anime, pixar, noir) using a canonical subject so the style
    picker shows real previews. Saves to static/img/style-samples/<id>.jpg.
    Run once per deploy when AVAI prompts change. ~5 LLM calls × ~10s each."""
    actor = current_user_email() or ''
    if actor != PRIMARY_USER_EMAIL and AUTH_ENABLED:
        return jsonify({'error': 'admin only'}), 403
    # Persistent location — see notes on /api/style-sample above.
    out_dir = DATA_ROOT / '_global' / 'style-samples'
    out_dir.mkdir(parents=True, exist_ok=True)
    base_subject = 'a Black male rapper in his 50s with long braids, gold chains, sunglasses'
    results = []
    for preset_id, preset in _VISUAL_STYLE_PRESETS.items():
        if not preset.get('desc'):
            continue   # skip 'auto' — no fixed style
        prompt = (
            f"{base_subject}. {preset['desc']}. Centered portrait composition, "
            f"neutral background. Square 1:1 framing."
        )
        prompt = re.sub(r'\s+', ' ', prompt).strip()
        try:
            avai_url = _avai_call('banana', prompt, aspect_ratio='1:1')
            import requests
            r = requests.get(avai_url, timeout=60)
            r.raise_for_status()
            out_path = out_dir / f'{preset_id}.jpg'
            out_path.write_bytes(r.content)
            results.append({'id': preset_id, 'ok': True, 'path': str(out_path)})
        except Exception as e:
            results.append({'id': preset_id, 'ok': False, 'err': str(e)[:200]})
    return jsonify({'results': results})


@app.route('/style-samples/<path:filename>')
def serve_style_sample(filename):
    """Serve baseline preset samples from the persistent DATA_ROOT location.
    Falls back to the old static/img/style-samples/<filename> if a sample
    hasn't been migrated yet — keeps existing series working during the
    transition. The first time admin clicks 'Перегенерировать стили' all five
    baseline samples land in DATA_ROOT/_global/style-samples/ and stay there
    across deploys."""
    from flask import send_from_directory, abort
    persistent = DATA_ROOT / '_global' / 'style-samples'
    target = persistent / filename
    if target.exists():
        return send_from_directory(persistent, filename)
    legacy = BASE / 'static' / 'img' / 'style-samples'
    if (legacy / filename).exists():
        return send_from_directory(legacy, filename)
    abort(404)


@app.route('/style-samples-cache/<path:filename>')
def serve_style_sample_cache(filename):
    """Serve user-generated custom-style samples from the persistent cache."""
    from flask import send_from_directory, abort
    cache = DATA_ROOT / '_global' / 'style-samples-cache'
    if (cache / filename).exists():
        return send_from_directory(cache, filename)
    legacy = BASE / 'static' / 'img' / 'style-samples-cache'
    if (legacy / filename).exists():
        return send_from_directory(legacy, filename)
    abort(404)

def _rmtree_hard(path):
    """Permanently wipe a directory tree. Robust against AppleDouble (`._*`)
    metadata races on macOS external (exFAT/HFS+) drives where Python's
    shutil.rmtree silently leaves stragglers. Falls back to /bin/rm -rf which
    handles those cases atomically.

    Returns (ok: bool, msg: str).
    """
    p = Path(path)
    if not p.exists():
        return True, ''
    # First try shutil — fast path on clean drives.
    try:
        shutil.rmtree(p)
    except Exception:
        pass
    if not p.exists():
        return True, ''
    # Fallback: shell out to /bin/rm -rf. Handles AppleDouble + locked metadata.
    try:
        result = subprocess.run(
            ['/bin/rm', '-rf', '--', str(p)],
            capture_output=True, text=True, timeout=60
        )
        if p.exists():
            return False, (result.stderr or 'rm -rf не смог удалить папку').strip()
        return True, ''
    except Exception as e:
        return False, str(e)


@app.route('/api/series/<sid>', methods=['DELETE'])
def delete_series(sid):
    p = series_path(sid)
    ok, msg = _rmtree_hard(p)
    if not ok:
        return jsonify({'error': f'Не удалось удалить папку с диска: {msg}'}), 500
    return jsonify({'ok': True})

