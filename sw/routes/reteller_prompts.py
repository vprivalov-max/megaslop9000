"""Reteller prompt builders: per-episode and range prompts (routes)."""
from flask import jsonify, request

from sw.core import app
from sw.llm import claude_ask_fast
from sw.storage import load_episode, load_series, save_episode
from sw.story_prompts import _build_cast_block, _outfit_ids

# ── Episode Reteller prompt ───────────────────────────────────────────────────

_RTL_PROMPT_SYSTEM = """You are a director writing a self-contained Reteller.ai production prompt for one episode of a short vertical drama (~60 seconds, ends on a cliffhanger).

The output is a SINGLE prompt that can be pasted into the generator without any extra context. The generator has NO memory of previous episodes — every visual element must be re-described from scratch.

═══════════════════════════════════════════
LANGUAGE MAP — DIFFERENT BLOCKS USE DIFFERENT LANGUAGES
═══════════════════════════════════════════
• Блок 1 (world setting) — ENGLISH
• Блок 2 (characters)   — ENGLISH (full inline descriptions)
• Блок 3 (camera)       — RUSSIAN (verbatim canonical text — see below)
• Блок 4 (cast list)    — names as written (English Latin letters)
• Блок 5 (location)     — ENGLISH
• Блок 6 (positioning)  — RUSSIAN (3–5 sentences for the director/operator)
• Блок 7 (beats)        — Dialogue text in ENGLISH inside quotes; emotion brackets in RUSSIAN; ACTION beat descriptions in RUSSIAN.

This split is intentional: blocks 1/2/5 feed an English-trained image generator; blocks 3/6 + emotions/actions are read by a Russian-speaking director. Do not "fix" the language of any block — follow the map.

═══════════════════════════════════════════
OUTPUT — EXACTLY 7 BLOCKS, IN THIS ORDER, WITH THESE EXACT HEADERS:
═══════════════════════════════════════════

═══ БЛОК 1: WORLD SETTING ═══
(English, 200–400 words, identical for all episodes of this series)
Describe: physics/rules of the world (how magic/tech/power/social system works), visual palette of BOTH sides of the conflict (warm vs cold colors, materials), key plot-driving rules (contracts, debts, laws), specific named places / plants / artifacts. Be concrete — name things.

═══ БЛОК 2: CHARACTERS ═══
(English, ONLY characters who appear in THIS episode. Each character = ONE continuous prompt-line of comma-separated descriptors, NO PERIODS, NO LINE BREAKS inside the description. Adapted to this episode's state.)

Format per character:
NAME — descriptor1, descriptor2, descriptor3, ...

The description must be self-contained enough to regenerate the character from scratch. Cover, IN THIS ORDER:
  1) overall beauty/look impression
  2) age
  3) height + body build
  4) face — shape → nose → lips → brows → cheekbones → jawline
  5) eyes — color → shape → special features → lashes
  6) hair — color → texture → length → style
  7) special non-human features (ears, fangs, glowing skin) — only if applicable
  8) headwear / crown — only if applicable
  9) clothing TOP-TO-BOTTOM — top → armor/cloak → belt → bottoms (each item with color + material)
 10) footwear (color + material)
 11) hands & fingers — skin condition → nails → rings → bracelets → scars
 12) accessories (amulets, weapons)
 13) aura / magical effects — only if applicable
 14) MANDATORY closing technical tags: "ultra realistic, cinematic lighting, high detail, unique face, not resembling any real person"
 15) style tag matching the series world ("medieval fantasy setting" / "dark fantasy" / "modern urban" / "neo-noir" / etc.)

ADAPT THE DESCRIPTION TO THIS EPISODE'S STATE. If by this episode the character has: torn clothes from a fight, fresh bruise on the cheekbone, dust in the hair, a new tattoo, broken amulet (empty cord), stained dress, tear-streaked makeup — INCLUDE these adaptations in the description for THIS episode. The description is not a static character sheet — it is "what the character looks like RIGHT NOW in this episode".

═══ WARDROBE CHANGES — HARD RULE, DO NOT VIOLATE ═══
If the input data lists MORE THAN ONE outfit for the same character in this episode (you will see "this episode wears: CHANGES CLOTHES — outfit_A (...); outfit_B (...)" or multiple CHARACTER lines for the same name in the script's === EPISODE CAST === block), then BLOCK 2 MUST contain a SEPARATE entry per outfit:

   CLAIRE (scene 1 — morning_lingerie) — full descriptor line ending with "...white silk slip, hair messy from sleep, bare feet, no makeup, ultra realistic..."
   CLAIRE (scene 2 — gala_gown) — full descriptor line ending with "...floor-length black silk gown, smoky eyeliner, hair pinned up, diamond earrings, ultra realistic..."

EACH entry repeats the WHOLE top-to-bottom description (face, eyes, hair, body, technical tags) — only clothing/hair/makeup change between entries. The image generator does NOT carry visual state between entries; if you write only one entry, it will use that look for every scene.

This is NOT optional. If you see two outfits in the input, you write two entries. If you see three outfits, three entries. Same character name, parenthetical scene tag, full re-description each time.

═══ БЛОК 3: ИНСТРУКЦИИ ПО СЪЕМКЕ ═══
COPY THE FOLLOWING TEXT VERBATIM IN RUSSIAN — DO NOT TRANSLATE, DO NOT PARAPHRASE, DO NOT SHORTEN:

Статичной камеры нет вообще, камера всегда медленно двигается. В самые напряженные моменты камера становится агрессивной, резкой с применением различных операторских приемов таких как голландский угол, ручная живая камера, резкие зумы, резкие ракурсы, игра света и теней, изменение света, моргание и тд. СТИЛЬ МАКСИМАЛЬНО ФОТОРЕАЛИСТИЧНЫЙ ДОКУМЕНТАЛЬНЫЙ, как будто снято на айфон. Фон — размытый. Смена ракурса и крупности плана регулярная в самые нужные моменты.

═══ БЛОК 4: ДЕЙСТВУЮЩИЕ ЛИЦА ═══
Plain comma-separated list of character names present in this episode (English Latin letters, exactly as in Block 2).

═══ БЛОК 5: LOCATION ═══
(English, 80–150 words PER location. If the episode changes location, write a sub-block per location: "LOCATION 1: NAME", "LOCATION 2: NAME".)
For each location used in this episode write a paragraph covering:
  - room/space dimensions
  - wall / floor / ceiling materials
  - light sources and the type of light
  - specific objects in frame
  - atmosphere — ambient sounds, smells, the felt sense of the space
If the location recurs from an earlier episode and something has changed (broken vials, overturned chair, dried blood, missing painting) — name what changed. Be concrete, not generic.

═══ БЛОК 6: ПОЛОЖЕНИЕ В КАДРЕ ═══
(RUSSIAN, 3–5 sentences for the director/operator.)
Кто где стоит / сидит / лежит, куда движется камера, какие крупности планов используются, ключевые визуальные моменты эпизода (например: «крупный план дрожащих пальцев на стакане → отъезд на средний план, когда антагонист входит в кадр со спины → внезапный голландский угол на финальной реплике»).

═══ БЛОК 7: РЕПЛИКИ ═══
HARD CONTRACT — read carefully:
1. COUNT every dialogue line in the script (every quoted line spoken by a character). Call this N.
2. БЛОК 7 MUST contain EXACTLY N dialogue beats — no more, no less.
   • If script has 14 dialogue lines → Block 7 has 14 dialogue beats.
   • Do NOT merge two short lines into one beat.
   • Do NOT split one long line into two beats.
   • Do NOT skip any line, even short ones like "Oh.", "Wait.", "What?".
   • Do NOT add invented dialogue beats not in the script.
3. ACTION beats sit BETWEEN dialogue beats, ONE PER non-verbal moment described in the script's action lines (slap, door slam, fall, character entrance). They are extra — they do NOT count toward N.

Format:
  Dialogue beat:  N - NAME — "exact line from script" [эмоция на русском]
  Action beat:    N - ACTION — описание действия на русском

Beat numbering is sequential across BOTH dialogue and action beats (1, 2, 3, ... in order of occurrence).

Rules for dialogue text inside the quotes:
• Conversational AMERICAN English — not literary, not British. Short phrases, max 1–2 sentences. Slang allowed when fits the character. Interjections welcome ("huh", "wait", "look", "hey"). Contractions standard ("don't", "can't", "you're", "gonna", "wanna").
• Copy from the script. The screenwriter already wrote in this style — preserve their wording. If you spot a literary phrase, you MAY tighten it into conversational form, but do NOT invent dialogue that wasn't in the script.
• Speaker NAME = exact match to the script's speaker label.
• Order = script order. Do not reorder.

Rules for the emotion bracket [...]:
• Always in RUSSIAN.
• Describes HOW the line is said: tone, volume, body cue. Examples: [тихо, сквозь зубы] [срывается на крик] [холодно, не моргая] [шёпотом, на грани слёз] [с холодной усмешкой].
• Not optional — every dialogue beat has one.

Rules for ACTION beats:
• Description in RUSSIAN, present tense, concrete physical action ("Антагонист резко ставит стакан на стойку, осколки разлетаются по полу").
• Used only for moments the script explicitly contains as action lines.

LAST beat must preserve the script's cliffhanger ending exactly — if the script ends on a line of dialogue, the last beat is that dialogue beat verbatim; if the script ends on an action, the last beat is an ACTION beat matching it.

═══════════════════════════════════════════
PRE-OUTPUT CHECKLIST (silent — verify each before sending):
☐ Block 1 in English, 200–400 words, world physics + palette + named things
☐ Block 2 in English; one line per character actually in this episode; comma-separated descriptors with NO periods; full top-to-bottom coverage; ends with technical tags + style tag; ADAPTED to this episode's state
☐ Block 3 = canonical RUSSIAN camera paragraph, copied VERBATIM
☐ Block 4 = names only, comma-separated
☐ Block 5 in English, 80–150 words per location, recurring locations note what changed
☐ Block 6 in RUSSIAN, 3–5 sentences
☐ Block 7 numbered, dialogue text inside quotes in English, [эмоция] brackets in Russian, ACTION beats in Russian
☐ Block 7 dialogue beat count == script dialogue line count, in script order, last beat = cliffhanger
☐ Episode ≈ 60 seconds
☐ Self-contained (no references to other episodes in generator-facing blocks)

OUTPUT ONLY the 7 blocks separated by their headers. No preamble, no commentary, no JSON, no markdown fences."""


@app.route('/api/series/<sid>/episodes/<int:num>/reteller-prompt', methods=['POST'])
def episode_reteller_prompt(sid, num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'not found'}), 404
    script = ep.get('script', '').strip()
    if not script:
        return jsonify({'error': 'Сначала напиши или сгенерируй сценарий'}), 400

    cast_block = _build_cast_block(s, ep)

    # Pass FULL world + style + per-character + per-location detail so the model can
    # fill BLOCK 1 (world), BLOCK 2 (characters), BLOCK 5 (location) with real content
    # rather than inventing it. The 7-block contract requires all of these in English.
    chars_map = {c['id']: c for c in s.get('characters', [])}
    locs_map  = {l['id']: l for l in s.get('locations', [])}
    ep_outfits = ep.get('character_outfits', {})

    char_details = []
    for cid in ep.get('characters_used', []):
        c = chars_map.get(cid)
        if not c: continue
        outfit_ids_list = _outfit_ids(ep_outfits.get(cid))
        ep_outfits_objs = [
            o for oid in outfit_ids_list
            for o in c.get('outfits', []) if o['id'] == oid
        ]
        # Base line — one per character, with stable identity descriptors
        char_details.append(
            f'  • {c["name"]} ({c.get("gender","")}) — '
            f'appearance: {c.get("appearance","")} | description: {c.get("description","")}'
        )
        # Active disguise / new-identity state — HOLDS across episodes until the
        # story reveals the character. Tell the writer to describe the disguised
        # hair (not the natural hair) and to tag the disguise outfit every scene,
        # so the look doesn't flip back mid-arc (Claire→blonde "Emma Cross" bug).
        _idsh = c.get('identity_shift') or {}
        if _idsh.get('active'):
            _alias = _idsh.get('alias') or ''
            _hair = _idsh.get('hair') or ''
            char_details.append(
                f'      ⚠ UNDERCOVER / NEW IDENTITY (HOLDS until story reveals her): '
                f'{c["name"]} is currently disguised'
                + (f' as "{_alias}"' if _alias else '')
                + (f' — HAIR IS {_hair.upper()} in this state (NOT the natural hair above)' if _hair else '')
                + '. In Block 2 describe '
                + (_hair + ' hair' if _hair else 'the disguised hair')
                + ', and tag a dedicated disguise OUTFIT (its OUTFIT_DESC must include the '
                + (_hair + ' hair / wig' if _hair else 'disguise hair')
                + ') in EVERY scene — do NOT reuse her pre-disguise looks.'
            )
        # Wardrobe entries — ONE LINE PER OUTFIT so the AI can\'t miss them.
        # Multi-outfit characters get a hard "CHANGES CLOTHES — N entries" header.
        if len(ep_outfits_objs) >= 2:
            char_details.append(
                f'      ⚠ CHANGES CLOTHES IN THIS EPISODE — {len(ep_outfits_objs)} separate looks. '
                f'Block 2 MUST have {len(ep_outfits_objs)} entries for {c["name"]} '
                f'(one per scene/outfit, with full top-to-bottom re-description).'
            )
            for idx, o in enumerate(ep_outfits_objs, 1):
                desc = (o.get('description') or '').strip()
                char_details.append(
                    f'      ↳ look {idx} — outfit_label="{o["label"]}" | wardrobe & state: {desc or "(no description, infer from outfit_label)"}'
                )
        elif len(ep_outfits_objs) == 1:
            o = ep_outfits_objs[0]
            desc = (o.get('description') or '').strip()
            char_details.append(
                f'      wears: {o["label"]} — {desc or "(see character base appearance)"}'
            )
        else:
            char_details.append('      wears: base look (default appearance from character description)')
    char_block = ('CHARACTERS IN THIS EPISODE — full reference data (use to write BLOCK 2):\n'
                  + '\n'.join(char_details)) if char_details else ''

    loc_details = []
    for lid in ep.get('locations_used', []):
        l = locs_map.get(lid)
        if not l: continue
        loc_details.append(f'  • {l["name"]} — {l.get("description","")}')
    loc_block = ('LOCATIONS IN THIS EPISODE — full reference data (use to write BLOCK 5):\n'
                 + '\n'.join(loc_details)) if loc_details else ''

    world = s.get('world_description', '')
    style_type = s.get('style', {}).get('type', '')
    style_custom = s.get('style', {}).get('custom_description', '')
    world_ctx = (
        f'SERIES WORLD (use to write BLOCK 1, expand to 200–400 English words):\n'
        f'  Title: {s["title"]} | Genre: {s.get("genre","")} | Tone: {s.get("tone","")} | Visual style: {style_type}\n'
        f'  World: {world}\n'
        + (f'  Custom style notes: {style_custom}\n' if style_custom else '')
    )

    prompt = (
        world_ctx + '\n'
        + (char_block + '\n\n' if char_block else '')
        + (loc_block + '\n\n' if loc_block else '')
        + (cast_block + '\n\n' if cast_block else '')
        + f'EPISODE {num} SYNOPSIS: {ep.get("synopsis","")}\n\n'
        + f'EPISODE {num} SCRIPT (source — convert into the 7-block prompt):\n{script}\n\n'
        + 'TASK: Output the complete 7-block Reteller production prompt for this episode, following your system instructions EXACTLY. Per-block reminders:\n'
        + '— БЛОК 1 (English, 200–400 words): expand SERIES WORLD above into a full description — physics, palette of both sides, named places/items/laws.\n'
        + '— БЛОК 2 (English, full inline descriptions): ONE line per character present in this episode. Use the CHARACTERS data above (appearance + this-episode outfit) and the script to write a comma-separated description with NO periods, covering: overall look → age → height/build → face (shape/nose/lips/brows/cheekbones/jaw) → eyes → hair → headwear if any → clothing top-to-bottom (each item with color + material) → footwear → hands/rings → accessories → aura (if magical) → end with the mandatory tags `ultra realistic, cinematic lighting, high detail, unique face, not resembling any real person` + a style tag matching the world. ADAPT to this episode\'s state (torn clothes after a fight, fresh bruise, dust in hair, broken amulet, tear-streaked makeup — whatever the script implies for THIS scene).\n'
        + '— БЛОК 3: copy the canonical RUSSIAN camera-instructions paragraph from your system prompt VERBATIM — do not translate or paraphrase.\n'
        + '— БЛОК 4: comma-separated character names (English Latin letters as in Block 2).\n'
        + '— БЛОК 5 (English, 80–150 words per location): use LOCATIONS data above + script context. Cover dimensions, wall/floor/ceiling materials, light sources, specific objects, atmosphere. Note what changed if the location recurs.\n'
        + '— БЛОК 6 (RUSSIAN, 3–5 sentences): кто где находится, движение камеры, крупности планов, ключевые визуальные моменты эпизода.\n'
        + '— БЛОК 7: numbered beats interleaving dialogue and ACTION. DIALOGUE TEXT INSIDE QUOTES = the script\'s line, preserved word-for-word (the screenwriter already wrote conversational American English — keep it as-is, do not reorder, do not skip, do not invent). The [...] emotion bracket is in RUSSIAN describing HOW the line is said. ACTION beats use RUSSIAN descriptions of physical action. Number of dialogue beats == number of dialogue lines in the script. LAST beat preserves the script\'s cliffhanger.\n'
        + 'Output ONLY the 7 blocks separated by their headers. No commentary.'
    )
    def _count_script_dialogue_lines(scr: str) -> int:
        """Count quoted dialogue lines in the script. A dialogue line is a
        quoted string in a screenplay block — we count occurrences of opening
        smart-quote " or straight " preceding text on a line, OR a NAME:
        followed by a non-empty quoted line on the next non-blank line."""
        import re as _re
        # Count "..."  and "..." (smart and straight) — each opening quote = 1 line
        # Smart quotes used in scripts: " (left double) and ' (left single for stage)
        count = 0
        for m in _re.finditer(r'[“"][^”"\n]{1,400}[”"]', scr):
            count += 1
        return count

    def _count_block7_dialogue_beats(rtl: str) -> int:
        import re as _re
        # Beat lines look like: "1 - NAME — "...""  or "1 - ACTION — ..."
        # Dialogue beats contain a quoted string.
        c = 0
        for ln in rtl.splitlines():
            if _re.match(r'\s*\d+\s*-\s*[A-Z]', ln) and ('"' in ln or '“' in ln) and 'ACTION' not in ln.split('—')[0].upper():
                c += 1
        return c

    def _cyrillic_leak(rtl: str) -> int:
        """Count Cyrillic chars in blocks where Russian is FORBIDDEN.
        Russian IS expected in: Block 3 (camera, full), Block 6 (positioning, full),
        Block 7 emotion brackets [...] and ACTION beat descriptions.
        Russian is FORBIDDEN in: Block 1, 2, 4, 5; and inside dialogue quotes in Block 7.
        Block headers (═══ БЛОК ...) are always allowed to keep Cyrillic."""
        import re as _re
        leaked = 0
        block = 0
        for ln in rtl.splitlines():
            m = _re.match(r'\s*═══\s*БЛОК\s*(\d+)', ln)
            if m:
                block = int(m.group(1))
                continue
            if block in (3, 6):
                continue  # Russian fully allowed
            if block == 7:
                # Inside Block 7: only check dialogue text inside quotes — those must be English.
                # Strip emotion brackets and the post-name action descriptions; keep only quoted text.
                quoted = _re.findall(r'[“"]([^”"\n]+)[”"]', ln)
                for q in quoted:
                    for ch in q:
                        if '\u0400' <= ch <= '\u04FF':
                            leaked += 1
                continue
            if block in (1, 2, 4, 5):
                for ch in ln:
                    if '\u0400' <= ch <= '\u04FF':
                        leaked += 1
        return leaked

    expected_lines = _count_script_dialogue_lines(script)
    try:
        rtl_prompt = claude_ask_fast(prompt, system=_RTL_PROMPT_SYSTEM)
        got_lines = _count_block7_dialogue_beats(rtl_prompt)
        cyr = _cyrillic_leak(rtl_prompt)
        need_retry = (expected_lines and abs(got_lines - expected_lines) > 0) or cyr > 0
        if need_retry:
            reasons = []
            if expected_lines and abs(got_lines - expected_lines) > 0:
                reasons.append(f'dialogue count mismatch: script={expected_lines} block7={got_lines}')
            if cyr > 0:
                reasons.append(f'cyrillic leak: {cyr} chars outside headers')
            print(f'[reteller-prompt] retry needed — {"; ".join(reasons)}', flush=True)
            retry_prompt = (
                prompt
                + f'\n\nSTRICT FIX:\n'
                + (f'• The script has EXACTLY {expected_lines} dialogue lines (quoted spoken lines). Your previous output had {got_lines} dialogue beats. That is wrong. Block 7 MUST contain EXACTLY {expected_lines} dialogue beats — one per script line, in order, verbatim. ACTION beats are separate and not counted toward this number.\n' if (expected_lines and abs(got_lines - expected_lines) > 0) else '')
                + (f'• Your previous output contained {cyr} Cyrillic characters in blocks where Russian is FORBIDDEN. Russian is allowed ONLY in Block 3 (camera), Block 6 (positioning), Block 7 [эмоция] brackets, and Block 7 ACTION beat descriptions. Block 1 (world), Block 2 (characters), Block 4 (cast list), Block 5 (location), and the dialogue text INSIDE QUOTES in Block 7 — all of these MUST be English. Rewrite the offending blocks in English; keep blocks 3 and 6 + emotions + ACTION descriptions in Russian as required by the spec.\n' if cyr > 0 else '')
                + 'Regenerate the entire 7-block prompt now with these fixes.'
            )
            rtl_prompt2 = claude_ask_fast(retry_prompt, system=_RTL_PROMPT_SYSTEM)
            got2 = _count_block7_dialogue_beats(rtl_prompt2)
            cyr2 = _cyrillic_leak(rtl_prompt2)
            print(f'[reteller-prompt] retry result: block7={got2} (target {expected_lines}), cyrillic={cyr2}', flush=True)
            # Pick whichever is closer to clean
            score_old = abs(got_lines - (expected_lines or got_lines)) + cyr
            score_new = abs(got2 - (expected_lines or got2)) + cyr2
            if score_new < score_old:
                rtl_prompt = rtl_prompt2
        ep['reteller_prompt'] = rtl_prompt
        ep['reteller_prompt_dialogue_check'] = {
            'script_lines': expected_lines,
            'block7_beats': _count_block7_dialogue_beats(rtl_prompt),
            'cyrillic_leak': _cyrillic_leak(rtl_prompt),
        }
        save_episode(sid, num, ep)
        return jsonify({
            'prompt': rtl_prompt,
            'dialogue_check': ep['reteller_prompt_dialogue_check'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Range Reteller prompt ─────────────────────────────────────────────────────

@app.route('/api/series/<sid>/reteller/range-prompt', methods=['POST'])
def range_reteller_prompt(sid):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    data   = request.json or {}
    from_n = int(data.get('from', 1))
    to_n   = int(data.get('to', 1))

    episodes = [load_episode(sid, n) for n in range(from_n, to_n + 1)]
    episodes = [e for e in episodes if e]

    # Collect characters/locations and ALL outfits used per character across range
    all_char_ids = set()
    all_loc_ids  = set()
    # char_id -> set of outfit_ids used in this range
    char_outfits_used: dict = {}

    for ep in episodes:
        all_char_ids.update(ep.get('characters_used', []))
        all_loc_ids.update(ep.get('locations_used', []))
        ep_co = ep.get('character_outfits', {}) or {}
        for cid, raw in ep_co.items():
            if cid in ep.get('characters_used', []):
                for oid in _outfit_ids(raw):
                    char_outfits_used.setdefault(cid, set()).add(oid)

    chars_in_range = [c for c in s.get('characters', []) if c['id'] in all_char_ids]
    locs_in_range  = [l for l in s.get('locations',  []) if l['id']  in all_loc_ids]

    # Collect pre-generated reteller_prompt from each episode — no Claude calls needed
    missing = []
    ep_prompts = []
    for ep in episodes:
        rp = ep.get('reteller_prompt', '').strip()
        if rp:
            ep_prompts.append(f'{"━"*51}\n{s["title"].upper()} — EPISODE {ep["number"]}\n{"━"*51}\n\n{rp}')
        else:
            missing.append(ep['number'])

    rtl_prompt = '\n\n\n'.join(ep_prompts)
    if missing:
        note = f'⚠ Episodes without Reteller prompt (generate script first): {", ".join(str(n) for n in missing)}\n\n'
        rtl_prompt = note + rtl_prompt if rtl_prompt else note.strip()

    # Build character list with all outfits used in range
    chars_out = []
    for c in chars_in_range:
        outfit_ids = char_outfits_used.get(c['id'], set())
        outfits = [o for o in c.get('outfits', []) if o['id'] in outfit_ids]
        chars_out.append({
            'id': c['id'],
            'name': c['name'],
            'outfits': [{'id': o['id'], 'label': o['label']} for o in outfits],
            'has_multiple_outfits': len(outfits) > 1,
        })
    return jsonify({
        'prompt': rtl_prompt,
        'characters': chars_out,
        'locations':  [{'id': l['id'], 'name': l['name']} for l in locs_in_range],
        'missing_prompts': missing or None,
    })
