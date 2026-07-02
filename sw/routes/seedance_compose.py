"""Seedance compose route: builds the full generation prompt for one chunk
(refs, blocking, continuity, undress states, pose lock, image-tag remap)."""
import json
import re

from flask import jsonify, request

from sw.cast import _char_name_in_text
from sw.config import STRICT_CHAR_FILTER
from sw.core import app
from sw.imagetags import _build_image_tag_remap, _remap_image_tags
from sw.jsonutils import strip_json
from sw.llm import claude_ask, claude_ask_vision
from sw.logging_utils import _log_event
from sw.scriptparse import is_scene_heading
from sw.seedance import (_avai_upload_local_image, _detect_cuts,
                         _extract_keyframes_at_cuts, _extract_last_frame,
                         _resolve_ref_url, _seedance_chunks)
from sw.storage import load_episode, load_series, save_episode, series_path
from sw.textrules_banlists import _extract_script_blocking
from sw.textrules_sanitizer import (_DRESSED_OUTFIT_RE, _UNDRESSED_OUTFIT_RE,
                                    _binding_desc_with_undress,
                                    _canonical_char_description,
                                    _detect_char_undressed_states,
                                    _undress_state_clothing)
from sw.anthro import _override_vision_with_script_poses

@app.route('/api/series/<sid>/episodes/<int:num>/seedance/compose', methods=['POST'])
def seedance_compose(sid, num):
    """Claude composes prompt + picks refs from a script chunk."""
    s = load_series(sid)
    ep = load_episode(sid, num)
    if not s or not ep:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    chunk_text = (body.get('chunk_text') or '').strip()
    if not chunk_text:
        return jsonify({'error': 'chunk_text required'}), 400
    use_prev_lastframe = bool(body.get('use_prev_lastframe', True))
    use_prev_cutframes = bool(body.get('use_prev_cutframes', False))
    base_outfits_only  = bool(body.get('base_outfits_only', False))
    close_up_only      = bool(body.get('close_up_only', False))
    # Optional: caller hands in a FIXED list of refs (kind/id/outfit) — Claude
    # must use ONLY these as the visible roster. Used by "🔄 Перекомпоновать
    # с текущими рефами" — user manually pruned some refs and wants the prompt
    # rewritten without the removed ones.
    locked_refs = body.get('locked_refs') or []
    style_override = (body.get('style') or '').strip()
    # Fall back to project's visual_style (default = realistic — no special handling needed)
    if not style_override:
        proj_style = (s.get('visual_style') or '').strip()
        if proj_style:
            style_override = proj_style

    # ── Adjacent-chunk continuity ─────────────────────────────────────────
    # Find chunks that sit BEFORE / AFTER the current chunk_text in the FULL
    # script (by substring position), not by creation order. This is what
    # actually matters for "who was just in frame" continuity.
    full_script_for_pos = (ep.get('script') or '')
    # Use the shared is_scene_heading() helper — recognises BOTH formal
    # (INT./EXT./ИНТ./...) AND inferred (Локация:, СЦЕНА N, ALL-CAPS slug,
    # [bracketed slug]) headings so continuity survives in scripts without INT./EXT.
    def _pos_in_script(txt):
        """Find the script byte-offset where this chunk_text starts.
        Tries multiple anchors and prefers the FIRST unique match — falls back
        to any match. Skips scene headings and very short lines as anchors
        because those tend to repeat across the script."""
        if not txt or not full_script_for_pos:
            return -1
        cands = []
        for ln in txt.splitlines():
            s = ln.strip()
            if len(s) < 25:
                continue
            if is_scene_heading(s):
                continue
            cands.append(s)
            if len(cands) >= 6:
                break
        # Pass 1: prefer anchors that match exactly once in the script
        for c in cands:
            idx = full_script_for_pos.find(c)
            if idx == -1:
                continue
            if full_script_for_pos.find(c, idx + 1) == -1:
                return idx
        # Pass 2: any anchor that matches at all
        for c in cands:
            idx = full_script_for_pos.find(c)
            if idx != -1:
                return idx
        return -1
    def _range_in_script(txt):
        """Return (start, end) byte-offsets of the chunk in the script.
        End is estimated via the LAST long unique-ish anchor inside the chunk
        (so chunks that share their first line still get distinct ranges)."""
        start = _pos_in_script(txt)
        if start < 0:
            return -1, -1
        # Walk text lines in reverse to find the last anchor we can locate
        # forward from `start`.
        for ln in reversed(txt.splitlines()):
            s = ln.strip()
            if len(s) < 25:
                continue
            if is_scene_heading(s):
                continue
            idx = full_script_for_pos.find(s, start)
            if idx >= 0:
                return start, idx + len(s)
        return start, start + len(txt)

    cur_start, cur_end = _range_in_script(chunk_text)
    cur_pos = cur_start  # keep old name for downstream code
    raw_chunks = _seedance_chunks(ep)
    # Dedupe by script_order BEFORE neighbour selection. When the user retries
    # a chunk, both v1 and v2 sit in seedance_chunks with the same script_order
    # — the older v1 still carries `cutframes_avai_urls` / `lastframe_avai_url`
    # cached on its record. If we let v1 win the neighbour race (which happens
    # when its `end_pos` in script is even 1 char longer than v2's), the next
    # chunk's compose attaches v1's stale frames as continuity refs and the
    # new generation inherits the old costumes / characters / mise-en-scène.
    # User-reported on «My Roommate From Craigslist Is Hunting Me»: pre-cut
    # frames showed an old (since-replaced) character's wardrobe.
    # Defense layer 2 — also drop chunks whose mp4 file is gone from disk;
    # they can't supply real continuity frames and are lingering orphans.
    by_so = {}
    no_so = []
    for c in raw_chunks:
        vp = c.get('video_path')
        if vp and not (series_path(sid) / vp).exists():
            continue  # orphan: video deleted but record lingered
        so = c.get('script_order')
        if isinstance(so, int):
            prev = by_so.get(so)
            if prev is None:
                by_so[so] = c
            else:
                cur_key  = (c.get('idx') or 0, c.get('created_at') or 0)
                prev_key = (prev.get('idx') or 0, prev.get('created_at') or 0)
                if cur_key > prev_key:
                    by_so[so] = c
        else:
            no_so.append(c)
    # Legacy retries created before script_order was propagated land in no_so
    # but their chunk_text matches a by_so entry verbatim — same script slot,
    # different retry. Pick the newest.
    chunk_text_to_so = {
        (by_so[k].get('chunk_text') or '').strip(): k
        for k in by_so
        if (by_so[k].get('chunk_text') or '').strip()
    }
    truly_orphan = []
    for c in no_so:
        ct = (c.get('chunk_text') or '').strip()
        so = chunk_text_to_so.get(ct) if ct else None
        if so is not None:
            prev = by_so[so]
            cur_key  = (c.get('idx') or 0, c.get('created_at') or 0)
            prev_key = (prev.get('idx') or 0, prev.get('created_at') or 0)
            if cur_key > prev_key:
                by_so[so] = c
        else:
            truly_orphan.append(c)
    all_chunks = list(by_so.values()) + truly_orphan
    located = []
    for c in all_chunks:
        sp, ep_ = _range_in_script(c.get('chunk_text') or '')
        if sp >= 0:
            located.append((sp, ep_, c))
    prev_neighbour = None
    next_neighbour = None
    prev_neighbour_ep = num         # which episode prev_neighbour belongs to
    prev_neighbour_obj = ep         # the loaded episode dict (for save_episode)
    # B3: helper — last scene-heading byte offset at or before given pos.
    # Used to weight same-scene candidates above cross-scene ones in
    # `_prev_score` so parallel auto-mode / retry timing can't put a chunk
    # from the previous scene ahead of the just-pending same-scene one.
    def _last_scene_heading_before(pos):
        if pos <= 0:
            return -1
        last = -1
        cursor = 0
        for ln in full_script_for_pos.split('\n'):
            if cursor > pos:
                break
            if is_scene_heading(ln):
                last = cursor
            cursor += len(ln) + 1
        return last
    cur_scene_anchor = _last_scene_heading_before(cur_start) if cur_start >= 0 else -1

    if cur_start >= 0:
        # PREV = chunk whose END is closest to (but ≤) the current chunk's START.
        # PRIMARY tiebreak: same-scene (no scene heading between cur and candidate).
        # On scoring ties (regenerations of the same fragment): prefer completed,
        # then prefer the most recently created.
        def _prev_score(c, end_pos):
            same_scene = (_last_scene_heading_before(end_pos) == cur_scene_anchor)
            return (
                1 if same_scene else 0,                     # 1) same scene wins HARD
                end_pos,                                    # 2) max end position
                1 if c.get('status') == 'completed' else 0, # 3) completed wins
                int(c.get('created_at') or 0),              # 4) newer wins
            )
        # Compose-time chunk_text equality check — covers the case where retry
        # chunks share text with what user is composing right now (or with each
        # other). Without this, prev_neighbour could pick a duplicate-segment
        # chunk that overlaps the current one. Bug: «The Fox CEO's Trap» ep 1
        # — 7 chunks shared «The Raccoon steps closer...» text; compose of a
        # later segment picked one of those as «prev» because its byte range
        # differed by a few chars due to trailing-newline trim.
        cur_text_norm = (chunk_text or '').strip()
        best_prev = None  # (score_tuple, chunk)
        for sp, ep_pos, c in located:
            c_text_norm = (c.get('chunk_text') or '').strip()
            if c_text_norm == cur_text_norm:
                continue   # same segment (incl. retries of current)
            if sp == cur_start and ep_pos == cur_end:
                continue   # same range
            if ep_pos <= cur_start + 5:           # tolerate tiny overlap of 5 chars
                sc = _prev_score(c, ep_pos)
                if best_prev is None or sc > best_prev[0]:
                    best_prev = (sc, c)
        if best_prev:
            prev_neighbour = best_prev[1]
        # NEXT = smallest start > cur_end (ties: completed > newer)
        def _next_score(c, sp):
            # Negate sp so smaller start = higher score under > comparison
            return (
                -sp,
                1 if c.get('status') == 'completed' else 0,
                int(c.get('created_at') or 0),
            )
        best_next = None
        for sp, ep_pos, c in located:
            if sp >= cur_end - 5 and sp != cur_start:
                sc = _next_score(c, sp)
                if best_next is None or sc > best_next[0]:
                    best_next = (sc, c)
        if best_next:
            next_neighbour = best_next[1]
    elif located:
        # Fallback: chunk_text not found in script (edited?) — use last by creation
        prev_neighbour = all_chunks[-1] if all_chunks else None

    # ── Cross-episode continuity ──────────────────────────────────────────
    # If we're at the start of this episode (no PREVIOUS chunk found in current
    # ep), pull the LAST chunk of episode N-1 by its position in that script.
    # This makes Seedance compose aware of what just happened on screen even
    # across episode boundaries.
    if not prev_neighbour and num > 1:
        prev_ep_obj_x = load_episode(sid, num - 1)
        if prev_ep_obj_x:
            prev_ep_script = (prev_ep_obj_x.get('script') or '')
            def _pos_in_prev(txt):
                if not txt: return -1
                anchor = next((ln.strip() for ln in txt.splitlines() if len(ln.strip()) > 25), txt[:80].strip())
                return prev_ep_script.find(anchor)
            # Same dedup as in-episode neighbour selection: drop orphaned
            # records whose mp4 is gone, then keep only the newest take per
            # script_order so a stale v1 doesn't supply continuity frames.
            raw_prev = _seedance_chunks(prev_ep_obj_x)
            by_so_p = {}
            no_so_p = []
            for c in raw_prev:
                vp = c.get('video_path')
                if vp and not (series_path(sid) / vp).exists():
                    continue
                so = c.get('script_order')
                if isinstance(so, int):
                    prev = by_so_p.get(so)
                    if prev is None:
                        by_so_p[so] = c
                    else:
                        cur_key  = (c.get('idx') or 0, c.get('created_at') or 0)
                        prev_key = (prev.get('idx') or 0, prev.get('created_at') or 0)
                        if cur_key > prev_key:
                            by_so_p[so] = c
                else:
                    no_so_p.append(c)
            prev_ep_chunks = list(by_so_p.values()) + no_so_p
            located_prev = []
            for c in prev_ep_chunks:
                p = _pos_in_prev(c.get('chunk_text') or '')
                if p >= 0:
                    located_prev.append((p, c))
            if located_prev:
                located_prev.sort(key=lambda x: x[0])
                prev_neighbour = located_prev[-1][1]      # latest by script pos
            elif prev_ep_chunks:
                prev_neighbour = prev_ep_chunks[-1]       # fallback by creation order
            if prev_neighbour:
                prev_neighbour_ep = num - 1
                prev_neighbour_obj = prev_ep_obj_x

    # Surface which prev_neighbour got picked so user can see why continuity
    # refs landed (or didn't) for this compose. Diagnostic only.
    print(f'[seedance_compose] composing chunk_text head={(chunk_text or "")[:80]!r} script_range=({cur_start},{cur_end})', flush=True)
    if prev_neighbour:
        prev_head = (prev_neighbour.get('chunk_text') or '')[:80]
        print(f'[seedance_compose] prev_neighbour: idx={prev_neighbour.get("idx")} ep={prev_neighbour_ep} so={prev_neighbour.get("script_order")} status={prev_neighbour.get("status")} vp={prev_neighbour.get("video_path")} text_head={prev_head!r}', flush=True)
    else:
        print(f'[seedance_compose] prev_neighbour: NONE (first chunk in ep or no in-ep candidate matched)', flush=True)

    def _summarize_neighbour(c, where, ep_label=None):
        if not c:
            return ''
        bits = []
        ep_tag = f", from EP {ep_label}" if ep_label else ""
        bits.append(f"\n=== {where} CHUNK (idx={c.get('idx')}, status={c.get('status')}{ep_tag}) ===")
        # Who was in frame: derived from the chunk's own refs
        char_names = []
        loc_name = ''
        for r in (c.get('refs') or []):
            if r.get('kind') == 'char':
                ch = next((x for x in (s.get('characters') or []) if x['id'] == r['id']), None)
                if ch:
                    nm = ch['name']
                    if r.get('outfit'): nm += f" ({r['outfit']})"
                    char_names.append(nm)
            elif r.get('kind') == 'loc':
                lc = next((x for x in (s.get('locations') or []) if x['id'] == r['id']), None)
                if lc: loc_name = lc['name']
        if char_names:
            bits.append(f"  IN FRAME: {', '.join(char_names)}")
        if loc_name:
            bits.append(f"  LOCATION: {loc_name}")
        if c.get('prompt'):
            bits.append(f"  PROMPT (1 line): {(c['prompt'][:200]).replace(chr(10), ' ')}")
        if c.get('ending_state'):
            bits.append(f"  ENDING STATE: {c['ending_state']}")
        snippet = (c.get('chunk_text') or '').strip().replace('\n', ' ')[:200]
        if snippet:
            bits.append(f"  SCRIPT SNIPPET: {snippet}")
        return '\n'.join(bits)

    prev_label = (prev_neighbour_ep if prev_neighbour_ep != num else None)
    prev_block = _summarize_neighbour(prev_neighbour, 'PREVIOUS', ep_label=prev_label)
    next_block = _summarize_neighbour(next_neighbour, 'NEXT')
    if prev_block or next_block:
        cross_note = ""
        if prev_neighbour_ep != num:
            cross_note = (
                f"ВНИМАНИЕ: PREVIOUS CHUNK взят из ПРЕДЫДУЩЕГО ЭПИЗОДА (ep {prev_neighbour_ep}), "
                f"потому что текущий CHUNK — самое начало эпизода {num}. "
                "Если в начале нового эпизода нет явного time-jump или смены локации — "
                "это продолжение той же сцены конца прошлого эпизода. Сохрани локацию, "
                "состав в кадре и позы из предыдущего чанка. Если же scene heading в начале "
                "нового эпизода явно говорит о новой локации/времени — начинай свежо.\n"
            )
        prev_block = (
            "\n=== ADJACENT GENERATED CHUNKS — CONTINUITY CONTEXT ===\n"
            f"{cross_note}"
            "Эти чанки соседствуют с твоим CHUNK по позиции в сценарии. "
            "Если они в той же сцене (та же локация, нет скачка во времени) — "
            "ВСЕ персонажи из IN FRAME предыдущего чанка ОБЯЗАНЫ остаться в кадре, "
            "если в сценарии явно не сказано что они вышли. Если кто-то из них был "
            "в фокусе действия (например, его держали, бьют, он на коленях) — он точно в кадре."
            f"{prev_block}{next_block}\n"
            "=== END ADJACENT ===\n"
        )
    else:
        prev_block = ''

    # Compact char/loc rosters. Critical: list each char's available outfit
    # labels so the composer can pick a VALID one (not hallucinate). Without
    # this list the composer either fills "outfit": null (always base) or
    # invents a label that fails to resolve and silently falls back to base —
    # both lead to outfit drift across chunks of the same scene.
    chars_lines = []
    for c in s.get('characters', []) or []:
        # Include any char that has SOME image source — base AVAI url, outfit url,
        # or local ref_images (we'll lazy-upload base on resolve).
        has_outfit = any((o.get('avai_url')) for o in (c.get('outfits') or []))
        if not (c.get('avai_base_url') or has_outfit or (c.get('ref_images') or [])):
            continue
        tag = '' if c.get('avai_base_url') else ' [no_base_url]'
        line = f"- {c['name']} (id={c['id']}){tag}: {c.get('appearance','')[:120]}"
        outfits_with_url = [o for o in (c.get('outfits') or []) if o.get('avai_url')]
        if outfits_with_url:
            outfit_lines = []
            for o in outfits_with_url:
                desc = (o.get('description') or '').replace('\n', ' ')[:100]
                label = o.get('label', '')
                if not label:
                    continue
                marker = ' [base]' if o.get('is_base') else ''
                outfit_lines.append(f'    • "{label}"{marker}: {desc}')
            if outfit_lines:
                line += "\n  Доступные значения для \"outfit\" (выбирай ТОЛЬКО из этого списка):\n"
                line += "\n".join(outfit_lines)
                line += "\n    • null — базовый портрет (если нет специфики сцены ИЛИ персонаж появляется в первый раз)"
        else:
            line += "\n  Outfits: только база (\"outfit\": null)"
        chars_lines.append(line)
    locs_lines = []
    for l in s.get('locations', []) or []:
        # Include all locations that have any image (ref_images OR avai_url) — LLM can still pick them.
        if l.get('avai_url') or l.get('ref_images'):
            tag = '' if l.get('avai_url') else ' [NO_AVAI_URL — нужно перегенерировать]'
            locs_lines.append(f"- {l['name']} (id={l['id']}){tag}: {l.get('description','')[:120]}")
    # Plot-relevant items roster (locket, USB stick, bouquet, etc.).
    # Composer attaches them only when the chunk explicitly shows / mentions
    # the object visually. Cap descriptions short — these only need to anchor
    # the LLM's identification of "is this prop in the chunk?".
    items_lines = []
    for it in s.get('items', []) or []:
        if not it.get('avai_url'):
            continue   # need a public URL for Seedance to use it as ref
        items_lines.append(f"- {it['name']} (id={it['id']}): {it.get('description','')[:100]}")

    sysprompt = (
        "Ты — режиссёр-композитор шотов для коротких драм TikTok, генерируемых через ByteDance Seedance 2.0 "
        "(reference-fast: до 9 картинок-референсов, видео ~5–15 сек, аспект 9:16).\n\n"
        "ЯЗЫК ПРОМПТА — ЖЁСТКОЕ ПРАВИЛО: ВЕСЬ описательный текст промпта (subject/action/scene/camera/style/constraints, "
        "эмоции/тон перед репликами, ярлыки @Image*) пиши ИСКЛЮЧИТЕЛЬНО НА РУССКОМ. Никакого английского в описаниях. "
        "ЕДИНСТВЕННОЕ ИСКЛЮЧЕНИЕ — сами реплики персонажей внутри кавычек: их сохраняй verbatim из сценария "
        "(обычно английский). Технические термины камеры тоже переводи: 'tracking shot' → 'трекинг-шот' или "
        "'движение камеры за героем', 'medium close-up' → 'средний крупный план', 'OTS' → 'через плечо', "
        "'slow dolly in' → 'медленный наезд'. Если поймал себя на английской фразе вне кавычек — перепиши.\n\n"
        "ГОЛОС И АКЦЕНТ — ОБЯЗАТЕЛЬНО: озвучка ВСЕХ реплик строго на стандартном американском английском "
        "(General American). Никаких British / Australian / European / Indian / exotic акцентов. "
        "Если в реплике есть нестандартное слово (slang / regional) — оно произносится с американским произношением, "
        "не с акцентом носителя. Это касается И обычной речи персонажей, И voiceover-нарратора. "
        "Композитор: вставь упоминание в blocке STYLE/ATMOSPHERE («Voice: standard American English accent throughout») — "
        "сервер дополнительно укрепит это финальной строкой промпта.\n\n"
        "СТРУКТУРА промпта (~60–110 слов всего, первые 20–30 слов решают):\n"
        "  0) BINDING — ПЕРВАЯ строка промпта. Биндим имена к ref-слотам ровно ОДИН раз:\n"
        "     'В refs: @Image1=Ethan, @Image2=Maya, @Image3=Lobby (локация).' "
        "Дальше в промпте используй ИМЕНА (Ethan, Maya, Lobby) — без @ImageN.\n"
        "     Зачем: повторение @ImageN в каждом блоке поощряет Seedance рендерить персонажа дважды "
        "(каждое упоминание = potential render anchor). Биндинг один раз в начале — модель уже знает кто кто.\n"
        "  1) SUBJECT — кто в кадре. После биндинга используй ИМЕНА: 'Ethan и Maya в лобби'.\n"
        "  2) ACTION — что они делают, ИМЕНАМИ: 'Ethan ставит чашку на стол, Maya садится напротив'.\n"
        "  3) SCENE — где, ИМЕНЕМ локации: 'Действие в Lobby — стеклянное холодное лобби, утренний свет'.\n"
        "  4) CAMERA — используй канонические Seedance-keyword'ы: 'close-up' (дефолт — face filling ~50-60% of frame), 'macro close-up' (extreme emotion — face filling >70%), 'over-the-shoulder', 'medium close-up' (chest-up), 'medium shot' (waist-up), "
        "'tracking shot' / 'slow dolly in' (на эмоции), 'wide shot' / 'establishing wide' (ТОЛЬКО для границ сцены).\n"
        "  4a) FRAMING REFERENCE — ЖЁСТКИЙ ЗАПРЕТ И ЭТАЛОНЫ:\n"
        "     ❌ КАТЕГОРИЧЕСКИ НЕЛЬЗЯ: ставить одного персонажа размытым/расфокусированным силуэтом "
        "сзади в центре кадра лицом в камеру, пока другой говорит на переднем плане. Это ХУДШИЙ "
        "возможный кадр и его не должно быть НИКОГДА. Если в кадре 2 персонажа — оба обязаны "
        "быть полноценными фигурами с понятной позой и равной резкостью; если второй не нужен в "
        "кадре — выкидывай его из refs и пиши medium close-up / close-up одного.\n"
        "     ❌ КАТЕГОРИЧЕСКИ НЕЛЬЗЯ: «изобретать» постороннего человека для OTS-плеча / "
        "статиста на фоне / случайного силуэта в кадре. Все фигуры в кадре ОБЯЗАНЫ быть из refs[]. "
        "Если для OTS-кадра нужно чьё-то плечо/затылок на переднем плане — это плечо ОДНОГО ИЗ "
        "уже заявленных персонажей (того кого ты укажешь в `framing_anchor`), НЕ нового. "
        "В тексте промпта формулируй ИМЕНЕМ существующего перса: 'over Maya (@Image2)'s shoulder, "
        "back-of-head bottom-left — that's the SAME Maya from @Image2, same hair and outfit'. "
        "БЕЗ имени = модель сама изобретает кому это плечо принадлежит, и часто рисует левую девушку.\n"
        "     ❌ Запрещённые формулировки (даже если они «передают атмосферу»): "
        "'<name> stands in the background, blurred', '<name> visible far behind <other>, soft focus', "
        "'<name> out of focus while <other> sharp' (когда оба заявлены в refs), 'silhouette of <name> "
        "in the background', '<name> на заднем плане размыто', '<name> вдалеке в расфокусе'.\n"
        "     ✅ Валидные альтернативы (выбирай по ситуации, не угадываю за тебя контекст):\n"
        "       • OTS: 'over <A>'s shoulder, back-of-head bottom-left, blurred earpiece. <B> sharp center.' "
        "(<A> остаётся в кадре как foreground-силуэт, НЕ как фон).\n"
        "       • Two-shot статика: 'both faces in a balanced two-shot, shoulder to shoulder, parallel to camera, equal focus on both'.\n"
        "       • Profile-two (машина / тесное пространство): 'both in profile to camera, cramped car cabin / at the table, equal focus on both'.\n"
        "       • Tracking (walking-and-talking): 'camera tracks alongside, both walking in stride shoulder-to-shoulder, equal sharpness, parallel motion'.\n"
        "       • Active/activity-scene с двумя в кадре: '<A> studies a laptop at the table, <B> stands close behind reading over <A>'s shoulder' "
        "(оба явно в кадре, ни один не «фон»). Или: '<A> at the window, profile to camera; <B> reflected in window's glass talking to <A>'.\n"
        "       • Если по тексту реально нужен один в фокусе — выкидывай второго из refs и пиши medium close-up / close-up одного спикера, в CAMERA: 'остальные за пределами кадра / off-screen'.\n"
        "  5) DIALOGUE — ИСКЛЮЧЕНИЕ из правила биндинга. Для КАЖДОЙ реплики — формат из ДВУХ частей "
        "ОБЯЗАТЕЛЬНО с @ImageN (для lipsync-привязки):\n"
        "     (a) ШОТ-БИТ перед репликой, имя + @ImageN. По умолчанию используй CLOSE-UP (face filling ~50-60% of frame — это канонический Seedance-keyword, не 'tight CU'). "
        "Варианты ракурса: 'close-up Maya (@Image2), face filling frame, shoulders barely visible', "
        "'over-the-shoulder Ethan на Maya (@Image2)', 'close-up Maya (@Image2), slight low angle'.\n"
        "         Wide / two-shot ИЗБЕГАЙ во время реплик — модель плохо удерживает мимику и lipsync на дистанции.\n"
        "     (b) Сама реплика: 'Maya (@Image2), <эмоция/тон на русском>, говорит: \"<точная реплика как в сценарии>\"'\n"
        "     Полный пример (две реплики двух разных персов):\n"
        "       'Medium close-up Maya (@Image2), плечи и лицо в кадре. Maya (@Image2), ухмыляясь с презрением, говорит: \"Lost, Aria?\" "
        "Камера переключается на medium close-up Ethan (@Image1) через плечо Maya. Ethan (@Image1), дрожа от ярости, отвечает: \"Get out.\"'\n"
        "     ПРАВИЛО: в DIALOGUE между сменой говорящего ОБЯЗАТЕЛЬНО шот-бит со склейкой/наездом на нового спикера + указание @ImageN. "
        "Без @ImageN в shot-bit'е и в самой реплике Seedance прицеливает lipsync к не тому персу.\n"
        "     ⚠ КРИТИЧНО — НИКАКИХ СКЛЕЕК ВНУТРИ ОДНОЙ РЕПЛИКИ: пока спикер произносит ОДНУ реплику "
        "(от открывающей кавычки до закрывающей) — камера НЕ режет, НЕ переключается на другого, НЕ "
        "делает 'cut to reaction shot'. Камера держит шот на спикере всю фразу. Склейка возможна "
        "ТОЛЬКО на смене говорящего, либо между репликами при значимой паузе (≥1с по сценарию), "
        "либо при явном action-beat'е в action-ремарке ('она отворачивается и говорит', 'он встаёт и продолжает'). "
        "Запрещено: 'Maya begins \"I never—\" then cut to Ethan reacting, then back to Maya \"—said that.\"'. "
        "Разрешено: 'Maya (@Image2) holds the medium close-up for the entire line \"I never said that.\", then cut to Ethan (@Image1) reaction shot'.\n"
        "     Эмоция/тон по-русски ОБЯЗАТЕЛЬНА перед каждой репликой — без неё лип-синк хуже.\n"
        "     Внутри DIALOGUE @ImageN можно повторить (это нужно для lipsync). В SUBJECT/ACTION/SCENE — НЕТ.\n"
        "     (c) VOICEOVER / ЗАКАДРОВЫЙ ГОЛОС — КРИТИЧНО. Если в исходной реплике сценария есть пометка "
        "         «(voiceover)» / «(V.O.)» / «(VO)» / «(off-screen)» / «(O.S.)» / «(narration)» / «(закадр)» / "
        "         «(голос за кадром)» / «(внутренний голос)» / «(мысленно)» — это НЕ обычная произнесённая реплика. "
        "         Это голос за кадром: звук идёт, но персонаж в кадре НЕ открывает рот. Если ты прицепишь lipsync "
        "         к такому персу — Seedance заставит его шевелить губами под закадровый текст, что выглядит как баг.\n"
        "         Формат VO-реплики ДРУГОЙ:\n"
        "           • НЕ ставь @ImageN рядом с репликой и НЕ пиши «говорит» / «отвечает».\n"
        "           • Пиши: «За кадром (voiceover) — голос Elena: \"<реплика дословно>\". В кадре Elena (@ImageN) "
        "             молчит, рот закрыт, взгляд задумчивый/вдаль/мимо камеры, лёгкое движение глаз/ресниц "
        "             синхронно с эмоцией текста, но БЕЗ движения губ».\n"
        "           • Shot-bit перед VO-репликой описывает СОСТОЯНИЕ персонажа (где стоит/сидит, на что смотрит), "
        "             не lipsync. @ImageN в shot-bit'е оставь — для удержания внешности перса, но в самой реплике убери.\n"
        "         Пример shot-bit + VO: «Medium close-up Elena (@Image1) у окна, она смотрит вдаль, лицо отрешённое, "
        "         губы сомкнуты. За кадром (voiceover) — голос Elena: \"Ten years. That's how long it takes for "
        "         everyone to forget your face.\" В кадре губы Elena остаются СОМКНУТЫМИ, никакого lipsync.»\n"
        "         Если в той же сцене есть И обычные реплики И VO — в обычных пиши «Elena (@Image1) говорит: ...» "
        "         (с lipsync), в VO пиши «За кадром voiceover голос Elena: ... губы сомкнуты».\n"
        "  6) STYLE/ATMOSPHERE — короткие фразы (cinematic, harsh fluorescent light, cold colour palette). "
        "Если в userprompt задан STYLE OVERRIDE — ВСЕ описания (рендер, материалы, освещение, текстуры лиц и одежды, фон) подчиняются этому стилю; "
        "перепиши style-блок и вшей стилистические маркеры также в SUBJECT и SCENE (например 'Pixar 3D animation, soft volumetric light, exaggerated facial expressions, slightly stylised proportions, vibrant saturated palette'). "
        "Реплики в кавычках при этом не меняй.\n"
        "  7) CONSTRAINTS — ТОЛЬКО позитивные формулировки ('smooth gimbal motion', 'stable framing'). "
        "     НЕ пиши 'no shake', 'without distortion' — модель плохо понимает отрицания.\n\n"
        "АНАЛИЗ КОНТЕКСТА — ОБЯЗАТЕЛЬНО ДЕЛАЙ ЭТО ДО ПРОМПТА:\n"
        "Тебе дают: (а) полный сценарий эпизода, (б) выделенный кусок (CHUNK), (в) активный состав персов и локаций эпизода.\n"
        "Прочитай ПОЛНЫЙ сценарий, найди в нём CHUNK и ответь себе на вопросы:\n"
        "  • В какой ЛОКАЦИИ происходит этот кусок? Ищи ПОСЛЕДНИЙ scene heading перед CHUNK. Маркеры могут быть РАЗНЫЕ:\n"
        "    – формальные: 'INT./EXT./INT.\\/EXT. <LOCATION> — <TIME>', 'ИНТ./ЭКСТ./НАТ./ВНУТР./ИНТЕРЬЕР <ЛОКАЦИЯ>'\n"
        "    – явные: 'Локация: <место>. <время>.', 'СЦЕНА N', 'Сцена N', 'SCENE N'\n"
        "    – inferred: одиночная ALL-CAPS строка ('ДОМ АННЫ — НОЧЬ', 'OFFICE — DAY') или в скобках '[КАФЕ — НОЧЬ]'\n"
        "    Если scene heading не нашёлся вообще — локация продолжается с самого начала сценария или предыдущей сцены.\n"
        "  • Какие ПЕРСОНАЖИ физически находятся в кадре? Это НЕ только говорящие. "
        "Если в сцене сказано что Liam стоит рядом и наблюдает — он в кадре, даже если в этом куске молчит. "
        "Если предыдущий чанк закончился тем что Selena вошла в комнату — она всё ещё в кадре в новом чанке той же сцены.\n"
        "  • Где каждый персонаж стоит/находится относительно других? (за столом, у двери, на коленях, и т.п.)\n\n"
        "РЕФЕРЕНСЫ — ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:\n"
        "1. Сначала персонажи, в порядке важности в кадре → @Image1, @Image2, @Image3...\n"
        "   В refs включай ВСЕХ персов в кадре (не только говорящих). Молчащий перс рядом — это часть мизансцены.\n"
        "2. СЮЖЕТНЫЕ ПРЕДМЕТЫ (items) — добавляй в refs ОБЯЗАТЕЛЬНО, если в CHUNK предмет ВИДЕН или ВРУЧАЕТСЯ:\n"
        "   – персонаж держит/протягивает/вручает букет, конверт, локет, флешку, кольцо, документ → ДОБАВЬ в refs\n"
        "   – предмет упомянут в action-ремарке как visible prop ('он сжимает локет', 'кладёт конверт на стол') → ДОБАВЬ\n"
        "   – предмет лишь подразумевается / упоминается репликой без визуального присутствия → НЕ добавляй\n"
        "   В refs items идут ПОСЛЕ персов, ДО локации. Без них Seedance нарисует обобщённый prop с другим цветом/формой.\n"
        "   Используй имена items в SUBJECT/ACTION после BINDING ('Wolf протягивает Daisy Bouquet к Bunny').\n"
        "3. ПОСЛЕДНИМ обязательно идёт ЛОКАЦИЯ → @Image<N+1>. ЭТО НЕ ОПЦИЯ.\n"
        "   Если в AVAILABLE LOCATIONS есть локация, совпадающая с местом действия (по сцен-хедеру или контексту) — "
        "   ОБЯЗАТЕЛЬНО прикрепи её последним @Image. Без локации фон будет рандомным и серия развалится визуально.\n"
        "   Если в roster нет идеально совпадающей локации — выбери максимально близкую по описанию (офис, лобби, спальня и т.п.).\n"
        "   Локацию НЕ ВКЛЮЧАЙ только если в roster вообще нет ни одной подходящей локации с фото.\n"
        "4. В тексте промпта в блоке SCENE явно упомяни локацию ИМЕНЕМ (после BINDING): 'Действие в Lobby — стеклянное лобби корпорации, холодное освещение'.\n"
        "5. Максимум 9 референсов (Seedance hard cap). Обычно 1–3 перса + 0–2 предмета + 1 локация.\n\n"
        "CONTINUITY — КРИТИЧНО:\n"
        "Если в userprompt есть блок ADJACENT GENERATED CHUNKS — это твой главный источник кто физически в кадре.\n"
        "Алгоритм:\n"
        "  1. Определи: соседний чанк (PREVIOUS) — это ТА ЖЕ СЦЕНА что и текущий CHUNK? "
        "Та же сцена = одна и та же локация + нет смены времени суток + нет скачка дня + действие непрерывно.\n"
        "     • Та же сцена → персонажи остаются В СЦЕНЕ (в той же комнате, не вышли). НО это НЕ значит "
        "что они автоматически попадают в твои refs[]. refs[] = кто В КАДРЕ ИМЕННО ЭТОГО чанка, не вся сцена.\n"
        "     • КТО В КАДРЕ — определяется CHUNK TEXT'ом (не предыдущим чанком). Алгоритм:\n"
        "         A. Добавь в refs всех СПИКЕРОВ этого CHUNK'а (тех у кого Name: реплика).\n"
        "         B. Добавь персов явно упомянутых в action-ремарках ВНУТРИ chunk'а с физическим действием "
        "('Liam stares at her', 'Sophie steps closer', 'Adrian frowns at the letter') — у них есть on-screen действие.\n"
        "         C. Если в чанке есть физическое взаимодействие нескольких ('Liam держит Leo за горло', "
        "'Maya обнимает Sophie') — оба/все в refs. Continuity по позам ('Leo на коленях') — обязательна.\n"
        "         D. Просто 'присутствует в той же комнате' (молча, без действия в этом chunk'е) → "
        "НЕ В RREFS. Ему place в фоне Seedance может нарисовать сам как обобщённый силуэт, "
        "но без отдельного @Image-ref'а ты НЕ заставишь Seedance рендерить его лицо.\n"
        "     • CLOSE-UP кейс — самый частый и самый ломаемый:\n"
        "         CHUNK = 1 короткая реплика ОДНОГО спикера ИЛИ длинный монолог ОДНОГО спикера, без action "
        "на других в этом chunk'е → CLOSE-UP/MEDIUM single-subject.\n"
        "         refs = [тот спикер + локация]. Других НЕ ВКЛЮЧАЙ даже если они присутствуют в сцене. "
        "В тексте промпта (CAMERA): 'крупный план / средний план Maya, остальные за пределами кадра'.\n"
        "         Camera-cues 'крупный план X', 'close-up of X', 'tight on X', 'lens on Y' — "
        "STRICT single-subject mode, refs только X/Y + локация.\n"
        "     • GROUP/WIDE кейс: 3+ реплик от разных персов или явная wide-cue ('все собрались', "
        "'wide shot', 'establishing') → refs все участники + локация.\n"
        "     • ФИЗИЧЕСКИЕ ПОЗИЦИИ — ХРАНИТЬ. Если в PREVIOUS Leo стоял рядом с Liam — он РЯДОМ С LIAM, "
        "а не телепортируется к маме. Если Selena была в правом углу — она остаётся в правом углу. "
        "В ACTION прямо пиши конкретные позиции ИМЕНАМИ (без @Image): "
        "'Leo остаётся вплотную к Liam справа от него, Selena на заднем плане у двери'. "
        "Без явного описания позиций модель шафлит героев.\n"
        "     • ФОНОВЫЕ ЭЛЕМЕНТЫ из ENDING STATE / PROMPT предыдущего чанка тоже сохраняй: "
        "если Kaelen только что вышел из лифта — в кадре за его спиной должен быть открытый/закрывающийся лифт. "
        "Если до этого в кадре был стол с ноутбуком — он тут же. В SCENE так и пиши именами: "
        "'на заднем плане Lobby — двери лифта только что закрылись за Kaelen'.\n"
        "     • Если ENDING STATE говорит что персонаж 'на коленях' / 'без сознания' / 'у двери' — "
        "отрази эту позу в SUBJECT/ACTION текущего промпта.\n"
        "     • Локация: если PREVIOUS использовал @Image<X>=Lobby и сцена та же — твой last @Image тоже Lobby.\n"
        "  2. Если сцена другая (новый scene heading МЕЖДУ чанками — формальный INT./EXT./ИНТ./..., либо "
        "'Локация:', 'СЦЕНА N', одиночный ALL-CAPS slug 'ДОМ АННЫ — НОЧЬ', либо явный 'CUT TO:' / 'FADE TO:' "
        "на другую локацию, либо явный перепрыг во времени) — начинай свежо, состояние НЕ тащим.\n"
        "  3. NEXT CHUNK (если есть) используй только для проверки: твой ending не должен противоречить началу следующего.\n\n"
        "POSTURE & STATE CONTINUITY — КРИТИЧНО (частый баг «телепорт стоя→сидя без посадки»):\n"
        "Внутри ОДНОЙ сцены поза/позиция/контакт каждого перса ОБЯЗАНЫ совпадать с ENDING STATE предыдущего чанка. "
        "Seedance НЕ помнит позы между чанками — если ты не пропишешь явно, она сгенерит «нормальную» позу "
        "(обычно стоя фронтально), и герой телепортируется.\n"
        "Конкретно ЛОЧИМ между чанками одной сцены (без перехода — никаких изменений):\n"
        "  • СТОЯ / СИДЯ / НА КОЛЕНЯХ / ЛЁЖА / ПРИСЛОНЁН К СТЕНЕ — если в PREVIOUS ENDING STATE «Maya сидит "
        "    за столом», то в текущем чанке Maya ВСЁ ЕЩЁ сидит за тем же столом. НЕ «Maya стоит у стола».\n"
        "  • ПОЗИЦИЯ В КОМНАТЕ — у двери, у окна, в центре, в углу, за столом, перед камином. Фиксируется.\n"
        "  • ФИЗИЧЕСКИЙ КОНТАКТ — держит за руку, обнимает, удерживает за плечо, держит за горло, "
        "    нависает над, прижимает к стене. Если был в PREVIOUS — продолжается, пока CHUNK явно не разорвёт.\n"
        "  • ЧТО В РУКАХ — чашка, телефон, нож, бокал, документ. Если в PREVIOUS «Marcus держит бокал» — "
        "    в текущем чанке бокал всё ещё у Marcus в руке, пока сценарий не скажет «ставит бокал на стол».\n"
        "  • МИМИКА/ЭМОЦИОНАЛЬНОЕ СОСТОЯНИЕ — заплаканная, в крови, ярость на лице, истерика. Не сбрасывается "
        "    в «нейтральное лицо» только потому что новый чанк.\n"
        "ИЗМЕНЕНИЕ ПОЗЫ ВНУТРИ СЦЕНЫ РАЗРЕШЕНО ТОЛЬКО при ЯВНОЙ scripted action в тексте CHUNK:\n"
        "  • «Maya садится» / «садится за стол» / «опускается на стул» → можно показать процесс или начать "
        "    с уже сидящей в этом чанке.\n"
        "  • «встаёт» / «поднимается» / «отходит к окну» → можно сменить позу.\n"
        "  • «берёт <предмет>» / «кладёт» / «выпускает из рук» → можно сменить что в руках.\n"
        "  • «отступает» / «делает шаг ближе» / «выходит из комнаты» → можно сменить позицию.\n"
        "  Если в CHUNK НЕТ глагола действия — ПОЗА ИЗ PREVIOUS ОБЯЗАТЕЛЬНА. Не «улучшай» сцену добавляя «героиня "
        "  садится» из головы.\n"
        "ОБЯЗАТЕЛЬНЫЙ ТЕКСТ В ACTION для каждого compose внутри сцены:\n"
        "  • Первая фраза ACTION = «продолжаем с PREVIOUS: [имя] [поза] [где] [с чем в руках]». Пример: "
        "    «Продолжая с предыдущего чанка: Marcus стоит у книжного шкафа справа, бокал виски в правой руке; "
        "    Elena сидит за столом по центру, опершись локтями на разложенные документы».\n"
        "  • Затем — то новое что происходит ИМЕННО в этом чанке (реакции, повороты головы, реплики). "
        "    БЕЗ изменения поз/позиций если их не было в сценарии.\n"
        "САМОПРОВЕРКА перед выводом:\n"
        "  – Прочти ENDING STATE / IN FRAME предыдущего чанка.\n"
        "  – Выпиши себе: «X стоит/сидит у Y, держит Z, эмоция W» для каждого перса.\n"
        "  – Прочти текст ТЕКУЩЕГО CHUNK'а — есть ли явный глагол смены позы для каждого перса?\n"
        "  – Если нет глагола → твой ACTION ОБЯЗАН описать ту же позу/позицию/предмет.\n"
        "  – Если в твоём ACTION перс «вдруг сидит» а в PREVIOUS он стоял и в CHUNK нет «садится» — "
        "    это телепорт, перепиши.\n\n"
        "REMOTE CONVERSATION (ТЕЛЕФОН / ВИДЕО-ЗВОНОК / ЧЕРЕЗ СТЕКЛО) — КРИТИЧНО (баг «телепорт из телефона в лицом-к-лицу»):\n"
        "Если сцена — телефонный разговор (или видео-звонок, или разговор через стекло допросной), собеседники "
        "ФИЗИЧЕСКИ НЕ В ОДНОЙ КОМНАТЕ. Каждый — в своём пространстве, с трубкой/телефоном у уха или перед лицом. "
        "Чанки одного звонка ОБЯЗАНЫ оставаться звонком до сценарной фразы окончания. Частый баг: чанк 0 — "
        "звонок по мобильному, чанк 1 — стационарный телефон, чанк 2 — герои лицом к лицу в одной комнате. "
        "Это разваливает сцену.\n"
        "ДЕТЕКЦИЯ — звонок ИЛИ продолжается, ИЛИ нет. Скани FULL SCRIPT (не только CHUNK), ищи маркеры:\n"
        "  • Действия: «picks up the phone», «answers», «звонит», «набирает номер», «берёт трубку», "
        "    «hangs up», «ends the call», «кладёт трубку», «сбрасывает».\n"
        "  • Ремарки у спикера: «(on phone)», «(into phone)», «(V.O.)», «(FILTERED)», «(over phone)», "
        "    «(по телефону)», «(в трубку)».\n"
        "  • Структура: «INTERCUT BETWEEN:», «INTERCUT — Elena's apartment / Marcus's office», "
        "    «split screen», parallel cutting между двумя локациями с alternating диалогом.\n"
        "  • Звонок начался → ВСЕ последующие чанки в той же сцене = звонок, пока не встретилось ЯВНОЕ "
        "    окончание («hangs up», «кладёт трубку», «звонок прерывается», «связь обрывается»).\n"
        "ПРАВИЛА КОМПОЗИЦИИ для чанков-звонков:\n"
        "  1. ТИП ЗВОНКА ЛОЧИТСЯ. Если в первом чанке звонка Elena говорит с мобильного — во всех "
        "     последующих чанках того же звонка она с того же мобильного. НЕ «во втором чанке стационарный, "
        "     потому что компоновщику показалось красивее». Тип определяется первым явным упоминанием "
        "     в скрипте («cell», «мобильный», «smartphone» / «landline», «стационарный», «receiver»).\n"
        "  2. ДВА ПЕРСА В РАЗНЫХ ЛОКАЦИЯХ. Для звонка с показом обоих собеседников выбирай ОДИН из вариантов "
        "     (НЕ оба сразу):\n"
        "     (a) SINGLE-SIDE chunk: в кадре только один собеседник (Elena в своей квартире, телефон у уха), "
        "         голос второго слышен «через трубку». Refs = Elena + локация Elena. БЕЗ Marcus в кадре.\n"
        "     (b) INTERCUT chunk: split-screen или быстрая склейка между двумя локациями. Refs = оба перса + "
        "         ДВЕ локации (обе). В CAMERA пиши «split screen / intercut between [Elena's apartment] "
        "         and [Marcus's office]; left half — Elena, right half — Marcus, оба с телефонами у уха».\n"
        "     НИКОГДА не ставь Elena и Marcus в один кадр в одной комнате во время звонка. Это разрушает "
        "     логику сцены.\n"
        "  3. ТЕЛЕФОН В РУКЕ — ОБЯЗАТЕЛЕН в каждом чанке звонка. Прописывай в ACTION: «Elena у уха правое — "
        "     мобильный телефон в правой руке». Какая рука / у какого уха — лочится с первого чанка.\n"
        "  4. EYE-LINE при звонке: глаза НЕ направлены на собеседника (его нет рядом). Глаза слегка вниз, в "
        "     сторону, на стену, в окно — нейтральный «думающий» взгляд. НЕ прямо в камеру (не разрушаем "
        "     четвёртую стену). В ACTION: «взгляд Elena чуть в сторону, не на камеру, фокус слухового внимания».\n"
        "  5. ФОНОВАЯ ЛОКАЦИЯ каждого собеседника лочится. Если Elena в первом чанке у себя дома на кухне — "
        "     во всех последующих чанках того же звонка она в той же кухне (не «вдруг в спальне»).\n"
        "  6. ОКОНЧАНИЕ ЗВОНКА — нужен сценарный триггер. После «Elena hangs up» / «кладёт трубку» / «связь "
        "     прерывается» следующий чанк МОЖЕТ быть лицом к лицу или новой сценой. БЕЗ триггера — нельзя.\n"
        "САМОПРОВЕРКА перед выводом для каждого чанка:\n"
        "  – PREVIOUS чанк был телефонным разговором (по analysis / по script context)? → ТЕКУЩИЙ тоже звонок, "
        "    пока я не нашёл явный «hangs up» между ними.\n"
        "  – В ACTION есть «телефон у уха» / «mobile in hand»? Должен быть, если звонок.\n"
        "  – Я случайно не поставил обоих собеседников в одну комнату? Это запрещено.\n"
        "  – Тип телефона тот же что в PREVIOUS? Не «мобильный → стационарный».\n"
        "  – VOICE-ONLY ПЕРСОНАЖИ: Если в AVAILABLE CHARACTERS у персонажа appearance начинается "
        "    с «Voice on phone» / «Off-screen voice» / «голос по телефону» / «голос за кадром» — "
        "    это персонаж НА ДРУГОМ КОНЦЕ звонка. Его @ImageN ref нужен для голосовой консистентности, "
        "    но визуально В КАДРЕ ОН НЕ ПОЯВЛЯЕТСЯ. Сцена показывает только тех кто в комнате слушает "
        "    звонок (на громкой связи) или говорит в трубку. Реплику voice-only персонажа оформляй "
        "    как '(V.O.) from phone speaker' или 'off-screen voice through phone'. НЕ описывай его "
        "    физическое присутствие в SUBJECT/ACTION/SCENE. Прод-инцидент 2026-05-19 'My Stepmother' "
        "    ep 43: Detective Morris (voice-on-phone) был поставлен в SUITE 12 с папками в руках, "
        "    хотя должен был быть только голосом в speakerphone.\n\n"
        "OUTFIT CONSISTENCY — КРИТИЧНО ДЛЯ КОНСИСТЕНТНОСТИ ОДЕЖДЫ:\n"
        "Это самая частая причина дрейфа костюмов между чанками. Жёсткие правила:\n"
        "  1. Поле \"outfit\" в каждом char-ref'е выбирай ТОЛЬКО из списка значений, явно перечисленного "
        "под персонажем в AVAILABLE CHARACTERS / ACTIVE THIS EPISODE. Если списка нет — ставь null. "
        "НЕ ПРИДУМЫВАЙ label'ы (типа 'casual', 'formal', 'dress' и т.п.) — они тихо упадут к base.\n"
        "  2. SAME-SCENE LOCK: Если в блоке ADJACENT GENERATED CHUNKS есть PREVIOUS CHUNK И он в той же сцене что текущий CHUNK "
        "(см. CONTINUITY алгоритм выше), то outfit КАЖДОГО перса ОБЯЗАН СОВПАДАТЬ с тем что использовал PREVIOUS CHUNK. "
        "Имя outfit'а видно в IN FRAME предыдущего чанка как 'Maya (work_scrubs)'. Берёшь дословно тот же label. "
        "Менять outfit в той же сцене категорически нельзя — даже если по описанию сцены кажется что другой подходит лучше.\n"
        "  3. SCENE-CHANGE ROUTE: Если сцена меняется (новый scene heading, time-jump) — выбери outfit по контексту "
        "новой сцены. Например в roster Maya: work_scrubs (для смен в больнице), street_clothes (для улицы), "
        "evening_dress (для свидания). Сценарий обычно сам подсказывает контекст. Если нет явного — null (база).\n"
        "  4. WARDROBE-CHANGE EXCEPTION: outfit меняется ВНУТРИ той же сцены ТОЛЬКО если сценарий явно описывает "
        "переодевание ('Maya меняет блузку', 'переодевается в платье', 'снимает пальто'). Без явной ремарки — не менять.\n"
        "  5. DRESS-CODE ОТМЕНЯЕТ SAME-SCENE LOCK — КРИТИЧНО (частый баг «работница пришла на похороны в форме»):\n"
        "Когда CHUNK или его FULL-SCRIPT окружение содержит маркер ЦЕРЕМОНИАЛЬНОГО / СПЕЦИАЛЬНОГО события — "
        "outfit ОБЯЗАН соответствовать дресс-коду этого события, даже если предыдущий чанк той же сцены был "
        "в рабочей форме. Continuity костюма ПРОИГРЫВАЕТ адекватности контекста — лучше визуальная нестыковка "
        "«было work_uniform, стало black_dress на похоронах», чем «работница в фартуке плачет у гроба».\n"
        "ТРИГГЕРЫ (любого упоминания в текущем CHUNK или scene heading достаточно):\n"
        "  • Похороны / поминки / кладбище / гроб / отпевание / funeral / wake / cemetery / casket → "
        "тёмная/чёрная формальная одежда. Ищи outfit с label-словами: 'black', 'funeral', 'mourning', "
        "'formal_black', 'dark_suit'. Если нет — null (база) НЕ годится если база = work uniform; в этом случае "
        "выбери самый близкий формальный outfit ('formal_dress', 'business_suit', 'evening_dress' тёмного тона). "
        "В ACTION/SUBJECT прямо опиши состояние: 'в тёмной формальной одежде', 'на лице траурное выражение'.\n"
        "  • Свадьба / венчание / wedding / ceremony / алтарь / banquet hall → нарядная одежда. Невеста — "
        "wedding_dress / white_dress если есть. Гости — formal/cocktail. Look for: 'wedding', 'gown', 'tuxedo', "
        "'suit', 'cocktail', 'formal'.\n"
        "  • Суд / зал заседаний / courtroom / depositions / hearing → деловой костюм. 'court_suit', "
        "'business_suit', 'professional'. Не в фартуке/spa-форме/casual.\n"
        "  • Гала / приём / red carpet / charity event / opera → вечерний наряд. 'evening_dress', 'tuxedo', "
        "'gala', 'ball_gown'.\n"
        "  • Госпиталь как ПАЦИЕНТ (не персонал) → больничный халат / 'hospital_gown' / 'patient'. БЕЗ "
        "повседневной одежды поверх.\n"
        "  • Тюрьма / задержание / police station as detainee → тюремная роба / 'prison_jumpsuit' / 'inmate'. "
        "Если перс ушёл в тюрьму одним outfit'ом, а вышел — это новый outfit (тюремная или новая гражданская).\n"
        "  • Пляж / бассейн / swim → swimwear / 'swimsuit' / 'beach'. Не в зимнем пальто.\n"
        "  • Спортзал / тренировка / workout → 'gym', 'sportswear', 'athletic'.\n"
        "  • Сон / спальня / постель в начале сцены → 'pajamas', 'nightgown', 'robe', 'sleepwear'.\n"
        "  • Душ / ванна → халат / полотенце ('bathrobe', 'towel') ИЛИ оставь null если outfit'а нет.\n"
        "АЛГОРИТМ:\n"
        "  1. Прочти текущий CHUNK + scene heading + 2-3 строки FULL_SCRIPT вокруг — есть ли event trigger?\n"
        "  2. Если да — пройдись по списку доступных outfit'ов перса. Ищи label или description со словами "
        "из триггер-семантики.\n"
        "  3. Если нашёл подходящий — используй его (НЕЗАВИСИМО от того что было в PREVIOUS CHUNK).\n"
        "  4. Если НЕТ подходящего — пиши null И в поле `reasoning` JSON'а отметь: «event=funeral, но нет "
        "формального outfit'а в roster — рендерим в base, нужно сгенерить outfit». Это поможет юзеру увидеть "
        "и добавить нужный костюм.\n"
        "  5. SAME-SCENE LOCK всё ещё работает ВНУТРИ одной event-сцены — если перс уже в чёрном на похоронах "
        "в чанке N, в чанке N+1 (тех же похоронах) он всё ещё в чёрном.\n\n"
        "FRAMING / РАКУРС — КРИТИЧНО (частая проблема «герои телепортируются на широких планах»):\n"
        "Дефолт: ВСЯ серия должна выглядеть как series of close-ups (face filling ~50-60% of frame — канонический Seedance keyword 'close-up'), потому что:\n"
        "  • Mobile vertical retention: лицо занимает большую часть экрана — зритель не отрывается. "
        "    TikTok/Reels-grammar: tight CU = первые 3 секунды hook.\n"
        "  • Wide-планы между чанками показывают конкретные позы тел, и Seedance не помнит точное положение "
        "    рук/ног/корпуса из предыдущего чанка → герой «перепрыгивает» с места на место.\n"
        "  • Tight close-up обрезает почти всё тело, lipsync чище, мимика читается крупно, "
        "    перепрыгивание неощутимо (тела под кадром нет вообще).\n"
        "\nПРАВИЛА:\n"
        "1. По умолчанию ВСЕ shot-биты в DIALOGUE — это close-up (face filling ~50-60% of frame, плечи едва видны, "
        "без обстановки в фокусе). Medium close-up (по грудь) — только когда явно нужен жест руки.\n"
        "2. OTS (over-the-shoulder) — отличная альтернатива для смены ракурса. Чередуй tight CU и OTS "
        "по сменам говорящих чтобы было разнообразие, но оба остаются «близкими».\n"
        "3. Extreme close-up на одно лицо (только глаза или только губы) — ТОЛЬКО для эмоциональных акцентов "
        "(шок, ужас, слёзы). Обычная реплика — обычный tight CU.\n"
        "4. Wide shot / establishing wide / two-shot во весь рост ИСКЛЮЧИТЕЛЬНО в двух случаях:\n"
        "   (a) ПЕРВЫЙ chunk ПОСЛЕ scene heading (новая сцена) — establishing wide на 2 секунды, "
        "       показать пространство и расстановку, дальше медленный наезд на medium close-up первого спикера.\n"
        "       Признак: ПЕРЕД CHUNK в FULL SCRIPT стоит scene heading (INT./EXT./ИНТ./Локация:/СЦЕНА N/ALL-CAPS slug), "
        "       а между ним и CHUNK максимум 1-2 строки ремарки.\n"
        "   (b) ПОСЛЕДНИЙ chunk сцены — wide на эмоциональный outro если в конце сцены героев накрывает "
        "       что-то значительное (расставание, удар, откровение). Признак: ПОСЛЕ CHUNK в FULL SCRIPT "
        "       идёт следующая scene heading или конец сценария.\n"
        "   Wide-ы в середине сцены — НЕТ. Если по сюжету нужен жест/движение которое не показать close-up "
        "   (герой идёт через комнату, бьёт кулаком стол, обнимает другого) — сделай medium SHOT (по пояс), "
        "   не wide. Medium shot покажет действие и не телепортирует.\n"
        "5. Если действие физически требует wide (драка, погоня, падение, групповая сцена 4+ человек) — "
        "   делай wide, но в SCENE/ACTION пропиши КОНКРЕТНЫЕ позиции каждого ИМЕНАМИ ('Maya справа от стола, "
        "   Ethan слева, Liam на заднем плане у двери') чтобы Seedance не теряла геометрию.\n"
        "6. Establishing-wide в начале сцены — ОТДЕЛЬНЫЙ tag в CAMERA: 'establishing wide shot of Lobby — 2с, "
        "   потом slow dolly in на close-up Maya для первой реплики'. Это сообщает модели «сначала "
        "   мир, потом крупно». Не пиши «wide shot of everyone» — потеряешь lipsync.\n"
        "7. ОДНО ДВИЖЕНИЕ КАМЕРЫ НА ШОТ-БИТ (Seedance hard rule per ByteDance docs): "
        "   'slow dolly in', 'pan left', 'tracking shot', 'rack focus' — выбирай ОДНО. "
        "   Комбо вида 'slow dolly in with rack focus' или 'tracking shot + handheld jitter' "
        "   гарантированно вызывает jitter и потерю композиции. Если нужны два движения — "
        "   это два отдельных shot-bit'а со склейкой между ними.\n"
        "8. CAP НА NAMED ПЕРСОНАЖЕЙ В КАДРЕ — 2 (Seedance hard limit per community benchmark). "
        "   Больше 2 named characters одновременно в одном shot-bit'е → identity blend / feature averaging. "
        "   Если в чанке 3+ named — оставляй в фокусе кадра МАКСИМУМ 2, остальных явно убирай: "
        "   'Liam off-screen / out of frame' или 'Liam silhouette in deep background, blurred'. "
        "   Сцены массовки (court, restaurant) — extras с размытыми лицами не считаются — лимит 2 на NAMED.\n"

        "ПРОВЕРКА перед выводом: если в твоём prompt-е больше одного wide/two-shot ракурса вне scene-edges — "
        "пересмотри и замени средние/крупные на medium close-up. Это улучшит continuity видеогенерации.\n\n"
        "ВАРИАТИВНОСТЬ РАКУРСОВ МЕЖДУ SHOT-BIT'АМИ ВНУТРИ ОДНОГО ЧАНКА — КРИТИЧНО (баг «склейки есть, "
        "но ракурсы выглядят одинаково, героиня будто телепортируется лицом»):\n"
        "Seedance ДЕЛАЕТ склейки которые ты прописал. Проблема в том, что без явных директив каждый shot-bit "
        "получается медиум-крупным планом примерно с того же положения камеры и того же расстояния — две "
        "соседние склейки выглядят как «слегка покачнули камеру, лицо то же». Зритель видит лишь сменившийся "
        "lipsync, а не реверс-шот. Чтобы каждая склейка реально читалась — соседние shot-bit'ы должны "
        "ОТЛИЧАТЬСЯ как минимум по ДВУМ из этих параметров:\n"
        "  • СТОРОНА КАМЕРЫ (180°-line): OTS со стороны A → reverse OTS со стороны B (зеркально). "
        "    «Камера через ЛЕВОЕ плечо Clara» → следующая склейка «через ПРАВОЕ плечо Adrian-а», "
        "    НЕ «через левое плечо Adrian-а».\n"
        "  • РАЗМЕР КАДРА: medium close-up (по грудь) → close-up (только лицо), либо medium close-up → "
        "    OTS, либо close-up → medium shot (по пояс с жестом). НЕ две medium close-up подряд с того же "
        "    угла. Менять «насколько близко» — самый сильный визуальный сигнал смены кадра.\n"
        "  • ВЫСОТА/УГОЛ камеры: eye-level → лёгкий low-angle (снизу-вверх, добавляет давления), либо "
        "    eye-level → лёгкий high-angle (сверху-вниз, добавляет уязвимости). Не два eye-level подряд.\n"
        "  • НАПРАВЛЕНИЕ ВЗГЛЯДА в кадре: если в shot 1 говорящий смотрит ВПРАВО (на собеседника справа), "
        "    то в shot 2 на этом собеседнике он смотрит ВЛЕВО (на исходного говорящего). Зеркальная "
        "    геометрия экрана — обязательна.\n"
        "В тексте промпта пиши ракурсы КОНКРЕТНО ПО ПАРАМ. Не «cut на Adrian», а:\n"
        "  «OTS через ЛЕВОЕ плечо Clara, средний крупный план Adrian-а во весь кадр справа, eye-level, "
        "  он смотрит влево вниз на Clara» → следующая склейка → «reverse — OTS через ПРАВОЕ плечо Adrian-а, "
        "  close-up только лицо Clara слева, лёгкий low-angle (мы смотрим из-под подбородка Adrian-а на её "
        "  лицо), она смотрит вправо вверх на Adrian-а».\n"
        "ЗАПРЕЩЁННЫЙ ПАТТЕРН (как было в ep27): «medium close-up Clara → medium close-up Adrian → medium "
        "close-up Clara → medium close-up Adrian» — все с одного и того же угла, одного размера, "
        "eye-level. Это и есть «телепорт лицом» в глазах зрителя. ОБЯЗАТЕЛЬНО варьируй размер/угол/сторону.\n"
        "САМОПРОВЕРКА перед выводом: для каждой пары соседних shot-bit'ов в чанке посмотри — отличаются ли "
        "они хотя бы двумя из (сторона, размер, высота, направление взгляда)? Если оба читаются как "
        "«medium close-up чел смотрит вперёд» — переделай, добавь зеркальную сторону и смену размера кадра.\n\n"
        "EYE-LINE / ВЗГЛЯД ПЕРСОНАЖЕЙ — КРИТИЧНО (частый баг 'герои смотрят в разные стороны'):\n"
        "Дефолтное правило: ВО ВРЕМЯ ДИАЛОГА персонажи смотрят на собеседника. Если в кадре двое и они "
        "разговаривают — их взгляды направлены друг на друга. Если троих — говорящий смотрит на адресата реплики, "
        "слушатели смотрят на говорящего. БЕЗ явной директивы Seedance часто поворачивает героев лицом в камеру "
        "или в случайные стороны — получается визуально странно ('сидят рядом, говорят, но смотрят мимо').\n"
        "Правила:\n"
        "  1. В ACTION для КАЖДОГО диалогового шот-бита явно указывай куда направлен взгляд:\n"
        "     – 'Maya смотрит Ethan-у в глаза, говоря «...»'\n"
        "     – 'Ethan не отрывает взгляд от Maya, отвечая «...»'\n"
        "     – 'Maya отводит взгляд к двери на секунду, потом снова смотрит на Ethan-а'\n"
        "  2. В CAMERA для two-shot / OTS дополняй направлением eye-line:\n"
        "     – 'over-the-shoulder с правой стороны Maya, в кадре её затылок и лицо Ethan-а, его глаза направлены на Maya'\n"
        "     – 'medium two-shot, Maya слева смотрит вправо на Ethan-а, Ethan справа смотрит влево на Maya'\n"
        "  3. ИСКЛЮЧЕНИЯ — ОБЯЗАТЕЛЬНО следуй сценарию если он явно указывает другое направление взгляда:\n"
        "     – 'Maya смотрит в окно / на улицу / вдаль' → взгляд НЕ на собеседника, а в указанном направлении\n"
        "     – 'Maya отворачивается / прячет лицо / смотрит в пол / закрывает глаза' → отрази в ACTION\n"
        "     – 'Maya говорит сама с собой / в зеркало / на телефон / в камеру для записи' → не на собеседника\n"
        "     – Монолог одного перса без собеседника рядом → взгляд по контексту (вдаль, на предмет, в потолок)\n"
        "     – Эмоциональное избегание ('не может смотреть ему в глаза', 'отводит взгляд') явно указано\n"
        "     – Камера/wide shot — герой стоит спиной к камере / в три четверти / профиль (но даже тогда eye-line "
        "       к собеседнику если они разговаривают)\n"
        "  4. ШОТ-БИТЫ В DIALOGUE: при переходе на нового говорящего, помимо склейки/наезда укажи направление взгляда:\n"
        "     – 'Склейка на крупный план Maya — она смотрит прямо на Ethan-а, не моргая. Maya (@Image2), холодно: \"...\"'\n"
        "     – 'Камера переходит на Ethan через плечо Maya — Ethan встречает её взгляд. Ethan (@Image1), тихо: \"...\"'\n"
        "  5. Если в SUBJECT перечислены 3+ персонажей в кадре, в ACTION укажи кто на кого смотрит:\n"
        "     – 'Liam стоит между ними — переводит взгляд с Maya на Ethan-а, ловя их перепалку'\n"
        "  6. Когда сцена 'разговор по телефону' или 'через стекло' — собеседник физически не в кадре, "
        "     но взгляд героя направлен на телефон/трубку/стекло, не блуждает.\n"
        "Игнорировать это правило = персы будут смотреть в случайные стороны и видео визуально развалится.\n\n"
        "BODY ORIENTATION + SHOT/REVERSE-SHOT — КРИТИЧНО (частый баг 'слушатель отвернулся, говорящий смотрит в камеру'):\n"
        "Этот баг возникает потому, что без явных директив Seedance ставит обоих героев фронтально к камере "
        "(как для фото), а не друг к другу — получается две головы лицом в объектив, а между ними пустое "
        "пространство. Эмоционально это читается как «они в разных вселенных, не разговаривают».\n"
        "Правила КОТОРЫЕ НАДО ВСТАВЛЯТЬ В КАЖДЫЙ ДИАЛОГОВЫЙ shot-бит ИЛИ В CAMERA:\n"
        "  1. КОРПУС/ПОЗА: слушатель повёрнут КОРПУСОМ к говорящему (не задом, не на 90° в сторону). "
        "     Когда в кадре двое говорящих и видим обоих — оба повёрнуты ТОРСАМИ ДРУГ К ДРУГУ под ~30-60° к камере, "
        "     не фронтально 0°. Прямо так и пиши: 'Maya обращена корпусом к Ethan-у, не к камере'.\n"
        "  2. ЗАПРЕТ ВЗГЛЯДА В ЛИНЗУ: ни говорящий, ни слушатель НИКОГДА не смотрят прямо в объектив во время "
        "     диалога (это разрушает четвёртую стену). Lens-direct gaze разрешён ТОЛЬКО когда сценарий явно "
        "     просит 'смотрит в камеру', 'POV-ракурс собеседника' или 'speech to audience'. По дефолту "
        "     добавляй в ACTION: 'взгляд НЕ в камеру, направлен на [имя собеседника]'.\n"
        "  3. ОБЯЗАТЕЛЬНАЯ АЛЬТЕРНАЦИЯ ракурсов между shot-битами разных говорящих (shot/reverse-shot, "
        "     правило 180°): если предыдущий shot-бит был OTS со стороны Maya (через её плечо на Ethan-а), "
        "     следующий shot-бит при смене говорящего ОБЯЗАН быть ЗЕРКАЛЬНЫМ — OTS со стороны Ethan-а "
        "     (через его плечо на Maya). НЕЛЬЗЯ два подряд OTS-а с одной и той же стороны — это и есть "
        "     'оба кадра выглядят одинаково'. То же для medium close-up: если первый был frontal на Maya, "
        "     второй на Ethan-а должен быть с противоположного направления камеры (Ethan слева смотрит "
        "     вправо vs Maya справа смотрит влево — eye-lines встречаются по экранной геометрии).\n"
        "  4. ПРЯМОЙ ЯЗЫК ДЛЯ CAMERA при смене говорящего:\n"
        "     – 'reverse shot — теперь OTS со стороны Ethan-а, в кадре его затылок справа и лицо Maya слева, "
        "        Maya смотрит вправо вверх на Ethan-а, корпус развернут к нему'\n"
        "     – 'cut на medium close-up Ethan-а, eye-line влево (туда, где Maya была в предыдущем кадре), "
        "        чтобы экранная геометрия сошлась'\n"
        "  5. ФОНОВЫЙ ПЕРСОНАЖ (когда в two-shot один на foreground, второй на background): фоновый "
        "     ОБЯЗАТЕЛЬНО смотрит на foreground-героя (затылок переднего + лицо заднего, направленное "
        "     к переднему). НЕ 'передний у стола, задний смотрит в камеру' — это и есть бракованный ракурс. "
        "     Пиши явно: '@Image2 на заднем плане, смотрит на @Image1 (передний план), не на камеру'.\n"
        "  6. САМОПРОВЕРКА перед выводом каждого compose: для соседних shot-бит разных говорящих — посмотри "
        "     по описанию: чувствуется ли разница ракурсов? Если оба читаются как 'X смотрит вперёд, Y тоже "
        "     смотрит вперёд' — пересмотри, добавь направления взглядов и зеркальные OTS.\n\n"
        "АНТИ-ДУБЛИРОВАНИЕ ПЕРСОНАЖЕЙ — КРИТИЧНО (частый баг 'две Maya в одном кадре'):\n"
        "Seedance с reference-* может рендерить персонажа дважды если получит несколько визуальных "
        "источников одного и того же перса. ЖЁСТКИЕ правила:\n"
        "  1. Каждый персонаж = РОВНО ОДИН char-ref в массиве refs[]. Не пихай Maya дважды с разными outfit'ами. "
        "Если по сценарию ей надо переодеться — это либо новая сцена (тогда новый compose с новым outfit'ом), "
        "либо явный wardrobe-change внутри сцены (тогда выбираешь финальный outfit для всего чанка).\n"
        "  2. Если continuity-кадр (lastframe / cutframe) уже содержит персонажа Maya — это композиционный "
        "референс расстановки, НЕ повод добавить второй ref для Maya. У Maya остаётся ОДИН char-ref (@Image1) "
        "плюс continuity-кадр как @Image_LF — этого Seedance'у достаточно. Если ты добавишь Maya base portrait "
        "и Maya outfit и lastframe c Maya — она появится в кадре дважды или трижды.\n"
        "  3. В SUBJECT/ACTION/SCENE НЕ повторяй @ImageN многократно. После BINDING-строки используй имя. "
        "Не пиши: 'Maya у двери. @Image1 говорит. @Image1 поворачивается.' — Seedance может сплитнуть "
        "это в 3 разные Maya. Пиши слитно с именем: 'Maya, поворачиваясь у двери, говорит ...'. "
        "@ImageN допустим ТОЛЬКО в DIALOGUE shot-bit'ах (для lipsync-привязки) и в BINDING.\n"
        "  4. Если в кадре ОДИН перс (моноспикер монологом) — refs может содержать только: 1 char-ref + "
        "1 loc-ref (= 2 ref'а минимум) или плюс continuity-кадр. Не раздувай 5 рефами одного перса.\n"
        "  5. Легитимный кейс двух Maya — ТОЛЬКО если сценарий явно говорит про зеркало/двойника/раздвоение "
        "('Maya видит себя в отражении', 'два разных временных Maya'). В этом случае пиши явно: "
        "'@Image1 — настоящая Maya у окна, отражение в зеркале справа дублирует её' — и это всё равно "
        "ОДИН char-ref.\n"
        "  6. (УДАЛЕНО) Раньше здесь было обязательное «CAST IN FRAME: ровно N — …». "
        "     Правило снято: не давало обещанной защиты от дублей лиц И мешало legitimate "
        "     extras (пустые залы суда / рестораны), И принуждало модель буквально рендерить "
        "     N статичных фигур в кадре — иногда вторая фигура появлялась за спиной первого "
        "     в моменты когда композитор хотел close-up одного. НЕ декларируй headcount в SUBJECT.\n"
        "  7. UNNAMED EXTRAS / БЕЗЫМЯННЫЕ ПЕРСОНАЖИ (адвокат, охранник, прохожий, официант) — "
        "     САМЫЙ ЧАСТЫЙ источник бага 'два одинаковых лица в кадре'. У них НЕТ char-ref'а, поэтому "
        "     Seedance копирует лицо ближайшего ref-перса (главгероя). Правила:\n"
        "     (a) Если по сценарию extra нужен В КАДРЕ — в SUBJECT обязательно дай ему ОТЛИЧИТЕЛЬНОЕ "
        "         описание которое ВЕРБАЛЬНО ОТТАЛКИВАЕТСЯ от всех ref-персов: возраст-противоположность, "
        "         другая раса/телосложение/причёска/борода/очки. Пример: главгерой — 'седой мужчина 55+ "
        "         в синем костюме, очки' → extra-адвокат должен быть 'молодой мужчина 30 лет, бритый, "
        "         без очков, в чёрной мантии' (а НЕ 'мужчина в костюме'). Иначе Seedance клонирует главгероя.\n"
        "     (b) В ACTION пиши явно: 'extra-адвокат — НЕ похож на @Image1, другое лицо, другой возраст'. "
        "         Это negative-prompt пункт.\n"
        "     (c) Если extra можно убрать из кадра без потери смысла — УБЕРИ. 'Marcus стоит у стола, "
        "         адвокат за кадром слышен голос' лучше чем рисовать двух мужчин рядом.\n"
        "     (d) Если в сценарии ДВА именованных персонажа с похожей внешностью (двое 50-летних мужчин "
        "         в костюмах) — в BINDING/ACTION усили различия: 'Marcus — седые волосы, очки в роговой "
        "         оправе. Hartwell — лысый, без очков, седая борода'. БЕЗ этого Seedance их сольёт в "
        "         близнецов.\n"
        "  8. ЗАПРЕЩЁННЫЕ СИММЕТРИЧНЫЕ КОМПОЗИЦИИ когда в refs только ОДИН char-ref:\n"
        "     – 'окружена двумя мужчинами' / 'flanked by two men' / 'между двух фигур' — НЕТ. Модель "
        "       автоматически продублирует единственный мужской ref на обе позиции.\n"
        "     – 'на фоне толпы похожих людей' — НЕТ. Толпа клонирует ref-лицо.\n"
        "     – Зеркальное расположение двух людей по бокам от третьего — НЕТ если в refs не два разных перса.\n"
        "     Используй ассиметрию: один человек на foreground + локация на background, без фигур-двойников.\n"
        "  9. САМОПРОВЕРКА перед выводом:\n"
        "     – Если в SUBJECT/ACTION/SCENE упомянуты несколько персонажей — у каждого NAMED перса должен быть свой char-ref. "
        "       Безымянных extras прописывай только с отличающей фразой (см. п.7).\n"
        "     – КРИТИЧНО: Проверь chunk_text на ВСЕ named characters. Если персонаж совершает любое visible action "
        "       (бежит, исчезает, оборачивается, смотрит, тянется, кричит, исчезает за углом) — он ДОЛЖЕН быть в refs[]. "
        "       Единственное исключение — когда персонаж УПОМИНАЕТСЯ только в диалоге («где Leo?») без visible action: "
        "       тогда его в refs НЕ кладёшь. Если хотя бы один глагол действия привязан к имени — ref ОБЯЗАТЕЛЕН. "
        "       Прод-инцидент 2026-05-19: Leo в chunk_text 'Leo исчезает в другой стороне' / 'Rex замечает что Leo "
        "       нет рядом' — composer выбросил Leo из refs → Seedance нарисовал галлюцинацию вместо канонического Leo.\n"
        "     – При CLOSE-UP / MEDIUM CLOSE-UP одного спикера — формулировка должна явно убирать остальных из кадра: "
        "       «close-up Adrian, остальные out-of-frame». БЕЗ этой фразы Seedance часто всунет вторую фигуру на задний план.\n"
        "     – Если число персонажей в SUBJECT > 1 и char-ref только один — это красный флаг, либо убери "
        "       extras, либо дай им жёсткую визуальную дифференциацию.\n\n"
        # BINDING-format/appearance section REMOVED 2026-05-18 per council debate
        # finding: server auto-injects canonical char descriptions into BINDING
        # AFTER composer output (see _canonical_char_description + injection at
        # ~line 16527), so instructing composer about clothing/appearance rules
        # was dead weight (~2k tokens, behaviorally inert). Composer just needs
        # to emit '@Image1=Ethan, @Image2=Maya, @Image3=Lobby' — server fills rest.
        "ОПИСАНИЕ ПЕРСОНАЖА — короткая инструкция:\n"
        "  • В BINDING-строке пиши только имя: '@Image1=Ethan, @Image2=Maya, @Image3=Lobby'. "
        "Сервер сам допишет одежду/внешность из карточки персонажа.\n"
        "  • В SUBJECT/ACTION одежду/внешность НЕ описывай — это сделает сервер. "
        "Описывай только лицо/поза/действие/эмоция.\n"
        "  • ИЗМЕНЁННОЕ СОСТОЯНИЕ одежды (порвана, мокрая, в крови) — упоминай родовым словом без цвета: "
        "'разорванная блузка', 'мокрая рубашка'. Цвет уже в карточке."
    )
    # Active episode cast & locations (already checked off in sidebar)
    active_char_ids = set(ep.get('characters_used') or [])
    active_loc_ids  = set(ep.get('locations_used') or [])
    active_chars = [c for c in (s.get('characters') or []) if c['id'] in active_char_ids]
    active_locs  = [l for l in (s.get('locations') or []) if l['id'] in active_loc_ids]

    # Voice-only marker — composer must SEE this character is off-screen voice
    # only. Without an explicit «🔊 VOICE-ONLY» tag in the roster, composer
    # treats them as regular present characters and places them in the room.
    _VOICE_ONLY_LINE_RE = re.compile(
        r'^\s*(?:voice\s+(?:on|via|through|over)\s+phone|voice-?on-?phone'
        r'|off[\s\-]?screen\s+voice|voiceover|voice[\s\-]?only|via\s+phone'
        r'|on\s+the\s+phone\s+(?:from|in)|phone\s+voice|голос\s+по\s+телефону'
        r'|голос\s+за\s+кадром|закадровый\s+голос)\b',
        re.IGNORECASE,
    )

    def _active_char_line(c):
        app_raw = (c.get('appearance') or '')
        is_voice_only = bool(_VOICE_ONLY_LINE_RE.match(app_raw.strip()))
        if is_voice_only:
            # Loud marker — voice-only character. Two-part directive:
            # (1) INCLUDE @ImageN ref so Seedance has voice/lipsync anchor
            # (2) DON'T render them visually in the frame
            # Real prod bug 2026-05-25 «My Stepmother» ep 53 chunk 5: composer
            # interpreted «НЕ В КАДРЕ» as «исключи из refs» → Detective Morris
            # говорил реплику без ref → Seedance hallucinated his voice.
            # Server-side cast_restriction code already splits voice-only chars
            # into a separate «VOICE-ONLY off-screen» group, so the composer
            # SHOULD include them in refs whenever they speak.
            line = (
                f"- 🔊 VOICE-ONLY {c['name']} (id={c['id']}): голос через телефон/интерком/V.O.\n"
                f"  ВКЛЮЧАЙ его @ImageN в refs если он ГОВОРИТ в этом чанке "
                f"(нужно для голосовой консистентности lipsync). "
                f"НО в SUBJECT/ACTION/SCENE физически НЕ описывай — он не в кадре. "
                f"Реплики оформляй как «{c['name']} (V.O.) from phone speaker: \"...\"» "
                f"или «off-screen voice through phone». "
                f"Original appearance: «{app_raw[:80]}»."
            )
        else:
            line = f"- {c['name']} (id={c['id']}): {app_raw[:120]}"
        labels = [o.get('label') for o in (c.get('outfits') or []) if o.get('avai_url') and o.get('label')]
        if labels and not is_voice_only:
            line += "\n  outfit-варианты: " + ", ".join(f'"{l}"' for l in labels) + ", null"
        return line
    active_chars_block = '\n'.join(_active_char_line(c) for c in active_chars) or '(не отмечены)'
    active_locs_block = '\n'.join(
        f"- {l['name']} (id={l['id']}): {l.get('description','')[:120]}"
        for l in active_locs
    ) or '(не отмечены)'

    # ── LOCKED-REFS mode ─────────────────────────────────────────────────────
    # When caller passes `locked_refs`, Claude must use EXACTLY those — no extra
    # auto-detection. Used by manual "Перекомпоновать с текущими рефами" after
    # the user pruned the ref list.
    locked_refs_block = ''
    if locked_refs:
        # Build a human-readable summary of the locked roster + a strict directive.
        char_lookup = {c['id']: c for c in (s.get('characters') or [])}
        loc_lookup  = {l['id']: l for l in (s.get('locations') or [])}
        locked_lines = []
        for r in locked_refs:
            kind = (r.get('kind') or '').lower()
            rid = r.get('id') or ''
            outfit = r.get('outfit') or ''
            if kind == 'char':
                ch = char_lookup.get(rid)
                if ch:
                    appearance = (ch.get('appearance') or '')[:120]
                    suffix = f' / outfit="{outfit}"' if outfit and outfit != 'base' else ''
                    locked_lines.append(f'- char "{ch["name"]}" (id={rid}{suffix}): {appearance}')
            elif kind == 'loc':
                lc = loc_lookup.get(rid)
                if lc:
                    locked_lines.append(f'- loc "{lc["name"]}" (id={rid}): {(lc.get("description") or "")[:120]}')
            elif kind == 'lastframe':
                locked_lines.append('- lastframe (continuity reference — not a character)')
            elif kind == 'cutframe':
                locked_lines.append('- cutframe (continuity reference — not a character)')
            elif kind == 'url':
                locked_lines.append(f'- custom image url ref ({rid}) — treat as a fixed visual element')
        locked_refs_block = (
            '\n=== LOCKED REFS — STRICT MODE ═══\n'
            'Caller pinned the EXACT refs[] for this composition. Do NOT pick anything else, '
            'do NOT auto-detect missing characters from the chunk, do NOT add the location '
            'unless it is in the list below. Your refs[] output MUST match this list 1-to-1 '
            'in the same order, with the same `outfit` value for each char.\n'
            'PINNED ROSTER:\n' + '\n'.join(locked_lines) + '\n'
            'If the chunk text mentions characters NOT in this roster — DO NOT bind them to @ImageN, '
            'DO NOT describe them in SUBJECT/SCENE. Treat them as off-screen. The viewer will not see '
            'them in this shot. Rewrite the prompt to focus on the pinned roster only.\n'
            '=== END LOCKED REFS ═══\n'
        )

    full_script = (ep.get('script') or '').strip()
    full_script_block = full_script[:8000]  # safety cap

    base_only_block = ''
    if base_outfits_only:
        base_only_block = (
            "\n=== BASE OUTFITS ONLY — STRICT ===\n"
            "Для КАЖДОГО персонажа в refs ВСЕГДА выставляй \"outfit\": null. "
            "Не подбирай и не упоминай альтернативные outfit-варианты этого персонажа "
            "(костюмы для других сцен, формы, повседневные вариации). "
            "Используется только базовое референс-фото каждого персонажа. "
            "В тексте промпта тоже не описывай специфическую одежду которая отличается от базы — "
            "только то что видно на базовом референсе.\n"
            "=== END BASE OUTFITS ONLY ===\n"
        )

    # Heuristic auto-detection of single-speaker chunks → soft hint to composer.
    # Looks for: (a) exactly 1 unique speaker in chunk, (b) ZERO bracketed/prose
    # action remarks (those almost always involve a 2nd actor or physical
    # interaction), (c) no other char names in non-dialogue text — checked via
    # canonical name AND first-name-token (e.g. "WOLF" matches both WOLF and
    # WOLF_SON), and we DEFER the hint when any uppercase Cyrillic name-like
    # token appears in action text (likely a Russian declension of a character
    # name not covered by our English aliases — e.g. "Волчонка" → WOLF_SON).
    # Was burning composer in the wolf-bull warehouse scene where Bull pushes
    # WOLF_SON forward in a bracketed remark — heuristic fired single-speaker,
    # composer dropped the cub from refs.
    active_char_names = [c['name'] for c in active_chars]
    # Build alias set per active char: canonical name + first-name-token +
    # last-name-token (e.g. "WOLF_SON" → {"wolf_son", "wolf"}; "DR OLIVER
    # CROSS" → {"dr oliver cross", "dr", "cross", "dr cross"}). Helps match
    # both stems and abbreviated forms (script writes "DR CROSS:" while
    # series stores "DR OLIVER CROSS").
    def _aliases(name):
        al = {name.lower()}
        tokens = re.split(r'[\s_]+', name.strip())
        tokens = [t for t in tokens if t]
        if tokens:
            al.add(tokens[0].lower())
            if len(tokens) > 1:
                al.add(tokens[-1].lower())
            # First + last token combined ("DR CROSS" from "DR OLIVER CROSS")
            if len(tokens) >= 3:
                al.add(f'{tokens[0]} {tokens[-1]}'.lower())
        return al

    # Detect ALL speakers in this chunk and surface them as a HARD directive in
    # the composer prompt. Composer occasionally drops a speaker (especially on
    # entrance shots where Vision-analyzed prev lastframe shows only one char
    # and the Russian-declined name in [Дверь… Dr Cross входит] doesn't match
    # the Vision state-analysis output). Forcing speakers into refs[] eliminates
    # this whole class of "the second speaker isn't in the scene" failures.
    chunk_speakers = []
    if chunk_text and active_char_names:
        seen_speakers = set()
        for line in chunk_text.split('\n'):
            stripped = line.lstrip()
            m = re.match(r'^([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-\']{0,30}):\s', stripped)
            if not m: continue
            nm = m.group(1).strip().lower()
            for cn in active_char_names:
                if nm in _aliases(cn) and cn not in seen_speakers:
                    chunk_speakers.append(cn)
                    seen_speakers.add(cn)
                    break

    auto_close_up_block = ''
    if not close_up_only:
        if active_char_names and chunk_text:
            speakers_found = set(chunk_speakers)
            if len(speakers_found) == 1:
                sole = next(iter(speakers_found))
                # Strip quoted strings + dialogue cue tails so we only check action-text
                action_only = re.sub(r'"[^"]*"', '', chunk_text)
                action_only = re.sub(r'^[A-Za-zА-Яа-яЁё][^:\n]{0,30}:.*$', '', action_only, flags=re.MULTILINE)
                action_only_stripped = action_only.strip()
                others_in_action = []
                for cn in active_char_names:
                    if cn.lower() == sole.lower():
                        continue
                    for alias in _aliases(cn):
                        if re.search(r'\b' + re.escape(alias) + r'\b', action_only, re.IGNORECASE):
                            others_in_action.append(cn)
                            break
                # Tripwire: ANY bracketed [...] action-prose or substantial
                # parenthesized prose almost always involves a 2nd actor.
                # Don't risk dropping refs in those cases.
                bracketed_action = bool(re.search(r'\[[^\]]{8,}\]', action_only))
                paren_action = bool(re.search(r'\([^\)]{12,}\)', action_only))
                has_action_prose = bool(action_only_stripped) and (bracketed_action or paren_action or len(action_only_stripped) > 30)
                # Tripwire: any Cyrillic capitalized word in action text — almost
                # certainly a Russian-declined character name not in our alias set.
                has_cyrillic_proper = bool(re.search(r'[А-ЯЁ][а-яё]{2,}', action_only))
                trigger_close_up = (
                    not others_in_action
                    and not has_action_prose
                    and not has_cyrillic_proper
                )
                if trigger_close_up:
                    auto_close_up_block = (
                        f"\n=== AUTO-DETECTED: SINGLE-SPEAKER CHUNK ===\n"
                        f"В этом CHUNK'е говорит ровно ОДИН персонаж ({sole}) и нет упоминаний "
                        f"других персов в action-ремарках. Это сильный сигнал на CLOSE-UP / single-subject шот.\n"
                        f"DEFAULT: refs = [{sole} + локация]. НЕ ДОБАВЛЯЙ других персов даже если они "
                        f"были в IN FRAME предыдущего чанка — для ЭТОГО кадра они вне фрейма.\n"
                        f"В CAMERA пиши 'крупный план {sole}' или 'medium close-up {sole}', НЕ 'over-the-shoulder', "
                        f"НЕ 'wide', НЕ группу.\n"
                        f"Исключение: если в continuity-кадрах ENDING STATE предыдущего чанка явно описывает "
                        f"физическое взаимодействие (держат за горло, обнимают и т.п.) И это продолжается "
                        f"на текущем CHUNK'е — тогда необходимый второй перс остаётся.\n"
                        f"=== END AUTO-DETECTED ===\n"
                    )

    close_up_block = ''
    if close_up_only:
        close_up_block = (
            "\n=== CLOSE-UP ONLY MODE — STRICT ===\n"
            "Это CLOSE-UP / single-subject шот. Жёсткие правила:\n"
            "  1. refs[] = МАКСИМУМ ОДИН char-ref (тот кто говорит больше всего слов в CHUNK'е "
            "или единственный спикер) + локация. ВСЁ.\n"
            "  2. Игнорируй persons из 'IN FRAME' предыдущего чанка — они присутствуют в сцене "
            "но НЕ в этом кадре. Не добавляй их в refs.\n"
            "  3. Continuity-кадры (lastframe / cutframes) тоже могут принести лица других персов в кадр. "
            "Они всё ещё прицепятся как композиционные референсы, НО в тексте промпта явно скажи: "
            "'close-up на лицо <Имя> (face filling frame), остальные вне кадра, размытый/тёмный фон'.\n"
            "  4. В CAMERA блоке промпта: 'close-up' (дефолт), 'macro close-up' (extreme emotion) или 'medium close-up' — "
            "не 'wide', не 'two-shot', не 'group'.\n"
            "  5. SCENE: упомяни локацию через @Image, но добавь 'фон вне фокуса / приглушён' "
            "чтобы Seedance не пытался прорисовать остальных людей в задних планах.\n"
            "=== END CLOSE-UP ONLY ===\n"
        )

    style_block = ''
    if style_override:
        style_block = (
            "\n=== STYLE OVERRIDE (важно) ===\n"
            f"Финальный визуальный стиль клипа: «{style_override}».\n"
            "Перепиши блок STYLE/ATMOSPHERE целиком вокруг этого стиля. Также добавь "
            "стилистические маркеры в SUBJECT и SCENE (рендер/материалы/освещение/палитра/линии лиц). "
            "Если стиль предполагает анимацию или нереалистичный рендер (Pixar / anime / claymation / oil painting) — "
            "явно укажи это в первых 20 словах промпта (модель решает рендер по началу). "
            "Реплики персонажей в кавычках НЕ менять. Continuity-логику (персонажи в кадре, позы, локация) "
            "сохрани как обычно — стиль это только оболочка рендера, не сюжет.\n"
            "=== END STYLE ===\n"
        )
    # Hard directive block — server-detected speakers in this chunk that
    # composer must not drop from refs[]. Empty when chunk has no recognized
    # dialogue lines (action-only or extras-only chunks).
    mandatory_speakers_block = ''
    if chunk_speakers:
        mandatory_speakers_block = (
            f"=== ОБЯЗАТЕЛЬНЫЕ СПИКЕРЫ (детектировано сервером в CHUNK) ===\n"
            f"Эти персонажи ГОВОРЯТ в этом чанке (есть строки 'Name:' с их именем или алиасом). "
            f"Они ВИДНЫ В КАДРЕ во время своей реплики (даже если на lastframe их не было — реплика "
            f"это и есть момент их появления). ВКЛЮЧИ их в refs[] и в BINDING — невключение спикера "
            f"= провал композиции:\n"
            + '\n'.join(f"  - {nm}" for nm in chunk_speakers) + '\n'
            + f"=== END ОБЯЗАТЕЛЬНЫЕ СПИКЕРЫ ===\n\n"
        )

    # Active items in THIS episode (for the prompt's "ACTIVE THIS EPISODE" section)
    active_item_ids = set(ep.get('items_used') or [])
    active_items_block = '\n'.join(
        f"  - {it['name']} (id={it['id']})"
        for it in (s.get('items', []) or [])
        if it.get('id') in active_item_ids and it.get('avai_url')
    ) or '  (none)'

    # Pull [BLOCKING] / [BLOCKING_OUT] fences out of the script for the scene
    # this chunk belongs to. Author-written setup (outfits, positions, lighting,
    # props) is 0-chrono in the segmenter but BECOMES authoritative context here.
    # episode_blocks = constants for the whole episode; scene_blocks_for_chunk =
    # opening setup of THIS chunk's scene + closing setup of the prior scene
    # (for cross-scene continuity).
    try:
        episode_blocks, scene_blocks_for_chunk = _extract_script_blocking(
            full_script_block or (ep.get('script') or ''),
            chunk_text,
        )
    except Exception as _e:
        episode_blocks, scene_blocks_for_chunk = '', ''
        print(f'[seedance_compose] blocking extract failed: {_e}', flush=True)

    blocking_block = ''
    if episode_blocks or scene_blocks_for_chunk:
        parts = []
        if episode_blocks:
            parts.append(f"EPISODE-WIDE CONSTANTS (одежда, базовая палитра, общие пропсы):\n{episode_blocks}")
        if scene_blocks_for_chunk:
            parts.append(f"SCENE OPENING SETUP (где стоят, что держат, освещение, состояние — для ЭТОЙ сцены):\n{scene_blocks_for_chunk}")
        blocking_block = (
            "AUTHOR BLOCKING — постановка из [BLOCKING]…[/BLOCKING] блоков сценария. "
            "Это АВТОРИТАТИВНАЯ постановка: позы, наряды, освещение, реквизит, мизансцена. "
            "Используй ИМЕННО эти позиции / outfits / атрибуты в prompt-е; если что-то в CHUNK не описано — бери из блокинга.\n"
            + "\n\n".join(parts)
            + "\n\n"
        )

    userprompt = (
        f"AVAILABLE CHARACTERS (весь roster серии):\n{chr(10).join(chars_lines) or '(none)'}\n\n"
        f"AVAILABLE LOCATIONS (весь roster серии):\n{chr(10).join(locs_lines) or '(none)'}\n\n"
        f"AVAILABLE ITEMS (сюжетные предметы — букеты, конверты, локеты, флешки и т.п.):\n{chr(10).join(items_lines) or '(none)'}\n\n"
        f"ACTIVE THIS EPISODE (отмечены в эпизоде — приоритет при выборе):\n"
        f"  Characters:\n{active_chars_block}\n"
        f"  Locations:\n{active_locs_block}\n"
        f"  Items:\n{active_items_block}\n"
        f"{base_only_block}"
        f"{close_up_block}"
        f"{auto_close_up_block}"
        f"{locked_refs_block}"
        f"{style_block}"
        f"{prev_block}\n"
        f"{blocking_block}"
        f"FULL EPISODE SCRIPT (читай ВЕСЬ — тут scene headings, ремарки, кто где находится):\n"
        f"```\n{full_script_block}\n```\n\n"
        f"CHUNK — выделенный кусок для генерации (его репликам сохраняй verbatim):\n"
        f"```\n{chunk_text}\n```\n\n"
        f"Найди CHUNK внутри FULL SCRIPT, посмотри ближайший SCENE HEADING выше него — оттуда возьми локацию и время суток. "
        f"Посмотри ремарки/[действия] вокруг CHUNK — оттуда возьми кто физически в кадре (включая молчащих). "
        f"Эти персонажи ОБЯЗАТЕЛЬНО идут в refs, даже если в CHUNK у них нет реплик.\n\n"
        f"{mandatory_speakers_block}"
        "Верни JSON и НИЧЕГО кроме JSON:\n"
        "{\n"
        '  "prompt": "ru/en motion prompt, ~60-110 слов, по структуре выше, с эмоциями перед каждой репликой и финальным @Image<N> локации",\n'
        '  "refs": [{"kind":"char","id":"...","outfit":"label_or_null"}, ..., {"kind":"item","id":"..."}, ..., {"kind":"loc","id":"..."}],\n'
        '  "scene_continuity": true|false,\n'
        '  "framing": "close_up_single" | "medium_single" | "ots" | "two_shot" | "profile_two" | "tracking_shot" | "action_wide" | "wide",\n'
        '  "framing_anchor": "<имя персонажа foreground для OTS — чьим плечом загораживаем>; null для остальных framing\'ов",\n'
        '  "reasoning": "одно предложение — почему именно эти референсы, continuity и framing"\n'
        "}\n\n"
        "FRAMING — обязательное поле, выбирай ОДИН вариант:\n"
        "  • close_up_single / medium_single — в refs ОДИН персонаж + локация (плюс items). Используй когда в кадре правда один герой.\n"
        "  • ots — статичный диалог лицом к лицу, камера за плечом anchor'а; в refs оба + локация; framing_anchor = имя того кого видим со спины.\n"
        "  • two_shot — оба в кадре статика (рядом, лицом к камере или под лёгким углом).\n"
        "  • profile_two — оба профилями к камере в тесном пространстве (в машине, за столом, плечом-к-плечу).\n"
        "  • tracking_shot — walking-and-talking, оба бок-о-бок, камера движется параллельно.\n"
        "  • action_wide — физический контакт / драка / групповое действие, оба видны полностью.\n"
        "  • wide — establishing shot или групповая сцена с 3+ персонажами.\n"
        "  ПРИНЦИП: если в refs[] >=2 character'а — framing должен быть ИЗ {ots, two_shot, profile_two, tracking_shot, action_wide, wide}. "
        "Никогда не выбирай close_up_single/medium_single при двух персах в refs — это противоречие и приведёт ко «второй размыто сзади». "
        "Если по контексту нужен close-up — выкидывай второго из refs[] (он молчит и не действует в этом chunk'е).\n\n"
        "ПРОВЕРКА перед выводом:\n"
        "- BINDING-строка (@Image1=<имя>, @Image2=<имя>, ...) идёт ПЕРВОЙ в prompt? Если нет — допиши.\n"
        "- В SUBJECT/ACTION/SCENE используются ИМЕНА (без @ImageN)? Если нашёл @ImageN в этих блоках — замени на имя.\n"
        "- В DIALOGUE shot-bit'ах есть И имя И @ImageN ('Maya (@Image2)')? Это нужно для lipsync.\n"
        "- ВСЕ описания на русском (кроме реплик в кавычках)? Если нашёл английское слово вне кавычек — перепиши.\n"
        "- Каждая реплика из chunk имеет эмоцию/тон по-русски перед ней? Если нет — допиши.\n"
        "- Локация прикреплена последним ref и упомянута в SCENE именем? Если в roster есть подходящая — обязательно.\n"
        "- Все @ImageN из BINDING/DIALOGUE совпадают по индексу с порядком в refs?\n"
        "- В prompt нет 'no <X>'? Замени на позитив.\n"
        "- ВЫБОР OUTFIT: для каждого char-ref значение outfit либо ровно из списка под этим персом в roster, либо null. "
        "Если PREVIOUS CHUNK той же сцены — outfit совпадает с PREVIOUS дословно (см. IN FRAME).\n"
        "- В SUBJECT/ACTION/SCENE НЕТ описания одежды (цвета/типы костюма/ткани). Сервер сам вставит "
        "описание в BINDING из Vision-анализа ref'а. Если нашёл клозет-описание вне BINDING — удали. "
        "Допустимо только описание ИЗМЕНЁННОГО состояния (порвана, мокрая, в крови) родовыми словами.\n"
        "- АНТИ-ДУБЛЬ: каждый персонаж в refs[] ОДИН раз (по id). Никаких повторов одного перса с разными outfit'ами. "
        "Каждый @ImageN в тексте промпта тоже привязан к ровно одному персу."
    )
    try:
        raw = claude_ask(userprompt, system=sysprompt)
        data = json.loads(strip_json(raw))
    except Exception as e:
        return jsonify({'error': f'compose failed: {e}'}), 500

    refs = data.get('refs') or []
    # Filter voice-only characters that composer added without justification.
    # Real prod bug 2026-05-20 «My Stepmother» ep 52: composer auto-pulled
    # Detective Morris (appearance="Voice on phone...") into refs of every
    # chunk even when his name appears NOWHERE in chunk_text. Server-side
    # voice-only handling (BINDING rewrite + cast_restriction) couldn't
    # prevent Seedance from getting his @ImageN reference image and
    # potentially using it. Solution: drop voice-only chars from refs[]
    # unless their name is actually mentioned in chunk_text.
    _voice_only_pattern = re.compile(
        r'^\s*(?:voice\s+(?:on|via|through|over)\s+phone|voice-?on-?phone'
        r'|off[\s\-]?screen\s+voice|voiceover|voice[\s\-]?only|via\s+phone'
        r'|on\s+the\s+phone\s+(?:from|in)|phone\s+voice|голос\s+по\s+телефону'
        r'|голос\s+за\s+кадром|закадровый\s+голос)\b',
        re.IGNORECASE,
    )
    # Snapshot the composer's ORIGINAL ordered refs BEFORE any server-side
    # filtering. The @ImageN tokens in data['prompt'] are positional against
    # THIS list. The tag-remap at the end must diff original→final against this
    # snapshot, NOT against data['refs'] (which the voice-only/strict filter
    # mutates in place below) — otherwise a char dropped by STRICT_CHAR_FILTER
    # is invisible to the remap and its `@ImageN=Name` BINDING clause survives
    # in the prompt with no image + no description. Real bug: «My Wedding Night
    # Mistake» ep4, Lena named only as "her finger" in chunk_text → strict-
    # dropped from refs → prompt still said `@Image2=Lena` with no ref/desc.
    original_refs_for_remap = list(refs)
    dropped_voice_only = []
    dropped_strict = []   # only populated when STRICT_CHAR_FILTER is on
    # Continuity carry-over: ids of chars who were on-screen in the immediately
    # preceding chunk. Used to spare a continuing lead from STRICT_CHAR_FILTER
    # when the chunk_text names them only by pronoun ("he slips it on HER
    # finger"). A spurious roster over-attach (the «Sophie sits in chair» case
    # the filter targets) is absent from the previous chunk, so this gate can't
    # readmit it. Bug: «My Wedding Night Mistake» ep4 ring scene — Lena dropped.
    prev_chunk_char_ids = {
        r.get('id') for r in ((prev_neighbour or {}).get('refs') or [])
        if r.get('kind') == 'char' and r.get('id')
    }
    kept_continuity = []  # strict-drop candidates spared by continuity carry-over
    if refs and chunk_text:
        ct_lower = chunk_text.lower()
        filtered = []
        for r in refs:
            if r.get('kind') == 'char':
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                if ch:
                    name = (ch.get('name') or '').strip()
                    name_in_text = _char_name_in_text(name, chunk_text, ch.get('aliases'))
                    app_field = (ch.get('appearance') or '').strip()
                    # Voice-only filter: drop voice-only chars whose name
                    # not in chunk_text (always on — known-bad pattern).
                    if _voice_only_pattern.match(app_field):
                        if not name_in_text:
                            dropped_voice_only.append({'name': name, 'id': r.get('id')})
                            continue
                    # Strict filter (experimental — STRICT_CHAR_FILTER flag):
                    # drop ANY char whose name isn't in chunk_text. Catches
                    # composer over-attaching from episode roster (e.g. Sophie
                    # «sits in the chair» when chunk only has Emma+Adrian).
                    # Risk: false-positive on physically-present but unnamed
                    # chars («her hand visible at edge»). Dropped chars are
                    # surfaced in compose_warnings so regression is visible.
                    # Rollback: STRICT_CHAR_FILTER=0 env var, or flip default.
                    elif STRICT_CHAR_FILTER and not name_in_text:
                        # Spare a character who was on-screen in the previous
                        # chunk — a pronoun-referenced continuing lead, not a
                        # roster over-attach. Keeps Lena in the ring-exchange
                        # two-shot even though chunk_text says only "her finger".
                        if r.get('id') in prev_chunk_char_ids:
                            kept_continuity.append({'name': name, 'id': r.get('id')})
                        else:
                            dropped_strict.append({'name': name, 'id': r.get('id')})
                            continue
            filtered.append(r)
        if dropped_voice_only or dropped_strict:
            refs = filtered
            data['refs'] = refs
    # If base-only flag is on, strip outfit selection from every char ref
    if base_outfits_only:
        for r in refs:
            if r.get('kind') == 'char':
                r['outfit'] = None
    # CLOSE-UP ONLY post-filter: drop all char-refs except the one whose name
    # actually appears in CHUNK text (tightest single-subject framing). Keeps
    # location refs untouched. Belt-and-suspenders insurance — composer prompt
    # should already produce single-char refs, but if it carried over prev-chunk
    # bystanders we strip them here.
    closeup_dropped = []
    if close_up_only:
        # Keep every SPEAKING character — a char with a dialogue cue ("NAME:")
        # in chunk_text. A close-up of a back-and-forth dialogue is a
        # shot/reverse-shot of close-ups and still needs BOTH faces; dropping a
        # co-speaker's ref makes their reverse shot hallucinate a face. Only
        # non-speaking bystanders are stripped for tight framing.
        # Bug it fixes: a 2-speaker segment (per-line close-up auto-ticks the
        # global close-up flag) kept only the FIRST-NAMED char — often just a
        # stage-direction mention ("Victoria enters") — silently dropping the
        # other speaker (Emma). «My Boss…Sleep With Him» ep4 chunk 3.
        speaker_ids = set()
        for r in refs:
            if r.get('kind') != 'char':
                continue
            ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
            nm = (ch.get('name') or '').strip() if ch else ''
            if not nm:
                continue
            # Dialogue cue: line starts with "NAME:" / "NAME (beat):" / "NAME：".
            cue = re.compile(r'^\s*' + re.escape(nm) + r'\s*[:：(]', re.MULTILINE | re.IGNORECASE)
            if cue.search(chunk_text):
                speaker_ids.add(r.get('id'))
        if speaker_ids:
            keep_ids = speaker_ids
        else:
            # No dialogue cues — a silent action beat. Fall back to a single
            # subject: the first char NAMED in chunk_text (or composer's first).
            chars_in_chunk = []
            ct_lower = chunk_text.lower()
            for r in refs:
                if r.get('kind') != 'char':
                    continue
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                if not ch:
                    continue
                pos = ct_lower.find(ch['name'].lower())
                if pos >= 0:
                    chars_in_chunk.append((r.get('id'), pos))
            chars_in_chunk.sort(key=lambda x: x[1])
            first_id = chars_in_chunk[0][0] if chars_in_chunk else (
                next((r.get('id') for r in refs if r.get('kind') == 'char'), None)
            )
            keep_ids = {first_id} if first_id else set()
        filtered = []
        for r in refs:
            if r.get('kind') == 'char' and r.get('id') not in keep_ids:
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                closeup_dropped.append({'char_id': r.get('id'), 'char_name': ch['name'] if ch else r.get('id')})
                continue
            filtered.append(r)
        refs = filtered
    # Hard de-dup: never let the same character go in twice (a frequent cause
    # of "two Mayas in one frame"). Keep the FIRST occurrence — usually that's
    # the composer's preferred (often outfit-specific) one. Any subsequent
    # ref with the same kind=char + id is dropped and reported.
    deduped_refs = []
    seen_char_ids = set()
    duplicate_chars = []
    for r in refs:
        if r.get('kind') == 'char':
            cid = r.get('id')
            if cid in seen_char_ids:
                duplicate_chars.append({'char_id': cid, 'dropped_outfit': r.get('outfit')})
                continue
            seen_char_ids.add(cid)
        deduped_refs.append(r)
    refs = deduped_refs
    ref_urls = []
    ref_meta = []
    unresolved = []
    outfit_fallbacks = []  # composer requested outfit label that doesn't exist → fell back to base
    for r in refs:
        url = _resolve_ref_url(s, r, sid=sid)
        if url:
            if r.get('_outfit_fallback'):
                # Composer hallucinated an outfit label — log so user sees the drift cause
                ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
                outfit_fallbacks.append({
                    'char_id': r.get('id'),
                    'char_name': ch['name'] if ch else r.get('id'),
                    'requested_outfit': r.get('_outfit_requested'),
                    'available_labels': [o.get('label') for o in (ch.get('outfits') or []) if o.get('avai_url')] if ch else [],
                })
            # Strip private bookkeeping fields before returning to client
            clean = {k: v for k, v in r.items() if not k.startswith('_')}
            ref_urls.append(url)
            ref_meta.append({**clean, 'url': url})
        else:
            unresolved.append({**r, 'reason': 'нет фото / avai_url не получился'})

    # CRITICAL: server-side filtering (close-up, dedup, unresolved) may have
    # dropped refs that the composer's prompt still references via @ImageN.
    # If we don't sync the prompt with the final refs[], Seedance will see
    # @ImageN tokens with no matching reference image and hallucinate a random
    # face for that 'character'. Real bug from production: refs=[Adrian, Lobby]
    # but prompt said @Image2=Vivian, @Image3=Sophie, @Image4=Clara → garbage.
    # Diff the composer's ORIGINAL refs (pre-filter snapshot) against the
    # final resolved refs so @ImageN bindings for chars dropped by the voice-
    # only / strict filter are stripped/renumbered too — not just close-up /
    # dedup / unresolved drops.
    _tag_remap = _build_image_tag_remap(original_refs_for_remap, ref_meta)
    if any(v is None for v in _tag_remap.values()) or any(k != v for k, v in _tag_remap.items() if v is not None):
        data['prompt'] = _remap_image_tags(data.get('prompt') or '', _tag_remap)

    # CANONICAL char description for BINDING — uses the SAME text that was fed
    # to the image generator (appearance + outfit description). Visual ref and
    # textual description are guaranteed aligned.
    # Detect transient undress states (towel / post-shower / robe / shirtless)
    # ONCE here so the BINDING builder below can OVERRIDE a contradicting outfit,
    # and the wardrobe_state_mismatch guard further down can reuse the same map.
    try:
        _undressed = _detect_char_undressed_states(ep.get('script') or '', s.get('characters') or [])
    except Exception:
        _undressed = {}
    _undress_overridden = {}   # char_id -> cue, for accurate guard messaging
    name_to_clothing = {}   # keyed by full name AND first name for prompt lookup
    for r in ref_meta:
        if r.get('kind') != 'char':
            continue
        desc = _canonical_char_description(s, r.get('id'), r.get('outfit'))
        if not desc:
            continue
        # Transient wardrobe state: the script stages this character undressed
        # (towel/post-shower/robe/…) but the chunk assigns a DRESSED outfit (or
        # base, which is dressed). The catalogued desc ("charcoal gray suit")
        # then contradicts the scene and Seedance flips wardrobe between chunks.
        # OVERRIDE the BINDING with the undress state so it holds across the whole
        # span. The reference PHOTO still anchors the face; clothing is text-driven
        # (same lever as the disguise-hair fix). A dedicated undress outfit, if one
        # exists, is detected as already-undress below and left untouched.
        _cue = _undressed.get(r.get('id'))
        if _cue:
            _label = r.get('outfit') or ''
            _ch0 = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
            _odesc = ''
            if _ch0 and _label:
                _o = next((o for o in (_ch0.get('outfits') or []) if o.get('label') == _label), None)
                _odesc = (_o.get('description') or '') if _o else ''
            _hay = f'{_label} {_odesc}'
            _already_undress = bool(_UNDRESSED_OUTFIT_RE.search(_hay))
            if (not _already_undress) and ((not _label) or _DRESSED_OUTFIT_RE.search(_hay)):
                _ov = _binding_desc_with_undress(s, r.get('id'), _cue)
                if _ov:
                    desc = _ov
                    _undress_overridden[r.get('id')] = _cue
        ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
        if ch:
            full_name = ch['name']
            name_to_clothing[full_name] = desc
            # Composers write first-name-only in BINDING (e.g. "Marcus" not "Marcus Bellacourt").
            # Index by first name too so the regex lookup matches.
            first_name = full_name.split()[0]
            if first_name != full_name and first_name not in name_to_clothing:
                name_to_clothing[first_name] = desc
            r['clothing'] = desc          # surface in response so UI can show it
    # Inject descriptions into the BINDING line. Pattern: '@Image1=Maya' →
    # '@Image1=Maya (navy scrubs, hair in bun)'. Only the first occurrence per
    # name so we don't duplicate inside DIALOGUE shot-bits.
    if name_to_clothing:
        prompt_text = data.get('prompt') or ''
        already_injected = set()
        def _inject_clothing(m):
            num = m.group(1)
            name = m.group(2).strip()
            if name in already_injected:
                return m.group(0)
            desc = name_to_clothing.get(name)
            if not desc:
                return m.group(0)
            already_injected.add(name)
            return f'@Image{num}={name} ({desc})'
        # Match `@Image<N>=<Name>` with `=` or `—` or `-` separator
        prompt_text = re.sub(
            r'@Image(\d+)\s*[=—\-]\s*([A-Za-zА-яЁё][A-Za-zА-яЁё\d _\-]{0,30})',
            _inject_clothing, prompt_text
        )
        # Fallback: no @ImageN binding lines found at all → prepend explicit note
        missing = [nm for nm in name_to_clothing if nm not in already_injected
                   and nm.split()[0] not in already_injected]
        # Deduplicate: keep only full names (skip first-name aliases already covered)
        missing_full = [nm for nm in missing if nm in {ch2['name'] for ch2 in (s.get('characters') or [])}]
        if not already_injected:
            # Composer wrote no BINDING line at all — prepend one
            binding_parts = [f'{nm} ({name_to_clothing[nm]})' for nm in missing_full]
            prompt_text = (
                'Note: одежда из refs — ' + ', '.join(binding_parts) + '. '
                'Используй эти описания если упоминаешь одежду; больше ничего о ней не пиши.\n\n'
                + prompt_text
            )
        elif missing_full:
            # Some chars were injected but others were missed (shouldn't happen now,
            # but as a safety net append the missed ones after the binding line).
            extra = ', '.join(f'{nm} ({name_to_clothing[nm]})' for nm in missing_full)
            prompt_text = re.sub(
                r'(В refs:[^\n]+)',
                lambda m2: m2.group(0) + f' Также в сцене: {extra}.',
                prompt_text, count=1
            )
        data['prompt'] = prompt_text

    # Optional: attach previous chunk's LAST FRAME as a continuity reference
    lastframe_attached = False
    if (use_prev_lastframe and prev_neighbour
            and prev_neighbour.get('video_path')
            and data.get('scene_continuity') is not False
            and len(ref_urls) < 9):
        try:
            extracted = _extract_last_frame(sid, prev_neighbour['video_path'])
            if extracted:
                relpath, abs_path = extracted
                lf_url = prev_neighbour.get('lastframe_avai_url')
                if not lf_url:
                    try:
                        lf_url = _avai_upload_local_image(abs_path)
                        prev_neighbour['lastframe_avai_url'] = lf_url
                        # save to whichever episode owns the prev_neighbour
                        save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
                    except Exception:
                        lf_url = None
                if lf_url:
                    img_idx = len(ref_urls) + 1  # 1-based
                    ref_urls.append(lf_url)
                    ref_meta.append({
                        'kind': 'lastframe',
                        'source': 'prev_chunk',
                        'prev_idx': prev_neighbour.get('idx'),
                        'name': f'last frame · prev #{prev_neighbour.get("idx")}',
                        'url': lf_url,
                    })
                    print(f'[seedance_compose] lastframe attached: prev_idx={prev_neighbour.get("idx")} url={lf_url[:60]}', flush=True)
                else:
                    print(f'[seedance_compose] lastframe MISSING for prev_idx={prev_neighbour.get("idx")}: extraction or upload failed silently', flush=True)
                    extra = (
                        f"\n\nДополнительно: @Image{img_idx} — это ПОСЛЕДНИЙ КАДР предыдущего чанка "
                        f"эпизода. Биндить его в BINDING-строке НЕ надо (это композиционный референс, не персонаж/локация). "
                        f"Используй его ТОЛЬКО как continuity-референс: повтори ту же расстановку "
                        f"персонажей и тот же задний план, плавно продолжая действие. НЕ описывай его как "
                        f"отдельный кадр в SUBJECT/SCENE.\n"
                        f"АНТИ-ДУБЛИРОВАНИЕ: персонажи видные на этом lastframe НЕ создают для себя дополнительные "
                        f"char-ref'ы. Если на кадре Maya — это та же Maya из BINDING, не считай её отдельной. "
                        f"В тексте промпта упоминай её только именем 'Maya' (или через её BINDING-@Image в DIALOGUE)."
                    )
                    data['prompt'] = (data.get('prompt') or '').rstrip() + extra
                    lastframe_attached = True
        except Exception as e:
            print(f'[seedance_compose] lastframe attach raised: {type(e).__name__}: {e}', flush=True)

    # Optional: ALSO attach pre-cut keyframes from prev chunk's video.
    # Detects internal cuts (склейки) inside the prev clip and grabs the last
    # frame of EACH outgoing shot. Gives the next chunk visual context for
    # mid-clip mise-en-scène, not just the absolute final frame. Capped at 2
    # extra frames to leave budget for char/loc refs.
    cutframes_attached = 0
    if (use_prev_cutframes and prev_neighbour
            and prev_neighbour.get('video_path')
            and data.get('scene_continuity') is not False
            and len(ref_urls) < 9):
        try:
            video_abs = series_path(sid) / prev_neighbour['video_path']
            # Cache cut timestamps on the chunk so we don't re-run ffmpeg every compose.
            cuts = prev_neighbour.get('cuts_detected')
            if cuts is None:
                cuts = _detect_cuts(str(video_abs))
                prev_neighbour['cuts_detected'] = cuts
                save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
            print(f'[seedance_compose] cutframes: prev_idx={prev_neighbour.get("idx")} detected_cuts={len(cuts or [])} cached_urls={len(prev_neighbour.get("cutframes_avai_urls") or [])}', flush=True)
            if cuts:
                # Cache uploaded urls per-cut on the chunk to avoid re-uploading.
                cf_urls = list(prev_neighbour.get('cutframes_avai_urls') or [])
                budget = 9 - len(ref_urls)
                # Cap restored to 3 (was 1 from 2026-05-19) — user-reported
                # 2026-05-23 «The Landlord's Daughter» ep 1 chunk 2: prev
                # chunk had multiple internal cuts but only 1 cutframe was
                # attached, losing mise-en-scène context for the new chunk.
                #
                # My earlier 1-cap was based on a misapplied WaveSpeed
                # finding: «2-3 refs > 6-9 refs for identity stability» —
                # that's about different ANGLES of the SAME character (more
                # refs = feature averaging). Cutframes serve a different
                # role (scene/blocking continuity, not identity). They don't
                # cause feature averaging the way duplicate character refs do.
                #
                # 9-ref AVAI hard cap is still respected via `budget`.
                max_attach = min(3, budget)
                wanted_cuts = cuts[:max_attach]
                # Need to extract any frames not yet uploaded
                if len(cf_urls) < len(wanted_cuts):
                    extracted = _extract_keyframes_at_cuts(
                        sid, prev_neighbour['video_path'], wanted_cuts, max_frames=max_attach
                    )
                    print(f'[seedance_compose] cutframes: wanted={len(wanted_cuts)} extracted={len(extracted)} starting_uploaded={len(cf_urls)}', flush=True)
                    while len(cf_urls) < len(extracted):
                        relpath, abs_path = extracted[len(cf_urls)]
                        try:
                            cf_urls.append(_avai_upload_local_image(abs_path))
                        except Exception as e:
                            print(f'[seedance_compose] cutframe upload #{len(cf_urls)+1} FAILED: {type(e).__name__}: {e}', flush=True)
                            break
                    print(f'[seedance_compose] cutframes: final_uploaded={len(cf_urls)}', flush=True)
                    prev_neighbour['cutframes_avai_urls'] = cf_urls
                    save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
                # Attach as refs (cap to budget)
                cut_extras = []
                for i, cf_url in enumerate(cf_urls[:max_attach]):
                    if not cf_url or len(ref_urls) >= 9:
                        break
                    img_idx = len(ref_urls) + 1
                    ref_urls.append(cf_url)
                    ref_meta.append({
                        'kind': 'cutframe',
                        'source': 'prev_chunk',
                        'prev_idx': prev_neighbour.get('idx'),
                        'cut_index': i,
                        'cut_time': float(wanted_cuts[i]) if i < len(wanted_cuts) else None,
                        'name': f'pre-cut #{i+1} · prev #{prev_neighbour.get("idx")}',
                        'url': cf_url,
                    })
                    cut_extras.append(f"@Image{img_idx} (кадр перед {i+1}-й склейкой прошлого чанка)")
                    cutframes_attached += 1
                if cut_extras:
                    extra = (
                        "\n\nЕщё continuity-референсы: " + ", ".join(cut_extras) + ". "
                        "Это последние кадры разных шотов внутри прошлого чанка — каждый показывает "
                        "состав в кадре и расстановку до соответствующей склейки. Биндить их в BINDING-строке "
                        "НЕ надо. Используй их вместе с last frame для понимания мизансцены. "
                        "В SUBJECT/SCENE их КАК отдельные кадры НЕ описывай.\n"
                        "АНТИ-ДУБЛИРОВАНИЕ: персонажи на этих cutframe'ах НЕ создают новых char-ref'ов. "
                        "Если Maya видна на cutframe — это та же Maya из BINDING, не отдельная инстанция. "
                        "В тексте промпта ссылайся на неё именем."
                    )
                    data['prompt'] = (data.get('prompt') or '').rstrip() + extra
        except Exception:
            pass

    # ── State analysis: tell Claude WHAT'S in each attached continuity frame ──
    # If we attached lastframe and/or cutframes, run a single Haiku Vision call
    # to label what each frame ACTUALLY shows (who's visible, in what state,
    # mise-en-scène). Inject into the prompt so the composer knows e.g. "Maya
    # has a busted lip and wet hair RIGHT NOW" — base portrait won't tell it that.
    state_analysis_attached = False
    state_frames = [r for r in ref_meta if r.get('kind') in ('lastframe', 'cutframe')]
    if state_frames and prev_neighbour:
        # v7 = clip truncation now respects word boundaries («out fro...»
        # → «out from beneath...» or trimmed cleanly at the last space).
        # Bump invalidates v6 blocks with ugly mid-word ellipsis.
        cache_key = 'v7|' + '|'.join(r.get('url', '') for r in state_frames)
        # Always resolve prev_chars — needed by override pass even on cache hit.
        prev_char_ids = [r['id'] for r in (prev_neighbour.get('refs') or []) if r.get('kind') == 'char']
        prev_chars = [c for c in (s.get('characters') or []) if c['id'] in prev_char_ids]
        cached = prev_neighbour.get('frame_state_analysis') or {}
        analysis_text = ''
        if cached.get('cache_key') == cache_key and cached.get('text'):
            analysis_text = cached['text']
            # Re-apply override on cache hit too — script changes between
            # composes (user edits chunk_text) without invalidating Vision
            # cache should still flow into the analysis. Cheap regex pass.
            analysis_text = _override_vision_with_script_poses(
                analysis_text,
                prev_neighbour.get('chunk_text') or '',
                prev_chars,
            )
        else:
            roster_lines = '\n'.join(
                f'  - {c["name"]}: {c.get("appearance","")[:140]}'
                for c in prev_chars
            ) or '  (нет данных о составе прошлого чанка)'
            frame_labels = []
            for i, r in enumerate(state_frames):
                if r.get('kind') == 'lastframe':
                    frame_labels.append(f'Кадр {i+1}: lastframe (последний кадр прошлого чанка)')
                else:
                    frame_labels.append(f'Кадр {i+1}: cutframe #{r.get("cut_index", i)+1} (перед склейкой внутри прошлого чанка)')
            v_prompt = (
                "Опиши ТЕКУЩЕЕ ФИЗИЧЕСКОЕ СОСТОЯНИЕ персонажей на присоединённых кадрах. "
                "Это последние кадры предыдущего сгенерированного видео-чанка, используются как continuity-контекст для генерации СЛЕДУЮЩЕГО чанка.\n\n"
                f"ПЕРСОНАЖИ ИЗ ПРОШЛОГО ЧАНКА (roster — кого ожидать в кадрах):\n{roster_lines}\n\n"
                f"КАДРЫ В ТОМ ЖЕ ПОРЯДКЕ ЧТО ПРИКРЕПЛЕНЫ:\n" + '\n'.join(frame_labels) + "\n\n"
                "Для КАЖДОГО кадра по порядку:\n"
                "1) Видимые персонажи (имя из roster + краткое 'кто это', если roster короткий — просто имя).\n"
                "2) Для каждого СТРОГО по этим полям:\n"
                "   • Поза: ровно одно из — стоит / сидит / на коленях / лежит / приседает / опирается / наклоняется.\n"
                "   • Blocking (КРИТИЧНО для continuity): сторона кадра + куда обращён.\n"
                "     Формат: '<сторона кадра>, обращён <куда>'. Стороны: левая часть / правая часть / центр / "
                "     передний план / задний план. Обращённость: лицом к [имя другого перса] / лицом к камере / "
                "     спиной к камере / профилем влево / профилем вправо / спиной к [имя] / лицом в окно.\n"
                "     Пример: 'левая часть, обращён лицом к Ethan (который справа)' или 'правая часть, профилем влево, "
                "     лицом к Maya'. БЕЗ этого поля следующий чанк теряет геометрию и персонажи разворачиваются.\n"
                "   • Где именно в комнате: у двери / у окна / в центре / в углу / за столом / перед камином / "
                "     рядом с [имя другого перса] / на заднем плане.\n"
                "   • Что в руках: телефон / бокал / документ / нож / чашка / ничего. Если в одной руке одно — пиши какой.\n"
                "   • Физический контакт с другими: держит [имя] за руку / обнимает [имя] / нависает над [имя] / "
                "     никакого контакта.\n"
                "   • Выражение лица: страх / гнев / слёзы / шок / нейтральное / улыбка.\n"
                "   • Видимые повреждения: синяки, кровь, царапины, мокрые волосы, разорванная одежда (или 'нет').\n"
                "3) Общая мизансцена кадра (где, освещение, ключевые объекты на фоне).\n\n"
                "ПРАВИЛА:\n"
                "- Описывай ТОЛЬКО то что РЕАЛЬНО видно на кадре. НЕ додумывай и НЕ фантазируй про повреждения которых нет.\n"
                "- Если перс из roster на кадре не виден — НЕ упоминай его.\n"
                "- На каждого перса = ОДНА строка вида «Имя: поза | где | в руках | контакт | эмоция | повреждения».\n"
                "- ПО-РУССКИ.\n\n"
                "Формат строго (поля через | в одной строке на персонажа):\n"
                "Кадр 1 (lastframe):\n"
                "  • <Имя>: поза=стоит | blocking=правая часть кадра, обращён лицом к <Имя2> (слева) | где=у книжного шкафа справа | в руках=бокал виски | контакт=нет | эмоция=напряжён | повреждения=нет\n"
                "  • <Имя2>: поза=сидит | blocking=левая часть кадра, профилем вправо, лицом к <Имя> | где=за столом слева | в руках=ничего, ладони на документах | контакт=нет | эмоция=шок | повреждения=нет\n"
                "  • Сцена: <мизансцена одной строкой — локация, свет, ключевые объекты фона>\n"
                "Кадр 2 (cutframe #1):\n"
                "  • ..."
            )
            try:
                urls_only = [r.get('url') for r in state_frames if r.get('url')]
                analysis_text = claude_ask_vision(v_prompt, urls_only).strip()
                if analysis_text:
                    # ── Script-pose override (Vision misread guard) ────────────
                    # Real production bug: «The Fox CEO's Trap» ep 1 — chunk 0's
                    # script said «Fox Woman lies halfway under the car». Pixar
                    # render botched the pose and drew her standing. Vision then
                    # honestly transcribed the broken render as «поза=стоит»
                    # for the POSTURE LOCK fed to chunk 1. Chunk 1 inherited
                    # «стоит» and re-rendered her standing — propagating the
                    # error. We pull pose verbs directly from the prev chunk's
                    # script text and overwrite the Vision line when they
                    # disagree. Script = ground truth, Vision = best-guess.
                    analysis_text = _override_vision_with_script_poses(
                        analysis_text,
                        prev_neighbour.get('chunk_text') or '',
                        prev_chars,
                    )
                    prev_neighbour['frame_state_analysis'] = {
                        'cache_key': cache_key,
                        'text': analysis_text,
                    }
                    save_episode(sid, prev_neighbour_ep, prev_neighbour_obj)
            except Exception as e:
                print(f'[seedance_compose] vision analysis failed: {e}', flush=True)
                analysis_text = ''

        if analysis_text:
            state_extra = (
                "\n\nТЕКУЩЕЕ СОСТОЯНИЕ ПЕРСОНАЖЕЙ И СЦЕНЫ (из присоединённых continuity-кадров):\n"
                f"{analysis_text}\n\n"
                "ВАЖНО: базовые портреты персонажей (@Image1, @Image2...) показывают КАНОНИЧЕСКИЙ ВИД персонажа "
                "ДО событий сцены. ТЕКУЩЕЕ СОСТОЯНИЕ — то что описано выше из continuity-кадров. "
                "В SUBJECT/ACTION текущего промпта ОБЯЗАТЕЛЬНО отрази это состояние явными словами "
                "(например: 'Maya, на губе кровь из разбитой губы, мокрые волосы, разорванная блузка, дрожит'). "
                "НЕ описывай персонажей как «свежих» / в базовом виде — они продолжаются из прошлого кадра.\n\n"
                "POSTURE/STATE LOCK ИЗ ЭТОГО АНАЛИЗА:\n"
                "Состояние выше — это твой ENDING STATE предыдущего чанка. По умолчанию переноси его в начало "
                "текущего чанка (поза стоя/сидя, где стоит, что в руках, физический контакт).\n"
                "ВАЖНОЕ ИСКЛЮЧЕНИЕ — СЦЕНАРИЙ ВЫШЕ POSTURE LOCK'а: если CHUNK TEXT этого чанка ИЛИ предыдущего "
                "явно описывает позу/положение которое ПРОТИВОРЕЧИТ анализу (например chunk_text: «Fox Woman lies "
                "halfway under the car», а Vision-анализ говорит «Fox Woman стоит») — ВЕРЬ СЦЕНАРИЮ, не анализу. "
                "Vision-анализ может ошибаться когда предыдущий рендер не справился со сложной позой "
                "(persona под машиной, на коленях, в нестандартной позе) и нарисовал её в дефолтной стоячей позе. "
                "Сценарий — ground truth, анализ — лучшая догадка по картинке. При конфликте: "
                "  • В SUBJECT/ACTION пиши позу ИЗ СЦЕНАРИЯ («Fox Woman продолжает лежать наполовину под машиной, "
                "    голова и плечи торчат наружу»).\n"
                "  • НЕ повторяй неправильную позу из анализа в первой строке ACTION.\n"
                "Если в CHUNK явный глагол смены позы (садится, встаёт, выходит) — это нормальный переход, "
                "выполняй сценарий.\n"
                "ОБЯЗАТЕЛЬНАЯ первая строка ACTION (с blocking из анализа — это ЕДИНСТВЕННЫЙ способ сохранить "
                "пространственную геометрию между чанками, Seedance не выводит её из lastframe-картинки сам): "
                "«Продолжая с прошлого чанка: [имя1] [сторона кадра, обращён лицом к/спиной/профилем] [поза] "
                "[где] [с чем]; [имя2] [сторона кадра, обращён лицом к/спиной/профилем] [поза] [где] [с чем]». "
                "Если в текущем чанке смена ракурса (например cut to reverse shot) — отрази что [имя1] теперь "
                "видим с другой стороны, но направление взгляда персонажа в пространстве сцены сохраняется."
            )
            data['prompt'] = (data.get('prompt') or '').rstrip() + state_extra
            state_analysis_attached = True

    # ── POSE LOCK FALLBACK CHAIN (Track B2) ─────────────────────────────────
    # If Vision didn't run (no continuity frames attached) OR Vision returned
    # empty (network blip / content-filter), we still need SOMETHING anchoring
    # the next chunk's poses to the previous chunk. Fall back to:
    #   (2) prev_neighbour.ending_state (coarse one-liner written by Haiku
    #       after the prev chunk completed)
    #   (3) regex-extract pose verbs from prev_neighbour.chunk_text
    # If even those produce nothing — leave as before (no block); model
    # falls back to base portraits. The compose response includes
    # `pose_lock_fallback` so logs/UI can see what level fired.
    pose_lock_fallback = 'vision' if state_analysis_attached else None
    if not state_analysis_attached and prev_neighbour:
        fallback_block = ''
        ending_state = (prev_neighbour.get('ending_state') or '').strip()
        if ending_state:
            fallback_block = (
                "\n\nПОЗИЦИИ ИЗ ПРОШЛОГО ЧАНКА (упрощённое продолжение — Vision-анализ continuity-кадров не "
                "доступен; используем ending_state):\n"
                f"  {ending_state}\n\n"
                "Это финальная мизансцена предыдущего чанка. Сохрани эти позы и расстановку в начале текущего "
                "чанка, пока в CHUNK'е нет ЯВНОГО ГЛАГОЛА смены позы (садится, встаёт, выходит). "
                "Первая строка ACTION должна явно продолжать эту расстановку."
            )
            pose_lock_fallback = 'ending_state'
        else:
            prev_text = (prev_neighbour.get('chunk_text') or '')
            if prev_text:
                # Regex-извлечение поз: имя_персонажа + pose-verb в одной фразе.
                # Имена ловим жадно (CamelCase / ALL CAPS / просто заглавная), затем
                # ищем pose-verb в пределах ~30 символов.
                pose_re = re.compile(
                    r'\b([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё\-]{1,30})\b[^.,;\n]{0,30}\b'
                    r'(?:сидит|сидят|стоит|стоят|лежит|лежат|на коленях|опирается|опираются|'
                    r'наклоняется|наклоняются|обнимает|обнимают|держит за|приседает|приседают|'
                    r'sits|stands|kneels|leans|holds|hugs|kneeling|sitting|standing|leaning)\b',
                    re.IGNORECASE,
                )
                hits = pose_re.findall(prev_text)
                if hits:
                    uniq = []
                    for h in hits:
                        if h not in uniq:
                            uniq.append(h)
                        if len(uniq) >= 3:
                            break
                    fallback_block = (
                        f"\n\nПОЗИЦИИ ИЗ ПРОШЛОГО ЧАНКА (regex-extract — нет ни Vision-анализа, "
                        f"ни ending_state):\n"
                        f"  В прошлом чанке упоминались позы у: {', '.join(uniq)}\n\n"
                        f"Если эти персонажи присутствуют в текущем CHUNK'е — сохрани их позу/действие "
                        f"из предыдущего чанка, пока нет ЯВНОГО ГЛАГОЛА смены."
                    )
                    pose_lock_fallback = 'chunk_text_regex'
        if fallback_block:
            data['prompt'] = (data.get('prompt') or '').rstrip() + fallback_block
            _log_event('INFO', 'pose_lock_fallback', sid=sid, ep_num=num,
                       fallback_level=pose_lock_fallback,
                       prev_idx=prev_neighbour.get('idx'))

    # ── FRAMING FORCED-INJECTION (Track A1) ─────────────────────────────────
    # If composer returned a structured framing value AND there are 2+ char
    # refs, append a deterministic per-framing composition block. This is the
    # main defence against the «второй размыто сзади» artifact — instead of
    # hoping Claude's prose contains the right spatial cue, the server writes
    # the exact phrasing Seedance responds to.
    #
    # Back-compat: if `framing` is missing/unknown, OR there's <2 chars in
    # refs, we don't inject anything → existing single-shot flows are unchanged.
    framing = (data.get('framing') or '').strip().lower()
    framing_anchor = (data.get('framing_anchor') or '').strip()
    char_refs_ordered = [r for r in (data.get('refs') or []) if r.get('kind') == 'char']
    char_ref_count = len(char_refs_ordered)
    # Resolve character ref names + their @ImageN slot (1-based, matches the
    # order in `refs[]`). Used to tie OTS shoulder to a SPECIFIC existing
    # character — without this Seedance invents a third figure for the
    # foreground shoulder («over X's shoulder» → model generates someone
    # whose shoulder THIS is, even when X is one of the existing refs).
    char_names_by_slot = {}     # 1-based slot index → canonical name (IN-FRAME chars)
    voice_only_by_slot = {}     # 1-based slot index → name (off-screen voice chars)
    # Detect characters whose `appearance` field describes them as voice-only
    # (someone on the other end of a phone call, voiceover, off-screen). Real
    # prod bug 2026-05-19 «My Stepmother» ep 43 — Detective Morris has
    # appearance="Voice on phone delivering urgent summons..." (script-writer
    # LLM put role description into the appearance field instead of physical
    # description). Composer dutifully placed him IN frame with the room cast,
    # contradicting the phone-call narrative. Detection: appearance starts
    # with "voice", "off-screen", "voiceover", "phone voice", "via phone".
    _VOICE_ONLY_RE = re.compile(
        r'^\s*(?:voice\s+(?:on|via|through|over)\s+phone|voice-?on-?phone'
        r'|off[\s\-]?screen\s+voice|voiceover|voice[\s\-]?only|via\s+phone'
        r'|on\s+the\s+phone\s+(?:from|in)|phone\s+voice|голос\s+по\s+телефону'
        r'|голос\s+за\s+кадром|закадровый\s+голос)\b',
        re.IGNORECASE,
    )
    for i, r in enumerate(char_refs_ordered):
        ch = next((c for c in (s.get('characters') or []) if c.get('id') == r.get('id')), None)
        if not (ch and ch.get('name')):
            continue
        slot = i + 1
        app = (ch.get('appearance') or '').strip()
        if _VOICE_ONLY_RE.match(app):
            voice_only_by_slot[slot] = ch['name']
        else:
            char_names_by_slot[slot] = ch['name']
    cast_list = ', '.join(f'@Image{idx}={nm}' for idx, nm in char_names_by_slot.items())
    voice_list = ', '.join(f'@Image{idx}={nm}' for idx, nm in voice_only_by_slot.items())
    # Soft-restrict: ban INVENTED ref-look-alikes (anti-phantom-3rd-character
    # pattern) but allow legitimate background massovka in public spaces like
    # courtrooms/restaurants/streets. User feedback 2026-05-18: jeлзкое
    # «ровно эти персонажи, никаких других» давало пустые залы суда.
    parts = []
    if char_names_by_slot:
        parts.append(
            f" Named характеры в кадре — только эти: {cast_list}. "
            "Если рамка кадра требует чьё-то плечо/затылок/руку на переднем плане (OTS shot) — "
            "это часть тела ОДНОГО ИЗ УЖЕ ЗАЯВЛЕННЫХ персонажей выше (того кого ты указал в "
            "`framing_anchor`), НЕ новой выдуманной фигуры. Силуэты на дальнем фоне в публичных "
            "местах (залы суда, рестораны, улицы) допустимы как extras с РАЗМЫТЫМИ лицами — "
            "они НЕ должны иметь черты ни одного из ref-персонажей."
        )
    if voice_only_by_slot:
        parts.append(
            f" VOICE-ONLY off-screen (НЕ В КАДРЕ, только голос): {voice_list}. "
            "Эти персонажи находятся НА ДРУГОМ КОНЦЕ телефонного звонка / интеркома / рации. "
            "Их голос слышен в кадре (через динамик телефона на громкой связи, либо через "
            "трубку у уха другого персонажа), но САМИ ОНИ В КАДРЕ НЕ ПОЯВЛЯЮТСЯ. "
            "НЕ помещай их в комнату с другими персонажами. НЕ рисуй их фигуру. "
            "В DIALOGUE-блоках их реплики оформляй как (V.O.) / voiceover from phone speaker / "
            "off-screen voice through phone — но НЕ как visible character speaking. "
            "Сами @ImageN рефы этих персонажей нужны Seedance для голосовой консистентности (lipsync "
            "поверх audio), но визуально на frame они отсутствуют."
        )
    cast_restriction = ''.join(parts)
    # Find @ImageN slot for the OTS anchor (matched by canonical name, case-insensitive).
    anchor_slot = None
    if framing_anchor:
        for idx, nm in char_names_by_slot.items():
            if nm.strip().lower() == framing_anchor.strip().lower():
                anchor_slot = idx
                break
    _FRAMING_BLOCKS = {
        'ots': (
            "КОМПОЗИЦИЯ КАДРА (FORCED OTS): Камера расположена за правым плечом {anchor} "
            "(@Image{anchor_slot} в refs выше). Плечо и затылок в нижнем-левом углу кадра "
            "— это ТОТ ЖЕ САМЫЙ персонаж @Image{anchor_slot}: те же волосы, та же одежда, "
            "тот же силуэт что на его reference-фотографии. "
            "НЕ создавай нового персонажа для этого плеча. По центру кадра в фокусе — лицо "
            "другого спикера на среднем крупном плане, смотрит мимо камеры в сторону @Image{anchor_slot}. "
            "@Image{anchor_slot} ЕСТЬ в кадре как foreground-силуэт, НЕ как фон. "
            "НИ ОДИН из персонажей НЕ размыт силуэтом сзади лицом в камеру."
        ),
        'two_shot': (
            "КОМПОЗИЦИЯ КАДРА (FORCED TWO-SHOT): Оба персонажа в кадре на среднем плане, "
            "плечом к плечу или под лёгким углом друг к другу, лица на одной линии резкости. "
            "Равная резкость обоих. Ни один не размыт в фоне."
        ),
        'profile_two': (
            "КОМПОЗИЦИЯ КАДРА (FORCED PROFILE-TWO): Оба персонажа профилями к камере "
            "в тесном пространстве (кабина машины / за столом / у стойки). "
            "Оба видны как полноценные фигуры, равная резкость. Ни один не в расфокусе."
        ),
        'tracking_shot': (
            "КОМПОЗИЦИЯ КАДРА (FORCED TRACKING): Камера движется параллельно героям, "
            "оба идут бок-о-бок в стрид-кадре, равная резкость обоих. "
            "НЕ ставь одного спереди резкого + второго позади размытого — оба на одной линии."
        ),
        'action_wide': (
            "КОМПОЗИЦИЯ КАДРА (FORCED ACTION-WIDE): Все участники действия полностью в кадре "
            "(головы и ноги видны), физическое взаимодействие чётко прочитывается. "
            "Никаких размытых силуэтов на фоне — все фигуры с понятной позой."
        ),
    }
    if framing in _FRAMING_BLOCKS and char_ref_count >= 2:
        block = _FRAMING_BLOCKS[framing]
        if framing == 'ots':
            if anchor_slot:
                # We resolved anchor → @ImageN slot. Tie the OTS shoulder
                # specifically to that ref so Seedance can't invent a third
                # person to be the shoulder-foreground. User-reported bug:
                # «Pregnant by the Man Who Hates Me» ep 1 — 2 characters in
                # refs, but the first two frames were OTS of a phantom «girl
                # in black» the model invented for the shoulder slot.
                block = block.format(anchor=framing_anchor, anchor_slot=anchor_slot)
            else:
                # OTS anchor name didn't match any char ref (composer mistake
                # or stale name). Fall back to two_shot phrasing — safer than
                # an unanchored OTS that will invite invented bystanders.
                block = _FRAMING_BLOCKS['two_shot']
                framing = 'two_shot'
        # Append cast restriction to ALL multi-char framings so no framing
        # can implicitly justify a phantom third person.
        data['prompt'] = (data.get('prompt') or '').rstrip() + "\n\n" + block + cast_restriction

    # ── B4: POSTURE/STATE LOCK ECHO at the very end of prompt ────────────────
    # When pose-lock is active (Vision OR fallback), append a compact one-line
    # caps reminder as the LAST thing in the prompt. Banana/Seedance hold tail-
    # of-prompt instructions stronger than mid-prompt — the upfront block sets
    # context, this echo reinforces «don't change pose» right before generation.
    if pose_lock_fallback in ('vision', 'ending_state', 'chunk_text_regex'):
        data['prompt'] = (data.get('prompt') or '').rstrip() + (
            "\n\nКРИТИЧНО: позы из CHUNK TEXT (сценарий выше) — ground truth. Если сценарий говорит "
            "«Fox Woman lies under the car», а POSTURE LOCK анализ говорит «стоит» — рендери "
            "позу ИЗ СЦЕНАРИЯ. Анализ полезен только когда сценарий не уточняет позу. "
            "Не меняй позы из головы — следуй сценарию + POSTURE LOCK как fallback. "
            "Continuity > свобода интерпретации."
        )

    # ── ANTI-BACKGROUND SCRUB (Track A3) ────────────────────────────────────
    # Safety net: even with A1/A4 rules, Claude occasionally slips a phrase
    # like «Anna stands in the background, blurred». Such a phrase, even one
    # sentence, primes Seedance to place the character exactly that way.
    # Pattern-strip the known bad formulations; log every hit for analysis.
    scrub_hits = []
    ref_char_names = []
    for r in (data.get('refs') or []):
        if r.get('kind') == 'char':
            ch = next((c for c in (s.get('characters') or []) if c.get('id') == r.get('id')), None)
            if ch and ch.get('name'):
                ref_char_names.append(re.escape(ch['name']))
    if ref_char_names and (data.get('prompt') or ''):
        name_re = re.compile(r'\b(?:' + '|'.join(ref_char_names) + r')\b')
        # Detect sentences that COMBINE a background-marker AND a blur-marker
        # AND contain a character name from refs. Sentence-level granularity
        # is safer than fragment regex — we keep nearby clean sentences intact.
        bg_markers = re.compile(
            r'(?:in (?:the )?(?:deep )?background\b'
            r'|far behind\b'
            r'|silhouette\b'
            r'|silhouetted\b'
            r'|на заднем плане\b'
            r'|вдалеке\b'
            r'|в глубине кадра\b'
            r'|сзади в кадре\b'
            r'|силуэт\w*\b)',
            re.IGNORECASE,
        )
        blur_markers = re.compile(
            r'(?:\bblurred\b'
            r'|\bout of focus\b'
            r'|\bsoft focus\b'
            r'|\bdefocus\w*\b'
            r'|\bобрезает\b'
            r'|\bразмыт\w*\b'
            r'|\bрасфокус\w*\b'
            r'|\bнерезк\w*\b)',
            re.IGNORECASE,
        )
        # Split prompt into sentences-ish (`.`, `!`, `?`, `;`, line breaks)
        sentences = re.split(r'(?<=[\.\!\?;])\s+|\n+', data.get('prompt') or '')
        kept = []
        for s_text in sentences:
            has_bg = bool(bg_markers.search(s_text))
            has_blur = bool(blur_markers.search(s_text))
            has_char = bool(name_re.search(s_text))
            # Special case allow-list: forced OTS block contains "blurred earpiece"
            # which is desired phrasing. Skip if sentence has "back-of-head" or
            # "over <name>'s shoulder" or "earpiece" — those are valid OTS cues.
            is_valid_ots = bool(re.search(
                r'\b(?:back-of-head|earpiece|over [A-ZА-ЯЁ][^\s]*\'?s? shoulder|за плечом|плечо\s+\S+\s+как foreground)',
                s_text, re.IGNORECASE,
            ))
            if has_bg and has_blur and has_char and not is_valid_ots:
                scrub_hits.append(s_text.strip())
                continue   # drop this sentence
            kept.append(s_text)
        if scrub_hits:
            new_prompt = ' '.join(kept)
            new_prompt = re.sub(r'\s+([\.,;\!\?])', r'\1', new_prompt)
            new_prompt = re.sub(r'\s{2,}', ' ', new_prompt).strip()
            data['prompt'] = new_prompt
            _log_event('INFO', 'anti_background_scrub', sid=sid, ep_num=num,
                       hits=scrub_hits[:5], hit_count=len(scrub_hits))

    debug_prev = None
    if prev_neighbour:
        prev_end_pos = next(
            (ep_pos for sp, ep_pos, c in located if c is prev_neighbour),
            -1,
        )
        prev_scene_anchor = _last_scene_heading_before(prev_end_pos) if prev_end_pos >= 0 else -1
        debug_prev = {
            'idx': prev_neighbour.get('idx'),
            'episode': prev_neighbour_ep,
            'has_video': bool(prev_neighbour.get('video_path')),
            'pos_found': cur_pos >= 0,
            'same_scene': (cur_scene_anchor == prev_scene_anchor) if cur_scene_anchor >= 0 else None,
            'status': prev_neighbour.get('status'),
        }

    # ── B5: COMPOSE SANITY WARNINGS ─────────────────────────────────────────
    # Defence-in-depth: surface internal inconsistencies as `compose_warnings`
    # to the UI, so when the user reports «chunk is bad» we can immediately
    # see which guard failed.
    compose_warnings = []
    # 1) prev_neighbour exists + scene_continuity claims true, but no pose lock
    #    of any kind landed in the prompt → next chunk WILL drift in pose.
    if (prev_neighbour and data.get('scene_continuity') is not False
            and not pose_lock_fallback):
        compose_warnings.append({
            'kind': 'no_pose_lock_with_prev',
            'detail': 'prev_neighbour exists and continuity is on, but neither Vision-analysis '
                      'nor ending_state nor chunk_text regex produced a pose-lock block. '
                      'Pose drift likely.',
        })
    # 0a) voice-only chars dropped (always-on filter)
    if dropped_voice_only:
        compose_warnings.append({
            'kind': 'voice_only_chars_dropped',
            'detail': (
                f'Voice-only character(s) dropped from refs because their name'
                f' is not in chunk_text: {", ".join(c["name"] for c in dropped_voice_only)}.'
            ),
            'dropped_chars': dropped_voice_only,
        })
    # 0b) strict-filter chars dropped (experimental — STRICT_CHAR_FILTER)
    if dropped_strict:
        compose_warnings.append({
            'kind': 'strict_filter_chars_dropped',
            'detail': (
                f'STRICT_CHAR_FILTER (экспериментальный) удалил из refs персонажей '
                f'которых composer положил, но их имя не упомянуто в chunk_text: '
                f'{", ".join(c["name"] for c in dropped_strict)}. '
                f'Если они должны быть в кадре физически (например «her hand visible»), '
                f'отключи фильтр: env STRICT_CHAR_FILTER=0.'
            ),
            'dropped_chars': dropped_strict,
        })
    if kept_continuity:
        compose_warnings.append({
            'kind': 'strict_filter_continuity_kept',
            'detail': (
                'STRICT_CHAR_FILTER оставил персонажей, которых нет по имени в '
                'chunk_text, но они были в кадре предыдущего чанка (continuity): '
                f'{", ".join(c["name"] for c in kept_continuity)}.'
            ),
            'kept_chars': kept_continuity,
        })
    # 2) prev_neighbour from a different scene than current chunk.
    if debug_prev and debug_prev.get('same_scene') is False:
        compose_warnings.append({
            'kind': 'prev_cross_scene',
            'detail': f"prev_neighbour idx={debug_prev.get('idx')} is from a different scene "
                      f"than the current chunk — pose continuity will not transfer.",
        })
    # 3) framing missing but composer returned 2+ char refs.
    if char_ref_count >= 2 and not framing:
        compose_warnings.append({
            'kind': 'multi_char_no_framing',
            'detail': f"{char_ref_count} character refs but composer did not return a `framing` "
                      "field. Server-side OTS/two-shot injection skipped.",
        })
    # 4) Named character VISIBLE in chunk_text but NOT in refs[]: AUTO-ADD.
    #    Composer-LLM repeatedly forgets to include characters doing visible
    #    actions even with explicit sysprompt rules (real prod: «Vice Beasts»
    #    ep 1 chunks 6, 8, 9 — Leo dropped despite «Leo disappears in another
    #    direction»). Switched from warn-only to auto-fix on 2026-05-20.
    #
    #    Heuristic: character is VISIBLE if their name appears in chunk_text
    #    OUTSIDE dialogue lines (i.e. in action/scene description). Mention
    #    inside a quoted dialogue («Rex: "Leo?"») does NOT count — that's
    #    just calling the name. Mention as a speaker cue («LEO:» on a line
    #    by itself before a dialogue) DOES count — that means Leo speaks,
    #    which means he's somewhere (visible or voice-over; either way he
    #    needs his lipsync ref).
    chunk_text_for_check = body.get('chunk_text') or ''
    auto_added_chars = []
    if chunk_text_for_check and s.get('characters'):
        ref_char_ids = {r.get('id') for r in (data.get('refs') or [])
                        if r.get('kind') == 'char'}
        active_char_ids = set(ep.get('characters_used') or [])
        candidates = [
            c for c in (s.get('characters') or [])
            if c.get('id') in active_char_ids and c.get('id') not in ref_char_ids
        ]
        # Strip dialogue content from chunk_text — we only check names
        # appearing in action/scene description. Two flavours of dialogue:
        #   (a) Quoted: `Adrian says: "Leo?"` → strip the quoted part
        #   (b) Screenplay format: `EMMA: Vivian.` (name + colon + unquoted
        #       dialogue text) → strip the whole line. Real prod bug
        #       2026-05-20 «My Stepmother» ep 52: chunk_text had
        #       `EMMA: Vivian.` — Emma SPEAKS Vivian's name. Old strip
        #       only handled quotes, so «Vivian» landed in action_only and
        #       got auto-added to refs incorrectly.
        action_only = re.sub(r'[\"«„][^"»“\n]{1,500}[\"»“]', '', chunk_text_for_check)
        # Screenplay-format dialogue: lines starting with NAME (1-3 words,
        # leading letter) + colon. Strip the entire line including content.
        action_only = re.sub(
            r'^[A-Za-zА-Яа-яЁё][^:\n]{0,40}:.*$',
            '', action_only, flags=re.MULTILINE,
        )
        for c in candidates:
            name = (c.get('name') or '').strip()
            if not name:
                continue
            # Use _char_name_in_text for punctuation-safe matching (handles
            # «Mrs.», «Dr.», «Officer» etc that the old simple regex missed).
            if _char_name_in_text(name, action_only, c.get('aliases')):
                auto_added_chars.append({'name': name, 'id': c.get('id')})
        # Auto-add to refs[] + ref_urls + ref_meta. Skip voice-only chars
        # (those handled separately by the voice-only marker, must not appear
        # in frame). Resolve URL inline so the returned compose response is
        # complete and the next /seedance/start has them.
        if auto_added_chars:
            data['refs'] = data.get('refs') or []
            actually_added = []
            for added in auto_added_chars:
                # Skip voice-only chars — they're handled by the voice-only
                # mechanism and must NOT be added as visible refs.
                ch = next((c for c in (s.get('characters') or []) if c['id'] == added['id']), None)
                app_field = (ch.get('appearance') or '').strip() if ch else ''
                if _VOICE_ONLY_RE.match(app_field):
                    continue
                ref_entry = {
                    'kind': 'char',
                    'id': added['id'],
                    'outfit': None,
                    '_auto_added': True,
                }
                url = _resolve_ref_url(s, ref_entry, sid=sid)
                if not url:
                    continue  # no avai_base_url available, can't add
                data['refs'].append(ref_entry)
                # Append to ref_urls + ref_meta so the returned response is
                # complete. New slot index is len(ref_urls)+1 (1-based).
                slot_idx = len(ref_urls) + 1
                ref_urls.append(url)
                clean = {k: v for k, v in ref_entry.items() if not k.startswith('_')}
                ref_meta.append({**clean, 'url': url, '_auto_added': True})
                actually_added.append({**added, 'slot': slot_idx})
            if actually_added:
                # Tell Seedance about the auto-added characters — server-side
                # appended note. Without this the prompt text body still won't
                # mention them, so even with the image in refs Seedance might
                # not visualize. The note specifically says «include them in
                # visual per chunk_text actions».
                names_str = ', '.join(
                    f'{c["name"]} (@Image{c["slot"]})' for c in actually_added
                )
                data['prompt'] = (data.get('prompt') or '').rstrip() + (
                    f"\n\nДОБАВЛЕННЫЕ СЕРВЕРОМ ПЕРСОНАЖИ: {names_str} — composer "
                    f"пропустил их, но они УПОМЯНУТЫ в chunk_text сценарии вне диалога "
                    f"(совершают visible action). Включи их в визуал согласно сценарию. "
                    f"Если в действии написано «{actually_added[0]['name']} disappears in another direction» — "
                    f"покажи это движение, не игнорируй персонажа."
                )
                compose_warnings.append({
                    'kind': 'named_char_auto_added_to_refs',
                    'detail': (
                        f'Сервер автоматически добавил в refs персонажей которых composer пропустил, '
                        f'хотя они упомянуты в chunk_text вне диалога: '
                        f'{", ".join(c["name"] for c in actually_added)}. '
                        f'Если это были voice-only/off-screen — удали их вручную.'
                    ),
                    'auto_added': actually_added,
                })
    # 6) Wardrobe-state mismatch: the script stages a character undressed
    #    (towel / post-shower / robe / sleepwear / shirtless) but this chunk
    #    assigns a fully-dressed outfit (or base, which is dressed). The composer
    #    can only pick from defined outfits, so without a dedicated state outfit
    #    it defaults to e.g. the business suit → the render flips between chunks
    #    (Damien towel→suit, ep3). The BINDING builder above now AUTO-OVERRIDES the
    #    clothing for this chunk so the render holds the undress state; we still
    #    surface the cause so the user creates a dedicated outfit (with a real
    #    reference photo) for best fidelity. `_undressed` was computed above.
    for r in ref_meta:
        if r.get('kind') != 'char':
            continue
        cue = _undressed.get(r.get('id'))
        if not cue:
            continue
        label = r.get('outfit') or ''
        ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
        odesc = ''
        if ch and label:
            o = next((o for o in (ch.get('outfits') or []) if o.get('label') == label), None)
            odesc = (o.get('description') or '') if o else ''
        haystack = f'{label} {odesc}'
        if _UNDRESSED_OUTFIT_RE.search(haystack):
            continue   # outfit already matches the undressed state — no conflict
        if label and not _DRESSED_OUTFIT_RE.search(haystack):
            continue   # neutral label, don't assume a conflict
        _was_overridden = r.get('id') in _undress_overridden
        if _was_overridden:
            detail = (
                f'{ch["name"] if ch else r.get("id")} по сценарию в раздетом/переходном состоянии '
                f'(«{cue}»), а назначенный образ "{label or "base"}" этому противоречил — '
                f'BINDING для этого чанка АВТО-ПЕРЕОПРЕДЕЛЁН на «{_undress_state_clothing(cue)}», '
                f'чтобы гардероб держался по всей сцене. Для максимальной точности заведи отдельный '
                f'образ (напр. "Post-Shower"/"Towel") с настоящим референс-фото и назначь на ВСЕ чанки сцены.'
            )
        else:
            detail = (
                f'{ch["name"] if ch else r.get("id")} по сценарию в раздетом/переходном состоянии '
                f'(«{cue}»), но в этом чанке назначен образ "{label or "base"}", который этому '
                f'противоречит. Заведи отдельный образ (напр. "Post-Shower" / "Towel": towel around '
                f'waist, bare chest, wet hair) и назначь его на ВСЕ чанки этой сцены — иначе '
                f'гардероб будет прыгать между чанками (towel ↔ костюм).'
            )
        compose_warnings.append({
            'kind': 'wardrobe_state_mismatch',
            'detail': detail,
            'char_id': r.get('id'),
            'cue': cue,
            'assigned_outfit': label or 'base',
            'binding_auto_overridden': _was_overridden,
        })
    # 7) Identity / disguise HAIR mismatch: the character is in an ACTIVE narrative
    #    disguise persisted from an earlier episode (char['identity_shift'] — e.g.
    #    Claire went blonde as "Emma Cross" in ep4) but THIS chunk's outfit doesn't
    #    carry the disguised hair, so the render reverts to the natural hair (the
    #    ep5 brunette regression). Surface it so a dedicated disguise look gets made
    #    and assigned across the whole span instead of silently flipping hair.
    for r in ref_meta:
        if r.get('kind') != 'char':
            continue
        ch = next((c for c in (s.get('characters') or []) if c['id'] == r.get('id')), None)
        idsh = (ch.get('identity_shift') or {}) if ch else {}
        if not idsh.get('active') or not idsh.get('hair'):
            continue
        hair = idsh['hair']
        label = r.get('outfit') or ''
        outfit_obj = None
        if ch and label:
            outfit_obj = next((o for o in (ch.get('outfits') or []) if o.get('label') == label), None)
        haystack = ' '.join(filter(None, [
            label,
            (outfit_obj.get('description') if outfit_obj else '') or '',
            (outfit_obj.get('appearance_override') if outfit_obj else '') or '',
            (ch.get('appearance') if ch else '') or '',
        ])).lower()
        hair_tokens = [t for t in re.split(r'[^a-zа-яё]+', hair.lower()) if len(t) > 2]
        if any(t in haystack for t in hair_tokens):
            continue   # assigned look already carries the disguised hair — OK
        alias = idsh.get('alias') or ''
        compose_warnings.append({
            'kind': 'identity_appearance_mismatch',
            'detail': (
                f'{ch["name"] if ch else r.get("id")} по сюжету сейчас в изменённом образе'
                + (f' («{alias}»)' if alias else '')
                + f' — волосы должны быть {hair.upper()}, но назначенный образ "{label or "base"}" '
                f'этого не отражает, поэтому отрисуется исходный цвет волос. Заведи отдельный '
                f'образ для этой личности (напр. "{alias or "Disguise"}" с OUTFIT_DESC, включающим '
                f'"{hair} hair / wig" + одежду личности; либо задай образу '
                f'appearance_override="{hair} hair") и назначь его на ВСЕ чанки всего отрезка, пока '
                f'персонаж в этом образе — иначе волосы будут прыгать {hair}↔natural по сериалу.'
            ),
            'char_id': r.get('id'),
            'required_hair': hair,
            'alias': alias,
            'assigned_outfit': label or 'base',
        })
    if compose_warnings:
        _log_event('INFO', 'compose_warnings', sid=sid, ep_num=num,
                   warnings=compose_warnings)

    # ── VOICE / ACCENT FINAL ENFORCEMENT ────────────────────────────────────
    # Last line of the prompt — Banana/Seedance reads tail-of-prompt as
    # high-priority directive. Even if the composer forgot the STYLE-block
    # accent hint, this short caps line locks American-English narration
    # for every spoken line (regular dialogue + voiceover).
    # Positive-phrased per ByteDance docs (Seedance does not support negative
    # prompts; "No British/European..." rewritten as positive constraint).
    data['prompt'] = (data.get('prompt') or '').rstrip() + (
        "\n\nVOICE: every spoken line — regular dialogue AND voiceover — is "
        "performed in clear standard American English (General American accent), "
        "natural conversational American delivery throughout."
    )

    return jsonify({
        'prompt': data.get('prompt', ''),
        'refs': ref_meta,
        'ref_urls': ref_urls,
        'unresolved_refs': unresolved,
        'outfit_fallbacks': outfit_fallbacks,
        'duplicate_chars_dropped': duplicate_chars,
        'closeup_dropped': closeup_dropped if close_up_only else [],
        'auto_close_up_detected': bool(auto_close_up_block),
        'scene_continuity': data.get('scene_continuity'),
        'reasoning': data.get('reasoning', ''),
        'framing': framing or None,
        'framing_anchor': framing_anchor or None,
        'anti_background_scrub_hits': scrub_hits,
        'lastframe_attached': lastframe_attached,
        'cutframes_attached': cutframes_attached,
        'state_analysis_attached': state_analysis_attached,
        'pose_lock_fallback': pose_lock_fallback,
        'compose_warnings': compose_warnings,
        'prev_neighbour': debug_prev,
        'cur_pos_found': cur_pos >= 0,
    })
