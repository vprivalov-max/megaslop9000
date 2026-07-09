"""Seedance run routes: start, poll, kill-switch, QC, delete, heal-prompt."""
import json
import os
import re
import time

from flask import jsonify, request

from sw.auth import _get_user_avai_key, _spawn_with_keys
from sw.config import ANTHROPIC_KEY, OPENAI_KEY
from sw.core import app
from sw.jsonutils import strip_json
from sw.llm import claude_ask
from sw.locks import _episode_lock
from sw.logging_utils import _log_event
from sw.recovery import _SUBMITTING_TIMEOUT_RUNTIME_SEC, _recover_inflight_chunks
from sw.logging_utils import _start_log_cleanup_loop
from sw.seedance import (SEEDANCE_PRECHECK_ENABLED, AVAICircuitBreakerError,
                         _AVAI_KILL_SWITCH, _avai_kill_switch_status,
                         _avai_seedance_start, _avai_seedance_status,
                         _classify_moderation_block,
                         _download_video, _next_chunk_idx, _purge_continuity_sidecars,
                         _qc_run_chunk, _resolve_ref_url, _seedance_chunks,
                         _seedance_moderation_precheck)
from sw.storage import (list_episodes, load_episode, load_series, save_episode,
                        series_path, vid_dir)
from sw.textrules_sanitizer import _sanitize_appearance_for_moderation

# ── Moderation auto-recovery ladder ─────────────────────────────────────────
# On a moderation block we classify the block (see _classify_moderation_block)
# and apply the CHEAPEST *legitimate* fix for that class — never evasion
# (grid/collage/cartoon are NOT used automatically, per product decision), never
# muting audio (the Seedance voice is the final deliverable), never rewriting
# dialogue meaning or character appearance. Each class has an ordered list of
# rungs; one full pass per chunk, then we STOP and surface an actionable message
# so the human decides (deep rewrite / swap reference / manual mute) rather than
# the tool silently mangling the scene. Cost stays bounded by the AVAI circuit
# breaker + this per-chunk one-pass cap (mod_ladder_step).
#   - 'retry'             : resubmit the identical prompt once. The prompt-text
#                           filter is non-deterministic, so a borderline chunk
#                           often clears on a second identical attempt at zero
#                           intent cost. (Same fingerprint → breaker allows ≤3.)
#   - 'redescribe_action' : Claude rewrites ONLY the ACTION/SCENE description
#                           ("what it looks like, not what it is") — dialogue,
#                           appearance and @ImageN refs are left untouched.
#   - 'dialogue_min'      : Claude minimally edits ONLY the offending spoken
#                           line(s), preserving length for lip-sync. Used for
#                           audio-path blocks where the voice trips the filter.
# AUTO ladder (hands-off, poll-driven): only gentle, beat-preserving rungs, then
# STOP + surface. Deliberately excludes the invasive 'deep_rewrite' so a batch
# auto-run never silently rewrites a scene — that heavier step is reserved for
# the explicit manual «Попробовать обойти модерацию» button (see the driver).
_MOD_LADDER = {
    'content': ['retry', 'redescribe_action', 'redescribe_aggressive'],
    'audio':   ['dialogue_min', 'redescribe_action', 'redescribe_aggressive'],
    # 'face' and 'hard' have no ladder — a prompt/dialogue edit can't clear a
    # reference-image face block or a hard (real-person/NSFW/minor) block, and
    # we refuse evasion — so we stop immediately and tell the user what to do.
}

# Full sequence for the MANUAL «pass moderation» button — same legit tools plus
# the deep series-aware scene rewrite as the final rung. No evasion, voice kept.
def _pass_mod_sequence(klass):
    # Manual «pass moderation» battery. AVAI's block response is fully opaque
    # (images:["/error.jpg"], cost 0, NO reason — verified from logs) so we can't
    # classify, and blocks are free — so the rational move is a DIVERSE battery of
    # legit attempts: re-rolls exploit the non-deterministic filter; 'reduce_refs'
    # tests whether a specific reference image is the trigger; deep_rewrite is the
    # heaviest. Bounded by _CHUNK_SUBMIT_LIMIT. No evasion, voice kept.
    base = ['retry', 'redescribe_action', 'redescribe_aggressive', 'reduce_refs', 'deep_rewrite', 'retry']
    return (['dialogue_min'] if klass == 'audio' else []) + base

_MOD_ACTION_LABEL = {
    'retry': 'повторная генерация (текстовый фильтр недетерминирован)',
    'redescribe_action': 'переописываю ДЕЙСТВИЕ визуально (реплики и внешность не трогаю)',
    'redescribe_aggressive': 'усиленное переописание действия + смена ракурса',
    'dialogue_min': 'минимальная правка проблемной реплики (длина сохранена под lip-sync)',
    'reduce_refs': 'пробую без части референс-картинок (вдруг режет конкретное фото)',
    'deep_rewrite': 'глубокий рерайт сцены с учётом всей серии',
}


def _mod_progress_message(klass, action, step):
    return (f'Заблокировано модерацией Seedance ({klass}). '
            f'Авто-фикс #{step + 1}: {_MOD_ACTION_LABEL.get(action, action)}…')


def _mod_stop_message(klass):
    if klass == 'hard':
        return ('Хард-блок Seedance (реальное лицо / NSFW / несовершеннолетние). '
                'Легитимного обхода нет и обходы мы не используем — перепиши сцену '
                'вручную кнопкой «✍️ Переписать сцену».')
    if klass == 'face':
        return ('Фильтр режет ЛИЦО в референс-картинке персонажа (слишком '
                'фотореалистичное/похоже на реального человека). Сетку и другие '
                'обходы не используем — замени фото персонажа на менее '
                'фотореалистичное или сгенерированный референс и перезапусти.')
    return ('Не прошло модерацию после авто-фиксов (повтор + переописание '
            'действия). Дальше — вручную: «✍️ Переписать сцену» (глубокий рерайт '
            'с учётом всей серии). Обходы (сетка/мультяшность) не применяем намеренно.')


def _ask_json(user, system, retries=1):
    """claude_ask + strict JSON parse, tolerant of the model occasionally
    emitting an unescaped quote/newline inside a string value (which broke the
    aggressive-rewrite rung with «Expecting ',' delimiter»). On a parse failure
    we retry once with a hard «valid JSON, escape quotes/newlines» instruction.
    Raises if it still can't parse."""
    raw = claude_ask(user, system=system)
    try:
        return json.loads(strip_json(raw))
    except Exception:
        for _ in range(max(0, retries)):
            raw = claude_ask(
                user + "\n\n⚠️ ВАЖНО: верни ТОЛЬКО валидный JSON, без текста вне JSON. "
                "Внутри строковых значений экранируй все двойные кавычки как \\\" и "
                "переносы строк как \\n.",
                system=system)
            try:
                return json.loads(strip_json(raw))
            except Exception:
                continue
        raise


# ── Per-chunk lifetime submit cap (kill-switch / budget guard) ───────────────
# AVAI reports cost 0 on a moderation block (verified from real logs), so the
# real risk of grinding a doomed chunk is not $ but tripping the global
# kill-switch on submit COUNT. This cap stops any single chunk from burning more
# than N submits across its life; the user can reset the counter to re-arm it.
_CHUNK_SUBMIT_LIMIT = 8


def _reserve_chunk_submit(sid, num, idx):
    """Atomically check + bump the per-chunk lifetime submit counter. Returns
    (allowed, count). A chunk that keeps failing can't grind past the limit until
    the user resets it via /reset-submit-limit."""
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        cc = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None) if ep else None
        if not cc:
            return False, 0
        n = int(cc.get('submit_count') or 0)
        if n >= _CHUNK_SUBMIT_LIMIT:
            return False, n
        cc['submit_count'] = n + 1
        save_episode(sid, num, ep)
        return True, n + 1


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/start', methods=['POST'])
def seedance_start(sid, num):
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    prompt = (body.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'prompt required'}), 400
    # Scrub sexualizing / NSFW wording out of the incoming composed prompt
    # BEFORE anything else — the pre-flight moderation gate (_seedance_moderation_
    # precheck) and the stored chunk both need the cleaned text, otherwise a
    # prompt the client composed from a charged appearance/script still gets
    # flagged here even though _avai_seedance_start would have scrubbed it at
    # submit. De-escalates only (see _sanitize_appearance_for_moderation).
    _clean = _sanitize_appearance_for_moderation(prompt)
    if _clean != prompt:
        try:
            _log_event('INFO', 'seedance_start_prompt_sanitized', sid=sid, ep=num,
                       before=prompt[:200], after=_clean[:200])
        except Exception:
            pass
        prompt = _clean
    ref_urls = body.get('ref_urls') or []
    # accept ref descriptors {kind,id,outfit} too
    if not ref_urls and body.get('refs'):
        for r in body['refs']:
            u = _resolve_ref_url(s, r, sid=sid)
            if u:
                ref_urls.append(u)
    duration = max(5, min(15, int(body.get('duration') or 15)))
    resolution = body.get('resolution') or '720p'
    if resolution not in ('720p', '480p'):
        resolution = '720p'
    mod = body.get('moderation_bypass') or 'off'
    if mod not in ('off', 'grid', 'collage_grid', 'cartoon'):
        mod = 'off'
    model_tier = body.get('model') or 'reference-fast'
    if model_tier not in ('reference-pro', 'reference-fast'):
        model_tier = 'reference-fast'
    aspect = body.get('aspect_ratio') or '9:16'
    gen_audio = bool(body.get('generate_audio', True))
    # Optional canonical script order — set by auto-mode parallel so the UI
    # can sort chunks by script position regardless of network arrival order.
    script_order_raw = body.get('script_order')
    try:
        script_order = int(script_order_raw) if script_order_raw is not None else None
    except (TypeError, ValueError):
        script_order = None

    # Scene metadata — persisted on chunk so music generator can group chunks
    # by scene. Optional (legacy chunks without these can't be musicked
    # automatically but are otherwise unaffected).
    def _as_int_or_none(v):
        try: return int(v) if v is not None else None
        except (TypeError, ValueError): return None
    scene_idx_val = _as_int_or_none(body.get('sceneIdx'))
    seg_idx_val   = _as_int_or_none(body.get('segIdx'))
    duration_sec_val = _as_int_or_none(body.get('durationSec')) or duration
    # B1: composer-reported «scene_continuity was reset» reason. Persisted on
    # the chunk so the UI can show a yellow «⚠ континьюити сброшен» badge
    # explaining why a pose/position discontinuity occurred relative to prev chunk.
    continuity_reset_reason = (body.get('continuity_reset_reason') or '').strip()[:500]

    # ── PRE-FLIGHT MODERATION CHECK ─────────────────────────────────────────
    # Run BEFORE creating a chunk or hitting AVAI. Saves ~$0.10-0.30 per
    # rejected submit + the post-moderation billing. Calibrated only against
    # OFFICIAL ByteDance Content Pre-filter categories (sexual/violence_graphic/
    # self_harm/hate_speech/misinformation). No community-lore substitutions.
    #
    # Honors body['skip_precheck']=true escape hatch for cases where user
    # explicitly wants to bypass (e.g. retry after manual review).
    precheck_result = None
    if SEEDANCE_PRECHECK_ENABLED and not body.get('skip_precheck'):
        precheck_result = _seedance_moderation_precheck(prompt)
        if precheck_result.get('verdict') == 'reject':
            print(f'[precheck] REJECTED prompt for sid={sid} ep={num}: '
                  f'{precheck_result.get("reasoning", "")[:200]}', flush=True)
            return jsonify({
                'error': 'moderation_precheck_reject',
                'message': (
                    f'Промпт почти наверняка зарубит модерация Seedance: '
                    f'{precheck_result.get("reasoning", "")}. '
                    f'Подсказка: {precheck_result.get("suggestion", "")}. '
                    f'Если уверен что это false positive — добавь '
                    f'"skip_precheck": true в body запроса.'
                ),
                'precheck': precheck_result,
            }), 400

    # Per-episode lock — serializes the read-load-mutate-save cycle so parallel
    # /start calls don't race and erase each other's chunks (Flask threaded=True
    # would otherwise let 4 parallel callers all read [], all add their chunk,
    # all save → only the last write survives).
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)            # re-read inside lock for freshness
        chunks = _seedance_chunks(ep)
        idx = _next_chunk_idx(ep)
        chunk = {
            'idx': idx,
            'job_id': '',
            'status_url': '',
            'status': 'submitting',
            'prompt': prompt,
            'chunk_text': body.get('chunk_text', ''),
            'ref_urls': ref_urls,
            'refs': body.get('refs') or [],
            'duration': duration,
            'resolution': resolution,
            'moderation_bypass': mod,
            'model': model_tier,
            'aspect_ratio': aspect,
            'created_at': int(time.time()),
            'submit_count': 1,   # lifetime AVAI submits for this chunk (cap guard)
            # Auto-mode sets this so a moderation block runs the FULL bypass
            # battery (6 rungs) via the poll ladder instead of the short one, and
            # auto-mode waits it out before moving on. Bounded by submit_count cap.
            'mod_full_battery': bool(body.get('mod_full_battery')),
            'video_url': '',
            'video_path': '',
            'ending_state': '',
            'cost': None,
            'script_order': script_order,
            'sceneIdx': scene_idx_val,
            'segIdx': seg_idx_val,
            'durationSec': duration_sec_val,
            'continuity_reset_reason': continuity_reset_reason or None,
            # Persist precheck result on chunk so UI shows it. None if skipped.
            'precheck': precheck_result,
        }
        chunks.append(chunk)
        save_episode(sid, num, ep)

    # Kick off the AVAI call in a background thread so the UI gets the chunk
    # card instantly. The poll endpoint will then track job_id/status as soon
    # as the thread finishes the submission.
    #
    # CRITICAL: spawn via _spawn_with_keys so the per-user AVAI key is captured
    # from the live Flask `session` and forwarded to the worker thread. Without
    # this, _get_user_avai_key() inside the thread hits a torn-down request
    # context, silently returns '', and AVAI gets `x-api-key: ` → on prod the
    # request hangs until our 240s read timeout (videos die, images survive
    # only because image calls are synchronous within the request handler).
    # Anti-grid-leak instruction for grid-based moderation bypass modes.
    # Real production bug: «Diner Opens at Midnight» ep 3 chunk «1 доп 4»
    # — Seedance's grid-tiling bypass failed to de-tile cleanly, output
    # video had visible grid lines across the whole frame for the full
    # duration. Telling the model explicitly to deliver a single clean
    # frame without visible cell borders / tiling artifacts pushes the
    # de-tiling stage to do its job. `cartoon` and `off` don't tile so
    # they don't need this hint.
    mod_bypass_prompt = None
    if mod in ('grid', 'collage_grid'):
        mod_bypass_prompt = (
            "Final output MUST be a single continuous full-frame composition. "
            "NO visible grid lines, NO cell borders, NO tiling artifacts, NO "
            "panel separators, NO visible seams between regions of the frame. "
            "The frame fills 100% of the canvas as one unified shot — any "
            "grid used internally for moderation bypass must be fully removed "
            "from the rendered output."
        )

    # ── Subtitle prevention — three-layer fix ──────────────────────────────
    # Real prod bug: «My Stepmother» ep 43-44 chunks had burned-in subtitles.
    # Root cause: ANY straight/curly quotes in the prompt get interpreted by
    # reference-fast as «render this text in-frame». Sources of unwanted quotes:
    #   (1) Dialogue with speech verb: `говорит: "line"` — Layer A
    #   (2) Screenplay format: `NAME: "line"` (no verb) — Layer A2 (NEW)
    #   (3) Metaphor quotes ("клетке"), server-injected example phrases
    #       («a character in a hard pose») — Layer C (NEW)
    def _strip_dialogue_quotes_for_video(text):
        if not text:
            return text
        # Layer A — speech-verb-led dialogue → em-dash (preserves spoken text)
        speak_verbs = (
            r'(?:says?|asks?|replies|whispers?|shouts?|yells?|murmurs?|breathes?|'
            r'mutters?|growls?|hisses?|barks?|spits?|sneers?|snaps?|delivers?|states?|'
            r'требует|спрашива[еют]+|отвеча[еют]+|шепч[ёе]т|кричит|произносит|говорит|'
            r'бросает|роняет|выпаливает|выкрикивает|зов[её]т|восклица[еют]+)'
        )
        pat_a = re.compile(
            r'(' + speak_verbs + r'[^"«„]{0,40}?[:,]\s*)[\"«„]([^"»“]{2,400}?)[\"»“]',
            re.IGNORECASE | re.UNICODE,
        )
        text = pat_a.sub(lambda m: f'{m.group(1)}— {m.group(2)}', text)
        # Layer A2 — screenplay name+colon form (optional «голос/voice of»
        # prefix for voiceovers). E.g. `голос Detective Morris: "47 Hawthorne..."`
        pat_a2 = re.compile(
            r'(\b(?:голос|voice\s+(?:of|on|via|through|over))?\s*'
            r'(?:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё\-]{1,30}(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё\-]{1,30}){0,3})'
            r'\s*[:：]\s*)[\"«„]([^"»“]{2,400}?)[\"»“]',
            re.UNICODE,
        )
        text = pat_a2.sub(lambda m: f'{m.group(1)}— {m.group(2)}', text)
        # Layer C — aggressive final pass: drop ALL remaining quote PAIRS
        # around 3-400 chars. Nothing in a Seedance prompt legitimately needs
        # visible quote chars. Apostrophes inside words (what's) survive
        # because they're not closing quotes for any opener.
        pat_c = re.compile(
            r'[\"«„]([^"»“\n]{3,400}?)[\"»“]',
            re.UNICODE,
        )
        text = pat_c.sub(lambda m: m.group(1), text)
        return text

    prompt = _strip_dialogue_quotes_for_video(prompt)

    # Layer B (anti_subs_clause) REMOVED 2026-05-18 per council debate finding:
    # explicit "NO subtitles..." text in the tail of the prompt acted as a
    # mention=render paradox — naming the artifact reinforced it. Layer A
    # (em-dash dialogue sanitization above) is the root-cause fix; tail
    # negation was redundant and harmful. A/B watch on next 50 chunks.

    # Quality tail — ByteDance's officially recommended quality string per
    # BytePlus ModelArk docs. Positive-phrased only (Seedance does NOT support
    # negative prompts — they're recommended against by ByteDance). Goes on
    # every generation as the last line so the model sees it strongest.
    # Cheap (~15 tokens) free quality bump.
    quality_tail = (
        "\n\n4K, ultra HD, rich details, sharp clarity, cinematic texture, "
        "natural colors, stable picture, audio voice only delivers dialogue, "
        "frame contains visual action only."
    )
    prompt = (prompt or '').rstrip() + quality_tail

    def _submit():
        try:
            job = _avai_seedance_start(
                prompt=prompt, ref_urls=ref_urls,
                duration=duration, resolution=resolution,
                moderation_bypass=mod, aspect_ratio=aspect,
                generate_audio=gen_audio,
                moderation_bypass_prompt=mod_bypass_prompt,
                model=model_tier,
            )
            with _episode_lock(sid, num):
                ep2 = load_episode(sid, num)
                for c in _seedance_chunks(ep2):
                    if c.get('idx') == idx:
                        c['job_id'] = job['job_id']
                        c['status_url'] = job['status_url']
                        # RESURRECT: even if reaper marked us 'failed' for being
                        # slow, AVAI now confirms the job IS running and we have
                        # a job_id. Wasteful to discard a working render — flip
                        # back to pending so the poll loop tracks it to completion.
                        # User pays for these jobs whether or not we track them.
                        if c.get('status') in ('submitting', 'failed'):
                            prev_status = c.get('status')
                            c['status'] = 'pending'
                            if prev_status == 'failed':
                                c['error'] = ''   # clear stale "submit timed out" message
                                print(f'[seedance {sid}/ep{num}/#{idx}] late submit returned '
                                      f'job_id={job.get("job_id")}, resurrecting from failed → pending', flush=True)
                        break
                save_episode(sid, num, ep2)
        except Exception as e:
            with _episode_lock(sid, num):
                ep2 = load_episode(sid, num)
                for c in _seedance_chunks(ep2):
                    if c.get('idx') == idx:
                        # Don't overwrite a more specific reaper message
                        if c.get('status') != 'failed':
                            c['status'] = 'failed'
                            c['error'] = f'submit failed: {e}'
                        break
                save_episode(sid, num, ep2)

    _spawn_with_keys(_submit)
    return jsonify({'chunk': chunk})

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/poll', methods=['POST'])
def seedance_poll(sid, num):
    """Poll all non-final chunks; download finished mp4s; extract ending_state.
    Holds the per-episode lock so concurrent polls / starts / deletes don't
    clobber each other's writes."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return jsonify({'error': 'not found'}), 404
        chunks = _seedance_chunks(ep)
        changed = False
        now = int(time.time())
        for c in chunks:
            if c.get('status') in ('completed', 'failed'):
                continue
            if not c.get('job_id'):
                if c.get('status') == 'submitting':
                    age = now - int(c.get('created_at') or now)
                    if age > _SUBMITTING_TIMEOUT_RUNTIME_SEC:
                        c['status'] = 'failed'
                        c['error'] = f'submit timed out after {age}s — нажми ↻ Reuse и попробуй снова'
                        changed = True
                continue
            try:
                st = _avai_seedance_status(c['job_id'], c.get('status_url'))
            except Exception as e:
                c['status'] = 'failed'
                c['error'] = str(e)
                changed = True
                continue
            c['status'] = st.get('status') or c['status']
            if st.get('progress') is not None:
                c['progress'] = st['progress']
            if st.get('cost') is not None:
                c['cost'] = st['cost']
            if st.get('error'):
                c['error'] = st['error']
            if st.get('status') == 'completed' and st.get('video_url') and not c.get('video_path'):
                vurl = (st.get('video_url') or '').strip()
                vurl_lower = vurl.lower()
                is_moderation = (
                    'moderation' in vurl_lower
                    or vurl.startswith('/')
                    or not vurl_lower.startswith(('http://', 'https://'))
                    or not (vurl_lower.endswith('.mp4') or '.mp4?' in vurl_lower or '/video' in vurl_lower)
                )
                if is_moderation:
                    # The manual «pass moderation» driver owns escalation for its
                    # chunk — let it decide the next rung; the poll only serves as
                    # the download safety-net for a PASS (handled above). Don't run
                    # the auto-ladder here or the two would fight.
                    if c.get('mod_driver_active'):
                        changed = True
                        continue
                    # Moderation block. Classify it from AVAI's response and log
                    # the raw payload verbatim (once) — AVAI is undocumented, so
                    # this is how we learn the true block shape instead of
                    # guessing. Then run the LEGITIMATE recovery ladder for that
                    # class (no evasion / no grid / no audio-mute / no dialogue-
                    # or appearance-mangling). One full pass per chunk, then STOP
                    # and surface an actionable message so the human decides.
                    raw = st.get('raw') or {}
                    if not c.get('_mod_logged'):
                        try:
                            _log_event('INFO', 'seedance_moderation_block',
                                       sid=sid, ep=num, idx=c.get('idx'),
                                       poll_status=st.get('status'),
                                       video_url=vurl[:200],
                                       raw=json.dumps(raw, ensure_ascii=False)[:2000])
                        except Exception:
                            pass
                        c['_mod_logged'] = True
                    klass = _classify_moderation_block(raw, st.get('error'), vurl)
                    c['mod_block_class'] = klass
                    # Auto-mode chunks (mod_full_battery) get the full 6-rung bypass
                    # battery; hands-off manual chunks get the short ladder then stop.
                    seq = _pass_mod_sequence(klass) if c.get('mod_full_battery') else _MOD_LADDER.get(klass)
                    step = int(c.get('mod_ladder_step') or 0)
                    if not seq or step >= len(seq):
                        # No legitimate auto-fix (face/hard) OR ladder exhausted
                        # → stop and hand the decision to the user. The circuit
                        # breaker + this one-pass cap bound total spend.
                        c['status'] = 'failed'
                        c['error'] = _mod_stop_message(klass)
                        c.pop('_pending_mod_action', None)
                        changed = True
                        print(f'[seedance_poll] chunk {c.get("idx")} moderation STOP (class={klass}, step={step})', flush=True)
                        continue
                    action = seq[step]
                    c['mod_ladder_step'] = step + 1
                    c['status'] = 'moderation_blocked'   # visible intermediate state
                    c['progress'] = 0
                    c['error'] = _mod_progress_message(klass, action, step)
                    c.pop('job_id', None)
                    c.pop('status_url', None)
                    c['_pending_mod_action'] = action
                    changed = True
                    print(f'[seedance_poll] chunk {c.get("idx")} moderation class={klass} → rung {step+1}/{len(seq)}: {action}', flush=True)
                    continue
                try:
                    vid_local = vid_dir(sid) / f'seedance_ep{int(num):03d}_chunk{c["idx"]:03d}.mp4'
                    new_video_path = str(vid_local.relative_to(series_path(sid)))
                    # This stem may have been occupied by a previous take (idx is
                    # reused after a delete-all + regen). Purge any continuity-frame
                    # sidecars cached against it AND drop the record's cached frame
                    # refs, so the next chunk's compose extracts THIS video's frames
                    # instead of serving the deleted take's (stale hair/wardrobe).
                    _purge_continuity_sidecars(sid, new_video_path)
                    for _stale in ('lastframe_avai_url', 'cutframes_avai_urls',
                                   'cuts_detected', 'frame_state_analysis'):
                        c.pop(_stale, None)
                    _download_video(vurl, vid_local)
                    c['video_url'] = vurl
                    c['video_path'] = new_video_path
                    c.pop('mod_driver_active', None)   # driver's job passed & landed — release it
                    # Successful render — wipe any stale error field carried over
                    # from a previous transient failure (e.g. AVAI 401 on a tick
                    # before the user fixed the key, then chunk eventually
                    # completed). Without this the card keeps showing red-text
                    # "401: Unauthorized" even though the video is right there.
                    if c.get('error'):
                        c.pop('error', None)
                    if c.get('chunk_text'):
                        try:
                            es = claude_ask(
                                f"Script chunk just rendered as a video clip:\n{c['chunk_text']}\n\n"
                                "In ONE short sentence (≤25 words) describe the FINAL physical state at the end of this clip: "
                                "where each character is, their pose (standing/sitting/lying), what they hold, who is in frame, "
                                "and the location. No interpretation, just the final tableau.",
                                system="You produce one short factual continuity note. No preamble."
                            ).strip()
                            c['ending_state'] = es[:400]
                        except Exception:
                            pass
                except Exception as e:
                    c['status'] = 'failed'
                    c['error'] = f'download failed: {e}'
            changed = True
        if changed:
            save_episode(sid, num, ep)

        # ── QC trigger: spawn background QC for each newly-completed chunk
        #    that hasn't been checked yet. We snapshot the idx list under the
        #    lock and dispatch threads AFTER releasing it so QC API calls
        #    (Whisper, Vision, AVAI upload) don't block the lock.
        qc_targets = []
        for c in chunks:
            if c.get('status') == 'completed' and c.get('video_path') and not c.get('qc'):
                qc_targets.append(c.get('idx'))
        # Snapshot moderation-recovery candidates: chunks where the ladder just
        # queued an action + wiped job_id. We run the (cheap) Claude edit and the
        # fresh AVAI submit OUT of lock, on the SAME chunk slot (not a new idx,
        # so QC's sibling retry-counter is unaffected). We carry the chunk's OWN
        # moderation_bypass through unchanged — we never auto-switch to grid.
        mod_actions = []
        for c in chunks:
            action = c.pop('_pending_mod_action', None)
            if action:
                mod_actions.append({
                    'idx': c.get('idx'),
                    'action': action,
                    'block_class': c.get('mod_block_class') or 'content',
                    'prompt': c.get('prompt') or '',
                    'chunk_text': c.get('chunk_text') or '',
                    'ref_urls': list(c.get('ref_urls') or []),
                    'duration': int(c.get('duration') or 15),
                    'resolution': c.get('resolution') or '720p',
                    'aspect_ratio': c.get('aspect_ratio') or '9:16',
                    'moderation_bypass': 'off',   # recovery NEVER evades (ignore stale grid/collage)
                    'model': c.get('model') or 'reference-fast',
                    'generate_audio': c.get('generate_audio', True),
                })
        # CRITICAL: persist the popped flags before releasing the lock. Without
        # this the disk still holds _pending_mod_action → next poll re-fires the
        # same rung forever (prod bug «Vice Beasts» ep 1, 2026-05-19).
        if mod_actions:
            save_episode(sid, num, ep)

    # ── Out of lock — run each queued recovery rung + resubmit (one rung/poll).
    if mod_actions:
        for act in mod_actions:
            def _mod_runner(act=act):
                idx = act['idx']
                action = act['action']
                prompt = act['prompt']
                chunk_text = act['chunk_text']
                changes = []
                try:
                    # 1. Apply the cheapest LEGITIMATE edit for this rung via the
                    #    shared strategy helper ('retry' resubmits the identical
                    #    prompt — the text filter is non-deterministic; the others
                    #    do a scoped Claude rewrite that never touches meaning /
                    #    appearance / refs and never uses evasion).
                    prompt, chunk_text, changes = _apply_mod_strategy(
                        sid, num, action,
                        {'prompt': prompt, 'chunk_text': chunk_text,
                         'error': 'Заблокировано модерацией Seedance'})
                    # 2. Persist the edited prompt BEFORE submit so the UI shows it.
                    with _episode_lock(sid, num):
                        ep_h = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_h):
                            if cc.get('idx') == idx:
                                cc['prompt'] = prompt
                                cc['chunk_text'] = chunk_text
                                if action != 'retry':
                                    cc['heal_count'] = int(cc.get('heal_count') or 0) + 1
                                cc['error'] = (
                                    f'Модерация → {_MOD_ACTION_LABEL.get(action, action)}. '
                                    + ('Изменения: ' + '; '.join(changes[:3]) if changes
                                       else 'Повторная попытка без правок текста.')
                                )
                                break
                        save_episode(sid, num, ep_h)
                    # 3. Resubmit — SAME moderation_bypass as the chunk (never
                    #    auto-switched to grid), voice KEPT (generate_audio).
                    # Per-chunk lifetime submit cap (kill-switch / budget guard).
                    allowed, cnt = _reserve_chunk_submit(sid, num, idx)
                    if not allowed:
                        with _episode_lock(sid, num):
                            ep_e = load_episode(sid, num)
                            for cc in _seedance_chunks(ep_e):
                                if cc.get('idx') == idx:
                                    cc['status'] = 'failed'
                                    cc['error'] = (f'Достигнут лимит {_CHUNK_SUBMIT_LIMIT} генераций на чанк — '
                                                   f'авто-восстановление остановлено. «🔓 Сбросить лимит» чтобы продолжить.')
                                    break
                            save_episode(sid, num, ep_e)
                        return
                    # 'reduce_refs' rung: drop all but the first 2 reference images
                    # to test whether a specific ref photo is the trigger.
                    rung_refs = (act['ref_urls'][:2]
                                 if (action == 'reduce_refs' and len(act['ref_urls']) > 2)
                                 else act['ref_urls'])
                    print(f'[seedance_poll] chunk {idx} resubmitting after {action} ({len(rung_refs)} refs)', flush=True)
                    job = _avai_seedance_start(
                        prompt=prompt, ref_urls=rung_refs,
                        duration=act['duration'], resolution=act['resolution'],
                        moderation_bypass=act['moderation_bypass'],
                        aspect_ratio=act['aspect_ratio'],
                        generate_audio=bool(act.get('generate_audio', True)),
                        model=act['model'],
                    )
                    with _episode_lock(sid, num):
                        ep_e = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_e):
                            if cc.get('idx') == idx:
                                cc['job_id'] = job['job_id']
                                cc['status_url'] = job['status_url']
                                cc['status'] = 'pending'   # moderation_blocked → pending
                                break
                        save_episode(sid, num, ep_e)
                except AVAICircuitBreakerError as cbe:
                    print(f'[seedance_poll] recovery BLOCKED by circuit breaker for chunk {idx}: {cbe}', flush=True)
                    with _episode_lock(sid, num):
                        ep_e = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_e):
                            if cc.get('idx') == idx:
                                cc['status'] = 'failed'
                                cc['error'] = f'авто-восстановление отклонено circuit breaker\'ом: {cbe}'
                                break
                        save_episode(sid, num, ep_e)
                except Exception as e:
                    print(f'[seedance_poll] recovery FAILED for chunk {idx}: {e}', flush=True)
                    with _episode_lock(sid, num):
                        ep_e = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_e):
                            if cc.get('idx') == idx:
                                cc['status'] = 'failed'
                                cc['error'] = f'авто-восстановление не удалось: {e}'
                                break
                        save_episode(sid, num, ep_e)
            _spawn_with_keys(_mod_runner)

    # ── Out of lock — dispatch QC threads
    if qc_targets:
        avai_key = _get_user_avai_key() or ''
        anthropic_key = ANTHROPIC_KEY
        openai_key = OPENAI_KEY
        def _qc_runner(target_idx):
            # _qc_run_chunk reads OPENAI_KEY / AVAI key from module globals;
            # `_spawn_with_keys` passes them through thread-local for AVAI.
            try:
                _qc_run_chunk(sid, num, target_idx)
            except Exception as e:
                print(f'[qc] background runner for chunk {target_idx} crashed: {e}', flush=True)
        for tidx in qc_targets:
            _spawn_with_keys(_qc_runner, tidx)
    # Include kill-switch state in EVERY poll response so frontend can show
    # full-screen warning the moment it trips. Cheap (single file existence
    # check). Frontend renders a fixed-top red banner when active=True.
    return jsonify({
        'chunks': chunks,
        'avai_kill_switch': _avai_kill_switch_status(),
    })


@app.route('/api/avai/kill-switch', methods=['GET'])
def avai_kill_switch_get():
    """Public endpoint to check kill switch state. Used by frontend to render
    a global full-screen banner regardless of which page user is on."""
    return jsonify(_avai_kill_switch_status())


@app.route('/api/avai/kill-switch', methods=['DELETE'])
def avai_kill_switch_clear():
    """Manually clear the kill switch (admin / operator action). Removes the
    file. Use only after investigating the root cause."""
    if _AVAI_KILL_SWITCH.exists():
        try:
            _AVAI_KILL_SWITCH.unlink()
            print('[avai-cb] kill switch manually cleared via API', flush=True)
            return jsonify({'cleared': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'cleared': False, 'note': 'kill switch was not active'})


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/qc', methods=['POST'])
def seedance_qc(sid, num, idx):
    """Manually trigger QC on a single chunk. Synchronous — returns the qc
    dict so the caller can decide what to do (retry / accept). Used by the
    UI «🔍 Re-check QC» button and by frontend auto-retry recovery flow."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    qc = _qc_run_chunk(sid, num, idx)
    if qc is None:
        return jsonify({'error': 'chunk not found or not completed'}), 400
    return jsonify({'qc': qc})

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>', methods=['DELETE'])
def seedance_delete(sid, num, idx):
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return jsonify({'error': 'not found'}), 404
        chunks = _seedance_chunks(ep)
        chunk = next((c for c in chunks if c.get('idx') == idx), None)
        if not chunk:
            return jsonify({'error': 'chunk not found'}), 404
        # Delete local mp4 if exists — plus the continuity-frame sidecars
        # (<stem>_lastframe.png / <stem>_cutframe_*.png). Leaving the sidecars
        # behind let a re-generated chunk that reuses this idx/stem inherit the
        # deleted take's frames as continuity refs (old hair/wardrobe bug).
        if chunk.get('video_path'):
            p = series_path(sid) / chunk['video_path']
            try:
                if p.exists(): p.unlink()
            except Exception: pass
            _purge_continuity_sidecars(sid, chunk['video_path'])
        chunks[:] = [c for c in chunks if c.get('idx') != idx]
        save_episode(sid, num, ep)
        return jsonify({'ok': True})

# ── Heal sysprompts (legitimate, scoped — NO evasion, NO carpet word-swaps) ──
# Two narrow modes, chosen by the block class. Neither touches character
# appearance, prompt block structure, @ImageN refs, timings, or (for the action
# mode) dialogue. This replaces the old catch-all dictionary heal that mangled
# every line regardless of what actually blocked.
_REDESCRIBE_ACTION_SYS = (
    "Ты переписываешь Seedance-видео-промпт так, чтобы он ЛЕГИТИМНО прошёл модерацию, "
    "БЕЗ обходов (никаких grid/collage/сеток/мультяшности) и БЕЗ потери драматургии.\n\n"
    "МЕНЯТЬ РАЗРЕШЕНО ТОЛЬКО описание ДЕЙСТВИЯ и СЦЕНЫ (то, что видно в кадре). "
    "Принцип: «опиши, как это ВЫГЛЯДИТ, а не что это ЕСТЬ» — замени триггерные видео-"
    "элементы нейтральными физическими описаниями того же момента, сохранив силу сцены, "
    "композицию кадра и эмоцию. Можно сменить план на более общий / ракурс со спины / "
    "силуэт, если это снижает риск, не ломая сцену.\n\n"
    "ПРИМЕРЫ (сохраняя интенсивность, перенося её в физику/камеру):\n"
    "• удар/драка → резкое столкновение, тело отброшено, камера дёргается\n"
    "• кровь/рана → тёмное пятно расплывается по ткани\n"
    "• выстрел → яркая вспышка и резкий отскок, клуб дыма\n"
    "• нож/оружие (если не критично сюжетно) → небольшой тёмный предмет в руке\n"
    "• падение/смерть → тело обмякает и замирает, взгляд стекленеет\n"
    "• ярость → сжатая челюсть, резкое дыхание, побелевшие костяшки (НЕ 'спокойно')\n\n"
    "⚠️ СТРОГО ЗАПРЕЩЕНО:\n"
    "- Трогать, удалять или СОКРАЩАТЬ реплики в кавычках — СОХРАНИ ВСЕ реплики до одной, дословно и в том же порядке. Ни одной не выкидывай (диалог критичен для сюжета; на визуальную модерацию он не влияет).\n"
    "- Трогать ВНЕШНОСТЬ персонажей (лицо/глаза/волосы/тело/одежду) — ломает консистентность с reference-картинкой.\n"
    "- Ослаблять эмоцию — переноси её в позу/дыхание/камеру, а не убирай.\n"
    "- Любые упоминания grid/сетки/мультяшности как способа обойти фильтр.\n\n"
    "СОХРАНИ структуру блоков промпта (BINDING/SUBJECT/ACTION/SCENE/CAMERA/DIALOGUE/…), все @ImageN "
    "теги в том же порядке, тайминги, камеру, свет, стиль. Если триггерных видео-элементов нет — "
    "верни промпт практически без изменений.\n\n"
    "Верни СТРОГО JSON и ничего кроме него:\n"
    '{"prompt": "переписанный motion prompt (та же структура)", '
    '"chunk_text": "chunk text без изменений, если правились только визуальные ремарки", '
    '"changes": ["изменение 1", ...], "reasoning": "одно предложение"}'
)

_DIALOGUE_MINIMAL_SYS = (
    "Модерация Seedance зарубила АУДИО-дорожку чанка (озвученные реплики). Голос — финальный "
    "продукт, поэтому его НЕ убираем; вместо этого сделай МИНИМАЛЬНУЮ точечную правку.\n\n"
    "Пройдись по репликам в кавычках и найди ТОЛЬКО те слова, что реально триггерят аудио-фильтр "
    "(мат, hate-лексика, явные угрозы убийством с упоминанием оружия и т.п.). Замени МИНИМАЛЬНО — "
    "одно-два слова на равные по силе, СОХРАНИВ длину реплики (критично для lip-sync), адресата, "
    "эмоцию и смысл. НИКОГДА не удаляй ни одной реплики — рискованную ПЕРЕФОРМУЛИРУЙ другими словами, "
    "сохранив смысл (реплики критичны для сюжета). НЕ переписывай реплику целиком без нужды. "
    "НЕ трогай реплики без триггеров. НЕ трогай "
    "описание действия, внешность, структуру промпта, @ImageN. Если явных триггеров в репликах нет — "
    "верни всё без изменений.\n\n"
    "Верни СТРОГО JSON и ничего кроме него:\n"
    '{"prompt": "промпт с минимально поправленными репликами (та же структура)", '
    '"chunk_text": "chunk text с теми же минимальными правками реплик", '
    '"changes": ["изменение 1", ...], "reasoning": "одно предложение"}'
)

# Stronger version of the action re-description — used when a mild re-describe
# already failed. Same hard rules (no evasion, no dialogue/appearance edits) but
# licensed to neutralize the visual trigger MORE thoroughly and to change the
# camera framing (wider / from behind / silhouette) so the sensitive detail is
# small, obscured, or off-frame — the community-endorsed legitimate move.
_REDESCRIBE_AGGRESSIVE_SYS = (
    _REDESCRIBE_ACTION_SYS
    + "\n\n═══ УСИЛЕННЫЙ РЕЖИМ (мягкое переописание уже не прошло) ═══\n"
    "Действуй решительнее: полностью УБЕРИ из кадра видимый триггер (оружие, кровь, "
    "рану, момент удара по телу) — покажи его КОСВЕННО (реакция, звук за кадром, "
    "тень, предмет вне фокуса) или ПЕРЕНЕСИ действие за границу кадра. ОБЯЗАТЕЛЬНО "
    "смени план на более общий / ракурс со спины / силуэт / съёмку через препятствие, "
    "чтобы чувствительная деталь была мелкой, размытой или вне кадра. Драматический "
    "смысл, эмоция и реплики сохраняются полностью; меняется только то, ЧТО и КАК "
    "показывает камера. По-прежнему НЕ трогай реплики, внешность и структуру промпта."
)


def _heal_chunk_via_claude(chunk, block_class='content'):
    """Scoped, legitimate heal of a chunk's prompt/chunk_text to clear a Seedance
    moderation block. Returns dict {prompt, chunk_text, changes, reasoning} or
    raises. Chooses the fix by block class — NEVER a catch-all word-swap:
      'audio'          → minimal offending-line edit (voice is the deliverable).
      'content'/'face' → ACTION/SCENE re-description only (dialogue+appearance
                         untouched). 'face' blocks can't really be cleared by a
                         prompt edit, but this is the closest legitimate attempt
                         when a caller asks for a heal anyway.
    Shared by the /heal-prompt button and the server recovery ladder."""
    if block_class == 'audio':
        sysprompt = _DIALOGUE_MINIMAL_SYS
        task = ("Найди в репликах триггеры аудио-фильтра и поправь их МИНИМАЛЬНО, "
                "сохранив длину и смысл. Дай список изменений на русском.")
    elif block_class == 'content_aggressive':
        sysprompt = _REDESCRIBE_AGGRESSIVE_SYS
        task = ("Мягкое переописание не прошло — УБЕРИ видимый триггер из кадра "
                "решительнее (косвенно/за кадром) и смени ракурс на общий/со спины/"
                "силуэт, не трогая реплики и внешность. Дай список изменений на русском.")
    else:
        sysprompt = _REDESCRIBE_ACTION_SYS
        task = ("Переопиши только ДЕЙСТВИЕ/СЦЕНУ визуально, чтобы убрать видео-триггер, "
                "не трогая реплики и внешность. Дай список изменений на русском.")
    error_hint = (chunk.get('error') or '').strip()[:300]
    user = (
        f"ОРИГИНАЛЬНЫЙ ПРОМПТ:\n```\n{(chunk.get('prompt') or '')}\n```\n\n"
        f"ОРИГИНАЛЬНЫЙ CHUNK TEXT:\n```\n{(chunk.get('chunk_text') or '')}\n```\n\n"
        f"ОШИБКА МОДЕРАЦИИ (что не пропустило): {error_hint or '(не указано)'}\n\n"
        f"{task}"
    )
    data = _ask_json(user, sysprompt)
    return {
        'prompt':     data.get('prompt') or chunk.get('prompt') or '',
        'chunk_text': data.get('chunk_text') or chunk.get('chunk_text') or '',
        'changes':    data.get('changes') or [],
        'reasoning':  data.get('reasoning') or '',
    }


def _deep_rewrite_prompt(sid, num, base):
    """Deep, series-aware rewrite of BOTH the composed motion prompt's ACTION/
    SCENE and the chunk_text so the scene no longer carries the moderation
    trigger — preserving dramatic function, series continuity, prompt block
    structure, @ImageN refs, dialogue and character appearance. Returns
    {prompt, chunk_text, changes, reasoning}. No evasion; voice/appearance kept."""
    s = load_series(sid) or {}
    ep = load_episode(sid, num) or {}
    full_script = ep.get('script', '') or ''
    prev_parts = []
    try:
        for prev in sorted(list_episodes(sid), key=lambda e: e['number']):
            if prev['number'] < num and prev.get('script'):
                prev_parts.append(f"Ep {prev['number']} (конец):\n{prev['script'][-1200:]}")
    except Exception:
        pass
    prev_block = ('\nПРЕДЫДУЩИЕ ЭПИЗОДЫ (конец):\n' + '\n---\n'.join(prev_parts[-2:]) + '\n') if prev_parts else ''
    cps = sorted((s.get('checkpoints') or []), key=lambda c: int(c.get('episode', 0)))
    cps_str = '\n'.join(f"  - Ep {c['episode']}: {(c.get('description') or '').strip()}"
                        for c in cps if c.get('description'))
    fin = s.get('finale') or {}
    fin_str = (f"Ep {fin.get('episode', '?')}: {fin.get('description', '')}" if fin.get('description') else '')
    sysprompt = (
        "Ты — сценарист короткой драмы. Перепиши конкретный чанк так, чтобы он ГАРАНТИРОВАННО "
        "прошёл модерацию ByteDance Seedance 2.0, сохранив драматическую функцию сцены и логику "
        "всей серии. БЕЗ обходов (никаких grid/сеток/мультяшности).\n\n"
        "УБРАТЬ из сцены: кровь/раны/порезы/синяки, удары по телу/лицу, удушение, оружие (даже "
        "упоминание в действии), обнажение/секс, явную смерть/агонию.\n"
        "ЗАМЕНИТЬ на: психологическое давление, холодное презрение, угрожающий жест/нависание, "
        "удар по предмету вместо человека, эмоциональный крах (слёзы, дрожь, срывающийся голос), "
        "более общий план / ракурс со спины.\n\n"
        "СОХРАНИТЬ: тех же персонажей, локацию, драматический исход и клиффхэнгер; ВСЕ реплики без "
        "исключения — НИ ОДНУ не удаляй и не теряй по смыслу; рискованную для модерации ПЕРЕФОРМУЛИРУЙ "
        "другими словами (смысл и сюжетную функцию сохранить ОБЯЗАТЕЛЬНО), длину можно чуть подогнать "
        "под lip-sync; ВНЕШНОСТЬ персонажей не менять; "
        "структуру блоков промпта (BINDING/SUBJECT/ACTION/SCENE/CAMERA/DIALOGUE/…) и все @ImageN "
        "теги в том же порядке.\n\n"
        "Тебе дают исходный motion-промпт и chunk_text — верни обновлённые ОБА, согласованные "
        "между собой.\n\n"
        "Верни СТРОГО JSON и ничего кроме него:\n"
        '{"prompt": "переписанный motion prompt (та же структура/теги)", '
        '"chunk_text": "переписанный фрагмент сценария", '
        '"changes": ["изменение 1", ...], "reasoning": "почему теперь пройдёт"}'
    )
    ctx = [f'Серия: "{s.get("title", "")}" | Жанр: {s.get("genre", "")} | Тон: {s.get("tone", "")}']
    if s.get('synopsis'):
        ctx.append(f'Синопсис серии: {s.get("synopsis")}')
    if s.get('arc'):
        ctx.append(f'Арка: {s.get("arc")}')
    if cps_str:
        ctx.append(f'Контрольные точки:\n{cps_str}')
    if fin_str:
        ctx.append(f'Финал серии: {fin_str}')
    if full_script:
        ctx.append(f'\nПОЛНЫЙ СЦЕНАРИЙ ЭПИЗОДА {num}:\n{full_script[:6000]}')
    if prev_block:
        ctx.append(prev_block)
    err_hint = (base.get('error') or '').strip()[:300]
    user = (
        '\n'.join(ctx) +
        f'\n\nИСХОДНЫЙ motion-ПРОМПТ:\n```\n{base.get("prompt") or ""}\n```\n'
        f'ИСХОДНЫЙ CHUNK TEXT:\n```\n{base.get("chunk_text") or ""}\n```\n'
        f'ОШИБКА МОДЕРАЦИИ: {err_hint or "(не указано)"}\n\n'
        'Перепиши сцену так, чтобы пройти модерацию, сохранив смысл, эмоцию и континьюити.'
    )
    data = _ask_json(user, sysprompt)
    return {
        'prompt':     data.get('prompt') or base.get('prompt') or '',
        'chunk_text': data.get('chunk_text') or base.get('chunk_text') or '',
        'changes':    data.get('changes') or [],
        'reasoning':  data.get('reasoning') or '',
    }


def _apply_mod_strategy(sid, num, action, base):
    """Produce (prompt, chunk_text, changes) for one recovery rung. Shared by the
    poll auto-ladder and the manual pass-moderation driver. Never evasion.
    'retry' resubmits unchanged; the rest are scoped Claude rewrites."""
    if action in ('retry', 'reduce_refs'):
        # No prompt change — 'retry' re-rolls the same prompt; 'reduce_refs'
        # keeps the prompt but the driver submits with fewer reference images.
        return base.get('prompt') or '', base.get('chunk_text') or '', []
    if action == 'deep_rewrite':
        r = _deep_rewrite_prompt(sid, num, base)
    else:
        bc = {'dialogue_min': 'audio', 'redescribe_aggressive': 'content_aggressive'}.get(action, 'content')
        r = _heal_chunk_via_claude(base, block_class=bc)
    return (r.get('prompt') or base.get('prompt') or '',
            r.get('chunk_text') or base.get('chunk_text') or '',
            r.get('changes') or [])


def _poll_job_until_terminal(job_id, status_url, timeout=480):
    """Poll an AVAI job in-thread until terminal. Returns 'pass' | 'block' |
    'fail'. 'block' = completed but the URL is a moderation placeholder (same
    heuristic as seedance_poll). Used by the manual pass-moderation driver."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = _avai_seedance_status(job_id, status_url)
        except Exception:
            time.sleep(6)
            continue
        status = (st.get('status') or '').lower()
        if status == 'completed':
            vurl = (st.get('video_url') or '').strip()
            vl = vurl.lower()
            is_block = ('moderation' in vl or vurl.startswith('/')
                        or not vl.startswith(('http://', 'https://'))
                        or not (vl.endswith('.mp4') or '.mp4?' in vl or '/video' in vl))
            if is_block:
                # Ground truth: log AVAI's raw response verbatim so we can finally
                # tell WHY it blocked (face/reference vs content) — the manual
                # driver path previously didn't capture this.
                try:
                    _log_event('INFO', 'seedance_block_raw', job_id=job_id,
                               video_url=vurl[:200],
                               raw=json.dumps(st.get('raw'), ensure_ascii=False)[:2500])
                except Exception:
                    pass
                return 'block'
            return 'pass'
        if status in ('failed', 'error'):
            try:
                _log_event('INFO', 'seedance_block_raw', job_id=job_id, poll_status=status,
                           error=str(st.get('error'))[:300],
                           raw=json.dumps(st.get('raw'), ensure_ascii=False)[:2500])
            except Exception:
                pass
            return 'fail'
        time.sleep(6)
    return 'fail'


def _pass_mod_fail(sid, num, idx, msg):
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return
        for cc in _seedance_chunks(ep):
            if cc.get('idx') == idx:
                cc['status'] = 'failed'
                cc['error'] = msg
                cc['mod_driver_active'] = False
                break
        save_episode(sid, num, ep)


def _pass_moderation_driver(sid, num, idx):
    """Manual «Попробовать обойти модерацию»: run the full LEGITIMATE arsenal
    automatically until the chunk passes or every rung is spent — no evasion,
    voice kept. Each rung: edit → submit → poll the job in-thread; on pass hand
    the job to the normal poll for download/QC. While driving we keep job_id OFF
    the chunk record so the frontend poll skips it (no ladder conflict). Cost is
    bounded by the AVAI circuit breaker + the fixed rung list."""
    ep = load_episode(sid, num)
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None) if ep else None
    if not chunk:
        return
    seq = _pass_mod_sequence(chunk.get('mod_block_class') or 'content')
    ref_urls = list(chunk.get('ref_urls') or [])
    duration = int(chunk.get('duration') or 15)
    resolution = chunk.get('resolution') or '720p'
    aspect = chunk.get('aspect_ratio') or '9:16'
    model = chunk.get('model') or 'reference-fast'
    mod = 'off'   # recovery NEVER evades — ignore any stale grid/collage/cartoon
                  # left on the chunk by the old auto-escalation (user: no bypass).
    for i, action in enumerate(seq):
        try:
            ep_b = load_episode(sid, num)
            cb = next((c for c in _seedance_chunks(ep_b) if c.get('idx') == idx), None) if ep_b else None
            if not cb:
                return
            base = {'prompt': cb.get('prompt') or '', 'chunk_text': cb.get('chunk_text') or '',
                    'error': cb.get('error') or 'Заблокировано модерацией Seedance'}
            with _episode_lock(sid, num):
                ep_h = load_episode(sid, num)
                for cc in _seedance_chunks(ep_h):
                    if cc.get('idx') == idx:
                        cc['status'] = 'moderation_blocked'
                        cc['progress'] = 0
                        cc.pop('job_id', None)
                        cc.pop('status_url', None)
                        cc['error'] = f'Обхожу модерацию — шаг {i + 1}/{len(seq)}: {_MOD_ACTION_LABEL.get(action, action)}…'
                        break
                save_episode(sid, num, ep_h)
            # Per-chunk lifetime submit cap — stop before spending another submit
            # on a chunk that has already had too many (kill-switch / budget guard).
            allowed, cnt = _reserve_chunk_submit(sid, num, idx)
            if not allowed:
                _pass_mod_fail(sid, num, idx,
                               f'Достигнут лимит {_CHUNK_SUBMIT_LIMIT} генераций на этот чанк — '
                               f'стоп (защита от переполнения лимита сабмитов Seedance). Нажми '
                               f'«🔓 Сбросить лимит», чтобы дать ещё {_CHUNK_SUBMIT_LIMIT} попыток.')
                return
            prompt, chunk_text, changes = _apply_mod_strategy(sid, num, action, base)
            with _episode_lock(sid, num):
                ep_h = load_episode(sid, num)
                for cc in _seedance_chunks(ep_h):
                    if cc.get('idx') == idx:
                        cc['prompt'] = prompt
                        cc['chunk_text'] = chunk_text
                        cc['heal_count'] = int(cc.get('heal_count') or 0) + 1
                        break
                save_episode(sid, num, ep_h)
            # 'reduce_refs' rung: drop all but the first 2 reference images (usually
            # the main characters) to test whether a specific ref photo is the trigger.
            rung_refs = ref_urls[:2] if (action == 'reduce_refs' and len(ref_urls) > 2) else ref_urls
            print(f'[pass-moderation] chunk {idx} rung {i + 1}/{len(seq)}: {action} → submit ({len(rung_refs)} refs)', flush=True)
            job = _avai_seedance_start(
                prompt=prompt, ref_urls=rung_refs, duration=duration, resolution=resolution,
                moderation_bypass=mod, aspect_ratio=aspect, generate_audio=True, model=model)
            # Expose job_id on the chunk NOW so the normal poll can download it as
            # a SAFETY NET even if this driver thread dies or times out (fixes
            # «passed in AVAI but stuck on 'обход' in the tool»). Keep status
            # 'moderation_blocked' + mod_driver_active so the poll does NOT run its
            # own escalation on a block — the driver owns that.
            with _episode_lock(sid, num):
                ep_j = load_episode(sid, num)
                for cc in _seedance_chunks(ep_j):
                    if cc.get('idx') == idx:
                        cc['job_id'] = job['job_id']
                        cc['status_url'] = job.get('status_url')
                        cc['mod_driver_active'] = True
                        break
                save_episode(sid, num, ep_j)
            result = _poll_job_until_terminal(job['job_id'], job.get('status_url'))
            if result == 'pass':
                with _episode_lock(sid, num):
                    ep_e = load_episode(sid, num)
                    for cc in _seedance_chunks(ep_e):
                        if cc.get('idx') == idx:
                            cc['job_id'] = job['job_id']
                            cc['status_url'] = job['status_url']
                            cc['status'] = 'pending'   # hand to normal poll for download + QC
                            cc['mod_ladder_step'] = 0
                            cc['mod_driver_active'] = False
                            cc['error'] = f'Прошло модерацию: {_MOD_ACTION_LABEL.get(action, action)}.'
                            break
                    save_episode(sid, num, ep_e)
                print(f'[pass-moderation] chunk {idx} PASSED via {action}', flush=True)
                return
            print(f'[pass-moderation] chunk {idx} rung {action} → {result}, next', flush=True)
        except AVAICircuitBreakerError as cbe:
            _pass_mod_fail(sid, num, idx, f'Обход модерации остановлен circuit breaker\'ом: {cbe}')
            return
        except Exception as e:
            print(f'[pass-moderation] chunk {idx} rung {action} error: {e}', flush=True)
            continue
    _pass_mod_fail(sid, num, idx,
                   'Не удалось провести через модерацию легитимными способами (переописание → '
                   'усиленное переописание → глубокий рерайт), без обходов. Убери из этого '
                   'сегмента оружие/кровь/жёсткое насилие/NSFW или слишком реалистичное лицо в '
                   'референсе и запусти заново.')


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/pass-moderation', methods=['POST'])
def seedance_pass_moderation(sid, num, idx):
    """One-click «Попробовать обойти модерацию»: kick off the background driver
    that runs the full legitimate recovery arsenal (re-describe → aggressive →
    deep rewrite) until the chunk passes. No evasion, voice kept. Returns
    immediately; progress shows on the chunk card via the normal poll."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
    if not chunk:
        return jsonify({'error': 'chunk not found'}), 404
    with _episode_lock(sid, num):
        ep2 = load_episode(sid, num)
        for cc in _seedance_chunks(ep2):
            if cc.get('idx') == idx:
                cc['status'] = 'moderation_blocked'
                cc['progress'] = 0
                cc.pop('job_id', None)
                cc.pop('status_url', None)
                cc['error'] = 'Запускаю авто-проход модерации…'
                break
        save_episode(sid, num, ep2)
    _spawn_with_keys(lambda: _pass_moderation_driver(sid, num, idx))
    return jsonify({'ok': True})


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/reset-submit-limit', methods=['POST'])
def seedance_reset_submit_limit(sid, num, idx):
    """Zero the per-chunk lifetime submit counter so «pass moderation» / retries
    can run again — the same _CHUNK_SUBMIT_LIMIT re-applies from zero."""
    with _episode_lock(sid, num):
        ep = load_episode(sid, num)
        if not ep:
            return jsonify({'error': 'not found'}), 404
        cc = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
        if not cc:
            return jsonify({'error': 'chunk not found'}), 404
        cc['submit_count'] = 0
        save_episode(sid, num, ep)
    return jsonify({'ok': True, 'submit_count': 0})


_HEAL_PROMPT_SYSPROMPT = (
    # Calibrated from a sibling production pipeline (colleague's Seedance/AVAI
    # project) — empirically proven substitution list that passes Seedance
    # classifier without losing drama. Replaces our earlier guideline-based
    # version which was less precise. Key insight from their prod data:
    # classifier scans BOTH action descriptions AND dialogue lines, hits on
    # keyword regardless of context (metaphorical or literal).
    "Перепиши этот Seedance-промпт чанка ЦЕЛИКОМ, чтобы он гарантированно прошёл "
    "классификатор модерации Seedance/fal.ai. Замени все слова и фразы высокой "
    "эмоциональной или физической интенсивности на квалитативные эквиваленты, но "
    "СОХРАНИ драматургию сцены, кадровую композицию и реплики.\n\n"
    "⚠️ ГЛАВНЫЙ ПРИНЦИП: НЕ ТЕРЯТЬ ЭМОЦИОНАЛЬНУЮ ЭНЕРГИЮ.\n"
    "Триггерные слова заменяй НЕ ослабляющими синонимами ('furious' → 'calm' — "
    "❌ запрещено), а ПЕРЕНОСИ интенсивность в физические/визуальные детали "
    "(глаза, челюсть, дыхание, поза, тон, поза рук). Зритель должен увидеть ту же "
    "ярость / шок / угрозу, но через то, что персонаж ДЕЛАЕТ телом.\n\n"
    "КОНКРЕТНЫЕ ЗАМЕНЫ (триггер слева → равнозначные по силе описания справа):\n"
    "• shocked / stunned → frozen mid-breath, eyes blown wide, lips parted, all colour drained from face\n"
    "• frozen (как замороженный) → rigid, breath caught in throat, hands locked at sides\n"
    "• shattered / devastated → eyes hollow, shoulders collapsed, breathing shallow, jaw slack\n"
    "• furious / raging / fury / rage → eyes blazing, jaw clenched hard, breath sharp through nostrils, knuckles white\n"
    "• screaming / shouting → voice raised to a roar, throat cords visible, jaw thrown wide, words hammered out\n"
    "• yelling → voice carrying across the room, sharp and cutting, jaw tight\n"
    "• terrified / horrified → eyes white-rimmed, hand trembling visibly, breath shallow and rapid\n"
    "• violent / brutal / savage / extreme → ferocious, unflinching, surging with force\n"
    "• slap / strike across face → sharp open-palm contact at the cheek (медленнее, описательнее, но не теряет силы)\n"
    "• punch / hit → fist driven forward, full weight behind it\n"
    "• blood / bleeding → dark stain spreading, slick mark\n"
    "• gun / weapon (если не сюжетно критично) → small dark object in hand\n"
    "• dies / killed / death → falls still, eyes glazing, body gone slack\n"
    "• crying / weeping → tears streaming, breath ragged, shoulders shaking\n"
    "• intimate / sensual / bare / nude — убирать или менять на нейтральное (контекст одежды/позы)\n"
    "• hate (как чувство) → loathing in the eyes, lip curling\n\n"
    "ПРИНЦИП: если триггер был 'furious', и ты заменил на 'with controlled restraint' "
    "— это ХУЖЕ оригинала и **запрещено**. Правильно — 'eyes blazing, jaw clenched, "
    "knuckles white' — это РАВНО по силе или СИЛЬНЕЕ.\n\n"
    "Если в действии было 'screams' — итог должен по-прежнему передавать крик через "
    "тон, мимику и язык тела, а не превращаться в спокойную речь.\n\n"
    "РЕПЛИКИ В КАВЫЧКАХ ({Name} says: \"...\"):\n\n"
    "⚠️ ОБЯЗАТЕЛЬНО ПРОЙДИСЬ ПО КАЖДОЙ РЕПЛИКЕ И ПРОВЕРЬ НА СПИСОК ТРИГГЕРОВ НИЖЕ. "
    "Если хоть одно слово из списка есть в реплике — ЗАМЕНИ его. Не оставляй 'как есть' "
    "с мыслью 'ну это же мягко'. Классификатор Seedance не различает контекст — он бьёт "
    "по словарю (empirically verified on colleague's production pipeline).\n\n"
    "ОБЯЗАТЕЛЬНЫЕ ЗАМЕНЫ В РЕПЛИКАХ (если встречается слева — поменяй на правое):\n"
    "• 'ass' (в любом контексте: your ass, my ass, kick ass) → 'skin' / 'neck' / убрать слово.\n"
    "  Примеры: 'saved your ass' → 'saved your skin'; 'kick your ass' → 'wreck you'; 'my ass!' → 'the hell I will!'\n"
    "• 'Shut up' / 'Shut your mouth' → 'Enough!' / 'Quiet now!' / 'Stop right there!'\n"
    "• 'fuck' / 'fucking' / 'fucked' → 'hell' / 'bloody' / 'twisted' / убрать.\n"
    "  Примеры: 'fucking idiot' → 'bloody fool'; 'fuck you' → 'to hell with you'\n"
    "• 'damn' / 'goddamn' → 'bloody' / 'cursed' / убрать\n"
    "• 'bastard' → 'snake' / 'worm' / 'rat'\n"
    "• 'bitch' → 'snake' / 'viper' / убрать\n"
    "• 'kill' / 'I'll kill you' → 'end' / 'I'll end you' / 'You're finished'\n"
    "• 'die' / 'dead' → 'fall' / 'gone' / 'won't be back'\n"
    "• 'blood' / 'bleeding' → 'marks' / 'stained'\n"
    "• 'smash' / 'beat' / 'punch' → 'wreck' / 'break' / 'shatter (метафорически)'\n"
    "• 'hate' → 'despise' / 'loathe'\n"
    "• 'stupid' / 'idiot' / 'moron' — обычно проходит, не трогай если контекст подходит\n\n"
    "ДЕПЕРСОНАЛИЗАЦИЯ И ПСИХОЛОГИЧЕСКОЕ НАСИЛИЕ В ДИАЛОГЕ:\n"
    "Seedance блокирует фразы которые буквально отрицают существование / идентичность человека, "
    "даже в контексте драмы. Найди и замени:\n"
    "• 'you don't exist' / 'you cease to exist' → 'you're only here when I call' / 'make yourself scarce'\n"
    "• 'you are invisible' / 'be invisible' / 'stay invisible' → 'stay in the background' / 'keep out of sight' / 'don't draw attention to yourself'\n"
    "• 'you are nothing' / 'you're nothing' → 'you're just here to do a job'\n"
    "• 'you don't matter' / 'you don't count' → 'your presence isn't required'\n"
    "• 'you are nobody' / 'you're nobody' → 'you're just staff'\n"
    "• 'you have no voice' / 'you have no say' → 'this isn't your decision'\n"
    "• 'you belong to me' / 'you're mine' (в контексте контроля) → 'you answer to me'\n"
    "• 'worthless' → 'replaceable' / 'expendable'\n"
    "• 'beneath me' / 'below me' (о человеке) → 'not at my level'\n"
    "• 'I own you' → 'I'm the one giving orders here'\n\n"
    "КЛЮЧЕВОЙ ПРИНЦИП для этой категории: сохрани власть и холодность говорящего, "
    "убери буквальное отрицание существования адресата. "
    "'You don't exist' → 'You're invisible to my guests' или 'Act like you're not here' — "
    "смысл тот же, но без прямого отрицания человека как такового.\n\n"
    "Сохраняй при замене:\n"
    "- Смысл и драматургическую функцию (угроза остаётся угрозой)\n"
    "- Примерную длину (плюс-минус 1-2 слова — КРИТИЧНО для lip-sync)\n"
    "- Адресата и эмоциональный регистр\n\n"
    "Если реплика после прохода по списку чистая — оставь дословно.\n\n"
    "ОБЯЗАТЕЛЬНО СОХРАНИТЬ:\n"
    "- Структуру блоков промпта (BINDING/SUBJECT/ACTION/SCENE/CAMERA/DIALOGUE/...)\n"
    "- Все @ImageN теги на тех же персонажах в том же порядке\n"
    "- Тайминги шотов\n"
    "- Описания одежды, причёски, позы, расположения\n"
    "- Camera framing, ракурсы, движение камеры\n"
    "- Освещение, атмосферу, стиль\n\n"
    "═══ ⚠️ КРИТИЧНО — ВНЕШНОСТЬ ПЕРСОНАЖЕЙ НЕ ТРОГАТЬ ═══\n\n"
    "ЗАПРЕЩЕНО изменять, добавлять или удалять ЛЮБЫЕ описания внешности персонажей. К внешности относятся:\n"
    "- ЛИЦО: цвет/форма глаз, ресницы, брови, нос, скулы, челюсть, губы, кожа, морщины, веснушки, шрамы, родинки, борода, усы\n"
    "- ВОЛОСЫ: цвет, длина, причёска, оттенок, фактура\n"
    "- ТЕЛО: рост, телосложение, татуировки\n\n"
    "Если в оригинале было 'pale skin and dark hair' — оставь дословно.\n"
    "Если оригинал НЕ описывал глаза персонажа — НЕ ВЫДУМЫВАЙ ('eyes blazing' можно ТОЛЬКО как реакцию, не как «icy blue eyes blazing»).\n\n"
    "Эмоции через лицо описывай ТОЛЬКО через действие, не через цвет/форму:\n"
    "- ✅ 'jaw clenched', 'lips parted', 'brows drawn together', 'tear tracks on the cheeks'\n"
    "- ❌ 'icy blue eyes blazing' — меняет цвет глаз, ломает консистентность лица с reference image.\n\n"
    "ПОЧЕМУ ВАЖНО: Seedance берёт лицо персонажа из reference-картинки @ImageN. Если в тексте появляется "
    "новое описание лица/глаз/волос, не совпадающее с картинкой — модель усредняет, и лицо 'плывёт'.\n\n"
    "ПРАВИЛО: если в исходном промпте про внешность сказано «X», в финальном должно быть РОВНО «X». "
    "Если ничего не сказано — ничего и не добавляй.\n\n"
    "═══════════════════════════════════════════════\n\n"
    "Если в исходнике слов высокой интенсивности нет — верни промпт практически без изменений.\n\n"
    "Верни СТРОГО JSON и ничего кроме него:\n"
    "{\n"
    '  "prompt": "переписанный motion prompt (английский, та же структура)",\n'
    '  "chunk_text": "переписанный фрагмент сценария (если были запрещённые элементы)",\n'
    '  "changes": ["изменение 1", "изменение 2", ...],\n'
    '  "reasoning": "одно предложение — что было критичного и как обошёл"\n'
    "}"
)


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/heal-prompt', methods=['POST'])
def seedance_heal_prompt(sid, num, idx):
    """User-facing endpoint: heal a chunk's prompt + chunk_text to pass moderation.
    Delegates to _heal_chunk_via_claude. Returns the healed prompt for the
    frontend to display/re-submit. Increments heal_count for UI gating."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
    if not chunk:
        return jsonify({'error': 'chunk not found'}), 404
    try:
        result = _heal_chunk_via_claude(chunk, block_class=chunk.get('mod_block_class') or 'content')
    except Exception as e:
        return jsonify({'error': f'heal failed: {e}'}), 500
    with _episode_lock(sid, num):
        ep2 = load_episode(sid, num)
        chunk2 = next((c for c in _seedance_chunks(ep2) if c.get('idx') == idx), None)
        if chunk2 is not None:
            chunk2['heal_count'] = int(chunk2.get('heal_count') or 0) + 1
            save_episode(sid, num, ep2)
    return jsonify(result)


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/rewrite-chunk', methods=['POST'])
def seedance_rewrite_chunk(sid, num, idx):
    """Deep-rewrite a chunk's scene content with full series context to pass moderation.
    Unlike heal-prompt (surface prompt tweaks), this rewrites the underlying script
    from scratch while preserving dramatic function and series continuity."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
    if not chunk:
        return jsonify({'error': 'chunk not found'}), 404

    # Build series context
    episodes = sorted(list_episodes(sid), key=lambda e: e['number'])
    full_script = ep.get('script', '')

    # Last 2 previous episodes for continuity
    prev_scripts_parts = []
    for prev in episodes:
        if prev['number'] < num and prev.get('script'):
            prev_scripts_parts.append(f"Ep {prev['number']} (конец):\n{prev['script'][-1500:]}")
    prev_scripts_block = ('\nПРЕДЫДУЩИЕ ЭПИЗОДЫ (конец скрипта):\n'
                          + '\n---\n'.join(prev_scripts_parts[-2:]) + '\n') if prev_scripts_parts else ''

    # Checkpoints and finale
    cps = sorted((s.get('checkpoints') or []), key=lambda c: int(c.get('episode', 0)))
    cps_str = '\n'.join(f"  - Ep {c['episode']}: {(c.get('description') or '').strip()}"
                        for c in cps if c.get('description'))
    fin = s.get('finale') or {}
    fin_str = (f"Ep {fin.get('episode','?')}: {fin.get('description','')}"
               if fin.get('description') else '')

    sysprompt = (
        "Ты — сценарист короткой драмы. Твоя задача — переписать конкретный фрагмент эпизода "
        "так, чтобы он ГАРАНТИРОВАННО прошёл модерацию ByteDance Seedance 2.0, сохранив "
        "драматическую функцию сцены и не нарушив логику всей серии.\n\n"
        "ЧТО ОБЯЗАТЕЛЬНО УБРАТЬ ИЗ НОВОГО ВАРИАНТА:\n"
        "  • Кровь, раны, царапины, порезы, синяки, любые видимые физические повреждения\n"
        "  • Удары в лицо, кулаком, по голове, удары ногой\n"
        "  • Удушение, захват за горло, пытки, издевательства с физическим контактом\n"
        "  • Оружие (нож, пистолет, бита, любое оружие — даже упоминание в действии)\n"
        "  • Обнажение, откровенные сексуальные сцены\n"
        "  • Явная смерть, суицид, описание агонии\n\n"
        "ЧТО ИСПОЛЬЗОВАТЬ ВМЕСТО:\n"
        "  • Психологическое давление, унижение через слова, холодное презрение\n"
        "  • Пощёчина без следа → замена: толчок в плечо / резкий хват за запястье\n"
        "  • Угрожающий жест + нависание, нарушение личного пространства\n"
        "  • Удар по предмету (стол, стена) вместо удара по человеку\n"
        "  • Эмоциональный крах: слёзы, шок, дрожание рук, голос срывается\n\n"
        "ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ:\n"
        "  • Те же персонажи, та же локация, тот же драматический исход сцены\n"
        "  • Не нарушить логику серии — продолжить из того же состояния, прийти к тому же сюжетному результату\n"
        "  • Формат: ремарки на русском, диалоги в кавычках как были\n"
        "  • Клиффхэнгер / финал сцены сохранить\n\n"
        "Верни СТРОГО JSON и ничего кроме него:\n"
        '{"chunk_text": "переписанный фрагмент", '
        '"changes": ["изменение 1", "изменение 2", ...], '
        '"reasoning": "почему новый вариант пройдёт модерацию"}'
    )

    context_parts = [
        f'Серия: "{s["title"]}" | Жанр: {s.get("genre", "")} | Тон: {s.get("tone", "")}\n',
        f'Синопсис серии: {s.get("synopsis", "")}\n',
        f'Арка: {s.get("arc", "")}\n',
    ]
    if cps_str:
        context_parts.append(f'Контрольные точки:\n{cps_str}\n')
    if fin_str:
        context_parts.append(f'Финал серии: {fin_str}\n')
    if full_script:
        context_parts.append(f'\nПОЛНЫЙ СЦЕНАРИЙ ЭПИЗОДА {num}:\n{full_script}\n')
    if prev_scripts_block:
        context_parts.append(prev_scripts_block)

    error_hint = (chunk.get('error') or '').strip()[:300]
    user = (
        ''.join(context_parts) +
        f'\nФРАГМЕНТ ДЛЯ ПЕРЕПИСКИ (chunk #{idx}):\n```\n{chunk.get("chunk_text", "")}\n```\n\n'
        f'ОШИБКА МОДЕРАЦИИ: {error_hint or "(не указано — убери любые потенциально блокируемые элементы)"}\n\n'
        'Перепиши этот фрагмент так, чтобы он прошёл модерацию ByteDance, сохранив смысл и эмоцию сцены. '
        'Учти полный сценарий эпизода — новый вариант должен органично вписываться в него.'
    )

    try:
        raw = claude_ask(user, system=sysprompt)
        data = json.loads(strip_json(raw))
    except Exception as e:
        return jsonify({'error': f'rewrite failed: {e}'}), 500

    return jsonify({
        'chunk_text': data.get('chunk_text') or chunk.get('chunk_text') or '',
        'changes':    data.get('changes') or [],
        'reasoning':  data.get('reasoning') or '',
    })


@app.route('/api/series/<sid>/episodes/<int:num>/seedance/<int:idx>/ending_state', methods=['PATCH'])
def seedance_patch_ending(sid, num, idx):
    ep = load_episode(sid, num)
    if not ep:
        return jsonify({'error': 'not found'}), 404
    chunk = next((c for c in _seedance_chunks(ep) if c.get('idx') == idx), None)
    if not chunk:
        return jsonify({'error': 'not found'}), 404
    chunk['ending_state'] = ((request.json or {}).get('ending_state') or '')[:400]
    save_episode(sid, num, ep)
    return jsonify({'chunk': chunk})
