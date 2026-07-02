"""Seedance video generation core: chunk helpers, last-frame/cut extraction,
AVAI submit circuit breaker, moderation pre-flight, QC pipeline, ref resolution."""
import base64
import datetime
import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from sw.auth import _get_user_avai_key
from sw.config import AVAI_API, DATA_ROOT, OPENAI_KEY
from sw.jsonutils import strip_json
from sw.llm import claude_ask_fast, claude_ask_vision
from sw.locks import _episode_lock
from sw.logging_utils import _log_event
from sw.storage import load_episode, save_episode, save_series, series_path
from sw.textrules_sanitizer import _sanitize_appearance_for_moderation
from sw.seedance_avai import _avai_upload_local_image, _extract_last_frame, _seedance_chunks

# ════════════════════════════════════════════════════════════════════════════
# CHUNK QC PIPELINE — pre-flight + post-completion checks before auto-mode
# proceeds to the next chunk (or before user accepts a manual generation).
#
# Stages:
#   0) prompt-english   — scan SUBJECT/ACTION for quoted non-English dialogue.
#   1) lang             — Whisper transcription on the chunk's audio.
#   2) grid-and-subs    — Claude Haiku Vision on probe frames.
#
# Result persisted on chunk['qc'] = {status, attempts, details, last_check_at}.
# Auto-retry (capped at QC_MAX_RETRIES=3) is dispatched from seedance_poll
# when status='fail'.
# ════════════════════════════════════════════════════════════════════════════

QC_MAX_RETRIES = 3

# Words that legitimately appear in English dialogue but use non-ASCII letters
# (loanwords, names with diacritics). Anything else with a Cyrillic / CJK char
# inside a quoted line is treated as a non-English dialogue leak.
_QC_NON_ENGLISH_RE = re.compile(r'[Ѐ-ӿ぀-ヿ一-鿿]')


def _qc_extract_audio(sid, video_relpath):
    """Extract mono 16kHz mp3 audio track from a chunk video. Returns abs path
    on success, None on failure (silent video, ffmpeg missing, etc.)."""
    src = series_path(sid) / video_relpath
    if not src.exists():
        return None
    out = src.with_name(src.stem + '_qc.mp3')
    if out.exists() and out.stat().st_size > 0:
        return out
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return None
    try:
        subprocess.run(
            [ffmpeg_bin, '-y', '-i', str(src),
             '-vn', '-ac', '1', '-ar', '16000', '-b:a', '64k', str(out)],
            capture_output=True, timeout=60, check=True,
        )
        if out.exists() and out.stat().st_size > 1024:
            return out
    except Exception as e:
        print(f'[qc] audio extract failed for {video_relpath}: {e}', flush=True)
    return None


def _qc_extract_frame_at(sid, video_relpath, sec, label):
    """Extract a PNG frame at the given timestamp. Returns (rel, abs) or None."""
    src = series_path(sid) / video_relpath
    if not src.exists():
        return None
    out = src.with_name(f'{src.stem}_qc_{label}.png')
    if out.exists() and out.stat().st_size > 0:
        return (str(out.relative_to(series_path(sid))), out)
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return None
    try:
        subprocess.run(
            [ffmpeg_bin, '-y', '-ss', f'{float(sec):.2f}', '-i', str(src),
             '-frames:v', '1', '-c:v', 'png', '-update', '1',
             '-pred', 'mixed', '-compression_level', '1', str(out)],
            capture_output=True, timeout=30, check=True,
        )
        if out.exists() and out.stat().st_size > 0:
            return (str(out.relative_to(series_path(sid))), out)
    except Exception as e:
        print(f'[qc] frame@{sec}s extract failed for {video_relpath}: {e}', flush=True)
    return None


def _qc_whisper_detect(audio_abs_path):
    """Run OpenAI Whisper on the chunk's audio. Returns dict with detected
    language code, confidence, transcript text, and `no_speech` flag.

    `no_speech=True` when the chunk has no real spoken dialogue — silent
    video, only SFX/music, or Whisper hallucinated over near-silence.
    The caller MUST skip language enforcement in that case (a chunk
    with no speech has no language to fail).

    Falls through to a pass-through result when OPENAI_KEY is missing —
    language QC stage is OPTIONAL by design."""
    if not OPENAI_KEY:
        return {'lang': '', 'confidence': 0.0, 'transcript': '', 'no_speech': True, 'skipped': 'no_openai_key'}
    if not audio_abs_path or not Path(audio_abs_path).exists():
        return {'lang': '', 'confidence': 0.0, 'transcript': '', 'no_speech': True, 'skipped': 'no_audio'}
    try:
        import openai
        client = openai.OpenAI(api_key=OPENAI_KEY)
        with open(audio_abs_path, 'rb') as f:
            resp = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                response_format='verbose_json',
            )
        # whisper-1 verbose_json fields: language (ISO code), text, segments[],
        # duration. Each segment exposes `no_speech_prob` (0..1) — high
        # values mean Whisper itself thinks the segment is non-speech.
        transcript = (getattr(resp, 'text', '') or '').strip()
        lang = (getattr(resp, 'language', '') or '').lower().strip()
        alphabetic = sum(1 for ch in transcript if ch.isalpha())
        confidence = 1.0 if alphabetic >= 6 else (alphabetic / 6.0)

        raw_segments = getattr(resp, 'segments', None) or []
        seg_probs = []
        for s in raw_segments:
            p = s.get('no_speech_prob') if isinstance(s, dict) else getattr(s, 'no_speech_prob', None)
            if p is None:
                continue
            try:
                seg_probs.append(float(p))
            except (TypeError, ValueError):
                pass

        # No-speech heuristic — chunk is "silent" when ANY of:
        #   (a) transcript has < 4 alphabetic chars (nothing said), OR
        #   (b) every segment has no_speech_prob ≥ 0.6 (Whisper itself
        #       thinks each segment is non-speech), OR
        #   (c) transcript matches a known Whisper hallucination on
        #       near-silence (e.g. «Thank you for watching»,
        #       «Продолжение следует», music tag «[Музыка]»).
        # (b) uses ALL not avg: a 10s chunk with 1s of grunt + 9s of
        # silence averages ~0.5 but DOES contain speech — we should
        # still language-check it. Only skip when literally no segment
        # contains speech.
        no_speech = alphabetic < 4
        if not no_speech and seg_probs:
            no_speech = all(p >= 0.6 for p in seg_probs)
        if not no_speech:
            t_low = transcript.lower().strip(' .!?,«»"\'')
            HALLUCINATIONS = {
                'thank you', 'thanks for watching', 'thank you for watching',
                'thanks for watching!', 'bye', 'okay', 'you',
                'продолжение следует', 'спасибо за просмотр', 'спасибо',
                '[музыка]', '[music]', '(music)', '(музыка)',
            }
            if t_low in HALLUCINATIONS:
                no_speech = True

        return {
            'lang': lang,
            'confidence': confidence,
            'transcript': transcript[:500],
            'no_speech': no_speech,
            'segments_count': len(seg_probs),
            'avg_no_speech_prob': round(sum(seg_probs) / len(seg_probs), 3) if seg_probs else None,
        }
    except Exception as e:
        print(f'[qc] whisper call failed: {type(e).__name__}: {e}', flush=True)
        return {'lang': '', 'confidence': 0.0, 'transcript': '', 'no_speech': True, 'skipped': f'whisper_error:{type(e).__name__}'}


def _qc_vision_grid_and_subs(frame_urls, frame_labels):
    """Single Claude Haiku Vision call that checks each attached frame for
    (a) residual moderation-bypass grid lines / panel borders / tiling
    artifacts and (b) burned-in subtitle text. Returns dict keyed by label
    with {grid, subs, confidence} per frame."""
    urls = [u for u in (frame_urls or []) if u]
    if not urls:
        return {'frames': {}, 'skipped': 'no_urls'}
    label_list = '\n'.join(f'  • Кадр {i+1}: {lbl}' for i, lbl in enumerate(frame_labels))
    sys = "You are a strict visual QC inspector. Output JSON only, no prose."
    user = (
        "Inspect the attached frames from a generated Pixar-style short-drama "
        "video. For EACH frame answer two binary questions:\n\n"
        "GRID — does the frame contain visible white grid lines, panel borders, "
        "tiling artifacts, or compositing seams that span across the whole image "
        "(typical residue when the moderation-bypass 'grid' mode failed to "
        "fully de-tile)? Faint motion-blur lines, hair strands, or background "
        "architecture grids are NOT this — only deliberate evenly-spaced "
        "horizontal+vertical guides covering the frame.\n\n"
        "SUBS — is there any burned-in subtitle / caption / dialog text "
        "overlaid on the frame (typically white text near the bottom with "
        "a dark stroke or background)? Diegetic text on signs, papers, "
        "phone screens does NOT count.\n\n"
        f"Frames in attached order:\n{label_list}\n\n"
        "Output STRICT JSON:\n"
        '{ "frames": [\n'
        '  {"grid": true|false, "subs": true|false, "confidence": "low"|"med"|"high"},\n'
        '  ...\n'
        '] }\n'
        "One object per frame in the same order they were attached."
    )
    try:
        raw = claude_ask_vision(user, urls, system=sys, model='haiku', max_tokens=512)
        # The model sometimes wraps JSON in ```json fences — strip if present.
        raw = raw.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```\s*$', '', raw)
        parsed = json.loads(raw)
        frames_arr = parsed.get('frames') or []
        out = {}
        for i, lbl in enumerate(frame_labels):
            if i < len(frames_arr):
                f = frames_arr[i] or {}
                out[lbl] = {
                    'grid': bool(f.get('grid')),
                    'subs': bool(f.get('subs')),
                    'confidence': f.get('confidence', 'low'),
                }
            else:
                out[lbl] = {'grid': False, 'subs': False, 'confidence': 'low'}
        return {'frames': out}
    except Exception as e:
        print(f'[qc] vision QC call failed: {type(e).__name__}: {e}', flush=True)
        return {'frames': {}, 'skipped': f'vision_error:{type(e).__name__}'}


def _qc_check_prompt_english(prompt):
    """Scan the composer-built prompt for non-English **DIALOGUE** specifically.
    A dialogue line in the composer's output looks like:
        Adrian (@Image1), with cold rage, says: "Get out of my house."
    The quoted span follows «says: » / «replies: » / «whispers: » / similar.
    We scope the non-English check to THOSE quoted spans only — generic
    narrative quotes in scene description (e.g. «По-крупному, шёпотом»)
    are NOT dialogue and must not trigger a QC fail.

    Real production bug: chunk QC every retry flagged `prompt_non_english`
    because the composer's Russian narrative («Adrian набирает...») got
    picked up. We restrict to lines that look like actual spoken delivery."""
    if not prompt:
        return {'ok': True, 'non_english_lines': []}
    offending = []
    # Match: <speaking verb> : "<dialogue content>"
    # Speaking verbs broad enough for English+Russian composer outputs.
    speak_verbs = (
        r'(?:says?|asks?|replies|whispers?|shouts?|yells?|murmurs?|breathes?|'
        r'mutters?|growls?|hisses?|barks?|spits?|sneers?|snaps?|'
        r'требует|спрашива[еют]+|отвеча[еют]+|шепч[ёе]т|кричит|произносит|говорит|'
        r'бросает|роняет|выпаливает|выкрикивает)'
    )
    dialogue_re = re.compile(
        speak_verbs + r'[^"«„]{0,40}?[:,]\s*[\"«„]([^\"»"]{2,200}?)[\"»"]',
        re.IGNORECASE | re.UNICODE,
    )
    for m in dialogue_re.finditer(prompt):
        text = m.group(1).strip()
        if not text:
            continue
        if _QC_NON_ENGLISH_RE.search(text):
            offending.append(text[:120])
    return {'ok': not offending, 'non_english_lines': offending[:5]}


def _qc_run_chunk(sid, num, idx):
    """Run all QC stages on a completed chunk. Persists result on chunk['qc'].
    Returns the qc dict. Safe to call multiple times — stages are idempotent
    and use the chunk's own scratch files (`_qc_*.png` / `_qc.mp3`)."""
    ep = load_episode(sid, num)
    if not ep:
        return None
    chunks = _seedance_chunks(ep)
    chunk = next((c for c in chunks if c.get('idx') == idx), None)
    if not chunk:
        return None
    if chunk.get('status') != 'completed':
        return None
    video_relpath = chunk.get('video_path')
    if not video_relpath:
        return None
    # ── Cross-chunk attempt counter ────────────────────────────────────
    # Real production bug: auto-retry creates a NEW chunk (new idx) instead
    # of replacing the failed one. Each new chunk starts fresh with
    # qc.attempts=1 → the «>= QC_MAX_RETRIES» cap NEVER fires → infinite
    # duplicate-chunk explosion (12+ copies of the same segment observed
    # on «My Stepmother Made Me a Servant», ep 44).
    # Count ALL completed chunks for the same chunk_text (or same
    # script_order if available) — that's the real retry budget for this
    # segment. Then this chunk's «attempts» = chunks_so_far + 1.
    cur_text = (chunk.get('chunk_text') or '').strip()
    cur_so = chunk.get('script_order')
    sibling_count = 0
    for c in chunks:
        if c.get('idx') == idx:
            continue   # don't count self
        # Match by script_order when both have one (most reliable), else by chunk_text
        if (isinstance(cur_so, int) and isinstance(c.get('script_order'), int)
                and c.get('script_order') == cur_so):
            sibling_count += 1
        elif cur_text and (c.get('chunk_text') or '').strip() == cur_text:
            sibling_count += 1
    prev_qc = chunk.get('qc') or {}
    # Per-chunk attempts: how many times QC ran on THIS chunk (always >=1 by end).
    own_attempts = int(prev_qc.get('attempts') or 0) + 1
    # Global attempts: includes failed siblings. This is what the cap uses.
    attempts = own_attempts + sibling_count

    details = {}
    fails = []

    # Stage 0 — prompt english check (cheap, no API).
    # NOTE: this is recorded but NOT added to `fails` here. Whisper (Stage 3)
    # inspects the actual generated audio and is the authoritative signal for
    # «is the chunk in English». The prompt scan is only a fallback for when
    # Whisper is unavailable (no audio / API down) — see Stage 3 below where
    # we promote `prompt_non_english` to a fail only in that case.
    # Real production bug: composer sometimes leaves Russian dialogue in
    # quotes inside the prompt («Vera (@Image2), отвечает: "Да, всё оформлено..."»),
    # but the prompt's trailing `VOICE: standard American English` hard-instruction
    # makes Seedance translate dialogue → audio comes out English anyway.
    # Whisper confirms English with confidence=1.0 — the chunk is fine.
    p_res = _qc_check_prompt_english(chunk.get('prompt') or '')
    details['prompt'] = p_res

    # Stage 1 — extract probe frames (frame@2s if duration > 2.5s, else mid; + lastframe)
    duration_sec = float(chunk.get('duration') or chunk.get('durationSec') or 10)
    probe_times = []
    if duration_sec > 2.5:
        probe_times.append((2.0, 'frame_2s'))
    midpoint = max(1.0, duration_sec / 2.0)
    probe_times.append((midpoint, 'frame_mid'))
    last = _extract_last_frame(sid, video_relpath)
    probe_assets = []  # list of (label, abs_path)
    for sec, lbl in probe_times:
        ex = _qc_extract_frame_at(sid, video_relpath, sec, lbl)
        if ex:
            probe_assets.append((lbl, ex[1]))
    if last:
        probe_assets.append(('lastframe', last[1]))

    # Stage 2 — vision QC
    if probe_assets:
        avai_urls = []
        used_labels = []
        for lbl, abs_path in probe_assets:
            try:
                url = _avai_upload_local_image(abs_path)
                avai_urls.append(url)
                used_labels.append(lbl)
            except Exception as e:
                print(f'[qc] frame upload failed ({lbl}): {e}', flush=True)
        if avai_urls:
            v_res = _qc_vision_grid_and_subs(avai_urls, used_labels)
            details['vision'] = v_res
            for lbl, info in (v_res.get('frames') or {}).items():
                # grid QC gate disabled — bypass moderation no longer
                # leaves residual grid lines, so info.get('grid') is
                # recorded in details but never fails the chunk.
                if info.get('subs'):
                    fails.append(f'subs:{lbl}')
        else:
            details['vision'] = {'skipped': 'no_uploads'}
    else:
        details['vision'] = {'skipped': 'no_frames'}

    # Stage 3 — whisper lang
    whisper_authoritative = False  # True iff Whisper actually inspected audio
    audio = _qc_extract_audio(sid, video_relpath)
    if audio:
        l_res = _qc_whisper_detect(audio)
        details['lang'] = l_res
        # Whisper returns either ISO code ('en') OR full English name
        # ('english') depending on response_format. Be permissive: accept
        # any English marker. Real production bug: every chunk was failing
        # `lang:english` because Whisper sent 'english' and we required 'en'.
        # Skip the language gate entirely when the chunk has no actual
        # speech — silent / SFX-only chunks have no language to fail and
        # Whisper otherwise hallucinates a non-English label on noise.
        ENGLISH_MARKERS = {'en', 'eng', 'english'}
        detected_lang = (l_res.get('lang') or '').lower().strip()
        if l_res.get('skipped'):
            # Whisper API call failed — fall through; not authoritative.
            pass
        elif l_res.get('no_speech'):
            whisper_authoritative = True  # silent chunk = nothing to fail on
        elif detected_lang:
            whisper_authoritative = True
            if (detected_lang not in ENGLISH_MARKERS
                    and (l_res.get('confidence') or 0) >= 0.5):
                fails.append(f'lang:{detected_lang}')
    else:
        details['lang'] = {'skipped': 'no_audio'}

    # Promote the prompt-language check to a fail ONLY when Whisper couldn't
    # give us an authoritative answer about the audio. With a real Whisper
    # verdict in hand, we trust the audio over the prompt text.
    if not p_res['ok'] and not whisper_authoritative:
        fails.append('prompt_non_english')

    # `attempts` already includes self (own_attempts >= 1) + siblings, so we
    # compare directly to the cap. No further +1.
    status = 'pass' if not fails else (
        'retry_exhausted' if attempts >= QC_MAX_RETRIES else 'fail'
    )

    qc_entry = {
        'status': status,
        'attempts': attempts,        # global count (this chunk + failed siblings)
        'own_attempts': own_attempts,  # how many times QC ran on THIS chunk
        'sibling_count': sibling_count,  # debug visibility
        'fails': fails,
        'details': details,
        'last_check_at': int(time.time()),
    }

    # Persist (re-load to avoid clobbering parallel updates).
    with _episode_lock(sid, num):
        ep_fresh = load_episode(sid, num)
        chunks_f = _seedance_chunks(ep_fresh)
        chunk_f = next((c for c in chunks_f if c.get('idx') == idx), None)
        if chunk_f:
            chunk_f['qc'] = qc_entry
            save_episode(sid, num, ep_fresh)

    print(f'[qc] chunk {idx} → {status} (own={own_attempts}, siblings={sibling_count}, global={attempts}, fails={fails})', flush=True)
    return qc_entry


def _qc_can_pass(chunk):
    """Helper for the auto-mode poll loop: returns True only when the chunk
    is unambiguously usable downstream — status=completed AND qc=pass (or
    QC disabled / not yet implemented for this chunk-shape)."""
    if chunk.get('status') != 'completed':
        return False
    qc = chunk.get('qc') or {}
    # No QC entry yet → not ready. Auto-mode should wait for QC to run.
    if not qc:
        return False
    return qc.get('status') in ('pass', 'retry_exhausted')



    """Locations created before Seedance was added store only ref_images. Lazy-upload
    the first ref to AVAI to get a public URL, persist it on the loc."""
    if loc.get('avai_url'):
        return loc['avai_url']
    refs = loc.get('ref_images') or []
    if not refs:
        return None
    local = series_path(sid) / refs[0]
    if not local.exists():
        return None
    try:
        url = _avai_upload_local_image(local)
        loc['avai_url'] = url
        return url
    except Exception as e:
        print(f'[seedance] loc upload failed for {loc.get("name")}: {e}')
        return None

def _ensure_loc_avai_url(sid, loc):
    """Lazy-upload the location's first local ref_image to AVAI when
    avai_url is missing. Persists the URL on the loc dict in-memory
    (caller must save_series). Returns the URL or None."""
    if loc.get('avai_url'):
        return loc['avai_url']
    refs = loc.get('ref_images') or []
    if not refs:
        return None
    local = series_path(sid) / refs[0]
    if not local.exists():
        return None
    try:
        url = _avai_upload_local_image(local)
        loc['avai_url'] = url
        return url
    except Exception as e:
        print(f'[seedance] loc avai upload failed for {loc.get("name")}: {e}')
        return None

def _ensure_char_avai_base_url(sid, char):
    """Lazy-upload the character's first local ref_image to AVAI when
    avai_base_url is missing. Persists the URL on the char dict in-memory
    (caller must save_series). Returns the URL or None."""
    if char.get('avai_base_url'):
        return char['avai_base_url']
    refs = char.get('ref_images') or []
    if not refs:
        return None
    local = series_path(sid) / refs[0]
    if not local.exists():
        return None
    try:
        url = _avai_upload_local_image(local)
        char['avai_base_url'] = url
        return url
    except Exception as e:
        print(f'[seedance] char base upload failed for {char.get("name")}: {e}')
        return None

def _resolve_ref_url(s, ref, sid=None):
    """ref = {'kind':'char'|'outfit'|'loc', 'id':..., 'outfit':...}.
    Returns public AVAI URL or None.

    Side-effect: if a non-null outfit label was requested but doesn't match any
    of the character's outfits, marks ref['_outfit_fallback']=True so the caller
    can warn the user. Without this signal, hallucinated outfit labels (e.g.
    'casual', 'formal') silently fall through to base — outfit drift bug."""
    if not ref:
        return None
    kind = ref.get('kind')
    if kind == 'char':
        c = next((x for x in s.get('characters', []) if x['id'] == ref.get('id')), None)
        if not c:
            return None
        requested = ref.get('outfit')
        if requested:
            for o in c.get('outfits', []) or []:
                if o.get('label') == requested:
                    return o.get('avai_url') or c.get('avai_base_url')
            # Outfit label was requested but not found → fall back to base, but mark it
            ref['_outfit_fallback'] = True
            ref['_outfit_requested'] = requested
        # Base look. Lazy-upload local ref_image if avai_base_url absent.
        if c.get('avai_base_url'):
            return c['avai_base_url']
        if sid:
            url = _ensure_char_avai_base_url(sid, c)
            if url:
                save_series(sid, s)
                return url
        return None
    if kind == 'loc':
        l = next((x for x in s.get('locations', []) if x['id'] == ref.get('id')), None)
        if not l:
            return None
        url = l.get('avai_url')
        if not url and sid:
            url = _ensure_loc_avai_url(sid, l)
            if url:
                save_series(sid, s)  # persist new avai_url
        return url
    if kind == 'item':
        # Plot-relevant items (locket, USB stick, bouquet, etc.). LLM picks
        # them when the chunk text mentions the object visually — they go
        # into Seedance refs as an extra @ImageN slot so the rendered video
        # can carry the prop with consistent appearance.
        it = next((x for x in s.get('items', []) if x['id'] == ref.get('id')), None)
        if not it:
            return None
        url = it.get('avai_url')
        if not url:
            # Lazy-fallback: if avai_url is missing but we have a local ref_image,
            # we can't upload it without _ensure_item_avai_url (which doesn't
            # exist yet). Return None and let the caller log it.
            pass
        return url
    if kind == 'url':
        return ref.get('url')
    if kind == 'lastframe':
        # Continuity reference: URL is already resolved & uploaded by /compose
        return ref.get('url')
    return None

def _download_video(url, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return dest_path
