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
from sw.routes.import_series_worker import _import_status, _import_worker, _split_script_into_episodes

@app.route('/api/series/import-from-script/preview', methods=['POST'])
def import_from_script_preview():
    """Returns the proposed episode breakdown for a pasted script WITHOUT
    creating anything. UI shows it as a confirmable preview. Cheap (regex-
    only, no LLM).

    Also runs `_detect_dialogue_language()` — if more than 15% of dialogue
    lines are non-English, returns a `dialogue_lang_warning` payload so
    the UI can offer to adapt the script to English before commit."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    eps = _split_script_into_episodes(script)
    lang_info = _detect_dialogue_language(script)
    payload = {
        'episodes': [
            {'number': e['number'], 'title': e['title'], 'preview': e['body'][:240], 'length': len(e['body'])}
            for e in eps
        ],
        'total_chars': len(script),
    }
    # 15% threshold — below that the few stray non-EN words are probably
    # quoted phrases or names, not the dominant dialogue language.
    if lang_info['ratio'] > 0.15 and lang_info['non_english_lines'] > 0:
        payload['dialogue_lang_warning'] = lang_info
    return jsonify(payload)


_TRANSLATE_DIALOGUES_SYSTEM = """You are a screenplay localization editor. \
Your only job: rewrite the dialogue lines in the user's script so the spoken \
text is natural conversational ENGLISH, while preserving everything else \
EXACTLY as it appears in the input.

WHAT TO TRANSLATE:
- Spoken dialogue body — the text AFTER `CHARACTER:` (or `CHARACTER (action):`).
  Make it natural spoken English. Preserve emotional tone and meaning. Keep
  the same approximate length (±20% words). Use contractions ("I'm", "don't")
  for realistic speech.

WHAT TO LEAVE UNTOUCHED, BYTE-FOR-BYTE:
- Episode/scene headers (e.g. `**СЕРИЯ 1 — "TITLE"**`, `=== ЭПИЗОД 5 ===`,
  `## EPISODE 1`, scene slug lines like `ИНТА. РЕСТОРАН — НОЧЬ`).
- Character name cues — leave the speaker label in original casing
  (e.g. `VICTORIA:`, `МАРКУС:` — DON'T transliterate Cyrillic names).
- Action lines / stage directions wrapped in `[...]`, `(...)`, or `*(...)*`.
  These may stay in their original language (per project convention).
- Blank lines, separators (`---`, `===`), markdown formatting (`**`, `*`).
- English dialogue lines that are ALREADY in English — output them unchanged.

OUTPUT FORMAT:
- Return ONLY the rewritten script. No preamble, no explanation, no code fence.
- Preserve line order exactly. Preserve line breaks. Same number of lines as input.

EXAMPLE:
Input:
  **СЕРИЯ 1 — "ПОТОЛОК"**
  *(awkward silence in the store)*
  VICTORIA *(nervous laugh)*: Подожди... нет, это шутка. Ты серьёзно?
  MARCUS: Это было до того, как я узнал.

Output:
  **СЕРИЯ 1 — "ПОТОЛОК"**
  *(awkward silence in the store)*
  VICTORIA *(nervous laugh)*: Wait... no, this has to be a joke. Are you serious?
  MARCUS: That was before I knew.
"""

@app.route('/api/series/import-from-script/translate-dialogues', methods=['POST'])
def import_from_script_translate_dialogues():
    """Single Claude pass that rewrites only the spoken-text portion of each
    dialogue line to natural English, leaving headers / action lines / scene
    slugs / character cues untouched. UI calls this when the user clicks
    «Адаптировать на английский» in the preview modal.

    Body: {script: str}
    Returns: {translated_script: str, lines_changed_estimate: int,
              before_ratio: float, after_ratio: float}
    """
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    if len(script) > 200000:
        # Sanity guard — gigantic paste would blow up Claude's context. We
        # could split-and-stitch but that's an iteration-2 feature; for now
        # tell the user to split manually.
        return jsonify({'error': 'script too long (>200k chars). Split into smaller batches and translate each.'}), 400

    before = _detect_dialogue_language(script)
    try:
        translated = claude_ask(
            script,
            system=_TRANSLATE_DIALOGUES_SYSTEM,
            model='sonnet',  # need translation quality, not haiku speed
            max_tokens=24000,
            timeout=600,
        ).strip()
    except Exception as e:
        _log_event('WARN', 'translate_dialogues_failed', err=str(e)[:300],
                   script_chars=len(script))
        return jsonify({'error': f'Не удалось адаптировать сценарий: {e}'}), 500

    # Strip accidental code-fence wrappers if the model still added them.
    if translated.startswith('```'):
        translated = re.sub(r'^```[a-zA-Z]*\n?', '', translated)
        translated = re.sub(r'\n?```\s*$', '', translated)
        translated = translated.strip()

    after = _detect_dialogue_language(translated)
    _log_event('INFO', 'translate_dialogues_ok',
               before_ratio=before['ratio'], after_ratio=after['ratio'],
               before_lang=before.get('detected_lang'),
               script_chars=len(script), translated_chars=len(translated))

    return jsonify({
        'translated_script': translated,
        'before_ratio': before['ratio'],
        'after_ratio':  after['ratio'],
        'before_lang':  before.get('detected_lang', 'en'),
        'lines_total':  before['dialogue_lines'],
        'lines_changed_estimate': before['non_english_lines'] - after['non_english_lines'],
    })


_ADAPT_TO_STANDARD_SYSTEM = (
    "You are a script formatter for short-form drama. Your task has THREE parts:\n\n"

    "PART 1 — POSITION BLOCKS\n"
    "Add POSITION BLOCKS to every scene:\n"
    "[BLOCKING] — insert immediately after EVERY scene heading (ИНТА./ЭКСТ. line):\n"
    "  [BLOCKING]\n"
    "  LOCATION: <English location name>\n"
    "  CHARACTER_NAME: <position in Russian> :: OUTFIT: <Outfit Name>\n"
    "  [/BLOCKING]\n\n"
    "[BLOCKING_END] — insert at the very end of each episode (absolute last thing):\n"
    "  [BLOCKING_END]\n"
    "  LOCATION: <English location name>\n"
    "  CHARACTER_NAME: <final position at cut — in Russian>\n"
    "  [/BLOCKING_END]\n\n"
    "Rules for position blocks:\n"
    "- [BLOCKING] lists ONLY characters PRESENT at scene START\n"
    "- OUTFIT FIELD = a short Title Case NAME of the outfit asset (NOT a clothing description). Examples: `Business Suit`, `Casual`, `Pajamas`, `Red Dress`, `School Uniform`, `Hospital Gown`, `Swimsuit`. The system uses this label to reuse the same outfit asset across scenes.\n"
    "- When the outfit NAME is new (not seen for this character before) add a description after a pipe: `OUTFIT: Pajamas | OUTFIT_DESC: light blue cotton pajamas, bare feet`. For names that were already introduced in a previous scene of this or earlier episode, OMIT `| OUTFIT_DESC:` — the system already has the description.\n"
    "- DEDUP: don't invent 10 names for nearly-identical looks. If the character is in their default clothes use `Base` or the existing label they already have. New label = real wardrobe change.\n"
    "- [BLOCKING_END] lists only characters present at end of episode (no OUTFIT needed — it's still the same outfit as in [BLOCKING])\n"
    "- If consecutive episodes continue the same scene, [BLOCKING] of episode N+1 MUST match [BLOCKING_END] of episode N\n\n"

    "PART 2 — DIALOGUE TRANSLATION\n"
    "Translate any dialogue lines NOT in English to English. "
    "Pattern: CHARACTER_NAME_ALLCAPS: \"dialogue\" or CHARACTER_NAME_ALLCAPS: (parenthetical) dialogue. "
    "Do NOT translate action lines (in [brackets]) or scene headings or EPISODE NOTES sections. "
    "Preserve character names exactly as written (ALL CAPS). "
    "Already-English dialogue — leave unchanged.\n\n"

    "PART 3 — SEEDANCE MODERATION SCAN\n"
    "After adapting the script, scan ALL dialogue lines for content that may trigger Seedance AI video generation moderation filters. "
    "Seedance flags: explicit violence (killing, blood, gore, graphic weapon use), sexual content, suicide/self-harm references, "
    "explicit drug use, death threats. "
    "For each flagged line provide 2-3 NATURAL alternative rewrites that sound organic in context — "
    "based on the surrounding scene context and character relationships. "
    "CRITICAL: rewrites must feel like real human speech in the moment. "
    "Bad example: 'Put down the gun' → 'Remove the tactical equipment' (robotic, unnatural). "
    "Good example: 'Put down the gun' → 'Put that down!' or 'Drop it, now!' (natural, urgent, fits the scene). "
    "Only flag lines that are genuinely likely to cause moderation failure — do NOT flag mild drama, "
    "emotional conflict, or normal thriller tension.\n\n"

    "Output format: return a JSON object with THREE keys:\n"
    "  script: the full adapted script as a string\n"
    "  changes: array of short strings describing what was done, e.g. "
    "[\"Added BLOCKING to episode 1\", \"Added BLOCKING_END to episode 3\", \"Translated 5 dialogue lines\"]\n"
    "  moderation_warnings: array of objects, each: "
    "{\"original\": \"JOHN: \\\"I'll kill you\\\"\", \"reason\": \"Explicit death threat\", "
    "\"suggestions\": [\"JOHN: \\\"You'll regret this!\\\"\", \"JOHN: \\\"I swear you'll pay for this!\\\"\"]}\n"
    "If no moderation issues found, moderation_warnings must be an empty array [].\n\n"
    "Output ONLY valid JSON. No markdown fences."
)


@app.route('/api/adapt-script-to-standard', methods=['POST'])
def adapt_script_to_standard():
    """Takes a raw multi-episode script and adapts it to tool standard:
    1. Adds [BLOCKING] after each scene heading and [BLOCKING_END] at end of each episode
    2. Translates non-English dialogue to English (preserves already-English dialogue)
    3. Does NOT change plot, character names, or action lines
    Body: {script: str}
    Returns: {script: str, changes: [str]}
    """
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    if len(script) > 300000:
        return jsonify({'error': 'script too long (>300k chars). Split into smaller batches.'}), 400

    prompt = (
        "Adapt the following multi-episode script to the position-blocks standard. "
        "Add [BLOCKING] blocks after every scene heading and [BLOCKING_END] at the end of every episode. "
        "Translate any non-English dialogue lines to English. "
        "Return JSON with 'script' and 'changes' keys.\n\n"
        "SCRIPT:\n" + script
    )

    try:
        raw = claude_ask(
            prompt,
            system=_ADAPT_TO_STANDARD_SYSTEM,
            model='claude-sonnet-4-5',
            max_tokens=32000,
            timeout=180,
        ).strip()
    except Exception as e:
        _log_event('WARN', 'adapt_script_to_standard_failed', err=str(e)[:300])
        return jsonify({'error': f'Не удалось адаптировать сценарий: {e}'}), 500

    # Strip accidental code-fence wrappers
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)
        raw = raw.strip()

    try:
        result = json.loads(strip_json(raw))
    except Exception:
        # If JSON parse fails, return the raw text as script with no change list
        _log_event('WARN', 'adapt_script_to_standard_json_parse_fail', raw_chars=len(raw))
        return jsonify({'script': raw, 'changes': ['(не удалось распарсить список изменений)']})

    adapted_script = result.get('script', raw)
    # Same deterministic backstop as /api/check-moderation — scan the ADAPTED
    # script so figurative/third-person triggers ("that's suicide", "slaughter")
    # the LLM rationalized away still surface.
    merged_warnings = _merge_moderation_warnings(
        result.get('moderation_warnings', []), adapted_script
    )
    return jsonify({
        'script':              adapted_script,
        'changes':             result.get('changes', []),
        'moderation_warnings': merged_warnings,
    })


from sw.textrules_moderation import (
    _MOD_TRIGGER_GROUPS,
    _NON_SPEAKER_LABELS,
    _lexical_moderation_scan,
    _SOFTEN_MAP,
    _soften_line,
    _fallback_suggestions,
    _REWRITE_SYSTEM,
    _author_rewrites,
    _modkey,
    _merge_moderation_warnings,
    _PHRASE_CHECK_SYSTEM,
)
@app.route('/api/check-moderation', methods=['POST'])
def check_moderation():
    """Fast phrase scan: checks script dialogue for Seedance moderation risk.
    No position blocks, no translation — only moderation_warnings.
    Body: {script: str}
    Returns: {moderation_warnings: [{original, reason, suggestions}]}
    """
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'moderation_warnings': []})
    if len(script) > 200000:
        return jsonify({'error': 'script too long (>200k chars)'}), 400

    # LLM advisor (best-effort — adds nuance + authors rewrites). Its failure
    # must NOT swallow the deterministic lexical scan, which is the real recall
    # guarantee. So we never 500 here: worst case the lexical scan stands alone.
    llm_warnings = []
    try:
        raw = claude_ask(
            f"Scan this script for Seedance moderation risks:\n\n{script}",
            system=_PHRASE_CHECK_SYSTEM,
            model='claude-haiku-4-5',   # fast + cheap — just a scan
            max_tokens=4096,
            timeout=60,
        ).strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
            raw = re.sub(r'\n?```\s*$', '', raw).strip()
        llm_warnings = (json.loads(strip_json(raw)) or {}).get('moderation_warnings', []) or []
    except Exception as e:
        _log_event('WARN', 'check_moderation_llm_failed', err=str(e)[:200])

    merged = _merge_moderation_warnings(llm_warnings, script)
    return jsonify({'moderation_warnings': merged})


@app.route('/api/series/import-from-script', methods=['POST'])
def import_from_script():
    """Two-phase commit: create series + episodes (synchronous, fast),
    then kick off background extraction. Returns immediately so UI can
    redirect to the new series page and start polling /import-status.

    Accepts EITHER:
      • JSON body { title, script, extract_entities?, synopsis? } — classic path.
      • multipart/form-data with the same fields + optional file uploads:
          character_files[]  — image files; name = filename stem (uppercased)
          location_files[]   — same, for locations
          extract_characters / extract_locations / extract_items — per-type bool
            flags (each defaults to extract_entities). When false, the worker
            still links existing roster entries by name but won't CREATE new
            entities of that type — so the user's pre-uploaded set is final.
    """
    is_multipart = request.content_type and request.content_type.startswith('multipart/')
    if is_multipart:
        form = request.form
        title = (form.get('title') or '').strip()
        script = (form.get('script') or '').strip()
        synopsis = form.get('synopsis') or ''
        def _flag(name, default):
            v = form.get(name)
            if v is None or v == '': return default
            return v not in ('0', 'false', 'False', 'off', 'no')
        do_extract        = _flag('extract_entities', True)
        do_extract_chars  = _flag('extract_characters', do_extract)
        do_extract_locs   = _flag('extract_locations',  do_extract)
        do_extract_items  = _flag('extract_items',      do_extract)
        dialogue_lang_hint = (form.get('dialogue_language_hint') or '').strip().lower()
        style_type = (form.get('style_type') or 'cinematic').strip().lower() or 'cinematic'
        style_custom_desc = (form.get('style_custom_description') or '').strip()
        writer_model_in = (form.get('writer_model') or '').strip().lower()
        char_files = request.files.getlist('character_files') or request.files.getlist('character_files[]')
        loc_files  = request.files.getlist('location_files')  or request.files.getlist('location_files[]')
    else:
        data = request.json or {}
        title = (data.get('title') or '').strip()
        script = (data.get('script') or '').strip()
        synopsis = data.get('synopsis', '')
        do_extract = bool(data.get('extract_entities', True))
        do_extract_chars = bool(data.get('extract_characters', do_extract))
        do_extract_locs  = bool(data.get('extract_locations',  do_extract))
        do_extract_items = bool(data.get('extract_items',      do_extract))
        dialogue_lang_hint = (data.get('dialogue_language_hint') or '').strip().lower()
        style_type = (data.get('style_type') or 'cinematic').strip().lower() or 'cinematic'
        style_custom_desc = (data.get('style_custom_description') or '').strip()
        writer_model_in = (data.get('writer_model') or '').strip().lower()
        char_files, loc_files = [], []
    writer_model_value = writer_model_in if writer_model_in in WRITER_MODEL_WHITELIST else WRITER_MODEL_DEFAULT

    if not title:
        return jsonify({'error': 'title required'}), 400
    if not script:
        return jsonify({'error': 'script required'}), 400

    eps = _split_script_into_episodes(script)
    if not eps:
        return jsonify({'error': 'script split produced no episodes'}), 400

    # Build the series shell — same defaults as create_series().
    slug = slugify(title)
    sid = slug if slug and not (user_root() / slug).exists() else f"{slug}-{str(uuid.uuid4())[:6]}"
    # Resolve user-chosen style: preset → canonical desc from _VISUAL_STYLE_PRESETS,
    # or 'custom' → use user-supplied description. visual_style is what the
    # background extraction worker reads when generating char/loc portraits,
    # so it MUST be populated correctly BEFORE the worker fires below.
    visual_style_value = ''
    if style_type == 'custom' and style_custom_desc:
        visual_style_value = style_custom_desc
    elif style_type in _VISUAL_STYLE_PRESETS:
        visual_style_value = _VISUAL_STYLE_PRESETS[style_type].get('desc', '') or ''
    series_data = {
        'id': sid, 'title': title,
        # Deterministic episode titles `<Series_Title>_E<N>` for all new series.
        'episode_title_format': 'series_indexed',
        'genre': '', 'tone': '', 'target_audience': '', 'world_description': '',
        'synopsis': synopsis,
        'auto_generate_assets': True, 'batch_mode': False, 'batch_size': 1,
        'stage': 4, 'arc': None, 'milestone_synopses': {},
        'checkpoints': [], 'finale': None,
        'devices_index': {},
        'cadence_policy': {'default_min_gap': 4, 'hard_limit': 3},
        'created_at': datetime.datetime.utcnow().isoformat(),
        'video_provider': 'seedance',
        'characters': [], 'locations': [], 'items': [],
        'style': {
            'type': style_type if style_type in _VISUAL_STYLE_PRESETS or style_type == 'custom' else 'cinematic',
            'custom_description': style_custom_desc if style_type == 'custom' else '',
            'ref_images': [],
        },
        'visual_style': visual_style_value,
        'writer_model': writer_model_value,
        'settings': {
            'voice': 'Enceladus', 'tts_provider': 'elevenlabs',
            'image_provider': 'banana', 'aspect_ratio': '9:16',
            'language': 'English', 'duration': 'auto-frames',
            'enable_music': True, 'music_volume': 0.30,
            'enable_animation': True, 'animation_speed': 'fast',
            'animation_resolution': '480p', 'animation_model': 'seedance-2-ref',
            'enable_grid': False, 'cinema': False, 'trim': True,
            'no_fades': True, 'multi_voice': False, 'enable_subtitles': False,
            'image_size': '1K',
        },
    }
    # User imported a non-English script and explicitly chose «оставить как
    # есть» in the preview warning — remember it on the series so the UI can
    # show a persistent badge `🌐 Диалоги: русский` and the user isn't
    # surprised later. Empty / 'en' = no badge.
    if dialogue_lang_hint and dialogue_lang_hint not in ('en', 'english'):
        series_data['dialogue_language_hint'] = dialogue_lang_hint
    save_series(sid, series_data)
    scaffold_series_folders(sid, title)

    # ── Persist pre-uploaded characters / locations BEFORE the worker runs.
    # Filename stem becomes the entity name (so the worker's name-based dedup
    # picks them up when the script mentions them). Image saved as the canonical
    # asset → user sees their character/location with an image right away,
    # before any AI generation.
    def _stem_to_name(filename):
        # «mia_chen.jpg» → «Mia Chen»; «ОСОБНЯК БЕЛЛАКУРТОВ.png» → «Особняк
        # Беллакуртов» (Title-cased — looks better in roster than ALL-CAPS).
        # Strip extension, replace separators with spaces, collapse, title-case.
        stem = re.sub(r'\.[^.]+$', '', filename or '').strip()
        stem = re.sub(r'[._\-]+', ' ', stem).strip()
        stem = re.sub(r'\s+', ' ', stem)
        if not stem: return ''
        # If user typed ALL CAPS, preserve as Title Case for legibility.
        if stem.isupper(): stem = stem.title()
        return stem

    uploaded_chars_count = 0
    for f in (char_files or []):
        if not f or not getattr(f, 'filename', ''): continue
        if not allowed_file(f.filename):
            _log_event('WARN', 'import_char_skip_badtype', name=f.filename); continue
        name = _stem_to_name(f.filename)
        if not name: continue
        # Skip name duplicates within this batch.
        if any(c['name'].lower() == name.lower() for c in series_data['characters']):
            continue
        cid = str(uuid.uuid4())[:8]
        char_dir = assets_dir(sid) / 'characters' / cid
        char_dir.mkdir(parents=True, exist_ok=True)
        safe = secure_filename(f.filename) or f'{cid}.jpg'
        dst = char_dir / safe
        try: f.save(dst)
        except Exception as e:
            _log_event('WARN', 'import_char_save_failed', name=name, err=str(e)[:160]); continue
        rel_path = str(dst.relative_to(series_path(sid)))
        series_data['characters'].append({
            'id': cid, 'name': name,
            'description': '', 'appearance': '',
            'gender': 'female', 'voice_id': '',
            'ref_images': [rel_path],
            'outfits': [], 'base_outfit_label': 'base',
        })
        uploaded_chars_count += 1

    uploaded_locs_count = 0
    for f in (loc_files or []):
        if not f or not getattr(f, 'filename', ''): continue
        if not allowed_file(f.filename):
            _log_event('WARN', 'import_loc_skip_badtype', name=f.filename); continue
        name = _stem_to_name(f.filename)
        if not name: continue
        if any(l['name'].lower() == name.lower() for l in series_data['locations']):
            continue
        lid = str(uuid.uuid4())[:8]
        loc_dir = assets_dir(sid) / 'locations' / lid
        loc_dir.mkdir(parents=True, exist_ok=True)
        safe = secure_filename(f.filename) or f'{lid}.jpg'
        dst = loc_dir / safe
        try: f.save(dst)
        except Exception as e:
            _log_event('WARN', 'import_loc_save_failed', name=name, err=str(e)[:160]); continue
        rel_path = str(dst.relative_to(series_path(sid)))
        series_data['locations'].append({
            'id': lid, 'name': name,
            'description': '',
            'ref_images': [rel_path], 'avai_url': '',
        })
        uploaded_locs_count += 1

    # Re-save now that pre-uploaded entities are baked in. The worker will pick
    # this up via load_series() inside its per-episode loop.
    if uploaded_chars_count or uploaded_locs_count:
        save_series(sid, series_data)

    # Create each episode with the script body pre-filled.
    ep_records = []
    for e in eps:
        ep_dict = {
            'number': e['number'],
            'title':  e['title'],
            'synopsis': '',
            'script':   e['body'],
            'characters_used': [],
            'locations_used':  [],
            'items_used':      [],
            'notes': '', 'reteller_prompt': '',
            'status': 'draft', 'ready': False,
            'created_at': datetime.datetime.utcnow().isoformat(),
        }
        save_episode(sid, e['number'], ep_dict)
        ep_records.append({'number': e['number']})
        # Sync [BLOCKING] outfits right after save — bulk-import is the most
        # common path where Margaret-style outfits get missed (a long import
        # of N episodes with many one-off labels could otherwise silently lose
        # them all until the user runs autogen). Idempotent + cheap.
        try:
            _sync_script_outfits(sid, e['body'])
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_import_failed',
                       sid=sid, ep=e['number'], err=str(_oe)[:200])

    # Kick off the extraction worker in the background. _spawn_with_keys
    # carries the user's auth context across the thread boundary. We always
    # run the worker if ANY per-type extraction is enabled (so episode
    # *_used arrays get populated by name-matching against pre-uploaded
    # roster) — even when no new-entity creation is allowed of that type.
    worker_should_run = do_extract_chars or do_extract_locs or do_extract_items
    if worker_should_run:
        _spawn_with_keys(
            _import_worker, sid, ep_records,
            create_chars=do_extract_chars,
            create_locs=do_extract_locs,
            create_items=do_extract_items,
        )

    # Auto-Vision appearance backfill for pre-uploaded characters. Each char
    # uploaded by the user lands with empty `appearance` — BINDING line for
    # Seedance is just the name, no text anchor for outfit / build / hair.
    # Run a background job per uploaded char that: (1) uploads the local
    # ref image to AVAI public storage to get a URL, (2) asks Claude Haiku
    # Vision for a Russian appearance description, (3) writes it to the
    # char's `appearance` field. Latency ~5-8s per char in parallel.
    if uploaded_chars_count:
        uploaded_char_ids = [c['id'] for c in series_data['characters'][-uploaded_chars_count:]]
        _spawn_with_keys(_backfill_uploaded_char_appearances, sid, uploaded_char_ids)

    return jsonify({
        'sid': sid,
        'episodes_created': len(eps),
        'extraction_started': worker_should_run,
        'characters_uploaded': uploaded_chars_count,
        'locations_uploaded': uploaded_locs_count,
        'extract_characters': do_extract_chars,
        'extract_locations':  do_extract_locs,
        'extract_items':      do_extract_items,
    }), 201


@app.route('/api/series/<sid>/import-status')
def import_status(sid):
    return jsonify(_import_status(sid))
