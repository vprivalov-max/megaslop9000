"""Seedance batch routes: batch-compose (all main chunks of one episode in a
single Claude call) and TURBO revise-batch."""
import datetime
import hashlib
import json
import re

from flask import jsonify, request

from sw.auth import current_user_email
from sw.config import DEFAULT_AUTO_REVISE_INSTRUCTION, _load_user_settings
from sw.core import app
from sw.imagetags import _build_image_tag_remap, _remap_image_tags
from sw.jsonutils import loads_lenient, strip_json
from sw.llm import claude_ask
from sw.locks import _episode_lock
from sw.logging_utils import _log_event
from sw.seedance import _resolve_ref_url
from sw.storage import _sync_script_outfits, load_episode, load_series, save_episode
from sw.textrules_banlists import (_build_episode_tag_mapping,
                                   _prev_episode_ending_context,
                                   _seedance_apply_banlist,
                                   _seedance_inject_blocking,
                                   _seedance_inject_hard_cuts,
                                   _seedance_validate_autonomy,
                                   _seedance_validate_durations,
                                   _seedance_validate_wardrobe)
from sw.textrules_sanitizer import _canonical_char_description

_SD_BATCH_RULES = """
You are breaking ONE episode of a TikTok-style short drama into a sequence of segments for Seedance 2.0. Each segment is its own Seedance generation, autonomous, with its own ready-to-render promptEn.

═══ THE GOLDEN RULE — AUTONOMY ═══
Seedance does NOT remember previous prompts. Each segment is described as if it's the only one the model will see.
In every segment's promptEn re-describe from scratch:
- the full location (no "as before")
- which characters are in frame and their @ImageN tag bindings
- props and where they are placed
- character poses and positions
- time of day, weather, lighting

FORBIDDEN words in promptEn: "same", "still", "as before", "as previous", "continues", "прежний", "тот же" — any reference to what the model "saw earlier". Server validates this and warns on violations.

If a location stays the same across segments → COPY the description verbatim, do NOT shorten or refer back.

═══ DIALOGUE ═══
- Take dialogue lines VERBATIM from the script's `Name: text` format. Translate to English LITERALLY (no creative paraphrase) — preserve meaning, emotion, length.
- In Action timeline insert dialogue as: `{Name} says: "..."` or `{Name} whispers: "..."` or `{Name} shouts: "..."`.
- ALL dialogues from script chunk_text must be covered across the segment(s) where they appear. Don't drop lines.
- Story sequence must match the script — order of who-says-what is sacred.

═══ @Image TAG NUMBERING ═══
EPISODE-LEVEL — fixed by the server BEFORE you see the input. The TAG MAPPING input gives you `@Image1`, `@Image2`, ... mapped to specific characters and locations by first-appearance order in the script. Use ONLY these tags. Locations get tags too.

═══ WARDROBE FIDELITY ═══
Each character in TAG MAPPING has a `wardrobe` field (their base/default outfit) and an `outfit_options` list (available costume variants). The ACTIVE wardrobe for a character in a segment is the outfit matching their chosen `variantId` — see OUTFIT SELECTION below. Clothing words in actionTimeline must come from the chosen outfit's description.

Rules for actionTimeline.description:
1. NEVER mention clothing/footwear/headwear/outerwear/accessories that aren't in the character's active outfit. Before writing: jacket, blazer, coat, suit jacket, tie, scarf, hat, cap, beanie, gloves, boots, sunglasses, watch, belt, vest, hoodie, sweater — verify they appear in the chosen outfit. If not — DO NOT write them.
2. Pockets: pick a pocket consistent with what they have on. Safe default: `trouser pocket`. `inside breast pocket of the jacket` ONLY if jacket is in active outfit.
3. Pose, dirt, blood, tears, hair state, expression, flushed cheeks, trembling hands — describe freely. The ban is ONLY about wardrobe items / accessories.
4. Before submitting, re-check each `description` and verify that every clothing/accessory word appears in the active outfit of one of `charactersInSegment`. If not — rephrase.

═══ OUTFIT SELECTION ═══
Each character entry in TAG MAPPING has `wardrobe` (base/default outfit) and `outfit_options` (available variants with descriptions). Choose the right outfit per character per segment:

BASE = character's PRIMARY outfit — what they naturally wear in their default environment. Use `"base"` whenever the scene is within that character's normal domain. Do NOT switch to a variant just because one exists.

NON-BASE = use ONLY when this specific character's situation in the scene explicitly requires different clothing:
  • THIS character is in bed / lying down / going to sleep / waking up → look for: pajamas, nightgown, sleepwear. NOTE: being in a bedroom does NOT trigger this for every character — only for the one who is actually in bed. A character sitting next to a bed, visiting a bedroom, or standing in the room keeps their base outfit.
  • THIS character at beach / pool / swimming → swimsuit, swimwear, bikini
  • THIS character at gym / workout / training → sportswear, athletic, gym
  • THIS character at funeral / mourning → black_dress, dark_suit, formal
  • THIS character at wedding / ceremony → wedding_dress, tuxedo, formal
  • THIS character in court / legal hearing → business_suit, court_suit
  • THIS character as hospital PATIENT (not staff) → hospital_gown — staff who work there keep base
  • THIS character in prison / arrested → prison_jumpsuit, inmate
  • THIS character in shower / bath → bathrobe, towel
  • THIS character off-duty / at home when base is a work uniform → casual, street, home

ALGORITHM per character per segment:
  1. Read segment text + scene heading. Does THIS character (not the scene location) have a context trigger?
  2. YES → scan this character's `outfit_options` labels and descriptions for a semantic match.
  3. Found → set `variantId` to that label.
  4. No match or no trigger → set `variantId` to `"base"` (never invent a label not in `outfit_options`).
  5. SAME-SCENE LOCK: once variantId is set for a character in a scene, keep it for subsequent segments of that same scene unless the script shows an explicit costume change.

═══ SHOT RHYTHM ═══
- A 15-second segment is split into 2-4 shots, each 2-7 seconds. Sum of shot lengths = durationSec.
- Shorter segments (5-12s) split into 1-3 shots proportionally.
- FORBIDDEN: monotone equal-length cuts (e.g. 0-3/3-7/7-11/11-15 or 0-5/5-10/10-15). Vary the rhythm: 2/5/4/4 or 6/4/5 or 3/7/5 — real cinematic pacing.

═══ ESTABLISHING SHOT — when seg_specs has `establishing_shot: true` ═══
This segment is the FIRST chunk of a new location (scene change). Open it with a 2-second establishing shot of the location's exterior/facade BEFORE any dialogue or character action.
- First shot in actionTimeline: `0-2s: wide shot, static frame, exterior facade of <Location>, no people in frame, no dialogue.`
- Second shot onward (2s → durationSec): regular dialogue/action as usual, with characters now inside.
- The 2-second beat counts toward the segment's total durationSec (NOT a separate Seedance call).
- Constraints block must say `no people in establishing shot 0-2s`.
- Refs: keep ALL refs (location + characters); the model needs character refs for shots after the 2s mark.
- For non-establishing segments (`establishing_shot: false` or missing), ignore this section entirely.

═══ CAMERA — embedded in each shot ═══
Format: "[shot size] [movement or static], [angle], [action]"
Example: "0-5s: medium slow push-in, eye-level, Lina opens the leather folder and looks down at the document."
2-3 camera attributes per shot, camera before action.

Camera movement — ONLY when the shot benefits. If stillness serves better → use "static frame" / "locked-off". DON'T add motion to every shot.

Vocabulary: wide shot, medium shot, close-up, extreme close-up, low angle, high angle, eye-level, over-the-shoulder, tracking shot, slow push-in, handheld drift, orbit, pan, tilt, rack focus, whip pan, static frame, forward drift, pull-back.

═══ EYELINE — gaze direction of the speaker ═══
MANDATORY in every shot description with dialogue:
- Speaker's body is turned toward addressee (per blocking).
- Speaker's eyes look at addressee, NOT at camera, NOT at floor.
- Write directly: "Kyle on the right side of frame turns his head to face Hale who stands on the left, eyes locked on Hale. Kyle says: \\"...\\"".
- FORBIDDEN: speaker facing camera while addressee is behind their back — eyeline violation. Even if only the speaker is in frame, describe where they're looking ("sideways toward Hale's offscreen-left position").

═══ CONTINUITY between segments (within the same scene) ═══
- Pose at end of segment N → start of segment N+1 matches or shows the transition.
- Position relative to props is preserved.
- Props remain where left (unless used / removed in frame).
- Physical state preserved (blood, dirt, tears) once established.
- Don't change without reason: pose, costume, appearance, props, interior.
- Change only when the script demands.

═══ CROSS-EPISODE CONTINUITY ═══
TikTok-format episodes often pick up immediately where the previous episode left off — same scene, same characters, same positions. Server may pass you a `PREV EPISODE CONTEXT` block with: (1) prev episode's last scene heading, (2) prev episode's last 6 lines, (3) prev episodeBlocking, (4) prev last chunk's ending_state.

If THIS episode's first segment is a CONTINUATION of the same scene (same location, no new INT./EXT. heading change at script top, no explicit time-jump in opening lines like "утром" / "next day" / "через час") — INHERIT positions from PREV EP. Adrian at the door → he's still at the door. Clara at the desk → still there. Reflect this in `episodeBlocking` of THIS episode.

If the scene CLEARLY changed (new location heading, time-jump phrase, fresh setup) — start positioning from scratch and IGNORE PREV EP context.

═══ SCENE BLOCKING (top-level field — REQUIRED) ═══
You return a top-level `episodeBlocking` field — a single English paragraph 60-120 words describing the episode's spatial setup. Server automatically injects this into the Constraints of every segment that uses overlapping @ImageN tags.

If the user provided SCENE BLOCKING (you'll see it in input) — copy it VERBATIM into `episodeBlocking`. Do NOT paraphrase.
If not provided — generate it yourself: where does the table/chairs/window/door sit; where does each @ImageN character stand or sit relative to props and to each other; when does each character enter/exit; what props are on stage and where.

Example: `"@Image2 Kyle seated at the FAR RIGHT head of the long table, body angled camera-left toward Hale; laptop closed in front of him. @Image1 Hale enters from back-left door at start of segment 1, walks to LEFT side of the table, body angled camera-right toward Kyle. Leather folder at centre-left of the table from segment 3 onward."`

═══ promptEn STRUCTURE — STRICT 6 BLOCKS, ENGLISH ═══
```
Location: <full English description from scratch — no "as before">

Characters in this segment: Name @Image1, Name @Image2

Action timeline:
0-5s: medium slow push-in, eye-level, <action>. <Name> says: "<dialogue verbatim>"
5-12s: close-up, static frame, <reaction>.
12-15s: pull-back, eye-level, <final action>.

Lighting and atmosphere: <light + atmosphere>

Style: <cinematic / documentary / dramatic + 1-2 specifics>

Constraints: use <Name1> as @Image1, use <Name2> as @Image2, keep exact facial identity and outfit from each reference image, <positions/poses>, <props>, no extra characters, no text, no watermark, maintain environment consistency
```

═══ OUTPUT JSON SCHEMA — STRICT ═══
{
  "totalDurationSec": <integer sum of all durationSecs>,
  "scriptDialogueCount": <number of Name: lines in the script>,
  "episodeBlocking": "60-120 word English geometry paragraph (verbatim from input if provided)",
  "segments": [
    {
      "anchor": "<COPY EXACTLY from input — this is the segment ID>",
      "plan": {
        "chunkIndex": <0-based or sceneIdx.segIdx — copy from input>,
        "kind": "main",
        "durationSec": <copy from input>,
        "summaryRu": "1-2 sentence Russian description of what happens",
        "locationDescription": "<full English location description>",
        "charactersInSegment": [
          { "tag": "@Image1", "name": "Lina", "slug": "<id from tag mapping>", "variantId": "base" }
          // variantId: "base" = default outfit, OR a label from outfit_options (e.g. "pajamas") when THIS character's situation requires it — see OUTFIT SELECTION.
        ],
        "actionTimeline": [
          {
            "fromSec": 0, "toSec": 5,
            "description": "medium slow push-in, eye-level, Lina opens the folder. Lina says: \\"Open the folder.\\"",
            "dialogue": {
              "speakerTag": "@Image1", "speakerName": "Lina",
              "en": "Open the folder.",
              "ruOriginal": "Откройте папку.",
              "scriptLineNumber": 1
            }
          }
        ],
        "lightingAndAtmosphere": "Cold morning light...",
        "style": "cinematic film tone, 35mm grain",
        "constraintsExtra": ["specific positional constraint 1", "..."],
        "dialogues": [
          { "speakerTag": "@Image1", "speakerName": "Lina", "ru": "...", "en": "...", "scriptLineNumber": 1 }
        ],
        "riskFlags": [],
        "close_up": false
      },
      "prompt": {
        "promptEn": "Location: <full description>\\n\\nCharacters in this segment: Lina @Image1\\n\\nAction timeline:\\n0-5s: ...\\n\\nLighting and atmosphere: ...\\n\\nStyle: ...\\n\\nConstraints: use Lina as @Image1, keep exact facial identity and outfit from each reference image, no extra characters, no text, no watermark, maintain environment consistency",
        "tagsUsed": ["@Image1", "@Image2"],
        "appliedReplacements": [],
        "riskLevel": "low",
        "aspectRatio": "9:16"
      }
    }
  ]
}

═══ MANDATORY CHECKLIST — verify before output ═══
1. `segments[]` length matches input segments count exactly.
2. Each `anchor` copied verbatim from input — server matches by anchor.
3. Each `plan.charactersInSegment` uses ONLY tags from TAG MAPPING.
4. Each `prompt.promptEn` follows the 6-block structure with `\\n\\n` between blocks.
5. No "same/still/as before/continues" anywhere in any promptEn.
6. Each `actionTimeline` shot's (toSec - fromSec) sums equal `durationSec`.
7. No clothing/accessory words outside the character's `wardrobe`.
8. `episodeBlocking` populated (verbatim from input if provided, else generated).
9. Constraints block of every promptEn ends with: `no extra characters, no text, no watermark, maintain environment consistency`.

Return ONLY valid JSON. No markdown fences. No commentary.
"""

# ─────────────────────────────────────────────────────────────────────────────
# TURBO-mode auto-revise: one Claude call rewrites ALL main promptEns of one
# episode according to a single Russian instruction (from per-user settings).
# Mirrors colleague's REVISE_EPISODE_RULES in
# /tmp/shadow-founder/src/main/services/chunk-builder.ts (ShadowFounder reference).
# Used by the Turbo auto-pipeline after batch-compose, and by the manual
# «✨ Применить правку» button on the episode page. Sequential mode never
# touches this.
# ─────────────────────────────────────────────────────────────────────────────
_SD_REVISE_BATCH_RULES = """
Ты редактируешь набор готовых Seedance-промптов всех main-чанков ОДНОЙ серии. Серия идёт непрерывно: это последовательность 5-15-секундных клипов в одной (или нескольких) локации с теми же персонажами. Между чанками действует общий episodeBlocking (расстановка персонажей).

ЦЕЛЬ ПРАВКИ — задана пользователем на русском. Применяй её ко ВСЕМ чанкам СОГЛАСОВАННО. Если правка про continuity между чанками («кадры скачут», «свет меняется», «персонаж дёргается», «телепортируется») — синхронизируй описания всех чанков. Если правка про конкретный аспект во всей серии («больше статичных кадров», «убери handheld», «следи за длинной реплик») — примени везде.

ОБЯЗАТЕЛЬНО СОХРАНИТЬ В КАЖДОМ ЧАНКЕ:
1. Структура из 6 блоков ровно в порядке: Location / Characters in this segment / Action timeline / Lighting and atmosphere / Style / Constraints.
2. Реплики в кавычках («{Name} says: "..."») — ДОСЛОВНО, если пользователь явно не просит их менять. Если правка просит укоротить реплики — укороти, сохранив смысл и эмоции.
3. Тайминги шотов (0-5s, 5-10s и т.п.) и общая длительность чанка в секундах не меняй.
4. ЗАПРЕЩЕНО в финальном тексте: "same", "still", "as before", "as previous", "continues", "previous chunk", "прежний", "тот же" — каждый чанк автономен.
5. Не добавляй одежду/аксессуары которых нет в активном outfit персонажа.

ДОБАВЛЕНИЕ / ИЗМЕНЕНИЕ ПЕРСОНАЖЕЙ И ТЕГОВ В ЧАНКЕ:
Если пользователь явно просит добавить нового персонажа в кадр («покажи Ethan», «Victoria тоже в кадре», «cross-cutting на спикеров», «покажи говорящих» и т.п.) — ты МОЖЕШЬ изменить состав @ImageN тегов в чанке. Правила:
- Используй ТОЛЬКО @ImageN теги из EPISODE TAG MAPPING (он передан выше). Не выдумывай новых тегов и slug-ов.
- Если добавил/убрал персонажа — в выходном JSON для этого чанка верни ПОЛНЫЙ обновлённый `charactersInSegment` И `tagsUsed`.
- Если состав не меняется — НЕ возвращай эти поля, оставь только `promptEn`.

ЕСЛИ ПРАВКА ЯВНО КАСАЕТСЯ continuity:
- Сделай позы и расположение персонажей в начале чанка N+1 совпадающими с концом чанка N.
- Убедись что свет/атмосфера/наряд не «прыгают» между чанками.
- Описывай состояние внешнего вида явно в каждом чанке (наушники, кепка, что в руках) если это есть в outfit/blocking.

ВЫХОДНОЙ ФОРМАТ (СТРОГО ЭТОТ JSON, без markdown). Поля `charactersInSegment` и `tagsUsed` ОПЦИОНАЛЬНЫЕ — включай только если состав реально изменился:
{
  "chunks": [
    {
      "anchor": "<COPY EXACTLY from input — это ID чанка>",
      "promptEn": "<полный новый promptEn для этого чанка, 6 блоков, английский>",
      "charactersInSegment": [{"tag":"@Image1","name":"Victoria","slug":"victoria","variantId":"base"}],
      "tagsUsed": ["@Image1", "@Image3"]
    }
  ]
}

Никаких пояснений ДО или ПОСЛЕ JSON. Только JSON.
"""


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/revise-batch', methods=['POST'])
def seedance_revise_batch(sid, num):
    """ONE Claude call rewrites ALL batch_prompts of one episode according to a
    Russian instruction. Mirrors colleague's `reviseEpisodePrompts()`.

    Body: { instruction?: str }  — empty/missing falls back to per-user setting.
    Response: { updated: [{anchor, promptEn}], warnings: [...] }
    """
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    # Resolve the instruction: explicit body > per-user setting > default constant.
    instr = (body.get('instruction') or '').strip()
    if not instr:
        try:
            email = current_user_email() or ''
            us = _load_user_settings(email) if email else {}
            instr = (us.get('auto_revise_instruction') or '').strip()
        except Exception:
            instr = ''
    if not instr:
        instr = DEFAULT_AUTO_REVISE_INSTRUCTION
    batch = ep.get('batch_prompts') or {}
    if not batch:
        return jsonify({'error': 'no batch_prompts — сначала запусти batch-compose'}), 400
    tag_mapping = ep.get('batch_tag_mapping') or []
    episode_blocking = (ep.get('batch_episode_blocking') or ep.get('scene_blocking') or '').strip()
    valid_tags = {t['tag'] for t in tag_mapping if t.get('tag')}

    # Snapshot original promptEns so we can build the LLM input AND keep a
    # backup on disk (allows per-chunk «↺ Оригинал» rollback later if needed).
    source = []
    for anchor, entry in batch.items():
        prompt_en = (entry.get('prompt') or '').strip()
        if not prompt_en:
            continue
        source.append({
            'anchor': anchor,
            'promptEn': prompt_en,
            'plan': entry.get('plan') or {},
            'tagsUsed': entry.get('tagsUsed') or [],
            'sceneIdx': entry.get('sceneIdx'),
            'segIdx': entry.get('segIdx'),
        })
    if not source:
        return jsonify({'error': 'no usable promptEns in batch_prompts'}), 400

    # Build the AI input. Pack each chunk in its own delimited block — anchor +
    # plan summary + tagsUsed + the full promptEn — so Claude can edit in place.
    blocks = []
    for p in source:
        blocks.append('\n────── CHUNK ' + str(p['anchor']) + ' ──────')
        plan = p['plan']
        chars = plan.get('charactersInSegment') or []
        blocks.append('// duration: ' + str(plan.get('durationSec', '')))
        blocks.append('// charactersInSegment: ' + json.dumps(chars, ensure_ascii=False))
        blocks.append('// tagsUsed: ' + json.dumps(p['tagsUsed'], ensure_ascii=False))
        blocks.append(p['promptEn'])

    ai_input = '\n'.join([
        _SD_REVISE_BATCH_RULES.strip(),
        '',
        '═══════════ EPISODE TAG MAPPING ═══════════',
        '(все @ImageN которые можно использовать в этой серии — refs уже загружены под этими тегами)',
        json.dumps(tag_mapping, ensure_ascii=False, indent=2),
        '',
        '═══════════ EPISODE BLOCKING ═══════════',
        episode_blocking or '(не задан)',
        '',
        '═══════════ ОРИГИНАЛЬНЫЕ ' + str(len(source)) + ' MAIN-ПРОМПТА ═══════════',
        *blocks,
        '',
        '═══════════ ПРАВКА ОТ ПОЛЬЗОВАТЕЛЯ ═══════════',
        instr,
        '',
        'Верни JSON с переписанными ' + str(len(source)) + ' промптами. Используй ТОЛЬКО теги из EPISODE TAG MAPPING выше.',
    ])

    try:
        raw = claude_ask(ai_input, system='You are a JSON-only response API. Output strict JSON, no markdown fences, no commentary.')
        data = json.loads(strip_json(raw))
    except Exception as e:
        return jsonify({'error': f'AI не вернул валидный JSON: {e}'}), 500
    chunks_out = (data or {}).get('chunks') or []
    if not isinstance(chunks_out, list) or not chunks_out:
        return jsonify({'error': 'AI вернул пустой chunks[]'}), 500

    by_anchor = {}
    for c in chunks_out:
        a = c.get('anchor')
        pe = (c.get('promptEn') or '').strip()
        if not isinstance(a, str) or not pe:
            continue
        # Strip a stray markdown fence if Claude wrapped the value.
        m = re.match(r'^```(?:[a-z]*)?\s*\n([\s\S]*?)\n```\s*$', pe)
        if m:
            pe = m.group(1).strip()
        tags_used = c.get('tagsUsed')
        if isinstance(tags_used, list):
            tags_used = [t for t in tags_used if isinstance(t, str) and t in valid_tags]
        else:
            tags_used = None
        chars_in_seg = c.get('charactersInSegment')
        if isinstance(chars_in_seg, list):
            chars_in_seg = [x for x in chars_in_seg if isinstance(x, dict) and x.get('tag') in valid_tags]
        else:
            chars_in_seg = None
        by_anchor[a] = {'promptEn': pe, 'tagsUsed': tags_used, 'charactersInSegment': chars_in_seg}

    updated = []
    warnings = []
    with _episode_lock(sid, num):
        ep_fresh = load_episode(sid, num) or ep
        batch_fresh = ep_fresh.get('batch_prompts') or {}
        for anchor, new in by_anchor.items():
            entry = batch_fresh.get(anchor)
            if not entry:
                warnings.append({'code': 'unknown-anchor', 'anchor': anchor, 'message': 'AI вернул чанк которого нет в batch_prompts'})
                continue
            # Inject blocking and ban-list pass on the rewritten promptEn —
            # mirrors compose-time post-processing so revise output is
            # immediately ready for Seedance without an extra round.
            tags_for_inject = new.get('tagsUsed') or entry.get('tagsUsed') or []
            with_block = _seedance_inject_blocking(new['promptEn'], episode_blocking, tags_for_inject)
            finalized, applied = _seedance_apply_banlist(with_block)
            # Preserve a one-shot backup of the pre-revise text so a future
            # «↺ Оригинал» UI button can roll a single chunk back.
            if not entry.get('prompt_original'):
                entry['prompt_original'] = entry.get('prompt') or ''
            entry['prompt'] = finalized
            if new.get('tagsUsed'):
                entry['tagsUsed'] = new['tagsUsed']
            if new.get('charactersInSegment'):
                entry.setdefault('plan', {})['charactersInSegment'] = new['charactersInSegment']
            if applied:
                entry['appliedReplacements'] = applied
            batch_fresh[anchor] = entry
            updated.append({'anchor': anchor, 'promptEn': finalized})
        ep_fresh['batch_prompts'] = batch_fresh
        ep_fresh['batch_revised_at'] = datetime.datetime.utcnow().isoformat()
        ep_fresh['batch_revised_instruction'] = instr
        save_episode(sid, num, ep_fresh)

    return jsonify({
        'ok': True,
        'updated': updated,
        'count': len(updated),
        'expected': len(source),
        'warnings': warnings,
        'instruction_used': instr,
    })


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/batch-compose', methods=['POST'])
def seedance_batch_compose(sid, num):
    """Reference-style episode plan builder. ONE Claude call produces a strict
    JSON with `episodeBlocking` + per-segment `plan{}` + `prompt.promptEn`.
    Server then runs the same post-processing pipeline as the Shadow Founder
    reference: filtered scene-blocking inject → hard-cuts inject → banlist
    replacements → 4 validators (autonomy / durations / dialogue coverage /
    wardrobe). Final `prompt` field is the ready-for-Seedance English text.

    Body: {
      segments: [{anchor, sceneIdx, segIdx, text, has_close_up, durationSec?}],
      base_outfits_only: bool,
      style: str,
    }
    """
    import hashlib
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    segments = body.get('segments') or []
    if not segments:
        return jsonify({'error': 'no segments'}), 400
    base_outfits_only = bool(body.get('base_outfits_only', False))
    style_override = (body.get('style') or '').strip() or (s.get('visual_style') or '').strip()

    script = (ep.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script empty'}), 400

    # ─── SAFETY NET: re-sync [BLOCKING] outfits before composing. If for
    # any reason the script was saved through a path that skipped sync
    # (legacy import, direct edit, history restore from before sync
    # existed), missing outfit objects get created here so Claude sees
    # them in `outfit_options` and can pick the right variantId. The
    # corresponding images may not be ready THIS run if just created —
    # the spawned autogen worker handles them in the background.
    try:
        _sync_script_outfits(sid, script)
        # reload after sync — character.outfits may have grown
        s = load_series(sid)
    except Exception as _e:
        _log_event('WARN', 'pre_compose_outfit_sync_failed',
                   sid=sid, ep=num, err=str(_e)[:200])

    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    tag_mapping = _build_episode_tag_mapping(script, active_chars, active_locs)
    valid_tags = {t['tag'] for t in tag_mapping}
    tag_for = {(t['kind'], t['id']): t['tag'] for t in tag_mapping}

    # Build descriptors per tag — fed to Claude with canonical wardrobe
    tag_descriptors = []
    wardrobe_by_tag = {}
    for t in tag_mapping:
        if t['kind'] == 'char':
            ch = next((c for c in active_chars if c['id'] == t['id']), None)
            if not ch: continue
            base_outfit = next((o for o in (ch.get('outfits') or []) if o.get('is_base')), None)
            if not base_outfit and (ch.get('outfits') or []):
                base_outfit = ch['outfits'][0]
            base_label = (base_outfit or {}).get('label')
            canonical = _canonical_char_description(s, ch['id'], base_label)
            # outfit_options as objects with label+description so Claude can
            # match scene context semantically, not just by label string.
            outfit_options_full = [
                {'label': o.get('label'), 'description': (o.get('description') or '')[:120], 'is_base': bool(o.get('is_base'))}
                for o in (ch.get('outfits') or [])
                if o.get('avai_url') and o.get('label')
            ]
            # Wardrobe validation pool = base + all variant descriptions so
            # the validator doesn't false-flag clothing from a chosen variant.
            all_outfit_descs = ' '.join(
                (o.get('description') or '')
                for o in (ch.get('outfits') or [])
                if o.get('avai_url')
            )
            wardrobe_by_tag[t['tag']] = canonical + (' ' + all_outfit_descs if all_outfit_descs.strip() else '')
            tag_descriptors.append({
                'tag': t['tag'],
                'kind': 'character',
                'slug': ch['id'],
                'name': ch['name'],
                'gender': ch.get('gender', ''),
                'appearance': (ch.get('appearance') or '')[:300],
                'wardrobe': canonical,                  # base outfit description
                'outfit_options': outfit_options_full,  # variants with descriptions
            })
        else:
            lc = next((x for x in active_locs if x['id'] == t['id']), None)
            if not lc: continue
            tag_descriptors.append({
                'tag': t['tag'],
                'kind': 'location',
                'slug': lc['id'],
                'name': lc['name'],
                'description': (lc.get('description') or '')[:300],
            })

    scene_blocking_input = (ep.get('scene_blocking') or '').strip()
    prev_context = _prev_episode_ending_context(sid, num)

    # Segments specs for Claude — anchor + actual computed durationSec
    seg_specs = []
    for seg in segments:
        seg_specs.append({
            'anchor': (seg.get('anchor') or '')[:60],
            'sceneIdx': seg.get('sceneIdx', 0),
            'segIdx': seg.get('segIdx', 0),
            'has_close_up': bool(seg.get('has_close_up')),
            'durationSec': int(seg.get('durationSec') or 15),
            'establishing_shot': bool(seg.get('establishing_shot')),
            'text': (seg.get('text') or '')[:1500],
        })

    # System prompt — reference EPISODE_PLAN_RULES adapted to our case (variable
    # segments instead of fixed 4+1+1; English promptEn output; 6-block strict).
    sysprompt = _SD_BATCH_RULES

    style_block = ''
    if style_override:
        style_block = (
            f"\n=== STYLE OVERRIDE ===\n"
            f"Final visual style: «{style_override}». Inject this style into "
            f"every segment's Style block, plus subtle markers in Subject and "
            f"Scene blocks. Keep dialogue verbatim from the script.\n"
        )
    base_only_block = ''
    if base_outfits_only:
        base_only_block = (
            "\n=== BASE OUTFITS ONLY — STRICT ===\n"
            "For EVERY char-ref across ALL segments set \"outfit\": null. "
            "Do not pick alternative outfit variants — use base portraits only.\n"
        )
    blocking_block = ''
    if scene_blocking_input:
        blocking_block = (
            f"\n=== SCENE BLOCKING — USER-PROVIDED (do not rewrite) ===\n"
            f"```\n{scene_blocking_input}\n```\n"
            f"Copy this VERBATIM as `episodeBlocking` in your response. The "
            f"server will then filter and inject it into each segment's Constraints.\n"
        )

    prev_block = ''
    if prev_context:
        prev_block = (
            f"\n=== PREV EPISODE CONTEXT — for cross-episode continuity ===\n"
            f"```\n{prev_context}\n```\n"
            f"CRITICAL: if the FIRST segment of THIS episode picks up from the same scene "
            f"as PREV EP last lines (same location, no time-jump, no new scene heading) — "
            f"INHERIT the spatial positioning from PREV EP's episodeBlocking and ending state. "
            f"Adrian was at the door at end of PREV EP → he's still at the door at start of THIS one. "
            f"Clara was at the desk → she's still there. Don't reset positions on a continuous scene.\n"
            f"If THIS episode's script clearly opens a new scene (different location heading, "
            f"explicit time-jump like 'утром' / 'next day' / 'через час'), ignore PREV EP context "
            f"and start fresh.\n"
            f"=== END PREV EPISODE CONTEXT ===\n"
        )

    userprompt = (
        f"EPISODE TAG MAPPING (deterministic — do not invent new tags):\n"
        f"{json.dumps(tag_descriptors, ensure_ascii=False, indent=2)}\n\n"
        f"FULL EPISODE SCRIPT:\n```\n{script[:10000]}\n```\n\n"
        f"SEGMENTS (N={len(seg_specs)}, in script-position order):\n"
        f"{json.dumps(seg_specs, ensure_ascii=False, indent=2)}\n\n"
        f"{prev_block}{blocking_block}{style_block}{base_only_block}\n"
        f"=== HARD COUNT REQUIREMENT ===\n"
        f"Return EXACTLY {len(seg_specs)} elements in segments[]. Not {len(seg_specs)-1}, "
        f"not {len(seg_specs)+1}. Each input anchor maps to one output segment with the "
        f"same `anchor` string copied verbatim (it's the ID).\n"
        f"Do NOT merge, skip, or duplicate segments. One input → one output.\n"
        f"Before submitting JSON, count segments[] — it MUST be exactly {len(seg_specs)}.\n\n"
        f"Return ONLY JSON (no markdown fences, no preamble)."
    )

    try:
        # 32k max_tokens — full episode batch JSON can exceed default 8k easily
        raw = claude_ask(userprompt, system=sysprompt, max_tokens=32000)
        cleaned = strip_json(raw)
        # Detect truncation: a valid JSON top-level object/array must end with } or ]
        looks_truncated = bool(cleaned) and cleaned[-1] not in '}]'
        if looks_truncated:
            print(f'[batch-compose] response looks truncated (ends with {cleaned[-50:]!r}); raising max_tokens and retrying', flush=True)
            raw = claude_ask(userprompt, system=sysprompt, max_tokens=64000)
            cleaned = strip_json(raw)
        try:
            data = loads_lenient(cleaned)
        except Exception as parse_err:
            # Last-resort: ask Claude to repair the JSON it just emitted
            print(f'[batch-compose] initial JSON parse failed ({parse_err}); asking Claude to repair', flush=True)
            repair_prompt = (
                "The following text is supposed to be valid JSON but has a syntax error. "
                "Return ONLY the corrected JSON — no commentary, no markdown fences, no explanation. "
                "Preserve all content verbatim; fix ONLY syntax (trailing commas, unquoted keys, "
                "unescaped quotes inside strings, missing closing braces if truncated, etc.).\n\n"
                f"```\n{cleaned}\n```"
            )
            repaired = claude_ask(repair_prompt, system="You are a JSON repair tool. Return only valid JSON.", max_tokens=64000)
            data = loads_lenient(strip_json(repaired))
    except Exception as e:
        return jsonify({'error': f'batch-compose failed: {e}'}), 500

    episode_blocking = (data.get('episodeBlocking') or scene_blocking_input or '').strip()
    out_segments = data.get('segments') or []
    by_anchor = {}
    for so in out_segments:
        a = (so.get('anchor') or '').strip()[:60]
        if a:
            by_anchor[a] = so

    # Pipeline per segment: get plan+promptEn → filter+inject blocking →
    # inject hard-cuts → banlist → resolve refs → store.
    final_prompts = {}
    unresolved_anchors = []
    segments_for_validation = []  # list of (spec, plan, finalized_promptEn)

    for spec in seg_specs:
        anchor = spec['anchor']
        out = by_anchor.get(anchor)
        if not out:
            unresolved_anchors.append(anchor)
            continue
        plan = out.get('plan') or {}
        prompt_obj = out.get('prompt') or {}
        prompt_en = (prompt_obj.get('promptEn') or '').strip()
        if not prompt_en:
            unresolved_anchors.append(anchor)
            continue

        # Defensive defaults
        plan.setdefault('charactersInSegment', [])
        plan.setdefault('actionTimeline', [])
        plan.setdefault('dialogues', [])
        plan.setdefault('constraintsExtra', [])
        plan.setdefault('riskFlags', [])
        plan.setdefault('durationSec', spec['durationSec'])

        # Tags Claude says are used — fall back to chars-in-segment tags
        tags_used = prompt_obj.get('tagsUsed') or []
        if not tags_used:
            tags_used = [c.get('tag') for c in plan.get('charactersInSegment', []) if c.get('tag')]
        tags_used = [t for t in tags_used if t in valid_tags]

        # Inject filtered blocking, then hard-cuts (multi-shot only), then banlist
        with_blocking = _seedance_inject_blocking(prompt_en, episode_blocking, tags_used)
        with_hardcuts = (_seedance_inject_hard_cuts(with_blocking)
                         if len(plan.get('actionTimeline') or []) > 1
                         else with_blocking)
        finalized, applied = _seedance_apply_banlist(with_hardcuts)

        # Build refs from plan.charactersInSegment (Claude already chose them)
        refs = []
        if base_outfits_only:
            for c in plan.get('charactersInSegment', []):
                if c.get('slug'):
                    refs.append({'kind': 'char', 'id': c['slug'], 'outfit': None})
        else:
            for c in plan.get('charactersInSegment', []):
                if c.get('slug'):
                    refs.append({
                        'kind': 'char',
                        'id': c['slug'],
                        'outfit': c.get('variantId') if c.get('variantId') and c.get('variantId') != 'base' else None,
                    })
        # Always add the location ref last if there's an active loc tag in mapping
        loc_tag = next((t for t in tag_mapping if t['kind'] == 'loc'), None)
        if loc_tag:
            refs.append({'kind': 'loc', 'id': loc_tag['id']})

        # CLOSE-UP filter (mirrors compose endpoint) — keep every SPEAKING
        # char (dialogue cue "NAME:"); a back-and-forth close-up needs both
        # faces for reverse shots. Drop only non-speaking bystanders. Falls
        # back to a single first-named subject on a silent action beat.
        is_close_up = bool(plan.get('close_up')) or spec.get('has_close_up') or bool(out.get('close_up'))
        if is_close_up:
            spec_text = spec['text']
            speaker_ids = set()
            for r in refs:
                if r.get('kind') != 'char': continue
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                nm = (ch.get('name') or '').strip() if ch else ''
                if not nm: continue
                if re.search(r'^\s*' + re.escape(nm) + r'\s*[:：(]', spec_text, re.MULTILINE | re.IGNORECASE):
                    speaker_ids.add(r.get('id'))
            if speaker_ids:
                keep_ids = speaker_ids
            else:
                chunk_lower = spec_text.lower()
                chars_in_chunk = []
                for r in refs:
                    if r.get('kind') != 'char': continue
                    ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                    if not ch: continue
                    pos = chunk_lower.find(ch['name'].lower())
                    if pos >= 0: chars_in_chunk.append((r.get('id'), pos))
                chars_in_chunk.sort(key=lambda x: x[1])
                first_id = chars_in_chunk[0][0] if chars_in_chunk else next(
                    (r.get('id') for r in refs if r.get('kind') == 'char'), None
                )
                keep_ids = {first_id} if first_id else set()
            refs = [r for r in refs if r.get('kind') != 'char' or r.get('id') in keep_ids]

        # De-dup char refs
        seen_char = set(); deduped = []
        for r in refs:
            if r.get('kind') == 'char':
                if r.get('id') in seen_char: continue
                seen_char.add(r.get('id'))
            deduped.append(r)
        refs = deduped

        # Resolve URLs (drop unresolvable)
        original_refs_for_remap = list(refs)
        resolved_refs = []
        ref_urls = []
        for r in refs:
            url = _resolve_ref_url(s, r, sid=sid)
            if not url: continue
            clean = {k: v for k, v in r.items() if not k.startswith('_')}
            ref_urls.append(url)
            resolved_refs.append({**clean, 'url': url})

        # If filtering changed refs[] (close-up / dedup / unresolved drops),
        # remap @ImageN tokens in finalized to match the new ref ordering. The
        # plan's @ImageN are positional per Claude's response — server's filter
        # may have shifted them. Keep prompt and refs in sync.
        _tag_remap = _build_image_tag_remap(original_refs_for_remap, resolved_refs)
        if any(v is None for v in _tag_remap.values()) or any(k != v for k, v in _tag_remap.items() if v is not None):
            finalized = _remap_image_tags(finalized, _tag_remap)

        final_prompts[anchor] = {
            'prompt': finalized,                                # ready-for-Seedance English promptEn
            'refs': resolved_refs,
            'ref_urls': ref_urls,
            'plan': plan,                                       # structured (for UI / debug)
            'tagsUsed': tags_used,
            'appliedReplacements': applied,
            'close_up': is_close_up,
            'shot_type': out.get('shot_type', ''),
            'sceneIdx': spec['sceneIdx'],
            'segIdx': spec['segIdx'],
        }
        segments_for_validation.append((spec, plan, finalized))

    # Per-anchor fallback for what Claude skipped → call /seedance/compose-style
    # mini AI for each missing anchor. Always converges to 100% anchor coverage.
    fallback_succeeded = []
    fallback_failed = []
    if unresolved_anchors:
        print(f'[batch-compose] Claude skipped {len(unresolved_anchors)} anchors — running per-chunk fallback', flush=True)
        for missed_anchor in unresolved_anchors:
            spec = next((s for s in seg_specs if s['anchor'] == missed_anchor), None)
            if not spec:
                continue
            try:
                mini_sys = (
                    "You are a Seedance 2.0 shot composer. The user gives ONE script segment. "
                    "Return strict JSON with the same plan{} + prompt.promptEn structure as the "
                    "main batch (English 6-block: Location / Characters in this segment / Action timeline / "
                    "Lighting and atmosphere / Style / Constraints). NO 'same/still/as before' refs.\n\n"
                    "Output: {\"plan\":{...},\"prompt\":{\"promptEn\":\"...\",\"tagsUsed\":[...]}}"
                )
                mini_user = (
                    f"TAG MAPPING:\n{json.dumps(tag_descriptors, ensure_ascii=False)}\n\n"
                    f"SCENE BLOCKING:\n{episode_blocking[:600] if episode_blocking else '(none)'}\n\n"
                    f"SEGMENT TEXT:\n```\n{spec['text']}\n```\n\n"
                    f"durationSec={spec['durationSec']}, has_close_up={spec['has_close_up']}\n"
                    f"Return JSON for this single segment."
                )
                mini_raw = claude_ask(mini_user, system=mini_sys)
                mini_data = json.loads(strip_json(mini_raw))
                m_plan = mini_data.get('plan') or {}
                m_prompt = mini_data.get('prompt') or {}
                m_promptEn = (m_prompt.get('promptEn') or '').strip()
                if not m_promptEn:
                    fallback_failed.append(missed_anchor); continue

                m_plan.setdefault('charactersInSegment', [])
                m_plan.setdefault('actionTimeline', [])
                m_plan.setdefault('dialogues', [])
                m_plan.setdefault('durationSec', spec['durationSec'])
                m_tags = m_prompt.get('tagsUsed') or [c.get('tag') for c in m_plan['charactersInSegment'] if c.get('tag')]
                m_tags = [t for t in m_tags if t in valid_tags]

                m_with_blocking = _seedance_inject_blocking(m_promptEn, episode_blocking, m_tags)
                m_with_hardcuts = (_seedance_inject_hard_cuts(m_with_blocking)
                                   if len(m_plan['actionTimeline']) > 1
                                   else m_with_blocking)
                m_finalized, m_applied = _seedance_apply_banlist(m_with_hardcuts)

                m_refs = []
                if base_outfits_only:
                    for c in m_plan['charactersInSegment']:
                        if c.get('slug'):
                            m_refs.append({'kind': 'char', 'id': c['slug'], 'outfit': None})
                else:
                    for c in m_plan['charactersInSegment']:
                        if c.get('slug'):
                            m_refs.append({'kind': 'char', 'id': c['slug'],
                                           'outfit': c.get('variantId') if c.get('variantId') and c.get('variantId') != 'base' else None})
                if loc_tag:
                    m_refs.append({'kind': 'loc', 'id': loc_tag['id']})

                m_orig_for_remap = list(m_refs)
                m_resolved = []; m_urls = []
                for r in m_refs:
                    url = _resolve_ref_url(s, r, sid=sid)
                    if not url: continue
                    clean = {k: v for k, v in r.items() if not k.startswith('_')}
                    m_urls.append(url); m_resolved.append({**clean, 'url': url})
                m_remap = _build_image_tag_remap(m_orig_for_remap, m_resolved)
                if any(v is None for v in m_remap.values()) or any(k != v for k, v in m_remap.items() if v is not None):
                    m_finalized = _remap_image_tags(m_finalized, m_remap)

                final_prompts[missed_anchor] = {
                    'prompt': m_finalized,
                    'refs': m_resolved,
                    'ref_urls': m_urls,
                    'plan': m_plan,
                    'tagsUsed': m_tags,
                    'appliedReplacements': m_applied,
                    'close_up': bool(m_plan.get('close_up')) or spec['has_close_up'],
                    'shot_type': '',
                    'sceneIdx': spec['sceneIdx'],
                    'segIdx': spec['segIdx'],
                    '_via_fallback': True,
                }
                segments_for_validation.append((spec, m_plan, m_finalized))
                fallback_succeeded.append(missed_anchor)
            except Exception as e:
                print(f'[batch-compose] fallback failed for {missed_anchor[:30]}: {e}', flush=True)
                fallback_failed.append(missed_anchor)

    # Run all 4 reference validators
    warnings = []
    warnings.extend(_seedance_validate_autonomy(segments_for_validation))
    warnings.extend(_seedance_validate_durations(segments_for_validation))
    warnings.extend(_seedance_validate_wardrobe(segments_for_validation, wardrobe_by_tag))
    # Dialogue coverage — check that every Name:dialogue line in script is
    # represented in some segment's plan.dialogues
    script_dialogues = re.findall(r'^[A-Za-zА-Яа-яЁё][^:\n]{0,30}:\s*(.+)$', script, re.MULTILINE)
    expected_dialogues = len(script_dialogues)
    covered = 0
    for spec, plan, _pe in segments_for_validation:
        covered += len(plan.get('dialogues') or [])
    if expected_dialogues > 0 and covered < int(expected_dialogues * 0.8):
        warnings.append({
            'code': 'dialogues-missing',
            'message': f'Покрыто диалогов: {covered}, в скрипте: {expected_dialogues} (<80%). Возможны пропуски реплик.',
            'anchor': '',
        })

    if warnings:
        print(f'[batch-compose] {len(warnings)} validator warnings:', flush=True)
        for w in warnings[:8]:
            print(f'  [{w["code"]}] {w["message"]}', flush=True)

    # Persist (locked to avoid clobbering parallel /start mutations)
    with _episode_lock(sid, num):
        ep_fresh = load_episode(sid, num) or ep
        ep_fresh['batch_prompts'] = final_prompts
        ep_fresh['batch_episode_blocking'] = episode_blocking
        ep_fresh['batch_script_hash'] = hashlib.sha256(script.encode('utf-8')).hexdigest()[:16]
        ep_fresh['batch_built_at'] = datetime.datetime.utcnow().isoformat()
        ep_fresh['batch_warnings'] = warnings
        ep_fresh['batch_tag_mapping'] = tag_mapping
        if episode_blocking and not ep_fresh.get('scene_blocking'):
            ep_fresh['scene_blocking'] = episode_blocking
        save_episode(sid, num, ep_fresh)
        ep = ep_fresh

    return jsonify({
        'ok': True,
        'count': len(final_prompts),
        'requested_count': len(seg_specs),
        'unresolved_anchors': fallback_failed,
        'fallback_filled': fallback_succeeded,
        'claude_skipped': unresolved_anchors,
        'episode_blocking': episode_blocking,
        'tag_mapping': tag_mapping,
        'warnings': warnings,
        'script_hash': ep['batch_script_hash'],
    })
