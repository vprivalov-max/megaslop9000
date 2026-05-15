"""Thin client for ElevenLabs Music API.

ONE endpoint — POST /v1/music — instrumental cinematic-score generation from
a Claude-built composition_plan. We request MP3 (44.1 kHz, 192 kbps stereo),
let ffmpeg transcode to WAV downstream — that way we don't have to guess
channel count / sample format from raw PCM (an early version assumed mono
44.1k and the actual stereo output played back at half speed → "broken
ambient" complaint).

Reference behaviour borrowed from shadow-founder-studio:
  - never pass `music_length_ms` when sending composition_plan (422 otherwise);
  - never pass `force_instrumental` with a plan (instrumentality enforced by
    `lines: []` in every section);
  - `respect_sections_durations=false` for blended transitions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

ELEVENLABS_MUSIC_URL = 'https://api.elevenlabs.io/v1/music'
ELEVENLABS_OUTPUT_FORMAT = 'mp3_44100_192'  # stereo MP3 — transcoded to WAV by ffmpeg


class ElevenLabsMusicError(RuntimeError):
    pass


def generate_music(
    api_key: str,
    composition_plan: dict,
    out_path: Path,
    seed: int | None = None,
    timeout: int = 300,
) -> dict:
    """Generate one instrumental track, write MP3 bytes to out_path.

    Caller is responsible for transcoding MP3 → WAV via ffmpeg before further
    postprocessing (silence-trim / loudnorm operate on WAV).

    Returns: {'request_id': str, 'duration_ms_expected': int, 'bytes': int}.
    Raises ElevenLabsMusicError on HTTP failure or empty response.
    """
    if not api_key:
        raise ElevenLabsMusicError('ELEVENLABS_KEY_MISSING: ELEVENLABS_API_KEY не задан')

    sections = composition_plan.get('sections') or []
    expected_ms = sum(int(s.get('duration_ms') or 0) for s in sections)
    if expected_ms <= 0:
        raise ElevenLabsMusicError('composition_plan.sections is empty or zero-duration')

    payload = {
        'composition_plan': composition_plan,
        'respect_sections_durations': False,
    }
    if seed is not None:
        payload['seed'] = int(seed)

    headers = {
        'xi-api-key': api_key,
        'Content-Type': 'application/json',
        'Accept': 'audio/mpeg',
    }
    params = {'output_format': ELEVENLABS_OUTPUT_FORMAT}

    t0 = time.time()
    print(f'[elevenlabs-music] POST /v1/music expected_ms={expected_ms} seed={seed} fmt={ELEVENLABS_OUTPUT_FORMAT}', flush=True)

    try:
        resp = requests.post(
            ELEVENLABS_MUSIC_URL,
            params=params,
            headers=headers,
            data=json.dumps(payload),
            stream=True,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise ElevenLabsMusicError(f'network error: {e}') from e

    request_id = resp.headers.get('request-id') or resp.headers.get('x-request-id') or ''

    if resp.status_code != 200:
        body_preview = ''
        try:
            body_preview = resp.text[:500]
        except Exception:
            pass
        raise ElevenLabsMusicError(
            f'elevenlabs HTTP {resp.status_code}: {body_preview}'
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_bytes = 0
    with open(out_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                n_bytes += len(chunk)
    if n_bytes < 1024:
        try: out_path.unlink(missing_ok=True)
        except Exception: pass
        raise ElevenLabsMusicError(f'response too small: {n_bytes} bytes')

    elapsed = time.time() - t0
    print(
        f'[elevenlabs-music] OK {n_bytes/1024:.0f}KB in {elapsed:.1f}s req_id={request_id}',
        flush=True,
    )
    return {
        'request_id': request_id,
        'duration_ms_expected': expected_ms,
        'bytes': n_bytes,
    }
