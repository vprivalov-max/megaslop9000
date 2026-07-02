"""Item image generation routes (story prop references) + inline item detection."""
import json
import re
import uuid
from pathlib import Path

from flask import jsonify, request

from sw.avai import _series_image_provider, avai_generate
from sw.core import app
from sw.era import (_modern_document_directive, _no_caption_text_clause,
                    _strip_cast_names_for_visual)
from sw.jsonutils import loads_lenient
from sw.llm import claude_ask
from sw.storage import (assets_dir, list_episodes, load_episode, load_series,
                        save_episode, save_series, series_path)
from sw.style import _series_style_clause
from sw.utils import asset_name, slugify

# ── Item image generation (story prop reference) ─────────────────────────────

@app.route('/api/series/<sid>/items/<item_id>/upload-photo', methods=['POST'])
def upload_item_photo(sid, item_id):
    s = load_series(sid)
    item = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no file'}), 400
    item_slug = slugify(item['name'])
    item_dir = assets_dir(sid) / 'items' / item_slug
    item_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    filename = f'{asset_name(item["name"])}.{ext}'
    (item_dir / filename).write_bytes(f.read())
    rel_path = f'assets/items/{item_slug}/{filename}'
    refs = item.setdefault('ref_images', [])
    if rel_path not in refs:
        refs.insert(0, rel_path)
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'series': s})


@app.route('/api/series/<sid>/items/<item_id>/generate-image', methods=['POST'])
def generate_item_image(sid, item_id):
    s = load_series(sid)
    item = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'not found'}), 404

    style_clause = _series_style_clause(s)
    constraints = (item.get('image_constraints') or '').strip()
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {constraints}." if constraints else ""
    modern_doc_clause = _modern_document_directive(item)
    # Documents legitimately carry printed text → skip the no-text clause for them.
    no_text_clause = '' if modern_doc_clause else _no_caption_text_clause(constraints)
    it_name = _strip_cast_names_for_visual(item['name'], s)
    it_desc = _strip_cast_names_for_visual(item.get('description', ''), s)
    prompt = (
        f"{it_name}. {it_desc}.{constraints_clause}{modern_doc_clause}{no_text_clause} "
        f"Product-style still-life photo of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless gray background (#dadada) — flat color field NOT a photo studio set (no lighting rigs, no trusses, no equipment visible), soft even diffused illumination on the subject only, "
        f"subtle shadow on ground, sharp focus on object texture and details. "
        f"Square 1:1 framing. Photorealistic, high detail. {style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()

    item_slug = slugify(item['name'])
    item_dir = assets_dir(sid) / 'items' / item_slug
    out_path = item_dir / f'{asset_name(item["name"])}.jpg'

    try:
        # Items use square aspect — works as a portable reference for both
        # vertical (Reteller) and horizontal (Seedance) compositions.
        image_url = avai_generate(prompt, out_path, aspect_ratio='1:1', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = item.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        item['avai_url'] = image_url  # used by Seedance for video refs
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/items/<item_id>/regenerate', methods=['POST'])
def regenerate_item(sid, item_id):
    """Regenerate the item's reference image with user-supplied constraints.
    Persists constraints on the item for future regenerations."""
    s = load_series(sid)
    item = next((it for it in s.get('items', []) if it['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'item not found'}), 404
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()
    item['image_constraints'] = wishes
    style_clause = _series_style_clause(s)
    constraints_clause = f" IMPORTANT — strictly follow these constraints: {wishes}." if wishes else ""
    modern_doc_clause = _modern_document_directive(item)
    no_text_clause = '' if modern_doc_clause else _no_caption_text_clause(wishes)
    it_name = _strip_cast_names_for_visual(item['name'], s)
    it_desc = _strip_cast_names_for_visual(item.get('description', ''), s)
    prompt = (
        f"{it_name}. {it_desc}.{constraints_clause}{modern_doc_clause}{no_text_clause} "
        f"Product-style still-life photo of the object alone. No people, no hands, no characters. "
        f"Centered composition, neutral seamless gray background (#dadada) — flat color field NOT a photo studio set (no lighting rigs, no trusses, no equipment visible), soft even diffused illumination on the subject only, "
        f"subtle shadow on ground, sharp focus on object texture and details. "
        f"Square 1:1 framing. Photorealistic, high detail. {style_clause}"
    )
    prompt = re.sub(r'\s+', ' ', prompt).strip()
    item_slug = slugify(item['name'])
    item_dir = assets_dir(sid) / 'items' / item_slug
    item_dir.mkdir(parents=True, exist_ok=True)
    out_path = item_dir / f'{asset_name(item["name"])}.jpg'
    try:
        if out_path.exists():
            try: out_path.unlink()
            except Exception: pass
        image_url = avai_generate(prompt, out_path, aspect_ratio='1:1', preferred_provider=_series_image_provider(s))
        rel_path = str(out_path.relative_to(series_path(sid)))
        refs = item.setdefault('ref_images', [])
        refs[:] = [r for r in refs if Path(r).stem != out_path.stem]
        refs.insert(0, rel_path)
        item['avai_url'] = image_url
        save_series(sid, s)
        return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}', 'image_url': image_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Cyrillic → Latin transliteration table for cross-language item dedup.
# Tiny on purpose — we only need it for the simple case where the same prop
# is described in Russian and English versions of the same script (e.g.
# "диктофон" / "dictaphone", "локет" / "locket", "флешка" / "flash drive").
_TRANSLIT_DEDUP = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f',
    'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y',
    'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})

def _norm_for_dedup(s):
    """Lowercase + translit + alnum-only for fuzzy comparisons."""
    s = (s or '').lower().translate(_TRANSLIT_DEDUP)
    return re.sub(r'[^a-z0-9]+', '', s)

def _llm_dedupe_against_existing(existing_items, new_items):
    """Ask Claude to merge synonym/translation duplicates between newly-detected
    items and the existing series.items list. The lexical _fuzzy_find_item misses
    synonyms — 'dictaphone' / 'voice recorder' / 'recording device' are all the
    same prop but share no substring or 3-word description overlap.

    Returns {new_idx: existing_id} for items the model says are duplicates.
    Items without a mapping are kept as truly new.

    No-op (empty mapping) when no existing items or no new items."""
    if not existing_items or not new_items:
        return {}
    payload = {
        'existing': [
            {'id': it['id'], 'name': it.get('name', ''), 'description': (it.get('description') or '')[:160]}
            for it in existing_items
        ],
        'new': [
            {'idx': i, 'name': (it.get('name') or ''), 'description': (it.get('description') or '')[:160]}
            for i, it in enumerate(new_items)
        ],
    }
    system = (
        "You merge duplicate plot-items. Two items are DUPLICATES if they refer to "
        "the same physical prop in the story, regardless of:\n"
        "  - language drift (Russian vs English description)\n"
        "  - synonyms (dictaphone = voice recorder = recording device; locket = pendant; "
        "gun = pistol = revolver; flashdrive = USB stick = USB drive)\n"
        "  - paraphrasing (silver locket vs antique silver pendant with photo)\n\n"
        "They are NOT duplicates if:\n"
        "  - one is a SECOND distinct copy of the same kind of object the script "
        "treats as a separate plot-prop (e.g. 'second dictaphone' that's NOT the "
        "hidden one — both can exist)\n"
        "  - they're different objects that just look similar\n\n"
        "Return STRICT JSON, no prose, no markdown:\n"
        '{"merges":[{"new_idx":INT,"existing_id":"..."}]}\n'
        "Include ONLY confirmed duplicates. Items not listed in `merges` are kept "
        "as new entries. If nothing duplicates, return {\"merges\":[]}."
    )
    try:
        raw = claude_ask(json.dumps(payload, ensure_ascii=False), system=system, max_tokens=1024)
        parsed = loads_lenient(raw)
        out = {}
        for m in (parsed.get('merges') or []):
            try:
                idx = int(m.get('new_idx'))
                eid = str(m.get('existing_id') or '').strip()
                if eid and any(it['id'] == eid for it in existing_items):
                    out[idx] = eid
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f'[item-dedupe-llm] failed: {e}', flush=True)
        return {}


def _fuzzy_find_item(items, name, desc):
    """Find an existing item matching the new name/description, even when
    the LLM returned a slight rewording or a translation. Tiered match:
      1. Exact case-insensitive name (fast path, current behaviour)
      2. Normalised name (strip punctuation, transliterate Cyrillic → Latin)
      3. Substring overlap of normalised name (e.g. 'hidden recorder' vs
         'recorder') — both ways
      4. Description overlap (≥3 words shared in normalised form) — handles
         total renames like 'диктофон' → 'recording device'
    Returns the matched dict or None."""
    if not items:
        return None
    name_l = name.lower()
    # Tier 1: exact ci match
    for it in items:
        if it.get('name', '').lower() == name_l:
            return it
    name_n = _norm_for_dedup(name)
    if not name_n:
        return None
    # Tier 2 + 3: normalised exact / substring
    for it in items:
        ex = _norm_for_dedup(it.get('name', ''))
        if not ex:
            continue
        if ex == name_n:
            return it
        # Substring either direction (longer-than-3 to skip noise like 'a')
        if len(name_n) >= 4 and len(ex) >= 4:
            if name_n in ex or ex in name_n:
                return it
    # Tier 4: description-word overlap
    if desc:
        desc_words = set(re.findall(r'[a-zа-яё]{4,}', desc.lower()))
        desc_words_n = {_norm_for_dedup(w) for w in desc_words}
        desc_words_n.discard('')
        for it in items:
            ex_desc = it.get('description', '')
            if not ex_desc:
                continue
            ex_words = set(re.findall(r'[a-zа-яё]{4,}', ex_desc.lower()))
            ex_words_n = {_norm_for_dedup(w) for w in ex_words}
            ex_words_n.discard('')
            if len(desc_words_n & ex_words_n) >= 3:
                return it
    return None


@app.route('/api/series/<sid>/dedupe-items', methods=['POST'])
def dedupe_series_items(sid):
    """Walks series.items, finds dups via fuzzy + LLM-synonym pass, merges
    them into a canonical set. Used to clean up dups accumulated BEFORE the
    detect-items dedup logic was added (e.g. 'Hidden Voice Recorder' +
    'hidden dictaphone' + 'desk dictaphone' all referring to one prop).
    For each dup group:
      - keeps the entry with the longest description (or earliest by id)
        as canonical
      - rewrites every episode.items_used to point at canonical
      - removes the dup entry from series.items
    Returns {'merged': N, 'kept': N, 'groups': [...]}.
    Idempotent — running twice does nothing the second time."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    items = list(s.get('items', []) or [])
    if len(items) < 2:
        return jsonify({'merged': 0, 'kept': len(items), 'groups': []}), 200

    # Build groups by walking pairs through fuzzy + LLM. Greedy: each item is
    # tested against already-formed group leads; if matches → joins that group.
    groups = []  # list of [item, item, ...]
    for it in items:
        joined = False
        for grp in groups:
            lead = grp[0]
            if _fuzzy_find_item([lead], it['name'], it.get('description', '')):
                grp.append(it); joined = True; break
        if not joined:
            groups.append([it])

    # Stage B: try to merge groups that didn't match lexically — pass each
    # group's lead vs all OTHER leads through the LLM dedup.
    if len(groups) >= 2:
        leads = [grp[0] for grp in groups]
        # Use LLM to find synonym-pairs among leads. Treat first lead as
        # "existing", every other lead as "new" — then iterate pairs.
        # To keep prompt small we batch all leads and ask for transitive
        # equivalence sets.
        try:
            sys = (
                "Cluster these plot-items into groups where each group is the "
                "SAME prop (across language, synonyms, paraphrasing). Return "
                'JSON: {"groups":[["id1","id2",...], ["id3"], ...]}. Items not '
                'paired with any duplicate go in their own singleton group. '
                'No prose, strict JSON.'
            )
            payload = json.dumps({
                'items': [
                    {'id': lead['id'], 'name': lead.get('name', ''), 'description': (lead.get('description') or '')[:160]}
                    for lead in leads
                ]
            }, ensure_ascii=False)
            raw = claude_ask(payload, system=sys, max_tokens=1024)
            parsed = loads_lenient(raw)
            llm_groups = parsed.get('groups') or []
            # Validate: collect lead-id → llm-group-idx
            id_to_grp = {}
            for gi, grp_ids in enumerate(llm_groups):
                for iid in grp_ids:
                    id_to_grp[iid] = gi
            # Re-cluster `groups` according to llm groupings.
            if id_to_grp:
                new_groups = {}
                for grp in groups:
                    lead_id = grp[0]['id']
                    gi = id_to_grp.get(lead_id)
                    if gi is None:
                        # LLM dropped it — keep as own group
                        new_groups[f'orphan-{lead_id}'] = new_groups.get(f'orphan-{lead_id}', []) + grp
                    else:
                        new_groups.setdefault(gi, []).extend(grp)
                groups = list(new_groups.values())
        except Exception as e:
            print(f'[dedupe-items-llm] failed (using fuzzy-only): {e}', flush=True)

    # Apply merges: pick canonical, rewrite items_used in all episodes,
    # remove dups from series.items.
    canonical_by_dup_id = {}  # dup_id → canonical_id
    final_items = []
    merged_groups_log = []
    for grp in groups:
        if len(grp) == 1:
            final_items.append(grp[0])
            continue
        # Canonical = longest description (most info), tie-break on shortest name.
        grp_sorted = sorted(grp, key=lambda x: (-len(x.get('description', '')), len(x.get('name', ''))))
        canonical = grp_sorted[0]
        # Merge ref_images / avai_url from any group member if canonical is empty
        for member in grp:
            if member is canonical:
                continue
            if not canonical.get('ref_images') and member.get('ref_images'):
                canonical['ref_images'] = member['ref_images']
            if not canonical.get('avai_url') and member.get('avai_url'):
                canonical['avai_url'] = member['avai_url']
            canonical_by_dup_id[member['id']] = canonical['id']
        final_items.append(canonical)
        merged_groups_log.append({
            'canonical': {'id': canonical['id'], 'name': canonical['name']},
            'merged': [{'id': m['id'], 'name': m['name']} for m in grp if m is not canonical],
        })

    if not canonical_by_dup_id:
        return jsonify({'merged': 0, 'kept': len(items), 'groups': []})

    s['items'] = final_items
    save_series(sid, s)
    # Rewrite every episode's items_used to use canonical ids only.
    for ep in list_episodes(sid):
        used = ep.get('items_used') or []
        if not used:
            continue
        rewritten = []
        seen = set()
        for iid in used:
            cid = canonical_by_dup_id.get(iid, iid)
            if cid not in seen:
                rewritten.append(cid); seen.add(cid)
        if rewritten != used:
            ep['items_used'] = rewritten
            save_episode(sid, ep['number'], ep)

    return jsonify({
        'merged': len(canonical_by_dup_id),
        'kept': len(final_items),
        'groups': merged_groups_log,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/detect-items', methods=['POST'])
def detect_items_in_episode(sid, num):
    """LLM extracts PLOT-RELEVANT items from the episode's script. Plot-relevant
    means the item is load-bearing for the story (the locket revealed at the
    climax, the USB stick with evidence, the stolen handbag) — NOT every random
    prop in frame (coffee cups, generic furniture, background dressing).

    For each detected item:
      - if a series.items entry with the same lowercased name exists → reuse its id
      - otherwise create a new series.items entry (no ref_image yet — autogen
        sweep or manual click handles photo)
      - add the id to episode.items_used (idempotent)

    Returns {'detected': [{name, description, status: 'created'|'existing'}, ...]}.
    """
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'episode has no script yet'}), 400

    # KNOWN-items context so re-runs across language barriers don't dup. The
    # LLM was previously fed the script with no awareness of what's already
    # in series.json — so e.g. a Russian script containing "скрытый диктофон"
    # extracted as "Hidden Recorder" / "Recording Device" / "Скрытый диктофон"
    # on different runs, producing 3 separate item entries for the same prop.
    # Now the prompt explicitly lists known items (name + description) and
    # tells the model to reuse the EXACT existing name when it sees the same
    # plot-prop, regardless of language drift in the script.
    known_items_block = ''
    existing_items = s.get('items', []) or []
    if existing_items:
        known_lines = [
            f"  - {it['name']!r}: {(it.get('description') or '').strip()[:120]}"
            for it in existing_items if it.get('name')
        ]
        known_items_block = (
            "\n\n=== ALREADY KNOWN ITEMS (use the EXACT existing name when the script "
            "describes the same prop, even if the script uses a different language or "
            "synonym; do NOT create a duplicate with a translated/paraphrased name) ===\n"
            + '\n'.join(known_lines) + '\n'
        )

    system = (
        "You extract PLOT-RELEVANT items from a short-drama script. Return STRICT JSON.\n"
        "PLOT-RELEVANT = the item is load-bearing for the story: it gets revealed,\n"
        "stolen, exchanged, hidden, used as evidence, gifted, broken, found,\n"
        "carried by a character through multiple scenes, or its presence/absence\n"
        "drives a beat. Examples: a locket with a photo, a USB stick with files,\n"
        "a wedding ring, a stolen handbag, a contract document, a vial of poison.\n\n"
        "NOT plot-relevant — DO NOT extract: generic furniture, coffee cups,\n"
        "phones used only for routine calls, clothing (covered separately by\n"
        "outfits), food eaten without significance, background dressing.\n\n"
        "Return JSON: {\"items\": [{\"name\": \"...\", \"description\": \"...\"}, ...]}\n"
        "name: short concrete noun phrase. PREFER the EXACT existing name from the\n"
        "  KNOWN ITEMS list when the script is talking about the same prop, even\n"
        "  across languages (Russian script + English known name = use the English\n"
        "  known name). Only invent a new name when the prop is genuinely new.\n"
        "description: 1 sentence describing visual appearance for image gen.\n"
        "If nothing qualifies (or all qualifying items are already in KNOWN), return\n"
        "{\"items\": []}. No prose, no preamble."
    )
    raw = claude_ask(
        f"Script:\n\n{script[:18000]}{known_items_block}",
        system=system, model='', max_tokens=2048,
    )
    try:
        parsed = loads_lenient(raw)
        detected_raw = parsed.get('items') or []
    except Exception as e:
        return jsonify({'error': f'LLM returned unparseable JSON: {e}; raw={raw[:300]}'}), 502

    s.setdefault('items', [])
    if not isinstance(ep.get('items_used'), list):
        ep['items_used'] = []
    # Cap detected list early so the dedup-pass payload stays small.
    detected_capped = detected_raw[:20]
    # Two-stage dedup vs existing items:
    #   Stage A: cheap lexical _fuzzy_find_item (handles exact + transliteration
    #            + substring + description-word-overlap)
    #   Stage B: if anything remains "new", ask Claude to merge synonyms
    #            (dictaphone↔voice recorder, etc.) — single small LLM call.
    pre_matches = {}  # idx → existing item dict
    leftovers   = []  # [(idx, name, desc), ...] for stage B
    for i, d in enumerate(detected_capped):
        name = (d.get('name') or '').strip()
        desc = (d.get('description') or '').strip()
        if not name:
            continue
        match = _fuzzy_find_item(s['items'], name, desc)
        if match:
            pre_matches[i] = match
        else:
            leftovers.append((i, name, desc))
    llm_merges = {}
    if leftovers and s['items']:
        new_for_llm = [{'name': n, 'description': desc} for (_, n, desc) in leftovers]
        merged = _llm_dedupe_against_existing(s['items'], new_for_llm)
        # `merged` keys are indices into new_for_llm; map back to detected_capped indices.
        for j, eid in merged.items():
            if 0 <= j < len(leftovers):
                orig_idx = leftovers[j][0]
                existing = next((it for it in s['items'] if it['id'] == eid), None)
                if existing:
                    llm_merges[orig_idx] = existing

    detected_summary = []
    for i, d in enumerate(detected_capped):
        name = (d.get('name') or '').strip()
        desc = (d.get('description') or '').strip()
        if not name:
            continue
        existing = pre_matches.get(i) or llm_merges.get(i)
        if existing:
            item_id = existing['id']
            status = 'existing'
            # Refresh description if currently empty.
            if not (existing.get('description') or '').strip() and desc:
                existing['description'] = desc
        else:
            item_id = str(uuid.uuid4())[:8]
            s['items'].append({
                'id': item_id,
                'name': name,
                'description': desc,
                'ref_images': [],
                'avai_url': '',
                'image_constraints': '',
            })
            status = 'created'
        if item_id not in ep['items_used']:
            ep['items_used'].append(item_id)
        detected_summary.append({'name': name, 'description': desc, 'status': status})

    save_series(sid, s)
    save_episode(sid, num, ep)
    return jsonify({'detected': detected_summary, 'items_used': ep['items_used']})


