"""Reference-style validators, Seedance banlist, blocking-tag injection,
wardrobe/hair phrase rules."""
import re

from sw.scriptparse import is_scene_heading
from sw.storage import _normalize_blocking_tags, load_episode

# ─── Reference-style validators / banlist / blocking-injection ─────────────
# Ported from /tmp/shadow-founder/services/chunk-builder.ts. Goal: each segment's
# final promptEn matches the reference's clean 6-block English structure with
# spatially-consistent `episodeBlocking` injected, banlist replacements applied
# for moderation, and validation warnings caught programmatically.

# Replacements that catch Seedance moderation triggers without losing narrative
# meaning. Originally 21 ported in lock-step with reference
# (chunk-builder.ts:199-221); since extended with the violence verbs the
# reference omitted (slaughter/massacre/behead/strangle/… — the ep22 "slaughter"
# leak, Jun 2026) so the visual-prompt sanitizer matches the dialogue detector's
# trigger lexicon (_MOD_TRIGGER_GROUPS).
_SD_BANLIST = [
    (re.compile(r'\b(shoots?|fires?|shooting|firing)\b', re.IGNORECASE), 'muzzle flash illuminates'),
    (re.compile(r'\b(guns?|pistols?|rifles?|weapons?)\b', re.IGNORECASE), 'tactical equipment'),
    (re.compile(r'\b(stabs?|slashes?|stabbing|slashing)\b', re.IGNORECASE), 'sharp impact'),
    (re.compile(r'\b(knife|knives|blades?)\b', re.IGNORECASE), 'metallic object'),
    (re.compile(r'\b(attacks?|attacking|attacked)\b', re.IGNORECASE), 'closes distance'),
    (re.compile(r'\b(fights?|fighting|fought|punch(?:es|ed|ing)?|kick(?:s|ed|ing)?|strikes?|hits?|beats?)\b', re.IGNORECASE), 'impact'),
    (re.compile(r'\b(kills?|killing|killed|murders?|murdered)\b', re.IGNORECASE), 'falls still'),
    (re.compile(r'\b(slaughters?|slaughtered|slaughtering|massacres?|massacred|massacring|butchers?|butchered|beheads?|beheaded|slays?|slain|assassinates?|assassinated)\b', re.IGNORECASE), 'overpowers'),
    (re.compile(r'\b(strangles?|strangled|strangling|chokes?|choked|choking|drowns?|drowned|drowning|tortures?|tortured|torturing)\b', re.IGNORECASE), 'overpowers'),
    (re.compile(r'\b(dies?|dying|dead|death)\b', re.IGNORECASE), 'final moment'),
    (re.compile(r'\b(corpse|corpses|dead body|dead bodies)\b', re.IGNORECASE), 'motionless figure on the ground'),
    (re.compile(r'\b(blood|bleeds?|bleeding|bled)\b', re.IGNORECASE), 'crimson liquid'),
    (re.compile(r'\b(wounds?|wounded|injuries|injured|hurt)\b', re.IGNORECASE), 'surface damage'),
    (re.compile(r'\b(explodes?|exploded|exploding|explosions?)\b', re.IGNORECASE), 'rapid expansion'),
    (re.compile(r'\b(destroys?|destroyed|destroying|smashes?|smashed|smashing)\b', re.IGNORECASE), 'structural failure'),
    (re.compile(r'\b(crashes?|crashed|crashing)\b', re.IGNORECASE), 'sudden impact'),
    (re.compile(r'\bscreams?\b', re.IGNORECASE), 'open mouth'),
    (re.compile(r'\b(cries in pain|cried in pain|crying in pain)\b', re.IGNORECASE), 'contorted expression'),
    (re.compile(r'\b(child|kid|boy|girl|teen|teenager|baby|infant|minor|schoolgirl|schoolboy)\b', re.IGNORECASE), 'young person'),
    (re.compile(r'\b(violent|violence|brutal|graphic|gore|horror|torture|abuse|terrorist)\b', re.IGNORECASE), 'dramatic confrontation'),
    (re.compile(r'\b(victim|suicide)\b', re.IGNORECASE), 'figure in distress'),
    (re.compile(r'\b(nude|naked|sexy|seductive|erotic|intimate|undressed|lingerie)\b', re.IGNORECASE), 'composed appearance'),
    (re.compile(r'\b(passionate kisses?|passionate kissing)\b', re.IGNORECASE), 'close embrace'),
]

def _seedance_apply_banlist(text):
    """Run all 21 banlist replacements on the prompt text. Returns
    (replaced_text, list_of_applied_matches). Mirrors reference applyBanlist."""
    if not text:
        return text or '', []
    applied = []
    out = text
    for pattern, replacement in _SD_BANLIST:
        def _repl(m, _r=replacement):
            applied.append({'original': m.group(0), 'replaced': _r})
            return _r
        out = pattern.sub(_repl, out)
    return out, applied


# Forbidden cross-chunk references — kill autonomy. Mirrors reference FORBIDDEN_REFS.
_SD_FORBIDDEN_AUTONOMY_RE = [
    re.compile(r'\bsame\b', re.IGNORECASE),
    re.compile(r'\bstill\b', re.IGNORECASE),
    re.compile(r'\bas before\b', re.IGNORECASE),
    re.compile(r'\bas previous\b', re.IGNORECASE),
    re.compile(r'\bcontinues\b', re.IGNORECASE),
    re.compile(r'\bпрежний\b', re.IGNORECASE),
    re.compile(r'\bтот же\b', re.IGNORECASE),
]

def _seedance_validate_autonomy(segments_data):
    """Each segment's promptEn must NOT reference prior chunks ("same/still/as
    before/continues") — Seedance has no memory across chunks, so any reference
    to "earlier" content causes drift. Returns list of {code, message, anchor}."""
    warnings = []
    for spec, plan, prompt_text in segments_data:
        if not prompt_text:
            continue
        for rx in _SD_FORBIDDEN_AUTONOMY_RE:
            m = rx.search(prompt_text)
            if m:
                warnings.append({
                    'code': 'autonomy-violation',
                    'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}: запрещённое слово "{m.group(0)}" в promptEn — нарушает автономность.',
                    'anchor': spec['anchor'],
                })
    return warnings


def _seedance_validate_durations(segments_data):
    """Sum of shot lengths in actionTimeline must equal durationSec. Also flag
    monotone rhythm (all shots equal length)."""
    warnings = []
    for spec, plan, prompt_text in segments_data:
        if not isinstance(plan, dict):
            continue
        timeline = plan.get('actionTimeline') or []
        duration = plan.get('durationSec') or 0
        if not timeline or not duration:
            continue
        try:
            sum_shots = sum(int(s.get('toSec', 0)) - int(s.get('fromSec', 0)) for s in timeline)
        except Exception:
            sum_shots = -1
        if sum_shots != duration:
            warnings.append({
                'code': 'shot-sum-mismatch',
                'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}: сумма шотов {sum_shots}с ≠ durationSec={duration}с',
                'anchor': spec['anchor'],
            })
        # Monotone check — only when ≥2 shots
        if len(timeline) >= 2:
            try:
                lens = [int(s.get('toSec', 0)) - int(s.get('fromSec', 0)) for s in timeline]
                if all(l == lens[0] for l in lens):
                    warnings.append({
                        'code': 'monotone-rhythm',
                        'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}: все шоты {lens[0]}с — однообразная нарезка.',
                        'anchor': spec['anchor'],
                    })
            except Exception:
                pass
    return warnings


# Wardrobe-fidelity guard: items often hallucinated by AI even when not in
# canonical char description. If a word from this list appears in actionTimeline
# but is NOT in the character's wardrobe text → warning.
_SD_WARDROBE_WORDS = [
    'jacket', 'blazer', 'coat', 'overcoat', 'tie', 'scarf', 'hat', 'cap',
    'beanie', 'gloves', 'boots', 'sunglasses', 'glasses', 'watch', 'belt',
    'vest', 'hoodie', 'sweater'
]

def _seedance_validate_wardrobe(segments_data, wardrobe_by_tag):
    """If a segment's actionTimeline mentions a wardrobe word that's not in any
    of the segment's characters' canonical wardrobe descriptions — warn."""
    warnings = []
    for spec, plan, prompt_text in segments_data:
        if not isinstance(plan, dict):
            continue
        chars_in_scene = plan.get('charactersInSegment') or []
        tags_in_scene = [c.get('tag') for c in chars_in_scene if c.get('tag')]
        allowed_text = ' '.join(
            (wardrobe_by_tag.get(tag) or '').lower()
            for tag in tags_in_scene
        )
        timeline = plan.get('actionTimeline') or []
        for i, shot in enumerate(timeline):
            desc = (shot.get('description') or '').lower()
            for word in _SD_WARDROBE_WORDS:
                if re.search(r'\b' + re.escape(word) + r'\b', desc) and not re.search(r'\b' + re.escape(word) + r'\b', allowed_text):
                    warnings.append({
                        'code': 'wardrobe-mismatch',
                        'message': f'Сегмент sc{spec["sceneIdx"]}.seg{spec["segIdx"]}, шот {i+1}: упомянут "{word}", но ни у одного персонажа в сцене его нет в wardrobe.',
                        'anchor': spec['anchor'],
                    })
    return warnings


def _seedance_filter_blocking_for_chunk(blocking_text, tags_in_chunk):
    """Drop sentences in blocking that mention @ImageN tags NOT used by this
    chunk — otherwise blocking text leaks descriptions of off-frame chars into
    the chunk's prompt and Seedance tries to render them.
    Mirrors reference filterBlockingForChunk."""
    if not blocking_text:
        return ''
    used_set = set(tags_in_chunk or [])
    parts = re.split(r'(?<=\.)\s+', blocking_text)
    kept = []
    for p in parts:
        matches = re.findall(r'@[Ii]mage(\d+)', p)
        if not matches:
            kept.append(p.strip())
            continue
        all_tags_in_part = {f'@Image{n}' for n in matches}
        if all_tags_in_part.issubset(used_set):
            kept.append(p.strip())
    return ' '.join(kept).strip()


def _extract_script_blocking(script_text, chunk_text=None):
    """Pull every [BLOCKING] / [BLOCKING_OUT] block out of the raw script.
    Returns (episode_blocks, scene_blocks_for_chunk) where:
      - episode_blocks: all [BLOCKING] content that appears BEFORE the first
        scene heading (these are episode-wide constants like base outfits).
      - scene_blocks_for_chunk: concatenated content of every [BLOCKING] /
        [BLOCKING_OUT] block in the SAME scene as `chunk_text`. If chunk_text
        is None we return ALL blocking, joined.
    Lines inside the fences are 0-chrono in the segmenter; this function just
    reads them as scene-setup context for the Seedance prompt builder."""
    if not script_text:
        return '', ''
    lines = script_text.split('\n')
    # Walk script and bucket blocks by scene index. Scene index increments at
    # every scene heading; index 0 means "before any scene heading" = episode-level.
    blocks = []   # list of {kind: 'in'|'out', scene_idx: int, text: str}
    scene_starts = []  # byte-offset of each scene heading start
    cur_scene_idx = 0
    in_block = False
    block_kind = ''
    buf = []
    off = 0
    # Permissive fence detector — accepts every common variant LLM-writers emit:
    # [BLOCKING], [BLOCKING_START], [BLOCKING_BEGIN], [BLOCKING_OPEN], [/BLOCKING],
    # [BLOCKING_END], [BLOCKING_CLOSE], plus _OUT variants for closing setups.
    fence_re = re.compile(
        r'^\[/?\s*BLOCKING(?:_OUT)?(?:_(?:START|BEGIN|OPEN|END|CLOSE))?\s*\]\s*$',
        re.IGNORECASE,
    )
    # Implicit-blocking signatures — for setups the writer-LLM emitted WITHOUT
    # fence markers. Same heuristic as the client-side parser.
    outfit_line_re = re.compile(r'::\s*(?:OUTFIT|WEARING|WEAR|CLOTHES|COSTUME)\s*[:：]', re.IGNORECASE)
    blocking_meta_re = re.compile(
        r'^(?:LOCATION|MOOD|LIGHTING|PROPS|CAMERA|FRAMING|SETTING|TIME|WEATHER|ATMOSPHERE|ОСВЕЩЕНИЕ|РЕКВИЗИТ|ЛОКАЦИЯ|АТМОСФЕРА)\s*[:：]\s*\S',
        re.IGNORECASE,
    )
    position_verb_re = re.compile(
        r'^(?:стои[тю]|стоят|сиди[тю]|сидят|лежи[тшю]|лежат|держи[тшю]|держат|смотри[тшю]|смотрят|одет[аоы]?|оперевш\w*|прислон\w*|сжима\w*|стиска\w*|наблюда\w*|замер\w*|опуст\w*|поднят\w*|опущен\w*|облокот\w*|прижим\w*|нависа\w*|нагиба\w*|склон\w*|присел\w*|развалил\w*|wears?|stands?|sits?|lies?|holds?|looks?\s+at|watches?|leans?|grips?|clenches?|presses?|tilts?|rests?|stays?|crouches?|kneels?|squats?|positions?)(?=[\s,.;:!?]|$)',
        re.IGNORECASE,
    )
    name_cue_re = re.compile(r'^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 \-\'.#]{0,40}\s*[:：]\s*(.+)$')
    def is_position_line(t):
        m = name_cue_re.match(t)
        return bool(m and position_verb_re.match(m.group(1).strip()))
    def is_blocking_content(t):
        return bool(outfit_line_re.search(t)) or is_position_line(t) or bool(blocking_meta_re.match(t))
    # Pre-pass: mark indices that belong to implicit-blocking clusters.
    implicit_blocking_idx = set()
    for i, ln in enumerate(lines):
        t = ln.strip()
        # Primary trigger: outfit-line OR position-NAME line. A lone LOCATION:
        # is not enough — it could still be a real (inferred) scene heading.
        if not outfit_line_re.search(t) and not is_position_line(t):
            continue
        implicit_blocking_idx.add(i)
        for j in range(i - 1, -1, -1):
            tj = lines[j].strip()
            if not tj:
                continue
            if is_blocking_content(tj):
                implicit_blocking_idx.add(j); continue
            break
        for j in range(i + 1, len(lines)):
            tj = lines[j].strip()
            if not tj:
                continue
            if is_blocking_content(tj):
                implicit_blocking_idx.add(j); continue
            break

    implicit_buf = []   # accumulator for current implicit-blocking run
    def _flush_implicit():
        nonlocal implicit_buf
        if implicit_buf:
            blocks.append({'kind': 'in', 'scene_idx': cur_scene_idx, 'text': '\n'.join(implicit_buf)})
            implicit_buf = []

    for i, ln in enumerate(lines):
        line_off = off
        off += len(ln) + 1   # +1 for newline
        t = ln.strip()
        if fence_re.match(t):
            _flush_implicit()
            is_close = t.lstrip().startswith('[/') or bool(re.search(r'_(?:END|CLOSE)\s*\]', t, re.IGNORECASE))
            is_out = bool(re.search(r'BLOCKING_OUT', t, re.IGNORECASE))
            if is_close:
                if in_block and buf:
                    blocks.append({'kind': block_kind, 'scene_idx': cur_scene_idx, 'text': '\n'.join(buf)})
                in_block = False
                block_kind = ''
                buf = []
            else:
                in_block = True
                block_kind = 'out' if is_out else 'in'
                buf = []
            continue
        if in_block:
            if t:
                buf.append(ln)
            continue
        # Implicit blocking — accumulate without creating a scene from LOCATION:.
        if i in implicit_blocking_idx:
            if t:
                implicit_buf.append(ln)
            continue
        else:
            _flush_implicit()
        # Scene heading detection (delegated to existing helper).
        if is_scene_heading(t):
            cur_scene_idx += 1
            scene_starts.append(line_off)
    # Flush any trailing implicit-blocking content that didn't hit a non-blocking line.
    _flush_implicit()
    # Episode-level blocks = those that sat before scene 1.
    episode_blocks = '\n'.join(b['text'] for b in blocks if b['scene_idx'] == 0).strip()
    if chunk_text is None:
        scene_only = '\n'.join(b['text'] for b in blocks).strip()
        return episode_blocks, scene_only
    # Locate the chunk in the script and figure out which scene it belongs to.
    chunk_scene_idx = 0
    if chunk_text:
        cands = [s.strip() for s in chunk_text.split('\n') if len(s.strip()) >= 25 and not is_scene_heading(s.strip())]
        chunk_off = -1
        for c in cands[:6]:
            idx = script_text.find(c)
            if idx >= 0 and script_text.find(c, idx + 1) == -1:
                chunk_off = idx
                break
        if chunk_off < 0:
            for c in cands[:6]:
                idx = script_text.find(c)
                if idx >= 0:
                    chunk_off = idx
                    break
        if chunk_off >= 0:
            # Find which scene_start the chunk_off sits inside
            for i, start in enumerate(scene_starts, start=1):
                if start > chunk_off:
                    break
                chunk_scene_idx = i
    scene_text = '\n'.join(
        b['text'] for b in blocks
        if b['scene_idx'] == chunk_scene_idx and b['kind'] == 'in'
    ).strip()
    # Also include the PREVIOUS scene's [BLOCKING_OUT] for continuity context.
    prev_out = '\n'.join(
        b['text'] for b in blocks
        if b['scene_idx'] == chunk_scene_idx - 1 and b['kind'] == 'out'
    ).strip()
    if prev_out:
        scene_text = (prev_out + '\n' + scene_text).strip() if scene_text else prev_out
    return episode_blocks, scene_text


def _seedance_inject_blocking(prompt_text, blocking_text, chunk_tags_used):
    """Insert filtered blocking BEFORE 'Constraints:' in the 6-block promptEn.
    Falls back to append if Constraints not found. Mirrors reference injectBlocking."""
    if not blocking_text or not blocking_text.strip():
        return prompt_text
    filtered = _seedance_filter_blocking_for_chunk(blocking_text, chunk_tags_used)
    if not filtered or len(filtered) < 30:
        return prompt_text
    trimmed = re.sub(r'\s+', ' ', filtered).strip()
    idx = prompt_text.rfind('Constraints:')
    if idx < 0:
        return prompt_text + f'\n\nScene blocking: {trimmed}'
    return (
        prompt_text[:idx]
        + f'Scene blocking (consistent across all chunks of this episode): {trimmed}\n\n'
        + prompt_text[idx:]
    )


_SD_HARD_CUTS_CLAUSE = (
    'hard cuts between shots with different camera angles, no fade transitions, '
    'no dissolves, no cross-fades, sharp instant edits between framings'
)

def _seedance_inject_hard_cuts(prompt_text):
    """Append hard-cuts directive to Constraints. Mirrors reference injectHardCuts.
    Programmatic — more reliable than asking AI to write it."""
    if not prompt_text or 'hard cuts between shots' in prompt_text:
        return prompt_text
    idx = prompt_text.rfind('Constraints:')
    if idx < 0:
        return prompt_text + f'\n\nConstraints: {_SD_HARD_CUTS_CLAUSE}'
    # Append at end of the last (Constraints) line
    return re.sub(
        r'(Constraints:[^\n]*?)(\s*)$',
        lambda m: f'{m.group(1)}, {_SD_HARD_CUTS_CLAUSE}',
        prompt_text, count=1, flags=re.DOTALL
    )


def _prev_episode_ending_context(sid, num):
    """Build a multi-section text describing how episode N-1 ended — used as
    continuity context when generating scene_blocking / batch-compose for N.
    TikTok-format episodes often pick up immediately where N-1 left off, so
    character positions must be inherited (Adrian still at the door, Clara
    still at the desk) instead of reset.

    Returns None when there's no prev episode or no useful context.
    Sections (concat'd with blank lines):
      1. Last scene heading of N-1's script
      2. Last 6 non-empty lines of N-1's script
      3. N-1's batch_episode_blocking (whole-episode geometry from last build)
      4. Last completed seedance chunk's ending_state (if poll captured it)
    """
    try:
        n = int(num)
    except Exception:
        return None
    if n <= 1:
        return None
    prev_ep = load_episode(sid, n - 1)
    if not prev_ep:
        return None
    parts = []

    prev_script = _normalize_blocking_tags((prev_ep.get('script') or '').strip())
    if prev_script:
        last_heading = None
        for line in prev_script.split('\n'):
            t = line.strip()
            if is_scene_heading(t):
                last_heading = t
        if last_heading:
            parts.append(f"PREV EP {n-1} — last scene heading:\n{last_heading[:160]}")

        non_empty = [l.strip() for l in prev_script.split('\n') if l.strip()]
        clean = [l for l in non_empty
                 if not l.startswith('- **')
                 and not l.startswith('**КРАТКОЕ')
                 and not l.startswith('## ')]
        tail = clean[-6:] if len(clean) > 6 else clean
        if tail:
            parts.append("PREV EP — last 6 lines (immediate beats before cut):\n" + '\n'.join(tail))

        # Extract [BLOCKING_END] position block if present
        ep_end_start = prev_script.find('[BLOCKING_END]')
        ep_end_finish = prev_script.find('[/BLOCKING_END]')
        if ep_end_start != -1 and ep_end_finish != -1 and ep_end_finish > ep_end_start:
            ep_end_block = prev_script[ep_end_start:ep_end_finish + len('[/BLOCKING_END]')].strip()
            parts.append(
                f"PREV_END_POSITION:\n{ep_end_block}\n"
                "CONTINUITY RULE: If this episode opens in the same scene/location — "
                "the [BLOCKING] block MUST exactly match this end position "
                "(same characters, same positions). If starting a new scene, create a fresh [BLOCKING]."
            )

    prev_blocking = (prev_ep.get('batch_episode_blocking') or prev_ep.get('scene_blocking') or '').strip()
    if prev_blocking:
        parts.append(f"PREV EP — episodeBlocking (whole-episode geometry):\n{prev_blocking[:1500]}")

    chunks = prev_ep.get('seedance_chunks') or []
    last_completed = None
    for c in reversed(chunks):
        if c.get('status') == 'completed' and (c.get('ending_state') or '').strip():
            last_completed = c
            break
    if last_completed:
        es = (last_completed.get('ending_state') or '').strip()
        parts.append(f"PREV EP — last generated chunk ending state (final tableau before cut):\n{es[:400]}")

    if not parts:
        return None
    return '\n\n'.join(parts)


def _build_episode_tag_mapping(script_text, active_chars, active_locs):
    """Deterministic tag mapping `@Image1=name1, @Image2=name2, ...` based on
    first-appearance order in the script. Run BEFORE the AI call so Claude
    receives a fixed mapping and can't shuffle indices between segments.

    Strategy:
      1. Find first occurrence of each active character's name in script.
      2. Find first occurrence of each active location's name (or scene
         heading slug) in script.
      3. Sort by position; assign @Image1, @Image2, ...
    Returns list of {tag, kind: 'char'|'loc', id, name}.
    """
    if not script_text:
        return []
    lower = script_text.lower()
    entries = []
    seen_ids = set()
    for c in active_chars or []:
        nm = (c.get('name') or '').strip()
        if not nm or c['id'] in seen_ids: continue
        # Find first appearance — prefer line-start "Name:" cue, fall back to any mention
        pat_cue = re.compile(r'^\s*' + re.escape(nm) + r'\s*[:：]', re.MULTILINE | re.IGNORECASE)
        m = pat_cue.search(script_text)
        pos = m.start() if m else lower.find(nm.lower())
        if pos >= 0:
            entries.append({'kind': 'char', 'id': c['id'], 'name': nm, 'pos': pos})
            seen_ids.add(c['id'])
    for l in active_locs or []:
        nm = (l.get('name') or '').strip()
        if not nm: continue
        pos = lower.find(nm.lower())
        if pos < 0:
            # Try scene-heading style (INT./EXT. NAME — TIME)
            pat = re.compile(
                r'(?:INT\.|EXT\.|ИНТ\.|ИНТА\.|ЭКСТ\.|ЭКС\.|НАТ\.|ВНУТР\.|ИНТЕРЬЕР|Локация\s*:)\s*' +
                re.escape(nm), re.IGNORECASE
            )
            m = pat.search(script_text)
            pos = m.start() if m else -1
        if pos >= 0:
            entries.append({'kind': 'loc', 'id': l['id'], 'name': nm, 'pos': pos})
    entries.sort(key=lambda e: e['pos'])
    out = []
    for i, e in enumerate(entries):
        out.append({
            'tag': f'@Image{i+1}',
            'kind': e['kind'],
            'id': e['id'],
            'name': e['name'],
        })
    return out


# Generic clothing nouns. If the appearance ends with one of these
# (preceded by optional adjectives), the trailing phrase is "vague" — it
# tells the model only that there are clothes, not which ones. Seedance
# treats this as a creative freedom and picks a different concrete look
# per chunk (sweater on chunk 1, coat on chunk 2, etc.) — causing the
# clothing-drift bug Eduard reported with Lena. Concrete garment names
# (blazer / dress / suit / coat / jeans / shirt / ...) are NOT in this
# list — they're left intact because they actually constrain the look.
_VAGUE_CLOTHING_TAIL_RE = re.compile(
    r'[,;]?\s*(?:and\s+|wearing\s+|dressed\s+in\s+|in\s+|with\s+)?'
    r'(?:[a-zA-Zа-яА-ЯёЁ\-]+\s+){0,5}'
    r'(?:clothing|clothes|attire|outfit|wear|garments?|wardrobe'
    r'|одежда|одежде|одежду|наряд|наряде|гардероб)\b'
    r'\.?\s*$',
    flags=re.IGNORECASE,
)

def _strip_vague_clothing_tail(appearance: str) -> str:
    """Remove a trailing vague-clothing phrase (e.g. "and open casual
    clothing.", "in everyday attire", "wearing casual outfit") from an
    appearance string. Specific garment names are preserved because they
    don't end in the generic nouns matched by `_VAGUE_CLOTHING_TAIL_RE`.

    Returns the original string unchanged if stripping would gut more
    than 70% of the text (paranoid guard against a degenerate match
    swallowing the whole sentence)."""
    if not appearance:
        return appearance
    candidate = _VAGUE_CLOTHING_TAIL_RE.sub('', appearance).rstrip(' ,;.').strip()
    if len(candidate) >= max(15, int(len(appearance) * 0.3)):
        return candidate
    return appearance


# Concrete garment nouns (EN + RU). Unlike _VAGUE_CLOTHING_TAIL_RE these name a
# SPECIFIC item, so a clause "<intro cue> … <garment>" is a real wardrobe
# statement we must excise before appending an authoritative outfit.
_GARMENT_NOUN = (
    r'(?:dress(?:es)?|gown|apron|cloak|cape|robe|bathrobe|housecoat|suit|shirt|blouse|skirt'
    r'|trousers?|pants|slacks|jeans|coat|raincoat|overcoat|peacoat|trench(?:coat)?|jacket'
    r'|uniform|scrubs|armou?r|tunic|vest|sweater|sweatshirt|sweatpants|jumper|hoodie'
    r'|t-?shirt|tee|tank\s+top|turtleneck|polo|jersey|leotard|onesie|tracksuit'
    r'|slip|nightgown|nightdress|nightshirt|nightie|pyjamas|pajamas'
    r'|boots?|shoes?|heels?|sandals?|slippers?|sneakers?|trainers?|loafers?|gloves?|mittens?'
    r'|hat|cap|beanie|bonnet|helmet|mask|goggles|scarf|shawl|veil|tie|kimono|sari'
    r'|overalls|dungarees|leggings|shorts|jumpsuit|romper|bodice|corset|petticoat|frock|smock'
    r'|breeches|doublet|waistcoat|sash|kaftan|caftan|turban|headscarf|towel'
    r'|cardigan|blazer|parka|anorak|poncho|toga|loincloth|garb|swimsuit|bikini|trunks'
    r'|briefs|boxers|underwear|lingerie|tuxedo|tux|kilt|cassock|habit|negligee|camisole'
    r'|stockings|tights|socks?|bowtie|bow\s+tie|tiara|crown|diadem|circlet|brooch|sarong'
    r'|jodhpurs|chemise|kirtle|surcoat|mantle|wrap|gauntlets?'
    # worn accessories — bundled with outfits via "and/with", so listing them
    # lets the connector consume "…and expensive watch" instead of leaving a
    # dangling "and" (ring is deliberately omitted — too many non-jewelry senses)
    r'|watch|wristwatch|necklace|earrings?|bracelet|pendant|locket|choker|anklet'
    r'|cuff-?links?|suspenders|belt|glasses|sunglasses|monocle|wristband'
    r'|платье\w*|рубаш\w*|костюм\w*|плащ\w*|пальто|куртк\w*|юбк\w*|брюк\w*'
    r'|джинс\w*|сапог\w*|туфл\w*|кроссовк\w*|ботинк\w*|перчатк\w*|шляп\w*|шарф\w*|мундир\w*'
    r'|форм\w*|фартук\w*|сарафан\w*|пиджак\w*|халат\w*|корон\w*|диадем\w*|брошь\w*|носк\w*)'
)

# A clothing clause introduced by a cue ("wearing / in / dressed in / …") and a
# concrete garment. Extension is CONNECTOR-driven (not greedy-to-clause-end), so
# it captures compound garments ("frock coat"), connected items ("dress WITH
# white apron") and trailing accessories ("gown WITH an emerald tiara") WITHOUT
# swallowing a bundled non-clothing trait ("scrubs WITH her hair tied back" →
# keeps "her hair tied back"; "red dress AND clearly pregnant" → keeps the
# pregnancy). The garment gate keeps non-clothing "in …" phrases ("in a
# wheelchair", "in her thirties", "needle in hand") from matching at all.
_CLOTHING_CLAUSE_RE = re.compile(
    r'(?:[,;]\s*|\s+|^)'
    r'(?:wearing|dressed\s+in|clad\s+in|sporting|donning|attired\s+in'
    r'|одет\w*\s+в|носит|in|в)\s+'
    r'(?:(?:a|an|the|her|his|their|its|some|plain|simple|pair\s+of|её|его|их)\s+)?'
    r'[^,;.]*?\b' + _GARMENT_NOUN + r'\b'
    r'(?:[ \t\-]+' + _GARMENT_NOUN + r'\b)*'                       # compound garments
    r'(?:\s+(?:with|and|over|under|featuring|plus|paired\s+with|и|с)\s+'
    r'[^,;.]*?\b' + _GARMENT_NOUN + r'\b)*',                       # connected garments/accessories
    flags=re.IGNORECASE,
)

# A garment clause with NO intro cue — e.g. "practical rubber gloves on her
# hands", "worn sneakers". The cued regex above can't see these. Bounded by
# clause delimiters so it removes the whole bare-garment clause, not a fragment.
_BARE_GARMENT_CLAUSE_RE = re.compile(
    r'(?:(?<=,)|(?<=;)|(?<=—)|(?<=–)|^)'
    r'\s*(?:(?:a|an|the|her|his|their|its|some|plain|simple|practical|worn|old|new|pair\s+of)\s+)?'
    r'(?:[a-zA-Zа-яёА-ЯЁ]+\s+){0,3}'
    r'\b' + _GARMENT_NOUN + r'\b'
    r'(?:[ \t\-]+' + _GARMENT_NOUN + r'\b)*'
    r'(?:\s+(?:on|around|over|under|across)\s+(?:her|his|their|the|its)\s+[a-zA-Zа-яёА-ЯЁ]+)?'
    r'\s*(?=,|;|—|–|\.|$)',
    flags=re.IGNORECASE,
)


def _strip_concrete_clothing(appearance: str) -> str:
    """Remove every concrete clothing clause from an appearance string, leaving
    only the PERSON (face / build / hair / role). Run before appending an
    authoritative outfit description so BINDING never lists two outfits at once
    — the root cause of Seedance rendering a hybrid wardrobe (Elena
    "grey seamstress dress + black Dark Cloak", Jun 2026; Elena-the-nurse
    "hospital scrubs + black sheath dress" leaking medical scrubs into a date
    scene, ep3 My_Affair, Jun 2026; same class as the Claire & Lydia incidents
    whose narrow `wearing…$` fix missed the "in a … dress" phrasing).

    Two passes: cued clothing clauses (wearing/in/dressed in/…) AND bare
    no-cue garment clauses ("practical rubber gloves on her hands")."""
    if not appearance:
        return appearance
    candidate = _CLOTHING_CLAUSE_RE.sub('', appearance)
    candidate = _BARE_GARMENT_CLAUSE_RE.sub('', candidate)
    # Tidy separators left behind by mid-sentence removals.
    candidate = re.sub(r'\s+', ' ', candidate)
    candidate = re.sub(r'\s*[—–\-]\s*(?=,|;|\.|$)', '', candidate)      # dangling dash before delim
    candidate = re.sub(r'(?:^|(?<=[,;]))\s*[—–]\s*', ' ', candidate)    # leading dash after delim/start
    candidate = re.sub(r'\s*,\s*(?=,|\.|;|$)', '', candidate)
    candidate = re.sub(r',\s*,+', ', ', candidate)
    candidate = candidate.replace(' ,', ',').replace(' .', '.').replace(' ;', ';')
    return candidate.strip(' ,;.—–-').strip()


# ─────────────────────────────────────────────────────────────────────────────
# HAIR / ALTERNATE-IDENTITY support.
#
# Root cause of the "disguise still has the old hair" class (Claire goes blonde
# as "Emma Cross" in ep4 of Pregnant by My Director but renders brunette in ep5):
#   • a character has ONE `appearance` string that owns face+body+HAIR;
#   • outfits override only CLOTHING (the clothing-stripper keeps hair);
#   • the outfit reference image is i2i'd from the base portrait with the
#     instruction "only the clothing changes" — so hair is hard-locked brunette.
# There was therefore NO path for hair to change with a look. These helpers add
# one: a look can declare a HAIR override (explicit `appearance_override` field
# or a hair phrase detected inside its description); the canonical BINDING then
# swaps the base hair for the look's hair, and the outfit-image gen unlocks hair.
# ─────────────────────────────────────────────────────────────────────────────

# Colour / style words that, next to "hair"/"волос" (or as a wig), denote a
# deliberate HAIR look — used to detect that an outfit/disguise changes the hair.
_HAIR_LOOK_WORDS = (
    r'platinum|blonde|blond|brunette|jet[-\s]?black|raven|auburn|ginger|redhead'
    r'|red|copper|chestnut|silver|ash|honey|caramel|strawberry|bleached|dyed|dyes|dye'
    r'|frosted|highlighted|salt[-\s]?and[-\s]?pepper|black|brown|grey|gray|white'
    r'|buzz[-\s]?cut|shaved|bald|crew[-\s]?cut|pixie|cropped|braided|cornrows|dreadlocks'
    r'|блонд\w*|брюнет\w*|рыж\w*|сед\w*|русоволос\w*|темноволос\w*|перекраш\w*|крашен\w*|налысо|лыс\w*'
)
# A look/outfit description "changes the hair" if it names a wig OR a
# hair-colour/style word sits next to the word "hair"/"волос".
_OUTFIT_HAIR_RE = re.compile(
    r'\b(?:'
    r'(?:' + _HAIR_LOOK_WORDS + r')(?:[\s-]+\w+){0,2}?[\s-]+(?:hair|волос\w*)'
    r'|(?:hair|волос\w*)(?:[\s-]+\w+){0,2}?[\s-]+(?:' + _HAIR_LOOK_WORDS + r')'
    r'|wig|hairpiece|парик\w*'
    r')\b',
    re.IGNORECASE,
)

# Hair adjectives used to bound the hair NOUN-PHRASE inside a base appearance.
# Deliberately a CLOSED list (no generic `\w+` fallback) so a substitution can
# never swallow a neighbouring trait ("dark eyes and pulled-back hair" must keep
# "dark eyes"; "long brown hair and green eyes" must keep "green eyes").
_HAIR_ADJ = (
    r'(?:long|short|shoulder[-\s]?length|mid[-\s]?length|medium|cropped|wavy|curly|straight'
    r'|sleek|messy|tousled|neat|pulled[-\s]?back|swept[-\s]?back|tied[-\s]?back|loose|fine|thick|thin'
    r'|dark|light|pale|jet|salt[-\s]?and[-\s]?pepper'
    r'|platinum|blonde|blond|brunette|raven|auburn|ginger|red|copper|chestnut|silver|grey|gray'
    r'|white|brown|black|honey|caramel|strawberry|ash|bleached|dyed'
    r'|длинн\w*|коротк\w*|тёмн\w*|темн\w*|светл\w*|сед\w*|русо\w*|рыж\w*|кудряв\w*|прям\w*)'
)
_HAIR_CLAUSE_RE = re.compile(
    r'(?<![A-Za-zА-Яа-яЁё-])'
    r'(?:(?:' + _HAIR_ADJ + r')[\s-]+){0,4}'
    r'(?:hair|волос\w*)'
    r'(?:\s+(?:tied|pulled|swept|pinned|braided|worn|hanging|falling|cascading|loose|down|up|back'
    r'|in\s+(?:a\s+)?(?:bun|ponytail|braid|plait|knot|chignon|updo|pixie|bob))'
    r'(?:\s+[a-zA-Zа-яё]+){0,4})?',
    flags=re.IGNORECASE,
)


def _outfit_hair_phrase(outfit) -> str:
    """Authoritative HAIR phrase for a look, if it deliberately changes hair
    (disguise / dyed / wig). Priority: explicit `appearance_override` field, then
    a hair phrase detected inside the outfit description. Returns '' when the look
    keeps the character's natural hair (the common case — most outfits)."""
    if not outfit:
        return ''
    ov = (outfit.get('appearance_override') or '').strip()
    if ov:
        return ov
    m = _OUTFIT_HAIR_RE.search(outfit.get('description') or '')
    return m.group(0) if m else ''


def _override_hair_in_appearance(appearance: str, hair_phrase: str) -> str:
    """Rewrite the hair clause of a base appearance for an alternate look.

      • hair_phrase non-empty → REPLACE the base hair clause with it (e.g. base
        "…pulled-back hair…" + "platinum-blonde hair" → "…platinum-blonde hair…").
        If the appearance has no hair clause, the phrase is appended so the look's
        hair is still asserted.
      • hair_phrase empty → STRIP the base hair clause (used when the outfit
        description ALREADY carries the hair, so BINDING isn't doubled).

    Closed-vocabulary clause regex guarantees neighbouring traits survive."""
    if not appearance:
        return appearance
    repl = (hair_phrase or '').strip()
    new, n = _HAIR_CLAUSE_RE.subn(repl, appearance, count=1)
    if n:
        new = re.sub(r'\s+', ' ', new)
        if not repl:
            # collapse connectors orphaned by the removal ("eyes and  and headset"
            # → "eyes and headset"; dangling trailing "… and ," → "…,").
            new = re.sub(r'\b(and|with|и|с)\s+(and|with|и|с)\b', r'\2', new, flags=re.IGNORECASE)
            new = re.sub(r'\b(and|with|и|с)\s*(?=,|;|\.|$)', '', new, flags=re.IGNORECASE)
            new = re.sub(r'\s*,\s*(?=,|\.|;|$)', '', new)
            new = re.sub(r',\s*,+', ', ', new)
            new = new.replace(' ,', ',').replace(' .', '.').replace(' ;', ';')
            new = re.sub(r'\s+', ' ', new)
        return new.strip(' ,;.').strip()
    if repl:
        return (appearance.rstrip(' .,;') + '; ' + repl).strip()
    return appearance


