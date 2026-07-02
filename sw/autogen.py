"""Background auto-generation sweep: fills missing character/location/item
assets after script generation."""
import re
import time
import uuid
from pathlib import Path

from flask import jsonify, request

from sw.anthro import (_anthro_preflight, _detect_animal_species,
                       _is_anthro_world, _llm_infer_species_for_char,
                       _patch_appearance_with_species)
from sw.auth import _spawn_with_keys, _thread_keys
from sw.avai import _series_image_provider, avai_generate
from sw.core import app
from sw.era import (_clothing_clause, _modern_document_directive,
                    _no_caption_text_clause, _series_era_hint,
                    _strip_cast_names_for_visual)
from sw.logging_utils import _log_event
from sw.storage import (_expand_outfit_label_to_desc, _sync_script_outfits,
                        assets_dir, list_episodes, load_series, save_series,
                        series_path)
from sw.style import _series_style_clause, _series_visual_style
from sw.textrules_banlists import _outfit_hair_phrase, _override_hair_in_appearance
from sw.textrules_sanitizer import _sanitize_appearance_for_moderation
from sw.utils import asset_name, slugify


def auto_facades_for_new_locations(sid):
    # lazy proxy: facades cluster is extracted separately
    try:
        from sw.routes.facades import auto_facades_for_new_locations as f
    except ImportError:
        from app import auto_facades_for_new_locations as f
    return f(sid)


def sync_episode_with_cast_block(sid, num):
    # lazy: lives in sw.routes.scripts (importing at module level would be routes->routes noise)
    from sw.routes.scripts import sync_episode_with_cast_block as f
    return f(sid, num)

# ── Auto-generate missing assets (background sweep) ──────────────────────────

import threading
import concurrent.futures

# Per-series lock so a sweep doesn't run twice in parallel for the same series
_AUTOGEN_LOCKS = {}
_AUTOGEN_STATUS = {}  # sid -> {'running': bool, 'queue': int, 'done': int, 'errors': [], 'in_progress': [...]}

def _autogen_status(sid):
    return _AUTOGEN_STATUS.setdefault(sid, {
        'running': False, 'queue': 0, 'done': 0, 'errors': [],
        # in_progress: list of {kind, parent_id, child_id?, name} entries currently
        # being generated. Frontend uses this to show spinners on specific cards.
        'in_progress': [],
    })

def _gen_char_base_inline(s, sid, char):
    """Generate base ref for character. Mutates s, saves at end."""
    if char.get('ref_images'):
        return
    constraints = (char.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    appearance = (char.get('appearance') or '').strip()
    # Scrub the canonical appearance at creation time too: it's persisted and
    # rides into every later prompt/BINDING. The one-off spicy intent stays in
    # image_constraints (constraints_clause), which is NOT persisted into appearance.
    _appearance_clean = _sanitize_appearance_for_moderation(appearance)
    if _appearance_clean != appearance:
        char['appearance'] = _appearance_clean
        appearance = _appearance_clean
    description = (char.get('description') or '').strip()
    # Detect anthropomorphic species — check NAME first (catches «Wolf»/
    # «Hyena»/«Fox Woman» where appearance text was written as «a man in
    # worn-out clothes»), fall back to appearance keywords for human-named
    # chars described with fur/muzzle markers.
    species_hint = _detect_animal_species(char.get('name'), appearance, series=s)
    # Self-heal for anthro worlds — same logic as generate_character_image.
    # Catches the auto_generate_assets path where chars came from extractors
    # without species in appearance.
    if not species_hint and _is_anthro_world(s):
        inferred = _llm_infer_species_for_char(s, char)
        if inferred and inferred != 'human':
            patched = _patch_appearance_with_species(appearance, inferred, char.get('gender', ''))
            if patched and patched != appearance:
                char['appearance'] = patched
                appearance = patched
                print(f'[anthro-heal] inline: char {char.get("name")} → species={inferred}', flush=True)
            species_hint = _detect_animal_species(char.get('name'), appearance, series=s)
    is_animal = bool(species_hint)
    species_override = ''
    if is_animal:
        gender_word = 'female' if char.get('gender') == 'female' else 'male'
        kind_label = f', a {gender_word} {species_hint}'
        species_override = (
            f" CRITICAL: {char['name']} is an ANTHROPOMORPHIC {species_hint.split()[-1].upper()}, "
            f"NOT a human. The character has a {species_hint.split()[-1]}'s head/face "
            f"(realistic snout, ears, eyes typical of the species) with appropriate fur/feathers/scales, "
            f"walking upright with anthropomorphic body proportions, wearing human-style clothing. "
            f"Zootopia/Pixar-style anthropomorphic animal — DO NOT render as a plain human. "
            f"Ignore any wording like «a man» / «a woman» in the description above — those describe "
            f"the character's gender role, not human anatomy."
        )
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        kind_label = f', a {gender}'
    # Style: project's visual_style overrides default photorealistic. Pixar/anime/etc
    # require explicit style directive AND removal of "Photorealistic" suffix —
    # otherwise model gets conflicting signals and renders human-looking realism.
    style_clause = _series_style_clause(s)
    visual_style = _series_visual_style(s)
    era_clause = _series_era_hint(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality, high detail on face and clothing.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    clothing_fallback = '' if is_animal else _clothing_clause(appearance, description, era_hint=era_clause)
    prompt = (
        f"{style_prefix}"
        f"Full body portrait of {char['name']}{kind_label}. "
        f"{appearance}. {description}.{constraints_clause}{species_override} "
        f"{era_clause + ' ' if era_clause else ''}"
        f"{clothing_fallback}"
        f"Standing facing camera, slight 3/4 angle. Neutral relaxed pose. "
        f"Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. "
        f"STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. "
        f"Soft even diffused illumination on the character only (lighting source NOT visible in frame), no harsh shadows on face or body, no visible lights or equipment."
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / f'{asset_name(char["name"], "BASE")}.jpg'
    # Race-safe path: write to a unique temp file FIRST, never the canonical
    # path directly. The autogen orchestrator (_run_task) atomically moves
    # the temp into place ONLY if no concurrent user-driven regenerate has
    # populated `char['ref_images']` in the meantime.
    # Without this, the following sequence corrupts user state:
    #   T0  autogen worker reads stale snapshot (ref_images empty)
    #   T0+5  autogen avai_generate writes canonical CATHERINE_BASE.jpg
    #   T0+10 user clicks Regenerate, regen avai_generate ALSO writes
    #         canonical CATHERINE_BASE.jpg (user's version)
    #   T0+15 autogen avai_generate (slow API for a different concurrent
    #         worker) finishes and overwrites CATHERINE_BASE.jpg with the
    #         stale autogen image — silently undoing the user's regen on
    #         disk. User-reported on series «Six Weeks After the Gala».
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f'.autogen-{uuid.uuid4().hex[:8]}-{out_path.name}')
    image_url = avai_generate(prompt, tmp_path, preferred_provider=_series_image_provider(s))
    rel_path = str(out_path.relative_to(series_path(sid)))
    # Stash the staging info on the char so _run_task can commit it under lock.
    # `_autogen_pending` is intentionally NOT persisted to disk — orchestrator
    # consumes it before any save_series.
    char['_autogen_pending'] = {
        'tmp_path': str(tmp_path),
        'canonical_path': str(out_path),
        'rel_path': rel_path,
        'image_url': image_url,
    }

def _gen_outfit_inline(s, sid, char, outfit):
    """Generate outfit photo via i2i from char base. Mutates outfit."""
    if outfit.get('photo'):
        return
    if outfit.get('is_base') and char.get('ref_images'):
        outfit['photo'] = char['ref_images'][0]
        outfit['avai_url'] = char.get('avai_base_url', '')
        return
    if not char.get('ref_images'):
        raise RuntimeError(f'Char "{char["name"]}" has no base ref yet')
    reference_url = char.get('avai_base_url')
    constraints = (char.get('image_constraints') or '').strip()
    constraints_clause = f' IMPORTANT — strictly follow these constraints: {constraints}. ' if constraints else ''
    appearance = (char.get('appearance') or '').strip()
    appearance_low = appearance.lower()
    animal_words = ('fur', 'muzzle', 'snout', 'tail', 'paws', 'claws', 'whiskers',
                    'mane', 'feathers', 'beak', 'horns', 'antlers', 'hooves', 'scales',
                    'cub', 'pup', 'kitten', 'fang', 'fangs',
                    'шерсть', 'мордa', 'морду', 'морды', 'хвост', 'лапы', 'когти',
                    'клыки', 'грива', 'перья', 'клюв', 'рога', 'копыта')
    is_animal = any(w in appearance_low for w in animal_words)
    if is_animal:
        same_clause = 'Same character as the reference image (same species, same fur/markings, same age). '
        intro_clause = f'Full body portrait of {char["name"]}. {appearance}. '
    else:
        gender = 'woman' if char.get('gender') == 'female' else 'man'
        same_clause = f'Same {gender} as the reference image. '
        intro_clause = f'Full body portrait of {char["name"]}, a {gender}. {appearance}. '
    # Alternate-identity HAIR: a disguise look (dyed / wig / new identity) must be
    # ALLOWED to change the hair. The default i2i instruction below hard-locks hair
    # to the base portrait ("only the clothing changes"), which is exactly why a
    # "blonde" disguise rendered brunette. When the look declares a hair override,
    # keep only the FACE identical and restyle the hair instead.
    hair_phrase = _outfit_hair_phrase(outfit)
    changes_hair = bool(hair_phrase) and not is_animal
    if changes_hair:
        if reference_url:
            same_clause = (f'Same {gender} as the reference image — keep the EXACT same face, '
                           f'facial features and bone structure. ')
        else:
            # No base ref: render from appearance, but with the disguised hair.
            intro_clause = (f'Full body portrait of {char["name"]}, a {gender}. '
                            f'{_override_hair_in_appearance(appearance, hair_phrase)}. ')
        hair_change_clause = (
            f'IMPORTANT — this is a deliberate new look / disguise: the HAIR is now {hair_phrase}. '
            f'Restyle the hair to {hair_phrase}; do NOT keep the reference hair colour or style. '
            f'The FACE stays identical — only the hair and wardrobe change. '
        )
        clothing_lock_clause = ''
    else:
        hair_change_clause = ''
        clothing_lock_clause = 'Same face, same body — only the clothing changes. ' if reference_url else ''
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    prompt = (
        f"{style_prefix}"
        + (same_clause if reference_url else intro_clause)
        + f'Now wearing: {outfit["label"]}. {outfit.get("description", "")}. '
        + clothing_lock_clause
        + hair_change_clause
        + constraints_clause
        + 'Full body, front-facing, slight 3/4 angle. Neutral relaxed pose. '
          'Arms hanging loosely at sides, hands open and empty — no objects held, no props, not in pockets. '
          'STRICT BACKGROUND: ONLY a flat featureless solid gray (#808080) backdrop behind the character — a uniform color field, NOT a photo studio set. ABSOLUTELY NO windows, doors, walls, room interiors, furniture, plants, objects, decor, outdoor scenes, NO photography studio elements (NO lighting rigs, NO trusses, NO backdrop curtains with visible seams, NO floor-to-wall transition, NO studio equipment), or any environmental elements whatsoever. Character must be isolated against the flat gray field — no setting, no architecture, no context. No shadows or reflections on the background. '
          'Soft even diffused illumination on the character only (no visible lights or equipment).'
        + realism_suffix
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    char_slug = slugify(char['name'])
    out_path = assets_dir(sid) / 'characters' / char_slug / 'outfits' / f'{asset_name(char["name"], outfit["label"])}.jpg'
    image_url = avai_generate(prompt, out_path, reference_url=reference_url, preferred_provider=_series_image_provider(s))
    outfit['photo'] = str(out_path.relative_to(series_path(sid)))
    outfit['avai_url'] = image_url

def _gen_loc_inline(s, sid, loc):
    if loc.get('ref_images'):
        return
    tone = s.get('tone', '')
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, cinematic quality, high detail.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    loc_name = _strip_cast_names_for_visual(loc['name'], s)
    loc_desc = _strip_cast_names_for_visual(loc.get('description', ''), s)
    prompt = (
        f"{style_prefix}"
        f"{loc_name}. {loc_desc}. "
        f"No people, no characters in frame. "
        f"{(tone + ' atmosphere. ') if tone else ''}"
        f"Cinematic wide establishing shot. Horizontal landscape composition, 16:9 framing. "
        f"Atmospheric lighting.{_no_caption_text_clause()}"
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    loc_slug = slugify(loc['name'])
    out_path = assets_dir(sid) / 'locations' / loc_slug / f'{asset_name(loc["name"])}.jpg'
    image_url = avai_generate(prompt, out_path, aspect_ratio='16:9', preferred_provider=_series_image_provider(s))
    loc.setdefault('ref_images', []).insert(0, str(out_path.relative_to(series_path(sid))))


def _gen_item_inline(s, sid, item):
    """Generate ref image for a story-prop item. Mutates item, caller saves.
    Uses square 1:1 product-still-life style — works as portable ref for both
    Seedance (9:16) and Reteller (vertical) compositions.
    Idempotent: skips if item already has refs."""
    if item.get('ref_images'):
        return
    style_clause = _series_style_clause(s)
    is_stylised = bool(style_clause and 'strict' in style_clause.lower())
    realism_suffix = '' if is_stylised else ' Photorealistic, high detail.'
    style_prefix = (style_clause + ' ') if style_clause else ''
    constraints = (item.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    modern_doc_clause = _modern_document_directive(item)
    no_text_clause = '' if modern_doc_clause else _no_caption_text_clause(constraints)
    it_name = _strip_cast_names_for_visual(item['name'], s)
    it_desc = _strip_cast_names_for_visual(item.get('description', ''), s)
    prompt = (
        f"{style_prefix}"
        f"{it_name}. {it_desc}.{constraints_clause}{modern_doc_clause}{no_text_clause} "
        f"Product-style still-life of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless gray background (#dadada) — flat color field NOT a photo studio set (no lighting rigs, no trusses, no equipment visible), soft even diffused illumination on the subject only, "
        f"subtle shadow on ground, sharp focus on object texture and details. "
        f"Square 1:1 framing."
        f"{realism_suffix}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    item_slug = slugify(item['name'])
    out_path = assets_dir(sid) / 'items' / item_slug / f'{asset_name(item["name"])}.jpg'
    image_url = avai_generate(prompt, out_path, aspect_ratio='1:1', preferred_provider=_series_image_provider(s))
    rel_path = str(out_path.relative_to(series_path(sid)))
    item.setdefault('ref_images', []).insert(0, rel_path)
    item['avai_url'] = image_url


_AUTOGEN_SAVE_LOCKS: dict[str, threading.Lock] = {}
_AUTOGEN_PARALLELISM = 4

def auto_generate_missing_assets(sid):
    """Background sweep: generate base photos for chars without refs,
    outfit photos for outfits without photos, location refs for locations without refs.
    Pipeline:
      Phase 1 — char-bases in parallel (outfits need them as i2i refs)
      Phase 2 — outfits + locations in parallel (independent of each other)
    Each worker calls the slow avai_generate API outside any lock; only the
    final read-modify-write of series.json runs under a per-series save-lock,
    so 4 concurrent workers can image-gen at once without trashing the file.
    Idempotent — skips anything already generated.
    Also pre-syncs every episode's cast block so chars/outfits referenced in scripts
    but missing from series.json get created before the sweep runs."""
    lock = _AUTOGEN_LOCKS.setdefault(sid, threading.Lock())
    if not lock.acquire(blocking=False):
        print(f'[autogen {sid}] already running, skipping')
        return
    save_lock = _AUTOGEN_SAVE_LOCKS.setdefault(sid, threading.Lock())
    st = _autogen_status(sid)
    st.update({'running': True, 'queue': 0, 'done': 0, 'errors': [], 'in_progress': []})
    try:
        # ─── Pre-sweep: ensure every episode's cast block AND [BLOCKING] outfits
        # are reflected in series.json. This is the GUARANTEE that no outfit
        # mentioned in any saved script gets missed — even if the path that
        # saved the script (import worker, history restore, direct file edit,
        # legacy versions before sync was hooked everywhere) forgot to call
        # _sync_script_outfits. The autogen sweep runs whenever any asset
        # generation is requested, so this acts as the final reconciliation
        # layer. Idempotent: existing outfits match by label and are skipped.
        # Skip episodes where extraction hasn't been confirmed by the user yet.
        for ep in list_episodes(sid):
            if not (ep.get('script') or '').strip():
                continue
            if ep.get('cast_extracted', True) is False:
                continue
            try:
                sync_episode_with_cast_block(sid, ep['number'])
            except Exception as e:
                print(f'[autogen {sid}] cast-block sync ep{ep["number"]} failed: {e}')
            # ALSO sync [BLOCKING] outfits — separate from cast-block sync.
            # This is the layer that catches scenarios like Margaret's missing
            # Prison Jumpsuit in ep 21 (script had the OUTFIT line but no save
            # path triggered the sync). Note: _sync_script_outfits internally
            # tries to _spawn_with_keys(auto_generate_missing_assets, sid)
            # again — that nested spawn is BLOCKED by _AUTOGEN_LOCKS lock,
            # so no recursion; the newly-created outfits will be picked up
            # by THIS sweep's task list (built right below after a reload).
            try:
                _sync_script_outfits(sid, ep.get('script') or '')
            except Exception as e:
                print(f'[autogen {sid}] blocking-outfit sync ep{ep["number"]} failed: {e}')

        s = load_series(sid)
        if not s: return
        # Build task list — phased
        char_tasks = []
        outfit_tasks = []
        loc_tasks = []
        # `_skip_autogen=true` on a char/loc/item explicitly opts out of the
        # sweep (set via the Accept-script modal's "🚫 Не генерить эту группу"
        # checkbox). Honored at task-build time — the entity is never queued
        # so it stays empty until the user clicks generate manually later.
        for c in s.get('characters', []):
            if c.get('_skip_autogen'):
                continue
            if not c.get('ref_images'):
                char_tasks.append(('char', c['id'], None))
        for c in s.get('characters', []):
            if c.get('_skip_autogen'):
                continue
            for o in c.get('outfits', []):
                if o.get('_skip_autogen'):
                    continue
                if not o.get('photo') and not o.get('is_base'):
                    outfit_tasks.append(('outfit', c['id'], o['id']))
        for l in s.get('locations', []):
            if l.get('_skip_autogen'):
                continue
            if not l.get('ref_images'):
                loc_tasks.append(('loc', l['id'], None))
        item_tasks = []
        for it in s.get('items', []):
            if it.get('_skip_autogen'):
                continue
            if not it.get('ref_images'):
                item_tasks.append(('item', it['id'], None))
        total_tasks = len(char_tasks) + len(outfit_tasks) + len(loc_tasks) + len(item_tasks)
        st['queue'] = total_tasks
        print(f'[autogen {sid}] {total_tasks} assets to generate '
              f'(chars={len(char_tasks)}, outfits={len(outfit_tasks)}, locs={len(loc_tasks)}, items={len(item_tasks)}, parallel={_AUTOGEN_PARALLELISM})',
              flush=True)

        def _ip_add(entry):
            # status['in_progress'] is a plain list — protect with the save_lock
            with save_lock:
                st['in_progress'].append(entry)

        def _ip_remove(parent_id, child_id):
            with save_lock:
                st['in_progress'] = [e for e in st['in_progress']
                                      if not (e.get('parent_id') == parent_id and e.get('child_id') == child_id)]

        # Snapshot the user-keys context HERE while we're still on the parent
        # thread (which had keys propagated by _spawn_with_keys). Each child
        # worker copies this into its own threading.local at the top of its
        # task — without this, ThreadPoolExecutor's child threads run with an
        # empty _thread_keys and every helper that calls user_root() /
        # current_user_email() / _get_user_avai_key() crashes with
        # "Working outside of request context". This was the root cause of the
        # "[autogen ...] FAILED item/...: Working outside of request context"
        # spam — items don't have any other auth-attached call paths so they
        # showed it loudest, but chars/locs would have hit it too if they
        # didn't already have refs (ref_images guard skips before keys are used).
        _ctx_email     = getattr(_thread_keys, 'email', None)
        _ctx_avai_key  = getattr(_thread_keys, 'avai_key', None)
        _ctx_rtl_key   = getattr(_thread_keys, 'reteller_key', None)

        def _run_task(kind, parent_id, child_id):
            # Apply captured user-keys context to THIS worker thread.
            _thread_keys.email        = _ctx_email
            _thread_keys.avai_key     = _ctx_avai_key
            _thread_keys.reteller_key = _ctx_rtl_key
            ip_entry = None
            try:
                # Re-load LOCALLY so each worker has a fresh read for its mutation
                s_local = load_series(sid)
                if not s_local:
                    return
                if kind == 'char':
                    char = next((c for c in s_local.get('characters', []) if c['id'] == parent_id), None)
                    if not char or char.get('ref_images'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'char', 'parent_id': parent_id, 'child_id': None, 'name': char.get('name', '')}
                    _ip_add(ip_entry)
                    _gen_char_base_inline(s_local, sid, char)  # SLOW: avai API call → writes to temp path
                    pending = char.get('_autogen_pending') or {}
                    tmp_p   = Path(pending.get('tmp_path', ''))
                    canon_p = Path(pending.get('canonical_path', ''))
                    new_url = pending.get('image_url', '')
                    new_rel = pending.get('rel_path', '')
                    if not tmp_p or not tmp_p.exists():
                        return  # generation failed before producing a file
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk:
                            tmp_p.unlink(missing_ok=True)
                            return
                        c_disk = next((c for c in s_disk.get('characters', []) if c['id'] == parent_id), None)
                        if c_disk and not c_disk.get('ref_images'):
                            # Commit: rename temp → canonical, persist series.json
                            try:
                                canon_p.parent.mkdir(parents=True, exist_ok=True)
                                tmp_p.replace(canon_p)  # atomic on same filesystem
                            except Exception as e:
                                _log_event('WARN', 'autogen_char_commit_failed',
                                           char_id=parent_id, err=str(e)[:200])
                                tmp_p.unlink(missing_ok=True)
                                return
                            c_disk['ref_images'] = [new_rel]
                            c_disk['avai_base_url'] = new_url
                            c_disk['updated_at'] = int(time.time())
                            save_series(sid, s_disk)
                        else:
                            # User regen / manual upload already populated this
                            # char between our stale snapshot and this commit.
                            # Discard our work — keep the user's version intact.
                            tmp_p.unlink(missing_ok=True)
                            _log_event('INFO', 'autogen_char_skipped_by_user_regen',
                                       char_id=parent_id, name=char.get('name', ''))
                elif kind == 'outfit':
                    char = next((c for c in s_local.get('characters', []) if c['id'] == parent_id), None)
                    if not char: return
                    outfit = next((o for o in char.get('outfits', []) if o['id'] == child_id), None)
                    if not outfit or outfit.get('photo'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'outfit', 'parent_id': parent_id, 'child_id': child_id,
                                'name': f"{char.get('name','')}/{outfit.get('label','')}"}
                    _ip_add(ip_entry)
                    _gen_outfit_inline(s_local, sid, char, outfit)  # SLOW: avai i2i call
                    new_photo = outfit.get('photo') or ''
                    new_url   = outfit.get('avai_url') or ''
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        c_disk = next((c for c in s_disk.get('characters', []) if c['id'] == parent_id), None)
                        if not c_disk: return
                        o_disk = next((o for o in c_disk.get('outfits', []) if o['id'] == child_id), None)
                        if o_disk and not o_disk.get('photo'):
                            o_disk['photo'] = new_photo
                            o_disk['avai_url'] = new_url
                            save_series(sid, s_disk)
                elif kind == 'loc':
                    loc = next((l for l in s_local.get('locations', []) if l['id'] == parent_id), None)
                    if not loc or loc.get('ref_images'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'loc', 'parent_id': parent_id, 'child_id': None, 'name': loc.get('name', '')}
                    _ip_add(ip_entry)
                    _gen_loc_inline(s_local, sid, loc)  # SLOW: avai API call
                    new_refs = loc.get('ref_images') or []
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        l_disk = next((l for l in s_disk.get('locations', []) if l['id'] == parent_id), None)
                        if l_disk and not l_disk.get('ref_images'):
                            l_disk['ref_images'] = new_refs
                            save_series(sid, s_disk)
                elif kind == 'item':
                    item = next((it for it in s_local.get('items', []) if it['id'] == parent_id), None)
                    if not item or item.get('ref_images'):
                        st['done'] += 1
                        return
                    ip_entry = {'kind': 'item', 'parent_id': parent_id, 'child_id': None, 'name': item.get('name', '')}
                    _ip_add(ip_entry)
                    _gen_item_inline(s_local, sid, item)  # SLOW: avai API call
                    new_refs = item.get('ref_images') or []
                    new_url = item.get('avai_url') or ''
                    with save_lock:
                        s_disk = load_series(sid)
                        if not s_disk: return
                        i_disk = next((it for it in s_disk.get('items', []) if it['id'] == parent_id), None)
                        if i_disk and not i_disk.get('ref_images'):
                            i_disk['ref_images'] = new_refs
                            i_disk['avai_url'] = new_url
                            save_series(sid, s_disk)
                st['done'] += 1
            except Exception as e:
                err_msg = f'{kind}/{parent_id}: {str(e)[:200]}'
                st['errors'].append(err_msg)
                print(f'[autogen {sid}] FAILED {err_msg}', flush=True)
            finally:
                _ip_remove(parent_id, child_id)
                # Clear the worker-thread's _thread_keys so a recycled pool
                # thread doesn't leak the previous task's user context into a
                # later task (or another series's sweep on the same process).
                _thread_keys.email        = None
                _thread_keys.avai_key     = None
                _thread_keys.reteller_key = None

        # Phase 1: char bases — outfits depend on these
        if char_tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_AUTOGEN_PARALLELISM) as pool:
                list(pool.map(lambda t: _run_task(*t), char_tasks))
        # Phase 2: outfits + locations + items together (none depend on chars)
        phase2 = outfit_tasks + loc_tasks + item_tasks
        if phase2:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_AUTOGEN_PARALLELISM) as pool:
                list(pool.map(lambda t: _run_task(*t), phase2))
    finally:
        st['running'] = False
        st['in_progress'] = []
        lock.release()
        print(f'[autogen {sid}] done — {st["done"]}/{st["queue"]} ok, {len(st["errors"])} errors')
        # Re-check: if new chars/outfits/locs appeared during the sweep (e.g. a parallel
        # extract-characters or sync_episode_with_cast_block added rows mid-flight), the
        # current run has already left them un-generated. Kick off another sweep so the
        # user doesn't have to click "regenerate" manually.
        try:
            s2 = load_series(sid)
            if s2 and s2.get('auto_generate_assets'):
                # NB: must mirror the _skip_autogen logic from task-collection
                # above. Otherwise entities the user explicitly opted-out of
                # auto-gen (via "🚫 Не генерить" tickbox in the Accept-script
                # modal) are counted as pending forever — sweep finishes with
                # queue=0 (everything skipped at task-build time), recheck sees
                # pending>0 (skip flag ignored here), spawns another sweep,
                # loops infinitely. Symptom on the frontend: the heartbeat
                # catches every brief running=true blip and the «ничего не
                # нужно генерить» line keeps re-painting under the button.
                pending = (
                    sum(1 for c in s2.get('characters', [])
                        if not c.get('ref_images') and not c.get('_skip_autogen')) +
                    sum(1 for c in s2.get('characters', []) if not c.get('_skip_autogen')
                        for o in c.get('outfits', [])
                        if not o.get('photo') and not o.get('is_base') and not o.get('_skip_autogen')) +
                    sum(1 for l in s2.get('locations', [])
                        if not l.get('ref_images') and not l.get('_skip_autogen')) +
                    sum(1 for it in s2.get('items', [])
                        if not it.get('ref_images') and not it.get('_skip_autogen'))
                )
                if pending > 0:
                    print(f'[autogen {sid}] {pending} new assets queued during sweep — re-running')
                    # We're already inside a thread-local-scoped worker (spawned via
                    # _spawn_with_keys), so _capture_user_keys() reads the propagated
                    # keys and forwards them to the recursive sweep.
                    _spawn_with_keys(auto_generate_missing_assets, sid)
                # ALWAYS kick off facade auto-grouping after a sweep — not gated
                # on pending==0. If a recursive sweep is also spawned above,
                # auto_facades_for_new_locations is idempotent (its own running-
                # flag check + no-op when no ungrouped locs) so the duplicate
                # spawn is harmless. Without this, a sweep that ends with
                # pending>0 only fires facades on the recursive tail — which
                # could fail silently (Claude outage, key issue) and never
                # retry. Firing here too gives us a second chance.
                try:
                    _spawn_with_keys(auto_facades_for_new_locations, sid)
                except Exception as e:
                    print(f'[autogen {sid}] facade auto-trigger failed to spawn: {e}')
        except Exception as e:
            print(f'[autogen {sid}] re-check failed: {e}')


def trigger_autogen_if_enabled(sid):
    """Public entry point: kick off background sweep if the series has auto_generate_assets on.
    Uses _spawn_with_keys so AVAI calls inside the sweep have a valid x-api-key
    after the request context tears down."""
    s = load_series(sid)
    if not s or not s.get('auto_generate_assets'):
        return False
    _spawn_with_keys(auto_generate_missing_assets, sid)
    return True


@app.route('/api/series/<sid>/auto-generate', methods=['POST'])
def toggle_auto_generate(sid):
    """Toggle auto_generate_assets flag. If enabling, immediately kick off a sweep."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    data = request.json or {}
    enabled = bool(data.get('enabled', True))
    if enabled:
        _nd_anthro, _fl_anthro = _anthro_preflight(s, s.get('characters') or [])
        if _nd_anthro:
            return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                            'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    s['auto_generate_assets'] = enabled
    save_series(sid, s)
    started = False
    if enabled:
        started = trigger_autogen_if_enabled(sid)
    return jsonify({'enabled': enabled, 'sweep_started': started, 'status': _autogen_status(sid)})


@app.route('/api/series/<sid>/auto-generate/status', methods=['GET'])
def autogen_status_endpoint(sid):
    st = _autogen_status(sid)
    # Phantom-running self-heal: if running=true but the worker thread is
    # actually dead (queue=0, nothing in progress, lock acquired but never
    # released), reset the state. Happens when the daemon thread dies mid-
    # run (server reload kills daemon threads, KeyboardInterrupt, hard
    # crash) and the finally{} that flips running=false never executed.
    # Without this self-heal the UI spinner spins forever at 0/0.
    if st.get('running') and not st.get('queue') and not (st.get('in_progress') or []):
        # Try to acquire the per-series lock non-blocking. If we can grab
        # it, the worker is definitely not running anymore — release it
        # back and reset the status.
        lock = _AUTOGEN_LOCKS.get(sid)
        if lock is None or lock.acquire(blocking=False):
            if lock is not None:
                lock.release()
            st['running'] = False
            st['in_progress'] = []
            print(f'[autogen {sid}] phantom-running detected — reset', flush=True)
    return jsonify(st)


@app.route('/api/series/<sid>/auto-generate/sweep', methods=['POST'])
def trigger_autogen_sweep(sid):
    """Manual sweep — runs regardless of auto_generate_assets toggle.
    Pre-syncs every episode's cast block, then generates all missing assets."""
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    # Force-enable the toggle (user explicitly asked for a sweep, this is what they want)
    if not s.get('auto_generate_assets'):
        s['auto_generate_assets'] = True
        save_series(sid, s)
    _nd_anthro, _fl_anthro = _anthro_preflight(s, s.get('characters') or [])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    _spawn_with_keys(auto_generate_missing_assets, sid)
    return jsonify({'started': True, 'status': _autogen_status(sid)})


@app.route('/api/series/<sid>/reanalyze-outfits', methods=['POST'])
def reanalyze_outfits(sid):
    """Bulk-reparse [BLOCKING] outfits across selected episodes (or all
    episodes with a script when `episode_numbers` is omitted). Fix for the
    common case where an episode was generated under an older writer prompt
    or before character-name matching was lenient — re-running the sync now
    detects outfits the first pass missed and queues image generation.

    Also back-fills WEAK descriptions on existing outfits — when the writer
    only provided a label (e.g. `OUTFIT: Business Casual` with no OUTFIT_DESC),
    the outfit was created with `description == label`, which is a useless
    text anchor and makes Seedance reinvent the cloth on every chunk. We
    detect these via `description == label` (case-insensitive) and expand them
    via a single Claude call each, plus clear the outfit photo so the autogen
    sweep regenerates the image with the new concrete description.

    Body: {"episode_numbers": [int, ...]}  // optional. Omit/empty → all-with-script.
    Returns: {episodes_processed, total_new_outfits, total_weak_descs_fixed,
              by_episode: [{number, new_outfits: [...]}]}
    """
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    requested = body.get('episode_numbers') or []
    requested_set = set(int(n) for n in requested) if requested else None

    all_eps = list_episodes(sid)
    targets = [ep for ep in all_eps
               if (ep.get('script') or '').strip()
               and (requested_set is None or ep['number'] in requested_set)]

    by_episode = []
    total = 0
    for ep in targets:
        try:
            new_outfits = _sync_script_outfits(sid, ep.get('script') or '')
        except Exception as e:
            _log_event('WARN', 'reanalyze_outfits_failed',
                       sid=sid, ep=ep['number'], err=str(e)[:200])
            new_outfits = []
        by_episode.append({
            'number': ep['number'],
            'title': ep.get('title', ''),
            'new_outfits': new_outfits,
        })
        total += len(new_outfits)

    # ── Back-fill weak descriptions on existing outfits ─────────────────
    # An outfit is "weak" when description is empty OR equals the label —
    # legacy state from before _sync_script_outfits enforced expansion.
    s = load_series(sid)  # reload — _sync_script_outfits may have written
    weak_fixed = 0
    weak_changed = False
    for c in (s.get('characters') or []):
        for o in (c.get('outfits') or []):
            label = (o.get('label') or '').strip()
            desc  = (o.get('description') or '').strip()
            if not label:
                continue
            if desc and desc.lower() != label.lower():
                continue  # already has a real description
            # Expand via Claude
            new_desc = _expand_outfit_label_to_desc(
                label,
                c.get('appearance', ''),
                c.get('gender', ''),
            )
            if not new_desc or new_desc.strip().lower() == label.lower():
                continue  # expansion failed or returned the same label
            o['description'] = new_desc.strip()
            # Clear the existing photo so the autogen sweep regenerates the
            # outfit reference image with the concrete description — without
            # this the old weakly-anchored ref keeps causing chunk-to-chunk drift.
            o['photo'] = None
            o['avai_url'] = ''
            weak_fixed += 1
            weak_changed = True
            _log_event('INFO', 'outfit_weak_desc_expanded',
                       sid=sid, char=c.get('name'), label=label,
                       new_desc=new_desc[:120])
    if weak_changed:
        save_series(sid, s)

    # Fire one consolidated autogen sweep — covers both newly-created outfits
    # AND the photo-cleared ones from back-fill above.
    if total > 0 or weak_fixed > 0:
        try:
            _spawn_with_keys(auto_generate_missing_assets, sid)
        except Exception as e:
            print(f'[reanalyze-outfits] autogen spawn failed for {sid}: {e}')

    return jsonify({
        'episodes_processed': len(targets),
        'total_new_outfits': total,
        'total_weak_descs_fixed': weak_fixed,
        'by_episode': by_episode,
    })

