"""Story logic pipeline: writer/brief/audit prompt systems, logic-hole audit,
script doctor, crowding/no-name/overlength detectors, logic brief, canon
extraction and rollback."""
import json
import math as _math
import re
from collections import Counter

from sw.jsonutils import _strip_markdown_fence, loads_lenient, strip_json
from sw.llm import claude_ask, claude_ask_fast
from sw.logging_utils import _log_event
# routes->helpers exception: trajectory/finale helpers live in landmarks
# (no cycle: landmarks does not import story_logic).
from sw.routes.landmarks import build_trajectory_block, is_finale_episode
from sw.storage import (DEVICE_TAXONOMY, NARRATIVE_ARCHETYPES,
                        NARRATIVE_EMOTIONS, WORLD_RULES, _next_id,
                        _resolve_char_by_script_name, load_canon, load_episode,
                        load_series, save_canon)
from sw.story_prompts import _format_mode_block
from sw.story_logic_audit import _AUDIT_SYSTEM, _BRIEF_SYSTEM, _EXTRACT_SYSTEM, _build_crowd_constraint_block, _build_plot_device_history, _format_canon_for_prompt

def _script_runtime_metrics(script: str) -> dict:
    """Programmatic length scan — counts dialogue lines + spoken words + action lines
    + estimated runtime.

    Returns:
        dialogue_lines, dialogue_words, action_lines, longest_line_words, avg_line_words,
        est_runtime_sec.

    Heuristic (calibrated against real TikTok/Reels short drama timings):
      - Spoken delivery: ~150 wpm for plain rapid dialogue, dropping to ~120 wpm for
        long emotional beats. We use 135 wpm midpoint.
      - Each dialogue beat: +1.2s baseline for the actor's "settle" + reaction
        (longer beats need more visual support, so we add another +0.8s per beat
        with >8 words).
      - Each action line (non-dialogue narrative outside [BLOCKING]): +2.0s — actions
        like "She picks up the phone" or "He walks across the room" take real screen
        time even with no words spoken.
      - Looks for explicit time markers in action lines ("две минуты молча", "for ten
        seconds") and adds them — writers sometimes script multi-minute beats in a
        single sentence.
    """
    if not script:
        return {
            'dialogue_lines': 0, 'dialogue_words': 0, 'action_lines': 0,
            'longest_line_words': 0, 'avg_line_words': 0, 'est_runtime_sec': 0,
        }
    # Speaker cue. CRITICAL: must accept parenthetical cues like
    #   DEREK (O.S.):  /  JESSICA (V.O.):  /  ETHAN (CONT'D):  /  MAYA (тихо):
    # Before 2026-06-04 the char-class excluded '(' ')' so EVERY `NAME (O.S.):`
    # line fell through to "action line" — the detector saw 3 dialogue lines +
    # 17 action lines in a normal 8-line dialogue scene, undercounting spoken
    # words ~75% and overcounting action. That made the length governor fire
    # contradictory violations (too few words AND too much action at once),
    # broke the retry loop, and shipped wildly inconsistent episode lengths.
    dialogue_re = re.compile(
        r'^\s*[A-ZА-ЯЁ][A-ZA-Zа-яёА-ЯЁ0-9\.\-\' ]{0,40}(?:\([^)]*\))?\s*:\s*(.*)$')
    # Skip lines inside [BLOCKING]/[BLOCKING_END] / scene headers / cut-markers
    scene_header_re = re.compile(r'^\s*(?:INT\.|EXT\.|ИНТ\.|ЭКСТ\.|INT/EXT\.)', re.IGNORECASE)
    cut_marker_re   = re.compile(r'═══\s*END\s+EPISODE', re.IGNORECASE)
    # Time markers in narrative — "две минуты молча", "for 30 seconds", "10 секунд"
    minute_re = re.compile(r'\b(\d+|одну?|две|три|четыре|пять|десять|fifteen|twenty|thirty)\s*(?:минут|minutes|min)\b', re.IGNORECASE)
    second_re = re.compile(r'\b(\d+|десять|fifteen|twenty|thirty)\s*(?:секунд|seconds|sec)\b', re.IGNORECASE)
    _word_to_num = {'one':1,'two':2,'three':3,'four':4,'five':5,'ten':10,'fifteen':15,'twenty':20,'thirty':30,
                    'одну':1,'один':1,'две':2,'два':2,'три':3,'четыре':4,'пять':5,'десять':10}

    # Parenthetical stage directions inside a dialogue line — e.g.
    # `ETHAN: (в микрофон, указывая на Кайна) Viktor Kain laundered three…`
    # The parenthetical is stage direction, NOT spoken text — strip before
    # counting words. Real bug 2026-05-30: «I Became My Dead Brother's Ghost»
    # ep 1 reported 91 spoken words; user said «по факту персонажи говорят
    # около 50 слов». Difference was 100% explained by parentheticals being
    # counted as speech.
    paren_re = re.compile(r'\([^)]*\)')
    in_blocking = False
    in_dialogue_continuation = False  # for multi-line dialogue (NAME:\n"line")
    current_speaker = None
    dialogue_lines = 0
    dialogue_words = 0
    action_lines = 0
    longest_line_words = 0
    explicit_time_sec = 0
    line_word_counts = []

    lines = script.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        # Toggle BLOCKING fences
        if '[BLOCKING]' in raw.upper() and '[/BLOCKING]' not in raw.upper():
            in_blocking = True
            continue
        if '[/BLOCKING]' in raw.upper() or '[BLOCKING_END]' in raw.upper():
            in_blocking = False
            continue
        if in_blocking:
            continue
        if not line:
            in_dialogue_continuation = False
            continue
        if scene_header_re.match(raw) or cut_marker_re.search(raw):
            in_dialogue_continuation = False
            continue
        if line.startswith('Кратко:') or line.startswith('Episode '):
            continue
        # Dialogue header (NAME:)
        m = dialogue_re.match(raw)
        if m:
            current_speaker = True
            tail = m.group(1).strip()
            # Strip parenthetical stage directions — they're NOT spoken words.
            tail_spoken = paren_re.sub('', tail).strip()
            if tail_spoken:
                # Inline dialogue: NAME: text
                wc = len(tail_spoken.split())
                dialogue_lines += 1
                dialogue_words += wc
                line_word_counts.append(wc)
                if wc > longest_line_words:
                    longest_line_words = wc
                in_dialogue_continuation = False
            elif tail:
                # Header had ONLY a parenthetical (e.g. `ETHAN: (whispers)`) —
                # the spoken text comes on the next line.
                in_dialogue_continuation = True
            else:
                # Header on its own — next non-empty line is the actual dialogue
                in_dialogue_continuation = True
            continue
        # Continuation of a dialogue header (multi-line: NAME:\n"text")
        if in_dialogue_continuation:
            # Strip surrounding quotes + parenthetical stage directions
            tail = line.strip('"').strip("'").strip('«»').strip()
            tail = paren_re.sub('', tail).strip()
            if tail:
                wc = len(tail.split())
                dialogue_lines += 1
                dialogue_words += wc
                line_word_counts.append(wc)
                if wc > longest_line_words:
                    longest_line_words = wc
            in_dialogue_continuation = False
            continue
        # Otherwise — action / narrative line
        action_lines += 1
        # Scan for explicit time markers
        for mm in minute_re.finditer(line):
            raw_v = mm.group(1).lower()
            n = int(raw_v) if raw_v.isdigit() else _word_to_num.get(raw_v, 0)
            explicit_time_sec += n * 60
        for mm in second_re.finditer(line):
            raw_v = mm.group(1).lower()
            n = int(raw_v) if raw_v.isdigit() else _word_to_num.get(raw_v, 0)
            explicit_time_sec += n

    avg_line = round(dialogue_words / dialogue_lines, 1) if dialogue_lines else 0

    # Runtime estimate — CALIBRATED TO THE JS SEGMENTER (static/app.js), which is
    # the ground truth for actual rendered video length. The segmenter packs the
    # script into Seedance chunks using SPEECH_WPS=2.65 and ACTION_BEAT_SEC=1.5,
    # then each chunk carries a ~1.5s buffer. If THIS estimator uses a different
    # calibration (it used 135wpm≈2.25wps + 2.0s/action before 2026-06-04) the
    # writer is told "60s" by one yardstick while the renderer produces ~45s —
    # which is exactly why 60s-target episodes came out at 30/40/60s.
    _SPEECH_WPS = 2.65          # mirror SPEECH_WPS in app.js
    _ACTION_BEAT_SEC = 1.5      # mirror ACTION_BEAT_SEC in app.js
    _LINE_PREPAUSE = 0.4        # mirror per-line 0.4s pre-pause in _lineDuration
    _CHUNK_BUFFER = 1.5         # mirror per-chunk buffer in _estimateChunkDurationSec
    _CHUNK_SEC = 14.0           # mirror effective chunk packing size

    spoken_sec = dialogue_words / _SPEECH_WPS
    beat_overhead = _LINE_PREPAUSE * len(line_word_counts)
    action_sec = action_lines * _ACTION_BEAT_SEC
    # Cap explicit_time_sec at 120s to avoid runaway from typos like "100 минут"
    explicit_time_sec = min(explicit_time_sec, 120)

    content_sec = spoken_sec + beat_overhead + action_sec + explicit_time_sec
    # Per-chunk buffer: the renderer splits content into ~14s chunks, each padded.
    import math as _math
    num_chunks = max(1, _math.ceil(content_sec / _CHUNK_SEC)) if content_sec > 0 else 0
    est_runtime = round(content_sec + num_chunks * _CHUNK_BUFFER)

    return {
        'dialogue_lines': dialogue_lines,
        'dialogue_words': dialogue_words,
        'action_lines': action_lines,
        'longest_line_words': longest_line_words,
        'avg_line_words': avg_line,
        'est_runtime_sec': est_runtime,
        'explicit_time_sec': explicit_time_sec,
    }


def detect_script_overlength(s, script: str) -> dict:
    """Programmatic length-budget detector. Returns {} (within budget) or a violation dict.

    Target duration comes from `target_duration_sec` on the series (default 60s).
    Triggers (any of):
      • estimated runtime > 130% of target (script too long)
      • any single dialogue line > 12 words (long monologues kill TikTok pacing)
      • avg dialogue line > 9 words (overall too talky)
      • action line count > 1.5× action budget (too much narrative business)
      • dialogue_words < 60% of target — UNDERSHOOT. Writer is too cautious and
        delivers half the spoken-words target; the resulting video has long
        silent stretches because the chunker still produces the BLOCKING-driven
        scenes but the audio runs out. Catches the 2026-05-30 issue where a
        ~100-word target produced ~50 spoken words.
    """
    try:
        target_sec = int((s or {}).get('target_duration_sec') or 60)
    except (TypeError, ValueError):
        target_sec = 60
    # Speech budget @2.65 wps (matches JS segmenter): ~110 words fills a 60s
    # episode once per-line pre-pauses, action beats and chunk buffers are added.
    target_words = round(target_sec / 60 * 110)
    target_lines = max(3, min(40, round(target_sec / 4.5)))
    # SYMMETRIC band — the whole point of this detector is that a 60s target
    # produces ~60s, not 30/40/60. Both ends are enforced so the writer can't
    # under- OR over-shoot. Floor 0.8×, ceiling 1.2× of target runtime.
    floor_sec = target_sec * 0.8
    ceil_sec = target_sec * 1.2
    floor_words = round(target_words * 0.75)
    m = _script_runtime_metrics(script)
    est_sec = m['est_runtime_sec']
    dialogue_lines = m['dialogue_lines']
    dialogue_words = m['dialogue_words']
    longest_line = m['longest_line_words']
    avg_line = m['avg_line_words']
    action_lines = m['action_lines']

    reasons = []
    if est_sec > ceil_sec:
        reasons.append(
            f"runtime ~{est_sec}с — СЛИШКОМ ДЛИННО ({round(est_sec/target_sec, 1)}× от {target_sec}с). "
            f"Сократи реплики/action до ~{target_sec}с (потолок {round(ceil_sec)}с).")
    elif est_sec < floor_sec and est_sec > 0:
        reasons.append(
            f"runtime ~{est_sec}с — СЛИШКОМ КОРОТКО (нужно ~{target_sec}с, минимум {round(floor_sec)}с). "
            f"Добавь реплик/действий до ~{target_sec}с. Серия выйдет короче заявленной длины.")
    if longest_line > 12:
        reasons.append(f"самая длинная реплика {longest_line} слов (лимит 10, идеал 3-7)")
    if avg_line > 9:
        reasons.append(f"средняя длина реплики {avg_line} слов (лимит 7, идеал 5)")
    # Action lines budget proportional to target duration
    action_budget = max(3, round(target_sec / 12))
    if action_lines > action_budget * 1.5:
        reasons.append(f"action-строк {action_lines} (лимит ~{action_budget})")
    # Words floor only fires when runtime didn't already flag undershoot (avoid
    # double-reporting the same problem).
    if dialogue_words < floor_words and est_sec >= floor_sec:
        reasons.append(
            f"спикерских слов {dialogue_words} — мало (минимум {floor_words}, цель {target_words}). "
            f"ACTION/BLOCKING не считаются. Сцена выйдет полупустой."
        )

    if not reasons:
        return {}
    return {
        'target_sec': target_sec,
        'target_lines': target_lines,
        'target_words': target_words,
        'est_sec': est_sec,
        'dialogue_lines': dialogue_lines,
        'dialogue_words': dialogue_words,
        'longest_line_words': longest_line,
        'avg_line_words': avg_line,
        'action_lines': action_lines,
        'ratio': round(est_sec / target_sec, 1),
        'reasons': reasons,
    }


def _build_narrative_state_block(sid: str, num: int) -> str:
    """Build the narrative momentum context block for episode num's generation prompt.
    Returns a POSITIVE DIRECTIVE block (not a prohibition list) showing the story trajectory
    and what this episode must achieve. Empty string if insufficient data.
    """
    s = load_series(sid)
    if s is None:
        return ''
    index = s.get('narrative_index') or []
    # Only use episodes before current num
    prior = [e for e in index if e.get('ep', 0) < num]
    if len(prior) < 2:
        return ''  # Not enough history yet

    window = prior[-8:]  # Last 8 episodes

    # Build history lines
    history_lines = []
    for e in window:
        ep_num = e.get('ep', '?')
        arch   = e.get('archetype', '?')
        pd     = e.get('power_delta', '?')
        em     = e.get('closing_emotion', '?')
        am     = e.get('antagonist_momentum', '?')
        history_lines.append(f"  Ep{ep_num}: {arch} → power: {pd} → mood: {em} | antagonist: {am}")

    # Detect patterns in last 6 episodes
    last6 = prior[-6:]
    arch_counts     = Counter(e.get('archetype') for e in last6 if e.get('archetype'))
    power_counts    = Counter(e.get('power_delta') for e in last6 if e.get('power_delta'))
    emotion_counts  = Counter(e.get('closing_emotion') for e in last6 if e.get('closing_emotion'))
    ant_counts      = Counter(e.get('antagonist_momentum') for e in last6 if e.get('antagonist_momentum'))

    # Device history — keep as secondary signal
    dev_index = s.get('devices_index') or {}
    device_warnings = []
    for dev_id in DEVICE_TAXONOMY:
        entries = dev_index.get(dev_id, [])
        if len(entries) >= 3:
            last_ep = max(e.get('ep', 0) for e in entries)
            device_warnings.append(f"  • {dev_id} — использован {len(entries)} раз (последний: ep{last_ep}) — ИСЧЕРПАН нарративно")

    # Build pattern warnings
    pattern_alerts = []
    overused_archs = [a for a, n in arch_counts.items() if n >= 3]
    if overused_archs:
        pattern_alerts.append(f"⚠️  Archetype '{overused_archs[0]}' — {arch_counts[overused_archs[0]]} из последних 6 серий")

    ant_losing_streak = power_counts.get('antagonist_wins', 0)
    if ant_losing_streak >= 4:
        pattern_alerts.append(f"⚠️  Протагонист проигрывает {ant_losing_streak} из последних 6 серий — зритель потерял веру")

    prot_winning_streak = power_counts.get('protagonist_wins', 0)
    if prot_winning_streak >= 4:
        pattern_alerts.append(f"⚠️  Протагонист побеждает {prot_winning_streak} из последних 6 — нужен серьёзный setback")

    dominant_emotion = emotion_counts.most_common(1)
    if dominant_emotion and dominant_emotion[0][1] >= 4:
        pattern_alerts.append(f"⚠️  Финальная эмоция '{dominant_emotion[0][0]}' повторяется {dominant_emotion[0][1]} раз — зритель привыкает")

    # Build REQUIRED directives
    directives = []
    if overused_archs:
        forbidden_arch_str = ', '.join(overused_archs)
        allowed_archs = [a for a in NARRATIVE_ARCHETYPES if a not in overused_archs]
        directives.append(f"→ archetype: НЕ '{forbidden_arch_str}' — выбери из: {', '.join(allowed_archs[:4])}")
    if ant_losing_streak >= 4:
        directives.append(f"→ power_delta: ДОЛЖЕН БЫТЬ protagonist_wins — протагонист добивается РЕАЛЬНОЙ победы, не просто 'узнаёт что-то'")
    if dominant_emotion and dominant_emotion[0][1] >= 4:
        bad_em = dominant_emotion[0][0]
        other_ems = [e for e in NARRATIVE_EMOTIONS if e != bad_em]
        directives.append(f"→ closing_emotion: НЕ '{bad_em}' снова — целься в: {', '.join(other_ems[:3])}")
    if ant_counts.get('escalating', 0) >= 4:
        directives.append(f"→ antagonist: должен впервые столкнуться с серьёзным препятствием, ошибкой или неожиданным осложнением")
    if device_warnings:
        directives.append(f"→ info-delivery: НЕ использовать исчерпанные механизмы (см. список ниже)")

    if not pattern_alerts and not directives:
        # Story has good variety — just show history as context, no hard directives
        block = (
            '═══ NARRATIVE MOMENTUM (последние серии) ═══\n'
            + '\n'.join(history_lines)
            + '\n✓ Хорошее разнообразие — продолжай варьировать archetype и emotional close.\n'
            '═══════════════════════════════════════════════════\n\n'
        )
        return block

    # Build the full block
    lines = ['═══ NARRATIVE MOMENTUM — ИСТОРИЯ КАК ЕЁ ВИДИТ ЗРИТЕЛЬ ═══']
    lines.append('Последние серии:')
    lines.extend(history_lines)
    if pattern_alerts:
        lines.append('')
        lines.extend(pattern_alerts)
    lines.append('')
    lines.append('════ ЭТОТ ЭПИЗОД ДОЛЖЕН ════')
    if directives:
        lines.extend(directives)
    else:
        lines.append('→ Поддержи хорошее разнообразие — не повторяй ни archetype, ни emotional close прошлой серии')
    if device_warnings:
        lines.append('')
        lines.append('Исчерпанные info-delivery механизмы (не использовать):')
        lines.extend(device_warnings)
    lines.append('')
    lines.append('ГЛАВНОЕ: каждый эпизод должен заканчиваться в ДРУГОМ эмоциональном состоянии, чем предыдущий.')
    lines.append('Разнообразие — это не смена декораций, это смена того, кто побеждает и что зритель чувствует.')
    lines.append('═══════════════════════════════════════════════════')
    lines.append('')

    return '\n'.join(lines)


def build_logic_brief(sid, num):
    """Pre-write: produce a constraints brief for episode `num` from canon + recent context."""
    s = load_series(sid)
    if not s: return ''
    canon = load_canon(sid)
    ep = load_episode(sid, num) or {}

    canon_block = _format_canon_for_prompt(canon)
    rules_block = json.dumps(WORLD_RULES, ensure_ascii=False, indent=2)

    # Recent episodes context (last 2 synopses + last script tail)
    recent = []
    for n in range(max(1, num - 2), num):
        prev = load_episode(sid, n)
        if prev:
            recent.append(f"Ep{n} synopsis: {prev.get('synopsis','')[:300]}")

    requested_day_advance = ep.get('days_since_previous')
    advance_hint = (f"\nPLANNED time skip from previous episode: {requested_day_advance} day(s)."
                    if requested_day_advance else
                    "\nNo planned time skip specified — infer minimum needed for biology/logic.")

    prompt = (
        f'Series: "{s.get("title","")}" | Genre: {s.get("genre","")}\n'
        f'Synopsis of THIS episode (ep {num}): {ep.get("synopsis","")}\n'
        + advance_hint + '\n\n'
        f'=== CANON ===\n{canon_block}\n\n'
        f'=== RECENT EPISODES ===\n' + ('\n'.join(recent) or '—') + '\n\n'
        f'=== WORLD RULES (real-world constants) ===\n{rules_block}\n\n'
        'Produce a constraints brief in RUSSIAN with these sections (use exactly these headers):\n'
        '## TIMELINE\n  - на каком дне происходит серия, сколько прошло с предыдущей, проверка биологических окон\n'
        '## LOCKED FACTS IN PLAY\n  - какие факты из канона активны в этой серии и как их соблюсти\n'
        '## CHARACTER KNOWLEDGE — ЧТО МОЖНО / НЕЛЬЗЯ ГОВОРИТЬ\n  - для каждого персонажа в серии: что он знает, что НЕ может знать (запрещённые реплики)\n'
        '## REQUIRED REVERSAL ANCHOR\n  - какой канонический факт реверсал может перевернуть, чтобы не вводить новый ретконн\n'
        '## OPEN THREADS TO ADDRESS\n  - какие открытые вопросы серия ОБЯЗАНА закрыть или явно отложить\n'
        '## FORBIDDEN CONTRADICTIONS\n  - короткий список конкретных вещей, которые сломают канон если появятся\n'
        '## BIOLOGY/PHYSICS CHECKS\n  - применимые числовые ограничения из WORLD RULES для этой серии\n'
        'Будь предельно конкретным. Если поле пустое — напиши "—". Не больше 25 строк всего.'
    )
    try:
        brief = claude_ask_fast(prompt, system=_BRIEF_SYSTEM).strip()
    except Exception as e:
        brief = f'(logic brief generation failed: {e})'
    # Append format-mode block (short_drama vs instagram_series) — top of brief
    try:
        fmt_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])
        if fmt_block:
            brief = f'## FORMAT MODE\n{fmt_block}\n' + brief
    except Exception:
        pass
    # Prepend user-pinned story trajectory (finale + checkpoints) — this is the
    # signal that the auditor and the writer both need to honor. Without it
    # surfaced at the top of the brief, the auditor cannot flag finale drift.
    try:
        traj = build_trajectory_block(s, num)
        if traj:
            brief = f'## STORY TRAJECTORY (USER-PINNED LANDMARKS)\n{traj}\n' + brief
    except Exception:
        pass
    # Append scene character cap rule (hard production constraint)
    try:
        crowd_rule = _build_crowd_constraint_block(s)
        if crowd_rule:
            brief += f'\n\n## ЛИМИТ ПЕРСОНАЖЕЙ В СЦЕНЕ (HARD RULE)\n{crowd_rule}'
    except Exception:
        pass
    # Append narrative momentum context to brief
    try:
        narrative_block = _build_narrative_state_block(sid, num)
        if narrative_block:
            brief += f'\n\n## NARRATIVE MOMENTUM — VARIETY REQUIRED\n{narrative_block}'
    except Exception:
        pass
    # Append plot-device forbidden list if present (secondary signal)
    try:
        devices_block = _build_plot_device_history(sid, num)
        if devices_block:
            brief += f'\n\n## ИСЧЕРПАННЫЕ INFO-DELIVERY МЕХАНИЗМЫ\n{devices_block}'
    except Exception:
        pass
    return brief


def audit_script(sid, num, script, brief):
    """Post-write: returns dict {passes, violations}. Never raises."""
    canon = load_canon(sid)
    canon_block = _format_canon_for_prompt(canon)
    prompt = (
        f'=== CANON ===\n{canon_block}\n\n'
        f'=== LOGIC BRIEF FOR EP {num} ===\n{brief}\n\n'
        f'=== SCRIPT TO AUDIT ===\n{script}\n\n'
        'Find every continuity violation. Output strict JSON per the schema.'
    )
    try:
        raw = claude_ask_fast(prompt, system=_AUDIT_SYSTEM)
        data = loads_lenient(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
        # Last-ditch repair: try just regex-stripping fences + lenient parse
        try:
            data = loads_lenient(_strip_markdown_fence(raw))
            if isinstance(data, dict):
                data.setdefault('violations', [])
                data['passes'] = bool(data.get('passes', not any(
                    v.get('severity') == 'critical' for v in data['violations']
                )))
                return data
        except Exception:
            pass
        _log_event('WARN', 'audit_json_parse_fail', err=str(e)[:200], raw_head=raw[:200] if 'raw' in dir() else '')
        return {'passes': True, 'violations': [], 'audit_error': str(e)}


def extract_canon_updates(sid, num, script):
    """Auto-extract canon updates from a finalized script and merge into canon.json."""
    canon = load_canon(sid)
    canon_block = _format_canon_for_prompt(canon)
    prompt = (
        f'=== EXISTING CANON (for context, do NOT repeat existing facts) ===\n{canon_block}\n\n'
        f'=== EPISODE {num} SCRIPT ===\n{script}\n\n'
        'Extract canon updates per the JSON schema. Only NEW information from THIS episode.'
    )
    try:
        raw = claude_ask_fast(prompt, system=_EXTRACT_SYSTEM)
        upd = json.loads(strip_json(raw))
    except Exception as e:
        return {'error': str(e)}

    # Merge into canon
    wc = canon['world_clock']
    advance = max(0, int(upd.get('world_day_advance') or 0))
    # If episode N replaces a previously recorded one, recompute from previous tl entry
    prev_day = wc.get('current_day', 0)
    new_day = prev_day + (advance if num > wc.get('last_episode', 0) else 0)
    wc['current_day'] = new_day
    wc['last_episode'] = max(wc.get('last_episode', 0), num)

    # Timeline (replace any existing entry for this ep)
    canon['timeline'] = [t for t in canon.get('timeline', []) if t.get('ep') != num]
    canon['timeline'].append({
        'ep': num, 'day': new_day, 'events': upd.get('events', [])[:6]
    })
    canon['timeline'].sort(key=lambda t: t.get('ep', 0))

    # New facts
    fact_id_map = {}
    for nf in upd.get('new_facts', []):
        fid = _next_id('F', canon['facts'])
        canon['facts'].append({
            'id': fid, 'ep': num,
            'fact': nf.get('fact', '')[:240],
            'locked': True,
            'supersedes': nf.get('supersedes') or None,
        })

    # Character state
    cs = canon.setdefault('character_state', {})
    for name, ch_upd in (upd.get('character_updates') or {}).items():
        st = cs.setdefault(name, {'knows': [], 'suspects': [], 'physical': {}, 'location': None})
        # store learned facts as inline strings prefixed with episode (cheap, no fact-id matching)
        for learned in (ch_upd.get('learned') or [])[:5]:
            tag = f'ep{num}: {learned}'[:120]
            if tag not in st['knows']:
                st['knows'].append(tag)
        if ch_upd.get('physical'):
            st.setdefault('physical', {}).update(ch_upd['physical'])
        if ch_upd.get('location'):
            st['location'] = ch_upd['location']

    # Threads
    for t_open in upd.get('threads_opened', []):
        tid = _next_id('T', canon['open_threads'])
        canon['open_threads'].append({
            'id': tid, 'opened_ep': num,
            'question': t_open.get('question', '')[:240],
            'status': 'open',
        })
    closed_ids = set(upd.get('threads_closed') or [])
    for t in canon['open_threads']:
        if t.get('id') in closed_ids and t.get('status') != 'closed':
            t['status'] = 'closed'
            t['resolved_ep'] = num

    save_canon(sid, canon)
    return {'ok': True, 'world_day': new_day, 'new_facts': len(upd.get('new_facts', []))}


def rollback_canon_for_episode(sid, num):
    """Remove all canon entries created by episode `num` (used before regenerating)."""
    canon = load_canon(sid)
    canon['facts'] = [f for f in canon['facts'] if f.get('ep') != num]
    canon['timeline'] = [t for t in canon['timeline'] if t.get('ep') != num]
    canon['open_threads'] = [t for t in canon['open_threads'] if t.get('opened_ep') != num]
    for t in canon['open_threads']:
        if t.get('resolved_ep') == num:
            t.pop('resolved_ep', None)
            t['status'] = 'open'
    # Roll back character knowledge tagged with ep
    for name, st in (canon.get('character_state') or {}).items():
        st['knows'] = [k for k in st.get('knows', []) if not k.startswith(f'ep{num}:')]
    # Recompute world clock from remaining timeline
    if canon['timeline']:
        last = max(canon['timeline'], key=lambda t: t.get('ep', 0))
        canon['world_clock']['current_day'] = last.get('day', 0)
        canon['world_clock']['last_episode'] = last.get('ep', 0)
    else:
        canon['world_clock'] = {'current_day': 0, 'last_episode': 0}
    save_canon(sid, canon)


