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
from sw.routes.scripts_landmark import _series_beats_episode_block, trigger_autogen_if_enabled

@app.route('/api/series/<sid>/episodes/<int:num>/generate-script', methods=['POST'])
def generate_episode_script(sid, num):
    s = load_series(sid)
    if not s: return jsonify({'error': 'not found'}), 404
    ep = load_episode(sid, num)
    if not ep: return jsonify({'error': 'Episode not found'}), 404

    if num > 1:
        prev_ep = load_episode(sid, num - 1)
        if not prev_ep or not prev_ep.get('script', '').strip():
            return jsonify({'error': f'Generate script for episode {num - 1} first'}), 400
        prev_script = prev_ep['script']
    else:
        prev_script = ''

    next_ep = load_episode(sid, num + 1)
    next_synopsis = next_ep.get('synopsis', '') if next_ep else ''
    chars = s.get('characters', [])
    locs  = s.get('locations', [])
    cast_block = _build_cast_block(s, ep)

    batch = is_batch_mode(s)
    bs = batch_size(s)
    _is_finale = is_finale_episode(s, num)
    if batch:
        a, b = chunk_range(s, num)
        unit_label = f'CHUNK {num} (sub-episodes {a}–{b})'
        prev_a, prev_b = chunk_range(s, num - 1) if num > 1 else (0, 0)
        prev_label = f'PREVIOUS CHUNK {num-1} (sub-episodes {prev_a}–{prev_b})'
        next_label = f'NEXT CHUNK {num+1}'
    else:
        unit_label = f'EPISODE {num}'
        prev_label = f'PREVIOUS EPISODE {num-1}'
        next_label = f'NEXT EPISODE {num+1}'

    if prev_script:
        # Surface the FULL previous unit (no truncation) and additionally pull out its last beats
        # into a dedicated ENDING STATE block so the writer cannot accidentally ignore where the
        # previous scene was left.
        prev_lines = [ln for ln in prev_script.splitlines() if ln.strip() and not ln.strip().startswith('━')]
        # Drop the EPISODE NOTES / CHUNK NOTES tail if present
        for stop_kw in ('EPISODE NOTES', 'CHUNK NOTES'):
            if stop_kw in prev_lines:
                prev_lines = prev_lines[:prev_lines.index(stop_kw)]
        tail = '\n'.join(prev_lines[-25:])
        # Last ~10 dialogue lines specifically — these are most likely to be re-staged
        # by a writer who treats "continuation" as "rewind the climax".
        recent_dialogue = []
        for ln in reversed(prev_lines):
            if re.match(r'^\s*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-\']{0,30}:\s', ln.strip()):
                recent_dialogue.insert(0, ln.strip())
                if len(recent_dialogue) >= 10:
                    break
        forbidden_dialogue_block = ''
        if recent_dialogue:
            forbidden_dialogue_block = (
                f'\n═══ FORBIDDEN — DO NOT REPEAT OR PARAPHRASE THESE LINES ═══\n'
                f'These dialogue beats already happened in {prev_label}. The viewer SAW them. '
                f'You may NOT have any character say them again, paraphrase them, or re-deliver '
                f'the same threat / ultimatum / revelation in different words.\n'
                + '\n'.join(recent_dialogue)
                + f'\n═══════════════════════════════════════════════════\n'
            )
        prev_block = (
            f'{prev_label} SCRIPT (FULL — read it all):\n{prev_script}\n\n'
            f'═══ ENDING STATE OF {prev_label} — read this carefully ═══\n'
            f'These are the LAST beats of the previous unit. If they show a scene still in motion '
            f'(someone just arrived, a question hangs in the air, two characters mid-confrontation, '
            f'a reaction shot is the cliffhanger) — THIS unit MUST open in the SAME scene, SAME '
            f'location, SAME characters present, continuing dialogue from the very next beat. '
            f'Do NOT teleport to a new room or "later that day".\n'
            f'{tail}\n'
            f'═══════════════════════════════════════════════════\n'
            f'{forbidden_dialogue_block}\n'
            f'═══ NO PLOT-RECAP — HARD RULE ═══\n'
            f'"Continue from the next beat" means PUSH FORWARD, not REWIND. Events that '
            f'happened in {prev_label} (ultimatums delivered, threats made, revelations, '
            f'character entrances/exits, decisions stated out loud) are DONE. Do NOT have a '
            f'character re-issue the same ultimatum, re-state the same threat, or re-deliver '
            f'the same key line in slightly different words just to "remind" the audience. '
            f'A short drama viewer just watched {prev_label} 3 seconds ago — they remember. '
            f'If the cliffhanger of {prev_label} was "Claire demands he confess on camera or '
            f'she releases the files", THIS unit must show what happens NEXT — Cross\'s '
            f'reaction, Melissa\'s move, an arrival, a counter-twist — NOT Claire saying the '
            f'same demand again. RECAP-AS-OPENING IS A FAILURE STATE.\n'
            f'═══════════════════════════════════════════════════\n\n'
        )
    else:
        prev_block = f'This is the FIRST {("CHUNK" if batch else "EPISODE")} — no previous.\n\n'
    next_block = f'{next_label} SYNOPSIS (for continuity):\n{next_synopsis}\n\n' if next_synopsis else ''

    # ─── PRE-WRITE: rollback canon for this ep (in case of regeneration) and build logic brief
    rollback_canon_for_episode(sid, num)
    try:
        brief = build_logic_brief(sid, num)
    except Exception as e:
        brief = f'(brief unavailable: {e})'
    brief_block = f'═══ LOGIC CONSTRAINTS — MUST RESPECT ALL OF THESE ═══\n{brief}\n═══════════════════════════════════════════════════\n\n'
    try:
        devices_block = _build_plot_device_history(sid, num)
    except Exception:
        devices_block = ''
    try:
        narrative_block = _build_narrative_state_block(sid, num)
    except Exception:
        narrative_block = ''
    try:
        crowd_block = _build_crowd_constraint_block(s)
    except Exception:
        crowd_block = ''
    trajectory_block = build_trajectory_block(s, num)
    # Keep the ordered beat skeleton (ноды) steering the script — prepend so it
    # sits above the per-episode trajectory in the prompt. The opening episodes
    # march through the beats in order, then improvise (see
    # _series_beats_episode_block).
    _beats_block = _series_beats_episode_block(s)
    if _beats_block:
        trajectory_block = _beats_block + trajectory_block
    # Concrete plot bridge from THIS episode to the finale — generated by a quick Haiku
    # planning call. This is the load-bearing fix for "writer ignores the finale":
    # prev_script tends to dominate, so we explicitly tell the writer what THIS episode
    # must do to converge to the finale.
    try:
        bridge_data = build_finale_bridge_plan(s, num) or {}
    except Exception as _be:
        _log_event('WARN', 'bridge_plan_skip', err=str(_be)[:200])
        bridge_data = {}
    bridge_block = bridge_data.get('block', '')
    bridge_this_beat = (bridge_data.get('this_beat') or '').strip()
    bridge_state_before = (bridge_data.get('this_state_before') or '').strip()
    bridge_finale_ep = bridge_data.get('finale_ep')
    # Format-mode block (short_drama vs instagram_series) — drives ending style,
    # cliffhanger requirement, standalone-ness, pacing.
    series_format_mode = _format_mode_of(s)
    format_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])

    # Hard mandatory block for IG-series — every episode of every series MUST
    # open on a hook and close on a cliffhanger, no exceptions, regardless of
    # how soft the synopsis reads. Plus explicit ban on legal-procedural plot
    # engines (lawsuit / court / paperwork) that kill the pace of a 60s show.
    ig_hard_block = (
        '\n╔══════════════════════════════════════════════════════════════════════╗\n'
        '║ INSTAGRAM SERIES — HARD CONTRACT FOR THIS SCRIPT — VIOLATING = FAIL  ║\n'
        '╚══════════════════════════════════════════════════════════════════════╝\n'
        '1. COLD-OPEN HOOK (seconds 0-5) — MANDATORY. Drop the viewer into action / '
        '   conflict / question already in motion. Examples of valid opens:\n'
        '     • Mid-shout / mid-slap / mid-grab\n'
        '     • A line of dialogue that contains a reveal or accusation\n'
        '     • A door slamming open / a phone ringing repeatedly / a body on floor\n'
        '     • A character running mid-stride\n'
        '   FORBIDDEN opens: establishing shot of a building, slow zoom, narration, '
        '   "next morning", "two days later", small-talk warm-up, character making '
        '   coffee / sewing / journaling.\n'
        '2. ESCALATION — every dialogue exchange must reveal, flip, or raise stakes. '
        '   No filler. No "checking in" scenes. No tea-time chat. If a scene does not '
        '   move the plot, delete it.\n'
        '3. CLIFFHANGER on the LAST LINE / LAST FRAME — MANDATORY. Examples of valid '
        '   closes:\n'
        '     • A reveal / name spoken / face seen\n'
        '     • An arrival (door opens, car pulls up, knock)\n'
        '     • A phone showing a message / a photograph\n'
        '     • A weapon raised, a shot fired offscreen\n'
        '     • A line of dialogue that flips everything ("That\'s not my daughter.")\n'
        '   FORBIDDEN closes: character looking at sunset, journaling, smiling, '
        '   resolved hug, "everything will be okay", "and so they...", any wrap-up.\n'
        '4. DROP-IN FRIENDLY — assume the viewer never saw earlier episodes. Within '
        '   the first 10 seconds they must understand WHO this is and WHAT is at '
        '   stake through context (a costume cue, a line, a reaction). No exposition '
        '   dump, no "previously on", no narrator.\n'
        '5. BANNED PLOT ENGINES (none of these may drive the episode):\n'
        '     • lawsuits, court hearings, trials, depositions, judges, lawyers, '
        '       paralegals, legal motions, evidence binders\n'
        '     • eviction filings, paperwork submissions, signing contracts as climax\n'
        '     • tenant unions filing complaints, building inspectors arriving with forms\n'
        '   If the synopsis above leans on any of these — REWRITE the beat into a '
        '   FACE-TO-FACE confrontation, chase, betrayal, reveal, blackmail, escape, '
        '   or physical clash. People doing things to people. Not paperwork.\n'
        '6. DIALOGUE — short and clipped. Two-line clarity: one line states, the next '
        '   flips. No monologues. No "let me explain" speeches. No banter padding.\n'
        '7. LOCATIONS — vary as the plot demands. Do NOT lock the whole episode (or '
        '   the season) to one room. Move where the action goes.\n'
        '═══════════════════════════════════════════════════════════════════════\n\n'
    )

    if batch:
        a, b = chunk_range(s, num)
        if series_format_mode == 'instagram_series':
            # Self-contained chapters of a serialized mini-drama — each sub-ep ends
            # on its own cliffhanger; the chunk overall escalates into chunk+1.
            instruction = (
                ig_hard_block +
                f'Write the complete script for {unit_label}. '
                f'It must contain {bs} self-contained chapters (sub-eps {a} through {b}), each ending with the EXACT cut marker '
                f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
                f'Each sub-episode = SHARP cold-open hook (first 3-5s) → escalating conflict → cliffhanger turn at the end. '
                f'Apply ALL 7 rules of the HARD CONTRACT to every single sub-episode in this chunk. '
                f'The chunk overall ends on the sharpest cliffhanger pulling into chunk {num+1}. '
            )
        else:
            instruction = (
                f'Write the complete script for {unit_label}. '
                f'It must contain {bs} sub-episodes (sub-eps {a} through {b}), each ending with the EXACT cut marker '
                f'`═══ END EPISODE X/{bs} — CLIFFHANGER: <type> ═══` on its own line. '
                f'The chunk overall has its own midpoint REVERSAL and a final cliffhanger that leads into chunk {num+1}. '
            )
    else:
        if series_format_mode == 'instagram_series':
            instruction = (
                ig_hard_block +
                f'Write the complete script for Episode {num}. '
                + ('Open on a SHARP COLD-OPEN HOOK in the first 3-5 seconds — drop us into action / conflict / line-mid-confrontation. NO setup, NO establishing shot. ' if not prev_script else
                   'Open on a SHARP COLD-OPEN HOOK tied to where the story left off — drop us back in mid-action. A fresh viewer must catch up in 10 seconds through context, NOT exposition. ')
                + 'Escalate through clipped dialogue — every line reveals, flips, or raises stakes. '
                + ('This is the SERIES FINALE — resolve every thread and end on a CONCLUSIVE final beat (see the FINALE CONTRACT below). Do NOT end on a cliffhanger and ignore HARD CONTRACT rule 3. '
                   if _is_finale else
                   'End on a HARD CLIFFHANGER on the final line / frame. Apply ALL 7 rules of the HARD CONTRACT above.')
            )
        else:
            # Length budget — series-level target_duration_sec (default 60s).
            try:
                _target_sec = int(s.get('target_duration_sec') or 60)
            except (TypeError, ValueError):
                _target_sec = 60
            # Spoken words ≈ 100 words per 60s of TikTok-paced drama (135 wpm
            # speaking rate w/ pauses). Floor at 75% of target — model was
            # observed undershooting to ~50 spoken words on a 100-word target
            # because the prior phrasing was «MAXIMUM, never exceed, cut
            # mercilessly». Now framed as a RANGE the model must HIT, not a
            # ceiling to fear.
            _spoken_target = round(_target_sec / 60 * 100)
            _spoken_floor = round(_spoken_target * 0.75)
            _spoken_ceiling = round(_spoken_target * 1.10)
            # Dialogue lines (each NAME: line) — separate from action.
            _dlg_target = max(3, min(40, round(_target_sec / 4.5)))
            _dlg_floor = max(3, round(_dlg_target * 0.75))
            _dlg_ceiling = _dlg_target + 3
            _action_budget = max(3, round(_target_sec / 12))
            instruction = (
                f'Write the complete script for Episode {num}. '
                + ('Continue naturally from where Episode {prev} ended.'.format(prev=num-1) if prev_script else 'Hook the viewer immediately.')
                + (' This is the SERIES FINALE — resolve every open thread and end conclusively (see the FINALE CONTRACT below); do NOT end on a cliffhanger. ' if _is_finale else ' End on a cliffhanger. ')
                + f'\n\n⚠ БЮДЖЕТ ДЛИНЫ — серия ≈ {_target_sec} секунд экрана.\n'
                + f'\n📣 РЕЧЕВЫЕ СЛОВА (только то, что произносят персонажи — слова после «NAME:» / в кавычках):\n'
                + f'• ЦЕЛЬ: {_spoken_target} слов. ДИАПАЗОН: {_spoken_floor}–{_spoken_ceiling}.\n'
                + f'• НЕ НЕДОБИРАЙ ниже {_spoken_floor} — иначе сцена пустая, аудио короче нужного.\n'
                + f'• НЕ ПРЕВЫШАЙ {_spoken_ceiling} — иначе сцена не влезет в {_target_sec}с.\n'
                + f'• Каждая реплика: 3-8 слов в среднем, максимум 10. >10 слов → РАЗБЕЙ на две короткие реплики.\n'
                + f'• МОНОЛОГОВ НЕТ. Короткие рваные удары. Никаких речей «Your Honor, I would like to explain…».\n'
                + f'\n💬 РЕПЛИКИ (количество строк диалога):\n'
                + f'• ЦЕЛЬ: {_dlg_target} строк диалога. ДИАПАЗОН: {_dlg_floor}–{_dlg_ceiling}.\n'
                + f'\n🎬 ACTION / BLOCKING (описание движения, [BLOCKING] блоки, ремарки):\n'
                + f'• Это ОТДЕЛЬНЫЙ бюджет — НЕ СЧИТАЕТСЯ как речевые слова.\n'
                + f'• Максимум ~{_action_budget} action-строк. Описывай только ключевое движение, не каждое мелкое.\n'
                + f'• [BLOCKING] / [BLOCKING_END] блоки пиши столько сколько нужно — они вне счёта.\n'
                + f'• ЗАПРЕЩЕНЫ time-markers в action-lines: «слушает две минуты молча», «молчит десять секунд», '
                + f'«проходит минута» — это раздувает хронометраж экрана.\n'
                + f'\n✅ ПРОВЕРЬ ПЕРЕД ВЫВОДОМ:\n'
                + f'• Речевых слов в диапазоне {_spoken_floor}–{_spoken_ceiling}? (action и BLOCKING НЕ В СЧЁТ)\n'
                + f'• Строк диалога в диапазоне {_dlg_floor}–{_dlg_ceiling}?\n'
                + f'• Action-строк не больше {_action_budget}?\n'
                + f'• Это короткая драма для TikTok/Reels — НЕ полнометражный сценарий, но и НЕ обрубок на 50 слов.\n'
                + f'• Программный детектор проверит спикерские слова + длину каждой реплики; нарушения → автоматическая перезапись.'
            )

    base_prompt = (
        f'Series: "{s["title"]}" | Genre: {s.get("genre","")} | Tone: {s.get("tone","")}\n'
        f'Series arc: {s.get("arc","")}\n\n'
        + _anthro_world_block(s)  # ← furry/anthro world directive (empty for human worlds)
        + _revision_instructions_block(s)  # ← clone-time revisions (empty unless cloned w/ edits)
        + trajectory_block        # ← user-pinned finale + checkpoints, FIRST so it's impossible to miss
        + bridge_block            # ← concrete per-episode plan from current state to finale
        + crowd_block             # ← HARD limit on characters per scene, lifted up front
        + format_block
        + (cast_block + '\n\n' if cast_block else '')
        + brief_block
        + devices_block
        + prev_block
        + f'{unit_label} SYNOPSIS (may be stale — see THIS EPISODE\'S BEAT below):\n{ep.get("synopsis","")}\n\n'
        + (
            (
                f'╔══════════════════════════════════════════════════════════════════╗\n'
                f'║ 🎯 THIS EPISODE\'S BEAT — execute exactly this, override synopsis ║\n'
                f'╚══════════════════════════════════════════════════════════════════╝\n'
                + (f'СОСТОЯНИЕ МИРА В НАЧАЛЕ ЭТОЙ СЕРИИ (Ep {num}):\n  {bridge_state_before}\n\n'
                   if bridge_state_before else '')
                + (f'TEMPORAL ANCHOR: финальные события (Ep {bridge_finale_ep or "—"}) ЕЩЁ НЕ ПРОИЗОШЛИ. '
                   f'Не пиши сцены так будто кто-то уже арестован/осуждён/мёртв, если по плану это случится позже.\n\n'
                   if bridge_finale_ep else '')
                + f'Эта серия (Ep {num}) ОБЯЗАНА выполнить следующий beat из плана-моста к финалу:\n\n'
                f'  ▶▶▶ {bridge_this_beat}\n\n'
                f'IF THE SYNOPSIS ABOVE CONTRADICTS THIS BEAT — THE BEAT WINS. The synopsis may have\n'
                f'been generated before the finale was pinned and is now stale. The beat above is\n'
                f'the authoritative instruction. Write the script to deliver THIS BEAT.\n'
                f'Do NOT invent new named characters not in the cast.\n'
                f'Do NOT introduce a subplot that the bridge plan does not include.\n'
                f'Do NOT depict characters in their FINALE-state (arrested, sentenced, exposed) — '
                f'use their CURRENT state from the СОСТОЯНИЕ МИРА block above.\n'
                f'═══════════════════════════════════════════════════════════════════\n\n'
            ) if bridge_this_beat else ''
        )
        + next_block
        + narrative_block
        + instruction
        + ' EVERY constraint in the LOGIC CONSTRAINTS block above is mandatory — violating canon is a hard fail. '
        + 'And EVERY landmark in the NARRATIVE TRAJECTORY block at the top is mandatory — drift from the finale is a hard fail. '
        + ('The THIS EPISODE\'S BEAT above is the authoritative scene direction — execute it. '
           'If the synopsis or prev_script set up a different subplot, fold it into the beat or park it; '
           'NEVER continue a subplot the bridge plan does not include.' if bridge_this_beat else '')
        # ← SERIES FINALE override: appended LAST so it trumps the cliffhanger
        #   mandate above. Empty string for every non-finale episode.
        + build_finale_contract_block(s, num)
    )

    script_system = _build_batch_script_system(s) if batch else _build_script_system(s)

    try:
        # ─── WRITE → AUDIT → RETRY loop (max 2 retries, fully automated)
        MAX_RETRIES = 2
        script = ''
        audit_report = {'passes': True, 'violations': [], 'retries': 0}
        prompt = base_prompt
        for attempt in range(MAX_RETRIES + 1):
            script = claude_ask(prompt, system=script_system)
            report = audit_script(sid, num, script, brief)
            # Run logic-hole auditor in parallel with continuity auditor — different concerns.
            logic_report = audit_logic_holes(sid, num, script)
            # Programmatic check for scene overcrowding — reliable signal that Claude-based
            # auditor sometimes misses. We count NAMED-CAST speakers per scene and flag any
            # scene that exceeds the user's max_main_chars_per_scene setting.
            crowd_violations = detect_scene_overcrowding(s, script)
            crowd_critical = []
            for cv in crowd_violations:
                names_str = ', '.join(cv['characters'])
                crowd_critical.append({
                    'type': 'scene_overcrowding',
                    'severity': 'critical',
                    'where': f"сцена {cv['scene_idx']} ({cv['location']})",
                    'explanation': (
                        f"в сцене {cv['count']} именованных персонажа из каста "
                        f"({names_str}), а лимит сериала = {cv['limit']}. "
                        f"Программный детектор посчитал диалоговые реплики и BLOCKING-разметку."
                    ),
                    'fix': (
                        f"Перепиши сцену так чтобы говорящих/действующих именованных персонажей "
                        f"было НЕ БОЛЕЕ {cv['limit']}. Варианты: (a) убери из сцены лишних персонажей "
                        f"(они могут появиться в ОТДЕЛЬНОЙ последовательной сцене); "
                        f"(b) разбей сцену на две — сначала одна группа, потом другая входит после ухода первой; "
                        f"(c) оставь лишних только в фоне без реплик и без [BLOCKING] упоминания."
                    ),
                })
            if crowd_critical:
                print(f'[scene-crowd] ep {num} attempt {attempt+1}: {len(crowd_critical)} overcrowded scene(s) detected programmatically', flush=True)
                for cv in crowd_violations:
                    print(f'[scene-crowd]   scene {cv["scene_idx"]} ({cv["location"]}): {cv["count"]}>{cv["limit"]} — {", ".join(cv["characters"])}', flush=True)
            # Programmatic no-name-character check — every on-camera/speaking character
            # MUST have a unique proper name + cast-block line so its reference binds.
            # Bare role cues (CLIENT, OLD WOMAN) and uncast named speakers force a rewrite.
            naming_critical = detect_unnamed_characters(s, script)
            if naming_critical:
                print(f'[name-check] ep {num} attempt {attempt+1}: {len(naming_critical)} unnamed/uncast character(s) — '
                      + ', '.join(v['where'] for v in naming_critical), flush=True)
            # ── Programmatic over-length detector — count dialogue lines and spoken words,
            # estimate runtime, fail if >30% over the target duration.
            length_critical = []
            length_violation = detect_script_overlength(s, script)
            if length_violation:
                lv = length_violation
                reasons_str = '; '.join(lv.get('reasons') or [])
                # Branch the fix message based on direction. Undershoot
                # (writer delivered too few spoken words) gets a different
                # corrective than overshoot (writer was too verbose).
                _floor_words = round(lv['target_words'] * 0.75)
                # Undershoot = rendered runtime falls below target (the renderer
                # would produce a clip shorter than the series setting). Word
                # count is the secondary signal.
                _undershoot = (lv['est_sec'] < lv['target_sec'] * 0.9) or (lv['dialogue_words'] < _floor_words)
                if _undershoot:
                    fix_msg = (
                        f"ДОПИШИ диалог до целевого объёма. Конкретно: "
                        f"(a) у тебя сейчас {lv['dialogue_words']} спикерских слов, нужно ~{lv['target_words']} "
                        f"(минимум {_floor_words}) — НЕДОБОР почти в два раза; "
                        f"(b) добавь {lv['target_words'] - lv['dialogue_words']}+ спикерских слов через "
                        f"новые короткие реплики (3-7 слов каждая), НЕ через длинные монологи; "
                        f"(c) ACTION/BLOCKING НЕ СЧИТАЮТСЯ — речь только то, что произносят персонажи "
                        f"после «NAME:» или в кавычках; "
                        f"(d) сцена развивается через диалог — добавь обмены репликами, реакции, "
                        f"подколы, угрозы, признания. НЕ через action-описания «он смотрит на неё»; "
                        + (f"(e) это ФИНАЛ — концовка остаётся конклюзивной (без клиффхэнгера), но к ней ведёт больше реплик."
                           if _is_finale else
                           f"(e) cliffhanger остаётся, но к нему ведёт больше реплик.")
                    )
                else:
                    fix_msg = (
                        f"СОКРАТИ беспощадно. Конкретно: "
                        f"(a) каждая реплика МАКСИМУМ 7 слов, идеал 3-5 (короткие рваные удары); "
                        f"(b) если есть монолог >10 слов — разрежь на короткие реплики ИЛИ удали лишнее; "
                        f"(c) action-строк не больше {max(3, round(lv['target_sec']/12))} — убери все 'смотрит / встаёт / делает паузу' если они не двигают сцену; "
                        f"(d) общий лимит: ~{lv['target_words']} спикерских слов, ~{lv['target_lines']} диалоговых строк суммарно; "
                        f"(e) НИКАКИХ time-markers вроде 'в течение двух минут' / 'десять секунд молча' — это раздувает хронометраж; "
                        + (f"(f) удали экспозицию и повторы — только живые удары + конклюзивная развязка ФИНАЛА (без клиффхэнгера). "
                           if _is_finale else
                           f"(f) удали экспозицию и повторы — только живые удары + cliffhanger. ")
                        + f"Это короткая драма для TikTok ({lv['target_sec']}с), не полнометражный сценарий."
                    )
                length_critical.append({
                    'type': 'script_overlength',
                    'severity': 'critical',
                    'where': 'весь сценарий серии',
                    'explanation': (
                        f"сценарий нарушает бюджет длины: {reasons_str}. "
                        f"Метрики: {lv['dialogue_lines']} реплик · {lv['dialogue_words']} слов · "
                        f"{lv['action_lines']} action-строк · самая длинная реплика {lv['longest_line_words']} слов · "
                        f"средняя {lv['avg_line_words']} слов · оценка ~{lv['est_sec']}с экрана. "
                        f"Лимит сериала: {lv['target_sec']}с, ~{lv['target_words']} слов, ~{lv['target_lines']} строк."
                    ),
                    'fix': fix_msg,
                })
                print(f'[script-length] ep {num} attempt {attempt+1}: {lv["est_sec"]}s ({lv["ratio"]}×), '
                      f'lines={lv["dialogue_lines"]} words={lv["dialogue_words"]} '
                      f'longest={lv["longest_line_words"]} avg={lv["avg_line_words"]} actions={lv["action_lines"]} '
                      f'reasons=[{reasons_str}]', flush=True)
            all_violations = list(report.get('violations', [])) + list(logic_report.get('violations', [])) + crowd_critical + length_critical + naming_critical
            critical = [v for v in all_violations if v.get('severity') == 'critical']
            audit_report = {
                'passes': not critical,
                'violations': all_violations,
                'retries': attempt,
                'audit_error': report.get('audit_error') or logic_report.get('audit_error'),
                'logic_passes': logic_report.get('passes', True),
                'continuity_passes': report.get('passes', True),
                'crowd_violations': crowd_violations,
            }
            if not critical:
                break
            # Build a fix-it prompt and retry — group violations by source for clarity
            cont_fixes = [v for v in critical if v.get('type') in ('timeline','fact','knowledge','biology','setup','paperwork','scene_teleport')]
            logic_fixes = [v for v in critical if v.get('type') in ('status','hidden_position','enabling_condition','legal_term','unmotivated_delay','ambiguous_cliffhanger','protagonist_stagnation','emotional_monotony','scene_overcrowding','finale_drift','script_overlength','unnamed_character','uncast_character')]
            fixes_parts = []
            if cont_fixes:
                fixes_parts.append('CONTINUITY/CANON ISSUES:\n' + '\n'.join(
                    f"- [{v.get('type','?')}] {v.get('explanation','')} → FIX: {v.get('fix','')}" for v in cont_fixes))
            if logic_fixes:
                fixes_parts.append('STORY-LOGIC HOLES:\n' + '\n'.join(
                    f"- [{v.get('type','?')}] WHERE: {v.get('where','?')} | {v.get('explanation','')} → FIX: {v.get('fix','')}" for v in logic_fixes))
            prompt = (
                base_prompt
                + '\n\n═══ PREVIOUS DRAFT FAILED AUDIT ═══\n'
                + 'You MUST address every issue below. Do not repeat the same mistakes.\n'
                + '\n\n'.join(fixes_parts)
                + '\n═══════════════════════════════════════════════════\n'
                + 'Rewrite the entire script from scratch fixing ALL issues above while still respecting the LOGIC CONSTRAINTS.'
            )

        # ─── Save script + audit/brief metadata first
        # If there was a previous script, archive it before overwriting
        prev_script_text = (ep.get('script') or '').strip()
        if prev_script_text:
            ep.setdefault('script_history', []).append({
                'ts': time.time(),
                'reason': 'regenerate',
                'script': prev_script_text[:80000],
            })
            ep['script_history'] = ep['script_history'][-10:]
        ep['script'] = _normalize_blocking_tags(script)
        script = ep['script']   # use normalized version downstream
        ep['status'] = 'draft'
        ep['logic_brief'] = brief
        ep['audit_report'] = audit_report
        # Store end_position for continuity context (used by _prev_episode_ending_context)
        end_pos = _extract_end_position(script)
        if end_pos:
            ep['end_position'] = end_pos
        save_episode(sid, num, ep)
        # Extract and register plot devices + narrative state for anti-repetition tracking
        try:
            gen_devices = _extract_devices_from_script(script)
            if gen_devices:
                ep['plot_devices'] = gen_devices
                save_episode(sid, num, ep)
                _update_devices_index(sid, num, gen_devices)
        except Exception as _de:
            _log_event('WARN', 'device_extract_after_gen_failed', err=str(_de)[:200])
        try:
            gen_narrative = _extract_narrative_state_from_script(script)
            if gen_narrative:
                ep['narrative_state'] = gen_narrative
                save_episode(sid, num, ep)
                _update_narrative_index(sid, num, gen_narrative)
        except Exception as _ne:
            _log_event('WARN', 'narrative_extract_after_gen_failed', err=str(_ne)[:200])
        # Sync outfits from SCENE_OPEN blocks (non-blocking — failures are logged)
        try:
            _sync_script_outfits(sid, script)
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_gen_failed', err=str(_oe)[:200])

        # NOTE: cast-block sync is INTENTIONALLY NOT run after script-gen.
        # User wants explicit control — they'll click "🤖 Извлечь персонажей и локации"
        # to trigger /extract-characters when ready. Mark the episode so the auto-heal
        # paths (get_series, autogen pre-sweep) skip it until extraction happens.
        ep['cast_extracted'] = False
        save_episode(sid, num, ep)
        sync_result = {'created_characters': [], 'created_outfits': [], 'created_locations': [],
                       'detected_locations': [], 'healed_refs': 0,
                       'characters_used': ep.get('characters_used', []),
                       'character_outfits': ep.get('character_outfits', {}),
                       'locations_used': ep.get('locations_used', [])}
        print(f'[script-gen {sid}/ep{num}] script saved, cast extraction pending — user must click extract', flush=True)

        # Fallback: scan script text for location names (flexible match)
        s = load_series(sid)  # reload to get the synced state
        ep = load_episode(sid, num)
        locs = s.get('locations', [])
        script_upper = script.upper()
        def loc_in_script(name):
            u = name.upper()
            if u in script_upper: return True
            no_article = re.sub(r'^(THE|AN?)\s+', '', u)
            if no_article != u and no_article in script_upper: return True
            words = [w for w in u.split() if len(w) > 2]
            return bool(words) and all(w in script_upper for w in words)
        detected_locs = [l['id'] for l in locs if loc_in_script(l['name'])]
        ep['locations_used'] = list(set(ep.get('locations_used', [])) | set(detected_locs))
        save_episode(sid, num, ep)

        # ─── POST-WRITE: extract canon updates from finalized script
        try:
            extract_result = extract_canon_updates(sid, num, script)
        except Exception as e:
            extract_result = {'error': str(e)}

        # Append to canon audit log
        try:
            canon = load_canon(sid)
            canon.setdefault('audit_log', []).append({
                'ep': num,
                'ts': time.time(),
                'passes': audit_report['passes'],
                'retries': audit_report['retries'],
                'violations': [
                    {'type': v.get('type'), 'severity': v.get('severity'), 'explanation': v.get('explanation')}
                    for v in audit_report['violations']
                ],
            })
            canon['audit_log'] = canon['audit_log'][-100:]  # cap
            save_canon(sid, canon)
        except Exception:
            pass

        # Auto-generate any newly-introduced character/outfit/location images in background
        trigger_autogen_if_enabled(sid)
        return jsonify({
            'script': script,
            'characters_used':  ep['characters_used'],
            'character_outfits': ep['character_outfits'],
            'locations_used':   ep['locations_used'],
            'logic_brief': brief,
            'audit_report': audit_report,
            'canon_update': extract_result,
            'series': s,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
