"""ffmpeg-based postprocessing for ElevenLabs music output.

Borrowed from shadow-founder-studio:
  - silence-trim at start + end (ElevenLabs stably emits 5–20s fade-in);
  - loudnorm to −14 LUFS (consistent volume across episodes);
  - active-duration analyzer → retry decision when track is mostly silent.

All ffmpeg invocations expect the binary in PATH; caller resolves the path via
shutil.which and passes it (so we don't repeat that lookup here).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


# Silence threshold: anything below -50dB counts as "silence" for trimming
# (matches reference). For active-duration analysis we use -40dB (slightly
# more permissive, since real music has dips).
_TRIM_THRESHOLD_DB = -50
_ACTIVE_THRESHOLD_DB = -40
_TRIM_MIN_SILENCE_S = 0.3


class PostprocessError(RuntimeError):
    pass


def mp3_to_wav(ffmpeg_bin: str, in_path: Path, out_path: Path,
               channels: int = 2, sample_rate: int = 44100) -> None:
    """Transcode ElevenLabs MP3 → WAV. Stereo by default — preserves the
    spatial mix that the model created (mono-downmix would flatten it)."""
    cmd = [
        ffmpeg_bin, '-y', '-i', str(in_path),
        '-ar', str(sample_rate),
        '-ac', str(channels),
        '-c:a', 'pcm_s16le',
        str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise PostprocessError(f'mp3→wav failed: {res.stderr[-1500:]}')


def silence_trim(ffmpeg_bin: str, in_path: Path, out_path: Path) -> None:
    """Trim leading and trailing silence (deterministic — no fade analysis)."""
    af = (
        f'silenceremove=start_periods=1:'
        f'start_duration={_TRIM_MIN_SILENCE_S}:'
        f'start_threshold={_TRIM_THRESHOLD_DB}dB,'
        f'areverse,'
        f'silenceremove=start_periods=1:'
        f'start_duration={_TRIM_MIN_SILENCE_S}:'
        f'start_threshold={_TRIM_THRESHOLD_DB}dB,'
        f'areverse'
    )
    cmd = [ffmpeg_bin, '-y', '-i', str(in_path), '-af', af, str(out_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise PostprocessError(
            f'silence_trim failed: {res.stderr[-1500:]}'
        )


def loudnorm(ffmpeg_bin: str, in_path: Path, out_path: Path,
             target_lufs: float = -14.0, lra: float = 4.0,
             true_peak: float = -1.5) -> None:
    """Single-pass loudnorm. Sufficient for stand-alone WAV files; we don't
    need two-pass measurement since output is consumed by humans, not chained
    into broadcast-graded pipeline."""
    af = f'loudnorm=I={target_lufs}:LRA={lra}:TP={true_peak}'
    cmd = [ffmpeg_bin, '-y', '-i', str(in_path), '-af', af,
           '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le', str(out_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise PostprocessError(
            f'loudnorm failed: {res.stderr[-1500:]}'
        )


_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)')
_MEAN_VOL_RE = re.compile(r'mean_volume:\s*(-?\d+\.?\d*)\s*dB')
_MAX_VOL_RE = re.compile(r'max_volume:\s*(-?\d+\.?\d*)\s*dB')


def probe_duration(ffprobe_bin: str | None, path: Path) -> float:
    """Returns duration in seconds, 0.0 on failure."""
    if not ffprobe_bin:
        return 0.0
    try:
        out = subprocess.check_output(
            [ffprobe_bin, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', str(path)],
            text=True, timeout=10,
        )
        return float((out or '').strip() or 0.0)
    except Exception:
        return 0.0


def analyze_activity(ffmpeg_bin: str, path: Path,
                     target_duration_s: float) -> dict:
    """Coarse 'is the track actually playing?' check. Uses volumedetect filter
    to get mean/max dB; combined with duration we infer whether ElevenLabs
    emitted something usable or a dead track.

    Returns: {
      duration_s, mean_dB, max_dB,
      ok: bool,           # passes minimum quality bar
      reason: str,        # why ok=False (or empty)
    }
    """
    dur = probe_duration_ff(ffmpeg_bin, path)
    cmd = [ffmpeg_bin, '-i', str(path), '-af', 'volumedetect',
           '-f', 'null', '-']
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    stderr = res.stderr or ''
    mean_db = -100.0
    max_db = -100.0
    m1 = _MEAN_VOL_RE.search(stderr)
    if m1:
        try: mean_db = float(m1.group(1))
        except Exception: pass
    m2 = _MAX_VOL_RE.search(stderr)
    if m2:
        try: max_db = float(m2.group(1))
        except Exception: pass

    # Heuristics:
    #  - duration must cover at least 80% of target;
    #  - max_dB above -25dB means real signal (mostly-silent tracks come back
    #    with max around -40dB);
    #  - mean above _ACTIVE_THRESHOLD_DB means broadly active throughout.
    ok = True
    reason = ''
    if target_duration_s > 0 and dur < target_duration_s * 0.8:
        ok = False
        reason = f'short: dur={dur:.1f}s vs target={target_duration_s:.1f}s'
    elif max_db < -25.0:
        ok = False
        reason = f'flat: max_dB={max_db:.1f} (likely silent)'
    elif mean_db < _ACTIVE_THRESHOLD_DB - 5:
        ok = False
        reason = f'low_mean: mean_dB={mean_db:.1f}'
    return {
        'duration_s': dur,
        'mean_dB': mean_db,
        'max_dB': max_db,
        'ok': ok,
        'reason': reason,
    }


def probe_duration_ff(ffmpeg_bin: str, path: Path) -> float:
    """Fallback duration probe via ffmpeg stderr (works without ffprobe)."""
    res = subprocess.run(
        [ffmpeg_bin, '-i', str(path), '-f', 'null', '-'],
        capture_output=True, text=True, timeout=60,
    )
    m = _DURATION_RE.search(res.stderr or '')
    if not m:
        return 0.0
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def concat_wavs(ffmpeg_bin: str, parts: list[Path], out_path: Path) -> None:
    """Concatenate multiple WAVs into one (lossless, no re-encode).
    Caller ensures parts are mono 44.1k 16-bit (our pipeline always produces this)."""
    if not parts:
        raise PostprocessError('concat_wavs: empty parts')
    list_file = out_path.with_suffix('.concat.txt')
    list_file.write_text(
        '\n'.join(f"file '{str(p)}'" for p in parts),
        encoding='utf-8',
    )
    try:
        cmd = [ffmpeg_bin, '-y', '-f', 'concat', '-safe', '0',
               '-i', str(list_file), '-c', 'copy', str(out_path)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            # fallback to re-encode if codec drift
            cmd2 = [ffmpeg_bin, '-y', '-f', 'concat', '-safe', '0',
                    '-i', str(list_file),
                    '-ar', '44100', '-ac', '1', '-c:a', 'pcm_s16le',
                    str(out_path)]
            res2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
            if res2.returncode != 0:
                raise PostprocessError(
                    f'concat failed: {res.stderr[-1000:]} | reencode: {res2.stderr[-1000:]}'
                )
    finally:
        try: list_file.unlink(missing_ok=True)
        except Exception: pass


def make_silence_wav(ffmpeg_bin: str, out_path: Path, duration_s: float) -> None:
    """Generate a silent mono 44.1k WAV of the given length — used as filler
    when concatenating partial music (some scenes failed)."""
    duration_s = max(0.1, float(duration_s))
    cmd = [ffmpeg_bin, '-y',
           '-f', 'lavfi', '-i', 'anullsrc=cl=stereo:r=44100',
           '-t', f'{duration_s:.3f}',
           '-c:a', 'pcm_s16le', '-ac', '2', '-ar', '44100', str(out_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise PostprocessError(f'silence wav failed: {res.stderr[-800:]}')
