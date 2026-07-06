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
from sw.routes.import_series_worker import _import_worker, _split_script_into_episodes

@app.route('/api/series/<sid>/analyze-more-episodes', methods=['POST'])
def analyze_more_episodes(sid):
    """Re-analyze the source drama's NEXT episode range and append the beats to the
    series' source_episode_outline, so the series can be continued past its original
    5-episode outline. Generation itself is unchanged: once the outline covers episodes
    6-10, generate_script_batch + _source_outline_episode_block pick them up automatically.

    Body: {count?: int (1-5, default 5)}
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    src = s.get('source_drama') or {}
    if not (src.get('id') or src.get('title')):
        return jsonify({'error': 'no source drama linked'}), 400
    body = request.json or {}
    count = max(1, min(5, int(body.get('count') or 5)))
    outline = [str(x) for x in (s.get('source_episode_outline') or [])]
    analyzed_through = int(src.get('analyzed_through') or len([x for x in outline if x.strip()]))
    ep_from = analyzed_through + 1
    ep_to = ep_from + count - 1
    # Context = the beats already laid out, so the continuation stays coherent.
    context = '\n'.join(f'Эп.{i+1}: {b}' for i, b in enumerate(outline) if b.strip())
    from sw.routes.ideas_dramas import _analyze_drama_range
    try:
        beats = _analyze_drama_range(src, ep_from, ep_to, context=context)
    except Exception as e:
        _log_event('WARN', 'analyze_more_episodes_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
    if not beats:
        return jsonify({'error': 'analysis returned no episodes'}), 502
    # Pad so index i → episode i+1 stays aligned, then append the new beats.
    while len(outline) < ep_from - 1:
        outline.append('')
    outline.extend(beats)
    ep_to = ep_from + len(beats) - 1
    s['source_episode_outline'] = outline
    src['analyzed_through'] = ep_to
    s['source_drama'] = src
    save_series(sid, s)
    return jsonify({'range': [ep_from, ep_to], 'beats': beats, 'analyzed_through': ep_to})


@app.route('/api/series/<sid>/generate-script-batch', methods=['POST'])
def generate_script_batch(sid):
    """Generate N new episodes for an existing series. Returns the generated
    text as ONE multi-episode script (with «Episode N:» headers) ready to be
    pasted into the append-flow textarea. After generation user can preview-
    split, logic-check, fix, and commit via /append-from-script.

    Body: {count: int, direction?: str}
      - count: how many episodes to write (1-20)
      - direction: optional plot-direction hint
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    body = request.json or {}
    count = max(1, min(20, int(body.get('count') or 5)))
    direction = (body.get('direction') or '').strip()
    # Optional advanced parameters — empty/None = Claude decides.
    duration_sec_raw = body.get('duration_sec')
    lines_count_raw  = body.get('lines_count')
    try:
        duration_sec = int(duration_sec_raw) if duration_sec_raw not in (None, '', 0) else None
        if duration_sec is not None: duration_sec = max(30, min(240, duration_sec))
    except (TypeError, ValueError):
        duration_sec = None
    try:
        lines_count = int(lines_count_raw) if lines_count_raw not in (None, '', 0) else None
        if lines_count is not None: lines_count = max(3, min(40, lines_count))
    except (TypeError, ValueError):
        lines_count = None
    style_preset = (body.get('style') or '').strip()
    no_interruptions = bool(body.get('no_interruptions', True))  # default: interruptions forbidden
    max_chars_raw = body.get('max_main_chars_per_scene') or s.get('max_main_chars_per_scene')
    try:
        max_main_chars = int(max_chars_raw) if max_chars_raw not in (None, '', 0) else None
        if max_main_chars is not None: max_main_chars = max(1, min(6, max_main_chars))
    except (TypeError, ValueError):
        max_main_chars = None
    # Persist to series so individual episode generation picks it up too
    if max_main_chars and s.get('max_main_chars_per_scene') != max_main_chars:
        s['max_main_chars_per_scene'] = max_main_chars
        save_series(sid, s)
    # Style presets translated to Claude-friendly directives.
    _STYLE_PRESETS = {
        'short_punchy': 'Реплики КОРОТКИЕ и рваные (1-7 слов). TikTok-ритм: быстрые удары, шок-фразы, paus'
                        'е через действие. Никаких длинных монологов. Цель — макс эмоциональная плотность.',
        'balanced':     'Реплики СБАЛАНСИРОВАННЫЕ (5-15 слов). Средний темп. Можно изредка длинные эмоциональные '
                        'удары, основное — короткие.',
        'long_meaty':   'Реплики ДЛИННЫЕ и насыщенные (10-25 слов). Эмоциональные монологи, развёрнутые откровения, '
                        'весомые угрозы. Подходит для драматических кульминаций.',
    }
    style_clause = _STYLE_PRESETS.get(style_preset, '')

    # Pull existing episodes for context. Cap content to keep prompt sane:
    # last 8 episodes verbatim, earlier ones as synopsis-only.
    # `from_scratch` mode kicks in when the series has no episodes yet — we
    # write from Эп.1 using ONLY the series bible (title, genre, tone,
    # audience, world, roster) + user-provided direction.
    existing = sorted([e for e in list_episodes(sid) if e.get('script')], key=lambda e: e.get('number', 0))
    from_scratch = not existing
    if from_scratch:
        last_num = 0
    else:
        last_num = existing[-1].get('number', 0)
    first_new_num = last_num + 1
    last_new_num = last_num + count

    # Context window (skipped in from-scratch mode — no prior episodes)
    verbatim_window = existing[-8:] if not from_scratch else []
    earlier = existing[:-8] if not from_scratch else []
    earlier_block = ''
    if earlier:
        earlier_lines = []
        for e in earlier:
            syn = (e.get('synopsis') or '').strip()[:200]
            if not syn:
                syn = (e.get('script') or '')[:200].replace('\n', ' ').strip()
            earlier_lines.append(f"Эп.{e.get('number')}: {syn}")
        earlier_block = "СИНОПСИСЫ РАННИХ СЕРИЙ (краткий контекст):\n" + '\n'.join(earlier_lines) + '\n\n'

    verbatim_block = '\n\n'.join(
        f"=== Эп.{e.get('number')}: {(e.get('title') or '').strip()} ===\n{(e.get('script') or '')[:6000]}"
        for e in verbatim_window
    ) if verbatim_window else ''

    # Roster of known entities so the generated script reuses them by name.
    chars_list = ', '.join(c.get('name', '') for c in (s.get('characters') or []) if c.get('name'))[:1000]
    locs_list  = ', '.join(l.get('name', '') for l in (s.get('locations') or []) if l.get('name'))[:1000]
    items_list = ', '.join(it.get('name', '') for it in (s.get('items') or []) if it.get('name'))[:1000]

    if from_scratch:
        # From-scratch needs a real direction OR a decent synopsis — without
        # either we're writing fanfic with no idea what the series is about.
        if not direction and not (s.get('synopsis') or '').strip():
            return jsonify({
                'error': 'У сериала нет ни синопсиса в Bible, ни направления от тебя — '
                         'не из чего писать первую серию. Заполни синопсис в Bible '
                         'или укажи направление сюжета в поле «Куда сюжет идёт дальше».'
            }), 400
        direction_block = (
            f"\nЭТО СТАРТ СЕРИАЛА — пишешь С НУЛЯ, Эп.1–{count}.\n"
            f"СИНОПСИС / IDEA СЕРИАЛА:\n{(s.get('synopsis') or '').strip() or '(не задан в Bible)'}\n"
        )
        if direction:
            direction_block += f"\nНАПРАВЛЕНИЕ ОТ ПОЛЬЗОВАТЕЛЯ:\n{direction}\n"
        direction_block += (
            "\nТРЕБОВАНИЯ К ПИЛОТУ (Эп.1):\n"
            "- Открой сериал hook'ом за первые 5 секунд (визуальный шок, провокационная фраза, "
            "  острый конфликт). НЕ начинай с экспозиции.\n"
            "- Представь главных героев через действие, не через рассказ о них.\n"
            "- Заложи центральный конфликт + 1-2 побочные сюжетные линии для будущих серий.\n"
            "- Финал пилота — мощный cliffhanger, после которого хочется смотреть Эп.2.\n"
        )
        if count > 1:
            direction_block += (
                "\nТРЕБОВАНИЯ К ДУГЕ:\n"
                f"- За {count} серий построй полный мини-арк: пилот → нарастающие осложнения → "
                "точка невозврата → кульминация → финал последней серии (либо завершение арки, "
                "либо большой cliffhanger для продолжения).\n"
                "- Каждая серия развивает не менее одной сюжетной линии. Не дублируй конфликты.\n"
            )
    elif direction:
        direction_block = f"\nЖЕЛАЕМОЕ НАПРАВЛЕНИЕ СЮЖЕТА ОТ ПОЛЬЗОВАТЕЛЯ:\n{direction}\n"
    else:
        direction_block = (
            "\nПОЛЬЗОВАТЕЛЬ НЕ УКАЗАЛ НАПРАВЛЕНИЕ — придумай развитие сам, опираясь на открытые сюжетные линии "
            "из последних серий, нерешённые загадки, и эмоциональные арки персонажей. Не повторяй уже произошедшее.\n"
        )

    mode_label = 'старт сериала с нуля' if from_scratch else 'продолжение существующего сериала'
    # Compute effective length / lines targets. Speech delivery ≈ 3.8 wps
    # (matches the SPEECH_WPS calibration in the segmenter). Default episode
    # = ~60s ≈ 12-15 lines (user-tunable). If user specifies one but not the
    # other, we derive a sensible default for the missing one so Claude has
    # a coherent target.
    eff_duration = duration_sec if duration_sec else 60
    if lines_count:
        eff_lines = lines_count
    else:
        # Roughly: 1 line ≈ 4-5s of screen (dialogue + action beat). So a
        # 60s episode ~ 12-15 lines; 90s ~ 18-22; 30s ~ 6-8.
        eff_lines = max(3, min(40, round(eff_duration / 4.5)))
    lines_range_word = (
        f"{max(3, eff_lines-2)}-{eff_lines+2}"  # ±2 wiggle so Claude isn't pinned to exact number
    )
    # ── SPOKEN-WORD BUDGET (the real driver of episode runtime & chunk count) ──
    # The renderer (static/app.js segmenter — DO NOT TOUCH) packs the script into
    # ~14-15s Seedance chunks driven almost entirely by SPOKEN WORDS @2.65 wps.
    # A 60s episode needs ~100-110 spoken words to fill 4+ chunks. The batch path
    # historically framed length as a CEILING only ("≤N lines, режь беспощадно")
    # with NO word floor — so the writer skimped (e.g. 9 dialogue lines / 51
    # words → ~50s → only 2-3 chunks). We now give an explicit FLOOR tied to the
    # ≥4-chunks-per-minute requirement, separate from the action-line budget.
    eff_spoken_target  = round(eff_duration / 60 * 110)        # words actually spoken
    eff_spoken_floor   = round(eff_duration / 60 * 95)         # hard floor — below this = too few chunks
    eff_spoken_ceiling = round(eff_duration / 60 * 125)        # don't overflow the minute
    eff_dlg_lines      = eff_lines                              # DIALOGUE lines only (action excluded)
    eff_dlg_floor      = max(4, round(eff_dlg_lines * 0.8))
    eff_dlg_ceiling    = eff_dlg_lines + 3
    eff_action_budget  = max(3, round(eff_duration / 12))      # action lines — SEPARATE budget
    eff_min_chunks     = max(4, round(eff_duration / 15))      # renderer ≈ 1 chunk / 14-15s
    length_clause = (
        f"Каждая серия ≈ {eff_duration}с экрана: ~{eff_spoken_target} ПРОИЗНЕСЁННЫХ слов "
        f"(диапазон {eff_spoken_floor}–{eff_spoken_ceiling}), ~{eff_dlg_lines} реплик диалога, "
        f"+ до {eff_action_budget} action-строк. Это НИЖНЯЯ планка тоже — не недобирай, "
        f"иначе серия порежется всего на 2-3 чанка вместо нужных {eff_min_chunks}+. "
    )
    style_block = (f"\nСТИЛЬ РЕПЛИК: {style_clause}\n" if style_clause else '')
    # Scene-character cap directive. NOT about total cast size — about how
    # many MAIN characters actively drive any given scene. Crowds/extras
    # don't count. Default is 2, max 4 only for emotional climaxes.
    if max_main_chars:
        if max_main_chars == 1:
            crowd_clause = 'ОДИН главный персонаж на сцену (моно-сцены). Изредка может быть второй на короткую реплику.'
        elif max_main_chars == 2:
            crowd_clause = '2 главных персонажа в большинстве сцен (диалог). Изредка 3 на ключевые моменты. Никаких сцен где 4+ главных героев постоянно обсуждают.'
        else:
            crowd_clause = f'В большинстве сцен 2 главных персонажа, изредка 3, МАКСИМУМ {max_main_chars} ТОЛЬКО для эмоциональной кульминации (откровение, конфронтация всей семьи). Не делай сцен где {max_main_chars} главных героев постоянно мусолят одно — это вяло.'
        crowd_block = (
            f"\nЛИМИТ ПЕРСОНАЖЕЙ В СЦЕНЕ (главных): {max_main_chars}.\n"
            f"{crowd_clause}\n"
            "ВАЖНО — это НЕ запрет на массовку: сцены на свадьбе, вечеринке, "
            "митинге, в зале суда МОГУТ иметь толпу фоновых персонажей. Лимит "
            "только на основных героев которые активно ведут сцену (имеют реплики/действия).\n"
        )
    else:
        crowd_block = ''
    # HARD numeric caps + banned soap-opera tropes. Without this Claude
    # defaults to «voiceover-narrated paperwork-reveal» style and triples the
    # line count. Mirrors the DIALOGUE-FIRST rule from _IDEAS_SYSTEM but
    # applied at the script-writing stage where it actually constrains output.
    if eff_duration <= 35:
        max_scenes = 1
        scene_clause = "1 СЦЕНА на серию (одна локация, без переездов)."
    elif eff_duration <= 75:
        max_scenes = 2
        scene_clause = "МАКСИМУМ 2 сцены/локации на серию. Лучше — 1 непрерывная сцена."
    elif eff_duration <= 120:
        max_scenes = 3
        scene_clause = "МАКСИМУМ 3 сцены на серию. Не разбрасывайся локациями."
    else:
        max_scenes = 4
        scene_clause = f"МАКСИМУМ {max_scenes} сцены — не больше."
    hard_caps_block = (
        "\n=== ЖЁСТКИЕ ЛИМИТЫ (обязательные, проверяй САМ перед выводом) ===\n"
        "0. 🚨 ЛОКАЦИЯ — ПРАВИЛО №1, СТРОЖАЙШЕЕ:\n"
        "   ПОСЛЕ строки «Кратко: ...» ПЕРВАЯ строка серии = ЗАГОЛОВОК СЦЕНЫ С ЛОКАЦИЕЙ.\n"
        "   НЕ диалог. НЕ действие. СНАЧАЛА ЛОКАЦИЯ.\n"
        "   Формат: ИНТА. ENGLISH LOCATION NAME — ВРЕМЯ\n"
        "   Примеры: ИНТА. STORAGE UNIT — ДЕНЬ / ИНТА. HOTEL SUITE — УТРО / ИНТА. ROOFTOP TERRACE — НОЧЬ / ИНТА. HOSPITAL CORRIDOR — ВЕЧЕР\n"
        "   ❌ Избегай как основной локации: COURTROOM, LAW FIRM, JUDGE'S CHAMBERS, DEPOSITION ROOM, PROSECUTOR'S OFFICE, PRISON VISITING ROOM, EVIDENCE LOCKER — драма живёт в спальнях, кухнях, коридорах, отелях, машинах, на крышах, в больницах, НЕ в зданиях суда.\n"
        "   ❌ ЗАПРЕЩЕНО начинать серию так: 'SOPHIE: There's one more box.' (диалог без локации)\n"
        "   ❌ ЗАПРЕЩЕНО начинать серию так: 'Sophie открывает коробку.' (действие без локации)\n"
        "   ✅ ПРАВИЛЬНО: 'ИНТА. STORAGE UNIT — ДЕНЬ\\nSophie открывает коробку.'\n"
        "   Это правило применяется к КАЖДОЙ серии, даже если она продолжает ту же локацию.\n"
        "   Русские названия локаций в заголовках ЗАПРЕЩЕНЫ (не КАБИНЕТ — пиши FATHER'S STUDY).\n\n"
        "0b. 📍 БЛОКИ ПОЗИЦИЙ — ОБЯЗАТЕЛЬНО В КАЖДОЙ СЕРИИ:\n"
        "   [BLOCKING] — сразу после КАЖДОГО заголовка сцены (первого и каждого нового внутри серии):\n"
        "     [BLOCKING]\n"
        "     LOCATION: <English location name>\n"
        "     ИМЯ_ПЕРСОНАЖА: <что делает, где стоит/сидит> :: OUTFIT: <Outfit Name>\n"
        "     [/BLOCKING]\n"
        "   [BLOCKING_END] — в самом конце серии (последнее перед ничем):\n"
        "     [BLOCKING_END]\n"
        "     LOCATION: <English location name>\n"
        "     ИМЯ_ПЕРСОНАЖА: <финальная позиция> (по-русски)\n"
        "     [/BLOCKING_END]\n"
        "   Правила: [BLOCKING] перечисляет ТОЛЬКО персонажей ПРИСУТСТВУЮЩИХ В НАЧАЛЕ сцены. "
        "Описания позиций — по-русски, 1 строка на персонажа.\n"
        "   OUTFIT — КОРОТКОЕ Title Case ИМЯ ассета outfit'а (НЕ описание). Примеры: `Business Suit`, `Casual`, `Pajamas`, `Red Dress`, `School Uniform`, `Hospital Gown`, `Swimsuit`. Система по этому имени переиспользует тот же визуальный ассет в разных сценах.\n"
        "   Если в этой сцене НОВЫЙ outfit (ранее у этого перса такого имени не было) — добавь описание через пайп: `OUTFIT: Pajamas | OUTFIT_DESC: light blue cotton pajamas, bare feet`. Для уже введённых имён OUTFIT_DESC можно опустить — система знает описание.\n"
        "   DEDUP: НЕ плоди 10 имён для практически одинаковой одежды. Если перс в своём базовом образе — пиши `Base` или существующий лейбл. Новое имя = реальная смена костюма.\n"
        "   Если предоставлен PREV_END_POSITION и серия открывается в той же локации — [BLOCKING] ДОЛЖЕН СОВПАДАТЬ с ним.\n"
        "   ⛔ ЗАПРЕЩЕНО писать [SCENE_OPEN], [EPISODE_END] — это старые устаревшие теги. Только [BLOCKING]/[BLOCKING_END].\n\n"
        f"1. 🎯 ДЛИНА КАЖДОЙ СЕРИИ — это ДИАПАЗОН, который НУЖНО ПОПАСТЬ (не потолок!):\n"
        f"   • ПРОИЗНЕСЁННЫЕ СЛОВА (то что персонажи реально говорят вслух — диалог + VO): "
        f"ЦЕЛЬ ~{eff_spoken_target}, ДИАПАЗОН {eff_spoken_floor}–{eff_spoken_ceiling}. "
        f"⚠ НЕ НЕДОБИРАЙ ниже {eff_spoken_floor} — иначе серия выходит на {eff_duration//2}с и режется всего на 2-3 чанка "
        f"вместо нужных {eff_min_chunks}+ (рендер режет по словам ~2.65 сл/сек).\n"
        f"   • РЕПЛИКИ ДИАЛОГА: {eff_dlg_floor}–{eff_dlg_ceiling} строк (цель {eff_dlg_lines}). "
        f"Каждая реплика 4-9 слов; >12 слов — разбей на две короткие.\n"
        f"   • ACTION-СТРОКИ — ОТДЕЛЬНЫЙ бюджет, НЕ заменяют диалог: максимум {eff_action_budget}. "
        f"Нельзя добивать длину серии действиями вместо реплик — слова важнее.\n"
        f"   • VOICEOVER считается в слова, но НЕ строй серию на нём (макс 1-2 блока).\n"
        f"   САМОПРОВЕРКА: посчитай произнесённые слова. Меньше {eff_spoken_floor}? → ДОПИШИ диалог. "
        f"Больше {eff_spoken_ceiling}? → сократи. Цель — РОВНО на {eff_duration}с, не короче.\n"
        f"2. СЦЕНЫ: {scene_clause} Сцена = одна локация/время. Переезд = новая сцена. Каждая дополнительная сцена жрёт 3-4 реплики только на сетап.\n"
        "3. VOICEOVER: разрешён точечно (1-2 на серию максимум, как стилистический приём — открытие/закрытие). "
        "НЕ строй сюжет через закадр: откровения, эмоции, мотивацию персонажа показывай через диалог и действие, не через монолог в камеру. "
        "Если в серии 3+ VO-блока — это уже не сериал, а аудиокнига, переписывай.\n"
        "4. ЗАПРЕЩЕНО (нарушение = переписать с нуля):\n"
        "   • БУМАЖНЫЕ РАСКРЫТИЯ (paperwork reveals): нельзя двигать сюжет через письмо/завещание/документ/email/SMS/курьерский конверт/папку с бумагами/фото на телефоне/«экран ноутбука прокручивает документы»/диктофонную запись/USB-флешку/«запись с камеры». "
        "Откровения должны звучать ВСЛУХ из уст персонажа, не читаться с бумаги и не доставаться из конверта.\n"
        "   • ЮРИДИЧЕСКИЕ ДВИЖКИ (legal/courtroom engines): нельзя сводить сюжет к иску, суду, заседанию, прокурору, адвокату, судье, сбору улик/доказательств, «свидетели против него», «выиграем в суде», «возбуждено дело», «дача показаний», «экспертиза покажет». Зрителю вертикального видео не интересно смотреть процесс. "
        "Замена: прямая личная конфронтация / шантаж в лицо / преследование / похищение / физическое столкновение / разоблачение лицом к лицу / предательство близкого / угроза ребёнку / публичное унижение. Люди против людей, не люди против бумаг и не люди против системы правосудия.\n"
        "   • ФЛЭШБЕКИ и сны в первой серии. Только настоящее время.\n"
        "   • «Тем временем в…» / «А в это время…» — параллельный монтаж сложен и жрёт хронометраж.\n"
        "5. CLIFFHANGER в конце — да, но НЕ через прибывшее письмо/звонок/тайный документ/USB-флешку/запись с камеры. И НЕ через «увидимся в суде», «подаю иск завтра», «дело передано в суд». "
        "Лучше: фраза которая меняет всё, неожиданное появление человека, прямая угроза в лицо, действие которое нельзя отменить, оружие в кадре, удар, объятия с тем кого считали врагом.\n"
        "6. САМОПРОВЕРКА перед выводом каждой серии — посчитай:\n"
        f"   – Сколько ПРОИЗНЕСЁННЫХ слов? (ДОЛЖНО быть {eff_spoken_floor}–{eff_spoken_ceiling}, цель {eff_spoken_target}. "
        f"Меньше {eff_spoken_floor} = серия слишком короткая, ДОПИШИ диалог!)\n"
        f"   – Сколько реплик диалога? (должно быть {eff_dlg_floor}–{eff_dlg_ceiling})\n"
        f"   – Сколько action-строк? (≤ {eff_action_budget} — НЕ добивай длину действиями)\n"
        f"   – Сколько разных локаций/сцен? (должно быть ≤ {max_scenes})\n"
        "   – Сколько VO-блоков? (≤ 2, и сюжет НЕ должен ими двигаться)\n"
        "   – Двигается ли сюжет через бумагу/экран/запись? (должно быть НЕТ)\n"
        "   – Двигается ли сюжет через суд/иск/прокурора/сбор улик? (должно быть НЕТ)\n"
        "   – Локация сцены — не суд/юр.фирма/прокуратура как основное место действия? (должно быть НЕТ)\n"
        "   Если хоть один тест провален — перепиши серию до вывода.\n"
        + (
        "7. ПЕРЕБИВАНИЯ — ЖЁСТКИЙ ЗАПРЕТ: НИКОГДА не обрывай реплику персонажа на полуслове тире (—). "
        "Каждая произнесённая реплика — законченное предложение. ЗАПРЕЩЕНО: «ELENA: You should have—» или «(перебивает)». "
        "Видеогенератор рендерит обрезанные реплики как двух одновременно говорящих — это выглядит сломанным. "
        "Хочешь показать перебивание — заверши реплику + action line показывает физическое вмешательство + следующий персонаж говорит полную реплику.\n"
        if no_interruptions else
        "7. ПЕРЕБИВАНИЯ — РАЗРЕШЕНЫ: персонажи могут перебивать друг друга (обрыв фразы тире, «(перебивает)»). "
        "Это создаёт живой темп, но не злоупотребляй — не более 2-3 перебиваний на серию, только в эмоциональных пиках.\n"
        )
        + "===\n"
    )
    # ════════════════════════════════════════════════════════════════════════
    # HARD CONTRACT — TOP OF SYSTEM PROMPT. Two failure modes have plagued
    # this generator: (1) every episode synopsis defaults to paperwork-reveals
    # («показывает фото», «протягивает файл», «находит конверт», «достаёт USB
    # с записью») because that's the easiest 1-sentence device, (2) arcs
    # converge to courtroom / lawsuit / evidence-gathering because that's the
    # easiest macro engine. Both kill short-drama pacing. The contract below
    # is placed BEFORE everything else in the system message so the model
    # cannot skip it. The `Кратко:` line spec further down is bound to this
    # contract — if any banned token appears in Кратко, regenerate.
    # ════════════════════════════════════════════════════════════════════════
    hard_contract_block = (
        "╔════════════════════════════════════════════════════════════════════╗\n"
        "║  ЖЁСТКИЙ КОНТРАКТ — НАРУШЕНИЕ = ПЕРЕПИСАТЬ СЕРИЮ ЦЕЛИКОМ          ║\n"
        "╚════════════════════════════════════════════════════════════════════╝\n"
        "A. БУМАЖНЫЕ/ЭКРАННЫЕ НОСИТЕЛИ СЮЖЕТА — ЗАПРЕЩЕНЫ ВО ВСЕХ СЕРИЯХ.\n"
        "   Сюжет НЕ ДВИГАЕТСЯ через: фотографии, фотоснимки, фото на телефоне,\n"
        "   распечатанные фото, альбом с фото, ✦ВСЯКИЕ ФОТО ВООБЩЕ✦,\n"
        "   письма, записки, конверты, запечатанные пакеты, визитки, флаеры,\n"
        "   документы, контракты, договоры, завещания, файлы, папки, досье,\n"
        "   улики, доказательства собранные в папку, evidence binders,\n"
        "   SMS, мессенджеры, чаты, e-mail, переписку, скриншоты переписки,\n"
        "   экраны телефонов/ноутбуков/планшетов, любые UI на экране,\n"
        "   USB-флешки, micro-SD, жёсткие диски, «вот тут вся правда»,\n"
        "   диктофонные записи, voice memo, hidden mic, аудиозаписи,\n"
        "   запись с камер видеонаблюдения / CCTV / телеобъектив издалека,\n"
        "   дневники, voiceover, news headlines, газеты, новости по ТВ.\n"
        "   ❌ ЗАПРЕЩЕНО писать в Кратко: «показывает фото», «протягивает\n"
        "   конверт», «вручает визитку», «открывает папку», «приносит файл»,\n"
        "   «играет запись», «достаёт диктофон», «на флешке доказательства»,\n"
        "   «получает SMS», «на экране видно».\n"
        "   ✓ ВМЕСТО ЭТОГО: персонаж А ВСЛУХ обвиняет/угрожает/признаётся\n"
        "   персонажу Б в лицо. Откровения = устные конфронтации.\n"
        "   УЗКОЕ ИСКЛЮЧЕНИЕ — ОДИН раз на ВЕСЬ СЕРИАЛ (не на эпизод):\n"
        "   короткий физический предмет (кольцо, тест, ключ) показан 1-2с +\n"
        "   персонаж в той же фразе ВСЛУХ называет смысл. Если этот лимит уже\n"
        "   израсходован в предыдущих сериях — НИКАКИХ предметов вообще.\n"
        "\n"
        "B. ЮРИДИЧЕСКИЕ/СУДЕБНЫЕ ДВИЖКИ — ЗАПРЕЩЕНЫ КАК ДРАЙВЕР СЮЖЕТА.\n"
        "   Сериал НЕ должен сводиться к: иску, суду, заседанию, слушанию,\n"
        "   депозиции, приговору, обвинительному заключению, mediation,\n"
        "   сбору улик/доказательств как самостоятельной арке,\n"
        "   подготовке к процессу, поиску свидетелей для суда,\n"
        "   встречам с адвокатом/прокурором/детективом как климаксу,\n"
        "   опеке через суд, выселению через суд, разводу через суд как\n"
        "   главному движку, расследованию полиции как процедурной арке,\n"
        "   рейду как климаксу, ордеру на арест как кульминации.\n"
        "   ❌ ЗАПРЕЩЕНО в Кратко: «суд», «иск», «адвокат» как двигатель,\n"
        "   «прокурор», «свидетель против», «доказательства против него»,\n"
        "   «детектив приносит улики», «расследование вскрывает», «суд решит»,\n"
        "   «выходим в суд», «подаёт иск», «пересмотр опеки в суде»,\n"
        "   «передача дела в суд», «адвокатская фирма», «частный детектив\n"
        "   собрал доказательства», «инвестигатор показывает файл», «открывает\n"
        "   дело», «свидетельские показания», «слушание по опеке».\n"
        "   ✓ ВМЕСТО ЭТОГО (замены 1-к-1):\n"
        "   • «суд решит опеку»     → похищение ребёнка одним из родителей\n"
        "   • «адвокат принёс файл»  → бывший муж/любовник появляется на\n"
        "                              пороге и говорит правду в лицо\n"
        "   • «прокурор обвиняет»    → жертва даёт пощёчину в публичном месте\n"
        "   • «детектив принёс улики»→ родственник звонит в дверь со словами\n"
        "                              «я знаю что ты сделал» — устная сцена\n"
        "   • «сбор свидетелей»      → свидетель сам приходит и устраивает\n"
        "                              скандал на свадьбе/похоронах\n"
        "   • «расследование»        → личное преследование, погоня, засада\n"
        "   Закон может СУЩЕСТВОВАТЬ в мире (один намёк на одну серию: «мой\n"
        "   адвокат уже едет», полицейский в дверях на 5 секунд), но НЕ как\n"
        "   двигатель сюжета и НЕ как место действия.\n"
        "\n"
        "C. ЛОКАЦИИ — избегай как ОСНОВНОЕ место сцены: COURTROOM, LAW FIRM,\n"
        "   JUDGE'S CHAMBERS, DEPOSITION ROOM, PROSECUTOR'S OFFICE, DA'S\n"
        "   OFFICE, EVIDENCE LOCKER, PRISON VISITING ROOM (как повторяющееся),\n"
        "   POLICE STATION INTERROGATION ROOM (как климакс). Драма живёт в\n"
        "   спальнях, кухнях, коридорах, отелях, машинах, на крышах, в\n"
        "   больницах, в местах работы героев — НЕ в зданиях правосудия.\n"
        "\n"
        "D. САМОПРОВЕРКА КАЖДОЙ СЕРИИ перед выводом:\n"
        "   1) Содержит ли строка «Кратко: …» хоть одно слово из списка A или\n"
        "      B? → ДА = ПЕРЕПИШИ Кратко через устную конфронтацию.\n"
        "   2) Двигается ли центральное событие серии через бумагу/экран/\n"
        "      запись? → ДА = ПЕРЕПИШИ сцену через устное обвинение.\n"
        "   3) Это судебная/следственная сцена или подготовка к ней?\n"
        "      → ДА = ПЕРЕПИШИ через личное столкновение.\n"
        "   4) Локация сцены — не суд/прокуратура/юр.фирма? → ДОЛЖНО быть НЕТ.\n"
        "   Если хоть одна проверка провалена — НЕ ВЫВОДИ серию, перепиши.\n"
        "════════════════════════════════════════════════════════════════════\n\n"
    )
    system = (
        hard_contract_block
        + f"Ты — сценарист короткой драмы для вертикального TikTok/Reels. Пишешь {mode_label} на N серий. "
        f"{length_clause}Формат: "
        "имена ВЕРХНИМ регистром перед репликами, диалог короткий и накалённый, обязательный cliffhanger "
        "в конце КАЖДОЙ серии (открытый вопрос или новая угроза которая толкает к следующей).\n"
        f"{style_block}"
        f"{crowd_block}"
        f"{hard_caps_block}\n"
        + ("ПРАВИЛА ПИЛОТА И СТАРТОВОЙ ДУГИ:\n"
           "1. Если в roster уже есть персонажи — используй их имена дословно. Если roster пустой — "
           "сам придумай героев, дай каждому отчётливое имя и личность.\n"
           "2. Локации: если в roster есть — используй. Если нет — придумай простые однозначные "
           "(КОФЕЙНЯ, ОФИС, КВАРТИРА БРАТА). Не уходи в фэнтези-сеттинг если жанр reality/драма.\n"
           "3. Стиль/тон бери из жанра + tone из Bible. Если они пустые — пиши как короткая драма "
           "для соцсетей: высокая эмоция, простые конфликты, неожиданные повороты.\n"
           "4. Каждая серия имеет свой arc (начало → обострение → cliffhanger).\n"
           "5. За {count} серий построй мини-арк со сквозным конфликтом.\n\n"
            if from_scratch else
           "ПРАВИЛА ПРОДОЛЖЕНИЯ:\n"
           "1. Используй СУЩЕСТВУЮЩИХ персонажей и локации из roster (имена дословно). Новых вводи только "
           "если без них не обойтись по сюжету.\n"
           "2. Сохраняй tone и стиль предыдущих серий — посмотри последние 8 серий для калибровки.\n"
           "3. Каждая серия должна иметь свой arc (начало → обострение → cliffhanger), но быть частью общей дуги.\n"
           "4. Не повторяй уже произошедшие события дословно — двигай сюжет вперёд.\n"
           "5. Используй существующие сюжетные предметы (items) когда они уместны.\n"
           "6. Открытые линии из предыдущих серий — либо двигай их, либо логично откладывай.\n\n")
        + "ФОРМАТ ВЫХОДА — СТРОГО:\n"
        + f"Episode {first_new_num}: <короткое название серии — БЕЗ слов 'Photo', 'File', 'Evidence', 'Investigator', 'Letter', 'Court', 'Trial', 'Hearing', 'Lawyer', 'Witness'>\n"
        + f"Кратко: <1-2 предложения о чём серия — ОБЯЗАТЕЛЬНО через устную конфронтацию двух людей; БЕЗ упоминания фото/файла/конверта/визитки/папки/USB/SMS/экрана/записи/иска/суда/адвоката/прокурора/детектива-с-уликами>\n"
        + f"ИНТА. LOCATION NAME — ВРЕМЯ  ← ОБЯЗАТЕЛЬНО, первая строка до любого диалога/действия. Не COURTROOM / LAW FIRM / DA'S OFFICE как основная локация.\n"
        + f"<реплики и действия персонажей — диалог, action lines. Сюжет двигается только через устную речь и физическое действие, НЕ через бумагу/экран/запись>\n"
        + "\n"
        + f"Episode {first_new_num + 1}: <название без запретных слов>\n"
        + f"Кратко: <синопсис — устная конфронтация, без запретных носителей>\n"
        + f"ИНТА. LOCATION NAME — ВРЕМЯ  ← обязательно каждый раз\n"
        + f"<содержимое>\n"
        + "\n"
        + f"... и так далее до Episode {last_new_num}.\n\n"
        + f"⏱ ХРОНОМЕТРАЖ — ЖЁСТКО И В ОБЕ СТОРОНЫ: каждая серия = ~{eff_duration}с экрана. "
        + f"Это значит {eff_spoken_floor}–{eff_spoken_ceiling} ПРОИЗНЕСЁННЫХ слов (цель {eff_spoken_target}). "
        + f"⚠ НЕДОБОР так же плох как перебор: серия на {eff_spoken_floor-20} слов выходит на {eff_duration//2}с и режется на 2-3 чанка вместо {eff_min_chunks}+. "
        + f"Если слов меньше {eff_spoken_floor} — ДОПИШИ живой диалог (короткие реплики, реакции, обострение), НЕ растягивай action. "
        + f"Если больше {eff_spoken_ceiling} — сократи или перенеси в следующую серию. РОВНО ~{eff_duration}с на каждую серию.\n"
        + "Каждая серия начинается с СТРОГО строки 'Episode N: <title>' — без других маркеров. "
        + "Никакой markdown, никаких '===', никаких '#'. Только plain text. "
        + ("ЯЗЫК — СТРОГО ПО СТАНДАРТУ (не зависит от языка библии/синопсиса/предыдущих серий):\n"
           "  • ДИАЛОГ (все реплики персонажей и VO) — ТОЛЬКО английский. Каждая произносимая строка на English.\n"
           "  • ACTION-строки, ремарки, описания позиций в [BLOCKING]/[BLOCKING_END] — русский.\n"
           "  • Заголовки сцен: часть-локация на English (см. правило про ИНТА. ENGLISH LOCATION выше).\n"
           "  • Названия серий (Episode N: ...) и 'Кратко:' — русский.\n"
           "  • Имена персонажей в репликах (NAME:) — ALL CAPS, точно как заданы, НЕ переводить.\n"
           "⚠ Даже если библия/синопсис на русском — реплики ВСЁ РАВНО пиши по-английски: "
           "downstream видео-генерация (Seedance) требует английский диалог, это нередактируемое требование стандарта.\n\n")
        + "ВАЖНО: возвращай ТОЛЬКО сценарий, без преамбулы 'Вот сценарий:' и без post-комментариев."
    )
    # Build narrative history + device history blocks for batch gen
    try:
        _batch_devices_block = _build_plot_device_history(sid, first_new_num)
    except Exception:
        _batch_devices_block = ''
    try:
        _batch_narrative_block = _build_narrative_state_block(sid, first_new_num)
    except Exception:
        _batch_narrative_block = ''
    _beats_block = _series_beats_episode_block(s)
    _source_outline_block = _source_outline_episode_block(s, first_new_num, last_new_num)
    # Finale awareness — if the pinned finale falls inside the [first..last] range
    # this bulk write covers, the finale episode must RESOLVE (no cliffhanger),
    # overriding the per-episode "обязательно cliffhanger" rule below.
    # lazy: lives in sw.routes.landmarks (module-level import would shift
    # its route-registration order)
    from sw.routes.landmarks import finale_episode_num
    _fin_ep = finale_episode_num(s)
    _finale_in_range = _fin_ep is not None and first_new_num <= _fin_ep <= last_new_num
    _batch_finale_note = ''
    if _finale_in_range:
        _fin_desc = ((s.get('finale') or {}).get('description') or '').strip()
        _batch_finale_note = (
            f"\n\n🏁🏁🏁 ВНИМАНИЕ: Эп.{_fin_ep} В ЭТОМ ДИАПАЗОНЕ — ЭТО ФИНАЛ СЕРИАЛА (последняя серия). "
            f"Эп.{_fin_ep+1} НЕ СУЩЕСТВУЕТ.\n"
            f"Для Эп.{_fin_ep} правило «обязательный cliffhanger в конце» НЕ ДЕЙСТВУЕТ — наоборот:\n"
            f"• закрой ВСЕ открытые сюжетные линии прямо на экране — никаких «решится завтра», "
            f"«to be addressed», «setup for next episode», отложенной расплаты;\n"
            f"• исполни зафиксированные события финала ТОЧНО (те самые персонажи, развязки, примирения, финальный кадр);\n"
            f"• дай эмоциональный катарсис и заверши КОНКЛЮЗИВНЫМ финальным битом — НЕ клиффхэнгером, "
            f"НЕ новой угрозой/загадкой, НЕ заделом на следующую серию.\n"
            f"Все серии ДО Эп.{_fin_ep} в этом диапазоне заканчиваются клиффхэнгером как обычно.\n"
            f"ЗАФИКСИРОВАННЫЙ ФИНАЛ (исполни как конечное состояние полностью):\n{_fin_desc}\n"
        )
    user_msg = (
        f"СЕРИАЛ: «{s.get('title') or 'untitled'}»\n"
        f"Жанр: {s.get('genre') or '?'} · Тон: {s.get('tone') or '?'} · "
        f"Аудитория: {s.get('target_audience') or '?'}\n"
        f"{('Мир: ' + (s.get('world_description') or '')[:300] + chr(10)) if s.get('world_description') else ''}"
        f"ROSTER ПЕРСОНАЖЕЙ: {chars_list or '(пусто — можешь придумать сам)' if from_scratch else (chars_list or '(пусто)')}\n"
        f"ROSTER ЛОКАЦИЙ:    {locs_list or '(пусто — придумай простые)' if from_scratch else (locs_list or '(пусто)')}\n"
        f"СЮЖЕТНЫЕ ПРЕДМЕТЫ: {items_list or '(пусто)'}\n"
        f"{_beats_block}\n"
        f"{_source_outline_block}"
        f"{earlier_block}"
        + (f"ПОСЛЕДНИЕ {len(verbatim_window)} СЕРИЙ (verbatim, для тонкой калибровки стиля и continuity):\n```\n{verbatim_block}\n```\n\n"
           if verbatim_block else '')
        + f"{direction_block}\n"
        + (_batch_narrative_block if _batch_narrative_block else '')
        + (_batch_devices_block if _batch_devices_block else '')
        + (f"НАПИШИ ПЕРВЫЕ {count} СЕРИЙ (Эп.{first_new_num}–{last_new_num}). "
            if from_scratch else
           f"НАПИШИ СЛЕДУЮЩИЕ {count} СЕРИЙ (Эп.{first_new_num}–{last_new_num}). ")
        + f"Каждая РОВНО ≈ {eff_duration}с экрана = {eff_spoken_floor}–{eff_spoken_ceiling} произнесённых слов "
        + f"(цель {eff_spoken_target}), ~{eff_dlg_lines} реплик диалога, до {eff_action_budget} action-строк. "
        + f"НЕ НЕДОБИРАЙ (короткая серия = 2-3 чанка вместо {eff_min_chunks}+) и не переполняй. Обязательно cliffhanger в конце.\n\n"
        + "🚨 ПОСЛЕДНЯЯ ПРОВЕРКА ПЕРЕД ВЫВОДОМ — пройдись по КАЖДОЙ серии:\n"
        + "  ✗ Если в строке «Кратко: …» есть слова: фото, фотограф, фотоснимок, файл, папка, конверт, "
        + "записка, письмо, визитка, USB, флешка, диктофон, запись, камера наблюдения, телеобъектив, SMS, "
        + "переписка, экран, документ, контракт, завещание, дневник, газета — ПЕРЕПИШИ Кратко с нуля через "
        + "устную конфронтацию двух людей лицом к лицу.\n"
        + "  ✗ Если в Кратко или в теле серии есть: иск, суд, адвокат, прокурор, детектив-с-уликами, "
        + "слушание, заседание, депозиция, опека-через-суд, расследование-как-арка, ордер, рейд, evidence — "
        + "ПЕРЕПИШИ через личное столкновение (конфронтация, шантаж в лицо, преследование, похищение, "
        + "публичное унижение, физический удар, неожиданное появление человека).\n"
        + "  ✗ Если локация сцены — COURTROOM / LAW FIRM / DA'S OFFICE / JUDGE'S CHAMBERS / DEPOSITION ROOM "
        + "— ПЕРЕПИШИ сцену в спальне / кухне / отеле / коридоре / больнице / машине / на крыше.\n"
        + f"  ✗ Если в серии МЕНЬШЕ {eff_spoken_floor} произнесённых слов — серия СЛИШКОМ КОРОТКАЯ, ДОПИШИ диалог до ~{eff_spoken_target} "
        + f"(порежется на 2-3 чанка вместо {eff_min_chunks}+). Если больше {eff_spoken_ceiling} — сократи/перенеси.\n"
        + "Эти проверки делай для КАЖДОЙ из серий перед выводом. Не выводи серию, которая хоть одну проверку провалила."
        + _batch_finale_note   # ← finale override, appended LAST so it wins for the finale episode
    )
    try:
        # Allow up to 24K output for 5+ episodes.
        raw = claude_ask(user_msg, system=system, max_tokens=24000)
        # Strip any code-fence accidents
        text = raw.strip()
        if text.startswith('```'):
            # Drop first line + last line if they're fence markers
            lines = text.split('\n')
            if lines[0].startswith('```'): lines = lines[1:]
            if lines and lines[-1].startswith('```'): lines = lines[:-1]
            text = '\n'.join(lines).strip()

        # ── PER-EPISODE LENGTH SAFETY NET ──────────────────────────────────
        # Instructions alone don't guarantee length — the model still skimps on
        # some episodes (esp. early ones in a batch), producing scripts that the
        # renderer slices into only 2-3 chunks. We measure EACH episode with the
        # same detector the single-episode path uses and, if any UNDERSHOOT, do
        # ONE corrective pass that names the short episodes + their word deficit
        # and asks for the full batch back with those episodes expanded. We do
        # NOT touch the chunking system — we only push the WRITER to hit length.
        _len_series = {'target_duration_sec': eff_duration}
        def _short_episodes(script_text):
            shorts = []
            for ep in (_split_script_into_episodes(script_text) or []):
                body = ep.get('body') or ''
                m = _script_runtime_metrics(body)
                # Undershoot = rendered runtime well under target OR spoken words
                # below the floor (either → too few chunks).
                if (m['est_runtime_sec'] < eff_duration * 0.85) or (m['dialogue_words'] < eff_spoken_floor):
                    shorts.append({
                        'number': ep.get('number'),
                        'title': ep.get('title') or '',
                        'words': m['dialogue_words'],
                        'est': m['est_runtime_sec'],
                        'deficit': max(0, eff_spoken_target - m['dialogue_words']),
                    })
            return shorts

        shorts = _short_episodes(text)
        if shorts:
            short_list = '; '.join(
                f"Эп.{x['number']} ({x['words']} слов ≈ {x['est']}с — добавь ещё ~{x['deficit']} слов)"
                for x in shorts
            )
            print(f'[batch-length] {len(shorts)} short episode(s): {short_list}', flush=True)
            fix_msg = (
                "Ниже — сгенерированный многосерийный сценарий. ЧАСТЬ СЕРИЙ СЛИШКОМ КОРОТКИЕ: "
                "у них мало ПРОИЗНЕСЁННЫХ слов, поэтому рендер порежет их всего на 2-3 чанка "
                f"вместо нужных {eff_min_chunks}+.\n\n"
                f"СЕРИИ ТРЕБУЮЩИЕ РАСШИРЕНИЯ: {short_list}.\n\n"
                f"ЗАДАЧА: верни ВЕСЬ сценарий целиком (все {count} серий, Эп.{first_new_num}–{last_new_num}, "
                "в том же формате 'Episode N: …' + 'Кратко: …' + тело), но КАЖДУЮ помеченную серию "
                f"допиши до {eff_spoken_floor}–{eff_spoken_ceiling} произнесённых слов (цель {eff_spoken_target}). "
                "КАК расширять — правильно:\n"
                "• добавляй КОРОТКИЕ живые реплики (4-9 слов): реакции, возражения, подколы, угрозы, признания;\n"
                "• углубляй конфликт сцены — больше обмена ударами между персонажами;\n"
                "• НЕ добивай длину action-строками («он смотрит», «пауза») и НЕ растягивай монологами;\n"
                "• сохрани cliffhanger, локации, [BLOCKING] блоки и сюжет — меняется только плотность диалога;\n"
                "• серии, которые НЕ помечены, оставь как есть.\n"
                "Соблюдай ВСЕ прежние запреты (никаких бумаг/экранов/судов). Верни ТОЛЬКО сценарий."
            )
            try:
                raw2 = claude_ask(fix_msg + "\n\n=== СЦЕНАРИЙ ДЛЯ ДОРАБОТКИ ===\n" + text,
                                  system=system, max_tokens=24000)
                text2 = raw2.strip()
                if text2.startswith('```'):
                    l2 = text2.split('\n')
                    if l2[0].startswith('```'): l2 = l2[1:]
                    if l2 and l2[-1].startswith('```'): l2 = l2[:-1]
                    text2 = '\n'.join(l2).strip()
                # Accept the retry only if it actually reduced the shortfall and
                # still splits into the expected episode count (guard against the
                # model returning a partial / mangled batch).
                eps2 = _split_script_into_episodes(text2)
                if eps2 and len(_short_episodes(text2)) < len(shorts):
                    print(f'[batch-length] corrective pass improved: '
                          f'{len(shorts)} → {len(_short_episodes(text2))} short', flush=True)
                    text = text2
                else:
                    print('[batch-length] corrective pass did not improve — keeping original', flush=True)
            except Exception as _re:
                print(f'[batch-length] corrective pass FAILED: {_re}', flush=True)

        payload = {
            'script': text,
            'first_episode': first_new_num,
            'last_episode':  last_new_num,
            'count': count,
            'from_scratch': from_scratch,
        }
        # Generated text SHOULD be English dialogue per _BATCH_SCRIPT_SYSTEM,
        # but Claude occasionally drifts to Russian when the bible / direction
        # is in Russian. Detect and warn so the UI can offer adaptation
        # before the user appends these episodes to the series.
        lang_info = _detect_dialogue_language(text)
        if lang_info['ratio'] > 0.15 and lang_info['non_english_lines'] > 0:
            payload['dialogue_lang_warning'] = lang_info
        return jsonify(payload)
    except Exception as e:
        _log_event('WARN', 'generate_script_batch_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<sid>/append-from-script', methods=['POST'])
def append_from_script(sid):
    """Append a multi-episode script to an EXISTING series. Splits the pasted
    text into episodes (using the same regex pipeline as /import-from-script),
    numbers them continuing from the highest existing episode in this series,
    and kicks off the entity-extraction worker. Returns immediately so the UI
    can poll /import-status for progress.

    Body:
      {script: str, extract_entities?: bool}
    """
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'series not found'}), 404
    data = request.json or {}
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400
    do_extract = bool(data.get('extract_entities', True))
    dialogue_lang_hint = (data.get('dialogue_language_hint') or '').strip().lower()
    if dialogue_lang_hint and dialogue_lang_hint not in ('en', 'english'):
        s['dialogue_language_hint'] = dialogue_lang_hint
        save_series(sid, s)

    eps = _split_script_into_episodes(script)
    if not eps:
        return jsonify({'error': 'script split produced no episodes'}), 400

    # Find the next free episode number — keep continuous numbering so the
    # editor's «соседи» strip stays usable.
    existing_eps = list_episodes(sid)
    next_num = (max((e.get('number') or 0) for e in existing_eps) + 1) if existing_eps else 1

    ep_records = []
    for offset, e in enumerate(eps):
        num = next_num + offset
        ep_dict = {
            'number': num,
            'title':  e['title'] or f'Эпизод {num}',
            'synopsis': '',
            'script':   e['body'],
            'characters_used': [],
            'locations_used':  [],
            'items_used':      [],
            'notes': '', 'reteller_prompt': '',
            'status': 'draft', 'ready': False,
            'created_at': datetime.datetime.utcnow().isoformat(),
        }
        save_episode(sid, num, ep_dict)
        # Sync [BLOCKING] outfits on append too — same Margaret-prevention rule
        # as the bulk import path. Idempotent & cheap.
        try:
            _sync_script_outfits(sid, e['body'])
        except Exception as _oe:
            _log_event('WARN', 'outfit_sync_after_append_failed',
                       sid=sid, ep=num, err=str(_oe)[:200])
        # Extract plot devices for anti-repetition tracking (best-effort, non-blocking)
        try:
            append_devices = _extract_devices_from_script(e['body'])
            if append_devices:
                ep_dict['plot_devices'] = append_devices
                save_episode(sid, num, ep_dict)
                _update_devices_index(sid, num, append_devices)
        except Exception as _ade:
            _log_event('WARN', 'device_extract_append_failed', ep=num, err=str(_ade)[:200])
        ep_records.append({'number': num})

    if do_extract and ep_records:
        _spawn_with_keys(_import_worker, sid, ep_records)

    return jsonify({
        'sid': sid,
        'first_episode': ep_records[0]['number'] if ep_records else None,
        'last_episode':  ep_records[-1]['number'] if ep_records else None,
        'episodes_appended': len(ep_records),
        'extraction_started': do_extract and bool(ep_records),
    }), 201
