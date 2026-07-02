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

# ── Import series from existing script ───────────────────────────────────────
# Lets the user paste / upload a 70-episode script and get a fully populated
# series in one shot: episodes split + chars/locs/items extracted per-episode.
# Two-step UX: (1) preview boundaries before commit, (2) bulk create + start
# background extraction worker. Status polled via /import-status.

# Regex matchers for episode-boundary detection. Tried in order; first that
# yields ≥2 matches wins. Without this fallback chain a script that uses an
# unusual marker (just "5." at the start of a line) would land in episode 1
# alone.
# NOTE: leading whitespace is `[ \t]*` (NOT `\s*`) on purpose. `\s` matches
# newlines too — with multiline `^`, a `\s*` quantifier can consume entire
# lines BETWEEN the boundary markers, swallowing actual episode body into the
# next match. Restricting to spaces/tabs keeps each match anchored to a single
# line.
# `[*_]{0,3}` tolerates markdown bold/italic wrappers like `**СЕРИЯ 1 — "TITLE"**`
# or `__Episode 5__`. Without this, a script copy-pasted from chat (where the
# author wrapped headings in `**`) silently parsed as a single mega-episode.
# User-reported bug: Natia uploaded 5-episode RU script, preview said 1.
_EPISODE_BOUNDARY_PATTERNS = [
    # Triple-equals fenced: === ЭПИЗОД 5 === / === EPISODE 5 === (±**bold**)
    r'(?im)^[ \t]*[*_]{0,3}[ \t]*={2,}[ \t]*(?:эпизод|серия|episode|ep\.?)[ \t]*(\d+)[^\n]*$',
    # Markdown headers: ## ЭПИЗОД 5 / # Episode 5
    r'(?im)^#{1,6}[ \t]*(?:эпизод|серия|episode|ep\.?)[ \t]*(\d+)[^\n]*$',
    # Plain bare line: ЭПИЗОД 5 / Episode 5 / Серия 5 / **СЕРИЯ 5 — "TITLE"**
    r'(?im)^[ \t]*[*_]{0,3}[ \t]*(?:эпизод|серия|episode|ep\.?)[ \t]+(\d+)[ \t]*[:\-—]?[ \t]*[^\n]*$',
    # Numbered with period only: 5. (when on its own line)
    r'(?m)^[ \t]*(\d+)\.[ \t]*$',
]

def _split_script_into_episodes(text):
    """Returns [{'number': int, 'title': str, 'body': str}, ...] or [] if
    no boundaries could be found. Pattern chain tries the most-specific
    markers first and falls back to looser ones."""
    if not text or not text.strip():
        return []
    for pattern in _EPISODE_BOUNDARY_PATTERNS:
        matches = list(re.finditer(pattern, text))
        if len(matches) < 2:
            continue
        episodes = []
        # Prefix content — anything before the FIRST marker. Often the user
        # pastes a series where the very first episode has no «Episode N:»
        # header (just a body or «Кратко: …» summary). Previously this got
        # silently dropped. Now: if the prefix has more than 50 non-whitespace
        # chars, treat it as a leading episode (number = first_match_num - 1,
        # or 1 if that goes < 1). Title is taken from the first non-empty line.
        first_start = matches[0].start()
        prefix = text[:first_start].strip()
        if len(re.sub(r'\s+', '', prefix)) > 50:
            try:
                first_num = int(matches[0].group(1))
            except (ValueError, IndexError):
                first_num = 2
            prefix_num = max(1, first_num - 1)
            # Title from first non-empty line of prefix (strip «Кратко:» etc).
            prefix_title = ''
            for ln in prefix.split('\n'):
                t = ln.strip()
                if t:
                    t = re.sub(r'^(кратко|brief|summary|синопсис)\s*[:\-—]\s*', '', t, flags=re.IGNORECASE)
                    prefix_title = t[:80]
                    break
            episodes.append({'number': prefix_num, 'title': prefix_title, 'body': prefix})
        for i, m in enumerate(matches):
            try:
                num = int(m.group(1))
            except (ValueError, IndexError):
                num = i + 1
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            # Title = the matched line (without the marker prefix and any
            # trailing decorators), trimmed. Strip markdown bold/italic
            # wrappers (** __ *) — for `**СЕРИЯ 1 — "YOUR CEILING"**` the
            # title should come out as `"YOUR CEILING"`, not `**"YOUR CEILING"**`.
            line = m.group(0).strip()
            title = re.sub(r'^[#=*_\s]+', '', line)                                              # leading # = * _
            title = re.sub(r'[#=*_\s]+$', '', title)                                             # trailing # = * _
            title = re.sub(r'^(эпизод|серия|episode|ep\.?)\s*\d+\s*[:\-—]?\s*', '', title, flags=re.IGNORECASE)
            title = title.strip(' \t*_#"\'')                                                     # final polish for stray quotes/decorators
            episodes.append({'number': num, 'title': title, 'body': body})
        if episodes:
            # Renumber sequentially if numbers are dup or non-monotonic.
            seen = set()
            for ep in episodes:
                if ep['number'] in seen or ep['number'] < 1:
                    ep['number'] = max(seen, default=0) + 1
                seen.add(ep['number'])
            return episodes
    # No boundaries found → treat whole text as a single episode.
    return [{'number': 1, 'title': '', 'body': text.strip()}]


from sw.scriptparse import (
    _DIALOGUE_LINE_RE,
    _ACTION_LINE_RE,
    _CYRILLIC_RE,
    _CJK_RE,
    _HIRAGANA_RE,
    _KATAKANA_RE,
    _HANGUL_RE,
    _LATIN_RE,
    _detect_dialogue_language,
    _IMPORT_STATUS,
    _IMPORT_LOCKS,
)
def _import_status(sid):
    return _IMPORT_STATUS.setdefault(sid, {
        'running': False, 'total': 0, 'done': 0, 'errors': [],
        'started_at': None, 'finished_at': None, 'current': None,
    })


def _llm_extract_episode_entities(script_text, known_chars, known_locs, known_items, series=None):
    """One LLM call per episode that returns chars + locs + items in JSON.
    Faster than running /extract-characters + /detect-items separately. Passes
    known names so the model can flag re-uses vs new entities.
    `series` (optional) carries world-context — if it describes an anthropomorphic-
    animal world, we inject a directive forcing species into every appearance."""
    if not script_text or not script_text.strip():
        return {'characters': [], 'locations': [], 'items': []}
    known_section = ''
    if known_chars or known_locs or known_items:
        known_section = (
            f"\n\nALREADY KNOWN ENTITIES (REUSE these names where the script mentions them):\n"
            f"  characters: {', '.join(sorted(known_chars)) or '(none)'}\n"
            f"  locations:  {', '.join(sorted(known_locs))  or '(none)'}\n"
            f"  items:      {', '.join(sorted(known_items)) or '(none)'}\n"
        )
    # World-context block — only emitted when the series is anthropomorphic.
    # Forces appearance text to start with the species marker so portrait gen
    # later renders an animal, not a human.
    world_block = ''
    if isinstance(series, dict) and _is_anthro_world(series):
        world_block = (
            f"\n\nSERIES WORLD CONTEXT (CRITICAL):\n"
            f"  title: {series.get('title','')}\n"
            f"  world: {(series.get('world_description') or '')[:600]}\n"
            f"  synopsis: {(series.get('synopsis') or '')[:600]}\n\n"
            + _anthro_world_block(series)
        )
    # Casting aesthetics — leads & romance/intimacy roles must read as attractive.
    # Needs synopsis context, so emit it for every series (anthro or human).
    casting_block = ''
    if isinstance(series, dict):
        if not world_block:
            casting_block = (
                f"\n\nSERIES CONTEXT:\n"
                f"  title: {series.get('title','')}\n"
                f"  genre: {series.get('genre','')}\n"
                f"  synopsis: {(series.get('synopsis') or '')[:600]}\n\n"
            )
        casting_block += _casting_aesthetics_block(series)
    system = (
        "You extract structured cast/crew data from a single short-drama episode script. "
        "Return STRICT JSON, no prose, no markdown.\n\n"
        "Schema:\n"
        '{\n'
        '  "characters": [{"name": "...", "gender": "male|female", "appearance": "1 sentence visual description"}],\n'
        '  "locations":  [{"name": "...", "description": "1 sentence about the place"}],\n'
        '  "items":      [{"name": "...", "description": "1 sentence visual description"}]\n'
        '}\n\n'
        "Rules:\n"
        "- characters: every named person who SPEAKS or ACTS. Skip extras and crowd ('официант', 'прохожий').\n"
        "- locations: every distinct setting where action happens. Use INT/EXT slug as the name when present.\n"
        "- items: ONLY plot-relevant objects (the locket revealed at climax, the USB stick with evidence,\n"
        "  the stolen handbag). NOT random props (coffee cups, generic furniture).\n"
        "- Names: prefer the canonical full-name as it first appears in the script.\n"
        "- If an entity matches an already-known name (case-insensitive), use the EXACT known spelling so dedup works.\n"
        "- Empty arrays are valid. No fields beyond schema.\n"
        "- If a WORLD CONVENTION block is present in the user message, OBEY it for the 'appearance' field of every character.\n"
        "- A CASTING & APPEARANCE AESTHETICS block is present in the user message — OBEY it: cast looks by narrative role; leads and any romance/seduction/intimacy role must read as attractive and age-appropriate."
    )
    raw = claude_ask(
        f"Episode script:\n\n{script_text[:18000]}{known_section}{world_block}{casting_block}",
        system=system, model='', max_tokens=2500,
    )
    try:
        return loads_lenient(raw)
    except Exception as e:
        print(f'[import-extract] LLM JSON parse failed: {e}; raw[:400]={raw[:400]!r}', flush=True)
        return {'characters': [], 'locations': [], 'items': []}


def _import_worker(sid, episode_records, create_chars=True, create_locs=True, create_items=True):
    """Background worker: walks every episode, runs one LLM extraction per ep,
    merges results into series.characters/locations/items + ep.characters_used /
    locations_used / items_used. Updates _IMPORT_STATUS as it goes so the UI
    can show progress.

    create_{chars,locs,items}: when False, the worker still RUNS the LLM
    extraction (so per-episode *_used lists get linked to existing roster
    entries by name), but it will NOT create NEW entities of that type. Used
    by the import-from-script flow when the user pre-uploaded their own
    characters/locations and only wants their explicit roster — extracted
    names that don't match existing get silently dropped from *_used.
    """
    st = _import_status(sid)
    st.update({
        'running': True, 'total': len(episode_records), 'done': 0,
        'errors': [], 'started_at': datetime.datetime.utcnow().isoformat(),
        'finished_at': None, 'current': None,
    })
    try:
        for ep_record in episode_records:
            num = ep_record['number']
            st['current'] = f'Эп. {num}'
            try:
                # Reload series each iteration so we get the freshest known set
                # (other ticks may have added entities).
                s = load_series(sid)
                if not s:
                    st['errors'].append({'episode': num, 'error': 'series vanished'})
                    continue
                ep = load_episode(sid, num)
                if not ep:
                    st['errors'].append({'episode': num, 'error': 'episode missing'})
                    continue
                known_chars = {c['name'] for c in s.get('characters', [])}
                known_locs  = {l['name'] for l in s.get('locations', [])}
                known_items = {it['name'] for it in s.get('items', [])}
                # Retry LLM extraction up to 3 times. Common failure modes:
                # - claude returned a markdown-fenced JSON we couldn't parse
                # - rate-limit retry inside claude_ask ran out (rare but happens)
                # - random empty-list result on transient overload
                # Each retry waits a few seconds. If all 3 fail, mark the
                # episode as cast_extracted=True anyway BUT with empty used
                # arrays, and append a clear error so the user knows to retry
                # extraction manually via the «🔁 Принять заново» path.
                extracted = None
                last_err = None
                for try_idx in range(3):
                    try:
                        extracted = _llm_extract_episode_entities(
                            ep.get('script', ''), known_chars, known_locs, known_items, series=s
                        )
                        # A valid response has at least one of the three lists
                        # populated (rare to have an episode with literally no
                        # entities). If all empty, it's almost certainly a
                        # parse error swallowed by the lenient loader.
                        nonempty = (
                            len(extracted.get('characters') or []) +
                            len(extracted.get('locations')  or []) +
                            len(extracted.get('items')      or [])
                        )
                        if nonempty > 0 or len((ep.get('script') or '').strip()) < 200:
                            break  # accept (short scripts may legit have no entities)
                        last_err = 'LLM returned empty entity lists for non-trivial script'
                    except Exception as e:
                        last_err = str(e)[:300]
                        print(f'[import-worker] ep {num} LLM try {try_idx+1}/3 failed: {last_err}', flush=True)
                    if try_idx < 2:
                        time.sleep(3 + try_idx * 2)   # 3s, 5s
                if extracted is None:
                    # All retries threw — leave script as-is, log, skip merge.
                    st['errors'].append({'episode': num, 'error': f'LLM extract failed after 3 tries: {last_err}'})
                    extracted = {'characters': [], 'locations': [], 'items': []}

                # Merge characters
                ep_char_ids = []
                for c in (extracted.get('characters') or [])[:30]:
                    name = (c.get('name') or '').strip()
                    if not name:
                        continue
                    existing = next((x for x in s['characters'] if x['name'].lower() == name.lower()), None)
                    if existing:
                        ep_char_ids.append(existing['id'])
                    elif create_chars:
                        new_c = {
                            'id': str(uuid.uuid4())[:8],
                            'name': name,
                            'description': '',
                            'appearance': (c.get('appearance') or '').strip(),
                            'gender': (c.get('gender') or 'female').lower(),
                            'voice_id': '',
                            'ref_images': [],
                            'outfits': [],
                            'base_outfit_label': 'base',
                        }
                        s['characters'].append(new_c)
                        ep_char_ids.append(new_c['id'])
                    # else: create_chars=False and no roster match → drop

                # Merge locations
                ep_loc_ids = []
                for l in (extracted.get('locations') or [])[:30]:
                    name = (l.get('name') or '').strip()
                    if not name:
                        continue
                    existing = next((x for x in s['locations'] if x['name'].lower() == name.lower()), None)
                    if existing:
                        ep_loc_ids.append(existing['id'])
                    elif create_locs:
                        new_l = {
                            'id': str(uuid.uuid4())[:8],
                            'name': name,
                            'description': (l.get('description') or '').strip(),
                            'ref_images': [],
                            'avai_url': '',
                        }
                        s['locations'].append(new_l)
                        ep_loc_ids.append(new_l['id'])
                    # else: create_locs=False and no roster match → drop

                # Merge items — fuzzy dedup so cross-language re-imports of the
                # same prop don't make duplicates ("Hidden Recorder" / "скрытый
                # диктофон" / "Recording Device" → all collapse to one entry).
                ep_item_ids = []
                for it in (extracted.get('items') or [])[:20]:
                    name = (it.get('name') or '').strip()
                    desc = (it.get('description') or '').strip()
                    if not name:
                        continue
                    # lazy: lives in sw.routes.images_items (module-level import
                    # would shift its route-registration order)
                    from sw.routes.images_items import _fuzzy_find_item
                    existing = _fuzzy_find_item(s['items'], name, desc)
                    if existing:
                        ep_item_ids.append(existing['id'])
                    elif create_items:
                        new_it = {
                            'id': str(uuid.uuid4())[:8],
                            'name': name,
                            'description': (it.get('description') or '').strip(),
                            'ref_images': [],
                            'avai_url': '',
                            'image_constraints': '',
                        }
                        s['items'].append(new_it)
                        ep_item_ids.append(new_it['id'])

                ep['characters_used'] = ep_char_ids
                ep['locations_used']  = ep_loc_ids
                ep['items_used']      = ep_item_ids
                # Mark cast as user-confirmed so the scene-view auto-opens on
                # next page-load — user already "accepted" the script by
                # importing it. Without this flag the FE thinks they still
                # need to click «✅ Принять сценарий» on every episode.
                ep['cast_extracted'] = True
                save_series(sid, s)
                save_episode(sid, num, ep)

                # ── Canon update: per-episode extraction of timeline events,
                # canon facts (locked story-truths), character knowledge state,
                # and open story-threads. Without this the series canon stays
                # empty when user adds episodes via «📜 Добавить сценарий» or
                # «✨ Сгенерировать новые» — only manual /reaccept rebuilds it.
                # Best-effort: failures logged but don't block the worker.
                try:
                    st['current'] = f'Эп. {num} · обновляю канон…'
                    rollback_canon_for_episode(sid, num)   # idempotent re-imports
                    upd = extract_canon_updates(sid, num, ep.get('script', ''))
                    if upd and not upd.get('error'):
                        # Annotate stats so the frontend pipeline banner can
                        # surface canon-update progress if it wants to.
                        st.setdefault('canon', {'updated': 0, 'facts': 0, 'threads': 0, 'errors': 0})
                        st['canon']['updated']  = st['canon'].get('updated', 0) + 1
                        st['canon']['facts']   += int(upd.get('new_facts')   or 0)
                        st['canon']['threads'] += int(upd.get('new_threads') or 0)
                    elif upd and upd.get('error'):
                        st.setdefault('canon', {'updated': 0, 'facts': 0, 'threads': 0, 'errors': 0})
                        st['canon']['errors'] = st['canon'].get('errors', 0) + 1
                        print(f'[import-worker] canon ep{num} error: {upd.get("error")}', flush=True)
                except Exception as e:
                    print(f'[import-worker] canon update ep{num} crashed: {e}', flush=True)
            except Exception as e:
                import traceback
                print(f'[import-worker] ep {num} crashed: {e}', flush=True)
                traceback.print_exc()
                st['errors'].append({'episode': num, 'error': str(e)})
            finally:
                st['done'] += 1
    finally:
        st['running'] = False
        st['current'] = None
        st['finished_at'] = datetime.datetime.utcnow().isoformat()
    # Hand off to autogen sweep: now that every episode has its
    # characters_used / locations_used / items_used populated, fire the asset
    # sweep so portraits / outfit shots / location stills / item images all
    # start generating in the background. Frontend transitions its progress
    # banner to «🎨 Генерация ассетов» when it sees autogen-status running.
    try:
        s = load_series(sid)
        if s and s.get('auto_generate_assets'):
            print(f'[import-worker] {sid}: handoff → autogen sweep', flush=True)
            _spawn_with_keys(auto_generate_missing_assets, sid)
    except Exception as e:
        print(f'[import-worker] autogen handoff failed: {e}', flush=True)


@app.route('/api/series/<sid>/episodes/logic-check-multi', methods=['POST'])
def episodes_logic_check_multi(sid):
    """Cross-episode logic audit on a SELECTED set of already-saved episodes.
    Mirrors /import-from-script/logic-check but reads scripts from disk
    instead of taking a pasted script. Body: {episode_numbers: [int, ...]}.
    Returns the same {issues, episodes_analyzed} shape so the same UI can
    render results."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    body = request.json or {}
    nums = body.get('episode_numbers') or []
    if not isinstance(nums, list) or not nums:
        return jsonify({'error': 'episode_numbers required (non-empty list of ints)'}), 400
    nums = sorted({int(n) for n in nums if isinstance(n, (int, float)) or (isinstance(n, str) and n.strip().isdigit())})
    eps = []
    for n in nums:
        e = load_episode(sid, n)
        if e and (e.get('script') or '').strip():
            eps.append(e)
    if not eps:
        return jsonify({'error': 'у выбранных серий нет сценариев'}), 400
    blocks = []
    for e in eps:
        head = f"--- Episode {e.get('number')}: {(e.get('title') or '').strip()} ---"
        body_txt = (e.get('script') or '')[:18000]
        blocks.append(f"{head}\n{body_txt}")
    joined = '\n\n'.join(blocks)
    system = (
        "You are a strict logic auditor for a short-drama TV series. "
        "Read all episodes in order and find INCONSISTENCIES: "
        "(a) factual contradictions between episodes, "
        "(b) plot holes, "
        "(c) forgotten threads, "
        "(d) character continuity (knowledge/state/location jumps), "
        "(e) timeline errors. "
        "Output STRICT JSON, no prose, no markdown:\n"
        '{"issues": [{"severity":"critical|high|medium|low","type":"contradiction|plot_hole|forgotten_thread|continuity|timeline","episodes":[int],"summary":"...","evidence":"...","fix":"..."}]}\n'
        "No commentary outside JSON. Empty issues list is valid. Output in the language of the script."
    )
    try:
        raw = claude_ask(joined, system=system, max_tokens=4000)
        parsed = loads_lenient(raw)
        issues = parsed.get('issues') if isinstance(parsed, dict) else None
        if not isinstance(issues, list):
            return jsonify({'error': 'LLM returned malformed JSON', 'raw': raw[:400]}), 500
        return jsonify({
            'episodes_analyzed': len(eps),
            'episode_numbers':   [e.get('number') for e in eps],
            'issues':            issues,
        })
    except Exception as e:
        _log_event('WARN', 'logic_check_multi_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/episodes/logic-apply-multi', methods=['POST'])
def episodes_logic_apply_multi(sid):
    """Apply selected logic fixes to a SET of already-saved episodes. Body:
    {episode_numbers: [int...], issues: [{...}]}. Pipeline:
      1. Re-read each episode's script
      2. Build the same Episode-N-headered concat as /logic-check-multi
      3. Ask Claude to rewrite minimally addressing the listed issues
      4. Split the rewritten text back into per-episode scripts
      5. Write each updated script to disk (preserving everything else on ep)
      6. Return per-episode before/after lengths + which were modified
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    body = request.json or {}
    nums = body.get('episode_numbers') or []
    issues = body.get('issues') or []
    if not isinstance(nums, list) or not nums:
        return jsonify({'error': 'episode_numbers required'}), 400
    if not isinstance(issues, list) or not issues:
        return jsonify({'error': 'issues required (non-empty list)'}), 400
    nums = sorted({int(n) for n in nums if isinstance(n, (int, float)) or (isinstance(n, str) and n.strip().isdigit())})

    eps_by_num = {}
    blocks = []
    for n in nums:
        e = load_episode(sid, n)
        if not e or not (e.get('script') or '').strip():
            continue
        eps_by_num[n] = e
        head = f"--- Episode {e.get('number')}: {(e.get('title') or '').strip()} ---"
        blocks.append(f"{head}\n{(e.get('script') or '')[:18000]}")
    if not eps_by_num:
        return jsonify({'error': 'у выбранных серий нет сценариев'}), 400
    joined = '\n\n'.join(blocks)

    fix_lines = []
    for i, it in enumerate(issues, 1):
        if not isinstance(it, dict):
            continue
        eps_ref = it.get('episodes') or []
        fix_lines.append(
            f"{i}. [{(it.get('severity') or '?').upper()}] {it.get('type','?')} · Эп.{','.join(map(str, eps_ref))}\n"
            f"   PROBLEM:  {it.get('summary','')}\n"
            f"   EVIDENCE: {it.get('evidence','')}\n"
            f"   FIX:      {it.get('fix','')}"
        )
    fixes_block = '\n\n'.join(fix_lines) or '(no fixes provided)'
    system = (
        "You are a surgical script editor for a short-drama TV series. "
        "Apply the listed logic-fixes to the MULTI-EPISODE script with MINIMAL edits. "
        "Preserve EVERY '--- Episode N: Title ---' header line exactly. "
        "Preserve every other character and dialogue line verbatim. Only change what's "
        "strictly needed to address each listed issue. Keep the same language as the "
        "original script. Output STRICT JSON, no prose, no markdown:\n"
        '{"script": "full rewritten multi-episode text with \\n line breaks", '
        '"changes": [{"issue_index": int, "summary": "1 sentence what you changed"}]}\n'
        "issue_index is the 1-based number from the input list."
    )
    user_msg = (
        f"=== MULTI-EPISODE SCRIPT TO PATCH ===\n{joined[:90000]}\n\n"
        f"=== ISSUES TO FIX ===\n{fixes_block}\n\n"
        "Return the corrected full multi-episode text + per-issue change summary. JSON only."
    )
    try:
        raw = claude_ask(user_msg, system=system, max_tokens=20000)
        parsed = loads_lenient(raw)
        new_script = parsed.get('script') if isinstance(parsed, dict) else None
        changes = parsed.get('changes') if isinstance(parsed, dict) else []
        if not isinstance(new_script, str) or not new_script.strip():
            return jsonify({'error': 'LLM returned no script', 'raw': raw[:400]}), 500
    except Exception as e:
        _log_event('WARN', 'logic_apply_multi_llm_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500

    # Split rewritten multi-episode text by «--- Episode N: ... ---» headers.
    # Tolerant: matches lines starting with «---» that have «Episode <N>» token.
    pattern = re.compile(r'^\s*---\s*Episode\s+(\d+)[^\n-]*---\s*$', re.IGNORECASE | re.MULTILINE)
    matches = list(pattern.finditer(new_script))
    if not matches:
        # Fallback — maybe Claude dropped the «---» fences. Try plain «Episode N:» markers.
        pattern2 = re.compile(r'(?im)^[ \t]*episode[ \t]+(\d+)[ \t]*[:\-—]?[^\n]*$')
        matches = list(pattern2.finditer(new_script))
        if not matches:
            return jsonify({'error': 'Не удалось разбить переписанный сценарий по сериям — Claude сломал разметку. Попробуй ещё раз или применяй фиксы по одному.'}), 500

    updated = []
    skipped = []
    for i, m in enumerate(matches):
        try:
            num = int(m.group(1))
        except (ValueError, IndexError):
            continue
        if num not in eps_by_num:
            skipped.append({'number': num, 'reason': 'not in selected set'})
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(new_script)
        body_txt = new_script[start:end].strip()
        if not body_txt:
            skipped.append({'number': num, 'reason': 'empty body'})
            continue
        ep = eps_by_num[num]
        before_len = len(ep.get('script') or '')
        # Archive previous script as a history entry so the user can revert.
        try:
            history = ep.setdefault('script_history', [])
            history.append({
                'script': ep.get('script') or '',
                'saved_at': datetime.datetime.utcnow().isoformat(),
                'reason': 'logic-apply-multi',
            })
            ep['script_history'] = history[-15:]   # cap history
        except Exception:
            pass
        ep['script'] = body_txt
        end_pos = _extract_end_position(body_txt)
        if end_pos:
            ep['end_position'] = end_pos
        save_episode(sid, num, ep)
        updated.append({'number': num, 'before_len': before_len, 'after_len': len(body_txt)})

    return jsonify({
        'updated':       updated,
        'skipped':       skipped,
        'applied_count': len(issues),
        'changes':       changes if isinstance(changes, list) else [],
    })


@app.route('/api/series/import-from-script/logic-check', methods=['POST'])
def import_from_script_logic_check():
    """Cross-episode logic audit BEFORE creating/appending. Splits the pasted
    script the same way as /preview, then asks Claude to read every episode in
    order and surface inconsistencies — contradictions, plot holes, forgotten
    threads, character continuity issues. Returns structured list of issues
    with severity + episode references. The user fixes the script in the
    textarea and re-runs, or accepts as-is and clicks «Добавить серии»."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    eps = _split_script_into_episodes(script)
    if not eps:
        return jsonify({'error': 'не удалось разбить сценарий на серии'}), 400
    # Cap to first 18000 chars per episode to keep prompt sane on huge series.
    blocks = []
    for e in eps:
        head = f"--- Episode {e['number']}: {e.get('title') or ''} ---"
        body = (e.get('body') or '')[:18000]
        blocks.append(f"{head}\n{body}")
    joined = '\n\n'.join(blocks)
    system = (
        "You are a strict logic auditor for a short-drama TV series. "
        "Read all episodes in order and find INCONSISTENCIES: "
        "(a) factual contradictions between episodes (character was dead, then alive), "
        "(b) plot holes (an action has no setup or no consequence), "
        "(c) forgotten threads (a question/promise/item introduced and never resolved), "
        "(d) character continuity (knowledge/state/location jumps without explanation), "
        "(e) timeline errors (event order impossible). "
        "Output STRICT JSON, no prose, no markdown:\n"
        '{\n'
        '  "issues": [\n'
        '    {\n'
        '      "severity": "critical|high|medium|low",\n'
        '      "type":     "contradiction|plot_hole|forgotten_thread|continuity|timeline",\n'
        '      "episodes": [int, int],   // episode numbers involved\n'
        '      "summary":  "1 sentence — what is wrong",\n'
        '      "evidence": "short direct quote(s) showing it",\n'
        '      "fix":      "1 concrete suggestion how to fix"\n'
        '    }\n'
        '  ]\n'
        '}\n'
        'No commentary outside JSON. Empty issues list is valid. Output in the language of the script (Russian if Russian, English if English).'
    )
    try:
        raw = claude_ask(joined, system=system, max_tokens=4000)
        parsed = loads_lenient(raw)
        issues = parsed.get('issues') if isinstance(parsed, dict) else None
        if not isinstance(issues, list):
            return jsonify({'error': 'LLM returned malformed JSON', 'raw': raw[:400]}), 500
        return jsonify({
            'episodes_analyzed': len(eps),
            'issues': issues,
        })
    except Exception as e:
        _log_event('WARN', 'logic_check_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/import-from-script/apply-fixes', methods=['POST'])
def import_from_script_apply_fixes():
    """Apply selected logic-check fixes to the script. Body:
      {script: str, issues: [{summary, type, episodes, evidence, fix, ...}]}
    Claude rewrites the script with MINIMAL edits — only addressing the listed
    issues, preserving everything else verbatim. Returns {script: <new>,
    changes_summary: <1-line per issue what was changed>}.
    UI puts the rewritten text back into the textarea and lets the user
    re-run logic-check until clean."""
    data = request.json or {}
    script = (data.get('script') or '').strip()
    issues = data.get('issues') or []
    if not script:
        return jsonify({'error': 'script required'}), 400
    if not issues or not isinstance(issues, list):
        return jsonify({'error': 'issues required (non-empty list)'}), 400
    # Render the fix-list as compact instructions for Claude.
    fix_lines = []
    for i, it in enumerate(issues, 1):
        if not isinstance(it, dict):
            continue
        eps = it.get('episodes') or []
        fix_lines.append(
            f"{i}. [{it.get('severity','?').upper()}] {it.get('type','?')} · Эп.{','.join(map(str, eps))}\n"
            f"   PROBLEM:  {it.get('summary','')}\n"
            f"   EVIDENCE: {it.get('evidence','')}\n"
            f"   FIX:      {it.get('fix','')}"
        )
    fixes_block = '\n\n'.join(fix_lines) or '(no fixes provided)'
    system = (
        "You are a surgical script editor for a short-drama TV series. "
        "Apply the listed logic-fixes to the script with MINIMAL edits. "
        "Preserve episode boundaries (lines like «Episode 17: Title»), preserve every "
        "other character and dialogue line verbatim. Only change what's strictly "
        "needed to address each listed issue (rewrite, add 1-2 lines for setup, "
        "remove a contradictory line — whichever is most surgical). "
        "Keep the same language as the original script (Russian if Russian, English if English). "
        "Output STRICT JSON, no prose, no markdown:\n"
        '{\n'
        '  "script":  "the full rewritten script as one string with \\n line breaks",\n'
        '  "changes": [{"issue_index": int, "summary": "1 sentence what you changed"}]\n'
        '}\n'
        "issue_index is the 1-based number from the input list."
    )
    user_msg = (
        f"=== SCRIPT TO PATCH ===\n{script[:60000]}\n\n"
        f"=== ISSUES TO FIX ===\n{fixes_block}\n\n"
        "Return the corrected full script + a short list of what you changed. JSON only."
    )
    try:
        raw = claude_ask(user_msg, system=system, max_tokens=16000)
        parsed = loads_lenient(raw)
        new_script = parsed.get('script') if isinstance(parsed, dict) else None
        changes = parsed.get('changes') if isinstance(parsed, dict) else []
        if not isinstance(new_script, str) or not new_script.strip():
            return jsonify({'error': 'LLM returned no script', 'raw': raw[:400]}), 500
        return jsonify({
            'script':  new_script,
            'changes': changes if isinstance(changes, list) else [],
            'applied_count': len(issues),
        })
    except Exception as e:
        _log_event('WARN', 'logic_fix_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
