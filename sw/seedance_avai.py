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

# SEEDANCE 2.0 — video generation via AVAI Gen
# Async flow: POST → 202 {job_id, status_url} → poll status_url → download mp4
# State persisted on episode JSON: episode['seedance_chunks'] = [
#   {idx, job_id, status, video_url, video_path, prompt, ref_urls,
#    chunk_text, duration, resolution, moderation_bypass, ending_state, cost}
# ]
# ════════════════════════════════════════════════════════════════════════════

def _seedance_chunks(ep):
    return ep.setdefault('seedance_chunks', [])

def _next_chunk_idx(ep):
    chunks = _seedance_chunks(ep)
    return (max((c.get('idx', -1) for c in chunks), default=-1)) + 1

def _extract_last_frame(sid, video_relpath):
    """Extract a PNG of the last frame of a chunk's video.
    Cached: returns (relpath, abs_path). Re-extracted if missing.

    Real production bug: `-sseof -0.1` returns empty on many Seedance MP4s
    because the last keyframe is often 0.2-0.3s before the absolute end, so
    the 0.1s window catches nothing decodable. Walk a stepladder of -sseof
    values, then fall back to ffprobe-based absolute output-seek.
    """
    src = series_path(sid) / video_relpath
    if not src.exists():
        return None
    out = src.with_name(src.stem + '_lastframe.png')
    if out.exists() and out.stat().st_size > 0:
        return (str(out.relative_to(series_path(sid))), out)
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        print(f'[_extract_last_frame] ffmpeg binary not found', flush=True)
        return None
    png_args = ['-frames:v', '1', '-c:v', 'png', '-update', '1',
                '-pred', 'mixed', '-compression_level', '1', str(out)]
    # Pass 1: input-seek before -i (fast). Widen window until ffmpeg actually
    # decodes a frame. 0.1s is too tight on many real videos.
    last_err = None
    for sseof in ('-0.3', '-0.6', '-1.2', '-2.5'):
        try:
            subprocess.run(
                [ffmpeg_bin, '-y', '-sseof', sseof, '-i', str(src)] + png_args,
                capture_output=True, timeout=30, check=True,
            )
            if out.exists() and out.stat().st_size > 0:
                return (str(out.relative_to(series_path(sid))), out)
        except Exception as e:
            last_err = e
            continue
    # Pass 2: ffprobe → absolute output-seek.
    ffprobe_bin = shutil.which('ffprobe')
    if ffprobe_bin:
        try:
            res = subprocess.run(
                [ffprobe_bin, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', str(src)],
                capture_output=True, timeout=15, check=True,
            )
            dur = float((res.stdout or b'').decode().strip())
            ss = max(0.0, dur - 0.08)
            subprocess.run(
                [ffmpeg_bin, '-y', '-ss', f'{ss:.3f}', '-i', str(src)] + png_args,
                capture_output=True, timeout=30, check=True,
            )
            if out.exists() and out.stat().st_size > 0:
                return (str(out.relative_to(series_path(sid))), out)
        except Exception as e:
            last_err = e
    if last_err is not None:
        print(f'[_extract_last_frame] all attempts failed for {video_relpath}: {type(last_err).__name__}: {last_err}', flush=True)
    return None


def _detect_cuts(video_abs_path, threshold=0.35):
    """Detect hard cuts inside a video using ffmpeg's scene detection.
    Returns sorted list of cut timestamps (seconds, float) where the FIRST
    frame of the NEW shot starts. Returns [] on any failure or if ffmpeg
    is not installed. Threshold: 0.3-0.45 catches most AI-generated cuts."""
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin or not video_abs_path or not Path(video_abs_path).exists():
        return []
    try:
        # showinfo prints pts_time for each frame the select filter passes.
        proc = subprocess.run(
            [ffmpeg_bin, '-hide_banner', '-i', str(video_abs_path),
             '-vf', f"select='gt(scene,{threshold})',showinfo",
             '-an', '-f', 'null', '-'],
            capture_output=True, timeout=60, text=True
        )
        # ffmpeg writes filter output to stderr
        out = (proc.stderr or '') + (proc.stdout or '')
    except Exception:
        return []
    cuts = []
    for m in re.finditer(r'pts_time:([\d.]+)', out):
        try:
            t = float(m.group(1))
            # Drop very-early "cuts" (often the first frame itself).
            if t > 0.4:
                cuts.append(t)
        except ValueError:
            continue
    # Dedupe near-duplicates (within 0.3s of each other) — sometimes ffmpeg
    # emits 2 close hits for the same cut due to motion.
    cuts.sort()
    deduped = []
    for t in cuts:
        if not deduped or (t - deduped[-1]) > 0.3:
            deduped.append(t)
    return deduped


def _extract_keyframes_at_cuts(sid, video_relpath, cut_timestamps, max_frames=3,
                               pre_offset=0.05):
    """For each cut timestamp T, extract the frame at T-pre_offset (i.e. the
    LAST frame of the OUTGOING shot, just before the cut). Cached on disk as
    <stem>_cutframe_<i>.png (lossless — Seedance uses these as input refs and
    JPEG artifacts compound at chunk boundaries). Caps at max_frames (oldest
    cuts first → most context) to keep ref budget under control. Returns list
    of (relpath, abs_path)."""
    src = series_path(sid) / video_relpath
    if not src.exists() or not cut_timestamps:
        return []
    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        return []
    selected = list(cut_timestamps)[:max_frames]
    out_paths = []
    for i, t in enumerate(selected):
        seek_t = max(0.0, t - pre_offset)
        out = src.with_name(f'{src.stem}_cutframe_{i}.png')
        if out.exists() and out.stat().st_size > 0:
            out_paths.append((str(out.relative_to(series_path(sid))), out))
            continue
        try:
            subprocess.run(
                [ffmpeg_bin, '-y', '-ss', f'{seek_t:.3f}', '-i', str(src),
                 '-frames:v', '1', '-c:v', 'png', '-pred', 'mixed', '-compression_level', '1',
                 str(out)],
                capture_output=True, timeout=30, check=True
            )
            if out.exists() and out.stat().st_size > 0:
                out_paths.append((str(out.relative_to(series_path(sid))), out))
        except Exception:
            continue
    return out_paths


def _purge_continuity_sidecars(sid, video_relpath):
    """Delete the cached continuity-frame sidecars derived from a chunk video:
    <stem>_lastframe.png and <stem>_cutframe_*.png.

    WHY this exists — production bug: chunk videos are named deterministically
    `seedance_ep{N}_chunk{IDX}.mp4`, and _extract_last_frame / _extract_keyframes
    cache their PNGs next to the mp4 keyed by that stem (returning the cached
    file whenever it already exists). `_next_chunk_idx` reuses idx 0,1,2… after
    a chunk record is removed, so when the user deletes ALL chunks and re-runs
    the series, the freshly generated `..._chunk001.mp4` lands on the SAME stem
    as the deleted one — and continuity extraction returns the PREVIOUS take's
    stale lastframe/cutframes (old hair colour, wardrobe, characters). Deleting
    the mp4 alone did not clear these. Call this whenever a chunk video is
    deleted OR a new video is written to a stem, so a re-generated chunk can
    never inherit the prior occupant's frames.
    """
    if not video_relpath:
        return
    try:
        src = series_path(sid) / video_relpath
        parent = src.parent
        stem = src.stem
    except Exception:
        return
    try:
        victims = [parent / f'{stem}_lastframe.png']
        victims += list(parent.glob(f'{stem}_cutframe_*.png'))
        for p in victims:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ── AVAI Submit Circuit Breaker ───────────────────────────────────────────
# Hard physical limit on AVAI submits. Lives inside _avai_seedance_start so
# EVERY path that sends money to AVAI must pass through these checks. No
# caller can bypass — if any future bug causes a loop, the breaker trips and
# refuses further submits.
#
# Real production incident 2026-05-19: a stuck escalation flag fired one new
# AVAI submit every 8 seconds for hours, costing ~$230 before user noticed.
# This breaker would have stopped at submit #3 (per-fingerprint cap).
_AVAI_AUDIT_LOG = DATA_ROOT / 'avai_submit_audit.jsonl'
_AVAI_KILL_SWITCH = DATA_ROOT / 'AVAI_KILL_SWITCH'
_avai_rate_lock = threading.Lock()
_avai_recent_submits = []   # list[(epoch_sec, fingerprint)]

# Tunable limits — calibrated against real user flows
_AVAI_MAX_PER_FP_10MIN = 3   # same prompt+refs can't fire 4×+ in 10 min
_AVAI_MAX_PER_MINUTE   = 15  # global cap per 60s — bulk-gen of 10 = OK; loop = blocked
_AVAI_KILLSWITCH_5MIN  = 20  # 20+ submits in 5 min auto-trips permanent kill switch
                              # (tightened from 60 by user after $600 incident — max $12 damage)

class AVAICircuitBreakerError(RuntimeError):
    """Raised when a submit is refused by the circuit breaker. Indicates a
    runaway loop bug or pricing risk — refuse to spend money."""
    pass


# ── Seedance Moderation Pre-Flight Checker ─────────────────────────────────
# Calibrated against the ONLY official ByteDance moderation source we could
# verify (docs.byteplus.com/en/docs/ModelArk/Content_Pre-filter):
#   - Nudity, sexual, and graphic content (pornography, abuse, violence, self-harm)
#   - Misinformation
#   - Hate speech (racial, nationality, ethnic, sexual orientation, physical disability)
# Everything else (specific keywords, before/after substitutions, dialogue
# scanning) is community lore — NOT used here. The checker uses Haiku to
# evaluate prompts against these categories and returns a verdict the user
# can act on. Cost: ~$0.0004 per check.
_SEEDANCE_MODERATION_CHECKER_SYS = (
    "You are a content-moderation pre-flight checker for ByteDance Seedance 2.0 "
    "video generation. Your sole job: read a Seedance motion prompt and decide "
    "whether it will likely be REJECTED by Seedance's documented Content Pre-filter.\n\n"
    "OFFICIAL ByteDance Content Pre-filter categories (from "
    "docs.byteplus.com/en/docs/ModelArk/Content_Pre-filter — the ONLY documented "
    "source). These are the ONLY things you evaluate:\n"
    "  1. SEXUAL — pornography, explicit sexual acts, nudity, post-clothed states, "
    "     erotic situations after a kiss. NOT blocked: kissing, embracing, romantic "
    "     tension, dancing close, undressing implied off-screen.\n"
    "  2. VIOLENCE_GRAPHIC — extreme graphic violence with visible gore, dismemberment, "
    "     extensive blood spray on-screen. NOT blocked: action scenes, fights, "
    "     gunshots without visible wounds, punches landing, falling, dramatic "
    "     confrontations, threats, slaps. (Crime drama is allowed; gore is not.)\n"
    "  3. SELF_HARM — explicit self-injury depicted on screen (slashing wrists shown, "
    "     overdose acts depicted, etc.). NOT blocked: emotional despair, character "
    "     contemplating, dialogue about pain.\n"
    "  4. HATE_SPEECH — slurs, dehumanization of protected groups, racial/ethnic/"
    "     national/sexual-orientation/disability targeting in dialogue or text.\n"
    "  5. MISINFORMATION — visually depicting real public figures in fabricated events "
    "     (real politicians/celebrities by name doing things they didn't).\n\n"
    "CRITICAL RULES — read carefully:\n"
    "  • Crime drama with violence, gunshots, threats, slaps, action, confrontations, "
    "    chase scenes, intimidation, fights = ALLOWED. Do NOT flag these.\n"
    "  • Romantic scenes with kissing, embracing, attraction, passion = ALLOWED.\n"
    "  • Visible weapons (guns, knives) as props or in action = ALLOWED unless "
    "    paired with explicit gore (cut-off limbs, exposed organs, gushing blood).\n"
    "  • Dialogue lines (text inside quotes spoken by characters) — DO NOT MODERATE. "
    "    Dialogue is delivered as lip-synced audio. Threats in dialogue ('you'll "
    "    pay for this', 'I'll kill you') are NOT a moderation problem.\n"
    "  • Mention of blood/wounds is fine; visible explicit gore is the line.\n"
    "  • Style of the prompt (cartoon/cel-shaded/anime/photoreal) does NOT change "
    "    your judgment — Seedance applies the same rules.\n"
    "  • You are NOT a censor. You only flag what Seedance's documented filter "
    "    will REJECT. If it's just 'edgy', mark PASS.\n\n"
    "Return ONLY valid JSON, no markdown fences, no commentary:\n"
    "{\n"
    '  "verdict": "pass" | "warn" | "reject",\n'
    '  "categories": ["sexual" | "violence_graphic" | "self_harm" | "hate_speech" | "misinformation"],\n'
    '  "reasoning": "one short sentence Russian — why this verdict",\n'
    '  "problem_snippets": ["exact substring from the prompt that triggered each category"],\n'
    '  "suggestion": "one short Russian sentence — how to rewrite minimally if reject. Empty if pass."\n'
    "}\n\n"
    "VERDICT GUIDE:\n"
    "  - 'pass' = nothing problematic, full submit recommended.\n"
    "  - 'warn' = grey area, might pass but flagged for user awareness. Submit proceeds.\n"
    "  - 'reject' = clear violation, Seedance will block. Refuse submit.\n\n"
    "Default to 'pass' when uncertain. False rejects are MORE costly than false passes."
)


# Master switch for the pre-flight Haiku moderation gate on /seedance/start.
# Disabled (2026-06-09): it false-positived on ordinary dramatic beats (a
# character collapsing, a poisoning scene) and HALTED Auto-mode — every flagged
# chunk popped a blocking browser confirm() that a human had to click "ОК" on,
# defeating the whole point of automation. The in-editor phrase-scan panel
# («⚠️ Возможные проблемы с модерацией Seedance») still surfaces risks
# non-blockingly, and Seedance's own server-side moderation remains the real
# gate. Flip back to True to re-enable the pre-flight block.
SEEDANCE_PRECHECK_ENABLED = False


def _seedance_moderation_precheck(prompt_text):
    """Pre-flight moderation check via Haiku. Returns dict with verdict +
    reasoning. Safe to call inline — Haiku is fast (~2s) and cheap (~$0.0004).
    Returns {'verdict': 'pass', ...} on any error (fail-open — don't block
    legitimate submits on infrastructure issues)."""
    if not prompt_text or len(prompt_text.strip()) < 20:
        return {'verdict': 'pass', 'reasoning': '(prompt empty or too short to evaluate)',
                'categories': [], 'problem_snippets': [], 'suggestion': ''}
    try:
        raw = claude_ask_fast(
            f"Промпт для проверки (между маркерами):\n=== PROMPT ===\n{prompt_text[:8000]}\n=== END PROMPT ===\n\n"
            f"Верни strict JSON по схеме.",
            system=_SEEDANCE_MODERATION_CHECKER_SYS,
        )
        data = json.loads(strip_json(raw))
        # Normalize required fields
        return {
            'verdict': data.get('verdict') or 'pass',
            'categories': data.get('categories') or [],
            'reasoning': data.get('reasoning') or '',
            'problem_snippets': data.get('problem_snippets') or [],
            'suggestion': data.get('suggestion') or '',
            'checked_at': int(time.time()),
        }
    except Exception as e:
        # Fail-open. Never block on infra errors.
        print(f'[moderation-precheck] check failed (fail-open): {e}', flush=True)
        return {
            'verdict': 'pass',
            'reasoning': f'(checker error: {type(e).__name__})',
            'categories': [],
            'problem_snippets': [],
            'suggestion': '',
            'checked_at': int(time.time()),
            'error': str(e)[:200],
        }


# ── Moderation-block classification (legitimate-recovery ladder) ────────────
# AVAI is undocumented publicly, so the exact block-response shape is unknown.
# We classify BEST-EFFORT from a stringified blob of (error + raw + placeholder
# url) so it works regardless of AVAI's field names, and we LOG the raw payload
# verbatim on every block (see seedance_poll) to learn ground truth over time.
# Per product decision we do NOT use evasion (grid/collage/cartoon) — the class
# only steers which *legitimate* content fix the ladder applies.
_MOD_HARD_RE  = re.compile(
    r'minor|child|underage|csam'
    r'|nud(?:e|ity)|porn|nsfw|sexual|explicit'
    r'|celebrit|public[\s_-]?figure|deepfake',
    re.IGNORECASE)
_MOD_FACE_RE  = re.compile(
    r'\bface|portrait|likeness|resembl|identit|real[\s_-]?person'
    r'|input[\s_-]?image|reference[\s_-]?image',
    re.IGNORECASE)
_MOD_AUDIO_RE = re.compile(
    r'audio|voice|speech|\bsong|music|lyric|soundtrack',
    re.IGNORECASE)


def _classify_moderation_block(raw, error='', video_url=''):
    """Best-effort class of a Seedance/AVAI moderation block:
      'hard'    — real person / NSFW / minors: no legitimate workaround → stop.
      'face'    — reference-image face/likeness block: fix is swapping the
                  reference photo, NOT editing the prompt → stop + surface.
      'audio'   — audio/dialogue/song path block: fix is a minimal offending-
                  line edit (voice is kept — it is the final deliverable).
      'content' — generic prompt/output content block: fix is retry / action
                  re-description.
    Defaults to 'content' when nothing recognizable is present."""
    try:
        raw_str = json.dumps(raw, ensure_ascii=False)[:4000] if raw else ''
    except Exception:
        raw_str = str(raw)[:4000]
    blob = ' '.join([str(error or ''), raw_str, str(video_url or '')])
    if _MOD_HARD_RE.search(blob):
        return 'hard'
    if _MOD_FACE_RE.search(blob):
        return 'face'
    if _MOD_AUDIO_RE.search(blob):
        return 'audio'
    return 'content'


# ── Render-prompt meta-instruction scrubber (submit-time safety net) ─────────
# Legacy chunk prompts (composed BEFORE the compose-side cleanup) have Russian
# meta-instructions / internal notes / the hardcoded pose example baked into
# chunk['prompt']. Every re-submit path (heal / pass-moderation / retry / reuse)
# sends that STORED prompt, so the junk still reaches AVAI. This strips the known
# meta blocks at the LAST mile — every submit passes through _avai_seedance_start
# — while KEEPING the real content (ТЕКУЩЕЕ СОСТОЯНИЕ state, ПОЗИЦИИ/ending_state,
# clothing desc, camera framing, VOICE). Idempotent: a no-op on already-clean
# (freshly composed) prompts.
# The prompt STRUCTURE (positions, POSTURE LOCK, continuity, framing) is the
# user's intentional design and must be KEPT. Only the hardcoded EXAMPLE phrases
# baked into old stored prompts (the moderation-trigger "Fox Woman … under the
# car", the "Maya … blood" example, the "{name} disappears" example) are
# neutralized here — the last-mile net for legacy chunks re-submitted via heal /
# pass-moderation / retry / reuse. New prompts (composer already cleaned) hit
# these as no-ops. We do NOT strip any instruction block.
_EXAMPLE_SUBS = [
    (re.compile(r'Fox Woman lies halfway under the car', re.IGNORECASE), 'персонаж в нестандартной позе'),
    (re.compile(r'Fox Woman lies under the car', re.IGNORECASE), 'персонаж в нестандартной позе'),
    (re.compile(r'Fox Woman продолжает лежать наполовину под машиной,?\s*(?:голова и плечи торчат наружу)?'),
     'персонаж в позе из сценария'),
    (re.compile(r'Fox Woman стоит'), 'персонаж стоит'),
    (re.compile(r'\bFox Woman\b'), 'персонаж'),
    (re.compile(r"'Maya, на губе кровь из разбитой губы, мокрые волосы, разорванная блузка, дрожит'"),
     'видимое состояние персонажа из анализа'),
    (re.compile(r'persona под машиной, на коленях', re.IGNORECASE), 'на коленях, лёжа'),
    (re.compile(r'«([^»]{1,40}) disappears in another direction»'), r'«\1 уходит в сторону»'),
]


def _strip_meta_instructions(prompt):
    """Neutralize ONLY the hardcoded example phrases baked into legacy prompts
    (Fox Woman / Maya-blood / disappears). Keeps the whole prompt STRUCTURE
    (positions, POSTURE LOCK, framing, VOICE) intact. Idempotent."""
    if not prompt:
        return prompt
    out = prompt
    for rx, repl in _EXAMPLE_SUBS:
        out = rx.sub(repl, out)
    return out


def _avai_kill_switch_status():
    """Returns dict {active, since, reason, file_path} for UI display.
    Always safe to call — handles missing file / parse errors gracefully."""
    if not _AVAI_KILL_SWITCH.exists():
        return {'active': False, 'reason': '', 'since': None, 'file_path': str(_AVAI_KILL_SWITCH)}
    try:
        reason = _AVAI_KILL_SWITCH.read_text()[:800]
        since = int(_AVAI_KILL_SWITCH.stat().st_mtime)
    except Exception as e:
        reason = f'(read failed: {e})'
        since = None
    return {
        'active': True,
        'reason': reason,
        'since': since,
        'file_path': str(_AVAI_KILL_SWITCH),
    }

def _avai_fingerprint(prompt, ref_urls, duration, moderation_bypass):
    """Stable 16-char hash of submit content. Same fingerprint = duplicate
    submit. Identical prompt+refs+duration+bypass → blocked at the breaker."""
    payload = '\n'.join([
        (prompt or '')[:5000],
        '|'.join(sorted(ref_urls or [])),
        str(duration),
        str(moderation_bypass or ''),
    ])
    return hashlib.sha256(payload.encode('utf-8', errors='ignore')).hexdigest()[:16]

def _avai_circuit_breaker_check(prompt, ref_urls, duration, moderation_bypass):
    """Hard-block submits exceeding rate limits. Raises AVAICircuitBreakerError
    on block. Records to audit log on pass. MUST be called before every AVAI
    submit — already wired into _avai_seedance_start as the first line."""
    # Permanent kill switch first (cheapest check)
    if _AVAI_KILL_SWITCH.exists():
        raise AVAICircuitBreakerError(
            f'AVAI submits DISABLED — kill switch active at {_AVAI_KILL_SWITCH}. '
            f'Investigate, then delete the file to re-enable.'
        )
    fp = _avai_fingerprint(prompt, ref_urls, duration, moderation_bypass)
    now = time.time()
    with _avai_rate_lock:
        # Prune entries older than 10 min
        cutoff = now - 600
        _avai_recent_submits[:] = [(t, f) for (t, f) in _avai_recent_submits if t > cutoff]
        # 1) Per-fingerprint duplicate cap in last 10 min
        fp_count = sum(1 for (t, f) in _avai_recent_submits if f == fp)
        if fp_count >= _AVAI_MAX_PER_FP_10MIN:
            err = (
                f'AVAI circuit breaker: identical content (fp={fp}) submitted '
                f'{fp_count}× in last 10 min — refusing duplicate. Looks like '
                f'a retry loop. Investigate before manually retrying.'
            )
            print(f'[avai-cb] BLOCKED fp={fp}: {err}', flush=True)
            raise AVAICircuitBreakerError(err)
        # 2) Global per-minute cap
        per_min = sum(1 for (t, f) in _avai_recent_submits if t > now - 60)
        if per_min >= _AVAI_MAX_PER_MINUTE:
            err = (
                f'AVAI circuit breaker: {per_min} submits in last 60 sec '
                f'(cap {_AVAI_MAX_PER_MINUTE}). Refusing — runaway loop suspected.'
            )
            print(f'[avai-cb] BLOCKED rate: {err}', flush=True)
            raise AVAICircuitBreakerError(err)
        # 3) 5-min escalation → permanent kill switch
        per_5min = sum(1 for (t, f) in _avai_recent_submits if t > now - 300)
        if per_5min >= _AVAI_KILLSWITCH_5MIN:
            try:
                _AVAI_KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
                _AVAI_KILL_SWITCH.write_text(
                    f'Auto-tripped at {datetime.datetime.now().isoformat()}\n'
                    f'{per_5min} AVAI submits in 5 minutes (limit {_AVAI_KILLSWITCH_5MIN}).\n'
                    f'Recent fingerprints: {set(f for (t, f) in _avai_recent_submits if t > now - 300)}\n'
                    f'Investigate root cause, then delete this file to re-enable submits.'
                )
            except Exception as e:
                print(f'[avai-cb] kill-switch write failed: {e}', flush=True)
            err = (
                f'AVAI KILL SWITCH AUTO-TRIPPED: {per_5min} submits in 5 min. '
                f'All AVAI submits BLOCKED until {_AVAI_KILL_SWITCH} is removed.'
            )
            print(f'[avai-cb] {err}', flush=True)
            raise AVAICircuitBreakerError(err)
        # OK — record this submit
        _avai_recent_submits.append((now, fp))
    # Audit log — append-only is atomic on POSIX
    try:
        _AVAI_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_AVAI_AUDIT_LOG, 'a') as f:
            f.write(json.dumps({
                'ts': int(now),
                'iso': datetime.datetime.fromtimestamp(now).isoformat(),
                'fp': fp,
                'prompt_prefix': (prompt or '')[:80],
                'ref_count': len(ref_urls or []),
                'duration': duration,
                'moderation_bypass': moderation_bypass,
            }) + '\n')
    except Exception as e:
        print(f'[avai-cb] audit log write failed: {e}', flush=True)


def _avai_seedance_start(prompt, ref_urls, duration, resolution, moderation_bypass,
                          aspect_ratio='9:16', generate_audio=True,
                          moderation_bypass_prompt=None, avai_key=None,
                          model='reference-fast'):
    """Kick off an async Seedance 2.0 reference-* job.
    Returns dict {job_id, status_url, raw}.

    model: 'reference-pro' (premium) or 'reference-fast' (~1.5× cheaper,
    slightly lower quality but supports the same up-to-9 contextImages
    reference flow). Plain 'pro'/'fast' max 2 contextImages — NOT compatible
    with our 4-9 ref pipeline (chars + location + lastframe + cutframes).

    avai_key: REQUIRED when called from a background thread (no Flask request
    context). Caller must resolve it via _get_user_avai_key() inside the request
    handler and pass it explicitly. Falls back to _get_user_avai_key() only when
    called inline from a request handler (image-style sync calls)."""
    # FINAL moderation net — scrub sexualizing / NSFW wording out of the FULL
    # composed prompt right before it leaves for the provider. Catches prompts
    # that were frozen into episode JSON (seedance_chunks[].prompt) before the
    # upstream fixes existed, plus anything a fresh compose still let through.
    # De-escalates only (see _sanitize_appearance_for_moderation); the BINDING
    # already carries clean descriptions, this just guarantees the submit too.
    _clean_prompt = _sanitize_appearance_for_moderation(prompt)
    if _clean_prompt != prompt:
        try:
            _log_event('INFO', 'avai_prompt_sanitized',
                       before=prompt[:200], after=_clean_prompt[:200])
        except Exception:
            pass
        prompt = _clean_prompt
    # Strip legacy meta-instruction junk baked into old stored prompts (the
    # last-mile net — EVERY submit passes through here, incl. heal / pass-
    # moderation / retry / reuse of chunks composed before the compose-side
    # cleanup). Keeps content (state / positions / framing / clothing / VOICE),
    # drops the noise + the hardcoded pose example. No-op on clean prompts.
    _stripped = _strip_meta_instructions(prompt)
    if _stripped != prompt:
        try:
            _log_event('INFO', 'avai_prompt_meta_stripped',
                       removed_chars=len(prompt) - len(_stripped))
        except Exception:
            pass
        prompt = _stripped
    # Hard rate-limit / circuit-breaker — fires BEFORE any AVAI network call
    # so cost is bounded regardless of caller bugs. Raises AVAICircuitBreakerError
    # if limits exceeded; caller must catch + show user-friendly error.
    _avai_circuit_breaker_check(prompt, ref_urls, duration, moderation_bypass)
    if model not in ('reference-pro', 'reference-fast'):
        model = 'reference-fast'
    payload = {
        'provider': 'seedance2',
        'model': model,
        'prompt': prompt,
        'duration': str(int(duration)),
        'resolution': resolution,        # '720p' | '480p'
        'aspect_ratio': aspect_ratio,
        'generate_audio': bool(generate_audio),
        'num_outputs': 1,
    }
    if ref_urls:
        payload['contextImages'] = [{'url': u} for u in ref_urls if u][:9]
    if moderation_bypass and moderation_bypass != 'off':
        payload['moderation_bypass'] = moderation_bypass
        if moderation_bypass_prompt:
            payload['moderation_bypass_prompt'] = moderation_bypass_prompt
    key = avai_key if avai_key is not None else _get_user_avai_key()
    if not key:
        raise RuntimeError('AVAI seedance2 start: no API key (request context lost in background thread or user has no key configured)')
    headers = {'x-api-key': key, 'content-type': 'application/json'}
    # Async mode: server returns 202 with job_id+status_url immediately
    resp = requests.post(
        AVAI_API + '?async=true', json=payload, headers=headers, timeout=(15, 240)
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f'AVAI seedance2 start error {resp.status_code}: {resp.text[:400]}')
    data = resp.json()
    job_id = data.get('job_id') or data.get('id') or data.get('message_id')
    status_url = data.get('status_url') or (
        f'/api/public/generate/jobs/{job_id}' if job_id else None
    )
    # Normalize relative URL → absolute
    if status_url and status_url.startswith('/'):
        status_url = 'https://avai-gen.com' + status_url
    if not job_id:
        raise RuntimeError(f'AVAI seedance2: no job_id in response: {str(data)[:300]}')
    return {'job_id': job_id, 'status_url': status_url, 'raw': data}

def _avai_seedance_status(job_id, status_url=None, avai_key=None):
    """Poll job. Returns dict {status, progress, video_url, cost, error, raw}.
    avai_key: optional explicit override for callers outside request context.

    Retries on transient network errors (SSL EOF, connection reset, timeout)
    — common when AVAI restarts a worker or an intermediate proxy hiccups.
    Without retry a SINGLE network blip during poll permanently marks the
    chunk as failed (caller catches the exception and writes status='failed'),
    erasing minutes of actual work."""
    key = avai_key if avai_key is not None else _get_user_avai_key()
    headers = {'x-api-key': key}
    url = status_url or f'https://avai-gen.com/api/public/generate/jobs/{job_id}'
    if url.startswith('/'):
        url = 'https://avai-gen.com' + url
    last_err = None
    for attempt in range(3):   # 3 attempts total
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            break
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            last_err = e
            if attempt == 2:
                # Final attempt failed — surface as a structured «pending» so
                # the caller treats it as «check again later» instead of a
                # hard failure. The reaper in /seedance/poll will eventually
                # mark it failed if the job genuinely never recovers.
                print(f'[avai-status] {job_id} network fail after 3 tries: {e.__class__.__name__}: {str(e)[:200]}', flush=True)
                return {'status': 'pending', 'progress': None, 'video_url': '', 'cost': None,
                        'error': f'transient network error: {e.__class__.__name__}', 'raw': {}}
            time.sleep(1.5 * (attempt + 1))   # 1.5s, 3s
    if not resp.ok:
        return {'status': 'error', 'error': f'{resp.status_code}: {resp.text[:200]}'}
    data = resp.json()
    status = data.get('status', 'pending').lower()
    video_url = ''
    # AVAI returns mp4 URLs in `images` (yes, the field is named that) for seedance2.
    # Also try `videos`/`outputs` for forward-compat.
    candidates = data.get('images') or data.get('videos') or data.get('outputs') or []
    if candidates:
        first = candidates[0]
        if isinstance(first, dict):
            video_url = first.get('url') or ''
        elif isinstance(first, str):
            video_url = first
    if not video_url:
        video_url = data.get('video_url') or data.get('output_url') or ''
    # cost can be a number or a dict {estimated_cost_usd: ...}
    cost_raw = data.get('cost')
    if isinstance(cost_raw, dict):
        cost_val = cost_raw.get('estimated_cost_usd') or cost_raw.get('cost') or cost_raw.get('total')
    else:
        cost_val = cost_raw
    progress = data.get('progress')
    if status == 'completed':
        progress = 100
    return {
        'status': status,
        'progress': progress,
        'video_url': video_url,
        'cost': cost_val,
        'error': data.get('error'),
        'raw': data,
    }

def _avai_upload_local_image(local_path: Path) -> str:
    """Upload a local image file to AVAI public storage, return the public URL.
    Used when an asset has only a local ref_image and we need a URL for Seedance."""
    import base64, mimetypes
    if not local_path.exists():
        raise RuntimeError(f'file not found: {local_path}')
    mime = mimetypes.guess_type(str(local_path))[0] or 'image/png'
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        mime = 'image/png'
    b64 = base64.b64encode(local_path.read_bytes()).decode('ascii')
    headers = {'x-api-key': _get_user_avai_key(), 'content-type': 'application/json'}
    resp = requests.post(
        'https://avai-gen.com/api/public/upload-image',
        json={'image_base64': b64, 'mime_type': mime},
        headers=headers, timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f'AVAI upload {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    url = (
        data.get('url')
        or data.get('image_url')
        or (data.get('data') or {}).get('url')
        or ((data.get('images') or [{}])[0] or {}).get('url')
    )
    if not url:
        raise RuntimeError(f'AVAI upload: no url in response: {str(data)[:300]}')
    return url
