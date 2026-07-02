"""Script generation routes: generate-script, generate-to-landmark,
cast-block sync, gender inference, character/outfit extraction."""
import json
import re
import threading
import time
import uuid

from flask import jsonify, request

from sw.anthro import _anthro_world_block, _revision_instructions_block
from sw.canon_index import (_build_plot_device_history,
                            _extract_devices_from_script,
                            _extract_narrative_state_from_script,
                            _update_devices_index, _update_narrative_index)
from sw.core import app
from sw.llm import claude_ask
from sw.logging_utils import _log_event
# routes->routes: landmark builders shared by writer prompts (no cycle).
from sw.routes.landmarks import (build_finale_bridge_plan,
                                 build_finale_contract_block,
                                 build_trajectory_block, is_finale_episode)
from sw.storage import (_extract_end_position, _normalize_blocking_tags,
                        _resolve_char_by_script_name, _sync_script_outfits,
                        batch_size, chunk_range, is_batch_mode, list_episodes,
                        load_canon, load_episode, load_series, save_canon,
                        save_episode, save_series)
from sw.story_logic import (_build_crowd_constraint_block,
                            _build_narrative_state_block,
                            _extract_speaker_and_blocking_labels,
                            _label_is_unnamed, audit_logic_holes, audit_script,
                            build_logic_brief, detect_scene_overcrowding,
                            detect_script_overlength,
                            detect_unnamed_characters, extract_canon_updates,
                            rollback_canon_for_episode)
from sw.story_prompts import (_build_cast_block, _format_mode_block,
                              _format_mode_of, _outfit_ids)
from sw.story_writer import _build_batch_script_system, _build_script_system

def sync_episode_with_cast_block(sid, num):
    """Reconcile episode's character_used / character_outfits IDs with current series state.
    Idempotent — safe to call repeatedly. Re-parses the cast block from the script and:
      - re-creates any missing characters / outfits in series.json (with new UUIDs)
      - rewrites episode.characters_used and character_outfits to use current valid IDs
      - drops any stale IDs that no longer resolve
    Returns dict {created_chars: [...], created_outfits: [...], healed_refs: int}."""
    s = load_series(sid)
    if not s: return {'error': 'series not found'}
    ep = load_episode(sid, num)
    if not ep or not (ep.get('script') or '').strip(): return {'error': 'episode has no script'}

    chars = s.setdefault('characters', [])
    pre_char_ids = {c['id'] for c in chars}
    pre_outfit_ids = {(c['id'], o['id']) for c in chars for o in c.get('outfits', [])}

    cast_chars, cast_outfits = _parse_cast_block(ep['script'], chars)

    # Track what was just created
    created_chars = [c['name'] for c in chars if c['id'] not in pre_char_ids]
    created_outfits = [
        f"{c['name']}/{o['label']}"
        for c in chars for o in c.get('outfits', [])
        if (c['id'], o['id']) not in pre_outfit_ids
    ]

    # Heal episode refs: drop IDs that no longer exist; merge in cast_block IDs
    valid_char_ids = {c['id'] for c in chars}
    valid_outfit_ids = {(c['id'], o['id']) for c in chars for o in c.get('outfits', [])}

    # characters_used — start from cast_chars (authoritative), keep any extra valid IDs
    healed = 0
    if cast_chars:
        new_used = list(dict.fromkeys(cast_chars))  # dedup, preserve order
        # Keep any extra valid pre-existing IDs (e.g. silent characters not in cast block)
        for cid in ep.get('characters_used', []):
            if cid in valid_char_ids and cid not in new_used:
                new_used.append(cid)
        if ep.get('characters_used') != new_used:
            healed += 1
            ep['characters_used'] = new_used
    else:
        # No cast block parsed — just drop dead IDs
        cleaned = [cid for cid in ep.get('characters_used', []) if cid in valid_char_ids]
        if cleaned != ep.get('characters_used'):
            healed += 1
            ep['characters_used'] = cleaned

    # character_outfits — rewrite from cast_outfits (now LISTS), drop dead refs.
    # cast_outfits is the authoritative source for which outfits the script demands.
    # We also union in any pre-existing valid outfit refs the user manually added.
    new_outfits = {}
    for cid, oids in (cast_outfits or {}).items():
        oids_list = oids if isinstance(oids, list) else [oids]
        kept = [oid for oid in oids_list if (cid, oid) in valid_outfit_ids]
        if kept:
            new_outfits[cid] = list(dict.fromkeys(kept))  # dedup preserve order
    # Preserve any pre-existing valid outfit refs (legacy single-string OR list)
    for cid, raw in (ep.get('character_outfits') or {}).items():
        prev_ids = _outfit_ids(raw)
        existing = new_outfits.setdefault(cid, [])
        for oid in prev_ids:
            if (cid, oid) in valid_outfit_ids and oid not in existing:
                existing.append(oid)
        if not existing:
            new_outfits.pop(cid, None)
    if ep.get('character_outfits') != new_outfits:
        healed += 1
        ep['character_outfits'] = new_outfits

    # ─── Locations — parse scene headings (INT./EXT./ИНТА./ЭКС.) and tick matching series locations.
    # Scene heading format we emit: "ИНТА. HOTEL ROOM — УТРО" / "INT. BOARDROOM — DAY".
    # We grab the middle slug (location name) and fuzzy-match against series.locations by name.
    locs = s.setdefault('locations', [])
    detected_loc_names = []
    # Allow leading markdown decorators (**, __, #, >) before the INT./EXT. cue —
    # writers sometimes emit `**INT. RANCH HOUSE — MORNING**` for visual emphasis,
    # and the old regex silently dropped those whole headings, leaving the
    # location un-tied to the episode.
    heading_re = re.compile(
        r'^[\s*_#>]*(?:INT\.|EXT\.|ИНТА?\.|ЭКС?\.|INT/EXT\.|EXT/INT\.)\s*([^—\-\n]+?)\s*[—\-]',
        re.IGNORECASE | re.MULTILINE,
    )
    for m in heading_re.finditer(ep.get('script') or ''):
        # Strip trailing markdown closers (**, __) that may be on the last token
        nm = m.group(1).strip().strip('"').strip("'").rstrip('*_').strip()
        # Strip leading time-of-day artefacts that sometimes leak into the name
        nm = re.sub(r'^(DAY|NIGHT|MORNING|EVENING|УТРО|НОЧЬ|ДЕНЬ|ВЕЧЕР)\s+', '', nm, flags=re.IGNORECASE).strip()
        if nm and nm.lower() not in (x.lower() for x in detected_loc_names):
            detected_loc_names.append(nm)

    detected_loc_ids = []
    created_loc_names = []
    for nm in detected_loc_names:
        nl = nm.lower()
        match = next((l for l in locs if l['name'].lower() == nl), None)
        if not match:
            # fuzzy: substring either way (e.g. "HOTEL ROOM" vs "Hotel Suite Room")
            match = next(
                (l for l in locs if nl in l['name'].lower() or l['name'].lower() in nl),
                None,
            )
        if not match:
            # AUTO-CREATE: if the script uses a scene heading we don't have on file,
            # add it to series.locations on the fly so the checkbox CAN be ticked.
            # This is the fix for "локации без галочек" — previously we silently dropped
            # any heading that didn't match an existing entry. The series-locations list
            # was often populated from the synopsis, while the script invented its own
            # places, so nothing ever matched. Now the script is the source of truth.
            new_loc = {
                'id': str(uuid.uuid4())[:8],
                'name': nm.title() if nm.isupper() or nm.islower() else nm,
                'description': 'Auto-created from episode script scene heading',
                'ref_images': [],
            }
            locs.append(new_loc)
            created_loc_names.append(new_loc['name'])
            match = new_loc
        if match:
            if match['id'] not in detected_loc_ids:
                detected_loc_ids.append(match['id'])

    valid_loc_ids = {l['id'] for l in locs}
    new_locs_used = list(detected_loc_ids)
    # Keep any pre-existing user-ticked locations that are still valid (don't drop manual picks)
    for lid in ep.get('locations_used', []) or []:
        if lid in valid_loc_ids and lid not in new_locs_used:
            new_locs_used.append(lid)
    if ep.get('locations_used') != new_locs_used:
        healed += 1
        ep['locations_used'] = new_locs_used

    # ── No-name cue → card auto-alias (heals already-written episodes) ──────────
    # If the script addresses a character by a bare role label (e.g. «CLIENT:»)
    # that differs from its card name (e.g. «Mrs. Park»), the binding filter
    # would drop the reference in any chunk that uses only the role label →
    # the model renders the wrong face. We conservatively link the orphan label
    # to its card as an alias so `_char_name_in_text` keeps the ref. Only fires
    # when the mapping is UNAMBIGUOUS: exactly one un-cued role label and exactly
    # one episode card that nothing else claims. Anything ambiguous is left for
    # the write-time detector / user to fix.
    linked_aliases = []
    unnamed_warnings = []
    try:
        ep_cards = [c for c in chars if c['id'] in set(ep.get('characters_used', []))]
        if ep_cards:
            def _resolve_label(lab):
                ll = lab.lower()
                for c in ep_cards:
                    if (c.get('name') or '').lower() == ll:
                        return c
                    if ll in {(a or '').lower() for a in (c.get('aliases') or [])}:
                        return c
                return _resolve_char_by_script_name(lab, ep_cards)
            claimed, generic_orphans, named_orphans = set(), {}, {}
            for lab, _kind in _extract_speaker_and_blocking_labels(ep['script']):
                c = _resolve_label(lab)
                if c:
                    claimed.add(c['id'])
                elif _label_is_unnamed(lab):
                    generic_orphans.setdefault(lab.lower(), lab)
                else:
                    named_orphans.setdefault(lab.lower(), lab)
            unclaimed = [c for c in ep_cards if c['id'] not in claimed]
            # Auto-link ONLY when unambiguous: a single bare role label and a
            # single un-cued card. A named-but-uncast orphan is NOT auto-aliased
            # (it needs its own card) — it's surfaced as a warning instead.
            if len(generic_orphans) == 1 and not named_orphans and len(unclaimed) == 1:
                lab_low, lab_orig = next(iter(generic_orphans.items()))
                card = unclaimed[0]
                al = card.setdefault('aliases', [])
                if lab_low not in {(a or '').lower() for a in al}:
                    al.append(lab_low)
                    linked_aliases.append({'alias': lab_orig, 'char': card.get('name'), 'char_id': card['id']})
                    _log_event('INFO', 'cue_auto_aliased', sid=sid, ep_num=num,
                               alias=lab_orig, char=card.get('name'), char_id=card['id'])
            else:
                for lab in list(generic_orphans.values()) + list(named_orphans.values()):
                    unnamed_warnings.append(lab)
    except Exception as _ae:
        _log_event('WARN', 'cue_auto_alias_failed', sid=sid, ep_num=num, err=str(_ae)[:200])

    save_series(sid, s)
    save_episode(sid, num, ep)
    return {
        'created_characters': created_chars,
        'created_outfits': created_outfits,
        'created_locations': created_loc_names,
        'detected_locations': detected_loc_names,
        'healed_refs': healed,
        'characters_used': ep['characters_used'],
        'character_outfits': ep['character_outfits'],
        'locations_used': ep['locations_used'],
        'linked_aliases': linked_aliases,
        'unnamed_warnings': unnamed_warnings,
    }


def _infer_gender_from_script(name, script):
    """Count gendered pronouns within ±3 lines of any line mentioning `name`.
    Returns 'female' / 'male' / None. Handles English + Russian.
    Used as a robust fallback when the cast block omits GENDER for a character —
    the previous tiny hardcoded female-name whitelist defaulted everyone-not-listed
    to 'male', which silently turned protagonists like LYDIA into men."""
    if not name or not script:
        return None
    import re as _re
    name_lower = name.lower()
    lines = script.splitlines()
    he_count = 0
    she_count = 0
    EN_HE  = _re.compile(r"\b(he|his|him|himself|he's|he'd|he'll)\b", _re.IGNORECASE)
    EN_SHE = _re.compile(r"\b(she|her|hers|herself|she's|she'd|she'll)\b", _re.IGNORECASE)
    RU_HE  = _re.compile(r"\b(он|его|ему|им|нём|него)\b", _re.IGNORECASE)
    RU_SHE = _re.compile(r"\b(она|её|ее|ей|ней|неё)\b", _re.IGNORECASE)
    for i, line in enumerate(lines):
        if name_lower not in line.lower():
            continue
        window = ' '.join(lines[max(0, i-3): min(len(lines), i+4)])
        he_count  += len(EN_HE.findall(window))  + len(RU_HE.findall(window))
        she_count += len(EN_SHE.findall(window)) + len(RU_SHE.findall(window))
    # Require a meaningful margin to avoid noise from generic "he was talking to her"
    if she_count >= 3 and she_count > he_count * 1.3:
        return 'female'
    if he_count >= 3 and he_count > she_count * 1.3:
        return 'male'
    return None


def _parse_cast_block(script, chars):
    """Parse === EPISODE CAST === block, auto-create new characters & outfits.
    Mutates `chars` (the series characters list) — caller must save_series afterwards.
    Returns (char_id_list, {char_id: outfit_id})."""
    import re as _re
    match = _re.search(r'=== EPISODE CAST ===(.*?)=== END CAST ===', script, _re.DOTALL)
    if not match:
        return [], {}

    def _norm(s):
        return (s or '').strip().strip('"').strip("'")

    def _find_char(name, chars_list):
        nl = name.lower()
        for c in chars_list:
            if c['name'].lower() == nl:
                return c
        # fuzzy: first word match
        first = nl.split()[0] if nl.split() else nl
        for c in chars_list:
            if first and first in c['name'].lower():
                return c
        return None

    char_ids, outfit_map = [], {}  # outfit_map: {char_id: [outfit_id, ...]} (preserves order, dedups)

    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line or not line.upper().startswith('CHARACTER:'):
            continue

        # Parse pipe-separated KEY: VALUE pairs
        fields = {}
        parts = line.split('|')
        for p in parts:
            if ':' not in p:
                continue
            k, v = p.split(':', 1)
            fields[k.strip().upper()] = _norm(v)

        raw_name = fields.get('CHARACTER', '')
        if not raw_name:
            continue

        # Strip trailing parentheticals like "Claire (mother)"
        raw_name = _re.sub(r'\s*\(.*?\)\s*$', '', raw_name).strip()

        char = _find_char(raw_name, chars)
        char_was_just_created = False

        # Auto-create unknown character
        if not char:
            char_was_just_created = True
            gender = fields.get('GENDER', '').lower()
            if gender not in ('male', 'female'):
                # Robust fallback: scan the script for gendered pronouns near this
                # character's name. Beats the old tiny female-name whitelist which
                # silently defaulted everyone (Lydia, Sarah, Emma, etc.) to male.
                inferred = _infer_gender_from_script(raw_name, script)
                if inferred:
                    gender = inferred
                else:
                    gender = 'female' if any(t in raw_name.lower() for t in [
                        'claire','elena','victoria','anna','maria','lina','lydia','sarah',
                        'emma','sophia','olivia','ava','isabella','mia','charlotte',
                        'amelia','harper','evelyn','abigail','grace','chloe','luna',
                        'ella','aurora','clara','rose','ruby','hazel','lily',
                    ]) else 'male'
            look = fields.get('LOOK', '') or fields.get('APPEARANCE', '')
            od = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
            # Fallback: if writer omitted LOOK, synthesize a baseline appearance.
            if not look:
                gword = 'woman' if gender == 'female' else 'man'
                look = f"young {gword}, photogenic, neutral attractive features, mid-20s to mid-30s"
            # First appearance defines the BASE outfit. Whatever this character is
            # wearing on their introduction — that's their default look (a bear-builder
            # IS in construction gear by default; he doesn't have separate "regular"
            # clothes hidden somewhere). Embed OUTFIT_DESC into appearance so the base
            # portrait gen has the clothing description; the outfit entry below will
            # also be flagged is_base so we never duplicate-generate it as a costume change.
            if od and 'wearing' not in look.lower():
                look = look.rstrip(' ,;') + f", wearing {od[:200]}"
            # Remember the OUTFIT label this character was introduced wearing.
            # Subsequent scenes that mention the SAME label are NOT a costume
            # change — they're just the character in their default look. Stored
            # on the character so the outfit-attachment logic below can skip
            # creating a redundant separate outfit entry. A real costume change
            # = a NEW label appearing in a later scene; only THEN do we create
            # an outfit (and never auto-flag it is_base — the base IS implicit).
            base_label_intro = (fields.get('OUTFIT', 'base').split('(')[0].strip().lower() or 'base')
            char = {
                'id': str(uuid.uuid4())[:8],
                'name': raw_name,
                'description': fields.get('ROLE', '') or f'Появляется в сценарии',
                'appearance':  look,
                'gender':      gender,
                'voice_id':    '',
                'ref_images':  [],
                'outfits':     [],
                'base_outfit_label': base_label_intro,  # implicit-base marker
            }
            chars.append(char)

        char_ids.append(char['id'])

        raw_outfit = fields.get('OUTFIT', 'base')
        raw_outfit = raw_outfit.split('(')[0].strip()
        if raw_outfit.lower() == 'base' or not raw_outfit:
            continue

        # ─── DEFENSIVE CHECK: writer encoded a SEPARATE PERSON in OUTFIT field ───
        # Common Claude failure mode: writes "CHARACTER: ARIA | OUTFIT: leo_child |
        # OUTFIT_DESC: small boy in navy hoodie..." — meaning Leo is a separate person,
        # not Aria's outfit. Detect this via two signals:
        #   1) outfit_label is the first-name token of an existing or canonical character
        #   2) OUTFIT_DESC contains a different-gender / different-age-class person word
        # If detected, treat the line as a NEW CHARACTER, not an outfit of the parent.
        outfit_desc_for_check = (fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')).lower()
        person_words_male   = (' boy ', ' man ', ' male ', ' father ', ' brother ', ' uncle ', ' grandfather ')
        person_words_female = (' girl ', ' woman ', ' female ', ' mother ', ' sister ', ' aunt ', ' grandmother ')
        person_words_child  = (' child ', ' kid ', ' toddler ', ' infant ', ' baby ')
        od_padded = ' ' + outfit_desc_for_check + ' '
        char_gender = (char.get('gender') or '').lower()
        # Gender mismatch — outfit_desc describes someone of the opposite gender
        gender_mismatch = (
            (char_gender == 'female' and any(w in od_padded for w in person_words_male))
            or (char_gender == 'male' and any(w in od_padded for w in person_words_female))
        )
        # Child mismatch — adult character but outfit_desc describes a child (and char is not flagged child)
        child_mismatch = any(w in od_padded for w in person_words_child) and 'child' not in (char.get('description','') + char.get('appearance','')).lower()
        # Outfit label looks like a person name (matches another character's first-name token)
        label_first = raw_outfit.split('_')[0].lower()
        label_is_person_name = False
        if label_first and len(label_first) >= 3:
            for other in chars:
                if other['id'] == char['id']:
                    continue
                if other['name'].lower().split()[0] == label_first:
                    label_is_person_name = True
                    break

        if (gender_mismatch or child_mismatch) and (label_is_person_name or '_' in raw_outfit):
            # Promote this line into its own CHARACTER. Use the outfit_label's first token
            # (capitalized) as the candidate name. Skip the outfit-attachment for the
            # parent character — the line was misencoded.
            new_name_guess = label_first.capitalize() if label_first else None
            if new_name_guess:
                # Try to find / create the standalone character
                spawned = _find_char(new_name_guess, chars)
                if not spawned:
                    if any(w in od_padded for w in person_words_child):
                        guessed_gender = 'male' if any(w in od_padded for w in person_words_male) else (
                            'female' if any(w in od_padded for w in person_words_female) else 'male'
                        )
                    else:
                        guessed_gender = 'male' if any(w in od_padded for w in person_words_male) else (
                            'female' if any(w in od_padded for w in person_words_female) else char_gender or 'female'
                        )
                    spawned = {
                        'id': str(uuid.uuid4())[:8],
                        'name': new_name_guess,
                        'description': f'Извлечён из OUTFIT_DESC ошибочно вписанного как образ персонажа {char["name"]}',
                        'appearance': fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', ''),
                        'gender': guessed_gender,
                        'voice_id': '',
                        'ref_images': [],
                        'outfits': [],
                    }
                    chars.append(spawned)
                # Roll back: remove parent char_id we appended IF this line was the only
                # reason it was added (parent had no other valid outfit refs in this pass).
                # Simpler: just append spawned and DROP the misencoded outfit. Keep parent
                # listed (they still appear in the script).
                if spawned['id'] not in char_ids:
                    char_ids.append(spawned['id'])
                print(f'[parse_cast] PROMOTED misencoded outfit "{raw_outfit}" of {char["name"]} '
                      f'→ standalone character "{spawned["name"]}" (od_excerpt="{outfit_desc_for_check[:60]}")')
                continue  # skip the outfit-attachment below

        # Skip outfit machinery entirely when this scene's OUTFIT matches the
        # character's IMPLICIT BASE LABEL — i.e. the look they were first
        # introduced wearing. That's not a costume change, it's the default
        # appearance, already baked into char.appearance. Creating a separate
        # outfit entry here would clutter the UI with a fake "outfit" chip on
        # the character card. The scene gets no outfit_map entry → renderers
        # fall back to the base ref photo automatically.
        base_label = (char.get('base_outfit_label') or '').lower()
        if base_label and raw_outfit.lower() == base_label:
            continue

        # Find existing outfit by label
        outfit = next((o for o in char.get('outfits', [])
                       if o.get('label','').lower() == raw_outfit.lower()), None)

        # IS_BASE marker: this outfit IS the character's base look — link to ref_images, no separate gen
        is_base_flag = fields.get('IS_BASE', '').lower() in ('true', 'yes', '1')

        # Auto-create new outfit if scene declares one with description.
        # NOTE: previously we auto-flagged the FIRST outfit of a freshly-created
        # character as is_base ("AUTO-BASE"). That's now obsolete — the implicit
        # base look is captured via base_outfit_label on the character itself
        # (see new-character creation block above), and matching scenes are
        # short-circuited via the `continue` above. Anything reaching this point
        # is a REAL costume change and gets a normal (non-base) outfit entry.
        if not outfit:
            outfit_desc = fields.get('OUTFIT_DESC', '') or fields.get('OUTFITDESC', '')
            if outfit_desc or raw_outfit.lower() != 'base':
                outfit = {
                    'id':          str(uuid.uuid4())[:8],
                    'label':       raw_outfit,
                    'description': outfit_desc,
                    'photo':       '',
                    'avai_url':    '',
                }
                char.setdefault('outfits', []).append(outfit)

        # Apply explicit IS_BASE flag from the cast block (writer override).
        if outfit and is_base_flag:
            # Unmark other outfits as base (only one base per char)
            for o in char.get('outfits', []):
                if o['id'] != outfit['id'] and o.get('is_base'):
                    o['is_base'] = False
            if char.get('ref_images') and not outfit.get('photo'):
                outfit['photo'] = char['ref_images'][0]
            if char.get('avai_base_url') and not outfit.get('avai_url'):
                outfit['avai_url'] = char.get('avai_base_url', '')
            outfit['is_base'] = True
            # Keep base_outfit_label in sync so subsequent scenes with this label
            # also short-circuit instead of re-creating.
            char['base_outfit_label'] = outfit.get('label', '').lower()

        if outfit:
            lst = outfit_map.setdefault(char['id'], [])
            if outfit['id'] not in lst:
                lst.append(outfit['id'])

    return char_ids, outfit_map

