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
from sw.routes.import_series_worker import _import_worker

@app.route('/api/series/<sid>/backfill-devices', methods=['POST'])
def backfill_devices(sid):
    """Extract plot_devices and narrative_state for all episodes that don't have them yet.
    Useful for series created before the device/narrative registry was introduced.
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    episodes = list_episodes(sid)
    updated_devices = 0
    updated_narrative = 0
    for ep in sorted(episodes, key=lambda e: e.get('number', 0)):
        ep_num = ep.get('number', 0)
        script = ep.get('script', '')
        if not script:
            continue
        if not ep.get('plot_devices'):
            devices = _extract_devices_from_script(script)
            ep['plot_devices'] = devices or []
            save_episode(sid, ep_num, ep)
            if devices:
                _update_devices_index(sid, ep_num, devices)
            updated_devices += 1
        if not ep.get('narrative_state'):
            narrative = _extract_narrative_state_from_script(script)
            if narrative:
                ep['narrative_state'] = narrative
                save_episode(sid, ep_num, ep)
                _update_narrative_index(sid, ep_num, narrative)
                updated_narrative += 1
    return jsonify({'updated_devices': updated_devices, 'updated_narrative': updated_narrative})


@app.route('/api/series/<sid>/reextract', methods=['POST'])
def reextract_series(sid):
    """Re-runs the per-episode entity extractor on an already-imported series.
    Use case: an earlier import partially failed (LLM JSON parse error,
    server restart killed the worker, etc.) and chars/items are missing.
    Walks every existing episode that has a script and queues the same worker
    used by /import-from-script. Skips episodes that already have ALL three
    of (characters_used, locations_used, items_used) populated unless
    body.force is true."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    force = bool(body.get('force', False))
    eps = list_episodes(sid)
    targets = []
    for ep in sorted(eps, key=lambda e: e['number']):
        if not (ep.get('script') or '').strip():
            continue
        if not force:
            has_chars = bool(ep.get('characters_used'))
            has_locs  = bool(ep.get('locations_used'))
            has_items = bool(ep.get('items_used'))
            if has_chars and has_locs and has_items:
                continue
        targets.append({'number': ep['number']})
    if not targets:
        return jsonify({'queued': 0, 'message': 'all episodes already have entities (use force=true to redo)'}), 200
    _spawn_with_keys(_import_worker, sid, targets)
    return jsonify({'queued': len(targets), 'started': True}), 202


# Max stored per-episode outline beats. Was a hard [:5] at create time; raised so
# the outline can grow when the source drama is re-analyzed for later episode ranges
# (6-10, 11-15…) via /api/series/<sid>/analyze-more-episodes.
_OUTLINE_MAX = 40

# Allowed attribution values for how confidently a series is tied to its source drama.
_SOURCE_ATTRIBUTION = ('exact', 'guessed', 'manual', 'unknown')


def _resolve_source_drama(raw, outline):
    """Normalize the source-drama link carried in from the 'make series from top
    drama' flow. Returns None when nothing usable was sent (no id and no title) so
    manually-created series stay unlinked. `analyzed_through` records how many of the
    drama's episodes are already laid out in source_episode_outline (used to compute
    the next range when re-analyzing episodes 6-10, etc.)."""
    if not isinstance(raw, dict):
        return None
    did = str(raw.get('id') or '').strip()
    title = str(raw.get('title') or '').strip()
    if not did and not title:
        return None
    attribution = str(raw.get('attribution') or 'exact').strip().lower()
    if attribution not in _SOURCE_ATTRIBUTION:
        attribution = 'exact'
    n_outline = len([x for x in (outline or []) if str(x).strip()])
    return {
        'id': did,
        'title': title,
        'genre': str(raw.get('genre') or '').strip(),
        'premise': str(raw.get('premise') or '').strip(),
        'attribution': attribution,
        'analyzed_through': n_outline,
    }


@app.route('/api/series', methods=['POST'])
def create_series():
    data = request.json
    slug = slugify(data.get('title', ''))
    sid = slug if slug and not (user_root() / slug).exists() else f"{slug}-{str(uuid.uuid4())[:6]}"
    # Format mode controls generators across the board: 'short_drama' (default, TikTok/ReelShort
    # addictive serial) or 'instagram_series' (standalone episodes, simpler titles, character-of-week).
    _format_mode_raw = (data.get('format_mode') or 'short_drama').strip().lower()
    _format_mode = _format_mode_raw if _format_mode_raw in ('short_drama', 'instagram_series') else 'short_drama'
    series_data = {
        'id': sid,
        'title': data['title'],
        # Deterministic episode titles `<Series_Title>_E<N>` for all new series.
        'episode_title_format': 'series_indexed',
        'genre': data.get('genre', ''),
        'tone': data.get('tone', ''),
        'target_audience': data.get('target_audience', ''),
        'world_description': data.get('world_description', ''),
        'synopsis': data.get('synopsis', ''),
        'format_mode': _format_mode,
        # Scenario constructor: ORDERED hook-beat sequence (ноды) assembled in
        # the create modal. Stored resolved as {id, ru, beat} (id='' for custom
        # free-text beats) so episode generators replay it in order — see
        # _series_beats_episode_block. Order is significant.
        'beat_sequence': _resolve_beats(data.get('beats') or []),
        # Per-episode outline carried in from a deep-analyzed top drama: the
        # first episodes are written to these beats, with THIS series' own cast
        # (the source names in the beats are placeholders). See _source_outline_episode_block.
        'source_episode_outline': [str(x).strip() for x in (data.get('source_episode_outline') or []) if str(x).strip()][:_OUTLINE_MAX],
        # Which real short drama this series is adapted from — id/title/premise +
        # attribution confidence + analyzed_through (how many of the drama's episodes
        # are already laid out in source_episode_outline). Enables continuing the
        # series by re-analyzing the drama's later episodes. None for original series.
        'source_drama': _resolve_source_drama(data.get('source_drama'), data.get('source_episode_outline')),
        # Asian-recast flag from the "make series from top drama" flow: keep the
        # source plot 1-to-1 but every character is Asian and the world is East-Asian.
        # Reinforced in episode generation via _source_outline_episode_block.
        'asian_recast': bool(data.get('asian_recast', False)),
        # Creative-writing model selector (ideas + episode scripts). Set at
        # creation time, can be overridden per-call from UI. Whitelist enforced
        # in _resolve_writer_model. Unknown / missing → default Claude.
        'writer_model': (data.get('writer_model') or '').strip().lower() or WRITER_MODEL_DEFAULT,
        'auto_generate_assets': bool(data.get('auto_generate_assets', True)),
        'batch_mode':           bool(data.get('batch_mode', False)),
        'batch_size':           int(data.get('batch_size', 5)) if data.get('batch_mode') else 1,
        # Episode duration target — accepts None (server default = 60s applied
        # downstream by the writer prompt). Clamp to writer-safe range matching
        # the bible modal's validator. Skip the override entirely on bad input.
        'target_duration_sec':  (
            max(30, min(240, int(data['target_duration_sec'])))
            if str(data.get('target_duration_sec') or '').strip().lstrip('-').isdigit()
            else None
        ),
        # Skip the legacy stage-1/2 milestones pipeline — new series start with empty
        # episode list. User adds episodes manually + optionally pins checkpoints / finale.
        'stage': 4,
        'arc': None,
        'milestone_synopses': {},
        'checkpoints': [],   # [{episode: int, description: str}]  story landmarks
        'finale': None,      # {episode: int, description: str} | None
        'created_at': datetime.datetime.utcnow().isoformat(),
        'video_provider': 'seedance',  # default for NEW series — Seedance mode active
        'characters': [],
        'locations': [],
        'items': [],                   # story-relevant props (handbag, gun, locket...)
        'devices_index': {},           # plot-device anti-repetition registry
        'cadence_policy': {'default_min_gap': 4, 'hard_limit': 3},
        'style': {
            'type': 'cinematic',
            'custom_description': '',
            'ref_images': []
        },
        'settings': {
            'voice':                'Enceladus',
            'tts_provider':         'elevenlabs',
            'image_provider':       'banana',         # → Reteller "Banana Pro"
            'aspect_ratio':         '9:16',
            'language':             'English',
            'duration':             'auto-frames',    # Reteller "Auto-frames" mode
            'enable_music':         True,
            'music_volume':         0.30,             # 30%
            'enable_animation':     True,
            'animation_speed':      'fast',
            'animation_resolution': '480p',
            'animation_model':      'seedance-2-ref', # → Reteller "Seedance 2.0 Ref"
            'enable_grid':          False,            # animation grid (frame grid overlay) OFF
            'cinema':               False,
            'trim':                 True,
            'no_fades':             True,
            'multi_voice':          False,
            'enable_subtitles':     False,
            'image_size':           '1K'
        }
    }
    # Pre-confirm the era for asset generation from the create-modal pick, so
    # character/portrait generation uses the right period immediately and the
    # user isn't re-asked via the «Ваш сериал в сеттинге X?» banner (which, if
    # ignored, used to silently generate modern-day characters). Only applied
    # for an EXPLICIT non-default pick — pure modern+realistic is left on 'auto'
    # so free-text-described periods still get auto-detection + the banner.
    _era_pick   = (data.get('era') or 'modern').strip().lower()
    _world_pick = (data.get('world_setting') or 'realistic').strip().lower()
    if _era_pick not in ('', 'modern') or _world_pick not in ('', 'realistic'):
        _mapped_era = _modal_setting_to_era_choice(
            data.get('era'), data.get('era_custom'),
            data.get('world_setting'), data.get('world_custom'),
            synopsis_text=' '.join(filter(None, [
                series_data.get('synopsis', ''), series_data.get('world_description', ''),
            ])),
        )
        if _mapped_era:
            series_data['era_choice'] = _mapped_era
            series_data['era_confirmed'] = True
    save_series(sid, series_data)
    scaffold_info = scaffold_series_folders(sid, data['title'])
    series_data['_scaffold'] = scaffold_info
    trigger_autogen_if_enabled(sid)
    return jsonify(series_data), 201


@app.route('/api/series/clone-from', methods=['POST'])
def clone_series_from():
    """Create a NEW series as a revised clone of an existing one.

    Body: {
      source_sid: str,                # series to clone from (required)
      title: str,                     # new series title (optional — defaults to «<src> (вариант)»)
      revision_instructions: str,     # free-text edits, e.g. «главная героиня молодая и красивая»
      episodes_to_copy: int,          # how many leading episodes to copy (0/absent = all)
      writer_model: str,              # optional override for the LLM revision pass
    }

    Behaviour (per the user's spec):
      • Deep-copies the source bible + assets + first N episode scripts into the new series.
      • Applies the revision instructions to the BIBLE and CAST immediately (one LLM pass):
        synopsis/world/tone/arc get rewritten only if the plot changes; each character's
        look/name/age is updated; changed portraits are wiped so autogen regenerates them
        with the new (e.g. beautiful) description.
      • Episode scripts are copied verbatim. We do NOT mass-rewrite them. Renamed characters
        are fixed in-place with a programmatic find/replace. Episodes that genuinely need a
        rewrite (the plot changed in them, or an age move could create a timeline
        contradiction) are queued in revision_plan.pending_episodes for one-by-one rewriting
        via /episodes/<num>/apply-revisions.
      • Future script/synopsis generation honours revision_instructions automatically
        (see _revision_instructions_block).
    """
    data = request.json or {}
    source_sid = (data.get('source_sid') or '').strip()
    title = (data.get('title') or '').strip()
    revision_instructions = (data.get('revision_instructions') or '').strip()
    try:
        episodes_to_copy = int(data.get('episodes_to_copy'))
    except (TypeError, ValueError):
        episodes_to_copy = 0  # 0 / missing → copy all
    if episodes_to_copy < 0:
        episodes_to_copy = 0

    if not source_sid:
        return jsonify({'error': 'source_sid required'}), 400
    src = load_series(source_sid)
    if not src:
        return jsonify({'error': 'source series not found'}), 404
    if not title:
        title = f"{src.get('title', 'Series')} (вариант)"

    slug = slugify(title)
    sid = slug if slug and not (user_root() / slug).exists() else f"{slug}-{str(uuid.uuid4())[:6]}"

    # ── 1) Deep-copy the bible, re-stamp identity, drop instance-specific state.
    new_series = copy.deepcopy(src)
    new_series['id'] = sid
    new_series['title'] = title
    # Deterministic episode titles `<Series_Title>_E<N>` for all new series
    # (clones included — copied episodes get re-titled to the new series name).
    new_series['episode_title_format'] = 'series_indexed'
    new_series['created_at'] = datetime.datetime.utcnow().isoformat()
    new_series['cloned_from'] = source_sid
    new_series['revision_instructions'] = revision_instructions
    if (data.get('writer_model') or '').strip().lower():
        new_series['writer_model'] = data['writer_model'].strip().lower()
    # Fresh cover (poster reflects the new title / possibly new looks).
    new_series['cover_image'] = ''
    new_series['cover_image_url'] = ''
    new_series['cover_image_version'] = 0
    for k in ('archived', 'pinned', 'pinned_at', '_scaffold', '_clone'):
        new_series.pop(k, None)

    save_series(sid, new_series)
    scaffold_info = scaffold_series_folders(sid, title)

    # ── 2) Copy asset reference images so character/location ref_images resolve.
    try:
        src_assets = assets_dir(source_sid)
        if src_assets.exists():
            shutil.copytree(str(src_assets), str(assets_dir(sid)), dirs_exist_ok=True)
    except Exception as e:
        print(f'[clone] asset copy warning: {e}', flush=True)

    # ── 3) Copy episode scripts (first N, or all). We copy ONLY story content and
    #       drop ALL generation/render state — seedance_chunks, assembled video,
    #       music scenes, reteller prompt/project, batch caches, etc. all reference
    #       rendered media in the source's OUT/VID dirs (which we do NOT copy), so
    #       carrying them over leaves "generated chunks with empty videos" in the
    #       clone (the exact bug this guards against). Allowlist (not denylist) so
    #       any future render field is dropped by default rather than leaking.
    _EP_CONTENT_KEYS = {
        'number', 'title', 'synopsis', 'script', 'characters_used', 'locations_used',
        'items_used', 'notes', 'character_outfits', 'ready', 'status', 'cast_extracted',
        'created_at', 'plot_devices', 'days_since_previous', 'scene_blocking',
    }
    src_eps = sorted(list_episodes(source_sid), key=lambda e: int(e.get('number', 0) or 0))
    src_total = len(src_eps)
    if episodes_to_copy > 0:
        src_eps = [e for e in src_eps if int(e.get('number', 0) or 0) <= episodes_to_copy]
    copied_numbers = []
    for ep in src_eps:
        num = int(ep.get('number', 0) or 0)
        if num <= 0:
            continue
        ep_copy = {k: copy.deepcopy(v) for k, v in ep.items() if k in _EP_CONTENT_KEYS}
        ep_copy['number'] = num
        # Fresh, un-generated render state — nothing is assembled yet for the clone.
        ep_copy['gen_status'] = ''
        ep_copy['reteller'] = {'project_id': None, 'status': None, 'video_url': None, 'submitted_at': None}
        save_episode(sid, num, ep_copy)
        copied_numbers.append(num)

    # ── 4) Apply revisions to the bible + cast (one LLM pass).
    revision_plan = {
        'scope': 'character_only',
        'reason': '',
        'pending_episodes': [],
        'renames': [],
        'applied_at': datetime.datetime.utcnow().isoformat(),
    }
    if revision_instructions:
        result = _llm_apply_revisions_to_bible(new_series, revision_instructions)
        if result:
            bible = result.get('bible') or {}
            for k in ('genre', 'tone', 'world_description', 'synopsis', 'arc'):
                v = (bible.get(k) or '').strip()
                if v:
                    new_series[k] = v
            char_by_id = {c.get('id'): c for c in new_series.get('characters', [])}
            renames = []
            for upd in (result.get('characters') or []):
                c = char_by_id.get(upd.get('id'))
                if not c:
                    continue
                old_name = c.get('name', '')
                if upd.get('name_changed') and (upd.get('name') or '').strip():
                    new_name = upd['name'].strip()
                    if new_name != old_name:
                        c['name'] = new_name
                        renames.append({'old': old_name, 'new': new_name})
                if upd.get('appearance_changed') and (upd.get('appearance') or '').strip():
                    # Scrub before persisting — a revision like «героиня должна
                    # быть очень красивой и сексуальной» must NOT bake sexualized
                    # wording into the canonical appearance that rides into every
                    # downstream prompt (this was the moderation bug).
                    c['appearance'] = _sanitize_appearance_for_moderation(upd['appearance'].strip())
                    # Wipe portrait + outfit refs so autogen regenerates the new look.
                    c['ref_images'] = []
                    for o in (c.get('outfits') or []):
                        o['ref_images'] = []
                if (upd.get('gender') or '').strip().lower() in ('male', 'female'):
                    c['gender'] = upd['gender'].strip().lower()
                if (upd.get('description') or '').strip():
                    c['description'] = upd['description'].strip()
            for r in (result.get('renames') or []):
                o = (r.get('old') or '').strip()
                n = (r.get('new') or '').strip()
                if o and n and o != n and not any(x['old'] == o for x in renames):
                    renames.append({'old': o, 'new': n})
            revision_plan['renames'] = renames
            revision_plan['scope'] = (result.get('revision_scope') or 'character_only').strip().lower()
            revision_plan['reason'] = (result.get('rewrite_reason') or '').strip()

            # Programmatic name find/replace across copied scripts (word-boundary).
            if renames and copied_numbers:
                for num in copied_numbers:
                    ce = load_episode(sid, num)
                    if not ce:
                        continue
                    sc = ce.get('script') or ''
                    changed = False
                    for r in renames:
                        new_sc, n = re.subn(r'\b' + re.escape(r['old']) + r'\b', r['new'], sc)
                        if n:
                            sc, changed = new_sc, True
                    if changed:
                        ce['script'] = sc
                        save_episode(sid, num, ce)

            # Queue episodes for one-by-one rewrite when the plot changed or an age
            # move risks a timeline contradiction. Pure name/appearance edits → no rewrite.
            ages_changed = any(u.get('age_changed') for u in (result.get('characters') or []))
            if (revision_plan['scope'] == 'plot' or ages_changed) and copied_numbers:
                revision_plan['pending_episodes'] = list(copied_numbers)

    new_series['revision_plan'] = revision_plan
    save_series(sid, new_series)

    # ── 5) Regenerate wiped portraits with the new descriptions + any missing assets.
    trigger_autogen_if_enabled(sid)

    new_series['_scaffold'] = scaffold_info
    new_series['_clone'] = {
        'source_sid': source_sid,
        'source_total_episodes': src_total,
        'copied_episodes': len(copied_numbers),
        'scope': revision_plan['scope'],
        'pending_rewrites': len(revision_plan['pending_episodes']),
        'reason': revision_plan['reason'],
        'renames': revision_plan['renames'],
    }
    return jsonify(new_series), 201


@app.route('/api/series/<sid>/episodes/<int:num>/apply-revisions', methods=['POST'])
def apply_revisions_to_episode(sid, num):
    """Rewrite ONE already-copied episode script so it obeys the series'
    revision_instructions (used after clone-from). Revises the EXISTING script in
    place — same beats / structure / hook / cliffhanger / length — changing only
    what the revisions (and world-consistency) require. Pops the episode off
    revision_plan.pending_episodes on success."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'episode not found'}), 404
    ri = (s.get('revision_instructions') or '').strip()
    if not ri:
        return jsonify({'error': 'у этого сериала нет правок — перезапись не требуется'}), 400
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'сценарий пустой — нечего переписывать'}), 400

    cast_block = _build_cast_block(s, ep)
    system = _build_script_system(s)
    prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n\n'
        + _anthro_world_block(s)
        + _revision_instructions_block(s)
        + (cast_block + '\n\n' if cast_block else '')
        + f'═══ EXISTING EPISODE {num} SCRIPT — REVISE IT IN PLACE ═══\n{script}\n'
        '═══════════════════════════════════════════════\n\n'
        'Rewrite THIS episode\'s script so it obeys the SERIES REVISION INSTRUCTIONS above. '
        'Change ONLY what the revisions require, plus whatever is needed to keep the world '
        'internally consistent (names, ages, timelines, who-met-whom-when). PRESERVE '
        'everything else: the same scene beats, the same structure, the same opening hook '
        'and the same closing cliffhanger, roughly the same length, and the existing dialogue '
        'wherever the revision does not touch it. Keep the [BLOCKING]/[BLOCKING_END] tags and '
        'scene headings intact. Output ONLY the rewritten script text — no commentary, no JSON.'
    )
    try:
        body = request.get_json(silent=True) or {}
        new_script = llm_ask(_resolve_writer_model(body, s), prompt, system=system)
    except Exception as e:
        return jsonify({'error': f'Не удалось переписать сценарий: {e}'}), 502
    new_script = _normalize_blocking_tags((new_script or '').strip())
    if not new_script:
        return jsonify({'error': 'модель вернула пустой сценарий'}), 502

    ep['script'] = new_script
    # Cast may have shifted — let the user re-extract characters for this episode.
    ep['cast_extracted'] = False
    save_episode(sid, num, ep)

    rp = s.get('revision_plan') or {}
    rp['pending_episodes'] = [n for n in (rp.get('pending_episodes') or []) if int(n) != int(num)]
    s['revision_plan'] = rp
    save_series(sid, s)
    return jsonify({'ok': True, 'script': new_script, 'pending_episodes': rp['pending_episodes']}), 200
