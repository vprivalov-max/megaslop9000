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
                         _download_video, _purge_continuity_sidecars,
                         _qc_run_chunk, _resolve_ref_url, _seedance_chunks,
                         _seedance_moderation_precheck)
from sw.storage import (list_episodes, load_episode, load_series, save_episode,
                        series_path, vid_dir)
from sw.textrules_sanitizer import _sanitize_appearance_for_moderation

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
    #       («Fox Woman lies under the car») — Layer C (NEW)
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
                    # Auto-escalation: AVAI returned moderation rejection.
                    # Behaviour (revised 2026-05-19 after $600 incident):
                    #   1. Mark chunk visibly as 'moderation_blocked' so user
                    #      SEES it was rejected BEFORE we attempt anything.
                    #   2. ONCE per chunk — heal the prompt via Claude (rewrite
                    #      softer) and re-submit with collage_grid bypass.
                    #   3. If THAT also hits moderation → final fail, surface
                    #      explicit message to user. Never escalate twice.
                    #
                    # Hard cap is enforced by `mod_escalated` flag (single use)
                    # AND by global circuit breaker (3 same-fp per 10 min) so
                    # any future bug can't loop more than 3× ($1).
                    already_escalated = bool(c.get('mod_escalated'))
                    cur_mod = (c.get('moderation_bypass') or 'off').lower()
                    if not already_escalated and cur_mod in ('off', 'cartoon'):
                        # Mark mod_escalated FIRST and save BEFORE setting
                        # _pending flag. Idempotent — even if subsequent steps
                        # crash, mod_escalated=True prevents re-attempt.
                        c['mod_escalated'] = True
                        c['moderation_bypass'] = 'collage_grid'
                        c['status'] = 'moderation_blocked'   # visible intermediate state
                        c['progress'] = 0
                        c['error'] = (
                            f'Заблокировано модерацией Seedance ({vurl[:60]}). '
                            f'Лечу промпт и пробую ещё раз с collage_grid bypass...'
                        )
                        c.pop('job_id', None)
                        c.pop('status_url', None)
                        c['_pending_mod_escalation'] = True
                        changed = True
                        print(f'[seedance_poll] chunk {c.get("idx")} hit moderation with bypass={cur_mod} → marking moderation_blocked + queueing heal+escalate', flush=True)
                        continue
                    # Already escalated — give up, surface to user.
                    c['status'] = 'failed'
                    c['error'] = (
                        f'Заблокировано модерацией Seedance даже после heal+collage_grid '
                        f'(вернул "{vurl[:60]}"). Перепиши промпт мягче руками — убери: '
                        f'царапины/раны/кровь, удары в лицо, обнажение, оружие, явное '
                        f'насилие. Переформулируй действие через эмоцию вместо физического урона.'
                    )
                    changed = True
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
        # Snapshot moderation-escalation candidates: chunks where we just
        # bumped bypass to collage_grid and wiped job_id. Need to spawn a
        # fresh AVAI submit out-of-lock — the second attempt at the same
        # chunk slot (not a new idx, so QC retry-counter doesn't see them
        # as siblings).
        mod_escalations = []
        for c in chunks:
            if c.pop('_pending_mod_escalation', False):
                mod_escalations.append({
                    'idx': c.get('idx'),
                    'prompt': c.get('prompt') or '',
                    'chunk_text': c.get('chunk_text') or '',
                    'ref_urls': list(c.get('ref_urls') or []),
                    'duration': int(c.get('duration') or 15),
                    'resolution': c.get('resolution') or '720p',
                    'aspect_ratio': c.get('aspect_ratio') or '9:16',
                    'model': c.get('model') or 'reference-fast',
                })
        # CRITICAL: persist the popped flags. Without this, the prior save at
        # `if changed: save_episode` ran BEFORE this pop, so the disk still
        # holds `_pending_mod_escalation=True` → next poll re-fires escalation
        # → infinite escalation submits every 8s (real prod bug
        # «Vice Beasts» ep 1, 2026-05-19). One-line fix: re-save after popping.
        if mod_escalations:
            save_episode(sid, num, ep)

    # ── Out of lock — dispatch heal+escalation per chunk (one-shot per chunk)
    if mod_escalations:
        for esc in mod_escalations:
            def _esc_runner(esc=esc):
                try:
                    # 1. Heal the prompt via Claude (rewrite softer)
                    print(f'[seedance_poll] chunk {esc["idx"]} heal+escalate: calling heal-prompt...', flush=True)
                    healed = _heal_chunk_via_claude({
                        'prompt': esc['prompt'],
                        'chunk_text': esc.get('chunk_text', ''),
                        'error': 'Заблокировано модерацией Seedance — переписать без насилия/крови/оружия',
                    })
                    healed_prompt = healed.get('prompt') or esc['prompt']
                    healed_chunk_text = healed.get('chunk_text') or esc.get('chunk_text', '')
                    # 2. Persist healed prompt to chunk BEFORE submit so the UI
                    #    can show it. Also increment heal_count.
                    with _episode_lock(sid, num):
                        ep_h = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_h):
                            if cc.get('idx') == esc['idx']:
                                cc['prompt'] = healed_prompt
                                cc['chunk_text'] = healed_chunk_text
                                cc['heal_count'] = int(cc.get('heal_count') or 0) + 1
                                cc['error'] = (
                                    f'Заблокировано модерацией → промпт вылечен, пересабмит с collage_grid. '
                                    f'Изменения: {"; ".join((healed.get("changes") or [])[:3])}'
                                )
                                break
                        save_episode(sid, num, ep_h)
                    # 3. Submit healed prompt with collage_grid bypass
                    print(f'[seedance_poll] chunk {esc["idx"]} submitting healed prompt with collage_grid', flush=True)
                    job = _avai_seedance_start(
                        prompt=healed_prompt, ref_urls=esc['ref_urls'],
                        duration=esc['duration'], resolution=esc['resolution'],
                        moderation_bypass='collage_grid',
                        aspect_ratio=esc['aspect_ratio'],
                        generate_audio=True,
                        moderation_bypass_prompt=(
                            "Final output MUST be a single continuous full-frame composition. "
                            "NO visible grid lines, NO cell borders, NO tiling artifacts, NO "
                            "panel separators, NO visible seams. Any grid used internally for "
                            "moderation bypass must be fully removed from the rendered output."
                        ),
                        model=esc['model'],
                    )
                    with _episode_lock(sid, num):
                        ep_e = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_e):
                            if cc.get('idx') == esc['idx']:
                                cc['job_id'] = job['job_id']
                                cc['status_url'] = job['status_url']
                                cc['status'] = 'pending'   # flip from moderation_blocked → pending now that new job exists
                                break
                        save_episode(sid, num, ep_e)
                except AVAICircuitBreakerError as cbe:
                    print(f'[seedance_poll] escalation BLOCKED by circuit breaker for chunk {esc["idx"]}: {cbe}', flush=True)
                    with _episode_lock(sid, num):
                        ep_e = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_e):
                            if cc.get('idx') == esc['idx']:
                                cc['status'] = 'failed'
                                cc['error'] = f'auto-escalation отказано circuit breaker\'ом: {cbe}'
                                break
                        save_episode(sid, num, ep_e)
                except Exception as e:
                    print(f'[seedance_poll] heal+escalation FAILED for chunk {esc["idx"]}: {e}', flush=True)
                    with _episode_lock(sid, num):
                        ep_e = load_episode(sid, num)
                        for cc in _seedance_chunks(ep_e):
                            if cc.get('idx') == esc['idx']:
                                cc['status'] = 'failed'
                                cc['error'] = f'heal+escalation submit failed: {e}'
                                break
                        save_episode(sid, num, ep_e)
            _spawn_with_keys(_esc_runner)

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

def _heal_chunk_via_claude(chunk):
    """Heal a chunk's prompt + chunk_text via Claude to pass Seedance moderation.
    Returns dict {prompt, chunk_text, changes, reasoning} or raises.

    Shared between the user-facing /heal-prompt endpoint and the server-side
    auto-escalation runner (post-moderation single-shot heal). Single source
    of truth for the heal sysprompt + Claude call."""
    sysprompt = _HEAL_PROMPT_SYSPROMPT
    error_hint = (chunk.get('error') or '').strip()[:300]
    user = (
        f"ОРИГИНАЛЬНЫЙ ПРОМПТ:\n```\n{(chunk.get('prompt') or '')}\n```\n\n"
        f"ОРИГИНАЛЬНЫЙ CHUNK TEXT:\n```\n{(chunk.get('chunk_text') or '')}\n```\n\n"
        f"ОШИБКА МОДЕРАЦИИ (что не пропустило): {error_hint or '(не указано — обработай оба текста на любые потенциально блокируемые элементы)'}\n\n"
        "Найди и замени блокирующие элементы. Дай список изменений на русском."
    )
    raw = claude_ask(user, system=sysprompt)
    data = json.loads(strip_json(raw))
    return {
        'prompt':     data.get('prompt') or chunk.get('prompt') or '',
        'chunk_text': data.get('chunk_text') or chunk.get('chunk_text') or '',
        'changes':    data.get('changes') or [],
        'reasoning':  data.get('reasoning') or '',
    }


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
        result = _heal_chunk_via_claude(chunk)
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
